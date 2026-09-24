"""Discovers every hook module this package wires into a consuming project's
`.claude/settings.json`, the way `nightshift.gates.run.discover` discovers gates --
so `test_gate_subjects.py` and `deletion_sweep` can each ask the same question of
both families: "does this module declare a SUBJECT?" (`correction-loop-defaults`,
2026-09-23 framework review, part 2).

A hook is not found the same way a gate is. `nightshift.gates.run.discover` can glob
a directory because every gate is `hasattr(module, "check")` and every `.py` file in
`.ai/gates/` is a gate candidate. `nightshift/hooks/` also holds pure helpers with no
guard of their own (`shellwords`, `session_memo`, `project_script`) that a glob would
wrongly count, so this instead names the two ways a module earns hook status:

* **A fence** -- `check(payload) -> str | None` -- is one of `nightshift.hooks.pre`'s
  own `RULES`, which is already the single source of truth `pre.py` dispatches from.
  A fence added there needs no second registration for this to see it.
* `preflight_guard`, `gates_on_edit`, `pre` and `post` are wired individually rather
  than through `RULES`: `preflight_guard` decides as well as denies and is spawned by
  its own `if` conditions; `gates_on_edit` is what `post.py` calls directly; the two
  dispatchers are named because each is itself a claim about what it dispatches for.
"""
from __future__ import annotations

import importlib

from nightshift.hooks import pre as _pre

#: Wired outside `pre.RULES` -- see the module docstring for why each one is here.
_INDIVIDUALLY_WIRED = ("preflight_guard", "gates_on_edit", "pre", "post")


def discover() -> dict[str, object]:
    """`{hook name: module}` for every fence in `pre.RULES` plus the individually
    wired hooks. Order is `RULES` order, then `_INDIVIDUALLY_WIRED` order."""
    names = [name for name, _ in _pre.RULES] + list(_INDIVIDUALLY_WIRED)
    return {name: importlib.import_module(f"nightshift.hooks.{name}") for name in names}
