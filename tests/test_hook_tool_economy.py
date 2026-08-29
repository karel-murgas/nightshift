"""Tests for the `tool_economy` PreToolUse hook.

Closes the measurement on Dungeoneer's `tile-layer-surface-cache` (2026-08-29):
the worker ran the full suite three times — once correctly parallel, then twice
as a bare serial `python -m pytest -q`, about eleven minutes of wall time for
nothing — and reached for `sed -n '…p' <file>` and `grep -n <pattern> <file>`
where Read and Grep were the instructed tools. Both behaviours already had
prose against them, in `worker_prompt.TOOL_ECONOMY` and in the dispatch prompt
respectively. Prose lost.

The cases below are the **verbatim commands from that attempt's stream**, not
invented ones, on both sides: the three pytest invocations it actually issued
(one of which must stay allowed) and the shell reads it actually typed. A rule
written from a real transcript and tested against a synthetic one can pass while
missing the thing it was written for.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from nightshift.hooks import tool_economy


# --- the measured incident, verbatim --------------------------------------------

#: Every pytest command the 2026-08-29 attempt actually ran. The first is the
#: one the prompt asked for and must survive; the two bare ones are the waste.
_MEASURED_PYTEST_ALLOWED = (
    'cd "C:\\x" && python -m pytest tests/ -n auto --dist loadfile -q 2>&1 | tail -100',
    'cd "C:\\x" && python -m pytest tests/test_tile_render_cache.py -q 2>&1 | tail -20',
    'python -m pytest tests/test_tile_render_cache.py -q 2>&1 | tail -80',
)
_MEASURED_PYTEST_DENIED = (
    'cd "C:\\x" && python -m pytest -q 2>&1 | tail -30',
    'cd "C:\\x" && python -m pytest -q 2>&1 | tail -40',
)


@pytest.mark.parametrize("command", _MEASURED_PYTEST_DENIED)
def test_the_serial_full_suite_runs_that_cost_eleven_minutes_are_denied(command):
    reason = tool_economy._verdict(command)
    assert reason and "-n auto --dist loadfile" in reason


@pytest.mark.parametrize("command", _MEASURED_PYTEST_ALLOWED)
def test_the_same_attempts_legitimate_pytest_runs_are_untouched(command):
    """The parallel full run and the single-file runs. Denying any of these would
    make the rule worse than nothing — a worker cannot check its own work."""
    assert tool_economy._verdict(command) is None


def test_a_single_test_file_may_be_run_serially():
    """Not "always pass -n". xdist's startup costs more than one file's tests,
    so narrowing the run is the *better* behaviour and must not be punished."""
    assert tool_economy._verdict("pytest tests/test_x.py") is None
    assert tool_economy._verdict("pytest tests/test_x.py::test_y") is None


@pytest.mark.parametrize("command,tool", [
    ("sed -n '1345,1355p' .claude/memory/state.md", "Read"),
    ("cat dungeoneer/core/settings.py", "Read"),
    ("head -50 dungeoneer/world/map.py", "Read"),
    ("tail -100 tests/test_x.py", "Read"),
    ("grep -n 'distance' dungeoneer/ai/enemy.py", "Grep"),
    ("grep -rn 'pattern' dungeoneer/", "Grep"),
])
def test_shell_file_reads_are_pointed_at_the_tool_that_does_it_cheaper(command, tool):
    reason = tool_economy._verdict(command)
    assert reason and f"Use the {tool} tool" in reason


@pytest.mark.parametrize("command", [
    "git diff dungeoneer/rendering/tile_renderer.py | head -80",
    "python tools/frame_cost_report.py | tail -20",
    "python -m nightshift.gates.run | tail -30",
    "echo hi && git log --oneline | head -5",
    "ls -t .ai/runs/ | head -20",
])
def test_truncating_a_commands_output_is_not_a_file_read(command):
    """The distinction the rule turns on. `head` after a pipe is keeping a chatty
    command's result small — the behaviour we want more of, not less."""
    assert tool_economy._verdict(command) is None


def test_ordinary_commands_pass():
    for command in ("git status --short", "python main.py", "git commit -m 'x'"):
        assert tool_economy._verdict(command) is None


# --- arming -------------------------------------------------------------------


def _run(payload: dict, env: dict | None = None, cwd=None) -> dict | None:
    """The hook as the harness runs it: JSON on stdin, JSON decision on stdout."""
    proc = subprocess.run(
        [sys.executable, "-m", "nightshift.hooks.tool_economy"],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=cwd,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def test_an_interactive_session_is_untouched(monkeypatch):
    """The fence env var is a dispatched worker's, and only a worker's. These are
    economics for an unattended hundred-turn agent, not style rules for a person
    — a human who wants to `cat` a file is not the problem being solved."""
    monkeypatch.delenv(tool_economy._env_name(), raising=False)
    assert not tool_economy._armed()


def test_it_arms_when_the_worker_fence_var_is_set(monkeypatch):
    monkeypatch.setenv(tool_economy._env_name(), "/some/worktree")
    assert tool_economy._armed()


def test_a_deny_is_a_well_formed_decision_that_names_the_hook(monkeypatch, capsys):
    """The shape the harness reads back: a PreToolUse decision whose reason is
    what reaches the model. It must say which guard spoke, or a worker cannot
    tell an economics rule from a real refusal."""
    monkeypatch.setattr(tool_economy, "_armed", lambda: True)
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "python -m pytest -q"}})))
    assert tool_economy.main() == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith("[tool_economy] ")
    assert "-n auto" in out["permissionDecisionReason"]


def test_only_bash_is_judged():
    """A Read is the thing this hook redirects people *to*."""
    out = _run({"tool_name": "Read", "tool_input": {"file_path": "/x/y.py"}})
    assert out is None


def test_it_fails_open_on_unparseable_input():
    """A guard that wedges a worker on confusion is worse than none."""
    proc = subprocess.run(
        [sys.executable, "-m", "nightshift.hooks.tool_economy"],
        input="not json at all", capture_output=True, text=True)
    assert proc.returncode == 0
    assert not proc.stdout.strip()
