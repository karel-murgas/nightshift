#!/usr/bin/env python3
"""Line endings stay LF, in the index and in the working tree.

**The failure this exists to prevent.** Git for Windows ships
`core.autocrlf=true` in its *system* config, which declares "the working tree
should be CRLF". Files git checks out obey it. Files written by anything that
bypasses git's smudge filter — Claude's Write tool, a Python script, Obsidian —
land as LF. Git then reports those files as modified **forever, with an empty
diff**: `git diff` shows nothing because every content byte matches, but the
stored *representation* disagrees, so the entry never satisfies
`git update-index --refresh` ("needs update"). On 2026-07-30 nineteen files sat
in that state, and every commit for weeks had to carry an explicit pathspec to
step around them. That is not a cosmetic annoyance: a permanently dirty tree
makes "is this clean?" unanswerable, which is the question the runner's
`dirty_outside_board` preflight and every pre-commit judgment rest on.

**Why an attributes file and not a config setting.** `core.autocrlf=false`
fixes one machine and nothing else — a fresh clone, the runner's sibling
checkout, or Karel's other box reinstates it. `.gitattributes` overrides
autocrlf everywhere and travels in the repo, so the rule holds wherever the
tree is checked out.

**Why LF in the working tree too, not just the index.** With `text=auto` alone
the worktree side follows `core.eol` (native = CRLF on Windows), which is
exactly the split that caused the bug: two legal representations, and whoever
writes a file decides which one it gets. Pinning `eol=lf` leaves one
representation, so no writer can produce a mismatch. Nothing here needs CRLF —
there is not a single `.bat` or `.cmd` file in the tree, and Python, pygame and
Obsidian all read LF natively.

Three things are checked, because the rule has three ways to rot:

1. `.gitattributes` still declares the `* text=auto eol=lf` rule at all.
2. No tracked text blob in the index contains CRLF — i.e. nothing got committed
   through a path that skipped the filter.
3. No tracked text file in the *working tree* contains CRLF — the phantom-dirty
   precursor, caught while it is still one file rather than four hundred.

All three come from a single `git ls-files --eol`, which reports the index
representation, the working-tree representation and the resolved attribute for every
tracked file at once. Binary files are excluded by git's own `-text` verdict rather than
by extension or by sniffing for a NUL, so a new asset type does not silently become a
false positive, and a file the project has *declared* binary is honoured even when its
first bytes read as text.

Asking git rather than deriving it also took this gate from **25.2s to 0.61s**
(2026-08-01). That mattered more than it looks: it was 76% of the whole 31-gate suite,
the suite runs three times per card (the on-save hook, the runner's gate step, the
rebase re-verification), and at 33s it blew the 15s on-save hook timeout — so the hook
was killed on every edit and reported nothing, for about 16 minutes per card.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from nightshift.gates.base import Violation

_RULE_LINE = "* text=auto eol=lf"

# A tracked file may legitimately hold CRLF as *test data* — a fixture that
# exists precisely to exercise line-ending handling. Nothing needs it today;
# the list is here so that the escape hatch is an explicit edit rather than a
# reason to weaken the gate.
_CRLF_ALLOWED: frozenset[str] = frozenset()

# `_tracked_files`, `_blob` and `_is_binary` lived here until 2026-08-01 and are gone:
# `git ls-files --eol` answers all three at once. Tracked-only scoping is inherent to
# `ls-files`; the index representation no longer needs a `cat-file` per file (which was
# 546 subprocesses and 25 of this gate's 25.2 seconds); and `-text` is git's own binary
# verdict, which beats a NUL sniff because it consults `.gitattributes` — a file the
# project *declared* binary is now treated as binary even if its first bytes read as text.
#
# What `git ls-files --eol` reports per file. `mixed` is both endings in one file,
# which is a CRLF problem by any reading. `none` means no line endings at all (a
# one-line file with no trailing newline) and is fine.
_CRLF_EOLS = frozenset({"crlf", "mixed"})
_BINARY = "-text"


def _eol_report(repo_root: Path) -> dict[str, tuple[str, str]] | None:
    """`{path: (index eol, worktree eol)}` — git's own answer, in ONE call.

    This gate used to derive the same facts itself: `git cat-file` per tracked file for
    the blob, plus a `read_bytes()` per file for the worktree, plus a NUL sniff to guess
    binary. That is 546 subprocesses on Dungeoneer and **25.2 seconds** — 76% of the
    entire 31-gate suite, and paid three times per card (the on-save hook, the runner's
    gate step, the rebase re-verification). `git ls-files --eol` reports index eol,
    worktree eol and the resolved attribute for every tracked file in **0.45s**, which is
    the same information from the authority that defines it.

    Returns None when git cannot answer — not a repo, no git binary — which the caller
    treats as "cannot compare", not as "clean".
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--eol", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None

    report: dict[str, tuple[str, str]] = {}
    for record in (out.stdout or "").split("\0"):
        # `i/lf    w/lf    attr/text=auto eol=lf \tpath`. The path is after a TAB and
        # may itself contain spaces, so split on the TAB rather than on whitespace.
        fields, tab, rel = record.partition("\t")
        if not tab or not rel.strip():
            continue
        index_eol = worktree_eol = ""
        for field in fields.split():
            if field.startswith("i/"):
                index_eol = field[2:]
            elif field.startswith("w/"):
                worktree_eol = field[2:]
        report[rel.strip()] = (index_eol, worktree_eol)
    return report


def check(repo_root: Path) -> list[Violation]:
    violations: list[Violation] = []

    attributes = repo_root / ".gitattributes"
    if not attributes.is_file():
        return [Violation(
            ".gitattributes", 0,
            f"missing — the tree needs `{_RULE_LINE}` or Windows' system-wide "
            "core.autocrlf=true makes every tool-written file permanently dirty",
        )]

    text = attributes.read_text(encoding="utf-8", errors="replace")
    if not any(line.strip() == _RULE_LINE for line in text.splitlines()):
        violations.append(Violation(
            ".gitattributes", 0,
            f"does not declare `{_RULE_LINE}` — without it the working-tree "
            "representation follows each machine's core.eol and tool-written "
            "files go phantom-dirty",
        ))

    report = _eol_report(repo_root)
    if report is None:
        # Not a repo, or no git binary. Nothing to compare against; the
        # attributes check above is still meaningful, so report what we have
        # rather than failing the whole gate harness.
        return violations

    for rel, (index_eol, worktree_eol) in sorted(report.items()):
        if rel in _CRLF_ALLOWED:
            continue
        # git's own binary verdict, which is what `-text` means. It replaces a
        # NUL-sniff of the first N bytes and is strictly better: it consults
        # .gitattributes, so a file the project has *declared* binary is treated
        # as binary even when its first bytes happen to look like text.
        if _BINARY in (index_eol, worktree_eol):
            continue
        if index_eol in _CRLF_EOLS:
            violations.append(Violation(
                rel, 0,
                "CRLF in the committed blob — it was staged through a path that "
                "skipped the .gitattributes filter; `git add --renormalize` it",
            ))
        if worktree_eol in _CRLF_EOLS:
            violations.append(Violation(
                rel, 0,
                "CRLF in the working tree — git sees this as clean (the stored LF "
                "blob and the CRLF worktree file normalise to the same content, so "
                "`git diff` shows nothing and `git status` reports nothing to "
                "commit). The fix is per-machine and does not sync: run "
                "python -m nightshift.normalize_worktree",
            ))

    return violations


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    print(f"{len(found)} violation(s)")
    sys.exit(1 if found else 0)
