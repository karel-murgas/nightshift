# Doc 10 — The self-improvement loop (Session H, 2026-07-23)

Phase 4. The correction log had been written to for six sessions and read by nothing.
This is the session that read it.

Spec inputs: `00_architecture.md` §14 (the maintenance roles) and §15 (what to measure).
Finding input: `09_runner.md` §8.

---

## 1. The clusters

50 entries, 2026-07-21 → 2026-07-23. Reproduce with `python -m nightshift.corrections`; the
class assignment for every entry is a field in the log, so these counts are a file-state
lookup rather than a reading of prose.

| Class | n | % | Gateable? |
|---|---|---|---|
| **`derived-not-verified`** | **12** | 24% | **no — earns a procedure, §3** |
| `doc-staleness` | 7 | 14% | already gated (Session D) |
| `process-metric` | 7 | 14% | not defects |
| `judgment-not-mechanisable` | 4 | 8% | no, by definition — Tier 2 candidates |
| `unenforced-rule` | 4 | 8% | partly, §5 |
| `game-code-rule` | 3 | 6% | already gated (Session A) |
| `doc-hygiene` | 3 | 6% | one candidate, deferred with reasons — §5 |
| `silent-noop` | 3 | 6% | **yes — `subprocess_result_checked`, §5** |
| `check-stopped-checking` | 2 | 4% | folds into the above |
| `deferred-instead-of-fixed` | 2 | 4% | no |
| `scope-decision` | 2 | 4% | not defects |
| `tool-rot` | 1 | 2% | the entry-point smoke gate, specced and still not built — §5 |

`silent-noop` and `check-stopped-checking` are one family split by what the thing was:
a mechanism that did nothing, versus a check that passed for the wrong reason. Both are
*"nothing happened and it looked fine"*. Counted together they are 5, second place.

### Who found them — and this is the number that matters

§15 is explicit that the metric is **Karel's attention**, not defect count.

| Channel | n | % |
|---|---|---|
| claude (noticed mid-session) | 17 | 34% |
| **karel** | **16** | **32%** |
| none (bookkeeping rows) | 5 | 10% |
| gate | 4 | 8% |
| test | 4 | 8% |
| audit | 4 | 8% |

**Gates found 4 of 50, and three of those four are from day one** — the i18n and
render-codepoint hits on 2026-07-21, when the gates were new and being pointed at a
codebase they had never seen. Sessions E, F and G produced **zero** gate-channel entries.
Tests found 4, and every one is a test asserting the system's *own bookkeeping* rather
than the codebase, which is a pattern both Session D and Session G recorded independently.

Karel's 16 are not spread evenly: **4 on 2026-07-22 and 12 on 2026-07-23.** Read naively
that is the wrong direction for a project whose stated goal is to spend less of his
attention.

**The confound, stated so it is not quoted without it:** 2026-07-23 was Session G, an
interactive design session with Karel in the room reviewing the runner as it was written.
Corrections he made there are cheap ones — caught at design time, before anything shipped
— and they are the *good* case, not the expensive one. What §15 actually costs is a defect
that survives to the morning. The log currently cannot distinguish the two, because
`channel: karel` covers both "Karel asked a question mid-design" and "Karel found this
over coffee after a wasted night." That distinction is the next thing the log needs, and
it needs a night to have run before it means anything. **Not added now** — a field with no
instances is a guess, which is the mistake in §3.

### The zeros

- **`game-code-rule`: 3 total, all on 2026-07-21, zero since.** No Project Tigress defect has
  been logged in two days. This is not evidence that the game rules are now safe; it is
  that Sessions C–G were entirely meta-work and the log has recorded almost nothing but
  the AI team building itself. **Consequence for §5, and it is a limit on this whole
  session: every gate earned from this corpus is a gate on the machinery, not on the
  game.** The next real test of the log is a session that actually writes game code.
- **Zero `gate`-channel entries in Sessions E, F and G.**
- **Zero entries whose gate verdict is *yes*** — no correction was ever caught by a gate that already
  existed at the time. 26 are `yes-now` (a gate built in response), which is the gate suite
  working as designed but is a lagging measure, not a leading one.

---

## 2. Verdict on the log format: it did not survive contact

The read very nearly failed, and the failure was in the log rather than in the entries.

