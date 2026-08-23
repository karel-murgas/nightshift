"""Gate: no tracked text file carries a git conflict marker.

**Earned by an observed failure, not by being easy** (`00_architecture.md` §15), and
it was proposed by the correction that observed it — then not built for two weeks,
during which the same class recurred.

Karel, 2026-08-09, `conflict-marker-survived-a-hand-resolved-merge`: resolving a
rebase by hand, the content between the markers was edited correctly and the trailing
`>>>>>>> 0c5905f (...)` line was left in place. It survived staging, committing,
merging into the integration branch and pushing, and sat in a shipped, reviewed-as-
complete card for about a day — caught only while editing the same card for an
unrelated reason. The correction's own wording: *a successful git operation is not
evidence a hand-resolved conflict was resolved correctly* — git only cares that every
marker-delimited region was edited into **something**, never that the something is
marker-free content. It ends by naming this gate as the cheap, mechanical fix.

On 2026-08-23 two cards' register entries collided in `.claude/memory/state.md` and
`state_history.md`, three regions were hand-resolved in one session, and the only
thing standing between that and a repeat of 2026-08-09 was the resolver remembering
to grep. That is the gap this closes — and it is now load-bearing rather than
hygienic, because `runner._resolve_conflict` lets an **agent** resolve a rebase
unattended. Automated resolution is only as trustworthy as the check that follows it.

**Zero tolerance, and no appeal marker.** A real conflict marker in committed content
is unambiguous evidence of an unfinished resolution — there is no legitimate instance
to appeal, and a gate that can be waived is one that will be waived at 3 AM. A file
that genuinely needs to *discuss* the syntax indents its example, which the
whole-line anchoring in `conflictmarkers` already ignores.

Scope is every tracked text file, from `git ls-files`, not a directory list: the
2026-08-09 instance was in `Board/`, tonight's would have been in `.claude/memory/`,
and neither is tooling or source. Binary blobs are skipped by `conflictmarkers.scan`,
which cannot decode them.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nightshift import conflictmarkers
from nightshift.gates.base import Violation

NAME = "conflict_markers"
FAST = True
DESCRIPTION = "no tracked text file carries a git conflict marker"


def _tracked(repo_root: Path) -> list[str]:
    """Every tracked path, or `[]` when git cannot answer.

    `[]` on failure is "cannot look", not "clean" — but unlike `line_endings` there
    is no second half of the check to report, so an unanswerable scan is silent.
    A gate that invented violations from a missing git binary would be worse.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [rel for rel in (out.stdout or "").split("\0") if rel.strip()]


def check(repo_root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for rel in _tracked(repo_root):
        target = repo_root / rel
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in conflictmarkers.markers_in(text):
            violations.append(Violation(
                rel, number,
                f"git conflict marker left behind — `{line.strip()[:80]}`. A rebase or "
                f"merge was resolved without removing every marker; git does not check "
                f"this and neither did anything else until this gate",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
