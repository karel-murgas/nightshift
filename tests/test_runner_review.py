"""After the worker stops: the review stage and its outcomes, streaming the
worker's output, the worktree fence, the JUnit verdict, the dedicated
integration checkout, rebase-then-merge, publishing, and the durable
staleness-sweep record.

One of three modules split out of `test_runner.py`, whose 315 tests on one
xdist worker were the suite's critical path. The fixtures, and why the split
exists, are in `_runner_helpers.py`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nightshift import board
from nightshift import limits
from nightshift import night
from nightshift import run_record
from nightshift import runner
from nightshift import stale_sweep
from nightshift.hooks import worktree_fence

import _runner_helpers
from _runner_helpers import (  # noqa: F401  (fixtures register by name)
    _RUNNER_SOURCE,
    _advance_on_remote,
    _art_card,
    _bare_origin,
    _branch_with_file,
    _card,
    _charter,
    _commit_on_base,
    _crashing_night,
    _fake_loop_walling,
    _fake_review_run,
    _fake_worker,
    _gates_pass_by_default,
    _home,
    _host_mode,
    _junit,
    _landed_wall,
    _last_commit_subject,
    _loaded_board,
    _night,
    _no_xdist_in_fixtures,
    _only_record,
    _remote_has,
    _remote_tip,
    _repo,
    _reviewed_branch,
    _roots,
    _split_repo,
    _stub_reviewer,
    _tier_binding,
    _worktree_repo,
    _write_manifest,
)


def test_an_ok_verdict_becomes_a_reviewed_outcome(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {"verdict": "ok", "notes": "clean"}, cost=0.2)

    result = runner.review_stage(root, card, runner.Dispatch("review", "2 commits", 0.5),
                                 "development_team", 0.0, 120)
    assert result.outcome == "reviewed"
    assert result.cost_usd == pytest.approx(0.7)   # dispatch 0.5 + reviewer 0.2, once


def test_a_needs_decision_verdict_carries_the_reviewers_question(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {"verdict": "needs_decision",
                                 "question": "worker chose HP; the card allows HP or heat"})

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.1),
                                 "development_team", 0.0, 120)
    assert result.outcome == "needs_decision"
    assert "HP or heat" in result.detail


def test_the_review_stage_skips_an_artefact_only_card(tmp_path, monkeypatch):
    """Art produces no commit on its branch, so there is no diff to review and
    nothing to merge. The stage leaves it exactly as before — a human eye at
    review/ — and never spawns the reviewer (this card does not change art)."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path, "icon", worker="art", commit=False)
    spawned = _stub_reviewer(monkeypatch, {"verdict": "ok"})

    result = runner.review_stage(root, card, runner.Dispatch("review", "art done", 0.3),
                                 "development_team", 0.0, 120)
    assert result.outcome == "review"             # unchanged
    assert result.cost_usd == pytest.approx(0.3)  # reviewer never ran, no added cost
    assert spawned == []


def test_the_review_stage_falls_back_to_review_on_a_wall(tmp_path, monkeypatch):
    """The plan running out mid-review is not the card's fault, and gates+tests
    already passed. It lands in review/ for a manual look rather than a guess."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {}, cost=0.3,
                   wall=limits.Wall(limits.SESSION, None, "usage limit reached"))

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.1),
                                 "development_team", 0.0, 120)
    assert result.outcome == "review"
    assert result.cost_usd == pytest.approx(0.4)   # cost still carried forward
    # …and the wall rides home even on the degrade path (wall-on-review-wrapup-
    # discards-a-verdict): the card's lane is decided on its merits, but the
    # night's window is shut and the run loop has to be told.
    assert result.wall is not None


def test_the_review_stage_falls_back_to_review_on_an_unusable_verdict(tmp_path, monkeypatch):
    """No verdict file, or a verdict the runner cannot read, degrades to the
    pre-existing behaviour — a human review at review/ — never to a guessed
    routing (§12)."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {}, cost=0.0)

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.0),
                                 "development_team", 0.0, 120)
    assert result.outcome == "review"


def test_a_checker_that_walls_after_writing_pass_has_its_verdict_honoured(
        tmp_path, monkeypatch):
    """The reported scenario, end to end. A complete `pass` written before the
    wall means the checking genuinely finished, so the card lands in review/ and
    the attempt is spent — it produced a reviewed deliverable. The wall is not
    thrown away with it: it rides home on `Dispatch.wall` so the night still
    knows its window is closed."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop_walling(monkeypatch, ["pass"])

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"          # NOT "limited"
    assert result.wall is not None and result.wall.scope == limits.SESSION

    runner.settle(root, "icon", result)
    card = board.find(root, "icon")
    assert card.lane == "review"
    assert card.attempts == 1                  # spent, not given back


def test_a_checker_that_walls_having_written_nothing_still_gives_the_attempt_back(
        tmp_path, monkeypatch):
    """The regression guard, first half. Honouring an artefact must not become
    honouring the absence of one — a checker that died before writing is exactly
    what the warm-resume path exists for, and it keeps it unchanged."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop_walling(monkeypatch, ["pass"], write_verdict=False)

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "limited"

    runner.settle(root, "icon", result)
    card = board.find(root, "icon")
    assert card.lane == "tasks"
    assert card.attempts == 0
    assert "## Error" not in card.text


def test_a_checker_that_walls_on_revise_with_rounds_left_is_still_limited(
        tmp_path, monkeypatch):
    """The regression guard, second half, and the reason the terminal test is not
    just "is the verdict complete?". `revise` at round 1 of 3 is a *resumable*
    interruption: the loop cannot spend rounds 2-3 in a closed window, and the
    path below it would file the card as "did not pass this in 1 round(s)" —
    parking a card that had two rounds it never received."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop_walling(monkeypatch, ["revise"])
    assert runner.MAX_ROUNDS > 1, "this test is only meaningful with rounds to spare"

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "limited"          # never "parked"

    runner.settle(root, "icon", result)
    card = board.find(root, "icon")
    assert card.lane == "tasks" and card.attempts == 0


def test_a_checker_that_walls_on_revise_in_the_last_round_is_honoured_as_a_park(
        tmp_path, monkeypatch):
    """The other side of `rounds_left`. Once the rounds are spent there is no
    resumable work left, so a complete `revise` on the final round is terminal
    and the card reaches Karel with the critique — which is the outcome it would
    have had if the wrap-up call had returned cleanly."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop_walling(monkeypatch, ["revise"] * runner.MAX_ROUNDS,
                       wall_at=runner.MAX_ROUNDS)

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    assert result.wall is not None
    assert f"did not pass this in {runner.MAX_ROUNDS} round(s)" in result.detail


def test_a_producer_that_walls_after_parking_keeps_its_question(tmp_path, monkeypatch):
    """Answer B. A park is terminal by construction — the round loop already
    `break`s on it and no re-roll resolves an ambiguity — and discarding one costs
    Karel a whole night's question. It reaches needs-decision/ carrying it."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached",
                 verdict={"outcome": "parked",
                          "summary": "HP or heat? the card allows either"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    assert result.wall is not None
    assert "HP or heat" in result.detail

    runner.settle(root, "probe", result)
    card = board.find(root, "probe")
    assert card.lane == "needs-decision"
    assert "HP or heat" in card.text


def test_a_producer_that_walls_on_any_other_verdict_is_unchanged(tmp_path, monkeypatch):
    """B and not C. A verdict attests the *worker's* account, not that the tree
    is finished, so anything but a park keeps today's `_limit_reached` behaviour —
    otherwise a half-written tree goes red on the gates and converts a free
    give-back into a spent attempt, which is the harm the wall path prevents."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached",
                 verdict={"outcome": "done", "summary": "implemented"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "limited"

    runner.settle(root, "probe", result)
    assert board.find(root, "probe").attempts == 0


def test_a_reviewer_that_walls_after_writing_ok_still_routes_on_it(tmp_path, monkeypatch):
    """The diff reviewer's entire output *is* its verdict file, so a complete one
    means the review finished and the wall landed on the wrap-up after it. It
    routes, instead of degrading to review/ for a human who has nothing to add."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {"verdict": "ok", "notes": "clean"}, cost=0.2,
                   wall=limits.Wall(limits.SESSION, None, "usage limit reached"))

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.5),
                                 "development_team", 0.0, 120)
    assert result.outcome == "reviewed"
    assert result.wall is not None
    assert result.cost_usd == pytest.approx(0.7)


def test_a_reviewer_that_walls_after_needs_decision_still_carries_the_question(
        tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {"verdict": "needs_decision", "question": "which cap?"},
                   wall=limits.Wall(limits.SESSION, None, "usage limit reached"))

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.1),
                                 "development_team", 0.0, 120)
    assert result.outcome == "needs_decision"
    assert "which cap?" in result.detail
    assert result.wall is not None


def test_the_review_stage_does_not_spawn_the_reviewer_into_a_closed_window(
        tmp_path, monkeypatch):
    """An earlier stage already walled and had its verdict honoured, so the
    incoming `Dispatch` carries the wall. Spawning the reviewer now would call
    into a window already proven shut, burn a subprocess and degrade anyway."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    spawned = _stub_reviewer(monkeypatch, {"verdict": "ok"})

    wall = limits.Wall(limits.SESSION, None, "usage limit reached")
    result = runner.review_stage(
        root, card, runner.Dispatch("review", "x", 0.4, 1, wall),
        "development_team", 0.0, 120)
    assert spawned == []                          # never called
    assert result.outcome == "review"
    assert result.wall is wall                    # still on its way to the run loop
    assert result.cost_usd == pytest.approx(0.4)  # nothing spent


def test_a_landed_card_carrying_a_wall_still_stops_the_night(tmp_path, monkeypatch):
    """The wall's other half is not thrown away, only moved. The card settled, so
    it is not retried — but the window is shut, and `--sessions 1` means "work
    until the session limit is reached" whichever way the card went."""
    root = _loaded_board(tmp_path, "a", "b", "c")

    calls = _night(monkeypatch, root, [_landed_wall()])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a"]        # not ["a", "b", …] — the window is not open


def test_a_landed_card_carrying_a_wall_sleeps_and_goes_on_rather_than_retrying(
        tmp_path, monkeypatch):
    """The difference from the `limited` branch, and the whole point of moving
    the wall rather than dropping it: `a` is finished and must NOT take the
    `continue  # same card, new window` retry path. The night sleeps out the
    window and resumes at the *next* card."""
    root = _loaded_board(tmp_path, "a", "b")
    slept: list = []

    calls = _night(monkeypatch, root, [_landed_wall()])
    monkeypatch.setattr(runner, "_sleep_until", lambda when: slept.append(when) or True)
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))

    assert calls == ["a", "b"]   # `a` landed once; `b` follows it, `a` is not re-run
    assert len(slept) == 1, "the night must wait out the window, not walk straight on"


