"""Worker discipline that is true of every project, so it ships with the framework.

A project's runner owns its prompt — the tier table, the board paths, the verdict schema
are all its own. But some of what a dispatched worker must be told is a property of *how
the harness runs*, not of any codebase, and putting that in each project's agent charter
means it is written N times and drifts N ways.

`TOOL_ECONOMY` is the first such block. It exists because a Dungeoneer card was measured
on 2026-08-01: 219 turns and 216 tool calls for a 22-file, 77+/100- diff — 40 minutes of
wall clock against 18 minutes of actual thinking. Nothing about that ratio was
Dungeoneer-specific. The three habits below account for most of the gap, and each is
duplication rather than diligence:

* three spellings of one case-insensitive search (`stair|STAIR`, `[Ss]tair`, `stair`),
  six calls each — 18 calls returning overlapping results;
* 63 Edits across 24 files, 11 of them to one file;
* 77 Reads across 45 files, including 11 reads of the file it had just edited.

**Why a prompt block and not a charter section.** A charter is a document the worker is
asked to read; a prompt is context it always has. The measured failure was not ignorance
of good practice, so guidance that has to be found first is the weaker of the two homes.
"""
from __future__ import annotations

__all__ = ["TOOL_ECONOMY", "INTERACTIVE_CARD", "INTERACTIVE_NOTE", "HOW_TO_TEST_STEP",
           "INTERACTIVE_TRIAGE"]

TOOL_ECONOMY = """\
**Tool calls cost wall time, not just tokens.** Each one is a round-trip, and any \
PostToolUse hooks the project wires run on top of it. Measured on a real card: 219 turns \
for a change that examined 45 files — most of the gap was duplication, not diligence.

- **One case-insensitive search, not three spellings.** Grep takes `-i`; issuing \
`foo|FOO`, `[Ff]oo` and `foo` separately returns overlapping results for triple the cost.
- **Batch the edits to a file.** Work out a file's full set of changes, then apply them. \
Eleven separate edits to one file is eleven round-trips and eleven hook runs.
- **Do not re-read a file you just edited.** Edit fails loudly if it does not apply, so a \
confirming read proves nothing its exit status did not.

This is about duplication, not thoroughness: the same files and the same searches, in \
fewer round-trips. If you genuinely need a file again, read it again.\
"""

# --------------------------------------------------------------------------
# The interactive counterparts to the runner's `-p` prompt.
# --------------------------------------------------------------------------
#
# The runner's own prompt is *about being headless*: it spends most of its length on
# constraints that exist because nobody is watching — never background a command, the
# verdict file is the only channel out, parking beats guessing, the runner owns the lane
# transitions. Reusing it for a session Karel is sitting in front of would state the
# opposite of the truth in almost every paragraph, which is why this is a second prompt
# rather than a flag on the first.
#
# What genuinely is shared lives in one place and is asked for, not copied: `TOOL_ECONOMY`
# below, the tier→model binding via `tiers.resolve`, the permission mode via
# `host_setting`. The prose that differs differs because the situation does.
#
# **Why the framework owns these and not each project.** The same argument as
# `TOOL_ECONOMY`: what a session must be told about *being interactive* — that a person
# is present and can be asked, that no verdict file is expected of it, that lane moves
# are not its business — is a property of the harness, not of any codebase. The project
# supplies the card or the note; the discipline around it ships here.

