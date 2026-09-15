"""What the diff reviewer is shown: the change, minus what carries nothing to judge.

Two savings, both measured on reviewer transcripts (`token-economy.md` §1, §2 phase 4):

1. **Inline, when it fits.** The reviewer used to be handed a path, and spent its
   first turns reading the patch — often a `cat` of it and then the same patch again
   in four to six `sed` chunks, each chunk a lead-tier turn carrying everything
   before it. A diff under `INLINE_MAX_CHARS` now arrives inside the prompt, which is
   also the cached prefix.
2. **Filtered, with the omission stated.** Only files with nothing a reader could
   judge are cut, each listed by name: binary files (a patch shows nothing but
   "differ") and the board's generated views (regenerated, never authored).

**Translation values stay in, deliberately.** Filtering the target-language hunks was
built and removed the same day (2026-09-15): the gates check key parity, untranslated
leftovers and loanwords, but nothing in the pipeline checks that a translation *means*
what the source says — the reviewer reading it is the only such check before the
maintainer plays the result. **Docs, comments and memory files always stay in** too:
11 of 14 historical `needs_fix` verdicts were wrong prose.

Everything here fails open: a filter that cannot tell returns the diff untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Inline up to this many characters (~15–20k tokens); above it the reviewer gets a
#: path, as before. Big enough for an ordinary card's diff, small enough that a
#: sweeping change does not crowd the criteria out of the prompt.
INLINE_MAX_CHARS = 60_000


@dataclass
class ReviewDiff:
    text: str
    omitted: list[str] = field(default_factory=list)

    def omitted_note(self, full_patch: str) -> str:
        if not self.omitted:
            return ""
        lines = "\n".join(f"- {entry}" for entry in self.omitted)
        return ("Left out of this diff because there is nothing in it to read — the "
                f"unfiltered patch is at `{full_patch}` if you need it:\n{lines}\n")


def _file_chunks(diff: str) -> list[str]:
    chunks: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") or not chunks:
            chunks.append(line)
        else:
            chunks[-1] += line
    return chunks


def _path_of(chunk: str) -> str:
    first = chunk.split("\n", 1)[0]
    match = re.match(r'diff --git "?a/.*?"? "?b/(.*?)"?$', first.rstrip("\r"))
    return match.group(1) if match else ""


def _is_binary(chunk: str) -> bool:
    return "\nBinary files " in chunk or "\nGIT binary patch" in chunk


def filter_diff(diff: str) -> ReviewDiff:
    """The reviewer's view of `diff`."""
    try:
        return _filter(diff)
    except Exception:
        return ReviewDiff(diff)


def _filter(diff: str) -> ReviewDiff:
    from nightshift import board

    generated = set(board.GENERATED_VIEWS)
    kept: list[str] = []
    omitted: list[str] = []
    binaries: list[str] = []
    for chunk in _file_chunks(diff):
        path = _path_of(chunk)
        if path and _is_binary(chunk):
            binaries.append(path)
        elif path in generated:
            omitted.append(f"`{path}` — a generated board view")
        else:
            kept.append(chunk)
    if binaries:
        omitted.append("binary: " + ", ".join(f"`{p}`" for p in binaries))
    return ReviewDiff("".join(kept), omitted)
