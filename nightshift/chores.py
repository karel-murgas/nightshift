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
does not track simplicity. **Nor does effort**, which is the correction this module took
on 2026-08-14: a turn budget sat here as a mechanical backstop on the reasoning that
effort tracks simplicity, and it does not — a turn is a tool round-trip, so the count
measures how much of the repo had to be walked. The enforcement is the worker's own
bounce, alone: it is the only actor that opens the code, so it is the only one that
discovers the truth, and it says so in words. `cost_note` records what an item cost and
nothing weighs it.

**The worker tier for a chore is the cheap one.** Decided 2026-08-14: a one-prompter
does not want the session's top model, and the definition of the kind — no fork, one
obvious home, a visible failure — is exactly the description of work a mid tier does
well. Resolved through the tier table by *name* (`CHORE_TIER`) rather than off the
card, so a hand-edited `tier:` cannot pull a batch onto the expensive model — and as
a short alias, never a dated id, because an alias survives a CLI release and a pinned
id rots.

**The money rule is checked before every dispatch, not once per batch.** A fan-out
that starts with headroom can lose it partway, and a dispatch cannot be un-started.
The default is stop; only an explicit per-invocation opt-in continues onto paid
credits.

**Where the two phases live.** Everything that drives a worktree, a merge or a suite
run is `runner`'s — this module calls into it and never grows a second copy. What is
here is the batch's own arithmetic: which items go in, when one has stopped being a
one-prompter, what to merge, what to bisect when the combined result reddens, and
what to put in front of a human at the end.
"""
from __future__ import annotations

import argparse
import datetime as dt
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import (board, gitmerge, manifest, run_record, runner, suite, textio,
                        tiers, usage)
from nightshift.runner import repo_root

#: Written at the repo root next to the digest, because that is the vault root. The
#: name comes from `board`, which owns the set — see `GENERATED_VIEWS` on why a view
#: has to be in one list to be committed and in another to not block a dispatch.
OUT = Path(board.CHORES_VIEW)

#: How many chores go in one batch by default. Not a technical limit — a review limit:
#: the batch's value is that one pass of human attention covers all of it, and that
#: stops being true somewhere past a screenful.
DEFAULT_BATCH = 8

#: There is deliberately **no effort budget here** — no turn cap, no wall cap of this
#: module's own. `cost_note` records what an item cost and nothing weighs it. The
#: runaway that a cap was meant to stop is bounded where it can still be stopped: the
#: wall-clock timeout on the worker's own process, inside `runner._run_worker`. See
#: `cost_note` for the two measurements that removed the budget that used to sit here.

#: A chore gets one attempt. A failed one-prompter is worth a human's eye, not a second
#: dispatch, and it keeps the arithmetic honest: 8 chores x 3 attempts is a night.
#: Defined in `runner` beside the full-card limit, because the dispatcher's queue
#: selection and its retirement rule both read it and must not disagree.
MAX_ATTEMPTS = runner.CHORE_MAX_ATTEMPTS

KIND = board.KIND_CHORE

#: The tier a chore dispatches at, resolved through the project's own tier table.
#: By name, not off the card: `eligible` already refuses `tier: lead`, and naming it
#: here means a hand-edited card cannot pull the whole batch onto the expensive model.
CHORE_TIER = "worker"

#: The batch branch's namespace. Deliberately not under `ai/`, which is the per-card
#: namespace `publish` and the rescue-branch machinery scan: a batch branch is not a
#: card's branch and must not be swept up as one.
BATCH_NAMESPACE = "chores"

#: How many times the combined result may come back red before the whole batch is
#: handed to a human instead of being narrowed further. Two, because the second red
#: is evidence about *routing* — items are reaching the batch that are not chores —
#: and more bisecting does not answer that. Each red costs one suite run plus about
#: log2(n) probes, so this is also what bounds the pathological night.
MAX_RED_ROUNDS = 2

#: Seconds for one full-suite run over the merged batch. Longer than a card's default
#: because this is the whole suite by definition, not a slice.
BATCH_TEST_TIMEOUT_S = 1800


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
    #: The worker's scenario, carried from its verdict to the card when the batch
    #: lands. Only the worker that built the thing knows which door it is behind.
    how_to_test: str = ""

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


def cost_note(turns: int, wall_s: float) -> str:
    """What this chore cost, as one phrase, or `""` when there is nothing notable.

    **Recorded, never a verdict.** This used to be `effort_exceeded`, which failed a
    chore that exceeded a turn budget and dropped it from the batch as "not a
    one-prompter". Two things were wrong with that, and the second is the one that
    matters:

    1. **Turns measure the repo, not the request.** A turn is one assistant step that
       used a tool, so the count is dominated by how many files must be opened to be
       sure and how often the suite runs — properties of the codebase. Measured
       2026-08-14: a two-line edit in two files plus one new gate took 48 turns and 3.4M
       tokens of cache reads in a 1,900-test project, and was failed by a budget of 40.
       Maintainer, on being shown it: *"the question is if the work is straightforward
       (=simple) not if it needs X turns or modify X files."* Correct, and the plan's
       claim that "effort does track simplicity" was the wrong half of it.
    2. **A cap read after the work finished protects nothing.** By the time the turn
       count is known the time is already spent. Runaway protection has to interrupt,
       and it already does: `_run_worker` bounds every attempt with a wall-clock timeout
       on the process, which is what stops an item eating the night. A second check
       afterwards could only throw away a finished, gated, tested result — which is
       exactly what it did.

    So there is no effort verdict at all now. The bounce is the whole detector, as
    `03_board.md` and this module's own docstring always said: the worker is the only
    actor that opens the code, so it is the only one that can find out the routing was
    wrong, and it reports that in words rather than in a number.
    """
    parts = []
    if turns:
        parts.append(f"{turns} turns")
    if wall_s:
        parts.append(f"{wall_s / 60:.0f} min")
    return ", ".join(parts)


def eligible(card: board.Card, *, capabilities: frozenset[str] | set[str]) -> str:
    """Why `card` cannot join a batch, or `""` if it can.

    Kept separate from `select` so the reason can be reported per card. A chore that is
    silently absent from a batch is indistinguishable from one that was never written.

    `capabilities` is this machine's, from `runner.host_capabilities()` — required
    rather than defaulted, because the check it feeds is one this function did not have
    until 2026-08-18 and a default of "assume none" or "assume all" would both be wrong
    silently. Two callers now ask this question (the batch itself, and the panel
    deciding what to list under `Chores`), and neither may answer it differently.
    """
    if card.kind != KIND:
        return "not a chore"
    if card.tier == "lead":
        # `card_schema` already refuses this combination; checked again because a
        # hand-edited card reaches the dispatcher before a gate run does.
        return "tier: lead - a chore has nothing to decide"
    if card.fields.get("unattended", "true").strip().lower() != "true":
        return "unattended: false - the batch runs without a human present"
    # The same precondition `runner.select` enforces for a dispatched card, and its
    # absence here was a real hole rather than a tidiness point: `requires:` is what
    # keeps an art or audio card off the laptop that has no ComfyUI stack, and a chore
    # was exempt from it for no reason anybody chose. `ad-sound-for-recharge` is
    # `requires: gpu-box`, and was kept off this box only by *also* carrying
    # `unattended: false` — remove that flag, as the schema now demands, and the laptop
    # would have dispatched it. Declared capabilities, never probed: see `host_config`.
    if card.requires and card.requires not in capabilities:
        return (f"requires: {card.requires} - {socket.gethostname()} does not declare it; "
                f"the batch runs where the card can actually be done")
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
    capabilities = runner.host_capabilities(root)
    for card in board.cards(root, lane):
        if card.kind != KIND:
            continue                      # not a chore at all: not "skipped", just other work
        why = eligible(card, capabilities=capabilities)
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
         "the note implied. This is the routing signal, not a failure - and it is always "
         "something the worker read in the code, never a number about what it cost."),
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


# ---------------------------------------------------------------- phase 1: per chore
#
# Own worktree, own worker, then the gates and only the tests that reach what it
# changed. **Not the suite** — that is phase 2's job, once, over the merged result,
# and it is what makes a narrow pass here safe rather than optimistic. A failure
# drops the item from the batch; it never reaches the branch the others merge onto.


def _guard(allow_paid: bool, what: str) -> usage.Verdict:
    """The money rule, checked immediately before spending on `what`.

    Before *every* dispatch and not once at the top: a batch that starts with
    headroom can lose it four items in, and there is no way to un-start the fifth.
    """
    verdict = usage.check(usage.read(), allow_paid=allow_paid)
    if not verdict.allow:
        print(f"  REFUSED before {what} - {verdict.reason}")
        if verdict.resume_at:
            print(f"    resume at {verdict.resume_at:%Y-%m-%d %H:%M}")
        if verdict.refused_for_money:
            print("    override: re-run with --allow-paid")
    elif not verdict.metered:
        print(f"  (unmetered - {verdict.reason})")
    return verdict


def _outcome_for(card: board.Card) -> Outcome:
    """A pending outcome carrying the card's own labels, read before it is dispatched.

    Read here rather than at the end because the card object is rewritten and moved
    between lanes by then, and the checklist needs the title, the surface and the
    verification route whatever became of the item.
    """
    return Outcome(card_id=card.id, title=card.title, verify=card.verify,
                   surface=card.surface)


def run_one(work: Path, card: board.Card, base: str, model: str, *,
            card_budget: float, test_timeout: int) -> tuple[Outcome, runner.Dispatch]:
    """Dispatch one chore and judge it on the gates plus the tests it can reach.

    Returns the batch outcome *and* the raw dispatch, because the caller needs the
    second for facts about the night rather than about the item — a usage wall rides
    home on it and decides whether the batch goes on at all.

    The four states, and each is a different answer to a different question:

    * `done` — gates green, reachable tests green. A survivor; it merges in phase 2.
    * `bounced` — the routing was wrong, and the worker is the only actor that could
      find that out: it parked with a question, because it opened the code and the note
      turned out to hide a fork. There is no numeric route into this state, deliberately
      — a cost cannot tell you a request was not straightforward.
    * `parked` — it failed its own checks. One attempt, so it is a human's to read.
    * `blocked` — nothing was decided about the item: a wall, a crashed gate harness,
      a dropped connection. The attempt is given back and the batch stops.
    """
    out = _outcome_for(card)
    result = runner.dispatch(work, card, base, model, card_budget, test_timeout,
                             test_selector=suite.touched)

    if result.outcome in ("limited", "blocked", "interrupted"):
        out.state, out.detail = "blocked", result.detail
        print("  " + runner.settle(work, card.id, result))
        return out, result

    telemetry = runner.read_telemetry(runner.run_dir(work, card, card.attempts))
    out.turns = int(telemetry.get("turns", 0))
    out.wall_s = float(telemetry.get("wall_s", 0.0))

    if result.outcome == "parked":
        out.state = "bounced"
        out.detail = result.detail or "the worker parked it; its question is on the card"
        print("  " + runner.settle(work, card.id, result))
        return out, result

    if result.outcome != "review":
        out.state, out.detail = "parked", result.detail
        print("  " + runner.settle(work, card.id, result))
        return out, result

    # Green: gates clean and every test that reaches the diff passing. It merges, and
    # what it cost is recorded beside it rather than being weighed against a cap — see
    # `cost_note` on why the cap that used to sit here could only discard good work.
    # An item that was in truth too big for a batch is caught where the evidence is:
    # the worker's own bounce above, or the batch suite in phase 2.
    out.state, out.detail = "done", result.detail
    if note := cost_note(out.turns, out.wall_s):
        print(f"  {card.id}: green ({note})")
    return out, result


# ------------------------------------------------------------- phase 2: per batch
#
# One branch, the survivors merged onto it in order, then the gates and the **whole**
# suite once. On red the merge commits are already there, one per chore, so
# attribution is a binary search rather than a guess.


def batch_branch(now: dt.datetime) -> str:
    """The batch's branch name. Timestamped so two batches in one day do not collide,
    and outside the per-card namespace so nothing that scans it sees a batch."""
    return f"{BATCH_NAMESPACE}/{now:%Y%m%d-%H%M}"


def _merge_prefix(work: Path, tree: Path, base: str,
                  order: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Reset the batch branch to `base` and merge `order`'s card branches onto it.

    Returns `(merged ids, [(id, why) refused])`. A branch that will not apply is
    dropped from the batch and named — never left half-applied, and never silently
    absent, which is the same rule `select` follows for a card left out.

    Rebuilding from `base` each time rather than un-merging is what makes the bisect
    below simple: every probe is one deterministic replay of a prefix.
    """
    runner._git(tree, "reset", "--hard", base)
    merged: list[str] = []
    refused: list[tuple[str, str]] = []
    for card_id in order:
        ref = f"ai/{card_id}"
        if runner._git(work, "rev-parse", "--verify", ref).returncode != 0:
            refused.append((card_id, f"`{ref}` no longer exists"))
            continue
        applied = runner._git(tree, "merge", *gitmerge.STRATEGY_ARGS, "--no-ff",
                              "-m", f"chore {card_id}", ref)
        if applied.returncode != 0:
            runner._git(tree, "merge", "--abort")
            refused.append((card_id, gitmerge.failure_detail(applied)))
            continue
        merged.append(card_id)
    return merged, refused


