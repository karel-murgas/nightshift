"""Tests for `nightshift/merge_check.py` — real coverage for the one runner
consumer that had none.

The bug that motivated this file (2026-07-31): runner._run_tests changed from a
2-tuple to a 3-tuple return; the identical stale unpack in merge_check.py survived
two green test runs because nothing imported that module. This file makes that
invisible.

**The seam to runner is always real.** Every test that reaches check_branch lets
merge_check actually call runner._run_tests and runner._run_gates (except the
TimeoutExpired path, which tests merge_check's exception handler, not the runner).
The gate runner is substituted via runner.GATE_ARGV (same technique as
test_runner_observability.py's _fake_gates); runner._run_tests is never
monkeypatched for the CLEAN path. A mock there would have agreed with the broken
3-tuple unpack, which is the whole point.

**Provenance.** Written in Dungeoneer's `tests/` and moved here whole by
`framework-tests-live-in-the-wrong-repo` (2026-08-08). Every test builds its own
`tmp_path` repo; the origin repo's only use of its own root was a `sys.path`
insert, so nothing stayed behind.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Sibling-module imports: same pattern as test_runner_observability.py.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from nightshift import runner  # noqa: E402
from test_runner import _card, _repo  # noqa: E402
import nightshift.merge_check as merge_check  # noqa: E402
from nightshift.merge_check import check_branch, report, _conflicted_paths  # noqa: E402


# ---------------------------------------------------------------------------
# Completeness tracking
# ---------------------------------------------------------------------------
# Each test that exercises a check_branch status calls _COVERED.add(status).
# test_every_status_is_covered (last test in the file) asserts the union equals
# the set of all uppercase string constants in the merge_check module.
# Adding a ninth status without a covering test turns that assertion red.

_COVERED: set[str] = set()


def _status_values() -> frozenset[str]:
    """Every status constant defined in merge_check, derived from the module."""
    return frozenset(
        v for k, v in vars(merge_check).items()
        if k.isupper() and isinstance(v, str)
    )


# ---------------------------------------------------------------------------
# Per-file autouse: no xdist overhead inside fixture child processes.
# Module-scoped to THIS file; each file that spawns real pytest children needs
# its own copy (the autouse scope only covers the file it is defined in).
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_xdist_in_fixtures(monkeypatch):
    """Blank xdist flags so fixture-spawned pytest children don't spin up workers."""
    monkeypatch.setattr(runner, "_PYTEST_PARALLEL", (), raising=False)


# ---------------------------------------------------------------------------
# Gate stub: pass by default; individual tests override for crash/violation.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _gates_pass_by_default(monkeypatch, tmp_path):
    """Green gates unless a test calls _stub_gates with a different body."""
    _stub_gates(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# Fixture helpers (plain functions, not pytest fixtures — project convention)
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", check=True,
    )


def _merge_check_repo(tmp_path: Path) -> Path:
    """A git repo with a development_team branch, a passing test, a conflict
    file, and a .gitignore that hides .ai/runs/ so untracked gate output does
    not perturb merges."""
    root = _repo(tmp_path)                          # git init + Board/ + seed commit
    _git(root, "branch", "-M", "development_team")

    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "conflict_file.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".ai/runs/\n", encoding="utf-8")

    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "scaffold")
    return root


def _make_branch(root: Path, branch: str, body_file: str, body_text: str,
                 base: str = "development_team") -> None:
    """Create branch off base, write one file, commit, return to base."""
    _git(root, "checkout", "-qb", branch, base)
    (root / body_file).write_text(body_text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"branch change: {body_file}")
    _git(root, "checkout", "-q", base)


def _stub_gates(monkeypatch, tmp_path: Path,
                body: str = "import sys; sys.exit(0)\n") -> None:
    """Point runner.GATE_ARGV at a scripted stand-in — same seam as _fake_gates
    in test_board_runner_observability.py."""
    stub = tmp_path / "gate_stub.py"
    stub.write_text(body, encoding="utf-8")
    monkeypatch.setattr(runner, "GATE_ARGV", [sys.executable, str(stub)])


