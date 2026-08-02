#!/usr/bin/env python3
"""Re-run the guideline-enforcement audit. Deterministic; no LLM
(`00_architecture.md` §12).

**Why this exists.** §14b ends with a meta-rule: *"the audit in
`01_audit_findings.md` must be re-runnable. It is the health metric for this
whole system (rules total, % enforced, charter length trend). If it can only be
produced by hand, self-maintenance is fiction."*

It could only be produced by hand, and it had already gone stale. Measured
2026-07-23 (Session H): the headline still read **"11 gate files, 20 of 57
rules enforced"** from Session A on 2026-07-21, while `.ai/gates/` held 19
gates — Sessions D, E and H having added eight between them without the
inventory moving. And `feedback_doc_leanness.md`, a rule Karel gave on
2026-07-22, had **no row in the matrix at all**.

That is the `rule-scout` job (§14) minus its judgment half. Deciding whether a
sentence is a new normative rule needs the matrix, the feedback files and enough
codebase to answer "is this checkable" — a briefing, so it stays with the lead
(`10_self_improvement.md` §6). Counting what is already written down does not,
and §12 says what Python can do, Python should do.

What it reports:

* the headline counts, recomputed from the table rather than remembered;
* **gates that exist but no row cites** — the drift that made this necessary;
* **rows citing a gate file that is gone** — the mirror failure;
* **`feedback_*.md` files with no row anywhere** — a rule that never entered
  the inventory, which is how `doc_leanness` was found.

**The machinery ports; the matrix does not** (07_portability.md §7, §8 step 4).
Counting rows, spotting an uncited gate and spotting a cited-but-absent one is
the same job in any repo. The 55-row matrix and the `feedback_*.md` files it
indexes are *this* project's earned evidence, and shipping them as another
project's rules is the thing §7 forbids. So a project declares where its matrix
lives — `[audit].matrix` — and gets nothing if it has none.

Usage:
    python -m nightshift.audit            # the report
    python -m nightshift.audit --check    # exit 1 if anything drifted (for the gate)
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from nightshift import manifest as _manifest
from nightshift.manifest import AI_DIR, ManifestError, find_root

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

GATES_DIR = Path(AI_DIR) / "gates"
FEEDBACK_DIR = Path(".claude/memory")

# Modules under .ai/gates/ that are machinery, not gates. Mirrors run.py's own
# skip list plus the shared helpers; kept here rather than imported because a
# gate must stay independently runnable with no siblings on the path.
_NOT_GATES = frozenset({"run", "base", "doc_scan"})

def matrix_path(repo_root: Path) -> Path | None:
    """The rule matrix this project keeps, or `None` if it keeps none.

    `[audit].matrix`. `None` is a supported answer, not a misconfiguration: a
    fresh repo starts with an *empty* audit matrix (§7), and a report about a
    document that does not exist is noise.
    """
    try:
        declared = _manifest.load(repo_root).audit.matrix
    except ManifestError:
        return None
    return repo_root / declared if declared else None


def project_infra_gates(repo_root: Path) -> dict[str, str]:
    """Gates that stay project-side but enforce framework machinery rather than
    a rule about the project's own product — the entries `core_gate_names`
    cannot find automatically, because they have not moved into the package.

    `[audit].infra_gates`, each with a written reason: the same bargain as a
    `# gate-ok(...)` appeal — an exemption you can read. A matrix scoped to
    "the rules the maintainer wrote about making the product" should not grow
    an invented row for a gate about the harness.
    """
    try:
        return dict(_manifest.load(repo_root).audit.infra_gates)
    except ManifestError:
        return {}


def core_gate_names(repo_root: Path) -> frozenset[str]:
    """Gates the `nightshift` package ships, found the same way
    `nightshift.gates.run` finds them — not a hand-maintained list, so a gate
    that moves to core (07_portability.md §8) drops out of §A's scope the day
    it moves, with no edit needed here. `gate_files(repo_root)` already unions
    core and project gates; subtracting this project's own directory leaves
    exactly the core set."""
    from nightshift.gates.run import discover

    return frozenset(discover(repo_root)) - local_gate_files(repo_root)

# `| 5 ★ | rule text | source | enforcement | checkability |`
_ROW = re.compile(r"^\|\s*(\d+)\s*(★?)\s*\|(.+)$")
# A gate citation, project-side (`.ai/gates/name.py`) or core
# (`nightshift/gates/name.py`, or the dotted `nightshift.gates.name` form) — a
# row's enforcement can name either home (07_portability.md §8 step 3).
_GATE_REF = re.compile(r"(?:\.ai/gates/|nightshift/gates/|nightshift\.gates\.)(\w+?)(?:\.py)?(?=[`\s,.)]|$)")
# The rule matrix is §A. The doc has other numbered tables — §B's work-type
# list is also `| 1 | … |` — and the first version of this parser swallowed
# them, reporting 67 rules against the doc's own headline of 57. Caught by
# `verify-before-shipping-a-rule` step 4: report the count, including the one
# you did not expect.
_SECTION_A = re.compile(r"^##\s*A\.", re.MULTILINE)
_NEXT_SECTION = re.compile(r"^##\s+(?!A\.)", re.MULTILINE)


@dataclass(frozen=True)
class Row:
    number: int
    starred: bool
    rule: str
    source: str
    enforcement: str
    checkability: str

    @property
    def state(self) -> str:
        """`test` / `partial` / `prose`, read off the enforcement cell."""
        cell = self.enforcement.lower()
        # Order matters: a partial row also contains the word "test".
        if "partial" in cell:
            return "partial"
        if "test" in cell:
            return "test"
        return "prose"

    @property
    def gates(self) -> set[str]:
        return set(_GATE_REF.findall(self.enforcement))


def _section_a(repo_root: Path) -> str:
    """Just the rule matrix — §A, up to the next top-level heading. Empty when
    the project declares no matrix, or the declared one is not there."""
    path = matrix_path(repo_root)
    if path is None or not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8")
    start = _SECTION_A.search(text)
    if not start:
        return ""
    rest = text[start.end():]
    end = _NEXT_SECTION.search(rest)
    return rest[: end.start()] if end else rest


def rows(repo_root: Path) -> list[Row]:
    found: list[Row] = []
    for line in _section_a(repo_root).splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        cells = [c.strip() for c in match.group(3).split("|")]
        if len(cells) < 4:
            continue
        found.append(Row(int(match.group(1)), bool(match.group(2)),
                         cells[0], cells[1], cells[2], cells[3]))
    return found


def local_gate_files(repo_root: Path) -> set[str]:
    """Gates in this project's own `.ai/gates/` — unchanged meaning."""
    return {
        path.stem
        for path in (repo_root / GATES_DIR).glob("*.py")
        if path.stem not in _NOT_GATES
    }


