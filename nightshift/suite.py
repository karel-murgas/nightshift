#!/usr/bin/env python3
"""How the test suite is selected, parallelised and judged — the one place that
policy lives, shared by every caller that runs pytest on a project's behalf
(the runner, `nightshift.merge_check`, `nightshift.preflight`).

Three parts, and they compose into one answer to "run the suite and tell me
whether it passed":

1. **Which slice** (`select`) — the game/system split, below.
2. **How to run it** (`parallel_args`) — `-n auto --dist loadfile` when
   pytest-xdist is installed, serial when it is not.
3. **How to judge it** (`check_junit`) — pytest's own JUnit report, not an exit
   code and not a claim that the suite ran.

Keeping all three here is what lets the preflight run *exactly* what the runner
runs. They used to be spread across three files with the preflight having none
of them: it shelled a plain full-suite `pytest tests/ -q` and judged the exit
code, so the mandatory pre-merge boundary was both the slowest pytest invocation
in the project and the only one that could not tell a 0-collected run from a pass.

## Which slice a diff needs — the game/system split

**Everything project-specific here is three manifest fields**
(07_portability.md §8 step 4): `project.source_dirs` says which prefixes are the
project's own code, `tests.dir` where its tests live, `board.root` where the
board is. §2's re-read found there is nothing else — `classify` is four prefix
checks of which one is the project's, and `split_test_files`/`board_test_files`
read the real tree and parse imports rather than consulting a table. The
per-part test globs a first reading expects to find hardcoded are globs *by
design*: a newly added `test_gate_*.py` must classify without anyone remembering
a list. The incidents quoted below are Dungeoneer's, because they are the
evidence this module was built from; the rules they justify are not.

The suite it was measured on is ~1650 tests, ~500 of them AI-team infrastructure
(the board, the runner, the gates, the staleness machinery). The two halves share
no imports — game tests never touch `.ai/`, system tests never touch the project
package — so a change confined to one half **cannot** break a test in the
other. Selecting on that wall lets a game-only card skip the whole system suite
(and back), which is most of a simple card's wall-clock (`drop-item`, 2026-07-25:
6.8 min of pytest, run twice, on a change that deleted one class).

Deterministic (`00_architecture.md` §12, no LLM): a set of changed paths in, a
pytest selection out. The one rule that keeps it safe: when the diff is **not**
cleanly one half — it touches both, or something neither (docs, config) — the
answer is ALL. "Not sure what this affects" resolves to "run everything," never
to "run less." The only unsafe direction is running too few tests, and this
design can only err toward too many.

Note the asymmetry that makes the wall safe to select on: a *game* invariant that
a game change breaks is caught by the **gate** for it (run separately by the
runner), not by a `test_gate_*` file — those exercise the gate's own logic and
live under `.ai/`. So a game-only diff genuinely needs no `test_gate_*`/`test_board_*`.

## Board content is a third, narrower slice

`Board/` is card *content* — markdown, not code — and it splits off from the rest
of the system half by the same asymmetry. A card's validity (frontmatter present
and resolvable, `state:` agreeing with its lane, the actionable sections a `tasks/`
card needs) is enforced by the **`card_schema` gate**, which runs separately and
always. The `test_board_*.py` files exercise the board *machinery* — the gate's own
logic, the digest, the reconciler, the runner — against synthetic cards in tmp
dirs, so editing a real card cannot break them; the only pytest that reads the real
board is `test_board_schema.py::test_the_real_board_passes`, and that asserts
exactly what the gate already checked. So:

* a diff of only **actionable** board lanes (`tasks/`, `review/`, `done/`, …)
  selects **BOARD** — just the `test_board_*.py` files, not the whole system suite.
* a diff of only **`ideas/`** or **`inbox/`** — freeform notes Karel drops in for
  triage, which the gate treats leniently or does not scan at all — selects
  **NONE**: no pytest runs. NONE is a real, judged answer, not a 0-collected
  accident: a caller that gets it skips the pytest step and reports a pass, because
  the gate is the check for these files and there is no test to run.

BOARD is a strict subset of SYSTEM (its glob is a subset), so a board change that
rides along with a real `.ai/` change just resolves to SYSTEM — no coverage lost.

## A diff touching several parts runs the union of their tests

This is not a fixed list of allowed combinations — it is a **union**. A change is
classified into the test-bearing *parts* it touches (`game`, `system`, `board`;
notes and docs touch none), and the run is the union of each part's test files.
Every combination falls out of that one rule rather than being special-cased:

* `game` + `board` → the game slice **plus** the board's tests, and nothing else
  (not the gates/staleness/runner tests neither part can affect).
* `board` + `system` → just `system`: the board's tests are a subset of the system
  slice, so the union collapses back to it, losing no coverage.
* `game` + `system` → every file, i.e. ALL.

`Selection` therefore carries the reduced set of parts, and `pytest_args` computes
the union from it; the `bucket` string (`game`, `board`, `board+game`, `all`,
`none`, …) is a derived, human-readable *label* for that set, used in logs and the
receipt — the mechanism keys on the parts, never on the label. Adding a future
part would need no new combination code: `select` would put it in the set and the
union would already run it.

**Which side a test file is on is read off its imports, not only its name.** The
first version of this module decided that from the filename alone
(`test_board_*`/`test_gate_*`/`test_stale*`), and two files sat outside those
globs while importing `.ai/` modules: `tests/test_self_improvement.py` (the
preflight's and the runner's own tests) and `tests/test_asset_hygiene.py`. Both
were classified GAME, so a `.ai/`-only diff selected SYSTEM and skipped them —
the tests for the changed code were exactly what the selection dropped. Note that
both directions of a wrong answer lose coverage here, so this is not a place to
pick a "safe default": the classification has to be *accurate*, which is why it
parses imports rather than pattern-matching names, and why a file that imports
both halves is run by both slices instead of being assigned to one.
"""
from __future__ import annotations

