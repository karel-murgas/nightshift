"""Tests for the `dead_code` gate — the first core gate with a real dependency.

Three properties, and two of them are refusals:

* the `[dead_code]` table is an **override, never a prerequisite** — a repo whose
  manifest has no such table still gets the check, on `source_dirs` at 80,
  because "a new project gets this from `pip install` alone" is the entire
  reason the gate is in core rather than in a project's `.ai/gates/`;
* a **missing vulture is a violation**, not a skip. A dead-code gate that
  no-ops when its tool is absent is green and wrong forever, and nobody
  investigates a gate that passes;
* so is **vulture failing or changing its output format**. Both look identical
  to "clean" from the outside, which is what makes them worth a test.

The detection itself is vulture's and is not re-tested here. What is tested is
everything around it: configuration, path selection, and every way this gate can
report success without having checked anything.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import dead_code  # noqa: E402

# One unused import (90% confidence) and one unused parameter (100%) — both
# above the default floor — plus an unused module-level constant and an unused
# local, which vulture rates at 60% and the default therefore must not report.
_DEAD = '''\
import json


LEFTOVER = 3


def live(n, extra):
    scratch = n * 2
    return n
'''

_ALIVE = '''\
def live(n):
    return n * 2
'''


def _project(tmp_path: Path, source: str, *, manifest: str,
             package: str = "pkg") -> Path:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(manifest, encoding="utf-8",
                                                    newline="")
    pkg = tmp_path / package
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "mod.py").write_text(source, encoding="utf-8", newline="")
    return tmp_path


_SOURCE_DIRS_ONLY = '[project]\nsource_dirs = ["pkg"]\n'


# --- no table at all: the gate still runs -------------------------------------

def test_a_manifest_with_no_dead_code_table_still_checks_source_dirs(tmp_path):
    """The property the whole card turns on. A project that has configured
    nothing gets dead-code detection from the install, or "nothing to add
    manually" is not true for the repo that has not configured it yet."""
    root = _project(tmp_path, _DEAD, manifest=_SOURCE_DIRS_ONLY)
    rules = [v.rule for v in dead_code.check(root)]
    assert any("'json'" in r for r in rules), rules
    assert any("'extra'" in r for r in rules), rules


def test_the_default_floor_is_80_so_60_percent_findings_stay_quiet(tmp_path):
    """`LEFTOVER` is an unused module-level constant — vulture's 60% band, where
    the genuinely interesting findings live. The default deliberately does not
    reach it: 80 is what an unswept tree can go green on, and a project lowers
    the number after it has cleared the band."""
    root = _project(tmp_path, _DEAD, manifest=_SOURCE_DIRS_ONLY)
    assert not any("LEFTOVER" in v.rule for v in dead_code.check(root))


def test_a_clean_source_dir_reports_nothing(tmp_path):
    root = _project(tmp_path, _ALIVE, manifest=_SOURCE_DIRS_ONLY)
    assert dead_code.check(root) == []


