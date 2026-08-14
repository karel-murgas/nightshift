"""Chores: one-prompters, dispatched and verified as a batch.

A chore is a work item where the note already said what to change and what the result
should be — no fork, one obvious home for the change, a wrong result that is visible
rather than subtle. `kind: chore` in the frontmatter; `card_schema` relaxes exactly one
thing for it (`## Approach`) and nothing else.

**Why they need their own path at all.** The dispatcher's unit of work is a card, so
anything not worth a card could not be automated regardless of whether it needed a
human. That is the whole gap: a class of items that would each take one prompt, need
no decision, and were stuck being done by hand one at a time.

**Why they are verified as a batch.** Verified independently, eight chores cost eight
full suite runs *and* eight pre-merge checks, because each merge to the integration
branch needs its own. Measured complaint, 2026-08-14: the verification took longer than
the work. So:

1. **Per chore, cheap and parallel** — its own worktree, then the gates and only the
   tests that touch what it changed. Not the suite. A failure here parks the item and
   drops it from the batch.
2. **Per batch, once** — merge the survivors serially onto one batch branch, then run
   the full suite *once* over the combined result. One pre-merge check, one merge.
3. **Review once, over the whole batch diff** — the question for a chore is "did any
   of these do something needing a human decision", which is answerable across the set,
   and a batch-wide reviewer sees interactions between items that per-item review
   cannot.

**This is an optimistic scheme, deliberately.** All-green costs one suite run instead
of eight; one failure costs one plus a bisect. It wins if batches are usually green and
loses modestly if they are not — and if they are usually not, the finding is that
routing is too liberal, not that batching was wrong. A narrow phase-1 slice is safe
*because* phase 2 runs everything: a false green in phase 1 is caught at the batch
suite, which is not true of a narrow slice on a lone card.

**What defines a chore is the shape of the request, not the size of the diff.** A
line-count cap was proposed and rejected: a mechanical edit across three translation
dicts is 200 lines and trivial, a subtle off-by-one is four lines and hard. Output size
does not track simplicity. The enforcement is the worker's own bounce — it is the only
actor that opens the code, so it is the one that discovers the truth — with an *effort*
budget as the mechanical backstop, because effort does track simplicity.

**The worker tier for a chore is the cheap one.** Decided 2026-08-14: a one-prompter
does not want the session's top model, and the definition of the kind — no fork, one
obvious home, a visible failure — is exactly the description of work a mid tier does
well. The model is not a constant here on purpose: nothing in this module dispatches
yet, and an unread constant is what `dead_code` exists to catch. It belongs in the
execution half, as a short alias rather than a dated id.

**Not yet wired: the execution half.** Selection, the effort budget, the bisect
decision and the report live here and are tested. Driving worktrees, merges and the
suite runs means changing the dispatcher's per-card verification into the two phases
above, which is surgery on the module that runs unattended overnight — done separately,
on purpose, rather than hastily alongside this.
"""
from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board, textio
from nightshift.runner import repo_root

#: Written at the repo root next to the digest, because that is the vault root.
OUT = Path("Chores.md")

#: How many chores go in one batch by default. Not a technical limit — a review limit:
#: the batch's value is that one pass of human attention covers all of it, and that
#: stops being true somewhere past a screenful.
DEFAULT_BATCH = 8

#: The mechanical backstop on "was this actually a one-prompter". Effort, not output:
#: a chore that takes 40 turns was not a chore whatever its diff looks like. Generous
#: on purpose — this catches runaways, and the bounce catches misroutes.
MAX_TURNS = 40
MAX_WALL_S = 20 * 60

#: A chore gets one attempt. A failed one-prompter is worth a human's eye, not a second
#: dispatch, and it keeps the arithmetic honest: 8 chores x 3 attempts is a night.
MAX_ATTEMPTS = 1

KIND = "chore"


