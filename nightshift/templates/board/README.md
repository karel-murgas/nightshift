# The board

One card = one markdown file. One state = which directory it is in. That is the whole
mechanism — no database, no daemon, resumable from disk alone after a 3 AM reboot.

```
ideas/ → inbox/ → tasks/ → review/ → testing/ → done/
  ↑                 ↕                             ↘ failed/
{{maintainer}}'s      needs-decision/
```

## Seeing it

Open this repository as an **Obsidian vault** (Obsidian → *Open folder as vault*, and
point it at the repo root). `{{board}}.base` — written by `nightshift init` — gives you
three views of these directories:

| View | What it shows |
|---|---|
| **Kanban** | one column per lane, cards draggable between them |
| **Live** | everything except `done/` and `failed/`, grouped by lane |
| **Archive** | only `done/` and `failed/` |

**Two plugins, and the Kanban needs the second one.** *Bases* is core (ships with Obsidian
1.9+) and gives you the two tables. The Kanban view type is **not** core — it comes from
the **Base Board** community plugin (Settings → Community plugins → Browse → *Base Board*).

If you see **`Unknown view type: kanban`** when you open `{{board}}.base`, that is exactly
this: Bases is on, Base Board is not. Install and enable it and the column view appears;
the Live and Archive tables render either way.

Neither is required for anything to *work* — nothing in the framework reads this file, so
on an older Obsidian, or with no plugins at all, the board behaves identically and you
read it as directories.

**The directory is the state, not the view.** Dragging a card in the Kanban rewrites its
`state:` field, and the reconciler moves the file to match — see the mismatch table
below. Nothing in the framework reads `{{board}}.base`, so you can restyle it freely.
`{{board}}/{{private_lane}}` is filtered out of every view, with one exception: a note
you have flagged with a `state:` appears, because that flag is how a note asks to leave
the lane.

## The three input lanes, and who owns each

| Lane | Owner | What it means |
|---|---|---|
| `ideas/` | **{{maintainer}} alone** | quick notes, half-thoughts, things they want to sit on. Czech is fine — nothing parses it. **No judgment actor may open this folder.** |
| `inbox/` | shared | "I've decided I want this. Help me refine it." Raw is fine — {{maintainer}} never writes frontmatter; triage fits it. |
| `tasks/` | the system | actionable, dispatchable, no further human input needed |

**Readying an idea: set `state: inbox` on the note.** It appears on the Kanban at once,
and the next reconcile run moves the file. A note with no `state:` is invisible to
everything — not on the board, not read by any agent, not scanned for staleness.

## Two copies of one fact, and the script that keeps them honest

The lane directory is the truth. `state:` is a denormalised copy, needed because Base
Board groups on a property and because the gate uses their disagreement as a crash check.
**Base Board never moves files** — a drag writes `state:` and stops — so
`python -m nightshift.reconcile` makes the folder catch up:

| Situation | Signal | What reconcile does |
|---|---|---|
| a note you just placed | no `state:` | folder wins → stamp `state:` |
| a card you dragged in the Kanban | `state:` ≠ folder | state wins → move the file |
| an idea you flagged ready | `state:` on an `ideas/` note | move it out to that lane |

It reports by default and changes nothing; `--apply` performs, `--commit` commits.

**One habit this asks of you:** once a card is on the board, move it by dragging in the
Kanban, not by dragging the file in the sidebar. A manual file move leaves the old
`state:` behind, which reads as a drag, and the card gets pulled back.

## The rest

- **`needs-decision/`** — parked on a human answer. This is a **success state**, not a
  failure. A card here must carry a `## Question` section stating what was attempted,
  what is ambiguous, the candidate answers and what each implies.
- **`review/`** — gates green, awaiting Claude review, or waiting on a sibling card.
- **`testing/`** — merged to the session branch, awaiting {{maintainer}} at the keyboard.
- **`done/` and `failed/`** are the archive. **Nothing is deleted** — Session H's
  self-improvement loop reads them as its evidence. Filter them out of the Bases view
  rather than deleting the files.

## The runner

