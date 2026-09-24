"""Gate: every composed charter/skill is exactly its template plus the project addendum.

`nightshift.compose` says what a composed file must contain; this gate fails when the file
on disk is anything else — a hand edit to the generated file, or a template/addendum that
moved without `python -m nightshift.update --apply`. A repo with no manifest, or with no
composed file on disk, has nothing to check. (`framework-text-not-copied`)
"""
from __future__ import annotations

from pathlib import Path

from nightshift import compose
from nightshift.gates.base import Violation

SUBJECT = ".claude/agents, .claude/skills"

NAME = "composed_text"
DESCRIPTION = ("each composed charter/skill equals the framework template plus the "
               "project's addendum")


def check(repo_root: Path) -> list[Violation]:
    from nightshift import init, update  # heavy; only when a composed file exists
    if not any((repo_root / rel).is_file() for rel in compose.COMPOSED):
        return []
    try:
        tables = update.manifest_tables(repo_root)
    except update.UpdateError:
        return []
    plan = init.Plan(root=repo_root, tables=tables)
    init.stage_templates(plan, repo_root, tables)
    return [
        Violation(rel, 1,
                  f"{NAME}: generated from the nightshift template + "
                  f"{compose.addendum_rel(rel)} and no longer matches — put project text "
                  f"in the addendum, then run `python -m nightshift.update --apply`")
        for rel in compose.stale(repo_root, plan.staged)
    ]
