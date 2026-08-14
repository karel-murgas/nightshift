"""`nightshift.chores` — one-prompters selected and reported as a batch.

What is worth pinning here is not the happy path but the places a batch could quietly
lie to the person verifying it:

**Nothing is silently left out.** A chore missing from a batch has to come with a
reason, because "absent" and "never written" look identical on a report otherwise.

**The checklist only holds what needs an eye.** A `review`-verified chore that landed
green was accepted by its gates; putting it on the list anyway is how a checklist grows
long enough to stop being read, which costs the batch its whole advantage.

**One attempt.** A chore that already burned its attempt must not silently re-enter a
batch, or eight chores become a night.

The execution half is not wired, so there is nothing here that dispatches — which is
also why `main` without `--plan` returns non-zero rather than doing nothing quietly.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from nightshift import board, chores


_CARD = """---
id: {id}
title: "{title}"
state: tasks
kind: {kind}
tier: {tier}
worker: code-thread
recipe: none
unattended: {unattended}
verify: {verify}
{extra}created: 2026-08-14
---

## Intent

Do the one obvious thing.

## Acceptance criteria

- done

## Open questions

none
"""


def _repo(tmp_path: Path, *cards: dict) -> Path:
    lane = tmp_path / "Board" / "tasks"
    lane.mkdir(parents=True)
    for spec in cards:
        spec = {"kind": "chore", "tier": "worker", "unattended": "true",
                "verify": "review", "extra": "", "title": "A chore", **spec}
        (lane / f"{spec['id']}.md").write_text(_CARD.format(**spec), encoding="utf-8")
    return tmp_path


def _now() -> dt.datetime:
    return dt.datetime(2026, 8, 14, 9, 30)


# ------------------------------------------------------------------------ selection

def test_only_chores_are_selected_and_other_work_is_not_reported_as_skipped(tmp_path):
    root = _repo(tmp_path, {"id": "a"}, {"id": "b", "kind": "chore"})
    (root / "Board" / "tasks" / "big.md").write_text(
        _CARD.format(id="big", title="Big", kind="chore", tier="worker",
                     unattended="true", verify="play", extra="").replace(
            "kind: chore\n", ""), encoding="utf-8")
    chosen, skipped = chores.select(root)
    assert {c.id for c in chosen} == {"a", "b"}
    # The non-chore is other work, not a skipped chore - it must not appear as noise.
    assert "big" not in {s.card_id for s in skipped}


def test_a_chore_that_burned_its_attempt_is_left_out_with_a_reason(tmp_path):
    root = _repo(tmp_path, {"id": "spent", "extra": "attempts: 1\n"})
    chosen, skipped = chores.select(root)
    assert chosen == []
    assert len(skipped) == 1 and "already attempted" in skipped[0].reason


def test_an_unattended_false_chore_is_left_out(tmp_path):
    root = _repo(tmp_path, {"id": "manual", "unattended": "false"})
    _, skipped = chores.select(root)
    assert "unattended: false" in skipped[0].reason


def test_a_workerless_chore_is_left_out(tmp_path):
    root = _repo(tmp_path, {"id": "orphan"})
    body = (root / "Board" / "tasks" / "orphan.md").read_text(encoding="utf-8")
    (root / "Board" / "tasks" / "orphan.md").write_text(
        body.replace("worker: code-thread", "worker: none"), encoding="utf-8")
    _, skipped = chores.select(root)
    assert "no worker" in skipped[0].reason


def test_a_lead_tier_chore_is_a_contradiction_and_left_out(tmp_path):
    root = _repo(tmp_path, {"id": "thinky", "tier": "lead"})
    _, skipped = chores.select(root)
    assert "nothing to decide" in skipped[0].reason


def test_the_batch_limit_holds_and_the_overflow_is_named(tmp_path):
    root = _repo(tmp_path, *[{"id": f"c{n}"} for n in range(5)])
    chosen, skipped = chores.select(root, limit=2)
    assert len(chosen) == 2
    assert len(skipped) == 3
    assert all("batch is full" in s.reason for s in skipped)


def test_selection_on_an_empty_board_is_empty_not_an_error(tmp_path):
    root = _repo(tmp_path)
    assert chores.select(root) == ([], [])


# --------------------------------------------------------------------- effort budget

def test_effort_is_measured_in_turns_and_time_not_diff_size():
    assert chores.effort_exceeded(5, 60.0) == ""
    assert "turns" in chores.effort_exceeded(chores.MAX_TURNS + 1, 60.0)
    assert "min" in chores.effort_exceeded(3, chores.MAX_WALL_S + 1)


def test_the_overrun_reason_carries_the_number_because_it_lands_on_the_card():
    reason = chores.effort_exceeded(999, 0.0)
    assert "999" in reason and str(chores.MAX_TURNS) in reason


def test_the_budget_is_tunable_and_actually_read():
    assert chores.effort_exceeded(10, 0.0, max_turns=5) != ""
    assert chores.effort_exceeded(10, 0.0, max_turns=50) == ""


# ------------------------------------------------------------------------- bisecting

def test_the_probe_is_the_midpoint_of_the_remaining_candidates():
    assert chores.next_probe(["a", "b", "c"]) == "b"
    assert chores.next_probe([]) is None
    assert chores.next_probe(["only"]) == "only"


# ---------------------------------------------------------------------- the report

def test_only_playable_chores_reach_the_checklist():
    batch = chores.Batch(outcomes=[
        chores.Outcome("seen", state="done", verify="play", title="Visible thing"),
        chores.Outcome("quiet", state="done", verify="review", title="Inner wiring"),
    ])
    text = chores.report(batch, _now())
    # Split on top-level headings only: `### <surface>` sub-headings live inside this
    # section, so splitting on "##" would cut the checklist off before its items.
    checklist = text.split("## Check these")[1].split("\n## ")[0]
    assert "Visible thing" in checklist
    assert "Inner wiring" not in checklist
    assert "Inner wiring" in text            # still reported, just not as a task


def test_a_batch_with_nothing_playable_says_so_rather_than_showing_an_empty_list():
    batch = chores.Batch(outcomes=[chores.Outcome("q", state="done", verify="review")])
    text = chores.report(batch, _now())
    assert "nothing needs an eye" in text


def test_the_checklist_groups_by_surface_so_one_run_covers_several():
    batch = chores.Batch(outcomes=[
        chores.Outcome("a", state="done", verify="play", surface="combat", title="A"),
        chores.Outcome("b", state="done", verify="play", surface="combat", title="B"),
        chores.Outcome("c", state="done", verify="play", surface="hub", title="C"),
    ])
    text = chores.report(batch, _now())
    assert "### combat" in text and "### hub" in text
    combat = text.split("### combat")[1].split("###")[0]
    assert "A" in combat and "B" in combat and "C" not in combat


def test_a_chore_without_a_surface_is_grouped_honestly_not_dropped():
    batch = chores.Batch(outcomes=[
        chores.Outcome("a", state="done", verify="play", title="Homeless")])
    text = chores.report(batch, _now())
    assert "### unsorted" in text and "Homeless" in text


def test_the_checklist_items_are_tickable():
    batch = chores.Batch(outcomes=[
        chores.Outcome("a", state="done", verify="play", title="A")])
    assert "- [ ] **A**" in chores.report(batch, _now())


def test_bounced_chores_are_framed_as_a_routing_signal_not_a_failure():
    batch = chores.Batch(outcomes=[
        chores.Outcome("b", state="bounced", detail="needs a decision")])
    text = chores.report(batch, _now())
    assert "not chores after all" in text and "needs a decision" in text
    assert "routing signal" in text


def test_parked_chores_say_they_get_one_attempt():
    batch = chores.Batch(outcomes=[chores.Outcome("p", state="parked", detail="red gate")])
    text = chores.report(batch, _now())
    assert "One attempt each" in text and "red gate" in text


def test_unreached_chores_are_reported_rather_than_vanishing():
    batch = chores.Batch(outcomes=[
        chores.Outcome("z", state="blocked", detail="usage window closed")])
    text = chores.report(batch, _now())
    assert "Not reached" in text and "usage window closed" in text


def test_left_out_chores_appear_with_their_reason():
    batch = chores.Batch(skipped=[chores.Skipped("old", "already attempted 1x")])
    text = chores.report(batch, _now())
    assert "Left out of this batch" in text and "already attempted" in text


def test_the_report_names_the_branch_and_suite_when_it_knows_them():
    text = chores.report(chores.Batch(), _now(), branch="ai/chores-2026-08-14",
                         suite="1937 passed")
    assert "ai/chores-2026-08-14" in text and "1937 passed" in text


# -------------------------------------------------------------------------- the CLI

def test_plan_writes_the_report_and_dispatches_nothing(tmp_path, capsys):
    root = _repo(tmp_path, {"id": "a"}, {"id": "b"})
    assert chores.main(["--root", str(root), "--plan"]) == 0
    text = (root / chores.OUT).read_text(encoding="utf-8")
    assert "Chore batch" in text
    assert b"\r\n" not in (root / chores.OUT).read_bytes()


def test_a_real_run_refuses_with_a_reason_rather_than_doing_nothing_quietly(tmp_path,
                                                                            capsys):
    """A batch that cannot run says which precondition stopped it. The silent
    no-op is the worst outcome available here, because it is indistinguishable
    from a batch that ran and found nothing to do."""
    root = _repo(tmp_path, {"id": "a"})           # not a git repo, no tier binding
    assert chores.main(["--root", str(root)]) == 1
    assert "refusing to run" in capsys.readouterr().out


def test_the_cli_reports_what_was_left_out(tmp_path, capsys):
    root = _repo(tmp_path, {"id": "spent", "extra": "attempts: 1\n"})
    chores.main(["--root", str(root), "--plan"])
    assert "spent" in capsys.readouterr().out


def test_outcome_states_are_disjoint_so_a_chore_cannot_be_counted_twice():
    batch = chores.Batch(outcomes=[
        chores.Outcome("a", state="done", verify="play"),
        chores.Outcome("b", state="bounced"),
        chores.Outcome("c", state="parked"),
        chores.Outcome("d", state="blocked"),
    ])
    counted = sum(len(batch.by_state(s))
                  for s in ("done", "bounced", "parked", "blocked"))
    assert counted == len(batch.outcomes)
    assert [o.card_id for o in batch.survivors] == ["a"]
