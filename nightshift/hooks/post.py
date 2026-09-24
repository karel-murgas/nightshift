#!/usr/bin/env python3
"""PostToolUse dispatcher for Write/Edit: the gate report and the project's own
after-edit scripts, in one Python process.

Runs `gates_on_edit` (a report, never a block), then each repo-relative script named
on the command line in-process with the same payload on stdin — what
`nightshift.hooks.project_script` used to spawn a second interpreter for. Always
exits 0: a failing script is reported, not allowed to block the edit that would fix it.

    python -m nightshift.hooks.post .ai/recipes/hint.py
"""
from __future__ import annotations

SUBJECT = "nightshift/hooks/gates_on_edit.py"

import contextlib
import io
import json
import runpy
import sys
from pathlib import Path

from nightshift.hooks import gates_on_edit, project_script

#: `nightshift.update` strips a settings entry naming one of these alone.
SUPERSEDED = frozenset({"nightshift.hooks.gates_on_edit"})


def _run_script(rel: str, raw: str, cwd: Path) -> str:
    """Run one project script as `__main__` with `raw` on stdin; its stdout, or why not."""
    _, target, error = project_script.resolve(rel, cwd)
    if error:
        return f"nightshift.hooks.post: {error}"
    out = io.StringIO()
    saved_stdin, saved_argv = sys.stdin, sys.argv
    sys.stdin, sys.argv = io.StringIO(raw), [str(target)]
    try:
        with contextlib.redirect_stdout(out):
            runpy.run_path(str(target), run_name="__main__")
    except SystemExit:
        pass
    except Exception as exc:  # noqa: BLE001 — a project script must not break the hook
        return f"nightshift.hooks.post: {rel} raised {type(exc).__name__}: {exc}"
    finally:
        sys.stdin, sys.argv = saved_stdin, saved_argv
    return out.getvalue().rstrip()


def main(argv: list[str] | None = None) -> int:
    scripts = sys.argv[1:] if argv is None else argv
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    given = Path(str(payload.get("cwd") or ""))
    cwd = given if str(given) not in ("", ".") and given.is_dir() else Path.cwd()
    try:
        from nightshift.manifest import find_root

        print(gates_on_edit.run(find_root(cwd).resolve(), str(payload.get("session_id") or "")))
    except Exception as exc:  # noqa: BLE001
        print(f"gates_on_edit: could not run the gates ({type(exc).__name__}: {exc}) — "
              f"run `python -m nightshift.gates.run` by hand")
    for rel in scripts:
        said = _run_script(rel, raw, cwd)
        if said:
            print(said)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
