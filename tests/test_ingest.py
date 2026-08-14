"""`nightshift.ingest` — routing the inbox before anything expensive runs.

Three properties are load-bearing and none is visible by reading the happy path, so
they are asserted directly:

**Triage is never dispatched from here.** It is the route that costs an hour and
competes with the night for the same window, so this command only ever *lists* it.
A regression that fanned it out automatically would be the exact failure the module
was written to remove.

**The money rule is re-checked before every dispatch, not once at the start.** A
fan-out that began with headroom can lose it partway through, and a dispatch cannot
be un-started.

**A note the classifier skips is not silently dropped.** Unrouted has to mean
`inline` — the note surfaces for Karel — rather than vanishing from the report.

Nothing here reaches the network or the CLI: `_dispatch` and `usage.read` are both
replaced, which is also what proves the module keeps its side effects in those two
named seams.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from nightshift import ingest, usage


# --------------------------------------------------------------------------- fixtures

def _repo(tmp_path: Path, **notes: str) -> Path:
    lane = tmp_path / "Board" / "inbox"
    lane.mkdir(parents=True)
    (lane / ".gitkeep").write_text("", encoding="utf-8")
    for name, body in notes.items():
        (lane / f"{name}.md").write_text(body, encoding="utf-8")
    return tmp_path


def _healthy() -> usage.Snapshot:
    return usage.Snapshot(buckets=(usage.Bucket("five_hour", 12.0, None),), fetched=True)


def _spent() -> usage.Snapshot:
    return usage.Snapshot(buckets=(usage.Bucket("five_hour", 100.0, None),),
                          paid_enabled=True, fetched=True)


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every dispatch instead of making one. Returns (agent, prompt)."""
    recorded: list[tuple[str, str]] = []

    def fake(agent: str, prompt: str, root: Path, model: str, timeout: int):
        recorded.append((agent, prompt))
        if agent == "classifier":
            return json.dumps({"notes": [
                {"file": "alpha.md", "route": "chore", "why": "obvious", "dispatchable": True,
                 "confidence": "high"},
                {"file": "beta.md", "route": "triage", "why": "has a fork",
                 "dispatchable": True, "confidence": "high"},
            ]}), ""
        return json.dumps({"ok": True}), ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    monkeypatch.setattr(usage, "read", _healthy)
    return recorded


# ------------------------------------------------------------------ collecting notes

def test_notes_finds_markdown_and_skips_the_keepfile(tmp_path: Path):
    root = _repo(tmp_path, alpha="a", beta="b")
    names = [n.name for n in ingest.notes(root)]
    assert set(names) == {"alpha.md", "beta.md"}
    assert ".gitkeep" not in names


def test_notes_on_a_repo_with_no_lane_is_empty_not_an_error(tmp_path: Path):
    assert ingest.notes(tmp_path) == []


def test_notes_carries_the_body_because_routing_reads_it(tmp_path: Path):
    root = _repo(tmp_path, alpha="the note body")
    assert ingest.notes(root)[0].text == "the note body"
    assert ingest.notes(root)[0].size == len("the note body")


# --------------------------------------------------------------- parsing model output

@pytest.mark.parametrize("reply", [
    '{"notes": []}',
    'here you go:\n```json\n{"notes": []}\n```\nhope that helps',
    'prose first {"notes": []} prose after',
])
def test_json_is_found_however_the_model_wrapped_it(reply: str):
    payload, why = ingest._extract_json(reply)
    assert payload == {"notes": []} and why == ""


def test_a_reply_with_no_json_is_an_error_not_an_empty_routing():
    payload, why = ingest._extract_json("I have decided not to answer.")
    assert payload is None and why


class _Done:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.returncode, self.stdout, self.stderr = code, out, err


def test_cli_result_reports_a_nonzero_exit_with_its_last_line():
    text, why = ingest._cli_result(_Done(1, err="boom\nthe real reason"))
    assert text == "" and "the real reason" in why and "exited 1" in why


def test_cli_result_surfaces_an_error_envelope():
    body = json.dumps({"is_error": True, "result": "model refused"})
    text, why = ingest._cli_result(_Done(0, out=body))
    assert text == "" and "model refused" in why


def test_cli_result_unwraps_the_envelope_and_tolerates_a_missing_one():
    body = json.dumps({"is_error": False, "result": "the answer"})
    assert ingest._cli_result(_Done(0, out=body)) == ("the answer", "")
    # A CLI-shape change must not lose the reply.
    assert ingest._cli_result(_Done(0, out="bare text")) == ("bare text", "")


