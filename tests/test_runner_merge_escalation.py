"""Tests for the narrow-scope merge escalation `rebase_and_merge` reaches for
when a *rebase*-based conflict resolution fails but `base` has only moved in
board/memory bookkeeping since `branch` forked (`_bookkeeping_divergence`,
`_merge_with_resolver`, `_resolve_merge_conflict`).

Written for `stun-animation` (2026-08-27): a card's own board file changed
lanes on `development_team` — dispatched from `Board/tasks/`, finished in
`Board/blocked/` — while its branch still carried a commit touching the old
path. The rebase-based resolver was thrown out for touching files outside the
conflict trying to reconcile it, and the card sat in `blocked/` for a human to
resolve by hand, which is exactly the kind of collision `merge-conflict-has-no-
owner` (2026-08-23) already exists to avoid — this closes the gap it left: a
rebase failure that could not be settled used to go straight to a human with
no second attempt at all.

`_resolve_merge_conflict`'s own exit-code discipline (markers, stray edits,
still-unmerged) is the same code `_resolve_conflict` already exercises in
`test_runner_resolve_conflict.py` — a few guardrail tests are repeated here for
the merge variant's own abort command (`git merge --abort`, not `git rebase
--abort`), not the full matrix. What is genuinely new, and gets the bulk of the
coverage, is the wiring: whether the escalation fires at all (bookkeeping-only
vs. production divergence) and what happens to `development_team` and the
branch when it does.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from nightshift import board, gitmerge, runner

import _fixtures  # noqa: E402
import _runner_helpers  # noqa: E402
from _runner_helpers import (  # noqa: F401  (fixtures register by name)
    _branch_with_file,
    _commit_on_base,
    _gates_pass_by_default,
    _worktree_repo,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _card() -> board.Card:
    return board.Card(path=Path("probe.md"), lane="review",
                      fields={"id": "probe", "title": "a probe card"},
                      text="# probe\n")


# --------------------------------------------------------------------------
# _bookkeeping_divergence
# --------------------------------------------------------------------------

def test_bookkeeping_divergence_is_empty_for_a_board_only_change(tmp_path):
    root = _worktree_repo(tmp_path)  # on development_team
    merge_base = runner._git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "Board").mkdir(exist_ok=True)
    (root / "Board" / "blocked").mkdir(exist_ok=True)
    (root / "Board" / "blocked" / "probe.md").write_text("card\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> blocked")

    production = runner._bookkeeping_divergence(root, "development_team", merge_base)
    assert production == []


def test_bookkeeping_divergence_reports_a_production_path(tmp_path):
    root = _worktree_repo(tmp_path)
    merge_base = runner._git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "dungeoneer").mkdir(exist_ok=True)
    (root / "dungeoneer" / "combat.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "combat: touch something")

    production = runner._bookkeeping_divergence(root, "development_team", merge_base)
    assert production == ["dungeoneer/combat.py"]


def test_bookkeeping_divergence_folds_in_the_memory_fragment_directory(tmp_path):
    """`.ai/memory-fragments/` would otherwise classify as SYSTEM — `.ai/` is where
    the gates and tooling live too — and a per-card fragment is neither."""
    root = _worktree_repo(tmp_path)
    merge_base = runner._git(root, "rev-parse", "HEAD").stdout.strip()
    frag_dir = root / ".ai" / "memory-fragments"
    frag_dir.mkdir(parents=True, exist_ok=True)
    (frag_dir / "probe.md").write_text("## register\n- shipped\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "memory: fold probe")

    production = runner._bookkeeping_divergence(root, "development_team", merge_base)
    assert production == []


# --------------------------------------------------------------------------
# rebase_and_merge's escalation wiring
# --------------------------------------------------------------------------

def test_rebase_and_merge_escalates_a_bookkeeping_only_conflict_to_a_merge(
        tmp_path, monkeypatch):
    """The `stun-animation` shape: the branch's own board card conflicts because
    `development_team` moved it to a new lane while the branch still touches the
    old path. The rebase-based resolver is stubbed to decline (its own guardrails
    are `test_runner_resolve_conflict.py`); since the only thing `development_team`
    gained since the fork is board content, the merge fallback gets a shot, and a
    resolver that accepts `development_team`'s deletion lands the card."""
    root = _worktree_repo(tmp_path)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "tasks" / "probe.md").write_text(
        "line one\nline two\nline three\nline four\nline five\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> tasks")

    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    # `branch` also carries a stale edit to the file `development_team` is about
    # to delete out from under it — the modify/delete shape. Content deliberately
    # dissimilar from what `development_team` replaces it with below: git's rename
    # detection silently merges a modify/delete when the two sides are similar
    # enough, which would hide the very conflict this test needs.
    wt = tmp_path / "seed-branch-edit"
    subprocess.run(["git", "worktree", "add", str(wt), "ai/probe"], cwd=root, check=True)
    (wt / "Board" / "tasks" / "probe.md").write_text(
        "line one CHANGED\nline two CHANGED\nline three CHANGED\nline four CHANGED\n"
        "line five CHANGED\nbranch-only tail\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "ai/probe: edit the stale card"],
                   cwd=wt, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, check=True)

    # development_team moves the card to a new lane — board bookkeeping only.
    (root / "Board" / "tasks" / "probe.md").unlink()
    (root / "Board" / "blocked").mkdir(exist_ok=True)
    (root / "Board" / "blocked" / "probe.md").write_text(
        "totally different content\nnothing shared\nreview findings go here\n"
        "merge notes go here\nunrelated text block\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> blocked")

    monkeypatch.setattr(runner, "_resolve_conflict",
                        lambda *a, **k: (False, "the merge-resolver declined it"))

    def settle_merge(root_, tree, card_, branch, base, out_dir, **kwargs):
        # Accept development_team's deletion — the correct call per
        # merge-resolver's board-file-lane-race rule.
        _git(tree, "rm", "Board/tasks/probe.md")
        return True, "kept development_team's deletion — the card already moved lanes"

    monkeypatch.setattr(runner, "_resolve_merge_conflict", settle_merge)

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")

    assert merged, why
    assert "plain merge" in why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (root / "Board" / "tasks" / "probe.md").exists()
    assert (root / "Board" / "blocked" / "probe.md").exists()
    # A real merge, not a rebase replay — branch's own tip is now an ancestor,
    # so the safe `-d` delete must have succeeded.
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_rebase_and_merge_does_not_escalate_a_production_code_conflict(
        tmp_path, monkeypatch):
    """The narrow-scope guard: when `development_team` moved in real code since the
    fork, not just board/memory bookkeeping, the merge fallback must not even be
    tried — this stays exactly the human-escalation path it was before."""
    root = _worktree_repo(tmp_path)
    monkeypatch.setattr(runner, "_resolve_conflict",
                        lambda *a, **k: (False, "the merge-resolver declined it"))
    escalated = []
    monkeypatch.setattr(runner, "_merge_with_resolver",
                        lambda *a, **k: escalated.append(1) or (True, "should not run"))

    _branch_with_file(root, tmp_path, "ai/probe", "shared.py", "value = 'A'\n")
    _commit_on_base(root, tmp_path, "shared.py", "value = 'B'\n")
    base_before = runner._git(root, "rev-parse", "development_team").stdout.strip()

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")

    assert not merged
    assert "shared.py" in why
    assert not escalated, "the merge fallback must not run for a production-code conflict"
    assert runner._git(root, "rev-parse", "development_team").stdout.strip() == base_before
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode == 0


