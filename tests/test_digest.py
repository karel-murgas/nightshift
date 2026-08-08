"""Tests for `nightshift/digest.py` — the deterministic morning digest.

The digest is trusted at 7 AM without being audited, so the properties worth
pinning are (a) every claim is a real file-state lookup, and (b) the structure
answers the morning questions in the order they get asked.

Restructured 2026-07-30 (`digest-reports-the-run`): the digest reports **runs**,
read from `.ai/runs/records/`, not lane movement on the board. The regression
that forced it is the first test below — three cards failed one night, all three
bounced back to `tasks/` (the lane they came from), and a lane-diffing digest
reported "0 failed · Nothing failed" about the night that aborted because of
them. Half the tests here exist to keep a failure visible.

**Provenance.** Written in Dungeoneer's `tests/` and moved here by
`framework-tests-live-in-the-wrong-repo` (2026-08-08). One test stayed behind
under the same filename — *the live board renders without error* — because it
renders a real `Board/`, which this package does not have.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from nightshift import digest
from nightshift import run_record
from nightshift import stale_sweep


def _card(root: Path, lane: str, name: str, body: str) -> None:
    lane_dir = root / "Board" / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    (lane_dir / f"{name}.md").write_text(body, encoding="utf-8")


def _declare_attributor(root: Path, handle: str = "karel") -> None:
    """`[board].decision_attributor`, the token `digest` matches against a dated
    `## Thread` heading to spot an answered-but-not-moved card.

    Declared per-test rather than via an autouse fixture: every other test in
    this file builds a bare `tmp_path` with no `.ai/manifest.toml` at all, and
    `_decision_attributor` treats a missing manifest as "undeclared" — the same
    disable-rather-than-guess rule that governs an empty value (2026-08-04,
    `attributor-literal-was-a-silent-noop`, nightshift). Only the two tests that
    exercise the stalled-move advisory need the table.
    """
    ai_dir = root / ".ai"
    ai_dir.mkdir(parents=True, exist_ok=True)
    (ai_dir / "manifest.toml").write_text(
        f'[board]\ndecision_attributor = "{handle}"\n', encoding="utf-8")


def _parked(state: str, worker: str = "code-thread") -> str:
    return (
        f"---\nid: probe\ntitle: A parked thing\nstate: {state}\ntier: worker\n"
        f"worker: {worker}\nrecipe: none\nunattended: false\ncreated: 2026-07-23\n---\n\n"
        "## Intent\n\nDo it.\n\n"
        "## Question\n\n**Which pool does it drain?**\n\n"
        "- **A — Heat** the cheap one\n- **B — HP** the expensive one\n"
    )


def _plain(state: str, title: str, worker: str = "code-thread") -> str:
    return (
        f"---\nid: probe\ntitle: {title}\nstate: {state}\ntier: worker\n"
        f"worker: {worker}\nrecipe: none\nunattended: false\ncreated: 2026-07-23\n---\n\n## Intent\n\nx\n"
    )


def _done(title: str, worker: str = "code-thread", *, intent: str = "Do the thing.",
          summary: str | None = None, telemetry: str | None = None, lane: str = "testing") -> str:
    body = (
        f"---\nid: probe\ntitle: {title}\nstate: {lane}\ntier: worker\n"
        f"worker: {worker}\nrecipe: none\nunattended: true\ncreated: 2026-07-23\n---\n\n"
        f"## Intent\n\n{intent}\n"
    )
    if summary is not None:
        body += f"\n## Summary\n\n{summary}\n"
    if telemetry is not None:
        body += f"\n## Telemetry\n\n{telemetry}\n"
    return body


def _proposal(title: str, worker: str = "code-thread", *,
              intent: str = "Give the hint an EP cost.",
              approach: str | None = "Add an {ep} placeholder and format it in — display only.",
              subject: str | None = None) -> str:
    body = (
        f"---\nid: probe\ntitle: {title}\nstate: tasks\ntier: worker\n"
        f"worker: {worker}\nrecipe: none\nunattended: true\ncreated: 2026-07-23\n---\n\n"
        f"## Intent\n\n{intent}\n"
    )
    if approach is not None:
        body += f"\n## Approach\n\n{approach}\n"
    if subject is not None:
        body += f"\n## Subject\n\n{subject}\n"
    return body


def _task(name: str, days_old: int | None, attempts: int | None = None) -> str:
    """A `tasks/` card that was created `days_old` days ago.

    Relative to today, never a literal date: the queue section measures a wait,
    so a fixture with a hardcoded `created:` would pass on the day it was
    written and slowly turn every card starving afterwards.
    """
    created = "" if days_old is None else \
        (dt.date.today() - dt.timedelta(days=days_old)).isoformat()
    tried = f"attempts: {attempts}\n" if attempts is not None else ""
    return (
        f"---\nid: {name}\ntitle: Card {name}\nstate: tasks\ntier: worker\n"
        f"worker: code-thread\nrecipe: none\nunattended: true\n"
        f"created: {created}\n{tried}---\n\n## Intent\n\nx\n"
    )


# --- run-record fixtures -----------------------------------------------------
#
# Written as files rather than through `run_record.start()` + event calls, so a
# test can pin the exact shape the digest must survive — including shapes only a
# killed run produces (`finished: null`, a `stale` block with no `cards` key).


def _dispatch(card: str, outcome: str, *, worker: str = "code-thread", model: str = "sonnet",
              attempt: int = 1, detail: str = "", cost_usd: float = 0.0,
              landed: str = "") -> dict:
    return {"card": card, "title": "", "worker": worker, "model": model,
            "attempt": attempt, "outcome": outcome, "detail": detail,
            "cost_usd": cost_usd, "landed": landed, "at": "2026-07-30T03:00:00"}


# The default record is "a run that happened TODAY", because that is the case the
# digest's headers are normally read in — and, on 2026-07-31, because the literal
# date these defaults used to carry turned two tests into time bombs. `render`
# suffixes a run's heading with its date only when the run is not from today
# (`digest.py`: `if stamp != today`), so `## Last run 02:14–05:57` was true on
# 2026-07-30 and false every day after. Both tests passed for a calendar reason
# rather than a behavioural one, and failed the next morning.
#
# A test that needs a run from a *different* day passes `started=` explicitly —
# `test_a_run_from_a_previous_day_is_dated` is the one that asserts the dated form.
_TODAY = dt.date.today().isoformat()


def _record(root: Path, *, started: str = f"{_TODAY}T02:14:08",
            finished: str | None = f"{_TODAY}T05:57:40", complete: bool = True,
            kind: str = "run", label: str = "up to 8 card(s), staleness sweep",
            stop_reason: str | None = None, dispatched: list[dict] | None = None,
            skipped: list[tuple[str, str]] | None = None, stale: dict | None = None,
            oversized: list[tuple[str, int, int]] | None = None,
            cost_usd: float = 0.0, walls: int = 0) -> Path:
    data = {
        "started": started, "finished": finished, "complete": complete,
        "kind": kind, "label": label, "host": "testbox", "stop_reason": stop_reason,
        "cost_usd": cost_usd, "walls": walls,
        "cards_dispatched": len(dispatched or []),
        "dispatched": list(dispatched or []),
        "skipped": [{"card": c, "reason": r} for c, r in (skipped or [])],
        "oversized": [{"card": c, "bytes": b, "threshold": t}
                      for c, b, t in (oversized or [])],
        "notes": [],
    }
    if stale is not None:
        data["stale"] = stale
    out = root / run_record.DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{run_record._stamp(started)}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _run_section(out: str) -> str:
    """Everything from the first run block to the standing half."""
    return out[out.index("## Last run"):out.index("## Still waiting on you")]


def _standing(out: str) -> str:
    return out[out.index("## Still waiting on you"):]


def _sub(section: str, header: str) -> str:
    """One `###` subsection of a run block."""
    start = section.index(header)
    rest = section[start + len(header):]
    end = rest.index("\n### ") if "\n### " in rest else len(rest)
    return rest[:end]


# --- THE regression: a failure that never changed lane ----------------------

def test_a_failed_attempt_that_bounced_back_to_tasks_is_reported(tmp_path):
    """2026-07-30. Three cards were dispatched, all three failed, all three went
    straight back to `tasks/` — so the lane-diffing digest reported "0 failed ·
    Nothing failed" about the night that aborted because of them. A failure is an
    event in the run record, not a lane transition.
    """
    _card(tmp_path, "tasks", "probe", _task("probe", 2, attempts=1))
    _record(tmp_path,
            stop_reason="3 dispatches failed in a row. If this was a usage limit…",
            dispatched=[_dispatch("probe", "failed", detail="gates: boom",
                                  landed="probe: attempt 1 failed, will retry (gates: boom)")])
    out = digest.render(tmp_path)
    assert "### Failed — 1" in out
    assert "Nothing failed" not in out
    assert "back in the queue" in _run_section(out)


def test_the_banner_counts_failures_the_board_cannot_see(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, dispatched=[_dispatch(f"c{i}", "failed") for i in range(3)])
    assert "3 failed" in digest.render(tmp_path).splitlines()[2]


def test_an_aborted_run_says_so_in_its_header(tmp_path):
    (tmp_path / "Board").mkdir()
    # Dated *today*, because the heading only omits the date for a same-day run
    # (`test_a_run_from_a_previous_day_is_dated` covers the other branch). Written
    # on 2026-07-30 against `_record`'s hardcoded default, this asserted the undated
    # form and so passed for exactly one day.
    today = dt.date.today().isoformat()
    _record(tmp_path, started=f"{today}T02:14:08", finished=f"{today}T05:57:40",
            stop_reason="3 dispatches failed in a row. See `.ai/runs/`")
    out = digest.render(tmp_path)
    assert "## Last run 02:14–05:57 — aborted" in out
    assert "3 dispatches failed in a row" in out


def test_a_run_killed_before_it_finished_is_still_reported(tmp_path):
    """The 02:14 night was killed mid-sweep and never wrote a digest of its own.
    Nothing else in the system would ever have mentioned it."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, finished=None, complete=False,
            dispatched=[_dispatch("probe", "failed")])
    out = digest.render(tmp_path)
    assert "— killed" in out
    assert "02:14–?" in out
    assert "never reached its own end" in out


