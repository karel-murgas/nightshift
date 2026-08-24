"""Tests for `nightshift.memoryfold` — the fix for the collision, not for its symptom.

Every card wants to add its entry at the top of the same shared log, so two cards
finishing the same night conflict by construction. The fold moves that write off the
card's branch and into a serial post-merge step. What is worth pinning here is
therefore: the entry lands where a human would have put it, the file's own formatting
survives, and **nothing is ever silently dropped** — a fragment that cannot be placed
stays on disk rather than disappearing, because losing a card's record is invisible
and permanent while a leftover fragment is neither.

The two real target shapes are both represented, because they disagree about spacing
and about what follows the heading, and a fold correct for one was wrong for the
other on the first attempt:

* a **register** — heading, a prose preamble, then a tight bullet list;
* a **history** — heading, then blank-line-separated `###` sections.
"""
from __future__ import annotations

from pathlib import Path

from nightshift import memoryfold

REGISTER = """\
---
name: state
---

Some preamble about the file as a whole.

## Current State (updated 2026-08-18, branch `ai/perks-tinkering`)

Newest first. Each line is a register entry — what shipped and when.

- **Older thing** (2026-08-20, `older`): one line.
- **Oldest thing** (2026-08-19, `oldest`): one line.

## Roadmap

- something else entirely
"""

HISTORY = """\
# Project history

## Shipped / fixed log

### 2026-08-20 — Older thing (`older`)

Its narrative, which contains a bullet list of its own:

- a nested bullet that is not an entry
- another

### 2026-08-19 — Oldest thing (`oldest`)

More narrative.
"""


