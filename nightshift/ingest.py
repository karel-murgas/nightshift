"""Draining the inbox: route every note once, cheaply, before anything expensive runs.

The step this replaces cost roughly an hour and most of a session window to turn two
notes into two cards, because one agent both *investigated what the work is* and
*wrote the card describing it*. Only the first needs the codebase, and most notes do
not need it at all — so the routing decision is made here, by an agent that reads the
notes and nothing else, and the expensive route is spent only where a note earns it.

**Four routes**, defined in the project's own `classifier` charter: `chore` (a thin card
that runs in a batch), `inline` (Karel, at the keyboard), `scribe` (the note is already
elaborated and needs only the envelope), `triage` (there is a fork that cannot be posed
without reading the code).

**It reports before it spends.** Classification is one cheap dispatch over the whole
lane; everything after it is opt-in. That ordering is the point: the person who wrote
the notes is the best judge of which ones hide a fork, and a glance costs nothing
against an hour.

**Triage is never dispatched from here.** It is the expensive route and it is the one
that competes with the night for the same session window, so this command only ever
*lists* what triage would take. Choosing when and where to spend that is Karel's
(`feedback_account_dispatch`, and the plan's §3.1).

**The money rule is checked before every dispatch, not once at the start.** A run that
begins with headroom can lose it partway through a fan-out, and the whole point of
`usage.check` is that a dispatch cannot be un-started.

Deliberately not a lane: the report is a *view* over `inbox/`, regenerated on demand.
A note leaves the lane when it becomes a card or when its inline work is done, so
re-running is idempotent and there is no checklist state to lose.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board, manifest, textio, usage
# The one place the CLI is executed lives in `runner`; see the alias's comment there
# on why this imports it rather than growing a second copy of the deadlock fix.
from nightshift.runner import (claude_binary, ensure_workspace_trusted, repo_root,
                               run_cli)

#: Written at the repo root, next to the digest, because that is the Obsidian vault
#: root — a report the maintainer has to go looking for is a report they do not read.
#: The name comes from `board`, which owns the set: a view is committed *and* exempt
#: from the dispatch dirty-check, and this one was neither until 2026-08-14.
OUT = Path(board.ROUTING_VIEW)

#: Short model aliases, not dated ids: the alias is stable across CLI releases and a
#: pinned id rots. Classification is a reading task with a one-line-per-note tail, so
#: it does not want the session's default tier — that mismatch is the cost this whole
#: module exists to remove.
CLASSIFIER_MODEL = "sonnet"
SCRIBE_MODEL = "sonnet"

CLASSIFY_TIMEOUT_S = 900
SCRIBE_TIMEOUT_S = 900

ROUTES = ("chore", "inline", "scribe", "triage")

#: A fenced block the model wrapped its JSON in. Tolerated rather than forbidden: the
#: charter asks for bare JSON, and rejecting a fence would fail a run over formatting.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class Note:
    """One bare note in the inbox. Not a card — it has no schema to satisfy."""

    path: Path
    text: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size(self) -> int:
        return len(self.text)


@dataclass
class Decision:
    """How one note was routed, and why."""

    note: str
    route: str
    why: str = ""
    dispatchable: bool = True
    confidence: str = "high"

    @property
    def suspect(self) -> bool:
        """Whether Karel should look at this one first when confirming."""
        return self.confidence != "high" or (self.route == "triage" and not self.dispatchable)


@dataclass
class Routing:
    decisions: list[Decision] = field(default_factory=list)
    error: str = ""

    def by_route(self, route: str) -> list[Decision]:
        return [d for d in self.decisions if d.route == route]


def notes(root: Path) -> list[Note]:
    """Every bare note in `inbox/`, oldest first.

    `.gitkeep` and anything not markdown is skipped. Nothing here looks at `ideas/` —
    that lane is private and the hook would refuse the read anyway.
    """
    lane = board.board_dir(root) / "inbox"
    if not lane.is_dir():
        return []
    found: list[Note] = []
    for path in sorted(lane.glob("*.md"), key=lambda p: p.stat().st_mtime):
        try:
            found.append(Note(path=path, text=path.read_text(encoding="utf-8")))
        except OSError as exc:
            print(f"  ! skipping {path.name} - unreadable ({exc})")
    return found


def _extract_json(text: str) -> tuple[dict | None, str]:
    """The JSON object in a model's reply, however it wrapped it."""
    for candidate in ([m.group(1) for m in _FENCE.finditer(text)]
                      + [text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""]):
        if not candidate.strip():
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed, ""
    return None, "no JSON object in the reply"