import ast
import fnmatch
import importlib.util
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from nightshift import manifest as _manifest
from nightshift.manifest import AI_DIR, ManifestError

# The three test-bearing *parts* a diff can touch. A `select` result is the union
# of the parts a diff hits; these names double as the single-part bucket labels.
GAME, SYSTEM, BOARD = "game", "system", "board"
# Two derived bucket labels with fixed spellings the callers/receipt expect: ALL is
# `game`+`system` (which covers every file), NONE is the empty part set (no test).
ALL, NONE = "all", "none"
# `NOTE` is not a part and not a bucket — it is what `classify` gives a board *note*
# (an `ideas/`/`inbox/` path), a path that carries no test weight. Kept distinct
# from NONE on purpose: NOTE labels a path, NONE labels a run. (Read them
# carefully; they differ by one letter.)
NOTE = "note"

# The parts whose union is every test file — game code plus all system code. A
# diff hitting both is the whole suite.
_ALL_PARTS = frozenset({GAME, SYSTEM})
# BOARD's tests live *inside* the system slice, so once a diff already pulls in
# SYSTEM the BOARD part is redundant. `_reduce` folds it away; keeping the two
# in one place is what makes `{system, board} -> system` a fact rather than a rule
# repeated at every call site.
_REDUNDANT_ONCE_SYSTEM = frozenset({BOARD})


def _reduce(parts) -> frozenset:
    """The minimal set of parts with the same test union — board folds into system."""
    parts = frozenset(parts) & (_ALL_PARTS | {BOARD})
    if SYSTEM in parts:
        parts -= _REDUNDANT_ONCE_SYSTEM
    return parts


def _label(parts) -> str:
    """The bucket string for a set of parts: `none`, `all`, or the parts joined —
    `game`, `board`, `board+game`. Deterministic (sorted) so the label of a given
    part set never wobbles between runs or callers."""
    parts = _reduce(parts)
    if not parts:
        return NONE
    if parts == _ALL_PARTS:
        return ALL
    return "+".join(sorted(parts))


# The single composite label worth naming for callers/tests. Equal to what `_label`
# derives, so it cannot drift from the mechanism: game code + board content.
GAME_BOARD = _label({GAME, BOARD})

# Test files that exercise AI-team infrastructure, by filename glob so a
# newly-added one (a new gate's test, say) is classified correctly without anyone
# remembering to edit a list here — the failure mode a hardcoded list would have.
_SYSTEM_TEST_GLOBS = ("test_board_*.py", "test_gate_*.py", "test_stale*.py")
# The board's own tests — the subset of the system suite a change to *card
# content* (a card moved between lanes, a new `tasks/` card, an edited field) can
# actually affect. A glob for the same reason `_SYSTEM_TEST_GLOBS` is one: a new
# `test_board_*.py` is picked up without editing a list. Note this is a subset of
# `_SYSTEM_TEST_GLOBS`, which is what makes SYSTEM a safe superset of BOARD.
_BOARD_TEST_GLOBS = ("test_board_*.py",)


@dataclass(frozen=True)
class Layout:
    """The three manifest fields `classify` needs, resolved once.

    A dataclass rather than three lookups because they are read together on
    every classified path, and a `select` over a 300-file diff would otherwise
    re-parse the manifest 300 times.

    **The no-manifest answer is deliberately not "guess the project".**
    `source_dirs` is empty, so no path classifies as GAME and `select` falls
    through to its ALL default — "diff touches no classifiable code, running
    everything to be safe". That is the same safe direction the module has
    always erred in, and it is why this is a fallback rather than a `require()`:
    a preflight in a half-configured repo should run too many tests, not refuse
    to run. `tests` and `Board` are the schema defaults, which are the literals
    this replaced.
    """
    source_dirs: tuple[str, ...] = ()
    tests_dir: str = "tests"
    board_root: str = "Board"

    @property
    def source_packages(self) -> frozenset[str]:
        """The top-level import names of the project's own code — what
        `touches_game` matches against, since `_imports` returns bare module
        names. `src/game` imports as `game`."""
        return frozenset(Path(d).name for d in self.source_dirs)


def layout(repo_root: Path | None) -> Layout:
    """Where this project keeps its code, tests and board."""
    if repo_root is None:
        return Layout()
    try:
        m = _manifest.load(repo_root)
    except ManifestError:
        return Layout()
    return Layout(source_dirs=m.project.source_dirs,
                  tests_dir=m.tests.dir,
                  board_root=m.board.root)


