# Doc 09 — The runner (Session G, 2026-07-23)

`nightshift/runner.py`. It rescans the board, dispatches what is dispatchable, runs the gates
over the result, moves the card, commits, and writes the digest. It contains no judgment
(`00_architecture.md` §5, §12) and it holds no state (§5).

Everything below is derived from what the board and the gates already do. §8 records what
the derivation got wrong, because that is the part worth keeping.

## 1. The rule that shapes the whole file

> **Resumable from disk alone.** If the machine dies at 3 AM, the next boot reconstructs
> everything by rescanning `Board/`.

Two consequences that are not obvious until you try to violate them:

- **`attempts` is incremented and committed *before* the worker starts.** The tempting
  order is to run the worker and record the attempt with its result, which reads better
  and is wrong: a machine that dies inside the worker comes back having spent nothing, so
  it retries, dies, and retries — a reboot loop with no bound. Paying the attempt up front
  costs one retry in the rare case where the crash was not the card's fault, and that is
  the cheap side of the trade. Tested directly, in nightshift's own tests/test_runner
<!-- stale-ok: names a test that MOVED to nightshift's suite on 2026-08-08 (tests/test_runner.py), not one that was deleted. deletion_sweep resolves symbols against this repo's source only and cannot see across the repo boundary, so a live reference to framework-side test reads as stale here. -->
  (test_attempts_is_committed_before_the_worker_starts).
- **`started:` with no `finished:` is the crash signature.** Nothing else produces it once
  the lock is ours, because every exit path the runner controls clears `started:`. So
  recovery needs no journal — the card *is* the journal, and the four runner-owned fields
  in `03_board.md` §2 turn out to be exactly sufficient. Nothing was added to the schema.

## 2. Zero LLM calls — stated precisely, because this file starts a Claude process

§12 forbids the **orchestrator** from making decisions with an LLM. It does not forbid the
orchestrator from *starting* one; the worker is the thing being orchestrated. The
distinction that keeps this honest:

> The runner never asks a model whether to retry, which card to take, whether output is
> good, or what a failure means. It asks `pytest`, `nightshift/gates/run.py`, and the filesystem.

Made checkable rather than promised. The CLI is executed in exactly one function,
`_run_worker`, and nightshift's own tests/test_runner asserts by AST that (a) it is the only
function building that argv and (b) `select`, `host_capabilities`
and `_deadline` do not shell out at all. A future edit that slips an LLM call into a
decision path fails a test rather than a code review.

The seam pays a second time: it is what lets the full dispatch cycle — worktree, verdict,
gates, tests, card move, commit, digest — run in the test suite in ~20 seconds without
spending a night to discover that a file move was wrong.

## 3. The cycle

```
preflight ─ kill switch · base branch · dirty tree · CLI present · tier binding
    ↓
lock ───── .ai/runs/.lock, PID. A stale lock (dead PID) is TAKEN OVER, not honoured —
           the 3 AM reboot leaves exactly that, and refusing would need a human
    ↓
recover ── `started:` and no `finished:` ⇒ interrupted; `## Error`, attempt already spent
    ↓
select ─── every card in tasks/, each with a yes/no AND A REASON (§5)
    ↓
dispatch ─ worktree → bump attempts → claude -p → verdict → gates → pytest
    ↓
settle ─── rescan, then review/ | needs-decision/ | retry | failed/
    ↓
record ─── nightshift/run_record.py: every dispatch, skip, stop reason and sweep result,
           flushed as it happens so a run that dies still testifies
    ↓
