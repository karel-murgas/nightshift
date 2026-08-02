"""Tests for the `readme_generated` core gate (07_portability.md §8 D4).

Synthetic repos throughout — the mechanism travels to core, but the marker
convention only ever fires where a README actually opts into it, which today
is this package's own (`test_readme_gen.py::test_readme_gens_own_readme_is_current`
covers that baseline directly).
"""
from __future__ import annotations

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import readme_generated  # noqa: E402


def test_no_readme_is_not_a_violation(tmp_path):
    assert readme_generated.check(tmp_path) == []


def test_a_readme_with_no_generated_markers_is_not_a_violation(tmp_path):
    (tmp_path / "README.md").write_text("# hello\n\nplain prose, no markers.\n",
                                        encoding="utf-8")
    assert readme_generated.check(tmp_path) == []


def test_a_stale_block_is_one_violation_naming_the_block(tmp_path):
    text = ("# hello\n\n<!-- generated:board-lanes -->\nstale content\n"
           "<!-- /generated:board-lanes -->\n")
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    violations = readme_generated.check(tmp_path)
    assert len(violations) == 1
    assert violations[0].file == "README.md"
    assert "board-lanes" in violations[0].rule


def test_a_fresh_block_is_clean(tmp_path):
    from nightshift import readme_gen

    text = readme_gen.render(
        tmp_path,
        "<!-- generated:board-lanes -->\nplaceholder\n<!-- /generated:board-lanes -->\n",
    )
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    assert readme_generated.check(tmp_path) == []


def test_two_stale_blocks_are_two_violations(tmp_path):
    text = (
        "<!-- generated:board-lanes -->\nstale\n<!-- /generated:board-lanes -->\n\n"
        "<!-- generated:manifest-fields -->\nstale\n<!-- /generated:manifest-fields -->\n"
    )
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    violations = readme_generated.check(tmp_path)
    assert {v.rule.split("`")[1] for v in violations} == {"board-lanes", "manifest-fields"}
