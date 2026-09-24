# Doc 12 — Corrections-log lifecycle: harvest trigger + compaction

**STATUS: SHIPPED, 2026-07-26.** Renumbered from 11 to 12 on build — `SESSIONS.md` had
already reserved doc 11 for the (unwritten) Session J audio pipeline design, and this file
predates that reservation only by accident of when it happened to be written the same day.

## 1. The problem

The self-improvement loop (doc 10) has three steps: **capture**, **report**, **act**.

- **Capture** is automatic and mandatory — the pre-merge preflight refuses a merge/push/PR
  unless the branch touched the log or recorded a reasoned zero.
- **Report** is a script — the corrections reader clusters by class, channel and gate verdict.
- **Act** — turning the accumulated lessons into system changes — is the step with no
  machinery. It happens only when a human says "learn now". The first time was Session H
  (2026-07-23); this pass (2026-07-26) is the second. Between them the log grew from 50 to 78
  entries and nobody looked.

Two gaps follow from that, and they are Karel's two words: *automatic* and *clear up*.

1. **Harvest is not periodic.** Nothing notices "N un-acted-on lessons have piled up". The
   trigger is a person remembering — the most expensive channel, which is the exact metric the
   loop exists to drive down.
2. **The log never compacts.** It is append-only, ~85 KB / 78 entries, and the reader has no
   notion of "resolved". An entry that already became a gate sits forever beside a fresh,
   un-harvested one, indistinguishable from it. The file that is meant to be read back grows
   without bound, which is how it "nearly failed at the format" on its first read (doc 10 §2).

Constraint carried from Karel, and it rules out the obvious wrong answer: **improve the
system, do not add more code, checks or LLM calls.** A nightly LLM harvester on every merge is
exactly what this must not be.

## 2. What today's pass proves about the shape of the fix

The 2026-07-26 harvest read all 78 entries across five themes. Result: the overwhelming
majority were **already handled** — every anchor traced back to a shipped gate, recipe,
charter rewrite or structural fix that the capture→act loop had already produced at ship time.
The whole pass surfaced roughly six small residual actions, and almost all of them *improved
an existing mechanism* rather than added a rule.

That is the design input. The backlog a periodic harvester works is **usually small**, because
most lessons are absorbed the day they are logged. So the periodic job is mostly to confirm
"nothing to do" cheaply, and to catch the occasional cluster before it rots (the 124-day
staleness incident is the cost of not doing that). This means the harvest judgment does **not**
need automating — only the *visibility of the backlog* and the *leanness of the log* do. Both
are mechanical.

## 3. The design (mechanical; zero new LLM calls)

Three parts, each bookkeeping.

**a. A resolved marker.** Each entry gains a disposition: the durable change it produced — a
gate name, a recipe, a commit, or an explicit "no-action: judgment-only". A closed vocabulary
for the disposition kind, free text for the pointer, validated the same way the existing class
/ channel / gate fields already are. It is written by whoever ships the change, or by the
harvest session. This is the field the reader is missing: it separates "acted on" from "open".

**b. Compaction.** Resolved entries move out of the active log into a sibling archive file,
leaving the active log holding only open lessons. This is the same move already made for the
memory tree (the orientation files shed their history into companion files). The reader loads
both for historical counts, but the file a human opens stays small. *Alternative:* keep one
file and only add the disposition field (no archive). Simpler, but the log keeps growing —
which is half the problem. **Recommend the archive.**

**c. A backlog count, surfaced where it is already read.** The reader reports how many entries
are open. The morning digest prints that count and, past a threshold, one line: "harvest
backlog — consider a learn-now pass". That line is the trigger that replaces "Karel
remembers". No new surface, no new run — it rides the digest that already renders every
morning.

The harvest itself stays **on demand and human-run**, exactly like today. The loop only makes
the backlog visible and keeps the log readable.

## 4. Why not more

- **Not a nightly LLM harvester.** New model calls on every merge — the thing ruled out — and
  §2 shows the backlog is usually too small to justify it.
