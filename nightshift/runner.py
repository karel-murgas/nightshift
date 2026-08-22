#!/usr/bin/env python3
"""The overnight runner — a dumb orchestrator (`09_runner.md`).

Rescan the board, pick what is dispatchable, dispatch it, run the gates over the
result, move the card, commit, write the digest. **Zero LLM calls in the runner
itself** (`00_architecture.md` §5, §12): every branch below is a file-state
lookup or a subprocess exit code.

That rule is worth stating precisely, because this script does start a Claude
process. §12 forbids the orchestrator from making *decisions* with an LLM. The
worker is the thing being orchestrated, not a decision procedure the runner
consults — the runner never asks a model whether to retry, which card to take,
or whether output is good. It asks `pytest` and `nightshift.gates.run`.

**Resumable from disk alone.** Nothing survives in memory. Windows reboots at
3 AM; the next boot rescans `Board/` and reconstructs everything, because the
only state is where a card sits plus four runner-owned frontmatter fields
(`03_board.md` §2). The corollary that shapes the code: `attempts` is
incremented and committed *before* dispatch, so a crash loop is bounded even if
the crash happens mid-worker.

Usage:
    python -m nightshift.runner --dry-run       # what would be dispatched, and why not
    python -m nightshift.runner --max-cards 1   # one card, then stop
    python -m nightshift.runner --until 06:30   # overnight; stop before the morning
    python -m nightshift.runner --sessions 2 --until 07:00   # spend two session windows

**What ends a night is the plan's session window, not a dollar figure**
(`limits.py`). `--sessions 1`, the default, works until the usage limit is
reached and stops. `--sessions 2` sleeps through the first reset and stops at the
second wall. The card in flight when a wall lands is *not* blamed for it: its
attempt is given back and it is retried in the next window.

Kill switch: create `.ai/STOP`. The runner exits at the top of the loop without
dispatching. The vault syncs, so dropping that file from the phone stops tonight.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board          # the card model
from nightshift import branches       # branch roles
from nightshift import digest
from nightshift import gitmerge       # merge strategy + failure reporting, one home
from nightshift import gitpaths       # git's path lists, read NUL-separated
from nightshift import limits
from nightshift import manifest as _manifest
from nightshift import reconcile
from nightshift import run_record
from nightshift import stale_sweep
from nightshift import suite          # test policy
from nightshift import textio         # LF-pinned writes (gate write_newline)
from nightshift import tiers
from nightshift import worker_prompt  # harness-level worker discipline
from nightshift.manifest import AI_DIR, ManifestError, find_root

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
# the card and is what backoff and `failed/` count; a round is one make-then-judge
# cycle within it. Conflating them would make `attempts` mean different things
# depending on the worker.
MAX_ROUNDS = 3
# Backoff before re-dispatching a card that failed, indexed by attempts already
# made. A table, not a formula: §5 says the runner's decisions are lookups.
BACKOFF_MINUTES: tuple[int, ...] = (0, 20, 90)

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
    return declared or f".{_project_name(root)}-integration"

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


def _stop_requested() -> bool:
    """Has the kill switch appeared in either root?

    Checked in the control root (the vault, where Karel drops `.ai/STOP` from his
    phone) *and* the work root (the dedicated checkout), so the switch trips
    wherever the file lands — the two directories are separate working trees once
    the runner uses a dedicated checkout, and a switch that only watched one of
    them would be a promise the runner could quietly break. Returns False before
    `run()` has set the roots, which is correct: nothing is dispatching yet.
    """
    for r in (_CTRL_ROOT, _WORK_ROOT):
        if r is not None and (r / STOP_FILE).is_file():
            return True
    return False


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


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # `encoding=` is not optional on Windows — see the note in
    # `.ai/gates/deletion_sweep.py._git` for the failure it caused on 2026-07-23.
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def current_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def dirty_outside_board(root: Path) -> list[str]:
    """Uncommitted work that is not the board's.

    The runner commits `Board/` and the generated views, never `-a`, so it cannot
    sweep up someone else's work in progress — but it also branches worktrees off
    HEAD, and a half-finished change sitting unstaged means HEAD is not what the
    maintainer thinks they left. Refusing is cheaper than explaining it in the
    morning.

    **Every `board.GENERATED_VIEWS` file is exempt, not just the digest.** Each is
    rewritten by the command that owns it, often moments before a dispatch — the
    inbox is routed and a batch is planned right before it runs — so treating one
    as somebody's work in progress refuses the very run that produced it. Measured
    2026-08-14: `Routing.md` and `Chores.md` at the repo root blocked `chores`
    outright, because each shipped a new view and joined neither this list nor
    `commit_board`'s.
    """
    dirty = []
    for _, path in gitpaths.status(root):
        if path.startswith("Board/") or path in board.GENERATED_VIEWS:
            continue
        if path.startswith(".obsidian/"):
            # The editor's own state, and the same phenomenon as the `Board/`
            # exemption one directory over: somebody looking at the board while a
            # session runs. `workspace.json` in particular is rewritten on nearly
            # every interaction, so a repo that committed it once is dirty forever
            # and can never dispatch again — reported from a real install,
            # 2026-08-04, three times in one day.
            #
            # Safe to skip because the reason for refusing does not apply: this is
            # never "a half-finished change means HEAD is not what you left". No
            # worktree, gate, test or card reads a byte of it. `templates/gitignore`
            # keeps the volatile files out of git in the first place; this is what
            # rescues a repo that tracked one before that shipped.
            continue
        dirty.append(path)
    return dirty


def claude_binary() -> str | None:
    """`claude` is not always on PATH — on this box it lives in
    `%USERPROFILE%\\.local\\bin`. Checked rather than assumed, because a runner
    that discovers this at 3 AM has already moved a card and bumped `attempts`."""
    if override := os.environ.get("CLAUDE_BIN"):
        return override if Path(override).is_file() else None
    if found := shutil.which("claude"):
        return found
    fallback = Path(os.path.expanduser("~")) / ".local" / "bin" / (
        "claude.exe" if os.name == "nt" else "claude")
    return str(fallback) if fallback.is_file() else None


def ensure_workspace_trusted(root: Path) -> None:
    """Mark the repo trusted in `~/.claude.json` before any worker is dispatched.

    Headless `claude -p` has no trust dialog, so an untrusted workspace does not
    stop — it *degrades silently*: the worker drops every `permissions.allow` and
    `additionalDirectories` entry (2026-07-25: a whole run's dispatches ran with
    149 allow + 9 dir entries ignored, the warning buried in each worker's
    stderr). Trust is deliberately machine-local — it lives in `~/.claude.json`,
    never the repo, because a repo cannot vouch for itself — so the only fix that
    travels to every machine is code that writes the flag itself.

    Keys off the resolved repo root (Claude resolves a worktree's trust to its
    git root, so this covers every `.<project>-worktrees/<card>` checkout too).
    Idempotent: writes at most once per machine, then no-ops, which also keeps it
    from racing another Claude process's writes to the same file. Defensive: a
    missing file, unreadable JSON or an unwritable home degrades to the same
    dropped-permissions warning it is trying to prevent — not worth failing a
    night over — so every failure is swallowed.
    """
    config = Path.home() / ".claude.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    target = str(root.resolve()).replace("\\", "/")
    projects = data.setdefault("projects", {})
    keys = [k for k in projects if k.replace("\\", "/").lower() == target.lower()] \
        or [target]
    changed = False
    for key in keys:
        proj = projects.setdefault(key, {})
        if proj.get("hasTrustDialogAccepted") is not True:
            proj["hasTrustDialogAccepted"] = True
            changed = True
    if not changed:
        return
    try:
        textio.write_text_lf(config, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        _log("marked the workspace trusted in ~/.claude.json — headless `-p` has no "
             "trust dialog, and an untrusted workspace silently drops the worker's "
             "permissions.allow / additionalDirectories entries")
    except OSError:
        pass


@dataclass
class Preflight:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def preflight(root: Path, base: str, dry_run: bool) -> Preflight:
    reasons: list[str] = []

    if (root / STOP_FILE).is_file():
        note = (root / STOP_FILE).read_text(encoding="utf-8").strip()
        reasons.append(f"kill switch: {STOP_FILE.as_posix()} exists" + (f" — {note}" if note else ""))

    forbidden = forbidden_bases(root)
    if base in forbidden:
        reasons.append(f"base branch `{base}` is forbidden — the runner never builds on {sorted(forbidden)}")
    elif _git(root, "rev-parse", "--verify", base).returncode != 0:
        reasons.append(f"base branch `{base}` does not exist")

    # The launch checkout being dirty only matters when the runner will operate
    # *in it* — the in-place fallback, when Karel is still on the integration
    # branch. Once he has moved to his own branch (the new topology), the runner
    # works in a dedicated checkout and his uncommitted edits are none of its
    # business — that separation is the whole point of runner-hardening #3. The
    # dedicated checkout's own cleanliness is checked in `run()`, after it is cut.
    on_base = current_branch(root) == base
    if on_base and (dirty := dirty_outside_board(root)):
        shown = ", ".join(dirty[:4]) + (f" (+{len(dirty) - 4} more)" if len(dirty) > 4 else "")
        note = f"working tree is dirty outside Board/: {shown}"
        # A dry run writes nothing and cuts no worktree, so a dirty tree is
        # information, not an obstacle — and refusing would make the one command
        # meant for "show me the board" unusable exactly when you are editing.
        if dry_run:
            _log(f"note — {note}")
        else:
            reasons.append(note)

    if not dry_run and claude_binary() is None:
        reasons.append("the `claude` CLI was not found (set CLAUDE_BIN, or put it on PATH)")

    try:
        tiers.binding(root)
    except tiers.TierError as exc:
        reasons.append(str(exc))

    return Preflight(ok=not reasons, reasons=reasons)


def schema_violations(root: Path) -> dict[str, list[str]]:
    """`card_schema` violations, grouped by card id.

    A malformed card is not dispatched. It is not a failure either — it is a
    card that is not finished being written, and the digest is where it should
    surface. Running the gate rather than reimplementing its rules is the point:
    the schema has exactly one owner.
    """
    out = subprocess.run(
        [sys.executable, "-m", "nightshift.gates.run", "card_schema"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    grouped: dict[str, list[str]] = {}
    for line in (out.stdout or "").splitlines():
        if "Board/" not in line:
            continue
        location = line.split(" — ", 1)[0]
        card_id = Path(location.split(":", 1)[0]).stem
        grouped.setdefault(card_id, []).append(line.strip())
    return grouped


# --------------------------------------------------------------------------
# Crash recovery
# --------------------------------------------------------------------------

def recover(root: Path) -> list[str]:
    """A card with `started:` and no `finished:` was interrupted.

    Nothing else can produce that combination once the lock is ours: the runner
    clears `started:` on every exit path it controls, so what is left is a
    machine that went away mid-dispatch. `attempts` was already incremented
    before the worker started, which is what stops a reboot loop from retrying
    forever — recovery does not need to guess how far it got.
    """
    notes: list[str] = []
    for card in board.cards(root, "tasks"):
        if not card.fields.get("started") or card.fields.get("finished"):
            continue
        started = card.fields["started"]
        card.write({"started": None, "finished": _now()})
        card.write_section(
            "Error",
            f"Attempt {card.attempts} was interrupted — the runner or the machine went "
            f"away after `started: {started}` and never recorded a result. Recovered on "
            f"the next boot; `attempts` had already been incremented, so this counts.",
        )
        notes.append(f"{card.id}: recovered interrupted attempt {card.attempts}")
    if notes:
        board.commit_board(root, f"board: recover {len(notes)} interrupted attempt(s)")
    return notes


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    card: board.Card
    dispatchable: bool
    reason: str


# The size past which a `tasks/` card has stopped being good worker input.
#
# A card *is* the worker's opening message, and the wrapper around it is only
# ~4.6 KB — so card size and prompt size track each other almost exactly, and
# nothing has bounded either since `worker-prompt-off-argv` retired the argv
# limit. The cost of an oversized card is not a crash, it is attention:
# `menu-unlock-indicators` reached 36 KB, and to fix a pip count its worker had
# to read ~25 KB of superseded history first.
#
# 14 KB because the largest legitimately dense cards in `tasks/` are around
# 11-12 KB (`hack-end-summary` 11.7 KB, `monthly-wall-doc-drift` 11.2 KB — a
# full acceptance split plus a findings list). 14 KB clears those with real
# headroom, so the line fires on genuine growth rather than on a normal big
# card, and sits at well under half the 36 KB that actually hurt.
#
# A named constant rather than an inline literal for two reasons: a comfort
# threshold on a growing board is a number that will move, and `oversize_note`
# cites this name in its message so the reader is told what to change.
CARD_COMFORT_BYTES = 14 * 1024


def card_bytes(card: board.Card) -> int:
    """The size of the input a worker is actually handed for this card.

    `card.text` rather than `path.stat().st_size` for two reasons: the text is
    the thing put in front of a worker, and it is the same number on a checkout
    whose working files carry CRLF. One function because there are now two
    callers — the message below and the digest's record entry — and a signal
    whose two surfaces disagreed about the size would be worse than no signal.
    """
    return len(card.text.encode("utf-8"))


def oversize_note(card: board.Card) -> str:
    """One advisory line about a card grown past `CARD_COMFORT_BYTES`, or `""`.

    **Advisory by construction.** `select()` folds this into the candidate's
    `reason`, which is what `run()` logs per card — it never touches
    `dispatchable`. `run()` also uses "returned something" as its *predicate*
    for `record.oversized`, which is how a dispatched oversized card reaches
    `Digest.md` at all: it is dispatchable, so it is not in `record.skipped`,
    so before that field the digest was silent about exactly the case this
    signal exists for. Reusing this function as the test rather than
    re-deriving the condition is deliberate — it keeps the lane rule and the
    comparison in one place for both readers. That is the
    whole design: gates here have exactly one severity (`Violation`), so a
    blocking version of this would turn the board red on Karel using his own
    board, and a gate that does that is a gate that gets muted. Nothing about a
    card's size makes it undispatchable.

    **`tasks/` only, and that is the subtlety worth stating.** A card grows
    after dispatch and it grows *legitimately*: the runner and close-out append
    `## Summary`, `## Thread`, `## Telemetry` and `## Error`, which is how
    `done/` holds 20-21 KB cards that were half that when they were handed to a
    worker. Measuring any other lane would fire on exactly the cards this has
    nothing useful to say about — noise about the record working as designed.
    The lane check lives here rather than at the call site so that a second
    caller cannot get it wrong; `select()` reads `tasks/` and nothing else, so
    today the check is belt as well as braces.

    Measured on `card.text` rather than `path.stat().st_size` because the text
    is the thing actually handed to a worker, and because it is the same number
    on a checkout whose working files carry CRLF.
    """
    if card.lane != "tasks":
        return ""
    size = card_bytes(card)
    if size <= CARD_COMFORT_BYTES:
        return ""
    return (f"{size / 1024:.1f} KB, over the {CARD_COMFORT_BYTES / 1024:.0f} KB "
            f"CARD_COMFORT_BYTES threshold — compact it, or split it into "
            f"separate cards, before it is dispatched again")


def _backoff_remaining(card: board.Card) -> int:
    """Minutes still to wait before this card may be retried, from `finished:`."""
    finished = card.fields.get("finished")
    if not finished or card.attempts <= 0:
        return 0
    wait = BACKOFF_MINUTES[min(card.attempts, len(BACKOFF_MINUTES) - 1)]
    try:
        when = dt.datetime.fromisoformat(finished)
    except ValueError:
        return 0
    elapsed = (dt.datetime.now() - when).total_seconds() / 60
    return max(0, int(wait - elapsed))


def attempt_limit(card: board.Card) -> int:
    """How many dispatches this card gets before it is retired to `failed/`.

    One function rather than a `MAX_ATTEMPTS` literal at each site, because the
    two places that read it — queue selection and retirement — must agree. They
    disagreeing is the failure this exists to prevent: a chore that selection
    refuses to re-queue but retirement leaves in `tasks/` is a card nothing will
    ever pick up and nothing will ever report.
    """
    return CHORE_MAX_ATTEMPTS if card.kind == board.KIND_CHORE else MAX_ATTEMPTS


def select(root: Path, capabilities: set[str], bad_schema: dict[str, list[str]],
           forced: str | None = None) -> list[Candidate]:
    """Every card in `tasks/`, each with a yes/no and the reason.

    The reasons are returned rather than filtered away on purpose: "nothing was
    dispatchable" and "nothing was dispatched" look identical in a log that only
    records what ran, and telling them apart is the first question anyone asks
    of a quiet night.

    `forced` is the id of a card a human named explicitly (`--card`). For that
    one card the **advisory** checks are waived and the **physical** ones are
    not, which is the only division that makes sense here:

    * waived — `unattended:`, backoff, the attempt limit. Each of these exists to
      decide what may run *with nobody watching*, and someone typing the card's
      id is the watching. `unattended: false` in particular means "a machine
      cannot tell whether this attempt succeeded"; a human asking for it is a
      human volunteering to be the judge.
    * enforced — a broken schema, no worker, no charter, a missing host
      capability. Wanting it harder does not put a GPU in the laptop, and
      dispatching a malformed card produces confident nonsense rather than an
      error.

    A card over `CARD_COMFORT_BYTES` has `oversize_note`'s line appended to
    whichever reason it earned. It is a remark about the card, not a verdict on
    it: an oversized card is dispatchable exactly when it would have been at
    half the size, and the note rides the `reason` field precisely because that
    field is already carried to both readers — the run log and the digest.
    """
    out: list[Candidate] = []
    for card in board.cards(root, "tasks"):
        forced_now = forced is not None and card.id == forced

        def add(dispatchable: bool, reason: str) -> None:
            note = oversize_note(card)
            out.append(Candidate(card, dispatchable, f"{reason}; {note}" if note else reason))

        def no(reason: str) -> None:
            add(False, reason)

        if card.id in bad_schema:
            no(f"card_schema: {len(bad_schema[card.id])} violation(s) — not a finished card")
        elif card.kind == board.KIND_CHORE and not forced_now:
            # Not a refusal — a routing fact. A chore is verified as part of a
            # batch (`nightshift.chores`): a cheap per-item pass, then one full
            # suite run over the merged result. Dispatching it here would give it
            # the full per-card treatment the batch exists to avoid, and three
            # attempts instead of one. Waived for `--card`, which is a human
            # asking for this one item by name.
            no("kind: chore — dispatched as part of a batch by `python -m "
               "nightshift.chores`, not one card at a time")
        elif not card.unattended and not forced_now:
            no("unattended: false — declared as needing a human")
        elif card.worker == "none":
            no("worker: none — nothing to dispatch to")
        elif not (root / ".claude" / "agents" / f"{card.worker}.md").is_file():
            no(f"worker: {card.worker} has no charter")
        elif card.requires and card.requires not in capabilities:
            no(f"requires: {card.requires} — {socket.gethostname()} does not declare it "
               f"(.ai/hosts.json)")  # gate-ok(source_reference_liveness): same HOSTS_FILE as above
        elif card.attempts >= attempt_limit(card) and not forced_now:
            no(f"attempts: {card.attempts} — at the limit, belongs in failed/")
        elif (wait := _backoff_remaining(card)) > 0 and not forced_now:
            no(f"backoff: {wait} more minute(s) after attempt {card.attempts}")
        else:
            why = f"tier: {card.tier}, worker: {card.worker}"
            if forced_now and not card.unattended:
                why += " — asked for by name, so `unattended: false` is waived"
            add(True, why)
    return out


def oversized_entries(candidates: list[Candidate]) -> list[tuple[str, int, int]]:
    """The `(card_id, bytes, threshold)` rows `record.oversized` takes.

    **Dispatchable ones only, and that is the whole of the field.** A card that
    was skipped already carries `oversize_note`'s sentence verbatim inside its
    skip reason, so it is reported under `### Skipped` where it belongs;
    repeating it here would double-report it *and* file a card that never ran
    under a heading whose first two words are "Dispatched anyway". The cards
    left are precisely the ones no other section of the digest can mention — the
    reason `Digest.md` was silent about a card being dispatched over and over
    while it grew.

    A named function rather than a comprehension inline in `run()` for the
    reason `oversize_note` is one: the predicate is the thing worth pinning, and
    a test that re-spelled it at the call site would be a mirror of the code
    rather than a check on it.
    """
    return [(c.card.id, card_bytes(c.card), CARD_COMFORT_BYTES)
            for c in candidates if c.dispatchable and oversize_note(c.card)]


def resolve_named(root: Path, card_id: str,
                  candidates: list[Candidate]) -> tuple[list[Candidate], str]:
    """Narrow the run to one named card, or explain why it cannot run.

    A `--card` that matches nothing used to fall through the loop and produce a
    clean, quiet, entirely successful run in which nothing happened — the same
    silent-nothing shape that has now bitten this project four times. Asking for
    a card by name is a specific request, so an unmet one is an error with a
    reason, never a no-op.
    """
    picked = [c for c in candidates if c.card.id == card_id]
    if picked:
        if picked[0].dispatchable:
            return picked, ""
        return [], f"`{card_id}` is in tasks/ but cannot run — {picked[0].reason}"

    elsewhere = board.find(root, card_id)
    if elsewhere is None:
        return [], (f"no card `{card_id}` anywhere on the board "
                    f"(ids are filename stems, e.g. `menu-unlock-indicators`)")
    return [], (f"`{card_id}` is in `{elsewhere.lane}/`, not `tasks/` — only tasks/ is "
                f"dispatched. Move it there first if it is ready.")


# --------------------------------------------------------------------------
# Worktrees
# --------------------------------------------------------------------------

def worktree_root(root: Path) -> Path:
    """Outside the repo, deliberately.

    A worktree under `Board/`'s repo would be walked by `doc_scan`,
    `card_schema` and the digest, and every card would appear N+1 times. Putting
    it beside the repo costs one `..` and removes a whole class of confusion.
    """
    return root.parent / f".{_project_name(root)}-worktrees"


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

    On any *other* failure this is exactly `_git(root, "worktree", "add",
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
    result = _git(root, "worktree", "add", *args)
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
    return root.parent / integration_checkout_dir(root)


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
        current = current_branch(path)
        if current == base:
            return path
        if not dirty_outside_board(path):
            switched = _git(path, "checkout", base)
            if switched.returncode == 0 and current_branch(path) == base:
                return path
        raise RuntimeError(
            f"the dedicated integration checkout at {path} is on `{current}`, not "
            f"`{base}`, and could not be switched cleanly — resolve it by hand "
            f"(commit or discard its changes, `git checkout {base}` there)")

    # Not a worktree this repo tracks. A bare leftover directory (a pruned
    # registration, or one belonging to a prior test repo) is cleared so the add
    # below has an empty target; the branch carries every board commit, so there is
    # nothing to lose in the directory itself.
    _git(root, "worktree", "prune")
    if path.exists():
        _git(root, "worktree", "remove", "--force", str(path))
        _git(root, "worktree", "prune")
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
FRESH, REENTER, FROM_WIP = "fresh", "reenter", "from-wip"


@dataclass
class Handover:
    """What one limit-interrupted attempt leaves for the next (Decision 1).

    `session_id` is read straight out of the walled worker's own result JSON —
    it was already written to `worker-N.json`, so nothing has to be *captured*,
    only used. `diff_hash` is the working-tree state at the interruption, the
    input to the progress gate (Decision 2). `no_progress` counts consecutive
    resumes that moved nothing, so a card that cannot advance is filed rather
    than given its attempt back forever.
    """
    session_id: str = ""
    diff_hash: str = ""
    no_progress: int = 0


def _handover_path(root: Path, card_id: str) -> Path:
    return root / RUNS / card_id / "handover.json"


def read_handover(root: Path, card_id: str) -> Handover:
    data = _load_json(_handover_path(root, card_id))
    return Handover(
        session_id=str(data.get("session_id", "")),
        diff_hash=str(data.get("diff_hash", "")),
        no_progress=int(data.get("no_progress", 0) or 0),
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


def _worktree_dirty(root: Path, tree: Path) -> bool:
    return gitpaths.dirty(tree)


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
    # Read through `gitpaths`, which is not cosmetic here: git quotes a path whose
    # bytes are not printable ASCII, so a card touching `Animation – attack.md`
    # used to hash the *escaped* spelling and then fail `is_file()` on it — the
    # file's content dropped out of the hash entirely, and the progress gate stopped
    # being able to see changes to exactly the files most likely to be board content.
    entries = sorted(gitpaths.status(tree, "-uall"))
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
    return _git(root, "rev-parse", "--verify", branch).returncode == 0


def _worktree_registered(root: Path, path: Path) -> bool:
    """Whether `path` is a live worktree git still tracks. A bare `path.exists()`
    is not enough: a crash can leave the directory with git's registration
    pruned, or the registration with the directory gone, and either half alone
    cannot be re-entered."""
    listed = _git(root, "worktree", "list", "--porcelain").stdout
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
    _git(tree, "add", "-A")
    done = _git(tree, "commit", "-m", f"wip: {card_id} interrupted")
    return done.returncode == 0


def _rescue_prefix(card_id: str) -> str:
    return f"ai/{card_id}@failed-"


def _rescue_branches(root: Path, card_id: str) -> list[str]:
    """Every `ai/<card_id>@failed-N` ref, whatever order git lists them in."""
    prefix = _rescue_prefix(card_id)
    listed = _git(root, "for-each-ref", "--format=%(refname:short)",
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

    Remote before local, because `_delete_remote_branch`'s guard is an ancestry
    test against the local tip and there is nothing left to test against after
    `git branch -D`. The remote is resolved here rather than threaded through
    the callers because two of them (`sweep_terminal_cards`,
    `cap_rescue_branches_in_flight`) reap across cards at startup, before the
    run loop has resolved a remote of its own.
    """
    _delete_remote_branch(root, str(host_setting(root, "publish_remote", "")).strip(),
                          name, action="reaped")
    _git(root, "branch", "-D", name)


