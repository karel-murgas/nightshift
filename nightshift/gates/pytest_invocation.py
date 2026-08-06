"""Gate: no file under `.ai/` hand-rolls pytest's parallel flags — they belong to
`.ai/suite.py` alone.

**Earned by the bug it would have caught, not by being easy** (`00_architecture.md`
§15). Three places in this tree run pytest on the project's behalf: `runner.py`,
`merge_check.py` and `preflight.py`. When `-n auto --dist loadfile` was introduced
(2026-07-26, `runner-test-selection`) it was written as a literal tuple in
`runner.py`, and the consequences were exactly what a duplicated policy does:

* `preflight.py` — the one **mandatory** boundary, whose receipt gates
  `git push` / `git merge` / `gh pr create` — kept its own inline
  `[sys.executable, "-m", "pytest", "tests/", "-q"]` for a further day. It was
  simultaneously the slowest pytest invocation in the project and the only one
  judging by an exit code, so a 0-collected run read as a pass there long after
  `_check_junit` existed to stop precisely that.
* the flags were passed unconditionally while `requirements.txt` documents
  pytest-xdist as optional, so on a checkout without it every caller failed with
  `error: unrecognized arguments: -n` and no JUnit report at all
  (`xdist-assumed-not-probed`, 2026-07-27).

Both were fixed by making `suite.parallel_args()` the single source: it probes for
the plugin and degrades to serial, and every caller goes through it. This gate is
what stops the fourth caller from starting the cycle over — the rule is now stated
in `CLAUDE.md` ("never hand-roll a parallel pytest invocation in tooling, call
`suite.parallel_args()`"), and a rule stated in prose with no enforcement point is
the `unenforced-rule` class this suite exists to close.

**Why `--dist` and not `loadfile` alone.** `--dist load` is the *dangerous*
spelling — it splits a file across xdist workers and the game suite flakes under
it, which is the whole reason `loadfile` is mandatory. A gate that only banned the
correct value would have let the broken one through, so the flag name is what is
watched.

Scope and precision:

  * `.ai/**/*.py`, minus `suite.py`, which is where the tuple is supposed to live.
  * **only inside a list or tuple literal** — i.e. something being built into an
    argv. `runner.py`'s worker prompt contains the sentence "run `python -m pytest
    tests/ -n auto --dist loadfile` yourself", which is an instruction to a worker
    and not an invocation; a text search would flag it and a gate with a false
    positive on its own docs gets muted. Prose, comments and docstrings are
    invisible to an AST walk over literal sequences, which is the distinction the
    rule actually wants.

Appeals: `# gate-ok(pytest_invocation): <reason>` — see `appeal_markers.py` and
`10_self_improvement.md` §4.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates import corpus
from nightshift.gates import scope
from nightshift.gates.base import Violation

NAME = "pytest_invocation"
FAST = True
DESCRIPTION = "only suite.py may spell pytest's parallel flags; callers use parallel_args()"

# Scope: `.ai/` plus `[project].tooling_dirs` — see `gates/scope.py`.
# The file that owns the policy. Excluded rather than special-cased in the walk so
# the exemption is one readable name.
_OWNER = "suite.py"
# Flags that mean "this code is building a parallel pytest argv". `-n` is xdist's
# worker count; `--dist` is its distribution mode, watched by flag name so the
# unsafe `load` value is caught alongside the required `loadfile`.
_WATCHED = frozenset({"-n", "--dist", "--numprocesses"})


def _files(repo_root: Path) -> list[Path]:
    return scope.tooling_files(repo_root, exclude=_OWNER)


def _offending_sequences(tree: ast.AST) -> list[tuple[int, str]]:
    """`(line, flag)` for every list/tuple literal holding a watched flag.

    Restricted to sequence literals on purpose — see the module docstring. A
    watched string anywhere else in the file (a docstring, a prompt, a log line)
    is prose about running pytest, not an invocation of it.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        for element in node.elts:
            if (isinstance(element, ast.Constant) and isinstance(element.value, str)
                    and element.value in _WATCHED):
                found.append((getattr(element, "lineno", node.lineno), element.value))
    return found


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []

    for path in _files(repo_root):
        tree = corpus.tree(path)
        if tree is None:
            continue  # not this gate's job to report unparseable Python
        rel = path.relative_to(repo_root).as_posix()
        for line, flag in _offending_sequences(tree):
            if appeal_markers.exempt(appeals, NAME, rel, line):
                continue
            violations.append(Violation(
                rel, line,
                f"pytest_invocation: `{flag}` is built into an argv here. The parallel "
                f"flags live in nightshift/suite.py — call `suite.parallel_args()` instead, which "
                f"probes for pytest-xdist and degrades to serial when it is missing",
            ))
    return violations


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(find_root()):
        print(violation)
