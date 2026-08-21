"""Draining the inbox: route every note once, cheaply, before anything expensive runs.

The step this replaces cost roughly an hour and most of a session window to turn two
notes into two cards, because one agent both *investigated what the work is* and
*wrote the card describing it*. Only the first needs the codebase, and most notes do
not need it at all — so the routing decision is made here, by an agent that reads the
notes and nothing else, and the expensive route is spent only where a note earns it.

**Four routes**, defined in the project's own `classifier` charter: `chore` (a thin card
that runs in a batch), `inline` (a person at the keyboard), `scribe` (the note is already
elaborated and needs only the envelope), `triage` (there is a fork that cannot be posed
without reading the code).

**The decision is written onto the note**, as `route:` in its frontmatter, by the
deterministic step that follows the dispatch (`apply_routing`). Everything else in this
module's economy rests on that: `unrouted` is what a pass is *for*, so classifying twice
costs one dispatch rather than two, and a note routed `triage` — which nothing here ever
dispatches — stops being re-decided every time the button is pressed.

**An `inline` note becomes a card immediately**, in `tasks/`, carrying `unattended: false`
and `kind: inline`. It is the one route with nothing left to write, so there is nothing to
wait in the lane for. The point is not bookkeeping: it puts a person's work on the same
rails as everything else — one classify step, one place that says what is ready, one lane
move to finish — instead of in a parallel world of notes that no lane-reading view could
see (Karel, 2026-08-18: *"I would like the similar flow for all the files"*).

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

Deliberately not a lane: the report is a *view* over `inbox/`, regenerated on demand from
the routes stamped on the notes themselves. It is a report, not a record — nothing reads
it to decide what to do, so losing it costs a regeneration and not an answer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board, manifest, suite, textio, usage
from nightshift.manifest import AI_DIR
# The one place the CLI is executed lives in `runner`; see the alias's comment there
# on why this imports it rather than growing a second copy of the deadlock fix.
from nightshift.runner import (cannot_edit, claude_binary, ensure_workspace_trusted,
                               host_setting, repo_root, run_cli)

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

#: The vocabulary lives in `board` because it is written into frontmatter and the
#: schema gate has to know it; this module is where it is *decided*.
ROUTES = board.ROUTES

#: Routes whose note stays in `inbox/` after the pass that decided them, carrying
#: `route:` so the next pass knows not to ask again.
#:
#: `inline` is absent because an inline note does not stay — it becomes a card in
#: `tasks/` immediately (`card_inline`). That is the whole of the difference between
#: the two halves of `apply_routing`: these three are still waiting to become cards,
#: and an inline note already is one.
STAMPED_ROUTES = ("chore", "scribe", "triage")

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
#: needs no scribe: its card is written deterministically by `card_inline` the moment
#: the route is decided, because a card whose acceptance is "a person decided" has no
#: envelope an agent could add. `triage` is never dispatched from here at all (see the
#: module docstring): it is the expensive route and choosing when to spend it is a
#: human's call.
WRITABLE_ROUTES = ("chore", "scribe")

#: A fenced block the model wrapped its JSON in. Tolerated rather than forbidden: the
#: charter asks for bare JSON, and rejecting a fence would fail a run over formatting.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class Note:
    """One note in the inbox, at whatever stage of becoming a card it has reached.

    Not "a bare note": on any board somebody opens in Obsidian every note already
    carries `state:` and `kanban_order:`, and after a routing pass it carries
    `route:` as well. What makes it a note rather than a card is the lane it is in —
    `inbox/` is `_UNTRIAGED`, where the schema asks for no sections at all.
    """

    path: Path
    text: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size(self) -> int:
        return len(self.text)

    @property
    def fields(self) -> dict[str, str]:
        return board.parse_fields(self.text)

    @property
    def route(self) -> str:
        """The route stamped on this note, or "" if no pass has answered for it.

        An unrecognised value reads as unrouted rather than raising: the field is
        hand-editable in Obsidian, and a typo should cost one classify pass, not a
        traceback in a command that is reading the whole lane.
        """
        found = self.fields.get("route", "").strip().lower()
        return found if found in ROUTES else ""


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
    """Every note in `inbox/`, oldest first, routed or not.

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


def unrouted(found: list[Note]) -> list[Note]:
    """The notes a classify pass still has a question about.

    **This is what stops the classifier being paid twice for the same answer.**
    Routing used to live only in `Routing.md`, a view regenerated from scratch on
    every run, so a note sat in the lane being re-read and re-decided for as long as
    it took to become a card — and a note routed `triage`, which nothing dispatches
    automatically, was re-decided forever. Karel, 2026-08-18: *"Definitely not
    running classifier multiple times."*

    A note whose text has changed since it was routed is *not* picked up again by
    this. That is deliberate: an edit does not necessarily invalidate the routing,
    and quietly re-spending on a typo fix would be the same defect in a subtler form.
    Deleting the `route:` line puts a note back in this set, and it is a human's call
    — the inbox page marks a note edited since the pass that routed it, which is the
    prompt to make it rather than a reason to spend on his behalf.
    """
    return [note for note in found if not note.route]


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


