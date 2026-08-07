"""Gate: a Python string literal that looks like a repo path must resolve to
something real (`hook-parsed-a-module-that-moved`, 2026-08-02, still open in
Dungeoneer's `.ai/corrections.log` until this lands).

`doc_reference_liveness` catches a **doc** naming a file or symbol that no
longer exists. Nothing did the same for **source** — the one form neither the
type checker, the import graph, nor the test suite can follow. Three real
incidents share the same shape: `worktree_fence._fence_env_name` and
`ideas_fence.known_lanes` AST-parsed `.ai/runner.py` and `.ai/board.py` as
paths after both modules had moved into this package, and both fenced
functions fail *open* by design, so the guards they fed simply stopped
arming — no error, no log line, only a test that happened to check. A fourth,
found the same day: `tiers.py` held `ARCHITECTURE =
Path(".claude/plans/ai_team/00_architecture.md")`, a doc that
`07_portability.md` §7 says deliberately does not port, so a second project
installing this package got a `TierError` naming a file that was never going
to exist there.

**Built on the shared AST corpus** (`gates/corpus.py`, so this costs no
second parse) **and on `doc_scan`'s resolution machinery** — the same
git-ls-files-based, framework-package-aware index `doc_reference_liveness`
already trusts, not a second implementation of "does this path exist."
`doc_scan.project_file_paths` is the one addition made there for this gate:
`doc_scan._path_suffixes` only keeps tail suffixes, which cannot answer
whether a *directory-only* claim (`.ai/gates`, `dungeoneer/assets`) is real,
since a suffix set has already thrown away which segment was the front.

**Scope is manifest-driven, never a literal** — `[project].source_dirs` and
`extra_source_dirs`, plus `.ai/` and `[project].tooling_dirs` by reusing
`gates/scope.py`'s `tooling_dirs()`. A gate about stale path literals that
hardcoded its own directory name would be the joke writing itself
(`gate-scope-outlived-its-directory`).

**`[tests].dir` is carved back out of that scope, and this is the one real
finding from reading the raw hit list before settling the heuristic (§ step
2's own instruction).** Scanning both repos' real trees with every other
piece of this gate already in place produced ~350 unresolved candidates
under `tests/` in each and, past that noise, close to zero real ones. Test
modules build synthetic fixture trees on purpose —
`(tmp_path / "Board/tasks/broken.md")`, `"dungeoneer/does_not_exist.py"`
literally named to not exist — and a path literal there names content the
*test* creates or deliberately omits, not content this repository already
has. Asking whether it resolves against `repo_root` is the wrong question,
the same way `doc_scan.py`'s exclusion of `Board/` is: a lane (or here, a
directory) whose entire genre is naming things that are not yet, or are
deliberately never, real.

**Two more things the same run of "read the corpus first" surfaced, both
now handled structurally rather than by marker:**

* **A bare docstring/comment-shaped string statement is not scanned.**
  `nightshift/tiers.py`'s own docstring quotes a 2026-08-02 error message
  verbatim, containing a path that stopped existing on 2026-08-06 — exactly
  the "dated historical quote" shape `08_staleness.md`'s `stale-ok:` covers
  on the doc side. But this project's docstrings are long and discursive by
  house style, and scanning them turned up dozens of sites that were never
  the defect this gate exists to catch: a framework module narrating "here
  is how a *consuming* project would reference this" using Dungeoneer as the
  worked example, or `init.py`/`uninstall.py` describing paths they write
  into *whatever repo installs this package*, never their own. All four
  historical incidents this gate is proving out are *live* literals —  an
  AST-parse target, a `Path(...)` assignment, a subprocess argv — never
  prose about one, so excluding bare string-literal statements costs nothing
  against the evidence and removes a false-positive class an allowlist file
  would otherwise have to carry one entry per site. (This is a real
  divergence from the card's letter — see the dispatch report.)
* **An f-string fragment is reconstructed before matching, not scanned
  fragment-by-fragment.** `.ai/recipes/{recipe}.md` split at the `{recipe}`
  slot leaves the literal half reading `.ai/recipes`, a bare directory that
  (in this repo) does not exist — not because the reference is stale, but
  because it is a *parameterised* path, Python's own `{{project}}`. A match
  that contains a slot, **or runs straight into one**, is dropped for exactly
  the reason `nightshift/templates/`'s token paths are exempt category 2 —
  and the second half of that is not a refinement but the whole case: a
  pattern stops *at* the sentinel, so `.ai/recipes/{recipe}.md` would
  otherwise be reported as the bare directory `.ai/recipes`. Only the right
  side is checked. A claim preceded by a slot — `f"{worktree}/.ai/gates/run.py"`
  — is fully known from its first real segment on, and is one of the four
  instances this gate must catch.

**A `Path(...) / "seg" / "seg"` chain is reconstructed the same way, and it is
the gate's first known blind spot** (`path-literals-split-across-segments`,
2026-08-06): `branch_role_prose._DOCS` held `Path(".claude") / "plans" /
"ai_team" / "SESSIONS.md"` months after that doc moved, and this gate — built
the same day for exactly this failure mode — could not see it, because no
single string constant in the chain contains a `/`. `_div_chain_leaves` walks
the left-associative `BinOp(op=Div)` spine leaves-first; `_leaf_literal` reads
a leaf as a literal segment if it is a bare string constant or a `Path(...)`
call wrapping exactly one (the leading leaf's usual shape), and anything else
— `Path(root)`, a `Name`, an f-string leaf — becomes a `_SENTINEL` slot,
visited for its own literals, exactly like an f-string's formatted value. The
segments are then joined with `/` and handed to the same `_extract`, so a
dynamic leading segment (`Path(AI_DIR) / "gates"` in `audit.py`) inherits the
existing sentinel-adjacency rule and drops out the same way
`f"{worktree}/.ai/gates/run.py"` already does — no separate case needed.

**The raw hit list against both repos' real trees (step 2 of that card, read
before the rule was settled) surfaced eight `Path(...) / "seg"` chains in
each repo. Six of the sixteen did not resolve outright; two of those six were
already covered by the gitignore mechanism below, leaving four real gate
violations, all in this repo** — not the zero a narrower rule would have made
convenient to assume. Reading them changed two things this docstring would
otherwise get wrong by omission:

* `branch_role_prose._DOCS` (the incident this card is about) reconstructs to
  **two** unresolved candidates, not one — both the stale
  `.claude/plans/ai_team/SESSIONS.md` *and* the live
  `.claude/memory/ai_team/SESSIONS.md`, because self-gating this repo can
  never confirm either: the doc they name lives only in a consuming project
  (Dungeoneer), never in nightshift's own tree, and `branch_role_prose.check()`
  already resolves the real question — which home the checked project has —
  at runtime. Appealed in place, both lines, with that reasoning; narrowing
  the gate to somehow accept one and not the other would have meant
  special-casing the one shape this card exists to catch.
* `stale_sweep.LEDGER` (`.ai/stale_ledger.json`) reconstructed to an
  unresolved candidate because its own docstring's claim — "gitignored,
  rebuildable" — was not true of this checkout's `.gitignore`. Fixed by adding
  the missing line, which is the honest fix: the structural gitignore
  resolution below now covers it, exactly as the docstring already claimed it
  would. `stale_sweep.STATUS` (`.ai/stale_status.json`) is the opposite case —
  meant to be committed, but only once a `--stale` sweep has actually run
  against this repo and produced it, which has not happened yet; appealed,
  since `read_status()`'s own contract already treats that absence as "never
  run" and fabricating a stub file would have been the invented value this
  process exists to refuse.

**Runtime-created paths** (`.ai/runs/`, `.ai/STOP`, `stale_ledger.json`) are
recognised structurally too: a candidate that `git check-ignore` says this
repo's own `.gitignore` declares is treated as resolved. A gitignored path is
not an accident of "nothing exists there yet" — someone wrote the pattern on
purpose, which is the same distinction `08_staleness.md`'s "was this ever
tracked" test draws for docs.

What is left after all three structural passes is genuine debt, appealed the
way every other AST gate here is: `# gate-ok(source_reference_liveness):
<reason>`, `appeal_markers.py`, counted by `gate_appeals`. No per-path
allowlist file — `08_staleness.md` made that call for the doc side and the
reasoning carries unchanged: an allowlist accumulates entries nobody
re-reads, which is the defect this gate is about.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import appeal_markers
from nightshift import manifest as _manifest
from nightshift.gates import corpus
from nightshift.gates import doc_scan
from nightshift.gates import scope as _scope
from nightshift.gates.base import Violation
from nightshift.manifest import AI_DIR

NAME = "source_reference_liveness"
FAST = False  # builds the same whole-tree file index doc_reference_liveness does
DESCRIPTION = "a source string literal that looks like a repo path must resolve to something real"

# Extensions a source literal plausibly names. Deliberately a superset of
# `doc_scan._PATH_RE`'s list: source references run logs (`.jsonl`, `.log`),
# config (`.ini`, `.yaml`, `.yml`) and web assets docs never cite.
_EXTENSIONS = (
    "py", "md", "json", "jsonl", "toml", "cfg", "ini", "yaml", "yml",
    "txt", "log", "html", "css", "js", "ts",
    "png", "jpg", "jpeg", "gif", "mp3", "ogg", "wav",
)
_EXT_ALT = "|".join(_EXTENSIONS)

# A slash-separated, extension-terminated run. `[\w.-]+` per segment (dots for
# hidden dirs and multi-part names like `settings.hooks.json`), at least one
# `/`, and the whole thing bounded so it cannot start or end mid-word.
_PATH_EXT_RE = re.compile(
    rf"(?<![\w./-])(?:[\w.-]+/)+[\w.-]+\.(?:{_EXT_ALT})(?![\w])"
)

_SENTINEL = "\x00"  # stands in for a JoinedStr's dynamic slots; never a real path char


def _root_segment_re(root_names: tuple[str, ...]) -> re.Pattern[str]:
    """A slash-separated run that *starts* with one of the manifest's declared
    root names (longest first, so `.ai` never shadows a longer sibling) —
    the "starts with a known repo-root segment" half of the card's heuristic,
    for directory-only claims an extension would miss (`.ai/gates`,
    `dungeoneer/assets`)."""
    roots = sorted({*root_names, AI_DIR}, key=len, reverse=True)
    alt = "|".join(re.escape(r) for r in roots)
    return re.compile(rf"(?<![\w./-])(?:{alt})(?:/[\w.-]+)+(?![\w])")


def _root_names(repo_root: Path) -> tuple[str, ...]:
    try:
        project = _manifest.load(repo_root).project
    except _manifest.ManifestError:
        project = _manifest.Project()
    return tuple(dict.fromkeys(
        (*project.source_dirs, *project.extra_source_dirs, *project.tooling_dirs)))


def _scan_dirs(repo_root: Path) -> list[Path]:
    """Source + extra source dirs (the configured test directory removed —
    see the module docstring), unioned with `.ai/` + `[project].tooling_dirs`
    via `scope.tooling_dirs`, which is the literal reuse the card asks for."""
    try:
        m = _manifest.load(repo_root)
        project, tests_dir = m.project, m.tests.dir
    except _manifest.ManifestError:
        project, tests_dir = _manifest.Project(), _manifest.Tests().dir
    names = dict.fromkeys((*project.source_dirs, *project.extra_source_dirs))
    names.pop(tests_dir, None)
    dirs = [repo_root / n for n in names if (repo_root / n).is_dir()]
    for d in _scope.tooling_dirs(repo_root):
        if d not in dirs:
            dirs.append(d)
    return dirs


def _scan_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for d in _scan_dirs(repo_root):
        for path in sorted(d.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _truncated_by_slot(text: str, end: int) -> bool:
    """Does the claim ending at `end` run straight into a formatted-value slot?

    `f".ai/recipes/{recipe}.md"` reconstructs as `.ai/recipes/<sentinel>.md`, and
    the root-segment pattern stops at the sentinel with `.ai/recipes` in hand — a
    match that contains no sentinel and would otherwise be reported as a bare
    directory claim. It is not one: the path continues into a slot whose value
    this gate cannot know, which is exactly `templates/`' `{{project}}` case.

    Only the right-hand side is checked. A claim *preceded* by a slot —
    `f"{worktree}/.ai/gates/run.py"` — is fully known from its first real segment
    on, and is one of the four historical instances this gate must catch.
    """
    rest = text[end:]
    if rest.startswith("/"):
        rest = rest[1:]
    return rest.startswith(_SENTINEL)


def _extract(text: str, root_re: re.Pattern[str]) -> set[str]:
    """Path-shaped substrings of `text`, matches that contain or run into a
    formatted-value slot dropped (a parameterised path, not a concrete claim),
    and trailing sentence punctuation trimmed ("...at .ai/corrections.log." is
    not part of the path; a segment charset permissive enough for hidden dirs
    and multi-dot filenames cannot also exclude a trailing period up front)."""
    found: set[str] = set()
    for pattern in (_PATH_EXT_RE, root_re):
        for match in pattern.finditer(text):
            token = match.group(0)
            if _SENTINEL in token or _truncated_by_slot(text, match.end()):
                continue
            found.add(token.rstrip(".-"))
    return found


def _div_chain_leaves(node: ast.BinOp) -> list[ast.AST]:
    """Flatten a left-associative `a / b / c` `BinOp(op=Div)` spine into its
    leaves, left to right — `Path(".claude") / "plans" / "ai_team" /
    "SESSIONS.md"` parses as `((Path(".claude") / "plans") / "ai_team") /
    "SESSIONS.md"`, so the leaves are read off by walking the left spine."""
    leaves: list[ast.AST] = []

    def walk(n: ast.AST) -> None:
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            walk(n.left)
            leaves.append(n.right)
        else:
            leaves.append(n)

    walk(node)
    return leaves


def _leaf_literal(node: ast.AST) -> str | None:
    """The literal path segment one `/`-chain leaf contributes, or `None` if
    the leaf is not a literal. Two shapes: a bare string constant (`"plans"`),
    or a `Path(...)` call wrapping exactly one string constant — the leading
    leaf of every chain in house style (`Path(".claude") / ...`). Anything
    else (`Path(root)`, a Name, an f-string) is not a literal segment; the
    caller treats it as a dynamic slot, the same as an f-string's formatted
    value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == "Path"
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def _collect(tree: ast.AST, root_re: re.Pattern[str]) -> list[tuple[int, str]]:
    """`(lineno, candidate)` for every path-shaped string in `tree`, skipping
    bare docstring/comment-shaped statements and reconstructing f-strings and
    `Path(...) / "seg" / "seg"` chains before matching — see the module
    docstring for why both matter."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            return  # a bare string-literal statement: a docstring or its kin
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append(_SENTINEL)
                    visit(value)  # the dynamic slot may itself hold real literals
            for cand in _extract("".join(parts), root_re):
                found.append((node.lineno, cand))
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = []
            for leaf in _div_chain_leaves(node):
                literal = _leaf_literal(leaf)
                if literal is not None:
                    parts.append(literal)
                else:
                    parts.append(_SENTINEL)
                    visit(leaf)  # the dynamic segment may itself hold real literals
            for cand in _extract("/".join(parts), root_re):
                found.append((node.lineno, cand))
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for cand in _extract(node.value, root_re):
                found.append((node.lineno, cand))
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _gitignored(repo_root: Path, tokens: list[str]) -> set[str]:
    """Which of `tokens` this repo's own `.gitignore` declares — a batched
    `git check-ignore`, one process for every candidate rather than one per.

    Bytes in, bytes out. `subprocess.run(..., text=True)` translates `\\n` to
    `\\r\\n` when *writing* stdin on Windows, so a candidate that should read
    as `.ai/runs` arrives at git as `.ai/runs\\r` and silently fails to
    match — the same class of bug `write_newline`/`subprocess_encoding` exist
    to catch elsewhere, found here by running this gate against a real
    Windows checkout rather than assuming the call was safe.

    **Each candidate is probed both as written and as a directory**, because a
    `.gitignore` entry with a trailing slash matches directories *only*, and
    `git check-ignore` cannot tell that a path is a directory when nothing
    exists at it. That is the normal case here rather than an edge one: the
    whole reason a path reaches this function is that it is created at runtime,
    so it is absent from a clean checkout by definition. Without the second
    probe, `.ai/runs` fails to match this repo's own `.ai/runs/` pattern — which
    is how the module docstring came to name `.ai/runs/` as *the* structural
    case while the gate reported ten violations on it, in `runner.py`,
    `digest.py` and `hooks/correction_prompt.py`. `git check-ignore .ai/runs`
    says no; `git check-ignore '.ai/runs/'` says yes.
    """
    if not tokens:
        return set()
    # Maps every probe back to the candidate it came from, so a hit on the
    # directory form still answers for the token as the source actually wrote it.
    probes: dict[str, str] = {}
    for token in tokens:
        probes.setdefault(token, token)
        if not token.endswith("/"):
            probes.setdefault(token + "/", token)
    payload = ("\n".join(probes) + "\n").encode("utf-8")
    try:
        proc = subprocess.run(
            # `--non-matching`, not its `-n` short form: `pytest_invocation` reads a
            # bare `-n` in any argv as pytest's parallel flag, and it is right to —
            # the long form costs nothing and leaves no appeal to re-read later.
            ["git", "-C", str(repo_root), "check-ignore", "--stdin", "-v",
             "--non-matching"],
            input=payload, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    text = proc.stdout.decode("utf-8", errors="replace")
    ignored: set[str] = set()
    for line in text.splitlines():
        source, sep, path = line.partition("\t")
        if sep and not source.startswith("::"):
            probed = path.strip('"')
            if probed in probes:
                ignored.add(probes[probed])
    return ignored


def _resolves(repo_root: Path, token: str, doc: Path, full_paths: frozenset[str]) -> bool:
    if doc_scan.resolve_path(repo_root, token, doc=doc):
        return True
    prefix = token.rstrip("/") + "/"
    return any(p.startswith(prefix) for p in full_paths)


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    root_re = _root_segment_re(_root_names(repo_root))
    full_paths = doc_scan.project_file_paths(str(repo_root))

    candidates: list[tuple[Path, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    for path in _scan_files(repo_root):
        tree = corpus.tree(path)
        if tree is None:
            continue
        rel = path.relative_to(repo_root).as_posix()
        for lineno, cand in _collect(tree, root_re):
            key = (rel, lineno, cand)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((path, lineno, cand))

    unresolved = [
        (path, lineno, cand) for path, lineno, cand in candidates
        if not _resolves(repo_root, cand, path, full_paths)
    ]
    ignored = _gitignored(repo_root, [c for _, _, c in unresolved])

    violations: list[Violation] = []
    for path, lineno, cand in unresolved:
        if cand in ignored:
            continue
        rel = path.relative_to(repo_root).as_posix()
        if appeal_markers.exempt(appeals, NAME, rel, lineno):
            continue
        violations.append(Violation(
            rel, lineno,
            f"source_reference_liveness: {cand!r} looks like a repo path but resolves "
            f"to nothing. If the target moved or was deleted, fix the reference; if it "
            f"names a dated quote, a template/token path, or something created at "
            f"runtime, appeal with `# gate-ok({NAME}): <reason>`",
        ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
