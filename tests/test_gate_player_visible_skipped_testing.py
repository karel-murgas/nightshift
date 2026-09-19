"""Tests for the `player_visible_skipped_testing` core gate.

Synthetic repo per test (the module docstring's own reasoning for why the "was
it played" half is not git archaeology): a real `Board/done/` corpus test
lives in `.ai/corrections.log`'s own record, not here — a test pinned to
Project Tigress's real board would fail on every legitimate card edit.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import player_visible_skipped_testing as gate  # noqa: E402

import _fixtures

_MANIFEST = """
[board]
player_visible_paths = ["game/rendering", "game/i18n.py"]
"""

_MANIFEST_NO_PATHS = "[project]\nname = \"x\"\n"


def _run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _card(verify: str | None, how_to_test: bool) -> str:
    verify_line = f"verify: {verify}\n" if verify else ""
    body = "## Summary\n\nDid the thing.\n"
    if how_to_test:
        body += "\n## How to test\n\nOpen the game and look.\n"
    return f"---\nid: probe\ntitle: probe\nstate: done\n{verify_line}---\n\n{body}"


def _repo(tmp_path: Path, *, manifest: str = _MANIFEST, verify: str | None = "review",
         how_to_test: bool = False, touched: str | None = "game/rendering/x.py",
         merge_message: str = "Merge ai/probe: added the thing") -> Path:
    """A one-card repo: `game/` holds the "application" tree, `Board/done/probe.md`
    is the card. The landing commit (if `merge_message` is given) touches
    `touched`, or nothing at all if `touched` is None."""
    _fixtures.git_init(tmp_path, email="t@t")
    _write(tmp_path / ".ai" / "manifest.toml", manifest)
    _write(tmp_path / "game" / "logic.py", "x = 1\n")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-qm", "seed")

    if merge_message is not None:
        if touched is not None:
            _write(tmp_path / touched, "changed = True\n")
        else:
            _write(tmp_path / "game" / "logic.py", "x = 2\n")
        _run(tmp_path, "add", "-A")
        _run(tmp_path, "commit", "-qm", merge_message)

    _write(tmp_path / "Board" / "done" / "probe.md", _card(verify, how_to_test))
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-qm", "board: probe tasks -> done")
    return tmp_path


# --- what the gate must catch -------------------------------------------------


def test_a_touched_path_with_no_evidence_of_play_is_flagged(tmp_path):
    root = _repo(tmp_path)
    violations = gate.check(root)
    assert len(violations) == 1
    assert violations[0].file == "Board/done/probe.md"
    assert "game/rendering/x.py" in violations[0].rule


def test_a_bare_path_prefix_without_a_trailing_slash_still_matches(tmp_path):
    """`game/i18n.py` is declared as a file, not a directory — the check must not
    require a `/` after it to count a hit."""
    root = _repo(tmp_path, touched="game/i18n.py")
    assert len(gate.check(root)) == 1


def test_the_case_insensitive_review_stage_spelling_is_found(tmp_path):
    root = _repo(tmp_path, merge_message="merge ai/probe: reviewed ok by the runner")
    assert len(gate.check(root)) == 1


def test_the_runner_into_base_spelling_is_found(tmp_path):
    root = _repo(tmp_path, merge_message="Merge ai/probe into main")
    assert len(gate.check(root)) == 1


# --- what it must not ---------------------------------------------------------


def test_verify_play_is_not_flagged_even_with_a_touched_path(tmp_path):
    root = _repo(tmp_path, verify="play")
    assert gate.check(root) == []


def test_a_how_to_test_section_is_not_flagged(tmp_path):
    root = _repo(tmp_path, verify="review", how_to_test=True)
    assert gate.check(root) == []


def test_a_diff_outside_the_declared_paths_is_not_flagged(tmp_path):
    root = _repo(tmp_path, touched="game/logic_other.py")
    assert gate.check(root) == []


def test_no_landing_commit_is_no_evidence_not_a_violation(tmp_path):
    """The card claims nothing was played, and the diff cannot be found at all —
    the same "nothing to review" silence as an artefact-only card. Reproduces
    the shape `alarm-ice` predates: no landing commit under the current naming
    convention is a real, common state for old cards, and must not flood the
    corpus with unresolvable violations."""
    root = _repo(tmp_path, merge_message=None)
    assert gate.check(root) == []


def test_no_declared_paths_is_a_project_that_has_no_opinion_yet(tmp_path):
    root = _repo(tmp_path, manifest=_MANIFEST_NO_PATHS)
    assert gate.check(root) == []


def test_no_manifest_at_all_is_a_noop(tmp_path):
    _fixtures.git_init(tmp_path, email="t@t")
    _write(tmp_path / "game" / "logic.py", "x = 1\n")
    _write(tmp_path / "Board" / "done" / "probe.md", _card("review", False))
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-qm", "no manifest at all")
    assert gate.check(tmp_path) == []


# --- the gate's reach ----------------------------------------------------------


def test_a_card_with_no_verify_field_at_all_is_a_candidate(tmp_path):
    """Pre-dates the `verify:` mechanism entirely, or triage never set it — the
    absent case must be treated the same as `review`, not skipped as "unset,
    so unknown, so ignore"."""
    root = _repo(tmp_path, verify=None)
    assert len(gate.check(root)) == 1