def tests_rel(repo_root: Path | None) -> str:
    """Where the tests live, relative to the repo root.

    Relative on purpose: every caller but the preflight resolves it against a
    *worktree* of the repo rather than the repo itself, and handing them an
    absolute path rooted in the main checkout is how a merge check ends up
    running the tests of the tree it was trying to validate a merge against.
    """
    return layout(repo_root).tests_dir


def is_system_test(name: str) -> bool:
    """Does this filename alone mark a system test? Naming convention only.

    **Not sufficient on its own** — see `touches_ai`. Two files under `tests/`
    import `.ai/` modules while being named outside these globs
    (`test_self_improvement.py`, `test_asset_hygiene.py`), so a glob-only answer
    put them in the GAME half and a `.ai/`-only diff skipped the tests for the
    very code it changed. Kept as its own function because the *convention* is
    still worth expressing and a file matching it needs no source read.
    """
    return any(fnmatch.fnmatch(name, glob) for glob in _SYSTEM_TEST_GLOBS)


# The framework package the tooling was extracted into (07_portability.md §8
# step 2). A test that imports it is a system test in exactly the same sense as
# one importing `.ai/` — and without this, the move opened a silent hole: a future
# `tests/test_something.py` importing only `nightshift` would be classified GAME, so
# the SYSTEM slice would not run it and a `.ai/`-only diff would skip it entirely.
# That is the same bug `touches_ai` was written to fix, reintroduced by relocation.
_FRAMEWORK_PACKAGE = "nightshift"


def ai_module_names(repo_root: Path) -> frozenset[str]:
    """Every bare module name importable from `.ai/`, read off the real tree, plus
    the framework package.

    `.ai` cannot be a package (a leading dot is not a legal identifier), so its
    modules are imported as top-level names after a `sys.path` insert — `import
    suite`, `import asset_hygiene`. The importable set is therefore just the
    stems under `.ai/` and `.ai/gates/`, derived rather than listed for the same
    reason the globs are globs. `nightshift` is added flat because `_imports` already
    reduces `from nightshift import textio` to its top-level name.
    """
    names: set[str] = {_FRAMEWORK_PACKAGE}
    for sub in (repo_root / ".ai", repo_root / ".ai" / "gates"):
        if sub.is_dir():
            names.update(p.stem for p in sub.glob("*.py") if not p.stem.startswith("__"))
    return frozenset(names)


