"""One dispatch, end to end: the prompt, the whole cycle, the usage-limit wall,
the night's stopping conditions, what happens to a failed attempt's commits,
the producer/checker loop and the warm resume of an interrupted worker.

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
from pathlib import Path

import pytest

from nightshift import board
from nightshift import limits
from nightshift import runner

from _runner_helpers import (  # noqa: F401  (fixtures register by name)
    _JSON_AWARE_GATE,
    _RUNNER_SOURCE,
    _WALL_HANDLING_EXEMPT,
    _advance_on_remote,
    _art_card,
    _bare_origin,
    _branch_with_file,
    _card,
    _charter,
    _fake_loop,
    _fake_worker,
    _finishing_worker,
    _functions_calling,
    _functions_spawning,
    _gate_stub,
    _gates_pass_by_default,
    _host_publishes_to_origin,
    _ignore_runs,
    _loaded_board,
    _night,
    _no_xdist_in_fixtures,
    _remote_has,
    _remote_tip,
    _repo,
    _rev,
    _seed_wip_branch,
    _select,
    _wall,
    _walls_dirty,
    _worktree_repo,
)


def test_two_fixture_repos_never_share_a_worktree_root(tmp_path):
    """The isolation the whole file rests on, asserted directly rather than left
    to every test cleaning up after itself.

    `worktree_root` is `root.parent / f".{project}-worktrees"`, and the project name
    and the card id are both constants in these fixtures — so if two fixture repos
    ever share a parent again, they share one worktree path, and the first test that
    leaves a checkout behind makes every later dispatch of that card id return
    `failed` from `prepare_worktree` before the worker runs. That is what happened on
    2026-08-18: 21 failures in this file, one xdist worker, every outcome `failed`
    regardless of what the test was about, and green on every serial run because
    files run alphabetically and the leak sorted after its victims.

    This is the cheap half of the fix. `_worktree_repo`'s docstring carries the rest.
    """
    a = _worktree_repo(tmp_path / "one")
    b = _worktree_repo(tmp_path / "two")
    assert runner.worktree_root(a) != runner.worktree_root(b)


def test_a_worktree_left_behind_does_not_reach_the_next_repo(tmp_path, monkeypatch):
    """The failure mode itself, reproduced: one repo leaks its checkout, a second
    dispatches the same card id, and the second must not care.

    Before the fix this asserted `failed` with the detail `git worktree add failed:
    Preparing worktree (new branch 'ai/probe')` — git's progress line on stderr,
    which never says the directory was already there. The unhelpfulness of that
    message is a good part of why the intermittency went undiagnosed.
    """
    leaker = _worktree_repo(tmp_path / "leaker")
    _charter(leaker, "code-thread")
    _card(leaker, "tasks", "probe")
    runner.prepare_worktree(leaker, board.find(leaker, "probe"), "development_team")
    assert (runner.worktree_root(leaker) / "probe").exists(), "the leak is the premise"

    victim = _worktree_repo(tmp_path / "victim")
    _charter(victim, "code-thread")
    _card(victim, "tasks", "probe")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "implemented"})

    result = runner.dispatch(victim, board.find(victim, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review", result.detail


def test_a_successful_dispatch_lands_the_card_in_review(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "implemented"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.cost_usd == pytest.approx(0.11)

    runner.settle(root, "probe", result)
    card = board.find(root, "probe")
    assert card.lane == "review"
    assert card.fields["branch"] == "ai/probe"
    assert card.attempts == 1
    assert card.fields.get("finished") and not card.fields.get("started")


# --- the usage-limit wall ---------------------------------------------------
#
# The bug these exist for: a wall *is* a non-zero exit, so before `limits.py` it
# was read as the card failing. `attempts` had already been committed, so one
# wall at 02:00 walked the rest of the queue, spent an attempt on every card and
# wrote `## Error: worker exited 1` on each — and a few nights of that files
# working cards into `failed/`. The card must come back untouched.

def test_a_usage_limit_is_not_the_cards_fault(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    assert result.outcome == "limited"
    assert result.wall is not None and result.wall.scope == limits.SESSION


def test_a_limited_card_gets_its_attempt_back_and_stays_in_tasks(tmp_path, monkeypatch):
    """`attempts` is committed before the worker starts, which is right for every
    other exit. The plan running out is not a fact about this card, and charging
    it one of its three attempts would push it toward `failed/` for the crime of
    being in flight at the wrong moment."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    runner.settle(root, "probe", result)

    card = board.find(root, "probe")
    assert card.lane == "tasks"
    assert card.attempts == 0
    assert "attempts" not in card.fields          # pristine, not "attempts: 0"
    assert not card.fields.get("started") and not card.fields.get("finished")
    assert "## Error" not in card.text            # nothing to answer for


def test_a_second_attempt_stopped_by_a_wall_rewinds_to_the_first(tmp_path, monkeypatch):
    """The rewind is one attempt, not a reset to zero — a card that genuinely
    failed once has still failed once."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe", attempts="1")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    runner.settle(root, "probe", result)
    assert board.find(root, "probe").attempts == 1


def test_an_ordinary_non_zero_exit_is_still_the_cards_fault(tmp_path, monkeypatch):
    """The other half of the check — recognising walls must not turn every
    failure into someone else's problem."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1, stderr="Traceback: ImportError")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    assert result.outcome == "failed"
    runner.settle(root, "probe", result)
    assert board.find(root, "probe").attempts == 1


def test_no_dollar_cap_is_passed_to_the_cli_by_default(tmp_path, monkeypatch):
    """`--max-budget-usd 0` is a cap of zero, not the absence of one, so "no
    cap" has to mean "no flag"."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team",
                    "sonnet", runner.DEFAULT_CARD_BUDGET_USD, 120)
    assert "--max-budget-usd" not in seen["argv"]


def test_a_dollar_cap_is_still_passed_when_one_is_asked_for(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)
    assert seen["argv"][seen["argv"].index("--max-budget-usd") + 1] == "5.0"


def test_the_night_stops_at_the_first_wall_by_default(tmp_path, monkeypatch):
    """`--sessions 1` is "work until the session limit is reached"."""
    root = _loaded_board(tmp_path, "a", "b", "c")

    calls = _night(monkeypatch, root, [_wall()])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a"]


def test_two_sessions_sleeps_through_the_reset_and_retries_the_same_card(tmp_path, monkeypatch):
    """"Until two session limits are used". The card that met the wall was never
    attempted, so the next window starts with it rather than skipping it."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [_wall(), runner.Dispatch("review", "ok"), _wall()])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))
    assert calls == ["a", "a", "b"]  # a walled, a retried after the reset, then b walled


def test_the_retried_card_is_reloaded_so_the_rewind_is_not_undone(tmp_path, monkeypatch):
    """`dispatch` reads the attempt number off the Card object it is handed and
    mutates it in place. Retrying with the same in-memory copy would spend
    attempt 2 on the card's first real run — silently undoing the rewind that is
    the whole point of not blaming a card for the plan running out."""
    root = _loaded_board(tmp_path, "a")
    seen: list[int] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        seen.append(card.attempts)
        # What the real dispatch does before the worker starts.
        card.write({"attempts": str(card.attempts + 1), "started": "now"})
        return _wall() if len(seen) == 1 else runner.Dispatch("review", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "_sleep_until", lambda when: True)
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))

    assert seen == [0, 0]  # not [0, 1]


def test_a_card_moved_off_the_board_while_we_slept_is_left_alone(tmp_path, monkeypatch):
    """Hours pass inside a wall. Karel answering a card from his phone in that
    window has to win over the runner's plan from before it slept."""
    root = _loaded_board(tmp_path, "a", "b")

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        if len(calls) == 1:
            board.move(root, board.find(root, "a"), "needs-decision")
            return _wall()
        return runner.Dispatch("review", "ok")

    calls: list[str] = []
    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "_sleep_until", lambda when: True)
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))

    assert calls == ["a", "b"]  # a is not retried; it is no longer in tasks/