def test_a_clean_run_is_not_called_aborted(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, stop_reason="reached --max-cards 1")
    out = digest.render(tmp_path)
    assert "— finished" in out
    assert "aborted" not in out


def test_a_run_stopped_by_a_usage_wall_is_cut_short_not_aborted(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, stop_reason="1 session limit(s) used, which is --sessions 1", walls=1)
    out = digest.render(tmp_path)
    assert "— cut short" in out
    assert "1 usage wall(s)" in out


# --- one broken gate is one problem, not N failures -------------------------

def test_failures_sharing_a_root_cause_are_collapsed(tmp_path):
    """The night's three failures were one bug: a `doc_reference_liveness` gate
    firing on a `LICENSE` reference. Three cards, three different file paths in
    the message, one thing to fix — and the useful unit of work is the gate."""
    (tmp_path / "Board").mkdir()
    detail = ("{path}:145 — doc_reference_liveness: symbol `LICENSE` does not exist in "
              "the source tree; 1 violation(s) across 26 gate(s): asset_hygiene, audio_volume")
    _record(tmp_path, dispatched=[
        _dispatch("heavy-guard", "failed", detail=detail.format(path="a/research_notes.md")),
        _dispatch("fullscreen", "failed", detail=detail.format(path="Board/tasks/heavy.md")),
        _dispatch("stair-remnants", "failed", detail=detail.format(path="Board/tasks/full.md")),
    ])
    failed = _sub(_run_section(digest.render(tmp_path)), "### Failed — 3")
    assert "**All 3 failed the same way**" in failed
    assert "doc_reference_liveness: symbol LICENSE does not exist" in failed
    # The shared cause is stated once, not once per card.
    assert failed.count("doc_reference_liveness") == 1
    for cid in ("heavy-guard", "fullscreen", "stair-remnants"):
        assert cid in failed


