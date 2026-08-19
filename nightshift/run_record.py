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
  still there to be reported by the *next* run's digest.

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
# `bounced` is a chore batch's addition and is in none of them for the same reason,
# spelled out because it is the one an unwary reader would "fix" into Failed: a
# bounce is the worker opening the code and reporting that the item was not a
# one-prompter after all. That is the routing signal the batch exists to produce
# (`chores` module docstring), not a fact about the card being wrong — the card
# goes back to `tasks/` intact, to be dispatched normally. Counting it as a failure
# would report a working detector as a nightly breakage.
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
DECISION_OUTCOMES = frozenset({"parked", "needs_decision"})
LANDED_OUTCOMES = frozenset({"review", "reviewed"})
FAILED_OUTCOMES = frozenset({"failed"})


def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat()


def _stamp(iso: str) -> str:
    """A filename-safe form of an ISO timestamp: `2026-07-30T02:14:08` →
    `20260730-021408`. Sorts lexicographically in time order, which is what
    `records_since` relies on instead of reading every file to sort them."""
    return iso.replace("-", "").replace(":", "").replace("T", "-")


class Record:
    """One run's record, flushed to disk after every event.

    A full overwrite per event rather than an append log, for the same reason
    `runner._status` overwrites `status.json`: the file is tiny, the caller
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

    def skipped(self, entries: list[tuple[str, str]]) -> None:
        """The cards `select()` would not dispatch, as `(card_id, reason)`.

        Recorded in one call from the selection loop rather than accumulated,
        because selection happens once per run and the whole list is known then.
        The digest groups these by reason: five art cards blocked on
        `requires: gpu-box` is one fact about the host, not five about the cards.
        """
        self.data["skipped"] = [{"card": cid, "reason": reason} for cid, reason in entries]
        self.save()

    def oversized(self, entries: list[tuple[str, int, int]]) -> None:
        """Cards that were **dispatched** while over `runner.CARD_COMFORT_BYTES`,
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
        the log, N cards under one shared remedy in the digest.
        """
        self.data["oversized"] = [{"card": cid, "bytes": size, "threshold": limit}
                                  for cid, size, limit in entries]
        self.save()

    def stale(self, *, selected: int, checked: int, verified: int, carded: int,
              incomplete: int = 0, cards: list[str] | None = None) -> None:
        """The sweep's real yield. `selected` and `incomplete` are the two the
        old `stale_status.json` never carried, and they are the two that
        distinguish "swept, nothing had drifted" from "swept 58 docs and every
        single verdict came back unusable" — which is what actually happened on
        2026-07-30 and which the digest reported as silence."""
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
        "dispatched": [], "skipped": [], "oversized": [], "notes": [],
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


def records_since(root: Path, since: str | None) -> list[dict]:
    """Records that started after `since` (an ISO timestamp), newest first.

    `since` is the previous digest commit's timestamp, so the window is exactly
    "runs Karel has not been shown yet" — which is normally one, and was two on
    2026-07-30 (an aborted night plus a manual midday run) precisely because the
    aborted one never rendered a digest of its own. `None` (no digest has ever
    been committed) returns everything.

    Derived from the commit rather than from a marker file the digest writes, so
    that re-rendering the digest is idempotent: running the digest twice in a row
    must not make the second one claim nothing happened.
    """
    records = read_all(root)
    if since is None:
        return records
    return [r for r in records if str(r.get("started", "")) > since]


# --- reading one record ------------------------------------------------------
#
# Small accessors rather than the digest reaching into raw dicts. The point is
# that the *record* owns what "failed" means, so a new outcome string added in
# `runner.Dispatch` is classified in one place instead of in every report.


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