def test_rebase_and_merge_leaves_development_team_untouched_when_the_merge_also_fails(
        tmp_path, monkeypatch):
    """A bookkeeping-only conflict is not a guarantee — the merge attempt can
    still be declined, and that must land the card in the same human-escalation
    place a rebase-only failure would, with development_team unmoved."""
    root = _worktree_repo(tmp_path)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "tasks" / "probe.md").write_text(
        "line one\nline two\nline three\nline four\nline five\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> tasks")

    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    wt = tmp_path / "seed-branch-edit"
    subprocess.run(["git", "worktree", "add", str(wt), "ai/probe"], cwd=root, check=True)
    (wt / "Board" / "tasks" / "probe.md").write_text(
        "line one CHANGED\nline two CHANGED\nline three CHANGED\nline four CHANGED\n"
        "line five CHANGED\nbranch-only tail\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "ai/probe: edit"], cwd=wt, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, check=True)

    (root / "Board" / "tasks" / "probe.md").unlink()
    (root / "Board" / "blocked").mkdir(exist_ok=True)
    (root / "Board" / "blocked" / "probe.md").write_text(
        "totally different content\nnothing shared\nreview findings go here\n"
        "merge notes go here\nunrelated text block\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> blocked")
    base_before = runner._git(root, "rev-parse", "development_team").stdout.strip()

    monkeypatch.setattr(runner, "_resolve_conflict",
                        lambda *a, **k: (False, "the merge-resolver declined it"))
    monkeypatch.setattr(runner, "_resolve_merge_conflict",
                        lambda *a, **k: (False, "the two sides disagree"))

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")

    assert not merged
    assert "retried as a plain merge" in why
    assert runner._git(root, "rev-parse", "development_team").stdout.strip() == base_before
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode == 0


# --------------------------------------------------------------------------
# aim-crit-display-desync (2026-08-29): the rebase itself can replay and
# re-verify clean, yet the final `merge_branch` step onto `development_team`
# still fails — and until now that failure had no fallback at all, unlike the
# two conflict shapes above. `_bookkeeping_merge_fallback` closes that gap.
# --------------------------------------------------------------------------

def test_rebase_and_merge_escalates_when_merge_branch_itself_fails(
        tmp_path, monkeypatch):
    """The exact shape that hit aim-crit-display-desync: the rebase replays and
    re-verifies cleanly (no conflict at all), but landing it onto
    `development_team` (`merge_branch`) fails on its own — git's "local changes
    would be overwritten" is one real cause, but any `merge_branch` failure
    belongs to this class. Since `development_team` has only moved in board
    bookkeeping since the fork, the plain-merge fallback gets a shot and lands
    the card even though `merge_branch` itself never recovers."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    (root / "Board" / "blocked").mkdir(parents=True, exist_ok=True)
    _commit_on_base(root, tmp_path, "Board/blocked/probe.md", "board bookkeeping only\n")

    monkeypatch.setattr(runner, "merge_branch",
                        lambda *a, **k: (False, "your local changes to the following "
                                        "files would be overwritten by merge"))

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")

    assert merged, why
    assert "plain merge" in why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_rebase_and_merge_does_not_escalate_merge_branch_failure_on_production_divergence(
        tmp_path, monkeypatch):
    """The narrow-scope guard applies here too: `merge_branch` failing is not a
    blank cheque to retry as a plain merge when `development_team` has moved in
    real production code, not just bookkeeping."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    _commit_on_base(root, tmp_path, "shared.py", "value = 'B'\n")
    base_before = runner._git(root, "rev-parse", "development_team").stdout.strip()

    monkeypatch.setattr(runner, "merge_branch",
                        lambda *a, **k: (False, "some merge_branch failure"))
    escalated = []
    monkeypatch.setattr(runner, "_merge_with_resolver",
                        lambda *a, **k: escalated.append(1) or (True, "should not run"))

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")

    assert not merged
    assert "some merge_branch failure" in why
    assert not escalated, "the merge fallback must not run for a production-code divergence"
    assert runner._git(root, "rev-parse", "development_team").stdout.strip() == base_before


def test_rebase_and_merge_escalates_a_rebase_failure_with_no_conflict_markers(
        tmp_path, monkeypatch):
    """The other half of the same gap: a rebase that fails without leaving any
    unmerged path (git's dirty-tree-style refusal, rather than a CONFLICT
    marker) used to be treated as an unconditional dead end — `there is nothing
    for a resolver to resolve` — even when `development_team` had only moved in
    bookkeeping. A real conflicting rebase is built here (mirroring the
    bookkeeping-only-conflict test above) and `_unmerged_paths` is stubbed to
    report none *for the rebase worktree only*, standing in for git's no-marker
    refusal; the merge fallback's own conflict detection is untouched, so
    `_resolve_merge_conflict` still does real work resolving it."""
    root = _worktree_repo(tmp_path)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "tasks" / "probe.md").write_text(
        "line one\nline two\nline three\nline four\nline five\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> tasks")

    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    wt = tmp_path / "seed-branch-edit"
    subprocess.run(["git", "worktree", "add", str(wt), "ai/probe"], cwd=root, check=True)
    (wt / "Board" / "tasks" / "probe.md").write_text(
        "line one CHANGED\nline two CHANGED\nline three CHANGED\nline four CHANGED\n"
        "line five CHANGED\nbranch-only tail\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "ai/probe: edit the stale card"],
                   cwd=wt, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, check=True)

    (root / "Board" / "tasks" / "probe.md").unlink()
    (root / "Board" / "blocked").mkdir(exist_ok=True)
    (root / "Board" / "blocked" / "probe.md").write_text(
        "totally different content\nnothing shared\nreview findings go here\n"
        "merge notes go here\nunrelated text block\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> blocked")

    real_unmerged = runner._unmerged_paths
    rebase_tree = worktree_root_(root) / "_rebase-probe"

    def fake_unmerged(tree):
        if Path(tree).resolve() == rebase_tree.resolve():
            return []
        return real_unmerged(tree)

    monkeypatch.setattr(runner, "_unmerged_paths", fake_unmerged)

    def settle_merge(root_, tree, card_, branch, base, out_dir, **kwargs):
        _git(tree, "rm", "Board/tasks/probe.md")
        return True, "kept development_team's deletion — the card already moved lanes"

    monkeypatch.setattr(runner, "_resolve_merge_conflict", settle_merge)

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")

    assert merged, why
    assert "plain merge" in why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (root / "Board" / "tasks" / "probe.md").exists()
    assert (root / "Board" / "blocked" / "probe.md").exists()