def _imports(path: Path) -> frozenset[str]:
    """Top-level module names a Python file imports, via AST.

    AST and not a regex: the answer decides whether a file is dropped from a
    slice, and *both* directions of a wrong answer lose coverage (a game test
    called system is ignored by the GAME slice; a system test called game is
    ignored by the SYSTEM slice). A regex that matched the word `runner` inside a
    docstring would silently drop a real game test. An unparseable file returns
    nothing — it will fail collection anyway, which is a louder signal than
    anything guessed here.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return frozenset()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return frozenset(found)


def touches_ai(path: Path, ai_modules: frozenset[str]) -> bool:
    """Does this test file import an `.ai/` module — i.e. is it a system test in
    substance, whatever it is called?"""
    return bool(_imports(path) & ai_modules)


def touches_game(path: Path, packages: frozenset[str]) -> bool:
    """Does this test file import the project's own package? A file that imports
    *both* halves belongs in both slices rather than being assigned to one."""
    return bool(_imports(path) & packages)


def classify(path: str, repo_root: Path | None = None) -> str:
    """One changed path → `game` | `system` | `board` | `note` | `other`.

    `other` is docs, memory, `.claude/` config — anything no pytest asserts on.
    It is deliberately *not* game or system: it must not, on its own, force a
    bucket, because a typical game card also edits `.claude/memory/*.md` per the
    update rules and that must not drag every game card up to ALL.

    `repo_root` does two things, and both were one thing before the manifest:
    it lets a changed *test file* be classified by what it imports as well as by
    its name, and it is how the project's own `source_dirs`, `tests.dir` and
    `board.root` are known at all. Without it the answer is the naming
    convention plus the framework defaults — no path can be GAME, so `select`
    resolves to ALL, which is the safe direction. Every caller that has a root
    passes it.
    """
    where = layout(repo_root)
    p = path.replace("\\", "/")
    if p.startswith("./"):  # a `./` prefix only — NOT lstrip("./"), which would
        p = p[2:]           # also eat the leading dot of `.ai/`.
    name = p.rsplit("/", 1)[-1]
    tests_prefix = f"{where.tests_dir}/"
    board_prefix = f"{where.board_root}/"
    if p.startswith(tests_prefix) and name.startswith("test_") and name.endswith(".py"):
        if is_system_test(name):
            return SYSTEM
        if repo_root is not None:
            full = repo_root / p
            if full.is_file() and touches_ai(full, ai_module_names(repo_root)):
                # A system test by substance. GAME too when it also imports the
                # project package, which resolves to ALL in `select` — the correct
                # answer for a file both slices must run.
                return SYSTEM if not touches_game(full, where.source_packages) else ALL
        return GAME
    if p.startswith(f"{AI_DIR}/"):
        return SYSTEM
    if p.startswith(board_prefix):
        # Card content, not code. `ideas/` and `inbox/` are freeform notes the
        # card_schema gate handles (or, for ideas/, never scans) — they are NOTE,
        # which carries no pytest. Every other lane is a card whose content the
        # board's own tests cover — that is BOARD. See the module docstring.
        rest = p[len(board_prefix):]
        lane = rest.split("/", 1)[0] if "/" in rest else ""
        return NOTE if lane in ("ideas", "inbox") else BOARD
    if any(p.startswith(f"{d}/") for d in where.source_dirs):
        return GAME
    return "other"


def split_test_files(tests_dir: Path) -> tuple[list[Path], list[Path]]:
    """`(system_only, shared)` test files on disk.

    `system_only` is what the SYSTEM slice runs and what the GAME slice ignores.
    `shared` — a file importing an `.ai/` module *and* the game package — is in
    **both** lists' worth of runs: named by SYSTEM, and not ignored by GAME. No
    such file exists today; it is handled rather than asserted away because the
    alternative is that adding one silently drops it from one of the two slices,
    which is the failure this whole function exists to fix.
    """
    repo_root = tests_dir.parent
    ai_modules = ai_module_names(repo_root)
    packages = layout(repo_root).source_packages
    system_only: list[Path] = []
    shared: list[Path] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        by_name = is_system_test(path.name)
        by_import = touches_ai(path, ai_modules)
        if not (by_name or by_import):
            continue
        # A glob-named system test is system by convention; only an import-derived
        # one is asked whether it straddles the wall.
        straddles = by_import and not by_name and touches_game(path, packages)
        (shared if straddles else system_only).append(path)
    return system_only, shared


def board_test_files(tests_dir: Path) -> list[Path]:
    """The board's own test files on disk — what the BOARD slice runs.

    A glob against the real tree for the same reason `split_test_files` is: a
    newly-added `test_board_*.py` is included without editing a list. These are a
    subset of the system-only files, so the BOARD slice never runs anything the
    SYSTEM slice would not.
    """
    found: list[Path] = []
    for glob in _BOARD_TEST_GLOBS:
        found.extend(tests_dir.glob(glob))
    return sorted(set(found))


@dataclass(frozen=True)
class Selection:
    bucket: str            # the derived label: game | system | board | board+game | all | none
    reason: str
    parts: frozenset = frozenset()   # the reduced test-bearing parts this run covers

    @classmethod
    def for_parts(cls, parts, reason: str) -> "Selection":
        """Build a selection from the parts a diff touches. The label is derived,
        never passed — so it can never disagree with the parts it names."""
        reduced = _reduce(parts)
        return cls(_label(reduced), reason, reduced)

    def _parts(self) -> frozenset:
        """The parts to run. `for_parts` sets them; a bare `Selection(bucket, …)`
        (the `--full-tests` path, and tests) leaves them empty, so fall back to the
        parts the label stands for. Both paths reach the same union below."""
        return self.parts or _BUCKET_PARTS.get(self.bucket, _ALL_PARTS)

    def pytest_args(self, tests_dir: Path) -> list[str]:
        """The path arguments to hand pytest for this selection — the **union** of
        the test files of every part this selection covers.

        The files are read off the *real* tree (by name and by what they import),
        so the selection tracks the tree, not a copy of its filenames. When the
        union includes the game part it is expressed as `tests/` minus the files
        left out (the cheaper, order-independent form); otherwise the files to run
        are named explicitly.

        A part set that is empty (NONE) returns the **empty list** — the one
        selection that runs nothing. Every other selection returns at least
        `["tests/"]`, so an empty result is an unambiguous "skip pytest, report a
        pass" signal each caller checks before building a pytest command (board
        notes only, whose validity is a gate's job — see the module docstring).
        """
        parts = self._parts()
        if not parts:
            return []
        # The pathspec pytest is given, relative to the repo root it will run in.
        # Derived from the directory rather than read from the manifest a second
        # time: `split_test_files` below already treats `tests_dir.parent` as the
        # repo root, so a `tests.dir` nested deeper than one level is unsupported
        # here in exactly the way it was before this was configurable at all.
        prefix = tests_dir.name
        system_only, shared = split_test_files(tests_dir)
        system_names = {f.name for f in (*system_only, *shared)}
        system_only_names = {f.name for f in system_only}
        board_names = {f.name for f in board_test_files(tests_dir)}
        all_files = [f.name for f in sorted(tests_dir.glob("test_*.py"))]

        run: set[str] = set()
        if GAME in parts:
            run |= {n for n in all_files if n not in system_only_names}  # game files + shared
        if SYSTEM in parts:
            run |= system_names
        if BOARD in parts:
            run |= board_names

        if GAME in parts:
            # the test dir minus the files not being run — an empty ignore list is
            # just `["tests/"]` (the ALL case), so this one branch covers game,
            # game+board and game+system alike.
            ignore = sorted(n for n in all_files if n not in run)
            return [f"{prefix}/", *(f"--ignore={prefix}/{n}" for n in ignore)]
        return [f"{prefix}/{n}" for n in sorted(run)] or [f"{prefix}/"]


# Every bucket label mapped back to the parts it stands for. Used only for a bare
# `Selection(bucket, reason)` (the `--full-tests` escape hatch and the tests); a
# `select` result carries its parts directly. Kept in agreement with `_label` by
# `test_board_suite.py`, which round-trips every label through both.
_BUCKET_PARTS: dict[str, frozenset] = {
    NONE: frozenset(),
    GAME: frozenset({GAME}),
    SYSTEM: frozenset({SYSTEM}),
    BOARD: frozenset({BOARD}),
    GAME_BOARD: frozenset({GAME, BOARD}),
    ALL: _ALL_PARTS,
}


def _reason(raw_parts, reduced: frozenset, where: Layout | None = None) -> str:
    """A human sentence for a part set — what the diff touched and what runs.

    Built from the parts, not hand-written per combination, so it stays honest as
    combinations are added. `raw_parts` is what the diff hit (so a board path is
    still mentioned even when it folds into system); `reduced` is what runs.
    """
    dirs = ", ".join(f"{d}/" for d in (where.source_dirs if where else ())) or "project code"
    what = {
        GAME: f"game code ({dirs} + game tests)",
        SYSTEM: f"{AI_DIR}/ system code",
        BOARD: "board card content",
    }
    touched = " + ".join(what[p] for p in sorted(raw_parts))
    if reduced == _ALL_PARTS:
        return f"diff touches {touched} — the whole suite"
    if BOARD in raw_parts and SYSTEM in raw_parts:
        return f"diff touches {touched} — the system slice already runs the board tests"
    if len(reduced) > 1:
        return f"diff touches {touched} — running each part's tests, and only those"
    return f"diff touches only {touched}"


def select(changed: set[str], repo_root: Path | None = None) -> Selection:
    """The selection a set of changed paths needs — the union of the tests of every
    test-bearing part the diff touches.

    Keys on the *code* parts only (`game`/`system`/`board`); `note` and `other`
    paths carry no tests, so a game card that also edits memory docs or drops an
    inbox note still selects `game`. A diff that is purely board notes runs nothing
    (NONE); one with no classifiable code at all runs everything (ALL, the safe
    default — the only unsafe direction is running too few).

    An **empty** `changed` is answered before any of that: no paths at all is not
    the same fact as "paths that classified as nothing" (`empty-diff-preflight-
    runs-everything`). The former means nothing changed, so nothing needs testing
    — NONE, with its own reason distinct from the `{NOTE}` case below. The latter
    still falls through to the ALL default, untouched: "I saw paths and could not
    classify them" is still the right cue to run everything.

    `repo_root` is passed through to `classify` so a changed test file is judged
    by its imports too, not only its name; omitted, the answer is name-only.
    """
    if not changed:
        return Selection(NONE, "nothing changed against the base")
    kinds = {classify(p, repo_root) for p in changed}
    if ALL in kinds:
        # A single test file that imports both halves — already the whole suite.
        return Selection(ALL, "diff touches a test file that spans both halves", _ALL_PARTS)
    parts = kinds & {GAME, SYSTEM, BOARD}
    if parts:
        return Selection.for_parts(parts, _reason(parts, _reduce(parts), layout(repo_root)))
    if kinds == {NOTE}:
        # Purely board notes (ideas/ or inbox/), nothing else — not even a doc.
        # Their validity is the card_schema gate's job; no pytest applies.
        return Selection(NONE, "diff touches only board notes (ideas/ or inbox/) — card "
                               "validity is the card_schema gate's job, no pytest applies")
    return Selection(ALL, "diff touches no classifiable code — running everything to be safe",
                     _ALL_PARTS)


# --- 2. How to run it ---------------------------------------------------------

# `-n auto` spreads the run across cores; `--dist loadfile` keeps every test in
# one file on one worker, so a file's module-level state and in-file ordering are
# preserved — the game suite is safe under `loadfile` but flakes under the default
# `--dist load`, which splits a file across workers (runner-test-selection).
XDIST_ARGS: tuple[str, ...] = ("-n", "auto", "--dist", "loadfile")

# Why `-n auto` is not always safe, and what replaces it (2026-07-31).
#
# `auto` means "one worker per logical CPU" and knows nothing about memory. Each
# worker is a fresh interpreter that imports the whole stack the tests touch —
# pygame, numpy, and since the audio work `scipy` and `soundfile`, which are not
# small. On a 22-CPU box with ~10 GB free that is 22 concurrent copies, and the
# suite died with `pygame.error: Out of memory`, two `MemoryError`s and a crashed
# xdist worker spread across 13 unrelated files. Nothing was broken: the same
# tree, same commit, run with `-n 6`, was 2134 passed / 1 skipped.
#
# That failure is worse than slow, for the same reason the missing-xdist case
# below is: it is **indistinguishable from a real breakage**. It reports as two
# dozen failures in game code, so it costs a reader the time to disprove twenty
# four defects — and in the runner it fails a card for something that is not
# about the card at all, which is the shape that cost three cards on 2026-07-31.
#
# So the worker count is capped by whichever of CPU and memory is scarcer. The
# probe is best-effort and every failure path returns to `auto`: a box whose
# memory cannot be read is exactly as well served as before this existed.

# Resident memory one worker needs once it has imported that stack. Measured
# generously on purpose — the cost of overestimating is a slower run, the cost of
# underestimating is the OOM this constant exists to prevent.
WORKER_MEMORY_GB = 1.4

# Never cap below this. Two workers is still a meaningful speed-up over serial,
# and a probe that reports almost no free memory is more likely wrong than it is
# describing a box that cannot run two interpreters.
MIN_WORKERS = 2

# Memory the suite must leave for everything that is not the suite (2026-08-10).
#
# Spending *all* of the headroom is what made the 2026-08-10 incident hurt someone
# other than the test run. A cap computed from the whole free figure is arithmetic
# that just fits: on Karel's box it allowed 9 workers against 13.4 GB of commit
# headroom, i.e. 12.6 GB of workers and ~0.8 GB left for the editor those tests
# were launched from. The suite then survives and the desktop does not, which is
# the wrong thing to optimise — a slower suite costs minutes, a crashed VS Code
# costs the session.
#
# 4 GB is sized for what is realistically open while the suite runs (an IDE, a
# browser, a chat client) plus the growth they do while it runs, not for an idle
# box. It only ever binds when memory was already tight; on a box with room the
# subtraction changes nothing because the CPU count binds first.
SYSTEM_RESERVE_GB = 4.0


def xdist_available() -> bool:
    """Is pytest-xdist importable here? A spec lookup, not an import — this is
    asked on the way to building an argv, and paying for the plugin's import to
    decide whether to pass `-n` would be backwards."""
    return importlib.util.find_spec("xdist") is not None


def available_memory_gb() -> float | None:
    """Memory a new worker process can actually obtain, in GB — or `None` when it
    cannot be determined.

    `None` is a real answer meaning "no opinion", not an error: every caller
    falls back to `-n auto`, which is what this project did unconditionally
    before. Deliberately dependency-free — `psutil` would read this in one line,
    but adding a hard dependency to the module that decides how to run the tests
    means a box that has not installed it cannot run them at all, which is a
    worse failure than the one being fixed.

    **Why this is not "free physical memory" (measured 2026-08-10).** It was, and
    on Windows that reads the wrong number. Windows refuses an allocation when the
    system-wide *commit charge* would exceed the commit limit (RAM + pagefile),
    and commit is charged when a page is reserved, not when it is first touched —
    so a box can be at 40% physical memory use and simultaneously out of commit.
    Measured on Karel's box: 31.9 GB RAM with 18.9 GB free (`ullAvailPhys`), but
    only 13.4 GB of commit headroom (`ullAvailPageFile`) against a 127.9 GB limit
    — 89.5% committed, confirmed against `\\Memory\\%% Committed Bytes In Use` and
    `Win32_PerfFormattedData_PerfOS_Memory`. Reading `ullAvailPhys` there returned
    18.9, `worker_count(12, 18.9)` found 12 workers affordable, and the cap
    therefore declined to cap: `-n auto` spawned 12 workers wanting ~16.8 GB of
    commit against 13.4 GB of headroom.

    That overshoot does not fail like an OOM. Commit exhaustion is *system-wide*:
    the allocation that fails belongs to whichever process asks next, so VS Code,
    Explorer and Discord crashed while the tests kept running and Task Manager
    showed memory at ~52%. The guard existed and was inert in exactly the
    condition it was written for, because it was watching the wrong meter.

    So both meters are read and the scarcer one wins. Physical still matters (a
    box that must page every worker in and out is thrashing even with commit to
    spare); commit is what turns an overshoot into other people's crashes.

    POSIX is left reading available physical memory: Linux's default
    `overcommit_memory=0` has no comparable hard limit to run into, and the
    failure mode this docstring is about is a Windows commit-accounting one.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            # `ullAvailPageFile` is misnamed: it is commit headroom (commit limit
            # minus commit charge), not space left in pagefile.sys.
            return min(status.ullAvailPhys, status.ullAvailPageFile) / (1024 ** 3)
        except (OSError, AttributeError, ValueError):
            return None
    try:
        # POSIX: available pages x page size. `SC_AVPHYS_PAGES` is free memory
        # rather than total, which is the number that matters — a box already
        # running something else has less room for workers.
        return (os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024 ** 3)
    except (OSError, ValueError, AttributeError):
        return None