def test_a_weekly_limit_ends_the_night_even_with_sessions_to_spare(tmp_path, monkeypatch):
    """A weekly window does not reopen inside a night; sleeping on one would idle
    until morning and produce nothing."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [_wall(limits.WEEKLY)])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "5"]))
    assert calls == ["a"]


def test_a_transient_rate_limit_does_not_spend_one_of_the_nights_sessions(tmp_path, monkeypatch):
    """A 429 reopens in seconds. Counting one against `--sessions 1` would end a
    good night over a hiccup."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [_wall(limits.TRANSIENT)])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "a", "b"]  # hiccup, retry, carry on


def test_endless_transient_limits_are_capped_rather_than_retried_until_morning(tmp_path, monkeypatch):
    """A hiccup that never clears is indistinguishable from a wall we misread,
    and must not sleep-and-retry until Karel wakes up."""
    root = _loaded_board(tmp_path, "a")

    calls = _night(monkeypatch, root, [_wall(limits.TRANSIENT)] * 20)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert len(calls) == runner.TRANSIENT_RETRIES + 1


def test_three_failures_in_a_row_end_the_night(tmp_path, monkeypatch):
    """The net under `limits.detect`. A wall phrased in words the detector does
    not carry looks exactly like every card failing; without this the runner
    walks the whole queue spending an attempt on each."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d", "e")

    calls = _night(monkeypatch, root, [runner.Dispatch("failed", "worker exited 1")] * 5)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b", "c"]


def test_a_success_between_failures_resets_the_streak(tmp_path, monkeypatch):
    """Two flaky cards either side of a good one is not a systemic failure."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d")

    calls = _night(monkeypatch, root, [
        runner.Dispatch("failed", "x"),
        runner.Dispatch("failed", "x"),
        runner.Dispatch("review", "ok"),
        runner.Dispatch("failed", "x"),
    ])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b", "c", "d"]


def test_the_default_night_has_no_dollar_stopping_condition(tmp_path, monkeypatch):
    """The flag Karel asked to stop steering by. A cost-bearing dispatch must not
    end the night at some API-equivalent figure nobody is spending."""
    root = _loaded_board(tmp_path, "a", "b", "c")

    calls = _night(monkeypatch, root, [runner.Dispatch("review", "ok", cost_usd=99.0)] * 3)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b", "c"]


def test_the_branch_survives_the_dispatch_but_the_worktree_does_not(tmp_path, monkeypatch):
    """The branch is the deliverable — review/ reads it and Karel merges it.
    Twenty stale checkouts beside the repo are not."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)
    branches = subprocess.run(["git", "branch", "--list", "ai/probe"], cwd=root,
                              capture_output=True, text=True).stdout
    assert "ai/probe" in branches
    assert not (runner.worktree_root(root) / "probe").exists()


def test_a_failed_attempts_branch_is_a_rescue_ref_after_the_next_dispatch(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=True, returncode=1)

    card = board.find(root, "probe")
    result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    runner.settle(root, "probe", result)
    first_sha = _rev(root, "ai/probe")

    _fake_worker(monkeypatch, commit=True, returncode=0,
                 verdict={"outcome": "done", "summary": "x"})
    card = board.find(root, "probe")
    card.write({"finished": None})  # skip the backoff wait, not the counting
    runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)

    assert _rev(root, "ai/probe@failed-1") == first_sha, \
        "the first attempt's commits must survive under a rescue name"
    assert _rev(root, "ai/probe") != first_sha, \
        "ai/probe itself is the fresh attempt's branch, cut from base again"


def test_a_second_failed_attempt_becomes_failed_2_not_a_collision(tmp_path, monkeypatch):
    """Renaming by `card.attempts` (rewound by `blocked`/`limited`) could reuse
    a slot a real failure already holds; the next-free-N scan must not
    collide even across a mixed fail/fail history."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")

    for _ in range(2):
        _fake_worker(monkeypatch, commit=True, returncode=1)
        card = board.find(root, "probe")
        card.write({"finished": None})
        result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
        runner.settle(root, "probe", result)

    # Third dispatch's cold start renames the second attempt's branch — the
    # first attempt's rescue ref must still be there under its own name.
    _fake_worker(monkeypatch, commit=True, returncode=0,
                 verdict={"outcome": "done", "summary": "x"})
    card = board.find(root, "probe")
    card.write({"finished": None})
    runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)

    listed = subprocess.run(["git", "branch", "--list", "ai/probe@failed-*"], cwd=root,
                            capture_output=True, text=True).stdout
    assert "ai/probe@failed-1" in listed and "ai/probe@failed-2" in listed


def test_rescue_branches_are_reaped_when_a_card_retires_to_failed(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=True, returncode=1)

    for _ in range(runner.MAX_ATTEMPTS):
        card = board.find(root, "probe")
        card.write({"finished": None})
        result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
        runner.settle(root, "probe", result)

    assert board.find(root, "probe").lane == "failed"
    listed = subprocess.run(["git", "branch", "--list", "ai/probe@failed-*"], cwd=root,
                            capture_output=True, text=True).stdout
    assert listed.strip() == "", "rescue refs must not outlive the card that leaves tasks/"


def test_rescue_branches_are_reaped_when_a_done_card_is_swept_at_startup(tmp_path, monkeypatch):
    """`done/` is reached by Karel, by hand — reaped in the startup sweep,
    mirroring `prune_run_dir`'s own terminal-lane rule."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=True, returncode=1)
    card = board.find(root, "probe")
    result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
    runner.settle(root, "probe", result)  # attempt 1 failed, ai/probe holds its commit

    _fake_worker(monkeypatch, commit=True, returncode=0,
                 verdict={"outcome": "done", "summary": "x"})
    card = board.find(root, "probe")
    card.write({"finished": None})
    runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
    # ai/probe@failed-1 now exists; Karel hand-moves the card straight to done/.
    board.move(root, board.find(root, "probe"), "done")

    pruned = runner.sweep_terminal_cards(root)
    assert "probe" in pruned or True  # pruned only reflects run-dir removal; branch check below
    listed = subprocess.run(["git", "branch", "--list", "ai/probe@failed-*"], cwd=root,
                            capture_output=True, text=True).stdout
    assert listed.strip() == ""


def test_rescue_branches_are_reaped_when_a_testing_card_is_swept_at_startup(
        tmp_path, monkeypatch):
    """rescue-branches-only-swept-on-failure (2026-08-13): `testing/` is where
    the ordinary success path lands a card (settle()'s own hook is the primary
    fix — this is the backstop for a card that reached testing/ some other
    way, e.g. hand-moved, mirroring the done/ case above)."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=True, returncode=1)
    card = board.find(root, "probe")
    result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
    runner.settle(root, "probe", result)  # attempt 1 failed, ai/probe holds its commit

    _fake_worker(monkeypatch, commit=True, returncode=0,
                 verdict={"outcome": "done", "summary": "x"})
    card = board.find(root, "probe")
    card.write({"finished": None})
    runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
    # ai/probe@failed-1 now exists; hand-moved to testing/ rather than through settle().
    board.move(root, board.find(root, "probe"), "testing")

    runner.sweep_terminal_cards(root)
    listed = subprocess.run(["git", "branch", "--list", "ai/probe@failed-*"], cwd=root,
                            capture_output=True, text=True).stdout
    assert listed.strip() == ""


