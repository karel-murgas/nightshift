"""The structured record of one runner invocation — the digest's data source.

Until 2026-07-30 the digest answered "what happened last night?" by diffing the
board's lanes against the previous `digest:` commit. That data source cannot see
a run at all, only where cards ended up, and the difference is not academic —
on the night of 2026-07-30 it produced a digest that said **"0 failed · Nothing
failed"** about a night in which three cards were dispatched, all three failed
on the same broken gate, the run aborted on `CONSECUTIVE_FAILURE_STOP`, and the
staleness sweep then burned two and a half hours across 58 docs returning zero
verdicts. Lane-diffing saw none of it: a failed attempt bounces the card back to
`tasks/`, which is the lane it started in, so the diff was empty. Worse, the
baseline is the last *digest* commit rather than the last *run*, so two days of
Karel's own interactive work got reported as things the run had finished.

So the run reports on itself. Each invocation of `runner.py` opens a record here
and writes its own events into it — every dispatch with its outcome, every card
skipped and why, the sweep's real yield, why it stopped — and the digest reads
the records instead of guessing from lane state. Three consequences worth
naming, because each fixes a specific failure of the old approach:

- **A failure is an event, not a lane.** `attempt 1 failed, will retry` is
  recorded as a failure even though the card never left `tasks/`.
- **Daytime work is absent by construction.** A card Karel moves at 15:00 is in
  nobody's record, so it cannot be reported as something a run did (his
  instruction, 2026-07-30). The board sections of the digest still see it —
  they are "what is waiting on you", not "what happened".
- **An abandoned run still testifies.** The record is flushed after every event,
  so the night that was killed mid-sweep and never reached its own digest is
  still there to be read by the next thing that looks.

**No LLM anywhere in here** — same rule as the gates and the digest
(`00_architecture.md` §12). Every field is something the runner observed.

Records live in `.ai/runs/records/`, which is gitignored and machine-local, like
the rest of `.ai/runs/`. The rendered `Digest.md` is the committed artefact; the
evidence behind it stays on the host that produced it.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Callable

from nightshift import textio  # LF-pinned writes (gate write_newline)

DIR = Path(".ai/runs/records")

# Records are small (a few KB) and only the recent ones are ever read, but
# nothing else would delete them — the same "nothing ever unlinks anything in
# `.ai/runs/`" hole `runner-prune-run-dirs` fixed for attempt directories. Thirty
# is comfortably more than the digest's window (records since the last digest,
# normally one or two) with room for a stretch of runs that never rendered one.
KEEP = 30

# Dispatch outcomes that mean "a human has to decide something", "this landed",
# and "this failed" respectively — the three questions the morning asks, mapped
# once here rather than re-derived by every reader. `limited`/`blocked` are in
# none of them on purpose: neither is a fact about the card (the attempt is given
# back), so neither belongs under Failed. They surface as the run's stop reason.
#
# A chore batch's *bounce* — the worker opening the code and reporting that the item
# was not a one-prompter after all — is the routing signal the batch exists to produce
# (`chores` module docstring), not a fact about the card being wrong, and counting it
# as a failure would report a working detector as a nightly breakage. It reaches a
# record as `parked`, not as a literal `"bounced"`: the worker came back parked and
# `settle` put the card in `needs-decision/` with its question, so a decision is what
# it is and `DECISION_OUTCOMES` is where it belongs. `chores._RECORD_OUTCOME` carries
# that mapping and the story of getting it wrong; no producer writes `"bounced"` here.
#
# `needs_fix` (reviewer-needs-fix-verdict, 2026-08-19) is the same shape as
# `bounced` and is excluded for the same reason: in the ordinary case it also
# sends the card back to `tasks/` intact for a normal redispatch, so it is not a
# failure (gates+tests already passed) and not a decision (no human involved). It
# is recorded here as the bare string "needs_fix" every time, even the rarer case
# where `settle` escalates it to needs-decision/ after the card's attempts run
# out — `FAILED_OUTCOMES`'s own "failed" string has the identical retry-vs-retire
# ambiguity already and nothing distinguishes them there either. The board-lane
# section of the digest (not this file) is what actually shows a card now sitting
# in needs-decision/, regardless of how the dispatch that put it there was logged.
# `pick` is a decision even though the diff landed (`worktree.unadopted_artefacts`):
# the attempt produced candidates and installed none, so the card is in
# needs-decision/ waiting to be told which one. Reporting it as landed would put
# it in the digest’s "these are done" list, which is the misreport the outcome was
# introduced to stop.
DECISION_OUTCOMES = frozenset({"parked", "needs_decision", "pick"})
LANDED_OUTCOMES = frozenset({"review", "reviewed"})
FAILED_OUTCOMES = frozenset({"failed"})

#: Bumped to 2 on 2026-09-16, when `chores._RECORD_OUTCOME` stopped passing the
#: batch's internal state names through: a chore worker's park used to be written
#: `"bounced"` (in none of the three sets above) and a `_drop` `"parked"`, which is
#: each on the wrong side of `decisions()`/`failures()`. Records already on disk
#: keep the old spelling — nothing rewrites them — so anything that has to compare
#: a night before the fix with one after it reads them through `_outcome_of` below.
#: A record with no field at all is vocabulary 1, which is what every existing one is.
OUTCOME_VOCABULARY = 2

#: How vocabulary-1 chore records spell the two outcomes this changed. Only chore
#: records need it: the runner always wrote `parked` for a park and never wrote
#: `bounced` at all, so its old records are already right.
_LEGACY_CHORE_OUTCOMES = {"bounced": "parked", "parked": "failed"}


def _outcome_of(record: dict, entry: dict) -> str:
    """One dispatch's outcome in today's vocabulary, whatever the record's own is."""
    outcome = entry.get("outcome") or ""
    if record.get("kind") == "chores" and record.get("outcome_vocabulary", 1) < 2:
        return _LEGACY_CHORE_OUTCOMES.get(outcome, outcome)
    return outcome


