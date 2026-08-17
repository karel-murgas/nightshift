"""The chore batch's execution half — worktrees, merges, the two-phase suite runs.

`test_chores.py` covers the decisions this module makes on paper: who is in the
batch, what an item's cost is reported as, where the probe goes. This
covers the part that touches git and spends money, and it is written around the
four ways a batch could quietly do the wrong thing:

**Verification must actually be two-phase.** The entire saving is one full suite
run for the batch instead of one per item. A change that reverted to a per-item
suite run would still be green, still land the same cards, and cost exactly what
this was built to stop costing — so the *count* of full-suite runs is asserted
directly, not inferred from a wall clock.

**The money rule is per dispatch.** A fan-out that checks once at the top and then
spends eight times is the failure `usage.check` exists to prevent, and it is
invisible from the outcome: the batch looks identical either way until the night
it crosses into paid credits.

**Nothing merges past a request for a decision, and nothing merges unreviewed.**
Both are the same rule from opposite ends, and both have to survive a batch being
mostly fine.

**A red batch is attributed, not guessed at.** The bisect exists precisely so that
"one of these eight broke it" never becomes "we dropped the last one".

Real git repositories throughout, because merges, merge commits and branch
deletion are the subject. The Claude CLI is the only thing stubbed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightshift import board, chores, runner, suite, usage

import _fixtures

CARD = """\
---
id: {id}
title: "{title}"
state: tasks
kind: chore
tier: worker
worker: code-thread
recipe: none
unattended: true
verify: {verify}
surface: {surface}
created: 2026-08-14
---

## Intent

Change the one obvious thing.

## Acceptance

- machine: the gates are green.

## Open questions

none
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
forbidden_extra = ["dev", "master"]

[tiers]
binding_doc = ".claude/memory/ai_team/00_architecture.md"
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


# The seeded modules a chore may edit, each with one test that imports it directly.
# One module per chore, because two chores editing the same file is a merge conflict
# and that is a different test.
_MODULES = ("a", "b", "c", "x", "y", "seen", "quiet", "bad")

# The hole `suite.touched` documents and cannot close: a test that reaches the code
# through a string imports nothing that names it, so no import graph can find it.
# It is the reason the batch runs the whole suite, and it is what these fixtures use
# to build a genuine "red only in combination" case.
_REGISTRY_TEST = """\
import importlib


def test_the_plugins_are_intact():
    for name in ("plugin_one", "plugin_two"):
        assert importlib.import_module(f"myapp.{name}").NAME == "ok"
"""