# --------------------------------------------------------------------------
# `_reconcile_dirty_bookkeeping` (aim-crit-display-desync, 2026-08-29): the
# proactive half — fold a stray uncommitted bookkeeping write already sitting
# in `root` into a commit before any merge touches it, since that dirtiness is
# indistinguishable, once a merge refuses over it, from a genuinely broken
# checkout.
# --------------------------------------------------------------------------

def worktree_root_(root: Path) -> Path:
    return runner.worktree_root(root)


def test_reconcile_dirty_bookkeeping_is_a_noop_on_a_clean_tree(tmp_path):
    root = _worktree_repo(tmp_path)
    reconciled, detail = runner._reconcile_dirty_bookkeeping(root)
    assert not reconciled
    assert detail == ""


def test_reconcile_dirty_bookkeeping_folds_a_stray_board_write(tmp_path):
    root = _worktree_repo(tmp_path)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "tasks" / "probe.md").write_text("card\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> tasks")

    # A stray, uncommitted write to the card's own board file — exactly what a
    # runner step that writes-then-forgets-to-commit leaves behind.
    (root / "Board" / "tasks" / "probe.md").write_text(
        "card\nan uncommitted telemetry line\n", encoding="utf-8")

    reconciled, detail = runner._reconcile_dirty_bookkeeping(root)

    assert reconciled, detail
    assert runner._git(root, "status", "--porcelain").stdout.strip() == ""
    assert "an uncommitted telemetry line" in (
        root / "Board" / "tasks" / "probe.md").read_text(encoding="utf-8")