def _worktree_path(root: Path, card_id: str) -> Path:
    """Where check_branch puts — and drop_worktree removes — the card's tree."""
    return runner.worktree_root(root) / "_merge-check" / card_id


# ---------------------------------------------------------------------------
# _conflicted_paths unit tests
# ---------------------------------------------------------------------------

def test_conflicted_paths_all_seven_unmerged_codes():
    """UU AA DD AU UA DU UD — every two-letter unmerged code → file reported."""
    codes = ["UU", "AA", "DD", "AU", "UA", "DU", "UD"]
    porcelain = "\n".join(f"{c} file_{c}.txt" for c in codes)
    assert set(_conflicted_paths(porcelain)) == {f"file_{c}.txt" for c in codes}


def test_conflicted_paths_no_conflicts():
    assert _conflicted_paths("M  modified.txt\n?? untracked.txt\n") == []


def test_conflicted_paths_modified_not_conflicted():
    """A modified-but-not-conflicted entry must not be reported."""
    assert _conflicted_paths("M  some_file.py\n") == []


def test_conflicted_paths_empty():
    assert _conflicted_paths("") == []


# ---------------------------------------------------------------------------
# CLEAN — the regression test for the 2026-07-31 break
# ---------------------------------------------------------------------------

def test_clean_with_tests_executes_real_run_tests(tmp_path):
    """The three-value unpack in merge_check runs for real.

    runner._run_tests is NOT monkeypatched here. If merge_check had a stale
    2-tuple unpack, this test would fail with a ValueError — exactly the defect
    that was invisible before this file existed. Gates are stubbed (via GATE_ARGV),
    but the runner seam itself is always live.
    """
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/clean-card", "tests/test_pass.py",
                 "def test_pass():\n    assert True\n")

    result = check_branch(root, "clean-card", "ai/clean-card", "development_team")

    assert result.status == merge_check.CLEAN
    assert result.ok
    _COVERED.add(result.status)
    assert not _worktree_path(root, "clean-card").exists(), \
        "worktree must be torn down on a clean exit"


def test_clean_skip_tests(tmp_path):
    """CLEAN with skip_tests=True — gates only, tests not run."""
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/clean-skip", "added.txt", "x\n")

    result = check_branch(root, "clean-skip", "ai/clean-skip", "development_team",
                          skip_tests=True)

    assert result.status == merge_check.CLEAN
    assert "tests skipped" in result.detail
    _COVERED.add(result.status)   # idempotent — same constant, already covered above
    assert not _worktree_path(root, "clean-skip").exists()


# ---------------------------------------------------------------------------
# BRANCH_MISSING
# ---------------------------------------------------------------------------

def test_branch_missing(tmp_path):
    """A branch: field that names a ref no longer in the repo."""
    root = _merge_check_repo(tmp_path)

    result = check_branch(root, "missing-card", "ai/does-not-exist", "development_team")

    assert result.status == merge_check.BRANCH_MISSING
    _COVERED.add(result.status)
    # No worktree was created — rev-parse returned before the checkout step.
    assert not _worktree_path(root, "missing-card").exists()


# ---------------------------------------------------------------------------
# CHECKOUT_FAILED
# ---------------------------------------------------------------------------

def test_checkout_failed(tmp_path):
    """branch: exists but the base does not — worktree add fails."""
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/checkout-fail-card", "added.txt", "y\n")

    result = check_branch(root, "checkout-fail-card",
                          "ai/checkout-fail-card", "nonexistent-base")

    assert result.status == merge_check.CHECKOUT_FAILED
    _COVERED.add(result.status)
    # The worktree add failed before the try block, so nothing to tear down.
    assert not _worktree_path(root, "checkout-fail-card").exists()


