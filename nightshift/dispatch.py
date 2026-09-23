"""Candidate selection and the dispatch pipeline: pick a card, spawn its
producer round(s) through the checker loop, repair drift, and hand back a
`Dispatch`.

`select`/`Candidate` rank what the board makes dispatchable right now;
`dispatch`/`_dispatch_attempt` run one attempt end to end -- worktree,
producer rounds (`run_producer`, via `worker`), the checker loop (via
`review.run_checker`), drift repair (`repair_drift`) -- and `crashed_dispatch`
is what a stage that raised instead of returning becomes. The one split
module that imports `review`, for the checker loop a dispatch attempt runs
inline; `review` does not import this one back.
"""
from __future__ import annotations

import json
import socket
import subprocess
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from nightshift import (board, decide, git, limits, runtimes, suite, textio,
                        worker_prompt)
from nightshift import hostconfig, outcome, review, startup, telemetry, verify, worker, worktree

# Selection
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    card: board.Card
    dispatchable: bool
    reason: str


# The size past which a `tasks/` card has stopped being good worker input.
#
# A card *is* the worker's opening message, and the wrapper around it is only
# ~4.6 KB — so card size and prompt size track each other almost exactly, and
# nothing has bounded either since `worker-prompt-off-argv` retired the argv
# limit. The cost of an oversized card is not a crash, it is attention:
# `menu-unlock-indicators` reached 36 KB, and to fix a pip count its worker had
# to read ~25 KB of superseded history first.
#
# 14 KB because the largest legitimately dense cards in `tasks/` are around
# 11-12 KB (`hack-end-summary` 11.7 KB, `monthly-wall-doc-drift` 11.2 KB — a
# full acceptance split plus a findings list). 14 KB clears those with real
# headroom, so the line fires on genuine growth rather than on a normal big
# card, and sits at well under half the 36 KB that actually hurt.
#
# A named constant rather than an inline literal for two reasons: a comfort
# threshold on a growing board is a number that will move, and `oversize_note`
# cites this name in its message so the reader is told what to change.
CARD_COMFORT_BYTES = 14 * 1024


def card_bytes(card: board.Card) -> int:
    """The size of the input a worker is actually handed for this card.

    `card.text` rather than `path.stat().st_size` for two reasons: the text is
    the thing put in front of a worker, and it is the same number on a checkout
    whose working files carry CRLF. One function because there are now two
    callers — the message below and the digest's record entry — and a signal
    whose two surfaces disagreed about the size would be worse than no signal.
    """
    return len(card.text.encode("utf-8"))


def oversize_note(card: board.Card) -> str:
    """One advisory line about a card grown past `CARD_COMFORT_BYTES`, or `""`.

    **Advisory by construction.** `select()` folds this into the candidate's
    `reason`, which is what `run()` logs per card — it never touches
    `dispatchable`. `run()` also uses "returned something" as its *predicate*
    for `record.oversized`, which is how a dispatched oversized card reaches
    `Digest.md` at all: it is dispatchable, so it is not in `record.skipped`,
    so before that field the digest was silent about exactly the case this
    signal exists for. Reusing this function as the test rather than
    re-deriving the condition is deliberate — it keeps the lane rule and the
    comparison in one place for both readers. That is the
    whole design: gates here have exactly one severity (`Violation`), so a
    blocking version of this would turn the board red on Karel using his own
    board, and a gate that does that is a gate that gets muted. Nothing about a
    card's size makes it undispatchable.

    **`tasks/` only, and that is the subtlety worth stating.** A card grows
    after dispatch and it grows *legitimately*: the runner and close-out append
    `## Summary`, `## Thread`, `## Telemetry` and `## Error`, which is how
    `done/` holds 20-21 KB cards that were half that when they were handed to a
    worker. Measuring any other lane would fire on exactly the cards this has
    nothing useful to say about — noise about the record working as designed.
    The lane check lives here rather than at the call site so that a second
    caller cannot get it wrong; `select()` reads `tasks/` and nothing else, so
    today the check is belt as well as braces.

    Measured on `card.text` rather than `path.stat().st_size` because the text
    is the thing actually handed to a worker, and because it is the same number
    on a checkout whose working files carry CRLF.
    """
    if card.lane != "tasks":
        return ""
    size = card_bytes(card)
    if size <= CARD_COMFORT_BYTES:
        return ""
    return (f"{size / 1024:.1f} KB, over the {CARD_COMFORT_BYTES / 1024:.0f} KB "
            f"CARD_COMFORT_BYTES threshold — compact it, or split it into "
            f"separate cards, before it is dispatched again")


def attempt_limit(card: board.Card) -> int:
    """How many dispatches this card gets before it is retired to `failed/`.

    One function rather than a `MAX_ATTEMPTS` literal at each site, because the
    two places that read it — queue selection and retirement — must agree. They
    disagreeing is the failure this exists to prevent: a chore that selection
    refuses to re-queue but retirement leaves in `tasks/` is a card nothing will
    ever pick up and nothing will ever report.

    Counted from `card.retry_from`, not from zero. A play-test rejection is not a
    re-dispatch of the prompt that already went wrong — it adds `## Feedback` the
    earlier attempts never had — so it earns a fresh `attempt_budget`. Without that,
    every rejected chore (one attempt, already spent) fell out of the batch and into
    "Do now" as if it needed a person at the keyboard (Karel, 2026-09-16).
    """
    return card.retry_from + attempt_budget(card)


def attempt_budget(card: board.Card) -> int:
    """Dispatches per budget — one for a chore, three for a full card — regardless
    of how many earlier budgets a rejection already reset."""
    return hostconfig.CHORE_MAX_ATTEMPTS if card.kind == board.KIND_CHORE else hostconfig.MAX_ATTEMPTS


def select(root: Path, capabilities: set[str], bad_schema: dict[str, list[str]],
           forced: str | None = None) -> list[Candidate]:
    """Every card in `tasks/`, each with a yes/no and the reason.

    The reasons are returned rather than filtered away on purpose: "nothing was
    dispatchable" and "nothing was dispatched" look identical in a log that only
    records what ran, and telling them apart is the first question anyone asks
    of a quiet night.

    `forced` is the id of a card a human named explicitly (`--card`). For that
    one card the **advisory** checks are waived and the **physical** ones are
    not, which is the only division that makes sense here:

    * waived — `unattended:`, the attempt limit. Each of these exists to decide
      what may run *with nobody watching*, and someone typing the card's id is
      the watching. `unattended: false` in particular means "a machine cannot
      tell whether this attempt succeeded"; a human asking for it is a human
      volunteering to be the judge.
    * enforced — a broken schema, no worker, no charter, a missing host
      capability. Wanting it harder does not put a GPU in the laptop, and
      dispatching a malformed card produces confident nonsense rather than an
      error.

    A card over `CARD_COMFORT_BYTES` has `oversize_note`'s line appended to
    whichever reason it earned. It is a remark about the card, not a verdict on
    it: an oversized card is dispatchable exactly when it would have been at
    half the size, and the note rides the `reason` field precisely because that
    field is already carried to both readers — the run log and the digest.
    """
    out: list[Candidate] = []
    for card in board.cards(root, "tasks"):
        forced_now = forced is not None and card.id == forced

        def add(dispatchable: bool, reason: str, card: board.Card = card) -> None:
            note = oversize_note(card)
            out.append(Candidate(card, dispatchable, f"{reason}; {note}" if note else reason))

        def no(reason: str) -> None:
            add(False, reason)

        if card.id in bad_schema:
            no(f"card_schema: {len(bad_schema[card.id])} violation(s) — not a finished card")
        elif card.kind == board.KIND_CHORE and not forced_now:
            # Not a refusal — a routing fact. A chore is verified as part of a
            # batch (`nightshift.chores`): a cheap per-item pass, then one full
            # suite run over the merged result. Dispatching it here would give it
            # the full per-card treatment the batch exists to avoid, and three
            # attempts instead of one. Waived for `--card`, which is a human
            # asking for this one item by name.
            no("kind: chore — dispatched as part of a batch (the `chores` queue, "
               "which `--queue both` works first), not one card at a time")
        elif not card.unattended and not forced_now:
            no("unattended: false — declared as needing a human")
        elif card.worker == "none":
            no("worker: none — nothing to dispatch to")
        elif not (root / ".claude" / "agents" / f"{card.worker}.md").is_file():
            no(f"worker: {card.worker} has no charter")
        elif card.requires and card.requires not in capabilities:
            no(f"requires: {card.requires} — {socket.gethostname()} does not declare it "
               f"(.ai/hosts.json)")  # gate-ok(source_reference_liveness): same HOSTS_FILE as above
        elif card.attempts >= attempt_limit(card) and not forced_now:
            no(f"attempts: {card.attempts} — at the limit, belongs in failed/")
        else:
            why = f"tier: {card.tier}, worker: {card.worker}"
            if forced_now and not card.unattended:
                why += " — asked for by name, so `unattended: false` is waived"
            add(True, why)
    return out


