"""Every gate and hook names its subject (`correction-loop-defaults`, 2026-09-23
framework review, part 2): a module-level `SUBJECT` -- a path, a module, or a field
name it guards (a tuple if several) -- so `deletion_sweep` can flag one whose subject
has since been deleted, instead of the gate quietly checking nothing forever.

Two families, two discovery paths, both already the single source of truth their own
runner uses -- so a gate or a fence added there needs no third place edited to be
covered:

* gates: `nightshift.gates.run.discover`, run against this package's own checkout
  (core gates only -- there is no `.ai/gates/` here to add a project's own).
* hooks: `nightshift.hooks.discover.discover`.
"""
from __future__ import annotations

from pathlib import Path

from nightshift.gates import run as gates_run
from nightshift.hooks import discover as hooks_discover

REPO_ROOT = Path(__file__).resolve().parent.parent


def _assert_subject(name: str, module: object) -> None:
    subject = getattr(module, "SUBJECT", None)
    assert subject is not None, f"{name} has no module-level SUBJECT"
    if isinstance(subject, tuple):
        assert subject, f"{name}.SUBJECT is an empty tuple"
        for item in subject:
            assert isinstance(item, str) and item, (
                f"{name}.SUBJECT contains a non-string or empty entry: {item!r}")
    else:
        assert isinstance(subject, str) and subject, (
            f"{name}.SUBJECT must be a non-empty string or tuple of strings, "
            f"got {subject!r}")


def test_every_core_gate_declares_a_subject():
    gates = gates_run.discover(REPO_ROOT)
    assert len(gates) >= 20, "sanity check: gate discovery found suspiciously few gates"
    for name, module in gates.items():
        _assert_subject(name, module)


def test_every_hook_declares_a_subject():
    hooks = hooks_discover.discover()
    assert len(hooks) >= 8, "sanity check: hook discovery found suspiciously few hooks"
    for name, module in hooks.items():
        _assert_subject(name, module)