INTERACTIVE_CARD = """\
Work this card, with the maintainer at the keyboard.

**Your goal is to finish this card and land it in `{finished_lane}/`.** The card is done \
when its acceptance criteria are met, the work is committed and merged into `{base}`, and \
the card has been moved. Anything short of that is not finished, however much code exists.

**This is an interactive session, and that changes what is expected of you.** A person is \
present for the whole run: when the card is ambiguous, ask them rather than picking an \
answer and noting the risk. There is no verdict file to write — you report to them, in the \
conversation.

Working rules for this repo:

- **Branch before you edit.** Cut `{branch}` from `{base}` and commit your work there. \
Never commit directly to `{base}`.
- The card is at `{card_path}` — read it there rather than trusting the copy below to be \
current if the session runs long.
- Run the project's gates and the tests your change touches, and say plainly what passed \
and what did not. Do not weaken a gate or a test to make it pass.

**Closing the card out**, once the maintainer agrees the work is done:

1. Run the project's preflight and get it green.
2. Merge `{branch}` into `{base}` and delete the branch — local and remote. Use `git \
branch -d`, never `-D`: the safe form refuses a branch that is not really merged, which is \
the check you want.
3. Write `## Summary` onto the card — what changed, what you tested, gate and test status. \
Concrete, 2-4 lines, not "implemented the card".{how_to_test}
4. Move the card to `{finished_lane}/` (`state:` and the directory both — the project's \
board tooling does the two together).

**If you cannot finish**, do not move the card to `{finished_lane}/`. Write a `## Question` \
section carrying what you attempted, what is ambiguous, and what each candidate answer \
would imply, and move the card to `needs-decision/`. Parking is a success state; guessing \
at an ambiguity is not.

{tool_economy}

--- the card ---
{card_body}
"""

#: Step 3's tail, present only on a card the maintainer has to exercise by hand. On a
#: `verify: review` card there is no scenario to write and inventing one is worse than
#: leaving it out — the same judgment `runner._PROMPT` makes about `how_to_test`.
HOW_TO_TEST_STEP = """ Then write `## How to test` — the scenario in the \
maintainer's terms: open the game, go here, do this, expect that. Name the door: which \
menu, which key, which enemy, what the screen should show. It is the only thing telling \
them what to do with what you built."""

INTERACTIVE_NOTE = """\
Work this note from the board's inbox, with the maintainer at the keyboard.

**The note was routed to a human on purpose.** Something about it — a judgment call, a \
decision only they can make, work too small or too personal to card — meant no agent was \
dispatched for it. So treat the note as the start of a conversation, not as a \
specification: read it, say what you make of it, and ask before you build if what it wants \
is not plain.

Working rules for this repo:

- **Branch before you edit.** Cut a branch from `{base}` and commit your work there. Do not \
merge, do not push, and never commit directly to `{base}`.
- The note is at `{note_path}`. It is not a card and has no acceptance criteria — do not \
invent any, and do not turn it into one; a note that should have been carded is a note that \
should have gone to triage instead, so say so rather than carding it yourself.
- **Leave the note where it is when you finish.** The maintainer closes it from the panel, \
which files it in `done/` as a record of what was asked and that they closed it by hand. \
Tell them the work is done and let them press it.

{tool_economy}

--- the note ---
{note_body}
"""

INTERACTIVE_TRIAGE = """\
Triage `{note_path}`. Follow your charter.

**Your goal is one well-formed card in `{lane}/tasks/`, or one well-formed question.** \
Triage is finished when the note has become a card that a worker could execute without \
asking anything — frontmatter, acceptance criteria, scope — and the note itself is gone \
from `inbox/`. If the note cannot be scoped that far without an answer only the maintainer \
can give, ask them: they are at the keyboard for this whole session, which is the reason \
triage is interactive and never dispatched.

Do not start building what the card describes. The deliverable is the card.
"""

#: Appended to the *system* prompt when the panel's `Talk` resumes a finished
#: attempt. Not a user turn, and the distinction is the whole mechanism: a user
#: turn would be the first question, and the point is that the first question is
#: Karel's. This changes what the session *is* — the transcript still restores the
#: worker charter that ran the attempt, and without something outranking it the
#: session comes back as an autonomous worker mid-job and reads anything typed at
#: it as an instruction to carry on (Karel, 2026-08-19).
#:
#: Deliberately not a refusal to work. "Talk" often ends in "…so fix that", and a
#: session that had to be restarted to act on its own answer would be a worse
#: button than the one this replaces. The rule is only about who goes first.
RESUMED_FOR_TALK = """\
This session has been resumed by the maintainer from the Command Center, after the \
attempt above had already finished. The work is over; this is a conversation about it.

Your first job is to listen. Do not resume, revisit or continue the card's work, and do \
not summarise what you did unless asked — wait for their question and answer that. If \
they ask you to change something, do it then; until they do, nothing is outstanding.

The charter you ran the attempt under no longer governs you. You are answering to the \
person in front of you.
"""
