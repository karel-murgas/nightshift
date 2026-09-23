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

from nightshift import review


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

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(), tip)

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

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

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

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "needs_fix"
    assert out["fixed"] == []
    assert "executable content" in out["notes"]
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == tip


def test_a_reworded_docstring_is_prose(landed):
    """`reviewer-may-correct-a-docstring`. A docstring is compiled into the module, so
    a raw code-object comparison refused a reworded one and the correction took a
    lead-tier round-trip to be *typed*. Measured on
    `triage-findings-have-a-shelf-life` (2026-09-16): three findings, all text, two in
    a gate's docstring — $3.44 and a whole extra round to land 29 lines of prose. Each
    side now has its docstrings blanked before the compare, so the wording is compared
    as what it is."""
    root, tree, tip = landed
    (tree / "mod.py").write_text(
        'LIMIT = 5\n\n\ndef f(x):\n    """Add the limit."""\n    return x + LIMIT\n',
        encoding="utf-8")
    head = _commit_in(tree, "review: add a docstring")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "ok"
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == head


def test_a_reworded_module_docstring_is_prose(landed):
    """The module slot, not only the function one — and the one that matters most,
    because a gate's module docstring is where its rationale lives."""
    root, tree, tip = landed
    (tree / "mod.py").write_text(
        '"""What this module is for, corrected."""\nLIMIT = 5\n\n\n'
        'def f(x):\n    return x + LIMIT\n', encoding="utf-8")
    head = _commit_in(tree, "review: correct the module docstring")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "ok"
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == head


def test_a_comment_that_shifts_the_lines_below_it_is_prose(landed):
    """The false refusal the old guard had while its docstring denied having one.
    Comments do not reach `co_code`, but they *do* shift the `co_firstlineno` of every
    nested code object below them, and `co_consts` compares that — so inserting a
    comment line was refused as "executable content". Normalising through
    `ast.unparse` renumbers, so now it is prose, which is what it always was."""
    root, tree, tip = landed
    (tree / "mod.py").write_text(
        "# A whole new line of comment above everything.\n"
        "LIMIT = 5\n\n\ndef f(x):\n    return x + LIMIT\n", encoding="utf-8")
    head = _commit_in(tree, "review: add a clarifying comment")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "ok"
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == head


def test_a_string_that_is_not_in_the_docstring_slot_is_refused(landed):
    """The boundary is the AST, not "does it look like a docstring". An assigned
    string is a value the program uses, however triple-quoted it is, so blanking the
    docstring slot must not reach it."""
    root, tree, tip = landed
    (tree / "mod.py").write_text(
        'LIMIT = 5\n\n\ndef f(x):\n    msg = """Add the limit."""\n'
        '    return x + LIMIT if msg else 0\n', encoding="utf-8")
    _commit_in(tree, "review: 'just a string'")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "needs_fix"
    assert out["fixed"] == []
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == tip


def test_a_behaviour_edit_hidden_behind_a_docstring_reword_is_refused(landed):
    """The case the relaxation must not open: real prose *and* a changed constant in
    one commit. One card, one route — the whole verdict is `needs_fix`."""
    root, tree, tip = landed
    (tree / "mod.py").write_text(
        'LIMIT = 9\n\n\ndef f(x):\n    """Add the limit."""\n    return x + LIMIT\n',
        encoding="utf-8")
    _commit_in(tree, "review: docstring, and quietly the constant")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "needs_fix"
    assert "executable content" in out["notes"]
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == tip


def test_a_file_that_will_not_parse_is_refused(landed):
    """Unknown is not ok — a file the guard cannot normalise is one it cannot vouch
    for, so it refuses rather than waving the commit through."""
    root, tree, tip = landed
    (tree / "mod.py").write_text("LIMIT = 5\n\n\ndef f(x:\n", encoding="utf-8")
    _commit_in(tree, "review: broke the file")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(fixed=["mod.py"]), tip)

    assert out["verdict"] == "needs_fix"
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == tip


def test_a_rewritten_history_is_refused(landed):
    """An amend or a reset would carry the worker's own commits away underneath the
    correction, so the fix must *descend from* the reviewed tip."""
    root, tree, tip = landed
    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=tree, check=True,
                   capture_output=True)
    (tree / "notes.md").write_text("The limit is 5.\n", encoding="utf-8")
    _commit_in(tree, "review: correct it, on the wrong parent")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(), tip)

    assert out["verdict"] == "needs_fix"
    assert "descend" in out["notes"]


def test_claiming_a_fix_without_committing_one_is_refused(landed):
    root, tree, tip = landed
    out = review._land_review_fix(root, tree, "ai/probe", _verdict(), tip)
    assert out["verdict"] == "needs_fix"
    assert "committed nothing" in out["notes"]


def test_a_fix_with_no_finding_is_refused(landed):
    """`finding` is what makes a reviewer-applied fix reviewable after the fact. A
    silent correction is the one thing this must not become."""
    root, tree, tip = landed
    (tree / "notes.md").write_text("The limit is 5.\n", encoding="utf-8")
    _commit_in(tree, "review: silent correction")

    out = review._land_review_fix(root, tree, "ai/probe", _verdict(finding="  "), tip)

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
    assert review._land_review_fix(root, tree, "ai/probe", dict(verdict), tip) == verdict
    assert subprocess.run(["git", "rev-parse", "ai/probe"], cwd=root,
                          capture_output=True, text=True).stdout.strip() == tip