def test_unrelated_failures_each_carry_their_own_cause(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, dispatched=[
        _dispatch("one", "failed", detail="a/f.md:1 — doc_reference_liveness: symbol X missing"),
        _dispatch("two", "failed", detail="tests failed: 3 failed, 900 passed"),
    ])
    failed = _sub(_run_section(digest.render(tmp_path)), "### Failed — 2")
    assert "All 2 failed the same way" not in failed
    assert "doc_reference_liveness: symbol X missing" in failed
    assert "tests failed: 3 failed, 900 passed" in failed


def test_a_card_out_of_attempts_is_distinguished_from_one_that_will_retry(tmp_path):
    """`failed` alone does not say whether the card is back in the queue or dead
    until Karel looks — which is the difference between ignoring it and not."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, dispatched=[
        _dispatch("dead", "failed", attempt=3,
                  landed="dead: → failed/ after 3 attempts (gates: boom)"),
        _dispatch("retrying", "failed", attempt=1,
                  landed="retrying: attempt 1 failed, will retry (gates: boom)"),
    ])
    failed = _sub(_run_section(digest.render(tmp_path)), "### Failed — 2")
    assert "out of attempts" in failed
    assert "back in the queue" in failed


def test_a_usage_limit_is_not_reported_as_a_card_failure(tmp_path):
    """`limited` and `blocked` give the attempt back — neither is a fact about the
    card, so neither belongs under Failed. They surface as the stop reason."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, stop_reason="this is a weekly limit; it does not reopen inside a night",
            dispatched=[_dispatch("probe", "limited", detail="usage limit")])
    out = digest.render(tmp_path)
    assert "### Failed — 0" in out
    assert "Nothing failed." in out
    assert "weekly limit" in out


# --- what needs a decision, from the run -----------------------------------

def test_a_card_the_run_parked_is_in_the_run_block_with_its_question(tmp_path):
    _card(tmp_path, "needs-decision", "probe", _parked("needs-decision"))
    _record(tmp_path, dispatched=[_dispatch("probe", "parked")])
    run = _run_section(digest.render(tmp_path))
    assert "### Needs your decision — 1" in run
    assert "Which pool does it drain?" in run
    assert "A — Heat" in run and "B — HP" in run


def test_a_reviewer_flagged_decision_counts_as_one(tmp_path):
    _card(tmp_path, "needs-decision", "probe", _parked("needs-decision"))
    _record(tmp_path, dispatched=[_dispatch("probe", "needs_decision")])
    assert "### Needs your decision — 1" in digest.render(tmp_path)


def test_a_decision_open_from_before_is_carry_over_not_this_run(tmp_path):
    """No card appears twice: the run block is what the run parked, the standing
    half is what was already open."""
    _card(tmp_path, "needs-decision", "old", _parked("needs-decision").replace("id: probe", "id: old"))
    _record(tmp_path)  # a run that parked nothing
    out = digest.render(tmp_path)
    assert "*Nothing was parked for you.*" in _run_section(out)
    assert "Decide — 1 still open" in _standing(out)
    assert "Which pool does it drain?" in _standing(out)


def test_a_card_parked_this_run_is_not_also_listed_as_carry_over(tmp_path):
    _card(tmp_path, "needs-decision", "probe", _parked("needs-decision"))
    _record(tmp_path, dispatched=[_dispatch("probe", "parked")])
    out = digest.render(tmp_path)
    assert "Decide — 0 still open" in _standing(out)


def test_a_parked_card_with_a_dated_karel_answer_is_flagged_as_a_stalled_move(tmp_path):
    _declare_attributor(tmp_path)
    body = _parked("needs-decision").replace("id: probe", "id: old") + (
        "\n## Thread\n\n### 2026-07-24 · karel\n\nDrain Heat, 3 per hit. Ship it.\n")
    _card(tmp_path, "needs-decision", "old", body)
    assert "should this have moved?" in digest.render(tmp_path)


def test_a_discussion_thread_entry_does_not_count_as_a_karel_answer(tmp_path):
    _declare_attributor(tmp_path)
    body = _parked("needs-decision").replace("id: probe", "id: old") + (
        "\n## Thread\n\n### 2026-07-24 · interactive session (Karel + Claude)\n\n"
        "Talked through the options; nothing settled yet.\n")
    _card(tmp_path, "needs-decision", "old", body)
    assert "should this have moved?" not in digest.render(tmp_path)


def test_with_no_declared_attributor_the_stalled_move_advisory_is_silent(tmp_path):
    """Regression for the bug the manifest field replaced: before
    `[board].decision_attributor` existed, this exact card — dated answer, real
    `karel` signature — was matched by a hardcoded literal. A repo with no
    manifest at all (this fixture) must not silently fall back to that literal;
    it must simply not advise, the same as a repo that declared the field empty.
    """
    body = _parked("needs-decision").replace("id: probe", "id: old") + (
        "\n## Thread\n\n### 2026-07-24 · karel\n\nDrain Heat, 3 per hit. Ship it.\n")
    _card(tmp_path, "needs-decision", "old", body)
    assert "should this have moved?" not in digest.render(tmp_path)


# --- what passed, and what Karel does with it -------------------------------

def test_a_merged_code_card_is_tagged_play_with_its_summary_cost_and_model(tmp_path):
    tel = ("- **attempt 1** · 57.0 min wall · 26.8 min in API · 74 turns · $3.70 equivalent\n"
           "- **model** · claude-sonnet-4-6 · ended `completed`\n")
    _card(tmp_path, "testing", "probe",
          _done("A feature", summary="Delayed the first payout; 9 tests; gates green.", telemetry=tel))
    _record(tmp_path, dispatched=[_dispatch("probe", "reviewed", model="sonnet-4-6",
                                            cost_usd=3.70)])
    passed = _sub(_run_section(digest.render(tmp_path)), "### Passed — 1")
    assert "**play**" in passed
    assert "Delayed the first payout" in passed
    assert "sonnet-4-6" in passed and "$3.70" in passed
    assert "57 min" in passed


