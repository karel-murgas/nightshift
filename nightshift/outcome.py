"""The `Dispatch` outcome type -- one attempt's result, threaded through the
pipeline from a dispatch attempt to review to settle.

Deliberately its own module, smaller than any job in the split: `dispatch.py`
constructs it (a producer/checker attempt, a repair, a crash), `review.py`
constructs it too (a review verdict, a pick, an unreviewable card), and
`settle.py` reads it to decide what happens to the board. Splitting the type
out from either producer is what lets those two import each other's outputs
without importing each other.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nightshift import limits

@dataclass
class Dispatch:
    # "review" (gates+tests passed and a review is OWED AND OBTAINABLE — the stage
    # has not run yet, the window closed before it could, or the card is
    # artefact-only and Karel is its reviewer. `nightshift.drain` is the pass that
    # takes it from there, and since 2026-08-25 the night runs one itself at the
    # end. Until `drain` existed this comment said "awaiting the review stage *or*
    # a human's eye", and those two states behind one lane name are what hid
    # `review/` having no exit at all; the second of them is now "unreviewable") |
    # "unreviewable" (gates+tests passed but no verdict can be had here — no CLI,
    # no tier binding, a timeout, an unreadable verdict. Settle files it to
    # blocked/ with the command that unsticks it, never to review/, because a card
    # nothing will come back for must not sit in the lane that means it will) |
    # "reviewed" (the diff reviewer said ok — settle merges and lands it in testing/) |
    # "pick" (the same, except the attempt produced candidates and installed none of
    # them, so what is left is the maintainer choosing one: settle merges the diff
    # exactly as for "reviewed" and then files the card in needs-decision/ with the
    # pick as its question. `unadopted_artefacts` is the whole test, and its
    # docstring carries the failure it came out of) |
    # "needs_fix" (the reviewer found a concrete, verifiable defect with one correct
    # answer — settle sends the card back to tasks/ for another attempt, bounded by
    # the same attempt_limit as "failed"; only escalates to needs-decision if the
    # limit is exhausted) |
    # "needs_decision" (the reviewer flagged a choice for Karel) | "parked" |
    # "failed" | "limited" | "blocked" | "interrupted" (an API disconnection
    # that never reached verification — gives the attempt back like "blocked",
    # but does not stop the night: it is a fact about one connection, not the
    # repo or this machine)
    outcome: str
    detail: str
    cost_usd: float = 0.0
    rounds: int = 1
    # Set whenever a usage wall landed anywhere in this card's pipeline — NOT
    # only on "limited" (`wall-on-review-wrapup-discards-a-verdict`). A wall
    # carries two independent facts, and conflating them was the defect: "this
    # attempt produced nothing" (an accounting claim, often false) and "the
    # plan's window is closed" (a scheduling claim, always true). Only the second
    # is evidence, so a stage that had already written a terminal verdict now
    # honours it — the card settles on `review`/`parked`/`needs_decision` like any
    # other — and the wall rides home on this field regardless. The run loop
    # consults it after settling *any* outcome, so a card that landed still costs
    # the night its session and still triggers the sleep-or-stop.
    #
    # Carries the reset time the run loop needs to decide between sleeping and
    # stopping, and the evidence line for the run log.
    wall: limits.Wall | None = None
    # Set only on "limited" (runner-worker-handover). `kept` is True when the
    # worktree and session were preserved for a warm resume rather than dropped;
    # `progressed` is whether the working tree advanced since the last attempt
    # (the give-back gate); `stuck` is the no-progress breaker tripping, which
    # routes the card to failed/ instead of giving the attempt back again.
    kept: bool = False
    progressed: bool = True
    stuck: bool = False
    # Set on "failed": the bounded, quotable evidence behind `detail` — pytest's
    # failing tests and their assertions (`suite.failure_excerpt`) or the gate
    # log's own lines. `detail` is one line and goes in the run log; this is the
    # block `settle` writes onto the card so the reason survives being read from
    # another machine, or after `prune_run_dir` has taken the attempt directory.
    evidence: str = ""
    # Set only on "blocked": whether this is a gate violation whose every path
    # lies outside the attempt's own diff (repo drift, `failed-attempt-work-
    # is-deleted-not-resumed`) rather than the pre-existing gate-harness-crashed
    # reason. Both give the attempt back and stop the night; only the reason
    # text differs, and `settle` needs to know which one it is telling.
    repo_drift: bool = False
    # Set when a drift repair landed on this card's branch inside its own attempt
    # (drift-should-not-end-the-night): the drifted gate names and the repair
    # commit. It reaches the reviewer, which is judging the diff against the
    # card's criteria and would otherwise be right to object to a change that
    # meets none of them; and it reaches the card, so a diff carrying someone
    # else's fix says so where a human will read it.
    repaired: str = ""
    # The worker's scenario for Karel, written onto a `verify: play` card as
    # `## How to test` when it merges. It rides from the worker's verdict through
    # the review stage rather than being asked for at the end: only the worker
    # that built the thing knows which door it is behind, and `## Summary` proved
    # the shape — written deterministically by `settle()` from the verdict rather
    # than left to agent discretion (menu-summary-on-card).
    how_to_test: str = ""
    # The `--effort` every stage of this attempt was spawned with
    # (`stage_efforts`). Carried rather than recomputed at the record site
    # because `dispatch` is where the override is applied, and a second
    # resolution there would be free to disagree with what actually ran —
    # `token-economy.md` defect 4a in a new place.
    efforts: dict[str, str] = field(default_factory=dict)
    # How many harvested candidates this attempt produced without installing any —
    # `unadopted_artefacts`, computed in `dispatch` where both of its inputs are
    # already in hand, and carried rather than re-derived because the diff it reads
    # is gone by the time `settle` has merged the branch. Non-zero turns a
    # "reviewed" into a "pick" (see the outcome list above); zero is every card
    # whose deliverable is its diff.
    unadopted: int = 0