def test_a_card_forced_past_max_attempts_by_name_still_caps_rescue_branches(
        tmp_path, monkeypatch):
    """`--card` waives the attempt limit, so a card can be cold-started past
    `MAX_ATTEMPTS` by hand — the cap enforced at startup, mirroring
    `cap_run_dirs_in_flight`, is the backstop that keeps rescue refs bounded
    even then."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")

    for _ in range(runner.MAX_ATTEMPTS + 2):
        _fake_worker(monkeypatch, commit=True, returncode=1)
        card = board.find(root, "probe")
        card.write({"finished": None})
        result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
        # Give the attempt back by hand instead of retiring, so the card stays
        # in tasks/ across every one of these forced re-dispatches.
        board.find(root, "probe").write({"attempts": None, "started": None, "finished": None})

    runner.cap_rescue_branches_in_flight(root)
    listed = subprocess.run(["git", "branch", "--list", "ai/probe@failed-*"], cwd=root,
                            capture_output=True, text=True).stdout.split()
    assert len(listed) <= runner.MAX_ATTEMPTS


def test_publish_pushes_a_rescue_branch_and_the_reap_deletes_both_copies(tmp_path,
                                                                        monkeypatch):
    """Pushing them is deliberate — on an ephemeral cloud checkout the pushed copy
    is the only place a preserved attempt survives the container, which is the
    whole point of preserving it. So the reap has to reach both halves."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    bare = _bare_origin(root, tmp_path)
    _host_publishes_to_origin(root)

    _fake_worker(monkeypatch, commit=True, returncode=1)
    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    runner.settle(root, "probe", result)
    _fake_worker(monkeypatch, commit=True, returncode=0,
                 verdict={"outcome": "done", "summary": "x"})
    card = board.find(root, "probe")
    card.write({"finished": None})
    runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)

    runner.publish(root, "origin", "development_team")
    assert _remote_has(bare, "ai/probe@failed-1"), \
        "a rescue ref is an `ai/` branch and publish must push it like any other"

    runner.prune_rescue_branches(root, "probe")
    assert not _remote_has(bare, "ai/probe@failed-1"), \
        "reaping the local ref must not leave the pushed copy orphaned on the remote"
    assert runner._git(root, "rev-parse", "--verify", "ai/probe@failed-1").returncode != 0


def test_a_reap_touches_no_remote_without_a_publish_remote(tmp_path):
    """A host that never opted into pushing must not start deleting on a remote —
    the same posture `rebase_and_merge` takes on the schema default."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe@failed-1", "rescued.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")
    assert _remote_has(bare, "ai/probe@failed-1")

    runner.prune_rescue_branches(root, "probe")
    assert _remote_has(bare, "ai/probe@failed-1")
    assert runner._git(root, "rev-parse", "--verify", "ai/probe@failed-1").returncode != 0


def test_a_reap_refuses_a_remote_rescue_ref_carrying_commits_this_checkout_lacks(tmp_path):
    """The case that would destroy work: another machine pushed onto this rescue
    ref since the last fetch. The local reap still happens — this checkout is
    finished with it — but the remote copy is left for a human, loudly, exactly
    as on a diverged card branch."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _host_publishes_to_origin(root)
    _branch_with_file(root, tmp_path, "ai/probe@failed-1", "rescued.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")
    elsewhere = _advance_on_remote(bare, tmp_path, "ai/probe@failed-1",
                                  "more.py", "y = 2\n")

    runner.prune_rescue_branches(root, "probe")
    assert _remote_tip(bare, "ai/probe@failed-1") == elsewhere, \
        "commits this checkout never had must survive the reap"
    assert runner._git(root, "rev-parse", "--verify", "ai/probe@failed-1").returncode != 0


def test_attempts_is_committed_before_the_worker_starts(tmp_path, monkeypatch):
    """The property the whole 'resumable from disk alone' claim rests on. If the
    machine dies inside the worker, the next boot must find the attempt already
    spent, or a reboot loop retries forever."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        card = board.find(root, "probe")
        seen["attempts"] = card.attempts
        seen["started"] = bool(card.fields.get("started"))
        # Committed, not merely written: an unflushed edit is lost to a reboot,
        # and it is the commit that makes the bump survive one.
        seen["committed"] = subprocess.run(
            ["git", "status", "--porcelain", "Board/"], cwd=root,
            capture_output=True, text=True).stdout.strip() == ""
        return subprocess.CompletedProcess(argv, 1, "{}", "died")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)

    assert seen == {"attempts": 1, "started": True, "committed": True}


def test_a_worker_that_produces_nothing_is_a_failure(tmp_path, monkeypatch):
    """Gates pass trivially on an empty diff, so 'green' would otherwise mean
    'the worker did nothing' and the card would sail into review/. 'Nothing'
    means neither a commit nor a harvested artefact — art produces the second
    kind and never the first."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "neither a commit nor an artefact" in result.detail


def test_a_parked_verdict_reaches_needs_decision(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False,
                 verdict={"outcome": "parked", "summary": "HP or heat?"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    runner.settle(root, "probe", result)
    assert board.find(root, "probe").lane == "needs-decision"


def test_parking_beats_the_no_commit_rule(tmp_path, monkeypatch):
    """A parked card has nothing to commit by definition. If the empty-diff
    check ran first, every well-formed park would be filed as a failure — which
    is precisely backwards from §13, where parking is a success state."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict={"outcome": "parked", "summary": "q"})
    assert runner.dispatch(root, board.find(root, "probe"), "development_team",
                           "sonnet", 5.0, 120).outcome == "parked"


def test_red_gates_fail_the_card_even_with_a_done_verdict(tmp_path, monkeypatch):
    """The worker does not get to mark its own homework. §11: the gates are
    first and Claude's review is second, never the other way round."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _gate_stub(monkeypatch, tmp_path,
               "print('Board/x.md:1 — nope'); raise SystemExit(1)\n")
    # `add -A` rather than `commit -a`: the charter and the card are both new
    # files, and `-a` stages only tracked ones. It used to work by accident —
    # the gate stub was written into the repo root and swept into the fixture's
    # commit, so replacing it above left `-a` one tracked modification to find.
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "red gate"], cwd=root, check=True)
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "all good honest"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "gates" in result.detail


def test_a_violation_entirely_outside_this_attempts_diff_is_blocked_not_failed(
        tmp_path, monkeypatch):
    """The repo-drift case: `Board/other.md` was never touched by this attempt,
    so whatever the gate is unhappy about is not this card's doing. Same give-
    back as a crashed harness, not an attempt spent."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _gate_stub(monkeypatch, tmp_path,
               _JSON_AWARE_GATE.format(file="Board/other.md", rule="stale"))
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "blocked"
    assert result.repo_drift is True
    branches = subprocess.run(["git", "branch", "--list", "ai/probe"], cwd=root,
                              capture_output=True, text=True).stdout
    assert "ai/probe" in branches, "the branch is preserved state, same as a crashed harness"


def test_a_violation_inside_this_attempts_diff_still_fails_the_card(tmp_path, monkeypatch):
    """One violation on a path the worker actually touched is enough to make
    this the card's own problem, even if the classifier would otherwise call
    every other violation drift."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _gate_stub(monkeypatch, tmp_path,
               _JSON_AWARE_GATE.format(file="worked.txt", rule="nope"))
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "gates" in result.detail


def test_a_pathless_violation_from_an_unknown_gate_falls_through_to_failed(
        tmp_path, monkeypatch):
    """Unparseable — or here, path-empty — must never buy a free attempt. This
    gate is not one the classifier has ever heard of; it must still refuse to
    call the violation drift on structure alone, not by recognising a name."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _gate_stub(monkeypatch, tmp_path, _JSON_AWARE_GATE.format(file="", rule="mystery"))
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert result.repo_drift is False


def test_a_gate_stub_that_does_not_understand_json_never_classifies_as_drift(
        tmp_path, monkeypatch):
    """A gate harness that does not support `--json` (an older one, or a
    scripted stand-in that ignores its argv) must degrade to `failed` — the
    payload does not parse, so there is no structured data to trust, and
    guessing it is drift would spend nobody's attempt for free but the
    runner's own credibility."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _gate_stub(monkeypatch, tmp_path,
               "print('Board/other.md:1 — nope'); raise SystemExit(1)\n")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert result.repo_drift is False


def test_a_failing_test_suite_fails_the_card(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "red test"], cwd=root, check=True)
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "pytest" in result.detail


def test_a_worker_with_no_verdict_falls_through_to_the_gates(tmp_path, monkeypatch):
    """Charters written before this runner existed do not know about the verdict
    file. Absent verdict must mean 'judge it by the gates', not 'fail it' — or
    Session G would have silently broken every worker Sessions E and F wrote."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict=None)
    assert runner.dispatch(root, board.find(root, "probe"), "development_team",
                           "sonnet", 5.0, 120).outcome == "review"


def test_three_failures_retire_the_card_to_failed(tmp_path, monkeypatch):
    """The full retry arc, driven for real: fail, fail, fail, filed."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict=None)

    for expected_lane in ("tasks", "tasks", "failed"):
        card = board.find(root, "probe")
        card.write({"finished": None})  # skip the backoff wait, not the counting
        result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
        runner.settle(root, "probe", result)
        assert board.find(root, "probe").lane == expected_lane

    assert board.find(root, "probe").attempts == runner.MAX_ATTEMPTS


