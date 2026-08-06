"""Shared doc-scanning machinery for the staleness gates (08_staleness.md §3).

Three things live here because `doc_reference_liveness`, `doc_signature_drift`
and `deletion_sweep` all need them and must agree on them exactly:

  1. **The doc scope** — which files the staleness gates look at.
  2. **The exemption mechanism** — `doc_scope:` frontmatter and
     `<!-- stale-ok: reason -->` markers. This is the design problem §3.1 calls
     out as the real one: history is *supposed* to name dead things, and a gate
     that flags `state_history.md`'s correct "hack_scene.py deleted" line gets
     muted within a week. A per-symbol allowlist is explicitly rejected there —
     it grows monotonically and encodes no reason.
  3. **Reference extraction** — pulling paths and backticked symbols out of
     markdown, and resolving them against the source tree.

No LLM calls anywhere in this directory (00_architecture.md §12).
"""
from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from nightshift import manifest as _manifest
from nightshift.gates import corpus
from nightshift.manifest import AI_DIR, ManifestError

# --- 1. Scope -------------------------------------------------------------

# 08_staleness.md §3.1. Directories are scanned recursively for *.md.
DOC_ROOTS: tuple[str, ...] = (
    ".claude/memory",
    ".ai/recipes",  # gate-ok(source_reference_liveness): scanned in a CONSUMING project's
    # tree at check time; this framework's own checkout has no recipes of its own to scan.
    ".claude/agents",
    ".claude/plans",
)
# `Board/` is NOT a doc root. **A card is a plan or a record, never a description
# of the tree as it stands** — so asking a staleness gate whether its references
# resolve is asking the wrong question of the wrong document. `card_schema` is the
# gate that governs cards; this family does not.
#
# That conclusion was reached the expensive way, one lane at a time, and the list of
# exclusions grew three times before the premise was questioned:
#
# * `ideas/` (intake) — Karel's private lane; no script may enumerate it.
# * `inbox/` (intake) — rough notes name things that do not exist yet. That is what
#   makes them notes.
# * `review/`, `testing/`, `failed/` — added 2026-07-23, after the runner's first
#   real night: `board-parser-convergence` succeeded, moved to `review/`, and the
#   integration branch's gates went red on five symbols its own card names. Nothing
#   was wrong — the card described work on an unmerged branch. A card *succeeding*
#   broke the gates for every card dispatched after it.
#
# `tasks/`, `needs-decision/` and `done/` were kept in scope on the theory that those
# cards "describe the tree as it is today, and `stale-ok:` covers the deliberate
# forward reference". Both halves turned out to be wrong:
#
# 1. A card in `tasks/` describes work **not yet done**. Its `## Approach` names what
#   *will* exist — that is the entire genre. On 2026-08-01 every one of 19 live
#   violations was of this kind (`nightshift/doctor.py`, `next_level_desc`,
#   `Item.is_notable`, a `Board/` path whose card had been dragged to another lane).
#   Not one was a staleness defect. A `done/` card is the same problem in the past
#   tense: it narrates what happened, quoting names that have since moved.
# 2. `stale-ok:` did not "cover" it, it *taxed* it. `runner.py::_EVIDENCE_EXEMPTION`
#   exists solely to auto-inject a marker into every failed card, because otherwise
#   one card's pytest excerpt turns into a repo-wide violation and the next card in
#   the night is filed failed for someone else's bug. `Board/done/
#   runner-internals-arity-guard.md` carries two hand-typed markers for names the
#   card itself proposed. The mechanism was being used to suppress a category of
#   false positive, which is the signal that the scope was wrong rather than the
#   cards.
#
# So the root is gone rather than the exclusion list extended. A list that must name
# every lane is exactly the shape `writer-list-was-incomplete-by-construction`
# records — a new lane would silently re-enter scope, and nothing would say so.
DOC_EXCLUDE: tuple[str, ...] = ()


