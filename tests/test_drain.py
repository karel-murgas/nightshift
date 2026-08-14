"""Draining `review/` — the lane whose only exit was a human noticing.

The defect these cover is an absence, so most of them assert that something
*moved*. The four that matter are the ones asserting it did not:

**An artefact-only card is left alone, and costs nothing.** A card whose branch
carries no commit lives in `review/` for a human on purpose. A drain that
re-reviewed it every pass would be a nightly tax on the one card in the lane
nobody asked about — so the assertion is not "it stayed put", it is that no
reviewer was spawned at all.

**A wall stops the pass.** The cards after it must be untouched and *named* as
untouched; "the drain ran and this card is still here" and "the drain never
reached it" are different facts.

**The money rule is per review, not per pass.** A pass that starts with headroom
can lose it partway, and a review cannot be un-started.

**`ok` merges through `settle`.** The whole risk of this feature is a drain that
moves a card to `testing/` while its branch never landed, so the merge commit is
asserted on `base`, not the lane alone.

Real git repositories, real branches, real merges. The Claude CLI is the only
thing stubbed — everything this could get wrong is git or board state.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightshift import board, drain, limits, runner, usage

CARD = """\
---
id: {id}
title: "{id}, waiting on a reviewer"
state: review
tier: worker
worker: code-thread
recipe: none
unattended: true
verify: {verify}
branch: ai/{id}
attempts: 1
created: 2026-08-14
---

## Intent

Change the one obvious thing.

## Acceptance

- machine: the gates are green.

## Summary

Did the thing.
"""

_MANIFEST = """
[project]
name = "myapp"
source_dirs = ["myapp"]

[tests]
dir = "tests"

[board]
root = "Board"

[branches]
integration = "development_team"
stable = "main"