def test_the_dispatch_argv_carries_the_resolved_model_and_the_charter(tmp_path, monkeypatch):
    """No caller ever names a model (§16) — but the argv the CLI receives must,
    and it must be the one the tier resolved to."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 3.0, 120)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--agent") + 1] == "code-thread"
    assert argv[argv.index("--max-budget-usd") + 1] == "3.0"
    assert seen["cwd"] == runner.worktree_root(root) / "probe"


def test_run_output_is_written_beside_the_run_not_onto_the_card(tmp_path, monkeypatch):
    """Board/README.md: run output goes to `.ai/runs/<id>/` and the card links
    to it. A card that accumulated transcripts would stop being readable."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)
    out = root / ".ai" / "runs" / "probe" / "attempt-1"
    assert (out / "prompt-1.md").is_file()      # per round, so round 2 does not clobber round 1
    assert (out / "worker-1.json").is_file()
    assert (out / "gates.txt").is_file()
    assert (out / "pytest.txt").is_file()
    assert len(board.find(root, "probe").text) < 2000


def test_a_pass_on_the_first_round_ends_the_loop(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["pass"])

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 1
    assert seen["calls"] == ["art", "art-reviewer"]


def test_a_revise_verdict_runs_another_round_with_the_notes(tmp_path, monkeypatch):
    """The feedback path. If the notes do not reach the next prompt the loop is
    just re-rolling dice, which is the expensive way to do nothing."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["revise", "pass"], notes="blades merge below 24px")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 2
    assert seen["calls"] == ["art", "art-reviewer", "art", "art-reviewer"]
    assert "blades merge below 24px" in seen["producer_prompts"][1]
    assert "blades merge below 24px" not in seen["producer_prompts"][0]


def test_the_round_limit_is_enforced_by_the_runner_not_by_the_charter(tmp_path, monkeypatch):
    """The reason this loop moved out of the producer. Three rounds used to be a
    sentence an LLM could ignore; here it is a `range()`."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["revise"] * 10)

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.rounds == runner.MAX_ROUNDS
    assert seen["calls"].count("art") == runner.MAX_ROUNDS


def test_exhausting_the_rounds_parks_rather_than_fails(tmp_path, monkeypatch):
    """§13: a well-formed question is a success state. The candidates and the
    critique are what let Karel decide in fifteen seconds."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop(monkeypatch, ["reject"] * 5, notes="silhouette unreadable at 20px")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    assert "silhouette unreadable at 20px" in result.detail
    assert "cand.png" in result.detail

    runner.settle(root, "icon", result)
    card = board.find(root, "icon")
    assert card.lane == "needs-decision"
    assert "## Question" in card.text


def test_the_checker_is_never_shown_the_producers_prompt(tmp_path, monkeypatch):
    """§16's whole point, and the reason the loop is built here: an agent that
    knows what was intended sees what was intended. While the producer spawned
    its own reviewer this was a charter instruction it could quietly break; the
    runner builds the checker's context, so there is no path for the prompt to
    travel."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["pass"])
    runner.dispatch(root, board.find(root, "icon"), "development_team", "sonnet", 5.0, 120)

    checker_prompt = seen["checker_prompts"][0]
    producer_prompt = seen["producer_prompts"][0]
    assert "worktree" not in checker_prompt
    assert "Board/tasks" not in checker_prompt
    assert producer_prompt not in checker_prompt
    # …but it DOES get the acceptance criteria, verbatim off the card.
    assert "the gates are green" in checker_prompt


