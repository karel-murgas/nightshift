"""Tests for `nightshift/run_record.py` — the run's own account of itself.

The record exists because board state cannot describe a run (see the module
docstring). What has to be true of it:

- it survives a run that dies, because the run that dies is the one worth
  reporting — the 2026-07-30 night was killed mid-sweep and reported nothing;
- it never takes a run down with it, so every write is best-effort;
- its window is "runs Karel has not been shown", derived idempotently.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from nightshift import run_record


def _files(root: Path) -> list[Path]:
    return sorted((root / run_record.DIR).glob("*.json"))


# --- it is on disk before anything else happens -----------------------------

def test_a_record_exists_from_the_moment_the_run_starts(tmp_path):
    """Not written at the end: a run that never reaches its end is exactly the
    run whose record matters."""
    run_record.start(tmp_path, kind="run", label="up to 8 card(s)")
    assert len(_files(tmp_path)) == 1
    data = json.loads(_files(tmp_path)[0].read_text(encoding="utf-8"))
    assert data["complete"] is False
    assert data["finished"] is None
    assert data["label"] == "up to 8 card(s)"


def test_each_event_is_flushed_rather_than_buffered(tmp_path):
    record = run_record.start(tmp_path, kind="run")
    record.dispatched("probe", worker="code-thread", model="sonnet", attempt=1,
                      outcome="failed", detail="gates: boom",
                      landed="probe: attempt 1 failed, will retry")
    on_disk = json.loads(record.path.read_text(encoding="utf-8"))
    assert on_disk["dispatched"][0]["card"] == "probe"
    assert on_disk["complete"] is False   # still not finished, and still readable


def test_an_unfinished_record_reads_as_killed(tmp_path):
    """The property a reader's `killed` verdict depends on."""
    record = run_record.start(tmp_path, kind="run")
    record.dispatched("probe", worker="code-thread", model="sonnet", attempt=1,
                      outcome="failed")
    data = run_record.read_all(tmp_path)[0]
    assert data["complete"] is False
    assert run_record.window(data).endswith("–?")


def test_finish_records_the_totals(tmp_path):
    record = run_record.start(tmp_path, kind="run")
    record.finish(cost_usd=4.394, walls=1, dispatched=3)
    data = run_record.read_all(tmp_path)[0]
    assert data["complete"] is True
    assert data["cost_usd"] == 4.39      # rounded for display, not carried at float noise
    assert data["walls"] == 1 and data["cards_dispatched"] == 3


# --- outcome classification lives here, not in the report -------------------

def test_outcomes_are_classified_once_for_every_reader(tmp_path):
    record = run_record.start(tmp_path, kind="run")
    for card, outcome in (("a", "failed"), ("b", "reviewed"), ("c", "review"),
                          ("d", "parked"), ("e", "needs_decision"),
                          ("f", "limited"), ("g", "blocked")):
        record.dispatched(card, worker="code-thread", model="m", attempt=1, outcome=outcome)
    data = run_record.read_all(tmp_path)[0]
    assert [d["card"] for d in run_record.failures(data)] == ["a"]
    assert [d["card"] for d in run_record.landed(data)] == ["b", "c"]
    assert [d["card"] for d in run_record.decisions(data)] == ["d", "e"]


def test_a_usage_wall_is_neither_a_failure_nor_a_landing(tmp_path):
    """`limited` and `blocked` give the attempt back — neither is a fact about the
    card, so classifying either as a failure would blame a card for the plan
    running out."""
    record = run_record.start(tmp_path, kind="run")
    record.dispatched("probe", worker="code-thread", model="m", attempt=1, outcome="limited")
    data = run_record.read_all(tmp_path)[0]
    assert not run_record.failures(data)
    assert not run_record.landed(data)
    assert not run_record.decisions(data)


# --- the stop reason is set once ---------------------------------------------

def test_the_first_stop_reason_wins(tmp_path):
    """A later `break` is a consequence of the first one, not a competing
    explanation."""
    record = run_record.start(tmp_path, kind="run")
    record.stop("3 dispatches failed in a row")
    record.stop("past 06:00")
    assert run_record.read_all(tmp_path)[0]["stop_reason"] == "3 dispatches failed in a row"


# --- the sweep's real yield --------------------------------------------------

def test_the_sweep_records_selected_and_incomplete_not_just_the_old_triple(tmp_path):
    """`selected` against `verified` is what distinguishes a quiet sweep from a
    broken one; the three-number status file could express neither."""
    record = run_record.start(tmp_path, kind="run")
    record.stale(selected=58, checked=27, verified=0, carded=0, incomplete=27)
    stale = run_record.read_all(tmp_path)[0]["stale"]
    assert stale["selected"] == 58 and stale["incomplete"] == 27
    assert stale["verified"] == 0



# --- it must never be the thing that ends a night ---------------------------

def test_a_half_written_record_is_skipped_not_raised_on(tmp_path):
    run_record.start(tmp_path, kind="run")
    bad = tmp_path / run_record.DIR / "20260101-000000.json"
    bad.write_text('{"started": "2026-01-01T00:00:00", "dispa', encoding="utf-8")
    assert len(run_record.read_all(tmp_path)) == 1


