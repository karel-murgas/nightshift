#!/usr/bin/env python3
"""PreToolUse dispatcher: every fence, one Python process per tool call.

Each fence used to be its own hook entry, so an Edit spawned three interpreters and a
Bash call four, ~0.2 s each before the tool ran. This reads the payload once and asks
each fence's `check(payload)` in turn; every rule still runs, and each keeps its own
module, tests and fail-open behaviour. A fence that raises is skipped, never fatal.
All denials are returned together, so one retry can address all of them.

`preflight_guard` is not here: it is wired with `if` conditions that already spawn it
only for `git push`/`git merge`/`gh pr create`, and it answers `allow` as well as deny.

    echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | python -m nightshift.hooks.pre
"""
from __future__ import annotations

import importlib
import json
import sys

#: `(module, tools it judges)`, in the order their denials are reported.
RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("tier_guard", frozenset({"Agent"})),
    ("worktree_fence", frozenset({"Write", "Edit", "Bash", "NotebookEdit"})),
    ("fold_fence", frozenset({"Write", "Edit", "NotebookEdit"})),
    ("board_branch_fence", frozenset({"Write", "Edit", "NotebookEdit"})),
    ("commit_pathspec", frozenset({"Bash"})),
    ("ideas_fence", frozenset({"Read", "Grep", "Glob", "Bash"})),
    ("tool_economy", frozenset({"Read", "Bash"})),
)

#: The hook modules this dispatcher replaced as separate settings entries —
#: `nightshift.update` strips an entry naming one of them.
SUPERSEDED = frozenset(f"nightshift.hooks.{name}" for name, _ in RULES)


def denials(payload: dict) -> list[str]:
    """Every applicable fence's deny reason, in `RULES` order."""
    tool = payload.get("tool_name")
    found: list[str] = []
    for name, tools in RULES:
        if tool not in tools:
            continue
        try:
            module = importlib.import_module(f"nightshift.hooks.{name}")
            reason = module.check(payload)
        except Exception as exc:  # noqa: BLE001 — one broken fence must not disarm the rest
            print(f"nightshift.hooks.pre: {name} raised {exc!r}; skipped", file=sys.stderr)
            continue
        if reason:
            found.append(reason)
    return found


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse
    if not isinstance(payload, dict):
        return 0
    found = denials(payload)
    if found:
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "\n\n".join(found),
        }}, sys.stdout)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
