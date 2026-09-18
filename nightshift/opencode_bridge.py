#!/usr/bin/env python3
"""Run nightshift's PreToolUse hooks for an OpenCode tool call.

`04_local_runtime.md` §8: *"nightshift's hooks are Python modules reading JSON on
stdin, so one shim module bridges all of them."* This is that shim's Python half. The
JS half is a plugin that pipes a call here and applies the answer.

**Why a bridge and not a JS reimplementation.** The rules already have one home. A
second copy in another language is the defect that cost a day on 2026-09-18, when the
`stale-hunter` charter headlined one verdict schema while the runner's prompt specified
another and the model — correctly — followed the document it was pointed at. So this
calls each hook's existing `evaluate()` rather than restating what it decides.

**The translation is the actual work**, because the two runtimes disagree on names:

| | Claude Code | OpenCode |
|---|---|---|
| tool | `Read`, `Edit`, `Bash` | `read`, `edit`, `bash` |
| file argument | `file_path` | `filePath` |
| command argument | `command` | `command` |

A hook that silently sees no `file_path` decides "nothing to deny" and reports success,
which is `silent-noop` — the failure class this repo has 26 recorded instances of. So an
unmapped tool name is passed through deliberately and a *mapped* tool whose path argument
is missing is reported, rather than both quietly becoming "allow".
"""
from __future__ import annotations

import json
import sys

#: OpenCode tool name -> the Claude Code name the hooks match on. A name absent here
#: is a tool no hook watches; it is allowed without asking any of them.
TOOL_NAMES = {
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "patch": "Edit",
    "bash": "Bash",
    "grep": "Grep",
    "glob": "Glob",
    "list": "Glob",
}

#: OpenCode argument name -> Claude Code argument name, per mapped tool.
ARG_NAMES = {
    "filePath": "file_path",
    "path": "file_path",
    "pattern": "pattern",
    "command": "command",
    "content": "content",
    "oldString": "old_string",
    "newString": "new_string",
}


def to_claude_payload(call: dict) -> dict | None:
    """Translate one OpenCode call into the payload the hooks expect.

    Returns None when no hook could have an opinion, which is different from
    "allowed" and is why the caller distinguishes them.
    """
    tool = str(call.get("tool") or "").strip()
    name = TOOL_NAMES.get(tool.lower())
    if not name:
        return None
    args = call.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    tool_input = {ARG_NAMES.get(k, k): v for k, v in args.items()}
    return {"tool_name": name, "tool_input": tool_input}


def decide(call: dict) -> tuple[str, str]:
    """(status, reason) for one call — status is 'allow' or 'deny'.

    Each hook is asked in turn and the first denial wins. A hook that raises is a bug
    in the hook, and the call is ALLOWED rather than blocked: a fence that crashes must
    not become a fence that stops all work, and the traceback goes to stderr where the
    run log keeps it.
    """
    payload = to_claude_payload(call)
    if payload is None:
        return "allow", ""

    try:
        for reason in _hook_verdicts(payload):
            if reason:
                return "deny", reason
    except Exception as exc:  # noqa: BLE001
        # _hook_verdicts guards each hook individually; this guards the generator
        # itself, which is what fails if `nightshift.hooks` cannot even be imported.
        print(f"opencode_bridge: verdict generator raised: {exc!r}", file=sys.stderr)
    return "allow", ""


def _hook_verdicts(payload: dict):
    """Yield each watching hook's verdict, importing lazily so one broken hook
    cannot stop the others from being consulted."""
    from nightshift.hooks import ideas_fence, worktree_fence

    try:
        env_name = worktree_fence._fence_env_name(worktree_fence._repo_root())
        yield worktree_fence.evaluate(payload, worktree_fence._allow_roots(env_name))
    except Exception as exc:  # noqa: BLE001 - see decide()'s docstring
        print(f"opencode_bridge: worktree_fence raised: {exc!r}", file=sys.stderr)

    try:
        # ideas_fence resolves both from the repo root, exactly as its own main() does;
        # hard-coding either here would be the second home this module exists to avoid.
        root = ideas_fence._repo_root()
        if root is not None:
            yield ideas_fence.evaluate(
                payload, ideas_fence._board_root(root), ideas_fence.private_lanes(root))
    except Exception as exc:  # noqa: BLE001
        print(f"opencode_bridge: ideas_fence raised: {exc!r}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    try:
        call = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Never block on a payload we cannot parse — same posture as the hooks
        # themselves, and the reason is the same: a fence that fails closed on a
        # parse error stops the night for a quoting bug.
        json.dump({"status": "allow", "reason": ""}, sys.stdout)
        return 0
    status, reason = decide(call if isinstance(call, dict) else {})
    json.dump({"status": status, "reason": reason}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
