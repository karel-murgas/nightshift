---
recipe: write-a-test-that-earns-its-place
kind: spine
updated: 2026-08-04
---

<!-- Shipped by nightshift as a template, then written here by `nightshift init`.
     It is yours now: edit it, and add the cases your own repo earns. -->

# Recipe — write a test that earns its place

For the moment you are about to add a test, or about to decide whether the ones you
have are enough. Not a style guide: everything below is a shape that let a real defect
through, in a suite that was green at the time.

**Why this exists.** Three separate mechanisms in this framework rest on your tests and
none of them can look inside one. `preflight` runs a slice of the suite and writes a
receipt; the push guard denies `git push` without it. `suite.parallel_args` owns the
invocation policy so no caller reinvents it. The runner runs the suite inside a worktree
before a card may leave `testing`. Every one of those asks the same question — *did it
pass* — and not one of them can ask whether passing meant anything.

The framework already refuses one version of that answer: `suite.check_junit` treats a
report of **zero tests collected** as a failure rather than a pass, because a selection
that matched nothing exits 0 and looks identical to success. This recipe is the same
refusal one level down. A test that asserts nothing worth asserting is a zero-collected
run that collects.

Every failure named below happened in one of the two suites this was written from, and
each was found the same way: by something breaking in production that the suite had
already been asked about.

## What a keeper has

**A name that is a sentence about behaviour**, and one behaviour per test. Not the
function under test — the claim being made about it. Real names, taken verbatim:

```
test_a_receipt_for_an_older_commit_does_not_unblock_the_new_tip
test_an_unknown_skip_name_is_an_error_not_a_silent_no_op
test_a_module_without_a_check_is_a_helper_not_a_gate
test_the_private_lane_is_still_absent_from_lanes
test_a_cross_repo_push_is_allowed_when_the_target_itself_is_validated
```

Each says what is true and, in four of the five, what the tempting wrong answer was.
When one of these goes red the report line alone is a bug description. Against:

```
test_push_guard
test_uninstall_works
test_gate_ok
```

**A docstring that says what failure earned it** — not what the code does. The code is
right there; what is not is *why anyone thought this was worth pinning*. One from the
guard tests, in full:

> The old hook allowed anything it could not lex. That is the hole restored silently:
> an unreadable command is exactly when the guard knows least.

and one from an installer test:

> A `.gitattributes` has no prose. An unprefixed sentence is a PATTERN with attributes —
> the first draft of this block declared `text` on files named `Normalise`, which git
> parsed without complaint.

Neither describes the assertion. Both describe the moment somebody was wrong, which is
the only thing the next person to break this test needs and cannot recover from the
source.

**But not every test carries an incident, and the numbers say so.** Counted over both
suites the day this was written: 188 of 533 test functions in one and 1406 of 2027 in
the other have no docstring at all. That is not 1594 lapses. It is what happens when the
name has already said the whole claim and there is no story behind it — a boundary, a
default, a second value of an enum.

What *is* invariant: **every one of the 138 test files in both suites has a module
docstring.** That is where the incident lives when one bug shaped a whole file. From a
cache added to the checker layer, where 13 of the 16 tests below it are deliberately
bare:

> The tests that matter here are not the hit-rate ones — those only prove it is fast.
> The load-bearing tests are the *invalidation* ones, because the whole risk of adding a
> cache to a checker is that a gate starts answering about a file as it used to be. A
> gate that passes on a stale parse would be invisible: the run goes green, which is
> what everyone expected anyway.

Put the reasoning where the reasoning belongs. A file defending one property says it
once at the top; a test defending one incident says it on the test.

## The spine

| # | Step | What it produces |
|---|---|---|
| 1 | **Say the claim in one sentence.** That sentence, with underscores, is the test name | the name |
| 2 | **Say where the sentence came from** — a bug, a report, a decision, a boundary. If the honest answer is "the code does this", you are restating rather than testing | the docstring, or a deliberate silence |
| 3 | **Build the fixture from the state the bug lived in**, not from the cleanest state that reaches the code | a fixture that can hold the defect |
| 4 | **Assert on the artefact, not on the report about it** — the file, the tree, the returned object, the pixels; never the count | the assertion |
| 5 | **Add the mirror case.** What must still be allowed, still be kept, still pass | a second test, named for what it permits |
| 6 | **Watch it go red.** Break the production code, run it, put it back | proof the test's subject is the code |
| 7 | **Where the invariant is a set, assert set equality against whatever generates it** | a test that fails when something is *added* elsewhere |
| 8 | **Run the file, not just the test.** A fixture that leaks state passes alone and fails third | a clean run of the whole file |