def denials(completed) -> list[str]:
    """Which tool calls the CLI refused, out of its own envelope.

    `runner.read_telemetry` has read this field for months and reports it as *"the
    worker was refused a tool it asked for"*; this module ignored it, so an agent
    that could not write the file it was told to write reported nothing at all and
    the run said only that nothing happened. It is the difference between "the
    scribe declined" and "the scribe was not allowed".

    **The tool names, not just a count.** A refusal is not on its own a failure:
    `acceptEdits` approves file edits and nothing else, so an agent that reaches
    for a shell command gets refused, works around it, and produces a perfectly
    good card — which is exactly what happened to the first note of the 16:10 run.
    A bare count cannot tell that story from the one where the refusal *was* the
    reason nothing happened. The name can.
    """
    try:
        envelope = json.loads(completed.stdout)
    except (ValueError, AttributeError):
        return []
    found = envelope.get("permission_denials") if isinstance(envelope, dict) else None
    if not isinstance(found, list):
        return []
    names = []
    for entry in found:
        if not isinstance(entry, dict):
            names.append("an unnamed tool")
            continue
        name = str(entry.get("tool_name") or "an unnamed tool")
        # A reason when the refuser gave one. `PreToolUse` hooks do — that is how
        # `ideas_fence` explains that the private lane is not readable — and it is
        # the difference between a line you can act on and a line you can only
        # worry about.
        why = entry.get("message") or entry.get("reason") or ""
        names.append(f"{name}: {str(why)[:120]}" if why else name)
    return names


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
    argv = [binary, "-p", "--agent", agent, "--output-format", "json",
            # **The flag this module was missing, and it silently broke the scribe.**
            # Every dispatch site in `runner` passes it — `run_producer`, the
            # checker, the reviewer, the drain — and this one did not. A headless
            # `-p` session in the default permission mode has nobody to approve an
            # edit, so the scribe could not write the card it was asked for: it
            # explained itself in prose, returned exit 0, and the run reported
            # "nothing happened" with no idea why. Measured 2026-08-17: two notes
            # stranded on two consecutive runs.
            #
            # Read through `host_setting` rather than hardcoded, for the reason
            # `runner` reads it that way: the posture is a property of the machine
            # (`.ai/hosts.json`), not of the verb, and a box that wants a stricter
            # mode must be able to say so once.
            "--permission-mode", str(host_setting(root, "permission_mode", "acceptEdits"))]
    if model:
        argv += ["--model", model]
    completed = run_cli(argv, cwd=root, timeout=timeout, prompt=prompt)
    if refused := denials(completed):
        # Parenthesised, not prefixed with `!`, and that is not cosmetic. `!` is
        # this module's failure prefix — `parse_progress` reads `    ! <text>` as
        # the reason a note stranded — so announcing a *survivable* refusal that
        # way both alarms the reader and mis-parses. Karel, 2026-08-17, on seeing
        # it above a card that had been written perfectly well: quoting the two
        # lines back, which is what a reader does when a page contradicts itself.
        #
        # Said here rather than returned, so the seam `_dispatch` presents to its
        # callers — and to every test that fakes it — stays two values wide.
        # **It does not say why, because it cannot know why.** The first version of
        # this line blamed the permission mode. On the machine it was written for,
        # `hosts.json` sets `bypassPermissions`, under which the mode refuses
        # nothing at all — so the refusal came from one of the six `PreToolUse`
        # hooks (`ideas_fence` fencing the private lane, `commit_pathspec`,
        # `preflight_guard`, `worktree_fence`, `tier_guard`), each of which is doing
        # its job. Asserting a cause the data does not carry is the same mistake
        # this module spent the day fixing, one layer up.
        print(f"    ({len(refused)} tool call(s) refused — by the permission mode or a "
              f"PreToolUse hook: {'; '.join(sorted(set(refused)))})")
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


#: What an `inline`-routed note becomes, in `tasks/`, the moment it is routed.
#:
#: **Every stamped value is one this step can actually know**, which is the rule
#: `_CLOSED_INLINE` in `boardcmd` set and the reason both templates are this short:
#:
#: * `worker: none` / `recipe: none` — nothing will dispatch it, and naming an agent
#:   would put its name on a person's work.
#: * `unattended: false` — the field governs exactly one decision, "may the runner
#:   start this", and for work routed to a person the answer is no. `runner.select`
#:   and `chores.select` both already refuse on it, so this is the card declaring
#:   itself to machinery that is already listening.
#: * `kind: inline` — what tells this card apart from one a worker would take, in a
#:   lane that holds both. It is also what exempts it from `## Approach`.
#: * `tier: worker` — `_TIERS` is `{worker, lead}` and neither describes a human. The
#:   lower is the honest choice: a tier is what a dispatcher would resolve to a
#:   model, and nothing here will ever resolve it.
#: * `verify: review` — the person who did the work is the person who would have
#:   played it, so `testing/` would be asking Karel to verify himself.
#:
#: `## Acceptance` says who decides rather than inventing criteria. A note is routed
#: `inline` *because* it has no machine-checkable brief; writing one here would
#: fabricate the brief the route exists to do without.
_INLINE_CARD = """\
---
id: {ident}
title: "{title}"
state: tasks
tier: worker
worker: none
recipe: none
unattended: false
kind: inline
verify: review
created: {today}
---

## Intent

{body}

## Steps

At the keyboard, not dispatched.

## Acceptance

- human: routed `inline` from `{lane}/{filename}` on {today}, which means a person is the process and their judgment that it is finished is the acceptance — there were never machine criteria to meet.

## Open questions

none
"""


