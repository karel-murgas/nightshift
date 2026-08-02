---
name: Log a Correction
description: Append one well-formed entry to .ai/corrections.log. Invoke when {{maintainer}} says to log something ("log this", "that should not happen again", /log-a-correction), or when Claude, a gate or a test catches a systematic problem after the fact. Never invoke to log a {{maintainer}} correction they did not ask to have logged.
---

<!-- Shipped by nightshift as a template, then filled in by nightshift init.
     Section references point at the framework's design notes, which deliberately do
     not ship; every rule they cite is stated in full where it is cited. -->

# Log a Correction

**Concern:** turn one correction into one honest, clusterable line in
`.ai/corrections.log`.

Nothing else. Not fixing the thing — do that too, in the same turn, but this skill is only
about the record.

## When to invoke — split by who is judging

**{{maintainer}}'s corrections: only on their explicit say-so.** They say "log this", "write that
down", "that should not happen again", or invokes `/log-a-correction`. Otherwise do not
log their correction, however obviously right it was.

> **Why (the origin project's maintainer, 2026-07-23):** *"I'm not sure you will correctly recognize when the
> correction is local ('you got me wrong!') and when it is systematic ('this should not
> happen again')."* They are right, and the evidence is in this log: of three entries logged
> automatically in one conversation, one (`explained-instead-of-fixed`) restated a lesson
> already recorded two days earlier as `deferred-instead-of-fixed`. **They are the only one
> who knows whether they will have to say it again**, which is the definition of systematic.
>
> This replaced a `UserPromptSubmit` hook that asked on every prompt. The hook file is
> still at `nightshift/hooks/correction_prompt.py`, unwired, if the decision reverses.

**Claude's, a gate's or a test's catches: log them without asking.** `channel: claude`,
`gate` or `test` means {{maintainer}} was not involved, so a marginal entry costs them nothing and
the recall is worth having. Still apply the local/systematic test below — to yourself.

**The local/systematic test.** A correction is *local* if it is about this one artefact and
nothing generalises: a typo, a wrong number, a misread file. It is *systematic* if the same
reasoning would produce the same mistake again on a different subject. **Only systematic
corrections belong here.** If you cannot write the "what generalises" sentence honestly,
that is the answer — do not log it.

## Why the record, not just the fix

`.ai/corrections.log` is the instrument every metric in §15 depends
on, and §15's whole argument is that **the cost of a defect here is {{maintainer}}'s attention**,
not a bad commit. A defect they catch twice is pure waste, and the only way anyone knows
it happened twice is if the first one was written down.

Session H clustered the first 50 entries and the largest class — `derived-not-verified`,
12 of 50 — was invisible until they were countable. That finding did not come from any
single entry. It came from having fifty.

## The line

Six pipe-separated fields, one line, no wrapping:

```
DATE | SLUG | CLASS | CHANNEL | GATE | NOTE
```

| Field | How to fill it |
|---|---|
| `DATE` | `YYYY-MM-DD`, today |
| `SLUG` | short kebab-case handle, the human name for this entry. Reuse an existing slug only if it is literally the same recurring problem |
| `CLASS` | from `.ai/gates/data/corrections_vocab.json`. **Read the definitions** — do not guess from the name |
| `CHANNEL` | who found it: `karel`, `claude`, `gate`, `test`, `audit`, `none` |
| `GATE` | `yes` / `yes-now` / `no-judgment` / `no-gap` / `n/a` |
| `NOTE` | the evidence. See below |

Validated by `nightshift/gates/corrections_log.py`, which runs on every edit. A malformed line
fails the gate, so a bad entry cannot sit there quietly.

## Choosing CLASS

Run `python -m nightshift.corrections` to see the current distribution, and read the vocabulary
file. Two that get confused:

- **`derived-not-verified`** — the rule, definition, schema or ordering was itself wrong,
  reasoned out without being run against the corpus it governs. The code implementing it
  was usually correct. **This is the largest class; suspect it first.**
- **`silent-noop`** — the rule was right and the mechanism did nothing while reporting
  success.

If nothing fits, that is a finding: say so in the note and propose the new class rather
than forcing an existing one. Adding a vocabulary value is a deliberate edit to the data
file, which is what keeps the list closed.

## Writing the NOTE — the part that carries the value

Minimum 40 characters, enforced. But length is not the point; **a note that only says what
happened is nearly worthless.** The entries that earned Session H's clusters all answer
three things:

1. **What was wrong, concretely** — with the file, the symbol, the number.
2. **Why it was believed** — the reasoning that produced the mistake. This is the field
   that makes clustering possible, because the *shape of the error* is what repeats, not
   its subject.
3. **What generalises** — one sentence someone could apply to a different problem. Skip it
   if there genuinely isn't one; do not invent one.

Quote {{maintainer}} verbatim when they corrected you. Their wording is evidence; your paraphrase is
already an interpretation.

## Rules

- **One correction, one line.** Two unrelated corrections in one message are two entries.
- **Record before the fix is finished**, in the same turn. A correction logged "later"
  is a correction logged never — that is the recall failure this whole mechanism replaces.
- **Log it even when the fix is trivial.** Especially then: `deferred-instead-of-fixed`
  exists because a ten-minute fix got written up in three places instead.
- **Log it even when you were half right.** The half you got wrong is the data.
- **Never soften.** "I had inferred X from a doc about Y and promoted it to a fact about
  this machine" is the entry. "There was a small config discrepancy" is not.
- **Do not log a correction you invented** to satisfy the hook. An honest silence is
  correct; the pre-merge preflight has an explicit `--no-corrections <reason>` path for
  exactly that.

## What the gate already guarantees

- The line parses into six fields.
- `CLASS`, `CHANNEL` and `GATE` are in vocabulary.
- `DATE` is well-formed; `SLUG` is a single token; `NOTE` is at least 40 chars.

So do not spend attention on the format. Spend it on 2 and 3 above.

## Marking one resolved

This skill is only about the append. Separately: once a correction has produced a durable
change — a gate, a recipe, a doc fix, a commit — **the session that ships that change**
appends `[[disposition: kind: pointer]]` to the end of the entry's NOTE, e.g.
`[[disposition: gate: corrections_log]]`. `kind` is one of `gate | recipe | commit | doc |
no-action` (`.ai/gates/data/corrections_vocab.json`); `no-action` is for a correction looked
at and deliberately left open, not one nobody got to. `python -m nightshift.corrections --compact`
then moves every resolved entry into `.ai/corrections.archive.log`, keeping the active log to
what is still open. Do this only when you actually know what closed the correction out — an
invented disposition is worse than none, for the same reason an invented correction is.