Steps 3, 5 and 6 are the ones nobody does, and they are the three that catch the failures
below. Step 7 is worth its row on evidence: an installer built the board by iterating a
list that deliberately omitted one lane, and the lane nobody created was invisible until
a test asserted set equality instead of `the directory exists`. The same assertion now
guards the shipped recipes, and it is the reason adding this file to the templates makes
a test fail until somebody is told about it. A test that only fails when something is
*removed* is half a test.

## The four ways a test passes and teaches nothing

### 1. The fixture is too clean to contain the bug

`uninstall` decided what to delete by asking the installer what it writes and
intersecting that with the disk. After a run, *our* files exist — so the second pass
files them under `kept`, the same bucket as a `CLAUDE.md` or a `.gitattributes` the
project already had and the installer deliberately left alone. In any repo that was not
empty, uninstall deleted the operator's own documents.

Nine tests covered `uninstall`. All nine passed. Every one of their fixtures was a
**fresh repo**, where `kept` is empty — and in the empty case the wrong list and the
right list are the same list.

That is the general shape and it is not about uninstall: **the degenerate case is where
the wrong algorithm and the right one agree.** A fixture is built to reach the code, and
the cheapest fixture that reaches the code is almost always the one where the
distinction under test has collapsed.

**Do:** before writing the fixture, name the two states the code must tell apart, then
check that your fixture is not sitting on the line between them. If the function's job
is to distinguish *ours* from *theirs*, the fixture needs some of theirs. The repair
here was nine tests re-run through a repo carrying four documents of its own.

**And watch the second-order version:** a guard was written and correct for the case
where a merge-base could not be resolved, and did nothing when the merge-base resolved
to `HEAD` itself — a real, resolving base and a vacuously empty diff. "Did this resolve?"
is not the same question as "did it resolve to something with information in it".

### 2. The corpus is the author's own understanding

A rule was validated against three documents its author wrote. They agreed with it,
because the same hand wrote both. Its first contact with a real repo produced a false
positive on a file that was doing exactly the right thing.

This is not a testing problem you can test your way out of, and it has its own spine:
`verify-before-shipping-a-rule`, whose steps 3 and 4 — run the rule against every member
of the real corpus, report the count including the zeros — would have caught it in one
command. Go there when what you are shipping is a rule. What belongs *here* is the tell:
**if the inputs in your test file were typed by the same person who wrote the code, the
test measures agreement and not correctness.** Fixtures copied verbatim out of real
artefacts are worth their awkwardness for exactly this reason.

### 3. It is drawn from the threat model and never from the traffic

The push guard was rewritten to resolve the target repository from the command rather
than from the session, and to fail closed on anything it could not attribute. Correct
policy, verified against 18 hand-piped payloads and 24 tests — every one of them a
*command*: a push, a push after `cd`, a push in a subshell, a push through `xargs`, an
unterminated quote.

None of them was a command whose **prose** mentions pushing. The first thing it met in
real use was the commit of its own rewrite, whose message described the incident and
contained both the words `git push` and an apostrophe. The heredoc body made the command
look guarded, the apostrophe made it unlexable, and it denied a commit that pushed
nothing. Then it denied the script written to investigate that.

**A guard is tested against what it must catch and lives against what it must let
through.** The corpus that matters for a deny-by-default rule is the ordinary traffic —
and for anything reading commands or text, that means the documentation of the rule
itself is the input most likely to trip it.

**Do:** for every test asserting a refusal, write the neighbour asserting a permission,
and take its input from something that already exists rather than inventing it. Those
neighbours are named for what they allow, and the suite carries several with the reason
attached — *"the counterweight to the test above: a grep over a file that mentions
pushing must not be denied, or the guard becomes noise and gets removed."* The cost of
the false positive is the whole mechanism: the author learns to work around the check,
then to disable it.

