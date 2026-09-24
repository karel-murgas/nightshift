---
doc_scope: history
---

# Token economy — do more cards with the same budget

Branches: `nightshift` → `perf/token-economy` (merges first), `project_tigress` →
`karel/token-economy` (merges second). Started 2026-09-14.

**Rule for every item below: cut only work that is duplicated or thrown away.** A check
that catches real defects stays; if it is expensive, it moves to where it is cheapest
(a script instead of a model, once instead of per edit, the runner instead of the worker).

## 1. Where the budget goes today (measured)

Source: `.ai/runs/*/attempt-*/worker-1.json` and the worker/reviewer transcripts from the
last runs that were dispatched on KMu-NTB (Aug 2026, same Sonnet worker + Opus reviewer,
same hooks as today).

| process | model | turns | cache reads / call | avg context / turn |
|---|---|---|---|---|
| worker, chore | sonnet | 24–69 | 1.4–5.3M | 59–77k |
| worker, card | sonnet | 58–124 | 5.5–26.6M | 98–215k |
| reviewer, card | opus | 25–55 | 1.5–5.5M | 62–100k |
| reviewer, chore batch | opus | 27–49 | 1.8–4.3M | 66–88k |

- **Cache reads are ~70% of weighted cost** (output 15–20%, cache writes the rest). Cost is
  *turns × context*, so every turn cut and every kB kept out of context counts twice.
- **Workers are ~75% of spend**; reviewers most of the rest.
- **Close-out is 25–72% of a worker's cache reads** (measured from the first full-suite run /
  `post-implementation-cleanup` invocation to the end).
- **Every process starts at 32–39k tokens** before doing anything (system prompt, CLAUDE.md
  13 kB, skill listing 14 kB, agent listing 6 kB, prompt + card).

### What fills it

| finding | evidence |
|---|---|
| Worker re-runs verification the runner repeats anyway | 1–3 full-suite runs per card, plus `pytest -q` serial twice (tile-layer), 3–6 manual `gates.run`, `git stash` re-runs — then the runner runs gates + the test slice itself |
| `post-implementation-cleanup` contradicts the runner | skill says "full serial suite is the gate of record, don't trust `-n auto`"; runner prompt says run parallel, the runner checks the slice |
| Gate suite runs after **every** Edit/Write and its output stays in context | 50–143k chars of hook output per card (~15–35k tokens), mostly the 51-gate name list and the same `dead_code (60% confidence)` lines repeated; also 21 s wall per edit |
| Recipe hint hook repeats the same line each edit | 7–10k chars per card |
| Whole reads of huge files | `game_scene.py` 284 kB, `i18n.py` 387 kB, `hud.py` 64 kB; `game_scene.py` read 12× in one card; one `hud.py` read = 55k chars |
| Workers load orientation/history memory they do not need | taser-cyberware read `MEMORY.md`, `arch.md`, `design.md`, `design_detail.md`, `ref_i18n_catalog.md`, `ref_i18n_history.md` |
| Screenshots re-read | one PNG read 7× in aim-crit |
| Reviewer reads the diff in 4–6 `sed` chunks, after a `cat` of it | every chunk is a turn; the `cat` + `sed` pass reads it twice |
| Worker and reviewer both hand-write throwaway render scripts | `/tmp/shot1..5.py`, `/tmp/pix2..4b.py` — ~20 reviewer turns on hud |
| Chores go through the full `code-thread` feature pipeline | one-prompters run the cleanup chain, `verify-in-game`, memory fragment, doc-truth pass |
| All workers inherit effortLevel "high" from the user's `~/.claude/settings.json` | runner passes `--model` but no `--effort` |
| Chore cost is not recorded | `chores.run_one` reads turns + wall only → panel shows $0 |
| A Claude session babysat the Sep 14 batch | trailer on `b6849095`; that session's tokens come out of the same window |

## 2. The plan

Ordered by *savings ÷ risk*. Est. = share of that process's spend, rough, to be replaced by
measured numbers from phase 0.

### Phase 0 — measure first (nightshift) · no quality risk

0.1 **Record usage for every LLM call** in the run record: input, cache write, cache read,
    output, cost, turns, model, effort — worker, checker, reviewer, batch reviewer, repair,
    resolvers, `fix`, `update`. Fixes the chores `$0` too.
0.2 **`python -m nightshift.costreport <run>`** — the transcript breakdown used for this plan
    (tool-result sizes by tool, hook output, re-reads, share of reads after first edit /
    close-out). So every later item is verified by numbers, not belief.
0.3 **Quality counters** alongside: needs_fix rate, needs_decision rate, parked rate,
    `testing/` rejections, red-after-merge on `test`. Any item that moves these the wrong
    way gets reverted.

### Phase 1 — stop duplicate verification in workers · est. −25–40% worker

