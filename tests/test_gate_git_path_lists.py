"""Tests for the `git_path_lists` core gate.

Synthetic trees, for the reason every gate test in this suite gives: a test pinned
to a real project's source fails whenever someone legitimately edits it, and a gate
whose test is muted is worse than no gate.

What the gate is *for* is measured next door, in `test_gitpaths.py`, against a real
repository with real awkward filenames. This file only asks whether the gate can
tell a path-listing argv from everything else that mentions git.
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


def _tree(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    (tmp_path / ".ai").mkdir(exist_ok=True)
    (tmp_path / ".ai" / name).write_text(body, encoding="utf-8", newline="")
    return tmp_path


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
    """Both spellings this package uses: loose arguments to a `_git` helper, and a
    real argv built as a list. The gate reads the tokens off the call, so neither
    is the privileged one."""
    root = _tree(tmp_path, f"def f(root, tree, repo, base, subprocess, _git):\n    {call}\n")
    assert _lines(root) == [2]


# --- what it must not ---------------------------------------------------------

@pytest.mark.parametrize("call", [
    '_git(root, "status", "--porcelain", "-z")',
    '_git(root, "diff", "--name-only", "-z", f"{base}..HEAD")',
    '_git(root, "ls-files", "-z")',
    'subprocess.run(["git", "status", "--porcelain", "-z"], cwd=root)',
])
def test_asking_for_nul_separation_is_the_point(tmp_path, call):
    root = _tree(tmp_path, f"def f(root, base, subprocess, _git):\n    {call}\n")
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
    root = _tree(tmp_path, f"def f(root, rel, remote, branch, _git):\n    {call}\n")
    assert _lines(root) == []


def test_the_reader_itself_is_where_these_argvs_belong(tmp_path):
    """`gitpaths` is the one module that may spell them, and it spells them with
    `-z` — but it is exempt by path rather than by luck, so the exemption survives
    a future function that needs a listing the module does not yet have."""
    (tmp_path / "nightshift").mkdir()
    (tmp_path / "nightshift" / "gitpaths.py").write_text(
        'def f(root, _git):\n    _git(root, "status", "--porcelain")\n',
        encoding="utf-8", newline="")
    (tmp_path / ".ai").mkdir()
    assert _lines(tmp_path) == []


def test_an_appeal_is_taken_at_its_line(tmp_path):
    root = _tree(
        tmp_path,
        'def f(root, _git):\n'
        '    # gate-ok(git_path_lists): the output is fed straight back to git\n'
        '    _git(root, "status", "--porcelain")\n')
    assert _lines(root) == []


def test_an_appeal_naming_another_gate_does_not_count(tmp_path):
    root = _tree(
        tmp_path,
        'def f(root, _git):\n'
        '    # gate-ok(subprocess_encoding): wrong gate for this defect\n'
        '    _git(root, "status", "--porcelain")\n')
    assert _lines(root) == [3]
