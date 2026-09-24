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
deleted it.

Scope and exemptions come from `doc_scan.py`, so `doc_scope: history` files —
which are *supposed* to say "hack_scene.py was deleted" — never fail this.

**Also flags a project-local gate whose own `SUBJECT` no longer resolves**
(`correction-loop-defaults`, 2026-09-23 framework review, part 2). Every gate now
declares `SUBJECT` — the path, module or field it guards — precisely so this
question can be asked mechanically instead of discovered the way `scope.py`'s own
docstring describes: four discipline gates pointed at a directory that had moved,
silently checking nothing, until someone measured it.

**Scoped to a project's own `.ai/gates/`, not core.** A core gate's `SUBJECT` has
to mean something across every consuming repo, including this package's own
checkout, which has no `Board/` and no game source — checking core subjects
against an arbitrary `repo_root` would flag `card_schema`'s `SUBJECT = "Board"` as
"deleted" in a repo that never had one, which is a different failure from drift.
A project gate is different: it was written *because* something in that project's
own tree existed, so a `SUBJECT` that no longer resolves there is exactly the
drift this gate exists to catch — the same one line up move for gates that
`doc_reference_liveness` already makes for prose.
"""
from __future__ import annotations

SUBJECT = "nightshift/gates/doc_scan.py"

import ast
import re
from pathlib import Path

from nightshift.gates import doc_scan
from nightshift.gates import run as gates_run
from nightshift import branches, git
from nightshift.gates.base import Violation
from nightshift.manifest import AI_DIR

NAME = "deletion_sweep"
DESCRIPTION = "a removed file or top-level class/def must not still be named by any live doc"


def _watched_roots(repo_root: Path) -> tuple[str, ...]:
    """Path prefixes whose deletions this gate cares about.

    Was the literal `"dungeoneer/"`, which made the gate a permanent no-op in every
    repo but the one it was written in — silently, because a `startswith` filter
    that matches nothing produces no violations rather than an error. Measured
    2026-08-02: deleting a documented module in a second project while the doc
    still named it reported "All clear".

    `doc_scan.source_dirs` is the same table the rest of this family already reads,
    so the three doc gates now agree about what "the source tree" means instead of
    two of them agreeing and one holding a private answer.
    """
    return tuple(f"{d.rstrip('/')}/" for d in doc_scan.source_dirs(repo_root))


def _merge_base(repo_root: Path) -> str:
    # `nightshift.branches` is the one place a branch name is bound to a role;
    # hardcoding the integration branch here is what this replaced.
    for base in branches.merge_base_candidates(repo_root):
        found = (git.run(repo_root, "merge-base", "HEAD", base).stdout or "").strip()
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
    watched = _watched_roots(repo_root)

    def note(name: str, reason: str) -> None:
        dead.setdefault(name, reason)

    def bury_file(path: str, old_source: str, reason: str) -> None:
        note(Path(path).name, reason)
        note(Path(path).stem, reason)
        for name in _top_level_names(old_source):
            note(name, reason)

    # 1. Working-tree deletions (what a pre-commit hook sees).
    for code, path in git.status(repo_root):
        if "D" not in code or not path.startswith(watched):
            continue
        old = (git.run(repo_root, "show", f"HEAD:{path}").stdout or "")
        bury_file(path, old, f"deleted from the working tree ({path})")

    if not base:
        return dead

    # 2. Committed deletions and per-file symbol removals since the merge-base.
    for status, path in git.name_status(repo_root, f"{base}..HEAD"):
        if not path.startswith(watched) or not path.endswith(".py"):
            continue
        if status.startswith("D"):
            bury_file(path, (git.run(repo_root, "show", f"{base}:{path}").stdout or ""),
                      f"deleted in this branch ({path})")
        elif status.startswith("M") and (repo_root / path).is_file():
            # `is_file()` because a path modified in the committed range can be
            # *gone from disk* — a branch that edited a file and has since deleted
            # it, which is the normal mid-change state of any removal. Step 1 has
            # already buried it as a working-tree deletion, so there is nothing to
            # add here; without the guard the `read_text` raised FileNotFoundError
            # and took the whole gate run down with a traceback rather than
            # reporting anything. Observed 2026-08-17, splitting the since-retired
            # `test_runner.py` into three modules: 23 gates stopped running because one file was
            # `git rm`-ed while its edits were still in the branch.
            before = _top_level_names((git.run(repo_root, "show", f"{base}:{path}").stdout or ""))
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


def _pattern(name: str) -> re.Pattern[str]:
    """A plain-word name (`night`, `panel`) is matched only where prose writes it as
    code — backticked or after a dot — because the same word in a sentence is not a
    reference. Anything identifier-shaped (`hack_scene`, `GameApp`, or a bare
    *.py* filename) is matched bare, as before."""
    if name.isalpha() and name.islower():
        return re.compile(rf"`{re.escape(name)}`|(?<=\.){re.escape(name)}(?![\w])")
    return re.compile(rf"(?<![\w.]){re.escape(name)}(?![\w])")


def _project_gate_subjects(repo_root: Path) -> dict[str, tuple[str, ...]]:
    """`{gate name: SUBJECT paths}`, for gates that live in this project's own
    `.ai/gates/` — never core, see the module docstring for why."""
    project_dir = (repo_root / AI_DIR / "gates").resolve()
    if not project_dir.is_dir():
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for name, module in gates_run.discover(repo_root).items():
        origin = getattr(module, "__file__", None)
        if not origin or Path(origin).resolve().parent != project_dir:
            continue
        subject = getattr(module, "SUBJECT", None)
        if subject is None:
            continue
        out[name] = (subject,) if isinstance(subject, str) else tuple(subject)
    return out


def stale_subjects(repo_root: Path) -> list[Violation]:
    """A project gate's `SUBJECT` naming a path that does not exist in this
    tree — reported against the gate file itself, not the missing path (the
    gate is what needs fixing or appealing; the path may be gone on purpose)."""
    violations: list[Violation] = []
    for name, subjects in sorted(_project_gate_subjects(repo_root).items()):
        rel_gate = f"{AI_DIR}/gates/{name}.py"
        for subject in subjects:
            if (repo_root / subject).exists():
                continue
            violations.append(Violation(
                rel_gate, 1,
                f"deletion_sweep: SUBJECT names {subject!r}, which no longer exists in "
                f"this tree — update SUBJECT to what the gate now inspects, or retire "
                f"the gate (turn-a-correction-into-a-gate.md's retirement review) if "
                f"its subject is gone for good.",
            ))
    return violations


def check(repo_root: Path) -> list[Violation]:
    violations = stale_subjects(repo_root)
    dead = removed_names(repo_root)
    if not dead:
        return violations

    patterns = {name: _pattern(name) for name in dead}

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
