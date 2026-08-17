"""What counts as a dirty tree, and the two things that deliberately do not.

`dirty_outside_board` is the runner's preflight refusal: uncommitted work outside
`Board/` means HEAD is not what the maintainer left, and a worktree branched off it
would carry a half-finished change into a card. Right rule, and it had a hole shaped
like the editor.

**The failure this exists to prevent (2026-08-04, `looking-at-it-broke-it`.)** A real
install committed `.obsidian/workspace.json` — which is what happens when `init`
writes into `.obsidian/` and then closes by telling you `git add -A`. Obsidian
rewrites that file when you open a pane, switch tabs or scroll, so the tree was dirty
again within seconds of every commit and the runner refused to dispatch. Reported
three times in one day by the same project. Opening the board to read it was what
stopped the board being worked.

Two fixes, and both are needed because they cover different repos:
`templates/gitignore` keeps the volatile files out of git at all in a fresh install,
and this exemption rescues a repo that tracked one before that shipped.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift import runner

import _fixtures


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


def _build(r: Path) -> None:
    (r / "Board" / "tasks").mkdir(parents=True)
    (r / ".obsidian").mkdir()
    (r / "pkg").mkdir()
    (r / "pkg" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (r / "Board" / "tasks" / "a.md").write_text("---\nid: a\n---\n", encoding="utf-8")
    (r / "Digest.md").write_text("# Digest\n", encoding="utf-8")
    (r / ".obsidian" / "workspace.json").write_text('{"active": "a"}\n', encoding="utf-8")
    _fixtures.git_init(r, branch="main", autocrlf="false")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "fixture")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _fixtures.repo_copy("board-and-obsidian", tmp_path / "proj", _build)


def test_a_clean_tree_is_clean(repo):
    assert runner.dirty_outside_board(repo) == []


def test_real_uncommitted_work_still_refuses(repo):
    """The rule this is all protecting: a half-finished source change means a
    worktree branched off HEAD is not what anybody intended."""
    (repo / "pkg" / "core.py").write_text("x = 2\n", encoding="utf-8")
    assert runner.dirty_outside_board(repo) == ["pkg/core.py"]


def test_the_board_and_the_digest_are_exempt(repo):
    """Long-standing: the runner commits these itself, and the maintainer edits cards
    while it runs."""
    (repo / "Board" / "tasks" / "a.md").write_text("---\nid: a\nstate: tasks\n---\n",
                                                   encoding="utf-8")
    (repo / "Digest.md").write_text("# Digest\n\nchanged\n", encoding="utf-8")
    assert runner.dirty_outside_board(repo) == []


def test_obsidian_window_state_does_not_block_a_dispatch(repo):
    """The regression. A tracked `workspace.json` changes every time somebody looks at
    the board, and before this it meant the runner could never dispatch again."""
    (repo / ".obsidian" / "workspace.json").write_text('{"active": "b"}\n',
                                                       encoding="utf-8")
    assert runner.dirty_outside_board(repo) == []


def test_obsidian_state_is_skipped_without_hiding_real_work(repo):
    """Skipping a path must not swallow the reason the check exists — a vault rewrite
    and a source edit in the same tree still refuses, and names the source edit."""
    (repo / ".obsidian" / "workspace.json").write_text('{"active": "c"}\n',
                                                       encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("x = 3\n", encoding="utf-8")
    assert runner.dirty_outside_board(repo) == ["pkg/core.py"]


def test_a_new_untracked_file_outside_the_board_still_refuses(repo):
    """`--porcelain` reports untracked files too, and a stray new module is exactly
    the "HEAD is not what you left" case."""
    (repo / "pkg" / "extra.py").write_text("y = 1\n", encoding="utf-8")
    assert runner.dirty_outside_board(repo) == ["pkg/extra.py"]