def test_a_landed_wall_that_does_not_reopen_stops_the_night(tmp_path, monkeypatch):
    """A weekly limit does not reopen inside a night, so there is nothing to sleep
    for — the same reasoning the `limited` branch applies, reached from a card
    that landed."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [_landed_wall(limits.WEEKLY)])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))
    assert calls == ["a"]


def test_the_run_log_tells_an_honoured_wall_apart_from_an_empty_one(tmp_path, monkeypatch):
    """Karel's morning has to distinguish three things at a glance: "walled with
    nothing — attempt given back", "walled after a complete verdict — honoured,
    card landed", and "the gate harness crashed". The first and third already name
    themselves; being unable to name the second is the whole complaint on this
    card."""
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [_landed_wall()])
    monkeypatch.setattr(runner, "_sleep_until", lambda when: True)
    lines: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda msg: lines.append(str(msg)))

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    landed = [l for l in lines if "verdict was honoured" in l]
    assert landed, f"no line named the honoured-verdict case; got {lines}"
    assert "the night's window is still closed" in landed[0]


def test_a_give_back_outcome_carrying_a_wall_is_never_called_a_landing(
        tmp_path, monkeypatch):
    """The compound case: a checker is honoured, and *then* the gate harness
    crashes. `settle` files that as `blocked` — "not attempted, attempt given
    back" — and appending "the card landed" to it would contradict itself in the
    one line a 6 AM reader trusts. The honoured-verdict note is for landings
    only, so it names every give-back outcome as excluded rather than `limited`
    alone."""
    root = _loaded_board(tmp_path, "a")
    blocked = runner.Dispatch("blocked", "the gate harness crashed", 0.0, 1,
                              limits.Wall(limits.SESSION, None, "usage limit reached"))
    _night(monkeypatch, root, [blocked])
    monkeypatch.setattr(runner, "settle",
                        lambda r, cid, res: f"{cid}: not attempted, attempt given back")
    lines: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda msg: lines.append(str(msg)))

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert not [l for l in lines if "the card landed" in l], (
        "a blocked/limited/interrupted dispatch gives the attempt back — it never landed"
    )


def test_the_reviewer_is_never_shown_the_workers_prompt(tmp_path, monkeypatch):
    """§16: an agent that knows what was intended sees what was intended. The
    runner builds the reviewer's context — the diff, the criteria, the repo — and
    there is no path for the worker's prompt or transcript to reach it, because
    the runner never puts them there."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        seen["argv"] = argv
        seen["prompt"] = prompt
        target = Path(next(l.strip() for l in prompt.splitlines()
                           if l.strip().endswith(".json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"verdict": "ok", "notes": ""}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.review_stage(root, card, runner.Dispatch("review", "x", 0.0),
                        "development_team", 0.0, 120)

    argv = seen["argv"]
    prompt = seen["prompt"]
    assert argv[argv.index("--agent") + 1] == runner.REVIEWER_AGENT
    # a line unique to the worker's dispatch prompt, never present in the review one
    assert "Commit your work on this branch" not in prompt
    assert "Do not move the card" not in prompt
    # …but it DOES get the acceptance criteria verbatim off the card
    assert "the gates are green" in prompt
    # Structural blindness, not a charter promise: the only directory handed to
    # the reviewer is a throwaway checkout, never the main repo (whose gitignored
    # `.ai/runs/` physically holds the worker's prompt and transcript).
    add_dirs = [argv[i + 1].replace("\\", "/") for i, a in enumerate(argv) if a == "--add-dir"]
    assert add_dirs and all("_review-probe" in d for d in add_dirs)
    assert all(str(root.resolve()).replace("\\", "/") != d for d in add_dirs)
    assert all(".ai/runs" not in d for d in add_dirs)


# --- settling the review stage's outcomes -----------------------------------

def test_settle_reviewed_merges_and_lands_in_testing(tmp_path, monkeypatch):
    """An `ok` verdict: the runner merges `ai/<id>` into the integration branch
    and the card lands in testing/, awaiting Karel — the whole point of the card."""
    root = _worktree_repo(tmp_path)
    card = _reviewed_branch(root, tmp_path)
    card.write({"started": "2026-07-24T03:00:00"})
    calls: list = []
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (calls.append((branch, base, remote)) or (True, "merged")))

    note = runner.settle(root, "probe", runner.Dispatch("reviewed", "clean"))
    settled = board.find(root, "probe")
    assert settled.lane == "testing"
    assert settled.fields["state"] == "testing"
    assert not settled.fields.get("started")
    # Rebased onto + merged into the integration branch, and told which remote to
    # delete the branch on — `""` here, since this fixture's host declares no
    # `publish_remote` (see `test_settle_threads_the_publish_remote_to_the_merge`).
    assert calls == [("ai/probe", runner.default_base(root), "")]
    assert "testing/" in note


def test_settle_reaps_rescue_branches_the_moment_a_card_succeeds_into_testing(
        tmp_path, monkeypatch):
    """rescue-branches-only-swept-on-failure (2026-08-13): the far more common
    outcome — reviewed ok, merged, lands in testing/ — never called
    `prune_rescue_branches` at all; a card's `@failed-N` refs from earlier
    attempts survived until (if ever) a later trip through done/failed's
    startup sweep. `settle`'s wrapper must reap them immediately, the same
    dispatch that lands the card, not on some later sweep."""
    root = _worktree_repo(tmp_path)
    card = _reviewed_branch(root, tmp_path)
    card.write({"started": "2026-07-24T03:00:00"})
    subprocess.run(["git", "branch", "ai/probe@failed-1", "development_team"],
                   cwd=root, check=True)
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (True, "merged"))

    runner.settle(root, "probe", runner.Dispatch("reviewed", "clean"))

    assert board.find(root, "probe").lane == "testing"
    listed = subprocess.run(["git", "branch", "--list", "ai/probe@failed-*"], cwd=root,
                            capture_output=True, text=True).stdout
    assert listed.strip() == ""


def test_settle_threads_the_publish_remote_to_the_merge(tmp_path, monkeypatch):
    """The merge step deletes the card's branch on the remote as well as locally,
    and it learns which remote the same way it learns the integration branch:
    from the caller. `settle` resolves the host's `publish_remote` and passes it
    down — so a host that opted into publishing also opts into the cleanup, and
    one that did not is handed `""` and touches no remote."""
    root = _worktree_repo(tmp_path)
    _reviewed_branch(root, tmp_path)
    (root / ".ai").mkdir(exist_ok=True)
    (root / runner.HOST_FILE).write_bytes(
        json.dumps({"publish_remote": "origin"}).encode("utf-8"))
    seen: list = []
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (seen.append(remote) or (True, "merged")))

    runner.settle(root, "probe", runner.Dispatch("reviewed", "clean"))
    assert seen == ["origin"]


def test_settle_reviewed_but_unmergeable_goes_to_review_not_testing(tmp_path, monkeypatch):
    """Reviewed ok, but the branch will not merge — blocked on a sibling that
    landed first (03_board.md §1: that is exactly what review/ means). It must go
    to review/ for a human to resolve the merge, never silently to testing/ as if
    it had merged, and never to done/."""
    root = _worktree_repo(tmp_path)
    card = _reviewed_branch(root, tmp_path)
    card.write({"started": "2026-07-24T03:00:00"})
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (False, "conflict in board.py"))

    note = runner.settle(root, "probe", runner.Dispatch("reviewed", "clean"))
    settled = board.find(root, "probe")
    assert settled.lane == "review"
    assert "could not be rebased" in settled.text
    assert "conflict in board.py" in note


def test_settle_needs_decision_files_a_schema_valid_question(tmp_path):
    """The reviewer's ambiguity reaches Karel: the card lands in needs-decision/
    with a `## Question`, which `card_schema` requires there and would reject
    without."""
    from nightshift.gates import card_schema  # moved (07_portability.md §8 step 3)

    root = _worktree_repo(tmp_path)
    card = _reviewed_branch(root, tmp_path)
    card.write({"started": "2026-07-24T03:00:00"})

    runner.settle(root, "probe", runner.Dispatch(
        "needs_decision", "worker chose HP; the card allows HP or heat — "
        "HP means A, heat means B. Which did you intend?"))
    settled = board.find(root, "probe")
    assert settled.lane == "needs-decision"
    assert "## Question" in settled.text
    assert "HP or heat" in settled.text
    probe_violations = [str(v) for v in card_schema.check(root) if "probe" in str(v)]
    assert probe_violations == []


def test_only_a_verify_review_card_reaches_done_from_this_stage(tmp_path, monkeypatch):
    """`done/` used to be unreachable from this stage — testing/ was the only exit,
    which is what `unplayable-cards-still-land-in-testing` was about: eleven of the
    sixteen cards waiting there had no surface Karel could exercise. It is reachable
    now, but only for a card that *declares* `verify: review`, and only on a clean
    merge. Drive every routing the stage can produce and pin where each one lands."""
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (True, "merged"))
    cases = {
        "needs_decision": ("needs-decision", "play", runner.Dispatch("needs_decision", "q?")),
        "reviewed-play": ("testing", "play", runner.Dispatch("reviewed", "ok")),
        "reviewed-review": ("done", "review", runner.Dispatch("reviewed", "ok")),
    }
    for i, (label, (expected_lane, verify, result)) in enumerate(cases.items()):
        sub = tmp_path / f"case{i}"
        sub.mkdir()
        root = _worktree_repo(sub)
        _reviewed_branch(root, sub, verify=verify)
        runner.settle(root, "probe", result)
        assert board.find(root, "probe").lane == expected_lane, label

    # A card that cannot merge goes to review/ from either value: "a human must
    # resolve a merge" is true whatever the card declares about verification.
    for i, verify in enumerate(("play", "review")):
        sub = tmp_path / f"unmergeable{i}"
        sub.mkdir()
        root = _worktree_repo(sub)
        _reviewed_branch(root, sub, verify=verify)
        monkeypatch.setattr(runner, "rebase_and_merge",
                            lambda r, card, branch, base, test_timeout=600, remote="":
                            (False, "conflict"))
        runner.settle(root, "probe", runner.Dispatch("reviewed", "ok"))
        assert board.find(root, "probe").lane == "review", verify