### 4. It asserts on the summary line rather than on the thing

The gate runner ends with one line: a count, plus the name of every gate that ran. The
test suite's own report ends with `N failure(s) across M test(s)`. Both are true, both
are useless, and both were being read as the verdict:

* the receipt records that last gate line — a count and 21 names, 400 characters that
  never say which gate failed — so the code that hands failures to an agent has to ask
  the runner a second time and keep only the lines naming a path;
* the failure count *was* the whole test verdict until the day somebody needed to know
  which test, and the first failing test's name now rides along from the report that was
  already parsed.

Test-side the same shape has a visible scar: one test has to assert that a skipped gate
is absent **from the output minus its summary line**, because the summary names every
gate including the skipped one. Read whole, the output "contains" the thing the test is
checking is not there.

**Do:** assert on the smallest artefact that carries the claim. A count passes for the
wrong reason as easily as the right one — `2 failures` is equally true when the two
failures are different failures. And when the artefact really is text, assert on the
line that names a path, a symbol or a remedy, never on the total.

**The remedy in a message is an artefact too.** A gate spent weeks telling every
installed repo to run a script that existed in exactly one of them; the fix was a test
asserting that what the message names can actually be resolved from a consuming repo.

## Can any of this be a gate? — the decision, and why

**No gate ships with this spine.** Two mechanical candidates were measured, and they
fail for two different reasons. Both are recorded here so the question is not reopened
from scratch.

**Candidate A — a docstring on every test function.** Measured over both source suites:
188 of 533 and 1406 of 2027 have none. A gate shipping into either goes red on its first
run with four figures of violations, which is the definition of muted on day one, and
`turn-a-correction-into-a-gate` names that as the first way a new gate dies. Worse, it
would be **wrong**: the bare tests are largely bare on purpose, under a module docstring
that has already said what the file defends. No deterministic reader can tell a
deliberate silence from a forgotten one, which by that spine's own step 0 makes this
`no-judgment` rather than `no-gap`.

**Candidate B — a docstring on every test module.** Measured the same day: 138 of 138
test files in both suites already have one. Zero violations on the day it ships. It is
mechanically clean, it holds an invariant rather than announcing a backlog, and it is
about 15 lines.

**It is still not shipped**, and the reason is doctrine rather than noise: **no entry in
either corrections log records a missing module docstring costing anything.** A gate is
earned by an observed failure in the repo that runs it, never adopted because it sounds
prudent — and "we measured that everybody already does it" is not a failure, it is a
compliment. Shipping candidate B would put a rule nobody earned into every consuming
repo, which is the suite-nobody-reads-the-output-of problem arriving pre-installed.

**So it is yours to write, the day you earn it.** When a file lands with no module
docstring and a session later cannot tell what it was defending, that is the incident;
follow `turn-a-correction-into-a-gate`, and note that its step 4 is already done for you
above. Your gate goes in `.ai/gates/`, where a rule this repo earned belongs.

## Don't

- **Do not accept "pre-existing failure" without checking which commit was called
  "before".** A delegated result once reported two red tests as pre-existing, having
  correctly stashed and re-run — against a baseline that already contained the change in
  question, because an editable install makes a dependency's `HEAD` live in the consuming
  project the instant it is committed. A clean bisect against the wrong baseline is
  indistinguishable from a clean bill of health, and the claim to check is the baseline,
  not the procedure.
- **Do not make one test assert five things.** When it fails you learn that one of five
  broke, and only the traceback says which — so the name, which is the part everyone
  reads, has stopped being true. Split it, and let the mirror cases have their own names.
- **Do not reproduce the implementation's own string-building in the assertion.** A test
  that rebuilds what the generator generates breaks on every legitimate addition and
  passes on every wrong one that both halves make together. Assert the *shape*: that
  every declared item appears, not that today's exact rendering does.
- **Do not turn a red check green by removing the test.** Deleting it, marking it
  `xfail`, narrowing its fixture and skipping it are the same move, and each leaves the
  suite reporting a coverage it no longer has. This framework's own repair pass forbids
  that list by name because an agent told to make checks pass will make them pass.
- **Do not leave a test you have never seen fail.** Until you have broken the code and
  watched the report go red, the only proven claim is that the test does not crash.