def card_inline(root: Path, note: Note) -> str:
    """Turn one `inline`-routed note into its card in `tasks/`. Returns the card id.

    **The route that used to be the exception is now on the same rails as the rest.**
    An inline note had no card *by definition*, so it stayed in `inbox/` while its
    work happened somewhere else entirely — invisible to every lane-reading view,
    re-routed by each classify pass, and closable only by a bespoke verb written for
    the purpose. Karel, 2026-08-18: *"I would like the similar flow for all the files
    — classify, triage if needed, work on them (inline or bulk or runner), review if
    needed, move to testing / done, merge, push."*

    So the card is the flow, and *how* the work gets done is a field on it rather
    than a separate world: `unattended: false` says a person does it, and `tasks/`
    says it is ready to be done. Nothing about the lanes changes to accommodate it.

    The note's own text becomes `## Intent` — it is the best statement of what the
    work is, and it was written before anyone knew the answer.
    """
    ident = board.slug(note.path.stem)
    target = board.board_dir(root) / "tasks" / f"{ident}.md"
    if target.exists():
        raise FileExistsError(f"tasks/{ident}.md already exists")
    body = board.FRONTMATTER.sub("", note.text, count=1).strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(target, _INLINE_CARD.format(
        ident=ident, title=note.path.stem.replace('"', "'"),
        today=dt.date.today().isoformat(), body=body or "(the note was empty)",
        lane="inbox", filename=note.name))
    note.path.unlink()
    return ident


def stamp_route(note: Note, route: str) -> str:
    """Write `route:` onto the note, and return what its file should now contain.

    A note with no frontmatter block gets one rather than being refused: `inbox/` is
    `_UNTRIAGED`, so a block holding one field is legal there, and the alternative is
    a lane where some notes remember their routing and some cannot.
    """
    if board.FRONTMATTER.match(note.text):
        return board.set_fields(note.text, {"route": route})
    return f"---\nroute: {route}\n---\n\n{note.text.lstrip()}"


@dataclass
class Applied:
    """What the deterministic half of a pass did to the lane.

    Separated from `Routing` because they answer different questions: the routing is
    what the classifier *said*, and this is what the board *did* about it. A pass
    that classified ten notes and moved none is a real state — every route already
    stamped — and it should read as one rather than as a failure.
    """

    carded: list[str] = field(default_factory=list)
    stamped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def apply_routing(root: Path, routing: Routing, found: list[Note]) -> Applied:
    """Put every decision on disk: inline notes become cards, the rest get stamped.

    **Deterministic, and that is the point.** No agent runs here — the classifier has
    already answered, and this is the file work that answer implies. It is what makes
    a routing pass have an effect on the lane instead of only on a view, and it is
    why `unrouted` can be trusted the next time round.

    Idempotent by construction: a note already carrying the route it is being given
    is left alone, and one that has already become a card is not in `found` at all.
    """
    by_name = {note.name: note for note in found}
    out = Applied()
    for decision in routing.decisions:
        note = by_name.get(decision.note)
        if note is None:
            continue
        try:
            if decision.route == "inline":
                out.carded.append(card_inline(root, note))
            elif decision.route in STAMPED_ROUTES:
                if note.route == decision.route:
                    continue
                textio.write_text_lf(note.path, stamp_route(note, decision.route))
                out.stamped.append(note.name)
        except (OSError, ValueError, FileExistsError) as exc:
            print(f"  ! {decision.note}: could not apply route "
                  f"{decision.route!r} - {exc}")
            out.failed.append(decision.note)
    return out


def cannot_card(root: Path) -> str:
    """Why the scribe could not write a card here, or "" if it can.

    **Checked before spending, because the siblings already learned to.**
    `fix.can_dispatch` refuses a pass whose mode cannot run Bash — *"it would spend
    a round and a budget discovering that"* — and `update.merge` refuses `default`
    with the sentence that describes exactly what happened here on 2026-08-17:
    *"which cannot edit files — the agent could read both versions and write
    neither."* Two modules had the knowledge and the guard. This one had neither,
    so it spent two runs and twenty-two minutes discovering it, and reported the
    result as the scribe declining.

    Only on the card-writing path: `classify` reads notes and writes no file, which
    is why it kept working throughout and hid the fault.
    """
    if mode := cannot_edit(root):
        # `AI_DIR` rather than the literal, which is how `fix` and `update` write
        # the same sentence: the path exists only in a consuming project, so
        # spelling it out here makes `source_reference_liveness` report a dangling
        # reference in this repo, correctly.
        return (f"permission_mode is `{mode}`, under which a headless agent cannot write "
                f"a file — the scribe would read the note and card nothing. Set it to "
                f"`acceptEdits` for this machine in {AI_DIR}/hosts.json.")
    return ""


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
    if why := cannot_card(root):
        print(f"  REFUSED before the scribe - {why}")
        return 0, 0, len(decisions), 0
    lane = board.board_dir(root) / "inbox"
    written = bounced = blocked = stranded = 0
    for position, decision in enumerate(decisions, start=1):
        if not _guard(allow_paid, f"scribe on {decision.note}").allow:
            blocked = len(decisions) - written - bounced - stranded
            break
        # The counter is what makes the log answer "how far are we". A fan-out of
        # five notes at up to a few minutes each is a long silence otherwise, and
        # the panel renders this log as its progress view.
        print(f"  [{position}/{len(decisions)}] scribe: {decision.note}")
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
            # **The reply is the only evidence, so it is not thrown away.** A
            # stranded dispatch is by definition the case nobody understands: the
            # agent returned cleanly, emitted no bounce, and changed nothing. On
            # 2026-08-17 that happened twice in a row and the log said only
            # "nothing happened" — the sentence in which the scribe explained
            # itself had been discarded by the code that printed the complaint.
            for line in _quote(text):
                print(line)
            stranded += 1
            # Committed even so, because the half-transition is already on disk
            # and leaving it dirty does not undo it — it only means the next
            # board write sweeps it up under an unrelated message. The message
            # is the honest one, so `git log` says a human has to look.
            if did.carded or did.consumed:
                _commit(root, f"{decision.note} stranded — {did.complaint}")
    return written, bounced, blocked, stranded


