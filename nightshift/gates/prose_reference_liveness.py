"""Gate: a backticked module/file name inside a docstring or comment in the
tooling source must resolve to something real.

`source_reference_liveness` deliberately does not scan docstrings/comments —
its own docstring explains why: this codebase's docstrings are long and
discursive by house style, and scanning them turned up dozens of sites that
were never the defect that gate exists to catch, mostly prose describing how
a *consuming* project would reference something. That exclusion is right for
*live-code* path literals. It leaves a real gap for a narrower shape: a
backtick-quoted `nightshift.<module>`, a bare *name.py*, or a slash-separated
*path/name.py*, naming something that used to exist and does not any more
-- `digest.py` (deleted 2026-09-02) surviving as a live-sounding reference in
a dozen docstrings across this package (docstring-and-manifest-diet, slice 1)
is exactly the failure this exists to catch the next time a module is
deleted.

**Deliberately narrow, on purpose, to keep false positives low**: only the
three backticked shapes above are matched (never a bare word in prose), and a
match is exempt the moment "since removed", "deleted" or "retired" appears
within ~200 characters of it -- a historical mention saying its own piece is
not a violation, and this is the same bargain `08_staleness.md`'s
`stale-ok:` marker makes on the doc side rather than adding a second
allowlist file. `# gate-ok(prose_reference_liveness): <reason>` covers
whatever that window still misses.

**Resolution**: `nightshift.<dotted>` walks the longest resolvable prefix
against the installed package's real files, the same "attribute of the
module that exists" rule `nightshift.suite._resolve` already uses for import
graphs -- `nightshift.decide.answer_pattern` is fine because `decide.py`
exists, even though `answer_pattern` itself is not independently checked.
A slash-separated path resolves against the repo root, or against the
installed package's own directory when the first segment is `nightshift`. A
bare filename resolves if that basename exists anywhere under the scanned
trees.

**Scope**: docstrings and `#` comments under `nightshift/` (this package)
and, in a consuming project, its own `.ai/` -- the same two trees
`source_reference_liveness` reasons about paths in, on purpose (a rule about
tooling prose belongs at tooling scope, not at the whole project).
"""
from __future__ import annotations

import ast
import importlib.util
import re
import tokenize
from io import StringIO
from pathlib import Path

import appeal_markers
from nightshift import manifest as _manifest
from nightshift.gates import corpus
from nightshift.gates.base import Violation
from nightshift.manifest import AI_DIR

NAME = "prose_reference_liveness"
DESCRIPTION = ("a backticked dotted nightshift module or .py file name in a "
               "docstring or comment must resolve to something real")
SUBJECT = ("nightshift", AI_DIR)

# `` `nightshift.hooks.correction_prompt` `` -- a dotted path starting with the
# package name itself, so a bare `` `nightshift` `` alone never matches (nothing
# after the first dot).
_DOTTED_RE = re.compile(r"`(nightshift(?:\.[A-Za-z_][\w]*)+)`")
# A backticked token ending `.py`, with or without a leading slash-separated path
# (e.g. path/to/name.py or a bare name.py). `[\w./-]*` allows hidden dirs and
# hyphenated names; a literal backtick can never be part of the match since the
# outer backticks bound it.
_PY_RE = re.compile(r"`([\w.][\w./-]*\.py)`")

_EXEMPT_WORDS = ("since removed", "deleted", "retired")
_EXEMPT_WINDOW = 200


def _exempted(text: str, start: int, end: int) -> bool:
    """A "(since removed)"/deleted/retired word within `_EXEMPT_WINDOW` chars of
    the match marks it as a deliberate historical mention rather than a stale
    reference nobody noticed -- a loose, sentence-ish window rather than a real
    parse, on the same "cheap and low-false-positive beats exact" reasoning
    `source_reference_liveness` already used for its own heuristics."""
    lo, hi = max(0, start - _EXEMPT_WINDOW), min(len(text), end + _EXEMPT_WINDOW)
    return any(word in text[lo:hi].lower() for word in _EXEMPT_WORDS)


def _docstrings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every module/class/function docstring's own text, tagged with the line
    its literal opens on -- so a match deep inside a long docstring is still
    reported (and exempted/appealed) at the line it actually sits on, not the
    line the enclosing `def`/`class`/module starts on."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            body = getattr(node, "body", None)
            if doc and body:
                out.append((body[0].lineno, doc))
    return out


