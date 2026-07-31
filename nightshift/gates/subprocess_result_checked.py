"""Gate: no `subprocess.run(...)` under `.ai/` throws its result away.

**Earned by an observed cluster, not by being easy** (`00_architecture.md` §15).
Session H's read of the correction log found `silent-noop` — *a mechanism ran,
did nothing, and reported success* — as a recurring class, and one of its
members is exactly this pattern:

> `commit_board` staged `Board/` and `Digest.md` in one `git add`. git fails the
> **whole** pathspec when any entry matches nothing, so in a tree without a
> `Digest.md` yet it staged nothing and the commit was a silent no-op.
> Consequence: `attempts` never reaches disk, so a card that crashes retries
> forever. — `git-add-pathspec-noop`, 2026-07-23

That bug was fixed at the call site by adding an existence check. It was never
fixed as a *class*: the return code of that `git add` is still discarded, and so
is every other one under `.ai/`. Any other reason for it to fail — an
`index.lock`, a permission error, a path outside the work tree — produces the
identical silent wasted night, and the fix that landed does not cover them.

The rule is therefore about the return value, not about git: **a subprocess
whose result is discarded cannot be distinguished from one that succeeded.**
Assigning the result is enough to satisfy the gate. That is deliberately weak —
it does not prove the code goes on to *look* at `returncode`, and no cheap AST
rule can. What it does buy is that discarding is never the silent default, which
is the shape the observed bug actually had.

Scope: `.ai/` only. That is where the orchestrator lives, where a silent failure
costs a night, and where §12 says the code must be dumb and predictable. Game
code under `dungeoneer/` does not shell out, and widening a gate past its
evidence is how gates acquire false positives and get muted.

Appeals: `# gate-ok(subprocess_result_checked): <reason>` — see
`appeal_markers.py` and `10_self_improvement.md` §4.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates.base import Violation

NAME = "subprocess_result_checked"
FAST = True
DESCRIPTION = "no subprocess call under .ai/ discards its result"

_ROOT = Path(".ai")
# Every subprocess entry point that returns something worth looking at.
# `check_call`/`check_output` raise on failure, so discarding those is safe and
# they are absent on purpose.
_WATCHED = frozenset({"run", "call", "Popen"})


def _is_subprocess_call(node: ast.AST) -> str | None:
    """Return the attribute name if `node` is `subprocess.<watched>(...)`."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _WATCHED:
        value = func.value
        if isinstance(value, ast.Name) and value.id == "subprocess":
            return func.attr
    return None


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []
    base = repo_root / _ROOT
    if not base.is_dir():
        return violations

    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(repo_root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            # An `ast.Expr` statement is a value computed and thrown away.
            # Anything else — assignment, `return`, a comparison, an argument —
            # keeps the result reachable, which is all this gate asks for.
            if not isinstance(node, ast.Expr):
                continue
            attr = _is_subprocess_call(node.value)
            if attr is None:
                continue
            if appeal_markers.exempt(appeals, NAME, rel, node.lineno):
                continue
            violations.append(Violation(
                rel, node.lineno,
                f"subprocess.{attr}(...) result discarded — a failed call is "
                f"indistinguishable from a successful one; assign it and check "
                f"returncode, or appeal with `# gate-ok({NAME}): <reason>`",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