**You start it by hand — there is no scheduler.**

```
python -m nightshift.runner --dry-run             # what it would do, and why each card is skipped
python -m nightshift.runner --card menu-unlock-indicators   # just this one
python -m nightshift.runner --max-cards 1         # first dispatchable card, then stop
python -m nightshift.runner --until 06:30                   # until the session limit, or 06:30
python -m nightshift.runner --until 07:00 --sessions 2      # spend two session windows
```

**A night is measured in session windows, not dollars.** `--sessions 1` (the default) works
until the plan's usage limit is reached and stops; `--sessions 2` sleeps through the first
reset and stops at the second wall. A weekly limit always stops — it does not reopen before
morning. `--budget` and `--card-budget` still exist but default to 0, meaning no dollar cap:
on a subscription plan the figure is API-equivalent rather than money spent.

**The card in flight when a limit lands is not blamed for it.** Its attempt is given back
and it is retried at the head of the next window, because the plan running out is not a
fact about that card.

**Running one card on purpose.** `--card <id>` (the id is the filename stem) is the
interactive path — ask Claude in chat to "run card XYZ via the runner" and this is what it
does. Because naming a card is an explicit human request, the checks that exist to decide
what may run *with nobody watching* are waived for it: `unattended: false`, the backoff
wait, and the attempt limit. The ones about physical reality are not — a missing
`requires:` capability, no `worker:`, no charter, or a card that fails `card_schema` still
refuse, and so does a card that is not in `tasks/`. **It always says why and exits
non-zero** rather than quietly doing nothing.

`--dry-run` changes nothing and does not mind a dirty tree, so it is the right thing to run
any time you wonder whether the board is in a fit state.

A real run takes cards from `tasks/` only, one worktree and branch (`ai/<id>`) each, and
files the result: gates green → `review/`, worker parked it → `needs-decision/`, gates red
→ retried up to three times, then `failed/`. It writes `Digest.md` at the end.

- **Stop a run: create `.ai/STOP`.** The runner exits at the top of the loop, mid-run
  included — and while it is sleeping out a usage limit, which it checks for every minute
  rather than sleeping straight through. Gitignored, and the vault syncs, so it works from
  your phone.
- **A card only runs if it says `unattended: true`** — which claims a *machine can tell a
  failed attempt from a finished one*, **not** that nobody needs to look at the result. You
  still see everything at `testing/`; that is what the lane is for. Hardware goes in
  `requires:`, open decisions go in `needs-decision/` — neither is a reason to set this
  `false`. Default it to `true` on code cards.
- Run output (transcripts, gate and pytest logs) goes to `.ai/runs/<id>/attempt-N/`. The
  card keeps the current `## Error` — and since 2026-07-31 that section quotes the failing
  tests and their assertions inline, because `.ai/runs/` is gitignored (so it says nothing
  on any other machine) and is deleted when the card is retired (so it says nothing on that
  one either). The `## Error` block, and the same excerpt in `Digest.md`, are the copies
  that sync; the run directory is the unabridged one, on one host, until it is pruned.
- **Do not move a card by hand while a run is in progress.** The runner rescans, but a move
  mid-dispatch is the one race the file-move design does not cover.