wrapup ── nightshift/board.py commits the board, then publishes
```

**The record is not optional bookkeeping.** Since 2026-07-30 the digest reports *runs*, and a
run is only visible in what it wrote down — a failed attempt returns its card to `tasks/`, the
lane it came from, so board state cannot see it. A dispatch missing from the record is a
dispatch that never happened as far as the morning is concerned. `run()` opens the record before
`select()` (so skips have somewhere to go) and calls `record.finish()` **before** rendering the
digest, never after: rendering first would publish a report of a night that had not yet admitted
how it ended. Every stop path goes through the local `_stop()` helper so the log and the record
cannot disagree — the first draft of this left `record.stop` off two of nine `break` paths, and
a night that ends for an unrecorded reason reads as a night that just ran out of cards.

### Preflight refuses rather than copes

Five checks, and each exists because the alternative is a wasted night:

| Check | Why refusing beats continuing |
|---|---|
| `.ai/STOP` exists | the kill switch. Gitignored, so it cannot turn every machine off at once; the vault syncs, so it works from the phone |
| base branch is stable and not the integration branch | SESSIONS.md's standing rule is "never commit to `dev`", and an unattended process is exactly who would. **Derived, not listed** — see below |
| working tree dirty outside `Board/` | worktrees are cut from HEAD; unstaged work means HEAD is not what Karel thinks he left. **Warned, not refused, under `--dry-run`** — that command's whole job is "show me the board", including while editing |
| `claude` not found | discovered *after* a dispatch, this has already bumped `attempts` and cut a worktree. `claude` is not on PATH on this box (it is in `%USERPROFILE%\.local\bin`), so this is a live case, not a hypothetical |
| §16's tier block unreadable | see §4 |

#### The forbidden set is derived from a role, not written down (2026-07-23)

The first version hardcoded `FORBIDDEN_BASES = {"dev", "main", "master"}` alongside
`DEFAULT_BASE = "development_team"`, and Karel found the trap by asking what happens when
the quarantine branch is retired: *"later I would like to renew the use of dev again and
close development_team."*

On that day the old code refuses **every** dispatch — `dev` would be simultaneously the
integration branch and a forbidden base, and preflight reports "the runner never builds on
dev". The conflation is that one frozenset was expressing two unrelated facts: `main` and
`master` are *permanently* stable, while `dev` was forbidden only because it did not
currently hold the integration role.

`nightshift/branches.py` is now the single binding of branch name → role — reading the
names from `.ai/manifest.toml`'s `[branches]` since 07_portability.md §8 step 4 — and the
refusal set is computed as **everything stable, minus whichever branch holds the
integration role.**
Retiring `development_team` is a one-line manifest edit — **collected 2026-08-06**, when the role moved to `test` and the edit was, in the code, exactly that one line plus adding the retired name to `forbidden_extra`. What it did *not* cover was prose: four live instruction sites named the old branch and had to be found by grep, which is the hole `branch_role_prose` was built for and only half closes (it gates `CLAUDE.md`; the runner skill and this doc set it does not reach). The two gates that diff against the
integration branch (`memory_freshness`, `deletion_sweep`) read the same module, so they
cannot drift from it — before this they each hardcoded the name, and on the switchover
would have silently fallen through to `main` and reported a diff spanning the entire
AI-team effort. That is the dangerous direction: a gate that fails loudly gets fixed, a
gate that quietly compares against the wrong base gets believed.

`doc_scan`'s `_NOT_A_SYMBOL` deliberately does **not** read from that module — it is a list
of words documents are permitted to say, and a retired branch is still named in history
docs forever. It must stay a superset and only ever grow.

Tested both directions: the invariant (the integration branch is never forbidden) and the
migration itself (setting it to `dev` moves `dev` out of the forbidden set and makes it the
first merge-base candidate, while `main` stays protected).

### Selection reports the noes

`select()` returns every card in `tasks/` with a verdict and a reason, not just the ready
ones. "Nothing was dispatchable" and "nothing was dispatched" are different nights and
must not read the same in a log. The reasons are the seven ways a card is skipped:
schema-invalid, `unattended: false`, `worker: none`, no charter, an unmet `requires:`, at
the attempt limit, or a tier that does not resolve.

**The runner never infers `unattended:`.** SESSIONS.md's §G brief says so and it is the one
line of that brief that is a safety property rather than a design preference: the card's
author declares that every acceptance criterion is machine-checkable, and the runner
checks the declaration, never the work.

### Acceptance is the gates, then the tests, then nothing

§11: gate-failed work is never read by Claude, only counted. So the runner's accept step is
`nightshift/gates/run.py` followed by pytest, both inside the worktree, and a card
reaching `review/` means a machine proved it — not that a worker said so.

The pytest half is not a bare `pytest tests/ -q`: it is `nightshift/suite.py` — the slice the card's
diff can affect, `-n auto --dist loadfile` across cores when pytest-xdist is installed, and a
verdict read out of pytest's own `--junitxml` report rather than off the exit code, where **0
tests collected is a failure** (a selection matching nothing must never read as green). The
same module backs `merge_check` and the pre-merge preflight, so all three run and judge
identically.

Three ordering details that are each a bug if reversed:

- **A parked verdict is honoured before the empty-diff check.** A parked card has nothing
  to commit by definition; checking commits first would file every well-formed park as a
  failure, which is precisely backwards from §13.
- **A worker that committed nothing is a failure even with a green verdict.** Gates pass
  trivially on an empty diff, so "green" would otherwise mean "did nothing" and the card
  would sail into `review/`.
- **A `done` verdict does not survive red gates.** The worker does not mark its own
  homework.

## 4. The tier→model lookup — §16's third landing place

§16 says the tier table is *the only place the tier→model binding may be written down*,
and that no caller ever names a model. Together those rule out the obvious implementation:
a dict in `nightshift/tiers.py` would be a second place the binding lives, and it would go stale
the day Phase 5 rebinds the worker tier — which is the exact failure §16 was written
after, a correct table that nothing read.

So `nightshift/tiers.py` **parses §16**. A fenced ` ```tier-binding ` block was added there
carrying `lead = opus` / `worker = sonnet`; the block is operative, the prose table above
it is the reasoning, and Session I edits the block and nothing else. `resolve()` raises
rather than defaulting — a dispatcher that fell back to a default model would reintroduce
the 2026-07-22 violation in a form nobody could see from the outside.

