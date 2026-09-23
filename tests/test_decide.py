"""`nightshift.decide` — reading a card's `## Question`, and recording the answer.

Two things are worth pinning here, and neither is the happy path.

**The parser must not invent a picker, and must not miss one.** Both failures were live
in the digest before this module existed, on real cards: options written `1.`/`2.`
parsed to nothing, and any emphasised prose (`**What I found:** …`) was mistaken for a
sub-question and listed where the real options should have been. Both directions are
tested against the exact shapes that produced them.

**The recorded answer is verbatim and machine-recognisable.** The convention only works
whether a reader can tell the maintainer's answer from an agent's note — that is what
`[board].decision_attributor` is for — and if the words the worker reads at 3 AM are the
words that were chosen. A paraphrase here is worse than no feature.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from nightshift import board, decide, worker_prompt
from nightshift import dispatch, review


_CARD = """---
id: {id}
title: A parked card
tier: worker
worker: code-thread
recipe: none
unattended: true
verify: review
after_answer: {route}
created: 2026-08-18
---

## Intent

Something is undecided.

## Acceptance

- decided

## Open questions

{open}

## Question

{question}
{tail}"""


def _repo(tmp_path: Path, question: str, *, card_id: str = "parked",
          attributor: str = "karel", open_questions: str = "none",
          tail: str = "", route: str = "tasks") -> Path:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "probe"\n\n[board]\n'
        f'decision_attributor = "{attributor}"\n', encoding="utf-8")
    lane = tmp_path / "Board" / "needs-decision"
    lane.mkdir(parents=True, exist_ok=True)
    text = _CARD.format(id=card_id, question=question, open=open_questions, tail=tail,
                        route=route)
    if not route:                                  # a card written before the field
        text = text.replace("after_answer: \n", "")
    (lane / f"{card_id}.md").write_text(text, encoding="utf-8")
    return tmp_path


def _question(text: str) -> str:
    return _CARD.format(id="x", open="none", tail="", question=text, route="tasks")


def _text(root: Path, card_id: str = "parked") -> str:
    return board.find(root, card_id).text


def _lane(root: Path, card_id: str = "parked") -> str:
    return board.find(root, card_id).lane


# ---------------------------------------------------------------------- parsing

def test_bullet_options_are_found_with_their_consequence_attached():
    subs = decide.parse(_question(
        "- **A** — do it now, cheaper\n- **B** — wait for phase 5\n"))
    assert len(subs) == 1
    assert [o.text for o in subs[0].options] == [
        "**A** — do it now, cheaper", "**B** — wait for phase 5"]


def test_numbered_options_are_found_too():
    """The failure this module was extracted for. The digest's `_LIST_ITEM` matched only
    `-`/`*`, so a worker that parked a card with `1.` / `2.` options — the natural way
    to enumerate two choices — produced a card the reader reported as having none, and
    a picker built on it would have offered nothing to pick."""
    subs = decide.parse(_question(
        "1. File it in the other repo\n2. Relax the fence for this one\n"))
    assert [o.text for o in subs[0].options] == [
        "File it in the other repo", "Relax the fence for this one"]


def test_an_option_wrapped_over_two_lines_is_one_whole_option():
    subs = decide.parse(_question(
        "- **A** — do it now, which costs a rework\n  once phase 5 lands\n"))
    assert subs[0].options[0].text == (
        "**A** — do it now, which costs a rework once phase 5 lands")


def test_bold_prose_is_not_mistaken_for_a_sub_question():
    """`command-center-back-buttons`, exactly. Two bold spans of narration and two
    numbered options; the old rule ("a line starting with `**` heads a question")
    listed the narration as the choices and dropped both real ones."""
    subs = decide.parse(_question(
        "**What I found:** the code lives in the other repo.\n\n"
        "**This needs a decision:** options —\n\n"
        "1. File it as a card in that repo\n"
        "2. Relax the fence just here\n"))
    assert len(subs) == 1, "narration must not split the picker"
    assert [o.text for o in subs[0].options] == [
        "File it as a card in that repo", "Relax the fence just here"]


def test_a_multi_decision_card_keeps_its_sub_questions_apart():
    """Three batched decisions, each headed by a bold sentence that *wraps* — which is
    how every real one is written, and what a single-line bold pattern misses."""
    subs = decide.parse(_question(
        "**How should this be sequenced, given phase 6 is blocked on\n"
        "phase 5?**\n\n- A — now\n- B — split it\n\n"
        "**Does the licensing bar apply to audio too?**\n\n"
        "- Yes\n- No\n"))
    assert len(subs) == 2
    assert subs[0].prompt.startswith("How should this be sequenced")
    assert "**" not in subs[0].prompt, "the heading's own markers are not content"
    assert [o.text for o in subs[1].options] == ["Yes", "No"]


def test_the_recommended_marker_is_read_without_eating_the_option_label():
    """A loose "asterisks around the word" pattern swallows the closing `**` of
    `- **B — split it** *(recommended)*`, leaving unbalanced markdown that renders as
    literal asterisks in the form and in the recorded answer."""
    subs = decide.parse(_question(
        "- **A — now**\n- **B — split it** *(recommended)*\n"))
    a, b = subs[0].options
    assert not a.recommended and b.recommended
    assert b.text == "**B — split it**"
    assert b.text.count("**") % 2 == 0
    # A reader quotes the card, so it keeps the mark the form strips.
    assert "recommended" in b.raw


def test_a_decide_heading_makes_only_its_own_options_count():
    """`runner-worker-handover`, in shape: a bulleted list of findings under a bold lead-in,
    above the real choice. The guessing parser offered the findings as options. Once the
    section carries `### Decide:`, everything above it is context, however it is formatted."""
    subs = decide.parse(_question(
        "**Why it cannot today** — two causes:\n\n"
        "- the session id is dropped\n- the branch is renamed\n\n"
        "### Decide: Resume the worker's session or start fresh?\n\n"
        "- **A — resume** *(recommended)* — keeps its context\n"
        "- **B — fresh** — costs a re-read\n"))
    assert len(subs) == 1
    assert subs[0].prompt == "Resume the worker's session or start fresh?"
    assert [o.text for o in subs[0].options] == [
        "**A — resume** — keeps its context", "**B — fresh** — costs a re-read"]
    assert [o.recommended for o in subs[0].options] == [True, False]


def test_each_decide_heading_is_its_own_sub_question():
    subs = decide.parse(_question(
        "### Decide: Which screens?\n\n- **A — all**\n- **B — hub only**\n\n"
        "### Decide: If B, keep the old art?\n\n- **Yes**\n- **No**\n"))
    assert [s.prompt for s in subs] == ["Which screens?", "If B, keep the old art?"]
    assert [o.text for o in subs[1].options] == ["**Yes**", "**No**"]


def test_under_a_decide_heading_nested_detail_stays_inside_its_option():
    """The guessing parser splits a nested bullet into an option of its own. Under the
    marker the shape is known, so it is detail and folds into the option above."""
    subs = decide.parse(_question(
        "### Decide: Which?\n\n- **A — now**\n  - costs a rework\n- **B — later**\n"))
    assert [o.text for o in subs[0].options] == [
        "**A — now** - costs a rework", "**B — later**"]


def test_a_later_heading_ends_a_decide_block():
    subs = decide.parse(_question(
        "### Decide: Which?\n\n- **A**\n- **B**\n\n### Notes\n\n- not an option\n"))
    assert [o.text for o in subs[0].options] == ["**A**", "**B**"]


def test_a_decide_heading_with_no_options_offers_no_empty_picker():
    assert decide.parse(_question("### Decide: What should regen do?\n\nNo idea yet.\n")) == []


def test_options_inline_in_a_sentence_are_not_a_picker():
    """`OPTIONS: (A) … (B) …` is what the reviewer's one-line JSON `question` produced on
    `end-of-turn-events`. Pinned so the fix stays the shape the prompts teach, not a guess
    at inline letters in prose."""
    assert decide.parse(_question("OPTIONS: (A) keep it. (B) tick only.\n")) == []


def test_the_example_every_agent_is_shown_parses_as_a_picker():
    """The drift guard between the prompt and the parser. If `QUESTION_EXAMPLE` stopped
    parsing, every agent that parks a card would be taught to write an unreadable one."""
    assert worker_prompt.QUESTION_EXAMPLE in worker_prompt.QUESTION_FORMAT
    subs = decide.parse(_question(worker_prompt.QUESTION_EXAMPLE))
    assert len(subs) == 1 and subs[0].prompt
    assert len(subs[0].options) == 2
    assert [o.recommended for o in subs[0].options] == [True, False]


def test_every_prompt_that_can_park_a_card_carries_the_format():
    for name, template in [("dispatch._PROMPT", dispatch._PROMPT),
                           ("review._REVIEW_PROMPT", review._REVIEW_PROMPT),
                           ("review._BATCH_REVIEW_PROMPT", review._BATCH_REVIEW_PROMPT),
                           ("INTERACTIVE_CARD", worker_prompt.INTERACTIVE_CARD),
                           ("INTERACTIVE_CARD_FEEDBACK", worker_prompt.INTERACTIVE_CARD_FEEDBACK)]:
        assert "{question_format}" in template, name


@pytest.mark.parametrize("template", ["agents/triage.md", "agents/code-reviewer.md",
                                      "skills/manage-board/SKILL.md"])
def test_every_charter_that_describes_the_question_teaches_the_marker(template):
    """Charters are rendered into consuming repos, so they cannot import the constant;
    they can only be held to naming the marker the parser keys on."""
    text = (Path(decide.__file__).parent / "templates" / template).read_text(encoding="utf-8")
    assert "### Decide:" in text


def test_prose_only_question_parses_to_nothing_rather_than_failing():
    """A card that asks in prose must still be answerable — in free text. Those are the
    hard questions, and refusing them would invert the point of the feature."""
    assert decide.parse(_question(
        "Should regen tick before or after the enemy acts?\n")) == []


def test_a_card_with_no_question_section_parses_to_nothing():
    assert decide.parse("---\nid: x\n---\n\n## Intent\n\nnothing asked\n") == []


# ---------------------------------------------------------------------- writing

def test_the_answer_is_recorded_verbatim_dated_and_attributed(tmp_path):
    root = _repo(tmp_path, "- **A** — now\n- **B** — later\n")
    decide.write_answer(root, "parked", ["**B** — later"], "",
                        today=dt.date(2026, 8, 18))
    text = _text(root)
    assert "## Thread" in text
    assert "### 2026-08-18 · karel" in text
    assert "> **B** — later" in text


def test_the_recorded_answer_is_what_the_reader_looks_for(tmp_path):
    """The convention is only worth following if the answered-but-not-moved nudge fires
    on it — that is the thing standing between an answered card and a card that sits
    parked forever because everyone assumed someone had moved it."""
    root = _repo(tmp_path, "- A\n- B\n")
    decide.write_answer(root, "parked", ["B"], "", today=dt.date(2026, 8, 18))
    card = board.find(root, "parked")
    assert decide.has_maintainer_answer(card.text, "karel")


def test_a_free_text_note_is_quoted_as_the_maintainers_own_words(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n")
    decide.write_answer(root, "parked", [""], "Neither — drain heat instead.",
                        today=dt.date(2026, 8, 18))
    assert "> Neither — drain heat instead." in _text(root)


def test_a_partial_answer_records_only_what_was_decided(tmp_path):
    root = _repo(tmp_path, "**One?**\n\n- A1\n- B1\n\n**Two?**\n\n- A2\n- B2\n")
    decide.write_answer(root, "parked", ["A1", ""], "", today=dt.date(2026, 8, 18))
    text = _text(root)
    assert "> A1" in text
    assert "> A2" not in text and "> B2" not in text


def test_an_existing_thread_is_appended_to_not_replaced(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n",
                 tail="\n## Thread\n\n> the original worry\n")
    decide.write_answer(root, "parked", ["A"], "", today=dt.date(2026, 8, 18))
    text = _text(root)
    assert "> the original worry" in text
    # By heading, not by bare date: the frontmatter carries `created: 2026-08-18`.
    assert text.index("the original worry") < text.index("### 2026-08-18")


def test_a_new_thread_is_placed_before_telemetry(tmp_path):
    """`## Telemetry` is appended by the runner and is always last; a Thread written
    after it would read as part of the machine's own report."""
    root = _repo(tmp_path, "- A\n- B\n", tail="\n## Telemetry\n\n- **attempt 1**\n")
    decide.write_answer(root, "parked", ["A"], "", today=dt.date(2026, 8, 18))
    text = _text(root)
    assert text.index("## Thread") < text.index("## Telemetry")


