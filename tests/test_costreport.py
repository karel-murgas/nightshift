"""Tests for `nightshift.costreport` — the transcript breakdown behind
`token-economy.md`.

Two layers, tested separately: `transcript_stats` reads one `stream.jsonl` and
must get the arithmetic right off a hand-built stream shaped like a real one
(assistant `tool_use` blocks, user `tool_result` blocks, correlated by
`tool_use_id`); everything above it (`report_card`/`report_run`/`main`) is
tested against `run_record`/`.ai/runs/` fixtures built the same way the rest
of this suite builds them, never against a real CLI transcript.
"""
from __future__ import annotations

import json
from pathlib import Path

from nightshift import costreport, run_record, runner


def _event(kind: str, content: list[dict], *, ts: str = "2026-01-01T00:00:00Z") -> dict:
    return {"type": kind, "message": {"role": kind, "content": content}, "timestamp": ts}


def _tool_use(tool_id: str, name: str, tool_input: dict, *, ts: str) -> dict:
    return _event("assistant", [{"type": "tool_use", "id": tool_id, "name": name,
                                 "input": tool_input}], ts=ts)


def _tool_result(tool_id: str, text: str, *, ts: str) -> dict:
    return _event("user", [{"type": "tool_result", "tool_use_id": tool_id,
                            "content": text}], ts=ts)


