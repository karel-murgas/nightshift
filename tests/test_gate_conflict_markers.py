"""Tests for the `conflict_markers` core gate and the predicate it shares.

The gate exists because a hand-resolved rebase left a trailing `>>>>>>> 0c5905f (...)`
behind on 2026-08-09 and it shipped; the predicate is shared with
`runner._resolve_conflict`, which checks an *agent's* resolution before letting the
rebase continue. So the two things worth testing are that a real marker is caught in
either position, and that the near-misses which would make the gate un-runnable —
a setext heading underline, an indented example — are not.

Real `git init` repos for the gate itself: its scope is `git ls-files`, so a fixture
that only wrote files to disk would test nothing about which files it looks at.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

from nightshift import conflictmarkers

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import conflict_markers  # noqa: E402

import _fixtures  # noqa: E402

CONFLICTED = """\
intro line
<<<<<<< HEAD
ours
=======
theirs
>>>>>>> abc1234 (a sibling card)
tail line
"""

# The 2026-08-09 shape exactly: the region was edited correctly, the closing marker
# was not removed. This is the one that shipped, so it is the one that must fire.
ORPHAN_CLOSE = """\
- **Ranged enemies now de-aggro on lost sight** (2026-08-22)
- **Shared distance metric** (2026-08-22)
>>>>>>> 73cf3a0 (Add shared grid_distance metric)
"""

# Two shapes that must NOT fire, or the gate cannot run on this repo's own docs.
SETEXT = """\
A heading
=========

Another heading
=======

body text
"""
INDENTED_EXAMPLE = """\
Resolve it by deleting the markers:

    <<<<<<< HEAD
    ours
    =======
    theirs
    >>>>>>> abc1234 (msg)
"""


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = _fixtures.git_init(tmp_path / "repo")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")
    return repo


def test_a_full_conflict_region_is_reported_on_all_three_marker_lines():
    hits = conflictmarkers.markers_in(CONFLICTED)
    assert [line for line, _ in hits] == [2, 4, 6]


def test_an_orphaned_closing_marker_fires_on_its_own():
    """The 2026-08-09 miss. The region above it reads correctly and git was happy;
    only the trailing marker survived, and nothing looked for it."""
    hits = conflictmarkers.markers_in(ORPHAN_CLOSE)
    assert len(hits) == 1
    assert hits[0][0] == 3


def test_a_setext_heading_underline_is_not_a_conflict_marker():
    """`=======` under a heading is markdown, and this repo's docs are full of it.
    A gate that fired on those would be turned off within a day."""
    assert conflictmarkers.markers_in(SETEXT) == []


def test_an_indented_marker_is_documentation_not_a_conflict():
    """A doc explaining the syntax indents its example — including this gate's own
    charter and `conflictmarkers`' docstring."""
    assert conflictmarkers.markers_in(INDENTED_EXAMPLE) == []


def test_gate_reports_a_tracked_file_carrying_a_marker(tmp_path: Path):
    repo = _repo(tmp_path, {"Board/tasks/card.md": CONFLICTED})
    found = conflict_markers.check(repo)
    assert [v.file for v in found] == ["Board/tasks/card.md"] * 3
    assert "conflict marker left behind" in found[0].rule


def test_gate_is_clean_on_a_repo_with_only_lookalikes(tmp_path: Path):
    repo = _repo(tmp_path, {"docs/a.md": SETEXT, "docs/b.md": INDENTED_EXAMPLE})
    assert conflict_markers.check(repo) == []


def test_gate_ignores_an_untracked_file(tmp_path: Path):
    """Scope is `git ls-files`. A scratch file in the worktree is not the repo's
    problem, and reporting one would make the gate red on a machine mid-resolution."""
    repo = _repo(tmp_path, {"docs/a.md": SETEXT})
    (repo / "scratch.md").write_bytes(CONFLICTED.encode("utf-8"))
    assert conflict_markers.check(repo) == []


def test_gate_skips_a_binary_blob(tmp_path: Path):
    """A file that does not decode as UTF-8 cannot carry a marker; the gate must
    step over it rather than raising and taking the whole suite down."""
    repo = _fixtures.git_init(tmp_path / "repo")
    (repo / "sprite.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")
    assert conflict_markers.check(repo) == []


def test_nightshift_own_tree_is_marker_free():
    """The baseline this gate exists to hold. Meant to fail the day a resolution
    ships half-done here."""
    root = Path(_nightshift_gates.__file__).resolve().parents[2]
    assert conflict_markers.check(root) == []
