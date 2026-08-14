"""Reading the plan's own usage meters, and refusing to spend money by default.

`limits.py` is the *reactive* half of this concern: it recognises a wall after the
CLI has already hit one, by scanning prose on a non-zero exit. Its own docstring is
candid that this is a phrase list and can miss a novel wording. What it cannot do —
by construction, because a wall is the only evidence it ever sees — is answer
"should I start this dispatch at all".

This module is the *predictive* half. It reads the same meters the editor's usage
indicator reads, so the dispatcher can decline to start work that would cross a
line, instead of discovering the line by walking into it.

**Why this is worth a module rather than a field.** Before it existed, the only
visible signal was the wall, so a session window could be consumed by one expensive
step and the cost was invisible until the next step failed. Measured on 2026-08-14:
one triage run took roughly an hour and most of a window, and the only reason anyone
noticed was that the *following* command had nothing left to run with.

**The money rule, and it is the default.** Karel, 2026-08-14: *"I would love the
default option to be 'stop, when free usage runs out'. With optional 'continue
nevertheless' decision."* So when the plan's own allowance is spent and continuing
would draw on paid credits, the answer is **refuse**, and the caller must pass an
explicit per-invocation opt-in to proceed. There is deliberately **no persisted
setting** for that opt-in: a config field saying "always spend money" is a foot-gun
that outlives the intent behind it, and the point of the rule is that each such
decision is made by a human at the moment it applies.

**This module never chooses an account and never falls back to another one.** It
reads exactly one credential — the one in this config directory — and reports on it.
Selecting between accounts on measured headroom is prohibited (`feedback_account_dispatch`,
audit matrix row 72): a meter may *veto* a dispatch, never *pick* one.

**It fails open, and that is a deliberate trade.** The endpoint is undocumented, so
it will change shape or move without notice. A fetch failure therefore yields
`fetched=False` and an *allow* verdict, not a refusal — because failing closed on an
undocumented dependency means one upstream rename silently stops every overnight run,
which is a worse outcome than the one this module exists to prevent. `limits.py`
remains the backstop for exactly that case. The reason is always carried on the
verdict so a caller can log "ran without a meter" rather than implying it checked.

No LLM: an HTTP GET and arithmetic over the JSON it returns.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# The credential the interactive clients write. Read-only here; refreshing it is
# the CLI's job, and a token this module finds expired is reported as such rather
# than renewed behind the user's back.
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

#: Seconds to wait on the endpoint. The editor extension uses 5s; matching it keeps
#: a slow network from turning a pre-dispatch check into a hang.
TIMEOUT_S = 5

#: How much plan allowance must remain before a *new* dispatch may start when paid
#: overage is enabled. A dispatch cannot be un-started: one begun at 98% will cross
#: into paid credits mid-run and there is no way to stop it politely. This margin is
#: the width of that hazard, not a general safety buffer — see `check()` on why it is
#: not applied when paid overage is off.
START_MARGIN_PCT = 5.0

#: Utilization at which the plan's own allowance is considered spent.
EXHAUSTED_PCT = 100.0


@dataclass(frozen=True)
class Bucket:
    """One usage window the plan meters. Frozen — a reading, not state."""

    name: str
    utilization: float              # percent of the plan allowance used, 0-100+
    resets_at: dt.datetime | None    # None when the endpoint did not say

    @property
    def exhausted(self) -> bool:
        return self.utilization >= EXHAUSTED_PCT

    @property
    def headroom_pct(self) -> float:
        return max(0.0, EXHAUSTED_PCT - self.utilization)


@dataclass(frozen=True)
class Snapshot:
    """What the meters said, or why they could not be read."""

    buckets: tuple[Bucket, ...] = ()
    paid_enabled: bool = False       # overage credits available past the plan limit
    paid_used_minor: int | None = None
    paid_currency: str = ""
    paid_exponent: int = 2
    subscription: str = ""
    fetched: bool = False
    reason: str = ""                 # why not fetched; "" when it was

    @property
    def worst(self) -> Bucket | None:
        """The bucket closest to its limit — the one that decides a verdict."""
        return max(self.buckets, key=lambda b: b.utilization, default=None)

    @property
    def free_exhausted(self) -> bool:
        return any(b.exhausted for b in self.buckets)

    @property
    def earliest_reset(self) -> dt.datetime | None:
        """When the soonest exhausted bucket lifts.

        Only exhausted buckets are considered: a healthy bucket's reset time says
        nothing about when blocked work could resume.
        """
        times = [b.resets_at for b in self.buckets if b.exhausted and b.resets_at]
        return min(times) if times else None

    @property
    def paid_used_display(self) -> str:
        if self.paid_used_minor is None:
            return ""
        amount = self.paid_used_minor / (10 ** self.paid_exponent)
        return f"{amount:.2f} {self.paid_currency}".strip()


@dataclass(frozen=True)
class Verdict:
    """Whether a dispatch may start, and what to tell the log if not."""

    allow: bool
    reason: str
    resume_at: dt.datetime | None = None
    metered: bool = True             # False when it allowed without a reading
    #: Whether `allow_paid=True` would flip this refusal. An explicit field, not a
    #: phrase test on `reason`: the first version sniffed for "paid" and matched the
    #: *no*-overage refusal ("...and no paid overage"), which would have offered an
    #: override that cannot help — there is nothing to spend. Caught by
    #: `test_exhausted_plan_without_paid_overage_refuses_and_is_not_a_money_refusal`.
    overridable: bool = False

    @property
    def refused_for_money(self) -> bool:
        """Whether the only thing in the way is the paid-credit rule — i.e. whether
        offering the explicit opt-in to the user would be honest."""
        return not self.allow and self.overridable


def _parse_iso(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    # Compare in local naive time, as `limits.py` does, so the two modules'
    # timestamps can be reasoned about together without a tz dance at every site.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _token(creds: dict) -> tuple[str, str]:
    """The access token and the subscription label, by key name.

    Searched rather than indexed: the credential file's nesting has changed shape
    before, and a depth-first hunt for the token key survives that where a literal
    path does not.
    """
    subscription = ""
    found = ""

    def walk(node: object) -> None:
        nonlocal found, subscription
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            low = key.lower()
            if isinstance(value, str):
                if not found and low in {"accesstoken", "access_token"}:
                    found = value
                elif not subscription and low in {"subscriptiontype", "subscription_type"}:
                    subscription = value
            walk(value)

    walk(creds)
    return found, subscription


def _expired(creds: dict, now: dt.datetime) -> bool:
    """Whether the access token's own stamp says it has lapsed.

    Checked before the call so an expired token reports "re-authenticate" instead of
    a bare 401, which reads like the endpoint moved.
    """
    stamp: object = None

    def walk(node: object) -> None:
        nonlocal stamp
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if stamp is None and key.lower() in {"expiresat", "expires_at"}:
                stamp = value
            walk(value)

    walk(creds)
    if isinstance(stamp, (int, float)):
        seconds = stamp / 1000 if stamp > 10_000_000_000 else stamp
        try:
            return dt.datetime.fromtimestamp(seconds) <= now
        except (OverflowError, OSError, ValueError):
            return False
    parsed = _parse_iso(stamp)
    return bool(parsed and parsed <= now)


def _buckets(payload: dict) -> tuple[Bucket, ...]:
    """Every metered window in the payload, whatever they are called.

    **The key list is never hardcoded.** The live response carries a dozen-odd
    windows beyond the familiar ones, most of them null and several with internal
    codenames that mean nothing outside the vendor. A parser expecting three fixed
    names renders a blank panel the day those names shift, so this walks whatever
    came back and keeps the entries that look like a meter.
    """
    found: list[Bucket] = []
    for name, value in payload.items():
        if not isinstance(value, dict):
            continue
        util = value.get("utilization")
        if not isinstance(util, (int, float)):
            continue
        found.append(Bucket(name=name,
                            utilization=float(util),
                            resets_at=_parse_iso(value.get("resets_at"))))
    return tuple(sorted(found, key=lambda b: b.name))


def _paid(payload: dict) -> tuple[bool, int | None, str, int]:
    """Whether spending past the plan limit is possible, and what it has cost.

    Two independent places say so — an `extra_usage` block and a `spend` block —
    and either being enabled is enough to make the money rule bite.
    """
    enabled = False
    used: int | None = None
    currency = ""
    exponent = 2

    extra = payload.get("extra_usage")
    if isinstance(extra, dict):
        enabled = enabled or bool(extra.get("is_enabled"))

    spend = payload.get("spend")
    if isinstance(spend, dict):
        enabled = enabled or bool(spend.get("enabled"))
        block = spend.get("used")
        if isinstance(block, dict):
            amount = block.get("amount_minor")
            if isinstance(amount, int):
                used = amount
            currency = str(block.get("currency") or "")
            if isinstance(block.get("exponent"), int):
                exponent = block["exponent"]
    return enabled, used, currency, exponent


def read(credentials: Path | None = None, *,
         now: dt.datetime | None = None,
         url: str = USAGE_URL) -> Snapshot:
    """Fetch the meters for *this* config directory's account.

    Never raises for an unreachable or changed endpoint: every failure comes back as
    `fetched=False` with a reason, because the caller's job is to dispatch work and a
    missing meter must not be able to stop it. See the module docstring.
    """
    now = now or dt.datetime.now()
    path = credentials or CREDENTIALS

    if not path.is_file():
        return Snapshot(reason=f"no credential file at {path}")
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Snapshot(reason=f"credential file unreadable: {exc}")

    token, subscription = _token(creds)
    if not token:
        return Snapshot(subscription=subscription,
                        reason="no access token in the credential file")
    if _expired(creds, now):
        return Snapshot(subscription=subscription,
                        reason="access token expired - re-authenticate in a normal session")

    request = urllib.request.Request(url, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        # Sent because the editor client sends one. Whether the endpoint requires it
        # is unverified; it answers with the header present, which is what matters.
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        hint = {401: " - re-authenticate", 403: " - not a subscription account",
                404: " - endpoint moved; re-grep the editor bundle"}.get(exc.code, "")
        return Snapshot(subscription=subscription,
                        reason=f"usage endpoint returned HTTP {exc.code}{hint}")
    except Exception as exc:                      # network, DNS, TLS, malformed JSON
        return Snapshot(subscription=subscription,
                        reason=f"usage endpoint unreachable: {exc!r}")

    if not isinstance(payload, dict):
        return Snapshot(subscription=subscription,
                        reason="usage endpoint returned a non-object")

    enabled, used, currency, exponent = _paid(payload)
    return Snapshot(buckets=_buckets(payload), paid_enabled=enabled,
                    paid_used_minor=used, paid_currency=currency,
                    paid_exponent=exponent, subscription=subscription, fetched=True)


def check(snapshot: Snapshot, *, allow_paid: bool = False,
          margin_pct: float = START_MARGIN_PCT) -> Verdict:
    """Whether a new dispatch may start, given what the meters said.

    The default is the money rule: **stop when the plan's own allowance runs out.**
    `allow_paid=True` is the caller's explicit "continue nevertheless", and it is
    the only thing that turns a paid refusal into an allow.
    """
    if not snapshot.fetched:
        return Verdict(True, f"no usage reading ({snapshot.reason})", metered=False)

    worst = snapshot.worst
    if worst is None:
        return Verdict(True, "no metered windows reported", metered=False)

    if snapshot.free_exhausted:
        spent = [b.name for b in snapshot.buckets if b.exhausted]
        where = ", ".join(spent)
        if not snapshot.paid_enabled:
            return Verdict(False, f"plan allowance spent ({where}) and no paid overage",
                           snapshot.earliest_reset)
        if not allow_paid:
            return Verdict(False,
                           f"plan allowance spent ({where}) - continuing would draw "
                           f"paid credits; pass the explicit opt-in to proceed",
                           snapshot.earliest_reset, overridable=True)
        return Verdict(True, f"plan allowance spent ({where}) - proceeding on paid "
                             f"credits by explicit opt-in")

    # Below the line, the only hazard worth refusing for is money. Crossing into a
    # wall when there is nothing to spend is what `limits.py` already handles, and
    # duplicating that here would add a refusal path with no new protection.
    if snapshot.paid_enabled and not allow_paid and worst.headroom_pct <= margin_pct:
        return Verdict(False,
                       f"only {worst.headroom_pct:.1f}% of {worst.name} left - a dispatch "
                       f"started now would cross into paid credits mid-run; pass the "
                       f"explicit opt-in to proceed",
                       snapshot.earliest_reset or worst.resets_at, overridable=True)

    return Verdict(True, f"{worst.name} at {worst.utilization:.1f}%")


def describe(snapshot: Snapshot) -> list[str]:
    """Human-readable meter lines, for a log, a digest or a panel."""
    if not snapshot.fetched:
        return [f"usage unavailable - {snapshot.reason}"]
    lines = [f"{b.name:22} {b.utilization:5.1f}%"
             + (f"  resets {b.resets_at:%Y-%m-%d %H:%M}" if b.resets_at else "")
             for b in snapshot.buckets]
    if snapshot.paid_enabled:
        spent = snapshot.paid_used_display
        lines.append(f"paid overage ENABLED" + (f" - {spent} used" if spent else ""))
    else:
        lines.append("paid overage disabled")
    return lines


def main(argv: list[str] | None = None) -> int:
    """`python -m nightshift.usage` — the meters and the verdict.

    Exit 0 allow, 3 refuse, 1 usage error. A distinct refusal code is what lets a
    shell script or the runner branch on the verdict without parsing prose.
    """
    parser = argparse.ArgumentParser(
        description="Show this account's plan usage and whether a dispatch may start.")
    parser.add_argument("--allow-paid", action="store_true",
                        help="proceed even if continuing draws on paid credits "
                             "(the explicit 'continue nevertheless' decision)")
    parser.add_argument("--margin", type=float, default=START_MARGIN_PCT,
                        help=f"headroom%% required to start when paid overage is on "
                             f"(default {START_MARGIN_PCT})")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    snapshot = read()
    verdict = check(snapshot, allow_paid=args.allow_paid, margin_pct=args.margin)

    if args.json:
        print(json.dumps({
            "fetched": snapshot.fetched,
            "reason": snapshot.reason,
            "subscription": snapshot.subscription,
            "paid_enabled": snapshot.paid_enabled,
            "paid_used_minor": snapshot.paid_used_minor,
            "paid_currency": snapshot.paid_currency,
            "buckets": [{"name": b.name, "utilization": b.utilization,
                         "resets_at": b.resets_at.isoformat() if b.resets_at else None}
                        for b in snapshot.buckets],
            "allow": verdict.allow,
            "verdict": verdict.reason,
            "metered": verdict.metered,
            "resume_at": verdict.resume_at.isoformat() if verdict.resume_at else None,
        }, indent=2))
    else:
        for line in describe(snapshot):
            print(f"  {line}")
        print()
        print(f"{'ALLOW' if verdict.allow else 'REFUSE'} - {verdict.reason}")
        if verdict.resume_at:
            print(f"  resume at {verdict.resume_at:%Y-%m-%d %H:%M}")
        if verdict.refused_for_money:
            print("  override: re-run with --allow-paid")

    if not snapshot.fetched and not verdict.allow:
        return 1
    return 0 if verdict.allow else 3


if __name__ == "__main__":
    raise SystemExit(main())
