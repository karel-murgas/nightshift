# AI Developer Team — Big-Picture Architecture

Status: **built.** Phases 0–7 shipped between 2026-07-21 and 2026-08-06; the framework is
the installed `nightshift` package and this repo is its origin consumer. `SESSIONS.md`
holds the per-session state. This is the stable top-level doc, and §16's `tier-binding`
block is **live** — `nightshift.tiers` parses it via `[tiers].binding_doc`, so it is code
in prose clothing rather than a record.

Read the sections below as *why the system is shaped this way*, not as a proposal. Where a
section still describes an intention rather than a shipped thing it says so locally (§8 is
the flagged one — its two-directory sketch is superseded by `07_portability.md`).
Detail lives in sibling numbered docs, each written in its own session.

---

## 0. Goal ordering (Karel, 2026-07-21)

1. **Consistent adherence to project guidelines** — the primary goal.
2. Sustainable quality as scope grows.
3. Token savings / local offload — a bonus, not a driver.

Why roles solve #1: today every project rule is prose in `CLAUDE.md` that Claude must
recall *in full, every session*. Adherence is therefore inconsistent **by construction** —
it scales inversely with how many rules exist, and the rule set only grows. Roles fix this
on two axes:

- a **narrow charter** is loaded only when relevant, so it is short enough to actually
  follow end-to-end;
- anything expressible as a **gate** stops depending on recall at all.

Corollary: the value of this overhaul does **not** depend on local models. Phase 0 is
justified on adherence alone. Local models are strictly additive.

**Nothing is grandfathered.** The existing skills (`post-implementation-cleanup`,
`localization-naturalness`, `pixelart-asset`), the `.claude/memory/` tree, and `CLAUDE.md`
itself are *inputs* to the redesign, not fixed points. They are evidence of what work
recurs in this project; they may be re-cut freely. Example: `post-implementation-cleanup`
currently bundles ~5 distinct jobs (bug audit, help-catalog freshness, UI consistency,
i18n completeness, memory docs) — that is a role roster hiding inside one skill.

Sole hard constraint: **do not break the game.** The refactor is of the development
environment, not of `project_tigress/`. Game-code changes made by this system go through the
normal gates + review path like any other work.

---

## 1. The reframe

Karel's spec organises workers by **topic** (coding, testing, docs, assets, translation).
That is the wrong primary axis. The correct primary axis is **verifiability**.

Measured reality for the target hardware (Ryzen 5 5600X / RTX 3060 12 GB / 32 GB DDR4):

- 30B-class MoE coder models (Qwen3-Coder-30B-A3B, Q4_K_M) reach **~12–15 tok/s** with
  CPU offload. Dense 7–9B models fully in VRAM reach **~35–40 tok/s**.
- Tool-calling reliability for the best local models in 2026: **90%+ on single calls,
  80–90% end-to-end on multi-step workflows**. Q4_K_M is the quantisation floor —
  Q3/Q2 degrade tool-calling before they degrade chat.
- 7B is the practical floor for any tool use at all.

Consequence: a local worker will silently produce wrong output **1 task in 5 to 10**.
That is *fine* — but only if a machine can catch it. So:

> **A role may be delegated to a local model only if its output has a deterministic,
> automated pass/fail gate. No gate → no delegation. The gate is the deliverable,
> not the worker.**

This inverts the build order. We build **gates first**, workers second.

## 2. Where the token savings actually come from

Not from "the model is local". Local generation is free, but if Claude still reads
every result, nothing is saved. Real savings, in order of size:

1. **Gates reject bad work before Claude sees it.** Claude reads a summary of 10
   accepted diffs, not 40 attempts.
2. **Summaries and structured reports instead of raw output.** Workers return JSON
   against a schema; Claude reads the schema, not the transcript.
3. **A machine-readable code index + tighter memory**, so Claude stops re-reading the
   codebase every session. (`.claude/memory/` is currently **545 KB** — too big to be
   a cheap orientation layer; it needs an index/detail split.)

## 3. The keystone: one worker contract, two runtimes

Since Jan 2026, llama.cpp / Ollama / LM Studio natively serve the **Anthropic Messages
API**. Claude Code can be pointed at them via `ANTHROPIC_BASE_URL` +
`ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`.

Therefore a worker is defined **exactly once**, as a Claude Code artefact:

```
.claude/agents/<role>.md      # charter: scope, tools, output schema, refusal rules
.claude/skills/<role>/        # procedure: the steps, the references, the examples
.ai/gates/<role>.py           # the machine gate that decides accept/reject
```

The *same* definition runs on:

- **Cloud Claude** — interactive work, hard tasks, review, anything without a gate.
- **Local model** — headless overnight batches, gated tasks only.

This satisfies "must work without local models" for free: the fallback isn't a
separate code path, it's the *same* path with a different endpoint. It also satisfies
portability — the three files above are the entire unit of transfer.

## 4. Layers

