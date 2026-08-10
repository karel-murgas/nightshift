#!/usr/bin/env python3
"""Gate runner. Deterministic Python only -- no LLM calls anywhere in this
directory (00_architecture.md SS12). Each gate module is independently
importable and runnable; this script just discovers and drives them.

Usage (from anywhere inside the repo being gated):
    python -m nightshift.gates.run [--fix] [gate_name ...]

**Two gate directories, not one.** Core gates ship inside this package; a
project's own gates live in its `<root>/.ai/gates/`. That split is permanent, not
a migration artefact: 00_architecture.md SS15's rule is that gate selection follows
*observed failures*, so a rule a repo earned from its own incident belongs to that
repo. A new project starts with the core gates, an empty audit matrix and an empty
corrections log, and grows its own set from there.

A name collision is an error rather than a silent precedence rule. If a project
writes `line_endings.py` while core also ships one, "which one ran?" is a question
whose answer must not be a convention nobody remembers -- and the fix (rename
yours, or appeal the core one) is cheap and obvious once it is said out loud.

**The `.ai` directory name.** 00_architecture.md and the existing `Board/` use a
dot-prefixed `.ai/` root throughout. Python cannot import a dotted directory name
as a package (`import ai` will never find a folder literally named `.ai`), so a
project's gates cannot be a package either. This script puts both gate directories
on `sys.path`, which is what lets a project gate do a plain `import doc_scan` or
`import appeal_markers` for its neighbours -- while `from nightshift.gates.base import
Violation` resolves through the installed package from anywhere, including when a
gate module is run directly as a script.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from nightshift.manifest import AI_DIR, find_root

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # violation text can contain cs/es diacritics

_CORE_GATES_DIR = Path(__file__).resolve().parent

# Not gates: `run` is this file, `base` is the shared type. Helper modules --
# core's `doc_scan` and `appeal_markers`, or a project's own (Dungeoneer's
# `i18n_adapter_loader`, shared by its three i18n gates) -- need no entry here:
# they are identified by having no `check`, which is why adding a helper has never
# required editing this list.
_NOT_GATES = frozenset({"run", "base", "__init__"})


def gate_dirs(repo_root: Path) -> list[Path]:
    """Core gates first, then the project's own. Order is display order only --
    collisions are refused rather than resolved by precedence."""
    dirs = [_CORE_GATES_DIR]
    project = repo_root / AI_DIR / "gates"
    if project.is_dir() and project.resolve() != _CORE_GATES_DIR:
        dirs.append(project)
    return dirs


def discover(repo_root: Path) -> dict[str, object]:
    """`{gate name: module}` across both directories."""
    dirs = gate_dirs(repo_root)
    for directory in dirs:
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))

    gates: dict[str, object] = {}
    origin: dict[str, Path] = {}
    for directory in dirs:
        for path in sorted(directory.glob("*.py")):
            if path.stem in _NOT_GATES:
                continue
            module = importlib.import_module(path.stem)
            if not hasattr(module, "check"):
                continue
            if path.stem in gates:
                raise RuntimeError(
                    f"two gates are named {path.stem!r}: {origin[path.stem]} and {path}. "
                    f"Rename the project's one -- which gate ran must never depend on a "
                    f"precedence rule nobody remembers."
                )
            gates[path.stem] = module
            origin[path.stem] = path
    return gates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Nightshift gates (deterministic Python only).")
    parser.add_argument("gates", nargs="*", help="specific gate names to run (default: all)")
    parser.add_argument("--fix", action="store_true",
                        help="apply autofixes where a gate supports one (currently none do)")
    parser.add_argument("--json", action="store_true",
                        help="emit violations as structured JSON ({file, line, rule, gate}) "
                             "instead of the human-readable text, for a caller that needs to "
                             "act on *which paths* failed rather than read a sentence about it")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to gate (default: found from the working directory)")
    # Everything EXCEPT these. The distinction from naming gates positionally matters
    # for the caller this exists for -- an editor's on-save hook, which has a timeout
    # and must not be the place a slow gate is discovered. Listing the ~30 wanted gates
    # instead would be a hand-written list of subjects, which goes stale silently the
    # day a gate is added: the new gate simply would not run there and nothing would
    # say so. Skipping names the exemption instead, so the default stays "all of them".
    parser.add_argument("--skip", action="append", default=[], metavar="NAME",
                        help="run every gate EXCEPT these (repeatable)")
    args = parser.parse_args(argv)

    repo_root = (args.root or find_root()).resolve()
    all_gates = discover(repo_root)
    selected = args.gates or sorted(all_gates)
    # A typo'd --skip is an error, not a silent no-op. It fails in the *safe*
    # direction (the gate still runs), which is exactly why it would never be
    # noticed -- the same "typo'd key that silently does nothing" this project
    # refuses in `manifest.validate`.
    unknown = [g for g in (*selected, *args.skip) if g not in all_gates]
    if unknown:
        print(f"Unknown gate(s): {', '.join(unknown)}")
        print(f"Available: {', '.join(sorted(all_gates))}")
        return 2
    selected = [g for g in selected if g not in set(args.skip)]
    if not selected:
        print("Nothing to run — every gate was skipped.")
        return 2

    total = 0
    # `--json`'s payload, built alongside the text output rather than instead of
    # running the gates a second time -- one pass over `selected` either way.
    structured: list[dict] = []
    for name in selected:
        module = all_gates[name]
        violations = module.check(repo_root)  # type: ignore[attr-defined]
        if args.fix and hasattr(module, "fix"):
            module.fix(repo_root)  # type: ignore[attr-defined]
        for v in violations:
            if args.json:
                structured.append({"gate": name, "file": v.file, "line": v.line,
                                   "rule": v.rule})
            else:
                print(str(v))
            total += 1

    if args.json:
        # One line, one object -- a caller that only wants to know whether this
        # run was clean can check `violations == []` without touching `total`.
        print(json.dumps({"violations": structured, "total": total,
                          "gates": selected}))
        return 1 if total else 0

    if total:
        print(f"\n{total} violation(s) across {len(selected)} gate(s): {', '.join(selected)}")
        return 1

    print(f"All clear — {len(selected)} gate(s), 0 violations: {', '.join(selected)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
