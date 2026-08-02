"""Gate: prose naming the integration branch must agree with
`.ai/manifest.toml`'s `[branches].integration`. Dungeoneer audit row 22.

`nightshift.branches` was written to make a branch-role migration a one-line
edit: change `integration`, and `forbidden_bases()` and
`merge_base_candidates()` both follow. Its own docstring then concedes the
hole — a standing rule restated in prose elsewhere names the branch too, which
no import can reach. A one-line edit with a manual step attached is a two-line
edit that will be done once and forgotten.

It had already drifted in Dungeoneer, which is how this gate came to exist. On
2026-07-24 `CLAUDE.md` still read *"Active development branch: `dev` / Always
commit to `dev`"* — written before a branch-role migration, never revised.
`dev` had since become *stable*, so `forbidden_bases()` rejected it: the
highest-authority instruction file in the repo was telling every session to
commit to a branch the runner refuses to build on.

## What it checks

Every doc in `_DOCS` that names *any* branch holding a role — the integration
branch or a forbidden one — must name the right one. Concretely: a line
matching `_ROLE_LINE` (prose asserting "X is the branch you work on") must name
`[branches].integration`, and must not assert that a branch `forbidden_bases()`
rejects is the working one.

This is deliberately narrow. It does not police every mention of a retired
branch name — the docs discuss branch history, and a gate that fired on the
word would be muted within a week. It fires on the *claim*, which is the thing
that misleads.

`_DOCS` names Dungeoneer's own doc set (`CLAUDE.md` plus its session log) —
the one place this gate is not purely config-free (07_portability.md §8 D3):
a project without those exact files simply has nothing for it to check, which
is a no-op rather than a crash.
"""
from __future__ import annotations

import re
from pathlib import Path

from nightshift import branches
from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError

NAME = "branch_role_prose"
FAST = True
DESCRIPTION = "docs naming the integration branch must agree with .ai/manifest.toml [branches]"

_SOURCE = ".ai/manifest.toml [branches].integration"
_DOCS = (
    Path("CLAUDE.md"),
    Path(".claude") / "plans" / "ai_team" / "SESSIONS.md",
)

# Prose asserting a branch holds the working/integration role. The branch name
# is captured from the backticked or bolded token on the same line.
_ROLE_LINE = re.compile(
    r"(?:active\s+(?:development|integration)\s+branch"
    r"|always\s+commit\s+to"
    r"|integration\s+branch\s+is"
    r"|work\s+(?:on|off)\s+(?:a\s+branch\s+off\s+)?(?:the\s+)?)"
    r"[^\n]*?[`*]{1,2}(?P<branch>[\w./-]+)[`*]{1,2}",
    re.IGNORECASE,
)

# A line that points at the source of truth is always correct, whatever branch
# it names in passing — that is the pattern this gate wants docs to adopt.
#
# Must match the *config location*, not the word "integration": the first
# version of this was `branches\.py|INTEGRATION` case-insensitively, which
# exempted every line reading "Active integration branch: X" — precisely the
# lines it exists to check. Its own tests caught it, but only because they
# asserted a wrong branch is *rejected* rather than only that a right one is
# accepted.
#
# `branches.py` is deliberately NOT still accepted here. That file was deleted
# by 07_portability.md §8 step 4, and prose deferring to a file that no longer
# exists is not deference, it is a dangling pointer that happens to be exempt.
_DEFERS = re.compile(r"manifest\.toml|branches\.integration")


def check(repo_root: Path) -> list[Violation]:
    roles = _roles(repo_root)
    if roles is None:
        return []
    integration, forbidden = roles

    violations: list[Violation] = []
    for doc in _DOCS:
        path = repo_root / doc
        if not path.exists():
            continue
        rel = doc.as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DEFERS.search(line):
                continue
            m = _ROLE_LINE.search(line)
            if not m:
                continue
            named = m.group("branch")
            if named == integration:
                continue
            if named in forbidden:
                violations.append(Violation(
                    rel, lineno,
                    f"{NAME}: names `{named}` as the branch to work on, but "
                    f"`forbidden_bases()` rejects `{named}` as a base. The integration "
                    f"branch is `{integration}` ({_SOURCE})",
                ))
            else:
                violations.append(Violation(
                    rel, lineno,
                    f"{NAME}: names `{named}` as the integration branch, but "
                    f"{_SOURCE} is `{integration}`",
                ))
    return violations


def _roles(repo_root: Path) -> tuple[str, frozenset[str]] | None:
    """`(integration, forbidden_bases)` from the manifest, or `None`.

    `None` — the whole gate is a no-op — whenever the project has not declared
    `[branches].integration`. That is the same shape as the old "no
    `branches.py` here" answer and for the same reason: this gate checks prose
    against a declared fact, and with no fact declared there is nothing to
    check. It must not invent one, for `manifest.py`'s third-shape reason.
    """
    try:
        return branches.integration(repo_root), branches.forbidden_bases(repo_root)
    except ManifestError:
        return None