def test_a_play_card_lands_carrying_the_workers_scenario(tmp_path, monkeypatch):
    """The other half of the split: a card that does reach Karel's desk arrives
    with a `## How to test` written by the worker that built it, not bare."""
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (True, "merged"))
    root = _worktree_repo(tmp_path)
    _reviewed_branch(root, tmp_path, verify="play")
    runner.settle(root, "probe", runner.Dispatch(
        "reviewed", "ok", how_to_test="Start a run, open the hotbar, expect two slots."))
    landed = board.find(root, "probe")
    assert "## How to test" in landed.text
    assert "expect two slots" in landed.text


def test_a_night_lands_a_reviewed_ok_card_in_testing(tmp_path, monkeypatch):
    """End to end through `run()`: a code card passes gates+tests, the reviewer
    says ok, and the runner merges and lands it in testing/ — the night's happy
    path this card exists to automate. The producer runs for real through the
    faked CLI; the reviewer and the merge are stubbed (no live dispatch, no live
    merge)."""
    root = _loaded_board(tmp_path, "feat")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "done"})
    monkeypatch.setattr(runner, "review_branch",
                        lambda *a, **k: ({"verdict": "ok", "notes": "clean"}, 0.1, None))
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (True, "merged"))

    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))
    assert board.find(root, "feat").lane == "testing"


def test_merge_branch_refuses_when_the_checkout_is_on_the_wrong_branch(tmp_path):
    """`merge_branch` never merges into whatever branch happens to be checked out.
    The main checkout must be on the integration branch — the runner is started
    there — and a mismatch is refused with a reason rather than merging blindly.
    (No live merge: the guard returns before any git merge is attempted.)"""
    root = _worktree_repo(tmp_path)          # HEAD is on development_team
    ok, why = runner.merge_branch(root, "ai/probe", "some-other-branch")
    assert not ok
    assert "not the integration branch" in why


# --- streaming the worker's output (runner-stream-worker-output) -----------
#
# `_run_worker` used to be one `subprocess.run(..., capture_output=True)` call,
# so nothing about a running worker existed on disk until it exited. These
# drive the real `Popen`-based rewrite against tiny stub subprocesses — not
# mocks — because the whole point is behaviour a mock would agree with either
# way: bytes landing on disk *while* the process is still alive, a timeout that
# actually kills the child, and a join that does not hang when a grandchild the
# child spawned is still holding the pipe open.

class TestTerminalResult:
    """`_terminal_result` is what replaced `json.loads(proc.stdout)` once stdout
    could be a JSONL stream instead of one blob: the last line on the stream
    that parses as a JSON object, scanning from the end so a stream cut short
    mid-line does not stop the true last complete line from being found."""

    def test_a_single_object_is_read_exactly_like_the_old_output_format_json(self):
        """The old shape — every existing test double still hands this back —
        is one line, so the last line is the only line and this must agree
        with a plain `json.loads` exactly."""
        stdout = json.dumps({"total_cost_usd": 0.5, "session_id": "s1"})
        assert runner._terminal_result(stdout) == {"total_cost_usd": 0.5, "session_id": "s1"}

    def test_a_multi_event_stream_reads_the_last_line_not_the_first(self):
        stdout = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
            json.dumps({"type": "result", "total_cost_usd": 1.23,
                       "terminal_reason": "completed"}),
        ])
        assert runner._terminal_result(stdout) == {
            "type": "result", "total_cost_usd": 1.23, "terminal_reason": "completed"}

    def test_trailing_blank_lines_do_not_hide_the_result(self):
        stdout = json.dumps({"type": "result", "total_cost_usd": 2.0}) + "\n\n\n"
        assert runner._terminal_result(stdout)["total_cost_usd"] == 2.0

    def test_unparseable_stdout_is_an_empty_dict_not_an_exception(self):
        assert runner._terminal_result("not json at all\nstill not json") == {}

    def test_empty_stdout_is_an_empty_dict(self):
        assert runner._terminal_result("") == {}


def test_producer_argv_asks_for_stream_json_and_verbose(tmp_path, monkeypatch):
    """`--verbose` is required alongside `-p --output-format stream-json` on
    the installed CLI — confirmed directly against it: a bare invocation with
    no `--verbose` refuses with "Error: When using --print,
    --output-format=stream-json requires --verbose". The old single-blob
    `--output-format json` must be gone, not merely joined by the new flags."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "ok"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)

    argv = seen["argv"]
    assert argv.count("--output-format") == 1
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"
    assert "--verbose" in argv


def test_the_producer_and_checker_argvs_both_use_stream_json_and_verbose(tmp_path, monkeypatch):
    """The checker is the second spawn site (`run_checker`), built by the
    runner rather than nested inside the producer (§16) — it must carry the
    same two flags independently, not inherit them from the producer's call."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    argvs: list = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        argvs.append(argv)
        agent = argv[argv.index("--agent") + 1]
        if agent == "art":
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "cand.png").write_bytes(b"\x89PNG")
        else:
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"verdict": "pass", "best": "cand.png"}),
                              encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert len(argvs) == 2   # producer, then checker
    for one in argvs:
        assert one.count("--output-format") == 1
        idx = one.index("--output-format")
        assert one[idx + 1] == "stream-json"
        assert "--verbose" in one


def test_stale_check_argv_uses_stream_json_and_verbose(tmp_path, monkeypatch):
    """The third spawn site, `run_stale_check` — the one not reachable through
    `dispatch` at all, so it needs its own direct call."""
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        seen["argv"] = argv
        seen["stream_path"] = stream_path
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.02}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    runner.run_stale_check(tmp_path, "docs/x.md", out_dir, "sonnet", 0.0, 60)

    argv = seen["argv"]
    assert argv.count("--output-format") == 1
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"
    assert "--verbose" in argv
    assert seen["stream_path"] == out_dir / "stream.jsonl"