def _repo(tmp_path: Path, *cards: tuple[str, str, str]) -> Path:
    """A committed repo on `development_team` with chore cards in `tasks/`."""
    root = tmp_path / "repo"
    root.mkdir()
    _fixtures.git_init(root, email="t@t")

    (root / ".ai").mkdir()
    (root / ".ai" / "manifest.toml").write_text(_MANIFEST, encoding="utf-8")
    (root / ".gitignore").write_text(".ai/runs/\n", encoding="utf-8")
    (root / "myapp").mkdir()
    tests = root / "tests"
    tests.mkdir()
    for name in _MODULES:
        (root / "myapp" / f"mod_{name}.py").write_text(f"VALUE = 1  # {name}\n",
                                                       encoding="utf-8")
        (tests / f"test_mod_{name}.py").write_text(
            f"from myapp.mod_{name} import VALUE\n\n\ndef test_{name}():\n"
            f"    assert VALUE\n", encoding="utf-8")
    for name in ("plugin_one", "plugin_two"):
        (root / "myapp" / f"{name}.py").write_text('NAME = "ok"\n', encoding="utf-8")
        (tests / f"test_{name}.py").write_text(
            f"from myapp.{name} import NAME\n\n\ndef test_{name}():\n"
            f"    assert NAME\n", encoding="utf-8")
    (tests / "test_registry.py").write_text(_REGISTRY_TEST, encoding="utf-8")

    doc = root / ".claude" / "memory" / "ai_team"
    doc.mkdir(parents=True)
    (doc / "00_architecture.md").write_text(
        "```tier-binding\nworker = sonnet\nlead = opus\n```\n", encoding="utf-8")
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    for name in ("code-thread", "code-reviewer"):
        (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

    lane = root / "Board" / "tasks"
    lane.mkdir(parents=True)
    for card_id, verify, surface in cards:
        (lane / f"{card_id}.md").write_text(
            CARD.format(id=card_id, title=f"{card_id} chore", verify=verify,
                        surface=surface), encoding="utf-8")

    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    _git(root, "branch", "-M", "development_team")
    return root


@pytest.fixture(autouse=True)
def _no_meter(monkeypatch):
    """No live usage endpoint in a test. The default is the unmetered snapshot,
    which `usage.check` answers with an allow — the fail-open behaviour a box
    with no network already has. Tests about the money rule override it."""
    monkeypatch.setattr(usage, "read", lambda *a, **k: usage.Snapshot(
        reason="no reading in tests"))


@pytest.fixture(autouse=True)
def _gates_pass(monkeypatch):
    """A gate harness that always passes, substituted at the argv rather than by
    writing a `run.py` the fixture alone would have."""
    monkeypatch.setattr(runner, "GATE_ARGV", ["python", "-c", "pass"])


@pytest.fixture(autouse=True)
def _serial_pytest(monkeypatch):
    """xdist's startup dwarfs a two-test suite, and these run pytest many times."""
    monkeypatch.setattr(runner, "_PYTEST_PARALLEL", ())
    _fixtures.serial_child_pytest(monkeypatch)


@pytest.fixture(autouse=True)
def _leave_the_real_config_alone(monkeypatch):
    """`ensure_workspace_trusted` edits `~/.claude.json`, which is the developer's
    own file. A test must not leave a dozen temp directories in it."""
    monkeypatch.setattr(runner, "ensure_workspace_trusted", lambda root: None)


class _Worker:
    """A stubbed Claude CLI: one scripted behaviour per card id, plus the reviewer.

    Records what it was asked to do, because half of what these tests assert is
    about the *shape* of the dispatch — which model, how many times, in what order
    — rather than about the diff it produced.
    """

    def __init__(self, *, edits: dict, verdicts: dict | None = None,
                 review: dict | None = None, turns: dict | None = None):
        self.edits = edits                 # card id -> (filename, contents) or None
        self.verdicts = verdicts or {}
        self.review = review if review is not None else {"verdict": "ok", "notes": "fine"}
        self.turns = turns or {}
        self.dispatched: list[str] = []
        self.reviews: list[str] = []
        self.models: list[str] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
        monkeypatch.setattr(runner, "_run_worker", self)
        return self

    def __call__(self, argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        cwd = Path(cwd)
        agent = argv[argv.index("--agent") + 1]
        model = argv[argv.index("--model") + 1]
        self.models.append(model)
        target = Path(next(line.strip() for line in prompt.splitlines()
                           if line.strip().endswith(".json")))
        if agent == runner.REVIEWER_AGENT:
            self.reviews.append(str(cwd))
            target.write_text(json.dumps(self.review), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, json.dumps({}), "")

        card_id = cwd.name
        self.dispatched.append(card_id)
        edit = self.edits.get(card_id)
        if edit is not None:
            name, body = edit
            path = cwd / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            _git(cwd, "add", "-A")
            _git(cwd, "commit", "-qm", f"worker: {card_id}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(
            self.verdicts.get(card_id, {"outcome": "done", "summary": f"did {card_id}",
                                        "how_to_test": f"look at {card_id}"})),
            encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"total_cost_usd": 0.01,
                                 "num_turns": self.turns.get(card_id, 3),
                                 "duration_ms": 60_000}), "")


