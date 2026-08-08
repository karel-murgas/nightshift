"""Tests for `runner.py`, `board.py` and `tiers` — the dispatch loop.

The runner is the one component that runs with nobody watching, so the things
worth testing hard are the ones whose failure is invisible until morning:

* **Selection** — every reason a card is *not* dispatched. A runner that
  silently skips work looks exactly like a quiet night, and telling those two
  apart is the first question anyone asks.
* **Crash recovery** — `attempts` is committed before the worker starts, so a
  reboot loop is bounded. This is the property the whole "resumable from disk
  alone" claim rests on.
* **The tier binding** — it is parsed out of the document `[tiers].binding_doc`
  names precisely so it cannot be restated in code, and a test that hard-coded a
  model alias would reintroduce the second copy the design exists to prevent. So
  the tests assert the *mechanism* (a card resolves to whatever the block says)
  and the invariants (every `card_schema` tier resolves; a missing block raises
  rather than guessing), never the value.
* **The forbidden bases** — an unattended process is exactly who would commit to
  a branch it must never build on.

No test here spawns a worker. The dispatch call is a subprocess boundary; what
matters on this side of it is that every outcome settles the board correctly,
which is tested directly against `settle()`.

Every fixture below builds its own `tmp_path` project — a git repo, a manifest,
a `Board/` and a charter directory — so nothing here reads a real tree. The
manifests say `name = "dungeoneer"` and declare `dungeoneer/assets/.tmp` as a
harvest directory because that is the project the incidents happened in; the
value is fixture content, and the tests prove the runner reads it from the
manifest rather than knowing the word.

**Provenance.** This file was written in Dungeoneer's `tests/` between
2026-07-23 and 2026-08-08 and moved here whole by
`framework-tests-live-in-the-wrong-repo`. The ~9 assertions it made about *that*
repo's real board, `hosts.json` and tier-binding document stayed behind, in a
file of the same name: they are claims about a project, and a claim about a
project asserts nothing once it is read against the framework.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nightshift import board
from nightshift import limits
from nightshift import worker_prompt
from nightshift import night
from nightshift import run_record
from nightshift import runner
from nightshift import stale_sweep
from nightshift import tiers
from nightshift.hooks import worktree_fence


# The runner's own source, for the AST guards below. Read off the installed
# module rather than `.ai/runner.py`: 07_portability.md §8 step 4 moved it into
# the package, and a guard that reads a path instead of the module it is about
# is the `fixture-created-the-file-production-lacked` shape.
_RUNNER_SOURCE = Path(runner.__file__)


CARD = """\
---
id: {id}
title: "{id} probe"
state: {lane}
tier: {tier}
worker: {worker}
recipe: none
unattended: {unattended}
verify: {verify}
created: 2026-07-23
{extra}---

## Intent

A probe card.

## Approach

Probe the thing directly, since that is what the card is for.

## Acceptance

- machine: the gates are green.

## Open questions

none
"""


def _card(root: Path, lane: str, card_id: str, *, tier: str = "worker",
          worker: str = "code-thread", unattended: str = "true",
          verify: str = "play", **extra: str) -> Path:
    lane_dir = root / "Board" / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    path = lane_dir / f"{card_id}.md"
    path.write_text(
        CARD.format(id=card_id, lane=lane, tier=tier, worker=worker,
                    unattended=unattended, verify=verify,
                    extra="".join(f"{k}: {v}\n" for k, v in extra.items())),
        encoding="utf-8")
    return path


def _charter(root: Path, name: str) -> None:
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    """A git repo with a board, so `git mv` and the commits are real."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "Board").mkdir(exist_ok=True)
    # A manifest, because the fixture is a *consumer* of one. `[branches]` moved
    # out of `.ai/branches.py` in 07_portability.md §8 step 4, and the step's own
    # checklist names this trap: a hardcoded default moved behind a manifest field
    # changes behaviour for every caller that supplies no config, and a synthetic
    # fixture is exactly such a caller. Without this, `runner.preflight` raises
    # ManifestError instead of judging the base branch.
    _write_manifest(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)
    return tmp_path


def _write_manifest(root: Path, integration: str = "development_team",
                    stable: str = "main",
                    forbidden_extra: tuple[str, ...] = ("dev", "master")) -> None:
    """The tables the runner reads. `[branches]` is parameterised so a test can
    simulate the branch-role migration by writing a different one, which is what
    the swap used to be tested by monkeypatching a module constant.

    `[worker]` mirrors this repo's real manifest because the dispatch fixtures
    below write art candidates into `dungeoneer/assets/.tmp` and expect
    `harvest()` to rescue them. Since 07_portability.md §8 step 4 that path is
    declared rather than hardcoded, so a fixture that omitted it would report
    "the worker produced nothing" — the exact confusing failure `harvest` exists
    to prevent, reproduced by the config rather than by the code.
    """
    extra = ", ".join(f'"{b}"' for b in forbidden_extra)
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "manifest.toml").write_text(
        f'[project]\nname = "dungeoneer"\nsource_dirs = ["dungeoneer"]\n\n'
        f'[branches]\nintegration = "{integration}"\nstable = "{stable}"\n'
        f"forbidden_extra = [{extra}]\n\n"
        f'[worker]\nharvest_dirs = ["dungeoneer/assets/.tmp"]\n'
        f'fence_env = "DUNGEONEER_FENCE_ALLOW"\n\n'
        # Declared, because the core default stopped being this project's plan doc
        # on 2026-08-02: it was `.claude/plans/ai_team/00_architecture.md` (the docs
        # moved to `.claude/memory/ai_team/` on 2026-08-06), a path
        # no other repo has — the `deferral-note-nobody-collected` coupling, fixed
        # in the *message* at step 5 and left in the *value*. A fixture that writes
        # the plan doc and relies on the default is exactly the "hardcoded default
        # moved behind a manifest field" caller §8 step 4's checklist warns about,
        # and this is the fourth time that item has fired.
        f'[tiers]\nbinding_doc = ".claude/memory/ai_team/00_architecture.md"\n',
        encoding="utf-8")


def _ignore_runs(root: Path) -> None:
    """Match production's `.gitignore:42` (`.ai/runs/`). Needed only by tests
    that write a run-dir file directly into the fixture and then call the real
    `runner.run()` — a real `dispatch()` never trips this because `.ai/runs/`
    files it writes are gitignored in the real repo; the bare test fixture has
    no `.gitignore` at all, so an unignored `.ai/runs/` would misread as
    `dirty_outside_board` and `preflight` would refuse to run."""
    (root / ".gitignore").write_text(".ai/runs/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore .ai/runs/"], cwd=root, check=True)


def _select(root: Path, capabilities: set[str] | None = None,
            bad: dict | None = None) -> dict[str, runner.Candidate]:
    return {c.card.id: c for c in runner.select(root, capabilities or set(), bad or {})}


@pytest.fixture(autouse=True)
def _no_xdist_in_fixtures(monkeypatch):
    """The dispatch tests run a real one-test acceptance suite in a tmp worktree.
    Spinning up xdist workers for a single test is pure per-invocation overhead
    (it turned this file's runtime from ~3 to ~5 min), so blank the parallel flags
    for fixtures. `test_run_tests_passes_the_parallel_and_junit_flags` restores
    them to prove production still parallelises."""
    monkeypatch.setattr(runner, "_PYTEST_PARALLEL", (), raising=False)


# --- the tier binding is read from §16, never restated ----------------------


def _binding_repo(tmp_path: Path, body: str) -> None:
    """A repo whose manifest points at a plan doc holding — or not holding — a block.

    The manifest line is load-bearing, not scaffolding. `[tiers].binding_doc`
    defaulted to this project's own plan doc until 2026-08-02 and now defaults to
    `docs/tier-binding.md`, so a fixture that writes the plan doc and declares
    nothing is asserting about a file nothing reads: both tests below would then
    pass on "the binding document is missing" and would keep passing if the block
    parser were deleted outright.
    """
    doc = tmp_path / ".claude" / "memory" / "ai_team"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "00_architecture.md").write_text(body, encoding="utf-8")
    (tmp_path / ".ai").mkdir(exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[tiers]\nbinding_doc = ".claude/memory/ai_team/00_architecture.md"\n',
        encoding="utf-8")


def test_a_missing_binding_block_raises_rather_than_defaulting(tmp_path):
    """A dispatcher that fell back to a default model would reintroduce the
    2026-07-22 violation in a form nobody could see from the outside."""
    _binding_repo(tmp_path, "# no block here\n")
    with pytest.raises(tiers.TierError, match="tier-binding"):
        tiers.binding(tmp_path)


def test_a_block_that_drops_a_known_tier_raises(tmp_path):
    _binding_repo(tmp_path, "```tier-binding\nworker = sonnet\n```\n")
    with pytest.raises(tiers.TierError, match="lead"):
        tiers.binding(tmp_path)


def test_the_binding_is_actually_parsed_from_the_declared_document(tmp_path):
    """The positive case the two refusals above cannot prove on their own — added
    2026-08-02, when both were found passing for the wrong reason."""
    _binding_repo(tmp_path, "```tier-binding\nworker = haiku\nlead = opus\n```\n")
    assert tiers.binding(tmp_path) == {"worker": "haiku", "lead": "opus"}


# --- selection: every reason a card is skipped ------------------------------

def test_a_ready_card_is_dispatchable(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "ready")
    assert _select(root)["ready"].dispatchable


def test_an_attended_card_is_never_dispatched(tmp_path):
    """`unattended:` is declared by the author and checked by the runner; the
    runner never infers it (SESSIONS.md §G: 'do not let the runner decide what
    is safe to run unattended')."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "attended", unattended="false")
    candidate = _select(root)["attended"]
    assert not candidate.dispatchable
    assert "unattended" in candidate.reason


def test_a_card_requiring_a_capability_this_host_lacks_is_skipped(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "art")
    _card(root, "tasks", "icon", worker="art", requires="gpu-box")
    assert not _select(root, capabilities=set())["icon"].dispatchable
    assert _select(root, capabilities={"gpu-box"})["icon"].dispatchable


def test_a_card_whose_worker_has_no_charter_is_skipped(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "ghost", worker="nobody")
    assert not _select(root)["ghost"].dispatchable


def test_a_card_with_worker_none_is_skipped(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "manual", worker="none")
    assert "nothing to dispatch" in _select(root)["manual"].reason


def test_a_card_that_fails_the_schema_is_not_dispatched(tmp_path):
    """A malformed card is unfinished, not failed. It must not be handed to a
    worker, and it must not be filed as a failure either."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "broken")
    candidate = _select(root, bad={"broken": ["Board/tasks/broken.md:1 — card_schema: nope"]})["broken"]
    assert not candidate.dispatchable
    assert "card_schema" in candidate.reason


def test_a_card_at_the_attempt_limit_is_skipped(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "burnt", attempts=str(runner.MAX_ATTEMPTS))
    assert not _select(root)["burnt"].dispatchable


def test_a_recently_failed_card_waits_for_its_backoff(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "flaky", attempts="1", finished=dt.datetime.now().isoformat())
    candidate = _select(root)["flaky"]
    assert not candidate.dispatchable
    assert "backoff" in candidate.reason


