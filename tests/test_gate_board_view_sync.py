"""Tests for the `board_view_sync` core gate.

Synthetic repos in `tmp_path`. The one thing that cannot be synthetic is the
lane rule: the gate derives it by calling `runner.oversize_note`, and a test
that stubbed that out would only pin the gate against itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates
from nightshift import runner

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import board_view_sync  # noqa: E402

_THRESHOLD = runner.CARD_COMFORT_BYTES


def _view(folder: str = "Board/tasks", threshold: int = _THRESHOLD,
          name: str = "oversize", referenced: bool = True) -> str:
    """The shape the real files have — `properties:` block included.

    That block is not decoration in a fixture: it keys the formula by its full
    name, so a fixture without it cannot reproduce the masking that made the
    "is anything drawing this?" check pass vacuously on the very files it ships
    with.
    """
    order = f"      - formula.{name}\n" if referenced else ""
    return (
        "filters:\n"
        '  - file.inFolder("Board")\n'
        "formulas:\n"
        f'  {name}: \'if(file.inFolder("{folder}") && file.size > {threshold}, '
        '(file.size / 1024).toFixed(1) + " KB", "")\'\n'
        "properties:\n"
        f"  formula.{name}:\n"
        "    displayName: Too big\n"
        "views:\n"
        "  - type: table\n"
        "    name: Live\n"
        "    order:\n"
        "      - title\n"
        f"{order}"
    )


def _repo(tmp_path: Path, view: str | None, board_root: str = "Board") -> Path:
    manifest = tmp_path / ".ai" / "manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f'[board]\nroot = "{board_root}"\n', encoding="utf-8")
    if view is not None:
        (tmp_path / f"{board_root}.base").write_text(view, encoding="utf-8")
    return tmp_path


# --- the agreement the gate exists to hold --------------------------------

def test_a_view_matching_runner_is_clean(tmp_path):
    assert board_view_sync.check(_repo(tmp_path, _view())) == []


def test_catches_a_stale_threshold(tmp_path):
    """The drift this gate exists for: `CARD_COMFORT_BYTES` moves and the YAML
    literal, which no import can reach, stays where it was."""
    v = board_view_sync.check(_repo(tmp_path, _view(threshold=_THRESHOLD - 1024)))
    assert len(v) == 1
    assert "CARD_COMFORT_BYTES" in v[0].rule
    assert str(_THRESHOLD) in v[0].rule


def test_catches_a_view_naming_a_different_lane(tmp_path):
    v = board_view_sync.check(_repo(tmp_path, _view(folder="Board/review")))
    assert len(v) == 1
    assert "`Board/review`" in v[0].rule
    assert "`Board/tasks`" in v[0].rule


def test_catches_a_lane_named_without_the_board_root(tmp_path):
    """`inFolder("tasks")` selects a top-level `tasks/` directory, not the board's
    lane — different folder, no cards, silent board. Comparing trailing names
    instead of whole folders would call these two equal."""
    v = board_view_sync.check(_repo(tmp_path, _view(folder="tasks")))
    assert len(v) == 1
    assert "`tasks`" in v[0].rule and "`Board/tasks`" in v[0].rule


def test_catches_a_view_marking_an_extra_lane(tmp_path):
    """A superset is drift in the direction a filter grows by accident."""
    both = ('if(file.inFolder("Board/tasks") || file.inFolder("Board/testing"), '
            f"file.size > {_THRESHOLD}, \"\")")
    view = ("formulas:\n"
            f"  oversize: '{both}'\n"
            "views:\n  - order:\n      - formula.oversize\n")
    v = board_view_sync.check(_repo(tmp_path, view))
    assert len(v) == 1
    assert "Board/testing" in v[0].rule


def test_catches_the_formula_going_missing(tmp_path):
    """A board view with no size formula is the feature silently gone, which is
    a divergence from `runner.py` exactly as much as a wrong number is."""
    view = "filters:\n  - file.inFolder(\"Board\")\nviews:\n  - type: table\n"
    v = board_view_sync.check(_repo(tmp_path, view))
    assert len(v) == 1
    assert "no formula here reads `file.size`" in v[0].rule


def test_says_so_when_the_formula_is_there_but_unreadable(tmp_path):
    """A block scalar is legal YAML and Bases would still evaluate it. Reporting
    "restore the formula" for one sitting in front of the reader is the kind of
    wrong diagnostic that gets a gate distrusted, so the two are separated."""
    view = ("formulas:\n"
            "  oversize: >-\n"
            f'    if(file.inFolder("Board/tasks") && file.size > {_THRESHOLD}, "big", "")\n'
            "views:\n  - order:\n      - formula.oversize\n")
    v = board_view_sync.check(_repo(tmp_path, view))
    assert len(v) == 1
    assert "single quoted line" in v[0].rule


def test_catches_a_formula_no_view_draws(tmp_path):
    """Computed and thrown away — the half of this feature that fails without
    producing any visible error.

    The fixture keeps its `properties: formula.oversize:` entry, because that is
    the realistic shape of the accident: an `order:` list loses a line while the
    display config stays. A substring search for `formula.oversize` anywhere in
    the file is satisfied by that entry alone and reports nothing.
    """
    view = _view(referenced=False)
    assert "formula.oversize" in view, "fixture cannot reproduce the masking"
    v = board_view_sync.check(_repo(tmp_path, view))
    assert len(v) == 1
    assert "order:" in v[0].rule


def test_reads_a_double_quoted_scalar_with_escaped_quotes(tmp_path):
    """Obsidian rewrites `Board.base` on every vault open. If a rewrite ever
    picks double quotes, the inner `"Board/tasks"` becomes `\\"Board/tasks\\"` —
    the same formula, which must not be reported as naming no folder."""
    expr = (f'if(file.inFolder(\\"Board/tasks\\") && file.size > {_THRESHOLD}, '
            '\\"big\\", \\"\\")')
    view = ("formulas:\n"
            f'  oversize: "{expr}"\n'
            "views:\n  - order:\n      - formula.oversize\n")
    assert board_view_sync.check(_repo(tmp_path, view)) == []


def test_catches_a_second_size_formula(tmp_path):
    view = _view() + ("formulas:\n"
                      f"  other: 'if(file.size > {_THRESHOLD}, \"big\", \"\")'\n")
    v = board_view_sync.check(_repo(tmp_path, view))
    assert len(v) == 1
    assert "formulas read" in v[0].rule


# --- what it reads, and what it declines to read --------------------------

def test_the_view_path_follows_the_manifest_not_a_hardcoded_name(tmp_path):
    """`board.root` is project-configurable and `init` names the view after it,
    so a project whose board is `Cards/` must be checked at `Cards.base`."""
    root = _repo(tmp_path, _view(folder="Cards/tasks"), board_root="Cards")
    (root / "Board.base").write_text(_view(threshold=1), encoding="utf-8")
    assert board_view_sync.check(root) == []


def test_a_repo_with_no_board_view_is_a_no_op(tmp_path):
    assert board_view_sync.check(_repo(tmp_path, None)) == []


def test_a_repo_with_no_manifest_is_a_no_op(tmp_path):
    (tmp_path / "Board.base").write_text(_view(threshold=1), encoding="utf-8")
    assert board_view_sync.check(tmp_path) == []


def test_a_manifest_broken_in_an_unrelated_table_silences_this_gate(tmp_path):
    """Pinned because it is a real vacuous-pass vector, not because it is the
    behaviour anyone wants: `manifest.load` raises for a defect in any table, so
    a malformed `[[layering.forbid]]` row hides a real divergence in the board
    view, even though `[board]` right above it is perfectly readable. Shared with
    `branch_role_prose._roles`, and left there on purpose — `doctor` is what
    reports a broken manifest, and a gate that parsed the file its own way to
    stay alive would disagree with it about what "valid" means.

    Written against a defect that genuinely raises: most wrong-typed fields are
    coerced by `parse` and do *not*, so a fixture picked by intuition would have
    tested the gate running normally and called it this.
    """
    manifest = tmp_path / ".ai" / "manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text('[board]\nroot = "Board"\n\n[[layering.forbid]]\nimporter = 5\n',
                        encoding="utf-8")
    (tmp_path / "Board.base").write_text(_view(threshold=1), encoding="utf-8")
    assert board_view_sync.check(tmp_path) == []


def test_a_consuming_project_is_not_gated_on_the_frameworks_template(tmp_path):
    """The template lives in the installed package, which for a consumer is
    somebody else's checkout. Containment, not existence, is the test."""
    assert board_view_sync._TEMPLATE.is_file()
    assert not board_view_sync._TEMPLATE.is_relative_to(tmp_path.resolve())
    assert board_view_sync._targets(tmp_path, "Board") == []


# --- the lane rule is asked for, not restated -----------------------------

def test_the_lane_set_comes_from_oversize_note(tmp_path):
    """Recomputed here from `oversize_note` directly, so this asserts the gate
    agrees with the runner rather than with a literal typed twice."""
    oversized = "x" * (_THRESHOLD + 1)
    from nightshift import board

    expected = {lane for lane in board.LANES
                if runner.oversize_note(board.Card(path=Path("c.md"), lane=lane,
                                                   fields={}, text=oversized))}
    assert board_view_sync._marked_lanes() == expected
    assert expected, "oversize_note marks no lane at all — the probe is vacuous"


def test_the_frameworks_own_template_is_checked_and_clean():
    """Self-gating's half of the cross-repo pair: the template and a project's
    copy are separate files, so the repo that owns the template checks it."""
    repo = Path(_nightshift_gates.__file__).resolve().parents[2]
    targets = board_view_sync._targets(repo, "Board")
    assert board_view_sync._TEMPLATE in [path for path, _ in targets]
    assert board_view_sync.check(repo) == []
