"""`nightshift.landing` — one path from a finished branch to the card's lane.

What is pinned: `land` does all five duties or none (`chore-landing-skipped-the-memory-
fold` was a landing path that did one of them); `land_batch` does them per card; the
transitions `land` refuses; the inline `boardcmd land` verb; and that no other module
in the package merges into the integration branch.
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

import nightshift
from nightshift import board, boardcmd, landing, preflight, runner

import _runner_helpers  # noqa: F401  (fixtures register by name)
from _runner_helpers import _card, _commit_on_base, _worktree_repo

BASE = "development_team"

REGISTER = """\
# State

## Current State

Newest first.

- **Older thing** (2026-08-20, `older`): one line.
"""

FOLD_ROWS = """
[[memory.fold]]
path = "mem/state.md"
under = "## Current State"
key = "register"
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


def _has_branch(root: Path, name: str) -> bool:
    return runner._git(root, "rev-parse", "--verify", f"refs/heads/{name}").returncode == 0


def _repo(tmp_path: Path, *, player_visible: tuple[str, ...] = ()) -> Path:
    """A repo on `development_team` that folds memory, with an optional
    player-visible prefix declared."""
    root = _worktree_repo(tmp_path)
    manifest = root / ".ai" / "manifest.toml"
    text = manifest.read_text(encoding="utf-8") + FOLD_ROWS
    if player_visible:
        text += "\n[board]\nplayer_visible_paths = [" + ", ".join(
            f'"{p}"' for p in player_visible) + "]\n"
    manifest.write_text(text, encoding="utf-8")
    (root / "mem").mkdir()
    (root / "mem" / "state.md").write_text(REGISTER, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fold targets")
    return root


def _landable(root: Path, tmp_path: Path, card_id: str = "probe", *,
              verify: str = "review", file: str = "feature.py") -> board.Card:
    """An inline card in tasks/, and its branch carrying a file and a fragment."""
    _card(root, "tasks", card_id, verify=verify, kind="inline")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"board: {card_id}")
    tree = tmp_path / f"wt-{card_id}"
    _git(root, "worktree", "add", "-q", "-b", f"ai/{card_id}", str(tree), BASE)
    (tree / file).parent.mkdir(parents=True, exist_ok=True)
    (tree / file).write_text("x = 1\n", encoding="utf-8")
    fragment = tree / ".ai" / "memory-fragments" / f"{card_id}.md"
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text(
        f"## register\n- **{card_id} shipped** (2026-09-23, `{card_id}`): yes.\n",
        encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", f"ai/{card_id}: work and its memory fragment")
    _git(root, "worktree", "remove", "--force", str(tree))
    return board.find(root, card_id)


@pytest.fixture
def root(tmp_path):
    return _repo(tmp_path)


def test_land_does_all_five_duties(root, tmp_path):
    card = _landable(root, tmp_path)
    ok, why = landing.land(root, card, branch="ai/probe", base=BASE,
                           plan=landing.Plan("done"), message="Merge ai/probe")
    assert ok, why
    assert (root / "feature.py").exists(), "the merge"
    assert "probe shipped" in (root / "mem" / "state.md").read_text(encoding="utf-8")
    assert not (root / ".ai" / "memory-fragments" / "probe.md").exists(), "the fold"
    assert not _has_branch(root, "ai/probe"), "the branch cleanup"
    assert board.find(root, "probe").lane == "done", "the lane move"
    assert _git(root, "status", "--porcelain", "--", "Board", "mem") == "", "the commits"


def test_a_failed_merge_touches_nothing(root, tmp_path):
    card = _landable(root, tmp_path, file="shared.py")
    _commit_on_base(root, tmp_path, "shared.py", "y = 2\n")
    before = _git(root, "rev-parse", BASE)
    ok, _ = landing.land(root, card, branch="ai/probe", base=BASE,
                         plan=landing.Plan("done"))
    assert not ok
    assert _git(root, "rev-parse", BASE) == before
    assert _has_branch(root, "ai/probe")
    assert board.find(root, "probe").lane == "tasks"
    assert "probe shipped" not in (root / "mem" / "state.md").read_text(encoding="utf-8")


def test_before_move_runs_only_after_the_merge(root, tmp_path):
    card = _landable(root, tmp_path)
    ran: list[str] = []
    ok, _ = landing.land(root, card, branch="ai/probe", base=BASE,
                         plan=landing.Plan("testing", lambda c: ran.append(c.lane)))
    assert ok and ran == ["tasks"]
    assert board.find(root, "probe").lane == "testing"


def test_a_batch_folds_every_card_it_lands(root, tmp_path):
    """`chore-landing-skipped-the-memory-fold`: the chore batch merged through a path
    that did the merge and none of the bookkeeping."""
    one = _landable(root, tmp_path / "a", "one", file="one.py")
    two = _landable(root, tmp_path / "b", "two", file="two.py")
    _git(root, "branch", "chores/batch", BASE)
    tree = tmp_path / "batch-tree"
    _git(root, "worktree", "add", str(tree), "chores/batch")
    _git(tree, "merge", "--no-ff", "-m", "one", "ai/one")
    _git(tree, "merge", "--no-ff", "-m", "two", "ai/two")
    _git(root, "worktree", "remove", "--force", str(tree))

    ok, why = landing.land_batch(root, "chores/batch", BASE, [
        (one, "ai/one", landing.Plan("done")), (two, "ai/two", landing.Plan("done"))])
    assert ok, why
    state = (root / "mem" / "state.md").read_text(encoding="utf-8")
    assert "one shipped" in state and "two shipped" in state
    assert not _has_branch(root, "ai/one") and not _has_branch(root, "ai/two")
    assert board.find(root, "one").lane == board.find(root, "two").lane == "done"


def test_a_player_visible_change_lands_in_testing_whatever_verify_says(tmp_path):
    root = _repo(tmp_path, player_visible=("game/ui",))
    card = _landable(root, tmp_path, file="game/ui/panel.py")
    assert card.verify == "review"
    assert landing.finished_lane(root, card, "ai/probe", BASE) == "testing"


def test_a_change_off_the_player_visible_paths_keeps_its_lane(tmp_path):
    root = _repo(tmp_path, player_visible=("game/ui",))
    card = _landable(root, tmp_path, file="tools/x.py")
    assert landing.finished_lane(root, card, "ai/probe", BASE) == "done"


def test_a_move_into_review_must_say_why_a_review_is_owed(root):
    _card(root, "tasks", "probe")
    card = board.find(root, "probe")
    with pytest.raises(board.TransitionRefused):
        board.move(root, card, "review")
    moved = board.move(root, card, "review", review_owed="a diff nobody has reviewed yet")
    assert moved.lane == "review"


# --- the inline verb ----------------------------------------------------------


def test_land_verb_refuses_an_unvalidated_branch(root, tmp_path):
    _landable(root, tmp_path)
    with pytest.raises(boardcmd.BoardCommandError, match="preflight"):
        boardcmd.land(root, "probe")
    assert board.find(root, "probe").lane == "tasks"


def test_land_verb_lands_a_validated_branch(root, tmp_path, monkeypatch):
    _landable(root, tmp_path)
    monkeypatch.setattr(preflight, "is_validated", lambda r, sha: True)
    said = boardcmd.land(root, "probe")
    assert "tasks/ → done/" in said
    assert board.find(root, "probe").lane == "done"
    assert not _has_branch(root, "ai/probe")
    assert "Merge ai/probe" in _git(root, "log", "-3", "--format=%s")


def test_land_verb_finishes_a_branch_already_merged_by_hand(root, tmp_path):
    """The stall `boardhealth.merged_but_not_landed` reports: no receipt is needed,
    because nothing new reaches the integration branch."""
    _landable(root, tmp_path)
    _git(root, "merge", "--no-ff", "-m", "by hand", "ai/probe")
    boardcmd.land(root, "probe")
    assert board.find(root, "probe").lane == "done"
    assert not _has_branch(root, "ai/probe")


def test_land_verb_refuses_off_the_integration_branch(root, tmp_path):
    _landable(root, tmp_path)
    _git(root, "checkout", "-q", "-b", "elsewhere")
    with pytest.raises(boardcmd.BoardCommandError, match="integration branch"):
        boardcmd.land(root, "probe")


def test_land_verb_without_a_branch_needs_no_branch_flag(root):
    _card(root, "tasks", "probe", verify="review", kind="inline")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "board")
    with pytest.raises(boardcmd.BoardCommandError, match="--no-branch"):
        boardcmd.land(root, "probe")
    boardcmd.land(root, "probe", no_branch=True)
    assert board.find(root, "probe").lane == "done"


