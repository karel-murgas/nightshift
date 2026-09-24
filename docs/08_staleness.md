# 03 — Staleness: detection, review, cleanup

Status: **shipped** (Session D, 2026-07-22). Three gates — `doc_reference_liveness`,
`doc_signature_drift`, `deletion_sweep`, all now in `nightshift/gates/` — plus the
`stale-hunter` Tier-2 worker and the `--stale` runner phase (`nightshift/stale_sweep.py`).
Depends on: `00_architecture.md` §12, §14, §15. Consumed by: Session D (`SESSIONS.md`).

---

## 0. Why this is its own process

Every other quality process in this system asks *"is the new work correct?"*. This one asks
*"is what we already wrote still true?"* — a different question with a different failure
mode, a different detection method, and a different cost curve.

Staleness is the only defect class that **gets worse with no one doing anything.** A bug is
introduced by an edit. Stale documentation is introduced by an edit *somewhere else* — and
the file that becomes wrong is never in the diff, which is precisely why nobody looks at it.

It also violates the assumption the rest of the system rests on. `00_architecture.md` §2
and §16 both justify the memory tree as the cheap orientation layer that stops Claude
re-reading the codebase. **A stale memory tree is worse than no memory tree**: it is
confidently wrong, it is read first, and it is trusted. The cost is not a wasted read — it
is an agent that plans against a codebase that does not exist.

---

## 1. The measured incident (2026-07-22)
<!-- stale-ok: this section IS the record of the 2026-03-20 deletion — hack_scene.py,
     hack_generator.py, hack_routing.py, HackScene/HackMap/HackNode/HackParams,
     generate_hack_map, _NODE_R and feedback_hack_routing.md are all named here because
     they are dead. That is the finding, not a defect. -->

Karel asked whether the classic hack minigame still ran. Findings, verified by hand:

- `project_tigress/minigame/hack_scene.py` (1710 lines), `hack_generator.py` (246 lines) and
  `hack_routing.py` were **deleted in `e2e2f51`, 2026-03-20**.
- `.claude/memory/ref_minigame.md` still documented all three as live — a "Variants"
  section naming the files, plus ~55 lines of Classic data model / generation / scene API
  (`HackNode`, `HackMap`, `HackParams`, `generate_hack_map`, `HackScene`, `_NODE_R`).
- **~4 months undetected.** It surfaced because Karel asked a direct question, not because
  anything checked.
- Collateral in the same file: the launcher documented as taking a `[grid|classic]`
  argument that does not exist, and `on_complete` documented with **4** parameters against
  an actual **7** (`hack_scene_grid.py:883`).
- `.claude/memory/feedback_hack_routing.md` is *entirely* about `_NODE_R` in the deleted
  `hack_scene.py` — a whole file with no live referent.
- `MEMORY.md`'s index line still advertised the file as "HackScene, HackMap, HackParams".

### What the incident actually teaches
<!-- stale-ok: this section IS the record of the 2026-03-20 deletion — hack_scene.py,
     hack_generator.py, hack_routing.py, HackScene/HackMap/HackNode/HackParams,
     generate_hack_map, _NODE_R and feedback_hack_routing.md are all named here because
     they are dead. That is the finding, not a defect. -->

**(a) `arch.md` was correct the whole time.** Its module map says grid is the only variant.
So staleness is **per-file and per-section**, not a global property of the tree. There is no
single "last verified" signal to trust, and a sweep must go file by file.

**(b) `memory_freshness` would have caught this one.** `e2e2f51` touched
`project_tigress/minigame/` and updated `arch.md`, `ref_i18n.md`, `state.md` — but **not**
`ref_minigame.md`. The gate built in Session A fires exactly here. Record this as a real
replay hit, dated, in Session A's hit-rate table.

**(c) …and it would still not be enough.** `memory_freshness` proves a file was *touched*,
never that it is *true*. Had that commit added one unrelated line to `ref_minigame.md`, the
gate would have passed green over 55 lines of fiction.