**Adding a machine:** add a row to `.ai/hosts.json`, keyed by its hostname
(`python -c "import socket; print(socket.gethostname())"`). It is committed, so a box is
configured once and then syncs. The row declares what that machine can do (`capabilities`,
matched against a card's `requires:`) and how much permission a worker gets there. An
unlisted machine gets nothing — `requires:` cards stay in `tasks/` and cost nothing —
which is why adding a box is a visible edit rather than something that happens by default.
If the checkout pre-dates the `.gitattributes` landing (2026-07-30), run
`.ai/normalize_worktree.py` once to re-materialise CRLF files through the `eol=lf`
filter; a fresh clone does not need this.

Spec and the reasoning behind every choice: `.claude/plans/ai_team/09_runner.md`.

## Conventions

Schema and the reasoning behind every field: `.claude/plans/ai_team/03_board.md`.
Enforced on every edit by `nightshift/gates/card_schema.py` — run
`python -m nightshift.gates.run card_schema`.

`tier:` is what the dispatcher resolves to a model, via §16's table.
**No caller ever names a model.** A `PreToolUse` hook (`nightshift/hooks/tier_guard.py`) refuses
a spawn that names a card path without stating a resolved tier.

**Business level lives at the top of a card; technical detail goes below `## Technical`.**
Run output — diffs, full test logs, error dumps, attempt history — does not belong in the
card; it goes to `.ai/runs/<id>/` and the card links to it. The one deliberate exception is
the bounded failure excerpt in `## Error` (a few lines, marked `stale-ok` because quoted
pytest output is evidence about a past attempt, not a claim about the current tree): a
committed card is the only copy a second machine can read.

**A card that reached `review/` carries a `## Summary`** — the worker's own 2-4 line account
of what it did, written by `settle()` from the mandatory `summary` field in its verdict JSON.
This happens whether or not the worker also wrote a `## Thread` entry, so it is the thing to
read first at `testing/`; `## Thread`, when present, is the fuller log.

`Digest.md` in the vault root is **generated and overwritten on every run** by
`python -m nightshift.digest`. Never write an answer there; it will be gone. Answers go in the
card's `## Thread`.

<!-- stale-ok: `.ai/stale_status.json` is created at runtime by the first `--stale` run on a
     machine (committed once it exists, unlike the gitignored ledger) and legitimately does
     not exist in a fresh clone. -->
`Digest.md` reports **the runs**, not the board (restructured 2026-07-30). It opens with a
one-line banner — "N run(s) since the <date> digest: X landed · Y failed · Z for you to
decide" — and then has two halves that must not be confused:

**1. What happened, one block per run, newest first.** Every run since the last digest gets
its own block, headed with its clock window and a one-glance verdict — `finished`, `cut short`
(ran out of the budget it was given), `aborted` (something was *wrong*), or `killed` (died
without writing a digest of its own). Inside, your questions in your order:

- **Failed** — every failed attempt, whether or not the card changed lane. When several
  failures share one root cause they are collapsed: one broken gate is one problem to fix,
  not N unrelated cards;
- **Needs your decision** — what the run parked, question and options inline;
- **Passed** — what landed, tagged `play` / `look` / `review`, with the worker's
  `## Summary` and `model · $cost · time`;
- **Stale hunter** — docs selected vs. verified vs. carded, and a loud line when a sweep
  produced nothing at all (that is a broken sweep, not a quiet one);
- **Skipped** — the cards that could not be dispatched, grouped by reason.

**2. Still waiting on you** — standing board state, labelled as carry-over and deliberately
terse. Open decisions from earlier runs (the one thing here that keeps its options inline,
because it is what you *answer* from), the `failed/` lane with each `## Error`, a **count**
for `testing/` + `review/` (the Kanban lanes are the list — this used to be six paragraphs
duplicating them), the queue with never-dispatched cards and a starving warning once one has
waited a week, and the two advisories that explain why work is *not* happening.

Work you did yourself during the day never appears in half 1 — it is in no run's record, so
nothing can report it as unattended work. It still shows in half 2 if it is waiting on you.

**A stretch of unattended runs never loses one.** Every run keeps its own record regardless,
but whether the *report* window resets is separate: a normal run's digest commit advances the
"you've seen this" baseline, while `runner.py --append-digest` — `night.py`'s default for its
unattended path — writes its digest without advancing it, so the run after it still reports
back to the last time the baseline *did* move. A weekend of scheduled nights with nobody
reading them stacks up rather than each overwriting the last; running the runner yourself
resets it, on the assumption that if you started it, you're about to look.
Note the consequence: `triage` runs at the lead tier in an interactive session, not inside the
runner, so its new cards are daytime work and get no section. The stale sweep's fix-cards *are*
run output and appear under their run's **Stale hunter**. Each card's prose is inlined so you
rarely open one — which is also why every `tasks/` code card carries a one-paragraph
**`## Approach`** (or `## Subject` for art), the core "how".
