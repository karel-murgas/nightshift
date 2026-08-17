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

#: The routes `--scribe` can turn into cards, and the reason the set is not just
#: `("scribe",)`.
#:
#: **`chore` was a dead end until 2026-08-17.** The classifier's charter says a
#: chore "becomes a thin card and runs in a batch overnight", the scribe's charter
#: has a whole section on writing one (*"A short note with nothing to decide is a
#: chore card, and writing it is exactly your job"*), and `chores.select` reads
#: `kind: chore` cards out of `tasks/` — every piece was built. Nothing ever asked
#: the scribe for them: this fan-out took `by_route("scribe")` alone, so a
#: chore-routed note sat in the lane forever and the panel's Chores section was
#: permanently, accurately empty.
#:
#: The two that stay out are out for their own reasons, not by omission. `inline`
#: is Karel's own work and gets no card *by definition* — that is what the route
#: means. `triage` is never dispatched from here at all (see the module docstring):
#: it is the expensive route and choosing when to spend it is a human's call.
WRITABLE_ROUTES = ("chore", "scribe")

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


def card_ids(root: Path) -> set[str]:
    """Every card id on the board, whatever lane holds it.

    `inbox` is excluded deliberately: a bare note is not a card, and the whole
    question this set is used to answer is whether one *became* one.
    """
    return {card.id for lane in board.LANES if lane != "inbox"
            for card in board.cards(root, lane)}


@dataclass
class Wrote:
    """What one scribe dispatch actually did to the board.

    A dispatch that returns cleanly is not evidence that a card exists. **The
    scribe's contract is the one `triage` already states** — *"the note **is** the
    card. You edit the file in place and move it; you never copy it into a new
    card and leave the original behind"* — and until 2026-08-17 nothing checked
    it, on either path. The failure that hides in the gap is not a missing card;
    it is a **duplicate**: a card written while the note stays in `inbox/` gets
    re-routed by the next classify pass and carded again, and the second card
    looks exactly as legitimate as the first.

    So the effect is measured rather than assumed, by the only two facts a
    deterministic caller can check: did a card appear, and did the note leave.
    The note's *name* cannot be part of that check — a card id is a kebab-case
    stem and real notes are called `Regenerate soundtrack.md`, so the scribe has
    to rename as it moves.
    """

    note: str
    carded: list[str] = field(default_factory=list)
    consumed: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.carded) and self.consumed

    @property
    def complaint(self) -> str:
        """What to say when it is not `ok`. Empty when it is."""
        if self.ok:
            return ""
        if self.carded and not self.consumed:
            return (f"wrote {', '.join(self.carded)} but left the note in inbox/ - the "
                    f"next classify pass will route it again and card it twice")
        if self.consumed and not self.carded:
            return "the note left inbox/ and no card appeared anywhere on the board"
        return "no card appeared and the note is untouched - nothing happened"