def test_a_card_left_in_review_is_tagged_review(tmp_path):
    _card(tmp_path, "review", "probe", _plain("review", "A code thing"))
    _record(tmp_path, dispatched=[_dispatch("probe", "review")])
    assert "**review**" in _run_section(digest.render(tmp_path))


def test_an_art_card_is_tagged_look_not_play(tmp_path):
    _card(tmp_path, "testing", "sprite",
          _plain("testing", "A sprite", worker="art").replace("id: probe", "id: sprite"))
    _record(tmp_path, dispatched=[_dispatch("sprite", "reviewed", worker="art")])
    passed = _run_section(digest.render(tmp_path))
    assert "**look**" in passed
    assert "**play**" not in passed


def test_the_account_falls_back_to_intent_without_a_summary(tmp_path):
    _card(tmp_path, "testing", "probe",
          _done("Legacy", intent="Make the interrupted worker survive."))
    _record(tmp_path, dispatched=[_dispatch("probe", "reviewed")])
    assert "Make the interrupted worker survive." in _run_section(digest.render(tmp_path))


def test_a_card_already_filed_to_done_still_reports_as_passed(tmp_path):
    """Karel can play a card and file it before he reads the digest. The run still
    landed it, and the report must not silently drop it."""
    _card(tmp_path, "done", "probe", _done("A feature", lane="done", summary="Shipped."))
    _record(tmp_path, dispatched=[_dispatch("probe", "reviewed")])
    passed = _sub(_run_section(digest.render(tmp_path)), "### Passed — 1")
    assert "Shipped." in passed


def test_a_card_deleted_since_the_run_falls_back_to_its_id(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, dispatched=[_dispatch("vanished", "reviewed")])
    assert "`vanished`" in _run_section(digest.render(tmp_path))


def test_sublines_are_nested_bullets_not_plain_indented_text(tmp_path):
    """Regression (Karel, 2026-07-27): a 4-space plain-text continuation under a
    tight list item breaks Obsidian's rendering of the rest of the list."""
    import re as _re
    _card(tmp_path, "testing", "probe", _done("A feature", summary="Did the thing."))
    _record(tmp_path, dispatched=[_dispatch("probe", "reviewed")])
    run = _run_section(digest.render(tmp_path))
    assert "\n    - Did the thing." in run
    assert not _re.search(r"^    [^ \-]", run, _re.M), "a 4-space plain-text subline leaked in"


def test_angle_brackets_in_a_summary_are_escaped_not_left_as_html(tmp_path):
    """Regression (Karel, 2026-07-28): a summary naming `ai/<id>` reads to
    Obsidian as an unclosed HTML tag and swallows every list item after it."""
    _card(tmp_path, "testing", "probe",
          _done("A feature", summary="Moves a card once gates pass on ai/<id>, then stops."))
    _record(tmp_path, dispatched=[_dispatch("probe", "reviewed")])
    run = _run_section(digest.render(tmp_path))
    assert "&lt;id&gt;" in run
    assert "<id>" not in run


def test_an_html_comment_is_never_shown_as_the_summary(tmp_path):
    _card(tmp_path, "testing", "probe",
          _done("Commented", intent="<!-- stale-ok: markup only -->\nThe real intent sentence."))
    _record(tmp_path, dispatched=[_dispatch("probe", "reviewed")])
    run = _run_section(digest.render(tmp_path))
    assert "The real intent sentence." in run
    assert "stale-ok" not in run


# --- the stale hunter, including the failure mode that read as silence ------

def test_a_sweep_that_produced_nothing_says_so_loudly(tmp_path):
    """2026-07-30: 58 docs selected, every verdict incomplete, nothing ledgered —
    two and a half hours that bought nothing, reported by the old digest as
    "staleness swept, nothing to check"."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, stale={"selected": 58, "checked": 27, "verified": 0,
                             "carded": 0, "incomplete": 27, "cards": []})
    stale = _sub(_run_section(digest.render(tmp_path)), "### Stale hunter")
    assert "58 doc(s) selected" in stale
    assert "27 returned no usable verdict" in stale
    assert "**The sweep produced nothing.**" in stale


def test_a_sweep_that_verified_docs_is_not_called_broken(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, stale={"selected": 1, "checked": 1, "verified": 1,
                             "carded": 0, "incomplete": 0, "cards": []})
    stale = _sub(_run_section(digest.render(tmp_path)), "### Stale hunter")
    assert "1 verified" in stale
    assert "produced nothing" not in stale


def test_the_sweeps_fix_cards_are_named_with_what_they_intend_to_do(tmp_path):
    body = _proposal("Fix stale arch_detail.md",
                     intent="arch_detail.md names source that has drifted.",
                     approach="Reconcile the doc with the code, one finding at a time.")
    _card(tmp_path, "tasks", "fix-stale-arch-detail",
          body.replace("id: probe", "id: fix-stale-arch-detail"))
    _record(tmp_path, stale={"selected": 1, "checked": 1, "verified": 1, "carded": 1,
                             "incomplete": 0, "cards": ["fix-stale-arch-detail"]})
    stale = _sub(_run_section(digest.render(tmp_path)), "### Stale hunter")
    assert "arch_detail.md names source that has drifted." in stale
    assert "→ Reconcile the doc with the code" in stale


def test_a_run_without_the_sweep_says_it_did_not_run(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path)  # no `stale` block at all
    assert "*Did not run —" in _sub(_run_section(digest.render(tmp_path)), "### Stale hunter")


def test_a_sweep_with_nothing_to_check_is_distinguished_from_one_that_never_ran(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, stale={"selected": 0, "checked": 0, "verified": 0, "carded": 0})
    stale = _sub(_run_section(digest.render(tmp_path)), "### Stale hunter")
    assert "nothing had changed since its last verification" in stale
    assert "Did not run" not in stale


# --- why a card did NOT run -------------------------------------------------

def test_skipped_cards_are_grouped_by_reason(tmp_path):
    """Five art cards blocked on a capability this host lacks is one fact about
    the host, not five about the cards."""
    (tmp_path / "Board").mkdir()
    gpu = "requires: gpu-box — Mithlond does not declare it (.ai/hosts.json)"
    human = "unattended: false — declared as needing a human (03_board.md §2)"
    _record(tmp_path, skipped=[(f"menu-art-{n}", gpu) for n in ("a", "b", "c", "d", "e")]
            + [("hack-colors", human)])
    skipped = _sub(_run_section(digest.render(tmp_path)), "### Skipped — 6")
    assert "**5** — requires: gpu-box" in skipped
    assert "**1** — unattended: false" in skipped
    assert "`menu-art-a`, `menu-art-b`" in skipped


def test_a_run_that_skipped_nothing_says_so(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path)
    assert "Every card on the board was dispatchable" in digest.render(tmp_path)


# --- a card that DID run, while grown past useful worker input ---------------

def test_a_card_dispatched_while_oversized_is_reported(tmp_path):
    """Round 2 of `oversized-cards-are-bad-worker-input`. The signal was built to
    land "where he already looks every morning", and this is that place — but an
    oversized card is dispatchable by design, so it is absent from `skipped`, and
    the digest said nothing at all about the one case the feature exists for: a
    card dispatched again and again while it grows."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, oversized=[("menu-unlock-indicators", 16_309, 14 * 1024)])
    block = _sub(_run_section(digest.render(tmp_path)), "### Oversized — 1")
    assert "`menu-unlock-indicators`" in block
    assert "15.9 KB" in block          # its own size
    assert "over 14 KB" in block       # the threshold it passed


