"""Worktree lifecycle: creation, the integration checkout, warm resume,
rescue branches, run-dir/worktree pruning, and pushing a night's branches to a
remote.

`worktree_root`/`_worktree_add`/`prepare_worktree` create and register a
card's worktree (cold or resumed via `Handover`); `ensure_integration_checkout`
owns the dedicated `base` checkout dispatch never touches directly;
`prune_run_dir`/`cap_rescue_branches`/`enforce_worktree_ceiling` and friends
keep both bounded; `publish` pushes `base` and live `ai/<id>` branches to a
configured remote. Imports only `hostconfig`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from nightshift import board, branches, git, gitmerge, landing, textio
from nightshift import manifest as _manifest
from nightshift import hostconfig

# Worktrees
# --------------------------------------------------------------------------

def worktree_root(root: Path) -> Path:
    """Outside the repo, deliberately.

    A worktree under `Board/`'s repo would be walked by `doc_scan`,
    `card_schema` and the digest, and every card would appear N+1 times. Putting
    it beside the repo costs one `..` and removes a whole class of confusion.
    """
    return root.parent / _manifest.sibling_dir_name(hostconfig._project_name(root), "worktrees")


# --------------------------------------------------------------------------
# The Windows long-path backstop (nightshift-worktree-paths-not-defensive-on-
# windows) — every `git worktree add` call site in this module and in
# `merge_check.check_branch` routes through `_worktree_add` below.
# --------------------------------------------------------------------------

# Quoted verbatim from the 2026-08-06 sweep recorded on the card. Both are
# what `MAX_PATH` looks like from the outside: a git exit code and a message
# that reads like a bug in git, not like "this path is 260 characters".
_LONG_PATH_PATTERNS = (
    re.compile(r"unable to create file .*: Filename too long"),
    re.compile(r"'\$GIT_DIR' too big"),
)


class WorktreePathTooLong(RuntimeError):
    """`git worktree add` failed in one of the two shapes the sweep on
    `nightshift-worktree-paths-not-defensive-on-windows` produced — Windows'
    260-char `MAX_PATH`, not a bug in git or in this checkout. Callers that
    already raise on a worktree-add failure need no further handling: this is
    already a `RuntimeError`. Callers that degrade gracefully (`review_branch`,
    the rebase merge, `merge_check.check_branch`) catch this specifically and
    fold its message into their existing failure path.
    """


def _worktree_add(root: Path, *args: str) -> subprocess.CompletedProcess:
    """`git worktree add …`, with the Windows long-path failure re-reported.

    On any *other* failure this is exactly `git.run(root, "worktree", "add",
    *args)` — the `CompletedProcess` comes back unexamined, and each caller
    keeps its own existing handling (raise here, log-and-return there). Only
    the two stderr shapes above raise, and they raise with the doctor's own
    three numbers attached — `worktree_root`'s length, the worst worktree name
    this framework generates today, and the worst relative path this checkout
    can hold — plus the remedies, so the operator sees those instead of a bare
    git exit code.

    Deliberately not `core.longpaths`. `nightshift.doctor`'s module docstring
    and `nightshift-worktree-paths-not-defensive-on-windows` record why: it
    makes git succeed and then Python fail to open a file that visibly exists
    on disk (`FileNotFoundError`), and at greater checkout depth git fails
    anyway with a *worse* message (`'$GIT_DIR' too big`) — strictly worse than
    doing nothing. Do not add it here; that is exactly the "obvious fix" a
    future reader will want.
    """
    result = git.run(root, "worktree", "add", *args)
    if result.returncode == 0:
        return result
    stderr = f"{result.stderr or ''}\n{result.stdout or ''}"
    if not any(pattern.search(stderr) for pattern in _LONG_PATH_PATTERNS):
        return result

    from nightshift import doctor  # deferred: doctor imports this module

    wt_root_len = len(str(worktree_root(root)))
    name_len, name = doctor.worst_worktree_name(root)
    rel_len, rel = doctor.worst_relative(root)
    slack, _ = doctor.headroom(wt_root_len, name_len, rel_len)
    raise WorktreePathTooLong(
        f"git worktree add hit what looks like Windows' MAX_PATH (260 chars): "
        f"worktree root {wt_root_len} chars + worktree name {name_len} "
        f"({name!r}) + longest path {rel_len} ({rel!r}) — {slack} chars of "
        f"slack by `nightshift doctor`'s count. Remedies: enable "
        f"LongPathsEnabled (admin, the real fix), move this checkout nearer "
        f"the drive root, or `subst`/`mklink /J` a short alias for it — not "
        f"`core.longpaths` (see this function's docstring). git said: "
        f"{(result.stderr or result.stdout or '').strip()[:200]}")


# --------------------------------------------------------------------------
# The dedicated integration checkout (runner-hardening #3)
# --------------------------------------------------------------------------

def integration_checkout_path(root: Path) -> Path:
    """Where the runner's own `development_team` worktree lives — a sibling of the
    launch checkout, next to the per-card worktrees. A fixed name (not per-card):
    there is exactly one, permanently on the integration branch."""
    return root.parent / hostconfig.integration_checkout_dir(root)


def ensure_integration_checkout(root: Path, base: str) -> Path:
    """The runner's dedicated `base` checkout, created if absent, returned either way.

    `root` is the *launch* checkout, which the caller has already established is
    **not** on `base` (git forbids one branch in two worktrees, so this only runs
    when Karel has moved his checkout to his own branch). A sibling worktree is cut
    on `base` and reused on later nights — it is long-lived, the one place the
    integration branch is checked out, and the only writer of the board and of
    merges into `base`.

    Robust to the states a previous night can leave: an already-registered worktree
    on `base` is reused as-is; one on the wrong branch is switched back if clean;
    a leftover directory that git no longer tracks (a crash pruned the registration)
    is cleared and recut. Raises `RuntimeError` — never guesses — when it cannot
    reach a clean checkout on `base`, so `run()` can refuse with the reason rather
    than dispatch against a broken tree.
    """
    path = integration_checkout_path(root)

    if _worktree_registered(root, path):
        current = hostconfig.current_branch(path)
        if current == base:
            return path
        if not hostconfig.dirty_outside_board(path):
            switched = git.run(path, "checkout", base)
            if switched.returncode == 0 and hostconfig.current_branch(path) == base:
                return path
        raise RuntimeError(
            f"the dedicated integration checkout at {path} is on `{current}`, not "
            f"`{base}`, and could not be switched cleanly — resolve it by hand "
            f"(commit or discard its changes, `git checkout {base}` there)")

    # Not a worktree this repo tracks. A bare leftover directory (a pruned
    # registration, or one belonging to a prior test repo) is cleared so the add
    # below has an empty target; the branch carries every board commit, so there is
    # nothing to lose in the directory itself.
    git.run(root, "worktree", "prune")
    if path.exists():
        git.run(root, "worktree", "remove", "--force", str(path))
        git.run(root, "worktree", "prune")
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        raise RuntimeError(
            f"{path} exists but is not a git worktree and could not be removed — "
            f"move it aside so the runner can cut its dedicated {base} checkout")

    path.parent.mkdir(parents=True, exist_ok=True)
    made = _worktree_add(root, str(path), base)
    if made.returncode != 0:
        raise RuntimeError(
            f"could not cut the dedicated {base} checkout at {path}: "
            f"{(made.stderr or made.stdout or '').strip()[:200]}")
    return path


def stranded_board_edits(ctrl: Path, base: str) -> list[str]:
    """Board files the launch checkout holds and `base` does not.

    The dedicated-checkout topology moved the board's *reader* without moving the
    board's *editor*. The Obsidian vault is the launch checkout, and so is every
    interactive session, while `ensure_integration_checkout` points every dispatch
    at a second working copy of `Board/`. So an answer typed into a card while the
    launch checkout sits on a feature branch is invisible to the very run it was
    typed for — and invisible *quietly*: `select()` reports every card it left out,
    but a card sitting in a lane of a board it never opened is not something it can
    report at all.

    Measured 2026-08-28: `drained-vault-graphics` was answered, reset to
    `attempts: 0` and promoted to `tasks/` in the launch checkout on
    `ai/minigame-timing-invariant`, uncommitted. Two chore batches then read the
    dedicated checkout, found the card still parked in `needs-decision/` with its
    one attempt spent, and wrote "nothing to dispatch" (Karel: *"I have
    'drained-vault-graphics' in chores, but run chores doesn't run it"*).

    Both ways a board edit strands, because both are the same mistake:

    * **uncommitted** under `Board/` in the launch checkout, and
    * **committed on the launch branch** but not reachable from `base`. Board
      commits belong on the integration branch, so anything the launch branch
      changed under `Board/` on its own is a board only it can see. Three-dot, so
      this is what the launch branch did rather than what `base` moved on past it.

    Scoped to `Board/` deliberately: `Board.base` and every `GENERATED_VIEWS` file
    live at the repo root, so the rewrite Obsidian performs on `Board.base` at every
    vault open — expected, and explicitly not a finding — cannot make this cry wolf.
    """
    stranded = {path for _, path in git.status(ctrl) if path.startswith("Board/")}
    stranded.update(git.changed(ctrl, f"{base}...HEAD", "--", "Board/"))
    return sorted(stranded)


def stranded_board_refusal(ctrl: Path, work: Path, base: str) -> str:
    """Why a run reading `work` must not start, or `""` — see `stranded_board_edits`.

    Only ever a refusal when the board being read is not the board being edited:
    with the launch checkout as the work root there is one copy of `Board/` and
    nothing can strand, which is the in-place case and the common one.

    A refusal rather than a warning, for the same reason `dirty_outside_board` is
    one: the alternative is a run that dispatches from a board its maintainer cannot
    see, and the cheapest place to notice that is before anything is spent.
    """
    try:
        same = work.resolve() == ctrl.resolve()
    except OSError:
        same = work == ctrl
    if same:
        return ""
    stranded = stranded_board_edits(ctrl, base)
    if not stranded:
        return ""
    shown = ", ".join(stranded[:6]) + (f" (+{len(stranded) - 6} more)"
                                       if len(stranded) > 6 else "")
    return (f"the launch checkout at {ctrl} holds board edits that `{base}` does not: "
            f"{shown}. The board for this run is read from {work}, so those edits would "
            f"be ignored — silently, since a card in a lane that was never scanned "
            f"cannot even be reported as skipped. Land them on `{base}` first (commit "
            f"them there, or redo them in {work}) and re-run.")


# --------------------------------------------------------------------------
# Warm resume (runner-worker-handover)
# --------------------------------------------------------------------------
#
# A worker walled mid-stream used to lose everything: the worktree was
# force-removed the instant a wall was seen, it had committed nothing on its own,
# and the next attempt cold-started a fresh session from a blank prompt. The fix
# is to preserve the *session* and the *worktree* across a limit stop and to
# re-enter them, so a card bigger than one usage window finishes across attempts.
#
# The state that makes that possible is three facts about the last interruption,
# and it lives in the card's run directory — machine-local and gitignored, which
# is correct: a session id and a worktree cannot travel to another machine, so
# there is nothing to sync. It survives a reboot because `.ai/runs/` is on disk,
# which is the same guarantee every other piece of runner state rests on.

# Modes `prepare_worktree` returns, deciding how the next attempt continues:
#   FRESH   — cold start: no handover, or both worktree and branch are gone.
#   REENTER — the kept worktree is reused in place; its uncommitted diff *is*
#             where the last attempt ended (fallback paths 1 and 2).
#   FROM_WIP — the worktree was lost but the branch survives with a `wip:` commit,
#             so a checkout is cut from the branch and the worker is handed a
#             `## Progress` note (fallback path 3, the deepest persistence).
#   FROM_REVIEW — the last attempt *finished*: gates green, tests green, and the
#             reviewer returned `needs_fix`. Its commits are on `ai/<id>` and the
#             only work left is the finding. A checkout is cut from the branch,
#             exactly like FROM_WIP, and the worker is handed `_REVIEW_FIX_NOTE`.
#   FROM_BRANCH — no handover survived, but `ai/<id>` carries commits ahead of the
#             base. The work is real and durable even though the run directory
#             forgot it; a checkout is cut from the branch and the worker is handed
#             a `## Progress` note, exactly like FROM_WIP. The catch-all that stops
#             committed work falling through to a cold start.
FRESH, REENTER, FROM_WIP, FROM_REVIEW, FROM_BRANCH = (
    "fresh", "reenter", "from-wip", "from-review", "from-branch")


def _branch_has_work(root: Path, branch: str, base: str) -> bool:
    """Does `branch` exist and carry at least one commit `base` does not?

    The durable half of "was anything built for this card". Deliberately asks git
    rather than the run directory: `.ai/runs/` is gitignored and machine-local, so
    a handover is the first thing to go missing, while a commit on a branch
    survives a reboot, a killed runner and a cleaned run tree.

    False on any git failure — a missing branch, an unreadable base, a repo in a
    state `rev-list` will not answer for. That is the safe direction here: the
    caller's fallback is the cold start, which is merely expensive, whereas
    wrongly claiming work exists would hand a worker an unrelated tree.
    """
    if not _branch_exists(root, branch):
        return False
    counted = git.run(root, "rev-list", "--count", f"{base}..{branch}")
    if counted.returncode != 0:
        return False
    try:
        return int(counted.stdout.strip() or "0") > 0
    except ValueError:
        return False


@dataclass
class Handover:
    """What one limit-interrupted attempt leaves for the next (Decision 1).

    `session_id` is read straight out of the walled worker's own result JSON —
    it was already written to `worker-N.json`, so nothing has to be *captured*,
    only used. `diff_hash` is the working-tree state at the interruption, the
    input to the progress gate (Decision 2). `no_progress` counts consecutive
    resumes that moved nothing, so a card that cannot advance is filed rather
    than given its attempt back forever.

    `review_fix` is the one field that is not about an interruption: the last
    attempt ran to completion and the reviewer sent it back with a finding. It
    rides in the same file because it answers the same question — *does the next
    attempt start from what the last one built, or from nothing* — and a second
    file answering that question would be a second thing to keep in sync.

    `reviewed_sha` and `review_finding` exist for the same reason, one layer
    later: they are what the *next review* — not the next worker attempt — needs
    to avoid re-deriving what the last one already established. `reviewed_sha` is
    the branch tip `review_branch` actually diffed against last time, captured
    the moment the `needs_fix` verdict lands (before the next attempt adds a
    single commit on top); `review_stage` only trusts it as an incremental base
    after confirming it is still an ancestor of the current tip, so a branch that
    was cold-started from scratch in between (its history no longer contains that
    commit) falls back to a full review rather than silently hiding a rebuilt
    diff (`review-reviews-only-the-fix`).

    **These three fields survive an interruption; the other three are the
    interruption.** Every rewrite of an existing handover therefore goes through
    `dataclasses.replace`, never a fresh `Handover(session, hash, n)` — building a
    new one from the interruption fields alone silently dropped the review anchor
    and cost the next review its incremental base. `_reanchor_review` moves
    `reviewed_sha` onto the replayed tip when a resume rebases the branch, for the
    same reason (`review-anchor-survives-the-replay`).
    """
    session_id: str = ""
    diff_hash: str = ""
    no_progress: int = 0
    review_fix: bool = False
    reviewed_sha: str = ""
    review_finding: str = ""


def _handover_path(root: Path, card_id: str) -> Path:
    return root / hostconfig.RUNS / card_id / "handover.json"


def read_handover(root: Path, card_id: str) -> Handover:
    data = hostconfig._load_json(_handover_path(root, card_id))
    return Handover(
        session_id=str(data.get("session_id", "")),
        diff_hash=str(data.get("diff_hash", "")),
        no_progress=int(data.get("no_progress", 0) or 0),
        review_fix=bool(data.get("review_fix", False)),
        reviewed_sha=str(data.get("reviewed_sha", "")),
        review_finding=str(data.get("review_finding", "")),
    )


def write_handover(root: Path, card_id: str, handover: Handover) -> None:
    path = _handover_path(root, card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, json.dumps(handover.__dict__, indent=2))


def clear_handover(root: Path, card_id: str) -> None:
    """Drop the warm-resume state. Called wherever a worktree is dropped — once a
    card resolves (review/parked/failed) or is filed as stuck, the next dispatch
    of that id must cold-start, never re-enter a session that belonged to a run
    that is already over."""
    _handover_path(root, card_id).unlink(missing_ok=True)


def _reanchor_review(root: Path, card_id: str, tree: Path, mode: str) -> None:
    """Move `reviewed_sha` onto the just-replayed tip, so the rebase does not cost
    the next review its incremental base (`review-anchor-survives-the-replay`).

    Two correct fixes, five days apart, and the second silently disabled the first.
    `review-reviews-only-the-fix` (2026-08-27) recorded the tip the last review
    actually saw so the next one could diff `reviewed_sha...branch` — the fix alone
    — instead of the whole branch at lead-tier prices. Then the `menu-art-start-run`
    replay (2026-09-03) made every resumed branch rebase onto `base` first, which is
    right and must stay. But a rebase rewrites every hash on the branch, so
    `_is_ancestor(reviewed_sha, branch)` in `review_stage` stopped holding and the
    incremental base fell back to a full review — every `needs_fix` round, on every
    card whose branch was behind, which in a productive night is all of them.
    Measured on `triage-findings-have-a-shelf-life` (2026-09-16): its fourth round
    re-reviewed the full 688-line branch diff to approve a 29-line prose fix, and
    re-derived from scratch the whole census the third round had already confirmed.

    **Why the replayed tip is the right anchor, and not an approximation.** This runs
    on the `FROM_REVIEW` path only, and that path is entered *before* the worker is
    dispatched — at this moment the branch carries exactly the commits the last
    review saw and not one more. So the rebased `HEAD` is that same reviewed content,
    replayed: the anchor by construction, not a guess at a commit mapping. Re-anchoring
    rather than counting commits also survives a rebase that drops a commit as empty.

    Deliberately **not** done on the other resume paths. `FROM_WIP` and `REENTER`
    reach a branch whose tip may already carry the fix (a `wip:` commit, or an
    api-error interruption's), and re-anchoring there would hide exactly the change
    the reviewer is being asked to check. A rebase that *fails* never gets here —
    `from_branch` returns `None`, the branch is genuinely rebuilt from cold, and
    `review_stage`'s ancestor check correctly refuses the stale anchor as before.
    """
    if mode != FROM_REVIEW:
        return
    prior = read_handover(root, card_id)
    if not (prior.review_fix and prior.reviewed_sha):
        return
    tip = git.run(tree, "rev-parse", "HEAD").stdout.strip()
    if not tip:
        # Nothing to re-anchor onto. Leaving the old sha is the safe direction: it
        # is no longer an ancestor, so `review_stage` falls back to a full review.
        return
    write_handover(root, card_id, replace(prior, reviewed_sha=tip))


def _worktree_dirty(root: Path, tree: Path) -> bool:
    return git.dirty(tree)


def _worktree_state_hash(tree: Path) -> str:
    """A deterministic hash of the working tree relative to HEAD, tracked *and*
    untracked. Two attempts whose working trees are byte-identical hash the same;
    any moved byte changes it. This is the progress gate's whole instrument — a
    working-tree diff, not a branch-tip check, because preserved uncommitted
    changes are exactly the progress being measured (Decision 2). Read-only: it
    never touches the index, so measuring progress cannot itself disturb the
    state being resumed.
    """
    # `-uall` expands untracked directories to individual files, so their content
    # is hashed rather than just the directory name.
    #
    # Read through `git`, which is not cosmetic here: git quotes a path whose
    # bytes are not printable ASCII, so a card touching `Animation – attack.md`
    # used to hash the *escaped* spelling and then fail `is_file()` on it — the
    # file's content dropped out of the hash entirely, and the progress gate stopped
    # being able to see changes to exactly the files most likely to be board content.
    entries = sorted(git.status(tree, "-uall"))
    digest = hashlib.sha256()
    digest.update("\n".join(f"{code} {path}" for code, path in entries).encode("utf-8"))
    for _, rel in entries:
        blob = tree / rel
        if blob.is_file():
            digest.update(b"\0")
            try:
                digest.update(blob.read_bytes())
            except OSError:
                pass
    return digest.hexdigest()


def _branch_exists(root: Path, branch: str) -> bool:
    return git.run(root, "rev-parse", "--verify", branch).returncode == 0


def _is_ancestor(root: Path, ancestor: str, ref: str) -> bool:
    """Whether `ancestor` is reachable from `ref` — the safety check before
    reusing a prior review's sha as an incremental diff base (`review_stage`).
    False on any git failure (an empty or garbage sha, `ref` gone), never an
    exception: an invalid sha must fall back to a full review, not crash the
    stage that was about to run one anyway."""
    return git.run(root, "merge-base", "--is-ancestor", ancestor, ref).returncode == 0


def _worktree_registered(root: Path, path: Path) -> bool:
    """Whether `path` is a live worktree git still tracks. A bare `path.exists()`
    is not enough: a crash can leave the directory with git's registration
    pruned, or the registration with the directory gone, and either half alone
    cannot be re-entered."""
    listed = git.run(root, "worktree", "list", "--porcelain").stdout
    target = str(path.resolve())
    for line in listed.splitlines():
        if line.startswith("worktree "):
            if str(Path(line[len("worktree "):].strip()).resolve()) == target:
                return True
    return False


def commit_wip(root: Path, tree: Path, card_id: str) -> bool:
    """The deepest persistence (Decision 1, path 3). Stage everything in the
    worktree and commit it as `wip: <card> interrupted` on `ai/<id>`, so that if
    the *worktree* is later lost — a crash, or the disk-cap demotion owned by
    `runner-prune-run-dirs` — the work still survives on the branch and the next
    attempt can cut a fresh checkout from it. A no-op (returns False) when there
    is nothing uncommitted, so calling it defensively is free.
    """
    if not _worktree_dirty(root, tree):
        return False
    git.run(tree, "add", "-A")
    done = git.run(tree, "commit", "-m", f"wip: {card_id} interrupted")
    return done.returncode == 0


def _rescue_prefix(card_id: str) -> str:
    return f"ai/{card_id}@failed-"


def _rescue_branches(root: Path, card_id: str) -> list[str]:
    """Every `ai/<card_id>@failed-N` ref, whatever order git lists them in."""
    prefix = _rescue_prefix(card_id)
    listed = git.run(root, "for-each-ref", "--format=%(refname:short)",
                  f"refs/heads/{prefix}*").stdout
    return [line.strip() for line in listed.splitlines() if line.strip()]


def _next_rescue_branch(root: Path, card_id: str) -> str:
    """The next free `ai/<card_id>@failed-N` name.

    Not `card.attempts` — `blocked`/`limited` rewind `attempts` on give-back
    (`settle`), so the same number can recur across a card's life and collide
    with a rescue slot a real failure already used. Scanning existing refs for
    the highest `N` and adding one is correct regardless of how attempts were
    spent or given back, and needs no rewind-aware bookkeeping of its own.
    """
    prefix = _rescue_prefix(card_id)
    highest = 0
    for name in _rescue_branches(root, card_id):
        suffix = name[len(prefix):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1}"


def _drop_rescue_branch(root: Path, name: str) -> None:
    """Delete one rescue ref — on the remote first, then locally.

    `publish()` pushes *every* ref under `refs/heads/ai/` and a rescue branch
    lives there like any other, so on a host that declares `publish_remote`
    (Dungeoneer's laptop does) the ref being reaped here has a copy on the
    remote. Reaping only the local half would leave exactly the orphaned
    `<remote>/ai/<id>@failed-N` that `rebase_and_merge` stopped leaving behind
    for card branches on 2026-08-09 — up to `MAX_ATTEMPTS` per card, and
    nothing would ever look at them again.

    Excluding rescue refs from `publish()` instead would be the smaller change
    and the wrong one: on an ephemeral cloud checkout the pushed copy is the
    *only* place a preserved attempt survives the container, which is the whole
    point of preserving it (failed-attempt-work-is-deleted-not-resumed).

    Remote before local, because `landing.delete_remote_branch`'s guard is an ancestry
    test against the local tip and there is nothing left to test against after
    `git branch -D`. The remote is resolved here rather than threaded through
    the callers because two of them (`sweep_terminal_cards`,
    `cap_rescue_branches_in_flight`) reap across cards at startup, before the
    run loop has resolved a remote of its own.
    """
    landing.delete_remote_branch(root, str(hostconfig.host_setting(root, "publish_remote", "")).strip(),
                                 name, action="reaped")
    git.run(root, "branch", "-D", name)


def prune_rescue_branches(root: Path, card_id: str) -> None:
    """Delete every rescue branch for a card that has left `tasks/` for good.

    Called on `prune_run_dir`'s own triggers (failed-attempt-work-is-deleted-
    not-resumed): once a card is retired or otherwise settled, its preserved
    failed-attempt commits have nothing left to be rescued *for* — the same
    reasoning `prune_run_dir` already applies to `.ai/runs/<id>/`."""
    for name in _rescue_branches(root, card_id):
        _drop_rescue_branch(root, name)


def cap_rescue_branches(root: Path, card_id: str, keep: int = hostconfig.MAX_ATTEMPTS) -> None:
    """Backstop for a card still in flight, mirroring `cap_run_dir`: keep only
    the `keep` most recent rescue branches. A card dispatched by name
    (`--card`) waives the attempt limit (03_board.md), so without this a card
    re-run past `MAX_ATTEMPTS` by hand could accumulate rescue refs without
    bound; the ordinary retirement path never reaches more than `keep - 1`."""
    def rescue_no(name: str) -> int:
        suffix = name[len(_rescue_prefix(card_id)):]
        return int(suffix) if suffix.isdigit() else -1

    branches = sorted(_rescue_branches(root, card_id), key=rescue_no)
    excess = len(branches) - keep
    for stale in branches[:max(excess, 0)]:
        _drop_rescue_branch(root, stale)


def cap_rescue_branches_in_flight(root: Path, keep: int = hostconfig.MAX_ATTEMPTS) -> None:
    """`cap_rescue_branches` over every card still in `tasks/` — the only lane
    whose rescue branches are not otherwise reaped by `prune_rescue_branches`."""
    for card in board.cards(root, "tasks"):
        cap_rescue_branches(root, card.id, keep)


def _normalize_worktree(path: Path) -> None:
    """Re-materialise any CRLF a fresh checkout picked up, before a worker sees it.

    `git worktree add` is a checkout, and a checkout is exactly the shape
    `normalize_worktree` exists for (its own docstring: "run once per checkout
    that pre-dates the attributes landing"). But that instruction assumes one
    checkout per clone; the runner cuts a brand-new linked worktree per card,
    every dispatch, and nothing ever ran the fix on those — so on a host where
    the smudge filter re-introduces CRLF on checkout, every worktree started
    unnormalized and a worker had no standing to fix a per-machine gate
    violation on files it never touched (`fresh-worktree-never-gets-normalized`,
    2026-08-13). Best-effort: a worktree normalize() cannot clean (real
    uncommitted content, not just CRLF) is left for the gate to report as
    before — this only removes the false-positive case.
    """
    import io
    from contextlib import redirect_stdout

    from nightshift import normalize_worktree as _now

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = hostconfig._now.normalize(path)
    except Exception as exc:  # pragma: no cover - defensive, must never block dispatch
        hostconfig._log(f"    normalize_worktree raised on {path.name}, continuing anyway: {exc}")
        return
    if rc != 0:
        hostconfig._log(f"    normalize_worktree left violations in {path.name}:\n{buf.getvalue().rstrip()}")
    elif "rewrote" in buf.getvalue():
        hostconfig._log(f"    normalized {path.name}'s line endings before dispatch")


def _commits_behind(root: Path, branch: str, base: str) -> int:
    """How many commits `base` carries that `branch` does not."""
    out = git.run(root, "rev-list", "--count", f"{branch}..{base}")
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 0


def prepare_worktree(root: Path, card: board.Card,
                     base: str) -> tuple[Path, str, str]:
    """The worktree for this attempt, plus how it continues.

    A cold card gets a brand-new checkout cut from `base`, exactly as before. A
    card that was limit-interrupted (it has a handover on disk) is continued:
    its kept worktree is reused in place if it still exists, or — if only the
    branch survived a WIP commit — a checkout is cut from that branch. A card the
    reviewer returned `needs_fix` is continued too, from its own finished branch.

    Every path out of here normalizes the worktree's line endings first
    (`_normalize_worktree`) — a freshly cut worktree is a fresh checkout, which
    is precisely the state that needs it.

    **Every resumed branch is replayed onto `base` first, and a branch that will not
    replay is not resumed** (`from_branch`). Reading a resumed branch as-is is what
    ended the night of 2026-09-03 — see that function.
    """
    branch = branches.work_branch(card.id, card.fields.get("branch", ""))
    path = worktree_root(root) / card.id
    handover = read_handover(root, card.id)
    warm = bool(handover.session_id or handover.diff_hash)

    def from_branch(mode: str, why: str) -> tuple[Path, str, str] | None:
        """A fresh checkout of the existing `branch`, keeping its commits — brought
        up to `base` first, or `None` if it cannot be.

        **The replay is the point, not housekeeping.** A resumed branch is judged by
        the gates and the test slice *in its own tree*, so a branch that is behind is
        judged by an old copy of both. `menu-art-start-run` was resumed on 2026-09-03
        from a branch forked 989 commits earlier; it ran that branch's August gate
        suite — 42 gates, against the 50 on `test` that morning — over its August
        memory docs, which still cited two framework files nightshift had deleted in
        the meantime. Nine violations, none of them in the card's diff, so
        `_is_repo_drift` said drift and the night stopped with 23 minutes of its
        usage-limit window left, telling Karel to fix an integration branch that was
        already green. The card was blameless and so was `test`; the tree in between
        was three weeks old and nothing had asked.

        `None` rather than a raise or a merge: a replay that conflicts is real
        disagreement between this branch and `base`, and resolving it is not this
        function's job — it has no agent, no gates and no way to judge a resolution.
        Falling through to the cold start below is the honest answer, and it is
        already the safe one, because that path renames the branch to a rescue ref
        instead of deleting it. The work survives, reachable, and the attempt starts
        from a tree that matches the repo.

        `REENTER` is deliberately not replayed. That path reuses a *live* worktree
        mid-session, whose uncommitted edits are the resume's whole subject; a rebase
        there would either refuse on the dirty tree or rewrite the ground under a
        session that is still running. Its exposure is also much smaller — a warm
        resume happens inside one night, so its branch is at most a few merges
        behind, not three weeks.
        """
        if path.exists() or _worktree_registered(root, path):
            git.run(root, "worktree", "remove", "--force", str(path))
        git.run(root, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        made = _worktree_add(root, str(path), branch)
        if made.returncode != 0:
            raise RuntimeError(f"git worktree add ({why}) failed: {made.stderr.strip()}")
        behind = _commits_behind(root, branch, base)
        if behind:
            # `gitmerge.STRATEGY_ARGS` so this replay renormalizes line endings the
            # same way `rebase_and_merge`'s does — the one place that policy lives.
            # Normalizing the checkout is deliberately left until *after*: it rewrites
            # files in the working tree, and a dirty tree is one git refuses to rebase.
            #
            # `GIT_EDITOR=true` for the same reason `_resolve_conflict` sets it: this
            # process has no terminal for an editor git might want to open.
            done = git.run(path, "rebase", *gitmerge.STRATEGY_ARGS, base,
                           env={**os.environ, "GIT_EDITOR": "true"})
            if done.returncode != 0:
                git.run(path, "rebase", "--abort")
                hostconfig._log(f"    {branch} is {behind} commit(s) behind {base} and will not "
                     f"replay onto it — starting {card.id} cold, its commits kept as a "
                     f"rescue branch")
                return None
            hostconfig._log(f"    replayed {branch} onto {base} ({behind} commit(s) behind)")
            _reanchor_review(root, card.id, path, mode)
        _normalize_worktree(path)
        return path, branch, mode

    if warm and _worktree_registered(root, path):
        _normalize_worktree(path)
        return path, branch, REENTER
    if warm and _branch_exists(root, branch):
        # The worktree was lost but the branch (with its `wip:` commit) was not.
        if (resumed := from_branch(FROM_WIP, "from wip")) is not None:
            return resumed
    if handover.review_fix and _branch_exists(root, branch):
        # The reviewer sent a *finished* attempt back with a concrete finding.
        #
        # This branch's work is not a failed attempt and must not be treated as
        # one. Its gates passed, its tests passed, and the reviewer's objection
        # was to one verifiable detail — so the cheapest correct next step is to
        # apply that detail on top, which needs the commits that are already
        # here. Cutting from `base` instead is what the code below used to do,
        # and it was wrong in a way that hid behind a green board: the branch was
        # renamed to a rescue ref nothing ever checked out again, and the worker
        # was handed an empty tree plus a finding phrased as "apply this fix
        # directly; it does not need re-deriving" — advice that could not be
        # followed, because the thing to fix did not exist yet.
        #
        # Measured on 2026-08-25. `economy-early-late-balance` came back with a
        # one-substring correction to a memory doc and had to re-implement an
        # economy rebalance to reach it. `catalog-registry-and-guards` did three
        # full attempts, each a sound implementation, each producing a *different*
        # stale citation because each started from nothing — and was then filed to
        # needs-decision/ under "a reviewer-flagged fix recurred across 3
        # attempts". Nothing recurred. The fix was never applied once.
        if (resumed := from_branch(FROM_REVIEW, "from review")) is not None:
            return resumed
    if str(card.fields.get("last_outcome", "")) != "failed" and \
            _branch_has_work(root, branch, base):
        # The branch carries commits, nothing above claimed it, and the last
        # attempt did not *fail*.
        #
        # **That last condition is the boundary, and it is not cosmetic.** A failed
        # attempt is one whose gates or tests went red, and its commits are a broken
        # tree — resuming onto them builds the next attempt on the defect instead of
        # giving it the clean slate the cold start below exists to provide. A walled
        # attempt is the opposite: it was going fine and ran out of window. Only a
        # failure writes `last_outcome: failed` (a wall writes nothing), so the card
        # already records the distinction and nothing new has to be stored.
        #
        # Caught by `test_rescue_branches_are_reaped_when_a_card_retires_to_failed`
        # and its siblings, which is the right way round: without the guard this
        # swallowed the rescue-ref path whole, and a card that failed three times
        # would have kept re-entering its own broken branch forever.
        #
        # **The hole this closes, found by measurement on 2026-08-29.** Every warm
        # path above is gated on `warm` — a handover file. `taser-cyberware` walled
        # after spending $10.13 and left none, but its branch held five real
        # commits: the implementation, 22 tests, its i18n catalog entries and its
        # memory records. With no handover, `warm` was False, `review_fix` was
        # False, and the card fell straight through to the cold start below — which
        # renames the branch to a rescue ref and cuts a fresh worktree from `base`.
        # Nothing ever checks out a rescue ref. `ai/skills-tinkering@failed-1` shows
        # it had already happened once before anyone counted.
        #
        # So `failed-attempt-work-is-deleted-not-resumed` was only half fixed: the
        # rename stopped the commits being *destroyed*, and stopped there. Work that
        # survives but is never read again costs exactly what deleted work costs;
        # the rescue ref made the loss silent rather than smaller.
        #
        # The handover is machine-local and gitignored, so it is the *fragile* half
        # of the record by construction — a reboot, a `.ai/runs/` clean, a run
        # killed before it could write. The branch is the durable half and is the
        # better thing to key on: commits ahead of `base` are the work, whatever
        # the run directory does or does not remember about how they got there.
        if (resumed := from_branch(FROM_BRANCH, "from branch")) is not None:
            return resumed

    # Cold start — the empty case and every non-interrupted card.
    if path.exists() or _worktree_registered(root, path):
        git.run(root, "worktree", "remove", "--force", str(path))
    git.run(root, "worktree", "prune")
    if _branch_exists(root, branch):
        # This is where a failed attempt's work used to die: `git branch -D`
        # here, one line, discarded the previous attempt's commits the moment
        # the card was retried — nothing ever read them again (failed-attempt-
        # work-is-deleted-not-resumed). Renaming instead keeps them reachable
        # as a rescue ref until the card leaves `tasks/` for good
        # (`prune_rescue_branches`), while `branch` itself is free for the
        # fresh checkout below exactly as before.
        git.run(root, "branch", "-m", branch, _next_rescue_branch(root, card.id))
    clear_handover(root, card.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    made = _worktree_add(root, "-b", branch, str(path), base)
    if made.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {made.stderr.strip()}")
    _normalize_worktree(path)
    return path, branch, FRESH


def harvest(root: Path, tree: Path, out_dir: Path) -> int:
    """Rescue gitignored artefacts from the worktree before it is destroyed.

    Not every worker's output is a commit. The art pipeline generates candidates
    into a harvest dir such as `<pkg>/assets/.tmp/`, gitignored on purpose — they move
    into `assets/` only after Karel approves one. Without this step an art card
    would produce four good candidates, have them deleted with the worktree, and
    then be filed as "the worker committed nothing", which is both wrong and the
    single most confusing failure the runner could report.

    Returns the number of files rescued, which is also what stops the empty-diff
    check from firing on a card whose output was never meant to be a commit.
    """
    rescued = 0
    for relative in hostconfig.harvest_dirs(root):
        source = tree / relative
        if not source.is_dir():
            continue
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            target = out_dir / "artefacts" / relative.name / item.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            rescued += 1
    return rescued


def unadopted_artefacts(root: Path, harvested: int, changed: Iterable[str]) -> int:
    """How many candidates this attempt produced *without installing any of them*.

    Non-zero means what the maintainer is owed is a **choice**, not a
    play-through, and `settle` routes the card to `needs-decision/` on that basis
    rather than to whatever `verify:` asked for. Two facts, both already in hand
    by the end of a dispatch:

    * `harvested` — what `harvest` rescued out of the worktree. An artefact is by
      definition not a commit: it lives in a gitignored scratch dir, so nothing
      about it is on the branch, in the repo, or in the running program.
    * `changed` — this attempt's own diff. If it wrote nothing under the directory
      the candidates get promoted *into* (`neighbours_dir` — the approved siblings
      a `checker:` is already shown), then none was adopted and there is nothing
      installed to exercise.

    **The pair is what separates two shapes that used to share one lane.** A card
    that generates takes *and* commits one of them — the audio pipeline's synth
    fallback — has something in the program to play, and `testing/` is right for
    it. A card that generates candidates and installs none had nothing to play,
    and until 2026-09-08 reached `testing/` anyway: `stun-grenade-visuals` put
    four blue grenade icons in `assets/.tmp/`, nothing in `assets/items/`, and
    asked Karel to go and look at a picture that was not in the game. His answer:
    *"the card is not ready. It should ended in needs decision and give me a
    way to show and pick these results. It should end up in testing only after it
    is wired up in game."* `verify:` could not express that, and neither the
    checker nor the diff reviewer is positioned to notice — both had said `pass`
    on work that was, on its own terms, finished.

    Zero when the project declares no harvest dir, and zero when the harvest dir
    has no distinct parent inside the repo to adopt into. Both fall back to the
    pre-2026-09-08 routing, which is the safe direction: the card lands where its
    own `verify:` says and a human reads it either way.
    """
    if harvested <= 0:
        return 0
    adopt = hostconfig.neighbours_dir(root)
    if adopt is None:
        return 0
    try:
        prefix = adopt.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return 0
    if not prefix or prefix == ".":
        return 0
    # The scratch dirs themselves are gitignored, so they cannot appear in a diff
    # anyway — excluded explicitly so the rule reads as what it means rather than
    # relying on that, and so a project that tracks part of a harvest dir does not
    # get "adopted" from a candidate it merely regenerated in place.
    scratch = tuple(p.as_posix() for p in hostconfig.harvest_dirs(root))
    for path in changed:
        posix = str(path).replace("\\", "/")
        if posix.startswith(f"{prefix}/") and not any(
                posix == s or posix.startswith(f"{s}/") for s in scratch):
            return 0
    return harvested


def drop_worktree(root: Path, path: Path) -> None:
    """Remove the checkout, keep the branch. The branch is the deliverable —
    `review/` reads it and Karel merges it; twenty stale checkouts are not.

    Dropping the checkout also ends any warm-resume handover for this card: the
    worktree path's basename is the card id, and once the checkout is gone there
    is nothing left to re-enter, so a stale `session_id` must not lure the next
    dispatch into resuming a run that is over. A card being *kept* warm across a
    limit stop is never dropped here — that path returns before reaching this.
    """
    git.run(root, "worktree", "remove", "--force", str(path))
    git.run(root, "worktree", "prune")
    clear_handover(root, path.name)


# --------------------------------------------------------------------------
# Pruning (`runner-prune-run-dirs`)
# --------------------------------------------------------------------------
#
# `.ai/runs/` is gitignored and machine-local, and nothing used to delete
# anything from it: every attempt of every card kept its prompt, its worker
# JSON, its gate log and its pytest log forever. That is harmless at kilobytes
# and is not at megabytes-per-attempt (`runner-stream-worker-output`'s
# `stream.jsonl`) on a runner meant to work unattended every night — and the
# runner's own heartbeat and log tee live in the same directory, so a full disk
# takes out observability first.
#
# The rule this section enforces (`00_architecture.md` §13's "the answer should
# be at the call site", applied to disk instead of a card):
#
#   * `failed/` is reached by the runner itself (inside `settle()`), so it is
#     pruned **eagerly**, right after the move — two call sites, because
#     `runner-worker-handover` added a second in-run path to `failed/` (the
#     `stuck` breaker) alongside the pre-existing `MAX_ATTEMPTS` retirement.
#   * `done/` is reached by *Karel*, by hand, outside the runner — so it is
#     pruned in a **sweep at the next startup** (`sweep_terminal_
#     cards`, called from `run()` right after `recover()`). A card finished by
#     hand is cleaned on the next run, which is fine: run-dirs are only logs,
#     and are wanted *through* review anyway.
#   * Worktrees are a separate, earlier lifecycle. The only worktree that
#     outlives one dispatch is a limit-interrupted card still sitting in
#     `tasks/` (warm resume); the real backstop for that backlog is
#     `WORKTREE_KEEP_CEILING`, enforced once at startup alongside the sweep —
#     not `done/`, which is not where a kept worktree normally dies.
#
# Every function here is a directory listing, a `shutil.rmtree` or a `git
# worktree` call — no LLM anywhere near it (§12), and every one is a no-op, not
# an error, when its target does not exist.

def prune_run_dir(root: Path, card_id: str) -> None:
    """Remove `.ai/runs/<card_id>/` — every attempt, the handover marker, all of
    it. Never touches `status.json`, the day's `<date>.log` tee or `.lock`:
    those live directly under `.ai/runs/`, a sibling of this per-card directory,
    not inside it, so they cannot be reached from here even for the card
    currently running."""
    shutil.rmtree(root / hostconfig.RUNS / card_id, ignore_errors=True)


def cap_run_dir(root: Path, card_id: str, keep: int = hostconfig.MAX_ATTEMPTS) -> None:
    """Backstop for a card still in flight: keep only the last `keep` attempt
    directories. `keep` defaults to `MAX_ATTEMPTS`, so in the ordinary run a
    card never accumulates more attempts than this before it is retired — this
    only fires for a card that somehow accumulates more."""
    card_dir = root / hostconfig.RUNS / card_id
    if not card_dir.is_dir():
        return

    def attempt_no(path: Path) -> int:
        try:
            return int(path.name.split("-", 1)[1])
        except (IndexError, ValueError):
            return -1

    attempts = sorted(
        (p for p in card_dir.iterdir() if p.is_dir() and p.name.startswith("attempt-")),
        key=attempt_no,
    )
    excess = len(attempts) - keep
    for stale in attempts[:max(excess, 0)]:
        shutil.rmtree(stale, ignore_errors=True)


def cap_run_dirs_in_flight(root: Path, keep: int = hostconfig.MAX_ATTEMPTS) -> None:
    """`cap_run_dir` over every card still in `tasks/` — the only lane whose
    run-dirs are not otherwise pruned or swept."""
    for card in board.cards(root, "tasks"):
        cap_run_dir(root, card.id, keep)


_LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")


def prune_old_run_logs(root: Path, days: int = hostconfig.LOG_RETENTION_DAYS) -> None:
    """The `<date>.log` tee's own, simpler retention rule: keep the last N days.
    Distinct from run-dir pruning because a log is not tied to any one card's
    lane — it is the whole night's transcript, cards in flight and all."""
    runs_dir = root / hostconfig.RUNS
    if not runs_dir.is_dir():
        return
    cutoff = dt.date.today() - dt.timedelta(days=days)
    for path in runs_dir.glob("*.log"):
        match = _LOG_NAME.match(path.name)
        if not match:
            continue
        try:
            logged = dt.date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if logged < cutoff:
            path.unlink(missing_ok=True)


def prune_worktree_if_present(root: Path, card_id: str) -> None:
    """`drop_worktree`, but only if there is one — the terminal-lane sweep calls
    this over cards that may never have had a worktree survive to this point."""
    path = worktree_root(root) / card_id
    if path.exists() or _worktree_registered(root, path):
        drop_worktree(root, path)


def demote_worktree_to_wip(root: Path, tree: Path, card_id: str) -> None:
    """Demote a kept worktree to git-WIP-only, when `enforce_worktree_ceiling`
    finds more limit-interrupted checkouts than the ceiling allows: bank its
    uncommitted diff onto the card's own branch via `commit_wip` — the real
    primitive `runner-worker-handover` built for exactly this caller, named in
    its own docstring — then drop the checkout.

    `commit_wip` is already silent when there is nothing to bank (a clean
    tree). A genuine commit failure (a rejecting hook, a full disk) is
    different — that would silently lose the uncommitted diff the demotion
    exists to preserve, dressed up as routine cleanup — so it is logged loudly
    before the checkout is dropped regardless; the ceiling has to be enforced
    either way.
    """
    dirty = _worktree_dirty(root, tree)
    committed = commit_wip(root, tree, card_id)
    if dirty and not committed:
        hostconfig._log(f"  ! {card_id}: WIP commit failed while demoting its worktree past the "
             f"kept-worktree ceiling — the uncommitted diff may be lost; see "
             f"`git status` in {tree}")
    drop_worktree(root, tree)


def enforce_worktree_ceiling(root: Path, ceiling: int = hostconfig.WORKTREE_KEEP_CEILING) -> list[str]:
    """Cap concurrent kept worktrees at `ceiling` (Decision 3). Written over the
    physical worktree directories and each card's *current* lane, generically —
    not over any `runner-worker-handover`-specific state — so it does real work
    the moment a card is limit-interrupted and stays correct regardless of how
    that machinery evolves. A "kept" worktree, here, is simply one whose card is
    still sitting in `tasks/`: the only way a worktree outlives one dispatch.

    Demotes the *oldest* kept worktrees first (by directory mtime) until at or
    under the ceiling, and returns the card ids demoted, for the run log.
    """
    root_dir = worktree_root(root)
    if not root_dir.is_dir():
        return []
    kept: list[Path] = []
    for entry in root_dir.iterdir():
        if not entry.is_dir():
            continue
        card = board.find(root, entry.name)
        if card is not None and card.lane == "tasks":
            kept.append(entry)
    over = len(kept) - ceiling
    if over <= 0:
        return []
    kept.sort(key=lambda p: p.stat().st_mtime)
    demoted = []
    for entry in kept[:over]:
        demote_worktree_to_wip(root, entry, entry.name)
        demoted.append(entry.name)
    return demoted


def sweep_terminal_cards(root: Path) -> list[str]:
    """The startup half of the terminal-lane rule: `done/` and `testing/`
    are both reached by hand sometimes (a card closed out inline, outside the
    runner: `manage-board`'s own documented gap) so they are never pruned
    eagerly — this runs once at the next startup instead, over `done/`,
    `failed/` and `testing/` so a card the eager prune missed (settle()'s own
    hook covers every path *it* takes out of `tasks/`, but not a hand-moved
    one) is cleaned up too. `testing/` was missing here until
    `rescue-branches-only-swept-on-failure` (2026-08-13): a card that succeeds
    normally lands there, not in `done/`/`failed/`, and used to keep its
    `@failed-N` rescue refs forever. Returns the card ids whose run dir was
    actually pruned.

    **The run dir (`.ai/runs/<id>/`, not the worktree or the rescue branches)
    is pruned only for `done/`.** It carries the `worker-N.json` that the
    Command Center's Talk/`Open inline` buttons resume from
    (`panel._latest_session_for_card`) — deleting it the instant a card
    landed in `testing/` meant the very button meant for "reopen this and
    tell it what's wrong" could never resume anything: on this machine every
    `testing/` card had already lost its session by the next runner startup
    (Karel, 2026-08-28, after "Open inline" on a real `testing/` card fell
    back to a fresh session it should have been able to resume). `failed/`
    gets the same durability, for the same reason a maintainer resuming a
    failed card wants its last session rather than a blank one — except the
    no-progress retirement (`_settle` above) still prunes immediately on
    purpose: that session already proved it does not converge, so there is
    nothing there worth resuming. Worktrees and rescue branches carry no
    session state, so both still prune eagerly for all three lanes."""
    pruned = []
    for lane in ("done", "failed", "testing"):
        for card in board.cards(root, lane):
            prune_worktree_if_present(root, card.id)
            # Unconditional, unlike the run-dir branch below: a rescue branch
            # is a git ref, not a `.ai/runs/` file, so it can outlive (or
            # predate) whatever that existence check is testing for, and
            # `prune_rescue_branches` is already a no-op when there is nothing
            # to delete.
            prune_rescue_branches(root, card.id)
            if lane == "done" and (root / hostconfig.RUNS / card.id).exists():
                prune_run_dir(root, card.id)
                pruned.append(card.id)
    return pruned
def assert_integration_unmoved(root: Path, base: str, expected_sha: str) -> bool:
    """The guarantee behind the fence: a worker must never land *code* on the
    shared integration branch. The hook *prevents* the wrong-checkout write; this
    *undoes* one that slipped through anyway (a path the hook could not see, or a
    machine where the hook was not read).

    `base` is checked out in the main checkout, so a stray worker commit shows up
    as `base` having moved past the tip the runner last committed — but so does a
    live session's own legitimate `Board/` commit landing on `base` while a
    dispatch runs in the background, which happens whenever the launch checkout
    is still on the integration branch (`run-the-runner`'s in-place topology).
    "Did the tip move" alone cannot tell those apart, and conflating them cost a
    board edit outright: 2026-09-10, Dungeoneer — this reset `test` mid-dispatch
    and silently discarded a card the interactive session had committed seconds
    earlier, blaming it on the dispatched worker in the log line below even
    though that worker had made no git call at all (confirmed from its
    transcript). Recovered by hand from the reflog; nothing caught it on its own.

    So the reset fires only when something *outside* `board.board_rel(root)`
    changed between `expected_sha` and now. That is the actual shape of the
    2026-07-25 defect this guards — a worker landing game or framework code on
    the shared branch — and `Board/` is already the one root a worker's own
    writable roots deliberately allow on `base` (`_worker_env`, for its own
    `## Thread` note). A commit confined to `Board/` is ordinary bookkeeping
    racing the dispatch window and is left alone; anything else resets `base`
    back to `expected_sha`, discarding it and any uncommitted files with it — the
    worktree on `ai/<id>` is a separate checkout and is never touched.
    A no-op (returns False) when `base` is where the runner left it, or when
    everything since is confined to `Board/`. No LLM (§12): `rev-parse` plus,
    only if that differs, a name-only `diff` and, only if that finds a non-board
    path, a reset.
    """
    if not expected_sha:
        return False
    now = git.run(root, "rev-parse", base).stdout.strip()
    if now == expected_sha:
        return False
    changed = git.changed(root, expected_sha, now)
    board_prefix = board.board_rel(root).as_posix().rstrip("/") + "/"
    if changed and all(path.startswith(board_prefix) for path in changed):
        # Everything `base` picked up since dispatch started lives under
        # `Board/` — not the wrong-checkout defect, so leave it alone rather
        # than discard someone else's real work for no defect at all.
        return False
    # `reset --hard` on the main checkout discards the stray commit *and* any
    # uncommitted files the worker wrote there — both are the defect, both go. The
    # worktree on `ai/<id>` is a separate checkout and is not touched.
    git.run(root, "reset", "--hard", expected_sha)
    return True
# Publish — the cloud topology's missing half
# --------------------------------------------------------------------------
#
# Everything above commits locally: the board, merged cards on `base`, and each
# card's `ai/<id>` branch. That was always enough on Karel's laptop, where the
# checkout *is* his repo — the branch is already somewhere he can reach it. It
# is not enough when the run happens in an ephemeral clone (a cloud routine): and a card parked in
# `needs-decision/` leaves its `ai/<id>` work stranded in a container Karel has
# no way to open. Publishing closes that gap by pushing what the run produced
# to a remote, so `git fetch && git checkout ai/<id>` from any other machine is
# all it takes to continue — "the branch with the digest", as Karel put it.

def _card_branches(root: Path) -> list[str]:
    """Every local `ai/<id>` branch, in the same lightweight per-card namespace
    `prepare_worktree`/`drop_worktree` already use. A card that merged and had
    `rebase_and_merge` delete its branch does not appear — correct, since its
    work is already on `base`, which is published in its own right.

    That absence is what keeps `publish` from resurrecting a merged card's
    branch, but it is not what removes the copy already on the remote: nothing
    here reaches across, and until 2026-08-09 nothing did, so every published
    card left an orphaned `<remote>/ai/<id>` behind forever. Deleting that is
    `rebase_and_merge`'s job, at the moment it deletes the local ref and while it
    still has the tip to check the remote's against."""
    out = git.run(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/ai/")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]



def publish(root: Path, remote: str, base: str, trusted_branch: str = "") -> None:
    """Push `base` and every live `ai/<id>` branch to `remote`, so a night's work
    survives an ephemeral checkout — and, since 2026-08-08, so a card that
    succeeded is on the remote without anyone remembering to push it. A no-op
    when `remote` is empty (the schema default, for a host that has not opted in)
    or when `remote` is not configured in this checkout.

    **The empty default is not a statement that a persistent box should not
    push.** It was originally read that way — a laptop's checkout *is* the
    maintainer's repo, so a branch there is already reachable — and that reading
    was wrong about what the remote is for: reachable-on-this-disk is not the
    same as backed up, visible from another machine, or survivable. Dungeoneer's
    laptop declares `publish_remote: "origin"` for that reason (Karel,
    2026-08-08: *"Pushing to test after card success should be automatic"*), so
    a persistent host opting in is now the expected case, not the exception.
    What stays deliberately manual is everything downstream of `base` — no
    integration branch is merged anywhere, and no stable branch is touched.

    `base` is pushed **without force**: a rejection means something else (Karel,
    or another machine) moved `origin/<base>` since this checkout last saw it,
    and guessing which side wins is exactly the kind of decision §12 reserves for
    a human — so it is logged loudly and left for `git pull`/rebase by hand, never
    forced.

    Card branches are more delicate, and split into two cases:

    * `trusted_branch` — the branch of the card *this checkout just dispatched,
      this very call* (the caller in `run()` passes `f"ai/{card.id}"` right after
      settling it). This checkout has complete, current knowledge of it — nothing
      else could have touched it in between (the runner's single-instance lock)
      — so it is force-pushed with a plain `--force-with-lease` and nothing more.
      This matters because a cold-start retry (`prepare_worktree`'s FRESH path)
      deletes the old local branch and recreates it fresh from `base`, so a
      second attempt's commits are git-history **siblings** of a first attempt's,
      not descendants — the ancestry check below would misfire on that
      completely ordinary case if it applied here too.
    * every other live `ai/<id>` branch — a dormant leftover this checkout did
      NOT just work on: a card parked days ago, or one another machine picked up
      since. Here `--force-with-lease` alone is **not** the safety net it looks
      like: it only refuses when *something else moved the remote since this
      checkout last looked*, using its (possibly long-stale, or entirely absent)
      local `refs/remotes/<remote>/<branch>` as the expected value. A checkout
      that has simply never talked to `remote` about this branch before — the
      exact case of a dead local `ai/<id>` left over from an attempt whose real,
      later work was dispatched and pushed from somewhere else entirely — has no
      opinion to contradict, so the lease waves the push through and silently
      overwrites the remote's real commits with this checkout's stale ones.
      Verified against a real bare remote (`menu-unlock-indicators`, 2026-07-28):
      a local branch that never fetched the remote's tip force-pushed straight
      over newer, unrelated work with no complaint at all. So each of these gets
      a fresh, targeted `git fetch` immediately before its push, and is
      force-pushed only when the remote's current tip **is an ancestor of** the
      local branch (this checkout's history is a strict superset — the ordinary
      case, or the remote has no such branch yet). When the remote's tip is NOT
      an ancestor — it carries commits this checkout does not have, diverged or
      newer — the push is refused and logged loudly rather than guessed at; that
      is a human reconciliation, same as a rejected `base` push.

    A pushed card branch is not permanent. `rebase_and_merge` deletes it on this
    same remote when the card's work merges (2026-08-09), so what accumulates
    here is only the branches of cards still in flight — parked, in review, or
    failed — not one ref per card ever run. The ancestry gate below and the one
    guarding that delete look alike and are not the same check: this one asks
    whether the *remote* may be overwritten by local history, that one whether
    the *remote's* history is already contained in what merged.

    Every failure is logged and swallowed — same posture as `_log`'s own file
    tee: a push problem is an observability gap, not a reason to end the night.
    """
    if not remote:
        return
    if git.run(root, "remote", "get-url", remote).returncode != 0:
        hostconfig._log(f"  publish: remote `{remote}` is not configured in this checkout — skipping")
        return

    pushed_base = git.run(root, "push", remote, base)
    if pushed_base.returncode != 0:
        detail = (pushed_base.stderr or pushed_base.stdout or "").strip().splitlines()
        hostconfig._log(f"  ! publish: `{base}` was not pushed to {remote} (not forced) — "
             f"{detail[-1][:150] if detail else 'see git output'}")

    for branch in _card_branches(root):
        if branch != trusted_branch:
            fetched = git.run(root, "fetch", remote, branch)
            if fetched.returncode == 0:
                remote_tip = git.run(root, "rev-parse", "FETCH_HEAD").stdout.strip()
                if not _is_ancestor(root, remote_tip, branch):
                    hostconfig._log(f"  ! publish: `{branch}` NOT pushed — {remote} already carries "
                         f"commits this checkout does not have (diverged, not a stale "
                         f"retry); force-pushing would discard them. Reconcile by hand "
                         f"before publishing this branch.")
                    continue
            # A fetch that fails because the remote has no such branch yet is the
            # ordinary new-card-branch case, not an error — nothing to be an
            # ancestor of, so the push below is a plain create.
        pushed = git.run(root, "push", "--force-with-lease", remote, branch)
        if pushed.returncode != 0:
            detail = (pushed.stderr or pushed.stdout or "").strip().splitlines()
            hostconfig._log(f"  ! publish: `{branch}` was not pushed to {remote} — "
                 f"{detail[-1][:150] if detail else 'see git output'}")