```
┌─ Human (Karel) ──────────────────────────────────────────────┐
│  intake: notes → task cards.  review: morning digest.        │
└──────────────────────────────────────────────────────────────┘
┌─ Claude — lead dev / architect ──────────────────────────────┐
│  decomposes work into task cards, sets acceptance criteria,  │
│  reviews digests + exceptions, improves gates and charters.  │
└──────────────────────────────────────────────────────────────┘
┌─ Orchestrator (dumb, deterministic, Python) ─────────────────┐
│  file-based task board, worktree per task, retry policy,     │
│  model routing, resume-after-reboot, digest generation.      │
│  Contains NO judgment. Never calls an LLM to decide.         │
└──────────────────────────────────────────────────────────────┘
┌─ Workers ────────────────────────────────────────────────────┐
│  local model  |  cloud Claude subagent  |  non-LLM (ComfyUI) │
└──────────────────────────────────────────────────────────────┘
┌─ Gates (deterministic Python) ───────────────────────────────┐
│  pytest · i18n completeness · help-catalog overflow ·        │
│  import-layering · animation-speed guard · asset validators  │
└──────────────────────────────────────────────────────────────┘
```

The orchestrator being *dumb* is a hard rule. Every "should we retry / is this good /
which worker" decision is either a config table or Claude's, never an inline LLM call.
That is what makes 3 AM behaviour predictable.

## 5. Durability: the task board

Windows reboots at 3 AM. So there is **no daemon holding state**.

- A task is a file: `Board/{inbox,active,review,done,failed}/<id>.md`
  with YAML frontmatter (role, inputs, acceptance criteria, attempts, worktree, branch).
- State transitions are **file moves + a git commit**. Crash-safe by construction.
- The runner is a loop that on start scans the board and resumes. Started by Task
  Scheduler on boot; if it dies, the next tick picks up where it stopped.
- Each task runs in its own **git worktree on its own branch**. Nothing touches `dev`
  until a human or Claude merges.

## 6. Model tiering on the one box

The RTX 3060 already runs ComfyUI. 12 GB will not hold image generation and a coder
model at once — **VRAM contention is a real scheduling constraint the spec missed.**
The orchestrator holds an exclusive lock per GPU consumer; art batches and code batches
interleave, never overlap.

| Tier | Model class | Speed | Used for |
|---|---|---|---|
| A — worker | Qwen3-Coder-30B-A3B class, Q4_K_M, MoE + CPU offload | ~12–15 tok/s | code edits, tests, transforms |
| B — grunt | dense 7–9B, fully in VRAM | ~35–40 tok/s | triage, classification, summarising, extraction |
| C — art | ComfyUI (existing) | — | sprites, icons (already working) |
| D — lead | cloud Claude | — | design, review, gate authoring, hard bugs |

Exact model picks are **not fixed here** — they are chosen by the eval harness (§7)
against Karel's own tasks, and re-run whenever a better model appears. Swapping models
must be a config edit, never a rewrite. That is the answer to "survive changing tech".

## 7. Nothing goes local without a golden set

Before any role is routed to a local model, we build a **golden set of 15–30 real past
tasks from this repo** with known-good outcomes, and score candidate models on it.
Published benchmarks do not predict behaviour on this codebase. A role is promoted to
local only when it clears a measured threshold on its own golden set, and is demoted
automatically if its live accept-rate drops below it.

## 8. Portability split

```
.ai/core/      # portable: orchestrator, board schema, gate runner, generic charters
.ai/project/   # Project Tigress-only: gates, style refs, role roster, model routing table
```

A new project copies `.ai/core/`, writes its own `.ai/project/`. Charters state
explicitly which of the two they belong to.

---

## 9. Phases

**Reordered 2026-07-22 (Karel).** The original list put local-runtime bring-up at Phase 1.
It now sits second-to-last, immediately before extraction. Karel's framing: *"do all the
reliable, sustainable, self-cleaning, self-improving parts first and add local models just
before the make-it-portable part."*

The reasoning that makes this safe rather than merely cautious:

- **Nothing above local runtime depends on it.** §3's whole point is that a worker is
  defined once and the runtime is a config toggle. So local models are a *cost* change, not
  a *capability* change — and every phase below is specified in capabilities.
- **The headline feature does not need them.** The overnight ask (§13) — *"go through the
  backlog and implement as many features as you can"* — runs unattended on cloud Claude
  today. Bring-up would buy a cheaper night, not a night that was otherwise impossible.
- **Bring-up is the only phase with hardware risk**, shared-GPU contention, and a
  hard dependency on a golden set (§7) that cannot be written until the roles it scores
  actually exist and have been run enough to know what good output looks like. Doing it
  early means guessing at the eval.
- **It is the one phase that can fail and cost nothing.** If the box turns out to be too
  slow, phases 0–4 are already banked.

The corresponding risk, stated so it is not discovered later: **token spend stays at cloud
rates through Phase 4, and Phase 4 is the most token-hungry** (overnight batches). If cost
bites before then, Phase 5 can be pulled forward — it is genuinely independent, which is
the same property that lets it be late.

