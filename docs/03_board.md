# Doc 03 — The board and the card schema (Session E, 2026-07-22)

The board is `Board/`. A card is one markdown file with YAML frontmatter. A state
transition is a **file move plus a git commit** — no database, no daemon, no in-memory
state (`00_architecture.md` §5). Everything below is derived from the three cards that
already existed, plus one non-perk card written to falsify it — not invented up front.
§7 records what the derivation got wrong and how, because that is the part worth keeping.

Enforced by `nightshift/gates/card_schema.py`. If a rule here is not in that gate, it is a
suggestion, and §12 says suggestions do not hold.

## 1. Lanes

The board lives at `Board/`, at the repo root — **not under `.ai/`.** Obsidian ignores
any folder whose name starts with a dot, so a board under `.ai/queue/` could never be a
vault view, and Karel's requirement is to see the board's state at any time. `.ai/` keeps
the machinery (gates, hooks, recipes, agents); the repo root holds the state a human
looks at.

**The Obsidian vault root is the repo root** (2026-07-22). It was briefly a nested
`project_tigress_notes/` folder, and that was wrong for one concrete reason: the Claudian
plugin makes the vault root the working directory of the Claude Code session it embeds,
so a nested vault would launch every Obsidian-side agent one level below `CLAUDE.md`,
`.ai/gates/` and the codebase. Vault root and repo root have to agree. The cost is that
`Board/`, `Board.base` and `Design/` are top-level folders in a game repo, which is
honest — the board is part of this project now.