def _count_full_suite_runs(monkeypatch) -> list[list[str]]:
    """Record every pytest invocation's path arguments, so a test can tell a
    whole-suite run from a narrowed one by what it was pointed at."""
    seen: list[list[str]] = []
    real = runner._run_tests

    def spy(cwd, log, timeout, junit, pytest_args):
        seen.append(list(pytest_args))
        return real(cwd, log, timeout, junit, pytest_args)

    monkeypatch.setattr(runner, "_run_tests", spy)
    return seen


def _touch(name: str, value: int = 2) -> tuple[str, str]:
    """One chore's edit: its own seeded module, which one seeded test imports."""
    return f"myapp/mod_{name}.py", f"VALUE = {value}  # {name}\n"


def _break_plugin(name: str) -> tuple[str, str]:
    """An edit that its own narrow slice passes and the whole suite does not.

    `test_<plugin>.py` imports the module and asserts only that `NAME` is truthy,
    so it stays green; `test_registry.py` reaches the same module through
    `importlib` and checks the value, and no import graph can see that edge. This
    is the documented residual risk of a narrow phase 1, reproduced on purpose.
    """
    return f"myapp/{name}.py", 'NAME = "broken"\n'


# --------------------------------------------------------------- phase 1 shape

def test_a_chore_dispatches_on_the_cheap_tier_as_a_short_alias(tmp_path, monkeypatch):
    """A one-prompter does not want the session's top model. The alias, not a
    dated id: an alias survives a CLI release and a pinned id rots."""
    root = _repo(tmp_path, ("a", "review", "inner"))
    worker = _Worker(edits={"a": _touch("a")}).install(monkeypatch)
    chores.execute(root)
    assert worker.models[:1] == ["sonnet"]


def test_phase_one_runs_only_the_tests_that_reach_the_change(tmp_path, monkeypatch):
    """The narrow slice, and it must be narrow *by selection* rather than by luck:
    the batch's own run is what covers everything else."""
    root = _repo(tmp_path, ("a", "review", "inner"))
    _Worker(edits={"a": _touch("a")}).install(monkeypatch)
    runs = _count_full_suite_runs(monkeypatch)
    chores.execute(root)
    assert runs, "no pytest ran at all"
    assert runs[0] == ["tests/test_mod_a.py"], runs[0]


def test_the_batch_runs_the_whole_suite_exactly_once_for_all_of_them(tmp_path,
                                                                     monkeypatch):
    """The entire saving, asserted as a count. Three chores verified the old way
    would be three full-suite runs; here it is one, and the per-item runs are
    narrow slices rather than the suite."""
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "review", "x"),
                 ("c", "review", "x"))
    _Worker(edits={"a": _touch("a"), "b": _touch("b"), "c": _touch("c")}).install(
        monkeypatch)
    runs = _count_full_suite_runs(monkeypatch)
    code, batch = chores.execute(root)
    assert code == 0, batch
    whole = [r for r in runs if r == ["tests/"]]
    assert len(whole) == 1, runs


def test_the_money_rule_is_checked_before_every_dispatch_not_once(tmp_path,
                                                                  monkeypatch):
    """A batch that starts with headroom can lose it four items in, and a dispatch
    cannot be un-started. A guard consulted once at the top would dispatch all
    three here; this one refuses from the second onwards."""
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "review", "x"),
                 ("c", "review", "x"))
    worker = _Worker(edits={"a": _touch("a"), "b": _touch("b"),
                            "c": _touch("c")}).install(monkeypatch)

    calls = {"n": 0}

    def guard(snapshot, *, allow_paid=False, margin_pct=usage.START_MARGIN_PCT):
        calls["n"] += 1
        if calls["n"] == 1:
            return usage.Verdict(True, "plenty")
        return usage.Verdict(False, "plan allowance spent", overridable=True)

    monkeypatch.setattr(usage, "check", guard)
    _, batch = chores.execute(root)

    assert worker.dispatched == ["a"]
    assert {o.card_id for o in batch.by_state("blocked")} == {"b", "c"}
    assert calls["n"] >= 2, "the guard was consulted once for the whole fan-out"


