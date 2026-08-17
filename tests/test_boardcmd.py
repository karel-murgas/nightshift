"""The board's write verbs — the five gestures the control panel needs.

What these are actually guarding is not "does the file move". It is the three
properties a board write can quietly lose while still looking like it worked:

**Frontmatter survives a touch.** A board is hand-written in one editor and
machine-rewritten in another, so a writer that reorders fields or normalises
quoting produces a diff on every touch and the board's git history stops being
readable. Asserted as *byte equality* of the frontmatter block, not as a field
lookup — a lookup passes on a file that was rewritten from scratch.

**A lane change is two writes, and both have to happen.** Base Board groups on
`state:` and never moves a file. A verb that moved a card without reconciling
leaves the Kanban showing it where it no longer is, which is invisible from the
lane alone — so the reconcile is asserted through a *second, unrelated* card
that only a real reconcile pass would move.

**The private lane is moved, never read.** The whole output of `promote` is
checked for the note's own text, because the failure mode is not a refusal, it
is a helpful summary line nobody asked for.

Real git repositories throughout: `git mv`, staging and the commit messages are
the subject, and a fake would assert the mock.
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import board, boardcmd
from nightshift.gates import card_schema

import _fixtures

# Deliberately awkward: fields out of schema order, a quoted value, a single-quoted
# one, and `kanban_order` sitting in the middle rather than at the end. A writer
# that rebuilt this block instead of splicing it would tidy every one of those.
CARD = """\
---
id: {id}
title: "A card, with a comma"
state: {state}
worker: code-thread
kanban_order: {order}
tier: worker
recipe: 'none'
unattended: false
verify: play
---

## Intent

One thing.

## Acceptance

- machine: green.
"""

NOTE = """\
Some rough thought with a distinctive phrase: pomeranian-carburettor.

It has a second paragraph, and no frontmatter at all.
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _build(root: Path) -> None:
    _fixtures.git_init(root, email="t@t")
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    for lane in board.LANES + (board.PRIVATE_LANE,):
        (root / "Board" / lane).mkdir(parents=True)
        (root / "Board" / lane / ".gitkeep").write_bytes(b"")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def _repo(tmp_path: Path) -> Path:
    """A committed repo with every lane, and nothing in them."""
    return _fixtures.repo_copy("board-every-lane", tmp_path / "repo", _build)


def _card(root: Path, lane: str, card_id: str, *, state: str | None = None,
          order: str = "VN") -> Path:
    path = root / "Board" / lane / f"{card_id}.md"
    path.write_text(CARD.format(id=card_id, state=state or lane, order=order),
                    encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"card {card_id}")
    return path


def _frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    block = board.FRONTMATTER.match(text)
    assert block, f"{path} lost its frontmatter block entirely"
    return block.group(0)


def _lane_of(root: Path, card_id: str) -> str:
    card = board.find(root, card_id)
    assert card is not None, f"{card_id} is in no lane at all"
    return card.lane


# ------------------------------------------------------------------ verb 1


def test_reorder_writes_the_field_and_leaves_every_other_one_untouched(tmp_path):
    """The one assertion that matters here is *positional*: same keys, same order,
    same spelling of every value that was not asked about."""
    root = _repo(tmp_path)
    path = _card(root, "tasks", "a-card", order="VN")
    before = _frontmatter(path).splitlines()

    message = boardcmd.reorder(root, "a-card", "VZ")

    after = _frontmatter(path).splitlines()
    assert [line.split(":")[0] for line in after] == [line.split(":")[0] for line in before]
    assert [line for line in after if not line.startswith("kanban_order")] == \
           [line for line in before if not line.startswith("kanban_order")]
    assert "kanban_order: VZ" in after
    assert "VN" in message and "VZ" in message, message


def test_reorder_appends_the_field_when_a_card_never_had_one(tmp_path):
    """A card only acquires `kanban_order` by being dragged, so its absence is
    normal and must not be an error."""
    root = _repo(tmp_path)
    path = root / "Board" / "tasks" / "plain.md"
    path.write_text("---\nid: plain\nstate: tasks\n---\n\nbody\n",
                    encoding="utf-8", newline="")

    boardcmd.reorder(root, "plain", "VA")

    assert _frontmatter(path) == "---\nid: plain\nstate: tasks\nkanban_order: VA\n---\n"


