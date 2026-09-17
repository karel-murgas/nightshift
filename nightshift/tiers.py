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

**Step-4 note (07_portability.md), now done.** §1 measured this module as
project-free, and by the constant-counting measure it was. But the path pointed at
a plan doc, and §7 says the plan docs deliberately do *not* port — "a colleague
gets the README; the reasoning stays here". So a second project installing this
package had no `00_architecture.md`, and `binding()` raised `TierError` naming a
file that was never going to be there. Right *direction* — §16's rule is that a
dispatcher must never guess a model — wrong message.

Found by running it (2026-08-02): a synthetic fresh repo with a valid manifest got
`refusing to run — .claude/plans/ai_team/00_architecture.md is missing`, which
tells a new maintainer nothing they can act on. So the path is now
`[tiers].binding_doc`, defaulting to this project's, and the error names the key.
The binding itself stays in the document: a table of models in the manifest would
be the second home §16 exists to forbid.
"""
from __future__ import annotations

import re
from pathlib import Path

from nightshift import manifest as _manifest

# The default, and the fallback when there is no manifest to read. Kept as a
# module constant so the no-config behaviour is identical to what it replaced —
# 07_portability.md §8 step 4's first checklist item.
ARCHITECTURE = Path(_manifest.Tiers.binding_doc)


def binding_doc(repo_root: Path) -> Path:
    """The repo-relative document carrying the ```tier-binding block."""
    try:
        return Path(_manifest.load(repo_root).tiers.binding_doc)
    except _manifest.ManifestError:
        return ARCHITECTURE

# Public for the same reason as `line_endings.REQUIRED_ATTRIBUTE`: `discover` looks for
# this fence and `init` writes it, so the literal gets one owner rather than three.
FENCE = "tier-binding"
_BLOCK = re.compile(rf"^```{FENCE}[ \t]*\r?\n(.*?)^```", re.MULTILINE | re.DOTALL)
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
    rel = binding_doc(repo_root)
    doc = repo_root / rel
    if not doc.is_file():
        raise TierError(
            f"{rel.as_posix()} is missing — the tier binding lives there. "
            "Point `[tiers].binding_doc` in .ai/manifest.toml at a document of "
            "yours carrying a ```tier-binding block, one `<tier> = <model alias>` "
            f"line per tier ({', '.join(KNOWN_TIERS)})"
        )
    block = _BLOCK.search(doc.read_text(encoding="utf-8"))
    if not block:
        raise TierError(
            f"no ```tier-binding block in {rel.as_posix()} — "
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
            f"`tier: {tier}` is not bound in {binding_doc(repo_root).as_posix()} "
            f"(bound: {', '.join(sorted(table))})"
        )
    return table[tier]


#: The values `claude --effort` accepts, **in ascending order** — `effort_rank`
#: and every caller comparing two tiers depend on that order, not just on
#: membership. Read off `claude --help` rather than assumed: an earlier draft of
#: this tuple stopped at `high` and would have refused `xhigh` and `max`, which
#: are real levels, as typos.
#:
#: Validated here rather than left to the CLI because a typo in the manifest
#: surfaces as a dead worker process mid-night with the attempt already spent —
#: the failure is far from its cause and costs a card to diagnose.
KNOWN_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def effort(repo_root: Path, tier: str) -> str:
    """The `--effort` to dispatch `tier` with, or `""` to pass no flag at all.

    `resolve`'s sibling, and deliberately **not** read out of the same place.
    §16 says the tier→model binding lives in exactly one document and that no
    caller names a model; effort is not a model, so it is declared in
    `[tiers].effort` where a project's other dispatch facts already live, and
    §16's block is left with the one job the rule is about.

    **An undeclared tier returns `""`, which is not the same as a default.** It
    means *pass no flag*, so the stage inherits whatever the maintainer's own
    `~/.claude/settings.json` says — the behaviour of every stage before
    `token-economy.md` 3.3, kept as the answer for a project that declares
    nothing. `record_usage` writes that `""` through to the run record
    unchanged, so a run says "inherited" instead of claiming a value nobody
    passed (`token-economy.md` §5, defect 4a).

    Unlike `binding`, a missing manifest is not an error: there is nothing here
    a dispatcher could get *wrong* by having no answer, because having no answer
    is itself a supported answer. That asymmetry is the point — §16 forbids
    guessing a model precisely because a guessed model runs silently.
    """
    try:
        table = dict(_manifest.load(repo_root).tiers.effort)
    except _manifest.ManifestError:
        return ""
    value = table.get(tier, "")
    if value and value not in KNOWN_EFFORTS:
        raise TierError(
            f"`[tiers].effort.{tier} = \"{value}\"` is not one of "
            f"{', '.join(KNOWN_EFFORTS)} — a typo here would reach the CLI as a "
            "dead worker process with the attempt already spent"
        )
    return value


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    # Not `Path(__file__).parent.parent` any more: installed into site-packages
    # that answer is a directory inside the virtualenv, not the repo being asked
    # about. The root has to be found, and finding it must fail loudly rather than
    # fall back to the working directory.
    root = find_root()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for tier, model in sorted(binding(root).items()):
        how = effort(root, tier) or "inherited"
        print(f"{tier:8} → {model:12} @ {how}")
