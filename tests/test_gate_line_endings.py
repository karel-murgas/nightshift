"""Tests for the `line_endings` core gate.

Synthetic git repos for the mechanism, per every other gate test's reasoning —
plus a baseline test that nightshift's own tree is clean, which is the point
of the gate and is meant to fail the day CRLF creeps back in here.

Real `git init` repos rather than mocks: the whole defect being guarded against
lives in the disagreement between what git has *stored* and what is *on disk*,
and a mock of `git cat-file` would be a mock of exactly the thing under test.
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, encoding="utf-8", errors="replace",
    )


def _repo(tmp_path: Path, files: dict[str, bytes], *, attributes: str | None = line_endings._RULE_LINE) -> Path:
    """A real git repo with `files` committed byte-for-byte as given.

    `core.autocrlf=false` locally so the fixture's *stored* bytes are the ones
    written here — otherwise the harness's own conversion would decide what the
    test is testing.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    if attributes is not None:
        (repo / ".gitattributes").write_bytes(attributes.encode("utf-8") + b"\n")
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def test_an_lf_repo_passes(tmp_path):
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2\n"})
    assert line_endings.check(repo) == []


def test_a_missing_attributes_file_fails(tmp_path):
    repo = _repo(tmp_path, {"a.py": b"x = 1\n"}, attributes=None)
    violations = line_endings.check(repo)
    assert len(violations) == 1
    assert violations[0].file == ".gitattributes"
    assert "missing" in violations[0].rule


def test_an_attributes_file_without_the_rule_fails(tmp_path):
    """Present but declaring something else — `text=auto` alone is the specific
    half-measure that leaves the worktree side to each machine's `core.eol`,
    which is the configuration the bug came from."""
    repo = _repo(tmp_path, {"a.py": b"x = 1\n"}, attributes="* text=auto")
    violations = line_endings.check(repo)
    assert any(v.file == ".gitattributes" and "does not declare" in v.rule for v in violations)


def test_crlf_in_the_committed_blob_fails(tmp_path):
    """Committed *before* the rule existed, which is the only way a CRLF blob can
    get in: with the attributes in place `git add` normalises on the way through,
    so this fixture stages first and declares the rule afterwards. That is also
    the real migration case — a repo that adopts the rule with history already
    holding CRLF needs `git add --renormalize`, and the gate has to say so.
    """
    repo = _repo(tmp_path, {"a.py": b"x = 1\r\ny = 2\r\n"}, attributes=None)
    (repo / ".gitattributes").write_bytes(line_endings._RULE_LINE.encode("utf-8") + b"\n")
    violations = line_endings.check(repo)
    assert any(v.file == "a.py" and "committed blob" in v.rule for v in violations)


def test_crlf_in_the_working_tree_fails_even_when_the_blob_is_clean(tmp_path):
    """The phantom-dirty precursor, and the case the gate exists to catch early:
    git holds LF, disk holds CRLF, `git diff` reports nothing textual, and the
    file is stuck as modified. Nothing about the *commit* is wrong here — which
    is why checking blobs alone would miss it."""
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2\n"})
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    violations = line_endings.check(repo)
    assert [v.file for v in violations] == ["a.py"]
    assert "working tree" in violations[0].rule


def test_a_binary_file_full_of_crlf_is_not_flagged(tmp_path):
    """Binary is decided by a NUL in the first 8 kB, not by extension, so a new
    asset type cannot become a false positive just by being unlisted."""
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + b"\r\n" * 100
    repo = _repo(tmp_path, {"art.png": blob, "novel.ext": blob})
    assert line_endings.check(repo) == []


def test_no_git_still_reports_the_attributes_finding(tmp_path):
    """A non-repo directory: the blob comparison is impossible, but "the rule is
    not declared" is still knowable and still worth saying. The gate must not
    silently pass a tree it could not check."""
    plain = tmp_path / "plain"
    plain.mkdir()
    violations = line_endings.check(plain)
    assert len(violations) == 1
    assert violations[0].file == ".gitattributes"


def test_this_repo_is_clean():
    """The baseline for nightshift's own tree — the gate ships travel-ready."""
    repo_root = Path(__file__).resolve().parent.parent
    assert line_endings.check(repo_root) == []
