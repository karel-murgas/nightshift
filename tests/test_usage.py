"""`nightshift.usage` — the predictive half of the usage-limit concern.

Two properties carry most of the value here and neither is obvious from the code,
so they are asserted directly rather than left to inspection:

**The money rule defaults closed.** An exhausted plan with paid overage available
must refuse, and only an explicit opt-in may turn that into an allow. A regression
that flipped the default would be invisible in normal operation — it only shows up
as a bill.

**Everything else fails open.** A missing credential, a moved endpoint, a dead
network and a changed payload shape all have to come back as *allow, unmetered*.
Failing closed on an undocumented dependency would let one upstream rename stop
every overnight run, so the tests pin the direction of that trade.

The bucket parser is tested against a payload shaped like the real one — a dozen
null windows and unfamiliar codenames alongside the familiar names — because the
tempting implementation (index three known keys) passes any test written from the
three known keys alone.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from nightshift import usage


# --------------------------------------------------------------------------- helpers

def _creds(tmp_path: Path, *, token: str = "tok-" + "x" * 40,
           expires: object = None, nest: bool = True) -> Path:
    inner: dict = {"accessToken": token, "subscriptionType": "max"}
    if expires is not None:
        inner["expiresAt"] = expires
    body = {"claudeAiOauth": inner} if nest else inner
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


class _Response:
    def __init__(self, payload: object) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    monkeypatch.setattr(usage.urllib.request, "urlopen",
                        lambda *a, **k: _Response(payload))


def _raise(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise exc
    monkeypatch.setattr(usage.urllib.request, "urlopen", boom)


#: Shaped like the live 2026-08-14 response: two live windows, a pile of nulls, and
#: internal codenames that must neither crash the parser nor be depended upon.
_LIVE = {
    "five_hour": {"utilization": 20.0, "resets_at": "2026-08-14T11:39:59+00:00",
                  "limit_dollars": None, "used_dollars": None},
    "seven_day": {"utilization": 15.0, "resets_at": "2026-08-20T23:59:59+00:00"},
    "seven_day_sonnet": None,
    "seven_day_opus": None,
    "tangelo": None,
    "iguana_necktie": None,
    "nimbus_quill": {"utilization": 0.0, "resets_at": None},
    "amber_ladder": None,
    "extra_usage": {"is_enabled": True, "monthly_limit": None},
    "spend": {"used": {"amount_minor": 7212, "currency": "EUR", "exponent": 2},
              "limit": None, "enabled": True, "can_purchase_credits": False},
    "member_dashboard_available": False,
}


def _bucket(name: str, util: float, resets: dt.datetime | None = None) -> usage.Bucket:
    return usage.Bucket(name=name, utilization=util, resets_at=resets)


def _snap(*buckets: usage.Bucket, paid: bool = False) -> usage.Snapshot:
    return usage.Snapshot(buckets=buckets, paid_enabled=paid, fetched=True)


# ------------------------------------------------------------------- the money rule

def test_exhausted_plan_with_paid_overage_refuses_by_default():
    """The rule Karel asked for: stop when the free allowance runs out."""
    verdict = usage.check(_snap(_bucket("five_hour", 100.0), paid=True))
    assert verdict.allow is False
    assert "paid credits" in verdict.reason
    assert verdict.refused_for_money is True


def test_exhausted_plan_allows_only_with_the_explicit_opt_in():
    snapshot = _snap(_bucket("five_hour", 100.0), paid=True)
    assert usage.check(snapshot, allow_paid=True).allow is True
    assert usage.check(snapshot, allow_paid=False).allow is False


def test_exhausted_plan_without_paid_overage_refuses_and_is_not_a_money_refusal():
    """Nothing to spend, so the opt-in must not present itself as a way through."""
    verdict = usage.check(_snap(_bucket("five_hour", 101.0), paid=False))
    assert verdict.allow is False
    assert verdict.refused_for_money is False
    assert usage.check(_snap(_bucket("five_hour", 101.0)), allow_paid=True).allow is False


def test_thin_headroom_refuses_to_start_when_paid_overage_is_on():
    """A dispatch cannot be un-started, so the hazard is crossing 100 mid-run."""
    verdict = usage.check(_snap(_bucket("five_hour", 97.0), paid=True))
    assert verdict.allow is False
    assert verdict.refused_for_money is True


def test_thin_headroom_allows_when_there_is_nothing_to_spend():
    """The margin guards money, not attempts — `limits.py` owns the wall itself."""
    assert usage.check(_snap(_bucket("five_hour", 97.0), paid=False)).allow is True


def test_margin_is_tunable_and_actually_read():
    snapshot = _snap(_bucket("five_hour", 90.0), paid=True)
    assert usage.check(snapshot, margin_pct=5.0).allow is True
    assert usage.check(snapshot, margin_pct=15.0).allow is False


def test_the_worst_bucket_decides_not_the_first():
    verdict = usage.check(_snap(_bucket("aaa_healthy", 3.0),
                                _bucket("zzz_spent", 100.0), paid=True))
    assert verdict.allow is False
    assert "zzz_spent" in verdict.reason


# ------------------------------------------------------------------ failing open

@pytest.mark.parametrize("reason_fragment, build", [
    ("no credential file", lambda tmp: tmp / "absent.json"),
])
def test_missing_credential_allows_unmetered(tmp_path: Path, reason_fragment: str, build):
    snapshot = usage.read(build(tmp_path))
    assert snapshot.fetched is False
    assert reason_fragment in snapshot.reason
    verdict = usage.check(snapshot)
    assert verdict.allow is True and verdict.metered is False


def test_unreadable_credential_allows_unmetered(tmp_path: Path):
    path = tmp_path / ".credentials.json"
    path.write_text("{not json", encoding="utf-8")
    snapshot = usage.read(path)
    assert snapshot.fetched is False
    assert usage.check(snapshot).allow is True


def test_credential_without_a_token_allows_unmetered(tmp_path: Path):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "r"}}), encoding="utf-8")
    snapshot = usage.read(path)
    assert snapshot.fetched is False
    assert "no access token" in snapshot.reason


def test_expired_token_says_reauthenticate_rather_than_calling(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch):
    _raise(monkeypatch, AssertionError("must not call the endpoint with a dead token"))
    stale = (dt.datetime.now() - dt.timedelta(hours=1)).isoformat()
    snapshot = usage.read(_creds(tmp_path, expires=stale))
    assert snapshot.fetched is False
    assert "re-authenticate" in snapshot.reason


def test_expired_token_accepts_epoch_milliseconds(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    _raise(monkeypatch, AssertionError("must not call the endpoint with a dead token"))
    stale_ms = int((dt.datetime.now() - dt.timedelta(hours=1)).timestamp() * 1000)
    assert usage.read(_creds(tmp_path, expires=stale_ms)).fetched is False


def test_a_live_token_is_not_mistaken_for_expired(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    _serve(monkeypatch, _LIVE)
    ahead = (dt.datetime.now() + dt.timedelta(hours=8)).isoformat()
    assert usage.read(_creds(tmp_path, expires=ahead)).fetched is True


def test_http_error_allows_unmetered_and_names_the_code(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch):
    _raise(monkeypatch, usage.urllib.error.HTTPError(
        usage.USAGE_URL, 404, "Not Found", {}, None))  # type: ignore[arg-type]
    snapshot = usage.read(_creds(tmp_path))
    assert snapshot.fetched is False
    assert "404" in snapshot.reason and "endpoint moved" in snapshot.reason
    assert usage.check(snapshot).allow is True


def test_dead_network_allows_unmetered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _raise(monkeypatch, OSError("no route to host"))
    snapshot = usage.read(_creds(tmp_path))
    assert snapshot.fetched is False
    assert usage.check(snapshot).allow is True


def test_non_object_payload_allows_unmetered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _serve(monkeypatch, ["not", "an", "object"])
    snapshot = usage.read(_creds(tmp_path))
    assert snapshot.fetched is False
    assert usage.check(snapshot).allow is True


def test_a_fetch_with_no_metered_windows_is_unmetered_not_a_refusal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _serve(monkeypatch, {"spend": {"enabled": False}})
    snapshot = usage.read(_creds(tmp_path))
    assert snapshot.fetched is True and snapshot.buckets == ()
    verdict = usage.check(snapshot)
    assert verdict.allow is True and verdict.metered is False


# --------------------------------------------------------------- parsing the payload

def test_buckets_come_from_the_payload_not_a_fixed_list(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch):
    _serve(monkeypatch, _LIVE)
    names = [b.name for b in usage.read(_creds(tmp_path)).buckets]
    assert "five_hour" in names and "seven_day" in names
    # An unfamiliar codename is a meter like any other the day it starts reporting.
    assert "nimbus_quill" in names
    # Nulls and non-meters are dropped rather than crashing or arriving as zeroes.
    assert "seven_day_sonnet" not in names and "tangelo" not in names
    assert "member_dashboard_available" not in names
    assert "spend" not in names and "extra_usage" not in names


def test_a_renamed_window_still_meters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The whole point of not hardcoding: an unknown name at 100% must still refuse."""
    _serve(monkeypatch, {"some_future_window": {"utilization": 100.0, "resets_at": None},
                         "extra_usage": {"is_enabled": True}})
    snapshot = usage.read(_creds(tmp_path))
    assert usage.check(snapshot).allow is False