@dataclass(frozen=True)
class Skipped:
    """A chore card that cannot join this batch, and why. Never silent."""

    card_id: str
    reason: str


@dataclass
class Outcome:
    """What became of one chore in a batch."""

    card_id: str
    state: str = "pending"      # pending | done | bounced | parked | blocked
    detail: str = ""
    turns: int = 0
    wall_s: float = 0.0
    verify: str = "play"
    surface: str = ""
    title: str = ""

    @property
    def needs_an_eye(self) -> bool:
        """Whether this belongs on the human checklist.

        A `review`-verified chore that landed green is *not* on it: the gates and the
        suite were the acceptance. Putting it there anyway is how a checklist gets long
        enough to stop being read.
        """
        return self.state == "done" and self.verify != "review"


@dataclass
class Batch:
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)

    def by_state(self, state: str) -> list[Outcome]:
        return [o for o in self.outcomes if o.state == state]

    @property
    def survivors(self) -> list[Outcome]:
        """The ones eligible to merge onto the batch branch."""
        return self.by_state("done")


def effort_exceeded(turns: int, wall_s: float, *, max_turns: int = MAX_TURNS,
                    max_wall_s: float = MAX_WALL_S) -> str:
    """Why this chore overran its budget, or `""` if it did not.

    Returns prose rather than a bool because the reason lands on the card, and "it took
    too long" without a number is a finding nobody can act on.
    """
    if turns > max_turns:
        return f"took {turns} turns (budget {max_turns}) - not a one-prompter"
    if wall_s > max_wall_s:
        return (f"ran {wall_s / 60:.0f} min (budget {max_wall_s / 60:.0f} min) "
                f"- not a one-prompter")
    return ""


def eligible(card: board.Card) -> str:
    """Why `card` cannot join a batch, or `""` if it can.

    Kept separate from `select` so the reason can be reported per card. A chore that is
    silently absent from a batch is indistinguishable from one that was never written.
    """
    if card.fields.get("kind") != KIND:
        return "not a chore"
    if card.tier == "lead":
        # `card_schema` already refuses this combination; checked again because a
        # hand-edited card reaches the dispatcher before a gate run does.
        return "tier: lead - a chore has nothing to decide"
    if card.fields.get("unattended", "true").strip().lower() != "true":
        return "unattended: false - the batch runs without a human present"
    if card.worker in ("", "none"):
        return "no worker - nothing would pick it up"
    try:
        attempts = int(card.fields.get("attempts", "0") or 0)
    except ValueError:
        attempts = 0
    if attempts >= MAX_ATTEMPTS:
        return (f"already attempted {attempts}x - a chore gets {MAX_ATTEMPTS}; "
                f"read it rather than re-running it")
    return ""


def select(root: Path, *, limit: int = DEFAULT_BATCH,
           lane: str = "tasks") -> tuple[list[board.Card], list[Skipped]]:
    """The chores that form the next batch, and every one that was left out with why."""
    chosen: list[board.Card] = []
    skipped: list[Skipped] = []
    for card in board.cards(root, lane):
        if card.fields.get("kind") != KIND:
            continue                      # not a chore at all: not "skipped", just other work
        why = eligible(card)
        if why:
            skipped.append(Skipped(card.id, why))
            continue
        if len(chosen) >= limit:
            skipped.append(Skipped(
                card.id, f"batch is full at {limit} - it will lead the next one"))
            continue
        chosen.append(card)
    return chosen, skipped