def test_read_telemetry_survives_a_stream_json_worker(tmp_path, monkeypatch):
    """End to end: a producer whose stdout is a realistic 3-line stream-json
    stream (not the old single-blob shape) still leaves `worker-1.json` as one
    JSON object — the terminal `result` event, not the whole JSONL blob — so
    `read_telemetry` (which globs `worker-*.json` and `json.loads`s each one)
    and the card's `## Telemetry` block both come out exactly as they did
    under `--output-format json`."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")

    jsonl_stdout = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({
            "type": "result", "duration_ms": 12345, "duration_api_ms": 6000,
            "num_turns": 4, "total_cost_usd": 0.42,
            "usage": {"output_tokens": 100, "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 5, "input_tokens": 20},
            "modelUsage": {"claude-sonnet-5": {}}, "permission_denials": [],
            "terminal_reason": "completed", "session_id": "s1",
        }),
    ])

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        (Path(cwd) / "worked.txt").write_text("done\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "worker: did the thing"], cwd=cwd, check=True)
        verdict_line = next(l.strip() for l in prompt.splitlines()
                            if l.strip().endswith(".json"))
        Path(verdict_line).parent.mkdir(parents=True, exist_ok=True)
        Path(verdict_line).write_text(
            json.dumps({"outcome": "done", "summary": "implemented"}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, jsonl_stdout, "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.cost_usd == pytest.approx(0.42)

    out_dir = runner.run_dir(root, board.find(root, "probe"), 1)
    worker_json = json.loads((out_dir / "worker-1.json").read_text(encoding="utf-8"))
    assert worker_json["type"] == "result"
    assert worker_json["total_cost_usd"] == 0.42     # the result event, not the assistant line

    tel = runner.read_telemetry(out_dir)
    assert tel["cost_usd"] == pytest.approx(0.42)
    assert tel["turns"] == 4
    assert tel["ended"] == "completed"
    assert "claude-sonnet-5" in tel["models"]

    runner.settle(root, "probe", result)
    card_text = (root / "Board" / "review" / "probe.md").read_text(encoding="utf-8")
    assert "## Telemetry" in card_text and "0.42" in card_text


def test_stream_jsonl_grows_while_the_worker_is_still_running(tmp_path):
    """The acceptance criterion, verbatim: drive `_run_worker` against a stub
    process that emits one event, pauses, then emits its terminal result — and
    read `stream.jsonl` from this thread mid-pause to confirm only the first
    event has landed while the process is still alive."""
    stub = (
        "import json, time\n"
        "print(json.dumps({'type': 'event', 'n': 1}), flush=True)\n"
        "time.sleep(1.2)\n"
        "print(json.dumps({'type': 'result', 'total_cost_usd': 0.01}), flush=True)\n"
    )
    stream_path = tmp_path / "stream.jsonl"
    outcome: dict = {}

    def _run():
        outcome["proc"] = runner._run_worker(
            [sys.executable, "-c", stub], tmp_path, 10, stream_path)

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        deadline = time.time() + 5
        seen_first = False
        while time.time() < deadline:
            if stream_path.is_file() and '"n": 1' in stream_path.read_text(encoding="utf-8"):
                seen_first = True
                break
            time.sleep(0.05)
        assert seen_first, "the first event never landed on disk while the worker ran"
        # Still mid-sleep: the terminal event must not have landed yet.
        assert "total_cost_usd" not in stream_path.read_text(encoding="utf-8")
    finally:
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert "total_cost_usd" in stream_path.read_text(encoding="utf-8")
    assert outcome["proc"].returncode == 0


def test_a_worker_that_exceeds_its_timeout_is_still_killed(tmp_path):
    """The timeout path must survive the `Popen` rewrite: `subprocess.run
    (timeout=)` killed the child for free; a manual `Popen.wait(timeout=)` does
    not, so `_run_worker` has to kill it by hand before re-raising."""
    pidfile = tmp_path / "pid.txt"
    stub = (
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        runner._run_worker([sys.executable, "-c", stub, str(pidfile)], tmp_path, 2)

    deadline = time.time() + 5
    pid = None
    while time.time() < deadline:
        if pidfile.is_file() and pidfile.read_text(encoding="utf-8").strip():
            pid = int(pidfile.read_text(encoding="utf-8").strip())
            break
        time.sleep(0.05)
    assert pid is not None, "the stub never reported its own pid"
    assert not runner._pid_alive(pid), "the timed-out worker was not actually killed"


def test_run_worker_closes_the_stream_handle_when_popen_itself_fails(tmp_path):
    """A `Popen` failure (the binary vanishing between preflight and dispatch,
    say) must not leak the tee's file handle. Windows makes this observable
    directly: an open handle blocks deletion, so if the guard did not close
    it, this unlink would raise `PermissionError`."""
    stream_path = tmp_path / "stream.jsonl"
    missing = tmp_path / "does-not-exist-binary"
    with pytest.raises(OSError):
        runner._run_worker([str(missing)], tmp_path, 5, stream_path)
    stream_path.unlink()


def test_run_worker_returns_promptly_when_a_grandchild_holds_stdout_open(tmp_path):
    """The gap the 2026-07-24 review found: `t_out.join()`/`t_err.join()` on
    the success path used to be unbounded, unlike the timeout path's
    `join(timeout=5)`. A child that exits promptly while a grandchild it spawned
    (a Bash tool call, an MCP server) still holds the pipe's write end open
    leaves `readline()` never seeing EOF — the drain thread never finishes, and
    an unbounded `join()` would then block forever with no timeout backstop at
    all. This stub reproduces exactly that: it prints one event, hands a
    duplicate of its own stdout handle to a grandchild that sleeps for 30s, and
    exits immediately without waiting for it — `_run_worker` must still return
    in a bounded time, not after 30s and not never."""
    stub = (
        "import json, os, subprocess, sys\n"
        "print(json.dumps({'type': 'event', 'n': 1}), flush=True)\n"
        "fd = os.dup(1)\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=fd)\n"
        "os.close(fd)\n"
    )
    start = time.time()
    proc = runner._run_worker([sys.executable, "-c", stub], tmp_path, 20)
    elapsed = time.time() - start
    assert elapsed < 15, f"_run_worker took {elapsed:.1f}s — the drain-thread join is not bounded"
    assert proc.returncode == 0


def test_ensure_trusted_writes_the_flag_for_an_untrusted_workspace(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    key = str(tmp_path.resolve()).replace("\\", "/")
    config.write_text(json.dumps({"projects": {key: {"hasTrustDialogAccepted": False}}}),
                      encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["projects"][key]["hasTrustDialogAccepted"] is True


def test_ensure_trusted_creates_the_project_entry_when_absent(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    config.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    key = str(tmp_path.resolve()).replace("\\", "/")
    assert data["projects"][key]["hasTrustDialogAccepted"] is True


def test_ensure_trusted_matches_an_existing_key_case_insensitively(tmp_path, monkeypatch):
    # Windows stores the drive letter either case (both `c:/` and `C:/` were live
    # in Karel's real config); a naive add would leave the wrong-cased entry still
    # untrusted and create a trusted duplicate that Claude never reads.
    config = _home(monkeypatch, tmp_path)
    weird = str(tmp_path.resolve()).replace("\\", "/").swapcase()
    config.write_text(json.dumps({"projects": {weird: {"hasTrustDialogAccepted": False}}}),
                      encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["projects"][weird]["hasTrustDialogAccepted"] is True
    assert len(data["projects"]) == 1, "created a duplicate instead of updating in place"


def test_ensure_trusted_is_a_noop_when_already_trusted(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    key = str(tmp_path.resolve()).replace("\\", "/")
    config.write_text(json.dumps({"projects": {key: {"hasTrustDialogAccepted": True}}}),
                      encoding="utf-8")
    before = config.stat().st_mtime_ns
    runner.ensure_workspace_trusted(tmp_path)
    assert config.stat().st_mtime_ns == before, "rewrote the file with nothing to change"


def test_ensure_trusted_swallows_a_missing_config(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)  # no .claude.json written
    runner.ensure_workspace_trusted(tmp_path)  # must not raise


def test_ensure_trusted_swallows_unreadable_json(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    config.write_text("{ not json", encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)  # must not raise
    assert config.read_text(encoding="utf-8") == "{ not json", "clobbered a file it could not parse"


def test_fence_is_off_when_no_roots_are_declared(tmp_path):
    """Karel's own sessions never set the env var, so the fence must be a pure
    no-op then — a guard that fired on every interactive Write would be torn out
    within a day."""
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "x")}}
    assert worktree_fence.evaluate(payload, []) is None


def test_fence_denies_a_write_outside_the_worktree(tmp_path):
    wt = tmp_path / "wt"
    outside = tmp_path / "canonical" / "combat" / "action.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(outside)}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is not None


def test_fence_allows_a_write_inside_the_worktree(tmp_path):
    wt = tmp_path / "wt"
    inside = wt / "dungeoneer" / "combat" / "action.py"
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(inside)}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is None


def test_fence_allows_writes_to_any_declared_root(tmp_path):
    """The verdict JSON lands in the run dir (outside the worktree) and the card's
    Thread lives under Board/ — both are declared roots, both must pass."""
    wt, run_dir, board_dir = tmp_path / "wt", tmp_path / "runs", tmp_path / "Board"
    roots = _roots(wt, run_dir, board_dir)
    for target in (run_dir / "verdict-1.json", board_dir / "tasks" / "c.md"):
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
        assert worktree_fence.evaluate(payload, roots) is None, target


def test_fence_denies_a_bash_cd_to_an_absolute_path_outside(tmp_path):
    """The exact failure: `cd <canonical checkout> && git commit`."""
    wt = tmp_path / "wt"
    canonical = tmp_path / "canonical"
    cmd = f"cd {canonical.as_posix()} && git commit -am x"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is not None


def test_fence_allows_a_relative_cd_which_stays_in_the_worktree(tmp_path):
    """A relative `cd` resolves under the launch cwd (the worktree), so it is not
    the escape and must not be blocked — else the worker cannot `cd dungeoneer`."""
    wt = tmp_path / "wt"
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "cd dungeoneer && python -m pytest -q"}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is None


def test_fence_denies_git_dash_C_pointing_outside(tmp_path):
    wt = tmp_path / "wt"
    canonical = tmp_path / "canonical"
    cmd = f"git -C {canonical.as_posix()} commit -am x"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is not None


def test_worker_env_declares_exactly_the_three_writable_roots(tmp_path):
    """The env the runner hands a worker must allow its worktree, its run dir and
    Board/, and nothing else — those are the only places a worker legitimately
    writes."""
    _write_manifest(tmp_path)
    tree, out_dir = tmp_path / "wt", tmp_path / "runs" / "c" / "attempt-1"
    env = runner._worker_env(tmp_path, tree, out_dir)
    declared = env[runner.fence_env(tmp_path)].split(os.pathsep)
    expected = {str(p.resolve()) for p in (tree, out_dir, tmp_path / "Board")}
    assert set(declared) == expected
    assert "PATH" in env or "Path" in env, "must inherit the parent environment, not replace it"


def test_a_project_declaring_no_fence_env_leaves_the_fence_off(tmp_path):
    """`[worker].fence_env` is optional, and its absence must be a plain
    inherited environment rather than a `KeyError` or a variable named `""`.
    The fence then never arms, which is the safe direction the hook documents."""
    assert runner.fence_env(tmp_path) == ""
    env = runner._worker_env(tmp_path, tmp_path / "wt", tmp_path / "out")
    assert "" not in env
    assert "PATH" in env or "Path" in env


def test_backstop_resets_a_stray_commit_off_the_integration_branch(tmp_path):
    """A worker commit that lands on `base` from the wrong checkout is undone; the
    branch returns to the tip the runner left, no matter the card's fate."""
    root = _repo(tmp_path)
    base = runner.current_branch(root)
    tip = runner._git(root, "rev-parse", base).stdout.strip()

    # Simulate the defect: a commit lands on `base` in the main checkout.
    (root / "stray.py").write_text("print('wrong checkout')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "stray: committed to base by mistake"],
                   cwd=root, check=True)
    assert runner._git(root, "rev-parse", base).stdout.strip() != tip

    assert runner.assert_integration_unmoved(root, base, tip) is True
    assert runner._git(root, "rev-parse", base).stdout.strip() == tip
    assert not (root / "stray.py").exists(), "the stray commit's file must be gone too"


def test_backstop_is_a_no_op_when_the_branch_did_not_move(tmp_path):
    root = _repo(tmp_path)
    base = runner.current_branch(root)
    tip = runner._git(root, "rev-parse", base).stdout.strip()
    assert runner.assert_integration_unmoved(root, base, tip) is False
    assert runner._git(root, "rev-parse", base).stdout.strip() == tip


def test_check_junit_passes_an_all_green_report(tmp_path):
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=42))
    assert ok and why == ""


def test_check_junit_fails_on_a_failure(tmp_path):
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=42, failures=1))
    assert not ok and "1 failure" in why


def test_check_junit_fails_on_an_error(tmp_path):
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=42, errors=2))
    assert not ok and "2 error" in why


def test_check_junit_treats_zero_collected_as_a_failure(tmp_path):
    """The silent-green guard: a selection that matched nothing is not a pass."""
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=0))
    assert not ok and "0 tests" in why


def test_check_junit_fails_when_no_report_was_written(tmp_path):
    ok, why = runner._check_junit(tmp_path / "absent.xml")
    assert not ok and "no JUnit report" in why


def test_check_junit_fails_on_an_unreadable_report(tmp_path):
    bad = tmp_path / "j.xml"
    bad.write_text("<testsuites>not closed", encoding="utf-8")
    ok, why = runner._check_junit(bad)
    assert not ok and "unreadable" in why


