"""Git conflict markers: how to recognise one, and where to look for them.

One home, because there are two readers with the same question and opposite jobs.
`gates/conflict_markers.py` sweeps the committed tree and fails the build; the
runner's merge resolver checks a *hand-resolved* working tree before it lets the
rebase continue. A marker predicate written twice is a marker predicate that
drifts, and the whole point of this module is that the second reader exists at all.

**Why this is a module and not a `grep`.** Karel, 2026-08-09
(`conflict-marker-survived-a-hand-resolved-merge` in `.ai/corrections.log`):
resolving a rebase by hand, the content between the markers was edited correctly
and the trailing `>>>>>>> 0c5905f (...)` line was left behind. It survived staging,
committing, merging into the integration branch and pushing, and sat in a shipped
card for about a day. Nothing looked: the schema gate reads section structure, not
prose, and *a successful `git rebase --continue` is not evidence a hand-resolved
conflict was resolved correctly* — git only checks that each marked region was
edited into something, never that the something is marker-free.

That correction proposed exactly this check and it was never built, which is why
the same class recurred on 2026-08-23 when two cards' register entries collided.
Now that a resolver can be an agent rather than a person, the check stops being
hygiene and becomes the thing that makes automated resolution safe to trust.

**The `=======` line is anchored, the other two are prefixes.** `<<<<<<<` and
`>>>>>>>` are always followed by a ref name, so a bare seven-character run is not
a real marker; `=======` stands alone on its line. Markdown's setext-h2 underline
is also a run of `=`, and this repo's docs use it — hence the exact-length,
whole-line match for that one, and the requirement that a file contain the
*opening* marker before any `=======` in it counts at all.
"""
from __future__ import annotations

import re
from pathlib import Path

__all__ = ["FOUND_IN", "markers_in", "scan"]

#: `<<<<<<< HEAD`, `>>>>>>> 0c5905f (msg)`, and a bare `=======`.
#: Each must start the line — an indented one is quoted sample text, not a marker.
_OPEN = re.compile(r"^<{7}[ \t]")
_CLOSE = re.compile(r"^>{7}[ \t]")
_MID = re.compile(r"^={7}[ \t]*$")

#: What `scan` reports per hit, so both callers render the same sentence.
FOUND_IN = "{path}:{line}: git conflict marker left behind — {text}"


def markers_in(text: str) -> list[tuple[int, str]]:
    """Every `(1-based line number, line)` in `text` that is a conflict marker.

    A `=======` only counts once an opening marker has been seen in the same
    file, which is what keeps a setext heading underline (`Title` over `=====`)
    from reading as a conflict. An unpaired `>>>>>>>` still counts on its own:
    that is precisely the shape the 2026-08-09 miss left behind.
    """
    hits: list[tuple[int, str]] = []
    opened = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _OPEN.match(line):
            opened = True
            hits.append((number, line[:120]))
        elif _CLOSE.match(line):
            hits.append((number, line[:120]))
        elif opened and _MID.match(line):
            hits.append((number, line[:120]))
    return hits


def scan(root: Path, paths: list[str]) -> list[str]:
    """`FOUND_IN` lines for every marker in `paths`, relative to `root`.

    A path that does not exist or does not decode as UTF-8 text is skipped rather
    than reported: a binary file cannot carry a marker, and a deleted one is a
    resolution, not a defect.
    """
    found: list[str] = []
    for rel in paths:
        target = root / rel
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found += [FOUND_IN.format(path=rel, line=number, text=text_)
                  for number, text_ in markers_in(text)]
    return found