The dispatch prompt states the resolved tier in words, so it satisfies the same contract
`nightshift/hooks/tier_guard.py` enforces on interactive spawns. That is asserted by running the
hook's own `evaluate()` against the prompt in the test suite, rather than by matching the
hook's regex twice.

Aliases (`opus`, `sonnet`), not pinned model ids: an alias tracks the latest model in its
family and a pinned id rots silently.

## 5. The worker contract

The runner hands the worker a worktree, the card, and one obligation.

**Input** — a prompt containing the resolved tier, the branch, the card's absolute path in
the *main* repo, and the card body inline. The charter comes from `--agent <worker>`, so
`03_board.md`'s `worker:` field resolves to a real file and nothing is duplicated into the
prompt.

**Output** — a verdict file, `.ai/runs/<id>/attempt-N/verdict.json`:

```json
{"outcome": "done" | "parked", "summary": "one or two sentences"}
```

This is a **structured field the runner looks up**, never prose it interprets — the §12
line again. Two properties matter more than the schema:

- **Absent verdict falls through to the gates.** Charters written by Sessions D–F know
  nothing about this file, and a runner that failed cards for not writing it would have
  silently broken every worker on the roster. The default had to be "judge it by the
  gates", which is also the correct default on its own merits.
- **`parked` is a success state** and the prompt says so in those words, with §13's four
  required parts. The single worst overnight outcome is an ambiguity resolved by
  invention; the prompt spends four lines making that explicit because it is the one
  instruction that changes what a worker does when it is stuck at 4 AM.

**The worker does not move its own card.** Lane transitions are the runner's (`03_board.md`
§4). The worker may append to `## Thread` and write `## Question`; the runner reads the
verdict and does the move. Two owners of one transition is how you get a card in two lanes.

## 6. Worktrees

One card, one worktree, one branch `ai/<id>`, cut from `test` (§5).

**Outside the repo**, at `../.project_tigress-worktrees/<id>`. A worktree under the repo would
be walked by `doc_scan`, `card_schema` and the digest, and every card would appear twice.
One `..` removes the whole class.

**The checkout never survives; the branch survives only until it merges.** The branch is
the deliverable while a card sits in `review/` — Karel (or the reviewer stage) reads it,
and the card records it in `branch:`. Twenty stale checkouts beside the repo serve no one,
so `dispatch` removes the worktree on every exit path. Once `rebase_and_merge` lands a
card's rebased commits on `test`, `branch` itself is deleted (runner-hardening
follow-up, 2026-07-28) — nothing reads it after that point, and because the commit that
actually merges is a rebased copy rather than `branch`'s own tip, a kept branch would not
even show up as merged under `git branch --merged`. A branch that fails to merge is left in
place, since a human still needs it to resolve the conflict.

Run output — prompt, transcript, gate log, pytest log — goes to `.ai/runs/<id>/attempt-N/`,
gitignored. `Board/README.md` already said run output belongs there and not on the card;
this is the code that honours it. The durable half of a run is the card's `## Error` and
the branch, and both are tracked.

## 7. Retry, dispatch order, and the ways a night ends

`MAX_ATTEMPTS = 3` — not tuned, and not pretending to be: *one flake, one real retry, then
stop burning the night*.