def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _stamp(iso: str) -> str:
    """A filename-safe form of an ISO timestamp: `2026-07-30T02:14:08` →
    `20260730-021408`. Sorts lexicographically in time order, which is what
    `read_all` relies on instead of reading every file to sort them."""
    return iso.replace("-", "").replace(":", "").replace("T", "-")


class Record:
    """One run's record, flushed to disk after every event.

    A full overwrite per event rather than an append log, for the same reason
    `hostconfig._status` overwrites `status.json`: the file is tiny, the caller
    already holds the whole truth in memory, and a torn write costs one run's
    record rather than corrupting a stream. Every write is wrapped — a record
    that cannot be saved must never be the thing that ends a night. It is an
    observer, and an observer that can kill the run is worse than none.
    """

    def __init__(self, root: Path, path: Path, data: dict) -> None:
        self.root = root
        self.path = path
        self.data = data

    # --- events ------------------------------------------------------------

    def dispatched(self, card_id: str, *, worker: str, model: str, attempt: int,
                   title: str = "", outcome: str = "", detail: str = "",
                   cost_usd: float = 0.0, landed: str = "", evidence: str = "") -> None:
        """One dispatch and how it ended.

        `landed` is `settle`'s own one-line account (`→ testing/ (reviewed ok…)`,
        `attempt 1 failed, will retry (…)`), kept verbatim because it is the only
        place the *consequence* is stated — the outcome says `failed`, the landing
        says whether that meant a retry or `failed/`.

        `evidence` is the bounded block behind `detail` on a failure — which tests
        failed and on what assertion (`suite.failure_excerpt`). It is here as well
        as on the card because the two artefacts answer different questions and
        reach the maintainer by different routes: the card is the one place to look
        when working *that* card, and `Digest.md` is what gets read in the morning
        without opening anything. Both are committed, which is the point — the
        run-log copy is gitignored and cannot be read from another machine.
        """
        self.data.setdefault("dispatched", []).append({
            "card": card_id, "title": title, "worker": worker, "model": model,
            "attempt": attempt, "outcome": outcome, "detail": detail,
            "cost_usd": round(cost_usd, 2), "landed": landed,
            "evidence": evidence, "at": _now(),
        })
        self.save()

    def set_dispatched(self, entries: list[dict]) -> None:
        """Replace the whole dispatch list, for a producer whose outcomes *change*.

        `dispatched()` appends, which is right for the runner: a card settles once
        and what it settled to is final the moment it is known. A chore batch is not
        like that. An item is green at the end of phase 1, and phase 2 can still take
        it back out — `chores._drop` re-parks an item whose branch would not merge or
        whose commit reddened the batch suite. Appending a second entry for the same
        card would leave the record saying a chore both landed and was dropped, with
        nothing to say which is current; `chores` therefore holds its `Batch` as the
        truth and re-states the whole list here whenever it changes.

        Still flushed on every call, so the property that matters is unchanged: a
        batch killed mid-phase leaves a record of everything settled up to that point.
        """
        self.data["dispatched"] = list(entries)
        self.save()

    def usage(self, stage: str, *, card_id: str, model: str = "", effort: str = "",
             calls: int = 1, turns: int = 0, wall_s: float = 0.0, api_s: float = 0.0,
             cost_usd: float = 0.0, input_tokens: int = 0, cache_read_tokens: int = 0,
             cache_write_tokens: int = 0, output_tokens: int = 0,
             denials: int = 0) -> None:
        """What one pipeline stage spent on one card — worker, checker, reviewer,
        a chore batch's own reviewer pass, repair, or a merge conflict resolver.

        Until this existed, `dispatched()`'s single `cost_usd` was the only number
        that survived a run: everything else the CLI reports about a call —
        turns, wall/API time, the four token counters, which model actually did
        the work — was downloaded, parsed once for its dollar figure, and
        discarded (`token-economy.md` §1). `telemetry.usage_breakdown()` is the
        reader that recovers the rest from what the stage already wrote to
        `.ai/runs/`; this is where it lands so a report can compare stages and
        cards without re-parsing a transcript.

        `calls` is how many rounds this stage made on this card — a
        producer/checker loop can run several — summed into one event rather
        than reported per round, at the same grain the plan's own cost table
        already uses: nobody comparing workers to reviewers wants to see round 2
        of 3 as its own line.

        `effort` is empty until a caller actually varies it (`token-economy.md`
        phase 3.3); the field exists now so recording does not have to change
        shape again once it does.
        """
        self.data.setdefault("usage", []).append({
            "stage": stage, "card": card_id, "model": model, "effort": effort,
            "calls": calls, "turns": turns, "wall_s": round(wall_s, 1),
            "api_s": round(api_s, 1), "cost_usd": round(cost_usd, 4),
            "input_tokens": input_tokens, "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens, "output_tokens": output_tokens,
            "denials": denials, "at": _now(),
        })
        self.save()

    def skipped(self, entries: list[tuple[str, str]]) -> None:
        """The cards `select()` would not dispatch, as `(card_id, reason)`.

        Recorded in one call from the selection loop rather than accumulated,
        because selection happens once per run and the whole list is known then.
        The digest groups these by reason: five art cards blocked on
        `requires: gpu-box` is one fact about the host, not five about the cards.
        """
        self.data["skipped"] = [{"card": cid, "reason": reason} for cid, reason in entries]
        self.save()

    def planned(self, entries: list[tuple[str, str, str]]) -> None:
        """Every card this run intends to work, as `(card_id, title, queue)`, in the
        order it will take them.

        **Written before the first dispatch, which is the whole point.** Everything
        else in a record is written as it happens, so until the run was well under
        way the panel had nothing to show but an empty roster and had to reconstruct
        "what is still coming" by re-reading `tasks/` — a live board read that knew
        nothing of the run's own order, and nothing of the chore batch at all, since
        chores are not in the night's candidate list. A run that works both queues
        has one roster, and this is it.

        `queue` is the card's type as the page shows it (`chores` / `tasks`), taken
        from the `runner.Queue` that holds it rather than from the card's own
        `kind:`, because the queue is what actually decided how it would be worked.

        Appended rather than replaced: `--queue both` builds its queues one at a
        time and each announces itself as it is built.
        """
        self.data.setdefault("planned", []).extend(
            {"card": cid, "title": title, "queue": queue} for cid, title, queue in entries)
        self.save()

    def oversized(self, entries: list[tuple[str, int, int]]) -> None:
        """Cards that were **dispatched** while over `dispatch.CARD_COMFORT_BYTES`,
        as `(card_id, bytes, threshold)`.

        A field of its own, and the separation is the whole reason this exists.
        An oversized card is dispatchable by design — that is the choice made on
        `oversized-cards-are-bad-worker-input`, advisory over a hard stop — so it
        is absent from `skipped` by construction, and the case the signal exists
        for (a card dispatched again and again while it grows) was therefore the
        one case `Digest.md` stayed silent about. Folding it into `skipped`
        instead would have made the digest's own `### Skipped — N` heading and
        its *"Every card on the board was dispatchable"* fallback say something
        untrue about a card that ran.

        Numbers, not the sentence the run log prints. The record holds what the
        runner observed and the digest decides how it reads in the morning — and
        the two surfaces phrase it differently on purpose: one line per card in
        the log, N cards under one shared remedy on the panel.
        """
        self.data["oversized"] = [{"card": cid, "bytes": size, "threshold": limit}
                                  for cid, size, limit in entries]
        self.save()

    def stale(self, *, selected: int, checked: int, verified: int, carded: int,
              incomplete: int = 0, cards: list[str] | None = None) -> None:
        """The sweep's real yield, and since `.ai/stale_status.json` went with the
        digest, the only record of it. `selected` and `incomplete` are the two that
        status file never carried, and they are the two that distinguish "swept,
        nothing had drifted" from "swept 58 docs and every single verdict came back
        unusable" — which is what actually happened on 2026-07-30 and which the
        digest reported as silence."""
        self.data["stale"] = {
            "selected": selected, "checked": checked, "verified": verified,
            "carded": carded, "incomplete": incomplete, "cards": cards or [],
        }
        self.save()

    def note(self, message: str) -> None:
        """A run-level event that is not a dispatch — a wall waited out, a
        recovery, a publish refusal. Kept as flat prose: these are read, not
        counted, and inventing a taxonomy for them would be a schema to keep in
        sync with `_log` call sites for no reader's benefit."""
        self.data.setdefault("notes", []).append({"at": _now(), "message": message})
        self.save()

    def stop(self, reason: str) -> None:
        """Why the loop ended. Set once — the first reason is the real one; any
        later `break` is a consequence of it."""
        if not self.data.get("stop_reason"):
            self.data["stop_reason"] = reason
            self.save()

    def finish(self, *, cost_usd: float = 0.0, walls: int = 0,
               dispatched: int = 0) -> None:
        """Close the record. `complete` is the flag that separates a run that
        reached its own end from one that was killed — the 02:14 night was
        killed mid-sweep, wrote no digest, and would otherwise be
        indistinguishable from a run that simply had nothing to do."""
        self.data.update({"finished": _now(), "complete": True,
                          "cost_usd": round(cost_usd, 2), "walls": walls,
                          "cards_dispatched": dispatched})
        self.save()

    # --- persistence -------------------------------------------------------

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            textio.write_text_lf(self.path, json.dumps(self.data, indent=2, ensure_ascii=False))
        except (OSError, ValueError):
            pass


