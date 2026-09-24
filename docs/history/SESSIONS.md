---
doc_scope: history
---

# AI Team — Session Runbook

**Every session has run. This is now the record of how the system got built**, kept because
the reasoning is the reusable part — the runbooks below say what each session had to decide
and what its derivation got wrong, which is what a future phase reads before adding to the
system. It is no longer a queue: there is no next session to execute.

Still the entry point for a cold session that has to *work on* the framework. Read
`00_architecture.md` §0 and §12 first — the two governing principles — then only the section
you need. Do **not** read every doc; each session names exactly what it touched.

## Status (2026-08-06)

| Session | What | Phase | State |
|---|---|---|---|
| A | Gate suite | 0 | ✅ 11 gates, runner, 2 hooks |
| B | Recipe + step skills | 0 | ✅ recipe + 9 single-concern skills |
| — | First card through the system (`adrenaline-pump`) | 0 | ✅ shipped; ran at the wrong tier — see §16 |
| C | Memory restructure | 0 | ✅ orientation set 344 KB → 45 KB |
| D | Staleness gates + first sweep | 1 | ✅ 3 gates + 1 hook, 200→0 dangling refs, 52 exemptions |
| E | Board & card schema (doc 03) | 2 | ✅ doc 03, 8 lanes, `card_schema` gate, `tier_guard` hook, 4-worker roster, 3 cards migrated |
<!-- stale-ok: a dated session-by-session log. Session F shipped the Obsidian intake and the digest;
     both were removed in 2026-09 (`remove-obsidian`), and a log row that no longer says
     what the session actually shipped is not a log -->
| F | Obsidian intake + digest (doc 05) | 2 | ✅ vault=repo root, board in vault, Bases+Base Board kanban, `reconcile.py`, `triage` charter, `digest.py`, `manage-board` skill |
| G | The runner (doc 09) | 3 | ✅ doc 09, `runner.py` + `board.py` + `tiers.py`, §16 tier block, 72 tests; `unattended:` redefined and the board went 0 → 10 of 11 dispatchable |
| H | Self-improvement loop (doc 10) | 4 | ✅ doc 10, log clustered + re-fielded, 3 gates, appeal process used once, 3 roles ruled on |
| I | Local runtime (doc 04) | 5 | ⏸ **researched and parked 2026-07-30** — costed against real telemetry, then stopped by Karel: *"too much work for too little effect."* Nothing installed, doc 04 deliberately unwritten. Findings in `research_notes.md`; two idea cards carry the work worth doing. See the outcome section below **before** reopening |
| J | Audio generation pipeline (doc 11) | 6 | ✅ **done 2026-07-30, out of order** — pipeline installed, 23 SFX shipped and wired, `audio_hygiene` gate, `audio-asset` skill; `audio` **deliberately unchartered** and doc 11 **deliberately unwritten**, both recorded as findings |
| K | Portability extraction (doc 07) | 7 | ✅ **§8 steps 1–7 done, 2026-07-31 → 2026-08-05.** `nightshift` is an installed package with 19 core gates, 4 hooks, `init`/`doctor`, a generated README and its own gates run against itself. A 2026-08-02 review fixed a `check-stopped-checking` cluster the extraction had opened. Step 7 landed twice over on 2026-08-05: Karel ran a real card through a full cycle on his own second install (three findings this repo could not have produced), and `nightshift-runner-unproven-end-to-end` gave the watched, attributable account of the same cycle. **Step 8 — the colleague handoff — is postponed on purpose** (Karel, 2026-08-06), not outstanding work |

**K deliberately had no runbook, and now has a spec instead.** Every other session's runbook was
written one session ahead, and both I and J then found their inherited blockers already stale — a
runbook written in advance describes a world the intervening session has already changed. So the
2026-07-30 loose-end sweep compiled *what K has to decide* instead of *what K should do*, and K's
design pass then answered those seven decisions against measured coupling rather than against
what the split "should" look like. **`07_portability.md` is the spec — read that, not K's section
below**, which is kept as the record of the questions and of the evidence they were grounded in.

Standing rules for every session:
- **Card execution runs at the worker tier — see `00_architecture.md` §16.** The tier is
  authoritative there; do not restate the model name here, it changes as local runtimes
  land. Dispatch must set it **explicitly**: a subagent with no `model` override silently
  inherits the lead's. Since Session E this is enforced rather than remembered — every card
  carries `tier:` (gate `card_schema`) and `nightshift/hooks/tier_guard.py` refuses an `Agent`
  spawn that names a card path without stating a resolved tier. Beyond cost, this is what
  makes the evaluation honest — a card is
  *worker-ready* only if the worker tier can execute it from the card alone. Run it at the
  lead tier and the lead re-derives what the card failed to say, hiding the gap in the
  card, which is the artefact the recipe system is actually trying to get right.
  **Violated once, 2026-07-22** (adrenaline-pump ran at lead tier) — see §16 enforcement note.
- Work on a branch off the **integration branch**, and merge back to it when done; Karel
  tests there. The branch holding that role is `.ai/manifest.toml`'s `[branches].integration`
  — read it rather than assuming. **It is `test` as of 2026-08-06**, the third and
  deliberately final answer: `dev`, then `development_team` for the AI-team run, now one
  branch named for what it is for. Everything cuts from it and merges back to it, Karel plays
  it whole, and `main` sees it only as a PR. `dev` and `development_team` are both retired and
  both in `forbidden_extra`, so **never commit to either** — `forbidden_bases()` refuses them.
  This line is prose and no import can reach it, so it is on the migration checklist in
  `nightshift/branches.py`'s docstring.
- Do not break the game: the suite must stay green — ~2065 tests as of 2026-08-02. Run it
  as `python -m pytest tests/ -q -n auto --dist loadfile` (~50 s; serial is ~263 s, so
  allow a generous timeout either way). `--dist loadfile` is not optional — the default
  `load` splits a file across workers and the game tests flake. Do not hand-roll those
  flags anywhere in tooling: `nightshift/suite.py::parallel_args()` is the one home for them, and
  gate `pytest_invocation` enforces it.
- Update `.claude/memory/` per `CLAUDE.md` before finishing.

---

## Session A — Build the gate suite

**Why first:** highest value, pays with or without local models, and directly serves the
primary goal (guideline adherence). 76% of this project's rules are currently enforced by
nothing but Claude's memory.

**Read first:** `01_audit_findings.md` (whole thing — it is the spec) and
`02_role_experiment.md` § "Gates this experiment earned".

**Model:** mechanical work — Sonnet is appropriate for most of it.

### Steps

1. **Create `.ai/gates/` and a runner.** `python -m ai.gates.run [--fix] [gate_name…]`,
   exits non-zero on any failure, prints one line per violation as `file:line — rule`.
   Gates are plain Python. **No LLM calls anywhere in this directory** (`00_architecture.md`
   §12). Each gate is importable and independently runnable.

