"""Gate: an orientation document must not become a log.

`orientation_budget` catches the *size* of the always-loaded set, and by the time it
fires the damage is done — someone has to compress a 196 KB file under deadline. This
catches the *shape* that produces that size, which is visible from the first few entries:
a document whose sections are dated.

The failure is measured, not theoretical. In the origin project `state.md` grew to
196 KB by accumulating one dated section per session, each individually reasonable, until
reading it cost more than re-reading the code — at which point nobody read either. The
restructure that followed (2026-07-22) split every orientation file in two: a small
current-state document, and a companion holding the dated narrative, loaded only when a
question sends you there. That is the rule this gate enforces:

    **Orientation says what is true now. History goes in a companion.**

## What it checks

For each file in `[memory].orientation`, headings whose text carries a date. Three or
more of them is a log, and the gate says so.

Three, not one, and headings only. A design document recording *"decided 2026-07-24,
because…"* inline is doing exactly the right thing, and a single dated heading is a
note. What repeats is the pattern of a heading per session, and that is what the
threshold names. A gate that fired on any date in an orientation file would be muted
within a week, which is worse than not having it.

**Absence disables it.** No `[memory].orientation` means no opinion — the same rule every
other manifest-driven gate here follows. `init` declares the memory stubs it writes, so a
fresh install is covered without the operator having claimed anything about their own
documents.

## What it deliberately does not do

It does not read the companion. A file named `state_history.md` is *supposed* to be
nothing but dated sections; it is out of scope precisely because it is not loaded every
session. Orientation is the budgeted thing, so orientation is the checked thing.
"""
from __future__ import annotations

import re
from pathlib import Path

from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError, load

NAME = "orientation_shape"
FAST = True
DESCRIPTION = "declared orientation docs hold current state, not dated history"

# A heading, at any level, whose text carries an ISO date. Matched on the heading rather
# than the line so that prose mentioning a date is untouched.
_DATED_HEADING = re.compile(r"^#{1,6}[ \t]+.*\b\d{4}-\d{2}-\d{2}\b", re.MULTILINE)

# Below this, it is a note. At and above it, it is a log.
THRESHOLD = 3


def check(repo_root: Path) -> list[Violation]:
    try:
        declared = load(repo_root).memory.orientation
    except ManifestError:
        return []
    if not declared:
        return []

    out: list[Violation] = []
    for rel in declared:
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue                     # a missing orientation file is another gate's
        lines = text.splitlines()
        dated = [n for n, line in enumerate(lines, start=1)
                 if _DATED_HEADING.match(line)]
        if len(dated) < THRESHOLD:
            continue
        companion = Path(rel).stem + "_history" + Path(rel).suffix
        out.append(Violation(
            rel, dated[0],
            f"orientation_shape: {len(dated)} dated headings in a file every session "
            f"loads — this is becoming a log. Orientation says what is true NOW; move "
            f"the dated narrative to a companion (`{companion}`) that is read only when "
            f"a question sends you there, and leave one line here per shipped thing. The "
            f"origin project reached 196 KB this way, at which point the file cost more "
            f"to read than the code it described"))
    return out
