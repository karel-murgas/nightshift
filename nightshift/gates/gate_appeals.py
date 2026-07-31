"""Gate: every gate appeal is well-formed, and the debt is counted.

This is the enforcement point for the appeal process in
`10_self_improvement.md` §4. Without it, that process is a paragraph in a plan
doc describing how people ought to behave -- which is precisely the largest
cluster Session H found in the correction log: **a rule stated correctly in
prose with nothing running it** (`unenforced-rule`, 4 entries, and the shape
behind another 12 in `derived-not-verified`). Writing an appeal process and not
gating it would have been the same mistake in the same session that named it.

What it checks:

* every `# gate-ok(<gate>)` marker names a gate that actually exists — a
  typo'd or renamed gate name silently exempts nothing, which is the harmless
  direction, but it also means the marker's author believes they are covered
  when they are not, and that belief outlives the marker;
* the reason is present and >= `MIN_REASON` chars;
* the marker sits within a few lines of something, i.e. it is not orphaned at
  the end of a file after the code it excused was deleted.

What it does not check is whether the reason is *good*. That is judgment and it
is Karel's, at review time — which is the point of making the count visible.

**"A gate that actually exists" means core or project, both.** `known_gates`
reuses `nightshift.gates.run.discover`, the exact function that decides which
gates the runner drives — so an appeal naming a gate that just moved to core
(07_portability.md §8 step 3) keeps working the day it moves, with no
hand-maintained list to update here.
"""
from __future__ import annotations

from pathlib import Path

import appeal_markers
from nightshift.gates.base import Violation

NAME = "gate_appeals"
FAST = True
DESCRIPTION = "every `# gate-ok(...)` appeal names a real gate and carries a written reason"


def known_gates(repo_root: Path) -> set[str]:
    from nightshift.gates.run import discover

    return set(discover(repo_root))


def check(repo_root: Path) -> list[Violation]:
    gates = known_gates(repo_root)
    violations: list[Violation] = []
    for appeal in appeal_markers.scan(repo_root):
        if not appeal.gate:
            violations.append(Violation(
                appeal.file, appeal.line,
                "gate-ok marker names no gate — a blanket exemption is not an appeal, "
                "name the one gate being appealed",
            ))
        elif appeal.gate not in gates:
            violations.append(Violation(
                appeal.file, appeal.line,
                f"gate-ok names {appeal.gate!r}, which is not a gate anywhere in this "
                f"project (known: {', '.join(sorted(gates))})",
            ))
        if len(appeal.reason) < appeal_markers.MIN_REASON:
            violations.append(Violation(
                appeal.file, appeal.line,
                f"gate-ok reason is {len(appeal.reason)} chars, minimum "
                f"{appeal_markers.MIN_REASON} — say why the violation is not real",
            ))
    return violations


def debt(repo_root: Path) -> int:
    """The number Karel is meant to look at. Reported by the test suite."""
    return len(appeal_markers.scan(repo_root))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    print(f"appeal debt: {debt(find_root())} marker(s)")
    raise SystemExit(1 if found else 0)