def _verify_tree(work: Path, tree: Path, out_dir: Path, tag: str,
                 test_timeout: int) -> tuple[bool, str, int]:
    """Gates plus the **whole** suite over the merged result. `(ok, why, tests run)`.

    The full suite by construction, not a selection: this run is the reason phase 1
    is allowed to be narrow, so narrowing it too would remove the backstop and leave
    nothing checking the combination. Judged by the JUnit report, like every other
    pytest this package runs.
    """
    status, why = runner._run_gates(work, tree, out_dir / f"{tag}-gates.txt")
    if status != runner.GATE_PASS:
        return False, why, 0
    junit = out_dir / f"{tag}-junit.xml"
    whole = suite.Selection(suite.ALL, "the merged batch - everything, once")
    try:
        ok, why, _ = runner._run_tests(
            tree, out_dir / f"{tag}-pytest.txt", test_timeout, junit,
            whole.pytest_args(tree / suite.tests_rel(work)))
    except subprocess.TimeoutExpired:
        return False, f"pytest: timed out after {test_timeout}s", 0
    return ok, why, suite.junit_total(junit)


def bisect(work: Path, tree: Path, branch: str, base: str, order: list[str],
           out_dir: Path, test_timeout: int) -> str | None:
    """Which chore reddened the batch — by replaying prefixes, never by guessing.

    The dispatcher holds one merge commit per chore, so this is a binary search over
    the merge order rather than an attribution problem. `next_probe` picks the
    midpoint rather than the last-merged, because a batch is as likely to have broken
    early as late.

    The **last** suspect is never probed: its prefix is the whole batch, which is
    already known red, so testing it would spend a suite run to learn nothing — and
    would not narrow the window, which is how this loop would fail to terminate.

    `None` when the suspects run out, which means the red is not attributable to any
    one item: the caller hands the whole batch over rather than blaming one.
    """
    suspects = list(order)
    while len(suspects) > 1:
        probe = next_probe(suspects[:-1])
        if probe is None:
            return None
        through = order[:order.index(probe) + 1]
        print(f"    bisect: replaying {len(through)} of {len(order)} "
              f"(up to and including {probe})")
        _merge_prefix(work, tree, base, through)
        ok, why, _ = _verify_tree(work, tree, out_dir, f"bisect-{probe}", test_timeout)
        index = suspects.index(probe)
        if ok:
            suspects = suspects[index + 1:]
        else:
            print(f"      red at {probe} - {why[:110]}")
            suspects = suspects[:index + 1]
    return suspects[0] if suspects else None


