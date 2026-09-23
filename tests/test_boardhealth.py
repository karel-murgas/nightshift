"""`nightshift.boardhealth` — cards stuck between two lanes, found where someone looks.

Carries the two checks that were tree gates (`tasks_branch_already_merged`,
`review_verdict_unread`): a hand-made merge whose lane move never happened, and a
reviewer verdict nothing acted on.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nightshift import boardhealth, panel, runner
from nightshift import hostconfig

import _runner_helpers  # noqa: F401
from _runner_helpers import _card, _worktree_repo

BASE = "development_team"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _commit_board(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board")


def _branch(root: Path, name: str) -> None:
    _git(root, "branch", name, BASE)


def test_a_clean_board_has_no_findings(tmp_path):
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "probe", kind="inline")
    _commit_board(root)
    assert boardhealth.check(root) == []


def test_an_inline_card_whose_branch_merged_by_hand_is_reported(tmp_path):
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "probe", kind="inline")
    _commit_board(root)
    _branch(root, "ai/probe")          # an ancestor of the base: merged, never moved
    found = boardhealth.check(root)
    assert [(f.lane, f.card_id) for f in found] == [("tasks", "probe")]
    assert "boardcmd land probe" in found[0].text


def test_a_dispatched_card_on_a_never_diverged_branch_is_not_reported(tmp_path):
    """A retry can cut a branch that never diverged; the runner owns that card."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "probe")
    _commit_board(root)
    _branch(root, "ai/probe")
    assert boardhealth.check(root) == []


def _verdict(root: Path, card_id: str, attempt: int, verdict: str) -> None:
    out = root / ".ai" / "runs" / card_id / f"attempt-{attempt}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "review-verdict.json").write_text(json.dumps({"verdict": verdict}),
                                             encoding="utf-8")


def test_a_review_card_with_a_decisive_verdict_is_reported(tmp_path):
    root = _worktree_repo(tmp_path)
    _card(root, "review", "probe")
    _verdict(root, "probe", 1, "ok")
    found = boardhealth.check(root)
    assert [(f.lane, f.card_id) for f in found] == [("review", "probe")]


def test_only_the_latest_attempts_verdict_counts(tmp_path):
    root = _worktree_repo(tmp_path)
    _card(root, "review", "probe")
    _verdict(root, "probe", 1, "needs_fix")
    (root / ".ai" / "runs" / "probe" / "attempt-2").mkdir(parents=True)
    assert boardhealth.check(root) == []


def test_an_unmerged_batch_verdict_is_reported_and_a_landed_one_is_not(tmp_path):
    root = _worktree_repo(tmp_path)
    _card(root, "review", "probe")
    _commit_board(root)
    batch = root / ".ai" / "runs" / "_chores" / "20260923-0100"
    batch.mkdir(parents=True)
    (batch / "review-verdict.json").write_text(
        json.dumps({"items": [{"id": "probe", "verdict": "ok"}]}), encoding="utf-8")
    tree = tmp_path / "batch"
    _git(root, "worktree", "add", "-q", "-b", "chores/20260923-0100", str(tree), BASE)
    (tree / "x.py").write_text("x = 1\n", encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "batch")
    _git(root, "worktree", "remove", "--force", str(tree))
    assert [f.card_id for f in boardhealth.check(root)] == ["probe"]

    _git(root, "branch", "-D", "chores/20260923-0100")    # landed and pruned
    assert boardhealth.check(root) == []


def test_the_runner_logs_findings_at_startup(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "probe", kind="inline")
    _commit_board(root)
    _branch(root, "ai/probe")
    logged: list[str] = []
    monkeypatch.setattr(hostconfig, "_log", logged.append)
    runner._startup_housekeeping(root, root)
    assert any(line.startswith("board health — tasks/probe") for line in logged), logged


def test_the_panel_lists_findings_on_the_system_page(tmp_path):
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "probe", kind="inline")
    _commit_board(root)
    _branch(root, "ai/probe")
    html = panel._system_board_health(panel.read_context(root))
    assert "Board health" in html and "tasks/probe" in html.replace("</b>", "")
