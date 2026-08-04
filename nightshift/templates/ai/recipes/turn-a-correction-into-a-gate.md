---
recipe: turn-a-correction-into-a-gate
kind: spine
updated: 2026-08-04
---

<!-- Shipped by nightshift as a template, then written here by `nightshift init`.
     It is yours now: edit it, and add the cases your own repo earns. -->

# Recipe — turn a correction into a gate

For the moment a correction has been logged and you are deciding what, if anything, runs
because of it. Not for writing a gate you thought of in the shower: a gate is earned by an
observed failure **in this repo**, never adopted because it sounds prudent.

**Why this exists.** The loop this framework is built around has three steps — something
goes wrong, a correction is logged with its generalisation, and *if the shape is
mechanically detectable, a gate*. A fresh install ships the machinery for the first two
(an empty `.ai/corrections.log`, an empty vocabulary, the `log-a-correction` skill) and
`gate_appeals` for when a gate turns out to be wrong. This is the missing third step. It
is the engine: the gates you end up with are the only part of the system that keeps
working when nobody is paying attention.

Every failure named below happened to a gate in the shipped set, and each was found the
same way: by running the gate against a real corpus instead of reasoning about it. None
of it is hypothetical, and none of it was obvious to the person who wrote the gate.

## Step 0 — is this a gate at all?

The `GATE` field in the correction line has already asked you this, and its vocabulary is
the triage:

| Value | Meaning | What to do |
|---|---|---|
| `yes` | an existing gate would have failed on this | nothing — check *why* it did not run |
| `yes-now` | a gate built or fixed by this correction now catches it | this recipe, and you are on it |
| `no-judgment` | genuinely needs human or LLM judgment | write a recipe, a charter line or a skill |
| `no-gap` | mechanically catchable in principle, no gate yet | file a card; do not pretend it is done |
| `n/a` | not a defect | nothing |

**The test that separates `no-judgment` from `no-gap`:** can a deterministic reader see the
violation *in the artefact*, without knowing what the author meant? If answering needs the
intent, it is not a gate. The largest correction class in the origin project —
`derived-not-verified`, a rule reasoned out carefully and wrong the first time reality
touched it — has **no gate and cannot have one**, because "this rule was never run against
its corpus" leaves no trace in the rule. That is why
`verify-before-shipping-a-rule` is a recipe instead.

Being honest here is the whole of step 0. A gate written for a judgment problem fires on a
proxy for the thing it cares about, and the next section is what happens then.

## The spine

| # | Step | What it produces |
|---|---|---|
| 1 | **Quote the generalisation.** The correction's "what generalises" sentence is the specification. If you cannot restate it as *"no file in X may Y"*, stop — go back to step 0 | one sentence, in the gate's docstring |
| 2 | **Name the corpus, and say how the gate finds it.** Not a literal path: a manifest field, a `git` query, a glob rooted in one | the scope expression |
| 3 | **Write `check` so absence is loud.** Every early return is a claim that there is nothing to check; each one must be true for a reason you can state | a list of the guards, each with its reason |
| 4 | **Run it against the whole corpus and report the count, zeros included** — how many pass, how many it *rejects*, and which ones | the number, recorded in the docstring |
| 5 | **Reproduce the original failure.** Rebuild the artefact that started this as a fixture, watch the gate go red, then fix it and watch it go green | a test that fails without the gate |
| 6 | **Assert the gate's reach**, not only its verdict. A test that the scope resolves to something | a second test |
| 7 | **Write the reason into the docstring** — the incident, the measurement, the threshold and why that number and not another | the module docstring |
| 8 | **Close the correction.** Append `[[disposition: gate: <the gate name>]]` to its NOTE | the log entry, resolved |

Steps 4 and 6 are the two nobody does, and they are the two that catch the failures below.
Step 4 is `verify-before-shipping-a-rule`'s step 4: a gate is a rule, and a rule that
rejects nothing usually says nothing.

## The four ways a new gate goes wrong

### 1. It fires on the word rather than the claim, and is muted within a week

`orientation_shape` enforces *"an orientation document says what is true now; history goes
in a companion"*. It shipped matching an ISO date **anywhere** in a heading. Its first
contact with a real repo, minutes later, flagged a 40 KB decision register whose headings
are subsystems with the decision date attached — `## Traps (decided 2026-07-17)` — which
is precisely the shape an orientation document *should* have.

The claim is "the session is the subject". The word is "there is a date here". The
corrected rule anchors the date at the **start** of the heading text, which is exactly the
difference between the two. Counted over 30 real documents: the shipped rule rejected 4,
the corrected rule rejects 1, and that one is a file the gate never reads.

The asymmetry is what makes this the first failure mode rather than the fourth. A false
negative costs you one missed violation. A false positive costs you the gate: the author
learns to reach for an appeal marker, then to skip the gate, then to skip the suite. The
origin project's advisory hint hook carries the same note above its rule table, for the
same reason — *"deliberately narrow: a hook that fires on every edit gets tuned out,
which costs more than it saves. Add a row only when something was actually forgotten
without it."*

**Do:** pick a threshold that makes the rule about the shape and not about the vocabulary,
and write down what the threshold is protecting. `orientation_shape` fires at three dated
headings, not one, because one is a note; and on headings only, because prose recording
*"decided 2026-07-24, because…"* is not the failure.

### 2. It fails open, and reports success while reading zero bytes

`run_stop_recorded` targeted a file that had been deleted in a refactor. Its `check`
opened with *no such file, therefore no violations* — so it reported green, in every repo,
for as long as it had shipped, without reading a byte. In the same move, four discipline
gates carried a scope of `Path(".ai")` after ~16,800 lines moved out of that directory;
two more were filtered on a literal package name and were no-ops in any other repo.

