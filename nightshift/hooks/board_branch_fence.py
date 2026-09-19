#!/usr/bin/env python3
"""PreToolUse hook: a `Board/` write must happen from the integration branch.

`.ai/corrections.log`, slug `edited-the-board-on-a-branch-whose-skill-lacked-the-rule`
(2026-08-29): a live session reopened two cards, wrote a third and edited two docs
under `Board/` while the launch checkout sat on `ai/minigame-timing-invariant`, not
`test`. `Board/` is a tracked directory, so every branch carries its own copy of it —
the edit was invisible to the runner, and three of Karel's own commits on that branch
swept the stray files up under messages describing none of it. `manage-board`'s own
SKILL.md already says, in bold, "Never leave a board edit in a checkout that is not on
`test`" — but a skill file under version control is only as present as the checked-out
branch makes it, so the rule was missing from exactly the branch where the mistake
happened (it had been added on `test` after that branch was cut). The corrections
entry's own note: "a PreToolUse hook could refuse a write under `Board/` when
`git branch --show-current` is not the manifest's integration branch." This is that
hook.

**Why a hook and not only the doc.** The same corrections entry generalises: "for any
rule about WHICH checkout to write to, the branch check has to come from the session's
own state, never from the instructions, because the instructions are the thing that may
not have arrived." A skill can be stale on the branch that needs it most; `git branch
--show-current`, read fresh on every call, cannot be.

**Not the same failure `worktree_fence`/`fold_fence` guard.** Those two arm only when
the runner dispatches a worker into a linked worktree — an interactive session is the
case they deliberately leave untouched. This hook is the reverse: it exists *because*
nothing was watching the interactive session, so it is always on, for any checkout,
dispatched or not. A dispatched worker never writes `Board/` itself (the runner does
that, from a checkout `run()` has already put on the integration branch), so there is
nothing legitimate for this hook to block there either.

**Resolved from the write's own target, not the session's `cwd`.** The failure this
guards is precisely a checkout other than the one the session started in (or the
session's checkout having moved under it), so the repository asked about is the one
`file_path` actually names — the same reasoning `preflight_guard` uses for the
repository a `git push`/`merge` acts on.

Fails **open** on anything it cannot resolve: no git repository at the target, no
`.ai/manifest.toml`, no declared `[branches].integration`, or a current branch git
cannot report (detached HEAD). A guard that wedges a session on a config or a git
state it could not read is worse than none, and `manage-board`'s own backstop
(`runner.stranded_board_refusal`) still catches what reaches the runner regardless.

Reads the PreToolUse payload on stdin, writes a JSON decision on stdout. No LLM
(`00_architecture.md` §12): path resolution and `git branch --show-current` only.

Wired as `python -m nightshift.hooks.board_branch_fence` in a consuming project's
`.claude/settings.json` (Write|Edit|NotebookEdit). Runnable by hand for testing:
    echo '{"tool_name":"Edit","tool_input":{"file_path":"/repo/Board/tasks/x.md"}}' \\
        | python -m nightshift.hooks.board_branch_fence
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

from nightshift.manifest import AI_DIR, MANIFEST_NAME, ManifestError

NAME = "board_branch_fence"


def _git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo_root), *args],
                         capture_output=True, text=True, timeout=10,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _nearest_existing_dir(path: Path) -> Path | None:
    """The closest existing ancestor of `path`'s directory, or None.

    `Write` may name a file that does not exist yet, so `git -C` needs a
    directory it can actually open — walking up costs nothing and a fresh
    `Board/tasks/<new-card>.md` is the common case a strict `is_dir()` would
    otherwise miss entirely.
    """
    directory = path if path.is_dir() else path.parent
    for candidate in (directory, *directory.parents):
        if candidate.is_dir():
            return candidate
    return None


def _under_board(target: str, repo_root: Path) -> str | None:
    """`target`'s path relative to `repo_root`, if it names something under
    `Board/`; else None. A first-path-segment check, not a prefix string
    match, so a hypothetical `Boardwalk/` directory is not mistaken for it."""
    try:
        resolved = Path(str(target).strip().strip("'\"")).resolve()
        rel = resolved.relative_to(repo_root.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    posix = PurePosixPath(rel.as_posix())
    if not posix.parts or posix.parts[0] != "Board":
        return None
    return posix.as_posix()


def _toplevel(directory: Path) -> Path | None:
    out = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=10,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        return None
    line = (out.stdout or "").strip()
    return Path(line).resolve() if line else None


def evaluate(payload: dict) -> str | None:
    """Return a denial reason, or None to allow."""
    if payload.get("tool_name") not in ("Write", "Edit", "NotebookEdit"):
        return None
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not target:
        return None

    directory = _nearest_existing_dir(Path(str(target).strip().strip("'\"")))
    if directory is None:
        return None
    root = _toplevel(directory)
    if root is None:
        return None  # not a git work tree

    rel = _under_board(target, root)
    if rel is None:
        return None  # not a Board/ write

    if not (root / AI_DIR / MANIFEST_NAME).is_file():
        return None  # not a nightshift project — not ours to guard

    from nightshift import branches

    try:
        integration = branches.integration(root)
    except ManifestError:
        return None  # no declared integration branch — nothing to check against

    current = _git(root, "branch", "--show-current")
    if not current or current == integration:
        return None

    return (
        f"Blocked: `{rel}` would be written from `{current}`, but "
        f".ai/manifest.toml's `[branches].integration` names `{integration}` as the "
        f"branch the runner reads Board/ from.\n"
        f"`Board/` is a tracked directory, so every branch carries its own copy — an "
        f"edit made here is invisible to the runner until it reaches `{integration}` "
        f"(manage-board's \"The board you see may not be the board the runner "
        f"reads\"). This is the exact failure logged as "
        f"`edited-the-board-on-a-branch-whose-skill-lacked-the-rule`: a board edit "
        f"stranded on a feature branch a night's dispatch never opens.\n"
        f"Either switch this checkout to `{integration}` (`git checkout {integration}`) "
        f"and make the edit there, or make it from a checkout that already is on "
        f"`{integration}` — this project's dedicated integration checkout, if it has "
        f"cut one (nightshift.hooks.board_branch_fence)."
    )


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
