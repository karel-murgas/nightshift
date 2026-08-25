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

The scan only ever runs on a **non-zero exit**, or on a CLI *error terminal* —
a line that parses as the CLI's own `{"type": "result", "is_error": true,
"api_error_status": <4xx/5xx>}` object. That precondition is what makes a phrase
list safe: `stdout` carries the worker's own final message, so a card about rate
limiting would otherwise trip the detector by talking about it. Prose alone
never qualifies; the escape hatch is structural, and it exists because the CLI
has been observed reporting `"is_error": true` and `"subtype": "success"` in the
same object (2026-08-25) — its own fields disagree, so the exit code is not the
only thing worth reading.

**Prefer the CLI's structured fields over its prose wherever it emits them.**
A `--output-format stream-json` run carries, on the request that was refused:

    "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                        "resetsAt": <epoch>,
                        "unifiedWindows": {"five_hour": {"utilization": 1, ...},
                                           "seven_day": {"utilization": 0.23, ...}}}

which answers *which* wall, *until when*, and — the one no sentence carries —
**how spent that window actually is**. `--output-format json` carries none of
it, so the phrase list is still the only reading available there, which is
precisely how the 2026-08-25 review walls got through (see `_WALL`'s
`session limit` entry).

**`utilization` is the check that keeps a hiccup from ending a night.** A
refusal naming a window that is 31% used is not plan exhaustion however its
prose reads, and treating it as one would sleep until a window resets that the
CLI itself says is fine. So a low utilization downgrades the wall to TRANSIENT:
a short wait, no session spent (Karel, 2026-08-25: *"one bad 429 would break the
night? This should not happen"*). The threshold and the asymmetry behind it are
on `PLAN_WALL_UTILIZATION`.

The scope this module returns therefore comes from, in order: the CLI's own
window name and meter; then its prose; then a bare 429, which is TRANSIENT. An
unrecognised window name yields *no* structured answer rather than a guessed
one — see `_window_scope` on why defaulting there would be the expensive
mistake.

No LLM (`00_architecture.md` §12) — lookups over text the CLI already printed.
"""
from __future__ import annotations

import datetime as dt
import json
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

# Matched case-insensitively against stdout+stderr, and only past the
# precondition in `detect`. Grow this list from `.ai/runs/` evidence when a wall
# gets through.
#
# `session limit` is the 2026-08-25 evidence, and it cost a whole run. The CLI's
# wording for the rolling window is "You've hit your session limit · resets
# 5:50pm (Europe/Prague)" — which matches none of the entries above it. Under
# `--output-format stream-json` that went unnoticed, because the worker's stream
# also carries a `rate_limit_info` block and `rate[ _]limit` caught *that*
# (as TRANSIENT, wrongly — see `_PLAN`). Under `--output-format json` there is no
# such block, so `review_branch`, `run_checker` and `run_stale_check` saw no wall
# at all: two finished cards had their reviewer walled mid-verdict, were filed as
# "no usable review verdict", and were parked in `review/` as though a human had
# to look at them. Neither the sleep nor the give-back ran.
_WALL = re.compile(
    r"\busage limit\b"
    r"|\bplan limit\b"
    r"|\bsession limit\b"               # the rolling window, as the CLI words it
    r"|\bspend limit\b"                 # the monthly billing cap, worded like none of the others
    r"|limit reached\|\d+"              # the machine-readable form, with an epoch
    r"|\brate[ _]limit(?:_error)?\b"
    r"|\btoo many requests\b"
    r"|\binsufficient quota\b",
    re.IGNORECASE,
)

# The monthly spend cap, checked before the generic plan wall so it is named as
# itself in the log rather than folded into SESSION — the *behaviour* is now
# SESSION's (see the scope table above), but which wall was hit is still worth
# reporting accurately. "monthly" is optional — the prose is "hit your monthly
# spend limit", but "spend limit" alone is unambiguous and no other wall uses
# the word.
_MONTHLY = re.compile(r"\bspend limit\b", re.IGNORECASE)

# What separates a plan wall from a 429. Checked first: plan exhaustion is
# sometimes delivered *as* a `rate_limit_error` with usage-limit prose attached,
# and reading that as a hiccup would have the runner retry into the same wall
# every five minutes until morning.
#
# `session limit` belongs here for a sharper version of that reason. On
# 2026-08-25 a genuine five-hour wall reached this function inside a stream that
# also held `"rate_limit_info"`, so `_TRANSIENT_ONLY` claimed it and the night
# logged "transient, 2 of 3": it never counted against `--sessions`, and it was
# capped by `TRANSIENT_RETRIES` instead — three waits and the run stops, whatever
# `--sessions` said. The sleep time happened to be right (the prose carried a
# reset), so the misfiling was invisible in the log except as one wrong word.
_PLAN = re.compile(r"\b(?:usage|plan|session) limit\b|limit reached\|\d+", re.IGNORECASE)
_TRANSIENT_ONLY = re.compile(r"\brate[ _]limit(?:_error)?\b|\btoo many requests\b",
                             re.IGNORECASE)

# The CLI's own machine-readable verdict on which window closed, emitted in the
# `rate_limit_info` block of a `--output-format stream-json` run. Preferred over
# every prose reading below: it is the same fact without the wording risk, and it
# is what would have classified the 2026-08-25 wall correctly with no phrase list
# involved at all. Observed values: `five_hour` (the rolling session window) and,
# by the same naming scheme, a longer one for the weekly allowance — so the match
# is on the *unit*, not on an enumeration this module would have to chase.
#
# **Parsed, not matched.** The first version of this was a regex over the block,
# and it never fired once: `rate_limit_info` contains a nested `unifiedWindows`
# object, and the `[^{}]*?` that kept the match from wandering across a stream
# could not reach the closing brace past it. It was dead code that looked like a
# working structured read, and the `session limit` phrase below was quietly doing
# all the work — which is the same "wrote it down confidently, never ran it
# against the corpus" shape this module already carries a correction for.
#
# A stream holds one of these per message, almost all `"status": "allowed"`, so
# the one that matters is the *rejected* one — and reading it as JSON is what
# makes "the block that says rejected" and "the window it names" the same object
# rather than two independent searches that could pair up across messages.
_HAS_LIMIT_INFO = '"rate_limit_info"'

#: How spent a window must be before a refusal naming it counts as plan
#: exhaustion. Karel, 2026-08-25: *"If error occurs and last known value was low
#: enough (90%?) it should continue."*
#:
#: The CLI attaches `unifiedWindows` to its refusal, with a `utilization` per
#: window — so the meter reading arrives *inside the evidence*, and there is no
#: need to go and ask for it. Below this, a refusal is a hiccup whatever its
#: prose says: the service declined this request, but the window it named is
#: nowhere near spent, so sleeping until that window resets would idle the night
#: on a number the CLI itself says is fine.
#:
#: The asymmetry sets the threshold. Downgrading a real wall costs three short
#: waits before the transient cap stops the run; upgrading a hiccup costs the
#: rest of the night. So the bar for "this really is exhaustion" is high, and
#: 0.9 leaves room for a rejection that lands slightly under a full window
#: (concurrent requests can be refused at 0.97 without the window being closed).
PLAN_WALL_UTILIZATION = 0.9

# `Claude AI usage limit reached|1750000000` — seconds, or milliseconds when the
# value is long enough that seconds would put it in the year 5138. `resetsAt` is
# the same number under the CLI's own key, from the `rate_limit_info` block.
_EPOCH = re.compile(r"limit reached\|(\d{9,13})|\"resetsAt\"\s*:\s*(\d{9,13})",
                    re.IGNORECASE)
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
    # Two alternatives, one number: whichever group matched is the epoch.
    value = int(found.group(1) or found.group(2))
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


def _find_key(value: object, key: str, depth: int = 6) -> object:
    """The first `key` anywhere in a decoded JSON value, or `None`.

    Depth-bounded rather than trusting the shape: `rate_limit_info` has been seen
    at the top level of an event and nested under `message`, and pinning either
    one would make this a second thing to keep in step with the CLI.
    """
    if depth < 0:
        return None
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for nested in value.values():
            found = _find_key(nested, key, depth - 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, key, depth - 1)
            if found is not None:
                return found
    return None


def _rejected_window(text: str) -> dict | None:
    """The `rate_limit_info` block attached to the request the service refused.

    The rejected one specifically — a stream carries an `"allowed"` block per
    message, and those describe a window that is doing fine.
    """
    for line in reversed(text.splitlines()):
        line = line.strip()
        if _HAS_LIMIT_INFO not in line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        info = _find_key(event, "rate_limit_info")
        if isinstance(info, dict) and str(info.get("status", "")).lower() == "rejected":
            return info
    return None


def _window_is_spent(info: dict) -> bool | None:
    """Is the window this refusal names actually used up? `None` if unstated.

    `None` and `False` are different answers and the caller must not conflate
    them: `None` means the CLI attached no `utilization` for that window, so
    there is nothing to argue with and the refusal stands as read. `False` means
    it attached one and it is low — the positive evidence that this is a hiccup.
    """
    windows = info.get("unifiedWindows")
    named = str(info.get("rateLimitType", ""))
    window = windows.get(named) if isinstance(windows, dict) else None
    used = window.get("utilization") if isinstance(window, dict) else None
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return used >= PLAN_WALL_UTILIZATION


def _window_scope(name: str) -> str | None:
    """Which wall a `rateLimitType` names, or `None` if this does not know.

    Read off the *unit* rather than an enumeration of values, so `five_hour` and
    a hypothetical `one_hour` both land on SESSION without a table edit. `None`
    is the important return: an unrecognised name is not evidence, and must fall
    through to the prose reading rather than defaulting to a scope. Defaulting
    to SESSION here would mean a short-window throttle the CLI names in a word
    this function has not seen puts the night to sleep for five hours.
    """
    name = name.lower()
    if "hour" in name:
        return SESSION
    if "day" in name or "week" in name or "month" in name:
        return WEEKLY
    return None


def _error_terminal(text: str) -> dict | None:
    """The CLI's own terminal result object, if it reported an API error.

    A *structural* read, deliberately: it must parse as JSON, be the terminal
    `result` event, say `is_error`, and carry an HTTP status of 400 or above.
    Prose cannot satisfy all four, which is what makes this safe to trust past a
    zero exit code — unlike a phrase, which the worker's own final message could
    contain innocently (that is the whole reason `detect` has a precondition).

    Exists because the CLI's fields have been seen to disagree: the 2026-08-25
    walls arrived as `{"is_error": true, "subtype": "success", "terminal_reason":
    "api_error", "api_error_status": 429}`. Reading the exit code alone means
    trusting whichever of those two the launcher happened to map it from.

    Line-oriented, so it works on both output formats with one pass: `json`
    prints a single object, `stream-json` prints one per line and the terminal
    result is the last of them.
    """
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{") or '"is_error"' not in line:
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        if event.get("is_error") is not True:
            continue
        status = event.get("api_error_status")
        if isinstance(status, int) and status >= 400:
            return event
    return None


def detect(returncode: int, stdout: str = "", stderr: str = "",
           now: dt.datetime | None = None) -> Wall | None:
    """The wall this CLI run hit, or `None` if it did not hit one.

    `None` is the answer for every clean run — a zero exit with no error
    terminal — without reading a character of its prose. See the module
    docstring on why that precondition is what makes a phrase list safe.
    """
    text = f"{stdout}\n{stderr}"
    terminal = _error_terminal(text)
    if returncode == 0 and terminal is None:
        return None

    # A 429 the CLI reported in its own status field *is* "too many requests",
    # whatever prose came with it. Reading it here rather than adding another
    # phrase is what keeps this working through the next rewording: the number is
    # the protocol, the sentence around it is not.
    throttled = bool(terminal) and terminal.get("api_error_status") == 429
    found = _WALL.search(text)
    if not found and not throttled:
        return None

    now = now or dt.datetime.now()
    # A reset already in the past tells us nothing useful — a stale timestamp
    # from an earlier attempt in the same log is likelier than a window that
    # reopened while we were reading about it. Treat it as unknown.
    #
    # Filtered per candidate, not once over the winner: the readings are ranked
    # by precision, and a stale `resetsAt` earlier in a stream must not shadow a
    # perfectly good reset time in the prose below it. Ranking first and
    # discarding after would do exactly that — `or` short-circuits on the stale
    # one and the later candidates are never consulted.
    resets = next((when for when in (_epoch(text), _iso(text), _clock(text, now))
                   if when is not None and when > now), None)

    # The CLI's own account of which window closed, and how spent it is. Both
    # outrank every prose reading below: they are the same facts without the
    # wording risk, and the utilization is the one signal that can say "the
    # service refused this, and it was still not exhaustion".
    named = _window_scope(str((rejected or {}).get("rateLimitType", ""))) \
        if (rejected := _rejected_window(text)) else None
    if named and _window_is_spent(rejected) is False:
        # Refused, but the window it named is under `PLAN_WALL_UTILIZATION`. Not
        # plan exhaustion, whatever the prose says — a short wait that spends no
        # session, rather than sleeping until a window that is 31% used resets.
        named = TRANSIENT

    if named:
        scope = named
    elif _MONTHLY.search(text):
        scope = MONTHLY
    elif _PLAN.search(text):
        scope = WEEKLY if _WEEKLY_WORD.search(text) else SESSION
    elif _TRANSIENT_ONLY.search(text):
        scope = TRANSIENT
    elif throttled:
        # A bare 429 with nothing else to go on: the pre-existing reading, and
        # the safe one — a short wait that does not spend one of the night's
        # sessions. If it was really the window closing, the next attempt says so.
        scope = TRANSIENT
    else:
        scope = SESSION

    line = next((l.strip() for l in text.splitlines() if _WALL.search(l)),
                found.group(0) if found else "")
    # A stream's terminal result is one enormous line, and its first 200
    # characters are boilerplate — the CLI's own message says more in less, so
    # prefer it when there is one. This is what the run log prints and what
    # lands on the card, so it is the only account a 6 AM reader gets.
    if terminal and isinstance(terminal.get("result"), str) and terminal["result"].strip():
        line = terminal["result"].strip()
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

    TRANSIENT is capped for the same reason, one scale down. A hiccup reopens in
    seconds *by definition* — that is what the scope means — so any reset time
    read out of the same text belongs to a plan window that happened to be
    described alongside it, not to the hiccup. Measured while adding
    `rate_limit_info`'s `resetsAt` as an epoch source: a 429 at 02:00 whose
    stream mentioned the five-hour window's 17:50 reset produced a **15h52m**
    sleep for a wall that spends no session and is meant to cost seven minutes.
    """
    now = now or dt.datetime.now()
    default = dt.timedelta(minutes=TRANSIENT_MINUTES) if wall.scope == TRANSIENT \
        else dt.timedelta(hours=SESSION_HOURS)
    when = wall.resets_at or (now + default)
    if wall.scope == MONTHLY:
        when = min(when, now + dt.timedelta(hours=SESSION_HOURS))
    if wall.scope == TRANSIENT:
        when = min(when, now + dt.timedelta(minutes=TRANSIENT_MINUTES))
    return when + dt.timedelta(minutes=GRACE_MINUTES)