Every one of those is a `check` that cannot fail. Nothing in the output distinguishes
"nothing is wrong" from "nothing was read", because both print the same thing: nothing.

**Do:** treat every early return as a claim. There is a legitimate version of this —
*"absence disables it"*, the rule every manifest-driven gate here follows, where a project
that has declared nothing gets no opinion. The difference is that the disabling condition
is a **declaration** the operator made, not a path that happens not to exist. If the gate
returns early because something it expected is missing, that is a violation, not silence.

**And check the degenerate case, not only the absent one.** A guard asking *"did this
resolve?"* does not cover *"it resolved to a trivial answer"*: the preflight's
merge-base guard was written and correct for a fresh clone with no merge-base, and did
nothing when the merge-base resolved to `HEAD` itself — a real, resolving base and a
vacuously empty diff, so a branch that had logged several corrections was reported as
having logged none. It cost five hand-written excuses in one day before anyone looked.

### 3. Narrowing the scope looks exactly like success

A gate that scans fewer files reports fewer violations, and fewer violations is
indistinguishable from progress. That is why the seven gates above survived: each scope
was correct when written, and none of them looks wrong afterwards.

The same asymmetry makes narrowing the tempting *repair*. A gate goes red on something you
believe is fine; the smallest edit that turns it green is to stop looking there. That edit
leaves no record, and the gate never fires on that class again.

**Do:** whenever you change what a gate reads — a scope, a threshold, a glob, a default
behind a new config seam — print the finding count before and the finding count after, and
put both in the commit message. A change that reduces findings is a claim, and a claim
needs its number.

**Do not fix a red gate by weakening it.** If the violation is not real, appeal it: a
`# gate-ok(<gate>): <reason>` comment naming the one gate and carrying a written reason,
which `gate_appeals` validates and counts. An appeal leaves a record where a narrowing
leaves a hole, and the count is what makes the mechanism safe to have at all.

**Watch config seams especially.** Moving a hardcoded default behind a manifest field
changes the default for **every caller that supplies no config** — and a synthetic test
fixture is exactly such a caller. Keep the fallback behaviour-identical to the value it
replaced, or go and update every fixture that was relying on the old implicit default.

### 4. A rule tested only against its author's examples has been tested against its author

`orientation_shape` was validated against three synthetic documents. They agreed with the
rule, because the same hand wrote both. That is not a test; it is a restatement.

The rule was also *correct about the incident it came from* — an orientation file that
reached 196 KB one dated section at a time — and a rule that explains its originating case
feels verified by it. It is not. The originating case is one member of the corpus, and it
is the member the rule was fitted to.

**Do:** run the finished gate against the real corpus and print the count. Four rejections
out of thirty, two of them obviously fine, is visible the moment it is printed and
invisible until then. If the corpus has fewer than three members, say so out loud — that
is a sample-size warning, not a pass.

## Where the gate goes, and what it has to be

Your gates live in `.ai/gates/` and never move into the package. Core gates ship with the
framework; a rule this repo earned from its own incident belongs to this repo. An empty
`.ai/gates/` is the correct starting state, and a gate name that collides with a core one
is an error rather than a precedence rule — rename yours, or appeal theirs.

A gate module is a plain `.py` file in that directory. There is no registration: the
runner globs the directory, and a module is a gate exactly when it has a `check`. Its
**filename is its name** — the name that appears in the run output, in a `# gate-ok(...)`
appeal, and in a `[[disposition: gate: ...]]` pointer.

```python
"""Gate: <the generalisation, in one sentence>.

<The incident. The measurement. The threshold and why that number.>

<The corpus, counted: how many pass, how many this rejects, and which.>
"""
from pathlib import Path

from nightshift.gates.base import Violation

# Optional, and the only module-level name anything reads: the gate list in the
# framework's README. Left out, the docstring's first sentence is used instead.
DESCRIPTION = "one line, saying what the gate checks"


def check(repo_root):
    out = []
    for path in corpus(repo_root):           # step 2 — never a literal path
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if offends(line):                # step 1 — the claim, not a word
                rel = Path(path).relative_to(repo_root).as_posix()
                out.append(Violation(rel, lineno, "my_gate: what is wrong, and what to do"))
    return out
```

Deterministic Python only — no LLM calls anywhere in that directory. Violations name a
file, a line and a rule; the rule text is read by whoever the gate stops, so it says what
to do and not only what is wrong. Neighbouring modules in `.ai/gates/` are imported flat
(`import my_helper`), because that directory cannot be a package; the framework is
imported by package path. A helper with no `check` is not a gate, which is why adding one
has never required editing a list.

Run it with `python -m nightshift.gates.run`, or by name.

## Don't

- **Do not adopt a gate because it sounds prudent.** Selection follows observed failures.
  A suite full of rules nobody earned is a suite nobody reads the output of.
- **Do not re-derive what an authority already answers.** A gate that shells out once per
  file is nearly always a bulk query spelled as a loop — `line_endings` walked the tree and
  ran one `git` call per tracked file, 25.2 seconds and 76% of the whole suite, where
  `git ls-files --eol` answers in 0.61s. The correctness half matters more than the 41x:
  the hand-rolled binary sniff was reimplementing a decision `.gitattributes` already
  makes, so a file the project had *declared* binary was being sniffed anyway.
- **Do not write the gate and leave the correction open.** The disposition pointer is how
  anyone later knows this class is closed, and an invented disposition is worse than none.
- **Do not add a class to the corrections vocabulary just to file this one.** The
  vocabulary is closed on purpose and every declared value must be used by some entry. If
  nothing fits, say so in the note and propose the class deliberately.
