---
recipe: verify-before-shipping-a-rule
kind: spine
updated: 2026-08-04
---

<!-- Shipped by nightshift as a template, then written here by `nightshift init`.
     It is yours now: edit it, and add the cases your own repo earns. -->

# Recipe — verify before shipping a rule

For any change that ships a **rule**: a definition, a schema field, a flag, a frontmatter
convention, a lane policy, a charter constraint, an ordering guarantee. Not for code that
does a thing — for text that says what a thing must be.

**Why this exists.** In the origin project, `derived-not-verified` was the largest class
in `.ai/corrections.log` — 12 of 50 entries, 7 of them found by the maintainer rather
than by anything that runs. Every one is a rule that was reasoned out carefully, written
down confidently, and was wrong the first time reality touched it. None of them is a
coding mistake; the code implementing them was correct in every case. There is no pattern
for a gate to match, so this is a step instead.

Your log starts empty and that class is not yours yet. The recipe still ships, because
the failure is a property of writing rules and not of that project's subject matter —
and because the first time you meet it, the cost is a rule already in use.

## The spine

| # | Step | What it produces |
|---|---|---|
| 1 | **Name the corpus** the rule governs — every card, every charter, every call site, every doc that will be read against it | a list, written down |
| 2 | **Enumerate it.** `ls`, `glob`, `grep` — do not recall it | a count |
| 3 | **Run the rule against every member by hand.** Not the ones that come to mind; all of them | a verdict per member |
| 4 | **Report the count, zeros included** — how many pass, how many the rule *rejects*, and what the rejected ones are | the number that goes in the commit message or the card |
| 5 | If the corpus has **fewer than three members**, say so explicitly | a sample-size warning, not a pass |
| 6 | **Walk the rules derived from this one.** Grep the docs for restatements and check each against the new wording | a list of edits, or "none found" |

Step 4 is the one that pays. A rule that rejects nothing is usually a rule that says
nothing; a rule that rejects half its corpus is usually wrong. Both are invisible until
the corpus is counted.

## The three failures that earned each corollary

- **Agreement within a sample is not confirmation.** The card schema was derived from two
  cards that agreed; the third card in the tree would have been rejected by it. Two
  agreeing samples read as evidence and were one sample.
- **Fixing a definition does not fix the rules derived from it.** The `unattended:` card
  field was redefined, and a design note one layer down went on stating the old definition
  with its own reasoning, reading as independently authoritative. Step 6 exists for this.
- **"Known limitation" written more than once in a document is a trigger.** Four of one
  design note's unsolved problems had a single cause, and it took the maintainer asking a
  question that touched all four to surface it. If you write the phrase twice, stop and
  ask what the two have in common.

## Don't

- **Do not treat "the code implementing it is correct and tested" as verification of the
  rule.** All 12 entries passed that bar. The tests test the implementation; step 3 tests
  the rule.
- **Do not defer the check because the rule "is obviously right".** Two of the 12 cost
  more to write up and route to a future session than the check would have cost.
- **Do not promote a fact about a doc into a fact about the world.** A host configuration
  put a GPU on the wrong machine because a design note described the project's *target*
  hardware and that was read as a description of the box the session was running on. Where
  a rule's input is a claim about the physical world, a person confirms it — no test can.
- **Do not treat a doc's description of the code as evidence the code exists.** When asked
  whether a previously-requested change works, verify with `git log -S <symbol> -- <the
  file that would have changed>`, never the doc that describes it — the doc is what the
  last session *believed*, written by the same process that may have skipped the work. The
  card field `kanban_order` was documented as a considered "read by nobody" decision and
  repeated back as deliberate; the code implementing it did not exist. A doc that
  confidently states a settled decision is the strongest possible disguise for work that
  never happened.
- **Do not design around a constraint before checking whether a component you already
  depend on solves it one layer up.** A budget, a limit, a missing capability — before you
  build a knob for it or hand the decision to the maintainer, look in the module you have
  already read. A doc-staleness sweep was designed with a manual "you pick N docs" budget
  because no session/spend limit could be read, when the runner it dispatches through
  already carried `--max-budget-usd`, `--until`, a kill switch and per-item resumability.
  "The human will supply X" is a design smell when X exists in code you have already seen.

## What the gates already guarantee

- `card_schema` runs over **every** card on every edit, so a schema change is checked
  against the whole board rather than against the cards someone happened to open. Step 3 is
  already done for that one corpus.
- `corrections_log` keeps the log clusterable, which is what makes this recipe's evidence
  base countable next time.
- `gate_appeals` means a rule relaxed at one site is counted rather than quietly dropped.

Nothing else is covered. Steps 1, 2, 4, 5 and 6 are yours.

## After the rule holds

If the rule turned out to be mechanically checkable, it wants a gate rather than a
paragraph — `turn-a-correction-into-a-gate` is the spine for that, and its step 4 is this
recipe's step 4 wearing a different hat.