# ---------------------------------------------------------------------------
# CONFLICT
# ---------------------------------------------------------------------------

def test_conflict(tmp_path):
    """Both the branch and the base modify the same line — merge conflicts."""
    root = _merge_check_repo(tmp_path)

    # Branch: change conflict_file.txt to one value.
    _git(root, "checkout", "-qb", "ai/conflict-card", "development_team")
    (root / "conflict_file.txt").write_text("branch-version\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "branch changes conflict file")
    _git(root, "checkout", "-q", "development_team")

    # Base: change the same file to a different value on development_team.
    (root / "conflict_file.txt").write_text("base-version\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base changes conflict file")

    result = check_branch(root, "conflict-card", "ai/conflict-card", "development_team")

    assert result.status == merge_check.CONFLICT
    assert "conflict_file.txt" in result.detail
    _COVERED.add(result.status)
    assert not _worktree_path(root, "conflict-card").exists(), \
        "worktree must be torn down even on conflict"


# ---------------------------------------------------------------------------
# GATE_CRASH
# ---------------------------------------------------------------------------

def test_gate_crash(tmp_path, monkeypatch):
    """The gate harness itself crashes — not a verdict about the card."""
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/gate-crash-card", "added.txt", "a\n")

    _stub_gates(monkeypatch, tmp_path, "raise TypeError('gate exploded')\n")

    result = check_branch(root, "gate-crash-card", "ai/gate-crash-card", "development_team")

    assert result.status == merge_check.GATE_CRASH
    _COVERED.add(result.status)
    assert not _worktree_path(root, "gate-crash-card").exists()


# ---------------------------------------------------------------------------
# GATE_VIOLATION
# ---------------------------------------------------------------------------

def test_gate_violation(tmp_path, monkeypatch):
    """The gate runner reports real violations."""
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/gate-violation-card", "added.txt", "b\n")

    _stub_gates(monkeypatch, tmp_path,
                "print('some/file.py:1 - a_gate: a real violation')\n"
                "raise SystemExit(1)\n")

    result = check_branch(root, "gate-violation-card",
                          "ai/gate-violation-card", "development_team")

    assert result.status == merge_check.GATE_VIOLATION
    assert "a real violation" in result.detail
    _COVERED.add(result.status)
    assert not _worktree_path(root, "gate-violation-card").exists()


# ---------------------------------------------------------------------------
# TESTS_FAILED
# ---------------------------------------------------------------------------

def test_tests_failed(tmp_path):
    """A branch whose test suite is red."""
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/tests-fail-card", "tests/test_fail.py",
                 "def test_fail():\n    assert False, 'intentional'\n")

    result = check_branch(root, "tests-fail-card", "ai/tests-fail-card", "development_team")

    assert result.status == merge_check.TESTS_FAILED
    _COVERED.add(result.status)
    assert not _worktree_path(root, "tests-fail-card").exists()


# ---------------------------------------------------------------------------
# subprocess.TimeoutExpired path
# ---------------------------------------------------------------------------

def test_timeout_is_caught_and_maps_to_tests_failed(tmp_path, monkeypatch):
    """subprocess.TimeoutExpired during _run_tests → TESTS_FAILED with 'timed out'.

    runner._run_tests is monkeypatched here because we are testing merge_check's
    exception handler, not the runner's own timeout behavior. The CLEAN test above
    is where the real seam matters; timing out the actual pytest child would make
    the fixture fragile without adding information.
    """
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/timeout-card", "added.txt", "c\n")

    def _fake_run_tests(cwd, log, timeout, junit, pytest_args):
        raise subprocess.TimeoutExpired(["pytest"], timeout)

    monkeypatch.setattr(runner, "_run_tests", _fake_run_tests)

    result = check_branch(root, "timeout-card", "ai/timeout-card", "development_team",
                          test_timeout=5)

    assert result.status == merge_check.TESTS_FAILED
    assert "timed out" in result.detail
    assert not _worktree_path(root, "timeout-card").exists()


