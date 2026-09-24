#!/usr/bin/env python3
"""Gate: every tracked text file ends with a trailing newline.

**What this is not an upgrade of.** `write_newline` is a static AST scan over the
tooling's own source, looking for a `.write_text()` / `open(..., "w")` call that
omits `newline=` — a *producer* check, about code that could corrupt a file's
representation on write. It says nothing about whether a file on disk right now
ends with a newline. No gate checked that before this one existed (measured
2026-09-18, the hygiene-rules-belong-in-a-script card).

**Why this exists as its own gate rather than folded into `line_endings`.**
Different failure, different fix, same "exactly one correct output" shape that
makes it worth automating rather than asking a worker to notice: a missing
trailing newline is an *append*, never a rewrite of existing bytes, so it carries
none of `line_endings.fix()`'s "is this lossless here" reasoning — appending a
single `\n` cannot clobber a real edit whatever the file's content is.

Scope matches `line_endings`: every tracked, non-binary file in the tree, not
just `[project].tooling_dirs` (`write_newline`'s scope) — this is a mechanical,
project-wide hygiene rule, not a rule about the orchestrator's own source.

An empty file (0 bytes) is not flagged. There is no line to leave unterminated,
and appending a lone `\n` to nothing would manufacture content a fix has no
business inventing.

Text/binary is git's own verdict (`git ls-files --eol`'s `-text` marker), shared
with `line_endings.eol_report` rather than re-derived — see that function's
docstring.
"""
from __future__ import annotations

SUBJECT = ".gitattributes"

import sys
from pathlib import Path

import line_endings
from nightshift.gates.base import Violation

NAME = "trailing_newline"
DESCRIPTION = "every tracked text file ends with a trailing newline"


def check(repo_root: Path) -> list[Violation]:
    report = line_endings.eol_report(repo_root)
    if report is None:
        # Not a repo, or no git binary — nothing tracked to check.
        return []

    violations: list[Violation] = []
    for rel in sorted(report):
        index_eol, worktree_eol = report[rel]
        if line_endings.BINARY in (index_eol, worktree_eol):
            continue
        path = repo_root / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not data or data.endswith(b"\n"):
            continue
        violations.append(Violation(rel, 0, "missing trailing newline"))
    return violations


def fix(repo_root: Path) -> list[str]:
    """Append exactly one `\\n` to every file `check` flags. An append, never a
    rewrite of existing bytes — see the module docstring. Returns the paths fixed."""
    fixed: list[str] = []
    for violation in check(repo_root):
        path = repo_root / violation.file
        try:
            data = path.read_bytes()
        except OSError:
            continue
        path.write_bytes(data + b"\n")
        fixed.append(violation.file)
    return fixed


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    print(f"{len(found)} violation(s)")
    sys.exit(1 if found else 0)
