"""The dispatcher's `tier: → model` lookup — §16's third landing place.

`00_architecture.md` §16 says the tier table is **the only place the tier→model
binding may be written down**, and that no caller ever names a model. Both halves
of that are load-bearing, and together they rule out the obvious implementation:
a dict in this file would be a second place the binding lives, and it would go
stale the day Phase 5 rebinds the worker tier to a local runtime — which is
exactly the failure §16 was written after (a correct table nothing read).

So this module **parses §16** rather than restating it. The doc carries a fenced
```tier-binding block whose lines are `<tier> = <model alias>`; that block is the
operative form and the prose table above it is the reasoning. Session I edits the
block and nothing here changes.

No LLM (§12): it is a five-line parser over a markdown file.

**Step-4 note (07_portability.md).** §1 measured this module as project-free, and
by the constant-counting measure it is. But `ARCHITECTURE` points at a plan doc,
and §7 says the plan docs deliberately do *not* port — "a colleague gets the
README; the reasoning stays here". So a second project installing this package has
no `00_architecture.md`, and `binding()` raises `TierError` naming a file that was
never going to be there. That is the right *direction* (§16's rule is that a
dispatcher must never guess a model), but the wrong message. The fix belongs with
step 4: the path becomes a manifest field, defaulting to this one, and the error
says which manifest key to set. Recorded here rather than fixed now because step 2
is a move, not a redesign.
"""
from __future__ import annotations

import re
from pathlib import Path

ARCHITECTURE = Path(".claude/plans/ai_team/00_architecture.md")

_BLOCK = re.compile(r"^```tier-binding[ \t]*\r?\n(.*?)^```", re.MULTILINE | re.DOTALL)
_LINE = re.compile(r"^[ \t]*([a-z][a-z0-9_-]*)[ \t]*=[ \t]*(\S+)[ \t]*$", re.MULTILINE)

# The tiers `card_schema` accepts. Kept here so a binding block that grows a
# third tier without the gate learning about it is a loud error rather than a
# card that silently cannot be dispatched.
KNOWN_TIERS = ("worker", "lead")


class TierError(RuntimeError):
    """The binding could not be resolved. Never guessed around — a dispatcher
    that fell back to a default model would reintroduce the 2026-07-22 bug in a
    form nobody could see."""


def binding(repo_root: Path) -> dict[str, str]:
    """`{tier: model alias}` as declared in §16. Raises if the block is gone."""
    doc = repo_root / ARCHITECTURE
    if not doc.is_file():
        raise TierError(f"{ARCHITECTURE.as_posix()} is missing — the tier binding lives there")
    block = _BLOCK.search(doc.read_text(encoding="utf-8"))
    if not block:
        raise TierError(
            f"no ```tier-binding block in {ARCHITECTURE.as_posix()} §16 — "
            "that block is the only place the tier→model binding may live"
        )
    found = {m.group(1): m.group(2) for m in _LINE.finditer(block.group(1))}
    if not found:
        raise TierError("the ```tier-binding block is empty")
    missing = [t for t in KNOWN_TIERS if t not in found]
    if missing:
        raise TierError(
            f"the ```tier-binding block does not bind {', '.join(missing)} — "
            f"`card_schema` accepts {KNOWN_TIERS}, so every one of them must resolve"
        )
    return found


def resolve(repo_root: Path, tier: str) -> str:
    """The model alias to pass to `claude --model`, for one card's `tier:`."""
    table = binding(repo_root)
    if tier not in table:
        raise TierError(
            f"`tier: {tier}` is not bound in 00_architecture.md §16 "
            f"(bound: {', '.join(sorted(table))})"
        )
    return table[tier]


if __name__ == "__main__":
    import sys

    from aiteam.manifest import find_root

    # Not `Path(__file__).parent.parent` any more: installed into site-packages
    # that answer is a directory inside the virtualenv, not the repo being asked
    # about. The root has to be found, and finding it must fail loudly rather than
    # fall back to the working directory.
    root = find_root()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for tier, model in sorted(binding(root).items()):
        print(f"{tier:8} → {model}")
