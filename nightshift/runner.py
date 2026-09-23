#!/usr/bin/env python3
"""The overnight runner — a dumb orchestrator (`09_runner.md`).

Rescan the board, pick what is dispatchable, dispatch it, run the gates over the
result, move the card, commit the board. **Zero LLM calls in the runner
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

**A run works one or more queues, and `--queue` picks which.** `chores` is the
`kind: chore` batch — dispatched cheaply, merged as one unit, reviewed once;
`tasks` is the board's own queue, each card reviewed and merged on its own; `both`
is the default and works the batch first. They are the same loop with different
policy (`Queue`, `Landing`), on one budget and one set of session windows, which is
the whole point of running them in one process rather than two.

Usage:
    python -m nightshift.runner --dry-run       # what would be dispatched, and why not
    python -m nightshift.runner --max-cards 1   # one card, then stop
    python -m nightshift.runner --queue tasks   # the task queue only; leave chores alone
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
import contextlib
import datetime as dt
import json
import socket
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from nightshift import board          # the card model
from nightshift import boardhealth    # cards stuck between two lanes
from nightshift import limits
from nightshift import run_record
from nightshift import runtimes       # the `runtime` axis: cloud | local (doc 04 §4)
from nightshift import suite          # test policy
from nightshift import tiers

# The split modules (`git-module-and-runner-split`): each owns one job this
# file used to do inline. This module is left as the entry point and the main
# loop -- host config/lock, worktrees, gates+tests, dispatch, review, the
# staleness phase and settle each moved out; `python -m nightshift.runner`
# and `runner.main`/`runner.run` still work unchanged.
from nightshift import dispatch, hostconfig, outcome, review, settle, stale, startup, telemetry, verify, worktree


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
        if hostconfig._stop_requested():
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
    """How this run was asked for, in the few words Command Center heads a run
    with — `night, up to 8 cards, staleness sweep` or `card ice-damage`.

    Reconstructed from the parsed args rather than `sys.argv`, so it says the
    same thing whether the run came from the Command
    Center's button or Karel's own command line. It exists because two runs in one night are not
    interchangeable: on 2026-07-30 an aborted 8-card night and a deliberate
    one-card rerun both landed in the same window, and a report that cannot name
    which was which cannot explain either.
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
    return ", ".join(parts)


def print_status(root: Path) -> int:
    """`--status`: where the runner is right now, read from disk alone.

    Deliberately does not take the lock, run preflight or touch git — it is the
    question you ask *while* a night is in flight, and a status command that
    could interfere with the thing it reports on would be worse than no status
    command. Safe to run from a second terminal at 3 AM.
    """
    path = root / hostconfig.STATUS_FILE
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
    alive = hostconfig._pid_alive(pid)
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

    log = root / hostconfig.RUNS / f"{dt.date.today().isoformat()}.log"
    if log.is_file():
        print(f"log     : {log.as_posix()}")
    return 0


# ------------------------------------------------------------- the run lifecycle
#
# Everything that happens around a queue rather than to a card: the refusals that
# stop a run before it starts, the checkout it works in, the lock it holds, the
# record it keeps, and the teardown that must happen however it ends.
#
# **It is one function because it was two.** `run()` and `chores.execute()` each
# opened a run the same way — `preflight` -> `acquire_lock` -> resolve the working
# checkout -> `run_record.start` -> `ensure_workspace_trusted` — and each closed it
# with its own `finally` doing lock release, record `finish` and local-server
# teardown. Neither copy was wrong. The cost was that every run-level fact had to
# be added to both, and `--no-local` is the measured case: a flag, a parameter,
# another parameter and a keyword, to reach code that already had it one chain over.
#
# **They had already drifted, which is the rest of the argument.** The batch
# guarded against leaving an unfinished record behind and the night did not, so a
# night that crashed left a record reading as still in flight — the exact failure
# `run_record`'s docstring is about, arrived at from the other side. One lifecycle
# means the guard is not something either path can be missing.


class RunRefused(RuntimeError):
    """A run that never started. The reasons are already logged; `code` is the exit.

    An exception rather than a sentinel return because the refusals happen at six
    different depths of `run_lifecycle`'s setup, and handing each one back as a
    `tuple[RunContext | None, int]` would put every caller in charge of remembering
    which of them still has a lock to release. The `with` block knows instead.
    """

    def __init__(self, code: int = 1) -> None:
        super().__init__(f"the run was refused (exit {code})")
        self.code = code


@dataclass
class RunContext:
    """What a run establishes once, and every queue inside it then reads.

    `ctrl` and `work` are two checkouts and the distinction is load-bearing: the
    control plane (STOP file, lock, status, run log) stays where the maintainer
    interacts with it, while the board and git work may have moved to a dedicated
    `base` checkout. They are the same path only when the launch checkout is
    already on the integration branch.
    """

    ctrl: Path                      # the launch checkout — the control plane's home
    work: Path                      # where the board and git work happen
    base: str                       # the integration branch
    record: run_record.Record
    publish_remote: str             # "" on a host that declares none
    dry_run: bool
    capabilities: set[str]
    bad_schema: dict[str, list[str]]


def _resolve_workspace(ctrl: Path, base: str, dry_run: bool) -> Path:
    """The checkout the board and git work happen in, or `RunRefused`.

    The control plane always stays with the launch checkout `ctrl`; only the
    board/git work moves (runner-hardening #3):

      * launch checkout OFF the integration branch — the normal topology. Cut a
        dedicated `base` checkout so the runner never touches the working copy and
        the two never fight over the branch.
      * launch checkout ON `base` — git forbids a second checkout of it, so work in
        place as before, and nudge towards moving off it so coding can continue.
      * dry run — read an existing dedicated checkout if there is one, else the
        launch checkout; write nothing, cut nothing.
    """
    on_base = hostconfig.current_branch(ctrl) == base
    if dry_run:
        existing = worktree.integration_checkout_path(ctrl)
        work = existing if worktree._worktree_registered(ctrl, existing) else ctrl
        if work is ctrl and not on_base:
            hostconfig._log("[dry-run] no dedicated integration checkout yet — reading the board "
                 f"from the launch checkout on `{hostconfig.current_branch(ctrl)}`, which may lag "
                 f"`{base}`")
        return work
    if on_base:
        hostconfig._log(f"note — the launch checkout is on `{base}`, so the runner is operating in "
             f"it directly. Move it to your own branch (e.g. `git switch -c my/work`) "
             f"to keep coding during a run — the runner will then use a dedicated `{base}` "
             f"checkout and leave your working copy alone (runner-hardening #3).")
        return ctrl
    try:
        work = worktree.ensure_integration_checkout(ctrl, base)
    except RuntimeError as exc:
        hostconfig._log(f"refusing to run — {exc}")
        raise RunRefused(1) from exc
    hostconfig._log(f"runner working in its dedicated `{base}` checkout at {work} — your launch "
         f"checkout on `{hostconfig.current_branch(ctrl)}` is untouched")
    if dirty := hostconfig.dirty_outside_board(work):
        shown = ", ".join(dirty[:4]) + (f" (+{len(dirty) - 4} more)"
                                        if len(dirty) > 4 else "")
        hostconfig._log(f"refusing to run — the dedicated `{base}` checkout is dirty outside "
             f"Board/: {shown}. It is runner-owned; commit or discard those changes "
             f"in {work} and re-run.")
        raise RunRefused(1)
    # The mirror of the check above, one checkout over: that one guards the board
    # the runner *writes*, this one the board the maintainer *edits*. Redirecting
    # here means their `Board/` and the runner's are two files, and an answer typed
    # into the wrong one is the failure nothing else can see.
    if refusal := worktree.stranded_board_refusal(ctrl, work, base):
        hostconfig._log(f"refusing to run — {refusal}")
        raise RunRefused(1)
    return work