def _project(tmp_path: Path, *, fold_rows: str) -> Path:
    root = tmp_path / "proj"
    (root / ".ai").mkdir(parents=True)
    (root / "Board").mkdir()
    (root / "mem").mkdir()
    (root / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "proj"\n\n' + fold_rows, encoding="utf-8")
    (root / "mem" / "state.md").write_text(REGISTER, encoding="utf-8")
    (root / "mem" / "history.md").write_text(HISTORY, encoding="utf-8")
    return root


BOTH_ROWS = """\
[[memory.fold]]
path = "mem/state.md"
under = "## Current State"
key = "register"

[[memory.fold]]
path = "mem/history.md"
under = "## Shipped / fixed log"
key = "history"
"""


def _fragment(root: Path, card_id: str, text: str) -> Path:
    directory = memoryfold.fragment_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{card_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_register_entry_lands_below_the_preamble_not_below_the_heading(tmp_path):
    """The bug the first implementation shipped: "directly below the heading" put
    the entry between `## Current State` and the paragraph explaining what a
    register entry is. The anchor is the section's current newest *entry*."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    _fragment(root, "probe", "## register\n\n- **New thing** (2026-08-23, `probe`): x.\n")

    memoryfold.fold(root)

    lines = (root / "mem" / "state.md").read_text(encoding="utf-8").splitlines()
    assert "Newest first. Each line is a register entry — what shipped and when." in lines
    new = lines.index("- **New thing** (2026-08-23, `probe`): x.")
    assert lines[new - 1] == ""                      # the blank after the preamble
    assert lines[new + 1].startswith("- **Older thing**")


def test_the_registers_tight_spacing_survives(tmp_path):
    """A blank line inserted per card would slowly reformat a curated file and walk
    it toward the orientation byte budget. The separator is read off the file."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    _fragment(root, "probe", "## register\n\n- **New thing** (2026-08-23, `probe`): x.\n")

    memoryfold.fold(root)

    text = (root / "mem" / "state.md").read_text(encoding="utf-8")
    assert ("- **New thing** (2026-08-23, `probe`): x.\n"
            "- **Older thing** (2026-08-20, `older`): one line.") in text


def test_the_historys_blank_line_separation_survives(tmp_path):
    """The other convention, from the same rule. Its entries *are* separated by a
    blank line, and a fold that stripped it would be as wrong as one that added it
    to the register."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    _fragment(root, "probe",
              "## history\n\n### 2026-08-23 — New thing (`probe`)\n\nNarrative.\n")

    memoryfold.fold(root)

    text = (root / "mem" / "history.md").read_text(encoding="utf-8")
    assert ("### 2026-08-23 — New thing (`probe`)\n\nNarrative.\n\n"
            "### 2026-08-20 — Older thing (`older`)") in text


def test_a_bullet_inside_a_history_entry_is_not_mistaken_for_the_next_entry(tmp_path):
    """The spacing is read from the gap between two entries *of the same kind*. The
    older entry's body contains its own bullet list, and keying off "the next line
    starting with `-`" would measure the wrong pair."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    _fragment(root, "probe",
              "## history\n\n### 2026-08-23 — New thing (`probe`)\n\nNarrative.\n")

    memoryfold.fold(root)

    text = (root / "mem" / "history.md").read_text(encoding="utf-8")
    assert "- a nested bullet that is not an entry" in text
    assert "Narrative.\n\n### 2026-08-20" in text


def test_one_fragment_feeds_both_targets_and_is_then_gone(tmp_path):
    """The ordinary case: a card writes one fragment carrying a one-line register
    entry and a paragraph of history, both land, and the fragment is removed so the
    next fold does not apply it twice."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    path = _fragment(root, "probe",
                     "## register\n\n- **New** (2026-08-23, `probe`): x.\n\n"
                     "## history\n\n### 2026-08-23 — New (`probe`)\n\nNarrative.\n")

    report = memoryfold.fold(root)

    assert "- **New** (2026-08-23, `probe`): x." in (root / "mem" / "state.md").read_text(encoding="utf-8")
    assert "### 2026-08-23 — New (`probe`)" in (root / "mem" / "history.md").read_text(encoding="utf-8")
    assert not path.exists()
    assert len(report) == 2


def test_a_fragment_whose_heading_is_gone_is_left_on_disk(tmp_path):
    """The whole safety property. A renamed heading is a configuration error, and
    the fold must not respond by appending the entry somewhere nobody re-reads, nor
    by deleting the fragment. It stays, it is reported, and the run can be fixed."""
    rows = BOTH_ROWS.replace('under = "## Current State"', 'under = "## Gone"')
    root = _project(tmp_path, fold_rows=rows)
    path = _fragment(root, "probe", "## register\n\n- **New** (2026-08-23, `probe`): x.\n")

    report = memoryfold.fold(root)

    assert path.exists(), "the card's record must survive a misconfiguration"
    assert any("LEFT IN PLACE" in line for line in report)
    assert "- **New**" not in (root / "mem" / "state.md").read_text(encoding="utf-8")


def test_a_fragment_with_an_unknown_key_is_left_on_disk(tmp_path):
    """Same rule, the other way in: a section naming no declared target. Reported,
    never dropped — a typo'd heading must not silently discard the record."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    path = _fragment(root, "probe", "## registers\n\n- **New** (2026-08-23, `probe`): x.\n")

    report = memoryfold.fold(root)

    assert path.exists()
    assert any("no `[[memory.fold]]` target has key" in line for line in report)


def test_dry_run_changes_nothing(tmp_path):
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    before = (root / "mem" / "state.md").read_text(encoding="utf-8")
    path = _fragment(root, "probe", "## register\n\n- **New** (2026-08-23, `probe`): x.\n")

    report = memoryfold.fold(root, apply=False)

    assert report, "a dry run must still say what it would do"
    assert (root / "mem" / "state.md").read_text(encoding="utf-8") == before
    assert path.exists()


def test_only_the_named_card_is_folded(tmp_path):
    """What the runner uses: a card's record lands the moment *that* card merges,
    and a sibling still in flight keeps its fragment."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    _fragment(root, "mine", "## register\n\n- **Mine** (2026-08-23, `mine`): x.\n")
    sibling = _fragment(root, "theirs", "## register\n\n- **Theirs** (2026-08-23, `theirs`): y.\n")

    memoryfold.fold(root, card_id="mine")

    text = (root / "mem" / "state.md").read_text(encoding="utf-8")
    assert "- **Mine**" in text
    assert "- **Theirs**" not in text
    assert sibling.exists()


def test_a_fragment_left_at_the_old_board_location_still_folds(tmp_path):
    """The transition case: a worker dispatched before the fragment moved out of the
    board was told to write `Board/.memory/<card-id>.md`, and its branch merges after
    the move. `fold()` must still find and fold it rather than silently skipping a
    card's record because it looked only in the new location."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    legacy = memoryfold._legacy_fragment_dir(root)
    legacy.mkdir(parents=True, exist_ok=True)
    path = legacy / "probe.md"
    path.write_text("## register\n\n- **New thing** (2026-08-25, `probe`): x.\n",
                     encoding="utf-8")

    report = memoryfold.fold(root)

    assert "- **New thing** (2026-08-25, `probe`): x." in \
        (root / "mem" / "state.md").read_text(encoding="utf-8")
    assert not path.exists()
    assert len(report) == 1


def test_a_project_declaring_no_targets_folds_nothing(tmp_path):
    """Off unless declared: the whole mechanism is inert for a project that never
    opted in, including one that happens to have a fragment directory."""
    root = _project(tmp_path, fold_rows="")
    path = _fragment(root, "probe", "## register\n\n- **New**: x.\n")

    assert memoryfold.fold(root) == []
    assert path.exists()


def test_two_cards_folded_in_sequence_both_survive(tmp_path):
    """The property the whole change exists for. Under the old scheme these two
    edits collided; here the second fold reads the file the first one wrote, and
    both entries are present with the newer one on top."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    _fragment(root, "first", "## register\n\n- **First** (2026-08-23, `first`): x.\n")
    _fragment(root, "second", "## register\n\n- **Second** (2026-08-23, `second`): y.\n")

    memoryfold.fold(root, card_id="first")
    memoryfold.fold(root, card_id="second")

    lines = (root / "mem" / "state.md").read_text(encoding="utf-8").splitlines()
    assert lines.index("- **Second** (2026-08-23, `second`): y.") \
        < lines.index("- **First** (2026-08-23, `first`): x.") \
        < lines.index("- **Older thing** (2026-08-20, `older`): one line.")


def test_an_ambiguous_heading_is_refused_rather_than_guessed(tmp_path):
    """`under` is a unique prefix. Two headings starting with it is a configuration
    error, and picking one would put half the project's records in the wrong
    section — silently, and for as long as nobody re-read the file."""
    root = _project(tmp_path, fold_rows=BOTH_ROWS)
    state = root / "mem" / "state.md"
    state.write_text(REGISTER + "\n## Current State (archive)\n\n- old\n", encoding="utf-8")
    path = _fragment(root, "probe", "## register\n\n- **New**: x.\n")

    report = memoryfold.fold(root)

    assert path.exists()
    assert any("no unique heading" in line for line in report)