def oversized_entries(candidates: list[Candidate]) -> list[tuple[str, int, int]]:
    """The `(card_id, bytes, threshold)` rows `record.oversized` takes.

    **Dispatchable ones only, and that is the whole of the field.** A card that
    was skipped already carries `oversize_note`'s sentence verbatim inside its
    skip reason, so it is reported under `### Skipped` where it belongs;
    repeating it here would double-report it *and* file a card that never ran
    under a heading whose first two words are "Dispatched anyway". The cards
    left are precisely the ones no other section of the digest can mention — the
    reason `Digest.md` was silent about a card being dispatched over and over
    while it grew.

    A named function rather than a comprehension inline in `run()` for the
    reason `oversize_note` is one: the predicate is the thing worth pinning, and
    a test that re-spelled it at the call site would be a mirror of the code
    rather than a check on it.
    """
    return [(c.card.id, card_bytes(c.card), CARD_COMFORT_BYTES)
            for c in candidates if c.dispatchable and oversize_note(c.card)]


def resolve_named(root: Path, card_id: str,
                  candidates: list[Candidate]) -> tuple[list[Candidate], str]:
    """Narrow the run to one named card, or explain why it cannot run.

    A `--card` that matches nothing used to fall through the loop and produce a
    clean, quiet, entirely successful run in which nothing happened — the same
    silent-nothing shape that has now bitten this project four times. Asking for
    a card by name is a specific request, so an unmet one is an error with a
    reason, never a no-op.
    """
    picked = [c for c in candidates if c.card.id == card_id]
    if picked:
        if picked[0].dispatchable:
            return picked, ""
        return [], f"`{card_id}` is in tasks/ but cannot run — {picked[0].reason}"

    elsewhere = board.find(root, card_id)
    if elsewhere is None:
        return [], (f"no card `{card_id}` anywhere on the board "
                    f"(ids are filename stems, e.g. `menu-unlock-indicators`)")
    return [], (f"`{card_id}` is in `{elsewhere.lane}/`, not `tasks/` — only tasks/ is "
                f"dispatched. Move it there first if it is ready.")
# Dispatch
# --------------------------------------------------------------------------

_PROMPT = """\
Execute one card from this project's board, at **tier: {tier}** (resolved to model \
`{model}` — this is the tier the card declares, and running above it is a defect, not a \
favour).

Your working directory is a git worktree on branch `{branch}`, checked out from `{base}`.
Do all code work here. **Commit your work on this branch.** Do not merge, do not push, \
and never check out `dev`, `main` or `{base}`.

This is a single, non-interactive run: when your turn ends, this process ends. There is no \
later turn in which a background task's completion can reach you, so **never start a command \
with `run_in_background` and then end your turn to wait for it** — a backgrounded process is \
killed the instant your turn ends, and any edit you had not committed dies with it. Run any \
command whose result you need — the gate runner and the test suite included — in the \
foreground and wait for it inline (give it a generous timeout).

**Test what you touch; verify once.** While you iterate, run only the test files your change \
reaches, by name. Before your verdict, run exactly what the runner will judge your branch on, \
once each: `python -m nightshift.gates.run`, then `{slice_cmd}` (your diff's test slice, in \
parallel). Do not run the whole suite, do not run it serially, and do not `git stash` to \
re-run anything against the base: the runner runs the gates and that same slice over your \
branch after you finish, judges by its report, and checks by itself whether a red test was \
already red on the base. A second copy of that run decides nothing.

**Do not orient from memory files.** The card carries the context this work needs. Open a \
memory or history file only to update it, or when the card names it; a start-of-session \
reading list in the project's instructions is for interactive sessions, not for a dispatched \
run.

And **commit as you go**: an \
uncommitted edit is not the work, the commit is, so commit before you run a long check and \
again before you write your verdict. A finished but uncommitted worktree is the one way to \
do all the work and still have the run record that you produced nothing.

The card is at:
  {card_path}
You may append to its `## Thread` and write a `## Question` section. **Do not move the \
card between lanes** — lane transitions are the runner's, and it will move this card \
based on whether the gates pass.
{fold}

When you are finished, write your outcome to:
  {verdict_path}
as JSON, exactly these keys:
  {{"outcome": "done" | "parked", "summary": "<2-4 short lines>", \
"how_to_test": "<a scenario, or "" >"}}
`summary` is written verbatim onto the card as its `## Summary` section — the first thing \
the maintainer reads at `testing/`, before the verbose `## Thread` prose if you wrote one. \
Make it concrete: what changed, what you tested, gate/test status — not "implemented the \
card".
`how_to_test` is written onto the card as `## How to test`, and it is the **only** thing \
telling the maintainer what to do with what you built. Write it as a scenario in their \
terms — open the game, go here, do this, expect that — never as a description of the diff. \
Name the door: which menu, which key, which enemy, what the screen should show. If the card \
declares `verify: review` (no surface they can exercise), leave it empty — nobody has to \
invent a scenario for a gate or a refactor, and inventing one is worse than none.
Use `"parked"` when the card cannot be finished without an answer from the maintainer. \
**Parking is a success state, not a failure**, and it requires a `## Question` section on \
the card carrying what you attempted, what is ambiguous, the candidate answers and what \
each would imply — a question they can answer in fifteen seconds without opening the repo. \
Guessing at an ambiguity is the worst possible overnight outcome; parking is better than \
inventing.

{question_format}

The runner will run `nightshift.gates.run` and the test slice your change touches over your \
branch (in parallel, judged by its JUnit report). Do not \
weaken a gate or a test to make it pass.

{doc_truth}

{tool_economy}

--- the card ---
{card_body}
"""

# A limit-interrupted attempt is continued, not restarted (runner-worker-handover
# Decision 1). Three shapes, tried in order of how much context they keep:
#
#   _RESUME_PROMPT   path 1 — `claude --resume <session_id>`. The session still
#                    carries the whole conversation, so the message is a nudge,
#                    not the card. A `--resume` that errors falls through to
#                    path 2 automatically, inside the same attempt.
#   _REENTER_NOTE    path 2 — a fresh session in the *kept* worktree. The session
#                    is gone but the uncommitted diff is not, and that diff is
#                    where the last attempt ended.
#   _WIP_NOTE        path 3 — the worktree was lost too; the work survives only as
#                    a `wip:` commit on the branch, now checked out for you.
_RESUME_PROMPT = """\
Continue the card you were working on before this session was interrupted by a usage limit. \
Your worktree, your branch and your prior reasoning are intact — pick up exactly where you \
left off. When finished, write the same verdict JSON to:
  {verdict_path}
as before: {{"outcome": "done" | "parked", "summary": "<2-4 short lines>", \
"how_to_test": "<a scenario in the maintainer's terms, or "" on a `verify: review` card>"}}. The runner \
will run `nightshift.gates.run` and the test slice your change touches over your branch; do not \
weaken either, and do not run the whole suite yourself.
"""

_REENTER_NOTE = """\

--- continuing an interrupted attempt ---
A previous attempt at this card was interrupted by a usage limit before it finished. Its \
session could not be resumed, but **its uncommitted work is still in this worktree** — run \
`git status` and `git diff` to see exactly where it left off, then continue from there \
rather than starting over. Its work may be incomplete or mid-edit; read it before building on it.
"""

# FROM_BRANCH's note. Deliberately *not* `_WIP_NOTE`, which was the obvious reuse
# and is wrong in the one way that matters: it tells the worker its branch tip is a
# `wip:` commit to be replaced (`git reset --soft HEAD~1`), and here the commits are
# ordinary, finished ones. A worker following that advice would soft-reset real work
# it did not write and could not see the reason for.
_FROM_BRANCH_NOTE = """\

--- continuing a card whose earlier attempt left committed work ---
A previous attempt at this card ran out of its usage window. Its worktree and session are \
gone, but **the commits it made are real and are already checked out on your branch** — \
they are ordinary commits, not a `wip:` placeholder, and nothing needs undoing.

Start by reading what is there: `git log {base}..HEAD` and `git diff {base}...HEAD`. Treat \
it as work you did and have forgotten, not as someone else's draft to redo — it was written \
against this same card. Then finish the card from that point.

**Do not re-implement what is already committed.** If part of it looks wrong, fix that part; \
if it looks done, verify it and move on to what is missing. The gates and the test suite \
have not necessarily been run over this tree, so run them before you write your verdict.\
"""

