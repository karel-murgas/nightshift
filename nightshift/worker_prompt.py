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

__all__ = ["TOOL_ECONOMY"]

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