def test_reconcile_dirty_bookkeeping_leaves_production_code_alone(tmp_path):
    """A real, uncommitted production-code change is not this function's to
    touch (§12) — Karel's own in-place checkout could be mid-edit on it."""
    root = _worktree_repo(tmp_path)
    (root / "dungeoneer").mkdir(exist_ok=True)
    (root / "dungeoneer" / "combat.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "combat: add a module")
    (root / "dungeoneer" / "combat.py").write_text("x = 2  # uncommitted\n", encoding="utf-8")

    reconciled, detail = runner._reconcile_dirty_bookkeeping(root)

    assert not reconciled
    assert detail == ""
    assert "uncommitted" in (root / "dungeoneer" / "combat.py").read_text(encoding="utf-8")


def test_rebase_and_merge_folds_roots_own_stray_bookkeeping_write_before_merging(
        tmp_path):
    """End to end: `development_team` carries an uncommitted edit to the card's
    own board file — the runner's own prior write, never committed — when
    `rebase_and_merge` starts. Before this fix, whatever merge attempt reached
    that path would refuse outright with git's dirty-tree message and no
    unmerged paths, reading as an unresolvable conflict. Now it is folded into
    a commit before either merge attempt runs, so the ordinary rebase path
    lands without ever tripping over it."""
    root = _worktree_repo(tmp_path)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "tasks" / "probe.md").write_text("card\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board: probe -> tasks")

    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")

    # A stray, uncommitted write to the same board file the branch never
    # touches — root is dirty, but not on a path the merge itself needs.
    (root / "Board" / "tasks" / "probe.md").write_text(
        "card\nan uncommitted telemetry line\n", encoding="utf-8")

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")

    assert merged, why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"
    assert "an uncommitted telemetry line" in (
        root / "Board" / "tasks" / "probe.md").read_text(encoding="utf-8")
    assert runner._git(root, "status", "--porcelain").stdout.strip() == ""


