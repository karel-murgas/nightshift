"""Gate: no argv built in the tooling carries a worker prompt on `-p`.

**Earned by an observed failure, not by being easy** (`00_architecture.md` §15).
On 2026-08-06, launching the overnight run, the `menu-unlock-indicators` card had
grown to 36,146 B; the prompt the runner built around it was 40,716 B. Windows caps
a `CreateProcess` command line at 32,767 characters, so `subprocess.Popen` in
`runner._run_worker` never started the child at all — it raised
`FileNotFoundError: [WinError 206] The filename or extension is too long` and took
the queue with it. The prompt was on `argv`:

    argv = [binary, "-p", prompt, "--agent", card.worker, ...]

The fix (`worker-prompt-off-argv`) was to change the transport, not to bound the
prompt: `claude -p` reads its prompt from **stdin** when no prompt positional is
given, so `_run_worker` takes `prompt=` and writes it down the child's pipe. There
is no size ceiling left on that path.

**Why a gate and not a comment.** There were five call sites, not one — four in
`runner.py` (`run_checker`, `review_branch`, `run_stale_check`, `run_producer`) and
one in `fix.py` — and the set is still growing: a chartered audio worker and a
second checker stage are both scoped. The sixth will be written by copying the
nearest existing one, and if that one still spelled `-p prompt` the bug would come
back **silently, on Windows only, on a big prompt only** — which is exactly why it
went unnoticed for the entire life of the runner. That is the `unenforced-rule`
class: a rule that lives only in prose is a rule the next author never reads.

Precision, and why it is an AST walk over sequence literals rather than a text
search (the same distinction `pytest_invocation` draws):

  * **only inside a list or tuple literal**, i.e. something being built into an
    argv. This file's own docstring shows the defective line, and `runner.py`'s
    docstrings discuss `-p` mode at length; a text search would flag its own
    documentation and a gate with false positives on its own docs gets muted.
  * **only when the element after the flag is not another flag.** `[binary, "-p",
    "--agent", ...]` is the corrected shape and must stay green; `[binary, "-p",
    prompt, ...]` and `[binary, "--print", text]` are the defect. A trailing `-p`
    with nothing after it is fine, and so is a `*splat` — every splat in this tree
    is a flag group (`_STREAM_ARGV`, `_budget_argv(...)`), and flagging it would
    cost a false positive to catch a spelling nobody writes.

Scope: `.ai/` plus `[project].tooling_dirs` — see `gates/scope.py`. The rule is
about code that *dispatches* a worker, which is what tooling means here.

Appeals: `# gate-ok(prompt_not_in_argv): <reason>` — see `appeal_markers.py` and
`10_self_improvement.md` §4. A genuine appeal would have to be a `-p` belonging to
some other program entirely (`git log -p`, say), which is why the marker names the
line rather than the file.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates import corpus
from nightshift.gates import scope
from nightshift.gates.base import Violation

NAME = "prompt_not_in_argv"
FAST = True
DESCRIPTION = "no argv carries a worker prompt on `-p`; it goes down the child's stdin"

# Both spellings the CLI accepts. `--print` is the long form and takes no value
# either; a gate that only knew the short one would miss half the ways back in.
_FLAGS = frozenset({"-p", "--print"})


def _is_flag_literal(node: ast.expr) -> bool:
    """A string constant that looks like another option, e.g. `"--agent"`.

    This is what separates the corrected shape from the defect: after `-p` the
    next argv element should be the next *flag*, never a value.
    """
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith("-"))


def _offending_elements(tree: ast.AST) -> list[tuple[int, str]]:
    """`(line, flag)` for every `-p`/`--print` in a sequence literal that is
    followed by something which is not a flag."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        for index, element in enumerate(node.elts[:-1]):
            if not (isinstance(element, ast.Constant)
                    and isinstance(element.value, str)
                    and element.value in _FLAGS):
                continue
            following = node.elts[index + 1]
            # A splat is a flag group, not a value — see the module docstring.
            if isinstance(following, ast.Starred) or _is_flag_literal(following):
                continue
            found.append((getattr(following, "lineno", node.lineno), element.value))
    return found


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []

    for path in scope.tooling_files(repo_root):
        tree = corpus.tree(path)
        if tree is None:
            continue  # not this gate's job to report unparseable Python
        rel = path.relative_to(repo_root).as_posix()
        for line, flag in _offending_elements(tree):
            if appeal_markers.exempt(appeals, NAME, rel, line):
                continue
            violations.append(Violation(
                rel, line,
                f"{NAME}: `{flag}` is followed by a value in this argv. A prompt on the "
                f"command line dies at Windows' 32,767-character limit with WinError 206 "
                f"and takes the whole run with it — leave `{flag}` bare and pass the text "
                f"as `_run_worker(..., prompt=...)`, which writes it to the child's stdin",
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
