---
name: Run The Runner
description: How to invoke nightshift/runner.py from a live session — the exact commands and flags for a dry run, one named card, or a night; how to background it and watch it; what each outcome and refusal means. Invoke whenever {{maintainer}} says "run the runner", "run card X", "dispatch X", "start the night", "what is the runner doing", or asks to stop a run.
---

<!-- Shipped by nightshift as a template, then filled in by nightshift init.
     Section references point at the framework's design notes, which deliberately do
     not ship; every rule they cite is stated in full where it is cited. -->

# Run The Runner

**Concern:** invoke `nightshift/runner.py` correctly, first time, without reading it. Everything a
session needs to type is on this page; the reasoning behind the design is
the framework's design notes and does not need to be read to run it.

The runner is the *only* sanctioned way to implement a board card. It cuts a worktree,
resolves `tier: → model`, dispatches the worker (and its `checker:` loop), runs the gates
and the full test suite, moves the card and commits. **Never implement a card inline in
chat, and never spawn its worker with `Agent` by hand** — either bypasses the branch
isolation, the gate run and the attempt bookkeeping, and that is the path that produced the
2026-07-22 tier violation.

Board editing — adding, refining, answering, moving cards — is `manage-board`, not this.

## The four commands

Run from the repo root. `python`, not `py`.

```bash
python -m nightshift.runner --dry-run            # what would dispatch, and why not. No LLM, no writes, no lock
python -m nightshift.runner --card <id>          # one named card. <id> is the filename stem
python -m nightshift.runner --until 06:30 --stale # a night: work the queue, then sweep stale docs
python -m nightshift.runner --status             # where a run is right now. Safe while one is in flight
```

The night command carries `--stale`: after the card loop, the runner spends **whatever budget
is left** on the Tier-2 staleness sweep (change-ordered, most-churned docs first; findings
become fix-cards in `tasks/`). It runs last and consumes only leftover window, so it never
takes budget from cards. Drop it (or pass `--stale 0`) if a night must stay card-only.

`--dry-run` is the default move when {{maintainer}} is vague. It prints every card in `tasks/` with
`YES`/`no` and the reason, costs nothing, and does not mind a dirty tree.

A night takes cards in **{{maintainer}}'s Kanban column order** (`kanban_order`, written by Base
Board on every drag); cards they never dragged sort last, alphabetically. So "run the top of
the queue" is `--max-cards 1`, and the dry run already prints them in the order they will go.

### Where the runner works (topology, runner-hardening #3)

The runner keeps `{{integration}}` in its **own** sibling checkout
(`../.{{package}}-integration`) so {{maintainer}} can keep coding on their own branch while it runs. It
is created automatically the first time. So:

- **{{maintainer}} codes off `{{integration}}`** (on a `<you>/<x>` branch): the runner uses the
  dedicated checkout, commits the board and merges reviewed cards there, and **never touches
  their working copy**. Their board view lags until they `git merge {{integration}}` (accepted).
- **{{maintainer}} is still on `{{integration}}`**: git forbids a second checkout of it, so the runner
  works **in-place** as before and logs a nudge to switch. Nothing breaks.
- **Card intake is committed-only**: the runner reads the dedicated checkout, so a card must be
  **committed to `{{integration}}`** to be dispatched. Ready + commit the board on
  `{{integration}}`, *then* switch to `<you>/<x>` and run.
- **`--status`, `.ai/STOP`, the lock and the log stay in the launch checkout** — unchanged.
  The kill switch is honoured whether it lands there or in the dedicated checkout.
- Reviewed-ok cards are **rebased onto the current `{{integration}}` tip, re-verified, then
  merged** automatically. A rebase conflict or a re-verify failure leaves the card in
  `review/` with a `## Merge` note for a human — never a guessed resolution.

### Run it in the background, always

One card is minutes to an hour (the worker's own timeout is `--test-timeout × 6`, so 60 min
by default; the checker gets ×2). The Bash tool caps at 10 minutes, so a foreground run is
guaranteed to time out and leave a night running with nobody reading it. Launch with
`run_in_background: true` and you are notified when it exits; meanwhile
`python -m nightshift.runner --status` answers "is it stuck?" from disk without touching the run.