_WIP_NOTE = """\

--- continuing an interrupted attempt (recovered from a WIP commit) ---
A previous attempt at this card was interrupted and its worktree was reclaimed, but its work \
was preserved as a `wip: {card_id} interrupted` commit, now checked out on your branch. Run \
`git show HEAD` and `git log` to see what it had done, then continue from there. **Before you \
finish, replace that WIP commit with a real commit** (e.g. `git reset --soft HEAD~1` then \
commit properly) so the branch does not carry a `wip:` commit into review.
"""

# The `needs_fix` continuation. Its job is to stop a worker re-doing work that is
# already done and already green — the failure mode this whole path exists to end.
_REVIEW_FIX_NOTE = """\

--- this card is already implemented; apply the review finding and stop ---
**The work is done.** A previous attempt implemented this card in full, its commits are \
checked out on your branch right now, and both the gate runner and the test suite passed \
on exactly this tree. It was then read by a diff reviewer, which found **one concrete, \
verifiable defect** and sent it back. That finding is in the card's `## Review Finding` \
section, and it is your entire task.

Read `git log` and `git diff {base}...HEAD` first, so you are looking at what actually \
exists rather than at what the card describes. Then apply the finding, exactly as stated. \
The reviewer verified it and stated the correct answer; you are not being asked to \
re-derive it, re-litigate it, or re-check the rest of the diff.

**Do not re-implement the card. Do not improve, refactor or extend anything the finding \
does not name.** A change outside it is unreviewed work arriving after review, which is \
the one thing this step cannot absorb — and the acceptance criteria are already met, so \
there is nothing for it to buy. If the finding turns out to be wrong, or cannot be \
applied without a decision, **park the card and say why**: that is a success state, and \
it is far better than a guess or a second implementation.

Expect this to be a small commit. Re-run the gates and the tests that your change can \
reach, write your verdict, and stop.
"""

# A `kind: chore` is a one-prompter: the note already said what to change and what
# the result should be, so there is nothing to elaborate and no fork to resolve.
# Whether that is *true* of any given item is a routing guess made without opening
# the code, and the worker is the only actor that does open it — so the worker is
# the only place the guess can be checked. That is what this note asks for, and
# saying it here rather than in each charter is deliberate: the condition is the
# card's `kind:`, which a charter cannot see, and a per-charter copy would be one
# more thing to remember when a new worker is added.
#
# A bounce is cheap and a wrong batch is not: a parked chore costs one dispatch and
# tells the router it misfired, which is the feedback that makes routing tunable.
# Guessing instead produces a green diff nobody asked for, inside a batch that
# lands as one unit.
_CHORE_NOTE = """\

--- this card is a one-prompter ---
It was routed here as work with **nothing to decide**: the change has one obvious \
home, the intent is the whole approach, and a wrong result would be visible rather \
than subtle. It runs in a batch with other items like it, and the batch lands as one \
unit.

You are the first actor to open the code, so you are the only one who can find out \
that the routing was wrong. **Stop and park — do not proceed — the moment any of \
these turns out to be true**: the change needs a design decision; the thing to change \
is not where the card implies; doing it properly means touching considerably more than \
the card describes; or finishing it would need an answer nobody is awake to give.

Parking here is not a failure and costs almost nothing — it is the signal that this \
was not a one-prompter, and it is worth more than a plausible guess. Write what you \
found into `## Question` as usual. Do **not** widen the change to make it fit, and do \
not do a reduced version of it without saying so.

**Locate, don't read.** Grep for the name, string or symptom the card gives you and read \
only the lines around what you find. A one-prompter has one obvious home, so finding it \
should take a search, not a tour of the codebase. Skip the feature pipeline: no close-out \
skill chain, no in-game check, no memory orientation — run the tests for what you changed, \
the gates once, commit, and write your verdict.

**Judge whether to park by what you still do not know, not by how long you have taken.** \
Being unable to say where the change goes, or which of several places is the right one, is \
the finding — park and say so.
"""

_FEEDBACK = """\

--- round {round_no} of {max_rounds}: what the checker said about your last attempt ---
It saw only the artefacts and the acceptance criteria — never your prompt, your settings \
or your reasoning. That blindness is the point: it reports what you actually made rather \
than what you meant to make. Act on the words.

Verdict: {verdict}
{notes}

Previous rounds already tried: {history}
Change **one thing** this round, and do not repeat a change from that list.
"""
def _answered_note(root: Path, card: board.Card) -> str:
    """`worker_prompt.ANSWERED` for a card carrying a live answer, else `""`.

    Ordered *after* `_CHORE_NOTE` in the prompt on purpose. That note tells a chore
    worker to park the moment "the change needs a design decision" — correct in
    general, and precisely the instruction that sent an already-decided card back to
    `needs-decision/` for the third time (`show-weapon-schematic-stats`, 2026-09-17).
    The decision it names has been made; the block that says so has to come last.

    Silent when the project declares no `[board].decision_attributor` — `decide` has
    no token to recognise an answer by, and a prompt asserting an answer exists when
    nothing can tell is worse than the prompt this replaces.
    """
    answer = decide.latest_answer(card.text, decide.attributor(root))
    return worker_prompt.ANSWERED.format(answer=answer) if answer else ""


