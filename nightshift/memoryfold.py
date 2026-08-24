"""Fold each finished card's memory record into the shared logs, serially.

**The collision this removes.** A project's memory has one or two files every card
writes to: a per-subsystem register (*what shipped, newest first*) and a dated
history (*the narrative, newest first*). Each card adds its entry at the top of the
same list, so two cards finishing the same night conflict **by construction** — not
because they disagree, but because they chose the same anchor. Git cannot merge that
and should not: it is two additions at one insertion point.

Dungeoneer paid for it four times — 2026-08-09 (`menu-art-cyberware`), 2026-08-13
(`hack-end-protection`), and both cards of 2026-08-22 — each resolved by hand, each
looking like a one-off. Karel, 2026-08-23, on being asked to fix the escalation and
then the escalation's cause: *"Fix the cause too (the append, state.md and all that
stuff.)"*

**The fix is to move the write, not to merge it better.** A card writes its record to
a file of its own — `.ai/memory-fragments/<card-id>.md`, one fragment, nobody else's —
and that file is the only thing on its branch. Two cards therefore touch two different
paths and cannot conflict at all. After a card's branch **merges**, `fold()` moves the
fragment's contents into the real logs and deletes it. Merges are serial, so the
insertion that used to race now happens one card at a time on the integration branch,
which is the one place it is safe.

This is the newsfragment pattern (towncrier, changelog.d) and it is chosen for the
reason those tools exist: a shared changelog is the canonical merge-conflict generator
in any repo with more than one branch in flight.

**Why not a union merge driver.** `-Xunion` or `merge=union` in `.gitattributes` would
dissolve the conflict declaratively and would also silently union two branches that
genuinely edited the same prose, with nothing able to tell the two apart. It treats the
symptom in a way that can lose content; this removes the cause.

**Why `.ai/`, not the board.** This lived at `<board>/.memory/` from 2026-08-23 to
2026-08-25, on the theory that a dot prefix would keep every board-scanning tool blind
to it — Obsidian's Bases view does honour that convention, but `nightshift.gates.
card_schema`'s orphan check does not: it walks every `.md` file under the board looking
for one outside a known lane, with no notion of an exempt subdirectory, dot-prefixed or
not. A card that wrote its fragment exactly as instructed therefore failed that gate as
an "orphaned card" (Karel, 2026-08-25: *"is board a good place to store these? ... maybe
some dedicated folder would be better"*). `.ai/` already holds this project's other
per-run, per-card machinery — `runs/`, `gates/`, `corrections.log` — so a fragment
belongs beside its actual neighbours, not inside the board it was mistaken for.
`card_schema`'s orphan check was also hardened to ignore any dot-prefixed board
subdirectory on general principle, in case the board ever grows another one, but the
fragment itself no longer depends on that exemption at all.

**Off unless declared.** A project with no `[[memory.fold]]` rows has no fragment
directory, no fold step and no behaviour change.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from nightshift import board as _board
from nightshift import textio
from nightshift.manifest import AI_DIR, FoldTarget, ManifestError
from nightshift.manifest import load as _load

__all__ = ["FRAGMENT_DIR", "Fragment", "fragment_path", "fragments", "targets",
           "insert_under", "fold"]

#: Where a card's pending record lives, relative to `.ai/` -- framework/build state,
#: not board content: it has no lane, it is deleted the moment it folds, and
#: `[memory].orientation`'s budget gate must not count it. See the module docstring's
#: "Why `.ai/`, not the board" for why this is not under `Board/`.
FRAGMENT_DIR = "memory-fragments"


@dataclass(frozen=True)
class Fragment:
    """One card's pending memory record: `{fold key: text}`, plus where it came from.

    The file is markdown with one `## <key>` section per fold target, so it is
    readable and editable by hand and needs no serialisation format:

        ## register
        - **Thing shipped** (2026-08-22, `card-id`): one line.

        ## history
        ### 2026-08-22 — Thing shipped (`card-id`)

        Several paragraphs.
    """
    card_id: str
    path: Path
    sections: dict[str, str]


def targets(root: Path) -> tuple[FoldTarget, ...]:
    """The declared fold targets, or `()` when the project declares none."""
    try:
        return _load(root).memory.fold
    except ManifestError:
        return ()


def fragment_dir(root: Path) -> Path:
    return root / AI_DIR / FRAGMENT_DIR


def fragment_path(root: Path, card_id: str) -> Path:
    return fragment_dir(root) / f"{card_id}.md"


def _sections(text: str) -> dict[str, str]:
    """`{heading: body}` for every `## ` section, bodies stripped.

    Deliberately tolerant: an unknown heading is kept and simply matches no target,
    which `fold` reports rather than discards. Losing a card's record because it
    used the wrong heading is exactly the invisible loss this whole mechanism is
    supposed to make impossible.
    """
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            found.setdefault(current, [])
        elif current is not None:
            found[current].append(line)
    return {name: "\n".join(body).strip() for name, body in found.items()}


def _legacy_fragment_dir(root: Path) -> Path:
    """Where a fragment lived from 2026-08-23 to 2026-08-25, before landing under
    `.ai/` instead of the board (see the module docstring's "Why `.ai/`, not the
    board"). Read-only fallback, for exactly one reason: a worker already dispatched
    with the old instruction baked into its prompt will still write here, and its
    branch will merge after this change is live. Without this, `fold()` would look
    only in the new location, find nothing, and silently skip that card's record —
    the exact loss this whole mechanism exists to prevent. Delete once no branch
    still to merge can contain a fragment at this path."""
    return _board.board_dir(root) / ".memory"


def fragments(root: Path) -> list[Fragment]:
    """Every pending fragment, oldest card id first for a stable fold order.

    Checks the legacy directory too (`_legacy_fragment_dir`) — see its docstring.
    """
    out: list[Fragment] = []
    for directory in (fragment_dir(root), _legacy_fragment_dir(root)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            out.append(Fragment(path.stem, path, _sections(text)))
    out.sort(key=lambda fragment: fragment.card_id)
    return out


def insert_under(text: str, heading: str, entry: str) -> str | None:
    """`text` with `entry` inserted directly below `heading`, or None.

    **The match is a prefix, and it must be unique.** Exact would be tighter and does
    not survive contact: Dungeoneer's register heading is `## Current State (updated
    2026-08-18, branch \\`ai/perks-tinkering\\`)` — a stamp that changes whenever
    someone edits the file, so an exact `under` would silently stop matching one day
    and the fold would start reporting a missing heading for a file that is fine.
    A prefix tolerates the stamp; requiring it to be *unique* is what keeps the
    tolerance from picking the wrong section.

    **Where exactly: immediately before the section's current newest entry**, not
    immediately after the heading. The two are the same thing only when the heading
    is followed straight away by the list, and Dungeoneer's register is not —
    `## Current State` is followed by a two-line paragraph explaining what a register
    entry is, and inserting "directly below the heading" wedged the new entry between
    the heading and its own preamble. Verified by running it against the real file
    before this rule was written; the first version shipped that bug.

    So the anchor is the first *entry-shaped* line after the heading — one starting
    with `-`, `*` or `#` — which is the current newest entry in a newest-first log,
    and skipping prose to reach it is what keeps a preamble intact. A section with no
    entry yet has no such line, and the entry goes after the heading and its
    preamble, at the end of the run of prose.

    None means "did not match exactly one heading" — absent, or ambiguous. Both are
    configuration errors and both must stop the fold: appending at the end instead
    would bury a card's record where nobody re-reads it, which is the silent loss
    this whole mechanism exists to prevent.
    """
    lines = text.splitlines()
    wanted = heading.strip()
    matches = [i for i, line in enumerate(lines)
               if line.strip() == wanted or line.strip().startswith(wanted)]
    if len(matches) != 1:
        return None
    start = matches[0] + 1
    at = None
    for index in range(start, len(lines)):
        if lines[index].lstrip()[:1] in ("-", "*", "#"):
            at = index
            break
    if at is None:
        # No entry yet: land after the heading and whatever preamble follows it,
        # which is the end of the section rather than the middle of its prose.
        at = len(lines)
        while at > start and not lines[at - 1].strip():
            at -= 1
    body = entry.strip().splitlines()
    return "\n".join(lines[:at] + body + _separator(lines, at) + lines[at:]) + "\n"


def _same_kind(anchor: str) -> "callable[[str], bool]":
    """Does another line start an entry of the same kind as `anchor`?

    Needed because "the next entry" cannot be "the next line starting with `-` or
    `#`": a history entry's *body* may contain bullets and sub-headings, and picking
    one of those would read the spacing off the wrong pair of lines.
    """
    hashes = len(anchor) - len(anchor.lstrip("#"))
    if hashes:
        prefix = "#" * hashes + " "
        return lambda line: line.startswith(prefix)
    bullet = anchor[:1]
    # Column 0 only: an indented `-` is a nested bullet inside the current entry.
    return lambda line: line[:1] == bullet and not line[:1].isspace()


def _separator(lines: list[str], at: int) -> list[str]:
    """`[""]` or `[]` — whatever already separates two entries in this section.

    Read off the file rather than fixed, because the two real targets disagree and
    both are deliberate: the orientation register is a tight list with no blank line
    between bullets, while the history log separates its dated sections with one. A
    hardcoded blank line reformats the register a little on every single card, which
    is both a diff nobody asked for and a slow walk toward the byte budget that gate
    watches.
    """
    if at >= len(lines):
        return []
    anchor = lines[at]
    is_entry = _same_kind(anchor)
    for index in range(at + 1, len(lines)):
        if is_entry(lines[index]):
            return [""] if not lines[index - 1].strip() else []
    # Only one entry in the section: fall back to the entry's own shape — a dated
    # `###` section wants air around it, a one-line bullet does not.
    return [""] if anchor.startswith("#") else []


def fold(root: Path, *, card_id: str = "", apply: bool = True) -> list[str]:
    """Fold pending fragments into their targets. Returns one report line each.

    `card_id` folds just that card's fragment — what the runner uses, immediately
    after that card's branch merges. Empty folds everything pending, which is the
    recovery path for a fragment left behind by an interrupted run.

    A fragment is deleted only once **every** section it carries has been written,
    so a renamed heading leaves the fragment on disk to be dealt with rather than
    dropping the card's record on the floor.
    """
    declared = targets(root)
    if not declared:
        return []
    by_key = {target.key: target for target in declared}
    report: list[str] = []

    for fragment in fragments(root):
        if card_id and fragment.card_id != card_id:
            continue
        if not fragment.sections:
            report.append(f"{fragment.card_id}: fragment is empty — left in place")
            continue
        unwritten: list[str] = []
        for key, entry in sorted(fragment.sections.items()):
            target = by_key.get(key)
            if target is None:
                unwritten.append(f"no `[[memory.fold]]` target has key = {key!r}")
                continue
            if not entry:
                continue
            path = root / target.path
            if not path.is_file():
                unwritten.append(f"{target.path} does not exist")
                continue
            folded = insert_under(path.read_text(encoding="utf-8"), target.under, entry)
            if folded is None:
                unwritten.append(f"{target.path} has no unique heading starting "
                                 f"{target.under!r}")
                continue
            if apply:
                textio.write_text_lf(path, folded)
            report.append(f"{fragment.card_id}: -> {target.path} "
                          f"under {target.under!r}")
        if unwritten:
            report.append(f"{fragment.card_id}: LEFT IN PLACE — " + "; ".join(unwritten))
            continue
        if apply:
            fragment.path.unlink(missing_ok=True)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    from nightshift.manifest import find_root

    # Karel's console is cp1252 and this prints paths and headings straight from a
    # project's own files. `report_tiger`'s stray U+2265 already killed a tool
    # mid-report here once, so the stream is pinned rather than the output policed.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Fold finished cards' memory fragments into the shared logs.")
    parser.add_argument("--card", default="", help="fold only this card's fragment")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be folded and change nothing")
    args = parser.parse_args(argv)

    root = find_root()
    lines = fold(root, card_id=args.card, apply=not args.dry_run)
    if not lines:
        print("nothing to fold" if targets(root) else
              "no [[memory.fold]] targets declared — the fold step is off")
    for line in lines:
        print(line)
    return 1 if any("LEFT IN PLACE" in line for line in lines) else 0


if __name__ == "__main__":
    raise SystemExit(main())