def doc_file_names(repo_root: Path) -> tuple[str, ...]:
    """Individual docs in scope that are not under a `DOC_ROOTS` directory —
    `project.doc_files`, which defaults to `CLAUDE.md`.

    A manifest read since 07_portability.md §8 step 4; it was the literal
    `("CLAUDE.md",)` before, which is exactly what the schema default still
    gives a project that declares nothing.
    """
    try:
        return _manifest.load(repo_root).project.doc_files
    except ManifestError:
        return _manifest.Project().doc_files

# Root-level entry points are included because docs reference `main.py`
# constantly. Deliberately a glob rather than a name, so a second entry point
# needs no gate change.
SOURCE_GLOBS: tuple[str, ...] = ("*.py",)


def source_dirs(repo_root: Path) -> tuple[str, ...]:
    """Where the symbol table is built from: the project's own code, plus the
    directories that are scanned for symbols without being part of the code
    slice (`extra_source_dirs` — `tests/`, `tools/`), plus `.ai/`.

    `.ai` is always included and is not a manifest field: the docs in scope
    document the gate suite itself (`doc_scope`, `deletion_sweep`, `_RULES`),
    and without it every doc describing this tooling reports its own subject as
    dangling. That is a framework fact, true of any consuming project.

    A manifest read since 07_portability.md §8 step 4. For Dungeoneer the result
    is `("dungeoneer", "tests", "tools", ".ai")` — byte-identical to the tuple
    this replaced, which is the property step 4's checklist asks for.
    """
    try:
        project = _manifest.load(repo_root).project
    except ManifestError:
        return (AI_DIR,)
    return (*project.source_dirs, *project.extra_source_dirs, AI_DIR)

# The AI-team framework itself, which as of 07_portability.md §8 step 2 lives in
# an installed package (`nightshift`) rather than in `.ai/`. Docs in scope document
# that tooling — `python -m nightshift.preflight`, `nightshift/gates/run.py`,
# `write_text_lf` — and every one of those references became dangling the moment
# the modules moved out of this tree, despite naming something that very much
# exists and that this project imports by name.
#
# Indexed as a source root and as a path prefix, so `nightshift/preflight.py` resolves
# the same way a module under one of the project's own source roots does.
#
# **This is not the `sources/` bug in a new coat** (see `_path_suffixes`). That bug
# was an *ignored, machine-local* directory silently satisfying references; this is
# a declared dependency that the project cannot run without, so its absence means
# the checkout is broken rather than that the doc is stale. And the failure
# direction is the safe one: if the package is missing, references go red — the
# gate never turns green on something a clean checkout would not have.
FRAMEWORK_PACKAGE = "nightshift"


def doc_files(repo_root: Path) -> list[Path]:
    """Every markdown file in the staleness scope, sorted, deduplicated."""
    found: list[Path] = []
    excluded = tuple((repo_root / rel).resolve() for rel in DOC_EXCLUDE)
    for rel in DOC_ROOTS:
        root = repo_root / rel
        if root.is_dir():
            found.extend(
                path
                for path in sorted(root.rglob("*.md"))
                if not any(path.resolve().is_relative_to(base) for base in excluded)
            )
    for rel in doc_file_names(repo_root):
        path = repo_root / rel
        if path.is_file():
            found.append(path)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


# --- 2. Exemptions --------------------------------------------------------

_FRONTMATTER_SCOPE = re.compile(r"^doc_scope:\s*(\w+)\s*$", re.MULTILINE)
# The marker may wrap across lines — reasons are prose and a one-line cap would
# push authors toward terse, useless reasons, which defeats the point of making
# the reason mandatory. `_STALE_OK_OPEN` starts it, `-->` on that or any later
# line closes it; coverage then runs to the next heading.
_STALE_OK_OPEN = re.compile(r"<!--\s*stale-ok:\s*(\S.*)?$")
_STALE_OK_END = re.compile(r"<!--\s*/stale-ok\s*-->")
# Docs that explain the exemption mechanism write `<!-- stale-ok: reason -->`
# inside a code span. That is prose about the syntax, not an invocation of it —
# and treating it as one both inflates the debt count and silently exempts the
# rest of the section. Code spans are stripped before marker detection.
_CODE_SPAN = re.compile(r"`[^`\n]*`")


