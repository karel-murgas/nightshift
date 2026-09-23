"""Landing a card: the one path from a finished branch to the card's final lane.

`land` merges a card's work into the integration branch, folds the card's memory
fragment, deletes its branch (locally, and on the publish remote), and moves the card
to its lane — the move commits the board. Every path by which a card branch reaches the
integration branch goes through here: the runner's `settle` (and `drain`, through it),
the chore batch (`land_batch`), and an inline session (`python -m nightshift.boardcmd
land <id>`). No other module merges into the integration branch;
`tests/test_landing.py` scans the package and fails on a new site.

Why one function: the post-merge duties used to live at the runner's two merge sites
only, and the chore batch — which "called the merge" — skipped the fold
(`chore-landing-skipped-the-memory-fold`).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nightshift import board, git, gitmerge, memoryfold
from nightshift.manifest import ManifestError
from nightshift.manifest import load as _load_manifest

__all__ = ["Plan", "land", "land_batch", "finished_lane", "delete_remote_branch",
           "set_log"]


@dataclass(frozen=True)
class Plan:
    """Where a landed card goes, and what is written onto it just before it moves.

    `before_move` runs only once the merge has succeeded, so a section like
    `## How to test` is never written onto a card that did not land.
    """
    lane: str
    before_move: Callable[[board.Card], None] | None = None


def _default_log(message: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {message}", flush=True)


_sink: Callable[[str], None] = _default_log


def set_log(sink: Callable[[str], None]) -> None:
    """Route this module's log lines through `sink` (the runner tees them to its run log)."""
    global _sink
    _sink = sink


def _log(message: str) -> None:
    _sink(message)