class _NullRecord(Record):
    """What a dry run gets: every event method is a no-op.

    `--dry-run` promises "no LLM, no writes", and a record file is a write. The
    alternative — `if record is not None` at every call site — is the shape that
    grows a missing guard on the one path nobody tested.
    """

    def __init__(self) -> None:
        super().__init__(Path("."), Path(os.devnull), {})

    def save(self) -> None:
        pass


def start(root: Path, *, kind: str, label: str = "", host: str = "") -> Record:
    """Open a record for a run starting now."""
    started = _now()
    path = root / DIR / f"{_stamp(started)}.json"
    record = Record(root, path, {
        "started": started, "finished": None, "complete": False,
        "kind": kind, "label": label, "host": host,
        "stop_reason": None, "cost_usd": 0.0, "walls": 0, "cards_dispatched": 0,
        "dispatched": [], "skipped": [], "oversized": [], "notes": [], "usage": [],
        "outcome_vocabulary": OUTCOME_VOCABULARY,
    })
    record.save()
    prune(root)
    return record


def null() -> Record:
    """The no-op record for a dry run."""
    return _NullRecord()


def prune(root: Path, keep: int = KEEP) -> None:
    """Drop all but the newest `keep` records. Newest by filename, which sorts
    in time order by construction (`_stamp`) — no need to open any of them."""
    try:
        files = sorted((root / DIR).glob("*.json"))
    except OSError:
        return
    for path in files[:-keep] if len(files) > keep else []:
        try:
            path.unlink()
        except OSError:
            pass


