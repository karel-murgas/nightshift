"""Board health: cards stuck *between* transitions, found by the runner and the panel.

`landing.land` makes a merge and its lane move one act, so the stalls below cannot
happen on any path that goes through it. They can still happen by hand — a `git merge`
typed in a terminal, a verdict written by a session resuming an interrupted review —
so this looks for them where someone will see it: the runner logs the findings when a
run starts, and the Command Center's System page lists them. It replaces the tree
gates `tasks_branch_already_merged` and `review_verdict_unread`, which re-derived the
same facts on every edit.

    python -m nightshift.boardhealth
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from nightshift import board, branches, gitpaths
from nightshift.manifest import ManifestError, find_root

RUNS = Path(".ai/runs")
#: `chores.BATCH_NAMESPACE`, not imported: this module is read by the panel on every
#: System page render and must not pull the chore batch in with it.
BATCH_NAMESPACE = "chores"

_ATTEMPT_RE = re.compile(r"attempt-(\d+)$")
_DECISIVE = ("ok", "needs_fix", "needs_decision")


@dataclass(frozen=True)
class Finding:
    card_id: str
    lane: str
    text: str

    def __str__(self) -> str:
        return f"{self.lane}/{self.card_id}: {self.text}"


def _branch_set(root: Path, *args: str) -> set[str] | None:
    result = gitpaths.git(root, "branch", "--format=%(refname:short)", *args)
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _read_json(path: Path) -> dict:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _latest_verdict(runs: Path, card_id: str) -> dict:
    """The verdict of the card's highest-numbered attempt — an older attempt's verdict
    is superseded the moment a retry starts."""
    best, best_dir = -1, None
    for attempt in (runs / card_id).glob("attempt-*"):
        match = _ATTEMPT_RE.search(attempt.name)
        if match and int(match.group(1)) > best:
            best, best_dir = int(match.group(1)), attempt
    return _read_json(best_dir / "review-verdict.json") if best_dir else {}


def merged_but_not_landed(root: Path, integration: str) -> list[Finding]:
    """An inline `tasks/` card whose own branch is already an ancestor of the
    integration branch: merged by hand, lane never moved
    (`inline-session-stopped-before-its-own-closing-checklist`). Inline only — a
    dispatched card's retry can cut a branch that never diverged."""
    merged = _branch_set(root, "--merged", integration)
    if merged is None:
        return []
    out = []
    for card in board.cards(root, "tasks"):
        if card.kind != board.KIND_INLINE:
            continue
        name = branches.work_branch(card.id, card.fields.get("branch", ""))
        if name != integration and name in merged:
            out.append(Finding(card.id, "tasks", (
                f"`{name}` is already merged into `{integration}` but the card never "
                f"moved — finish it with `python -m nightshift.boardcmd land {card.id}`")))
    return out


def verdict_unread(root: Path, integration: str | None) -> list[Finding]:
    """A `review/` card whose own reviewer verdict already decided it
    (`verdict-written-where-nothing-reads-it`): the card's latest single-card verdict,
    or a batch verdict whose `chores/<batch>` branch still exists unmerged."""
    review = {c.id: c for c in board.cards(root, "review")}
    if not review:
        return []
    runs = root / RUNS
    out = []
    for card_id in review:
        called = str(_latest_verdict(runs, card_id).get("verdict", "")).lower()
        if called in _DECISIVE:
            out.append(Finding(card_id, "review", (
                f"its reviewer already wrote `{called}` to .ai/runs/{card_id}/ and nothing "
                f"acted on it — route it by hand")))
    chores_dir = runs / "_chores"
    if integration is None or not chores_dir.is_dir():
        return out
    existing = _branch_set(root)
    merged = _branch_set(root, "--merged", integration)
    if existing is None or merged is None:
        return out
    for batch_dir in sorted(chores_dir.iterdir()):
        batch = f"{BATCH_NAMESPACE}/{batch_dir.name}"
        if batch not in existing or batch in merged:
            continue  # landed, or pruned after landing
        items = _read_json(batch_dir / "review-verdict.json").get("items")
        for entry in items if isinstance(items, list) else []:
            if not isinstance(entry, dict):
                continue
            card_id = str(entry.get("id", "")).strip()
            called = str(entry.get("verdict", "")).lower()
            if card_id in review and called in _DECISIVE:
                out.append(Finding(card_id, "review", (
                    f"the batch review on `{batch}` wrote `{called}` for it, but `{batch}` "
                    f"is still unmerged — land or route it by hand, then merge or delete "
                    f"`{batch}`")))
    return out


def check(root: Path) -> list[Finding]:
    """Every finding, or `[]`. Never raises on an unconfigured project."""
    try:
        integration: str | None = branches.integration(root)
    except ManifestError:
        integration = None
    found = merged_but_not_landed(root, integration) if integration else []
    found += verdict_unread(root, integration)
    return sorted(found, key=lambda f: (f.lane, f.card_id))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    found = check(find_root())
    for finding in found:
        print(finding)
    if not found:
        print("board health: nothing stuck between lanes")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