> **Co-change gates prove attention, not accuracy.** They are necessary and cheap. They are
> not a staleness check, and must not be reported as one.

**(d) The trigger was a deletion.** Nobody forgets to document a feature they just built —
that is the interesting part (§12b: *"the spine is what gets forgotten, the body is what
gets enjoyed"*). Deletions have no body. Nothing is enjoyable about them and no artefact
announces them. **Removals are the highest-yield staleness trigger and deserve their own
gate.**

---

## 2. Staleness classes (the taxonomy the sweep works from)
<!-- stale-ok: this section IS the record of the 2026-03-20 deletion — hack_scene.py,
     hack_generator.py, hack_routing.py, HackScene/HackMap/HackNode/HackParams,
     generate_hack_map, _NODE_R and feedback_hack_routing.md are all named here because
     they are dead. That is the finding, not a defect. -->

Do not sweep "for stale things" — that is a ten-item checklist and it will blur (§12
refinement). Sweep one class at a time.

| # | Class | Example from the incident | Detection |
|---|---|---|---|
| S1 | **Dangling reference** — doc names a file/symbol that no longer exists | `hack_scene.py`, `HackParams` | **deterministic** |
| S2 | **Signature drift** — doc states an API that still exists but changed | `on_complete` 4 args vs. 7 | **deterministic** (partial) |
| S3 | **Orphan document** — whole file's subject is gone | `feedback_hack_routing.md` | deterministic (S1 density) |
| S4 | **Stale index** — pointer file describes its target wrongly | `MEMORY.md` row | deterministic (partial) |
| S5 | **Semantic drift** — every name resolves, but the described behaviour is no longer what the code does | a doc describing a reworked mechanic | **narrow LLM checker** |
| S6 | **Superseded rule** — a `feedback_*.md` rule now enforced structurally, or overruled by a later one | `feedback_skill_hint_position.md` | Claude + Karel |
| S7 | **Tool rot** — a dev entry point nobody runs, quietly broken | `main_hack.py` freeze (§6) | **smoke gate** |

S1–S4 are Tier 1. S5 is Tier 2. S6 is Tier 3. S7 is Tier 1 but needs a runner, not a parser.

---

## 3. Tier 1 — the deterministic core

Per §12, everything a script can decide is a gate, never a review bullet.

### 3.1 `doc_reference_liveness` (S1) — the flagship
<!-- stale-ok: this section IS the record of the 2026-03-20 deletion — hack_scene.py,
     hack_generator.py, hack_routing.py, HackScene/HackMap/HackNode/HackParams,
     generate_hack_map, _NODE_R and feedback_hack_routing.md are all named here because
     they are dead. That is the finding, not a defect. -->

Scope: `.claude/memory/**`, `.ai/recipes/**`, `Board/tasks/**`, `CLAUDE.md`,
`.claude/plans/**`.

Extract from prose and fenced blocks, then resolve against the tree:

- **paths** — anything matching `project_tigress/…`, `tests/…`, `.ai/…`, `*.py`, `*.md` →
  must exist on disk.
- **symbols** — backticked identifiers that look like code (`ClassName`,
  `function_name()`, `module.attr`) → must appear in an AST symbol table built once from
  `project_tigress/`.

Against today's tree this fires on `hack_scene.py`, `hack_generator.py`,
`hack_routing.py`, `HackScene`, `HackParams`, `HackMap`, `HackNode`, `generate_hack_map`,
`_NODE_R` — i.e. it catches the entire incident, with zero judgment.

**The one real design problem: history is supposed to name dead things.**
`state.md:2421` reads *"Classic hack scene removed — `hack_scene.py` and
`hack_generator.py` deleted"*. That sentence is **correct** and must not be flagged. A gate
that cannot tell a history log from a live reference will be muted within a week, and a
muted gate is worse than no gate.

Resolution — two mechanisms, both needed, neither an allowlist of individual names:

1. **File-level scope declaration.** Each doc declares `doc_scope: current | history` in
   its frontmatter. `history` files (`state_history.md` after Session C, the dated pass
   logs in `ref_i18n.md`) are skipped wholesale. Missing frontmatter defaults to
   `current` — the safe direction.
2. **Section-level opt-out.** An `<!-- stale-ok: reason -->` marker exempts the following
   block inside an otherwise-current file, with the reason mandatory. Session-narrative
   sections in `state.md` use this until Session C splits them out.

A per-name allowlist is explicitly rejected: it grows monotonically, encodes no reason, and
would have to list every symbol the project ever deleted.

### 3.2 `doc_signature_drift` (S2)

Narrower and worth doing because `ref_minigame.md` is *built* out of fenced signature
blocks. For every fenced `python` block containing `def <name>(` under a heading that names
a live module: parse the block, parse the real definition, compare **parameter names and
count** (not defaults, not annotations, not order-insensitively — exact names, in order).

`on_complete: Callable[[bool, list[Item], int, int], None]` vs. the live 7-arg call would
have been flagged the day the arity changed. Types stay out of scope — too much surface for
too little yield.

<!-- stale-ok: `doc_orphan` is a threshold on 3.1, not a module — no doc_orphan.py exists
     by design. -->
### 3.3 `doc_orphan` (S3)
<!-- stale-ok: `entrypoint_smoke` / `doc_orphan` are gates this spec proposes. A gate
     that is specced but not yet built reads exactly like a deleted one. -->

Not a separate parser — a **threshold on 3.1**. A doc whose dangling-reference ratio
<!-- stale-ok: `doc_orphan` is a threshold on 3.1, not a module — there is deliberately
     no doc_orphan.py to resolve against. -->
exceeds ~60% of its total resolved references is reported as an orphan candidate rather
than as N individual violations. This turns `feedback_hack_routing.md` into one
actionable line ("this file has no live referent") instead of eleven noisy ones.

Deletion is never automatic. It escalates to Tier 3 (§5), and Session C's rule holds:
**confirm with Karel before deleting a memory file.**

**Code-side sibling:** `.ai/gates/event_posted.py` ([[unreferenced-symbol-gate]],
2026-08-02) is S3's shape with no doc in the loop — a declared `Event` subclass with no
live referent (nothing ever posts it), deterministic, reported one line not N, deletion
never automatic. It cannot share this mechanism: every class in §2's taxonomy takes a
*document* as its subject, and this rule's subject is a class declaration, so it lives as
its own gate rather than a fourth doc-scanning module here.

### 3.4 `deletion_sweep` (the trigger from §1d)

The counterpart to `memory_freshness`, and the gate that targets the actual observed
failure. On any diff that **deletes a file under `project_tigress/` or removes a top-level
`class`/`def`**: grep the doc scope (§3.1) for the deleted path and symbol names. Any hit
outside a `history`-scoped file fails the gate.

Cheap, exact, and it fires at the only moment when the person who caused the staleness is
still in the room. **This is the highest-value gate in this document** — everything else in
§3 is cleanup for staleness that already escaped.

---

## 4. Tier 2 — the `stale-hunter` checker (S5)

One concern, one call, per §12's *"ten single-concern checkers beat one ten-item checker"*.

```
input:   ONE doc file  +  the current source of the modules it names
                          (resolved via 3.1's reference extraction — no free-roaming reads)
output:  [{ claim: <verbatim quote>, verdict: true|false|unverifiable, evidence: <file:line> }]
task:    "Which sentences in this document are no longer true of this source?"
```

Constraints that make it reliable and cheap:

- **One file per call.** Never "sweep the memory tree" in one prompt.
- **Quote-or-drop.** A finding must quote the doc verbatim and cite `file:line` in the
  source. No quote, no finding. Unverifiable is a legitimate, non-penalised verdict — the
  same principle as `needs-decision/` being a success state (§13).
- **It never edits.** It reports. Editing is Tier 3.
- **It runs only on files Tier 1 already passed.** Tier 1 catches the cheap 80%; asking an
  LLM to notice a missing file is exactly the waste §12 forbids.

This is a strong local-model candidate under §11/§12: narrow, schema-constrained,
evidence-cited, and its output is verifiable — every cited `file:line` can be checked by a
script before Claude reads a word.

---

## 5. Tier 3 — Claude's decision, and the only real judgment call

Tier 1 and 2 produce *findings*. Every finding resolves to exactly one of four verdicts,
and choosing is the part that cannot be automated:

| Verdict | When | Action |
|---|---|---|
| **Correct** | the claim was true, the code moved | rewrite the claim against current source |
| **Relocate** | the claim is true *history* | move to a `history`-scoped file — Session C's rule: *do not delete history, relocate it* |
| **Delete** | no live referent, no historical value | remove; **Karel confirms** for any `feedback_*.md` |
| **Fix the code** | the doc describes the intended behaviour and the *code* drifted | file a task card — the doc was right |
| **Delete the subject** | the *thing being described* has outlived its use | drop the code/tool too, not just the doc — see §6, Karel's standalone-launcher ruling (executed 2026-07-30) |

The last row is the cheapest verdict available and the easiest to miss, because every
finding arrives phrased as "this text is wrong" and the obvious repairs are all edits.
**Ask "should this exist?" before "is this correct?"** — deletion is the only verdict that
removes the surface from every future sweep instead of resetting its clock.

The last row is not hypothetical and is why this is not a docs-only process. It is also why
the sweep must never be delegated wholesale to a worker that only has write access to docs:
the correct fix is sometimes in `project_tigress/`.

---

## 6. S7 — tool rot, and the standalone-launcher case
<!-- stale-ok: `entrypoint_smoke` / `doc_orphan` are gates this spec proposes. A gate
     that is specced but not yet built reads exactly like a deleted one. -->

The same session surfaced a second, non-doc staleness: the standalone launcher
`main_hack.py` freezes. Pressing Esc/Q before the first move pops the only scene off the
stack (`hack_scene_grid.py:833-839`), leaving `SceneManager.current is None` while
`self.running` stays `True` — the loop spins forever, the last frame stays on screen, and
no key does anything. `GameApp.run()` never hits this because `MainMenuScene` sits
underneath. Also: no `VIDEORESIZE` handling, and `python main_hack.py tiger` crashes on
`int("")`.

Nothing caught it because **no test imports `main_hack.py` and no gate runs it.** ~1198
tests, none of which touch the dev entry points.

Gate: `entrypoint_smoke` — for each standalone entry point, boot it headless
(`SDL_VIDEODRIVER=dummy`), drive a scripted event sequence including **the quit path**, and
assert the process exits cleanly rather than looping. Cheap, deterministic, and it
generalises to every future `main_*.py`.

Deliberately **not** in scope for Session D's doc sweep — it is listed here because it is
the same *class* of rot (unwatched surface) and belongs to whoever builds the gate suite.

### Karel's ruling (2026-07-22): the launcher is deleted, not fixed — **executed 2026-07-30**
<!-- stale-ok: `entrypoint_smoke` / `doc_orphan` are gates this spec proposes. A gate
     that is specced but not yet built reads exactly like a deleted one. The launcher
     this section is a case study of was itself deleted on 2026-07-30; §6 above names it
     in prose rather than as a backticked path for exactly that reason. -->

> *"We will delete it later, when we do a big cleanup. It's not useful anymore."*

Queued in `Úklid.md`, and **it sat there for eight days** — which is its own small finding:
the queue was a checklist in Karel's private `ideas/` lane, which no judgment actor may read,
so nothing in the system could ever pick the ruling up. A decision parked where only its
author can see it is indistinguishable from a decision not made. It was finally executed in
the 2026-07-30 loose-end sweep, along with the seven live docs that still named the file.
**Do not fix the freeze** — spending review attention on a dev tool nobody runs is exactly
the waste this process exists to stop.

This is a verdict the §5 table was missing, and it generalises:

> **Before asking "is this correct?", ask "should this exist?"** For an unwatched surface —
> a dev tool, an orphan doc, a dead branch — deletion is usually the cheapest correct answer,
> and it is the only one that removes the surface from every future sweep.

Consequences for the gate:

- `entrypoint_smoke` is still worth building, for `main.py` and for whatever entry points
  come later. But it **loses its known-red example** when the launcher goes, so build it
  against a *synthetic* frozen-loop fixture rather than against `main_hack.py` — otherwise
  the gate's only test disappears with the file it was written for.
- **Order matters:** delete first, then gate. Writing the gate first buys one red that is
  about to be resolved by `rm`.
- The launcher's staleness was *also* an S1 source (`CLAUDE.md:7` and `ref_minigame.md`
  both document its arguments). Deleting the file converts those from "incomplete" to
  "dangling" — `doc_reference_liveness` and `deletion_sweep` will catch them at that
  moment, which is the system working as designed. The Úklid entry names them anyway so
  the cleanup does not depend on the gates existing yet.