### Phase 0 — Foundations (no local models involved) — **largely done**
Everything here pays off with Claude alone, and is a prerequisite for everything else.
- ✅ Gate suite — 11 gates in `.ai/gates/`, runner at `python -m nightshift.gates.run`, two
  `PostToolUse` hooks wired (Session A, 2026-07-21).
- ✅ Recipe + single-concern step skills — `.ai/recipes/add-a-perk.md`, nine skills
  (Session B, 2026-07-21).
- ✅ First card executed through the system — `adrenaline-pump` (2026-07-22).
- 🔄 Memory restructure — Session C, in progress.
- ⬜ Worker contract formalised (charter + skill + gate triple) — partial: skills and gates
  exist, charters do not.
- ⬜ Task board card schema — two ad-hoc cards exist, no defined fields or state machine.
- **Deliverable: a measurably tighter Claude-only workflow.** If Phase 0 doesn't
  improve things on its own, the rest won't save it.

### Phase 1 — Self-cleaning (Session D)
- Staleness gates: `doc_reference_liveness`, `deletion_sweep`, `doc_signature_drift`
  (`08_staleness.md`).
- First sweep; the §9 backlog cleared.
- **Deliverable: the orientation layer is trustworthy, and stops silently rotting.**
  This is a prerequisite for autonomy, not a nicety — an unattended run plans against
  memory, and §0 of `08_staleness.md` is the argument for why a wrong memory tree is worse
  than none.

### Phase 2 — The board (doc 03) and intake (doc 05)
- Card schema, state machine, branch policy, retry rules.
- Obsidian → `ideas/` → triage → `tasks/` → `needs-decision/` → `to-test/` → `done/`.
- Still driven by hand. **No runner yet.**
- **Deliverable: work exists as durable artefacts rather than as conversation.**

### Phase 3 — Overnight autonomy (cloud Claude)
- Dumb runner: rescan-on-boot, file moves, git commits, zero LLM calls (§5).
- Task Scheduler, retry/dispatch-order, morning digest.
- Only card types with a proven gate run unattended; everything else parks in
  `needs-decision/`, which is a success state (§13).
- **Deliverable: wake up to reviewable work.** Note this lands *without* local models.

### Phase 4 — Self-improvement — ✅ **done (Session H, 2026-07-23; doc 10)**
- Correction log → gate improvements → recipe and charter amendments, from the log rather
  than from raw transcripts (§14, §15). Log clustered, re-fielded so it stays clusterable,
  and read by `nightshift/corrections.py`.
- Three gates earned from observed clusters; one cluster (the **largest**) refused a gate
  and produced a recipe instead — `derived-not-verified`, doc 10 §3.
- The gate-appeal process (doc 10 §4): how a gate is *legitimately* relaxed, with a counted
  debt number, used once on a real gate the day it was written.
- §14's three maintenance roles ruled on: **none chartered.** All three fail §16's overlap
  test for one reason — maintenance compares an artefact against the whole context that
  produced it, which is the opposite of the producer/checker seam. Doc 10 §6.
- **Deliverable: the system's own defect stream drives its next gate.** Met, with a stated
  limit: the log has recorded no *game-code* defect since 2026-07-21, so every gate earned
  so far is a gate on the machinery. The first session that writes game code is the real
  test of the loop.

### Phase 5 — Local runtime bring-up
- Install llama.cpp/Ollama on the box, expose Anthropic-compatible endpoint on the LAN.
- Build the golden-set eval harness; score 3–4 candidate models per tier. **By now the
  golden set writes itself** — it is the accumulated card corpus from Phases 2–4.
- GPU lock shared with ComfyUI.
- Rebind the worker tier in §16's table — the single place the binding lives.
- **Deliverable: a measured table of which roles a local model can actually do.**

### Phase 6 — Audio generation pipeline
- The third generative pipeline, after code and art. Local models on the GPU box, sharing
  the lock ComfyUI already contends for (§6).
- Three use cases, three shapes: soundtrack, SFX, narrator. Only the narrator touches i18n,
  and it is the only one that is content rather than decoration.
- Closes the one roster row `03_board.md` §5 left deliberately unchartered, on the grounds
  that a charter for a pipeline that does not exist describes a job nobody can dispatch.
- **Deliverable: audio stops being the thing the system cannot help with.**

### Phase 7 — Extraction
- Extract `.ai/core/` as a reusable template + setup manual (§8, doc 07).
- **Why after audio, not before.** Two pipelines cannot show you where the generic /
  project-specific line runs — anything true of both reads as a rule. The third is the first
  real test of which parts of the shape are structural and which were coincidences of code
  and art. Extracting first produces a core that needs reshaping the moment audio lands.

---

## 10. Follow-up planning docs (each its own session)

Deliberately separate so no single session carries the whole design.