1.1 **One source of truth for "did it pass": the runner.** Worker instruction becomes: run the
    tests for what you touched while iterating; before the verdict, run exactly the slice the
    runner will run (`python -m nightshift.suite touched` — new tiny CLI over
    `suite.touched`) and `gates.run` once. No full suite, no serial suite, no stash re-runs.
    **Parallel by default** (`suite.parallel_args()`); serial only when running one specific
    test file or test id.
    *Quality kept:* the runner still runs gates + slice and judges by JUnit; the chore batch
    still runs the whole suite once.
1.2 **Fix `post-implementation-cleanup` §2** to match: in a dispatched run (env var the runner
    already sets for the fence) → the runner owns the suite. Interactive sessions keep a full
    parallel run; drop the stale "serial is the gate of record" text.
1.3 **PostToolUse gate hook: quiet and scoped.**
    - All-clear prints nothing (or `gates: ok`), never the 51 names.
    - Only violations in files this session changed; each one printed once, not on every edit.
    - Dispatched workers: run fast gates only per edit (i18n parity, import layering…), the
      slow/tree-wide ones once at the end — the runner runs all of them anyway.
    *Quality kept:* same gates, same enforcement point for merge; the worker still sees a
    violation it caused, immediately.
1.4 **Recipe hint hook: once per session per hint.**

### Phase 2 — a real chore path · est. −50–70% per chore

2.1 **`chore-thread` agent** (short charter): read card → locate with Grep → edit → targeted
    test → commit → verdict. No cleanup chain, no `verify-in-game`, no memory orientation.
    Memory fragment only when the diff changes a fact a memory file states. `chores.py`
    dispatches it regardless of the card's `worker:`.
    *Quality kept:* one attempt + park-on-doubt, runner gates + slice, batch full suite, Opus
    batch review per item, Karel's play-test at `testing/`.
2.2 **`--effort medium` for chore workers** (per-tier setting in manifest, see 3.3).
2.3 **Chore note:** replace "reading a lot of a large codebase is ordinary" with the locate-
    don't-read rule (3.1); keep the park triggers.

### Phase 3 — context hygiene for every process · est. −10–25% everywhere

3.1 **Big-file guard.** Extend `nightshift.hooks.tool_economy`: a `Read` of a file > ~60 kB with
    no `offset/limit` is denied with "Grep -n first, then Read the range". Deterministic, costs
    nothing, applies to worker, reviewer and interactive sessions alike.
3.2 **Dispatched workers skip memory orientation.** `code-thread` charter + `_PROMPT`: the card
    carries the context; open a memory file only to update it or when the card names it.
    CLAUDE.md's "read MEMORY.md at start" stays for interactive sessions.
3.3 **Effort per tier, explicit.** `[tiers]` gains effort (`worker = medium`, `lead = high`),
    runner passes `--effort` so no process inherits the user's interactive default. Flip the
    code-card worker only after phase 0 shows no quality drop on a few cards.
3.4 **Trim what every session loads on every turn.**
    - `CLAUDE.md` 13 kB → rules + pointers (~6 kB). The rationale paragraphs (xdist cap,
      cross-repo landing, memory kinds) move to where the pointers already point.
    - Skill descriptions: one or two lines each; `install-nightshift` gets
      `disable-model-invocation: true`.
    - Workers and reviewers get `--disallowed-tools Agent` if phase 0 shows it drops the agent
      listing (they must not spawn anyway — `tier_guard` exists for that).
3.5 **Screenshots are looked at once.** `verify-in-game`: save, read once, state what you saw;
    do not re-read the same PNG.

### Phase 4 — reviewer (opus) · est. −20–35% reviewer

4.1 **Diff inline in the prompt** when under a size cap (else the file, as today). Saves the
    `cat` + chunked `sed` turns and the double read.