# --------------------------------------------------------------------------
# _resolve_merge_conflict's own guardrails (the merge-conflict variant of
# `_resolve_conflict` — a subset of `test_runner_resolve_conflict.py`'s matrix,
# just enough to pin the one thing that differs: the abort command).
# --------------------------------------------------------------------------

BASE_LOG = "# Log\n\n- older entry\n"
OURS_LOG = "# Log\n\n- the card's own entry\n- older entry\n"
THEIRS_LOG = "# Log\n\n- a sibling card's entry\n- older entry\n"
BOTH_LOG = "# Log\n\n- the card's own entry\n- a sibling card's entry\n- older entry\n"


def _merge_conflicted(tmp_path: Path) -> tuple[Path, str, str]:
    repo = _fixtures.git_init(tmp_path / "repo", branch="main")
    (repo / "log.md").write_bytes(BASE_LOG.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "log.md").write_bytes(OURS_LOG.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the card")

    _git(repo, "checkout", "-q", "main")
    (repo / "log.md").write_bytes(THEIRS_LOG.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the sibling")
    return repo, "feature", "main"


def _pause_merge(repo: Path) -> None:
    out = _git(repo, "merge", *gitmerge.STRATEGY_ARGS, "--no-ff", "--no-commit", "feature")
    assert out.returncode != 0, "fixture must actually conflict"
    assert runner._unmerged_paths(repo), "fixture must leave an unmerged path"


def _verdict_path(prompt: str) -> Path:
    found = re.search(r"`([^`]+\.json)`", prompt)
    assert found, "the prompt must name a verdict path"
    return Path(found.group(1))


def _resolver(monkeypatch, *, write: str | None, verdict: dict | None,
              stage: bool = True, also_touch: str | None = None):
    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        tree = Path(cwd)
        if write is not None:
            (tree / "log.md").write_bytes(write.encode("utf-8"))
            if stage:
                _git(tree, "add", "log.md")
        if also_touch is not None:
            (tree / also_touch).write_bytes(b"a stray edit\n")
            _git(tree, "add", also_touch)
        if verdict is not None:
            _verdict_path(prompt).write_text(json.dumps(verdict), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "host_setting", lambda root, key, default=None: default)


def test_resolve_merge_conflict_a_kept_both_resolution_lands(tmp_path, monkeypatch):
    repo, branch, base = _merge_conflicted(tmp_path)
    _pause_merge(repo)
    _resolver(monkeypatch, write=BOTH_LOG,
              verdict={"resolved": True, "summary": "kept both entries"})

    resolved, detail = runner._resolve_merge_conflict(
        repo, repo, _card(), branch, base, tmp_path / "out", model="test-model", timeout=60)

    assert resolved, detail
    assert "kept both entries" in detail
    assert (repo / "log.md").read_text(encoding="utf-8") == BOTH_LOG
    assert not runner._unmerged_paths(repo)


def test_resolve_merge_conflict_a_declined_resolution_aborts_the_merge(tmp_path, monkeypatch):
    repo, branch, base = _merge_conflicted(tmp_path)
    _pause_merge(repo)
    _resolver(monkeypatch, write=None,
              verdict={"resolved": False, "summary": "both sides disagree"})

    resolved, detail = runner._resolve_merge_conflict(
        repo, repo, _card(), branch, base, tmp_path / "out", model="test-model", timeout=60)

    assert not resolved
    assert "declined" in detail
    assert "both sides disagree" in detail
    assert not runner._unmerged_paths(repo)
    assert not (repo / ".git" / "MERGE_HEAD").exists(), "the merge must be aborted, not left paused"


def test_resolve_merge_conflict_a_stray_edit_aborts_the_merge(tmp_path, monkeypatch):
    repo, branch, base = _merge_conflicted(tmp_path)
    _pause_merge(repo)
    _resolver(monkeypatch, write=BOTH_LOG, also_touch="unrelated.md",
              verdict={"resolved": True, "summary": "kept both, tidied a neighbour"})

    resolved, detail = runner._resolve_merge_conflict(
        repo, repo, _card(), branch, base, tmp_path / "out", model="test-model", timeout=60)

    assert not resolved
    assert "unrelated.md" in detail
    assert not (repo / ".git" / "MERGE_HEAD").exists()
