"""Tests for the `board_branch_fence` PreToolUse hook.

`.ai/corrections.log`, `edited-the-board-on-a-branch-whose-skill-lacked-the-rule`
(2026-08-29): a `Board/` edit landed on a feature branch the runner never reads,
because nothing checked `git branch --show-current` before the write happened. This
suite builds a real git repository so the thing under test is the actual git call,
not a stub of it — the same reasoning `test_hook_preflight_guard.py` gives for why
a mocked git would have missed the bug that guard was fixing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nightshift.hooks import board_branch_fence

import _fixtures

MANIFEST = """\
[project]
name = "proj"

[branches]
integration = "test"
stable = "main"
"""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True,
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip()


def _repo(tmp_path: Path, name: str = "proj", *, manifest: str | None = MANIFEST) -> Path:
    repo = tmp_path / name
    (repo / "Board" / "tasks").mkdir(parents=True)
    (repo / "Board" / "tasks" / "existing.md").write_text("x\n", encoding="utf-8")
    if manifest is not None:
        (repo / ".ai").mkdir(exist_ok=True)
        (repo / ".ai" / "manifest.toml").write_text(manifest, encoding="utf-8")
    _fixtures.git_init(repo, branch="test",
                       extra_config=(("commit.gpgsign", "false"),))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


def _verdict(path: str, *, tool: str = "Edit") -> str | None:
    return board_branch_fence.evaluate({"tool_name": tool, "tool_input": {"file_path": path}})


def test_a_write_on_the_integration_branch_is_allowed(tmp_path):
    repo = _repo(tmp_path)
    target = repo / "Board" / "tasks" / "existing.md"
    assert _verdict(str(target)) is None


def test_a_write_on_a_feature_branch_is_denied(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    target = repo / "Board" / "tasks" / "existing.md"
    reason = _verdict(str(target))
    assert reason is not None
    assert "ai/some-card" in reason
    assert "`test`" in reason


def test_a_new_file_that_does_not_exist_yet_is_still_checked(tmp_path):
    """`Write` names a card that has not been created yet — the common case for a
    brand-new note — and the hook must not need the file to exist to see it."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    target = repo / "Board" / "tasks" / "brand-new-card.md"
    reason = _verdict(str(target), tool="Write")
    assert reason is not None


def test_a_write_outside_board_is_allowed(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    (repo / "src").mkdir()
    target = repo / "src" / "thing.py"
    target.write_text("x\n", encoding="utf-8")
    assert _verdict(str(target)) is None


def test_a_similarly_named_top_level_directory_is_not_matched(tmp_path):
    """A first-path-segment check, not a prefix string match: `Boardwalk/` is not
    `Board/`."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    (repo / "Boardwalk").mkdir()
    target = repo / "Boardwalk" / "thing.md"
    target.write_text("x\n", encoding="utf-8")
    assert _verdict(str(target)) is None


def test_a_project_with_no_manifest_is_untouched(tmp_path):
    repo = _repo(tmp_path, manifest=None)
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    target = repo / "Board" / "tasks" / "existing.md"
    assert _verdict(str(target)) is None


def test_a_project_with_no_declared_integration_branch_is_untouched(tmp_path):
    repo = _repo(tmp_path, manifest='[project]\nname = "proj"\n')
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    target = repo / "Board" / "tasks" / "existing.md"
    assert _verdict(str(target)) is None


def test_a_non_git_directory_is_allowed(tmp_path):
    (tmp_path / "Board" / "tasks").mkdir(parents=True)
    target = tmp_path / "Board" / "tasks" / "x.md"
    target.write_text("x\n", encoding="utf-8")
    assert _verdict(str(target)) is None


def test_bash_is_not_inspected(tmp_path):
    """Bash is deliberately not pattern-matched, the same call `fold_fence` makes:
    the failure being prevented is the ordinary edit tool a session reaches for."""
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    target = repo / "Board" / "tasks" / "existing.md"
    reason = board_branch_fence.evaluate(
        {"tool_name": "Bash", "tool_input": {"command": f"echo x >> {target}"}})
    assert reason is None


def test_the_denial_names_how_to_proceed(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai/some-card")
    target = repo / "Board" / "tasks" / "existing.md"
    reason = _verdict(str(target))
    assert "git checkout test" in reason
