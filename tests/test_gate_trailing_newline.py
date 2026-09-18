"""Tests for the `trailing_newline` core gate.

Mirrors `test_gate_line_endings.py`'s fixture shape — real git repos, because
the text/binary distinction comes from git's own `-text` verdict, read through
`line_endings.eol_report`, and mocking that would be testing the mock rather
than the gate.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import line_endings  # noqa: E402
import trailing_newline  # noqa: E402

import _fixtures  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, encoding="utf-8", errors="replace",
    )


def _repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """A real git repo, `* text=auto eol=lf` declared, `files` committed as given."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _fixtures.git_init(repo, autocrlf="false")
    (repo / ".gitattributes").write_bytes(
        line_endings.REQUIRED_ATTRIBUTE.encode("utf-8") + b"\n")
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def test_a_file_ending_in_a_newline_passes(tmp_path):
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2\n"})
    assert trailing_newline.check(repo) == []


def test_a_file_missing_the_trailing_newline_fails(tmp_path):
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2"})
    violations = trailing_newline.check(repo)
    assert [v.file for v in violations] == ["a.py"]
    assert "missing trailing newline" in violations[0].rule


def test_an_empty_file_is_not_flagged(tmp_path):
    """Nothing to leave unterminated, and appending a lone `\\n` to nothing
    would manufacture content a fix has no business inventing."""
    repo = _repo(tmp_path, {"empty.py": b""})
    assert trailing_newline.check(repo) == []


def test_a_binary_file_missing_a_trailing_newline_is_not_flagged(tmp_path):
    """Binary is git's own verdict (the `-text` marker via `eol_report`),
    shared with `line_endings` rather than re-derived."""
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    repo = _repo(tmp_path, {"art.png": blob})
    assert trailing_newline.check(repo) == []


def test_no_git_reports_nothing(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert trailing_newline.check(plain) == []


def test_this_repo_is_clean():
    """The baseline for nightshift's own tree."""
    repo_root = Path(__file__).resolve().parent.parent
    assert trailing_newline.check(repo_root) == []


# --- fix() -----------------------------------------------------------------


def test_fix_appends_exactly_one_newline(tmp_path):
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2"})
    fixed = trailing_newline.fix(repo)
    assert fixed == ["a.py"]
    assert (repo / "a.py").read_bytes() == b"x = 1\ny = 2\n"
    assert trailing_newline.check(repo) == []


def test_fix_only_appends_never_rewrites_existing_bytes(tmp_path):
    """An append can never clobber a real, in-progress edit — unlike
    `line_endings.fix()`, this needs no "is this lossless here" reasoning."""
    repo = _repo(tmp_path, {"a.py": b"x = 1\r\ny = 2"})  # CRLF AND no trailing newline
    trailing_newline.fix(repo)
    assert (repo / "a.py").read_bytes() == b"x = 1\r\ny = 2\n"


def test_fix_on_an_already_clean_repo_is_a_no_op(tmp_path):
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2\n"})
    assert trailing_newline.fix(repo) == []
