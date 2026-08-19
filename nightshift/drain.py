#!/usr/bin/env python3
"""The exit `review/` never had.

`review/` means *gates and tests are green, the reviewer has not concluded*. Its
exit is supposed to be an LLM review that routes the card onward — to `testing/`
when the diff is fine, to `needs-decision/` when it found something only a human
can settle.

**Nothing drained it.** `runner.review_stage` runs *inside* a dispatch, so a card
was only ever reviewed on its way through. Once a card came to rest in the lane,
no queue picked it up: the night and the chore batch both take their work from
`tasks/`, and the batch's single review runs over the merged diff. Every path in
`review_stage` that cannot conclude — a usage wall mid-review, an unreadable
verdict, no CLI, a timeout — ends by deliberately leaving the card in `review/`
"for a manual look". Each of those is right on its own. Together they meant the
lane accumulated cards whose review simply never happened, and nothing said so.
The runner's own comment carried the ambiguity that hid it: it called the outcome
*"awaiting the review stage **or** a human's eye"*, which is two different things
behind one lane name.

So: this module is the pass that takes each card in `review/` with commits on its
branch, runs the review it is owed, and routes it on the verdict.

Two shape decisions, recorded here because both were real choices
------------------------------------------------------------------

**1. The night does not drain the lane. This is a command, run deliberately.**

The tempting alternative — draining before the night takes its queue — makes the
night self-healing, and it was rejected on three grounds:

* A card in `review/` is *already green*. Its gates and tests passed; nothing
  about it decays overnight. The urgency of concluding it belongs to the person
  waiting on it, not to the machine, and a night that spent its window on
  yesterday's leftovers before starting today's work would be paying for that
  urgency with the one resource the whole cost programme is about.
* The lane is unbounded and the night's accounting is not built for it. Attempts,
  backoff, rescue branches, the session/wall arithmetic and the run record all
  hang off a `Candidate` taken from `tasks/`. A drain inside the loop would either
  duplicate that arithmetic or restructure it, in a module that runs unattended
  and has ~320 tests around it — for work that is not time-critical.
* The invocation has a home already. The Verify page surfaces a stuck card and
  its button is this command; the defect was that the lane had *no* exit, not
  that it lacked a nightly one.

What would have to change to revisit it: the drain would have to join the night's
session and wall accounting rather than sit beside it, and there would have to be
evidence that cards actually pile up faster than they are looked at. Neither is
true today.

**2. An `ok` verdict merges, through `runner.settle` — nothing is hand-rolled.**

A drain reviewing a card whose branch never merged has to decide what `ok` means.
It means exactly what it means inside a dispatch: `settle` rebases the branch onto
the integration tip, re-verifies the replayed result, merges, and lands the card in
`testing/` or `done/` per its own `verify:`. A drain that only moved the card would
leave a merge nobody performed and a lane saying it was done; a drain with its own
merge would be a second implementation of the most dangerous step in the system.
The failure path comes free with it too: a branch that will not rebase-and-merge
goes back to `review/` carrying a `## Merge` note that says why, which is what that
lane is for.

The rest is the standing rules, applied
---------------------------------------

* **`review_stage` is reused, not reimplemented.** It already owns the criteria,
  the reviewer agent, the tier resolution, the throwaway-checkout blindness and
  every degradation path.
* **The two cards that legitimately rest here are left alone, and cost nothing.**
  One is artefact-only — no commit on its branch (`art`, or a branch never cut) —
  and its check is a single `git rev-list` before any spend. The other has already
  been reviewed `ok` and carries `## Merge`, meaning its branch would not rebase
  onto the integration tip: the review is finished and a person is the blocker, so
  buying that verdict again on every pass would be pure repricing. Both are skipped
  in a sweep and both are reviewed anyway when named with `--card`, because an
  explicit request is a decision — the same waiver `runner --card` makes.
* **The money rule is checked before each review, never once for the pass.** A
  fan-out that begins with headroom can lose it partway, and a review cannot be
  un-started.
* **A wall stops the pass, with the remaining cards untouched.** They are still in
  `review/`, still green, and the next pass takes them from the top.
* **One at a time.** The pass takes the runner's lock: it merges into the
  integration branch and moves cards, and doing that while a night is in flight is
  the one way this could damage something.

No card is ever routed to `needs-decision/` merely because it could not be
reviewed. That lane means a decision only a human can make, and *"review this
diff"* is not a decision — it is work. Moving the problem to a lane that gets read
more often would hide it rather than fix it.

Nor is a card routed there merely because the reviewer found something wrong with
it (`reviewer-needs-fix-verdict`, 2026-08-19). A `needs_fix` verdict is a concrete,
verifiable defect with one correct answer, not a choice — "apply this fix" is not
a decision either, so `runner.settle` sends the card back to `tasks/` for another
attempt instead, the same bounded retry an ordinary `failed` attempt gets. Only a
fix that keeps recurring past the card's attempt limit escalates to
`needs-decision/`, because at that point it has stopped being mechanical.

    python -m nightshift.drain --dry-run          # what the lane holds, and what would run
    python -m nightshift.drain                    # review everything with a diff
    python -m nightshift.drain --card <id>        # one card — the panel's per-row button
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board, runner, usage
from nightshift.manifest import find_root

#: The lane this drains, and the one a degraded review leaves a card in.
LANE = "review"

#: The section `runner.settle` writes onto a card that came back `ok` and whose branch
#: would then not rebase onto the integration tip. Its presence means the review is
#: **done** and what is left is a conflict only a person can resolve — so a sweep skips
#: it, for the same reason it skips an artefact-only card. Found by the first real pass:
#: the card it drained reviewed `ok` in 94 seconds for $0.82 and then hit a genuine
#: conflict, and nothing would have stopped the next pass buying that verdict again.
BLOCKED_SECTION = "Merge"

#: What happened to one card in a pass.
#:   REVIEWED      — the reviewer said ok; `settle` merged it and moved it on
#:   NEEDS_FIX     — the reviewer found a fixable defect; `settle` sent it back to
#:                   `tasks/` for another attempt (or, past its attempt limit, on to
#:                   needs-decision/ — either way `settle` decides, this is just the log)
#:   NEEDS_DECISION— the reviewer flagged a choice; `settle` filed it with the question
#:   LEFT          — the review could not conclude; the card stays in `review/`
#:   SKIPPED       — no commits on its branch; nothing to review, nothing spent
#:   NOT_REACHED   — the pass stopped before this card; it is untouched
REVIEWED, NEEDS_FIX, NEEDS_DECISION = "reviewed", "needs-fix", "needs-decision"
LEFT, SKIPPED, NOT_REACHED = "left", "skipped", "not-reached"


@dataclass(frozen=True)
class Outcome:
    """One card's result. `detail` is `settle`'s own account where there is one —
    the same sentence the night's log carries — so the two readings agree."""

    card_id: str
    state: str
    detail: str
    cost_usd: float = 0.0


