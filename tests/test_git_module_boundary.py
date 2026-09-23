"""Stays fixed: `nightshift/git.py` is the only place a git subprocess is
invoked by hand.

The git-module-and-runner-split workstream replaced 14 private `_git()`
helpers (boardcmd, discover, doctor, freshness, ingest, merge_check,
preflight, runner, three gates, three hooks) plus a scatter of raw
`subprocess.run(["git", ...])` calls with the one reader in `nightshift/git.py`.
This is the regression guard: a `def _git(` anywhere else in the package, or a
bare `subprocess.run`/`subprocess.Popen` call whose argv starts with `"git"`,
fails the whole tree. `gates/git_path_lists.py` narrowed to checking `git.py`
alone (§ its own docstring) on the strength of this broader net existing.

Not a gate: this is a boundary that should never need an appeal — the fix is
always "call `nightshift.git` instead" — so unlike the gates it carries no
`# gate-ok(...)` escape hatch. `ALLOWED` exists for the same reason
`git_path_lists`' appeal path does: a rare, genuine exception should be
possible without weakening the check for everyone else, but it is named here,
in the test, with a reason — not hidden behind a marker comment a future edit
could paste anywhere.
"""
from __future__ import annotations

import ast
from pathlib import Path

import nightshift

#: `(module, reason)` pairs exempted from the "no `def _git(`" half of the
#: check. Empty on purpose: the whole point of `nightshift.git` is that
#: nothing else needs one.
ALLOWED_GIT_DEF = {}

#: `module -> reason` exempted from the "no bare git subprocess call" half.
#: Both entries are binary-mode reads `git.run()` cannot serve: it always
#: passes `text=True, encoding="utf-8"`, and each of these needs raw bytes —
#: `normalize_worktree._index_bytes` reads an unfiltered blob (a text decode
#: would apply the same CRLF/LF conversion the caller exists to detect), and
#: `source_reference_liveness._gitignored` writes `--stdin` (a text write
#: translates `\n` to `\r\n` on Windows, corrupting the very paths it probes —
#: see that function's own docstring). Adding a binary-mode parameter to
#: `git.run()` for these two callers would be one shape grown for one caller
#: each; two real callers that cannot use the text contract are the reason
#: this allowlist exists rather than the check having no exceptions at all.
ALLOWED_BARE_CALL = {
    "normalize_worktree.py": "cat-file blob :path must read unfiltered bytes",
    "gates/source_reference_liveness.py": "git check-ignore --stdin must write bytes, not text",
}

PACKAGE = Path(nightshift.__file__).parent


def _modules() -> list[tuple[str, Path]]:
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts or "templates" in path.parts:
            continue
        rel = path.relative_to(PACKAGE).as_posix()
        found.append((rel, path))
    return found


def _is_git_argv(node: ast.Call) -> bool:
    """Whether `node` is `subprocess.run(...)`/`subprocess.Popen(...)` (however
    spelled) whose first positional argument is a list/tuple literal starting
    with the string `"git"`."""
    func = node.func
    is_subprocess_call = (
        isinstance(func, ast.Attribute) and func.attr in ("run", "Popen", "call",
                                                           "check_call", "check_output")
        and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
    )
    if not is_subprocess_call or not node.args:
        return False
    first = node.args[0]
    if not isinstance(first, (ast.List, ast.Tuple)) or not first.elts:
        return False
    head = first.elts[0]
    return isinstance(head, ast.Constant) and head.value == "git"


def test_no_second_git_helper_is_defined():
    """A `def _git(` anywhere outside `git.py` is the one shape this whole
    workstream removed 14 copies of."""
    violations = []
    for rel, path in _modules():
        if rel == "git.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_git":
                if rel not in ALLOWED_GIT_DEF:
                    violations.append(f"{rel}:{node.lineno}")
    assert not violations, (
        f"a second `_git()` helper reappeared at {violations} — call "
        f"`nightshift.git` instead, or add it to ALLOWED_GIT_DEF with a reason "
        f"if it is genuinely needed")


def test_no_bare_git_subprocess_call_outside_git_py():
    """A raw `subprocess.run(["git", ...])`/`Popen(["git", ...])` anywhere
    outside `git.py` is exactly the pattern `nightshift.git` exists to be the
    one spelling of."""
    violations = []
    for rel, path in _modules():
        if rel == "git.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_git_argv(node):
                if rel not in ALLOWED_BARE_CALL:
                    violations.append(f"{rel}:{node.lineno}")
    assert not violations, (
        f"a bare git subprocess call reappeared at {violations} — call "
        f"`nightshift.git` instead, or add it to ALLOWED_BARE_CALL with a "
        f"reason if it is genuinely needed")


def test_git_py_itself_still_has_the_real_thing():
    """The scan is not vacuously green because it stopped seeing `git.py`."""
    tree = ast.parse((PACKAGE / "git.py").read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.Call) and _is_git_argv(n) for n in ast.walk(tree))
