"""Gate: a git command that asks for a list of paths asks for it NUL-separated.

**Earned by two measurements on one day, and by eleven sites** (`00_architecture.md`
§15). Git's default output for a path list is ambiguous in two ways at once, and
this package had a reader for each:

* separated by newlines, so `.stdout.split()` — five sites — splits
  `Board/inbox/Stale README.md` into two paths, the second a file nobody touched;
* **quoted and C-escaped** when a path is not printable ASCII (`core.quotepath`),
  so `.splitlines()` — six sites — hands back
  `"Board/ideas/Animation \\342\\200\\223 attack.md"`, quotes and all, for a name
  with an en dash in it.

Every one of those sites was correct-looking, individually reasonable, and wrong
only for filenames nobody on the team writes — which is exactly the population a
*board* is made of, because a note is named the way a person names a thought. The
cost was invisible for months: a fabricated or mangled token classifies as
`other`, and `other` selects no tests, refuses no merge and hides no card. That is
`cost-only-visible-in-aggregate`, whose own entry says the enforcement point has
to be built while it still looks like over-engineering.

`-z` removes the ambiguity at the source: no quoting, no escaping, and a separator
that cannot occur in a path. `nightshift/gitpaths.py` is the one reader, and this
is what stops the next site being written without it.

**What is watched:** a call whose string arguments form a git argv asking for
paths — `status --porcelain`, anything with `--name-only`/`--name-status`, or
`ls-files`. The argv may be spelled either way this package spells one: as a list
literal (`["git", "status", "--porcelain"]`) or as loose arguments to a `_git`
helper (`_git(root, "status", "--porcelain")`), which is why the tokens are read
off the call rather than off a particular parameter.

**What is not:** `git worktree list --porcelain`, which is a key-value listing and
not a path list — the trigger is `status` *with* `--porcelain`, never the flag
alone; and `ls-files --error-unmatch <path>`, which asks a yes/no question about a
path it already holds and reads only the exit code.

Appeals: `# gate-ok(git_path_lists): <reason>` — see `appeal_markers.py` and
`10_self_improvement.md` §4. A genuine one is rare, and `--error-unmatch` shows
its shape: the output is not being read as a list of paths at all.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates import corpus
from nightshift.gates import scope
from nightshift.gates.base import Violation

NAME = "git_path_lists"
FAST = True
DESCRIPTION = "a git command that lists paths lists them NUL-separated"

#: The module that owns the reading, and the only place these argvs belong.
_READER = "nightshift/gitpaths.py"

#: Flags that make an argv a path list on their own.
_LISTING_FLAGS = frozenset({"--name-only", "--name-status"})

#: Subcommands that list paths, each with the condition that makes it one.
#: `status` needs `--porcelain` to be machine-read at all; `ls-files` is a path
#: list by nature.
_LISTING = (("status", "--porcelain"), ("ls-files", ""))

#: The one `ls-files` shape that is a question rather than a listing: it names the
#: path it is asking about and the answer is the exit code.
_NOT_A_LISTING = frozenset({"--error-unmatch"})


def _tokens(node: ast.Call) -> list[str]:
    """Every string constant this call passes, from its own arguments and from any
    list or tuple literal among them.

    Both spellings in one pass, deliberately. A gate that only understood the list
    literal would miss `_git(root, "status", "--porcelain")`, which is how most of
    this package spells it, and a gate that only understood loose arguments would
    miss the hooks, which build a real argv.
    """
    found: list[str] = []
    for arg in list(node.args) + [kw.value for kw in node.keywords]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.append(arg.value)
        elif isinstance(arg, (ast.List, ast.Tuple)):
            found += [element.value for element in arg.elts
                      if isinstance(element, ast.Constant)
                      and isinstance(element.value, str)]
    return found


def _lists_paths(tokens: list[str]) -> str:
    """What makes this argv a path list, or `""` if nothing does."""
    if _NOT_A_LISTING & set(tokens):
        return ""
    if flags := _LISTING_FLAGS & set(tokens):
        return sorted(flags)[0]
    for subcommand, needs in _LISTING:
        if subcommand in tokens and (not needs or needs in tokens):
            return f"{subcommand} {needs}".strip()
    return ""


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []
    for path in scope.tooling_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        if rel.endswith(_READER):
            continue
        tree = corpus.tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            tokens = _tokens(node)
            asked = _lists_paths(tokens)
            if not asked or "-z" in tokens:
                continue
            if appeal_markers.exempt(appeals, NAME, rel, node.lineno):
                continue
            violations.append(Violation(
                rel, node.lineno,
                f"`git {asked}` without `-z` returns paths git may quote and "
                f"C-escape, separated by newlines a filename is free to contain — "
                f"read it through `nightshift.gitpaths` (or pass `-z` and split on "
                f"NUL), or appeal with `# gate-ok({NAME}): <reason>`",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
