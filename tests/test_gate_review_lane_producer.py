"""Tests for the `review_lane_producer` core gate.

Synthetic trees, for the reason every gate test in this suite gives: a test pinned
to a real project's source fails whenever someone legitimately edits it, and a gate
whose test is muted is worse than no gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import review_lane_producer  # noqa: E402


def _tree(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    (tmp_path / ".ai").mkdir(exist_ok=True)
    (tmp_path / ".ai" / name).write_text(body, encoding="utf-8", newline="")
    return tmp_path


def _lines(tmp_path: Path) -> list[int]:
    return sorted(v.line for v in review_lane_producer.check(tmp_path))


# --- what the gate must catch ------------------------------------------------

def test_a_review_move_with_no_appeal_is_flagged(tmp_path):
    root = _tree(
        tmp_path,
        'def f(root, card, board):\n'
        '    board.move(root, card, "review")\n')
    assert _lines(root) == [2]


def test_the_keyword_spelling_is_caught_too(tmp_path):
    root = _tree(
        tmp_path,
        'def f(root, card, board):\n'
        '    board.move(root, card, to_lane="review")\n')
    assert _lines(root) == [2]


def test_a_second_producer_added_without_reasoning_is_caught(tmp_path):
    """The exact shape of the residual gap: a producer other than the one that
    earned the original fix, reaching review/ by a road nothing asserted anything
    about."""
    root = _tree(
        tmp_path,
        'def _hand_over(work, card, board):\n'
        '    board.move(work, card, "review")\n'
        '    return "handed over"\n')
    assert _lines(root) == [2]


# --- what it must not ---------------------------------------------------------

def test_an_appeal_on_the_line_clears_it(tmp_path):
    root = _tree(
        tmp_path,
        'def f(root, card, board):\n'
        '    # gate-ok(review_lane_producer): reached only after review_stage\n'
        '    # already asserted owed-and-obtainable for this exact dispatch\n'
        '    board.move(root, card, "review")\n')
    assert _lines(root) == []


def test_an_appeal_naming_another_gate_does_not_count(tmp_path):
    root = _tree(
        tmp_path,
        'def f(root, card, board):\n'
        '    # gate-ok(git_path_lists): wrong gate for this defect\n'
        '    board.move(root, card, "review")\n')
    assert _lines(root) == [3]


@pytest.mark.parametrize("call", [
    'board.move(root, card, "tasks")',
    'board.move(root, card, "done")',
    'board.move(root, card, board.BLOCKED_LANE)',
    'board.move(root, card, lane)',
])
def test_a_move_to_another_lane_is_left_alone(tmp_path, call):
    root = _tree(tmp_path, f"def f(root, card, board, lane):\n    {call}\n")
    assert _lines(root) == []


def test_an_unrelated_move_call_is_left_alone(tmp_path):
    """`.move` is matched on shape, not on an import path — but a call that
    mentions "review" nowhere in its arguments is not this gate's business."""
    root = _tree(
        tmp_path,
        'def f(sprite, x, y):\n'
        '    sprite.move(x, y)\n')
    assert _lines(root) == []