def _comments(path: Path) -> list[tuple[int, str]]:
    """Every `#` comment, consecutive lines joined into one block so a backtick
    span wrapped across two comment lines by house style still reads as one
    unit. Tagged with the block's first line."""
    try:
        tokens = tokenize.generate_tokens(StringIO(
            path.read_text(encoding="utf-8", errors="replace")).readline)
        comment_tokens = [t for t in tokens if t.type == tokenize.COMMENT]
    except (tokenize.TokenError, SyntaxError, IndentationError, OSError, UnicodeError):
        return []
    blocks: list[tuple[int, str]] = []
    current: list[str] = []
    start_line = 0
    last_line = -1
    for tok in comment_tokens:
        line = tok.start[0]
        text = tok.string.lstrip("#").strip()
        if current and line == last_line + 1:
            current.append(text)
        else:
            if current:
                blocks.append((start_line, " ".join(current)))
            current = [text]
            start_line = line
        last_line = line
    if current:
        blocks.append((start_line, " ".join(current)))
    return blocks


def _nightshift_root() -> Path | None:
    spec = importlib.util.find_spec("nightshift")
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0])


def _dotted_resolves(dotted: str, pkg_root: Path) -> bool:
    """Longest-prefix resolution: an attribute reached through a real module is
    not a violation, only a path that resolves to nothing at any depth is."""
    parts = dotted.split(".")[1:]  # drop the leading "nightshift"
    while parts:
        candidate = pkg_root.joinpath(*parts)
        if candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file():
            return True
        parts = parts[:-1]
    return False


def _py_resolves(token: str, repo_root: Path, pkg_root: Path | None,
                  source_dirs: tuple[str, ...] = ()) -> bool:
    if "/" in token:
        candidates = [repo_root / token]
        candidates.extend(repo_root / d / token for d in source_dirs)
        if pkg_root is not None:
            # House style inside the package omits the leading `nightshift/` --
            # `gates/scope.py` means `nightshift/gates/scope.py` from in here --
            # so both spellings resolve against the package directory.
            rel = token[len("nightshift/"):] if token.startswith("nightshift/") else token
            candidates.append(pkg_root / rel)
        return any(c.is_file() for c in candidates)
    # A bare filename: resolved if it exists anywhere under the repo, its
    # declared source dirs, the installed package, or (for a project citing a
    # nightshift test by name) the nightshift repo root the package sits in.
    if any(repo_root.rglob(token)):
        return True
    if pkg_root is not None:
        if any(pkg_root.rglob(token)):
            return True
        if any(pkg_root.parent.glob(f"tests/{token}")):
            return True
    return False


def _scan_dirs(repo_root: Path) -> list[Path]:
    dirs = []
    ns = repo_root / "nightshift"
    if ns.is_dir():
        dirs.append(ns)
    ai = repo_root / AI_DIR
    if ai.is_dir():
        dirs.append(ai)
    return dirs


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    pkg_root = _nightshift_root()
    violations: list[Violation] = []
    try:
        project = _manifest.load(repo_root).project
        source_dirs = (*project.source_dirs, *project.extra_source_dirs)
    except _manifest.ManifestError:
        source_dirs = ()

    files: list[Path] = []
    for d in _scan_dirs(repo_root):
        for path in sorted(d.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)

    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        tree = corpus.tree(path)
        blocks: list[tuple[int, str]] = list(_docstrings(tree)) if tree is not None else []
        blocks.extend(_comments(path))

        for start_line, text in blocks:
            for pattern in (_DOTTED_RE, _PY_RE):
                for m in pattern.finditer(text):
                    token = m.group(1)
                    if pattern is _DOTTED_RE:
                        ok = pkg_root is not None and _dotted_resolves(token, pkg_root)
                    else:
                        ok = _py_resolves(token, repo_root, pkg_root, source_dirs)
                    if ok:
                        continue
                    if _exempted(text, m.start(), m.end()):
                        continue
                    # The match's own line within a (possibly multi-line) block --
                    # not just the block's opening line -- so a marker can be
                    # placed near what it is actually exempting.
                    lineno = start_line + text.count("\n", 0, m.start())
                    if appeal_markers.exempt(appeals, NAME, rel, lineno):
                        continue
                    violations.append(Violation(
                        rel, lineno,
                        f"{NAME}: `{token}` looks like a module/file reference in prose "
                        f"but resolves to nothing. If it is a historical mention, say so "
                        f"(\"since removed\"/deleted/retired); otherwise fix or appeal "
                        f"with `# gate-ok({NAME}): <reason>`",
                    ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