def _write_stream(path: Path, events: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_transcript_stats_totals_bytes_per_tool(tmp_path):
    stream = tmp_path / "stream.jsonl"
    _write_stream(stream, [
        _tool_use("t1", "Read", {"file_path": "a.py"}, ts="2026-01-01T00:00:01Z"),
        _tool_result("t1", "x" * 100, ts="2026-01-01T00:00:02Z"),
        _tool_use("t2", "Bash", {"command": "ls"}, ts="2026-01-01T00:00:03Z"),
        _tool_result("t2", "y" * 40, ts="2026-01-01T00:00:04Z"),
    ])
    stats = costreport.transcript_stats(stream)
    assert stats["tool_bytes"] == {"Read": 100, "Bash": 40}
    assert stats["tool_calls"] == {"Read": 1, "Bash": 1}
    assert stats["read_bytes_total"] == 100


def test_a_second_read_of_the_same_path_counts_as_a_reread(tmp_path):
    stream = tmp_path / "stream.jsonl"
    _write_stream(stream, [
        _tool_use("t1", "Read", {"file_path": "a.py"}, ts="2026-01-01T00:00:01Z"),
        _tool_result("t1", "x" * 50, ts="2026-01-01T00:00:02Z"),
        _tool_use("t2", "Read", {"file_path": "b.py"}, ts="2026-01-01T00:00:03Z"),
        _tool_result("t2", "y" * 50, ts="2026-01-01T00:00:04Z"),
        _tool_use("t3", "Read", {"file_path": "a.py"}, ts="2026-01-01T00:00:05Z"),
        _tool_result("t3", "z" * 50, ts="2026-01-01T00:00:06Z"),
    ])
    stats = costreport.transcript_stats(stream)
    # Only the third call re-opens a path already seen — the first sighting of
    # any path is never counted against itself.
    assert stats["reread_bytes"] == 50
    assert stats["reread_share"] == round(50 / 150, 3)


def test_hook_output_is_estimated_past_the_confirmation_boilerplate(tmp_path):
    """An `Edit`/`Write` result under the boilerplate budget is the CLI's own
    confirmation text; bytes past it are presumed to be a PostToolUse hook's
    stdout riding along on the same tool result."""
    stream = tmp_path / "stream.jsonl"
    short = "The file x.py has been updated successfully."
    long = short + "\n" + "gate output " * 100
    _write_stream(stream, [
        _tool_use("t1", "Edit", {}, ts="2026-01-01T00:00:01Z"),
        _tool_result("t1", short, ts="2026-01-01T00:00:02Z"),
        _tool_use("t2", "Edit", {}, ts="2026-01-01T00:00:03Z"),
        _tool_result("t2", long, ts="2026-01-01T00:00:04Z"),
    ])
    stats = costreport.transcript_stats(stream)
    assert stats["hook_output_bytes_est"] == max(0, len(long) - costreport._EDIT_CONFIRMATION_BUDGET)


def test_reads_are_split_by_whether_they_came_before_or_after_the_first_edit(tmp_path):
    stream = tmp_path / "stream.jsonl"
    _write_stream(stream, [
        _tool_use("r1", "Read", {"file_path": "a.py"}, ts="2026-01-01T00:00:01Z"),
        _tool_result("r1", "x" * 60, ts="2026-01-01T00:00:01Z"),
        _tool_use("e1", "Edit", {}, ts="2026-01-01T00:00:02Z"),
        _tool_result("e1", "ok", ts="2026-01-01T00:00:02Z"),
        _tool_use("r2", "Read", {"file_path": "b.py"}, ts="2026-01-01T00:00:03Z"),
        _tool_result("r2", "y" * 40, ts="2026-01-01T00:00:03Z"),
    ])
    stats = costreport.transcript_stats(stream)
    assert stats["read_bytes_total"] == 100
    # Only the read at or after the first (and only) edit's own timestamp counts.
    assert stats["read_bytes_after_first_edit"] == 40
    assert stats["read_share_after_first_edit"] == 0.4
    # With one edit, "after the first" and "close-out" (after the last) agree.
    assert stats["read_share_closeout"] == 0.4


def test_transcript_stats_is_empty_for_a_missing_or_unparseable_file(tmp_path):
    assert costreport.transcript_stats(tmp_path / "nope.jsonl") == {}
    empty = tmp_path / "blank.jsonl"
    empty.write_text("", encoding="utf-8")
    assert costreport.transcript_stats(empty) == {}


def test_merge_stats_sums_across_transcripts_rather_than_averaging_shares(tmp_path):
    a = {"tool_calls": {"Read": 1}, "tool_bytes": {"Read": 100},
        "hook_output_bytes_est": 10, "read_bytes_total": 100, "reread_bytes": 0,
        "read_bytes_after_first_edit": 0, "read_bytes_closeout": 0}
    b = {"tool_calls": {"Read": 1}, "tool_bytes": {"Read": 300},
        "hook_output_bytes_est": 0, "read_bytes_total": 300, "reread_bytes": 300,
        "read_bytes_after_first_edit": 300, "read_bytes_closeout": 300}
    merged = costreport._merge_stats([a, b, {}])
    assert merged["tool_calls"] == {"Read": 2}
    assert merged["tool_bytes"] == {"Read": 400}
    assert merged["read_bytes_total"] == 400
    # 300 reread out of 400 total, not the mean of 0% and 100%.
    assert merged["reread_share"] == 0.75


def test_card_transcript_stats_walks_every_attempt(tmp_path):
    root = tmp_path
    for attempt in (1, 2):
        out = root / runner.RUNS / "probe" / f"attempt-{attempt}"
        out.mkdir(parents=True)
        _write_stream(out / "stream.jsonl", [
            _tool_use("t", "Bash", {}, ts="2026-01-01T00:00:01Z"),
            _tool_result("t", "x" * 10, ts="2026-01-01T00:00:02Z"),
        ])
    stats = costreport.card_transcript_stats(root, "probe")
    assert stats["tool_bytes"]["Bash"] == 20


def test_card_transcript_stats_is_empty_for_a_card_with_no_runs(tmp_path):
    assert costreport.card_transcript_stats(tmp_path, "ghost")["tool_bytes"] == {}


# --- reading run records / cards -------------------------------------------------


def test_find_record_defaults_to_the_most_recent(tmp_path):
    run_record.start(tmp_path, kind="run", label="first")
    run_record.start(tmp_path, kind="run", label="second")
    found = costreport._find_record(tmp_path, "")
    assert found["label"] == "second"


def test_find_record_matches_a_stamp_prefix(tmp_path):
    record = run_record.start(tmp_path, kind="run", label="the one")
    stamp = record.path.stem
    found = costreport._find_record(tmp_path, stamp[:8])
    assert found["label"] == "the one"


def test_find_record_is_none_for_no_match(tmp_path):
    assert costreport._find_record(tmp_path, "nothing-like-a-stamp") is None
    assert costreport._find_record(tmp_path, "") is None


def test_report_card_reads_usage_and_transcript_together(tmp_path):
    out = tmp_path / runner.RUNS / "probe" / "attempt-1"
    out.mkdir(parents=True)
    (out / "worker-1.json").write_text(json.dumps({
        "duration_ms": 60_000, "num_turns": 12, "total_cost_usd": 0.75,
        "usage": {"cache_read_input_tokens": 1000}, "modelUsage": {"claude-sonnet-5": {}},
    }), encoding="utf-8")
    _write_stream(out / "stream.jsonl", [
        _tool_use("t", "Read", {"file_path": "a.py"}, ts="2026-01-01T00:00:01Z"),
        _tool_result("t", "hi", ts="2026-01-01T00:00:02Z"),
    ])
    text = costreport.report_card(tmp_path, "probe")
    assert "worker" in text and "$0.75" in text
    assert "Read" in text


def test_report_card_says_so_when_a_card_never_ran(tmp_path):
    text = costreport.report_card(tmp_path, "ghost")
    assert "no result files found" in text
    assert "no transcript found" in text


def test_report_run_includes_quality_counters(tmp_path):
    record = run_record.start(tmp_path, kind="run")
    record.dispatched("a", worker="w", model="m", attempt=1, outcome="needs_fix")
    data = run_record.read_all(tmp_path)[0]
    text = costreport.report_run(tmp_path, data)
    assert "needs_fix_rate" in text and "1" in text


def test_report_run_says_so_for_a_record_from_before_phase_0_1(tmp_path):
    """A record on disk before `usage()` existed carries no `usage` key at
    all — `report_run` must read that as absent, not raise a `KeyError`."""
    record = run_record.start(tmp_path, kind="run")
    data = run_record.read_all(tmp_path)[0]
    del data["usage"]
    text = costreport.report_run(tmp_path, data)
    assert "before token-economy phase 0.1" in text


# --- the CLI ------------------------------------------------------------------


def test_main_reports_on_a_card_id_directly(tmp_path, capsys):
    out = tmp_path / runner.RUNS / "probe" / "attempt-1"
    out.mkdir(parents=True)
    (out / "worker-1.json").write_text(json.dumps(
        {"num_turns": 5, "total_cost_usd": 0.1}), encoding="utf-8")
    code = costreport.main(["probe", "--root", str(tmp_path)])
    assert code == 0
    assert "probe" in capsys.readouterr().out


def test_main_refuses_cleanly_with_no_records_and_no_matching_card(tmp_path, capsys):
    code = costreport.main(["ghost", "--root", str(tmp_path)])
    assert code == 1
    assert "ghost" in capsys.readouterr().err


def test_main_json_output_is_valid_json(tmp_path, capsys):
    run_record.start(tmp_path, kind="run")
    code = costreport.main(["--root", str(tmp_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert "quality" in payload and "transcript" in payload
