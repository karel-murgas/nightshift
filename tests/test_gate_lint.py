"""Tests for the `lint` gate — ruff wrapped the same way `dead_code` wraps
vulture, with the same three properties to prove:

* the `[lint]` table is an **override, never a prerequisite** — a repo with
  no such table still gets checked, on `source_dirs` at nightshift's own
  default `select`, because a new project gets this from `pip install` alone;
* a **missing ruff is a violation**, not a skip;
* so is ruff **failing, erroring on its own arguments, or changing its output
  format**. All three look identical to "clean" from the outside.

The detection itself is ruff's and is not re-tested here. What is tested is
everything around it: configuration, the always-explicit `--select`, and
every way this gate can report success without having checked anything.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import lint  # noqa: E402

_UNUSED_IMPORT = '''\
import json


def live(n):
    return n * 2
'''

_CLEAN = '''\
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

def test_a_manifest_with_no_lint_table_still_checks_source_dirs(tmp_path):
    """The property the whole card turns on. A project that has configured
    nothing gets lint from the install, or "nothing to add manually" is not
    true for the repo that has not configured it yet."""
    root = _project(tmp_path, _UNUSED_IMPORT, manifest=_SOURCE_DIRS_ONLY)
    rules = [v.rule for v in lint.check(root)]
    assert any("F401" in r and "json" in r for r in rules), rules


def test_a_clean_source_dir_reports_nothing(tmp_path):
    root = _project(tmp_path, _CLEAN, manifest=_SOURCE_DIRS_ONLY)
    assert lint.check(root) == []


def test_no_manifest_at_all_is_silence_not_a_guessed_tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(_UNUSED_IMPORT, encoding="utf-8",
                                             newline="")
    assert lint.check(tmp_path) == []


# --- the table overrides -------------------------------------------------------

def test_the_table_selects_paths_that_source_dirs_does_not_cover(tmp_path):
    root = _project(tmp_path, _CLEAN, manifest=(
        '[project]\nsource_dirs = ["pkg"]\n'
        '[lint]\npaths = ["pkg", "main.py"]\n'
    ))
    (root / "main.py").write_text("import os\n", encoding="utf-8", newline="")
    rules = [v.rule for v in lint.check(root)]
    assert any("F401" in r and "os" in r for r in rules), rules
    assert all(v.file == "main.py" for v in lint.check(root))


def test_ignore_subtracts_a_code_back_out_of_select(tmp_path):
    root = _project(tmp_path, _UNUSED_IMPORT, manifest=(
        _SOURCE_DIRS_ONLY + '[lint]\nignore = ["F401"]\n'
    ))
    assert lint.check(root) == []


def test_a_narrowed_select_only_checks_the_named_codes(tmp_path):
    """A file with both an unused import (F401) and a bare except (E722):
    selecting only F401 must not surface the other."""
    source = '''\
import json


def live():
    try:
        pass
    except:
        pass
'''
    root = _project(tmp_path, source, manifest=(
        _SOURCE_DIRS_ONLY + '[lint]\nselect = ["F401"]\n'
    ))
    rules = [v.rule for v in lint.check(root)]
    assert any("F401" in r for r in rules)
    assert not any("E722" in r for r in rules)


def test_findings_carry_a_real_file_and_line(tmp_path):
    root = _project(tmp_path, _UNUSED_IMPORT, manifest=_SOURCE_DIRS_ONLY)
    found = lint.check(root)
    assert len(found) == 1
    assert found[0].file == "pkg/mod.py", "not repo-relative posix"
    assert found[0].line == 1
    assert (root / found[0].file).is_file()


# --- every way this could pass without checking anything ----------------------

def test_a_missing_ruff_is_a_violation_never_an_empty_result(tmp_path, monkeypatch):
    root = _project(tmp_path, _UNUSED_IMPORT, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(lint, "find_spec", lambda name: None)
    found = lint.check(root)
    assert len(found) == 1
    assert "ruff is not installed" in found[0].rule
    assert "pip install" in found[0].rule, "the message must say how to fix it"


def test_a_manifest_declaring_no_paths_at_all_is_refused(tmp_path):
    root = _project(tmp_path, _UNUSED_IMPORT, manifest="[tests]\ndir = 'tests'\n")
    found = lint.check(root)
    assert len(found) == 1
    assert "nothing to scan" in found[0].rule


def test_an_unknown_select_code_is_a_loud_error_not_a_silent_pass(tmp_path):
    """ruff exits 2 on a bad `--select`/`--ignore` argument — a config typo
    that must not read as a clean tree."""
    root = _project(tmp_path, _CLEAN, manifest=(
        _SOURCE_DIRS_ONLY + '[lint]\nselect = ["NOTAREALCODE"]\n'
    ))
    found = lint.check(root)
    assert len(found) == 1
    assert "exited 2" in found[0].rule


def test_empty_output_is_loud_rather_than_clean(tmp_path, monkeypatch):
    root = _project(tmp_path, _CLEAN, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(lint, "_run", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 0, stdout="", stderr=""))
    found = lint.check(root)
    assert len(found) == 1
    assert "no output at all" in found[0].rule


def test_output_that_is_not_json_is_loud_rather_than_clean(tmp_path, monkeypatch):
    root = _project(tmp_path, _CLEAN, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(lint, "_run", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 1, stdout="not json at all", stderr=""))
    found = lint.check(root)
    assert len(found) == 1
    assert "did not parse as JSON" in found[0].rule


def test_json_that_is_not_a_list_is_loud_rather_than_clean(tmp_path, monkeypatch):
    root = _project(tmp_path, _CLEAN, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(lint, "_run", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 1, stdout=json.dumps({"not": "a list"}), stderr=""))
    found = lint.check(root)
    assert len(found) == 1
    assert "not a list of findings" in found[0].rule


def test_a_finding_with_an_unexpected_shape_is_reported_not_dropped(tmp_path, monkeypatch):
    root = _project(tmp_path, _CLEAN, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(lint, "_run", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 1, stdout=json.dumps([{"surprising": "shape"}]), stderr=""))
    found = lint.check(root)
    assert len(found) == 1
    assert "did not match the expected JSON shape" in found[0].rule


def test_an_unexpected_exit_code_carries_ruffs_own_message(tmp_path, monkeypatch):
    root = _project(tmp_path, _CLEAN, manifest=_SOURCE_DIRS_ONLY)
    monkeypatch.setattr(lint, "_run", lambda argv, cwd: subprocess.CompletedProcess(
        argv, 2, stdout="", stderr="ruff failed\n  Cause: something"))
    found = lint.check(root)
    assert len(found) == 1
    assert "exited 2" in found[0].rule
    assert "Cause: something" in found[0].rule


# --- it is a gate like any other ---------------------------------------------

def test_the_gate_is_discovered_and_driven_by_the_runner(tmp_path, capsys):
    from nightshift.gates import run as gates_run

    root = _project(tmp_path, _UNUSED_IMPORT, manifest=_SOURCE_DIRS_ONLY)
    assert "lint" in gates_run.discover(root)
    assert gates_run.main(["--root", str(root), "lint"]) == 1
    out = capsys.readouterr().out
    assert "pkg/mod.py:1 — lint: F401" in out