def scribe(decisions: list[Decision], root: Path, *, allow_paid: bool = False,
           model: str = SCRIBE_MODEL,
           timeout: int = SCRIBE_TIMEOUT_S) -> tuple[int, int, int, int]:
    """Fan the scribe over the writable routes.

    Returns (written, bounced, blocked, stranded) — where `written` means a card
    is on the board *and* the note has left the lane, and `stranded` counts the
    dispatches that returned cleanly without achieving both. See `Wrote`.
    """
    lane = board.board_dir(root) / "inbox"
    written = bounced = blocked = stranded = 0
    for decision in decisions:
        if not _guard(allow_paid, f"scribe on {decision.note}").allow:
            blocked = len(decisions) - written - bounced - stranded
            break
        print(f"  scribe: {decision.note}")
        before = card_ids(root)
        text, why = _dispatch("scribe", (
            f"Write the card for `Board/inbox/{decision.note}`. Follow your charter: "
            f"read the note and the schema, not the codebase. The note **is** the card - "
            f"edit that file in place and move it to its lane; never leave the original "
            f"behind. Bounce only if the note contains a fork you cannot resolve without "
            f"reading the code - not because you cannot name the file or symbol the work "
            f"lands in, which is the worker's job to find."
        ), root, model, timeout)
        if why:
            print(f"    ! failed - {why}")
            bounced += 1
            continue
        payload, _ = _extract_json(text)
        if payload and payload.get("bounce"):
            reason = str(payload.get("reason", "no reason given"))
            print(f"    bounced to triage - {reason}")
            # The re-route the charter promises, performed rather than described.
            # Without it the note keeps the route that just failed, and the button
            # offering to card it buys the same bounce again.
            if reroute_to_triage(root, decision.note, reason):
                print("      -> re-routed to triage in the view")
                _commit(root, f"{decision.note} re-routed to triage by a scribe bounce")
            bounced += 1
            continue
        did = Wrote(note=decision.note,
                    carded=sorted(card_ids(root) - before),
                    consumed=not (lane / decision.note).exists())
        if did.ok:
            print(f"    -> {', '.join(did.carded)}")
            written += 1
            _commit(root, f"{decision.note} carded as {', '.join(did.carded)}")
        else:
            print(f"    ! {did.complaint}")
            stranded += 1
            # Committed even so, because the half-transition is already on disk
            # and leaving it dirty does not undo it — it only means the next
            # board write sweeps it up under an unrelated message. The message
            # is the honest one, so `git log` says a human has to look.
            if did.carded or did.consumed:
                _commit(root, f"{decision.note} stranded — {did.complaint}")
    return written, bounced, blocked, stranded


def _commit(root: Path, what: str) -> None:
    """Close the transaction the scribe opened.

    **Nothing on this path committed, and every neighbouring path does.** Every
    `boardcmd` verb commits, `board.move` commits, `chores` commits — but a card
    written here sat in the working tree with the note's deletion beside it, so
    the board's state and git's disagreed until somebody noticed. Observed on the
    first real fan-out, 2026-08-17: `skills-tinkering` had become a card an hour
    earlier and git still showed `D Board/inbox/skills-tinkering.md` next to an
    untracked `Board/tasks/skills-tinkering.md`.

    **Per note, not per run.** A fan-out that loses its window partway — which
    `_guard` exists to make happen — would otherwise leave every card it did
    write uncommitted, so the cheapest failure would cost the most work.

    `commit_board` stages `Board/` and the generated views wholesale, so an
    unrelated board edit sitting in the tree rides along. That is the framework's
    settled behaviour for every board write (its docstring: "Never `-a`: the
    runner must not be able to sweep up someone else's work in progress" — the
    limit is the board, not the file), and being the one path that behaves
    differently would be worse than the sweep.
    """
    board.commit_board(root, f"board: {what}")


#: Heading and blurb per route, in the order the report lists them.
#:
#: A module constant rather than a local because `report` is no longer the only
#: thing that needs it: `read_view` parses the file back, and a reader keyed on
#: headings a writer can silently reword is a reader that goes quietly blank. The
#: two live together, and `test_ingest` round-trips a report through the parser so
#: rewording one without the other fails a test rather than a panel.
#:
#: Second person, not a name: this package ships to other repos, and a report that
#: addresses someone else's owner is the `project_agnostic` test's whole subject.
#: Docstrings may name the origin project; runtime strings may not.
ROUTE_HEADINGS: dict[str, tuple[str, str]] = {
    "inline": ("Do now - inline", "You, at the keyboard. No card is written for these."),
    "chore": ("Chores - batch overnight", "Thin cards, verified as one batch."),
    "scribe": ("Scribe - needs the envelope only", "Already elaborated; no investigation."),
    "triage": ("Waiting on triage", "The expensive route. Launch it deliberately, "
                                    "on the account you meant."),
}


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

    for route in ("inline", "chore", "scribe", "triage"):
        bucket = routing.by_route(route)
        title, blurb = ROUTE_HEADINGS[route]
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