@dataclass
class Pass:
    """What one drain did. `stopped` is empty when the pass ran to the end."""

    outcomes: list[Outcome] = field(default_factory=list)
    stopped: str = ""

    @property
    def cost_usd(self) -> float:
        return sum(o.cost_usd for o in self.outcomes)

    def by_state(self, state: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.state == state]


def branch_of(card: board.Card) -> str:
    """The branch a card's work is on — the same derivation `review_stage` and
    `settle` use, so all three agree about a card whose `branch:` was never written."""
    return card.fields.get("branch") or f"ai/{card.id}"


def waiting(root: Path, card_id: str = "") -> list[board.Card]:
    """Every card at rest in `review/`, in board order; or the one named.

    A named card that is *not* in `review/` comes back empty rather than being
    reviewed where it stands: the lane is the precondition, and a card in
    `tasks/` wanting a review wants a dispatch instead.
    """
    cards = board.cards(root, LANE)
    return [c for c in cards if c.id == card_id] if card_id else cards


def skip_reason(root: Path, base: str, card: board.Card, *, named: bool = False) -> str:
    """Why `card` would not be reviewed this pass, or `""` if it would be.

    **One predicate, two readers.** The pass consults it before spending anything, and
    `--dry-run` prints it. Those were two separate checks for exactly one commit, and a
    dry run that disagrees with the pass is worse than no dry run at all — its entire
    job is to say what the pass will do, and the blocked-card skip landed in one of
    them and not the other.

    `named` is what `--card` buys: an explicit request is a decision, so the checks a
    sweep applies to protect an at-rest card are waived, the same way `runner --card`
    waives `unattended:`, backoff and the attempt limit. The commits check is *not*
    waived — there is genuinely nothing to review, and no request makes a diff exist.
    """
    branch = branch_of(card)
    if not runner.branch_has_commits(root, base, branch):
        return (f"no commits on `{branch}` — nothing to review as a diff; this is where an "
                f"artefact-only card waits for a human, and it was left alone")
    if not named and board.section(card.text, BLOCKED_SECTION):
        return (f"already reviewed `ok`; `## {BLOCKED_SECTION}` says its branch will not "
                f"land without a human, and reviewing it again would buy the same verdict "
                f"at full price. Name it with `--card` to review it anyway")
    return ""


