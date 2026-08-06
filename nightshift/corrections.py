#!/usr/bin/env python3
"""Read `.ai/corrections.log` and report it. Deterministic; no LLM anywhere
(`00_architecture.md` §12).

The log existed from Session A and was written to for six sessions without
anything ever reading it back. This is the reader. It does exactly the part a
script can do -- parse, count, tabulate, and refuse a malformed line -- and
nothing more: deciding *which* class an entry belongs to is judgment and stays
with whoever appends the line.

Usage (from anywhere inside the repo being reported on):
    python -m nightshift.corrections              # the full report
    python -m nightshift.corrections --class derived-not-verified   # one cluster's entries
    python -m nightshift.corrections --since 2026-07-23
    python -m nightshift.corrections --compact    # archive every resolved entry (see below)

The lifecycle half (`12_corrections_lifecycle.md`, 2026-07-26): a resolved entry's
NOTE ends `[[disposition: kind: pointer]]` -- the reader's own missing field,
separating "acted on" from "open". `--compact` moves every entry that now
carries one out of the active log into the sibling `.ai/corrections.archive.log`,
so the file that gets read back stays only what nobody has acted on yet.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from nightshift import textio  # both logs are committed; write_text would CRLF them on Windows
from nightshift.manifest import find_root

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOG = Path(".ai/corrections.log")
ARCHIVE = Path(".ai/corrections.archive.log")  # gate-ok(source_reference_liveness): created by
# --compact the first time it runs; this repo's own log has never been compacted, so the file
# does not exist here yet, though Dungeoneer's .gitignore tracks it once created.
VOCAB = Path(".ai/gates/data/corrections_vocab.json")

FIELDS = ("date", "slug", "klass", "channel", "gate", "note")

# A resolved entry's NOTE ends with `[[disposition: kind: pointer]]`
# (12_corrections_lifecycle.md). Deliberately NOT a seventh pipe field: several
# existing notes contain a literal `|` (`[grid|classic]`, a quoted table row),
# so widening the split on `|` would silently corrupt them. The disposition is
# the LAST such marker at end of line: an entry may legitimately mention the
# marker syntax inside its own prose (the entry documenting this very format
# does), so the capture stops at the first `]]` -- `(?!\]\])` -- and `.search`
# skips any earlier inline example, matching only a real trailing annotation.
# The five-pipe split below is completely unchanged from before this field existed.
_DISPOSITION_RE = re.compile(r"\[\[disposition:\s*((?:(?!\]\]).)+?)\s*\]\]\s*$")

_ARCHIVE_HEADER = (
    "# Corrections archive\n"
    "#\n"
    "# Resolved entries moved out of .ai/corrections.log by\n"
    "# `python -m nightshift.corrections --compact`. Same six\n"
    "# pipe fields as the active log; every entry here carries a `[[disposition: ...]]`\n"
    "# annotation on its NOTE -- that is what made it eligible to move. Read by\n"
    "# `corrections.report()` for historical cluster counts; never by `backlog()`, so\n"
    "# an archived entry can never itself re-trigger the digest's harvest nudge.\n"
    "\n"
)


@dataclass(frozen=True)
class Entry:
    line_no: int
    date: str
    slug: str
    klass: str
    channel: str
    gate: str
    note: str
    disposition: str = ""


def load_vocab(root: Path) -> dict[str, dict[str, str]]:
    data = json.loads((root / VOCAB).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def _split_disposition(note: str) -> tuple[str, str]:
    m = _DISPOSITION_RE.search(note)
    if not m:
        return note, ""
    return note[: m.start()].rstrip(), m.group(1).strip()


def _parse_text(text: str) -> tuple[list[Entry], list[tuple[int, str]]]:
    entries: list[Entry] = []
    bad: list[tuple[int, str]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in raw.split("|", len(FIELDS) - 1)]
        if len(parts) != len(FIELDS):
            bad.append((line_no, f"{len(parts)} field(s), expected {len(FIELDS)}"))
            continue
        note, disposition = _split_disposition(parts[-1])
        entries.append(Entry(line_no, *parts[:-1], note, disposition))
    return entries, bad


def parse(root: Path) -> tuple[list[Entry], list[tuple[int, str]]]:
    """Return (entries, malformed) where malformed is [(line_no, reason)].

    Parsing never raises. A malformed line is a finding to report, not a
    crash -- the log is hand-appended and a reader that dies on one bad line
    is a reader nobody runs twice.
    """
    return _parse_text((root / LOG).read_text(encoding="utf-8"))


def parse_archive(root: Path) -> tuple[list[Entry], list[tuple[int, str]]]:
    """Same shape as `parse()`, over the archive. `([], [])` if it does not exist
    yet -- a fresh checkout, or one where nothing has ever been compacted."""
    path = root / ARCHIVE
    if not path.is_file():
        return [], []
    return _parse_text(path.read_text(encoding="utf-8"))


def validate(entries: list[Entry], vocab: dict[str, dict[str, str]]) -> list[tuple[int, str]]:
    """Closed-vocabulary and shape checks. Returns [(line_no, reason)]."""
    problems: list[tuple[int, str]] = []
    disposition_vocab = vocab.get("disposition", {})
    for e in entries:
        if len(e.date) != 10 or e.date[4] != "-" or e.date[7] != "-" or not e.date.replace("-", "").isdigit():
            problems.append((e.line_no, f"date {e.date!r} is not YYYY-MM-DD"))
        for field, key in (("klass", "class"), ("channel", "channel"), ("gate", "gate")):
            value = getattr(e, field)
            if value not in vocab[key]:
                allowed = ", ".join(sorted(vocab[key]))
                problems.append((e.line_no, f"{key} {value!r} not in vocabulary ({allowed})"))
        if not e.slug or " " in e.slug:
            problems.append((e.line_no, f"slug {e.slug!r} must be a non-empty single token"))
        if len(e.note) < 40:
            problems.append((e.line_no, f"note is {len(e.note)} chars; a one-liner is not evidence"))
        if e.disposition:
            kind, sep, pointer = e.disposition.partition(":")
            kind, pointer = kind.strip(), pointer.strip()
            if not sep:
                problems.append((e.line_no, f"disposition {e.disposition!r} must be 'kind: pointer'"))
            elif kind not in disposition_vocab:
                allowed = ", ".join(sorted(disposition_vocab))
                problems.append((e.line_no, f"disposition kind {kind!r} not in vocabulary ({allowed})"))
            elif not pointer:
                problems.append((e.line_no, f"disposition {e.disposition!r} has no pointer after the kind"))
    return problems


def backlog(root: Path) -> tuple[int, str | None]:
    """Open (undispositioned) defects in the ACTIVE log, and the oldest one's date.

    `(0, None)` when the log does not exist or nothing is open. Bookkeeping rows
    (`klass == "process-metric"`) never count -- they are not lessons waiting to
    be acted on. Archive entries never count either, by construction: they all
    carry a disposition, or `--compact` would not have moved them.
    """
    if not (root / LOG).is_file():
        return 0, None
    entries, _ = parse(root)
    open_defects = [e for e in entries if not e.disposition and e.klass != "process-metric"]
    if not open_defects:
        return 0, None
    return len(open_defects), min(e.date for e in open_defects)


def compact(root: Path) -> int:
    """Move every entry that now carries a disposition out of the active log and
    into the archive, verbatim. Returns the number moved.

    Operates on raw lines, not on re-serialised fields, so an entry's exact text
    -- including any `|` inside its NOTE -- survives the move unchanged.
    """
    log_path = root / LOG
    text = log_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    entries, _ = _parse_text(text)
    move = {e.line_no for e in entries if e.disposition}
    if not move:
        return 0

    kept = [line for i, line in enumerate(lines, start=1) if i not in move]
    moved = [line for i, line in enumerate(lines, start=1) if i in move]

    archive_path = root / ARCHIVE
    needs_header = not archive_path.is_file() or archive_path.stat().st_size == 0
    if moved and not moved[-1].endswith("\n"):
        moved[-1] += "\n"
    textio.append_text_lf(archive_path,
                          (_ARCHIVE_HEADER if needs_header else "") + "".join(moved))

    textio.write_text_lf(log_path, "".join(kept))
    return len(move)


def _table(title: str, counts: Counter, total: int) -> str:
    width = max((len(k) for k in counts), default=0)
    out = [title, "-" * len(title)]
    for key, n in counts.most_common():
        bar = "#" * n
        pct = 100 * n / total if total else 0
        line = f"  {key:<{width}}  {n:>3}  {pct:>4.0f}%  {bar}"
        out.append(line)
    return "\n".join(out)


def report(root: Path | None = None) -> str:
    """`root=None` finds the repo (`manifest.find_root`).

    It used to default to `Path(__file__).parent.parent`, which was this module's
    own repo and is now a directory inside site-packages. A default that is
    evaluated at import time cannot be right for a module that ships to N repos, so
    the default is now "find out", computed per call.
    """
    root = root or find_root()
    vocab = load_vocab(root)
    entries, malformed = parse(root)
    problems = validate(entries, vocab)
    archived, _ = parse_archive(root)
    combined = entries + archived
    total = len(combined)

    defects = [e for e in combined if e.klass != "process-metric"]
    open_count, oldest = backlog(root)
    out: list[str] = []
    summary = f"{total} entries  ({len(defects)} defects, {total - len(defects)} bookkeeping rows)"
    if archived:
        summary += f"  [{len(entries)} active, {len(archived)} archived]"
    out.append(summary)
    out.append(f"open (undispositioned) backlog: {open_count}"
               + (f", oldest {oldest}" if oldest else ""))
    dates = sorted({e.date for e in combined})
    if dates:
        out.append(f"span {dates[0]} .. {dates[-1]}")
    out.append("")
    out.append(_table("by class", Counter(e.klass for e in combined), total))
    out.append("")
    out.append(_table("by channel", Counter(e.channel for e in combined), total))
    out.append("")
    out.append(_table("by gate verdict", Counter(e.gate for e in combined), total))
    out.append("")
    out.append(_table("by date", Counter(e.date for e in combined), total))

    if malformed or problems:
        out.append("")
        out.append("PROBLEMS")
        out.append("--------")
        for line_no, reason in sorted(malformed + problems):
            out.append(f"  .ai/corrections.log:{line_no} — {reason}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report on .ai/corrections.log.")
    parser.add_argument("--class", dest="klass", help="list the entries in one class")
    parser.add_argument("--channel", help="list the entries found by one channel")
    parser.add_argument("--since", help="only entries on or after this YYYY-MM-DD")
    parser.add_argument("--compact", action="store_true",
                        # gate-ok(source_reference_liveness): help text for the flag that
                        # creates .ai/corrections.archive.log; see the ARCHIVE constant above.
                        help="move every entry that now carries a [[disposition: ...]] "
                             "into .ai/corrections.archive.log")
    args = parser.parse_args(argv)

    root = find_root()
    if args.compact:
        moved = compact(root)
        print(f"moved {moved} resolved entr{'y' if moved == 1 else 'ies'} to {ARCHIVE}")
        return 0

    entries, _ = parse(root)
    if args.since:
        entries = [e for e in entries if e.date >= args.since]

    if args.klass or args.channel:
        selected = [
            e for e in entries
            if (not args.klass or e.klass == args.klass)
            and (not args.channel or e.channel == args.channel)
        ]
        for e in selected:
            print(f"{e.date}  {e.slug}  [{e.channel}/{e.gate}]")
            print(f"    {e.note[:160]}{'…' if len(e.note) > 160 else ''}")
        print(f"\n{len(selected)} entr(y/ies).")
        return 0

    print(report(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