def test_backoff_expires(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    long_ago = (dt.datetime.now() - dt.timedelta(days=1)).isoformat()
    _card(root, "tasks", "flaky", attempts="1", finished=long_ago)
    assert _select(root)["flaky"].dispatchable


def test_only_the_tasks_lane_is_dispatched(tmp_path):
    """A card in review/ is waiting for Claude, one in needs-decision/ for Karel.
    Dispatching either would redo work or overwrite a parked question."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    for lane in ("review", "needs-decision", "testing", "done", "failed", "inbox"):
        _card(root, lane, f"in-{lane}")
    assert _select(root) == {}


def test_selection_reports_every_card_not_just_the_ready_ones(tmp_path):
    """'nothing was dispatchable' and 'nothing was dispatched' are different
    nights and must not read the same in the log."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "yes")
    _card(root, "tasks", "no", unattended="false")
    candidates = runner.select(root, set(), {})
    assert len(candidates) == 2
    assert all(c.reason for c in candidates)


# --- a card grown past useful worker input ----------------------------------
#
# The card *is* the worker's opening message, so its size is the size of the
# input a worker is handed, and nothing bounds it since `worker-prompt-off-argv`
# retired the argv limit. The signal is deliberately advisory: gates here have
# one severity, so the blocking version would turn the board red on Karel using
# his own board, and that gate would get muted.

def _grow(path: Path, size: int) -> Path:
    """Pad a card past `size` bytes the way real cards grow — appended prose in
    a section, not a blob. Pure ASCII, so one character is one byte."""
    text = path.read_text(encoding="utf-8")
    filler = "Superseded history a worker still has to read first. "
    text += "\n## Thread\n\n" + filler * (2 + max(0, size - len(text)) // len(filler))
    path.write_text(text, encoding="utf-8")
    return path


def test_an_oversized_card_still_dispatches(tmp_path):
    """**The point of the whole feature.** Karel chose the advisory option over a
    `card_schema` hard stop, so size must be a remark about a card and never a
    verdict on it: a card that would have run at 8 KB runs at 20 KB, and the only
    difference is what the reason says. If this ever inverts, an unattended night
    stops on a card whose only fault is being long."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _grow(_card(root, "tasks", "fat"), runner.CARD_COMFORT_BYTES + 2000)
    candidate = _select(root)["fat"]
    assert candidate.dispatchable
    # and for the ordinary reason, unchanged — the note is an addition, not a
    # replacement, so `--card` still prints why it was picked.
    assert "worker: code-thread" in candidate.reason


def test_an_oversized_card_says_its_size_the_threshold_and_what_to_do(tmp_path):
    """A number that was exceeded is not actionable. The line has to name the
    constant to change and the two things Karel can do about it.

    The sizes are read back off the fixture and the constant rather than written
    in as literals, for the reason this file's docstring gives about the tier
    binding: the threshold is a comfort number on a growing board and is expected
    to move, so a test that hard-coded `14` would go red on a legitimate
    adjustment while proving nothing about the mechanism."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    path = _grow(_card(root, "tasks", "fat"), runner.CARD_COMFORT_BYTES + 2000)
    size = len(path.read_text(encoding="utf-8"))
    reason = _select(root)["fat"].reason

    assert "CARD_COMFORT_BYTES" in reason      # what to change
    assert "compact" in reason and "split" in reason   # what to do
    stated, limit = (float(n) for n in re.findall(r"([\d.]+) KB", reason))
    assert abs(stated * 1024 - size) < 100     # its own size, in KB
    assert limit * 1024 == runner.CARD_COMFORT_BYTES   # the threshold it passed


def test_a_card_under_the_threshold_says_nothing_about_its_size(tmp_path):
    """Every card is measured, so the quiet case is the common one — a note on
    an ordinary card would make the run log's per-card line unreadable and the
    digest's skip grouping useless."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "lean")
    reason = _select(root)["lean"].reason
    assert reason == "tier: worker, worker: code-thread"


def test_an_oversized_card_that_is_also_skipped_carries_both_reasons(tmp_path):
    """`record.skipped` is what reaches `Digest.md`, and it carries the reason
    verbatim — so an oversized card that is skipped for some other cause must
    still say so there, rather than having the size note displace the cause."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _grow(_card(root, "tasks", "fat", unattended="false"),
          runner.CARD_COMFORT_BYTES + 2000)
    candidate = _select(root)["fat"]
    assert not candidate.dispatchable
    assert "unattended" in candidate.reason
    assert "CARD_COMFORT_BYTES" in candidate.reason


def test_the_size_signal_measures_the_tasks_shape_only(tmp_path):
    """Cards grow after dispatch and they grow legitimately — the runner and
    close-out append `## Summary`, `## Thread`, `## Telemetry` and `## Error`,
    which is why `done/` holds 20-21 KB cards that were half that when a worker
    saw them. Firing on those would be noise about the record working as
    designed, so the lane check lives in the helper, not at its call site."""
    root = _repo(tmp_path)
    for lane in ("testing", "done", "failed", "review", "needs-decision"):
        path = _grow(_card(root, lane, f"fat-{lane}"),
                     runner.CARD_COMFORT_BYTES + 8000)
        card = board.Card.load(path, lane)
        assert len(card.text.encode("utf-8")) > runner.CARD_COMFORT_BYTES
        assert runner.oversize_note(card) == ""


def test_the_threshold_clears_a_normal_dense_card(tmp_path):
    """14 KB was chosen against the board's real numbers: the densest cards in
    `tasks/` are ~11-12 KB (a full acceptance split plus a findings list), and
    the card that actually hurt was 36 KB. A threshold that fired on a normal
    big card is the version that gets ignored, so pin the headroom rather than
    only the trip."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    path = _grow(_card(root, "tasks", "dense"), 12_500)
    assert 12_000 < len(path.read_text(encoding="utf-8")) < runner.CARD_COMFORT_BYTES
    assert "CARD_COMFORT_BYTES" not in _select(root)["dense"].reason


# --- naming one card by hand (`--card`) -------------------------------------
#
# This is the interactive path: Karel says "run card XYZ now" and someone types
# `--card XYZ`. Naming a card is an explicit human request, so the checks that
# exist to decide what may run *with nobody watching* are waived; the ones about
# physical reality are not.

def test_naming_a_card_waives_unattended_false(tmp_path):
    """`unattended: false` means 'a machine cannot tell whether this attempt
    succeeded'. Someone asking for it by name is a human volunteering to be the
    judge, which is exactly the missing ingredient."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "attended", unattended="false")
    assert not _select(root)["attended"].dispatchable
    forced = {c.card.id: c for c in runner.select(root, set(), {}, forced="attended")}
    assert forced["attended"].dispatchable
    assert "waived" in forced["attended"].reason


def test_naming_a_card_waives_backoff_and_the_attempt_limit(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "burnt", attempts=str(runner.MAX_ATTEMPTS),
          finished=dt.datetime.now().isoformat())
    assert not _select(root)["burnt"].dispatchable
    forced = {c.card.id: c for c in runner.select(root, set(), {}, forced="burnt")}
    assert forced["burnt"].dispatchable


def test_naming_a_card_does_not_conjure_hardware(tmp_path):
    """Wanting it harder does not put a GPU in the laptop."""
    root = _repo(tmp_path)
    _charter(root, "art")
    _card(root, "tasks", "icon", worker="art", requires="gpu-box")
    forced = runner.select(root, set(), {}, forced="icon")
    assert not forced[0].dispatchable
    assert "gpu-box" in forced[0].reason


def test_naming_a_card_does_not_bypass_a_broken_schema_or_a_missing_worker(tmp_path):
    """A malformed card dispatched produces confident nonsense rather than an
    error, and `worker: none` has nothing to dispatch to at all."""
    root = _repo(tmp_path)
    _card(root, "tasks", "broken")
    _card(root, "tasks", "manual", worker="none")
    bad = {"broken": ["Board/tasks/broken.md:1 — card_schema: nope"]}
    forced = {c.card.id: c for c in runner.select(root, set(), bad, forced="broken")}
    assert not forced["broken"].dispatchable
    forced = {c.card.id: c for c in runner.select(root, set(), {}, forced="manual")}
    assert not forced["manual"].dispatchable


def test_an_unknown_card_id_is_an_error_not_a_quiet_no_op(tmp_path):
    """A `--card` matching nothing used to fall through the loop and produce a
    clean, quiet, entirely successful run in which nothing happened — the same
    silent-nothing shape that has bitten this project four times."""
    root = _repo(tmp_path)
    picked, why = runner.resolve_named(root, "no-such-card", [])
    assert picked == []
    assert "no card `no-such-card`" in why


def test_naming_a_card_in_another_lane_says_which_lane(tmp_path):
    """The most likely mistake after a typo: asking for something already done,
    or still in inbox/. Saying where it actually is turns a refusal into an
    instruction."""
    root = _repo(tmp_path)
    _card(root, "review", "already-built")
    picked, why = runner.resolve_named(root, "already-built", [])
    assert picked == []
    assert "review/" in why


def test_naming_a_dispatchable_card_narrows_the_run_to_it(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "wanted")
    _card(root, "tasks", "other")
    candidates = runner.select(root, set(), {}, forced="wanted")
    picked, why = runner.resolve_named(root, "wanted", candidates)
    assert why == ""
    assert [c.card.id for c in picked] == ["wanted"]


# --- crash recovery ---------------------------------------------------------

def test_an_interrupted_attempt_is_recovered_and_already_counted(tmp_path):
    """The machine died mid-dispatch. `attempts` was incremented and committed
    before the worker started, so recovery neither loses nor double-counts it —
    this is what bounds a reboot loop."""
    root = _repo(tmp_path)
    _card(root, "tasks", "crashed", attempts="1", started="2026-07-23T03:00:00")
    notes = runner.recover(root)
    assert len(notes) == 1
    card = board.find(root, "crashed")
    assert card.attempts == 1
    assert not card.fields.get("started")
    assert card.fields.get("finished")
    assert "interrupted" in card.text.lower()


def test_recovery_leaves_a_finished_attempt_alone(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "clean", attempts="1", finished="2026-07-23T03:10:00")
    assert runner.recover(root) == []


def test_recovery_is_idempotent(tmp_path):
    """It runs on every boot. A second boot after a recovered crash must not
    burn another attempt."""
    root = _repo(tmp_path)
    _card(root, "tasks", "crashed", attempts="1", started="2026-07-23T03:00:00")
    runner.recover(root)
    assert runner.recover(root) == []
    assert board.find(root, "crashed").attempts == 1


def test_a_recovered_card_backs_off_before_retrying(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "crashed", attempts="1", started="2026-07-23T03:00:00")
    runner.recover(root)
    assert not _select(root)["crashed"].dispatchable


# --- settling an outcome ----------------------------------------------------

def test_a_green_run_moves_the_card_to_review(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "good", attempts="1", started="2026-07-23T03:00:00")
    runner.settle(root, "good", runner.Dispatch("review", "2 commits on ai/good"))
    card = board.find(root, "good")
    assert card.lane == "review"
    assert card.fields["state"] == "review"
    assert not card.fields.get("started")


def test_a_green_run_writes_the_workers_summary_onto_the_card(tmp_path):
    """The verdict's `summary` is already mandatory data — persist it as `##
    Summary` instead of only using it for the runner's own log line, so every
    card that reaches testing/ carries a brief account of what was done."""
    root = _repo(tmp_path)
    _card(root, "tasks", "good", attempts="1", started="2026-07-23T03:00:00")
    runner.settle(root, "good", runner.Dispatch(
        "review", "Added the grid layout; 9 new tests; gates green."))
    text = board.find(root, "good").text
    assert "## Summary" in text
    assert "Added the grid layout; 9 new tests; gates green." in text


def test_a_later_attempts_summary_replaces_the_earlier_one(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "good", attempts="1", started="2026-07-23T03:00:00")
    runner.settle(root, "good", runner.Dispatch("review", "first pass"))
    card = board.find(root, "good")
    card.write({"attempts": "2", "started": "2026-07-24T03:00:00"})
    runner.settle(root, "good", runner.Dispatch("review", "second pass, addressed feedback"))
    text = board.find(root, "good").text
    assert text.count("## Summary") == 1
    assert "second pass, addressed feedback" in text and "first pass" not in text


def test_a_parked_card_goes_to_needs_decision_with_its_question(tmp_path):
    """Parking is a success state (§13). The question is what makes it one."""
    root = _repo(tmp_path)
    _card(root, "tasks", "unclear", attempts="1", started="2026-07-23T03:00:00")
    runner.settle(root, "unclear", runner.Dispatch("parked", "Damage in HP or heat?"))
    card = board.find(root, "unclear")
    assert card.lane == "needs-decision"
    assert "Damage in HP or heat?" in card.text
    assert "## Question" in card.text


def test_settle_does_not_clobber_a_question_the_worker_already_wrote(tmp_path):
    """The worker prompt (§13) tells it to write `## Question` onto the card
    itself. `Dispatch.detail` for a parked outcome is only the verdict's
    `summary` field — a different, shorter field — truncated to 300 chars.
    `settle` must leave the worker's own question alone rather than stomping
    it with that truncated summary (menu-unlock-indicators, 2026-07-28)."""
    root = _repo(tmp_path)
    _card(root, "tasks", "unclear", attempts="1", started="2026-07-23T03:00:00")
    card = board.find(root, "unclear")
    card.write_section("Question", "Pick (A) re-arming or (B) permanent-once-seen dismissal.")
    truncated_summary = "Shipped points 1 & 2 of the note. Tiger unt"
    runner.settle(root, "unclear", runner.Dispatch("parked", truncated_summary))
    text = board.find(root, "unclear").text
    assert "Pick (A) re-arming or (B) permanent-once-seen dismissal." in text
    assert truncated_summary not in text


def test_a_failure_below_the_limit_stays_in_tasks_for_a_retry(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "flaky", attempts="1", started="2026-07-23T03:00:00")
    runner.settle(root, "flaky", runner.Dispatch("failed", "pytest: FAILED test_x"))
    card = board.find(root, "flaky")
    assert card.lane == "tasks"
    assert "pytest: FAILED test_x" in card.text
    assert card.fields.get("finished")


def test_a_failure_at_the_limit_moves_to_failed(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "doomed", attempts=str(runner.MAX_ATTEMPTS),
          started="2026-07-23T03:00:00")
    runner.settle(root, "doomed", runner.Dispatch("failed", "gates: import_layering"))
    assert board.find(root, "doomed").lane == "failed"


def test_repeated_failures_leave_one_current_error_not_a_stack(tmp_path):
    """Three attempts must not leave three stale errors on the card. Attempt
    history lives in `.ai/runs/`; the card carries the current state."""
    root = _repo(tmp_path)
    _card(root, "tasks", "flaky", attempts="1", started="2026-07-23T03:00:00")
    runner.settle(root, "flaky", runner.Dispatch("failed", "first failure"))
    card = board.find(root, "flaky")
    card.write({"attempts": "2", "started": "2026-07-23T04:00:00"})
    runner.settle(root, "flaky", runner.Dispatch("failed", "second failure"))
    text = board.find(root, "flaky").text
    assert text.count("## Error") == 1
    assert "second failure" in text and "first failure" not in text


def test_settling_a_card_the_worker_moved_does_not_crash(tmp_path):
    """The runner rescans before settling because the disk, not its memory, is
    the state. A card that vanished entirely is reported, not raised."""
    root = _repo(tmp_path)
    note = runner.settle(root, "ghost", runner.Dispatch("review", "x"))
    assert "vanished" in note


# --- preflight and the kill switch ------------------------------------------

def test_the_kill_switch_stops_the_runner(tmp_path):
    root = _repo(tmp_path)
    (root / ".ai").mkdir(exist_ok=True)
    (root / runner.STOP_FILE).write_text("Karel is testing tonight\n", encoding="utf-8")
    check = runner.preflight(root, "development_team", dry_run=True)
    assert not check.ok
    assert any("kill switch" in r for r in check.reasons)
    assert any("Karel is testing tonight" in r for r in check.reasons)


# The fixture repo's own forbidden set, not this repo's. They differed the moment
# the integration role moved to `test` (2026-08-06): `development_team` became
# forbidden here while the fixture's manifest still declared it as *its*
# integration branch, so the parametrize demanded a refusal the fixture was right
# to allow. A test that reads one repo's config and asserts against another's is
# testing the migration, not the rule.
@pytest.mark.parametrize("base", ("dev", "master", "main"))
def test_the_runner_refuses_to_build_on_dev_or_main(tmp_path, base):
    """SESSIONS.md's standing rule is 'never commit to dev'. An unattended
    process is exactly who would."""
    root = _repo(tmp_path)
    check = runner.preflight(root, base, dry_run=True)
    assert not check.ok
    assert any("forbidden" in r for r in check.reasons)



def test_the_runner_reads_the_base_branch_from_the_manifest_it_is_pointed_at(tmp_path):
    """Simulates the migration rather than trusting the invariant above to hold
    under a different value — the failure was latent precisely because nothing
    exercised the post-swap configuration. `--base dev` is refused today and
    accepted the moment the manifest says `dev` holds the role."""
    root = _repo(tmp_path)
    assert not runner.preflight(root, "dev", dry_run=True).ok

    _write_manifest(root, integration="dev", forbidden_extra=("development_team", "master"))
    assert "dev" not in runner.forbidden_bases(root)
    assert runner.default_base(root) == "dev"
    # main stays protected across the swap; that fact is not migration-dependent.
    assert "main" in runner.forbidden_bases(root)


def test_a_dirty_tree_blocks_a_real_run_but_not_a_dry_run(tmp_path):
    root = _repo(tmp_path)
    (root / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    assert not runner.preflight(root, "master", dry_run=False).ok
    # dry-run's only remaining objection is the forbidden base, not the dirt
    assert not any("dirty" in r for r in runner.preflight(root, "x", dry_run=True).reasons)


def test_board_edits_alone_do_not_count_as_a_dirty_tree(tmp_path):
    """The runner writes to Board/ constantly. If its own writes read as dirt it
    could never take a second card in one night."""
    root = _repo(tmp_path)
    _card(root, "tasks", "edited")
    assert runner.dirty_outside_board(root) == []


# --- host capabilities ------------------------------------------------------

def _hosts(root: Path, mapping: dict) -> None:
    (root / ".ai").mkdir(exist_ok=True)
    (root / runner.HOSTS_FILE).write_text(json.dumps(mapping), encoding="utf-8")


def test_no_host_config_means_no_capabilities(tmp_path):
    """The safe default: a card that requires something never dispatches,
    rather than being dispatched onto a machine that cannot run it."""
    assert runner.host_capabilities(tmp_path) == set()


def test_capabilities_are_keyed_by_hostname(tmp_path):
    """Committed and hostname-keyed, so one file is correct on every machine and
    a box is configured once rather than per clone."""
    import socket
    _hosts(tmp_path, {socket.gethostname(): {"capabilities": ["gpu-box"]}})
    assert runner.host_capabilities(tmp_path) == {"gpu-box"}


def test_an_unknown_hostname_gets_no_capabilities(tmp_path):
    """A new machine must not silently inherit another box's hardware. It gets
    nothing, so `requires:` cards stay in tasks/ at zero cost until someone adds
    a row."""
    _hosts(tmp_path, {"some-other-box": {"capabilities": ["gpu-box"]}})
    assert runner.host_capabilities(tmp_path) == set()


def test_the_local_override_wins_over_the_shared_file(tmp_path):
    import socket
    _hosts(tmp_path, {socket.gethostname(): {"capabilities": ["gpu-box"]}})
    (tmp_path / runner.HOST_FILE).write_text(
        json.dumps({"capabilities": []}), encoding="utf-8")
    assert runner.host_capabilities(tmp_path) == set()


def test_a_corrupt_host_file_degrades_to_no_capabilities(tmp_path):
    (tmp_path / ".ai").mkdir()
    (tmp_path / runner.HOSTS_FILE).write_text("{not json", encoding="utf-8")
    assert runner.host_capabilities(tmp_path) == set()


# --- harvesting artefacts that were never meant to be commits ---------------

def test_artefacts_are_rescued_before_the_worktree_is_destroyed(tmp_path):
    """An art card commits nothing — candidates sit in a gitignored `.tmp/` and
    move into `assets/` only once Karel picks one. Without this they would be
    deleted with the worktree and the card would be filed as 'produced
    nothing', which is both wrong and the most confusing failure available."""
    tree = tmp_path / "tree"
    (tree / "dungeoneer" / "assets" / ".tmp").mkdir(parents=True)
    (tree / "dungeoneer" / "assets" / ".tmp" / "icon_a.png").write_bytes(b"\x89PNG")
    (tree / "dungeoneer" / "assets" / ".tmp" / "raw" / "icon_b.png").parent.mkdir()
    (tree / "dungeoneer" / "assets" / ".tmp" / "raw" / "icon_b.png").write_bytes(b"\x89PNG")
    out = tmp_path / "out"
    out.mkdir()
    # `harvest_dirs` is `[worker].harvest_dirs` since 07_portability.md §8 step 4,
    # so the harvest root has to say which repo it is reading the manifest of.
    (tmp_path / ".ai").mkdir(exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[worker]\nharvest_dirs = ["dungeoneer/assets/.tmp"]\n', encoding="utf-8")

    assert runner.harvest(tmp_path, tree, out) == 2
    assert (out / "artefacts" / ".tmp" / "icon_a.png").is_file()
    assert (out / "artefacts" / ".tmp" / "raw" / "icon_b.png").is_file()


def test_harvest_is_silent_when_there_is_nothing_to_rescue(tmp_path):
    (tmp_path / "tree").mkdir()
    (tmp_path / "out").mkdir()
    assert runner.harvest(tmp_path, tmp_path / "tree", tmp_path / "out") == 0


def test_a_project_declaring_no_harvest_dirs_rescues_nothing(tmp_path):
    """The default for a project whose workers only ever commit. It must be a
    quiet zero, not a crash — `[worker]` is optional by design."""
    (tmp_path / "tree" / "assets" / ".tmp").mkdir(parents=True)
    (tmp_path / 'tree' / 'assets' / '.tmp' / 'x.png').write_bytes(b'fake-png')
    (tmp_path / "out").mkdir()
    assert runner.harvest_dirs(tmp_path) == ()
    assert runner.harvest(tmp_path, tmp_path / "tree", tmp_path / "out") == 0


def test_an_artefact_counts_as_output_even_with_no_commit(tmp_path, monkeypatch):
    """The empty-diff check must be 'neither a commit nor an artefact'. Commits
    alone is wrong for art; artefacts alone would be wrong for code."""
    root = _worktree_repo(tmp_path)
    _charter(root, "art")
    _card(root, "tasks", "icon", worker="art")

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "candidate_1.png").write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.4}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert (root / ".ai" / "runs" / "icon" / "attempt-1" / "artefacts" / ".tmp"
            / "candidate_1.png").is_file()


def test_artefacts_are_kept_when_the_card_parks(tmp_path, monkeypatch):
    """A parked art card is exactly the case where the candidates matter most —
    they are what makes the question answerable in 15 seconds."""
    root = _worktree_repo(tmp_path)
    _charter(root, "art")
    _card(root, "tasks", "icon", worker="art")

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "best_of_twelve.png").write_bytes(b"\x89PNG")
        verdict = Path(next(l.strip() for l in prompt.splitlines()
                            if l.strip().endswith(".json")))
        verdict.parent.mkdir(parents=True, exist_ok=True)
        verdict.write_text(json.dumps(
            {"outcome": "parked", "summary": "3 rounds, no pass — pick one or rethink"}),
            encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    assert (root / ".ai" / "runs" / "icon" / "attempt-1" / "artefacts" / ".tmp"
            / "best_of_twelve.png").is_file()


def test_an_uncommitted_diff_is_banked_not_dropped(tmp_path, monkeypatch):
    """A worker can finish its edits and die before `git commit` — the
    background-a-test-and-wait pattern in a one-shot run (2026-07-24,
    orientation-size-budget attempt 2 lost 11 edits this way). The diff is work,
    not silence: the runner must bank it as a `wip:` commit and let it reach the
    acceptance step, never drop the worktree and file 'produced nothing'."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "forgot-commit")

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        # Edit a tracked file but never commit — exactly what a worker that died
        # awaiting a backgrounded test leaves behind in its worktree.
        (Path(cwd) / "seed.txt").write_text("edited by the worker\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "forgot-commit"),
                             "development_team", "sonnet", 5.0, 120)

    assert result.outcome == "review", result.detail
    # The edit survives on the branch as a wip commit, not lost with the worktree.
    banked = subprocess.run(["git", "show", "ai/forgot-commit:seed.txt"],
                            cwd=root, capture_output=True, text=True)
    assert banked.returncode == 0 and "edited by the worker" in banked.stdout
    log = subprocess.run(["git", "log", "--oneline", "ai/forgot-commit"],
                         cwd=root, capture_output=True, text=True).stdout
    assert "wip: forgot-commit" in log


def test_a_worker_that_truly_did_nothing_still_fails(tmp_path, monkeypatch):
    """Banking an uncommitted diff must not mask a worker that changed nothing:
    a clean worktree is still 'produced neither a commit nor an artefact'."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "did-nothing")

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "did-nothing"),
                             "development_team", "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "neither a commit nor an artefact" in result.detail


# --- the single-instance lock -----------------------------------------------

def test_the_lock_is_taken_and_released(tmp_path):
    assert runner.acquire_lock(tmp_path)
    assert (tmp_path / runner.LOCK_FILE).is_file()
    runner.release_lock(tmp_path)
    assert not (tmp_path / runner.LOCK_FILE).is_file()


def test_a_live_lock_blocks_a_second_runner(tmp_path):
    runner.acquire_lock(tmp_path)  # writes our own, live, PID
    assert not runner.acquire_lock(tmp_path)


def test_a_stale_lock_is_taken_over(tmp_path):
    """The 3 AM reboot leaves exactly this. A runner that refused to start
    because of a dead PID's lock would need a human, which defeats the point."""
    (tmp_path / ".ai" / "runs").mkdir(parents=True)
    (tmp_path / runner.LOCK_FILE).write_text("999999 2026-07-23T03:00:00\n", encoding="utf-8")
    assert runner.acquire_lock(tmp_path)


# --- the card writer --------------------------------------------------------

def test_setting_a_field_preserves_order_and_the_body(tmp_path):
    root = _repo(tmp_path)
    path = _card(root, "tasks", "probe")
    before = path.read_text(encoding="utf-8")
    card = board.Card.load(path, "tasks")
    card.write({"attempts": "1"})
    after = path.read_text(encoding="utf-8")
    assert after.index("id:") < after.index("title:") < after.index("state:")
    assert "## Intent" in after and "A probe card." in after
    assert before.split("---")[2] == after.split("---")[2]  # body untouched


def test_runner_fields_are_appended_not_interleaved(tmp_path):
    """03_board.md §2 splits the schema by owner so a runner that dies mid-card
    has rewritten only its own half."""
    root = _repo(tmp_path)
    path = _card(root, "tasks", "probe")
    card = board.Card.load(path, "tasks")
    card.write({"attempts": "1", "branch": "ai/probe"})
    text = path.read_text(encoding="utf-8")
    assert text.index("created:") < text.index("attempts:") < text.index("branch:")


def test_setting_a_field_to_none_removes_it(tmp_path):
    root = _repo(tmp_path)
    path = _card(root, "tasks", "probe", started="2026-07-23T03:00:00")
    card = board.Card.load(path, "tasks")
    card.write({"started": None})
    assert "started:" not in path.read_text(encoding="utf-8")


def test_a_move_rewrites_state_to_match_the_new_lane(tmp_path):
    """The lane is the truth and `state:` is the denormalised copy; a move that
    updated only one of them is the half-done move `card_schema` exists to
    catch, and the runner must never be the one producing it."""
    root = _repo(tmp_path)
    _card(root, "tasks", "probe")
    board.move(root, board.find(root, "probe"), "review")
    card = board.find(root, "probe")
    assert card.lane == "review" and card.fields["state"] == "review"
    assert not (root / "Board" / "tasks" / "probe.md").exists()


def test_the_board_commits_even_when_there_is_no_digest_yet(tmp_path):
    """Regression, found by the attempts-before-dispatch test. `git add` fails
    the *whole* pathspec when one entry matches nothing, so naming `Digest.md`
    before it exists staged nothing and every board commit was a silent no-op.
    In a fresh clone that means `attempts` never reaches disk and a crashed card
    retries forever — a bug whose only symptom is a wasted night."""
    root = _repo(tmp_path)
    (root / "Digest.md").unlink(missing_ok=True)
    _card(root, "tasks", "probe")
    board.commit_board(root, "board: probe attempt 1")
    pending = subprocess.run(["git", "status", "--porcelain", "Board/"], cwd=root,
                             capture_output=True, text=True).stdout.strip()
    assert pending == "", "the card was not committed"


def test_the_board_commit_leaves_other_work_in_progress_alone(tmp_path):
    """Never `-a`. The runner must not be able to sweep up Karel's half-finished
    edit into a board commit at 3 AM."""
    root = _repo(tmp_path)
    _card(root, "tasks", "probe")
    (root / "half-finished.py").write_text("x = 1\n", encoding="utf-8")
    board.commit_board(root, "board: probe")
    still_dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                 capture_output=True, text=True).stdout
    assert "half-finished.py" in still_dirty


def test_find_locates_a_card_in_any_lane(tmp_path):
    root = _repo(tmp_path)
    _card(root, "review", "elsewhere")
    assert board.find(root, "elsewhere").lane == "review"
    assert board.find(root, "nonexistent") is None


def test_find_does_not_enumerate_the_private_lane(tmp_path):
    """`ideas/` is Karel's. `.ai/reconcile.py` is the only code permitted to look
    inside, and it reads one field (03_board.md §1). The runner is a judgment
    actor and must not see it at all."""
    root = _repo(tmp_path)
    _card(root, "ideas", "private")
    assert "ideas" not in board.LANES
    assert board.find(root, "private") is None


def test_appending_a_section_replaces_rather_than_stacks(tmp_path):
    text = "---\nid: x\n---\n\n## Intent\n\nBody.\n"
    once = board.append_section(text, "Error", "first")
    twice = board.append_section(once, "Error", "second")
    assert twice.count("## Error") == 1
    assert "second" in twice and "first" not in twice
    assert "## Intent" in twice


# --- the dispatch prompt ----------------------------------------------------

def test_the_prompt_states_a_resolved_tier(tmp_path):
    """`.ai/hooks/tier_guard.py` is the enforcement point for §16, and it looks
    for a stated tier in the spawn text. The runner is the dispatcher named as
    §16's second landing place, so its prompt must satisfy the same contract the
    hook enforces on interactive spawns."""
    from nightshift.hooks import tier_guard

    prompt = runner._PROMPT.format(
        tier="worker", model="sonnet", branch="ai/x", base="development_team",
        card_path="Board/tasks/x.md", verdict_path="v.json",
        tool_economy=worker_prompt.TOOL_ECONOMY, card_body="body")
    assert tier_guard.evaluate({"tool_name": "Agent",
                                "tool_input": {"prompt": prompt}}) is None