def run_producer(root: Path, card: board.Card, tree: Path, out_dir: Path, branch: str,
                 base: str, model: str, card_budget: float, timeout: int,
                 round_no: int = 1, feedback: str = "", resume_session: str = "",
                 continue_note: str = "", agent: str = "", effort: str = "",
                 slice_cmd: str = suite.SLICE_COMMAND,
                 runtime: str = runtimes.CLOUD,
                 local: runtimes.LocalModel | None = None,
                 ) -> tuple[dict, float, int, limits.Wall | None]:
    """One producer round. Its verdict, its cost, its exit code, and the usage
    limit it hit if it hit one.

    `agent` overrides the card's `worker:` charter and `effort` sets the CLI's effort
    level — both empty by default, which is the card's own worker at the CLI's default.
    The chore batch passes both (`chores.CHORE_AGENT`, `chores.CHORE_EFFORT`).
    `slice_cmd` is the command the prompt tells the worker to verify with: the one
    that computes the same slice this dispatch's `test_selector` will judge it on.

    `resume_session` and `continue_note` continue a limit-interrupted attempt
    (runner-worker-handover). With `resume_session` set, the CLI is invoked
    `--resume <id>` on a nudge rather than the full card, warm-continuing the
    previous session. A `--resume` that errors — the session expired, say — is
    **not** allowed to burn the attempt: it falls through here, inside the same
    call, to a fresh session that re-enters the kept worktree and reads its
    uncommitted diff. `continue_note` (used without `resume_session`, or by that
    fallback) is a `## Progress`-style addendum telling a fresh worker that the
    worktree already holds interrupted work.

    `runtime` is `04_local_runtime.md` §4's second axis, orthogonal to `tier`, and
    it is resolved by the caller rather than here — `dispatch` asks
    `runtimes.resolve` once and hands the answer down, so one dispatch cannot
    change its mind between rounds. `runtimes.CLOUD` is the ground state and the
    default, which is why every existing caller needed no change: a machine that
    declares no local model, a charter §7 has not admitted, an unreachable
    endpoint and a `--no-local` run all arrive here as `CLOUD` and take exactly
    the path they took before this parameter existed.

    Under `LOCAL` the *only* things that differ are the argv and how the run's
    facts are read back off its stream. The prompt, the worktree, the verdict
    path, the gates, the test slice and the handover are identical — which is the
    point, and the reason the projected charters must carry the same verdict
    contract the prompt states (§9c Result 3: the one measured failure here was a
    charter and a prompt specifying two different schemas, and the model
    correctly followed the charter it was told to follow).
    """
    verdict_path = out_dir / f"verdict-{round_no}.json"
    on_local = runtime == runtimes.LOCAL and local is not None
    binary = runtimes.binary() if on_local else startup.claude_binary()
    assert binary  # preflight checked this; `resolve` checked the local one

    def _cold_prompt() -> str:
        return _PROMPT.format(
            tier=card.tier, model=model, branch=branch, base=base,
            card_path=(board.board_dir(root) / "tasks" / card.path.name).resolve().as_posix(),
            verdict_path=verdict_path.resolve().as_posix(),
            tool_economy=worker_prompt.TOOL_ECONOMY,
            question_format=worker_prompt.QUESTION_FORMAT,
            doc_truth=worker_prompt.DOC_TRUTH.format(base=base),
            fold=review._fold_instruction(root, card),
            slice_cmd=slice_cmd,
            card_body=card.text,
        ) + (_CHORE_NOTE if card.kind == board.KIND_CHORE else "") \
          + _answered_note(root, card) + continue_note + feedback

    def _argv(session: str) -> list[str]:
        """Flags only — the prompt reaches the child on stdin (`_run_worker`)."""
        if on_local:
            # Nothing from the cloud branch below carries over: OpenCode has no
            # `--permission-mode` (its config owns permissions), no
            # `--allowed-tools`, no `--add-dir` and no `--effort`. Building a
            # shared list and subtracting would be the more compact spelling and
            # the wrong one — every flag here is a different tool's vocabulary,
            # and the two lists drifting apart is the thing to want, not the
            # thing to prevent.
            return runtimes.worker_argv(local, agent or card.worker, session, cwd=tree)
        out = [
            binary, "-p",
            "--agent", agent or card.worker,
            "--model", model,
            *(["--effort", effort] if effort else []),
            *worker._STREAM_ARGV,
            *(["--resume", session] if session else []),
            *worker._budget_argv(card_budget),
            *worker._STRICT_MCP_ARGV,
            "--permission-mode", str(hostconfig.host_setting(root, "permission_mode", "acceptEdits")),
            "--add-dir", str((board.board_dir(root)).resolve()),
            "--add-dir", str(out_dir.resolve()),
        ]
        # An optional allowlist, for running the worker below `bypassPermissions`.
        # Worth knowing before reaching for it: a tool the card turns out to need
        # and the list does not carry is *denied*, and in `-p` mode there is
        # nobody to ask — so the worker stalls, the attempt is spent, and the card
        # fails for a reason that looks nothing like the real one. A tight list
        # trades one risk for a subtler one; that is why it is opt-in.
        if allowed := hostconfig.host_setting(root, "allowed_tools", None):
            out += ["--allowed-tools", *allowed] if isinstance(allowed, list) else \
                   ["--allowed-tools", str(allowed)]
        return out

    def _once(prompt: str, session: str) -> tuple[dict, float, int, limits.Wall | None]:
        textio.write_text_lf(out_dir / f"prompt-{round_no}.md", prompt)
        proc = worker._run_worker(_argv(session), tree, timeout,
                           out_dir / "stream.jsonl", env=worker._worker_env(root, tree, out_dir),
                           prompt=prompt)
        # `worker-N.json` holds exactly the terminal result event, not the raw
        # multi-line stream — `read_telemetry`/`_session_id` both `json.loads`
        # this file as one object, which a raw JSONL tee would break. Falls
        # back to the raw stdout only when nothing on it parsed at all, so a
        # garbled run still leaves *something* to read on disk.
        # Two runtimes, two wire formats, one `worker-N.json` shape on disk.
        # `stream_facts` normalises OpenCode's `sessionID`/`step_finish` events
        # onto the keys `_session_id` and the cost accounting already read, so
        # warm resume and the run record work on a local attempt without either
        # of them learning a second vocabulary.
        result = runtimes.stream_facts(proc.stdout) if on_local \
            else telemetry._terminal_result(proc.stdout)
        textio.write_text_lf(
            out_dir / f"worker-{round_no}.json",
            json.dumps(result) if result else proc.stdout)
        if proc.stderr:
            textio.write_text_lf(out_dir / f"worker-{round_no}.stderr.txt", proc.stderr)
        spent = 0.0
        try:
            spent = float(result.get("total_cost_usd", 0.0))
        except (ValueError, AttributeError, TypeError):
            pass
        verdict = telemetry._read_verdict(verdict_path)
        wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
        if wall is None and not verdict:
            # The compaction reading, and it is asked here because this is the
            # only place that holds both the stream and the verdict. §5: one
            # compaction on a card that is nearly done is a good trade; a card
            # that compacts twice and *still* has nothing to show is a worker
            # about to lose its work to a third. `context_pressure` cannot be
            # folded into `detect` because a compacted session exits zero, and
            # `detect`'s refusal to read a clean run as a wall is what makes its
            # phrase list safe at all.
            wall = limits.context_pressure(proc.stdout)
        return (verdict, spent, proc.returncode, wall)

    if resume_session:
        prompt = _RESUME_PROMPT.format(verdict_path=verdict_path.resolve().as_posix())
    else:
        prompt = _cold_prompt()

    cost = 0.0
    try:
        verdict, spent, code, wall = _once(prompt, resume_session)
        cost += spent
        # A `--resume` that errored (and did not merely hit the wall again) burns
        # nothing: fall through to a fresh session in the same kept worktree,
        # same attempt. The uncommitted diff is the handover (path 2).
        if resume_session and wall is None and code != 0:
            hostconfig._log(f"    --resume failed (exit {code}) — re-entering the kept worktree "
                 f"with a fresh session, same attempt")
            verdict, spent, code, wall = _once(_cold_prompt() + _REENTER_NOTE, "")
            cost += spent
        return verdict, cost, code, wall
    except subprocess.TimeoutExpired:
        # A timeout is the card's problem, not the plan's: the process was
        # running and producing tokens for the whole hour. Never a wall.
        return {}, cost, 124, None


def _limit_reached(root: Path, card: board.Card, tree: Path, branch: str,
                   out_dir: Path, round_no: int, wall: limits.Wall, cost: float,
                   detail: str) -> outcome.Dispatch:
    """A wall landed. Decide what of this attempt to preserve (runner-worker-handover).

    Three shapes come out of here, all of them `limited`:

    * **Empty first call** — nothing was done and there is no prior handover. This
      is the pre-existing behaviour: drop the worktree (settle drops the branch
      too), give the attempt back, preserve nothing. `kept=False` says so.
    * **Warm interruption** — there is uncommitted work, or a handover already
      exists. Keep the worktree, record the session id and the working-tree hash,
      and hand the attempt back so the next dispatch re-enters. `kept=True`.
    * **Stuck** — a warm interruption whose working tree has not moved for
      `NO_PROGRESS_STOP` consecutive resumes. The give-back has become a loop, so
      the attempt is spent and settle files the card to `failed/`. `stuck=True`.

    The worktree is **not** dropped on the warm/stuck paths here — settle owns
    that, because only it knows the card's final disposition.
    """
    prior = worktree.read_handover(root, card.id)
    had_prior = bool(prior.session_id or prior.diff_hash)
    dirty = worktree._worktree_dirty(root, tree)

    if not dirty and not had_prior:
        worktree.drop_worktree(root, tree)
        return outcome.Dispatch("limited", detail, cost, round_no, wall, kept=False)

    session = telemetry._session_id(out_dir, round_no) or prior.session_id
    state_hash = worktree._worktree_state_hash(tree)
    if not had_prior:
        # First interruption — Step 1: nothing to compare against, so it always
        # counts as progress and the attempt is given back unconditionally.
        progressed, no_progress = True, 0
    else:
        progressed = state_hash != prior.diff_hash
        no_progress = 0 if progressed else prior.no_progress + 1
    stuck = (not progressed) and (no_progress >= hostconfig.NO_PROGRESS_STOP)

    # `replace(prior, ...)`, not a fresh `Handover`: the three review fields are
    # not about this interruption and must survive it. Constructing a new one from
    # the three interruption fields dropped `reviewed_sha` on the floor, so a fix
    # attempt that happened to be walled lost the next review's incremental base
    # for the same reason the replay did (`review-anchor-survives-the-replay`). The
    # sha stays a valid ancestor across a wall — the worktree is kept, so whatever
    # partial fix it holds is uncommitted or a `wip:` commit on top — and the
    # incremental diff showing that partial fix is exactly what the re-review wants.
    worktree.write_handover(root, card.id, replace(
        prior, session_id=session, diff_hash=state_hash, no_progress=no_progress))
    if not progressed:
        hostconfig._log(f"    {card.id} resumed but the working tree did not move "
             f"({no_progress}/{hostconfig.NO_PROGRESS_STOP} — "
             + ("filing as stuck" if stuck else "attempt spent") + ")")
    message = detail if progressed or stuck else \
        f"{detail} (no working-tree progress since the last attempt)"
    return outcome.Dispatch("limited", message, cost, round_no, wall,
                    kept=True, progressed=progressed, stuck=stuck)
# Drift repair (drift-should-not-end-the-night)
# --------------------------------------------------------------------------
#
# Repo drift used to end the night. Twice it ended one over something no human
# needed to see: 2026-09-03 stopped with 23 minutes of usage window left because
# a resumed branch ran August's gate suite over August's docs, and 2026-09-04
# stopped because the orientation set was 506 bytes over its budget. Both times
# the queue behind it was healthy and every remaining card went undispatched.
#
# So the runner now tries to fix it before it gives up. The repair runs **inside
# the card's own live worktree, on the card's own branch** (Karel, 2026-09-04:
# *"Repair and finish the card — the merge will be done with it"*), which is what
# makes this cheap: no synthetic card, no second review cycle, no separate merge.
# The repair commit rides along in the diff the card was already going to land,
# and if the card merges, the drift is fixed on `base` for everyone.
#
# The cost of that choice, stated plainly: a card that never merges takes its
# repair with it, and the next card meets the same drift and repairs it again.
# That is the right trade while drift is rare — paying for a repair twice is
# cheaper than losing a night — but it is the thing to watch if it stops being
# rare.
REPAIR_AGENT = "code-thread"

