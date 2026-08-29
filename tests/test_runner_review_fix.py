"""Tests for the reviewer applying a prose-only fix itself (`_land_review_fix`).

The reviewer is forbidden to edit everywhere else, and relaxing that needs a reason
this size: a census of every `needs_fix` on Dungeoneer's record (2026-08-29) found
**11 of 14 were false prose, not defective code** — a number re-tuned after the
sentence was written, a symbol the diff deleted still cited in a recipe, a comment
naming a mechanism the change replaced. In each the reviewer had already read the
tree, verified the defect and written the correct replacement out in full, and the
finding then went to a fresh worker attempt whose whole job was to type it in. That
round-trip is ~44% of everything the board has spent.

So the interesting tests here are not the happy path. They are the refusals: the
guard's entire value is that a reviewer which edits *behaviour* cannot get it past,
and that every refusal degrades to the ordinary `needs_fix` round-trip rather than
to a lost correction or a silent merge.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift import runner


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "T")
    (root / "mod.py").write_text("LIMIT = 5\n\n\ndef f(x):\n    return x + LIMIT\n",
                                 encoding="utf-8")
    (root / "notes.md").write_text("The limit is 4.\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "base")
    run("git", "checkout", "-qb", "ai/probe")
    # The worker's own commit, so the branch has history under the reviewer's —
    # a fixture with only the base commit cannot express "built on the wrong
    # parent", which is one of the refusals that matters most.
    (root / "feature.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "probe: the worker's work")
    return root


def _commit_in(tree: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=tree, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=tree, check=True,
                   capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tree,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def landed(tmp_path):
    """A repo with `ai/probe` reviewed, plus a detached checkout of it for the
    reviewer to work in — the real shape, since `review_branch` hands the reviewer
    a `--detach` worktree whose commits reach no branch until this function moves
    one."""
    root = _repo(tmp_path)
    tip = subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                         capture_output=True, text=True).stdout.strip()
    tree = tmp_path / "reviewtree"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(tree), "ai/probe"],
                   cwd=root, check=True, capture_output=True)
    return root, tree, tip


def _verdict(**kw) -> dict:
    out = {"verdict": "ok", "fixed": ["notes.md"],
           "finding": "notes.md said 4; the constant is 5.", "notes": ""}
    out.update(kw)
    return out


# --- the case it exists for ----------------------------------------------------


def test_a_markdown_only_fix_lands_on_the_branch(landed):
    root, tree, tip = landed
    (tree / "notes.md").write_text("The limit is 5.\n", encoding="utf-8")
    head = _commit_in(tree, "review: correct the limit in notes.md")

    out = runner._land_review_fix(root, tree, "ai/probe", _verdict(), tip)

    assert out["verdict"] == "ok"
    moved = subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                           capture_output=True, text=True).stdout.strip()
    assert moved == head, "the branch was not fast-forwarded onto the fix"


def test_a_comment_only_change_to_python_is_prose(landed):
    """Comments do not reach bytecode, so the guard lets them through — this is the
    `inside-man-prehacked-rework` shape, two comments naming a replaced mechanism."""
    root, tree, tip = landed
    (tree / "mod.py").write_text(
        "LIMIT = 5  # the real ceiling\n\n\ndef f(x):\n    return x + LIMIT\n",
        encoding="utf-8")
    head = _commit_in(tree, "review: correct a comment")

    out = runner._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "ok"
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == head


# --- the refusals, which are the point -----------------------------------------


def test_a_changed_constant_is_refused_and_becomes_needs_fix(landed):
    """The whole reason the guard exists. A reviewer that edits behaviour has had
    nobody review *it*, so the card must take the ordinary round-trip instead."""
    root, tree, tip = landed
    (tree / "mod.py").write_text("LIMIT = 9\n\n\ndef f(x):\n    return x + LIMIT\n",
                                 encoding="utf-8")
    _commit_in(tree, "review: 'fix' the constant")

    out = runner._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "needs_fix"
    assert out["fixed"] == []
    assert "executable content" in out["notes"]
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == tip


def test_a_changed_docstring_is_refused(landed):
    """Conservative on purpose: a docstring is compiled into the module, so it reads
    as code here. A refused docstring correction costs one round-trip; a wrongly
    admitted behavioural edit costs a silently altered program."""
    root, tree, tip = landed
    (tree / "mod.py").write_text(
        'LIMIT = 5\n\n\ndef f(x):\n    """Add the limit."""\n    return x + LIMIT\n',
        encoding="utf-8")
    _commit_in(tree, "review: add a docstring")

    out = runner._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "needs_fix"


def test_a_rewritten_history_is_refused(landed):
    """An amend or a reset would carry the worker's own commits away underneath the
    correction, so the fix must *descend from* the reviewed tip."""
    root, tree, tip = landed
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=tree, check=True,
                   capture_output=True)
    (tree / "notes.md").write_text("The limit is 5.\n", encoding="utf-8")
    _commit_in(tree, "review: correct it, on the wrong parent")

    out = runner._land_review_fix(root, tree, "ai/probe", _verdict(), tip)

    assert out["verdict"] == "needs_fix"
    assert "descend" in out["notes"]


def test_claiming_a_fix_without_committing_one_is_refused(landed):
    root, tree, tip = landed
    out = runner._land_review_fix(root, tree, "ai/probe", _verdict(), tip)
    assert out["verdict"] == "needs_fix"
    assert "committed nothing" in out["notes"]


def test_a_fix_with_no_finding_is_refused(landed):
    """`finding` is what makes a reviewer-applied fix reviewable after the fact. A
    silent correction is the one thing this must not become."""
    root, tree, tip = landed
    (tree / "notes.md").write_text("The limit is 5.\n", encoding="utf-8")
    _commit_in(tree, "review: silent correction")

    out = runner._land_review_fix(root, tree, "ai/probe", _verdict(finding="  "), tip)

    assert out["verdict"] == "needs_fix"
    assert "no finding" in out["notes"]


# --- the untouched paths -------------------------------------------------------


@pytest.mark.parametrize("verdict", [
    {"verdict": "ok", "fixed": [], "finding": ""},
    {"verdict": "needs_fix", "fixed": [], "finding": "something real"},
    {"verdict": "needs_decision", "fixed": [], "finding": "", "question": "which?"},
])
def test_an_ordinary_verdict_passes_through_untouched(landed, verdict):
    """No `fixed`, no involvement. The common case must not be reshaped by a path
    it never takes."""
    root, tree, tip = landed
    assert runner._land_review_fix(root, tree, "ai/probe", dict(verdict), tip) == verdict
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == tip
