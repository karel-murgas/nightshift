#!/usr/bin/env python3
"""The Command Center — a launcher, a registry and a tail, never a chat client.

`.claude/plans/dispatch-cost-and-control-panel.md` (a Project Tigress doc) §3.4 is the design
session this implements; the plan itself is project-side because it is that project's own
programme, but the panel is framework — any project with a board can run one.

**Every verb already exists as a CLI command**, so **the server owns no logic**. Every POST
that changes anything goes through `run_command`/`spawn_background`, which shell out to
`python -m nightshift.<module> <args>` — the same command a person would type. This mirrors
the framework's own precedent: `boardcmd`'s test suite states outright that "the panel will
not import this module — it will run it", and this module holds to that for every board- or
dispatch-shaped verb. Reading board/usage/freshness state to *render* a page is not "logic"
in that sense — `drain.py` and `ingest.py` already import `board`/`usage` directly for the
same reason — so GET handlers read via the ordinary Python API and only POST handlers shell
out.

**Pages by whose move it is, server-rendered, one URL each:** `/queue` (ready work),
`/capture` (inbox and ideas), `/you` (decide, play, blocked, review), `/running` (the
current run; `/history` one click away) and `/system`. The old addresses redirect
(`OLD_PAGES`). Every list is `_item` rows; a card appears on one page; anything that starts
an agent opens the launch dialog, which holds the account, tier, local-model and
paid-override choices (`command-center-restructure`).

**Two ways to be on a different account, and the panel owns neither of them.** A repo may
declare `[[accounts]]`, each naming its own `CLAUDE_CONFIG_DIR`; selecting one sets that
variable for this process's dispatch subprocesses only (`worker._worker_env` inherits the
environment wholesale, so nothing downstream needs to know a selector exists). But the
ordinary gesture is `claude auth login` against the *one* config directory you already have —
settings, history and MCP servers stay put and only the signed-in identity swaps. That is a
browser flow, so the panel launches it and stops; what it owns is **reading who is logged in,
on every page load and never from a cache**, so the rail cannot name the wrong account. The
meter cache is keyed on the account for the same reason.

**`_ACCOUNT` is process-wide and reset on restart** — deliberately not persisted, the same
shape as `usage`'s `--allow-paid` (`feedback_account_dispatch`): a "spend on this account"
decision that outlived the click that made it is the foot-gun the whole rule exists to
prevent. An account carrying `dispatch: never`, **or one whose live `hasExtraUsageEnabled`
says API spend is on**, is refused server-side (`_guard_dispatch_account`), not merely hidden
in the UI: a crafted request must fail exactly where a browser click would have declined to
send one. A human may waive that per request and never persistently, which is the rule
satisfied rather than bypassed — the property protected is *who decides*.

**Framework freshness fetches only on an explicit Refresh, never on page load** (§3.4's own
rule) — `read_rail` always calls `freshness.read(fetch=False)`; only `/api/freshness/refresh`
passes `fetch=True`.

**The money rule is left exactly where it is.** `chores`, `ingest` and `drain` each check
`usage` before they spend, and they take `--allow-paid`; `runner` does not consult `usage` at
all — a night is protected reactively by `limits.py` after a wall, by design. So the override
checkbox is wired to the commands that can honour it and is inert for the night, which is
stated on the control rather than hidden behind it. Adding a money check here instead would be
this server growing the one kind of logic it must not have.

No LLM anywhere in this module. Reading board/usage state is arithmetic and file I/O; every
LLM-touching action is a subprocess this module starts and does not wait on.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import urlopen

from nightshift import (board, boardhealth, branches, chores, corrections, decide, drain,
                        freshness, git, ingest, init, jobs, manifest, preflight, run_record,
                        runtimes, textio, tiers, update, usage, worker_prompt)
from nightshift.manifest import ManifestError, find_root
from nightshift.hostconfig import (
    RUNS,
    STATUS_FILE,
    STOP_FILE,
    current_branch,
    default_base,
    host_capabilities,
    host_setting,
)
from nightshift.startup import claude_binary, schema_violations
from nightshift.dispatch import Candidate, attempt_limit, card_bytes, oversize_note
from nightshift.review import branch_has_commits
from nightshift.telemetry import read_telemetry
# Private, and imported rather than reimplemented on purpose: "is this pid still
# alive" carries a Windows-specific subtlety (`tasklist`, not a signal) that
# `print_status` already got right, and a second copy here would be the same
# question answered twice. It is the one thing standing between a heartbeat file
# and the claim that a run is live.
from nightshift.hostconfig import _pid_alive
from nightshift.dispatch import select as select_candidates

DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "panel_static"
TEMPLATE = STATIC_DIR / "app.html"

#: The rail, in order — pages by whose move it is.
PAGES = ("queue", "capture", "you", "running", "system")
PAGE_LABELS = {"queue": "Queue", "capture": "Capture", "you": "You",
               "running": "Running", "system": "System"}

#: The run's phases, as the pills across the status rail: `(status.json value, label)`.
#: `starting` and `checker` fold onto `worker` because they are the same span of the run
#: from the outside. `merge` is last and is never itself a live phase — `settle` merges
#: without a heartbeat — so it lights only once the run has moved past `review`, which is
#: honest about what is known rather than inventing a phase the runner does not report.
PHASE_STEPS: tuple[tuple[str, str], ...] = (
    ("worker", "dispatch"), ("gates", "gates"), ("pytest", "tests"),
    ("review", "review"), ("merge", "merge"),
)
_PHASE_ALIASES = {"starting": "worker", "checker": "worker"}
#: Phases that mean the run is past every step above. `wrapup` is the board commit
#: and the push; it was called `digest` until the digest was removed, and the token is
#: written by `hostconfig._status` and also read by `freshness`, so all three moved together.
_PHASE_DONE = frozenset({"wrapup", "finished"})


# --------------------------------------------------------------------------
# The account in force — process-wide, in-memory, never persisted.
# --------------------------------------------------------------------------


@dataclass
class AccountState:
    label: str = ""
    config_dir: str = ""
    dispatch: str = "always"


_ACCOUNT = AccountState()


class PanelError(RuntimeError):
    """A request was refused before any command ran. Carries the sentence to render."""


def _accounts(root: Path) -> tuple[manifest.Account, ...]:
    try:
        return manifest.load(root).accounts
    except ManifestError:
        return ()


def select_account(root: Path, label: str) -> AccountState:
    """Set the account every subsequent dispatch subprocess runs under.

    `label=""` returns to the ambient account (whatever `CLAUDE_CONFIG_DIR` this
    server process itself inherited, or `~/.claude` if unset) — not a fourth
    hardcoded default, just "stop overriding".
    """
    global _ACCOUNT
    # A different account has a different credentials path, so `usage.read_cached`
    # already keys its cache on that and needs no help invalidating here.
    if not label:
        _ACCOUNT = AccountState()
        return _ACCOUNT
    match = next((a for a in _accounts(root) if a.label == label), None)
    if match is None:
        known = ", ".join(a.label for a in _accounts(root)) or "(none configured)"
        raise PanelError(f"no account named {label!r} in [[accounts]] — known: {known}")
    _ACCOUNT = AccountState(label=match.label, config_dir=match.config_dir,
                            dispatch=match.dispatch)
    return _ACCOUNT


def _dispatch_env() -> dict[str, str] | None:
    """The environment a dispatch subprocess runs under.

    `None` means "inherit exactly what this server process has" — the ordinary case
    with no account selected. Selecting an account adds exactly one variable on top
    of the inherited environment, never a replacement of it.
    """
    if not _ACCOUNT.config_dir:
        return None
    return {**os.environ, "CLAUDE_CONFIG_DIR": _ACCOUNT.config_dir}


def _account_paths(state: AccountState) -> tuple[Path | None, Path | None]:
    if not state.config_dir:
        return None, None
    base = Path(state.config_dir).expanduser()
    return base / ".credentials.json", base / ".claude.json"


def _guard_dispatch_account(waived: bool = False) -> None:
    """Refuse a dispatch on an account marked `dispatch: never`, or one whose
    live `hasExtraUsageEnabled` says API spend is on — unless a human waived it.

    Checked here, not only left to the UI hiding the button — a crafted POST must
    fail exactly where a real click would have declined to send one at all.

    **Config may only ever exclude, never promote** (`feedback_account_dispatch`):
    `[[accounts]]` may be missing an entry for the account actually in force — it
    was, on the very first live run of this module — so the config check alone
    would have let automated work reach the one account the whole rule exists to
    protect. The live identity read is the veto config forgot to mark, so it is
    checked unconditionally, not only when `[[accounts]]` names the account.

    **`waived` is the human saying so, and it is the whole point of the rule.**
    The exclusion is *"a default, enforced against tooling, waived only by a
    human"* — Karel, 2026-08-14: *"It may be legitimate ... to allow automated
    work for dollars on purpose. But the user should decide if the exception will
    be given."* So this is not a hole in the veto; a veto with no human override
    would be the tool deciding, which is the thing forbidden. It arrives per
    request from the override checkbox and is **never persisted** — the same
    shape as `--allow-paid`, for the reason that file gives: a stored "this
    account is fine now" outlives the reason it was true.
    """
    if waived:
        return
    if _ACCOUNT.dispatch == "never":
        raise PanelError(
            f"the account {_ACCOUNT.label!r} is configured `dispatch: never` in "
            f"[[accounts]] — this panel will not dispatch work to it. Select a "
            f"different account, or tick the override to allow it this once."
        )
    _, identity_path = _account_paths(_ACCOUNT)
    identity = usage.read_identity(identity_path)
    if identity.fetched and identity.has_extra_usage_enabled:
        raise PanelError(
            f"the account in force ({identity.email or '(no email on file)'}) has API "
            f"spend enabled (`hasExtraUsageEnabled`) — this panel will not dispatch "
            f"automated work to it by default. Select a different account, or tick "
            f"the override to allow it this once."
        )


# --------------------------------------------------------------------------
# The tier in force — process-wide, in-memory, never persisted. Same shape as
# the account above, and for the same reason: it is a property of this sitting
# at the panel, not a setting the repo should wake up carrying.
# --------------------------------------------------------------------------


@dataclass
class TierChoice:
    """What tier an interactive session this panel opens should run at.

    **A tier, never a model.** §16's rule is that the `tier: → model` binding lives
    in exactly one document and that no caller names a model. A dropdown offering
    `opus` and `sonnet` would be a second binding, written in a Python file, going
    stale the day the block is edited — so the control offers the tiers §16
    declares and *shows* what each currently resolves to. The label moves when §16
    moves; nothing here knows a model name.

    Two fields rather than one, because Karel asked one control for two behaviours
    (2026-08-19): *"I want the cards with predefined tier to keep that. Other than
    that one dropdown in status [rail] is nice. Dropdown should have an override
    checkbox — with it on, it overrides card. With it off, card's tier is picked if
    present."* So `tier` is the choice and `override` is whether the choice outranks
    a card that already declares one.

    A card with **no** `tier:` takes the choice either way. There is nothing for it
    to outrank, and the alternative is the CLI's default model picked by nobody —
    which is the state this control exists to end (Karel: *"we need to utilize
    sonnet everywhere where it is enough"*).

    **Nothing here reaches the runner.** A dispatched card runs at the tier its
    frontmatter declares, checked by `hooks/tier_guard.py`; this governs only the
    sessions the panel opens for a person at the keyboard, which is exactly the
    population `unattended: false` describes.

    **Why the local-model toggle beside it *does* reach the runner, and is not a
    bug.** `LocalChoice` is the panel's other dispatch-shaped control and it
    deliberately breaks the rule above. Both halves of the reason matter
    (`04_local_runtime.md` §6, and the card `local-runtime-axis`):

    * *What the rule protects.* §16 argues the worker tier is **the measuring
      instrument for card quality** — if a card that failed at `worker` could have
      been silently run at `opus` from this page, no run record would mean
      anything. A panel that could force a tier onto a dispatched card would
      destroy that. So `tier` stays sealed off, and this docstring's first
      paragraph still holds in full.
    * *Why runtime does not threaten it.* The runtime toggle **does not change the
      tier**. A card dispatched local runs at exactly the tier its frontmatter
      declares, judged by exactly the same gates and the same test slice; what
      moves is which machine executes it. The measuring instrument is untouched.

    And it is safe in the direction that matters because it is **asymmetric**: the
    toggle may always force *cloud*, and may only permit *local* for a charter
    already in the host's allowlist. Off is safe by construction — cloud is the
    ground state, so turning it off asks for the behaviour every card had before
    the axis existed. On cannot reach a charter `00_architecture.md` §7 has not
    admitted, because `runtimes.resolve` checks the allowlist *after* it checks
    this toggle, and no code path skips it.

    So: two controls, two different rules, and the difference is not an oversight.
    A control that changes *how much judgment* a dispatched card gets is refused;
    a control that changes *where* it executes is allowed, because only the first
    one can make a run record lie.
    """

    tier: str = ""
    override: bool = False


_TIER = TierChoice()


def tier_menu(root: Path) -> list[tuple[str, str]]:
    """`[(tier, model alias)]` as §16's block declares them, in `KNOWN_TIERS` order.

    Read per render rather than cached: the binding lives in a document a session
    may edit between two page loads, and a dropdown offering a model the table no
    longer names is precisely the drift `tiers` exists to refuse. An unreadable
    binding yields an empty menu rather than an exception — the rail must still
    render in a repo whose `binding_doc` is missing, which is every repo that has
    not been installed yet.
    """
    try:
        binding = tiers.binding(root)
    except tiers.TierError:
        return []
    return [(name, binding[name]) for name in tiers.KNOWN_TIERS if name in binding]


def select_tier(root: Path, tier: str, override: bool) -> TierChoice:
    """Set the tier every subsequent interactive session opens at.

    `tier=""` means "no choice" — a card's own tier still applies, and a session
    with no card gets the CLI's default. Validated against the binding for the same
    reason `select_account` validates its label: a typo that silently selected
    nothing would be indistinguishable from the control not working.
    """
    global _TIER
    known = {name for name, _ in tier_menu(root)}
    if tier and tier not in known:
        listed = ", ".join(sorted(known)) or "(the tier binding could not be read)"
        raise PanelError(f"no tier named {tier!r} — declared: {listed}")
    _TIER = TierChoice(tier=tier, override=bool(override))
    return _TIER


@dataclass
class LocalChoice:
    """Whether dispatches from this sitting may use the machine's local model.

    The second of `04_local_runtime.md` §6's three off switches — the revocable,
    this-sitting one, between the permanent per-machine one (no `local_model`
    block in `.ai/hosts.json`) and the per-run one (`--no-local`). Each is
    sufficient alone and none needs the others.

    **One field, and it can only ever subtract.** `on` defaults to `True` and that
    is not "local by default": it means *this control is not the thing saying no*.
    A box with no host block, a charter outside its allowlist and a server that is
    down all still resolve to cloud with this `True`, because `runtimes.resolve`
    checks them independently. So the honest reading of the field is "the
    maintainer has not switched it off", which is why the default is `True` and
    why turning it on is not a grant of anything.

    Process-wide and never persisted, exactly like `_ACCOUNT` and `_TIER`: a
    setting the repo woke up carrying would be a decision nobody made this
    morning, and this one reaches dispatched work.

    Unlike `TierChoice`, this **does** reach the runner — see that class's
    docstring for why the two differ and why neither is a bug.
    """

    on: bool = True


_LOCAL = LocalChoice()


def select_local(on: bool) -> LocalChoice:
    """Turn the local runtime off (or back on) for this sitting.

    No validation and no root, unlike `select_tier`: there is no value here that
    could be a typo. `False` is always meaningful — every machine can decline to
    use a local model — and `True` is a request that `runtimes.resolve` is still
    free to refuse for any of its own reasons.
    """
    global _LOCAL
    _LOCAL = LocalChoice(on=bool(on))
    return _LOCAL


def local_enabled() -> bool:
    """Whether this sitting permits local dispatch. The panel's half of
    `runtimes.resolve`'s `enabled` argument."""
    return _LOCAL.on


def effective_runtime(root: Path, agent: str) -> str:
    """The runtime a card dispatched *right now* with `agent` would run on.

    `effective_tier`'s counterpart, and the single place the toggle is applied —
    so a row's chip and the dispatch it describes cannot disagree, which is the
    same rule the tier chip follows.

    **Probed with `probe=False`.** This renders once per card row, and opening a
    socket per row would make every page load wait on the model server. The chip
    therefore answers the *declared* question — "is this card eligible" — and the
    real dispatch re-asks it with the probe. A chip saying `local` on a box whose
    server is down is not a lie the page can avoid without costing every page load
    a round trip; the run log says what actually happened.
    """
    return runtimes.resolve(root, agent, enabled=local_enabled(), probe=False)


def effective_tier(card_tier: str = "") -> str:
    """The tier a session opened *right now* would run at, for a card declaring
    `card_tier` (empty for a session that has no card).

    The single place `TierChoice`'s two-field rule is applied. Every caller that
    launches goes through it and so does the chip on every row, so the row and the
    button beside it cannot disagree — a panel that says `worker` and spends `opus`
    is worse than one that offered no choice at all.
    """
    if _TIER.override:
        return _TIER.tier
    return card_tier or _TIER.tier


# --------------------------------------------------------------------------
# The one command-running helper. Every write/dispatch verb goes through one
# of these — never a re-implementation of board or runner logic.
# --------------------------------------------------------------------------


def _module_argv(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", f"nightshift.{module}", *args]


def run_command(module: str, args: list[str], root: Path, *,
                timeout: int = 120) -> subprocess.CompletedProcess:
    """Run `python -m nightshift.<module> <args>` to completion and capture it.

    For the short verbs — a board write, a plan, a dry-run — that return in well
    under `timeout`. Nothing here parses or re-derives what the command does; the
    command's own stdout/stderr is the answer, exactly as a terminal would show it.
    """
    return subprocess.run(
        _module_argv(module, *args), cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=_dispatch_env(),
    )


def spawn_background(module: str, args: list[str], root: Path) -> int:
    """Start `python -m nightshift.<module> <args>` detached and return its pid.

    For a verb that may dispatch an actual LLM session and run for minutes. The
    HTTP request returns immediately; progress is read back from `jobs`, never
    from this function holding the connection open. Detached on purpose: closing
    the panel must not kill a night.

    **It used to send the command's output to `DEVNULL`, and that was the whole
    bug.** A verb that succeeded and a verb that died on its first line were
    indistinguishable from the browser, because the only thing either produced was
    a toast; `jobs.py`'s docstring carries the measured case. Every background verb
    goes through here or through `spawn_sequence`, so keeping the record in these
    two functions is what makes the property true of *all* of them rather than of
    whichever ones someone remembered.
    """
    return spawn_job(root, module, _module_argv(module, *args))


def spawn_sequence(card_ids: list[str], root: Path, *, sessions: int = 0) -> int:
    """Dispatch each card in turn, in the order given, detached from this process.

    This is what "run the ticked cards" means, and it deliberately owns **no
    dispatch logic**: it runs `python -m nightshift.panel --dispatch-cards a b c`,
    whose whole body is a loop calling `runner --card` — the runner's own per-card
    path, once per card, in the panel's order. A subset could not otherwise be
    expressed: `runner` with no arguments takes the whole queue, and there is no
    flag for "these five".

    Its own module is the sequencer for the same reason the verbs are commands:
    the thing the button does can be typed into a terminal and watched.

    `sessions` is a budget for the sequence as a whole, spent across however
    many of these N per-card processes actually meet a wall — see
    `dispatch_cards` for how that is tracked across processes that each only
    know their own share of it.
    """
    argv = [sys.executable, "-m", "nightshift.panel", "--dispatch-cards", *card_ids]
    if sessions:
        argv += ["--sessions", str(sessions)]
    return spawn_job(root, "run", argv)


def spawn_job(root: Path, label: str, argv: list[str]) -> int:
    """Record `argv`, start it detached through the job wrapper, return the pid.

    Three things happen in this order and the order is the point:

    1. **The record is written first**, so a spawn that fails outright still
       leaves a row saying what was meant to run.
    2. **The log file is opened here** and handed to the wrapper as its stdout and
       stderr. The wrapper's child inherits them, so the command's own output —
       and the wrapper's own line about how it exited — land in one file, in
       order, with nothing in between that could drop them.
    3. **The wrapper, not the command, is what gets spawned.** It waits, and
       writes the exit code back. This process cannot: the panel is closable by
       design, and a status inferred from a pid is the recycled-pid trap
       `run_is_live` documents.
    """
    job = jobs.record(root, label, argv)
    path = jobs.log_path(root, job.ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = [sys.executable, "-m", "nightshift.jobs", "--root", str(root),
               "--run", job.ident]
    with path.open("a", encoding="utf-8", newline="") as sink:
        proc = subprocess.Popen(
            wrapper, cwd=root, env=_dispatch_env(),
            stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
            **_detached(),
        )
    return proc.pid


def _detached() -> dict[str, object]:
    """The platform's "outlive this process" flags for `Popen`."""
    if os.name == "nt":
        return {"creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def dispatch_cards(card_ids: list[str], root: Path, *, sessions: int = 0) -> int:
    """`--dispatch-cards`: run `runner --card <id>` for each id, in order.

    Stops at the first card whose run exits non-zero — a night that could not
    finish card 2 has no business starting card 3 on the same assumption, and the
    runner's own exit code is the only judgment consulted.

    **A `needs_fix` verdict redispatches the same card immediately**, before
    moving on to the next id in the queue, instead of waiting for it to come
    up again on its own (`retry-needs-fix-immediately`) — the queue is a fixed
    list of ids given once, so without this a card that needed a fix was
    never tried again this sequence at all, and 2026-08-22 shipped a morning
    with three of them still sitting exactly where the reviewer left them.
    Backoff and the attempt limit are already waived for a named `--card`
    dispatch (`select`'s own doc), so nothing here has to reimplement either —
    it only has to ask the board, after each attempt, whether the card is
    still in `tasks/` under its attempt limit. That question is what tells
    "will retry" apart from the rarer case where the card's last attempt just
    escalated it to `needs-decision/`, which the outcome string alone cannot:
    `run_record` records that case as the bare string `needs_fix` too.

    **`sessions` is a budget for the sequence, not for each card in it**
    (`sequence-sessions-reset-per-card`, 2026-08-22: a ten-card queue with
    `--sessions 2` slept through a usage-limit reset *per card that hit one*,
    twice, burning most of a 9.5-hour night on two five-hour sleeps a real
    per-night cap would have stopped after the first). Each card gets only
    what is left of the budget — its own record says how many windows *it*
    actually slept through, which comes out of what the next card is handed —
    and once the budget is spent the sequence stops there rather than letting
    the next card start a fresh one.

    **Every card's dispatch is folded into one record for the sequence**
    (`sequence-cards-vanish-into-their-own-record`), because each `runner
    --card` invocation is its own process that opens and closes its own
    record — ten cards otherwise leave nine of them visible only as a
    one-line "N dispatched" in `/run`'s "Earlier runs" list, with no outcome,
    no matter how many needed a fix. The sequence's own record is written
    last, so it is the one `_latest_record` (and therefore `/run`) actually
    shows — with a row for every card the sequence ran, in order, need-a-fix
    included.
    """
    remaining = sessions
    accumulated: list[dict] = []
    total_cost = 0.0
    total_walls = 0
    started_at = dt.datetime.now().replace(microsecond=0).isoformat()
    code = 0
    stop_reason = ""
    halt = False
    for card_id in card_ids:
        retry = 0
        while True:
            extra = ["--sessions", str(remaining)] if sessions else []
            label = f"panel: dispatching {card_id}"
            if retry:
                label += f" (retry {retry}, right after its own needs_fix)"
            if sessions:
                label += f" (sessions budget: {remaining} left)"
            print(label, flush=True)
            result = subprocess.run(_module_argv("runner", "--card", card_id, *extra),
                                    cwd=root, check=False)
            # The card's own process already closed its own record by the time its
            # `subprocess.run` returns, and nothing else here writes one — so this
            # is unambiguously that card's record, not a stale one from earlier in
            # the sequence or a race with whatever comes next.
            card_record = _latest_record(root)
            entries = card_record.get("dispatched") or []
            accumulated.extend(entries)
            total_cost += float(card_record.get("cost_usd") or 0.0)
            total_walls += int(card_record.get("walls") or 0)
            if sessions:
                remaining = max(0, remaining - int(card_record.get("walls") or 0))
            if result.returncode != 0:
                stop_reason = f"{card_id} exited {result.returncode}"
                print(f"panel: {stop_reason} — stopping the sequence", flush=True)
                code = result.returncode
                halt = True
                break
            # `needs_fix` sends the card back to `tasks/` for exactly this reason
            # (`retry-needs-fix-immediately`, Karel 2026-08-22: "it should be
            # dispatched again right after the review... so it is actually
            # finished in the morning") — waiting for the queue meant it only got
            # a second look if the sequence happened to name it again later, or
            # never. Re-checking the board rather than trusting the outcome string
            # alone is what tells "will retry" apart from the rarer case where
            # `settle` has already spent the card's last attempt and moved it to
            # `needs-decision/` — the outcome is recorded as `needs_fix` there too
            # (`run_record`'s own note on why), and a card no longer in `tasks/`
            # would refuse the next `--card` dispatch outright.
            outcome = str((entries[-1] if entries else {}).get("outcome") or "")
            fresh = board.find(root, card_id)
            still_retriable = (outcome == "needs_fix" and fresh is not None
                              and fresh.lane == "tasks"
                              and fresh.attempts < attempt_limit(fresh))
            if not still_retriable:
                break
            if sessions and remaining <= 0:
                stop_reason = f"sessions budget ({sessions}) spent, mid-retry on {card_id}"
                print(f"panel: {stop_reason} — stopping the sequence for tonight", flush=True)
                halt = True
                break
            retry += 1
        if halt:
            break
    seq = run_record.start(root, kind="run", label="sequence", host=socket.gethostname())
    seq.data["started"] = started_at
    seq.set_dispatched(accumulated)
    if stop_reason:
        seq.stop(stop_reason)
    seq.finish(cost_usd=total_cost, walls=total_walls, dispatched=len(accumulated))
    return code


def open_terminal(root: Path, *command: str) -> None:
    """Hand `command` to a new, visible console window rather than running it here.

    This is the "talk to this one" / "launch triage" gesture (§3.4: *"it is a
    launcher, a registry and a tail — never a chat client"*). `claude --resume
    <session_id>` is the concrete case `runner.py` already supports (the session id
    is captured per attempt); triage is `claude --agent triage` — deliberately
    interactive, because triage is investigative work a person drives, not a
    one-shot `-p` dispatch this module could run for them.

    **`claude` is resolved here, not left to the new window's PATH.** Every other
    dispatch path in the framework goes through `startup.claude_binary()`, which
    knows the CLI may live in `%USERPROFILE%\\.local\\bin` without being on PATH —
    and on the box this was found on (2026-08-17) that is exactly where it lives.
    This function was the one place that passed the bare name and trusted `cmd` to
    find it, so every launcher button on the page — Work on this, Talk, Triage,
    Setup, Switch account — opened a console window reading `'claude' is not
    recognized`, while the runner dispatched the same CLI fine. Failing here with a
    sentence the page can render beats spawning a window whose only content is that
    error.

    **`env=_dispatch_env()`, for the same reason `spawn_background` passes it.** The
    account selector sets one variable — `CLAUDE_CONFIG_DIR` — and a launcher that
    spawns with the server's own inherited environment ignores the selection
    silently: the window opens on the ambient account, logged in as whoever, and
    nothing on the page says so. Every other spending path here answers to the
    selector; this one did not, which made the dropdown a lie for five buttons.

    **No `cmd.exe` in the Windows path, because a prompt has newlines in it.** This
    used to spawn `cmd /c start "Command Center" cmd /k <argv>`, which put the whole
    invocation through two rounds of `cmd` parsing — and `cmd` treats a newline
    inside an argument as a command separator. Measured 2026-08-17, from the session
    transcript: a card prompt arrived as its **first line only**, so the session was
    told "Work this note from the board's inbox" with no note named, went and picked
    one off the board itself, and worked the wrong thing under the right session
    name. A direct `CreateProcess` with `CREATE_NEW_CONSOLE` gets the same detached,
    visible window with no shell in between; a probe writing a six-line argument
    through both paths returns one line through `cmd` and six through this one.

    The trade-off, stated because it is a real loss: `cmd /k` kept the window open
    after the CLI exited, and this does not. What that window was worth was a
    readable error when the launch failed — and the launch failures now surface
    better than they did, as a `PanelError` sentence on the page (no binary; an argv
    the OS refuses), because without a shell swallowing them they reach this process
    as an exception instead of a window that closes before you can read it.
    """
    if command and command[0] == "claude":
        binary = claude_binary()
        if binary is None:
            raise PanelError("the `claude` CLI was not found (checked $CLAUDE_BIN, "
                             "PATH and ~/.local/bin). Install it, or set CLAUDE_BIN.")
        command = (binary, *command[1:])
    if os.name == "nt":
        try:
            subprocess.Popen(command, cwd=root, env=_dispatch_env(),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
            # gate-ok(subprocess_result_checked): a detached, visible console window the
            # user drives from here on — there is nothing this function could do with an
            # exit code from a window that has not been interacted with yet
        except OSError as exc:
            # Reaches the page now instead of flashing a console. The one that bites is
            # a command line over the 32767-char `CreateProcess` limit; `session_argv`
            # spills a long prompt to a file to stay under it, so arriving here means
            # something else — a bad path, a refused executable.
            raise PanelError(f"the session could not be started: {exc}") from exc
    else:
        # `shlex.join`, not `" ".join`: the emulator hands this string to a shell, and
        # both a resolved binary path and the card prompt can contain spaces — joined
        # raw, the prompt arrives as a dozen arguments instead of one.
        subprocess.Popen(["x-terminal-emulator", "-e", shlex.join(command)], cwd=root,
                         env=_dispatch_env(), shell=False)
        # gate-ok(subprocess_result_checked): same reason as the Windows branch just above


def session_argv(root: Path, *, name: str = "", prompt: str = "", tier: str = "",
                 agent: str = "", resume: str = "", system_append: str = "") -> list[str]:
    """The `claude` invocation for an interactive session this panel launches.

    **The button used to open a bare `claude` and stop there**, which is a launcher
    that has done almost none of the launching: Karel reported it 2026-08-17 as
    *"it opens Claude CLI. I need to login and insert a prompt. It is not related to
    the card, nor my actually used account and I guess the model is default too.
    This helps very little."* Every one of those four is something the caller
    already knows and the CLI takes on the command line, so the fix is to say them:

    * `prompt` as the trailing positional — `claude "<prompt>"` starts an
      *interactive* session already primed with that first message. Not `-p`: the
      whole point is that the person can answer it.
    * `--model`, resolved from the card's own `tier:` through `tiers.resolve` — the
      same call the runner makes, so the tier table stays the one place a model is
      named. A card that declares no tier gets no `--model` and the CLI's default,
      which is correct: inventing a tier here would be a second tier policy.
    * `--agent`, from the card's `worker:` field, so `Work on this` on a code card
      opens the same charter the night would have dispatched.
    * `--permission-mode`, from `interactive_permission_mode` — **deliberately not
      the `permission_mode` the runner uses.** That setting answers "what may a
      worker do when there is nobody to ask", and on this machine its answer is
      `bypassPermissions`, which is a reasonable thing to say about an unattended
      3 AM dispatch and the wrong thing to say about a session with a person in
      front of it: it would silently hand every session the panel opens fewer
      guardrails than the same person gets in their editor. Two capabilities, two
      postures — the inverse of `shared-dispatch-missing-the-writers-permissions`,
      where one dispatch helper wrongly gave every caller the *least* privileged
      answer. Defaults to `acceptEdits`, so edits flow and everything else still
      asks.

    **`--remote-control` is on by default, and that is the answer to "should we
    build a chat UI".** It keeps the session running locally — this repo, this
    filesystem, the project's MCP servers — while mirroring it to claude.ai/code
    and the phone app, so the interface for the session's questions is one
    Anthropic maintains and the panel stays a launcher (§3.4: *"never a chat
    client"*). Turned off with `remote_control = false` in the host config. An
    account without the entitlement is not a broken button: the CLI documents that
    `--remote-control` still starts the interactive session and shows a
    Remote Control failure notification, so the degradation is a notice rather than
    a dead window. Team and Enterprise plans have the feature off until an Owner
    enables it, which no amount of argv gets around.

    **`resume` is how `Talk` comes back through here, and it had to.** That button
    used to build its own `claude --resume <id>` by hand, which meant it was the one
    launcher on the page with no account posture, no permission mode, no name and no
    remote control — the exact list of omissions the paragraph above was written
    about, surviving in the one place that did not call this function. Resuming is a
    *property of a session*, not a different kind of thing to launch, so it is a
    keyword here rather than a second builder.

    `system_append` rides with it and is why `Talk` now opens waiting for you. A
    resumed transcript restores the worker's charter — the CLI records it as
    `agent-setting` in the very first line of the `.jsonl` — so the session that
    comes back is still an autonomous worker whose brief is to land the card, and
    anything typed at it is read as an instruction about that work rather than a
    question about it. Karel, 2026-08-19: *"'talk' button not just opens
    conversation, but make it continue. It should allow me to ask a question
    first."* `--append-system-prompt` is what changes the posture without changing
    the transcript; `--agent` is deliberately *not* passed on a resume, so the
    charter is not re-asserted on top of the instruction that supersedes it.

    Flag order is load-bearing in one place: `--remote-control` takes an *optional*
    positional name, so the token after it must start with `-` or it eats it.
    `--name` follows it for that reason. The long form rather than `-n`, because
    `pytest_invocation` reads a bare `-n` in an argv as the xdist worker count and is
    right to — that gate has no way to know whose argv this is, and one character of
    brevity is not worth an appeal.

    **`--` before the prompt, and it is load-bearing.** The first version of this
    appended the prompt as a bare trailing token and the session opened with an empty
    input box — the card never reached it, which is the original defect wearing a
    better label. `--add-dir <directories...>` is *variadic*: it consumes every
    following non-flag token, so the prompt was read as a second directory to grant
    access to. Measured 2026-08-17 — `claude -p --add-dir <dir> "Reply with exactly:
    ALPHA"` fails with *"Input must be provided either through stdin or as a prompt
    argument when using --print"*, and the same line with `--` inserted answers
    normally. Ending option parsing is the fix that keeps working when someone adds
    another variadic flag here (`--tools`, `--allowed-tools` and `--plugin-dir` are
    all variadic too); reordering the flags so a single-value one happens to come
    last is the same fix by luck.
    """
    binary = claude_binary()
    if binary is None:
        raise PanelError("the `claude` CLI was not found (checked $CLAUDE_BIN, PATH "
                         "and ~/.local/bin). Install it, or set CLAUDE_BIN.")
    argv = [binary]
    if host_setting(root, "remote_control", True):
        argv.append("--remote-control")
    if name:
        argv += ["--name", name]
    if resume:
        argv += ["--resume", resume]
    if system_append:
        argv += ["--append-system-prompt", system_append]
    if tier:
        try:
            argv += ["--model", tiers.resolve(root, tier)]
        except tiers.TierError as exc:
            # Not fatal, and deliberately not silent either. An unbound tier is a
            # board/config mismatch the maintainer needs to hear about, but refusing
            # to open the session would make a typo in one card's frontmatter into a
            # dead button — so the session opens on the CLI's default model and the
            # page says which model it did not get.
            raise PanelError(f"cannot resolve the model for `tier: {tier}` — {exc}") from exc
    if agent and agent != "none":
        argv += ["--agent", agent]
    argv += ["--permission-mode",
             str(host_setting(root, "interactive_permission_mode", "acceptEdits"))]
    argv += ["--add-dir", str(board.board_dir(root).resolve())]
    if prompt:
        argv += ["--", _prompt_arg(root, name, prompt, argv)]
    return argv


#: How long the whole command line may get before the prompt is handed over as a file
#: instead. `CreateProcess` refuses a command line over 32767 characters, and this
#: board is already past that: `grid-distance-metric` builds a 32610-character prompt
#: (measured 2026-08-17 across 109 cards), which with the flags around it cannot be
#: launched at all. The margin below that ceiling covers quoting expansion and leaves
#: the great majority of cards on the direct path, where the session starts working
#: without reading anything first.
ARGV_BUDGET = 24000


def _prompt_arg(root: Path, name: str, prompt: str, argv: list[str]) -> str:
    """The prompt itself, or a one-line instruction pointing at it on disk.

    Spilling is not the preferred path and is not used unless the direct one would
    fail: a prompt on the command line is in front of the session immediately, while
    a spilled one costs it a `Read` before it can start. But a card long enough to
    blow the command-line limit is a card the button simply could not open, and
    "this one card does nothing when you click it" is a worse answer than one extra
    tool call.

    The file is written where the panel's other per-run artefacts live and named for
    the session, so a spilled prompt is inspectable after the fact — the same reason
    `run_producer` writes `prompt-N.md` beside its stream.
    """
    if len(subprocess.list2cmdline([*argv, prompt])) <= ARGV_BUDGET:
        return prompt
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "session").strip("-") or "session"
    path = root / RUNS / "_panel" / f"prompt-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, prompt)
    return (f"Your instructions are in `{path.resolve().as_posix()}` — it was too long "
            f"to pass on the command line. Read that file and follow it.")


# --------------------------------------------------------------------------
# Reading — the framework's own read API, never a second parser.
# --------------------------------------------------------------------------


@dataclass
class Rail:
    identity: usage.Identity
    snapshot: usage.Snapshot
    verdict: usage.Verdict
    freshness_line: str
    freshness_known: bool
    account_label: str
    account_dispatch: str
    accounts: tuple[manifest.Account, ...]
    run_status: dict = field(default_factory=dict)


def read_rail(root: Path, *, fetch_freshness: bool = False) -> Rail:
    credentials, identity_path = _account_paths(_ACCOUNT)
    identity = usage.read_identity(identity_path)
    # `usage.read_identity` is a local file and is never cached anywhere — it is
    # what `_guard_dispatch_account` vetoes on, and a safety check answering
    # from a stale copy is not a safety check. The meters are different: they
    # are ambient (on every page) and shared cross-process by `read_cached`
    # itself, keyed on `credentials` — see that function's own docstring for
    # why a per-process cache here stopped being enough.
    snapshot = usage.read_cached(credentials)
    verdict = usage.check(snapshot)
    fresh = freshness.read(fetch=fetch_freshness)
    return Rail(
        identity=identity, snapshot=snapshot, verdict=verdict,
        freshness_line=freshness.describe(fresh), freshness_known=fresh.known,
        account_label=_ACCOUNT.label, account_dispatch=_ACCOUNT.dispatch,
        accounts=_accounts(root), run_status=_read_json(root / STATUS_FILE),
    )


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class Context:
    """Everything the five pages read, gathered once per request.

    One object rather than each `_render_*` reaching for the board itself: the
    left rail carries a count for every page, so a single page load needs all
    five answers anyway, and reading the lanes three times to produce them would
    be the same board parsed three times.
    """

    root: Path
    rail: Rail
    base: str
    candidates: list[Candidate] = field(default_factory=list)
    #: Every audio artefact any run has harvested, newest run first within each
    #: card. Gathered here for the same reason the five lane counts are: the Run
    #: page needs it on every load, and `scan_audio_candidates` is cheap enough
    #: (a directory walk under `.ai/runs/`, no subprocess) to pay unconditionally
    #: rather than behind a second read path.
    audio: list[AudioGroup] = field(default_factory=list)
    #: Deliberately *not* the same for images. Image candidates are read on the
    #: one page that is about them — `/decide/<card>`, via `_decide_images` — and
    #: nowhere else, so gathering them into the shared context would make every
    #: page pay a walk of `.ai/runs/` plus a header read per PNG for a list only
    #: one page can use. See `_decide_images` on why the Run page stopped drawing
    #: them at all.
    decisions: list[board.Card] = field(default_factory=list)
    testing: list[board.Card] = field(default_factory=list)
    review: list[board.Card] = field(default_factory=list)
    #: `blocked/` — reviewed ok, work done, the merge needs a person. Gathered
    #: beside `review` because the NOW page draws them adjacently and they used to
    #: be the same list; see `board.BLOCKED_LANE` for why they stopped being one.
    blocked: list[board.Card] = field(default_factory=list)
    #: `failed/` — gates or tests stayed red past `MAX_ATTEMPTS` dispatches and the
    #: card was retired. Shown in the same section as `blocked`, not a section of
    #: its own (Karel, 2026-08-26): both are "reviewed nothing further, a person's
    #: hands fix it", not a decision. Unlike `blocked`, the card's own branch is
    #: already reaped by the time it lands here (README's runner section) — its
    #: `## Error` excerpt is what is left to work from.
    failed: list[board.Card] = field(default_factory=list)
    notes: list[ingest.Note] = field(default_factory=list)
    ideas: list[str] = field(default_factory=list)
    routing: ingest.RoutingView = field(default_factory=ingest.RoutingView)
    #: What this panel started in the background, newest first. Read for every
    #: page because the rail carrying it is on every page — the same reason the
    #: five lane counts are gathered here rather than per-page.
    jobs: list[jobs.Job] = field(default_factory=list)

    @property
    def tonight(self) -> list[Candidate]:
        return [c for c in self.candidates if c.dispatchable]

    @property
    def elsewhere(self) -> list[Candidate]:
        """Work whose *only* blocker is this machine — a `requires:` it does not
        declare. Not "do now": it is *Tonight, on the other machine*.

        `unattended` is part of the test, and it has to be: a card that is both
        `requires: gpu-box` and `unattended: false` needs a person wherever it
        runs, so filing it under "the other machine will take it" would promise
        something no machine is going to do.
        """
        capabilities = host_capabilities(self.root)
        return [c for c in self.candidates
                if not c.dispatchable and c.card.requires
                and c.card.requires not in capabilities and c.card.unattended]

    def notes_routed(self, route: str) -> list[tuple[ingest.Note, ingest.Decision | None]]:
        """Notes in `inbox/` carrying this route, with the last report's `why` if it
        still has one.

        **Keyed on the note, not on the view.** The two used to disagree — a page
        reading the view listed notes the lane no longer held, and a page reading the
        lane could say nothing about routes — and the disagreement is what showed one
        note in two places at once. `Note.route` is the single answer both halves of
        the panel now ask.
        """
        out = []
        for note in self.notes:
            if note.route != route:
                continue
            decision = self.routing.of(note.name)
            out.append((note, decision if decision and decision.route == route else None))
        return out

    @property
    def triage_notes(self) -> list[tuple[ingest.Note, ingest.Decision | None]]:
        """The triage queue: notes routed to the expensive pass and still waiting.

        Nothing dispatches these automatically — that is the route's whole meaning —
        so this is a queue in the ordinary sense, and it lives in `inbox/` because a
        note awaiting triage is precisely what that lane is for.
        """
        return self.notes_routed("triage")

    @property
    def chores(self) -> list[Candidate]:
        """The chore batch's work — which the night skips by *routing*, not refusal.

        `dispatch.select` is explicit that this is "not a refusal — a routing fact":
        a chore is dispatched as a *batch* — by the run's `chores` queue, which
        `--queue both` works before the task queue — because the per-card treatment
        is exactly what the batch exists to avoid. Filing it
        under "Do now" therefore said something false — it told you a person was
        needed at the keyboard for work that has its own button two sections down,
        under a chip that truncated the explanation mid-sentence at seventy
        characters.

        A chore that *also* wants another machine is left to `elsewhere`, which is
        the more useful of the two facts: the batch here cannot take it either.

        **`kind: chore` is not on its own enough to list a card here**, and listing it
        on that alone is what put `ad-sound-for-recharge` under this heading beside a
        `Run chores` button that could never take it: the card was `unattended: false`,
        which `chores.eligible()` refuses, so every batch reported it as "left out" and
        the panel went on advertising it as batch work. The section now asks the batch
        itself what it would take, so the answer here and the answer there cannot
        differ. A chore it refuses falls through to `do_now` — which is where a card
        needing a person at the keyboard belongs, and which gives it a `Work on this`
        button instead of a button that does nothing for it.
        """
        elsewhere = {id(c) for c in self.elsewhere}
        capabilities = host_capabilities(self.root)
        return [c for c in self.candidates
                if not c.dispatchable and c.card.kind == board.KIND_CHORE
                and id(c) not in elsewhere
                and not chores.eligible(c.card, capabilities=capabilities)]

    @property
    def do_now(self) -> list[Candidate]:
        """Everything else the night will not take — `unattended: false`,
        `worker: none`, a broken schema. It needs a person present.

        **`kind: inline` cards land here, and that is the whole of what this page
        needed.** They used to be notes listed beside this section under a second
        heading, because an inline note had no card to appear as a candidate; now
        `ingest` cards them into `tasks/` with `unattended: false`, so the field that
        already means "the night must not start this" puts them exactly where the
        page was drawing them by hand.
        """
        parked = {id(c) for c in self.elsewhere} | {id(c) for c in self.chores}
        return [c for c in self.candidates if not c.dispatchable and id(c) not in parked]

    def counts(self) -> dict[str, int]:
        return {
            "queue": len(self.tonight) + len(self.elsewhere) + len(self.chores)
                     + len(self.do_now),
            "capture": len(self.notes) + len(self.ideas),
            "you": len(self.decisions) + len(self.testing) + len(self.review)
                   + len(self.blocked) + len(self.failed),
            "running": (int(run_is_live(self.rail.run_status, _latest_record(self.root)))
                        + sum(1 for job in self.jobs if jobs.state(job) == jobs.RUNNING)),
            # Things the System page wants you for — never "everything it could do".
            "system": system_attention(self.root),
        }


def read_context(root: Path, *, fetch_freshness: bool = False) -> Context:
    """Everything a page reads. Renders in a repo nightshift has never been installed in.

    **That last property is load-bearing and was not free.** `bootstrap` writes the
    launchers before the install, so the first thing a new project does is open this
    panel in a repo with no `.ai/manifest.toml` — and the Setup page is what runs the
    install. Several reads below degrade on their own (`board_root` falls back to
    `Board`, `_accounts` catches), which is what made it *look* as though the whole
    function already did. `default_base` does not: it goes through
    `branches.integration`, which raises **on purpose** rather than guessing, because
    every card is built on whatever it returns.

    So the uninstalled case is answered here, once, rather than by asking each read to
    tolerate it: no manifest means no board, no queue and no branch role, so the honest
    context is an empty one. `base` is left empty rather than defaulted — inventing an
    integration branch is precisely what `branches.integration` refuses to do, and this
    is not the place to do it on its behalf.
    """
    if not installed(root):
        # Jobs are read even here: `/api/setup` is a button an uninstalled repo
        # has, and a setup that failed silently is the same bug in the same place.
        return Context(root=root, rail=read_rail(root, fetch_freshness=fetch_freshness),
                       base="", jobs=jobs.read_all(root, limit=JOBS_READ))
    return Context(
        root=root,
        rail=read_rail(root, fetch_freshness=fetch_freshness),
        base=default_base(root),
        candidates=select_candidates(root, host_capabilities(root), schema_violations(root)),
        decisions=board.cards(root, "needs-decision"),
        testing=board.cards(root, "testing"),
        review=board.cards(root, "review"),
        blocked=board.cards(root, board.BLOCKED_LANE),
        failed=board.cards(root, "failed"),
        notes=ingest.notes(root),
        ideas=read_ideas(root),
        routing=ingest.read_view(root),
        jobs=jobs.read_all(root, limit=JOBS_READ),
        audio=scan_audio_candidates(root),
    )


#: How many job records a page load reads. `jobs.KEEP` is what the directory
#: retains; this is what a *render* needs, and the two are different numbers on
#: purpose — the rail shows the newest handful, and reading eighty JSON files to
#: draw four lines is a cost paid on every click.
JOBS_READ = 12


def read_ideas(root: Path) -> list[str]:
    """Filenames only. The private lane is enumerated here and never summarised;
    a body reaches the browser only when the person asks for one to edit
    (`/api/body`), which is the same act `boardcmd edit` exists to serve."""
    lane = board.board_dir(root) / board.PRIVATE_LANE
    if not lane.is_dir():
        return []
    return sorted(p.name for p in lane.glob("*.md"))


def _latest_record(root: Path) -> dict:
    records = run_record.read_all(root)
    return records[0] if records else {}


def attempt_dir(root: Path, card_id: str, attempt: int) -> Path:
    """Where one attempt's artefacts are. Deliberately **not** `telemetry.run_dir`,
    which creates the directory: a page load must not leave a trail of empty
    folders behind for cards it merely rendered."""
    return root / RUNS / card_id / f"attempt-{attempt}"


def session_id(out_dir: Path) -> str:
    """The CLI session behind one attempt, for `claude --resume`."""
    for path in sorted(out_dir.glob("worker-*.json"), reverse=True):
        data = _read_json(path)
        found = data.get("session_id")
        if isinstance(found, str) and found:
            return found
    return ""


def _latest_session_for_card(root: Path, card: board.Card) -> str:
    """The CLI session behind a card's most recent attempt, if the runner ran one.

    Empty for a card with no `attempts:` (never dispatched through the runner) or
    whose attempt directory did not survive — `.ai/runs/` is gitignored and
    machine-local, so a card built on a different machine has no session here
    even though its frontmatter still says `attempts: N`. Callers must treat
    empty as "fall back to a fresh session", not as an error.
    """
    if not card.attempts:
        return ""
    return session_id(attempt_dir(root, card.id, card.attempts))


# --------------------------------------------------------------------------
# Audio candidates — what a run harvested but did not adopt. `audio-asset/
# SKILL.md` §4b is explicit that a text checker cannot hear a clip, so there is
# deliberately no `audio-reviewer` the way `art-reviewer` judges a sprite: the
# ear stays Karel's, and everything here is read-and-serve, never a verdict.
# --------------------------------------------------------------------------

#: The three kinds `audio-asset/SKILL.md` §5 actually produces — `.wav` for
#: SFX, `.mp3` for music loops, `.ogg` for voice lines — not an open-ended
#: sniff of whatever a harvest dir happens to hold.
AUDIO_EXTS = frozenset({".wav", ".mp3", ".ogg"})

_AUDIO_CONTENT_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}

#: Where a pick survives a reload and a panel restart. Machine-local and
#: gitignored — the same category as the preflight receipt and `.ai/host.json`,
#: not a decision the repo itself carries.
AUDIO_PICKS_FILE = Path(".ai") / "audio_picks.json"  # gate-ok(source_reference_liveness):
# written the first time `write_audio_pick` runs, in a consuming project; this repo dispatches
# nothing and has picked nothing, so the file does not exist here.


@dataclass
class AudioTake:
    """One harvested file — a generated candidate, a synth fallback, or
    whatever else a run left beside a `candidates.json`."""

    id: str
    rel: str  # posix path relative to `root`; `/audio/<rel>` is what serves it
    meta: dict = field(default_factory=dict)  # this take's row in candidates.json, or {}


@dataclass
class AudioGroup:
    """One `candidates.json` (or its absence) worth of takes, from one
    attempt's harvested artefacts."""

    card: str
    attempt: int
    rel_dir: str  # the directory holding the takes, relative to `root`
    sound: str
    generated: str
    adopted_id: str
    mtime: float
    takes: list[AudioTake] = field(default_factory=list)

    def pick_key(self) -> str:
        """What a pick for this group files under. The sound name when the
        manifest names one; the directory otherwise — still stable across a
        reload, just not shared with a same-named sound from a run whose
        manifest never said so."""
        return self.sound or self.rel_dir


def scan_audio_candidates(root: Path) -> list[AudioGroup]:
    """Every audio artefact any run has harvested — newest run first within
    each card, cards ordered by their own newest run.

    Reads `.ai/runs/*/attempt-*/artefacts/` — `worktree.harvest`'s own layout,
    read here rather than reimplemented as a second guess at it (`RUNS`,
    imported from `nightshift.runner`). Nothing here is Project Tigress-specific:
    the harvest dir's name and the `candidates.json` shape both come from
    whatever the project's own worker wrote, so a project that harvests no
    audio — or none at all — gets an empty list rather than an error.
    """
    runs_dir = root / RUNS
    if not runs_dir.is_dir():
        return []
    groups: list[AudioGroup] = []
    for card_dir in runs_dir.iterdir():
        if not card_dir.is_dir():
            continue
        for attempt in sorted(card_dir.glob("attempt-*")):
            match = re.fullmatch(r"attempt-(\d+)", attempt.name)
            artefacts = attempt / "artefacts"
            if not match or not artefacts.is_dir():
                continue
            by_dir: dict[Path, list[Path]] = {}
            for item in artefacts.rglob("*"):
                if item.is_file() and item.suffix.lower() in AUDIO_EXTS:
                    by_dir.setdefault(item.parent, []).append(item)
            for directory, files in by_dir.items():
                rel_dir = directory.relative_to(root).as_posix()
                mtime = directory.stat().st_mtime
                raw = _read_json_any(directory / "candidates.json")
                if isinstance(raw, list):
                    groups.extend(_groups_from_take_list(
                        root, card_dir.name, int(match.group(1)), rel_dir, mtime,
                        files, raw))
                    continue
                sound_manifest = raw if isinstance(raw, dict) else {}
                entries = {str(c.get("id")): c for c in sound_manifest.get("candidates", [])
                          if isinstance(c, dict)}
                adopted = sound_manifest.get("adopted")
                takes = [AudioTake(id=f.stem, rel=f.relative_to(root).as_posix(),
                                   meta=entries.get(f.stem, {}))
                        for f in sorted(files)]
                groups.append(AudioGroup(
                    card=card_dir.name, attempt=int(match.group(1)),
                    rel_dir=rel_dir,
                    sound=str(sound_manifest.get("sound") or ""),
                    generated=str(sound_manifest.get("generated") or ""),
                    adopted_id=str(adopted.get("id") or "") if isinstance(adopted, dict) else "",
                    mtime=mtime, takes=takes,
                ))
    by_card: dict[str, list[AudioGroup]] = {}
    for group in groups:
        by_card.setdefault(group.card, []).append(group)
    ordered: list[AudioGroup] = []
    for card in sorted(by_card, key=lambda c: max(g.mtime for g in by_card[c]), reverse=True):
        ordered.extend(sorted(by_card[card], key=lambda g: g.mtime, reverse=True))
    return ordered


def _read_json_any(path: Path) -> dict | list | None:
    """`_read_json` without the dict-only filter — `candidates.json` comes in two
    shapes, and throwing the second away is how a run's metadata went missing."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _groups_from_take_list(root: Path, card: str, attempt: int, rel_dir: str,
                           mtime: float, files: list[Path], rows: list) -> list[AudioGroup]:
    """One group per sound out of a *flat* `candidates.json` — a list of take rows,
    each naming its sound in `name` and its file in `file`.

    That is the shape `audio-asset/SKILL.md` §5's own wording produces ("per take:
    the `name`, the exact `prompt`, the `seed`…"), and `sound-for-taser`
    (2026-09-11) wrote exactly it: two sounds, four takes, one directory. Read as a
    dict it was `{}`, so all four takes landed in one group with no metadata and
    **one** pick between them — a choice between a firing sound and a hit sound,
    which is not a choice anyone was asked to make. Split by `name`, each sound gets
    its own pick; a file with no row keeps the directory as its group, the same
    fallback a manifest-less directory has.

    The validator's numbers sit nested under `validator` in this shape, so the ones
    the renderer shows (`ok`, `problems`, duration) are lifted to the top of `meta`.
    """
    by_file: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("file"):
            by_file[Path(str(row["file"])).stem] = row
    by_sound: dict[str, list[AudioTake]] = {}
    for found in sorted(files):
        row = by_file.get(found.stem, {})
        meta = dict(row)
        validator = row.get("validator")
        if isinstance(validator, dict):
            meta.setdefault("ok", validator.get("ok"))
            meta.setdefault("problems", validator.get("problems") or [])
            if validator.get("out_seconds") is not None:
                meta.setdefault("seconds", validator["out_seconds"])
        by_sound.setdefault(str(row.get("name") or ""), []).append(AudioTake(
            id=found.stem, rel=found.relative_to(root).as_posix(), meta=meta))
    return [AudioGroup(card=card, attempt=attempt, rel_dir=rel_dir, sound=sound,
                       generated="", adopted_id="", mtime=mtime, takes=takes)
            for sound, takes in sorted(by_sound.items())]


def read_audio_picks(root: Path) -> dict[str, str]:
    """`{pick key: chosen take's rel path}`, read from disk rather than kept in
    a process-wide variable — a pick is the opposite of `_ACCOUNT`/`_TIER`: a
    decision that should outlive the click that made it, not one that must not.
    """
    data = _read_json(root / AUDIO_PICKS_FILE)
    return {str(key): str(value.get("rel", "")) for key, value in data.items()
            if isinstance(value, dict) and value.get("rel")}


def write_audio_pick(root: Path, key: str, rel: str) -> None:
    """Record `rel` as the pick for `key`. `rel` must already have been proven
    to point at a real, harvested audio file — callers pass it through
    `resolve_audio_path` first, exactly as `_route`'s own POST handler does."""
    if not key:
        raise PanelError("no sound to record the pick against")
    path = root / AUDIO_PICKS_FILE
    data = _read_json(path)
    if not isinstance(data, dict):
        data = {}
    data[key] = {"rel": rel,
                 "picked_at": dt.datetime.now().replace(microsecond=0).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def resolve_audio_path(root: Path, rel: str) -> Path:
    """Confine `rel` to `.ai/runs/` — the harvest root, and the one place a
    request for "an audio file" is allowed to reach. Raises `PanelError`
    rather than ever serving or recording a path a URL or a POST body pointed
    outside it, the same discipline `read_body` holds for the board.
    """
    candidate = Path(rel)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    runs_root = (root / RUNS).resolve()
    if candidate != runs_root and runs_root not in candidate.parents:
        raise PanelError(f"{rel} is not inside {RUNS.as_posix()}")
    if candidate.suffix.lower() not in AUDIO_EXTS:
        raise PanelError(f"{rel} is not an audio file")
    if not candidate.is_file():
        raise PanelError(f"{rel} does not exist")
    return candidate


def answer_audio_pick(root: Path, card_id: str) -> str:
    """Write the picks into `card_id`'s `## Thread` once **every** sound its newest
    attempt produced has one; otherwise say how many are still owed.

    Per sound, not per click: a card that generated a firing sound and a hit sound
    owes two picks, and answering on the first would release the installing pass
    with half a decision. Only the newest attempt counts — an older attempt's takes
    were re-rolled away, and a pick filed under the same sound name from another
    card's run is not a pick of *these* takes (hence the membership check, not a
    key lookup alone).

    `""` for a card that is not parked in `needs-decision/`: the pick is still kept,
    and inventing a lane move from wherever the card is would be exactly what
    `api/image/pick` refuses to do too. Nothing is copied or installed here — the
    adoption steps are the project's (`audio-asset/SKILL.md` §5), run by the pass
    this answer releases.
    """
    card = board.find(root, card_id)
    if card is None or card.lane != "needs-decision":
        return ""
    groups = [g for g in scan_audio_candidates(root) if g.card == card_id]
    if not groups:
        return ""
    newest = max(g.attempt for g in groups)
    picks = read_audio_picks(root)
    chosen: list[tuple[AudioGroup, str]] = []
    owed: list[str] = []
    for group in sorted((g for g in groups if g.attempt == newest), key=lambda g: g.pick_key()):
        rel = picks.get(group.pick_key(), "")
        if rel and rel in {t.rel for t in group.takes}:
            chosen.append((group, rel))
        else:
            owed.append(group.sound or group.rel_dir)
    if owed:
        return (f"{len(owed)} more sound(s) to pick before the card is answered: "
                + ", ".join(f"`{name}`" for name in owed))
    adopt = "; ".join(f"`{Path(rel).name}` for `{g.sound or g.rel_dir}`" for g, rel in chosen)
    where = sorted({Path(rel).parent.as_posix() for _, rel in chosen})
    try:
        return decide.write_answer(
            root, card_id, [],
            f"Adopt {adopt} — picked in the Command Center's audio candidates from "
            + ", ".join(f"`{w}`" for w in where) + ".")
    except decide.DecideError as exc:
        raise PanelError(f"the pick is recorded, but the answer was refused: {exc}") from exc


# --------------------------------------------------------------------------
# Image candidates — the visual twin of the audio block above, and the surface
# `settle._park_for_pick`'s `## Question` points at by name. A card that
# generated N candidates and installed none is filed in `needs-decision/`
# rather than `testing/` (`worktree.unadopted_artefacts`), precisely because
# what it owes the maintainer is a *choice*; before this there was nowhere to
# make it, and the card asked him to go and play a picture that was never in
# the game.
#
# Unlike audio there is no `candidates.json` here, so the two facts each
# candidate carries are read off what is actually on disk: the checker's
# `review-<round>.json` verdict, and the PNG's own IHDR. Everything is
# read-and-serve; nothing here judges, converts or installs anything.
# --------------------------------------------------------------------------

#: `.png` only — what the pixel-art pipeline produces, and the one format whose
#: header this module can read without a decoder. Deliberately not an
#: open-ended sniff of whatever a harvest dir holds: a `.jpg` would render but
#: could carry no dimension chip, and a silently chipless row is worse than an
#: absent one.
IMAGE_EXTS = frozenset({".png"})

_IMAGE_CONTENT_TYPES = {".png": "image/png"}

#: Where an image pick survives a reload and a panel restart — same category and
#: same reasoning as `AUDIO_PICKS_FILE`, keyed by **card** rather than by a name
#: out of a manifest, because a card is what the answer is written against.
IMAGE_PICKS_FILE = Path(".ai") / "image_picks.json"  # gate-ok(source_reference_liveness):
# written the first time `write_image_pick` runs, in a consuming project; this repo
# dispatches nothing and has picked nothing, so the file does not exist here.


@dataclass
class ImageShot:
    """One harvested image, with the two facts a fifteen-second decision needs."""

    id: str
    rel: str  # posix path relative to `root`; `/image/<rel>` is what serves it
    width: int = 0  # 0 when the IHDR could not be read — shown as such, never faked
    height: int = 0
    #: Named by the checker's own verdict as the strongest candidate. Advice, not
    #: a decision: `worktree.unadopted_artefacts` exists because a checker saying
    #: `pass` is exactly what is *not* enough to install one.
    best: bool = False


@dataclass
class ImageGroup:
    """One directory of harvested images from one attempt, with that attempt's
    checker verdict."""

    card: str
    attempt: int
    rel_dir: str  # the directory holding the shots, relative to `root`
    verdict: str  # "pass" / "revise" / … from review-<round>.json, or ""
    notes: str  # the checker's prose, or ""
    best: str  # the filename it named, or ""
    mtime: float
    shots: list[ImageShot] = field(default_factory=list)


def png_size(path: Path) -> tuple[int, int] | None:
    """`(width, height)` from the PNG's IHDR, or `None` for anything else.

    Twenty-four bytes off the front of the file rather than a decoder: IHDR sits
    at a fixed offset in every PNG, so the dimensions cost no dependency at all —
    the same four lines the consuming project's own `asset_hygiene` gate uses.
    `None` for a truncated file, a non-PNG behind a `.png` name, or an unreadable
    one; the caller shows that as unknown rather than guessing 32x32.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(24)
    except OSError:
        return None
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def read_checker_verdict(out_dir: Path) -> dict:
    """The verdict that stood for one attempt: its **highest-numbered**
    `review-<round>.json`, or `{}`.

    `review.run_checker` writes one per round (`review-1.json`, `review-2.json`,
    …), so the last is the one that decided the attempt. Deliberately **not**
    `review-verdict.json`, which sits in the same directory and is the *diff*
    reviewer's judgement of the branch — a different agent judging a different
    thing, and reading it here would put prose about a Python diff under a row
    of sprites.

    `{}` for an attempt whose checker never ran, and for one whose newest
    verdict file is unparsable: the section degrades to "no checker verdict on
    disk" rather than quietly falling back to an older round, because "round 2
    said this" and "round 1 said this" are not interchangeable claims.
    """
    rounds = []
    for path in out_dir.glob("review-*.json"):
        match = re.fullmatch(r"review-(\d+)\.json", path.name)
        if match:
            rounds.append((int(match.group(1)), path))
    if not rounds:
        return {}
    return _read_json(max(rounds, key=lambda item: item[0])[1])


def scan_image_candidates(root: Path) -> list[ImageGroup]:
    """Every image any run has harvested — newest run first within each card,
    cards ordered by their own newest run.

    The same walk and the same ordering as `scan_audio_candidates` over the same
    `worktree.harvest` layout (`.ai/runs/*/attempt-*/artefacts/`), grouped by the
    directory the files landed in so one attempt that wrote two harvest dirs
    reads as two groups rather than one pile. A project that harvests no images —
    or nothing at all — gets an empty list rather than an error.
    """
    runs_dir = root / RUNS
    if not runs_dir.is_dir():
        return []
    groups: list[ImageGroup] = []
    for card_dir in runs_dir.iterdir():
        if not card_dir.is_dir():
            continue
        for attempt in sorted(card_dir.glob("attempt-*")):
            match = re.fullmatch(r"attempt-(\d+)", attempt.name)
            artefacts = attempt / "artefacts"
            if not match or not artefacts.is_dir():
                continue
            # Per attempt, not per directory: the verdict is written about the
            # attempt's `artefacts/` as a whole, so both harvest dirs of a
            # two-dir attempt legitimately carry the same notes.
            verdict = read_checker_verdict(attempt)
            best = str(verdict.get("best") or "")
            by_dir: dict[Path, list[Path]] = {}
            for item in artefacts.rglob("*"):
                if item.is_file() and item.suffix.lower() in IMAGE_EXTS:
                    by_dir.setdefault(item.parent, []).append(item)
            for directory, files in by_dir.items():
                shots = []
                for found in sorted(files):
                    size = png_size(found)
                    shots.append(ImageShot(
                        id=found.stem, rel=found.relative_to(root).as_posix(),
                        width=size[0] if size else 0, height=size[1] if size else 0,
                        best=bool(best) and found.name == best))
                groups.append(ImageGroup(
                    card=card_dir.name, attempt=int(match.group(1)),
                    rel_dir=directory.relative_to(root).as_posix(),
                    verdict=str(verdict.get("verdict") or ""),
                    notes=str(verdict.get("notes") or ""), best=best,
                    mtime=directory.stat().st_mtime, shots=shots,
                ))
    by_card: dict[str, list[ImageGroup]] = {}
    for group in groups:
        by_card.setdefault(group.card, []).append(group)
    ordered: list[ImageGroup] = []
    for card in sorted(by_card, key=lambda c: max(g.mtime for g in by_card[c]), reverse=True):
        ordered.extend(sorted(by_card[card], key=lambda g: g.mtime, reverse=True))
    return ordered


def read_image_picks(root: Path) -> dict[str, str]:
    """`{card id: chosen shot's rel path}`, read fresh off disk on every render
    for the same reason `read_audio_picks` is — a pick must outlive the click
    that made it, and a process-wide variable would not survive a restart."""
    data = _read_json(root / IMAGE_PICKS_FILE)
    return {str(key): str(value.get("rel", "")) for key, value in data.items()
            if isinstance(value, dict) and value.get("rel")}


def write_image_pick(root: Path, card_id: str, rel: str) -> None:
    """Record `rel` as the pick for `card_id`. `rel` must already have been
    proven to point at a real, harvested image — callers pass it through
    `resolve_image_path` first, exactly as `_route`'s own POST handler does."""
    if not card_id:
        raise PanelError("no card to record the pick against")
    path = root / IMAGE_PICKS_FILE
    data = _read_json(path)
    if not isinstance(data, dict):
        data = {}
    data[card_id] = {"rel": rel,
                     "picked_at": dt.datetime.now().replace(microsecond=0).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def resolve_image_path(root: Path, rel: str) -> Path:
    """Confine `rel` to `.ai/runs/` — the harvest root, and the one place a
    request for "an image" is allowed to reach. Same three refusals as
    `resolve_audio_path`, for the same reason: this path arrives from a URL or a
    POST body, and neither is trusted to stay inside the run dir on its own.
    """
    candidate = Path(rel)
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
    runs_root = (root / RUNS).resolve()
    if candidate != runs_root and runs_root not in candidate.parents:
        raise PanelError(f"{rel} is not inside {RUNS.as_posix()}")
    if candidate.suffix.lower() not in IMAGE_EXTS:
        raise PanelError(f"{rel} is not an image file")
    if not candidate.is_file():
        raise PanelError(f"{rel} does not exist")
    return candidate


def diff_stat(root: Path, base: str, branch: str) -> str:
    """`+38 −12 · 2 files`, or `""` when git cannot say.

    Read rather than stored: the card carries no diff stat, and the branch is
    right there. A failure is silence — a missing stat must not be able to stop
    a page rendering.
    """
    text = git.text(root, "diff", "--shortstat", f"{base}...{branch}")
    if not text:
        return ""
    files = insertions = deletions = 0
    for part in text.split(","):
        part = part.strip()
        number = part.split(" ", 1)[0]
        if not number.isdigit():
            continue
        if "file" in part:
            files = int(number)
        elif "insertion" in part:
            insertions = int(number)
        elif "deletion" in part:
            deletions = int(number)
    return f"+{insertions} −{deletions} · {files} file{'s' if files != 1 else ''}"


def elapsed_since(stamp: str) -> str:
    """`6:12` — minutes and seconds since an ISO stamp, or `""`."""
    try:
        started = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    seconds = int((dt.datetime.now() - started).total_seconds())
    if seconds < 0:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def span(started: str, finished: str) -> str:
    """`2 h 11 min` between two ISO stamps, or `""`."""
    try:
        first = dt.datetime.fromisoformat(started)
        last = dt.datetime.fromisoformat(finished)
    except (TypeError, ValueError):
        return ""
    minutes = int((last - first).total_seconds() // 60)
    if minutes < 0:
        return ""
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def machine_lines(root: Path) -> list[str]:
    """The small block at the foot of the rail: which box, what it can do, and
    which two commits are actually in play."""
    capabilities = sorted(host_capabilities(root))
    fresh = freshness.read(fetch=False)
    sha = _git_out(freshness.framework_checkout(), "rev-parse", "--short", "HEAD")
    return [
        f"<b>{_e(socket.gethostname())}</b>",
        _e(", ".join(capabilities) if capabilities else "no declared capabilities"),
        _e(f"{current_branch(root) or '?'} @ {_git_out(root, 'rev-parse', '--short', 'HEAD') or '?'}"),
        _e(f"nightshift {fresh.branch or '?'} @ {sha or '?'}"),
    ]


def _git_out(cwd: Path, *args: str) -> str:
    return git.text(cwd, *args) or ""


# --------------------------------------------------------------------------
# Rendering. One HTML file with `{{SLOT}}` placeholders; everything below
# builds escaped fragments to drop into them.
# --------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _attr(value: object) -> str:
    """A value safe to sit inside a single-quoted JS string in an attribute."""
    return html.escape(str(value), quote=True).replace("'", "&#39;")


def _chip(text: str, kind: str = "mute") -> str:
    return f'<span class="chip {kind}">{_e(text)}</span>'


def _act(label: str, *, onclick: str = "", href: str = "", primary: bool = False,
         disabled: bool = False, extra: str = "") -> str:
    classes = "act primary" if primary else "act"
    if href:
        return f'<a class="{classes}" href="{_e(href)}" {extra}>{_e(label)}</a>'
    state = " disabled" if disabled else ""
    return (f'<button type="button" class="{classes}"{state} '
            f'onclick="{onclick}" {extra}>{_e(label)}</button>')


def _row(*, body: str, acts: str = "", marker: str = "&middot;", grip: bool = False,
         control: str = "", card_id: str = "", data: str = "") -> str:
    """One register line: grip · control · marker · body · actions. Only `_item` calls it."""
    classes = "row" if grip else "row no-grip"
    grip_cell = ('<span class="grip" title="Drag to reorder">&#x2059;</span>'
                 if grip else '<span class="grip">&nbsp;</span>')
    data += f' data-id="{_e(card_id)}"' if card_id else ""
    draggable = ' draggable="true"' if grip else ""
    return (f'<div class="{classes}"{draggable}{data}>{grip_cell}'
            f'{control or "<span></span>"}'
            f'<span class="marker">{marker}</span>'
            f'<div class="body">{body}</div>'
            f'<div class="acts">{acts}</div></div>')


def _meta(items: list[str]) -> str:
    """The small mono facts under a row.

    Every item is wrapped in its own element even when it is already one: `.meta`
    is a flex row and its `gap` only separates *children*, so a bare text node
    lands flush against its neighbour — which is how `code-thread` and
    `verify: play` rendered as `code-threadverify: play` the first time this was
    looked at.
    """
    if not items:
        return ""
    cells = "".join(item if item.startswith("<span") else f"<span>{item}</span>"
                    for item in items if item)
    return f'<div class="meta">{cells}</div>'


def _tag_chips(card: board.Card) -> list[str]:
    """The card's tags; `nightshift` is amber because the night never takes one."""
    return [_chip(tag, "warn" if tag == "nightshift" else "mute") for tag in card.tags]


def _size_chip(card: board.Card) -> str:
    """**Too big** on a `tasks/` card past the runner's own `oversize_note`, else "".

    Advisory, never blocking — the card still dispatches; the tooltip carries the note.
    """
    note = oversize_note(card)
    if not note:
        return ""
    return (f'<span class="chip warn" title="{_attr(note)}">too big &middot; '
            f'{card_bytes(card) / 1024:.1f} KB</span>')


def _tier_chip(card: board.Card) -> str:
    """The tier a session opened on this card now would run at (`effective_tier`).

    Amber when the Options dialog's override beats the card's own `tier:`.
    """
    tier = effective_tier(card.tier)
    beaten = bool(card.tier) and tier != card.tier
    if beaten:
        title = f"Options override this card's own tier ({card.tier})"
    elif card.tier:
        title = "the card's own tier"
    elif tier:
        title = "from Options — the card sets no tier"
    else:
        title = "no tier set: the CLI's default model"
    return (f'<span class="chip {"warn" if beaten else "mute"}" '
            f'title="{_e(title)}">tier {_e(tier or "none")}</span>')


def _local_chip(root: Path, card: board.Card) -> str:
    """`local` on a card a dispatch would send to the local model; silent otherwise.

    Unprobed: it says the card is eligible, not that the server is up.
    """
    worker = (card.worker or "").strip()
    if not worker or worker == "none":
        return ""
    if effective_runtime(root, worker) != runtimes.LOCAL:
        return ""
    local = runtimes.local_model(root)
    title = (f"Runs on {local.model if local else 'the local model'} when its server "
             f"answers, else on cloud. Turn it off in Options.")
    return f'<span class="chip mute" title="{_e(title)}">local</span>'


def _info_box(card: board.Card, *, how_to_test: bool = False) -> str:
    """The goal, and — on a `testing/` row — the play-through scenario, collapsed
    under the row until `toggleInfo` opens it.

    In the card's own words rather than summarised: `board.section` returns the
    `## Intent`/`## How to test` body verbatim, and this only ever wraps it in
    markdown. Each part is its own `<details>` so either can be closed without
    closing the other — a card whose goal is short and scenario is long (or the
    reverse) should not force both open or both shut.

    `how_to_test` is a separate flag, not `card.verify == "play"`: the review
    lane's cards are pre-merge, and `## How to test` is written by `settle` only
    once a card actually lands (`runner.py`) — asking for it there would show
    "not recorded" on every row, for a section nothing has had the chance to
    write yet, rather than the absence of a scenario meaning anything.

    **Each `<details>` carries an id, and it has to.** `open` here is a static
    attribute this function always writes — it says nothing about whether a
    person closed one by hand. The live refresh (`softRefresh`, every 10 s)
    re-renders the whole section and swaps in whatever changed; `carryState`
    already knows how to carry a checkbox, a slider and this very box's own
    `.card-info.open` class across that swap, but nothing carried a native
    `<details>`'s open/closed state, so closing "Goal" and waiting past one tick
    reopened it — the swap was comparing the live DOM's `outerHTML`, which no
    longer said `open` once you closed it, against a fresh render that always
    does, and the mismatch alone triggered a full re-swap. The id is what
    `carryState`'s new branch matches on to restore the boolean rather than the
    markup.
    """
    intent = board.section(card.text, "Intent") or "(no `## Intent` on this card)"
    parts = [f'<details open id="info-{_e(card.id)}-goal"><summary>Goal</summary>'
             f'<div class="doc">{markdown(intent)}</div></details>']
    if how_to_test:
        how = board.section(card.text, "How to test") or "(not recorded on this card)"
        parts.append(f'<details open id="info-{_e(card.id)}-test"><summary>How to test</summary>'
                     f'<div class="doc">{markdown(how)}</div></details>')
    return f'<div class="card-info" id="info-{_e(card.id)}">{"".join(parts)}</div>'


def _section(title: str, count: int, rows: str, *, note: str = "", sub: str = "",
             bar: str = "", empty: str = "", rows_id: str = "",
             sec_id: str = "") -> str:
    """One titled block of the page.

    `sec_id` is what the live refresh swaps on. Every section that can change while
    you are looking at it should carry one: the client re-renders the page, matches
    regions up by id, and replaces only the ones whose HTML actually differs. A
    section with no id is simply never swapped — safe, but it goes stale, so the
    absence should be a decision rather than an oversight.
    """
    head = [f'<div class="sec-head"><h2>{_e(title)}</h2>'
            f'<span class="count">{count}</span>']
    if note:
        head.append(f'<p class="note">{_e(note)}</p>')
    head.append("</div>")
    out = [f'<section id="sec-{_e(sec_id)}">' if sec_id else "<section>",
           "".join(head)]
    if sub:
        out.append(f'<p class="sec-sub">{sub}</p>')
    # The id is what the drag-and-drop and the take-first slider bind to; a
    # section that carries reorderable rows and no id is a section whose rows
    # silently cannot be dragged.
    ident = f' id="{_e(rows_id)}"' if rows_id else ""
    out.append(f'<div class="rows"{ident}>{rows}</div>' if rows
               else f'<div class="empty">{_e(empty or "Nothing here.")}</div>')
    if bar:
        out.append(bar)
    out.append("</section>")
    return "".join(out)


def _group(label: str) -> str:
    return f'<div class="group-label">{_e(label)}</div>'


def _phases_html(phase: str) -> str:
    """The five pills, with the current one filled and everything before it dim
    green. An unknown phase lights nothing rather than guessing."""
    current = _PHASE_ALIASES.get(phase, phase)
    names = [name for name, _ in PHASE_STEPS]
    index = names.index(current) if current in names else (
        len(names) if phase in _PHASE_DONE else -1)
    out = []
    for position, (_, label) in enumerate(PHASE_STEPS):
        state = "done" if position < index else ("now" if position == index else "")
        out.append(f'<span class="phase {state}">{_e(label)}</span>')
    return f'<div class="phases">{"".join(out)}</div>'


def _active_row_lane(status: dict) -> str:
    """What the roster's own `lane` cell should say for the one `tasks/` row
    that is currently dispatching, in place of a flat, unchanging "queued".

    The sidebar rail already turns this same `status.json` heartbeat into the
    phase pills (`_phases_html`) — this reuses `PHASE_STEPS`'s labels so the
    two never drift apart, then appends the attempt number when there is one,
    since a retry is exactly the other transition `details-on-run` asked for.
    An unrecognised phase (e.g. "sleeping") is shown as-is rather than folded
    back to "queued", which would hide that something is actually happening.
    """
    phase = str(status.get("phase") or "")
    resolved = _PHASE_ALIASES.get(phase, phase)
    label = dict(PHASE_STEPS).get(resolved, phase or "queued")
    attempt = status.get("attempt")
    return f"{label} (attempt {attempt})" if attempt else label


#: How old a heartbeat may be and still be believed. The runner rewrites
#: `status.json` at every phase change and its longest legitimate phase is the
#: worker, bounded by a wall-clock timeout of an hour — so two hours is past
#: anything a live run can produce, without being so tight that a slow card
#: reads as dead.
HEARTBEAT_TRUSTED_FOR = dt.timedelta(hours=2)


def run_is_live(status: dict, record: dict) -> bool:
    """Whether `status.json` describes a run that is *still going*.

    The heartbeat is a file, and a file outlives the process that wrote it: a run
    that ended — or was killed — leaves its last phase behind forever. Rendering
    that as "Running now" with a pulsing dot is the same class of lie as showing
    `paid overage enabled` in green, and worse than showing nothing, because the
    elapsed time keeps climbing.

    **The pid alone cannot answer it, which is the trap this walked into.**
    `print_status` asks only whether the pid is alive, and pids are recycled: the
    first time this panel was looked at, the pid in a two-day-old status file had
    been reassigned to an unrelated process, so `_pid_alive` said yes and the rail
    reported a 41-hour dispatch as live. So three things are asked, cheapest last:

    * a **finished record** supersedes the heartbeat — the run said it was done,
      and nothing clears `status.json` on the way out;
    * a heartbeat older than `HEARTBEAT_TRUSTED_FOR` is not believed at all;
    * and only then, is the pid still there.

    What survives: a run killed within the last two hours whose pid was recycled
    inside that window still reads as live. That needs the runner to record its
    own death, which it cannot do when it is killed — so it is left, rather than
    papered over with a shorter window that would call slow cards dead.
    """
    if not status:
        return False
    updated = str(status.get("updated") or "")
    if record.get("complete") and str(record.get("finished") or "") >= updated:
        return False
    # A wall's sleep is a known future wake time, not an unattended heartbeat —
    # `_window_closed` writes it once and then goes quiet for up to `--sessions`
    # windows, which routinely outlasts `HEARTBEAT_TRUSTED_FOR`. Trusting
    # `resume_at` instead of the heartbeat's age is what tells "sleeping through
    # a five-hour reset" apart from "died five hours ago" — the two used to look
    # identical to this function and the panel reported the former as no run at
    # all.
    if status.get("phase") == "sleeping":
        try:
            return dt.datetime.now() < dt.datetime.fromisoformat(str(status.get("resume_at") or ""))
        except (TypeError, ValueError):
            return False
    try:
        if dt.datetime.now() - dt.datetime.fromisoformat(updated) > HEARTBEAT_TRUSTED_FOR:
            return False
    except (TypeError, ValueError):
        return False
    try:
        return _pid_alive(int(status.get("pid") or 0))
    except (TypeError, ValueError):
        return False


#: The largest window budget the panel will offer. `runner --sessions` takes any
#: integer, and this control deliberately does not: the difference between 1 and 2
#: is "stop at the wall" against "sleep through one reset and carry on", which is
#: a decision about tonight. Anything past a few is a decision about the *week*,
#: and a week-long run started by a slider nobody meant to drag past 4 is not a
#: thing this panel should be able to do. The command line still can.
SESSIONS_MAX = 4


def read_sessions(body: dict) -> int:
    """The window budget a run button asked for: 1..`SESSIONS_MAX`, or 0 for none.

    0 means *say nothing to the runner*, which is not the same as 1 — it leaves
    `DEFAULT_SESSIONS` in force, so a browser that sends no such field (an older
    tab left open across an update) starts exactly the run it used to. Out-of-range
    values are refused rather than clamped: a clamp turns a typo into a spend, and
    this is the one control on the bar that can make a run last until morning.
    """
    raw = body.get("sessions")
    if raw in (None, "", 0):
        return 0
    try:
        sessions = int(raw)
    except (TypeError, ValueError):
        raise PanelError(f"sessions must be a whole number, not {raw!r}") from None
    if not 1 <= sessions <= SESSIONS_MAX:
        raise PanelError(f"sessions must be between 1 and {SESSIONS_MAX}, not {sessions}")
    return sessions


#: How long a finished job stays in the rail. Long enough that a classify pass
#: started, watched, and left alone for a coffee still says how it went when you
#: come back; short enough that the rail is not a history page. `/run` is where
#: history lives.
JOB_SHOWN_FOR = dt.timedelta(hours=2)

#: Most rows the rail will carry — counted in *verbs*, since `shown_jobs` gives
#: each label one row however many times it was pressed. Four is more distinct
#: things than are ever usefully in flight at once, and the rail is a status line
#: rather than a list.
JOB_ROWS = 4

_JOB_MARK = {jobs.RUNNING: ("", "running"), jobs.DONE: ("ok", "finished"),
             jobs.STOPPED: ("warn", "stopped"),
             jobs.FAILED: ("bad", "failed"), jobs.LOST: ("warn", "ended without saying how")}


def shown_jobs(all_jobs: list[jobs.Job], *,
               now: dt.datetime | None = None) -> list[tuple[jobs.Job, str]]:
    """The background commands worth a line right now, each with its state:
    everything still running, plus everything that ended recently enough to still
    be news.

    The state is returned rather than left for the caller to ask again, because
    asking is not free: on Windows `jobs.state` answers "is that pid alive" by
    running `tasklist`, and this is a rail on every page. One question per job per
    render is the budget.

    A *failed* job is kept on the same clock as a successful one, deliberately.
    The temptation is to hold failures longer so they cannot be missed — but the
    rail is on every page, and a red line that outlives the moment it described
    becomes the thing you learn to scroll past. The log is kept either way, and
    the job's own page is where an old failure is read on purpose.

    **One row per flow, not one per press.** Pressing Classify three times in a
    coffee break left three `classify` lines stacked in the rail — done, failed,
    failed — which reads as three different things having happened and buries the
    only one that is still true. Karel, 2026-08-21: *"The upper overview panel
    added new runs instead of overwriting one row."* A verb is a *place* in the
    rail now: the newest press of it owns that row and the earlier ones are
    history, which is what `/run` is for. A row is only ever *replaced* by a
    running job, never by a finished one — a second press that was refused in
    under a second must not hide the first one, which is still going.

    **Across verbs, only the single most-recently-started one still counts as
    current.** Karel, 2026-08-22: *"Only the latest (running or finished) should
    show. [If] run [finished, then] ingest [started], run should disappear."*
    The per-verb collapse above answers "what is this verb's row", but nothing
    stopped an *older* verb's finished row from sitting beside a newer verb's —
    a `run` that landed an hour ago is exactly the history this box was never
    meant to be, once `ingest` has since started. A row that is still `RUNNING`
    always stays, whichever verb it is — it is happening *right now*, and no
    later start date changes that — but a finished row is dropped the moment any
    job (running or not) started after it. Ties on `started` (second
    resolution) are not split further; both stay, which favours showing more
    over guessing which press actually came first.
    """
    now = now or dt.datetime.now()
    per_label: dict[str, tuple[jobs.Job, str]] = {}
    order: list[str] = []
    for job in all_jobs:
        status = jobs.state(job, now=now)
        finished = job.finished_at
        if not (status == jobs.RUNNING or (finished and now - finished < JOB_SHOWN_FOR)):
            continue
        existing = per_label.get(job.label)
        if existing is None:
            per_label[job.label] = (job, status)
            order.append(job.label)
        elif status == jobs.RUNNING and existing[1] != jobs.RUNNING:
            per_label[job.label] = (job, status)
        # else: `all_jobs` is newest-first, so whatever is already kept for this
        # label is either the newer press or a still-running older one — both
        # outrank this one.

    rows = [per_label[label] for label in order]
    newest_started = max((j.started_at or dt.datetime.min) for j, _ in rows) \
        if rows else None
    current = [(j, s) for j, s in rows
              if s == jobs.RUNNING or j.started_at == newest_started]
    return current[:JOB_ROWS]


#: The two windows worth a permanent meter, and what to call them. The endpoint
#: returns a dozen-odd others — most null, several with internal codenames that
#: mean nothing here (`nimbus_quill`, `tangelo`, …) — and a rail that renders all
#: of them is mostly noise about buckets this plan does not use.
#:
#: **The parser still reads every key** (`usage._buckets`, and §4 is emphatic that
#: hardcoding the list is how a panel goes blank when the names shift). This is a
#: display choice on top of a complete reading, which is why `shown_buckets` can
#: still surface an unlisted bucket: one that is actually exhausted is the reason
#: a dispatch just got refused, and hiding it would leave the refusal unexplained.
METER_LABELS: dict[str, str] = {"five_hour": "session", "seven_day": "weekly"}


def shown_buckets(snapshot: usage.Snapshot) -> list[usage.Bucket]:
    """The meters to draw: the two named windows, plus anything spent.

    Ordered so the two familiar ones come first and in a stable order — a rail
    whose rows reshuffle between page loads is unreadable.
    """
    by_name = {b.name: b for b in snapshot.buckets}
    shown = [by_name[name] for name in METER_LABELS if name in by_name]
    shown += [b for b in snapshot.buckets
              if b.name not in METER_LABELS and b.exhausted]
    return shown


# ------------------------------------------------------------------ the pages


# ------------------------------------------------------------------ one row


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


def _more(menu: list[str], key: str) -> str:
    """The ⋯ menu: every action on a row except its primary one."""
    items = "".join(m for m in menu if m)
    if not items:
        return '<span class="more-slot"></span>'
    return (f'<details class="more" id="more-{_e(_slug(key))}">'
            f'<summary aria-label="More actions" title="More">&middot;&middot;&middot;</summary>'
            f'<div class="menu">{items}</div></details>')


#: What a row is a row *of*. `card` rows carry `data-card`, which the one-place test counts.
ITEM_KINDS = ("card", "note", "idea", "run", "job", "take", "check")


def _item(ident: str, *, kind: str, title: str = "", chips: list[str] | None = None,
          why: str = "", extra: str = "", primary: str = "",
          menu: list[str] | None = None, marker: str = "&middot;", control: str = "",
          grip: bool = False, card_id: str = "", info: bool = False) -> str:
    """One row — id · title · chips · one primary action · a ⋯ menu for the rest.

    Every list on every page goes through here (`test_every_row_is_an_item`), so the
    rows read as one system. `primary` is the lane's one next step; `menu` is the rest.
    """
    if kind not in ITEM_KINDS:
        raise ValueError(f"unknown row kind {kind!r}")
    if info and card_id:
        head = (f'<span class="id clickable" onclick="toggleInfo(\'{_attr(card_id)}\')" '
                f'title="Goal and how to test">{_e(ident)}</span>')
    else:
        head = f'<span class="id">{_e(ident)}</span>'
    body = [head]
    if title and title != ident:
        body.append(f'<span class="title">{_e(title)}</span>')
    if why:
        body.append(f'<p class="why">{_e(why)}</p>')
    body.append(_meta([c for c in (chips or []) if c]))
    body.append(extra)
    data = f' data-item="{kind}"' + (f' data-card="{_e(card_id)}"' if kind == "card" else "")
    return _row(body="".join(body), acts=primary + _more(menu or [], f"{kind}-{ident}"),
                marker=marker, grip=grip, control=control,
                card_id=card_id if grip else "", data=data)


def _card_item(ctx: Context, card: board.Card, *, chips: list[str] | None = None,
               why: str = "", primary: str = "", menu: list[str] | None = None,
               marker: str = "&middot;", control: str = "", grip: bool = False,
               how_to_test: bool = False) -> str:
    """A board card's row: its own chips first, then the lane's, then the goal box."""
    own = (_tag_chips(card) + [_tier_chip(card)]
           + [c for c in (_local_chip(ctx.root, card), _size_chip(card)) if c])
    return (_item(card.id, kind="card", title=card.title, chips=own + (chips or []),
                  why=why, primary=primary,
                  menu=list(menu or []) + [_act("Read card", href=f"/card/{card.id}")],
                  marker=marker, control=control, grip=grip, card_id=card.id, info=True)
            + _info_box(card, how_to_test=how_to_test))


def _launch(label: str, title: str, path: str, body: dict, *,
            primary: bool = False, disabled: bool = False) -> str:
    """A button that starts an agent: it opens the launch dialog, which carries the
    account, tier, local-model and paid-override choices, and posts on Start."""
    args = _attr(json.dumps([title, path, body]))
    return _act(label, onclick=f"launch.apply(null,{args})", primary=primary,
                disabled=disabled)


def _work_act(*, card: str = "", note: str = "", tier: str = "", worker: str = "",
              lane: str = "", primary: bool | None = None) -> str:
    """`Work on this`: an interactive session on a card or a note, via the launch dialog."""
    target = {"card": card} if card else {"note": note}
    return _launch("Work on this", f"Work on {card or note}", "/api/work", target,
                   primary=bool(card) if primary is None else primary)


def _short_reason(reason: str) -> str:
    """`unattended: false — declared as needing a human` → `unattended: false`."""
    first = reason.split(";")[0]
    return re.split(r" — | \(", first, maxsplit=1)[0].strip()[:48]


def _reason_chip(reason: str) -> str:
    return (f'<span class="chip mute" title="{_attr(reason)}">'
            f'{_e(_short_reason(reason))}</span>')


# ------------------------------------------------------------------ the header


def _runstate_html(ctx: Context) -> str:
    """The header's left half: what is running right now, and the kill switch."""
    status = ctx.rail.run_status
    record = _latest_record(ctx.root)
    stop = _act("Stop run", onclick="post('/api/stop',{})",
                extra='title="The run finishes the card it is on, then stops."')
    if run_is_live(status, record):
        card_id = str(status.get("card") or "") or _kind_label(record)
        if status.get("phase") == "sleeping":
            try:
                resume = dt.datetime.fromisoformat(
                    str(status.get("resume_at") or "")).strftime("%H:%M")
            except (TypeError, ValueError):
                resume = "?"
            said = f"waiting for the usage reset · resumes {resume}"
        else:
            phase = _active_row_lane(status)
            elapsed = elapsed_since(str(status.get("since") or ""))
            said = " · ".join(x for x in (phase, elapsed) if x)
        return (f'<span class="hrun"><span class="live-dot"></span>'
                f'<a class="card-id" href="/running">{_e(card_id)}</a>'
                f'<span class="dim">{_e(said)}</span>{stop}</span>')
    shown = shown_jobs(ctx.jobs)
    if not shown:
        return '<span class="hrun"><span class="dim">No run in progress</span></span>'
    chips = []
    for job, state in shown:
        kind, word = _JOB_MARK.get(state, ("", state))
        if state == jobs.FAILED and job.exit_code is not None:
            word += f" · exit {job.exit_code}"
        dot = '<span class="live-dot"></span>' if state == jobs.RUNNING else ""
        chips.append(f'<a class="hjob" href="/log/{_e(job.ident)}" title="{_attr(job.command)}">'
                     f'{dot}{_chip(job.label, kind)}<span class="dim">{_e(word)}</span></a>')
    return f'<span class="hrun">{"".join(chips)}</span>'


def _meters_html(ctx: Context) -> str:
    """The header's allowance: a short bar per window, and the spend state."""
    snapshot = ctx.rail.snapshot
    out = []
    if not snapshot.fetched:
        out.append(f'<span class="dim" title="{_attr(snapshot.reason or "")}">'
                   f'allowance unknown</span>')
    stale = (f" (as of {snapshot.checked_at:%H:%M})"
             if snapshot.stale and snapshot.checked_at else "")
    for bucket in shown_buckets(snapshot):
        fill = min(100.0, max(0.0, bucket.utilization))
        kind = "bad" if bucket.exhausted else ("warn" if bucket.headroom_pct <= 15 else "")
        resets = f"resets {bucket.resets_at:%d %b %H:%M}" if bucket.resets_at else ""
        label = METER_LABELS.get(bucket.name, bucket.name.replace("_", " "))
        out.append(f'<span class="mi" title="{_attr(resets + stale)}">'
                   f'<span class="mi-l">{_e(label)}</span>'
                   f'<span class="track"><i class="{kind}" style="width:{fill:.0f}%"></i></span>'
                   f'<b>{bucket.utilization:.0f}%</b></span>')
    if snapshot.paid_enabled:
        used = snapshot.paid_used_display
        out.append(_chip(f"paid overage on{f' · {used}' if used else ''}", "warn"))
    elif snapshot.fetched:
        out.append(_chip("paid overage off", "ok"))
    if ctx.rail.identity.fetched and ctx.rail.identity.has_extra_usage_enabled:
        out.append(_chip("API spend enabled — dispatch refused", "bad"))
    rail = ctx.rail
    never = (_chip("dispatch: never", "bad")
             if rail.account_label and rail.account_dispatch == "never" else "")
    out.append(f'<span class="dim">{_e(rail.account_label or "ambient")}</span>{never}')
    return f'<span class="hmeters">{"".join(out)}</span>'


def _statusrail_html(ctx: Context) -> str:
    """The one-line header: run state · allowance · the two launchers."""
    return ('<div class="statusrail" id="statusrail"><div class="hline">'
            + _runstate_html(ctx) + _meters_html(ctx)
            + '<span class="hacts">'
            + _launch("New session", "New session in this repo", "/api/session", {})
            + _act("Options", onclick="launch(null)")
            + '</span></div></div>')


def _account_html(ctx: Context) -> str:
    rail = ctx.rail
    label = rail.account_label or "ambient"
    email = rail.identity.email if rail.identity.fetched else "identity unavailable"
    selector = ""
    if rail.accounts:
        options = ['<option value="">(ambient)</option>']
        for account in rail.accounts:
            selected = " selected" if account.label == rail.account_label else ""
            options.append(f'<option value="{_e(account.label)}"{selected}>'
                           f'{_e(account.label)}</option>')
        selector = ('<select onchange="this.blur();post(\'/api/account\',{label:this.value})">'
                    + "".join(options) + "</select>")
    switch = _act("Switch account", onclick="post('/api/switch-account',{})",
                  extra='title="Opens a terminal running claude auth login."')
    return (f'<div class="opt"><span class="opt-l">Account</span>'
            f'<span><b>{_e(label)}</b> <span class="dim">{_e(email)}</span></span>'
            f'<span>{selector}{switch}</span></div>')


def _tier_html(ctx: Context) -> str:
    """Tier for interactive sessions, and whether it beats a card's own `tier:`."""
    menu = tier_menu(ctx.root)
    if not menu:
        return ('<div class="opt"><span class="opt-l">Tier</span>'
                "<span class=\"dim\">no tier binding — the CLI's default model</span></div>")
    options = ['<option value="">(card\'s own, else the CLI default)</option>']
    for name, model in menu:
        chosen = " selected" if name == _TIER.tier else ""
        options.append(f'<option value="{_e(name)}"{chosen}>'
                       f'{_e(name)} &middot; {_e(model)}</option>')
    ticked = " checked" if _TIER.override else ""
    send = ("post('/api/tier',{tier:document.getElementById('tierpick').value,"
            "override:document.getElementById('tieroverride').checked})")
    return ('<div class="opt"><span class="opt-l">Tier</span>'
            f'<select id="tierpick" onchange="this.blur();{send}">' + "".join(options)
            + '</select>'
            f'<label class="override soft" title="On: this tier wins over the card\'s own.">'
            f'<input type="checkbox" id="tieroverride" data-server="1"{ticked} '
            f'onchange="this.blur();{send}"> Override the card</label></div>')


#: Launches that open an interactive `claude` session. The local model only ever runs
#: dispatched chores and tasks, so the dialog hides its Local row for these.
INTERACTIVE_PATHS = ("/api/session", "/api/talk", "/api/triage", "/api/work",
                     "/api/work-feedback")


def _local_html(ctx: Context) -> str:
    """The local-model toggle — absent on a machine that declares no local model."""
    local = runtimes.local_model(ctx.root)
    if local is None:
        return ""
    agents = ", ".join(local.agents) or "no charters allowlisted"
    ticked = " checked" if local_enabled() else ""
    send = "post('/api/local',{on:document.getElementById('uselocal').checked})"
    title = (f"Dispatched {agents} cards run on {local.model} when its server answers; "
             f"everything else, and every interactive session, runs on cloud."
             + (f" Start the server with {local.launcher}." if local.launcher else ""))
    return (f'<div class="opt" id="opt-local"><span class="opt-l">Local</span>'
            f'<label class="override soft" title="{_e(title)}">'
            f'<input type="checkbox" id="uselocal" data-server="1"{ticked} '
            f'onchange="this.blur();{send}"> Use the local model</label>'
            f'<span class="dim">{_e(agents)} &middot; {_e(local.model)} &middot; '
            f'only when its server answers</span></div>')


def _paid_html(ctx: Context) -> str:
    """The one-shot waiver for the money rule and the account exclusion."""
    return ('<div class="opt"><span class="opt-l">Spend</span>'
            '<label class="override" title="Waives the paid-credit check and the '
            'account exclusion for what you start next. Never remembered.">'
            '<input type="checkbox" id="allowpaid"> Override, this once</label></div>')


def _launch_dialog(ctx: Context) -> str:
    """Start-run / work-on-this: the dispatch options live here, not in the header."""
    return (f'<dialog id="launch" class="launch" '
            f'data-interactive="{_attr(json.dumps(list(INTERACTIVE_PATHS)))}">'
            '<h2 id="launch-title">Options</h2>'
            + _account_html(ctx) + _tier_html(ctx) + _local_html(ctx) + _paid_html(ctx)
            + '<div class="acts">'
            + _act("Start", onclick="startLaunch()", primary=True, extra='id="launch-go"')
            + _act("Close", onclick="closeLaunch()")
            + '</div></dialog>')


def _rail_html(ctx: Context, active: str) -> str:
    counts = ctx.counts()
    try:
        project = manifest.load(ctx.root).project.name
    except ManifestError:
        project = ctx.root.name
    links = []
    for page in PAGES:
        current = ' aria-current="page"' if page == active else ""
        links.append(f'<a href="/{page}"{current}>{PAGE_LABELS[page]} '
                     f'<span class="n">{counts[page]}</span></a>')
    return (
        '<nav class="rail" id="rail">'
        f'<div class="wordmark"><b>Command Center</b><span>{_e(project)}</span></div>'
        f'<div class="pages">{"".join(links)}</div>'
        f'<div class="machine">{"<br>".join(machine_lines(ctx.root))}</div>'
        '</nav>'
    )


# ------------------------------------------------------------------ Queue


def _live_card(ctx: Context) -> str:
    return (str(ctx.rail.run_status.get("card") or "")
            if run_is_live(ctx.rail.run_status, _latest_record(ctx.root)) else "")


def _tonight_section(ctx: Context) -> str:
    tonight = ctx.tonight
    live_card = _live_card(ctx)
    rows = []
    for position, candidate in enumerate(tonight, start=1):
        card = candidate.card
        running = live_card == card.id
        chips = [_e(f"verify: {card.verify}")]
        if card.attempts:
            chips.append(_e(f"attempt {card.attempts + 1}"))
        if running:
            chips.append(_chip("running", "ok"))
        control = (f'<input type="checkbox" class="pick" data-id="{_e(card.id)}" checked '
                   f'aria-label="include {_e(card.id)}">')
        primary = (_act("Running", disabled=True) if running else
                   _launch("Run", f"Run {card.id}", "/api/dispatch", {"card_id": card.id},
                           primary=True))
        menu = [] if running else [_work_act(card=card.id, primary=False)]
        rows.append(_card_item(ctx, card, chips=chips, primary=primary, menu=menu,
                               marker=str(position), control=control, grip=True))
    if ctx.elsewhere:
        rows.append(_group(f"Another machine — {len(ctx.elsewhere)}"))
        for c in ctx.elsewhere:
            rows.append(_card_item(ctx, c.card, chips=[_chip(f"requires {c.card.requires}")],
                                   marker="&mdash;"))
    sessions = (
        '<label class="runopt" title="Usage windows a run may spend: 1 stops at the '
        'limit, more sleeps through each reset and carries on.">Sessions'
        f'<input type="range" id="sessions" min="1" max="{SESSIONS_MAX}" value="1">'
        '<output for="sessions" id="sessionsout">1</output></label>')
    bar = (
        '<div class="barbox">'
        '<p><span id="picked">0 of 0</span> ticked</p>'
        '<label class="runopt">Take first'
        f'<input type="range" id="takefirst" min="0" max="{len(tonight)}" value="{len(tonight)}">'
        '<output for="takefirst" id="takefirstout">0</output></label>'
        + sessions
        + '<div class="acts">'
        + _act("Run the whole queue", onclick="runNight()")
        + _act("Run the ticked", onclick="runTicked()", primary=True)
        + '</div></div>')
    return _section("Tonight", len(tonight), "".join(rows), rows_id="queue",
                    note="Drag to set the order.", bar=bar if tonight else "",
                    empty="Nothing the night can take right now.", sec_id="tonight")


def _chores_section(ctx: Context) -> str:
    rows = []
    for candidate in ctx.chores:
        card = candidate.card
        chips = [_chip("chore", "ok")]
        if card.surface:
            chips.append(_e(card.surface))
        if card.attempts:
            chips.append(_e(f"{card.attempts} attempt(s)"))
        rows.append(_card_item(ctx, card, chips=chips,
                               primary=_work_act(card=card.id, primary=False)))
    bar = ('<div class="barbox"><p>One batch, one suite run over the merged result.</p>'
           '<div class="acts">' + _act("Run chores", onclick="runChores()", primary=True)
           + '</div></div>')
    return _section("Chores", len(ctx.chores), "".join(rows), bar=bar if rows else "",
                    empty="No chores waiting.", sec_id="chores")


def _keyboard_section(ctx: Context) -> str:
    capabilities = host_capabilities(ctx.root)
    rows = []
    for candidate in ctx.do_now:
        card = candidate.card
        chips = [_reason_chip(candidate.reason)]
        if card.requires and card.requires not in capabilities:
            chips.append(_chip(f"needs {card.requires}", "warn"))
        if card.attempts:
            chips.append(_e(f"{card.attempts} attempt(s)"))
        rows.append(_card_item(ctx, card, chips=chips, marker="&rsaquo;",
                               primary=_work_act(card=card.id)))
    return _section("At the keyboard", len(ctx.do_now), "".join(rows),
                    note="The night will not take these.",
                    empty="Nothing needs you at the keyboard.", sec_id="keyboard")


def _render_queue(ctx: Context) -> str:
    return "".join([_tonight_section(ctx), _chores_section(ctx), _keyboard_section(ctx),
                    f'<footer>Board on {_e(current_branch(ctx.root))}</footer>'])


# ------------------------------------------------------------------ Capture


def _note_primary(note: str, route: str) -> str:
    if route in ingest.WRITABLE_ROUTES:
        return _launch("Write the card", f"Write the card for {note}", "/api/ingest/one",
                       {"note": note}, primary=True)
    if route == "triage":
        return _launch("Triage this", f"Triage {note}", "/api/triage", {"note": note},
                       primary=True)
    return _work_act(note=note, primary=True)


def _note_menu(root: Path, note: ingest.Note, route: str) -> list[str]:
    rel = _rel(root, note.path)
    menu = [_act("Edit", href=_body_href(rel, edit=True)),
            _act("Open note", href=_body_href(rel))]
    if route in ingest.WRITABLE_ROUTES or route == "triage":
        menu.append(_work_act(note=note.name, primary=False))
    if route != "triage":
        menu.append(_launch("Triage this", f"Triage {note.name}", "/api/triage",
                            {"note": note.name}))
    menu.append(_act("Done", onclick=f"post('/api/close',{{note:'{_attr(note.name)}'}})",
                     extra='title="Files it in done/ without carding it."'))
    menu.append(_delete_act(note.name, "inbox"))
    return menu


def _delete_act(name: str, lane: str) -> str:
    """`git rm` a bare note, after the typed-name guard in `confirmDeleteNote`."""
    return _act("Delete", onclick=f"confirmDeleteNote('{_attr(name)}','{_attr(lane)}')")


def _inbox_section(ctx: Context) -> str:
    view = ctx.routing
    ordered: dict[str, list[tuple[ingest.Note, ingest.Decision | None]]] = {
        route: [] for route, _, _ in _ROUTE_GROUPS}
    for note in ctx.notes:
        route = note.route if note.route in ordered else ""
        decision = view.of(note.name)
        ordered[route].append(
            (note, decision if decision and decision.route == route else None))
    rows = []
    for route, heading, kind in _ROUTE_GROUPS:
        bucket = ordered.get(route) or []
        if not bucket:
            continue
        rows.append(_group(f"{heading} — {len(bucket)}"))
        for note, decision in bucket:
            chips = [_chip(route, kind)] if route else []
            chips += [f"{note.size} B", _e(_stamp_of(note.path))]
            if decision:
                if decision.confidence != "high":
                    chips.append(_chip(f"confidence {decision.confidence}", "warn"))
                if not decision.dispatchable:
                    chips.append(_chip("needs a human", "warn"))
                if _changed_since(note.path, view.written):
                    chips.append(_chip("edited since routing", "warn"))
            rows.append(_item(note.name, kind="note", marker="&rsaquo;", chips=chips,
                              why=decision.why if decision and decision.why else "",
                              primary=_note_primary(note.name, route),
                              menu=_note_menu(ctx.root, note, route)))
    writable = sum(1 for note in ctx.notes if note.route in ingest.WRITABLE_ROUTES)
    bar = ('<div class="barbox"><div class="acts">'
           + _act("New note", onclick="openEditor('new-note')")
           + _launch(f"Write {writable} card(s)" if writable else "Write the cards",
                     "Write the cards for the routed notes", "/api/ingest", {"write": True},
                     disabled=not writable)
           + _launch("Classify all", "Classify the inbox", "/api/ingest", {"scribe": True},
                     primary=True)
           + '</div></div>'
           + _editor("new-note", save="saveNew('new-note','inbox')", named=True,
                     placeholder="One or two sentences is enough."))
    if not view.known and not any(note.route for note in ctx.notes):
        note_line = "Not classified yet."
    else:
        when = f"{view.written:%d %b %H:%M}" if view.written else "at an unrecorded time"
        unrouted = len(ordered.get("") or [])
        note_line = f"Routed {when}" + (f" · {unrouted} new since" if unrouted else "")
    return _section("Inbox", len(ctx.notes), "".join(rows), note=note_line, bar=bar,
                    empty="The inbox is empty.", sec_id="inbox")


def _ideas_section(ctx: Context) -> str:
    """Idea *names* only — the panel never opens an idea to summarise it."""
    rows = []
    for name in ctx.ideas:
        path = f"{board.board_rel(ctx.root).as_posix()}/{board.PRIVATE_LANE}/{name}"
        rows.append(_item(name, kind="idea",
                          primary=_act("Promote", primary=True,
                                       onclick=f"post('/api/promote',{{name:'{_attr(name)}'}})"),
                          menu=[_act("Read", href=_body_href(path)),
                                _act("Edit", href=_body_href(path, edit=True)),
                                _delete_act(name, board.PRIVATE_LANE)]))
    bar = ('<div class="barbox"><div class="acts">'
           + _act("New idea", onclick="openEditor('new-idea')", primary=True)
           + '</div></div>'
           + _editor("new-idea", save="saveNew('new-idea','ideas')", named=True,
                     placeholder="Half a thought is fine."))
    return _section("Ideas", len(ctx.ideas), "".join(rows),
                    note="Private. Promote moves one to the inbox.", bar=bar,
                    empty="No ideas parked.", sec_id="ideas")


def _render_capture(ctx: Context) -> str:
    return _inbox_section(ctx) + _ideas_section(ctx) + (
        f'<footer>{len(ctx.notes)} note(s) · {len(ctx.ideas)} idea(s)</footer>')


# ------------------------------------------------------------------ You


def _decide_section(ctx: Context) -> str:
    rows = []
    for card in ctx.decisions:
        subs = decide.parse(card.text)
        questions, options = len(subs), sum(len(s.options) for s in subs)
        chips = [_e(str(card.fields.get("created", "")))]
        if questions:
            chips.append(_chip(_asks(questions, options), "warn"))
        rows.append(_card_item(ctx, card, chips=chips, marker="?",
                               primary=_act("Answer", href=f"/decide/{card.id}",
                                            primary=True)))
    return _section("Decide", len(ctx.decisions), "".join(rows),
                    note="Nothing else moves until this does.",
                    empty="Nothing is waiting on a decision.", sec_id="decide")


def _playthrough_section(ctx: Context) -> str:
    by_surface: dict[str, list[board.Card]] = {}
    for card in ctx.testing:
        by_surface.setdefault(card.surface or "unsorted", []).append(card)
    rows = []
    for surface in sorted(by_surface):
        cards = by_surface[surface]
        rows.append(_group(f"{surface} — {len(cards)}"))
        for card in cards:
            branch = card.fields.get("branch") or f"ai/{card.id}"
            chips = []
            if stat := diff_stat(ctx.root, ctx.base, branch):
                chips.append(_e(stat))
            if card.attempts:
                chips.append(_e(f"{card.attempts} attempt(s)"))
            if card.verify == "review":
                chips.append(_chip("verify: review", "ok"))
            control = (f'<input type="checkbox" class="tick" data-id="{_e(card.id)}" '
                       f'aria-label="{_e(card.id)} verified">')
            menu = [_act("Not OK", onclick=f"openEditor('feedback-{_attr(card.id)}')"),
                    _launch("Open inline", f"Reopen {card.id}'s session",
                            "/api/work-feedback", {"card_id": card.id}),
                    _act("Diff", href=f"/diff/{card.id}")]
            rows.append(_card_item(ctx, card, chips=chips, control=control, menu=menu,
                                   primary=_act("Mark OK", primary=True,
                                                onclick=f"markOK(this,'{_attr(card.id)}')"),
                                   how_to_test=True))
            rows.append(_editor(f"feedback-{card.id}",
                                save=f"submitFeedback('{_attr(card.id)}')",
                                placeholder="What was wrong — it goes onto the card and "
                                            "sends it back to tasks/."))
    bar = ('<div class="barbox"><p><span id="ticked">Nothing ticked.</span></p>'
           '<div class="acts">' + _act("Mark ticked OK", onclick="saveTicked()")
           + '</div></div>')
    return _section("Play through", len(ctx.testing), "".join(rows),
                    note=f"On {ctx.base}, grouped by where you would see it.",
                    bar=bar if ctx.testing else "",
                    empty="Nothing is waiting to be played.", sec_id="playthrough")


def _blocked_section(ctx: Context) -> str:
    """`blocked/` (reviewed, will not land) and `failed/` (out of attempts)."""
    rows = []
    for card in ctx.blocked:
        branch = card.fields.get("branch") or f"ai/{card.id}"
        why = (board.section(card.text, "Merge") or "").strip()
        chips = [_e(stat) for stat in [diff_stat(ctx.root, ctx.base, branch)] if stat]
        chips.append(_chip("reviewed ok · will not merge", "warn"))
        rows.append(_card_item(ctx, card, chips=chips, why=why[:400], marker="!",
                               primary=_work_act(card=card.id),
                               menu=[_act("Diff", href=f"/diff/{card.id}")]))
    for card in ctx.failed:
        why = (board.section(card.text, "Error") or "").strip()
        chips = [_chip(f"failed · {card.attempts} attempt(s)"
                       if card.attempts else "failed", "bad")]
        rows.append(_card_item(ctx, card, chips=chips, why=why[:400], marker="!",
                               primary=_work_act(card=card.id)))
    return _section("Blocked", len(ctx.blocked) + len(ctx.failed), "".join(rows),
                    note="Only your hands move these.",
                    empty="Nothing is blocked or failed.", sec_id="blocked")


def _review_section(ctx: Context) -> str:
    """`review/`: nothing schedules a reviewer, so each row carries its own button."""
    stuck = 0
    rows = []
    for card in ctx.review:
        reason = drain.skip_reason(ctx.root, ctx.base, card)
        branch = card.fields.get("branch") or f"ai/{card.id}"
        chips = [_e(stat) for stat in [diff_stat(ctx.root, ctx.base, branch)] if stat]
        if reason:
            chips.append(_chip("left alone", "mute"))
            primary = ""
        else:
            stuck += 1
            primary = _launch("Review it", f"Review {card.id}", "/api/review",
                              {"card_id": card.id}, primary=True)
        rows.append(_card_item(ctx, card, chips=chips, why=reason, marker="!",
                               primary=primary,
                               menu=[_act("Diff", href=f"/diff/{card.id}")]))
    bar = ('<div class="barbox"><div class="acts">'
           + _launch(f"Review all ({stuck})", "Review the review/ lane", "/api/review-all",
                     {}, primary=True)
           + '</div></div>') if stuck else ""
    return _section("Review", len(ctx.review), "".join(rows),
                    note="Waiting for a reviewer run.", bar=bar,
                    empty="Nothing is waiting for review.", sec_id="review")


def _render_you(ctx: Context) -> str:
    return "".join([_decide_section(ctx), _playthrough_section(ctx), _blocked_section(ctx),
                    _review_section(ctx), _audio_section(ctx),
                    f'<footer>{len(ctx.testing)} card(s) on {_e(ctx.base)} to play</footer>'])


# ------------------------------------------------------------------ Running


def _job_tail(ctx: Context, job: jobs.Job) -> str:
    """A job's own progress: per-note rows for `ingest`, the raw tail otherwise."""
    text = jobs.read_log(ctx.root, job.ident, tail=JOB_TAIL_BYTES)
    if not text.strip():
        return ""
    if job.label == "ingest":
        return _ingest_rows(ingest.parse_progress(text))
    lines = text.strip().splitlines()[-JOB_TAIL_LINES:]
    return f'<pre class="tail">{_e(chr(10).join(lines))}</pre>'


def _ingest_rows(progress: ingest.Progress) -> str:
    """One row per note, with what became of it — for a classify or a carding pass."""
    marks = {"m-ok": "&check;", "m-bad": "&times;", "m-now": "&middot;"}
    rows = []
    for item in progress.items:
        css, glyph = ingest.ITEM_STATES.get(item.state, ("m-wait", "&middot;"))
        said = {"done": "carded as", "bounced": "bounced to triage —",
                "stranded": "stranded —"}.get(item.state, "")
        rows.append(_item(item.name, kind="job", marker=f'<span class="{css}">{glyph}</span>',
                          chips=[_e(item.route or item.state)],
                          why=f"{said} {item.detail}".strip()))
    routed = any(item.route for item in progress.items)
    if progress.routes and not routed:
        counts = " · ".join(f"{n} {route}" for route, n in progress.routes.items())
        rows.append(_item("routed", kind="job", marker=marks["m-ok"],
                          chips=[_e(f"{progress.total} note(s)")], why=counts))
    if not rows:
        rows.append(_item(progress.phase or "starting", kind="job",
                          chips=[_e(f"{progress.total} note(s)")],
                          why="one pass over the whole inbox"))
    elif progress.total and not routed and not progress.routes:
        rows.append(_item(f"{progress.finished}/{progress.total}", kind="job",
                          why=progress.phase))
    return "".join(rows)


def _job_item(ctx: Context, job: jobs.Job, status: str, *, detail: bool) -> str:
    _, word = _JOB_MARK.get(status, ("", status))
    said = word if status != jobs.FAILED else f"{word} — exit {job.exit_code}"
    marker = ('<span class="live-dot"></span>' if status == jobs.RUNNING else
              {jobs.DONE: '<span class="m-ok">&check;</span>',
               jobs.FAILED: '<span class="m-bad">&times;</span>'}.get(status, "?"))
    chips = [_e(said), _e(jobs.elapsed(job))]
    if job.started_at:
        chips.append(_e(f"{job.started_at:%d %b %H:%M}"))
    return (_item(job.label, kind="job", marker=marker, chips=chips, why=job.command,
                  primary=_act("Output", href=f"/log/{job.ident}"))
            + (_job_tail(ctx, job) if detail else ""))


def _now_section(ctx: Context) -> str:
    """Everything automated that is going right now, wherever it was started."""
    rows = []
    status = ctx.rail.run_status
    record = _latest_record(ctx.root)
    if run_is_live(status, record):
        card_id = str(status.get("card") or "") or _kind_label(record)
        attempt = status.get("attempt")
        chips = [_e(str(status.get(k) or "")) for k in ("worker", "model")]
        if attempt:
            chips.append(_e(f"attempt {attempt}"))
        if elapsed := elapsed_since(str(status.get("since") or "")):
            chips.append(_e(elapsed))
        if status.get("card"):
            telemetry = read_telemetry(attempt_dir(ctx.root, card_id, int(attempt or 1)))
            if telemetry.get("turns"):
                chips.append(_e(f"{telemetry['turns']} turns"))
        rows.append(_item(card_id, kind="run", marker='<span class="live-dot"></span>',
                          chips=chips, extra=_phases_html(str(status.get("phase") or "")),
                          primary=_act("Stop run", onclick="post('/api/stop',{})")))
    count = len(rows)
    for job in ctx.jobs:
        if jobs.state(job) == jobs.RUNNING:
            count += 1
            rows.append(_job_item(ctx, job, jobs.RUNNING, detail=True))
    return _section("Running now", count, "".join(rows),
                    empty="Nothing automated is running.", sec_id="running")


def _roster_item(ctx: Context, entry: dict, kind: str) -> str:
    """One card the run has a verdict for."""
    outcome = str(entry.get("outcome", ""))
    cls = ("m-ok" if outcome in run_record.LANDED_OUTCOMES else
           "m-bad" if outcome in run_record.FAILED_OUTCOMES else
           "m-now" if outcome in run_record.DECISION_OUTCOMES else "m-wait")
    cls = _live_mark(ctx, entry, cls)
    glyph = {"m-ok": "&check;", "m-bad": "&times;", "m-now": "?"}.get(cls, "&middot;")
    card_id = str(entry.get("card", ""))
    out_dir = attempt_dir(ctx.root, card_id, int(entry.get("attempt") or 1))
    telemetry = read_telemetry(out_dir)
    session = session_id(out_dir)
    took = f"{telemetry['wall_s'] / 60:.0f} min" if telemetry.get("wall_s") else ""
    cost = entry.get("cost_usd") or 0
    return _item(card_id, kind="run", marker=f'<span class="{cls}">{glyph}</span>',
                 chips=[_e(kind), _e(_live_lane(ctx, entry)), _e(took), _e(f"${cost:.2f}")],
                 why=_said(entry),
                 primary=_launch("Talk", f"Talk to {card_id}'s session", "/api/talk",
                                 {"session_id": session}) if session else "")


def _run_section(ctx: Context) -> str:
    """The newest run's roster — every card it set out to work, and where each is."""
    record = _latest_record(ctx.root)
    dispatched = record.get("dispatched", [])
    planned = record.get("planned", [])
    by_card = {str(d.get("card") or ""): d for d in dispatched}
    status = ctx.rail.run_status
    live = run_is_live(status, record)
    active_id = str(status.get("card") or "") if live else ""

    def pending(card_id: str, kind: str) -> str:
        active = live and card_id == active_id
        return _item(card_id, kind="run", marker=("&rsaquo;" if active else "&middot;"),
                     chips=[_e(kind), _e(_active_row_lane(status) if active else "queued")])

    body: list[str] = []
    seen: set[str] = set()
    for item in planned:
        card_id = str(item.get("card") or "")
        seen.add(card_id)
        kind = _QUEUE_LABEL.get(str(item.get("queue") or ""), "task")
        entry = by_card.get(card_id)
        body.append(_roster_item(ctx, entry, kind) if entry is not None
                    else pending(card_id, kind))
    for entry in dispatched:
        card_id = str(entry.get("card") or "")
        if card_id not in seen:
            seen.add(card_id)
            body.append(_roster_item(ctx, entry, _roster_kind(record, entry)))
    if live and not planned:
        for candidate in ctx.tonight:
            if candidate.card.id not in seen:
                seen.add(candidate.card.id)
                body.append(pending(candidate.card.id, "task"))
    if str(record.get("kind") or "") in ("chores", "both"):
        for note in record.get("notes", []):
            body.append(_item("batch", kind="run", why=str(note.get("message", ""))))
        if reason := str(record.get("stop_reason") or ""):
            body.append(_item("stopped", kind="run", marker='<span class="m-bad">&times;</span>',
                              why=reason))
    started = str(record.get("started", ""))
    today = started[:10] == dt.date.today().isoformat()
    heading = "This run" if live else ("Today's run" if today else "Last run")
    when = ("today " + started[11:16] if today
            else f"{started[:10]} {started[11:16]}") if started else ""
    note = (f"{_kind_label(record)} · {when} on {record.get('host', '?')} · "
            f"{'in flight' if live else ('complete' if record.get('complete') else 'ended early')}"
            f" · ${record.get('cost_usd', 0) or 0:.2f}") if started else ""
    return _section(heading, len(seen), "".join(body), note=note,
                    empty="No run recorded on this machine yet.", sec_id="lastrun")


def _render_running(ctx: Context) -> str:
    out = [_now_section(ctx)]
    source, _, newest_job = latest_activity(ctx)
    if source == "job" and newest_job is not None:
        out.append(_section(f"Last {newest_job.label}", 1,
                            _job_item(ctx, newest_job, jobs.state(newest_job), detail=True),
                            note="the most recent thing that ran here", sec_id="lastjob"))
    out.append(_run_section(ctx))
    out.append('<footer>' + _act("Earlier runs and jobs", href="/history") + '</footer>')
    return "".join(out)


def _render_history(ctx: Context) -> str:
    """Earlier runs and everything the buttons started — one click from Running."""
    rows = []
    earlier = run_record.read_all(ctx.root)[1:6]
    for old in earlier:
        mark = ('<span class="m-ok">&check;</span>' if old.get("complete")
                else '<span class="m-bad">&times;</span>')
        when = str(old.get("started", ""))[:16].replace("T", " ")
        took = span(str(old.get("started", "")), str(old.get("finished") or ""))
        cost = old.get("cost_usd", 0) or 0
        rows.append(_item(when, kind="run", marker=mark,
                          chips=[_e(str(old.get("kind", ""))), _e(took), _e(f"${cost:.2f}")],
                          why=str(old.get("stop_reason")
                                  or f"{len(old.get('dispatched', []))} dispatched")))
    out = [_section("Earlier runs", len(earlier), "".join(rows),
                    empty="No earlier runs on this machine.", sec_id="earlierruns")]
    jobs_rows = []
    for job in ctx.jobs[:JOBS_ON_RUN_PAGE]:
        status = jobs.state(job)
        if status != jobs.RUNNING:
            jobs_rows.append(_job_item(ctx, job, status, detail=False))
    out.append(_section("Earlier from here", len(jobs_rows), "".join(jobs_rows),
                        empty="No button here has started anything yet.",
                        sec_id="jobhistory"))
    out.append(f'<footer>Records in {run_record.DIR.as_posix()}</footer>')
    return "".join(out)


def _asks(questions: int, options: int) -> str:
    """The parked-card chip: how many things it asks, and how much is pre-written.

    Two numbers in one phrase because they answer different worries — "how much of
    my evening is this" and "do I have to compose the answer myself" — and because
    the previous single number silently answered the second while being read as the
    first.
    """
    head = "1 question" if questions == 1 else f"{questions} questions"
    return f"{head} · {options} option(s) offered" if options else head


def _stamp_of(path: Path) -> str:
    try:
        return f"written {dt.datetime.fromtimestamp(path.stat().st_mtime):%d %b %H:%M}"
    except OSError:
        return ""


#: Route → (group heading, chip kind). The headings are the report's own, minus
#: its second-person blurbs; the order is the order `ingest.report` lists them, so
#: the page and the file read the same way round. `""` is the note no pass has
#: reached yet, which is the only bucket the page used to have.
_ROUTE_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("", "Not yet classified", "warn"),
    ("triage", "Waiting on triage — the expensive route", "warn"),
    ("chore", "Chores — batched overnight", "ok"),
    ("scribe", "Scribe — full cards, reviewed on their own", "ok"),
    # A routing pass cards an inline note into `tasks/` on the spot, so this group is
    # empty on every ordinary path and the Do now page is where such work appears.
    # It is kept because `route:` is a field in a file somebody edits in Obsidian: a
    # note set to `inline` by hand has not been through `apply_routing` yet, and a
    # group that does not exist is how a note becomes invisible rather than pending.
    ("inline", "Inline — carded on the next pass", "mute"),
)


def _changed_since(path: Path, when: dt.datetime | None) -> bool:
    """Whether the note was written after the routing pass that judged it.

    A minute of slack: the report's own timestamp is minute-resolution, so a note
    saved in the same minute as the pass that read it would otherwise read as
    having changed underneath it.
    """
    if when is None:
        return False
    try:
        touched = dt.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return touched - when > dt.timedelta(minutes=1)


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _body_href(rel: str, *, edit: bool = False) -> str:
    """The address of one board file's page, in the mode asked for.

    **Percent-encoded, because board files are named the way people name
    thoughts** — "Regenerate soundtrack.md", "Animation – attack.md". A browser
    will encode a space in an `href` for you; nothing guarantees it for an em
    dash, and an address that only works because the client was forgiving is one
    that breaks the first time something else reads it. `safe="/"` keeps the path
    separators as separators. The handler `unquote`s this back before it looks for
    the file.
    """
    return f"/body/{quote(rel, safe='/')}" + ("?edit=1" if edit else "")


def _editor(slug: str, *, save: str, named: bool = False, placeholder: str = "") -> str:
    name_field = ('<input type="text" placeholder="filename.md">' if named else "")
    return (f'<div class="editor" id="ed-{_e(slug)}">{name_field}'
            f'<textarea placeholder="{_e(placeholder)}"></textarea>'
            f'<div class="acts">{_act("Save", onclick=save, primary=True)}'
            f'{_act("Cancel", onclick=f"closeEditor(&#39;{slug}&#39;)")}</div></div>')


def _landed_lane(entry: dict) -> str:
    """`→ testing/` out of `settle`'s full sentence.

    `landed` reads *"card-id: → testing/ (reviewed ok, rebased ai/… onto test and
    merged)"* — the whole account, which is the right thing to keep in the record
    and the wrong thing to put in a table column. The lane is the part this column
    is for; the rest is the row's `said`.
    """
    landed = str(entry.get("landed") or "")
    if "→" in landed:
        return "→ " + landed.split("→", 1)[1].split("(")[0].strip()
    return str(entry.get("outcome") or "")


def _live_lane(ctx: "Context", entry: dict) -> str:
    """Where the card is **now**, falling back to what the dispatch recorded.

    The run record is a dispatch-time snapshot and the review stage runs after it:
    `enemy-position-knowledge` (2026-09-04) had its row written at 01:33 with
    outcome `review`, then the reviewer returned `needs_decision` and `settle`
    moved it to `needs-decision/` at 05:59 — and this table went on reporting
    `review` all morning, next to an answer section that was already showing the
    reviewer's question. Karel: *"when reviewer decides, it should be displayed in
    the result section"*.

    Re-reading the board is the same correction `_run_sequence` already makes for
    `needs_fix` — the outcome string alone cannot tell you what the reviewer did
    with the branch afterwards. The record stays untouched: it is the honest
    history of what was dispatched, and this column answers "where did it
    end up", which are two different questions that only look alike when nothing
    moved in between.
    """
    card_id = str(entry.get("card") or "")
    fresh = board.find(ctx.root, card_id) if card_id else None
    if fresh is None:
        # Off the board entirely — most often `done/` after a human filed it, or a
        # card deleted since. The snapshot is the only answer left.
        return _landed_lane(entry)
    recorded = _landed_lane(entry)
    now = f"→ {fresh.lane}/"
    # Only mark it when the two genuinely disagree; the overwhelming case is a
    # card that settled where it was dispatched to and needs no annotation.
    if recorded.rstrip("/") in (now.rstrip("/"), ""):
        return now
    return f"{now} (was {recorded.removeprefix('→ ').strip()})"


#: Lane → the mark the Run table puts in front of a row, for a card still on the
#: board. It outranks the recorded outcome because the lane is the later fact:
#: `review` is in `LANDED_OUTCOMES`, so a card the reviewer escalated to
#: `needs-decision/` rendered a green tick — the one reading a human is least
#: likely to look twice at, on the one row that most needed them to.
_LANE_MARK: dict[str, str] = {
    "testing": "m-ok", "done": "m-ok",
    "needs-decision": "m-now", "blocked": "m-now",
    "failed": "m-bad",
}


def _live_mark(ctx: "Context", entry: dict, fallback: str) -> str:
    """The row's mark, preferring where the card actually is over what it returned."""
    card_id = str(entry.get("card") or "")
    fresh = board.find(ctx.root, card_id) if card_id else None
    if fresh is None:
        return fallback
    return _LANE_MARK.get(fresh.lane, fallback)


def _said(entry: dict, limit: int = 90) -> str:
    """One line of what the reviewer or the failure said, bounded.

    A worker's `detail` can be several paragraphs. Rendered whole it stretched the
    roster far past the window and pushed every other column into a two-character
    ribbon — so it is cut to its first line, and to `limit` characters of that.
    """
    text = " ".join(str(entry.get("detail") or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


#: How far back the Run page's job history reaches. The rail shows what is news
#: (`JOB_SHOWN_FOR`, two hours); this is the page you open to ask what happened,
#: so it shows everything still on file — `jobs.KEEP` records — and lets the
#: reader decide what is old.
JOBS_ON_RUN_PAGE = jobs.KEEP

#: How many jobs get their output inlined under their row, and how much of it.
#: Two is the running one plus the last one, which is "what is happening" and
#: "what just happened" — the two questions the page is opened with. More than
#: that and the history below is pushed off the screen by logs nobody asked for.
JOB_TAILS_INLINE = 2
JOB_TAIL_LINES = 14
#: Read a little more than `JOB_TAIL_LINES` could need, so the last lines are
#: whole ones. Reading the whole file to show fourteen lines of it would make
#: every page load carry a night's worth of `runner` output.
JOB_TAIL_BYTES = 8_000


#: How a run record's `kind` reads on the page. The record's own vocabulary is the
#: runner's argv (`run` is a night, `card` is one named card, `chores` is a batch,
#: `both` is one run that worked the chore batch and then the task queue); none of
#: those words says on its own what it was, which is precisely how the heading
#: managed to present a fortnight-old night as the thing that just happened.
_KINDS = {"run": "night", "card": "single card", "chores": "chore batch",
          "both": "chores + night"}


#: How a planned entry's `queue` reads in the roster's type column. The record
#: stores the queue's own name; one row wants the singular noun for one card.
_QUEUE_LABEL = {"chores": "chore", "tasks": "task"}


def _roster_kind(record: dict, entry: dict) -> str:
    """The type cell for a dispatched row the run's plan does not name.

    Reached only for a `--card` run or a record written before the roster was
    recorded up front, so it reads the record's own kind rather than guessing per
    card: a `chores` record dispatched nothing that was not a chore.
    """
    return "chore" if str(record.get("kind") or "") == "chores" else "task"


def _kind_label(record: dict) -> str:
    kind = str(record.get("kind") or "")
    return _KINDS.get(kind, kind or "run")


def latest_activity(ctx: Context) -> tuple[str, dict, jobs.Job | None]:
    """The newest automated process of any kind: `(source, record, job)`.

    `source` is `"record"` or `"job"`; exactly one of the latter two is meaningful.

    **Why this is not just `_latest_record`.** Two families of thing run here and
    only one of them wrote a record. A night, a single card and (since 2026-08-18) a
    chore batch open a `run_record`; `ingest`, `preflight`, `drain` and `update` do
    not, because they dispatch no cards and a record shaped around a dispatch list
    would be mostly empty fields. They leave a `jobs` record instead. Reading only
    the first family is what produced the bug this function exists for: a batch that
    finished at 11:41 was invisible, and the page confidently reported a night from
    2026-08-02 under a heading that says "Last run".

    So both families are compared on the one axis they share — when they started —
    and the newest wins. Ties go to the record, which is the richer artefact: a
    batch started from a panel button has *both*, and the record is the one that can
    say what the batch did rather than what command line started it.
    """
    record = _latest_record(ctx.root)
    newest_job = ctx.jobs[0] if ctx.jobs else None
    record_at = str(record.get("started") or "")
    job_at = ""
    if newest_job is not None and newest_job.started_at is not None:
        job_at = newest_job.started_at.isoformat()
    if newest_job is not None and job_at > record_at:
        return "job", {}, newest_job
    if record_at:
        return "record", record, None
    return ("job", {}, newest_job) if newest_job is not None else ("none", {}, None)


def _audio_section(ctx: Context) -> str:
    """Every audio artefact a run harvested, with a player and the
    `candidates.json` facts beside each take — the review surface the card's
    own note says everything before this stopped short of
    (`audio-asset/SKILL.md` §5: an unattended run generates and validates, but
    "never adopts unheard").

    **Absent entirely, not merely empty, when nothing has been harvested** —
    `ctx.audio` is `[]` in every repo that has never generated audio, and a
    permanent "Audio candidates" heading with nothing under it is exactly the
    UI the acceptance criteria rule out for that case.

    A take whose validator verdict failed is shown with its `problems`, not
    filtered out — "the generator put almost nothing in the window" is
    information Karel needs when deciding whether to re-roll, and hiding it
    would be this page making that call for him.
    """
    blocks, total = _audio_blocks(ctx.root, ctx.audio)
    if not blocks:
        return ""
    return _section("Audio candidates", total, blocks,
                    note="What a run generated but nobody has heard yet.",
                    sec_id="audio")


def _audio_blocks(root: Path, groups: list[AudioGroup]) -> tuple[str, int]:
    """`(markup, take count)` for a list of audio groups — shared by the Run page's
    section and a parked card's own page (`_decide_audio`), so the two cannot
    drift the way `_image_blocks` exists to prevent for images."""
    picks = read_audio_picks(root)
    blocks = []
    total = 0
    for group in groups:
        picked_rel = picks.get(group.pick_key(), "")
        rows = []
        for take in group.takes:
            total += 1
            meta = take.meta
            chips = []
            if meta:
                if meta.get("seconds") is not None:
                    chips.append(_e(f"{float(meta['seconds']):.2f}s"))
                if meta.get("seed") is not None:
                    chips.append(_e(f"seed {meta['seed']}"))
                if meta.get("generator"):
                    chips.append(_e(str(meta["generator"])))
                if meta.get("post_flags"):
                    chips.append(_e(str(meta["post_flags"])))
                if "ok" in meta:
                    chips.append(_chip("failed validation", "warn") if meta.get("ok") is False
                                 else _chip("ok", "ok"))
            else:
                # Degrades honestly rather than pretending to know: the player
                # still works, the facts beside it read as unknown.
                chips.append(_chip("no candidates.json — metadata unknown", "mute"))
            if take.rel == picked_rel:
                chips.append(_chip("picked", "ok"))
            problems = meta.get("problems") or []
            why = " — ".join(x for x in (str(meta.get("prompt") or ""),
                                         "; ".join(str(p) for p in problems)) if x)
            player = (f'<audio controls preload="none" '
                      f'src="/audio/{quote(take.rel, safe="/")}"></audio>')
            acts = (_act("Picked", disabled=True) if take.rel == picked_rel else
                    _act("Pick", onclick=f"post('/api/audio/pick',"
                                         f"{{key:'{_attr(group.pick_key())}',"
                                         f"rel:'{_attr(take.rel)}'}})"))
            rows.append(_item(take.id, kind="take", marker="&#9835;", chips=chips,
                              why=why, extra=player, primary=acts))
        heading = group.sound or group.rel_dir
        when = f" · {group.generated}" if group.generated else ""
        blocks.append(_group(f"{heading} — {group.card} attempt {group.attempt}{when}")
                     + "".join(rows))
    return "".join(blocks), total


def _image_blocks(root: Path, groups: list[ImageGroup]) -> tuple[str, int]:
    """`(markup, shot count)` for a list of image groups — the candidate strip.

    **Zoomed, nearest-neighbour, on a checkerboard.** These are 32x32 pixel-art
    icons; at 1:1 they are a smudge the size of this sentence's full stop, and a
    bilinear upscale of pixel art is a blur that hides exactly the edge quality
    being judged (`.shot img` — `image-rendering: pixelated`). The checkerboard
    is not decoration either: half these sprites are dark and half are light,
    and a transparent background over a flat panel makes one of the two
    invisible.

    Every candidate in a group is shown, including ones the checker argued
    against. Its `notes` are the fifteen-second version of the decision, not a
    filter: "d drifts toward cyan" is a reason to look at d, not a reason to
    hide it.

    Empty markup for an empty list, which is what lets the caller keep the
    absent-not-empty rule with one `if`.
    """
    picks = read_image_picks(root)
    blocks = []
    total = 0
    for group in groups:
        # Keyed on the card, so a card whose second attempt re-rolled shows the
        # pick against whichever shot was chosen and nothing against the rest.
        picked_rel = picks.get(group.card, "")
        shots = []
        for shot in group.shots:
            total += 1
            chips = [_chip(f"{shot.width}x{shot.height}") if shot.width and shot.height
                     else _chip("unreadable PNG header", "warn")]
            if shot.best:
                chips.append(_chip("checker's pick", "ok"))
            if shot.rel == picked_rel:
                chips.append(_chip("picked", "ok"))
            act = (_act("Picked", disabled=True) if shot.rel == picked_rel else
                   _act("Pick", onclick=f"post('/api/image/pick',"
                                        f"{{card:'{_attr(group.card)}',"
                                        f"rel:'{_attr(shot.rel)}'}})"))
            shots.append(
                f'<div class="shot{" best" if shot.best else ""}">'
                f'<img src="/image/{quote(shot.rel, safe="/")}" '
                f'alt="{_e(shot.id)}" loading="lazy">'
                f'<span class="shot-name">{_e(shot.id)}</span>'
                f'{_meta(chips)}<div class="acts">{act}</div></div>')
        chips = ([_chip(f"checker: {group.verdict}",
                             "ok" if group.verdict == "pass" else "warn")]
                      if group.verdict else
                      # Degrades honestly rather than pretending to know, the way
                      # a missing `candidates.json` does one section up.
                      [_chip("no checker verdict on disk", "mute")])
        blocks.append(_group(f"{group.card} attempt {group.attempt} — {group.rel_dir}")
                      + _item(group.rel_dir, kind="take", marker="&#9635;", chips=chips,
                              why=group.notes or "",
                              extra=f'<div class="shots">{"".join(shots)}</div>'))
    return "".join(blocks), total


def _decide_images(root: Path, card_id: str) -> str:
    """This card's own harvested candidates, for the page where its question is
    read and its answer is typed. **The one place candidates are drawn.**

    **The gap this closes is the complaint in miniature.** `_park_for_pick`
    writes a question that says "N candidates were produced and none installed";
    without this, reading it here and then having to go to another page to see
    what it is *about* is exactly the friction the parking was meant to remove.

    **The Run page used to draw them too, and that was the defect** (Karel,
    2026-09-09: *"the run summary is filled with images … I don't think this
    belongs in run summary. In the card parked in needs-decision — great"*). It
    listed every group `.ai/runs/` still held, for every card, forever: nothing
    there was keyed to a card that is *waiting on a choice*, so a card answered
    weeks ago and long since in `done/` kept its candidates on the page, and the
    intermediate `raw/` and `.tmp/` harvest directories showed up beside the
    finals as equal candidates. Six of the seven groups on the page had no
    question behind them. Filtered to one card, that cannot happen: the page
    exists because the card is parked, and it goes when the card does.

    A cross-card overview may still be worth having, but as its own page with
    its own cleanup rule — not as a permanent block on the page that answers
    "what happened last night".

    Empty string when this card harvested no images, so an ordinary parked card
    is unchanged: absent, not an empty heading.

    **An `<h3>`, not a `_section`.** `render_document` puts everything inside one
    `<div class="doc">` within one `<section>`, so a nested `_section` would sit
    under `.doc h2`'s rules and read as a page-within-a-page. This matches the
    "Already on record" block a few lines down, which is the same kind of thing:
    a labelled part of one document. The *candidates* themselves go through the
    shared `_image_blocks`, which is where the drift would have been.
    """
    groups = [g for g in scan_image_candidates(root) if g.card == card_id]
    blocks, total = _image_blocks(root, groups)
    if not blocks:
        return ""
    return (f'<h3>Image candidates <span class="count">{total}</span></h3>'
            f'<p class="note">Picking one records it as this card\'s answer, '
            f'below. The card stays here until you send it on.</p>'
            f'<div class="rows">{blocks}</div>')


def _decide_audio(root: Path, card_id: str) -> str:
    """The audio twin of `_decide_images`: this card's own takes, where its pick
    question is answered. Newest attempt only — `answer_audio_pick` counts only
    those, and showing a re-rolled attempt's takes beside them would offer picks
    that can never complete the answer. Empty string when there are none."""
    groups = [g for g in scan_audio_candidates(root) if g.card == card_id]
    if not groups:
        return ""
    newest = max(g.attempt for g in groups)
    blocks, total = _audio_blocks(root, [g for g in groups if g.attempt == newest])
    if not blocks:
        return ""
    return (f'<h3>Audio candidates <span class="count">{total}</span></h3>'
            f'<p class="note">Pick one take per sound. Once every sound has a pick, '
            f'the picks are recorded as this card\'s answer, below.</p>'
            f'<div class="rows">{blocks}</div>')


# --------------------------------------------------------------------------
# System — the framework maintaining itself
# --------------------------------------------------------------------------
#
# **Why this page exists at all.** Every verb below already worked from a command
# line and none of them was discoverable: nothing in any document mentioned the
# Command Center, `update` did not exist, and the closest thing to a health check
# was remembering that `nightshift doctor` is a thing. A framework whose
# maintenance is folklore gets maintained by whoever remembers the folklore.
#
# **It renders in a repo that has no install**, which is what makes the Setup
# section useful rather than decorative: a repo holding nothing but the two
# launchers `bootstrap` wrote can open this page, and the page is what runs the
# install. That is a property of `read_context`'s early return, not something the
# module got for free — see its docstring, and the correction it came from. Every
# section below therefore checks `installed()` and renders nothing when it is
# False, rather than assuming a board, a manifest or a queue is there to read.


def installed(root: Path) -> bool:
    """Whether nightshift is installed here — judged on the manifest, not the receipt.

    **The receipt is the wrong test and it took a live check to notice.** `bootstrap`
    writes one, because it stages the install skill and the launchers through the same
    `Plan` so `uninstall` can take them back. So a repo that has done nothing but
    bootstrap *has* a receipt, and keying on it made this page report a finished install
    and then fail rendering on the manifest that was never written.

    `.ai/manifest.toml` is the honest marker: it is what `init` writes, what every
    branch, board and gate read resolves through, and the one file whose absence means
    none of them can answer.
    """
    return (root / manifest.AI_DIR / manifest.MANIFEST_NAME).is_file()


def system_attention(root: Path) -> int:
    """The rail's count: files an update would change, plus ones needing a decision.

    Zero for an uninstalled repo — the Setup section is an offer, not a backlog, and
    a permanent `1` beside a page nobody has installed yet is noise. Never raises:
    this is on the rail, so it is on the critical path of every page.
    """
    try:
        found = update.survey(root)
    except (update.UpdateError, OSError):
        return 0
    attention = found.changes + len(found.by(update.CONFLICT))
    try:
        attention += len(boardhealth.check(root))
    except OSError:
        pass
    # The harvest nudge counts as one thing to look at, not as N corrections: the rail
    # number is "how many things on this page want you", and a backlog is one of them.
    try:
        if corrections.harvest_due(*corrections.backlog(root))[0]:
            attention += 1
    except OSError:
        pass
    return attention


def _system_verb(name: str, root: Path, *, waived: bool, body: dict) -> str:
    """One of the System page's buttons, each shelling out to its own module.

    Split by *how long it takes and whether it spends*, which is the only distinction
    that matters to an HTTP handler: `doctor` and `gates` answer in seconds and their
    output is the point, so they are captured; `preflight` runs a test suite and `fix`
    dispatches an agent for up to three rounds, so they are detached and report a pid.
    Holding a request open for either would time the browser out and, worse, tie a run
    to a page nobody promised to leave open.
    """
    if name == "doctor":
        return _verb(run_command("doctor", [], root, timeout=180))
    if name == "corrections":
        # Read-only and fast: it parses one log and prints the clusters. Captured for
        # the same reason `doctor` is — its output *is* the answer.
        return _verb(run_command("corrections", [], root, timeout=180))
    if name == "gates":
        return _verb(run_command("gates.run", [], root, timeout=300))
    if name == "preflight":
        return f"preflight started (pid {spawn_background('preflight', [], root)})"
    if name == "fix":
        _guard_dispatch_account(waived)
        return f"fix pass started (pid {spawn_background('fix', [], root)})"
    if name == "uninstall":
        # Dry run unless the operator retyped the project's own name. `uninstall` is
        # dry-run by default and that default is honoured rather than worked around:
        # `--yes` is added only when the confirmation matches.
        if str(body.get("confirm", "")) == root.name:
            return _verb(run_command("uninstall", ["--yes"], root, timeout=180))
        return _verb(run_command("uninstall", [], root, timeout=180))
    raise PanelError(f"no such system verb: {name}")


def _system_setup(ctx: Context) -> str:
    """Install if there is none; otherwise what the install wrote."""
    if not installed(ctx.root):
        return _section("Setup", 1, _item(
            "nightshift is not installed here", kind="check", marker="+",
            why="Opens a session running the install skill. It asks which branch work "
                "merges into and what a dispatched worker may do on this machine.",
            primary=_act("Set up nightshift", onclick="post('/api/setup')", primary=True)),
            sec_id="setup")
    created = init.receipt_created(init.read_receipt(ctx.root) or {})
    return _section("Setup", 0, _item("Installed", kind="check", marker="=",
                                      chips=[_e(f"{len(created)} file(s) in {init.RECEIPT}")]),
                    sec_id="setup")


def _system_files(ctx: Context) -> str:
    """The update survey: what moved, and what needs deciding."""
    try:
        found = update.survey(ctx.root)
    except update.UpdateError as exc:
        return _section("Project files", 0, "", empty=str(exc), sec_id="projectfiles")
    rows = []
    for finding in found.by(update.STALE, update.MISSING):
        rows.append(_item(finding.rel, kind="check", marker="+",
                          why=("the template moved; you never edited this"
                               if finding.verdict == update.STALE else "missing from disk")))
    for finding in found.by(update.CONFLICT):
        target = {"path": finding.rel}
        rows.append(_item(
            finding.rel, kind="check", marker="!",
            why="you edited this and the template moved — nothing is overwritten until "
                "you choose",
            primary=_act("Diff", href=f"/update-diff?path={finding.rel}", primary=True),
            menu=[_act("Take theirs", onclick=f"post('/api/update/take',"
                                              f"{{path:'{_attr(finding.rel)}'}})"),
                  _act("Keep mine", onclick=f"post('/api/update/keep',"
                                            f"{{path:'{_attr(finding.rel)}'}})"),
                  _launch("Merge", f"Merge {finding.rel}", "/api/update/merge", target)]))
    bar = ""
    if found.changes:
        bar = (f'<div class="barbox"><p>{found.changes} safe change(s)</p><div class="acts">'
               f'{_act("Update these", onclick="post(&#39;/api/update/apply&#39;)", primary=True)}'
               f'</div></div>')
    note = (f"{len(found.by(update.CURRENT))} current · "
            f"{len(found.by(update.YOURS))} yours · "
            f"{len(found.by(update.DECLINED))} declined · "
            f"{len(found.by(update.FROZEN_V))} frozen")
    return _section("Project files", found.changes + len(found.by(update.CONFLICT)),
                    "".join(rows), note=note, bar=bar,
                    empty="Every file matches its template.", sec_id="projectfiles")


def _system_outgoing(ctx: Context) -> str:
    """What this install carries that the templates never received — silent when none."""
    try:
        found = update.survey(ctx.root)
    except update.UpdateError:
        return ""
    rows = [_item(f.rel, kind="check", marker="^",
                  chips=[_e(f"{f.ahead} line(s) the template lacks")],
                  primary=_act("Read", href=f"/update-outgoing?path={f.rel}"))
            for f in found.outgoing]
    if not rows:
        return ""
    return _section("Outgoing", len(rows), "".join(rows),
                    note="Project text the framework never received.", sec_id="outgoing")


#: The read-only and repair verbs: `(id, label, button, blurb, dispatches)`.
SYSTEM_VERBS = (
    ("doctor", "Health", "Run doctor",
     "Per-machine preconditions and manifest drift. Changes nothing.", False),
    ("gates", "Gates", "Run gates", "The gate suite, as the save hook runs it.", False),
    ("preflight", "Preflight", "Run preflight",
     "Gates, audit matrix, corrections and the affected tests. Required before a push.",
     False),
    ("fix", "Repair", "Dispatch fix",
     "Runs every check, then an agent repairs what failed — up to three rounds. "
     "Never weakens a check, never commits.", True),
)


def _system_corrections(ctx: Context) -> str:
    """The harvest nudge — silent until the backlog crosses `corrections`' thresholds."""
    try:
        count, oldest = corrections.backlog(ctx.root)
    except OSError:
        return ""
    due, age = corrections.harvest_due(count, oldest)
    if not due:
        return ""
    age_clause = f", oldest {age} days" if age is not None else ""
    return _section("Corrections to harvest", count, _item(
        f"{count} correction(s) with no disposition", kind="check", marker="!",
        why=f"In {corrections.LOG}{age_clause}. Turn each into a gate, a rule or a card.",
        primary=_act("Read them clustered", onclick="post('/api/system/corrections')")),
        sec_id="corrections")


def _system_board_health(ctx: Context) -> str:
    """Cards stuck between two lanes (`nightshift.boardhealth`) — silent when none are."""
    if not installed(ctx.root):
        return ""
    found = boardhealth.check(ctx.root)
    if not found:
        return ""
    rows = "".join(_item(f"{f.lane}/{f.card_id}", kind="check", marker="!", why=f.text)
                   for f in found)
    return _section("Board health", len(found), rows,
                    note="A merge or a verdict that never reached its lane.",
                    sec_id="board-health")


def _system_verbs(ctx: Context) -> str:
    rows = []
    for ident, label, button, blurb, dispatches in SYSTEM_VERBS:
        primary = (_launch(button, "Dispatch the fix pass", f"/api/system/{ident}", {})
                   if dispatches else
                   _act(button, onclick=f"post('/api/system/{ident}')"))
        rows.append(_item(label, kind="check", why=blurb, primary=primary))
    fresh = ctx.rail.freshness_line
    rows.append(_item("Framework", kind="check", why=fresh,
                      chips=[] if ctx.rail.freshness_known else [_chip("unknown", "warn")],
                      primary=_act("Pull", onclick="post('/api/freshness/pull',{})"),
                      menu=[_act("Refresh", onclick="post('/api/freshness/refresh',{})")]))
    return _section("Checks and repair", 0, "".join(rows), sec_id="checks")


def _system_danger(ctx: Context) -> str:
    """Uninstall: a dry run first, then only after typing the project's own name."""
    name = ctx.root.name
    return _section("Danger", 0, _item(
        "Uninstall nightshift", kind="check", marker="!",
        why="Removes what the install wrote, including the launcher and the manifest — "
            "this page stops working when it succeeds. Your own files stay.",
        primary=_act("Show what it would remove", onclick="post('/api/system/uninstall')"),
        menu=[_act("Uninstall", onclick=f"confirmUninstall('{_attr(name)}')")]),
        sec_id="danger")


def _render_system(ctx: Context) -> str:
    """One status line; the detail opens on its own only when something needs you."""
    if not installed(ctx.root):
        return _system_setup(ctx)
    attention = system_attention(ctx.root)
    line = ("All good" if not attention else
            f"{attention} thing{'s' if attention != 1 else ''} need{'' if attention != 1 else 's'} you")
    dot = ('<span class="chip ok">ok</span>' if not attention
           else '<span class="chip warn">look</span>')
    inner = "".join([_system_files(ctx), _system_outgoing(ctx), _system_corrections(ctx),
                     _system_board_health(ctx), _system_verbs(ctx), _system_setup(ctx),
                     _system_danger(ctx)])
    return (f'<section id="sec-status"><details class="sys" id="sys-all"'
            f'{" open" if attention else ""}><summary class="sys-line">{dot}'
            f'<b>{_e(line)}</b><span class="dim">{_e(ctx.rail.freshness_line)}</span>'
            f'</summary>{inner}</details></section>')


#: The pages, in rail order. `history` renders too but is reached from Running.
_RENDER = {"queue": _render_queue, "capture": _render_capture, "you": _render_you,
           "running": _render_running, "system": _render_system,
           "history": _render_history}

#: Old addresses, kept working: each redirects to the page that now holds its content.
OLD_PAGES = {"now": "queue", "verify": "you", "inbox": "capture", "ideas": "capture",
             "run": "running"}


# --------------------------------------------------------------------------
# Markdown, enough of it for a card. A card is written to be read, and reading
# one as raw text means reading its `##` and `**` as noise.
#
# Hand-rolled because the package takes no dependency it does not need for a
# gate (`pyproject.toml`'s own note on `vulture`), and this needs one function,
# not a library. It is a *subset*, deliberately: the constructs cards actually
# use. Anything unrecognised falls through as its own paragraph, escaped — the
# failure mode is "renders plainly", never "renders as markup".
#
# **Escaping happens first and once**, before any tag is introduced, so no
# amount of markdown in a card can inject HTML into this page.
# --------------------------------------------------------------------------

_MD_FENCE = re.compile(r"^```")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET = re.compile(r"^\s*[-*]\s+(.*)$")
_MD_NUMBER = re.compile(r"^\s*\d+\.\s+(.*)$")
_MD_QUOTE = re.compile(r"^&gt;\s?(.*)$")
_MD_RULE = re.compile(r"^(-{3,}|\*{3,})$")
_MD_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")

_MD_CODE = re.compile(r"`([^`]+)`")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_MD_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _md_inline(text: str) -> str:
    """Inline spans. Code first, so `**` inside a code span stays literal."""
    holes: list[str] = []

    def stash(match: re.Match) -> str:
        holes.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(holes) - 1}\x00"

    text = _MD_CODE.sub(stash, text)
    text = _MD_BOLD.sub(r"<b>\1</b>", text)
    text = _MD_ITALIC.sub(r"<em>\1</em>", text)
    # A wikilink names a card on this board, so it becomes a link to it.
    text = _MD_WIKILINK.sub(r'<a href="/card/\1">\1</a>', text)
    text = _MD_LINK.sub(r'<a href="\2" rel="noreferrer">\1</a>', text)
    for index, hole in enumerate(holes):
        text = text.replace(f"\x00{index}\x00", hole)
    return text


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A card's frontmatter and its body, apart.

    Rendered as markdown the block is neither: the `---` fences become rules and
    the `key: value` lines collapse into one run-on paragraph, which is what the
    top of every opened card looked like. The fields are data and belong in a
    strip of their own; the body is the prose.
    """
    block = board.FRONTMATTER.match(text)
    if not block:
        return {}, text
    return board.parse_fields(text), text[block.end():]


def _fields_html(fields: dict[str, str]) -> str:
    """The frontmatter as a strip of small facts, in the card's own order."""
    if not fields:
        return ""
    cells = []
    for key, value in fields.items():
        if not value:
            continue
        shown = value.strip("[]") if key == "tags" else value
        cells.append(f'<span><b>{_e(key)}</b> {_e(shown)}</span>')
    return f'<div class="fields">{"".join(cells)}</div>'


def markdown(text: str) -> str:
    """A card as HTML. Input is raw markdown; output is safe to insert."""
    lines = html.escape(text, quote=False).splitlines()
    out: list[str] = []
    para: list[str] = []
    list_tag = ""
    in_code = False
    code: list[str] = []
    table: list[str] = []

    def close_para() -> None:
        if para:
            out.append(f"<p>{_md_inline(' '.join(para))}</p>")
            para.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = ""

    def close_table() -> None:
        if not table:
            return
        rows = []
        for position, row in enumerate(table):
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            tag = "th" if position == 0 else "td"
            rows.append("<tr>" + "".join(
                f"<{tag}>{_md_inline(c)}</{tag}>" for c in cells) + "</tr>")
        out.append(f"<table>{''.join(rows)}</table>")
        table.clear()

    def close_all() -> None:
        close_para()
        close_list()
        close_table()

    for line in lines:
        if _MD_FENCE.match(line):
            if in_code:
                out.append(f"<pre>{chr(10).join(code)}</pre>")
                code.clear()
            else:
                close_all()
            in_code = not in_code
            continue
        if in_code:
            code.append(line)
            continue

        if line.strip().startswith("|"):
            close_para()
            close_list()
            if not _MD_TABLE_SEP.match(line.strip()):
                table.append(line)
            continue
        close_table()

        if not line.strip():
            close_all()
            continue
        if heading := _MD_HEADING.match(line):
            close_all()
            level = min(len(heading.group(1)) + 1, 6)
            out.append(f"<h{level}>{_md_inline(heading.group(2))}</h{level}>")
            continue
        if _MD_RULE.match(line.strip()):
            close_all()
            out.append("<hr>")
            continue
        if quote := _MD_QUOTE.match(line.strip()):
            close_all()
            out.append(f"<blockquote>{_md_inline(quote.group(1))}</blockquote>")
            continue
        bullet, number = _MD_BULLET.match(line), _MD_NUMBER.match(line)
        if bullet or number:
            close_para()
            wanted = "ul" if bullet else "ol"
            if list_tag != wanted:
                close_list()
                out.append(f"<{wanted}>")
                list_tag = wanted
            item = (bullet or number).group(1)
            out.append(f"<li>{_md_inline(item)}</li>")
            continue
        # An indented line under a list item is that item continuing, not a new
        # paragraph. Treating it as one closed the list, so the *next* numbered
        # item opened a fresh `<ol>` and every step in a wrapped list was
        # numbered "1." — which is what a card's `## Steps` section looked like.
        if list_tag and line[:1] in (" ", "\t") and out and out[-1].endswith("</li>"):
            out[-1] = out[-1][: -len("</li>")] + " " + _md_inline(line.strip()) + "</li>"
            continue
        close_list()
        para.append(line.strip())

    if in_code and code:
        out.append(f"<pre>{chr(10).join(code)}</pre>")
    close_all()
    return "".join(out)


def render_shell(ctx: Context, *, title: str, active: str, content: str) -> str:
    """The page furniture around any content — the rail, the status rail, the
    stylesheet. A card and a diff get the same frame the five pages do, so
    opening one is still inside the panel rather than a bare text dump."""
    template = TEMPLATE.read_text(encoding="utf-8")
    out = template.replace("{{TITLE}}", f"Command Center — {title}")
    out = out.replace("{{RAIL}}", _rail_html(ctx, active))
    out = out.replace("{{STATUSRAIL}}", _statusrail_html(ctx) + _launch_dialog(ctx))
    return out.replace("{{CONTENT}}", content)


def render_page(page: str, root: Path) -> str:
    page = OLD_PAGES.get(page, page)
    ctx = read_context(root)
    active = "running" if page == "history" else page
    return render_shell(ctx, title=page.capitalize(), active=active,
                        content=_RENDER[page](ctx))


def render_job(root: Path, ident: str) -> str:
    """One background command's output, framed like any other document.

    The command's stdout, unedited, and the three facts around it: what was run,
    how long it took, and how it ended. Nothing here summarises the log — a
    summary of a failure is the thing that hid the failure in the first place.

    A running job's page carries a reload, because the file it is showing is
    still being written and a static snapshot of a live log is a page that lies
    quietly as you read it.
    """
    job = jobs.load(root, ident)
    if job is None:
        return render_document(root, title="no such job",
                               subtitle=f"nothing recorded under {ident}",
                               body="<p>The record has been pruned, or was never "
                                    "written.</p>")
    status = jobs.state(job)
    facts = [f"<b>{_e(status)}</b>", _e(job.command)]
    if took := jobs.elapsed(job):
        facts.append(_e(f"{'running for' if status == jobs.RUNNING else 'took'} {took}"))
    if job.exit_code is not None:
        facts.append(_e(f"exit {job.exit_code}"))
    text = jobs.read_log(root, job.ident) or "(nothing written yet)"
    live = ('<p class="note">This job is still running; the page reloads every '
            'five seconds.</p><script>setTimeout(function(){location.reload();},5000);'
            '</script>' if status == jobs.RUNNING else "")
    # The per-note account survives the fan-out finishing: both it and the raw log.
    roster = ""
    if job.label == "ingest":
        rows = _ingest_rows(ingest.parse_progress(text))
        if rows:
            roster = f'<div class="rows">{rows}</div>'
    return render_document(
        root, title=f"{job.label} · output", subtitle=job.started.replace("T", " "),
        body=f'{_meta(facts)}{live}{roster}<pre>{_e(text)}</pre>',
        acts=_act("Reload", href=f"/log/{job.ident}"))


@dataclass(frozen=True)
class DecideState:
    """What one parked card is waiting for, and which button says so.

    `next_act` is `"tasks"`, `"triage"` or `"answer"` — the one control this page
    should lead with. `send_enabled` is the `tasks/` guard, kept identical to
    `decide.promote_to_tasks` so the button and the endpoint cannot disagree.
    """

    send_enabled: bool
    next_act: str
    banner: str


def _decide_state(card: board.Card, who: str) -> DecideState:
    """What this parked card is waiting for, said on the page rather than inferred.

    **`needs-decision/` holds two different things that looked identical here.** A
    card parked because triage could not scope it without an answer, and a card parked
    mid-implementation because a scoped card hit one ambiguity — Karel, 2026-08-23:
    *"When needing an info for triage to continue. Or when the triage is done and it
    needs just some small clarification / it run into decision during implementation.
    Based on that it should return to triage or to tasks."* They resume by opposite
    routes and rendered the same picker with the same four buttons, so the page never
    said which one you were looking at.

    **The route is read, not guessed** — `after_answer:` on the card, written by
    whoever parked it (`board.AFTER_ANSWER`). This page did briefly guess it from
    whether `## Open questions` read `none`, which conflates "nothing to answer" with
    "answer it and dispatch" and cannot express the second scenario at all: a scoped
    card with one live question *is* headed for `tasks/`, and the inference always
    called that a re-triage. A card written before the field existed has no route, and
    that case falls back to the old inference while saying that is what it is doing.
    """
    settled = decide.open_questions_settled(card.text)
    answered = decide.has_maintainer_answer(card.text, who)
    route = card.after_answer

    def banner(kind: str, chip: str, *paras: str) -> str:
        return (f'<div class="decide-state {kind}">{_chip(chip, kind)}'
                + "".join(f"<p>{p}</p>" for p in paras) + "</div>")

    def blocking() -> str:
        """The first line of what is still open, quoted back."""
        first = next((line.strip() for line in
                      board.section(card.text, "Open questions").splitlines()
                      if line.strip()), "")
        return _md_inline(_e(first[:160] + ("…" if len(first) > 160 else "")))

    if route == board.AFTER_ANSWER_TASKS and not settled:
        # The scenario the field was added for: scoped card, one live question, and the
        # answer is the whole of what it is waiting for.
        if answered:
            return DecideState(True, "tasks", banner(
                "ok", "answered · ready to dispatch",
                "<b>This card is scoped, and you have answered it.</b> It declares "
                "<code>after_answer: tasks</code>, so the answer was the only thing "
                "between it and a worker: <b>Send to tasks</b> is the next click.",
                "That click also settles <code>## Open questions</code> &mdash; it has "
                "to, because <code>card_schema</code> refuses a live question in "
                "<code>tasks/</code> &mdash; rewriting it to <code>none</code> and "
                "keeping what it said underneath as history."))
        return DecideState(False, "answer", banner(
            "warn", "waiting on your answer",
            "<b>This card is scoped and waiting on exactly this question.</b> It "
            "declares <code>after_answer: tasks</code>, so answering it is all that is "
            f"needed: <span class=\"dim\">{blocking()}</span>",
            "Record the answer and <b>Send to tasks</b> becomes available &mdash; it "
            "will settle <code>## Open questions</code> for you on the way through, "
            "keeping the question as history."))

    if route == board.AFTER_ANSWER_TRIAGE and not settled:
        return DecideState(False, "triage" if answered else "answer", banner(
            "warn", "answered · returns to triage" if answered
            else "waiting on your answer",
            "<b>This card is not scoped yet, and your answer is what scoping needs.</b> "
            "It declares <code>after_answer: triage</code>: "
            f"<span class=\"dim\">{blocking()}</span>",
            ("<b>Re-triage this</b> is the next click &mdash; it rewrites the card "
             "around your answer and clears <code>## Open questions</code>. "
             if answered else
             "Record the answer, then <b>Re-triage this</b> &mdash; which rewrites the "
             "card around it and clears <code>## Open questions</code>. ")
            + "<b>Send to tasks</b> stays disabled: there is no "
              "<code>## Approach</code> for a worker to follow yet, which is the whole "
              "reason the card was parked here."))

    if not settled:
        # No declared route and a live question — a card written before the field
        # existed. Say what is missing rather than picking a route on its behalf.
        return DecideState(False, "answer", banner(
            "warn", "waiting on your answer",
            "<b>This card has a live question and does not say how it resumes.</b> It "
            "carries no <code>after_answer:</code> route, so nothing here can tell "
            "whether your answer scopes the card or settles one point inside an "
            f"already-scoped one: <span class=\"dim\">{blocking()}</span>",
            "Recording an answer puts it in <code>## Thread</code> and does <em>not</em> "
            "rewrite <code>## Open questions</code>, so <b>Send to tasks</b> stays "
            "disabled. <b>Re-triage this</b> is the safe next click &mdash; it rescopes "
            "the card against your answer and clears that section."))

    if answered:
        return DecideState(True, route or "tasks", banner(
            "ok", "answered · ready to move",
            "<b>Nothing here is waiting on you.</b> Your answer is on record below and "
            "<code>## Open questions</code> reads <code>none</code>, so this card can "
            "move as it stands."
            + (" It declares <code>after_answer: triage</code>, so <b>Re-triage this</b> "
               "is what its parker expected next &mdash; though <b>Send to tasks</b> is "
               "available if the answer left the card dispatchable after all."
               if route == board.AFTER_ANSWER_TRIAGE else
               " <b>Send to tasks</b> is the next click."),
            "<b>Close</b> instead if the answer ended the card."))

    return DecideState(True, route or "tasks", banner(
        "ok", "no open question",
        "<b>This card is reporting, not asking.</b> <code>## Open questions</code> "
        "reads <code>none</code>, so it is parked on what a worker ran into rather "
        "than on a decision of yours &mdash; read the report below, and if it is dealt "
        "with, <b>Send to tasks</b> re-dispatches the card as written."
        + (" (It declares <code>after_answer: triage</code>, so its parker expected "
           "a rescope first.)" if route == board.AFTER_ANSWER_TRIAGE else ""),
        "Answer below only to put something on the record for the next worker; "
        "<b>Re-triage this</b> if the report means the card itself needs rescoping."))


def render_decide(root: Path, card_id: str) -> str:
    """The answering form for one parked card.

    **A page rather than a row.** The Decide section on `/now` lists what is waiting;
    this is where you sit down with one of them. The options on a real card are whole
    sentences with their consequences attached — `audio-generation-pipeline` batches
    three decisions with three prose options each — and none of that fits in a row
    beside a button.

    **Deterministic all the way through.** The options come from the card's own
    `## Question` via `decide.parse`; picking one writes that option's text into
    `## Thread` verbatim. Nothing is summarised, nothing is inferred, and no model is
    consulted — see `decide`'s module docstring for why that rule is stricter here than
    elsewhere. `Chat about it` is the escape hatch when the question needs a
    conversation, and it deliberately writes nothing.
    """
    card = board.find(root, card_id)
    if card is None:
        return render_document(root, title=card_id, subtitle="no such card",
                               body="<p>That card is not on the board.</p>")
    subquestions = decide.parse(card.text)
    who = decide.attributor(root)

    # First on the page, above the question itself: which of the two kinds of parked
    # card this is, and what the next click is. See `_decide_state`.
    state = _decide_state(card, who)
    settled = decide.open_questions_settled(card.text)
    blocks = [state.banner]

    question = board.section(card.text, "Question")
    if question:
        blocks.append(f'<div class="doc">{markdown(question)}</div>')

    # Directly under the question, because on an artefact card it *is* the
    # question: `settle._park_for_pick` writes "N candidates were produced and
    # none of them installed", and reading that with nothing on screen to look at
    # is the friction parking the card was supposed to remove. Absent on every
    # other card, which is all of them until a run harvests images.
    blocks.append(_decide_images(root, card.id))
    blocks.append(_decide_audio(root, card.id))

    # Shown before the picker, not after: this page used to be the one place on the
    # board where you could not see your own answer once you had given it — the
    # Thread lives only in the raw card body, and this form never read it. Karel
    # answered the same card four times because nothing on screen ever confirmed the
    # first click had landed (2026-08-22).
    thread = board.section(card.text, "Thread")
    if thread.strip():
        blocks.append(f'<div class="doc"><h3>Already on record</h3>'
                      f'{markdown(thread)}</div>')

    for index, sub in enumerate(subquestions):
        options = []
        for _choice, option in enumerate(sub.options):
            mark = _chip("recommended", "ok") if option.recommended else ""
            options.append(
                f'<label class="pickone">'
                f'<input type="radio" name="q{index}" value="{_attr(option.text)}"'
                f'{" checked" if option.recommended else ""}>'
                f'<span>{_md_inline(_e(option.text))} {mark}</span></label>')
        # Always offered, and never pre-selected: an enumerated list is triage's best
        # guess at the shape of the decision, and "none of these" is a real answer that
        # a picker without it silently converts into no answer at all.
        options.append(
            f'<label class="pickone"><input type="radio" name="q{index}" value="">'
            f'<span class="dim">Something else — say what below</span></label>')
        prompt = (f'<h3>{_md_inline(_e(sub.prompt))}</h3>' if sub.prompt else "")
        blocks.append(f'<div class="decide-q">{prompt}{"".join(options)}</div>')

    if not subquestions:
        blocks.append('<p class="note">This card asks in prose rather than offering a '
                      'picker, so there is nothing to tick — write the answer below.</p>')

    blocks.append('<div class="decide-q"><h3>In your own words</h3>'
                  '<textarea id="answernote" rows="6" placeholder="Recorded verbatim, '
                  'as a quote, under your name. Optional when you have ticked '
                  'something."></textarea></div>')

    # Deliberately separate buttons rather than one that decides for you. An answer
    # is not a ticket to `tasks/` — see `decide.write_answer` on park-over-promote —
    # so recording one and advancing the card are different clicks for anything that
    # opens more work. Closing as not-applicable/already-satisfied is the one
    # exception: see `decide.close_parked` on why that answer gets its own button
    # that both records and moves.
    #
    # Which of them is *primary* is the card's own `after_answer:` route, so the page
    # leads with the step the parker said comes next instead of always leading with
    # "Record the answer" — including on a card where recording one is already done.
    send_title = ("Moves the card to tasks/."
                  if state.send_enabled and settled else
                  "Settles ## Open questions to `none` — keeping the question as "
                  "history — then moves the card to tasks/."
                  if state.send_enabled else
                  "Its ## Open questions does not read `none`, and card_schema refuses "
                  "a card in tasks/ with a live question. This card declares "
                  f"after_answer: {card.after_answer or '(none)'}.")
    acts = (
        _act("Record the answer",
             onclick=f"answerCard('{_attr(card.id)}')",
             primary=state.next_act == "answer",
             extra='title="Writes what you ticked into the card\'s ## Thread, dated and '
                   'signed. The card does not move."')
        + _act("Close — no further work needed", onclick=f"closeCard('{_attr(card.id)}')",
               extra='title="Records the pick/note (if any) and moves the card straight '
                     'to done/ — for an answer like \'not applicable\' or \'already '
                     'satisfied\' that ends the card rather than opening more work."')
        + _act("Re-triage this",
               onclick=f"post('/api/triage',{{card:'{_attr(card.id)}'}})",
               primary=state.next_act == "triage",
               extra='title="Opens triage on the card, for when the answer scopes it or '
                     'changed its shape enough to need re-scoping."')
        + _act("Send to tasks", onclick=f"sendToTasks('{_attr(card.id)}')",
               disabled=not state.send_enabled,
               primary=state.next_act == "tasks" and state.send_enabled,
               extra=f'title="{send_title}"')
        + _work_act(card=card.id, tier=card.tier, worker=card.worker, primary=False)
    )
    signed = (f"Signed <code>{_e(who)}</code>, today." if who else
              '<b class="warn">No <code>[board].decision_attributor</code> in the '
              'manifest — an answer cannot be recorded until one is declared.</b>')
    body = ("".join(blocks)
            + f'<p class="note">{signed} The answer goes in <code>## Thread</code>; '
              f'the card stays in <code>{_e(card.lane)}/</code> until you move it.</p>')
    return render_document(root, title=card.id,
                           subtitle=f"{card.lane}/ · {card.title}",
                           body=body, acts=acts)


def render_body(root: Path, target: str, text: str, *, editing: bool) -> str:
    """One board file, rendered — and editable without leaving the page.

    **The editor used to be a two-line box on the row it belonged to.** Karel,
    2026-08-21: *"When editing an inbox card, it opens a small edit box, which is
    hard to work with. It needs to be either a new page or a popup. It should also
    probably be intertwined with open note, so I can switch between edit and
    display easily."* A note is a paragraph or three of prose that a person is
    thinking about — the reason the whole board is markdown files — and a 5.5rem
    textarea wedged between two rows is a place to fix a typo, not a place to
    write. So the note's own page *is* the editor: same page, same URL, two modes.

    **Both modes are always in the document**, with one hidden — not fetched when
    you press Edit. The text is already here (this page rendered from it), a second
    read would be a second answer to a question already answered, and switching
    modes has to be instant and lossless: half-typed text survives a toggle to the
    rendered view and back, because nothing is thrown away to make the switch.

    `?edit=1` is the mode in the address, so the Inbox's Edit button lands straight
    in the editor, and a reload — or a bookmark, or the back button — keeps you
    where you were rather than dropping you into the reader.
    """
    name = Path(target).name
    editor = (
        f'<div class="pageedit{"" if editing else " hidden"}" id="bodyedit">'
        f'<textarea id="bodytext" spellcheck="false" '
        f'data-path="{_e(target)}">{_e(text)}</textarea>'
        f'</div>')
    # `Save` stays visible in both modes and is disabled in the reader, rather than
    # appearing when you switch: a button that materialises under the cursor is how
    # you press the one that was not there a moment ago.
    acts = (_act("Edit", onclick="setBodyMode(true)", primary=not editing,
                 extra='id="bodyedit-btn"')
            + _act("Read", onclick="setBodyMode(false)", primary=editing,
                   extra='id="bodyread-btn"')
            + _act("Save", onclick=f"saveBodyPage('{_attr(target)}')", primary=True,
                   disabled=not editing, extra='id="bodysave-btn"'))
    return render_document(root, title=name, subtitle=target,
                           body=markdown(text), acts=acts,
                           editor=editor, editing=editing)


def render_document(root: Path, *, title: str, subtitle: str, body: str,
                    acts: str = "", editor: str = "", editing: bool = False) -> str:
    """A card or a diff, framed. `body` is already-safe HTML.

    **Back returns to wherever this page was opened from, not always to `/now`.**
    Every document-style page (`/card`, `/diff`, `/log`, `/decide`, `/body`,
    `/update-diff`) is a real full-page navigation — there is no client-side router
    here, only `/api/refresh` polling a page that is already open — so the browser's
    own history stack already has "the page this was opened from" sitting one entry
    back. `history.back()` reads that stack instead of a hardcoded destination, so
    NOW -> card A -> diff B unwinds B -> A -> NOW instead of jumping straight to NOW
    from anywhere. The `history.length>1` guard is for a page opened with no prior
    entry in this tab (a direct load or a bookmark), where `history.back()` would
    silently do nothing — that case still falls back to `/now`, matching the old
    behaviour exactly.
    """
    ctx = read_context(root)
    head = (f'<div class="sec-head"><h2>{_e(title)}</h2>'
            f'<p class="note">{_e(subtitle)}</p></div>')
    back = _act("Back",
               onclick="history.length>1?history.back():location.assign('/queue')")
    # `editor` is the second mode a document may have (see `render_body`); a page
    # with none renders exactly as it always did, which is every page but a note.
    doc = f'<div class="doc{" hidden" if editing else ""}" id="bodyview">{body}</div>'
    content = (f'<section>{head}{doc}{editor}'
               f'<div class="barbox"><p></p><div class="acts">{acts}'
               f'{back}</div></div></section>')
    return render_shell(ctx, title=title, active="", content=content)


# --------------------------------------------------------------------------
# The HTTP server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    root: Path = None  # type: ignore[assignment]  # set by `serve`

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib signature
        pass  # the run log and status file are the record; a console tee is noise

    def _send(self, status: int, body: bytes,
              content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 — stdlib method name
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        if path == "":
            # An uninstalled repo lands on the page that can install it.
            self.send_response(302)
            self.send_header("Location", "/queue" if installed(self.root) else "/system")
            self.end_headers()
            return
        if path in OLD_PAGES:
            self.send_response(302)
            self.send_header("Location", f"/{OLD_PAGES[path]}")
            self.end_headers()
            return
        if path in _RENDER:
            self._send(200, render_page(path, self.root).encode("utf-8"))
            return
        if path == "api/refresh":
            # The live refresh, and deliberately **the same bytes as the page** rather
            # than a purpose-built payload: the client re-renders whichever page it is
            # on and swaps the regions whose HTML actually differs. A second rendering
            # path here would be a second thing to keep in step with every section
            # added, and the first time it fell behind the panel would quietly stop
            # updating the part nobody remembered to add to it.
            wanted = parse_qs(parsed.query).get("page", [""])[0]
            if wanted in _RENDER or wanted in OLD_PAGES:
                self._send(200, render_page(wanted, self.root).encode("utf-8"))
                return
            # `decide/<id>` is not in `_RENDER` — it takes a card id, not a bare page
            # name, and renders standalone rather than inside the shell's nav — but it
            # still needs the same live refresh: the answer-then-nothing-changes gap
            # this closes (2026-08-22) is exactly what this endpoint exists to prevent.
            if wanted.startswith("decide/"):
                self._send(200, render_decide(self.root, wanted[len("decide/"):])
                          .encode("utf-8"))
                return
            self._send_json(400, {"ok": False, "message": f"no page {wanted!r}"})
            return
        if path == "api/body":
            wanted = parse_qs(parsed.query).get("path", [""])[0]
            try:
                self._send_json(200, {"body": read_body(self.root, wanted)})
            except PanelError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
            return
        if path.startswith("decide/"):
            self._send(200, render_decide(self.root, path[len("decide/"):]).encode("utf-8"))
            return
        if path.startswith("card/"):
            card = board.find(self.root, path[len("card/"):])
            if card is None:
                self._send_text(404, "no such card")
                return
            fields, body = split_frontmatter(card.text)
            self._send(200, render_document(
                self.root, title=card.id,
                subtitle=f"{card.lane}/ · {card.title}",
                body=_fields_html(fields) + markdown(body),
                acts=_act("Diff", href=f"/diff/{card.id}")).encode("utf-8"))
            return
        if path.startswith("body/"):
            # **Unquoted, because a note name is prose.** Board files are named the
            # way a person names a thought — "Regenerate soundtrack.md", "Animation
            # – attack.md" — so the browser sends `%20` and `%E2%80%93`, and a
            # handler that slices the raw path looks for a file spelt with a percent
            # sign in it. `read_body` confines the result to the board either way,
            # so decoding here cannot widen what is reachable.
            target = unquote(path[len("body/"):])
            try:
                text = read_body(self.root, target)
            except PanelError as exc:
                self._send_text(400, str(exc))
                return
            editing = parse_qs(parsed.query).get("edit", [""])[0] not in ("", "0")
            self._send(200, render_body(
                self.root, target, text, editing=editing).encode("utf-8"))
            return
        if path.startswith("log/"):
            self._send(200, render_job(self.root, path[len("log/"):]).encode("utf-8"))
            return
        if path.startswith("audio/"):
            # Unquoted like `body/`: the rel path is generated by this module's
            # own scanner, never typed by a person, but the browser still
            # percent-encodes it in the `<audio src>` this section wrote.
            rel = unquote(path[len("audio/"):])
            try:
                resolved = resolve_audio_path(self.root, rel)
            except PanelError as exc:
                self._send_text(400, str(exc))
                return
            content_type = _AUDIO_CONTENT_TYPES.get(resolved.suffix.lower(),
                                                     "application/octet-stream")
            self._send(200, resolved.read_bytes(), content_type)
            return
        if path.startswith("image/"):
            # Beside `audio/` and identical in shape: unquoted for the same
            # reason (the rel path is this module's own, but the browser still
            # percent-encodes it in the `<img src>` the section wrote), and
            # confined by `resolve_image_path` rather than by this handler.
            rel = unquote(path[len("image/"):])
            try:
                resolved = resolve_image_path(self.root, rel)
            except PanelError as exc:
                self._send_text(400, str(exc))
                return
            content_type = _IMAGE_CONTENT_TYPES.get(resolved.suffix.lower(),
                                                    "application/octet-stream")
            self._send(200, resolved.read_bytes(), content_type)
            return
        if path.startswith("diff/"):
            card = board.find(self.root, path[len("diff/"):])
            if card is None:
                self._send_text(404, "no such card")
                return
            branch = card.fields.get("branch") or f"ai/{card.id}"
            base = default_base(self.root)
            if branch_has_commits(self.root, base, branch):
                diff = _git_out(self.root, "diff", f"{base}...{branch}") or "(git said nothing)"
            else:
                diff = (f"`{branch}` carries no commits against {base} — either the card "
                        f"produced an artefact rather than a diff, or it has already merged "
                        f"and its branch was deleted.")
            self._send(200, render_document(
                self.root, title=f"{card.id} · diff", subtitle=f"{base}...{branch}",
                body=f"<pre>{_e(diff)}</pre>",
                acts=_act("Read card", href=f"/card/{card.id}")).encode("utf-8"))
            return
        if path == "update-diff":
            # Your installed copy against today's template, framed like any other
            # document rather than dumped as text — reading a conflict is the step
            # before deciding it, so it happens inside the panel.
            target = parse_qs(parsed.query).get("path", [""])[0]
            try:
                found = update.survey(self.root)
                finding = update.find(found, target)
                text = update.diff(finding, self.root)
            except update.UpdateError as exc:
                self._send_text(400, str(exc))
                return
            acts = "".join([
                _act("Take theirs", onclick=f"post('/api/update/take',"
                                            f"{{path:'{_attr(finding.rel)}'}})"),
                _act("Keep mine", onclick=f"post('/api/update/keep',"
                                          f"{{path:'{_attr(finding.rel)}'}})"),
                _act("Merge", onclick=f"post('/api/update/merge',"
                                      f"{{path:'{_attr(finding.rel)}'}})"),
            ])
            self._send(200, render_document(
                self.root, title=f"{finding.rel} · diff",
                subtitle="yours (-) against nightshift's (+)",
                body=f"<pre>{_e(text or 'identical')}</pre>",
                acts=acts).encode("utf-8"))
            return
        if path == "update-outgoing":
            # The other direction. Not a diff: a diff of a personalised file is
            # mostly the personalisation, and the question here is only "is this a
            # rule the framework should carry".
            target = parse_qs(parsed.query).get("path", [""])[0]
            try:
                found = update.survey(self.root)
                finding = update.find(found, target)
                text = update.outgoing_blocks(finding, self.root)
            except update.UpdateError as exc:
                self._send_text(400, str(exc))
                return
            self._send(200, render_document(
                self.root, title=f"{finding.rel} · outgoing",
                subtitle=f"{finding.ahead} line(s) here that the template does not have",
                body=f"<pre>{_e(text or 'nothing outgoing')}</pre>").encode("utf-8"))
            return
        self._send_text(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 — stdlib method name
        path = urlparse(self.path).path.strip("/")
        body = self._body()
        try:
            message = self._route(path, body)
        except PanelError as exc:
            self._send_json(400, {"ok": False, "message": str(exc)})
            return
        except subprocess.TimeoutExpired as exc:
            self._send_json(504, {"ok": False, "message": f"timed out: {exc}"})
            return
        if message is None:
            self._send_json(404, {"ok": False, "message": f"no such action: {path}"})
            return
        self._send_json(200, {"ok": True, "message": message})

    def _route(self, path: str, body: dict) -> str | None:
        root = self.root
        # One checkbox, both waivers (§3.4): the money rule for the commands that
        # consult it, and the account exclusion for the dispatch guard. Per request,
        # never stored.
        waived = bool(body.get("allow_paid"))
        paid = ["--allow-paid"] if waived else []

        if path == "api/dispatch":
            _guard_dispatch_account(waived)
            card_id = str(body.get("card_id", ""))
            return f"dispatching {card_id} (pid {spawn_background('runner', ['--card', card_id], root)})"

        if path == "api/night":
            _guard_dispatch_account(waived)
            ids = [str(i) for i in body.get("card_ids", []) if str(i).strip()]
            # The one run setting the panel could not express until 2026-08-21.
            # Karel: *"I would like to have an option to choose the number of
            # sessions the process can take. We have this option in the runner
            # call, but nowhere in the command center."* It is read here rather
            # than folded into the argv above, because a refusal has to reach the
            # browser as a refusal — see `read_sessions`.
            sessions = read_sessions(body)
            windows = f", up to {sessions} session window(s)" if sessions else ""
            if ids:
                pid = spawn_sequence(ids, root, sessions=sessions)
                return (f"running {len(ids)} card(s) in order{windows} "
                        f"(pid {pid}): {', '.join(ids)}")
            args = ["--sessions", str(sessions)] if sessions else []
            return (f"tonight's run started{windows} "
                    f"(pid {spawn_background('runner', args, root)})")

        if path == "api/chores/run":
            _guard_dispatch_account(waived)
            return f"chore batch started (pid {spawn_background('chores', paid, root)})"

        if path == "api/ingest":
            # `write` is the second step over the recorded routing; `scribe` is the
            # combined one-shot, kept because the CLI has it and an unattended
            # caller wants both halves in one command. The bar only offers the two
            # separate steps — see `_render_inbox` on why the ordering is the rule.
            _guard_dispatch_account(waived)
            if body.get("write"):
                args = ["--write-cards"] + paid
                return (f"writing the cards for the routed notes "
                        f"(pid {spawn_background('ingest', args, root)})")
            args = (["--scribe"] if body.get("scribe") else []) + paid
            return f"classifying the inbox (pid {spawn_background('ingest', args, root)})"

        if path == "api/ingest/one":
            # One note's card, on the route the last pass gave it. The same verb
            # the bar runs, narrowed by a flag — not a second code path, and not
            # this server deciding what a route means.
            _guard_dispatch_account(waived)
            note = str(body.get("note", ""))
            if not note:
                raise PanelError("no note given")
            args = ["--only", note] + paid
            return (f"writing the card for {note} "
                    f"(pid {spawn_background('ingest', args, root)})")

        if path == "api/review":
            _guard_dispatch_account(waived)
            card_id = str(body.get("card_id", ""))
            args = ["--card", card_id] + paid
            return f"reviewing {card_id} (pid {spawn_background('drain', args, root)})"

        if path == "api/review-all":
            # No `--card`: `drain` sweeps every reviewable card in `review/` in one
            # pass — the bulk form of the row above, both spawning the same module.
            _guard_dispatch_account(waived)
            return f"reviewing the review/ lane (pid {spawn_background('drain', paid, root)})"

        if path == "api/answer":
            # No dispatch guard and no `paid`: this spends nothing, starts nothing and
            # calls no model. It is a person typing into a file, which is the whole
            # point of the feature — see `decide`.
            card_id = str(body.get("card_id", ""))
            picks = [str(p) for p in body.get("picks", [])]
            note = str(body.get("note", ""))
            try:
                return decide.write_answer(root, card_id, picks, note)
            except decide.DecideError as exc:
                raise PanelError(str(exc)) from exc

        if path == "api/decide-close":
            # `decide.close_parked` does the write and the move together — see its
            # docstring for why that answer, unlike a promotion to tasks/, gets one
            # click instead of two.
            card_id = str(body.get("card_id", ""))
            picks = [str(p) for p in body.get("picks", [])]
            note = str(body.get("note", ""))
            try:
                message = decide.close_parked(root, card_id, picks, note)
            except decide.DecideError as exc:
                raise PanelError(str(exc)) from exc
            return message

        if path == "api/tasks":
            # The deliberate second click after an answer. The policy — including when
            # an answered `after_answer: tasks` card may settle its own questions on the
            # way through — is `decide.promote_to_tasks`, which also moves the card; the
            # guard lives with the rest of the decide flow so the button cannot turn the
            # board red.
            card_id = str(body.get("card_id", ""))
            try:
                message = decide.promote_to_tasks(root, card_id)
            except decide.DecideError as exc:
                raise PanelError(str(exc)) from exc
            return message

        if path == "api/reorder":
            return _verb(run_command("boardcmd", ["reorder", str(body.get("card_id", "")),
                                                  str(body.get("order", ""))], root))

        if path == "api/reorder-many":
            return _reorder_many(root, body.get("writes", []))

        if path == "api/verified":
            return _verb(run_command("boardcmd", ["verified", str(body.get("card_id", ""))], root))

        if path == "api/verified-many":
            done = [_verb(run_command("boardcmd", ["verified", str(cid)], root))
                    for cid in body.get("card_ids", [])]
            return f"{len(done)} card(s) marked verified"

        if path == "api/rejected":
            # `Not OK`: spends nothing and dispatches nothing — a board write, same
            # shape as `api/verified`, carrying the textbox's feedback onto the card.
            note = str(body.get("note", ""))
            if not note.strip():
                raise PanelError("say what was wrong first")
            return _verb(run_command(
                "boardcmd", ["rejected", str(body.get("card_id", "")), "--note", note], root))

        if path == "api/promote":
            return _verb(run_command("boardcmd", ["promote", str(body.get("name", ""))], root))

        if path == "api/delete":
            # No server-side confirmation to check: the typed-name guard is
            # `confirmDeleteNote`, client-side, the same shape as the uninstall
            # button's — a verb here stays scriptable and non-interactive, exactly
            # like every other one `boardcmd` exposes.
            lane = str(body.get("lane") or "inbox")
            return _verb(run_command(
                "boardcmd", ["delete", str(body.get("name", "")), "--lane", lane], root))

        if path == "api/close":
            # Spends nothing and dispatches nothing: it is a board write, so it
            # runs to completion like every other one and answers in its own words.
            note = str(body.get("note", ""))
            if not note:
                raise PanelError("no note given")
            return _verb(run_command("boardcmd", ["close", note], root))

        if path == "api/note":
            lane = str(body.get("lane") or "inbox")
            return _verb(run_command("boardcmd", ["note", str(body.get("name", "")),
                                                  "--lane", lane,
                                                  "--body", str(body.get("body", ""))], root))

        if path == "api/edit":
            return _verb(run_command("boardcmd", ["edit", str(body.get("path", "")),
                                                  "--body", str(body.get("body", ""))], root))

        if path == "api/freshness/refresh":
            return freshness.describe(freshness.read(fetch=True))

        if path == "api/freshness/pull":
            return _verb(run_command("freshness", ["--pull"], root))

        if path == "api/account":
            state = select_account(root, str(body.get("label", "")))
            return f"account: {state.label or '(ambient)'}"

        if path == "api/tier":
            # Server-side, like the account and unlike the paid override. It has to
            # be: every card row renders a chip saying what tier a session on it
            # would open at, and a choice held only in the browser could not be read
            # by the renderer that draws them. Still per-process and never written
            # to disk — a tier the repo woke up carrying would be a decision nobody
            # made this morning.
            choice = select_tier(root, str(body.get("tier", "")),
                                 bool(body.get("override")))
            if not choice.tier:
                return "tier: no choice — cards keep their own, the rest take the default"
            return (f"tier: {choice.tier}"
                    + (", overriding every card" if choice.override
                       else ", for cards that declare none"))

        if path == "api/local":
            # Server-side for the same reason the tier is: every card row renders
            # a chip saying which runtime a dispatch would take, and a choice held
            # only in the browser could not be read by the renderer that draws
            # them. Unlike the tier, this one *does* reach dispatched work — see
            # `TierChoice`'s docstring for why that is deliberate and why the
            # asymmetry (always may force cloud, may only permit local for an
            # allowlisted charter) is what makes it safe.
            choice = select_local(bool(body.get("on")))
            if not choice.on:
                return "local model off — every card this sitting dispatches runs on cloud"
            described = runtimes.describe(root)
            return (f"local model on for allowlisted charters — {described}"
                    if described else
                    "local model on, but this machine declares none — everything "
                    "runs on cloud")

        if path == "api/switch-account":
            # A browser sign-in against the one config directory. The panel is a
            # launcher: it opens the terminal and stops there. Re-authenticating
            # keeps the same credentials path but can swap which account it
            # describes, which a path-keyed cache cannot see on its own — drop
            # it explicitly rather than serving the old account's headroom
            # under the new one's name.
            usage.invalidate_cache(_account_paths(_ACCOUNT)[0])
            open_terminal(root, "claude", "auth", "login")
            return ("opened a terminal running `claude auth login` — sign in, then "
                    "reload this page to see which account is in force")

        if path == "api/stop":
            # The kill switch the runner already watches for (`hostconfig.STOP_FILE`),
            # dropped where it looks. Not a signal, not a pid: a file, so it works
            # across the dedicated integration checkout the runner may be using.
            stop = root / STOP_FILE
            stop.parent.mkdir(parents=True, exist_ok=True)
            textio.write_text_lf(stop, "stop\n")
            return f"{STOP_FILE.as_posix()} written — the run stops after the card it is on"

        if path == "api/setup":
            # The install, driven the one way it has ever been driven: the skill, in an
            # interactive session. Not a `-p` dispatch — two of its four questions are
            # never guessed by policy and a headless agent cannot ask them — and not a
            # form on this page, which would be a second install driver competing with
            # the first. The panel launches; the skill installs.
            open_terminal(root, "claude", "/install-nightshift")
            return ("opened a terminal running `/install-nightshift` — answer its two "
                    "questions, then reload this page")

        if path == "api/update/apply":
            return _verb(run_command("update", ["--apply"], root))

        if path in ("api/update/take", "api/update/keep"):
            verb = path.rsplit("/", 1)[1]
            target = str(body.get("path", ""))
            if not target:
                raise PanelError("no path given")
            return _verb(run_command("update", [f"--{verb}", target], root))

        if path == "api/update/merge":
            # A real agent session on a real file: a spending verb, so it answers to
            # the account veto exactly as a card dispatch does.
            _guard_dispatch_account(waived)
            target = str(body.get("path", ""))
            if not target:
                raise PanelError("no path given")
            return (f"merging {target} (pid "
                    f"{spawn_background('update', ['--merge', target], root)})")

        if path.startswith("api/system/"):
            return _system_verb(path.rsplit("/", 1)[1], root, waived=waived, body=body)

        if path == "api/session":
            # The one deliberately empty session on the page. Everything else that
            # opens a terminal now knows what it is for; this is the exception Karel
            # asked to keep (2026-08-17: *"there can be a one button for that (so I
            # don't have to use VSC for general stuff)"*) — a session in this repo,
            # on the selected account, for work that has no card and no note yet.
            #
            # Empty of *work*, not of posture: with no card there is nothing to
            # inherit a tier from, so the rail's choice applies outright. This is
            # the button most likely to be opened for something small, which is
            # exactly the population Karel wants on sonnet.
            tier = effective_tier()
            open_terminal(root, *session_argv(root, name="general", tier=tier))
            return (f"opened a session in this repo — no card, no prompt"
                    f"{f', {tier} tier' if tier else ''}")

        if path == "api/work":
            return _work_verb(root, body)

        if path == "api/work-feedback":
            return _work_feedback_verb(root, body)

        if path == "api/talk":
            # **Resumed as a conversation, not as a worker.** `claude --resume <id>`
            # alone brought the attempt's charter back with the transcript — the CLI
            # stores it as `agent-setting` on the first line of the session's
            # `.jsonl` — so the thing that answered was still an autonomous worker
            # whose brief was to land the card, and a question read to it as an
            # instruction. Karel, 2026-08-19: *"'talk' button not just opens
            # conversation, but make it continue. It should allow me to ask a
            # question first."*
            #
            # In place rather than forked, his call: `/run` names one session per
            # attempt, and a fork would give the row a second id that the record
            # does not carry. The cost, stated because it is real — the questions
            # and answers land in the attempt's own transcript, so that file stops
            # being purely what the night did.
            session = str(body.get("session_id", ""))
            if not session:
                raise PanelError("no session_id given")
            open_terminal(root, *session_argv(
                root, name=f"talk {session[:8]}", resume=session,
                tier=effective_tier(), system_append=worker_prompt.RESUMED_FOR_TALK))
            return f"resuming {session} for a conversation — ask it something"

        if path == "api/triage":
            # **With the note or the card named, when one is given.** The button
            # used to open `claude --agent triage` and stop there, leaving you to
            # remember which of five notes you had meant and type it — for the one
            # route whose whole discipline is "one item at a time, deliberately".
            # The charter takes exactly one note per call, so the launcher may as
            # well say which. Interactive on purpose (§3.4): triage is investigative
            # work a person drives, never a `-p` dispatch.
            note = str(body.get("note", ""))
            card_id = str(body.get("card", ""))
            if note and card_id:
                raise PanelError("give a note or a card, not both")
            lane = board.board_rel(root)
            if card_id:
                # "Re-triage this" on the decide page (§3419): the card already
                # exists, in `needs-decision/`, and the maintainer just answered its
                # question — this re-scopes it rather than turning an inbox note
                # into a first card. `INTERACTIVE_TRIAGE` cannot serve this: it is
                # note-shaped (`note_path` inside `inbox/`), and the charter itself
                # only accepts "exactly one file from Board/inbox/" as input, so a
                # card here needs its own prompt rather than a different `.format()`
                # of the note one.
                card = board.find(root, card_id)
                if card is None:
                    raise PanelError(f"no card named {card_id!r} on the board")
                open_terminal(root, *session_argv(
                    root, name=f"triage {card.id}", agent="triage",
                    tier=effective_tier(),
                    prompt=worker_prompt.INTERACTIVE_RETRIAGE.format(
                        card_path=card.path.resolve().as_posix(), lane=lane.as_posix())))
                return f"opened a terminal running triage on {card.id}"
            # The prompt goes *through* `session_argv`, never appended to what it
            # returns: it is the function that knows the argv ends with a variadic
            # `--add-dir` and puts the `--` in. Appending here is how this line
            # silently dropped the note for one commit.
            open_terminal(root, *session_argv(
                root, name=f"triage {note}".strip(), agent="triage",
                tier=effective_tier(),
                prompt=(worker_prompt.INTERACTIVE_TRIAGE.format(
                    note_path=f"{lane.as_posix()}/inbox/{note}", lane=lane.as_posix())
                    if note else "")))
            return (f"opened a terminal running triage on {note}" if note else
                    "opened a terminal running the triage charter")

        if path == "api/audio/pick":
            # No dispatch guard and no `paid`: this spends nothing and calls no
            # model — the same posture as `api/answer`, a person recording a
            # decision, not the panel starting anything.
            #
            # Like `api/image/pick`, a pick also *answers* the card when it is parked
            # on the pick question (`settle._park_for_pick`) — before 2026-09-11 it
            # only wrote the JSON file, so nothing downstream ever learned of it and
            # `sound-for-taser` sat in `testing/` playing its synth stopgap while
            # Karel's pick waited in `.ai/audio_picks.json`. The card is read off
            # the path, not the body: `rel` is already confined to `.ai/runs/<card>/`.
            key = str(body.get("key", ""))
            rel = str(body.get("rel", ""))
            resolved = resolve_audio_path(root, rel)  # raises PanelError on anything bogus
            write_audio_pick(root, key, rel)
            picked = f"picked {rel!r} for {key!r}"
            card_id = resolved.relative_to((root / RUNS).resolve()).parts[0]
            answered = answer_audio_pick(root, card_id)
            return f"{picked} · {answered}" if answered else picked

        if path == "api/image/pick":
            # Two effects from one click, and the second is what closes the loop:
            # the pick is recorded, *and* — when the card is parked on exactly this
            # question (`settle._park_for_pick`) — written into its `## Thread` as
            # the answer. The deliberate second click stays where it is: an answer
            # is not a ticket to `tasks/` (`decide.write_answer`'s park-over-promote),
            # so "To tasks" on the decide page still releases the installing pass.
            #
            # No dispatch guard and no `paid`, like `api/audio/pick` and
            # `api/answer`: this spends nothing, starts nothing and calls no model.
            #
            # **Nothing is copied or installed.** Adoption means Project Tigress nouns —
            # `assets/items/`, a factory row — and the framework deliberately does
            # not own that vocabulary; see `audio-audition-review-in-command-center`
            # on why the panel records a pick and stops.
            card_id = str(body.get("card", ""))
            rel = str(body.get("rel", ""))
            resolve_image_path(root, rel)  # raises PanelError on anything bogus
            # Written *before* the answer, and it stays written if the answer
            # fails: the pick is machine-local state that is true either way — he
            # did choose that file — and discarding it would make him choose again
            # after fixing the manifest. The refusal below names the real problem.
            write_image_pick(root, card_id, rel)
            picked = f"picked {Path(rel).name!r} for {card_id!r}"
            card = board.find(root, card_id)
            if card is None or card.lane != "needs-decision":
                # Not a failure and not a lane move. A pick against a card that
                # already moved on (or was never on the board — a scratch run dir)
                # is still a pick worth keeping; inventing a move from whatever
                # lane it is in is exactly what this endpoint must not do.
                return f"{picked} · pick recorded only (not parked in needs-decision/)"
            try:
                answered = decide.write_answer(
                    root, card_id, [],
                    f"Adopt `{Path(rel).name}` — picked in the Command Center's "
                    f"image candidates from `{Path(rel).parent.as_posix()}`.")
            except decide.DecideError as exc:
                raise PanelError(f"{picked}, but the answer was refused: {exc}") from exc
            return f"{picked} · {answered}"

        return None


def _work_verb(root: Path, body: dict) -> str:
    """`Work on this`: an interactive session that already carries the work.

    One endpoint for both kinds of thing the page can hand a person, because the
    difference between them is two lines of prompt and one `--agent`, not two
    verbs. A card brings its own tier and worker; a note brings neither by
    definition — it is the route that exists *because* no agent was dispatched —
    so it runs at the lead tier with no charter and a prompt that says so.

    Not guarded by `_guard_dispatch_account`. That veto is for spend this panel
    starts and walks away from; an interactive session is a person choosing to
    spend, in front of the account chip that says whose money it is, and refusing
    it would leave no way to work at all on the account the veto names.
    """
    base = preflight.integration_base(root)
    card_id = str(body.get("card", ""))
    note = str(body.get("note", ""))
    if card_id and note:
        raise PanelError("give a card or a note, not both")

    if card_id:
        card = board.find(root, card_id)
        if card is None:
            raise PanelError(f"no card named {card_id!r} on the board")
        lane = board.finished_lane(card)
        prompt = worker_prompt.INTERACTIVE_CARD.format(
            card_id=card.id,
            branch=branches.work_branch(card.id, card.fields.get("branch", "")), base=base,
            card_path=card.path.resolve().as_posix(), finished_lane=lane,
            how_to_test=(worker_prompt.HOW_TO_TEST_STEP if card.verify == "play" else ""),
            tool_economy=worker_prompt.TOOL_ECONOMY, card_body=card.text,
            question_format=worker_prompt.QUESTION_FORMAT,
        )
        tier = effective_tier(card.tier)
        open_terminal(root, *session_argv(root, name=card.id, prompt=prompt,
                                         tier=tier, agent=card.worker))
        return (f"working {card.id} → {lane}/ — {tier or 'default'} tier"
                + (f" (card says {card.tier})" if card.tier and tier != card.tier else "")
                + (f", {card.worker}" if card.worker and card.worker != "none" else ""))

    if note:
        path = board.board_dir(root) / "inbox" / note
        if not path.is_file():
            raise PanelError(f"no note named {note!r} in the inbox")
        prompt = worker_prompt.INTERACTIVE_NOTE.format(
            base=base, note_path=path.resolve().as_posix(),
            tool_economy=worker_prompt.TOOL_ECONOMY,
            note_body=path.read_text(encoding="utf-8"),
        )
        # A note has no frontmatter to declare a tier, so `lead` is this verb's own
        # default — and the rail, when it has been given a choice, is a person
        # saying otherwise about the session in front of them.
        tier = effective_tier("lead")
        open_terminal(root, *session_argv(root, name=note, prompt=prompt, tier=tier))
        return f"working {note} — {tier or 'default'} tier"

    raise PanelError("no card or note given")


def _work_feedback_verb(root: Path, body: dict) -> str:
    """`Open inline`: the Verify page's other "not OK" tool — reopen the session
    that actually built the card, so it already has the context, told to wait for
    the maintainer's own account of what was wrong before it fixes anything.

    **Resumes by preference, the same way `Talk` does** (`_latest_session_for_card`
    + `RESUMED_FOR_TALK`) — this button differs from `Talk` only in *how* the
    session is found (by card id, since the Verify page has no row-level
    `session_id` the way the Run page's dispatch history does), not in what
    happens once it opens: wait for the maintainer, fix if asked, and close out
    — merge, delete branch, land the card — only once they say it is good
    (Karel, 2026-08-27: close-out should work "from anywhere", gated on
    confirmation rather than on which lane the card happens to be reopened in).

    **Falls back to a fresh session** (`INTERACTIVE_CARD_FEEDBACK`, same branch/
    tier/charter `_work_verb` would use) when no recorded session survives — a
    card never dispatched through the runner, or whose `.ai/runs/` artefacts are
    gone (gitignored, machine-local, pruned). Unlike `boardcmd rejected`, nothing
    here touches the board on open: the card stays where it is and the session
    decides, with the maintainer, whether and where to land it.
    """
    card_id = str(body.get("card_id", ""))
    card = board.find(root, card_id)
    if card is None:
        raise PanelError(f"no card named {card_id!r} on the board")

    session = _latest_session_for_card(root, card)
    if session:
        open_terminal(root, *session_argv(
            root, name=f"{card.id} feedback", resume=session,
            tier=effective_tier(card.tier), system_append=worker_prompt.RESUMED_FOR_TALK))
        return f"resuming {card.id}'s own session — say what was wrong"

    base = preflight.integration_base(root)
    lane = board.finished_lane(card)
    prompt = worker_prompt.INTERACTIVE_CARD_FEEDBACK.format(
        card_id=card.id,
        branch=branches.work_branch(card.id, card.fields.get("branch", "")), base=base,
        card_path=card.path.resolve().as_posix(), finished_lane=lane,
        tool_economy=worker_prompt.TOOL_ECONOMY, card_body=card.text,
        question_format=worker_prompt.QUESTION_FORMAT,
    )
    tier = effective_tier(card.tier)
    open_terminal(root, *session_argv(root, name=f"{card.id} feedback", prompt=prompt,
                                     tier=tier, agent=card.worker))
    return f"opened a terminal on {card.id}, waiting for your feedback"


def read_body(root: Path, target: str) -> str:
    """One board file's **body**, for the person editing it in their own browser.

    Body and not the whole file, and the distinction is the bug this is named
    after. `boardcmd.edit_body` — the one write this read feeds — keeps the file's
    existing frontmatter verbatim and replaces everything after it with the text
    it is handed. So handing the textarea the whole file made the round trip
    asymmetric: whatever the person saved was spliced in *after* a frontmatter
    block the file already had, and the block appeared twice.

    Nothing complained, because a doubled block is only malformed to a reader that
    parses past the first one: the note rendered, its lane was right, and the
    duplication surfaced days later on `boardcmd.close_note`, the one verb that
    inspects a note's fields (Karel, 2026-09-19: *"Close the note, I can't do it in
    this state."*). Measured on `unify-runner-and-chores`: one edit through this
    page, +7 lines, four of them a byte-identical second block.

    Editing the frontmatter here never worked in the first place — `edit_body`
    preserves it by contract — so showing it in the textarea offered an edit that
    could not land, and this is also what `/card/<id>` has always done with the
    same text (`split_frontmatter`, the fields rendered as their own strip).

    **This is the human's own read, and it is the only one.** `hooks.ideas_fence`
    stops an *agent* opening a private note, and `boardcmd edit` exists because of
    it — its docstring says editing one "has to be an operation the human drives,
    with the text arriving from their editor". A textarea in the maintainer's
    browser is that editor, and it cannot be prefilled without reading the file.
    So the bytes go straight to the person who owns them: nothing summarises them,
    no model sees them, and they reach no commit message, log line or report.

    Confined to the board for the same reason `boardcmd.edit_body` is — a resolved
    path outside it is the one failure that could not be undone from the board.
    """
    path = Path(target)
    path = (path if path.is_absolute() else root / path).resolve()
    lanes = board.board_dir(root).resolve()
    if lanes not in path.parents:
        raise PanelError(f"{target} is not inside the board")
    if not path.is_file():
        raise PanelError(f"{target} does not exist")
    return split_frontmatter(path.read_text(encoding="utf-8"))[1]


def _reorder_many(root: Path, writes: list) -> str:
    """Persist a drag: one `boardcmd reorder` per card whose order actually moved.

    The no-op filter is not an optimisation, it is what keeps a drag from
    rewriting — and committing — every card in the lane every time one moves.
    """
    changed = 0
    for entry in writes:
        if not isinstance(entry, dict):
            continue
        card_id, order = str(entry.get("card_id", "")), str(entry.get("order", ""))
        if not card_id or not order:
            continue
        card = board.find(root, card_id)
        if card is None or card.fields.get("kanban_order", "") == order:
            continue
        _verb(run_command("boardcmd", ["reorder", card_id, order], root))
        changed += 1
    return f"{changed} card(s) reordered" if changed else "order unchanged"


def _verb(result: subprocess.CompletedProcess) -> str:
    text = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise PanelError(text or f"exited {result.returncode}")
    return text


class _Server(ThreadingHTTPServer):
    """`ThreadingHTTPServer`, minus the address reuse it turns on by default.

    **`allow_reuse_address = 1` is a lie about safety on Windows.** It sets
    `SO_REUSEADDR`, which on Linux only shortens the TIME_WAIT wait, but on Windows
    lets a *second live socket* bind a port another process is already listening
    on. Both then "serve" and connections land on either, so a second Command
    Center starts without complaint and the page you are reading may come from
    whichever process the OS felt like — including one holding code from before
    your last change.

    Measured on 2026-08-17: three restarts in a row appeared not to take. Each time
    the old panel had survived, the new one bound alongside it, and the fix under
    test was reported as live while the page came from the previous build. It cost
    two false "verified" claims, which is the specific harm — the panel exists to
    say what is true, and a duplicate of it says what *was* true.

    So the port is exclusive: the second panel fails to start and says why, which
    is the behaviour a person wants from "the port is already in use".
    """

    allow_reuse_address = False


def already_serving(port: int) -> bool:
    """Whether a Command Center is answering on this port already.

    Asked when the bind fails, to tell the two reasons apart. A panel already
    running is **not an error** — it is the thing the person double-clicking the
    launcher wanted, and they should get it rather than a refusal. Something else
    holding the port is an error and needs saying.

    Identified by asking it: a page of ours carries its own wordmark. A bare port
    probe cannot distinguish a panel from anything else that happens to listen.
    """
    try:
        with urlopen(f"http://127.0.0.1:{port}/queue", timeout=2) as answer:
            return b"Command Center" in answer.read(4096)
    except Exception:                             # noqa: BLE001 — any failure is "not ours"
        return False


def serve(root: Path, port: int = DEFAULT_PORT, *, open_browser: bool = True) -> None:
    Handler.root = root
    try:
        server = _Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        # **Two reasons, two answers, and conflating them broke the launcher.**
        # Making the port exclusive was right — a second panel used to bind
        # alongside the first on Windows and serve stale code. But refusing
        # outright turned the ordinary case, "I clicked the launcher and a panel is
        # already up", into an instant exit; the `.bat` has no pause, so the window
        # vanished before the reason could be read. Karel, 2026-08-17: *"Running
        # the bat opens and immediately closes the window and no browser page
        # opens."*
        url = f"http://127.0.0.1:{port}/queue"
        if already_serving(port):
            print(f"Command Center is already serving at {url} — opening that one.\n"
                  f"  Stop it and run this again if you want it restarted "
                  f"(a running panel holds the code it started with).")
            if open_browser:
                webbrowser.open(url)
            return
        print(f"Command Center: port {port} is in use by something that is not a "
              f"panel ({exc}).\n"
              f"  Free the port, or start on another one with --port.")
        raise SystemExit(2) from exc
    url = f"http://127.0.0.1:{port}/queue"
    print(f"Command Center serving {root} at {url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The Command Center: a local web launcher for the board and the runner.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo root (default: found from the cwd)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("--dispatch-cards", nargs="+", metavar="ID", default=None,
                        help="run `runner --card <id>` for each id, in order, and exit. "
                             "This is what the panel's 'run the ticked' button spawns; it "
                             "owns no dispatch logic of its own")
    parser.add_argument("--sessions", type=int, default=0, metavar="N",
                        help="with --dispatch-cards: how many usage-limit windows each "
                             "card's run may spend, passed straight to `runner --sessions`. "
                             "0 (default) passes nothing and leaves the runner's own default")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    root = (args.root or find_root()).resolve()
    if args.dispatch_cards:
        return dispatch_cards(list(args.dispatch_cards), root, sessions=args.sessions)
    serve(root, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