- **`RULE_CLASS` was free text and had grown 40 distinct values across 50 entries.** The
  one operation the log exists to support — cluster by class of thing corrected — had no
  field to cluster on. Every count in §1 had to be produced by reading all fifty notes by
  hand.
- **The most valuable axis was not a field at all.** Who found the defect is §15's
  headline metric and it existed only as prose inside `NOTE`, recoverable only by reading
  for `"KAREL:"`.

Fixed rather than noted, because step 2's own instruction is that this is cheap now and
expensive later. The log is now six fields —
DATE | SLUG | CLASS | CHANNEL | GATE | NOTE — with the class, channel and gate fields closed
vocabularies in `.ai/gates/data/corrections_vocab.json`, checked by
`nightshift/gates/corrections_log.py`. All 50 existing entries were migrated; nothing was
rewritten, three fields were inserted. The slug keeps the human handle so nothing that made
the log readable was traded away for making it countable.

Two design choices worth stating:

- **The vocabulary is a data file, not a constant in the gate.** Same rule as the i18n
  allowlists: a value list that ships inside the gate gets edited to make a run pass.
  Editing a data file is a visible decision, and it is this gate's appeal path (§4).
- **The gate never checks whether a class is the *right* one.** That is judgment, it is
  the whole content of the entry, and a gate guessing at it would be re-checking what a
  person decided (§12).

The gate also fails on a vocabulary value **no entry uses** — the mirror failure, and how
a closed list quietly becomes an open one by accumulating plausible categories that never
describe anything real.

---

## 3. `deferred-instead-of-fixed`: the hypothesis was right, about the wrong cluster

Session G's last entry asked Session H to test whether deferring-instead-of-fixing is a
pattern, on the evidence of *"three entries describing a rule that was correct in prose
and wrong in effect."*

**As its own class it is 2, not a cluster.** Both are real and both are Claude deferring
something cheap (the `unattended:` redefinition; the per-machine runner config, presented to Karel as a
blocker while a safer `--allowed-tools` option went unmentioned). Two instances in three
days is worth watching and is not yet a pattern.

**But the thing the entry actually described is the largest cluster in the log, at 12.**
Not "rules that get deferred" — *rules that were reasoned out carefully, written down
confidently, and were wrong the first time reality touched them.* That is
`derived-not-verified`, and its members are:

| Entry | What was derived without being run |
|---|---|
| `audit-verdict-wrong` | a retirement list written from file sizes and one-line summaries |
| `derivation-sample-too-small` | a card schema derived from 2 cards while a 3rd existed that it rejected |
| `field-judged-write-only-wrongly` | a frontmatter field dropped as write-only on a 2-card sample; the 3rd card used it |
| `missed-split-seam` | the producer/checker seam, missed by running only the overlap test |
| `parked-before-empty-diff` | two correct checks in an order that inverted §13 |
| `verdict-absent-must-not-fail` | a new orchestrator↔worker contract that would have broken all six existing workers |
| `unattended-definition-too-strong` | a definition that disqualified most of the project |
| `derived-rule-not-rewalked` | §5's art rule, still stating the *old* definition one layer down |
| `art-output-is-not-a-commit` | the empty-diff rule, never walked against a card that commits nothing |
| `per-machine-config-should-have-been-shared` | untracked config, right about the fact and wrong about the fix |
| `wrong-machine-got-the-gpu` | a fact about a doc promoted to a fact about this box |
| `loop-belongs-in-the-runner` | two objections to a split that both collapsed on inspection |

Deferring is a **symptom** of this, not a sibling of it: a rule you never ran against a
real case is a rule you have no concrete failure to point at, and an abstract problem is
exactly the kind that gets written up and routed to a future session.

**7 of the 12 were found by Karel.** They are what he is currently spending his attention
on, and they are not defects a gate can see — every one is a *correctly implemented*
decision that was wrong.

### It does not earn a gate. It earns a procedure.

Per step 3's own instruction (*"stop and record it if a cluster turns out to need judgment
— that is a finding, not a failure"*). There is no pattern to match on: the code is
correct, the prose is coherent, and the defect is only visible against a corpus nobody
enumerated. What there *is* is a repeatable move, and every one of the 12 would have been
caught by it:

> **Before shipping a rule, definition, schema or flag: name the corpus it governs,
> enumerate every member, run the rule against each one by hand, and report the count
> including zeros. If the corpus has fewer than three members, say so — that is the
> sample-size warning, not a pass.**

