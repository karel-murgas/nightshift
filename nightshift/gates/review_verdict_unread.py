"""Gate: a card in `review/` whose own reviewer verdict already decided it.

`.ai/corrections.log`, `verdict-written-where-nothing-reads-it` (2026-09-02): asked to
continue a chore-batch review that a usage limit had killed mid-flight, a session
reviewed all six items and wrote a complete verdict to
`.ai/runs/_chores/20260902-0007/review-verdict.json` — and stopped there. Nothing moved:
the five `ok` items stayed in `review/`, the batch branch stayed unmerged, and the board
still told Karel the review had never reached a verdict. Karel: *"The OK cards are not
moved. Move them to propper lanes."*

The runner never leaves this gap on its own path: `review_stage` and `chores._land_the_
batch` each write the verdict file and act on it in the same call, so a verdict and its
card's lane move happen together or not at all. The gap only opens when a *human*
resumes an interrupted pipeline by hand and reproduces the verdict-writing step without
the caller that was supposed to read it back — exactly what the correction's own closing
line names: "a check could compare a run dir's `review-verdict.json` against the lanes
of the cards it names and flag any item whose verdict and lane disagree." This is that
check, narrowed to the one lane where the disagreement is unambiguous.

## Why `review/` and nothing else

`review/` means "a review is still owed and obtainable" (`one-lane-for-owed-and-
unobtainable`, `board.py`'s own `BLOCKED_LANE` docstring). A card sitting there whose
own most recent verdict already says `ok`, `needs_fix` or `needs_decision` is a
contradiction in terms — the review already happened, so nothing about "still owed" is
true. That is a strong enough signal on its own; modelling what lane each verdict value
*should* have produced (merge success, unadopted artefacts routing to a `pick`, a
batch's per-item escalation) would mean re-deriving `settle()` and `_route_flagged`
here, for cases this gate does not need to tell apart. It only needs to know the card is
stuck, not why.

## Two shapes, two staleness rules

A verdict file does not disappear once it is spent — nothing deletes `.ai/runs/`, and
retried cards accumulate `attempt-2/`, `attempt-3/`, … each with its own stale copy. So
"a verdict exists for this card" is not enough; it has to be the verdict that would
still govern this card's *current* sit in `review/`.

* **Single-card** (`.ai/runs/<id>/attempt-N/review-verdict.json`): only the
  highest-numbered `attempt-N/` on disk is read. A card's branch is reused across
  retries (`branches.work_branch`), so an older attempt's `needs_fix` sitting next to a
  fresh, not-yet-reviewed `attempt-N+1` must not be read as still open — the retry is
  what answered it.
* **Batch** (`.ai/runs/_chores/<batch>/review-verdict.json`): several cards share one
  branch (`chores/<batch>`), and unlike a card's own branch, that name is never reused
  — every batch gets a fresh timestamped branch and a fresh out_dir. So staleness here
  is not about attempt numbers; it is about whether the batch itself ever landed. A
  batch whose branch is already merged, or no longer exists at all (pruned after a
  normal `git branch -d`), already spent its verdict one way or another — only a batch
  branch that is still sitting there, unmerged, means the verdict it once produced was
  never acted on.

Both share the same race a card's own branch can never hit and a batch's barely can:
gates run standalone, and the runner's own automated path writes a verdict and moves
the lane inside one call, so the window in which this could catch a genuinely in-flight
review is not zero but is narrow — the same trade-off `tasks_branch_already_merged`
already accepts for its own git-state check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from nightshift import board, branches, gitpaths
from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError

NAME = "review_verdict_unread"
DESCRIPTION = "a review/ card whose own reviewer verdict already decided it is flagged"

RUNS = Path(".ai/runs")
#: `chores.BATCH_NAMESPACE` — not imported to avoid pulling `chores.py` (and
#: everything it imports) into every gate run; this is the one string the two
#: modules have to agree on, the same trade-off `review_lane_producer` makes for
#: matching `board.move(...)` by shape instead of by import.
BATCH_NAMESPACE = "chores"

_ATTEMPT_RE = re.compile(r"attempt-(\d+)$")
_DECISIVE = ("ok", "needs_fix", "needs_decision")


def _read_json(path: Path) -> dict:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _local_branches(repo_root: Path) -> set[str] | None:
    result = gitpaths.git(repo_root, "branch", "--format=%(refname:short)")
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _merged_branches(repo_root: Path, integration: str) -> set[str] | None:
    result = gitpaths.git(repo_root, "branch", "--format=%(refname:short)",
                          "--merged", integration)
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _latest_single_verdict(runs_dir: Path, card_id: str) -> dict:
    """This card's own `review-verdict.json`, from its highest-numbered
    `attempt-N/` — or `{}` if it never got one. A lower-numbered attempt's file
    is superseded the moment a retry starts, whatever it said."""
    card_dir = runs_dir / card_id
    if not card_dir.is_dir():
        return {}
    best = -1
    best_path: Path | None = None
    for attempt_dir in card_dir.glob("attempt-*"):
        match = _ATTEMPT_RE.search(attempt_dir.name)
        if not match:
            continue
        n = int(match.group(1))
        if n > best:
            best, best_path = n, attempt_dir
    if best_path is None:
        return {}
    return _read_json(best_path / "review-verdict.json")


def _batch_verdicts(runs_dir: Path) -> list[tuple[str, dict]]:
    """`(batch branch name, verdict)` for every batch review still on disk."""
    chores_dir = runs_dir / "_chores"
    if not chores_dir.is_dir():
        return []
    found = []
    for batch_dir in sorted(chores_dir.iterdir()):
        if not batch_dir.is_dir():
            continue
        verdict = _read_json(batch_dir / "review-verdict.json")
        if verdict:
            found.append((f"{BATCH_NAMESPACE}/{batch_dir.name}", verdict))
    return found


def check(repo_root: Path) -> list[Violation]:
    review_cards = {c.id: c for c in board.cards(repo_root, "review")}
    if not review_cards:
        return []

    runs_dir = repo_root / RUNS
    violations: list[Violation] = []

    for card_id, card in review_cards.items():
        verdict = _latest_single_verdict(runs_dir, card_id)
        called = str(verdict.get("verdict", "")).lower()
        if called not in _DECISIVE:
            continue
        rel = card.path.relative_to(repo_root).as_posix()
        violations.append(Violation(
            rel, 1,
            f"{NAME}: its reviewer already wrote `{called}` to "
            f".ai/runs/{card_id}/ but this card still sits in review/ — the "
            f"verdict was produced and nothing acted on it "
            f"(verdict-written-where-nothing-reads-it). Route it by hand: merge "
            f"and land it on `ok`, back to tasks/ with the finding on "
            f"`needs_fix`, or to needs-decision/ with the question on "
            f"`needs_decision`.",
        ))

    batches = _batch_verdicts(runs_dir)
    if batches:
        try:
            integration = branches.integration(repo_root)
        except ManifestError:
            integration = None
        if integration is not None:
            existing = _local_branches(repo_root)
            merged = _merged_branches(repo_root, integration)
            if existing is not None and merged is not None:
                for branch, verdict in batches:
                    if branch not in existing or branch in merged:
                        continue  # already landed, or pruned after landing
                    items = verdict.get("items")
                    if not isinstance(items, list):
                        continue
                    for entry in items:
                        if not isinstance(entry, dict):
                            continue
                        card = review_cards.get(str(entry.get("id", "")).strip())
                        if card is None:
                            continue
                        called = str(entry.get("verdict", "")).lower()
                        if called not in _DECISIVE:
                            continue
                        rel = card.path.relative_to(repo_root).as_posix()
                        violations.append(Violation(
                            rel, 1,
                            f"{NAME}: the batch review on `{branch}` already "
                            f"wrote `{called}` for this card, but `{branch}` is "
                            f"still unmerged and the card still sits in "
                            f"review/ — the verdict was produced and nothing "
                            f"acted on it (verdict-written-where-nothing-reads-"
                            f"it). Land or route it by hand, then merge or "
                            f"delete `{branch}`.",
                        ))

    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(find_root()):
        print(violation)