#: Gates whose repair is prose and whose blast radius is a doc. Not a whitelist —
#: `repair_drift` will attempt any gate that reproduces on `base` (Karel's call,
#: 2026-09-04) — but a repair that stays inside this set is reported as routine,
#: and one that leaves it is called out in the morning log so a code-touching
#: repair is never something you find out about by reading a diff.
PROSE_GATES: frozenset[str] = frozenset({
    "orientation_budget", "orientation_shape", "memory_freshness",
    "doc_reference_liveness", "source_reference_liveness", "line_endings",
    "write_newline", "readme_generated", "coreference_sweep",
})

_REPAIR_PROMPT = """The gates are red on this repo's integration branch, and it is **not** because of the change in this worktree. Your whole job is to make them green again, and nothing else.

You are in a live worktree on branch `{branch}`, which already carries another card's finished work. That work is correct and already verified — **do not revise it, do not "improve" it, do not touch its files** except where a drifted gate names them.

The drift, confirmed to reproduce on `{base}` independently of this branch:

{drifted}

The full gate output:

{gates}

What to do:

1. Read the violation. The gate's own message says what it wants — these messages are    written to be actionable and usually name the fix.
2. Make the smallest change that makes the gate honest. **Fix the thing the gate is    pointing at, never the gate.** Raising a budget to fit what is already there, adding    an allowlist entry, deleting a test, or loosening a threshold is the failure this    whole mechanism exists to prevent — if that genuinely is the only correct fix, do    NOT do it: stop and say so in your summary instead.
3. Run `python -m nightshift.gates.run` and confirm it comes back clean.
4. Commit, on this branch, with a message starting `drift:` and a body naming the gate    and why it drifted. One commit, separate from the card's own work.

If you cannot fix it — the fix needs a judgment call only a human should make, or it would take a change far outside the drifted gate's own subject — leave the tree untouched, commit nothing, and say so. Stopping is a correct outcome here and costs the card nothing; a wrong "fix" committed onto someone else's branch costs a lot.

Write a one-paragraph summary of what you changed (or why you stopped) as the last thing in your final message.
"""


def repair_drift(root: Path, tree: Path, card_id: str, branch: str, base: str,
                 drifted: str, gates_why: str, out_dir: Path, model: str,
                 card_budget: float, timeout: int, *, effort: str = ""
                 ) -> tuple[bool, float, str, "limits.Wall | None"]:
    """Try to fix drift in the card's own worktree.

    Returns `(fixed, cost, note, wall)`, where a non-`None` `wall` means the repair
    ran out of window rather than out of ideas — the caller gives the attempt back
    as `limited` instead of blaming the drift.

    `fixed` is the *gates'* answer, never the agent's: the gate suite is re-run in
    `tree` afterwards and only a clean result counts. An agent that reports success
    over red gates is simply wrong, and an agent that stops honestly still gets its
    gates re-run — sometimes a sibling card's merge fixed the drift while this one
    was working.

    A repair that fails leaves the branch as it found it *only* if the agent kept
    its side of the bargain. It may not have, so the caller treats a failed repair
    exactly like the old unrepaired drift — attempt given back, card blocked, night
    stopped — and the branch is preserved as a rescue ref either way.
    """
    binary = startup.claude_binary()
    if not binary:
        return False, 0.0, "no CLI on this machine to run a repair with", None

    prompt = _REPAIR_PROMPT.format(branch=branch, base=base, drifted=drifted,
                                   gates=review._gates_block(out_dir) or gates_why)
    textio.write_text_lf(out_dir / "repair-prompt.md", prompt)

    argv = [
        binary, "-p",
        "--agent", REPAIR_AGENT,
        "--model", model,
        *(["--effort", effort] if effort else []),
        *worker._STREAM_ARGV,
        *worker._budget_argv(card_budget),
        *worker._STRICT_MCP_ARGV,
        "--permission-mode", str(hostconfig.host_setting(root, "permission_mode", "acceptEdits")),
        "--add-dir", str(tree.resolve()),
    ]
    cost = 0.0
    wall: limits.Wall | None = None
    before = git.run(root, "rev-parse", branch).stdout.strip()
    try:
        proc = worker._run_worker(argv, tree, timeout, out_dir / "repair-stream.jsonl",
                           prompt=prompt)
        textio.write_text_lf(out_dir / "repair.log", proc.stdout + proc.stderr)
        try:
            cost = float(telemetry._terminal_result(proc.stdout).get("total_cost_usd", 0.0))
        except (ValueError, AttributeError, TypeError):
            pass
        wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return False, cost, f"the repair timed out after {timeout}s", None

    # Bank an uncommitted repair for the same reason `dispatch` banks an
    # uncommitted worker diff: edits made and never committed are work, and
    # dropping the worktree would drop them.
    worktree.commit_wip(root, tree, f"{card_id}-drift-repair")

    status, why = verify._run_gates(root, tree, out_dir / "gates-after-repair.txt")
    after = git.run(root, "rev-parse", branch).stdout.strip()

    # The gates' answer is asked BEFORE the process's exit, which is the ordering
    # `wall-on-review-wrapup-discards-a-verdict` exists to enforce. A repair that
    # made the tree green and *then* walled on its wrap-up call has done the job,
    # and the night should carry on rather than stop over drift that is gone.
    if telemetry.verdict_survives_a_wall(telemetry.REPAIR_STAGE, {"gates_clean": status == verify.GATE_PASS}):
        if after == before:
            # Green with no commit: a sibling merge fixed it underneath us, or the
            # violation was never real. Either way there is nothing to carry.
            return True, cost, "the drift cleared without a repair commit", None
        return True, cost, f"repaired on {branch} ({after[:8]})", None

    # Red gates AND a wall is not a failed repair — it is a repair that never got
    # to finish. Handing that back as `blocked` would blame the drift for a closed
    # window and stop a night that had only run out of window, so the wall goes up
    # to the caller and the card is given back the ordinary way.
    if wall is not None:
        return False, cost, "the repair hit a usage wall before it cleared the gates", wall
    return False, cost, (f"the repair did not clear the gates — {why}"
                         if after != before else
                         "the repair agent changed nothing and the gates are still red"), None


def dispatch(root: Path, card: board.Card, base: str, model: str,
             card_budget: float, test_timeout: int,
             test_selector: Callable[[set[str], Path], suite.Selection]
             = suite.touched, *, worker: str = "", effort: str = "",
             allow_local: bool = True) -> outcome.Dispatch:
    """One attempt, with the effort map stamped onto whatever it returns.

    A wrapper and not part of `_dispatch_attempt` because that function has a
    dozen exit paths and the map belongs on **every** one of them — including
    the failure paths, where what a stage was dispatched with is exactly what a
    post-mortem wants. Threading a keyword through twelve `return Dispatch(...)`
    calls is a change that goes stale the first time a thirteenth is added; a
    single assignment at the boundary cannot.
    """
    efforts = telemetry.stage_efforts(root, card.tier, worker=effort)
    result = _dispatch_attempt(root, card, base, model, card_budget, test_timeout,
                               test_selector, worker=worker, efforts=efforts,
                               allow_local=allow_local)
    result.efforts = efforts
    return result


