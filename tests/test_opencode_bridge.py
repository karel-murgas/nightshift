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


class TestToolEconomy:
    """`tool_economy` slots into `_hook_verdicts` as two more pure-function checks
    (see `opencode_bridge.py`'s module docstring) — these prove the translation
    reaches it, not the rule itself (that is `test_hook_tool_economy.py`'s job)."""

    def test_denies_unbounded_read_of_a_big_file(self, tmp_path):
        from nightshift.hooks import tool_economy

        big = tmp_path / "big.py"
        big.write_text("x = 1\n" * (tool_economy.BIG_FILE_BYTES // 6 + 10), encoding="utf-8")
        status, reason = opencode_bridge.decide(
            {"tool": "read", "args": {"filePath": str(big)}})
        assert status == "deny" and "offset" in reason

    def test_denies_whole_suite_pytest_when_armed(self, monkeypatch):
        from nightshift.hooks import tool_economy

        monkeypatch.setenv(tool_economy._env_name(), "/some/worktree")
        status, reason = opencode_bridge.decide(
            {"tool": "bash", "args": {"command": "python -m pytest -q"}})
        assert status == "deny" and "nightshift.suite slice" in reason

    def test_allows_the_same_bash_call_when_unarmed(self, monkeypatch):
        from nightshift.hooks import tool_economy

        monkeypatch.delenv(tool_economy._env_name(), raising=False)
        assert opencode_bridge.decide(
            {"tool": "bash", "args": {"command": "python -m pytest -q"}}) == ("allow", "")


class TestAfterReport:
    """`after_report()` is the second shape: a report, not a verdict."""

    def test_returns_exactly_what_gates_on_edit_run_produces(self, monkeypatch, tmp_path):
        from nightshift.hooks import gates_on_edit

        calls = []

        def fake_run(root, session_id):
            calls.append((root, session_id))
            return "gates: ok"

        monkeypatch.setattr(gates_on_edit, "run", fake_run)
        monkeypatch.setattr("nightshift.manifest.find_root", lambda start=None: tmp_path)
        got = opencode_bridge.after_report({"cwd": str(tmp_path), "sessionID": "s1"})
        assert got == "gates: ok"
        assert calls == [(tmp_path.resolve(), "s1")]


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
