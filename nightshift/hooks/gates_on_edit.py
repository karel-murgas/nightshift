#!/usr/bin/env python3
"""PostToolUse hook: the gate suite after an edit, reporting only what is news.

It replaces `python -m nightshift.gates.run; exit 0` as the on-edit command. The
gates are the same and so is the enforcement point that matters — the runner and
the preflight still run every gate before anything merges. What changes is what the
model is made to read after each Edit, because a PostToolUse hook's stdout is
appended to the tool result and carried in context for the rest of the session.

Measured on real worker transcripts (`token-economy.md` §1): 50–143k characters of
this hook's output per card, ~15–35k tokens, almost all repetition — a 51-name
all-clear line after every edit, the same violation lines again after every edit
that did not touch them — plus ~21 s of wall time per edit.

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

**A dispatched worker skips the slow gates per edit.** When the worker fence env
var is set (the same switch `tool_economy` and `worktree_fence` read), any gate
whose last measured run took longer than `SLOW_S` is deferred: the worker is told
to run `python -m nightshift.gates.run` once before its verdict, and the runner runs
every gate over its branch regardless. Timings are measured by this hook on every
gate it does run and kept per project in the temp directory, so a new gate runs
until it is known to be slow and no list of slow gates exists to go stale. An
interactive session always runs all of them.

Always exits 0: a violation must show, not block the edit that would fix it. Fails
open with a one-line note on anything unexpected.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

NAME = "gates_on_edit"

#: A gate slower than this, measured, is deferred per edit in a dispatched worker.
#: Measured 2026-09-15 on Project Tigress: 51 gates, 15.4 s in-process, of which
#: the slowest one alone was 4.7 s and the six above this line were 9 s.
SLOW_S = 0.5


def _timings_path(root: Path) -> Path:
    try:
        from nightshift.manifest import load

        name = load(root).project.name or root.name
    except Exception:
        name = root.name
    return Path(tempfile.gettempdir()) / "nightshift-hooks" / f"gate-timings-{name}.json"


def _load_timings(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_timings(path: Path, timings: dict[str, float]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(timings, sort_keys=True), encoding="utf-8", newline="\n")
    except OSError:
        pass


def report(found: dict[str, str], previous: set[str] | None, deferred: int) -> str:
    """The text for one run. `found` maps a violation's stable key to its line;
    `previous` is the key set open at the last run (None: first run this session)."""
    tail = (f" ({deferred} slow gate(s) deferred — run `python -m nightshift.gates.run` "
            f"once before you finish)" if deferred else "")
    if not found:
        return f"gates: ok{tail}"
    new = [key for key in found if previous is None or key not in previous]
    lines = [found[key] for key in new]
    still = len(found) - len(new)
    if new:
        summary = f"gates: {len(new)} new violation(s)"
        if still:
            summary += f", {still} still open from earlier edits"
    else:
        summary = f"gates: {still} violation(s) still open, unchanged since shown"
    return "\n".join([*lines, summary + tail])


def run(root: Path, session_id: str, armed: bool) -> str:
    from nightshift.gates import run as gates_run
    from nightshift.hooks import session_memo

    gates = gates_run.discover(root)
    timings_path = _timings_path(root)
    timings = _load_timings(timings_path)
    deferred = sorted(n for n in gates if armed and timings.get(n, 0.0) > SLOW_S)

    found: dict[str, str] = {}
    for name in sorted(gates):
        if name in deferred:
            continue
        started = time.perf_counter()
        try:
            violations = gates[name].check(root)  # type: ignore[attr-defined]
        except Exception as exc:
            found[f"{name}|crash"] = f"{name}: the gate crashed — {type(exc).__name__}: {exc}"
            continue
        finally:
            timings[name] = round(time.perf_counter() - started, 3)
        for v in violations:
            # The line number is left out of the key on purpose: an edit above a
            # violation moves it, and a moved line is not a new violation.
            found[f"{name}|{v.file}|{v.rule}"] = f"{v} [{name}]"
    _save_timings(timings_path, timings)

    previous = session_memo.swap(session_id, "gates", found)
    return report(found, previous, len(deferred))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        from nightshift.hooks import tool_economy
        from nightshift.manifest import find_root

        given = Path(str(payload.get("cwd") or ""))
        start = given if str(given) not in ("", ".") and given.is_dir() else Path.cwd()
        root = find_root(start).resolve()
        print(run(root, str(payload.get("session_id") or ""), tool_economy.armed()))
    except Exception as exc:
        print(f"{NAME}: could not run the gates ({type(exc).__name__}: {exc}) — "
              f"run `python -m nightshift.gates.run` by hand")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