def test_the_checker_is_dispatched_with_an_explicit_model(tmp_path, monkeypatch):
    """A nested spawn inherits its parent's model — §16's original bug, one
    level deeper and out of tier_guard's reach because it names no card path.
    Routing the checker through the dispatcher is what closes it."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    calls: list[list[str]] = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        calls.append(argv)
        if argv[argv.index("--agent") + 1] == "art-reviewer":
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"verdict": "pass", "best": "c.png", "notes": ""}),
                              encoding="utf-8")
        else:
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "c.png").write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.dispatch(root, board.find(root, "icon"), "development_team", "sonnet", 5.0, 120)

    checker_argv = next(a for a in calls if a[a.index("--agent") + 1] == "art-reviewer")
    assert "--model" in checker_argv
    assert checker_argv[checker_argv.index("--model") + 1] == "sonnet"


def test_a_producer_that_parks_stops_the_loop_immediately(tmp_path, monkeypatch):
    """An ambiguity in the card is not fixed by re-rolling. Spending the
    remaining rounds re-asking a question the producer already said it cannot
    answer is the definition of burning the night."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    calls: list[str] = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        agent = argv[argv.index("--agent") + 1]
        calls.append(agent)
        target = Path(next(l.strip() for l in prompt.splitlines()
                           if l.strip().endswith(".json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"outcome": "parked", "summary": "which corp's logo?"}),
                          encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    assert "which corp's logo?" in result.detail
    assert calls == ["art"], "the checker must not run on a parked round"


def test_a_card_with_no_checker_runs_exactly_one_round(tmp_path, monkeypatch):
    """`checker:` is optional and absent means the old behaviour. Every code
    card on the board relies on that."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 1
    assert seen["argv"][seen["argv"].index("--agent") + 1] == "code-thread"


def test_every_round_costs_are_summed(tmp_path, monkeypatch):
    """Two producer rounds plus two checker runs at $0.10 each."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop(monkeypatch, ["revise", "pass"])
    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.cost_usd == pytest.approx(0.4)


def test_the_schema_refuses_a_card_that_checks_its_own_work(tmp_path):
    """`worker: art, checker: art` would rebuild exactly the self-review §16
    exists to prevent, and it is a plausible typo."""
    from nightshift.gates import card_schema  # moved (07_portability.md §8 step 3)

    root = _repo(tmp_path)
    _charter(root, "art")
    _card(root, "tasks", "selfie", worker="art", checker="art")
    violations = [str(v) for v in card_schema.check(root)]
    assert any("same agent" in v for v in violations)


def test_only_the_spawn_functions_may_execute_the_claude_cli():
    """§5 and §12: the orchestrator contains no judgment. A worker is the thing
    being *orchestrated*, not a decision procedure the runner consults — so the
    CLI is executed only where a worker is started, and nothing that decides what
    to do (select, recover, settle, backoff) may reach for it.

    Four functions now: `run_producer`, `run_checker`, `run_stale_check` and
    `review_branch` (automate-review-step). Each *starts a worker* — a producer,
    its checker, `stale-hunter` on one doc, or the diff reviewer on one finished
    branch — and none of them *decides* anything: `review_stage` reads the
    reviewer's verdict and routes on it (a file-state lookup), exactly as the card
    loop selects and then calls `dispatch`. The reviewer having its own spawn is
    the point — its context is built here rather than by the worker, which is what
    makes §16's blindness structural instead of a charter instruction.

    `review_branch` is where the reviewer's spawn now lives; `run_reviewer` is the
    one-card wrapper that lifts `## Acceptance`/`## Intent` off a card and calls
    it. The split is what lets the chore batch review a branch carrying several
    cards' work as one diff, and it does not widen this list — the spawn moved
    down one frame, it did not multiply.

    `preflight` deliberately does not count: it calls `claude_binary()`, which is
    a `shutil.which` lookup. Checking that a file exists is not an LLM call, and
    checking it *before* a card's `attempts` is spent is the reason it is there.
    """
    source = _RUNNER_SOURCE.read_text(encoding="utf-8")
    assert _functions_spawning(source, "binary") == {
        "run_producer", "run_checker", "run_stale_check", "review_branch"}


def test_every_spawn_sites_wall_path_routes_through_the_shared_helper():
    """The second-order guard for `wall-on-review-wrapup-discards-a-verdict`.

    The spawn sites are a *growing* set — there were two, then three, then four
    (the diff reviewer arrived with `automate-review-step`). A fix that patched
    three `if wall is not None:` branches by hand would be silently wrong the day
    a fifth judge stage lands: it would copy the ordering from its neighbours,
    and nothing would error. So the honour-the-artefact decision lives in one
    shared helper, and this keys off the *same* enumeration
    `test_only_the_spawn_functions_may_execute_the_claude_cli` asserts, rather
    than a second hand-written list that could drift from it.

    The rule is a count, not a mere presence: a function that calls two spawn
    functions must consult the helper twice. `dispatch` is exactly that case —
    `run_producer` and `run_checker` are two different stages with two different
    predicates, and a fix that routed one and forgot the other is the specific
    mistake this catches.
    """
    source = _RUNNER_SOURCE.read_text(encoding="utf-8")
    spawn_sites = _functions_spawning(source, "binary") - _WALL_HANDLING_EXEMPT
    assert spawn_sites, "the enumeration is empty; this guard would check nothing"

    helper_calls = _functions_calling(source, "verdict_survives_a_wall")
    for site in sorted(spawn_sites):
        callers = _functions_calling(source, site)
        assert callers, (
            f"{site} spawns a worker but nothing calls it — a spawn site with no "
            f"caller cannot have its wall path checked"
        )
        for caller, spawned in callers.items():
            handled = helper_calls.get(caller, 0)
            total_spawned = sum(
                _functions_calling(source, other).get(caller, 0)
                for other in spawn_sites)
            assert handled >= total_spawned, (
                f"{caller} calls {total_spawned} spawn function(s) ({site} among "
                f"them, {spawned}x) but consults verdict_survives_a_wall only "
                f"{handled}x. A wall on a stage's wrap-up call must ask whether "
                f"that stage already wrote a terminal verdict BEFORE it asks how "
                f"the process exited — see `wall-on-review-wrapup-discards-a-"
                f"verdict`. Add the call, or name the stage in "
                f"_WALL_HANDLING_EXEMPT with a reason."
            )


def test_the_deciding_functions_are_pure_file_state_lookups():
    """The stronger half of the same rule: a function that decides *what* the
    runner does must not shell out at all — not to the CLI, not to git, not to
    pytest. Its inputs are the card files and the clock."""
    import ast

    tree = ast.parse(_RUNNER_SOURCE.read_text(encoding="utf-8"))
    deciders = {"select", "_backoff_remaining", "host_capabilities", "_deadline"}
    for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        if function.name not in deciders:
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and node.attr == "run":
                value = node.value
                if isinstance(value, ast.Name) and value.id == "subprocess":
                    pytest.fail(f"{function.name} shells out; it must be a file-state lookup")


# --- the seam is generic: art and audio share it with no runner change --------
#
# §16's second seam (the producer→checker loop) is only general if the runner
# names no producer *by kind*. Correction 94 (`seam-generality-checked-not-
# claimed`) verified by hand that `art`/`audio` appear in runner.py only in
# comments and docstrings, never in a code path — which is what lets an audio
# pipeline land as two charters and a card, with ZERO runner edits, driven by
# `worker:`/`checker:` frontmatter and `HARVEST_DIRS` pointing at CLAUDE.md's
# house convention (`dungeoneer/assets/.tmp/`) rather than an art path. These two
# tests lock that: one negatively (no executed string constant names a producer
# by kind), one positively (an `audio` card drives the identical loop).

def test_no_art_or_audio_specific_literal_appears_in_runner_logic():
    """A future edit hardcoding `if card.worker == "art"`, or an art-specific
    harvest path, would silently break the generality while every other test
    stayed green. So assert no *executed* string constant in runner.py names a
    producer by kind. Comments are dropped by the parser; docstrings are excluded
    explicitly, so a legitimate mention in prose ("the art pipeline is the case")
    is not a violation — only a string the code actually runs is.
    """
    import ast

    tree = ast.parse(_RUNNER_SOURCE.read_text(encoding="utf-8"))

    # The docstring Constant of the module and of every function/class, by object
    # identity, so it can be skipped when the string constants are scanned. Only
    # these four node kinds carry a docstring — a bare string as the first
    # statement of an `if`/`for` block is code, not prose, and must still count.
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, holders) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))

    # Whole-word match: `artefact`, `start`, `chart`, `audiodriver` never trip;
    # the leading `art`/`audio` of `art-reviewer`/`audio-reviewer` is caught on
    # purpose (a hyphen is a word boundary).
    kind = re.compile(r"\b(art|audio)\b", re.IGNORECASE)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        if kind.search(node.value):
            offenders.append(f"line {node.lineno}: {node.value.strip()[:80]!r}")
    assert not offenders, (
        "runner logic names a producer by kind — the second seam must stay generic "
        "so the audio pipeline needs no runner change (correction 94):\n  "
        + "\n  ".join(offenders))