def _current_branch(root: Path) -> str:
    return git.run(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _is_ancestor(root: Path, ancestor: str, ref: str) -> bool:
    return git.run(root, "merge-base", "--is-ancestor", ancestor, ref).returncode == 0


# --------------------------------------------------------------------------- the lane


def player_visible_hit(root: Path, branch: str, base: str) -> str:
    """The first path `branch` changed (since it forked from `base`) under a declared
    `[board].player_visible_paths` prefix, or `""`.
    """
    try:
        prefixes = _load_manifest(root).board.player_visible_paths
    except ManifestError:
        return ""
    if not prefixes:
        return ""
    for changed in git.changed(root, f"{base}...{branch}"):
        if any(changed == p or changed.startswith(p.rstrip("/") + "/") for p in prefixes):
            return changed
    return ""


def finished_lane(root: Path, card: board.Card, branch: str, base: str) -> str:
    """The lane a landed card belongs in: `board.finished_lane(card)`, except that a
    card whose diff touches a declared player-visible path goes to `testing/` whatever
    its `verify:` says — `done/` would claim the maintainer saw something nobody ran
    (`inline-route-assumed-karel-was-the-author`). Replaces the tree gate
    `player_visible_skipped_testing`: the transition is refused where it happens.
    """
    lane = board.finished_lane(card)
    if lane == "done" and player_visible_hit(root, branch, base):
        return "testing"
    return lane


# --------------------------------------------------------------------------- merge


def _merge(root: Path, ref: str, base: str, *, label: str = "", message: str = "",
           ff_only: bool = False) -> tuple[bool, str]:
    """Merge `ref` into `base`, which `root` must have checked out. The only
    `git merge` into the integration branch in this package.

    A merge that does not apply is aborted and reported, never left half-applied.
    `ff_only` is for a merge commit already built and verified elsewhere.
    """
    name = label or ref
    head = _current_branch(root)
    if head != base:
        return False, f"the checkout is on `{head}`, not the integration branch `{base}`"
    if git.run(root, "rev-parse", "--verify", ref).returncode != 0:
        return False, f"`{name}` no longer exists"
    if ff_only:
        done = git.run(root, "merge", "--ff-only", ref)
        if done.returncode == 0:
            return True, "fast-forwarded"
        return False, (done.stderr or done.stdout or "").strip()[:150]
    merged = git.run(root, "merge", *gitmerge.STRATEGY_ARGS, "--no-ff", "-m",
                  message or f"merge {name}: reviewed ok by the runner", ref)
    if merged.returncode == 0:
        return True, "merged"
    if git.run(root, "merge", "--abort").returncode != 0:
        # Expected when git refused before starting: there is no merge to abort.
        _log(f"  ! merge --abort of {name} found no merge in progress — git refused the "
             f"merge before starting it; the checkout is untouched")
    return False, gitmerge.failure_detail(merged)


# --------------------------------------------------------------------------- after the merge


def fold_memory(root: Path, card_id: str, base: str) -> None:
    """Fold this card's memory fragment into the shared logs and commit it.

    Every failure is logged and swallowed: the work is already on `base`, and a
    fragment that will not fold stays on disk for `python -m nightshift.memoryfold`.
    """
    if not memoryfold.targets(root):
        return
    fragment = memoryfold.fragment_path(root, card_id)
    if not fragment.is_file():
        return
    head = _current_branch(root)
    if head != base:
        _log(f"  ! merged {card_id} but did not fold its memory record — the checkout "
             f"is on `{head}`, not `{base}`; run `python -m nightshift.memoryfold`")
        return
    try:
        report = memoryfold.fold(root, card_id=card_id)
    except OSError as exc:
        _log(f"  ! merged {card_id} but its memory fragment would not fold — {exc}")
        return
    for line in report:
        _log(f"    {line}")
    if any("LEFT IN PLACE" in line for line in report):
        return
    paths = [str(root / target.path) for target in memoryfold.targets(root)]
    added = git.run(root, "add", "--", str(fragment), *paths)
    if added.returncode != 0:
        _log(f"  ! folded {card_id}'s memory record but could not stage it — "
             f"{(added.stderr or added.stdout or '').strip()[:150]}")
        return
    committed = git.run(root, "commit", "-m", f"memory: fold {card_id}'s record",
                     "--", str(fragment), *paths)
    if committed.returncode != 0:
        _log(f"  ! folded {card_id}'s memory record but could not commit it — "
             f"{(committed.stderr or committed.stdout or '').strip()[:150]}")


def delete_remote_branch(root: Path, remote: str, branch: str, *,
                         action: str = "merged") -> None:
    """Delete `branch` on `remote` once its work has landed or been reaped.

    Call while `branch` still exists locally: the guard is that the remote's tip must
    be an ancestor of the local tip, so a remote carrying commits this checkout never
    saw (pushed from elsewhere) is left alone and logged. A no-op with no `remote`, an
    unconfigured one, or a remote without the branch. Failures are logged, not raised.
    """
    if not remote:
        return
    if git.run(root, "remote", "get-url", remote).returncode != 0:
        return
    if git.run(root, "fetch", remote, branch).returncode != 0:
        return  # never published — the ordinary case, and nothing to report
    remote_tip = git.run(root, "rev-parse", "FETCH_HEAD").stdout.strip()
    if not remote_tip or not _is_ancestor(root, remote_tip, branch):
        _log(f"  ! {action} {branch} but did NOT delete it on {remote} — the remote "
             f"carries commits this checkout does not have (pushed from elsewhere "
             f"since the last fetch); deleting would destroy them. Reconcile "
             f"`{remote}/{branch}` by hand.")
        return
    deleted = git.run(root, "push", remote, "--delete", branch)
    if deleted.returncode != 0:
        detail = (deleted.stderr or deleted.stdout or "").strip().splitlines()
        _log(f"  ! {action} {branch} but could not delete it on {remote} — "
             f"{detail[-1][:150] if detail else 'see git output'}")


def _delete_local_branch(root: Path, branch: str, *, force: bool) -> None:
    """`-D` only when the caller landed a rebased copy (the branch's own tip is then
    never an ancestor of the base); `-d` everywhere else, which refuses an unmerged
    branch."""
    if git.run(root, "rev-parse", "--verify", f"refs/heads/{branch}").returncode != 0:
        return
    deleted = git.run(root, "branch", "-D" if force else "-d", branch)
    if deleted.returncode != 0:
        _log(f"  ! merged {branch} but could not delete it — "
             f"{(deleted.stderr or deleted.stdout or '').strip()[:150]}")


def _finish(root: Path, card: board.Card, *, branch: str, base: str, remote: str,
            plan: Plan | None, force_delete: bool) -> None:
    """Everything after the merge: fold, branch cleanup, lane move (which commits)."""
    fold_memory(root, card.id, base)
    if branch:
        delete_remote_branch(root, remote, branch)
        _delete_local_branch(root, branch, force=force_delete)
    if plan is None:
        return
    if plan.before_move is not None:
        plan.before_move(card)
    board.move(root, card, plan.lane)


# --------------------------------------------------------------------------- the entry points


def land(root: Path, card: board.Card, *, branch: str, base: str, plan: Plan | None,
         remote: str = "", ref: str = "", label: str = "", message: str = "",
         ff_only: bool = False, force_delete: bool = False,
         already_merged: bool = False) -> tuple[bool, str]:
    """Merge `ref` (default: `branch`) into `base`, then fold, delete `branch`, and move
    the card per `plan`. Returns `(landed, detail)`; on `False` nothing was touched.

    `plan=None` skips the lane move — for a caller holding a card that is not on the
    board. `already_merged` skips the merge for work that reached `base` by hand.
    """
    if already_merged:
        ok, why = True, "already merged"
    else:
        ok, why = _merge(root, ref or branch, base, label=label or branch,
                         message=message, ff_only=ff_only)
    if not ok:
        return False, why
    _finish(root, card, branch=branch, base=base, remote=remote, plan=plan,
            force_delete=force_delete)
    return True, why


def land_batch(root: Path, ref: str, base: str,
               cards: list[tuple[board.Card, str, Plan]], *, remote: str = "",
               label: str = "") -> tuple[bool, str]:
    """Merge one batch branch carrying several cards' work, then finish each card
    exactly as `land` would. Returns `(landed, detail)`."""
    ok, why = _merge(root, ref, base, label=label or ref)
    if not ok:
        return False, why
    for card, branch, plan in cards:
        _finish(root, card, branch=branch, base=base, remote=remote, plan=plan,
                force_delete=False)
    return True, why
