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
