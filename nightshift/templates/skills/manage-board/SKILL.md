---
name: Manage Board
description: How to read and edit the AI-team board (Board/) safely from a live session — adding ideas, refining cards, answering a parked card, and moving cards between lanes — so an interactive session follows the same rules as an overnight run. Invoke whenever {{maintainer}} talks about the board, a card, or says "put this on the board" / "here are my answers".
---

<!-- Shipped by nightshift as a template, then filled in by nightshift init.
     Section references point at the framework's design notes, which deliberately do
     not ship; every rule they cite is stated in full where it is cited. -->

# Manage Board

**Concern:** let a live session edit the board without breaking the rules an overnight run
follows. You are the interactive front door to `Board/`; the contract is identical whether
{{maintainer}} is chatting at 2 PM or the runner is grinding at 3 AM.

The board is `Board/`, one card per markdown file, one state per lane. Full schema and the
reasoning: the framework's design notes. The lanes and ownership: `Board/README.md`.

**Assumed tier: `lead`, medium reasoning effort** (the origin project's maintainer, 2026-07-23). A skill has no
`tier:` field — it runs at whatever the calling session is — so this is a statement of what
the skill *expects*, and the caller (Claudian's model setting, or the terminal session) has
to match it. §16 is the only place the tier→model binding is written;
do not name a model here.

Why lead and not worker: the session's own residual judgment is answer-routing and deciding
*whether* a card is a simple confirm or needs re-scoping. Those are ungated — no script
catches a mis-routed answer or a lossy paraphrase — and {{maintainer}} is the only reviewer. Gated
work gets cheap tiers; ungated judgment does not. Medium effort suffices because the heavy
re-scoping is **dispatched** to `triage`, not done inline. **Dispatch rarely** — each spawn
re-pays context cold (§16), so aggressive dispatching costs more than running the bulk at
lead tier.

## The one rule everything else serves

**A live discussion about a card ends with the card updated.** If {{maintainer}} and you settle
something in chat and it lives only in this transcript, the 3 AM runner cannot read it and
the decision evaporates. Chat is a *view onto the card*, never a parallel memory. So before
you move on from any board conversation: the reasoning goes in `## Thread`, the refined
choice in `## Question`, the answer recorded and the card moved. No exceptions — this is the
whole reason the skill exists.

## Never do these

- **Never open or read `Board/ideas/`.** It is {{maintainer}}'s private lane. Not "avoid unless
  useful" — never. `doc_scan` and triage do not touch it; neither do you, and no code reads
  it either — `boardcmd promote` moves a note out by name without opening it.
  **You may commit and push those files** — `git add`/`commit`/`push` naming
  `{{board}}/{{private_lane}}/…` goes through the fence, because moving a note is not reading
  it. Write the commit message from the filenames and `git status`, never from a diff:
  `git show`/`diff`/`log -p` over the lane is still denied, and the standing rule is that no
  note's text feeds a card or a design decision.
- **Never hand-edit a `board.GENERATED_VIEWS` file** (`Routing.md`, `Chores.md`). Each is
  rewritten by the command that owns it, so an edit there is lost at the next run. There is
  no generated *report* file left to mistake for a place to answer — the digest and its
  `Digest.md` were removed in 2026-09. Answers go in a card's `## Thread`, which is the only
  place anything reads them from.
- **Never hand-drag a card file between lanes**, and never tell {{maintainer}} to. Lane changes go
  through `## Thread` + `boardcmd move` (below), which moves and commits in one step. There
  is no `state:` field — the lane is the directory, and `card_schema` refuses the field.
- **Never invent frontmatter {{maintainer}} would not write.** They never write any; triage fits it.
  If a card needs `tier`/`worker`/`unattended`, that is a triage judgment — make it as one,
  do not guess to fill the field.
- **Never write code or touch `{{package}}/`** from this skill. Refining a card is planning;
  building it is a `code-thread` dispatch.
- **Never hard-wrap a card's prose across multiple source lines.** Most repos wrap their docs
  at a fixed column for git-diff friendliness, and that convention must **not** bleed into
  `{{board}}/`: a card is edited in a plain textarea — Command Center's card editor, or any
  editor opened on the raw file — which shows each source line as its own row instead of
  reflowing the paragraph, so a manual wrap makes a card read as broken short rows. And a
  `` `code` `` span that opens on one line and closes on a later one does not survive the
  split in any renderer that does not join a paragraph's lines first: CommonMark closes a
  code span at the next backtick regardless of the newline in between, so the wrapped span
  leaks unescaped text into the render. Measured, not hypothetical: three spans wrapped this
  way in one card broke the rest of that card's render outright, and were caught by
  screenshot rather than by anyone reading the raw file. Command Center's own renderer
  happens to join a paragraph's lines before it processes them and so survives such a span,
  which makes this a rule about every *other* reader rather than a reason to relax it. Write
  every paragraph, bullet and picker option as **one continuous source line**, however long —
  let the editor soft-wrap it for display. This is a card-*writing* rule, not just an editing
  one: it binds `triage` and any worker appending `## Summary` / `## Thread` as much as it
  binds you.

## The operations

### "Put this on the board" / a new idea

Write it to `Board/inbox/<slug>.md` with the idea as the body and no frontmatter —
nothing more (`python -m nightshift.boardcmd note <slug>.md --body-file -` writes and commits it). It is now triage's input. **Never straight to `tasks/`**: dictation
does not skip triage. If {{maintainer}} is clearly ready to have it triaged now, dispatch the
`triage` agent on it (see below); otherwise leave it in `inbox/` for the next triage pass.

### Refine a card (the cyberdeck-mode conversation)

{{maintainer}} wants to talk through an option on a parked card. This is interactive triage, and it
runs at your tier, so it is legitimate — you *are* the lead. Talk it through, then **write
the result back**: sharpen the relevant `## Question` picker, or add a dated `## Thread`
entry capturing what you concluded. If the conversation actually resolves the card, that is
the "answer a parked card" flow next.

Apply the triage rules while you do this — they are in `.claude/agents/triage.md` and you
should read it when refining. The ones that bite most often:

- `## Question` is a **picker**: a `### Decide:` heading per decision carrying one sentence,
  terse `- **label** — implication` options under it, a `*(recommended)*` default. Only
  the bullets under that heading reach the Command Center's picker.
- **Batch** every open decision into one round; a card must not need a second answer.
- Run the **second-order lens**: does this ripple into work that does not exist yet? If a
  set will grow, require enumeration + a guard, do not hardcode today's members.
- Cross-reference other cards with `[[wikilinks]]`, never file paths.

### Answer a parked card (the bulk flow)

{{maintainer}} pastes replies against several cards at once — *"ICE pick B and 3; redesign
all-recommended; …"*. This is judgment (matching fuzzy references to cards) and the place
bookkeeping can quietly corrupt the board, so:

1. **Match each reply to its card.** If a reply is ambiguous between two cards, **ask** —
   do not guess. Park-over-invent applies to bookkeeping exactly as to implementation.
2. **Write the answer verbatim into that card's `## Thread`**, dated and attributed
   (`### <date> · {{decision_attributor}}`). The worker reads your record at 3 AM; a lossy
   paraphrase is how "pick 3 EP" becomes "around 3, tune to taste."

   **The handle is not decoration.** `decide.has_maintainer_answer` matches
   `[board].decision_attributor` from `.ai/manifest.toml` literally, and Command Center's
   decide page uses it to say *"answered · ready to dispatch"* on a card you have already
   settled — it cannot tell your answer from an agent's own `### <date> · triage` note any
   other way, which is why the token is declared rather than guessed.

   So the two have to agree, in both directions: sign with a different handle and the page
   goes on asking for an answer you already gave, and **if that key is absent from the
   manifest the recognition is off entirely** — `init` only writes it when you confirm it,
   and silence there is a real answer, not a default. Add the key to switch it on.
3. **Re-triage the card — do not mechanically promote it.** An answer is not automatically
   a ticket to `tasks/`. Getting the answer can *open* a question that could not be asked
   until now, or reshape the card. So after recording it, judge the card as triage would.

   **Start from the card's own `after_answer:`** — the parker's declaration of how it
   resumes, `triage` (the answer scopes the card, so it must be rewritten around it) or
   `tasks` (the card is scoped and the answer settles one point inside it). It is a
   starting point, not a verdict: you are looking at the answer, which the parker was not,
   so a `tasks` card whose answer reshaped the work still goes back through triage. What it
   removes is the guessing when nothing reshaped anything.

   - **Fully actionable now** — every acceptance criterion is statable and no question
     remains, which is the normal outcome for `after_answer: tasks`: restate `##
     Acceptance`/`## Steps` for the chosen branch, set `## Open questions` to `none`
     (keeping the question as a quote under it, so nothing is lost), re-evaluate `tier:` if
     the answer changed it, then move it to `tasks/` (below). The panel's `Send to tasks` does
     exactly this for you on an answered `after_answer: tasks` card.

     **`kind: chore` needs one more edit: reset `attempts: 0`.** A chore bounced to
     `needs-decision/` already spent its one attempt (`nightshift.chores.MAX_ATTEMPTS` is 1),
     so leaving `attempts:` as-is strands the card the moment it lands back in `tasks/` —
     `chores.eligible()` refuses it ("already attempted 1x") and the nightly `runner.select()`
     refuses every `kind: chore` card outright, so nothing else will ever pick it up either.
     The `branch:` field stays as-is (the worker resumes the existing `ai/<id>` branch rather
     than cold-starting); only `attempts:` needs clearing.
   - **The answer opened a new question, or settled only part of a batched one:** keep it in
     `needs-decision/`, add the new question (batched, picker-shaped), and tell {{maintainer}} what is
     still open. Do **not** advance it.
   - **The answer changed the card's shape enough to need real re-scoping:** dispatch the
     `triage` agent to re-triage it rather than reshaping it inline.

   **On any doubt, leave it parked.** The costs are asymmetric: a card wrongly promoted is
   picked up by a worker and produces confident wrong work that costs more to review than to
   redo; a card wrongly kept parked just waits one more cycle. Park-over-promote is the same
   rule as park-over-invent.

   The backstop, so this is safe even when the judgment slips: **`card_schema` refuses any
   card in `tasks/` whose `## Open questions` is not `none`** (§3). A half-baked
   card physically cannot sit in `tasks/` — the gate goes red on the move. The re-triage
   above is what keeps you from leaning on that; the gate is what catches you when you do.
4. **Echo back one line per answer** — "ICE → tasks, drains heat, 3/hit; redesign → tasks,
   all recommended" — so a mis-route is visible to {{maintainer}} now, not discovered next week.

### Move a card between lanes

```bash
python -m nightshift.boardcmd move <id> <lane>
```

It moves the file through `board.move` and commits it as `board: <id> <from> → <to>`. The
lane is the directory; there is no field to edit. A move into `review/` is refused — that
lane belongs to the runner and `drain`. To close out a card you worked yourself, use
`boardcmd land` (below) instead: it merges the branch as well.

One thing still breaks on a move, caught by the gates on save: **a doc citing a card by
its lane path breaks the moment the card moves.** That is why the cross-reference rule is
`[[wikilinks]], never file paths` — `doc_reference_liveness` fires on the stale path, in a
file nobody was editing. Fix by converting the citation, not by moving the card back.

Rewriting a card with `pathlib.write_text` on Windows used to convert it to CRLF and leave
`normalize_worktree` unable to help (a file with real content changes was outside its
narrower, index-comparison repair). That is no longer something to remember to avoid:
`line_endings.fix()` rewrites any CRLF file back to LF automatically, at the next gate
run — the on-save hook after your next `Write`/`Edit`, or `preflight` if none comes first
— with no index comparison at all (hygiene-rules-belong-in-a-script card).
`write_bytes`/`newline=""` is still the cleaner habit, not a workaround you have to apply.

### Never leave a board edit in a checkout that is not on `{{integration}}`

**Check `git branch --show-current` before you finish any board conversation.** If it is
not `{{integration}}`, the board you just edited is not the board the runner will read.

`{{board}}/` is a tracked directory, so every branch carries its own copy of it, and the
launch checkout is on whatever branch you have out. On `{{integration}}` there is one copy
and everything agrees — that is the normal case and nothing below applies. Once the launch
checkout moves to a feature branch, `nightshift.runner`, `nightshift.chores` and
`nightshift.drain` all redirect to the dedicated integration checkout named by
`[worker].integration_checkout_dir` — a sibling directory, permanently on `{{integration}}` —
which holds a **second** copy of `{{board}}/`. An edit made here does not exist there.

So:

- **On `{{integration}}`** — edit in place, commit (or `boardcmd move`, which commits). Done.
- **On any other branch** — the edit has to reach `{{integration}}`. Either make it in the
  integration checkout (permanently on `{{integration}}`) and commit it there, or commit it
  here and merge it to `{{integration}}`. **Committing it on the feature branch alone does
  not count**: board commits belong on the integration branch, and one sitting on a feature
  branch is as invisible to the runner as an uncommitted edit while looking considerably
  more finished.

`runner.stranded_board_refusal` is the backstop, not the check: a night, a chore batch or a
drain that redirects to the integration checkout **refuses to start** while the launch
checkout holds board edits `{{integration}}` does not, and names the files. It converts a
silent wrong answer into a refusal you can read. Do not lean on it — a refused batch is
still a batch that did not run.

### Closing out a card you did yourself

Occasionally {{maintainer}} authorises a card to be done **inline**, in this session, rather
than through the runner — cross-repo work the runner cannot cut a worktree for, most often.
The close-out is one command, the same landing the runner and the chore batch use:

1. Write `## Telemetry` onto the card — what was done, where it landed, what was verified
   by running. It is a board edit, so on the integration branch.
2. Run `python -m nightshift.preflight` on the card's branch, so its tip carries a receipt.
3. From the integration branch: `python -m nightshift.boardcmd land <id>`.

`land` merges `ai/<id>`, folds its memory fragment, deletes the branch locally and on the
publish remote, moves the card to its lane and commits — or refuses and touches nothing (no
receipt, the wrong branch checked out, a merge that does not apply). The lane follows
`verify:`, except that a diff touching a declared player-visible path always goes to
`testing/`, and `--lane done` is refused for it. `--lane testing|done` overrides otherwise;
`--no-branch` is for work that landed in another repository, so only the card moves. A
branch already merged by hand is finished without a receipt, and
`python -m nightshift.boardhealth` lists any card left merged but unmoved.

Which lane: `done/` when it shipped and {{maintainer}} has already seen the result;
`testing/` when it shipped and still needs them at the keyboard. Do not use `done/` to mean
"I am finished typing."

**"Already seen the result" means {{maintainer}} actually watched it run — not that they agreed the design in chat before you built it.** Agreeing a number, picking an option from a picker, approving an approach: none of that is having seen it. The inline route made this mistake systematically until `ingest._INLINE_CARD` stopped hardcoding `verify: review`, and that fix does not make you immune to making it by hand. If the change has a surface — anything they could open, run, read on screen or in another language — it goes to `testing/`.

**A card you land in `testing/` needs a `## How to test`, and you are the one who writes it.** `card_schema` requires the section on a `verify: play` card in that lane and goes red on the move without it, but the gate is the backstop, not the reason: the runner has `settle()` to write it from the worker's verdict JSON and an inline card has nobody but you. Write the scenario in {{maintainer}}'s terms — open it, do X, expect Y — including the negative case where it applies, because "check it works" is not a scenario and they will have forgotten the diff by the time they read it.

### "Run card XYZ now" — dispatching through the runner

When {{maintainer}} asks for a specific card to be *implemented* (not refined), hand it to the runner
rather than doing it inline. **Invoke the `run-the-runner` skill** — it carries the commands,
every flag and its default, the preflight refusals and what each outcome means, so you do not
have to read `nightshift/runner.py` to find out.

The one rule that belongs here, because it is about the board rather than the runner: **do
not implement the card yourself in the chat session**, and do not spawn the worker with
`Agent` by hand. Either bypasses the branch isolation, the gate run and the attempt
bookkeeping, and it is the path that produced the 2026-07-22 tier violation.

## Preparing work for a local runtime

A local model runs each step with a cleared window, so the *step* has to carry what a
session normally would. Four rules, each earned on a plan that stalled at the first step
whose check the model could not run:

- **Every step ends in a command the model can run, with a literally-quoted expected
  output.** "Tests pass" and "it works when you play it" are not that. A step whose check
  the model cannot perform is a step it cannot close, and it re-reads, re-plans and laps
  instead of stopping.
- **The check must prove the artefact exists, not that the model believes it does.** A
  completion was once reported over an empty directory; `ls <file>` plus a syntax check
  catches that in one command, and restarting the session does not.
- **Steps record their own completion** — a commit per step, or a progress file the next
  step reads first. With no record, a cleared window re-derives progress from the tree,
  and a step that wrote nothing looks exactly like a step not yet started. That is how the
  same work gets done twice.
- **Split what the model can close from what {{maintainer}} must judge.** Play-tests, visual
  checks and anything needing the running product are theirs, named as their own steps,
  never buried in the model's done-condition.

Shape: one file and a bounded edit per step, an explicit tool-call budget, and a stop
condition — *about to re-read a file you already read → stop and report what you have*.
The worker charters in `.claude/agents/` carry the budget wording to copy.

**This is not extra checking on your side.** The whole point is a step the local model can
close alone; if preparing one costs more than doing the work would, do it yourself.

## Dispatching triage

For turning a raw `inbox/` note into a card, prefer the `triage` agent over doing it inline
— it is chartered for exactly this and runs at the lead tier. Dispatch it with the card path
**and a stated tier** (`tier: lead`), or `nightshift/hooks/tier_guard.py` refuses the spawn. You
may resume a prior triage agent to reshape a card it already wrote — it keeps its discovery
and needs no re-tracing (that is how the ICE and menu cards were reformatted).

## What runs when — do not over-test

Moving a card or editing its markdown changes **no Python**. The gate suite
(`python -m nightshift.gates.run`, ~12s) runs on every edit via a hook and is all the proof a board
change needs; `card_schema` is the relevant part. That figure was `~1s` here until 2026-08-01
and had drifted to 33s without anything noticing — it is load-bearing (it is the reason
running the gates on every edit is worth it), so treat it as a claim to re-measure rather
than a constant, and read it as *fast enough to run on save* rather than as a number.
**Do not run the full pytest suite for a
board edit** — it exercises `{{package}}/`, which a card move does not touch. The full suite
is a pre-merge gate, not a per-edit one. Run the narrow `tests/test_board_*.py` only if you
changed `.ai/` Python.