def test_the_oversized_block_says_what_to_do_not_just_that_a_number_was_passed(tmp_path):
    """The acceptance criterion in as many words. A digest line naming a number
    Karel cannot act on is a line he learns to skip — it has to carry the two
    remedies and the name of the constant to change."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, oversized=[("fat", 20_000, 14 * 1024)])
    block = _sub(_run_section(digest.render(tmp_path)), "### Oversized — 1")
    assert "compact" in block and "split" in block
    assert "CARD_COMFORT_BYTES" in block


def test_an_oversized_card_is_never_counted_among_the_skipped(tmp_path):
    """The reason this is its own field rather than an entry in `skipped`. The
    card ran; a `### Skipped — 1` heading and an "Every card on the board was
    dispatchable" fallback that disagreed with each other would make the section
    Karel checks first untrustworthy, to report something that is not a skip."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, oversized=[("fat", 20_000, 14 * 1024)])
    run = _run_section(digest.render(tmp_path))
    assert "### Skipped — 0" in run
    assert "Every card on the board was dispatchable" in _sub(run, "### Skipped — 0")
    assert "fat" not in _sub(run, "### Skipped — 0")


def test_a_run_with_nothing_oversized_grows_no_section(tmp_path):
    """Unlike the five sections above it, this one is a nudge and not one of the
    morning's standing questions — and the threshold was chosen to clear a normal
    dense card, so silence is the honest steady state. A permanent "*nothing was
    over the threshold*" line is a fixture reporting a non-event."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path)
    assert "### Oversized" not in digest.render(tmp_path)


def test_a_record_written_before_the_field_existed_still_renders(tmp_path):
    """The digest reads records it did not write, including ones from runs older
    than this change — `.ai/runs/records/` keeps thirty."""
    (tmp_path / "Board").mkdir()
    path = _record(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["oversized"]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert "### Oversized" not in digest.render(tmp_path)


# --- daytime work is not unattended work ------------------------------------

def test_a_card_karel_moved_himself_is_never_reported_as_run_output(tmp_path):
    """Karel, 2026-07-30: "Daytime work should not go into digest." The old
    baseline was the last *digest* commit, so two days of his own interactive
    work got reported as things the night had finished."""
    _card(tmp_path, "done", "handmade", _done("Karel did this himself", lane="done",
                                              summary="Finished by hand at 15:00."))
    _record(tmp_path, dispatched=[_dispatch("probe", "reviewed")])
    _card(tmp_path, "testing", "probe", _done("The run did this", summary="Run output."))
    out = digest.render(tmp_path)
    assert "Karel did this himself" not in out
    assert "Finished by hand" not in out
    assert "The run did this" in out


def test_no_records_at_all_says_so_rather_than_inventing_a_report(tmp_path):
    (tmp_path / "Board").mkdir()
    _init_repo(tmp_path)
    _commit_all(tmp_path, "digest: 2026-07-20")
    out = digest.render(tmp_path)
    assert "No run has recorded itself since the 2026-07-20 digest" in out
    assert "## Last run" not in out


def test_the_banner_says_first_digest_with_no_git(tmp_path):
    (tmp_path / "Board").mkdir()
    assert "**First digest**" in digest.render(tmp_path).splitlines()[2]


# --- more than one run since the last digest ---------------------------------

def test_two_runs_are_reported_newest_first(tmp_path):
    """2026-07-30 had two: an 8-card night that aborted at 03:20, and a manual
    one-card rerun at 12:05. A report that cannot tell them apart explains
    neither."""
    (tmp_path / "Board").mkdir()
    # Both runs are dated TODAY, so the headings carry times only — the same reason
    # `_record`'s defaults are today-relative. Pinning the literal 2026-07-30 here
    # is what made this test pass on one calendar day and fail on the rest.
    _record(tmp_path, started=f"{_TODAY}T02:14:08", finished=f"{_TODAY}T05:57:40",
            complete=False, dispatched=[_dispatch("night-card", "failed")])
    _record(tmp_path, started=f"{_TODAY}T12:05:09", finished=f"{_TODAY}T13:10:51",
            kind="card", label="card bleed-regen", stop_reason="reached --max-cards 1",
            dispatched=[_dispatch("bleed-regen", "reviewed")])
    out = digest.render(tmp_path)
    assert "2 recorded run(s)" in out.splitlines()[2]   # no git repo here, so no baseline date
    assert out.index("## Last run 12:05–13:10") < out.index("## Earlier run 02:14–05:57")
    assert "card bleed-regen" in out


def test_a_run_from_a_previous_day_is_dated(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path, started="2026-07-29T22:00:00", finished="2026-07-30T02:00:00")
    assert "## Last run 2026-07-29 22:00–02:00" in digest.render(tmp_path)


def test_many_runs_collapse_to_a_tail_rather_than_a_wall(tmp_path):
    (tmp_path / "Board").mkdir()
    for i in range(digest._RUNS_SHOWN + 3):
        _record(tmp_path, started=f"2026-07-30T{i + 1:02d}:00:00",
                finished=f"2026-07-30T{i + 1:02d}:30:00")
    out = digest.render(tmp_path)
    assert out.count("### Failed —") == digest._RUNS_SHOWN
    assert "## 3 older run(s) since the last digest" in out


# --- the standing half: terse, and labelled as carry-over -------------------

def test_testing_and_review_are_a_count_not_a_list_of_summaries(tmp_path):
    """The old design listed every `testing/` card with its summary and cost here,
    duplicating a Kanban lane Karel is already looking at and pushing the actual
    news off the top of the report."""
    for i in range(6):
        _card(tmp_path, "testing", f"t{i}", _done(f"Old feature {i}", summary=f"Summary {i}."))
    _record(tmp_path)
    standing = _standing(digest.render(tmp_path))
    assert "6 in `testing/` to play" in standing
    assert "Summary 0." not in standing
    assert "Old feature 0" not in standing


def test_the_failed_lane_carries_each_cards_recorded_error(tmp_path):
    body = _plain("failed", "A broken thing") + "\n## Error\n\nAssertionError: expected 3 got 5\n"
    _card(tmp_path, "failed", "probe", body)
    standing = _standing(digest.render(tmp_path))
    assert "In `failed/` — 1" in standing
    assert "AssertionError: expected 3 got 5" in standing


def test_a_failed_lane_card_without_an_error_says_so(tmp_path):
    _card(tmp_path, "failed", "probe", _plain("failed", "Mystery"))
    assert "no error recorded" in digest.render(tmp_path)


def test_the_standing_half_is_labelled_carry_over(tmp_path):
    (tmp_path / "Board").mkdir()
    assert "Carry-over from earlier runs" in digest.render(tmp_path)


def test_run_blocks_come_before_the_standing_half(tmp_path):
    (tmp_path / "Board").mkdir()
    _record(tmp_path)
    out = digest.render(tmp_path)
    assert out.index("## Last run") < out.index("## Still waiting on you")


# --- the queue, and the starvation it would otherwise hide ------------------

def test_an_empty_tasks_lane_says_so(tmp_path):
    (tmp_path / "Board").mkdir()
    out = digest.render(tmp_path)
    assert "**Queue — 0 in `tasks/`**" in out
    assert "Empty — nothing waiting" in out


def test_never_dispatched_cards_are_listed_longest_wait_first(tmp_path):
    _card(tmp_path, "tasks", "fresh", _task("fresh", 0))
    _card(tmp_path, "tasks", "old", _task("old", 30))
    _card(tmp_path, "tasks", "middling", _task("middling", 5))
    queue = _standing(digest.render(tmp_path))
    assert queue.index("Card old") < queue.index("Card middling") < queue.index("Card fresh")
    assert "waiting 30 days" in queue


def test_a_card_that_has_been_attempted_is_not_counted_as_never_dispatched(tmp_path):
    _card(tmp_path, "tasks", "tried", _task("tried", 20, attempts=2))
    _card(tmp_path, "tasks", "untouched", _task("untouched", 1))
    out = digest.render(tmp_path)
    assert "1 never dispatched" in out
    assert "Card untouched" in out
    assert "1 card(s) have been attempted and are back in the queue" in out


def test_a_starving_queue_is_announced_before_the_cards_it_is_about(tmp_path):
    _card(tmp_path, "tasks", "forgotten", _task("forgotten", digest._STARVING_DAYS + 3))
    out = digest.render(tmp_path)
    assert f"**1 card(s) have waited {digest._STARVING_DAYS}+ days**" in out
    assert f"({digest._STARVING_DAYS + 3} days ago)" in out
    assert out.index("have waited") < out.index("Card forgotten")


def test_a_young_queue_is_not_called_starving(tmp_path):
    _card(tmp_path, "tasks", "recent", _task("recent", digest._STARVING_DAYS - 1))
    out = digest.render(tmp_path)
    assert "have waited" not in out
    assert "1 never dispatched" in out


def test_a_long_queue_is_truncated_rather_than_dumped(tmp_path):
    for i in range(digest._QUEUE_SHOWN + 4):
        _card(tmp_path, "tasks", f"c{i:02d}", _task(f"c{i:02d}", i))
    out = digest.render(tmp_path)
    assert _standing(out).count("- [[") == digest._QUEUE_SHOWN
    assert "…and 4 more." in out


def test_a_card_without_a_created_date_sorts_last_and_says_why(tmp_path):
    _card(tmp_path, "tasks", "undated", _task("undated", None))
    _card(tmp_path, "tasks", "dated", _task("dated", 2))
    queue = _standing(digest.render(tmp_path))
    assert queue.index("Card dated") < queue.index("Card undated")
    assert "no `created:` date" in queue


# --- the two advisories that explain why work is NOT happening --------------

def test_an_answered_card_still_flagged_unattended_false_is_called_out(tmp_path):
    _declare_attributor(tmp_path)
    body = _task("answered", 3).replace("unattended: true", "unattended: false") + (
        "\n## Thread\n\n### 2026-07-27 · karel\n\nOption A. Go.\n")
    _card(tmp_path, "tasks", "answered", body)
    out = digest.render(tmp_path)
    assert "**Flag may be stale:**" in out
    assert "`answered`" in out


def test_an_unanswered_unattended_card_is_not_flagged(tmp_path):
    _card(tmp_path, "tasks", "waiting",
          _task("waiting", 3).replace("unattended: true", "unattended: false"))
    assert "Flag may be stale" not in digest.render(tmp_path)


def _correction(slug: str, days_old: int = 0, resolved: bool = False) -> str:
    date = (dt.date.today() - dt.timedelta(days=days_old)).isoformat()
    note = "a note long enough to clear the forty character minimum here"
    disposition = " [[disposition: gate: x]]" if resolved else ""
    return f"{date} | {slug} | silent-noop | claude | n/a | {note}{disposition}\n"


def _corrections_log(root: Path, body: str) -> None:
    log = root / ".ai" / "corrections.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(body, encoding="utf-8")


def test_no_corrections_log_at_all_is_silent(tmp_path):
    (tmp_path / "Board").mkdir()
    assert "Harvest backlog" not in digest.render(tmp_path)


def test_a_small_fresh_backlog_does_not_nudge(tmp_path):
    (tmp_path / "Board").mkdir()
    _corrections_log(tmp_path, "".join(_correction(f"s{i}") for i in range(3)))
    assert "Harvest backlog" not in digest.render(tmp_path)


def test_a_large_count_triggers_the_nudge(tmp_path):
    (tmp_path / "Board").mkdir()
    _corrections_log(tmp_path, "".join(_correction(f"s{i}")
                                       for i in range(digest._HARVEST_BACKLOG_COUNT)))
    out = digest.render(tmp_path)
    assert "**Harvest backlog:**" in out
    assert f"{digest._HARVEST_BACKLOG_COUNT} correction(s)" in out


def test_a_single_old_entry_triggers_the_nudge_on_age_alone(tmp_path):
    (tmp_path / "Board").mkdir()
    _corrections_log(tmp_path, _correction("ancient", days_old=digest._HARVEST_BACKLOG_DAYS + 5))
    out = digest.render(tmp_path)
    assert "**Harvest backlog:**" in out
    assert f"({digest._HARVEST_BACKLOG_DAYS + 5} days ago)" in out


def test_resolved_entries_do_not_count_toward_the_backlog(tmp_path):
    (tmp_path / "Board").mkdir()
    _corrections_log(tmp_path, "".join(
        _correction(f"s{i}", resolved=True) for i in range(digest._HARVEST_BACKLOG_COUNT + 5)))
    assert "Harvest backlog" not in digest.render(tmp_path)


# --- prose extraction, unchanged by the restructure -------------------------

def test_the_question_line_is_the_sentence_not_a_mid_sentence_bold(tmp_path):
    """Regression: a picker bolds its options and often a word mid-question, so
    'first bold span' grabbed the wrong text ('and' from 'Pick one **and**...')."""
    body = _plain("needs-decision", "T").replace(
        "## Intent\n\nx\n",
        "## Intent\n\nx\n\n## Question\n\n"
        "Which pool does it drain, and how much? Pick a letter **and** an amount.\n\n"
        "- **A — HP** big\n- **B — heat** small\n",
    )
    _card(tmp_path, "needs-decision", "probe", body)
    out = digest.render(tmp_path)
    assert "Which pool does it drain, and how much?" in out
    assert "— and\n" not in out  # the old bug


def test_a_wrapped_option_is_joined_before_clipping(tmp_path):
    body = _plain("needs-decision", "T").replace(
        "## Intent\n\nx\n",
        "## Intent\n\nx\n\n## Question\n\n**Which?**\n\n"
        "- **A — HP** the literal reading that\n  continues onto a second line here\n",
    )
    _card(tmp_path, "needs-decision", "probe", body)
    assert "continues onto a second line" in digest.render(tmp_path)


def test_a_multi_decision_card_lists_its_subquestions_not_every_option(tmp_path):
    """A polished plan batches several decisions as bold sub-question headers
    with A/B under each. The digest must show the headers (what is being
    decided), not flatten all the A/B options into a wall."""
    body = _plain("needs-decision", "T").replace(
        "## Intent\n\nx\n",
        "## Intent\n\nx\n\n## Question\n\nThree calls, batched.\n\n"
        "**1 — Which screens?**\n- **A — hub** *(recommended)*\n- **B — everywhere**\n\n"
        "**2 — Layout?**\n- **A — cards** *(recommended)*\n- **B — grid**\n\n"
        "**3 — Scope?**\n- **A — one image** *(recommended)*\n- **B — full reskin**\n",
    )
    _card(tmp_path, "needs-decision", "probe", body)
    out = digest.render(tmp_path)
    for token in ("1 — Which screens?", "2 — Layout?", "3 — Scope?",
                  "A — hub", "B — everywhere", "B — grid", "*(recommended)*",
                  "Three calls, batched."):
        assert token in out


def test_a_wrapped_decision_option_is_joined_not_cut_at_the_source_linebreak(tmp_path):
    """Regression (Karel, 2026-07-28): a multi-decision card's option was shown
    as only its first *source* line, so an option the card wrapped dangled
    mid-sentence."""
    body = _plain("needs-decision", "T").replace(
        "## Intent\n\nx\n",
        "## Intent\n\nx\n\n## Question\n\nTwo calls.\n\n"
        "**1 — First?**\n- **A — yes** because the pipeline does not depend on what she\n"
        "  says, only on whether it exists\n- **B — no**\n\n"
        "**2 — Second?**\n- **A — go** *(recommended)*\n- **B — wait**\n",
    )
    _card(tmp_path, "needs-decision", "probe", body)
    out = digest.render(tmp_path)
    assert "does not depend on what she says, only on whether it exists" in out
    assert "what she\n" not in out


def test_clip_clean_ends_on_a_full_sentence_and_ignores_abbreviations():
    s = ("This opening sentence is comfortably past the sixty-character floor here. "
         "And a second one that pushes the whole string over the budget.")
    assert digest._clip_clean(s, 100) == \
        "This opening sentence is comfortably past the sixty-character floor here."
    abbrev = "It arms on the next round, i.e. one round later than the tooltip would suggest."
    assert not digest._clip_clean(abbrev, 60).endswith("i.e.")


# --- determinism and the real board -----------------------------------------

def test_the_render_is_byte_identical_across_two_runs(tmp_path):
    """A report that reshuffles itself cannot be diffed against yesterday's."""
    for name, age in (("b", 3), ("a", 3), ("c", 1)):
        _card(tmp_path, "tasks", name, _task(name, age))
    _record(tmp_path, dispatched=[_dispatch("a", "failed"), _dispatch("b", "reviewed")],
            skipped=[("c", "unattended: false — needs a human")])
    assert digest.render(tmp_path) == digest.render(tmp_path)


