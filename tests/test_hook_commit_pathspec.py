"""Tests for the `commit_pathspec` PreToolUse hook.

Closes `commit-swept-staged-index` (2026-07-28): `git add A B` followed by a separate
`git commit` was read as "commit only A and B". `git commit` commits the whole index,
which already held three board-card renames Karel had staged in Obsidian, and the commit
carried them along — seven `card_schema` violations absent from the parent.

Real git repositories in `tmp_path` rather than mocks, because the thing under test is
what git actually does with an index. A mocked `git diff --cached` would assert my
belief about git, which is precisely the belief that was wrong.

**Provenance.** Written in Dungeoneer's `tests/` and moved here whole by
`nothing-stops-framework-tests-landing-here` (2026-08-08) — the eleventh mover,
found by the `framework_test_grounding` gate that card built rather than by anyone
reading. It was not in the relocation note's table: every repository it touches is
built in `tmp_path`, and it referenced the origin repo's own tree **zero** times, so
nothing stayed behind.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift.hooks import commit_pathspec

import _fixtures


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", check=False)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"


def _build(root: Path) -> None:
    _fixtures.git_init(root, branch="main", name="T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "--", "seed.txt")
    _git(root, "commit", "-qm", "seed")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _fixtures.repo_copy("pathspec-seed", tmp_path, _build)


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _stage(repo: Path, name: str) -> None:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "--", name)


# --- the incident ---------------------------------------------------------------


def test_the_incident_is_refused(repo):
    """The literal 2026-07-28 shape: mine staged, plus somebody else's already in the
    index, then a bare `git commit`."""
    _stage(repo, "digest.py")            # what the session staged
    _stage(repo, "someone-elses-card.md")  # what Obsidian had already staged
    reason = commit_pathspec.evaluate(_bash('git commit -m "fix digest"'), cwd=repo)
    assert reason is not None
    assert "someone-elses-card.md" in reason, "the denial must list what would be swept"
    assert "digest.py" in reason


def test_the_prescribed_fix_is_allowed(repo):
    """`git commit -- <paths>` is the form the correction prescribes; a guard that also
    refused the cure would just be turned off."""
    _stage(repo, "digest.py")
    _stage(repo, "someone-elses-card.md")
    assert commit_pathspec.evaluate(
        _bash('git commit -m "fix digest" -- digest.py'), cwd=repo) is None


def test_the_compound_add_then_commit_form_is_still_seen(repo):
    """`git add x && git commit` in ONE call is why the wiring carries no `if:` prefix
    filter — a filter keyed on the command's first word never sees this."""
    _stage(repo, "someone-elses-card.md")
    reason = commit_pathspec.evaluate(
        _bash('git add -- mine.txt && git commit -m "x"'), cwd=repo)
    assert reason is not None


# --- what must not be blocked ---------------------------------------------------


def test_an_empty_index_is_allowed(repo):
    """Nothing to sweep. The commit fails on its own terms, which is not this hook's
    business."""
    assert commit_pathspec.evaluate(_bash('git commit -m "x"'), cwd=repo) is None


def test_a_merge_in_progress_is_allowed(repo):
    """git populates the index itself during a merge and rejects a pathspec outright, so
    requiring one would make conflict resolution impossible."""
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "f.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "--", "f.txt")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "f.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "add", "--", "f.txt")
    _git(repo, "commit", "-qm", "main")
    subprocess.run(["git", "merge", "side"], cwd=str(repo), capture_output=True,
                   text=True, encoding="utf-8", errors="replace", check=False)
    assert (repo / ".git" / "MERGE_HEAD").exists(), "fixture did not create a conflict"
    assert commit_pathspec.evaluate(_bash('git commit -m "merge"'), cwd=repo) is None


def test_a_non_commit_bash_call_is_untouched(repo):
    _stage(repo, "x.txt")
    for command in ("git status", "pytest -q", "git add -- x.txt", "ls Board"):
        assert commit_pathspec.evaluate(_bash(command), cwd=repo) is None, command


def test_a_non_bash_tool_is_untouched(repo):
    _stage(repo, "x.txt")
    payload = {"tool_name": "Read", "tool_input": {"file_path": "git commit -m x"}}
    assert commit_pathspec.evaluate(payload, cwd=repo) is None


def test_outside_a_repository_it_fails_open(tmp_path):
    """A guard that wedges a session when it cannot tell is worse than none."""
    assert commit_pathspec.evaluate(_bash('git commit -m "x"'), cwd=tmp_path) is None


