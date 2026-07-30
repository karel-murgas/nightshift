"""Tests for `nightshift/limits.py` — recognising the usage-limit wall.

Two properties carry the weight here, and they pull against each other.

**A wall must be recognised**, because the alternative is the bug this module
was written for: the CLI exits non-zero, the runner reads it as the card
failing, `attempts` is spent, and one wall at 02:00 walks the whole queue.

**A non-wall must not be**, because a false positive stops the night for
nothing — and the text being scanned includes the worker's own final message, so
a card about rate limiting is a real thing that can appear in it.

The second is why every scan is gated on a non-zero exit, and why the false
positive cases below are as numerous as the true ones.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from nightshift import limits

NOW = dt.datetime(2026, 7, 23, 2, 0, 0)


# --- what counts as a wall --------------------------------------------------

def test_the_machine_readable_form_is_recognised_with_its_reset_time():
    epoch = int(dt.datetime(2026, 7, 23, 7, 0, 0).timestamp())
    wall = limits.detect(1, f"Claude AI usage limit reached|{epoch}", now=NOW)
    assert wall is not None
    assert wall.scope == limits.SESSION
    assert wall.resets_at == dt.datetime(2026, 7, 23, 7, 0, 0)


def test_prose_forms_are_recognised():
    for text in (
        "Error: usage limit reached",
        "You have reached your usage limit for this 5-hour window",
        "API Error: rate_limit_error",
        "429 Too Many Requests",
        "usage limit exceeded",
    ):
        assert limits.detect(1, text, now=NOW) is not None, text


def test_a_wall_with_no_reset_time_is_still_a_wall():
    """Knowing we are blocked matters more than knowing until when — the caller
    falls back to a whole session window rather than treating it as a failure."""
    wall = limits.detect(1, "Error: usage limit reached", now=NOW)
    assert wall is not None and wall.resets_at is None
    assert limits.resume_at(wall, NOW) == NOW + dt.timedelta(
        hours=limits.SESSION_HOURS, minutes=limits.GRACE_MINUTES)


def test_the_evidence_line_is_kept_for_the_run_log():
    wall = limits.detect(1, "some noise\nError: usage limit reached, try later\nmore noise",
                         now=NOW)
    assert "usage limit reached, try later" in wall.evidence


# --- what does not ----------------------------------------------------------

def test_a_successful_run_is_never_a_wall_whatever_it_says():
    """The gate that makes a phrase list safe. `stdout` carries the worker's own
    summary, so a card that *implemented* rate limiting says so in the text —
    and a worker that exited 0 did not hit anything."""
    assert limits.detect(0, "Added rate limit handling and a usage limit reached banner") is None


def test_an_ordinary_failure_is_not_a_wall():
    assert limits.detect(1, "AssertionError: expected 3 got 5", "traceback…") is None


def test_a_timeout_exit_code_alone_is_not_a_wall():
    assert limits.detect(124, "", "") is None


# --- session vs weekly ------------------------------------------------------

def test_a_weekly_limit_is_marked_as_one_and_is_not_waited_out():
    """A weekly window does not reopen inside a night. Sleeping on it would idle
    until morning and produce nothing, so the caller has to be able to tell the
    two apart."""
    wall = limits.detect(1, "You have reached your weekly usage limit", now=NOW)
    assert wall.scope == limits.WEEKLY
    assert not wall.waits_out


def test_the_monthly_spend_cap_is_recognised_and_not_waited_out():
    """The exact 429 that ended the 2026-07-24 night: "You've hit your monthly
    spend limit". None of the SESSION/WEEKLY wording appears in it, so before
    this phrase existed it fell through to `failed` and cost three cards an
    attempt each. It must not wait out — a billing cap lifts at the reset or by
    hand, never inside a night."""
    wall = limits.detect(
        1, "You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
        now=NOW)
    assert wall.scope == limits.MONTHLY
    assert not wall.waits_out


def test_a_spend_limit_is_not_read_as_a_transient_hiccup():
    """Delivered as a 429, but retrying into a hard billing wall every five
    minutes until morning is the exact failure the scope split exists to stop."""
    wall = limits.detect(1, "api_error 429: You have hit your spend limit", now=NOW)
    assert wall.scope == limits.MONTHLY
    assert not wall.waits_out


def test_a_session_limit_is_waited_out():
    wall = limits.detect(1, "usage limit reached for this session", now=NOW)
    assert wall.scope == limits.SESSION
    assert wall.waits_out
    assert wall.spends_a_session


def test_a_bare_429_is_transient_and_does_not_spend_a_session():
    """A service-side rate limit reopens in seconds. Reading one as the plan
    window closing would throw away five hours of a good night over a hiccup."""
    wall = limits.detect(1, "API Error: 429 Too Many Requests")
    assert wall.scope == limits.TRANSIENT
    assert wall.waits_out and not wall.spends_a_session
    assert limits.resume_at(wall, NOW) == NOW + dt.timedelta(
        minutes=limits.TRANSIENT_MINUTES + limits.GRACE_MINUTES)


def test_plan_exhaustion_delivered_as_a_rate_limit_error_is_not_transient():
    """The case that decides the precedence. Plan exhaustion is sometimes
    delivered *as* a `rate_limit_error` with usage-limit prose attached; reading
    that as a hiccup would retry into the same wall every five minutes until
    morning."""
    wall = limits.detect(1, "rate_limit_error: Claude AI usage limit reached", now=NOW)
    assert wall.scope == limits.SESSION
    assert wall.spends_a_session


# --- reading the reset time -------------------------------------------------

def test_an_iso_reset_time_is_read():
    wall = limits.detect(1, "usage limit reached; resets at 2026-07-23T06:30:00", now=NOW)
    assert wall.resets_at == dt.datetime(2026, 7, 23, 6, 30)


def test_a_bare_clock_time_resolves_to_the_next_occurrence():
    wall = limits.detect(1, "usage limit reached, resets at 4am", now=NOW)
    assert wall.resets_at == dt.datetime(2026, 7, 23, 4, 0)


def test_a_clock_time_already_past_today_rolls_to_tomorrow():
    """Read at 02:00, "resets at 1am" cannot mean an hour ago. Rolling forward is
    the only reading that never produces a reset in the past — and a reset in the
    past reads as "the window is open" and costs a card to disprove."""
    wall = limits.detect(1, "usage limit reached, resets at 1am", now=NOW)
    assert wall.resets_at == dt.datetime(2026, 7, 24, 1, 0)


def test_pm_is_read_as_the_afternoon():
    wall = limits.detect(1, "usage limit reached, resets at 3:30pm", now=NOW)
    assert wall.resets_at == dt.datetime(2026, 7, 23, 15, 30)


def test_a_reset_time_in_the_past_is_treated_as_unknown():
    """A stale timestamp from an earlier attempt in the same log is likelier than
    a window that reopened while we were reading about it."""
    epoch = int(dt.datetime(2026, 7, 23, 1, 0, 0).timestamp())
    wall = limits.detect(1, f"usage limit reached|{epoch}", now=NOW)
    assert wall is not None and wall.resets_at is None


def test_millisecond_epochs_are_not_read_as_the_year_5138():
    epoch_ms = int(dt.datetime(2026, 7, 23, 7, 0, 0).timestamp()) * 1000
    wall = limits.detect(1, f"usage limit reached|{epoch_ms}", now=NOW)
    assert wall.resets_at == dt.datetime(2026, 7, 23, 7, 0, 0)


def test_resume_is_a_little_after_the_reset_not_on_it():
    """Waking on the exact second the window lifts is racing the service's own
    clock, and losing costs a card's attempt to find out."""
    wall = limits.detect(1, "usage limit reached; resets at 2026-07-23T06:30:00", now=NOW)
    assert limits.resume_at(wall, NOW) == dt.datetime(2026, 7, 23, 6, 30) + \
        dt.timedelta(minutes=limits.GRACE_MINUTES)


# --- the module stays deterministic -----------------------------------------

def test_no_subprocess_or_network_in_here():
    """`00_architecture.md` §12: the runner's decisions are lookups. This module
    is one of them — it reads text the CLI already printed and nothing else.

    Import statements, not substrings: the phrase list contains the words "too
    many requests", and a naive substring scan for "requests" fails on it. That
    is a test finding its own false positive, which is the same mistake the
    detector itself has to avoid."""
    source = Path(limits.__file__).read_text(encoding="utf-8")
    for forbidden in ("import subprocess", "import requests", "import urllib",
                      "import socket", "os.system", "os.popen"):
        assert forbidden not in source
