"""Choosing a card, and everything that happens around a dispatch rather than
inside one: which card is selected and every reason one is skipped, the
chores queue, crash recovery, settling an outcome, the kill switch, the
generated views, host capabilities, artefact harvest, the single-instance
lock and the card writer.

One of three modules split out of `test_runner.py`, whose 315 tests on one
xdist worker were the suite's critical path. The fixtures, and why the split
exists, are in `_runner_helpers.py`.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import socket
import subprocess
from pathlib import Path

import pytest

from nightshift import board
from nightshift import worker_prompt
from nightshift import runner
from nightshift import tiers

from _runner_helpers import (  # noqa: F401  (fixtures register by name)
    _binding_repo,
    _card,
    _charter,
    _fake_worker,
    _gates_pass_by_default,
    _grow,
    _hosts,
    _no_xdist_in_fixtures,
    _repo,
    _select,
    _worktree_repo,
    _write_manifest,
)


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


# --- chores are a different queue, not a different kind of night ------------
#
# `kind: chore` is dispatched in a batch (`nightshift.chores`): a cheap per-item
# pass, then one full suite run over the merged result. The night must therefore
# leave chores alone — dispatching one here would give it the full per-card
# treatment the batch exists to avoid, and three attempts instead of one. Two
# rules, and they have to agree: what selection refuses to queue, retirement must
# take out of `tasks/`, or the card is one nothing ever picks up again.


def test_a_chore_is_not_dispatched_by_the_ordinary_night(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "small", kind="chore")
    candidate = _select(root)["small"]
    assert not candidate.dispatchable
    assert "batch" in candidate.reason


def test_naming_a_chore_by_name_still_runs_it(tmp_path):
    """`--card` is a human asking for this one item, which is the same warrant
    that waives `unattended: false` and the attempt limit."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "small", kind="chore")
    forced = {c.card.id: c for c in runner.select(root, set(), {}, forced="small")}
    assert forced["small"].dispatchable


def test_a_chore_gets_one_attempt_and_a_full_card_gets_three(tmp_path):
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "small", kind="chore")
    _card(root, "tasks", "big")
    assert runner.attempt_limit(board.find(root, "small")) == runner.CHORE_MAX_ATTEMPTS
    assert runner.attempt_limit(board.find(root, "big")) == runner.MAX_ATTEMPTS


def test_a_failed_chore_retires_instead_of_waiting_in_tasks_for_a_retry(tmp_path):
    """The half of the one-attempt rule that is easy to leave out. Selection
    already refuses a spent chore, so a chore left in `tasks/` after its single
    failure is invisible to both queues at once."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "small", kind="chore", attempts="1")
    runner.settle(root, "small", runner.Dispatch("failed", "the tests went red"))
    assert board.find(root, "small").lane == "failed"


def test_a_chores_prompt_tells_the_worker_to_bounce_and_a_full_cards_does_not(
        tmp_path, monkeypatch):
    """The routing guess is made without opening the code; the worker is the only
    actor that does open it, so it is the only place the guess can be checked. The
    condition is the card's `kind:`, which a charter cannot see — hence the prompt."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "small", kind="chore")
    _card(root, "tasks", "big")
    seen = _fake_worker(monkeypatch, verdict={"outcome": "done", "summary": "x"})

    prompts = {}
    for card_id in ("small", "big"):
        runner.dispatch(root, board.find(root, card_id), "development_team",
                        "sonnet", 0.0, 120)
        out = runner.run_dir(root, board.find(root, card_id), 1)
        prompts[card_id] = (out / "prompt-1.md").read_text(encoding="utf-8")
    assert seen["argv"]
    assert "one-prompter" in prompts["small"]
    assert "one-prompter" not in prompts["big"]


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


# --- and the half of that signal the digest can see (round 2) ----------------
#
# The run log gets a line for every card, but `Digest.md` is fed `record.skipped`,
# which is `[c for c in candidates if not c.dispatchable]` — and an oversized card
# is dispatchable *by design*. So the case the whole feature exists for, a card
# dispatched over and over while it grows, was the one case the morning report
# stayed silent about. `oversized_entries` is the separate list that fixes it.

def test_a_card_that_ran_while_oversized_is_reported_to_the_digest(tmp_path):
    """The round-2 regression, at the source. This card is *not* in the skip
    list — that is the point — so without its own list nothing Karel reads in
    the morning would mention it."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    path = _grow(_card(root, "tasks", "fat"), runner.CARD_COMFORT_BYTES + 2000)
    candidates = runner.select(root, set(), {})

    assert [c.card.id for c in candidates if not c.dispatchable] == []
    assert runner.oversized_entries(candidates) == [
        ("fat", len(path.read_text(encoding="utf-8")), runner.CARD_COMFORT_BYTES)]


def test_a_normal_card_is_reported_to_the_digest_not_at_all(tmp_path):
    """Every card is measured, so silence has to be the common case — a nightly
    section listing the whole board is the section that gets skipped."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _card(root, "tasks", "lean")
    assert runner.oversized_entries(runner.select(root, set(), {})) == []


