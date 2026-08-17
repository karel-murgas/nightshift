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
import re
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


_SCRIBED = """\
---
id: {stem}
title: "{stem}"
state: tasks
tier: worker
worker: code-thread
recipe: none
unattended: true
verify: play
created: 2026-08-17
---

## Intent

What the note asked for.
"""


def _card_the_note(root: Path, note: str) -> str:
    """Do to the board what a scribe does: the note becomes the card and leaves.

    `triage`'s charter states the contract — *"the note **is** the card. You edit
    the file in place and move it; you never copy it into a new card and leave the
    original behind"* — and `ingest.scribe` now checks it, so a fake that returns
    cleanly while touching nothing is a fake of a **broken** scribe. Simulating the
    effect is what makes these tests exercise the check instead of tripping it.

    The rename is part of the contract too, not incidental: a card id is a
    kebab-case stem and real notes are called `Regenerate soundtrack.md`.
    """
    stem = Path(note).stem.lower().replace(" ", "-")
    lane = root / "Board" / "tasks"
    lane.mkdir(parents=True, exist_ok=True)
    (lane / f"{stem}.md").write_text(_SCRIBED.format(stem=stem), encoding="utf-8")
    (root / "Board" / "inbox" / note).unlink()
    return stem


def _note_of(prompt: str) -> str:
    """Which note a scribe prompt is about. The prompt names it in backticks."""
    match = re.search(r"Board/inbox/([^`]+)`", prompt)
    return match.group(1) if match else ""


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
        _card_the_note(root, _note_of(prompt))
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


def _routes(monkeypatch: pytest.MonkeyPatch, routing: dict[str, str],
            *, effect=None) -> list[str]:
    """Classify `routing` (note → route), then record which notes reach the scribe.

    `effect` is what the scribe does to the board; the default is what a working
    one does. Pass a no-op to fake a scribe that returns cleanly and achieves
    nothing.
    """
    scribed: list[str] = []
    act = _card_the_note if effect is None else effect

    def fake(agent: str, prompt: str, root: Path, *_a, **_k):
        if agent == "classifier":
            return json.dumps({"notes": [{"file": f, "route": r, "why": "x"}
                                         for f, r in routing.items()]}), ""
        note = _note_of(prompt)
        scribed.append(note)
        act(root, note)
        return "{}", ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    monkeypatch.setattr(usage, "read", _healthy)
    return scribed


def test_scribe_covers_both_writable_buckets(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch):
    """`chore` was a dead end until 2026-08-17.

    The classifier routed notes there, the scribe's charter had a section on
    writing the card, `chores.select` read `kind: chore` out of `tasks/` — and this
    fan-out took `by_route("scribe")` alone, so nothing ever asked. A chore-routed
    note sat in the lane forever and the panel's Chores section was permanently,
    accurately empty.
    """
    root = _repo(tmp_path, alpha="a", beta="b", gamma="g")
    scribed = _routes(monkeypatch, {"alpha.md": "scribe", "beta.md": "chore",
                                    "gamma.md": "inline"})
    assert ingest.main(["--root", str(root), "--scribe"]) == 0
    assert set(scribed) == {"alpha.md", "beta.md"}