---

## 7. Cadence — three triggers, not a schedule

A monthly sweep alone would have let the hack-scene fiction stand for up to a month; the
real gap was four. Staleness needs event triggers, with the calendar as backstop only.

| Trigger | Runs | Cost |
|---|---|---|
| **Every diff** (hook) | `doc_reference_liveness` on *changed* docs + `deletion_sweep` on the diff | milliseconds |
| **On removal** — any deleted file or symbol under `project_tigress/` | `deletion_sweep` full-tree | ~1 s |
| **Monthly** (`stale-hunter` role, §14) | Tier 1 full-tree, then Tier 2 on the N files whose named modules changed most since the last sweep | one batch |

The monthly pass is **change-ordered, not alphabetical.** Sweeping `ref_tileset.md` (3 KB,
unchanged for months) at the same rate as `ref_minigame.md` (33 KB, in the middle of the
net-tiger work) spends the budget in the wrong place.

Record the last sweep as one line in `.ai/corrections.log` so the next run knows its
baseline. No separate state file.

---

## 8. Measurement — and why history replay is *legitimate here*

`00_architecture.md` §15 rejects replaying gates against git history, because defects are
fixed before commit and history is therefore a censored sample of survivors.

**Staleness is the documented exception, and the reasoning inverts cleanly.** Stale docs
are not caught before commit — they are committed, and they *stay* committed until someone
notices. History is not censored for this class; it is the complete record of it. The
hack-scene gap is measurable to the day (`e2e2f51`, 2026-03-20 → 2026-07-22) precisely
because both endpoints are in git.

