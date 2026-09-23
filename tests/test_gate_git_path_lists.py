"""Tests for the `git_path_lists` core gate.

Synthetic trees, for the reason every gate test in this suite gives: a test pinned
to a real project's source fails whenever someone legitimately edits it, and a gate
whose test is muted is worse than no gate.

**Narrowed scope.** Since the git-module-and-runner-split workstream, every git
call in the package goes through `nightshift/git.py` — nothing else may spell a
raw git argv at all (`tests/test_git_module_boundary.py` is the broad net that
catches that). This gate now only reads `nightshift/git.py` itself, as a
regression guard on that one file: a future path-listing function added there
without `-z` still gets caught. What the gate is *for* is measured next door, in
`test_git.py` (against a real repository with real awkward filenames, run through
`nightshift.git`). This file only asks whether the gate can tell a path-listing
argv from everything else that mentions git, inside `git.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import git_path_lists  # noqa: E402


def _reader(tmp_path: Path, body: str) -> Path:
    (tmp_path / "nightshift").mkdir(exist_ok=True)
    (tmp_path / "nightshift" / "git.py").write_text(body, encoding="utf-8", newline="")
    return tmp_path


def _reader_with_manifest(tmp_path: Path, body: str) -> Path:
    """Like `_reader`, but with the `[project].source_dirs` declaration real
    checkouts carry — needed only where the gate's own appeal lookup has to find
    `nightshift/git.py` (`appeal_markers.scan` falls back to `.ai/` alone
    without one)."""
    root = _reader(tmp_path, body)
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "t"\nsource_dirs = ["nightshift"]\n',
        encoding="utf-8", newline="")
    return root


def _lines(tmp_path: Path) -> list[int]:
    return sorted(v.line for v in git_path_lists.check(tmp_path))


# --- what the gate must catch ------------------------------------------------

@pytest.mark.parametrize("call", [
    '_git(root, "status", "--porcelain")',
    '_git(root, "diff", "--name-only", f"{base}..HEAD")',
    '_git(tree, "diff", "--name-only", "--diff-filter=U")',
    '_git(root, "diff", "--name-status", f"{base}..HEAD")',
    '_git(root, "ls-files")',
    'subprocess.run(["git", "status", "--porcelain"], cwd=root)',
    'subprocess.run(["git", "-C", str(root), "diff", "--name-only"], check=False)',
    '_git(repo, ["diff", "--cached", "--name-only"])',
])
def test_a_path_listing_without_z_is_flagged(tmp_path, call):
    """Both spellings this package has used: loose arguments to a `_git`-shaped
    helper, and a real argv built as a list. The gate reads the tokens off the
    call, so neither is the privileged one — even inside `git.py`, a new
    function written the old way is still caught."""
    root = _reader(tmp_path,
                   f"def f(root, tree, repo, base, subprocess, _git):\n    {call}\n")
    assert _lines(root) == [2]


# --- what it must not ---------------------------------------------------------

@pytest.mark.parametrize("call", [
    '_git(root, "status", "--porcelain", "-z")',
    '_git(root, "diff", "--name-only", "-z", f"{base}..HEAD")',
    '_git(root, "ls-files", "-z")',
    'subprocess.run(["git", "status", "--porcelain", "-z"], cwd=root)',
])
def test_asking_for_nul_separation_is_the_point(tmp_path, call):
    root = _reader(tmp_path, f"def f(root, base, subprocess, _git):\n    {call}\n")
    assert _lines(root) == []


@pytest.mark.parametrize("call", [
    # A key-value listing, not a path list: `--porcelain` alone is never the trigger.
    '_git(root, "worktree", "list", "--porcelain")',
    # A yes/no question about a path already in hand; only the exit code is read.
    '_git(root, "ls-files", "--error-unmatch", rel)',
    # Nothing to do with paths at all.
    '_git(root, "rev-parse", "--abbrev-ref", "HEAD")',
    '_git(root, "log", "--format=%H", "-1")',
    '_git(root, "push", remote, branch)',
])
def test_a_git_call_that_is_not_a_path_list_is_left_alone(tmp_path, call):
    root = _reader(tmp_path, f"def f(root, rel, remote, branch, _git):\n    {call}\n")
    assert _lines(root) == []


def test_no_git_py_in_the_tree_is_not_a_violation(tmp_path):
    """A repo without `nightshift/git.py` at all (a consuming project's own
    checkout) has nothing for this gate to read — `report.py` and the doctor gate
    already say so elsewhere; this gate just stays quiet."""
    assert git_path_lists.check(tmp_path) == []


def test_an_appeal_is_taken_at_its_line(tmp_path):
    root = _reader_with_manifest(
        tmp_path,
        'def f(root, _git):\n'
        '    # gate-ok(git_path_lists): the output is fed straight back to git\n'
        '    _git(root, "status", "--porcelain")\n')
    assert _lines(root) == []


def test_an_appeal_naming_another_gate_does_not_count(tmp_path):
    root = _reader_with_manifest(
        tmp_path,
        'def f(root, _git):\n'
        '    # gate-ok(subprocess_encoding): wrong gate for this defect\n'
        '    _git(root, "status", "--porcelain")\n')
    assert _lines(root) == [3]