def _startup_housekeeping(ctrl: Path, work: Path) -> None:
    """The once-per-run file-state sweeps, in the order they have always run.

    `runner-prune-run-dirs`: `done/` is reached by hand, outside the runner, so it
    is never pruned eagerly — this is the startup sweep that catches it (and any
    `failed/` card an eager prune missed before that shipped). The run-dir and
    rescue-branch caps ride along for the same reason: deterministic housekeeping
    that only needs to happen once per run. The `<date>.log` tee is the one piece
    that lives with the control plane, so its retention sweep is rooted at `ctrl`.
    """
    for note in startup.recover(work):
        hostconfig._log(f"recovered — {note}")
    if swept := worktree.sweep_terminal_cards(work):
        hostconfig._log(f"pruned run-dir(s) for {len(swept)} card(s) settled in a "
             f"terminal lane since last run: {', '.join(swept)}")
    worktree.cap_run_dirs_in_flight(work)
    worktree.cap_rescue_branches_in_flight(work)
    worktree.prune_old_run_logs(ctrl)
    if demoted := worktree.enforce_worktree_ceiling(work):
        hostconfig._log(f"kept-worktree ceiling ({hostconfig.WORKTREE_KEEP_CEILING}) exceeded — "
             f"demoted to git-WIP-only: {', '.join(demoted)}")
    # Cards stuck between two lanes by a hand-made merge or an unread verdict — logged,
    # never acted on: the fix is a person's (`nightshift.boardhealth`).
    for finding in boardhealth.check(work):
        hostconfig._log(f"board health — {finding}")


@contextlib.contextmanager
def run_lifecycle(ctrl: Path, base: str, *, kind: str, label: str,
                  dry_run: bool = False,
                  named_card: bool = False) -> Iterator[RunContext]:
    """Open a run, yield what it established, and close it however it ends.

    Raises `RunRefused` — reasons already logged — for every condition that stops a
    run before any work is dispatched. Past the `yield`, the teardown runs on every
    path out including a crash, which is the whole reason this is a context manager
    rather than a pair of functions: a ~13.8 GB pinned local model outliving the run
    that started it is `asset-generation-processes-dont-shut-down`, and the paths
    that matter for it are the abnormal ones.
    """
    global _CTRL_ROOT, _WORK_ROOT
    # Opened before preflight so a refusal is recorded too — "it refused and I did
    # not see why" is the same invisible-failure shape as a silent night.
    if not dry_run:
        hostconfig._open_run_log(ctrl)
    check = startup.startup_checks(ctrl, base, dry_run, named_card=named_card)
    if not check.ok:
        for reason in check.reasons:
            # A kill-switch refusal is the maintainer's own stop, not a defect — it
            # reads as "stopped", the same word `jobs.state()` already shows for it
            # via `EXIT_STOPPED`, rather than "refusing to run", which reads like
            # every other genuine preflight failure beside it.
            prefix = "stopped" if reason.startswith("kill switch:") else "refusing to run"
            hostconfig._log(f"{prefix} — {reason}")
        raise RunRefused(hostconfig.EXIT_STOPPED if (ctrl / hostconfig.STOP_FILE).is_file() else 1)

    if not dry_run and not hostconfig.acquire_lock(ctrl):
        raise RunRefused(1)

    record = run_record.null()
    try:
        work = _resolve_workspace(ctrl, base, dry_run)
        _CTRL_ROOT, _WORK_ROOT = ctrl, work

        if not dry_run:
            # Headless `-p` has no trust dialog, so an untrusted workspace makes
            # every dispatch fail with no useful message.
            startup.ensure_workspace_trusted(ctrl)
            hostconfig._status(work, phase="starting", since=hostconfig._now())

        if not dry_run:
            _startup_housekeeping(ctrl, work)

        bad = startup.schema_violations(work)
        capabilities = hostconfig.host_capabilities(work)
        hostconfig._log(f"host capabilities: {', '.join(sorted(capabilities)) or '(none declared)'}")

        # The run's own account of itself, which the digest reports instead of
        # inferring from lane state (`run_record`). Opened here — after the work
        # root is fixed, so it lands in the checkout whose digest will read it, and
        # before any `select()`, so the skip list has somewhere to go. A dry run
        # gets the no-op record: `--dry-run` promises no writes.
        record = run_record.null() if dry_run else run_record.start(
            work, kind=kind, label=label, host=socket.gethostname())

        # `publish_remote` is empty on any host that doesn't declare it (a laptop's
        # normal state — the branch already lives in the maintainer's own repo), and set
        # in the gitignored `.ai/host.json` override where a remote copy is wanted. See `publish()`.
        publish_remote = str(hostconfig.host_setting(work, "publish_remote", "")).strip()
        hostconfig._log(f"publish: {'pushing to ' + publish_remote if publish_remote else 'off (no publish_remote for this host)'}")

        yield RunContext(ctrl=ctrl, work=work, base=base, record=record,
                         publish_remote=publish_remote, dry_run=dry_run,
                         capabilities=capabilities, bad_schema=bad)
    finally:
        # `complete` separates a run that reached its own end from one that was
        # killed, so it is set on every path out — including the ones that raise.
        # Already-finished records are not reopened: re-stamping would move the
        # finish time of a run that ended cleanly minutes earlier.
        if not dry_run and not record.data.get("complete"):
            record.stop("the run ended without reaching its own end")
            record.finish(dispatched=len(record.data.get("dispatched", [])))
        # Stop only the local servers *this process* started (`runtimes` records
        # them; one the maintainer had running is never in that set), so a run that
        # merely used their llama-server does not kill it at 4 AM. In the teardown
        # rather than beside the happy path's return, because the paths that matter
        # are the other ones: a wall, the deadline, the kill switch and a crash all
        # leave through here, and a ~13.8 GB pinned, non-reclaimable model outliving
        # the run is exactly `asset-generation-processes-dont-shut-down`.
        if stopped_ports := runtimes.stop_started_servers():
            hostconfig._log("stopped the local server(s) this run started: "
                 + ", ".join(str(p) for p in stopped_ports))
        if not dry_run:
            hostconfig.release_lock(ctrl)
        # Reset the module globals so state does not leak to another run — which
        # matters most under pytest, where the same module is reused across tests.
        _CTRL_ROOT = None
        _WORK_ROOT = None