So, uniquely for this process:

| # | Metric | Source |
|---|---|---|
| 1 | **Staleness half-life** — days between a symbol's deletion and the last doc reference to it being removed | git, directly replayable |
| 2 | **Escaped-reference count** — dangling references present at any given HEAD | run 3.1 over past commits |
| 3 | **Discovery channel** — found by gate / by Claude mid-task / **by Karel asking** | `.ai/corrections.log` |

Metric 3 is the one that matters, and it maps onto §15's real currency: **attention.**
Today's incident scores *"by Karel asking"* — the most expensive channel there is. The
target is to drive that count to zero, not to drive the dangling-reference count to zero.

Baseline to record before building anything: run 3.1 by hand over the current tree and
write down the number. Without it there is no before/after.

---

## 9. Backlog handed to Session D — **CLEARED 2026-07-22**
<!-- stale-ok: this section IS the record of the 2026-03-20 deletion — hack_scene.py,
     hack_generator.py, hack_routing.py, HackScene/HackMap/HackNode/HackParams,
     generate_hack_map, _NODE_R and feedback_hack_routing.md are all named here because
     they are dead. That is the finding, not a defect. -->

Known-stale, already verified this session — the sweep's first work items, not a full list:

- `ref_minigame.md` §"Variants"/Classic block (~55 lines), the `[grid|classic]` launcher
  arg, and the 4-arg `on_complete` (actual: 7).
