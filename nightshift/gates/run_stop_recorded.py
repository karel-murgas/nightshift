"""Gate: the runner never logs a stop reason without recording it.

**Earned by the bug it would have caught** (`00_architecture.md` §15). Since
2026-07-30 the morning digest reports *runs*, not board state, and it learns why a
run ended from `run_record`'s `stop_reason`. The dispatch loop in `runner.run()`
has nine `break` paths — max-cards, the deadline, the budget, the kill switch, a
broken gate harness, the session-limit ceiling, endless transient limits, a
non-reopening scope, and three failures in a row — and each one used to be a bare
`_log(f"stopping — …")`. The instrumentation added a `_stop()` helper that writes
to both the log and the record, and the first draft of it converted seven of the
nine.

That is a silent failure of exactly the kind this suite exists to close: a night
that ends for an unrecorded reason renders in the digest as a night that simply
ran out of cards. Nothing fails, no test breaks, and the report is confidently
wrong about the most important fact of the morning — which is the same shape as
the "0 failed · Nothing failed" bug the whole restructure was fixing.

The rule is one line: **in `.ai/runner.py`, "stopping — " belongs to `_stop()`.**
Call `_stop("<reason>")`, which logs *and* records; never `_log("stopping — …")`.

Scope and precision:

  * `.ai/runner.py` only. Every other module logs through its own helpers and has
    no run record to keep in sync.
  * Flagged on a **call to `_log`** whose first argument is a string starting with
    `stopping —`, whether plain or an f-string. Restricted to the call site rather
    than a text search because this gate's own prose and the docstrings that
    explain the convention both contain the literal.
  * `_stop`'s own body is exempt by name — it is the one place that is *supposed*
    to log that line, and it is what every other path is told to call. Found by
    running the gate: it flagged the helper on the first pass.
  * The em-dash is part of the marker, matching the runner's actual format. A
    reworded stop line without it is not what this gate is about; the risk it
    guards is a *copied* `break` path, and a copy carries the format with it.

Appeals: `# gate-ok(run_stop_recorded): <reason>` — see `appeal_markers.py` and
`10_self_improvement.md` §4.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates.base import Violation

NAME = "run_stop_recorded"
FAST = True
DESCRIPTION = "runner.py logs a stop reason only via _stop(), which also records it"

_TARGET = Path(".ai") / "runner.py"
# The helper that owns the format. A call to it is the fix, not the violation.
_HELPER = "_stop"
_LOGGER = "_log"
_MARKER = "stopping —"


def _first_arg_text(call: ast.Call) -> str:
    """The literal text a `_log(...)` call starts with, for a plain string or an
    f-string. "" when the argument is computed — a stop reason assembled at
    runtime is not the copy-paste hazard this watches for, and guessing at its
    value would be the kind of false positive that gets a gate muted."""
    if not call.args:
        return ""
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        head = arg.values[0] if arg.values else None
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return ""


def _helper_lines(tree: ast.AST) -> set[int]:
    """Every line belonging to a `def _stop(...)`.

    The helper is the one place that is meant to log the line — it is what all the
    other paths are told to call — so its body cannot be a violation of its own
    rule. Matched by function name rather than by line number so moving it does
    not silently re-arm the false positive.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _HELPER:
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            lines.update(range(node.lineno, end + 1))
    return lines


def check(repo_root: Path) -> list[Violation]:
    path = repo_root / _TARGET
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError):
        return []  # not this gate's job to report unparseable Python

    rel = _TARGET.as_posix()
    exempt_lines = _helper_lines(tree)
    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == _LOGGER):
            continue
        if not _first_arg_text(node).startswith(_MARKER):
            continue
        if node.lineno in exempt_lines:
            continue
        if appeal_markers.exempt(appeals, NAME, rel, node.lineno):
            continue
        violations.append(Violation(
            rel, node.lineno,
            f"run_stop_recorded: this logs a stop reason directly. Call "
            f"`{_HELPER}(\"<reason>\")` instead — it writes the same log line *and* "
            f"puts the reason in the run record, which is where the morning digest "
            f"reads why the run ended. A stop that only reaches the log renders as "
            f"a run that simply ran out of cards",
        ))
    return violations


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(find_root()):
        print(violation)