**There used to be a flat time-based backoff here** (a lookup table, `0, 20, 90` minutes,
indexed by attempts already made) before a card could be re-dispatched. Retired 2026-08-26
(Karel: *"I'm not generally convinced that cooldown is helpful in any scenario"*) and
replaced with an ordering rule instead of a clock: `board.dispatch_order` gained a
`last_outcome` bucket. A card the reviewer just sent back `needs_fix` — a finished branch
plus one small, already-scoped commit — sorts to the *front* of the queue, ahead of even a
card Karel dragged to the top of the column; a card whose most recent attempt plain-`failed`
sorts to the *back*, behind every card that has not just failed, so one broken card gets its
turn only after healthier work has had first pick rather than being frozen out by a timer
that has nothing to do with whether it is ready to retry. Between two cards in the same
bucket, `kanban_order` still decides — this changes *when* a card is due, never which of two
equally-due cards goes first. `settle` writes the field on both outcomes (`Card.last_outcome`
carries the reasoning for why `dispatch` deliberately never clears it — the
`limited`/`blocked`/`interrupted` give-back depends on that).

A night stops for one of eight reasons, all deterministic: `--max-cards`, `--until` /
`--max-minutes`, the session budget (`--sessions`, below), the consecutive-failure circuit
breaker, an optional dollar cap, the kill switch appearing mid-run, nothing left
dispatchable, or the card list running out.

### The stopping condition is session windows, not dollars (2026-07-23)

It used to be dollars: `--budget 25` for the night, `--max-budget-usd 5` per worker, with
the runner summing each worker's reported `total_cost_usd`. Karel: *"I would like to work
'until session limit is reached' or 'until 2 session limits are used'. Not on $
equivalent."*

He is right, and the reason is that **on a subscription plan `total_cost_usd` is not a
number anyone spends** — it is the API-equivalent price of the tokens. A dollar stopping
condition therefore ended the night at a point unrelated to the only limit that exists,
and the unit it was denominated in was one the plan is not sold in.

`--sessions N` replaces it. `1` (the default) works until the usage limit is reached and
stops. `2` sleeps through the first reset and stops at the second wall. Both dollar flags
survive, default `0` meaning *no cap at all* — `--max-budget-usd 0` would be a cap of zero
rather than the absence of one, so "no cap" has to mean "no flag." They remain useful to
anyone running this on API billing, where the figure is real.

What bounds a runaway worker now is the wall-clock timeout on its process, which is what
was actually bounding it before: the dollar cap was never the thing that stopped a stuck
worker, only the thing that stopped a productive night.

### Recognising the wall — and why it had to land in the same change

`limits.py` is a text scan over the CLI's output, gated on a non-zero exit. It classifies a
wall as `SESSION`, `WEEKLY`, `MONTHLY` or `TRANSIENT` (a 429), because the right response to
each differs: spend one of the night's windows and sleep — for `SESSION`, and (since
2026-08-07) for `MONTHLY` too, since the overage a monthly wall caps is not needed once the
session window reopens; stop, since a weekly window does not reopen before morning; or wait a
few minutes without spending a window.

**This is not a nicety attached to `--sessions`; removing the dollar cap without it would
have been destructive.** A wall *is* a non-zero exit, and `dispatch` turned any non-zero
into `Dispatch("failed", …)` with `attempts` already committed — so one wall at 02:00 spent
an attempt on every remaining card, wrote `## Error: worker exited 1` on each, and over a
few nights filed working cards into `failed/`. The $25 cap was, by accident, the only thing
stopping the night before the plan limit could reach that code. So the *protection* had to
be built before the *cap* could be lifted, in one change.

A `limited` outcome is therefore the one exit that **gives the attempt back**: `attempts` is
rewound by one, `started`/`finished` cleared, and a card that had never run comes back
pristine. Charging a card one of its three attempts because the plan ran out while it
happened to be in flight is exactly the mis-attribution the lane model exists to prevent.

Two admissions the mechanism does not hide:

- **It is a phrase list**, so a wall worded in words it does not carry is not recognised,
  and the failure mode is a regression to the behaviour above. Mitigated by archiving every
  dispatch's raw CLI output under `.ai/runs/<id>/attempt-N/` — an unrecognised wall leaves
  the evidence needed to add its phrasing — and by `CONSECUTIVE_FAILURE_STOP = 3`, which
  bounds the damage to three cards rather than the queue. Fixing a missed wall is one line
  in `limits._WALL` plus a case in that module's own test suite — both in the framework
  repo since 07_portability.md §8 step 2 moved `limits` out of `.ai/`.