Three corollaries the 12 entries earned specifically:

1. **Agreement within a sample is not confirmation.** Two cards agreed and the agreement
   read as evidence; the third card was the corpus.
2. **Fixing a definition does not fix the rules derived from it.** They have to be walked.
   A derived rule reads as independently authoritative because it is stated as its own
   rule with its own reasoning.
3. **Writing "known limitation" more than once in a document is itself a trigger.** Four
   of `09_runner.md`'s unsolved problems had one cause and nobody noticed until Karel
   asked a question that touched all four.

This is now a section of `.ai/recipes/verify-before-shipping-a-rule.md` so it fires as a
step rather than as a memory.

---

## 4. The gate-appeal process

The step with no prior art in the repo, and the one most likely to be skipped. The
temptation when a gate blocks is to weaken it until it passes, and Session D shipped the
machinery that makes that easy — 45 doc-level exemptions already exist.

### The three levels, and who decides

| Level | What it is | Who decides | Written where |
|---|---|---|---|
| **L1 — exempt one site** | this line is a false positive | **Claude may**, and must say why | inline marker, counted as debt |
| **L2 — narrow the gate** | the gate's *rule* is wrong at the edges | Claude proposes, **Karel approves** | the gate module's docstring **and** a `corrections.log` entry |
| **L3 — retire the gate** | the rule should not be enforced at all | **Karel only** | `01_audit_findings.md` §A row moves back to `prose` |

**A fourth move is not on the list and is never legitimate: editing the rule's prose until
the code complies.** That is the failure this section exists to stop. If the prose is what
is wrong, that is L2 or L3 and it goes through a person.

**Precondition for any level: the violation has been verified by hand as not a real
defect.** An appeal filed because a gate is inconvenient is a defect with paperwork.

### The marker

Session D's `<!-- stale-ok: reason -->` covers docs and is unchanged. Code had no
equivalent, so it gets the same shape:

```python
subprocess.run(...)  # gate-ok(subprocess_result_checked): git commit exits non-zero
                     # when nothing is staged, which is a normal outcome here.
```

Three properties, each because of a specific way exemptions rot: **it names one gate** (a
blanket "ignore everything here" is how one awkward line suppresses checks nobody was
thinking about); **the reason is mandatory and ≥ 20 chars**; **they are counted**. The
count is the whole reason the process is safe to have — `python nightshift/gates/gate_appeals.py`
prints it, and `tests/test_self_improvement.py` bounds it.

Machinery in `nightshift/gates/appeal_markers.py`, enforced by the `gate_appeals` gate.

### The process must not itself be an unenforced prose rule

Which is the point. `unenforced-rule` is 4 entries and is the shape behind 12 more; writing
an appeal process as a paragraph describing how people ought to behave, in the session that
named that cluster, would have been the same mistake in the same document. So the appeal
mechanism has a gate, and the gate refuses a marker that names no gate, names a gate that
does not exist, or carries no reason.

### Used once, on a real gate, the day it was written

<!-- stale-ok: reconcile.py was removed 2026-09-23 (`remove-obsidian-leftovers`); this is the
     dated record of the gate's first run -->
`subprocess_result_checked` (§5) reported **6 violations on its first run**. Each was
verified by hand:

| Site | Verdict |
|---|---|
| `board.py` `move` — `git add -A Board/` | **real** — fixed |
| `board.py` `commit_board` — `git add -A -- paths` | **real** — fixed; this is the exact site of the 2026-07-23 bug |
| reconcile.py — `git add -A Board/` | **real** — fixed |
| `board.py` `move` — `git commit` | **appealed** (L1) |
| `board.py` `commit_board` — `git commit` | **appealed** (L1) |
| reconcile.py — `git commit` | **appealed** (L1) |

The appeal's reason, in full: `git commit` exits non-zero when nothing is staged, which is
a normal outcome for a board move that changed no bytes or that a previous run already
committed. The next `git add -A Board/` sweeps up anything left behind, so treating it as
an error would report a benign case on every run — and a gate that fires on a benign case
every run is a gate that gets muted. The `git add` above it is the call whose failure is
silent and costly, and that one is checked.

**Current appeal debt: 3.** That is the number to look at, and the number that should be
suspicious if it grows without anyone noticing.