2. **Write the four gates the experiment earned**, in this order:
   - `i18n_parity` — `set(en) == set(cs) == set(es)` over `_STRINGS`. No such test exists
     today; ~1351 keys are currently unguarded except for a handful of hand-listed tuples.
   - `i18n_untranslated` — cs/es value byte-identical to en. **Needs an allowlist**;
     measured 14 cs / 9 es legitimate matches (`skills.xp_format`, `hud.exposure_level`,
     `cheat.*` and other format-only strings). Put the allowlist in a data file, not in code.
   - `i18n_loanwords` — English terms inside cs/es values. **Must exclude `{placeholders}`
     and deliberate keeps (`heat`, es `planta`) or it is ~50% false positives.** Known live
     hit to confirm: `help.desc.shoot` cs says "melee nebo na dálku".
   - `memory_freshness` — diff touches `core/i18n.py` ⇒ diff must touch
     `.claude/memory/ref_i18n.md`. Generalise to the 8 rows of the CLAUDE.md memory table.

3. **Replay each gate against the right corpus.** Read `00_architecture.md` §15 first —
   **replaying against git history is explicitly rejected** and must not be re-proposed.
   Replay against the 12 `.claude/memory/feedback_*.md` files: for each, would this gate
   have caught it? Report the hit rate honestly, including zeros.

4. **Work down the `★` rows in `01_audit_findings.md` §A** — ~24 rules marked
   prose-only + easy. Cheapest first. Stop and report if a gate turns out to need judgment
   after all; that is a finding, not a failure.

5. **Wire the fast gates as hooks** in `.claude/settings.json` (there are currently **no
   hooks configured**). A hook is the only mechanism Claude cannot forget. Use the
   `update-config` skill. Only wire gates that run in well under a second.

6. **Start the correction log** — `.ai/corrections.log`, one line per correction:
   date, rule class, would-a-gate-have-caught-it. Inferred by Claude at review time, Karel
   confirms, manual append allowed. Every metric in §15 depends on this existing.

### Acceptance
- `python -m ai.gates.run` runs clean on current `development_team` HEAD, or every failure
  it reports is a **real** defect (verify each by hand before claiming it).
- Replay hit-rates reported per gate, zeros included.
- At least the four earned gates plus five `★` rows implemented.
- `01_audit_findings.md` §A updated: rows moved from `prose` to `test`.

### Do not
- Do not use an LLM inside a gate.
- Do not "fix" a rule by weakening it until it passes.
- Do not replay against git history (see §15 — censored sample).

---

## Session B — Recipe + step skills

**Why:** turns the topology decision into something runnable, and re-cuts the existing
skills that are secretly role rosters.

**Read first:** `02_role_experiment.md` (whole), `00_architecture.md` §12b and §16,
`01_audit_findings.md` §B and §D.

### Steps

1. **Write `.ai/recipes/add-a-perk.md`** — the **spine only** (§12b). Extract it from
   `02_role_experiment.md` and the shipped `hand_razors` work. Must include the
   *don't-touch* list, not just the to-do list — see the melee-gate constraints in
   `Board/done/hand-razors.md`.

2. **Add the branch table** — "where does the effect live?" pointers: combat path →
   `action_resolver`; hotbar activation → `_fire_perk`; passive → the `perk_levels` check
   at point of use; minigame → the relevant scene. Pointers, not specs. Branches are grown
   by use; do not invent ones nothing has needed yet.

3. **Re-cut `post-implementation-cleanup`.** It bundles **15 distinct jobs**
   (`01_audit_findings.md` §B). Six are machine-checkable → they belong in Session A's gate
   suite, not in a skill. The rest become narrow single-concern skills. **One skill, one
   concern** (§12). Ten single-concern checkers beat one ten-item checker.

4. **Write the sequential-thread step skills** — "how to implement here", "how to write
   tests here", "how to wire UI here". Each carries a **"what the gate already guarantees"**
   section (§12) so the LLM never re-checks what a script proved.

5. **Open problem to solve or explicitly punt:** a skill only helps if it *fires*. Charters
   fire when dispatched, which is still recall-dependent — the same problem one level up.
   Decide how step skills get invoked reliably, or record it as unsolved.

### Acceptance
- `add-a-perk.md` is short enough to read in one pass and contains no hand-razors specifics.
- Every re-cut skill states its one concern in a single sentence.
- The next perk card can be written in under ten lines by pointing at the recipe.

### Do not
- Do not put damage numbers, EP costs, or anything hand-razors-specific in the recipe.
- Do not write branches for work types that have not occurred yet.

---

## Session C — Memory restructure
<!-- stale-ok: this section IS the record of the 2026-03-20 deletion — hack_scene.py,
     hack_generator.py, hack_routing.py, HackScene/HackMap/HackNode/HackParams,
     generate_hack_map, _NODE_R and feedback_hack_routing.md are all named here because
     they are dead. That is the finding, not a defect. -->

**Why:** `state.md` (195.8 KB) + `arch.md` (139.2 KB) are **63%** of the memory tree and are
loaded to orient every session. Pure token win, no design risk.

**Read first:** `01_audit_findings.md` §E only.

### Steps

1. **Split `state.md`.** Lines 1–1291 are orientation; the bulk is per-session narrative
   plus a 254-line condensed history. Keep a small current-state file; move history to
   `state_history.md`, loaded only on demand.
2. **Compress `arch.md`.** 293 lines at **475 bytes/line** — the content is right
   (orientation) but the module-map rows are enormous. Tighten rows; do not delete entries.
3. **Split `ref_i18n.md`** — lines 1–60 orientation, the namespace catalogue and six dated
   pass logs are detail.
4. **Retire dead files:** `feedback_skill_hint_position.md` (already enforced structurally
<!-- stale-ok: names files deleted before or by this audit (feedback_hack_routing.md,
     hack_scene.py, hack_generator.py, hack_routing.py). The audit finding IS that they
     are gone. `conftest.py` and `arch/design/state.md` are shorthand — a file that does
     not exist yet, and the arch/design/state trio. -->
   by `award_xp()`'s signature), `feedback_skill_gating.md` (superseded by
   `feedback_tutorial_visibility_gating.md`), `feedback_hack_routing.md` (revert values from
   one 2026-03 session). Confirm with Karel before deleting.
5. **Fix the split rule set.** Four rules live only in *user-scoped* memory and do not sync
   across machines: cheat spawns use `resolver.give_item()`; menus need [X] + click-outside;
   float→int uses `round()`; i18n completeness. Move them into the repo.
6. **Fix the encoding damage** in `ref_i18n.md` (mojibake: `ÄŒeskÃ½`, `â€”` — UTF-8 read as
   cp1252).

### Acceptance
- `MEMORY.md` + always-loaded files total **under 60 KB**.
- No rule text is lost — only relocated. Verify by diffing the rule list against
  `01_audit_findings.md` §A, which is the canonical inventory of all 55.

### Do not
- Do not delete history; relocate it.
- Do not drop a rule because it looks redundant — check §A first.

---

## Session D — Staleness gates + the first sweep
<!-- stale-ok: this section IS the record of the 2026-03-20 deletion — hack_scene.py,
     hack_generator.py, hack_routing.py, HackScene/HackMap/HackNode/HackParams,
     generate_hack_map, _NODE_R and feedback_hack_routing.md are all named here because
     they are dead. That is the finding, not a defect. -->

**Why:** the memory tree is the cheap orientation layer the whole system rests on
(`00_architecture.md` §2, §16). Measured 2026-07-22, part of it had been **wrong for four
months** and nothing noticed. A confidently-wrong orientation layer is worse than none: it
is read first and trusted.

