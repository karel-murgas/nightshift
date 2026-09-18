"""Tests for `preflight._fix_hygiene`: mechanical hygiene (CRLF, a missing
trailing newline) is fixed before anything downstream inspects the tree —
`doctor.checks` included, which is why this runs ahead of doctor, not just
ahead of the gate suite (hygiene-rules-belong-in-a-script card, finding 4:
`doctor.py`'s own charter is "nothing here fixes anything", and that stays
true only if the tree is already clean by the time doctor looks at it).
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path

from nightshift import doctor, preflight
from nightshift.gates import line_endings

import _fixtures


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, encoding="utf-8", errors="replace",
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _fixtures.git_init(repo, autocrlf="false")
    (repo / ".gitattributes").write_bytes(
        line_endings.REQUIRED_ATTRIBUTE.encode("utf-8") + b"\n")
    (repo / "a.py").write_bytes(b"x = 1\ny = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def test_fix_hygiene_rewrites_a_worktree_crlf_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    preflight._fix_hygiene(repo)
    assert (repo / "a.py").read_bytes() == b"x = 1\ny = 2\n"


def test_fix_hygiene_runs_ahead_of_doctors_lf_worktree_check(tmp_path):
    """The point of running the sweep before `doctor.checks`, not just before
    the gate suite: a CRLF worktree file left behind by this session must not
    fail `lf_worktree` — it is repaired first, so doctor reports a clean tree
    rather than the per-machine precondition failure it exists for."""
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    preflight._fix_hygiene(repo)
    check = doctor.lf_worktree(repo)
    assert check.ok, check.detail


def test_fix_hygiene_appends_a_missing_trailing_newline(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.py").write_bytes(b"x = 1\ny = 2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop the newline")
    preflight._fix_hygiene(repo)
    assert (repo / "a.py").read_bytes() == b"x = 1\ny = 2\n"


def test_fix_hygiene_is_generic_over_whatever_gate_defines_fix(tmp_path, monkeypatch):
    """No hand-written `line_endings, trailing_newline` list here — a gate is
    fixed because it defines `fix()`, discovered the same way `gates.run
    --fix` finds one. A gate with no `fix()` is left alone."""
    called: list[str] = []

    def fixable_fix(root):
        called.append("fixable")

    modules = {
        "fixable": types.SimpleNamespace(check=lambda r: [], fix=fixable_fix),
        "report_only": types.SimpleNamespace(check=lambda r: []),
    }
    monkeypatch.setattr("nightshift.gates.run.discover", lambda root: modules)
    preflight._fix_hygiene(tmp_path)
    assert called == ["fixable"]