def read_all(root: Path) -> list[dict]:
    """Every readable record, newest first.

    A record that will not parse is skipped rather than raised on: it is one
    run's evidence, the digest's job is to report the rest, and a half-written
    file from a run killed mid-`save` is a thing that will happen.
    """
    try:
        files = sorted((root / DIR).glob("*.json"), reverse=True)
    except OSError:
        return []
    out: list[dict] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("started"):
            out.append(data)
    return out



def failures(record: dict) -> list[dict]:
    return [d for d in record.get("dispatched", [])
            if d.get("outcome") in FAILED_OUTCOMES]


def decisions(record: dict) -> list[dict]:
    return [d for d in record.get("dispatched", [])
            if d.get("outcome") in DECISION_OUTCOMES]


def landed(record: dict) -> list[dict]:
    return [d for d in record.get("dispatched", [])
            if d.get("outcome") in LANDED_OUTCOMES]


def window(record: dict) -> str:
    """`02:14–05:57` — the clock span, or `02:14–?` for a run that never
    finished. Times, not dates: the record is read the morning after."""
    start = str(record.get("started", ""))[11:16]
    end = str(record.get("finished") or "")[11:16]
    return f"{start}–{end}" if end else f"{start}–?"


def day(record: dict) -> str:
    return str(record.get("started", ""))[:10]