def test_the_card_is_not_moved(tmp_path):
    """Park-over-promote. An answer can *open* a question, so advancing the card is a
    separate, deliberate click — never a side effect of recording one."""
    root = _repo(tmp_path, "- A\n- B\n")
    decide.write_answer(root, "parked", ["A"], "", today=dt.date(2026, 8, 18))
    assert (root / "Board" / "needs-decision" / "parked.md").is_file()
    assert not (root / "Board" / "tasks" / "parked.md").exists()


def test_the_card_keeps_lf_endings(tmp_path):
    """`pathlib.write_text` converts a card to CRLF on Windows, which reddens the
    `line_endings` gate and leaves changes `normalize_worktree` will not touch."""
    root = _repo(tmp_path, "- A\n- B\n")
    decide.write_answer(root, "parked", ["A"], "", today=dt.date(2026, 8, 18))
    assert b"\r\n" not in (root / "Board" / "needs-decision" / "parked.md").read_bytes()


def test_an_undeclared_attributor_refuses_rather_than_guessing(tmp_path):
    """A guessed token makes the recognition silently never fire while reporting a
    clean board — worse than not having the check at all."""
    root = _repo(tmp_path, "- A\n- B\n", attributor="")
    with pytest.raises(decide.DecideError, match="decision_attributor"):
        decide.write_answer(root, "parked", ["A"], "")