def test_the_prompt_forbids_moving_the_card_and_touching_dev(tmp_path):
    prompt = runner._PROMPT.format(
        tier="worker", model="sonnet", branch="ai/x", base="development_team",
        card_path="Board/tasks/x.md", verdict_path="v.json",
        tool_economy=worker_prompt.TOOL_ECONOMY, card_body="body")
    assert "Do not move the card" in prompt
    assert "never check out `dev`" in prompt
    assert "parked" in prompt and "success state" in prompt


# --- the whole cycle, end to end --------------------------------------------
#
# These drive a real dispatch — worktree, argv, verdict, gate/test run, card
# move, commit — with the CLI itself replaced at `_run_worker`, the one
# non-deterministic line in the file. Everything else is the production path.
# The point is that a wrong file move or a mis-settled outcome is found here, in
# eight seconds, rather than at 7 AM after a night that produced nothing.

def _gate_stub(monkeypatch, tmp_path: Path, body: str = "import sys; sys.exit(0)\n") -> Path:
    """Point `runner.GATE_ARGV` at a scripted stand-in for the gate suite.

    The fixture repo used to stub the harness by *writing* `.ai/gates/run.py`, and
    that is precisely why the suite could not see step 3 break production: the
    fixture created the very file the extraction had deleted, so the tests kept
    exercising a path that no longer existed in a real worktree. Substituting the
    argv keeps the seam pointed at whatever `_run_gates` actually invokes.
    """
    stub = tmp_path / "gate_stub.py"
    stub.write_text(body, encoding="utf-8")
    monkeypatch.setattr(runner, "GATE_ARGV", [sys.executable, str(stub)])
    return stub


@pytest.fixture(autouse=True)
def _gates_pass_by_default(monkeypatch, tmp_path):
    """Green gates unless a test says otherwise — what the `.ai/gates/run.py`
    stub in `_worktree_repo` used to provide. A test that wants red gates calls
    `_gate_stub(monkeypatch, tmp_path, <failing body>)` to replace this."""
    _gate_stub(monkeypatch, tmp_path)


def _worktree_repo(tmp_path: Path) -> Path:
    """A repo with a `development_team` branch, so `dispatch` can cut a real
    worktree and run its real acceptance step. The gate harness is stubbed by
    `_gates_pass_by_default` substituting `runner.GATE_ARGV` — deliberately NOT
    by writing `.ai/gates/run.py` here, which is what previously hid the gate
    runner's move behind a file only the fixture created."""
    root = _repo(tmp_path)
    subprocess.run(["git", "branch", "-M", "development_team"], cwd=root, check=True)
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    # Match production: art candidates and run dirs are gitignored, so an art
    # card's `.tmp/` output never registers as an uncommitted diff. Without this
    # the WIP-rescue path would stage those candidates in the fixture and the
    # artefact-counts-as-output test would reach review via a commit instead.
    (root / ".gitignore").write_text(
        "dungeoneer/assets/.tmp/\n.ai/runs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "scaffold"], cwd=root, check=True)
    return root


def _fake_worker(monkeypatch, *, verdict: dict | None = None, commit: bool = True,
                 returncode: int = 0, cost: float = 0.11, stderr: str = ""):
    """Stand in for the CLI: optionally commit on the branch, optionally write a
    verdict, and report a cost the way `--output-format json` does."""
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        seen["argv"] = argv
        seen["cwd"] = Path(cwd)
        if commit:
            (Path(cwd) / "worked.txt").write_text("done\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-qm", "worker: did the thing"], cwd=cwd, check=True)
        if verdict is not None:
            target = next(a for i, a in enumerate(argv) if a.endswith(".json")) \
                if any(a.endswith(".json") for a in argv) else None
            path = Path(target) if target else None
            if path is None:  # the path is named in the prompt, not the argv
                path = Path(next(l.strip() for l in prompt.splitlines()
                                 if l.strip().endswith(".json")))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(verdict), encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, returncode, json.dumps({"total_cost_usd": cost}), stderr)

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    return seen


def test_a_successful_dispatch_lands_the_card_in_review(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "implemented"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.cost_usd == pytest.approx(0.11)

    runner.settle(root, "probe", result)
    card = board.find(root, "probe")
    assert card.lane == "review"
    assert card.fields["branch"] == "ai/probe"
    assert card.attempts == 1
    assert card.fields.get("finished") and not card.fields.get("started")


# --- the usage-limit wall ---------------------------------------------------
#
# The bug these exist for: a wall *is* a non-zero exit, so before `limits.py` it
# was read as the card failing. `attempts` had already been committed, so one
# wall at 02:00 walked the rest of the queue, spent an attempt on every card and
# wrote `## Error: worker exited 1` on each — and a few nights of that files
# working cards into `failed/`. The card must come back untouched.

def test_a_usage_limit_is_not_the_cards_fault(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    assert result.outcome == "limited"
    assert result.wall is not None and result.wall.scope == limits.SESSION


def test_a_limited_card_gets_its_attempt_back_and_stays_in_tasks(tmp_path, monkeypatch):
    """`attempts` is committed before the worker starts, which is right for every
    other exit. The plan running out is not a fact about this card, and charging
    it one of its three attempts would push it toward `failed/` for the crime of
    being in flight at the wrong moment."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    runner.settle(root, "probe", result)

    card = board.find(root, "probe")
    assert card.lane == "tasks"
    assert card.attempts == 0
    assert "attempts" not in card.fields          # pristine, not "attempts: 0"
    assert not card.fields.get("started") and not card.fields.get("finished")
    assert "## Error" not in card.text            # nothing to answer for


def test_a_second_attempt_stopped_by_a_wall_rewinds_to_the_first(tmp_path, monkeypatch):
    """The rewind is one attempt, not a reset to zero — a card that genuinely
    failed once has still failed once."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe", attempts="1")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    runner.settle(root, "probe", result)
    assert board.find(root, "probe").attempts == 1


def test_an_ordinary_non_zero_exit_is_still_the_cards_fault(tmp_path, monkeypatch):
    """The other half of the check — recognising walls must not turn every
    failure into someone else's problem."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, returncode=1, stderr="Traceback: ImportError")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 0.0, 120)
    assert result.outcome == "failed"
    runner.settle(root, "probe", result)
    assert board.find(root, "probe").attempts == 1


def test_no_dollar_cap_is_passed_to_the_cli_by_default(tmp_path, monkeypatch):
    """`--max-budget-usd 0` is a cap of zero, not the absence of one, so "no
    cap" has to mean "no flag"."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team",
                    "sonnet", runner.DEFAULT_CARD_BUDGET_USD, 120)
    assert "--max-budget-usd" not in seen["argv"]


def test_a_dollar_cap_is_still_passed_when_one_is_asked_for(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)
    assert seen["argv"][seen["argv"].index("--max-budget-usd") + 1] == "5.0"


# --- the night's stopping conditions ----------------------------------------
#
# These drive `run()` with `dispatch` replaced, because what is under test is the
# loop's arithmetic — how many windows have been spent, whether the same card is
# retried, when the night ends — and not what any one dispatch does.

def _tier_binding(root: Path) -> None:
    """The §16 block `tiers.resolve` reads. Real, not stubbed — the binding being
    parsed out of the doc rather than hardcoded is the guarantee that no caller
    names a model, and a test that patched it away would stop checking that."""
    doc = root / ".claude" / "memory" / "ai_team"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "00_architecture.md").write_text(
        "```tier-binding\nworker = sonnet\nlead = opus\n```\n", encoding="utf-8")


def _loaded_board(tmp_path: Path, *card_ids: str) -> Path:
    """A committed repo with a tier binding, a charter and some ready cards."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _tier_binding(root)
    for card_id in card_ids:
        _card(root, "tasks", card_id)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "cards"], cwd=root, check=True)
    return root


def _night(monkeypatch, root: Path, outcomes: list[runner.Dispatch]) -> list[str]:
    """Run one night with a scripted sequence of dispatch outcomes. Returns the
    card ids dispatched, in order — the last one repeating means a wall sent the
    loop back to the same card, which is the property most of these check."""
    calls: list[str] = []
    script = list(outcomes)

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        return script.pop(0) if script else runner.Dispatch("review", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "_sleep_until", lambda when: True)
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    return calls


def _wall(scope: str = limits.SESSION) -> runner.Dispatch:
    return runner.Dispatch("limited", "usage limit reached", 0.0, 1,
                           limits.Wall(scope, None, "usage limit reached"))


def test_the_night_stops_at_the_first_wall_by_default(tmp_path, monkeypatch):
    """`--sessions 1` is "work until the session limit is reached"."""
    root = _loaded_board(tmp_path, "a", "b", "c")

    calls = _night(monkeypatch, root, [_wall()])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a"]


def test_two_sessions_sleeps_through_the_reset_and_retries_the_same_card(tmp_path, monkeypatch):
    """"Until two session limits are used". The card that met the wall was never
    attempted, so the next window starts with it rather than skipping it."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [_wall(), runner.Dispatch("review", "ok"), _wall()])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))
    assert calls == ["a", "a", "b"]  # a walled, a retried after the reset, then b walled


def test_the_retried_card_is_reloaded_so_the_rewind_is_not_undone(tmp_path, monkeypatch):
    """`dispatch` reads the attempt number off the Card object it is handed and
    mutates it in place. Retrying with the same in-memory copy would spend
    attempt 2 on the card's first real run — silently undoing the rewind that is
    the whole point of not blaming a card for the plan running out."""
    root = _loaded_board(tmp_path, "a")
    seen: list[int] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        seen.append(card.attempts)
        # What the real dispatch does before the worker starts.
        card.write({"attempts": str(card.attempts + 1), "started": "now"})
        return _wall() if len(seen) == 1 else runner.Dispatch("review", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "_sleep_until", lambda when: True)
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))

    assert seen == [0, 0]  # not [0, 1]


def test_a_card_moved_off_the_board_while_we_slept_is_left_alone(tmp_path, monkeypatch):
    """Hours pass inside a wall. Karel answering a card from his phone in that
    window has to win over the runner's plan from before it slept."""
    root = _loaded_board(tmp_path, "a", "b")

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        if len(calls) == 1:
            board.move(root, board.find(root, "a"), "needs-decision")
            return _wall()
        return runner.Dispatch("review", "ok")

    calls: list[str] = []
    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "_sleep_until", lambda when: True)
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "2"]))

    assert calls == ["a", "b"]  # a is not retried; it is no longer in tasks/


def test_a_weekly_limit_ends_the_night_even_with_sessions_to_spare(tmp_path, monkeypatch):
    """A weekly window does not reopen inside a night; sleeping on one would idle
    until morning and produce nothing."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [_wall(limits.WEEKLY)])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--sessions", "5"]))
    assert calls == ["a"]


def test_a_transient_rate_limit_does_not_spend_one_of_the_nights_sessions(tmp_path, monkeypatch):
    """A 429 reopens in seconds. Counting one against `--sessions 1` would end a
    good night over a hiccup."""
    root = _loaded_board(tmp_path, "a", "b")

    calls = _night(monkeypatch, root, [_wall(limits.TRANSIENT)])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "a", "b"]  # hiccup, retry, carry on


def test_endless_transient_limits_are_capped_rather_than_retried_until_morning(tmp_path, monkeypatch):
    """A hiccup that never clears is indistinguishable from a wall we misread,
    and must not sleep-and-retry until Karel wakes up."""
    root = _loaded_board(tmp_path, "a")

    calls = _night(monkeypatch, root, [_wall(limits.TRANSIENT)] * 20)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert len(calls) == runner.TRANSIENT_RETRIES + 1


def test_three_failures_in_a_row_end_the_night(tmp_path, monkeypatch):
    """The net under `limits.detect`. A wall phrased in words the detector does
    not carry looks exactly like every card failing; without this the runner
    walks the whole queue spending an attempt on each."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d", "e")

    calls = _night(monkeypatch, root, [runner.Dispatch("failed", "worker exited 1")] * 5)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b", "c"]


def test_a_success_between_failures_resets_the_streak(tmp_path, monkeypatch):
    """Two flaky cards either side of a good one is not a systemic failure."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d")

    calls = _night(monkeypatch, root, [
        runner.Dispatch("failed", "x"),
        runner.Dispatch("failed", "x"),
        runner.Dispatch("review", "ok"),
        runner.Dispatch("failed", "x"),
    ])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b", "c", "d"]


def test_the_default_night_has_no_dollar_stopping_condition(tmp_path, monkeypatch):
    """The flag Karel asked to stop steering by. A cost-bearing dispatch must not
    end the night at some API-equivalent figure nobody is spending."""
    root = _loaded_board(tmp_path, "a", "b", "c")

    calls = _night(monkeypatch, root, [runner.Dispatch("review", "ok", cost_usd=99.0)] * 3)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))
    assert calls == ["a", "b", "c"]


def test_the_branch_survives_the_dispatch_but_the_worktree_does_not(tmp_path, monkeypatch):
    """The branch is the deliverable — review/ reads it and Karel merges it.
    Twenty stale checkouts beside the repo are not."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)
    branches = subprocess.run(["git", "branch", "--list", "ai/probe"], cwd=root,
                              capture_output=True, text=True).stdout
    assert "ai/probe" in branches
    assert not (runner.worktree_root(root) / "probe").exists()


def test_attempts_is_committed_before_the_worker_starts(tmp_path, monkeypatch):
    """The property the whole 'resumable from disk alone' claim rests on. If the
    machine dies inside the worker, the next boot must find the attempt already
    spent, or a reboot loop retries forever."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        card = board.find(root, "probe")
        seen["attempts"] = card.attempts
        seen["started"] = bool(card.fields.get("started"))
        # Committed, not merely written: an unflushed edit is lost to a reboot,
        # and it is the commit that makes the bump survive one.
        seen["committed"] = subprocess.run(
            ["git", "status", "--porcelain", "Board/"], cwd=root,
            capture_output=True, text=True).stdout.strip() == ""
        return subprocess.CompletedProcess(argv, 1, "{}", "died")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)

    assert seen == {"attempts": 1, "started": True, "committed": True}


def test_a_worker_that_produces_nothing_is_a_failure(tmp_path, monkeypatch):
    """Gates pass trivially on an empty diff, so 'green' would otherwise mean
    'the worker did nothing' and the card would sail into review/. 'Nothing'
    means neither a commit nor a harvested artefact — art produces the second
    kind and never the first."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "neither a commit nor an artefact" in result.detail


