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
import subprocess
from pathlib import Path

import pytest

from nightshift import ingest, usage


# --------------------------------------------------------------------------- fixtures

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _repo(tmp_path: Path, **notes: str) -> Path:
    """A real git repo, because this module commits now.

    It did not use to, and that was the defect: a card the scribe wrote sat in the
    working tree beside the note's deletion, with nothing closing the transaction.
    A fixture with no git would let that regress silently — `board.commit_board`
    warns rather than raising, on purpose, so the tests would go on passing while
    committing nothing.
    """
    lane = tmp_path / "Board" / "inbox"
    lane.mkdir(parents=True)
    (lane / ".gitkeep").write_text("", encoding="utf-8")
    for name, body in notes.items():
        (lane / f"{name}.md").write_text(body, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _log(root: Path) -> list[str]:
    return _git(root, "log", "--format=%s").stdout.strip().splitlines()


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


# ---------------------------------------------- a bounce re-routes, as promised


def test_a_bounce_moves_the_note_to_triage_in_the_view(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The charter says a bounce "re-routes the note to `triage`, which is the
    correct outcome and counts as success". Nothing performed it, so the note kept
    the route that had just failed and the panel's button bought the same bounce
    again. Measured on the first real fan-out: three of five notes bounced, all
    three still routed `chore` afterwards.
    """
    root = _repo(tmp_path, alpha="a", beta="b")

    def fake(agent: str, prompt: str, root: Path, *_a, **_k):
        if agent == "classifier":
            return json.dumps({"notes": [
                {"file": "alpha.md", "route": "chore", "why": "looks thin"},
                {"file": "beta.md", "route": "chore", "why": "also thin"}]}), ""
        if _note_of(prompt) == "alpha.md":
            return json.dumps({"bounce": True, "note": "alpha.md",
                               "reason": "no anchor number to ground acceptance"}), ""
        _card_the_note(root, _note_of(prompt))
        return "{}", ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    monkeypatch.setattr(usage, "read", _healthy)
    ingest.main(["--root", str(root), "--scribe"])

    view = ingest.read_view(root)
    assert view.of("alpha.md").route == "triage"
    assert "no anchor number" in view.of("alpha.md").why
    assert "scribe bounced it" in view.of("alpha.md").why


def test_a_bounce_does_not_move_the_classification_timestamp(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The pass really did happen when it says. Restamping would make a bounce look
    like a fresh classify pass nobody paid for."""
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "chore"})
    ingest.main(["--root", str(root)])
    before = ingest.read_view(root).written

    ingest.reroute_to_triage(root, "alpha.md", "needs the code", _healthy())

    view = ingest.read_view(root)
    assert view.written == before
    assert view.of("alpha.md").route == "triage"