| # | Doc | Status | Depends on | Phase |
|---|---|---|---|---|
| 01 | Role roster & verifiability audit — every candidate role scored on gate strength, volume, blast radius | ✅ written | — | 0 |
| 02 | Role experiment record — what the four-role `hand_razors` run actually did | ✅ written | 01 | 0 |
| 03 | Task board & card schema — frontmatter fields, state machine, worktree/branch policy, retry rules | ✅ written | — | 2 |
| 04 | Local runtime spec — install, endpoint, model routing table, GPU lock, eval harness + golden set | ✅ written 2026-09-17 (Session I reopened) | 03, corpus from 2–4 | **5** |
| 05 | Human I/O — Obsidian intake, morning digest format, escalation rules | ✅ written | 03 | 2 |
| 06 | Memory & code-index restructure | ✅ Session C | — | 0 |
| 07 | Portability extraction & setup manual | ⬜ | all | **7** |
| 08 | **Staleness — detection, review, cleanup** (written 2026-07-22). Added to the roster after the fact: a measured 4-month-old stale-doc incident showed no existing doc owned this process (§14b) | ✅ written | 01 | 1 |
| 09 | Runner spec — rescan-on-boot, retry/dispatch-order, digest, kill-switch | ✅ written | 03, 05 | 3 |
| 10 | **Self-improvement loop** — correction-log clustering, gate selection from observed failures, the gate-appeal process, verdicts on §14's roles | ✅ written | 09 | 4 |
| 11 | **Audio generation pipeline** — soundtrack / SFX / narrator; models, gate, charter, checker boundary. Intake already on the board, in `Board/ideas/` | ⬜ | 04 (shares the GPU box and the eval harness) | 6 |

Doc 02 as originally listed ("gate suite spec") was absorbed into 01 §A, which is the real
spec; the number was reused for the experiment record. Noted so the mismatch is not read
as a missing doc.

**04 is the only unwritten doc left**, and it is deliberately last but one (§9). Everything
between here and unattended operation is written and shipped; what 04 buys is a cheaper
night, not a night that was otherwise impossible.

---

## 11. Decisions (settled 2026-07-21)

**Autonomy — two-stage acceptance, batched onto a session branch.**

```
worker → task branch → GATES (machine, no Claude) ──fail──→ retry / failed/ + log
                            │
                            pass
                            ↓
                     Claude review (diff only, depth ∝ blast radius)
                            │
                            ok
                            ↓
                    merge → ai/session-<date>  ← Karel tests the batch
                            │
                     post-implementation-cleanup skill
                            ↓
                           dev
```

Load-bearing rule: **Claude's review is second, never first.** Gate-failed work is never
read by Claude — only counted in the digest. This is what keeps review affordable; it is
also the existing Karel↔Claude workflow with the mechanical pass already done.

The session branch is a real artefact: accumulates several finished tasks so Karel tests
features together rather than one at a time.

**First local role:** deferred to doc 01's verifiability scoring — no pre-commitment.

**Board home:** in the game repo under `.ai/`. Git-tracked, syncs across machines.

**GPU box availability: intermittent** (overnight, sometimes remote, sometimes off).
This is a first-class case, not an edge case:

- The runner **health-checks the endpoint** before claiming a task.
- Endpoint down → tasks stay in `inbox/`; the board is a queue, not a schedule.
- A card may declare `fallback: claude` — those escalate to a cloud subagent instead of
  parking. Same charter, same gates, different runtime (§3).
- Nothing in the design may assume the box is up. Karel working from a laptop with the
  box off must be a fully functional mode.

---

## 12. Governing principle (Karel, 2026-07-21)

> **What Python can do, Python should do. Use an LLM only where it adds value.**

This supersedes and sharpens §1. Applied at every layer:

- A rule that a script can check is **never** a charter bullet — it is a gate.
- The orchestrator makes zero LLM calls.
- A role's charter should shrink over time as its checkable parts migrate into its gate.
  **A charter that isn't shrinking is a smell.**
- Deterministic beats probabilistic even when the LLM would be "good enough", because
  deterministic is free, instant, and doesn't drift.

Consequence for role design: every charter carries an explicit
**"what the gate already guarantees"** section, so the LLM is never asked to re-check what
a script proved. That section is what keeps charters short enough to be followed.

### Refinement (Karel, 2026-07-21): narrow LLM checkers are a first-class tier

"Python-first" does **not** mean "never LLM". It means *never LLM for what is
deterministic*. For genuine judgment, a **narrow** checker with an explicit short list
beats a broad one — because context blurring is precisely why 55 prose rules in one
`CLAUDE.md` fail today. Three tiers:

| Tier | Mechanism | Use for | Cost |
|---|---|---|---|
| 1 | **Deterministic gate** (Python) | anything syntactic/structural | free, instant, no drift |
| 2 | **Narrow LLM checker** — one role, one short checklist, structured output | judgment rules: "is this Czech natural", "does this FX read as lying-still", "is this string user-visible" | cheap; reliable *because* narrow |
| 3 | **Claude review** | what neither caught; cross-cutting design judgment | expensive — spend deliberately |

**Rule: one checker, one concern.** Ten single-concern checkers beat one ten-item checker,
even at ten times the calls — the calls are cheap and the reliability difference is large.
Never merge two checkers to save a call.