- **A false positive costs a night.** `stdout` carries the worker's own final message, so a
  card that *implemented* rate limiting would trip a naive scan. This is why detection runs
  only on a non-zero exit: a worker that succeeded did not hit a wall, whatever its prose
  says.

<!-- stale-ok: `bypassPermissions` is a `claude --permission-mode` argument value, not a
     symbol in this codebase; it resolves against the CLI, which is out of the tree. -->
**Host capabilities** answer `requires:`. They live in `.ai/hosts.json`, **committed and
keyed by hostname**.

The first version was a single untracked `host.json`, on the reasoning that `gpu-box` is
true on the desktop and false on the laptop, so a tracked answer would be wrong on one of
them. Karel, 2026-07-23: *"Why is host.json local and not shared?"* The reasoning was right
about the fact and wrong about the fix — keying on hostname makes **one committed file
correct on every machine**, and it turns "configure each clone and remember to" into
"configure a box once, it syncs." The failure mode of the untracked version was the bad
kind: a missing file is indistinguishable from a machine with no hardware, so a forgotten
setup step looks exactly like a quiet, correct night. `.ai/host.json` survives as an
optional untracked override for a one-off answer you do not want to commit.

An unlisted hostname resolves to nothing, which is the safe default and is what makes
§11's "Karel working from a laptop with the box off" a functional mode: a card that
`requires: gpu-box` simply stays in `tasks/`, at **zero token cost**, until it is run
somewhere that has one. Adding a machine is a visible edit, never an accidental capability.

**Declared rather than probed — and the good reason is not the one first written down.**
The original justification here was determinism: a file lookup cannot fail or hang the way
a probe can. Karel, 2026-07-23: *"It's not true, it is python testable, if there is the
graphics model installed. But I like to keep this declarative, so no agent tries to install
it on the notebook."*

He is right that it is detectable, and his reason is the stronger one. A probe answers *is
the stack present?*, and the honest response to "no" from something with the initiative to
fix things is to go and install it — twenty gigabytes of model weights onto a laptop at
3 AM, which no gate would catch because nothing about it is a rule violation. A **declared**
capability answers a different question — *is this machine allowed to do this work?* — and
"no" is then a decision already made rather than a problem to solve. Worth generalising:
**where a capability check exists to bound behaviour rather than to discover a fact, the
declarative form is not the lazy option, it is the correct one.**

The same row holds `permission_mode`. The default is `acceptEdits`, which **cannot run
Bash** and therefore cannot finish a code card — pytest and git are both unavailable, so
the card burns an attempt and produces nothing. A real run needs `bypassPermissions`. What
contains it: the worker runs in a throwaway worktree outside the repo, on its own branch,
and the runner refuses to build on `dev`/`main`/`master`. What it does **not** contain: it
is the whole machine, not a sandbox. `allowed_tools` is the tighter alternative, and it
trades one risk for a subtler one — a tool the card needs but the list omits is denied,
and in `-p` mode there is nobody to ask, so the worker stalls and the attempt is spent for
a reason that looks nothing like the real one.

## 8. What Session G found

### The board had no unattended work — and the flag's definition was why

The runner's first `--dry-run` against the real board: **10 cards in `tasks/`, 0
dispatchable, every one `unattended: false`.** Session G initially reported this as a
finding about intake and deferred it to Session H. **Karel rejected that reading the same
day, and he was right.**

> *"There is a lot of others I would expect to run. They can run and it is OK if the
> results need to be manually verified."* — Karel, 2026-07-23

The fault was in `03_board.md` §2's definition, which read *"every acceptance criterion on
this card is machine-checkable."* That disqualifies anything with a visible surface, which
is most of this project — and §3 has every card split its acceptance into machine-checkable
and judgment halves *precisely because both halves are normal*. Under the old wording the
runner could only ever have run pure-logic refactors.

The conflation: the flag was duplicating the job of the lanes. §11's pipeline is gates →
Claude review (`review/`) → Karel at the keyboard (`testing/`), so **the human checkpoints
are downstream of the runner, not instead of it.** A card that runs overnight is already
seen twice before it reaches `dev`. "The badge is legible at hub scale" and "3 damage feels
right in play" are `testing/`'s job.

Redefined: `unattended: true` claims **a machine can tell a failed attempt from a finished
one** — nothing about who looks at the result. Full wording and the two mechanisms it must
not duplicate (`requires:` for hardware, `needs-decision/` for ambiguity) are in
`03_board.md` §2; the decision procedure is now a section of `.claude/agents/triage.md`,
with `true` as the **default**.

