"""Tests for the `review_verdict_unread` core gate.

Synthetic repo per test, same reasoning as `test_gate_tasks_branch_already_merged.py`:
this gate's own corpus is a live board plus `.ai/runs/`, and pinning a test to a real
one would fail on every legitimate run.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import review_verdict_unread as gate  # noqa: E402

import _fixtures

_MANIFEST = '[branches]\nintegration = "main"\n'
_MANIFEST_NO_BRANCHES = '[project]\nname = "x"\n'


def _run(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _base_repo(tmp_path: Path, *, manifest: str = _MANIFEST) -> Path:
    _fixtures.git_init(tmp_path, branch="main", email="t@t")
    _write(tmp_path / ".ai" / "manifest.toml", manifest)
    _write(tmp_path / "src" / "x.py", "x = 1\n")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def _add_card(root: Path, lane: str, card_id: str) -> None:
    text = f"---\nid: {card_id}\ntitle: {card_id}\nstate: {lane}\n---\n\n## Intent\n\nDo the thing.\n"
    _write(root / "Board" / lane / f"{card_id}.md", text)
    # `.ai/runs/` is gitignored and never committed — the gate reads it straight
    # off disk, so board cards are the only thing that needs a commit here to
    # keep the fixtures consistent with a real checkout.
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", f"board: add {card_id}")


def _write_verdict(root: Path, card_id: str, attempt: int, verdict: dict) -> None:
    path = root / ".ai" / "runs" / card_id / f"attempt-{attempt}" / "review-verdict.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict), encoding="utf-8")


def _write_batch_verdict(root: Path, batch: str, verdict: dict) -> None:
    path = root / ".ai" / "runs" / "_chores" / batch / "review-verdict.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(verdict), encoding="utf-8")


def _cut_branch(root: Path, branch: str) -> None:
    """A branch that exists and is not an ancestor of `main` — the shape a
    dispatch in flight, or a landing that never happened, leaves behind."""
    _run(root, "checkout", "-qb", branch)
    _write(root / "src" / f"{branch.replace('/', '_')}.py", "y = 1\n")
    _run(root, "add", "-A")
    _run(root, "commit", "-qm", f"work on {branch}")
    _run(root, "checkout", "-q", "main")


def _merge_branch(root: Path, branch: str, *, delete: bool = False) -> None:
    _run(root, "merge", "--no-ff", "-q", "-m", f"Merge {branch}", branch)
    if delete:
        _run(root, "branch", "-d", branch)


# --- single-card verdicts ----------------------------------------------------


def test_a_decisive_verdict_left_in_review_is_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "probe")
    _write_verdict(root, "probe", 1, {"verdict": "ok", "notes": "reviewed ok"})
    violations = gate.check(root)
    assert len(violations) == 1
    assert violations[0].file == "Board/review/probe.md"
    assert "ok" in violations[0].rule
    assert "probe" in violations[0].rule


def test_needs_fix_and_needs_decision_are_also_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "fixme")
    _write_verdict(root, "fixme", 1, {"verdict": "needs_fix", "finding": "x"})
    _add_card(root, "review", "askme")
    _write_verdict(root, "askme", 1, {"verdict": "needs_decision", "question": "y"})
    violations = gate.check(root)
    assert {v.file for v in violations} == {
        "Board/review/fixme.md", "Board/review/askme.md",
    }


def test_no_verdict_file_at_all_is_not_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "probe")
    assert gate.check(root) == []


def test_an_unparsable_verdict_is_not_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "probe")
    path = root / ".ai" / "runs" / "probe" / "attempt-1" / "review-verdict.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert gate.check(root) == []


def test_a_card_outside_review_is_not_in_scope(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "tasks", "probe")
    _write_verdict(root, "probe", 1, {"verdict": "ok"})
    assert gate.check(root) == []


def test_an_older_attempts_verdict_is_superseded_by_a_fresh_retry(tmp_path):
    """attempt-1 said needs_fix, the card went back to tasks/, was re-dispatched
    and is now correctly back in review/ awaiting a NEW review — attempt-2 has
    no verdict yet. The stale attempt-1 file must not be read as still open."""
    root = _base_repo(tmp_path)
    _add_card(root, "review", "probe")
    _write_verdict(root, "probe", 1, {"verdict": "needs_fix", "finding": "old"})
    (root / ".ai" / "runs" / "probe" / "attempt-2").mkdir(parents=True)
    assert gate.check(root) == []


def test_only_the_highest_attempt_is_read(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "probe")
    _write_verdict(root, "probe", 1, {"verdict": "needs_fix", "finding": "old"})
    _write_verdict(root, "probe", 2, {"verdict": "ok", "notes": "fixed"})
    violations = gate.check(root)
    assert len(violations) == 1
    assert "wrote `ok`" in violations[0].rule
    assert "wrote `needs_fix`" not in violations[0].rule


# --- batch verdicts ------------------------------------------------------


def test_a_batch_verdict_on_an_unmerged_branch_is_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "chore-one")
    _cut_branch(root, "chores/20260902-0007")
    _write_batch_verdict(root, "20260902-0007", {
        "items": [{"id": "chore-one", "verdict": "ok"}],
    })
    violations = gate.check(root)
    assert len(violations) == 1
    assert violations[0].file == "Board/review/chore-one.md"
    assert "chores/20260902-0007" in violations[0].rule


def test_a_landed_batch_is_not_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "chore-one")
    _cut_branch(root, "chores/20260902-0007")
    _merge_branch(root, "chores/20260902-0007")
    _write_batch_verdict(root, "20260902-0007", {
        "items": [{"id": "chore-one", "verdict": "ok"}],
    })
    assert gate.check(root) == []


def test_a_pruned_batch_branch_is_not_flagged(tmp_path):
    """The ordinary path: the batch branch is deleted once it lands, same as a
    dispatched card's own branch — `git branch -d` runs before anyone looks."""
    root = _base_repo(tmp_path)
    _add_card(root, "review", "chore-one")
    _cut_branch(root, "chores/20260902-0007")
    _merge_branch(root, "chores/20260902-0007", delete=True)
    _write_batch_verdict(root, "20260902-0007", {
        "items": [{"id": "chore-one", "verdict": "ok"}],
    })
    assert gate.check(root) == []