_HEADING = re.compile(r"^#{1,6}\s")


def _strip_code_spans(line: str) -> str:
    return _CODE_SPAN.sub("", line)

# Scopes skipped wholesale. `current` is the default when no frontmatter says
# otherwise — the safe direction, per §3.1.
#
#   history  — dated narrative. Naming dead things is its job.
#   external — the doc's SUBJECT is a toolchain outside this repo (a generator
#              stack on another drive, an unversioned scratch directory,
#              third-party library internals). Not a loophole for stale claims
#              about the project's own code: it says "resolving these names
#              against our source roots is a category error", which is true of
#              the whole file or it is true of none of it. Prefer per-section
#              `<!-- stale-ok: -->` markers whenever a doc is only partly external.
EXEMPT_SCOPES = frozenset({"history", "external"})


def read_scope(text: str) -> str:
    """`doc_scope:` from YAML frontmatter, defaulting to 'current'."""
    if not text.startswith("---"):
        return "current"
    end = text.find("\n---", 3)
    if end == -1:
        return "current"
    match = _FRONTMATTER_SCOPE.search(text[:end])
    return match.group(1) if match else "current"


def is_exempt_file(text: str) -> bool:
    return read_scope(text) in EXEMPT_SCOPES


def exempt_lines(text: str) -> set[int]:
    """1-based line numbers covered by a `<!-- stale-ok: reason -->` marker.

    A marker exempts from its own line until the next markdown heading, or
    until an explicit `<!-- /stale-ok -->`, whichever comes first. Heading-
    bounded because that is the unit a doc is actually written in — a marker
    that ran to end-of-file would silently swallow later sections.

    One exception, because it is where authors actually put the marker: a
    marker written *immediately above* a heading claims that heading and the
    section beneath it. Without this the natural placement exempts exactly one
    line — the marker itself — and appears to do nothing.

    The reason text is mandatory by regex: `<!-- stale-ok -->` with nothing
    after the colon does not match and exempts nothing. The marker may wrap
    over several lines; `-->` closes it.
    """
    covered: set[int] = set()
    active = False
    in_marker = False
    just_opened = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = _strip_code_spans(raw)
        if in_marker:
            covered.add(lineno)
            if "-->" in line:
                in_marker = False
                just_opened = True
            continue
        if _STALE_OK_OPEN.search(line):
            active = True
            covered.add(lineno)
            in_marker = "-->" not in line
            just_opened = not in_marker
            continue
        # A marker written immediately above a heading is claiming that heading
        # and the section under it — that is where authors naturally put it, and
        # ending coverage on the heading would exempt nothing at all.
        if active and just_opened and _HEADING.match(line):
            covered.add(lineno)
            just_opened = False
            continue
        just_opened = False
        if active and (_STALE_OK_END.search(line) or _HEADING.match(line)):
            active = False
            if _STALE_OK_END.search(line):
                covered.add(lineno)
            continue
        if active:
            covered.add(lineno)
    return covered