def test_land_verb_merges_a_fix_to_a_landed_card_and_leaves_it_in_its_lane(
        root, tmp_path, monkeypatch):
    _landable(root, tmp_path)
    board.move(root, board.find(root, "probe"), "testing")
    monkeypatch.setattr(preflight, "is_validated", lambda r, sha: True)
    boardcmd.land(root, "probe")
    assert board.find(root, "probe").lane == "testing"
    assert (root / "feature.py").exists() and not _has_branch(root, "ai/probe")
    with pytest.raises(boardcmd.BoardCommandError, match="no fix to merge"):
        boardcmd.land(root, "probe")


def test_land_verb_will_not_file_a_player_visible_change_as_done(tmp_path, monkeypatch):
    root = _repo(tmp_path, player_visible=("game/ui",))
    _landable(root, tmp_path, file="game/ui/panel.py")
    monkeypatch.setattr(preflight, "is_validated", lambda r, sha: True)
    with pytest.raises(boardcmd.BoardCommandError, match="player-visible"):
        boardcmd.land(root, "probe", lane="done")
    boardcmd.land(root, "probe")
    assert board.find(root, "probe").lane == "testing"


# --- stays fixed: no second landing path ----------------------------------------

#: `(module, function)` pairs that run `git merge` *not* into the integration branch.
NOT_INTO_INTEGRATION = {
    ("landing", "_merge"): "the one merge into the integration branch",
    ("chores", "_merge_prefix"): "card branches onto the batch branch, in a worktree",
    ("merge_check", "check_branch"): "a trial merge in a throwaway worktree",
    ("runner", "_merge_with_resolver"): "builds the merge in a worktree; lands via landing",
    ("runner", "_fast_forward"): "moves a card's own branch onto a reviewer's fix",
    ("freshness", "pull"): "fast-forwards the framework checkout from its upstream",
}


