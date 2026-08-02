#!/usr/bin/env python3
"""PreToolUse hook: a dispatched worker may only write inside its own worktree.

The failure this exists to prevent (Dungeoneer, 2026-07-25,
`drop-item-action-unwired`): a worker was handed a git worktree and told to do
all its work there, but it typed an **absolute path** to the canonical checkout
— the path it knows from the project's own instructions and memory — and ran
its edits and a `git commit` against the integration branch in the *main*
checkout instead. It caught itself that time; the next one may not, and the
cost is code committed onto the shared integration branch under a human who
never touched it.

"Tell it the cwd" is a convention a worker can forget the moment it reaches for
a path it already knows. This hook makes the wrong thing *impossible* rather
than merely discouraged — the same move `tier_guard`/`preflight_guard` make for
their rules. It is the prevention half; the guarantee is the runner's own
post-worker backstop (`assert_integration_unmoved`), which undoes any commit
that lands on the integration branch regardless of how it got there. Defense in
depth, because a worktree is a *linked* checkout: it shares `.git`, so a stray
commit from the wrong directory is a real object on a real branch, not a
sandbox scribble.

**Off unless the runner turns it on.** It fires only when the project's fence
env var is set — an `os.pathsep`-joined list of the roots a worker may write to
(its worktree, plus the run directory its verdict JSON legitimately lands in,
which sits under the main repo). Interactive sessions never set it, so this
guard is invisible there and touches only dispatched workers. An empty/unset
var is a no-op: allow everything.

**The env var name is read from `[worker].fence_env`, not duplicated here.**
It was parsed out of the project's `runner.py::FENCE_ENV` until
07_portability.md §8 step 4 moved the runner into this package and the constant
into the manifest. This hook must not invent a second, driftable source for the
one setting that decides whether it can see the fence at all — the same
reasoning `branch_role_prose` uses to read the declared integration branch
rather than hardcode a copy. A project that declares no `fence_env` falls back
to a generic default name, which nothing sets, so the fence stays off — the
same safe direction as every other failure path here.

Reads the PreToolUse payload on stdin, writes a JSON decision on stdout. Fails
**open** on anything it cannot parse — a guard that wedges a worker on confusion
is worse than none, and the runner backstop is the real net. No LLM
(`00_architecture.md` §12): path resolution and string inspection only.

Wired as `python -m nightshift.hooks.worktree_fence` in a consuming project's
`.claude/settings.json` (Bash|Edit|Write|NotebookEdit). Runnable by hand:
    DUNGEONEER_FENCE_ALLOW=/abs/worktree \\
      echo '{"tool_name":"Write","tool_input":{"file_path":"/etc/x"}}' \\
      | python -m nightshift.hooks.worktree_fence
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

NAME = "worktree_fence"

# What nothing sets when a project declares no `[worker].fence_env` — the fence
# then never arms, which is the correct behaviour for a project that has not
# configured one.
_DEFAULT_ENV_NAME = "NIGHTSHIFT_FENCE_ALLOW"

# `cd`/`pushd` to an absolute path is the exact move that took the worker out of
# its worktree. A relative `cd` stays within the tree it was launched in, so it
# is not the failure and not matched. `-C <path>`/`--git-dir=<path>` are git's
# own "operate over there" flags and get the same treatment.
_CD = re.compile(r"(?:^|[;&|]|\&\&|\|\|)\s*(?:cd|pushd)\s+(?P<path>[^\s;&|]+)")
_GIT_C = re.compile(r"\bgit\b[^;&|]*?\s-C\s+(?P<path>[^\s;&|]+)")
_GIT_DIR = re.compile(r"--git-dir(?:=|\s+)(?P<path>[^\s;&|]+)")


def _repo_root() -> Path | None:
    try:
        from nightshift.manifest import find_root

        return find_root()
    except Exception:
        return None


def _fence_env_name(repo_root: Path | None) -> str:
    """The env var name this project's runner sets — `[worker].fence_env`.

    Every failure to read it returns the default, which nothing sets, so the
    fence stays off. That is deliberate: a guard that wedges a worker because
    it could not parse a config is worse than no guard, and the runner backstop
    is the real net.
    """
    if repo_root is None:
        return _DEFAULT_ENV_NAME
    try:
        from nightshift.manifest import load

        return load(repo_root).worker.fence_env or _DEFAULT_ENV_NAME
    except Exception:
        return _DEFAULT_ENV_NAME


def _resolve(path: str) -> Path | None:
    """Best-effort absolute resolution. A path we cannot resolve is not matched —
    the runner backstop covers what the hook cannot see."""
    text = path.strip().strip("'\"")
    if not text:
        return None
    try:
        return Path(text).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _allow_roots(env_name: str) -> list[Path]:
    raw = os.environ.get(env_name, "")
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        if resolved := _resolve(part):
            roots.append(resolved)
    return roots


def evaluate(payload: dict, roots: list[Path]) -> str | None:
    """Return a denial reason, or None to allow. `roots` empty ⇒ fence off."""
    if not roots:
        return None
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}

    if tool in ("Write", "Edit", "NotebookEdit"):
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        resolved = _resolve(str(target))
        if resolved is not None and not _inside(resolved, roots):
            return _deny_text(str(target), roots)
        return None

    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        for pattern in (_CD, _GIT_C, _GIT_DIR):
            for match in pattern.finditer(command):
                candidate = match.group("path")
                resolved = _resolve(candidate)
                # Only an *absolute* escape is the failure; a relative token
                # resolves under the launch cwd (the worktree) and is fine.
                if resolved is not None and Path(candidate.strip().strip("'\"")).is_absolute() \
                        and not _inside(resolved, roots):
                    return _deny_text(candidate, roots)
        return None

    return None


def _deny_text(target: str, roots: list[Path]) -> str:
    shown = ", ".join(r.as_posix() for r in roots)
    return (
        f"Blocked: `{target}` is outside your assigned worktree.\n"
        f"You were dispatched into a git worktree and must do ALL work there — never "
        f"`cd` to an absolute path, and never touch the canonical checkout. Committing "
        f"to the shared integration branch from the wrong directory is the exact defect "
        f"this fence prevents (nightshift.hooks.worktree_fence).\n"
        f"Writable roots for this run: {shown}\n"
        f"Use paths relative to your worktree, or the absolute path of the worktree itself."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse
    env_name = _fence_env_name(_repo_root())
    reason = evaluate(payload if isinstance(payload, dict) else {}, _allow_roots(env_name))
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