# --------------------------------------------------------------------------
# Reading the view back. The lane is the state and the report is the view over
# it — but a *reader* of that view is what lets the inbox stop lying about it.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingView:
    """A routing pass as it can be read back off disk.

    **Why this exists at all.** A note does not leave `inbox/` when it is routed —
    routing is a *view*, deliberately, so re-running is idempotent and there is no
    checklist state to lose. The cost of that design was paid by the panel, which
    listed every note under "Not yet classified" whatever had happened to it: on
    2026-08-17 all thirteen notes were classified and all thirteen still read as
    unclassified, with the answer sitting in a file the page linked but did not
    read. So the page reads it.

    Parsing our own generated markdown rather than writing a JSON sidecar is the
    deliberate choice: a second artefact is a second thing to keep in sync, to
    commit or ignore, and to explain to `GENERATED_VIEWS`. The writer and the
    reader are twenty lines apart in one module and a round-trip test binds them.
    """

    written: dt.datetime | None = None
    decisions: dict[str, Decision] = field(default_factory=dict)

    def of(self, note: str) -> Decision | None:
        return self.decisions.get(note)

    @property
    def known(self) -> bool:
        return bool(self.decisions) or self.written is not None


_VIEW_STAMP = re.compile(r"^#\s+Routing\s+-\s+(?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$")
_VIEW_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s+\((?P<count>\d+)\)\s*$")
_VIEW_ENTRY = re.compile(r"^-\s+\*\*(?P<note>.+?)\*\*\s+-\s+(?P<why>.*)$")
#: The trailing `_(confidence low; not unattended-dispatchable)_` the writer appends.
_VIEW_FLAGS = re.compile(r"\s*_\((?P<flags>[^)]*)\)_\s*$")


def parse_report(text: str) -> RoutingView:
    """A routing report, read back into the decisions that produced it.

    Keyed on the headings in `ROUTE_HEADINGS` and nothing else: a section this
    does not recognise — "Check these first", or anything a later version adds —
    contributes no decisions rather than guessing a route for its entries. The
    first mention of a note wins, so the repeat listing under "Check these first"
    cannot overwrite the real one even if its shape ever changes to match.
    """
    by_title = {title: route for route, (title, _) in ROUTE_HEADINGS.items()}
    written: dt.datetime | None = None
    decisions: dict[str, Decision] = {}
    route = ""
    for line in text.splitlines():
        if stamp := _VIEW_STAMP.match(line):
            try:
                written = dt.datetime.strptime(stamp.group("when"), "%Y-%m-%d %H:%M")
            except ValueError:
                written = None
            continue
        if heading := _VIEW_HEADING.match(line):
            route = by_title.get(heading.group("title").strip(), "")
            continue
        if not route:
            continue
        entry = _VIEW_ENTRY.match(line)
        if not entry or entry.group("note") in decisions:
            continue
        why = entry.group("why").strip()
        confidence, dispatchable = "high", True
        if flags := _VIEW_FLAGS.search(why):
            why = why[:flags.start()].strip()
            for flag in flags.group("flags").split(";"):
                flag = flag.strip()
                if flag.startswith("confidence "):
                    confidence = flag[len("confidence "):].strip()
                elif flag == "not unattended-dispatchable":
                    dispatchable = False
        decisions[entry.group("note")] = Decision(
            note=entry.group("note"), route=route, why=why,
            dispatchable=dispatchable, confidence=confidence)
    return RoutingView(written=written, decisions=decisions)