# ------------------------------------------------- phase 3: one review, one landing


def _review_context(cards: dict[str, board.Card], order: list[str]) -> tuple[str, str]:
    """The `(criteria, intent)` blocks for a review of several cards at once.

    Numbered and attributed, because the reviewer's question for a batch is *"did
    any of these do something needing a decision"* and an unattributed answer is
    not actionable. Nothing else about the review changes: it still sees the diff,
    the criteria and the repo, and nothing about how any of it was made.
    """
    criteria: list[str] = [
        f"This branch carries {len(order)} independent one-prompter changes. Each is "
        f"listed below with its own criteria; judge them together, and say so if two "
        f"of them interact.", ""]
    intent: list[str] = []
    for n, card_id in enumerate(order, 1):
        card = cards[card_id]
        said = (board.section(card.text, "Acceptance")
                or board.section(card.text, "Acceptance criteria")
                or "(none stated on the card)")
        criteria += [f"### {n}. {card.title} (`{card_id}`)", "", said, ""]
        intent += [f"### {n}. {card.title} (`{card_id}`)", "",
                   board.section(card.text, "Intent") or "(none stated on the card)", ""]
    return "\n".join(criteria), "\n".join(intent)


def _land(work: Path, card: board.Card, outcome: Outcome, branch: str,
          remote: str) -> str:
    """Move one landed chore to its final lane and reap its branch.

    Where it goes is the card's own `verify:` declaration, exactly as `settle` reads
    it for a full card: `play` has a surface to exercise and lands in `testing/`
    carrying the batch checklist's row; `review` has none — a gate, an encoding fix,
    inner wiring — so the gates and the suite were its acceptance and it goes
    straight to `done/`. That is what keeps the checklist short enough to be read.
    """
    card.write({"started": None, "finished": runner._now()})
    card.write_section("Summary", outcome.detail or "landed as part of a chore batch")
    if card.verify == "play":
        card.write_section("How to test", outcome.how_to_test or
                           "The worker recorded no scenario - that is itself a defect on a "
                           f"`verify: play` card; the diff is on `{branch}`.")
    lane = "testing" if card.verify == "play" else "done"
    board.move(work, card, lane)

    ref = f"ai/{card.id}"
    runner._delete_remote_branch(work, remote, ref)
    # `-d`, not `-D`: the safe form refuses a branch that is not actually merged,
    # which is exactly the check wanted here. Nothing was rebased, so the branch's
    # own tip really is an ancestor of the integration branch once the batch landed.
    dropped = runner._git(work, "branch", "-d", ref)
    if dropped.returncode != 0:
        print(f"  ! landed {card.id} but could not delete {ref} - "
              f"{(dropped.stderr or dropped.stdout or '').strip()[:120]}")
    return f"{card.id}: -> {lane}/"