def test_a_parked_verdict_reaches_needs_decision(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False,
                 verdict={"outcome": "parked", "summary": "HP or heat?"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    runner.settle(root, "probe", result)
    assert board.find(root, "probe").lane == "needs-decision"


def test_parking_beats_the_no_commit_rule(tmp_path, monkeypatch):
    """A parked card has nothing to commit by definition. If the empty-diff
    check ran first, every well-formed park would be filed as a failure — which
    is precisely backwards from §13, where parking is a success state."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict={"outcome": "parked", "summary": "q"})
    assert runner.dispatch(root, board.find(root, "probe"), "development_team",
                           "sonnet", 5.0, 120).outcome == "parked"


def test_red_gates_fail_the_card_even_with_a_done_verdict(tmp_path, monkeypatch):
    """The worker does not get to mark its own homework. §11: the gates are
    first and Claude's review is second, never the other way round."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _gate_stub(monkeypatch, tmp_path,
               "print('Board/x.md:1 — nope'); raise SystemExit(1)\n")
    subprocess.run(["git", "commit", "-aqm", "red gate"], cwd=root, check=True)
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "all good honest"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "gates" in result.detail


def test_a_failing_test_suite_fails_the_card(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert False\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-aqm", "red test"], cwd=root, check=True)
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "failed"
    assert "pytest" in result.detail


def test_a_worker_with_no_verdict_falls_through_to_the_gates(tmp_path, monkeypatch):
    """Charters written before this runner existed do not know about the verdict
    file. Absent verdict must mean 'judge it by the gates', not 'fail it' — or
    Session G would have silently broken every worker Sessions E and F wrote."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict=None)
    assert runner.dispatch(root, board.find(root, "probe"), "development_team",
                           "sonnet", 5.0, 120).outcome == "review"


def test_three_failures_retire_the_card_to_failed(tmp_path, monkeypatch):
    """The full retry arc, driven for real: fail, fail, fail, filed."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict=None)

    for expected_lane in ("tasks", "tasks", "failed"):
        card = board.find(root, "probe")
        card.write({"finished": None})  # skip the backoff wait, not the counting
        result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
        runner.settle(root, "probe", result)
        assert board.find(root, "probe").lane == expected_lane

    assert board.find(root, "probe").attempts == runner.MAX_ATTEMPTS


def test_the_dispatch_argv_carries_the_resolved_model_and_the_charter(tmp_path, monkeypatch):
    """No caller ever names a model (§16) — but the argv the CLI receives must,
    and it must be the one the tier resolved to."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 3.0, 120)
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--agent") + 1] == "code-thread"
    assert argv[argv.index("--max-budget-usd") + 1] == "3.0"
    assert seen["cwd"] == runner.worktree_root(root) / "probe"


def test_run_output_is_written_beside_the_run_not_onto_the_card(tmp_path, monkeypatch):
    """Board/README.md: run output goes to `.ai/runs/<id>/` and the card links
    to it. A card that accumulated transcripts would stop being readable."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)
    out = root / ".ai" / "runs" / "probe" / "attempt-1"
    assert (out / "prompt-1.md").is_file()      # per round, so round 2 does not clobber round 1
    assert (out / "worker-1.json").is_file()
    assert (out / "gates.txt").is_file()
    assert (out / "pytest.txt").is_file()
    assert len(board.find(root, "probe").text) < 2000


# --- the producer/checker loop (00_architecture.md §16, "the second seam") ---
#
# The loop lives in the runner rather than inside the producer, because every
# step of it is a file-state lookup or an integer comparison — which is exactly
# the work §5 says belongs there. These tests pin the four properties that move
# gained: the bound is a `range()` and not a sentence in a charter; the checker's
# blindness is structural; both tiers go through the dispatcher; and feedback
# reaches the next round.

def _fake_loop(monkeypatch, verdicts: list[str], *, notes: str = "too dark"):
    """Drive N rounds: the producer writes a candidate, the checker returns the
    next verdict in the list. Records what each side was actually shown."""
    seen: dict = {"producer_prompts": [], "checker_prompts": [], "calls": []}
    pending = list(verdicts)

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        agent = argv[argv.index("--agent") + 1]
        seen["calls"].append(agent)
        if agent == "art":
            seen["producer_prompts"].append(prompt)
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "cand.png").write_bytes(b"\x89PNG")
        else:
            seen["checker_prompts"].append(prompt)
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(
                {"verdict": pending.pop(0), "best": "cand.png", "notes": notes}),
                encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    return seen


def _art_card(root: Path, card_id: str = "icon") -> None:
    _charter(root, "art")
    _charter(root, "art-reviewer")
    _card(root, "tasks", card_id, worker="art", checker="art-reviewer")


def test_a_pass_on_the_first_round_ends_the_loop(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["pass"])

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 1
    assert seen["calls"] == ["art", "art-reviewer"]


def test_a_revise_verdict_runs_another_round_with_the_notes(tmp_path, monkeypatch):
    """The feedback path. If the notes do not reach the next prompt the loop is
    just re-rolling dice, which is the expensive way to do nothing."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["revise", "pass"], notes="blades merge below 24px")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 2
    assert seen["calls"] == ["art", "art-reviewer", "art", "art-reviewer"]
    assert "blades merge below 24px" in seen["producer_prompts"][1]
    assert "blades merge below 24px" not in seen["producer_prompts"][0]


def test_the_round_limit_is_enforced_by_the_runner_not_by_the_charter(tmp_path, monkeypatch):
    """The reason this loop moved out of the producer. Three rounds used to be a
    sentence an LLM could ignore; here it is a `range()`."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["revise"] * 10)

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.rounds == runner.MAX_ROUNDS
    assert seen["calls"].count("art") == runner.MAX_ROUNDS


def test_exhausting_the_rounds_parks_rather_than_fails(tmp_path, monkeypatch):
    """§13: a well-formed question is a success state. The candidates and the
    critique are what let Karel decide in fifteen seconds."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop(monkeypatch, ["reject"] * 5, notes="silhouette unreadable at 20px")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    assert "silhouette unreadable at 20px" in result.detail
    assert "cand.png" in result.detail

    runner.settle(root, "icon", result)
    card = board.find(root, "icon")
    assert card.lane == "needs-decision"
    assert "## Question" in card.text


def test_the_checker_is_never_shown_the_producers_prompt(tmp_path, monkeypatch):
    """§16's whole point, and the reason the loop is built here: an agent that
    knows what was intended sees what was intended. While the producer spawned
    its own reviewer this was a charter instruction it could quietly break; the
    runner builds the checker's context, so there is no path for the prompt to
    travel."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    seen = _fake_loop(monkeypatch, ["pass"])
    runner.dispatch(root, board.find(root, "icon"), "development_team", "sonnet", 5.0, 120)

    checker_prompt = seen["checker_prompts"][0]
    producer_prompt = seen["producer_prompts"][0]
    assert "worktree" not in checker_prompt
    assert "Board/tasks" not in checker_prompt
    assert producer_prompt not in checker_prompt
    # …but it DOES get the acceptance criteria, verbatim off the card.
    assert "the gates are green" in checker_prompt


def test_the_checker_is_dispatched_with_an_explicit_model(tmp_path, monkeypatch):
    """A nested spawn inherits its parent's model — §16's original bug, one
    level deeper and out of tier_guard's reach because it names no card path.
    Routing the checker through the dispatcher is what closes it."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    calls: list[list[str]] = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        calls.append(argv)
        if argv[argv.index("--agent") + 1] == "art-reviewer":
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"verdict": "pass", "best": "c.png", "notes": ""}),
                              encoding="utf-8")
        else:
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "c.png").write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.dispatch(root, board.find(root, "icon"), "development_team", "sonnet", 5.0, 120)

    checker_argv = next(a for a in calls if a[a.index("--agent") + 1] == "art-reviewer")
    assert "--model" in checker_argv
    assert checker_argv[checker_argv.index("--model") + 1] == "sonnet"


def test_a_producer_that_parks_stops_the_loop_immediately(tmp_path, monkeypatch):
    """An ambiguity in the card is not fixed by re-rolling. Spending the
    remaining rounds re-asking a question the producer already said it cannot
    answer is the definition of burning the night."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    calls: list[str] = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        agent = argv[argv.index("--agent") + 1]
        calls.append(agent)
        target = Path(next(l.strip() for l in prompt.splitlines()
                           if l.strip().endswith(".json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"outcome": "parked", "summary": "which corp's logo?"}),
                          encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "parked"
    assert "which corp's logo?" in result.detail
    assert calls == ["art"], "the checker must not run on a parked round"


def test_a_card_with_no_checker_runs_exactly_one_round(tmp_path, monkeypatch):
    """`checker:` is optional and absent means the old behaviour. Every code
    card on the board relies on that."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 1
    assert seen["argv"][seen["argv"].index("--agent") + 1] == "code-thread"


def test_every_round_costs_are_summed(tmp_path, monkeypatch):
    """Two producer rounds plus two checker runs at $0.10 each."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    _fake_loop(monkeypatch, ["revise", "pass"])
    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.cost_usd == pytest.approx(0.4)


def test_the_schema_refuses_a_card_that_checks_its_own_work(tmp_path):
    """`worker: art, checker: art` would rebuild exactly the self-review §16
    exists to prevent, and it is a plausible typo."""
    from nightshift.gates import card_schema  # moved (07_portability.md §8 step 3)

    root = _repo(tmp_path)
    _charter(root, "art")
    _card(root, "tasks", "selfie", worker="art", checker="art")
    violations = [str(v) for v in card_schema.check(root)]
    assert any("same agent" in v for v in violations)


# --- the guarantee the whole design rests on --------------------------------

def _functions_spawning(source: str, callee: str) -> set[str]:
    """Names of the top-level functions that build an argv starting with
    `callee`. AST rather than text search, so a mention in a docstring or a
    comment does not count — the question is what the code *executes*."""
    import ast

    tree = ast.parse(source)
    found: set[str] = set()
    for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
            if not (isinstance(node, ast.List) and node.elts):
                continue
            first = node.elts[0]
            if isinstance(first, ast.Name) and first.id == callee:
                found.add(function.name)
    return found


def test_only_the_spawn_functions_may_execute_the_claude_cli():
    """§5 and §12: the orchestrator contains no judgment. A worker is the thing
    being *orchestrated*, not a decision procedure the runner consults — so the
    CLI is executed only where a worker is started, and nothing that decides what
    to do (select, recover, settle, backoff) may reach for it.

    Four functions now: `run_producer`, `run_checker`, `run_stale_check` and
    `run_reviewer` (automate-review-step). Each *starts a worker* — a producer,
    its checker, `stale-hunter` on one doc, or the diff reviewer on one finished
    branch — and none of them *decides* anything: `review_stage` reads the
    reviewer's verdict and routes on it (a file-state lookup), exactly as the card
    loop selects and then calls `dispatch`. The reviewer having its own spawn is
    the point — its context is built here rather than by the worker, which is what
    makes §16's blindness structural instead of a charter instruction.

    `preflight` deliberately does not count: it calls `claude_binary()`, which is
    a `shutil.which` lookup. Checking that a file exists is not an LLM call, and
    checking it *before* a card's `attempts` is spent is the reason it is there.
    """
    source = _RUNNER_SOURCE.read_text(encoding="utf-8")
    assert _functions_spawning(source, "binary") == {
        "run_producer", "run_checker", "run_stale_check", "run_reviewer"}


def test_the_deciding_functions_are_pure_file_state_lookups():
    """The stronger half of the same rule: a function that decides *what* the
    runner does must not shell out at all — not to the CLI, not to git, not to
    pytest. Its inputs are the card files and the clock."""
    import ast

    tree = ast.parse(_RUNNER_SOURCE.read_text(encoding="utf-8"))
    deciders = {"select", "_backoff_remaining", "host_capabilities", "_deadline"}
    for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        if function.name not in deciders:
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and node.attr == "run":
                value = node.value
                if isinstance(value, ast.Name) and value.id == "subprocess":
                    pytest.fail(f"{function.name} shells out; it must be a file-state lookup")


# --- the seam is generic: art and audio share it with no runner change --------
#
# §16's second seam (the producer→checker loop) is only general if the runner
# names no producer *by kind*. Correction 94 (`seam-generality-checked-not-
# claimed`) verified by hand that `art`/`audio` appear in runner.py only in
# comments and docstrings, never in a code path — which is what lets an audio
# pipeline land as two charters and a card, with ZERO runner edits, driven by
# `worker:`/`checker:` frontmatter and `HARVEST_DIRS` pointing at CLAUDE.md's
# house convention (`dungeoneer/assets/.tmp/`) rather than an art path. These two
# tests lock that: one negatively (no executed string constant names a producer
# by kind), one positively (an `audio` card drives the identical loop).

def test_no_art_or_audio_specific_literal_appears_in_runner_logic():
    """A future edit hardcoding `if card.worker == "art"`, or an art-specific
    harvest path, would silently break the generality while every other test
    stayed green. So assert no *executed* string constant in runner.py names a
    producer by kind. Comments are dropped by the parser; docstrings are excluded
    explicitly, so a legitimate mention in prose ("the art pipeline is the case")
    is not a violation — only a string the code actually runs is.
    """
    import ast

    tree = ast.parse(_RUNNER_SOURCE.read_text(encoding="utf-8"))

    # The docstring Constant of the module and of every function/class, by object
    # identity, so it can be skipped when the string constants are scanned. Only
    # these four node kinds carry a docstring — a bare string as the first
    # statement of an `if`/`for` block is code, not prose, and must still count.
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, holders) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))

    # Whole-word match: `artefact`, `start`, `chart`, `audiodriver` never trip;
    # the leading `art`/`audio` of `art-reviewer`/`audio-reviewer` is caught on
    # purpose (a hyphen is a word boundary).
    kind = re.compile(r"\b(art|audio)\b", re.IGNORECASE)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        if kind.search(node.value):
            offenders.append(f"line {node.lineno}: {node.value.strip()[:80]!r}")
    assert not offenders, (
        "runner logic names a producer by kind — the second seam must stay generic "
        "so the audio pipeline needs no runner change (correction 94):\n  "
        + "\n  ".join(offenders))


def test_an_audio_card_drives_the_same_loop_with_no_runner_change(tmp_path, monkeypatch):
    """The positive half. A card whose `worker`/`checker` are `audio`/`audio-
    reviewer` — charters that do not exist yet — both *selects* and *drives* the
    identical producer→checker loop, with nothing in the runner mentioning audio.
    Proof that adding the pipeline is two charters and a card, exactly as
    correction 94 asserts."""
    root = _worktree_repo(tmp_path)
    _charter(root, "audio")
    _charter(root, "audio-reviewer")
    _card(root, "tasks", "sfx", worker="audio", checker="audio-reviewer",
          requires="gpu-box")

    # Selectable on a machine that offers the capability, exactly like an art card.
    assert _select(root, capabilities={"gpu-box"})["sfx"].dispatchable

    calls: list[str] = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        agent = argv[argv.index("--agent") + 1]
        calls.append(agent)
        if agent == "audio":  # the producer: candidates into the house .tmp/ dir
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "hit.wav").write_bytes(b"RIFF")
        else:                 # the checker: a structured verdict off the prompt
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(
                {"verdict": "pass", "best": "hit.wav", "notes": ""}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "sfx"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.rounds == 1
    assert calls == ["audio", "audio-reviewer"]


# --- warm resume of a limit-interrupted worker (runner-worker-handover) -------
#
# Before this, a worker walled mid-stream lost everything: the worktree was
# force-removed the instant a wall was seen, it had committed nothing, and the
# next attempt cold-started from a blank prompt. These drive the three
# preservation paths — resume the session, re-enter the kept worktree, recover
# from a WIP commit — plus the progress gate that stops a card that cannot
# advance from being given its attempt back forever. All over synthetic state;
# none spawns a real worker.

def _walls_dirty(monkeypatch, *, session_id: str = "sess-1", content: str = "draft",
                 record: list | None = None):
    """A worker that does real, uncommitted work (writes a file into the worktree)
    and *then* hits a usage-limit wall — the 45-turns-then-walled case the card
    was written for. Its result JSON carries the `session_id`, exactly as the CLI
    reports it, so the runner can resume it."""
    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        if record is not None:
            record.append({"argv": argv, "prompt": prompt})
        (Path(cwd) / "wip.txt").write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 1, json.dumps({"total_cost_usd": 0.5, "session_id": session_id}),
            "Claude AI usage limit reached")
    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")


def _finishing_worker(monkeypatch, *, record: list | None = None,
                      resume_returncode: int = 0, resume_stderr: str = "",
                      session_id: str = "sess-2"):
    """A worker that finishes the card: commits, writes a `done` verdict. If
    `resume_returncode` is non-zero it fails *only* the `--resume` invocation (a
    dead session) and succeeds on the fresh re-entry, to drive the fallback."""
    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        if record is not None:
            record.append({"argv": argv, "prompt": prompt})
        if "--resume" in argv and resume_returncode != 0:
            return subprocess.CompletedProcess(
                argv, resume_returncode, json.dumps({"total_cost_usd": 0.05}), resume_stderr)
        (Path(cwd) / "wip.txt").write_text("final", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "worker: finished"], cwd=cwd, check=True)
        verdict = Path(next(l.strip() for l in prompt.splitlines()
                            if l.strip().endswith(".json")))
        verdict.parent.mkdir(parents=True, exist_ok=True)
        verdict.write_text(json.dumps({"outcome": "done", "summary": "finished"}),
                           encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"total_cost_usd": 0.2, "session_id": session_id}), "")
    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")


def test_a_walled_worker_with_dirty_work_keeps_its_worktree_and_records_the_session(
        tmp_path, monkeypatch):
    """Step 1. A worker walled 45 turns deep has real uncommitted work and a live
    session. Both are preserved: the worktree is kept (not dropped), the session
    id is recorded off the worker's own JSON, and the attempt is given back."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "keepwt")
    _walls_dirty(monkeypatch, session_id="sess-keep")

    result = runner.dispatch(root, board.find(root, "keepwt"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "limited"
    assert result.kept and result.progressed and not result.stuck
    assert (runner.worktree_root(root) / "keepwt").exists()   # kept, not dropped
    handover = runner.read_handover(root, "keepwt")
    assert handover.session_id == "sess-keep"
    assert handover.diff_hash and handover.no_progress == 0

    runner.settle(root, "keepwt", result)
    card = board.find(root, "keepwt")
    assert card.lane == "tasks"
    assert card.attempts == 0                                 # attempt given back
    assert card.fields.get("branch") == "ai/keepwt"           # branch kept (it is state)
    assert (runner.worktree_root(root) / "keepwt").exists()   # still there to re-enter
    assert runner.read_handover(root, "keepwt").session_id == "sess-keep"


def test_a_wall_at_the_first_call_with_no_changes_drops_everything_as_before(
        tmp_path, monkeypatch):
    """Step 6, the regression guard. A wall at the very first API call, with
    nothing done, must behave exactly as it did before warm resume existed:
    worktree and branch dropped, attempt given back, nothing preserved."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "empty")
    _fake_worker(monkeypatch, commit=False, returncode=1,
                 stderr="Claude AI usage limit reached")

    result = runner.dispatch(root, board.find(root, "empty"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "limited" and not result.kept
    assert not (runner.worktree_root(root) / "empty").exists()      # dropped
    assert runner.read_handover(root, "empty").session_id == ""     # nothing preserved

    runner.settle(root, "empty", result)
    card = board.find(root, "empty")
    assert card.lane == "tasks"
    assert card.attempts == 0
    assert "branch" not in card.fields          # branch dropped, pristine
    assert "## Error" not in card.text          # nothing to answer for


def test_the_next_dispatch_of_an_interrupted_card_resumes_its_session(tmp_path, monkeypatch):
    """Step 2. After a warm interruption the next dispatch resumes the session
    (`--resume <id>`) into the kept worktree, rather than cold-starting."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "resumecard")
    _walls_dirty(monkeypatch, session_id="sess-resume")
    runner.settle(root, "resumecard",
                  runner.dispatch(root, board.find(root, "resumecard"),
                                  "development_team", "sonnet", 0.0, 120))

    seen: list = []
    _finishing_worker(monkeypatch, record=seen)
    result = runner.dispatch(root, board.find(root, "resumecard"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "review"
    argv = seen[0]["argv"]
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-resume"
    # and it happened *in the kept worktree*, not a fresh checkout
    # (the finishing worker committed there and gates/tests ran green)
    assert board.find(root, "resumecard").lane == "tasks"   # settle not yet called


def test_a_failing_resume_falls_through_to_a_fresh_session_in_the_same_worktree(
        tmp_path, monkeypatch):
    """Step 3, the resilience requirement. A `--resume` that errors (a dead
    session) must not burn the attempt discovering it — it falls through, inside
    the same attempt, to a fresh session that re-enters the kept worktree and
    reads its uncommitted diff."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "fallbackcard")
    _walls_dirty(monkeypatch, session_id="sess-dead")
    runner.settle(root, "fallbackcard",
                  runner.dispatch(root, board.find(root, "fallbackcard"),
                                  "development_team", "sonnet", 0.0, 120))

    seen: list = []
    _finishing_worker(monkeypatch, record=seen, resume_returncode=1,
                      resume_stderr="No conversation found with session ID sess-dead")
    result = runner.dispatch(root, board.find(root, "fallbackcard"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "review"
    assert len(seen) == 2                          # resume, then the fresh fallback
    assert "--resume" in seen[0]["argv"] and "--resume" not in seen[1]["argv"]
    assert result.cost_usd == pytest.approx(0.25)  # both runs' cost summed
    # the fallback prompt hands the worker the uncommitted diff, not a blank slate
    assert "uncommitted work is still in this worktree" in seen[1]["prompt"]


def test_commit_wip_banks_dirty_work_and_is_a_noop_when_clean(tmp_path):
    """The deepest-persistence primitive (path 3). It commits the worktree's
    uncommitted work as `wip: <card> interrupted`, and does nothing when the tree
    is already clean, so it is safe to call defensively."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "wipcard")
    tree, branch, mode = runner.prepare_worktree(
        root, board.find(root, "wipcard"), "development_team")
    assert mode == runner.FRESH
    assert runner.commit_wip(root, tree, "wipcard") is False        # clean → no-op
    (tree / "draft.txt").write_text("half done", encoding="utf-8")
    assert runner.commit_wip(root, tree, "wipcard") is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=tree,
                         capture_output=True, text=True).stdout
    assert "wip: wipcard interrupted" in log
    assert runner.commit_wip(root, tree, "wipcard") is False        # nothing left


def _seed_wip_branch(root: Path, tmp_path: Path, card_id: str, content: str) -> None:
    """Synthetic disk-cap demotion state: branch ai/<id> carries a `wip:` commit,
    a handover marker is on disk, and the worktree directory is absent — exactly
    what runner-prune-run-dirs's demotion leaves behind."""
    seed = tmp_path / f"seed-{card_id}"
    subprocess.run(["git", "worktree", "add", "-b", f"ai/{card_id}", str(seed),
                    "development_team"], cwd=root, check=True)
    (seed / "wip.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", f"wip: {card_id} interrupted"], cwd=seed, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(seed)], cwd=root, check=True)
    runner.write_handover(root, card_id,
                          runner.Handover(session_id="", diff_hash="stale", no_progress=0))


def test_a_lost_worktree_is_rebuilt_from_its_wip_commit(tmp_path):
    """Step 4, the rebuild. With the worktree gone but the branch's `wip:` commit
    intact, `prepare_worktree` cuts a fresh checkout from the branch (mode
    FROM_WIP) carrying the interrupted work, not a blank one from base."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "wtrebuild")
    _seed_wip_branch(root, tmp_path, "wtrebuild", "interrupted work")

    tree, branch, mode = runner.prepare_worktree(
        root, board.find(root, "wtrebuild"), "development_team")
    assert mode == runner.FROM_WIP
    assert (tree / "wip.txt").read_text(encoding="utf-8") == "interrupted work"


def test_a_dispatch_from_a_wip_commit_hands_over_a_progress_note_and_does_not_resume(
        tmp_path, monkeypatch):
    """Step 4, the handover. A dispatch of a card in the WIP-recovered state gives
    the worker a `## Progress` note pointing at the commit, with no `--resume`
    (the session did not survive)."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "wtnote")
    _seed_wip_branch(root, tmp_path, "wtnote", "interrupted work")

    seen: list = []
    _finishing_worker(monkeypatch, record=seen)
    result = runner.dispatch(root, board.find(root, "wtnote"),
                             "development_team", "sonnet", 0.0, 120)
    assert result.outcome == "review"
    assert "--resume" not in seen[0]["argv"]
    assert "recovered from a WIP commit" in seen[0]["prompt"]


def test_a_resume_that_moves_nothing_spends_the_attempt_then_files_the_card(
        tmp_path, monkeypatch):
    """Step 5, the progress gate. A resume that leaves the working tree
    byte-identical is not free: the first repeat spends the attempt, and once the
    tree has not moved for NO_PROGRESS_STOP windows the card is filed to failed/
    rather than being given its attempt back at the head of the queue forever."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "stuckcard")
    _walls_dirty(monkeypatch, session_id="s", content="frozen")   # same bytes every time

    # wall 1 — first interruption: given back, worktree kept.
    runner.settle(root, "stuckcard",
                  runner.dispatch(root, board.find(root, "stuckcard"),
                                  "development_team", "sonnet", 0.0, 120))
    assert board.find(root, "stuckcard").attempts == 0

    # wall 2 — no movement: the attempt is SPENT (not rewound), card stays in tasks/.
    r2 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    assert r2.kept and not r2.progressed and not r2.stuck
    runner.settle(root, "stuckcard", r2)
    card = board.find(root, "stuckcard")
    assert card.lane == "tasks" and card.attempts == 1

    # wall 3 — still no movement: the breaker trips and the card leaves the head.
    r3 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    assert r3.stuck
    runner.settle(root, "stuckcard", r3)
    assert board.find(root, "stuckcard").lane == "failed"


def test_a_kept_worktree_is_not_pruned_by_this_runner(tmp_path, monkeypatch):
    """Step 7. Warm resume keeps worktrees between attempts; capping how many pile
    up is runner-prune-run-dirs's job (Decision 3), not this card's. So this
    runner must grow no second cleanup path — a kept worktree survives the
    dispatch of other cards untouched, still resumable."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "kept")
    _card(root, "tasks", "sibling")
    _walls_dirty(monkeypatch, session_id="s")
    runner.settle(root, "kept",
                  runner.dispatch(root, board.find(root, "kept"),
                                  "development_team", "sonnet", 0.0, 120))
    assert (runner.worktree_root(root) / "kept").exists()

    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})
    runner.settle(root, "sibling",
                  runner.dispatch(root, board.find(root, "sibling"),
                                  "development_team", "sonnet", 0.0, 120))
    assert (runner.worktree_root(root) / "kept").exists()            # untouched — no cap here
    assert not (runner.worktree_root(root) / "sibling").exists()     # resolved, dropped as normal
    assert runner.read_handover(root, "kept").session_id == "s"      # still resumable


