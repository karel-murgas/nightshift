"""`nightshift.board` — the card model, and the one project fact in it.

The module travelled from Dungeoneer's `.ai/board.py` at 07_portability.md §8
step 4 with no test file of its own: its behaviour was covered end-to-end
through that project's runner tests, which stay there because the runner does.
What is written here is the half that is now *core* and had no direct test on
this side — the manifest-driven board root, and the two writer invariants the
module docstring says matter more than they look.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift import board

_CARD = """---
id: probe
title: A probe card
state: tasks
kanban_order: a0
---

## Goal

Do the thing.
"""


def _repo(tmp_path: Path, manifest: str | None = None, lane: str = "tasks",
          board_root: str = "Board") -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    if manifest is not None:
        (tmp_path / ".ai").mkdir(exist_ok=True)
        (tmp_path / ".ai" / "manifest.toml").write_text(manifest, encoding="utf-8")
    lane_dir = tmp_path / board_root / lane
    lane_dir.mkdir(parents=True)
    (lane_dir / "probe.md").write_text(_CARD, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    return tmp_path


# --- where the board lives ----------------------------------------------------


def test_no_manifest_keeps_the_literal_this_replaced(tmp_path):
    """§8 step 4's first checklist item: a hardcoded default moved behind a
    manifest field must not change behaviour for a caller that supplies no
    config. `Board/` is what `board.py` hardcoded, so `Board/` is what a repo
    with no manifest still gets."""
    root = _repo(tmp_path, manifest=None)
    assert board.board_rel(root) == Path("Board")
    assert [c.id for c in board.cards(root, "tasks")] == ["probe"]


def test_an_empty_manifest_is_the_same_answer(tmp_path):
    root = _repo(tmp_path, manifest="[project]\nname = \"x\"\n")
    assert board.board_rel(root) == Path("Board")


def test_board_root_is_an_override_when_declared(tmp_path):
    root = _repo(tmp_path, manifest='[board]\nroot = "kanban"\n', board_root="kanban")
    assert board.board_rel(root) == Path("kanban")
    assert board.board_dir(root) == root / "kanban"
    assert [c.id for c in board.cards(root, "tasks")] == ["probe"]


def test_a_move_follows_the_declared_root(tmp_path):
    """The regression a relative-vs-absolute mistake would cause: the card lands
    in the right place *and* the `git add` pathspec covers it, so the move
    reaches git rather than sitting uncommitted."""
    root = _repo(tmp_path, manifest='[board]\nroot = "kanban"\n', board_root="kanban")
    card = board.cards(root, "tasks")[0]

    moved = board.move(root, card, "review")

    assert moved.path == root / "kanban" / "review" / "probe.md"
    assert moved.fields["state"] == "review"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                            capture_output=True, text=True, check=True)
    assert status.stdout.strip() == ""


# --- the two writer invariants the docstring calls out ------------------------


def test_field_order_is_preserved_and_values_are_not_requoted():
    """Karel hand-writes frontmatter and Obsidian rewrites it; a writer that
    normalised quoting would produce a diff on every touch."""
    text = '---\nid: probe\ntitle: "A: quoted title"\nstate: tasks\n---\n\nbody\n'
    out = board.set_fields(text, {"state": "review"})
    assert out.splitlines()[:4] == [
        "---", "id: probe", 'title: "A: quoted title"', "state: review"]


def test_runner_fields_are_appended_never_interleaved():
    """`03_board.md` §2 splits the schema by owner so a runner that dies
    mid-card has rewritten only its own half."""
    out = board.set_fields(_CARD, {"attempts": "1", "branch": "ai/probe"})
    lines = out.splitlines()
    assert lines[lines.index("---", 1) - 2:lines.index("---", 1)] == [
        "attempts: 1", "branch: ai/probe"]


def test_a_replaced_section_does_not_stack(tmp_path):
    """Three attempts must leave one current error, not three stale ones."""
    once = board.append_section(_CARD, "Error", "first")
    twice = board.append_section(once, "Error", "second")
    assert twice.count("## Error") == 1
    assert "first" not in twice


def test_setting_fields_on_a_card_with_no_frontmatter_raises():
    with pytest.raises(ValueError, match="frontmatter"):
        board.set_fields("no frontmatter here\n", {"state": "tasks"})


# --- ordering -----------------------------------------------------------------


def test_dispatch_order_puts_dragged_cards_first(tmp_path):
    """`kanban_order` is the only prioritisation gesture the GUI offers; a card
    that has never been dragged must not jump ahead of one that has."""
    root = _repo(tmp_path)
    lane = root / "Board" / "tasks"
    (lane / "undragged.md").write_text(
        "---\nid: undragged\nstate: tasks\n---\n\nbody\n", encoding="utf-8")
    (lane / "dragged.md").write_text(
        "---\nid: dragged\nstate: tasks\nkanban_order: a1\n---\n\nbody\n", encoding="utf-8")

    assert [c.id for c in board.cards(root, "tasks")] == ["probe", "dragged", "undragged"]


# ------------------------------------------------------- YAML's other list form


def test_a_block_list_reads_the_same_as_an_inline_one():
    """Obsidian writes `tags:` followed by indented `- item` lines. Read as the
    empty string — which is what happened until 2026-08-16 — every rule keyed on
    the field is silently disabled while the file plainly carries it, and this
    module disagreed with `card_schema`, which has folded the form since
    `tag-parser-blind-to-block-form`."""
    block = board.parse_fields(
        "---\nid: x\ntags:\n  - nightshift\n  - art\nstate: tasks\n---\n\nbody\n")
    inline = board.parse_fields(
        "---\nid: x\ntags: [nightshift, art]\nstate: tasks\n---\n\nbody\n")
    assert block["tags"] == inline["tags"] == "[nightshift, art]"
    assert block["state"] == "tasks", "the field after the block list was lost"


def test_an_empty_value_that_is_not_a_block_list_stays_empty():
    fields = board.parse_fields("---\nid: x\nchecker:\nstate: tasks\n---\n\nbody\n")
    assert fields["checker"] == ""
    assert fields["state"] == "tasks"


def test_tags_are_read_in_either_form(tmp_path):
    root = _repo(tmp_path)
    lane = root / "Board" / "tasks"
    (lane / "block.md").write_text(
        "---\nid: block\nstate: tasks\ntags:\n  - nightshift\n---\n\nbody\n",
        encoding="utf-8")
    (lane / "inline.md").write_text(
        "---\nid: inline\nstate: tasks\ntags: [nightshift]\n---\n\nbody\n",
        encoding="utf-8")
    (lane / "none.md").write_text(
        "---\nid: none\nstate: tasks\n---\n\nbody\n", encoding="utf-8")

    tags = {c.id: c.tags for c in board.cards(root, "tasks")}

    assert tags["block"] == ["nightshift"]
    assert tags["inline"] == ["nightshift"]
    assert tags["none"] == []