def _hand_over(work: Path, card: board.Card, branch: str, why: str) -> str:
    """A survivor whose batch did not land. Its diff is green on its own branch and
    the batch's is not its fault, so it goes to `review/` — "gates green, waiting on
    something else" is exactly what that lane means — with the reason on the card."""
    card.write({"started": None, "finished": runner._now()})
    card.write_section("Merge", why)
    board.move(work, card, "review")
    return f"{card.id}: -> review/ (the batch did not land)"


# --------------------------------------------------------------------- the driver


def _workspace(root: Path, base: str) -> tuple[Path, str]:
    """Where the board and git work happen, or `(root, reason)` on a refusal.

    The same topology `run()` establishes and for the same reason: when the launch
    checkout is on the integration branch the batch works in place, otherwise it
    uses the runner's dedicated checkout and leaves the working copy alone.
    """
    if runner.current_branch(root) == base:
        return root, ""
    try:
        work = runner.ensure_integration_checkout(root, base)
    except RuntimeError as exc:
        return root, str(exc)
    if dirty := runner.dirty_outside_board(work):
        shown = ", ".join(dirty[:4]) + (f" (+{len(dirty) - 4} more)" if len(dirty) > 4 else "")
        return root, (f"the dedicated `{base}` checkout is dirty outside the board: "
                      f"{shown}. It is runner-owned; commit or discard those changes "
                      f"in {work} and re-run.")
    return work, ""