[tiers]
binding_doc = ".claude/memory/ai_team/00_architecture.md"
"""

BASE = "development_team"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _repo(tmp_path: Path, *cards: tuple[str, str]) -> Path:
    """A repo on `development_team` with cards at rest in `review/`.

    Each card gets a real `ai/<id>` branch; whether that branch carries a commit
    is the fixture's second argument, because "has a diff" is the drain's first
    decision about every card it sees.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")

    (root / ".ai").mkdir()
    (root / ".ai" / "manifest.toml").write_text(_MANIFEST, encoding="utf-8")
    (root / ".gitignore").write_text(".ai/runs/\n", encoding="utf-8")
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    (root / "myapp").mkdir()
    (root / "myapp" / "__init__.py").write_text("", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text("def test_smoke():\n    assert True\n",
                                         encoding="utf-8")

    doc = root / ".claude" / "memory" / "ai_team"
    doc.mkdir(parents=True)
    (doc / "00_architecture.md").write_text(
        "```tier-binding\nworker = sonnet\nlead = opus\n```\n", encoding="utf-8")
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    for name in ("code-thread", "code-reviewer"):
        (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

    lane = root / "Board" / LANE_DIR
    lane.mkdir(parents=True)
    for order, (card_id, verify) in enumerate(cards):
        (lane / f"{card_id}.md").write_text(CARD.format(id=card_id, verify=verify),
                                            encoding="utf-8", newline="")

    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    _git(root, "branch", "-M", BASE)
    return root


LANE_DIR = "review"


def _branch_with_a_commit(root: Path, card_id: str, *, empty: bool = False) -> None:
    """Cut `ai/<id>` off the base, optionally with one commit on it.

    `empty=True` is the artefact-only case: the branch exists, and there is
    nothing on it a diff reviewer could read.
    """
    _git(root, "branch", f"ai/{card_id}", BASE)
    if empty:
        return
    tree = root.parent / f"wt-{card_id}"
    _git(root, "worktree", "add", "-q", str(tree), f"ai/{card_id}")
    (tree / "myapp" / f"{card_id}.py").write_text(f"VALUE = '{card_id}'\n",
                                                  encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", f"worker: {card_id}")
    _git(root, "worktree", "remove", "--force", str(tree))


@pytest.fixture(autouse=True)
def _no_meter(monkeypatch):
    """No live usage endpoint in a test; the unmetered snapshot allows, which is
    what a box with no network already does. The money-rule tests override it."""
    monkeypatch.setattr(usage, "read", lambda *a, **k: usage.Snapshot(
        reason="no reading in tests"))


@pytest.fixture(autouse=True)
def _gates_pass(monkeypatch):
    monkeypatch.setattr(runner, "GATE_ARGV", ["python", "-c", "pass"])


@pytest.fixture(autouse=True)
def _serial_pytest(monkeypatch):
    monkeypatch.setattr(runner, "_PYTEST_PARALLEL", ())


@pytest.fixture(autouse=True)
def _leave_the_real_config_alone(monkeypatch):
    monkeypatch.setattr(runner, "ensure_workspace_trusted", lambda root: None)


class _Reviewer:
    """A stubbed Claude CLI that only ever plays the diff reviewer.

    Records the cards it was asked about, because half of what these tests assert
    is that a card was *not* reviewed.
    """

    def __init__(self, verdict: dict | None = None, per_card: dict | None = None):
        self.verdict = verdict if verdict is not None else {"verdict": "ok",
                                                            "notes": "fine"}
        self.per_card = per_card or {}
        self.reviewed: list[str] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
        monkeypatch.setattr(runner, "_run_worker", self)
        return self

    def __call__(self, argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        branch = next(line.split("`")[1] for line in prompt.splitlines()
                      if "`ai/" in line)
        card_id = branch.split("/", 1)[1]
        self.reviewed.append(card_id)
        target = Path(next(line.strip() for line in prompt.splitlines()
                           if line.strip().endswith(".json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.per_card.get(card_id, self.verdict)),
                          encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.02}), "")


def _lane_of(root: Path, card_id: str) -> str:
    card = board.find(root, card_id)
    assert card is not None, f"{card_id} vanished from the board"
    return card.lane


def _merged_into_base(root: Path, card_id: str) -> bool:
    return f"{card_id}.py" in _git(root, "ls-tree", "--name-only", "-r",
                                   BASE, "myapp/").stdout


# ------------------------------------------------------------------ the routing


def test_a_green_card_at_rest_is_reviewed_merged_and_moved_on(tmp_path, monkeypatch):
    """The whole point. Before this, the card in this test stayed in `review/`
    forever and nothing said so."""
    root = _repo(tmp_path, ("a-card", "play"))
    _branch_with_a_commit(root, "a-card")
    reviewer = _Reviewer().install(monkeypatch)

    result = drain.drain(root, BASE)

    assert reviewer.reviewed == ["a-card"]
    assert [o.state for o in result.outcomes] == [drain.REVIEWED]
    assert _lane_of(root, "a-card") == "testing"
    assert _merged_into_base(root, "a-card"), (
        "the card moved to testing/ but its branch never landed — a lane saying "
        "'ready to play' over work that is not on the integration branch")


def test_a_review_card_with_no_surface_lands_in_done(tmp_path, monkeypatch):
    """`verify: review` means the reviewer's ok *is* the acceptance. That routing
    lives in `settle`, and reusing it is how the drain inherits it for free."""
    root = _repo(tmp_path, ("inner", "review"))
    _branch_with_a_commit(root, "inner")
    _Reviewer().install(monkeypatch)

    drain.drain(root, BASE)

    assert _lane_of(root, "inner") == "done"


def test_a_needs_decision_verdict_files_the_question_for_a_human(tmp_path, monkeypatch):
    root = _repo(tmp_path, ("a-card", "play"))
    _branch_with_a_commit(root, "a-card")
    _Reviewer({"verdict": "needs_decision",
               "question": "Should this have touched the save format?"}).install(monkeypatch)

    result = drain.drain(root, BASE)

    assert [o.state for o in result.outcomes] == [drain.NEEDS_DECISION]
    assert _lane_of(root, "a-card") == "needs-decision"
    card = board.find(root, "a-card")
    assert "save format" in board.section(card.text, "Question")
    assert not _merged_into_base(root, "a-card"), "a card awaiting a decision merged"


def test_an_unreadable_verdict_leaves_the_card_where_it_was(tmp_path, monkeypatch):
    """Degrading to 'a human looks' is always safe; guessing a routing is not.
    The drain must not turn a failed review into a lane change."""
    root = _repo(tmp_path, ("a-card", "play"))
    _branch_with_a_commit(root, "a-card")
    _Reviewer({}).install(monkeypatch)

    result = drain.drain(root, BASE)

    assert [o.state for o in result.outcomes] == [drain.LEFT]
    assert _lane_of(root, "a-card") == "review"
    assert not _merged_into_base(root, "a-card")


# --------------------------------------------------- the card that must be left alone


def test_an_artefact_only_card_is_not_reviewed_at_all(tmp_path, monkeypatch):
    """Not 'is left in review/' — *is never spawned for*. A card with no commit on
    its branch is the lane's legitimate resident, and a drain that reviewed it
    would bill for it on every pass, for as long as it waits."""
    root = _repo(tmp_path, ("some-art", "play"))
    _branch_with_a_commit(root, "some-art", empty=True)
    reviewer = _Reviewer().install(monkeypatch)

    result = drain.drain(root, BASE)

    assert reviewer.reviewed == [], "a card with no diff was sent to the reviewer"
    assert [o.state for o in result.outcomes] == [drain.SKIPPED]
    assert _lane_of(root, "some-art") == "review"
    assert result.cost_usd == 0.0


def test_a_card_whose_branch_never_existed_is_treated_the_same_way(tmp_path, monkeypatch):
    """A dead or never-cut branch is the same fact as an empty one: nothing to read."""
    root = _repo(tmp_path, ("orphan", "play"))
    reviewer = _Reviewer().install(monkeypatch)

    result = drain.drain(root, BASE)

    assert reviewer.reviewed == []
    assert [o.state for o in result.outcomes] == [drain.SKIPPED]


def test_a_card_already_reviewed_and_blocked_on_a_merge_is_not_reviewed_again(
        tmp_path, monkeypatch):
    """The other card that legitimately rests in this lane, and the one the first real
    pass found: reviewed `ok`, then its branch would not rebase onto the tip, so
    `settle` sent it back here with `## Merge`. The review is *finished*; the blocker
    is a person. A sweep that re-reviewed it would buy the same verdict at full price
    every time — measured at $0.82 for 94 seconds on the card that exposed this."""
    root = _repo(tmp_path, ("blocked", "review"))
    _branch_with_a_commit(root, "blocked")
    card = root / "Board" / LANE_DIR / "blocked.md"
    card.write_text(card.read_text(encoding="utf-8")
                    + "\n## Merge\n\nReviewed `ok`, but the branch will not rebase.\n",
                    encoding="utf-8", newline="")
    reviewer = _Reviewer().install(monkeypatch)

    result = drain.drain(root, BASE)

    assert reviewer.reviewed == [], "a finished review was bought a second time"
    assert [o.state for o in result.outcomes] == [drain.SKIPPED]
    assert result.cost_usd == 0.0
    assert "--card" in result.outcomes[0].detail, (
        "the skip must name its own override, or it reads as a refusal")


def test_naming_a_blocked_card_reviews_it_anyway(tmp_path, monkeypatch):
    """`--card` is an explicit human request, and after resolving the conflict by hand
    asking for another review is a legitimate thing to want. Same waiver `runner --card`
    makes for `unattended:`, backoff and the attempt limit."""
    root = _repo(tmp_path, ("blocked", "review"))
    _branch_with_a_commit(root, "blocked")
    card = root / "Board" / LANE_DIR / "blocked.md"
    card.write_text(card.read_text(encoding="utf-8")
                    + "\n## Merge\n\nReviewed `ok`, but the branch will not rebase.\n",
                    encoding="utf-8", newline="")
    reviewer = _Reviewer().install(monkeypatch)

    drain.drain(root, BASE, card_id="blocked")

    assert reviewer.reviewed == ["blocked"]


def test_a_second_pass_over_an_artefact_only_card_changes_nothing(tmp_path, monkeypatch):
    """'Not re-attempted' means the second pass is indistinguishable from the
    first — same lane, same file, still nothing spawned."""
    root = _repo(tmp_path, ("some-art", "play"))
    _branch_with_a_commit(root, "some-art", empty=True)
    reviewer = _Reviewer().install(monkeypatch)
    before = (root / "Board" / LANE_DIR / "some-art.md").read_bytes()

    drain.drain(root, BASE)
    drain.drain(root, BASE)

    assert reviewer.reviewed == []
    assert (root / "Board" / LANE_DIR / "some-art.md").read_bytes() == before


# ------------------------------------------------------------------ the money rule


def test_the_money_rule_is_checked_before_every_review_not_once(tmp_path, monkeypatch):
    """A pass that begins with headroom can lose it partway, and the second card
    here must never be dispatched for."""
    root = _repo(tmp_path, ("first", "review"), ("second", "review"))
    for card_id in ("first", "second"):
        _branch_with_a_commit(root, card_id)
    reviewer = _Reviewer().install(monkeypatch)

    calls = {"n": 0}

    def guard(snapshot, *, allow_paid=False, margin_pct=usage.START_MARGIN_PCT):
        calls["n"] += 1
        if calls["n"] == 1:
            return usage.Verdict(True, "plenty")
        return usage.Verdict(False, "plan allowance spent", overridable=True)

    monkeypatch.setattr(usage, "check", guard)

    result = drain.drain(root, BASE)

    assert calls["n"] == 2, "the guard was consulted once for the whole pass"
    assert reviewer.reviewed == ["first"]
    assert [o.state for o in result.outcomes] == [drain.REVIEWED, drain.NOT_REACHED]
    assert _lane_of(root, "second") == "review"
    assert "allowance" in result.stopped


def test_a_wall_part_way_through_stops_the_pass_and_names_what_it_did_not_reach(
        tmp_path, monkeypatch):
    """The remaining cards are untouched — and *said to be* untouched. Silence
    here reads as 'the drain looked at it and left it', which is the exact
    ambiguity this whole card exists to remove."""
    root = _repo(tmp_path, ("first", "review"), ("second", "review"),
                 ("third", "review"))
    for card_id in ("first", "second", "third"):
        _branch_with_a_commit(root, card_id)
    reviewer = _Reviewer().install(monkeypatch)

    real = runner.review_stage

    def walled(root_, card, result, base, card_budget, timeout):
        out = real(root_, card, result, base, card_budget, timeout)
        if card.id != "first":
            return out
        return runner.Dispatch(out.outcome, out.detail, out.cost_usd, out.rounds,
                               limits.Wall(limits.WEEKLY, None, "weekly limit reached"),
                               how_to_test=out.how_to_test)

    monkeypatch.setattr(runner, "review_stage", walled)

    result = drain.drain(root, BASE)

    assert reviewer.reviewed == ["first"]
    assert [(o.card_id, o.state) for o in result.outcomes] == [
        ("first", drain.REVIEWED), ("second", drain.NOT_REACHED),
        ("third", drain.NOT_REACHED)]
    assert _lane_of(root, "second") == "review"
    assert _lane_of(root, "third") == "review"
    assert result.stopped


def test_the_kill_switch_stops_the_pass_before_the_next_card(tmp_path, monkeypatch):
    root = _repo(tmp_path, ("first", "review"), ("second", "review"))
    for card_id in ("first", "second"):
        _branch_with_a_commit(root, card_id)
    reviewer = _Reviewer().install(monkeypatch)

    real = runner.review_stage

    def then_stop(root_, card, *args):
        out = real(root_, card, *args)
        (root / runner.STOP_FILE).parent.mkdir(parents=True, exist_ok=True)
        (root / runner.STOP_FILE).write_text("stop\n", encoding="utf-8")
        return out

    monkeypatch.setattr(runner, "review_stage", then_stop)

    result = drain.drain(root, BASE)

    assert reviewer.reviewed == ["first"]
    assert result.outcomes[-1].state == drain.NOT_REACHED


# ------------------------------------------------------------------ selection


def test_only_the_named_card_is_drained(tmp_path, monkeypatch):
    """The panel's per-row button. Everything else in the lane is untouched."""
    root = _repo(tmp_path, ("first", "review"), ("second", "review"))
    for card_id in ("first", "second"):
        _branch_with_a_commit(root, card_id)
    reviewer = _Reviewer().install(monkeypatch)

    drain.drain(root, BASE, card_id="second")

    assert reviewer.reviewed == ["second"]
    assert _lane_of(root, "first") == "review"


def test_a_card_outside_the_lane_is_not_reviewed_by_name(tmp_path, monkeypatch):
    """`review/` is the precondition, not a hint. A card in `tasks/` wanting a
    review wants a dispatch."""
    root = _repo(tmp_path, ("waiting", "review"))
    (root / "Board" / "tasks").mkdir(parents=True)
    (root / "Board" / "tasks" / "queued.md").write_text(
        CARD.format(id="queued", verify="review").replace("state: review",
                                                          "state: tasks"),
        encoding="utf-8", newline="")
    reviewer = _Reviewer().install(monkeypatch)

    result = drain.drain(root, BASE, card_id="queued")

    assert reviewer.reviewed == []
    assert result.outcomes == []


def test_limit_caps_the_pass_and_the_rest_are_simply_not_in_it(tmp_path, monkeypatch):
    root = _repo(tmp_path, ("first", "review"), ("second", "review"))
    for card_id in ("first", "second"):
        _branch_with_a_commit(root, card_id)
    reviewer = _Reviewer().install(monkeypatch)

    result = drain.drain(root, BASE, limit=1)

    assert reviewer.reviewed == ["first"]
    assert len(result.outcomes) == 1


# ------------------------------------------------------------------ the CLI


def test_dry_run_says_what_would_happen_and_spawns_nothing(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, ("a-card", "review"), ("some-art", "review"))
    _branch_with_a_commit(root, "a-card")
    _branch_with_a_commit(root, "some-art", empty=True)
    reviewer = _Reviewer().install(monkeypatch)

    code = drain.main(["--root", str(root), "--base", BASE, "--dry-run"])
    printed = capsys.readouterr().out

    assert code == 0
    assert reviewer.reviewed == []
    assert "review" in printed and "a-card" in printed
    assert "some-art" in printed and "no commits" in printed
    assert _lane_of(root, "a-card") == "review"


def test_the_command_reports_each_card_and_releases_the_lock(tmp_path, monkeypatch,
                                                             capsys):
    root = _repo(tmp_path, ("a-card", "review"))
    _branch_with_a_commit(root, "a-card")
    _Reviewer().install(monkeypatch)

    code = drain.main(["--root", str(root), "--base", BASE])
    printed = capsys.readouterr().out

    assert code == 0, printed
    assert "a-card" in printed
    assert not (root / runner.LOCK_FILE).exists(), (
        "the lock outlived the pass — the next night would refuse to start")


def test_an_empty_lane_is_a_clean_zero(tmp_path, capsys):
    root = _repo(tmp_path)
    code = drain.main(["--root", str(root), "--base", BASE])
    assert code == 0
    assert "nothing to do" in capsys.readouterr().out


def test_the_command_refuses_while_a_runner_holds_the_lock(tmp_path, monkeypatch):
    """It merges into the integration branch. A night doing the same thing at the
    same time is the one way this could damage something rather than fail."""
    root = _repo(tmp_path, ("a-card", "review"))
    _branch_with_a_commit(root, "a-card")
    reviewer = _Reviewer().install(monkeypatch)
    monkeypatch.setattr(runner, "acquire_lock", lambda r: False)

    assert drain.main(["--root", str(root), "--base", BASE]) == 1
    assert reviewer.reviewed == []