def set_route(root: Path, note: str, route: str, why: str,
              snapshot: usage.Snapshot | None = None) -> bool:
    """Change one note's recorded route, keeping the rest of the view as it stands.

    The classifier is cheap and reads no code, so its answer is a recommendation
    and being wrong about one note is affordable *provided something can correct
    it*. Until 2026-08-17 nothing could: the route was written once by a pass over
    the whole lane and the only way to change one was to pay for another pass.

    The view is rewritten rather than patched line by line, because it is a *view*
    — `report()` is the one thing that knows its shape, and a second writer editing
    its markdown in place is how a reader and a writer drift apart. The
    classification timestamp is preserved on purpose: the routing pass really did
    happen when it says, and moving the stamp to now would make a correction look
    like a fresh classify pass nobody paid for. The `why` carries who corrected it.
    """
    if route not in ROUTES:
        raise ValueError(f"{route!r} is not one of {ROUTES}")
    view = read_view(root)
    if note not in view.decisions:
        return False
    view.decisions[note] = Decision(
        note=note, route=route, why=why, confidence="high",
        dispatchable=view.decisions[note].dispatchable)
    routing = Routing(decisions=list(view.decisions.values()))
    stamp = view.written or dt.datetime.now()
    textio.write_text_lf(root / OUT, report(
        routing, notes(root), snapshot if snapshot is not None else usage.read(), stamp))
    return True


def reroute_to_triage(root: Path, note: str, reason: str,
                      snapshot: usage.Snapshot | None = None) -> bool:
    """Record that the scribe bounced this note, by moving it to `triage` in the view.

    **The charter already promised this and nothing did it.** The scribe's own
    bounce section says a bounce *"costs one cheap dispatch and re-routes the note
    to `triage`, which is the correct outcome and counts as success"* — and the
    bounce was reported to stdout and then dropped. The note kept its old route, so
    the panel went on offering *Write the card* for it, and pressing that button
    bought the same bounce again. Measured on the first real fan-out, 2026-08-17:
    three of five notes bounced, all three still routed `chore` afterwards.

    One caller of `set_route`, named for what it means rather than what it does, so
    the bounce path reads as the charter states it.
    """
    return set_route(root, note, "triage", f"scribe bounced it: {reason}", snapshot)