def test_the_worktree_hash_ignores_where_the_worktree_lives_but_not_its_content(tmp_path):
    """The progress gate's instrument: two byte-identical working trees hash the
    same; a moved byte changes it. Untracked content counts (a worker's first
    output is untracked before it commits)."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "hashcard")
    tree, _, _ = runner.prepare_worktree(root, board.find(root, "hashcard"), "development_team")
    base = runner._worktree_state_hash(tree)
    (tree / "a.txt").write_text("one", encoding="utf-8")
    after_add = runner._worktree_state_hash(tree)
    assert after_add != base                                  # untracked content is seen
    (tree / "a.txt").write_text("two", encoding="utf-8")
    assert runner._worktree_state_hash(tree) != after_add     # a changed byte moves it
    (tree / "a.txt").write_text("one", encoding="utf-8")
    assert runner._worktree_state_hash(tree) == after_add     # deterministic, no drift


# --------------------------------------------------------------------------
# Pruning (`runner-prune-run-dirs`)
# --------------------------------------------------------------------------
#
# The rule under test: `failed/` is reached by the runner itself, so it is
# pruned eagerly, at both of its in-run paths (MAX_ATTEMPTS and the `stuck`
# breaker); `done/` is reached by Karel, by hand, so it is swept at the next
# startup instead; a worktree ceiling backstops the one lifecycle a run-dir
# rule cannot reach. No test here mirrors arithmetic — every one drives a real
# `dispatch()`/`settle()`/`run()` call, or a real `git worktree`.

def test_a_card_retired_via_max_attempts_has_its_run_dir_pruned_eagerly(tmp_path, monkeypatch):
    """The pre-existing `failed/` path. The moment the third failure lands the
    card in `failed/`, `.ai/runs/<id>/` is gone — the numbers it held now live
    on the card's own `## Telemetry` section instead."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    _fake_worker(monkeypatch, commit=False, verdict=None)

    for expected_lane in ("tasks", "tasks", "failed"):
        card = board.find(root, "probe")
        card.write({"finished": None})
        result = runner.dispatch(root, card, "development_team", "sonnet", 5.0, 120)
        runner.settle(root, "probe", result)
        assert board.find(root, "probe").lane == expected_lane

    assert not (root / ".ai" / "runs" / "probe").exists()
    assert "## Telemetry" in board.find(root, "probe").text


def test_a_card_filed_as_stuck_is_pruned_eagerly_in_the_same_settle(tmp_path, monkeypatch):
    """The second in-run path to `failed/`, added by `runner-worker-handover`
    after this card was first scoped: the no-progress breaker must prune just
    as eagerly as the MAX_ATTEMPTS path, not wait for the next-startup sweep."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "stuckcard")
    _walls_dirty(monkeypatch, session_id="s", content="frozen")

    runner.settle(root, "stuckcard", runner.dispatch(
        root, board.find(root, "stuckcard"), "development_team", "sonnet", 0.0, 120))
    r2 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    runner.settle(root, "stuckcard", r2)
    assert (root / ".ai" / "runs" / "stuckcard").exists()   # still in tasks/ — not pruned yet

    r3 = runner.dispatch(root, board.find(root, "stuckcard"),
                         "development_team", "sonnet", 0.0, 120)
    assert r3.stuck
    runner.settle(root, "stuckcard", r3)
    assert board.find(root, "stuckcard").lane == "failed"
    assert not (root / ".ai" / "runs" / "stuckcard").exists()
    assert "## Telemetry" in board.find(root, "stuckcard").text


def test_a_hand_moved_done_card_is_pruned_by_the_next_runner_startup(tmp_path, monkeypatch):
    """`done/` is reached by Karel, by hand — the runner never moves a card
    there itself — so its run-dir is not pruned eagerly; it is swept once at
    the next startup instead, which is what `run()` does right after
    `recover()`."""
    root = _loaded_board(tmp_path)
    _ignore_runs(root)
    _card(root, "done", "shipped")
    run_dir = root / ".ai" / "runs" / "shipped" / "attempt-1"
    run_dir.mkdir(parents=True)
    (run_dir / "worker-1.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert not (root / ".ai" / "runs" / "shipped").exists()


def test_the_sweep_also_drops_an_orphaned_worktree_left_on_a_terminal_card(tmp_path, monkeypatch):
    """The sweep's worktree half: the only physical worktree that can outlive a
    dispatch today (absent `runner-worker-handover`'s kept-worktree machinery in
    this repo) is an orphan from a crash mid-dispatch — `recover()` fixes up a
    card's fields but never touches its checkout. A card sitting in `failed/`
    with such an orphan gets it dropped by the sweep, branch preserved."""
    root = _loaded_board(tmp_path)
    _card(root, "failed", "orphan")
    subprocess.run(["git", "worktree", "add", "-b", "ai/orphan",
                    str(runner.worktree_root(root) / "orphan"), "development_team"],
                   cwd=root, check=True)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert not (runner.worktree_root(root) / "orphan").exists()
    kept = subprocess.run(["git", "branch", "--list", "ai/orphan"], cwd=root,
                          capture_output=True, text=True).stdout
    assert "ai/orphan" in kept   # only the checkout is dropped, the branch survives


def test_a_card_in_tasks_with_attempts_keeps_its_run_dirs_through_the_sweep(tmp_path):
    """`sweep_terminal_cards` only ever looks at `done/` and `failed/` — a card
    still in `tasks/` keeps every attempt directory it has."""
    root = _repo(tmp_path)
    _card(root, "tasks", "inflight")
    run_dir = root / ".ai" / "runs" / "inflight" / "attempt-1"
    run_dir.mkdir(parents=True)
    (run_dir / "worker-1.json").write_text("{}", encoding="utf-8")

    swept = runner.sweep_terminal_cards(root)

    assert swept == []
    assert run_dir.is_dir()


def test_cap_run_dir_keeps_only_the_last_n_attempts(tmp_path):
    """The in-flight backstop — N=3 matches MAX_ATTEMPTS, so nothing a card
    actually accumulates in the ordinary run is ever pruned by this; it only
    fires for a card that somehow accumulates more."""
    root = _repo(tmp_path)
    for n in (1, 2, 3, 4, 5):
        (root / ".ai" / "runs" / "capcard" / f"attempt-{n}").mkdir(parents=True)

    runner.cap_run_dir(root, "capcard", keep=3)

    remaining = sorted(p.name for p in (root / ".ai" / "runs" / "capcard").iterdir())
    assert remaining == ["attempt-3", "attempt-4", "attempt-5"]


def test_cap_run_dirs_in_flight_only_touches_tasks_lane_cards(tmp_path):
    root = _repo(tmp_path)
    _card(root, "tasks", "inflight")
    for n in (1, 2, 3, 4):
        (root / ".ai" / "runs" / "inflight" / f"attempt-{n}").mkdir(parents=True)

    runner.cap_run_dirs_in_flight(root, keep=3)

    remaining = sorted(p.name for p in (root / ".ai" / "runs" / "inflight").iterdir())
    assert remaining == ["attempt-2", "attempt-3", "attempt-4"]


def test_prune_run_dir_and_prune_worktree_are_no_ops_when_absent(tmp_path):
    """Pruning a run dir or worktree that does not exist is a no-op, not an
    error — a sweep over every terminal card must not care which of them ever
    actually had a run dir or a surviving worktree."""
    root = _repo(tmp_path)
    runner.prune_run_dir(root, "never-existed")               # must not raise
    runner.prune_worktree_if_present(root, "never-existed")   # neither must this


def test_status_json_log_and_lock_survive_pruning_the_running_cards_own_dir(tmp_path):
    """Never prune the current run's own files — `status.json`, the day's log
    tee, or `.lock` — even when the card being pruned is the one currently
    running. They are siblings of `.ai/runs/<id>/`, not inside it, so a prune of
    the card's own directory structurally cannot reach them."""
    root = _repo(tmp_path)
    runs = root / ".ai" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "status.json").write_text("{}", encoding="utf-8")
    (runs / ".lock").write_text("12345", encoding="utf-8")
    (runs / "2026-07-24.log").write_text("log\n", encoding="utf-8")
    (runs / "running-now" / "attempt-1").mkdir(parents=True)
    (runs / "running-now" / "attempt-1" / "worker-1.json").write_text("{}", encoding="utf-8")

    runner.prune_run_dir(root, "running-now")

    assert (runs / "status.json").is_file()
    assert (runs / ".lock").is_file()
    assert (runs / "2026-07-24.log").is_file()
    assert not (runs / "running-now").exists()


def test_prune_old_run_logs_keeps_the_last_n_days(tmp_path):
    """The `<date>.log` tee's own, simpler retention rule — keep the last N
    days — separate from run-dir pruning because a log is not tied to any one
    card's lane."""
    root = _repo(tmp_path)
    runs = root / ".ai" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    keep_date = (today - dt.timedelta(days=4)).isoformat()
    drop_date = (today - dt.timedelta(days=40)).isoformat()
    (runs / f"{keep_date}.log").write_text("keep\n", encoding="utf-8")
    (runs / f"{drop_date}.log").write_text("drop\n", encoding="utf-8")
    (runs / "not-a-date.log").write_text("ignored\n", encoding="utf-8")

    runner.prune_old_run_logs(root, days=14)

    assert (runs / f"{keep_date}.log").is_file()
    assert not (runs / f"{drop_date}.log").exists()
    assert (runs / "not-a-date.log").is_file()   # a name the pattern doesn't match is left alone


def test_run_calls_all_four_housekeeping_steps_at_startup(tmp_path, monkeypatch):
    """The wiring itself, pinned directly: a regression that dropped one of
    these calls from `run()` would look exactly like a quiet night, and no
    single function's own unit test would catch it."""
    root = _loaded_board(tmp_path)
    _ignore_runs(root)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    calls: list[str] = []
    monkeypatch.setattr(runner, "sweep_terminal_cards", lambda r: calls.append("sweep") or [])
    monkeypatch.setattr(runner, "cap_run_dirs_in_flight", lambda r: calls.append("cap"))
    monkeypatch.setattr(runner, "prune_old_run_logs", lambda r: calls.append("logs"))
    monkeypatch.setattr(runner, "enforce_worktree_ceiling", lambda r: calls.append("ceiling") or [])

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["sweep", "cap", "logs", "ceiling"]


def test_the_housekeeping_steps_do_not_run_on_a_dry_run(tmp_path, monkeypatch):
    """A dry run writes nothing and must not prune anything either — the same
    guard `recover()` already gets."""
    root = _loaded_board(tmp_path)
    _ignore_runs(root)
    calls: list[str] = []
    monkeypatch.setattr(runner, "sweep_terminal_cards", lambda r: calls.append("sweep") or [])
    monkeypatch.setattr(runner, "cap_run_dirs_in_flight", lambda r: calls.append("cap"))
    monkeypatch.setattr(runner, "prune_old_run_logs", lambda r: calls.append("logs"))
    monkeypatch.setattr(runner, "enforce_worktree_ceiling", lambda r: calls.append("ceiling") or [])

    runner.run(root, runner._parser(root).parse_args(["--base", "development_team", "--dry-run"]))

    assert calls == []


def test_demote_is_silent_when_there_is_nothing_to_bank(tmp_path, monkeypatch):
    """`commit_wip` is already a no-op on a clean tree; the demotion path must
    stay silent through it rather than logging a phantom failure."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "cleanwt")
    tree, _, mode = runner.prepare_worktree(root, board.find(root, "cleanwt"),
                                            "development_team")
    assert mode == runner.FRESH
    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))

    runner.demote_worktree_to_wip(root, tree, "cleanwt")

    assert logged == []
    assert not tree.exists()


def test_demote_logs_but_still_drops_when_the_wip_commit_genuinely_fails(tmp_path, monkeypatch):
    """A real commit failure (a rejecting hook, a full disk) must not be quiet —
    that would silently lose the uncommitted diff the demotion path exists to
    preserve — but the checkout is dropped regardless, because the ceiling has
    to be enforced either way."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "failwt")
    tree, _, mode = runner.prepare_worktree(root, board.find(root, "failwt"),
                                            "development_team")
    assert mode == runner.FRESH
    (tree / "draft.txt").write_text("uncommitted", encoding="utf-8")
    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    monkeypatch.setattr(runner, "commit_wip", lambda root_, tree_, card_id: False)

    runner.demote_worktree_to_wip(root, tree, "failwt")

    assert logged and "WIP commit failed" in logged[0]
    assert not tree.exists()


def test_demote_calls_the_real_commit_wip_primitive_not_a_lookalike(tmp_path, monkeypatch):
    """The review finding this attempt fixes: an earlier version reimplemented
    `git add -A` + `git commit` instead of calling `commit_wip` — the primitive
    `runner-worker-handover` built for exactly this caller, named in its own
    docstring. Assert the real function is actually invoked, not a reimplementation
    that merely behaves like it."""
    root = _worktree_repo(tmp_path)
    _card(root, "tasks", "callcard")
    tree, _, mode = runner.prepare_worktree(root, board.find(root, "callcard"),
                                            "development_team")
    assert mode == runner.FRESH
    (tree / "draft.txt").write_text("data", encoding="utf-8")
    real_commit_wip = runner.commit_wip
    calls: list[tuple] = []

    def spy(root_, tree_, card_id):
        calls.append((root_, tree_, card_id))
        return real_commit_wip(root_, tree_, card_id)

    monkeypatch.setattr(runner, "commit_wip", spy)

    runner.demote_worktree_to_wip(root, tree, "callcard")

    assert calls == [(root, tree, "callcard")]
    log = subprocess.run(["git", "log", "--oneline", "ai/callcard"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "wip: callcard interrupted" in log


def test_the_kept_worktree_ceiling_demotes_the_oldest_first(tmp_path, monkeypatch):
    """Decision 3's backstop. More limit-interrupted cards than the ceiling
    allows demotes the oldest kept worktree(s) to git-WIP-only — checkout
    dropped, branch (with its banked diff) kept — until at or under the
    ceiling. Oldest is by directory mtime, stamped *after* every write: stamping
    before it would be silently overwritten by the write itself."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    ids = ["wt-a", "wt-b", "wt-c", "wt-d"]
    for cid in ids:
        _card(root, "tasks", cid)
        _walls_dirty(monkeypatch, session_id="s")
        runner.settle(root, cid, runner.dispatch(
            root, board.find(root, cid), "development_team", "sonnet", 0.0, 120))
        assert board.find(root, cid).lane == "tasks"   # kept — warm, not resolved

    for index, cid in enumerate(ids):
        stamp = 1_700_000_000 + index * 1_000   # strictly increasing; wt-a is oldest
        tree = runner.worktree_root(root) / cid
        os.utime(tree, (stamp, stamp))

    demoted = runner.enforce_worktree_ceiling(root, ceiling=2)

    assert demoted == ["wt-a", "wt-b"]
    assert not (runner.worktree_root(root) / "wt-a").exists()
    assert not (runner.worktree_root(root) / "wt-b").exists()
    assert (runner.worktree_root(root) / "wt-c").exists()
    assert (runner.worktree_root(root) / "wt-d").exists()
    log = subprocess.run(["git", "log", "--oneline", "ai/wt-a"], cwd=root,
                         capture_output=True, text=True).stdout
    assert "wip: wt-a interrupted" in log   # the demoted card's work is banked, not lost


def test_enforce_worktree_ceiling_is_a_noop_when_at_or_under_it(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "onlyone")
    _walls_dirty(monkeypatch, session_id="s")
    runner.settle(root, "onlyone", runner.dispatch(
        root, board.find(root, "onlyone"), "development_team", "sonnet", 0.0, 120))

    demoted = runner.enforce_worktree_ceiling(root, ceiling=runner.WORKTREE_KEEP_CEILING)

    assert demoted == []
    assert (runner.worktree_root(root) / "onlyone").exists()
# --- the review stage (automate-review-step) --------------------------------
#
# A new, distinct always-on stage that runs after gates+tests pass: the runner
# builds the diff reviewer's context, spawns it, and turns the two-way verdict
# into a routed lane — needs-decision/ (a choice for Karel) or merge → testing/
# (nothing needs his judgment first). These drive the routing over synthetic
# state: the reviewer is stubbed (no live dispatch) and the merge is stubbed (no
# live merge), the way every other runner test replaces its one non-deterministic
# seam. The safety property the whole card turns on — nothing reaches done/ from
# here, only via testing/ — has its own test at the end.

def _reviewed_branch(root: Path, tmp_path: Path, card_id: str = "probe",
                     *, worker: str = "code-thread", commit: bool = True,
                     verify: str = "play") -> board.Card:
    """A card in tasks/ whose branch `ai/<id>` carries a real commit — the state a
    card is in when the review stage runs (dispatch has finished, settle has not).
    With `commit=False` the branch is level with base, standing in for an
    artefact-only card (art) that has no diff to review."""
    _charter(root, worker)
    _card(root, "tasks", card_id, worker=worker, verify=verify)
    subprocess.run(["git", "branch", f"ai/{card_id}", "development_team"], cwd=root, check=True)
    if commit:
        seed = tmp_path / f"seed-{card_id}"
        subprocess.run(["git", "worktree", "add", str(seed), f"ai/{card_id}"],
                       cwd=root, check=True)
        (seed / "feature.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
        subprocess.run(["git", "commit", "-qm", "worker: did the thing"], cwd=seed, check=True)
        subprocess.run(["git", "worktree", "remove", "--force", str(seed)], cwd=root, check=True)
    card = board.find(root, card_id)
    card.write({"branch": f"ai/{card_id}", "attempts": "1"})
    return board.find(root, card_id)


def _stub_reviewer(monkeypatch, verdict: dict, cost: float = 0.2,
                   wall: "limits.Wall | None" = None) -> list:
    spawned: list = []

    def fake(root, card, out_dir, model, base, branch, card_budget, timeout):
        spawned.append(branch)
        return verdict, cost, wall

    monkeypatch.setattr(runner, "run_reviewer", fake)
    return spawned


def test_an_ok_verdict_becomes_a_reviewed_outcome(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {"verdict": "ok", "notes": "clean"}, cost=0.2)

    result = runner.review_stage(root, card, runner.Dispatch("review", "2 commits", 0.5),
                                 "development_team", 0.0, 120)
    assert result.outcome == "reviewed"
    assert result.cost_usd == pytest.approx(0.7)   # dispatch 0.5 + reviewer 0.2, once


def test_a_needs_decision_verdict_carries_the_reviewers_question(tmp_path, monkeypatch):
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {"verdict": "needs_decision",
                                 "question": "worker chose HP; the card allows HP or heat"})

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.1),
                                 "development_team", 0.0, 120)
    assert result.outcome == "needs_decision"
    assert "HP or heat" in result.detail


def test_the_review_stage_skips_an_artefact_only_card(tmp_path, monkeypatch):
    """Art produces no commit on its branch, so there is no diff to review and
    nothing to merge. The stage leaves it exactly as before — a human eye at
    review/ — and never spawns the reviewer (this card does not change art)."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path, "icon", worker="art", commit=False)
    spawned = _stub_reviewer(monkeypatch, {"verdict": "ok"})

    result = runner.review_stage(root, card, runner.Dispatch("review", "art done", 0.3),
                                 "development_team", 0.0, 120)
    assert result.outcome == "review"             # unchanged
    assert result.cost_usd == pytest.approx(0.3)  # reviewer never ran, no added cost
    assert spawned == []


def test_the_review_stage_falls_back_to_review_on_a_wall(tmp_path, monkeypatch):
    """The plan running out mid-review is not the card's fault, and gates+tests
    already passed. It lands in review/ for a manual look rather than a guess."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {}, cost=0.3,
                   wall=limits.Wall(limits.SESSION, None, "usage limit reached"))

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.1),
                                 "development_team", 0.0, 120)
    assert result.outcome == "review"
    assert result.cost_usd == pytest.approx(0.4)   # cost still carried forward


def test_the_review_stage_falls_back_to_review_on_an_unusable_verdict(tmp_path, monkeypatch):
    """No verdict file, or a verdict the runner cannot read, degrades to the
    pre-existing behaviour — a human review at review/ — never to a guessed
    routing (§12)."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    _stub_reviewer(monkeypatch, {}, cost=0.0)

    result = runner.review_stage(root, card, runner.Dispatch("review", "x", 0.0),
                                 "development_team", 0.0, 120)
    assert result.outcome == "review"


def test_the_reviewer_is_never_shown_the_workers_prompt(tmp_path, monkeypatch):
    """§16: an agent that knows what was intended sees what was intended. The
    runner builds the reviewer's context — the diff, the criteria, the repo — and
    there is no path for the worker's prompt or transcript to reach it, because
    the runner never puts them there."""
    root = _worktree_repo(tmp_path)
    _tier_binding(root)
    card = _reviewed_branch(root, tmp_path)
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        seen["argv"] = argv
        seen["prompt"] = prompt
        target = Path(next(l.strip() for l in prompt.splitlines()
                           if l.strip().endswith(".json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"verdict": "ok", "notes": ""}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.review_stage(root, card, runner.Dispatch("review", "x", 0.0),
                        "development_team", 0.0, 120)

    argv = seen["argv"]
    prompt = seen["prompt"]
    assert argv[argv.index("--agent") + 1] == runner.REVIEWER_AGENT
    # a line unique to the worker's dispatch prompt, never present in the review one
    assert "Commit your work on this branch" not in prompt
    assert "Do not move the card" not in prompt
    # …but it DOES get the acceptance criteria verbatim off the card
    assert "the gates are green" in prompt
    # Structural blindness, not a charter promise: the only directory handed to
    # the reviewer is a throwaway checkout, never the main repo (whose gitignored
    # `.ai/runs/` physically holds the worker's prompt and transcript).
    add_dirs = [argv[i + 1].replace("\\", "/") for i, a in enumerate(argv) if a == "--add-dir"]
    assert add_dirs and all("_review-probe" in d for d in add_dirs)
    assert all(str(root.resolve()).replace("\\", "/") != d for d in add_dirs)
    assert all(".ai/runs" not in d for d in add_dirs)


# --- settling the review stage's outcomes -----------------------------------

def test_settle_reviewed_merges_and_lands_in_testing(tmp_path, monkeypatch):
    """An `ok` verdict: the runner merges `ai/<id>` into the integration branch
    and the card lands in testing/, awaiting Karel — the whole point of the card."""
    root = _worktree_repo(tmp_path)
    card = _reviewed_branch(root, tmp_path)
    card.write({"started": "2026-07-24T03:00:00"})
    calls: list = []
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600:
                        (calls.append((branch, base)) or (True, "merged")))

    note = runner.settle(root, "probe", runner.Dispatch("reviewed", "clean"))
    settled = board.find(root, "probe")
    assert settled.lane == "testing"
    assert settled.fields["state"] == "testing"
    assert not settled.fields.get("started")
    assert calls == [("ai/probe", runner.default_base(root))]   # rebased onto + merged into it
    assert "testing/" in note


def test_settle_reviewed_but_unmergeable_goes_to_review_not_testing(tmp_path, monkeypatch):
    """Reviewed ok, but the branch will not merge — blocked on a sibling that
    landed first (03_board.md §1: that is exactly what review/ means). It must go
    to review/ for a human to resolve the merge, never silently to testing/ as if
    it had merged, and never to done/."""
    root = _worktree_repo(tmp_path)
    card = _reviewed_branch(root, tmp_path)
    card.write({"started": "2026-07-24T03:00:00"})
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600:
                        (False, "conflict in board.py"))

    note = runner.settle(root, "probe", runner.Dispatch("reviewed", "clean"))
    settled = board.find(root, "probe")
    assert settled.lane == "review"
    assert "could not be rebased" in settled.text
    assert "conflict in board.py" in note


def test_settle_needs_decision_files_a_schema_valid_question(tmp_path):
    """The reviewer's ambiguity reaches Karel: the card lands in needs-decision/
    with a `## Question`, which `card_schema` requires there and would reject
    without."""
    from nightshift.gates import card_schema  # moved (07_portability.md §8 step 3)

    root = _worktree_repo(tmp_path)
    card = _reviewed_branch(root, tmp_path)
    card.write({"started": "2026-07-24T03:00:00"})

    runner.settle(root, "probe", runner.Dispatch(
        "needs_decision", "worker chose HP; the card allows HP or heat — "
        "HP means A, heat means B. Which did you intend?"))
    settled = board.find(root, "probe")
    assert settled.lane == "needs-decision"
    assert "## Question" in settled.text
    assert "HP or heat" in settled.text
    probe_violations = [str(v) for v in card_schema.check(root) if "probe" in str(v)]
    assert probe_violations == []


def test_only_a_verify_review_card_reaches_done_from_this_stage(tmp_path, monkeypatch):
    """`done/` used to be unreachable from this stage — testing/ was the only exit,
    which is what `unplayable-cards-still-land-in-testing` was about: eleven of the
    sixteen cards waiting there had no surface Karel could exercise. It is reachable
    now, but only for a card that *declares* `verify: review`, and only on a clean
    merge. Drive every routing the stage can produce and pin where each one lands."""
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600: (True, "merged"))
    cases = {
        "needs_decision": ("needs-decision", "play", runner.Dispatch("needs_decision", "q?")),
        "reviewed-play": ("testing", "play", runner.Dispatch("reviewed", "ok")),
        "reviewed-review": ("done", "review", runner.Dispatch("reviewed", "ok")),
    }
    for i, (label, (expected_lane, verify, result)) in enumerate(cases.items()):
        sub = tmp_path / f"case{i}"
        sub.mkdir()
        root = _worktree_repo(sub)
        _reviewed_branch(root, sub, verify=verify)
        runner.settle(root, "probe", result)
        assert board.find(root, "probe").lane == expected_lane, label

    # A card that cannot merge goes to review/ from either value: "a human must
    # resolve a merge" is true whatever the card declares about verification.
    for i, verify in enumerate(("play", "review")):
        sub = tmp_path / f"unmergeable{i}"
        sub.mkdir()
        root = _worktree_repo(sub)
        _reviewed_branch(root, sub, verify=verify)
        monkeypatch.setattr(runner, "rebase_and_merge",
                            lambda r, card, branch, base, test_timeout=600: (False, "conflict"))
        runner.settle(root, "probe", runner.Dispatch("reviewed", "ok"))
        assert board.find(root, "probe").lane == "review", verify


def test_a_play_card_lands_carrying_the_workers_scenario(tmp_path, monkeypatch):
    """The other half of the split: a card that does reach Karel's desk arrives
    with a `## How to test` written by the worker that built it, not bare."""
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600: (True, "merged"))
    root = _worktree_repo(tmp_path)
    _reviewed_branch(root, tmp_path, verify="play")
    runner.settle(root, "probe", runner.Dispatch(
        "reviewed", "ok", how_to_test="Start a run, open the hotbar, expect two slots."))
    landed = board.find(root, "probe")
    assert "## How to test" in landed.text
    assert "expect two slots" in landed.text


