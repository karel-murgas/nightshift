#!/usr/bin/env python3
"""The Command Center — a launcher, a registry and a tail, never a chat client.

`.claude/plans/dispatch-cost-and-control-panel.md` (a Dungeoneer doc) §3.4 is the design
session this implements; the plan itself is project-side because it is Dungeoneer's own
programme, but the panel is framework — any project with a board can run one.

**Every verb already exists as a CLI command** (items 11-13 of that plan built them for
exactly this reason), so **the server owns no logic**. Every POST that changes anything goes
through `run_command`/`spawn_background`, which shell out to `python -m nightshift.<module>
<args>` — the same command a person would type. This mirrors the framework's own precedent:
`boardcmd`'s test suite states outright that "the panel will not import this module — it will
run it" (`tests/test_boardcmd.py`), and this module holds to that for every board- or
dispatch-shaped verb. Reading board/usage/freshness state to *render* a page is not "logic" in
that sense — `drain.py` and `ingest.py` already import `board`/`usage` directly for the same
reason — so GET handlers read via the ordinary Python API and only POST handlers shell out.

**Account selection lives here, in memory, and nowhere else.** `_ACCOUNT` is process-wide and
reset on restart — deliberately not persisted, the same shape as `usage`'s `--allow-paid`
(`feedback_account_dispatch`): a "spend on this account" decision that outlived the click that
made it is the foot-gun the whole rule exists to prevent. Selecting an account sets
`CLAUDE_CONFIG_DIR` for this process's own dispatch subprocesses only — `runner._worker_env`
already inherits the environment wholesale, confirmed in item 13, so nothing downstream needs
to know a selector exists. An account carrying `dispatch: never` is refused server-side
(`_guard_dispatch_account`), not merely hidden in the UI: a crafted request must fail exactly
where a browser click would have declined to send one.

**Framework freshness fetches only on an explicit Refresh, never on page load** (§3.4's own
rule) — `read_rail` always calls `freshness.read(fetch=False)`; only `/api/freshness/refresh`
passes `fetch=True`.

No LLM anywhere in this module. Reading board/usage state is arithmetic and file I/O; every
LLM-touching action is a subprocess this module starts and does not wait on.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from nightshift import board, drain, freshness, ingest, manifest, usage
from nightshift.manifest import ManifestError, find_root
from nightshift.runner import (
    STATUS_FILE,
    Candidate,
    claude_binary,
    default_base,
    host_capabilities,
    schema_violations,
)
from nightshift.runner import select as select_candidates

DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).resolve().parent / "panel_static"
TEMPLATE = STATIC_DIR / "app.html"

PAGES = ("now", "verify", "inbox", "ideas", "run")

# --------------------------------------------------------------------------
# The account in force — process-wide, in-memory, never persisted (see module
# docstring). A fresh server always starts on the ambient account.
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
    if not label:
        _ACCOUNT = AccountState()
        return _ACCOUNT
    match = next((a for a in _accounts(root) if a.label == label), None)
    if match is None:
        known = ", ".join(a.label for a in _accounts(root)) or "(none configured)"
        raise PanelError(f"no account named {label!r} in [[accounts]] — known: {known}")
    _ACCOUNT = AccountState(label=match.label, config_dir=match.config_dir, dispatch=match.dispatch)
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


def _guard_dispatch_account() -> None:
    """Refuse a dispatch on an account marked `dispatch: never`, or one whose
    live `hasExtraUsageEnabled` says API spend is on — whichever fires first.

    Checked here, not only left to the UI hiding the button — a crafted POST must
    fail exactly where a real click would have declined to send one at all.

    **Config may only ever exclude, never promote** (`feedback_account_dispatch`):
    `[[accounts]]` may be missing an entry for the account actually in force — it
    was, on the very first live run of this module, against Karel's own board —
    so the config check alone would have let automated work reach the one account
    the whole rule exists to protect. The live identity read is what `usage.py`'s
    own docstring calls the veto config forgot to mark, so it is checked
    unconditionally here, not only when `[[accounts]]` names the account.
    """
    if _ACCOUNT.dispatch == "never":
        raise PanelError(
            f"the account {_ACCOUNT.label!r} is configured `dispatch: never` in "
            f"[[accounts]] — this panel will not dispatch work to it. Select a "
            f"different account first."
        )
    _, identity_path = _account_paths(_ACCOUNT)
    identity = usage.read_identity(identity_path)
    if identity.fetched and identity.has_extra_usage_enabled:
        raise PanelError(
            f"the account in force ({identity.email or '(no email on file)'}) has API "
            f"spend enabled (`hasExtraUsageEnabled`) — this panel will not dispatch "
            f"automated work to it regardless of [[accounts]]. Select a different "
            f"account, or mark this one `dispatch: never` and do the work inline."
        )


# --------------------------------------------------------------------------
# The one command-running helper. Every write/dispatch verb goes through one
# of these two functions — never a re-implementation of board or runner logic.
# --------------------------------------------------------------------------


def _module_argv(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", f"nightshift.{module}", *args]


def run_command(module: str, args: list[str], root: Path, *, timeout: int = 120) -> subprocess.CompletedProcess:
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

    For a verb that may dispatch an actual LLM session and run for minutes — a
    card, a night, a chore batch, a classify pass, a review. The HTTP request
    returns immediately; progress is read from the files the command itself
    already writes (`.ai/runs/status.json`, `Board/Chores.md`, `Board/Routing.md`),
    never from this function holding the connection open.
    """
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        _module_argv(module, *args), cwd=root, env=_dispatch_env(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **kwargs,
    )
    return proc.pid