#: How a `Batch` outcome state reads in a run record. The record's vocabulary is the
#: runner's, and mapping onto it rather than inventing a parallel one is what lets the
#: digest and the Command Center report a batch and a night through the same accessors
#: (`run_record.landed`/`failures`/`decisions`).
#:
#: `done` is `review`, not `reviewed`: at the end of phase 1 the item is green on its
#: own branch and the batch review has not happened yet, which is exactly what `review`
#: means for a dispatched card. It becomes `reviewed` once the batch lands.
#:
#: `bounced` and `blocked` map to themselves and are in none of the record's three
#: sets — see the comment on `DECISION_OUTCOMES` for why a bounce must not be a
#: failure. A `_drop` shows up as `parked`, which is what the batch itself calls it;
#: the reason (would not merge / reddened the suite) rides in `detail`.
_RECORD_OUTCOME = {
    "done": "review", "bounced": "bounced", "parked": "parked",
    "blocked": "blocked", "pending": "",
}


def _record_outcomes(record: run_record.Record, batch: Batch,
                     cards: dict[str, board.Card], model: str, *,
                     landed: bool = False) -> None:
    """Re-state the batch's outcomes into the record. Cheap, and safe to call often.

    Called at every point the batch changes rather than once at the end, so a batch
    that is killed — or that dies on a red suite — still leaves a record of what it had
    settled. See `run_record.Record.set_dispatched` for why this replaces rather than
    appends.
    """
    entries = []
    for outcome in batch.outcomes:
        state = outcome.state
        mapped = _RECORD_OUTCOME.get(state, state)
        if landed and state == "done":
            mapped = "reviewed"
        card = cards.get(outcome.card_id)
        entries.append({
            "card": outcome.card_id,
            "title": outcome.title or (card.title if card else ""),
            "worker": card.worker if card else "",
            "model": model, "attempt": 1, "outcome": mapped,
            "detail": outcome.detail, "cost_usd": 0.0,
            "landed": cost_note(outcome.turns, outcome.wall_s),
            "evidence": "", "at": _now_iso(),
        })
    record.set_dispatched(entries)


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def execute(root: Path, *, limit: int = DEFAULT_BATCH, allow_paid: bool = False,
            card_budget: float = 0.0, test_timeout: int = 600,
            batch_test_timeout: int = BATCH_TEST_TIMEOUT_S,
            now: dt.datetime | None = None) -> tuple[int, Batch]:
    """Run one batch end to end. Returns `(exit code, batch)`.

    Exit codes: 0 the batch landed (or there was nothing to do), 1 a refusal before
    anything was dispatched, 3 the money rule stopped it, 4 work was done but the
    batch did not land and is waiting on a human.

    **The batch opens a run record** (`run_record`), exactly as a night does. Until
    2026-08-18 it did not, and the consequence was not a missing statistic: the
    Command Center's Run page reads the newest record to say what ran, so a morning
    that had just finished a 23-minute batch reported "Last run" as a night from a
    fortnight earlier. A run that does not testify is indistinguishable from one that
    never happened, which is the whole argument `run_record`'s docstring makes.
    """
    now = now or dt.datetime.now()
    try:
        base = runner.default_base(root)
    except manifest.ManifestError as exc:
        # A repo that was never set up, or one whose config names no integration
        # branch. Caught rather than allowed to propagate because this is a CLI and
        # an unhandled traceback is the least actionable refusal available.
        print(f"refusing to run - {exc}")
        return 1, Batch()

    check = runner.preflight(root, base, dry_run=False)
    if not check.ok:
        for reason in check.reasons:
            print(f"refusing to run - {reason}")
        return 1, Batch()

    if not runner.acquire_lock(root):
        return 1, Batch()
    record = run_record.null()
    try:
        work, why = _workspace(root, base)
        if why:
            print(f"refusing to run - {why}")
            return 1, Batch()

        # Opened here for the same reason the runner opens its own after the work root
        # is fixed and before `select()`: it must land in the checkout whose digest will
        # read it, and the skip list needs somewhere to go. Every refusal above this
        # line still holds the no-op record, so a batch that never got as far as looking
        # at the board does not leave a record claiming it ran.
        record = run_record.start(work, kind="chores", label=f"batch of up to {limit}",
                                  host=socket.gethostname())

        chosen, skipped = select(work, limit=limit)
        batch = Batch(skipped=list(skipped))
        record.skipped([(entry.card_id, entry.reason) for entry in skipped])
        print(f"chores: {len(chosen)} selected, {len(skipped)} left out")
        for entry in skipped:
            print(f"  - {entry.card_id}: {entry.reason}")
        if not chosen:
            textio.write_text_lf(work / OUT, report(batch, now))
            print(f"  nothing to dispatch; wrote {OUT}")
            record.stop("nothing to dispatch")
            record.finish()
            return 0, batch

        try:
            model = tiers.resolve(work, CHORE_TIER)
        except tiers.TierError as exc:
            print(f"refusing to run - {exc}")
            record.stop(str(exc))
            record.finish()
            return 1, Batch()

        # Headless `-p` has no trust dialog, so an untrusted workspace makes every
        # dispatch fail with no useful message. Same precondition the runner sets.
        runner.ensure_workspace_trusted(root)
        print(f"dispatching {len(chosen)} chore(s) at tier {CHORE_TIER} ({model})")

        # --- phase 1 ---------------------------------------------------------
        cards: dict[str, board.Card] = {}
        stopped = ""
        for index, card in enumerate(chosen):
            if (work / runner.STOP_FILE).is_file():
                stopped = "the kill switch appeared"
            elif not _guard(allow_paid, f"dispatching {card.id}").allow:
                stopped = "the usage window closed"
            if stopped:
                for later in chosen[index:]:
                    out = _outcome_for(later)
                    out.state, out.detail = "blocked", stopped
                    batch.outcomes.append(out)
                break
            cards[card.id] = card
            outcome, result = run_one(work, card, base, model,
                                      card_budget=card_budget, test_timeout=test_timeout)
            outcome.how_to_test = result.how_to_test
            batch.outcomes.append(outcome)
            _record_outcomes(record, batch, cards, model)
            if outcome.state == "blocked":
                stopped = outcome.detail
                for later in chosen[index + 1:]:
                    skipped_out = _outcome_for(later)
                    skipped_out.state, skipped_out.detail = "blocked", stopped
                    batch.outcomes.append(skipped_out)
                break

        survivors = [o.card_id for o in batch.survivors]
        _record_outcomes(record, batch, cards, model)
        phase1 = (f"phase 1: {len(survivors)} survivor(s), "
                  f"{len(batch.by_state('bounced'))} bounced, "
                  f"{len(batch.by_state('parked'))} parked, "
                  f"{len(batch.by_state('blocked'))} not reached")
        print(phase1)
        record.note(phase1)
        if not survivors:
            textio.write_text_lf(work / OUT, report(batch, now))
            print(f"  nothing survived to merge; wrote {OUT}")
            record.stop("nothing survived phase 1 to merge")
            record.finish(dispatched=len(batch.outcomes))
            return 0, batch

        code = _land_the_batch(work, base, batch, cards, survivors, now,
                               allow_paid=allow_paid, card_budget=card_budget,
                               batch_test_timeout=batch_test_timeout,
                               stopped_early=bool(stopped), record=record,
                               model=model)
        return code, batch
    finally:
        # `complete` separates a batch that reached its own end from one that was
        # killed, so it is set on every path out — including the ones that raise.
        # Already-finished records are not reopened: `finish` is idempotent enough
        # for that, and re-stamping would move the finish time of a batch that ended
        # cleanly minutes earlier.
        if not record.data.get("complete"):
            record.stop("the batch ended without reaching its own end")
            record.finish(dispatched=len(record.data.get("dispatched", [])))
        runner.release_lock(root)


