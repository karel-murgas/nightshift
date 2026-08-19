"""`nightshift.decide` — reading a card's `## Question`, and recording the answer.

Two things are worth pinning here, and neither is the happy path.

**The parser must not invent a picker, and must not miss one.** Both failures were live
in the digest before this module existed, on real cards: options written `1.`/`2.`
parsed to nothing, and any emphasised prose (`**What I found:** …`) was mistaken for a
sub-question and listed where the real options should have been. Both directions are
tested against the exact shapes that produced them.

**The recorded answer is verbatim and machine-recognisable.** The convention only works
if the digest can tell the maintainer's answer from an agent's note — that is what
`[board].decision_attributor` is for — and if the words the worker reads at 3 AM are the
words that were chosen. A paraphrase here is worse than no feature.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from nightshift import board, decide


_CARD = """---
id: {id}
title: A parked card
state: needs-decision
tier: worker
worker: code-thread
recipe: none
unattended: true
verify: review
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
          tail: str = "") -> Path:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "probe"\n\n[board]\n'
        f'decision_attributor = "{attributor}"\n', encoding="utf-8")
    lane = tmp_path / "Board" / "needs-decision"
    lane.mkdir(parents=True, exist_ok=True)
    (lane / f"{card_id}.md").write_text(
        _CARD.format(id=card_id, question=question, open=open_questions, tail=tail),
        encoding="utf-8")
    return tmp_path


def _question(text: str) -> str:
    return _CARD.format(id="x", open="none", tail="", question=text)


def _text(root: Path, card_id: str = "parked") -> str:
    return (root / "Board" / "needs-decision" / f"{card_id}.md").read_text(
        encoding="utf-8")


# ---------------------------------------------------------------------- parsing

def test_bullet_options_are_found_with_their_consequence_attached():
    subs = decide.parse(_question(
        "- **A** — do it now, cheaper\n- **B** — wait for phase 5\n"))
    assert len(subs) == 1
    assert [o.text for o in subs[0].options] == [
        "**A** — do it now, cheaper", "**B** — wait for phase 5"]


def test_numbered_options_are_found_too():
    """The failure this module was extracted for. `digest._LIST_ITEM` matched only
    `-`/`*`, so a worker that parked a card with `1.` / `2.` options — the natural way
    to enumerate two choices — produced a card the digest reported as having none, and
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
    # The digest quotes the card, so it keeps the mark the form strips.
    assert "recommended" in b.raw


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


def test_the_recorded_answer_is_what_the_digest_looks_for(tmp_path):
    """The convention is only worth following if the answered-but-not-moved nudge fires
    on it — that is the thing standing between an answered card and a card that sits
    parked forever because everyone assumed someone had moved it."""
    from nightshift import digest
    root = _repo(tmp_path, "- A\n- B\n")
    decide.write_answer(root, "parked", ["B"], "", today=dt.date(2026, 8, 18))
    card = board.find(root, "parked")
    assert digest._has_maintainer_answer(card, "karel")


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
    assert "state: needs-decision" in _text(root)


def test_the_card_keeps_lf_endings(tmp_path):
    """`pathlib.write_text` converts a card to CRLF on Windows, which reddens the
    `line_endings` gate and leaves changes `normalize_worktree` will not touch."""
    root = _repo(tmp_path, "- A\n- B\n")
    decide.write_answer(root, "parked", ["A"], "", today=dt.date(2026, 8, 18))
    assert b"\r\n" not in (root / "Board" / "needs-decision" / "parked.md").read_bytes()


def test_an_undeclared_attributor_refuses_rather_than_guessing(tmp_path):
    """A guessed token makes the digest's nudge silently never fire while reporting a
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
