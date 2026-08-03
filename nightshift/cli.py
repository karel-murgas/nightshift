#!/usr/bin/env python3
"""The `nightshift` command — `init` and `doctor`, and deliberately nothing else.

**Why only these three.** Every other entry point is already `python -m nightshift.<module>`,
and adding `nightshift gates` beside `python -m nightshift.gates.run` would give each
command two names. Two names is how a doc, a hook and a habit end up citing three
different spellings of the same thing, and it is the D4 duplication this framework
spends most of its gates preventing. These three earn the exception for the same
reason: `init` runs in a repo that does not have the package configured yet,
`uninstall` is what you reach for when `init` went wrong (and a half-configured repo
cannot be fixed by re-running `init`, because it never overwrites), and `doctor` is
the thing you reach for when you do not yet know what is wrong. All three are worse
to have to remember a module path for.

Each subcommand delegates to the module that owns it and passes the remaining
argv straight through, so `nightshift init --help` and `python -m nightshift.init
--help` print the same thing because they *are* the same parser.
"""
from __future__ import annotations

import sys

_USAGE = """\
usage: nightshift <command> [options]

  init       stand nightshift up in this repo — asks four questions, then writes
  doctor     per-machine preconditions, plus drift between the manifest and the tree
  uninstall  remove what init wrote, so a first install can be retried

Everything else is a module, and stays one:

  python -m nightshift.gates.run       the gate suite
  python -m nightshift.preflight       mandatory before push/merge
  python -m nightshift.runner          dispatch cards from the board
  python -m nightshift.reconcile       inbox notes -> cards
  python -m nightshift.digest          the morning report
  python -m nightshift.merge_check     does this branch merge clean, gated, tested?
  python -m nightshift.corrections     read and cluster the corrections log
"""


def main(argv: list[str] | None = None) -> int:
    # Before anything prints. This tree's prose carries em-dashes and arrows, and a
    # console defaulting to cp1252 mangles the first line of the first command a new
    # user runs — which reads as "this tool is broken" rather than "my terminal is".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_USAGE, end="")
        return 0
    command, rest = args[0], args[1:]

    if command == "init":
        from nightshift import init

        return init.main(rest)
    if command == "doctor":
        from nightshift import doctor

        return doctor.main(rest)
    if command == "uninstall":
        from nightshift import uninstall

        return uninstall.main(rest)

    print(f"unknown command: {command}\n", file=sys.stderr)
    print(_USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
