"""What the diff reviewer is shown: the change, minus what no reviewer should spend on.

Two savings, both measured on reviewer transcripts (`token-economy.md` §1, §2 phase 4):

1. **Inline, when it fits.** The reviewer used to be handed a path, and spent its
   first turns reading the patch — often a `cat` of it and then the same patch again
   in four to six `sed` chunks, each chunk a lead-tier turn carrying everything
   before it. A diff under `INLINE_MAX_CHARS` now arrives inside the prompt, which is
   also the cached prefix.
2. **Filtered, with the omission stated.** Hunks no reviewer judgment applies to are
   cut and listed as one line each: translation values in a project's target
   languages (the i18n gates own parity, untranslated and loanword checks, and
   naturalness is a translator's job, not a diff reviewer's), binary files (a patch
   shows nothing but "differ"), and the board's generated views (regenerated, never
   authored). **Docs, comments and memory files always stay in** — 11 of 14 historical
   `needs_fix` verdicts were wrong prose, which makes those the hunks most worth a
   reviewer's attention, not the least.

Everything here fails open: a filter that cannot tell returns the diff untouched.
"""
from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Inline up to this many characters (~15–20k tokens); above it the reviewer gets a
#: path, as before. Big enough for an ordinary card's diff, small enough that a
#: sweeping change does not crowd the criteria out of the prompt.
INLINE_MAX_CHARS = 60_000

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class ReviewDiff:
    text: str
    omitted: list[str] = field(default_factory=list)

    def omitted_note(self, full_patch: str) -> str:
        if not self.omitted:
            return ""
        lines = "\n".join(f"- {entry}" for entry in self.omitted)
        return ("Left out of this diff because a gate or another stage owns it — the "
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


def _target_zones(root: Path) -> tuple[str, list[tuple[str, int, int]]] | None:
    """`(i18n file, [(lang, first line, last line)])` for the target languages,
    from the project's own i18n adapter, or None when there is nothing to filter."""
    from nightshift import manifest

    spec = manifest.load(root).i18n
    if spec is None or not spec.targets:
        return None
    module_spec = importlib.util.spec_from_file_location(
        "_nightshift_reviewdiff_i18n_adapter", root / spec.adapter)
    if module_spec is None or module_spec.loader is None:
        return None
    adapter = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(adapter)
    rel = Path(adapter.i18n_path(root)).resolve().relative_to(root.resolve()).as_posix()
    zones = []
    for lang, keys in adapter.zone_key_lines(root).items():
        if lang in spec.targets and keys:
            zones.append((lang, min(keys.values()), max(keys.values())))
    return (rel, zones) if zones else None


def _split_hunks(chunk: str) -> tuple[str, list[str]]:
    head, hunks = "", []
    for line in chunk.splitlines(keepends=True):
        if _HUNK.match(line):
            hunks.append(line)
        elif hunks:
            hunks[-1] += line
        else:
            head += line
    return head, hunks


def _hunk_in_zones(hunk: str, zones: list[tuple[str, int, int]]) -> tuple[bool, int, int]:
    """Whether every changed line of `hunk` falls inside a target-language zone of
    the new file, and its added/removed line counts."""
    lines = hunk.splitlines()
    match = _HUNK.match(lines[0])
    if not match:
        return False, 0, 0
    new_no = int(match.group(1))
    added = removed = 0
    inside = True
    for line in lines[1:]:
        if line.startswith("\\"):
            continue
        mark = line[:1]
        if mark in "+-":
            if not any(start <= new_no <= end for _, start, end in zones):
                inside = False
            if mark == "+":
                added += 1
                new_no += 1
            else:
                removed += 1
        else:
            new_no += 1
    return inside and (added or removed) > 0, added, removed


def filter_diff(diff: str, root: Path) -> ReviewDiff:
    """The reviewer's view of `diff`, judged against the checkout at `root` (the
    branch under review — the new side of every hunk)."""
    try:
        return _filter(diff, root)
    except Exception:
        return ReviewDiff(diff)


def _filter(diff: str, root: Path) -> ReviewDiff:
    from nightshift import board

    try:
        i18n = _target_zones(root)
    except Exception:
        i18n = None
    generated = set(board.GENERATED_VIEWS)

    kept: list[str] = []
    omitted: list[str] = []
    binaries: list[str] = []
    for chunk in _file_chunks(diff):
        path = _path_of(chunk)
        if path and _is_binary(chunk):
            binaries.append(path)
            continue
        if path in generated:
            omitted.append(f"`{path}` — a generated board view")
            continue
        if i18n and path == i18n[0]:
            head, hunks = _split_hunks(chunk)
            survivors, dropped, plus, minus = [], 0, 0, 0
            for hunk in hunks:
                inside, added, removed = _hunk_in_zones(hunk, i18n[1])
                if inside:
                    dropped, plus, minus = dropped + 1, plus + added, minus + removed
                else:
                    survivors.append(hunk)
            if dropped:
                langs = "/".join(lang for lang, _, _ in i18n[1])
                omitted.append(f"`{path}` — {dropped} hunk(s) of {langs} translation "
                               f"values (+{plus} −{minus})")
            if survivors:
                kept.append(head + "".join(survivors))
            continue
        kept.append(chunk)
    if binaries:
        omitted.append("binary: " + ", ".join(f"`{p}`" for p in binaries))
    return ReviewDiff("".join(kept), omitted)
