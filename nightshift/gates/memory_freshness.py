"""Gate: a diff touching a declared source area must also touch its memory doc.

One row of `[memory].freshness` says "if the diff touches `touches`, it should have
touched `requires`". That is a co-change check, and its limits matter as much as
its coverage:

* **It proves attention, not accuracy** (`00_architecture.md` §14b). One unrelated
  line added to the doc turns it green over any amount of fiction. It is a nudge at
  the moment the author still knows the answer — the staleness sweep is what
  actually verifies content, and this must never be reported as staleness coverage.
* **Only rows checkable from a path.** "Phase completed → update the state doc" and
  "design decision changed → update the design doc" are judgment: you cannot tell
  from a diff whether a phase completed, only that a file changed. Those stay prose
  in `CLAUDE.md` and are deliberately absent here.

## Two diffs, unioned

The working tree (what a pre-commit-style hook sees) *and* the committed diff since
this branch's merge-base. Either alone has a blind spot — the first misses work
already committed on the branch, the second misses work not yet committed — and a
gate that runs both on save and in preflight has to be right in both contexts.
The merge-base comes from `nightshift.branches`, which degrades to the stable branch
in a repo that has not declared an integration branch.

## `when = "added"`

A row normally fires on any touch. `when = "added"` narrows it to files that are
*new* in this diff, which is the shape of an index rule: adding a
`feedback_<topic>.md` must update the index that lists them, while editing one need
not. Without the distinction the row is either missing or too loud, and too loud is
how a gate gets muted.

Portable since 07_portability.md §2 classified it as manifest-table — pure logic
over a project table. It stayed project-side one session too long, with a hardcoded
`_RULES` list contradicting the `[memory].freshness` schema the same document
specified.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nightshift import branches, gitpaths
from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError
from nightshift.manifest import load as load_manifest

NAME = "memory_freshness"
FAST = True
DESCRIPTION = "a diff touching a declared source area must also touch its memory doc"


def _git_output(repo_root: Path, args: list[str]) -> str:
    # `encoding=` is required — see the note in `deletion_sweep._git`. Without it
    # Windows decodes git's output as cp1252 and any non-mappable byte in a tracked
    # file makes `.stdout` `None` rather than raising.
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    )
    return result.stdout or ""


def _status_paths(repo_root: Path) -> list[tuple[str, str]]:
    """`(porcelain code, path)` for every working-tree change, renames resolved."""
    return gitpaths.status(repo_root)


def changed_files(repo_root: Path) -> set[str]:
    """Everything this diff touches: working tree ∪ committed-since-merge-base."""
    files = {path for _, path in _status_paths(repo_root)}
    for base in branches.merge_base_candidates(repo_root):
        merge_base = _git_output(repo_root, ["merge-base", "HEAD", base]).strip()
        if merge_base:
            files.update(gitpaths.changed(repo_root, f"{merge_base}..HEAD"))
            break
    return files


def added_files(repo_root: Path) -> set[str]:
    """Files new in this diff — staged adds and untracked, for `when = "added"`."""
    return {path for code, path in _status_paths(repo_root)
            if "A" in code or code.strip() == "??"}


def _matches(path: str, prefix: str) -> bool:
    """`touches` is a prefix, not necessarily a whole path.

    A trailing `/` names a directory and a bare stem names a filename prefix
    (`.claude/memory/feedback_`), which is what makes an index rule expressible.
    """
    normalised = path.replace("\\", "/")
    return normalised == prefix or normalised.startswith(prefix)


def check(repo_root: Path) -> list[Violation]:
    try:
        rules = load_manifest(repo_root).memory.freshness
    except ManifestError:
        return []
    if not rules:
        return []

    changed = changed_files(repo_root)
    if not changed:
        return []
    added: set[str] | None = None

    # Rows sharing a `touches` are alternatives, not separate obligations: a memory
    # doc may be split into an orientation file plus detail/history companions, and
    # logging a change in the history file satisfies the rule just as well. Demanding
    # the orientation file every time teaches people to make a no-op edit to it.
    accepted: dict[tuple[str, str], list[str]] = {}
    for rule in rules:
        accepted.setdefault((rule.touches, rule.when), []).append(rule.requires)

    violations: list[Violation] = []
    for (prefix, when), wanted in accepted.items():
        if when == "added":
            if added is None:
                added = added_files(repo_root)
            pool = added
        else:
            pool = changed
        touched = sorted(f for f in pool if _matches(f, prefix))
        if not touched or any(path in changed for path in wanted):
            continue
        violations.append(Violation(
            touched[0], 0,
            f"{NAME}: this diff {'adds' if when == 'added' else 'touches'} "
            f"{prefix} but does not touch {' or '.join(wanted)} — "
            f"`[memory].freshness` says that doc is supposed to move with it. "
            f"Update it now, while you still know what changed.",
        ))
    return sorted(violations, key=lambda v: v.file)


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