# ------------------------------------------------------------------- the queues
#
# A run works one or more *queues*. A queue is a body of work plus the policy that
# distinguishes it: which cards, at which tier, to which charter, and — the only
# difference that is not a single value — how a card lands once it has a verdict.
#
# There are two, and naming what actually separates them is most of the point:
#
#   tasks   — the board's own order, each card's own `tier:`/`worker:`, three
#             attempts, reviewed and merged one at a time as the loop goes.
#   chores  — `kind: chore` cards only, one tier by name for all of them, one
#             attempt each, and *held* when green: the batch merges its survivors
#             as a single unit and reviews that unit once. `chores.py` owns it.
#
# Everything else the loop does — the kill switch, the deadline, the budget, the
# usage walls and their sleeps, the crash guard, the two breakers, publishing — is
# a fact about a run and not about which queue it is working, so it is written
# once here and both get it. The chore batch had none of the wall handling before
# this; a wall used to end a batch where it puts a night to sleep.


@dataclass
class Tally:
    """The accounting a run keeps across every queue it works.

    One object for the whole run rather than one per queue, and that is the
    substantive gain from working chores and tasks in one process instead of two:
    `--budget`, `--sessions` and the transient-hiccup cap are facts about the
    *night*. Two processes get two of each and neither can see the other's spend.

    `stopped` is the reason the run may not go on to another queue — a spent
    budget, a closed window, the kill switch. Set through `_stop`, so it cannot
    disagree with what the log and the record were told.
    """

    spent: float = 0.0
    done: int = 0
    walls: int = 0       # usage-limit windows this run has spent
    hiccups: int = 0     # transient 429s waited out, which do not spend a window
    stopped: str = ""


class Landing:
    """What a queue does with a card once its dispatch has a verdict.

    This class *is* the per-card landing — the night's — rather than an abstract
    base with the night's version somewhere else, because the per-card case is the
    ordinary one and an empty subclass named after it would be ceremony. The other
    implementation is `chores.BatchLanding`, which holds green cards back instead
    of settling them and merges them as one unit in `close`.

    The three hooks are exactly the three places the two genuinely differ. Anything
    a future queue needs that is a *value* rather than a behaviour belongs on
    `Queue` instead.
    """

    #: Whether a green card goes to the diff reviewer on its own, inside the
    #: dispatch pipeline. False for a batch, which reviews the merged diff once —
    #: `chores`' opening argument, and the reason chores are cheap.
    reviews_each_card = True

    def may_dispatch(self, ctx: RunContext, card: board.Card) -> str:
        """`""` to go ahead, or the reason this queue stops before this card.

        Checked before *every* dispatch rather than once for the queue: a fan-out
        that starts with headroom can lose it partway, and a dispatch cannot be
        un-started.
        """
        return ""

    def settle(self, ctx: RunContext, candidate: dispatch.Candidate, result: outcome.Dispatch,
               model: str) -> str:
        """Land one dispatch, record it, and return the account the run log prints.

        Settling and recording are one hook because the record must carry the
        *landing*, not just the outcome: `failed` alone does not say whether the
        card is back in the queue for another attempt or has reached `failed/`,
        and that is the difference between "look at this tomorrow" and "this is
        dead until you look".
        """
        landed = settle.settle(ctx.work, candidate.card.id, result)
        # The three wall cases must be tellable apart at a glance, which is the
        # whole complaint on `wall-on-review-wrapup-discards-a-verdict`: "walled
        # with nothing" is `limited` and already says *not attempted, attempt
        # given back*; "the gate harness crashed" is `blocked` and says so; this
        # third one used to be indistinguishable from the first and now names
        # itself. Appended here rather than inside `settle` because it is a fact
        # about the run, not about the card's lane — and this is the one place the
        # run log line is produced.
        #
        # Excludes every give-back outcome, not just `limited`. A card can be
        # honoured at the checker and *then* meet a crashed gate harness or a
        # drifted gate, and `settle` files that as `blocked` with "not attempted,
        # attempt given back" — onto which "the card landed" would be a flat
        # contradiction in the one line a 6 AM reader trusts.
        if result.wall is not None and result.outcome not in (
                "limited", "blocked", "interrupted"):
            landed += (" — the stage walled on its wrap-up after writing a complete "
                       "verdict, so the verdict was honoured and the card landed; "
                       "the night's window is still closed")
        ctx.record.dispatched(
            candidate.card.id, title=candidate.card.title,
            worker=candidate.card.worker, model=model,
            attempt=candidate.card.attempts, outcome=result.outcome,
            detail=result.detail, cost_usd=result.cost_usd, landed=landed,
            evidence=result.evidence)
        # The full per-stage breakdown behind the one `cost_usd` float above
        # (`token-economy.md` phase 0.1) — a read of `out_dir`, not a second
        # dispatch, so it costs nothing to take even on a card that failed.
        telemetry.record_usage(ctx.record,
                     telemetry.run_dir(ctx.work, candidate.card, candidate.card.attempts),
                     card_id=candidate.card.id, model=model, efforts=result.efforts)
        return landed

    def close(self, ctx: RunContext, queue: Queue, tally: Tally) -> int:
        """Whatever must happen once the queue drains, and the run's exit code.

        Nothing, for a night: every card has already landed on its own way past.
        """
        return 0