def test_run_tests_passes_the_parallel_and_junit_flags(tmp_path, monkeypatch):
    """Production must parallelise by file and report to JUnit. Restores the real
    flags (the autouse fixture blanks them for speed) and captures the argv."""
    monkeypatch.setattr(runner, "_PYTEST_PARALLEL", ("-n", "auto", "--dist", "loadfile"))
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        _junit(tmp_path / "j.xml", tests=1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    ok, why, evidence = runner._run_tests(tmp_path, tmp_path / "log.txt", 60,
                                          tmp_path / "j.xml", ["tests/"])
    assert ok, why
    assert evidence == "", "a passing run has nothing to quote"
    argv = captured["argv"]
    assert "-n" in argv and "auto" in argv and "loadfile" in argv
    assert any(str(a).startswith("--junitxml=") for a in argv)
    assert "tests/" in argv


def test_check_junit_sums_across_multiple_testsuites(tmp_path):
    """pytest-xdist can emit several <testsuite> elements; the counts add up."""
    path = tmp_path / "j.xml"
    path.write_text(
        '<testsuites>'
        '<testsuite tests="10" failures="0" errors="0"></testsuite>'
        '<testsuite tests="5" failures="1" errors="0"></testsuite>'
        '</testsuites>', encoding="utf-8")
    ok, why = runner._check_junit(path)
    assert not ok and "1 failure" in why and "15 test" in why


def test_ensure_integration_checkout_creates_a_sibling_on_the_base(tmp_path):
    """A dedicated worktree, on the integration branch, beside the launch checkout
    (never inside it — doc_scan would walk a checkout under Board/). Idempotent: a
    second night reuses the same one."""
    root, path = _split_repo(tmp_path)
    made = runner.ensure_integration_checkout(root, "development_team")
    assert made == path
    assert path.is_dir() and path != root
    assert path.parent == root.parent           # a sibling, outside the repo
    assert runner.current_branch(path) == "development_team"
    assert runner.ensure_integration_checkout(root, "development_team") == path  # reused


def test_ensure_integration_checkout_recovers_a_leftover_directory(tmp_path):
    """A crash can prune the worktree registration while leaving the directory
    behind (or one from a prior clone lingers). The branch carries every board
    commit, so the stray directory is cleared and recut rather than trusted."""
    root, path = _split_repo(tmp_path)
    path.mkdir(parents=True)
    (path / "junk.txt").write_text("stale\n", encoding="utf-8")
    made = runner.ensure_integration_checkout(root, "development_team")
    assert made == path and runner.current_branch(path) == "development_team"
    assert not (path / "junk.txt").exists()


def test_the_kill_switch_trips_in_either_root(tmp_path, monkeypatch):
    """The switch may land in the vault (the control root, where Karel drops it
    from his phone) or in the dedicated checkout. It must be honoured either way —
    the two are separate working trees now, and a switch that watched only one
    would be a promise the runner could quietly break."""
    ctrl, work = tmp_path / "ctrl", tmp_path / "work"
    (ctrl / ".ai").mkdir(parents=True)
    (work / ".ai").mkdir(parents=True)
    monkeypatch.setattr(runner, "_CTRL_ROOT", ctrl)
    monkeypatch.setattr(runner, "_WORK_ROOT", work)
    assert not runner._stop_requested()
    (work / runner.STOP_FILE).write_text("", encoding="utf-8")
    assert runner._stop_requested()             # in the dedicated checkout
    (work / runner.STOP_FILE).unlink()
    (ctrl / runner.STOP_FILE).write_text("", encoding="utf-8")
    assert runner._stop_requested()             # in the vault


def test_the_status_heartbeat_lands_in_the_control_root(tmp_path, monkeypatch):
    """`--status` is read from the launch checkout, so the heartbeat must be
    written there even though every dispatch-time caller passes the work root."""
    ctrl, work = tmp_path / "ctrl", tmp_path / "work"
    (ctrl / ".ai" / "runs").mkdir(parents=True)
    (work / ".ai" / "runs").mkdir(parents=True)
    monkeypatch.setattr(runner, "_CTRL_ROOT", ctrl)
    runner._status(work, phase="worker", card="x")
    assert (ctrl / runner.STATUS_FILE).is_file()
    assert not (work / runner.STATUS_FILE).is_file()


def test_a_run_uses_a_dedicated_checkout_and_leaves_the_launch_copy_alone(tmp_path, monkeypatch):
    """The card this part exists for: with the launch checkout on Karel's own
    branch — dirty with his work-in-progress — the runner dispatches, reviews,
    rebases and lands the card entirely in the dedicated `development_team`
    checkout, and never moves his branch or disturbs his edits."""
    root, path = _split_repo(tmp_path, "feat")
    (root / "scratch.py").write_text("wip = 1\n", encoding="utf-8")   # Karel mid-edit
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "done"})
    monkeypatch.setattr(runner, "review_branch",
                        lambda *a, **k: ({"verdict": "ok", "notes": "clean"}, 0.1, None))

    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))

    # Landed in testing/ on the dedicated checkout's board (rebased + merged for real).
    assert board.find(path, "feat").lane == "testing"
    assert runner.current_branch(path) == "development_team"
    # The launch checkout never moved off Karel's branch and his edit survived.
    assert runner.current_branch(root) == "karel/work"
    assert (root / "scratch.py").read_text(encoding="utf-8") == "wip = 1\n"


def test_on_base_the_runner_works_in_place_and_cuts_no_dedicated_checkout(tmp_path, monkeypatch):
    """The migration fallback: a launch checkout still on the integration branch
    can't also hold it in a second worktree (git forbids it), so the runner works
    in-place exactly as before and cuts no sibling checkout."""
    root = _loaded_board(tmp_path, "feat")            # HEAD on development_team
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})
    monkeypatch.setattr(runner, "review_branch",
                        lambda *a, **k: ({"verdict": "needs_decision", "question": "q?"},
                                         0.0, None))

    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))

    assert not runner.integration_checkout_path(root).exists()
    assert board.find(root, "feat").lane == "needs-decision"


def test_a_dirty_launch_checkout_off_base_does_not_block_preflight(tmp_path, monkeypatch):
    """The whole point of #3: once Karel is on his own branch, his uncommitted work
    is none of the runner's business. Preflight only objects to dirt when it will
    operate in the checkout itself (the on-base fallback)."""
    root, _ = _split_repo(tmp_path)
    (root / "scratch.py").write_text("wip = 1\n", encoding="utf-8")
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")  # not on PATH under test
    check = runner.preflight(root, "development_team", dry_run=False)
    assert check.ok, check.reasons


def test_rebase_and_merge_lands_a_clean_branch(tmp_path):
    """The happy path: nothing landed since the branch forked, so the rebase is a
    no-op, re-verification passes and the branch merges — development_team now
    carries its file."""
    root = _worktree_repo(tmp_path)               # on development_team
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert merged, why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"


def test_rebase_and_merge_deletes_the_branch_once_it_lands(tmp_path):
    """Once `branch`'s rebased commits are on development_team, nothing reads
    `ai/<id>` again — keeping it forever is exactly the stale-ref litter that
    made `git branch --merged` unable to recognise already-merged cards."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert merged, why
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_rebase_and_merge_replays_over_a_non_conflicting_sibling(tmp_path):
    """A sibling card touched a *different* file on development_team since this one
    forked. The rebase replays cleanly, re-verification passes, and both files end
    up on the integration branch — the case the old plain merge also handled, kept
    working."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    _commit_on_base(root, tmp_path, "sibling.py", "y = 2\n")

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert merged, why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (root / "sibling.py").read_text(encoding="utf-8") == "y = 2\n"


def test_rebase_and_merge_leaves_a_conflicting_branch_for_a_human(tmp_path):
    """The problem #2 fixes: a sibling edited the *same* file on development_team
    since this card was reviewed. The rebase conflicts, so the branch does NOT
    merge, development_team is left exactly where it was, and the reason names the
    file — never a guessed resolution (decision #3)."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "shared.py", "value = 'A'\n")
    _commit_on_base(root, tmp_path, "shared.py", "value = 'B'\n")
    base_before = runner._git(root, "rev-parse", "development_team").stdout.strip()

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert not merged
    assert "conflict" in why and "shared.py" in why
    # development_team is untouched — the sibling's version still stands.
    assert runner._git(root, "rev-parse", "development_team").stdout.strip() == base_before
    assert (root / "shared.py").read_text(encoding="utf-8") == "value = 'B'\n"
    # The branch is kept — a human still needs it to resolve the conflict.
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode == 0


def test_rebase_and_merge_deletes_the_branch_on_the_remote_too(tmp_path):
    """The gap this closes: `publish` had pushed `ai/probe`, the card merges, the
    local ref goes — and the remote's copy used to stay forever. Both copies must
    go, and for the same reason: the work is on the integration branch now."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")
    assert _remote_has(bare, "ai/probe"), "fixture: the branch must be on origin first"
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team",
                                          remote="origin")
    assert merged, why
    assert not _remote_has(bare, "ai/probe")
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_rebase_and_merge_touches_no_remote_without_a_publish_remote(tmp_path):
    """A host that never opted into pushing must not start deleting on a remote:
    an empty `publish_remote` (the schema default) leaves `origin/ai/probe` exactly
    where it is, even though the local branch still goes."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert merged, why
    assert _remote_has(bare, "ai/probe")
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_rebase_and_merge_refuses_to_delete_a_remote_carrying_unmerged_commits(tmp_path,
                                                                               monkeypatch):
    """The case that destroys work, and the one that makes the guard's *direction*
    load-bearing. `origin/ai/probe` has a commit pushed from another machine since
    this checkout last fetched: it was no part of what was just rebased, gated and
    merged, and deleting the remote would be the only copy gone. Refuse, log, and
    leave it — `publish`'s posture on a diverged branch.

    Note what this does NOT check: ancestry against `development_team`. What
    landed there is the rebased *copy*, so `ai/probe`'s own tip is never its
    ancestor — a guard phrased that way would pass this test by refusing every
    delete, including the three above. The guard is against the local branch."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")
    elsewhere = _advance_on_remote(bare, tmp_path, "ai/probe", "later.py", "y = 2\n")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team",
                                          remote="origin")

    assert merged, why
    assert _remote_has(bare, "ai/probe"), \
        "a remote carrying commits this checkout never merged must never be deleted"
    assert runner._git(bare, "rev-parse", "refs/heads/ai/probe").stdout.strip() == elsewhere
    assert any("did NOT delete" in line for line in logged), logged
    # The local delete is unaffected: those commits *are* on the integration branch.
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_rebase_and_merge_deletes_neither_copy_when_the_merge_fails(tmp_path):
    """A conflicting card keeps both refs: a human needs the branch to resolve it,
    and needs it reachable from wherever they are, which is the remote."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "shared.py", "value = 'A'\n")
    runner.publish(root, "origin", "development_team")
    _commit_on_base(root, tmp_path, "shared.py", "value = 'B'\n")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    merged, _ = runner.rebase_and_merge(root, card, "ai/probe", "development_team",
                                        remote="origin")
    assert not merged
    assert _remote_has(bare, "ai/probe")
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode == 0


def test_rebase_and_merge_says_nothing_about_a_branch_that_was_never_published(tmp_path,
                                                                               monkeypatch):
    """Most cards merge without ever having been pushed, so a remote with no such
    branch is the ordinary case — not a failure, and not something to report as
    one. The merge, and the local delete, proceed in silence."""
    root = _worktree_repo(tmp_path)
    _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team",
                                          remote="origin")

    assert merged, why
    assert not any("delete" in line for line in logged), logged
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_publish_is_a_no_op_with_no_remote_configured(tmp_path):
    """The local default: a laptop's `hosts.json` entry carries no
    `publish_remote`, so `runner.host_setting` returns `""` and `publish` must
    not attempt any network call — asserted by there being no `origin` at all
    and the call still succeeding."""
    root = _worktree_repo(tmp_path)
    runner.publish(root, "", "development_team")  # must not raise


def test_publish_pushes_base_and_every_ai_branch(tmp_path):
    """The scope Karel asked for: the integration branch (board, digest, merged
    cards) and every live `ai/<id>` card branch, so `git fetch` from anywhere
    else gets the whole night."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/parked-card", "feature.py", "x = 1\n")
    _commit_on_base(root, tmp_path, "extra.py", "y = 2\n")

    runner.publish(root, "origin", "development_team")

    assert _remote_tip(bare, "refs/heads/development_team") == \
        runner._git(root, "rev-parse", "development_team").stdout.strip()
    assert _remote_tip(bare, "refs/heads/ai/parked-card") == \
        runner._git(root, "rev-parse", "ai/parked-card").stdout.strip()


