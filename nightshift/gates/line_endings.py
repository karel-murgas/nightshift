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

Binary files are excluded by content (a NUL in the first 8 kB), not by
extension, so a new asset type does not silently become a false positive.
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

_BINARY_SNIFF_BYTES = 8000


def _tracked_files(repo_root: Path) -> list[str] | None:
    """Paths git tracks, relative to the repo root. None if git cannot answer.

    Tracked only, deliberately: an untracked scratch file's line endings are
    nobody's business until it is committed, and flagging them would make the
    gate fire on work in progress.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, text=True, timeout=60,
            # Explicit, not the locale codec — a filename byte cp1252 cannot
            # map would otherwise make .stdout None rather than raise, and the
            # gate would report "git cannot answer" and check nothing.
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.split("\0") if p]


def _blob(repo_root: Path, rel: str) -> bytes | None:
    """The staged bytes for `rel`, unfiltered. `cat-file`, not `show`: `git show
    :path` applies text conversion, which would hand back exactly the
    normalised view this gate is trying to see past.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "blob", f":{rel}"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:_BINARY_SNIFF_BYTES]


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

    tracked = _tracked_files(repo_root)
    if tracked is None:
        # Not a repo, or no git binary. Nothing to compare against; the
        # attributes check above is still meaningful, so report what we have
        # rather than failing the whole gate harness.
        return violations

    for rel in tracked:
        if rel in _CRLF_ALLOWED:
            continue

        blob = _blob(repo_root, rel)
        if blob is not None and not _is_binary(blob) and b"\r\n" in blob:
            violations.append(Violation(
                rel, 0,
                "CRLF in the committed blob — it was staged through a path that "
                "skipped the .gitattributes filter; `git add --renormalize` it",
            ))

        path = repo_root / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue  # deleted or unreadable in this checkout; not this gate's call
        if not _is_binary(data) and b"\r\n" in data:
            violations.append(Violation(
                rel, 0,
                "CRLF in the working tree — git wants LF here, so this file will "
                "report as modified with an empty diff until it is rewritten",
            ))

    return violations


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    print(f"{len(found)} violation(s)")
    sys.exit(1 if found else 0)
