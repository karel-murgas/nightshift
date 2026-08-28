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


# ------------------------------------------------------------------------------
# The other side of the same coin. `Board/` is exempt from `dirty_outside_board`
# because it is not the maintainer's work in progress — but that exemption was
# written for the in-place topology, where there is exactly one copy of the board.
# Once `ensure_integration_checkout` redirects a run to a second working copy, an
# uncommitted board edit in the launch checkout stops being "somebody looking at
# the board" and becomes a card the run will never see.
#
# 2026-08-28: `drained-vault-graphics` was answered and promoted to `tasks/` in the
# launch checkout on a feature branch, uncommitted. Two chore batches read the
# dedicated checkout, found it still parked in `needs-decision/` with its one
# attempt spent, and reported "nothing to dispatch" — a board disagreeing with the
# one on screen, and nothing in either output saying they were different files.


def _card(root: Path, lane: str, card_id: str) -> None:
    (root / "Board" / lane / f"{card_id}.md").write_text(
        "\n".join(["---", f"id: {card_id}", "---", ""]), encoding="utf-8")


def _work_elsewhere(repo: Path) -> Path:
    """A stand-in for the dedicated checkout. Only its *identity* matters here:
    `stranded_board_refusal` reads the launch checkout and compares the two paths."""
    return repo.parent / ".proj-integration"


def test_an_uncommitted_board_edit_strands_when_the_run_reads_another_checkout(repo):
    _card(repo, "tasks", "answered")
    assert runner.stranded_board_edits(repo, "main") == ["Board/tasks/answered.md"]
    refusal = runner.stranded_board_refusal(repo, _work_elsewhere(repo), "main")
    assert "Board/tasks/answered.md" in refusal
    assert str(repo) in refusal and str(_work_elsewhere(repo)) in refusal


def test_the_same_edit_is_not_stranded_when_the_run_works_in_place(repo):
    """The common topology: one copy of `Board/`, so nothing can be left behind in
    it. Refusing here would break every in-place run for the ordinary act of
    editing a card — exactly what `dirty_outside_board` exempts `Board/` for."""
    _card(repo, "tasks", "answered")
    assert runner.stranded_board_refusal(repo, repo, "main") == ""


def test_a_board_commit_on_the_launch_branch_strands_too(repo):
    """Committing it is not landing it. Board commits belong on the integration
    branch, so one sitting on a feature branch is as invisible as an uncommitted
    edit — and looks considerably more finished."""
    _git(repo, "checkout", "-q", "-b", "feat")
    _card(repo, "tasks", "b")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "board: promote b")
    assert runner.stranded_board_edits(repo, "main") == ["Board/tasks/b.md"]


def test_what_base_moved_on_past_the_launch_branch_is_not_stranded(repo):
    """Three-dot, not two. A launch branch that merely lags `main` has stranded
    nothing, and counting the lag would refuse every run made from a branch cut
    more than one board commit ago — which is every branch by the second day."""
    _git(repo, "checkout", "-q", "-b", "feat")
    _git(repo, "checkout", "-q", "main")
    _card(repo, "tasks", "later")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "board: later")
    _git(repo, "checkout", "-q", "feat")
    assert runner.stranded_board_edits(repo, "main") == []


def test_a_clean_launch_checkout_strands_nothing(repo):
    assert runner.stranded_board_edits(repo, "main") == []
    assert runner.stranded_board_refusal(repo, _work_elsewhere(repo), "main") == ""


def test_the_obsidian_rewrite_at_the_repo_root_does_not_cry_wolf(repo):
    """`Board.base` and the generated views sit at the repo root, not under
    `Board/`. Bases normalises `Board.base` on every vault open, so a check that
    counted it would refuse practically every run — the `looking-at-it-broke-it`
    failure this file already exists for, arriving one directory over."""
    (repo / "Board.base").write_text("views: []\n", encoding="utf-8")
    (repo / "Digest.md").write_text("# Digest\n\nrewritten\n", encoding="utf-8")
    assert runner.stranded_board_edits(repo, "main") == []
