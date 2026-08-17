"""Gate: no test builds a git repo from scratch where a shared template exists.

**Earned by a measurement, not by being easy** (`00_architecture.md` §15). On
2026-08-17 this package's own suite was spending most of its time reconstructing
the world once per assertion. `tests/test_update.py` ran 23 tests in 13.3 s and
every one of the eight slowest entries pytest reported was `setup`, never a test
body. Per fixture: `git init` plus three `config` cost 135 ms, `add` plus
`commit` 85 ms, `init.apply(build_plan)` 247 ms — 467 ms, against 68 ms to copy
the finished tree. Across the suite that was 32 `git init` sites in 22 files.

The fix was `tests/_fixtures.py`: build once per process, hand every test a copy.
The whole suite went from 1799 tests in 1018 s to 1805 in 272 s.

**Nothing about that fix defends itself.** A new test file written from memory
will spell `git init` plus three `config` calls, pass every other gate, pass
review, and be quietly slow — which is exactly how it reached 32 sites the first
time. No single one of them was a mistake; the cost only exists in aggregate, and
aggregate cost is invisible in a diff. That is the `unenforced-rule` class, and a
rule this suite paid 750 seconds to learn is worth an enforcement point.

**Self-activating, so it ships harmlessly.** This is a core gate and therefore
runs in every consuming project, most of which have no such convention and no
`_fixtures.py`. So the gate looks for the template module first and reports
nothing when there is none: it enforces the convention only where the convention
exists, rather than imposing one on a project that never adopted it. A project
that later grows a `tests/_fixtures.py` with a `git_init` gets the enforcement
the moment it does, without editing a manifest.

What is watched, and why these two shapes:

  * a list or tuple literal holding both `"git"` and `"init"` — a `subprocess`
    argv being built by hand.
  * a call to something whose name contains `git`, passing a literal `"init"` —
    the `_git(repo, "init", "-q")` helper convention this suite uses everywhere.

Restricted to those two, and to literals, for `pytest_invocation`'s reason: a
docstring that mentions `git init` is prose about the rule, not a breach of it,
and a gate with false positives on its own documentation gets muted.

Appeals: `# gate-ok(fixture_rebuild): <reason>` — see `appeal_markers.py` and
`10_self_improvement.md` §4. Appeals here are expected rather than rare, and the
existing ones say what a genuine one looks like: a repo that cannot be copied
because something in it records an absolute path. `test_freshness.py` clones a
real bare origin, whose `.git/config` carries the origin's path, so every copy
would point back at one shared remote. A cut worktree records its gitdir the same
way. Those must be built, and saying so at the call site is the point — the split
between what can be copied and what cannot is the one thing about this change
that had to be established rather than assumed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates import corpus
from nightshift.gates.base import Violation

NAME = "fixture_rebuild"
FAST = True
DESCRIPTION = "tests copy the shared fixture template instead of running `git init` themselves"

#: The module that owns the policy, and the only file allowed to spell `git init`.
#: Excluded by name rather than special-cased in the walk, as `pytest_invocation`
#: excludes `suite.py`, so the exemption stays one readable line.
_OWNER = "_fixtures.py"
#: What `_OWNER` has to provide before the gate has anything to recommend. A
#: module of helpers that does not include this one is not the convention.
_REQUIRED = "git_init"


def _template_module(repo_root: Path) -> Path | None:
    """`tests/_fixtures.py`, if it exists and offers `git_init`.

    The manifest names the test directory; a project that declares none, or whose
    manifest does not parse, gets `None` and a silent gate. Fewer files and never
    a crash is the safe direction for a scan — the same fallback
    `appeal_markers._default_roots` takes, for the same reason.
    """
    try:
        from nightshift.manifest import load as load_manifest

        tests = load_manifest(repo_root).tests_path
    except Exception:
        return None
    owner = tests / _OWNER
    if not owner.is_file():
        return None
    tree = corpus.tree(owner)
    if tree is None:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _REQUIRED:
            return owner
    return None


def _func_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _subcommand(nodes) -> str:
    """The git subcommand `nodes` spells, reading them as an argv.

    `"git"` and any flag are dropped, and the first plain word left is the
    subcommand. This is the whole reason the gate does not simply look for the
    string `"init"` anywhere in the call: `_git(root, "commit", "-qm", "init")`
    names a *commit message*, and three of this suite's fixtures spell exactly
    that. Reading position rather than presence tells them apart.

    Non-constant elements — `str(repo)` in `["git", "-C", str(repo), "init"]` —
    are invisible here, which is what makes that shape read correctly. An argv
    that passed its path as a string literal instead would hide the subcommand
    behind it and go unreported; a gate that misses a case is repairable, one
    that cries wolf on its own fixtures gets muted.
    """
    for node in nodes:
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        word = node.value
        if word == "git" or word.startswith("-"):
            continue
        return word
    return ""


def _offences(tree: ast.AST) -> list[tuple[int, str]]:
    """`(line, what was seen)` for every hand-rolled `git init` in `tree`."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            words = [n.value for n in node.elts
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if "git" in words and _subcommand(node.elts) == "init":
                found.append((node.lineno, 'a ["git", "init", ...] argv'))
        elif isinstance(node, ast.Call):
            name = _func_name(node)
            if "git" in name.lower() and name != _REQUIRED \
                    and _subcommand(node.args) == "init":
                found.append((node.lineno, f'{name}(..., "init", ...)'))
    return found


def check(repo_root: Path) -> list[Violation]:
    owner = _template_module(repo_root)
    if owner is None:
        return []  # no shared template here — nothing to point a test at
    owner_rel = owner.relative_to(repo_root).as_posix()

    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []
    for path in sorted(owner.parent.rglob("*.py")):
        if path == owner or "__pycache__" in path.parts:
            continue
        tree = corpus.tree(path)
        if tree is None:
            continue  # not this gate's job to report unparseable Python
        rel = path.relative_to(repo_root).as_posix()
        for line, seen in _offences(tree):
            if appeal_markers.exempt(appeals, NAME, rel, line):
                continue
            violations.append(Violation(
                rel, line,
                f"fixture_rebuild: {seen} builds a git repo from scratch. "
                f"`git init` plus the identity config measured 135 ms against 18 ms "
                f"to copy a prebuilt skeleton, and a whole fixture tree 467 ms "
                f"against 68 ms. Call `{owner_rel}`'s `{_REQUIRED}()` or "
                f"`repo_copy()`, or — if something in this repo records an absolute "
                f"path and genuinely cannot be copied — appeal with "
                f"`# gate-ok({NAME}): <reason>`",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