Note what did *not* happen: the gate was not narrowed to skip `git commit`. Excluding
commit in the gate's own code would have been an L2 hiding as an implementation detail,
invisible in the count, and wrong the moment a `git commit` appears somewhere its failure
does matter.

---

## 5. Gates, in observed-frequency order

Per §15, selection follows observed failures. Frequency order, with what was built and
what was refused and why.

### Built: `subprocess_result_checked` — from `silent-noop` (5 with its sibling class)

A `subprocess.run(...)` under `.ai/` whose result is discarded. Scope is `.ai/` only —
that is where a silent failure costs a night, and game code does not shell out.

<!-- stale-ok: the digest and its generated report were removed in 2026-09 (`remove-obsidian`); this
     is the dated account of a bug they were involved in, kept as written because rewriting
     the names out of it would make the record describe something that never happened -->
The observed instance: `commit_board` staged `Board/` and `Digest.md` in one `git add`,
git fails the whole pathspec when one entry matches nothing, so in a fresh clone it staged
nothing and the commit was a silent no-op — `attempts` never reached disk and a crashed
card would retry forever. That was fixed **at the call site**, with an existence check on
`Digest.md`. It was never fixed **as a class**: every other reason `git add` can fail
(an `index.lock`, a permission error, a path outside the work tree) produced the identical
<!-- /stale-ok -->

wasted night, and five other calls had the same shape.

The rule is about the return value, not about git: *a subprocess whose result is discarded
cannot be distinguished from one that succeeded.* Assigning the result satisfies the gate,
which is deliberately weak — it does not prove the code goes on to read `returncode`, and
no cheap AST rule can. What it buys is that discarding is never the silent default.

Result: 3 real defects fixed, 3 legitimate appeals, and `board._warn_if_failed` now says
so on the run log when staging fails instead of letting the commit quietly no-op.

### Built: `corrections_log` — from this session's own read (§2)

Earned by a failure observed while doing step 2, not by ease. The log is the instrument
every §15 metric depends on, and it had become unclusterable without anyone noticing.

### Built: `gate_appeals` — the enforcement point for §4

See above. Not earned by a cluster of past defects but by the cluster's *shape*: an
appeal process with no enforcement is an `unenforced-rule` entry waiting to be written.

### Built: `event_posted` — from the `StairEvent` incident ([[unreferenced-symbol-gate]], 2026-08-02)

Not from this session's corrections-log clusters — earned by a single concrete failure
found while removing the stair remnants: `StairEvent` was an `Event` subclass with two
live subscribers and nothing that ever posted it, and `dispatch_reachability` stayed green
on it, because `bus.subscribe(StairEvent, self._on_stair)` **is** a reference by that
gate's own rule. Every dead-code tool asks the same question `dispatch_reachability` does;
none asks whether a declared `Event` is ever actually posted. That is a Project Tigress-specific
convention (events travel by `EventBus.post`), not a generic one, so it stays project-side
rather than moving to `nightshift` core alongside the dead-code gate the same triage
retired to [[dead-code-gate]].

Baseline **0 of 28** — every concrete `Event` subclass declared in `core/event_bus.py`
already has at least one `bus.post(SomeEvent(...))` call site somewhere under
`project_tigress/`. Cost measured at ~123ms marginal (one more `ast.walk` on `corpus.py`'s
shared cache), ~2.5% of the suite — the answer to "will every card take five minutes
longer" that motivated the measurement.

### Refused: a gate for `derived-not-verified` (12, the largest)

§3. Needs judgment; recorded as a procedure. This is the honest outcome step 3 explicitly
allows, and it is worth more than a gate would have been.

### Refused: a gate for `doc-staleness` (7)

Already gated by Session D — `doc_reference_liveness`, `doc_signature_drift`,
`deletion_sweep`. Nothing to add; every entry in the cluster predates them.

### Deferred with a measurement: the orientation-size budget — from `doc-hygiene` (3)

The log proposed this one itself: *"Candidate cheap gate for a later session: fail if the
orientation set exceeds ~60 KB"* (`memory-orientation-cost`, 2026-07-22). It was
measured before building, and the measurement is why it was not built:

