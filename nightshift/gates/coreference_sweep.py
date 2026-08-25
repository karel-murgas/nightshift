"""Gate: a distinctive literal this diff replaced must not survive in a live doc.

**This is the reviewer's most repetitive finding, moved to a script.** On the
2026-08-25 night every `needs_fix` verdict the diff reviewer returned was one
shape, and it was the same shape four times:

  * `18 of 1351 en keys` — the corpus count was corrected to 1349 in two files
    and left standing in a third
  * the vault curve `77/251/500/815/1192` — rewritten in `design_detail.md`,
    left in `arch_detail.md` beside the constant that no longer produces it
  * `EXPECTED_IDS` — deleted from the test suite by the same diff, still taught
    by `.ai/recipes/add-a-perk.md`
  * `Row 79.` where every other reference in the same diff said 80

The first three are one mechanical question: *this diff stopped saying X
somewhere — is anything still saying X?* Nothing asked it, so a reviewer did,
one card at a time, at lead-tier prices, and each `needs_fix` cost the card an
attempt. **The reviewer should be the last net, not the first**: anything a grep
can verify has no business consuming a review round.

**What this actually catches, checked against those branches rather than
argued: two of the four.** Replayed on `ai/economy-early-late-balance` it names
`.claude/memory/arch_detail.md:142` — the exact line the reviewer's finding
named — and on `ai/catalog-registry-and-guards` it names the surviving
`EXPECTED_IDS` citations. It finds nothing on
`ai/player-text-explains-design-decisions`, correctly: `1351` is a bare integer,
excluded by the shape rules below, and `Row 79` is not a co-reference at all —
that number appeared nowhere else and was simply wrong. Spelled out because a
gate that quietly covers half of a class reads as covering the class.

Scope and exemptions come from `doc_scan`, so a `doc_scope: history` file —
whose *job* is to say "the curve used to be 77/251/500/815/1192" — never fails
this, and a `<!-- stale-ok: ... -->` section is excused with a reason on the
record. `deletion_sweep` is the sibling that watches files and top-level
class/def; this one watches the literals inside them.

**Two token shapes, and the narrowness is the design.** A gate that fires on a
card's own honest edits gets appealed into uselessness, so each shape here was
picked because a false positive is close to impossible: a run of three or more
multi-digit numbers joined by slashes is a computed series and nothing else, and
a SCREAMING_SNAKE identifier the diff deleted is a symbol, not prose. Bare
integers — which would have caught the `1351` above — are deliberately excluded:
line-number citations, years, byte counts and test counts all share that shape,
and the resulting noise would cost more than the finding. A project that wants
its corpus counts checked should state the claim precisely in its own gate; this
one only reports what it can be sure of.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nightshift.gates import doc_scan
from nightshift import branches
from nightshift.gates.base import Violation

NAME = "coreference_sweep"
FAST = True
DESCRIPTION = ("a numeric series or SCREAMING_SNAKE symbol this diff replaced "
               "must not survive in a live doc")

#: Three or more numbers of two digits or more, slash-joined: `77/251/500/815`.
#: A computed series — a curve, a per-level table, a set of measured values — and
#: essentially never anything else. Two would match dates and ratios.
_SERIES = re.compile(r"\b\d{2,}(?:/\d{2,}){2,}\b")

#: `EXPECTED_IDS`, `CONTRACT_VAULT_EXP`. At least one underscore and six
#: characters, so `OK`, `ID` and `HTTP` are out. Matched anywhere, including
#: inside a code span, because that is exactly where a doc cites a symbol.
_SYMBOL = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")

#: Tokens a diff removes constantly without meaning anything by it. Kept tiny
#: and explicit rather than heuristic — a growing denylist is the sign the token
#: shapes above are too broad, and that is the thing to fix instead.
_NEVER = frozenset({"TODO", "FIXME", "NOTE", "XXX"})


def _git(repo_root: Path, args: list[str]) -> str:
    # `encoding=` is not optional on Windows — see `deletion_sweep._git` for the
    # decode failure this avoids and the card it cost.
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False,
        encoding="utf-8", errors="replace",
    )
    return result.stdout or ""


def _merge_base(repo_root: Path) -> str:
    for base in branches.merge_base_candidates(repo_root):
        found = _git(repo_root, ["merge-base", "HEAD", base]).strip()
        if found:
            return found
    return ""


def _tokens(text: str) -> set[str]:
    return ({m.group(0) for m in _SERIES.finditer(text)}
            | {m.group(0) for m in _SYMBOL.finditer(text)}) - _NEVER


def replaced_tokens(repo_root: Path) -> dict[str, str]:
    """{token -> the file that stopped saying it}, over this branch's whole diff.

    "Stopped saying it" is per file and deliberately strict: the token must
    appear on a removed line and on no added line *of the same file*. A card that
    moves a constant's citation from one paragraph to another within a doc has
    not stopped saying it, and must not be reported.

    Diffed against the merge base with the working tree included, so an
    uncommitted edit is judged the same as a committed one — the on-save hook and
    the runner's post-attempt check both want the same answer.
    """
    base = _merge_base(repo_root)
    if not base:
        return {}
    # `-U0`: only the changed lines, so unchanged context cannot look removed.
    # `--no-color`, `--no-ext-diff`: a user's diff pager or external differ must
    # not decide what a gate sees.
    diff = _git(repo_root, ["diff", "-U0", "--no-color", "--no-ext-diff", base])
    per_file: dict[str, tuple[set[str], set[str]]] = {}
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = path[2:] if path.startswith("b/") else path
            per_file.setdefault(current, (set(), set()))
            continue
        if not current or line.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if line.startswith("-"):
            per_file[current][0].update(_tokens(line[1:]))
        elif line.startswith("+"):
            per_file[current][1].update(_tokens(line[1:]))

    gone: dict[str, str] = {}
    for path, (removed, added) in per_file.items():
        if path == "/dev/null" or not (repo_root / path).is_file():
            # A file the diff deleted outright is `deletion_sweep`'s subject, not
            # this one: every token in it is "removed", which would be noise.
            continue
        for token in removed - added:
            gone.setdefault(token, path)
    return gone


def check(repo_root: Path) -> list[Violation]:
    gone = replaced_tokens(repo_root)
    if not gone:
        return []
    violations: list[Violation] = []
    for doc in doc_scan.doc_files(repo_root):
        text = doc_scan._read(doc)
        if not text or doc_scan.is_exempt_file(text):
            continue
        rel = doc_scan.relpath(doc, repo_root)
        exempt = doc_scan.exempt_lines(text)
        for number, line in enumerate(text.splitlines(), start=1):
            if number in exempt:
                continue
            for token in _tokens(line):
                source = gone.get(token)
                # `source == rel` is the doc that made the change — it is allowed
                # to still mention the old value in the same breath ("was X, now
                # Y"), and it is the one place a reader has the context to tell.
                if source is None or source == rel:
                    continue
                violations.append(Violation(
                    rel, number,
                    f"{NAME}: `{token}` was replaced in `{source}` by this diff but "
                    f"still stands here. Update it, or if this line is deliberately "
                    f"historical mark the section `<!-- stale-ok: ... -->` or the "
                    f"file `doc_scope: history`"))
    return violations