def gate_files(repo_root: Path) -> set[str]:
    """Every gate name §A may legitimately cite: this project's own gates, plus
    every gate the `nightshift` core package ships. A Dungeoneer rule can be
    enforced by a shared core gate — row 22 (branch_role_prose) is exactly
    this — so a project rule citing a gate that moved to core
    (07_portability.md §8) must not read as drift merely because the file now
    lives in the installed package rather than this checkout."""
    from nightshift.gates.run import discover

    return set(discover(repo_root))


def feedback_files(repo_root: Path) -> set[str]:
    return {p.stem for p in (repo_root / FEEDBACK_DIR).glob("feedback_*.md")}


def drift(repo_root: Path) -> dict[str, list[str]]:
    """Everything the matrix and the tree disagree about."""
    table = rows(repo_root)
    cited = {gate for row in table for gate in row.gates}
    present = gate_files(repo_root)
    # Scoped to §A, not the whole document. This searched the full text until
    # 2026-07-24 and so counted a file as inventoried on *any* mention — which
    # is how `feedback_assets.md` passed for months on a mention in §F, a gate
    # *replay* table that is not the rule matrix. `rows()` above was already
    # careful to scope itself; this check simply never got the same treatment,
    # making it a weaker instance of the drift this script exists to catch.
    text = _section_a(repo_root)
    infra = core_gate_names(repo_root) | set(project_infra_gates(repo_root))
    return {
        "gates_not_in_the_matrix": sorted(present - cited - infra),
        "rows_citing_a_missing_gate": sorted(cited - present),
        "feedback_files_with_no_row": sorted(
            name for name in feedback_files(repo_root) if name not in text
        ),
    }


def report(repo_root: Path | None = None) -> str:
    repo_root = repo_root or find_root()
    table = rows(repo_root)
    if not table:
        path = matrix_path(repo_root)
        return ("this project declares no `[audit].matrix` — nothing to count"
                if path is None else f"{path} has no §A rule matrix — nothing to count")
    counts = Counter(row.state for row in table)
    total = len(table)
    out = [
        f"{total} normative rules in the matrix",
        "",
        f"  enforced by a test   {counts['test']:>3}  ({100 * counts['test'] / total:.0f}%)",
        f"  partially enforced   {counts['partial']:>3}",
        f"  prose only           {counts['prose']:>3}  ({100 * counts['prose'] / total:.0f}%)",
        "",
        f"  gates in .ai/gates/  {len(local_gate_files(repo_root)):>3}",
        f"  gates from nightshift core {len(core_gate_names(repo_root)):>3}"
        f"   ({len(core_gate_names(repo_root)) + len(project_infra_gates(repo_root))} of them AI-team "
        f"infrastructure, out of §A's scope)",
        f"  gates cited by a row {len({g for r in table for g in r.gates}):>3}",
    ]
    starred_prose = [r.number for r in table if r.starred and r.state == "prose"]
    if starred_prose:
        out.append("")
        out.append(f"  ★ prose+easy rows still unbuilt ({len(starred_prose)}): "
                   f"{', '.join(str(n) for n in starred_prose)}")

    problems = drift(repo_root)
    if any(problems.values()):
        out.append("")
        out.append("DRIFT")
        out.append("-----")
        for label, items in problems.items():
            if items:
                out.append(f"  {label.replace('_', ' ')}: {', '.join(items)}")
    else:
        out.append("")
        out.append("no drift — every gate is cited and every feedback file has a row")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-run the enforcement audit (00_architecture.md §14b).")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the matrix and the tree disagree")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to audit (default: found from the working directory)")
    args = parser.parse_args(argv)
    root = (args.root or find_root()).resolve()
    print(report(root))
    if args.check and any(drift(root).values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