def test_a_green_chore_lands_however_many_turns_it_took(tmp_path, monkeypatch):
    """A turn count cannot fail a chore, and this is the case that used to.

    There was a 40-turn budget here, and it did two things wrong. Turns count tool
    round-trips, so in a large repo the number measures how many files had to be opened
    to be sure — a property of the codebase, not of the request. And it was read *after*
    the worker finished, when the time was already spent: the only thing a post-hoc cap
    can still do is discard a result that is gated and tested. Runaway protection is the
    wall-clock timeout on the worker's own process, which interrupts while that matters.
    """
    root = _repo(tmp_path, ("a", "review", "x"))
    _Worker(edits={"a": _touch("a")}, turns={"a": 500}).install(monkeypatch)
    _, batch = chores.execute(root)

    outcome = batch.outcomes[0]
    assert outcome.state == "done", "500 turns is a cost, not a verdict"
    assert outcome.turns == 500, "and it is still recorded"
    assert board.find(root, "a").lane == "done"


def test_a_long_green_chore_is_never_left_where_no_reviewer_will_look(tmp_path,
                                                                     monkeypatch):
    """The stranding the turn budget caused, pinned so it cannot come back.

    An overrun used to move the card to `review/` and drop it from the batch. Nothing
    drains that lane on its own — the batch's single review runs over the *merged* diff,
    and both the batch selector and the night take their queue from `tasks/` — so a
    green, gated, tested diff sat there with no reviewer scheduled and no way to notice.
    A chore that goes green must therefore reach a settled lane in the same batch.
    """
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "play", "y"))
    _Worker(edits={"a": _touch("a"), "b": _touch("b")},
            turns={"a": 500, "b": 500}).install(monkeypatch)
    _, batch = chores.execute(root)

    for card_id in ("a", "b"):
        assert board.find(root, card_id).lane in ("done", "testing"), (
            f"{card_id} must not be parked in review/ by a cost measurement")


def test_a_parked_chore_is_a_routing_signal_and_carries_its_question(tmp_path,
                                                                     monkeypatch):
    root = _repo(tmp_path, ("a", "review", "x"))
    _Worker(edits={"a": _touch("a")},
            verdicts={"a": {"outcome": "parked", "summary": "which one?"}}).install(
        monkeypatch)
    _, batch = chores.execute(root)
    assert batch.outcomes[0].state == "bounced"
    assert board.find(root, "a").lane == "needs-decision"


def test_a_failed_chore_retires_on_its_single_attempt(tmp_path, monkeypatch):
    """One attempt, and the retirement has to agree with the selection: a chore
    that selection would refuse to re-queue but that stayed in `tasks/` is a card
    nothing will ever pick up and nothing will ever report."""
    root = _repo(tmp_path, ("a", "review", "x"))
    _Worker(edits={"a": ("tests/test_broken.py",
                         "def test_broken():\n    assert False\n")}).install(monkeypatch)
    _, batch = chores.execute(root)
    assert batch.outcomes[0].state == "parked"
    card = board.find(root, "a")
    assert card.lane == "failed" and card.attempts == chores.MAX_ATTEMPTS


# ----------------------------------------------------------------- landing


def test_a_landed_batch_routes_each_card_by_its_own_verify_field(tmp_path,
                                                                 monkeypatch):
    """`play` has a surface to exercise and lands in `testing/` with a scenario;
    `review` has none, so the gates and the suite were its acceptance and it goes
    straight to `done/`. That is what keeps the checklist short."""
    root = _repo(tmp_path, ("seen", "play", "hub"), ("quiet", "review", ""))
    _Worker(edits={"seen": _touch("seen"), "quiet": _touch("quiet")}).install(monkeypatch)
    code, _ = chores.execute(root)

    assert code == 0
    assert board.find(root, "seen").lane == "testing"
    assert board.find(root, "quiet").lane == "done"
    assert "look at seen" in board.find(root, "seen").text