def test_readmes_do_not_appear_as_cards(tmp_path):
    (tmp_path / "Board" / "review").mkdir(parents=True)
    (tmp_path / "Board" / "review" / "README.md").write_text("the lane", encoding="utf-8")
    assert "the lane" not in digest.render(tmp_path)


def test_a_malformed_record_is_skipped_not_raised_on(tmp_path):
    """A run killed mid-`save` leaves a half-written file. One run's evidence is
    lost; the digest's job is to report the rest."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, started="2026-07-30T02:00:00",
            dispatched=[_dispatch("good", "reviewed")])
    bad = tmp_path / run_record.DIR / "20260730-030000.json"
    bad.write_text('{"started": "2026-07-30T03:00:00", "dispat', encoding="utf-8")
    out = digest.render(tmp_path)
    assert "1 run(s)" in out or "1 recorded run(s)" in out
    assert "`good`" in out or "good" in out


# --- the window is the last digest commit, not the last calendar day ---------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)


def test_a_run_already_reported_by_the_last_digest_is_not_reported_again(tmp_path):
    """Otherwise every morning re-reports the same night forever."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, started="2020-01-01T02:00:00", finished="2020-01-01T03:00:00",
            dispatched=[_dispatch("ancient", "failed")])
    _init_repo(tmp_path)
    _commit_all(tmp_path, "digest: 2026-07-29")   # committed now, after that record
    out = digest.render(tmp_path)
    assert "ancient" not in out
    assert "No run has recorded itself" in out


