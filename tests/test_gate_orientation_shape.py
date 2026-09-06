"""`orientation_shape` — an always-loaded document must not become a log.

`orientation_budget` catches the size, and by then someone has to compress a 196 KB file
under deadline. This catches the shape that produces the size, which is visible from the
first few entries.

Reported as a gap by the origin project's maintainer, 2026-08-03, installing into a
second repo: *"it would be nice if it set it for new project too... this probably should
be a rule enforced by nightshift?"*
"""
from __future__ import annotations

from pathlib import Path

from nightshift.gates import orientation_shape


def _repo(tmp_path: Path, files: dict[str, str], *, declared: list[str] | None = None,
          table: bool = True) -> Path:
    root = tmp_path / "proj"
    (root / ".ai").mkdir(parents=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    lines = ['[project]', 'name = "proj"', '']
    if table:
        names = declared if declared is not None else list(files)
        lines += ["[memory]", "orientation = [" + ", ".join(f'"{n}"' for n in names) + "]"]
    (root / ".ai" / "manifest.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


LOG = """\
# State

## 2026-07-01 — session one

Did a thing.

## 2026-07-02 — session two

Did another.

## 2026-07-03 — session three

And another.
"""


def test_a_file_of_dated_sections_is_reported(tmp_path):
    repo = _repo(tmp_path, {".claude/memory/state.md": LOG})
    found = orientation_shape.check(repo)

    assert len(found) == 1
    assert "3 dated headings" in found[0].rule
    assert "state_history.md" in found[0].rule, "and it names the companion"
    assert found[0].line == 3, "pointing at the first dated heading, not the file"


def test_two_dated_headings_are_notes_not_a_log(tmp_path):
    """The threshold is the whole design. A gate firing on any date in an orientation
    file would be muted within a week, which is worse than not having it."""
    two = "# State\n\n## 2026-07-01 — a thing\n\nx\n\n## 2026-07-02 — another\n\ny\n"
    assert orientation_shape.check(_repo(tmp_path, {"state.md": two})) == []


def test_dates_in_prose_are_untouched(tmp_path):
    """A design document recording *decided 2026-07-24, because…* inline is doing
    exactly the right thing."""
    prose = ("# Design\n\n## Tile size\n\nDecided 2026-07-24, revisited 2026-07-25, "
             "settled 2026-07-26 — 32px.\n")
    assert orientation_shape.check(_repo(tmp_path, {"design.md": prose})) == []


def test_a_companion_is_out_of_scope_because_it_is_not_declared(tmp_path):
    """`state_history.md` is *supposed* to be nothing but dated sections. It is out of
    scope precisely because it is not loaded every session."""
    repo = _repo(tmp_path,
                 {"state.md": "# State\n\nCurrent: phase 3.\n",
                  "state_history.md": LOG},
                 declared=["state.md"])
    assert orientation_shape.check(repo) == []


def test_no_declaration_means_no_opinion(tmp_path):
    """Absence disables it — the rule every manifest-driven gate here follows."""
    assert orientation_shape.check(_repo(tmp_path, {"state.md": LOG}, table=False)) == []


def test_a_missing_orientation_file_is_not_this_gates_problem(tmp_path):
    repo = _repo(tmp_path, {"state.md": LOG}, declared=["state.md", "gone.md"])
    assert len(orientation_shape.check(repo)) == 1


REGISTER = """\
# Design

## Traps (decided 2026-07-17)

Pressure plates, 3 kinds.

## Secret passages (2026-07-17)

Revealed by search.

## Skills System (decided 2026-07-18, IMPLEMENTED WP1-WP8)

Six trees.

## Hack ICE (decided 2026-07-16)

Four ICE types.
"""


def test_a_decision_register_is_not_a_log(tmp_path):
    """The distinction the first version of this gate got wrong, and the reason it is
    worth having tests taken from a real repo rather than invented.

    A log has headings that *are* dates — the session is the subject. A register has
    headings that are subjects with a date attached, and is exactly what an orientation
    document should look like. Shipped matching a date anywhere in the heading, this
    fired on the origin project's 40 KB `design.md`, which is a register doing its job.
    Verbatim from that file.
    """
    assert orientation_shape.check(_repo(tmp_path, {"design.md": REGISTER})) == []


def test_leading_decoration_around_a_date_is_still_a_log(tmp_path):
    """`## **2026-08-01**` is styling, not a subject."""
    styled = ("# State\n\n## **2026-08-01** notes\n\na\n\n## [2026-08-02] notes\n\nb\n\n"
              "## _2026-08-03_ notes\n\nc\n")
    assert len(orientation_shape.check(_repo(tmp_path, {"state.md": styled}))) == 1


def test_the_companion_named_is_one_the_project_already_has(tmp_path):
    """Suggesting `design_history.md` to a repo whose convention is `design_detail.md`
    invents a second home for the same content — from the gate that exists to stop
    exactly that sprawl."""
    repo = _repo(tmp_path,
                 {"design.md": LOG, "design_detail.md": "# Detail\n"},
                 declared=["design.md"])
    found = orientation_shape.check(repo)
    assert len(found) == 1
    assert "design_detail.md" in found[0].rule


# --- the second shape: a log made of bullets ---------------------------------
#
# Dungeoneer, 2026-09-06. `state.md` carried 240 register lines under a single undated
# `## Current State`, appended by a `[[memory.fold]]` row, and this gate passed it every
# day for months: the log-detector was heading-shaped and the log was bullet-shaped.
# Counts below are the real measured ones — 103 dated list items in the failing file,
# 7 in `design.md`, the register that must keep passing.

def _bullets(n: int, *, card: bool = True) -> str:
    """`n` register lines in the shape the fold row actually wrote."""
    tail = ", `card-{i}`" if card else ""
    return "# State\n\n## Current State\n\n" + "".join(
        f"- **Thing {i} shipped** (2026-09-0{i % 9 + 1}{tail.format(i=i)}): what it does.\n"
        for i in range(n))


def test_a_log_made_of_bullets_under_one_undated_heading_is_reported(tmp_path):
    """The failure this half was written for. Not one dated heading, so the original
    rule sees nothing at all."""
    repo = _repo(tmp_path, {".claude/memory/state.md": _bullets(103)})

    found = orientation_shape.check(repo)

    assert len(found) == 1
    assert "103 dated list items" in found[0].rule
    assert "one entry per subsystem" in found[0].rule


def test_a_seven_entry_register_of_dated_bullets_is_not_a_log(tmp_path):
    """`design.md`'s real measured count on the day the threshold was chosen. A register
    entry legitimately carries the date the decision was made; the gate must not punish
    the document that is doing its job, or it gets appealed into uselessness."""
    register = "# Design\n\n" + "".join(
        f"- **Subsystem {i}** (decided 2026-07-1{i}, IMPLEMENTED): what it is.\n"
        for i in range(7))
    repo = _repo(tmp_path, {".claude/memory/design.md": register})

    assert orientation_shape.check(repo) == []


def test_the_list_threshold_boundary_holds_on_both_sides(tmp_path):
    """Pinned explicitly because the number is the whole rule here — the shapes either
    side of it are identical, and an off-by-one silently moves where a register becomes
    a log."""
    below = _repo(tmp_path / "below", {".claude/memory/state.md": _bullets(11)})
    at = _repo(tmp_path / "at", {".claude/memory/state.md": _bullets(12)})

    assert orientation_shape.check(below) == []
    assert len(orientation_shape.check(at)) == 1


def test_a_status_table_of_dated_rows_is_not_a_list(tmp_path):
    """The shape `state.md` was restructured *into*. A table row is not a list item, and
    a subsystem table that happens to cite dates is exactly what this gate wants people
    to write — reporting it would punish the fix."""
    table = "# State\n\n## Current State\n\n| Subsystem | Status | Notes |\n|---|---|---|\n" + "".join(
        f"| System {i} | shipped | landed 2026-08-0{i % 9 + 1} |\n" for i in range(30))
    repo = _repo(tmp_path, {".claude/memory/state.md": table})

    assert orientation_shape.check(repo) == []


def test_prose_dates_outside_a_list_are_still_untouched(tmp_path):
    """`_DATE` matches anywhere in the line, so the list-item test is what keeps ordinary
    dated prose out. Without it every paragraph mentioning a date would count."""
    prose = "# State\n\n" + "".join(
        f"The {i}th decision was taken on 2026-08-0{i % 9 + 1} and still holds.\n\n"
        for i in range(30))
    repo = _repo(tmp_path, {".claude/memory/state.md": prose})

    assert orientation_shape.check(repo) == []


def test_a_file_that_is_both_shapes_reports_both(tmp_path):
    """They are independent findings about one file: fixing the headings does not fix the
    bullets. Reporting only the first would hide half the work."""
    both = LOG + "\n" + _bullets(12).split("\n\n", 1)[1]
    repo = _repo(tmp_path, {".claude/memory/state.md": both})

    found = orientation_shape.check(repo)

    assert len(found) == 2
    assert "dated headings" in found[0].rule
    assert "dated list items" in found[1].rule


def test_the_bullet_finding_names_an_existing_changelog_companion(tmp_path):
    """Same anti-sprawl bargain as the heading finding, extended to the companion a fold
    row would actually be repointed at."""
    repo = _repo(
        tmp_path,
        {".claude/memory/state.md": _bullets(20),
         ".claude/memory/state_changelog.md": "# Changelog\n"},
        declared=[".claude/memory/state.md"])

    found = orientation_shape.check(repo)

    assert "state_changelog.md" in found[0].rule
