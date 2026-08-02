#!/usr/bin/env python3
"""PreToolUse hook: no judgment actor may open the board's private lane.

The failure this exists to prevent (Dungeoneer, 2026-07-27, `triage-read-private-ideas`):
the `triage` agent, dispatched on an inbox note, glob/grep'd the board for context and
read `Board/ideas/Generovani zvuku.md` — the lane its own charter says no judgment actor
may ever open ("Not avoid unless useful: never"). It self-reported afterwards and
nothing from that file reached the card it wrote, so the cost that time was zero. The
reason it happened is the part that generalises: the prohibition existed **only as
charter prose the agent has to remember**, while the agent does its own file discovery.
`reconcile.py`, the one component permitted in that lane, is hard-coded to read exactly
one field from it — a boundary, not a promise. A prose-only "never open X" rule handed to
something that greps for itself will eventually be crossed by a wide enough search.

**What counts as private is derived, never listed here.** `nightshift.board.LANES` is the
board's single home for "which lanes exist", and the private lane is defined by its
*absence* from that tuple — that is literally what `board.py`'s own comment says
(`ideas/` is absent "for the same reason it is absent from reconcile.LANES: it is
Karel's private lane and no judgment actor — the runner included — enumerates it"). So
this hook parses that constant out of the project's `board.py` and treats any board
subdirectory missing from it as private, rather than writing the word "ideas" down a
fourth time. Same technique, and the same reason, as `worktree_fence` reading
`runner.py::FENCE_ENV` instead of copying it: `roster-has-two-homes` is the correction
about what a second copy costs.

A consequence worth stating, because it is a feature rather than a surprise: a *new*
directory under `Board/` is private until it is added to `LANES`. The denial says so and
names the fix. Defaulting an unknown lane to readable would be the wrong direction for a
guard whose entire subject is a lane nobody remembered to protect.

**Coverage, honestly bounded.** It denies `Read`, `Grep`, `Glob` and `Bash` calls whose
target names a private lane. That closes the path the incident actually took — the agent
enumerated the board, then *opened the file*, and the open is the step that leaks the
content. It does **not** stop a repo-wide content grep from surfacing a matching line in
passing; blocking every unscoped search would break ordinary work, and the residual is
recorded here rather than papered over.

**Off if it cannot be sure.** No repo root, no `board.py`, an unparseable `LANES`, or a
`LANES` that comes back empty ⇒ the fence does not arm. The alternative — treating every
board directory as private when the parse fails — would deny all board access on a
malformed file, and a guard that bricks the board when it is confused gets removed.

**Escape hatch, for Karel and nobody else.** Setting `NIGHTSHIFT_PRIVATE_LANE_UNLOCK=1`
in the environment the harness itself was launched with turns the fence off. A dispatched
agent cannot reach it: an inline `VAR=1 cmd` prefix in a `Bash` call sets the variable for
that child process, while this hook is spawned by the harness and inherits the harness's
environment, so the only actor who can set it is the human who started the session.

No LLM (`00_architecture.md` §12): AST for the constant, string inspection for the rest.

Wired as `python -m nightshift.hooks.ideas_fence` under `PreToolUse` for
`Read|Grep|Glob|Bash`. Runnable by hand:
    echo '{"tool_name":"Read","tool_input":{"file_path":"Board/ideas/x.md"}}' \\
      | python -m nightshift.hooks.ideas_fence
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

NAME = "ideas_fence"

_UNLOCK_ENV = "NIGHTSHIFT_PRIVATE_LANE_UNLOCK"

_WATCHED = ("Read", "Grep", "Glob", "Bash")
# Where each tool names the thing it is about. Checked as strings so an absolute path, a
# repo-relative one and a shell command are all covered by one rule.
_FIELDS = ("file_path", "path", "pattern", "glob", "command", "notebook_path")


def _repo_root() -> Path | None:
    try:
        from nightshift.manifest import find_root

        return find_root()
    except Exception:
        return None


def _board_root(repo_root: Path) -> str:
    try:
        from nightshift.manifest import load

        return load(repo_root).board.root
    except Exception:
        return "Board"


def known_lanes(repo_root: Path) -> tuple[str, ...]:
    """The lanes the board machinery enumerates — `board.LANES`.

    Parsed out of `.ai/board.py` until 07_portability.md §8 step 4, because
    `.ai/` is not an importable package and a hook must not depend on it being
    one. `board` now ships in this package, so the honest move is to import the
    one definition instead of keeping a parser that could disagree with it.

    `()` on any failure, which turns the fence off — the same safe direction the
    parser took, and the same one every other path in this file takes.
    """
    try:
        from nightshift.board import LANES

        return tuple(LANES)
    except Exception:
        return ()


def private_lanes(repo_root: Path) -> tuple[str, ...]:
    """Board subdirectories absent from `LANES`. Empty ⇒ the fence does not arm."""
    lanes = known_lanes(repo_root)
    if not lanes:
        return ()
    board = repo_root / _board_root(repo_root)
    if not board.is_dir():
        return ()
    return tuple(sorted(
        d.name for d in board.iterdir()
        if d.is_dir() and d.name not in lanes and not d.name.startswith(".")
    ))


def _mentions(text: str, board: str, lane: str) -> bool:
    """Does `text` name `<board>/<lane>` as a path, in any separator or case?

    Case-insensitive because Windows paths are, and a fence that a different casing
    walks straight through is not a fence.
    """
    needle = f"{board}/{lane}".lower()
    return needle in text.replace("\\", "/").lower()


def evaluate(payload: dict, board: str, lanes: tuple[str, ...]) -> str | None:
    """Return a denial reason, or None to allow. `lanes` empty ⇒ fence off."""
    if not lanes:
        return None
    if payload.get("tool_name") not in _WATCHED:
        return None
    tool_input = payload.get("tool_input") or {}
    haystack = " ".join(str(tool_input.get(field, "")) for field in _FIELDS)
    if not haystack.strip():
        return None
    for lane in lanes:
        if _mentions(haystack, board, lane):
            return _deny_text(board, lane)
    return None


def _deny_text(board: str, lane: str) -> str:
    return (
        f"Blocked: `{board}/{lane}/` is a private lane and no judgment actor may open "
        f"it.\n"
        f"It is private because it is absent from `nightshift.board.LANES`, which is the "
        f"board's single list of working lanes. The only component permitted in there is "
        f"`reconcile.py`, which reads one frontmatter field and never the body.\n"
        f"If you need context for a card, use `{board}/inbox/`, the card itself, and the "
        f"codebase — that is what the note you were given was built from.\n"
        f"If `{lane}` is in fact a working lane, the fix is to add it to `nightshift.board.LANES`, "
        f"not to read around this.\n"
        f"(nightshift.hooks.{NAME})"
    )


def main() -> int:
    if os.environ.get(_UNLOCK_ENV):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse
    root = _repo_root()
    if root is None:
        return 0
    reason = evaluate(payload if isinstance(payload, dict) else {},
                      _board_root(root), private_lanes(root))
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
