"""Tests for `empty-diff-preflight-runs-everything`: a preflight on a branch
whose diff against the base is empty must select `NONE` (no pytest) and pass
the corrections check without inventing a `--no-corrections` reason — instead
of running the entire suite and then refusing to write a receipt.

Real git repos throughout, not mocks — the bug lived in the disagreement
between "no paths at all" and "paths that classified as nothing", which only a
real `git diff`/`git status` against a real merge-base exercises honestly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift import preflight, suite

import _fixtures


@pytest.fixture(autouse=True)
def _no_xdist_in_child_runs(monkeypatch):
    """These tests run a real one-test suite in a temp repo, through preflight.

    `preflight._run_pytest` asks `suite.parallel_args()` for `-n auto --dist
    loadfile`, so without this each child spins up a full set of xdist workers to
    run a single test. Serially that is merely wasteful; inside the parallel suite
    it is six outer workers each spawning six inner ones, and this file's
    contribution measured 54.7 s of that contention against 16.0 s run on its own.

    The same fixture exists in `_runner_helpers.py`, `test_merge_check.py`,
    `test_drain.py` and `test_chores_execution.py` — they patch
    `runner._PYTEST_PARALLEL`, which is the seam their code path reads; this one
    goes through `suite.parallel_args` because preflight's does.
    """
    monkeypatch.setattr(suite, "parallel_args", lambda *a, **k: ())
    _fixtures.serial_child_pytest(monkeypatch)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True,
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip()


def _repo(tmp_path: Path) -> Path:
    """A git repo with an integration branch (`base`) and a feature branch
    checked out on top of it, level with it — the shape a real preflight runs
    against right after a push.

    Ignores its own run artefacts (`.ai/runs/`, `__pycache__/`) from the first
    commit on — a preflight that actually runs pytest here leaves bytecode cache
    behind, and an un-ignored one would show up as an untracked "changed" path on
    the *next* call, corrupting the very fingerprint under test for a reason that
    has nothing to do with the diff. Real projects ignore both (this package's
    own `.gitignore` template does), so a fixture that does not is testing an
    artifact of the fixture, not the mechanism.
    """
    return _fixtures.repo_copy("preflight-base-feature", tmp_path / "proj", _build)


def _build(repo: Path) -> None:
    _fixtures.git_init(repo, branch="base",
                       extra_config=(("commit.gpgsign", "false"),))
    (repo / "file.txt").write_text("one\n", encoding="utf-8", newline="\n")
    (repo / ".gitignore").write_text(".ai/runs/\n__pycache__/\n*.pyc\n",
                                     encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "checkout", "-q", "-b", "feature")


def _commit(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"touch {name}")


# --- _changed_paths: empty vs. cannot-tell are different answers --------------


def test_changed_paths_is_the_empty_set_on_a_branch_level_with_base(tmp_path):
    repo = _repo(tmp_path)
    changed, how, mb = preflight._changed_paths(repo, "base")
    assert changed == set()
    assert "vs base" in how
    assert mb  # a real merge-base resolved; it is just that nothing changed since it


def test_changed_paths_is_none_when_no_merge_base_resolves(tmp_path):
    """A base ref that does not exist at all is "cannot tell what changed" —
    a different fact from "nothing changed", and must not collapse to the same
    empty-set answer any more (an empty set now means something specific)."""
    repo = _repo(tmp_path)
    changed, how, mb = preflight._changed_paths(repo, "no-such-branch")
    assert changed is None
    assert "no merge-base" in how
    assert mb == ""


def test_changed_paths_includes_uncommitted_edits(tmp_path):
    repo = _repo(tmp_path)
    (repo / "untracked.txt").write_text("x\n", encoding="utf-8", newline="\n")
    changed, _, _ = preflight._changed_paths(repo, "base")
    assert changed == {"untracked.txt"}


# --- suite.select agrees with the diff-emptiness distinction ------------------


def test_an_empty_preflight_diff_selects_none(tmp_path):
    repo = _repo(tmp_path)
    changed, _, _ = preflight._changed_paths(repo, "base")
    assert suite.select(changed, repo).bucket == suite.NONE


def test_an_unresolvable_merge_base_is_not_handed_to_suite_select_as_empty(tmp_path):
    """The regression this card's fix could have introduced: feeding `None`
    straight to `suite.select` would crash it, and coalescing it to `set()`
    first would wrongly select NONE instead of the safe ALL. `_run_pytest`
    must build the ALL selection itself for this case rather than delegating."""
    repo = _repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_ok():\n    assert True\n",
                                     encoding="utf-8", newline="\n")
    changed, how, mb = preflight._changed_paths(repo, "no-such-branch")
    assert changed is None  # not fed to suite.select at all in this shape
    ok, detail, bucket = preflight._run_pytest(repo, "no-such-branch", changed, how, mb,
                                               full=False, fresh=True)
    assert bucket == suite.ALL, detail
    assert ok, detail
    assert "cannot tell what changed" in detail or "1 passed" in detail or "test(s)" in detail


# --- the 693-test observation: the reuse cache's own merge-base call ----------
#
# Confirmed cause, not theory (the card's own warning against asserting one
# without running something): `_run_pytest` used to compute its reuse-cache
# merge-base with a second, independent `git merge-base HEAD <base>` call that
# — unlike `_changed_paths` — never redirected through `origin/<base>` when
# `HEAD` *is* `base`. Working directly on the integration branch (routine for
# board-only commits) made that second call self-diff to HEAD's own SHA, a new
# value every commit, so the env tag it fed into every part's fingerprint never
# matched the prior run's and nothing was ever reused.


def test_the_reuse_cache_hits_across_two_board_only_commits_on_the_base_branch(tmp_path):
    """The exact shape of the observation: two commits landed directly on the
    integration branch, differing only in a `Board/`-classified path, with a
    fixed `origin/<base>` behind both. The `system` part's fingerprint must be
    identical across the two runs, because a board-only path folds out of the
    system fingerprint (`_part_fingerprint`) — so it must be reused, not re-run."""
    repo = _repo(tmp_path)
    # Work directly on `base` — the checkout the observation was measured on.
    _git(repo, "checkout", "-q", "base")
    (repo / "myapp").mkdir()
    (repo / ".ai").mkdir()
    (repo / "Board" / "tasks").mkdir(parents=True)
    (repo / ".ai" / "manifest.toml").write_text(
        '[project]\nsource_dirs = ["myapp"]\n\n[branches]\nintegration = "base"\n',
        encoding="utf-8", newline="\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_gate_x.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wire up manifest + a system test")
    # `origin/base` now sits behind everything that follows — the fixed point
    # both commits below diff against, exactly as a pushed-then-worked-on branch.
    origin_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/base", origin_sha)

    (repo / ".ai" / "runner.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "commit 1: a system file")
    changed1, how1, mb1 = preflight._changed_paths(repo, "base")
    ok1, detail1, bucket1 = preflight._run_pytest(repo, "base", changed1, how1, mb1,
                                                   full=False, fresh=False)
    assert ok1, detail1
    assert "reused" not in detail1  # nothing to reuse yet

    (repo / "Board" / "tasks" / "x.md").write_text("- card\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "commit 2: a board-only file")
    changed2, how2, mb2 = preflight._changed_paths(repo, "base")
    assert mb1 == mb2, "the merge-base behind both commits must agree — it is origin/base"
    ok2, detail2, bucket2 = preflight._run_pytest(repo, "base", changed2, how2, mb2,
                                                   full=False, fresh=False)
    assert ok2, detail2
    assert "reused" in detail2, (
        "the system part's fingerprint should have matched commit 1's and been "
        f"reused, not re-run: {detail2}")


# --- the corrections check ------------------------------------------------


def test_corrections_check_passes_on_an_empty_diff_without_no_corrections(tmp_path):
    repo = _repo(tmp_path)
    result = preflight.run_checks(repo, "base", no_corrections=None, skip_tests=True)
    corrections = next(c for c in result.checks if c.name == "corrections")
    assert corrections.ok, corrections.detail
    assert "empty" in corrections.detail


def test_an_empty_diff_preflight_reports_none_not_skipped_and_runs_no_pytest(tmp_path):
    """Acceptance: a preflight on a clean tree level with base reports `NONE`,
    runs no pytest, passes corrections — and the receipt's `tests` field must be
    able to tell "nothing to test" (`none`) apart from "skipped" (`--skip-tests`),
    since only one of the two means the diff was actually looked at."""
    repo = _repo(tmp_path)
    result = preflight.run_checks(repo, "base", no_corrections=None, skip_tests=False)
    corrections = next(c for c in result.checks if c.name == "corrections")
    pytest_check = next(c for c in result.checks if c.name == "pytest")
    assert corrections.ok, corrections.detail
    assert pytest_check.ok, pytest_check.detail
    assert "no tests apply" in pytest_check.detail
    assert result.tests_slice == suite.NONE
    assert result.tests_slice != "skipped"  # distinct from --skip-tests


def test_corrections_check_still_fails_on_a_real_diff_with_no_log_entry(tmp_path):
    """The regression this fix must not cause: a genuine diff with no logged
    correction is still refused."""
    repo = _repo(tmp_path)
    _commit(repo, "feature.txt", "new\n")
    result = preflight.run_checks(repo, "base", no_corrections=None, skip_tests=True)
    corrections = next(c for c in result.checks if c.name == "corrections")
    assert not corrections.ok
    assert "no correction logged" in corrections.detail


def test_corrections_check_passes_when_the_real_diff_touches_the_log(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, ".ai/corrections.log", "2026-08-06 something learned\n")
    result = preflight.run_checks(repo, "base", no_corrections=None, skip_tests=True)
    corrections = next(c for c in result.checks if c.name == "corrections")
    assert corrections.ok
    assert "log touched" in corrections.detail


def test_explicit_no_corrections_still_wins_on_an_empty_diff(tmp_path):
    """`--no-corrections` stays a valid, distinct answer even though an empty
    diff would now pass on its own — the escape hatch is not removed."""
    repo = _repo(tmp_path)
    result = preflight.run_checks(repo, "base", no_corrections="nothing to log",
                                  skip_tests=True)
    corrections = next(c for c in result.checks if c.name == "corrections")
    assert corrections.ok
    assert "explicit zero" in corrections.detail


# --- a suite that hangs must fail, not be waited on -----------------------------


def test_a_hung_pytest_is_killed_and_reported(tmp_path, monkeypatch):
    """Observed twice on 2026-08-16 in a project whose full suite takes 3 minutes:
    an xdist worker died, the replacement never rejoined, and the session parked —
    workers on a `threading.Event`, the controller on `queue.get()`. No CPU, no
    output, no end.

    The runner has always had a timeout here and `merge_check` catches
    `TimeoutExpired`; preflight — the one a person runs interactively, and the one
    gating every push — was the single caller without one. So its failure mode was
    an unbounded silent wait, which is indistinguishable from slowness, and the
    reasonable response to slowness is to keep waiting.
    """
    import subprocess as sp
    from nightshift import preflight

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)

    def hang(argv, **kwargs):
        assert kwargs.get("timeout"), "no timeout passed — a hang would be unbounded"
        raise sp.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(preflight.subprocess, "run", hang)
    monkeypatch.setattr(preflight, "tests_dir", lambda r: r / "tests")

    ok, total, why, _ = preflight._run_subset(root, frozenset({"system"}), "test")

    assert not ok, "a hung suite reported as a pass"
    assert total == 0
    assert "no result" in why and "deadlock" in why, why


def test_the_timeout_is_a_ceiling_not_a_budget(tmp_path):
    """It must sit far above any honest run, or it becomes a flake generator on a
    slow machine. The largest suite measured under this framework is ~13 min."""
    from nightshift import preflight

    assert preflight.DEFAULT_PYTEST_TIMEOUT_S >= 20 * 60
    assert preflight.pytest_timeout(tmp_path) == preflight.DEFAULT_PYTEST_TIMEOUT_S


def test_a_project_may_declare_its_own_ceiling(tmp_path):
    from nightshift import preflight

    root = tmp_path / "repo"
    (root / ".ai").mkdir(parents=True)
    (root / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "x"\n\n[tests]\ndir = "tests"\ntimeout_s = 999\n', encoding="utf-8")

    assert preflight.pytest_timeout(root) == 999
