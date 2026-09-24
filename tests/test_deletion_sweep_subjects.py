"""`deletion_sweep.stale_subjects` — a project gate's own `SUBJECT` naming a path
that no longer exists (`correction-loop-defaults`, 2026-09-23 framework review,
part 2). No git needed: the function only reads `.ai/gates/` and the filesystem.
"""
from __future__ import annotations

from pathlib import Path

from nightshift.gates import deletion_sweep


def _write_gate(repo_root: Path, name: str, subject_repr: str) -> None:
    gates_dir = repo_root / ".ai" / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / f"{name}.py").write_text(
        f'"""A test gate."""\n'
        f"from __future__ import annotations\n\n"
        f"SUBJECT = {subject_repr}\n\n"
        f"def check(repo_root):\n"
        f"    return []\n",
        encoding="utf-8",
    )


def test_no_project_gates_dir_is_silent(tmp_path):
    assert deletion_sweep.stale_subjects(tmp_path) == []


def test_subject_that_exists_is_clean(tmp_path):
    # A distinct module name per test: `discover()` imports gates by module
    # name, and Python caches an import by name across tests in one process --
    # reusing a name would silently hand a later test the earlier one's module.
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "settings.py").write_text("X = 1\n", encoding="utf-8")
    _write_gate(tmp_path, "subject_gate_exists", '"core/settings.py"')

    assert deletion_sweep.stale_subjects(tmp_path) == []


def test_subject_that_is_gone_is_flagged(tmp_path):
    _write_gate(tmp_path, "subject_gate_missing", '"core/settings.py"')

    violations = deletion_sweep.stale_subjects(tmp_path)

    assert len(violations) == 1
    v = violations[0]
    assert v.file == ".ai/gates/subject_gate_missing.py"
    assert "core/settings.py" in v.rule
    assert "no longer exists" in v.rule


def test_tuple_subject_reports_each_missing_entry(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "settings.py").write_text("X = 1\n", encoding="utf-8")
    _write_gate(tmp_path, "subject_gate_tuple", '("core/settings.py", "core/gone.py")')

    violations = deletion_sweep.stale_subjects(tmp_path)

    assert len(violations) == 1
    assert "core/gone.py" in violations[0].rule


def test_core_gates_are_never_checked_this_way(tmp_path):
    # A project with no .ai/gates/ at all -- core gates like card_schema declare
    # SUBJECT = "Board", which this repo (a bare tmp_path) does not have, and
    # that must not be reported: core subjects are cross-project conventions,
    # not this project's own drift.
    (tmp_path / ".ai").mkdir()
    assert deletion_sweep.stale_subjects(tmp_path) == []
