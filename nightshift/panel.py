#!/usr/bin/env python3
"""The Command Center — a launcher, a registry and a tail, never a chat client.

`.claude/plans/dispatch-cost-and-control-panel.md` (a Dungeoneer doc) §3.4 is the design
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

**Pages are server-rendered, one URL each.** The approved mockup switches its five pages
with JavaScript because a static mockup has no server; here `/now`, `/verify`, `/inbox`,
`/ideas` and `/run` are real addresses, so the left rail is links. That keeps deep-linking
and the reload-after-an-action behaviour every button depends on, and it means a page costs
only its own reads — the Verify page's per-card `git` calls are not paid for by someone
looking at Ideas. The *appearance* is the mockup's; only the mechanism differs.

**Two ways to be on a different account, and the panel owns neither of them.** A repo may
declare `[[accounts]]`, each naming its own `CLAUDE_CONFIG_DIR`; selecting one sets that
variable for this process's dispatch subprocesses only (`runner._worker_env` inherits the
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
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from nightshift import (board, drain, freshness, ingest, init, jobs, manifest,
                        run_record, textio, update, usage)
from nightshift.manifest import ManifestError, find_root
from nightshift.runner import (
    RUNS,
    STATUS_FILE,
    STOP_FILE,
    Candidate,
    branch_has_commits,
    current_branch,
    default_base,
    host_capabilities,
    read_telemetry,
    schema_violations,
)
# Private, and imported rather than reimplemented on purpose: "is this pid still
# alive" carries a Windows-specific subtlety (`tasklist`, not a signal) that
# `print_status` already got right, and a second copy here would be the same
# question answered twice. It is the one thing standing between a heartbeat file
# and the claim that a run is live.
from nightshift.runner import _pid_alive
from nightshift.runner import select as select_candidates

DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "panel_static"
TEMPLATE = STATIC_DIR / "app.html"

PAGES = ("now", "verify", "inbox", "ideas", "run", "system")

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
#: Phases that mean the run is past every step above.
_PHASE_DONE = frozenset({"digest", "finished"})


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
    global _ACCOUNT, _METERS
    # A different account has different meters, so the cached reading is not just
    # stale, it is about someone else.
    _METERS = None
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


def spawn_sequence(card_ids: list[str], root: Path) -> int:
    """Dispatch each card in turn, in the order given, detached from this process.

    This is what "run the ticked cards" means, and it deliberately owns **no
    dispatch logic**: it runs `python -m nightshift.panel --dispatch-cards a b c`,
    whose whole body is a loop calling `runner --card` — the runner's own per-card
    path, once per card, in the panel's order. A subset could not otherwise be
    expressed: `runner` with no arguments takes the whole queue, and there is no
    flag for "these five".

    Its own module is the sequencer for the same reason the verbs are commands:
    the thing the button does can be typed into a terminal and watched.
    """
    argv = [sys.executable, "-m", "nightshift.panel", "--dispatch-cards", *card_ids]
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


def dispatch_cards(card_ids: list[str], root: Path) -> int:
    """`--dispatch-cards`: run `runner --card <id>` for each id, in order.

    Stops at the first card whose run exits non-zero — a night that could not
    finish card 2 has no business starting card 3 on the same assumption, and the
    runner's own exit code is the only judgment consulted. Nothing here reads a
    board, moves a card or decides an outcome.
    """
    for card_id in card_ids:
        print(f"panel: dispatching {card_id}", flush=True)
        result = subprocess.run(_module_argv("runner", "--card", card_id), cwd=root,
                                check=False)
        if result.returncode != 0:
            print(f"panel: {card_id} exited {result.returncode} — stopping the sequence",
                  flush=True)
            return result.returncode
    return 0


def open_terminal(root: Path, *command: str) -> None:
    """Hand `command` to a new, visible console window rather than running it here.

    This is the "talk to this one" / "launch triage" gesture (§3.4: *"it is a
    launcher, a registry and a tail — never a chat client"*). `claude --resume
    <session_id>` is the concrete case `runner.py` already supports (the session id
    is captured per attempt); triage is `claude --agent triage` — deliberately
    interactive, because triage is investigative work a person drives, not a
    one-shot `-p` dispatch this module could run for them.
    """
    if os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "Command Center", "cmd", "/k", *command],
                         cwd=root)  # gate-ok(subprocess_result_checked): a detached, visible
                                    # console window the user drives from here on — there is
                                    # nothing this function could do with an exit code from a
                                    # window that has not been interacted with yet
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", " ".join(command)], cwd=root,
                         shell=False)  # gate-ok(subprocess_result_checked): same reason as the
                                       # Windows branch just above


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


#: How long a meter reading is reused before the endpoint is asked again.
#: The meters are ambient — they are on every page — so without this every click
#: in the rail is another HTTP call, and the endpoint starts answering 429, which
#: is exactly what it did after a few minutes of paging around. A minute is far
#: shorter than the windows being metered (five hours, seven days) and long
#: enough that browsing costs nothing.
METERS_CACHED_FOR = dt.timedelta(seconds=60)
#: `(taken at, whose, reading)`.
_METERS: tuple[dt.datetime, str, usage.Snapshot] | None = None


def read_meters(credentials: Path | None, *, account_key: str = "",
                force: bool = False) -> usage.Snapshot:
    """The usage snapshot, reused for `METERS_CACHED_FOR` — per account.

    Only the *network* read is cached. `usage.read_identity` is a local file and
    is never cached anywhere — it is what `_guard_dispatch_account` vetoes on, and
    a safety check answering from a minute-old copy is not a safety check.

    **`account_key` is not decoration.** The ordinary way to change accounts here
    is `claude auth login` against the *same* config directory, so the credential
    path never changes and a cache keyed on time alone would keep serving the
    previous account's headroom under the new account's name for up to a minute
    after a switch.
    """
    global _METERS
    now = dt.datetime.now()
    if (not force and _METERS is not None and _METERS[1] == account_key
            and now - _METERS[0] < METERS_CACHED_FOR):
        return _METERS[2]
    snapshot = usage.read(credentials)
    _METERS = (now, account_key, snapshot)
    return snapshot


def read_rail(root: Path, *, fetch_freshness: bool = False) -> Rail:
    credentials, identity_path = _account_paths(_ACCOUNT)
    identity = usage.read_identity(identity_path)
    snapshot = read_meters(credentials,
                           account_key=identity.account_uuid or identity.email)
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
    decisions: list[board.Card] = field(default_factory=list)
    testing: list[board.Card] = field(default_factory=list)
    review: list[board.Card] = field(default_factory=list)
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

    @property
    def inline_notes(self) -> list[tuple[ingest.Note, ingest.Decision]]:
        """Notes the classifier sent back to Karel, for the page about Karel's work.

        Not a lane and not a card — which is exactly why they were invisible from
        `/now`: every section there reads a board lane, and an inline note stays a
        note on purpose (`ingest`'s four routes: *"inline — Karel, at the keyboard.
        No card is written for these"*).
        """
        return [(note, decision) for note in self.notes
                if (decision := self.routing.of(note.name)) and decision.route == "inline"]

    @property
    def triage_notes(self) -> list[tuple[ingest.Note, ingest.Decision]]:
        """Notes the classifier said need the expensive route, newest routing first."""
        return [(note, decision) for note in self.notes
                if (decision := self.routing.of(note.name)) and decision.route == "triage"]

    @property
    def chores(self) -> list[Candidate]:
        """The chore batch's work — which the night skips by *routing*, not refusal.

        `runner.select` is explicit that this is "not a refusal — a routing fact":
        a chore is dispatched by `python -m nightshift.chores` as a batch, because
        the per-card treatment is exactly what the batch exists to avoid. Filing it
        under "Do now" therefore said something false — it told you a person was
        needed at the keyboard for work that has its own button two sections down,
        under a chip that truncated the explanation mid-sentence at seventy
        characters.

        A chore that *also* wants another machine is left to `elsewhere`, which is
        the more useful of the two facts: the batch here cannot take it either.
        """
        elsewhere = {id(c) for c in self.elsewhere}
        return [c for c in self.candidates
                if not c.dispatchable and c.card.kind == board.KIND_CHORE
                and id(c) not in elsewhere]

    @property
    def do_now(self) -> list[Candidate]:
        """Everything else the night will not take — `unattended: false`,
        `worker: none`, a broken schema. Which is the same list as the inline
        notes: it needs a person present."""
        parked = {id(c) for c in self.elsewhere} | {id(c) for c in self.chores}
        return [c for c in self.candidates if not c.dispatchable and id(c) not in parked]

    def counts(self) -> dict[str, int]:
        return {
            "now": len(self.decisions) + len(self.do_now) + len(self.tonight)
                   + len(self.elsewhere) + len(self.chores) + len(self.inline_notes),
            "verify": len(self.testing) + len(self.review),
            "inbox": len(self.notes),
            "ideas": len(self.ideas),
            "run": len(_latest_record(self.root).get("dispatched", [])),
            # What the System page would ask you to look at: files needing an update
            # or a decision. Deliberately not "everything nightshift could do here" —
            # a rail number that is never zero is a rail number nobody reads.
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
        notes=ingest.notes(root),
        ideas=read_ideas(root),
        routing=ingest.read_view(root),
        jobs=jobs.read_all(root, limit=JOBS_READ),
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
    """Where one attempt's artefacts are. Deliberately **not** `runner.run_dir`,
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


def diff_stat(root: Path, base: str, branch: str) -> str:
    """`+38 −12 · 2 files`, or `""` when git cannot say.

    Read rather than stored: the card carries no diff stat, and the branch is
    right there. A failure is silence — a missing stat must not be able to stop
    a page rendering.
    """
    try:
        done = subprocess.run(["git", "diff", "--shortstat", f"{base}...{branch}"],
                              cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    text = done.stdout.strip()
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
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


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
         control: str = "", card_id: str = "") -> str:
    """One register line: grip · control · marker · body · actions."""
    classes = "row" if grip else "row no-grip"
    grip_cell = ('<span class="grip" title="Drag to reorder">&#x2059;</span>'
                 if grip else '<span class="grip">&nbsp;</span>')
    data = f' data-id="{_e(card_id)}"' if card_id else ""
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
    """The card's tags, as chips.

    `nightshift` is the one with operational meaning — the deliverable lands in
    the framework repo, so the runner cannot cut a worktree for it and the card
    carries `unattended: false` to match. Worth seeing at a glance, since it is
    the difference between a card the night can take and one it never will.
    """
    return [_chip(tag, "warn" if tag == "nightshift" else "mute") for tag in card.tags]


def _card_body(card: board.Card, *, meta: list[str] | None = None, why: str = "") -> str:
    out = [f'<span class="id">{_e(card.id)}</span>']
    if card.title and card.title != card.id:
        out.append(f'<span class="title">{_e(card.title)}</span>')
    if why:
        out.append(f'<p class="why">{_e(why)}</p>')
    out.append(_meta(_tag_chips(card) + (meta or [])))
    return "".join(out)


def _section(title: str, count: int, rows: str, *, note: str = "", sub: str = "",
             bar: str = "", empty: str = "", rows_id: str = "") -> str:
    head = [f'<div class="sec-head"><h2>{_e(title)}</h2>'
            f'<span class="count">{count}</span>']
    if note:
        head.append(f'<p class="note">{_e(note)}</p>')
    head.append("</div>")
    out = ["<section>", "".join(head)]
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


def _rail_html(ctx: Context, active: str) -> str:
    counts = ctx.counts()
    try:
        project = manifest.load(ctx.root).project.name
    except ManifestError:
        project = ctx.root.name
    links = []
    for page in PAGES:
        current = ' aria-current="page"' if page == active else ""
        links.append(f'<a href="/{page}"{current}>{page.capitalize()} '
                     f'<span class="n">{counts[page]}</span></a>')
    links = "".join(links)
    return (
        '<nav class="rail">'
        f'<div class="wordmark"><b>Command Center</b><span>{_e(project)}</span></div>'
        f'<div class="pages">{links}</div>'
        f'<div class="machine">{"<br>".join(machine_lines(ctx.root))}</div>'
        '</nav>'
    )


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
    try:
        if dt.datetime.now() - dt.datetime.fromisoformat(updated) > HEARTBEAT_TRUSTED_FOR:
            return False
    except (TypeError, ValueError):
        return False
    try:
        return _pid_alive(int(status.get("pid") or 0))
    except (TypeError, ValueError):
        return False


def _runbox_html(ctx: Context) -> str:
    status = ctx.rail.run_status
    record = _latest_record(ctx.root)
    card_id = str(status.get("card") or "") if run_is_live(status, record) else ""
    landed = len(run_record.landed(record))
    failed = len(run_record.failures(record))
    queued = len(ctx.tonight)

    shown = shown_jobs(ctx.jobs)
    live_jobs = [job for job, state in shown if state == jobs.RUNNING]

    if not card_id:
        last = str(status.get("card") or "")
        when = str(status.get("updated") or "")
        tail = (f"Last was {last} at {when[11:16]}." if last and when else
                "Nothing has dispatched on this machine yet.")
        # **"No run in progress" over a pulsing `ingest` is a contradiction**, and
        # it is one a reader resolves against us: Karel read the rail with a live
        # classify pass on it and reported the panel as showing nothing about it.
        # The eyebrow is a claim about the *queue* — no card is being dispatched —
        # so when a background command is running it says that instead of implying
        # the machine is idle. Both statements were always true; only one of them
        # was on screen.
        eyebrow = ("No card dispatching" if live_jobs else "No run in progress")
        head = (f'<p class="eyebrow">{_e(eyebrow)}</p>'
                f'<div class="runline"><span class="dim">{_e(tail)}</span></div>')
    else:
        attempt = status.get("attempt")
        telemetry = read_telemetry(attempt_dir(ctx.root, card_id, int(attempt or 1)))
        facts = [("worker", status.get("worker", "?")), ("model", status.get("model", "?"))]
        if attempt:
            facts.append(("attempt", str(attempt)))
        if elapsed := elapsed_since(str(status.get("since") or "")):
            facts.append(("elapsed", elapsed))
        if telemetry.get("turns"):
            facts.append(("turns", str(telemetry["turns"])))
        pairs = "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in facts)
        head = (
            '<p class="eyebrow"><span class="live-dot"></span> Running now</p>'
            f'<div class="runline"><span class="card-id">{_e(card_id)}</span>'
            f'<dl>{pairs}</dl></div>'
            + _phases_html(str(status.get("phase") or ""))
        )
    tally = (f'<p class="tallyline"><b>{landed}</b> landed &middot; '
             f'<b>{failed}</b> back in tasks &middot; <b>{queued}</b> queued'
             f'{_act("Run detail", href="/run")}</p>')
    return f'<div class="runbox">{head}{_jobs_html(shown)}{tally}</div>'


#: How long a finished job stays in the rail. Long enough that a classify pass
#: started, watched, and left alone for a coffee still says how it went when you
#: come back; short enough that the rail is not a history page. `/run` is where
#: history lives.
JOB_SHOWN_FOR = dt.timedelta(hours=2)

#: Most rows the rail will carry. A night is one job, but a click-happy minute
#: can be five, and the rail is a status line rather than a list.
JOB_ROWS = 4

_JOB_MARK = {jobs.RUNNING: ("", "running"), jobs.DONE: ("ok", "finished"),
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
    """
    now = now or dt.datetime.now()
    keep = []
    for job in all_jobs:
        if len(keep) == JOB_ROWS:
            break
        status = jobs.state(job, now=now)
        finished = job.finished_at
        if status == jobs.RUNNING or (finished and now - finished < JOB_SHOWN_FOR):
            keep.append((job, status))
    return keep


def _jobs_html(shown: list[tuple[jobs.Job, str]]) -> str:
    """The rail's line per background command the panel started.

    This is the panel saying what *it* set in motion, which is a different
    question from `status.json`'s "where is the runner up to" — and it is the one
    that was unanswerable. A dispatch shows both: the phase pills above say how
    far the night has got, this line says the process is alive and where its
    output is.

    Takes the already-computed list rather than the context, because the caller
    needs the same answer for its own eyebrow and `jobs.state` is not free — it
    shells out to `tasklist` on Windows for every job with no finish on file.
    """
    if not shown:
        return ""
    rows = []
    for job, status in shown:
        kind, word = _JOB_MARK.get(status, ("", status))
        took = jobs.elapsed(job)
        when = f"{word} &middot; {_e(took)}" if took else word
        if status == jobs.FAILED and job.exit_code is not None:
            when += f" &middot; exit {job.exit_code}"
        dot = '<span class="live-dot"></span>' if status == jobs.RUNNING else ""
        rows.append(
            f'<p class="jobline">{dot}{_chip(job.label, kind)}'
            f'<span class="dim">{when}</span>'
            f'{_act("Output", href=f"/log/{job.ident}")}</p>')
    return f'<div class="jobs">{"".join(rows)}</div>'


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


def _meters_html(ctx: Context) -> str:
    snapshot = ctx.rail.snapshot
    out = ['<p class="eyebrow">Allowance</p>']
    if not snapshot.fetched:
        out.append(f'<p class="resets">{_e(snapshot.reason or "no reading")}</p>')
    for bucket in shown_buckets(snapshot):
        fill = min(100.0, max(0.0, bucket.utilization))
        kind = "bad" if bucket.exhausted else ("warn" if bucket.headroom_pct <= 15 else "")
        resets = (f'<p class="resets">resets {bucket.resets_at:%d %b %H:%M}</p>'
                  if bucket.resets_at else "")
        label = METER_LABELS.get(bucket.name, bucket.name.replace("_", " "))
        out.append(
            f'<div class="meter"><div class="meter-head">'
            f'<span>{_e(label)}</span>'
            f'<span>{bucket.utilization:.0f}%</span></div>'
            f'<div class="track"><i class="{kind}" style="width:{fill:.0f}%"></i></div>'
            f'{resets}</div>'
        )

    spend = []
    if snapshot.paid_enabled:
        used = snapshot.paid_used_display
        spend.append(_chip(f"paid overage on{f' · {used}' if used else ''}", "warn"))
    elif snapshot.fetched:
        spend.append(_chip("paid overage off", "ok"))
    if ctx.rail.identity.fetched and ctx.rail.identity.has_extra_usage_enabled:
        spend.append(_chip("API spend enabled — dispatch refused", "bad"))
    out.append(f'<div class="spend">{"".join(spend)}{_account_html(ctx)}</div>')
    return f'<div class="meters">{"".join(out)}</div>'


def _account_html(ctx: Context) -> str:
    rail = ctx.rail
    label = rail.account_label or "ambient"
    email = rail.identity.email if rail.identity.fetched else "identity unavailable"
    never = (' <span class="chip bad">dispatch: never</span>'
             if rail.account_label and rail.account_dispatch == "never" else "")
    # Two different ways to be on a different account, and only one of them is a
    # dropdown. `[[accounts]]` + `CLAUDE_CONFIG_DIR` points at a *separate config
    # directory*, which is a per-invocation choice this server can make. The
    # ordinary way, though, is `claude auth login` against the one config
    # directory you already have — settings and history stay put and the logged-in
    # identity swaps underneath. That needs a browser, so the panel can only
    # *launch* it; what it does own is reading who is logged in now, on every page
    # load, so the answer is never stale.
    selector = ""
    if rail.accounts:
        options = ['<option value="">(ambient)</option>']
        for account in rail.accounts:
            selected = " selected" if account.label == rail.account_label else ""
            options.append(f'<option value="{_e(account.label)}"{selected}>'
                           f'{_e(account.label)}</option>')
        selector = ('<br><select title="Accounts declared in [[accounts]], each a '
                    'separate CLAUDE_CONFIG_DIR" '
                    'onchange="post(\'/api/account\',{label:this.value})">'
                    + "".join(options) + "</select>")
    # The override lives here, next to the account it waives and on every page —
    # §3.4 is explicit that the account in force must be visible *at the moment of
    # dispatch*, and a waiver parked on one page while the Dispatch buttons sit on
    # three is the same failure as a switcher that is off-screen when you click.
    # One control, one state: two copies would be two selections that can disagree.
    override = ('<label class="override" title="Two waivers, one tick, and neither is '
                'remembered: the account exclusion (this panel refuses an account with API '
                'spend enabled) and the money rule (checked by the chore batch, ingest and '
                'the drain before they spend). A night is not gated on headroom at all — '
                'limits.py stops it reactively after a wall — so the money half cannot '
                'change what a night does.">'
                '<input type="checkbox" id="allowpaid"> Override, this once</label>')
    switch = _act("Switch account", onclick="post('/api/switch-account',{})",
                  extra='title="Opens a terminal running `claude auth login`. The '
                        'sign-in is a browser flow, so the panel launches it and does '
                        'not carry it out; reload this page afterwards and the rail '
                        'will name whoever is logged in then."')
    return (f'<p class="account">account <b>{_e(label)}</b>{never}<br>{_e(email)}'
            f'{selector}</p><div class="acts" style="justify-content:flex-start">'
            f'{switch}{override}</div>')


def _statusrail_html(ctx: Context) -> str:
    fresh_class = "" if ctx.rail.freshness_known else "warn"
    return (
        '<div class="statusrail"><div class="top">'
        + _runbox_html(ctx) + _meters_html(ctx) +
        '</div>'
        f'<p class="tallyline" style="padding:0 1.5rem 0.9rem;margin:0">'
        f'<span class="{fresh_class}">{_e(ctx.rail.freshness_line)}</span>'
        f'{_act("Refresh", onclick="post(\'/api/freshness/refresh\',{})")}'
        f'{_act("Pull", onclick="post(\'/api/freshness/pull\',{})")}</p>'
        '</div>'
    )


# ------------------------------------------------------------------ the pages


def _chores_section(ctx: Context) -> str:
    """The chore batch, as its own section with its own button.

    Its own section because a chore is neither of the two things the sections
    around it are: not work waiting on a person, and not a card the night takes
    one at a time. It has a third answer — one batch, one verified suite run —
    and a heading is the cheapest way to say so.
    """
    rows = []
    for candidate in ctx.chores:
        card = candidate.card
        meta = [_chip("chore", "ok"), _e(card.worker)]
        if card.surface:
            meta.append(_e(card.surface))
        if card.attempts:
            meta.append(_e(f"{card.attempts} attempt(s)"))
        rows.append(_row(marker="&middot;", body=_card_body(card, meta=meta),
                         acts=_act("Read card", href=f"/card/{card.id}")))
    bar = ('<div class="barbox">'
           '<p>One batch: a cheap pass per item, then one full suite run over the '
           'merged result.</p><div class="acts">'
           + _act("Run chores", onclick="runChores()", primary=True)
           + '</div></div>')
    return _section("Chores", len(ctx.chores), "".join(rows),
                    note="Batched, not dispatched one at a time.",
                    bar=bar if rows else "",
                    empty="No chores are waiting.")


def _render_now(ctx: Context) -> str:
    out = []

    rows = "".join(
        _row(marker="?", body=_card_body(
                card, meta=[_e(f"in needs-decision/ · {card.fields.get('created', '')}")]),
             acts=_act("Read card", href=f"/card/{card.id}"))
        for card in ctx.decisions
    )
    out.append(_section("Decide", len(ctx.decisions), rows,
                        note="Nothing else moves until this does.",
                        empty="Nothing is waiting on a decision."))

    do_now = ctx.do_now
    inline_rows = []
    for candidate in do_now:
        card = candidate.card
        meta = [_chip(candidate.reason.split(";")[0][:70])]
        if card.attempts:
            meta.append(_e(f"{card.attempts} attempt(s)"))
        inline_rows.append(_row(
            marker="&rsaquo;", body=_card_body(card, meta=meta),
            acts=_act("Read card", href=f"/card/{card.id}")
                 + _act("Start session", onclick="post('/api/session',{})", primary=True)))
    inline_rows = "".join(inline_rows)
    # The other half of the same question. A note routed `inline` is, by the route's
    # definition, work for Karel at the keyboard that will never become a card — so
    # the page that answers "what needs me" was answering half of it and pointing at
    # another page for the rest, with nothing on either saying so.
    note_rows = "".join(
        _row(marker="&rsaquo;",
             body=(f'<span class="id">{_e(note.name)}</span>'
                   + (f'<p class="why">{_e(decision.why)}</p>' if decision.why else "")
                   + _meta([_chip("note", "mute"), f"{note.size} B"])),
             acts=_act("Open note", href=f"/body/{_rel(ctx.root, note.path)}")
                  + _act("Start session", onclick="post('/api/session',{})")
                  + _done_act(note.name))
        for note, decision in ctx.inline_notes)
    body = ((_group("Cards the night cannot take") + inline_rows) if inline_rows else "")
    if note_rows:
        body += _group(f"Notes routed to you — {len(ctx.inline_notes)}") + note_rows
    out.append(_section("Do now", len(do_now) + len(ctx.inline_notes), body,
                        note="Work that needs you at the keyboard.",
                        empty="Nothing needs you at the keyboard."))

    out.append(_chores_section(ctx))

    tonight = ctx.tonight
    # The same liveness question the rail asks, asked once here: a stale heartbeat
    # must not grey out a card's Dispatch button for a run that ended days ago.
    live_card = (str(ctx.rail.run_status.get("card") or "")
                 if run_is_live(ctx.rail.run_status, _latest_record(ctx.root)) else "")
    queue_rows = []
    for position, candidate in enumerate(tonight, start=1):
        card = candidate.card
        meta = [_e(card.worker), _e(f"verify: {card.verify}")]
        if card.attempts:
            meta.append(_e(f"attempt {card.attempts + 1}"))
        running = live_card == card.id
        if running:
            meta.append(_chip("running", "ok"))
        control = (f'<input type="checkbox" class="pick" data-id="{_e(card.id)}" checked '
                   f'aria-label="include {_e(card.id)}">')
        acts = _act("Read card", href=f"/card/{card.id}")
        acts += (_act("Running", disabled=True) if running else
                 _act("Dispatch", onclick=f"post('/api/dispatch',{{card_id:'{_attr(card.id)}'}})",
                      primary=True))
        queue_rows.append(_row(grip=True, card_id=card.id, control=control,
                               marker=str(position), body=_card_body(card, meta=meta),
                               acts=acts))

    elsewhere_rows = "".join(
        _row(marker="&mdash;", body=_card_body(
                c.card, meta=[_chip(f"requires {c.card.requires}"), _e("waits for the other machine")]),
             acts=_act("Read card", href=f"/card/{c.card.id}"))
        for c in ctx.elsewhere
    )
    body = "".join(queue_rows)
    if elsewhere_rows:
        body += _group(f"Dispatchable, but not here — {len(ctx.elsewhere)}") + elsewhere_rows

    bar = (
        '<div class="barbox">'
        '<p>Order is saved to each card as you drag. <span id="picked">0 of 0</span> ticked.</p>'
        '<label class="takefirst">Take first'
        f'<input type="range" id="takefirst" min="0" max="{len(tonight)}" value="{len(tonight)}">'
        '<output for="takefirst" id="takefirstout">0</output></label>'
        '<div class="acts">'
        + _act("Run chores", onclick="runChores()")
        + _act("Run the whole queue", onclick="runNight()")
        + _act("Run the ticked", onclick="runTicked()", primary=True)
        + '</div></div>'
    )
    out.append(_section("Tonight", len(tonight), body, rows_id="queue",
                        note="Drag to set the order. Ticked cards are what a run takes.",
                        bar=bar if tonight else "",
                        empty="Nothing is dispatchable here right now."))

    # The notes themselves, one row each, each with its own button — not a single
    # row for `Routing.md` and one "Launch triage" that named no note. Triage takes
    # exactly one note per call and is the route whose cost is the reason the
    # classifier exists, so "which one, and on purpose" is the entire gesture.
    waiting = ctx.triage_notes
    triage_rows = "".join(
        _row(marker="&rsaquo;",
             body=(f'<span class="id">{_e(note.name)}</span>'
                   + (f'<p class="why">{_e(decision.why)}</p>' if decision.why else "")
                   + _meta([_chip("triage", "warn"), f"{note.size} B"]
                           + ([_chip(f"confidence {decision.confidence}", "warn")]
                              if decision.confidence != "high" else []))),
             acts=_act("Open note", href=f"/body/{_rel(ctx.root, note.path)}")
                  + _act("Triage this",
                         onclick=f"post('/api/triage',{{note:'{_attr(note.name)}'}})",
                         primary=True))
        for note, decision in waiting)
    out.append(_section("Waiting on triage", len(waiting), triage_rows,
                        note="One at a time, deliberately — it is the expensive route.",
                        empty="Nothing is waiting on triage."))

    out.append(f'<footer>Board on {_e(current_branch(ctx.root))} &middot; '
               f'{_e(ctx.rail.freshness_line)}</footer>')
    return "".join(out)


def _stamp_of(path: Path) -> str:
    try:
        return f"written {dt.datetime.fromtimestamp(path.stat().st_mtime):%d %b %H:%M}"
    except OSError:
        return ""


def _render_verify(ctx: Context) -> str:
    out = []
    by_surface: dict[str, list[board.Card]] = {}
    for card in ctx.testing:
        by_surface.setdefault(card.surface or "unsorted", []).append(card)

    rows = []
    for surface in sorted(by_surface):
        cards = by_surface[surface]
        rows.append(_group(f"{surface} — {len(cards)}"))
        for card in cards:
            branch = card.fields.get("branch") or f"ai/{card.id}"
            # The diff stat is only available while the branch still exists, and a
            # card reaches `testing/` by merging — after which the branch is deleted
            # by the standing cleanup rule. So it is shown when it can be and the
            # row falls back to the facts the card itself carries, rather than
            # rendering an empty meta line for every card that actually landed.
            meta = []
            if stat := diff_stat(ctx.root, ctx.base, branch):
                meta.append(_e(stat))
            elif card.worker != "none":
                meta.append(_e(card.worker))
            if card.attempts:
                meta.append(_e(f"{card.attempts} attempt(s)"))
            if card.verify == "review":
                meta.append(_chip("verify: review", "ok"))
            control = (f'<input type="checkbox" class="tick" data-id="{_e(card.id)}" '
                       f'aria-label="{_e(card.id)} verified">')
            acts = (_act("Diff", href=f"/diff/{card.id}")
                    + _act("Mark OK", onclick=f"markOK(this,'{_attr(card.id)}')", primary=True))
            rows.append(_row(control=control, marker="&nbsp;", acts=acts,
                             body=_card_body(card, meta=meta)))

    bar = ('<div class="barbox">'
           '<p><span id="ticked">Nothing ticked.</span> Saving reconciles every ticked '
           'card in one pass.</p><div class="acts">'
           + _act("Save ticked", onclick="saveTicked()", primary=True)
           + '</div></div>')
    out.append(_section("Play through", len(ctx.testing), "".join(rows),
                        note="Grouped by where in the game you would see it.",
                        sub=("Everything below is on <code>"
                             f"{_e(ctx.base)}</code> at once. <b>Mark OK</b> moves that card "
                             "to <code>done/</code> straight away; ticking several and saving "
                             "does the same in one go."),
                        bar=bar if ctx.testing else "",
                        empty="Nothing is waiting to be played."))

    stuck = []
    review_rows = []
    for card in ctx.review:
        reason = drain.skip_reason(ctx.root, ctx.base, card)
        branch = card.fields.get("branch") or f"ai/{card.id}"
        meta = [_e(stat) for stat in [diff_stat(ctx.root, ctx.base, branch)] if stat]
        if reason:
            meta.append(_chip("left alone", "mute"))
        else:
            stuck.append(card.id)
        acts = _act("Diff", href=f"/diff/{card.id}")
        if not reason:
            acts += _act("Review it", onclick=f"post('/api/review',{{card_id:'{_attr(card.id)}'}})",
                         primary=True)
        review_rows.append(_row(marker="!", acts=acts,
                                body=_card_body(card, meta=meta, why=reason)))

    flag = ""
    if stuck:
        flag = ('<div class="flag"><h3>No reviewer is scheduled for '
                f'{"this one" if len(stuck) == 1 else "these"}</h3>'
                '<p>The night takes its queue from <code>tasks/</code>, so nothing picks this '
                'lane up on its own — that is deliberate, and it is why the button is here. '
                '<b>Review it</b> runs the drain over this one card.</p></div>')
    out.append(_section("Under review", len(ctx.review), flag + "".join(review_rows),
                        note="The reviewer's lane, not yours.",
                        empty="Nothing is at rest in review/."))

    out.append(f'<footer>{len(ctx.testing)} card(s) on {_e(ctx.base)} awaiting a '
               f'play-through</footer>')
    return "".join(out)


#: Route → (group heading, chip kind). The headings are the report's own, minus
#: its second-person blurbs; the order is the order `ingest.report` lists them, so
#: the page and the file read the same way round. `""` is the note no pass has
#: reached yet, which is the only bucket the page used to have.
_ROUTE_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("", "Not yet classified", "warn"),
    ("inline", "Do now — inline, at the keyboard", "mute"),
    ("chore", "Chores — batched overnight", "ok"),
    ("scribe", "Scribe — needs the envelope only", "ok"),
    ("triage", "Waiting on triage — the expensive route", "warn"),
)


def _render_inbox(ctx: Context) -> str:
    """The inbox, grouped by what the last routing pass decided about each note.

    **The page used to state the opposite of the truth.** Every note was listed
    under "Not yet classified", because a note does not leave `inbox/` when it is
    routed — `ingest.RoutingView` has the full account. The routing was written to
    `Routing.md`, which this page linked by name and timestamp without reading, so
    a classification that had worked perfectly was indistinguishable from one that
    had never run.

    A note edited *after* the pass that routed it is marked rather than trusted:
    the routing describes text that has since changed, and the whole value of the
    view is that it says what is true of the note as it stands.
    """
    view = ctx.routing
    ordered: dict[str, list[tuple[ingest.Note, ingest.Decision | None]]] = {
        route: [] for route, _, _ in _ROUTE_GROUPS}
    for note in ctx.notes:
        decision = view.of(note.name)
        # A route this page has no group for lands with the unrouted, never in a
        # bucket nothing renders. Neither producer can currently emit one —
        # `classify` folds an unknown route to `inline` and `parse_report` only
        # assigns routes it has headings for — but a note silently missing from
        # the page is the exact class of failure this whole pass was about, and
        # "no producer can do that today" is a claim about today.
        route = decision.route if decision and decision.route in ordered else ""
        ordered[route].append((note, decision if route else None))

    rows = []
    position = 0
    for route, heading, kind in _ROUTE_GROUPS:
        bucket = ordered.get(route) or []
        if not bucket:
            continue
        rows.append(_group(f"{heading} — {len(bucket)}"))
        for note, decision in bucket:
            position += 1
            # Positional, not the filename — real note names carry spaces and capitals
            # ("Regenerate soundtrack.md"), and whitespace is not allowed in an HTML id.
            # Same reason the Ideas page numbers its editors.
            slug = f"note-{position}"
            rel = _rel(ctx.root, note.path)
            meta = [f"{note.size} B", _e(_stamp_of(note.path))]
            if decision:
                meta.insert(0, _chip(route, kind))
                if decision.confidence != "high":
                    meta.append(_chip(f"confidence {decision.confidence}", "warn"))
                if not decision.dispatchable:
                    meta.append(_chip("needs a human", "warn"))
                if _changed_since(note.path, view.written):
                    meta.append(_chip("edited since routing", "warn"))
            body = f'<span class="id">{_e(note.name)}</span>'
            if decision and decision.why:
                body += f'<p class="why">{_e(decision.why)}</p>'
            rows.append(_row(
                marker="&rsaquo;", body=body + _meta(meta),
                acts=_act("Edit", onclick=f"editBody('{slug}','{_attr(rel)}')")
                     + _act("Open note", href=f"/body/{rel}")
                     + _route_act(note.name, route)))
            rows.append(_editor(slug, save=f"saveBody('{slug}','{_attr(rel)}')"))
    rows = "".join(rows)

    # **Two steps, and they are two buttons because the ordering is the rule.**
    # `ingest`: *"It reports before it spends. Classification is one cheap
    # dispatch over the whole lane; everything after it is opt-in."* The old pair was
    # "Classify all" and "Classify + write cards" — the same first half twice, with
    # the second one unable to be opt-in about the second half and paying for a
    # fresh classify pass to re-learn what the report on disk already said. So the
    # second button now acts on the routing that exists: look, then write.
    writable = sum(1 for note in ctx.notes
                   if (d := ctx.routing.of(note.name)) and d.route in ingest.WRITABLE_ROUTES)
    write_label = f"Write the {writable} card(s)" if writable else "Write the cards"
    bar = ('<div class="barbox">'
           '<p>One cheap dispatch reads every note and sorts it, and spends nothing '
           'else. Writing the cards is the second step, on what it found.</p>'
           '<div class="acts">'
           + _act("New note", onclick="openEditor('new-note')")
           + _act(write_label, onclick="post('/api/ingest',{write:true})",
                  disabled=not writable,
                  extra='title="One scribe dispatch per chore- or scribe-routed note. '
                        'Each note becomes its card and leaves the lane; nothing is '
                        'reclassified."')
           + _act("Classify all", onclick="post('/api/ingest',{})", primary=True,
                  extra='title="One classifier dispatch over the whole lane. Writes '
                        'Routing.md and no cards — this is the look-before-you-spend '
                        'step."')
           + '</div></div>'
           + _editor("new-note", save="saveNew('new-note','inbox')", named=True,
                     placeholder="One or two sentences is enough."))
    unrouted = len(ordered.get("") or [])
    if not view.known:
        note = "No routing pass yet — Classify all is what fills this in."
    else:
        when = f"{view.written:%d %b %H:%M}" if view.written else "at an unrecorded time"
        note = (f"Routed {when}"
                + (f" · {unrouted} note(s) added since" if unrouted else "")
                + f" · {board.ROUTING_VIEW} has the full report")
    return _section("Inbox", len(ctx.notes), rows, note=note,
                    bar=bar, empty="The inbox is empty.") + (
        f'<footer>{len(ctx.notes)} note(s) in inbox</footer>')


def _route_act(note: str, route: str) -> str:
    """The actions a note's route implies, on the note's own row.

    Four routes, four different next steps, and before this the page offered the
    same two — Edit and Open — to all of them. The bar's bulk buttons could not
    stand in for it: the write button takes the whole writable set, and triage is
    explicitly the route you spend on **one** note at a time, chosen deliberately.
    So the choice belongs on the row, where the note you are looking at is the note
    the button acts on.

    **`Triage this` is on every row, whatever the route says**, including a note no
    pass has reached. The route is a recommendation from an agent that deliberately
    never opened the codebase — the classifier's own charter says *"route from the
    shape of the request, not the shape of the work"* and *"a wrong answer from you
    is affordable"* — so the human overruling it is the design working, not a
    bypass of it. It is also the one action that cannot be spent wrongly by
    accident: it opens an interactive session you are sitting in front of.

    Karel, 2026-08-17, asking for exactly this: *"change the classification if
    needed (like run triage on non triaged card for example)"*.

    The reverse override — forcing the scribe onto a triage-routed note — is
    deliberately **not** here. `ingest --only` refuses it and says why, because
    that direction is the one that spends on an agent forbidden to read the code
    and produces a confidently wrong `## Acceptance` if it guesses. Re-run the
    classify pass, or triage it.
    """
    target = _attr(note)
    acts = []
    if route in ingest.WRITABLE_ROUTES:
        acts.append(_act("Write the card",
                         onclick=f"post('/api/ingest/one',{{note:'{target}'}})",
                         primary=True,
                         extra='title="One scribe dispatch on this note, on the route the '
                               'last pass gave it. The note becomes the card and leaves the '
                               'lane; a bounce sends it to triage instead."'))
    if route == "inline":
        acts.append(_act("Start session", onclick="post('/api/session',{})",
                         extra='title="Opens a terminal in this repo. An inline note is '
                               'your own work at the keyboard — no card is dispatched '
                               'for it."'))
        acts.append(_done_act(note))
    acts.append(_act("Triage this", onclick=f"post('/api/triage',{{note:'{target}'}})",
                     primary=route == "triage",
                     extra='title="Opens a terminal running the triage charter on this '
                           'note, whatever the routing said. Interactive on purpose — '
                           'triage is investigative work you drive, and it is the '
                           'expensive route, so it is never dispatched for you."'))
    return "".join(acts)


def _done_act(note: str) -> str:
    """The gesture that closes an inline note, because nothing else can.

    Every other route's note is moved by the process that works it — the scribe
    and triage move theirs into a lane as they card it, the runner moves cards as
    it goes. `inline` means *you* are the process, so the move needs a hand, and
    without one the note sat in `inbox/` after the work was done: re-routed by the
    next classify pass, and listed by this page as still waiting.
    """
    return _act("Done", onclick=f"post('/api/close',{{note:'{_attr(note)}'}})",
                extra='title="Files this note in done/ as a minimal card — what it '
                      'asked for, and that you closed it by hand. It leaves the inbox, '
                      'so the next classify pass will not route it again."')


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


def _editor(slug: str, *, save: str, named: bool = False, placeholder: str = "") -> str:
    name_field = ('<input type="text" placeholder="filename.md">' if named else "")
    return (f'<div class="editor" id="ed-{_e(slug)}">{name_field}'
            f'<textarea placeholder="{_e(placeholder)}"></textarea>'
            f'<div class="acts">{_act("Save", onclick=save, primary=True)}'
            f'{_act("Cancel", onclick=f"closeEditor(&#39;{slug}&#39;)")}</div></div>')


def _render_ideas(ctx: Context) -> str:
    rows = []
    for position, name in enumerate(ctx.ideas, start=1):
        # The editor's id is positional, never the filename: real idea names carry
        # spaces, en dashes and diacritics ("Animation – attack.md"), none of which
        # may appear in an HTML id, and one apostrophe would end the JS string the
        # button hands it to. The *path* still travels verbatim, url-encoded.
        slug = f"idea-{position}"
        path = f"{board.board_rel(ctx.root).as_posix()}/{board.PRIVATE_LANE}/{name}"
        acts = (_act("Edit", onclick=f"editBody('{slug}','{_attr(path)}')")
                + _act("Promote", onclick=f"post('/api/promote',{{name:'{_attr(name)}'}})",
                       primary=True))
        rows.append(_row(marker=str(position), acts=acts,
                         body=f'<span class="id">{_e(name)}</span>'))
        rows.append(_editor(slug, save=f"saveBody('{slug}','{_attr(path)}')"))

    bar = ('<div class="barbox">'
           '<p>A new idea is one line and a filename. It costs nothing and commits you '
           'to nothing.</p><div class="acts">'
           + _act("New idea", onclick="openEditor('new-idea')", primary=True)
           + '</div></div>'
           + _editor("new-idea", save="saveNew('new-idea','ideas')", named=True,
                     placeholder="Half a thought is fine."))
    sub = ("Yours alone. Nothing reads these but you and this page &mdash; and promoting one "
           "<em>moves the file</em> rather than summarising it, so no idea's text reaches a "
           "card except by your hand. <b>They do not drag.</b> An idea is a bare file with "
           "no frontmatter, so there is no <code>kanban_order</code> to write and no verb "
           "that could add one without this tool authoring inside the private lane &mdash; "
           "they are listed by name instead. Cards drag, on Now.")
    return _section("Ideas", len(ctx.ideas), "".join(rows), note="Edit in place; promote "
                    "when one is ready.", sub=sub, bar=bar,
                    empty="No ideas parked.") + (
        f'<footer>{board.board_rel(ctx.root).as_posix()}/{board.PRIVATE_LANE} &middot; '
        f'committed and pushed, never read by anything else</footer>')


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


def _running_section(ctx: Context) -> str:
    """What this panel has started and has not finished — and nothing else.

    **Only the live one.** Karel, 2026-08-17: *"started from here — I don't think
    we need a full history at the top of the page ... only current run."* Right:
    the top of a page called Run is where "what is happening" belongs, and a
    fortnight of finished jobs above the night's own roster pushes the thing you
    came for below the fold. The history moved to `_job_history_section`, beside
    the earlier nights it belongs with.
    """
    live = [job for job in ctx.jobs if jobs.state(job) == jobs.RUNNING]
    rows = []
    for job in live:
        rows.append(
            f'<tr><td class="mark m-now">&middot;</td>'
            f'<td class="card">{_e(job.label)}</td>'
            f'<td class="lane">{_e(jobs.elapsed(job))}</td>'
            f'<td class="said"><code>{_e(job.command)}</code></td>'
            f'<td class="num">{_act("Output", href=f"/log/{job.ident}")}</td></tr>')
        rows.append(_job_detail(ctx, job))
    return _section("Running now", len(live),
                    f'<div class="roster"><table><tbody>{"".join(rows)}</tbody></table></div>'
                    if rows else "",
                    note="Started from this panel, still going.",
                    empty="Nothing this panel started is still running.")


def _job_detail(ctx: Context, job: jobs.Job) -> str:
    """A job's own progress, as a roster where the format is ours and a tail where
    it is not.

    Karel again, on seeing the raw tail: *"I would expect for the ingest (and other
    bulk actions) similar overview as for runs. Card xxx classified as XYZ, Card
    yyy in progress."* A log tail is what a command happens to print; a roster is
    the question answered. So a verb whose output this package owns gets parsed —
    `ingest` first, since it is the bulk action that exists — and everything else
    still gets the tail, which is honest about being raw rather than pretending to
    a structure nobody has written yet.
    """
    text = jobs.read_log(ctx.root, job.ident, tail=JOB_TAIL_BYTES)
    if not text.strip():
        return ""
    if job.label == "ingest":
        return _ingest_roster(ingest.parse_progress(text))
    lines = text.strip().splitlines()[-JOB_TAIL_LINES:]
    return (f'<tr class="joblog"><td></td><td colspan="3">'
            f'<pre>{_e(chr(10).join(lines))}</pre></td><td></td></tr>')


def _ingest_roster(progress: ingest.Progress) -> str:
    """One line per note, with what became of it.

    A classify pass has no per-note lines to show and that is not a gap: it is one
    dispatch over the whole lane, which is the entire economy of the step. So it
    reports its phase while it runs and its per-route counts when it lands, and the
    routing itself is on the Inbox page — inventing per-note progress for it would
    be reporting something nobody measured.
    """
    rows = []
    for item in progress.items:
        css, glyph = ingest.ITEM_STATES.get(item.state, ("m-wait", "&middot;"))
        said = {"done": "carded as", "bounced": "bounced to triage —",
                "stranded": "stranded —"}.get(item.state, "")
        rows.append(f'<tr><td class="mark {css}">{glyph}</td>'
                    f'<td class="card">{_e(item.name)}</td>'
                    f'<td class="lane">{_e(item.state)}</td>'
                    f'<td class="said">{_e(said)} {_e(item.detail)}</td>'
                    f'<td class="num"></td></tr>')
    if progress.routes:
        counts = " · ".join(f"{n} {route}" for route, n in progress.routes.items())
        rows.append(f'<tr><td class="mark m-ok">&check;</td>'
                    f'<td class="card">routed</td>'
                    f'<td class="lane">{progress.total} note(s)</td>'
                    f'<td class="said">{_e(counts)} — the Inbox page has each one</td>'
                    f'<td class="num"></td></tr>')
    if not rows:
        # Mid-classify: one dispatch over the lane, with nothing per-note yet.
        rows.append(f'<tr><td class="mark m-wait">&middot;</td>'
                    f'<td class="card">{_e(progress.phase or "starting")}</td>'
                    f'<td class="lane">{progress.total} note(s)</td>'
                    f'<td class="said">one dispatch over the whole lane; there is no '
                    f'per-note progress until it lands</td>'
                    f'<td class="num"></td></tr>')
    elif progress.total:
        rows.append(f'<tr class="pend"><td></td><td class="card"></td>'
                    f'<td class="lane">{progress.finished}/{progress.total}</td>'
                    f'<td class="said">{_e(progress.phase)}</td>'
                    f'<td class="num"></td></tr>')
    return "".join(rows)


def _job_history_section(ctx: Context) -> str:
    """Everything this panel started that has stopped, newest first.

    At the foot of the page with the earlier nights, because that is what it is:
    history. One line each, no inlined output — the link is enough for something
    you are looking up rather than watching.
    """
    rows = []
    shown = 0
    for job in ctx.jobs[:JOBS_ON_RUN_PAGE]:
        status = jobs.state(job)
        if status == jobs.RUNNING:
            continue
        shown += 1
        _, word = _JOB_MARK.get(status, ("", status))
        mark = {jobs.DONE: '<td class="mark m-ok">&check;</td>',
                jobs.FAILED: '<td class="mark m-bad">&times;</td>'}.get(
                    status, '<td class="mark m-wait">?</td>')
        started = job.started_at
        said = word if status != jobs.FAILED else f"{word} — exit {job.exit_code}"
        rows.append(
            f'<tr>{mark}<td class="card">{_e(job.label)}</td>'
            f'<td class="lane">{_e(f"{started:%d %b %H:%M}" if started else "")}</td>'
            f'<td class="num">{_e(jobs.elapsed(job))}</td>'
            f'<td class="said">{_e(said)} &mdash; <code>{_e(job.command)}</code></td>'
            f'<td class="num">{_act("Output", href=f"/log/{job.ident}")}</td></tr>')
    return _section("Earlier from here", shown,
                    f'<div class="roster"><table><tbody>{"".join(rows)}</tbody></table></div>'
                    if rows else "",
                    note="Every verb the buttons have spawned on this machine.",
                    empty="No button on this panel has started anything yet.")


def _render_run(ctx: Context) -> str:
    record = _latest_record(ctx.root)
    out = [_running_section(ctx)]
    dispatched = record.get("dispatched", [])

    body = []
    for entry in dispatched:
        outcome = str(entry.get("outcome", ""))
        if outcome in run_record.LANDED_OUTCOMES:
            mark = '<td class="mark m-ok">&check;</td>'
        elif outcome in run_record.FAILED_OUTCOMES:
            mark = '<td class="mark m-bad">&times;</td>'
        elif outcome in run_record.DECISION_OUTCOMES:
            mark = '<td class="mark m-now">?</td>'
        else:
            mark = '<td class="mark m-wait">&middot;</td>'
        cost = entry.get("cost_usd") or 0
        out_dir = attempt_dir(ctx.root, str(entry.get("card", "")),
                              int(entry.get("attempt") or 1))
        telemetry = read_telemetry(out_dir)
        session = session_id(out_dir)
        talk = (_act("Talk", onclick=f"post('/api/talk',{{session_id:'{_attr(session)}'}})")
                if session else "")
        took = f"{telemetry['wall_s'] / 60:.0f} min" if telemetry.get("wall_s") else ""
        body.append(
            f'<tr>{mark}<td class="card">{_e(entry.get("card", ""))}</td>'
            f'<td class="lane">{_e(_landed_lane(entry))}</td>'
            f'<td class="num">{_e(took)}</td>'
            f'<td class="num">${cost:.2f}</td>'
            f'<td class="said">{_e(_said(entry))}</td>'
            f'<td class="num">{talk}</td></tr>'
        )
    live = run_is_live(ctx.rail.run_status, record)
    if live:
        for candidate in ctx.tonight:
            if any(d.get("card") == candidate.card.id for d in dispatched):
                continue
            body.append(f'<tr class="pend"><td class="mark m-wait">&middot;</td>'
                        f'<td class="card">{_e(candidate.card.id)}</td>'
                        f'<td class="lane">queued</td><td class="num"></td>'
                        f'<td class="num"></td><td class="said"></td>'
                        f'<td class="num"></td></tr>')

    # "This run" is a claim about *now*, and the newest record can be weeks old —
    # on first inspection this page presented a run from two weeks earlier under
    # that heading, with a start time and no date, which reads as this morning.
    started = str(record.get("started", ""))
    today = started[:10] == dt.date.today().isoformat()
    heading = "This run" if live else ("Today's run" if today else "Last run")
    when = f"{started[:10]} {started[11:16]}" if started else ""
    note = (f"{when} on {record.get('host', '?')} · "
            f"{'in flight' if live else ('complete' if record.get('complete') else 'ended without finishing')}"
            ) if started else ""
    bar = ('<div class="barbox">'
           f'<p>Spent this run: <b>${record.get("cost_usd", 0) or 0:.2f}</b>. '
           'Stopping lets the current card finish and merges nothing after it.</p>'
           '<div class="acts">'
           + _act("Stop after this card", onclick="post('/api/stop',{})")
           + '</div></div>') if live else ""
    out.append(_section(heading, len(dispatched),
                        f'<div class="roster"><table><tbody>{"".join(body)}</tbody></table></div>'
                        if body else "", note=note, bar=bar,
                        sub="What the morning digest would have told you, except now.",
                        empty="No run has been recorded on this machine yet."))

    # Read off the board as it stands, **not** off the record's `skipped` list.
    # That list belongs to whichever run wrote it, and the newest run here was two
    # weeks old — so the section confidently listed cards that had since been
    # finished and moved to `done/`. A card the night will not take is a fact
    # about `tasks/` right now, and derived this way it cannot name a done card,
    # because a done card is no longer in the lane.
    left_out = ctx.do_now + ctx.elsewhere
    rows = "".join(
        _row(body=_card_body(c.card, meta=[_e(c.reason.split(";")[0][:80])]),
             acts=_act("Read card", href=f"/card/{c.card.id}"))
        for c in left_out
    )
    out.append(_section("Not taken", len(left_out), rows,
                        note="As the board stands now — never silent, a card left "
                             "out says why.",
                        empty="Every card in tasks/ is dispatchable here."))

    earlier = run_record.read_all(ctx.root)[1:6]
    rows = []
    for old in earlier:
        mark = ('<td class="mark m-ok">&check;</td>' if old.get("complete")
                else '<td class="mark m-bad">&times;</td>')
        when = str(old.get("started", ""))[:16].replace("T", " ")
        took = span(str(old.get("started", "")), str(old.get("finished") or ""))
        cost = old.get("cost_usd", 0) or 0
        said = old.get("stop_reason") or f"{len(old.get('dispatched', []))} dispatched"
        rows.append(
            f'<tr>{mark}<td class="card">{_e(when)} &mdash; {_e(old.get("kind", ""))}</td>'
            f'<td class="num">{_e(took)}</td><td class="num">${cost:.2f}</td>'
            f'<td class="said">{_e(said)}</td></tr>'
        )
    out.append(_section("Earlier runs", len(earlier),
                        f'<div class="roster"><table><tbody>{"".join(rows)}</tbody></table></div>'
                        if rows else "", empty="No earlier runs on this machine."))

    out.append(_job_history_section(ctx))

    out.append(f'<footer>Records in {run_record.DIR.as_posix()} &middot; transcripts stay '
               f'on the machine that produced them</footer>')
    return "".join(out)


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
    return found.changes + len(found.by(update.CONFLICT))


def _system_setup(ctx: Context) -> str:
    """Install if there is none; otherwise what the install was and how to update it."""
    if not installed(ctx.root):
        body = (
            "<b>nightshift is not installed in this repo.</b>"
            "<p class='note'>The button opens an interactive Claude session running "
            "the install skill. It asks you two things that are never guessed — the "
            "branch work merges into, and what a dispatched worker may do on this "
            "machine — then writes the manifest, the board, the gates and the hooks.</p>")
        acts = _act("Set up nightshift", onclick="post('/api/setup')", primary=True)
        return _section("Setup", 1, _row(marker="+", body=body, acts=acts),
                        note="This page works before the install. That is the point.")

    receipt = init.read_receipt(ctx.root) or {}
    created = init.receipt_created(receipt)
    rows = _row(marker="=", body=(
        f"<b>Installed</b><p class='note'>{len(created)} file(s) written by nightshift, "
        f"recorded in {_e(init.RECEIPT)} — that record is what lets an update tell your "
        f"edits from ours.</p>"))
    return _section("Setup", 0, rows, note="What this install put in the repo.")


def _system_files(ctx: Context) -> str:
    """The update survey: what moved, what you changed, what needs deciding."""
    if not installed(ctx.root):
        return ""
    try:
        found = update.survey(ctx.root)
    except update.UpdateError as exc:
        return _section("Project files", 0, "", empty=str(exc))

    rows = []
    for finding in found.by(update.STALE, update.MISSING):
        label = ("the template moved; you never edited this" if finding.verdict
                 == update.STALE else "ours, and missing from disk")
        rows.append(_row(marker="+", body=(
            f"<b>{_e(finding.rel)}</b><p class='note'>{label}</p>")))
    for finding in found.by(update.CONFLICT):
        # Four actions, one per `update` verb. The panel owns no resolution logic:
        # every button below POSTs to a flag that already works from a terminal.
        acts = "".join([
            _act("Diff", href=f"/update-diff?path={finding.rel}"),
            _act("Take theirs", onclick=f"post('/api/update/take',"
                                        f"{{path:'{_attr(finding.rel)}'}})"),
            _act("Keep mine", onclick=f"post('/api/update/keep',"
                                      f"{{path:'{_attr(finding.rel)}'}})"),
            _act("Merge", onclick=f"post('/api/update/merge',"
                                  f"{{path:'{_attr(finding.rel)}'}})"),
        ])
        rows.append(_row(marker="!", body=(
            f"<b>{_e(finding.rel)}</b><p class='note'>you edited this and the template "
            f"moved — nothing is overwritten until you say which wins</p>"), acts=acts))

    bar = ""
    if found.changes:
        bar = (f'<div class="barbox"><p>{found.changes} safe change(s) — conflicts are '
               f'never included</p><div class="acts">'
               f'{_act("Update these", onclick="post(&#39;/api/update/apply&#39;)", primary=True)}'
               f'</div></div>')
    note = (f"{len(found.by(update.CURRENT))} current, "
            f"{len(found.by(update.YOURS))} edited by you, "
            f"{len(found.by(update.DECLINED))} declined, "
            f"{len(found.by(update.FROZEN_V))} frozen")
    return _section("Project files", found.changes + len(found.by(update.CONFLICT)),
                    "".join(rows), note=note, bar=bar,
                    empty="Every file nightshift owns here matches its template.")


#: The read-only and repair verbs, as one table rather than five near-identical
#: blocks. `(id, label, button, blurb, dispatches)` — `dispatches` is what decides
#: whether the account veto applies, because it is what decides whether it spends.
SYSTEM_VERBS = (
    ("doctor", "Health", "Run doctor",
     "Per-machine preconditions and drift between the manifest and the tree. "
     "Reports; changes nothing.", False),
    ("gates", "Gates", "Run gates",
     "The gate suite, exactly as the save hook and preflight run it.", False),
    ("preflight", "Preflight", "Run preflight",
     "Gates, the audit matrix, the corrections check and the test slice this branch "
     "can affect. Required before a push, and it writes the receipt that unblocks one.",
     False),
    ("fix", "Repair", "Dispatch fix",
     "Runs every check, then dispatches an agent to repair what failed — up to three "
     "rounds. It never weakens a check to make it pass, and never commits.", True),
)


def _system_verbs(ctx: Context) -> str:
    if not installed(ctx.root):
        return ""
    rows = []
    for ident, label, button, blurb, dispatches in SYSTEM_VERBS:
        note = blurb + (" Spends on the account in force." if dispatches else "")
        rows.append(_row(marker="&middot;", body=f"<b>{_e(label)}</b>"
                                                 f"<p class='note'>{_e(note)}</p>",
                         acts=_act(button, onclick=f"post('/api/system/{ident}')")))
    return _section("Checks and repair", 0, "".join(rows),
                    note="Each one is the command you would have typed.")


def _system_danger(ctx: Context) -> str:
    """Uninstall. Shown last, and armed only by typing the project's own name.

    A dry run is always what the button produces first — `uninstall` is dry-run by
    default and that default is not overridden here. The typed confirmation is for the
    second step, because this is the one control on the page that removes work, and
    because it takes the launcher and the manifest with it: the panel serving this page
    stops answering immediately afterwards, which is confusing rather than dangerous
    but is worth being told before rather than after.
    """
    if not installed(ctx.root):
        return ""
    name = ctx.root.name
    body = ("<b>Uninstall nightshift from this repo</b>"
            "<p class='note'>Removes what the install wrote, strips our hook entries "
            "out of settings.json and leaves your own files — including the corrections "
            "log, if it has anything in it. It also deletes the launcher and the "
            "manifest, so this page stops working the moment it succeeds.</p>")
    acts = "".join([
        _act("Show what it would remove", onclick="post('/api/system/uninstall')"),
        _act("Uninstall", onclick=f"confirmUninstall('{_attr(name)}')"),
    ])
    return _section("Danger", 0, _row(marker="!", body=body, acts=acts),
                    note="Nothing here runs without a second, typed confirmation.")


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


def _render_system(ctx: Context) -> str:
    return "".join([
        _system_setup(ctx),
        _system_files(ctx),
        _system_verbs(ctx),
        _system_danger(ctx),
    ])


_RENDER = {"now": _render_now, "verify": _render_verify, "inbox": _render_inbox,
           "ideas": _render_ideas, "run": _render_run, "system": _render_system}


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
    out = out.replace("{{STATUSRAIL}}", _statusrail_html(ctx))
    return out.replace("{{CONTENT}}", content)


def render_page(page: str, root: Path) -> str:
    ctx = read_context(root)
    return render_shell(ctx, title=page.capitalize(), active=page,
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
    # **The roster belongs here too, not only on a live run.** The Run page leads
    # with what is running, by request — which would otherwise mean that the moment
    # a fan-out finishes, the structured account of it is replaced by a raw log. So
    # the job's own page carries both: what became of each item, and the output it
    # was read from.
    roster = ""
    if job.label == "ingest":
        rows = _ingest_roster(ingest.parse_progress(text))
        if rows:
            roster = (f'<div class="roster"><table><tbody>{rows}'
                      f'</tbody></table></div>')
    return render_document(
        root, title=f"{job.label} · output", subtitle=job.started.replace("T", " "),
        body=f'{_meta(facts)}{live}{roster}<pre>{_e(text)}</pre>',
        acts=_act("Reload", href=f"/log/{job.ident}"))


def render_document(root: Path, *, title: str, subtitle: str, body: str,
                    acts: str = "") -> str:
    """A card or a diff, framed. `body` is already-safe HTML."""
    ctx = read_context(root)
    head = (f'<div class="sec-head"><h2>{_e(title)}</h2>'
            f'<p class="note">{_e(subtitle)}</p></div>')
    content = (f'<section>{head}<div class="doc">{body}</div>'
               f'<div class="barbox"><p></p><div class="acts">{acts}'
               f'{_act("Back", href="/now")}</div></div></section>')
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
            # An uninstalled repo lands on the page that can do something about it.
            # `/now` in a repo with no board is five empty sections and no hint that
            # the install never happened — which is exactly the state a first-time
            # visitor arrives in, having opened the launcher `bootstrap` just wrote.
            self.send_response(302)
            self.send_header("Location", "/now" if installed(self.root) else "/system")
            self.end_headers()
            return
        if path in _RENDER:
            self._send(200, render_page(path, self.root).encode("utf-8"))
            return
        if path == "api/body":
            wanted = parse_qs(parsed.query).get("path", [""])[0]
            try:
                self._send_json(200, {"body": read_body(self.root, wanted)})
            except PanelError as exc:
                self._send_json(400, {"ok": False, "message": str(exc)})
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
            target = path[len("body/"):]
            try:
                text = read_body(self.root, target)
            except PanelError as exc:
                self._send_text(400, str(exc))
                return
            self._send(200, render_document(
                self.root, title=Path(target).name, subtitle=target,
                body=markdown(text)).encode("utf-8"))
            return
        if path.startswith("log/"):
            self._send(200, render_job(self.root, path[len("log/"):]).encode("utf-8"))
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
            if ids:
                pid = spawn_sequence(ids, root)
                return f"running {len(ids)} card(s) in order (pid {pid}): {', '.join(ids)}"
            return f"tonight's run started (pid {spawn_background('runner', [], root)})"

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

        if path == "api/promote":
            return _verb(run_command("boardcmd", ["promote", str(body.get("name", ""))], root))

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

        if path == "api/reconcile":
            return _verb(run_command("reconcile", ["--apply", "--commit"], root))

        if path == "api/freshness/refresh":
            return freshness.describe(freshness.read(fetch=True))

        if path == "api/freshness/pull":
            return _verb(run_command("freshness", ["--pull"], root))

        if path == "api/account":
            state = select_account(root, str(body.get("label", "")))
            return f"account: {state.label or '(ambient)'}"

        if path == "api/switch-account":
            # A browser sign-in against the one config directory. The panel is a
            # launcher: it opens the terminal and stops there. The cached meters go
            # with the old account, so they are dropped rather than left to be
            # served under the new one's name.
            global _METERS
            _METERS = None
            open_terminal(root, "claude", "auth", "login")
            return ("opened a terminal running `claude auth login` — sign in, then "
                    "reload this page to see which account is in force")

        if path == "api/stop":
            # The kill switch the runner already watches for (`runner.STOP_FILE`),
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
            open_terminal(root, "claude")
            return "opened a terminal in this repo"

        if path == "api/talk":
            session = str(body.get("session_id", ""))
            if not session:
                raise PanelError("no session_id given")
            open_terminal(root, "claude", "--resume", session)
            return f"opened a terminal resuming {session}"

        if path == "api/triage":
            # **With the note named, when one is given.** The button used to open
            # `claude --agent triage` and stop there, leaving you to remember which
            # of five notes you had meant and type it — for the one route whose
            # whole discipline is "one note at a time, deliberately". The charter
            # takes exactly one note per call, so the launcher may as well say
            # which. Interactive on purpose (§3.4): triage is investigative work a
            # person drives, never a `-p` dispatch.
            note = str(body.get("note", ""))
            command = ["claude", "--agent", "triage"]
            if note:
                lane = board.board_rel(root)
                command.append(f"Triage `{lane}/inbox/{note}`. Follow your charter.")
            open_terminal(root, *command)
            return (f"opened a terminal running triage on {note}" if note else
                    "opened a terminal running the triage charter")

        return None


def read_body(root: Path, target: str) -> str:
    """One board file's text, for the person editing it in their own browser.

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
    return path.read_text(encoding="utf-8")


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
        with urlopen(f"http://127.0.0.1:{port}/now", timeout=2) as answer:
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
        url = f"http://127.0.0.1:{port}/now"
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
    url = f"http://127.0.0.1:{port}/now"
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
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    root = (args.root or find_root()).resolve()
    if args.dispatch_cards:
        return dispatch_cards(list(args.dispatch_cards), root)
    serve(root, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