# --- testing/ rejections ------------------------------------------------------
#
# A human's play-through catches what gates, tests and the diff reviewer could
# not (`03_board.md`, `boardcmd.mark_rejected`'s own docstring on why that gate
# exists at all) — and that event happens outside any run, at whatever hour
# Karel gets to `testing/`. It cannot be a `dispatched()` entry because no
# dispatch produced it, so it gets a log of its own instead: one line per
# rejection, appended by `boardcmd.mark_rejected`, read back here as a rate.

REJECTIONS_LOG = Path(".ai/runs/testing_rejections.log")


def record_rejection(root: Path, card_id: str) -> None:
    """One card sent back from `testing/` to `tasks/` after failing a play-through.

    Best-effort, like every write in this module (`Record.save`'s docstring):
    a rejection that failed to log must not stop the rejection itself from
    landing, which is `boardcmd.mark_rejected`'s actual job.
    """
    path = root / REJECTIONS_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{_now()}\t{card_id}\n")
    except OSError:
        pass


def _read_log_lines(path: Path) -> list[str]:
    try:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def count_rejections(root: Path, *, since: str = "") -> int:
    """How many `testing/` rejections landed at or after `since` (an ISO stamp,
    `""` for all time)."""
    count = 0
    for line in _read_log_lines(root / REJECTIONS_LOG):
        stamp, _, _rest = line.partition("\t")
        if not since or stamp >= since:
            count += 1
    return count