- `ref_minigame.md` frontmatter `description:` and `MEMORY.md`'s row for it — both still
  advertise `HackScene, HackMap, HackParams`.
- ~~`feedback_hack_routing.md` — orphan (S3).~~ **Done 2026-07-22 by Session C** (Karel
  confirmed the deletion; recoverable from git history). Left here as the worked S3 example,
  not as an open item.
- `ref_i18n.md:86-93,114-116` — "HackScene" naming, cosmetic; keys themselves are correct.
- `CLAUDE.md:7` — `main_hack.py` usage line, incomplete rather than wrong (omits
  `neural1/2`, `noskel`, `tigerN`).
- `hack_node.py` is a **partial** case worth getting right: the file lives on and
  `LootKind`/`SecurityKind` are used by the grid variant — `HackNode`, `HackMap` **and
  `NodeType`** are dead (corrected 2026-07-22, Session D: this list originally kept
  `NodeType`, but the grid variant uses `GridCellType`; the backlog entry was itself
  slightly stale). Do not delete the section; correct it. This is the shape most S1
  findings take, and a blunt "the file is gone" gate would get it wrong.

---

## 10. Do not

- Do not let a worker delete a memory file. Deletion is Tier 3, Karel confirms.
- Do not build a per-symbol allowlist for 3.1 (§3.1 — use scope declarations).
- Do not ask the Tier 2 checker to sweep more than one file per call (§12).
- Do not report `memory_freshness` as staleness coverage (§1c).
- Do not rewrite history sections to "fix" them — relocate (§5).