# ------------------------------------------------------------------------ classifying

def test_classify_maps_every_note_and_keeps_the_route(tmp_path: Path, calls):
    root = _repo(tmp_path, alpha="a", beta="b")
    routing = ingest.classify(ingest.notes(root), root)
    assert [d.route for d in sorted(routing.decisions, key=lambda d: d.note)] \
        == ["chore", "triage"]
    assert calls == [c for c in calls if c[0] == "classifier"]


def test_classify_sends_the_notes_bodies_not_just_their_names(tmp_path: Path, calls):
    root = _repo(tmp_path, alpha="DISTINCTIVE-BODY-TEXT", beta="b")
    ingest.classify(ingest.notes(root), root)
    assert "DISTINCTIVE-BODY-TEXT" in calls[0][1]


def test_a_note_the_classifier_skipped_becomes_inline_not_a_gap(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a", forgotten="f")
    monkeypatch.setattr(ingest, "_dispatch", lambda *a, **k: (json.dumps({"notes": [
        {"file": "alpha.md", "route": "chore", "why": "ok"}]}), ""))
    routing = ingest.classify(ingest.notes(root), root)
    missed = [d for d in routing.decisions if d.note == "forgotten.md"]
    assert len(missed) == 1
    assert missed[0].route == "inline" and missed[0].confidence == "low"


def test_an_unknown_route_degrades_to_inline_rather_than_crashing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(ingest, "_dispatch", lambda *a, **k: (json.dumps({"notes": [
        {"file": "alpha.md", "route": "teleport", "why": "?"}]}), ""))
    assert ingest.classify(ingest.notes(root), root).decisions[0].route == "inline"


def test_a_hallucinated_filename_is_dropped_not_carded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(ingest, "_dispatch", lambda *a, **k: (json.dumps({"notes": [
        {"file": "alpha.md", "route": "chore", "why": "ok"},
        {"file": "invented.md", "route": "chore", "why": "does not exist"}]}), ""))
    names = {d.note for d in ingest.classify(ingest.notes(root), root).decisions}
    assert names == {"alpha.md"}


def test_a_failed_dispatch_yields_an_error_not_a_silent_empty_routing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(ingest, "_dispatch", lambda *a, **k: ("", "CLI exited 1: nope"))
    routing = ingest.classify(ingest.notes(root), root)
    assert routing.error and routing.decisions == []


# ------------------------------------------------------- triage is never dispatched

def test_triage_is_listed_and_never_dispatched(tmp_path: Path, calls, capsys):
    """The expensive route competes with the night for the same window."""
    root = _repo(tmp_path, alpha="a", beta="b")
    assert ingest.main(["--root", str(root), "--scribe"]) == 0
    agents = [agent for agent, _ in calls]
    assert "triage" not in agents
    assert agents.count("classifier") == 1
    text = (root / ingest.OUT).read_text(encoding="utf-8")
    assert "beta.md" in text and "Waiting on triage" in text


def test_scribe_runs_only_on_its_own_bucket(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a", beta="b", gamma="g")
    recorded: list[tuple[str, str]] = []

    def fake(agent: str, prompt: str, *_a, **_k):
        recorded.append((agent, prompt))
        if agent == "classifier":
            return json.dumps({"notes": [
                {"file": "alpha.md", "route": "scribe", "why": "elaborated"},
                {"file": "beta.md", "route": "chore", "why": "small"},
                {"file": "gamma.md", "route": "inline", "why": "needs Karel"}]}), ""
        return "{}", ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    monkeypatch.setattr(usage, "read", _healthy)
    assert ingest.main(["--root", str(root), "--scribe"]) == 0
    scribed = [prompt for agent, prompt in recorded if agent == "scribe"]
    assert len(scribed) == 1 and "alpha.md" in scribed[0]


# --------------------------------------------------------------- the money rule

def test_a_refusal_dispatches_nothing_at_all(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(usage, "read", _spent)
    monkeypatch.setattr(ingest, "_dispatch",
                        lambda *a, **k: pytest.fail("must not dispatch when refused"))
    assert ingest.main(["--root", str(root)]) == 3


def test_the_explicit_opt_in_lets_a_refused_run_proceed(tmp_path: Path, capsys,
                                                        monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(usage, "read", _spent)
    monkeypatch.setattr(ingest, "_dispatch", lambda *a, **k: (json.dumps({"notes": [
        {"file": "alpha.md", "route": "chore", "why": "ok"}]}), ""))
    assert ingest.main(["--root", str(root), "--allow-paid"]) == 0


def test_the_guard_is_rechecked_before_each_scribe_not_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Headroom can run out partway through a fan-out."""
    root = _repo(tmp_path, alpha="a", beta="b", gamma="g")
    readings = [_healthy(), _healthy(), _spent(), _spent(), _spent()]
    monkeypatch.setattr(usage, "read", lambda *a, **k: readings.pop(0) if readings
                        else _spent())
    scribed: list[str] = []

    def fake(agent: str, prompt: str, *_a, **_k):
        if agent == "classifier":
            return json.dumps({"notes": [
                {"file": n, "route": "scribe", "why": "x"}
                for n in ("alpha.md", "beta.md", "gamma.md")]}), ""
        scribed.append(prompt)
        return "{}", ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    assert ingest.main(["--root", str(root), "--scribe"]) == 3
    # It stopped once the window closed instead of driving the whole bucket.
    assert len(scribed) < 3


def test_an_unmetered_reading_still_lets_work_through(tmp_path: Path, capsys,
                                                      monkeypatch: pytest.MonkeyPatch):
    """Fail open: a missing meter must not become a stop. Karel, 2026-08-14: open is good."""
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(usage, "read",
                        lambda *a, **k: usage.Snapshot(reason="endpoint unreachable"))
    monkeypatch.setattr(ingest, "_dispatch", lambda *a, **k: (json.dumps({"notes": [
        {"file": "alpha.md", "route": "chore", "why": "ok"}]}), ""))
    assert ingest.main(["--root", str(root)]) == 0
    assert "unmetered" in capsys.readouterr().out


# ------------------------------------------------------------------ the scribe bounce

def test_a_bounce_is_counted_as_a_bounce_not_a_written_card(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(usage, "read", _healthy)

    def fake(agent: str, prompt: str, *_a, **_k):
        if agent == "classifier":
            return json.dumps({"notes": [
                {"file": "alpha.md", "route": "scribe", "why": "x"}]}), ""
        return json.dumps({"bounce": True, "note": "alpha.md",
                           "reason": "acceptance needs the code"}), ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    assert ingest.main(["--root", str(root), "--scribe"]) == 0
    out = capsys.readouterr().out
    assert "bounced to triage" in out and "acceptance needs the code" in out
    assert "0 card(s), 1 bounced" in out


# ------------------------------------------------------------------------ the report

def test_report_lists_every_bucket_even_when_empty(tmp_path: Path, calls):
    root = _repo(tmp_path, alpha="a", beta="b")
    ingest.main(["--root", str(root)])
    text = (root / ingest.OUT).read_text(encoding="utf-8")
    for heading in ("Do now - inline", "Chores - batch overnight",
                    "Scribe - needs the envelope only", "Waiting on triage"):
        assert heading in text
    assert "_none_" in text                      # inline and scribe were empty


def test_report_flags_low_confidence_and_undispatchable_triage():
    decisions = [
        ingest.Decision("shaky.md", "chore", "not sure", confidence="low"),
        ingest.Decision("pointless.md", "triage", "fork", dispatchable=False),
        ingest.Decision("fine.md", "chore", "clear"),
    ]
    text = ingest.report(ingest.Routing(decisions), [], _healthy(), dt.datetime(2026, 8, 14))
    assert "Check these first" in text
    assert "shaky.md" in text.split("Check these first")[1]
    assert "pointless.md" in text.split("Check these first")[1]
    assert "fine.md" not in text.split("Check these first")[1]


def test_report_carries_the_meter_reading_so_a_stale_view_is_obvious():
    text = ingest.report(ingest.Routing([]), [], _healthy(), dt.datetime(2026, 8, 14, 9, 30))
    assert "2026-08-14 09:30" in text and "five_hour" in text


def test_a_dry_run_dispatches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a")
    monkeypatch.setattr(usage, "read", _healthy)
    monkeypatch.setattr(ingest, "_dispatch",
                        lambda *a, **k: pytest.fail("dry run must not dispatch"))
    assert ingest.main(["--root", str(root), "--dry-run"]) == 0
    assert not (root / ingest.OUT).exists()


def test_an_empty_lane_is_a_clean_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(ingest, "_dispatch",
                        lambda *a, **k: pytest.fail("nothing to route"))
    assert ingest.main(["--root", str(root)]) == 0


def test_the_report_is_written_lf_because_it_is_committed(tmp_path: Path, calls):
    root = _repo(tmp_path, alpha="a", beta="b")
    ingest.main(["--root", str(root)])
    assert b"\r\n" not in (root / ingest.OUT).read_bytes()