def test_a_batch_item_not_currently_in_review_is_skipped(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "testing", "chore-one")  # already landed elsewhere
    _cut_branch(root, "chores/20260902-0007")
    _write_batch_verdict(root, "20260902-0007", {
        "items": [{"id": "chore-one", "verdict": "ok"}],
    })
    assert gate.check(root) == []


def test_a_non_decisive_batch_entry_is_not_flagged(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "review", "chore-one")
    _cut_branch(root, "chores/20260902-0007")
    _write_batch_verdict(root, "20260902-0007", {
        "items": [{"id": "chore-one", "verdict": ""}],
    })
    assert gate.check(root) == []


def test_no_branches_declared_skips_the_batch_check_but_not_the_single_card_one(tmp_path):
    root = _base_repo(tmp_path, manifest=_MANIFEST_NO_BRANCHES)
    _add_card(root, "review", "probe")
    _write_verdict(root, "probe", 1, {"verdict": "ok"})
    _add_card(root, "review", "chore-one")
    _cut_branch(root, "chores/20260902-0007")
    _write_batch_verdict(root, "20260902-0007", {
        "items": [{"id": "chore-one", "verdict": "ok"}],
    })
    violations = gate.check(root)
    assert [v.file for v in violations] == ["Board/review/probe.md"]


def test_no_review_cards_short_circuits_before_touching_runs(tmp_path):
    root = _base_repo(tmp_path)
    _add_card(root, "tasks", "probe")
    assert gate.check(root) == []