def open_terminal(root: Path, *command: str) -> None:
    """Hand `command` to a new, visible console window rather than running it here.

    This is the "talk to this one" / "launch triage" gesture (§3.4: *"it is a
    launcher, a registry and a tail — never a chat client"*). `claude --resume
    <session_id>` is the concrete case `runner.py` already supports (the session id
    is captured per attempt); triage is `claude --agent triage` — deliberately
    interactive, because triage is real investigative work Karel drives, not a
    one-shot `-p` dispatch this module could run for him.
    """
    if os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "Command Center", "cmd", "/k", *command],
                         cwd=root)  # gate-ok(subprocess_result_checked): a detached, visible
                                    # console window that Karel drives from here on — there is
                                    # nothing this function could do with an exit code from a
                                    # window that has not been interacted with yet
    else:
        subprocess.Popen(["x-terminal-emulator", "-e", " ".join(command)], cwd=root,
                         shell=False)  # gate-ok(subprocess_result_checked): same reason as the
                                       # Windows branch just above


# --------------------------------------------------------------------------
# Page data — reads only. `board.cards`/`usage.read`/`freshness.read` etc are
# the framework's own read API; no second parser is grown here.
# --------------------------------------------------------------------------


@dataclass
class Rail:
    identity: usage.Identity
    snapshot: usage.Snapshot
    verdict: usage.Verdict
    freshness_line: str
    account_label: str
    account_dispatch: str
    accounts: tuple[manifest.Account, ...]
    run_status: dict = field(default_factory=dict)


def read_rail(root: Path, *, fetch_freshness: bool = False) -> Rail:
    credentials, identity_path = _account_paths(_ACCOUNT)
    identity = usage.read_identity(identity_path)
    snapshot = usage.read(credentials)
    verdict = usage.check(snapshot)
    fresh = freshness.read(fetch=fetch_freshness)

    status_path = root / STATUS_FILE
    run_status: dict = {}
    if status_path.is_file():
        try:
            run_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            run_status = {}

    return Rail(
        identity=identity, snapshot=snapshot, verdict=verdict,
        freshness_line=freshness.describe(fresh),
        account_label=_ACCOUNT.label, account_dispatch=_ACCOUNT.dispatch,
        accounts=_accounts(root), run_status=run_status,
    )


@dataclass
class NowPage:
    decisions: list[board.Card]
    tonight: list[Candidate]
    elsewhere: list[Candidate]
    do_now: list[Candidate]
    routing_view_exists: bool
    routing_view_mtime: dt.datetime | None


def read_now(root: Path) -> NowPage:
    decisions = board.cards(root, "needs-decision")
    capabilities = host_capabilities(root)
    bad_schema = schema_violations(root)
    candidates = select_candidates(root, capabilities, bad_schema)

    tonight = [c for c in candidates if c.dispatchable]
    elsewhere = [c for c in candidates
                 if not c.dispatchable and c.card.requires and c.card.requires not in capabilities]
    do_now = [c for c in candidates if not c.dispatchable and c not in elsewhere]

    routing_path = root / board.ROUTING_VIEW
    exists = routing_path.is_file()
    mtime = dt.datetime.fromtimestamp(routing_path.stat().st_mtime) if exists else None
    return NowPage(decisions=decisions, tonight=tonight, elsewhere=elsewhere, do_now=do_now,
                  routing_view_exists=exists, routing_view_mtime=mtime)


