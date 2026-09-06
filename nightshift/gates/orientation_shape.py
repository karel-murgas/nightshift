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

## The second shape: a log made of bullets (2026-09-06)

The heading rule above has a blind spot, and Dungeoneer fell straight into it. Its
`state.md` grew to **240 register lines under a single `## Current State` heading** — one
bullet per shipped card, newest first, 22.1 KB of a 91.8 KB always-loaded set, appended
automatically by a `[[memory.fold]]` row. Not one heading was dated, so this gate passed
it every day for months while the file became exactly the thing the gate exists to
prevent. The maintainer found it by reading, not by tooling:

    *"`state.md` is read at the start of every session, but it is basically a changelog
    — one register line per card, newest first, not an overview of features. And it
    grows by one entry per card forever, so it is not sustainable."*

**The log-detector was heading-shaped; the log was bullet-shaped.** So a second shape:
list items that carry a date, counted per file.

The register/log distinction that makes the heading rule work does *not* transfer here,
and that is why this half is a count rather than a pattern. A log bullet
(`- **Thing shipped** (2026-09-06, `card-id`): …`) and a register bullet
(`- **Campaign difficulty = Contract Board** (decided 2026-07-16, IMPLEMENTED): …`) are
syntactically the same object: bold subject, parenthesised date. Nothing in the line says
which it is. What separates them is **how many there are** — a register has one entry per
subsystem and stops; a log has one per card and does not.

## The corpus, counted

Steps 2-4 of `verify-before-shipping-a-rule.md`, run over Dungeoneer's 42 memory and
instruction documents on 2026-09-06 rather than reasoned about:

| Document | Dated list items | In scope? |
|---|---|---|
| `state.md`, before the restructure | **103** | yes — the failure |
| `state_changelog.md` | 97 | no, a companion |
| `design_detail.md` | 37 | no, a companion |
| `arch_detail.md` | 24 | no, a companion |
| `ref_minigame.md` | 12 | no, not declared orientation |
| `state_history.md` | 10 | no, a companion |
| **`design.md`** | **7** | **yes — a legitimate register, and the number that sets the floor** |
| `arch.md`, `MEMORY.md`, `state.md` (after) | 0 | yes |
| 29 further documents | 0 | — |

So within the declared orientation set the rule must reject 103 and accept 7, and
`LIST_THRESHOLD = 12` sits in that gap. It is not the midpoint, deliberately:

* **Not 8.** One above `design.md`'s real count leaves a register no room to gain an
  entry, and a gate that reddens on a legitimate edit gets appealed into uselessness —
  the failure mode this module's first version already survived once.
* **Not 30 or 50.** The sibling `orientation_budget` is the late net; the whole argument
  for this gate is that it fires while the fix is still cheap. 12 catches the shape after
  roughly a dozen cards, not after a hundred.
* **12** leaves `design.md` 71% headroom and still rejects the observed failure by 8.6x.

A project whose orientation genuinely needs more dated bullets than this should raise the
constant in a visible commit, the same bargain `budget_bytes` offers.

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

# A Markdown list item: `- `, `* `, `+ ` or `1. `, at any indent. Nested items count —
# a log does not stop being a log because someone indented it.
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+")

# An ISO date anywhere in the line. `(?!\d)` for the same reason as `_DATED_HEADING`.
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?!\d)")

# Dated list items at or above this are a per-card log rather than a per-subsystem
# register. Sized against a counted corpus — see the docstring; the gap it sits in runs
# from a real register's 7 to the observed failure's 103.
LIST_THRESHOLD = 12


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

        # Name the companion the project already has, if it has one. Suggesting
        # `design_history.md` to a repo whose convention is `design_detail.md` invents a
        # second home for the same content — from the gate that exists to prevent exactly
        # that kind of sprawl.
        stem, suffix = Path(rel).stem, Path(rel).suffix
        existing = [f"{stem}{sfx}{suffix}" for sfx in ("_changelog", "_history", "_detail", "_log")
                    if (path.parent / f"{stem}{sfx}{suffix}").is_file()]
        companion = existing[0] if existing else f"{stem}_history{suffix}"

        dated = [n for n, line in enumerate(lines, start=1)
                 if _DATED_HEADING.match(line)]
        if len(dated) >= THRESHOLD:
            out.append(Violation(
                rel, dated[0],
                f"orientation_shape: {len(dated)} dated headings in a file every session "
                f"loads — this is becoming a log. Orientation says what is true NOW; move "
                f"the dated narrative to a companion (`{companion}`) that is read only when "
                f"a question sends you there, and leave one line here per shipped thing. The "
                f"origin project reached 196 KB this way, at which point the file cost more "
                f"to read than the code it described"))

        # The same rule, the other syntax. A log does not need dated headings — Dungeoneer's
        # ran to 240 bullets under one undated `## Current State` and this gate passed it
        # daily. Counted rather than pattern-matched: a register bullet and a log bullet are
        # the same object, and only the quantity tells them apart.
        bullets = [n for n, line in enumerate(lines, start=1)
                   if _LIST_ITEM.match(line) and _DATE.search(line)]
        if len(bullets) >= LIST_THRESHOLD:
            out.append(Violation(
                rel, bullets[0],
                f"orientation_shape: {len(bullets)} dated list items in a file every "
                f"session loads — a register has one entry per subsystem and stops, a log "
                f"has one per shipped card and does not. Move the per-card lines to a "
                f"companion (`{companion}`) and leave this file saying what is true NOW. "
                f"If a fold or append step writes them here, repoint it: that is what makes "
                f"the growth automatic and unbounded"))
    return out
