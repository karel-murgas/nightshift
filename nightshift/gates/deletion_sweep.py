"""Gate: when a diff removes a source file or a top-level class/def, no doc may
still name it (08_staleness.md §3.4).

**This is the highest-value gate in that spec** (§3.4, §1d), and the reason is
about timing rather than coverage. `doc_reference_liveness` is cleanup for
staleness that already escaped — it tells you a doc is wrong months later, when
whoever caused it is long gone and the correct fix has to be reconstructed.
This one fires on the diff that kills the symbol, while the person who knows
what should replace it is still in the room.

It is also aimed at the *observed* failure. §1d: nobody forgets to document a
feature they just built — the enjoyable part carries its own documentation.
Deletions have no body, nothing announces them, and that is exactly how
`hack_scene.py` stayed documented as live for 124 days after `e2e2f51`
removed it.

Scope and exemptions come from `doc_scan.py`, so `doc_scope: history` files —
which are *supposed* to say "hack_scene.py was deleted" — never fail this.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from nightshift.gates import doc_scan
from nightshift import branches
from nightshift.gates.base import Violation

NAME = "deletion_sweep"
FAST = True
DESCRIPTION = "a removed file or top-level class/def must not still be named by any live doc"

_WATCHED_ROOT = "dungeoneer/"


def _git(repo_root: Path, args: list[str]) -> str:
    # `encoding=` is not optional on Windows. `text=True` alone decodes with the
    # locale codec — cp1252 here — and `dungeoneer/core/i18n.py` carries 245
    # bytes that codec has no mapping for, in the cs and es translations. The
    # decode then raises inside subprocess's *reader thread*, where `run()`
    # cannot see it, so `.stdout` comes back `None` rather than raising. That is
    # what failed `ice-damage` on 2026-07-23: a card whose work was complete was
    # blamed for a gate that crashed on its own output.
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    )
    return result.stdout or ""


def _merge_base(repo_root: Path) -> str:
    # `nightshift.branches` is the one place a branch name is bound to a role;
    # hardcoding the integration branch here is what this replaced.
    for base in branches.merge_base_candidates(repo_root):
        found = _git(repo_root, ["merge-base", "HEAD", base]).strip()
        if found:
            return found
    return ""


def _top_level_names(source: str) -> set[str]:
    # `ValueError` and `TypeError` alongside `SyntaxError`: a caller that hands
    # this `None` (a git read that failed) or bytes must get "no names found",
    # not a traceback that takes the whole gate run down. Catching only
    # `SyntaxError` is what turned one bad decode into `run.py` exiting non-zero
    # with its diagnosis on stderr, where the runner was not reading.
    if not isinstance(source, str):
        return set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError):
        return set()
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def removed_names(repo_root: Path) -> dict[str, str]:
    """{dead name -> why it is dead}, over the working tree and the branch diff.

    Two sources of death, both checked: a whole file going away (the file's
    basename plus every top-level class/def it defined), and a surviving file
    losing a top-level definition.
    """
    dead: dict[str, str] = {}
    base = _merge_base(repo_root)

    def note(name: str, reason: str) -> None:
        dead.setdefault(name, reason)

    def bury_file(path: str, old_source: str, reason: str) -> None:
        note(Path(path).name, reason)
        note(Path(path).stem, reason)
        for name in _top_level_names(old_source):
            note(name, reason)

    # 1. Working-tree deletions (what a pre-commit hook sees).
    for line in _git(repo_root, ["status", "--porcelain"]).splitlines():
        code, _, path = line[:2], line[2:3], line[3:].strip().strip('"')
        if "D" not in code or not path.startswith(_WATCHED_ROOT):
            continue
        old = _git(repo_root, ["show", f"HEAD:{path}"])
        bury_file(path, old, f"deleted from the working tree ({path})")

    if not base:
        return dead

    # 2. Committed deletions and per-file symbol removals since the merge-base.
    for line in _git(repo_root, ["diff", "--name-status", f"{base}..HEAD"]).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        if not path.startswith(_WATCHED_ROOT) or not path.endswith(".py"):
            continue
        if status.startswith("D"):
            bury_file(path, _git(repo_root, ["show", f"{base}:{path}"]),
                      f"deleted in this branch ({path})")
        elif status.startswith("M"):
            before = _top_level_names(_git(repo_root, ["show", f"{base}:{path}"]))
            after = _top_level_names((repo_root / path).read_text(encoding="utf-8", errors="replace"))
            for name in before - after:
                note(name, f"top-level definition removed from {path}")

    # A name that still exists somewhere else in the tree is not dead — a move
    # or a rename-in-place must not fire this gate.
    #
    # Resolution here is deliberately stricter than `doc_reference_liveness`'s:
    # it asks "is this still DEFINED anywhere", not "does this token appear
    # anywhere in the source". The looser test would clear every deletion whose
    # now-broken call sites have not been cleaned up yet — which is most of
    # them, mid-diff, which is precisely when this gate is supposed to fire.
    defined = doc_scan.symbol_table(repo_root)
    stems = doc_scan.file_stems(repo_root)
    return {
        name: reason
        for name, reason in dead.items()
        if name not in defined
        and name not in stems
        and not doc_scan.resolve_path(repo_root, name)
    }


def check(repo_root: Path) -> list[Violation]:
    dead = removed_names(repo_root)
    if not dead:
        return []

    patterns = {name: re.compile(rf"(?<![\w.]){re.escape(name)}(?![\w])") for name in dead}
    violations: list[Violation] = []

    for path in doc_scan.doc_files(repo_root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if doc_scan.is_exempt_file(text):
            continue
        skip = doc_scan.exempt_lines(text)
        rel = doc_scan.relpath(path, repo_root)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if lineno in skip:
                continue
            for name, pattern in patterns.items():
                if pattern.search(line):
                    violations.append(
                        Violation(
                            rel,
                            lineno,
                            f"deletion_sweep: `{name}` is still named here but was "
                            f"{dead[name]} — correct the doc, relocate it to a "
                            f"doc_scope: history file, or mark the line "
                            f"<!-- stale-ok: reason -->",
                        )
                    )
    return violations


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent.parent.parent
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(root):
        print(violation)