| | post-Session-C (`3b2647e`) | now (2026-07-23) |
|---|---|---|
| `MEMORY.md` | 5.5 KB | **15.1 KB** |
| `state.md` | 5.3 KB | 7.3 KB |
| `arch.md` | 34.7 KB | 35.1 KB |
| **subtotal (what Session C measured)** | **45.5 KB** | **57.5 KB** |
| `design.md` — orientation per `CLAUDE.md`, **never counted** | 44.6 KB | 44.9 KB |
| **true orientation set** | **90.2 KB** | **102.4 KB** |

Two findings, and both are more useful than the gate would have been:

1. **Session C's headline number measured three files while `CLAUDE.md` names four**, and
   the omitted one is the second largest. "45 KB" was true of the set that was measured
   and was never true of the set that is loaded. This is itself a `derived-not-verified`
   instance, in the session that defined the term.
2. **`MEMORY.md` nearly tripled in one day** — Sessions D–G each appended their orientation
   paragraph, all of them reasonable, none of them looking at the total.

A 60 KB gate would therefore be **red on arrival by 42 KB**, and the only ways to green
are a compression pass that is not this session's scope, or a budget chosen to fit the
current size — which is the "weaken it until it passes" move §4 exists to stop. Filed as
[[orientation-size-budget]]: do the compression, *then* set the budget from
what the tree is afterwards, and the gate lands with the number meaning something.

<!-- stale-ok: `entrypoint_smoke` is specced in 08_staleness.md §6 and deliberately not
     built; it cannot resolve against the tree by construction, and saying so is the
     content of this section. Forward reference, not a stale one. -->
### Refused: `entrypoint_smoke` — from `tool-rot` (1)

Specced in `08_staleness.md` §6 and still not built. **The subject is now gone**: Karel
ruled the standalone launcher deleted rather than repaired, and that was executed
2026-07-30, so `main.py` is the only entry point left and `GameApp.run()` never hits the
frozen-loop path (`MainMenuScene` sits underneath). n=1 and the n is deleted, which retires
this row rather than deferring it again — the refusal is now permanent unless a second
entry point is ever added. Noted so it is not re-derived a fourth time.

### Not a gate: `judgment-not-mechanisable` (4)

