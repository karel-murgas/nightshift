"""Gate: a doc must not name a file or symbol that no longer exists
(08_staleness.md §3.1, staleness class S1 — the flagship).

Scope, exemption mechanism and extraction all live in `doc_scan.py`; this
module is the policy on top of them:

  * every extracted path must exist on disk;
  * every extracted backticked symbol must appear in the source tree, either
    as an AST definition or as a token/string-literal somewhere under
    `dungeoneer/` or `tests/`;
  * a doc whose dangling ratio exceeds `_ORPHAN_RATIO` is reported as ONE
    orphan-candidate line instead of N violations (§3.3, S3) — the whole point
    being that "this file has no live referent" is one decision, not eleven.

Exemptions are `doc_scope: history` frontmatter and `<!-- stale-ok: reason -->`
markers, never a per-symbol allowlist (§3.1 rejects that explicitly: it grows
monotonically, encodes no reason, and would have to list every symbol the
project ever deleted).
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from nightshift.gates import doc_scan
from nightshift.gates.base import Violation

NAME = "doc_reference_liveness"
FAST = False  # builds an AST index of the whole source tree (~1 s)
DESCRIPTION = "docs must not name files or symbols that no longer exist"

# §3.3: "~60% of total resolved references". Below this the individual lines
# are the actionable unit; above it, the file itself is the finding.
_ORPHAN_RATIO = 0.6
_ORPHAN_MIN_REFS = 5  # a 2-reference file at 100% is noise, not an orphan


def _dangling(repo_root: Path) -> tuple[list[doc_scan.Reference], dict[str, int]]:
    bad: list[doc_scan.Reference] = []
    totals: dict[str, int] = defaultdict(int)
    for path in doc_scan.doc_files(repo_root):
        for ref in doc_scan.extract_references(path, repo_root):
            totals[ref.file] += 1
            ok = (
                doc_scan.resolve_path(repo_root, ref.text, doc=path)
                if ref.kind == "path"
                else any(
                    doc_scan.resolve_symbol(repo_root, candidate)
                    for candidate in (ref.text, *ref.alts)
                )
            )
            if not ok:
                bad.append(ref)
    return bad, dict(totals)


def check(repo_root: Path) -> list[Violation]:
    bad, totals = _dangling(repo_root)

    by_file: dict[str, list[doc_scan.Reference]] = defaultdict(list)
    for ref in bad:
        by_file[ref.file].append(ref)

    violations: list[Violation] = []
    for file, refs in sorted(by_file.items()):
        total = totals.get(file, 0)
        if total >= _ORPHAN_MIN_REFS and len(refs) / total >= _ORPHAN_RATIO:
            violations.append(
                Violation(
                    file,
                    refs[0].line,
                    f"doc_reference_liveness: orphan candidate — {len(refs)}/{total} "
                    f"references dangle; this doc may have no live referent at all",
                )
            )
            continue
        for ref in sorted(refs, key=lambda r: r.line):
            violations.append(
                Violation(
                    ref.file,
                    ref.line,
                    # The offending token is quoted, NOT backticked. Two reasons,
                    # both learned the hard way on 2026-07-30: the runner writes
                    # this message verbatim into a failed card's `## Error`
                    # section, so a backticked token here becomes a *new* dangling
                    # reference in a tracked .md file that this same gate then
                    # flags — one root cause read as three consecutive card
                    # failures. And the fix is not obvious from the message alone,
                    # so it now names both escapes.
                    f'doc_reference_liveness: {ref.kind} "{ref.text}" does not exist '
                    f"in the source tree. If it names something outside this repo "
                    f"(another project's file, a third-party symbol), remove the "
                    f"backticks or cover the section with a stale-ok marker; if it "
                    f"named ours and it is gone, update the doc",
                )
            )
    return violations


def baseline(repo_root: Path) -> int:
    """Raw dangling-reference count, orphan-collapsing not applied — the
    number 08_staleness.md §8 asks to be recorded before any cleanup."""
    return len(_dangling(repo_root)[0])


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent.parent.parent
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(root):
        print(violation)