## Every flag, with its default

| Flag | Default | What it does |
|---|---|---|
| `--dry-run` | off | Report the selection and stop. No LLM, no writes, no lock, dirty tree only warned |
| `--status` | off | Print phase/card/elapsed from `.ai/runs/status.json` and exit. No lock, no git |
| `--card <id>` | — | Dispatch only this card. Filename stem, e.g. `menu-unlock-indicators` |
| `--base <branch>` | `[branches].integration` | Branch worktrees are cut from. **Do not pass this.** The default is the integration branch and stable branches are refused |
| `--max-cards N` | `0` (no limit) | Stop after N dispatches |
| `--until HH:MM` | — | Stop dispatching at that local time (tomorrow if already past) |
| `--max-minutes N` | — | Stop dispatching after N minutes. Overrides `--until` |
| `--sessions N` | `1` | Usage-limit windows the night may spend. `1` stops at the first wall; `2` sleeps through the reset and stops at the second. A weekly limit always stops |
| `--budget N` | `0` (none) | USD cap for the run. On a subscription plan the figure is API-equivalent, not money — use `--sessions` |
| `--card-budget N` | `0` (none) | USD cap handed to each worker process. `0` passes no cap flag at all |
| `--test-timeout N` | `600` | Seconds for the test suite (~2 min today). Also sets the worker (×6) and checker (×2) timeouts |
| `--stale [N]` | `0` (skip) | After the cards, spend leftover window on the Tier-2 staleness sweep. Bare `--stale` = every doc that changed since last verified; `--stale 3` caps it |
| `--append-digest` | off | Write the digest without advancing the read baseline — the next run's window still reaches back past this one. `night.py`'s unattended default carries it; a session you drive by hand should not, since running it yourself is the signal you are about to look |

Nothing else is needed for a normal request. `--budget`/`--card-budget` exist for API
billing and are off here on purpose. Leave `--append-digest` off too, for the same reason —
you invoking the runner means you are here to read `Digest.md` right after, so let it reset.

## Before you spend money

- **Confirm if {{maintainer}} was not explicit.** A dispatch makes real commits and burns real
  session window. *"Run card XYZ now"* is explicit; *"what about card XYZ?"* is not — answer
  with `--dry-run` instead.
- **Naming a card waives the unattended-night checks** — `unattended: false`, backoff and
  the attempt limit — because a person asking by name is the supervision those substitute
  for. It does **not** waive `requires:`, `worker: none`, a missing charter, a `card_schema`
  violation, or a card outside `tasks/`. The runner refuses with the reason and exits
  non-zero; relay that reason, do not retry around it.
- **An unmet `--card` is an error, never a quiet success.** Unknown id, or the card is in
  another lane — both refuse and name which.

## When it refuses (preflight)

Five checks, each exiting 1 before anything is dispatched:

| Refusal | Fix |
|---|---|
| kill switch: `.ai/STOP` exists | Delete the file. It is gitignored — a local, per-machine off switch |
| base branch is forbidden / missing | You passed `--base`. Don't; stable branches are refused by design |
| working tree is dirty outside `Board/` (only when on `{{integration}}`, the in-place case) | Commit or stash first — or switch to your own branch, where a dirty launch checkout no longer blocks. `--dry-run` only warns |
| the dedicated `{{integration}}` checkout is dirty | It is runner-owned; commit or discard changes in `../.{{package}}-integration` and re-run |
| the `claude` CLI was not found | Set `CLAUDE_BIN`, or put it on PATH. It lives in `%USERPROFILE%\.local\bin` on this box |
| tier binding unreadable | The ` ```tier-binding ` block in §16 is broken |
| another runner holds the lock | One at a time. A *stale* lock (dead PID) is taken over automatically |

## Stopping a run

Create `.ai/STOP` (any text in it is echoed as the reason). The runner exits at the top of
the loop and while sleeping out a usage window — it never dies mid-worker, so expect the
current card to finish first. Killing the process instead is safe but leaves `started:` with
no `finished:`; the next run recovers that card and the attempt still counts.

```bash
echo "stopped by hand" > .ai/STOP      # stop
rm .ai/STOP                            # re-enable
```

## Reading the result

Each card ends in exactly one lane, and the run log says which:

| Outcome | Lane | Means |
|---|---|---|
| reviewed ok | `testing/` | Gates + tests passed and the diff reviewer said `ok`; the runner **rebased `ai/<id>` onto `{{integration}}`, re-verified and merged it**. {{maintainer}} tests it |
| `needs_fix` | `tasks/`, or `needs-decision/` past the attempt limit | Gates + tests passed but the reviewer found a concrete, verifiable defect with one correct answer — no {{maintainer}} involved, it goes straight back for another attempt carrying the `## Review Finding`. Only escalates to `needs-decision/` if it keeps recurring past the card's attempts |
| reviewer flagged a choice | `needs-decision/` | Gates + tests passed but the reviewer needs {{maintainer}}'s judgment; the question is on the card |
| `review` | `review/` | Fell back to a human review — an artefact-only card (art), or the reviewer/rebase-merge could not decide (a wall, a conflict). The `## Merge` note says why |
| `parked` | `needs-decision/` | The worker hit a real ambiguity and wrote a `## Question`. **A success state**, not a failure |
| `failed` | `tasks/`, or `failed/` at 3 attempts | Gates red, tests red, worker non-zero, or it produced neither a commit nor an artefact. Reason lands in the card's `## Error` |
| `limited` | stays in `tasks/` | Usage wall, and the stage had **nothing** written when it hit. **The attempt is given back** — the card was not blamed |
| `blocked` | stays in `tasks/` | The gate harness itself crashed. Attempt given back, and the night stops: no card can be judged until it is fixed |

A wall is **not** an outcome of its own when the stage that met it had already written a
complete verdict. A checker that wrote `pass`, a reviewer that wrote `ok`/`needs_fix`/
`needs_decision`, a
`stale-hunter` that wrote `complete: true`, or a worker that wrote `outcome: parked` and only
*then* hit the wall on its wrap-up call has finished its work — the verdict is honoured, the
card settles in the lane above on its own merits, and the attempt is spent. The run log says
so in as many words ("the verdict was honoured and the card landed"), which is what tells that
case apart from the `limited` row above. The night still treats the wall as a wall: the session
is counted and it sleeps or stops exactly as it would have, just without retrying a card that
is already done. A verdict that is *not* terminal — a checker's `revise` with rounds still
unspent — stays `limited`, because those rounds cannot be spent in a closed window.

Where to look:

- `.ai/runs/<id>/attempt-N/` — prompt, worker JSON, gate log, pytest log, rescued artefacts.
  Gitignored and machine-local.
- `.ai/runs/YYYY-MM-DD.log` — the whole night, tee'd from stdout. The first thing to read.
- `.ai/runs/records/<stamp>.json` — the same run as structured data: every dispatch with its
  outcome and landing, every skip with its reason, the sweep's yield, why it stopped. This is
  what `Digest.md` is built from, so it is the file to check if the digest looks wrong. Written
  as the run goes, so it is readable *while* a night is in flight and survives one that dies.
- The card's own `## Telemetry` / `## Error` / `## Question` — committed, so these sync.
- `Digest.md` — rewritten at the end of every run. Never edit it by hand.

Report back which lane the card landed in and, if it did not pass, the one-line reason from
the log — not a paraphrase of the transcript.

## Do not

- **Do not run the full pytest suite yourself afterwards.** The runner already ran it inside
  the worktree; running it again on the main checkout tests a tree the work is not in.
- **Do not merge the `ai/<id>` branch.** The branch is the deliverable and merging is
  {{maintainer}}'s, through `review/` and the pre-merge preflight.
- **Do not edit a card the runner is holding** — it owns `attempts`, `branch`, `started`,
  `finished`, and lane moves. Wait for it to settle.