Re-read against the corrected test, three of the four code cards flipped:
`menu-unlock-indicators`, `menu-hub-grid-layout` and `ice-damage` all have a substantial
machine-checkable half (unit-tested predicates, a registry guard test, resize tests, the
adrenaline-pump invariant) and a judgment half that is a post-hoc visual check.
`plan-doc-extraction` stays `false` for an unrelated and honest reason: `worker: none`,
so there is nothing to dispatch it to regardless. Board went 0 → 4 dispatchable.

The general lesson, worth more than the fix: **a flag that gates access to a whole
subsystem should be defined by what the subsystem can do, not by how careful the author
feels.** The old wording was written from caution, and caution with no failure mode
attached is how a system ships fully built and does nothing.

### …and the same mistake was still sitting in §5, one layer down

Karel, immediately after the above: *"With the art, I don't understand. Requirements are
covered, runner should try and see they are not met and leave them in tasks, because of the
requirements — no token costs, right? When we are on the other computer, it should run,
art-review those and send it to test if the reviewer finds them good, or needs-decision if
the reviewer says the pipeline was not able to achieve that."*

Exactly right, and `03_board.md` §5's "art is `unattended: false` **permanently**" was the
*old* definition still in force one level down — a rule that had been correct under the
premise it was written against and was never re-derived when the premise changed. Fixing a
definition does not fix the rules that were derived from it; they have to be walked.

The art charter already terminates without Karel: batch of four, blind hand-off to
`art-reviewer`, at most three rounds, then park with the best candidates and the reviewer's
notes — which the charter itself calls success rather than failure. So the three questions
come apart cleanly: `requires: gpu-box` decides *where it can run* (and on the laptop it
sits in `tasks/` at zero token cost, exactly as he said); `unattended:` decides *whether a
machine can judge the attempt* (yes — `asset_hygiene`, the reviewer, a bounded retry, a
park exit); the lanes decide *who approves the picture* (Karel, forever — in
`needs-decision/`, one lane earlier than everyone else's check, because the first pass of an
art card installs nothing and `testing/` means *there is something in the program to
exercise*; corrected 2026-09-08, `03_board.md` §5, `runner.unadopted_artefacts`).

Board went 4 → **10 of 11 dispatchable**. Only `plan-doc-extraction` remains, for an
unrelated reason: `worker: none`.

Two real runner bugs fell out of taking it seriously, and neither would have been found by
reasoning about the flag alone:

- **An art card commits nothing.** Candidates land in a gitignored `assets/.tmp/` and move
  into `assets/` only once Karel picks one, so the empty-diff check would have failed every
  art card with "the worker committed nothing." Now the check is *neither a commit nor an
  artefact*, which is one rule covering both card types with no special-casing by worker.
- **The candidates lived in the worktree, which the runner destroys.** Four good images
  would have been generated, reviewed, and deleted. `harvest()` copies the gitignored
  output into `.ai/runs/<id>/attempt-N/artefacts/` before cleanup, on every exit path
  including the parked one — where the candidates are most of what makes the question
  answerable.

Worth noting what this repairs: the digest's "look at this" section (Session F) was
designed for art awaiting a human eye, and with art never dispatching it was unreachable
code. A feature built for a flow that the flag forbade is a good smell to have noticed
earlier.

#### The runner owns the generate→review→regenerate loop — reversed, correctly

Karel asked whether the runner orchestrates the rounds. It did not, and this document
argued at some length that it should not: putting the loop in the runner would be
*"art-shaped knowledge in a component whose whole value is being dumb"*, and it would make
`attempts` mean two different things depending on the worker. Karel:

> *"Shouldn't the back and forth be orchestrated by the runner? Run the art agent, when
> done run the reviewer, if OK - move the card, if not, run the art agent with the feedback
> from the reviewer, count number of retries, if fails 3 times, move card... seems pretty
> deterministic...?"*

It is, and both objections were bad. The first **mislabelled the knowledge**: nothing in
that loop is art-specific — it is producer/checker-shaped, and §16 already declares that a
first-class pattern with the standing requirement that *every producer charter names its
checker boundary*. Made declarative as a `checker:` frontmatter field, the runner needs to
know no more about art than it knows about perks. The second was **a naming problem wearing
a design objection's clothes**: `attempts` counts dispatches, `rounds` counts make-then-judge
cycles inside one. Two counters, two names, no ambiguity.