def test_a_bounced_note_can_no_longer_be_carded_from_the_per_note_verb(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """The point of the re-route, end to end: the second press is refused rather
    than buying the same bounce again."""
    root = _repo(tmp_path, alpha="a")
    scribed = _routes(monkeypatch, {"alpha.md": "chore"},
                      effect=lambda root, note: None)
    ingest.main(["--root", str(root)])
    monkeypatch.setattr(ingest, "_dispatch", lambda agent, prompt, *a, **k: (
        json.dumps({"bounce": True, "note": "alpha.md", "reason": "needs the code"}), ""))

    assert ingest.main(["--root", str(root), "--only", "alpha.md"]) == 1
    assert ingest.read_view(root).of("alpha.md").route == "triage"

    assert ingest.main(["--root", str(root), "--only", "alpha.md"]) == 2
    assert "expensive route" in capsys.readouterr().out


def test_rerouting_a_note_the_view_never_had_changes_nothing(tmp_path: Path):
    root = _repo(tmp_path, alpha="a")
    assert ingest.reroute_to_triage(root, "ghost.md", "why", _healthy()) is False


# ------------------------------------------- closing the transaction it opened


def test_a_written_card_is_committed_with_the_note_that_became_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every neighbouring path commits — `boardcmd`'s verbs, `board.move`,
    `chores` — and this one did not, so a card the scribe wrote sat in the working
    tree beside the note's deletion. Observed 2026-08-17: `skills-tinkering` had
    been a card for an hour and git still showed the note as deleted-but-unstaged
    next to an untracked card.
    """
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "chore"})

    assert ingest.main(["--root", str(root), "--scribe"]) == 0

    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert any("alpha.md carded as alpha" in line for line in _log(root)), _log(root)


def test_each_card_is_its_own_commit_so_a_lost_window_keeps_the_finished_ones(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Per note, not per run: `_guard` exists to stop a fan-out partway, and
    committing at the end would make the cheapest failure cost the most work."""
    root = _repo(tmp_path, alpha="a", beta="b")
    _routes(monkeypatch, {"alpha.md": "chore", "beta.md": "chore"})

    ingest.main(["--root", str(root), "--scribe"])

    carded = [line for line in _log(root) if "carded as" in line]
    assert len(carded) == 2, _log(root)


def test_the_classify_pass_commits_the_view_it_wrote(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`Routing.md` is in `GENERATED_VIEWS`, which is what makes it a committed
    artefact and why the dispatch dirty-check exempts it. Writing it without
    committing left the report Karel reads sitting modified for hours,
    indistinguishable from an edit somebody had made by hand."""
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "triage"})

    assert ingest.main(["--root", str(root)]) == 0

    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert any("routed 1 note(s)" in line for line in _log(root)), _log(root)


def test_a_bounce_commits_the_re_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path, alpha="a")

    def fake(agent: str, prompt: str, root: Path, *_a, **_k):
        if agent == "classifier":
            return json.dumps({"notes": [{"file": "alpha.md", "route": "chore",
                                          "why": "thin"}]}), ""
        return json.dumps({"bounce": True, "note": "alpha.md",
                           "reason": "needs the code"}), ""

    monkeypatch.setattr(ingest, "_dispatch", fake)
    monkeypatch.setattr(usage, "read", _healthy)
    ingest.main(["--root", str(root), "--scribe"])

    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert any("re-routed to triage by a scribe bounce" in line for line in _log(root))


def test_a_stranded_half_transition_is_committed_and_says_so(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The half-transition is already on disk, and leaving it dirty does not undo
    it — it only means the next board write sweeps it up under an unrelated
    message. So it is committed, with the message a reader needs."""
    root = _repo(tmp_path, alpha="a")

    def writes_but_keeps(root: Path, note: str) -> None:
        lane = root / "Board" / "tasks"
        lane.mkdir(parents=True, exist_ok=True)
        (lane / "alpha.md").write_text(_SCRIBED.format(stem="alpha"), encoding="utf-8")

    _routes(monkeypatch, {"alpha.md": "scribe"}, effect=writes_but_keeps)
    assert ingest.main(["--root", str(root), "--scribe"]) == 1

    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert any("stranded" in line and "card it twice" in line for line in _log(root))


def test_a_dispatch_that_changed_nothing_makes_no_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A commit per attempt rather than per effect would fill the log with
    entries recording that nothing happened."""
    root = _repo(tmp_path, alpha="a")
    _routes(monkeypatch, {"alpha.md": "chore"}, effect=lambda root, note: None)
    ingest.main(["--root", str(root), "--scribe"])

    assert not [line for line in _log(root) if "stranded" in line], _log(root)


def test_a_route_can_be_corrected_without_paying_for_another_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The classifier reads no code, so its answer is a recommendation and being
    wrong about one note is affordable *provided something can correct it*. Until
    2026-08-17 nothing could: the only way to change one route was another pass
    over the whole lane."""
    root = _repo(tmp_path, alpha="a", beta="b")
    _routes(monkeypatch, {"alpha.md": "triage", "beta.md": "inline"})
    ingest.main(["--root", str(root)])

    assert ingest.set_route(root, "alpha.md", "chore",
                            "one mechanic, one outcome — no fork", _healthy())

    view = ingest.read_view(root)
    assert view.of("alpha.md").route == "chore"
    assert "no fork" in view.of("alpha.md").why
    assert view.of("beta.md").route == "inline", "the rest of the view is untouched"


def test_a_route_that_is_not_a_route_is_refused(tmp_path: Path):
    root = _repo(tmp_path, alpha="a")
    with pytest.raises(ValueError, match="not one of"):
        ingest.set_route(root, "alpha.md", "somewhere-else", "why", _healthy())


# --------------------------------------------------------------------------
# Reading a run's own output back as a roster. Driven by logs from real runs,
# committed under tests/data/, because the format's only definition is what the
# prints in this module produce -- a hand-written fixture would let the two
# drift together and still agree.
# --------------------------------------------------------------------------

_LOGS = Path(__file__).parent / "data"


def test_a_real_carding_run_reads_back_as_its_roster():
    """The 2026-08-17 13:21 run: five notes, three bounced, one stranded, one
    carded. Every one of those outcomes is a different branch of the parser."""
    progress = ingest.parse_progress(
        (_LOGS / "ingest-carding.log").read_text(encoding="utf-8"))

    assert progress.total == 5
    assert [i.state for i in progress.items] == [
        "bounced", "bounced", "bounced", "stranded", "done"]
    assert progress.finished == 5
    by_name = {i.name: i for i in progress.items}
    assert by_name["skills-tinkering.md"].detail == "skills-tinkering"
    assert "one recharge mechanic" not in by_name["ad-sound-for-recharge.md"].detail
    assert "which recharge mechanic" in by_name["ad-sound-for-recharge.md"].detail


def test_the_fan_outs_own_tally_is_not_read_as_a_note():
    """`  scribe: 1 card(s), 3 bounced, ...` shares its prefix with a per-note
    line, and was read as a note called "1 card(s), 3 bounced, 1 stranded, 0 not
    reached" until the summary was checked first."""
    progress = ingest.parse_progress(
        (_LOGS / "ingest-carding.log").read_text(encoding="utf-8"))
    assert not [i for i in progress.items if "card(s)" in i.name]


def test_a_real_classify_run_reads_back_as_its_route_counts():
    """A classify pass is one dispatch over the whole lane, so it has no per-note
    progress to report and none is invented for it."""
    progress = ingest.parse_progress(
        (_LOGS / "ingest-classifying.log").read_text(encoding="utf-8"))

    assert progress.items == []
    assert progress.total == 13
    assert progress.phase == "routed"
    assert progress.routes == {"chore": 3, "inline": 6, "scribe": 2, "triage": 2}


def test_a_note_in_flight_is_on_the_roster_as_running():
    progress = ingest.parse_progress(
        "ingest: 3 note(s) to card\n"
        "  [1/3] scribe: done.md\n    -> done-card\n"
        "  [2/3] scribe: inflight.md\n")
    assert [(i.name, i.state) for i in progress.items] == [
        ("done.md", "done"), ("inflight.md", "running")]
    assert progress.finished == 1 and progress.total == 3


def test_the_meter_lines_and_quoted_replies_contribute_nothing():
    """The log carries meter readings, git warnings and an agent's own quoted
    words. A parser that interpreted those would report fiction about a run."""
    progress = ingest.parse_progress(
        "ingest: 1 note(s) to card\n"
        "  five_hour               43.0%  resets 2026-08-17 18:19\n"
        "  paid overage ENABLED - 74.48 EUR used\n"
        "  [1/1] scribe: n.md\n"
        "    ! no card appeared and the note is untouched - nothing happened\n"
        "      said: I could not write the file.\n"
        "            Permission was refused.\n")
    assert [(i.name, i.state) for i in progress.items] == [("n.md", "stranded")]
    assert progress.items[0].detail.startswith("no card appeared")


def test_an_empty_or_garbled_log_is_an_empty_roster_not_an_error():
    assert ingest.parse_progress("").items == []
    assert ingest.parse_progress("").total == 0
    assert ingest.parse_progress("total nonsense\nand more of it\n").items == []


class _Envelope:
    """A CLI result carrying `permission_denials`, as the real envelope does."""

    returncode = 0

    def __init__(self, denied: list) -> None:
        self.stdout = json.dumps({"result": "ok", "permission_denials": denied})
        self.stderr = ""


def test_a_refusal_names_the_tool_because_a_count_cannot_tell_the_story():
    """`acceptEdits` approves file edits and nothing else, so an agent that reaches
    for a shell command is refused, works around it, and produces a perfectly good
    card — which is what happened to the first note of the 16:10 run. A bare count
    cannot tell that from the case where the refusal *was* the reason."""
    assert ingest.denials(_Envelope([{"tool_name": "Bash"}])) == ["Bash"]
    assert ingest.denials(_Envelope([{"no_name_field": 1}])) == ["an unnamed tool"]
    assert ingest.denials(_Envelope([])) == []


def test_an_envelope_without_the_field_is_no_denials_not_a_crash():
    class _Bare:
        returncode = 0
        stdout = '{"result": "ok"}'
        stderr = ""

    assert ingest.denials(_Bare()) == []


def test_a_survivable_refusal_is_not_printed_as_a_failure():
    """`!` is this module's failure prefix, and `parse_progress` reads
    `    ! <text>` as the reason a note stranded. Announcing a refusal that way
    both alarmed the reader and mis-parsed the run."""
    log = """\
ingest: 1 note(s) to card
  [1/1] scribe: n.md
    (1 tool call(s) refused: Bash - the permission mode allows edits)
    -> n
"""
    progress = ingest.parse_progress(log)
    assert [(i.name, i.state) for i in progress.items] == [("n.md", "done")]
    assert progress.items[0].detail == "n"


# ------------------------------------- the guard the siblings already had


def _hosts(root: Path, mode: str) -> None:
    import socket
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "hosts.json").write_text(
        json.dumps({socket.gethostname(): {"permission_mode": mode}}), encoding="utf-8")


def test_a_mode_that_cannot_write_is_refused_before_a_single_dispatch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """`fix.can_dispatch` refuses a pass whose mode cannot run Bash, because "it
    would spend a round and a budget discovering that". `update.merge` refuses
    `default` with the sentence that describes what happened here: "which cannot
    edit files — the agent could read both versions and write neither." Two modules
    had the guard; this one had neither it nor the flag, and spent two runs and
    twenty-two minutes discovering it.
    """
    root = _repo(tmp_path, alpha="a")
    _hosts(root, "default")
    scribed = _routes(monkeypatch, {"alpha.md": "chore"})
    ingest.main(["--root", str(root)])

    code = ingest.main(["--root", str(root), "--write-cards"])

    assert scribed == [], "not one dispatch may be spent"
    assert code == 3
    said = capsys.readouterr().out
    assert "REFUSED before the scribe" in said
    assert "cannot write a file" in said
    assert "hosts.json" in said, "and it says where to change it"


@pytest.mark.parametrize("mode", ["default", "plan"])
def test_every_mode_that_cannot_write_is_named(tmp_path: Path, mode: str):
    """`plan` forbids edits outright, and leaving it out was the same omission one
    level down: `update.merge` checks `== "default"` alone and would dispatch a
    merge under `plan` that cannot write either."""
    root = _repo(tmp_path, alpha="a")
    _hosts(root, mode)
    assert mode in ingest.cannot_card(root)


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
def test_a_mode_that_can_write_is_not_refused(tmp_path: Path, mode: str):
    root = _repo(tmp_path, alpha="a")
    _hosts(root, mode)
    assert ingest.cannot_card(root) == ""


def test_a_mode_nobody_has_heard_of_fails_open(tmp_path: Path):
    """Enumerated rather than "not in the allowed set", so a new CLI mode does not
    silently block the verb — the scribe's own report says what happened instead."""
    root = _repo(tmp_path, alpha="a")
    _hosts(root, "someModeFromNextYear")
    assert ingest.cannot_card(root) == ""


def test_the_default_when_no_host_entry_exists_can_write(tmp_path: Path):
    """An unlisted machine must be able to card, or a fresh clone silently cannot —
    which is why the fallback here is `acceptEdits` and not `default`."""
    root = _repo(tmp_path, alpha="a")
    assert ingest.cannot_card(root) == ""


def test_classifying_is_not_gated_on_being_able_to_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`classify` reads notes and writes no card, which is why it kept working
    throughout and hid the fault."""
    root = _repo(tmp_path, alpha="a")
    _hosts(root, "default")
    _routes(monkeypatch, {"alpha.md": "chore"})
    assert ingest.main(["--root", str(root)]) == 0
    assert ingest.read_view(root).of("alpha.md").route == "chore"


def test_a_refusal_does_not_assert_a_cause_it_cannot_know(capsys):
    """The first version of the line blamed the permission mode. On the machine it
    was written for, `hosts.json` sets `bypassPermissions`, under which the mode
    refuses nothing — so the refusal came from one of the six `PreToolUse` hooks,
    each doing its job."""
    named = ingest.denials(_Envelope([{"tool_name": "Read",
                                       "message": "Board/ideas/ is private"}]))
    assert named == ["Read: Board/ideas/ is private"]
