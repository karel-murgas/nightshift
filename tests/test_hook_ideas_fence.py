"""Tests for the `ideas_fence` PreToolUse hook.

Closes `triage-read-private-ideas` (2026-07-27): the `triage` agent glob/grep'd the
board for context and opened `Board/ideas/…`, the lane its charter says no judgment
actor may ever read. The prohibition existed only as prose the agent had to
remember, while the agent does its own file discovery — so a wide enough search was
always going to cross it.

Travelled from Dungeoneer's `tests/test_hook_ideas_fence.py` at 07_portability.md
§8 step 4. Two tests stayed there, and they are the important ones to keep: "the
fence actually arms in this repo" and the verbatim incident call. Every test here
could pass against a fence that never fires in production — the
`check-stopped-checking` shape — so a consuming project keeps that baseline over
its own tree, and the mechanism lives here.
"""
from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from nightshift.hooks import ideas_fence


def _project(tmp_path: Path, *,
             dirs: tuple[str, ...] = ("inbox", "tasks", "done", "ideas")) -> Path:
    """A board with the given lane directories.

    No synthetic `board.py`: since step 4 `known_lanes` imports
    `nightshift.board.LANES` rather than parsing a project file, so these
    fixtures name only *declared* lanes plus whatever they want to be private.
    That is a better test than the parser version was — it exercises the real
    lane set instead of a copy that could drift from it.
    """
    (tmp_path / ".ai").mkdir(parents=True)
    for name in dirs:
        (tmp_path / "Board" / name).mkdir(parents=True)
    return tmp_path


def _read(path: str) -> dict:
    return {"tool_name": "Read", "tool_input": {"file_path": path}}


# --- deriving what is private ---------------------------------------------------


def test_private_lane_is_derived_from_board_lanes(tmp_path):
    """Not a hardcoded "ideas". The lane is private because it is absent from
    `nightshift.board.LANES`, which is the board's single home for that fact."""
    root = _project(tmp_path)
    assert "ideas" not in ideas_fence.known_lanes(root)
    assert {"inbox", "tasks", "done"} <= set(ideas_fence.known_lanes(root))
    assert ideas_fence.private_lanes(root) == ("ideas",)


def test_renaming_the_private_lane_moves_the_fence_with_it(tmp_path):
    """The point of deriving rather than listing: nothing here says "ideas"."""
    root = _project(tmp_path, dirs=("inbox", "tasks", "done", "notebook"))
    assert ideas_fence.private_lanes(root) == ("notebook",)
    assert ideas_fence.evaluate(_read("Board/notebook/x.md"), "Board",
                                ideas_fence.private_lanes(root)) is not None


def test_a_new_unlisted_board_directory_is_private_until_declared(tmp_path):
    """Stated as a feature in the docstring: defaulting an unknown lane to readable is
    the wrong direction for a guard whose subject is a lane nobody protected."""
    root = _project(tmp_path, dirs=("inbox", "tasks", "done", "ideas", "archive"))
    assert ideas_fence.private_lanes(root) == ("archive", "ideas")
    reason = ideas_fence.evaluate(_read("Board/archive/x.md"), "Board",
                                  ideas_fence.private_lanes(root))
    assert reason is not None
    assert "nightshift.board.LANES" in reason, "the denial must name how to un-private a lane"


# --- fail-open paths ------------------------------------------------------------


def test_no_board_directory_means_the_fence_does_not_arm(tmp_path):
    (tmp_path / ".ai").mkdir()
    assert ideas_fence.private_lanes(tmp_path) == ()


def test_an_unreadable_lane_set_means_the_fence_does_not_arm(tmp_path, monkeypatch):
    """Denying every board directory when the lane set cannot be read would brick
    the board, and a guard that bricks the board when confused gets removed. The
    parser this replaced failed open on a `SyntaxError`; the import fails open on
    anything at all, which is a wider net for the same reason."""
    monkeypatch.setattr(ideas_fence, "known_lanes", lambda root: ())
    root = _project(tmp_path)
    assert ideas_fence.private_lanes(root) == ()


def test_no_lanes_means_every_call_is_allowed():
    assert ideas_fence.evaluate(_read("Board/ideas/x.md"), "Board", ()) is None


# --- what gets denied -----------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"tool_name": "Read", "tool_input": {"file_path": "Board/ideas/x.md"}},
    {"tool_name": "Read", "tool_input": {"file_path": "/c/repo/Board/ideas/x.md"}},
    {"tool_name": "Read", "tool_input": {"file_path": r"C:\repo\Board\ideas\x.md"}},
    {"tool_name": "Read", "tool_input": {"file_path": "board/IDEAS/x.md"}},
    {"tool_name": "Glob", "tool_input": {"pattern": "Board/ideas/**/*.md"}},
    {"tool_name": "Grep", "tool_input": {"pattern": "audio", "path": "Board/ideas"}},
    {"tool_name": "Bash", "tool_input": {"command": "cat Board/ideas/x.md"}},
    {"tool_name": "Bash", "tool_input": {"command": r"type Board\ideas\x.md"}},
])
def test_every_way_in_is_denied(payload):
    """Absolute, relative, Windows-separated and differently-cased spellings all reach
    the same file. A fence a different casing walks through is not a fence."""
    assert ideas_fence.evaluate(payload, "Board", ("ideas",)) is not None


@pytest.mark.parametrize("payload", [
    {"tool_name": "Read", "tool_input": {"file_path": "Board/inbox/x.md"}},
    {"tool_name": "Read", "tool_input": {"file_path": "Board/README.md"}},
    {"tool_name": "Glob", "tool_input": {"pattern": "Board/tasks/*.md"}},
    {"tool_name": "Bash", "tool_input": {"command": "git status"}},
    {"tool_name": "Write", "tool_input": {"file_path": "Board/ideas/x.md"}},
])
def test_the_working_lanes_are_untouched(payload):
    """Triage legitimately reads the board. A guard that blocked that would be removed
    within a day — the `Write` case is out of scope on purpose: this hook is about
    *reading* a private lane, and `worktree_fence` owns where a worker may write."""
    assert ideas_fence.evaluate(payload, "Board", ("ideas",)) is None


def test_a_lane_named_as_a_substring_of_another_word_is_not_matched():
    """`Board/ideas-archive/` is a different directory from `Board/ideas/`."""
    payload = _read("Board/inbox/my-ideas-note.md")
    assert ideas_fence.evaluate(payload, "Board", ("ideas",)) is None


def test_the_denial_names_the_one_permitted_reader(tmp_path):
    """A refusal that does not say who *is* allowed to read the lane reads as a bug,
    and the next person's fix is to remove the fence."""
    root = _project(tmp_path)
    reason = ideas_fence.evaluate(_read("Board/ideas/note.md"), "Board",
                                  ideas_fence.private_lanes(root))
    assert reason is not None
    assert "reconcile.py" in reason


def test_reconcile_is_still_allowed_to_read_the_lane():
    """The fence intercepts *tool calls*. `reconcile.py` reaches the lane as Python
    code, so the one permitted reader is unaffected by construction — asserted rather
    than assumed, since a fence that broke reconcile would break the board."""
    from nightshift import reconcile

    source = pathlib.Path(reconcile.__file__).read_text(encoding="utf-8")
    assert "ideas" in source, "reconcile no longer names the lane it is permitted to read"