def test_paid_state_and_spend_are_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _serve(monkeypatch, _LIVE)
    snapshot = usage.read(_creds(tmp_path))
    assert snapshot.paid_enabled is True
    assert snapshot.paid_used_minor == 7212
    assert snapshot.paid_used_display == "72.12 EUR"
    assert snapshot.subscription == "max"


def test_either_paid_block_alone_is_enough_to_arm_the_rule(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch):
    _serve(monkeypatch, {"five_hour": {"utilization": 100.0},
                         "extra_usage": {"is_enabled": True}})
    assert usage.read(_creds(tmp_path)).paid_enabled is True
    _serve(monkeypatch, {"five_hour": {"utilization": 100.0},
                         "spend": {"enabled": True}})
    assert usage.read(_creds(tmp_path)).paid_enabled is True
    _serve(monkeypatch, {"five_hour": {"utilization": 100.0},
                         "extra_usage": {"is_enabled": False},
                         "spend": {"enabled": False}})
    assert usage.read(_creds(tmp_path)).paid_enabled is False


def test_the_token_is_found_however_the_file_nests_it(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch):
    _serve(monkeypatch, _LIVE)
    assert usage.read(_creds(tmp_path, nest=False)).fetched is True
    assert usage.read(_creds(tmp_path, nest=True)).fetched is True