def test_publish_is_idempotent(tmp_path):
    """Called after every settled card, so a no-op second call (nothing moved
    since the last publish) must not error or change anything."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/parked-card", "feature.py", "x = 1\n")

    runner.publish(root, "origin", "development_team")
    tip_before = _remote_tip(bare, "refs/heads/ai/parked-card")
    runner.publish(root, "origin", "development_team")  # nothing changed locally
    assert _remote_tip(bare, "refs/heads/ai/parked-card") == tip_before


def test_publish_never_forces_the_integration_branch(tmp_path, monkeypatch):
    """A rejected push to `base` means someone else moved `origin/development_team`
    since this checkout last saw it — deciding whose commits win is a human call
    (§12), never guessed here. The remote must be left exactly where the
    divergent push left it, and the failure logged loudly."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    runner.publish(root, "origin", "development_team")  # base now in sync

    # Diverge the remote: a clone pushes a commit `root` has never seen.
    clone = tmp_path / "sibling-clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    subprocess.run(["git", "checkout", "-q", "development_team"], cwd=clone, check=True)
    (clone / "sibling.py").write_text("z = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "sibling advances development_team"],
                   cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "development_team"], cwd=clone, check=True)
    diverged_tip = _remote_tip(bare, "refs/heads/development_team")
    assert diverged_tip != runner._git(root, "rev-parse", "development_team").stdout.strip()

    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    runner.publish(root, "origin", "development_team")

    assert _remote_tip(bare, "refs/heads/development_team") == diverged_tip, \
        "a rejected push to base must never be forced"
    assert any("not pushed" in line or "not forced" in line for line in logged)


def test_publish_force_with_lease_on_the_trusted_branch(tmp_path):
    """The card just dispatched this call (`trusted_branch`) IS force-pushed even
    when the rewrite makes it a git-history sibling rather than a descendant of
    what was previously published — exactly what a cold-start retry produces
    (`prepare_worktree`'s FRESH path deletes the old branch and recreates it
    fresh from `base`), and exactly what `git commit --amend` models here."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")

    # Rewrite ai/probe locally (as a cold-start retry or a rebase replay would) —
    # `--amend` reparents onto the ORIGINAL parent, making this a sibling of the
    # previously-published tip, not a descendant of it.
    subprocess.run(["git", "checkout", "-q", "ai/probe"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--amend", "-m", "ai/probe: rewritten"],
                   cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "development_team"], cwd=root, check=True)
    rewritten_tip = runner._git(root, "rev-parse", "ai/probe").stdout.strip()

    runner.publish(root, "origin", "development_team", trusted_branch="ai/probe")

    assert _remote_tip(bare, "refs/heads/ai/probe") == rewritten_tip


def test_publish_refuses_a_diverged_untrusted_branch(tmp_path, monkeypatch):
    """The bug this fix closes (`menu-unlock-indicators`, 2026-07-28): a local
    `ai/<id>` branch nobody asked this call to trust, which has diverged from
    what `origin` now carries (a different, later attempt dispatched and
    published from somewhere else). Without the fetch+ancestry gate,
    `--force-with-lease` alone would wave this straight through — verified
    empirically against a real bare remote before this fix existed — silently
    destroying the remote's real, current work. `publish` must refuse and log,
    leaving the remote exactly as it was."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "real-work.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")  # origin now has the real attempt
    real_tip = _remote_tip(bare, "refs/heads/ai/probe")

    # This checkout never talked to origin about ai/probe before independently
    # creating its own, unrelated (dead/superseded) branch of the same name.
    subprocess.run(["git", "branch", "-D", "ai/probe"], cwd=root, check=True)
    _branch_with_file(root, tmp_path, "ai/probe", "dead-attempt.py", "y = 2\n")

    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    runner.publish(root, "origin", "development_team")  # no trusted_branch

    assert _remote_tip(bare, "refs/heads/ai/probe") == real_tip, \
        "a diverged, untrusted branch must never overwrite the remote's real work"
    assert any("diverged" in line for line in logged)


def test_publish_degrades_when_the_remote_is_not_configured(tmp_path, monkeypatch):
    """A `publish_remote` naming a remote this checkout never got (a misconfigured
    routine) must log and return, never raise — a push problem is an
    observability gap, not a reason to end the night."""
    root = _worktree_repo(tmp_path)
    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))

    runner.publish(root, "origin", "development_team")  # no `origin` remote exists

    assert any("not configured" in line for line in logged)


def test_card_branches_lists_only_the_ai_namespace(tmp_path):
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/one", "a.py", "1\n")
    _branch_with_file(root, tmp_path, "ai/two", "b.py", "2\n")
    subprocess.run(["git", "branch", "not-a-card-branch"], cwd=root, check=True)

    assert set(runner._card_branches(root)) == {"ai/one", "ai/two"}


def test_publish_remote_is_read_from_host_config(tmp_path):
    """The one setting that gates publishing: absent on the ordinary laptop
    (`host_setting` returns the default, `""`), present in the cloud override
    `night.py` writes (`"origin"`)."""
    assert runner.host_setting(tmp_path, "publish_remote", "") == ""
    (tmp_path / ".ai").mkdir(exist_ok=True)
    (tmp_path / runner.HOST_FILE).write_text(
        json.dumps({"publish_remote": "origin"}), encoding="utf-8")
    assert runner.host_setting(tmp_path, "publish_remote", "") == "origin"


# --- the durable staleness-sweep record (digest's "Staleness" section) -----


def test_stale_status_round_trips(tmp_path):
    assert stale_sweep.read_status(tmp_path) is None
    stale_sweep.write_status(tmp_path, "2026-07-26", checked=5, verified=4, carded=1)
    status = stale_sweep.read_status(tmp_path)
    assert status == {"last_run": "2026-07-26", "checked": 5, "verified": 4, "carded": 1}


def test_stale_status_survives_a_zero_checked_run(tmp_path):
    """'Ran, nothing to check' must read differently from 'never ran' — writing
    unconditionally on any completed sweep attempt is what makes that true."""
    stale_sweep.write_status(tmp_path, "2026-07-26", checked=0, verified=0, carded=0)
    assert stale_sweep.read_status(tmp_path)["last_run"] == "2026-07-26"


def test_stale_status_is_not_gitignored(tmp_path):
    """Unlike the ledger, this file has to survive on every machine and sync
    through git — a gitignored status would make `digest.py` lie about staleness
    the moment someone clones fresh or the ignored file gets cleaned up."""
    assert not str(stale_sweep.STATUS).endswith("stale_ledger.json")
    root = _repo(tmp_path)
    stale_sweep.write_status(root, "2026-07-26", checked=1, verified=1, carded=0)
    status = subprocess.run(["git", "status", "--porcelain", str(stale_sweep.STATUS)],
                            cwd=root, capture_output=True, text=True, check=True)
    assert "??" in status.stdout  # untracked, i.e. NOT ignored (ignored files don't show at all
                                  # under plain `git status`, but `--porcelain` without `-uall`
                                  # still lists them as untracked unless .gitignore excludes them)


# --- commit_board's extra_paths ---------------------------------------------


def test_commit_board_includes_an_existing_extra_path(tmp_path):
    root = _repo(tmp_path)
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "extra.json").write_text("{}", encoding="utf-8")
    board.commit_board(root, "board: probe", extra_paths=(".ai/extra.json",))
    log = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    assert "extra.json" in log.stdout


def test_commit_board_skips_a_missing_extra_path_without_crashing(tmp_path):
    root = _repo(tmp_path)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "tasks" / "x.md").write_text("x", encoding="utf-8")
    # Must not raise, and must still commit the real Board change, even though
    # the named extra path was never written.
    board.commit_board(root, "board: probe", extra_paths=(".ai/does_not_exist.json",))
    log = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    assert "x.md" in log.stdout


