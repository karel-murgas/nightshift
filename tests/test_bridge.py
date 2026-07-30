"""Tests for `nightshift/bridge.py` — the seam that step 4 deletes.

There is exactly one thing worth testing about a temporary shim, and it is the
error message. A colleague who installs this package into a repo that has no
`.ai/runner.py` gets a bare `ModuleNotFoundError: No module named 'runner'`, which
reads as a broken install and sends them to pip. The true statement — this half of
the framework is not extracted yet — is the one that has to come out.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nightshift import bridge, merge_check, preflight
from nightshift.manifest import ManifestError


def _repo(tmp_path: Path, **modules: str) -> Path:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text("", encoding="utf-8", newline="")
    for name, source in modules.items():
        (tmp_path / ".ai" / f"{name}.py").write_text(source, encoding="utf-8", newline="")
    return tmp_path


def test_a_project_module_is_imported_from_the_projects_ai_dir(tmp_path):
    root = _repo(tmp_path, branches_stub="INTEGRATION = 'development_team'\n")
    module = bridge.project_module(root, "branches_stub", "a test")
    assert module.INTEGRATION == "development_team"


def test_a_missing_project_module_names_the_caller_and_the_step(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(ManifestError) as exc:
        bridge.project_module(root, "runner", "merge_check")
    message = str(exc.value)
    assert "merge_check" in message          # who wanted it
    assert ".ai/runner.py" in message        # what is missing
    assert "step 4" in message               # and that this is expected, not broken


def test_importing_merge_check_costs_nothing_in_an_unextracted_repo():
    """The reason the four lookups are functions rather than module-level imports:
    `import nightshift.merge_check` must not fail in a repo that has no `.ai/runner.py`,
    or the package cannot even be introspected there."""
    assert callable(merge_check.main)
    assert callable(preflight.main)