def _dispatch_attempt(root: Path, card: board.Card, base: str, model: str,
                      card_budget: float, test_timeout: int,
                      test_selector: Callable[[set[str], Path], suite.Selection]
                      = suite.touched, *, worker: str = "",
                      efforts: dict[str, str],
                      allow_local: bool = True) -> outcome.Dispatch:
    """One attempt. Every exit path leaves the card's runner fields consistent.

    `test_selector` is how the gates-green diff picks its pytest slice.

    **It defaults to `suite.touched`, not `suite.select`, since `per-module-
    test-slice` (2026-09-17).** Part 1b of `suite.py` states `touched`'s general
    condition — sound only for a caller with something re-checking the result
    with `select` anyway — and every caller here meets it, not only the chore
    batch that motivated the condition: `rebase_and_merge` unconditionally
    re-verifies every card's rebased result with `suite.select` (never
    `test_selector`, never skippable) before it can reach the integration
    branch (see its call there). Since `touched`'s file set is always a subset
    of what `select`'s matching bucket would run — it only narrows *within* the
    parts `select` would already include, and falls back to `select` outright
    for anything it cannot map with confidence — nothing this narrower first
    pass could miss is also missed at merge time. Narrowing it only changes how
    soon a real regression is caught, not whether it is, which is what makes it
    safe as the default rather than an opt-in a caller has to know to request.
    (`nightshift.preflight`'s pytest step is *not* the backstop this rests on —
    its default is `suite.select` too, same as `rebase_and_merge`, and only
    `--full-tests` forces literally everything; the unconditional `select`
    re-check at merge time is the actual guarantee.) A caller that wants the
    broader first pass regardless may still pass `suite.select` explicitly. A
    seam rather than a boolean because the two are both selections and the
    choice is which one, not whether.

    A card naming a `checker:` runs a **producer→checker loop inside this one
    attempt** (`00_architecture.md` §16's second seam), bounded by `MAX_ROUNDS`.
    The loop lives here rather than inside the producer for four reasons, and the
    first is the one that decided it: every step of it is a file-state lookup or
    an integer comparison, which is exactly the work §5 says belongs in the
    runner. The others — the bound becomes a `range()` instead of a sentence in
    a charter; a machine that dies in round 2 resumes rather than losing the
    attempt; the checker's blindness stops being a rule the producer could break
    and becomes a fact about how its context is built; and both tiers go through
    the dispatcher instead of a nested spawn silently inheriting a model.
    """
    attempt = card.attempts + 1
    out_dir = telemetry.run_dir(root, card, attempt)
    # Resolved by `dispatch` before anything spawns and handed down, so the
    # producer, the checker it may loop with and the drift repair all run at the
    # effort the record will report — one map, one resolution (`stage_efforts`).
    tier_effort = efforts["worker"]

    try:
        tree, branch, mode = worktree.prepare_worktree(root, card, base)
    except RuntimeError as exc:
        return outcome.Dispatch("failed", str(exc))

    # Committed BEFORE the worker starts. A machine that dies mid-dispatch must
    # come back with `attempts` already spent, or a reboot loop retries forever.
    # `last_outcome` is deliberately left alone here — see its docstring and the
    # `limited`/`blocked`/`interrupted` give-back below, which restores a card to
    # exactly how it looked before this attempt and depends on nothing having
    # touched the field in between.
    card.write({"attempts": str(attempt), "branch": branch,
                "started": hostconfig._now(), "finished": None})
    board.commit_board(root, f"board: {card.id} attempt {attempt} ({model})")

    # The tip of the integration branch right before the worker runs — the sha the
    # worktree fence's backstop restores to if a worker commits to `base` from the
    # wrong checkout (`assert_integration_unmoved`). Captured *after* the board
    # commit above, which is the last legitimate move of `base` before dispatch.
    base_tip = git.run(root, "rev-parse", base).stdout.strip()

    # Which runtime this attempt executes on (`04_local_runtime.md` §4). Resolved
    # **once per attempt, here**, rather than per round: a probe that succeeds for
    # round 1 and fails for round 2 would move a card's checker onto a different
    # model mid-attempt, and "which runtime ran this" would stop being a fact the
    # run record can state.
    #
    # Resolved against the *worktree* and not `root`, because the worktree is what
    # OpenCode will actually run in and therefore what its agent discovery will
    # actually see. A charter that exists on `test` but not on this card's branch
    # is not dispatchable for this card, and the listing is the only thing that
    # knows (§9c Result 4: a wrong `--agent` name does not error, it silently runs
    # the default agent).
    #
    # Every failure below lands on cloud, which is the ground state and the
    # behaviour this line replaces.
    runtime, local = runtimes.prepare(
        root, worker or card.worker, tree, enabled=allow_local, log=hostconfig._log)

    # How a limit-interrupted card continues (runner-worker-handover). Only the
    # first producer round of the attempt resumes/re-enters; later checker rounds
    # are feedback-driven fresh prompts as before. REENTER prefers `--resume` when
    # the session survived and falls back to the uncommitted diff otherwise;
    # FROM_WIP hands over a `## Progress` note pointing at the recovered commit.
    handover = worktree.read_handover(root, card.id)
    if mode == worktree.REENTER:
        resume_session = handover.session_id
        continue_note = "" if resume_session else _REENTER_NOTE
        hostconfig._log(f"  {card.id} was interrupted — "
             + ("resuming its session" if resume_session
                else "re-entering its worktree with a fresh session"))
    elif mode == worktree.FROM_WIP:
        resume_session = ""
        continue_note = _WIP_NOTE.format(card_id=card.id)
        hostconfig._log(f"  {card.id} worktree was lost — continuing from its `wip:` commit")
    elif mode == worktree.FROM_BRANCH:
        resume_session = ""
        continue_note = _FROM_BRANCH_NOTE.format(base=base)
        hostconfig._log(f"  {card.id} left no handover but its branch carries work — "
             f"continuing from {branch} rather than starting over")
    elif mode == worktree.FROM_REVIEW:
        resume_session = ""
        continue_note = _REVIEW_FIX_NOTE.format(base=base)
        hostconfig._log(f"  {card.id} is already implemented on {branch} — applying the "
             f"reviewer's finding on top of it, not re-implementing the card")
    else:
        resume_session = ""
        continue_note = ""

    checked = card.checker != "none"
    max_rounds = hostconfig.MAX_ROUNDS if checked else 1
    cost = 0.0
    verdict: dict = {}
    review: dict = {}
    rescued = 0
    feedback = ""
    tried: list[str] = []
    round_no = 0
    # A wall that landed on a stage which had *already* written a terminal
    # verdict (`verdict_survives_a_wall`). The verdict is honoured, so this
    # attempt is no longer `limited` — but the night's window is still closed, so
    # the wall rides home on every `Dispatch` this function returns from here on
    # and the run loop applies its sleep-or-stop after the card has settled.
    honoured_wall: limits.Wall | None = None

    while round_no < max_rounds:
        round_no += 1
        hostconfig._log(f"  dispatching {card.id} → {worker or card.worker} @ {model}"
             f"{'' if runtime == runtimes.CLOUD else ' [local]'} "
             f"(attempt {attempt}, round {round_no}/{max_rounds})")
        hostconfig._status(root, phase="worker", card=card.id, attempt=attempt,
                round=round_no, of_rounds=max_rounds, worker=worker or card.worker,
                model=model, since=hostconfig._now())

        verdict, spent, code, wall = run_producer(
            root, card, tree, out_dir, branch, base, model, card_budget,
            test_timeout * 6, round_no, feedback,
            resume_session=resume_session if round_no == 1 else "",
            continue_note=continue_note if round_no == 1 else "",
            agent=worker, effort=tier_effort,
            slice_cmd=suite.slice_command(touched=test_selector is suite.touched),
            runtime=runtime, local=local)
        cost += spent
        # Backstop the worktree fence before anything else this round: if the
        # worker committed to the shared integration branch from the wrong
        # checkout, undo it now. Protecting `base` is unconditional and separate
        # from the card's own success, which is still judged on `ai/<id>` below.
        if worktree.assert_integration_unmoved(root, base, base_tip):
            hostconfig._log(f"  ! {card.id} committed to `{base}` from outside its worktree — "
                 f"reset `{base}` to {base_tip[:8]}; the card's work is judged on {branch} "
                 f"(worktree fence backstop, nightshift/hooks/worktree_fence.py)")
        # Before the exit code, always: a wall *is* a non-zero exit, and reading
        # it as one is the bug this ordering exists to prevent — the card would
        # be charged an attempt and an `## Error` for the plan running out.
        if wall is not None:
            # And before the wall, the artefact: a producer that had already
            # written `outcome: parked` walled on its wrap-up, not on the work.
            # Its question is the deliverable and it is complete, so it is
            # honoured rather than thrown away with the window
            # (`wall-on-review-wrapup-discards-a-verdict`, answer B). Harvest
            # first — the candidates are most of what makes a park answerable —
            # then `break` to the park path below, which is also what skips the
            # `code != 0` branch a wall would otherwise fall into.
            if telemetry.verdict_survives_a_wall(telemetry.PRODUCER_STAGE, verdict):
                honoured_wall = wall
                hostconfig._log(f"    {card.id} walled on its wrap-up after parking — honouring the "
                     f"question it already wrote; the night's window is still closed")
                rescued = worktree.harvest(root, tree, out_dir)
                break
            return _limit_reached(root, card, tree, branch, out_dir, round_no, wall,
                                  cost, f"usage limit reached — {wall.evidence}")
        if code != 0:
            # Bank any diff the worker managed before it crashed, so a non-zero
            # exit costs the process, not the work (same reasoning as the
            # empty-diff path below — commit_wip is a no-op on a clean tree).
            worktree.commit_wip(root, tree, card.id)
            # An API disconnection that never reached verification is an
            # interruption, not a verdict on the card (failed-attempt-work-is-
            # deleted-not-resumed, the `api_error` slice): give the attempt back
            # and hand it a resume path, the same as a usage wall — just without
            # the wall's sleep-until-reset, since nothing here says the *plan*
            # is exhausted.
            if telemetry._api_error_interruption(out_dir, round_no):
                session = telemetry._session_id(out_dir, round_no)
                state_hash = worktree._worktree_state_hash(tree)
                worktree.drop_worktree(root, tree)
                # `replace`, for the reason `_limit_reached` uses it: the review
                # anchor is not about this interruption and must survive it.
                worktree.write_handover(root, card.id, replace(
                    worktree.read_handover(root, card.id), session_id=session,
                    diff_hash=state_hash, no_progress=0))
                # `kept=False`: the worktree was just dropped, and `kept` means
                # exactly "the checkout was preserved for a warm resume in
                # place" — `settle` appends "worktree kept for warm resume" from
                # it, which on this path would be a flat lie in the one line a
                # 6 AM reader trusts. Nothing is lost by saying so: what carries
                # this resume is the `wip:` commit plus the handover written
                # above, which is the `FROM_WIP` path, not the warm one.
                return outcome.Dispatch(
                    "interrupted",
                    "worker interrupted (api_error) before verification ran — "
                    "resuming from its `wip:` commit next dispatch",
                    cost, round_no, kept=False)
            worktree.drop_worktree(root, tree)
            # `evidence` is why, not just that: the gates and tests paths both fill
            # it and this one used to pass nothing, so a dead worker's `## Error`
            # said "worker exited 1" and pointed at a directory about to be pruned.
            return outcome.Dispatch("failed", f"worker exited {code}", cost, round_no,
                            evidence=telemetry.worker_exit_evidence(out_dir))

        # Rescued before any exit path removes the worktree — including the
        # parked one, where the candidates are most of what makes the question
        # answerable.
        rescued = worktree.harvest(root, tree, out_dir)

        # A producer that parks is stating an ambiguity, which no amount of
        # re-rolling resolves. Stop the loop rather than spending the remaining
        # rounds re-asking a question it already said it cannot answer.
        if verdict.get("outcome") == "parked":
            break
        if not checked:
            break

        hostconfig._log(f"    worker done — {card.checker} is judging round {round_no}")
        hostconfig._status(root, phase="checker", card=card.id, attempt=attempt,
                round=round_no, of_rounds=max_rounds, checker=card.checker,
                model=model, since=hostconfig._now())
        checker_verdict, spent, wall = review.run_checker(root, card, out_dir, round_no, model,
                                          card_budget, test_timeout * 2,
                                          effort=efforts["checker"])
        cost += spent
        if wall is not None:
            # The reported instance of this card, and the reason it exists:
            # 2026-08-09's round-2 art-reviewer wrote `review-2.json` — `pass`,
            # best `menu_card_cyberware_s30.png`, with reasoning — and walled on
            # its final turn, and `review` was fully parsed on the very return
            # path that ignored it. `rounds_left` is what keeps a `revise` with
            # rounds still unspent `limited`: honouring that would file the card
            # as "did not pass in 1 round(s)" and park work that had two rounds
            # it never received.
            if telemetry.verdict_survives_a_wall(telemetry.CHECKER_STAGE, checker_verdict,
                                       rounds_left=max_rounds - round_no):
                honoured_wall = wall
                hostconfig._log(f"    {card.checker} walled on its wrap-up after writing a complete "
                     f"verdict — honouring it; the night's window is still closed")
            else:
                return _limit_reached(
                    root, card, tree, branch, out_dir, round_no, wall,
                    cost, f"usage limit reached while checking — {wall.evidence}")
        called = str(checker_verdict.get("verdict", "")).lower()
        hostconfig._log(f"    {card.checker}: {called or '(no verdict)'} — "
             f"{str(checker_verdict.get('notes', ''))[:80]}")
        if called == "pass":
            break
        # A checker that reported nothing usable cannot drive another round —
        # re-rolling on no information is how the night gets burnt. Treat it as
        # the loop ending, and let the park path below carry it to Karel.
        if not called:
            break
        tried.append(f"round {round_no}: {str(checker_verdict.get('notes', ''))[:60]}")
        feedback = _FEEDBACK.format(
            round_no=round_no + 1, max_rounds=max_rounds, verdict=called,
            notes=str(checker_verdict.get("notes", "")), history="; ".join(tried) or "(none)",
        )

    # `honoured_wall` rides home on every exit path from here down. Which lane the
    # card reaches is settled on its own merits, exactly as it would have been
    # without a wall; what the wall still decides is the *night*, and the run loop
    # reads that off `Dispatch.wall` after the card has settled.
    if verdict.get("outcome") == "parked":
        worktree.drop_worktree(root, tree)
        return outcome.Dispatch("parked", str(verdict.get("summary", ""))[:300], cost, round_no,
                        honoured_wall)

    # The checker never passed inside the round budget. That is a park, not a
    # failure (§13): the candidates and the critique are exactly what Karel needs
    # to decide in fifteen seconds whether to pick one or rethink the concept.
    if checked and str(checker_verdict.get("verdict", "")).lower() != "pass":
        worktree.drop_worktree(root, tree)
        called = str(checker_verdict.get("verdict", "")) or "no verdict"
        best = str(checker_verdict.get("best", "")) or "none named"
        return outcome.Dispatch(
            "parked",
            f"{card.checker} did not pass this in {round_no} round(s) — last verdict "
            f"`{called}`, best candidate `{best}`. {str(checker_verdict.get('notes', ''))[:180]} "
            f"Candidates and every round's critique are in "
            f"`.ai/runs/{card.id}/attempt-{attempt}/`.",
            cost, round_no, honoured_wall,
        )

    # A worker can finish its edits and die before it ever runs `git commit` —
    # most often by kicking off the test suite with run_in_background and ending
    # its turn to "wait for the notification", which in a one-shot headless run
    # kills the task and the turn together, so the commit step never arrives
    # (2026-07-24, orientation-size-budget attempt 2: 11 edits, zero commits, the
    # whole diff dropped with the worktree). An edited-but-uncommitted tree is
    # work, not silence: bank it as a `wip:` commit before deciding the worker
    # produced nothing, exactly as commit_wip already does for the limit path. A
    # complete diff then flows into the gates+tests below and can pass on its own
    # merits; a partial one fails them but is preserved on the branch either way.
    if worktree.commit_wip(root, tree, card.id):
        hostconfig._log(f"    worker left its work uncommitted — banked it as a `wip:` commit "
             f"on {branch} (it never ran git commit itself)")

    # "Produced nothing" means neither a commit nor an artefact. Commits alone
    # would be wrong for art, whose output is deliberately not committed until
    # Karel picks one; artefacts alone would be wrong for code.
    commits = git.run(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip()
    if commits in ("", "0") and rescued == 0:
        worktree.drop_worktree(root, tree)
        return outcome.Dispatch("failed", "the worker produced neither a commit nor an artefact",
                        cost, round_no, honoured_wall)

    hostconfig._log(f"    worker produced {commits} commit(s) — running gates on {branch}")
    hostconfig._status(root, phase="gates", card=card.id, attempt=attempt, branch=branch,
            since=hostconfig._now())
    status, why = verify._run_gates(root, tree, out_dir / "gates.txt")
    # A crashed harness is never the card's fault, so it does not spend an
    # attempt and it does not move the card. It also ends the night: every
    # remaining card would meet the same broken gate and be filed as failed,
    # which is precisely what happened on 2026-07-23 before this existed.
    if status == verify.GATE_CRASH:
        worktree.drop_worktree(root, tree)
        return outcome.Dispatch("blocked", why, cost, round_no, honoured_wall)

    # A gate violation's evidence is the violation lines themselves. `why` already
    # carries the worst four joined by `; ` (`_run_gates`), so the block is that
    # same text split back onto its own lines — readable in a card, and no second
    # read of `gates.txt`, which is the file that does not survive pruning.
    #
    # Also this attempt's own file changes (suite.select, runner-test-selection),
    # computed once here rather than only inside the `ok` branch below: the
    # repo-drift classifier needs the same set for the *violation* case, and it
    # is cheap to compute regardless of which way the gates went.
    changed = set(git.changed(root, f"{base}...{branch}"))
    evidence = ""
    #: Non-empty once a drift repair has landed on this branch: names the gates and
    #: the commit. Carried to the reviewer, which would otherwise see an unexplained
    #: change outside the card's criteria and correctly object to it.
    repaired = ""
    if status == verify.GATE_VIOLATION:
        evidence = "\n".join(f"    {part}" for part in why.split("; ")[:suite.EXCERPT_TESTS])
        # Repo drift, not the card's own doing (failed-attempt-work-is-deleted-
        # not-resumed): every violating path lies outside this attempt's diff,
        # so the tree the gate is unhappy about is not the tree this worker
        # wrote. Give the attempt back exactly like a crashed harness — the two
        # are the same category, one notch less severe — and stop the night so
        # nothing else is judged against the same drifted state. A violation
        # with no usable path, or one that touches a changed file, is never
        # drift: `_is_repo_drift` returns False and this falls through to the
        # ordinary `failed` below.
        #
        # Outside-the-diff is necessary but not sufficient (`menu-art-start-run`,
        # 2026-09-03). It is equally true of a tree that is merely *behind*, and
        # stopping the night on that sends Karel to fix an integration branch
        # that is green. So the hypothesis is confirmed against `base` before it
        # is acted on, exactly as the pytest half below does — and only then is
        # this drift rather than this attempt's own red gates.
        payload = verify._gate_violations_json(tree)
        if verify._is_repo_drift(payload, changed) and                 (drifted := verify._gates_failing_on_base(root, base, payload or [], card.id)):
            # Drift confirmed on `base`. Before this ends the night — which is
            # what it used to do, twice, over a stale doc and 506 bytes — hand it
            # to a repair agent in this same worktree. A repair that works is
            # committed onto the card's own branch and merges with it; the card
            # then carries on through tests and review as if the drift had never
            # been there (drift-should-not-end-the-night).
            gate_names = sorted({str(v.get("gate", "")) for v in (payload or [])} - {""})
            reach = ("" if set(gate_names) <= PROSE_GATES
                     else " — this one is outside the prose gates, so read its commit")
            hostconfig._log(f"    repo drift on {base} ({', '.join(gate_names) or 'gates'}) — "
                 f"attempting a repair on {branch} before giving up{reach}")
            hostconfig._status(root, phase="repair", card=card.id, attempt=attempt,
                    branch=branch, since=hostconfig._now())
            fixed, repair_cost, note, repair_wall = repair_drift(
                root, tree, card.id, branch, base, drifted, why, out_dir, model,
                card_budget, test_timeout, effort=efforts["repair"])
            cost += repair_cost
            if not fixed and repair_wall is not None:
                # Out of window, not out of ideas. `limited` gives the attempt back
                # and lets the night's own wall arithmetic decide whether to wait
                # for the next window or stop — the same treatment a walled producer
                # gets, and the opposite of blaming the drift for the clock.
                hostconfig._log(f"    the repair hit a usage wall — {note}")
                worktree.drop_worktree(root, tree)
                return outcome.Dispatch("limited", f"{why} — {drifted} ({note})", cost,
                                round_no, repair_wall, kept=False)
            if not fixed:
                hostconfig._log(f"    repair failed — {note}")
                worktree.drop_worktree(root, tree)
                return outcome.Dispatch("blocked", f"{why} — {drifted} (repair attempted: {note})",
                                cost, round_no, honoured_wall, repo_drift=True,
                                evidence=evidence)
            hostconfig._log(f"    drift repaired — {note}; re-judging {branch} on the fixed tree")
            repaired = f"{', '.join(gate_names) or 'gates'}: {note}"
            # Everything downstream is judged against the repaired tree, so both
            # the gate verdict and the diff have to be re-read. `changed` in
            # particular now includes the repair's own files, which is what the
            # test slice must see (and what keeps `_is_repo_drift` honest if a
            # second violation surfaces).
            status, why = verify._run_gates(root, tree, out_dir / "gates.txt")
            changed = set(git.changed(root, f"{base}...{branch}"))
            evidence = ""

    ok = status == verify.GATE_PASS
    if ok:
        # `tree`, not `root`, is what the classification resolves against: a
        # changed test file is judged by what it imports, and the version that
        # matters is the one in the worktree pytest is about to run.
        selection = test_selector(changed, tree)
        hostconfig._log(f"    gates clean — {selection.bucket} test slice "
             f"({selection.reason})")
        hostconfig._status(root, phase="pytest", card=card.id, attempt=attempt, branch=branch,
                slice=selection.bucket, since=hostconfig._now())
        try:
            ok, why, evidence = verify._run_tests(tree, out_dir / "pytest.txt", test_timeout,
                                           out_dir / "junit.xml",
                                           selection.pytest_args(tree / suite.tests_rel(root)))
        except subprocess.TimeoutExpired:
            ok, why = False, f"pytest: timed out after {test_timeout}s"
            evidence = ""

    worktree.drop_worktree(root, tree)
    if not ok:
        # Symmetrical with the gate-drift check above: before this card is
        # blamed, ask whether the same tests were already red on the base it
        # forked from. If they were, the diff is not what broke them, and
        # spending an attempt teaches the board something false.
        if drifted := verify._already_failing_on_base(root, base, out_dir / "junit.xml",
                                               card.id, test_timeout):
            return outcome.Dispatch("blocked", f"pytest: {drifted}", cost, round_no,
                            honoured_wall, repo_drift=True, evidence=evidence)
        return outcome.Dispatch("failed", why, cost, round_no, honoured_wall, evidence=evidence)
    made = f"{commits} commit(s) on {branch}" + (f", {rescued} artefact(s)" if rescued else "")
    if checked:
        made += f", {card.checker} passed it in {round_no} round(s)"
    return outcome.Dispatch("review", str(verdict.get("summary", made))[:300], cost, round_no,
                    honoured_wall, repaired=repaired,
                    how_to_test=str(verdict.get("how_to_test", "")).strip()[:1000],
                    unadopted=worktree.unadopted_artefacts(root, rescued, changed))
# When a stage of the pipeline raises instead of returning
# --------------------------------------------------------------------------

# How much of a traceback is quoted into the card. Enough to name the frame that
# raised and the call path into it; not so much that a deep recursion buries the
# rest of the card. Head and tail are both kept because the two ends carry
# different facts — the first lines say the exception happened and where the
# runner entered, the last say what actually raised.
_CRASH_TB_HEAD = 6
_CRASH_TB_TAIL = 24


def _crash_traceback(exc: BaseException) -> list[str]:
    """`exc`'s traceback as bounded lines, elided in the middle if it is long."""
    lines: list[str] = []
    for chunk in traceback.format_exception(type(exc), exc, exc.__traceback__):
        lines += chunk.rstrip("\n").split("\n")
    if len(lines) <= _CRASH_TB_HEAD + _CRASH_TB_TAIL:
        return lines
    elided = len(lines) - _CRASH_TB_HEAD - _CRASH_TB_TAIL
    return (lines[:_CRASH_TB_HEAD]
            + [f"... {elided} line(s) elided ..."]
            + lines[-_CRASH_TB_TAIL:])


def crashed_dispatch(root: Path, card: board.Card, exc: BaseException) -> outcome.Dispatch:
    """One card's pipeline raised. Turn that into *that card's* failure.

    An exception out of `dispatch` used to propagate through `run` to `main` and
    exit 1, abandoning every card still in the queue: on 2026-08-06 a single
    `FileNotFoundError: [WinError 206]` from one oversized card's `Popen` ended
    the night before it had started, and left a worktree and branch behind for
    hand-cleaning. A card the runner cannot dispatch is a failure of that card,
    exactly like a red gate — so this returns the same `Dispatch("failed", …)`
    the gate path returns and lets the existing settle/record/publish path carry
    it. No new outcome value, no new lane; `CONSECUTIVE_FAILURE_STOP` is already
    the net that promotes a *systematic* crash to the night's problem.

    The attempt stays spent. `dispatch` commits `attempts` before the worker
    starts precisely so a machine that dies mid-dispatch cannot retry forever, so
    a crash past that point has spent it whatever this function decides.

    Two things make the failure diagnosable from the card alone, which is the ask
    the raw `WinError 206` failed: `detail` reads as a cause (`dispatch crashed —
    FileNotFoundError: …`) rather than as noise, and `evidence` names the card and
    quotes the traceback, so `## Error` answers *which card* and *roughly why*
    without opening the gitignored `.ai/runs/`.

    Cleanup is what `dispatch`'s own exit paths would have done and did not get
    to: `commit_wip` to bank whatever the worker managed, then `drop_worktree` —
    the same order, and for the same reason, as the `worker exited {code}` path.
    It is guarded in turn: a cleanup that raises must not re-crash the loop this
    function exists to protect.
    """
    detail = f"dispatch crashed — {type(exc).__name__}: {exc}".strip()
    quoted = [f"the runner crashed while dispatching card `{card.id}`; "
              f"no verdict was reached for it", ""] + _crash_traceback(exc)
    # Indented four spaces like every other excerpt, so a `#` anywhere in the
    # traceback cannot match `doc_scan._HEADING`.
    evidence = "\n".join(f"    {line}".rstrip() for line in quoted)

    try:
        tree = worktree.worktree_root(root) / card.id
        if tree.exists():
            worktree.commit_wip(root, tree, card.id)
            worktree.drop_worktree(root, tree)      # also clears the handover
        else:
            worktree.clear_handover(root, card.id)
    except Exception as cleanup_exc:       # noqa: BLE001 — see the docstring
        hostconfig._log(f"  ! could not clean up after {card.id}'s crash: "
             f"{type(cleanup_exc).__name__}: {cleanup_exc}. A worktree may be left "
             f"under {worktree.worktree_root(root)} for hand-removal.")

    return outcome.Dispatch("failed", detail, evidence=evidence)