def test_reset_times_come_back_naive_and_local(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch):
    """`limits.py` reasons in naive local time; mixing the two would raise at compare."""
    _serve(monkeypatch, _LIVE)
    stamps = [b.resets_at for b in usage.read(_creds(tmp_path)).buckets if b.resets_at]
    assert stamps and all(s.tzinfo is None for s in stamps)
    assert min(stamps) < max(stamps)          # both windows parsed, not one twice


def test_earliest_reset_ignores_windows_that_are_not_spent():
    """A healthy window's reset says nothing about when blocked work resumes."""
    soon = dt.datetime(2026, 8, 14, 11, 0)
    later = dt.datetime(2026, 8, 20, 23, 0)
    snapshot = _snap(_bucket("healthy", 10.0, soon), _bucket("spent", 100.0, later),
                     paid=True)
    assert snapshot.earliest_reset == later
    assert usage.check(snapshot).resume_at == later


def test_headroom_never_goes_negative():
    assert _bucket("over", 143.0).headroom_pct == 0.0


# ------------------------------------------------------------------------ the CLI

def test_cli_exit_codes_distinguish_refusal_from_error(tmp_path: Path, capsys,
                                                       monkeypatch: pytest.MonkeyPatch):
    """A distinct refusal code is what lets the runner branch without parsing prose."""
    monkeypatch.setattr(usage, "CREDENTIALS", _creds(tmp_path))

    _serve(monkeypatch, _LIVE)
    assert usage.main([]) == 0                      # 20% used, healthy

    spent = dict(_LIVE, five_hour={"utilization": 100.0, "resets_at": None})
    _serve(monkeypatch, spent)
    assert usage.main([]) == 3                      # refused for money
    assert "override: re-run with --allow-paid" in capsys.readouterr().out

    assert usage.main(["--allow-paid"]) == 0        # the explicit decision


def test_cli_json_carries_the_verdict(tmp_path: Path, capsys,
                                      monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(usage, "CREDENTIALS", _creds(tmp_path))
    _serve(monkeypatch, _LIVE)
    usage.main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["allow"] is True and payload["metered"] is True
    assert payload["paid_enabled"] is True
    assert {b["name"] for b in payload["buckets"]} >= {"five_hour", "seven_day"}


def test_describe_reports_the_paid_state_either_way():
    on = usage.describe(usage.Snapshot(buckets=(_bucket("five_hour", 20.0),),
                                       paid_enabled=True, paid_used_minor=7212,
                                       paid_currency="EUR", fetched=True))
    assert any("ENABLED" in line and "72.12 EUR" in line for line in on)
    off = usage.describe(_snap(_bucket("five_hour", 20.0), paid=False))
    assert any("disabled" in line for line in off)
    # ASCII in every runtime string: these land on a Windows console (cp1252) and in
    # the corrections log, and an em-dash renders as a replacement char in both.
    assert usage.describe(usage.Snapshot(reason="boom")) == ["usage unavailable - boom"]


def test_no_runtime_string_carries_non_ascii():
    """Docstrings may; anything that reaches a console or a log may not."""
    from nightshift import usage as module
    snapshots = [
        usage.Snapshot(reason="boom"),
        usage.Snapshot(buckets=(_bucket("five_hour", 100.0),), paid_enabled=True,
                       paid_used_minor=7212, paid_currency="EUR", fetched=True),
    ]
    emitted = [line for s in snapshots for line in module.describe(s)]
    emitted += [module.check(s, allow_paid=paid).reason
                for s in snapshots for paid in (True, False)]
    offenders = [text for text in emitted if any(ord(ch) > 127 for ch in text)]
    assert offenders == []