# --- the -a hole ----------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "git commit -am x",
    "git commit -a -m x",
    "git commit --all -m x",
    'git commit -a -m "x" -- f.txt',
])
def test_worktree_sweeping_forms_are_refused(repo, command):
    """`-a` sweeps every tracked modification, a wider blast radius than the index
    sweep that caused the incident. Left open it would be the easiest workaround for a
    denial, sitting right next to the thing denied."""
    _stage(repo, "x.txt")
    reason = commit_pathspec.evaluate(_bash(command), cwd=repo)
    assert reason is not None, command
    assert "-a" in reason


@pytest.mark.parametrize("command", [
    'git commit --amend -m "x"',
    'git commit --author="A <a@b.c>" -m "x"',
    'git commit --allow-empty -m "x"',
    'git commit --patch -m "x"',
])
def test_a_long_flag_merely_containing_the_letter_a_is_not_a_sweep(command):
    """`--amend`, `--author`, `--allow-empty` are not `-a`. Asserted on the flag bit
    directly rather than through `evaluate`: with a pathspec in the command the call
    would return None whether or not the sweep matcher works, which is a test that
    passes for the wrong reason."""
    _, sweeps, _ = commit_pathspec._flags(commit_pathspec.commit_segments(command)[0])
    assert sweeps is False, command


@pytest.mark.parametrize("command", ["git commit -am x", "git commit -a -m x",
                                     "git commit --all -m x", "git commit -va -m x"])
def test_the_sweep_bit_is_set_for_every_spelling_of_dash_a(command):
    _, sweeps, _ = commit_pathspec._flags(commit_pathspec.commit_segments(command)[0])
    assert sweeps is True, command


def test_a_path_after_the_pathspec_separator_is_not_read_as_a_flag():
    """Everything after `--` is a path. A file literally named `-a` must not arm the
    sweep matcher."""
    tokens = commit_pathspec.commit_segments('git commit -m "x" -- -a')[0]
    has_pathspec, sweeps, _ = commit_pathspec._flags(tokens)
    assert has_pathspec is True and sweeps is False


# --- parsing --------------------------------------------------------------------


def test_git_dash_C_points_the_index_check_at_the_other_repo(repo, tmp_path):
    """`git -C <path> commit` commits over there, so the index to inspect is over
    there. Checking the cwd's index instead would read a clean tree and allow it."""
    other = tmp_path / "other"
    other.mkdir()
    _fixtures.git_init(other, branch="main", name="T")
    (other / "a.txt").write_text("a\n", encoding="utf-8")
    _git(other, "add", "--", "a.txt")
    reason = commit_pathspec.evaluate(
        _bash(f'git -C {other.as_posix()} commit -m "x"'), cwd=repo)
    assert reason is not None and "a.txt" in reason


def test_the_word_commit_in_a_message_is_not_a_commit_call(repo):
    _stage(repo, "x.txt")
    assert commit_pathspec.evaluate(
        _bash('echo "remember to commit later"'), cwd=repo) is None


def test_an_untrack_in_the_index_is_allowed_because_no_pathspec_can_express_it(repo):
    """`git rm --cached` stages a deletion of a file that stays on disk, and
    `git commit -- <path>` rebuilds that path from the WORKING TREE — so naming it in a
    pathspec silently re-adds the file and undoes the untrack. Observed on 2026-08-01
    untracking `.obsidian/`: the pathspec commit reported success, touched only
    `.gitignore`, and left all 16 files tracked.

    Without this carve-out the guard does not scope the operation, it forbids it."""
    _stage(repo, "keep.txt")
    _git(repo, "commit", "-qm", "add keep.txt", "--", "keep.txt")
    _git(repo, "rm", "-r", "-q", "--cached", "keep.txt")
    assert (repo / "keep.txt").exists(), "the file must survive on disk — that is the case"
    assert commit_pathspec.evaluate(_bash('git commit -m "untrack"'), cwd=repo) is None


def test_a_staged_deletion_of_a_file_really_gone_still_needs_a_pathspec(repo):
    """The carve-out is narrow. `git rm` (no `--cached`) removes the file from disk too,
    and `git commit -- <path>` records that deletion correctly — so the ordinary rule
    still applies and an unrelated staged path must not ride along."""
    _stage(repo, "gone.txt")
    _git(repo, "commit", "-qm", "add gone.txt", "--", "gone.txt")
    _git(repo, "rm", "-q", "gone.txt")
    _stage(repo, "someone-elses.md")
    assert not (repo / "gone.txt").exists()
    assert commit_pathspec.evaluate(_bash('git commit -m "x"'), cwd=repo) is not None


def test_commit_segments_finds_the_call_in_a_chain():
    segments = commit_pathspec.commit_segments('git add -- a && git commit -m "x"; ls')
    assert len(segments) == 1
    assert segments[0][:2] == ["git", "commit"]