**This is where local models fit best.** A narrow checklist with schema-constrained output
is the exact task profile local models handle at 90%+ (see `research_notes.md`). The ~18
judgment rules that cannot be gated are the local workers' primary job — *not* code
writing. This inverts the naive assumption that local models are for "the coding".

## 12b. Recipe anatomy — spine, body, branches (agreed 2026-07-21)

A recipe has three parts, and only the first is guaranteed to pay.

**Spine — invariant, complete on day 1.** True for *every* task of this type regardless of
what it does. For `add-a-perk`: `PerkDef` in `catalog.py` with `len(prices) == max_level`;
name/desc keys in **all three** language dicts; help-catalog entry; shop picks it up from
`CATALOG` automatically; `test_perks_catalog` validates it; `state.md` updated; icon
optional.

**Body — the actual effect wiring.** Different every time and **deliberately not covered**.

**Branches — pointers, not specs.** A short "where does the effect live?" table that
shortens discovery without pre-solving: combat path → `action_resolver`; hotbar activation
→ `_fire_perk`; passive bonus → the `perk_levels` check at point of use; minigame → the
relevant scene. A task type with no branch yet **writes one** — the first hack-minigame
perk records where the scene hook is and how reveal timing works, and the second gets it
free. This is `recipe-keeper`'s job (§14) and is the concrete form of "self-improving".

### Why the spine is the valuable part

> **The spine is what gets forgotten. The body is what gets enjoyed.**

Nobody forgets to implement the effect — that is the interesting part and the reason the
task exists. What slips is the third language, the help entry, the memory update, the
gating check. **Karel's `feedback_*.md` files are almost entirely spine failures, not
effect failures.** So a recipe earns its keep on the boring half; the judgment half stays
with Claude and Karel.

**Honest caveat:** with ~16 perks that each differ substantially, the branch library may
never get dense enough to pay for itself. Expect the spine to pay and branches to be
marginal. Do not let branch-building block shipping.

### Trajectory: recipe steps must become gates

Every spine step should migrate from "a step you must remember" to "a gate that catches
you". "i18n in three languages" is *already* a test rather than a remembered step; "must
have a help entry" could be. **A recipe that is not shrinking means automation has
stalled** — same principle as shrinking charters (§12).

## 13. Intake and task lifecycle

Karel's intake is **rough ideas, dumped freely** (Obsidian). Refinement is the system's
job, not his. Lanes:

> **Superseded in part, 2026-07-22 (Session F).** The board lives at
> `Board/`, and `ideas/` now means Karel's *private pre-triage* lane,
> not a triage output. `03_board.md` §1 is authoritative on both points.

```
ideas/       ← Karel's private scratch. never read by any script.
  ↓ Karel moves it by hand
inbox/       ← raw dumps. no schema. Karel writes here, or a note is ingested.
  ↓ triage role + Claude
tasks/       ← actionable card: role, inputs, acceptance criteria, recipe
  ↓ worker
needs-decision/ ← BLOCKED ON A HUMAN ANSWER. see below.
  ↓ Karel answers → back to tasks/
review/      ← gates passed, awaiting Claude review
  ↓
testing/     ← merged to the session branch, awaiting Karel
  ↓
done/  |  failed/
```

### `needs-decision/` is a success state, not a failure

Karel, verbatim: *"even returning the issue with 'I need more info' is a good result."*

This is a hard design constraint, not a nicety:

- A worker that parks a task with a **well-formed question** has succeeded. It counts as
  a completed unit of work in the digest, not as a failure.
- The runner **prefers parking over guessing.** Ambiguity resolved by invention is the
  single worst overnight outcome — it produces confident wrong work that costs more to
  review than to redo.
- A parked card must carry: what was attempted, what is ambiguous, the candidate answers,
  and what each would imply. "I need more info" alone is a failure; a decision Karel can
  make in 15 seconds from his phone is the deliverable.
- The morning digest leads with `needs-decision/`, because that is the queue only Karel
  can unblock, and unblocking it is what makes the *next* night productive.

### The overnight ask

Target instruction: *"go through the backlog and implement as many features as you can
before the session limit runs out."* Note this is bounded by the **Claude session limit**,
not by local capacity — which reframes local models: their job is to absorb gated grunt
work so the Claude budget stretches further across more features.

## 14. Self-maintenance roles

Karel: *"the system needs to maintain itself."* The lists (rules, gates, recipes, roles)
must not rot. At least three maintenance roles, all scheduled, all reading **logs** rather
than transcripts:

| Role | Reads | Produces | Cadence |
|---|---|---|---|
| **rule-scout** | diffs of `CLAUDE.md`, `.claude/memory/`, and Karel's corrections | new rules detected; each classified checkable/not; a gate stub for the checkable ones | on memory-file change |
| **gate-smith** | the rule matrix + rejection log | promotes `prose → gate`; retires gates that never fire *and* have no historical catch; widens gates that miss | weekly |
| **recipe-keeper** | git history vs. the recipe set | new work-type detected → new recipe; recipe steps that are always skipped → removed; recipes that drift from what commits actually touch → corrected | monthly |
| **stale-hunter** | one memory/recipe/plan doc + the current source of the modules it names | per-claim true/false/unverifiable verdicts with `file:line` evidence; orphan-file candidates | on removal (hook) + monthly, change-ordered — see `08_staleness.md` §7 |