def test_a_landed_chore_has_its_branch_deleted(tmp_path, monkeypatch):
    """A branch is deleted when its work is merged. `-d`, not `-D`: the safe form
    refuses anything not actually merged, which is the check wanted here."""
    root = _repo(tmp_path, ("a", "review", "x"))
    _Worker(edits={"a": _touch("a")}).install(monkeypatch)
    chores.execute(root)
    assert _git(root, "rev-parse", "--verify", "ai/a").returncode != 0


def test_the_batch_actually_reaches_the_integration_branch(tmp_path, monkeypatch):
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "review", "x"))
    _Worker(edits={"a": _touch("a"), "b": _touch("b")}).install(monkeypatch)
    chores.execute(root)
    listed = _git(root, "ls-tree", "-r", "--name-only", "development_team").stdout
    assert "myapp/mod_a.py" in listed and "myapp/mod_b.py" in listed


def test_the_checklist_is_written_and_holds_only_what_needs_an_eye(tmp_path,
                                                                   monkeypatch):
    root = _repo(tmp_path, ("seen", "play", "hub"), ("quiet", "review", ""))
    _Worker(edits={"seen": _touch("seen"), "quiet": _touch("quiet")}).install(monkeypatch)
    chores.execute(root)
    text = (root / chores.OUT).read_text(encoding="utf-8")
    checklist = text.split("## Check these")[1].split("\n## ")[0]
    assert "seen chore" in checklist and "quiet chore" not in checklist
    assert "### hub" in checklist


# -------------------------------------------------------------- one review


def test_the_batch_is_reviewed_once_over_the_whole_diff(tmp_path, monkeypatch):
    """Not once per item — and not only as an economy: the reviewer's question for
    a chore is answerable across the set, and a reviewer holding the combined diff
    can see an interaction between two items that per-item review cannot."""
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "review", "x"),
                 ("c", "review", "x"))
    worker = _Worker(edits={"a": _touch("a"), "b": _touch("b"),
                            "c": _touch("c")}).install(monkeypatch)
    chores.execute(root)
    assert len(worker.reviews) == 1


def test_the_review_is_told_about_every_card_in_the_batch(tmp_path, monkeypatch):
    """An unattributed verdict over a combined diff is not actionable, so each
    item's own criteria and intent go across, numbered and named."""
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "review", "x"))
    _Worker(edits={"a": _touch("a"), "b": _touch("b")}).install(monkeypatch)
    chores.execute(root)
    prompts = list((root / ".ai" / "runs" / "_chores").rglob("review-prompt.md"))
    assert len(prompts) == 1
    text = prompts[0].read_text(encoding="utf-8")
    assert "`a`" in text and "`b`" in text


def test_a_needs_decision_stops_the_whole_batch_from_merging(tmp_path, monkeypatch):
    """The price of reviewing a batch as one unit, paid deliberately: merging past
    a request for a decision is the one thing no stage may do, and splitting the
    batch on the reviewer's say-so would mean guessing which items it meant."""
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "review", "x"))
    _Worker(edits={"a": _touch("a"), "b": _touch("b")},
            review={"verdict": "needs_decision", "question": "which colour?"}).install(
        monkeypatch)
    code, _ = chores.execute(root)

    assert code == 4
    assert "mod_a" not in _git(root, "show", "development_team:myapp/mod_a.py").stdout
    for card_id in ("a", "b"):
        card = board.find(root, card_id)
        assert card.lane == "review"
        assert "which colour?" in card.text
    # The branch survives, so the answer is a merge by hand rather than lost work.
    branches = _git(root, "for-each-ref", "--format=%(refname:short)",
                    "refs/heads/chores/").stdout
    assert branches.strip()


def test_nothing_merges_without_a_review(tmp_path, monkeypatch):
    """A green suite is not acceptance on its own. When the reviewer cannot be
    reached the batch waits for a human rather than landing unreviewed."""
    root = _repo(tmp_path, ("a", "review", "x"))
    _Worker(edits={"a": _touch("a")}, review={}).install(monkeypatch)
    code, _ = chores.execute(root)
    assert code == 4
    assert board.find(root, "a").lane == "review"


# ------------------------------------------------------------------ red batches


