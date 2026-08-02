"""Tests for `nightshift/normalize_worktree.py`.

Synthetic git repos for the mechanism, modelled on the `_repo` helper in
`nightshift/tests/test_gate_line_endings.py`.  Real `git init` repos rather
than mocks: the entire point of the normaliser is to interact with git's
actual checkout and smudge behaviour, and a mock would test the mock.

Six concerns, each as its own test:

1. A CRLF working tree is fully normalised to LF (the main success path).
2. A file with real content changes is skipped and preserved intact.
3. An untracked CRLF file is untouched (defence-in-depth).
4. An already-clean tree exits 0 and says something explicitly (not silent).
5. The gate's working-tree message names a remedy that exists everywhere.
6. Targets come from the gate, not from a second scan (binary file proof).
"""
from __future__ import annotations

import importlib
import re
import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import line_endings  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent

from nightshift import normalize_worktree  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, encoding="utf-8", errors="replace",
    )


def _repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """A real git repo with files committed byte-for-byte as given.

    Uses `core.autocrlf=false` locally so the *stored* bytes match what we
    write here -- otherwise the test harness's own conversion would decide
    what is under test.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    # Matching the real tree: LF-pinned attributes.
    (repo / ".gitattributes").write_bytes(
        (line_endings._RULE_LINE + "\n").encode("utf-8")
    )
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


# ---------------------------------------------------------------------------
# 1. CRLF working tree goes clean
# ---------------------------------------------------------------------------

def test_normalize_crlf_repo_goes_clean(tmp_path):
    """The main success path: a tree with several CRLF files is fully
    normalised and the gate reports 0 violations afterwards."""
    repo = _repo(tmp_path, {
        "a.py": b"x = 1\ny = 2\n",
        "b.txt": b"hello\nworld\n",
    })
    # Simulate the pre-attributes checkout: rewrite to CRLF on disk.
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
    (repo / "b.txt").write_bytes(b"hello\r\nworld\r\n")

    exit_code = normalize_worktree.normalize(repo)

    assert exit_code == 0
    wt_violations = [
        v for v in line_endings.check(repo) if "working tree" in v.rule
    ]
    assert wt_violations == [], (
        f"expected 0 working-tree violations after normalise, "
        f"got {len(wt_violations)}: {wt_violations}"
    )


# ---------------------------------------------------------------------------
# 2. File with real content changes is skipped and preserved
# ---------------------------------------------------------------------------

def test_normalize_skips_file_with_real_content_changes(tmp_path, capsys):
    """A CRLF file that also has a genuine content change (not just CRLF) is
    skipped, its content is left intact, and the run reports the skip."""
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2\n"})
    # CRLF *plus* a genuine edit that is not in the index.
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 99\r\n")  # 99 != 2

    exit_code = normalize_worktree.normalize(repo)

    # The run exits non-zero because a violation remains (the file was skipped).
    assert exit_code != 0
    # The file's content is exactly as we wrote it -- not modified, not deleted.
    assert (repo / "a.py").read_bytes() == b"x = 1\r\ny = 99\r\n"
    # The skip is mentioned in the output.
    captured = capsys.readouterr()
    assert "a.py" in captured.out


# ---------------------------------------------------------------------------
# 2b. A file to rewrite AND a file to preserve, in the same run
# ---------------------------------------------------------------------------

def test_a_safe_file_and_an_edited_file_together_preserve_the_edit(tmp_path, capsys):
    """The regression that sent this card to `needs-decision/` (2026-07-31).

    Test 2 above uses a single skipped file, so `safe` comes out empty and the
    re-materialise step never runs at all — which is exactly why it could not
    see this. With one file of each kind, the original
    `git checkout-index -f -a` rewrote *every tracked file* from the index and
    silently reverted the genuine edit, while the run printed "preserved".
    """
    repo = _repo(tmp_path, {
        "safe.py": b"x = 1\ny = 2\n",
        "mod.py": b"x = 1\ny = 2\n",
    })
    (repo / "safe.py").write_bytes(b"x = 1\r\ny = 2\r\n")      # CRLF only -> rewrite
    (repo / "mod.py").write_bytes(b"x = 1\r\ny = 999\r\n")     # CRLF + real edit -> preserve

    exit_code = normalize_worktree.normalize(repo)

    # The edit survives, byte for byte. This is the assertion that failed before.
    assert (repo / "mod.py").read_bytes() == b"x = 1\r\ny = 999\r\n", (
        "the genuine uncommitted edit was reverted — checkout-index must be "
        "scoped to the deleted paths, never -a"
    )
    # ...and the file the script *was* allowed to touch really was normalised.
    assert (repo / "safe.py").read_bytes() == b"x = 1\ny = 2\n"
    # The preserved file must not have been staged either: `git add -u` without
    # a pathspec would sweep someone's work in progress into the index.
    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.split()
    assert "mod.py" not in staged, f"skipped file was staged: {staged}"
    # Still non-zero, because a working-tree violation remains (the skipped one).
    assert exit_code != 0
    assert "mod.py" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3. Untracked CRLF file is untouched
# ---------------------------------------------------------------------------

def test_normalize_leaves_untracked_crlf_file_alone(tmp_path):
    """An untracked file with CRLF must not be deleted.  `line_endings.check`
    only returns tracked files, so the normaliser should never even see it --
    but verify the contract holds at the integration level."""
    repo = _repo(tmp_path, {"tracked.py": b"x = 1\ny = 2\n"})
    untracked = repo / "scratch.py"
    untracked.write_bytes(b"z = 3\r\n")

    exit_code = normalize_worktree.normalize(repo)

    # The tracked file is clean, so exit 0.
    assert exit_code == 0
    # The untracked file still exists and still has CRLF content.
    assert untracked.is_file()
    assert untracked.read_bytes() == b"z = 3\r\n"


# ---------------------------------------------------------------------------
# 4. Already-clean tree exits 0 and says so explicitly
# ---------------------------------------------------------------------------

def test_already_clean_tree_exits_0_and_announces_it(tmp_path, capsys):
    """An already-clean working tree must exit 0 *and* print something -- not
    be silent in a way that looks identical to a run that changed nothing."""
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2\n"})
    # No CRLF written -- tree is clean from the start.

    exit_code = normalize_worktree.normalize(repo)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.strip(), "expected at least one line of output for a clean run"


# ---------------------------------------------------------------------------
# 5. The gate's working-tree violation message names a script that exists
# ---------------------------------------------------------------------------

def test_working_tree_violation_message_names_a_remedy_that_exists(tmp_path):
    """The remedy the message names must be runnable *from a consuming repo*.

    It used to name `.ai/normalize_worktree.py`, and this test resolved that
    against the origin project's own tree -- where it existed. Everywhere else it
    did not: a core gate was telling every installed repo to run a file only one
    repo had, once per CRLF file. Caught on 2026-08-02 by initialising an empty
    repo and reading what the gate actually said (07_portability.md §8 step 5).

    So the remedy is a module, and the assertion is that it imports and exposes
    the entry point the message implies -- which is true wherever the package is.
    """
    repo = _repo(tmp_path, {"f.txt": b"hello\n"})
    # Introduce a working-tree CRLF violation.
    (repo / "f.txt").write_bytes(b"hello\r\n")

    violations = line_endings.check(repo)
    wt = [v for v in violations if "working tree" in v.rule]
    assert wt, "expected at least one working-tree violation to inspect"

    match = re.search(r"python -m ([\w.]+)", wt[0].rule)
    assert match, f"no runnable remedy in the working-tree message: {wt[0].rule!r}"
    module = importlib.import_module(match.group(1))
    assert callable(getattr(module, "main", None)), (
        f"{match.group(1)} is named as a remedy but is not runnable as one")

    assert ".ai/" not in wt[0].rule, (
        "a core gate must not point at a path inside a consuming project's .ai/ — "
        "that directory holds what the project owns, and the framework cannot know "
        "what is in it")


# ---------------------------------------------------------------------------
# 6. Targets come from the gate, not from a second CRLF scan
# ---------------------------------------------------------------------------

def test_binary_crlf_file_is_not_normalised(tmp_path):
    """A binary file containing CRLF sequences must not be touched.

    `line_endings.check` detects binary by a NUL byte in the first 8 kB and
    skips it.  The normaliser must produce the same result because it derives
    its target list from check() -- not from its own byte scan.  If it ever
    did its own scan, it would target this binary and the test would fail.
    """
    binary_crlf = b"\x89PNG\r\n\x1a\n\x00" * 100 + b"\r\n" * 50
    text_crlf = b"x = 1\r\ny = 2\r\n"
    repo = _repo(tmp_path, {
        "art.png": binary_crlf,
        "code.py": text_crlf,
    })
    # Ensure the binary is on disk with its original bytes too.
    (repo / "art.png").write_bytes(binary_crlf)
    (repo / "code.py").write_bytes(text_crlf)

    exit_code = normalize_worktree.normalize(repo)

    assert exit_code == 0
    # The text file is now LF.
    assert b"\r\n" not in (repo / "code.py").read_bytes()
    # The binary file is completely untouched.
    assert (repo / "art.png").read_bytes() == binary_crlf


# ---------------------------------------------------------------------------
# 7. --dry-run lists files without changing them
# ---------------------------------------------------------------------------

def test_dry_run_lists_targets_without_changing_them(tmp_path, capsys):
    """--dry-run must enumerate what would be rewritten and change nothing."""
    repo = _repo(tmp_path, {"a.py": b"x = 1\ny = 2\n"})
    (repo / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")

    exit_code = normalize_worktree.normalize(repo, dry_run=True)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "a.py" in out
    # The file is still CRLF -- nothing was changed.
    assert (repo / "a.py").read_bytes() == b"x = 1\r\ny = 2\r\n"