# --- quality counters ---------------------------------------------------------
#
# The tripwire for every phase after this one (`token-economy.md` §2, phase 0.3):
# "any item that moves these the wrong way gets reverted." Computed from records
# already on disk — no new dispatch, no new LLM call — over whatever window the
# caller names, so a before/after comparison across a token-economy change is a
# read, not a re-run.

#: Substrings the merge/rebase landing path writes into a failure's own `why`
#: when the *post-merge* re-verification (not the pre-merge one) is what caught
#: it (`runner.py` lines near "after merging into"/"after rebasing onto") —
#: the two re-verify call sites that exist specifically to stop something red
#: from ever reaching `test`. A card carrying either phrase is evidence the
#: guard fired, not evidence it failed to: this counts how often it *has* to,
#: which should stay at zero as the surrounding phases change what a worker
#: verifies for itself before handing a diff over.
_RED_AFTER_MERGE_MARKERS = ("after merging into", "after rebasing onto")


def _is_red_after_merge(entry: dict) -> bool:
    text = f"{entry.get('detail', '')} {entry.get('landed', '')}"
    return any(marker in text for marker in _RED_AFTER_MERGE_MARKERS)


def quality_counters(records: list[dict], *, root: Path | None = None,
                     since: str = "") -> dict:
    """Rates a token-economy change must not move the wrong way.

    `records` is normally `read_all(root)`, newest first, already on disk —
    this is arithmetic over what the runner and the chore batch already wrote,
    the same "no LLM anywhere in here" rule the record itself follows (module
    docstring). `root`/`since` are only needed for `testing_rejections`, which
    lives in its own log rather than in any one record (see above); omitting
    `root` reports it as `0` rather than raising, so a caller with only records
    in hand (a test, a unit report) still gets the other four rates.

    Every rate is `0.0` on an empty denominator rather than `NaN` or a raised
    `ZeroDivisionError` — "no dispatches in this window" is itself the answer a
    reader needs, and a crash here must not be how they find that out.
    """
    # Paired with its record, not flattened: a vocabulary-1 chore record spells two
    # of these outcomes differently, and `_outcome_of` needs the record to know that.
    # Comparing a pre-fix night with a post-fix one is the whole point of these
    # counters, so reading the old spelling correctly is not optional here.
    # Each dispatch paired with its outcome *in today's vocabulary*: a vocabulary-1
    # chore record spells two of these differently, and comparing a pre-fix night
    # with a post-fix one is the whole point of these counters, so reading the old
    # spelling correctly is not optional here.
    dispatched = [(_outcome_of(r, d), d)
                  for r in records for d in r.get("dispatched", [])]
    total = len(dispatched)

    def _rate(pred: Callable[[str, dict], bool]) -> float:
        return round(sum(1 for o, d in dispatched if pred(o, d)) / total, 3) if total else 0.0

    def _is(want: str) -> Callable[[str, dict], bool]:
        return lambda outcome, _entry: outcome == want

    return {
        "dispatched": total,
        "needs_fix_rate": _rate(_is("needs_fix")),
        # Not `DECISION_OUTCOMES`: that set also carries `parked` and `pick`
        # (`run_record.decisions()`'s own grouping), but the plan names these as
        # three separate rates (`token-economy.md` phase 0.3) — a `parked` card
        # is not a card the reviewer sent to Karel, and folding it in here would
        # double-count it against `parked_rate` below.
        "needs_decision_rate": _rate(_is("needs_decision")),
        "parked_rate": _rate(_is("parked")),
        "red_after_merge_rate": _rate(lambda _outcome, entry: _is_red_after_merge(entry)),
        "testing_rejections": count_rejections(root, since=since) if root else 0,
    }