@dataclass
class VerifyPage:
    by_surface: dict[str, list[board.Card]]
    review_cards: list[board.Card]
    review_skip_reason: dict[str, str]


def read_verify(root: Path) -> VerifyPage:
    testing_cards = board.cards(root, "testing")
    by_surface: dict[str, list[board.Card]] = {}
    for card in testing_cards:
        by_surface.setdefault(card.surface or "unsorted", []).append(card)

    base = default_base(root)
    review_cards = drain.waiting(root)
    skip = {c.id: drain.skip_reason(root, base, c) for c in review_cards}
    return VerifyPage(by_surface=by_surface, review_cards=review_cards, review_skip_reason=skip)


@dataclass
class InboxPage:
    notes: list[ingest.Note]
    routing_view_exists: bool
    routing_view_mtime: dt.datetime | None


def read_inbox(root: Path) -> InboxPage:
    found = ingest.notes(root)
    routing_path = root / board.ROUTING_VIEW
    exists = routing_path.is_file()
    mtime = dt.datetime.fromtimestamp(routing_path.stat().st_mtime) if exists else None
    return InboxPage(notes=found, routing_view_exists=exists, routing_view_mtime=mtime)


def read_ideas(root: Path) -> list[str]:
    """Filenames only, in the private lane — never a body. See the module
    docstring's account-selection note and `nightshift.hooks.ideas_fence`: this
    reads names from the filesystem, the same act `promote` already performs, and
    never opens a file inside `ideas/`."""
    lane = board.board_dir(root) / board.PRIVATE_LANE
    if not lane.is_dir():
        return []
    return sorted(p.name for p in lane.glob("*.md"))


def list_claude_agents(root: Path) -> list[dict]:
    """`claude agents --json --all --cwd <root>` (§5) — the cheap job-supervisor
    reading. No wrapper exists in the package for this (it is the `claude` CLI's
    own subcommand, not a nightshift one), so this is the one place panel.py talks
    to a binary outside the package, via the same resolver the package itself uses.
    """
    binary = claude_binary()
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "agents", "--json", "--all", "--cwd", str(root)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


@dataclass
class RunPage:
    status: dict
    candidates: list[Candidate]
    agents: list[dict]


def read_run(root: Path) -> RunPage:
    capabilities = host_capabilities(root)
    bad_schema = schema_violations(root)
    candidates = select_candidates(root, capabilities, bad_schema)
    status_path = root / STATUS_FILE
    status: dict = {}
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            status = {}
    return RunPage(status=status, candidates=candidates, agents=list_claude_agents(root))


# --------------------------------------------------------------------------
# Rendering — one HTML file, slots filled with html-escaped text. No template
# dependency: the placeholders are literal `{{name}}` tokens, replaced once.
# --------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _nav(active: str) -> str:
    links = "".join(
        f'<a href="/{p}" class="{"active" if p == active else ""}">{p.capitalize()}</a>'
        for p in PAGES
    )
    return f'<nav class="topnav">{links}</nav>'