`Design/` (formerly the vault's `Done/`) holds design specs for shipped features. It is
**not backlog and is never mined**: four plan docs point into it, and it is subject to
the same keep-extract-then-delete rule as the pre-AI-team plan docs
([[plan-doc-extraction]]). Putting those files in `Board/done/` was
considered and rejected — they are documents, not cards, and `card_schema` would
correctly reject every one of them.

```
ideas/           Karel's private scratch. Czech, unfinished, never read by any script.
  ↓ Karel moves it by hand — the one manual move on the board
inbox/           "I want this. Help me refine it." Raw is fine; frontmatter optional.
  ↓ triage (lead tier)
tasks/           actionable card. Ready to dispatch with no further human input.
  ↓ worker
needs-decision/  parked on a human answer. A SUCCESS state (§13), not a failure.
  ↓ Karel answers → back to tasks/
review/          gates green, awaiting Claude review, or waiting on a sibling card.
  ↓
testing/         merged to the session branch, awaiting Karel at the keyboard.
  ↓
done/  |  failed/
```

`00_architecture.md` §5 sketches a coarser five-lane set (`inbox, active, review, done,
failed`) and §13 the eight above. **§13 wins** — it is later, it is the one with the
`needs-decision/` design constraint attached, and `active` turned out to be a property of
a card (a runner-owned `started:` timestamp), not a lane. Recorded here so nobody
reconciles them again.

### `ideas/` changed meaning on 2026-07-22 — recorded so nobody reconciles it back

Session F renamed the input lanes to Karel's own three-way split, and the rename
**inverted `ideas/`**. It used to mean the post-triage lane ("recognised, deduped,
scoped, not yet actionable"); it now means Karel's *pre*-triage private scratch.
`inbox/` kept its meaning exactly. If you find a doc still describing `ideas/` as a
triage output, that doc is stale — this section is authoritative.

The middle state `ideas/` used to hold no longer needs a lane. A scoped-but-not-yet-
actionable card is an `inbox/` card that has grown frontmatter and a `## Thread`
conversation; it stays put while it is refined and moves once it is actionable.

`inbox/` is schema-optional: Karel never writes frontmatter, but if triage has fitted
some, it must be valid. Every other lane must pass `card_schema` in full.

### The privacy boundary around `ideas/`, and the one hole deliberately left in it

The first draft said "no script enumerates `ideas/`". That held for about a day, and the
thing that broke it is worth recording, because the replacement rule is better.

Karel wanted to ready an idea by setting a property on it rather than by dragging the
file between folders — and to see it on the Kanban the instant he did. Both halves are
reasonable, and neither is possible if no code may look in the folder. The rule is now:

> **No *judgment* actor ever reads a note in `ideas/`** — not triage, not any subagent,
> not the digest. Since 2026-09-23 no code reads it at all: Karel readies a note with the
> panel's *Promote*, i.e. `boardcmd promote <note>`, which moves the file into `inbox/` by
> name without opening it. (Until then a single-field reader, the reconciler, relocated a
> note flagged with `state:`; it went with the field — `remove-obsidian-leftovers`.)

What the boundary protects: no machine judges his raw half-thoughts, nothing mines them
overnight, no prose leaves the folder. `doc_scan` excludes it, `card_schema` does not
enumerate it, triage globs `inbox/` only.

Two consequences that keep it airtight:

- **`ideas` is not in `board.LANES`.** `promote` moves a note *out* of the private lane;
  nothing moves one *in* except a new note Karel writes there himself.
- **Version control may move the notes, and only move them** (2026-08-14). Karel: *"I want
  you to be able to commit and push cards in ideas folder. I just want you to not use text
  from them for any game planning, game concepts etc."* The notes are committed to git like
  everything else in `Board/`, so somebody has to be able to stage and push them — and until
  this, nobody could: `commit_pathspec` refuses a commit that does not name its paths and
  `ideas_fence` refused any command that did, so between them the lane was uncommittable by
  accident rather than by decision. `ideas_fence` now lets a `Bash` call through when every
  segment is a `git` subcommand that relocates a path without printing it. This does not
  dent the boundary above: the protected thing is *the text reaching a judgment*, and
  `git add` reaches no judgment. Reading is still never, and the second half of Karel's
  sentence is the rule that outlives the mechanism — **no idea's text feeds a card, a
  design decision, or a game concept**, whatever route it arrived by.
- **Command Center carries the oversize mark** (2026-08-08,
  `mark-oversized-cards-on-the-board`). A `tasks/` card past
  `runner.CARD_COMFORT_BYTES` gets a **too big · N KB** chip on its row, tooltipped with the
  advisory; every other lane and every smaller card show nothing.
  Deliberately *rendered*, not a field: no writer to keep in step, nothing to go stale, and
  a hand-compact clears it the moment the page re-renders — the one actor no written mark
  could ever have covered, since the `PostToolUse` hook fires on Claude's edits and never on
  a person's.
  It was an Obsidian Bases `oversize` formula over `file.size` until 2026-09, which had to
  restate the threshold and the lane in YAML because no import crossed that boundary, and
  could not even explain itself there (Bases dropped YAML comments on every vault open) — a
  whole gate, `board_view_sync`, existed to catch the restatement drifting. The panel calls
  `runner.oversize_note` instead, so the threshold is imported and the lane comes from
  *calling* it rather than from re-typing `"tasks"`. Durable prose: `Board/README.md`.

### No `state:` field (removed 2026-09-23)

Cards carried a `state:` copy of the lane because Obsidian's Base Board grouped on a
property and never moved files; the reconciler made the folder catch up. With Obsidian gone
(2026-09-02) the copy could only disagree with the folder, so the field, the reconciler and
its runner-start call were removed (`remove-obsidian-leftovers`). The lane directory is the
only state; `card_schema` refuses a `state:` key, so a template or agent reintroducing it
goes red.

## 2. Frontmatter

Two owners. This split is the useful part of the schema: it says what a human or a triage
pass writes, versus what the runner appends as it goes. A runner that crashes mid-card
rewrites only its own half.

### Author-owned — required in every lane except `inbox/`

| Field | Values | Who reads it |
|---|---|---|
| `id` | slug; **must equal the filename stem** | every cross-reference; the branch name |
| `title` | one line, imperative or declarative | morning digest |
| `tier` | `worker` \| `lead` | **the dispatcher** (§16). No caller ever names a model |
| `worker` | a stem in `.claude/agents/`, or `none` | the dispatcher, to load the charter |
| `recipe` | a stem in `.ai/recipes/`, or `none` | the executor, to load the spine |
| `unattended` | `true` \| `false` | the runner, **before** dispatch (§G) |
| `verify` | `play` \| `review` | `settle()`, to decide the landing lane; the digest, to label it |
| `created` | ISO date | digest, ageing |

`unattended: true` is a claim that **a machine can tell a failed attempt from a finished
one** — not a claim that no human needs to look at the result. It is declared by the
author and checked by the runner; the runner never infers it (Session G's "do not let the
runner decide what is safe to run unattended"). **Art is `unattended: true` with
`requires: gpu-box`** — its approval is permanently Karel's, but that happens at
`testing/`, which is not what this field means. See §5.

Three conditions, all of them about the *attempt*, none about the deliverable:

1. No live open question. Already enforced for `tasks/` by `card_schema`.
2. The machine-checkable half of `## Acceptance` is strong enough that a wrong attempt
   goes **red** rather than landing green-and-wrong.
3. The worst case is a branch Karel deletes — not corrupted state, not a spend on a wrong
   premise, not a change nobody can review.

### `verify:` — which desk a finished card lands on (2026-08-06)

`testing/` means "merged, awaiting Karel at the keyboard", and until 2026-08-06 it was the
**only** exit from a successful dispatch. So the reviewer's two-way verdict routed *when* he
saw a card, never *whether* — and eleven of the sixteen cards sitting in `testing/` had no
player-visible surface at all (`dead-code-gate`, `merge-check-has-no-tests`,
`unreferenced-symbol-gate`, …). They were not waiting on him in any actionable sense; they
were waiting on him because nothing else was possible, and the ~5 cards he genuinely should
play were buried among them. Karel, 2026-08-06: *"Lot of them is like 'something deleted from
code' or quite technical like inner wiring of something. I have no power to test them."*

| value | meaning | route after a `reviewed` verdict |
|---|---|---|
| `play` | has a player-visible surface; Karel must see it run | merge → `testing/`, carrying a `## How to test` |
| `review` | no surface he can exercise; the reviewer's `ok` is the acceptance | merge → `done/` |

Four properties, each load-bearing:

- **Declared at triage, not inferred at the end.** Modelled on `tier:` for the reason that
  field works: decided when the card is written, enforced by `card_schema`, consumed by the
  runner, and no caller ever guesses. `card_schema` requires it in `tasks/` only — archived
  cards predate the field and are not retroactively reddened.
- **Absent ⇒ `play`**, at every read site (`board.Card.verify`), and so does any value the
  schema would reject. The default must fall toward Karel's desk: a card reaching `done/`
  because someone forgot a field is the one direction that cannot be recovered from.
- **The reviewer still runs on every card.** There is deliberately no value that skips it.
  Its job is not re-proving correctness — gates and tests do that — but catching *"I didn't
  ask for that"*, and it is the only independent look between a worker's self-assessment and
  `done/`; the worker wrote both the code and the tests that pass on it. `needs_decision`
  keeps diverting to `needs-decision/` from either value.
- **The scenario is the worker's, not triage's.** Triage can decide `verify: play` up front;
  it cannot write *"start a run, take a shotgun, pick up a second one, the hotbar should
  show…"* before the work exists. So `how_to_test` joins `summary` in the worker's verdict
  JSON and `settle()` writes it onto the card as `## How to test`, the same deterministic
  path `## Summary` already takes (`menu-summary-on-card`) rather than left to agent
  discretion. `card_schema` requires the section on a `verify: play` card **in `testing/`**,
  where it is knowable — never in `tasks/`.

**An inline card derives it, and got it wrong for a month (2026-08-29).** `ingest._INLINE_CARD` hardcoded `verify: review` on the premise that *"the person who did the work is the person who would have played it, so `testing/` would be asking Karel to verify himself."* That conflates two things: `inline` means the work is handled in a live session at the keyboard rather than dispatched overnight — it does **not** mean Karel typed it. A session does the work; he may only have agreed the shape of it beforehand. So three finished, player-visible features went straight to `done/` unplayed ([[alarm-ice]], [[self-kill-stats]], [[clean-getaway-rework]]) before Karel noticed one of them: *"I think the card went straight to done instead of to the testing."* The value is now derived from the `nightshift` flag the classifier already sets — tooling gets `review`, everything else gets `play` — which puts the inline card back under the "default falls toward Karel's desk" rule the bullet above states. A tooling note the classifier missed costs one drag out of `testing/`; the reverse loses the verification silently.

### The definition this replaced, and why it was wrong (Karel, 2026-07-23)

The first version read *"every acceptance criterion on this card is machine-checkable"*.
That is far too strong, and Session G's runner shipped into a board where **it disqualified
every card** — ten in `tasks/`, none dispatchable.

The mistake is a conflation. `03_board.md` §3 has every card split its acceptance into
machine-checkable and judgment halves *precisely because both are normal*, and almost
anything with a visible surface has a judgment half — "the badge is legible at hub scale",
"3 damage feels right in play". Under the old wording the runner could only ever have run
pure-logic refactors, which is a sliver of this project's actual work.

What the old wording forgot is that **the human checkpoints are downstream of the runner,
not instead of it.** `00_architecture.md` §11's pipeline is gates → Claude review
(`review/`) → Karel at the keyboard (`testing/`). A card that runs overnight is *already*
seen by two reviewers before it reaches `dev`. Karel, 2026-07-23: *"it is OK if the results
need to be manually verified."* The lane machinery was built for exactly that and the flag
was quietly duplicating its job.

So the question `unattended:` asks is narrow: **can the runner decide, without judgment,
whether this attempt is worth a human's attention?** If yes, run it and let `review/` and
`testing/` do what they are for.

**Art is `true`, and the paragraph that used to say otherwise was the bug (2026-07-30).**
This section ended, for a week, with *"if the answer needs an eye — art, where no script
separates a good sprite from a bad one — it is `false`"* — flatly contradicting its own
opening paragraph thirty lines above, which says art is `unattended: true`, and
contradicting §5's *"Art and audio are approval-gated, permanently — and that is not
`unattended: false`"*. §5 was corrected on 2026-07-23; this paragraph was not, so the doc
answered the question both ways depending on where you stopped reading.

That is not a cosmetic inconsistency, and the cost is measurable. Every one of the five
`menu-art-*` cards carries `unattended: true` in its frontmatter and, in its body, the line
*"**Karel:** is this the picture he wanted — why `unattended: false`"* — the contradiction
copied verbatim into the work product. Three `tasks/` cards were triaged `false` and sat
undispatchable until Karel spotted them by hand on 2026-07-27, having correctly diagnosed
the field as *"given too often"*. A rule stated two ways is worse than a rule stated
loosely, because both readings can cite the doc.

**The narrow reading is the one that survives.** The three excluded reasons below cover
almost every case anyone reaches for the flag for, which is why `false` should be rare
enough to be surprising. If a card looks like it needs `false`, the near-certain answer is
that it needs one of these instead:

- **Needs particular hardware** → `requires:`. The runner checks it before dispatch, so a
  card that needs the ComfyUI box simply does not dispatch on the laptop.
- **Needs a decision** → the `needs-decision/` lane, and a worker that hits an ambiguity
  parks rather than guessing (§13). A card in `tasks/` has already passed that test.
- **Needs a human to look at the deliverable** → `review/` and `testing/`. This is the one
  that keeps getting mis-declared, and it is the whole point of the correction above:
  "somebody must check this afterwards" is what the *lanes* are for. It says nothing about
  whether the attempt can be made with nobody watching.

Enforced, now that the prose has been wrong once: `card_schema` rejects a card whose body
claims an `unattended:` value its own frontmatter does not have.

### Optional
<!-- stale-ok: `blocked_on` below is the retired frontmatter key `requires` replaced.
     It resolves against nothing by construction — that is the point of the sentence. -->

| Field | Values | Who reads it |
|---|---|---|
| `requires` | a host-capability slug, e.g. `gpu-box` | the runner, before dispatch — **not** a scheduling hint but a hard precondition |
| `checker` | a stem in `.claude/agents/`, ≠ `worker` | the runner — its presence turns the dispatch into a bounded producer→checker loop (§16's second seam) |
| `kind` | `chore` \| `inline`, or absent for a full card | `chores.select`, to find the batch; `card_schema`, to know which sections a card of that sort actually owes |
| `route` | one of `chore` \| `inline` \| `scribe` \| `triage`; **`inbox/` only** | `ingest`, to know which notes a classify pass still owes a dispatch; the panel, to group the lane |

`kind:` says what sort of work item this is when it is not a full card, and the only thing
the schema relaxes for either value is `## Approach` — for the same reason in both cases:
a mandated one-paragraph "how" produces a restatement of `## Intent` rather than a second
thought. For a `chore` the intent *is* the approach; for `inline` the person doing the work
is the one who would have written the paragraph. **`kind: inline` always carries
`unattended: false`**, and the gate rejects the combination that says otherwise.

`route:` is the classifier's answer, written onto the note itself rather than only into
`Routing.md`. That is what stops a routing pass paying twice for the same note: `ingest`
classifies only the notes with no `route:`, so a note waiting on the expensive triage route
— which nothing dispatches automatically — is decided once instead of on every pass
(Karel, 2026-08-18: *"Definitely not running classifier multiple times"*). It is legal in
`inbox/` alone; a card that left the lane answered the routing question by becoming a card.

`checker:` is declared per card rather than inferred from the worker, because whether an
artefact needs a second pair of eyes is a property of the **deliverable**, not of who made
it. The gate rejects `checker:` equal to `worker:`: an agent reviewing its own output sees
what it intended rather than what it made, which is the entire reason the seam exists, and
it is a plausible typo.

`requires:` is the only survivor of `blocked_on:`, and it survived because one real card
used it for something (see §7).

### Tool-owned — written by Obsidian, read by the runner for priority

| Field | Meaning |
|---|---|
| `kanban_order` | Base Board's within-column sort position, written on drag-reorder |

This is a fourth owner and it exists because the board has a GUI now. The gate must
tolerate `kanban_order` or the first time Karel reorders a column the whole board goes
red — and a gate that fires on him using his own board is a gate that gets muted within
a week (§12, §15).

**It is also the dispatch priority.** `board.dispatch_order` sorts `tasks/` by this field
first and by filename second, so dragging a card to the top of the column is what decides
what runs next; cards never dragged sort after every card that has been, alphabetically.

That is a reversal, and the original text is worth keeping visible because the way it got
written is the reusable part. Until 2026-07-23 this section read *"Nothing on our side
reads it, and nothing may start: it is Obsidian's scratch space, and the card's real state
is the lane."* The field had been met as a **schema** problem — Base Board wrote an unknown
key and `card_schema` went red — so the question actually answered was "may this key
exist?", and the answer to *that* question was correct. Writing it down as a decision about
ownership silently settled a second question nobody had asked: whether dragging a card up
the column means anything. It does. It is the only prioritisation gesture the GUI offers,
and Karel used it the first night the runner was real, on a queue that then ran in
alphabetical order.

The general form, worth applying to the next tool-written field: **when a tool writes a
field we tolerate, "who reads it" is a design decision, not a schema fact** — and deciding
it as a side effect of making a gate quiet is how it gets decided by default.

A fractional index sorts correctly as a plain string, which is what a fractional index is
*for*, so honouring it costs one sort key and keeps the property filename order was
originally chosen for: the same dispatch order on every boot, so a crash-and-resume cannot
reshuffle the night's work.

A card created outside triage with only `kanban_order` is born failing the schema —
correctly, because it is not yet a card. `card_schema` naming the missing fields is the
intended behaviour and the fastest route to a well-formed card.

### Runner-owned — absent until the runner touches the card

| Field | Meaning |
|---|---|
| `attempts` | integer, incremented per dispatch; drives the attempt limit |
| `branch` | the worktree branch this card ran on |
| `started` / `finished` | ISO timestamps |
| `last_outcome` | `needs_fix` / `failed` / absent — the most recent completed attempt's verdict, read by `board.dispatch_order` to jump a fixable card to the front of the queue or sink a failed one to the back (2026-08-26, replacing a flat time-based backoff) |

### Dropped from the existing cards
<!-- stale-ok: `triaged_by` and `blocked_on` are frontmatter keys this section
     REMOVES from the card schema. Naming them is the record of the removal; they
     resolve against nothing by construction. -->

| Field | Why |
|---|---|
| `triaged_by: karel + claude` | written twice, read never. `git log` carries authorship for free — the same argument as `feedback_doc_leanness` |
| `blocked_on` | two of three cards wrote `null`. The third wrote a **host capability**, not a human answer — so the field was doing two unrelated jobs badly. The human-answer job is the `needs-decision/` lane, which §13 says needs a whole section rather than a field; the host-capability job became `requires:` |

## 3. Body sections

The two perk cards converged on the same shape without anybody specifying it. The art card
did not — and that disagreement is what fixed the rule below.

**Required everywhere:** `## Intent` — what it is for and, explicitly, what it is *not*
for. Both perk cards used the negative half and it is the half that stops scope creep.

**Required in `tasks/` and later:**

- `## Acceptance` (or `## Acceptance criteria`) — split **machine-checkable** from
  **behavioural / judgment**, as all three cards did. The machine-checkable half is what
  the runner can prove; the other half is the test list, or the reason the card is
  `unattended: false`.
- `## Open questions` — must read `none` in `tasks/`. A card with a live open question
  belongs in `needs-decision/` or `ideas/`. This is the one rule that keeps the lanes
  meaningful, and it is cheap to check.
- `## Approach` — **one short paragraph stating the core of *how* the change works**, in
  plain language: the principle, not the criteria. It exists for the human reading the
  decide page (which inlines it under the card's Intent) — Karel decides whether to
  let a card run from this paragraph, so it must stand alone without opening `## Acceptance`
  or the code. Not a restatement of the acceptance bullets, not a file-by-file diff (that is
  `## Steps`), not the discovery trail (`## Triage findings`). The `heavy-guard-should-bleed`
  example: *"`is_drone` did double duty — AI and crit-DoT flavour; add an independent
  `is_mechanical` flag for the flavour and leave `is_drone` to the AI."* **Art / audio cards
  carry `## Subject` instead** — their substance is visual, judged by eye, so no prose "how"
  is required; the gate accepts either `## Approach` or `## Subject` on a `tasks/` card.

**Required only when nothing else carries the decomposition:** `## Steps` — a table of
`#`, step, files, gate. The `gate` column is what the step is finished *by*; "reviewed by
Karel" is a legitimate entry, and it does **not** force `unattended: false` — Karel reviews
at `testing/`, which is downstream of the run.

The condition is `recipe: none` **and** `worker: none`. A card naming a recipe has the
spine; a card naming a worker has the charter, which points at the skill that is the
pipeline. `hand-razors-icon` has no Steps table and should not grow one — it would
paraphrase `pixelart-asset` worse than the skill states it. Demanding a Steps table on
every card is how you get cards that restate their own recipe, which is precisely the
duplication §12b's spine/body split exists to prevent.

**Required in `needs-decision/`:** `## Question` carrying §13's four parts — what was
attempted, what is ambiguous, the candidate answers, what each implies. "I need more
info" alone is a failure; a decision Karel can make in 15 seconds from his phone is the
deliverable.

**Optional, and both cards used them:** `## Decisions locked during triage` (a table with
a rationale column — the rationale is what stops the executor relitigating a number),
`## Triage findings` (the numbered "read before implementing" list — this is where the
discovery that §16 says the card exists to preserve actually lives), `## Out of scope /
follow-ups`.

Extra sections are allowed. `adrenaline-pump` grew an "EP drain priority" section that was
a project-wide ordering rule discovered during triage; a schema that rejected it would have
lost the rule.

## 4. Transitions

A transition is `board.move`: `git mv` + one commit, with the message
`board: <id> <from> → <to>` (`boardcmd move <id> <lane>` from a session). The lane
directory is the card's only state, so there is no second copy for a half-done move to
leave disagreeing; a half-done *commit* (both copies on disk) is `card_schema`'s
duplicate-lane check.

Nothing is deleted. `done/` and `failed/` are the archive, and `failed/` is where the
evidence for Session H's correction log comes from.

## 5. The roster

`.claude/agents/`. One file per worker, `name` / `description` / `tier` frontmatter, same
format as `stale-hunter.md`.

**Two split rules, and both must be run on every new worker.**

1. **Overlap** (§16): split where context overlap is near zero, never where the topic
   label changes. §12's "ten single-concern checkers beat one ten-item checker" is about
   *checkers* and does not transfer to producers — ten narrow producers re-read the same
   files ten times, which is exactly what doc 02 measured.
2. **Producer / checker** (§16, "the second seam"): split the thing that makes an artefact
   from the thing that judges it, whenever the judgment needs only the artefact and the
   criteria. Cheaper, and — more importantly — *an agent reviewing its own output sees what
   it intended rather than what it made.*

Rule 1 alone is what Session E ran, and it is why `art-reviewer` was missed on the first
pass: rule 1 kept saying "don't split", correctly, three times in a row. **A new producer
charter must name its checker boundary, or record that it has none and why.**

| Worker | Chartered | Tier | Gate |
|---|---|---|---|
| `code-thread` | yes | worker | strong — the whole gate suite (`nightshift/gates/run.py`) + the test suite |
| `translator` | yes | worker | strongest — `i18n_parity`, `i18n_untranslated`, `i18n_loanwords` |
| `art` | yes | worker | `asset_hygiene` + `art-reviewer`, then **Karel, permanently** |
| `art-reviewer` | yes | worker | schema-checkable output |
| `code-reviewer` | yes (2026-07-24, [[automate-review-step]]) | lead | schema-checkable output; **structural** blindness — it reviews a throwaway detached checkout, and because `.ai/runs/` is gitignored the producer's prompt and transcript are not on disk there |
| `stale-hunter` | yes (Session D) | worker | schema-checkable output |
| audio | **no — not yet written, but scoped** (2026-07-30) | — | `audio_hygiene`, hard-routed to `needs-decision/` — **no reviewer, still unattended** |
| rule-scout / gate-smith / recipe-keeper | no — specced in §14, not yet needed | — | propose-only |

**Two rows are dispatched by the runner, not named by a card** — `code-reviewer` (the review
stage, after gates+tests pass) and `stale-hunter` (the sweep phase). Neither belongs in a
card's `worker:` or `checker:`; they are pipeline stages that happen to be agents.

**Audio was listed and not chartered because no pipeline existed.** That reason expired on
2026-07-30: the pipeline exists, it has shipped 23 sound effects into the game, and it has a
mechanical gate (`audio_hygiene`). **Audio still has no charter, and now that is a finding
rather than a placeholder.**

**The open question this row carried has an answer, and the answer is no.** The question was
*"whether a blind listener can judge a music clip from written criteria the way
`art-reviewer` judges a sprite"*, explicitly flagged as *"an open question, not an assumed
symmetry"*. Four rounds of Karel's feedback on the first 23 clips are the evidence, and not
one of them was recoverable from a criteria sheet:

| What Karel said | Why a blind checker could not have said it |
|---|---|
| *"Dog dying is too scary now. The beginning of the sound is too much (isn't almost human scream). The later part (dog whimper) is OK."* | Requires knowing what a dog death should sound like *in this game's register*. The criteria said "a dog death, 0.6 s, mono" — the clip met that exactly. |
| *"Machine gun is an awesome sound, but I liked it more when number of shots were the same as number of bullets."* | The defect is a mismatch with the **firing code**, not with the clip. `_BURST_INTERVAL` staggers one event per round, so the asset must be one shot. Audible only in play. |
| *"Dying sounds should be the same length (or little shorter) than dying animation."* | A cross-artefact relation. Nothing about the clip alone is wrong. |
| *"The heal sound is also too long ingame. The original synth sounds were quite short and it was fitting."* | The reference is the sound it replaced, which the checker never hears. |

Three of the four are about the clip's fit with **something outside the clip** — the firing
code, the death animation, the sound it replaced. That is the structural difference from
art: a sprite is judged against neighbouring sprites and a stated spec, both of which fit in
a payload, whereas a cue is judged against running code and the player's ear.

**So the boundary is recorded as absent — and Karel's call is that this does not mean
`unattended: false` (2026-07-30, correcting the same day's earlier draft of this section).**
No fitness reviewer does not mean no unattended dispatch; it means the loop that art runs —
generate, judge, land in `testing/` if the judge is satisfied or `needs-decision/` with its
notes if not — is missing its middle step. Drop the step, not the loop: **the worker still
runs unattended, still gates on `audio_hygiene`, and always lands in `needs-decision/`** for
Karel to accept or return, rather than only when a reviewer flags uncertainty. That is a
worse ratio of auto-accepted to reviewed than art gets — every clip needs his ear, none skip
straight to `testing/` — but it is not the same claim as "cannot run without him in the room",
and conflating the two was this section's actual mistake. §1 permits delegation only where
output has a deterministic gate; `audio_hygiene` gates *format*, never fitness, and that is
exactly why the lane, not the dispatch, is where his ear sits.

**Not yet built.** This section now records the *shape* the worker should take, not that one
exists: a chartered `audio` agent (`unattended: true`, `checker: none`, hard-routed to
`needs-decision/` rather than the default gates→`review/`→`testing/` path, since Claude
reviewing a diff cannot evaluate a clip any better than a gate can), a recipe wrapping the
`audio-asset` skill's generate→post-process loop, and the runner taught the no-checker
hard-route. All 23 shipped SFX so far were generated interactively, which is still how work
happens until that worker is written — carded as [[soundtrack-generation]] rather than
built in this pass.

**One of the four did mechanise, and that is the pattern to keep.** Once Karel stated the
third as a principle — *"this is to be generalized — sounds and animations should go hand in
hand"* — it became a derived budget (`death_animations._SHEETS` × `ANIMATION_FRAME_S`) and a
test. The route from ear to gate runs through Karel articulating the rule, not through a
reviewer inferring it.

**"Programmer" is not a role here.** Doc 02 measured splitting implement / test / UI into
three agents: 3–4× cost, ~50–70k of duplicated discovery, and a false bug report caused by
a mid-write read. They share nearly all their context. One sequential thread, three step
skills (`implement-here`, `write-tests-here`, `wire-ui-here`).

### `closer` is a skill, not an agent — applying the rule to Karel's own roster row

The sketch listed a **closer** owning the spine tail (gates, tests, help catalog, memory
update). Running §16's test on it: *would the handoff be a short payload or a briefing?*
The tail steps need the diff, the card, the surrounding code and the reason each choice was
made — that is a briefing, and a closer that re-derives it is doc 02's measurement again.

So the tail stays inside the code thread, invoked as a skill: `post-implementation-cleanup`
already exists and already routes to the single-concern checkers. The one genuinely disjoint
piece of closing — *review* — is not this role; it is the lead tier reading `review/`.

Reversible: if the tail turns out to be skipped in practice despite the skill firing, that
is evidence for a separate agent and this decision should be revisited with it.

### Art and audio are approval-gated, permanently — and that is not `unattended: false`

§1 permits delegation only where output has a deterministic gate, and no script can check
whether a sprite looks right. That much is structural: art and audio stay in the "wake up
and look at it" pile forever, and Session F's morning digest is **designed around that**
rather than treating it as a gap to close later.

**Corrected 2026-07-23 (Karel).** This section used to say art was `unattended: false`
*permanently*, and that was the old definition of the field leaking into a rule that did
not need it. Karel: *"When we are on the other computer, it should run, art-review those
and send it to test if the reviewer finds them good, or needs-decision if the reviewer says
the pipeline was not able to achieve that. Right?"* Right, and the charter already works
exactly that way — `art` generates a batch of four, hands them to `art-reviewer` blind,
retries at most three rounds, and parks with the best candidates and the reviewer's notes
if none passes, which it calls success rather than failure. That loop **terminates without
Karel.**

So the two things this section was conflating come apart cleanly:

| Question | Mechanism | Art's answer |
|---|---|---|
| Can this machine run it at all? | `requires: gpu-box` | only on the desktop; elsewhere it stays in `tasks/` at **zero token cost** |
| Can a machine tell a failed attempt from a finished one? | `unattended:` | **yes** — `asset_hygiene` for the mechanical layer, `art-reviewer` for craft, a bounded retry, a park exit |
| Who decides this is the picture we wanted? | the lanes | Karel, in `needs-decision/`, permanently |

Art is therefore `unattended: true, requires: gpu-box`. Nothing about his final say
changed, and the digest's "look at this" section, which exists precisely for this, stops
being unreachable.

**The lane in that last row read `testing/` until 2026-09-08, and that was wrong** — not
about who decides, but about where. `testing/` means *there is something in the program;
go and exercise it*, and the first pass of an art card installs nothing: candidates sit in
`assets/.tmp/`, gitignored, which is the same fact two paragraphs below already turned into
a runner consequence. `stun-grenade-visuals` landed in `testing/` with four blue grenade
icons nobody had adopted and a `## How to test` that amounted to *open these PNGs*. Karel:
*"the card is not ready. It should ended in needs decision and give me a way to show and
pick these results. It should end up in testing only after it is wired up in game."*

So his say-so happens one lane earlier than everyone else's, and the card passes through
`testing/` afterwards rather than instead: `runner.unadopted_artefacts` sees candidates
harvested with nothing written into `project_tigress/assets/`, files the card in
`needs-decision/` with the pick as its question and `after_answer: tasks`, and the pass that
answering releases installs the pick, wires it, and *then* lands in `testing/` — where the
question is no longer "is this the picture" but "does it look right in the game". The
Command Center's *Image candidates* section is the surface the pick is made on; it is the
visual twin of the audio audition surface, and for the same reason (§16's second seam ends
at a human eye, so the eye needs somewhere to look).

Two runner consequences fell out of it, both real bugs found by taking the question
seriously (`09_runner.md` §8): an art card commits **nothing** (candidates are gitignored
until approved), so the empty-diff check had to become "neither a commit nor an artefact";
and the candidates live in the worktree, which the runner destroys, so they are harvested
into `.ai/runs/<id>/attempt-N/artefacts/` first.

**And the loop itself moved into the runner** (Karel, same day). `art` no longer spawns
`art-reviewer`; the card names `checker: art-reviewer` and the runner drives producer →
checker → producer, bounded by `MAX_ROUNDS`. That is what makes the three-round limit real
rather than a sentence in a charter, and what makes the reviewer's blindness structural —
`run_checker` builds its context and never has the producer's prompt in scope. Reasoning
in `09_runner.md` §8.

What *is* delegable is everything below the final human call, and it splits three ways:

| Layer | Judges | Example |
|---|---|---|
| `asset_hygiene` gate | mechanical facts | 32×32, transparent, sheet width a whole number of frames, landed in `assets/` not `sources/` |
| `art-reviewer` | craft | reads at 20px, sits beside the other eight, clean pixels, subject legible |
| Karel | is this the picture I wanted | everything else |

The gate exists to keep the reviewer's attention off what a script already proved, and the
reviewer exists to keep weak candidates off Karel's morning. Neither promotes art to
`unattended: true`.

## 6. Tier enforcement

§16 names three landing places, cheapest first. Two of them land here:

1. **`tier:` in frontmatter** — done, required, gate-enforced.
2. **The dispatcher resolves `tier: → model`** from §16's table — Session G. Until it
   exists, a human dispatching a card passes the model explicitly, and the value comes from
   §16, never from memory.
3. **A `PreToolUse` hook on `Agent`** refusing a card-execution spawn with no resolved tier
   — done, `nightshift/hooks/tier_guard.py`, wired in `.claude/settings.json`. It fires when a
   spawn prompt names a path under `Board/`; anything else passes untouched. This is the
   enforcement point proper, because 1 and 2 are conventions and a convention is what failed
   on 2026-07-22.

The rule is **tier-based, never model-based**. §16's table is the only place the
tier→model binding may be written down; Session I rebinds it and nothing else changes.

## 7. Validation against non-perk work — where the derivation was wrong

Session E's brief said: derive the schema from the cards that exist, and validate it
against a work type that is not a perk, because a perk-only schema gets rewritten on the
first bug-fix card. That happened, twice, and both corrections are already folded in above.

### The art card was already on the board and was missed on the first pass
<!-- stale-ok: names the retired frontmatter keys (`blocked_on`, `recipe: generate-asset`)
     and the section headings the first-draft schema demanded. All are dead by design —
     recording what the derivation got wrong is this section's whole job. -->


The brief said "two ad-hoc cards exist". There are **three**:
`Board/tasks/hand-razors-icon.md` is a work-type-#7 art card, and it broke the
first-draft schema in three places:

- **It has no `## Steps`, `## Intent` or `## Acceptance criteria`.** It had `## Task`,
  `## Subject`, `## Style`, `## Why it matters`, `## Acceptance`. A schema derived from the
  two perk cards would have rejected a perfectly good card. Fixed by making `Steps`
  conditional and accepting `## Acceptance`; `## Task` was renamed to `## Intent`, which is
  the one rename that was not a lie.
- **`blocked_on:` is read after all.** The two perk cards wrote `blocked_on: null` and the
  first draft dropped it as write-only. The art card writes `blocked_on: gpu-box` — a
  *host capability*, which the runner genuinely must check before dispatch. Dropping it
  would have deleted a real precondition. It became `requires:`.
- **It named `recipe: generate-asset`, which does not exist.** No recipe was ever written;
  the `pixelart-asset` skill is the pipeline. Now `recipe: none`, `worker: art`, and the
  gate resolves both fields against real files so this cannot recur silently.

The lesson is not about art. It is that **three cards were barely enough** — the first two
agreed with each other and agreement read as confirmation. Any schema change from here
should be checked against every card on the board, which `card_schema` now does on every
edit.

### The synthetic non-perk card

[[plan-doc-extraction]] — work type #10 (cleanup / memory-doc), no recipe,
no code change, a deliverable that is mostly deletion. It is real queued work, not a
fixture. It moved two things:

- **`recipe:` had to accept `none`.** Only one recipe exists; most work types have none,
  and a required-and-invented recipe name is worse than an honest `none`.
- **The `Steps` table's `gate` column has no test to name** for doc work. It names a gate
  (`doc_reference_liveness`, `deletion_sweep`) instead — which is right, and is why the
  column is "gate" and not "test".

`## Decisions locked during triage` did not fit it either: the decision was made by Karel
in `SESSIONS.md` prose, not during triage. Kept optional; the card cites the source rather
than copying it.
