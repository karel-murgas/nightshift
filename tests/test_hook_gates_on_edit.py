"""Tests for the `gates_on_edit` PostToolUse hook and the `session_memo` it keeps.

The measurement behind it (`token-economy.md` §1): the on-edit gate run printed
50–143k characters into a worker's context per card — the same 51-name all-clear
line and the same violation lines after every edit. The rule under test is *what
reaches the model*: nothing but `gates: ok` when clean, a violation's text once,
and a violation that comes back after being fixed shown again — with every gate
still run on every edit.
"""
from __future__ import annotations

import types

import pytest

from nightshift.gates.base import Violation
from nightshift.hooks import gates_on_edit, session_memo


@pytest.fixture(autouse=True)
def _private_memo(tmp_path, monkeypatch):
    monkeypatch.setattr(session_memo, "_DIR", tmp_path / "memo")


def _gates(monkeypatch, table: dict[str, list[Violation]], ran: list[str] | None = None):
    def module(name, found):
        def check(root):
            if ran is not None:
                ran.append(name)
            return list(found)
        return types.SimpleNamespace(check=check)
    modules = {name: module(name, v) for name, v in table.items()}
    monkeypatch.setattr("nightshift.gates.run.discover", lambda root: modules)


def _v(file: str, rule: str, line: int = 1) -> Violation:
    return Violation(file=file, line=line, rule=rule)


def test_a_clean_run_prints_one_line_and_never_the_gate_names(tmp_path, monkeypatch):
    _gates(monkeypatch, {"alpha_gate": [], "beta_gate": []})
    assert gates_on_edit.run(tmp_path, "s1") == "gates: ok"


def test_every_gate_runs_on_every_edit(tmp_path, monkeypatch):
    """Quieter output, never fewer checks: a violation is seen at the edit that
    caused it, not at the end of the attempt."""
    ran: list[str] = []
    _gates(monkeypatch, {"fast": [], "slow_sweep": [], "dead_code": []}, ran)
    gates_on_edit.run(tmp_path, "s1")
    gates_on_edit.run(tmp_path, "s1")
    assert sorted(ran) == sorted(["fast", "slow_sweep", "dead_code"] * 2)


def test_a_gate_declaring_on_edit_false_is_left_to_the_full_suite(tmp_path, monkeypatch):
    """The one exemption: a gate whose subject no edit can change opts out by name,
    and only that gate is skipped."""
    ran: list[str] = []
    _gates(monkeypatch, {"history_audit": [_v("Board/done/a.md", "r")], "tree_gate": []}, ran)
    import nightshift.gates.run as gates_run
    gates_run.discover(tmp_path)["history_audit"].ON_EDIT = False
    assert gates_on_edit.run(tmp_path, "s1") == "gates: ok"
    assert ran == ["tree_gate"]


def test_a_violation_is_shown_once_then_counted(tmp_path, monkeypatch):
    _gates(monkeypatch, {"parity": [_v("i18n.py", "key missing in cs")]})
    first = gates_on_edit.run(tmp_path, "s1")
    assert "key missing in cs" in first and "1 new violation" in first

    second = gates_on_edit.run(tmp_path, "s1")
    assert "key missing in cs" not in second
    assert "1 violation(s) still open" in second


def test_a_moved_line_is_not_a_new_violation(tmp_path, monkeypatch):
    table = {"parity": [_v("i18n.py", "key missing in cs", line=10)]}
    _gates(monkeypatch, table)
    gates_on_edit.run(tmp_path, "s1")
    table["parity"][0] = _v("i18n.py", "key missing in cs", line=14)
    assert "still open" in gates_on_edit.run(tmp_path, "s1")


def test_a_fixed_then_reintroduced_violation_is_news_again(tmp_path, monkeypatch):
    table = {"parity": [_v("i18n.py", "key missing in cs")]}
    _gates(monkeypatch, table)
    gates_on_edit.run(tmp_path, "s1")
    table["parity"].clear()
    assert gates_on_edit.run(tmp_path, "s1") == "gates: ok"
    table["parity"].append(_v("i18n.py", "key missing in cs"))
    assert "key missing in cs" in gates_on_edit.run(tmp_path, "s1")


def test_sessions_do_not_share_what_they_were_told(tmp_path, monkeypatch):
    _gates(monkeypatch, {"parity": [_v("i18n.py", "key missing in cs")]})
    gates_on_edit.run(tmp_path, "s1")
    assert "key missing in cs" in gates_on_edit.run(tmp_path, "s2")


def test_without_a_session_id_everything_is_shown_every_time(tmp_path, monkeypatch):
    _gates(monkeypatch, {"parity": [_v("i18n.py", "key missing in cs")]})
    for _ in range(2):
        assert "key missing in cs" in gates_on_edit.run(tmp_path, "")


def test_a_gate_with_fix_is_fixed_before_it_is_checked(tmp_path, monkeypatch):
    """Mechanical hygiene (CRLF, a missing trailing newline) is fixed silently,
    in place, before `check()` runs — the violation it would otherwise report
    generally never exists to report (hygiene-rules-belong-in-a-script card)."""
    state = {"broken": True}

    def check(root):
        return [_v("a.py", "CRLF")] if state["broken"] else []

    def fix(root):
        state["broken"] = False

    monkeypatch.setattr(
        "nightshift.gates.run.discover",
        lambda root: {"line_endings": types.SimpleNamespace(check=check, fix=fix)},
    )
    assert gates_on_edit.run(tmp_path, "s1") == "gates: ok"


def test_a_gate_with_no_fix_method_is_only_checked(tmp_path, monkeypatch):
    """No behaviour change for a gate that defines no `fix()` — same as before
    this card."""
    _gates(monkeypatch, {"parity": [_v("i18n.py", "key missing in cs")]})
    out = gates_on_edit.run(tmp_path, "s1")
    assert "key missing in cs" in out


def test_a_crashing_gate_is_reported_not_swallowed(tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("bad parse")
    monkeypatch.setattr("nightshift.gates.run.discover",
                        lambda root: {"broken": types.SimpleNamespace(check=boom)})
    out = gates_on_edit.run(tmp_path, "s1")
    assert "broken" in out and "bad parse" in out


def test_unseen_reports_each_item_once_per_session():
    assert session_memo.unseen("s1", "hints", ["a", "b"]) == ["a", "b"]
    assert session_memo.unseen("s1", "hints", ["a", "b", "c"]) == ["c"]
    assert session_memo.unseen("s2", "hints", ["a"]) == ["a"]
    assert session_memo.unseen("", "hints", ["a"]) == ["a"]
    assert session_memo.unseen("", "hints", ["a"]) == ["a"]


def test_peek_sees_what_swap_last_recorded_without_recording_anything():
    assert session_memo.peek("s1", "gates") is None, "nothing swapped yet"
    session_memo.swap("s1", "gates", ["violation-a"])
    assert session_memo.peek("s1", "gates") == {"violation-a"}
    assert session_memo.peek("s1", "gates") == {"violation-a"}, "peek must not consume"
    session_memo.swap("s1", "gates", [])
    assert session_memo.peek("s1", "gates") == set(), "green is recorded, not absent"


def test_peek_without_a_session_id_or_channel_is_none():
    assert session_memo.peek("", "gates") is None
    session_memo.swap("s1", "gates", ["violation-a"])
    assert session_memo.peek("s1", "other-channel") is None