def _rail_html(rail: Rail, root: Path) -> str:
    account = rail.account_label or "(ambient — default credential)"
    dispatch_note = ""
    if rail.account_label and rail.account_dispatch == "never":
        dispatch_note = ' <span class="warn">dispatch: never — inline only</span>'
    identity_line = (
        f"{_e(rail.identity.email)}" if rail.identity.fetched and rail.identity.email
        else "(identity unavailable)"
    )
    spend_note = ""
    if rail.identity.fetched and rail.identity.has_extra_usage_enabled:
        spend_note = ' <span class="warn">API spend ENABLED on this account</span>'

    meters = ""
    if rail.snapshot.fetched:
        worst = rail.snapshot.worst
        if worst is not None:
            cls = "danger" if worst.exhausted else ("warn" if worst.headroom_pct < 15 else "data")
            meters = f'<span class="{cls}">{_e(worst.name)} {worst.utilization:.0f}%</span>'
        if rail.snapshot.paid_enabled:
            meters += ' <span class="warn">paid overage ENABLED</span>'
    else:
        meters = f'<span class="dim">usage unknown ({_e(rail.snapshot.reason)})</span>'

    status = rail.run_status
    if status.get("card"):
        tally = f'running: <span class="data">{_e(status.get("card"))}</span> ({_e(status.get("phase", "?"))})'
    else:
        tally = '<span class="dim">no run in progress</span>'

    selector = ""
    if rail.accounts:
        options = ['<option value="">(ambient)</option>']
        for a in rail.accounts:
            selected = " selected" if a.label == rail.account_label else ""
            options.append(f'<option value="{_e(a.label)}"{selected}>{_e(a.label)}</option>')
        selector = f'<select id="account-select" onchange="selectAccount(this.value)">{"".join(options)}</select>'

    return (
        '<div class="rail">'
        f'<div class="rail-row">{tally}</div>'
        f'<div class="rail-row">account: <span class="data">{_e(account)}</span>{dispatch_note} '
        f'&middot; {identity_line}{spend_note} {selector}</div>'
        f'<div class="rail-row">{meters}</div>'
        f'<div class="rail-row">{_e(rail.freshness_line)} '
        f'<button onclick="post(\'/api/freshness/refresh\',{{}})">Refresh</button> '
        f'<button onclick="post(\'/api/freshness/pull\',{{}})">Pull</button></div>'
        '</div>'
    )


def _candidate_row(candidate: Candidate, *, action: str) -> str:
    card = candidate.card
    btn = ""
    if action == "dispatch":
        btn = f'<button onclick="post(\'/api/dispatch\',{{card_id:\'{_e(card.id)}\'}})">Dispatch</button>'
    return (
        f'<li><span class="data">{_e(card.id)}</span> — {_e(card.title)} '
        f'<span class="dim">({_e(candidate.reason)})</span> {btn}</li>'
    )


def _render_now(root: Path) -> str:
    page = read_now(root)
    out = ['<h1>Now</h1>']

    out.append("<h2>Decide</h2><ul>")
    if not page.decisions:
        out.append("<li class='dim'>nothing waiting on a decision</li>")
    for card in page.decisions:
        out.append(f"<li><span class='data'>{_e(card.id)}</span> — {_e(card.title)}</li>")
    out.append("</ul>")

    out.append("<h2>Do now</h2><ul>")
    if not page.do_now:
        out.append("<li class='dim'>nothing needs you at the keyboard</li>")
    for c in page.do_now:
        out.append(_candidate_row(c, action="none"))
    out.append("</ul>")

    out.append("<h2>Tonight</h2><ul>")
    if not page.tonight:
        out.append("<li class='dim'>nothing dispatchable right now</li>")
    for c in page.tonight:
        out.append(_candidate_row(c, action="dispatch"))
    out.append('</ul><button onclick="post(\'/api/night\',{})">Start tonight\'s run</button>')

    if page.elsewhere:
        out.append("<h2>Tonight, on the other machine</h2><ul>")
        for c in page.elsewhere:
            out.append(_candidate_row(c, action="none"))
        out.append("</ul>")

    out.append("<h2>Waiting on triage</h2>")
    if page.routing_view_exists:
        out.append(
            f"<p>see <code>Routing.md</code> (updated {page.routing_view_mtime:%Y-%m-%d %H:%M}) "
            f"— <button onclick=\"openTerminal('triage')\">Launch triage</button></p>"
        )
    else:
        out.append("<p class='dim'>no routing view yet — run classify the inbox first</p>")

    return "\n".join(out)


def _render_verify(root: Path) -> str:
    page = read_verify(root)
    out = ["<h1>Verify</h1>", "<h2>Testing, by surface</h2>"]
    if not page.by_surface:
        out.append("<p class='dim'>nothing waiting to be played</p>")
    for surface, cards in sorted(page.by_surface.items()):
        out.append(f"<h3>{_e(surface)}</h3><ul>")
        for card in cards:
            out.append(
                f"<li><span class='data'>{_e(card.id)}</span> — {_e(card.title)} "
                f"<button onclick=\"post('/api/verified',{{card_id:'{_e(card.id)}'}})\">Mark OK</button></li>"
            )
        out.append("</ul>")

    out.append("<h2>Stuck in review</h2><ul>")
    if not page.review_cards:
        out.append("<li class='dim'>nothing at rest in review/</li>")
    for card in page.review_cards:
        reason = page.review_skip_reason.get(card.id, "")
        btn = (f'<button onclick="post(\'/api/review\',{{card_id:\'{_e(card.id)}\'}})">Review</button>'
               if not reason else f'<span class="dim">{_e(reason)}</span>')
        out.append(f"<li><span class='data'>{_e(card.id)}</span> — {_e(card.title)} {btn}</li>")
    out.append("</ul>")
    return "\n".join(out)