@dataclass
class Queue:
    """One body of work a run takes on, and the policy that distinguishes it.

    Every field but `landing` is a *value* the dispatch loop reads — which is the
    shape the note that prompted this asked for, and the test of whether a
    difference between a night and a batch has been named properly. A difference
    that cannot be written as one of these is a behaviour, and belongs on
    `Landing` where there are exactly three of them.
    """

    name: str                       # "tasks" | "chores" — what the log calls it
    cards: list[dispatch.Candidate]
    landing: Landing
    #: The tier every card in this queue runs at, by *name*, or `""` to take each
    #: card's own `tier:`. The batch names one (`chores.CHORE_TIER`) precisely so a
    #: hand-edited `tier:` cannot pull it onto the expensive model.
    tier: str = ""
    #: The charter every card is dispatched to, or `""` for each card's `worker:`.
    worker: str = ""
    effort: str = ""
    #: How much of the suite one card's own verification runs. `None` leaves
    #: `dispatch`'s own default.
    test_selector: Callable[[set[str], Path], suite.Selection] | None = None


def _record_kind(args: argparse.Namespace) -> str:
    """What `run_record` calls this run.

    The panel reads it to decide what to show — `_chores_phase_rows` is keyed off
    it — so `both` is its own kind rather than either of the two it contains.
    """
    if args.card:
        return "card"
    return {"tasks": "run", "chores": "chores", "both": "both"}[args.queue]


def run(root: Path, args: argparse.Namespace, *,
        queues: Callable[[RunContext], list[Queue]] | None = None) -> int:
    """One run: open it, work the queues it was asked for, close it.

    Everything around the queues — the refusals, the working checkout, the lock,
    the record and the teardown — is `run_lifecycle`. What a queue *is* is `Queue`
    and `Landing` above.

    `queues` overrides which ones this run works, and exists for exactly one
    caller: `chores.execute` builds its own so it can keep a reference to the
    `BatchLanding` and hand the finished `Batch` back to its CLI. Without the seam
    that function would need its own copy of `_work_the_run`, which is the second
    spine this whole change removes.
    """
    try:
        with run_lifecycle(root, args.base,
                           kind=_record_kind(args),
                           label=_invocation_label(args),
                           dry_run=args.dry_run,
                           named_card=bool(args.card)) as ctx:
            return _work_the_run(ctx, args, queues=queues)
    except RunRefused as exc:
        return exc.code


def _queues_for(ctx: RunContext, args: argparse.Namespace) -> list[Queue]:
    """The queues this run works, in the order it works them.

    **Chores first, then tasks, and the order is load-bearing.** Three reasons,
    worst consequence first:

      * `chores._land_the_batch` refuses to review a batch whose phase 1 stopped
        early, on the grounds that the window can no longer be trusted to hold a
        review. A task queue that ran first and spent the window would therefore
        leave every chore handed over unreviewed — strictly worse than running the
        two separately, which is the one outcome this change must not produce.
      * A batch branch cannot stay open across hours of per-card merges onto
        `base`: every task that landed would make the batch's own suite run stale,
        and its green a statement about a tree that no longer exists.
      * Chores are cheap and bounded — one attempt each, one suite run for the set
        — so putting them first means the cheap wins land even when the run dies
        early.

    Interleaving them was considered and is the same argument as the second point,
    only continuously rather than once.

    `chores` is imported here rather than at module scope because it imports *this*
    module for the dispatch path, so the dependency only works in one direction at
    import time — the same reason `drain` is imported at its call site below.
    """
    if args.card:
        # A named card is a person asking for one item; a queue mode would only
        # confuse what they asked for.
        return [_tasks_queue(ctx, args)]
    queues: list[Queue] = []
    if args.queue in ("chores", "both"):
        from nightshift import chores
        queues.append(chores.queue(ctx, args))
    if args.queue in ("tasks", "both"):
        queues.append(_tasks_queue(ctx, args))
    return queues


def _work_the_run(ctx: RunContext, args: argparse.Namespace, *,
                  queues: Callable[[RunContext], list[Queue]] | None = None) -> int:
    """Work each queue in turn on one shared budget, then the run-level phases."""
    deadline = _deadline(args.until, args.max_minutes)
    tally = Tally()
    built = (queues or (lambda c: _queues_for(c, args)))(ctx)
    if args.dry_run:
        return 0

    # The roster, before the first dispatch. The panel draws the run from this and
    # fills each line in as the run reaches it; without it the page could only show
    # what had already finished, and had to guess at the rest by re-reading the
    # board — which cannot see a chore batch at all. One call per queue, in the
    # order they will be worked.
    for queue in built:
        ctx.record.planned([(c.card.id, c.card.title, queue.name) for c in queue.cards])

    code = 0
    for queue in built:
        if tally.stopped:
            # Said out loud: a `both` run that worked chores and then skipped tasks
            # otherwise looks identical to one that found no tasks to work.
            hostconfig._log(f"not starting the {queue.name} queue — {tally.stopped}")
            break
        if queue.cards:
            _work_queue(ctx, args, queue, tally, deadline)
        # Even with no cards: a batch with nothing to dispatch still owes its
        # report, and `close` is where that is written.
        code = queue.landing.close(ctx, queue, tally) or code

    _drain_phase(ctx, args, tally, deadline)
    _stale_phase(ctx, args, tally, deadline)
    return _wrapup(ctx, args, tally) or code


def _tasks_queue(ctx: RunContext, args: argparse.Namespace) -> Queue:
    """The `tasks/` queue: the board's own order, each card's own tier and charter."""
    work, record = ctx.work, ctx.record
    candidates = dispatch.select(work, ctx.capabilities, ctx.bad_schema, forced=args.card)
    ready = [c for c in candidates if c.dispatchable]
    hostconfig._log(f"board: {len(candidates)} card(s) in tasks/, {len(ready)} dispatchable")
    for c in candidates:
        hostconfig._log(f"  {'YES' if c.dispatchable else ' no'}  {c.card.id} — {c.reason}")
    # Why a card did NOT run is a fact only the run knows: nothing moves, so no lane
    # diff can recover it, and five art cards stalled on a capability this host does
    # not have looked exactly like an empty night for a week.
    record.skipped([(c.card.id, c.reason) for c in candidates if not c.dispatchable])
    # And the ones that DID run while over `CARD_COMFORT_BYTES`. A separate field
    # because an oversized card is dispatchable by design, so it is absent from the
    # list above — which left the digest silent about the one case the signal exists
    # for, a card dispatched over and over while it grows.
    record.oversized(dispatch.oversized_entries(candidates))

    if args.card:
        ready, why = dispatch.resolve_named(work, args.card, candidates)
        if why:
            hostconfig._log(f"refusing to run — {why}")
            raise RunRefused(1)
        # Said out loud because `--card` narrows a list the lines above just printed
        # in full; without this, "which one is it actually going to run?" is
        # answered only by re-reading them.
        hostconfig._log(f"narrowed to `{args.card}` — {ready[0].reason}")

    return Queue(name="tasks", cards=ready, landing=Landing())


