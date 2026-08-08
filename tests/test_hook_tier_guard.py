"""The `tier_guard` PreToolUse hook, tested against its real stdin/stdout contract.

A hook that is wrong about its payload shape fails silently — it just stops
guarding, and nothing anywhere goes red. So these drive the module as a real
subprocess with a real JSON payload rather than calling `evaluate()` directly.

**Provenance.** Written in Dungeoneer's `tests/test_board_schema.py` and moved
here by `framework-tests-live-in-the-wrong-repo` (2026-08-08). The hook reads
nothing but stdin, so the working directory the subprocess is given is
incidental; it is this repo's root now, and the tests assert exactly what they
did before.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _run_hook(payload: dict) -> dict | None:
    result = subprocess.run(
        [sys.executable, "-m", "nightshift.hooks.tier_guard"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=_REPO,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def _decision(out: dict | None) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def test_card_spawn_without_a_tier_is_denied():
    out = _run_hook({
        "tool_name": "Agent",
        "tool_input": {"prompt": "Execute Board/tasks/hand-razors.md end to end."},
    })
    assert _decision(out) == "deny"
    # The refusal has to *teach*, not cite. It named `00_architecture.md §16` until
    # 2026-08-02; that document ships with this project and with no other, so every
    # consuming repo got a denial pointing at a file it had never had — the
    # `deferral-note-nobody-collected` shape. What must survive is the instruction:
    # read the card's tier, resolve it through the one binding block, pass a model.
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "tier" in reason and "tier-binding" in reason
    assert "model" in reason
    assert "00_architecture" not in reason, (
        "a hook message must not name a document only this project has")


@pytest.mark.parametrize("phrasing", ["at tier: worker", "at the worker tier", "tier=lead"])
def test_card_spawn_stating_a_tier_is_allowed(phrasing):
    out = _run_hook({
        "tool_name": "Agent",
        "tool_input": {"prompt": f"Execute Board/tasks/hand-razors.md {phrasing}."},
    })
    assert _decision(out) == "allow"


def test_an_ordinary_subagent_spawn_is_untouched():
    """A guard that blocked ordinary exploration would be disabled within a week."""
    out = _run_hook({
        "tool_name": "Agent",
        "tool_input": {"prompt": "Find where melee damage is computed.", "subagent_type": "Explore"},
    })
    assert _decision(out) == "allow"


def test_other_tools_are_untouched():
    out = _run_hook({"tool_name": "Bash", "tool_input": {"command": "cat Board/tasks/hand-razors.md"}})
    assert _decision(out) == "allow"


def test_unparseable_payload_never_blocks():
    result = subprocess.run(
        [sys.executable, "-m", "nightshift.hooks.tier_guard"], input="not json",
        capture_output=True, text=True, cwd=_REPO,
    )
    assert result.returncode == 0
    assert not result.stdout.strip()