def _render_inbox(root: Path) -> str:
    page = read_inbox(root)
    out = ["<h1>Inbox</h1>", "<h2>Notes</h2><ul>"]
    if not page.notes:
        out.append("<li class='dim'>inbox is empty</li>")
    for note in page.notes:
        out.append(f"<li><span class='data'>{_e(note.name)}</span> ({note.size} B)</li>")
    out.append("</ul>")
    out.append(
        '<button onclick="post(\'/api/ingest\',{scribe:false})">Classify the inbox</button> '
        '<button onclick="post(\'/api/ingest\',{scribe:true})">Classify + write cards</button>'
    )
    if page.routing_view_exists:
        out.append(f"<p class='dim'>Routing.md last updated {page.routing_view_mtime:%Y-%m-%d %H:%M}</p>")
    out.append(
        "<h2>New note</h2>"
        '<input id="note-name" placeholder="filename.md"><br>'
        '<textarea id="note-body" rows="4" cols="60"></textarea><br>'
        '<button onclick="createNote()">Save</button>'
    )
    return "\n".join(out)


def _render_ideas(root: Path) -> str:
    names = read_ideas(root)
    out = ["<h1>Ideas</h1><ul>"]
    if not names:
        out.append("<li class='dim'>no ideas parked</li>")
    for name in names:
        out.append(
            f"<li><span class='data'>{_e(name)}</span> "
            f'<button onclick="post(\'/api/promote\',{{name:\'{_e(name)}\'}})">Promote</button></li>'
        )
    out.append("</ul>")
    out.append(
        "<h2>New idea</h2>"
        '<input id="idea-name" placeholder="filename.md"><br>'
        '<textarea id="idea-body" rows="4" cols="60"></textarea><br>'
        '<button onclick="createIdea()">Save</button>'
    )
    return "\n".join(out)


def _render_run(root: Path) -> str:
    page = read_run(root)
    out = ["<h1>Run</h1>"]
    status = page.status
    if status.get("card"):
        out.append(
            f"<p>phase <span class='data'>{_e(status.get('phase', '?'))}</span> "
            f"on <span class='data'>{_e(status.get('card'))}</span>, "
            f"attempt {_e(status.get('attempt', '?'))}, "
            f"model {_e(status.get('model', '?'))}</p>"
        )
    else:
        out.append("<p class='dim'>no run in progress</p>")

    out.append("<h2>Queued</h2><ul>")
    dispatchable = [c for c in page.candidates if c.dispatchable]
    if not dispatchable:
        out.append("<li class='dim'>nothing queued</li>")
    for c in dispatchable:
        out.append(f"<li><span class='data'>{_e(c.card.id)}</span> — {_e(c.card.title)}</li>")
    out.append("</ul>")

    out.append("<h2>Sessions</h2><ul>")
    if not page.agents:
        out.append("<li class='dim'>no active or recent sessions</li>")
    for agent in page.agents:
        session_id = agent.get("session_id") or agent.get("id") or ""
        label = agent.get("label") or agent.get("name") or session_id
        out.append(
            f"<li>{_e(label)} "
            f"<button onclick=\"post('/api/talk',{{session_id:'{_e(session_id)}'}})\">Talk to this one</button></li>"
        )
    out.append("</ul>")
    return "\n".join(out)


_RENDER = {
    "now": _render_now,
    "verify": _render_verify,
    "inbox": _render_inbox,
    "ideas": _render_ideas,
    "run": _render_run,
}


def render_page(page: str, root: Path) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    rail = read_rail(root)
    content = _RENDER[page](root)
    out = template.replace("{{TITLE}}", f"Command Center — {page.capitalize()}")
    out = out.replace("{{NAV}}", _nav(page))
    out = out.replace("{{RAIL}}", _rail_html(rail, root))
    out = out.replace("{{CONTENT}}", content)
    return out