---

<!-- stale-ok: this section reports on the sweep, so it names the dead NodeType, the
     not-yet-built entrypoint_smoke gate, and the harness's Bash matcher — none of which
     can resolve against project_tigress/ by construction. -->
## 11. Session D outcome (2026-07-22)

Built: `doc_reference_liveness` (S1 + S3 threshold), `doc_signature_drift` (S2),
`deletion_sweep` (S1 at deletion time, wired as a `Bash` hook on `git rm`/`rm`/`git mv`),
shared machinery in `doc_scan.py`, 20 tests in `tests/test_staleness_gates.py`,
and the `stale-hunter` charter (§4) in `.claude/agents/` — which **did not exist before this
session**; Session E's roster step now has a directory and one worked example to follow.
(All four of those moved into the `nightshift` package on 2026-08-02, 07_portability.md §8
step 4, and the 20 mechanism tests with them; what `tests/test_staleness_gates.py` still
holds is this repo's own grounding.)

Dangling references **200 → 0**; exemption debt **52** (14 file-level `doc_scope:`,
38 section-level `stale-ok`), every one with a written reason, asserted by test.

Three things this session learned that the spec did not say:

1. **§9 was itself stale** (the `NodeType` correction above). A hand-written finding list
   is not more trustworthy than the tree it describes.
2. **The S5 gap is real and measurable.** Two value drifts — `DEADLINE_DIST_MULT` 2.0 vs.
   the live 3.0, and a `MinimapOverlay` parameter that no longer exists — were found *by
   hand* while verifying S1 hits. Every symbol in both resolves, so no Tier 1 gate can see
   them. That is the case for actually running `stale-hunter`.
3. **A sweep must shrink the file** (Karel, mid-session — `feedback_doc_leanness.md`). The
   first pass at `ref_minigame.md` replaced dead API blocks with *tombstones* ("deleted in
   `e2e2f51`", "there is exactly one variant"), which grows the orientation cost a staleness
   pass exists to cut. §5's **Delete** verdict applies to the sentence, not just the subject,
   and deletion beats a `stale-ok` marker. Git already holds the record.

**Not done, deliberately:** `entrypoint_smoke` (§6). Karel ruled `main_hack.py` deleted
rather than repaired, and §6 says *delete first, then gate* — building it now buys one red
that `rm` is about to resolve. SESSIONS.md step 8 marks this optional for exactly this case.
Left for whoever does the Úklid cleanup.
