r"""Gate: no text-mode write under `.ai/` leaves newline translation unpinned.

**Earned by an observed failure, not by being easy** (`00_architecture.md` §15) —
and it is the third member of a family. `subprocess_result_checked` is "the call
succeeded, nobody looked"; `subprocess_encoding` is "`text=True` decodes with the
locale codec"; this one is the write side of the same platform trap.

`.gitattributes` pins this repo to `* text=auto eol=lf` — LF in the index *and* in
the working tree, so that no filter ever rewrites anything and no writer can leave a
phantom-dirty file (one git reports as modified forever, with an empty diff, because
only the representation disagrees). Gate `line_endings` enforces the *result*.

`Path.write_text()` breaks the pin. It opens in text mode with `newline=None`, which
translates every `\n` to `os.linesep` -- `\r\n` here. So the read-modify-write shape
every `.ai/` tool uses on the files it owns silently converted them:

    >>> p.write_bytes(b"x\ny\n")
    >>> p.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    >>> p.read_bytes()
    b'x\r\ny\r\n'

Found 2026-07-30 by running `corrections.py --compact` and `digest.py`: three tracked
files went CRLF and `line_endings` went red. **The symptom gate worked; the producers
were the bug**, and they had merely not run since it landed the same day -- so the next
board move would have failed a card on a formality with nothing wrong with the card.

**Why a gate and not the test that already covers it.** The first fix shipped as an
AST test parameterised over the five modules that own a committed artefact
(`board`, `corrections`, `digest`, `reconcile`, `stale_sweep`). Writing *this* gate
immediately found **two more tracked-file writers the hand-written list had missed**,
both in `runner.py` and both on the path that actually runs at night: the runner writes
`Digest.md` itself in the work root, and the staleness sweep writes a new
`Board/tasks/fix-stale-*.md` card. A list of writers is exactly as complete as whoever
last remembered to extend it; a scan of `.ai/` is complete by construction.

**The rule is "pin it", not "use the helper".** Mirroring `subprocess_encoding`, which
checks that `encoding=` is *present* and never inspects its value: anything explicit is
a decision, and the defect is the absence of one. Two spellings pass --
`textio.write_text_lf()` / `append_text_lf()` (the house idiom, and the only one that
reads as intent), or an explicit `newline=` on the call. A site that genuinely wants
platform endings writes `newline=None` and says so.

Scope: `.ai/` only -- the orchestrator, where a silent failure costs a night. Game code
under `dungeoneer/` writes no committed text file. Note this is an `_INFRA_GATES` rule
(about the tooling, not about `dungeoneer/`), so it is in the set Session K's D3 ships
to a second project -- and it must be, because `line_endings` travels with `core/` too,
and a copied core without this gate ships a known tripwire on the platform most likely
to trip it.

Gitignored artefacts under `.ai/runs/` are *not* exempt. Nothing there needs LF, but a
uniform rule is what stops the next writer from copying the wrong pattern out of a
neighbouring line -- which is precisely how all seven original sites came to exist.

Appeals: `# gate-ok(write_newline): <reason>` -- see `appeal_markers.py` and
`10_self_improvement.md` §4. A genuine appeal is rare: if the bytes really are binary,
open in binary mode and skip text handling entirely.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates import corpus
from nightshift.gates.base import Violation

NAME = "write_newline"
FAST = True
DESCRIPTION = "no text-mode write under .ai/ leaves newline translation unpinned"

_ROOT = Path(".ai")


def _has_newline_kwarg(node: ast.Call) -> bool:
    """`newline=` present at all. Its value is not inspected -- see the module
    docstring: explicit is a decision, absent is the defect."""
    return any(kw.arg == "newline" for kw in node.keywords)


def _mode_arg(node: ast.Call, positional_index: int) -> str | None:
    """The literal mode string, from `mode=` or from its positional slot.

    Only a string literal counts. A computed mode could be anything, and a gate
    that guessed would fire on code that already decided correctly.
    """
    for kw in node.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) \
                and isinstance(kw.value.value, str) else None
    if len(node.args) > positional_index:
        arg = node.args[positional_index]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _is_text_write_mode(mode: str | None) -> bool:
    """A mode that writes or appends *text*. Binary is out of remit -- `newline=`
    is not even a legal argument there, so it cannot be the defect."""
    if mode is None:
        return False
    if "b" in mode:
        return False
    return any(ch in mode for ch in ("w", "a", "x", "+"))


def _offence(node: ast.Call) -> str | None:
    """The reason this call violates the rule, or None."""
    func = node.func

    # `<path>.write_text(...)` — always text mode, no mode argument to read.
    if isinstance(func, ast.Attribute) and func.attr == "write_text":
        if _has_newline_kwarg(node):
            return None
        return ("Path.write_text() without `newline=` translates \\n to os.linesep "
                "(CRLF on Windows), which breaks .gitattributes' `eol=lf` pin and "
                "turns the `line_endings` gate red")

    # `<path>.open("w")` — mode is the first positional argument.
    if isinstance(func, ast.Attribute) and func.attr == "open":
        mode = _mode_arg(node, 0)
        if _is_text_write_mode(mode) and not _has_newline_kwarg(node):
            return (f"Path.open({mode!r}) without `newline=` translates \\n to "
                    f"os.linesep (CRLF on Windows), breaking .gitattributes' "
                    f"`eol=lf` pin")
        return None

    # Builtin `open(path, "w")` — mode is the second positional argument.
    if isinstance(func, ast.Name) and func.id == "open":
        mode = _mode_arg(node, 1)
        if _is_text_write_mode(mode) and not _has_newline_kwarg(node):
            return (f"open(..., {mode!r}) without `newline=` translates \\n to "
                    f"os.linesep (CRLF on Windows), breaking .gitattributes' "
                    f"`eol=lf` pin")
        return None

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
        tree = corpus.tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            reason = _offence(node)
            if reason is None:
                continue
            if appeal_markers.exempt(appeals, NAME, rel, node.lineno):
                continue
            violations.append(Violation(
                rel, node.lineno,
                f"{reason}. Use textio.write_text_lf()/append_text_lf(), or pass "
                f"`newline=` explicitly, or appeal with `# gate-ok({NAME}): <reason>`",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