def prune_rescue_branches(root: Path, card_id: str) -> None:
    """Delete every rescue branch for a card that has left `tasks/` for good.

    Called on `prune_run_dir`'s own triggers (failed-attempt-work-is-deleted-
    not-resumed): once a card is retired or otherwise settled, its preserved
    failed-attempt commits have nothing left to be rescued *for* — the same
    reasoning `prune_run_dir` already applies to `.ai/runs/<id>/`."""
    for name in _rescue_branches(root, card_id):
        _drop_rescue_branch(root, name)


def cap_rescue_branches(root: Path, card_id: str, keep: int = MAX_ATTEMPTS) -> None:
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


def cap_rescue_branches_in_flight(root: Path, keep: int = MAX_ATTEMPTS) -> None:
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
            rc = _now.normalize(path)
    except Exception as exc:  # pragma: no cover - defensive, must never block dispatch
        _log(f"    normalize_worktree raised on {path.name}, continuing anyway: {exc}")
        return
    if rc != 0:
        _log(f"    normalize_worktree left violations in {path.name}:\n{buf.getvalue().rstrip()}")
    elif "rewrote" in buf.getvalue():
        _log(f"    normalized {path.name}'s line endings before dispatch")


def prepare_worktree(root: Path, card: board.Card,
                     base: str) -> tuple[Path, str, str]:
    """The worktree for this attempt, plus how it continues (FRESH/REENTER/FROM_WIP).

    A cold card gets a brand-new checkout cut from `base`, exactly as before. A
    card that was limit-interrupted (it has a handover on disk) is continued:
    its kept worktree is reused in place if it still exists, or — if only the
    branch survived a WIP commit — a checkout is cut from that branch.

    Every path out of here normalizes the worktree's line endings first
    (`_normalize_worktree`) — a freshly cut worktree is a fresh checkout, which
    is precisely the state that needs it.
    """
    branch = branches.work_branch(card.id, card.fields.get("branch", ""))
    path = worktree_root(root) / card.id
    handover = read_handover(root, card.id)
    warm = bool(handover.session_id or handover.diff_hash)

    if warm and _worktree_registered(root, path):
        _normalize_worktree(path)
        return path, branch, REENTER
    if warm and _branch_exists(root, branch):
        # The worktree was lost but the branch (with its `wip:` commit) was not.
        # Clear any stale directory/registration, then re-check out the branch.
        if path.exists() or _worktree_registered(root, path):
            _git(root, "worktree", "remove", "--force", str(path))
        _git(root, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        made = _worktree_add(root, str(path), branch)
        if made.returncode != 0:
            raise RuntimeError(f"git worktree add (from wip) failed: {made.stderr.strip()}")
        _normalize_worktree(path)
        return path, branch, FROM_WIP

    # Cold start — the empty case and every non-interrupted card.
    if path.exists() or _worktree_registered(root, path):
        _git(root, "worktree", "remove", "--force", str(path))
    _git(root, "worktree", "prune")
    if _branch_exists(root, branch):
        # This is where a failed attempt's work used to die: `git branch -D`
        # here, one line, discarded the previous attempt's commits the moment
        # the card was retried — nothing ever read them again (failed-attempt-
        # work-is-deleted-not-resumed). Renaming instead keeps them reachable
        # as a rescue ref until the card leaves `tasks/` for good
        # (`prune_rescue_branches`), while `branch` itself is free for the
        # fresh checkout below exactly as before.
        _git(root, "branch", "-m", branch, _next_rescue_branch(root, card.id))
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
    for relative in harvest_dirs(root):
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


def drop_worktree(root: Path, path: Path) -> None:
    """Remove the checkout, keep the branch. The branch is the deliverable —
    `review/` reads it and Karel merges it; twenty stale checkouts are not.

    Dropping the checkout also ends any warm-resume handover for this card: the
    worktree path's basename is the card id, and once the checkout is gone there
    is nothing left to re-enter, so a stale `session_id` must not lure the next
    dispatch into resuming a run that is over. A card being *kept* warm across a
    limit stop is never dropped here — that path returns before reaching this.
    """
    _git(root, "worktree", "remove", "--force", str(path))
    _git(root, "worktree", "prune")
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
#     pruned in a **reconcile-time sweep at the next startup** (`sweep_terminal_
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
    shutil.rmtree(root / RUNS / card_id, ignore_errors=True)


def cap_run_dir(root: Path, card_id: str, keep: int = MAX_ATTEMPTS) -> None:
    """Backstop for a card still in flight: keep only the last `keep` attempt
    directories. `keep` defaults to `MAX_ATTEMPTS`, so in the ordinary run a
    card never accumulates more attempts than this before it is retired — this
    only fires for a card that somehow accumulates more."""
    card_dir = root / RUNS / card_id
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


def cap_run_dirs_in_flight(root: Path, keep: int = MAX_ATTEMPTS) -> None:
    """`cap_run_dir` over every card still in `tasks/` — the only lane whose
    run-dirs are not otherwise pruned or swept."""
    for card in board.cards(root, "tasks"):
        cap_run_dir(root, card.id, keep)


_LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")


def prune_old_run_logs(root: Path, days: int = LOG_RETENTION_DAYS) -> None:
    """The `<date>.log` tee's own, simpler retention rule: keep the last N days.
    Distinct from run-dir pruning because a log is not tied to any one card's
    lane — it is the whole night's transcript, cards in flight and all."""
    runs_dir = root / RUNS
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
        _log(f"  ! {card_id}: WIP commit failed while demoting its worktree past the "
             f"kept-worktree ceiling — the uncommitted diff may be lost; see "
             f"`git status` in {tree}")
    drop_worktree(root, tree)


def enforce_worktree_ceiling(root: Path, ceiling: int = WORKTREE_KEEP_CEILING) -> list[str]:
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
    """The reconcile-time half of the terminal-lane rule: `done/` and `testing/`
    are both reached by hand sometimes (a card closed out inline, outside the
    runner: `manage-board`'s own documented gap) so they are never pruned
    eagerly — this runs once at the next startup instead, over `done/`,
    `failed/` and `testing/` so a card the eager prune missed (settle()'s own
    hook covers every path *it* takes out of `tasks/`, but not a hand-moved
    one) is cleaned up too. `testing/` was missing here until
    `rescue-branches-only-swept-on-failure` (2026-08-13): a card that succeeds
    normally lands there, not in `done/`/`failed/`, and used to keep its
    `@failed-N` rescue refs forever. Returns the card ids actually pruned."""
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
            if (root / RUNS / card.id).exists():
                prune_run_dir(root, card.id)
                pruned.append(card.id)
    return pruned


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

_PROMPT = """\
Execute one card from this project's board, at **tier: {tier}** (resolved to model \
`{model}` — this is the tier the card declares, and running above it is a defect, not a \
favour).

Your working directory is a git worktree on branch `{branch}`, checked out from `{base}`.
Do all code work here. **Commit your work on this branch.** Do not merge, do not push, \
and never check out `dev`, `main` or `{base}`.

This is a single, non-interactive run: when your turn ends, this process ends. There is no \
later turn in which a background task's completion can reach you, so **never start a command \
with `run_in_background` and then end your turn to wait for it** — a backgrounded process is \
killed the instant your turn ends, and any edit you had not committed dies with it. Run any \
command whose result you need — the gate runner and the test suite included — in the \
foreground and wait for it inline (give it a generous timeout). Run the suite in parallel — \
`python -m pytest tests/ -n auto --dist loadfile` — it is several times faster and safe; the \
runner runs the authoritative slice over your branch afterwards, so you need only satisfy \
yourself the area you changed is green. And **commit as you go**: an \
uncommitted edit is not the work, the commit is, so commit before you run a long check and \
again before you write your verdict. A finished but uncommitted worktree is the one way to \
do all the work and still have the run record that you produced nothing.

The card is at:
  {card_path}
You may append to its `## Thread` and write a `## Question` section. **Do not move the \
card between lanes** — lane transitions are the runner's, and it will move this card \
based on whether the gates pass.

When you are finished, write your outcome to:
  {verdict_path}
as JSON, exactly these keys:
  {{"outcome": "done" | "parked", "summary": "<2-4 short lines>", \
"how_to_test": "<a scenario, or "" >"}}
`summary` is written verbatim onto the card as its `## Summary` section — the first thing \
the maintainer reads at `testing/`, before the verbose `## Thread` prose if you wrote one. \
Make it concrete: what changed, what you tested, gate/test status — not "implemented the \
card".
`how_to_test` is written onto the card as `## How to test`, and it is the **only** thing \
telling the maintainer what to do with what you built. Write it as a scenario in their \
terms — open the game, go here, do this, expect that — never as a description of the diff. \
Name the door: which menu, which key, which enemy, what the screen should show. If the card \
declares `verify: review` (no surface they can exercise), leave it empty — nobody has to \
invent a scenario for a gate or a refactor, and inventing one is worse than none.
Use `"parked"` when the card cannot be finished without an answer from the maintainer. \
**Parking is a success state, not a failure**, and it requires a `## Question` section on \
the card carrying what you attempted, what is ambiguous, the candidate answers and what \
each would imply — a question they can answer in fifteen seconds without opening the repo. \
Guessing at an ambiguity is the worst possible overnight outcome; parking is better than \
inventing.

The runner will run `nightshift.gates.run` and the test slice your change touches over your \
branch (in parallel, judged by its JUnit report). Do not \
weaken a gate or a test to make it pass.

{tool_economy}

--- the card ---
{card_body}
"""

# A limit-interrupted attempt is continued, not restarted (runner-worker-handover
# Decision 1). Three shapes, tried in order of how much context they keep:
#
#   _RESUME_PROMPT   path 1 — `claude --resume <session_id>`. The session still
#                    carries the whole conversation, so the message is a nudge,
#                    not the card. A `--resume` that errors falls through to
#                    path 2 automatically, inside the same attempt.
#   _REENTER_NOTE    path 2 — a fresh session in the *kept* worktree. The session
#                    is gone but the uncommitted diff is not, and that diff is
#                    where the last attempt ended.
#   _WIP_NOTE        path 3 — the worktree was lost too; the work survives only as
#                    a `wip:` commit on the branch, now checked out for you.
_RESUME_PROMPT = """\
Continue the card you were working on before this session was interrupted by a usage limit. \
Your worktree, your branch and your prior reasoning are intact — pick up exactly where you \
left off. When finished, write the same verdict JSON to:
  {verdict_path}
as before: {{"outcome": "done" | "parked", "summary": "<2-4 short lines>", \
"how_to_test": "<a scenario in the maintainer's terms, or "" on a `verify: review` card>"}}. The runner \
will run `nightshift.gates.run` and the full test suite over your branch; do not weaken either.
"""

_REENTER_NOTE = """\

--- continuing an interrupted attempt ---
A previous attempt at this card was interrupted by a usage limit before it finished. Its \
session could not be resumed, but **its uncommitted work is still in this worktree** — run \
`git status` and `git diff` to see exactly where it left off, then continue from there \
rather than starting over. Its work may be incomplete or mid-edit; read it before building on it.
"""

_WIP_NOTE = """\

--- continuing an interrupted attempt (recovered from a WIP commit) ---
A previous attempt at this card was interrupted and its worktree was reclaimed, but its work \
was preserved as a `wip: {card_id} interrupted` commit, now checked out on your branch. Run \
`git show HEAD` and `git log` to see what it had done, then continue from there. **Before you \
finish, replace that WIP commit with a real commit** (e.g. `git reset --soft HEAD~1` then \
commit properly) so the branch does not carry a `wip:` commit into review.
"""

# A `kind: chore` is a one-prompter: the note already said what to change and what
# the result should be, so there is nothing to elaborate and no fork to resolve.
# Whether that is *true* of any given item is a routing guess made without opening
# the code, and the worker is the only actor that does open it — so the worker is
# the only place the guess can be checked. That is what this note asks for, and
# saying it here rather than in each charter is deliberate: the condition is the
# card's `kind:`, which a charter cannot see, and a per-charter copy would be one
# more thing to remember when a new worker is added.
#
# A bounce is cheap and a wrong batch is not: a parked chore costs one dispatch and
# tells the router it misfired, which is the feedback that makes routing tunable.
# Guessing instead produces a green diff nobody asked for, inside a batch that
# lands as one unit.
_CHORE_NOTE = """\

--- this card is a one-prompter ---
It was routed here as work with **nothing to decide**: the change has one obvious \
home, the intent is the whole approach, and a wrong result would be visible rather \
than subtle. It runs in a batch with other items like it, and the batch lands as one \
unit.

You are the first actor to open the code, so you are the only one who can find out \
that the routing was wrong. **Stop and park — do not proceed — the moment any of \
these turns out to be true**: the change needs a design decision; the thing to change \
is not where the card implies; doing it properly means touching considerably more than \
the card describes; or finishing it would need an answer nobody is awake to give.

Parking here is not a failure and costs almost nothing — it is the signal that this \
was not a one-prompter, and it is worth more than a plausible guess. Write what you \
found into `## Question` as usual. Do **not** widen the change to make it fit, and do \
not do a reduced version of it without saying so.

**Judge that by what you still do not know, not by how long you have taken.** Reading a \
lot of a large codebase to place a small change is ordinary and is not a finding. Being \
unable to say where the change goes, or which of several places is the right one, is — \
park and say so.
"""

_FEEDBACK = """\

--- round {round_no} of {max_rounds}: what the checker said about your last attempt ---
It saw only the artefacts and the acceptance criteria — never your prompt, your settings \
or your reasoning. That blindness is the point: it reports what you actually made rather \
than what you meant to make. Act on the words.

Verdict: {verdict}
{notes}

Previous rounds already tried: {history}
Change **one thing** this round, and do not repeat a change from that list.
"""

_CHECKER_PROMPT = """\
Judge the artefacts below against the acceptance criteria, at **tier: {tier}** (resolved to \
model `{model}`).

The artefacts are in:
  {artefacts}
Neighbouring assets already shipped, for the "does it belong?" judgement:
  {neighbours}

You are **not** told how these were made, and you must not go looking. No prompt, no \
pipeline settings, no generator reasoning, no card file. An agent that knows what was \
intended sees what was intended; you are here to see what is actually there.

Write your verdict to:
  {verdict_path}
as JSON, exactly these keys:
  {{"verdict": "pass" | "revise" | "reject",
    "best": "<filename of the strongest candidate, even if none passed>",
    "notes": "<one or two concrete, visual sentences the producer can act on \
without seeing the artefact>"}}

`pass` means it is good enough to ship, not that it is perfect. Name a `best` every time — \
whoever picks this up wants a ranking, not a tie.

--- acceptance criteria, verbatim from the card ---
{criteria}
"""


# The diff reviewer's prompt (automate-review-step). Same structural-blindness
# principle as `_CHECKER_PROMPT`: the runner builds this context, so there is no
# path by which the worker's prompt, transcript or reasoning reaches the reviewer
# — it sees the diff, the criteria and the repo, and nothing about how the change
# was made (§16). The verdict is a routing decision, not a quality score.
#
# Three-way since `reviewer-needs-fix-verdict` (2026-08-19). The original two-way
# contract (`ok` / `needs_decision`) forced every non-`ok` finding through the same
# lane whether or not it actually needed the maintainer's judgment — a concrete,
# verifiable defect with one correct fix (a wrong date, a misattributed commit, a
# name that does not match the code) got the identical "wake a human" treatment as
# a genuine ambiguity only they could resolve. `needs_decision` sat on the board
# waiting for an answer that was really "go check `git log`", the maintainer's time
# spent on something the next attempt could have applied directly. `needs_fix`
# closes that gap: it is drain.py's own principle ("'review this diff' is not a
# decision — it is work") extended one step further — *fixing* a diff whose defect
# has one verifiable correct answer is not a decision either.
_REVIEW_PROMPT = """\
Review the finished diff below against the card's acceptance criteria and the surrounding \
code, at **tier: lead** (resolved to model `{model}`).

The change is on branch `{branch}`. Its diff against the integration branch — what this \
branch added since it forked — is in:
  {diff}
The repository it changes is rooted at:
  {repo}
Read the diff, then read whatever surrounding code you need to judge whether the change \
does what the card asked and touched nothing it should not have.

You are **not** told how this was made, and you must not go looking: no worker prompt, no \
transcript, no reasoning. The gates and the full test suite have **already passed** on this \
exact branch — do not re-check anything they cover, and run `python -m nightshift.gates.run` \
once to see what that is rather than assuming, since the gate list is this project's and \
grows as it earns rules. Spend your attention only on what no script can see.

Your verdict is exactly three-way, and it is a routing decision:
- `ok` — nothing needs the maintainer's judgment before it merges. This is the common case. \
`ok` does not mean perfect; every `ok` card is still tested by the maintainer at testing/. \
Style you dislike or a cleaner refactor you can imagine is neither of the other two verdicts.
- `needs_fix` — the diff has a concrete, **objectively verifiable** defect with one correct \
answer: a fact you can confirm yourself (check `git log`, read the code it describes, run a \
calculation) rather than a preference. A claim the diff makes that is simply wrong; a value \
computed incorrectly; a docstring or comment that does not match what the code does; a \
reference to the wrong commit, date or symbol. State the defect **and** its correct fix — \
precise enough that a worker who never saw your reasoning can apply it without re-deriving \
it. This is not a lesser `needs_decision`: if you are choosing between the two because you \
are unsure whether your fix is *right*, it is `needs_decision`; choose `needs_fix` only when \
you would bet on the fix yourself.
- `needs_decision` — merging this needs the maintainer's judgment first: an ambiguity in the \
card the worker resolved by guessing, a design judgment call with more than one defensible \
answer, a behaviour that may not be what they want, or a value/name/rule the card left open \
and the worker picked. If two people could disagree about the right answer, this is it — \
`needs_fix` is reserved for the case where nobody reasonable would.

Write your verdict to:
  {verdict_path}
as JSON, exactly these keys:
  {{"verdict": "ok" | "needs_fix" | "needs_decision",
    "finding": "<if needs_fix: the defect, verified, and its correct fix, precise enough to \
apply without re-deriving it. Empty string otherwise.>",
    "question": "<if needs_decision: what was attempted, what is ambiguous, the candidate \
answers, and what each would imply — those four parts are what makes the question \
answerable. Empty string otherwise.>",
    "notes": "<one or two sentences of reasoning the runner can log>"}}

On `needs_fix` the `finding` goes back to a worker for another attempt, with no human in the \
loop — so it must be self-contained enough to act on alone. On `needs_decision` the \
`question` is put in front of the maintainer verbatim, with no human in between, so it must \
stand alone and be answerable in fifteen seconds from a phone. You never edit, fix or merge \
— you report, the runner routes.

--- acceptance criteria, verbatim from the card ---
{criteria}

--- what the card set out to do (its intent) ---
{intent}
"""


@dataclass
class Dispatch:
    # "review" (gates+tests passed, nobody has concluded — the review stage either
    # has not run yet or could not conclude, and `nightshift.drain` is the pass that
    # takes it from there. Until that module existed this comment said "awaiting the
    # review stage *or* a human's eye", and those two states behind one lane name are
    # what hid `review/` having no exit at all) |
    # "reviewed" (the diff reviewer said ok — settle merges and lands it in testing/) |
    # "needs_fix" (the reviewer found a concrete, verifiable defect with one correct
    # answer — settle sends the card back to tasks/ for another attempt, bounded by
    # the same attempt_limit as "failed"; only escalates to needs-decision if the
    # limit is exhausted) |
    # "needs_decision" (the reviewer flagged a choice for Karel) | "parked" |
    # "failed" | "limited" | "blocked" | "interrupted" (an API disconnection
    # that never reached verification — gives the attempt back like "blocked",
    # but does not stop the night: it is a fact about one connection, not the
    # repo or this machine)
    outcome: str
    detail: str
    cost_usd: float = 0.0
    rounds: int = 1
    # Set whenever a usage wall landed anywhere in this card's pipeline — NOT
    # only on "limited" (`wall-on-review-wrapup-discards-a-verdict`). A wall
    # carries two independent facts, and conflating them was the defect: "this
    # attempt produced nothing" (an accounting claim, often false) and "the
    # plan's window is closed" (a scheduling claim, always true). Only the second
    # is evidence, so a stage that had already written a terminal verdict now
    # honours it — the card settles on `review`/`parked`/`needs_decision` like any
    # other — and the wall rides home on this field regardless. The run loop
    # consults it after settling *any* outcome, so a card that landed still costs
    # the night its session and still triggers the sleep-or-stop.
    #
    # Carries the reset time the run loop needs to decide between sleeping and
    # stopping, and the evidence line for the run log.
    wall: limits.Wall | None = None
    # Set only on "limited" (runner-worker-handover). `kept` is True when the
    # worktree and session were preserved for a warm resume rather than dropped;
    # `progressed` is whether the working tree advanced since the last attempt
    # (the give-back gate); `stuck` is the no-progress breaker tripping, which
    # routes the card to failed/ instead of giving the attempt back again.
    kept: bool = False
    progressed: bool = True
    stuck: bool = False
    # Set on "failed": the bounded, quotable evidence behind `detail` — pytest's
    # failing tests and their assertions (`suite.failure_excerpt`) or the gate
    # log's own lines. `detail` is one line and goes in the run log; this is the
    # block `settle` writes onto the card so the reason survives being read from
    # another machine, or after `prune_run_dir` has taken the attempt directory.
    evidence: str = ""
    # Set only on "blocked": whether this is a gate violation whose every path
    # lies outside the attempt's own diff (repo drift, `failed-attempt-work-
    # is-deleted-not-resumed`) rather than the pre-existing gate-harness-crashed
    # reason. Both give the attempt back and stop the night; only the reason
    # text differs, and `settle` needs to know which one it is telling.
    repo_drift: bool = False
    # The worker's scenario for Karel, written onto a `verify: play` card as
    # `## How to test` when it merges. It rides from the worker's verdict through
    # the review stage rather than being asked for at the end: only the worker
    # that built the thing knows which door it is behind, and `## Summary` proved
    # the shape — written deterministically by `settle()` from the verdict rather
    # than left to agent discretion (menu-summary-on-card).
    how_to_test: str = ""


def _budget_argv(card_budget: float) -> list[str]:
    """`--max-budget-usd`, and only when a cap was actually asked for.

    Passing `--max-budget-usd 0` would be a cap of zero, not the absence of one,
    so "no cap" has to mean "no flag"."""
    return ["--max-budget-usd", str(card_budget)] if card_budget > 0 else []


# `--output-format stream-json` emits one JSON object per event as it happens,
# instead of one blob buffered until the process exits — what turns a silent
# hour into a live feed on disk (`_run_worker`'s `stream_path`). Confirmed
# directly against the installed CLI, not assumed: a bare `--output-format
# stream-json` without `--verbose` refuses with "Error: When using --print,
# --output-format=stream-json requires --verbose". `--verbose` changes nothing
# else functionally here — every event still lands in the tee and only the
# terminal one is read back (`_terminal_result`).
_STREAM_ARGV = ["--output-format", "stream-json", "--verbose"]


def _terminal_result(stdout: str) -> dict:
    """The one JSON object that carries the run's cost/usage/verdict fields,
    out of a worker's raw stdout — whether that stdout is a single
    `--output-format json` blob (the old shape, and every existing test
    double) or a `--output-format stream-json` JSONL stream whose *last* line
    is the terminal `result` event (the new shape).

    Reads from the end on purpose: under the old shape the whole string is one
    line, so the last line *is* the only line and this agrees with a plain
    `json.loads` exactly. Under the new shape, scanning backwards also survives
    a stream cut short mid-line (a killed worker) — the true last *complete*
    line is still found instead of the parse just failing on trailing garbage.
    `{}` — not an exception — when nothing on the whole stream parses as a JSON
    object, matching `_read_verdict`'s "always a lookup" contract (§12).
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def run_dir(root: Path, card: board.Card, attempt: int) -> Path:
    path = root / RUNS / card.id / f"attempt-{attempt}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_verdict(path: Path) -> dict:
    """A worker's structured report, or `{}`.

    Always a lookup, never an interpretation (§12). `{}` is a meaningful answer —
    for the producer it means "judge me by the gates", which is what keeps
    charters written before the runner existed working unchanged.
    """
    if not path.is_file():
        return {}
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


# The pipeline stages that produce a verdict, named so the wall-handling at each
# spawn site says which predicate it means rather than inlining a set of strings.
PRODUCER_STAGE, CHECKER_STAGE = "producer", "checker"
REVIEWER_STAGE, STALE_STAGE = "reviewer", "stale-hunter"


def verdict_survives_a_wall(stage: str, verdict: dict, *, rounds_left: int = 0) -> bool:
    """Whether a verdict already written is *terminal for its stage*, and so
    survives the wall that landed on the call that was wrapping it up
    (`wall-on-review-wrapup-discards-a-verdict`).

    **The defect this closes is an ordering, not a durability problem.** Every
    spawn function returns `(verdict, cost, wall)`, and at each site the wall
    branch returned before the verdict was ever looked at — so a stage that had
    written a complete, usable verdict and *then* walled on its own wrap-up call
    was recorded byte-identically to one that died before writing anything
    (2026-08-09, `menu-art-cyberware`: `review-2.json` said `pass`, and the run
    log said "not attempted, attempt given back"). Nothing has to be read back
    off disk to fix that; the parsed verdict is already a local variable next to
    the wall. This function is asked *first*, and only then is the process's exit
    consulted.

    **There is no threshold here, deliberately.** "Complete" is not a new
    judgment — it is the predicate the no-wall path a few lines below each site
    already applies, and a truncated file degrades for free because
    `_read_verdict` returns `{}` on a `JSONDecodeError`. Per stage:

    * `PRODUCER_STAGE` — `outcome: parked` only. A park is terminal by
      construction: the round loop already `break`s on it and no re-roll resolves
      an ambiguity, and losing one costs Karel a whole night's question. Any
      other producer verdict keeps the old give-back, because a verdict attests
      the *worker's* account, not that the tree is finished — honouring a
      half-written tree would turn a free give-back into a spent attempt.
    * `CHECKER_STAGE` — `pass`, or any complete verdict once the rounds are
      spent. **The `rounds_left` guard is not optional.** A checker that walls
      after writing `revise` at round 1 of 3 must still be `limited`: the loop
      cannot spend rounds 2-3 in a closed window, and the path below it would
      file the card as *"did not pass this in 1 round(s)"* — parking a card that
      had two rounds it never received.
    * `REVIEWER_STAGE` — the three routings the reviewer exists to produce. Its
      whole output *is* the verdict file, so a complete one means it finished.
    * `STALE_STAGE` — its own `complete: true`, which the prompt already defines
      as "I finished reading the whole document".

    An unrecognised stage is `False`: today's behaviour, never a guess. A fifth
    judge stage therefore has to state its own predicate here to be honoured, and
    `test_every_spawn_sites_wall_path_routes_through_the_shared_helper` is what
    stops it quietly copying the wrong ordering from its neighbours instead.
    """
    if not isinstance(verdict, dict) or not verdict:
        return False
    if stage == PRODUCER_STAGE:
        return str(verdict.get("outcome", "")).lower() == "parked"
    if stage == CHECKER_STAGE:
        called = str(verdict.get("verdict", "")).lower()
        if called == "pass":
            return True
        return rounds_left <= 0 and called in ("revise", "reject")
    if stage == REVIEWER_STAGE:
        return str(verdict.get("verdict", "")).lower() in ("ok", "needs_fix", "needs_decision")
    if stage == STALE_STAGE:
        return bool(verdict.get("complete"))
    return False


def _session_id(out_dir: Path, round_no: int) -> str:
    """The walled worker's session id, read from the result JSON it already wrote
    (`worker-N.json`, `runner.py`'s producer). Nothing had to capture it — the CLI
    reports it under `--output-format json` and the runner writes the whole blob to
    disk — so warm resume only has to *use* a fact that was already on disk."""
    data = _load_json(out_dir / f"worker-{round_no}.json")
    return str(data.get("session_id", "") or "")


def _api_error_interruption(out_dir: Path, round_no: int) -> bool:
    """Whether this round's non-zero exit was an API disconnection that never
    reached verification, rather than a genuine self-reported failure
    (failed-attempt-work-is-deleted-not-resumed, the `api_error` slice).

    Keys on `terminal_reason` from the worker's own result JSON plus the
    *absence* of a gate or pytest log — never on the exit code, which is `1`
    for both this and an ordinary failure (`corridor-generation-redesign`
    attempt 1: 121 turns, a disconnected socket, and a 1,237-line rescue
    nobody would have resumed). A worker that *did* reach verification and
    failed it writes `gates.txt`/`pytest.txt` before this is ever called (they
    are written later in `dispatch`, after `run_producer` returns) — that path
    returns `failed` through its own branch and never reaches this check, so
    the two are told apart by construction, not by guessing at intent.
    """
    data = _load_json(out_dir / f"worker-{round_no}.json")
    if str(data.get("terminal_reason", "")) != "api_error":
        return False
    return not (out_dir / "gates.txt").exists() and not (out_dir / "pytest.txt").exists()


def _si(n: float) -> str:
    """1234567 -> `1.23M`. Token counts are unreadable at full width."""
    for limit, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= limit:
            return f"{n / limit:.2f}{suffix}"
    return str(int(n))


def read_telemetry(out_dir: Path) -> dict:
    """What the CLI already reported about one attempt, summed over its rounds.

    **Every number here was already downloaded and thrown away.** The runner
    writes each round's `worker-N.json` to disk and reads exactly one key out of
    it — `total_cost_usd` — discarding wall time, API time, turn count, the four
    token counters, the per-model split and the permission denials. Surfacing
    them costs no tokens, no API calls and no wall time: it is a read of bytes
    already paid for, which is why this is a lookup in the runner rather than
    anything cleverer (§5, §12).

    Kept as a plain dict of totals rather than a dataclass because it is written
    straight to a card and to `status.json`, and both want JSON.
    """
    total = {
        "rounds": 0, "wall_s": 0.0, "api_s": 0.0, "turns": 0, "cost_usd": 0.0,
        "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "input_tokens": 0, "denials": 0, "models": [], "ended": "",
    }
    for path in sorted(out_dir.glob("worker-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        total["rounds"] += 1
        total["wall_s"] += float(data.get("duration_ms") or 0) / 1000
        total["api_s"] += float(data.get("duration_api_ms") or 0) / 1000
        total["turns"] += int(data.get("num_turns") or 0)
        total["cost_usd"] += float(data.get("total_cost_usd") or 0)
        usage = data.get("usage")
        if isinstance(usage, dict):
            total["output_tokens"] += int(usage.get("output_tokens") or 0)
            total["cache_read_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
            total["cache_write_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)
            total["input_tokens"] += int(usage.get("input_tokens") or 0)
        denials = data.get("permission_denials")
        if isinstance(denials, list):
            total["denials"] += len(denials)
        models = data.get("modelUsage")
        if isinstance(models, dict):
            for name in models:
                if name not in total["models"]:
                    total["models"].append(name)
        total["ended"] = str(data.get("terminal_reason") or data.get("stop_reason") or "")
    return total if total["rounds"] else {}


# How many lines of the CLI's own error text reach the card. Enough to carry a
# stack-less API error or a refusal message; short enough that `## Error` stays
# readable in Obsidian.
_WORKER_EVIDENCE_LINES = 12


def worker_exit_evidence(out_dir: Path) -> str:
    """Why the worker process itself failed, as a bounded block for `## Error`.

    The third of three evidence sources, and the one that was missing. `Dispatch`
    carries `evidence` for exactly this purpose, and the gates path fills it with the
    violation lines while the tests path fills it with `suite.failure_excerpt` — but
    the `code != 0` path constructed `Dispatch("failed", f"worker exited {code}")`
    and passed nothing. So a card whose *worker* died recorded a reason that named
    only the exit status, and the pointer beside it was to a directory
    `prune_run_dir` deletes on the retiring attempt.

    That is how `stair-remnants-removal` retired on 2026-08-01 saying "worker exited
    1" and nothing else. The actual cause — `ended api_error`, one permission denial
    — *had* been parsed, by `read_telemetry`, and written to `## Telemetry`: a
    different section, by a different code path, that happens to sit above this one.
    Reading the two together was luck, not design, and on any other day the numbers
    would have been in the same place while the cause was not.

    So this reuses `read_telemetry` rather than parsing `worker-*.json` a second
    time — the facts were already downloaded and are already on disk (§5, §12); what
    was missing was routing them to the section a human actually reads for a reason.
    Indented four spaces like every other excerpt, so a `#` in the CLI's own message
    cannot match `doc_scan._HEADING`.
    """
    tel = read_telemetry(out_dir)
    lines: list[str] = []
    if tel.get("ended"):
        lines.append(f"    worker ended: {tel['ended']}")
    if tel.get("denials"):
        lines.append(f"    permission denials: {tel['denials']} — the worker asked for a "
                     f"tool and was refused, with nobody to ask in -p mode")
    if tel.get("turns"):
        lines.append(f"    {tel['turns']} turns · {tel['wall_s'] / 60:.1f} min wall "
                     f"before it exited")

    # The CLI's own message, when it reported one. `is_error` is the flag it sets on
    # a terminal failure; `result` is where it puts the text.
    for path in sorted(out_dir.glob("worker-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("is_error"):
            continue
        text = str(data.get("result") or data.get("error") or "").strip()
        if not text:
            continue
        for line in text.splitlines()[:_WORKER_EVIDENCE_LINES]:
            lines.append(f"    {line[:200]}")
        break
    return "\n".join(lines)


def telemetry_markdown(tel: dict, attempt: int) -> str:
    """The `## Telemetry` block. Lives on the **card**, not only in `.ai/runs/`,
    because the card is committed and syncs to Karel's other machine while the
    run directory is gitignored and machine-local."""
    denials = tel["denials"]
    lines = [
        f"- **attempt {attempt}** · {tel['wall_s'] / 60:.1f} min wall · "
        f"{tel['api_s'] / 60:.1f} min in API · {tel['turns']} turns · "
        f"${tel['cost_usd']:.2f} equivalent",
        f"- **tokens** · {_si(tel['output_tokens'])} out · "
        f"{_si(tel['cache_write_tokens'])} cache-write · "
        f"{_si(tel['cache_read_tokens'])} cache-read · {_si(tel['input_tokens'])} in",
        f"- **model** · {', '.join(tel['models']) or 'unrecorded'}"
        + (f" · ended `{tel['ended']}`" if tel["ended"] else "")
        + (f" · {tel['rounds']} rounds" if tel["rounds"] > 1 else ""),
    ]
    # Only when non-zero. A denial is a real explanation for an attempt that
    # looks inexplicably bad — the worker wanted a tool and was refused, with
    # nobody to ask in `-p` mode — and a permanent "none" line trains the eye
    # to skip exactly the line that matters on the night it is not "none".
    if denials:
        lines.append(f"- **⚠ {denials} permission denial(s)** — the worker was refused a "
                     f"tool it asked for; see `.ai/runs/`")
    return "\n".join(lines)


def run_checker(root: Path, card: board.Card, out_dir: Path, round_no: int,
                model: str, card_budget: float,
                timeout: int) -> tuple[dict, float, limits.Wall | None]:
    """Spawn the card's checker on the artefacts of one round.

    **The checker's context is constructed here, and that is the whole point.**
    §16 says an agent reviewing its own output sees what it intended rather than
    what it made, so the reviewer must be denied the prompt, the pipeline and the
    reasoning. While the producer spawned its own reviewer that was a charter
    instruction — a rule the producer could quietly break. Built by the runner,
    the blindness is structural: there is no path by which the producer's prompt
    reaches the checker, because the runner never puts it there.

    It also puts the checker's tier through the dispatcher (§16's landing place
    2), which a nested spawn could not do: a subagent with no explicit model
    inherits its parent's, and `tier_guard` cannot see a spawn that names no
    card path.
    """
    artefacts = out_dir / "artefacts"
    verdict_path = out_dir / f"review-{round_no}.json"
    criteria = board.section(card.text, "Acceptance") or \
        board.section(card.text, "Acceptance criteria") or "(none stated on the card)"

    neighbours = neighbours_dir(root)
    prompt = _CHECKER_PROMPT.format(
        tier="worker", model=model, artefacts=artefacts.resolve().as_posix(),
        neighbours=(neighbours.resolve().as_posix() if neighbours else
                    "(this project declares no harvest dir, so there are none to compare against)"),
        verdict_path=verdict_path.resolve().as_posix(), criteria=criteria,
    )
    textio.write_text_lf(out_dir / f"review-{round_no}-prompt.md", prompt)

    binary = claude_binary()
    assert binary
    argv = [
        binary, "-p",
        "--agent", card.checker,
        "--model", model,
        *_STREAM_ARGV,
        *_budget_argv(card_budget),
        "--permission-mode", str(host_setting(root, "permission_mode", "acceptEdits")),
        "--add-dir", str(artefacts.resolve()),
        *(("--add-dir", str(neighbours.resolve())) if neighbours else ()),
    ]
    cost = 0.0
    try:
        proc = _run_worker(argv, out_dir, timeout, out_dir / "stream.jsonl",
                           prompt=prompt)
        textio.write_text_lf(out_dir / f"review-{round_no}.log", proc.stdout + proc.stderr)
        result = _terminal_result(proc.stdout)
        try:
            cost = float(result.get("total_cost_usd", 0.0))
        except (ValueError, AttributeError, TypeError):
            pass
        # Checked before the verdict is read: a checker that never ran left no
        # verdict, and "no verdict" is a park (§13). Parking a card because the
        # night ran out of window would put a question to Karel that has nothing
        # to do with the card.
        wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return {"verdict": "revise", "notes": "the checker timed out"}, cost, None
    return _read_verdict(verdict_path), cost, wall


def card_review_context(card: board.Card) -> tuple[str, str]:
    """The `(criteria, intent)` a single card hands the diff reviewer.

    Lifted verbatim off the card — never summarised, never rephrased — because
    the reviewer's whole job is judging the diff against what was *asked for*,
    and a paraphrase is already a judgment about that.
    """
    criteria = board.section(card.text, "Acceptance") or \
        board.section(card.text, "Acceptance criteria") or "(none stated on the card)"
    return criteria, board.section(card.text, "Intent") or "(none stated on the card)"


def review_branch(root: Path, label: str, out_dir: Path, model: str, base: str,
                  branch: str, card_budget: float, timeout: int, *,
                  criteria: str, intent: str) -> tuple[dict, float, limits.Wall | None]:
    """Spawn the diff reviewer on a finished branch (automate-review-step).

    **The reviewer's context is constructed here, and that is the whole point** —
    the same structural blindness as `run_checker` (§16). It is given the diff
    (`git diff base...branch`, on disk), the acceptance criteria and intent of
    whatever produced the branch, and the code to read surrounding context. It is
    *not* given the worker's prompt, transcript or reasoning.

    Blindness made structural rather than promised: the review runs inside a
    **throwaway detached checkout of the branch**, and that is the only directory
    it is handed. Because `.ai/runs/` is gitignored, a fresh checkout cannot
    contain the worker's `prompt-N.md` or `worker-N.json` — so there is no path on
    disk from which the reviewer could read how the change was made, not merely a
    charter rule telling it not to look. Same worktree discipline as `merge_check`.

    **A branch, not a card.** One card's branch is the ordinary case
    (`review_stage`, with `card_review_context` supplying the two blocks), but the
    chore batch hands over a branch carrying several cards' work and reviews the
    combined diff once — which is the only way to see an interaction between two
    items that per-item review structurally cannot. `label` names the throwaway
    worktree and the log lines: a card id in the first case, a batch name in the
    second.

    Returns `(verdict, cost, wall)`, all lookups — `{}` (no verdict file, a
    timeout, no CLI, or a worktree that would not cut) degrades in the caller to
    a human's eye, never to a guessed routing (§12).
    """
    binary = claude_binary()
    if not binary:
        return {}, 0.0, None

    tree = worktree_root(root) / f"_review-{label}"
    if tree.exists() or _worktree_registered(root, tree):
        _git(root, "worktree", "remove", "--force", str(tree))
    _git(root, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = _worktree_add(root, "--detach", str(tree), branch)
    except WorktreePathTooLong as exc:
        _log(f"    review worktree for {label} would not cut — {exc}")
        return {}, 0.0, None
    if made.returncode != 0:
        _log(f"    review worktree for {label} would not cut — {made.stderr.strip()[:120]}")
        return {}, 0.0, None

    try:
        # Diff and verdict live *inside* the throwaway checkout, so the only
        # directory handed to the reviewer holds the change and nothing about how
        # it was made. Archived copies go to out_dir (in the main repo, never shown
        # to the reviewer) for the post-mortem.
        diff = _git(root, "diff", f"{base}...{branch}")
        diff_path = tree / ".review-diff.patch"
        textio.write_text_lf(diff_path, diff.stdout)
        verdict_path = tree / ".review-verdict.json"

        prompt = _REVIEW_PROMPT.format(
            model=model, branch=branch, diff=diff_path.resolve().as_posix(),
            repo=tree.resolve().as_posix(),
            verdict_path=verdict_path.resolve().as_posix(),
            criteria=criteria, intent=intent,
        )
        textio.write_text_lf(out_dir / "review-prompt.md", prompt)
        textio.write_text_lf(out_dir / "review-diff.patch", diff.stdout)

        argv = [
            binary, "-p",
            "--agent", REVIEWER_AGENT,
            "--model", model,
            "--output-format", "json",
            *_budget_argv(card_budget),
            "--permission-mode", str(host_setting(root, "permission_mode", "acceptEdits")),
            "--add-dir", str(tree.resolve()),
        ]
        cost = 0.0
        try:
            proc = _run_worker(argv, tree, timeout, prompt=prompt)
            textio.write_text_lf(out_dir / "review.log", proc.stdout + proc.stderr)
            try:
                cost = float(json.loads(proc.stdout).get("total_cost_usd", 0.0))
            except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
                pass
            wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return {}, cost, None
        verdict = _read_verdict(verdict_path)
        if verdict:
            textio.write_text_lf(out_dir / "review-verdict.json", json.dumps(verdict, indent=2))
        return verdict, cost, wall
    finally:
        _git(root, "worktree", "remove", "--force", str(tree))
        _git(root, "worktree", "prune")


_STALE_PROMPT = """\
Check exactly ONE document for staleness, at **tier: worker** (resolved to model `{model}`). \
Follow your charter: quote-or-drop, cite a real `file:line`, never edit, `unverifiable` is a \
success.

The document:
  {doc}
Its named source lives under the repo root you have been given; read the doc, then read the \
current source of the modules it names, and report only sentences that are no longer true.

Report your verdict as **the last thing in your final message**: one ```json fenced block, \
nothing after it. You are a read-only checker and have no Write tool — do not try to create a \
file, and do not describe the verdict in prose instead of emitting the block.
  {{"complete": true,
    "findings": [{{"claim": "<verbatim quote from the doc>", "cite": "<file:line in source>", \
"why": "<what the source actually says now>"}}],
    "summary": "<one line: how many real drifts, or 'no drift found'>"}}

`complete` must be `true` only if you finished reading the whole document. If you ran out of \
room, set it `false` — the runner will re-check this doc next time rather than record a \
verification that did not happen. An empty `findings` list with `complete: true` is the good \
outcome: the doc is accurate.
"""

# ```json ... ``` anywhere in the final message, last one wins. Tolerates a bare
# {...} block too, since a model that skips the fence is still answering.
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _verdict_from_message(text: str) -> dict:
    """Parse a checker's verdict out of its final chat message.

    Why this exists (2026-07-30): `stale-hunter` is declared
    `tools: Read, Grep, Glob` — deliberately, because a checker that can write is
    no longer a checker (§16's producer/checker seam). The prompt nonetheless told
    it to *write* `verdict.json`, a file it has no tool to create. So all 26 docs
    in the first `--stale` night ran to `subtype: success` with zero permission
    denials, each printing a well-formed verdict into its transcript, and every
    one was read back as `{}` — "no complete verdict", re-check next time. ~$15 of
    correct analysis discarded, and the ledger stayed empty so the next run would
    redo it identically.

    The lesson is the shape, not the typo: **a handover channel the agent's own
    tool grant forbids is a guaranteed silent no-op.** The channel is now the
    agent's reply, which needs no tool at all.
    """
    if not text:
        return {}
    blocks = _FENCED_JSON.findall(text)
    if not blocks:
        start, end = text.find("{"), text.rfind("}")
        blocks = [text[start:end + 1]] if 0 <= start < end else []
    for block in reversed(blocks):
        try:
            found = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(found, dict):
            return found
    return {}


def stale_run_dir(root: Path, doc_rel: str) -> Path:
    slug = doc_rel.replace("/", "__").replace(".md", "")
    path = root / RUNS / "_stale" / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_stale_check(root: Path, doc_rel: str, out_dir: Path, model: str,
                    card_budget: float, timeout: int) -> tuple[dict, float, "limits.Wall | None"]:
    """Dispatch `stale-hunter` on ONE doc. Returns (verdict, cost, wall).

    The runner owns this the way it owns the producer→checker loop (§16): the
    checker's context is built here, not by the thing being checked, and its tier
    goes through the dispatcher so a nested spawn cannot inherit the wrong model.
    The verdict is a lookup, never an interpretation — `{}` degrades to
    "not verified", which re-checks next time rather than recording a lie.

    The verdict arrives in the agent's **final message**, not a file:
    `stale-hunter` is read-only by charter and has no Write tool, so a file
    handover could never work (see `_verdict_from_message`). `verdict.json` is
    still honoured if present, so a future checker that does write one keeps
    working, but the message is the channel that actually carries it.
    """
    verdict_path = out_dir / "verdict.json"
    prompt = _STALE_PROMPT.format(model=model, doc=doc_rel,
                                  verdict_path=verdict_path.resolve().as_posix())
    textio.write_text_lf(out_dir / "prompt.md", prompt)

    binary = claude_binary()
    if not binary:
        return {}, 0.0, None
    argv = [
        binary, "-p",
        "--agent", "stale-hunter",
        "--model", model,
        *_STREAM_ARGV,
        *_budget_argv(card_budget),
        "--permission-mode", str(host_setting(root, "permission_mode", "acceptEdits")),
        "--add-dir", str(root.resolve()),
    ]
    cost = 0.0
    try:
        proc = _run_worker(argv, root, timeout, out_dir / "stream.jsonl",
                           prompt=prompt)
        textio.write_text_lf(out_dir / "run.log", proc.stdout + proc.stderr)
        result = _terminal_result(proc.stdout)
        try:
            cost = float(result.get("total_cost_usd", 0.0))
        except (ValueError, AttributeError, TypeError):
            pass
        wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return {}, cost, None
    verdict = _read_verdict(verdict_path)
    if not verdict:
        verdict = _verdict_from_message(str(result.get("result") or ""))
        if verdict:
            # Persisted so the run dir stays the full record of the check even
            # though the checker could not write it itself.
            textio.write_text_lf(verdict_path, json.dumps(verdict, indent=2))
    return verdict, cost, wall


def _quote_safe(text: str) -> str:
    """Neutralise backticks in tool output before it is written into a card.

    The runner copies gate and worker output verbatim into tracked markdown
    (`## Error`). Any backticked token in that output therefore becomes a real
    reference in a real doc — and `doc_reference_liveness` scans `Board/`. On
    2026-07-30 that closed a loop: one dangling reference failed a card, the
    failure message naming it was written into the card, and the next two cards
    failed on *that* text. One root cause, three cards, and the third message
    quoted the second quoting the first.

    So tool output is data, not markup: backticks become straight quotes on the
    way in. Cheap, lossless for a reader, and it makes the card's own error text
    inert to every gate that reads docs.
    """
    return text.replace("`", '"')


def _stale_slug(doc_rel: str) -> str:
    """A clean card id/filename stem from a doc path — no leading dot, no slash,
    so `card_schema`'s id==stem rule holds and the file is not hidden."""
    import re
    body = re.sub(r"[^a-z0-9]+", "-", doc_rel.lower().removesuffix(".md")).strip("-")
    return f"fix-stale-{body}"


def _stale_card_text(doc_rel: str, verdict: dict, report_dir: Path) -> str:
    """A fix-card for a doc stale-hunter flagged. It carries the findings but not
    the fix: choosing the fix is Tier 3 (a doc may be right and the *code* drifted),
    so this parks the evidence for a code-thread worker or Karel, which is the
    producer/checker seam holding — the checker reports, it does not repair.

    report_dir must already be relative to the repo root (callers pass
    `out_dir.relative_to(root)`) — an absolute path bakes in this machine's
    checkout location and reads as broken on any other machine."""
    findings = verdict.get("findings") or []
    lines = [f"- **{f.get('claim', '?')}** — {f.get('cite', '?')}: {f.get('why', '')}"
             for f in findings if isinstance(f, dict)]
    body = "\n".join(lines) or "(see the run log)"
    return f"""---
id: {_stale_slug(doc_rel)}
title: "Staleness: {doc_rel} names source that has drifted"
state: tasks
tier: worker
worker: code-thread
recipe: none
unattended: false
verify: review
created: {dt.date.today().isoformat()}
---

## Intent

`stale-hunter` checked `{doc_rel}` against current source and reported drift. Each finding
quotes the doc verbatim and cites where the source now disagrees. **Choosing the fix is not
mechanical** — a doc may be right and the *code* may have drifted, or the doc may be the one
that is wrong. That judgement is why this is a card, not an auto-edit.

Full report: `{report_dir.as_posix()}`

## Approach

Reconcile the doc with the code it describes, one finding at a time: for each, decide whether
the *doc* drifted (correct the doc) or the *code* did (leave the doc, flag that separately) —
this card edits the doc, never code. Prefer deleting dead material over annotating it.

## Findings

{body}

## Acceptance

- Each finding above is resolved: the doc corrected, or the finding dismissed with a written
  reason (the source was right, or the claim is `unverifiable` intent). Prefer deleting dead
  material over annotating it (`feedback_doc_leanness`).
- `python -m nightshift.gates.run` stays clean.

## Open questions

none
"""


def stale_phase(root: Path, count: int, model: str, deadline, card_budget: float,
                timeout: int, record: run_record.Record | None = None) -> tuple[int, int, int]:
    """After the cards: spend leftover budget on the highest-churn docs.

    Deterministic selection (`stale_sweep`, no LLM), then one Tier-2 check per
    doc. A doc is written to the ledger only on a *complete* verdict, so a phase
    the deadline or kill switch cuts short re-checks that doc next time — Karel's
    rule. Findings become one fix-card per doc; the runner never edits a doc.

    Returns (checked, verified, carded) — the three numbers `stale_status.json`
    has always carried. `record`, when given, additionally receives the two this
    triple cannot express and that Karel's morning needs: how many docs were
    *selected* (58, against 0 verified, is a broken sweep; 1 against 1 is a quiet
    one, and the old triple renders both as "nothing to report"), and how many
    verdicts came back incomplete. Flushed inside the loop, not after it, so a
    sweep killed at 05:57 still says what it got through.
    """
    ledger = stale_sweep.load_ledger(root)
    chosen = stale_sweep.select(root, count, ledger)
    if not chosen:
        _log("stale sweep: nothing has changed since last verification — skipping")
        if record is not None:
            record.stale(selected=0, checked=0, verified=0, carded=0)
        return 0, 0, 0
    _log(f"stale sweep: {len(chosen)} doc(s) selected by churn, most-churned first")

    checked = verified = carded = incomplete = 0
    carded_cards: list[str] = []

    def _flush() -> None:
        if record is not None:
            record.stale(selected=len(chosen), checked=checked, verified=verified,
                         carded=carded, incomplete=incomplete, cards=carded_cards)

    _flush()
    for cand in chosen:
        if deadline and dt.datetime.now() >= deadline:
            _log(f"stale sweep: stopping — past {deadline:%H:%M}")
            break
        if _stop_requested():
            _log("stale sweep: stopping — kill switch appeared")
            break
        out_dir = stale_run_dir(root, cand.doc)
        _status(root, phase="stale-sweep", doc=cand.doc, model=model, since=_now())
        verdict, cost, wall = run_stale_check(root, cand.doc, out_dir, model, card_budget, timeout)
        checked += 1
        # The artefact decides, not the process's exit
        # (`wall-on-review-wrapup-discards-a-verdict`). `stale-hunter` is a pure
        # judge — its verdict *is* its entire output — so one that already says
        # `complete: true` means the doc was read to the end and the wall landed
        # on the wrap-up call after it. Honour it: the findings get carded and the
        # doc ledgered below, and only *then* does the sweep stop. A wall with
        # nothing complete written still leaves the ledger untouched, exactly as
        # before, so the doc is re-checked next time rather than recorded as
        # verified on a check that never finished.
        if wall is not None and not verdict_survives_a_wall(STALE_STAGE, verdict):
            _log(f"  {cand.doc}: usage limit during the sweep — leaving the ledger untouched")
            _flush()
            break
        if not verdict.get("complete"):
            _log(f"  {cand.doc}: no complete verdict — will re-check next time (not ledgered)")
            incomplete += 1
            _flush()
            continue

        # Order matters, and it is card → commit → ledger. The ledger is what
        # stops a doc being re-checked, so marking it before the findings are
        # durable is how a night can lose findings permanently: the doc reads
        # "verified" forever while the card carrying its drifts was never
        # committed. Committing per doc is the same guarantee the card loop above
        # gets from its per-card `publish` ("a cloud container can be killed
        # between cards") — this loop was written without it, and one killed
        # sweep is all it takes. A doc with no findings needs no commit: its only
        # record is the gitignored, per-machine, rebuildable ledger, so losing
        # that costs a re-check and nothing else.
        findings = verdict.get("findings") or []
        if findings:
            card_path = board.board_dir(root) / "tasks" / f"{_stale_slug(cand.doc)}.md"
            # Relative to root, not out_dir as-is: out_dir is anchored to this
            # machine's checkout, and Karel works this repo from more than one
            # machine with different absolute paths above the repo root.
            textio.write_text_lf(
                card_path, _stale_card_text(cand.doc, verdict, out_dir.relative_to(root)))
            carded += 1
            carded_cards.append(card_path.stem)
            # Status rides along in the same commit so "a sweep got this far" is a
            # committed fact even if the run never reaches the digest.
            stale_sweep.write_status(root, dt.date.today().isoformat(),
                                     checked, verified + 1, carded)
            board.commit_board(root, f"stale: {cand.doc} — {len(findings)} drift(s) carded",
                               extra_paths=(str(stale_sweep.STATUS),))
            _log(f"  {cand.doc}: {len(findings)} drift(s) — carded {card_path.name}")
        else:
            _log(f"  {cand.doc}: {verdict.get('summary', 'no drift')} — verified")
        stale_sweep.mark_verified(root, cand.doc, ledger)
        verified += 1
        _flush()
        if wall is not None:
            # This doc's verdict was honoured and is now durable — card committed,
            # ledger marked. The window is still shut, so the sweep stops here
            # rather than spawning the next `stale-hunter` into it.
            _log(f"  {cand.doc}: the checker walled on its wrap-up after a complete "
                 f"verdict — honoured and ledgered; stopping the sweep there")
            break
    _flush()
    return checked, verified, carded


# `_run_gates` outcomes. Three, not two, because "the gates said no" and "the
# gate harness fell over" are different facts about different things, and
# collapsing them is what blamed `ice-damage` for `deletion_sweep` crashing on
# 2026-07-23. A violation is about the card; a crash is about this machine, and
# no card can be judged until it is fixed.
GATE_PASS, GATE_VIOLATION, GATE_CRASH = "pass", "violation", "crash"

# How the gate suite is invoked. `-m`, not a path into `.ai/gates/`: step 3 of the
# extraction moved the gate runner into the installed `nightshift` package, so the
# old `<worktree>/.ai/gates/run.py` no longer exists and the module form is the one
# that resolves from any worktree — the same argv `nightshift.preflight` uses.
#
# A module-level constant rather than a literal inside `_run_gates` because the
# tests need to substitute a scripted harness. They used to do it by writing a stub
# to `.ai/gates/run.py` in the fixture repo — which is exactly why the suite stayed
# green while every real dispatch died here: the fixture created the file the
# extraction had deleted, so the tests never exercised the broken path.
GATE_ARGV: list[str] = [sys.executable, "-m", "nightshift.gates.run"]


def _run_gates(root: Path, cwd: Path, log: Path) -> tuple[str, str]:
    out = subprocess.run(GATE_ARGV, cwd=cwd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    stdout, stderr = out.stdout or "", out.stderr or ""
    textio.write_text_lf(log, stdout + stderr)
    if out.returncode == 0:
        return GATE_PASS, ""

    # The gate runner exits 1 both for "violations found" and for an uncaught exception
    # inside a gate, so the exit code alone cannot tell them apart. The
    # traceback can — and it is on **stderr**, which the previous version of this
    # function never read, so a crash produced the reason `"gates: "` with
    # nothing after it and the card carried that into its `## Error`.
    if "Traceback (most recent call last)" in stderr:
        tail = [l.strip() for l in stderr.splitlines() if l.strip()]
        return GATE_CRASH, ("the gate harness crashed, so no card can be judged on this "
                            f"machine — {tail[-1] if tail else f'exit {out.returncode}'}")

    lines = [l for l in stdout.splitlines() if l.strip()]
    if not lines:
        # Non-zero with nothing said. Whatever this is, it is not a verdict about
        # the card, and guessing that it is spends an attempt for free.
        return GATE_CRASH, (f"the gate harness exited {out.returncode} and reported "
                            f"nothing at all — see {log.name}")
    return GATE_VIOLATION, "gates: " + "; ".join(lines[:4])


def _gate_violations_json(cwd: Path) -> list[dict] | None:
    """A second, structured read of the same gate run `_run_gates` just did —
    `nightshift.gates.run --json`'s `file`/`line`/`rule` per violation, for the
    blocked-vs-failed classifier below. `None` when the payload does not parse
    (an older or scripted gate harness that does not understand `--json`, or a
    crash between the two calls): the caller must treat that exactly like a
    violation with no usable path, never as license to guess.

    A second subprocess call rather than threading a fourth return value through
    `_run_gates` and every one of its callers: gates are deterministic Python
    with no LLM (00_architecture.md SS12), so re-running them is cheap, and it
    keeps `_run_gates`'s existing contract — and `gates.txt`'s human-readable
    text — untouched for the rebase-and-merge caller that has no use for paths.
    """
    out = subprocess.run([*GATE_ARGV, "--json"], cwd=cwd, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    try:
        data = json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    violations = data.get("violations") if isinstance(data, dict) else None
    return violations if isinstance(violations, list) else None


def _is_repo_drift(violations: list[dict] | None, changed: set[str]) -> bool:
    """Whether a gate violation is a fact about the repo rather than about this
    attempt's own diff — every violation names a path, and every one of those
    paths lies outside `changed` (`git diff --name-only base...branch`).

    `False` — never drift, so the caller falls back to `failed` — when the
    payload did not parse (`violations is None`) or when *any* violation is
    missing a usable path or names a path this attempt actually touched.
    Unparseable or ambiguous must never buy a free attempt (Acceptance); one
    violation inside the diff is enough to make this the card's own problem,
    even if every other violation points elsewhere.
    """
    if not violations:
        return False
    for v in violations:
        path = str(v.get("file", "")).strip()
        if not path or path in changed:
            return False
    return True


# How the runner parallelises pytest — `suite.parallel_args()`, which resolves to
# `--dist loadfile` with either `-n auto` or a memory-capped worker count, and to
# `()` when pytest-xdist is missing (see that function for why a missing optional
# plugin must degrade rather than fail the run, and `suite.worker_count` for why
# `auto` alone can exhaust RAM on a many-core box and report it as two dozen
# unrelated game-test failures). Resolved once at import and kept as a named
# module-level tuple, so a test can still blank it to skip xdist's startup when
# it is running a one-test fixture.
_PYTEST_PARALLEL: tuple[str, ...] = suite.parallel_args()


def _check_junit(path: Path) -> tuple[bool, str]:
    """The JUnit verdict — `suite.check_junit`, shared with the preflight.

    A one-line delegation rather than a second copy: the preflight judges its own
    pytest run the same way, and "0 collected is a failure" is exactly the rule
    that must not drift between two callers.
    """
    return suite.check_junit(path)


def _run_tests(cwd: Path, log: Path, timeout: int, junit: Path,
               pytest_args: list[str]) -> tuple[bool, str, str]:
    """Run the selected slice of the suite over the worktree and judge it by the
    JUnit report it writes.

    `pytest_args` is `suite.Selection.pytest_args` — the game/system slice this
    diff needs. `--dist loadfile` runs the slice across cores **by file**:
    every test in a file stays on one worker, so a file's module-level state and
    in-file ordering are preserved (the order-dependent game tests break under the
    default `--dist load`, which splits a file across workers; `loadfile` is the
    structural version of "the conflicting tests still run serially"). The verdict
    comes from `_check_junit`, not the exit code — though a non-zero exit with a
    clean-looking report is still failed, because that is a collection/internal
    error the per-test XML would not carry.

    An **empty** `pytest_args` is the `suite.NONE` selection — a board-notes-only
    diff (`ideas/`/`inbox/`), whose validity is a gate's job, not pytest's. It is a
    pass with no run: running pytest with no path argument would collect the whole
    tree from cwd, the opposite of the intent. See `suite.Selection.pytest_args`.

    Returns `(ok, why, evidence)`. `evidence` is `suite.failure_excerpt`'s block —
    which failing tests and which assertions — and it is empty on a pass. It is
    returned rather than left in `junit.xml` because `junit.xml` lives under
    `.ai/runs/`, which does not survive the card being retired and never leaves
    this host at all; the card is the artefact that does both.
    """
    if not pytest_args:
        textio.write_text_lf(
            log,
            "no pytest applies to this diff (board notes only — the " "card_schema gate is the check)\n")
        return True, "", ""
    argv = [sys.executable, "-m", "pytest", *pytest_args, "-q",
            *_PYTEST_PARALLEL, f"--junitxml={junit}"]
    out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                         encoding="utf-8", errors="replace")
    stdout, stderr = out.stdout or "", out.stderr or ""
    textio.write_text_lf(log, " ".join(argv) + "\n\n" + stdout + stderr)
    ok, why = _check_junit(junit)
    if ok and out.returncode != 0:
        # A clean-looking report with a non-zero exit: a collection or internal
        # error, which the per-test XML cannot carry. pytest's own `FAILED` lines
        # on stdout are then the only evidence there is, so they become the block.
        failed = [l for l in stdout.splitlines() if l.startswith("FAILED")]
        why = "pytest: " + ("; ".join(failed[:4]) or f"exited {out.returncode} with a "
                            "clean report — a collection or internal error")
        return False, why, "\n".join(f"    {line}" for line in failed[:suite.EXCERPT_TESTS])
    return ok, why, ("" if ok else suite.failure_excerpt(junit))


def _run_worker(argv: list[str], cwd: Path, timeout: int,
                stream_path: Path | None = None,
                env: dict[str, str] | None = None,
                prompt: str = "") -> subprocess.CompletedProcess:
    """The one place the Claude CLI is executed.

    A named seam rather than an inline call, for two reasons. It is the only
    line in this file that is not deterministic, so isolating it makes the §12
    claim checkable rather than a promise (`tests/test_board_runner.py` asserts
    no decision function reaches it). And it is what lets the whole dispatch
    cycle — worktree, verdict, gates, tests, card move, digest — be driven end
    to end in a test without spending a night and a budget to find out that a
    file move was wrong.

    `Popen` + two draining threads, not `subprocess.run`: reading only one of
    stdout/stderr while the other fills is a deadlock once either pipe's OS
    buffer is full, so both have to be drained concurrently regardless of
    whether anything is teed to disk. Every stdout line is appended to an
    accumulator *and*, when `stream_path` is given, written to it and flushed
    immediately — that tee is the whole card: a worker's tool calls land on
    disk as they happen instead of staying invisible until the process exits.
    The return value is still `.returncode`/`.stdout`/`.stderr` shaped, so
    nothing downstream had to change shape.

    **The drain threads' joins are bounded on every path, not only the timeout
    one.** A child that exits promptly while a grandchild it spawned (a Bash
    tool call, an MCP server) still holds the pipe's write end open leaves
    `readline()` never seeing EOF — the drain thread never finishes on its own.
    An unbounded `join()` on the success path would then block forever with no
    timeout backstop at all (the timeout guard below only covers `proc.wait()`,
    not drain-thread completion). So both paths `join(timeout=...)` and log-and-
    proceed rather than wait, matching this file's existing rule that an
    observer able to end the run is worse than none.

    **`prompt` goes down the child's stdin, never on `argv`** (worker-prompt-off-argv).
    Windows caps a `CreateProcess` command line at 32,767 characters, so the old
    `[binary, "-p", prompt, ...]` shape died with `WinError 206` — "the filename or
    extension is too long" — the moment a card grew past ~32 KB. That happened for
    real on 2026-08-06 and took the whole overnight queue with it. `claude -p`
    already reads its prompt from stdin when no prompt positional is given
    (`--input-format` defaults to `text`), so this is a change of transport with no
    change of meaning: the text is still the opening user message, and `--agent`,
    `--resume`, `--max-budget-usd` and the `stream-json` tee are untouched. No
    prompt this project can build is size-limited by the OS again.

    Three details it is easy to get wrong here:

    * **A third thread, not an inline write.** ~40 KB does not fit a pipe buffer, so
      the write blocks until the child drains it — and a child that never reads
      stdin would block it forever, *before* `proc.wait(timeout=...)` is reached.
      That would quietly convert the timeout guarantee into a hang. Feeding on its
      own daemon thread keeps the guard reachable on every path; the joins below
      bound it exactly like the drain threads.
    * **stdin is opened and closed even when `prompt` is empty.** Today the child
      inherits the runner's console stdin, which is neither wanted nor observable.
      A closed pipe is an immediate EOF, which is what a non-interactive worker
      should see.
    * **`newline=""` on the write side.** `text=True` gives a `TextIOWrapper` whose
      default translates every `\\n` to `os.linesep`, i.e. silently CRLF-ifies the
      prompt on Windows. The prompt must reach the model byte-for-byte as written
      to `prompt-N.md`, so the translation is pinned off — the same rule the
      `write_newline` gate enforces everywhere else in this tree.
    """
    stream_fh = None
    if stream_path is not None:
        try:
            stream_fh = open(stream_path, "a", encoding="utf-8", errors="replace",
                             newline="")
        except OSError:
            stream_fh = None

    try:
        # `encoding=` is not optional: the worker's JSON carries em-dashes and
        # cs/es diacritics, and the locale codec on Windows has no mapping for
        # some of those bytes. Without it the decode raises in the drain
        # thread, the accumulator stays empty, and the verdict silently reads
        # as empty.
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=env)
    except OSError:
        if stream_fh is not None:
            stream_fh.close()
        raise

    # Pin newline translation off on the write side; see the docstring. Guarded
    # rather than assumed: this is best-effort formatting fidelity, and losing it
    # must never be the thing that fails a dispatch.
    try:
        proc.stdin.reconfigure(newline="")
    except (AttributeError, OSError, ValueError):
        pass

    out_lines: list[str] = []
    err_lines: list[str] = []

    def _drain_stdout() -> None:
        try:
            for line in proc.stdout:
                out_lines.append(line)
                if stream_fh is not None:
                    try:
                        stream_fh.write(line)
                        stream_fh.flush()
                    except (OSError, ValueError):
                        # A tee failure (disk full, a locked file) must not
                        # silently kill accumulation of the real stdout the
                        # verdict and cost are read from — swallow it here,
                        # the same way `_log`/`_status` swallow their own.
                        pass
        finally:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                err_lines.append(line)
        finally:
            try:
                proc.stderr.close()
            except (OSError, ValueError):
                pass

    def _feed_stdin() -> None:
        try:
            if prompt:
                proc.stdin.write(prompt)
                proc.stdin.flush()
        except (OSError, ValueError):
            # A child that exited before reading breaks the pipe. That is the
            # child's failure to report, not a reason to raise from the seam —
            # its exit code and stderr are already on their way back.
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()
    # Started *after* the drains, on purpose: a big prompt only fits down the pipe
    # as fast as the child consumes it, and a child blocked on its own full stdout
    # would never get there.
    t_in = threading.Thread(target=_feed_stdin, daemon=True)
    t_in.start()

    def _close_stream() -> None:
        if stream_fh is not None:
            try:
                stream_fh.close()
            except (OSError, ValueError):
                pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        t_in.join(timeout=5)
        _close_stream()
        raise subprocess.TimeoutExpired(argv, timeout, output="".join(out_lines),
                                        stderr="".join(err_lines))

    # Bounded exactly like the timeout path above, for the grandchild-holds-
    # the-pipe-open case described on the docstring: proc has exited, but the
    # drain thread may still be blocked on `readline()` with no EOF in sight.
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    t_in.join(timeout=5)
    _close_stream()

    return subprocess.CompletedProcess(argv, proc.returncode,
                                       "".join(out_lines), "".join(err_lines))


#: Supported name for the seam above, for callers outside this module.
#:
#: `ingest.py` dispatches the CLI too, and the docstring's claim — *the one place the
#: Claude CLI is executed* — is worth keeping literally true rather than nearly true.
#: A second copy of the `Popen` + two-draining-threads dance is exactly the drift this
#: avoids: that pattern is not defensive tidiness, it is the fix for a deadlock that
#: took a whole overnight queue on 2026-08-06, and a copy would not carry the reason.
#:
#: An alias rather than a move: relocating 180 lines and its logger out of this module
#: is a refactor with its own risk, and nothing needs it yet. When a third caller
#: appears, extract properly instead of adding a second alias.
run_cli = _run_worker


def _worker_env(root: Path, tree: Path, out_dir: Path) -> dict[str, str]:
    """The environment a dispatched worker runs under.

    Everything is inherited (the CLI keeps its PATH, auth and config) plus one
    addition: the fence env var (`[worker].fence_env`), the worktree fence's
    allowlist. A project that declares none gets a plain inherited environment
    and the fence stays off. The roots a worker may
    legitimately write to are exactly three — its own worktree; its run dir under
    `.ai/runs/` (where the verdict JSON lands, which is outside the worktree); and
    `Board/` (the prompt lets it append to the card's `## Thread`). Anything else
    is the wrong-checkout defect the fence exists to stop.
    """
    allow = os.pathsep.join(
        str(p.resolve()) for p in (tree, out_dir, board.board_dir(root)))
    name = fence_env(root)
    return {**os.environ, name: allow} if name else dict(os.environ)


def assert_integration_unmoved(root: Path, base: str, expected_sha: str) -> bool:
    """The guarantee behind the fence: a worker must never land a commit on the
    shared integration branch. The hook *prevents* the wrong-checkout write; this
    *undoes* one that slipped through anyway (a path the hook could not see, or a
    machine where the hook was not read).

    `base` is checked out in the main checkout, so a stray worker commit shows up
    as `base` having moved past the tip the runner last committed. Reset it back —
    unconditionally, because the shared branch is never the worker's to advance;
    the card's own success is still judged on its `ai/<id>` branch, untouched here.
    A no-op (returns False) when `base` is where the runner left it. No LLM (§12):
    two `rev-parse`s and, only if they differ, a reset.
    """
    if not expected_sha:
        return False
    now = _git(root, "rev-parse", base).stdout.strip()
    if now == expected_sha:
        return False
    # `reset --hard` on the main checkout discards the stray commit *and* any
    # uncommitted files the worker wrote there — both are the defect, both go. The
    # worktree on `ai/<id>` is a separate checkout and is not touched.
    _git(root, "reset", "--hard", expected_sha)
    return True


def run_producer(root: Path, card: board.Card, tree: Path, out_dir: Path, branch: str,
                 base: str, model: str, card_budget: float, timeout: int,
                 round_no: int = 1, feedback: str = "", resume_session: str = "",
                 continue_note: str = "") -> tuple[dict, float, int, limits.Wall | None]:
    """One producer round. Its verdict, its cost, its exit code, and the usage
    limit it hit if it hit one.

    `resume_session` and `continue_note` continue a limit-interrupted attempt
    (runner-worker-handover). With `resume_session` set, the CLI is invoked
    `--resume <id>` on a nudge rather than the full card, warm-continuing the
    previous session. A `--resume` that errors — the session expired, say — is
    **not** allowed to burn the attempt: it falls through here, inside the same
    call, to a fresh session that re-enters the kept worktree and reads its
    uncommitted diff. `continue_note` (used without `resume_session`, or by that
    fallback) is a `## Progress`-style addendum telling a fresh worker that the
    worktree already holds interrupted work.
    """
    verdict_path = out_dir / f"verdict-{round_no}.json"
    binary = claude_binary()
    assert binary  # preflight checked this

    def _cold_prompt() -> str:
        return _PROMPT.format(
            tier=card.tier, model=model, branch=branch, base=base,
            card_path=(board.board_dir(root) / "tasks" / card.path.name).resolve().as_posix(),
            verdict_path=verdict_path.resolve().as_posix(),
            tool_economy=worker_prompt.TOOL_ECONOMY,
            card_body=card.text,
        ) + (_CHORE_NOTE if card.kind == board.KIND_CHORE else "") \
          + continue_note + feedback

    def _argv(session: str) -> list[str]:
        """Flags only — the prompt reaches the child on stdin (`_run_worker`)."""
        out = [
            binary, "-p",
            "--agent", card.worker,
            "--model", model,
            *_STREAM_ARGV,
            *(["--resume", session] if session else []),
            *_budget_argv(card_budget),
            "--permission-mode", str(host_setting(root, "permission_mode", "acceptEdits")),
            "--add-dir", str((board.board_dir(root)).resolve()),
            "--add-dir", str(out_dir.resolve()),
        ]
        # An optional allowlist, for running the worker below `bypassPermissions`.
        # Worth knowing before reaching for it: a tool the card turns out to need
        # and the list does not carry is *denied*, and in `-p` mode there is
        # nobody to ask — so the worker stalls, the attempt is spent, and the card
        # fails for a reason that looks nothing like the real one. A tight list
        # trades one risk for a subtler one; that is why it is opt-in.
        if allowed := host_setting(root, "allowed_tools", None):
            out += ["--allowed-tools", *allowed] if isinstance(allowed, list) else \
                   ["--allowed-tools", str(allowed)]
        return out

    def _once(prompt: str, session: str) -> tuple[dict, float, int, limits.Wall | None]:
        textio.write_text_lf(out_dir / f"prompt-{round_no}.md", prompt)
        proc = _run_worker(_argv(session), tree, timeout,
                           out_dir / "stream.jsonl", env=_worker_env(root, tree, out_dir),
                           prompt=prompt)
        # `worker-N.json` holds exactly the terminal result event, not the raw
        # multi-line stream — `read_telemetry`/`_session_id` both `json.loads`
        # this file as one object, which a raw JSONL tee would break. Falls
        # back to the raw stdout only when nothing on it parsed at all, so a
        # garbled run still leaves *something* to read on disk.
        result = _terminal_result(proc.stdout)
        textio.write_text_lf(
            out_dir / f"worker-{round_no}.json",
            json.dumps(result) if result else proc.stdout)
        if proc.stderr:
            textio.write_text_lf(out_dir / f"worker-{round_no}.stderr.txt", proc.stderr)
        spent = 0.0
        try:
            spent = float(result.get("total_cost_usd", 0.0))
        except (ValueError, AttributeError, TypeError):
            pass
        return (_read_verdict(verdict_path), spent, proc.returncode,
                limits.detect(proc.returncode, proc.stdout, proc.stderr))

    if resume_session:
        prompt = _RESUME_PROMPT.format(verdict_path=verdict_path.resolve().as_posix())
    else:
        prompt = _cold_prompt()

    cost = 0.0
    try:
        verdict, spent, code, wall = _once(prompt, resume_session)
        cost += spent
        # A `--resume` that errored (and did not merely hit the wall again) burns
        # nothing: fall through to a fresh session in the same kept worktree,
        # same attempt. The uncommitted diff is the handover (path 2).
        if resume_session and wall is None and code != 0:
            _log(f"    --resume failed (exit {code}) — re-entering the kept worktree "
                 f"with a fresh session, same attempt")
            verdict, spent, code, wall = _once(_cold_prompt() + _REENTER_NOTE, "")
            cost += spent
        return verdict, cost, code, wall
    except subprocess.TimeoutExpired:
        # A timeout is the card's problem, not the plan's: the process was
        # running and producing tokens for the whole hour. Never a wall.
        return {}, cost, 124, None


def _limit_reached(root: Path, card: board.Card, tree: Path, branch: str,
                   out_dir: Path, round_no: int, wall: limits.Wall, cost: float,
                   detail: str) -> Dispatch:
    """A wall landed. Decide what of this attempt to preserve (runner-worker-handover).

    Three shapes come out of here, all of them `limited`:

    * **Empty first call** — nothing was done and there is no prior handover. This
      is the pre-existing behaviour: drop the worktree (settle drops the branch
      too), give the attempt back, preserve nothing. `kept=False` says so.
    * **Warm interruption** — there is uncommitted work, or a handover already
      exists. Keep the worktree, record the session id and the working-tree hash,
      and hand the attempt back so the next dispatch re-enters. `kept=True`.
    * **Stuck** — a warm interruption whose working tree has not moved for
      `NO_PROGRESS_STOP` consecutive resumes. The give-back has become a loop, so
      the attempt is spent and settle files the card to `failed/`. `stuck=True`.

    The worktree is **not** dropped on the warm/stuck paths here — settle owns
    that, because only it knows the card's final disposition.
    """
    prior = read_handover(root, card.id)
    had_prior = bool(prior.session_id or prior.diff_hash)
    dirty = _worktree_dirty(root, tree)

    if not dirty and not had_prior:
        drop_worktree(root, tree)
        return Dispatch("limited", detail, cost, round_no, wall, kept=False)

    session = _session_id(out_dir, round_no) or prior.session_id
    state_hash = _worktree_state_hash(tree)
    if not had_prior:
        # First interruption — Step 1: nothing to compare against, so it always
        # counts as progress and the attempt is given back unconditionally.
        progressed, no_progress = True, 0
    else:
        progressed = state_hash != prior.diff_hash
        no_progress = 0 if progressed else prior.no_progress + 1
    stuck = (not progressed) and (no_progress >= NO_PROGRESS_STOP)

    write_handover(root, card.id, Handover(session, state_hash, no_progress))
    if not progressed:
        _log(f"    {card.id} resumed but the working tree did not move "
             f"({no_progress}/{NO_PROGRESS_STOP} — "
             + ("filing as stuck" if stuck else "attempt spent") + ")")
    message = detail if progressed or stuck else \
        f"{detail} (no working-tree progress since the last attempt)"
    return Dispatch("limited", message, cost, round_no, wall,
                    kept=True, progressed=progressed, stuck=stuck)


def dispatch(root: Path, card: board.Card, base: str, model: str,
             card_budget: float, test_timeout: int,
             test_selector: Callable[[set[str], Path], suite.Selection]
             = suite.select) -> Dispatch:
    """One attempt. Every exit path leaves the card's runner fields consistent.

    `test_selector` is how the gates-green diff picks its pytest slice, and it
    defaults to the only answer that is safe on its own: `suite.select`, the
    game/system split. A caller that runs the **whole** suite over the result
    afterwards — the chore batch is the one that does — may pass
    `suite.touched` for a narrower first pass; part 1b of `suite.py` states why
    that condition is not optional. A seam rather than a boolean because the two
    are both selections and the choice is which one, not whether.

    A card naming a `checker:` runs a **producer→checker loop inside this one
    attempt** (`00_architecture.md` §16's second seam), bounded by `MAX_ROUNDS`.
    The loop lives here rather than inside the producer for four reasons, and the
    first is the one that decided it: every step of it is a file-state lookup or
    an integer comparison, which is exactly the work §5 says belongs in the
    runner. The others — the bound becomes a `range()` instead of a sentence in
    a charter; a machine that dies in round 2 resumes rather than losing the
    attempt; the checker's blindness stops being a rule the producer could break
    and becomes a fact about how its context is built; and both tiers go through
    the dispatcher instead of a nested spawn silently inheriting a model.
    """
    attempt = card.attempts + 1
    out_dir = run_dir(root, card, attempt)

    try:
        tree, branch, mode = prepare_worktree(root, card, base)
    except RuntimeError as exc:
        return Dispatch("failed", str(exc))

    # Committed BEFORE the worker starts. A machine that dies mid-dispatch must
    # come back with `attempts` already spent, or a reboot loop retries forever.
    card.write({"attempts": str(attempt), "branch": branch,
                "started": _now(), "finished": None})
    board.commit_board(root, f"board: {card.id} attempt {attempt} ({model})")

    # The tip of the integration branch right before the worker runs — the sha the
    # worktree fence's backstop restores to if a worker commits to `base` from the
    # wrong checkout (`assert_integration_unmoved`). Captured *after* the board
    # commit above, which is the last legitimate move of `base` before dispatch.
    base_tip = _git(root, "rev-parse", base).stdout.strip()

    # How a limit-interrupted card continues (runner-worker-handover). Only the
    # first producer round of the attempt resumes/re-enters; later checker rounds
    # are feedback-driven fresh prompts as before. REENTER prefers `--resume` when
    # the session survived and falls back to the uncommitted diff otherwise;
    # FROM_WIP hands over a `## Progress` note pointing at the recovered commit.
    handover = read_handover(root, card.id)
    if mode == REENTER:
        resume_session = handover.session_id
        continue_note = "" if resume_session else _REENTER_NOTE
        _log(f"  {card.id} was interrupted — "
             + ("resuming its session" if resume_session
                else "re-entering its worktree with a fresh session"))
    elif mode == FROM_WIP:
        resume_session = ""
        continue_note = _WIP_NOTE.format(card_id=card.id)
        _log(f"  {card.id} worktree was lost — continuing from its `wip:` commit")
    else:
        resume_session = ""
        continue_note = ""

    checked = card.checker != "none"
    max_rounds = MAX_ROUNDS if checked else 1
    cost = 0.0
    verdict: dict = {}
    review: dict = {}
    rescued = 0
    feedback = ""
    tried: list[str] = []
    round_no = 0
    # A wall that landed on a stage which had *already* written a terminal
    # verdict (`verdict_survives_a_wall`). The verdict is honoured, so this
    # attempt is no longer `limited` — but the night's window is still closed, so
    # the wall rides home on every `Dispatch` this function returns from here on
    # and the run loop applies its sleep-or-stop after the card has settled.
    honoured_wall: limits.Wall | None = None

    while round_no < max_rounds:
        round_no += 1
        _log(f"  dispatching {card.id} → {card.worker} @ {model} "
             f"(attempt {attempt}, round {round_no}/{max_rounds})")
        _status(root, phase="worker", card=card.id, attempt=attempt,
                round=round_no, of_rounds=max_rounds, worker=card.worker,
                model=model, since=_now())

        verdict, spent, code, wall = run_producer(
            root, card, tree, out_dir, branch, base, model, card_budget,
            test_timeout * 6, round_no, feedback,
            resume_session=resume_session if round_no == 1 else "",
            continue_note=continue_note if round_no == 1 else "")
        cost += spent
        # Backstop the worktree fence before anything else this round: if the
        # worker committed to the shared integration branch from the wrong
        # checkout, undo it now. Protecting `base` is unconditional and separate
        # from the card's own success, which is still judged on `ai/<id>` below.
        if assert_integration_unmoved(root, base, base_tip):
            _log(f"  ! {card.id} committed to `{base}` from outside its worktree — "
                 f"reset `{base}` to {base_tip[:8]}; the card's work is judged on {branch} "
                 f"(worktree fence backstop, nightshift/hooks/worktree_fence.py)")
        # Before the exit code, always: a wall *is* a non-zero exit, and reading
        # it as one is the bug this ordering exists to prevent — the card would
        # be charged an attempt and an `## Error` for the plan running out.
        if wall is not None:
            # And before the wall, the artefact: a producer that had already
            # written `outcome: parked` walled on its wrap-up, not on the work.
            # Its question is the deliverable and it is complete, so it is
            # honoured rather than thrown away with the window
            # (`wall-on-review-wrapup-discards-a-verdict`, answer B). Harvest
            # first — the candidates are most of what makes a park answerable —
            # then `break` to the park path below, which is also what skips the
            # `code != 0` branch a wall would otherwise fall into.
            if verdict_survives_a_wall(PRODUCER_STAGE, verdict):
                honoured_wall = wall
                _log(f"    {card.id} walled on its wrap-up after parking — honouring the "
                     f"question it already wrote; the night's window is still closed")
                rescued = harvest(root, tree, out_dir)
                break
            return _limit_reached(root, card, tree, branch, out_dir, round_no, wall,
                                  cost, f"usage limit reached — {wall.evidence}")
        if code != 0:
            # Bank any diff the worker managed before it crashed, so a non-zero
            # exit costs the process, not the work (same reasoning as the
            # empty-diff path below — commit_wip is a no-op on a clean tree).
            commit_wip(root, tree, card.id)
            # An API disconnection that never reached verification is an
            # interruption, not a verdict on the card (failed-attempt-work-is-
            # deleted-not-resumed, the `api_error` slice): give the attempt back
            # and hand it a resume path, the same as a usage wall — just without
            # the wall's sleep-until-reset, since nothing here says the *plan*
            # is exhausted.
            if _api_error_interruption(out_dir, round_no):
                session = _session_id(out_dir, round_no)
                state_hash = _worktree_state_hash(tree)
                drop_worktree(root, tree)
                write_handover(root, card.id, Handover(session, state_hash, 0))
                # `kept=False`: the worktree was just dropped, and `kept` means
                # exactly "the checkout was preserved for a warm resume in
                # place" — `settle` appends "worktree kept for warm resume" from
                # it, which on this path would be a flat lie in the one line a
                # 6 AM reader trusts. Nothing is lost by saying so: what carries
                # this resume is the `wip:` commit plus the handover written
                # above, which is the `FROM_WIP` path, not the warm one.
                return Dispatch(
                    "interrupted",
                    f"worker interrupted (api_error) before verification ran — "
                    f"resuming from its `wip:` commit next dispatch",
                    cost, round_no, kept=False)
            drop_worktree(root, tree)
            # `evidence` is why, not just that: the gates and tests paths both fill
            # it and this one used to pass nothing, so a dead worker's `## Error`
            # said "worker exited 1" and pointed at a directory about to be pruned.
            return Dispatch("failed", f"worker exited {code}", cost, round_no,
                            evidence=worker_exit_evidence(out_dir))

        # Rescued before any exit path removes the worktree — including the
        # parked one, where the candidates are most of what makes the question
        # answerable.
        rescued = harvest(root, tree, out_dir)

        # A producer that parks is stating an ambiguity, which no amount of
        # re-rolling resolves. Stop the loop rather than spending the remaining
        # rounds re-asking a question it already said it cannot answer.
        if verdict.get("outcome") == "parked":
            break
        if not checked:
            break

        _log(f"    worker done — {card.checker} is judging round {round_no}")
        _status(root, phase="checker", card=card.id, attempt=attempt,
                round=round_no, of_rounds=max_rounds, checker=card.checker,
                model=model, since=_now())
        review, spent, wall = run_checker(root, card, out_dir, round_no, model,
                                          card_budget, test_timeout * 2)
        cost += spent
        if wall is not None:
            # The reported instance of this card, and the reason it exists:
            # 2026-08-09's round-2 art-reviewer wrote `review-2.json` — `pass`,
            # best `menu_card_cyberware_s30.png`, with reasoning — and walled on
            # its final turn, and `review` was fully parsed on the very return
            # path that ignored it. `rounds_left` is what keeps a `revise` with
            # rounds still unspent `limited`: honouring that would file the card
            # as "did not pass in 1 round(s)" and park work that had two rounds
            # it never received.
            if verdict_survives_a_wall(CHECKER_STAGE, review,
                                       rounds_left=max_rounds - round_no):
                honoured_wall = wall
                _log(f"    {card.checker} walled on its wrap-up after writing a complete "
                     f"verdict — honouring it; the night's window is still closed")
            else:
                return _limit_reached(
                    root, card, tree, branch, out_dir, round_no, wall,
                    cost, f"usage limit reached while checking — {wall.evidence}")
        called = str(review.get("verdict", "")).lower()
        _log(f"    {card.checker}: {called or '(no verdict)'} — "
             f"{str(review.get('notes', ''))[:80]}")
        if called == "pass":
            break
        # A checker that reported nothing usable cannot drive another round —
        # re-rolling on no information is how the night gets burnt. Treat it as
        # the loop ending, and let the park path below carry it to Karel.
        if not called:
            break
        tried.append(f"round {round_no}: {str(review.get('notes', ''))[:60]}")
        feedback = _FEEDBACK.format(
            round_no=round_no + 1, max_rounds=max_rounds, verdict=called,
            notes=str(review.get("notes", "")), history="; ".join(tried) or "(none)",
        )

    # `honoured_wall` rides home on every exit path from here down. Which lane the
    # card reaches is settled on its own merits, exactly as it would have been
    # without a wall; what the wall still decides is the *night*, and the run loop
    # reads that off `Dispatch.wall` after the card has settled.
    if verdict.get("outcome") == "parked":
        drop_worktree(root, tree)
        return Dispatch("parked", str(verdict.get("summary", ""))[:300], cost, round_no,
                        honoured_wall)

    # The checker never passed inside the round budget. That is a park, not a
    # failure (§13): the candidates and the critique are exactly what Karel needs
    # to decide in fifteen seconds whether to pick one or rethink the concept.
    if checked and str(review.get("verdict", "")).lower() != "pass":
        drop_worktree(root, tree)
        called = str(review.get("verdict", "")) or "no verdict"
        best = str(review.get("best", "")) or "none named"
        return Dispatch(
            "parked",
            f"{card.checker} did not pass this in {round_no} round(s) — last verdict "
            f"`{called}`, best candidate `{best}`. {str(review.get('notes', ''))[:180]} "
            f"Candidates and every round's critique are in "
            f"`.ai/runs/{card.id}/attempt-{attempt}/`.",
            cost, round_no, honoured_wall,
        )

    # A worker can finish its edits and die before it ever runs `git commit` —
    # most often by kicking off the test suite with run_in_background and ending
    # its turn to "wait for the notification", which in a one-shot headless run
    # kills the task and the turn together, so the commit step never arrives
    # (2026-07-24, orientation-size-budget attempt 2: 11 edits, zero commits, the
    # whole diff dropped with the worktree). An edited-but-uncommitted tree is
    # work, not silence: bank it as a `wip:` commit before deciding the worker
    # produced nothing, exactly as commit_wip already does for the limit path. A
    # complete diff then flows into the gates+tests below and can pass on its own
    # merits; a partial one fails them but is preserved on the branch either way.
    if commit_wip(root, tree, card.id):
        _log(f"    worker left its work uncommitted — banked it as a `wip:` commit "
             f"on {branch} (it never ran git commit itself)")

    # "Produced nothing" means neither a commit nor an artefact. Commits alone
    # would be wrong for art, whose output is deliberately not committed until
    # Karel picks one; artefacts alone would be wrong for code.
    commits = _git(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip()
    if commits in ("", "0") and rescued == 0:
        drop_worktree(root, tree)
        return Dispatch("failed", "the worker produced neither a commit nor an artefact",
                        cost, round_no, honoured_wall)

    _log(f"    worker produced {commits} commit(s) — running gates on {branch}")
    _status(root, phase="gates", card=card.id, attempt=attempt, branch=branch,
            since=_now())
    status, why = _run_gates(root, tree, out_dir / "gates.txt")
    # A crashed harness is never the card's fault, so it does not spend an
    # attempt and it does not move the card. It also ends the night: every
    # remaining card would meet the same broken gate and be filed as failed,
    # which is precisely what happened on 2026-07-23 before this existed.
    if status == GATE_CRASH:
        drop_worktree(root, tree)
        return Dispatch("blocked", why, cost, round_no, honoured_wall)

    # A gate violation's evidence is the violation lines themselves. `why` already
    # carries the worst four joined by `; ` (`_run_gates`), so the block is that
    # same text split back onto its own lines — readable in a card, and no second
    # read of `gates.txt`, which is the file that does not survive pruning.
    #
    # Also this attempt's own file changes (suite.select, runner-test-selection),
    # computed once here rather than only inside the `ok` branch below: the
    # repo-drift classifier needs the same set for the *violation* case, and it
    # is cheap to compute regardless of which way the gates went.
    changed = set(gitpaths.changed(root, f"{base}...{branch}"))
    evidence = ""
    if status == GATE_VIOLATION:
        evidence = "\n".join(f"    {part}" for part in why.split("; ")[:suite.EXCERPT_TESTS])
        # Repo drift, not the card's own doing (failed-attempt-work-is-deleted-
        # not-resumed): every violating path lies outside this attempt's diff,
        # so the tree the gate is unhappy about is not the tree this worker
        # wrote. Give the attempt back exactly like a crashed harness — the two
        # are the same category, one notch less severe — and stop the night so
        # nothing else is judged against the same drifted state. A violation
        # with no usable path, or one that touches a changed file, is never
        # drift: `_is_repo_drift` returns False and this falls through to the
        # ordinary `failed` below.
        if _is_repo_drift(_gate_violations_json(tree), changed):
            drop_worktree(root, tree)
            return Dispatch("blocked", why, cost, round_no, honoured_wall,
                            repo_drift=True, evidence=evidence)

    ok = status == GATE_PASS
    if ok:
        # `tree`, not `root`, is what the classification resolves against: a
        # changed test file is judged by what it imports, and the version that
        # matters is the one in the worktree pytest is about to run.
        selection = test_selector(changed, tree)
        _log(f"    gates clean — {selection.bucket} test slice "
             f"({selection.reason})")
        _status(root, phase="pytest", card=card.id, attempt=attempt, branch=branch,
                slice=selection.bucket, since=_now())
        try:
            ok, why, evidence = _run_tests(tree, out_dir / "pytest.txt", test_timeout,
                                           out_dir / "junit.xml",
                                           selection.pytest_args(tree / suite.tests_rel(root)))
        except subprocess.TimeoutExpired:
            ok, why = False, f"pytest: timed out after {test_timeout}s"
            evidence = ""

    drop_worktree(root, tree)
    if not ok:
        return Dispatch("failed", why, cost, round_no, honoured_wall, evidence=evidence)
    made = f"{commits} commit(s) on {branch}" + (f", {rescued} artefact(s)" if rescued else "")
    if checked:
        made += f", {card.checker} passed it in {round_no} round(s)"
    return Dispatch("review", str(verdict.get("summary", made))[:300], cost, round_no,
                    honoured_wall,
                    how_to_test=str(verdict.get("how_to_test", "")).strip()[:1000])


# --------------------------------------------------------------------------
# Review stage (automate-review-step) — the LLM half of §11's review step
# --------------------------------------------------------------------------
#
# A new, distinct always-on pipeline stage, NOT the `checker:` producer/retry
# loop. It sits between "gates+tests pass" and the card's final lane: the runner
# builds the reviewer's context, spawns it, and turns the two-way verdict into a
# routed outcome. Every branch here is a file-state lookup on the verdict — the
# only LLM call is the reviewer itself (§12). The routing decisions live in
# `settle`; this function only produces the outcome that drives them.


def merge_branch(root: Path, branch: str, base: str,
                 label: str | None = None) -> tuple[bool, str]:
    """Merge `branch` (a branch name or a bare SHA) into `base`, for a reviewed-ok card.

    The last mechanical step of §11's pipeline: the branch is the deliverable and
    this advances the integration branch to include it. A merge that does not
    apply cleanly is **aborted and reported**, never left half-applied and never
    silently dropped — so `settle` can route it to a human instead of pretending
    it merged. No LLM (§12): every line is a git exit code.

    `root` must be checked out on `base`. In the new topology that is the runner's
    dedicated integration checkout; in the in-place fallback it is Karel's main
    checkout, which the runner is started on. Refusing on a mismatch is cheaper
    than merging into the wrong branch at 3 AM. `label` names the ref in the merge
    commit message when `branch` is a rebased SHA rather than a readable name.
    """
    name = label or branch
    head = current_branch(root)
    if head != base:
        return False, f"the checkout is on `{head}`, not the integration branch `{base}`"
    if _git(root, "rev-parse", "--verify", branch).returncode != 0:
        return False, f"`{name}` no longer exists"
    merged = _git(root, "merge", *gitmerge.STRATEGY_ARGS, "--no-ff", "-m",
                  f"merge {name}: reviewed ok by the runner", branch)
    if merged.returncode == 0:
        return True, "merged"
    aborted = _git(root, "merge", "--abort")  # leave no half-applied merge behind
    if aborted.returncode != 0:
        # Expected, and not itself a problem, when the merge was refused *before* it
        # started — git has no merge to abort then. Says so, because "may need
        # attention" reads as a wedged checkout and sent one 2026-08-01 investigation
        # looking for damage that was never there.
        _log(f"  ! merge --abort of {name} found no merge in progress — git refused the "
             f"merge before starting it; the checkout is untouched")
    return False, gitmerge.failure_detail(merged)


def _unmerged_paths(tree: Path) -> list[str]:
    """Paths git marks as unmerged (a rebase/merge conflict), for the review note."""
    return gitpaths.changed(tree, "--diff-filter=U")


def _delete_remote_branch(root: Path, remote: str, branch: str, *,
                          action: str = "merged") -> None:
    """Delete `branch` on `remote` once its work has landed, so a published card
    branch does not outlive the local one (Karel, 2026-08-09: *"Delete on merge,
    both local and remote."*).

    Must be called **while `branch` still exists locally** — the guard below is
    an ancestry test against that very ref, and after `git branch -D` there is
    nothing left to test against.

    `action` is what the caller just did to the branch, and it appears in the two
    failure logs: `merged` for `rebase_and_merge`, `reaped` for a rescue ref
    dropped by `_drop_rescue_branch`. Only the wording differs — the shared-remote
    reasoning below is the same either way, because the danger is a property of
    the remote, not of why this checkout is finished with the branch.

    **The guard is against the local branch, not against `base`.** Ancestry
    against `base` — the check `publish()`'s dormant-branch case would suggest —
    is exactly backwards here: what lands on `base` is the *rebased copy*, so
    `branch`'s own tip is never an ancestor of `base` after a successful merge
    (the same fact that forces the local delete to use `-D`, not `-d`). A guard
    phrased that way would refuse every delete, and a gate that never fires is
    worse than none. The warrant for deleting is not ancestry at all: it is that
    this checkout just rebased these commits, re-ran gates and the test slice on
    the replayed result, and merged it.

    What the local delete does not have to worry about, and this does, is that a
    remote is **shared**. `remote`'s copy of `branch` may carry commits this
    checkout has never seen — pushed by another machine or a cloud run since the
    last fetch — which were no part of what was just rebased and merged, and
    which a delete would destroy with no copy anywhere. That is the same class
    of loss `publish()` documents (verified against a real bare remote,
    `menu-unlock-indicators`, 2026-07-28). So: fetch the branch, and delete it
    only when the remote's tip **is an ancestor of the local tip** — everything
    the remote had is contained in what just merged. When it is not, refuse and
    log loudly, leaving it for a human, exactly `publish()`'s posture on a
    diverged branch.

    A no-op when `remote` is empty (the schema default: a host that never opted
    into pushing must not start deleting on a remote) or when `remote` is not
    configured in this checkout. A remote that simply has no such branch is the
    ordinary case — most cards merge without ever having been published — not a
    failure, and is not logged as one. Every real failure is logged and
    swallowed: a delete problem is an observability gap, not a reason to end the
    night.
    """
    if not remote:
        return
    if _git(root, "remote", "get-url", remote).returncode != 0:
        return

    fetched = _git(root, "fetch", remote, branch)
    if fetched.returncode != 0:
        # The remote has no such branch: this card was merged without ever being
        # published. Nothing to delete, and nothing to report.
        return
    remote_tip = _git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
    if not remote_tip or not _is_ancestor(root, remote_tip, branch):
        _log(f"  ! {action} {branch} but did NOT delete it on {remote} — the remote "
             f"carries commits this checkout does not have (pushed from elsewhere "
             f"since the last fetch); deleting would destroy them. Reconcile "
             f"`{remote}/{branch}` by hand.")
        return

    deleted = _git(root, "push", remote, "--delete", branch)
    if deleted.returncode != 0:
        detail = (deleted.stderr or deleted.stdout or "").strip().splitlines()
        _log(f"  ! {action} {branch} but could not delete it on {remote} — "
             f"{detail[-1][:150] if detail else 'see git output'}")


def rebase_and_merge(root: Path, card: board.Card, branch: str, base: str,
                     test_timeout: int = 600, remote: str = "") -> tuple[bool, str]:
    """Rebase a reviewed-ok card's branch onto the current integration tip,
    re-verify gates + the affected test slice, then merge (runner-hardening #3).

    The recurring problem this fixes: two cards edited the same night — memory docs
    especially — pass review each against the tip they forked from, then collide
    when the second tries to land. Rebasing `branch` onto today's `base` *before*
    merging replays the card's commits on the current tip, so a textual conflict
    surfaces here (→ a human) and a clean replay is re-checked before it lands.

    Done entirely in a throwaway detached worktree, so the real `ai/<id>` branch is
    never rewritten while this runs, and `base` is untouched until the verified,
    rebased result merges. Reuses `merge_check`'s worktree discipline. No LLM
    (§12): rebase, gates, tests and the merge are all exit codes.

    Once the rebased result actually merges, `branch` is deleted — locally, and
    on `remote` too. It was the deliverable back when Karel merged cards by hand
    (Session G) — nothing reads it once this function has merged automatically,
    and keeping it forever only produces stale refs that `git branch --merged`
    cannot even recognise as merged (the commit that lands is the rebased copy,
    not `branch`'s own tip). A failed merge leaves both copies in place, since a
    human still needs the branch to resolve the conflict.

    `remote` is the host's `publish_remote`, threaded in by `settle` the same way
    `base` is (`default_base`) rather than read from `host_setting` here: this
    stays a function of its arguments — testable against a bare remote without
    planting a host config — and it matches `publish(root, remote, base, …)`, so
    both halves of a card branch's remote lifetime take the remote the same way.
    The `""` default is the schema default and means "no remote deletion", so
    every caller that has not opted into publishing keeps today's behaviour
    exactly. `_delete_remote_branch` carries the guard and the reasoning; note
    only that it must run **before** the local `-D`, because that guard is an
    ancestry test against the local branch's own tip.

    Returns `(merged, detail)`. Any failure — the branch gone, a rebase conflict,
    or gates/tests failing on the replayed result — returns `(False, reason)` with
    `base` unmoved, and `settle` routes the card to review/ with the reason. Never
    a guess (decision #3).
    """
    if _git(root, "rev-parse", "--verify", branch).returncode != 0:
        return False, f"`{branch}` no longer exists"

    tree = worktree_root(root) / f"_rebase-{card.id}"
    if tree.exists() or _worktree_registered(root, tree):
        _git(root, "worktree", "remove", "--force", str(tree))
    _git(root, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = _worktree_add(root, "--detach", str(tree), branch)
    except WorktreePathTooLong as exc:
        return False, f"could not cut a rebase worktree for {branch}: {exc}"
    if made.returncode != 0:
        return False, (f"could not cut a rebase worktree for {branch}: "
                       f"{(made.stderr or made.stdout or '').strip()[:150]}")

    out_dir = run_dir(root, card, card.attempts)
    try:
        rebased = _git(tree, "rebase", *gitmerge.STRATEGY_ARGS, base)
        if rebased.returncode != 0:
            conflicts = _unmerged_paths(tree)
            why = gitmerge.failure_detail(rebased)
            _git(tree, "rebase", "--abort")
            return False, (f"rebasing {branch} onto {base} conflicts in "
                           f"{', '.join(conflicts) or '(no unmerged paths)'} — a human "
                           f"needs to resolve it: {why}")
        rebased_sha = _git(tree, "rev-parse", "HEAD").stdout.strip()

        # Re-verify the *replayed* result, not the branch as reviewed (decision #2).
        status, why = _run_gates(root, tree, out_dir / "rebase-gates.txt")
        if status != GATE_PASS:
            return False, f"after rebasing onto {base}, {why}"
        changed = set(gitpaths.changed(tree, f"{base}...HEAD"))
        selection = suite.select(changed, tree)
        try:
            # The excerpt is dropped here on purpose: this path's reason lands in
            # `## Merge` on a card bound for `review/`, where the question is "will
            # it replay onto the base", not "which assertion broke". `why` already
            # names the first failing test (`suite.check_junit`), which is the right
            # granularity for a merge note.
            ok, why, _ = _run_tests(tree, out_dir / "rebase-pytest.txt", test_timeout,
                                    out_dir / "rebase-junit.xml",
                                    selection.pytest_args(tree / suite.tests_rel(root)))
        except subprocess.TimeoutExpired:
            ok, why = False, f"pytest: timed out after {test_timeout}s"
        if not ok:
            return False, f"after rebasing onto {base}, {why}"

        # Merge the verified rebased commits. The throwaway worktree still holds
        # HEAD at `rebased_sha`, which keeps it referenced across the merge; the
        # `finally` drops the worktree only after.
        merged, why = merge_branch(root, rebased_sha, base, label=branch)
        if merged:
            # The remote copy goes first, while `branch` still resolves: its
            # guard is an ancestry test against this ref, which `-D` would take
            # away. A refusal or a failure there is logged and swallowed, so the
            # local delete below happens either way.
            _delete_remote_branch(root, remote, branch)
            # The dispatch worktree that had `branch` checked out is already gone
            # by this point (dropped at the end of `dispatch`, well before review
            # and settle run), so the ref is never checked out anywhere here.
            deleted = _git(root, "branch", "-D", branch)
            if deleted.returncode != 0:
                _log(f"  ! merged {branch} but could not delete it — "
                     f"{(deleted.stderr or deleted.stdout or '').strip()[:150]}")
        return merged, why
    finally:
        _git(root, "worktree", "remove", "--force", str(tree))
        _git(root, "worktree", "prune")


def branch_has_commits(root: Path, base: str, branch: str) -> bool:
    """Does `branch` carry anything `base` does not — i.e. is there a diff to review?

    The negative answer covers three states that all mean "there is nothing here
    for a diff reviewer": an artefact-only card (`art`, whose deliverable is a
    file a human looks at rather than a commit), a branch that was never cut, and
    a branch already merged. All three are the same fact to every caller, which is
    why they share one predicate rather than each testing `rev-list` for itself —
    `drain` asks it about a card at rest in `review/` for exactly the reason
    `review_stage` asks it about a card passing through.
    """
    count = _git(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip()
    return count not in ("", "0")


def review_stage(root: Path, card: board.Card, result: Dispatch, base: str,
                 card_budget: float, timeout: int) -> Dispatch:
    """Run the diff reviewer on a card whose gates+tests just passed, and route.

    Called for a `review` outcome only. Returns a new `Dispatch`:

    * `needs_decision` — the reviewer flagged a choice for Karel; settle files it
      to needs-decision/ with the reviewer's question.
    * `needs_fix` — the reviewer found a concrete, verifiable defect with one
      correct answer; settle sends the card back to tasks/ for another attempt
      carrying the finding, bounded by the same attempt_limit as an ordinary
      `failed` retry.
    * `reviewed` — the reviewer said `ok`; settle merges and lands it in testing/.
    * `review` (unchanged from the input) — the graceful fallback. An artefact-only
      card (art: no commit to review), no CLI, a wall, a timeout or an unreadable
      verdict all land the card in review/ for a human, exactly as before this
      stage existed. Degrading to "a human looks" is always safe; guessing a
      routing is not (§12, §13).

    The returned outcome's `cost_usd` carries the dispatch cost forward plus the
    reviewer's own, so the run loop accounts for it with one `spent +=`.
    """
    branch = branches.work_branch(card.id, card.fields.get("branch", ""))

    # The window is already known to be closed: an earlier stage walled and had
    # its verdict honoured (`wall-on-review-wrapup-discards-a-verdict`), so the
    # card reached here on its merits but the plan has run out. Spawning the
    # reviewer now would call into a window already proven shut, burn a
    # subprocess and degrade to review/ anyway — so degrade straight away, with
    # the wall still on the returned Dispatch so the night stops or sleeps.
    if result.wall is not None:
        _log(f"    a usage limit already closed this window — not spawning "
             f"{REVIEWER_AGENT} for {card.id}; leaving it in review/ for a manual look")
        return result

    # Nothing to review as a diff: an artefact-only card (art) has no commit on
    # its branch, and its human gate is Karel at review/, unchanged (this card
    # "does not change how art review works"). A dead branch lands here too.
    if not branch_has_commits(root, base, branch):
        return result

    try:
        model = tiers.resolve(root, "lead")
    except tiers.TierError as exc:
        _log(f"  review stage skipped for {card.id} — {exc}; leaving it in review/")
        return result

    out_dir = run_dir(root, card, card.attempts)
    _status(root, phase="review", card=card.id, branch=branch, model=model, since=_now())
    _log(f"    reviewing {card.id} → {REVIEWER_AGENT} @ {model}")
    criteria, intent = card_review_context(card)
    verdict, cost, wall = review_branch(root, card.id, out_dir, model, base, branch,
                                        card_budget, timeout * 3,
                                        criteria=criteria, intent=intent)
    total = result.cost_usd + cost

    # The artefact before the process's exit. A reviewer's *entire* output is its
    # verdict file, so one that says `ok` or `needs_decision` means the review
    # genuinely finished and the wall landed on the wrap-up call after it — the
    # measured shape of `wall-on-review-wrapup-discards-a-verdict`. It routes on
    # that verdict; an incomplete or missing one still degrades to review/ exactly
    # as before. Either way the wall rides home on the returned Dispatch, because
    # the plan running out is a fact about the *night* whatever this card did.
    if wall is not None and not verdict_survives_a_wall(REVIEWER_STAGE, verdict):
        # The plan ran out mid-review with nothing usable written. Gates+tests
        # already passed, so this is not the card's fault — land it in review/ for
        # a manual look rather than spending the night's machinery re-deciding a
        # card that is otherwise done.
        _log(f"    review hit a usage limit — leaving {card.id} in review/ for a manual look")
        return Dispatch("review", result.detail, total, result.rounds, wall,
                        how_to_test=result.how_to_test)
    if wall is not None:
        _log(f"    {REVIEWER_AGENT} walled on its wrap-up after writing a complete verdict "
             f"— honouring it; the night's window is still closed")

    called = str(verdict.get("verdict", "")).lower()
    _log(f"    {REVIEWER_AGENT}: {called or '(no verdict)'} — "
         f"{str(verdict.get('notes', ''))[:80]}")
    if called == "needs_fix":
        finding = str(verdict.get("finding", "")).strip()
        return Dispatch("needs_fix", finding or
                        "The reviewer flagged a fixable defect but recorded no finding — "
                        "treat that as itself needing a look; the diff and the review are "
                        f"in `.ai/runs/{card.id}/attempt-{card.attempts}/`.",
                        total, result.rounds, wall)
    if called == "needs_decision":
        question = str(verdict.get("question", "")).strip()
        return Dispatch("needs_decision", question or
                        "The reviewer flagged this for your decision but stated no question "
                        "— treat that as itself needing a look; the diff and the review are "
                        f"in `.ai/runs/{card.id}/attempt-{card.attempts}/`.",
                        total, result.rounds, wall)
    if called == "ok":
        return Dispatch("reviewed", str(verdict.get("notes", "reviewed ok"))[:300],
                        total, result.rounds, wall, how_to_test=result.how_to_test)
    # No usable verdict — degrade to the old behaviour, a human review at review/.
    _log(f"    no usable review verdict for {card.id} — leaving it in review/")
    return Dispatch("review", result.detail, total, result.rounds, wall,
                    how_to_test=result.how_to_test)


# --------------------------------------------------------------------------
# When a stage of the pipeline raises instead of returning
# --------------------------------------------------------------------------

# How much of a traceback is quoted into the card. Enough to name the frame that
# raised and the call path into it; not so much that a deep recursion buries the
# rest of the card. Head and tail are both kept because the two ends carry
# different facts — the first lines say the exception happened and where the
# runner entered, the last say what actually raised.
_CRASH_TB_HEAD = 6
_CRASH_TB_TAIL = 24


def _crash_traceback(exc: BaseException) -> list[str]:
    """`exc`'s traceback as bounded lines, elided in the middle if it is long."""
    lines: list[str] = []
    for chunk in traceback.format_exception(type(exc), exc, exc.__traceback__):
        lines += chunk.rstrip("\n").split("\n")
    if len(lines) <= _CRASH_TB_HEAD + _CRASH_TB_TAIL:
        return lines
    elided = len(lines) - _CRASH_TB_HEAD - _CRASH_TB_TAIL
    return (lines[:_CRASH_TB_HEAD]
            + [f"... {elided} line(s) elided ..."]
            + lines[-_CRASH_TB_TAIL:])


def crashed_dispatch(root: Path, card: board.Card, exc: BaseException) -> Dispatch:
    """One card's pipeline raised. Turn that into *that card's* failure.

    An exception out of `dispatch` used to propagate through `run` to `main` and
    exit 1, abandoning every card still in the queue: on 2026-08-06 a single
    `FileNotFoundError: [WinError 206]` from one oversized card's `Popen` ended
    the night before it had started, and left a worktree and branch behind for
    hand-cleaning. A card the runner cannot dispatch is a failure of that card,
    exactly like a red gate — so this returns the same `Dispatch("failed", …)`
    the gate path returns and lets the existing settle/record/publish path carry
    it. No new outcome value, no new lane; `CONSECUTIVE_FAILURE_STOP` is already
    the net that promotes a *systematic* crash to the night's problem.

    The attempt stays spent. `dispatch` commits `attempts` before the worker
    starts precisely so a machine that dies mid-dispatch cannot retry forever, so
    a crash past that point has spent it whatever this function decides.

    Two things make the failure diagnosable from the card alone, which is the ask
    the raw `WinError 206` failed: `detail` reads as a cause (`dispatch crashed —
    FileNotFoundError: …`) rather than as noise, and `evidence` names the card and
    quotes the traceback, so `## Error` answers *which card* and *roughly why*
    without opening the gitignored `.ai/runs/`.

    Cleanup is what `dispatch`'s own exit paths would have done and did not get
    to: `commit_wip` to bank whatever the worker managed, then `drop_worktree` —
    the same order, and for the same reason, as the `worker exited {code}` path.
    It is guarded in turn: a cleanup that raises must not re-crash the loop this
    function exists to protect.
    """
    detail = f"dispatch crashed — {type(exc).__name__}: {exc}".strip()
    quoted = [f"the runner crashed while dispatching card `{card.id}`; "
              f"no verdict was reached for it", ""] + _crash_traceback(exc)
    # Indented four spaces like every other excerpt, so a `#` anywhere in the
    # traceback cannot match `doc_scan._HEADING`.
    evidence = "\n".join(f"    {line}".rstrip() for line in quoted)

    try:
        tree = worktree_root(root) / card.id
        if tree.exists():
            commit_wip(root, tree, card.id)
            drop_worktree(root, tree)      # also clears the handover
        else:
            clear_handover(root, card.id)
    except Exception as cleanup_exc:       # noqa: BLE001 — see the docstring
        _log(f"  ! could not clean up after {card.id}'s crash: "
             f"{type(cleanup_exc).__name__}: {cleanup_exc}. A worktree may be left "
             f"under {worktree_root(root)} for hand-removal.")

    return Dispatch("failed", detail, evidence=evidence)


# --------------------------------------------------------------------------
# Settle — what the outcome does to the board
# --------------------------------------------------------------------------

# The exemption that lets machine-written evidence live in a card.
#
# **Belt and braces since 2026-08-01, not load-bearing.** `Board/` is no longer a
# `doc_scan.DOC_ROOTS` entry at all, so no lane is read by `doc_reference_liveness`
# and a pytest excerpt cannot trip it wherever the card sits.
#
# It was load-bearing, and the reason is worth keeping because it is what finally
# argued the scope change: every line of a retrying card's `## Error` used to be read
# by that gate, which asserts each path and backticked symbol still exists. A pytest
# excerpt is full of both, and the ones most likely to dangle are the interesting
# ones — a test file the failed attempt created on a branch that was then discarded.
# Unexempted, one card's failure became a repo-wide violation and the next card in
# the night was filed failed for someone else's bug: the 2026-07-23 shape
# `GATE_CRASH` exists to prevent. Needing this marker at all was the evidence that
# the gate's scope was wrong rather than the cards.
#
# Kept rather than deleted: it costs one comment line in a failed card, it states a
# true thing about the block ("quoted evidence is not a claim"), and it keeps the
# card honest if `Board/` is ever put back in scope. A marker covers from its own
# line to the next `##` heading (`doc_scan.exempt_lines`), which is precisely the
# rest of this section, and the reason is written out because
# `test_every_exemption_carries_a_written_reason` requires one.
_EVIDENCE_EXEMPTION = (
    "<!-- stale-ok: pytest and the gates wrote the block below while this attempt "
    "was failing. It is quoted evidence about that attempt, not a claim about the "
    "current tree, so liveness must not judge the paths and symbols in it. -->"
)


def _error_section(card_id: str, attempt: int, result: Dispatch, *,
                   retiring: bool) -> str:
    """The `## Error` body: what failed, the evidence, and where the rest of it is.

    Written to be readable from a machine that did not run the card, which is the
    thing the previous version could not do. It said `Full output:
    .ai/runs/<id>/attempt-N/` and nothing else, and that pointer is empty on every
    host but the one that ran the night (`.ai/runs/` is gitignored) — and on a
    terminal failure it is empty there too, because `prune_run_dir` deletes the
    directory two lines after this text is written. Karel went looking for a
    failure reason on 2026-07-31 and there was nowhere it could have been.

    `retiring` is whether this attempt is the card's last, and it changes what the
    last paragraph is allowed to promise: a directory that is about to be pruned is
    not offered as somewhere to look.
    """
    head = f"Attempt {attempt} failed — {_quote_safe(result.detail)}"
    parts = [head]
    if result.evidence:
        parts += [_EVIDENCE_EXEMPTION, result.evidence]
    # Naming the host is the other half of the fix. The pointer is only meaningful
    # on one machine, so the text says which — `.ai/hosts.json` has two, and "the
    # logs are not here" reads as "the logs are gone" without it.
    where = f"`.ai/runs/{card_id}/attempt-{attempt}/` on host `{socket.gethostname()}`"
    if retiring:
        parts.append(
            f"The full log was {where} and was deleted when this card was retired "
            f"(`prune_run_dir`). The block above is what survives — `.ai/runs/` is "
            f"gitignored, so it never left that machine either. Re-run the card to "
            f"get a fresh attempt directory.")
    else:
        parts.append(
            f"Full output: {where} — gitignored, so it is only on that machine, and "
            f"it is deleted if this card is retired after {MAX_ATTEMPTS} attempts. "
            f"This attempt's commits are not lost either way: they stay on `ai/{card_id}` "
            f"and, once this card is dispatched again, are preserved as a rescue branch "
            f"(`ai/{card_id}@failed-N` — `prune_rescue_branches`) rather than deleted at "
            f"the next cold start, until the card leaves `tasks/` for good.")
    return "\n\n".join(parts)


def _review_finding_section(card_id: str, finding: str) -> str:
    """The `## Review Finding` body written on a `needs_fix` verdict.

    Deliberately not `## Error` — the gates and the full test suite had already
    passed; nothing crashed. And not `## Question` — nothing here needs the
    maintainer's judgment, only a fix. The next attempt is the reader: `finding`
    is the reviewer's own account of the defect and its correct answer, precise
    enough (by the reviewer's prompt) to apply without re-deriving it.
    """
    return "\n\n".join([
        "The reviewer found a fixable defect — not a judgment call — in a diff whose "
        "gates and tests had already passed:",
        finding,
        f"This attempt's commits stay on `ai/{card_id}` and are preserved as a rescue "
        f"branch once this card is dispatched again. Apply this fix directly; it does "
        f"not need re-deriving.",
    ])


def _card_text_on_branch(root: Path, branch: str, relpath: str) -> str | None:
    """The card file as the worker last committed it on its own branch, or
    `None` if it cannot be read (branch gone, path renamed, not a git
    failure the caller should surface — settle() falls back to the
    pre-dispatch text it already has).

    Exists because the worktree is already dropped by the time `settle()`
    runs (drop_worktree happens before this is called), so the worker's
    committed content is reachable only through git, not the filesystem.
    """
    done = _git(root, "show", f"{branch}:{relpath}")
    return done.stdout if done.returncode == 0 else None


def settle(root: Path, card_id: str, result: Dispatch) -> str:
    """Apply one dispatch's outcome, then reap rescue branches if the card just
    left `tasks/` for good.

    A thin wrapper around `_settle_impl` rather than a call inside it: that
    function already has a dozen outcome branches, several returning early
    straight after their own `board.move(...)`, so a call sprinkled into each
    one is exactly the shape that gets a new branch added later and misses it
    — which is how `rescue-branches-only-swept-on-failure` (2026-08-13)
    happened in the first place (only the two `failed/`-retirement branches
    called `prune_rescue_branches`; the far more common success-into-`testing/`
    path never did, and neither did park-into-`needs-decision/` or
    bounce-into-`review/`). Checking the card's lane once, after the real
    work, covers every outcome uniformly and cannot be missed by a future one.
    A card still in `tasks/` (retried, or vanished) is left alone — its rescue
    branches are still what a future retry recovers from."""
    message = _settle_impl(root, card_id, result)
    card = board.find(root, card_id)
    if card is not None and card.lane != "tasks":
        prune_rescue_branches(root, card_id)
    return message


def _settle_impl(root: Path, card_id: str, result: Dispatch) -> str:
    """Apply one dispatch's outcome. Rescans first: the card may have moved."""
    card = board.find(root, card_id)
    if card is None:
        return f"{card_id}: vanished from the board — nothing settled"

    # Before the outcome branches, so the numbers land on the card whatever
    # happened. A *failed* attempt's telemetry is the most useful kind: it is
    # what distinguishes "ran 40 minutes and hit something hard" from "died in
    # four turns", and that is exactly the attempt nobody has other evidence for.
    telemetry = read_telemetry(run_dir(root, card, card.attempts))
    if telemetry:
        card.write_section("Telemetry", telemetry_markdown(telemetry, card.attempts))

    if result.outcome == "limited" and result.stuck:
        # The no-progress breaker (runner-worker-handover Decision 2). A warm
        # resume that leaves the working tree byte-identical for NO_PROGRESS_STOP
        # windows is not the plan running out — it is a per-card failure a wall
        # phrase matched by mistake, or a resume that never persists progress.
        # Giving the attempt back again would loop it at the head of the queue
        # forever, so it is filed to failed/ instead. Its last state is banked on
        # the branch first, for the post-mortem.
        tree = worktree_root(root) / card_id
        if tree.exists():
            commit_wip(root, tree, card_id)
            drop_worktree(root, tree)          # also clears the handover
        else:
            clear_handover(root, card_id)
        card.write({"started": None, "finished": _now()})
        card.write_section(
            "Error",
            f"Attempt {card.attempts} was interrupted by a usage limit "
            f"{NO_PROGRESS_STOP} times with no change to the working tree between them, so "
            f"it was filed as stuck rather than resumed forever "
            f"(runner-worker-handover Decision 2). This usually means a per-card failure a "
            f"wall phrase matched by mistake, or a resume that never persists progress. "
            f"The log was `.ai/runs/{card_id}/` on host `{socket.gethostname()}` and was "
            f"deleted when this card was retired (`prune_run_dir`); it was gitignored, so "
            f"it never left that machine either.")
        board.move(root, card, "failed")
        prune_run_dir(root, card_id)
        prune_rescue_branches(root, card_id)
        return (f"{card_id}: → failed/ (no working-tree progress across "
                f"{NO_PROGRESS_STOP} resumes)")

    if result.outcome == "limited" and result.kept and not result.progressed:
        # Under the breaker but not moving: spend the attempt (do NOT rewind) and
        # keep the warm state for one more resume. Spending is the give-back's
        # backstop — even if the breaker never trips, attempts drain toward the
        # limit rather than the card being retried for free forever.
        card.write({"started": None, "finished": _now()})
        board.commit_board(
            root, f"board: {card_id} resumed with no progress — attempt {card.attempts} spent")
        return (f"{card_id}: resumed but the working tree did not move — attempt "
                f"{card.attempts} spent, worktree kept for one more resume")

    if result.outcome in ("limited", "blocked", "interrupted"):
        # The outcomes that give the attempt back, because none of them is a
        # fact about the card. `attempts` is committed before the worker starts
        # so a crash cannot retry forever, and that is right for every other
        # exit — but charging a card for the plan running out, this machine's
        # gate harness being broken, or a socket dropping mid-response would
        # mean one bad night quietly pushed every card it touched closer to
        # `failed/`.
        back = card.attempts - 1
        rewind: dict[str, str | None] = {
            "attempts": str(back) if back > 0 else None,
            "started": None, "finished": None,
        }
        # A branch is dropped only in the empty first-call case — a wall at the
        # very first API call, with nothing done and no worktree kept, where the
        # branch records nothing. When the worktree is kept for a warm resume, its
        # branch is preserved state (commits, and any `wip:` commit), and
        # `blocked`/`interrupted` ran and committed — so neither drops it.
        if back <= 0 and result.outcome == "limited" and not result.kept:
            rewind["branch"] = None
        card.write(rewind)
        # Three different facts can all give the attempt back, and the morning
        # log must say which: a crashed harness is this machine's problem, repo
        # drift is a gate a *different* card (or a commit already on `base`)
        # made stale, an interruption is a dropped connection that never
        # reached verification — all three stop nothing being judged unfairly,
        # but only naming the right one saves a hunt
        # (failed-attempt-work-is-deleted-not-resumed).
        why = ("usage limit" if result.outcome == "limited"
              else "worker interrupted (api_error)" if result.outcome == "interrupted"
              else "repo drift" if result.repo_drift
              else "gate harness crashed")
        kept = " — worktree kept for warm resume" if result.kept else ""
        # Repo drift specifically: the branch this dispatch judged is real work
        # that a repo-wide fact, not this diff, made unjudgeable. It stays on
        # `ai/<card_id>` and is preserved as a rescue branch at the next cold
        # start, same as an ordinary `failed` attempt's commits.
        rescued = (f" — its commits stay on `ai/{card_id}` and will be preserved as a "
                  f"rescue branch (`ai/{card_id}@failed-N`) once this card is "
                  f"dispatched again" if result.repo_drift else "")
        board.commit_board(root, f"board: {card_id} not attempted — {why}")
        return f"{card_id}: not attempted, attempt given back — {result.detail}{kept}{rescued}"

    card.write({"started": None, "finished": _now()})

    if result.outcome == "review":
        # The worker's own one/two-sentence account of what it did, persisted onto
        # the card rather than only living in this return string — so a card that
        # reaches testing/ carries a brief "what happened" a human can read without
        # opening `.ai/runs/` or the verbose `## Thread` prose (menu-summary-on-card).
        card.write_section("Summary", result.detail)
        board.move(root, card, "review")
        return f"{card_id}: → review/ ({result.detail})"

    if result.outcome == "parked":
        # The worker prompt tells it to write `## Question` directly onto the
        # card (§13). `result.detail` is only the verdict's `summary` field —
        # a different, shorter field meant for `## Summary` — truncated to
        # 300 chars. Only fall back to it when the worker genuinely left no
        # question; otherwise this clobbers a real, carefully-written
        # question with a mid-sentence-truncated attempt summary.
        #
        # What "genuinely left no question" means is read from the WORKER'S
        # branch, never from `card.text` alone (the pre-dispatch snapshot this
        # process already had in memory) — a presence check on `card.text`
        # cannot distinguish "never answered" from "answered already, and the
        # worker wrote a NEW question on top of that history", so a card whose
        # `## Question` already had content (fully answered, sent to tasks/,
        # then redispatched and parked again) silently discarded everything
        # the worker actually wrote (parked-settle-trusts-stale-question-over-
        # worker-branch, second occurrence 2026-08-12, cross-language-text-
        # overflow-guard attempt 5 — found by hand again because the symptom
        # is invisible from the card alone: `attempts`/`finished` update, but
        # `## Question` just doesn't move).
        branch = branches.work_branch(card_id, card.fields.get("branch", ""))
        relpath = str(card.path.relative_to(root)).replace("\\", "/")
        branch_text = _card_text_on_branch(root, branch, relpath)
        branch_question = board.section(branch_text, "Question") if branch_text else ""
        pre_dispatch_question = board.section(card.text, "Question")
        if branch_question and branch_question != pre_dispatch_question:
            # Prepend rather than replace — `write_section` overwrites the
            # whole section, and the pre-dispatch content is real history
            # (prior pickers, prior parks), not a stale placeholder to drop.
            combined = (branch_question if not pre_dispatch_question
                       else f"{branch_question}\n\n---\n\n{pre_dispatch_question}")
            card.write_section("Question", combined)
        elif not pre_dispatch_question:
            card.write_section("Question", result.detail or
                               "The worker parked this card but recorded no question — "
                               "that is itself a defect; see `.ai/runs/` for the attempt.")
        board.move(root, card, "needs-decision")
        return f"{card_id}: → needs-decision/ (parked)"

    if result.outcome == "needs_fix":
        # A concrete, verifiable defect — not a decision. Same principle drain.py
        # already states for a card that merely could not be reviewed ("'review
        # this diff' is not a decision — it is work"), one step further: applying
        # a fix with one correct answer is not a decision either. It goes back to
        # tasks/ for another attempt, bounded by the same attempt_limit as an
        # ordinary `failed` retry — never straight to needs-decision/, which
        # would spend Karel's judgment on something the next attempt can just do.
        finding = result.detail or "The reviewer flagged a fixable defect but recorded none."
        if card.attempts >= attempt_limit(card):
            # The same class of "fixable" defect survived every attempt this
            # card gets — that is no longer mechanical, whatever the reviewer
            # called it. Escalate rather than retry forever.
            card.write_section(
                "Question",
                f"The reviewer found a fixable-looking defect across all {card.attempts} "
                f"attempts this card gets, and it kept recurring rather than getting fixed "
                f"— something about it is not as mechanical as it looked, or a later attempt "
                f"reintroduced it. Most recent finding:\n\n{finding}")
            board.move(root, card, "needs-decision")
            return (f"{card_id}: → needs-decision/ (a reviewer-flagged fix recurred across "
                    f"{card.attempts} attempts)")
        card.write_section("Review Finding", _review_finding_section(card_id, finding))
        if card.lane == "tasks":
            board.commit_board(
                root, f"board: {card_id} attempt {card.attempts} needs a fix, will retry")
        else:
            board.move(root, card, "tasks")
        return f"{card_id}: attempt {card.attempts} needs a fix, will retry ({finding[:80]})"

    # The three outcomes the review stage produces (automate-review-step,
    # reviewer-needs-fix-verdict). Where a `reviewed` one lands is the card's own
    # declaration, `verify:` — `play` for a card with a surface Karel can exercise,
    # `review` for one with none, where the reviewer's `ok` *is* the acceptance
    # (unplayable-cards-still-land-in-testing). Until 2026-08-06 every finished card
    # went to testing/ regardless, so eleven of the sixteen cards waiting there were
    # waiting on a verification he had no way to perform, and the five he should
    # actually play were buried among them. `needs_decision` still diverts to
    # needs-decision/ from either value; `needs_fix` is handled above it, back to
    # tasks/ rather than needs-decision/, because it never needed Karel at all.
    if result.outcome == "needs_decision":
        card.write_section("Question", result.detail or
                           "The reviewer flagged this for your decision but recorded no "
                           "question — see `.ai/runs/` for the diff and the review.")
        board.move(root, card, "needs-decision")
        return f"{card_id}: → needs-decision/ (reviewer flagged a decision)"

    if result.outcome == "reviewed":
        branch = branches.work_branch(card_id, card.fields.get("branch", ""))
        integration = default_base(root)
        # The host's `publish_remote`, resolved here rather than inside
        # `rebase_and_merge`, for the same reason `integration` is: the merge
        # step stays a function of its arguments. Empty on a host that never
        # opted into pushing, and then nothing is deleted on any remote.
        remote = str(host_setting(root, "publish_remote", "")).strip()
        merged, why = rebase_and_merge(root, card, branch, integration, remote=remote)
        if merged:
            # Written before the move, so the card carries its scenario into the
            # lane rather than arriving there bare — the same ordering, and for the
            # same reason, as `## Summary` on the `review` branch above.
            if card.verify == "play":
                card.write_section("How to test", result.how_to_test or
                                   "The worker recorded no scenario — that is itself a "
                                   "defect on a `verify: play` card; the diff is on "
                                   f"`{branch}`.")
            lane = board.finished_lane(card)
            board.move(root, card, lane)
            return (f"{card_id}: → {lane}/ (reviewed ok, rebased {branch} onto "
                    f"{integration} and merged)")
        # Reviewed ok but the branch will not rebase-and-merge — gates were green
        # when it was reviewed, but it conflicts with what has landed on the
        # integration branch since (a sibling card, a same-night memory edit), or
        # the replayed result no longer passes. That is exactly what review/ means
        # (03_board.md §1: "gates green, awaiting Claude review, or waiting on a
        # sibling card"), so it goes there for a human to resolve — never silently
        # into testing/ as if it had merged, and never a guessed resolution (§12).
        card.write_section(
            "Merge",
            f"Reviewed `ok`, but `{branch}` could not be rebased onto "
            f"`{integration}` and merged: {why}. A human needs to resolve it; "
            f"then it can go to testing/. The reviewed diff is in "
            f"`.ai/runs/{card_id}/attempt-{card.attempts}/`.")
        board.move(root, card, "review")
        return f"{card_id}: → review/ (reviewed ok but {branch} will not rebase-merge — {why})"

    retiring = card.attempts >= attempt_limit(card)
    card.write_section("Error", _error_section(card_id, card.attempts, result,
                                              retiring=retiring))
    if retiring:
        board.move(root, card, "failed")
        prune_run_dir(root, card_id)
        prune_rescue_branches(root, card_id)
        return f"{card_id}: → failed/ after {card.attempts} attempts ({result.detail})"
    board.commit_board(root, f"board: {card_id} attempt {card.attempts} failed")
    return f"{card_id}: attempt {card.attempts} failed, will retry ({result.detail})"


# --------------------------------------------------------------------------
# Publish — the cloud topology's missing half
# --------------------------------------------------------------------------
#
# Everything above commits locally: the board, merged cards on `base`, and each
# card's `ai/<id>` branch. That was always enough on Karel's laptop, where the
# checkout *is* his repo — the branch is already somewhere he can reach it. It
# is not enough in the cloud topology (`night.py`'s "known host" test): a
# claude.ai routine runs in an ephemeral clone, and a card parked in
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
    out = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/ai/")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _is_ancestor(root: Path, maybe_ancestor: str, descendant: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", maybe_ancestor, descendant).returncode == 0


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
    if _git(root, "remote", "get-url", remote).returncode != 0:
        _log(f"  publish: remote `{remote}` is not configured in this checkout — skipping")
        return

    pushed_base = _git(root, "push", remote, base)
    if pushed_base.returncode != 0:
        detail = (pushed_base.stderr or pushed_base.stdout or "").strip().splitlines()
        _log(f"  ! publish: `{base}` was not pushed to {remote} (not forced) — "
             f"{detail[-1][:150] if detail else 'see git output'}")

    for branch in _card_branches(root):
        if branch != trusted_branch:
            fetched = _git(root, "fetch", remote, branch)
            if fetched.returncode == 0:
                remote_tip = _git(root, "rev-parse", "FETCH_HEAD").stdout.strip()
                if not _is_ancestor(root, remote_tip, branch):
                    _log(f"  ! publish: `{branch}` NOT pushed — {remote} already carries "
                         f"commits this checkout does not have (diverged, not a stale "
                         f"retry); force-pushing would discard them. Reconcile by hand "
                         f"before publishing this branch.")
                    continue
            # A fetch that fails because the remote has no such branch yet is the
            # ordinary new-card-branch case, not an error — nothing to be an
            # ancestor of, so the push below is a plain create.
        pushed = _git(root, "push", "--force-with-lease", remote, branch)
        if pushed.returncode != 0:
            detail = (pushed.stderr or pushed.stdout or "").strip().splitlines()
            _log(f"  ! publish: `{branch}` was not pushed to {remote} — "
                 f"{detail[-1][:150] if detail else 'see git output'}")


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

def _sleep_until(when: dt.datetime) -> bool:
    """Wait for the window to reopen. False if the kill switch appeared while waiting.

    In slices, not one long sleep: the kill switch is the one promise the runner
    makes to a phone at 3 AM, and a single `sleep(5h)` would make the runner
    unstoppable during exactly the hours it is doing nothing anyway. Checks both
    roots (`_stop_requested`), so the switch trips wherever the file lands.
    """
    while (remaining := (when - dt.datetime.now()).total_seconds()) > 0:
        if _stop_requested():
            return False
        time.sleep(min(60.0, max(1.0, remaining)))
    return True


def _deadline(until: str | None, max_minutes: int | None) -> dt.datetime | None:
    if max_minutes:
        return dt.datetime.now() + dt.timedelta(minutes=max_minutes)
    if not until:
        return None
    hour, _, minute = until.partition(":")
    when = dt.datetime.now().replace(hour=int(hour), minute=int(minute or 0),
                                     second=0, microsecond=0)
    return when if when > dt.datetime.now() else when + dt.timedelta(days=1)


def _invocation_label(args: argparse.Namespace) -> str:
    """How this run was asked for, in the few words the digest heads a run block
    with — `night, up to 8 cards, staleness sweep` or `card ice-damage`.

    Reconstructed from the parsed args rather than `sys.argv`, so it says the
    same thing whether the run came from `night.py`'s defaults, Task Scheduler or
    Karel's own command line. It exists because two runs in one night are not
    interchangeable: on 2026-07-30 an aborted 8-card night and a deliberate
    one-card rerun both landed between two digests, and a report that cannot
    name which was which cannot explain either.
    """
    parts: list[str] = [f"card {args.card}"] if args.card else []
    if not args.card:
        parts.append(f"up to {args.max_cards} card(s)" if args.max_cards else "no card cap")
        if args.until:
            parts.append(f"until {args.until}")
        elif args.max_minutes:
            parts.append(f"{args.max_minutes} min")
        if args.stale:
            parts.append("staleness sweep")
        if args.budget:
            parts.append(f"${args.budget:.2f} budget")
    if args.append_digest:
        # Visible on the run block itself, not only in the commit log — Karel
        # reads this label to know why a run's report is stacked on top of an
        # earlier one instead of standing alone.
        parts.append("append mode")
    return ", ".join(parts)


def print_status(root: Path) -> int:
    """`--status`: where the runner is right now, read from disk alone.

    Deliberately does not take the lock, run preflight or touch git — it is the
    question you ask *while* a night is in flight, and a status command that
    could interfere with the thing it reports on would be worse than no status
    command. Safe to run from a second terminal at 3 AM.
    """
    path = root / STATUS_FILE
    if not path.is_file():
        print("no status yet — no run has started on this machine "
              f"({path.as_posix()} is absent)")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        print(f"{path.as_posix()} exists but is unreadable")
        return 1

    pid = int(data.get("pid") or 0)
    alive = _pid_alive(pid)
    phase = str(data.get("phase") or "?")
    # A stale file is the normal state between runs, and saying so plainly is the
    # point: "phase: worker" from a process that died an hour ago is exactly the
    # misreading this command exists to prevent.
    state = "RUNNING" if alive else "NOT RUNNING — stale, from a run that ended or died"
    print(f"runner pid {pid} — {state}")

    where = str(data.get("card") or "")
    if where:
        attempt = data.get("attempt")
        rnd, of = data.get("round"), data.get("of_rounds")
        detail = f"{where}, attempt {attempt}" if attempt else where
        if rnd and of and int(of) > 1:
            detail += f", round {rnd}/{of}"
        print(f"phase   : {phase} ({detail})")
    else:
        print(f"phase   : {phase}")

    for key, label in (("model", "model"), ("worker", "worker"),
                       ("checker", "checker"), ("branch", "branch")):
        if data.get(key):
            print(f"{label:<8}: {data[key]}")

    since = str(data.get("since") or data.get("updated") or "")
    try:
        mins = (dt.datetime.now() - dt.datetime.fromisoformat(since)).total_seconds() / 60
        print(f"elapsed : {mins:.1f} min in this phase (since {since})")
    except ValueError:
        print(f"updated : {data.get('updated', '?')}")

    log = root / RUNS / f"{dt.date.today().isoformat()}.log"
    if log.is_file():
        print(f"log     : {log.as_posix()}")
    return 0


def run(root: Path, args: argparse.Namespace) -> int:
    global _CTRL_ROOT, _WORK_ROOT
    ctrl = root                    # the launch checkout — the control plane's home
    base = args.base
    # Opened before preflight so a refusal is recorded too — "it refused and I
    # did not see why" is the same invisible-failure shape as a silent night.
    if not args.dry_run:
        _open_run_log(ctrl)
    check = preflight(ctrl, base, args.dry_run)
    if not check.ok:
        for reason in check.reasons:
            _log(f"refusing to run — {reason}")
        return EXIT_STOPPED if (ctrl / STOP_FILE).is_file() else 1

    if not args.dry_run and not acquire_lock(ctrl):
        return 1

    try:
        # Where the board and git work happen. The control plane (STOP, status,
        # lock, log) always stays with the launch checkout `ctrl`, where Karel
        # interacts with it; only the board/git work moves (runner-hardening #3):
        #   * launch checkout OFF the integration branch — the new topology. Cut a
        #     dedicated `base` checkout so the runner never touches his working copy
        #     and the two never fight over the branch.
        #   * launch checkout ON `base` — git forbids a second checkout of it, so
        #     work in-place as before, and nudge him to switch so he can keep coding.
        #   * dry-run — read an existing dedicated checkout if there is one, else the
        #     launch checkout; write nothing, cut nothing.
        on_base = current_branch(ctrl) == base
        if args.dry_run:
            existing = integration_checkout_path(ctrl)
            work = existing if _worktree_registered(ctrl, existing) else ctrl
            if work is ctrl and not on_base:
                _log("[dry-run] no dedicated integration checkout yet — reading the board "
                     f"from the launch checkout on `{current_branch(ctrl)}`, which may lag "
                     f"`{base}`")
        elif on_base:
            work = ctrl
            _log(f"note — the launch checkout is on `{base}`, so the runner is operating in "
                 f"it directly. Move it to your own branch (e.g. `git switch -c my/work`) "
                 f"to keep coding during a run — the runner will then use a dedicated `{base}` "
                 f"checkout and leave your working copy alone (runner-hardening #3).")
        else:
            try:
                work = ensure_integration_checkout(ctrl, base)
            except RuntimeError as exc:
                _log(f"refusing to run — {exc}")
                return 1
            _log(f"runner working in its dedicated `{base}` checkout at {work} — your launch "
                 f"checkout on `{current_branch(ctrl)}` is untouched")
            if dirty := dirty_outside_board(work):
                shown = ", ".join(dirty[:4]) + (f" (+{len(dirty) - 4} more)"
                                                if len(dirty) > 4 else "")
                _log(f"refusing to run — the dedicated `{base}` checkout is dirty outside "
                     f"Board/: {shown}. It is runner-owned; commit or discard those changes "
                     f"in {work} and re-run.")
                return 1

        _CTRL_ROOT, _WORK_ROOT = ctrl, work

        if not args.dry_run:
            ensure_workspace_trusted(ctrl)
            _status(work, phase="starting", since=_now())

        # Karel drags a card in the Kanban; Base Board writes `state:` and stops.
        # Reconciling first is what makes a card he readied from his phone at
        # midnight actually dispatchable at 2 AM.
        actions = reconcile.plan(work)
        if actions and not args.dry_run:
            reconcile.apply(work, actions, commit=True)
            _log(f"reconciled {len(actions)} board inconsistency/ies")
        elif actions:
            _log(f"[dry-run] would reconcile {len(actions)} board inconsistency/ies")

        if not args.dry_run:
            for note in recover(work):
                _log(f"recovered — {note}")

            # `runner-prune-run-dirs`: `done/` is reached by Karel, by hand,
            # outside the runner, so it is never pruned eagerly — this is the
            # startup sweep that catches it (and any `failed/` card an eager
            # prune missed before this shipped). Run-dir cap rides along for the
            # same reason: deterministic file-state housekeeping that only needs to
            # happen once per boot. The `<date>.log` tee is the one piece that lives
            # with the control plane (`ctrl`), so its retention sweep is rooted there.
            swept = sweep_terminal_cards(work)
            if swept:
                _log(f"pruned run-dir(s) for {len(swept)} card(s) settled in a "
                     f"terminal lane since last run: {', '.join(swept)}")
            cap_run_dirs_in_flight(work)
            cap_rescue_branches_in_flight(work)
            prune_old_run_logs(ctrl)
            demoted = enforce_worktree_ceiling(work)
            if demoted:
                _log(f"kept-worktree ceiling ({WORKTREE_KEEP_CEILING}) exceeded — "
                     f"demoted to git-WIP-only: {', '.join(demoted)}")

        bad = schema_violations(work)
        capabilities = host_capabilities(work)
        _log(f"host capabilities: {', '.join(sorted(capabilities)) or '(none declared)'}")

        # The run's own account of itself, which the digest reports instead of
        # inferring from lane state (`run_record`). Opened here — after the work
        # root is fixed, so it lands in the checkout whose digest will read it,
        # and before `select()`, so the skip list has somewhere to go. A dry run
        # gets the no-op record: `--dry-run` promises no writes.
        record = run_record.null() if args.dry_run else run_record.start(
            work, kind="card" if args.card else "run",
            label=_invocation_label(args), host=socket.gethostname())

        # `publish_remote` is empty on any host that doesn't declare it (the
        # laptop's normal state — the branch already lives in Karel's own repo),
        # and `origin` on a cloud checkout (`night.py` writes it into the
        # gitignored `.ai/host.json` override). See `publish()`.
        publish_remote = str(host_setting(work, "publish_remote", "")).strip()
        _log(f"publish: {'pushing to ' + publish_remote if publish_remote else 'off (no publish_remote for this host)'}")

        candidates = select(work, capabilities, bad, forced=args.card)
        ready = [c for c in candidates if c.dispatchable]
        _log(f"board: {len(candidates)} card(s) in tasks/, {len(ready)} dispatchable")
        for c in candidates:
            _log(f"  {'YES' if c.dispatchable else ' no'}  {c.card.id} — {c.reason}")
        # Why a card did NOT run is a fact only the run knows: nothing moves, so
        # no lane diff can recover it, and five art cards stalled on a capability
        # this host does not have looked exactly like an empty night for a week.
        record.skipped([(c.card.id, c.reason) for c in candidates if not c.dispatchable])
        # And the ones that DID run while over `CARD_COMFORT_BYTES`. A separate
        # field because an oversized card is dispatchable by design, so it is
        # absent from the list above — which left the digest silent about the
        # one case the signal exists for, a card dispatched over and over while
        # it grows.
        record.oversized(oversized_entries(candidates))

        if args.card:
            ready, why = resolve_named(work, args.card, candidates)
            if why:
                _log(f"refusing to run — {why}")
                return 1
            # Said out loud because `--card` narrows a list the lines above just
            # printed in full; without this, "which one is it actually going to
            # run?" is answered only by re-reading them.
            _log(f"narrowed to `{args.card}` — {ready[0].reason}")

        if args.dry_run:
            return 0

        deadline = _deadline(args.until, args.max_minutes)

        def _stop(reason: str) -> None:
            """End-of-loop reason, said once to both readers.

            The log is for tailing a run in flight; the record is what the
            morning digest reports. One call site so they cannot disagree — the
            first version of this instrumentation left `record.stop` off two of
            the nine `break` paths, and a night that ended for an unrecorded
            reason reads in the digest as a night that simply ran out of cards.
            """
            _log(f"stopping — {reason}")
            record.stop(reason)

        def _pipeline(candidate: Candidate, model: str) -> Dispatch:
            """Every stage that turns a ready card into a `Dispatch`.

            A function, rather than the two statements it inlines, so that the
            guard at its single call site covers a *region* instead of two named
            calls: a stage added to this pipeline later is inside the guard by
            construction, rather than by whoever adds it remembering to widen an
            `except` that names its predecessors. That property is the point —
            the crash this guard exists for came out of `dispatch`, but nothing
            makes `dispatch` the only stage that can raise.

            `settle` is deliberately *not* in here. A `settle` that raises leaves
            the card's lane indeterminate, and carrying on past that compounds
            the damage rather than containing it.
            """
            result = dispatch(work, candidate.card, base, model,
                              args.card_budget, args.test_timeout)

            # The review stage (automate-review-step): a card whose gates+tests
            # passed goes to the diff reviewer before it lands. The reviewer's
            # verdict routes it — needs-decision/ or (merge → testing/) — replacing
            # the old unconditional landing in review/. Only a `review` outcome
            # reaches it; a wall/failure/park is untouched. `review_stage` carries
            # the dispatch cost forward and adds its own, so `spent +=` stays once.
            if result.outcome == "review":
                result = review_stage(work, candidate.card, result, base,
                                      args.card_budget, args.test_timeout)
            return result

        def _settled(candidate: Candidate, result: Dispatch, model: str) -> str:
            """Settle one dispatch, record it, and return `settle`'s own account.

            Wrapped together because the record must carry the *landing*, not
            just the outcome: `failed` alone does not say whether the card is
            back in the queue for another attempt or has reached `failed/`, and
            that is the difference between "look at this tomorrow" and "this is
            dead until you look".
            """
            landed = settle(work, candidate.card.id, result)
            # The three wall cases must be tellable apart at a glance, which is
            # the whole complaint on `wall-on-review-wrapup-discards-a-verdict`:
            # "walled with nothing" is `limited` and already says *not attempted,
            # attempt given back*; "the gate harness crashed" is `blocked` and
            # says so; this third one used to be indistinguishable from the first
            # and now names itself. Appended here rather than inside `settle`
            # because it is a fact about the *night*, not about the card's lane —
            # and this is the one place the run log line is produced.
            #
            # Excludes every give-back outcome, not just `limited`. A card can be
            # honoured at the checker and *then* meet a crashed gate harness or a
            # drifted gate, and `settle` files that as `blocked` with "not
            # attempted, attempt given back" — onto which "the card landed" would
            # be a flat contradiction in the one line a 6 AM reader trusts.
            if result.wall is not None and result.outcome not in (
                    "limited", "blocked", "interrupted"):
                landed += (" — the stage walled on its wrap-up after writing a complete "
                           "verdict, so the verdict was honoured and the card landed; "
                           "the night's window is still closed")
            record.dispatched(
                candidate.card.id, title=candidate.card.title,
                worker=candidate.card.worker, model=model,
                attempt=candidate.card.attempts, outcome=result.outcome,
                detail=result.detail, cost_usd=result.cost_usd, landed=landed,
                evidence=result.evidence)
            return landed

        def _window_closed(wall: limits.Wall, card_id: str, *, retrying: bool) -> bool:
            """Spend one of the night's windows on `wall`, and say whether the run
            may go on. `False` means stop — `_stop` has already recorded why.

            Shared by the two callers because a wall carries the same scheduling
            fact whichever way its card settled
            (`wall-on-review-wrapup-discards-a-verdict`): the `limited` branch,
            where the card was never attempted and is retried in the next window,
            and a card that *landed* on an honoured verdict, whose index has
            already advanced. `retrying` is the only difference, and it is only
            wording — the arithmetic, the caps and the sleep are identical.
            """
            nonlocal walls, hiccups
            if wall.spends_a_session:
                walls += 1
                if walls >= args.sessions:
                    _stop(f"{walls} session limit(s) used, which is "
                          f"--sessions {args.sessions}")
                    return False
            else:
                # A 429 is a hiccup, not the window closing, so it buys a
                # short wait instead of one of the night's sessions. Capped
                # all the same: a hiccup that never clears is indistinguishable
                # from a wall we misread, and must not retry until morning.
                hiccups += 1
                if hiccups > TRANSIENT_RETRIES:
                    _stop(f"{hiccups} transient rate limits tonight; that is "
                          f"no longer a hiccup. See `.ai/runs/` for what the CLI said")
                    return False
            if not wall.waits_out:
                _stop(f"this is a {wall.scope} limit; it does not "
                      f"reopen inside a night, so waiting would idle until morning")
                return False
            resume = limits.resume_at(wall)
            if deadline and resume >= deadline:
                _stop(f"the window reopens at {resume:%H:%M}, past "
                      f"{deadline:%H:%M}")
                return False
            counted = f"{walls} of {args.sessions}" if wall.spends_a_session \
                else f"transient, {hiccups} of {TRANSIENT_RETRIES}"
            then = f"then retrying {card_id}" if retrying \
                else f"then going on from {card_id}, which landed"
            _log(f"usage limit reached ({counted}) — sleeping until "
                 f"{resume:%H:%M}, {then}")
            record.note(f"usage limit reached ({counted}) — slept until "
                        f"{resume:%H:%M}, {then}")
            # Otherwise `status.json` freezes on whatever phase the wall was met
            # in, its heartbeat ages past `HEARTBEAT_TRUSTED_FOR` a couple of
            # hours into a sleep that can run five, and the panel reports "no run
            # in progress" for a process that is very much still here, just
            # waiting on a clock it already knows. `resume_at` is the fact the
            # panel cannot derive from a stale heartbeat alone.
            _status(work, phase="sleeping", card=card_id,
                    resume_at=resume.isoformat(timespec="minutes"), since=_now())
            if not _sleep_until(resume):
                _stop("kill switch appeared while waiting for the window")
                return False
            return True

        spent = 0.0
        done = 0
        walls = 0            # usage-limit windows this night has spent
        hiccups = 0          # transient 429s waited out, which do not spend a window
        in_a_row = 0         # consecutive failures, the net under limits.detect

        # An index rather than `for candidate in ready`, because a card that met
        # the wall was never actually attempted and has to be retried at the head
        # of the next window. Only a real outcome advances it.
        index = 0
        while index < len(ready):
            candidate = ready[index]
            if args.max_cards and done >= args.max_cards:
                _stop(f"reached --max-cards {args.max_cards}")
                break
            if deadline and dt.datetime.now() >= deadline:
                _stop(f"past {deadline:%H:%M}")
                break
            if args.budget and spent >= args.budget:
                _stop(f"run budget ${args.budget:.2f} spent")
                break
            if _stop_requested():
                _stop("kill switch appeared mid-run")
                break

            try:
                model = tiers.resolve(work, candidate.card.tier)
            except tiers.TierError as exc:
                _log(f"  skipping {candidate.card.id} — {exc}")
                index += 1
                continue

            # `Exception`, never a bare `except`: `KeyboardInterrupt` and
            # `SystemExit` are not this card's failure and must still end the
            # night — a Ctrl-C that filed an `## Error` against whatever card was
            # in flight would be worse than the crash.
            try:
                result = _pipeline(candidate, model)
            except Exception as exc:       # noqa: BLE001 — see `crashed_dispatch`
                result = crashed_dispatch(work, candidate.card, exc)
                _log(f"  ! {candidate.card.id} — {result.detail}")
            spent += result.cost_usd

            # Before every other outcome: neither a broken harness nor a drifted
            # gate is a verdict on this card, and continuing would walk the rest
            # of the queue spending an attempt on each for a defect none of them
            # caused.
            if result.outcome == "blocked":
                _log("  " + _settled(candidate, result, model))
                if result.repo_drift:
                    _stop(f"a gate violation outside {candidate.card.id}'s own diff — "
                          f"repo drift, not this card's fault. No attempt was spent and "
                          f"no card was blamed; fix the drifted gate and re-run. "
                          f"{result.detail}")
                else:
                    _stop("the gate harness is broken on this machine, so no card "
                          "can be judged. No attempt was spent and no card was blamed; fix "
                          "it and re-run. The traceback is in `.ai/runs/`")
                break

            if result.outcome == "limited":
                _log("  " + _settled(candidate, result, model))
                if not _window_closed(result.wall, candidate.card.id, retrying=True):
                    break

                # Reloaded, not reused. `dispatch` computes the attempt number
                # from the Card object it is handed and mutates it in place, so
                # the copy in `ready` still carries the attempt `settle` has just
                # given back on disk — retrying with it would spend attempt 2 for
                # the first real run and quietly undo the rewind. Rescanning is
                # also how the runner notices Karel moved the card from his phone
                # during the hours it was asleep.
                fresh = board.find(work, candidate.card.id)
                if fresh is None or fresh.lane != "tasks":
                    where = "the board" if fresh is None else f"{fresh.lane}/"
                    _log(f"  {candidate.card.id} moved to {where} while we waited — "
                         f"leaving it alone")
                    index += 1
                    continue
                candidate.card = fresh
                _log("window reopened — resuming")
                continue  # same card, new window

            index += 1
            done += 1
            # `interrupted` counts too, and not as a courtesy: before that
            # outcome existed an `api_error` dispatch was `failed`, so a
            # connection dropping card after card tripped this breaker on the
            # third one. Excluding it would have quietly removed that net — and
            # made it removable in *both* directions, since an alternating
            # failed/interrupted night would never reach three of either. The
            # streak is about consecutive dispatches that produced no verdict,
            # which is exactly the shape "not about the cards" takes.
            in_a_row = (in_a_row + 1
                        if result.outcome in ("failed", "interrupted") else 0)
            _log("  " + _settled(candidate, result, model))
            # Idempotent, and after every settled card rather than only at the end
            # of the night — a cloud container can be killed between cards, and
            # this is what keeps that from losing already-settled work with it.
            # `blocked`/`limited` don't call this here; both `break` out of the
            # loop and reach the final publish below instead. `trusted_branch`
            # names the card just dispatched — this checkout has full, current
            # knowledge of it, so it skips the divergence check `publish` runs
            # against every other (dormant) card branch.
            publish(work, publish_remote, base, trusted_branch=f"ai/{candidate.card.id}")
            if args.budget:
                _log(f"  spent ${spent:.2f} of ${args.budget:.2f}")
            # Three unrelated cards failing back to back is not about the cards.
            # Most likely it is a wall `limits.detect` did not recognise, and the
            # damage of guessing wrong here (one quiet night) is far below the
            # damage of not guessing (the whole queue spent, then `failed/`).
            if in_a_row >= CONSECUTIVE_FAILURE_STOP:
                _stop(f"{in_a_row} dispatches failed in a row (an interruption counts "
                      f"as one — it produced no verdict either). If this was a usage "
                      f"limit the detector missed, the CLI output is under `.ai/runs/` "
                      f"and the phrase belongs in `limits._WALL`; if they were "
                      f"interruptions, the thing to look at is this machine's "
                      f"connection, not the cards")
                break

            # The night still treats a wall as a wall even when the card landed
            # (`wall-on-review-wrapup-discards-a-verdict`). A stage walled on its
            # wrap-up, its verdict was honoured and the card settled on its
            # merits — but the plan's window is shut, and walking on to the next
            # card as if it were open would spend an attempt on every one of
            # them. Same arithmetic as the `limited` branch above, minus the
            # retry: this card is finished and the index has already advanced, so
            # it must NOT take that branch's `continue  # same card, new window`.
            # After `publish`, so hours of sleep never sit between a settled card
            # and its being pushed.
            if result.wall is not None and not _window_closed(
                    result.wall, candidate.card.id, retrying=False):
                break

        # After the cards, spend whatever window is left on staleness — never
        # before, so a card that produces real work always wins the budget over
        # a maintenance check. Only if a window remains: a run that already hit a
        # wall or the deadline has nothing left to give the sweep.
        ran_stale_sweep = False
        if args.stale and not (deadline and dt.datetime.now() >= deadline) \
                and not _stop_requested():
            try:
                model = tiers.resolve(work, "worker")
                checked, verified, carded = stale_phase(
                    work, args.stale, model, deadline, args.card_budget,
                    args.test_timeout, record)
                _log(f"stale sweep: {checked} checked, {verified} verified, {carded} carded")
                # Written even when `checked` is 0 — "ran, nothing changed enough to
                # check" and "never run" are different facts, and Digest.md needs to
                # be able to tell them apart (menu-summary-on-card follow-up).
                stale_sweep.write_status(work, dt.date.today().isoformat(),
                                         checked, verified, carded)
                ran_stale_sweep = True
            except tiers.TierError as exc:
                _log(f"stale sweep skipped — {exc}")
                record.note(f"stale sweep skipped — {exc}")

        # Closed *before* the digest renders, never after: the digest reports on
        # every record since the last one, and this run's own record is the most
        # important of them. Rendering first would publish a report of a night
        # that had not yet admitted how it ended.
        record.finish(cost_usd=spent, walls=walls, dispatched=done)

        _status(work, phase="digest", since=_now())
        text = digest.render(work)
        textio.write_text_lf(work / digest.OUT, text + "\n")
        extra = (str(stale_sweep.STATUS),) if ran_stale_sweep else ()
        # `--append-digest` (2026-07-30, Karel: "for when I don't have time to
        # read or use a scheduler over weekend") writes a commit `digest.py`'s
        # `_previous_digest` does not recognise, so the read baseline does not
        # move — the *next* run's window still reaches back to the last real
        # `digest:` commit, and keeps reaching back across any number of append
        # runs, until one is invoked without the flag. See `digest.py`'s "Two
        # commit shapes, one baseline" for the mechanics.
        digest_prefix = digest.APPEND_COMMIT_PREFIX if args.append_digest else "digest"
        board.commit_board(work, f"{digest_prefix}: {dt.date.today().isoformat()}",
                           extra_paths=extra)
        # The final sweep: carries the just-written digest (the per-card publish
        # above cannot, since the digest is only rendered here) and is the one
        # publish reached by every early `break` above — `blocked`, a wall, the
        # deadline, the kill switch, all fall through to here.
        publish(work, publish_remote, base)
        _log(f"digest written to {digest.OUT.as_posix()} — {done} card(s) dispatched, "
             f"{walls} session limit(s) hit, ${spent:.2f} equivalent"
             + (" (append mode — read baseline not advanced)" if args.append_digest else ""))
        _status(work, phase="finished", cards_dispatched=done, session_limits=walls,
                spent_usd=round(spent, 2), since=_now())
        return 0
    finally:
        if not args.dry_run:
            release_lock(ctrl)
        # Reset the module globals to avoid leaking state to other tests or runs.
        # This is particularly important for pytest, where the same module is reused
        # across tests; leaving these set breaks test isolation.
        _CTRL_ROOT = None
        _WORK_ROOT = None


def _parser(root: Path | None = None) -> argparse.ArgumentParser:
    """Built here rather than inside `main` so tests parse the same flags and the
    same defaults the CLI does. Defaults are the thing under test — `--budget`
    and `--card-budget` changed meaning on 2026-07-23 — and a test that
    hand-built a Namespace would keep passing after they drifted."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be dispatched and why not; no LLM, no writes")
    parser.add_argument("--status", action="store_true",
                        help="print where a run currently is (card, phase, elapsed) from "
                             "`.ai/runs/status.json` and exit. Takes no lock and touches "
                             "no git — safe to run while a night is in flight")
    base = default_base(root)
    parser.add_argument("--base", default=base, help=f"branch to build on (default {base})")
    parser.add_argument("--card", help="dispatch only this card id. Naming a card is an "
                                       "explicit human request, so `unattended: false`, "
                                       "backoff and the attempt limit are waived for it; "
                                       "`requires:`, a missing charter and a broken schema "
                                       "are not. Exits non-zero with a reason if it cannot run.")
    parser.add_argument("--max-cards", type=int, default=0, help="stop after N dispatches")
    parser.add_argument("--until", help="stop dispatching at this local time, HH:MM")
    parser.add_argument("--max-minutes", type=int, help="stop dispatching after N minutes")
    parser.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS,
                        help="how many usage-limit windows the night may spend. "
                             "1 (default) works until the session limit is reached and "
                             "stops; 2 sleeps through the first reset and stops at the "
                             "second wall. A weekly limit always stops")
    parser.add_argument("--budget", type=float, default=DEFAULT_RUN_BUDGET_USD,
                        help="optional USD cap for the whole run. 0 (default) means none — "
                             "on a subscription plan the figure is API-equivalent rather "
                             "than money spent; use --sessions")
    parser.add_argument("--card-budget", type=float, default=DEFAULT_CARD_BUDGET_USD,
                        help="optional USD cap handed to each worker process; 0 (default) "
                             "passes no cap at all")
    parser.add_argument("--test-timeout", type=int, default=600,
                        help="seconds allowed for the test suite (~2 min today)")
    parser.add_argument("--stale", type=int, default=0, nargs="?", const=10_000,
                        metavar="N",
                        help="after the cards, run the Tier-2 staleness sweep on the N "
                             "highest-churn docs, spending only leftover window. Bare "
                             "--stale runs it dry (every doc that changed since it was last "
                             "verified); --stale 3 caps it at three; 0 (default) skips it. "
                             "A doc is ledgered only on a complete verdict, and findings "
                             "become one fix-card per doc — the runner never edits a doc")
    parser.add_argument("--append-digest", action="store_true",
                        help="write this run's digest without advancing the read baseline, "
                             "so the next run's digest still reaches back to the last one "
                             "written *without* this flag — for a stretch of runs (a "
                             "scheduled weekend) nobody is there to read each one. "
                             "night.py's unattended path defaults to this; a run started "
                             "by hand defaults to the opposite (baseline advances) on the "
                             "assumption someone is about to look")
    return parser


def main(argv: list[str] | None = None) -> int:
    root = repo_root()
    args = _parser(root).parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.status:
        return print_status(root)
    return run(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
