"""Tests for the `gates_on_edit` PostToolUse hook and the `session_memo` it keeps.

The measurement behind it (`token-economy.md` §1): the on-edit gate run printed
50–143k characters into a worker's context per card — the same 51-name all-clear
line and the same violation lines after every edit. The rule under test is *what
reaches the model*: nothing but `gates: ok` when clean, a violation's text once,
and a violation that comes back after being fixed shown again.
"""
from __future__ import annotations

import json
import types

import pytest

from nightshift.gates.base import Violation
from nightshift.hooks import gates_on_edit, session_memo


@pytest.fixture(autouse=True)
def _private_memo(tmp_path, monkeypatch):
    monkeypatch.setattr(session_memo, "_DIR", tmp_path / "memo")
    monkeypatch.setattr(gates_on_edit, "_timings_path",
                        lambda root: tmp_path / "memo" / "timings.json")


def _gates(monkeypatch, table: dict[str, list[Violation]], slow: set[str] = frozenset()):
    """Install fake gates; a gate in `slow` records a measured time above SLOW_S."""
    modules = {name: types.SimpleNamespace(check=lambda root, v=v: list(v))
               for name, v in table.items()}
    monkeypatch.setattr("nightshift.gates.run.discover", lambda root: modules)
    if slow:
        timings = {name: (gates_on_edit.SLOW_S * 4 if name in slow else 0.01)
                   for name in table}
        path = gates_on_edit._timings_path(None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(timings), encoding="utf-8")
    return modules


def _v(file: str, rule: str, line: int = 1) -> Violation:
    return Violation(file=file, line=line, rule=rule)


def test_a_clean_run_prints_one_line_and_never_the_gate_names(tmp_path, monkeypatch):
    _gates(monkeypatch, {"alpha_gate": [], "beta_gate": []})
    out = gates_on_edit.run(tmp_path, "s1", armed=False)
    assert out == "gates: ok"


def test_a_violation_is_shown_once_then_counted(tmp_path, monkeypatch):
    table = {"parity": [_v("i18n.py", "key missing in cs")]}
    _gates(monkeypatch, table)
    first = gates_on_edit.run(tmp_path, "s1", armed=False)
    assert "key missing in cs" in first and "1 new violation" in first

    second = gates_on_edit.run(tmp_path, "s1", armed=False)
    assert "key missing in cs" not in second
    assert "1 violation(s) still open" in second


def test_a_moved_line_is_not_a_new_violation(tmp_path, monkeypatch):
    table = {"parity": [_v("i18n.py", "key missing in cs", line=10)]}
    _gates(monkeypatch, table)
    gates_on_edit.run(tmp_path, "s1", armed=False)
    table["parity"][0] = _v("i18n.py", "key missing in cs", line=14)
    assert "still open" in gates_on_edit.run(tmp_path, "s1", armed=False)


def test_a_fixed_then_reintroduced_violation_is_news_again(tmp_path, monkeypatch):
    table = {"parity": [_v("i18n.py", "key missing in cs")]}
    _gates(monkeypatch, table)
    gates_on_edit.run(tmp_path, "s1", armed=False)
    table["parity"].clear()
    assert gates_on_edit.run(tmp_path, "s1", armed=False) == "gates: ok"
    table["parity"].append(_v("i18n.py", "key missing in cs"))
    assert "key missing in cs" in gates_on_edit.run(tmp_path, "s1", armed=False)


def test_sessions_do_not_share_what_they_were_told(tmp_path, monkeypatch):
    _gates(monkeypatch, {"parity": [_v("i18n.py", "key missing in cs")]})
    gates_on_edit.run(tmp_path, "s1", armed=False)
    assert "key missing in cs" in gates_on_edit.run(tmp_path, "s2", armed=False)


def test_without_a_session_id_everything_is_shown_every_time(tmp_path, monkeypatch):
    _gates(monkeypatch, {"parity": [_v("i18n.py", "key missing in cs")]})
    for _ in range(2):
        assert "key missing in cs" in gates_on_edit.run(tmp_path, "", armed=False)


def test_a_dispatched_worker_defers_measured_slow_gates_and_is_told(tmp_path, monkeypatch):
    _gates(monkeypatch, {"fast": [], "sweep": [_v("doc.md", "dead ref")]}, slow={"sweep"})
    out = gates_on_edit.run(tmp_path, "s1", armed=True)
    assert out.startswith("gates: ok")
    assert "1 slow gate(s) deferred" in out and "nightshift.gates.run" in out


def test_an_interactive_session_runs_the_slow_gates_too(tmp_path, monkeypatch):
    _gates(monkeypatch, {"fast": [], "sweep": [_v("doc.md", "dead ref")]}, slow={"sweep"})
    assert "dead ref" in gates_on_edit.run(tmp_path, "s1", armed=False)


def test_a_crashing_gate_is_reported_not_swallowed(tmp_path, monkeypatch):
    def boom(root):
        raise RuntimeError("bad parse")
    monkeypatch.setattr("nightshift.gates.run.discover",
                        lambda root: {"broken": types.SimpleNamespace(check=boom)})
    out = gates_on_edit.run(tmp_path, "s1", armed=False)
    assert "broken" in out and "bad parse" in out


def test_unseen_reports_each_item_once_per_session():
    assert session_memo.unseen("s1", "hints", ["a", "b"]) == ["a", "b"]
    assert session_memo.unseen("s1", "hints", ["a", "b", "c"]) == ["c"]
    assert session_memo.unseen("s2", "hints", ["a"]) == ["a"]
    assert session_memo.unseen("", "hints", ["a"]) == ["a"]
    assert session_memo.unseen("", "hints", ["a"]) == ["a"]