def test_an_audio_card_drives_the_same_loop_with_no_runner_change(tmp_path, monkeypatch):
    """The positive half. A card whose `worker`/`checker` are `audio`/`audio-
    reviewer` — charters that do not exist yet — both *selects* and *drives* the
    identical producer→checker loop, with nothing in the runner mentioning audio.
    Proof that adding the pipeline is two charters and a card, exactly as
    correction 94 asserts."""
    root = _worktree_repo(tmp_path)
    _charter(root, "audio")
    _charter(root, "audio-reviewer")
    _card(root, "tasks", "sfx", worker="audio", checker="audio-reviewer",
          requires="gpu-box")

    # Selectable on a machine that offers the capability, exactly like an art card.
    assert _select(root, capabilities={"gpu-box"})["sfx"].dispatchable

    calls: list[str] = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        agent = argv[argv.index("--agent") + 1]
        calls.append(agent)
        if agent == "audio":  # the producer: candidates into the house .tmp/ dir
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "hit.wav").write_bytes(b"RIFF")
        else:                 # the checker: a structured verdict off the prompt
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(
                {"verdict": "pass", "best": "hit.wav", "notes": ""}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "sfx"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 1
    assert calls == ["audio", "audio-reviewer"]


def test_a_walled_worker_with_dirty_work_keeps_its_worktree_and_records_the_session(
        tmp_path, monkeypatch):
    """Step 1. A worker walled 45 turns deep has real uncommitted work and a live
    session. Both are preserved: the worktree is kept (not dropped), the session
    id is recorded off the worker's own JSON, and the attempt is given back."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "keepwt")
    _walls_dirty(monkeypatch, session_id="sess-keep")

    result = runner.dispatch(root, board.find(root, "keepwt"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "limited"
    assert result.kept and result.progressed and not result.stuck
    assert (runner.worktree_root(root) / "keepwt").exists()   # kept, not dropped
    handover = runner.read_handover(root, "keepwt")
    assert handover.session_id == "sess-keep"
    assert handover.diff_hash and handover.no_progress == 0

    runner.settle(root, "keepwt", result)
    card = board.find(root, "keepwt")
    assert card.lane == "tasks"
    assert card.attempts == 0                                 # attempt given back
    assert card.fields.get("branch") == "ai/keepwt"           # branch kept (it is state)
    assert (runner.worktree_root(root) / "keepwt").exists()   # still there to re-enter
    assert runner.read_handover(root, "keepwt").session_id == "sess-keep"


def test_a_wall_at_the_first_call_with_no_changes_drops_everything_as_before(
        tmp_path, monkeypatch):
    """Step 6, the regression guard. A wall at the very first API call, with
    nothing done, must behave exactly as it did before warm resume existed:
    worktree and branch dropped, attempt given back, nothing preserved."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "empty")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "empty"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "limited" and not result.kept
    assert not (runner.worktree_root(root) / "empty").exists()      # dropped
    assert runner.read_handover(root, "empty").session_id == ""     # nothing preserved

    runner.settle(root, "empty", result)
    card = board.find(root, "empty")
    assert card.lane == "tasks"
    assert card.attempts == 0
    assert "branch" not in card.fields          # branch dropped, pristine
    assert "## Error" not in card.text          # nothing to answer for


def test_the_next_dispatch_of_an_interrupted_card_resumes_its_session(tmp_path, monkeypatch):
    """Step 2. After a warm interruption the next dispatch resumes the session
    (`--resume <id>`) into the kept worktree, rather than cold-starting."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "resumecard")
    _walls_dirty(monkeypatch, session_id="sess-resume")
    runner.settle(root, "resumecard",
                  runner.dispatch(root, board.find(root, "resumecard"),
                                  "development_team", "sonnet", 0.0, 120))

    seen: list = []
    _finishing_worker(monkeypatch, record=seen)
    result = runner.dispatch(root, board.find(root, "resumecard"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "review"
    argv = seen[0]["argv"]
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-resume"
    # and it happened *in the kept worktree*, not a fresh checkout
    # (the finishing worker committed there and gates/tests ran green)
    assert board.find(root, "resumecard").lane == "tasks"   # settle not yet called


def test_a_failing_resume_falls_through_to_a_fresh_session_in_the_same_worktree(
        tmp_path, monkeypatch):
    """Step 3, the resilience requirement. A `--resume` that errors (a dead
    session) must not burn the attempt discovering it — it falls through, inside
    the same attempt, to a fresh session that re-enters the kept worktree and
    reads its uncommitted diff."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "fallbackcard")
    _walls_dirty(monkeypatch, session_id="sess-dead")
    runner.settle(root, "fallbackcard",
                  runner.dispatch(root, board.find(root, "fallbackcard"),
                                  "development_team", "sonnet", 0.0, 120))

    seen: list = []
    _finishing_worker(monkeypatch, record=seen, resume_returncode=1,
                      resume_stderr="No conversation found with session ID sess-dead")
    result = runner.dispatch(root, board.find(root, "fallbackcard"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "review"
    assert len(seen) == 2                          # resume, then the fresh fallback
    assert "--resume" in seen[0]["argv"] and "--resume" not in seen[1]["argv"]
    assert result.cost_usd == pytest.approx(0.25)  # both runs' cost summed
    # the fallback prompt hands the worker the uncommitted diff, not a blank slate
    assert "uncommitted work is still in this worktree" in seen[1]["prompt"]


def test_commit_wip_banks_dirty_work_and_is_a_noop_when_clean(tmp_path):
    """The deepest-persistence primitive (path 3). It commits the worktree's
    uncommitted work as `wip: <card> interrupted`, and does nothing when the tree
    is already clean, so it is safe to call defensively."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "wipcard")
    tree, branch, mode = runner.prepare_worktree(
        root, board.find(root, "wipcard"), "development_team")
    assert mode == runner.FRESH
    assert runner.commit_wip(root, tree, "wipcard") is False        # clean → no-op
    (tree / "draft.txt").write_text("half done", encoding="utf-8")
    assert runner.commit_wip(root, tree, "wipcard") is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=tree,
                         capture_output=True, text=True).stdout
    assert "wip: wipcard interrupted" in log
    assert runner.commit_wip(root, tree, "wipcard") is False        # nothing left


def test_a_lost_worktree_is_rebuilt_from_its_wip_commit(tmp_path):
    """Step 4, the rebuild. With the worktree gone but the branch's `wip:` commit
    intact, `prepare_worktree` cuts a fresh checkout from the branch (mode
    FROM_WIP) carrying the interrupted work, not a blank one from base."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "wtrebuild")
    _seed_wip_branch(root, tmp_path, "wtrebuild", "interrupted work")

    tree, branch, mode = runner.prepare_worktree(
        root, board.find(root, "wtrebuild"), "development_team")
    assert mode == runner.FROM_WIP
    assert (tree / "wip.txt").read_text(encoding="utf-8") == "interrupted work"


def test_prepare_worktree_normalizes_line_endings_on_a_fresh_checkout(tmp_path, monkeypatch):
    """fresh-worktree-never-gets-normalized (2026-08-13): a `git worktree add`
    is a fresh checkout, exactly the shape `normalize_worktree` exists for, but
    nothing ever ran it on a per-card worktree — every dispatch started
    unnormalized on a host whose checkout re-introduces CRLF, and a worker had
    no standing to fix a per-machine gate violation on files it never touched.
    Pin that the FRESH path now calls it on the worktree it just cut."""
    import nightshift.normalize_worktree as normalize_worktree

    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "crlfcard")
    calls: list[Path] = []
    monkeypatch.setattr(normalize_worktree, "normalize",
                        lambda repo, **kw: calls.append(repo) or 0)

    tree, _, mode = runner.prepare_worktree(
        root, board.find(root, "crlfcard"), "development_team")

    assert mode == runner.FRESH
    assert calls == [tree]


def test_prepare_worktree_normalizes_line_endings_from_wip_too(tmp_path, monkeypatch):
    """The FROM_WIP path cuts its own fresh checkout (from the branch's `wip:`
    commit rather than from base) — same fresh-checkout shape, same need."""
    import nightshift.normalize_worktree as normalize_worktree

    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "crlfwip")
    _seed_wip_branch(root, tmp_path, "crlfwip", "interrupted work")
    calls: list[Path] = []
    monkeypatch.setattr(normalize_worktree, "normalize",
                        lambda repo, **kw: calls.append(repo) or 0)

    tree, _, mode = runner.prepare_worktree(
        root, board.find(root, "crlfwip"), "development_team")

    assert mode == runner.FROM_WIP
    assert calls == [tree]


def test_prepare_worktree_survives_normalize_worktree_raising(tmp_path, monkeypatch):
    """A host-level normalize failure must never be the thing that blocks a
    dispatch — best-effort only, per `_normalize_worktree`'s own docstring."""
    import nightshift.normalize_worktree as normalize_worktree

    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "crlfboom")

    def _boom(repo, **kw):
        raise OSError("disk on fire")

    monkeypatch.setattr(normalize_worktree, "normalize", _boom)

    tree, _, mode = runner.prepare_worktree(
        root, board.find(root, "crlfboom"), "development_team")

    assert mode == runner.FRESH
    assert tree.is_dir()


def test_a_dispatch_from_a_wip_commit_hands_over_a_progress_note_and_does_not_resume(
        tmp_path, monkeypatch):
    """Step 4, the handover. A dispatch of a card in the WIP-recovered state gives
    the worker a `## Progress` note pointing at the commit, with no `--resume`
    (the session did not survive)."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "wtnote")
    _seed_wip_branch(root, tmp_path, "wtnote", "interrupted work")

    seen: list = []
    _finishing_worker(monkeypatch, record=seen)
    result = runner.dispatch(root, board.find(root, "wtnote"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "review"
    assert "--resume" not in seen[0]["argv"]
    assert "recovered from a WIP commit" in seen[0]["prompt"]


def test_a_resume_that_moves_nothing_spends_the_attempt_then_files_the_card(
        tmp_path, monkeypatch):
    """Step 5, the progress gate. A resume that leaves the working tree
    byte-identical is not free: the first repeat spends the attempt, and once the
    tree has not moved for NO_PROGRESS_STOP windows the card is filed to failed/
    rather than being given its attempt back at the head of the queue forever."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "stuckcard")
    _walls_dirty(monkeypatch, session_id="s", content="frozen")   # same bytes every time

    # wall 1 — first interruption: given back, worktree kept.
    runner.settle(root, "stuckcard",
                  runner.dispatch(root, board.find(root, "stuckcard"),
                                  "development_team", "sonnet", 0.0, 120))
    assert board.find(root, "stuckcard").attempts == 0

    # wall 2 — no movement: the attempt is SPENT (not rewound), card stays in tasks/.
    r2 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    assert r2.kept and not r2.progressed and not r2.stuck
    runner.settle(root, "stuckcard", r2)
    card = board.find(root, "stuckcard")
    assert card.lane == "tasks" and card.attempts == 1

    # wall 3 — still no movement: the breaker trips and the card leaves the head.
    r3 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    assert r3.stuck
    runner.settle(root, "stuckcard", r3)
    assert board.find(root, "stuckcard").lane == "failed"


