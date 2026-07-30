"""`.ai/manifest.toml` — the per-project facts that used to be constants in core
modules (07_portability.md §4).

The whole extraction rests on one measurement: the orchestrator is ~97% project-free,
and what *is* project-specific is roughly forty lines of named constants at the top
of files — never logic. This module is where those forty lines go, so that one
framework repo can serve N projects instead of being forked per project.

**Absence is meaningful, and it is not the same as a default.** Three shapes:

* A table with sensible universal defaults (`[tests]`, `[branches].stable`,
  `[board]`) fills itself in when omitted.
* A table whose absence *disables a feature* returns `None`, never a default.
  `[i18n]` is the case §4 names explicitly: a project with no localisation omits
  the table, the three i18n gates never register, and nothing is lost. A default
  here would register three gates against a `core/i18n.py` that does not exist.
* A field that bounds behaviour is **never defaulted and never guessed**.
  `branches.integration` is the one: `forbidden_bases()` depends on it and a wrong
  answer means the runner builds on a branch nobody wanted. Dungeoneer is the
  cautionary case — `dev` is *stable* there and is a forbidden base, which no
  heuristic would guess. Reading it goes through `require()`, which raises with the
  manifest key in the message rather than returning something plausible.

**`validate()` checks shape, not policy.** Types, unknown keys, paths that escape
the repo root. It deliberately does *not* require the fields a given consumer needs,
because the set of consumers grows one step at a time (§8) and a manifest that is
valid today must not become invalid tomorrow merely because a module that reads a
new key was written. "This module needs that key" is enforced at the point of use,
by `require()`, where the error can say which module wanted it and why.

**Unknown keys are errors.** A typo'd key that silently does nothing is the
`derived-not-verified` failure in config form: the maintainer believes the fact is
declared, every reader disagrees, and nothing says so. The cost of being strict is
that adding a field to the schema is a breaking change for a manifest that already
used that name for something else — which is the loud direction.

Discovery — *proposing* these values by reading a repo — is `nightshift init`, step 5.
Nothing in this module infers anything: it reads what someone wrote down.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "AI_DIR", "MANIFEST_NAME", "ManifestError",
    "Manifest", "Project", "Tests", "Branches", "Board", "Worker",
    "Memory", "FreshnessRule", "LayeringRule", "I18n", "Problem",
    "find_root", "manifest_path", "load", "parse", "validate", "require",
]

# The consuming project's config directory. A framework convention, not a project
# fact, so it is a constant here rather than a manifest field — a manifest that
# declared where the manifest lives would be a bootstrap problem, not a config.
#
# It cannot be a Python package: a leading dot is not an identifier, so `import ai`
# will never find a folder literally named `.ai`. That is why the modules still
# living there are imported flat after a `sys.path` insert (see `bridge.py`) rather
# than as `ai.suite`, and why this package is `nightshift` rather than `ai`.
AI_DIR = ".ai"
MANIFEST_NAME = "manifest.toml"


class ManifestError(RuntimeError):
    """The manifest is missing, malformed, or does not declare something a caller
    needs. Never swallowed into a default — see the module docstring."""


# --- the schema ---------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    """What the code under test is called and where it lives.

    `source_dirs` is the one field §2's re-read found to be *only* a source-dir
    list: `suite.classify` is four prefix checks of which one is the project's,
    and `split_test_files` / `board_test_files` read the real tree and parse
    imports rather than consulting a table. The per-part test globs that a first
    reading expected to find hardcoded are globs *by design* — a newly added
    `test_gate_*.py` must classify without anyone remembering a list.

    `extra_source_dirs` are scanned for symbols (so a doc naming `tests/conftest.py`
    resolves) but are not part of the code slice.
    """
    name: str = ""
    source_dirs: tuple[str, ...] = ()
    extra_source_dirs: tuple[str, ...] = ()
    doc_files: tuple[str, ...] = ("CLAUDE.md",)


@dataclass(frozen=True)
class Tests:
    """`dir` is where pytest is pointed. `parallel` is a *permission*, not a
    detection: whether xdist is installed is a fact the code checks at run time, but
    whether this project's suite may be split across workers at all is a property of
    the suite. Dungeoneer's needs `--dist loadfile` because the default `load` splits
    a file across workers and its game tests flake — a project whose tests are not
    file-coupled loses nothing by saying so, and one that has never checked should
    say `false` rather than find out at 3 AM.
    """
    dir: str = "tests"
    parallel: bool = True


@dataclass(frozen=True)
class Branches:
    """`integration` has no default on purpose (module docstring). `forbidden_extra`
    covers the case that has no heuristic: a branch that is neither the integration
    branch nor `stable` but must still be refused as a base — Dungeoneer's `dev`,
    which *is* stable there while `development_team` carries the work."""
    integration: str | None = None
    stable: str = "main"
    forbidden_extra: tuple[str, ...] = ()


@dataclass(frozen=True)
class Board:
    """Lane names are framework config, not project facts (D5), so they are not
    here — only where the board lives."""
    root: str = "Board"


@dataclass(frozen=True)
class Worker:
    """The three constants that were the entire project-specific content of
    `runner.py`'s 3,597 lines.

    `harvest_dirs` names a *convention* — where a dispatched worker leaves output
    for a human to approve — not a path the runner reasons about; `art` appears
    nowhere in the runner's logic, which is what makes the producer/checker loop
    shippable as core (D1).
    """
    harvest_dirs: tuple[str, ...] = ()
    fence_env: str = ""
    integration_checkout_dir: str = ""


@dataclass(frozen=True)
class FreshnessRule:
    """"Touching `touches` means `requires` should have been updated." One row of
    what is currently `memory_freshness._RULES`."""
    touches: str
    requires: str


@dataclass(frozen=True)
class Memory:
    """`budget_bytes = None` means **the orientation-budget gate does not run**, and
    that is the recommended starting value.

    Not an oversight: doc 10 §4's lesson is that a budget picked to fit the current
    size is the move the gate exists to stop. `nightshift init` must therefore propose
    the gate disabled, or a round number the maintainer states out loud — never
    `sum(current sizes)`.
    """
    orientation: tuple[str, ...] = ()
    budget_bytes: int | None = None
    freshness: tuple[FreshnessRule, ...] = ()


@dataclass(frozen=True)
class LayeringRule:
    """`importer` must not import `imports`. Import paths, not directories."""
    importer: str
    imports: str


@dataclass(frozen=True)
class I18n:
    """Present only if the project has localisation; `Manifest.i18n` is `None`
    otherwise and the three i18n gates never register.

    `adapter` points at a project module supplying the three functions
    `i18n_common` already exposes — `load_strings() -> {lang: {key: value}}`,
    `zone_key_lines() -> {lang: {key: line}}` for `file:line` reporting, and
    `i18n_path()`. That interface is the only place in the whole system where a
    project's *data model* differs rather than its paths, which is why it is an
    adapter rather than another table (§2b). A project storing strings in gettext,
    `.po` files or JSON-per-language writes ~30 lines and gets all three gates.
    """
    adapter: str
    base: str = "en"
    targets: tuple[str, ...] = ()
    untranslated_allowlist: str = ""
    loanwords_denylist: str = ""


@dataclass(frozen=True)
class Manifest:
    """One project's declared facts, plus the repo root they are relative to.

    `root` is carried on the manifest rather than passed alongside it everywhere:
    every path field here is repo-relative, and the pairing of "these paths" with
    "this root" is exactly what a caller must not get to separate.
    """
    root: Path
    project: Project = field(default_factory=Project)
    tests: Tests = field(default_factory=Tests)
    branches: Branches = field(default_factory=Branches)
    board: Board = field(default_factory=Board)
    worker: Worker = field(default_factory=Worker)
    memory: Memory = field(default_factory=Memory)
    layering: tuple[LayeringRule, ...] = ()
    i18n: I18n | None = None

    # --- resolved paths, so no caller re-joins them ---------------------------

    @property
    def tests_path(self) -> Path:
        return self.root / self.tests.dir

    @property
    def board_path(self) -> Path:
        return self.root / self.board.root

    @property
    def ai_path(self) -> Path:
        return self.root / AI_DIR

    @property
    def project_gates_path(self) -> Path:
        """Where this project's own gates live. Core gates ship inside the package;
        these are the ones a project earned from its own observed failures, and §15
        is the reason they can only ever live here."""
        return self.root / AI_DIR / "gates"


@dataclass(frozen=True)
class Problem:
    """One validation finding. `where` is a dotted manifest path (`tests.dir`) so a
    message can be pasted straight back into the file being fixed."""
    level: str          # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.message}"


# --- locating the manifest ----------------------------------------------------


def find_root(start: Path | None = None) -> Path:
    """The repo root, walking up from `start` (default: the working directory).

    Three markers in falling order of confidence: `.ai/manifest.toml`, then `.ai/`,
    then `.git/`. The moved modules used to get this for free as
    `Path(__file__).parent.parent`; installed into site-packages that answer is a
    directory inside the virtualenv, so the root has to be *found* rather than
    derived from where the code happens to sit. Getting this wrong is silent — a
    preflight would validate an empty tree and write a receipt — so it raises
    rather than falling back to the working directory.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / AI_DIR / MANIFEST_NAME).is_file():
            return candidate
    for candidate in (here, *here.parents):
        if (candidate / AI_DIR).is_dir():
            return candidate
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ManifestError(
        f"no repo root above {here} — looked for {AI_DIR}/{MANIFEST_NAME}, "
        f"then {AI_DIR}/, then .git/"
    )


