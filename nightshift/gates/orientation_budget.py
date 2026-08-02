"""Gate: the orientation memory set stays under a declared byte budget.

Every project that keeps agent memory has the same failure: the files an agent
loads *at the start of every session* grow, nobody notices because each addition
is small and justified, and eventually orientation costs more than the work. This
is the cheapest possible check on that — `sum(sizes) <= budget` — and its whole
value is in where the number comes from.

## The budget is declared, and it is never the current size

`[memory].budget_bytes` has no default, and its absence means **this gate does not
run**. That is not an oversight to be tidied up later; it is the rule.

Dungeoneer, where the rule was earned, is the worked example. The correction log
proposed the gate at a ~60 KB target (`memory-orientation-cost`, 2026-07-22).
Measuring first found the set at 102.4 KB — red on arrival by 42 KB. There were two
ways forward and only one of them was honest: compress the tree, or pick a budget
that fits what is already there. The second is the move the gate exists to stop, so
the compression came first (`orientation-size-budget`, 2026-07-24), and the budget
was then read off the *compressed* tree plus headroom. A number chosen that way
means something; a number chosen to make a red gate green means nothing at all, and
is worse than no gate because it looks like one.

`nightshift init` therefore proposes this field **unset**, with that reasoning in
the message, and `discover.budget()` is deliberately written so that it cannot
compute a plausible `sum(sizes)` even by accident.

## Raising it is legitimate, and it is meant to be visible

If the set genuinely needs more room, the fix is a one-line manifest edit in a
commit — the gate-appeal path, at its cheapest level. What the gate prevents is the
budget moving *silently*, or moving as a side effect of a change whose subject was
something else.

Portable since 07_portability.md's §2 table classified it as a manifest-table gate
— pure logic over a project table. It stayed project-side one session too long, and
`[memory].orientation` sat in the manifest schema being read by nothing.
"""
from __future__ import annotations

from pathlib import Path

from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError
from nightshift.manifest import load as load_manifest

NAME = "orientation_budget"
FAST = True
DESCRIPTION = "the declared orientation files stay under [memory].budget_bytes"


def measure(repo_root: Path, files: tuple[str, ...]) -> tuple[int, list[str]]:
    """`(total_bytes, present_files)` over the declared set, missing files skipped.

    Missing rather than zero, and reported rather than assumed: a renamed
    orientation file should not quietly shrink the measurement to fit.
    """
    total = 0
    present: list[str] = []
    for rel in files:
        path = repo_root / rel
        if path.is_file():
            total += path.stat().st_size
            present.append(rel)
    return total, present


def check(repo_root: Path) -> list[Violation]:
    try:
        manifest = load_manifest(repo_root)
    except ManifestError:
        return []

    budget = manifest.memory.budget_bytes
    files = manifest.memory.orientation
    # No budget declared → the gate is off, by design (module docstring). No files
    # declared → there is nothing this project calls orientation, which is a real
    # answer and not a misconfiguration.
    if budget is None or not files:
        return []

    total, present = measure(repo_root, files)
    # None of the declared files present means this is not the tree the budget is
    # about — a bare fixture, a checkout mid-rename. Measuring nothing and reporting
    # a violation would fire on a repo the rule was never pointed at.
    if not present or total <= budget:
        return []

    over = total - budget
    return [Violation(
        present[0], 0,
        f"{NAME}: the orientation set ({', '.join(present)}) totals {total:,} B, over "
        f"the {budget:,} B budget by {over:,} B. These load at the start of every "
        f"session, so this is a cost paid on every task. Move dated narrative and "
        f"per-item detail into on-demand companion files rather than raising the "
        f"budget — raising `[memory].budget_bytes` to fit what is already there is "
        f"the move this gate exists to stop. If the growth is genuinely orientation, "
        f"raise it as a deliberate one-line edit in its own commit.",
    )]


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = find_root()
    found = check(root)
    for violation in found:
        print(str(violation))
    try:
        m = load_manifest(root)
        total, _ = measure(root, m.memory.orientation)
        cap = m.memory.budget_bytes
        print(f"orientation set: {total:,} B / "
              f"{f'{cap:,} B budget' if cap is not None else 'no budget declared (gate off)'}")
    except ManifestError as exc:
        print(exc)
    raise SystemExit(1 if found else 0)
