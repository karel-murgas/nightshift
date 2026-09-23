"""Tests for the `tasks_branch_already_merged` core gate.

Synthetic repo per test, same reasoning as `test_gate_player_visible_skipped_
testing.py`: this gate's own corpus is a live board, and pinning a test to a
real one would fail on every legitimate card edit.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import tasks_branch_already_merged as gate  # noqa: E402

import _fixtures

_MANIFEST = '[branches]\nintegration = "main"\n'
_MANIFEST_NO_BRANCHES = '[project]\nname = "x"\n'


def _run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _card(card_id: str, branch: str | None, *, kind: str | None = "inline") -> str:
    branch_line = f"branch: {branch}\n" if branch else ""
    kind_line = f"kind: {kind}\n" if kind else ""
    return (f"---\nid: {card_id}\ntitle: {card_id}\nstate: tasks\n{branch_line}"
            f"{kind_line}---\n\n## Intent\n\nDo the thing.\n")


def _base_repo(tmp_path: Path, *, manifest: str = _MANIFEST) -> Path:
    _fixtures.git_init(tmp_path, branch="main", email="t@t")
    _write(tmp_path / ".ai" / "manifest.toml", manifest)
    _write(tmp_path / "src" / "x.py", "x = 1\n")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _merge_branch(root: Path, branch: str, *, delete: bool = False) -> None:
    """Cut `branch`, commit on it, and really `git merge` it into `main` — the
    shape the interactive closing checklist produces."""
    _run(root, "checkout", "-qb", branch)
    _write(root / "src" / f"{branch.replace('/', '_')}.py", "y = 1\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", f"work on {branch}")
    _run(root, "checkout", "-q", "main")
    _run(root, "merge", "--no-ff", "-q", "-m", f"Merge {branch}", branch)
    if delete:
        _run(root, "branch", "-d", branch)


def _add_card(root: Path, lane: str, card_id: str, branch: str | None,
              *, kind: str | None = "inline") -> None:
    _write(root / "Board" / lane / f"{card_id}.md", _card(card_id, branch, kind=kind))
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", f"board: add {card_id}")


# --- what the gate must catch -------------------------------------------------


def test_a_real_merge_left_in_tasks_is_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _merge_branch(root, "ai/probe")
    _add_card(root, "tasks", "probe", "ai/probe")
    violations = gate.check(root)
    assert len(violations) == 1
    assert violations[0].file == "Board/tasks/probe.md"
    assert "ai/probe" in violations[0].rule
    assert "main" in violations[0].rule


def test_the_default_branch_name_is_used_when_the_card_carries_none(tmp_path):
    """`branch:` is runner-written; a card no attempt has touched yet falls
    back to `ai/<id>` — same as `branches.work_branch`."""
    root = _base_repo(tmp_path)
    _merge_branch(root, "ai/probe")
    _add_card(root, "tasks", "probe", None)
    assert len(gate.check(root)) == 1


# --- what it must not -----------------------------------------------------


def test_an_unmerged_branch_is_not_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _run(root, "checkout", "-qb", "ai/probe")
    _write(root / "src" / "probe.py", "y = 1\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", "work in progress")
    _run(root, "checkout", "-q", "main")
    _add_card(root, "tasks", "probe", "ai/probe")
    assert gate.check(root) == []


def test_a_rebased_landing_is_not_flagged(tmp_path):
    """The runner's shape: the branch's own tip never becomes an ancestor of
    `main`, only a copy with a new hash does — `git branch --merged` must not
    see this as a match, or every dispatched card in flight would misfire."""
    root = _base_repo(tmp_path)
    _run(root, "checkout", "-qb", "ai/probe")
    _write(root / "src" / "probe.py", "y = 1\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", "work on ai/probe")
    _run(root, "checkout", "-q", "main")
    # Reproduce the same change as a fresh commit on main, the way a rebase
    # (or squash) merge would land it — never touching ai/probe's own tip.
    _write(root / "src" / "probe.py", "y = 1\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", "Merge ai/probe (rebased copy)")
    _add_card(root, "tasks", "probe", "ai/probe")
    assert gate.check(root) == []


def test_a_deleted_branch_is_not_flagged(tmp_path):
    """The ordinary path: `git branch -d` runs before anyone looks here."""
    root = _base_repo(tmp_path)
    _merge_branch(root, "ai/probe", delete=True)
    _add_card(root, "tasks", "probe", "ai/probe")
    assert gate.check(root) == []


def test_a_dispatched_kind_is_not_flagged_even_if_merged(tmp_path):
    """`kind: chore` (or any non-inline kind) is the runner's lane, and its own
    `settle()` moves a card the moment its merge lands — reproduces the real
    false positive found against this project's board: a retried card whose
    branch was re-cut from `test` and never diverged is trivially an ancestor
    of `test`, with nothing of its own actually merged."""
    root = _base_repo(tmp_path)
    _merge_branch(root, "ai/probe")
    _add_card(root, "tasks", "probe", "ai/probe", kind="chore")
    assert gate.check(root) == []


def test_a_card_with_no_kind_at_all_is_not_flagged(tmp_path):
    """Absent `kind:` means a full (non-chore) card, but still not `inline` —
    the classifier stamps `kind: inline` explicitly, so an absent field must
    not be read as a match."""
    root = _base_repo(tmp_path)
    _merge_branch(root, "ai/probe")
    _add_card(root, "tasks", "probe", "ai/probe", kind=None)
    assert gate.check(root) == []


def test_a_card_outside_tasks_is_not_in_scope(tmp_path):
    root = _base_repo(tmp_path)
    _merge_branch(root, "ai/probe")
    _add_card(root, "done", "probe", "ai/probe")
    assert gate.check(root) == []


def test_no_branches_declared_is_a_project_that_has_no_opinion_yet(tmp_path):
    root = _base_repo(tmp_path, manifest=_MANIFEST_NO_BRANCHES)
    _merge_branch(root, "ai/probe")
    _add_card(root, "tasks", "probe", "ai/probe")
    assert gate.check(root) == []


def test_no_manifest_at_all_is_a_noop(tmp_path):
    _fixtures.git_init(tmp_path, branch="main", email="t@t")
    _write(tmp_path / "src" / "x.py", "x = 1\n")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-qm", "seed")
    _merge_branch(tmp_path, "ai/probe")
    _add_card(tmp_path, "tasks", "probe", "ai/probe")
    assert gate.check(tmp_path) == []


# --- the gate's reach -------------------------------------------------------


def test_only_the_merged_card_is_flagged_among_several(tmp_path):
    root = _base_repo(tmp_path)
    _merge_branch(root, "ai/probe")
    _run(root, "checkout", "-qb", "ai/other")
    _write(root / "src" / "other.py", "z = 1\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", "work on ai/other")
    _run(root, "checkout", "-q", "main")
    _add_card(root, "tasks", "probe", "ai/probe")
    _add_card(root, "tasks", "other", "ai/other")
    violations = gate.check(root)
    assert [v.file for v in violations] == ["Board/tasks/probe.md"]