def next_probe(candidates: list[str]) -> str | None:
    """The next chore to test in isolation when a batch suite went red.

    Binary search over the merge order: the dispatcher holds one commit per chore, so
    attribution never has to guess. Midpoint rather than last-merged-first, because a
    batch that reddens is as likely to have broken early as late, and last-first is
    only better when the newest change is the usual suspect.
    """
    if not candidates:
        return None
    return candidates[len(candidates) // 2]


def report(batch: Batch, now: dt.datetime, *, branch: str = "",
           suite: str = "") -> str:
    """The aggregated checklist: one pass of attention for the whole batch.

    Grouped by `surface:` — free text the project supplies, not a vocabulary this
    package invents — so one run through the app exercises several items instead of one
    per card. That grouping is the entire reason the batch is cheaper to verify than
    its parts.
    """
    lines = [f"# Chore batch - {now:%Y-%m-%d %H:%M}", ""]
    if branch:
        lines += [f"Batch branch: `{branch}`", ""]
    if suite:
        lines += [f"Suite: {suite}", ""]

    playable = [o for o in batch.outcomes if o.needs_an_eye]
    lines += [f"## Check these ({len(playable)})", ""]
    if not playable:
        lines += ["_nothing needs an eye - every landed chore was `verify: review`, "
                  "so the gates and the suite were its acceptance._", ""]
    else:
        lines += ["Tick what you saw working. An unticked item plus a note comes back "
                  "as a new chore carrying the note.", ""]
        by_surface: dict[str, list[Outcome]] = {}
        for outcome in playable:
            by_surface.setdefault(outcome.surface or "unsorted", []).append(outcome)
        for surface in sorted(by_surface):
            lines += [f"### {surface}", ""]
            for outcome in sorted(by_surface[surface], key=lambda o: o.card_id):
                label = outcome.title or outcome.card_id
                lines.append(f"- [ ] **{label}** (`{outcome.card_id}`)")
            lines.append("")

    landed_quietly = [o for o in batch.outcomes
                      if o.state == "done" and not o.needs_an_eye]
    if landed_quietly:
        lines += [f"## Landed without needing you ({len(landed_quietly)})", "",
                  "`verify: review` - green gates and a green suite were the acceptance.", ""]
        lines += [f"- {o.title or o.card_id}" for o in
                  sorted(landed_quietly, key=lambda o: o.card_id)]
        lines.append("")

    for state, title, blurb in (
        ("bounced", "Bounced - not chores after all",
         "The worker opened the code and found a decision, or the change was not where "
         "the note implied. This is the routing signal, not a failure."),
        ("parked", "Parked - failed their own checks",
         "One attempt each, by design. Read them rather than re-running them."),
        ("blocked", "Not reached",
         "The batch stopped before these - usually the usage window closing."),
    ):
        bucket = batch.by_state(state)
        if not bucket:
            continue
        lines += [f"## {title} ({len(bucket)})", "", blurb, ""]
        lines += [f"- **{o.card_id}** - {o.detail}" for o in
                  sorted(bucket, key=lambda o: o.card_id)]
        lines.append("")

    if batch.skipped:
        lines += [f"## Left out of this batch ({len(batch.skipped)})", ""]
        lines += [f"- **{s.card_id}** - {s.reason}" for s in
                  sorted(batch.skipped, key=lambda s: s.card_id)]
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Select and report the next batch of chores.")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help=f"chores per batch (default {DEFAULT_BATCH})")
    parser.add_argument("--plan", action="store_true",
                        help="list the batch and what was left out; dispatch nothing")
    args = parser.parse_args(argv)

    root = args.root or repo_root()
    chosen, skipped = select(root, limit=args.limit)

    print(f"chores: {len(chosen)} selected, {len(skipped)} left out")
    for card in chosen:
        print(f"  + {card.id}")
    for entry in skipped:
        print(f"  - {entry.card_id}: {entry.reason}")

    if not args.plan:
        # Deliberately not a silent no-op: the execution half is a separate change to
        # the dispatcher, and pretending otherwise here would be the worst outcome.
        print("\nexecution is not wired yet - re-run with --plan, or dispatch the "
              "cards individually. See this module's docstring.")
        return 2

    batch = Batch(skipped=list(skipped))
    textio.write_text_lf(root / OUT, report(batch, dt.datetime.now()))
    print(f"  wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