def stale_ok_markers(repo_root: Path) -> list[tuple[str, int, str]]:
    """(relative path, line, reason) for every marker in scope — the debt count."""
    out: list[tuple[str, int, str]] = []
    for path in doc_files(repo_root):
        text = _read(path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _STALE_OK_OPEN.search(_strip_code_spans(line))
            if match:
                out.append((relpath(path, repo_root), lineno, (match.group(1) or "").strip()))
    return out


def exempt_files(repo_root: Path) -> list[tuple[str, str]]:
    """(relative path, scope) for every wholesale-skipped doc — the other half
    of the debt count."""
    out: list[tuple[str, str]] = []
    for path in doc_files(repo_root):
        scope = read_scope(_read(path))
        if scope in EXEMPT_SCOPES:
            out.append((relpath(path, repo_root), scope))
    return out


# --- 3. Reference extraction ---------------------------------------------

def relpath(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Reference:
    kind: str  # "path" | "symbol"
    text: str
    file: str
    line: int
    alts: tuple[str, ...] = ()  # equally-valid readings; resolving any one is enough


def _suffix_continuations(token: str, earlier: list[str]) -> tuple[str, ...]:
    """Expand the house shorthand `FOO_BAR_ONE`/`_TWO`, where the second
    backtick carries only the differing tail.

    Real examples in this tree: ``pending_crit_chain_ranged`/`_melee``,
    ``BLUEPRINT_RIFLE_PROTO_AIM_ZONE_PENALTY`/`_CRIT_BONUS`/`_AMMO_CAPACITY``,
    ``TIGER_FEED_HUNT` or `_WARD``. Read literally, `_melee` is a dangling
    symbol; read as the author meant it, it is `pending_crit_chain_melee`,
    which is live.

    Every underscore-boundary prefix of each earlier token on the same line is
    tried. This cannot mask a real deletion — the joined name still has to
    exist in the source before the reference is accepted.
    """
    if not token.startswith("_") or not earlier:
        return ()
    out: list[str] = []
    for prev in earlier:
        parts = prev.split("_")
        for i in range(1, len(parts) + 1):
            out.append("_".join(parts[:i]) + token)
    return tuple(dict.fromkeys(out))


# A path: contains a '/' and ends in a known extension, or is a bare *.py /
# *.md filename. Trailing ':123' line anchors and punctuation are stripped by
# the caller. Deliberately narrow — matching every slash-containing token
# would flag prose like "and/or".
_PATH_RE = re.compile(
    r"(?<![\w./-])"
    r"(?:[\w.-]+/)*[\w.-]+\.(?:py|md|json|png|mp3|ogg|wav|txt|cfg|toml)"
    r"(?![\w])"
)

# A backticked span. Symbols are only harvested from inside backticks (§3.1) —
# harvesting identifiers from prose would flag every capitalised English word.
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# What counts as a code-shaped token inside backticks: an identifier or a
# dotted chain, optionally with a trailing () call.
_SYMBOL_RE = re.compile(r"^_*[A-Za-z][\w]*(?:\.[A-Za-z_]\w*)*(?:\(\))?$")

# ...and then narrowed to §3.1's own three examples — `ClassName`,
# `function_name()`, `module.attr`. A bare all-lowercase word with no
# underscore, dot, uppercase or call parens is not distinguishable from
# backticked prose: docs backtick `worker`, `recipe`, `prose`, `judgment`,
# `null`, `rm`, `español` constantly. Flagging those is how a gate earns its
# mute (§3.1). The cost is real and worth naming: a lowercase single-word
# identifier such as a perk id (`grenadier`) is invisible to this gate. That
# is a known false-negative, not an oversight — `deletion_sweep` (§3.4) is the
# gate that catches those, at the moment the name dies.
_CODE_SHAPED = re.compile(r"[A-Z_.]|\(\)$")


def is_code_shaped(token: str) -> bool:
    return bool(_CODE_SHAPED.search(token))

# Shapes that are never project symbols. These are *shape* rules with stated
# reasons, not a list of names that happen to be dead — see §3.1.
_NOT_A_SYMBOL = frozenset(
    {
        # language/stdlib keywords and builtins that appear in backticks constantly
        "None", "True", "False", "int", "float", "str", "bool", "list", "dict",
        "set", "tuple", "bytes", "object", "type", "len", "range", "print",
        "open", "self", "cls", "super", "Enum", "Optional", "Callable", "Any",
        "List", "Dict", "Set", "Tuple", "Union", "dataclass", "property",
        "staticmethod", "classmethod", "abstractmethod", "TYPE_CHECKING",
        # git/tooling nouns. Deliberately NOT sourced from `.ai/branches.py`,
        # unlike every other branch name in this tree: that file names the
        # branch that currently holds a role, and this is a list of words docs
        # are allowed to say. A retired branch is still mentioned in history
        # docs forever, so this set must stay a superset and only ever grow.
        "git", "main", "dev", "pytest", "python", "pygame", "HEAD", "origin",
        "development_team",
    }
)

# Roots of names that belong to somebody else's code. The gate's remit is
# "does this project still contain what the doc says it does" — it cannot and
# should not resolve stdlib or third-party attributes (`json.dump`,
# `scipy.ndimage.label`). This is a set of *foreign namespaces*, which is a
# bounded shape rule; it is not the per-project-symbol allowlist §3.1 rejects,
# and nothing ever deleted from the project's own source roots can hide behind it.
_EXTERNAL_ROOTS = frozenset(
    {
        "json", "os", "sys", "re", "math", "random", "pathlib", "subprocess",
        "shutil", "time", "datetime", "enum", "typing", "dataclasses", "ast",
        "itertools", "collections", "functools", "logging", "argparse",
        "unittest", "importlib", "inspect", "textwrap", "traceback", "copy", "pickle",
        "scipy", "numpy", "np", "PIL", "Image", "torch", "requests",
        "pygame", "pytest",
    }
)

# Claude Code harness identifiers — hook event names, env vars, hook-payload
# fields. Same rationale as _EXTERNAL_ROOTS: the planning docs describe the
# harness this project runs inside, and the gate has no business asserting
# that somebody else's API exists in `dungeoneer/`.
_HARNESS_NAMES = frozenset(
    {
        "PreToolUse", "PostToolUse", "SessionStart", "SessionEnd", "UserPromptSubmit",
        "PreCompact", "SubagentStop", "session_id", "tool_input", "tool_response",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL", "SDL_VIDEODRIVER", "SDL_AUDIODRIVER",
    }
)

# `[label](target)` — the label is prose and must not be scanned for paths,
# or "see [Meziherní menu.md](…)" yields a phantom `menu.md` reference.
_MD_LINK_LABEL = re.compile(r"\[([^\]\n]*)\]\(")


def extract_references(path: Path, repo_root: Path) -> list[Reference]:
    """Paths (anywhere) and code-shaped backticked symbols, minus exempt lines."""
    text = _read(path)
    if is_exempt_file(text):
        return []
    skip = exempt_lines(text)
    rel = relpath(path, repo_root)

    refs: list[Reference] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if lineno in skip:
            continue
        for match in _PATH_RE.finditer(_MD_LINK_LABEL.sub("(", line)):
            refs.append(Reference("path", match.group(0), rel, lineno))
        earlier: list[str] = []
        for span in _BACKTICK_RE.finditer(line):
            token = span.group(1).strip()
            if _PATH_RE.fullmatch(token):
                continue  # already captured as a path
            token = token.split(":")[0].strip()
            if not _SYMBOL_RE.match(token) or not is_code_shaped(token):
                continue
            bare = token[:-2] if token.endswith("()") else token
            if (
                bare in _NOT_A_SYMBOL
                or bare in _HARNESS_NAMES
                or bare.split(".")[0] in _EXTERNAL_ROOTS
            ):
                continue
            refs.append(
                Reference("symbol", bare, rel, lineno, _suffix_continuations(bare, earlier))
            )
            earlier.append(bare)
    return refs


# --- 4. Resolution --------------------------------------------------------


@lru_cache(maxsize=1)
def _framework_root() -> Path | None:
    """Where the installed `nightshift` package sits, or None if it is not installed.

    Located through the import system rather than a relative path, because the
    package may be an editable install pointing at a sibling checkout, a wheel in
    site-packages, or a virtualenv — and the doc references are the same in all
    three cases.
    """
    try:
        spec = importlib.util.find_spec(FRAMEWORK_PACKAGE)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).resolve().parent


def _framework_files() -> list[Path]:
    root = _framework_root()
    if root is None:
        return []
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _source_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in source_dirs(repo_root):
        root = repo_root / rel
        if root.is_dir():
            files.extend(
                p for p in root.rglob("*.py") if "__pycache__" not in p.parts
            )
    for pattern in SOURCE_GLOBS:
        files.extend(sorted(repo_root.glob(pattern)))
    files.extend(_framework_files())
    return files


@lru_cache(maxsize=4)
def _index(repo_root_str: str) -> tuple[frozenset[str], frozenset[str]]:
    """(defined names, every token/string-literal seen) over the source tree.

    Two tiers on purpose:

      * **defined** — AST `class`/`def` names (at any nesting depth), assignment
        targets, and module basenames. This is the "does this thing exist"
        table §3.1 asks for.
      * **seen** — every identifier token and every string-literal value in the
        source. A doc may legitimately backtick something that is not a
        definition: an i18n key (`hack.footer.hint` — a dict key, not a symbol),
        an attribute set on another object, a catalog id. Requiring those to be
        AST definitions would produce a wall of false positives, and a noisy
        gate gets muted (§3.1). Failing *both* tiers is the honest signal that
        a name has no referent anywhere in the code.
    """
    repo_root = Path(repo_root_str)
    defined: set[str] = set()
    seen: set[str] = set()
    token_re = re.compile(r"[A-Za-z_]\w*")

    for path in _source_files(repo_root):
        defined.add(path.stem)
        defined.add(path.name)
        source = corpus.read_text(path)
        if source is None:
            continue
        for token in token_re.findall(source):
            seen.add(token)
        tree = corpus.tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.Attribute):
                seen.add(node.attr)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                seen.add(node.value)
                seen.update(token_re.findall(node.value))
    return frozenset(defined), frozenset(seen)