**Read first:** `08_staleness.md` (whole thing — it is the spec). Skim
`00_architecture.md` §12 and §15. You do **not** need `01_audit_findings.md` for this.

**Model:** Opus for the §5 verdicts and for the 3.1 scope design; Sonnet for the parsers.

**Ordering vs. Session C:** independent, either order. They overlap on exactly one item
(`feedback_hack_routing.md` retirement) — **whichever session reaches it first does it,
and the other checks before repeating.** If C already ran, its `doc_scope:` split makes
step 1 easier; if not, do not wait.

> **Status 2026-07-22: Session C ran first.** `feedback_hack_routing.md` is deleted (Karel
> confirmed) — do not repeat it, and treat `08_staleness.md` §3.3's S3 orphan example as
> already resolved. C also shipped the `doc_scope:` frontmatter convention on the new
> `state_history.md` / `arch_detail.md` / `ref_i18n_catalog.md` / `ref_i18n_history.md`
> companions, so step 1's history problem is partly solved before you start: the bulk of
> `state.md`'s dated narrative now lives behind `doc_scope: history` rather than inline.
> `ref_minigame.md` was **not** split or corrected — the §9 backlog there is untouched and
> still yours.

### Steps

1. **`doc_reference_liveness`** (`08_staleness.md` §3.1) — paths + backticked symbols
   across `.claude/memory/`, `.ai/recipes/`, `Board/tasks/`, `.claude/plans/`,
   `CLAUDE.md`, resolved against an AST symbol table of `project_tigress/`.
   **Solve the history problem first, before writing the parser** — `doc_scope:`
   frontmatter + `<!-- stale-ok: reason -->` markers. A per-symbol allowlist is rejected
   (§3.1); if you find yourself starting one, stop and re-read that section. A gate that
   flags `state.md`'s correct history lines will be muted inside a week.

2. **Record the baseline before fixing anything** — the dangling-reference count at current
   HEAD. There is no before/after without it. One line in `.ai/corrections.log`.

3. **`deletion_sweep`** (§3.4) — on a diff deleting a file under `project_tigress/` or removing
   a top-level `class`/`def`, grep the doc scope for the dead names. **This is the
   highest-value gate in the spec** (§1d) — it fires while the person who caused the
   staleness is still in the room. Wire it as a hook; everything else in §3 is cleanup for
   staleness that already escaped.

4. **`doc_signature_drift`** (§3.2) — fenced `def …(` blocks vs. the real AST. Parameter
   **names and count only**, in order; types explicitly out of scope. Known live hit to
   confirm: `ref_minigame.md`'s `on_complete` documented with 4 params against the actual 7
   at `hack_scene_grid.py:883`.

<!-- stale-ok: steps 5-8 name the symbols the sweep was sent to hunt (HackNode, HackMap,
     NodeType) and gates that are specced but not built (entrypoint_smoke). Neither can
     resolve against project_tigress/ by construction — that is what makes them the work. -->
5. **Clear the §9 backlog** — the verified-stale items from the 2026-07-22 incident. Apply
   the §5 verdict table per finding; `hack_node.py` is the deliberate partial case (the file and
   TWO enums are live — `LootKind`/`SecurityKind`; `HackNode`, `HackMap` **and `NodeType`**
   are dead, corrected 2026-07-22) — **correct that section, do not
   delete it.** That is the shape most findings take.

6. **Replay for the real metric** (§8) — staleness half-life on the one datapoint we have
   (`e2e2f51` 2026-03-20 → 2026-07-22 = 124 days). Note in Session A's replay table that
   `memory_freshness` is a confirmed dated hit on that commit — and note alongside it that
   it is co-change, not accuracy (§1c).

7. **`stale-hunter` charter** (§4) — write it, do not run a tree-wide pass yet. One file per
   call, quote-or-drop, cites `file:line`, never edits. Run it on **two** files to sanity-check
   the output schema, then stop. Tier 2 only runs on files Tier 1 already passed.

8. **`entrypoint_smoke`** (§6) — boot each `main_*.py` headless with `SDL_VIDEODRIVER=dummy`,
   drive a scripted sequence **including the quit path**, assert clean exit.
   **Retired 2026-07-30, not deferred.** The one subject was the standalone launcher; Karel
   ruled it deleted rather than repaired and that is now executed, so `main.py` is the only
   `main_*.py` in the tree and `GameApp.run()` never reaches the frozen-loop path. Skip this
   step. Revive it only if a second entry point is ever added, and build it against a
   synthetic frozen-loop fixture even then.

### Acceptance
- Baseline dangling-reference count recorded **before** any cleanup.
- `doc_reference_liveness` runs clean, and every remaining exemption carries a written
  reason (`doc_scope:` or a `stale-ok:` marker) — count them; that number is the debt.
- `deletion_sweep` wired as a hook and demonstrated failing on a synthetic deletion.
- §9 backlog empty, each item resolved to one of the four §5 verdicts, verdict recorded.
- `stale-hunter` charter written; **not** run tree-wide.

### Do not
- Do not delete a memory file without Karel's confirmation (§10).
- Do not build a per-symbol allowlist (§3.1).
- Do not have the Tier 2 checker sweep more than one file per call (§4).
- Do not "fix" a history section — relocate it (§5).
- Do not claim `memory_freshness` as staleness coverage (§1c).

---

# Sessions F–K

**E–I are done or are runbooks** (each written by the session before it). **J and K are
still sketches**: they record intent and the decisions already made, so nothing is lost, but
each gets written out properly by the session before it. Writing them in detail now would be
guessing.

---

## Session E — Board & card schema (doc 03) — ✅ done 2026-07-22

**Spec: `03_board.md`.** Read that, not this.