Each obeys §12: they *propose* deterministic checks, and Claude approves. They do not
themselves become the judgment layer.

> **Verdicts, 2026-07-23 (Session H, doc 10 §6). This table describes the *work*; only one
> of the four is an agent.** Each was run through §16's two split rules before chartering,
> the way Session E ran them on `closer`:
>
> | Role | Verdict | Why |
> |---|---|---|
> | `rule-scout` | **skill, not agent** | its input looks like a payload and is a briefing; and the diff is already in the lead's context, because the lead just wrote it |
> | `gate-smith` | **not chartered** | largest briefing on the roster, and it would grade gates the lead wrote — an agent reviewing its own side of the seam. Session H *was* `gate-smith`, at the lead tier |
> | `recipe-keeper` | **not chartered** | corpus of two recipes. Revisit at ≥3 recipes plus a work type shipped ≥5 times |
> | `stale-hunter` | **chartered** ✅ | the only one whose judgment needs the artefact and the criteria rather than the context that produced them |
>
> The pattern is the finding, and it constrains any *future* maintenance role: all three
> refusals fail for one reason — **maintenance work is defined by comparing an artefact
> against the whole context it came from**, which is the exact opposite of §16's second
> seam, where the checker is valuable *because* it is denied that context. This table was
> written before that seam existed and had not been re-derived against it since.
>
> What survives is the mechanical half, and it now exists: `python -m nightshift.corrections`
> produces the cluster tables, and `nightshift/gates/gate_appeals.py` the exemption debt. Those
> are the inputs a `gate-smith` would have re-derived by reading (§12).

### 14b. Staleness is the maintenance case the other three miss

The first three roles all watch **new** work. None asks whether what we already wrote is
still true — and that is the one defect class that **worsens with nobody doing anything**,
because the file that becomes wrong is never in the diff that made it wrong.

Measured 2026-07-22: `ref_minigame.md` documented three deleted files
<!-- stale-ok: the hack-scene deletion is the worked example of staleness in this doc. -->
(`hack_scene.py`, `hack_generator.py`, `hack_routing.py`) and ~55 lines of their API for
**4 months** after `e2e2f51` removed them. It surfaced because Karel asked a direct
question. Full spec, taxonomy and cadence: `08_staleness.md`.

Three findings from that incident that constrain the design elsewhere in this document:

- **Co-change gates prove attention, not accuracy.** `memory_freshness` (§9 Phase 0) *would*
  have caught this specific commit — record it as a dated replay hit — but one unrelated
  line added to the doc would have turned it green over 55 lines of fiction. It must not be
  reported as staleness coverage.
- **Removals are the highest-yield trigger.** Nobody forgets to document the feature they
  just built (§12b: the body is what gets enjoyed). Deletions have no body and no artefact
  announces them, so they get their own gate rather than a charter bullet.
- **§15's rejection of git-history replay does not apply here.** That rejection rests on
  defects being fixed *before* the commit boundary. Stale docs are the opposite: they get
  committed and stay committed until noticed, so history is the complete record of this
  class, not a censored one. This is the single documented exception — see `08_staleness.md`
  §8, and do not generalise it to any other class.

Meta-rule: **the audit in `01_audit_findings.md` must be re-runnable.** It is the health
metric for this whole system (rules total, % enforced, charter length trend). If it can
only be produced by hand, self-maintenance is fiction.

## 15. Measuring the gain

### Two rejected instruments — do not re-propose these

**Rejected: replaying gates against git history.** Karel and Claude fix defects *before*
committing. Git history is therefore a **post-review, censored sample** — the commits are
the survivors. Replay would return ≈0 catches and produce the false conclusion that gates
do not pay. The defects this system must catch occur *during* a session and die before the
commit boundary. They are invisible to history by construction.

**Rejected: escaped-defect rate as the north star.** Same censoring. Almost nothing
escapes, because Karel catches it. Measuring escapes measures Karel's diligence, not the
system's.

### The actual insight (Karel, 2026-07-21)

His overnight image run produced results carrying mistakes **he had already pointed out
before**. Those defects will never reach a commit — because he caught them again, by hand,
in the morning.

> The cost of a defect here is not a bad commit. It is **Karel's attention** and a wasted
> night of compute.

The same defect caught by a gate at 3 AM and caught by Karel over coffee is the *identical
defect* at wildly different cost. **The metric is attention, not defect count.**

### Instruments that actually work