def symbol_table(repo_root: Path) -> frozenset[str]:
    return _index(str(repo_root))[0]


def resolve_symbol(repo_root: Path, token: str) -> bool:
    defined, seen = _index(str(repo_root))
    if token in defined or token in seen:
        return True
    # Docs routinely backtick a file by its stem (`feedback_ui_close`,
    # `add-a-perk`) — that is a live reference to a file that exists.
    if token in file_stems(repo_root):
        return True
    parts = token.split(".")
    if len(parts) > 1:
        # A dotted chain resolves if every component does; report the whole
        # chain as dangling otherwise, since naming the guilty component
        # requires knowing which object the attribute hangs off.
        return all(p in defined or p in seen for p in parts)
    return False


_IGNORED_TREE_PARTS = frozenset({".git", "__pycache__", ".tmp", "node_modules"})
# Path *prefixes* to skip outright, distinct from `_IGNORED_TREE_PARTS` above
# (which matches a bare component anywhere in the path — too broad for a
# two-segment prefix like this one). `.ai/runs/` is the runner's own gitignored
# working state: worker transcripts, gate logs, one JSON per attempt.
#
# Found 2026-07-24, the same night `merge_check.py` was built to catch exactly
# this shape of problem. A card correctly names `.ai/runs/<id>/attempt-N/…`
# paths — `runner-stream-worker-output.md` names `prompt-1.md`, `stream.jsonl`,
# `worker-N.json` — and those resolved anyway, not through the `stale-ok`
# exemption meant for them, but because the reviewer's own machine happened to
# have run those exact cards and the files were sitting on disk. That makes
# `doc_reference_liveness` **non-reproducible**: green here, red on a fresh
# clone, red in a CI checkout, red in the clean worktree `merge_check.py` builds
# to check a branch honestly — which is precisely how it surfaced.
_IGNORED_TREE_PREFIXES: tuple[tuple[str, ...], ...] = ((".ai", "runs"),)


