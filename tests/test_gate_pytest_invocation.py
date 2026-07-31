"""Tests for the `pytest_invocation` core gate — the parallel flags live in one
place: a project's own `.ai/suite.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import pytest_invocation  # noqa: E402


def _tree(tmp_path: Path, name: str, source: str) -> Path:
    ai = tmp_path / ".ai"
    ai.mkdir(exist_ok=True)
    (ai / name).write_text(source, encoding="utf-8")
    return tmp_path


def test_a_hand_rolled_parallel_argv_is_caught(tmp_path):
    """The regression the gate exists for: a fourth caller spelling the flags
    itself instead of asking `suite`."""
    root = _tree(tmp_path, "newthing.py", (
        "import subprocess, sys\n"
        'subprocess.run([sys.executable, "-m", "pytest", "tests/", "-n", "auto",\n'
        '                "--dist", "loadfile"])\n'
    ))
    violations = pytest_invocation.check(root)
    assert violations, "a hand-rolled -n/--dist argv must be reported"
    assert any("suite.parallel_args()" in v.rule for v in violations)


def test_the_unsafe_dist_value_is_caught_too(tmp_path):
    """`--dist load` splits a file across workers and a file-coupled suite flakes
    under it. Watching the flag name rather than `loadfile` is what catches the
    broken spelling."""
    root = _tree(tmp_path, "newthing.py", 'ARGS = ["-n", "auto", "--dist", "load"]\n')
    assert pytest_invocation.check(root)


def test_the_long_form_flag_is_caught(tmp_path):
    root = _tree(tmp_path, "newthing.py", 'ARGS = ["--numprocesses", "4"]\n')
    assert pytest_invocation.check(root)


def test_suite_py_itself_is_allowed_to_hold_the_tuple(tmp_path):
    """It is the owner of the policy — that is the whole point."""
    root = _tree(tmp_path, "suite.py",
                 'XDIST_ARGS = ("-n", "auto", "--dist", "loadfile")\n')
    assert pytest_invocation.check(root) == []


def test_prose_about_running_pytest_is_not_a_violation(tmp_path):
    """A worker prompt telling an agent to run `-n auto --dist loadfile` itself
    is an instruction, not an invocation, and a gate that flagged its own
    documentation would be muted. Only sequence literals are inspected."""
    root = _tree(tmp_path, "newthing.py", (
        '"""Run `python -m pytest tests/ -n auto --dist loadfile` for speed."""\n'
        '# -n auto --dist loadfile is the fast path\n'
        'PROMPT = "run pytest with -n auto --dist loadfile yourself"\n'
    ))
    assert pytest_invocation.check(root) == []


def test_a_call_to_parallel_args_is_the_blessed_form(tmp_path):
    root = _tree(tmp_path, "newthing.py", (
        "import suite\n"
        'ARGV = [*suite.parallel_args(), "--junitxml=x.xml"]\n'
    ))
    assert pytest_invocation.check(root) == []


def test_an_appeal_marker_exempts_a_line(tmp_path):
    root = _tree(tmp_path, "newthing.py", (
        "# gate-ok(pytest_invocation): a deliberate one-off, explained here\n"
        'ARGS = ["-n", "auto"]\n'
    ))
    assert pytest_invocation.check(root) == []


def test_an_unparseable_file_is_not_this_gates_problem(tmp_path):
    root = _tree(tmp_path, "broken.py", "def nope(:\n")
    assert pytest_invocation.check(root) == []


def test_no_ai_dir_is_not_a_crash(tmp_path):
    assert pytest_invocation.check(tmp_path) == []