| # | Metric | Why it's uncensored |
|---|---|---|
| 1 | **Repeat-correction rate** — how often Karel corrects the same *class* of thing twice | Pure waste. The 12 `feedback_*.md` files are an admission of it: each exists because a correction did not stick the first time |
| 2 | **Unattended fix rate** — defects caught *and* retried with no human in the loop | Measures exactly the thing being bought |
| 3 | **Wasted-night rate** — overnight runs producing nothing usable | The image run scores 0 today; it is a real baseline |

### Immediate evidence — replay against the right corpus

Not commits. **The 12 `feedback_*.md` files.** For each: would a gate have caught it, and
*could* one? Small N, but uncensored and on-target. Do this before building the gate suite.

### Prerequisite finding: the defect stream is not logged

Karel's mid-session corrections — the real defect data — are recorded **nowhere**. The
feedback files capture only those severe enough to warrant a permanent rule; everything
else evaporates. **Phase 0 deliverable: a correction log.** One line per correction
(date, class, would-a-gate-have-caught-it). Near-zero cost, and it is the instrument
every metric above depends on.

### Gate selection must follow observed failures, not ease of implementation

Cautionary example from this very session: Claude proposed the i18n **parity** test
(set-equality across en/cs/es) as the flagship gate. Karel's actual observed i18n failures
were (a) an English word left inside a Czech value, and (b) a string hardcoded and never
routed through `t()`. **Parity catches neither** — in (a) the key exists everywhere and the
*value* is wrong; in (b) the key never existed. The test was chosen because it was easy to
write, not because it targets a failure that happens here.

Gates aimed at the real failures:

- **hardcoded string** → AST scan for string literals at known call sites (matrix rule 4)
- **untranslated leftover** → `cs[key] == en[key]` byte-identical, with an allowlist for
  legitimately identical strings ("OK", numerals, proper nouns)

Parity is retained as cheap insurance, not claimed as gain.

**Not claimed:** one developer, ~90 commits. These are directional instruments, not proof.

## 16. Execution topology — split on context overlap (Karel, 2026-07-21)

Supersedes the naive "one role per topic" implied by §12b. Evidence: doc 02.

> **A process boundary belongs only where context overlap is near zero.
> Everywhere else, run sequentially in one thread.**

Test for a safe boundary: **can the handoff be a short payload rather than a briefing?**
If you would have to explain the codebase to the other side, do not split there.

### The topology

**One sequential thread** (Sonnet), sharing context:
`implement → write tests → wire UI → verify`.
Guidelines load as **skills at the step where they are needed**, not up front.

**Separate agents** — context genuinely disjoint from the code:

| Agent | Input payload | Needs code context? |
|---|---|---|
| translation | strings + terminology file + style guide | no |
| art | subject + style refs + ComfyUI | no |
| audio | subject + style refs | no |

Doc 02 measured the cost of getting this wrong: splitting implementer from test-writer
burned ~50–70k on pure re-discovery, and splitting i18n from UI (both writing `i18n.py`)
caused the stale-read false bug report.

### The second seam: producer / checker (Karel, 2026-07-22)

The overlap test above finds one seam. There is a **second**, and Session E missed it —
it correctly refused three bad splits (art by subject, implement/test/UI, the `closer`)
and in doing so failed to notice a good one sitting next to them.

> **Split the thing that makes an artefact from the thing that judges it, whenever the
> judgment does not need the context that produced it.**

For every producer, ask: *what does the checker actually need?* If the answer is the
artefact plus the acceptance criteria — and **not** the tooling, the prompt, the pipeline,
or the reasoning that got there — that is a boundary, and it is a boundary worth taking
even when tokens are free.

Two independent reasons, and the second is the important one:

1. **Cost.** The artefact never enters the producer's context. For art this is decisive:
   images are expensive and an iteration loop re-pays for them every round.
2. **Blindness is the point.** *An agent reviewing its own output sees what it intended,
   not what it made.* Knowing the prompt is what makes self-review weak, so the checker is
   deliberately denied it. This is a **quality** argument; it survives local models getting
   free, which the cost argument does not.

Consequences that are easy to get wrong:

- **Three layers, not two.** Script → checker → human. Anything mechanical (dimensions,
  parity, geometry, a filename) belongs in a gate; §12 already says that, and a checker
  spending attention on what a script proved is the waste this seam is meant to remove. The
  charter says so explicitly, in a "what the gate already proves" section.
- **A checker never fixes.** It reports; the producer acts on words. Once a checker edits,
  it needs the producer's context back and the seam closes.
- **Bound the loop.** Producer → checker → producer must have a hard retry limit and a
  park-for-human exit. Unbounded, it burns the night and delivers nothing.
- **A checker's pass is not approval** where the final gate is human (art, audio). It
  reduces how much reaches the human; it does not replace them. Prose alone did not hold
  that line: a passing art card went on to the diff reviewer, merged, and landed in
  `testing/` as if finished, so the checker's `pass` *was* functioning as approval until
  2026-09-08. It is now mechanical — `runner.unadopted_artefacts` sees candidates harvested
  with none installed and routes the card to `needs-decision/` regardless of what its
  `verify:` claims, so the human gate is a lane rather than a sentence (`03_board.md` §5).