def test_an_empty_answer_is_refused(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n")
    with pytest.raises(decide.DecideError, match="nothing to record"):
        decide.write_answer(root, "parked", ["", ""], "   ")


def test_an_unknown_card_is_refused(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n")
    with pytest.raises(decide.DecideError, match="no card"):
        decide.write_answer(root, "ghost", ["A"], "")


# ------------------------------------------------------- the promotion guard

def test_open_questions_none_is_the_gate_for_sending_to_tasks(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n", open_questions="none")
    assert decide.open_questions_settled(_text(root))


def test_a_card_with_a_live_open_question_is_not_promotable(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n",
                 open_questions="- what happens to the old saves?")
    assert not decide.open_questions_settled(_text(root))


@pytest.mark.parametrize("body", [
    "none",
    "none.",
    "- none",
    "* none",
    "**none**",
    "none — both questions were resolved on 2026-08-14 (see `## Decisions`), and "
    "both resolved to \"no behaviour change\".",
    "None — ready to execute.",
    "- none, this sequences after [[grid-distance-metric]]",
])
def test_none_with_the_reason_attached_is_still_none(tmp_path, body):
    """`card_schema` has always accepted any body *beginning* with `none`, and the
    corpus writes it that way — `.ai/recipes/add-a-perk.md` teaches `none — ready to
    execute`. This predicate demanded the body be *exactly* `none`, so
    `grid-distance-ranged-and-ai-sites` passed every gate while `Send to tasks` stayed
    greyed out, reporting "open questions are not none" about a section reading `none`
    (2026-08-23). One rule now, in `board`, for the gate and the button both.
    """
    root = _repo(tmp_path, "- A\n- B\n", open_questions=body)
    assert decide.open_questions_settled(_text(root))


@pytest.mark.parametrize("body", [
    "- what happens to the old saves?",
    "**1.** whether the range numbers stay — none of the options is obviously right",
    "nonetheless, the spawn band still needs a number",   # not the word `none`
])
def test_a_body_that_does_not_open_with_none_is_not_settled(tmp_path, body):
    root = _repo(tmp_path, "- A\n- B\n", open_questions=body)
    assert not decide.open_questions_settled(_text(root))


def test_the_gate_and_the_button_cannot_disagree_about_none():
    """The two callers of the rule are `card_schema` (which refuses the misfiled card)
    and the panel (which refuses to create one). They are only safe as a pair while
    they are the same predicate, so this asserts the delegation itself.
    """
    from nightshift import board as board_mod
    from nightshift.gates import card_schema

    assert decide.open_questions_settled.__module__ == "nightshift.decide"
    text = "## Open questions\n\nnone — resolved on 2026-08-14.\n"
    assert board_mod.open_questions_settled(text)
    assert decide.open_questions_settled(text)
    assert not hasattr(card_schema, "_section_body"), (
        "card_schema must read the section through board, not its own copy")


# ------------------------------------------- after_answer: the resume route
#
# Karel, 2026-08-23: two ways a card reaches needs-decision/ — triage cannot scope it
# without an answer, or a scoped card hit one ambiguity — and they resume by opposite
# routes. The field says which; these pin what each route does at the promotion.

def test_a_settled_card_is_promoted_whatever_its_route_says(tmp_path):
    """`Open questions: none` is the gate `card_schema` actually enforces, so a card
    that satisfies it moves. The route is the parker's expectation, not a lock."""
    root = _repo(tmp_path, "- A\n- B\n", open_questions="none", route="triage")
    decide.promote_to_tasks(root, "parked")
    assert _lane(root) == "tasks"


def test_an_answered_tasks_card_settles_its_own_questions_on_the_way(tmp_path):
    """The step that had no owner. `after_answer: tasks` says an answer makes the card
    dispatchable — but recording one leaves `## Open questions` alone, and `card_schema`
    refuses a live question in `tasks/`. So the promotion settles it, keeping the
    question as history rather than deleting it."""
    root = _repo(tmp_path, "- A\n- B\n", route="tasks",
                 open_questions="- should the damage be 3-6 or 4-7?")
    decide.write_answer(root, "parked", ["A"], "", today=dt.date(2026, 8, 23))
    decide.promote_to_tasks(root, "parked", today=dt.date(2026, 8, 23))

    text = _text(root)
    assert _lane(root) == "tasks"
    assert board.open_questions_settled(text)
    assert "none — answered on 2026-08-23" in text
    assert "> - should the damage be 3-6 or 4-7?" in text, "the question is kept"


def test_a_tasks_card_is_not_promoted_before_it_is_answered(tmp_path):
    """Otherwise the settling would erase a live question nobody answered."""
    root = _repo(tmp_path, "- A\n- B\n", route="tasks",
                 open_questions="- should the damage be 3-6 or 4-7?")
    with pytest.raises(decide.DecideError, match="no answer of yours is on record"):
        decide.promote_to_tasks(root, "parked")
    assert _lane(root) == "needs-decision"


def test_a_triage_card_with_a_live_question_is_refused_with_its_route(tmp_path):
    """It has no `## Approach` for a worker to follow — that is why it was parked."""
    root = _repo(tmp_path, "- A\n- B\n", route="triage",
                 open_questions="- which of the two engines?")
    decide.write_answer(root, "parked", ["A"], "", today=dt.date(2026, 8, 23))
    with pytest.raises(decide.DecideError, match="re-triage"):
        decide.promote_to_tasks(root, "parked")
    assert _lane(root) == "needs-decision"


def test_a_card_with_no_route_and_a_live_question_says_so(tmp_path):
    """Written before the field existed: refuse, and say the route is what is missing
    rather than picking one on the card's behalf."""
    root = _repo(tmp_path, "- A\n- B\n", route="",
                 open_questions="- which of the two engines?")
    with pytest.raises(decide.DecideError, match="does not declare an `after_answer:`"):
        decide.promote_to_tasks(root, "parked")


def test_a_promoted_chore_at_its_attempt_cap_gets_a_fresh_budget(tmp_path):
    """`chores.py` charges a chore's one attempt the moment it parks to
    `needs-decision/` (see `run_one`'s docstring). Promoted without a reset it would
    sit in `tasks/` looking dispatchable while `chores.eligible()` refused it — found
    2026-08-26 for `docs-production`. The answer earns it a fresh budget instead.
    """
    from nightshift import chores
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "probe"\n\n[board]\ndecision_attributor = "karel"\n',
        encoding="utf-8")
    lane = tmp_path / "Board" / "needs-decision"
    lane.mkdir(parents=True, exist_ok=True)
    (lane / "a-chore.md").write_text(
        "---\n"
        "id: a-chore\n"
        "title: A parked chore\n"
        "tier: worker\n"
        "worker: code-thread\n"
        "kind: chore\n"
        "unattended: true\n"
        "verify: review\n"
        "attempts: 1\n"
        "created: 2026-08-18\n"
        "---\n\n"
        "## Intent\n\nSomething.\n\n"
        "## Acceptance\n\n- decided\n\n"
        "## Open questions\n\nnone\n\n"
        "## Question\n\nWhat was found.\n",
        encoding="utf-8")

    message = decide.promote_to_tasks(tmp_path, "a-chore")

    assert message == "a-chore → tasks/"
    card = board.find(tmp_path, "a-chore")
    assert card.attempts == 1 and card.retry_from == 1
    assert chores.eligible(card, capabilities=set()) == ""


def test_a_promoted_full_card_gets_no_chore_warning(tmp_path):
    """The warning is specific to `kind: chore` — an ordinary card must not grow it."""
    root = _repo(tmp_path, "- A\n- B\n", open_questions="none")
    message = decide.promote_to_tasks(root, "parked")
    assert message == "parked → tasks/"


def test_settling_twice_does_not_stack_two_headers(tmp_path):
    """`board.settle_open_questions` is idempotent — a card promoted, moved back and
    promoted again must not grow a second `none — answered on …` line."""
    once = board.settle_open_questions(
        "## Open questions\n\n- one thing\n", on="2026-08-23")
    twice = board.settle_open_questions(once, on="2026-08-24")
    assert once == twice
    assert twice.count("none — answered on") == 1


# --------------------------------------------------------- closing as done

def test_closing_records_the_answer_and_moves_the_card_to_done(tmp_path):
    """`close_parked` writes the answer and moves the card, through `board.move`."""
    root = _repo(tmp_path, "- Close as not-applicable\n- Re-file against the other repo\n")
    message = decide.close_parked(root, "parked", ["Close as not-applicable"], "",
                                  today=dt.date(2026, 8, 22))
    text = _text(root)
    assert "### 2026-08-22 · karel" in text
    assert "> Close as not-applicable" in text
    assert "state:" not in text
    assert (root / "Board" / "done" / "parked.md").is_file()
    assert not (root / "Board" / "needs-decision" / "parked.md").exists()
    assert "parked" in message and "done/" in message


def test_closing_with_only_a_note_still_records_it(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n")
    decide.close_parked(root, "parked", [""], "Not applicable here.",
                        today=dt.date(2026, 8, 22))
    assert "> Not applicable here." in _text(root)


def test_closing_with_nothing_ticked_or_typed_still_moves_it_to_done(tmp_path):
    """Unlike `write_answer`, an empty pick/note is not refused here — the card may
    already carry its answer from an earlier `Record the answer` click, and Close is
    then just the move half of the job."""
    root = _repo(tmp_path, "- A\n- B\n", tail="\n## Thread\n\n> already answered earlier\n")
    decide.close_parked(root, "parked", ["", ""], "", today=dt.date(2026, 8, 22))
    text = _text(root)
    assert "> already answered earlier" in text
    assert _lane(root) == "done"


def test_closing_a_card_not_in_needs_decision_is_refused(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n")
    text = _text(root)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "needs-decision" / "parked.md").unlink()
    (root / "Board" / "tasks" / "parked.md").write_text(text, encoding="utf-8")
    with pytest.raises(decide.DecideError, match="needs-decision"):
        decide.close_parked(root, "parked", ["A"], "")


def test_closing_an_unknown_card_is_refused(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n")
    with pytest.raises(decide.DecideError, match="no card"):
        decide.close_parked(root, "ghost", ["A"], "")


def test_closing_keeps_lf_endings(tmp_path):
    root = _repo(tmp_path, "- A\n- B\n")
    decide.close_parked(root, "parked", ["A"], "", today=dt.date(2026, 8, 22))
    assert b"\r\n" not in (root / "Board" / "done" / "parked.md").read_bytes()


# --------------------------------------------------------- reopening a re-parked card

def test_reopen_makes_open_questions_unsettled():
    """A worker-parked, `after_answer: tasks` card is born with `## Open questions:
    none` — it was fully scoped at triage — and nothing ever reopened it on a park,
    which made `_decide_state`'s dedicated `after_answer: tasks` banner unreachable
    (`enemy-position-knowledge`, 2026-09-05)."""
    text = decide.reopen(_question("- A\n- B\n"))
    assert not board.open_questions_settled(text)


def test_reopen_is_idempotent_on_open_questions():
    once = decide.reopen(_question("- A\n- B\n"))
    twice = decide.reopen(once)
    assert board.section(once, "Open questions") == board.section(twice, "Open questions")


def test_a_prior_rounds_answer_does_not_count_after_a_reopen():
    """The other half of the bug: re-parking a card that already carries a signed
    answer from a previous round must not read as answered again until a *new*
    answer is recorded. Before the marker, `has_maintainer_answer` searched the
    whole `## Thread` and saw the stale entry as answering the fresh question."""
    card = _question("- A\n- B\n") + "\n## Thread\n\n### 2026-09-04 · karel\n\n> A\n"
    assert decide.has_maintainer_answer(card, "karel")  # answered, before reopening

    reopened = decide.reopen(card)
    assert not decide.has_maintainer_answer(reopened, "karel")


def test_an_answer_recorded_after_reopening_does_count(tmp_path):
    card_id = "parked"
    root = _repo(tmp_path, "- A\n- B\n", card_id=card_id)
    decide.write_answer(root, card_id, ["A"], "", today=dt.date(2026, 9, 4))
    text = _text(root, card_id)
    assert decide.has_maintainer_answer(text, "karel")

    reopened = decide.reopen(text)
    (root / "Board" / "needs-decision" / f"{card_id}.md").write_text(
        reopened, encoding="utf-8")
    assert not decide.has_maintainer_answer(reopened, "karel")

    decide.write_answer(root, card_id, ["B"], "", today=dt.date(2026, 9, 5))
    assert decide.has_maintainer_answer(_text(root, card_id), "karel")


def test_a_card_with_no_marker_still_reads_over_the_whole_thread():
    """Backward compatible: the corpus of cards parked before this fix existed has
    no `_REOPENED_MARKER`, and their answer must still be recognised exactly as
    before."""
    card = _question("- A\n- B\n") + "\n## Thread\n\n### 2026-09-04 · karel\n\n> A\n"
    assert decide.has_maintainer_answer(card, "karel")


def test_a_thematic_break_ends_a_decide_block():
    """`---` under the options separates the live question from a round kept as
    history. With only `###` terminating the block, the archived round's bullets
    were folded in as further options — `show-weapon-schematic-stats`, 2026-09-16,
    where the picker offered two real choices plus four of a reviewer's
    verification observations."""
    card = """## Question

What is left is a display decision.

### Decide: How should the bonus be shown?

- **A — beside the row** *(recommended)* — matches the perks panel.
- **B — expand downward** — what is there today.

---

*Previous round, kept for the record:*

Verified by driving the picker headlessly:

- rows are at y = 401 / 427 / 453 / 479.
- selecting the rifle moves every one of them.
"""
    subs = decide.parse(card)
    assert len(subs) == 1
    assert [option.text[:1] for option in subs[0].options] == ["*", "*"]
    assert len(subs[0].options) == 2
    assert subs[0].options[0].recommended
    assert not any("y = 401" in option.text for option in subs[0].options)


def test_a_picker_below_a_stale_question_section_still_parses():
    """The end-to-end shape of the same bug: two `## Question` sections, the picker
    in the second. `board.section` joins them, so the `### Decide:` heading is found
    and the stale section's bullets stay context rather than becoming options."""
    card = """## Question

The reviewer found a defect across all attempts. Most recent finding:

- rows are at y = 401 / 427 / 453 / 479.
- a left click equips a weapon other than the one drawn highlighted.

## Feedback

Unify it with the existing standard.

## Question

What is left is a display decision.

### Decide: How should the bonus be shown?

- **A — beside the row** *(recommended)* — matches the perks panel.
- **B — expand downward** — what is there today.
"""
    subs = decide.parse(card)
    assert len(subs) == 1
    assert subs[0].prompt == "How should the bonus be shown?"
    assert len(subs[0].options) == 2
    assert not any("y = 401" in option.text for option in subs[0].options)


# ------------------------------------- retiring a picker that has been answered
#
# `show-weapon-schematic-stats`, 2026-09-17, third park on one question. Karel
# answered it, promoted the card, ran the batch; the worker reported "No answer to
# the `### Decide:` below has been recorded" and parked the same question back. The
# answer was in the prompt it was handed. Nothing had gone wrong mechanically — the
# card simply said both things at once, and the picker is the louder half.

def test_promoting_an_answered_card_retires_its_picker(tmp_path):
    """The fix. A card handed to a worker must not still be displaying the shape
    every worker is taught to read as an unanswered decision."""
    root = _repo(tmp_path, "### Decide: How should the bonus be shown?\n\n"
                           "- **A — beside the row** *(recommended)* — matches perks.\n"
                           "- **B — expand downward** — what is there today.\n",
                 route="tasks", open_questions="- where does the bonus go?")
    decide.write_answer(root, "parked", ["**A — beside the row** — matches perks."], "",
                        today=dt.date(2026, 9, 16))
    decide.promote_to_tasks(root, "parked", today=dt.date(2026, 9, 16))

    text = _text(root)
    assert "### Decide:" not in text, "the live picker must not survive the promotion"
    assert "### Decided (2026-09-16): How should the bonus be shown?" in text
    assert "Answered — see `## Thread`." in text


def test_a_retired_picker_keeps_every_option_it_offered(tmp_path):
    """Nothing is deleted, for `settle_open_questions`' reason: the options are what
    was chosen *between*, and without them the Thread entry quoting one is unreadable."""
    root = _repo(tmp_path, "### Decide: How should the bonus be shown?\n\n"
                           "- **A — beside the row** *(recommended)* — matches perks.\n"
                           "- **B — expand downward** — what is there today.\n",
                 route="tasks", open_questions="- where does the bonus go?")
    decide.write_answer(root, "parked", ["**A — beside the row** — matches perks."], "",
                        today=dt.date(2026, 9, 16))
    decide.promote_to_tasks(root, "parked", today=dt.date(2026, 9, 16))

    text = _text(root)
    assert "**A — beside the row** *(recommended)* — matches perks." in text
    assert "**B — expand downward** — what is there today." in text


def test_a_retired_picker_is_not_offered_to_be_answered_again():
    """`parse` returns nothing rather than falling through to the guessing parser,
    which would happily scrape the kept options and offer a settled decision twice."""
    assert decide.parse(_question(
        "### Decided (2026-09-16): How should the bonus be shown?\n\n"
        "> **Answered — see `## Thread`.**\n\n"
        "- **A — beside the row** — matches perks.\n"
        "- **B — expand downward** — what is there today.\n")) == []


def test_a_live_picker_beside_a_retired_one_is_still_offered():
    """A card answered once and parked again on something new: only the new question
    is pickable, and the retired block lends it no options."""
    subs = decide.parse(_question(
        "### Decided (2026-09-16): How should the bonus be shown?\n\n"
        "- **A — beside the row** — matches perks.\n"
        "- **B — expand downward** — what is there today.\n\n"
        "### Decide: What happens at the screen edge?\n\n"
        "- **A — flip to the other side** *(recommended)* — never clipped.\n"))
    assert len(subs) == 1
    assert subs[0].prompt == "What happens at the screen edge?"
    assert [o.text for o in subs[0].options] == [
        "**A — flip to the other side** — never clipped."]


def test_retiring_a_picker_is_idempotent():
    """`Decide\b` cannot match `Decided`, so a second promotion cannot double the
    marker or restamp the date onto a decision made a week earlier."""
    once = decide.mark_decided(_question("### Decide: Which one?\n\n- **A** — this.\n"),
                               on="2026-09-16")
    assert decide.mark_decided(once, on="2026-09-17") == once


def test_retiring_a_picker_leaves_the_rest_of_the_card_alone():
    """Scoped to `## Question` through `board.map_section`: a `### Decide:` quoted in
    the Thread or in a review finding is prose about a decision, not the picker."""
    card = _CARD.format(id="x", open="none", route="tasks",
                        question="### Decide: Which one?\n\n- **A** — this.\n",
                        tail="\n## Thread\n\nThe worker wrote `### Decide: Which one?`\n")
    out = decide.mark_decided(card, on="2026-09-16")
    assert "The worker wrote `### Decide: Which one?`" in out, "## Thread is not ours"
    assert "### Decided (2026-09-16): Which one?" in out


# --------------------------------------- the answer the dispatched worker is shown

def test_the_maintainers_answer_is_readable_back_verbatim(tmp_path):
    """`has_maintainer_answer` says *that* one exists; the runner has to quote it into
    the worker's prompt, and a pointer is one more thing that can be looked past."""
    root = _repo(tmp_path, "### Decide: Which one?\n\n- **A** — this.\n- **B** — that.\n")
    decide.write_answer(root, "parked", ["**A** — this."], "and keep it simple",
                        today=dt.date(2026, 9, 16))
    answer = decide.latest_answer(_text(root), "karel")
    assert answer.startswith("### 2026-09-16 · karel")
    assert "> **A** — this." in answer
    assert "> and keep it simple" in answer


def test_an_answer_from_a_previous_round_is_not_quoted_at_a_re_parked_card(tmp_path):
    """Same boundary `has_maintainer_answer` uses, for the same reason: a card parked
    again keeps the old answer as history, and quoting it would tell the next worker
    its new question had been answered."""
    root = _repo(tmp_path, "### Decide: Which one?\n\n- **A** — this.\n- **B** — that.\n")
    decide.write_answer(root, "parked", ["**A** — this."], "", today=dt.date(2026, 9, 16))
    reparked = decide.reopen(_text(root))
    assert decide.latest_answer(reparked, "karel") == ""


def test_a_worker_note_after_the_answer_does_not_run_into_the_quote(tmp_path):
    """The quote ends at the next `###`, so a dated Thread note the worker appended
    is not handed to the next one as part of what Karel said."""
    root = _repo(tmp_path, "### Decide: Which one?\n\n- **A** — this.\n- **B** — that.\n")
    decide.write_answer(root, "parked", ["**A** — this."], "", today=dt.date(2026, 9, 16))
    text = _text(root) + "\n### 2026-09-17 · attempt 3 (chore-thread)\n\nRe-checked cold.\n"
    answer = decide.latest_answer(text, "karel")
    assert "> **A** — this." in answer
    assert "Re-checked cold" not in answer


def test_a_card_with_no_answer_has_nothing_to_quote(tmp_path):
    root = _repo(tmp_path, "### Decide: Which one?\n\n- **A** — this.\n")
    assert decide.latest_answer(_text(root), "karel") == ""


def test_the_answered_prompt_block_names_the_answer_and_forbids_re_parking():
    """The other half of the fix, and it has to be the other half: the card no longer
    lies, and the prompt now says the true thing outright. A worker reading only one
    of the two still gets it right."""
    block = worker_prompt.ANSWERED.format(answer="### 2026-09-16 · karel\n\n> **A**")
    assert "> **A**" in block
    assert "do not park this decision again" in block


def test_a_card_still_asking_has_a_live_picker():
    assert decide.has_live_picker(_question("### Decide: Which one?\n\n- **A** — this.\n"))
    assert not decide.has_live_picker(_question(
        "### Decided (2026-09-16): Which one?\n\n- **A** — this.\n"))
