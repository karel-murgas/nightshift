"""Tests for `nightshift.gates.card_schema` — the board's shape gate.

Every card here is **synthetic**, built in `tmp_path`. Pinning a gate test to a
real board would fail every time someone legitimately adds a card, which is the
"gate gets muted" failure mode: a gate that fires on normal use is a gate that
gets switched off.

**Provenance.** Written in Dungeoneer's `tests/test_board_schema.py` for the
Session E board and moved here by `framework-tests-live-in-the-wrong-repo`
(2026-08-08). One test stayed behind under that filename — *the real board
passes* — because that is a claim about a project's cards and asserts nothing
read against this package, which has no `Board/` at all. The hook contract half
moved to `test_hook_tier_guard.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nightshift.gates import card_schema


_GOOD = """---
id: {id}
title: "A card"
state: {lane}
tier: worker
worker: code-thread
recipe: none
unattended: true
verify: review
created: 2026-07-22
---

## Intent

Do a thing. Not another thing.

## Approach

Add one flag, read it in the one place that needs it, leave the rest alone.

## Steps

| # | Step | Files | Gate |
|---|---|---|---|
| 1 | do it | somewhere | a gate |

## Acceptance criteria

- it is done

## Open questions

None. Card is ready to execute.
"""


def _board(tmp_path: Path, lane: str, body: str, name: str = "probe") -> Path:
    """A repo-shaped tmp tree: the gate resolves `worker:`/`recipe:` against
    real directories, so those have to exist for a valid card to come out clean."""
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "code-thread.md").write_text("charter", encoding="utf-8")
    (tmp_path / ".ai" / "recipes").mkdir(parents=True)
    (tmp_path / ".ai" / "recipes" / "add-a-perk.md").write_text("spine", encoding="utf-8")
    lane_dir = tmp_path / "Board" / lane
    lane_dir.mkdir(parents=True)
    (lane_dir / f"{name}.md").write_text(body, encoding="utf-8")
    return tmp_path


def _rules(violations) -> str:
    return "\n".join(v.rule for v in violations)


# --- card_schema: the happy path ------------------------------------------

@pytest.mark.parametrize("lane", ["tasks", "review", "testing", "done", "failed"])
def test_well_formed_card_passes_in_every_actionable_lane(tmp_path, lane):
    root = _board(tmp_path, lane, _GOOD.format(id="probe", lane=lane))
    assert card_schema.check(root) == []


# --- card_schema: each rule fires -----------------------------------------

def test_id_mismatch_is_caught(tmp_path):
    body = _GOOD.format(id="something-else", lane="tasks")
    assert "does not match the filename stem" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


def test_lane_and_state_disagreeing_is_caught(tmp_path):
    """A half-done move: the file was `git mv`d but `state:` was not edited."""
    body = _GOOD.format(id="probe", lane="tasks")
    assert "half-done move" in _rules(card_schema.check(_board(tmp_path, "review", body)))


def test_a_model_name_in_the_tier_field_is_rejected(tmp_path):
    """§16: the rule is tier-based, never model-based. `tier: <a model>` is the
    exact mistake the 2026-07-22 violation came from, one level up."""
    body = _GOOD.format(id="probe", lane="tasks").replace("tier: worker", "tier: sonnet")
    assert "must be one of" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


def test_unknown_field_is_rejected(tmp_path):
    """03_board.md §2: a field nobody reads is a field the runner drops silently."""
    body = _GOOD.format(id="probe", lane="tasks").replace(
        "created: 2026-07-22", "created: 2026-07-22\nsome_invented_key: 1"
    )
    assert "unknown field" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


def test_unresolvable_worker_and_recipe_are_caught(tmp_path):
    body = _GOOD.format(id="probe", lane="tasks").replace(
        "worker: code-thread", "worker: nobody"
    ).replace("recipe: none", "recipe: no-such-recipe")
    rules = _rules(card_schema.check(_board(tmp_path, "tasks", body)))
    assert "has no charter" in rules
    assert "has no spine" in rules


def test_a_live_open_question_may_not_sit_in_tasks(tmp_path):
    """The rule that keeps the lanes meaningful. needs-decision/ is a success
    state (§13), so this is a misfiling, not a bad card."""
    body = _GOOD.format(id="probe", lane="tasks").replace(
        "None. Card is ready to execute.", "Should the damage be 3-6 or 4-7?"
    )
    assert "belongs in needs-decision/" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


def test_the_same_open_question_is_fine_in_needs_decision(tmp_path):
    body = _GOOD.format(id="probe", lane="needs-decision").replace(
        "None. Card is ready to execute.", "Should the damage be 3-6 or 4-7?"
    ).replace("## Open questions", "## Question\n\nAttempted X; ambiguous Y; either A or B.\n\n## Open questions")
    assert card_schema.check(_board(tmp_path, "needs-decision", body)) == []


def test_parked_card_without_a_question_section_is_caught(tmp_path):
    body = _GOOD.format(id="probe", lane="needs-decision")
    assert "parked card needs a `## Question`" in _rules(
        card_schema.check(_board(tmp_path, "needs-decision", body))
    )


# --- card_schema: the conditional Steps rule ------------------------------
# This is the rule the art card forced (03_board.md §7). Both directions matter:
# a card that names a worker or a recipe must NOT be made to restate it, and a
# card that names neither has nothing else carrying the decomposition.

def test_no_steps_is_fine_when_a_worker_or_recipe_carries_them(tmp_path):
    body = _GOOD.format(id="probe", lane="tasks")
    body = body[: body.index("## Steps")] + body[body.index("## Acceptance criteria"):]
    assert card_schema.check(_board(tmp_path, "tasks", body)) == []


def test_no_steps_is_caught_when_nothing_else_carries_them(tmp_path):
    body = _GOOD.format(id="probe", lane="tasks").replace("worker: code-thread", "worker: none")
    body = body[: body.index("## Steps")] + body[body.index("## Acceptance criteria"):]
    assert "nothing else carrying the decomposition" in _rules(
        card_schema.check(_board(tmp_path, "tasks", body))
    )


def test_bare_acceptance_heading_is_accepted(tmp_path):
    """The art card writes `## Acceptance`, the perk cards `## Acceptance
    criteria`. Rejecting the shorter one would have failed a good card."""
    body = _GOOD.format(id="probe", lane="tasks").replace("## Acceptance criteria", "## Acceptance")
    assert card_schema.check(_board(tmp_path, "tasks", body)) == []


# --- card_schema: the ## Approach rule (03_board.md §12b, tasks/ only) -----
# A `tasks/` card states the core of *how* the change works in one place a human
# reads first (the digest inlines it). Art/audio carry `## Subject` instead —
# their substance is visual — and the gate accepts either.

def test_a_tasks_card_without_approach_or_subject_is_caught(tmp_path):
    body = _GOOD.format(id="probe", lane="tasks")
    body = body[: body.index("## Approach")] + body[body.index("## Steps"):]
    assert "missing `## Approach`" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


def test_a_tasks_card_may_use_subject_in_place_of_approach(tmp_path):
    """An art/audio card's substance is what it depicts (`## Subject`), not a
    prose 'how' — so the gate accepts `## Subject` and does not force a card to
    invent an approach it has no use for."""
    body = _GOOD.format(id="probe", lane="tasks").replace("## Approach", "## Subject")
    assert card_schema.check(_board(tmp_path, "tasks", body)) == []


def test_approach_is_not_required_outside_tasks(tmp_path):
    """Only `tasks/` is gated: cards already in review/testing/done predate the
    rule and must not go retroactively red, and a card carries the section
    forward once it advances anyway."""
    body = _GOOD.format(id="probe", lane="done")
    body = body[: body.index("## Approach")] + body[body.index("## Steps"):]
    assert card_schema.check(_board(tmp_path, "done", body)) == []


def test_a_bare_note_in_inbox_passes(tmp_path):
    """`inbox/` is where Karel says "I want this, help me refine it". He never
    writes frontmatter — triage fits it — so a bare note must be legal."""
    root = _board(tmp_path, "inbox", "just a rough idea, no frontmatter at all\n")
    assert card_schema.check(root) == []


def test_partly_triaged_inbox_card_still_has_its_frontmatter_checked(tmp_path):
    """Optional is not the same as unchecked. Once triage has written
    frontmatter, a wrong `tier:` is a real defect even in inbox/."""
    body = _GOOD.format(id="probe", lane="inbox").replace("tier: worker", "tier: opus")
    assert "must be one of" in _rules(card_schema.check(_board(tmp_path, "inbox", body)))


def test_inbox_does_not_require_the_actionable_sections(tmp_path):
    body = _GOOD.format(id="probe", lane="inbox")
    body = body[: body.index("## Steps")]
    assert card_schema.check(_board(tmp_path, "inbox", body)) == []


def test_base_boards_kanban_order_is_tolerated(tmp_path):
    """03_board.md §2 tool-owned fields. Base Board writes `kanban_order` on
    every drag-reorder. If the gate rejected it, Karel using his own board would
    turn it red — and a gate that fires on normal use gets muted."""
    body = _GOOD.format(id="probe", lane="tasks").replace(
        "created: 2026-07-22", "created: 2026-07-22\nkanban_order: 3"
    )
    assert card_schema.check(_board(tmp_path, "tasks", body)) == []


# --- card_schema: body vs frontmatter on `unattended:` (2026-07-30) --------
#
# 03_board.md §2 contradicted itself for a week — its opening paragraph said art
# is `unattended: true`, its closing paragraph said `false` — and eight cards
# copied the losing half into their bodies while their frontmatter said the
# other thing. Karel found it by hand ("that parametr is given too often").

def test_body_contradicting_the_frontmatter_on_unattended_is_caught(tmp_path):
    body = _GOOD.format(id="probe", lane="tasks") + (
        "\n**Karel:** is this the picture he wanted — why `unattended: false`.\n"
    )
    rules = _rules(card_schema.check(_board(tmp_path, "tasks", body)))
    assert "body says `unattended: false`" in rules
    assert "frontmatter says `unattended: true`" in rules


def test_body_agreeing_with_the_frontmatter_is_fine(tmp_path):
    """The field may be discussed — a card explaining why it carries the value it
    carries is doing the right thing. Only disagreement is a defect."""
    body = _GOOD.format(id="probe", lane="tasks") + (
        "\nThis card is `unattended: true` because a wrong attempt goes red on the gates.\n"
    )
    assert card_schema.check(_board(tmp_path, "tasks", body)) == []


def test_the_word_unattended_without_a_value_is_not_a_claim(tmp_path):
    """Prose about the mechanism is not a self-declaration. Requiring the
    backticked `key: value` shape is what keeps the check free of false
    positives on cards that merely mention the flag."""
    body = _GOOD.format(id="probe", lane="tasks") + (
        "\nThe unattended flag is not the mechanism for this; `review/` is.\n"
    )
    assert card_schema.check(_board(tmp_path, "tasks", body)) == []


@pytest.mark.parametrize("lane", ["testing", "done", "failed"])
def test_a_dispatched_card_keeps_its_record(tmp_path, lane):
    """`unattended:` governs one decision — may the runner start this card. Once
    that decision is behind the card, prose about it is history, and rewriting a
    shipped card to agree with a rule clarified afterwards would erase the
    evidence that made the clarification necessary. `testing/ice-damage.md` and
    two `done/` cards are the real instances this protects."""
    body = _GOOD.format(id="probe", lane=lane) + (
        "\nJudgment items remain for Karel (`unattended: false`).\n"
    )
    assert card_schema.check(_board(tmp_path, lane, body)) == []


def test_the_frontmatter_block_itself_is_never_read_as_a_body_claim(tmp_path):
    """A `---` frontmatter fence appearing again later in the body (a horizontal
    rule) must not shift where the body is taken to start."""
    body = _GOOD.format(id="probe", lane="tasks") + (
        "\n---\n\nA later horizontal rule, then `unattended: true`, which agrees.\n"
    )
    assert card_schema.check(_board(tmp_path, "tasks", body)) == []


def test_a_card_outside_every_lane_is_caught(tmp_path):
    """Found by a real mishap on 2026-07-22: a card landed at `Board/` root and
    the gate said "All clear", because it enumerates lanes and a file in no lane
    is in no glob. A card with no lane has no state — invisible to the runner,
    absent from the digest, silently not-done."""
    root = _board(tmp_path, "tasks", _GOOD.format(id="probe", lane="tasks"))
    (root / "Board" / "stray.md").write_text(
        _GOOD.format(id="stray", lane="tasks"), encoding="utf-8"
    )
    assert "outside every lane" in _rules(card_schema.check(root))


def test_a_card_in_a_made_up_lane_is_caught(tmp_path):
    root = _board(tmp_path, "tasks", _GOOD.format(id="probe", lane="tasks"))
    bogus = root / "Board" / "archive"
    bogus.mkdir()
    (bogus / "stray.md").write_text(_GOOD.format(id="stray", lane="tasks"), encoding="utf-8")
    assert "which is not a lane" in _rules(card_schema.check(root))


def test_lane_readmes_are_not_mistaken_for_orphans(tmp_path):
    root = _board(tmp_path, "tasks", _GOOD.format(id="probe", lane="tasks"))
    (root / "Board" / "README.md").write_text("the board", encoding="utf-8")
    assert card_schema.check(root) == []


def test_ideas_is_not_enumerated_at_all(tmp_path):
    """03_board.md §1: `ideas/` is Karel's. The privacy boundary is structural —
    the gate never globs the folder, so it cannot read it by accident. A card
    there that would be invalid anywhere else must produce nothing."""
    root = _board(tmp_path, "ideas", "---\ntier: nonsense\n---\nrough czech note\n")
    assert card_schema.check(root) == []
    assert not [lane for _, lane in card_schema.cards(root) if lane == "ideas"]


# --- card_schema: `kind: chore` --------------------------------------------
#
# A chore is a one-prompter: the note said what to change and what the result should
# be, there is no fork, and the change has one obvious home. The only thing the
# schema relaxes for it is `## Approach`, and these tests pin *only* that — the
# tempting over-relaxation (a chore is small, so let it skip acceptance too) is what
# would make a chore card dispatchable with nothing to check it against.

_CHORE = """---
id: probe
title: "A chore"
state: tasks
kind: chore
tier: worker
worker: code-thread
recipe: none
unattended: true
verify: review
created: 2026-08-14
---