def manifest_path(root: Path) -> Path:
    return root / AI_DIR / MANIFEST_NAME


# --- reading ------------------------------------------------------------------


def _as_str_tuple(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{where} must be a list of strings")
    return tuple(value)


def _table(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ManifestError(f"[{name}] must be a table")
    return value


def _unknown(table: dict, known: tuple[str, ...], where: str) -> list[Problem]:
    return [Problem("error", f"{where}.{key}",
                    f"unknown key — known keys are {', '.join(known)}")
            for key in sorted(table) if key not in known]


_KNOWN_TABLES = ("project", "tests", "branches", "board", "worker", "memory",
                 "layering", "i18n")
_KNOWN: dict[str, tuple[str, ...]] = {
    "project": ("name", "source_dirs", "extra_source_dirs", "doc_files"),
    "tests": ("dir", "parallel"),
    "branches": ("integration", "stable", "forbidden_extra"),
    "board": ("root",),
    "worker": ("harvest_dirs", "fence_env", "integration_checkout_dir"),
    "memory": ("orientation", "budget_bytes", "freshness"),
    "layering": ("forbid",),
    "i18n": ("adapter", "base", "targets", "untranslated_allowlist", "loanwords_denylist"),
}


def parse(data: dict, root: Path) -> Manifest:
    """Build a `Manifest` from already-decoded TOML. Raises on anything that cannot
    be represented (a wrong type); *reports* everything else through `validate`."""
    project_t = _table(data, "project")
    tests_t = _table(data, "tests")
    branches_t = _table(data, "branches")
    board_t = _table(data, "board")
    worker_t = _table(data, "worker")
    memory_t = _table(data, "memory")
    layering_t = _table(data, "layering")

    freshness = []
    for row in memory_t.get("freshness", []):
        if not isinstance(row, dict) or "touches" not in row or "requires" not in row:
            raise ManifestError("memory.freshness rows must be { touches = ..., requires = ... }")
        freshness.append(FreshnessRule(str(row["touches"]), str(row["requires"])))

    layering = []
    for row in layering_t.get("forbid", []):
        if not isinstance(row, dict) or "importer" not in row or "imports" not in row:
            raise ManifestError("layering.forbid rows must be { importer = ..., imports = ... }")
        layering.append(LayeringRule(str(row["importer"]), str(row["imports"])))

    # `[i18n]` absent means "this project has no localisation", which is a real and
    # supported answer — not a table to fill with defaults. See the class docstring.
    i18n_t = data.get("i18n")
    i18n = None
    if i18n_t is not None:
        if not isinstance(i18n_t, dict):
            raise ManifestError("[i18n] must be a table")
        if not i18n_t.get("adapter"):
            raise ManifestError(
                "[i18n] declares no `adapter` — the three i18n gates cannot read a "
                "project's strings without one. Omit the whole table if this project "
                "has no localisation."
            )
        i18n = I18n(
            adapter=str(i18n_t["adapter"]),
            base=str(i18n_t.get("base", "en")),
            targets=_as_str_tuple(i18n_t.get("targets", []), "i18n.targets"),
            untranslated_allowlist=str(i18n_t.get("untranslated_allowlist", "")),
            loanwords_denylist=str(i18n_t.get("loanwords_denylist", "")),
        )

    budget = memory_t.get("budget_bytes")
    if budget is not None and not isinstance(budget, int):
        raise ManifestError("memory.budget_bytes must be an integer number of bytes")

    return Manifest(
        root=root,
        project=Project(
            name=str(project_t.get("name", "") or root.name),
            source_dirs=_as_str_tuple(project_t.get("source_dirs", []), "project.source_dirs"),
            extra_source_dirs=_as_str_tuple(project_t.get("extra_source_dirs", []),
                                            "project.extra_source_dirs"),
            doc_files=_as_str_tuple(project_t.get("doc_files", ["CLAUDE.md"]),
                                    "project.doc_files"),
        ),
        tests=Tests(
            dir=str(tests_t.get("dir", "tests")),
            parallel=bool(tests_t.get("parallel", True)),
        ),
        branches=Branches(
            integration=(str(branches_t["integration"])
                         if branches_t.get("integration") else None),
            stable=str(branches_t.get("stable", "main")),
            forbidden_extra=_as_str_tuple(branches_t.get("forbidden_extra", []),
                                          "branches.forbidden_extra"),
        ),
        board=Board(root=str(board_t.get("root", "Board"))),
        worker=Worker(
            harvest_dirs=_as_str_tuple(worker_t.get("harvest_dirs", []), "worker.harvest_dirs"),
            fence_env=str(worker_t.get("fence_env", "")),
            integration_checkout_dir=str(worker_t.get("integration_checkout_dir", "")),
        ),
        memory=Memory(
            orientation=_as_str_tuple(memory_t.get("orientation", []), "memory.orientation"),
            budget_bytes=budget,
            freshness=tuple(freshness),
        ),
        layering=tuple(layering),
        i18n=i18n,
    )


def load(root: Path | None = None) -> Manifest:
    """Read `<root>/.ai/manifest.toml`. `root=None` finds it (`find_root`).

    A missing file raises rather than returning an all-defaults manifest: every
    default in this module is safe *given* that someone chose to run the framework
    here, and silently inventing a whole config for a repo that was never set up is
    how a tool ends up validating the wrong tree.
    """
    root = (root or find_root()).resolve()
    path = manifest_path(root)
    if not path.is_file():
        raise ManifestError(
            f"{path} does not exist — run `nightshift init` in {root} to write one"
        )
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path} is not valid TOML: {exc}") from exc
    return parse(data, root)


# --- validation ---------------------------------------------------------------


def validate(manifest: Manifest, data: dict | None = None) -> list[Problem]:
    """Shape problems, worst first. Empty list means the file is well-formed.

    `data` is the raw decoded TOML when the caller has it; without it the
    unknown-key check cannot run, because a `Manifest` has by then dropped
    everything it did not recognise. `load` callers that want the full check should
    decode once and call `parse` + `validate(m, data)` — which is what `doctor`
    (step 5) will do.
    """
    problems: list[Problem] = []
    root = manifest.root

    if data is not None:
        for name in sorted(data):
            if name not in _KNOWN_TABLES:
                problems.append(Problem("error", name,
                                        f"unknown table — known tables are "
                                        f"{', '.join(_KNOWN_TABLES)}"))
            elif isinstance(data[name], dict):
                problems.extend(_unknown(data[name], _KNOWN[name], name))

    def check_path(rel: str, where: str, *, must_exist: bool, kind: str = "path") -> None:
        if not rel:
            return
        if Path(rel).is_absolute():
            problems.append(Problem("error", where,
                                    f"{rel!r} is absolute; every manifest path is "
                                    f"relative to the repo root"))
            return
        resolved = (root / rel).resolve()
        if not resolved.is_relative_to(root.resolve()):
            problems.append(Problem("error", where, f"{rel!r} escapes the repo root"))
            return
        if must_exist and not resolved.exists():
            problems.append(Problem("warning", where, f"{rel!r} does not exist ({kind})"))

    for i, rel in enumerate(manifest.project.source_dirs):
        check_path(rel, f"project.source_dirs[{i}]", must_exist=True, kind="source dir")
    for i, rel in enumerate(manifest.project.extra_source_dirs):
        check_path(rel, f"project.extra_source_dirs[{i}]", must_exist=True, kind="source dir")
    for i, rel in enumerate(manifest.project.doc_files):
        check_path(rel, f"project.doc_files[{i}]", must_exist=True, kind="doc")
    check_path(manifest.tests.dir, "tests.dir", must_exist=True, kind="test dir")
    check_path(manifest.board.root, "board.root", must_exist=False)
    for i, rel in enumerate(manifest.worker.harvest_dirs):
        check_path(rel, f"worker.harvest_dirs[{i}]", must_exist=False)
    for i, rel in enumerate(manifest.memory.orientation):
        check_path(rel, f"memory.orientation[{i}]", must_exist=True, kind="orientation doc")
    for i, rule in enumerate(manifest.memory.freshness):
        check_path(rule.touches, f"memory.freshness[{i}].touches", must_exist=True)
        check_path(rule.requires, f"memory.freshness[{i}].requires", must_exist=True)
    if manifest.i18n is not None:
        check_path(manifest.i18n.adapter, "i18n.adapter", must_exist=True, kind="adapter module")
        check_path(manifest.i18n.untranslated_allowlist, "i18n.untranslated_allowlist",
                   must_exist=True, kind="allowlist")
        check_path(manifest.i18n.loanwords_denylist, "i18n.loanwords_denylist",
                   must_exist=True, kind="denylist")
        if manifest.i18n.base in manifest.i18n.targets:
            problems.append(Problem("error", "i18n.targets",
                                    f"{manifest.i18n.base!r} is the base language; a "
                                    f"language cannot be its own translation target"))

    if manifest.memory.budget_bytes is not None and manifest.memory.budget_bytes <= 0:
        problems.append(Problem("error", "memory.budget_bytes",
                                "must be positive; omit the key to leave the gate off"))

    if manifest.branches.integration == manifest.branches.stable:
        problems.append(Problem("warning", "branches.integration",
                                "same as branches.stable — work would be based on the "
                                "stable branch, which forbidden_bases() exists to prevent"))

    problems.sort(key=lambda p: (p.level != "error", p.where))
    return problems


def require(manifest: Manifest, dotted: str, reason: str) -> object:
    """Read a field that has no safe default, or raise saying who wanted it and why.

    The alternative — a default — is the failure mode §5 describes for
    `branches.integration`: a heuristic answer is indistinguishable from a declared
    one at the point where it matters, and the runner builds on a branch nobody
    chose. `reason` is not decoration; it is the half of the message that tells the
    maintainer what breaks if they leave the key out.
    """
    node: object = manifest
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            raise ManifestError(
                f"{manifest_path(manifest.root)} declares no `{dotted}` — {reason}"
            )
    if node == "" or node == ():
        raise ManifestError(
            f"{manifest_path(manifest.root)} declares no `{dotted}` — {reason}"
        )
    return node
