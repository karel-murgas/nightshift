"""Tests for the `fold_fence` PreToolUse hook.

The hook's whole job is to refuse one specific write, so the tests that matter are
the ones that would catch it refusing nothing. That is not hypothetical: the first
implementation normalised its declared paths with `lstrip("./")`, which strips a
*character set* rather than a prefix, turned `.claude/memory/state.md` into
`claude/memory/state.md`, and therefore matched nothing and allowed every write it
exists to refuse — in every project whose memory lives under a dotted directory.
It looked correct and was a silent no-op, which is the failure class this repo's
corrections log names most often.
"""
from __future__ import annotations

from pathlib import Path

from nightshift.hooks import fold_fence

FOLD_MANIFEST = """\
[project]
name = "proj"

[worker]
fence_env = "PROJ_FENCE_ALLOW"

[[memory.fold]]
path = ".claude/memory/state.md"
under = "## Current State"
key = "register"
"""


def _project(tmp_path: Path, manifest: str = FOLD_MANIFEST) -> Path:
    root = tmp_path / "proj"
    (root / ".ai").mkdir(parents=True)
    (root / ".ai" / "manifest.toml").write_text(manifest, encoding="utf-8")
    return root


def _verdict(root: Path, path: str, *, tool: str = "Edit") -> str:
    reason = fold_fence.evaluate({"tool_name": tool, "tool_input": {"file_path": path}},
                                 fold_fence._fold_paths(root))
    return "deny" if reason else "allow"


def test_a_declared_log_is_refused(tmp_path):
    root = _project(tmp_path)
    assert _verdict(root, ".claude/memory/state.md") == "deny"


def test_the_dot_prefix_survives_normalisation(tmp_path):
    """The `lstrip` bug, pinned directly rather than only through `evaluate`: a
    declared path must come back with its leading dot intact."""
    root = _project(tmp_path)
    assert fold_fence._fold_paths(root) == [".claude/memory/state.md"]


def test_the_same_file_inside_a_worktree_is_refused(tmp_path):
    """A dispatched worker edits inside a linked worktree, so the path it sends is
    absolute and rooted somewhere else entirely. Matching on the tail is what makes
    the rule reach the case it was written for."""
    root = _project(tmp_path)
    assert _verdict(
        root, "C:/w/.proj-worktrees/probe/.claude/memory/state.md") == "deny"


def test_backslash_paths_are_refused_too(tmp_path):
    """Windows is the platform this runs on; a native path must not slip through."""
    root = _project(tmp_path)
    assert _verdict(root, r"C:\w\.proj-worktrees\probe\.claude\memory\state.md") == "deny"


def test_an_undeclared_file_is_allowed(tmp_path):
    root = _project(tmp_path)
    assert _verdict(root, "src/thing.py") == "allow"


def test_a_similarly_named_file_is_allowed(tmp_path):
    """The match is a whole path, not a prefix: `state_detail.md` is a different
    file and is nobody's shared log."""
    root = _project(tmp_path)
    assert _verdict(root, ".claude/memory/state_detail.md") == "allow"


def test_the_fragment_a_worker_should_write_is_allowed(tmp_path):
    """The fence has to leave open the thing it tells the worker to do instead."""
    root = _project(tmp_path)
    assert _verdict(root, ".ai/memory-fragments/probe.md") == "allow"


def test_a_project_declaring_no_fold_targets_is_untouched(tmp_path):
    root = _project(tmp_path, '[project]\nname = "proj"\n')
    assert _verdict(root, ".claude/memory/state.md") == "allow"


def test_bash_is_not_inspected(tmp_path):
    """Deliberate: the failure being prevented is the ordinary edit a charter asks
    for, and pattern-matching shell for file writes produces false denials."""
    root = _project(tmp_path)
    reason = fold_fence.evaluate(
        {"tool_name": "Bash", "tool_input": {"command": "echo x >> .claude/memory/state.md"}},
        fold_fence._fold_paths(root))
    assert reason is None


def test_the_denial_names_the_fragment_to_write_instead(tmp_path):
    """A refusal that does not say what to do instead just stalls the worker."""
    root = _project(tmp_path)
    reason = fold_fence.evaluate(
        {"tool_name": "Edit", "tool_input": {"file_path": ".claude/memory/state.md"}},
        fold_fence._fold_paths(root))
    assert ".ai/memory-fragments/<card-id>.md" in reason


def test_armed_only_when_the_dispatch_variable_is_set(tmp_path, monkeypatch):
    """An interactive session curating these files by hand must be untouched — that
    is a real and frequent thing a person does, and it is why this is a hook on the
    dispatch switch rather than a gate on the repo."""
    root = _project(tmp_path)
    monkeypatch.delenv("PROJ_FENCE_ALLOW", raising=False)
    assert not fold_fence._armed(root)
    monkeypatch.setenv("PROJ_FENCE_ALLOW", "C:/w/.proj-worktrees/probe")
    assert fold_fence._armed(root)