def test_no_manifest_at_all_is_silence_not_a_guessed_tree(tmp_path):
    """A repo nobody set nightshift up in. Guessing which directories to scan is
    how a tool ends up reporting against the wrong tree — `manifest.load`
    refuses the same guess."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(_DEAD, encoding="utf-8", newline="")
    assert dead_code.check(tmp_path) == []


# --- the table overrides ------------------------------------------------------

def test_the_table_selects_paths_that_source_dirs_does_not_cover(tmp_path):
    """The measured reason this field exists: an entry point outside the source
    dirs has to be scanned *with* them, because it is the sole consumer of
    imports that read as dead without it."""
    root = _project(tmp_path, _ALIVE, manifest=(
        '[project]\nsource_dirs = ["pkg"]\n'
        '[dead_code]\npaths = ["pkg", "main.py"]\n'
    ))
    (root / "main.py").write_text("import os\n", encoding="utf-8", newline="")
    rules = [v.rule for v in dead_code.check(root)]
    assert any(r.endswith("unused import 'os' (90% confidence)") for r in rules), rules
    assert all(v.file == "main.py" for v in dead_code.check(root))


def test_a_lowered_min_confidence_reaches_the_60_band(tmp_path):
    """What `dead-code-sweep`-shaped work buys: the same tree, the number moved
    to 60, and the findings that matter become visible."""
    root = _project(tmp_path, _DEAD, manifest=(
        _SOURCE_DIRS_ONLY + "[dead_code]\nmin_confidence = 60\n"
    ))
    assert any("LEFTOVER" in v.rule for v in dead_code.check(root))


def test_findings_carry_a_real_file_and_line(tmp_path):
    root = _project(tmp_path, _DEAD, manifest=_SOURCE_DIRS_ONLY)
    by_rule = {v.rule: v for v in dead_code.check(root)}
    unused_import = next(v for r, v in by_rule.items() if "'json'" in r)
    assert unused_import.file == "pkg/mod.py", "not repo-relative posix"
    assert unused_import.line == 1
    assert (root / unused_import.file).is_file()


# --- every way this could pass without checking anything ----------------------

def test_a_missing_vulture_is_a_violation_never_an_empty_result(tmp_path, monkeypatch):
    """Simulated at the seam where the gate learns the tool exists. The failure
    being prevented is not a crash — it is a green suite on a repo where the
    check never ran."""
    root = _project(tmp_path, _DEAD, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(dead_code, "find_spec", lambda name: None)
    found = dead_code.check(root)
    assert len(found) == 1
    assert "vulture is not installed" in found[0].rule
    assert "pip install" in found[0].rule, "the message must say how to fix it"


def test_a_declared_path_that_does_not_exist_is_reported(tmp_path):
    """vulture exits 1 on invalid input. Left unhandled that is an empty stdout,
    which parses as "clean"."""
    root = _project(tmp_path, _ALIVE, manifest=(
        _SOURCE_DIRS_ONLY + '[dead_code]\npaths = ["nope"]\n'
    ))
    found = dead_code.check(root)
    assert len(found) == 1
    assert "exited 1" in found[0].rule and "nope" in found[0].rule


def test_a_manifest_declaring_no_paths_at_all_is_refused(tmp_path):
    """Configured, but pointing at nothing. Silence would be a gate that passes
    forever without reading a line of the project."""
    root = _project(tmp_path, _DEAD, manifest="[tests]\ndir = 'tests'\n")
    found = dead_code.check(root)
    assert len(found) == 1
    assert "nothing to scan" in found[0].rule


def test_output_that_no_longer_parses_is_loud_rather_than_clean(tmp_path, monkeypatch):
    """vulture says it found dead code, in a format this gate cannot read. The
    dangerous outcome is not the traceback; it is the empty list."""
    root = _project(tmp_path, _ALIVE, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(dead_code, "_run", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 3, stdout="something entirely different\n", stderr=""))
    found = dead_code.check(root)
    assert len(found) == 1
    assert "format has changed" in found[0].rule


def test_an_unexpected_exit_code_carries_vultures_own_message(tmp_path, monkeypatch):
    root = _project(tmp_path, _ALIVE, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(dead_code, "_run", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 2, stdout="", stderr="vulture: error: unrecognized arguments\n"))
    found = dead_code.check(root)
    assert len(found) == 1
    assert "exited 2" in found[0].rule
    assert "unrecognized arguments" in found[0].rule


# --- it is a gate like any other ---------------------------------------------

def test_the_gate_is_discovered_and_driven_by_the_runner(tmp_path, capsys):
    from nightshift.gates import run as gates_run

    root = _project(tmp_path, _DEAD, manifest=_SOURCE_DIRS_ONLY)
    assert "dead_code" in gates_run.discover(root)
    assert gates_run.main(["--root", str(root), "dead_code"]) == 1
    assert "pkg/mod.py:1 — dead_code: unused import 'json'" in capsys.readouterr().out