def read_view(root: Path) -> RoutingView:
    """The last routing pass, or an empty view if there has never been one.

    Never raises: a missing, half-written or hand-mangled report means "nothing is
    known about these notes", which is the state the page rendered unconditionally
    before it read this at all.
    """
    try:
        return parse_report((root / OUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RoutingView()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Route every note in Board/inbox/, cheaply, and report before spending.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo root (default: found from the cwd)")
    parser.add_argument("--scribe", action="store_true",
                        help=f"after reporting, write cards for the "
                             f"{' and '.join(WRITABLE_ROUTES)} buckets")
    parser.add_argument("--only", metavar="NOTE", default="",
                        help="write the card for exactly this note, using the route the "
                             "last pass gave it, and classify nothing. The per-note verb "
                             "the panel's own rows need")
    parser.add_argument("--write-cards", action="store_true",
                        help=f"write the cards for every {' or '.join(WRITABLE_ROUTES)} "
                             f"note in the last pass, and classify nothing")
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

    if args.only:
        return _write_one(root, args.only, allow_paid=args.allow_paid)

    if args.write_cards:
        return _write_recorded(root, allow_paid=args.allow_paid)

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
    # The view is a committed artefact — that is what being in `GENERATED_VIEWS`
    # means, and it is why the dispatch dirty-check exempts it. Writing it and
    # not committing it left the report Karel reads sitting modified for hours on
    # 2026-08-17, indistinguishable from an edit somebody had made by hand.
    _commit(root, f"routed {len(found)} note(s) in {board.board_rel(root)}/inbox")

    if routing.error:
        print(f"  classification failed - {routing.error}")
        return 1

    for route in ROUTES:
        print(f"    {route:8} {len(routing.by_route(route))}")

    if args.scribe:
        # Both writable buckets, in the order the report lists them, so the cheap
        # batch work is carded before the elaborated notes that take longer.
        bucket = [d for route in WRITABLE_ROUTES for d in routing.by_route(route)]
        if not bucket:
            print(f"  no notes routed to {' or '.join(WRITABLE_ROUTES)}")
        else:
            written, bounced, blocked, stranded = scribe(bucket, root,
                                                         allow_paid=args.allow_paid)
            print(f"  scribe: {written} card(s), {bounced} bounced, "
                  f"{stranded} stranded, {blocked} not reached")
            if blocked:
                return 3
            if stranded:
                return 1
    return 0


def _write_recorded(root: Path, *, allow_paid: bool = False) -> int:
    """`--write-cards`: card every writable note the last pass routed, no reclassify.

    **The second step of the two the module's own ordering asks for**, separated so
    that it *is* a second step. `--scribe` does both halves in one command, and it
    has to for the unattended case — but as the shape of a button it quietly
    inverts the rule this module was built on: *"It reports before it spends.
    Classification is one cheap dispatch over the whole lane; everything after it
    is opt-in."* A combined button cannot be opt-in about the second half, and it
    pays for a fresh classify pass every time it is pressed to re-learn what the
    report on disk already says.

    So: classify to look, then write when the look was fine. `--only` is the same
    act on one note; this is the same act on all of them.
    """
    view = read_view(root)
    if not view.known:
        print("ingest: no routing pass on record - classify the inbox first "
              "(`python -m nightshift.ingest`)")
        return 2
    present = {note.name for note in notes(root)}
    bucket = [d for route in WRITABLE_ROUTES for d in view.decisions.values()
              if d.route == route and d.note in present]
    when = f", from the pass of {view.written:%Y-%m-%d %H:%M}" if view.written else ""
    print(f"ingest: {len(bucket)} note(s) to card{when}")
    if not bucket:
        print(f"  nothing routed to {' or '.join(WRITABLE_ROUTES)} is still in the lane")
        return 0
    ensure_workspace_trusted(root)
    written, bounced, blocked, stranded = scribe(bucket, root, allow_paid=allow_paid)
    print(f"  scribe: {written} card(s), {bounced} bounced, "
          f"{stranded} stranded, {blocked} not reached")
    if blocked:
        return 3
    return 1 if stranded else 0


def _write_one(root: Path, note: str, *, allow_paid: bool = False) -> int:
    """`--only <note>`: card exactly this note, on the route it was already given.

    It reads the route back off `Routing.md` rather than classifying again. That is
    the point of the flag: one note's card costs one scribe dispatch, not a fresh
    pass over the whole lane — and re-classifying to answer a question already
    answered is the expense this module exists to avoid.

    A note routed `inline` or `triage` is refused rather than quietly scribed. Both
    refusals are the route's own meaning: `inline` has no card by definition, and
    `triage` is never dispatched from here.
    """
    lane = board.board_dir(root) / "inbox"
    if not (lane / note).is_file():
        print(f"ingest: no note named {note!r} in {board.board_rel(root)}/inbox")
        return 2
    view = read_view(root)
    decision = view.of(note)
    if decision is None:
        print(f"ingest: {note} has no routing yet - classify the inbox first "
              f"(`python -m nightshift.ingest`)")
        return 2
    if decision.route not in WRITABLE_ROUTES:
        print(f"ingest: {note} is routed `{decision.route}`, and only "
              f"{' and '.join(WRITABLE_ROUTES)} can be written from here")
        if decision.route == "triage":
            print("  triage is the expensive route and is launched deliberately, "
                  "one note at a time, by a human")
        elif decision.route == "inline":
            print("  an inline note is your own work at the keyboard - it gets no card")
        return 2

    print(f"ingest: {note} ({decision.route})")
    if not _guard(allow_paid, f"scribe on {note}").allow:
        return 3
    ensure_workspace_trusted(root)
    written, bounced, blocked, stranded = scribe([decision], root, allow_paid=allow_paid)
    if blocked:
        return 3
    if stranded or not written:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