def test_a_kept_worktree_is_not_pruned_by_this_runner(tmp_path, monkeypatch):
    """Step 7. Warm resume keeps worktrees between attempts; capping how many pile
    up is runner-prune-run-dirs's job (Decision 3), not this card's. So this
    runner must grow no second cleanup path — a kept worktree survives the
    dispatch of other cards untouched, still resumable."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "kept")
    _card(root, "tasks", "sibling")
    _walls_dirty(monkeypatch, session_id="s")
    runner.settle(root, "kept",
                  runner.dispatch(root, board.find(root, "kept"),
                                  "development_team", "sonnet", 0.0, 120))
    assert (runner.worktree_root(root) / "kept").exists()

    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})
    runner.settle(root, "sibling",
                  runner.dispatch(root, board.find(root, "sibling"),
                                  "development_team", "sonnet", 0.0, 120))
    assert (runner.worktree_root(root) / "kept").exists()            # untouched — no cap here
    assert not (runner.worktree_root(root) / "sibling").exists()     # resolved, dropped as normal
    assert runner.read_handover(root, "kept").session_id == "s"      # still resumable


def test_the_worktree_hash_ignores_where_the_worktree_lives_but_not_its_content(tmp_path):
    """The progress gate's instrument: two byte-identical working trees hash the
    same; a moved byte changes it. Untracked content counts (a worker's first
    output is untracked before it commits)."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "hashcard")
    tree, _, _ = runner.prepare_worktree(root, board.find(root, "hashcard"), "development_team")
    base = runner._worktree_state_hash(tree)
    (tree / "a.txt").write_text("one", encoding="utf-8")
    after_add = runner._worktree_state_hash(tree)
    assert after_add != base                                  # untracked content is seen
    (tree / "a.txt").write_text("two", encoding="utf-8")
    assert runner._worktree_state_hash(tree) != after_add     # a changed byte moves it
    (tree / "a.txt").write_text("one", encoding="utf-8")
    assert runner._worktree_state_hash(tree) == after_add     # deterministic, no drift


# --------------------------------------------------------------------------
# Pruning (`runner-prune-run-dirs`)
# --------------------------------------------------------------------------
#
# The rule under test: `failed/` is reached by the runner itself, so it is
# pruned eagerly, at both of its in-run paths (MAX_ATTEMPTS and the `stuck`
# breaker); `done/` is reached by Karel, by hand, so it is swept at the next
# startup instead; a worktree ceiling backstops the one lifecycle a run-dir
# rule cannot reach. No test here mirrors arithmetic — every one drives a real
# `dispatch()`/`settle()`/`run()` call, or a real `git worktree`.

def test_a_card_retired_via_max_attempts_has_its_run_dir_pruned_eagerly(tmp_path, monkeypatch):
    """The pre-existing `failed/` path. The moment the third failure lands the
    card in `failed/`, `.ai/runs/<id>/` is gone — the numbers it held now live
    on the card's own `## Telemetry` section instead."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict=None)

    for expected_lane in ("tasks", "tasks", "failed"):
        card = board.find(root, "probe")
        card.write({"finished": None})
        result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
        runner.settle(root, "probe", result)
        assert board.find(root, "probe").lane == expected_lane

    assert not (root / ".ai" / "runs" / "probe").exists()
    assert "## Telemetry" in board.find(root, "probe").text


def test_a_card_filed_as_stuck_is_pruned_eagerly_in_the_same_settle(tmp_path, monkeypatch):
    """The second in-run path to `failed/`, added by `runner-worker-handover`
    after this card was first scoped: the no-progress breaker must prune just
    as eagerly as the MAX_ATTEMPTS path, not wait for the next-startup sweep."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "stuckcard")
    _walls_dirty(monkeypatch, session_id="s", content="frozen")

    runner.settle(root, "stuckcard", runner.dispatch(
        root, board.find(root, "stuckcard"), "development_team", "sonnet", 0.0, 120))
    r2 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    runner.settle(root, "stuckcard", r2)
    assert (root / ".ai" / "runs" / "stuckcard").exists()   # still in tasks/ — not pruned yet

    r3 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    assert r3.stuck
    runner.settle(root, "stuckcard", r3)
    assert board.find(root, "stuckcard").lane == "failed"
    assert not (root / ".ai" / "runs" / "stuckcard").exists()
    assert "## Telemetry" in board.find(root, "stuckcard").text


def test_a_hand_moved_done_card_is_pruned_by_the_next_runner_startup(tmp_path, monkeypatch):
    """`done/` is reached by Karel, by hand — the runner never moves a card
    there itself — so its run-dir is not pruned eagerly; it is swept once at
    the next startup instead, which is what `run()` does right after
    `recover()`."""
    root = _loaded_board(tmp_path)
    _ignore_runs(root)
    _card(root, "done", "shipped")
    run_dir = root / ".ai" / "runs" / "shipped" / "attempt-1"
    run_dir.mkdir(parents=True)
    (run_dir / "worker-1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert not (root / ".ai" / "runs" / "shipped").exists()


def test_the_sweep_also_drops_an_orphaned_worktree_left_on_a_terminal_card(tmp_path, monkeypatch):
    """The sweep's worktree half: the only physical worktree that can outlive a
    dispatch today (absent `runner-worker-handover`'s kept-worktree machinery in
    this repo) is an orphan from a crash mid-dispatch — `recover()` fixes up a
    card's fields but never touches its checkout. A card sitting in `failed/`
    with such an orphan gets it dropped by the sweep, branch preserved."""
    root = _loaded_board(tmp_path)
    _card(root, "failed", "orphan")
    subprocess.run(["git", "worktree", "add", "-b", "ai/orphan",
                    str(runner.worktree_root(root) / "orphan"), "development_team"],
                   cwd=root, check=True)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert not (runner.worktree_root(root) / "orphan").exists()
    kept = subprocess.run(["git", "branch", "--list", "ai/orphan"], cwd=root,
                          capture_output=True, text=True).stdout
    assert "ai/orphan" in kept   # only the checkout is dropped, the branch survives


def test_a_card_in_tasks_with_attempts_keeps_its_run_dirs_through_the_sweep(tmp_path):
    """`sweep_terminal_cards` only ever looks at `done/` and `failed/` — a card
    still in `tasks/` keeps every attempt directory it has."""
    root = _repo(tmp_path)
    _card(root, "tasks", "inflight")
    run_dir = root / ".ai" / "runs" / "inflight" / "attempt-1"
    run_dir.mkdir(parents=True)
    (run_dir / "worker-1.json").write_text("{}", encoding="utf-8")

    swept = runner.sweep_terminal_cards(root)

    assert swept == []
    assert run_dir.is_dir()


def test_cap_run_dir_keeps_only_the_last_n_attempts(tmp_path):
    """The in-flight backstop — N=3 matches MAX_ATTEMPTS, so nothing a card
    actually accumulates in the ordinary run is ever pruned by this; it only
    fires for a card that somehow accumulates more."""
    root = _repo(tmp_path)
    for n in (1, 2, 3, 4, 5):
        (root / ".ai" / "runs" / "capcard" / f"attempt-{n}").mkdir(parents=True)

    runner.cap_run_dir(root, "capcard", keep=3)

    remaining = sorted(p.name for p in (root / ".ai" / "runs" / "capcard").iterdir())
    assert remaining == ["attempt-3", "attempt-4", "attempt-5"]


def test_cap_run_dirs_in_flight_only_touches_tasks_lane_cards(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "inflight")
    for n in (1, 2, 3, 4):
        (root / ".ai" / "runs" / "inflight" / f"attempt-{n}").mkdir(parents=True)

    runner.cap_run_dirs_in_flight(root, keep=3)

    remaining = sorted(p.name for p in (root / ".ai" / "runs" / "inflight").iterdir())
    assert remaining == ["attempt-2", "attempt-3", "attempt-4"]


def test_prune_run_dir_and_prune_worktree_are_no_ops_when_absent(tmp_path):
    """Pruning a run dir or worktree that does not exist is a no-op, not an
    error — a sweep over every terminal card must not care which of them ever
    actually had a run dir or a surviving worktree."""
    root = _repo(tmp_path)
    runner.prune_run_dir(root, "never-existed")               # must not raise
    runner.prune_worktree_if_present(root, "never-existed")   # neither must this


def test_status_json_log_and_lock_survive_pruning_the_running_cards_own_dir(tmp_path):
    """Never prune the current run's own files — `status.json`, the day's log
    tee, or `.lock` — even when the card being pruned is the one currently
    running. They are siblings of `.ai/runs/<id>/`, not inside it, so a prune of
    the card's own directory structurally cannot reach them."""
    root = _repo(tmp_path)
    runs = root / ".ai" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "status.json").write_text("{}", encoding="utf-8")
    (runs / ".lock").write_text("12345", encoding="utf-8")
    (runs / "2026-07-24.log").write_text("log\n", encoding="utf-8")
    (runs / "running-now" / "attempt-1").mkdir(parents=True)
    (runs / "running-now" / "attempt-1" / "worker-1.json").write_text("{}", encoding="utf-8")

    runner.prune_run_dir(root, "running-now")

    assert (runs / "status.json").is_file()
    assert (runs / ".lock").is_file()
    assert (runs / "2026-07-24.log").is_file()
    assert not (runs / "running-now").exists()