def test_a_night_lands_a_reviewed_ok_card_in_testing(tmp_path, monkeypatch):
    """End to end through `run()`: a code card passes gates+tests, the reviewer
    says ok, and the runner merges and lands it in testing/ — the night's happy
    path this card exists to automate. The producer runs for real through the
    faked CLI; the reviewer and the merge are stubbed (no live dispatch, no live
    merge)."""
    root = _loaded_board(tmp_path, "feat")
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "done"})
    monkeypatch.setattr(runner, "run_reviewer",
                        lambda *a, **k: ({"verdict": "ok", "notes": "clean"}, 0.1, None))
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600: (True, "merged"))

    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))
    assert board.find(root, "feat").lane == "testing"


def test_merge_branch_refuses_when_the_checkout_is_on_the_wrong_branch(tmp_path):
    """`merge_branch` never merges into whatever branch happens to be checked out.
    The main checkout must be on the integration branch — the runner is started
    there — and a mismatch is refused with a reason rather than merging blindly.
    (No live merge: the guard returns before any git merge is attempted.)"""
    root = _worktree_repo(tmp_path)          # HEAD is on development_team
    ok, why = runner.merge_branch(root, "ai/probe", "some-other-branch")
    assert not ok
    assert "not the integration branch" in why


# --- streaming the worker's output (runner-stream-worker-output) -----------
#
# `_run_worker` used to be one `subprocess.run(..., capture_output=True)` call,
# so nothing about a running worker existed on disk until it exited. These
# drive the real `Popen`-based rewrite against tiny stub subprocesses — not
# mocks — because the whole point is behaviour a mock would agree with either
# way: bytes landing on disk *while* the process is still alive, a timeout that
# actually kills the child, and a join that does not hang when a grandchild the
# child spawned is still holding the pipe open.

class TestTerminalResult:
    """`_terminal_result` is what replaced `json.loads(proc.stdout)` once stdout
    could be a JSONL stream instead of one blob: the last line on the stream
    that parses as a JSON object, scanning from the end so a stream cut short
    mid-line does not stop the true last complete line from being found."""

    def test_a_single_object_is_read_exactly_like_the_old_output_format_json(self):
        """The old shape — every existing test double still hands this back —
        is one line, so the last line is the only line and this must agree
        with a plain `json.loads` exactly."""
        stdout = json.dumps({"total_cost_usd": 0.5, "session_id": "s1"})
        assert runner._terminal_result(stdout) == {"total_cost_usd": 0.5, "session_id": "s1"}

    def test_a_multi_event_stream_reads_the_last_line_not_the_first(self):
        stdout = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"content": []}}),
            json.dumps({"type": "result", "total_cost_usd": 1.23,
                       "terminal_reason": "completed"}),
        ])
        assert runner._terminal_result(stdout) == {
            "type": "result", "total_cost_usd": 1.23, "terminal_reason": "completed"}

    def test_trailing_blank_lines_do_not_hide_the_result(self):
        stdout = json.dumps({"type": "result", "total_cost_usd": 2.0}) + "\n\n\n"
        assert runner._terminal_result(stdout)["total_cost_usd"] == 2.0

    def test_unparseable_stdout_is_an_empty_dict_not_an_exception(self):
        assert runner._terminal_result("not json at all\nstill not json") == {}

    def test_empty_stdout_is_an_empty_dict(self):
        assert runner._terminal_result("") == {}


def test_producer_argv_asks_for_stream_json_and_verbose(tmp_path, monkeypatch):
    """`--verbose` is required alongside `-p --output-format stream-json` on
    the installed CLI — confirmed directly against it: a bare invocation with
    no `--verbose` refuses with "Error: When using --print,
    --output-format=stream-json requires --verbose". The old single-blob
    `--output-format json` must be gone, not merely joined by the new flags."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "ok"})

    runner.dispatch(root, board.find(root, "probe"), "development_team", "sonnet", 5.0, 120)

    argv = seen["argv"]
    assert argv.count("--output-format") == 1
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"
    assert "--verbose" in argv


def test_the_producer_and_checker_argvs_both_use_stream_json_and_verbose(tmp_path, monkeypatch):
    """The checker is the second spawn site (`run_checker`), built by the
    runner rather than nested inside the producer (§16) — it must carry the
    same two flags independently, not inherit them from the producer's call."""
    root = _worktree_repo(tmp_path)
    _art_card(root)
    argvs: list = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        argvs.append(argv)
        agent = argv[argv.index("--agent") + 1]
        if agent == "art":
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "cand.png").write_bytes(b"\x89PNG")
        else:
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"verdict": "pass", "best": "cand.png"}),
                              encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "icon"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert len(argvs) == 2   # producer, then checker
    for one in argvs:
        assert one.count("--output-format") == 1
        idx = one.index("--output-format")
        assert one[idx + 1] == "stream-json"
        assert "--verbose" in one


def test_stale_check_argv_uses_stream_json_and_verbose(tmp_path, monkeypatch):
    """The third spawn site, `run_stale_check` — the one not reachable through
    `dispatch` at all, so it needs its own direct call."""
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        seen["argv"] = argv
        seen["stream_path"] = stream_path
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.02}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    runner.run_stale_check(tmp_path, "docs/x.md", out_dir, "sonnet", 0.0, 60)

    argv = seen["argv"]
    assert argv.count("--output-format") == 1
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"
    assert "--verbose" in argv
    assert seen["stream_path"] == out_dir / "stream.jsonl"


def test_read_telemetry_survives_a_stream_json_worker(tmp_path, monkeypatch):
    """End to end: a producer whose stdout is a realistic 3-line stream-json
    stream (not the old single-blob shape) still leaves `worker-1.json` as one
    JSON object — the terminal `result` event, not the whole JSONL blob — so
    `read_telemetry` (which globs `worker-*.json` and `json.loads`s each one)
    and the card's `## Telemetry` block both come out exactly as they did
    under `--output-format json`."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "probe")

    jsonl_stdout = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({
            "type": "result", "duration_ms": 12345, "duration_api_ms": 6000,
            "num_turns": 4, "total_cost_usd": 0.42,
            "usage": {"output_tokens": 100, "cache_read_input_tokens": 10,
                      "cache_creation_input_tokens": 5, "input_tokens": 20},
            "modelUsage": {"claude-sonnet-5": {}}, "permission_denials": [],
            "terminal_reason": "completed", "session_id": "s1",
        }),
    ])

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        (Path(cwd) / "worked.txt").write_text("done\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "worker: did the thing"], cwd=cwd, check=True)
        verdict_line = next(l.strip() for l in prompt.splitlines()
                            if l.strip().endswith(".json"))
        Path(verdict_line).parent.mkdir(parents=True, exist_ok=True)
        Path(verdict_line).write_text(
            json.dumps({"outcome": "done", "summary": "implemented"}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, jsonl_stdout, "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")

    result = runner.dispatch(root, board.find(root, "probe"), "development_team",
                             "sonnet", 5.0, 120)
    assert result.outcome == "review"
    assert result.cost_usd == pytest.approx(0.42)

    out_dir = runner.run_dir(root, board.find(root, "probe"), 1)
    worker_json = json.loads((out_dir / "worker-1.json").read_text(encoding="utf-8"))
    assert worker_json["type"] == "result"
    assert worker_json["total_cost_usd"] == 0.42     # the result event, not the assistant line

    tel = runner.read_telemetry(out_dir)
    assert tel["cost_usd"] == pytest.approx(0.42)
    assert tel["turns"] == 4
    assert tel["ended"] == "completed"
    assert "claude-sonnet-5" in tel["models"]

    runner.settle(root, "probe", result)
    card_text = (root / "Board" / "review" / "probe.md").read_text(encoding="utf-8")
    assert "## Telemetry" in card_text and "0.42" in card_text


def test_stream_jsonl_grows_while_the_worker_is_still_running(tmp_path):
    """The acceptance criterion, verbatim: drive `_run_worker` against a stub
    process that emits one event, pauses, then emits its terminal result — and
    read `stream.jsonl` from this thread mid-pause to confirm only the first
    event has landed while the process is still alive."""
    stub = (
        "import json, time\n"
        "print(json.dumps({'type': 'event', 'n': 1}), flush=True)\n"
        "time.sleep(1.2)\n"
        "print(json.dumps({'type': 'result', 'total_cost_usd': 0.01}), flush=True)\n"
    )
    stream_path = tmp_path / "stream.jsonl"
    outcome: dict = {}

    def _run():
        outcome["proc"] = runner._run_worker(
            [sys.executable, "-c", stub], tmp_path, 10, stream_path)

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        deadline = time.time() + 5
        seen_first = False
        while time.time() < deadline:
            if stream_path.is_file() and '"n": 1' in stream_path.read_text(encoding="utf-8"):
                seen_first = True
                break
            time.sleep(0.05)
        assert seen_first, "the first event never landed on disk while the worker ran"
        # Still mid-sleep: the terminal event must not have landed yet.
        assert "total_cost_usd" not in stream_path.read_text(encoding="utf-8")
    finally:
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert "total_cost_usd" in stream_path.read_text(encoding="utf-8")
    assert outcome["proc"].returncode == 0


def test_a_worker_that_exceeds_its_timeout_is_still_killed(tmp_path):
    """The timeout path must survive the `Popen` rewrite: `subprocess.run
    (timeout=)` killed the child for free; a manual `Popen.wait(timeout=)` does
    not, so `_run_worker` has to kill it by hand before re-raising."""
    pidfile = tmp_path / "pid.txt"
    stub = (
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        runner._run_worker([sys.executable, "-c", stub, str(pidfile)], tmp_path, 2)

    deadline = time.time() + 5
    pid = None
    while time.time() < deadline:
        if pidfile.is_file() and pidfile.read_text(encoding="utf-8").strip():
            pid = int(pidfile.read_text(encoding="utf-8").strip())
            break
        time.sleep(0.05)
    assert pid is not None, "the stub never reported its own pid"
    assert not runner._pid_alive(pid), "the timed-out worker was not actually killed"


def test_run_worker_closes_the_stream_handle_when_popen_itself_fails(tmp_path):
    """A `Popen` failure (the binary vanishing between preflight and dispatch,
    say) must not leak the tee's file handle. Windows makes this observable
    directly: an open handle blocks deletion, so if the guard did not close
    it, this unlink would raise `PermissionError`."""
    stream_path = tmp_path / "stream.jsonl"
    missing = tmp_path / "does-not-exist-binary"
    with pytest.raises(OSError):
        runner._run_worker([str(missing)], tmp_path, 5, stream_path)
    stream_path.unlink()


def test_run_worker_returns_promptly_when_a_grandchild_holds_stdout_open(tmp_path):
    """The gap the 2026-07-24 review found: `t_out.join()`/`t_err.join()` on
    the success path used to be unbounded, unlike the timeout path's
    `join(timeout=5)`. A child that exits promptly while a grandchild it spawned
    (a Bash tool call, an MCP server) still holds the pipe's write end open
    leaves `readline()` never seeing EOF — the drain thread never finishes, and
    an unbounded `join()` would then block forever with no timeout backstop at
    all. This stub reproduces exactly that: it prints one event, hands a
    duplicate of its own stdout handle to a grandchild that sleeps for 30s, and
    exits immediately without waiting for it — `_run_worker` must still return
    in a bounded time, not after 30s and not never."""
    stub = (
        "import json, os, subprocess, sys\n"
        "print(json.dumps({'type': 'event', 'n': 1}), flush=True)\n"
        "fd = os.dup(1)\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=fd)\n"
        "os.close(fd)\n"
    )
    start = time.time()
    proc = runner._run_worker([sys.executable, "-c", stub], tmp_path, 20)
    elapsed = time.time() - start
    assert elapsed < 15, f"_run_worker took {elapsed:.1f}s — the drain-thread join is not bounded"
    assert proc.returncode == 0


# --- ensure_workspace_trusted -------------------------------------------------
# Headless `claude -p` has no trust dialog, so an untrusted workspace does not
# refuse — it silently drops the worker's permissions.allow / additionalDirectories
# entries (2026-07-25). The invisible-failure shape this whole file exists to
# guard, so the flag-writing is tested directly.

def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path / ".claude.json"


def test_ensure_trusted_writes_the_flag_for_an_untrusted_workspace(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    key = str(tmp_path.resolve()).replace("\\", "/")
    config.write_text(json.dumps({"projects": {key: {"hasTrustDialogAccepted": False}}}),
                      encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["projects"][key]["hasTrustDialogAccepted"] is True


def test_ensure_trusted_creates_the_project_entry_when_absent(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    config.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    key = str(tmp_path.resolve()).replace("\\", "/")
    assert data["projects"][key]["hasTrustDialogAccepted"] is True


def test_ensure_trusted_matches_an_existing_key_case_insensitively(tmp_path, monkeypatch):
    # Windows stores the drive letter either case (both `c:/` and `C:/` were live
    # in Karel's real config); a naive add would leave the wrong-cased entry still
    # untrusted and create a trusted duplicate that Claude never reads.
    config = _home(monkeypatch, tmp_path)
    weird = str(tmp_path.resolve()).replace("\\", "/").swapcase()
    config.write_text(json.dumps({"projects": {weird: {"hasTrustDialogAccepted": False}}}),
                      encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["projects"][weird]["hasTrustDialogAccepted"] is True
    assert len(data["projects"]) == 1, "created a duplicate instead of updating in place"


def test_ensure_trusted_is_a_noop_when_already_trusted(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    key = str(tmp_path.resolve()).replace("\\", "/")
    config.write_text(json.dumps({"projects": {key: {"hasTrustDialogAccepted": True}}}),
                      encoding="utf-8")
    before = config.stat().st_mtime_ns
    runner.ensure_workspace_trusted(tmp_path)
    assert config.stat().st_mtime_ns == before, "rewrote the file with nothing to change"


def test_ensure_trusted_swallows_a_missing_config(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)  # no .claude.json written
    runner.ensure_workspace_trusted(tmp_path)  # must not raise


def test_ensure_trusted_swallows_unreadable_json(tmp_path, monkeypatch):
    config = _home(monkeypatch, tmp_path)
    config.write_text("{ not json", encoding="utf-8")
    runner.ensure_workspace_trusted(tmp_path)  # must not raise
    assert config.read_text(encoding="utf-8") == "{ not json", "clobbered a file it could not parse"


# --- the worktree fence: a worker writes only in its worktree -----------------
#
# Prevention half (`.ai/hooks/worktree_fence.py::evaluate`) plus the runner's
# guarantee half (`assert_integration_unmoved`). Together they answer the
# 2026-07-25 wrong-checkout defect: a worker that commits game code onto the
# shared integration branch from the canonical checkout.

def _roots(*paths: Path) -> list[Path]:
    return [p.resolve() for p in paths]


def test_fence_is_off_when_no_roots_are_declared(tmp_path):
    """Karel's own sessions never set the env var, so the fence must be a pure
    no-op then — a guard that fired on every interactive Write would be torn out
    within a day."""
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "x")}}
    assert worktree_fence.evaluate(payload, []) is None


def test_fence_denies_a_write_outside_the_worktree(tmp_path):
    wt = tmp_path / "wt"
    outside = tmp_path / "canonical" / "combat" / "action.py"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(outside)}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is not None


def test_fence_allows_a_write_inside_the_worktree(tmp_path):
    wt = tmp_path / "wt"
    inside = wt / "dungeoneer" / "combat" / "action.py"
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(inside)}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is None


def test_fence_allows_writes_to_any_declared_root(tmp_path):
    """The verdict JSON lands in the run dir (outside the worktree) and the card's
    Thread lives under Board/ — both are declared roots, both must pass."""
    wt, run_dir, board_dir = tmp_path / "wt", tmp_path / "runs", tmp_path / "Board"
    roots = _roots(wt, run_dir, board_dir)
    for target in (run_dir / "verdict-1.json", board_dir / "tasks" / "c.md"):
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
        assert worktree_fence.evaluate(payload, roots) is None, target


def test_fence_denies_a_bash_cd_to_an_absolute_path_outside(tmp_path):
    """The exact failure: `cd <canonical checkout> && git commit`."""
    wt = tmp_path / "wt"
    canonical = tmp_path / "canonical"
    cmd = f"cd {canonical.as_posix()} && git commit -am x"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is not None


def test_fence_allows_a_relative_cd_which_stays_in_the_worktree(tmp_path):
    """A relative `cd` resolves under the launch cwd (the worktree), so it is not
    the escape and must not be blocked — else the worker cannot `cd dungeoneer`."""
    wt = tmp_path / "wt"
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "cd dungeoneer && python -m pytest -q"}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is None


def test_fence_denies_git_dash_C_pointing_outside(tmp_path):
    wt = tmp_path / "wt"
    canonical = tmp_path / "canonical"
    cmd = f"git -C {canonical.as_posix()} commit -am x"
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert worktree_fence.evaluate(payload, _roots(wt)) is not None


def test_worker_env_declares_exactly_the_three_writable_roots(tmp_path):
    """The env the runner hands a worker must allow its worktree, its run dir and
    Board/, and nothing else — those are the only places a worker legitimately
    writes."""
    _write_manifest(tmp_path)
    tree, out_dir = tmp_path / "wt", tmp_path / "runs" / "c" / "attempt-1"
    env = runner._worker_env(tmp_path, tree, out_dir)
    declared = env[runner.fence_env(tmp_path)].split(os.pathsep)
    expected = {str(p.resolve()) for p in (tree, out_dir, tmp_path / "Board")}
    assert set(declared) == expected
    assert "PATH" in env or "Path" in env, "must inherit the parent environment, not replace it"


def test_a_project_declaring_no_fence_env_leaves_the_fence_off(tmp_path):
    """`[worker].fence_env` is optional, and its absence must be a plain
    inherited environment rather than a `KeyError` or a variable named `""`.
    The fence then never arms, which is the safe direction the hook documents."""
    assert runner.fence_env(tmp_path) == ""
    env = runner._worker_env(tmp_path, tmp_path / "wt", tmp_path / "out")
    assert "" not in env
    assert "PATH" in env or "Path" in env


def test_backstop_resets_a_stray_commit_off_the_integration_branch(tmp_path):
    """A worker commit that lands on `base` from the wrong checkout is undone; the
    branch returns to the tip the runner left, no matter the card's fate."""
    root = _repo(tmp_path)
    base = runner.current_branch(root)
    tip = runner._git(root, "rev-parse", base).stdout.strip()

    # Simulate the defect: a commit lands on `base` in the main checkout.
    (root / "stray.py").write_text("print('wrong checkout')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "stray: committed to base by mistake"],
                   cwd=root, check=True)
    assert runner._git(root, "rev-parse", base).stdout.strip() != tip

    assert runner.assert_integration_unmoved(root, base, tip) is True
    assert runner._git(root, "rev-parse", base).stdout.strip() == tip
    assert not (root / "stray.py").exists(), "the stray commit's file must be gone too"