def worker_count(cpus: int | None, memory_gb: float | None) -> int | None:
    """How many xdist workers a box with these resources can afford — `None` for
    "no opinion", meaning the caller should use `auto`.

    **Pure on purpose.** Both arguments are required and nothing is probed here,
    so `None` unambiguously means "this input is unknown" rather than "please go
    and look". An earlier version defaulted them to the probe, which made
    `worker_count(22, None)` silently read *this* machine's memory — so the case
    that matters most (the probe failed, fall back to `auto`) was the one case a
    test could not express. `probed_worker_count` does the looking.

    `memory_gb` is what `available_memory_gb` reports — on Windows the scarcer of
    free physical memory and commit headroom. `SYSTEM_RESERVE_GB` comes off it
    before anything is divided, so the answer is how many workers fit in the room
    the suite is *allowed* to take, never how many fit in the room that exists.
    """
    if not cpus or memory_gb is None:
        return None
    for_workers = max(0.0, memory_gb - SYSTEM_RESERVE_GB)
    affordable = int(for_workers / WORKER_MEMORY_GB)
    capped = max(MIN_WORKERS, min(cpus, affordable))
    # Saying `-n 22` on a 22-CPU box is what `auto` already does. Returning None
    # keeps the argv identical to the historical one whenever memory is not the
    # binding constraint, so this only ever *narrows* behaviour.
    return None if capped >= cpus else capped