def _land_the_batch(work: Path, base: str, batch: Batch, cards: dict[str, board.Card],
                    survivors: list[str], now: dt.datetime, *, allow_paid: bool,
                    card_budget: float, batch_test_timeout: int,
                    stopped_early: bool, record: run_record.Record,
                    model: str) -> int:
    """Phases 2 and 3: merge the survivors, verify once, review once, land once."""
    branch = batch_branch(now)
    out_dir = work / runner.RUNS / "_chores" / f"{now:%Y%m%d-%H%M}"
    out_dir.mkdir(parents=True, exist_ok=True)
    remote = str(runner.host_setting(work, "publish_remote", "")).strip()

    runner._git(work, "branch", "-f", branch, base)
    tree = runner.worktree_root(work) / f"_batch-{now:%Y%m%d-%H%M}"
    if tree.exists() or runner._worktree_registered(work, tree):
        runner._git(work, "worktree", "remove", "--force", str(tree))
    runner._git(work, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)

    landed = False
    green = False
    why = ""
    total = 0
    order = list(survivors)
    try:
        made = runner._worktree_add(work, str(tree), branch)
        if made.returncode != 0:
            why = (f"could not cut a worktree for `{branch}`: "
                   f"{(made.stderr or made.stdout or '').strip()[:150]}")
            order = []
        for _round in range(MAX_RED_ROUNDS):
            if not order:
                break
            order, refused = _merge_prefix(work, tree, base, order)
            for card_id, reason in refused:
                _drop(work, batch, cards, card_id,
                      f"its branch would not merge onto the batch: {reason}")
            if refused:
                _record_outcomes(record, batch, cards, model)
            if not order:
                why = "no surviving branch would merge onto the batch"
                break
            print(f"phase 2: {len(order)} chore(s) merged onto {branch} - "
                  f"gates and the whole suite, once")
            ok, why, total = _verify_tree(work, tree, out_dir, "batch",
                                          batch_test_timeout)
            if ok:
                green = True
                break
            print(f"  the batch is red - {why[:140]}")
            culprit = bisect(work, tree, branch, base, order, out_dir,
                             batch_test_timeout)
            if culprit is None:
                why = (f"the combined result is red and no single item accounts for it: "
                       f"{why}")
                break
            print(f"  bisect names {culprit}; dropping it and re-verifying")
            _drop(work, batch, cards, culprit,
                  f"reddened the batch suite; found by bisecting the merge order. {why}")
            _record_outcomes(record, batch, cards, model)
            order = [c for c in order if c != culprit]
        else:
            # Out of rounds without a green result. A second red is evidence about
            # *routing* — items are reaching the batch that are not chores — and
            # more bisecting does not answer that. Whatever is still in `order` is
            # green on its own branch and is handed over below rather than dropped.
            why = (f"the batch came back red {MAX_RED_ROUNDS} times; that is a routing "
                   f"finding, not a bisecting one - read the items rather than "
                   f"re-running them. Last: {why}")

        if green:
            print(f"  green: {total} test(s) over the merged batch")
            if stopped_early:
                why = ("the batch is green but phase 1 stopped early, so the window "
                       "cannot be trusted to hold a review; nothing merges unreviewed")
            elif not _guard(allow_paid, f"reviewing {branch}").allow:
                why = ("the batch is green but the money rule stopped the review; "
                       "nothing merges without a review")
            else:
                why = _review(work, base, branch, out_dir, cards, order, card_budget)
                if not why:
                    landed, why = runner.merge_branch(work, branch, base, label=branch)
    finally:
        runner._git(work, "worktree", "remove", "--force", str(tree))
        runner._git(work, "worktree", "prune")

    suite_line = f"{total} test(s), green" if landed else (why or "not run")
    record.note(f"batch branch {branch} - suite: {suite_line}")
    if landed:
        print(f"phase 3: {branch} merged into {base}")
        for card_id in order:
            outcome = next(o for o in batch.outcomes if o.card_id == card_id)
            print("  " + _land(work, cards[card_id], outcome, branch, remote))
        runner._git(work, "branch", "-d", branch)
    else:
        record.stop(why)
        print(f"the batch did not land - {why}")
        for card_id in order:
            print("  " + _hand_over(
                work, cards[card_id], branch,
                f"This chore is green on `ai/{card_id}` and was merged onto the batch "
                f"branch `{branch}`, which did not land: {why}. The batch branch still "
                f"exists; resolve it there, or merge this card's branch by hand."))

    # After `_land`, so a chore that reached `testing/`/`done/` records `reviewed`
    # rather than the `review` it carried while the batch review was still ahead of it.
    _record_outcomes(record, batch, cards, model, landed=landed)
    record.finish(dispatched=len(batch.outcomes))

    textio.write_text_lf(work / OUT, report(batch, now, branch=branch, suite=suite_line))
    board.commit_board(work, f"chores: batch {branch}", extra_paths=(str(OUT),))
    runner.publish(work, remote, base)
    print(f"  wrote {OUT}")
    return 0 if landed else 4