def _work_queue(ctx: RunContext, args: argparse.Namespace, queue: Queue,
                tally: Tally, deadline: dt.datetime | None) -> None:
    """Work one queue to its end: dispatch each card, land it by the queue's
    policy, and stop the whole run when the run-level accounting says to.

    This is the loop that used to be the night's alone. The chore batch had its
    own, thirty lines long, with no deadline, no budget, no wall handling and no
    breakers — so a usage limit ended a batch where it puts a night to sleep.
    """
    work, base, record = ctx.work, ctx.base, ctx.record
    publish_remote = ctx.publish_remote

    def _stop(reason: str) -> None:
        """End-of-loop reason, said once to both readers.

        The log is for tailing a run in flight; the record is what the
        morning digest reports. One call site so they cannot disagree — the
        first version of this instrumentation left `record.stop` off two of
        the nine `break` paths, and a night that ended for an unrecorded
        reason reads in the digest as a night that simply ran out of cards.
        """
        hostconfig._log(f"stopping — {reason}")
        record.stop(reason)
        # Also the gate on any queue after this one: a run that stopped for a
        # spent budget or a closed window has nothing more to give the next.
        tally.stopped = reason

    def _pipeline(candidate: dispatch.Candidate, model: str) -> outcome.Dispatch:
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
        selector = (queue.test_selector,) if queue.test_selector else ()
        result = dispatch.dispatch(work, candidate.card, base, model,
                          args.card_budget, args.test_timeout, *selector,
                          worker=queue.worker, effort=queue.effort,
                          allow_local=args.local)

        # The review stage (automate-review-step): a card whose gates+tests
        # passed goes to the diff reviewer before it lands. The reviewer's
        # verdict routes it — needs-decision/ or (merge → testing/) — replacing
        # the old unconditional landing in review/. Only a `review` outcome
        # reaches it; a wall/failure/park is untouched. `review_stage` carries
        # the dispatch cost forward and adds its own, so `tally.spent +=` stays once.
        if result.outcome == "review" and queue.landing.reviews_each_card:
            result = review.review_stage(work, candidate.card, result, base,
                                  args.card_budget, args.test_timeout)
        return result

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
        if wall.retry_now:
            # A context wall, not a usage one: the model's own window filled,
            # no plan window closed and nothing reopens on a clock. So none of
            # the accounting below applies — it spends no session, waits out
            # nothing, and the right move is the next dispatch, now.
            #
            # It cannot loop: `_limit_reached` has already given the attempt
            # back *only* if the working tree moved, and files the card as
            # stuck after `NO_PROGRESS_STOP` resumes that changed nothing.
            # That is the brake, and it is the same one a resumed usage wall
            # has always used (Karel, 2026-09-19: *"dispatch Ornith again — if
            # he is making progress"*).
            hostconfig._log(f"context window filled on {card_id} — dispatching again into "
                 f"the kept worktree ({wall.evidence})")
            record.note(f"context window filled on {card_id} — re-dispatched "
                        f"into the kept worktree; no session spent")
            return True
        if wall.spends_a_session:
            tally.walls += 1
            if tally.walls >= args.sessions:
                _stop(f"{tally.walls} session limit(s) used, which is "
                      f"--sessions {args.sessions}")
                return False
        else:
            # A 429 is a hiccup, not the window closing, so it buys a
            # short wait instead of one of the night's sessions. Capped
            # all the same: a hiccup that never clears is indistinguishable
            # from a wall we misread, and must not retry until morning.
            tally.hiccups += 1
            if tally.hiccups > hostconfig.TRANSIENT_RETRIES:
                _stop(f"{tally.hiccups} transient rate limits tonight; that is "
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
        counted = f"{tally.walls} of {args.sessions}" if wall.spends_a_session \
            else f"transient, {tally.hiccups} of {hostconfig.TRANSIENT_RETRIES}"
        then = f"then retrying {card_id}" if retrying \
            else f"then going on from {card_id}, which landed"
        hostconfig._log(f"usage limit reached ({counted}) — sleeping until "
             f"{resume:%H:%M}, {then}")
        record.note(f"usage limit reached ({counted}) — slept until "
                    f"{resume:%H:%M}, {then}")
        # Otherwise `status.json` freezes on whatever phase the wall was met
        # in, its heartbeat ages past `HEARTBEAT_TRUSTED_FOR` a couple of
        # hours into a sleep that can run five, and the panel reports "no run
        # in progress" for a process that is very much still here, just
        # waiting on a clock it already knows. `resume_at` is the fact the
        # panel cannot derive from a stale heartbeat alone.
        hostconfig._status(work, phase="sleeping", card=card_id,
                resume_at=resume.isoformat(timespec="minutes"), since=hostconfig._now())
        if not _sleep_until(resume):
            _stop("kill switch appeared while waiting for the window")
            return False
        return True

    in_a_row = 0         # consecutive failures, the net under limits.detect

    # An index rather than `for candidate in ready`, because a card that met
    # the wall was never actually attempted and has to be retried at the head
    # of the next window. Only a real outcome advances it — and since
    # `needs_fix` joined the outcomes that do not, the loop can now turn
    # several times on one card for a reason other than a wall.
    index = 0
    # Consecutive review-fix retries of the card currently at `index`, and
    # the index that count belongs to. Paired so the counter resets itself
    # whenever the loop moves on, rather than depending on every `index += 1`
    # site remembering to clear it.
    fix_rounds = 0
    fix_rounds_for = -1
    # Every test id that has failed for some *already-settled* card this run.
    # A new failure intersecting this set is the baseline, not the card.
    red_elsewhere: set[str] = set()
    while index < len(queue.cards):
        candidate = queue.cards[index]
        if fix_rounds_for != index:
            fix_rounds_for, fix_rounds = index, 0
        if args.max_cards and tally.done >= args.max_cards:
            _stop(f"reached --max-cards {args.max_cards}")
            break
        if deadline and dt.datetime.now() >= deadline:
            _stop(f"past {deadline:%H:%M}")
            break
        if args.budget and tally.spent >= args.budget:
            _stop(f"run budget ${args.budget:.2f} spent")
            break
        if hostconfig._stop_requested():
            _stop("kill switch appeared mid-run")
            break
        # The money rule, checked before every dispatch and not once for the
        # queue: a fan-out that starts with headroom can lose it partway, and a
        # dispatch cannot be un-started. A night spends `--budget` instead and
        # defines none of this.
        if refusal := queue.landing.may_dispatch(ctx, candidate.card):
            _stop(refusal)
            break

        try:
            model = tiers.resolve(work, queue.tier or candidate.card.tier)
        except tiers.TierError as exc:
            hostconfig._log(f"  skipping {candidate.card.id} — {exc}")
            index += 1
            continue

        # The other direction of the local/GPU mutex (`runtimes.release_for`).
        # A card whose `requires:` names work that needs the box's memory — an
        # art or audio card on the ComfyUI machine — cannot start while a local
        # model is resident holding 13.8 GB of pinned pages. The model is stopped
        # here if this run started it, and the card is skipped if it did not,
        # because the maintainer's own llama-server is not the runner's to kill.
        #
        # Between cards rather than inside `dispatch`, for the same reason the
        # tier check above is: a card that cannot run must not have had a
        # worktree cut or an attempt spent on it. It stays in `tasks/` and the
        # next run picks it up, exactly like a capability this host lacks.
        if not runtimes.release_for(work, candidate.card.requires, log=hostconfig._log):
            hostconfig._log(f"  skipping {candidate.card.id} — the box cannot give it the "
                 f"memory it needs right now")
            # `note`, not `skipped`: that one *replaces* the selection-time list
            # — one call per run, by contract — and appending here would erase
            # the very thing the digest reads to explain a quiet night.
            record.note(f"{candidate.card.id} was not dispatched: a local model "
                        f"is resident, this run did not start it, and its memory "
                        f"could not be released")
            index += 1
            continue

        # `Exception`, never a bare `except`: `KeyboardInterrupt` and
        # `SystemExit` are not this card's failure and must still end the
        # night — a Ctrl-C that filed an `## Error` against whatever card was
        # in flight would be worse than the crash.
        try:
            result = _pipeline(candidate, model)
        except Exception as exc:       # noqa: BLE001 — see `crashed_dispatch`
            result = dispatch.crashed_dispatch(work, candidate.card, exc)
            hostconfig._log(f"  ! {candidate.card.id} — {result.detail}")
        tally.spent += result.cost_usd

        # Before every other outcome: neither a broken harness nor a drifted
        # gate is a verdict on this card, and continuing would walk the rest
        # of the queue spending an attempt on each for a defect none of them
        # caused.
        if result.outcome == "blocked":
            hostconfig._log("  " + queue.landing.settle(ctx, candidate, result, model))
            if result.repo_drift:
                # Reached only after `repair_drift` was tried and failed, so
                # this is no longer "the runner met drift" but "the runner met
                # drift it could not fix" — a much rarer thing, and the only
                # version of it that is still worth a human's night.
                _stop(f"a failure outside {candidate.card.id}'s own diff — repo "
                      f"drift, not this card's fault, and a repair on its branch "
                      f"did not clear it. No attempt was spent and no card was "
                      f"blamed; the card is in {board.BLOCKED_LANE}/. Fix it on "
                      f"`{base}` and re-run. {result.detail}")
            else:
                _stop("the gate harness is broken on this machine, so no card "
                      "can be judged. No attempt was spent and no card was blamed; fix "
                      "it and re-run. The traceback is in `.ai/runs/`")
            break

        if result.outcome == "limited":
            hostconfig._log("  " + queue.landing.settle(ctx, candidate, result, model))
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
                hostconfig._log(f"  {candidate.card.id} moved to {where} while we waited — "
                     f"leaving it alone")
                index += 1
                continue
            candidate.card = fresh
            hostconfig._log("window reopened — resuming")
            continue  # same card, new window

        # A `needs_fix` verdict is not the end of this card's turn.
        #
        # `settle` has just put the card back in `tasks/` with the reviewer's
        # finding on it, and `prepare_worktree` will now continue from its
        # finished branch — so the fix is something this run can do in one
        # small commit. Advancing the index instead deferred it to *the next
        # invocation of the runner*, which is what happened on 2026-08-25:
        # two cards were sent back at 13:13 and 13:34 and simply sat in
        # `tasks/` for the rest of a run that had four more hours of window.
        # Nothing said so, either — "will retry" in the log reads as a
        # promise this loop had already declined to keep.
        #
        # Bounded by `attempt_limit`, which `settle` enforces: once a card is
        # out of attempts it escalates to `needs-decision/` instead of coming
        # back to `tasks/`, and the lane check below is what sees that.
        if result.outcome == "needs_fix":
            hostconfig._log("  " + queue.landing.settle(ctx, candidate, result, model))
            worktree.publish(work, publish_remote, base,
                    trusted_branch=f"ai/{candidate.card.id}")
            # Before the retry, not after: a wall means the window that would
            # have to pay for the fix is shut, and the sleep belongs here for
            # the same reason it does on a landed card.
            if result.wall is not None and not _window_closed(
                    result.wall, candidate.card.id, retrying=True):
                break
            fix_rounds += 1
            # A backstop, not the bound. `settle` already escalates a card
            # that is out of attempts, and that is what normally ends this —
            # but the thing being bounded is an unattended loop that spends
            # money per turn, and it must not depend on another function's
            # arithmetic staying correct to terminate. Counted locally, reset
            # whenever the index moves.
            if fix_rounds >= dispatch.attempt_budget(candidate.card):
                hostconfig._log(f"  {candidate.card.id} has taken {fix_rounds} review-fix "
                     f"rounds this run without settling — moving on rather than "
                     f"spending another; it keeps its lane and its attempts")
                index += 1
                tally.done += 1
                in_a_row = 0
                continue
            # Reloaded for the same reason the `limited` branch reloads: the
            # copy in `ready` predates the attempt `settle` just committed.
            fresh = board.find(work, candidate.card.id)
            if fresh is None or fresh.lane != "tasks":
                # Out of attempts (escalated to `needs-decision/`), or moved
                # by hand. Either way this card is finished for tonight.
                index += 1
                tally.done += 1
                in_a_row = 0
                continue
            candidate.card = fresh
            in_a_row = 0
            hostconfig._log(f"  re-dispatching {candidate.card.id} to apply the "
                 f"reviewer's finding")
            continue  # same card, same run, one fix to apply

        # The same test failing across two *different* cards is not about
        # either of them.
        #
        # This is the order-dependent baseline breakage `_already_failing_on_
        # base` structurally cannot see: on 2026-08-25 a leaked
        # `SCREEN_WIDTH`/`HEIGHT` global made one test fail only when another
        # file had run earlier in the same xdist worker, so re-running it
        # alone on `base` passes. What gave it away was the repetition —
        # `catalog-registry-and-guards`, `credit-telemetry-by-source` and
        # `player-text-explains-design-decisions` all died on
        # `test_range_overlay_tints_exactly_what_validate_accepts`, with the
        # identical assertion and the identical missing tile. Three unrelated
        # branches do not independently break one test.
        #
        # Only the consecutive-failure breaker noticed, at three cards, after
        # one had already reached `failed/`. This notices at two, gives the
        # second card's attempt back, and names the test instead of the card.
        if result.outcome == "failed":
            fresh_red = verify._failing_test_ids(
                telemetry.run_dir(work, candidate.card, candidate.card.attempts) / "junit.xml")
            repeated = sorted(set(fresh_red) & set(red_elsewhere))
            if repeated:
                _stop(f"{', '.join(repeated[:3])} failed for "
                      f"{candidate.card.id} and for a different card earlier "
                      f"in this run. One test breaking on unrelated branches "
                      f"is the baseline, not the cards — most likely state "
                      f"leaking between test files. Fix it on `{base}` and "
                      f"re-run; the evidence is in `.ai/runs/`")
                # Not settled as this card's failure: give the attempt back
                # the way a drifted gate does, so the board does not record a
                # verdict the next run would have to undo.
                card_back = board.find(work, candidate.card.id)
                if card_back is not None:
                    card_back.write({"attempts": str(max(0, card_back.attempts - 1)),
                                     "started": None})
                    board.commit_board(
                        work, f"board: {candidate.card.id} attempt given back — "
                              f"{repeated[0]} is failing across cards")
                break
            red_elsewhere.update(fresh_red)

        index += 1
        tally.done += 1
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
        hostconfig._log("  " + queue.landing.settle(ctx, candidate, result, model))
        # Idempotent, and after every settled card rather than only at the end
        # of the night — a cloud container can be killed between cards, and
        # this is what keeps that from losing already-settled work with it.
        # `blocked`/`limited` don't call this here; both `break` out of the
        # loop and reach the final publish below instead. `trusted_branch`
        # names the card just dispatched — this checkout has full, current
        # knowledge of it, so it skips the divergence check `publish` runs
        # against every other (dormant) card branch.
        worktree.publish(work, publish_remote, base, trusted_branch=f"ai/{candidate.card.id}")
        if args.budget:
            hostconfig._log(f"  spent ${tally.spent:.2f} of ${args.budget:.2f}")
        # Three unrelated cards failing back to back is not about the cards.
        # Most likely it is a wall `limits.detect` did not recognise, and the
        # damage of guessing wrong here (one quiet night) is far below the
        # damage of not guessing (the whole queue spent, then `failed/`).
        if in_a_row >= hostconfig.CONSECUTIVE_FAILURE_STOP:
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


def _drain_phase(ctx: RunContext, args: argparse.Namespace, tally: Tally,
                 deadline: dt.datetime | None) -> None:
    """Conclude the reviews this run left owed, whichever queue left them."""
    work, base, record = ctx.work, ctx.base, ctx.record
    publish_remote = ctx.publish_remote

    # Before the stale sweep and after every queue: conclude any review this run
    # left owed — a card the reviewer walled on, and (since the chore batch and
    # the night became one run) a batch survivor handed to `review/` because the
    # batch itself did not land. Both rest in the same lane, so one pass drains
    # both and neither queue needs its own.
    #
    # `drain.py` records the decision NOT to do this, on three grounds, and
    # names what would have to change: *"there would have to be evidence that
    # cards actually pile up faster than they are looked at."* 2026-08-25 is
    # that evidence — two cards, one night, both finished and green, both
    # parked because the reviewer walled and nothing came back for them. The
    # other two grounds hold up and shape this: the lane is unbounded, so
    # this is capped; and a card in `review/` is already green, so it never
    # competes with `tasks/` for the window — it gets what is left, if
    # anything is.
    #
    # Placed inside the same `window remains` guard as the sweep below, for
    # the same reason, and *before* it: concluding finished work is worth
    # more than a maintenance check. `drain` brings its own kill-switch,
    # money-rule and wall handling, so the phase is the call plus its report.
    # Imported here, not at module scope: `drain` imports `runner` for
    # `review_stage`/`settle`, so the dependency only works in one direction
    # at import time. This is the one call site.
    from nightshift import drain

    drained = None
    if args.drain and not (deadline and dt.datetime.now() >= deadline) \
            and not hostconfig._stop_requested():
        owed = [c.id for c in drain.waiting(work)
                if not drain.skip_reason(work, base, c)]
        if owed:
            hostconfig._log(f"draining {len(owed)} card(s) whose review this run left owed: "
                 f"{', '.join(owed[:hostconfig.DRAIN_CAP])}"
                 + (f" (+{len(owed) - hostconfig.DRAIN_CAP} beyond tonight's cap)"
                    if len(owed) > hostconfig.DRAIN_CAP else ""))
            drained = drain.drain(work, base, limit=hostconfig.DRAIN_CAP,
                                  card_budget=args.card_budget,
                                  test_timeout=args.test_timeout)
            for line in drain.describe(drained):
                hostconfig._log(line)
            tally.spent += drained.cost_usd
            record.note(f"drained {len(owed)} review-owed card(s): "
                        + "; ".join(f"{o.card_id} {o.state}"
                                    for o in drained.outcomes))
            worktree.publish(work, publish_remote, base)


def _stale_phase(ctx: RunContext, args: argparse.Namespace, tally: Tally,
                 deadline: dt.datetime | None) -> None:
    """Spend whatever window is left on staleness — never before the cards."""
    work, record = ctx.work, ctx.record

    # After the cards, spend whatever window is left on staleness — never
    # before, so a card that produces real work always wins the budget over
    # a maintenance check. Only if a window remains: a run that already hit a
    # wall or the deadline has nothing left to give the sweep.
    if args.stale and not (deadline and dt.datetime.now() >= deadline) \
            and not hostconfig._stop_requested():
        try:
            model = tiers.resolve(work, "worker")
            checked, verified, carded = stale.stale_phase(
                work, args.stale, model, deadline, args.card_budget,
                args.test_timeout, record)
            hostconfig._log(f"stale sweep: {checked} checked, {verified} verified, {carded} carded")
        except tiers.TierError as exc:
            hostconfig._log(f"stale sweep skipped — {exc}")
            record.note(f"stale sweep skipped — {exc}")


def _wrapup(ctx: RunContext, args: argparse.Namespace, tally: Tally) -> int:
    """Close the record, commit the board, push, and say how the run ended."""
    work, base, record = ctx.work, ctx.base, ctx.record
    publish_remote = ctx.publish_remote

    # Closed before the wrap-up commit, never after, so the record on disk
    # already says how the night ended by the time anything reads it —
    # Command Center reads these records live, and a record still open while
    # its run commits and pushes reads as a night in flight.
    record.finish(cost_usd=tally.spent, walls=tally.walls,
                  dispatched=tally.done)

    # `wrapup` covers the board commit and the push below. It is a real phase,
    # not a formality: `publish` is a network round-trip, and a status that
    # said `finished` before it returned would show the panel a run that had
    # not yet pushed. (Was `digest` until the digest was removed — the token is
    # read by `panel._PHASE_DONE` and `freshness`, so all three moved together.)
    hostconfig._status(work, phase="wrapup", since=hostconfig._now())
    # The board's own commit, and its only job: the lane moves under `Board/`
    # and every `board.GENERATED_VIEWS` file. The subject is plain prose —
    # nothing parses it back. It used to double as the digest's read-window
    # marker, which is what `--append-digest` existed to suppress; with the
    # digest gone there is no baseline to advance or withhold, so both went.
    board.commit_board(work, f"board: {dt.date.today().isoformat()} run")
    # The final sweep, and the one publish reached by every early `break` above
    # — `blocked`, a wall, the deadline, the kill switch all fall through here.
    worktree.publish(work, publish_remote, base)
    hostconfig._log(f"run complete — {tally.done} card(s) dispatched, "
         f"{tally.walls} session limit(s) hit, ${tally.spent:.2f} equivalent")
    hostconfig._status(work, phase="finished", cards_dispatched=tally.done,
            session_limits=tally.walls,
            spent_usd=round(tally.spent, 2), since=hostconfig._now())
    return 0


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
    base = hostconfig.default_base(root)
    parser.add_argument("--base", default=base, help=f"branch to build on (default {base})")
    parser.add_argument("--queue", choices=("tasks", "chores", "both"), default="both",
                        help="which work this run takes on. `tasks` is the board's own "
                             "queue, each card reviewed and merged on its own; `chores` "
                             "is the `kind: chore` batch, dispatched cheaply and merged "
                             "as one unit; `both` works chores first and then tasks, on "
                             "one budget and one set of sessions (default). Ignored with "
                             "`--card`, which names one item.")
    parser.add_argument("--chore-limit", type=int, default=0,
                        help="chores per batch; 0 takes `chores.DEFAULT_BATCH`")
    parser.add_argument("--allow-paid", action="store_true",
                        help="proceed even if a dispatch would draw on paid credits "
                             "(the explicit 'continue nevertheless' decision). Read "
                             "by the chore queue, which asks `usage` before every "
                             "item; a task queue bounds itself with `--budget`.")
    parser.add_argument("--card", help="dispatch only this card id. Naming a card is an "
                                       "explicit human request, so `unattended: false` and "
                                       "the attempt limit are waived for it; "
                                       "`requires:`, a missing charter and a broken schema "
                                       "are not. Exits non-zero with a reason if it cannot run.")
    parser.add_argument("--max-cards", type=int, default=0, help="stop after N dispatches")
    parser.add_argument("--until", help="stop dispatching at this local time, HH:MM")
    parser.add_argument("--max-minutes", type=int, help="stop dispatching after N minutes")
    parser.add_argument("--sessions", type=int, default=hostconfig.DEFAULT_SESSIONS,
                        help="how many usage-limit windows the night may spend. "
                             "1 (default) works until the session limit is reached and "
                             "stops; 2 sleeps through the first reset and stops at the "
                             "second wall. A weekly limit always stops")
    parser.add_argument("--budget", type=float, default=hostconfig.DEFAULT_RUN_BUDGET_USD,
                        help="optional USD cap for the whole run. 0 (default) means none — "
                             "on a subscription plan the figure is API-equivalent rather "
                             "than money spent; use --sessions")
    parser.add_argument("--card-budget", type=float, default=hostconfig.DEFAULT_CARD_BUDGET_USD,
                        help="optional USD cap handed to each worker process; 0 (default) "
                             "passes no cap at all")
    parser.add_argument("--test-timeout", type=int, default=600,
                        help="seconds allowed for the test suite (~2 min today)")
    parser.add_argument("--no-local", dest="local", action="store_false",
                        help="run every card on cloud for this run, even on a machine "
                             "that declares a local model and even for a charter in its "
                             "allowlist. The narrowest of three independent off "
                             "switches: this one covers a single run, the Command "
                             "Center's `Use the local model` tick covers a sitting, and "
                             f"deleting the `{runtimes.HOST_KEY}` block from "
                             f"{hostconfig.HOSTS_FILE.as_posix()} covers the machine permanently. "
                             "Each is sufficient alone; "
                             "none needs either of the others turned off first. Nothing "
                             "is lost by passing it — cloud is the ground state, so this "
                             "asks for the behaviour every card had before the runtime "
                             "axis existed")
    parser.add_argument("--no-drain", dest="drain", action="store_false",
                        help=f"skip the end-of-night pass that concludes any review this "
                             f"run left owed (up to {hostconfig.DRAIN_CAP} cards, from whatever "
                             f"window is left after the cards). On by default since "
                             f"2026-08-25: without it a card whose reviewer walled sits "
                             f"in review/ until someone runs `python -m nightshift.drain` "
                             f"by hand, which is how two finished cards spent a night "
                             f"looking like queued work")
    parser.add_argument("--stale", type=int, default=0, nargs="?", const=10_000,
                        metavar="N",
                        help="after the cards, run the Tier-2 staleness sweep on the N "
                             "highest-churn docs, spending only leftover window. Bare "
                             "--stale runs it dry (every doc that changed since it was last "
                             "verified); --stale 3 caps it at three; 0 (default) skips it. "
                             "A doc is ledgered only on a complete verdict, and findings "
                             "become one fix-card per doc — the runner never edits a doc")
    return parser


def main(argv: list[str] | None = None) -> int:
    root = hostconfig.repo_root()
    args = _parser(root).parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.status:
        return print_status(root)
    return run(root, args)


if __name__ == "__main__":
    raise SystemExit(main())