def probed_worker_count() -> int | None:
    """`worker_count` against this machine. The one place the probes are called."""
    return worker_count(os.cpu_count(), available_memory_gb())


# "No argument was passed", as distinct from "the answer is None". `parallel_args`
# needs both: not passing `workers` means "go and probe this box", while passing
# `None` means "there is no cap, use `auto`" — and a test has to be able to say the
# second one. Defaulting to `None` collapsed the two and made the fall-back-to-auto
# case the one branch no test could express, which is the same trap `worker_count`
# had one level down.
_PROBE = object()


def parallel_args(available: bool | None = None, workers: int | None = _PROBE) -> tuple[str, ...]:
    """The parallel flags to add to a pytest argv — `()` when xdist is missing.

    **Why this degrades instead of assuming.** `requirements.txt` calls xdist
    optional, and a bare `pytest -n auto` without it is not a slow run but a hard
    `error: unrecognized arguments`, exit 4, no JUnit report written. In the
    runner that reads back as the maximally confusing "the suite did not run at
    all"; in the *preflight* it would be worse than confusing — the receipt is
    what unblocks `git push`, so a box that never installed the optional extra
    could not publish anything, and the fix would look like "the preflight is
    broken" rather than "install a plugin". Serial is slower, runs the identical
    tests, and is the only safe direction to fall back in.

    `available` and `workers` are injection points for tests, which must be able
    to assert every branch without depending on what the running box happens to
    have. Omitting `workers` probes this box; passing `None` states that there is
    no cap (see `_PROBE`).
    """
    present = xdist_available() if available is None else available
    if not present:
        return ()
    capped = probed_worker_count() if workers is _PROBE else workers
    if capped is None:
        return XDIST_ARGS
    return ("-n", str(capped), "--dist", "loadfile")


