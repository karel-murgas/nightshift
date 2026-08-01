#!/usr/bin/env python3
"""Run one of the consuming project's own hook scripts, from any working directory.

Why this exists (Dungeoneer, 2026-07-30, `hook-command-resolved-against-a-moved-cwd`):
every command in a project's `.claude/settings.json` is run by the harness with the
*session's* current working directory, not the repo root. A mid-session `cd` into a
subdirectory was still in effect when a `PostToolUse` hook fired, so
`python .ai/recipes/hint.py` resolved as `.ai/gates/.ai/recipes/hint.py` — "can't open
file" — and both the Edit that triggered it and the next Bash call were refused until
the cwd was reset. The hooks that exist to *refuse* things are disabled by the same
wedge, which is why a relative path in a hook command is not a cosmetic problem: a cwd a
worker happens to leave behind breaks the guard rather than tripping it.

`python -m nightshift.hooks.<name>` fixed that for every hook living in this package —
module resolution goes through the installed distribution and never consults the cwd. A
project's own scripts cannot move here (`.ai/recipes/hint.py`'s rule table is Dungeoneer
paths, and `00_architecture.md` §15 keeps a project's rules in the project), so they
need a cwd-independent way to be *reached* instead:

    python -m nightshift.hooks.project_script .ai/recipes/hint.py

The module part is resolved by the installed package; the script part is resolved
against `manifest.find_root()`, which walks *up* from the cwd — so it lands on the same
file whether the session sits at the repo root or three directories inside it. That is
the property the old form lacked, and the one its tests exercise directly.

**Deliberately not `$CLAUDE_PROJECT_DIR`.** The harness does export a project-directory
variable, and `python "$CLAUDE_PROJECT_DIR/.ai/recipes/hint.py"` would very likely work
too — but it is a bet on a shell performing the expansion, and that bet cannot be
checked from inside the repo without wiring a probe hook and watching it fire. The
correction being closed here belongs to a family (`derived-not-verified`) about shipping
a conclusion that was never run against a real case, so the form that can be verified by
running it wins over the form that merely ought to work. `find_root()` is checkable from
any directory, which is exactly what the tests do.

**Fails open, loudly.** An unfindable root or a missing script exits 0 with a message on
stderr rather than blocking the tool call: a wrapper that wedges a session because the
project moved a file is worse than one that does nothing, which is the same direction
`worktree_fence` takes on an unparseable payload. It is *loud* because a silent zero is
the `silent-noop` shape this log records over and over — the harness surfaces hook
stderr, so the operator sees why nothing ran. When the script does run its exit code is
passed through unchanged, so a project script that blocks stays blocking.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

NAME = "project_script"

_USAGE = "usage: python -m nightshift.hooks.project_script <repo-relative-script> [args...]"


def resolve(rel: str, start: Path | None = None) -> tuple[Path | None, Path | None, str | None]:
    """`(repo root, script path, error)` — the error is None iff both paths are set.

    `start` defaults to the working directory, which is the whole point: the answer must
    not depend on which directory inside the repo the harness happened to be in.
    """
    if not rel:
        return None, None, f"no script given; {_USAGE}"
    if Path(rel).is_absolute():
        return None, None, (
            f"{rel!r} is absolute — this shim takes a repo-relative path so the same "
            f"settings.json works on every machine and every checkout"
        )

    try:
        from nightshift.manifest import find_root

        root = find_root(start).resolve()
    except Exception as exc:  # ManifestError, or anything find_root's markers hit
        return None, None, f"no repo root above {(start or Path.cwd())}: {exc}"

    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        return None, None, f"{rel!r} escapes the repo root {root}"
    if not target.is_file():
        return None, None, f"{target} does not exist"
    return root, target, None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root, script, error = resolve(args[0] if args else "")
    if error is not None:
        print(f"{NAME}: {error}", file=sys.stderr)
        return 0  # fail open — see the module docstring

    # Not captured: the child's stdout/stderr are the hook's own, which is how a hint
    # script's output reaches the operator. Nothing is decoded here, so the
    # `text=True`-without-`encoding=` trap `subprocess_encoding` exists for cannot apply.
    result = subprocess.run([sys.executable, str(script), *args[1:]], cwd=str(root),
                            check=False)
    return result.returncode


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