def _cli_result(completed) -> tuple[str, str]:
    """The assistant's final text out of `--output-format json`, or why not.

    Falls back to raw stdout: a non-JSON envelope is a CLI-shape change, and losing the
    reply to it would be worse than parsing a little loosely.
    """
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return "", f"CLI exited {completed.returncode}: {tail[-1] if tail else 'no output'}"
    try:
        envelope = json.loads(completed.stdout)
    except ValueError:
        return completed.stdout, ""
    if isinstance(envelope, dict):
        if envelope.get("is_error"):
            return "", f"CLI reported an error: {str(envelope.get('result'))[:200]}"
        return str(envelope.get("result", "")), ""
    return completed.stdout, ""


def _guard(allow_paid: bool, what: str) -> usage.Verdict:
    """The money rule, checked immediately before spending on `what`."""
    verdict = usage.check(usage.read(), allow_paid=allow_paid)
    if not verdict.allow:
        print(f"  REFUSED before {what} - {verdict.reason}")
        if verdict.resume_at:
            print(f"    resume at {verdict.resume_at:%Y-%m-%d %H:%M}")
        if verdict.refused_for_money:
            print("    override: re-run with --allow-paid")
    elif not verdict.metered:
        print(f"  (unmetered - {verdict.reason})")
    return verdict


def _dispatch(agent: str, prompt: str, root: Path, model: str,
              timeout: int) -> tuple[str, str]:
    """Run one charter headlessly and return its final text.

    The prompt goes in on **stdin**, never in `argv`: a lane's worth of notes would
    overflow a Windows command line, and `prompt_not_in_argv` is a gate here for that
    reason.
    """
    binary = claude_binary()
    if not binary:
        return "", "the `claude` CLI was not found (set CLAUDE_BIN, or put it on PATH)"
    argv = [binary, "-p", "--agent", agent, "--output-format", "json"]
    if model:
        argv += ["--model", model]
    completed = run_cli(argv, cwd=root, timeout=timeout, prompt=prompt)
    return _cli_result(completed)


def _classify_prompt(found: list[Note]) -> str:
    listing = "\n\n".join(
        f"===== NOTE: {n.name} ({n.size} bytes) =====\n{n.text}" for n in found)
    return (
        "Route every note below. Follow your charter exactly: read only these notes, "
        "do not open the codebase, and emit one JSON object and nothing else.\n\n"
        f"There are {len(found)} notes.\n\n{listing}\n"
    )


def classify(found: list[Note], root: Path, *, model: str = CLASSIFIER_MODEL,
             timeout: int = CLASSIFY_TIMEOUT_S) -> Routing:
    """One dispatch over the whole lane. Cheap by construction — it reads no source."""
    text, why = _dispatch("classifier", _classify_prompt(found), root, model, timeout)
    if why:
        return Routing(error=why)
    payload, why = _extract_json(text)
    if payload is None:
        return Routing(error=why)

    known = {n.name for n in found}
    routing = Routing()
    seen: set[str] = set()
    for entry in payload.get("notes", []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("file", "")).strip()
        route = str(entry.get("route", "")).strip().lower()
        if name not in known:
            print(f"  ! classifier named an unknown note: {name!r} - ignored")
            continue
        if route not in ROUTES:
            print(f"  ! {name}: unknown route {route!r} - treating as inline")
            route = "inline"
        seen.add(name)
        routing.decisions.append(Decision(
            note=name, route=route, why=str(entry.get("why", "")).strip(),
            dispatchable=bool(entry.get("dispatchable", True)),
            confidence=str(entry.get("confidence", "high")).strip().lower()))

    # A note the classifier skipped is not silently dropped. Unrouted means Karel looks
    # at it, which is what `inline` means — the charter is explicit that an unclear note
    # is a routing decision rather than a gap, so a gap here is worth saying out loud.
    for note in found:
        if note.name not in seen:
            print(f"  ! {note.name}: no routing returned - defaulting to inline")
            routing.decisions.append(Decision(
                note=note.name, route="inline", why="classifier returned no entry",
                confidence="low"))
    return routing


def scribe(decisions: list[Decision], root: Path, *, allow_paid: bool = False,
           model: str = SCRIBE_MODEL,
           timeout: int = SCRIBE_TIMEOUT_S) -> tuple[int, int, int]:
    """Fan the scribe over its bucket. Returns (written, bounced, blocked)."""
    written = bounced = blocked = 0
    for decision in decisions:
        if not _guard(allow_paid, f"scribe on {decision.note}").allow:
            blocked = len(decisions) - written - bounced
            break
        print(f"  scribe: {decision.note}")
        text, why = _dispatch("scribe", (
            f"Write the card for `Board/inbox/{decision.note}`. Follow your charter: "
            f"read the note and the schema, not the codebase. If you cannot ground the "
            f"acceptance criteria, emit the bounce object instead of a card."
        ), root, model, timeout)
        if why:
            print(f"    ! failed - {why}")
            bounced += 1
            continue
        payload, _ = _extract_json(text)
        if payload and payload.get("bounce"):
            print(f"    bounced to triage - {payload.get('reason', 'no reason given')}")
            bounced += 1
        else:
            written += 1
    return written, bounced, blocked


