#!/usr/bin/env python3
"""PostToolUse hook: the gate suite after an edit, reporting only what is news.

It replaces `python -m nightshift.gates.run; exit 0` as the on-edit command. The
gates are the same — every one of them, after every edit, in every session — and so
is the enforcement point that matters: the runner and the preflight still run every
gate before anything merges. What changes is only what the model is made to read
after each Edit, because a PostToolUse hook's stdout is appended to the tool result
and carried in context for the rest of the session.

Measured on real worker transcripts (`token-economy.md` §1): 50–143k characters of
this hook's output per card, ~15–35k tokens, almost all repetition — a 51-name
all-clear line after every edit, the same violation lines again after every edit
that did not touch them.

What it prints now:

* **Nothing wrong:** `gates: ok`. Never the list of gate names.
* **A violation that was not open at the previous edit:** printed in full, once.
* **Violations still open from before:** one count line, not the text again. A
  violation that is fixed and later reintroduced is news again and is printed again.

**Not filtered to the files the session edited.** That was considered and refused:
a gate often reports on a file the edit did *not* touch — an i18n change reported
against the memory doc it now requires, a deleted symbol against the recipe that
still cites it — and those are exactly the violations a worker must see. Showing
each one once is the saving; hiding some is not.

**No gate is skipped per edit, deliberately.** Deferring the slow ones for a
dispatched worker was built and removed the same day (2026-09-15): it saved wall
time, not tokens, and it moved a violation's discovery from the edit that caused it
to the end of the attempt — where a worker that forgot its final gate run would
lose the whole attempt to the runner's gate check.

Always exits 0: a violation must show, not block the edit that would fix it. Fails
open with a one-line note on anything unexpected.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

NAME = "gates_on_edit"


def report(found: dict[str, str], previous: set[str] | None) -> str:
    """The text for one run. `found` maps a violation's stable key to its line;
    `previous` is the key set open at the last run (None: first run this session)."""
    if not found:
        return "gates: ok"
    new = [key for key in found if previous is None or key not in previous]
    lines = [found[key] for key in new]
    still = len(found) - len(new)
    if new:
        summary = f"gates: {len(new)} new violation(s)"
        if still:
            summary += f", {still} still open from earlier edits"
    else:
        summary = f"gates: {still} violation(s) still open, unchanged since shown"
    return "\n".join([*lines, summary])


def run(root: Path, session_id: str) -> str:
    from nightshift.gates import run as gates_run
    from nightshift.hooks import session_memo

    gates = gates_run.discover(root)
    found: dict[str, str] = {}
    for name in sorted(gates):
        try:
            violations = gates[name].check(root)  # type: ignore[attr-defined]
        except Exception as exc:
            found[f"{name}|crash"] = f"{name}: the gate crashed — {type(exc).__name__}: {exc}"
            continue
        for v in violations:
            # The line number is left out of the key on purpose: an edit above a
            # violation moves it, and a moved line is not a new violation.
            found[f"{name}|{v.file}|{v.rule}"] = f"{v} [{name}]"

    previous = session_memo.swap(session_id, "gates", found)
    return report(found, previous)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        from nightshift.manifest import find_root

        given = Path(str(payload.get("cwd") or ""))
        start = given if str(given) not in ("", ".") and given.is_dir() else Path.cwd()
        root = find_root(start).resolve()
        print(run(root, str(payload.get("session_id") or "")))
    except Exception as exc:
        print(f"{NAME}: could not run the gates ({type(exc).__name__}: {exc}) — "
              f"run `python -m nightshift.gates.run` by hand")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
