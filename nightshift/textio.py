#!/usr/bin/env python3
r"""LF-preserving text writes for the files this tooling commits.

A repo running this framework pins `.gitattributes` to `* text=auto eol=lf` -- LF
in the index *and* in the working tree, so that no filter ever rewrites anything
and no writer can leave a phantom-dirty file (one git reports as modified forever,
with an empty diff, because only the line-ending representation disagrees). The
`line_endings` gate enforces it, and `nightshift init` offers to write the line,
because on Windows Git ships `core.autocrlf=true` and a fresh clone without it
makes that gate fire immediately (07_portability.md §5).

`pathlib.Path.write_text()` breaks that pin on Windows. It opens in text mode with
`newline=None`, which translates every `\n` to `os.linesep` -- `\r\n` here -- so a
tool that reads an LF file and writes it back converts it:

    >>> p.write_bytes(b"x\ny\n")
    >>> p.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    >>> p.read_bytes()
    b'x\r\ny\r\n'

That is exactly the read-modify-write shape `board.py` uses for every card, and
`digest.py`/`corrections.py`/`stale_sweep.py` use for the files they own. Measured
2026-07-30 (the loose-end sweep before Session K): running `corrections.py --compact`
and `digest.py` on this box rewrote `.ai/corrections.log`, `.ai/corrections.archive.log`
and `Digest.md` as CRLF and turned the `line_endings` gate red. The board writers had
the same bug and had simply not run since the gate landed, so the next card move
would have tripped it.

**Every `.ai/` write to a git-tracked path goes through here.** Gitignored artefacts
under `.ai/runs/` do not need it -- git never sees them -- but there is no cost to
using it there either, and a uniform rule is what stops the seventh writer from
copying the wrong pattern. `encoding="utf-8"` is not optional and not a default:
this tree carries non-ASCII in cards, in the corrections log and in the digest.

Deliberately stdlib-only and dependency-free -- it imports nothing from `nightshift`
either -- so it is safe to import from any module regardless of import order. It is
the one thing almost everything else here depends on, which is exactly why it must
depend on nothing.
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["write_text_lf", "append_text_lf"]


def write_text_lf(path: Path, text: str) -> None:
    """Write `text` to `path` as UTF-8 with LF endings, whatever the platform.

    `newline=""` disables translation on write, so the bytes land exactly as the
    string spells them. Any `\r\n` already inside `text` survives -- this normalises
    the *writer*, not the content; a caller that has genuinely read CRLF content
    should strip it before calling.
    """
    path.write_text(text, encoding="utf-8", newline="")


def append_text_lf(path: Path, text: str) -> None:
    """Append `text` to `path` as UTF-8 with LF endings. Same contract as
    `write_text_lf`, for the log-append case (`corrections.py`'s archive)."""
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(text)
