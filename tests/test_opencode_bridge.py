"""The OpenCode -> nightshift hook bridge (04_local_runtime.md §8).

The bridge holds no rules. What these test is the translation, because that is where it
can fail silently: a tool name or argument name that does not map leaves the hook seeing
no path at all, and a hook with nothing to look at reports "nothing to deny" — which is
indistinguishable from a fence that ran and approved. That is `silent-noop`, the class
this repo has 26 recorded instances of.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from nightshift import opencode_bridge


class TestTranslation:
    def test_maps_tool_and_argument_names(self):
        got = opencode_bridge.to_claude_payload(
            {"tool": "read", "args": {"filePath": "a.py"}})
        assert got == {"tool_name": "Read", "tool_input": {"file_path": "a.py"}}

    def test_maps_every_tool_a_fence_watches(self):
        """ideas_fence watches Read/Grep/Glob/Bash; worktree_fence watches writes.
        A tool missing from the map is a fence that cannot see that call at all."""
        watched = {"Read", "Grep", "Glob", "Bash", "Write", "Edit"}
        assert watched <= set(opencode_bridge.TOOL_NAMES.values())

    def test_patch_counts_as_an_edit(self):
        """OpenCode has a `patch` tool with no Claude Code equivalent; it writes, so
        the write fence must see it."""
        got = opencode_bridge.to_claude_payload({"tool": "patch", "args": {}})
        assert got["tool_name"] == "Edit"

    def test_unknown_tool_is_none_not_allow(self):
        """None means 'no fence has an opinion', which the caller must not conflate
        with 'a fence looked and approved'."""
        assert opencode_bridge.to_claude_payload({"tool": "todowrite", "args": {}}) is None

    def test_tool_name_is_case_insensitive(self):
        assert opencode_bridge.to_claude_payload({"tool": "READ", "args": {}})["tool_name"] == "Read"

    def test_unmapped_arguments_are_passed_through_unchanged(self):
        got = opencode_bridge.to_claude_payload(
            {"tool": "read", "args": {"filePath": "a.py", "limit": 40}})
        assert got["tool_input"]["limit"] == 40

    def test_non_dict_args_do_not_raise(self):
        got = opencode_bridge.to_claude_payload({"tool": "read", "args": "nonsense"})
        assert got == {"tool_name": "Read", "tool_input": {}}


class TestDecide:
    def test_unwatched_tool_is_allowed(self):
        assert opencode_bridge.decide({"tool": "todowrite", "args": {}}) == ("allow", "")

    def test_a_raising_hook_allows_rather_than_blocks(self, monkeypatch, capsys):
        """A fence that crashes must not become a fence that stops all work. The
        traceback goes to stderr so the run log keeps it."""
        def boom(payload):
            raise RuntimeError("hook is broken")
            yield  # pragma: no cover - generator marker

        monkeypatch.setattr(opencode_bridge, "_hook_verdicts",
                            lambda payload: iter([]) if False else boom(payload))
        status, reason = opencode_bridge.decide({"tool": "read", "args": {"filePath": "x"}})
        assert status == "allow" and reason == ""

    def test_first_denial_wins(self, monkeypatch):
        monkeypatch.setattr(opencode_bridge, "_hook_verdicts",
                            lambda payload: iter([None, "second fence says no", "third"]))
        assert opencode_bridge.decide(
            {"tool": "read", "args": {"filePath": "x"}}) == ("deny", "second fence says no")


class TestProcessContract:
    """The JS half speaks to this over stdin/stdout, so the wire shape is a contract."""

    def _run(self, payload: str) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "nightshift.opencode_bridge"],
            input=payload, capture_output=True, text=True, encoding="utf-8",
        )
        return json.loads(proc.stdout)

    def test_answers_with_status_and_reason(self):
        got = self._run(json.dumps({"tool": "todowrite", "args": {}}))
        assert got == {"status": "allow", "reason": ""}

    def test_unparseable_input_allows_rather_than_hangs(self):
        """A fence that fails closed on a quoting bug stops the night."""
        assert self._run("{not json")["status"] == "allow"

    def test_non_object_input_allows(self):
        assert self._run("[1, 2, 3]")["status"] == "allow"