By construction. These are Tier 2 candidates (§12's table), and the roster currently has
exactly one Tier 2 checker — `stale-hunter`. Nothing here has enough instances to earn a
second; recorded so the count is visible when it does.

---

## 6. The maintenance roles — cadence, and whether they are agents at all

§14 lists `rule-scout`, `gate-smith` and `recipe-keeper` as scheduled roles. Per step 5,
§16's two split rules were run on each before chartering — the way Session E did for
`closer` and `audio`, and with the same willingness to conclude "not an agent".

The two rules:

- **Overlap.** Can the handoff be a short payload rather than a briefing? If you would
  have to explain the codebase to the other side, do not split there.
- **Producer/checker.** Does the judgment need the context that produced the artefact? If
  it needs only the artefact plus the criteria, that is a boundary worth taking.

### `rule-scout` → **a skill, not an agent**

*Reads diffs of `CLAUDE.md`/`.claude/memory/` and Karel's corrections; produces detected
rules classified checkable/not.*

- **Overlap: fails.** Its input looks like a payload (a diff) and is not. Deciding whether
  a sentence is a new normative rule needs the existing 57-row matrix, the 15 feedback
  files it would duplicate, and enough of the codebase to answer "is this checkable" —
  that is a briefing. Worse, the diff is already in the lead's context at the moment it
  matters, because the lead is the one who just wrote it.
- **Producer/checker: does not apply.** It judges nothing that was produced; it reads a
  diff.
- **Verdict:** the work is real, the process boundary is not. It becomes a step in the
  close-out path, invoked at the point a memory file changes. That trigger already exists
  as the `memory_freshness` gate and its `PostToolUse` hook, so the skill fires from
  something mechanical rather than from recall — which was §12's requirement for it.
- **Cadence:** on memory-file change, i.e. per-diff, not scheduled.

### `gate-smith` → **not chartered; it is what a session does**

*Promotes prose → gate, retires gates that never fire, widens gates that miss.*

- **Overlap: fails, badly.** Needs the whole gate suite, the audit matrix, the correction
  log and the appeal debt in context simultaneously. That is the largest briefing on the
  roster.
- **Producer/checker: fails in the dangerous direction.** Gate authoring is the lead's
  own output; a `gate-smith` grading gates the lead wrote is an agent reviewing its own
  side of the seam, which §16 says is the weak case.
- **Verdict:** this session *was* `gate-smith`, run at the lead tier, and §14's own rule
  that these roles "propose; Claude approves" collapses to nothing when the proposer would
  need everything the approver has. Not chartered.
- **What is salvageable is the mechanical half**, and it is now real: `python
  nightshift/corrections.py` produces the cluster table this session's judgment was applied to,
  `python -m nightshift.audit` regenerates the enforcement matrix (`--check` exits non-zero on
  drift), and `gate_appeals` produces the debt count. All three are inputs a `gate-smith`
  or `rule-scout` would otherwise re-derive by reading; §12 says what Python can do,
  Python should do. `nightshift/audit.py` earned itself on its first run: the matrix claimed 11
  gates against 19 present, and `feedback_doc_leanness.md` had no row at all — a rule
  Karel gave that was never counted as unenforced.
- **Cadence:** whenever the correction log has grown enough to re-cluster. Concretely:
  when it has ~25 new entries, or after the first real overnight run, whichever is first.
  The second is the more important trigger — see §1's confound.

### `recipe-keeper` → **not chartered; revisit at three recipes**

*Watches git history against the recipe set.*

- **Corpus of one.** `.ai/recipes/` holds `add-a-perk.md` and, as of today,
  `verify-before-shipping-a-rule.md`. §12b's own honest caveat says the branch library may
  never pay for itself; a role whose entire job is spotting drift across a corpus of two
  has nothing to compare.
- **Overlap: fails** for the same reason as `gate-smith` — "does this recipe still match
  what commits touch" needs both the recipe and the commits.
- **Verdict:** not chartered. This is the same call Session E made on `audio` — a charter
  for a role nobody can usefully dispatch is exactly what not to write.
- **Trigger to revisit, written down so it is not re-derived:** three or more recipes, and
  at least one work type that has shipped five or more times. `plan-doc-extraction` on the
  board is the card that would produce the third recipe.
- **Trigger fired, 2026-07-27** (`recipe-keeper-threshold-met` card). Both halves are met:
  three recipes exist (`add-a-perk.md`, `verify-before-shipping-a-rule.md`,
  `ship-a-subsystem.md`), and work type #2 ("ship a multi-WP subsystem") has shipped five
  times. **Verdict unchanged: still not chartered.** Meeting the corpus-size trigger fixes
  only the first of the two reasons above; the overlap failure is architectural, not a
  threshold, and still applies at three recipes exactly as it did at one. Resolution
  mirrors `gate-smith`'s: not an agent, but the mechanical half is salvageable as a script
  a lead reads output from — carded as `recipe-drift-script` (enumerates `.ai/recipes/*.md`,
  diffs each recipe's named paths against commits since its `updated:` date, flags
  mismatches). The corpus-size trigger itself is retired — it never tested the overlap
  objection, only counted recipes, so it cannot fire again to reopen this question.

### `stale-hunter` — unchanged

Chartered by Session D, validated on a known-answer fixture (5 findings, 5 true, 0 false
positives, and one known drift missed). §7 of `08_staleness.md` owns its cadence. Nothing
here changes it. It remains the roster's only Tier 2 checker.

### The pattern across all three

All three §14 roles fail the overlap test, and they fail it for one reason: **maintenance
work is defined by comparing an artefact against the whole context it came from.** That is
the opposite of the producer/checker seam, where the checker is valuable *because* it is
denied the context. §16's second seam does not generalise to maintenance, and it is worth
recording that the roster in §14 was written before §16's seam existed and has not been
re-derived against it since — which is, once more, a `derived-not-verified` shape.

---

## 7. Not done, and why

- **Not run tree-wide.** Step 6, the same rule Session D applied to `stale-hunter`: the
  clustering was proven on the whole existing log (50 entries, which is the entire corpus,
  not a sample) but nothing was re-run across the memory tree or the plan docs off the
  back of it.
- **No card was dispatched.** Session H is judgment work and ran at the lead tier
  throughout; nothing here needed a worker.
- **The `karel` channel is not yet split** into design-time and morning-after. §1 says why
  it must be and why not yet.
- **The orientation-size gate is a card, not a gate.** §5.
- **The entry-point smoke gate is still unbuilt.** §5.

---

## 8. Second pass — wiring it in (2026-07-23, same day)

The first pass built the instruments; Karel's follow-up was to make the loop *automatic and
mandatory* rather than advisory, and to work the queued backlog. What landed:

**Correction capture, split by who judges.** A `UserPromptSubmit` hook was built to ask, on
every prompt, whether the message was a correction — then **unwired at Karel's call**. His
reasoning, and the log agreed: of three entries the hook prompted for in one conversation,
one restated a lesson from two days earlier. He is the only one who knows *local* ("you got
me wrong") from *systematic* ("this must not happen again"), so a Karel-channel correction is
now logged **on his explicit command only**; Claude/gate/test catches still log unprompted.
The `log-a-correction` skill carries the procedure; the hook file stays in the tree, off, and
a test fails if it silently returns. This is itself a `derived-not-verified` catch — the hook
was the obvious design and was wrong the first time it met real use.

**The pre-merge preflight — the one place the loop refuses.** `nightshift/preflight.py` runs the
gates, `audit --check`, a corrections-logged check (with an explicit `--no-corrections
"<reason>"` escape so an honest zero is written, not invented) and pytest, then
writes a SHA-keyed receipt. `nightshift/hooks/preflight_guard.py` (`PreToolUse`) **denies** `git
push` / `git merge` / `gh pr create` for an unvalidated commit. It gates *both* the
integration-branch merge and the push/PR, because the session-end merge is the last moment a
session's lessons still exist in context (§3). The guard is instant — SHA lookup only; the
expensive checks ran once. It fails open on anything it cannot parse (a guard that blocks on
confusion gets disabled).

The pytest step runs **the slice the branch can affect, in parallel, judged by its own JUnit
report** — `nightshift/suite.py`, the same module the runner and `merge_check` use (2026-07-27).
It did not originally: it shelled a full-suite `pytest tests/ -q` and judged the exit code,
which left the mandatory boundary as the slowest pytest in the project *and* the only one
that could not distinguish 0-collected from a pass. Two properties are load-bearing and
specific to this caller, which is the only one running against a live working tree rather
than a clean worktree: the changed-path set includes **uncommitted** edits (pytest imports
the tree as it is on disk, so an uncommitted `.ai/` edit must widen the slice), and the JUnit
report is **deleted before each run** (it reuses one path, so otherwise a pytest that died
before writing anything would be judged by the last run's green report). `--full-tests`
forces ALL; the receipt records which slice ran, so a narrowed validation is never mistaken
for a full one. A narrowing that made this boundary weaker would be the wrong trade — the
slice is safe only because the game/system wall is a real import boundary and because
"cannot classify" resolves to ALL, never to less.

**The audit is re-runnable** (§14b's load-bearing requirement). `nightshift/audit.py` regenerates
`01_audit_findings.md`'s headline and `--check` exits non-zero on drift. On its first run it
found the matrix claiming 11 gates against 19 present and `feedback_doc_leanness` with no row
at all. Four infrastructure gates are declared out of §A's scope *with reasons* rather than
given invented rows. Now 21 of 59 rules test-enforced.

**One ★ gate built, the rest reported.** All 13 remaining "prose+easy" audit rows were worked
through. One clean build (`help_catalog_shape`, row 8). The "easy" label was optimistic:
row 21's rule is *stale* (the tree has live `items/` and `audio/` dirs its allowlist omits, so
a gate would ship red — a wrong rule found instead of a bad gate written), four need judgment,
three need a call-graph walk, three were already known hard. §15's discipline, applied.

**`rule-scout` is a skill, not an agent** (§6's verdict, now implemented). Its mechanical half
is `nightshift/audit.py`; the skill is the judgment half, fired by the `memory_freshness` hint.

**The staleness sweep is a runner phase.** `--stale N`, after the cards, spending only
leftover window. The selector (`nightshift/stale_sweep.py`) is deterministic and change-ordered — a
doc whose named source has not moved since it was verified is skipped, which is what makes a
~1.2M-token full sweep affordable by never running it. The ledger records a verified-at SHA
**only on a complete verdict** (Karel's rule: a cut-off sweep re-checks, never lies). Findings
become one `fix-stale-*` card per doc; the runner never edits a doc. The deterministic half is
tested; **no stale-hunter has run through the runner yet** — the live Tier-2 dispatch is
plumbed, not exercised, the same honest state Session G left the paid card dispatch in.