def test_a_night_records_every_dispatch_with_its_outcome(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b")
    _night(monkeypatch, root, [runner.Dispatch("failed", "gates: boom"),
                               runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    record = _only_record(root)
    assert [(d["card"], d["outcome"]) for d in record["dispatched"]] == \
        [("a", "failed"), ("b", "review")]
    assert record["complete"] is True
    assert record["cards_dispatched"] == 2


def test_a_recorded_failure_survives_the_card_going_back_to_tasks(tmp_path, monkeypatch):
    """THE regression. A failed attempt returns the card to `tasks/` — the lane it
    came from — so no lane diff can see it. The record is the only witness, and
    the digest's Failed section is built on it."""
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("failed", "gates: boom")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert board.find(root, "a").lane == "tasks"          # nothing moved...
    assert run_record.failures(_only_record(root))         # ...and it is still reported


def test_the_stop_reason_is_recorded_not_only_logged(tmp_path, monkeypatch):
    """A night that ended for an unrecorded reason reads in the digest as a night
    that simply ran out of cards."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d", "e")
    _night(monkeypatch, root, [runner.Dispatch("failed", "boom")] * 5)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert "failed in a row" in _only_record(root)["stop_reason"]


def test_max_cards_is_recorded_as_the_stop_reason(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))

    assert _only_record(root)["stop_reason"] == "reached --max-cards 1"


def test_a_skipped_card_is_recorded_with_the_reason_it_was_skipped(tmp_path, monkeypatch):
    """Nothing moves when a card is skipped, so this is the only place the reason
    can be recovered — and "five art cards need a GPU this host lacks" was
    invisible for a week."""
    root = _loaded_board(tmp_path, "a", "b")
    card = board.find(root, "b")
    card.write({"unattended": "false"})
    board.commit_board(root, "board: b needs a human")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    skipped = {s["card"]: s["reason"] for s in _only_record(root)["skipped"]}
    assert "b" in skipped
    assert "unattended" in skipped["b"]


def test_the_record_carries_the_landing_not_just_the_outcome(tmp_path, monkeypatch):
    """`failed` alone does not say whether the card retries or is in `failed/`."""
    root = _loaded_board(tmp_path, "a")
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        # What the real dispatch does before the worker starts — the attempt is
        # committed first so a crash cannot retry forever. The record reads the
        # attempt number off the same object, so the fake has to move it too.
        card.write({"attempts": str(card.attempts + 1), "started": "now"})
        return runner.Dispatch("failed", "gates: boom")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    entry = _only_record(root)["dispatched"][0]
    assert entry["attempt"] == 1
    assert "attempt 1 failed" in entry["landed"] and "will retry" in entry["landed"]


def test_the_records_totals_match_what_the_run_logged(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok", 1.25),
                               runner.Dispatch("review", "ok", 2.50)])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert _only_record(root)["cost_usd"] == 3.75


def test_a_dry_run_writes_no_record(tmp_path, monkeypatch):
    """`--dry-run` promises no LLM and no writes."""
    root = _loaded_board(tmp_path, "a")
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team", "--dry-run"]))

    assert not (root / run_record.DIR).exists()


def test_the_record_is_closed_before_the_digest_reads_it(tmp_path, monkeypatch):
    """Ordering matters: rendering first would publish a report of a night that
    had not yet admitted how it ended."""
    root = _loaded_board(tmp_path, "a")
    seen: list[bool] = []
    real_render = runner.digest.render

    def spy_render(work):
        seen.append(_only_record(work)["complete"])
        return real_render(work)

    monkeypatch.setattr(runner.digest, "render", spy_render)
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert seen == [True]


def test_append_digest_writes_the_non_advancing_commit_message(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--append-digest"]))

    subject = _last_commit_subject(root)
    assert subject == f"{runner.digest.APPEND_COMMIT_PREFIX}: {dt.date.today().isoformat()}"
    assert not subject.startswith("digest: ")   # must not accidentally match _DIGEST_COMMIT


def test_without_the_flag_the_commit_message_is_unchanged(tmp_path, monkeypatch):
    """Regression guard: the default path must still write exactly what it wrote
    before this feature existed — every test and skill that greps `digest: ` in
    git log depends on this string."""
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert _last_commit_subject(root) == f"digest: {dt.date.today().isoformat()}"


def test_an_append_run_does_not_close_the_window_for_the_next_run(tmp_path, monkeypatch):
    """End-to-end version of the digest-level windowing test: two real `runner.run()`
    calls, first with `--append-digest`, second without — the second run's own
    rendered `Digest.md` must still carry the first run's dispatch.

    Each run targets a specific card via `--card` rather than relying on
    `select()`'s ordering, and lands it for real (see `_fake_review_run`) so the
    card is genuinely out of `tasks/` and any later appearance in the digest can
    only come from the per-run report, not the queue.
    """
    root = _loaded_board(tmp_path, "a", "b")
    _fake_review_run(monkeypatch, root, "a")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "a", "--append-digest"]))

    # `_previous_digest`/`records_since` compare at second precision, and these
    # two `run()` calls otherwise land in the same wall-clock second — a purely
    # test-speed artefact (real runs are minutes apart), but without the gap the
    # second run's own record can tie its commit's timestamp and get excluded
    # from its own report by the strict `>` comparison.
    time.sleep(1.1)
    _fake_review_run(monkeypatch, root, "b")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "b"]))

    text = (root / "Digest.md").read_text(encoding="utf-8")
    assert "[[a|" in text   # first run's dispatch, still visible in a per-run block
    assert "[[b|" in text   # second run's own dispatch


def test_a_normal_run_after_an_append_run_closes_the_backlog(tmp_path, monkeypatch):
    """A third run, after the non-append run above, must report only itself —
    otherwise the backlog never actually closes and the digest grows forever.

    All three land in `testing/` for real, so a card already reported cannot
    reappear via the standing Queue section (it is no longer in `tasks/`) or the
    standing testing/review section (which only counts, never names). `[[<id>|`
    appearing anywhere is specifically evidence of a per-run block.
    """
    root = _loaded_board(tmp_path, "a", "b", "c")
    _fake_review_run(monkeypatch, root, "a")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "a", "--append-digest"]))

    # See the sibling test above for why the gap is needed: second-precision
    # window comparisons otherwise race against three `run()` calls landing in
    # the same wall-clock second.
    time.sleep(1.1)
    _fake_review_run(monkeypatch, root, "b")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "b"]))   # closes the backlog

    time.sleep(1.1)
    _fake_review_run(monkeypatch, root, "c")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "c"]))

    text = (root / "Digest.md").read_text(encoding="utf-8")
    assert "[[c|" in text                              # this run's own dispatch
    assert "[[a|" not in text and "[[b|" not in text   # already closed out two runs ago


def test_the_run_label_names_append_mode(tmp_path, monkeypatch):
    """So Karel can tell from the rendered block itself why a run's report is
    stacked on an earlier one instead of standing alone."""
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--append-digest"]))

    assert "append mode" in _only_record(root)["label"]


def test_night_defaults_to_append_digest(tmp_path):
    """The unattended path — nobody at the keyboard to read tonight's report
    before tomorrow night's run overwrites it."""
    assert "--append-digest" in night.DEFAULT_ARGS


def test_a_crashed_dispatch_fails_that_card_and_the_queue_carries_on(tmp_path, monkeypatch):
    """THE regression. The middle card's dispatch raises; the two either side of
    it must still run and the night must end normally."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, FileNotFoundError(
        "[WinError 206] The filename or extension is too long"))

    code = runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert code == 0                       # the exit code an ordinary failure returns


def test_a_crashed_dispatch_is_recorded_as_a_failure_not_as_a_short_night(tmp_path, monkeypatch):
    """The digest reports the record, so a crash the record misses reads as a
    night that simply ran out of cards."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    _crashing_night(monkeypatch, root, FileNotFoundError(
        "[WinError 206] The filename or extension is too long"))
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    record = _only_record(root)
    crashed = [d for d in record["dispatched"] if d["card"] == "b"]
    assert len(crashed) == 1
    assert crashed[0]["outcome"] == "failed"
    # The one-line detail reads as a cause, not as noise.
    assert crashed[0]["detail"] == ("dispatch crashed — FileNotFoundError: "
                                    "[WinError 206] The filename or extension is too long")
    assert record["cards_dispatched"] == 3


def test_a_crash_in_a_later_stage_is_contained_too(tmp_path, monkeypatch):
    """The load-bearing property: the guard covers the whole per-card region, not
    two named calls. `review_stage` runs *after* `dispatch` and only on a `review`
    outcome, so a guard that named `dispatch` alone would let this one out — and a
    stage added after this card is written must be inside by construction."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        return runner.Dispatch("review", "ok")

    def fake_review_stage(root_, card, result, base, card_budget, timeout):
        if card.id == "b":
            raise ValueError("the reviewer's own machinery fell over")
        return runner.Dispatch("reviewed", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "review_stage", fake_review_stage)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")

    code = runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert code == 0
    assert [d["outcome"] for d in _only_record(root)["dispatched"]] == \
        ["reviewed", "failed", "reviewed"]


def test_the_crashed_cards_error_names_the_card_and_carries_the_traceback(tmp_path, monkeypatch):
    """A raw `WinError 206` naming nothing is what turned a five-minute problem
    into a half-hour one. A reader must not have to open the gitignored
    `.ai/runs/` to learn which card crashed and roughly why."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    _crashing_night(monkeypatch, root, FileNotFoundError(
        "[WinError 206] The filename or extension is too long"), real_settle=True)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    error = board.section(board.find(root, "b").text, "Error")
    assert "dispatching card `b`" in error and "dispatch crashed" in error
    assert "FileNotFoundError" in error                     # the exception type
    assert "Traceback (most recent call last)" in error      # and the traceback
    # The frame that raised — which is wherever `_crashing_night` is defined, not
    # this file. Derived from the module rather than spelled, because it used to
    # read "test_runner.py" and the split moved the helper out from under it.
    assert Path(_runner_helpers.__file__).name in error


