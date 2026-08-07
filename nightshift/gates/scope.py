"""Which directories hold *tooling* — the scope the discipline gates ask about.

`subprocess_result_checked`, `subprocess_encoding`, `write_newline`,
`pytest_invocation`, `run_stop_recorded` and `prompt_not_in_argv` all enforce
rules about the code that *runs* the machinery, not about the code under test.
Every one of them was earned by an incident in the orchestrator, and the first
five spelled that scope `Path(".ai")`, because when they were written the
orchestrator lived there.

**It does not any more, and nothing noticed.** 07_portability.md §8 moved ~16,800
lines into this package; the gates stayed pointed at the directory those lines had
left. Measured 2026-08-02: in Dungeoneer the four AST gates covered 19 small files
instead of the 10,500-line orchestrator that earned them, in a consuming project
they covered that project's own gates only, and in this package's own checkout they
covered nothing at all. `run_stop_recorded` was worse — its target was
`.ai/runner.py`, a path that had not existed since step 4, so `if not
path.is_file(): return []` made it green while reading zero bytes, in every repo,
for as long as it had shipped.

That is the log's `check-stopped-checking` class, and the fix has to be a scope
that *follows the code* rather than a second literal to keep true:

* **`.ai/`** — always, in every repo. A project's own gates, adapters and helper
  scripts are tooling by definition, and this is the half that was never wrong.
* **`[project].tooling_dirs`** — declared, and empty for almost everybody. A
  consuming project writes no framework code, so it declares nothing and behaves
  exactly as before. This package's own manifest declares `["nightshift"]`, which
  is what finally puts the orchestrator back inside the rules it wrote for itself.

Why a declaration and not `source_dirs`: a game's rendering package is not tooling,
and pointing "no `subprocess` result may be discarded" at 41,800 lines of game code
would be a different rule adopted by accident. Tooling is a claim about what a
directory is *for*, which is the shape of thing the manifest exists to carry.
"""
from __future__ import annotations

from pathlib import Path

from nightshift.manifest import AI_DIR, ManifestError
from nightshift.manifest import load as _load

__all__ = ["tooling_dirs", "tooling_files"]


def tooling_dirs(repo_root: Path) -> tuple[Path, ...]:
    """Absolute directories holding tooling, in reporting order, existing only.

    `.ai/` first because it is true everywhere and needs no config; a repo with
    neither returns `()` and its callers report nothing, which is correct — there
    is no tooling here to have an opinion about.
    """
    out: list[Path] = []
    ai = repo_root / AI_DIR
    if ai.is_dir():
        out.append(ai)
    try:
        declared = _load(repo_root).project.tooling_dirs
    except ManifestError:
        declared = ()
    for rel in declared:
        path = repo_root / rel
        if path.is_dir() and path not in out:
            out.append(path)
    return tuple(out)


def tooling_files(repo_root: Path, *, exclude: str = "") -> list[Path]:
    """Every `.py` file under `tooling_dirs`, sorted, `__pycache__` dropped.

    `exclude` drops one file *by name* — `pytest_invocation`'s owner exemption,
    which has to survive the module living at `.ai/suite.py` in the tree that
    earned the rule and at `nightshift/suite.py` now.
    """
    found: list[Path] = []
    for base in tooling_dirs(repo_root):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if exclude and path.name == exclude:
                continue
            found.append(path)
    return found
