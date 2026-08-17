"""The fixture templates must never be the thing a test touches.

`_fixtures` exists because rebuilding a repo per assertion was most of this
suite's runtime. The trade it makes is that one tree is built once and copied,
and the whole trade is only safe while every caller gets a *copy* — a test that
wrote into a shared template would redden some other test, in some other file,
depending on execution order, which is a far worse failure than a slow suite.

So the guarantees are asserted here rather than assumed: two callers never
share a path, writing into one copy cannot be seen from the next, and a
skeleton `.git` carries nothing that names where it was built.
"""
from __future__ import annotations

import pytest

import _fixtures


def _build(target):
    target.mkdir(parents=True)
    (target / "seed.txt").write_text("one\n", encoding="utf-8")


def test_two_callers_get_two_different_trees(tmp_path):
    first = _fixtures.repo_copy("probe-distinct", tmp_path / "a", _build)
    second = _fixtures.repo_copy("probe-distinct", tmp_path / "b", _build)
    assert first != second
    assert first.exists() and second.exists()


def test_writing_into_a_copy_is_invisible_to_the_next_one(tmp_path):
    """The property the whole module rests on."""
    first = _fixtures.repo_copy("probe-isolated", tmp_path / "a", _build)
    (first / "seed.txt").write_text("clobbered\n", encoding="utf-8")
    (first / "extra.txt").write_text("added\n", encoding="utf-8")

    second = _fixtures.repo_copy("probe-isolated", tmp_path / "b", _build)
    assert (second / "seed.txt").read_text(encoding="utf-8") == "one\n"
    assert not (second / "extra.txt").exists()


def test_a_mutated_template_is_refused_rather_than_served(tmp_path):
    """The failure mode this module could introduce, caught where it happens.

    Reaching into the template is not something a caller can do through the
    public functions, so the test does it the only way it can be done at all —
    which is the point: if some future refactor hands a template out, this is
    the assertion that names it instead of a mystery failure three files away.
    """
    _fixtures.repo_copy("probe-guarded", tmp_path / "a", _build)
    template = _fixtures._BUILT["probe-guarded"]
    (template / "seed.txt").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="changed after it was built"):
        _fixtures.repo_copy("probe-guarded", tmp_path / "b", _build)


def test_a_copied_skeleton_records_nothing_about_where_it_was_built(tmp_path):
    """Why `git_init` can be copied at all, and `test_freshness.py` cannot.

    A fresh `.git` holds no absolute path; a clone's config holds the origin's,
    and a cut worktree holds its gitdir's. Those two build genuinely — this
    asserts the reason, so that the exemption stays legible.
    """
    root = _fixtures.git_init(tmp_path / "proj", branch="main")
    config = (root / ".git" / "config").read_text(encoding="utf-8")
    assert "[core]" in config
    assert str(tmp_path) not in config
    assert not list((root / ".git").glob("worktrees"))


def test_the_skeleton_is_a_real_repo_with_the_config_asked_for(tmp_path):
    import subprocess

    root = _fixtures.git_init(tmp_path / "proj", branch="main",
                              name="Alex Rivera", autocrlf="false")
    (root / "file.txt").write_text("one\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True)
    shown = subprocess.run(["git", "-C", str(root), "log", "--format=%an%n%d"],
                           capture_output=True, text=True, check=True).stdout
    assert "Alex Rivera" in shown
    assert "main" in shown


def test_a_different_config_is_a_different_skeleton(tmp_path):
    """Otherwise one cached skeleton would serve every caller's identity."""
    import subprocess

    def author(root):
        return subprocess.run(["git", "-C", str(root), "config", "user.name"],
                              capture_output=True, text=True, check=True).stdout.strip()

    assert author(_fixtures.git_init(tmp_path / "one", name="t")) == "t"
    assert author(_fixtures.git_init(tmp_path / "two", name="T")) == "T"