def test_reorder_refuses_an_empty_value(tmp_path):
    """Empty would read as 'never prioritised', which is a different card state
    from 'ordered first' and would silently sort it last."""
    root = _repo(tmp_path)
    _card(root, "tasks", "a-card")
    with pytest.raises(boardcmd.BoardCommandError):
        boardcmd.reorder(root, "a-card", "  ")


def test_a_verb_on_a_card_that_does_not_exist_refuses_by_id(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(boardcmd.BoardCommandError, match="ghost"):
        boardcmd.reorder(root, "ghost", "VA")


# ------------------------------------------------------------------ verb 2


def test_marking_a_card_verified_lands_it_in_done_and_reconciles(tmp_path):
    """Both halves, and the reconcile is asserted through a *different* card.

    Asserting it through the marked card proves nothing — `board.move` already
    stamps `state:` — so the board carries a second card whose `state:` and lane
    disagree, and only a real reconcile pass moves it.
    """
    root = _repo(tmp_path)
    _card(root, "testing", "played")
    _card(root, "tasks", "dragged", state="review")   # dragged in the Kanban, not moved

    message = boardcmd.mark_verified(root, "played")

    assert _lane_of(root, "played") == "done"
    assert board.find(root, "played").fields["state"] == "done"
    assert _lane_of(root, "dragged") == "review", (
        "the second card was left where it was — the verb moved a file and skipped "
        "the reconcile, which is the half-done transition this exists to prevent")
    assert "done/" in message


def test_marking_a_card_verified_refuses_from_any_other_lane(tmp_path):
    """`done/` means a human played it. Reaching that lane from `review/` through
    this verb would be a transition nobody made."""
    root = _repo(tmp_path)
    _card(root, "review", "unreviewed")
    with pytest.raises(boardcmd.BoardCommandError, match="review/"):
        boardcmd.mark_verified(root, "unreviewed")
    assert _lane_of(root, "unreviewed") == "review"


def test_marking_a_card_verified_commits_the_move(tmp_path):
    """The board is git state. A move that never reaches a commit is a move the
    next machine cannot see."""
    root = _repo(tmp_path)
    _card(root, "testing", "played")

    boardcmd.mark_verified(root, "played")

    assert not _git(root, "status", "--porcelain", "--", "Board").stdout.strip()


# ------------------------------------------------------------------ verb 3


def test_promoting_an_idea_moves_the_file_and_prints_nothing_from_inside_it(
        tmp_path, capsys):
    """The failure this guards is not a refusal — it is a helpful one-line summary
    of a note nobody was allowed to read."""
    root = _repo(tmp_path)
    (root / "Board" / board.PRIVATE_LANE / "thought.md").write_text(
        NOTE, encoding="utf-8", newline="")

    code = boardcmd.main(["--root", str(root), "promote", "thought.md"])
    printed = capsys.readouterr()

    assert code == 0
    assert (root / "Board" / "inbox" / "thought.md").read_text(encoding="utf-8") == NOTE
    assert not (root / "Board" / board.PRIVATE_LANE / "thought.md").exists()
    for stream in (printed.out, printed.err):
        assert "pomeranian-carburettor" not in stream, stream
        assert "second paragraph" not in stream, stream


def test_promoting_leaves_the_notes_text_out_of_the_commit_message_too(tmp_path):
    """git log is output as much as stdout is, and it is the copy that persists."""
    root = _repo(tmp_path)
    (root / "Board" / board.PRIVATE_LANE / "thought.md").write_text(
        NOTE, encoding="utf-8", newline="")

    boardcmd.promote(root, "thought.md")

    log = _git(root, "log", "-1", "--format=%B").stdout
    assert "thought.md" in log
    assert "pomeranian-carburettor" not in log, log


def test_promoting_refuses_rather_than_overwriting_a_note_of_the_same_name(tmp_path):
    root = _repo(tmp_path)
    (root / "Board" / board.PRIVATE_LANE / "thought.md").write_text(
        NOTE, encoding="utf-8", newline="")
    (root / "Board" / "inbox" / "thought.md").write_text("mine\n", encoding="utf-8",
                                                         newline="")
    with pytest.raises(boardcmd.BoardCommandError, match="already exists"):
        boardcmd.promote(root, "thought.md")
    assert (root / "Board" / "inbox" / "thought.md").read_text(encoding="utf-8") == "mine\n"


def test_a_verb_refuses_a_filename_that_is_a_path(tmp_path):
    """The only way any of these could write outside the lane it names."""
    root = _repo(tmp_path)
    for name in ("../outside.md", "sub/thought.md", ".."):
        with pytest.raises(boardcmd.BoardCommandError):
            boardcmd.promote(root, name)


# ------------------------------------------------------------------ verb 4


def test_a_new_note_is_bare_and_indistinguishable_from_a_typed_one(tmp_path):
    """No frontmatter, no `state:`. A note that arrived through a panel must look
    exactly like one typed into the lane, or the classifier sees two shapes."""
    root = _repo(tmp_path)

    message = boardcmd.create_note(root, "idea", "Just this.")

    path = root / "Board" / "inbox" / "idea.md"
    assert path.read_text(encoding="utf-8") == "Just this.\n"
    assert "idea.md" in message
    assert not _git(root, "status", "--porcelain", "--", "Board").stdout.strip()


def test_a_new_note_refuses_to_overwrite_an_existing_one(tmp_path):
    root = _repo(tmp_path)
    boardcmd.create_note(root, "idea", "first")
    with pytest.raises(boardcmd.BoardCommandError, match="edit"):
        boardcmd.create_note(root, "idea.md", "second")
    assert (root / "Board" / "inbox" / "idea.md").read_text(encoding="utf-8") == "first\n"


def test_a_new_note_may_target_the_private_lane_and_reads_nothing_back(tmp_path, capsys):
    """The panel's Ideas page needs a `new` action, and writing a fresh file into
    `ideas/` reads nothing — the same boundary `promote` keeps in the other
    direction. The printed confirmation must not echo the body."""
    root = _repo(tmp_path)

    message = boardcmd.create_note(root, "thought", "pomeranian-carburettor",
                                   lane=board.PRIVATE_LANE)

    path = root / "Board" / board.PRIVATE_LANE / "thought.md"
    assert path.read_text(encoding="utf-8") == "pomeranian-carburettor\n"
    assert "pomeranian-carburettor" not in message


def test_a_new_note_refuses_a_lane_that_is_not_inbox_or_ideas(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(boardcmd.BoardCommandError, match="not a lane"):
        boardcmd.create_note(root, "sneaky", "x", lane="tasks")


# ------------------------------------------------------------------ verb 5


def test_editing_replaces_the_body_and_preserves_the_frontmatter_byte_for_byte(tmp_path):
    """Byte equality, not field equality: a rebuilt block would tidy the quoting
    and the field order and still pass a lookup-based check."""
    root = _repo(tmp_path)
    path = _card(root, "tasks", "a-card")
    before = _frontmatter(path)

    message = boardcmd.edit_body(root, "Board/tasks/a-card.md", "## Intent\n\nSomething else.")

    assert _frontmatter(path) == before
    assert path.read_text(encoding="utf-8") == before + "## Intent\n\nSomething else.\n"
    assert "preserved" in message


def test_editing_a_file_with_no_frontmatter_replaces_it_whole(tmp_path):
    """A bare inbox note is the ordinary case, and inventing a block for it would
    be this module writing frontmatter."""
    root = _repo(tmp_path)
    path = root / "Board" / "inbox" / "thought.md"
    path.write_text(NOTE, encoding="utf-8", newline="")

    boardcmd.edit_body(root, path, "rewritten")

    assert path.read_text(encoding="utf-8") == "rewritten\n"


def test_editing_writes_lf_whatever_the_platform(tmp_path):
    """`.gitattributes` pins the tree to LF; a text-mode write would CRLF it on
    Windows and leave a phantom-dirty file forever."""
    root = _repo(tmp_path)
    path = root / "Board" / "inbox" / "thought.md"
    path.write_text(NOTE, encoding="utf-8", newline="")

    boardcmd.edit_body(root, path, "one\ntwo")

    assert path.read_bytes() == b"one\ntwo\n"


def test_editing_refuses_a_path_outside_the_board(tmp_path):
    """The one failure a panel could not undo from the board."""
    root = _repo(tmp_path)
    (root / "README.md").write_text("real\n", encoding="utf-8", newline="")
    with pytest.raises(boardcmd.BoardCommandError, match="board files only"):
        boardcmd.edit_body(root, "README.md", "clobbered")
    assert (root / "README.md").read_text(encoding="utf-8") == "real\n"


def test_editing_refuses_a_file_that_does_not_exist(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(boardcmd.BoardCommandError, match="does not exist"):
        boardcmd.edit_body(root, "Board/inbox/nothing.md", "x")


# ------------------------------------------------------- scriptable, not interactive


class _Forbidden:
    """A stdin that fails loudly rather than blocking, which is what a real one
    would do inside a request handler with nobody at a keyboard."""

    def read(self, *args, **kwargs):
        raise AssertionError("a verb read stdin without being told to")

    def readline(self, *args, **kwargs):
        return self.read()


@pytest.mark.parametrize("argv", [
    ["reorder", "a-card", "VZ"],
    ["verified", "played"],
    ["promote", "thought.md"],
    ["note", "fresh.md", "--body", "text"],
    ["edit", "Board/tasks/a-card.md", "--body", "text"],
])
def test_no_verb_touches_stdin_unless_it_was_named(tmp_path, monkeypatch, argv):
    root = _repo(tmp_path)
    _card(root, "tasks", "a-card")
    _card(root, "testing", "played")
    (root / "Board" / board.PRIVATE_LANE / "thought.md").write_text(
        NOTE, encoding="utf-8", newline="")
    monkeypatch.setattr(sys, "stdin", _Forbidden())

    assert boardcmd.main(["--root", str(root), *argv]) == 0


def test_a_body_may_be_piped_in_explicitly(tmp_path, monkeypatch, capsys):
    """`--body-file -` is the panel's path: the text comes from the request, not
    from a terminal."""
    root = _repo(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped body"))

    code = boardcmd.main(["--root", str(root), "note", "piped", "--body-file", "-"])
    capsys.readouterr()

    assert code == 0
    assert (root / "Board" / "inbox" / "piped.md").read_text(encoding="utf-8") == "piped body\n"


def test_a_refusal_exits_non_zero_and_says_why(tmp_path, capsys):
    """The caller is a request handler: it needs a reason it can render and a
    status it can branch on."""
    root = _repo(tmp_path)
    code = boardcmd.main(["--root", str(root), "reorder", "ghost", "VA"])
    printed = capsys.readouterr()
    assert code == 1
    assert "ghost" in printed.err


def test_every_verb_is_invocable_from_a_real_command_line(tmp_path):
    """One end-to-end run through `python -m`, because the panel will not import
    this module — it will run it."""
    root = _repo(tmp_path)
    _card(root, "tasks", "a-card")
    done = subprocess.run(
        [sys.executable, "-m", "nightshift.boardcmd", "--root", str(root),
         "reorder", "a-card", "VZ"],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False)
    assert done.returncode == 0, done.stderr
    assert "kanban_order" in done.stdout
    assert "kanban_order: VZ" in (root / "Board" / "tasks" / "a-card.md").read_text(
        encoding="utf-8")


# ------------------------------------------------------- no second parser


def test_the_module_grows_no_frontmatter_parser_of_its_own():
    """The card's standing constraint, made mechanical. Four copies of the
    frontmatter grammar already exist and each one is justified in its own
    docstring; a fifth arriving inside a CLI verb would be justified nowhere.
    """
    source = Path(boardcmd.__file__).read_text(encoding="utf-8")
    assert "import re" not in source, (
        "a regex in here is how the fifth frontmatter parser starts — every write "
        "goes through board.set_fields / board.move")
    assert "re.compile" not in source


# --------------------------------------------------------------------------
# Closing an inline note. Every other route's note is moved by the process that
# works it — the scribe and triage move theirs as they card it, the runner moves
# cards as it goes. `inline` means Karel is the process, so the move needed a
# hand, and without one the note sat in `inbox/` after the work was done: routed
# again by the next classify pass, and listed by the panel as still waiting.
# --------------------------------------------------------------------------


def test_closing_an_inline_note_files_it_in_done(tmp_path):
    root = _repo(tmp_path)
    (root / "Board" / "inbox" / "tidy the hotbar.md").write_text(
        "The hotbar spacing is off by a pixel.\n", encoding="utf-8")

    said = boardcmd.close_note(root, "tidy the hotbar.md")

    assert not (root / "Board" / "inbox" / "tidy the hotbar.md").exists()
    card = root / "Board" / "done" / "tidy-the-hotbar.md"
    assert card.is_file(), said
    text = card.read_text(encoding="utf-8")
    assert "id: tidy-the-hotbar" in text
    assert "state: done" in text
    assert "The hotbar spacing is off by a pixel." in text, "the note is the intent"


def test_a_closed_inline_note_satisfies_the_schema_it_is_now_subject_to(tmp_path):
    """`done/` is fully schema'd, so the stamp has to produce a real card — not a
    note in a lane that will redden the board on the next gate run."""
    root = _repo(tmp_path)
    (root / "Board" / "inbox" / "some note.md").write_text("do the thing\n",
                                                           encoding="utf-8")
    boardcmd.close_note(root, "some note.md")

    violations = card_schema.check(root)
    assert [v for v in violations if "some" in str(v.path) or "note" in str(v.path)] == []
    assert violations == [], violations


def test_every_stamped_field_is_one_the_verb_actually_knows(tmp_path):
    """No agent's name on a human's work, and no invented acceptance criteria.

    An inline note is one the classifier sent to a person *because* a card would
    have been overhead; writing machine-checkable criteria for it afterwards would
    fabricate the brief that deliberately never existed.
    """
    root = _repo(tmp_path)
    (root / "Board" / "inbox" / "n.md").write_text("x\n", encoding="utf-8")
    boardcmd.close_note(root, "n.md")

    text = (root / "Board" / "done" / "n.md").read_text(encoding="utf-8")
    assert "worker: none" in text and "recipe: none" in text
    assert "unattended: false" in text
    assert "closed by hand" in text


def test_closing_a_note_that_is_already_a_card_is_refused(tmp_path):
    """A file carrying triage's own fields is a card mid-triage, and a card advances
    through its own lanes rather than being stamped into a new one."""
    root = _repo(tmp_path)
    (root / "Board" / "inbox" / "half.md").write_text(
        "---\nid: half\nstate: inbox\n---\n\nbody\n", encoding="utf-8")

    with pytest.raises(boardcmd.BoardCommandError, match="carries triage field"):
        boardcmd.close_note(root, "half.md")
    assert (root / "Board" / "inbox" / "half.md").exists()


def test_a_note_carrying_only_lane_and_kanban_bookkeeping_still_closes(tmp_path):
    """The bug that made this verb unusable on a real board.

    The guard refused on *any* frontmatter, reasoning that a bare note has none.
    True of a note typed into the filesystem; false of every note on a board opened
    in Obsidian, whose Kanban plugin stamps `state:` and `kanban_order:` on all of
    them. Measured 2026-08-18 on the origin project: 7 of 7 inbox notes carried
    exactly these two keys, so `close` refused every one and the panel's `Done`
    gesture — the only route out of `inline` — could close nothing at all.
    """
    root = _repo(tmp_path)
    (root / "Board" / "inbox" / "n.md").write_text(
        "---\nstate: inbox\nkanban_order: VL\n---\n\nthe real prose\n",
        encoding="utf-8")

    boardcmd.close_note(root, "n.md")

    assert not (root / "Board" / "inbox" / "n.md").exists()
    text = (root / "Board" / "done" / "n.md").read_text(encoding="utf-8")
    assert "the real prose" in text
    # Exactly one frontmatter block, the stamped one — the note's own must not
    # survive inside `## Intent`, or Obsidian reads the file as having two.
    assert text.count("---\n") == 2, text
    assert "kanban_order" not in text
    assert "state: done" in text


def test_a_closed_note_that_had_bookkeeping_still_satisfies_the_schema(tmp_path):
    """The stamped card is subject to `card_schema`'s full `done/` requirements, and
    stripping the old frontmatter must not have broken that."""
    from nightshift.gates import card_schema

    root = _repo(tmp_path)
    (root / "Board" / "inbox" / "n.md").write_text(
        "---\nstate: inbox\nkanban_order: VL\n---\n\nbody\n", encoding="utf-8")
    boardcmd.close_note(root, "n.md")

    offending = [str(v) for v in card_schema.check(root) if "n.md" in v.file]
    assert not offending, offending


def test_closing_a_note_that_is_not_there_is_refused(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(boardcmd.BoardCommandError, match="no note called"):
        boardcmd.close_note(root, "ghost.md")


def test_closing_will_not_overwrite_a_card_that_already_has_the_name(tmp_path):
    root = _repo(tmp_path)
    (root / "Board" / "done" / "taken.md").write_text("the real one\n", encoding="utf-8")
    (root / "Board" / "inbox" / "taken.md").write_text("a note\n", encoding="utf-8")

    with pytest.raises(boardcmd.BoardCommandError, match="already exists"):
        boardcmd.close_note(root, "taken.md")
    assert (root / "Board" / "done" / "taken.md").read_text(encoding="utf-8") \
        == "the real one\n"


def test_the_close_is_committed_like_every_other_board_write(tmp_path):
    root = _repo(tmp_path)
    (root / "Board" / "inbox" / "n.md").write_text("x\n", encoding="utf-8")
    boardcmd.close_note(root, "n.md")

    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    assert "closed inline note" in _git(root, "log", "-1", "--format=%s").stdout