- **This does not re-open the splits already refused.** Those failed the *overlap* test —
  they were producers needing a briefing. This seam is producer-versus-checker, which is a
  different cut, and §12's "narrow single-concern checkers" was always the rule on that
  side of the line.

The first instance is `art` / `art-reviewer` (`03_board.md` §5). **Every new producer
charter must state where its checker boundary is, or record that it has none and why.**

### Skills are the durable asset; topology is a deployment choice

A "how to write tests here" skill is the **same text** whether executed as step 2 of a
Sonnet thread, as a standalone subagent, or by a local model at 3 AM. Write the guidelines
once; pick the topology per situation:

- **supervised / token-sensitive** → sequential single thread
- **overnight / wall-clock-sensitive** → parallel where context allows, because there the
  constraint is features-per-night, not tokens

Same one-contract-two-runtimes principle as §3, applied to sequencing instead of models.

### Model tiering (do not repeat doc 02's error)

| Work | Model |
|---|---|
| triage, design, review, gate authoring | Opus |
| implementation, tests, UI wiring | **Sonnet** |
| narrow checkers, translation, classification | Sonnet, or local when available |

#### The binding, in the form the dispatcher reads (Session G, 2026-07-23)

The table above is prose: it maps *work types* to models, and a dispatcher cannot
resolve `tier: worker` against it without a human in the loop. So the binding is written
once more, immediately below, in the form a script can read — **and this block, not the
prose table, is what `nightshift/tiers.py` parses.** Prose and block must agree; the block is
the operative one, the table is the reasoning.

Writing it here rather than in `nightshift/tiers.py` is the whole point. The rule this section
exists to enforce is that the tier→model binding lives in exactly one place, and a
constant in a Python file would be a second place that drifts silently the day Phase 5
rebinds the worker tier. Session I edits the block below and changes nothing else.

```tier-binding
lead   = opus
worker = sonnet
```

The values are CLI model aliases (`claude --model <alias>`), not full model ids, because
an alias tracks the latest model in its family and a pinned id would silently rot. If
Phase 5 points the worker tier at a local runtime, this becomes the endpoint's model name
and `ANTHROPIC_BASE_URL` does the rest (§3) — still one edit, still here.

#### Enforcement — the missing half (measured 2026-07-22)

This table has existed since 2026-07-21 and was **violated the first time a card was
executed**: `adrenaline-pump` ran at the lead tier because the dispatch call passed no
model and a subagent silently inherits the lead's. Nothing read this table. It is the same
failure mode as §15's prose-only rules — a correct rule with no enforcement point.

The rule is **tier-based, not model-based**. "Sonnet" is today's binding of the worker
tier; Phase 1 rebinds it to a local model (§9), and this table is the only place that
binding may be recorded. Enforcement must therefore name the *tier*, never the model.

Three places it has to land, cheapest first:

1. **Card front-matter carries `tier:`** (`worker` default, `lead` for cards explicitly
   flagged as needing judgment). Makes escalation a visible, reviewable decision instead of
   an invisible default. Belongs in the recipe's "what a card has to say" block.
2. **The dispatcher resolves `tier: → model`** from this table at spawn time. One lookup,
   one place to change when Phase 1 lands. No caller names a model.
3. **A `PreToolUse` hook on `Agent` refuses a card-execution spawn with no resolved tier**
   (§12: a hook is the only mechanism Claude cannot forget). This is the enforcement point
   proper — 1 and 2 are conventions, and conventions are what just failed.

**Why the tier binds even when the worker is local.** The cost argument weakens as local
inference gets cheap, and the temptation will be to run everything at the strongest
available tier "since it's free". That inverts the purpose: the worker tier is the
*measuring instrument* for card quality. A card that only executes correctly above its
declared tier is a defective card, and running it higher is how you avoid finding out.

### Context management inside the thread — corrected

An earlier draft claimed "persist the conclusion, drop the derivation" as a *token*
optimisation. **That was overstated.** Precisely:

- The card kills re-**discovery** (*which* file matters — greps, false paths, dead ends).
  This is the expensive part: the implementer spent 40 tool calls, mostly discovering.
- The card does **not** kill re-**reading** (what a known file currently says). Re-reading a
  known path is one cheap `Read`.
- Intra-session, files stay in context anyway unless compaction happens. **So the card's
  real value is surviving compaction, session death, and the 3 AM reboot — durability, not
  intra-step economy.**

Intra-thread token savings come from *not crossing process boundaries*, not from the card.

## Sources

- <https://www.promptquorum.com/power-local-llm/best-local-models-tool-calling-2026>
- <https://braindetox.kr/en/posts/local_llm_agentic_coding_2026.html>
- <https://www.runlocalai.co/guides/claude-code-with-local-models>
- <https://ollama.com/blog/claude>
- <https://unsloth.ai/docs/models/tutorials/qwen3-coder-how-to-run-locally>
- <https://amux.io/guides/claude-code-headless/>
- <https://code.claude.com/docs/en/sub-agents>
