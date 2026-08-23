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

**Appeals are file-scope, and that concession was earned within the hour.** This gate
originally shipped saying *"zero tolerance, and no appeal marker — a real conflict
marker in committed content is unambiguous evidence of an unfinished resolution, so
there is no legitimate instance to appeal."* The very first file written against it
was a legitimate instance: `tests/test_gate_conflict_markers.py` has to contain real
column-0 markers, because the defect it pins is markers at column 0, and a fixture
that dodged them would be testing something else. The gate went red on its own test
the moment that file became tracked. A rule asserted about a corpus and refuted by
the corpus's first member is the `derived-not-verified` shape this repo logs most.

The scope is the **file**, not the line, and that is forced rather than chosen: the
content being excused sits inside string literals and prose, where a `# gate-ok`
comment cannot be placed without editing the very bytes under test. So any line
containing `gate-ok(conflict_markers): <reason>` exempts the whole file. In a Python
file that is an ordinary comment, so `gate_appeals` still validates the gate name and
the reason and still counts the debt — the appeal stays inside the existing process
rather than inventing a private one.

It remains a narrow concession. A file that merely *mentions* the syntax needs no
appeal: `conflictmarkers` anchors on column 0, so an indented example — the way every
doc here writes one — is already invisible.

Scope is every tracked text file, from `git ls-files`, not a directory list: the
2026-08-09 instance was in `Board/`, tonight's would have been in `.claude/memory/`,
and neither is tooling or source. Binary blobs are skipped by `conflictmarkers.scan`,
which cannot decode them.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nightshift import conflictmarkers
from nightshift.gates.base import Violation

NAME = "conflict_markers"
FAST = True
DESCRIPTION = "no tracked text file carries a git conflict marker"

#: A file-scope appeal. `appeal_markers.MIN_REASON` is mirrored here as a length
#: floor rather than imported, because that module tokenizes Python and this gate
#: reads markdown and plain text too — but the spelling is deliberately identical,
#: so `gate_appeals` validates the ones that do live in Python files.
_APPEAL = re.compile(rf"gate-ok\({NAME}\):\s*(?P<reason>\S.*)")
_MIN_REASON = 12


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
        appeal = _APPEAL.search(text)
        if appeal and len(appeal.group("reason").strip()) >= _MIN_REASON:
            continue
        for number, line in conflictmarkers.markers_in(text):
            violations.append(Violation(
                rel, number,
                f"git conflict marker left behind — `{line.strip()[:80]}`. A rebase or "
                f"merge was resolved without removing every marker; git does not check "
                f"this and neither did anything else until this gate. A file that must "
                f"genuinely carry one (a test, a doc quoting the raw shape) appeals with "
                f"a `gate-ok({NAME}): <reason>` line anywhere in it — file-scope, since "
                f"the markers sit inside string literals a comment cannot reach",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