# --- 3. How to judge it -------------------------------------------------------


def check_junit(path: Path) -> tuple[bool, str]:
    """Read pytest's own JUnit report and turn it into a `(ok, why)` verdict.

    Karel's design (runner-test-selection): the **python suite generates the
    record, the runner checks it** — not an LLM, and not an exit code, saying the
    tests ran. The XML is the authoritative artefact. Two failure modes it is
    written to catch beyond the obvious `failures`/`errors`: a report that was
    never written (the suite did not run at all), and one reporting **zero
    tests** — a selection that matched nothing. Zero-collected is the
    silent-green this project has been bitten by repeatedly, so it is a failure
    here, never a pass.
    """
    if not path.is_file():
        return False, "pytest: no JUnit report was written — cannot confirm the suite ran"
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return False, "pytest: the JUnit report was unreadable"
    total = failures = errors = 0
    for suite_el in root.iter("testsuite"):
        total += int(suite_el.get("tests", 0) or 0)
        failures += int(suite_el.get("failures", 0) or 0)
        errors += int(suite_el.get("errors", 0) or 0)
    if total == 0:
        return False, ("pytest: 0 tests collected — a selection that runs nothing is not a "
                       "pass (suite.select picked an empty set)")
    if failures or errors:
        # The counts alone were the whole verdict until 2026-07-31, and they are
        # what the card and the digest quote. "2 failure(s) across 1860 test(s)"
        # is a fact about the run that says nothing about the bug, so the first
        # failing test rides along — one clause, from the report already parsed.
        # `failure_excerpt` is the block form for the card; this is the line form.
        why = f"pytest: {failures} failure(s), {errors} error(s) across {total} test(s)"
        first = first_failure(path)
        if first:
            why = f"{why} — first: {first}"
        return False, why
    return True, ""


def junit_total(path: Path) -> int:
    """How many tests the report accounts for; 0 when it is missing or unreadable.
    Used for the human-facing "N tests" line — never for the verdict, which is
    `check_junit`'s job."""
    if not path.is_file():
        return 0
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return 0
    return sum(int(el.get("tests", 0) or 0) for el in root.iter("testsuite"))


# --- 4. What actually failed --------------------------------------------------
#
# `check_junit` answers "did it pass", and a *verdict* needs nothing more. But its
# string is also the only thing that ever reached the two artefacts Karel actually
# reads — the card's `## Error` and `Digest.md` — and `"pytest: 2 failure(s), 0
# error(s) across 1860 test(s)"` does not say what broke. The per-test detail was
# sitting in the same JUnit report the whole time, read for a count and discarded.
#
# Why the detail has to end up in the *committed* artefact rather than behind the
# `.ai/runs/<card>/attempt-N/` pointer the card used to carry. That pointer was
# unusable two different ways at once, and both were silent:
#
#   * `.ai/runs/` is gitignored (machine-local by design, `ai_team.md`), so a
#     failure produced by the box that runs the night says nothing at all on the
#     box Karel reads the board from; and
#   * `runner.prune_run_dir` deletes the whole card directory the moment the card
#     is retired to `failed/` — so for a terminal failure the pointer was dead
#     even on the machine that wrote it, immediately, by construction.
#
# An excerpt captured at failure time and written into the card is immune to both:
# it is committed, it syncs, and it exists before anything is pruned.

