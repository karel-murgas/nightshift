"""Tests for `aiteam/run_record.py` — the run's own account of itself.

The record exists because board state cannot describe a run (see the module
docstring, and `test_board_digest.py`'s first test). What has to be true of it:

- it survives a run that dies, because the run that dies is the one worth
  reporting — the 2026-07-30 night was killed mid-sweep and never wrote a digest;
- it never takes a run down with it, so every write is best-effort;
- its window is "runs Karel has not been shown", derived idempotently.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from aiteam import run_record


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


def test_an_unfinished_record_reads_as_killed_by_the_digest(tmp_path):
    """The property the digest's `killed` header depends on."""
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


# --- the window ---------------------------------------------------------------

def test_records_since_returns_only_the_unreported_ones(tmp_path):
    for stamp in ("2026-07-28T02:00:00", "2026-07-30T02:00:00", "2026-07-30T12:00:00"):
        run_record.Record(tmp_path, tmp_path / run_record.DIR / f"{run_record._stamp(stamp)}.json",
                          {"started": stamp}).save()
    since = "2026-07-29T00:00:00"
    got = [r["started"] for r in run_record.records_since(tmp_path, since)]
    assert got == ["2026-07-30T12:00:00", "2026-07-30T02:00:00"]   # newest first


def test_no_baseline_returns_everything(tmp_path):
    run_record.start(tmp_path, kind="run")
    assert len(run_record.records_since(tmp_path, None)) == 1


def test_records_since_is_idempotent(tmp_path):
    """Re-rendering the digest must not make the second render claim nothing
    happened — which is why the window comes from the digest commit rather than
    from a marker the reader writes."""
    run_record.start(tmp_path, kind="run")
    first = run_record.records_since(tmp_path, None)
    second = run_record.records_since(tmp_path, None)
    assert first == second


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


def test_reading_a_root_with_no_records_directory_is_empty_not_an_error(tmp_path):
    assert run_record.read_all(tmp_path) == []
    assert run_record.records_since(tmp_path, "2026-01-01T00:00:00") == []


# --- the null record for a dry run -------------------------------------------

def test_a_dry_run_writes_nothing(tmp_path):
    """`--dry-run` promises "no LLM, no writes", and a record file is a write."""
    record = run_record.null()
    record.dispatched("probe", worker="w", model="m", attempt=1, outcome="failed")
    record.skipped([("a", "because")])
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


def test_a_stamp_sorts_in_time_order(tmp_path):
    """`prune` and `read_all` both sort by filename instead of opening every
    file; that is only correct if the stamp is monotonic."""
    early = run_record._stamp("2026-07-30T02:14:08")
    late = run_record._stamp("2026-07-30T12:05:09")
    next_year = run_record._stamp("2027-01-01T00:00:00")
    assert early < late < next_year