def test_an_oversized_card_that_was_skipped_is_not_also_reported_as_dispatched(tmp_path):
    """It is already in `record.skipped` with the size note inside its reason, so
    it reaches the digest under `### Skipped` where it belongs. Listing it here
    too would report it twice, the second time under a heading that opens with
    "Dispatched anyway" — about a card that never ran."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _grow(_card(root, "tasks", "fat", unattended="false"),
          runner.CARD_COMFORT_BYTES + 2000)
    candidates = runner.select(root, set(), {})

    assert not candidates[0].dispatchable
    assert "CARD_COMFORT_BYTES" in candidates[0].reason   # reported, via the skip
    assert runner.oversized_entries(candidates) == []     # and not a second time


def test_the_size_the_digest_shows_is_the_size_the_message_quotes(tmp_path):
    """Two surfaces, one measurement. A digest that said 15.9 KB while the run
    log said 16.3 KB about the same card would make both unusable, which is why
    `card_bytes` exists rather than each caller measuring for itself."""
    root = _repo(tmp_path)
    _charter(root, "code-thread")
    _grow(_card(root, "tasks", "fat"), runner.CARD_COMFORT_BYTES + 2000)
    candidates = runner.select(root, set(), {})

    (_, size, _), = runner.oversized_entries(candidates)
    stated, _ = (float(n) for n in re.findall(r"([\d.]+) KB", candidates[0].reason))
    assert f"{size / 1024:.1f}" == f"{stated:.1f}"


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


def test_settle_recovers_a_parked_question_written_on_the_branch_over_a_stale_one(
    tmp_path,
):
    """parked-settle-trusts-stale-question-over-worker-branch, second occurrence
    (cross-language-text-overflow-guard attempt 5, 2026-08-12): a card whose
    `## Question` already had content — fully answered, sent back to `tasks/`,
    then redispatched and parked again — must not have the WORKER'S new
    question silently discarded just because `card.text`'s pre-dispatch
    snapshot already had something under that heading. `settle` must read what
    the worker actually committed on its own branch, not infer it from
    presence-on-the-snapshot, and must prepend rather than replace so the
    answered history survives alongside the new question."""
    root = _repo(tmp_path)
    _card(root, "tasks", "unclear", attempts="2", started="2026-07-23T03:00:00")
    card = board.find(root, "unclear")
    card.write_section("Question", "Old, already-answered question from a prior attempt.")
    card.write({"branch": "ai/unclear"})
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "card with an old, answered question"],
                   cwd=root, check=True)

    subprocess.run(["git", "branch", "ai/unclear"], cwd=root, check=True)
    seed = tmp_path / "seed-unclear"
    subprocess.run(["git", "worktree", "add", str(seed), "ai/unclear"], cwd=root, check=True)
    branch_card_path = seed / "Board" / "tasks" / "unclear.md"
    branch_text = board.append_section(
        branch_card_path.read_text(encoding="utf-8"),
        "Question", "New question the worker actually asked this attempt.")
    branch_card_path.write_text(branch_text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", "worker: parked with a new question"],
                   cwd=seed, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(seed)], cwd=root, check=True)

    runner.settle(root, "unclear", runner.Dispatch("parked", "truncated attempt summary"))
    result_text = board.find(root, "unclear").text
    assert "New question the worker actually asked this attempt." in result_text
    assert "Old, already-answered question from a prior attempt." in result_text
    assert "truncated attempt summary" not in result_text


def test_settle_leaves_an_unchanged_question_alone(tmp_path):
    """The branch's `## Question` is byte-identical to the pre-dispatch one (the
    worker never touched it) — not the recovery path, and not the no-question
    fallback either. No duplication, no `result.detail` noise."""
    root = _repo(tmp_path)
    _card(root, "tasks", "unclear", attempts="2", started="2026-07-23T03:00:00")
    card = board.find(root, "unclear")
    card.write_section("Question", "Same question, never touched by the worker.")
    card.write({"branch": "ai/unclear"})
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "card with a question"], cwd=root, check=True)

    subprocess.run(["git", "branch", "ai/unclear"], cwd=root, check=True)

    runner.settle(root, "unclear", runner.Dispatch("parked", "truncated attempt summary"))
    result_text = board.find(root, "unclear").text
    assert result_text.count("Same question, never touched by the worker.") == 1
    assert "truncated attempt summary" not in result_text


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


def test_dry_run_only_reports_the_kill_switch_never_deletes_it(tmp_path):
    """`--dry-run` promises no writes, full stop — a stale switch is information
    to show, not something even this path may clear away."""
    root = _repo(tmp_path)
    (root / ".ai").mkdir(exist_ok=True)
    (root / runner.STOP_FILE).write_text("note\n", encoding="utf-8")
    assert not runner.preflight(root, "development_team", dry_run=True).ok
    assert (root / runner.STOP_FILE).is_file()


def test_a_named_card_still_refuses_on_a_stale_kill_switch(tmp_path):
    """Naming a card is explicit enough that it must not silently swallow a
    switch someone may have dropped moments ago for a still-relevant reason —
    only a plain start clears it (Karel, 2026-08-22: "card start should not
    delete STOP")."""
    root = _repo(tmp_path)
    (root / ".ai").mkdir(exist_ok=True)
    (root / runner.STOP_FILE).write_text("note\n", encoding="utf-8")
    check = runner.preflight(root, "development_team", dry_run=False, named_card=True)
    assert not check.ok
    assert any("kill switch" in r for r in check.reasons)
    assert (root / runner.STOP_FILE).is_file()


def test_a_plain_start_clears_a_stale_kill_switch_and_proceeds(tmp_path):
    """A real, un-named start's job is to run — a leftover switch from a run
    that already stopped must not force a manual `rm` every time (Karel,
    2026-08-22: "runner should clear at the start of the run")."""
    root = _repo(tmp_path)
    (root / ".ai").mkdir(exist_ok=True)
    (root / runner.STOP_FILE).write_text("note\n", encoding="utf-8")
    check = runner.preflight(root, "development_team", dry_run=False)
    assert not any("kill switch" in r for r in check.reasons)
    assert not (root / runner.STOP_FILE).is_file()


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


# --- the generated views are committed AND exempt ---------------------------
#
# Enumerated over `board.GENERATED_VIEWS` rather than named one per test, because
# the defect being pinned is a view existing that one of these lists has never
# heard of: `ingest` and `chores` each shipped one and joined neither list, and
# `Digest.md`-by-name passed every test in this file while doing it. A fourth view
# is covered by these three the day it is added to the tuple.


@pytest.mark.parametrize("view", board.GENERATED_VIEWS)
def test_a_generated_view_does_not_count_as_a_dirty_tree(tmp_path, view):
    """Each is rewritten by the command that owns it, often immediately before a
    dispatch — `ingest` routes the inbox, `chores` plans the batch. Reading one as
    somebody's work in progress refuses the run that just wrote it."""
    root = _repo(tmp_path)
    (root / view).write_text(f"# {view}\n", encoding="utf-8")
    assert runner.dirty_outside_board(root) == []


def test_every_generated_view_together_still_leaves_a_clean_tree(tmp_path):
    """The measured failure, 2026-08-14: `Routing.md` and `Chores.md` sitting at
    the root made the tree dirty and `chores` refused to dispatch at all — the two
    commands blocked each other."""
    root = _repo(tmp_path)
    for view in board.GENERATED_VIEWS:
        (root / view).write_text(f"# {view}\n", encoding="utf-8")
    assert runner.dirty_outside_board(root) == []


@pytest.mark.parametrize("view", board.GENERATED_VIEWS)
def test_commit_board_stages_a_generated_view_that_exists(tmp_path, view):
    """Exempt from the dirty check is only half of it. A view that is never
    committed exists on one machine only, which is board state the other machine
    cannot see."""
    root = _repo(tmp_path)
    _card(root, "tasks", "moved")
    (root / view).write_text(f"# {view}\n", encoding="utf-8")
    board.commit_board(root, "board: probe")
    log = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    assert view in log.stdout


def test_commit_board_stages_the_board_when_no_view_exists_yet(tmp_path):
    """A fresh clone has none of the three, and `git add` fails the *whole*
    pathspec when one entry matches nothing — which would silently stage nothing
    and turn the `attempts` bookkeeping commit into a no-op."""
    root = _repo(tmp_path)
    _card(root, "tasks", "moved")
    for view in board.GENERATED_VIEWS:
        assert not (root / view).exists()
    board.commit_board(root, "board: probe")
    log = subprocess.run(["git", "show", "--stat", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True)
    assert "moved" in log.stdout


def test_the_correction_prompt_hook_ignores_every_generated_view():
    """The third consumer of the same list. A regenerated report is not evidence
    that work happened, so it must not trip the correction nudge."""
    from nightshift.hooks import correction_prompt

    for view in board.GENERATED_VIEWS:
        assert view in correction_prompt._IGNORED


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