And the move fixes four things this document had, an hour earlier, written down as
unsolved:

| In the producer | In the runner |
|---|---|
| three rounds is prose an LLM may ignore — the pattern that has failed four times here | `while round_no < MAX_ROUNDS` |
| a crash in round 2 loses the whole attempt | each round's prompt, artefacts and verdict are on disk |
| the checker's blindness is a charter instruction the producer could quietly break | the runner **builds** the checker's context; there is no path for the prompt to reach it |
| the nested spawn inherits its parent's model — §16's original bug, out of `tier_guard`'s reach because it names no card path | both tiers go through the dispatcher |

The third row is the one worth dwelling on. §16's argument for the seam is that *an agent
reviewing its own output sees what it intended rather than what it made*, and while the
producer chose what to hand the reviewer, that was enforced by a sentence in a charter.
Now the reviewer receives the artefacts directory, the acceptance criteria lifted verbatim
off the card, and the neighbouring assets — assembled by `run_checker`, which never has the
producer's prompt in scope. The quality property became structural.

**How it runs.** `checker: none` (the default, and every code card) is one round and
behaves exactly as before. Otherwise: producer → harvest → checker → `pass` ends it;
`revise`/`reject` appends the notes and the list of what previous rounds already tried to
the next producer prompt; rounds exhausted **parks** the card with the candidates, the last
verdict and the best-named candidate, because §13 says a well-formed question is a success
state and this is the case it was written for. A producer that parks *itself* — an
ambiguity in the card, as opposed to a failure to draw it — ends the loop immediately, since
no number of re-rolls resolves a question about intent.