Shipped: eight lanes under `Board/` (`00_architecture.md` §5's five-lane sketch is
superseded by §13's eight — reconciled in `03_board.md` §1); the frontmatter schema with
its author-owned / runner-owned split; `nightshift/gates/card_schema.py` enforcing it; the three
existing cards migrated; the `plan-doc-extraction` card written to falsify the schema
against non-perk work; a four-worker roster in `.claude/agents/` with a `README.md`.

§16's tier enforcement now has two of its three landing places: `tier:` in frontmatter
(gate-enforced) and `nightshift/hooks/tier_guard.py`, a `PreToolUse` hook that refuses an `Agent`
spawn naming a card path without a stated tier. The third — the dispatcher's `tier: →
model` lookup — is Session G's.

Two roster decisions worth knowing before you dispatch anything:

- **`closer` was not chartered.** Applying §16's own test (short payload or briefing?) to
  the spine tail says briefing, so the tail stays inside the code thread as the existing
  `post-implementation-cleanup` skill. Reversible if the tail turns out to get skipped
  anyway. Reasoning in `03_board.md` §5.
- **`audio` was not chartered** — no pipeline exists, and a charter for an undispatchable
  role is the thing Session E was told not to produce.

Two backlog items were converted into a card rather than left as prose, and the prose is
gone accordingly: the decision to **keep-extract-then-delete** the 11 pre-AI-team plan docs
(Karel, 2026-07-22 — never delete one ad hoc; they are pointer targets and the work-type-#2
recipe has not been extracted from them yet), and the note that **`.ai/recipes/` still holds
only one recipe** while work type #2 has shipped five times. Both lived in
[[plan-doc-extraction]], including the unverified bit about the meta-menu
plan's decisions possibly never reaching `design.md`.

**All of it executed 2026-07-24.** The unverified bit was true — save location, audio-global,
Quick-Game-unsaved, profile-delete-confirm and stats-by-stable-id had reached
`arch.md`/`arch_detail.md` as *mechanisms* but never `design.md` as *decisions*; they are now
a "Persistence & profiles" section there. `.ai/recipes/ship-a-subsystem.md` carries work
type #2's spine, and the 11 plan docs are gone.

---

## Session F — Obsidian intake and the morning digest (doc 05)

**Why now:** the board exists and is empty except for three hand-written cards. Intake is
what fills it, and the digest is what makes a night's work legible in the morning. They are
two halves of the same interface — the whole overnight ask is worthless if the report is
unreadable, so do not split them across sessions.

**Read first:** `03_board.md` (whole — it is short and it is the contract you are feeding),
`00_architecture.md` §13 and §16. Skim `.claude/agents/README.md`. You do **not** need
doc 01 or doc 08.

**Model:** triage is judgment work and runs at the **lead** tier (§16). The digest generator
is deterministic Python — no LLM anywhere in it, same rule as the gates (§12).

**Inputs already settled — do not re-litigate:**
- Triage output is a card in `ideas/`, `tasks/`, or a question in `needs-decision/` —
  **never a silent guess.** Parking is a success state (§13).
- Cards carry `tier:` and the dispatcher resolves it. Triage writes the field; it never
  names a model.
- Art and audio cards are `unattended: false` permanently. The digest is designed around
  that (`03_board.md` §5), not as a gap to close later.

### Steps
<!-- stale-ok: `.claude/agents/triage.md` is Session F's DELIVERABLE. A runbook names what
     the session will create; it not existing yet is the reason the session exists. -->

1. **Settle the intake source with Karel — ask, do not assume.** ✅ **Answered
   2026-07-22, and the answer replaced the question.** Karel did not want intake bent
   around the folder layout that happened to exist; he restructured the vault instead.
   The Obsidian vault root is now the **repo root**, the board is `Board/`, and the old
   `Fatures/` + `Progress/` notes were moved by hand into `Board/ideas/`.

   So there is no note-parser to write and no discrimination problem to solve. Intake
   reads exactly one lane, `Board/inbox/`, and the lane a note sits in *is* the signal:

   - `Board/ideas/` — Karel's, private, **never enumerated by any script or agent**.
     Enforced structurally: neither `card_schema` nor `doc_scan` globs it.
   - `Board/inbox/` — "I want this, help me refine it." Triage's only input.
   - `Design/` (the old `Done/`) — design specs for shipped features, kept because four
     plan docs point at them. Not backlog, never mined.

   Notes are in **Czech**; cards are written in **English**.

2. **Write the triage charter** — `.claude/agents/triage.md`, `tier: lead`. Its one
   concern: turn one rough note into one well-formed card, or into a well-formed question.
   It must state the dedupe rule (see step 3) and it must not implement anything.

3. **Solve dedupe before writing the charter, not after.** A note re-read next week must not
   produce a second card, and `Done/` must not be re-mined. This is the step that decides
   whether intake can run unattended at all. Options to weigh and record: a `source:` field
   on the card pointing back at the note (needs a schema change and therefore a `03_board.md`
   edit + a `card_schema` rule), a content hash ledger, or Karel moving notes between folders
   as the signal. Prefer whichever survives Karel editing a note in place.

4. **Run triage on real notes and count.** Whatever Karel has moved into `Board/inbox/`.
   Report honestly how many produced a clean `tasks/` card, how many stayed in `inbox/`
   refined but not yet actionable, how many landed in `needs-decision/`, and how many
   produced nothing. A high `needs-decision/` count is a **good** result on the first
   pass; a high `tasks/` count on ambiguous input is the bad one.

   Expect a low `tasks/` count. The eleven notes now in `ideas/` are feature *areas* —
   `Nové mapy.md` is seven bullets that are one epic, `Úklid.md` is seven bullets that
   are seven unrelated jobs — so granularity is a judgment call per note. Default to one
   card per note; split only when distinct acceptance criteria can be named per bullet,
   and record the reason on the card.

5. **The morning digest.** A deterministic board scan — every number in it must be a
   file-state lookup, because that is what makes it trustworthy at 7 AM. It leads with
   `needs-decision/`, because that is the only queue Karel can unblock and unblocking it is
   what makes the *next* night productive. Then `testing/` (what he can play), then
   `review/`, then failures with their last error. Art and audio land in a "look at this"
   section by construction, never in "done".

6. **Decide where the digest is emitted** and record it: a file in the repo, a terminal
   command, or something pushed to his phone. Ask; do not build three.

### Acceptance
<!-- stale-ok: `.claude/agents/triage.md` is Session F's DELIVERABLE — see the Steps note. -->
- Karel's answer on the intake source is recorded in doc 05 as a settled decision.
- `.claude/agents/triage.md` exists, states one concern in one sentence, and passes
  `card_schema`'s worker resolution (i.e. a card may name `worker: triage`).
- Every card triage produced passes `python -m nightshift.gates.run card_schema` with no hand edits.
  A card the schema rejects is a finding about the charter, not about the schema.
- The dedupe rule is written down and demonstrated: running intake twice over the same
  notes produces zero new cards the second time.
- The digest generator runs on the current board and its output fits on one screen.
- Counts from step 4 reported, zeros included.

### Do not
- Do not let triage write code, or resolve an ambiguity by inventing an answer.
- Do not put an LLM call in the digest generator.
- Do not add a frontmatter field without adding the matching `card_schema` rule and the
  `03_board.md` row — a field nobody reads is a field the runner drops silently.
- Do not mine `Design/` or `Board/ideas/`. The first is the shipped-feature design record;
  the second is Karel's private lane and no script may enumerate it.

---

## Session G — The runner (doc 09) — ✅ done 2026-07-23

**Spec: `09_runner.md`.** Read that, not this.

Shipped: `nightshift/runner.py` (preflight → lock → reconcile → recover → select → dispatch →
settle → digest), `nightshift/board.py` (the card model), `nightshift/tiers.py`, `.ai/hosts.json`, 72 tests in
`tests/test_board_runner.py`.

§16's tier enforcement now has all three landing places. The third one is not a dict:
`nightshift/tiers.py` **parses §16**, which grew a fenced ` ```tier-binding ` block for the
purpose. A constant in Python would have been a second home for the binding, and §16 says
its table is the only one — Session I edits that block and nothing else.

Four things worth knowing before you touch this:

- **`unattended:` was redefined mid-session, and it is the finding that matters.** The
  first `--dry-run` showed 10 cards in `tasks/` and **0 dispatchable**. Session G reported
  that as a triage problem and deferred it; **Karel rejected that reading the same day** —
  *"there is a lot of others I would expect to run, and it is OK if the results need to be
  manually verified."* He was right. §2's old wording ("every acceptance criterion is
  machine-checkable") disqualified anything with a visible surface, and it duplicated the
  job of the lanes: `review/` and `testing/` are **downstream** of the runner, so an
  overnight card is seen twice before it reaches `dev`. Now it means *a machine can tell a
  failed attempt from a finished one*, default `true` on code cards. Board went 0 → 4.
  Reasoning in `03_board.md` §2 and `09_runner.md` §8; procedure in `.claude/agents/triage.md`.
<!-- stale-ok: `bypassPermissions` is a `claude --permission-mode` argument value, not a
     symbol in this codebase; it resolves against the CLI, which is out of the tree. -->
- **`.ai/hosts.json` gates whether anything works at all.** Committed, keyed by hostname.
  `permission_mode` must be `bypassPermissions`; the default cannot run Bash, so pytest and
  git are unavailable and a code card burns an attempt doing nothing. An unlisted machine
  gets no capabilities, so `requires:` cards stay put and cost nothing.
<!-- /stale-ok -->
- **No scheduler.** Karel runs it by hand (2026-07-23). The Task Scheduler script was
  written and then deleted rather than left unused; git has it if that reverses.
- **No paid dispatch has been run.** The cycle is proven end to end through the
  `_run_worker` seam; the literal CLI spawn is not. First real use should be
  `--max-cards 1`.

---

## Session H — Self-improvement loop (doc 10) — ✅ done 2026-07-23

**Spec: `10_self_improvement.md`.** Read that, not this.

Shipped: the correction log clustered for the first time (58 entries) and re-fielded so it
*stays* clusterable; `nightshift/corrections.py` as its reader; three gates — `corrections_log`,
`subprocess_result_checked`, `gate_appeals`; the gate-appeal process, written down **and
used once the day it was written**; `.ai/recipes/verify-before-shipping-a-rule.md`; verdicts
on all three §14 maintenance roles. 31 tests in `tests/test_self_improvement.py`. Gate suite
is 19 gates, 0 violations.

Five things worth knowing before you touch this:

- **`derived-not-verified` is the largest defect class — 12 of the first 50 entries, and 7
  of those found by Karel.** Rules, definitions, schemas and orderings reasoned out
  carefully, written down confidently, wrong the first time reality touched them. Not one
  is a coding mistake, which is why no gate sees them. Session G's guess ("is
  *deferring-instead-of-fixing* a cluster?") was right that there was a cluster and wrong
  about which one: deferral is 2, and it is a symptom of this. The answer is a procedure,
  not a gate — `.ai/recipes/verify-before-shipping-a-rule.md`, doc 10 §3.
- **Gates found 4 of 50 corrections, three of them on day one.** Sessions E, F and G
  produced zero gate-channel entries, and **no correction anywhere was caught by a gate
  that already existed at the time**. That is the honest state of the suite: it is very good
  at the class it was built for and has not yet caught anything it was not.
- **The log has recorded no game-code defect since 2026-07-21.** Sessions C–H were all
  meta-work, so every gate earned from this corpus is a gate on the machinery. The next
  session that writes game code is the first real test of the loop.
- **Appeal debt is 3, and it is a number that is meant to be looked at.** L1 exemptions
  (`# gate-ok(<gate>): reason`) Claude may add; L2 narrowing and L3 retirement are Karel's.
  Editing a rule's prose until the code complies is on none of the three lists.
- **The orientation memory set is 102 KB against the 60 KB budget the log proposed.** The
  gate was *deliberately not built* — red on arrival by 42 KB, and the only routes to green
  were out-of-scope compression or a budget picked to fit, which is the move §4 exists to
  stop. [[orientation-size-budget]] does the compression first. En route it
  turned out Session C's headline "45 KB" measured three files while `CLAUDE.md` names four.

---

## Session I — Local runtime bring-up (doc 04)

**Deliberately late.** `00_architecture.md` §9 for why, including the risk this ordering
accepts (cloud rates through the most token-hungry phase). Phases 0–4 are banked; this one
is the only phase with hardware risk, and it is the one that can fail and cost nothing.

**Read first:** `research_notes.md` (whole — it is the starting point and it names what to
re-verify), `00_architecture.md` §3, §6, §7 and §16, and `09_runner.md` §7 (host
capabilities and `_run_worker`, which is the seam you are re-pointing). Skim
`10_self_improvement.md` §5 for how a gate is meant to be earned. You do **not** need docs
01, 02, 03 or 08.

**Model:** design, the eval harness and every promote/demote call are **lead tier**. The
harness itself is deterministic Python.

**Facts age.** `research_notes.md` was gathered 2026-07-21 and says so; re-verify the
endpoint contract, the model shortlist and the tool-calling numbers before building on
them. Do not start from zero either — the note exists so this is not re-researched.

### Two blockers to clear before anything else

1. **`.ai/hosts.json` has no desktop row.** It is keyed `_TODO-desktop-hostname` because
   Session G inferred the wrong machine from `00_architecture.md` §6 and Karel caught it
   (`wrong-machine-got-the-gpu`). Run `python -c "import socket; print(socket.gethostname())"`
   **on the box**, fill in the row, and confirm the value with Karel rather than deriving
   it. Until then art dispatches nowhere and neither will anything local.
2. **No paid dispatch has ever been run.** The cycle is proven through the `_run_worker`
   seam in tests; the literal CLI spawn is not. Do `--card <id> --max-cards 1` on a cloud
   worker **before** introducing a local one, or a local-runtime bug and a
   never-exercised-dispatch bug will be indistinguishable.

### Steps

1. **Write doc 04** as you go, not at the end: install, endpoint, the routing table, the
   GPU lock, the eval harness. It is the spec for a thing with hardware in it, so it is
   worth as little as it is inaccurate.

2. **Bring the endpoint up and prove the contract, not the install.** llama.cpp / Ollama /
   LM Studio serve `/v1/messages` natively (`research_notes.md` — verify this is still
   true). The check that matters is that a `.claude/agents/` charter runs **unchanged**
   against it, because §3's whole claim is that the runtime is a config toggle rather than
   a second code path. If a charter needs editing to work locally, that claim is false and
   that is the session's most important finding.

3. **The GPU lock.** 12 GB will not hold ComfyUI and a coder model at once
   (`00_architecture.md` §6). The runner already holds a single-instance lock; art and code
   must interleave, never overlap. Note the shape this wants: `requires: gpu-box` today
   means *"this machine has the stack"*, and it is about to also mean *"the GPU is free
   right now"* — two different questions on one slug. Karel's rule applies
   (`declarative-because-of-initiative`): a check that **bounds** behaviour is declarative,
   a check that **discovers** a fact may probe. "Is the endpoint up" is discovery, so probe
   it. "May this machine run local inference" is bounding, so declare it in `hosts.json`.

4. **The golden set — and its size is a problem, so measure it first.** §7 says nothing goes
   local without one, and §9 promised that by now "the golden set writes itself" from the
   Phase 2–4 card corpus. **The corpus is 3 cards in `done/`** (`adrenaline-pump`,
   `hand-razors`, `menu-visual-redesign`) plus 11 in `tasks/`. That is short of the 15–30 §7
   asks for, and doc 10 §1 explains why it will not grow on its own: the log has recorded no
   game-code work in days. **Report the real number before scoring anything**, and if it is
   still under ten, say so rather than scoring a model on a corpus too small to mean
   anything. Padding it with synthesised tasks is worse than a small honest set — a golden
   set of invented tasks measures the inventor.

5. **Score the checkers first, not the coders.** §12's refinement is explicit and it inverts
   the obvious order: a narrow checklist with schema-constrained output is the profile local
   models hit at 90%+, and the ~18 judgment rules that cannot be gated are their primary job
   — *not* code writing. `art-reviewer` and `stale-hunter` are the two real Tier 2 checkers
   on the roster, both already producing structured verdicts, and `stale-hunter` has a
   known-answer fixture from Session D (5 findings, 5 true, 0 false positives) which is the
   nearest thing to a golden set that exists. Start there.

6. **Rebind the tier — in exactly one place.** `00_architecture.md` §16's fenced
   ` ```tier-binding ` block is what `nightshift/tiers.py` parses. Edit that block and nothing
   else. A test already asserts no other file under `.ai/` names a model alias on a line
   that also names a tier, so a second home will fail rather than drift.

7. **Promotion and demotion are measured, never argued.** §7: a role goes local only when it
   clears a threshold on its own golden set, and is demoted automatically when its live
   accept-rate drops below it. Write the demotion path in the same session as the promotion
   path — a system that can only promote is one that silently degrades.

8. **Write Session J's runbook** before finishing. Same obligation every session has carried.

### Acceptance
- Doc 04 written, with the measured numbers in it rather than the researched ones.
- `.ai/hosts.json` names the real desktop hostname, confirmed by Karel.
- One card dispatched end to end on **cloud** Claude before any local dispatch, reported.
- An unmodified charter runs against the local endpoint, or the reason it cannot is the
  session's headline finding.
- Golden-set size reported honestly, including if it is too small to score on.
- Per-tier scores for 3–4 candidates, zeros included, on tasks from **this** repo.
- §16's `tier-binding` block is the only place the binding changed. Verified by the test.
- `python -m pytest tests/ -q` green; `python -m nightshift.gates.run` clean.

### Do not
- Do not benchmark from published numbers (§7). They do not predict behaviour here, and
  `research_notes.md` says multiple sources say so.
- Do not put the tier→model binding anywhere but §16's block (`00_architecture.md` §16,
  and `tier-binding-would-have-been-a-second-home` in the log for what the alternative costs).
- Do not route code writing local first. §12's refinement says checkers; the naive order is
  named as the mistake.
- Do not synthesise golden-set tasks to reach 15. Report the small number.
- Do not let "local inference is free" become a reason to run cards above their declared
  tier — §16 is explicit that the worker tier is the *measuring instrument* for card
  quality, and that argument gets stronger, not weaker, once tokens stop costing.

### Outcome — researched and parked, 2026-07-30

**Karel stopped it after the research pass**: *"If coding is out of question, then this whole
exercise stops being important… Too much work for too little effect. Creating assets or story
is much better spend of resources right now."* Nothing was installed. The research is written
up in `research_notes.md` under "Local coding/checker models" — **read that, not this, before
reopening**, and do not re-research it.

**Both blockers above were already stale, and that is a finding about this runbook.** The
desktop row landed with Session J, and paid dispatch had already run four times. The telemetry
was sitting in the runner's per-run directories the whole time. A runbook written one session
ahead describes a world that the intervening session has already changed; the first act of any
session should be to verify its own blockers rather than trust them.

**The step that mattered was never in the plan: measure the existing runs.** Four real cards
cost $15.41 total, and the breakdown decides the whole phase — cost tracks *cumulative prefix*
almost linearly (7.1M cache-read tokens on one card, against 82 uncached input tokens), context
per card varies 5× (40k → 214k), and a reviewer call cost 16% of the card it reviewed. Prompt
caching is exactly what local inference lacks, so the number cloud bills at 0.1× is the number
local must recompute. **That single fact, not any benchmark, is what rules out the coder tier
on 12 GB.**

Three things worth knowing if this reopens:

- **"Coding is out" is true of the hardware, not of the idea.** The cheapest measured card
  needed only 40k of context and would fit a 24 GB card today. Full coverage of all four wants
  48 GB (2× 3090, ~$1,800–2,300, payback ~7–9 months at five nights a week). The number that
  actually decides it is the quality gap — SWE-bench 59.2 for the leading local candidate
  against Sonnet-class ~70+ — because a lower first-pass rate spends attempts on retries.
- **TurboQuant is not available.** ~15 pull requests into llama.cpp, every one closed, none
  merged; the feature request is still open. Do not plan around it.
- **§12's "checkers, not coders" refinement is right on feasibility and wrong on savings.**
  Checking is ~16% of run spend, so the checker tier cannot pay for itself on tokens. Justify
  it on "the sweep happens at all" — a tree-wide staleness pass and the 52-exemption review
  are jobs that currently do not happen — or do not do it.

**Session J's runbook was already written and executed out of order**, so the "write the next
session's runbook" obligation lands on K, whose gap Session J already flagged. This session
adds nothing to that: it built no pipeline, so it has no new evidence about which parts of the
shape are structural.

---

## Session J — Audio generation pipeline (doc 11) — ✅ done 2026-07-30

**The card already exists**: two notes in `Board/ideas/` — *Implement sound generation
pipeline* and Karel's Czech checklist *Generování zvuku*. (Named in italics rather than as
backticked paths because both filenames contain spaces, which `doc_reference_liveness`
splits on; they are real files, and this is a gate limitation, not a dangling reference.)
Both are intake, not a spec — the session starts by triaging them into real cards, and the
second carries decisions the first does not: CZ/ES dubbing, and that the narrator is the
Net Tiger, a synthetic female voice.

**Why it sits here and not earlier.** `03_board.md` §5 lists audio as the one roster row
deliberately left unchartered: *"There is no audio pipeline in this repo — no generator, no
conventions, no output layout. A charter would describe a job nobody can dispatch."* This
session is what makes that row writable. It is also the last thing that reshapes the
portability seams, which is why it must precede K rather than follow it — see below.

**Why immediately after I, specifically.** Session I stands up local generative
infrastructure on the GPU box and builds the eval harness that scores it. Audio needs the
same box, contends for the same 12 GB against ComfyUI (§6), and has the same "does this
output pass anything?" problem. Doing them adjacently means the GPU lock, the host
capability plumbing and the golden-set discipline are built once. Doing audio *before* I
would build a second, parallel version of all three.

### The three use cases are not one pipeline

Karel's card names three, and they are not variations of a theme — different models,
different lengths, different acceptance, and only the third touches i18n:

| Use case | Shape | The hard part |
|---|---|---|
| **Soundtrack** | minutes-long, looping, per-situation (menu, mission, fight, vault, hacking) | seamless loops and tonal consistency across a set, not single-track quality |
| **SFX** | sub-second, dozens of them, must sit in an existing mix | consistency with the ~existing sounds, and level-matching — `audio_volume` is already a gate |
| **Narrator** | voiced lines, **×3 languages** | it is the only one that is *content*, not decoration |

**The narrator is the one to be careful with.** It has a hard upstream dependency Karel
already named — *"we need to refine her personality first"* — and it is the only audio work
that multiplies by the i18n rule: every line needs en/cs/es, which turns each script change
into three regenerations and puts it in `translator`'s territory as well as audio's. Treat
it as a **separate work type from soundtrack and SFX**, and consider splitting it out of
this session entirely if the first two fill it.

### Steps (sketch — Session I writes the real runbook)

1. **Research, and write it into `research_notes.md`** — the same obligation that file was
   created for. State-of-the-art local audio models for a 12 GB RTX 3060, per use case.
   Music generation and TTS are different model families; do not assume one stack serves both.
2. **Triage the two intake notes into cards.** Three use cases probably means three cards.
3. **Architecture, mirroring the art pipeline** — Karel's own framing, *"similar to picture
   generation"*, and the right instinct: `art` + `art-reviewer` + `asset_hygiene` +
   `pixelart-asset` is a working four-part shape. Reuse the shape, not the contents.
4. **Install on the GPU box.** `requires: gpu-box` already exists and already works; this
   needs no new mechanism, only a capability name.
5. **The gate first, then the charter.** §1: no gate, no delegation.
   <!-- stale-ok: `audio_hygiene` is the gate this session is being sent to write. A
        forward reference in a runbook is not staleness; naming it is the instruction. -->
   `audio_hygiene` is the mechanical half — sample rate, channels, peak level, format,
   length, lands in `assets/audio/` and not `sources/`. `audio_volume` already exists and
   already checks part of this; extend it rather than starting a second gate.
6. **The `audio` charter** — and per `03_board.md` §5's rule, **it must name its checker
   boundary or record that it has none.** Art learned this the expensive way: rule 1
   (context overlap) correctly said "don't split" three times, and missed that a producer
   must not judge its own output. An `audio-reviewer` that receives the clip and the
   criteria but never the prompt is the obvious counterpart — but "can a blind listener
   judge this from criteria alone?" is a genuine open question for music in a way it is not
   for a 32×32 sprite. Answer it in the session; do not assume symmetry with art.
7. **One card end to end**, on the box, producing one real asset that ships.
8. **Write Session K's runbook** before finishing.

### Acceptance
- Doc 11 written; research in `research_notes.md`, not only in the session.
- `audio` chartered, with its checker boundary named or its absence justified.
- A mechanical gate exists and fails on a deliberately bad clip.
- One audio asset generated on the box, reviewed, merged, and audible in the game.
- The `03_board.md` §5 roster row for audio updated from "deliberately unchartered".

### Do not
- Do not charter `audio` before the pipeline exists — that is the exact failure §5 named.
- Do not split audio into soundtrack/SFX/narrator **workers**. Same tooling, same output
  conventions, same box: one worker, several recipes (§16's overlap rule). The narrator may
  earn a separate *work type*, which is not the same thing as a separate worker.
- Do not let the narrator's personality question block soundtrack and SFX. It is a design
  decision of Karel's, and `needs-decision/` is where it belongs if it is not settled.
- Do not generate voiced lines in one language and plan to add the others later. The i18n
  rule is en/cs/es together, and audio makes retrofitting expensive rather than tedious.

---

### Outcome — done 2026-07-30, and out of the planned order

**It ran ahead of Session I, which the sequencing argument above says it should not have.**
That argument was that audio should *inherit* the GPU lock and eval harness from Phase 5
rather than invent them. What actually happened is that Karel asked for sounds, the stack got
installed, and the work went through in one interactive stretch — so the inheritance ran the
other way: the GPU box entry (`hosts.json` names the desktop, capability `gpu-box`) and a second generative
stack on the same 12 GB now exist as facts Session I can measure instead of design. The
sequencing argument was not wrong, it was just overtaken.

**The three use cases did not turn out to be one pipeline, exactly as predicted — and only
one of them is done.** SFX shipped: 23 clips generated, post-processed, level-matched by ear,
wired through `_SFX_FILES` with a synth fallback for every name, and gated by
`audio_hygiene`. Music and narrator are untouched, and Karel's framing is why —
*"there will be a lot of sound generation, but this is future work, not sound generation
wiring."* Both are in `Board/inbox/` ([[soundtrack-generation]]), not in `tasks/`.

**The headline finding is the one this session was told not to assume.** Step 6 asked whether
a blind listener can judge a clip from criteria the way `art-reviewer` judges a sprite, and
said explicitly *"do not assume symmetry with art"*. **The answer is no**, with four rounds of
Karel's feedback as evidence — three of the four are about a clip's fit with something outside
the clip (the firing code's burst interval, the death animation's frame count, the synth cue
it replaced). Written up in `03_board.md` §5. Consequences:

- **No `audio-reviewer` — but Karel's correction (2026-07-30, same day): that does not mean
  no `audio` worker.** No fitness reviewer means the generate→judge→route loop art runs is
  missing its middle step, not that the whole loop is unattended-incompatible. The worker
  still runs unattended and still gates on `audio_hygiene`; it just hard-routes to
  `needs-decision/` every time rather than only when a reviewer is unsure, so he accepts or
  returns each clip. Not yet built — `03_board.md` §5 records the shape, and building it is
  carded ([[soundtrack-generation]]), not attempted in this pass. Until it exists, generation
  still runs interactively through `.claude/skills/audio-asset/SKILL.md`.
- **The route from ear to gate runs through Karel articulating a rule**, not through a
  reviewer inferring one. One of the four did mechanise once he generalised it —
  *"sounds and animations should go hand in hand"* became a budget derived from
  `death_animations._SHEETS × ANIMATION_FRAME_S` and a test.

**Two acceptance criteria were not met as written, both deliberately, both stated rather than
quietly dropped.** `audio` is unchartered (the acceptance allows "or its absence justified",
and the justification is above). Doc 11 was not written: its content has three homes that are
each already the right one — the skill for how to run it, `~/.claude/audiogen-stack.md` for
what is installed, `research_notes.md` §Audio for why these models — and `feedback_doc_leanness`
plus `tier-binding-would-have-been-a-second-home` both say a fourth copy is worse than none.

**Session K's runbook was not written either**, and unlike the two above that is a real gap
rather than a decision: K's premise is that a *third* pipeline shows which parts of the shape
are structural, and audio has now half-built that third pipeline in a way that argues against
K's own reasoning — it produced a skill and a gate but no worker and no reviewer, so the
"four-part shape" K expects to generalise from is two-part here. That is material K needs and
this session is not the place to decide it.

---

## Session K — Portability extraction (doc 07)

**Spec: `07_portability.md` (written 2026-07-30). Read that.** It answers D1–D7 below and
supersedes §8's two-directory sketch: the split is **config-level with the mechanism in its own
installable repo**, because §8's "a new project copies `.ai/core/`" fails both of Karel's use
cases — a copy cannot merge, and a copy has no version a colleague can be behind. The rest of
this section is the record of the questions, kept because the evidence in it is what the answers
were grounded in.

Original framing, superseded: split `.ai/core/` (generic) from `.ai/project/`
(Project Tigress-specific) per §8, write the setup manual, prove it by standing the core up in an
empty repo.

**Do this last on purpose.** Extracting a framework before it has run one full cycle
produces an abstraction shaped by guesses. After Phases 0–6 the seams are known.

**Audio is why this moved from J to K.** With code and art there are two pipelines, and two
points do not show you where the generic/project line runs — anything true of both looks
like a rule. A third pipeline built on the same shape is the first real test of which parts
of that shape are structural and which were coincidences of the first two. Extracting
before it exists would produce a `.ai/core/` that fits code and art and needs reshaping the
moment audio lands, which is precisely the guess-shaped abstraction this session is
sequenced last to avoid.

---

### What K has to decide — **not a runbook** (compiled 2026-07-30)

Karel's call: the loose-end sweep before K lists the open questions rather than writing K's
runbook, because these are the calls K exists to make and pre-answering them here would be the
guess-shaped abstraction Phase 7 is sequenced last to avoid. Each item below is grounded in
something measured in this tree, not in what the split "should" look like. **Every other
session's runbook was written one session ahead and both I and J found their inherited
blockers already stale** (§Session I's outcome) — so this list is deliberately evidence, not
instructions.

**D1 — K's own premise is contradicted by its third data point, and the resolution is narrower
than J feared.** Phase 7's argument is that audio, as the third pipeline, shows which parts of
the four-part shape (producer + checker + gate + skill) are structural. Audio came out
**two-part**: the `audio-asset` skill and the `audio_hygiene` gate, with **no worker and no
reviewer**, because Session J's headline finding is that a blind listener *cannot* judge a clip
from written criteria — three of Karel's four feedback rounds were about a clip's fit with code
or animation, not with the clip (`03_board.md` §5). So the producer/checker seam is **not**
universal. But the seam's *machinery* already is: `art` appears nowhere in the runner's logic,
`harvest_dirs` points at the CLAUDE.md-wide `.tmp/` convention, and a regression test locks
that generality. **The decision is therefore not "is the seam generic" but "does `core/` ship
the seam with `has a checker` as project configuration, or ship only the gate+skill spine and
let each project add the loop?"** J flagged this as material K needs; this is the material.

**D2 — file-level split or config-level split.** §8 draws the line as two *directories*
(`.ai/core/` portable, `.ai/project/` Project Tigress-only). Every candidate file measured against
that is ~90% generic mechanism wrapped around a small hardcoded project table:

| File | Generic part | Project table inside it |
|---|---|---|
| `suite.py` | selection as a union of affected parts, the JUnit verdict, `parallel_args` | which path prefix maps to which part (`project_tigress/`→game, `.ai/`→system, `Board/` lanes→board) and the per-part test globs |
| `gates/doc_scan.py` | reference extraction, `doc_scope`, gitignore-awareness | `SOURCE_DIRS = ("project_tigress", "tests", "tools", ".ai")`, `DOC_FILES = ("CLAUDE.md",)` |
| `board.py` | frontmatter read/write, move+commit, `_git_commit`'s empty-index handling | `Board/` and the eight lane names |
| `branches.py` | `forbidden_bases()`, the role indirection | the literal branch names + the migration checklist |
| `runner.py` | every phase | `harvest_dirs`, `FENCE_ENV`'s three roots |

A directory split forks each of these; a config split keeps one copy reading a project
manifest. §8 says directories and predates the evidence. **This is the load-bearing decision of
the session** — pick it first, because every other answer follows from it.

**D3 — the gate split is already drawn and already machine-readable.** `audit.py::_INFRA_GATES`
names **7 of 29** gates as being about `.ai/` itself rather than about `project_tigress/`, each with a
written reason, and `--check` already treats them as out of §A's scope. That is exactly the
core/project line for gates, derived bottom-up over four sessions rather than designed.
Decide whether to *promote* it to the mechanism (core ships those 7 + the runner; project ships
the other 22) or whether core ships a gate runner with zero gates. Do not re-derive it.

**D4 — what does `core/` ship as documentation, and how is it kept single-sourced?** The sweep
before this session found the same fact written in two prose homes twice over: the worker roster
(`.claude/agents/README.md` and `03_board.md` §5 — `code-reviewer` was missing from both for six
days, and the audio row said opposite things in each), and the ★ backlog (§A's markers vs §D's
verdicts disagreed for a week, and `audit.py` reported the wrong number the whole time because
it counts the markers). Extraction multiplies that: a second project gets a *copy*, and a copy
of a duplicated fact drifts twice as fast. Decide the rule — one home per fact, generated where
a number is involved — before copying any doc.

**D5 — annotate the charters, which §8 already requires and none of them does.** §8: *"Charters
state explicitly which of the two they belong to."* All seven lack it. On a first read
`triage`, `code-reviewer` and `stale-hunter` are generic and `code-thread`, `translator`, `art`
and `art-reviewer` are not — but `triage` names `Board/` lanes and `code-reviewer` names the
integration branch, so decide what "generic" means for a charter: no project nouns at all, or
project nouns supplied by the manifest of D2.

**D6 — what counts as proof.** §8's acceptance is "stand the core up in an empty repo". Define
it as a bar rather than a demo: gates run, a card moves inbox→tasks→review→testing, the runner
refuses correctly on a forbidden base, and a preflight passes — all with zero Project Tigress files
present. Note the one thing that already works in your favour: `suite.select` returns **NONE**
for a diff that touches no code part, and both run sites treat an empty arg list as a judged
pass without running, so an empty repo with no test suite is a case the tooling already handles
rather than one K has to invent.

**D7 — Karel's, and it gates the whole session: is there a second project?** Extraction pays
only against one. The Session I ruling on local models is the precedent — *"too much work for
too little effect; creating assets or story is much better spend of resources right now"* — and
it applies here on the same axis. Four measured cards cost $15.41 and the framework's value to
*this* repo is already banked. If the answer is "not yet", the honest version of K is a setup
manual plus the D5 annotations, deferring the directory surgery until a second project exists to
shape it.

**One gate now exists for `core/`'s own set, and it is the third member of an existing
family.** `write_newline` (built 2026-07-30) — no text-mode write under `.ai/` may leave
newline translation unpinned. Joins `subprocess_result_checked` and `subprocess_encoding`,
which are the same rule shape: *a stdlib default that silently corrupts on this platform.*
Like both, it is an `_INFRA_GATES` row — about the orchestrator, not about `project_tigress/` — so
**D3 puts it in the set that ships to a second project**, and it must be, because
`line_endings` (the project-facing rule, §A row 62) travels with `core/` too and a copied core
without its producer-side gate ships a known tripwire on the platform most likely to trip it.

**Writing it paid for itself before it ran once, and that is the argument for a gate over a
test.** The first fix shipped as an AST test over the five modules that own a committed
artefact. The gate — which scans `.ai/` instead of consulting a list — immediately found
**two more tracked-file writers the list had missed**, both in `runner.py` and both on the path
<!-- stale-ok: the digest and its generated report were removed in 2026-09 (`remove-obsidian`); this
     is the dated account of a bug they were involved in, kept as written because rewriting
     the names out of it would make the record describe something that never happened -->
that actually runs at night: the runner writes `Digest.md` itself in the work root, and the
staleness sweep writes a new `Board/tasks/fix-stale-*.md` card. Seven sites, not five. A list
<!-- /stale-ok -->

of writers is exactly as complete as whoever last remembered to extend it. The superseded test
is gone rather than kept alongside — a second home for one rule is the D4 defect.

A second gate was considered and **withdrawn**: a ★-vs-§D consistency check over
`01_audit_findings.md`. It fails the §15 test that gate selection follows observed failures — one
occurrence, a marker set with two rows left in it, and §D has not been appended to since
2026-07-24, so it would guard a live table against a frozen record. The defect's real cause is
that ★ duplicates what the `C` column already says, which makes it a **D4 instance, not a gate**:
single-source it and the class disappears. `C` is free text today, so that is real work.
