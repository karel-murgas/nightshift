"""Gate: a `done/` card that shipped a declared player-visible surface says so
on its own face — `verify: play` plus a `## How to test` section — the two
things a card actually gets by being played, rather than merely claimed.

`.ai/corrections.log`, `inline-route-assumed-karel-was-the-author` (2026-08-29):
`ingest._INLINE_CARD` hardcoded `verify: review` on every inline card, and
`verify: review` routes straight to `done/`, past `testing/` — the one lane
where the maintainer actually looks at what shipped. Three finished,
player-visible features (a new enemy mechanic with an overlay and a reaction
window, a defeat-screen stat, a skill rename plus an economy flip) reached the
archive unplayed before he caught one by accident: "There was a chore 'alarm
ICE', we might have finished that inline... I think the card went straight to
done instead of to the testing." The generation of `verify` was fixed
(`card_inline` now derives it from the classifier's own `nightshift` flag), but
nothing checks the *claim* a card in `done/` is actually making — a card whose
diff touches a declared player-visible path and carries `verify: review` (or no
`verify:` at all) with no `## How to test` section is the same shape the bug
produced, however it got there: a wrong triage judgment, a hand-edited field, a
future regression in whatever derives it next.

**Deliberately not git archaeology for the "was it played" half.** The
obvious-looking check — did this card's file ever physically sit under
`Board/testing/<id>.md` — was tried first and rejected: a card that failed it
is not stuck, because the actual fix Karel used twice already
(`self-kill-stats`, `clean-getaway-rework`, both 2026-08-29) never re-ran it
through `testing/` — it corrected `verify:` to `play` and wrote `## How to
test` from the summary, in place, in `done/`. A gate keyed on lane history
would have kept failing both after the maintainer had done exactly the right
thing, which is `orientation-shape`'s first failure mode (`turn-a-correction-
into-a-gate.md` §1): fires on a proxy for the claim instead of the claim
itself, gets appealed, gets muted. `verify:` + `## How to test` **is** the
claim card_schema already requires while a card sits in `testing/`
(`card_schema.py` line ~538); this gate is that same requirement, read one lane
later, for a card that claims to have skipped the trip.

**The "did it touch a player-visible path" half needs the diff, and the diff
is not always recoverable.** A card's branch is deleted once merged
(CLAUDE.md's git workflow), so the only trace left is the merge commit itself,
matched by message (`merge ai/<id>` — case-insensitive: the runner, an inline
session and the review-stage each spell the rest of that line differently,
but all three open the same way). Surveyed against Project Tigress's real
`Board/done/` (192 cards, 2026-09-19): a matching commit exists for roughly
15% of the *whole* corpus, because most of it predates this naming convention
or landed by squash/fast-forward with no per-card commit at all — but it is
found for essentially every card an interactive session or the runner has
merged under the current workflow, which is what matters for not recurring.
Narrowing the check to cards that already fail the in-artifact half first
(the large majority of `done/` is `verify: play` or already carries `## How to
test`, and needs no diff at all) keeps the archaeology to the minority where it
is actually decisive; where no landing commit can be found, the gate has no
evidence and says nothing, rather than guessing — the same "nothing to
review" silence `runner.branch_has_commits` already uses for an art-only card,
a branch never cut, or one already merged.

**Corpus, counted (2026-09-19, `player_visible_paths` seeded with
`project_tigress/rendering`, `project_tigress/core/i18n.py`,
`project_tigress/rendering/ui/help_catalog.py`):** 192 cards in `done/`, 94
carry `verify: review` (or no `verify:`) with no `## How to test`; of those, a
landing commit is found for 40. Zero touch a declared path today — not because
the corpus is clean (it is not: `alarm-ice` is exactly this shape, reopened by
hand in the same change that adds this gate, see its `## Thread`) but because
its own landing commit predates the `dungeoneer` → `project_tigress` rename
and touches `dungeoneer/rendering/ui/help_catalog.py`, a path this project's
declared prefixes no longer name. A gate proven only against its own author's
fixtures is unproven (`turn-a-correction-into-a-gate.md` §4); this one is
proven against 94 real candidates and rejects none of them under the paths as
declared today, which is the honest number to ship with rather than a
retrofitted legacy prefix chosen to make the count come out to one.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from nightshift import board
from nightshift import gitpaths
from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError
from nightshift.manifest import load as load_manifest

NAME = "player_visible_skipped_testing"
FAST = True
DESCRIPTION = "a done/ card touching a declared player-visible path carries verify: play and ## How to test"

_HOW_TO_TEST = re.compile(r"^##\s+how to test\s*$", re.MULTILINE | re.IGNORECASE)
_LANDING_LIMIT = 8  # merge commits to inspect per card; a card is merged once in the ordinary case


def _git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True,
                         text=True, timeout=20, encoding="utf-8", errors="replace")
    return out.stdout if out.returncode == 0 else ""


def _landing_commits(repo_root: Path, card_id: str) -> list[str]:
    """Commit hashes whose message opens `merge ai/<id>` (case-insensitive) —
    the runner's `Merge ai/<id> into <base>`, an inline session's `Merge
    ai/<id>: <summary>`, and `review_stage`'s `merge ai/<id>: reviewed ok by
    the runner` all match. Most recent first; capped, since one card is merged
    once in the ordinary case and a reopened card's replay is what would ever
    produce more than a couple."""
    pattern = rf"^merge ai/{re.escape(card_id)}\b"
    out = _git(repo_root, "log", "--pretty=%H", "-i", "-E", "--grep", pattern)
    return [line.strip() for line in out.splitlines() if line.strip()][:_LANDING_LIMIT]


def _touched_player_visible_path(repo_root: Path, commit: str, prefixes: tuple[str, ...]) -> str | None:
    """The first changed path under a declared prefix, or None. `commit^` as the
    base reads as "this commit's own contribution" whether `commit` is a real
    merge (diff against its first parent) or an ordinary single-parent commit —
    one expression covers both shapes the landing commit can take.

    `gitpaths.changed` (not a bare `git diff --name-only`) — a card-branch
    filename is exactly the free-text, human-typed shape `git_path_lists` exists
    to guard: quoted/escaped on a non-ASCII name, whitespace-split into two on a
    space, same as any other board or asset path in this package.
    """
    for changed in gitpaths.changed(repo_root, f"{commit}^", commit):
        if any(changed == prefix or changed.startswith(prefix.rstrip("/") + "/")
              for prefix in prefixes):
            return changed
    return None


def check(repo_root: Path) -> list[Violation]:
    try:
        manifest = load_manifest(repo_root)
    except ManifestError:
        return []
    prefixes = manifest.board.player_visible_paths
    if not prefixes:
        return []  # nothing declared — this project has no opinion yet

    violations: list[Violation] = []
    for card in board.cards(repo_root, "done"):
        if card.fields.get("verify") == "play":
            continue  # claims to have been played — its own claim, checked by card_schema
        if _HOW_TO_TEST.search(card.text):
            continue  # carries the scenario a played card is required to carry
        rel = card.path.relative_to(repo_root).as_posix()
        for commit in _landing_commits(repo_root, card.id):
            hit = _touched_player_visible_path(repo_root, commit, prefixes)
            if hit is None:
                continue
            violations.append(Violation(
                rel, 1,
                f"{NAME}: landing commit {commit[:8]} touches `{hit}` — a declared "
                f"player-visible path — but this card carries `verify: "
                f"{card.fields.get('verify', '(unset)')}` and no `## How to test` "
                f"section, so nothing here says the maintainer ever actually looked at "
                f"it. If it was actually played, correct `verify: play` and write "
                f"`## How to test` from the summary (manage-board's \"Closing out a card "
                f"you did yourself\"); if not, reopen it to `testing/` for a real look."
            ))
            break  # one violation per card is enough to act on
    return violations


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(find_root()):
        print(violation)