def _project_files(repo_root: Path) -> list[tuple[str, ...]] | None:
    """Git's own view of the project, as relative path-part tuples. None if git
    cannot answer (not a repo, no git binary).

    **Tracked plus untracked-but-not-ignored** — i.e. exactly the files `git
    status` would talk about. Tracked alone would flag a doc that names a file
    the author created but has not `git add`ed yet, which is a real workflow and
    not staleness. Ignored files are excluded, and that is the whole point:
    see `_path_suffixes`.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            capture_output=True, text=True, timeout=60,
            # Explicit, not the locale codec: a filename byte cp1252 cannot map
            # would otherwise make .stdout None inside the reader thread rather
            # than raise, and this function would report "git cannot answer" and
            # silently fall back to the disk walk it exists to replace.
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [tuple(rel.split("/")) for rel in out.stdout.split("\0") if rel]


@lru_cache(maxsize=4)
def _path_suffixes(repo_root_str: str) -> tuple[frozenset[str], frozenset[str]]:
    """(every path suffix of every project file, every file stem).

    Suffixes because docs write paths **package-relative**, not repo-relative:
    `core/i18n.py` means `dungeoneer/core/i18n.py`, `memory/ref_i18n.md` means
    `.claude/memory/ref_i18n.md`. That is the house style across CLAUDE.md and
    every plan doc, so a gate that demanded repo-relative paths would report
    hundreds of "violations" that are really a notation it disagrees with.

    **The file list comes from git, not from `rglob`** (2026-07-30). It used to
    walk the whole disk, which quietly made the gate machine-specific: an
    *ignored* local directory could satisfy a reference that does not exist in
    the project at all. That is not hypothetical — `sources/` holds Karel's
    manually downloaded asset packs (`CLAUDE.md`: never committed, never
    referenced from game code), and one of them ships a top-level licence file.
    A doc describing some *other* project's licence file therefore resolved here
    and failed in every clean checkout, which is how three runner cards died in a
    row while `preflight.py` on this box reported the same gate green minutes
    earlier. (Deliberately not naming that filename here: `_index` harvests every
    identifier token in this very file, so writing it would put it back in the
    symbol table and re-mask the bug this paragraph documents.)

    It is the same non-reproducibility `_IGNORED_TREE_PREFIXES` above already
    guards against for `.ai/runs`, reached through a different door — so the fix
    is to stop enumerating the disk rather than to grow that tuple. Anything git
    ignores is, by definition, not something a doc may cite as project content.
    """
    repo_root = Path(repo_root_str)
    suffixes: set[str] = set()
    stems: set[str] = set()
    project = _project_files(repo_root)
    if project is None:
        # No git: fall back to the disk walk. Worse (this is the old bug) but a
        # gate that cannot run at all is worse still, and a non-repo checkout is
        # not the case the reproducibility rule is about.
        project = [
            path.relative_to(repo_root).parts
            for path in repo_root.rglob("*")
            if path.is_file() and not _IGNORED_TREE_PARTS & set(path.parts)
        ]
    framework_root = _framework_root()
    if framework_root is not None:
        # `<pkg>/preflight.py`, `<pkg>/gates/run.py` — indexed under the package
        # name, which is how docs write them. See `FRAMEWORK_PACKAGE`.
        project = list(project) + [
            (FRAMEWORK_PACKAGE, *path.relative_to(framework_root).parts)
            for path in _framework_files()
        ]
    for parts in project:
        if _IGNORED_TREE_PARTS & set(parts):
            continue
        if any(parts[:len(prefix)] == prefix for prefix in _IGNORED_TREE_PREFIXES):
            continue
        stems.add(Path(parts[-1]).stem)
        for i in range(len(parts)):
            suffixes.add("/".join(parts[i:]))
    return frozenset(suffixes), frozenset(stems)


@lru_cache(maxsize=4)
def project_file_paths(repo_root_str: str) -> frozenset[str]:
    """Every project + framework file's *whole* repo-relative posix path, not
    the tail suffixes `_path_suffixes` keeps.

    Built for `source_reference_liveness`, which has a question `_path_suffixes`
    cannot answer: a source literal may claim a bare *directory*
    (`.ai/gates`, `dungeoneer/assets`) rather than a file, and "does some real
    path start with this prefix" needs the whole path, not a flattened bag of
    tail suffixes — a suffix set has already thrown away which segment was the
    front. Same project-plus-framework merge and the same
    `_IGNORED_TREE_PARTS`/`_IGNORED_TREE_PREFIXES` filtering as `_path_suffixes`,
    kept as a second small function rather than folded into it so that
    function's existing return shape (and every caller depending on it) is
    untouched.
    """
    repo_root = Path(repo_root_str)
    project = _project_files(repo_root)
    if project is None:
        project = [
            path.relative_to(repo_root).parts
            for path in repo_root.rglob("*")
            if path.is_file() and not _IGNORED_TREE_PARTS & set(path.parts)
        ]
    framework_root = _framework_root()
    if framework_root is not None:
        project = list(project) + [
            (FRAMEWORK_PACKAGE, *path.relative_to(framework_root).parts)
            for path in _framework_files()
        ]
    out: set[str] = set()
    for parts in project:
        if _IGNORED_TREE_PARTS & set(parts):
            continue
        if any(parts[:len(prefix)] == prefix for prefix in _IGNORED_TREE_PREFIXES):
            continue
        out.add("/".join(parts))
    return frozenset(out)


def _under_ignored_prefix(token: str) -> bool:
    """Whether a (normalized) relative path token sits under one of
    `_IGNORED_TREE_PREFIXES`, regardless of how many segments it has above that
    prefix — `.ai/runs/<card>/attempt-1/prompt-1.md` must not resolve just
    because it happens to be four segments deep rather than two."""
    parts = tuple(p for p in token.replace("\\", "/").split("/") if p not in ("", "."))
    return any(parts[:len(prefix)] == prefix for prefix in _IGNORED_TREE_PREFIXES)


def _in_project(repo_root: Path, rel: str) -> bool:
    """Whether `rel` (repo-relative, forward slashes) is project content.

    The bare-`.exists()` companion to `_path_suffixes`'s index. Both existence
    checks in `resolve_path` have to agree about what "in the project" means, or
    the stricter one is simply bypassed by the looser one — which is what happened
    twice: once for `.ai/runs` (2026-07-24, fixed with a prefix tuple) and again
    for gitignored trees (2026-07-30, this function). A prefix tuple cannot express
    "whatever `.gitignore` says", so the membership test is now the index itself.
    """
    if _under_ignored_prefix(rel):
        return False
    suffixes, _ = _path_suffixes(str(repo_root))
    if rel in suffixes:
        return True
    # No git (index built from the disk walk) — fall back to plain existence, the
    # pre-2026-07-30 behaviour, rather than failing every reference in a non-repo.
    return _project_files(repo_root) is None and (repo_root / rel).exists()


def resolve_path(repo_root: Path, token: str, doc: Path | None = None) -> bool:
    """A path reference resolves if it names project content — as a repo-relative
    path, a doc-relative one (markdown links are written `../../dungeoneer/…`), or
    a suffix of some project path.

    "Project content" is git's answer, not the filesystem's (see `_path_suffixes`).
    That matters here specifically because this function used to *also* test
    `(repo_root / token).exists()` directly, a second door the index exclusion did
    not reach: an ignored local directory — `sources/`, a stray download, an IDE
    cache — made a reference resolve on one machine and dangle on every other. Both
    doors now go through `_in_project`.
    """
    if _under_ignored_prefix(token):
        return False
    if doc is not None and ".." in token:
        try:
            resolved = (doc.parent / token).resolve()
            rel = resolved.relative_to(repo_root.resolve()).as_posix()
            if resolved.exists() and _in_project(repo_root, rel):
                return True
        except (OSError, ValueError):
            # ValueError: the doc-relative path resolves outside `repo_root`
            # entirely, which is not this function's concern either way.
            pass
        token = token.lstrip("./").replace("../", "")
        if _under_ignored_prefix(token):
            return False
    return _in_project(repo_root, token)


def file_stems(repo_root: Path) -> frozenset[str]:
    return _path_suffixes(str(repo_root))[1]


def clear_caches() -> None:
    _index.cache_clear()
    _path_suffixes.cache_clear()
    project_file_paths.cache_clear()
    _framework_root.cache_clear()