def test_an_inline_note_is_never_scribed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`inline` means Karel does it and no card is written — that is the route's
    definition, not a queue it is waiting in."""
    root = _repo(tmp_path, alpha="a")
    scribed = _routes(monkeypatch, {"alpha.md": "inline"})
    assert ingest.main(["--root", str(root), "--scribe"]) == 0
    assert scribed == []
    assert (root / "Board" / "inbox" / "alpha.md").exists()


# ------------------------------------------- the note has to leave the lane


def test_a_card_written_while_the_note_stays_is_reported_not_counted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """The failure this check exists for is a **duplicate**, not a missing card.

    A card written while the note stays in `inbox/` gets re-routed by the next
    classify pass and carded again — and the second card looks exactly as
    legitimate as the first.
    """
    root = _repo(tmp_path, alpha="a")

    def writes_but_keeps(root: Path, note: str) -> None:
        lane = root / "Board" / "tasks"
        lane.mkdir(parents=True, exist_ok=True)
        (lane / "alpha.md").write_text(_SCRIBED.format(stem="alpha"), encoding="utf-8")

    _routes(monkeypatch, {"alpha.md": "scribe"}, effect=writes_but_keeps)
    assert ingest.main(["--root", str(root), "--scribe"]) == 1
    out = capsys.readouterr().out
    assert "left the note in inbox/" in out
    assert "card it twice" in out
    assert "0 card(s)" in out and "1 stranded" in out


def test_a_scribe_that_achieves_nothing_says_so(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch, capsys):
    """A dispatch returning cleanly is not evidence that a card exists."""
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "chore"}, effect=lambda root, note: None)
    assert ingest.main(["--root", str(root), "--scribe"]) == 1
    assert "nothing happened" in capsys.readouterr().out


def test_a_note_that_leaves_without_a_card_is_not_a_success(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "chore"},
            effect=lambda root, note: (root / "Board" / "inbox" / note).unlink())
    assert ingest.main(["--root", str(root), "--scribe"]) == 1
    assert "no card appeared" in capsys.readouterr().out


def test_the_note_becomes_the_card_and_the_lane_empties(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """The whole point, end to end: one note in, one card out, empty lane."""
    root = _repo(tmp_path, **{"Regenerate soundtrack": "new tracks please"})
    _routes(monkeypatch, {"Regenerate soundtrack.md": "chore"})
    assert ingest.main(["--root", str(root), "--scribe"]) == 0
    assert ingest.notes(root) == []
    assert ingest.card_ids(root) == {"regenerate-soundtrack"}
    assert "1 card(s)" in capsys.readouterr().out


# ------------------------------------------------------------ one note at a time


def test_only_writes_the_card_for_one_note_on_its_recorded_route(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`--only` is the per-note verb the panel's rows need. It reads the route back
    off the report rather than paying for a second classify pass."""
    root = _repo(tmp_path, alpha="a", beta="b")
    scribed = _routes(monkeypatch, {"alpha.md": "chore", "beta.md": "chore"})
    assert ingest.main(["--root", str(root)]) == 0        # classify, write nothing
    assert scribed == []

    assert ingest.main(["--root", str(root), "--only", "beta.md"]) == 0
    assert scribed == ["beta.md"]
    assert [n.name for n in ingest.notes(root)] == ["alpha.md"]


def test_only_refuses_a_triage_note_because_a_human_chooses_that_spend(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    root = _repo(tmp_path, alpha="a")
    scribed = _routes(monkeypatch, {"alpha.md": "triage"})
    assert ingest.main(["--root", str(root)]) == 0

    assert ingest.main(["--root", str(root), "--only", "alpha.md"]) == 2
    assert scribed == []
    assert "expensive route" in capsys.readouterr().out


def test_only_refuses_an_inline_note_because_it_gets_no_card(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "inline"})
    assert ingest.main(["--root", str(root)]) == 0

    assert ingest.main(["--root", str(root), "--only", "alpha.md"]) == 2
    assert "your own work at the keyboard" in capsys.readouterr().out


def test_only_on_an_unrouted_note_says_to_classify_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {})
    assert ingest.main(["--root", str(root), "--only", "alpha.md"]) == 2
    assert "classify the inbox first" in capsys.readouterr().out


def test_only_on_a_note_that_does_not_exist_is_refused(tmp_path: Path, capsys):
    root = _repo(tmp_path, alpha="a")
    assert ingest.main(["--root", str(root), "--only", "ghost.md"]) == 2
    assert "no note named" in capsys.readouterr().out


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


# --------------------------------------------------------------------------
# Reading the view back. `report` and `parse_report` are two halves of one
# format, twenty lines apart in one module, and the round-trip is what keeps
# them that way: rewording a heading without teaching the reader about it fails
# here rather than silently emptying the panel's inbox page.
# --------------------------------------------------------------------------


def _decision(note: str, route: str, **kw) -> ingest.Decision:
    return ingest.Decision(note=note, route=route, why=f"because of {note}", **kw)


def test_a_report_round_trips_through_its_own_parser():
    routing = ingest.Routing([
        _decision("plain.md", "chore"),
        _decision("shaky.md", "triage", confidence="low"),
        _decision("human.md", "inline", dispatchable=False),
        _decision("both.md", "scribe", confidence="medium", dispatchable=False),
    ])
    text = ingest.report(routing, [], _healthy(), dt.datetime(2026, 8, 17, 9, 51))
    view = ingest.parse_report(text)

    assert view.written == dt.datetime(2026, 8, 17, 9, 51)
    assert set(view.decisions) == {"plain.md", "shaky.md", "human.md", "both.md"}
    for original in routing.decisions:
        back = view.of(original.note)
        assert back.route == original.route
        assert back.why == original.why
        assert back.confidence == original.confidence
        assert back.dispatchable == original.dispatchable


