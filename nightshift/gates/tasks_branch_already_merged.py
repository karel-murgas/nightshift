"""Gate: a `tasks/` card whose own branch is already merged into the
integration branch is the shape an inline session leaves behind when it does
the work and then stops short of `manage-board`'s closing checklist — merge
run, lane move skipped.

`.ai/corrections.log`, `inline-session-stopped-before-its-own-closing-
checklist` (2026-08-26): working a card at the keyboard, a session implemented
the feature, ran gates and the full test suite, and reported "it's built and
tested" — then stopped. The card sat in `tasks/` with nothing committed,
nothing merged, and no `## Summary`, even though the session's own prompt
spelled out the checklist verbatim. Karel: *"It is done. Move it to testing.
Which you should have done when you thought it was ready."* The prose fix
landed as `feedback_inline_closing.md` (project memory) — "the maintainer
agrees the work is done" is now the documented cue to run the whole checklist
in the same turn. That is one more instruction in the exact place the failure
already proved instructions do not bind on their own, which is the argument
for this gate: a card can still be merged and left behind, and when it is,
the shape is mechanical — its branch is an ancestor of the integration branch,
and it is still sitting in `Board/tasks/`.

**Deliberately narrow to a real `git merge`, not the runner's rebase-and-land.**
`git branch --merged <integration>` asks "is this branch's tip an ancestor of
`<integration>`" — true for the plain `git merge` the interactive closing
checklist runs, false for the runner's rebase, whose landed commits are copies
of the branch's with new hashes, never the branch's own tip. That is not a gap
in this gate: a dispatched card is moved by `runner.settle()` in the same
transaction that merges it, so there is no "merged but still in `tasks/`"
state for that shape to leave behind, and a `branch:` gate hunting for one
there would only be reading its own false negative as a defect. What is left
to catch is exactly the interactive case this correction is about, where the
merge and the lane move are two separate actions a person can stop between.

A merged branch that still exists locally is itself part of the same skipped
checklist (`git branch -d`, the next step after the merge) — which is why
`git branch --merged` can still see it: the ordinary path deletes the branch
before anyone would think to look for it here.

**Scoped to `kind: inline` cards, checked against this project's own live
board (2026-09-23).** The unfiltered check found one real hit —
`icons-for-ice` (`kind: chore`, `worker: code-thread`) — and it is a false
positive of a shape the rebase argument above does not cover: attempt 2 cut
`ai/icons-for-ice` fresh from `test` after attempt 1's real work had already
landed and been reviewed ok, then failed before committing anything of its
own, so the branch's tip is `git merge-base ai/icons-for-ice test` itself —
never diverged, trivially "merged" in the vacuous sense. The runner reuses one
branch name across attempts (`branches.work_branch`), so a stalled retry can
land at exactly this shape at any time; a dispatched card is also always
matched to `settle()`, which moves it the moment a merge lands, so there is no
legitimate case in that lane for this gate to be finding. Filtering to the one
`kind` this correction is actually about removes the false positive and
leaves the check pointed at what a person, not the runner, can leave stalled.
"""
from __future__ import annotations

from pathlib import Path

from nightshift import board, branches, gitpaths
from nightshift.board import KIND_INLINE
from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError

NAME = "tasks_branch_already_merged"
DESCRIPTION = "an inline tasks/ card's own branch must not already be merged into the integration branch"


def _merged_branches(repo_root: Path, integration: str) -> set[str] | None:
    """Local branches that are ancestors of `integration`, or `None` on a git
    error (no such branch locally, not a git repo at all) — absence of an
    answer, not evidence of zero merges."""
    result = gitpaths.git(repo_root, "branch", "--format=%(refname:short)",
                          "--merged", integration)
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def check(repo_root: Path) -> list[Violation]:
    try:
        integration = branches.integration(repo_root)
    except ManifestError:
        return []  # branch roles undeclared — a project that has no opinion yet

    merged = _merged_branches(repo_root, integration)
    if merged is None:
        return []

    violations: list[Violation] = []
    for card in board.cards(repo_root, "tasks"):
        if card.kind != KIND_INLINE:
            continue  # the runner's own settle() moves a dispatched card on merge
        recorded = card.fields.get("branch", "")
        branch_name = branches.work_branch(card.id, recorded)
        if branch_name == integration or branch_name not in merged:
            continue
        rel = card.path.relative_to(repo_root).as_posix()
        violations.append(Violation(
            rel, 1,
            f"{NAME}: `{branch_name}` is already merged into `{integration}`, "
            f"but this card still sits in tasks/ — finish the closing checklist "
            f"(preflight, delete the branch locally and on the remote, write "
            f"`## Summary`, move the card to testing/ or done/), or correct "
            f"`branch:` if this merge was not this card's."
        ))
    return violations


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(find_root()):
        print(violation)
