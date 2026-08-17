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

__all__ = ["TOOL_ECONOMY", "INTERACTIVE_CARD", "INTERACTIVE_NOTE"]

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

**This is an interactive session, and that changes what is expected of you.** A person is \
present for the whole run: when the card is ambiguous, ask them rather than picking an \
answer and noting the risk. There is no verdict file to write and no `outcome` to declare \
— you report to them, in the conversation, and they decide what happens next.

Working rules for this repo:

- **Branch before you edit.** Cut `{branch}` from `{base}` and commit your work there. Do \
not merge, do not push, and never commit directly to `{base}`.
- **Do not move the card between lanes.** Lane transitions belong to the runner and to the \
maintainer; a card that changes lane under them is a card they can no longer find. You may \
append to the card's `## Thread`.
- The card is at `{card_path}` — read it there rather than trusting the copy below to be \
current if the session runs long.
- Run the project's gates and the tests your change touches before you call it done, and \
say plainly what passed and what did not.

{tool_economy}

--- the card ---
{card_body}
"""

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
invent any, and do not file it as a card yourself; the maintainer closes it from the panel \
when the work is done.

{tool_economy}

--- the note ---
{note_body}
"""