def drain(root: Path, base: str, *, card_id: str = "", limit: int = 0,
          allow_paid: bool = False, card_budget: float = 0.0,
          test_timeout: int = 600) -> Pass:
    """Review each card in `review/` that has a diff, and route it on the verdict.

    Every stop is recorded on the returned `Pass` and every card the pass did not
    reach is listed as `NOT_REACHED` rather than omitted — "the drain ran and this
    card is still here" and "the drain never got to it" are different facts, and
    the second one is the one worth acting on.
    """
    result = Pass()
    cards = waiting(root, card_id)
    if limit > 0:
        cards = cards[:limit]

    for index, card in enumerate(cards):
        if (root / runner.STOP_FILE).is_file():
            result.stopped = "the kill switch appeared"
            result.outcomes += [Outcome(c.id, NOT_REACHED, result.stopped)
                                for c in cards[index:]]
            break

        branch = branch_of(card)
        # Before the money check and before anything is spawned: the two cards that
        # legitimately rest in this lane must cost nothing at all, or the drain becomes
        # a tax on the cards nobody asked to have re-reviewed.
        if reason := skip_reason(root, base, card, named=bool(card_id)):
            result.outcomes.append(Outcome(card.id, SKIPPED, reason))
            continue

        verdict = usage.check(usage.read(), allow_paid=allow_paid)
        if not verdict.allow:
            result.stopped = verdict.reason
            result.outcomes += [Outcome(c.id, NOT_REACHED, verdict.reason)
                                for c in cards[index:]]
            break

        # The input `Dispatch` stands in for the one a dispatch would have handed
        # over: outcome `review` (gates and tests are green, nobody has concluded),
        # no wall, no cost. `review_stage` returns it unchanged on every path that
        # cannot conclude, which is how the degradations stay one implementation.
        reviewed = runner.review_stage(
            root, card, runner.Dispatch("review", _detail(card, branch),
                                        how_to_test=_how_to_test(card, branch)),
            base, card_budget, test_timeout)

        if reviewed.outcome in ("reviewed", "needs_fix", "needs_decision"):
            landed = runner.settle(root, card.id, reviewed)
            state = (REVIEWED if reviewed.outcome == "reviewed"
                     else NEEDS_FIX if reviewed.outcome == "needs_fix"
                     else NEEDS_DECISION)
            result.outcomes.append(Outcome(card.id, state, landed, reviewed.cost_usd))
        else:
            result.outcomes.append(Outcome(
                card.id, LEFT,
                f"the review could not conclude ({reviewed.detail}) — left in "
                f"{LANE}/ for a manual look", reviewed.cost_usd))

        # A wall is a fact about the window, not about this card, so it ends the
        # pass whether or not the card itself landed. The rest are untouched and
        # the next pass takes them from the top — there is no state to resume.
        if reviewed.wall is not None:
            result.stopped = (f"a usage limit closed the window "
                              f"({reviewed.wall.scope})")
            result.outcomes += [Outcome(c.id, NOT_REACHED, result.stopped)
                                for c in cards[index + 1:]]
            break

    return result


def _detail(card: board.Card, branch: str) -> str:
    """The account a degraded review carries back. Never written onto the card by
    this path — `settle` only reads `detail` for outcomes the drain does not
    settle — but it is what the operator reads in the pass report."""
    return (f"drained from {LANE}/ after {card.attempts} dispatch attempt(s); "
            f"the diff is `{branch}`")