def test_a_why_containing_a_dash_survives_the_round_trip():
    """The entry line is `- **note** - why`, and a `why` that itself contains
    ` - ` is the ordinary case, not the exotic one."""
    routing = ingest.Routing([ingest.Decision(
        note="n.md", route="triage",
        why="the fork - aimed tile vs heat-map - is left for triage")])
    view = ingest.parse_report(
        ingest.report(routing, [], _healthy(), dt.datetime(2026, 8, 17, 9, 51)))
    assert view.of("n.md").why == "the fork - aimed tile vs heat-map - is left for triage"


def test_the_check_these_first_repeat_does_not_overwrite_the_real_routing():
    """A suspect note is listed twice in the report. The second listing carries
    no `why` and sits under a heading that is not a route, so it must contribute
    nothing rather than blanking the entry."""
    routing = ingest.Routing([_decision("shaky.md", "triage", confidence="low")])
    view = ingest.parse_report(
        ingest.report(routing, [], _healthy(), dt.datetime(2026, 8, 17, 9, 51)))
    assert view.of("shaky.md").route == "triage"
    assert view.of("shaky.md").why == "because of shaky.md"


def test_reading_a_view_that_was_never_written_is_empty_not_an_error(tmp_path: Path):
    view = ingest.read_view(_repo(tmp_path))
    assert not view.known
    assert view.of("anything.md") is None


def test_a_mangled_view_is_read_as_nothing_known_rather_than_raising(tmp_path: Path):
    root = _repo(tmp_path)
    (root / ingest.OUT).write_text("someone edited this by hand\n\n## Nonsense (2)\n",
                                   encoding="utf-8")
    view = ingest.read_view(root)
    assert view.decisions == {}


def test_the_view_written_by_a_real_run_reads_back(tmp_path: Path, calls):
    """End to end through `main`: the file the command writes is the file the
    reader reads, with no test-authored markdown in between."""
    root = _repo(tmp_path, alpha="a", beta="b")
    ingest.main(["--root", str(root)])
    view = ingest.read_view(root)
    assert set(view.decisions) == {"alpha.md", "beta.md"}


def test_write_cards_uses_the_recorded_routing_without_reclassifying(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The second of the two steps the module's own ordering asks for.

    `--scribe` does both halves at once and has to, for an unattended caller. As
    the shape of a *button* it inverts the rule — "it reports before it spends" —
    and pays for a fresh classify pass to re-learn what the report already says.
    """
    root = _repo(tmp_path, alpha="a", beta="b", gamma="g")
    classifies = []

    def fake(agent: str, prompt: str, root: Path, *_a, **_k):
        if agent == "classifier":
            classifies.append(prompt)
            return json.dumps({"notes": [
                {"file": "alpha.md", "route": "chore", "why": "x"},
                {"file": "beta.md", "route": "scribe", "why": "x"},
                {"file": "gamma.md", "route": "triage", "why": "x"}]}), ""
        _card_the_note(root, _note_of(prompt))
        return "{}", ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    monkeypatch.setattr(usage, "read", _healthy)

    assert ingest.main(["--root", str(root)]) == 0
    assert len(classifies) == 1

    assert ingest.main(["--root", str(root), "--write-cards"]) == 0
    assert len(classifies) == 1, "the second step must not pay for a second pass"
    assert [n.name for n in ingest.notes(root)] == ["gamma.md"], "triage note untouched"
    assert ingest.card_ids(root) == {"alpha", "beta"}


def test_write_cards_before_any_pass_says_to_classify_first(tmp_path: Path, capsys):
    root = _repo(tmp_path, alpha="a")
    assert ingest.main(["--root", str(root), "--write-cards"]) == 2
    assert "classify the inbox first" in capsys.readouterr().out


def test_write_cards_ignores_a_recorded_note_that_has_since_left_the_lane(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """The report is a view over the lane, so an entry for a note somebody carded
    or deleted by hand is stale — and re-scribing it would be the duplicate this
    whole check exists to prevent."""
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "chore"})
    assert ingest.main(["--root", str(root)]) == 0
    (root / "Board" / "inbox" / "alpha.md").unlink()

    assert ingest.main(["--root", str(root), "--write-cards"]) == 0
    assert "still in the lane" in capsys.readouterr().out