def test_a_run_after_the_last_digest_is_reported(tmp_path):
    (tmp_path / "Board").mkdir()
    _init_repo(tmp_path)
    _commit_all(tmp_path, "digest: 2026-07-29")
    _record(tmp_path, started=(dt.datetime.now() + dt.timedelta(minutes=1)).isoformat(
        timespec="seconds"), dispatched=[_dispatch("fresh", "failed")])
    out = digest.render(tmp_path)
    assert "### Failed — 1" in out
    assert "fresh" in out


# --- append mode: the read baseline only advances on a real `digest:` commit ---
#
# `runner.py --append-digest` (Karel, 2026-07-30: "for when I don't have time to
# read or use a scheduler over weekend") writes `Digest.md` and commits it, but
# under `f"{digest.APPEND_COMMIT_PREFIX}: <date>"` rather than `f"digest: <date>"`.
# The whole mechanism rests on `_DIGEST_COMMIT` not matching that message — these
# tests pin the contract at the digest.py level, independent of runner.py ever
# calling it correctly (that's `test_board_runner.py`'s job).

def _iso(offset_minutes: int) -> str:
    """A timestamp `offset_minutes` from now.

    Not used to interleave with any *particular* commit — git commits in these
    tests all land at the real wall-clock "now" the subprocess runs, which is
    sub-second apart regardless of how the test source orders the calls. Instead
    each test uses one big gap (tens of minutes) to separate "definitely before
    every commit in this test" from "definitely after all of them", so the
    assertion never races against how fast the test executes.
    """
    return (dt.datetime.now() + dt.timedelta(minutes=offset_minutes)).isoformat(
        timespec="seconds")