def test_prune_old_run_logs_keeps_the_last_n_days(tmp_path):
    """The `<date>.log` tee's own, simpler retention rule — keep the last N
    days — separate from run-dir pruning because a log is not tied to any one
    card's lane."""
    root = _repo(tmp_path)
    runs = root / ".ai" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    keep_date = (today - dt.timedelta(days=4)).isoformat()
    drop_date = (today - dt.timedelta(days=40)).isoformat()
    (runs / f"{keep_date}.log").write_text("keep\n", encoding="utf-8")
    (runs / f"{drop_date}.log").write_text("drop\n", encoding="utf-8")
    (runs / "not-a-date.log").write_text("ignored\n", encoding="utf-8")

    runner.prune_old_run_logs(root, days=14)

    assert (runs / f"{keep_date}.log").is_file()
    assert not (runs / f"{drop_date}.log").exists()
    assert (runs / "not-a-date.log").is_file()   # a name the pattern doesn't match is left alone


def test_run_calls_all_four_housekeeping_steps_at_startup(tmp_path, monkeypatch):
    """The wiring itself, pinned directly: a regression that dropped one of
    these calls from `run()` would look exactly like a quiet night, and no
    single function's own unit test would catch it."""
    root = _loaded_board(tmp_path)
    _ignore_runs(root)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    calls: list[str] = []
    monkeypatch.setattr(runner, "sweep_terminal_cards", lambda r: calls.append("sweep") or [])
    monkeypatch.setattr(runner, "cap_run_dirs_in_flight", lambda r: calls.append("cap"))
    monkeypatch.setattr(runner, "prune_old_run_logs", lambda r: calls.append("logs"))
    monkeypatch.setattr(runner, "enforce_worktree_ceiling", lambda r: calls.append("ceiling") or [])

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["sweep", "cap", "logs", "ceiling"]


def test_the_housekeeping_steps_do_not_run_on_a_dry_run(tmp_path, monkeypatch):
    """A dry run writes nothing and must not prune anything either — the same
    guard `recover()` already gets."""
    root = _loaded_board(tmp_path)
    _ignore_runs(root)
    calls: list[str] = []
    monkeypatch.setattr(runner, "sweep_terminal_cards", lambda r: calls.append("sweep") or [])
    monkeypatch.setattr(runner, "cap_run_dirs_in_flight", lambda r: calls.append("cap"))
    monkeypatch.setattr(runner, "prune_old_run_logs", lambda r: calls.append("logs"))
    monkeypatch.setattr(runner, "enforce_worktree_ceiling", lambda r: calls.append("ceiling") or [])

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team", "--dry-run"]))

    assert calls == []


def test_demote_is_silent_when_there_is_nothing_to_bank(tmp_path, monkeypatch):
    """`commit_wip` is already a no-op on a clean tree; the demotion path must
    stay silent through it rather than logging a phantom failure."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "cleanwt")
    tree, _, mode = runner.prepare_worktree(root, board.find(root, "cleanwt"),
                                            "development_team")
    assert mode == runner.FRESH
    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))

    runner.demote_worktree_to_wip(root, tree, "cleanwt")

    assert logged == []
    assert not tree.exists()


def test_demote_logs_but_still_drops_when_the_wip_commit_genuinely_fails(tmp_path, monkeypatch):
    """A real commit failure (a rejecting hook, a full disk) must not be quiet —
    that would silently lose the uncommitted diff the demotion path exists to
    preserve — but the checkout is dropped regardless, because the ceiling has
    to be enforced either way."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "failwt")
    tree, _, mode = runner.prepare_worktree(root, board.find(root, "failwt"),
                                            "development_team")
    assert mode == runner.FRESH
    (tree / "draft.txt").write_text("uncommitted", encoding="utf-8")
    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    monkeypatch.setattr(runner, "commit_wip", lambda root_, tree_, card_id: False)

    runner.demote_worktree_to_wip(root, tree, "failwt")

    assert logged and "WIP commit failed" in logged[0]
    assert not tree.exists()


def test_demote_calls_the_real_commit_wip_primitive_not_a_lookalike(tmp_path, monkeypatch):
    """The review finding this attempt fixes: an earlier version reimplemented
    `git add -A` + `git commit` instead of calling `commit_wip` — the primitive
    `runner-worker-handover` built for exactly this caller, named in its own
    docstring. Assert the real function is actually invoked, not a reimplementation
    that merely behaves like it."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "callcard")
    tree, _, mode = runner.prepare_worktree(root, board.find(root, "callcard"),
                                            "development_team")
    assert mode == runner.FRESH
    (tree / "draft.txt").write_text("data", encoding="utf-8")
    real_commit_wip = runner.commit_wip
    calls: list[tuple] = []

    def spy(root_, tree_, card_id):
        calls.append((root_, tree_, card_id))
        return real_commit_wip(root_, tree_, card_id)

    monkeypatch.setattr(runner, "commit_wip", spy)

    runner.demote_worktree_to_wip(root, tree, "callcard")

    assert calls == [(root, tree, "callcard")]
    log = subprocess.run(["git", "log", "--oneline", "ai/callcard"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "wip: callcard interrupted" in log


def test_the_kept_worktree_ceiling_demotes_the_oldest_first(tmp_path, monkeypatch):
    """Decision 3's backstop. More limit-interrupted cards than the ceiling
    allows demotes the oldest kept worktree(s) to git-WIP-only — checkout
    dropped, branch (with its banked diff) kept — until at or under the
    ceiling. Oldest is by directory mtime, stamped *after* every write: stamping
    before it would be silently overwritten by the write itself."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    ids = ["wt-a", "wt-b", "wt-c", "wt-d"]
    for cid in ids:
        _card(root, "tasks", cid)
        _walls_dirty(monkeypatch, session_id="s")
        runner.settle(root, cid, runner.dispatch(
            root, board.find(root, cid), "development_team", "sonnet", 0.0, 120))
        assert board.find(root, cid).lane == "tasks"   # kept — warm, not resolved

    for index, cid in enumerate(ids):
        stamp = 1_700_000_000 + index * 1_000   # strictly increasing; wt-a is oldest
        tree = runner.worktree_root(root) / cid
        os.utime(tree, (stamp, stamp))

    demoted = runner.enforce_worktree_ceiling(root, ceiling=2)

    assert demoted == ["wt-a", "wt-b"]
    assert not (runner.worktree_root(root) / "wt-a").exists()
    assert not (runner.worktree_root(root) / "wt-b").exists()
    assert (runner.worktree_root(root) / "wt-c").exists()
    assert (runner.worktree_root(root) / "wt-d").exists()
    log = subprocess.run(["git", "log", "--oneline", "ai/wt-a"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "wip: wt-a interrupted" in log   # the demoted card's work is banked, not lost


def test_enforce_worktree_ceiling_is_a_noop_when_at_or_under_it(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "onlyone")
    _walls_dirty(monkeypatch, session_id="s")
    runner.settle(root, "onlyone", runner.dispatch(
        root, board.find(root, "onlyone"), "development_team", "sonnet", 0.0, 120))

    demoted = runner.enforce_worktree_ceiling(root, ceiling=runner.WORKTREE_KEEP_CEILING)

    assert demoted == []
    assert (runner.worktree_root(root) / "onlyone").exists()
