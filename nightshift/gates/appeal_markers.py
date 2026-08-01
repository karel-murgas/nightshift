"""Shared machinery for gate appeals in *code* (`10_self_improvement.md` §4).

Session D built the exemption mechanism for *docs*: `doc_scope:` frontmatter
and `<!-- stale-ok: reason -->` markers, 45 of them, each carrying a written
reason and counted as debt. That mechanism works and is not changed here. What
it did not cover is a gate firing on a `.py` file, and the temptation there is
the one §4 exists to stop: quietly narrowing the gate until the run goes green.

The marker is deliberately the same shape as `stale-ok:` -- reason mandatory,
gate named explicitly, counted rather than hidden:

    subprocess.run(...)  # gate-ok(subprocess_result_checked): git commit exits
                         # non-zero when there is nothing to commit, which is a
                         # normal outcome here, not a failure to report.

Three properties, and each is there because of a specific way exemptions rot:

* **It names one gate.** A blanket "ignore all gates here" marker is how a
  single awkward line ends up suppressing checks nobody was thinking about.
* **The reason is mandatory and long.** Session D's tests assert >= 10 chars on
  `stale-ok:`; this asks 20, because a code exemption is read less often than a
  doc one and gets correspondingly less scrutiny.
* **They are counted.** `gate_appeals` prints the total. A number nobody prints
  is a number nobody looks at, and the debt count is the whole reason the
  process is safe to have at all.

Continuation lines are joined, so a reason may wrap -- the same decision, for
the same reason, as `_STALE_OK_OPEN` in `doc_scan.py`: a one-line cap pushes
authors toward terse, useless reasons.

**Markers are found with `tokenize`, not with a line regex**, so only a real
comment token counts. That is not tidiness: the first run of `gate_appeals`
flagged the marker written *inside this module's own docstring* as a live
appeal with an 8-word reason -- which is `gate-self-bug` (2026-07-22) repeating
itself exactly, in the session that read the log entry describing it. A doc
explaining a marker mechanism will always contain the marker; the parser has to
know the difference between mentioning one and writing one.

**Default scan roots come from the project's manifest** (07_portability.md
§8 step 3), not a hardcoded list of Dungeoneer directories. `.ai/` is always
included -- every appeal-bearing gate lives there or checks it -- and
`project.source_dirs` / `project.extra_source_dirs` add whatever the
consuming project calls its code and tests. A repo with no manifest (or one
`nightshift.manifest` cannot read) falls back to `.ai/` alone, which is the
safe direction: fewer files scanned, never a crash.
"""
from __future__ import annotations

import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

from nightshift.gates import corpus
from nightshift.manifest import AI_DIR

# `# gate-ok(<gate>): <reason>` — the gate name is a gate's module stem, core or
# project-side.
_MARKER = re.compile(r"#\s*gate-ok\(([A-Za-z0-9_]*)\)\s*:\s*(.*)$")
# A bare `#` line directly under a marker continues its reason.
_CONTINUATION = re.compile(r"^\s*#\s?(.*)$")

MIN_REASON = 20


@dataclass(frozen=True)
class Appeal:
    file: str       # repo-relative, posix
    line: int       # 1-based, the line the marker opens on
    end_line: int   # last line of the (possibly wrapped) reason
    gate: str
    reason: str


def _comment_lines(source: str) -> dict[int, tuple[str, bool]]:
    """`{line_no: (comment_text, standalone)}` for every real comment token.

    Anything inside a string literal is invisible here, which is the whole
    reason this goes through `tokenize` rather than reading lines. `standalone`
    means the comment is the only thing on its line — a *trailing* comment
    after code may open an appeal but may not continue one, or the next
    unrelated end-of-line note would silently become part of the reason.
    """
    comments: dict[int, tuple[str, bool]] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                standalone = not token.line[: token.start[1]].strip()
                comments[token.start[0]] = (token.string, standalone)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file that does not tokenize has bigger problems than its appeals,
        # and every other gate will say so.
        return comments
    return comments


def scan_file(path: Path, repo_root: Path) -> list[Appeal]:
    """Every `# gate-ok(...)` marker in one Python file, reasons joined.

    Cached per file *version* (`gates.corpus`), because seven gates support
    appeals and each used to tokenize the whole tree to find them — ~5.0s of
    an 8.3s suite spent re-deriving one answer seven times. The cache key
    includes the repo root: `Appeal.file` is repo-relative, so the same file
    scanned as part of two different roots is genuinely two different answers.
    """
    return corpus.cached(
        f"appeals:{repo_root}", path, lambda p: _scan_file_uncached(p, repo_root))


def _scan_file_uncached(path: Path, repo_root: Path) -> list[Appeal]:
    source = corpus.read_text(path)
    if source is None:
        return []
    rel = path.relative_to(repo_root).as_posix()
    comments = _comment_lines(source)
    found: list[Appeal] = []
    for line_no in sorted(comments):
        text, _ = comments[line_no]
        match = _MARKER.search(text)
        if not match:
            continue
        gate, reason = match.group(1), match.group(2).strip()
        # Absorb following comment-only lines as continuation of the reason,
        # stopping at the next marker or at any line that is not a standalone
        # comment.
        cursor = line_no + 1
        while cursor in comments:
            cont_text, standalone = comments[cursor]
            if not standalone or _MARKER.search(cont_text):
                break
            cont = _CONTINUATION.match(cont_text)
            if not cont:
                break
            reason = f"{reason} {cont.group(1).strip()}".strip()
            cursor += 1
        found.append(Appeal(rel, line_no, cursor - 1, gate, reason))
    return found


def _default_roots(repo_root: Path) -> tuple[str, ...]:
    """`.ai/` plus every source/test directory the project's manifest declares.

    Falls back to `.ai/` alone when there is no manifest yet, or it does not
    parse — the safe direction for a scan (fewer files, never a crash), and the
    same fallback `nightshift.preflight.tests_dir` takes for the same reason.
    """
    try:
        from nightshift.manifest import ManifestError, load as load_manifest

        project = load_manifest(repo_root).project
        return (AI_DIR, *project.source_dirs, *project.extra_source_dirs)
    except Exception:
        return (AI_DIR,)


def scan(repo_root: Path, roots: tuple[str, ...] | None = None) -> list[Appeal]:
    """Every appeal marker in the tree, sorted by file then line."""
    found: list[Appeal] = []
    for rel in (roots if roots is not None else _default_roots(repo_root)):
        base = repo_root / rel
        if base.is_dir():
            for path in sorted(base.rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                found.extend(scan_file(path, repo_root))
    return sorted(found, key=lambda a: (a.file, a.line))


def exempt(appeals: list[Appeal], gate: str, file: str, line: int, window: int = 3) -> bool:
    """Is `file:line` covered by an appeal naming `gate`?

    The window is measured from the appeal's *span* -- opening line through the
    last line of its wrapped reason -- not from its opening line. A reason
    worth writing runs to several lines, and measuring from the opening would
    mean the better an author explains themselves the further the marker drifts
    from the code it excuses. Three lines either side of the span is narrow
    enough that it cannot reach a neighbouring statement of any substance.
    """
    return any(
        a.gate == gate
        and a.file == file
        and a.line - window <= line <= a.end_line + window
        for a in appeals
    )