def test_a_record_that_is_not_a_dict_is_skipped(tmp_path):
    (tmp_path / run_record.DIR).mkdir(parents=True)
    (tmp_path / run_record.DIR / "20260101-000000.json").write_text("[1, 2]", encoding="utf-8")
    assert run_record.read_all(tmp_path) == []


def test_an_unwritable_record_does_not_raise(tmp_path, monkeypatch):
    """The record is an observer, and an observer that can kill the run is worse
    than none — the same rule `runner._status` and the log tee follow."""
    record = run_record.start(tmp_path, kind="run")

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    record.dispatched("probe", worker="w", model="m", attempt=1, outcome="failed")
    record.stale(selected=1, checked=1, verified=1, carded=0)
    record.finish(cost_usd=1.0)          # must not raise


# --- a card that ran while grown past useful worker input --------------------

def test_an_oversized_card_that_ran_is_recorded_apart_from_the_skipped_ones(tmp_path):
    """Round 2 of `oversized-cards-are-bad-worker-input`, and the separation is
    the fix. An oversized card is dispatchable by design, so it is never in
    `skipped` — which left `Digest.md` silent about exactly the case the signal
    exists for. Putting it in `skipped` instead would have made a reader's own
    `### Skipped — N` heading and its "Every card on the board was dispatchable"
    fallback describe a card that ran, so the two lists have to stay disjoint at
    the record, not just at the render."""
    record = run_record.start(tmp_path, kind="run")
    record.skipped([("parked", "unattended: false — declared as needing a human")])
    record.oversized([("fat", 16_309, 14 * 1024)])

    data = run_record.read_all(tmp_path)[0]
    assert data["oversized"] == [{"card": "fat", "bytes": 16_309, "threshold": 14 * 1024}]
    assert [s["card"] for s in data["skipped"]] == ["parked"]


def test_a_record_carries_the_field_before_any_card_is_measured(tmp_path):
    """`start()` seeds it like `skipped` and `dispatched`, so a run killed before
    selection has the same shape as one that finished — a reader reads records
    from runs it did not write, including older ones."""
    run_record.start(tmp_path, kind="run")
    assert run_record.read_all(tmp_path)[0]["oversized"] == []


def test_reading_a_root_with_no_records_directory_is_empty_not_an_error(tmp_path):
    assert run_record.read_all(tmp_path) == []


# --- the null record for a dry run -------------------------------------------

def test_a_dry_run_writes_nothing(tmp_path):
    """`--dry-run` promises "no LLM, no writes", and a record file is a write."""
    record = run_record.null()
    record.dispatched("probe", worker="w", model="m", attempt=1, outcome="failed")
    record.skipped([("a", "because")])
    record.oversized([("b", 20_000, 14 * 1024)])
    record.stale(selected=1, checked=1, verified=1, carded=0)
    record.stop("reason")
    record.finish(cost_usd=1.0)
    assert not (tmp_path / run_record.DIR).exists()


# --- retention ---------------------------------------------------------------

def test_old_records_are_pruned_so_nothing_grows_without_bound(tmp_path):
    """`.ai/runs/` had no unlink at all before `runner-prune-run-dirs`; this
    directory does not repeat that."""
    for i in range(run_record.KEEP + 5):
        stamp = (dt.datetime(2026, 1, 1) + dt.timedelta(hours=i)).isoformat(timespec="seconds")
        run_record.Record(tmp_path, tmp_path / run_record.DIR / f"{run_record._stamp(stamp)}.json",
                          {"started": stamp}).save()
    run_record.prune(tmp_path)
    kept = _files(tmp_path)
    assert len(kept) == run_record.KEEP
    # The newest are the ones kept — filenames sort in time order by construction.
    assert kept[-1].stem == run_record._stamp(
        (dt.datetime(2026, 1, 1) + dt.timedelta(hours=run_record.KEEP + 4)).isoformat(
            timespec="seconds"))


# --- usage: the per-stage breakdown behind `cost_usd` --------------------------

def test_usage_events_carry_the_full_breakdown_dispatched_never_did(tmp_path):
    """`dispatched()`'s own `cost_usd` is one float for the whole card; `usage()`
    is where the stage-by-stage numbers that float used to discard now land
    (`token-economy.md` phase 0.1)."""
    record = run_record.start(tmp_path, kind="run")
    record.usage("worker", card_id="probe", model="sonnet", turns=40,
                cost_usd=1.999, cache_read_tokens=4_000_000)
    data = run_record.read_all(tmp_path)[0]
    assert data["usage"][0]["stage"] == "worker"
    assert data["usage"][0]["card"] == "probe"
    assert data["usage"][0]["turns"] == 40
    assert data["usage"][0]["cost_usd"] == 1.999
    assert data["usage"][0]["cache_read_tokens"] == 4_000_000