def _drop(work: Path, batch: Batch, cards: dict[str, board.Card], card_id: str,
          detail: str) -> None:
    """Take one chore out of the batch, and settle its card so it leaves `tasks/`.

    The settle is not bookkeeping tidiness — it is the same hole `attempt_limit`
    closes one path over. A dropped chore has already spent its single attempt, so
    the batch will not re-queue it and the night will not dispatch it either: left
    in `tasks/` it would be a card nothing picks up and nothing reports. It goes to
    `failed/` with the reason on it, and its branch survives for whoever reads it.

    The recorded outcome is *mutated* rather than joined by a second entry: an item
    appearing twice on the report is worse than one in the wrong bucket, because
    the counts stop adding up and nothing says which entry is current.
    """
    for outcome in batch.outcomes:
        if outcome.card_id == card_id:
            outcome.state, outcome.detail = "parked", detail
            break
    card = cards.get(card_id)
    if card is not None:
        print("  " + runner.settle(work, card_id, runner.Dispatch("failed", detail)))


def _review(work: Path, base: str, branch: str, out_dir: Path,
            cards: dict[str, board.Card], order: list[str],
            card_budget: float) -> str:
    """One review over the whole batch diff. Returns why it must not merge, or `""`.

    Once, not per item, and that is not only an economy: the reviewer's question for
    a chore is *"did any of these do something needing a decision"*, which is
    answerable across the set, and a reviewer holding the combined diff can see an
    interaction between two items that per-item review structurally cannot.

    A `needs_decision` — or `needs_fix` — about one item stops the **whole** batch.
    That is the price of reviewing a batch as one unit and it is paid deliberately:
    merging past a request for a decision is the one thing no stage here is allowed
    to do, and splitting the batch on the reviewer's say-so would mean guessing which
    items its question was about. `needs_fix` gets the same treatment here as
    `needs_decision`, not the per-card retry `runner.review_stage` gives a single
    dispatched card: a batch diff has no single item to bounce back to `tasks/`
    for another attempt, only the combined result, so there is nothing to retry
    into. The branch survives, so the answer is a merge by hand rather than lost
    work either way.
    """
    try:
        model = tiers.resolve(work, "lead")
    except tiers.TierError as exc:
        return f"the reviewer's tier could not be resolved: {exc}"
    criteria, intent = _review_context(cards, order)
    print(f"phase 3: reviewing {branch} as one diff ({model})")
    verdict, _cost, wall = runner.review_branch(
        work, f"batch-{branch.replace('/', '-')}", out_dir, model, base, branch,
        card_budget, BATCH_TEST_TIMEOUT_S, criteria=criteria, intent=intent)
    called = str(verdict.get("verdict", "")).lower()
    print(f"  {called or '(no verdict)'} - {str(verdict.get('notes', ''))[:100]}")
    if called == "ok":
        return ""
    if called == "needs_decision":
        question = str(verdict.get("question", "")).strip()
        return ("the reviewer flagged something for a decision: "
                + (question or "it stated no question, which is itself worth a look"))
    if called == "needs_fix":
        finding = str(verdict.get("finding", "")).strip()
        return ("the reviewer found a fixable defect: "
                + (finding or "it stated no finding, which is itself worth a look"))
    if wall is not None:
        return "the review hit a usage limit before it reached a verdict"
    return "the review returned no usable verdict"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dispatch and verify the next batch of chores.")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help=f"chores per batch (default {DEFAULT_BATCH})")
    parser.add_argument("--plan", action="store_true",
                        help="list the batch and what was left out; dispatch nothing")
    parser.add_argument("--allow-paid", action="store_true",
                        help="proceed even if a dispatch would draw on paid credits "
                             "(the explicit 'continue nevertheless' decision)")
    parser.add_argument("--card-budget", type=float, default=0.0,
                        help="optional USD cap handed to each worker process")
    parser.add_argument("--test-timeout", type=int, default=600,
                        help="seconds allowed for one chore's own test slice")
    args = parser.parse_args(argv)

    if not args.plan:
        return execute(args.root or repo_root(), limit=args.limit,
                       allow_paid=args.allow_paid, card_budget=args.card_budget,
                       test_timeout=args.test_timeout)[0]

    root = args.root or repo_root()
    chosen, skipped = select(root, limit=args.limit)
    print(f"chores: {len(chosen)} selected, {len(skipped)} left out")
    for card in chosen:
        print(f"  + {card.id}")
    for entry in skipped:
        print(f"  - {entry.card_id}: {entry.reason}")

    batch = Batch(skipped=list(skipped))
    textio.write_text_lf(root / OUT, report(batch, dt.datetime.now()))
    print(f"  wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