def report(routing: Routing, found: list[Note], snapshot: usage.Snapshot,
           now: dt.datetime) -> str:
    """The routing view. Regenerated, never edited — the lane is the state."""
    lines = [f"# Routing - {now:%Y-%m-%d %H:%M}", "",
             f"{len(found)} note(s) in `inbox/`. "
             f"Regenerate with `python -m nightshift.ingest`.", ""]
    for line in usage.describe(snapshot):
        lines.append(f"- {line}")
    lines.append("")

    if routing.error:
        lines += [f"**Classification failed** - {routing.error}", ""]

    headings = {
        # Second person, not a name: this package ships to other repos, and a report
        # that addresses someone else's owner is the `project_agnostic` test's whole
        # subject. Docstrings may name the origin project; runtime strings may not.
        "inline": ("Do now - inline", "You, at the keyboard. No card is written for these."),
        "chore": ("Chores - batch overnight", "Thin cards, verified as one batch."),
        "scribe": ("Scribe - needs the envelope only", "Already elaborated; no investigation."),
        "triage": ("Waiting on triage", "The expensive route. Launch it deliberately, "
                                        "on the account you meant."),
    }
    for route in ("inline", "chore", "scribe", "triage"):
        bucket = routing.by_route(route)
        title, blurb = headings[route]
        lines += [f"## {title} ({len(bucket)})", "", blurb, ""]
        if not bucket:
            lines += ["_none_", ""]
            continue
        for decision in sorted(bucket, key=lambda d: d.note):
            flags = []
            if decision.confidence != "high":
                flags.append(f"confidence {decision.confidence}")
            if not decision.dispatchable:
                flags.append("not unattended-dispatchable")
            suffix = f" _({'; '.join(flags)})_" if flags else ""
            lines.append(f"- **{decision.note}** - {decision.why}{suffix}")
        lines.append("")

    suspect = [d for d in routing.decisions if d.suspect]
    if suspect:
        lines += ["## Check these first", "",
                  "Low confidence, or routed to triage while not dispatchable overnight "
                  "- which means the hour buys nothing.", ""]
        lines += [f"- **{d.note}** ({d.route})" for d in sorted(suspect, key=lambda d: d.note)]
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Route every note in Board/inbox/, cheaply, and report before spending.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo root (default: found from the cwd)")
    parser.add_argument("--scribe", action="store_true",
                        help="after reporting, write cards for the scribe bucket")
    parser.add_argument("--allow-paid", action="store_true",
                        help="proceed even if a dispatch would draw on paid credits")
    parser.add_argument("--model", default=CLASSIFIER_MODEL,
                        help=f"model for the classifier (default {CLASSIFIER_MODEL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the notes and the meters; dispatch nothing")
    args = parser.parse_args(argv)

    root = args.root or (repo_root() if args.root is None else args.root)
    try:
        manifest.find_root(root)
    except Exception:
        pass                                  # a repo without a manifest still has a lane

    found = notes(root)
    print(f"ingest: {len(found)} note(s) in {board.board_rel(root)}/inbox")
    if not found:
        print("  nothing to route")
        return 0

    snapshot = usage.read()
    for line in usage.describe(snapshot):
        print(f"  {line}")

    if args.dry_run:
        print()
        for note in found:
            print(f"  {note.name} ({note.size} B)")
        print("\n(dry run - nothing dispatched)")
        return 0

    if not _guard(args.allow_paid, "classifying").allow:
        return 3

    # Headless `-p` has no trust dialog, so an untrusted workspace makes every dispatch
    # fail with no useful message. Same precondition the runner establishes.
    ensure_workspace_trusted(root)

    print(f"  classifying with {args.model} ...")
    routing = classify(found, root, model=args.model)

    now = dt.datetime.now()
    textio.write_text_lf(root / OUT, report(routing, found, snapshot, now))
    print(f"  wrote {OUT}")

    if routing.error:
        print(f"  classification failed - {routing.error}")
        return 1

    for route in ROUTES:
        print(f"    {route:8} {len(routing.by_route(route))}")

    if args.scribe:
        bucket = routing.by_route("scribe")
        if not bucket:
            print("  no notes routed to scribe")
        else:
            written, bounced, blocked = scribe(bucket, root, allow_paid=args.allow_paid)
            print(f"  scribe: {written} card(s), {bounced} bounced, {blocked} not reached")
            if blocked:
                return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