def test_a_record_carries_the_usage_field_before_any_card_is_measured(tmp_path):
    run_record.start(tmp_path, kind="run")
    assert run_record.read_all(tmp_path)[0]["usage"] == []


# --- quality counters: the tripwire for every later phase -----------------------

def test_quality_counters_are_rates_over_dispatched_outcomes(tmp_path):
    record = run_record.start(tmp_path, kind="run")
    for card, outcome in (("a", "reviewed"), ("b", "needs_fix"), ("c", "parked"),
                          ("d", "needs_decision"), ("e", "reviewed")):
        record.dispatched(card, worker="w", model="m", attempt=1, outcome=outcome)
    counters = run_record.quality_counters(run_record.read_all(tmp_path))
    assert counters["dispatched"] == 5
    assert counters["needs_fix_rate"] == 0.2
    assert counters["needs_decision_rate"] == 0.2
    assert counters["parked_rate"] == 0.2


def test_quality_counters_read_a_pre_2026_09_16_chore_records_old_spelling(tmp_path):
    """Chore records written before `_RECORD_OUTCOME` was corrected spell a worker's
    park `"bounced"` and a `_drop` `"parked"` — each on the wrong side of the three
    sets. They are not rewritten, so the counters have to read them, or every
    comparison against a pre-fix baseline is wrong in exactly the direction that
    would hide a regression in the chore path (`token-economy.md` §5).
    """
    legacy = {"kind": "chores", "dispatched": [
        {"card": "a", "outcome": "reviewed"},
        {"card": "b", "outcome": "bounced"},   # the worker parked with a question
        {"card": "c", "outcome": "parked"},    # a drop: the card went to failed/
    ]}
    counters = run_record.quality_counters([legacy])
    assert counters["parked_rate"] == round(1 / 3, 3), "the bounce is the park"

    # The same shapes in a *runner* record were always right, and must not be
    # rewritten: there `parked` means parked and `bounced` was never written.
    straight = {"kind": "run", "dispatched": [{"card": "a", "outcome": "parked"}]}
    assert run_record.quality_counters([straight])["parked_rate"] == 1.0

    # Today's chores write the same night as vocabulary 2 — the park already spelled
    # `parked`, the drop already `failed` — and it is read as-is, to the same rates.
    current = {"kind": "chores",
               "outcome_vocabulary": run_record.OUTCOME_VOCABULARY,
               "dispatched": [{"card": "a", "outcome": "reviewed"},
                              {"card": "b", "outcome": "parked"},
                              {"card": "c", "outcome": "failed"}]}
    assert run_record.quality_counters([current])["parked_rate"] == round(1 / 3, 3)
    assert run_record.quality_counters([current])["dispatched"] == 3


def test_a_new_record_declares_the_outcome_vocabulary_it_was_written_in(tmp_path):
    run_record.start(tmp_path, kind="chores")
    record = run_record.read_all(tmp_path)[0]
    assert record["outcome_vocabulary"] == run_record.OUTCOME_VOCABULARY


def test_quality_counters_do_not_divide_by_zero_on_an_empty_window(tmp_path):
    counters = run_record.quality_counters([])
    assert counters["dispatched"] == 0
    assert counters["needs_fix_rate"] == 0.0


def test_red_after_merge_is_read_off_the_failure_wording_the_merge_re_verify_writes(tmp_path):
    """`runner.py`'s post-merge and post-rebase re-verification is what stops
    something red from ever reaching `test`; a card whose own failure detail
    carries that phrasing is evidence the guard fired, which is exactly what
    this counter is watching for a later phase to start letting through."""
    record = run_record.start(tmp_path, kind="run")
    record.dispatched("a", worker="w", model="m", attempt=1, outcome="failed",
                      detail="after merging into test, gates: boom")
    record.dispatched("b", worker="w", model="m", attempt=1, outcome="failed",
                      detail="worker exited 1")
    counters = run_record.quality_counters(run_record.read_all(tmp_path))
    assert counters["red_after_merge_rate"] == 0.5


def test_testing_rejections_are_read_from_their_own_log_not_a_record(tmp_path):
    """No dispatch produces this event (`boardcmd.mark_rejected`'s own
    docstring), so it cannot live in `dispatched` — it gets a log of its own."""
    run_record.record_rejection(tmp_path, "probe")
    run_record.record_rejection(tmp_path, "probe-2")
    assert run_record.count_rejections(tmp_path) == 2


def test_quality_counters_report_zero_rejections_without_a_root(tmp_path):
    """`root` is optional because a caller may have only records in hand (a
    test, a unit report); reporting `0` beats raising over a log this call was
    never given a path to."""
    counters = run_record.quality_counters([])
    assert counters["testing_rejections"] == 0


def test_a_stamp_sorts_in_time_order(tmp_path):
    """`prune` and `read_all` both sort by filename instead of opening every
    file; that is only correct if the stamp is monotonic."""
    early = run_record._stamp("2026-07-30T02:14:08")
    late = run_record._stamp("2026-07-30T12:05:09")
    next_year = run_record._stamp("2027-01-01T00:00:00")
    assert early < late < next_year