**It is general, and audio is the test of that** (Karel, 2026-07-23: *"we will add a
pipeline for generating audio and it should work the same way, if possible"*). It does, and
the claim is checkable rather than aspirational: `art` appears nowhere in the runner's
logic, only in comments explaining why a branch exists. Standing up audio needs **no runner
change at all**, provided it follows the house asset convention —

<!-- stale-ok: the audio charters below do not exist and deliberately will not until an
     audio pipeline does (03_board.md §5 — a charter for a role nobody can dispatch is the
     thing not to write). They are named here as the shape audio WOULD take; a forward
     reference is indistinguishable from a stale one, and this is the forward kind. -->
| Piece | Audio's version | Runner change |
|---|---|---|
| producer charter | `.claude/agents/audio.md` | none |
| checker charter | `.claude/agents/audio-reviewer.md` | none |
| the card | `worker: audio`, `checker: audio-reviewer`, `requires:` if it needs the box | none |
| where candidates land | `project_tigress/assets/.tmp/` — `CLAUDE.md`'s workflow for *all* generated assets | none; already in `HARVEST_DIRS` |
<!-- /stale-ok -->

`[worker].harvest_dirs` is the single place that would need a row, and only if audio generated
somewhere other than the house `.tmp/`. That it points at the convention rather than at
`assets/ui/` is what makes this hold.

What audio still needs is a **pipeline**, and §5's rule stands: a charter for a role nobody
can dispatch is the thing not to write. The seam is ready for it; the generator is not.

**Still untested end to end.** No art card has been through the runner; what is proven is
the loop's control flow, driven through `_run_worker` in the test suite. The first real
exercise belongs on the box with the GPU.

#### Running one card by name — the interactive path

`--card <id>`, which is what "run card XYZ via the runner" resolves to when Karel asks in
chat. Two properties make it usable rather than a footgun.

**Naming a card waives the checks that exist for unsupervised running.** `unattended:
false` means *no machine can tell whether this attempt succeeded*; a person asking for the
card by name is a person volunteering to be the judge, which is precisely the missing
ingredient. Backoff and the attempt limit go the same way — both are policies about
pacing an unattended night. What is **not** waived is anything physical: a missing
`requires:` capability, `worker: none`, a missing charter, a card failing `card_schema`, or
a card that is not in `tasks/`. Wanting it harder does not put a GPU in the laptop.

**An unmet `--card` is an error, never a no-op.** The first implementation filtered inside
the dispatch loop, so a typo produced a clean, quiet, entirely successful run in which
nothing happened — the *fifth* instance in this session of the silent-nothing shape. It now
resolves the name up front and refuses with the reason: unknown id, wrong lane (naming
which), or the specific blocker.

### What `unattended:` is actually for now — and it is nearly vestigial

Having removed hardware (`requires:`), ambiguity (`needs-decision/`), no-worker
(`worker: none`) and "a human approves it" (the lanes) as reasons, one job remains:

> **No machine can tell whether this attempt succeeded** — there is no gate, no checker,
> and no test, so a wrong result is indistinguishable from a right one until a person
> looks.

That is rare. Nothing currently on the board is an instance: `plan-doc-extraction` is
`false` today but for the unrelated `worker: none` reason. So the field is close to
vestigial, and **whether to retire it is a real question for Session H** — with the honest
counter-argument that a cheap explicit declaration is a reasonable place for an author to
say "I know this one cannot be judged", and that the cost of keeping it is one line of
frontmatter.

The `board-parser-convergence` card, written during this session, remains the cleanest
case — its entire acceptance is "these three test files pass with no test edited". (Named
by id rather than by lane path on purpose: a card's lane is the one thing about it
guaranteed to change, so a doc that hardcodes `Board/<lane>/<id>.md` goes stale the moment
the work it describes makes progress.)

### `git add` fails the whole pathspec when one entry matches nothing

<!-- stale-ok: the digest and its generated report were removed in 2026-09 (`remove-obsidian`); this
     is the dated account of a bug they were involved in, kept as written because rewriting
     the names out of it would make the record describe something that never happened -->
`commit_board` staged `Board/` and `Digest.md` together. In any tree without a `Digest.md`
yet — a fresh clone — `git add` failed the entire pathspec, staged nothing, and the commit
became a silent no-op. That means `attempts` never reached disk, which means a crashed card
retries forever: the one bug in this file whose only symptom is a wasted night, in the one
mechanism meant to prevent wasted nights.

<!-- stale-ok: names a test that MOVED to nightshift's suite on 2026-08-08 (tests/test_runner.py), not one that was deleted. deletion_sweep resolves symbols against this repo's source only and cannot see across the repo boundary, so a live reference to framework-side test reads as stale here. -->
Found by test_attempts_is_committed_before_the_worker_starts (nightshift's own suite
since 2026-08-08), which asserted the property
rather than the code path. Worth recording as a pattern: the bug was invisible in the repo
where it was written, because `Digest.md` exists here.

### A third frontmatter parser was added knowingly

<!-- stale-ok: the digest was removed in 2026-09 (`remove-obsidian`); this is the dated record of why
     a third parser was added rather than folded in at the time, and `digest.py` is one of
     the two parsers it is about -->
`nightshift/board.py` is the runner's card model. `digest.py` and reconcile.py each already had
their own regex, and folding them together mid-session — with both covered by tests a
refactor would have to be judged against — is how a working thing breaks quietly. So the
convergence is a card on the board rather than a diff in this session, which is the board
being used for its actual purpose.

`card_schema.py` keeps its own copy permanently and that is not debt: a gate must stay
independently importable with no siblings on the path. The card says so, so nobody
"finishes the job" later.

## 9. Not done, and why

- **No scheduler. Karel runs it by hand** (2026-07-23), so the Windows Task Scheduler
  registration script Session G wrote was deleted rather than left sitting unused — a
  script for a mode nobody operates is the "charter for an undispatchable role" mistake in
  another costume, and git holds it if the decision reverses. This costs nothing the design
  relied on: §5's rescan-on-boot, the single-instance lock and the crash recovery were never
  *for* the scheduler, they are for the run dying midway, which is just as possible under a
  manual start. What changes is that "resumable after a 3 AM reboot" becomes "resumable when
  Karel starts it again", which is the same code path.
<!-- stale-ok: `bypassPermissions` is a `claude --permission-mode` argument value, not a
     symbol in this codebase; it resolves against the CLI, which is out of the tree. -->
- **No paid dispatch was run.** The full cycle is exercised end to end in the test suite
  through `_run_worker`; what is unproven is the literal CLI spawn and how a real charter
  behaves inside it. Proving that needs `bypassPermissions` enabled on this machine and a
  real budget, both Karel's calls, and it is the first thing to do on the first night.
<!-- /stale-ok -->
- **`triage` was not amended.** The `unattended:` finding above is a charter change and
  charter changes are judgment work; it is written up as Session H's first input rather
  than applied here, where it would be a same-session fix to a problem this session found —
  the shape §12 warns about.
- **No health-check for a local endpoint** (§11). There is no local runtime until Phase 5,
  and a health-check for an endpoint nobody serves is the "charter for an undispatchable
  role" mistake in another costume. `requires:` is the hook it will attach to.
