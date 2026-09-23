"""Host- and run-level constants, the single-instance lock, status/log
plumbing, and repo-path helpers -- the ground floor the rest of the split
runner modules build on.

No card, worktree or dispatch logic here: `host_config`/`host_setting` read
`[worker]`/`[host]` off the manifest and `.ai/hosts.json`, `acquire_lock`/
`release_lock` are the one-run-at-a-time guarantee, `_status`/`_log` are the
`--status` heartbeat and the run log tee, and the rest (`repo_root`,
`harvest_dirs`, `fence_env`, ...) resolve where things live on disk. Imported
by every other split module; imports none of them back.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import subprocess
from pathlib import Path

from nightshift import board, branches, git, landing, textio
from nightshift import manifest as _manifest
from nightshift.manifest import ManifestError, find_root

def _worker(root: Path):
    """The `[worker]` table, or its all-empty default.

    A default rather than a `require()`: none of these three fields *bounds*
    behaviour the way `branches.integration` does. Undeclared means "no harvest
    dirs, no fence, a derived checkout name" — all of which are working
    configurations for a project whose workers only ever commit.
    """
    try:
        return _manifest.load(root).worker
    except ManifestError:
        return _manifest.Worker()


def _project_name(root: Path) -> str:
    try:
        return _manifest.load(root).project.name or root.name
    except ManifestError:
        return root.name


STOP_FILE = Path(".ai/STOP")

#: Returned by `main()` when the run never started because the kill switch was
#: already there — the one refusal that means "this went exactly as asked",
#: never "something is wrong". Distinct from the generic `1` every other
#: preflight refusal returns, because `panel.dispatch_cards` runs one runner
#: process per queued card: after a wall-sleep lands a card and the sequence
#: moves on to the next one, a `.ai/STOP` dropped during that sleep refuses the
#: *next* card's preflight — and without this, that refusal's plain `1` read
#: exactly like a crash, so `jobs.state()` reported a deliberate stop as
#: `failed`. Deliberately not `2` or `3` — both are already in use in tests
#: (and `2` in `jobs.main`) as generic "some other nonzero" placeholders, and
#: reusing one would make `jobs.state()` mistake an unrelated failure for a
#: deliberate stop.
EXIT_STOPPED = 42

LOCK_FILE = Path(".ai/runs/.lock")
RUNS = Path(".ai/runs")
# The heartbeat. One small JSON file, overwritten at every phase transition, so
# `--status` can answer "where is it" from disk alone — same principle as the
# rest of the runner's state (§ "Resumable from disk alone").
STATUS_FILE = Path(".ai/runs/status.json")
# Committed, keyed by hostname. HOST_FILE is an optional untracked override.
HOSTS_FILE = Path(".ai/hosts.json")  # gate-ok(source_reference_liveness): committed
# in a project with a Board/; this repo dispatches nothing, so it has none of its own.
HOST_FILE = Path(".ai/host.json")

def harvest_dirs(root: Path) -> tuple[Path, ...]:
    """Gitignored directories a worker may fill with artefacts that are NOT
    commits.

    The art pipeline is the case: candidates land in `assets/.tmp/` and only
    move into `assets/` after a human approves, so an art card's real output
    would otherwise be destroyed with the worktree and would look like "the
    worker did nothing".

    `[worker].harvest_dirs`; empty when a project declares none, which is the
    right answer for a project whose workers only ever commit. The convention
    is what the runner reasons about — `art` appears nowhere in its logic (D1),
    which is what makes the producer/checker loop shippable as core.
    """
    return tuple(Path(p) for p in _worker(root).harvest_dirs)


def neighbours_dir(root: Path) -> Path | None:
    """What a `checker:` is shown alongside the artefacts under review — the
    approved siblings the new one has to sit next to.

    Derived from the first harvest dir rather than declared, because the
    relationship is the one `harvest_dirs` already documents: candidates land
    in `<assets>/.tmp/` and move into `<assets>/` once approved. A second
    manifest field would be a second spelling of the same fact, free to drift
    from it. `None` when the project declares no harvest dir, in which case the
    checker sees only the artefacts.
    """
    dirs = harvest_dirs(root)
    return (root / dirs[0]).parent if dirs else None

# A card gets this many dispatches before it is filed as failed. Three is not a
# tuned number — it is "one flake, one real retry, then stop burning the night."
MAX_ATTEMPTS = 3
# How many review-owed cards the end-of-night drain will conclude in one run.
# `drain.py`'s recorded objection to draining inside a night is that the lane is
# unbounded while the night's accounting is not; a cap is the answer to that
# rather than a rebuttal of it. Four is "last night's backlog, twice" — enough
# that a normal night never leaves anything owed, small enough that a lane that
# has quietly grown to thirty cannot turn a run into a review marathon. The rest
# wait for `python -m nightshift.drain`, which is unbounded on purpose.
DRAIN_CAP = 4
# A `kind: chore` gets exactly one, and the arithmetic is the argument: a batch of
# eight one-prompters at three attempts each is a night, at one attempt each it is
# an hour. A failed one-prompter is also the more useful artefact — it is worth a
# human's eye, because a second dispatch of a prompt that already produced the
# wrong thing usually produces it again. Read by `select` (which refuses to
# re-queue a spent chore) and by `settle` (which retires one on its first failure
# rather than leaving it in `tasks/` where nothing would ever pick it up again).
CHORE_MAX_ATTEMPTS = 1
# Producer→checker iterations *inside* one dispatch, for a card that names a
# `checker:`. Distinct from MAX_ATTEMPTS on purpose: an attempt is a dispatch of
# the card and is what `failed/` counts; a round is one make-then-judge cycle
# within it. Conflating them would make `attempts` mean different things
# depending on the worker.
MAX_ROUNDS = 3
# There used to be a flat time-based backoff here (0, 20, 90 minutes, indexed
# by attempts already made) before a card could be re-dispatched. Removed
# 2026-08-26 (Karel: *"I'm not generally convinced that cooldown is helpful in
# any scenario"*): a clock is the wrong instrument for a queue that already has
# an ordering mechanism. `board.dispatch_order`'s `last_outcome` bucket does the
# job it was standing in for — a card that just needs a mechanical fix sorts to
# the *front* instead of waiting out a timer that has nothing to do with whether
# the fix is ready, and a card that hard-failed sorts to the *back* instead of
# being frozen out of the run entirely, so it still gets a turn once healthier
# work has had first pick. Neither case benefits from wall-clock delay: waiting
# does not make a `needs_fix` fix any more ready, and a `failed` card sitting
# at the tail of the same run is exactly as safe as one that waited 90 minutes
# for a *different* run to pick it up, without spending the window on nothing.

def repo_root() -> Path:
    """The project this run is against.

    **Found, not derived.** Until 07_portability.md §8 step 4 this was
    `Path(__file__).parent.parent`, which was exact while the module sat in
    `.ai/`; installed into site-packages that answer is a directory inside the
    virtualenv. `find_root` walks up from the working directory and raises
    rather than falling back to it, because the silent direction here is a
    runner that dispatches cards against an empty tree.

    A function, not a module constant: importing the runner must not require
    being inside a project — the framework's own test suite imports it.
    """
    return find_root()


def forbidden_bases(root: Path | None = None) -> frozenset[str]:
    """Branches the runner will never build on or commit to.

    Derived from `[branches]` in the manifest, which is the one place a branch
    name is bound to a role — see `nightshift.branches` for why this used to be
    a hardcoded frozenset and what broke when the roles were due to swap.

    A function rather than the module-level constant it was until
    07_portability.md §8 step 4: reading a manifest at import time would make
    importing this module fail in any repo that has not declared `[branches]`,
    which is every test fixture that builds a synthetic tree.
    """
    return branches.forbidden_bases(root or repo_root())


def default_base(root: Path | None = None) -> str:
    """The branch the runner builds on unless `--base` says otherwise."""
    return branches.integration(root or repo_root())

# The always-on diff reviewer (automate-review-step). A new, distinct pipeline
# stage — NOT the `checker:` producer/retry loop — that runs after gates+tests
# pass and turns a two-way verdict into a routed lane. Named here rather than in
# a card's frontmatter because it is not a card worker: the runner dispatches it
# itself on every code card, the way it dispatches `stale-hunter`. Resolved
# against a real charter at `.claude/agents/code-reviewer.md`.
REVIEWER_AGENT = "code-reviewer"

# The rebase-conflict resolver (merge-conflict-has-no-owner). Like the reviewer it
# is dispatched by the runner rather than named on a card, and for the same reason:
# it is a pipeline stage, not somebody's worker. See `_resolve_conflict`.
RESOLVER_AGENT = "merge-resolver"

#: How many times `_resolve_conflict` will hand one card's rebase to the resolver.
#: A rebase replays commit by commit and each one can conflict, so the bound is per
#: *pause*, not per card — but it is a bound, because "resolve, continue, conflict
#: again" with no ceiling is how an unattended loop spends a night on one card.
#: Three covers every multi-commit collision observed so far (the worst was a
#: 3-commit branch conflicting on its first).
MAX_RESOLVE_ROUNDS = 3

def fence_env(root: Path) -> str:
    """The worktree fence's activation switch
    (`nightshift/hooks/worktree_fence.py`). Set in a dispatched worker's
    environment to the `os.pathsep`-joined roots it may write to; unset (an
    interactive session) the fence is a no-op.

    `[worker].fence_env`. The hook cannot import this — a worker reads
    `.claude/settings.json` from its own worktree — so it reads the same
    manifest field rather than duplicating a spelling. `""` when undeclared,
    which leaves the fence permanently off: the safe direction, and the same
    one every other failure path in that hook takes.
    """
    return _worker(root).fence_env

# The runner's own dedicated checkout of the integration branch (runner-hardening
# #3). A sibling git worktree, permanently on `development_team`, so the runner can
# commit the board, cut worktrees and merge reviewed cards **without holding the
# branch in Karel's main checkout** — which frees him to work on his own branch
# during a run. Git forbids one branch in two worktrees at once, so this only
# activates when the launch checkout is OFF the integration branch; when Karel is
# still on it the runner operates in-place, as before, and says so. Placed beside
# the repo (never inside it) for the same reason as `worktree_root`: a checkout
# under `Board/`'s tree would be walked by `doc_scan`/`card_schema` and every card
# would appear twice.
#
# `[worker].integration_checkout_dir`, falling back to `.<project>-integration`
# — a name derived from the project rather than a shared literal, because two
# projects being run from sibling checkouts must not collide on one directory
# beside them.
def integration_checkout_dir(root: Path) -> str:
    declared = _worker(root).integration_checkout_dir
    return declared or _manifest.sibling_dir_name(_project_name(root), "integration")

# Dollar caps, **off by default since 2026-07-23**. On a subscription plan
# `total_cost_usd` is the API-equivalent price of the tokens rather than money
# anyone spends, so a dollar stopping condition ended the night at a point
# unrelated to the only limit that exists — and, worse, was the thing quietly
# protecting the queue from the unrecognised-wall bug, by stopping the run before
# the plan limit could. Session windows are the unit now (`--sessions`,
# `limits.py`); a runaway worker is bounded by the wall-clock timeout on its
# process. These remain for anyone on API billing, where the number is real.
# 0 means no cap, and no `--max-budget-usd` is passed to the CLI at all.
DEFAULT_CARD_BUDGET_USD = 0.0
DEFAULT_RUN_BUDGET_USD = 0.0

# How many usage-limit windows one night may spend. The unit is Karel's — "until
# two session limits are used" — because it is the unit the plan is sold in and
# the only one that maps to "work through the night". 1 stops at the first wall;
# 2 sleeps through the first reset and stops at the second.
DEFAULT_SESSIONS = 1

# Dispatches that may fail — or be interrupted, which is the same absence of a
# verdict — back-to-back before the night ends. This is the net under
# `limits.detect` rather than a feature in its own right: a wall phrased in words
# the detector does not carry looks exactly like every card failing, and without
# this the runner would walk the whole queue spending an attempt on each. Three,
# because three unrelated cards failing in a row is already not about the cards.
CONSECUTIVE_FAILURE_STOP = 3

# Transient 429s the night will wait out before deciding they are not transient.
# Without a cap, a rate limit misread as a hiccup retries every five minutes
# until morning and the night produces nothing but sleep.
TRANSIENT_RETRIES = 3

# Consecutive limit-interruptions that leave the worktree byte-identical before a
# limit-interrupted card is declared *stuck* and filed to `failed/`. The guard
# from `runner-worker-handover` Decision 2: a real limit reopens and the worker
# makes progress, so no-progress across resumes is the *misclassification* case
# (a per-card crash a `_WALL` phrase wrongly matched) or resume silently not
# persisting — either way a card that never advances must leave the head of the
# queue rather than being given its attempt back forever. Two, so a single
# reopened window too small to do anything in is forgiven once (the attempt is
# still spent), and the second empty window is decisive. Detected by a
# working-tree diff-hash — no LLM (§12).
NO_PROGRESS_STOP = 2

# Concurrent kept worktrees allowed above the ordinary one-per-dispatch case
# (`runner-prune-run-dirs`, Decision 3 named in `runner-worker-handover`). A
# worktree only outlives one dispatch when a card is limit-interrupted and still
# in `tasks/` (warm resume) — this is the backstop for that backlog, not `done/`,
# which is not where a worktree normally dies. A small number: a limit-interrupted
# backlog bigger than this is a signal to stop dispatching, not to hoard
# checkouts. The top of the "2-3" range both sibling cards independently proposed
# — a demotion never loses work (the branch banks it), so the only cost of the
# looser end is disk.
WORKTREE_KEEP_CEILING = 3

# How many days of `<date>.log` tees to keep. The card names the rule ("keep the
# last N days") but not the number, and no acceptance criterion rides on the
# exact value — a plain two-week default rather than parking a non-blocking
# implementation detail.
LOG_RETENTION_DAYS = 14


# Set once by `run()`. Every `_log` line is teed here as well as to stdout,
# because stdout is wherever the night was launched from — a terminal that gets
# closed, or a harness temp file whose path nobody remembers. The first question
# asked of a finished run is "what happened", and the answer has to outlive the
# console it was printed to.
_RUN_LOG: Path | None = None

# The runner's two roots, set once by `run()` (runner-hardening #3):
#   _CTRL_ROOT — the *launch* checkout (Karel's main working copy, the vault). It
#                owns the control plane: the STOP kill switch, `status.json`, the
#                lock and the log tee — everything Karel reaches from where he
#                started the runner and drops the kill switch from his phone.
#   _WORK_ROOT — where the board and git work happen. In the ordinary new topology
#                that is the dedicated `development_team` checkout; when the launch
#                checkout is still on the integration branch the two are equal.
# `_status` writes to `_CTRL_ROOT` and `_stop_requested` checks *both*, so the kill
# switch trips whether it lands in the vault or the dedicated checkout. Kept as
# module globals rather than threaded through every `_status` call for the same
# reason `_RUN_LOG` is — the alternative is a second root argument on a dozen deep
# functions that only ever want the one value `run()` already fixed.
_CTRL_ROOT: Path | None = None
_WORK_ROOT: Path | None = None


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _open_run_log(root: Path) -> None:
    global _RUN_LOG
    path = root / RUNS / f"{dt.date.today().isoformat()}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    _RUN_LOG = path


def _log(message: str) -> None:
    line = f"[{dt.datetime.now():%H:%M:%S}] {message}"
    print(line, flush=True)
    if _RUN_LOG is None:
        return
    try:
        with _RUN_LOG.open("a", encoding="utf-8", newline="") as fh:
            fh.write(line + "\n")
    except OSError:
        # The console is the primary. A full disk or a locked file must not be
        # the thing that ends a night — losing the tee is survivable, and
        # raising here would take down a run that is otherwise fine.
        pass


# Late-bound, so a test that monkeypatches `runner._log` also captures landing's lines.
landing.set_log(lambda message: _log(message))


def _stop_requested() -> bool:
    """Has the kill switch appeared in either root?

    Checked in the control root (the vault, where Karel drops `.ai/STOP` from his
    phone) *and* the work root (the dedicated checkout), so the switch trips
    wherever the file lands — the two directories are separate working trees once
    the runner uses a dedicated checkout, and a switch that only watched one of
    them would be a promise the runner could quietly break. Returns False before
    `run()` has set the roots, which is correct: nothing is dispatching yet.

    Single-use: the file is deleted the moment it is seen, in whichever root(s)
    carry it. Every call site checks this once and then ends the loop — by the
    time `True` comes back the stop has already been honoured, so there is
    nothing left for a human, or the next start, to clean up.
    """
    found = False
    for r in (_CTRL_ROOT, _WORK_ROOT):
        if r is not None and (r / STOP_FILE).is_file():
            found = True
            try:
                (r / STOP_FILE).unlink()
            except OSError:
                pass
    return found


def _status(root: Path, **fields: object) -> None:
    """Overwrite the heartbeat at `.ai/runs/status.json`.

    Answers "where is it right now, and how long has it been there" without
    reading a log or knowing a temp path — the question that had no command on
    the runner's first real night, when a card ran 28 minutes in total silence
    and there was no way to tell working from stuck.

    Deliberately a full overwrite of a tiny file rather than an append: this is
    *current state*, the log is already the history, and a torn write of two
    hundred bytes costs one refresh rather than a corrupted record. Failures are
    swallowed for the same reason as the log tee — the heartbeat is an
    observer, and an observer that can end the run is worse than none.
    """
    payload: dict[str, object] = {"pid": os.getpid(), "updated": _now(), **fields}
    # The heartbeat belongs in the control root — the launch checkout Karel runs
    # `--status` from — even though most callers pass the work root. Falls back to
    # the caller's root before `run()` has fixed the globals (dry-run, direct tests).
    path = (_CTRL_ROOT or root) / STATUS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        textio.write_text_lf(path, json.dumps(payload, indent=2))
    except OSError:
        pass


# --------------------------------------------------------------------------
# Host capabilities — the `requires:` precondition (03_board.md §2)
# --------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def host_config(root: Path) -> dict:
    """This machine's capabilities and permission posture.

    **Committed and keyed by hostname** (`.ai/hosts.json`). The first version of
    this was a single untracked `host.json`, on the reasoning that `gpu-box` is
    true on the desktop and false on the laptop so a tracked answer would be
    wrong on one of them. That reasoning was right about the *fact* and wrong
    about the *fix*: keying on hostname makes one committed file correct on every
    machine, and it means a box is configured once and then syncs, rather than
    being a setup step that has to be remembered per clone and is silently
    missing until something fails to dispatch.

    `.ai/host.json` is still read, as an untracked per-machine **override**, for
    the case where you want a different answer on one box without committing it.

    An unknown hostname resolves to nothing, which is the safe default: a card
    that `requires:` something simply never dispatches, rather than being sent to
    a machine that cannot run it.
    """
    if override := _load_json(root / HOST_FILE):
        return override
    hosts = _load_json(root / HOSTS_FILE)
    entry = hosts.get(socket.gethostname())
    return entry if isinstance(entry, dict) else {}


def host_capabilities(root: Path) -> set[str]:
    found = host_config(root).get("capabilities", [])
    return set(found) if isinstance(found, list) else set()


def host_setting(root: Path, key: str, default: object) -> object:
    return host_config(root).get(key, default)


#: Permission modes under which a headless agent cannot write a file: `default`
#: prompts for the edit and a `-p` session has nobody to answer, `plan` forbids
#: edits outright by design.
#:
#: **Named here because it was a category with one member.** While `default` was
#: the only answer, "cannot edit" and `"default"` were the same string, so each
#: consumer spelled the string out — `update.merge` still did on 2026-08-17 — and
#: the arrival of a second member meant every consumer had to be recalled from
#: memory rather than found by following a reference. That is
#: `category-of-one-read-as-a-literal`, and this is the reference.
#:
#: Enumerated rather than "anything outside the allowed set", so a mode the CLI
#: adds later fails open: a verb that refuses to start on an unrecognised string
#: is worse than one that tries and reports what happened.
MODES_WITHOUT_EDIT: tuple[str, ...] = ("default", "plan")


def cannot_edit(root: Path) -> str:
    """The configured permission mode's name if it cannot write a file, else "".

    The question two verbs ask before they spend — `ingest` before the scribe,
    `update` before a merge — because both dispatch an agent whose entire job is
    to write one.
    """
    mode = str(host_setting(root, "permission_mode", "acceptEdits"))
    return mode if mode in MODES_WITHOUT_EDIT else ""


# --------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError) as exc:
        return isinstance(exc, PermissionError)
    return True


def acquire_lock(root: Path) -> bool:
    """One runner at a time. A stale lock (dead PID) is taken over, not honoured
    — the 3 AM reboot case leaves exactly that, and a runner that refused to
    start because of it would need a human, which defeats the point."""
    path = root / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:
            held = int(path.read_text(encoding="utf-8").split(None, 1)[0])
        except (ValueError, IndexError):
            held = -1
        if _pid_alive(held):
            _log(f"another runner holds the lock (pid {held}) — exiting")
            return False
        _log(f"stale lock from pid {held} (not running) — taking over")
    textio.write_text_lf(path, f"{os.getpid()} {_now()}\n")
    return True


def release_lock(root: Path) -> None:
    (root / LOCK_FILE).unlink(missing_ok=True)
def current_branch(root: Path) -> str:
    return git.run(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def dirty_outside_board(root: Path) -> list[str]:
    """Uncommitted work that is not the board's.

    The runner commits `Board/` and the generated views, never `-a`, so it cannot
    sweep up someone else's work in progress — but it also branches worktrees off
    HEAD, and a half-finished change sitting unstaged means HEAD is not what the
    maintainer thinks they left. Refusing is cheaper than explaining it in the
    morning.

    **Every `board.GENERATED_VIEWS` file is exempt.** Each is rewritten by the
    command that owns it, often moments before a dispatch — the inbox is routed and a
    batch is planned right before it runs — so treating one as somebody's work in
    progress refuses the very run that produced it. Measured 2026-08-14: `Routing.md`
    and `Chores.md` at the repo root blocked `chores` outright, because each shipped a
    new view and joined neither this list nor `commit_board`'s.

    Nothing is exempt outside that list. An editor's own per-machine state used to be
    (`.obsidian/`, while `init` provisioned a vault), which is now a `.gitignore` line
    in `templates/gitignore` instead — an untracked directory git never reports cannot
    make this cry wolf, and an exemption is the wrong tool for a file that should not
    have been in git in the first place.
    """
    dirty = []
    for _, path in git.status(root):
        if path.startswith("Board/") or path in board.GENERATED_VIEWS:
            continue
        dirty.append(path)
    return dirty