def _how_to_test(card: board.Card, branch: str) -> str:
    """The scenario a `verify: play` card carries into `testing/` when it merges.

    Inside a dispatch this comes from the worker's own verdict, because only the
    thing that built it knows which door it is behind. A drain runs long after
    that worker exited and its run directory may have been pruned, so the card's
    own `## How to test` is used when a previous pass wrote one, and otherwise the
    absence is stated plainly. What it must *not* do is fall through to
    `settle`'s default, which reads "the worker recorded no scenario — that is
    itself a defect": true of a dispatch, and an accusation of the wrong party here.
    """
    return board.section(card.text, "How to test") or (
        f"Reviewed by the {LANE}/ drain rather than by the dispatch that produced it, "
        f"so the worker's own scenario was not carried over. The diff is on `{branch}`.")


def _work_root(root: Path, base: str) -> Path:
    """Where the merging happens — the same topology the night uses.

    `merge_branch` refuses unless the checkout is on the integration branch, so a
    drain run from a checkout on someone's feature branch would review correctly
    and then fail every merge, dropping each card back into `review/` with a note.
    That is a silent uselessness rather than an error, which is why this mirrors
    `run()` instead of hoping: work in place when the checkout is already on
    `base`, otherwise use the runner's dedicated `base` checkout.
    """
    if runner.current_branch(root) == base:
        return root
    return runner.ensure_integration_checkout(root, base)


def describe(result: Pass) -> list[str]:
    """The pass as lines, one per card, plus why it ended if it did."""
    lines = [f"  {o.state:12} {o.card_id} — {o.detail}" for o in result.outcomes]
    if result.stopped:
        lines.append(f"  stopped — {result.stopped}; the cards above marked "
                     f"`{NOT_REACHED}` are untouched")
    if result.cost_usd:
        lines.append(f"  ${result.cost_usd:.2f} equivalent spent")
    return lines


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="python -m nightshift.drain", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to drain (default: found from the working directory)")
    parser.add_argument("--base", default=None,
                        help="integration branch to review against and merge into "
                             "(default: the manifest's)")
    parser.add_argument("--card", default="",
                        help="drain only this card, if it is in review/")
    parser.add_argument("--limit", type=int, default=0,
                        help="review at most N cards this pass (0 = every one)")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what the lane holds and what would be reviewed; "
                             "spawn nothing, move nothing")
    parser.add_argument("--allow-paid", action="store_true",
                        help="proceed even if a review would draw on paid credits")
    parser.add_argument("--card-budget", type=float, default=0.0,
                        help="optional USD cap handed to each reviewer process")
    parser.add_argument("--test-timeout", type=int, default=600,
                        help="seconds allowed for the re-verification a merge runs")
    args = parser.parse_args(argv)

    root = (args.root or find_root()).resolve()
    base = args.base or runner.default_base(root)

    if args.dry_run:
        cards = waiting(root, args.card)
        print(f"drain: {len(cards)} card(s) in {board.board_rel(root).as_posix()}/{LANE}")
        for card in cards:
            reason = skip_reason(root, base, card, named=bool(args.card))
            print(f"  {'leave ' if reason else 'review'} {card.id} — "
                  f"{reason or branch_of(card)}")
        print("\n(dry run — nothing dispatched)")
        return 0

    try:
        work = _work_root(root, base)
    except RuntimeError as exc:
        print(f"refusing to drain — {exc}", file=sys.stderr)
        return 1
    if dirty := runner.dirty_outside_board(work):
        shown = ", ".join(dirty[:4]) + (f" (+{len(dirty) - 4} more)" if len(dirty) > 4 else "")
        print(f"refusing to drain — the `{base}` checkout is dirty outside the board: "
              f"{shown}. A review merges into it; commit or discard those first.",
              file=sys.stderr)
        return 1

    # The lock, not politeness: this merges into `base` and moves cards, and a
    # night doing the same thing at the same time is the one way a drain could
    # damage something rather than merely fail.
    if not runner.acquire_lock(root):
        return 1
    try:
        runner.ensure_workspace_trusted(root)
        result = drain(work, base, card_id=args.card, limit=args.limit,
                       allow_paid=args.allow_paid, card_budget=args.card_budget,
                       test_timeout=args.test_timeout)
    finally:
        runner.release_lock(root)

    if not result.outcomes:
        target = f"card `{args.card}`" if args.card else "cards"
        print(f"drain: no {target} at rest in {LANE}/ — nothing to do")
        return 0
    print(f"drain: {len(result.outcomes)} card(s) in {LANE}/")
    for line in describe(result):
        print(line)
    return 3 if result.stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
