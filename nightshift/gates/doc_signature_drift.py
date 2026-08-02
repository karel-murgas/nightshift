"""Gate: a signature written into a doc must match the real one
(08_staleness.md §3.2, staleness class S2).

Worth doing narrowly because `ref_minigame.md` is *built* out of fenced
signature blocks — when a constructor grows a parameter, that file is wrong and
nothing says so.

Deliberate scope limits, straight from §3.2:

  * **parameter names and count only, in order.** Types, defaults and
    annotations are explicitly out of scope — "too much surface for too little
    yield", and doc blocks abbreviate types constantly on purpose.
  * only fenced blocks. Prose that mentions an argument is not a signature.
  * a `def` the gate cannot resolve to exactly one real definition is dropped,
    not guessed at. Same quote-or-drop discipline as the Tier 2 checker (§4):
    an unresolvable finding is not a finding.

Doc blocks in this project omit `self` (`def __init__(app, params, …)`), so a
leading `self`/`cls` on the real definition is dropped when the doc's own
first parameter is not one.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
from pathlib import Path

from nightshift.gates import doc_scan
from nightshift.gates import corpus
from nightshift.gates.base import Violation

NAME = "doc_signature_drift"
FAST = False
DESCRIPTION = "a signature written into a doc must match the real parameter names, in order"

_FENCE = re.compile(r"^\s*```")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_CLASS = re.compile(r"^\s*class\s+(\w+)")


def _params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = node.args
    names = [arg.arg for arg in (*a.posonlyargs, *a.args)]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names.extend(arg.arg for arg in a.kwonlyargs)
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return names


def _source_signatures(repo_root: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(qualname -> params, bare name -> params for names defined exactly once)."""
    qualified: dict[str, list[str]] = {}
    by_name: dict[str, list[list[str]]] = defaultdict(list)

    for root in ("dungeoneer",):
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = corpus.tree(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            params = _params(child)
                            qualified[f"{node.name}.{child.name}"] = params
                            by_name[child.name].append(params)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = _params(node)
                    qualified.setdefault(node.name, params)
                    by_name[node.name].append(params)

    unique = {
        name: sigs[0]
        for name, sigs in by_name.items()
        if all(s == sigs[0] for s in sigs)
    }
    return qualified, unique


def _doc_signatures(text: str) -> list[tuple[int, str | None, str, list[str]]]:
    """(line, enclosing class or None, def name, params) for fenced blocks."""
    found: list[tuple[int, str | None, str, list[str]]] = []
    lines = text.splitlines()
    in_fence = False
    current_class: str | None = None

    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
            if in_fence:
                current_class = None
            i += 1
            continue
        if in_fence:
            class_match = _CLASS.match(line)
            if class_match:
                current_class = class_match.group(1)
            def_match = _DEF.match(line)
            if def_match:
                # Collect through to the balanced closing paren — signatures in
                # these docs routinely span 20 lines.
                buf = line[def_match.end() - 1:]
                depth = buf.count("(") - buf.count(")")
                start = i
                while depth > 0 and i + 1 < len(lines):
                    i += 1
                    if _FENCE.match(lines[i]):
                        break
                    buf += "\n" + lines[i]
                    depth = buf.count("(") - buf.count(")")
                if depth == 0:
                    params = _parse_params(buf)
                    if params is not None:
                        found.append((start + 1, current_class, def_match.group(1), params))
        i += 1
    return found


def _parse_params(buf: str) -> list[str] | None:
    """Parameter *names* out of a raw '(...)' span. Annotations and defaults
    are stripped rather than parsed — §3.2 puts types out of scope, and doc
    blocks abbreviate them (`Callable[[bool, List[Item]], None]`) in ways no
    parser should be asked to reconcile."""
    inner = buf[buf.index("(") + 1: buf.rindex(")")]
    names: list[str] = []
    depth = 0
    current = ""
    for char in inner:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += char
    names.append(current)

    out: list[str] = []
    for raw in names:
        token = raw.split("#")[0].split("=")[0].split(":")[0].strip()
        if not token or token in ("*", "/"):
            continue
        if not re.fullmatch(r"\*{0,2}\w+", token):
            return None  # not a plain parameter list — drop rather than guess
        out.append(token)
    return out


def check(repo_root: Path) -> list[Violation]:
    qualified, unique = _source_signatures(repo_root)
    violations: list[Violation] = []

    for path in doc_scan.doc_files(repo_root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if doc_scan.is_exempt_file(text):
            continue
        skip = doc_scan.exempt_lines(text)
        rel = doc_scan.relpath(path, repo_root)

        for line, cls, name, documented in _doc_signatures(text):
            if line in skip:
                continue
            if cls and f"{cls}.{name}" in qualified:
                actual = qualified[f"{cls}.{name}"]
            elif not cls and name in unique:
                actual = unique[name]
            else:
                continue  # unresolvable or ambiguous — drop, do not guess

            trimmed = list(actual)
            if trimmed and trimmed[0] in ("self", "cls") and documented[:1] != trimmed[:1]:
                trimmed = trimmed[1:]
            if documented == trimmed:
                continue
            violations.append(
                Violation(
                    rel,
                    line,
                    f"doc_signature_drift: `{name}` documented as "
                    f"({', '.join(documented)}) but is ({', '.join(trimmed)})",
                )
            )
    return violations


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent.parent.parent
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(root):
        print(violation)