EXCERPT_TESTS = 3   # how many failing tests to name
EXCERPT_LINES = 5   # how many lines of detail to keep per test


def _case_id(case: ET.Element) -> str:
    """A rerunnable pytest node id for one `<testcase>`.

    `tests/test_stairs.py::test_remnants_cleared`, or
    `tests/test_stairs.py::TestRemnants::test_cleared` for a test inside a class —
    something Karel can paste straight after `pytest`, which is the whole reason to
    bother reconstructing it rather than printing the raw attributes.

    The report does not hand this over ready-made. pytest omits the optional
    `file`/`line` attributes under this project's configuration (verified against
    the installed pytest, not assumed), leaving only its dotted `classname`:
    `tests.test_stairs` for a module-level test and `tests.test_stairs.TestRemnants`
    for one in a class. Both convert cleanly, because the class boundary is visible
    — module segments are lowercase by every convention this suite follows, and a
    class segment starts with a capital. `file` is still preferred when present: it
    is authoritative, and a config that emits it should win over this inference.

    Falls back to the dotted form unchanged if the shape is not recognisable, which
    is honest rather than a guess dressed as a node id.
    """
    name = case.get("name") or "?"
    file = case.get("file") or ""
    if file:
        return f"{file}::{name}"
    classname = case.get("classname") or ""
    if not classname:
        return name
    segments = classname.split(".")
    split_at = next((i for i, seg in enumerate(segments) if seg[:1].isupper()),
                    len(segments))
    module, classes = segments[:split_at], segments[split_at:]
    if not module:
        return f"{classname}.{name}"
    return "::".join(["/".join(module) + ".py", *classes, name])


def _one_line(text: str) -> str:
    """The first non-empty line, whitespace collapsed."""
    for line in text.splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


def _detail_lines(text: str, limit: int) -> list[str]:
    """The most informative slice of a pytest long repr.

    Prefers the `E `-prefixed lines from the **head** — that is the assertion and
    the start of its diff, which is what a human reads first — and falls back to
    the **tail** of the non-empty lines, which is where a collection error or a
    bare exception message ends up. Two directions on purpose: an assertion block
    is front-loaded and a traceback is back-loaded, so one rule for both would
    reliably keep the wrong end of one of them.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    marked = [line for line in lines if line.lstrip().startswith("E ")]
    return marked[:limit] if marked else lines[-limit:]


def junit_failures(path: Path) -> list[dict]:
    """Per-test failure detail from the JUnit report, in the order pytest wrote it.

    `[]` when the report is missing, unreadable or green — the same "always a
    lookup, never an exception" contract the rest of this module keeps, because
    every caller is already on a failure path and must not be derailed by a second
    one. Each entry carries `test` (`_case_id`), `kind` (`failure` or `error`),
    `message` (pytest's own one-line summary) and `lines` (`_detail_lines`).
    """
    if not path.is_file():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    out: list[dict] = []
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            for element in case.findall(kind):
                out.append({
                    "test": _case_id(case),
                    "kind": kind,
                    "message": _one_line(element.get("message") or ""),
                    "lines": _detail_lines(element.text or "", EXCERPT_LINES),
                })
    return out


def first_failure(path: Path) -> str:
    """`tests/test_x.py::test_y: AssertionError: assert 3 == 0`, or `""`.

    The one-line form, for appending to a verdict string that has room for a
    clause but not for a block.
    """
    found = junit_failures(path)
    if not found:
        return ""
    head = found[0]
    return f"{head['test']}: {head['message']}".rstrip(": ")


def failure_excerpt(path: Path, *, tests: int = EXCERPT_TESTS,
                    lines: int = EXCERPT_LINES) -> str:
    """A bounded, indented block naming what failed — for a card or the digest.

    Bounded, and the bound is the whole point: cards are committed, synced and
    read in Obsidian, so this must be the part a human recognises the bug from,
    not the log. `.ai/runs/<card>/attempt-N/pytest.txt` stays the unabridged copy
    for whoever is standing on that host.

    Every line is indented four spaces, which does three jobs at once: it renders
    as a code block in Obsidian without a fence, it keeps `doc_scan._HEADING`
    (anchored at column 0) from matching a `#` pytest happened to print — which
    would otherwise end the `stale-ok` exemption early and expose the rest of the
    section to the liveness gate — and it makes the quoted region obvious at a
    glance in the raw file.

    `""` when the report records no failure, so callers can append unconditionally.
    """
    found = junit_failures(path)
    if not found:
        return ""
    out: list[str] = []
    for entry in found[:tests]:
        head = entry["test"] + (" (error)" if entry["kind"] == "error" else "")
        message = entry["message"]
        out.append(f"    {head} — {message}" if message else f"    {head}")
        out.extend(f"    {line}" for line in entry["lines"][:lines])
    if len(found) > tests:
        out.append(f"    …and {len(found) - tests} more failing test(s).")
    return "\n".join(out)