def test_backstop_is_a_no_op_when_the_branch_did_not_move(tmp_path):
    root = _repo(tmp_path)
    base = runner.current_branch(root)
    tip = runner._git(root, "rev-parse", base).stdout.strip()
    assert runner.assert_integration_unmoved(root, base, tip) is False
    assert runner._git(root, "rev-parse", base).stdout.strip() == tip


# --- the JUnit-report verdict (runner-test-selection) -------------------------
#
# Karel's design: the python suite writes the record, the runner checks it. The
# XML is authoritative, and a zero-collected report is a failure, not a pass —
# the silent-green a mis-selection would otherwise produce.

def _junit(path: Path, *, tests: int, failures: int = 0, errors: int = 0) -> Path:
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="{errors}" skipped="0"></testsuite></testsuites>',
        encoding="utf-8")
    return path


def test_check_junit_passes_an_all_green_report(tmp_path):
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=42))
    assert ok and why == ""


def test_check_junit_fails_on_a_failure(tmp_path):
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=42, failures=1))
    assert not ok and "1 failure" in why


def test_check_junit_fails_on_an_error(tmp_path):
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=42, errors=2))
    assert not ok and "2 error" in why


def test_check_junit_treats_zero_collected_as_a_failure(tmp_path):
    """The silent-green guard: a selection that matched nothing is not a pass."""
    ok, why = runner._check_junit(_junit(tmp_path / "j.xml", tests=0))
    assert not ok and "0 tests" in why


def test_check_junit_fails_when_no_report_was_written(tmp_path):
    ok, why = runner._check_junit(tmp_path / "absent.xml")
    assert not ok and "no JUnit report" in why


def test_check_junit_fails_on_an_unreadable_report(tmp_path):
    bad = tmp_path / "j.xml"
    bad.write_text("<testsuites>not closed", encoding="utf-8")
    ok, why = runner._check_junit(bad)
    assert not ok and "unreadable" in why


def test_run_tests_passes_the_parallel_and_junit_flags(tmp_path, monkeypatch):
    """Production must parallelise by file and report to JUnit. Restores the real
    flags (the autouse fixture blanks them for speed) and captures the argv."""
    monkeypatch.setattr(runner, "_PYTEST_PARALLEL", ("-n", "auto", "--dist", "loadfile"))
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        _junit(tmp_path / "j.xml", tests=1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    ok, why, evidence = runner._run_tests(tmp_path, tmp_path / "log.txt", 60,
                                          tmp_path / "j.xml", ["tests/"])
    assert ok, why
    assert evidence == "", "a passing run has nothing to quote"
    argv = captured["argv"]
    assert "-n" in argv and "auto" in argv and "loadfile" in argv
    assert any(str(a).startswith("--junitxml=") for a in argv)
    assert "tests/" in argv


def test_check_junit_sums_across_multiple_testsuites(tmp_path):
    """pytest-xdist can emit several <testsuite> elements; the counts add up."""
    path = tmp_path / "j.xml"
    path.write_text(
        '<testsuites>'
        '<testsuite tests="10" failures="0" errors="0"></testsuite>'
        '<testsuite tests="5" failures="1" errors="0"></testsuite>'
        '</testsuites>', encoding="utf-8")
    ok, why = runner._check_junit(path)
    assert not ok and "1 failure" in why and "15 test" in why


# --- runner-hardening #3: the dedicated integration checkout ----------------
#
# The topology: the runner keeps `development_team` in its own sibling worktree so
# Karel's launch checkout can sit on his own branch and he can keep coding during a
# run. The board and every git operation happen in that dedicated checkout; the
# control plane (STOP, status, lock, log) stays with the launch checkout, where he
# reaches it. Reviewed-ok cards are rebased onto the current integration tip and
# re-verified before they merge, so a same-night sibling edit surfaces as a
# conflict a human resolves rather than a silent clobber.

def _split_repo(tmp_path: Path, *card_ids: str,
                work_branch: str = "karel/work") -> tuple[Path, Path]:
    """A repo whose board lives (committed) on `development_team` while the launch
    checkout has moved to `work_branch` — the #3 topology. Nested one level under
    `tmp_path` so the sibling `.dungeoneer-integration` / `.dungeoneer-worktrees`
    dirs are unique per test. Returns (launch_root, dedicated_checkout_path); the
    dedicated checkout is NOT created here — `run()`/`ensure_integration_checkout`
    does that."""
    main = tmp_path / "main"
    main.mkdir()
    root = _worktree_repo(main)          # on development_team, with gate stub + tests
    _charter(root, "code-thread")
    _tier_binding(root)
    for cid in card_ids:
        _card(root, "tasks", cid)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "board + config on development_team"],
                   cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", work_branch], cwd=root, check=True)
    return root, runner.integration_checkout_path(root)


def test_ensure_integration_checkout_creates_a_sibling_on_the_base(tmp_path):
    """A dedicated worktree, on the integration branch, beside the launch checkout
    (never inside it — doc_scan would walk a checkout under Board/). Idempotent: a
    second night reuses the same one."""
    root, path = _split_repo(tmp_path)
    made = runner.ensure_integration_checkout(root, "development_team")
    assert made == path
    assert path.is_dir() and path != root
    assert path.parent == root.parent           # a sibling, outside the repo
    assert runner.current_branch(path) == "development_team"
    assert runner.ensure_integration_checkout(root, "development_team") == path  # reused


def test_ensure_integration_checkout_recovers_a_leftover_directory(tmp_path):
    """A crash can prune the worktree registration while leaving the directory
    behind (or one from a prior clone lingers). The branch carries every board
    commit, so the stray directory is cleared and recut rather than trusted."""
    root, path = _split_repo(tmp_path)
    path.mkdir(parents=True)
    (path / "junk.txt").write_text("stale\n", encoding="utf-8")
    made = runner.ensure_integration_checkout(root, "development_team")
    assert made == path and runner.current_branch(path) == "development_team"
    assert not (path / "junk.txt").exists()


def test_the_kill_switch_trips_in_either_root(tmp_path, monkeypatch):
    """The switch may land in the vault (the control root, where Karel drops it
    from his phone) or in the dedicated checkout. It must be honoured either way —
    the two are separate working trees now, and a switch that watched only one
    would be a promise the runner could quietly break."""
    ctrl, work = tmp_path / "ctrl", tmp_path / "work"
    (ctrl / ".ai").mkdir(parents=True)
    (work / ".ai").mkdir(parents=True)
    monkeypatch.setattr(runner, "_CTRL_ROOT", ctrl)
    monkeypatch.setattr(runner, "_WORK_ROOT", work)
    assert not runner._stop_requested()
    (work / runner.STOP_FILE).write_text("", encoding="utf-8")
    assert runner._stop_requested()             # in the dedicated checkout
    (work / runner.STOP_FILE).unlink()
    (ctrl / runner.STOP_FILE).write_text("", encoding="utf-8")
    assert runner._stop_requested()             # in the vault


def test_the_status_heartbeat_lands_in_the_control_root(tmp_path, monkeypatch):
    """`--status` is read from the launch checkout, so the heartbeat must be
    written there even though every dispatch-time caller passes the work root."""
    ctrl, work = tmp_path / "ctrl", tmp_path / "work"
    (ctrl / ".ai" / "runs").mkdir(parents=True)
    (work / ".ai" / "runs").mkdir(parents=True)
    monkeypatch.setattr(runner, "_CTRL_ROOT", ctrl)
    runner._status(work, phase="worker", card="x")
    assert (ctrl / runner.STATUS_FILE).is_file()
    assert not (work / runner.STATUS_FILE).is_file()


def test_a_run_uses_a_dedicated_checkout_and_leaves_the_launch_copy_alone(tmp_path, monkeypatch):
    """The card this part exists for: with the launch checkout on Karel's own
    branch — dirty with his work-in-progress — the runner dispatches, reviews,
    rebases and lands the card entirely in the dedicated `development_team`
    checkout, and never moves his branch or disturbs his edits."""
    root, path = _split_repo(tmp_path, "feat")
    (root / "scratch.py").write_text("wip = 1\n", encoding="utf-8")   # Karel mid-edit
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "done"})
    monkeypatch.setattr(runner, "run_reviewer",
                        lambda *a, **k: ({"verdict": "ok", "notes": "clean"}, 0.1, None))

    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))

    # Landed in testing/ on the dedicated checkout's board (rebased + merged for real).
    assert board.find(path, "feat").lane == "testing"
    assert runner.current_branch(path) == "development_team"
    # The launch checkout never moved off Karel's branch and his edit survived.
    assert runner.current_branch(root) == "karel/work"
    assert (root / "scratch.py").read_text(encoding="utf-8") == "wip = 1\n"


def test_on_base_the_runner_works_in_place_and_cuts_no_dedicated_checkout(tmp_path, monkeypatch):
    """The migration fallback: a launch checkout still on the integration branch
    can't also hold it in a second worktree (git forbids it), so the runner works
    in-place exactly as before and cuts no sibling checkout."""
    root = _loaded_board(tmp_path, "feat")            # HEAD on development_team
    _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})
    monkeypatch.setattr(runner, "run_reviewer",
                        lambda *a, **k: ({"verdict": "needs_decision", "question": "q?"},
                                         0.0, None))

    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))

    assert not runner.integration_checkout_path(root).exists()
    assert board.find(root, "feat").lane == "needs-decision"


def test_a_dirty_launch_checkout_off_base_does_not_block_preflight(tmp_path, monkeypatch):
    """The whole point of #3: once Karel is on his own branch, his uncommitted work
    is none of the runner's business. Preflight only objects to dirt when it will
    operate in the checkout itself (the on-base fallback)."""
    root, _ = _split_repo(tmp_path)
    (root / "scratch.py").write_text("wip = 1\n", encoding="utf-8")
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")  # not on PATH under test
    check = runner.preflight(root, "development_team", dry_run=False)
    assert check.ok, check.reasons


# --- rebase-then-merge (decision #2) ----------------------------------------

def _branch_with_file(root: Path, tmp_path: Path, branch: str, name: str, body: str) -> None:
    """Create `branch` off development_team carrying one file, via a throwaway worktree."""
    subprocess.run(["git", "branch", branch, "development_team"], cwd=root, check=True)
    wt = tmp_path / f"seed-{branch.replace('/', '-')}"
    subprocess.run(["git", "worktree", "add", str(wt), branch], cwd=root, check=True)
    (wt / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", f"{branch}: {name}"], cwd=wt, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, check=True)


def _commit_on_base(root: Path, tmp_path: Path, name: str, body: str) -> None:
    """Advance development_team with one file, in the checkout it is on."""
    (root / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", f"development_team: {name}"], cwd=root, check=True)


def test_rebase_and_merge_lands_a_clean_branch(tmp_path):
    """The happy path: nothing landed since the branch forked, so the rebase is a
    no-op, re-verification passes and the branch merges — development_team now
    carries its file."""
    root = _worktree_repo(tmp_path)               # on development_team
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert merged, why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"


def test_rebase_and_merge_deletes_the_branch_once_it_lands(tmp_path):
    """Once `branch`'s rebased commits are on development_team, nothing reads
    `ai/<id>` again — keeping it forever is exactly the stale-ref litter that
    made `git branch --merged` unable to recognise already-merged cards."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")

    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert merged, why
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode != 0


def test_rebase_and_merge_replays_over_a_non_conflicting_sibling(tmp_path):
    """A sibling card touched a *different* file on development_team since this one
    forked. The rebase replays cleanly, re-verification passes, and both files end
    up on the integration branch — the case the old plain merge also handled, kept
    working."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    _commit_on_base(root, tmp_path, "sibling.py", "y = 2\n")

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert merged, why
    assert (root / "feature.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (root / "sibling.py").read_text(encoding="utf-8") == "y = 2\n"


def test_rebase_and_merge_leaves_a_conflicting_branch_for_a_human(tmp_path):
    """The problem #2 fixes: a sibling edited the *same* file on development_team
    since this card was reviewed. The rebase conflicts, so the branch does NOT
    merge, development_team is left exactly where it was, and the reason names the
    file — never a guessed resolution (decision #3)."""
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "shared.py", "value = 'A'\n")
    _commit_on_base(root, tmp_path, "shared.py", "value = 'B'\n")
    base_before = runner._git(root, "rev-parse", "development_team").stdout.strip()

    card = board.Card(root / "x.md", "tasks", {"id": "probe"}, "")
    merged, why = runner.rebase_and_merge(root, card, "ai/probe", "development_team")
    assert not merged
    assert "conflict" in why and "shared.py" in why
    # development_team is untouched — the sibling's version still stands.
    assert runner._git(root, "rev-parse", "development_team").stdout.strip() == base_before
    assert (root / "shared.py").read_text(encoding="utf-8") == "value = 'B'\n"
    # The branch is kept — a human still needs it to resolve the conflict.
    assert runner._git(root, "rev-parse", "--verify", "ai/probe").returncode == 0


# --- publish: pushing so a cloud run is pullable anywhere -------------------
#
# The runner commits everything locally — the board, merged cards on
# `development_team`, and each card's `ai/<id>` branch — which was always
# enough on Karel's laptop, where the checkout *is* his repo. In the cloud
# topology (`night.py`) the checkout is an ephemeral clone: a card parked in
# `needs-decision/` leaves its branch stranded in a container he cannot reach.
# `publish()` closes that gap; these tests are against a bare `origin` so a
# push is a real, checkable git operation, not a mock.

def _bare_origin(root: Path, tmp_path: Path, name: str = "origin.git") -> Path:
    """A bare repo wired as `origin`, so `publish()`'s pushes are real and their
    result can be inspected independently of the working checkout."""
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True)
    return bare


def _remote_tip(bare: Path, ref: str) -> str:
    return runner._git(bare, "rev-parse", ref).stdout.strip()


def test_publish_is_a_no_op_with_no_remote_configured(tmp_path):
    """The local default: a laptop's `hosts.json` entry carries no
    `publish_remote`, so `runner.host_setting` returns `""` and `publish` must
    not attempt any network call — asserted by there being no `origin` at all
    and the call still succeeding."""
    root = _worktree_repo(tmp_path)
    runner.publish(root, "", "development_team")  # must not raise


def test_publish_pushes_base_and_every_ai_branch(tmp_path):
    """The scope Karel asked for: the integration branch (board, digest, merged
    cards) and every live `ai/<id>` card branch, so `git fetch` from anywhere
    else gets the whole night."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/parked-card", "feature.py", "x = 1\n")
    _commit_on_base(root, tmp_path, "extra.py", "y = 2\n")

    runner.publish(root, "origin", "development_team")

    assert _remote_tip(bare, "refs/heads/development_team") == \
        runner._git(root, "rev-parse", "development_team").stdout.strip()
    assert _remote_tip(bare, "refs/heads/ai/parked-card") == \
        runner._git(root, "rev-parse", "ai/parked-card").stdout.strip()


def test_publish_is_idempotent(tmp_path):
    """Called after every settled card, so a no-op second call (nothing moved
    since the last publish) must not error or change anything."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/parked-card", "feature.py", "x = 1\n")

    runner.publish(root, "origin", "development_team")
    tip_before = _remote_tip(bare, "refs/heads/ai/parked-card")
    runner.publish(root, "origin", "development_team")  # nothing changed locally
    assert _remote_tip(bare, "refs/heads/ai/parked-card") == tip_before


def test_publish_never_forces_the_integration_branch(tmp_path, monkeypatch):
    """A rejected push to `base` means someone else moved `origin/development_team`
    since this checkout last saw it — deciding whose commits win is a human call
    (§12), never guessed here. The remote must be left exactly where the
    divergent push left it, and the failure logged loudly."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    runner.publish(root, "origin", "development_team")  # base now in sync

    # Diverge the remote: a clone pushes a commit `root` has never seen.
    clone = tmp_path / "sibling-clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    subprocess.run(["git", "checkout", "-q", "development_team"], cwd=clone, check=True)
    (clone / "sibling.py").write_text("z = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", "sibling advances development_team"],
                   cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "development_team"], cwd=clone, check=True)
    diverged_tip = _remote_tip(bare, "refs/heads/development_team")
    assert diverged_tip != runner._git(root, "rev-parse", "development_team").stdout.strip()

    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    runner.publish(root, "origin", "development_team")

    assert _remote_tip(bare, "refs/heads/development_team") == diverged_tip, \
        "a rejected push to base must never be forced"
    assert any("not pushed" in line or "not forced" in line for line in logged)


def test_publish_force_with_lease_on_the_trusted_branch(tmp_path):
    """The card just dispatched this call (`trusted_branch`) IS force-pushed even
    when the rewrite makes it a git-history sibling rather than a descendant of
    what was previously published — exactly what a cold-start retry produces
    (`prepare_worktree`'s FRESH path deletes the old branch and recreates it
    fresh from `base`), and exactly what `git commit --amend` models here."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "feature.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")

    # Rewrite ai/probe locally (as a cold-start retry or a rebase replay would) —
    # `--amend` reparents onto the ORIGINAL parent, making this a sibling of the
    # previously-published tip, not a descendant of it.
    subprocess.run(["git", "checkout", "-q", "ai/probe"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--amend", "-m", "ai/probe: rewritten"],
                   cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "development_team"], cwd=root, check=True)
    rewritten_tip = runner._git(root, "rev-parse", "ai/probe").stdout.strip()

    runner.publish(root, "origin", "development_team", trusted_branch="ai/probe")

    assert _remote_tip(bare, "refs/heads/ai/probe") == rewritten_tip


def test_publish_refuses_a_diverged_untrusted_branch(tmp_path, monkeypatch):
    """The bug this fix closes (`menu-unlock-indicators`, 2026-07-28): a local
    `ai/<id>` branch nobody asked this call to trust, which has diverged from
    what `origin` now carries (a different, later attempt dispatched and
    published from somewhere else). Without the fetch+ancestry gate,
    `--force-with-lease` alone would wave this straight through — verified
    empirically against a real bare remote before this fix existed — silently
    destroying the remote's real, current work. `publish` must refuse and log,
    leaving the remote exactly as it was."""
    root = _worktree_repo(tmp_path)
    bare = _bare_origin(root, tmp_path)
    _branch_with_file(root, tmp_path, "ai/probe", "real-work.py", "x = 1\n")
    runner.publish(root, "origin", "development_team")  # origin now has the real attempt
    real_tip = _remote_tip(bare, "refs/heads/ai/probe")

    # This checkout never talked to origin about ai/probe before independently
    # creating its own, unrelated (dead/superseded) branch of the same name.
    subprocess.run(["git", "branch", "-D", "ai/probe"], cwd=root, check=True)
    _branch_with_file(root, tmp_path, "ai/probe", "dead-attempt.py", "y = 2\n")

    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))
    runner.publish(root, "origin", "development_team")  # no trusted_branch

    assert _remote_tip(bare, "refs/heads/ai/probe") == real_tip, \
        "a diverged, untrusted branch must never overwrite the remote's real work"
    assert any("diverged" in line for line in logged)


