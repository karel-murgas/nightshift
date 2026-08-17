"""Branch roles — the one place a branch *name* is bound to a *job*.

Moved out of Dungeoneer's `.ai/branches.py` by `07_portability.md` §8 step 4.
The reasoning below is that file's, because it is the reasoning that makes this
a module rather than a constant, and it is not project-specific: only the three
names were.

Written 2026-07-23 after Karel asked what happens when `development_team` is
retired and `dev` resumes its normal role. The answer, before that file, was
"the runner refuses to start": `FORBIDDEN_BASES` listed `dev`, and the day it
became the integration branch preflight would have rejected every dispatch
with "the runner never builds on dev".

The bug was a conflation. Two different facts were being expressed by one
frozenset:

- **`main`/`master` are stable.** Permanent, true on any day, true in any repo.
- **`dev` is not the integration branch.** True *this month*, because the
  AI-team work is deliberately quarantined on `development_team` until Karel
  has tested enough of it to trust it (see `00_architecture.md` §11).

The second is a fact about a migration in progress, and it was written down as
though it were the first. So the rule is derived rather than listed:
**everything stable is forbidden, except whichever branch currently holds the
integration role.** Retiring an integration branch becomes a one-line edit to
the manifest, and `dev` protects itself right up to the moment it stops
needing to.

## What moved, and what did not

The old module carried three constants: `INTEGRATION`, `STABLE` and `RETIRED`.
All three are now `[branches]` in `.ai/manifest.toml`:

    [branches]
    integration = "development_team"   # was INTEGRATION
    stable = "main"                    # was the permanent half of STABLE
    forbidden_extra = ["dev", "master"]

`RETIRED` is gone as a separate name. It existed for "a branch that used to
hold a role, must be kept (an open PR), and must still be refused as a base" —
which is `forbidden_extra`'s definition word for word, and `forbidden_bases()`
unioned the two anyway. Two names for one set, one of them permanently empty,
is the kind of duplication the manifest exists to end.

`master` is now declared rather than assumed. The old comment justified it as
"a clone or a fork may use it and the cost of listing it is nil" — true, but
that is a *guess about this repo* baked into shared code, and a project whose
`master` is its integration branch would have been silently refused. A repo
that wants the protection says so in one line.

## Why the manifest and not git config

`.ai/hosts.json` is keyed by hostname because the answer genuinely differs per
machine. The integration branch does not: it is a property of the project at a
point in time, identical on every clone, and it belongs in the commit that
changes it. Git config would make the switch invisible in `git log`, which is
the one place it should be loud — and the manifest is committed, so it keeps
that property.

## Why `integration` has no default

`manifest.py`'s third shape: a field that *bounds behaviour* is never guessed.
`forbidden_bases()` depends on it and a wrong answer means the runner builds on
a branch nobody wanted. Dungeoneer is the cautionary case — `dev` is *stable*
there while `development_team` carries the work, which no heuristic would get
right. So reading it goes through `require()`, which raises naming the manifest
key rather than returning something plausible.

`merge_base_candidates()` is the one function that degrades instead of raising,
and deliberately: its callers are gates asking "what changed on this branch",
and a gate must not crash in a repo that has not configured `[branches]` yet.
It falls back to the stable branch alone, which is the same last-resort answer
the old tuple's `"main"` element was.
"""
from __future__ import annotations

from pathlib import Path

from nightshift import manifest as _manifest
from nightshift.manifest import Manifest, ManifestError

__all__ = [
    "integration", "stable", "forbidden_bases", "merge_base_candidates",
    "is_forbidden_base", "WORK_PREFIX", "work_branch",
]

#: What a card's own branch is called. Not configurable, and named here because it
#: was spelled `f"ai/{card.id}"` inline at three places in `runner.py` and was about
#: to be spelled a fourth time by the panel's `Work on this` button — the
#: `category-of-one-read-as-a-literal` shape from the corrections log, one member
#: and therefore indistinguishable from a string until a second caller arrives.
WORK_PREFIX = "ai/"


def work_branch(card_id: str, recorded: str = "") -> str:
    """The branch a card's work belongs on: the one recorded on it, else `ai/<id>`.

    `branch:` is a **runner-written** field (`board.RUNNER_FIELDS`), stamped onto the
    card once the worktree is cut, so the two halves cannot disagree: the site that
    cuts the branch has nothing to read yet and computes the name, and every site
    afterwards reads back what was stamped. Pass what the card carries and let the
    fallback handle a card no attempt has reached — which is also the panel's case,
    since `Work on this` may be the first thing to touch a card.
    """
    return recorded or f"{WORK_PREFIX}{card_id}"

_WHY_INTEGRATION = (
    "it is the branch work is cut from and merged back into. Nothing can guess "
    "it: a wrong answer means the runner builds on a branch nobody chose"
)


def _load(where: Manifest | Path | None) -> Manifest:
    """Accept a `Manifest` or the repo root it came from.

    Both spellings are load-bearing. Gates and the runner hold a `root: Path`
    and would otherwise each grow their own `manifest.load` call; `preflight`
    and `merge_check` have already loaded one and must not re-read the file
    between two questions about the same run.
    """
    if isinstance(where, Manifest):
        return where
    return _manifest.load(where)


def integration(where: Manifest | Path | None = None) -> str:
    """The branch feature work is cut from and merged back into.

    Raises `ManifestError` when undeclared — see the module docstring.
    """
    return str(_manifest.require(_load(where), "branches.integration", _WHY_INTEGRATION))


def stable(where: Manifest | Path | None = None) -> str:
    """The production branch. Defaults to `main`."""
    return _load(where).branches.stable


def forbidden_bases(where: Manifest | Path | None = None) -> frozenset[str]:
    """Branches the runner refuses to build on or commit to.

    Derived, not listed: the integration branch is exempt precisely because
    holding that role is what makes it a legitimate base. When a project's
    `integration` becomes `dev`, `dev` leaves this set on the same one-line
    manifest edit that puts it to work — which is the whole point of the file.
    """
    m = _load(where)
    return frozenset({m.branches.stable, *m.branches.forbidden_extra}) - {integration(m)}


def merge_base_candidates(where: Manifest | Path | None = None) -> tuple[str, ...]:
    """Bases to diff a working branch against, in preference order.

    Gates that ask "what has changed on this branch" (`memory_freshness`,
    `deletion_sweep`) walk this in order and take the first that resolves. The
    stable-branch fallback matters for a fresh clone that has never fetched the
    integration branch; without it the merge-base lookup returns empty and the
    gate silently sees no diff at all — passing green on an unreviewed change,
    which is the failure direction that actually hurts.

    The only function here that tolerates an undeclared `integration`, because
    its callers are gates: see the module docstring.
    """
    try:
        m = _load(where)
    except ManifestError:
        return (_manifest.Branches().stable,)
    try:
        return (integration(m), m.branches.stable)
    except ManifestError:
        return (m.branches.stable,)


def is_forbidden_base(branch: str, where: Manifest | Path | None = None) -> bool:
    return branch in forbidden_bases(where)
