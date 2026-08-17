#!/usr/bin/env python3
"""Re-materialise CRLF working-tree files through git's eol=lf smudge filter.

A checkout that pre-dates `.gitattributes`'s `* text=auto eol=lf` keeps its
files on disk as CRLF.  Git sees them as clean -- the index is LF; CRLF->LF
normalises to the same blob, so `git diff` shows nothing and `git status`
reports nothing to commit.  That is exactly why the fix is per-machine and
does not sync: there is genuinely nothing to commit.

Run once per checkout that pre-dates the attributes landing.  A fresh clone
made after they landed materialises LF already and this script is a no-op.

The mechanism (from the interactive session of 2026-07-31):

1. `git checkout-index -f` silently no-ops on files that already exist,
   so deleting the CRLF files first is the load-bearing step.
2. `git checkout-index -f --stdin -z` then re-materialises exactly those
   files through the smudge filter, which rewrites CRLF -> LF per `eol=lf`.
   Scoped to the deleted paths, never `-a`: `-a` rewrites every tracked file
   from the index and so reverts anything with genuine uncommitted edits.
3. `git add -u -- <those paths>` clears the stale stat cache; without it
   `git status` still shows the files as modified with an empty diff. Also
   scoped, so the script never stages someone else's work in progress.

Safety: only files whose working-tree content, after CRLF->LF normalisation,
is byte-identical to the index blob are touched.  A file with real uncommitted
content changes is skipped, preserved, and reported; the run exits non-zero so
the remainder is surfaced rather than silently left behind.

Targets are derived from `nightshift.gates.line_endings.check` so the binary-
sniffing and `_CRLF_ALLOWED` rules cannot drift into two implementations.

Moved into the package at 07_portability.md §8 step 5. It had lived in the origin
project's `.ai/`, which meant `line_endings` -- a core gate -- told every consuming
repo to run a file only that one repo had. A gate whose remedy does not exist is a
gate that gets muted, and this one fires once per CRLF file.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _find_repo_root() -> Path:
    from nightshift.manifest import find_root
    return find_root()


def worktree_crlf_files(repo: Path) -> list[str]:
    """The working-tree CRLF files the gate reports, by relative path.

    Delegates entirely to `nightshift.gates.line_endings.check` so the binary-
    sniffing and `_CRLF_ALLOWED` allowlist rules have one owner.

    Public because `board.commit_board` asks it first: normalising is cheap only
    when it is a no-op, and this is the one question that decides that.
    """
    import nightshift.gates.line_endings as _le
    return [v.file for v in _le.check(repo) if "working tree" in v.rule]


def _check_tracked(repo: Path, rel: str) -> bool:
    """True if the path is tracked by git (defence-in-depth; check() already
    filters to tracked files, but an explicit verification before any delete
    is the safe habit)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", rel],
        capture_output=True, encoding="utf-8",
    )
    return result.returncode == 0


def _index_bytes(repo: Path, rel: str) -> bytes | None:
    """The raw bytes stored in the index for *rel* (unfiltered cat-file).

    `cat-file blob :path` returns the stored blob without applying any text
    conversion -- what the index actually holds, which is what we compare
    against after normalising the worktree content.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f":{rel}"],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _is_content_identical(repo: Path, rel: str) -> bool:
    """True iff the worktree content, after CRLF->LF normalisation, equals the
    index blob.  A False means the file has real changes beyond CRLF encoding."""
    try:
        worktree = (repo / rel).read_bytes()
    except OSError:
        return False
    index = _index_bytes(repo, rel)
    if index is None:
        return False
    return worktree.replace(b"\r\n", b"\n") == index


def normalize(repo: Path, *, dry_run: bool = False) -> int:
    """Normalise the working tree.

    Returns 0 when the tree is clean (already or after the rewrite).
    Returns 1 when git commands fail or when CRLF violations remain (e.g.
    because some files had real content changes and were skipped).
    """
    targets_rel = worktree_crlf_files(repo)

    if not targets_rel:
        print("normalize_worktree: working tree is already clean -- nothing to do.")
        return 0

    safe: list[str] = []
    skipped: list[tuple[str, str]] = []

    for rel in targets_rel:
        if not _check_tracked(repo, rel):
            # Defence-in-depth: check() only returns tracked files, but verify
            # explicitly before touching anything.
            skipped.append((rel, "not tracked by git"))
            continue
        if not _is_content_identical(repo, rel):
            skipped.append((rel, "has uncommitted content changes -- preserved"))
            continue
        safe.append(rel)

    if dry_run:
        print(
            f"normalize_worktree: --dry-run: would rewrite {len(safe)} file(s):"
        )
        for rel in safe:
            print(f"  {rel}")
        if skipped:
            print(
                f"normalize_worktree: would skip {len(skipped)} file(s):"
            )
            for rel, reason in skipped:
                print(f"  {rel}: {reason}")
        return 0

    if safe:
        # Delete before checkout-index: the command silently no-ops on files
        # that already exist on disk, so the delete is what makes it re-smudge.
        for rel in safe:
            (repo / rel).unlink()

        # `--stdin -z` over exactly `safe`, NOT `-a`. `-a` re-materialises every
        # TRACKED file from the index, which silently reverts any file carrying
        # genuine uncommitted edits — including the ones this function just
        # classified as `skipped` and promised to preserve. Scoping the rewrite to
        # the paths that were deleted is what makes "preserved" true.
        # `--stdin` rather than argv: a pre-attributes checkout can have hundreds
        # of targets (304 on the host that motivated this card) and Windows caps a
        # command line at ~32k characters.
        result = subprocess.run(
            ["git", "-C", str(repo), "checkout-index", "-f", "--stdin", "-z"],
            input="".join(f"{rel}\0" for rel in safe),
            capture_output=True, encoding="utf-8",
        )
        if result.returncode != 0:
            print(
                f"normalize_worktree: ERROR: git checkout-index failed:\n"
                f"{result.stderr}",
                file=sys.stderr,
            )
            return 1

        # Also scoped: a bare `git add -u` stages every modified tracked file, so
        # it would sweep the skipped files' real edits into the index — not data
        # loss, but not this script's business either.
        result = subprocess.run(
            ["git", "-C", str(repo), "add", "-u", "--", *safe],
            capture_output=True, encoding="utf-8",
        )
        if result.returncode != 0:
            print(
                f"normalize_worktree: ERROR: git add -u failed:\n{result.stderr}",
                file=sys.stderr,
            )
            return 1

    if skipped:
        print("normalize_worktree: skipped files (preserved -- see reason below):")
        for rel, reason in skipped:
            print(f"  {rel}: {reason}")

    # Re-verify after the rewrite: exit non-zero if any violations remain so
    # the caller sees that not everything was fixed.
    remaining = worktree_crlf_files(repo)
    if remaining:
        print(
            f"normalize_worktree: ERROR: {len(remaining)} working-tree violation(s) "
            f"remain after normalisation:",
            file=sys.stderr,
        )
        for rel in remaining:
            print(f"  {rel}", file=sys.stderr)
        return 1

    if safe:
        print(
            f"normalize_worktree: rewrote {len(safe)} file(s); "
            f"working tree is now clean."
        )
    if skipped:
        print(
            f"normalize_worktree: {len(skipped)} file(s) skipped (see above); "
            f"re-run after committing or stashing those changes."
        )
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be rewritten, but change nothing.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repo root (default: auto-detected from the current directory).",
    )
    args = parser.parse_args()
    repo = args.repo if args.repo is not None else _find_repo_root()
    sys.exit(normalize(repo, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