def test_publish_degrades_when_the_remote_is_not_configured(tmp_path, monkeypatch):
    """A `publish_remote` naming a remote this checkout never got (a misconfigured
    routine) must log and return, never raise — a push problem is an
    observability gap, not a reason to end the night."""
    root = _worktree_repo(tmp_path)
    logged: list[str] = []
    monkeypatch.setattr(runner, "_log", lambda message: logged.append(message))

    runner.publish(root, "origin", "development_team")  # no `origin` remote exists

    assert any("not configured" in line for line in logged)


def test_card_branches_lists_only_the_ai_namespace(tmp_path):
    root = _worktree_repo(tmp_path)
    _branch_with_file(root, tmp_path, "ai/one", "a.py", "1\n")
    _branch_with_file(root, tmp_path, "ai/two", "b.py", "2\n")
    subprocess.run(["git", "branch", "not-a-card-branch"], cwd=root, check=True)

    assert set(runner._card_branches(root)) == {"ai/one", "ai/two"}


def test_publish_remote_is_read_from_host_config(tmp_path):
    """The one setting that gates publishing: absent on the ordinary laptop
    (`host_setting` returns the default, `""`), present in the cloud override
    `night.py` writes (`"origin"`)."""
    assert runner.host_setting(tmp_path, "publish_remote", "") == ""
    (tmp_path / ".ai").mkdir(exist_ok=True)
    (tmp_path / runner.HOST_FILE).write_text(
        json.dumps({"publish_remote": "origin"}), encoding="utf-8")
    assert runner.host_setting(tmp_path, "publish_remote", "") == "origin"


# --- the durable staleness-sweep record (digest's "Staleness" section) -----


def test_stale_status_round_trips(tmp_path):
    assert stale_sweep.read_status(tmp_path) is None
    stale_sweep.write_status(tmp_path, "2026-07-26", checked=5, verified=4, carded=1)
    status = stale_sweep.read_status(tmp_path)
    assert status == {"last_run": "2026-07-26", "checked": 5, "verified": 4, "carded": 1}


def test_stale_status_survives_a_zero_checked_run(tmp_path):
    """'Ran, nothing to check' must read differently from 'never ran' — writing
    unconditionally on any completed sweep attempt is what makes that true."""
    stale_sweep.write_status(tmp_path, "2026-07-26", checked=0, verified=0, carded=0)
    assert stale_sweep.read_status(tmp_path)["last_run"] == "2026-07-26"


def test_stale_status_is_not_gitignored(tmp_path):
    """Unlike the ledger, this file has to survive on every machine and sync
    through git — a gitignored status would make `digest.py` lie about staleness
    the moment someone clones fresh or the ignored file gets cleaned up."""
    assert not str(stale_sweep.STATUS).endswith("stale_ledger.json")
    root = _repo(tmp_path)
    stale_sweep.write_status(root, "2026-07-26", checked=1, verified=1, carded=0)
    status = subprocess.run(["git", "status", "--porcelain", str(stale_sweep.STATUS)],
                            cwd=root, capture_output=True, text=True, check=True)
    assert "??" in status.stdout  # untracked, i.e. NOT ignored (ignored files don't show at all
                                  # under plain `git status`, but `--porcelain` without `-uall`
                                  # still lists them as untracked unless .gitignore excludes them)


# --- commit_board's extra_paths ---------------------------------------------


def test_commit_board_includes_an_existing_extra_path(tmp_path):
    root = _repo(tmp_path)
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "extra.json").write_text("{}", encoding="utf-8")
    board.commit_board(root, "board: probe", extra_paths=(".ai/extra.json",))
    log = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    assert "extra.json" in log.stdout


def test_commit_board_skips_a_missing_extra_path_without_crashing(tmp_path):
    root = _repo(tmp_path)
    (root / "Board" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "Board" / "tasks" / "x.md").write_text("x", encoding="utf-8")
    # Must not raise, and must still commit the real Board change, even though
    # the named extra path was never written.
    board.commit_board(root, "board: probe", extra_paths=(".ai/does_not_exist.json",))
    log = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    assert "x.md" in log.stdout


# --------------------------------------------------------------------------
# The run records itself (`digest-reports-the-run`, 2026-07-30)
# --------------------------------------------------------------------------
#
# The digest reports runs, not lane movement, so what the run writes down is now
# load-bearing: a dispatch the record misses is a dispatch that never happened as
# far as the morning is concerned. These are the end-to-end checks that `run()`
# actually fills the record in — `test_board_run_record.py` covers the module.


def _only_record(root: Path) -> dict:
    files = sorted((root / run_record.DIR).glob("*.json"))
    assert len(files) == 1, f"expected one record, got {[f.name for f in files]}"
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_a_night_records_every_dispatch_with_its_outcome(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b")
    _night(monkeypatch, root, [runner.Dispatch("failed", "gates: boom"),
                               runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    record = _only_record(root)
    assert [(d["card"], d["outcome"]) for d in record["dispatched"]] == \
        [("a", "failed"), ("b", "review")]
    assert record["complete"] is True
    assert record["cards_dispatched"] == 2


def test_a_recorded_failure_survives_the_card_going_back_to_tasks(tmp_path, monkeypatch):
    """THE regression. A failed attempt returns the card to `tasks/` — the lane it
    came from — so no lane diff can see it. The record is the only witness, and
    the digest's Failed section is built on it."""
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("failed", "gates: boom")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert board.find(root, "a").lane == "tasks"          # nothing moved...
    assert run_record.failures(_only_record(root))         # ...and it is still reported


def test_the_stop_reason_is_recorded_not_only_logged(tmp_path, monkeypatch):
    """A night that ended for an unrecorded reason reads in the digest as a night
    that simply ran out of cards."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d", "e")
    _night(monkeypatch, root, [runner.Dispatch("failed", "boom")] * 5)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert "failed in a row" in _only_record(root)["stop_reason"]


def test_max_cards_is_recorded_as_the_stop_reason(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--max-cards", "1"]))

    assert _only_record(root)["stop_reason"] == "reached --max-cards 1"


def test_a_skipped_card_is_recorded_with_the_reason_it_was_skipped(tmp_path, monkeypatch):
    """Nothing moves when a card is skipped, so this is the only place the reason
    can be recovered — and "five art cards need a GPU this host lacks" was
    invisible for a week."""
    root = _loaded_board(tmp_path, "a", "b")
    card = board.find(root, "b")
    card.write({"unattended": "false"})
    board.commit_board(root, "board: b needs a human")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    skipped = {s["card"]: s["reason"] for s in _only_record(root)["skipped"]}
    assert "b" in skipped
    assert "unattended" in skipped["b"]


def test_the_record_carries_the_landing_not_just_the_outcome(tmp_path, monkeypatch):
    """`failed` alone does not say whether the card retries or is in `failed/`."""
    root = _loaded_board(tmp_path, "a")
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        # What the real dispatch does before the worker starts — the attempt is
        # committed first so a crash cannot retry forever. The record reads the
        # attempt number off the same object, so the fake has to move it too.
        card.write({"attempts": str(card.attempts + 1), "started": "now"})
        return runner.Dispatch("failed", "gates: boom")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    entry = _only_record(root)["dispatched"][0]
    assert entry["attempt"] == 1
    assert "attempt 1 failed" in entry["landed"] and "will retry" in entry["landed"]


def test_the_records_totals_match_what_the_run_logged(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok", 1.25),
                               runner.Dispatch("review", "ok", 2.50)])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert _only_record(root)["cost_usd"] == 3.75


def test_a_dry_run_writes_no_record(tmp_path, monkeypatch):
    """`--dry-run` promises no LLM and no writes."""
    root = _loaded_board(tmp_path, "a")
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team", "--dry-run"]))

    assert not (root / run_record.DIR).exists()


def test_the_record_is_closed_before_the_digest_reads_it(tmp_path, monkeypatch):
    """Ordering matters: rendering first would publish a report of a night that
    had not yet admitted how it ended."""
    root = _loaded_board(tmp_path, "a")
    seen: list[bool] = []
    real_render = runner.digest.render

    def spy_render(work):
        seen.append(_only_record(work)["complete"])
        return real_render(work)

    monkeypatch.setattr(runner.digest, "render", spy_render)
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert seen == [True]


# --------------------------------------------------------------------------
# --append-digest (Karel, 2026-07-30): "for when I don't have time to read or
# use a scheduler over weekend" — a run can write its digest without moving the
# read baseline, so a run started later still reports back to the last time the
# baseline genuinely advanced. `test_board_digest.py` pins the windowing
# contract (`_previous_digest` skipping a non-matching commit message); these
# pin that `runner.py` actually produces that commit shape.
# --------------------------------------------------------------------------


def _last_commit_subject(root: Path) -> str:
    out = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=root,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_append_digest_writes_the_non_advancing_commit_message(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--append-digest"]))

    subject = _last_commit_subject(root)
    assert subject == f"{runner.digest.APPEND_COMMIT_PREFIX}: {dt.date.today().isoformat()}"
    assert not subject.startswith("digest: ")   # must not accidentally match _DIGEST_COMMIT


def test_without_the_flag_the_commit_message_is_unchanged(tmp_path, monkeypatch):
    """Regression guard: the default path must still write exactly what it wrote
    before this feature existed — every test and skill that greps `digest: ` in
    git log depends on this string."""
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert _last_commit_subject(root) == f"digest: {dt.date.today().isoformat()}"


def _fake_review_run(monkeypatch, root: Path, card_id: str) -> None:
    """One card, dispatched by name and landed as a real `reviewed` outcome —
    genuinely moved to `testing/` via `runner.settle`, not the `_night` helper's
    no-op settle stub (which never moves a card, and would make an "is this card
    still individually named in the report" assertion meaningless). `dispatch`
    returns `reviewed` directly rather than `review`, so `run()`'s own
    `if result.outcome == "review": result = review_stage(...)` step — which
    would otherwise spawn a real reviewer subprocess — never triggers.
    `rebase_and_merge` is stubbed to succeed, same as
    `test_settle_reviewed_merges_and_lands_in_testing`.
    """
    monkeypatch.setattr(runner, "dispatch",
                        lambda root_, card, base, model, card_budget, test_timeout:
                        runner.Dispatch("reviewed", "ok"))
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600: (True, "merged"))


def test_an_append_run_does_not_close_the_window_for_the_next_run(tmp_path, monkeypatch):
    """End-to-end version of the digest-level windowing test: two real `runner.run()`
    calls, first with `--append-digest`, second without — the second run's own
    rendered `Digest.md` must still carry the first run's dispatch.

    Each run targets a specific card via `--card` rather than relying on
    `select()`'s ordering, and lands it for real (see `_fake_review_run`) so the
    card is genuinely out of `tasks/` and any later appearance in the digest can
    only come from the per-run report, not the queue.
    """
    root = _loaded_board(tmp_path, "a", "b")
    _fake_review_run(monkeypatch, root, "a")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "a", "--append-digest"]))

    # `_previous_digest`/`records_since` compare at second precision, and these
    # two `run()` calls otherwise land in the same wall-clock second — a purely
    # test-speed artefact (real runs are minutes apart), but without the gap the
    # second run's own record can tie its commit's timestamp and get excluded
    # from its own report by the strict `>` comparison.
    time.sleep(1.1)
    _fake_review_run(monkeypatch, root, "b")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "b"]))

    text = (root / "Digest.md").read_text(encoding="utf-8")
    assert "[[a|" in text   # first run's dispatch, still visible in a per-run block
    assert "[[b|" in text   # second run's own dispatch


def test_a_normal_run_after_an_append_run_closes_the_backlog(tmp_path, monkeypatch):
    """A third run, after the non-append run above, must report only itself —
    otherwise the backlog never actually closes and the digest grows forever.

    All three land in `testing/` for real, so a card already reported cannot
    reappear via the standing Queue section (it is no longer in `tasks/`) or the
    standing testing/review section (which only counts, never names). `[[<id>|`
    appearing anywhere is specifically evidence of a per-run block.
    """
    root = _loaded_board(tmp_path, "a", "b", "c")
    _fake_review_run(monkeypatch, root, "a")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "a", "--append-digest"]))

    # See the sibling test above for why the gap is needed: second-precision
    # window comparisons otherwise race against three `run()` calls landing in
    # the same wall-clock second.
    time.sleep(1.1)
    _fake_review_run(monkeypatch, root, "b")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "b"]))   # closes the backlog

    time.sleep(1.1)
    _fake_review_run(monkeypatch, root, "c")
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--card", "c"]))

    text = (root / "Digest.md").read_text(encoding="utf-8")
    assert "[[c|" in text                              # this run's own dispatch
    assert "[[a|" not in text and "[[b|" not in text   # already closed out two runs ago


def test_the_run_label_names_append_mode(tmp_path, monkeypatch):
    """So Karel can tell from the rendered block itself why a run's report is
    stacked on an earlier one instead of standing alone."""
    root = _loaded_board(tmp_path, "a")
    _night(monkeypatch, root, [runner.Dispatch("review", "ok")])
    runner.run(root, runner._parser(root).parse_args(
        ["--base", "development_team", "--append-digest"]))

    assert "append mode" in _only_record(root)["label"]


def test_night_defaults_to_append_digest(tmp_path):
    """The unattended path — nobody at the keyboard to read tonight's report
    before tomorrow night's run overwrites it."""
    assert "--append-digest" in night.DEFAULT_ARGS


# --------------------------------------------------------------------------
# A crash in one card's pipeline is that card's failure
# (`one-crashed-card-kills-the-night`, 2026-08-07)
# --------------------------------------------------------------------------
#
# An exception raised while dispatching one card used to propagate out of
# `dispatch` → `run` → `main` and exit 1, abandoning every remaining card: on
# 2026-08-06 one `FileNotFoundError: [WinError 206]` from an oversized card's
# `Popen` cost the whole night. These drive `run()` with a stage made to *raise*,
# because a test that only asserts the tree is currently fine proves nothing
# about containment.


def _crashing_night(monkeypatch, root: Path, boom: BaseException,
                    on_card: str = "b", *, real_settle: bool = False) -> list[str]:
    """One night in which `dispatch` raises `boom` for `on_card`. Returns the ids
    it was called with, so the assertion is "the loop carried on past the crash"
    rather than "nothing blew up"."""
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        # What the real dispatch commits before the worker starts. The attempt is
        # spent by then, so a crash past that point has spent it whatever the
        # handler decides — and `settle`/the record both read it off this object.
        card.write({"attempts": str(card.attempts + 1), "started": "now"})
        if card.id == on_card:
            raise boom
        return runner.Dispatch("review", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    if not real_settle:
        monkeypatch.setattr(runner, "settle",
                            lambda r, cid, result: f"{cid}: {result.outcome}")
    return calls


def test_a_crashed_dispatch_fails_that_card_and_the_queue_carries_on(tmp_path, monkeypatch):
    """THE regression. The middle card's dispatch raises; the two either side of
    it must still run and the night must end normally."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, FileNotFoundError(
        "[WinError 206] The filename or extension is too long"))

    code = runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert code == 0                       # the exit code an ordinary failure returns


def test_a_crashed_dispatch_is_recorded_as_a_failure_not_as_a_short_night(tmp_path, monkeypatch):
    """The digest reports the record, so a crash the record misses reads as a
    night that simply ran out of cards."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    _crashing_night(monkeypatch, root, FileNotFoundError(
        "[WinError 206] The filename or extension is too long"))
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    record = _only_record(root)
    crashed = [d for d in record["dispatched"] if d["card"] == "b"]
    assert len(crashed) == 1
    assert crashed[0]["outcome"] == "failed"
    # The one-line detail reads as a cause, not as noise.
    assert crashed[0]["detail"] == ("dispatch crashed — FileNotFoundError: "
                                    "[WinError 206] The filename or extension is too long")
    assert record["cards_dispatched"] == 3


def test_a_crash_in_a_later_stage_is_contained_too(tmp_path, monkeypatch):
    """The load-bearing property: the guard covers the whole per-card region, not
    two named calls. `review_stage` runs *after* `dispatch` and only on a `review`
    outcome, so a guard that named `dispatch` alone would let this one out — and a
    stage added after this card is written must be inside by construction."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        return runner.Dispatch("review", "ok")

    def fake_review_stage(root_, card, result, base, card_budget, timeout):
        if card.id == "b":
            raise ValueError("the reviewer's own machinery fell over")
        return runner.Dispatch("reviewed", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "review_stage", fake_review_stage)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")

    code = runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert code == 0
    assert [d["outcome"] for d in _only_record(root)["dispatched"]] == \
        ["reviewed", "failed", "reviewed"]


def test_the_crashed_cards_error_names_the_card_and_carries_the_traceback(tmp_path, monkeypatch):
    """A raw `WinError 206` naming nothing is what turned a five-minute problem
    into a half-hour one. A reader must not have to open the gitignored
    `.ai/runs/` to learn which card crashed and roughly why."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    _crashing_night(monkeypatch, root, FileNotFoundError(
        "[WinError 206] The filename or extension is too long"), real_settle=True)
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    error = board.section(board.find(root, "b").text, "Error")
    assert "dispatching card `b`" in error and "dispatch crashed" in error
    assert "FileNotFoundError" in error                     # the exception type
    assert "Traceback (most recent call last)" in error      # and the traceback
    assert "test_runner.py" in error                         # the frame that raised


def test_a_crash_leaves_no_worktree_and_banks_what_the_worker_managed(tmp_path, monkeypatch):
    """`dispatch`'s own exit paths would have done this; a crash skips them. On
    2026-08-06 that left `.dungeoneer-worktrees/<id>` and its branch for hand
    cleaning before the night could be relaunched."""
    root = _loaded_board(tmp_path, "a")

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        tree, _branch, _mode = runner.prepare_worktree(root_, card, base)
        (tree / "half-done.txt").write_text("what the worker managed\n", encoding="utf-8")
        raise RuntimeError("crashed with the checkout still on disk")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert not (runner.worktree_root(root) / "a").exists()
    # Banked before the checkout went, so the crash costs the process, not the work.
    committed = subprocess.run(["git", "show", "--name-only", "--format=%s", "ai/a"],
                               cwd=root, capture_output=True, text=True).stdout
    assert "wip: a interrupted" in committed
    assert "half-done.txt" in committed


def test_a_cleanup_that_itself_raises_does_not_re_crash_the_loop(tmp_path, monkeypatch):
    """The handler is the last line of defence, so it must not be the next thing
    that ends the night."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, RuntimeError("boom"))
    monkeypatch.setattr(runner, "clear_handover",
                        lambda r, cid: (_ for _ in ()).throw(OSError("cleanup died too")))

    code = runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert code == 0


def test_ctrl_c_still_stops_the_night(tmp_path, monkeypatch):
    """`except Exception`, never a bare `except`. A `KeyboardInterrupt` filed as
    an `## Error` against whatever card happened to be in flight would be worse
    than the crash this guard contains."""
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, KeyboardInterrupt(), on_card="b")

    with pytest.raises(KeyboardInterrupt):
        runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b"]             # it stopped, it did not carry on to c


def test_a_systemexit_is_not_swallowed_either(tmp_path, monkeypatch):
    root = _loaded_board(tmp_path, "a", "b", "c")
    calls = _crashing_night(monkeypatch, root, SystemExit(2), on_card="b")

    with pytest.raises(SystemExit):
        runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b"]


def test_a_systematic_crash_is_stopped_by_the_existing_failure_streak(tmp_path, monkeypatch):
    """The night is not defenceless against a crash that is about the harness
    rather than the card — but the net is `CONSECUTIVE_FAILURE_STOP`, which
    already exists, not a second one added beside it."""
    root = _loaded_board(tmp_path, "a", "b", "c", "d", "e")
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        raise FileNotFoundError("[WinError 206] The filename or extension is too long")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    runner.run(root, runner._parser(root).parse_args(["--base", "development_team"]))

    assert calls == ["a", "b", "c"]
    assert "failed in a row" in _only_record(root)["stop_reason"]


def test_the_crash_guard_wraps_a_region_rather_than_naming_its_stages(tmp_path):
    """The structural half of the load-bearing property, so it survives someone
    adding a stage without reading the tests above: the run loop's guarded call
    is one call to the pipeline helper, and the stages live inside that helper —
    not a `try` listing `dispatch` and `review_stage` by name."""
    source = _RUNNER_SOURCE.read_text(encoding="utf-8")
    loop = source[source.index("def run(root: Path, args: argparse.Namespace)"):]
    guarded = re.search(r"try:\n\s+result = (\w+)\(candidate, model\)\n"
                        r"\s+except Exception as exc:", loop)
    assert guarded, "the run loop's per-card guard is not a single guarded pipeline call"
    helper = guarded.group(1)
    body = loop[loop.index(f"def {helper}(candidate"):loop.index("def _settled(")]
    assert "dispatch(" in body and "review_stage(" in body, \
        f"`{helper}` is not where the per-card stages live, so the guard has stopped " \
        "covering the region it is meant to"