def test_a_crash_leaves_no_worktree_and_banks_what_the_worker_managed(tmp_path, monkeypatch):
    """`dispatch`'s own exit paths would have done this; a crash skips them. On
    2026-08-06 that left `.dungeoneer-worktrees/<id>` and its branch for hand
    cleaning before the night could be relaunched."""
    root = _loaded_board(tmp_path, "a")

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        tree, _branch, _mode = runner.prepare_worktree(root_, card, base)
        (tree / "half-done.txt").write_text("what the worker managed\n", encoding="utf-8")
        raise RuntimeError("crashed with the checkout still on disk")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert not (runner.worktree_root(root) / "a").exists()
    # Banked before the checkout went, so the crash costs the process, not the work.
    committed = subprocess.run(["git", "show", "--name-only", "--format=%s", "ai/a"],
                               cwd=root, capture_output=True, text=True).stdout
    assert "wip: a interrupted" in committed
    assert "half-done.txt" in committed


def test_a_cleanup_that_itself_raises_does_not_re_crash_the_loop(tmp_path, monkeypatch):
    """The handler is the last line of defence, so it must not be the next thing
    that ends the night."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, RuntimeError("boom"))
    monkeypatch.setattr(runner, "clear_handover",
                        lambda r, cid: (_ for _ in ()).throw(OSError("cleanup died too")))

    code = runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert code == 0


def test_ctrl_c_still_stops_the_night(tmp_path, monkeypatch):
    """`except Exception`, never a bare `except`. A `KeyboardInterrupt` filed as
    an `## Error` against whatever card happened to be in flight would be worse
    than the crash this guard contains."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, KeyboardInterrupt(), on_card="b")

    with pytest.raises(KeyboardInterrupt):
        runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b"]             # it stopped, it did not carry on to c


def test_a_systemexit_is_not_swallowed_either(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, SystemExit(2), on_card="b")

    with pytest.raises(SystemExit):
        runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b"]


def test_a_systematic_crash_is_stopped_by_the_existing_failure_streak(tmp_path, monkeypatch):
    """The night is not defenceless against a crash that is about the harness
    rather than the card — but the net is `CONSECUTIVE_FAILURE_STOP`, which
    already exists, not a second one added beside it."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d", "e")
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        raise FileNotFoundError("[WinError 206] The filename or extension is too long")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert "failed in a row" in _only_record(root)["stop_reason"]


def test_the_crash_guard_wraps_a_region_rather_than_naming_its_stages(tmp_path):
    """The structural half of the load-bearing property, so it survives someone
    adding a stage without reading the tests above: the run loop's guarded call
    is one call to the pipeline helper, and the stages live inside that helper —
    not a `try` listing `dispatch` and `review_stage` by name."""
    source = _RUNNER_SOURCE.read_text(encoding="utf-8")
    loop = source[source.index("def run(root: Path, args: argparse.Namespace)"):]
    guarded = re.search(r"try:\n\s+result = (\w+)\(candidate, model\)\n"
                        r"\s+except Exception as exc:", loop)
    assert guarded, "the run loop's per-card guard is not a single guarded pipeline call"
    helper = guarded.group(1)
    body = loop[loop.index(f"def {helper}(candidate"):loop.index("def _settled(")]
    assert "dispatch(" in body and "review_stage(" in body, \
        f"`{helper}` is not where the per-card stages live, so the guard has stopped " \
        "covering the region it is meant to"


# --- an api_error interruption is not a verdict (failed-attempt-work-is- -----
# --- deleted-not-resumed, the api_error slice) ------------------------------
#
# corridor-generation-redesign attempt 1: a disconnected socket after 121
# turns, 48.6 min, $12.80 — no gate log, no pytest log, because the run never
# reached verification. `commit_wip` rescued the diff as a `wip:` commit; the
# next cold start would have `git branch -D`'d it, and with no handover
# nothing would have resumed. The fix keys on `terminal_reason` plus the
# *absence* of a verification log, never on the exit code (1, same as an
# ordinary self-reported failure).

def test_api_error_with_no_verification_log_is_an_interruption(tmp_path):
    out_dir = tmp_path / "attempt-1"
    out_dir.mkdir()
    (out_dir / "worker-1.json").write_text(
        json.dumps({"terminal_reason": "api_error"}), encoding="utf-8")
    assert runner._api_error_interruption(out_dir, 1) is True


def test_api_error_after_gates_ran_is_not_an_interruption(tmp_path):
    """A worker that *did* reach verification and failed it must never be
    treated as merely interrupted — that would be a way to inherit a red tree
    for free, exactly what the rejected parts of option C would have allowed."""
    out_dir = tmp_path / "attempt-1"
    out_dir.mkdir()
    (out_dir / "worker-1.json").write_text(
        json.dumps({"terminal_reason": "api_error"}), encoding="utf-8")
    (out_dir / "gates.txt").write_text("", encoding="utf-8")
    assert runner._api_error_interruption(out_dir, 1) is False


def test_api_error_after_pytest_ran_is_not_an_interruption(tmp_path):
    out_dir = tmp_path / "attempt-1"
    out_dir.mkdir()
    (out_dir / "worker-1.json").write_text(
        json.dumps({"terminal_reason": "api_error"}), encoding="utf-8")
    (out_dir / "pytest.txt").write_text("", encoding="utf-8")
    assert runner._api_error_interruption(out_dir, 1) is False


def test_a_non_api_error_exit_1_is_never_read_as_an_interruption(tmp_path):
    """Both a genuine failure and an api_error interruption exit 1 — the
    classifier must key on `terminal_reason`, never on the code."""
    out_dir = tmp_path / "attempt-1"
    out_dir.mkdir()
    (out_dir / "worker-1.json").write_text(
        json.dumps({"terminal_reason": "some_other_reason"}), encoding="utf-8")
    assert runner._api_error_interruption(out_dir, 1) is False


def test_an_api_error_before_verification_gives_the_attempt_back_and_hands_over(
        tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        (Path(cwd) / "wip.txt").write_text("draft", encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 1, json.dumps({"total_cost_usd": 0.2, "terminal_reason": "api_error",
                                 "session_id": "sess-api-1"}),
            "Connection closed mid-response")
    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "interrupted"
    # `kept` means the *checkout* was preserved for a warm resume in place, and
    # this path drops it — `settle` renders it as "worktree kept for warm
    # resume", so claiming it here would put a flat falsehood in the one line a
    # 6 AM reader trusts. The resume rides on the `wip:` commit and the handover
    # (`FROM_WIP`), asserted below, not on a kept worktree.
    assert result.kept is False

    landed = runner.settle(root, "probe", result)
    assert "attempt given back" in landed
    assert "api_error" in landed
    assert "worktree kept" not in landed

    settled_card = board.find(root, "probe")
    assert settled_card.lane == "tasks"
    assert settled_card.attempts == 0, "the given-back attempt must not be spent"

    handover = runner.read_handover(root, "probe")
    assert handover.session_id == "sess-api-1"

    # The `wip:` commit is on the branch, not lost with the dropped worktree.
    log = subprocess.run(["git", "log", "--format=%s", "ai/probe"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "wip: probe interrupted" in log
    assert not (runner.worktree_root(root) / "probe").exists()

    # And the next dispatch resumes from that commit rather than cold-starting.
    _tree, _branch, mode = runner.prepare_worktree(root, settled_card, "development_team")
    assert mode == runner.FROM_WIP


def test_an_interrupted_dispatch_does_not_stop_the_night(tmp_path, monkeypatch):
    """Unlike `blocked`, an interruption is a fact about one connection, not
    about the repo or this machine — the rest of the queue is still good."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [
        runner.Dispatch("interrupted", "worker interrupted (api_error)"),
        runner.Dispatch("review", "ok"),
    ])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b"]


def test_interruptions_count_toward_the_consecutive_failure_breaker(tmp_path, monkeypatch):
    """One dropped connection is one card's bad luck; three in a row is this
    machine's network, and walking the rest of the queue only spends money on
    finding that out again. Before `interrupted` existed these were `failed` and
    tripped the breaker — introducing the outcome must not quietly remove that
    net, in either direction: an alternating failed/interrupted night must trip
    it too, or neither half ever reaches three."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d", "e")

    calls = _night(monkeypatch, root, [
        runner.Dispatch("interrupted", "worker interrupted (api_error)"),
        runner.Dispatch("failed", "worker exited 1"),
        runner.Dispatch("interrupted", "worker interrupted (api_error)"),
        runner.Dispatch("review", "ok"),
        runner.Dispatch("review", "ok"),
    ])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b", "c"]
    assert "failed in a row" in _only_record(root)["stop_reason"]


def test_the_modes_that_cannot_edit_are_a_named_category_not_a_literal():
    """`category-of-one-read-as-a-literal`, caught while adding a second member.

    While `default` was the only answer, "cannot edit" and `"default"` were the
    same string, so each consumer spelled the string out — `update.merge` did, from
    the day it was written until 2026-08-17. The arrival of `plan` meant every
    consumer had to be recalled from memory rather than found by following a
    reference, which is what the class describes.
    """
    assert set(runner.MODES_WITHOUT_EDIT) == {"default", "plan"}

    package = Path(runner.__file__).parent
    for module in ("ingest.py", "update.py"):
        text = (package / module).read_text(encoding="utf-8")
        assert 'permission_mode == "default"' not in text, (
            f"{module} asks about one member instead of the category")


@pytest.mark.parametrize("mode", ["default", "plan"])
def test_cannot_edit_names_the_mode_so_the_message_can_quote_it(tmp_path, mode):
    _host_mode(tmp_path, mode)
    assert runner.cannot_edit(tmp_path) == mode


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions", "aModeFromNextYear"])
def test_cannot_edit_is_silent_for_a_mode_that_writes_or_is_unknown(tmp_path, mode):
    """Enumerated rather than "outside the allowed set": a verb that refuses to
    start on an unrecognised string is worse than one that tries and reports what
    happened."""
    _host_mode(tmp_path, mode)
    assert runner.cannot_edit(tmp_path) == ""


def test_a_machine_with_no_host_entry_can_write():
    """A fresh clone must be able to card, so the fallback is `acceptEdits`."""
    assert runner.cannot_edit(Path(__file__).parent) == ""