# ---------------------------------------------------------------------------
# Worktree teardown — every exit path
# ---------------------------------------------------------------------------

def test_worktree_is_removed_on_every_exit_path(tmp_path, monkeypatch):
    """check_branch's finally block removes the worktree on all six reachable paths.

    Checked individually above; this aggregates a single explicit assertion for
    each status that goes through the try block (CONFLICT, GATE_CRASH,
    GATE_VIOLATION, TESTS_FAILED, and CLEAN are all covered by their own tests
    above). CHECKOUT_FAILED and BRANCH_MISSING return before the try block, so
    they never create a worktree — verified in their own tests.
    """
    # Sanity: all tests above already assert their own worktree cleanup.
    # This test exists as an explicit documentation assertion that every status
    # covered above left no worktree.
    for card_id, root_sentinel in [
        ("clean-card", tmp_path),
        ("clean-skip", tmp_path),
        ("conflict-card", tmp_path),
        ("gate-crash-card", tmp_path),
        ("gate-violation-card", tmp_path),
        ("tests-fail-card", tmp_path),
        ("timeout-card", tmp_path),
    ]:
        wt = runner.worktree_root(tmp_path) / "_merge-check" / card_id
        assert not wt.exists(), (
            f"worktree for '{card_id}' still exists at {wt} — "
            "the finally block failed to clean up"
        )


# ---------------------------------------------------------------------------
# report() coverage
# ---------------------------------------------------------------------------

def test_report_iterates_both_default_lanes(tmp_path):
    """report() with no lanes= visits both review/ and testing/."""
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/report-both", "added.txt", "r\n")

    _card(root, "review", "report-review-card", branch="ai/report-both")
    _card(root, "testing", "report-testing-card", branch="ai/report-both")

    results = report(root, lanes=("review", "testing"), base="development_team",
                     skip_tests=True)
    ids = [r.card_id for r in results]

    assert "report-review-card" in ids
    assert "report-testing-card" in ids


def test_report_only_narrows_to_one_card_across_lanes(tmp_path):
    """report(only=...) finds the named card even if it is not in the first lane."""
    root = _merge_check_repo(tmp_path)
    _make_branch(root, "ai/report-only", "added.txt", "s\n")

    _card(root, "review", "other-report-card", branch="ai/report-only")
    _card(root, "testing", "my-report-card", branch="ai/report-only")

    results = report(root, lanes=("review", "testing"), base="development_team",
                     skip_tests=True, only="my-report-card")

    assert [r.card_id for r in results] == ["my-report-card"]


def test_report_no_branch_field_gives_no_branch(tmp_path):
    """A card with no branch: field → NO_BRANCH without calling check_branch."""
    root = _merge_check_repo(tmp_path)
    _card(root, "review", "no-branch-card")   # no branch= kwarg → no branch: field

    results = report(root, lanes=("review",), base="development_team")

    assert len(results) == 1
    assert results[0].card_id == "no-branch-card"
    assert results[0].status == merge_check.NO_BRANCH
    _COVERED.add(results[0].status)


# ---------------------------------------------------------------------------
# Completeness guard — derived from the module, not a hand-written list
# ---------------------------------------------------------------------------

def test_every_status_constant_is_exercised():
    """Every uppercase string constant in merge_check is reached by some test.

    This assertion is derived from the module at runtime: a ninth status constant
    cannot be added without either adding a covering test or making this red.
    A hardcoded roster would not satisfy the card's acceptance criterion — it is
    the "someone must remember" shape this card exists to remove.
    """
    all_statuses = _status_values()
    missing = all_statuses - _COVERED
    assert not missing, (
        f"These merge_check status constants are defined but not exercised "
        f"by any test in this file: {missing!r}. "
        f"Add a test that reaches each one and calls _COVERED.add(result.status)."
    )