4.2 **Filter noise out of the reviewer's diff, list it as a stat line instead:** cs/es i18n
    values (gates own parity/untranslated/loanwords; naturalness is the translator's), generated
    reports, binary assets. **Memory/doc hunks stay in** — 11 of 14 historical needs_fix were
    wrong prose.
4.3 **One shared render tool** (a new render-surface script, not written yet): render any registered UI surface in
    en/cs/es, base vs branch, PNG + pixel diff. Used by `verify-in-game`, the reviewer and Karel.
    Replaces the per-run throwaway scripts on both sides.
4.4 Kept as is, deliberately: reviewer blindness, Opus tier, per-item batch verdicts, prose
    self-fix.

### Phase 5 — interactive & orchestration · est. varies

5.1 **Don't babysit a run from a Claude session.** Start runner/chores from the Command Center
    or a plain terminal; `run-the-runner` skill says: launch, background, report once when it
    ends — no polling or tailing of `stream.jsonl`.
5.2 **Charters are registers, not logs** — same rule as memory. `triage.md` 29 kB,
    `code-reviewer.md` 15 kB, `scribe.md` 15 kB, `classifier.md` 15 kB: rules stay, the
    "measured on…/Karel said…" history moves to `ai_team.md`. Loaded on every turn of every such
    session.
5.3 **Check the panel passes `tier`** for classifier/scribe sessions (worker → sonnet); otherwise
    they open on the user default (opus, high).

### Bonus, wall time (not tokens)

Gate hook 21 s × ~25 edits ≈ 9 min per card; 1.3 cuts most of it.

## 3. Order of work

1. Phase 0 (nightshift) — ship alone, run one normal night/batch to get a baseline.
2. Phase 1 + 2 (nightshift prompts/hooks/chores + tigress charter/skill) — paired, merge
   nightshift first.
3. Phase 3.1, 3.2, 3.5, 4.1, 4.2 — cheap, low risk.
4. Phase 3.3 (effort), 3.4, 4.3, 5.x — after the numbers from step 2.

Each step: compare `costreport` + quality counters against the baseline before the next.

## 4. Sessions and models

Three sessions, one fresh session per block, with this file as the hand-off. A measured
run happens between them, so a session can't pick up the next block before there are
numbers to check it against.

| session | scope | model | why |
|---|---|---|---|
| A | Phase 0 (measurement + quality counters) | sonnet | mechanical: read stream JSON, write fields, a report CLI |
| B | Phase 1 + 2 + 3.1/3.2/3.5 + 4.1/4.2 | opus | changes the prompts, hooks and runner flow every card depends on; judging what is duplicate vs. what catches defects |
| C | Phase 3.3/3.4, 4.3, 5.x | opus for trimming the charters and CLAUDE.md (deciding what is a rule), sonnet for the render tool | |

## 5. Status

**Session A (phase 0)** — merged to nightshift `main` as `8bac266`.

**Session B (2026-09-15)** — on `perf/token-economy` (nightshift) + `karel/token-economy`
(here), not merged yet. What landed, by plan item:

| item | where |
|---|---|
| 1.1 slice CLI + worker prompt | `python -m nightshift.suite slice [--touched]`; `_PROMPT` names the exact command for its selector (cards `select`, chores `--touched`). `tool_economy` now denies a worker's whole-suite run **parallel too**, pointing at the slice |
| 1.2 cleanup skill | `post-implementation-cleanup` §2: worker → slice once; interactive → whole suite once, parallel. Serial "gate of record" and the `git stash` re-run are gone; `write-tests-here`, `check-test-coverage` aligned |
| 1.3 gate hook | `nightshift.hooks.gates_on_edit` replaces `gates.run; exit 0`: `gates: ok`, each violation once (re-shown if it comes back), no gate list; every gate still runs on every edit. **Deviation:** a per-edit skip of slow gates for workers was built and removed the same day — it saved wall time, not tokens, and moved a violation's discovery to the end of the attempt. **Deviation:** not filtered to the session's files — a gate often reports on a file the edit did not touch (i18n → ref doc), so dedupe is the saving, not scoping |
| 1.4 hint hook | `.ai/recipes/hint.py` prints each hint once per session (`nightshift.hooks.session_memo`) |
| 2.1 chore-thread | charter here + template; `chores.py` dispatches it whenever the checkout carries it |
| 2.2 chore effort | `chores.CHORE_EFFORT = "medium"` → `--effort medium`. A constant for now; the manifest per-tier field is 3.3 |
| 2.3 chore note | `_CHORE_NOTE`: locate-don't-read, skip the feature pipeline; park triggers kept |
| 3.1 big-file guard | `tool_economy`: whole `Read` of a text file > 60 kB denied for **every** session (settings matcher `Read\|Bash`) |
| 3.2 memory orientation | `_PROMPT`, `code-thread` charter, `CLAUDE.md` — a dispatched worker does not read the memory set |
| 3.5 screenshots | `verify-in-game` step 5: look at each image once |
| 4.1 inline diff | `review_branch` inlines a diff ≤ 60k chars at the end of the prompt, else a path |
| 4.2 diff filter | `nightshift.reviewdiff`: binaries and generated board views cut and **listed**. **Deviation:** cs/es values stay in — no gate checks what a translation means, so the reviewer is the only such check; docs/memory stay in too. Reviewer charter also told to read the quoted gate report instead of re-running gates |

### Measured, 2026-09-16 (Mithlond)

The earlier note here said the Sep 14–15 cards "were not run on this box". That was written
from KMu-NTB. **On Mithlond they were** — 30 run records, Sep 10–15, every one stamped
`"host": "Mithlond"` and every one before the merge at 21:19 on 09-15. So the whole set is
clean pre-merge baseline, and the comparison below is same-machine.

Baseline: 16 attempts over 12 cards, captured before the run (the runner prunes settled
cards' run-dirs at startup — it pruned 3 of them 8 minutes later). New: the night of
2026-09-16 01:15, one card (`skill-overview-in-run`) plus a 4-chore batch.

**Read the per-attempt walk, not `costreport <record>`, for anything summed.** `report_run`
calls `card_transcript_stats(root, card)`, which globs *every* `attempt-*` of that card —
not the attempt from that run. A card in three records is counted three times, and because
tonight reused five baseline card ids, the new records' transcript blocks mix old attempts
with new. The numbers below come from a walk over attempt dirs split on mtime.

#### Same card, baseline attempt to new attempt (worker)

| card | turns | cache-read | cost |
|---|---|---|---|
| `help-catalog-text-center` 1→2 | 76 → 28 (−63%) | 8.78M → 2.08M (−76%) | $3.31 → $1.07 (−68%) |
| `tigress-bonus-time-node` 1→2 | 107 → 44 (−59%) | 13.33M → 3.52M (−74%) | $4.31 → $1.22 (−72%) |
| `icons-for-ice` 1→2 | 148 → 29 | 22.84M → 2.02M | $6.58 → $0.80 |
| `show-weapon-schematic-stats` 1→2 | 17 → 16 | 0.96M → 0.94M | $0.46 → $0.44 |
| `skill-overview-in-run` 3→4 (card path) | 45 → 39 (−13%) | 3.03M → 2.92M (−4%) | $1.04 → $1.13 (+9%) |

The first two are the real chore evidence: work was done in both attempts, and both land
inside phase 2's own −50–70% estimate. The next two parked in the new attempt, so they did
less work and their deltas are not a saving. **Every new attempt is a second attempt carrying
a handover, which is cheaper by construction — so treat these as an upper bound, not a
measurement.** The card path barely moved, which is expected: phases 1 and 3 touch what a
worker verifies and loads, and this card's attempt 3 was already small.

#### Per attempt, all attempts

| | baseline (16) | new (5) |
|---|---|---|
| worker turns | 68.3 | 31.2 |
| worker cache-read | 7.49M | 2.30M |
| cost | $4.21 | $1.38 |
| Read bytes | 110.6 kB | 27.3 kB |
| re-read share | 24.8% | 30.8% |
| hook output (est.) | 0.26 kB | 0.10 kB |

The card mix differs, so the headline is not a clean per-card saving. Re-read share went
*up*, on a much smaller absolute base (27 kB of reads against 111 kB) — 3.1's big-file guard
cuts whole-file reads, it does not stop a small file being opened twice.

#### Reviewer

| | baseline | new |
|---|---|---|
| chore batch, per chore | $2.79 (2 chores, $5.57) | $0.79 (4 chores, $3.17) |
| card (`skill-overview-in-run`) | 31 turns, 2.39M, $2.61 | 35 turns, 1.81M, $2.25 |

The chore drop is structural, not 4.x: the baseline batch ran a per-card reviewer ($2.80)
*and* the batch reviewer ($2.77); the new path runs only the batch reviewer. On the card,
4.1/4.2 bought −24% cache-read and −14% cost for +13% turns.

#### The four confirmations

1. **Slice, never the whole suite — confirmed.** Three workers ran `python -m nightshift.suite
   slice` (`--touched` for chores, bare for the card, matching the selector split above). The
   other two parked before reaching the verdict step, so they correctly never ran it. No bare
   whole-suite run anywhere. The targeted single-test-file pytest calls are phase 1.1's
   "run the tests for what you touched while iterating", not violations.
2. **`chore-thread` — confirmed** from the run log, all four chores. **`--effort medium` is
   *not* confirmed from the transcript**: `CHORE_EFFORT = "medium"` is passed through
   `runner.dispatch(effort=...)` into `["--effort", effort]`, but nothing writes it to
   the attempt worker JSON or the stream, so `costreport` shows `effort` empty for every stage. Code
   path only — see defect 4 below.
3. **Workers did not read the memory files — confirmed.** Zero `Read` of any memory file
   across all five. Two *grepped* one: `help-catalog-text-center` hit `state_history.md`,
   `tigress-bonus-time-node` hit `ref_minigame.md` and the memory dir. Grep returns matched
   lines, so the cost is small and 3.2's point (do not load the orientation set) holds.
4. **Reviewer got the diff inline — confirmed, both reviewers.** Card: 3.3 kB patch inside a
   13.7 kB prompt. Chore batch: 15.6 kB patch inside a 26.2 kB prompt. The card reviewer still
   made six hand calls, but they are `git show --stat` and `sed -n` over *source* files, which
   is review work, not re-reading the diff.

#### Quality counters

| | baseline cards | baseline chores | new card run | new chore run |
|---|---|---|---|---|
| dispatched | 31 | 20 | 1 | 4 |
| needs_fix | 0.323 | 0.0 | 0.0 | 0.0 |
| needs_decision | 0.065 | 0.100 | 0.0 | 0.0 |
| parked | 0.032 | 0.0 | 0.0 | 0.0 (**wrong — 2 of 4 parked**) |
| red_after_merge | 0.0 | 0.0 | 0.0 | 0.0 |

`testing_rejections` reads 4 in every window: it is a repo-cumulative count from its own log,
not a per-run figure, so it cannot be differenced between baseline and tonight. Treat it as a
level, not a rate.

**No item is a revert candidate on this evidence** — nothing moved a counter the wrong way,
the card was reviewed ok and merged, and the chore batch was green over 2604 tests with both
items ok. But the sample is one night, and the counter that would catch phase 2's own main
risk is broken (defect 1), so this is "no evidence of harm", not "no harm".

#### Four measurement defects, each of which blocks a clean re-measure

1. **`parked_rate` is blind to a chore that parks.** `chores.run_one` maps a worker's
   `parked` to state `bounced` on purpose ("a bounce must not be a failure"), and the record
   writes `outcome: "bounced"`; `run_record.quality_counters` counts `outcome == "parked"`.
   Tonight two of four chores parked with a question and the counter read `0.0`. Worse, the
   batch's *own* `parked` state means the opposite thing ("failed its own checks"), so on the
   chore path this rate currently measures close to the inverse of what phase 0.3 wants.
   **Phase 2's stated risk is one-attempt-plus-park-on-doubt, and this is the counter meant to
   catch that regressing.** Fix before any further phase is judged.
2. **`read_share_closeout` is structurally zero** — 0 in 37 of 37 transcripts on disk.
   `transcript_stats` defines close-out as reads with `ts >= last_edit_ts`; a worker commits
   after its last edit and reads nothing, so it is ~always 0. Phase 1's headline evidence
   ("close-out is 25–72% of a worker's cache reads") used §1's definition — from the first
   full-suite run or `post-implementation-cleanup` call to the end — which is a different
   quantity. The number phase 1 exists to drive down is not the number the tool reports.
3. **`costreport <record-stamp>` double-counts transcripts** (see above). The card form and a
   per-attempt walk are sound; the record form is not.
4. **Effort is never recorded, and chore records still carry `cost_usd: 0.0`.** No stage writes
   its `--effort` anywhere, so 2.2 and later 3.3 cannot be verified from a run — only read off
   the code. And the top-level `cost_usd` is 0.0 for a chore batch even where per-stage `usage`
   is complete ($3.54 worker + $3.17 reviewer tonight), so "chores show $0" is half fixed.

**Session C is not started**, per the hand-off. Before it: fix defect 1 at least, then re-run a
night whose cards are first attempts rather than re-attempts — tonight's five were all retries
or parks, which is why the worker deltas above are an upper bound.

### Session B evaluated, 2026-09-17 (Mithlond)

Session B's own measurement was an upper bound: every attempt it compared was a re-attempt
carrying a handover. Since then the merge landed (`bd66aaf`, 2026-09-15 21:19) and **five
first attempts have run post-merge** — two chores (`hacking-line-shortening-should-keep-speed`,
`one-second-left-no-hunt`) and three cards (`per-frame-cost-has-no-enforcement-point`,
`recipe-card-template-unvalidated`, `triage-findings-have-a-shelf-life`). That is the clean
comparison the hand-off asked for: **first attempt against first attempt, same machine.**

Numbers below are a per-attempt walk over `.ai/runs/*/attempt-*/` plus the per-stage `usage`
lists in the records — not `costreport <record>`, which still double-counts (defect 3, still open).
The three late-09-16 cards' attempt dirs were pruned by a later run, so their worker figures
come from the record's `usage` entries, which are per-stage and sound.

| comparison | baseline | new | delta | plan estimate |
|---|---|---|---|---|
| **chore worker**, 1st attempt (9 → 2) | 9.40M cache-read, $3.07 | 2.14M, $0.83 | **−77% / −73%** | phase 2: −50–70% |
| **card worker**, 1st attempt (4 → 3) | 15.81M, $4.68 | 8.24M, $2.85 | **−48% / −39%** | phase 1: −25–40% |
| **card reviewer**, all attempts (3 → 9) | 2.39M, $2.67 | 1.89M, $2.38 | −21% / −11% | phase 4: −20–35% |
| **chore review**, per chore reviewed | $2.79 | $0.79 / $1.05 (two batches) | −62–72% | — |

Both worker targets are met or beaten on first-attempt evidence. The chore number is the
strongest result in the whole plan and it now rests on genuine first attempts, not handovers.

**Caveat on the card row, stated plainly:** the three new cards are AI-team tooling cards
(gates, board, triage) and the baseline four are game-feature cards. The mix is not controlled.
It is *not* the case that the new cards were trivial — their merged diffs are 250/413/686
insertions against the baseline's 93 and 269 — but they touch 3–6 files where
`new-skill-bloodletting` touched 19. Read −48%/−39% as real but not yet replicated on a game
feature card.

**The reviewer.** The nine post-merge reviews are not one population: the review-stage cost
audit (`166ee3c` here + `58f3b79` in nightshift) merged 2026-09-17 10:46, so **eight are pre-fix
and one is post-fix.** Split that way, with average context per turn — the quantity a narrowed
tool list actually moves, and §1's own unit:

| | n | turns | ctx/turn | cost |
|---|---|---|---|---|
| all reviews before the tool fix | 16 | 30.2 | 69.0k | $2.53 |
| after it | 1 | 14.0 | **39.6k** | **$1.21** |
| | | −54% | **−43%** | **−52%** |

**The obvious objection — "it was just a small diff" — is the reverse of the truth.** That
review handled a **29.7 kB** diff. The cheapest pre-fix review on record (`skill-overview-in-run`
attempt 4, $2.25, 35 turns) handled **3.3 kB**. Per kB of diff, pre-fix reviews on comparable
30–40 kB diffs cost $0.065–$0.080/kB; the post-fix one cost **$0.041/kB**.

A tool census of the two makes the mechanism visible. Post-fix: 13 Bash calls, every one a
`sed`/`grep`/`cat`/`git log` over source, plus the verdict write. Pre-fix: 31 Bash + 3 Edit,
of which roughly eight turns are hand-written `python -c` and heredoc scripts computing
bytecode hashes, a re-run of `gates.run` the 4.2 charter change had already told it not to do,
and an excursion into the sibling nightshift checkout.

End-to-end on a first-attempt card, using the post-fix reviewer:

| | worker | reviewer | total | worker share |
|---|---|---|---|---|
| baseline | $4.68 | $0.92 | $5.60 | 84% |
| post-merge, pre-tool-fix | $2.85 | $2.07 | $4.92 | 58% |
| post-merge, post-tool-fix | $2.85 | $1.21 | **$4.06** | 70% |

So the card path is **−27% end-to-end**, not the −12% the pre-fix pool showed, and §1's
"workers are ~75% of spend" still roughly describes it (70/30).

#### Quality counters, with defect 1 fixed

Windows split at the merge. The night `run` record duplicates the per-card records under it in
the pre-merge shape, so cards are counted from `kind: card` before the merge and `kind: run`
after — mixing the two kinds double-counts the 09-13 night.

| | baseline cards (16) | new cards (9) | baseline chores (18) | new chores (7) |
|---|---|---|---|---|
| needs_fix | 0.250 | 0.222 | 0.0 | 0.0 |
| needs_decision | 0.062 | 0.222 | 0.056 | 0.0 |
| parked | 0.062 | 0.0 | **0.0** | **0.429** |
| red_after_merge | 0.0 | 0.0 | 0.0 | 0.0 |

Two counters moved, and neither is a revert signal:

- **`needs_decision` 0.062 → 0.222 on cards** is one card. `triage-findings-have-a-shelf-life`
  went `needs_decision` twice in a row and accounts for both; with it removed the rate is 0.0
  over 7. A hard card, not a phase.
- **`parked` 0.0 → 0.429 on chores** is the counter phase 2's own risk was supposed to trip, so
  it was read rather than waved off. All three parks are correct. Every one is a worker
  refusing to guess a design decision: `icons-for-ice` found Karel's feedback contradicting the
  committed code and said so; `show-weapon-schematic-stats` parked twice because the remaining
  work was "unify with the perk side-panel pattern", a redesign that rewrites two tests. Two of
  the three are the *same* mis-routed card — `show-weapon-schematic-stats` was later reclassified
  `kind: chore` → full card (`6ff32a5`) and then merged on the card path. **The rate is measuring
  a routing mistake, not park-on-doubt firing too early.** Park-on-doubt did the right thing
  three times out of three.

The red-after-merge rate is 0.0 on both sides and the chore batch stayed green, so nothing moved the
wrong way on the axis that matters. Verdict: **no item is a revert candidate, and phases 1, 2,
3.1/3.2, 3.5 and 4.1/4.2 are confirmed shipped and working.**

#### Defect status

| # | defect | status |
|---|---|---|
| 1 | `parked_rate` blind to a chore that parks | **fixed** — `fix/chore-outcome-vocabulary` (`767ad7c`). Records carry `outcome_vocabulary: 2`, `quality_counters` translates a vocabulary-1 `bounced` back, so pre- and post-fix nights are comparable |
| 2 | `read_share_closeout` structurally zero | **open** — still `ts >= last_edit_ts`; 42 of 42 transcripts on disk report 0 |
| 3 | `costreport <record>` double-counts transcripts | **open** — `report_run` still calls `card_transcript_stats(root, card)`, which globs every attempt of that card |
| 4a | effort never recorded | **half fixed** — `fix/record-effort-per-stage` (`398738a`). Chore workers now record `effort: medium`, so **2.2 is confirmed from a record** for the first time. Card workers and *all* reviewers still write `effort: ''` (19 + 17 entries) |
| 4b | chore records carry `cost_usd: 0.0` | **open** — all 5 chores records still read $0.00 while their stages sum to $3.57/$6.35/$6.70/$3.77/$0.43. Card and run records reconcile exactly. The panel reads `cost_usd`, so chores still show $0 |
| 5 | **new** — a recovered worker's usage is dropped | The 09-17 13:02 run records its worker as `turns: 0, cost_usd: 0.0`; the transcript shows 57 turns, 4.77M cache-read, $1.68. the attempt's worker JSON holds a task notification (status "stopped"), not a result — the runner-recovery path (`c643c98`) captured the wrong file. That record's $1.21 should be $2.89 |

#### What this means for session C

C's stated precondition — fix defect 1, then re-measure on first attempts — **is met**. C can start.

Two adjustments to C's scope, earned above:

1. **Finish 4a before judging 3.3.** 3.3 is "effort per tier, explicit", and card workers and
   reviewers record no effort at all. Shipping 3.3 today would be unverifiable from a run for
   exactly the two tiers it changes.
2. **Leave 4.3 in C's tail — do not promote it.** An earlier draft of this section promoted it
   on the grounds that the reviewer was 42% of a card's cost and had barely moved. That was
   measured on a pool that was eight-ninths pre-tool-fix and is now obsolete: the reviewer is
   back to ~30% and moved −52%. The specific waste 4.3 targets — hand-written throwaway render
   scripts — is visible in the *pre*-fix census above and absent from the post-fix one.
   **But it is absent untested:** that review was of a card needing no visual check, so it never
   reached for a render script. 4.3's case is weakened, not disproven. Re-read the census after
   the next review of a UI card before deciding.

Defects 2, 3, 4b and 5 do not block C from starting — they block the *next* clean measurement,
which is C's own evaluation. 5 is the most corrosive of the four: it silently understates a
run's cost rather than reporting zero, so it cannot be spotted by looking for a zero.

### Session C shipped, 2026-09-17 (Mithlond)

Branches: `perf/token-economy-c` (nightshift, merges first) + `karel/token-economy-c` (here).
Scope as §4 defines it — 3.3, 3.4, 4.3, 5.x — with the two adjustments the 09-17 evaluation
earned: finish 4a before 3.3, and leave 4.3 in the tail.

| item | where |
|---|---|
| **4a** effort never recorded | **closed.** `runner.stage_efforts(root, tier, worker=...)` is now the single answer to "what effort does this stage run at", read by *both* the spawn and the record. `dispatch` stamps the map onto every exit path — including the failure paths — so a post-mortem of a card that never landed still says what it ran at. `run_checker`, `review_branch`, `repair_drift` and both merge resolvers take an `effort` and pass `--effort`; before this only `run_producer` did |
| **3.3** effort per tier | `[tiers].effort` in the manifest, `tiers.effort()` to read it, `worker = "medium"` / `lead = "high"` declared here. Stage→tier map: checker and repair take the card's tier (their caller hands them the card's model), reviewer and resolvers take `lead` (they resolve `lead` themselves). `chores.CHORE_EFFORT` demoted to the fallback, as 2.2 said it would be |
| **3.4** CLAUDE.md | 13.1 kB → 8.3 kB (−37%) |
| **3.4** skill descriptions | 5 longest trimmed 2 507 → 1 700 chars, every trigger phrase kept. `install-nightshift` got `disable-model-invocation: true` |
| **5.1** don't babysit a run | `run-the-runner`: launch, background, report once. No polling loop, no tailing `stream.jsonl`, no narrating `--status`. The two one-call exceptions are named so the rule is followable |
| **5.3** classifier/scribe tier | `ingest` was the **only** dispatch path that pinned a model and let effort inherit — so a classifier correctly dropped to sonnet still ran at Karel's own `high`. Both now resolve off `INGEST_TIER` |

**The range is wider than the plan assumed.** `claude --effort` takes five levels —
`low, medium, high, xhigh, max` — so `high` is not the ceiling and `lead = high` is a
middle setting, not a maximum. Worth knowing before anyone tunes 3.3: there is headroom
above the reviewer's current level, and `medium` is one step below the old inherited
default rather than the bottom.

#### Three findings that changed what C did

1. **`[tiers].effort`, not the `tier-binding` block.** The plan said "`[tiers]` gains effort",
   which is right, but the reason matters: §16 reserves that block for the tier→model binding
   and forbids a second home for it. Effort is not a model, so the manifest is free — and
   extending the block's line grammar would have meant touching `init` and `discover` too.
2. **`""` is not a default, and that distinction is the whole of 4a.** An undeclared tier
   resolves to `""`, which means *pass no flag* — inherit the CLI default, the behaviour of
   every stage before 3.3. Giving effort a default value would let a run claim a setting the
   CLI never received, which is 4a wearing the fix's own clothes. Five tests pin it.
3. **The root `CLAUDE.md` was restating `.ai/CLAUDE.md`.** Both load every session, and the
   framework half — preflight, `suite.parallel_args()`, the install line, the branch-role
   rule — was written out twice. That is the plan's own "duplicated" category and it was the
   largest single cut. `MEMORY.md` already carries the rule for itself ("the rules are in
   `.ai/CLAUDE.md`… do not restate them here"); the root file now says the same.

   The memory procedure (the orientation/detail/history table, the what-to-update table, the
   register-never-a-log rule) moved into `MEMORY.md`, which every session reads anyway — so
   it is read once instead of being resident on every turn. The two rules a *dispatched
   worker* needs before it has read anything stayed in `CLAUDE.md`: don't open a companion
   reflexively, and write a fragment rather than the shared log.

   **A stale pointer was found and fixed on the way:** `CLAUDE.md` cited
   `guard-watched-the-wrong-meter` as being in `.ai/corrections.log`. It is not — it was
   retired to `.ai/corrections.archive.log`. A rule whose pointer does not resolve is a rule
   a session is entitled to treat as arbitrary, which is exactly what the file's own header
   forbids.

#### 3.4's target was not reached, and should not be

8.3 kB against the stated ~6 kB. What remains is the localisation rule, the help-catalog
rule, the architecture rules and the assets workflow — project rules with no second home,
each of which the plan's own top rule protects ("cut only work that is duplicated or thrown
away"). Reaching 6 kB means deleting rules. **The 4.8 kB that went was all duplication or
narrative; there is no third category left in that file.**

#### 5.2 does not survive its own measurement — not done, deliberately

5.2 says the four big charters are logs and that the "measured on…/Karel said…" history
should move to `ai_team.md`. Counted:

| charter | bytes | lines | dated lines |
|---|---|---|---|
| `triage.md` | 29 459 | 470 | 6 |
| `code-reviewer.md` | 17 711 | 257 | 4 |
| `scribe.md` | 15 020 | 255 | 1 |
| `classifier.md` | 14 880 | 251 | 7 |

Eighteen dated lines across 77 kB. These are not logs — they are rules, the reasoning that
stops a model getting the rule wrong, and worked examples. `triage.md`'s `verify:` paragraph
is the shape: four lines of rule and four of *why the asymmetry runs that way*, which is the
part that makes it followable rather than guessable. Cutting them would be cutting checks
that catch real defects, which §2's opening rule forbids.

**5.2 was written from the file sizes, not from the files.** It should be replaced with a
narrower item or dropped. The one thing worth keeping from it: `code-reviewer.md` has grown
17.7 kB from the 15 kB the plan recorded four days ago, so the *direction* is worth watching
even though the diagnosis was wrong.

#### The one new rule C introduced, and where it belongs

`CLAUDE.md` gained a normative sentence it did not have: *"Do not restate
[`.ai/CLAUDE.md`] here; a rule written twice is a rule that drifts on one of the two
copies."* Assessed as `rule-scout` requires:

- **Machine-checkable?** In principle — a check could look for the same normative sentence
  in both always-loaded files. In practice it is prose similarity, which this matrix has
  twice classified `hard` for the same reason (`feedback_int_conversion`, row 55): the
  false-positive rate on a pattern match over English is what makes the gate get muted.
- **Does it belong in this project's audit matrix?** No. `.ai/CLAUDE.md` is written by
  `nightshift init` and is identical in every repo that installs the framework, so a rule
  about the boundary between the two files is the framework's, not this game's — the same
  argument the manifest already makes for `comfy_server_teardown` being an infra gate rather
  than a §A row. **If it is ever built, it is built in nightshift**, where `init` and
  `update` already own both files.

No gate stubbed, no matrix row added, on purpose. `MEMORY.md` has stated the same rule for
itself since long before this session and has not drifted, which is weak but real evidence
that the prose rule is holding on its own.

#### 4.3 not started, per the hand-off

Left in the tail exactly as the 09-17 evaluation directed. Its case rests on a tool census of
a *pre*-fix reviewer, and the one post-fix review on record was of a card needing no visual
check, so it never reached for a render script. **Re-read the census after the next review of
a UI card before deciding.** Nothing in C's evidence bears on it either way.

#### What C could not verify, and why

3.3 and 4a are verified from code and from 2 392 framework tests, **not from a run.** Nothing
here has been dispatched yet, so no record carries `effort: high` for a reviewer. The first
night after this merges is C's own evaluation, and the things to read off it are:

- every stage's `effort` is populated — `worker`/`checker`/`repair` at `medium`, `reviewer`
  and any `resolver` at `high`. A `""` anywhere means a stage was missed.
- the reviewer at an explicitly chosen `high` costs what it cost at an inherited `high`;
  this is the tier becoming explicit, not a change of setting, and the numbers should say so.
- the card worker at `medium` against the −48%/−39% first-attempt baseline. **This is the one
  real quality risk in C** — 2.2's evidence is chores, and this extends it to card workers,
  which is what 3.3 itself flagged ("flip the code-card worker only after phase 0 shows no
  quality drop"). Watch `needs_fix` and `parked`; a rise in either is the revert signal, and
  the revert is one line in `.ai/manifest.toml`.

Defects 2, 3, 4b and 5 are all still open and still block a clean measurement.