## Intent

Change the one obvious thing. Not the other things.

## Acceptance criteria

- it is done

## Open questions

None. Card is ready to execute.
"""


def test_a_chore_may_omit_approach_because_its_intent_is_its_approach(tmp_path):
    assert card_schema.check(_board(tmp_path, "tasks", _CHORE)) == []


def test_a_full_card_still_owes_approach(tmp_path):
    """The relaxation must be scoped to the kind, not leak into every card."""
    body = _CHORE.replace("kind: chore\n", "")
    assert "missing `## Approach`" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


@pytest.mark.parametrize("section", ["## Intent", "## Acceptance criteria",
                                    "## Open questions"])
def test_a_chore_still_owes_intent_acceptance_and_open_questions(tmp_path, section):
    """Small is not the same as unchecked: a chore with no acceptance would be
    dispatchable with nothing to fail against, which is the whole hazard."""
    cut = _CHORE.index(section)
    body = _CHORE[:cut] + _CHORE[cut:].replace(section, "## Removed", 1)
    assert card_schema.check(_board(tmp_path, "tasks", body)) != [], \
        f"a chore missing {section} should not pass"


def test_an_unknown_kind_is_rejected_rather_than_ignored(tmp_path):
    """An unrecognised kind would silently relax whichever checks it was guessed to."""
    body = _CHORE.replace("kind: chore", "kind: errand")
    assert "must be one of" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


def test_a_chore_at_lead_tier_is_a_contradiction(tmp_path):
    """The shape a misrouted note takes, and the cheapest place to catch it."""
    body = _CHORE.replace("tier: worker", "tier: lead")
    assert "nothing to decide" in _rules(card_schema.check(_board(tmp_path, "tasks", body)))


def test_absent_kind_means_a_full_card_so_no_existing_card_changes_meaning(tmp_path):
    root = _board(tmp_path, "tasks", _GOOD.format(id="probe", lane="tasks"))
    assert card_schema.check(root) == []
