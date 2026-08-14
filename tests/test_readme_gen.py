"""Tests for `nightshift/readme_gen.py` — the README.md generated-block
mechanism (07_portability.md §8 D4: "any list in the README ... is generated
from the code that owns it").

Assertions here check *shape* (every schema table appears, every discovered
gate appears) rather than pinning today's exact rendered text — a test that
reproduced the generator's own string-building would break on every
legitimate addition to the gate set or the manifest schema, which is exactly
the D4 failure this module exists to stop happening a third time.
"""
from __future__ import annotations

from pathlib import Path

from nightshift import board, manifest, readme_gen
from nightshift.gates.run import discover


def _doc(name: str, body: str) -> str:
    return f"# example\n\n<!-- generated:{name} -->\n{body}\n<!-- /generated:{name} -->\n"


def test_block_names_finds_every_marker():
    text = (_doc("board-lanes", "stale")
           + "\n<!-- generated:gate-list -->\nx\n<!-- /generated:gate-list -->\n")
    assert readme_gen.block_names(text) == ["board-lanes", "gate-list"]


def test_stale_blocks_catches_a_retyped_list(tmp_path):
    text = _doc("board-lanes", "| Lane | Order |\n|---|---|\n| `inbox` | 1 |")
    stale = readme_gen.stale_blocks(tmp_path, text)
    assert [name for name, _ in stale] == ["board-lanes"]


def test_stale_blocks_reports_the_opening_markers_line(tmp_path):
    text = "line one\nline two\n" + _doc("board-lanes", "wrong")
    [(name, line)] = readme_gen.stale_blocks(tmp_path, text)
    assert name == "board-lanes"
    assert text.splitlines()[line - 1] == "<!-- generated:board-lanes -->"


def test_render_regenerates_a_stale_block_until_clean(tmp_path):
    text = _doc("board-lanes", "garbage")
    fresh = readme_gen.render(tmp_path, text)
    assert readme_gen.stale_blocks(tmp_path, fresh) == []
    assert readme_gen.board_lanes() in fresh


def test_render_leaves_free_text_around_a_block_untouched(tmp_path):
    text = "before\n" + _doc("board-lanes", "garbage") + "after\n"
    fresh = readme_gen.render(tmp_path, text)
    assert fresh.startswith("before\n")
    assert fresh.endswith("after\n")


def test_an_unregistered_marker_name_is_left_untouched(tmp_path):
    text = _doc("not-a-real-block", "keep me")
    assert readme_gen.render(tmp_path, text) == text
    assert readme_gen.stale_blocks(tmp_path, text) == []


def test_a_current_block_is_not_stale(tmp_path):
    text = readme_gen.render(tmp_path, _doc("board-lanes", "placeholder"))
    assert readme_gen.stale_blocks(tmp_path, text) == []


def test_board_lanes_names_every_real_lane():
    body = readme_gen.board_lanes()
    for lane in board.LANES:
        assert f"`{lane}`" in body


def test_manifest_fields_lists_every_schema_table():
    """Every schema table gets a row, named the way it is actually written —
    `[[accounts]]` is a repeatable array of tables, not a single `[accounts]`
    table, and a row claiming otherwise would teach a reader the wrong TOML."""
    body = readme_gen.manifest_fields()
    for table in manifest.schema():
        label = "[[accounts]]" if table == "accounts" else f"[{table}]"
        assert f"`{label}`" in body


def test_manifest_fields_marks_a_required_field(tmp_path):
    # `[i18n].adapter` has no default (manifest.py's own I18n dataclass) — the
    # one field in the whole schema this should be true of.
    assert "`adapter` (required)" in readme_gen.manifest_fields()


def test_gate_list_names_every_gate_discover_finds(tmp_path):
    gates = discover(tmp_path)
    body = readme_gen.gate_list(tmp_path)
    for name in gates:
        assert f"`{name}`" in body


def test_gate_list_picks_up_a_projects_own_gate(tmp_path):
    """`gate_list` must generalise past this package's own README: a project
    that adopts the marker convention for its own README should see its own
    `.ai/gates/` entries too, the same as `discover()` does."""
    project_gates = tmp_path / ".ai" / "gates"
    project_gates.mkdir(parents=True)
    (project_gates / "my_local_gate.py").write_text(
        'NAME = "my_local_gate"\n'
        'DESCRIPTION = "a project-only rule"\n'
        "def check(repo_root):\n    return []\n",
        encoding="utf-8",
    )
    body = readme_gen.gate_list(tmp_path)
    assert "`my_local_gate`" in body
    assert "a project-only rule" in body


def test_runner_flags_includes_a_known_flag(tmp_path):
    assert "`--dry-run`" in readme_gen.runner_flags(tmp_path)


def test_runner_flags_falls_back_when_the_repo_has_no_manifest(tmp_path):
    """This package's own repo has no `.ai/manifest.toml` — the case the
    synthetic-manifest fallback exists for. `tmp_path` reproduces it: a fresh
    directory with nothing written into it."""
    assert "`--base`" in readme_gen.runner_flags(tmp_path)


def test_readme_gens_own_readme_is_current():
    """The actual point of the mechanism: this package's committed README.md
    has no drift from a fresh generation right now."""
    root = Path(__file__).resolve().parent.parent
    text = (root / "README.md").read_text(encoding="utf-8")
    assert readme_gen.stale_blocks(root, text) == []