def test_an_append_commit_does_not_advance_the_window(tmp_path):
    (tmp_path / "Board").mkdir()
    _init_repo(tmp_path)
    _record(tmp_path, started=_iso(-100), finished=_iso(-90),
            dispatched=[_dispatch("friday", "failed")])
    _commit_all(tmp_path, "digest: 2026-07-25")   # the last real, acknowledged digest

    # Saturday and Sunday both ran unattended with --append-digest.
    _record(tmp_path, started=_iso(50), finished=_iso(60),
            dispatched=[_dispatch("saturday", "failed")])
    _commit_all(tmp_path, f"{digest.APPEND_COMMIT_PREFIX}: 2026-07-26")
    _record(tmp_path, started=_iso(70), finished=_iso(80),
            dispatched=[_dispatch("sunday", "failed")])
    _commit_all(tmp_path, f"{digest.APPEND_COMMIT_PREFIX}: 2026-07-27")

    out = digest.render(tmp_path)
    # Both weekend nights show up — the window still reaches back to Friday...
    assert "saturday" in out and "sunday" in out
    assert "2 run(s) since the 2026-07-25 digest" in out.splitlines()[2]
    # ...and Friday itself, already shown on the 25th, is not repeated.
    assert "friday" not in out


def test_a_real_digest_commit_after_append_runs_closes_the_window(tmp_path):
    """The morning Karel actually runs the runner himself, the backlog closes —
    and a run after *that* reports only itself."""
    (tmp_path / "Board").mkdir()
    _init_repo(tmp_path)
    _record(tmp_path, started=_iso(-100), finished=_iso(-90),
            dispatched=[_dispatch("saturday", "failed")])
    _commit_all(tmp_path, f"{digest.APPEND_COMMIT_PREFIX}: 2026-07-26")
    _record(tmp_path, started=_iso(-80), finished=_iso(-70),
            dispatched=[_dispatch("monday", "failed")])
    _commit_all(tmp_path, "digest: 2026-07-28")   # Karel is at the keyboard; baseline closes

    _record(tmp_path, started=_iso(50), finished=_iso(60),
            dispatched=[_dispatch("tuesday", "failed")])
    out = digest.render(tmp_path)
    assert "tuesday" in out
    assert "saturday" not in out and "monday" not in out


def test_the_kill_switch_is_not_reported_as_a_defect(tmp_path):
    """Karel dropped `.ai/STOP` himself. The run not finishing is the switch
    working, so it must not be counted among the runs that "did not finish
    cleanly" — that banner line exists to make him look at something."""
    (tmp_path / "Board").mkdir()
    _record(tmp_path, stop_reason="kill switch appeared mid-run")
    out = digest.render(tmp_path)
    assert "— stopped by you" in out
    assert "aborted" not in out
    assert "did not finish cleanly" not in out
