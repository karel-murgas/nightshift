"""Tests for `nightshift/reconcile.py` — the folder/`state:` reconciler.

The one rule worth testing hard is the disambiguator, because getting it
backwards is silently destructive: it would either undo every drag a maintainer
makes in the Kanban, or drag every note filed by hand back out again.

    no `state:`             -> the folder is the intent, stamp it
    `state:` set, ≠ folder  -> the state is the intent, move the file

Plus the privacy boundary: an unflagged note in `ideas/` must come back
untouched and unread.

**Provenance.** Written in Dungeoneer's `tests/` and moved here whole by
`framework-tests-live-in-the-wrong-repo` (2026-08-08). Every test builds its own
`tmp_path` board; the origin repo's only use of its own root was a `sys.path`
insert, so nothing stayed behind.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nightshift import reconcile


def _board(tmp_path: Path, lane: str, name: str, text: str) -> Path:
    lane_dir = tmp_path / "Board" / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    path = lane_dir / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _kinds(actions) -> list[str]:
    return [a.kind for a in actions]


# --- the disambiguator ------------------------------------------------------

def test_a_bare_note_gets_its_state_stamped_from_the_folder(tmp_path):
    """Karel drops a note in inbox/. He never writes frontmatter, so the folder
    is the only statement of intent there is."""
    path = _board(tmp_path, "inbox", "ice", "Implement ICE that deals damage.\n")
    actions = reconcile.plan(tmp_path)
    assert _kinds(actions) == ["stamp"]
    reconcile.apply(tmp_path, actions, commit=False)
    assert reconcile.read_state(path.read_text(encoding="utf-8")) == "inbox"


def test_stamping_preserves_the_body(tmp_path):
    path = _board(tmp_path, "inbox", "ice", "Implement ICE that deals damage.\n")
    reconcile.apply(tmp_path, reconcile.plan(tmp_path), commit=False)
    assert "Implement ICE that deals damage." in path.read_text(encoding="utf-8")


def test_stamping_a_note_that_already_has_frontmatter_keeps_the_other_fields(tmp_path):
    path = _board(tmp_path, "tasks", "probe", "---\ntier: worker\n---\n\nBody.\n")
    reconcile.apply(tmp_path, reconcile.plan(tmp_path), commit=False)
    text = path.read_text(encoding="utf-8")
    assert reconcile.read_state(text) == "tasks"
    assert "tier: worker" in text


def test_a_dragged_card_moves_the_file_to_match_its_state(tmp_path):
    """Base Board writes `state:` on drag and never moves the file. This is the
    half of reconcile that makes the Kanban actually work."""
    _board(tmp_path, "tasks", "probe", "---\nstate: review\n---\n\nBody.\n")
    actions = reconcile.plan(tmp_path)
    assert _kinds(actions) == ["move"]
    reconcile.apply(tmp_path, actions, commit=False)
    assert (tmp_path / "Board" / "review" / "probe.md").is_file()
    assert not (tmp_path / "Board" / "tasks" / "probe.md").exists()


def test_an_agreeing_card_is_left_alone(tmp_path):
    _board(tmp_path, "tasks", "probe", "---\nstate: tasks\n---\n\nBody.\n")
    assert reconcile.plan(tmp_path) == []


# --- the privacy boundary ---------------------------------------------------

def test_an_unflagged_idea_is_never_touched(tmp_path):
    """The whole privacy guarantee. A note in ideas/ with no `state:` must
    produce no action at all — not a stamp, not a move, not an error."""
    path = _board(tmp_path, "ideas", "raw", "Nějaký nedodělaný nápad.\n")
    before = path.read_text(encoding="utf-8")
    assert reconcile.plan(tmp_path) == []
    assert path.read_text(encoding="utf-8") == before


def test_a_flagged_idea_is_moved_out_to_the_lane_it_names(tmp_path):
    """Karel's 'ready for triage' gesture: set `state: inbox` on an idea and it
    leaves the private lane. This is the one reason reconcile may look in
    ideas/ at all, and it reads only that field."""
    _board(tmp_path, "ideas", "audio", "---\nstate: inbox\n---\n\nZvuky.\n")
    actions = reconcile.plan(tmp_path)
    assert _kinds(actions) == ["move"]
    reconcile.apply(tmp_path, actions, commit=False)
    assert (tmp_path / "Board" / "inbox" / "audio.md").is_file()
    assert not (tmp_path / "Board" / "ideas" / "audio.md").exists()


def test_an_idea_is_never_stamped_into_visibility(tmp_path):
    """The inverse of the rule above, and the one that would breach privacy:
    reconcile must never write `state: ideas` onto an unflagged note, which
    would publish it to the board without Karel asking."""
    _board(tmp_path, "ideas", "raw", "Poznámka.\n")
    assert "ideas" not in reconcile.LANES
    assert reconcile.plan(tmp_path) == []


# --- refusals ---------------------------------------------------------------

def test_an_unknown_state_is_reported_not_guessed(tmp_path):
    _board(tmp_path, "tasks", "probe", "---\nstate: in-progress\n---\n\nBody.\n")
    actions = reconcile.plan(tmp_path)
    assert _kinds(actions) == ["error"]
    assert "not a lane" in actions[0].detail


def test_a_card_at_the_board_root_is_reported_not_guessed(tmp_path):
    (tmp_path / "Board").mkdir(parents=True)
    (tmp_path / "Board" / "stray.md").write_text("---\nstate: tasks\n---\n\nB.\n", encoding="utf-8")
    actions = reconcile.plan(tmp_path)
    assert _kinds(actions) == ["error"]
    assert "in no lane" in actions[0].detail


def test_a_move_onto_an_existing_file_is_skipped_not_clobbered(tmp_path):
    _board(tmp_path, "tasks", "probe", "---\nstate: review\n---\n\nNew.\n")
    _board(tmp_path, "review", "probe", "---\nstate: review\n---\n\nOriginal.\n")
    reconcile.apply(tmp_path, reconcile.plan(tmp_path), commit=False)
    assert "Original." in (tmp_path / "Board" / "review" / "probe.md").read_text(encoding="utf-8")


def test_readmes_are_not_cards(tmp_path):
    (tmp_path / "Board" / "ideas").mkdir(parents=True)
    (tmp_path / "Board" / "ideas" / "README.md").write_text("private lane", encoding="utf-8")
    assert reconcile.plan(tmp_path) == []


# --- report-only default ----------------------------------------------------

def test_plan_alone_changes_nothing_on_disk(tmp_path):
    path = _board(tmp_path, "inbox", "ice", "Body.\n")
    reconcile.plan(tmp_path)
    assert path.read_text(encoding="utf-8") == "Body.\n"


@pytest.mark.parametrize("lane", list(reconcile.LANES))
def test_every_lane_round_trips(tmp_path, lane):
    path = _board(tmp_path, lane, "probe", "Body.\n")
    reconcile.apply(tmp_path, reconcile.plan(tmp_path), commit=False)
    assert reconcile.read_state(path.read_text(encoding="utf-8")) == lane