#: How much of an unexplained reply to quote into the log. Enough to carry a
#: refusal or a question, short of pasting a whole card draft into a run log.
QUOTED_LINES = 8
QUOTED_WIDTH = 160


def _quote(text: str) -> list[str]:
    """The agent's own words, indented, for the log to carry verbatim."""
    lines = [line.rstrip() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines:
        return ["      (it said nothing at all)"]
    out = [f"      said: {lines[0][:QUOTED_WIDTH]}"]
    out += [f"            {line[:QUOTED_WIDTH]}" for line in lines[1:QUOTED_LINES]]
    if len(lines) > QUOTED_LINES:
        out.append(f"            ... {len(lines) - QUOTED_LINES} more line(s)")
    return out


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
    "inline": ("Do now - inline", "Carded straight into tasks/ as `unattended: false`; "
                                  "the lane is empty of these by the time you read it."),
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

    **Why this exists at all.** Routing used to live *only* here, in a regenerated
    view, and a note carried no trace of having been decided. The cost was paid by
    the panel, which listed every note under "Not yet classified" whatever had
    happened to it: on 2026-08-17 all thirteen notes were classified and all thirteen
    still read as unclassified, with the answer sitting in a file the page linked but
    did not read. So the page reads it.

    Since 2026-08-18 the durable answer is `route:` on the note (`Note.route`), and
    this parser's remaining job is the part a frontmatter field cannot carry: the
    classifier's one-line `why`, its confidence, and whether it thought the night
    could take the work. A note whose `why` is missing here is still correctly
    routed — which is why every reader treats this as decoration over `Note.route`
    rather than as the source of it.

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


# --------------------------------------------------------------------------
# Reading a run's own output back as a roster. The panel renders this; the
# format belongs here, next to the prints that produce it, for the same reason
# `parse_report` sits next to `report`.
# --------------------------------------------------------------------------

#: What one note's card-writing attempt came to, and how to draw it.
ITEM_STATES = {
    "done": ("m-ok", "&check;"),
    "routed": ("m-ok", "&check;"),
    "bounced": ("m-now", "?"),
    "stranded": ("m-bad", "&times;"),
    "running": ("m-wait", "&middot;"),
}


@dataclass
class Item:
    """One note the run acted on, as its own line of it.

    Two passes produce these and they differ only in what the middle column
    says: the carding fan-out reports a `state` per note (done, bounced,
    stranded), and a classify pass reports the `route` it decided. `route` is
    therefore empty for everything but a classify pass, and a reader that finds
    it set is looking at a routing line.
    """

    name: str
    state: str = "running"
    detail: str = ""
    route: str = ""


@dataclass
class Progress:
    """A run's own output, read back as the roster the panel draws.

    **Why parsed rather than streamed into a status file.** A second artefact
    written for the panel's benefit is a second thing to keep in step with what
    the command prints, and the command has to print it anyway — the log is the
    record a person reads in a terminal. So the prints are the interface, and this
    is its reader, sitting in the same module for the same reason `parse_report`
    does. The tests drive real logs from real runs through it.

    **What a classify pass cannot offer, and what it can.** It is *one* dispatch
    over the whole lane — that is the entire economy of the thing (`classify`:
    "One dispatch over the whole lane. Cheap by construction"). So there is no
    per-note progress to report *while it runs*: it has a phase, and nothing
    finer, and inventing more would be reporting something nobody measured.
    What it does have, the moment it lands, is a decision per note — and since
    2026-08-21 it prints them, so the pass reads back as one row per note with
    the route it was given rather than as four counts. The counts are still
    parsed: an older log has only those, and a mid-flight pass has neither.
    """

    phase: str = ""
    total: int = 0
    items: list[Item] = field(default_factory=list)
    routes: dict[str, int] = field(default_factory=dict)

    @property
    def finished(self) -> int:
        return sum(1 for i in self.items if i.state != "running")


_P_TOTAL = re.compile(r"^ingest: (?P<n>\d+) note\(s\)")
_P_ITEM = re.compile(r"^  (?:\[(?P<at>\d+)/(?P<of>\d+)\] )?scribe: (?P<note>.+?)\s*$")
_P_DONE = re.compile(r"^    -> (?P<cards>.+?)\s*$")
_P_BOUNCED = re.compile(r"^    bounced to triage - (?P<why>.+?)\s*$")
_P_STRANDED = re.compile(r"^    ! (?P<why>.+?)\s*$")
_P_ROUTE = re.compile(r"^    (?P<route>\w[\w-]*)\s+(?P<n>\d+)\s*$")
#: `  routed: some-note.md -> triage - why it went there`. The note name is
#: non-greedy so the *first* arrow separates it from the route, and the detail is
#: whatever is left — the classifier's own sentence, which can hold anything.
_P_ROUTED = re.compile(r"^  routed: (?P<note>.+?) -> (?P<route>[\w-]+)"
                       r"(?: - (?P<detail>.*))?\s*$")
_P_SUMMARY = re.compile(r"^  scribe: \d+ card")


def parse_progress(text: str) -> Progress:
    """A run's log, read back as a roster. Never raises; unknown lines are skipped.

    A line this does not recognise contributes nothing rather than guessing — the
    log carries meter readings, git warnings and an agent's own quoted words, and
    a parser that tried to interpret those would report fiction about a run.
    """
    out = Progress()
    for line in (text or "").splitlines():
        if found := _P_TOTAL.match(line):
            out.total = int(found.group("n"))
            out.phase = "carding" if "to card" in line else "reading the lane"
        elif line.startswith("  classifying with"):
            out.phase = "classifying"
        elif line.startswith("  wrote "):
            out.phase = "routed"
        elif found := _P_ROUTED.match(line):
            # A routing line, not a card-writing one: the note is decided, so the
            # row is finished either way, and `!` is this module's failure prefix
            # wherever it appears.
            detail = (found.group("detail") or "").strip()
            state = "routed"
            if detail.startswith("!"):
                state, detail = "stranded", detail[1:].strip()
            out.items.append(Item(name=found.group("note"), state=state,
                                  detail=detail, route=found.group("route")))
        elif _P_SUMMARY.match(line):
            # The fan-out's own tally — `scribe: 1 card(s), 3 bounced, ...` — which
            # shares its prefix with a per-note line and would otherwise be read as
            # a note called "1 card(s), 3 bounced, 1 stranded, 0 not reached".
            # Checked before the item pattern rather than inside it: the two are
            # distinguished by which is more specific, not by argument order.
            out.phase = "done"
        elif found := _P_ITEM.match(line):
            out.phase = "carding"
            if found.group("of"):
                out.total = int(found.group("of"))
            out.items.append(Item(name=found.group("note")))
        elif out.items and (found := _P_DONE.match(line)):
            out.items[-1].state, out.items[-1].detail = "done", found.group("cards")
        elif out.items and (found := _P_BOUNCED.match(line)):
            out.items[-1].state, out.items[-1].detail = "bounced", found.group("why")
        elif out.items and not _P_SUMMARY.match(line) and (found := _P_STRANDED.match(line)):
            out.items[-1].state, out.items[-1].detail = "stranded", found.group("why")
        elif out.phase == "routed" and (found := _P_ROUTE.match(line)):
            out.routes[found.group("route")] = int(found.group("n"))
    return out


def set_route(root: Path, note: str, route: str, why: str,
              snapshot: usage.Snapshot | None = None) -> bool:
    """Change one note's recorded route, keeping the rest of the view as it stands.

    The classifier is cheap and reads no code, so its answer is a recommendation
    and being wrong about one note is affordable *provided something can correct
    it*. Until 2026-08-17 nothing could: the route was written once by a pass over
    the whole lane and the only way to change one was to pay for another pass.

    **The note is written first, and it is the part that matters.** `route:` on the
    file is the durable answer — `unrouted` reads it to decide what a pass still owes
    a dispatch — so a correction that only rewrote the view would be undone by the
    next classify pass, which is the failure the view-only design had all along.

    Re-routing to `inline` cards the note on the spot, exactly as a classify pass
    would. `reroute_to_triage` is the only caller today and it can never take that
    branch, so nothing exercises it in production — it is here because a function
    that takes a route must mean the same thing by a route as everything else does,
    and the alternative is a correction that silently leaves the note in the lane.
    A test covers it for that reason.

    The view is then rewritten rather than patched line by line, because it is a
    *view* — `report()` is the one thing that knows its shape, and a second writer
    editing its markdown in place is how a reader and a writer drift apart. The
    classification timestamp is preserved on purpose: the routing pass really did
    happen when it says, and moving the stamp to now would make a correction look
    like a fresh classify pass nobody paid for. The `why` carries who corrected it.

    Returns False when there is no such note in the lane — the one case where there
    is nothing to correct.
    """
    if route not in ROUTES:
        raise ValueError(f"{route!r} is not one of {ROUTES}")
    view = read_view(root)
    found = notes(root)
    target = next((n for n in found if n.name == note), None)
    if target is None:
        return False

    if route == "inline":
        card_inline(root, target)
    else:
        textio.write_text_lf(target.path, stamp_route(target, route))

    prior = view.of(note)
    view.decisions[note] = Decision(
        note=note, route=route, why=why, confidence="high",
        dispatchable=prior.dispatchable if prior else True)
    found = notes(root)
    live = [d for d in view.decisions.values() if d.note in {n.name for n in found}]
    stamp = view.written or dt.datetime.now()
    textio.write_text_lf(root / OUT, report(
        Routing(decisions=live), found,
        snapshot if snapshot is not None else usage.read(), stamp))
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


def recorded(root: Path, found: list[Note]) -> list[Decision]:
    """Decisions already stamped on the notes, dressed back up as `Decision`s.

    The report describes the *lane*, not the pass — so a note routed three passes
    ago and still waiting on triage has to appear in it, or the view would empty
    itself as the classifier stopped re-deciding notes it had already decided.

    The note's own `route:` wins over the view: it is the durable record, and the
    view is regenerated from it. The `why` is borrowed from the last report when it
    still describes the same route, because that sentence is the classifier's and
    this function cannot write a replacement for it.
    """
    view = read_view(root)
    out: list[Decision] = []
    for note in found:
        if not note.route:
            continue
        prior = view.of(note.name)
        if prior and prior.route == note.route:
            out.append(prior)
        else:
            out.append(Decision(note=note.name, route=note.route,
                                why="routed by an earlier pass"))
    return out


# --------------------------------------------------------------------------
# Publishing what the pass committed. The board is a shared artefact: a routing
# decision that exists only on the machine that made it is a decision the other
# machine — and the panel on it — cannot see.
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """git, captured. `encoding=` is not optional on Windows — these three lines
    are `runner._git`'s, copied rather than imported: reaching into another
    module's privates to save three lines is the worse of the two smells."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


#: The explicit zero `publish` hands the preflight's corrections check.
#:
#: That check exists to make a *lesson* mandatory when a branch taught one, and to
#: refuse a silent zero — `--no-corrections REASON` is the honest alternative it
#: ships with. This is the rare caller that can state its reason once and have it
#: be true every time: a routing pass moves card content and touches no code, so
#: there is nothing for it to have learned. A pass carrying code never reaches
#: here — `not_board` stops it first.
NO_LESSON = "routing pass: board content only, nothing to log"


def not_board(root: Path, paths: list[str]) -> str:
    """The first path a board write could not have produced, or `""`.

    **The whole safety of the automatic push rests here**, because what is pushed
    is a *branch*, not this run's own commits: whatever else was committed in this
    checkout and never published goes out with it. The argument for automating the
    push at all was that the commit carries no code (Karel, 2026-08-21: *"preflight
    only testing the cards as it does not touch the code"*), so that has to be a
    checked claim rather than an assumption about who else has been working here.

    `suite.classify` is the judge, so this and the test slice can never disagree
    about what a path is. BOARD and NOTE are what a board write produces; a
    generated view sits at the repo root and classifies as `other`, so the three
    are named — they are exactly what `commit_board` stages beside `Board/`.
    """
    for path in paths:
        if path in board.GENERATED_VIEWS:
            continue
        if suite.classify(path, root) not in (suite.BOARD, suite.NOTE):
            return path
    return ""


def unpushed(root: Path) -> list[str]:
    """Every path this branch has committed that its upstream does not carry.

    `-z`, never `.split()`: a note is named the way a person names a thought, and
    `Board/inbox/Stale README.md` splits on whitespace into two paths — one of them
    a file nobody has touched. (`preflight._changed_paths` and the runner's diff
    readers still split. It costs them nothing today, because a fabricated token
    classifies as `other` and `other` selects no tests; here it would refuse a
    perfectly good push.)
    """
    out = _git(root, "diff", "--name-only", "-z", "@{u}..HEAD")
    if out.returncode != 0:
        return []
    return [path for path in out.stdout.split("\0") if path]


def publish(root: Path) -> bool:
    """Push the branch when everything unpushed on it is board content. Never fatal.

    **Why a routing pass pushes at all.** Karel, 2026-08-21: *"This should push
    automatically as part of the ingest."* It is the request that put
    `publish_remote` into the runner a fortnight earlier — *"Pushing to test after
    card success should be automatic"* — arriving at the other verb that writes the
    board. A pass that lands eight decisions locally and stops leaves the other
    machine reading a board that no longer exists, and leaves the work one disk
    failure from gone.

    **Why not `publish_remote` itself.** That switch guards publishing *code*: the
    integration branch after a night, and the card branches with it. It is declared
    per machine, and the desktop has not declared it — so keying the board on it
    would mean the board goes on being kept to itself on the box this was asked
    for. What makes this safe is not a per-machine opinion but `not_board`: nothing
    unpushed is code. The branch's own upstream says where to push, so there is no
    second remote to configure and none to guess.

    **Why a preflight, when the runner's own `publish()` takes none.** On a
    board-only diff it costs seconds rather than minutes: `suite.select` resolves
    to the board bucket, so it runs the gates and the card tests and nothing else —
    measured in the origin project the day this was written, 14 s for 44 gates and
    27 tests. And it is the only thing standing between a malformed card and the
    branch every other machine pulls, which matters more here than anywhere else:
    this pass has just *written* cards, and `card_schema` is one of those gates.

    Every failure is reported and swallowed, and the push is never forced. A
    rejected push means somebody else moved the branch, which is a reconciliation
    for a human — and a pass that has already routed the lane must not report
    failure over the last hop.
    """
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not branch or branch == "HEAD":
        print("  publish: detached HEAD - nothing to push from")
        return False
    if _git(root, "rev-parse", "--abbrev-ref", "@{u}").returncode != 0:
        print(f"  publish: `{branch}` tracks no remote branch - the commits stay here")
        return False
    ahead = _git(root, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
    if ahead in ("", "0"):
        return False                       # level with the remote: nothing worth a line
    if stray := not_board(root, unpushed(root)):
        print(f"  ! publish: not pushing - `{branch}` also carries `{stray}`, which is "
              f"not board content. Preflight it and push it yourself.")
        return False
    remote = _git(root, "config", "--get", f"branch.{branch}.remote").stdout.strip()
    print(f"  publishing {ahead} board commit(s) to {remote}/{branch} - preflight first")
    # Uncaptured on purpose: the preflight's own report is the record, and it lands
    # in this run's log, which is what the panel is already showing.
    check = subprocess.run([sys.executable, "-m", "nightshift.preflight",
                            "--no-corrections", NO_LESSON], cwd=root, check=False)
    if check.returncode != 0:
        print("  ! publish: the preflight refused, above - not pushing")
        return False
    pushed = _git(root, "push", remote, branch)
    if pushed.returncode != 0:
        said = (pushed.stderr or pushed.stdout or "").strip().splitlines()
        print(f"  ! publish: `{branch}` was not pushed to {remote} - "
              f"{said[-1][:150] if said else 'see git output'}")
        return False
    print(f"  pushed {ahead} commit(s) to {remote}/{branch}")
    return True


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
    parser.add_argument("--no-push", action="store_true",
                        help="leave the board commits this pass makes unpublished. It "
                             "otherwise ends by pushing them, after a preflight, when "
                             "everything unpushed on the branch is board content")
    args = parser.parse_args(argv)

    root = args.root or (repo_root() if args.root is None else args.root)
    try:
        manifest.find_root(root)
    except Exception:
        pass                                  # a repo without a manifest still has a lane

    if args.only:
        code = _write_one(root, args.only, allow_paid=args.allow_paid)
    elif args.write_cards:
        code = _write_recorded(root, allow_paid=args.allow_paid)
    else:
        code = _classify_pass(root, args)

    # **The last step of a pass, not the tail of one branch of it.** Every exit
    # above can have committed: the classify path commits its routing, `--only`
    # and `--write-cards` commit a card each, and a pass that ends non-zero has
    # usually committed the most -- so the push is asked once, here, and about
    # the branch rather than about this run's own commits. That also carries out
    # a previous pass's commit that never made it, which is the case that
    # prompted this (Karel, 2026-08-21: "This should push automatically as part
    # of the ingest"). A dry run publishes nothing, because it touches nothing.
    if not (args.no_push or args.dry_run):
        publish(root)
    return code


def _classify_pass(root: Path, args: argparse.Namespace) -> int:
    """One pass over the unrouted notes: report, spend, apply the routing, write the view.

    Lifted out of `main` when the pass grew a step *after* it. It has nine exits,
    several of them refusals, and repeating the same tail at each one is how one of
    them silently comes to lack it.
    """
    found = notes(root)
    pending = unrouted(found)
    print(f"ingest: {len(found)} note(s) in {board.board_rel(root)}/inbox, "
          f"{len(pending)} unrouted")
    if not found:
        print("  nothing to route")
        return 0

    snapshot = usage.read()
    for line in usage.describe(snapshot):
        print(f"  {line}")

    if args.dry_run:
        print()
        for note in found:
            print(f"  {note.name} ({note.size} B)"
                  + (f" - already routed {note.route}" if note.route else ""))
        print("\n(dry run - nothing dispatched)")
        return 0

    # **The pass is over the notes with no answer yet, not over the lane.** A note
    # keeps its `route:`, so re-running this command costs one dispatch for what has
    # arrived since and nothing for what has not changed. When nothing has arrived it
    # costs nothing at all, which is what makes it safe to press the button twice.
    if not pending:
        print("  every note already carries a route - nothing to classify")
        print(f"  clear `route:` on a note to put it back in the queue")
        return 0

    if not _guard(args.allow_paid, "classifying").allow:
        return 3

    # Headless `-p` has no trust dialog, so an untrusted workspace makes every dispatch
    # fail with no useful message. Same precondition the runner establishes.
    ensure_workspace_trusted(root)

    print(f"  classifying with {args.model} ...")
    routing = classify(pending, root, model=args.model)

    # The deterministic half, before the view is written: it moves inline notes out
    # of the lane, so a report generated first would describe an inbox that no longer
    # exists. `found` is re-read for the same reason.
    applied = apply_routing(root, routing, pending)
    if applied.carded:
        print(f"  carded {len(applied.carded)} inline note(s) into tasks/: "
              f"{', '.join(applied.carded)}")
    if applied.stamped:
        print(f"  stamped {len(applied.stamped)} note(s) with their route")

    # **One line per note, because a tally is not a roster.** The pass used to
    # print only its per-route counts, so the panel could say "8 note(s) — 2
    # chore · 2 inline · 0 scribe · 4 triage" and nothing at all about *which*
    # note went where; the answer existed only on the Inbox page, one navigation
    # away from the run you were watching. Karel, 2026-08-21: *"I would prefer
    # one row for each card, stating the status, the name of the card and how it
    # was classified."* These lines are that roster's source — `parse_progress`
    # reads them back and the panel draws them, the same way the carding fan-out's
    # per-note lines have always worked. The counts still follow: they are the
    # summary of these, not a substitute for them.
    decided = {d.note: d for d in routing.decisions}
    could_not = set(applied.failed)
    for note in pending:
        decision = decided.get(note.name)
        if decision is None:
            continue          # the classifier said nothing about it; nothing to report
        if note.name in could_not:
            detail = "! could not be applied"
        else:
            why = " ".join((decision.why or "").split())
            detail = "carded into tasks/" if decision.route == "inline" else ""
            if why:
                detail = f"{detail}; {why}" if detail else why
        print(f"  routed: {note.name} -> {decision.route}"
              + (f" - {detail[:120]}" if detail else ""))

    found = notes(root)
    routing = Routing(decisions=recorded(root, found), error=routing.error)

    now = dt.datetime.now()
    textio.write_text_lf(root / OUT, report(routing, found, snapshot, now))
    print(f"  wrote {OUT}")
    # The view is a committed artefact — that is what being in `GENERATED_VIEWS`
    # means, and it is why the dispatch dirty-check exempts it. Writing it and
    # not committing it left the report Karel reads sitting modified for hours on
    # 2026-08-17, indistinguishable from an edit somebody had made by hand.
    _commit(root, f"routed {len(pending)} note(s) in {board.board_rel(root)}/inbox"
                  + (f", carded {len(applied.carded)} inline" if applied.carded else ""))

    if routing.error:
        print(f"  classification failed - {routing.error}")
        return 1

    # `inline` is counted from what was carded rather than from the view: those notes
    # left the lane in `apply_routing`, so the view — which describes the lane — has
    # nothing to say about them, and reporting a truthful zero would read as though
    # the classifier had routed none.
    for route in ROUTES:
        count = len(applied.carded) if route == "inline" else len(routing.by_route(route))
        print(f"    {route:8} {count}")
    if applied.failed:
        print(f"  ! {len(applied.failed)} note(s) could not be moved: "
              f"{', '.join(applied.failed)}")
        return 1

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
    found = notes(root)
    have = recorded(root, found)
    # An empty lane is not a missing routing pass. The two used to be one branch,
    # because the check was "has a pass ever been recorded" — and a lane whose notes
    # have all become cards is the *success* state of this command, not a state that
    # should send anyone back to classify.
    if not found:
        print(f"ingest: {board.board_rel(root)}/inbox is empty - nothing still in the "
              f"lane to card")
        return 0
    if not have:
        print("ingest: no note in the lane carries a route - classify the inbox first "
              "(`python -m nightshift.ingest`)")
        return 2
    # A note whose `route:` was set by hand — in Obsidian, or by the panel's re-route
    # — has never been through the deterministic step, so an `inline` one is still
    # sitting in the lane. Applying here as well as after a classify pass means both
    # buttons converge on the same board state instead of one of them leaving work
    # stranded in a group labelled "carded on the next pass".
    applied = apply_routing(root, Routing(decisions=have), found)
    if applied.carded:
        print(f"  carded {len(applied.carded)} inline note(s) into tasks/: "
              f"{', '.join(applied.carded)}")
        # Committed here rather than left to the scribe's per-note commit below: a
        # lane whose only routed notes were inline has an empty `bucket`, so nothing
        # downstream runs and the moved files would sit in the working tree.
        _commit(root, f"carded {len(applied.carded)} inline note(s) into tasks/")
        found = notes(root)
        have = recorded(root, found)
    bucket = [d for route in WRITABLE_ROUTES for d in have if d.route == route]
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

    A note routed `triage` is refused rather than quietly scribed: it is never
    dispatched from here. `inline` cannot reach this path at all — such a note became
    a card the moment it was routed and is no longer in the lane.

    The route is read off the note, not out of the view. Same reason `unrouted` does:
    the note is the record, and the view is regenerated from it.
    """
    lane = board.board_dir(root) / "inbox"
    if not (lane / note).is_file():
        print(f"ingest: no note named {note!r} in {board.board_rel(root)}/inbox")
        return 2
    found = next(n for n in notes(root) if n.name == note)
    if not found.route:
        print(f"ingest: {note} has no routing yet - classify the inbox first "
              f"(`python -m nightshift.ingest`)")
        return 2
    view = read_view(root)
    prior = view.of(note)
    decision = prior if prior and prior.route == found.route else Decision(
        note=note, route=found.route, why="routed by an earlier pass")
    if decision.route not in WRITABLE_ROUTES:
        print(f"ingest: {note} is routed `{decision.route}`, and only "
              f"{' and '.join(WRITABLE_ROUTES)} can be written from here")
        if decision.route == "triage":
            print("  triage is the expensive route and is launched deliberately, "
                  "one note at a time, by a human")
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
