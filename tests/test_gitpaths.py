"""`nightshift.gitpaths` — git's path lists, read the one unambiguous way.

Driven by **real repositories with real awkward filenames**, never by fixture
strings of what git is assumed to print. That assumption is the defect this module
exists for: every reader it replaced was written against a mental model of git's
output that was right for slug-shaped names and wrong for the names a board
actually carries. A test that hand-writes the expected output would encode the
same mental model and pass for the same wrong reason.

Two names do all the work here, and both are real:

* `Stale README.md` — a space, which whitespace-splitting turns into two paths.
* `Animation – attack.md` — an en dash, which git quotes and C-escapes by default,
  so a reader that strips the quotes is left holding a path that does not exist.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nightshift import gitpaths

import _fixtures

SPACED = "Board/inbox/Stale README.md"
DASHED = "Board/inbox/Animation – attack.md"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _repo(tmp_path: Path) -> Path:
    """A repo whose first commit carries both awkward names."""
    root = tmp_path / "repo"
    root.mkdir()
    _fixtures.git_init(root, email="t@t")
    (root / "Board" / "inbox").mkdir(parents=True)
    for rel in (SPACED, DASHED):
        (root / rel).write_text("a note\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


def test_git_still_quotes_and_splits_these_names_which_is_the_whole_point(tmp_path):
    """The premise, asserted rather than assumed: this is what the readers this
    module replaced were reading. If a future git stops doing it, this test is how
    we find out — not by discovering the module was pointless."""
    root = _repo(tmp_path)

    printed = _git(root, "diff", "--name-only", "HEAD~1..HEAD").stdout
    assert printed == "", "no parent; nothing to compare"

    listed = _git(root, "ls-files").stdout
    assert "Stale README.md" in listed
    assert DASHED not in listed, "git quotes a name that is not printable ASCII"
    assert "\\342\\200\\223" in listed, "and C-escapes its bytes"
    assert len(listed.split()) > 2, "and a space splits one path into two"


def test_both_names_survive_a_listing_whole(tmp_path):
    root = _repo(tmp_path)
    assert sorted(gitpaths.tracked(root)) == sorted([DASHED, SPACED])


def test_both_names_survive_a_diff_whole(tmp_path):
    root = _repo(tmp_path)
    (root / SPACED).write_text("edited\n", encoding="utf-8")
    (root / DASHED).write_text("edited\n", encoding="utf-8")
    _git(root, "commit", "-qam", "edit both")

    assert sorted(gitpaths.changed(root, "HEAD~1..HEAD")) == sorted([DASHED, SPACED])


def test_the_first_commit_reports_the_files_it_added(tmp_path):
    """`diff-tree` compares against a parent and the first commit has none, so
    without `--root` this would answer "no paths" for the one commit where every
    path in it is new."""
    root = _repo(tmp_path)
    assert sorted(gitpaths.committed(root)) == sorted([DASHED, SPACED])


def test_a_working_tree_change_is_a_code_and_a_path(tmp_path):
    root = _repo(tmp_path)
    (root / SPACED).write_text("edited\n", encoding="utf-8")

    assert gitpaths.status(root) == [(" M", SPACED)]
    assert gitpaths.dirty(root) is True


def test_a_clean_tree_says_nothing(tmp_path):
    root = _repo(tmp_path)
    assert gitpaths.status(root) == []
    assert gitpaths.dirty(root) is False


def test_a_rename_is_one_change_at_the_path_that_now_exists(tmp_path):
    """`status --porcelain -z` emits the surviving path first and the origin
    second, as two records rather than joined by ` -> ` — a separator a filename
    is free to contain. The origin is dropped rather than reported as a change of
    its own, which is what reporting it twice would amount to."""
    root = _repo(tmp_path)
    _git(root, "mv", SPACED, "Board/inbox/Renamed note.md")

    assert gitpaths.status(root) == [("R ", "Board/inbox/Renamed note.md")]


def test_name_status_orders_a_rename_the_other_way_round(tmp_path):
    """And this is why it is a separate reader: `--name-status -z` puts the status
    first, then the OLD path, then the new one — the opposite order from
    `--porcelain`. Both are reported at the path that exists to be read."""
    root = _repo(tmp_path)
    _git(root, "mv", SPACED, "Board/inbox/Renamed note.md")
    _git(root, "commit", "-qm", "rename")

    assert gitpaths.name_status(root, "HEAD~1..HEAD") == [
        ("R100", "Board/inbox/Renamed note.md")]


def test_name_status_reports_a_deletion_at_its_own_path(tmp_path):
    root = _repo(tmp_path)
    _git(root, "rm", "-q", DASHED)
    _git(root, "commit", "-qm", "delete")

    assert gitpaths.name_status(root, "HEAD~1..HEAD") == [("D", DASHED)]


def test_a_question_git_cannot_answer_is_an_empty_list_not_a_crash(tmp_path):
    """Every caller did this with the old text-splitting form — an unresolvable
    rev produced an empty string and an empty set — and the distinction that
    matters, "cannot tell" against "nothing changed", is resolved by the caller at
    the merge-base rather than here."""
    root = _repo(tmp_path)
    assert gitpaths.changed(root, "no-such-ref..HEAD") == []
    assert gitpaths.name_status(root, "no-such-ref..HEAD") == []


def test_the_empty_blob_is_no_paths_rather_than_one_empty_one(tmp_path):
    assert gitpaths.split("") == []
    assert gitpaths.split("\0") == []
    assert gitpaths.split("one\0two\0") == ["one", "two"]
