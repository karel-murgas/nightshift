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

For each file in `[memory].orientation`, headings that **begin** with a date. Three or
more of them is a log.

That distinction is the whole gate, and the first version got it wrong. A log has
headings that *are* dates — `## 2026-08-01 — session one` — because the session is the
subject. A **register** has headings that are subjects with a date attached —
`## Traps (decided 2026-07-17)`, `## Skills System (decided 2026-07-18)` — and is
exactly what an orientation document should look like: one entry per subsystem, saying
what was decided and when. The first version matched a date anywhere in the heading and
fired on the origin project's `design.md`, which is a register of 40 KB doing its job.

Caught the day it was written, by running it against a real corpus instead of the
synthetic files it was tested with — those agreed with the rule because the same person
wrote both. `.ai/recipes/verify-before-shipping-a-rule.md` step 3 says to run a new rule
against every member of the corpus by hand; skipping it cost one false positive on the
first real repo the gate met.

Three, not one, and headings only. A single dated heading is a note, and prose recording
*"decided 2026-07-24, because…"* is untouched at any count. A gate that fired on any date
in an orientation file would be muted within a week, which is worse than not having it.

## The corpus, counted

Run against all 30 memory documents in the origin project, 2026-08-03:

| Rule | Documents flagged | Which |
|---|---|---|
| date anywhere in the heading | **4 of 30** | `design.md` and `ref_minigame.md` (registers — wrong), `state_history.md` and `ref_i18n_history.md` (companions — out of scope) |
| date at the start of the heading | **1 of 30** | `ref_i18n_history.md`, a companion, so the gate never reads it |

Against that project's *declared orientation set* the rule rejects nothing, and that is
the expected result rather than a hollow one: the restructure this gate exists to enforce
happened there in July 2026, and the gate is preventive. It rejects the pre-restructure
`state.md` shape, which is what it is for.

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

# A heading whose text BEGINS with a date — the session-log shape. Leading decoration is
# allowed (`## **2026-08-01**`, `## [2026-08-01]`) because that is styling, not subject;
# a word before the date means the heading is about the word.
#
# `(?!\d)` rather than `\b` to close the date: `## _2026-08-03_ notes` has no word
# boundary between the `3` and the `_`, so `\b` made underscore styling invisible to a
# gate whose whole claim is that styling is not subject.
_DATED_HEADING = re.compile(r"^#{1,6}[ \t]+[\W_]*\d{4}-\d{2}-\d{2}(?!\d)")

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
        # Name the companion the project already has, if it has one. Suggesting
        # `design_history.md` to a repo whose convention is `design_detail.md` invents a
        # second home for the same content — from the gate that exists to prevent exactly
        # that kind of sprawl.
        stem, suffix = Path(rel).stem, Path(rel).suffix
        existing = [f"{stem}{s}{suffix}" for s in ("_history", "_detail", "_log")
                    if (path.parent / f"{stem}{s}{suffix}").is_file()]
        companion = existing[0] if existing else f"{stem}_history{suffix}"
        out.append(Violation(
            rel, dated[0],
            f"orientation_shape: {len(dated)} dated headings in a file every session "
            f"loads — this is becoming a log. Orientation says what is true NOW; move "
            f"the dated narrative to a companion (`{companion}`) that is read only when "
            f"a question sends you there, and leave one line here per shipped thing. The "
            f"origin project reached 196 KB this way, at which point the file cost more "
            f"to read than the code it described"))
    return out