def _merge_sites() -> set[tuple[str, str]]:
    """Every `(module, enclosing function)` whose code calls git with a `"merge"`
    argument that is not `--abort`."""
    package = Path(nightshift.__file__).parent
    found: set[tuple[str, str]] = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.relative_to(package).with_suffix("").as_posix().replace("/", ".")
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                words = [a.value for a in node.args if isinstance(a, ast.Constant)]
                if "merge" in words and "--abort" not in words:
                    found.add((module, func.name))
    return found


def test_only_landing_merges_into_the_integration_branch():
    sites = _merge_sites()
    unexpected = sorted(sites - set(NOT_INTO_INTEGRATION))
    assert not unexpected, (
        f"new `git merge` site(s) {unexpected}: a card branch reaches the integration "
        f"branch only through `landing.land`/`land_batch`, which also fold, clean up and "
        f"move the card. If this merge targets something else, add it to "
        f"NOT_INTO_INTEGRATION with the reason.")
    assert ("landing", "_merge") in sites, "the scan no longer sees the real merge"


def test_nothing_outside_landing_calls_its_merge():
    package = Path(nightshift.__file__).parent
    for path in package.rglob("*.py"):
        if path.name == "landing.py":
            continue
        assert "landing._merge" not in path.read_text(encoding="utf-8"), path
