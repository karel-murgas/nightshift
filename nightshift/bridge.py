"""**Temporary.** Imports a module that still lives in the consuming project's
`.ai/`, for the window between step 2 and step 4 of `07_portability.md` §8.

Delete this file when step 4 lands. Nothing else in `nightshift` should ever grow a
call to it.

**Why it exists at all.** §1 measured coupling as *Dungeoneer-specific content* —
named constants at the top of files — and by that measure `preflight.py` and
`merge_check.py` scored zero, which is why §8 puts them in step 2. That measure
missed a second kind of coupling: **module dependency**. `preflight` reaches for
`suite` and `branches`; `merge_check` reaches for `board`, `branches`, `runner` and
`suite`. None of those are project *facts*, but all four are modules that do not
move until step 4, so for now they are on the far side of the boundary.

Two honest ways to handle that: hold the two modules back until step 4, or ship
them on a named seam. This is the seam. It is one file, it is greppable, and every
caller carries a comment saying which step deletes it — which is more than a
`sys.path` insert scattered across two modules would give. What it must not become
is a general "import anything from the project" facility: a core module that can
reach into a project at will is not portable, it merely compiles.

**`.ai` cannot be a package** — a leading dot is not a Python identifier, so
`import ai.suite` will never work. The modules there are already imported flat
after a `sys.path` insert, which is why this is a path insert rather than an import
rewrite, and why step 4 is a *move* rather than a re-parenting.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

from nightshift.manifest import AI_DIR, ManifestError

__all__ = ["project_module", "add_project_path"]


def add_project_path(root: Path) -> None:
    """Put `<root>/.ai` on `sys.path`, once."""
    ai = str((root / AI_DIR).resolve())
    if ai not in sys.path:
        sys.path.insert(0, ai)


def project_module(root: Path, name: str, needed_for: str) -> ModuleType:
    """Import `<root>/.ai/<name>.py` as a top-level module.

    `needed_for` is not decoration. When a colleague installs this package into a
    repo that has no `.ai/runner.py`, the bare ImportError says only that `runner`
    is missing, which reads as a broken install. Naming the caller and the step
    turns it into the true statement: this module is not portable yet.
    """
    add_project_path(root)
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ManifestError(
            f"{needed_for} needs `{AI_DIR}/{name}.py` in {root}, and it is not there. "
            f"That module has not been extracted yet (07_portability.md §8 step 4), so "
            f"this part of the framework still requires a project that carries it."
        ) from exc