# --------------------------------------------------------------------------
# The HTTP server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    root: Path = None  # type: ignore[assignment]  # set by `serve`

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — stdlib signature
        pass  # the run log and status file are the record; a console tee is noise

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 — stdlib method name
        path = urlparse(self.path).path.strip("/")
        if path == "":
            self.send_response(302)
            self.send_header("Location", "/now")
            self.end_headers()
            return
        if path == "api/rail":
            rail = read_rail(self.root)
            self._send_json(200, {
                "run_status": rail.run_status,
                "freshness": rail.freshness_line,
                "account": rail.account_label,
            })
            return
        if path in _RENDER:
            self._send(200, render_page(path, self.root).encode("utf-8"))
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 — stdlib method name
        path = urlparse(self.path).path.strip("/")
        body = self._body()
        try:
            message = self._dispatch_post(path, body)
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

    def _dispatch_post(self, path: str, body: dict) -> str | None:
        root = self.root

        if path == "api/dispatch":
            _guard_dispatch_account()
            card_id = str(body.get("card_id", ""))
            pid = spawn_background("runner", ["--card", card_id], root)
            return f"dispatching {card_id} (pid {pid})"

        if path == "api/night":
            _guard_dispatch_account()
            pid = spawn_background("runner", [], root)
            return f"tonight's run started (pid {pid})"

        if path == "api/chores/plan":
            result = run_command("chores", ["--plan"], root)
            return result.stdout or result.stderr

        if path == "api/chores/run":
            _guard_dispatch_account()
            pid = spawn_background("chores", [], root)
            return f"chore batch started (pid {pid})"

        if path == "api/ingest":
            _guard_dispatch_account()
            args = ["--scribe"] if body.get("scribe") else []
            pid = spawn_background("ingest", args, root)
            return f"classifying the inbox (pid {pid})"

        if path == "api/reorder":
            result = run_command("boardcmd", ["reorder", str(body.get("card_id", "")),
                                              str(body.get("order", ""))], root)
            return _verb_result(result)

        if path == "api/verified":
            result = run_command("boardcmd", ["verified", str(body.get("card_id", ""))], root)
            return _verb_result(result)

        if path == "api/promote":
            result = run_command("boardcmd", ["promote", str(body.get("name", ""))], root)
            return _verb_result(result)

        if path == "api/note":
            lane = str(body.get("lane") or "inbox")
            result = run_command("boardcmd", ["note", str(body.get("name", "")),
                                              "--lane", lane,
                                              "--body", str(body.get("body", ""))], root)
            return _verb_result(result)

        if path == "api/edit":
            result = run_command("boardcmd", ["edit", str(body.get("path", "")),
                                              "--body", str(body.get("body", ""))], root)
            return _verb_result(result)

        if path == "api/review":
            _guard_dispatch_account()
            card_id = str(body.get("card_id", ""))
            pid = spawn_background("drain", ["--card", card_id], root)
            return f"reviewing {card_id} (pid {pid})"

        if path == "api/reconcile":
            result = run_command("reconcile", ["--apply", "--commit"], root)
            return _verb_result(result)

        if path == "api/freshness/refresh":
            state = freshness.read(fetch=True)
            return freshness.describe(state)

        if path == "api/freshness/pull":
            result = run_command("freshness", ["--pull"], root)
            return _verb_result(result)

        if path == "api/account":
            state = select_account(root, str(body.get("label", "")))
            return f"account: {state.label or '(ambient)'}"

        if path == "api/talk":
            session_id = str(body.get("session_id", ""))
            if not session_id:
                raise PanelError("no session_id given")
            open_terminal(root, "claude", "--resume", session_id)
            return f"opened a terminal resuming {session_id}"

        if path == "api/triage":
            open_terminal(root, "claude", "--agent", "triage")
            return "opened a terminal running the triage charter"

        return None


def _verb_result(result: subprocess.CompletedProcess) -> str:
    text = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise PanelError(text or f"exited {result.returncode}")
    return text


def serve(root: Path, port: int = DEFAULT_PORT, *, open_browser: bool = True) -> None:
    Handler.root = root
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
    parser.add_argument("--root", type=Path, default=None, help="repo root (default: found from the cwd)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = parser.parse_args(argv)

    root = (args.root or find_root()).resolve()
    serve(root, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
