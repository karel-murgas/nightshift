#!/usr/bin/env python3
"""PreToolUse hook: a card-execution spawn must resolve its tier explicitly.

00_architecture.md §16, enforcement point 3. Dungeoneer's `adrenaline-pump`
card once ran at the lead tier because the dispatch call passed no `model` and
a subagent silently inherits the parent's. The tier table had existed for a
day and nothing read it. Card frontmatter (`tier:`) and a dispatcher lookup are
conventions; this hook is the part a session cannot forget.

Narrow by construction: it only fires on an `Agent` spawn whose prompt names a
path under `Board/`. Every other subagent call passes untouched -- a guard
that blocked ordinary exploration would be disabled within a week.

Reads the PreToolUse payload on stdin, writes a JSON decision on stdout.
No LLM calls (SS12): this is pure string inspection.

It lives in `hooks/`, not `gates/`: a gate answers "is the tree clean?" and is
discovered by `gates/run.py`; a hook answers "may this call proceed?" and is
driven by the harness with a stdin payload. Same no-LLM rule applies to both.

Wired in a consuming project's `.claude/settings.json`. Runnable by hand for
testing:
    echo '{"tool_name":"Agent","tool_input":{...}}' | python -m nightshift.hooks.tier_guard
"""
from __future__ import annotations

import json
import re
import sys

NAME = "tier_guard"

_CARD_PATH = re.compile(r"\bBoard[/\\]\w[\w-]*[/\\]")
# The dispatcher's contract: whatever spawns a card states the tier it resolved,
# in words, in the prompt. "tier: worker" / "worker tier" / "tier=lead" all pass.
_TIER_STATED = re.compile(r"\btier\s*[:=]\s*(worker|lead)\b|\b(worker|lead)\s+tier\b", re.IGNORECASE)

_DENY = (
    "This spawn names a card under Board/ but does not state a resolved tier.\n"
    "00_architecture.md §16: card execution runs at the tier the card declares, and a "
    "subagent with no explicit model silently inherits the lead's.\n"
    "Read `tier:` from the card's frontmatter, resolve it to a model via §16's table "
    "(that table is the only place the binding lives), pass it as the Agent call's `model`, "
    "and say so in the prompt, e.g. \"Execute this card at tier: worker.\""
)


def evaluate(payload: dict) -> str | None:
    """Return a denial reason, or None to allow."""
    if payload.get("tool_name") != "Agent":
        return None
    tool_input = payload.get("tool_input") or {}
    haystack = " ".join(
        str(tool_input.get(key, "")) for key in ("prompt", "description", "subagent_type")
    )
    if not _CARD_PATH.search(haystack):
        return None
    if _TIER_STATED.search(haystack):
        return None
    return _DENY


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse
    reason = evaluate(payload if isinstance(payload, dict) else {})
    if reason:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
