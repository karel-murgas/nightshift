"""Tests for the `docstring_budget` core gate — a module docstring stays under a
line budget unless baselined, and the baseline only ever shrinks.

docstring-and-manifest-diet slice 3. Covers the five shapes the card asked for:
hit (unbaselined, over budget), miss (under budget), grown (baselined module's
docstring grew past its recorded count), shrunk-to-under-budget (a baselined
module now belongs off the baseline), and the `refresh()` invariant that a
regeneration never raises a recorded count.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

from nightshift.gates import corpus  # noqa: E402
from nightshift.gates import docstring_budget as gate  # noqa: E402


def _module(n_lines: int, *, n_dates: int = 0) -> str:
    """A module docstring of exactly `n_lines` body lines (plus opening/closing
    quotes), optionally citing `n_dates` distinct ISO dates."""
    body = [f"Line {i} of prose." for i in range(n_lines)]
    for i in range(n_dates):
        body.append(f"Measured 2026-0{(i % 9) + 1}-0{(i % 9) + 1}.")
    text = "\n".join(body)
    return f'"""{text}\n"""\ndef f():\n    pass\n'


def _repo(tmp_path: Path, name: str, source: str) -> Path:
    ai = tmp_path / ".ai"
    ai.mkdir(exist_ok=True)
    (ai / "gates").mkdir(exist_ok=True)
    (ai / "gates" / "data").mkdir(exist_ok=True)
    (ai / name).write_text(source, encoding="utf-8")
    corpus.clear()
    return tmp_path


def _set_local_baseline(root: Path, data: dict[str, int]) -> None:
    path = gate._local_baseline_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _messages(violations) -> str:
    return " ".join(v.rule for v in violations)


# --- hit / miss --------------------------------------------------------------

def test_an_unbaselined_over_budget_docstring_is_a_violation(tmp_path):
    root = _repo(tmp_path, "big.py", _module(gate.BUDGET_LINES + 5))
    violations = gate.check(root)
    assert violations, "an unbaselined module over budget must be reported"
    assert ".ai/big.py" in [v.file for v in violations]
    assert "over the" in _messages(violations)


def test_an_unbaselined_under_budget_docstring_is_not_reported(tmp_path):
    root = _repo(tmp_path, "small.py", _module(gate.BUDGET_LINES - 2))
    violations = gate.check(root)
    assert not violations, f"a module under budget must not be reported: {_messages(violations)}"


# --- grown ---------------------------------------------------------------------

def test_a_baselined_module_that_grew_past_its_recorded_count_is_a_violation(tmp_path):
    n = gate.BUDGET_LINES + 10
    root = _repo(tmp_path, "grew.py", _module(n))
    _set_local_baseline(root, {".ai/grew.py": n - 3})  # recorded lower than actual
    violations = gate.check(root)
    assert violations, "a docstring that grew past its baselined count must be reported"
    assert "grew from" in _messages(violations)


def test_a_baselined_module_at_its_recorded_count_is_not_reported(tmp_path):
    n = gate.BUDGET_LINES + 10
    root = _repo(tmp_path, "steady.py", _module(n))
    _set_local_baseline(root, {".ai/steady.py": n})
    violations = gate.check(root)
    assert not violations, f"an unchanged baselined module must not be reported: {_messages(violations)}"


# --- shrunk-to-under-budget ---------------------------------------------------

def test_a_baselined_module_now_under_budget_must_be_removed(tmp_path):
    root = _repo(tmp_path, "shrunk.py", _module(gate.BUDGET_LINES - 1))
    _set_local_baseline(root, {".ai/shrunk.py": gate.BUDGET_LINES + 10})
    violations = gate.check(root)
    assert violations, "a baselined module now under budget must be flagged to remove"
    assert "remove" in _messages(violations)


def test_a_baselined_module_still_over_budget_but_not_grown_is_quiet(tmp_path):
    """Shrinking without reaching the budget is encouraged, not demanded by this
    gate -- that is the slice-2 chore's job, not a hard requirement here."""
    n = gate.BUDGET_LINES + 10
    root = _repo(tmp_path, "partial.py", _module(n))
    _set_local_baseline(root, {".ai/partial.py": n + 5})  # shrank from 5-over-that to n, still > budget
    violations = gate.check(root)
    assert not violations, f"shrinking without going under budget must not be reported: {_messages(violations)}"


# --- baseline-only-shrinks (the `refresh()` invariant) ------------------------

def test_refresh_never_raises_a_recorded_count(tmp_path):
    n = gate.BUDGET_LINES + 20
    root = _repo(tmp_path, "regrew.py", _module(n))
    _set_local_baseline(root, {".ai/regrew.py": gate.BUDGET_LINES + 1})  # far below the real count

    local_new, _ = gate.refresh(root)
    assert local_new[".ai/regrew.py"] == gate.BUDGET_LINES + 1, (
        "a refresh must never absorb growth into the baseline -- only a deliberate "
        "manual edit may raise a recorded count")


def test_refresh_drops_a_module_that_is_now_under_budget(tmp_path):
    root = _repo(tmp_path, "fixed.py", _module(gate.BUDGET_LINES - 1))
    _set_local_baseline(root, {".ai/fixed.py": gate.BUDGET_LINES + 10})

    local_new, _ = gate.refresh(root)
    assert ".ai/fixed.py" not in local_new, "a module back under budget must leave the baseline"


def test_refresh_lowers_a_count_that_shrank_but_stayed_over_budget(tmp_path):
    n = gate.BUDGET_LINES + 5
    root = _repo(tmp_path, "trimmed.py", _module(n))
    _set_local_baseline(root, {".ai/trimmed.py": gate.BUDGET_LINES + 30})

    local_new, _ = gate.refresh(root)
    assert local_new[".ai/trimmed.py"] == n, "refresh may lower a recorded count, only never raise it"


# --- dated narrative (optional half, unbaselined modules only) ---------------

def test_an_unbaselined_module_with_many_dates_is_reported(tmp_path):
    root = _repo(tmp_path, "dated.py", _module(3, n_dates=gate.DATE_THRESHOLD))
    violations = gate.check(root)
    assert violations, "a short-but-dated docstring must still be reported"
    assert "dates" in _messages(violations)


def test_a_baselined_over_budget_module_is_not_double_flagged_for_dates(tmp_path):
    """Every baselined module is over budget for close to this exact reason --
    flagging all of them for dates too the day this gate ships would not be
    low-noise, which the module docstring says is the bar for this half."""
    n = gate.BUDGET_LINES + 10
    root = _repo(tmp_path, "grandfathered.py", _module(n, n_dates=gate.DATE_THRESHOLD))
    _set_local_baseline(root, {".ai/grandfathered.py": n + gate.DATE_THRESHOLD})
    violations = gate.check(root)
    assert not violations, f"a baselined module must not be flagged for dates: {_messages(violations)}"


# --- appeal ---------------------------------------------------------------------

def test_gate_ok_appeal_exempts_an_over_budget_module(tmp_path):
    source = _module(gate.BUDGET_LINES + 5)
    # The appeal must land near the docstring's closing line, which `_report`
    # checks against with a window of 3.
    lines = source.splitlines()
    close_at = next(i for i, l in enumerate(lines) if l == '"""')
    lines.insert(close_at + 1,
                 "# gate-ok(docstring_budget): illustrative fixture, not real prose, kept long on purpose.")
    root = _repo(tmp_path, "appealed.py", "\n".join(lines) + "\n")
    violations = gate.check(root)
    assert not violations, f"an appealed module must not be reported: {_messages(violations)}"
