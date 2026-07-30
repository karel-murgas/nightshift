"""Tests for `aiteam/gates/run.py` — the part of the move that is not a move.

Everything else in step 2 is the same code in a new home. Gate discovery is new
behaviour, because the split it implements is permanent: core gates ship in the
package, a project's own gates stay in its `.ai/gates/`, and 00_architecture.md
§15's rule (a gate is earned by an observed failure *here*) means that will never
collapse back into one directory.

Two properties matter, and one of them is a refusal:

* a project's gates are found and driven, with no registration step — the same
  "add the file, it runs" property the single-directory version had;
* a name collision is an **error**. "Which one ran?" must not be answered by a
  precedence rule nobody remembers.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from aiteam.gates import run as gates_run

GATE = '''
from aiteam.gates.base import Violation

def check(root):
    return [Violation("{file}", 1, "{rule}")]
'''

HELPER = '''
"""A module the project's gates import, not a gate itself."""

def helpfully(x):
    return x
'''


def _project(tmp_path: Path, **gates: str) -> Path:
    """A repo with a manifest and some gates of its own."""
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text("", encoding="utf-8", newline="")
    gate_dir = tmp_path / ".ai" / "gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    for name, source in gates.items():
        (gate_dir / f"{name}.py").write_text(source, encoding="utf-8", newline="")
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_import_state():
    """Discovery imports by bare module name off `sys.path`, so a gate called
    `no_tabs` in one test would be served from the import cache in the next. The
    real runner is one process per invocation and never sees this; the test file is
    the only place where two different trees are discovered in one interpreter."""
    before_path = list(sys.path)
    before_modules = set(sys.modules)
    yield
    sys.path[:] = before_path
    for name in set(sys.modules) - before_modules:
        del sys.modules[name]


# --- the project's own gates -------------------------------------------------

def test_a_project_gate_is_found_with_no_registration_step(tmp_path):
    root = _project(tmp_path, no_tabs=GATE.format(file="a.py", rule="tabs"))
    assert "no_tabs" in gates_run.discover(root)


def test_a_project_gate_runs_and_its_violations_are_reported(tmp_path, capsys):
    root = _project(tmp_path, no_tabs=GATE.format(file="a.py", rule="tabs are forbidden"))
    assert gates_run.main(["--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "a.py:1 — tabs are forbidden" in out
    assert "1 violation(s)" in out


def test_a_clean_project_reports_all_clear(tmp_path, capsys):
    root = _project(tmp_path, always_ok="def check(root):\n    return []\n")
    assert gates_run.main(["--root", str(root)]) == 0
    assert "All clear" in capsys.readouterr().out


def test_a_module_without_a_check_is_a_helper_not_a_gate(tmp_path):
    """`doc_scan`, `i18n_common` and `appeal_markers` are all this shape. Skipping
    by "has no check" rather than by name is why adding a helper has never
    required editing the runner."""
    root = _project(tmp_path, doc_scan=HELPER, real_gate="def check(root):\n    return []\n")
    found = gates_run.discover(root)
    assert "real_gate" in found and "doc_scan" not in found


def test_naming_an_unknown_gate_lists_what_is_available(tmp_path, capsys):
    root = _project(tmp_path, real_gate="def check(root):\n    return []\n")
    assert gates_run.main(["--root", str(root), "nope"]) == 2
    out = capsys.readouterr().out
    assert "Unknown gate(s): nope" in out and "real_gate" in out


# --- the two directories -----------------------------------------------------

def test_both_directories_are_searched(tmp_path):
    """Core ships no gates yet (step 3), so this asserts the mechanism rather than
    a count: the core directory is in the search path, and the project's is too."""
    root = _project(tmp_path, mine="def check(root):\n    return []\n")
    dirs = gates_run.gate_dirs(root)
    assert dirs[0] == Path(gates_run.__file__).resolve().parent
    assert dirs[-1] == root / ".ai" / "gates"


def test_a_repo_with_no_gates_of_its_own_is_fine(tmp_path):
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text("", encoding="utf-8", newline="")
    assert gates_run.gate_dirs(tmp_path) == [Path(gates_run.__file__).resolve().parent]
    assert gates_run.discover(tmp_path) == {}


def test_a_name_collision_between_core_and_project_is_refused(tmp_path, monkeypatch):
    """The failure this prevents is silent: a project writes `line_endings.py`, core
    already ships one, and the suite goes green (or red) on whichever import won."""
    core = tmp_path / "core_gates"
    core.mkdir()
    (core / "clash.py").write_text(GATE.format(file="core.py", rule="core"),
                                   encoding="utf-8", newline="")
    monkeypatch.setattr(gates_run, "_CORE_GATES_DIR", core)
    root = _project(tmp_path / "repo", clash=GATE.format(file="proj.py", rule="proj"))
    with pytest.raises(RuntimeError, match="two gates are named 'clash'"):
        gates_run.discover(root)


# --- it is runnable the way everything invokes it ----------------------------

def test_python_dash_m_finds_the_repo_from_the_working_directory(tmp_path):
    """`python -m aiteam.gates.run` with no `--root` is what the hooks, the
    preflight and the runner all use. Losing that is the failure mode of moving a
    script into a package: `Path(__file__).parent.parent` used to be the repo and
    is now a directory inside site-packages."""
    root = _project(tmp_path, always_ok="def check(root):\n    return []\n")
    nested = root / "pkg" / "sub"
    nested.mkdir(parents=True)
    done = subprocess.run([sys.executable, "-m", "aiteam.gates.run"], cwd=nested,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "All clear" in done.stdout
