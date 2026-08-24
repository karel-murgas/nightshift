#!/usr/bin/env python3
"""PreToolUse hook: a dispatched worker writes a memory *fragment*, not the shared log.

The sibling of `worktree_fence`, and the same move for a different rule: that one is
about *where* a worker may write, this one about *which file*. Both exist because a
convention a worker can forget is not a mechanism.

**The rule.** Every file declared in `[[memory.fold]]` is a shared append-at-the-top
log — a per-subsystem register, a dated history — and every card wants to add its
entry at the same anchor. A card that edits one directly conflicts with any sibling
card that finishes the same night, by construction rather than by disagreement. That
cost four hand-resolved rebases in Dungeoneer (2026-08-09, 2026-08-13, and two on
2026-08-22) before anyone counted them as one problem.

So a dispatched worker writes `.ai/memory-fragments/<card-id>.md` instead — a file no
other card touches — and `nightshift.memoryfold` folds it into the real log after the
card's branch merges, serially, where the insertion is safe. See that module for why
this is the newsfragment pattern, why a union merge driver was rejected, and why the
fragment lives under `.ai/` rather than under the board it was mistaken for at first.

**Off unless the runner turns it on, and off unless the project declares a target.**
It arms on the same `[worker].fence_env` variable `worktree_fence` reads, which only a
dispatched worker has set — so interactive curation of these files, which is a real
and frequent thing a human does, is untouched. A project with no `[[memory.fold]]`
rows has nothing to fence.

Fails **open** on anything it cannot read or parse: a guard that wedges a worker on a
config it could not load is worse than none, and a direct edit merely reintroduces the
conflict this prevents rather than corrupting anything.

Reads the PreToolUse payload on stdin, writes a JSON decision on stdout. No LLM
(`00_architecture.md` §12): path comparison only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

NAME = "fold_fence"

_DEFAULT_ENV_NAME = "NIGHTSHIFT_FENCE_ALLOW"


def _repo_root() -> Path | None:
    try:
        from nightshift.manifest import find_root

        return find_root()
    except Exception:
        return None


def _armed(repo_root: Path | None) -> bool:
    """Whether a dispatched worker is what is running. Same switch as the worktree
    fence, read the same way, so the two arm and disarm together."""
    name = _DEFAULT_ENV_NAME
    if repo_root is not None:
        try:
            from nightshift.manifest import load

            name = load(repo_root).worker.fence_env or _DEFAULT_ENV_NAME
        except Exception:
            pass
    return bool(os.environ.get(name, "").strip())


def _fold_paths(repo_root: Path | None) -> list[str]:
    """Declared fold targets as posix repo-relative paths, or `[]`."""
    if repo_root is None:
        return []
    try:
        from nightshift.manifest import load

        # `removeprefix`, never `lstrip("./")` — `lstrip` strips a *character set*,
        # so it ate the leading dot of `.claude/memory/state.md` and left
        # `claude/memory/state.md`, which matches nothing. The fence then allowed
        # every write it exists to refuse, silently and in every repo whose memory
        # lives under a dotted directory. Caught by running it, not by reading it.
        return [t.path.replace("\\", "/").removeprefix("./")
                for t in load(repo_root).memory.fold]
    except Exception:
        return []


def _fenced(target: str, fold_paths: list[str]) -> str | None:
    """The declared path `target` names, or None.

    Compared on the **tail** of the resolved path rather than against a repo root:
    the worker writes inside a linked worktree whose root is not the repo's, so
    `…/.dungeoneer-worktrees/probe/.claude/memory/state.md` must match a declared
    `.claude/memory/state.md`. A tail match cannot produce a false negative for the
    file this rule is about, and a false positive would need a second file of the
    same name at the same depth — which would be the same shared log anyway.
    """
    try:
        text = Path(str(target).strip().strip("'\"")).as_posix()
    except (OSError, ValueError, RuntimeError):
        return None
    for declared in fold_paths:
        if text == declared or text.endswith("/" + declared):
            return declared
    return None


def evaluate(payload: dict, fold_paths: list[str]) -> str | None:
    """A denial reason, or None to allow. `fold_paths` empty ⇒ fence off."""
    if not fold_paths:
        return None
    if payload.get("tool_name") not in ("Write", "Edit", "NotebookEdit"):
        # Bash is deliberately not inspected. A worker rewriting one of these files
        # through a shell one-liner is not the failure being prevented — the failure
        # is the ordinary edit a charter tells it to make — and pattern-matching
        # shell for file writes is the kind of guess that produces false denials.
        return None
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    declared = _fenced(str(target), fold_paths)
    if declared is None:
        return None
    # gate-ok(source_reference_liveness): `.ai/memory-fragments/` is a per-consuming-project
    # runtime directory nightshift.memoryfold creates on demand; this framework's own
    # checkout carries none, since fragments belong to a project's cards, not this package's.
    return (
        f"Blocked: `{declared}` is a shared memory log and a dispatched card must not "
        f"edit it directly.\n"
        f"Every card appends at the same anchor in this file, so two cards finishing "
        f"the same night conflict by construction — that cost four hand-resolved "
        f"rebases before it was fixed.\n"
        f"Write your record to `.ai/memory-fragments/<card-id>.md` instead, with one `## "
        f"<key>` section per fold target (see `[[memory.fold]]` in .ai/manifest.toml "
        f"for the keys). It is folded into this file automatically, serially, once "
        f"your branch merges (nightshift.hooks.fold_fence)."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse
    root = _repo_root()
    if not _armed(root):
        return 0
    reason = evaluate(payload if isinstance(payload, dict) else {}, _fold_paths(root))
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