def test_a_red_batch_is_attributed_by_bisecting_and_the_rest_still_land(
        tmp_path, monkeypatch):
    """The optimistic scheme's price, and the reason it is affordable: the merge
    commits are already there, one per chore, so "which one broke it" is a binary
    search rather than a guess or a blanket rejection of the batch."""
    root = _repo(tmp_path, ("a", "review", "x"), ("bad", "review", "x"),
                 ("c", "review", "x"))
    _Worker(edits={"a": _touch("a"), "bad": _break_plugin("plugin_one"),
                   "c": _touch("c")}).install(monkeypatch)

    code, batch = chores.execute(root)

    assert code == 0
    assert {o.card_id for o in batch.by_state("parked")} == {"bad"}
    assert "bisect" in next(o.detail for o in batch.outcomes if o.card_id == "bad")
    assert board.find(root, "a").lane == "done"
    assert board.find(root, "c").lane == "done"
    assert board.find(root, "bad").lane == "failed"


def test_a_batch_that_reddens_twice_is_handed_over_rather_than_narrowed_further(
        tmp_path, monkeypatch):
    """A second red is evidence about *routing* — items are reaching the batch
    that are not chores — and more bisecting does not answer that. Two items that
    each pass their own narrow slice and each break the same whole-suite test."""
    root = _repo(tmp_path, ("x", "review", "x"), ("y", "review", "x"))
    _Worker(edits={"x": _break_plugin("plugin_one"),
                   "y": _break_plugin("plugin_two")}).install(monkeypatch)
    code, batch = chores.execute(root)

    assert code == 4
    assert not batch.survivors, "a batch whose every item reddens must land nothing"
    assert {o.card_id for o in batch.by_state("parked")} == {"x", "y"}
    assert "ok" in _git(root, "show", "development_team:myapp/plugin_one.py").stdout


def test_a_branch_that_will_not_merge_is_dropped_with_the_reason(tmp_path,
                                                                 monkeypatch):
    """Never left half-applied and never silently absent — the same rule
    `select` follows for a card left out of the batch."""
    root = _repo(tmp_path, ("a", "review", "x"), ("b", "review", "x"))
    _Worker(edits={"a": ("myapp/clash.py", "VALUE = 'a'\n"),
                   "b": ("myapp/clash.py", "VALUE = 'b'\n")}).install(monkeypatch)

    _, batch = chores.execute(root)
    dropped = [o for o in batch.outcomes if o.state == "parked"]
    assert len(dropped) == 1
    assert "merge" in dropped[0].detail


# --------------------------------------------------------------- the bisect itself


def _scripted_bisect(order: list[str], culprit: str) -> tuple[str | None, int]:
    """Drive `chores.bisect`'s search with a scripted verifier: a prefix is red
    exactly when it contains `culprit`. Returns `(named, probes spent)`."""
    probes = {"n": 0}

    def merge(work, tree, base, through):
        probes["through"] = list(through)
        return list(through), []

    def verify(work, tree, out_dir, tag, timeout):
        probes["n"] += 1
        return culprit not in probes["through"], "red", 0

    import types
    fake = types.SimpleNamespace(_merge_prefix=merge, _verify_tree=verify)
    saved = chores._merge_prefix, chores._verify_tree
    chores._merge_prefix, chores._verify_tree = fake._merge_prefix, fake._verify_tree
    try:
        named = chores.bisect(Path("."), Path("."), "b", "base", order, Path("."), 1)
    finally:
        chores._merge_prefix, chores._verify_tree = saved
    return named, probes["n"]


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8, 13])
def test_the_bisect_names_the_culprit_wherever_it_sits_and_terminates(size):
    """Every position, every size — the loop shrinking on each step is the whole
    of its termination argument, and the two-item case is where a midpoint probe
    would sit on the last suspect and never narrow."""
    order = [f"c{n}" for n in range(size)]
    for culprit in order:
        named, probes = _scripted_bisect(order, culprit)
        assert named == culprit, (size, culprit)
        assert probes <= size, (size, culprit, probes)
