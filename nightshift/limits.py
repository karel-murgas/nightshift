"""Recognising the usage-limit wall, and working out when it lifts.

The runner's stopping condition used to be a dollar figure, and on a
subscription plan that is not a number anyone spends: `total_cost_usd` is the
API-equivalent price of the tokens, so `--budget 25` ended the night at a point
unrelated to the only limit that actually exists. What really ends a night is
the plan's rolling session window closing. This module is how the runner sees
that happen, so it can say "sleep until it reopens" or "stop, that was the last
one" instead of blaming the card that happened to be in flight.

**Why this matters more than a stopping condition normally would.** Before this
existed, a wall was indistinguishable from a card defect: the CLI exits
non-zero, `dispatch` turned any non-zero into `Dispatch("failed", …)`, and
`attempts` had already been committed. So one wall at 02:00 walked the rest of
the queue, spent an attempt on every card, wrote `## Error: worker exited 1` on
each, and over a few nights filed perfectly good cards into `failed/`. The
detection here is what makes an uncapped overnight run safe rather than
destructive.

**It is a text scan, and that is a real weakness worth stating plainly.** The
CLI reports exhaustion in prose, so recognition is a phrase list, and a wall
worded in a way this list does not carry is not recognised. Missing one is not
silent, it is a regression to the behaviour above — so two things are built
around it. Every dispatch's raw CLI output is archived under
`.ai/runs/<id>/attempt-N/`, which is exactly the evidence needed to add a
phrasing after the fact. And the runner carries a consecutive-failure circuit
breaker, so an unrecognised wall costs three cards instead of the whole queue.
Fixing a missed wall means adding a line to `_WALL` and a case to
`tests/test_board_limits.py`; nothing else has to change.

The scan only ever runs on a **non-zero exit**. That single precondition is what
makes a phrase list safe: `stdout` carries the worker's own final message, so a
card about rate limiting would otherwise trip the detector by talking about it.
A worker that succeeded did not hit a wall, whatever its prose says.

No LLM (`00_architecture.md` §12) — regexes over text the CLI already printed.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

# How long a rolling session window lasts, used only when the CLI does not say
# when the window reopens. Every path that can read a real reset time prefers it;
# this is the "we know we are blocked but not until when" fallback.
SESSION_HOURS = 5

# Slept a little past the reset rather than up to it. Waking on the exact second
# the window lifts is racing the service's clock, and losing that race costs a
# whole card's attempt to discover.
GRACE_MINUTES = 2

# Four kinds of wall, because the right response to each is different.
#
# SESSION   the rolling plan window closed. Reopens in hours; this is the one
#           `--sessions` counts, and the one a night may sleep through.
# WEEKLY    the weekly allowance is gone. Does not reopen inside a night, so
#           sleeping on it would idle until morning and produce nothing.
# MONTHLY   the account's monthly spend cap — the budget for tokens bought on
#           top of the subscription. It is *not* a capacity wall: running out of
#           extra tokens is not running out of tokens, because when the rolling
#           session window refreshes the plan's own allowance comes back and the
#           extras are not needed at all. So it sleeps and retries exactly like
#           SESSION and counts against `--sessions` the same way (Karel,
#           2026-08-07, after it ended a night at 00:56 with `--sessions 2` and
#           six cards still queued). What it must *not* do is wait for the
#           billing reset: the thing being waited for is the session refresh,
#           which is why `resume_at` caps this scope at one session window
#           however far out the CLI says the cap itself lifts. Kept a distinct
#           scope from SESSION only so the morning log names it accurately.
#           Delivered as a 429 whose prose is
#           "You've hit your monthly spend limit" — which is why it needs its
#           own phrase: none of the SESSION/WEEKLY wording appears in it, and a
#           bare-429 reading would treat a hard billing wall as a five-minute
#           hiccup and retry into it. Cost three cards their attempts on the
#           2026-07-24 night before it was recognised.
# TRANSIENT a service-side 429. Reopens in *seconds* — treating one as a session
#           wall would throw away five hours of a good night over a hiccup, so it
#           gets a short wait and does not count against `--sessions`.
SESSION = "session"
WEEKLY = "weekly"
MONTHLY = "monthly"
TRANSIENT = "transient"

# How long to wait out a TRANSIENT wall that named no retry time.
TRANSIENT_MINUTES = 5

# Matched case-insensitively against stdout+stderr, and only on a non-zero exit.
# Grow this list from `.ai/runs/` evidence when a wall gets through.
_WALL = re.compile(
    r"\busage limit\b"
    r"|\bplan limit\b"
    r"|\bspend limit\b"                 # the monthly billing cap, worded like none of the others
    r"|limit reached\|\d+"              # the machine-readable form, with an epoch
    r"|\brate[ _]limit(?:_error)?\b"
    r"|\btoo many requests\b"
    r"|\binsufficient quota\b",
    re.IGNORECASE,
)

# The monthly spend cap, checked before the generic plan wall because its
# response differs: it does not wait out. "monthly" is optional — the prose is
# "hit your monthly spend limit", but "spend limit" alone is unambiguous and no
# other wall uses the word.
_MONTHLY = re.compile(r"\bspend limit\b", re.IGNORECASE)

# What separates a plan wall from a 429. Checked first: plan exhaustion is
# sometimes delivered *as* a `rate_limit_error` with usage-limit prose attached,
# and reading that as a hiccup would have the runner retry into the same wall
# every five minutes until morning.
_PLAN = re.compile(r"\b(?:usage|plan) limit\b|limit reached\|\d+", re.IGNORECASE)
_TRANSIENT_ONLY = re.compile(r"\brate[ _]limit(?:_error)?\b|\btoo many requests\b",
                             re.IGNORECASE)

# `Claude AI usage limit reached|1750000000` — seconds, or milliseconds when the
# value is long enough that seconds would put it in the year 5138.
_EPOCH = re.compile(r"limit reached\|(\d{9,13})", re.IGNORECASE)
_ISO = re.compile(
    r"reset(?:s|ting)?(?:\s+at)?\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    r"reset(?:s|ting)?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
_WEEKLY_WORD = re.compile(r"\bweek(?:ly)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Wall:
    """One recognised usage limit. Frozen — it is evidence, not state."""

    scope: str                      # SESSION | WEEKLY | MONTHLY | TRANSIENT
    resets_at: dt.datetime | None   # None when the CLI did not say
    evidence: str                   # the matched line, for the run log and the card

    @property
    def waits_out(self) -> bool:
        """Whether sleeping on this wall could plausibly finish inside a night.

        MONTHLY waits out even though the spend cap itself does not lift tonight,
        because what the runner is waiting for is the session window reopening —
        past that the subscription's own allowance covers the work and the
        overage the cap governs is not needed. See the scope table above.
        """
        return self.scope in (SESSION, MONTHLY, TRANSIENT)

    @property
    def spends_a_session(self) -> bool:
        """Whether this counts against `--sessions`. A 429 does not — it is a
        hiccup in the window, not the window closing."""
        return self.scope != TRANSIENT


def _epoch(text: str) -> dt.datetime | None:
    found = _EPOCH.search(text)
    if not found:
        return None
    value = int(found.group(1))
    if value > 10_000_000_000:  # milliseconds
        value //= 1000
    try:
        return dt.datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(text: str) -> dt.datetime | None:
    found = _ISO.search(text)
    if not found:
        return None
    try:
        return dt.datetime.fromisoformat(found.group(1).replace(" ", "T"))
    except ValueError:
        return None


def _clock(text: str, now: dt.datetime) -> dt.datetime | None:
    """A bare wall-clock time — "resets at 4am" — resolved against `now`.

    Always the *next* occurrence: a window that reopens at 04:00, read at 02:00,
    means this morning, and read at 05:00 means tomorrow. Rolling forward is the
    only reading that cannot produce a reset time in the past, and a reset in the
    past would be taken as "the window is already open" and burn a card finding
    out otherwise.
    """
    found = _CLOCK.search(text)
    if not found:
        return None
    hour = int(found.group(1))
    minute = int(found.group(2) or 0)
    meridiem = (found.group(3) or "").lower()
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when > now else when + dt.timedelta(days=1)


def detect(returncode: int, stdout: str = "", stderr: str = "",
           now: dt.datetime | None = None) -> Wall | None:
    """The wall this CLI run hit, or `None` if it did not hit one.

    `None` is the answer for every successful run without reading a character of
    its output — see the module docstring on why that precondition is what makes
    a phrase list safe.
    """
    if returncode == 0:
        return None
    text = f"{stdout}\n{stderr}"
    found = _WALL.search(text)
    if not found:
        return None

    now = now or dt.datetime.now()
    resets = _epoch(text) or _iso(text) or _clock(text, now)
    # A reset already in the past tells us nothing useful — a stale timestamp
    # from an earlier attempt in the same log is likelier than a window that
    # reopened while we were reading about it. Treat it as unknown.
    if resets is not None and resets <= now:
        resets = None

    if _MONTHLY.search(text):
        scope = MONTHLY
    elif _PLAN.search(text):
        scope = WEEKLY if _WEEKLY_WORD.search(text) else SESSION
    elif _TRANSIENT_ONLY.search(text):
        scope = TRANSIENT
    else:
        scope = SESSION

    line = next((l.strip() for l in text.splitlines() if _WALL.search(l)), found.group(0))
    return Wall(scope=scope, resets_at=resets, evidence=line[:200])


def resume_at(wall: Wall, now: dt.datetime | None = None) -> dt.datetime:
    """When the runner should try again after `wall`.

    The CLI's own reset time when it gave one; otherwise a whole session window
    for a plan wall and a few minutes for a 429 — plus `GRACE_MINUTES` either
    way. Guessing five hours on a hiccup would cost a night; guessing five
    minutes on a real wall costs one wasted retry, so the asymmetry decides it.

    MONTHLY is the exception to preferring the CLI's own time. A spend cap's
    reset is the billing date, which can be weeks out, and that is not what the
    runner is waiting for — it is waiting for the session window that makes the
    overage unnecessary. So this scope is capped at one session window: without
    the cap, a `resets_at` parsed off the spend-limit message would put the night
    to sleep until next month.
    """
    now = now or dt.datetime.now()
    default = dt.timedelta(minutes=TRANSIENT_MINUTES) if wall.scope == TRANSIENT \
        else dt.timedelta(hours=SESSION_HOURS)
    when = wall.resets_at or (now + default)
    if wall.scope == MONTHLY:
        when = min(when, now + dt.timedelta(hours=SESSION_HOURS))
    return when + dt.timedelta(minutes=GRACE_MINUTES)
