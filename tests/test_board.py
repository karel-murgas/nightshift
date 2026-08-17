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

import _fixtures

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
    _fixtures.git_init(tmp_path, email="t@t")
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


# --------------------------------------------------------------------------
# Line endings, fixed at the funnel rather than at the writer.
#
# It had been fixed three times at three different writers — `Path.write_text`
# (2026-08-14), `prepare_worktree` (2026-08-13), and on 2026-08-17 an agent's own
# file tools and a human's editor, neither of which this package can reach.
# Chasing writers cannot converge: any tool that opens a file in text mode on
# Windows is a new one. Every board write in the framework ends at
# `commit_board`, so whatever wrote CRLF, it is on disk by the time we get here.
# --------------------------------------------------------------------------


def _pin_lf(root: Path) -> None:
    """What `init` writes into every installed repo, and what the whole mechanism
    rests on: with `eol=lf` pinned, `git add` stores an LF blob while the worktree
    copy stays CRLF — the phantom-dirty state the gate names and normalising
    resolves. Without it git keeps the CRLF verbatim and there is nothing to fix."""
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    subprocess.run(["git", "add", ".gitattributes"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "attrs"], cwd=root, check=True)


def test_a_board_commit_leaves_no_crlf_in_the_working_tree(tmp_path):
    root = _repo(tmp_path)
    _pin_lf(root)
    card = root / "Board" / "tasks" / "crlf-card.md"
    card.write_bytes(b"---\r\nid: crlf-card\r\nstate: tasks\r\n---\r\n\r\nbody\r\n")

    board.commit_board(root, "board: a card written by something in text mode")

    assert b"\r\n" not in card.read_bytes(), "CRLF survived the commit"


def test_a_commit_with_nothing_to_normalise_does_not_shell_out_to_do_it(tmp_path,
                                                                        monkeypatch):
    """The answer is almost always "nothing to do", and it costs one
    `git ls-files --eol` to know that. Normalising anyway would put several git
    invocations behind every card move the runner makes."""
    root = _repo(tmp_path)
    (root / "Board" / "tasks" / "clean.md").write_bytes(b"---\nid: clean\n---\n\nbody\n")

    from nightshift import normalize_worktree
    monkeypatch.setattr(normalize_worktree, "normalize",
                        lambda *a, **k: pytest.fail("nothing needed normalising"))
    board.commit_board(root, "board: a clean card")


def test_a_normalise_failure_does_not_take_the_board_write_down(tmp_path, monkeypatch,
                                                               capsys):
    """A board move failing at 3 AM over line endings would be far worse than the
    line endings — the same trade `_warn_if_failed` makes."""
    root = _repo(tmp_path)
    _pin_lf(root)
    (root / "Board" / "tasks" / "c.md").write_bytes(b"---\r\nid: c\r\n---\r\n\r\nx\r\n")

    from nightshift import normalize_worktree
    monkeypatch.setattr(normalize_worktree, "worktree_crlf_files",
                        lambda root: (_ for _ in ()).throw(OSError("git is missing")))

    board.commit_board(root, "board: still commits")
    assert "could not normalise" in capsys.readouterr().out
    said = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=root,
                          capture_output=True, text=True, check=True).stdout
    assert said.strip() == "board: still commits"