- **Not a new gate.** Nothing here needs *refusing*. Capture is already refused at the merge;
  this is a counter and an archive move downstream of that.
- **Not a new agent.** The judgment is the human's; there is nothing for a worker to do that a
  count and an archive-move script do not.

## 5. Decisions (Karel, 2026-07-26)

1. **Encoding: the archive file.** Not a disposition-only single-file scheme — the log
   staying lean was half the original problem, so a design that leaves it growing forever
   doesn't fix it.
2. **Threshold: both.** The digest nudges when the open-backlog *count* crosses a bound, or
   when the *oldest open entry's age* does, whichever fires first — a small pile of very old
   entries is exactly as worth surfacing as a large pile of fresh ones.
3. **Who writes the disposition: the shipping session, at merge time.** It is the session
   that knows what durable thing the correction produced (a gate, a recipe, a commit), and
   writing it there means the fact is captured while it is still cheap to state, the same
   argument `10_self_improvement.md` §3 makes for capture itself. Deliberately **not** wired
   into the preflight's refuse logic — §4's "not a new gate" applies here too: nothing about
   a disposition needs *refusing*.

## 6. What was built

- **The disposition annotation.** A resolved entry's NOTE ends with
  `[[disposition: <kind>: <pointer>]]` — `kind` from a closed vocabulary
  (`gate | recipe | commit | doc | no-action`) in `.ai/gates/data/corrections_vocab.json`,
  `pointer` free text (a gate module, a recipe file, a commit, a doc, or the reason for
  deliberately not acting further). **Not a seventh pipe field** — several existing notes
  contain a literal `|` (`[grid|classic]`, a quoted table row), so widening the split would
  have silently corrupted them. `[[` never occurs in the log, so it's a safe anchor;
  `corrections.py` strips it back out of `.note` and into `.disposition` after the existing
  five-pipe split, which is completely unchanged. An entry with no annotation parses exactly
  as it always has — `disposition == ""`, i.e. open. Zero of the 80 entries that existed
  before this shipped were touched or backfilled; retroactively assigning dispositions is
  its own judgment-heavy pass, out of scope here.
- **The archive.** `.ai/corrections.archive.log`, sibling to the active log, same six pipe
  fields. `python -m nightshift.corrections --compact` moves every line whose NOTE carries a
  disposition out of the active log and appends it verbatim to the archive (creating it with
  a header on first use). `corrections.report()` reads both for historical cluster counts;
  `corrections.backlog()` reads only the active log, so the archive can never itself
  contribute to a backlog. Gate `corrections_log` additionally checks the archive when it
  exists — it must still parse, and every entry in it must actually carry a disposition
  (otherwise something was compacted that shouldn't have been).
- **The harvest nudge.** Command Center's *System* page shows `Corrections to harvest` (and
  counts it on the rail) whenever `nightshift.corrections.harvest_due` says so. It was a
  `**Harvest backlog:** …` line in the morning digest until 2026-09, in the standing
  `## Still waiting on you` half, alongside the other "why work is not happening" advisory
  (`Flag may be stale`), when `corrections.backlog()`'s count is
  `>= _HARVEST_BACKLOG_COUNT` (20) or its oldest-open age is `>= _HARVEST_BACKLOG_DAYS` (30
  days) — the "both" from §5. Silent otherwise, including on a repo with no
  `.ai/corrections.log` at all (a fresh `.ai/core/` extraction, per doc 07/Session K).
- **No change** to capture, to the preflight's refuse logic, to `log-a-correction`'s
  append-a-line contract, or to any gate's pass/fail semantics beyond the archive check
  above — exactly §4's scope.

Tests: `tests/test_self_improvement.py` (disposition parsing/validation, backward
compatibility with pipe-bearing legacy notes, `backlog()`, `compact()`, the archive
invariant) and nightshift's own tests/test_digest (the nudge fires/stays silent correctly).
