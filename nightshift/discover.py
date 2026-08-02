"""What a repo can be asked about itself — the discovery half of `init` and `doctor`.

`07_portability.md` §5 is the spec, and its design constraint is Karel's own rule
from `hosts.json`: **a check that discovers a fact may probe; a check that bounds
behaviour must be declared.** So everything here proposes, nothing here decides,
and two fields are never proposed at all.

Three confidence levels, and they are not decoration — `init` treats each one
differently:

* `HIGH` — a fact read off the tree. Accepted unless the operator objects.
* `CONFIRM` — a guess. `init` never writes one without being told to. Three fields
  are here, and they split into two kinds:

  - **`branches.integration` is required and is asked every time**, including under
    `--yes`, because nothing downstream works without it and a plausible wrong
    answer is worse than none. Dungeoneer is the cautionary case: `dev` is *stable*
    there and is a forbidden base, which no heuristic would ever guess.
  - **`memory.orientation` and `layering.forbid` are optional**, so the honest
    non-interactive answer is to omit them. Each is a *claim* — "these are the files
    every session loads", "this dependency runs one way" — and a claim nobody made
    is not a default. Until 2026-08-02 both were written silently: `init` on a fresh
    repo emitted a `[[layering.forbid]]` rule its operator had never seen, and this
    docstring said there was only one field in this class.
* `NEVER` — proposed empty or not at all, on purpose. `hosts.capabilities` and
  `hosts.permission_mode`. A probe answers "is the stack present?", and the honest
  response to "no" from an agent with initiative is to install it — 20 GB of
  weights onto a laptop at 3 AM, which no gate would catch.

One more rule with its own line, because it is the one a well-meaning
implementation gets wrong: **`memory.budget_bytes` is never the current size.**
Doc 10 §4's whole lesson is that a budget picked to fit is the move the gate exists
to stop. `budget()` proposes the gate disabled and says why.

Nothing in this module writes. `init` writes; `doctor` compares.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import socket
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from nightshift import manifest as _manifest

HIGH = "high"
CONFIRM = "confirm"
NEVER = "never"

# Directories that hold Python but are never the project's own source. Kept
# small and literal: a heuristic that tried to be clever here would silently
# drop a real package, and a wrong `source_dirs` narrows the test slice, the
# doc scan and the dead-code sweep all at once.
_NOT_SOURCE = frozenset({
    "tests", "test", "docs", "doc", "examples", "scripts", "tools",
    "build", "dist", "venv", ".venv", "env", "site-packages", "node_modules",
})

_INTEGRATION_CANDIDATES = ("dev", "develop", "development", "integration")

# Above this many proposed layering rules, propose none: see `layering()`.
_LAYERING_READABLE = 12


@dataclass(frozen=True)
class Proposal:
    """One manifest field, as discovery would fill it in.

    `value is None` means "discovery has nothing" — which is a result, not a
    failure: `[worker].fence_env` on a repo with no assets directory, or
    `branches.integration` on a repo with one branch. `init` skips a `None` rather
    than writing an empty value that reads as a decision.
    """
    key: str
    value: object
    confidence: str
    why: str

    @property
    def needs_confirmation(self) -> bool:
        return self.confidence == CONFIRM


def _git(root: Path, *args: str) -> str | None:
    """stdout, stripped — or `None` if git could not answer. Never raises: a
    discovery pass that dies on a non-repo is a discovery pass nobody can run in
    the one place it is most needed, which is a directory that is not set up yet."""
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _pyproject(root: Path) -> dict:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


# --- [project] --------------------------------------------------------------


def project_name(root: Path) -> Proposal:
    """`pyproject.toml`'s name if there is one, else the directory's."""
    declared = _pyproject(root).get("project", {}).get("name")
    if isinstance(declared, str) and declared:
        return Proposal("project.name", declared, HIGH, "pyproject.toml [project].name")
    return Proposal("project.name", root.name, HIGH, "the repository directory name")


def source_dirs(root: Path) -> Proposal:
    """Top-level directories holding an `__init__.py`, corroborated by pyproject.

    The corroboration matters more than the probe: a repo can have a stray
    `__init__.py` in a fixtures directory, and `setuptools`' own package list is
    the maintainer having already answered this question once.
    """
    found = sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
        and d.name not in _NOT_SOURCE and (d / "__init__.py").is_file()
    )
    packages = _pyproject(root).get("tool", {}).get("setuptools", {}).get("packages", {})
    include = packages.get("find", {}).get("include", []) if isinstance(packages, dict) else []
    if include:
        # `include = ["nightshift*"]` — match on the stem before the glob.
        stems = {str(pattern).rstrip("*").rstrip(".") for pattern in include}
        agreed = [name for name in found if name in stems]
        if agreed:
            return Proposal("project.source_dirs", agreed, HIGH,
                            "packages with an __init__.py, agreeing with "
                            "pyproject.toml's [tool.setuptools.packages.find]")
    if not found:
        return Proposal("project.source_dirs", None, HIGH,
                        "no top-level package found — declare it by hand; without it "
                        "the test slice, the doc scan and the dead-code sweep all see "
                        "nothing")
    return Proposal("project.source_dirs", found, HIGH,
                    f"top-level {'directory' if len(found) == 1 else 'directories'} "
                    f"with an __init__.py")


def doc_files(root: Path) -> Proposal:
    """The root-level agent instruction files. `CLAUDE.md` is the convention, and
    `branch_role_prose` reads whichever of these exist."""
    present = [name for name in ("CLAUDE.md", "AGENTS.md") if (root / name).is_file()]
    if not present:
        return Proposal("project.doc_files", None, HIGH,
                        "no CLAUDE.md at the root — nothing for the prose gates to read")
    return Proposal("project.doc_files", present, HIGH, "present at the repository root")


# --- [tests] ----------------------------------------------------------------


def tests_dir(root: Path) -> Proposal:
    config = _pyproject(root).get("tool", {}).get("pytest", {}).get("ini_options", {})
    paths = config.get("testpaths") if isinstance(config, dict) else None
    if isinstance(paths, list) and paths:
        return Proposal("tests.dir", str(paths[0]), HIGH,
                        "pyproject.toml [tool.pytest.ini_options].testpaths")
    for name in ("tests", "test"):
        if (root / name).is_dir():
            return Proposal("tests.dir", name, HIGH, f"{name}/ exists")
    return Proposal("tests.dir", None, HIGH, "no tests directory found")


def tests_parallel(root: Path) -> Proposal:
    """Whether xdist is installed — a fact, not a preference.

    The *preference* half is the manifest's own: `Tests.parallel` is a permission,
    and a suite whose tests are file-coupled has to say `--dist loadfile` or find
    out at 3 AM. So discovery answers only "can this machine", and the field's
    docstring carries the rest.
    """
    installed = importlib.util.find_spec("xdist") is not None
    return Proposal("tests.parallel", installed, HIGH,
                    "pytest-xdist is installed" if installed
                    else "pytest-xdist is not installed, so the suite runs serially")


# --- [branches] -------------------------------------------------------------


def stable_branch(root: Path) -> Proposal:
    """What the remote calls its default branch, else what this checkout started on."""
    head = _git(root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if head:
        return Proposal("branches.stable", head.rsplit("/", 1)[-1], HIGH,
                        "git symbolic-ref refs/remotes/origin/HEAD")
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", f"refs/heads/{name}") is not None:
            return Proposal("branches.stable", name, HIGH, f"local branch {name} exists")
    current = _git(root, "branch", "--show-current")
    if current:
        return Proposal("branches.stable", current, HIGH,
                        "no origin/HEAD and no main/master — using the current branch")
    return Proposal("branches.stable", None, HIGH, "not a git repository")


def integration_branch(root: Path, stable: str | None = None) -> Proposal:
    """**The one field that is always confirmed.**

    `forbidden_bases()` is built from it, so a wrong answer means the runner builds
    every card on a branch nobody wanted — and unlike most misconfigurations that
    one produces work, commits and merges before anybody notices. Dungeoneer is the
    case that proves no heuristic suffices: `dev` looks exactly like an integration
    branch and is in fact *stable* there, with `development_team` carrying the work.
    """
    if stable is None:
        proposed = stable_branch(root).value
        stable = proposed if isinstance(proposed, str) else None

    heads = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    branches = [line for line in (heads or "").splitlines() if line.strip()]
    if not branches:
        return Proposal("branches.integration", None, CONFIRM,
                        "no branches to choose from — declare it before running anything")

    named = [name for name in _INTEGRATION_CANDIDATES if name in branches and name != stable]
    if named:
        return Proposal("branches.integration", named[0], CONFIRM,
                        f"a branch called {named[0]} exists — but this is a GUESS from the "
                        f"name, and in Dungeoneer the same guess is wrong: `dev` is stable "
                        f"there")

    ahead: list[tuple[int, str]] = []
    for name in branches:
        if name == stable:
            continue
        count = _git(root, "rev-list", "--count", f"{stable}..{name}")
        if count and count.isdigit() and int(count):
            ahead.append((int(count), name))
    if ahead:
        commits, name = max(ahead)
        return Proposal("branches.integration", name, CONFIRM,
                        f"furthest ahead of {stable} ({commits} commit(s)) — a guess from "
                        f"shape, not from anything declared")
    return Proposal("branches.integration", None, CONFIRM,
                    f"nothing is ahead of {stable}; there may be no integration branch yet")


# --- [worker] ---------------------------------------------------------------


def worker_config(root: Path, name: str | None = None) -> list[Proposal]:
    """All three `[worker]` fields, derived from the project name.

    `harvest_dirs` is proposed only when there is somewhere plausible to harvest
    *from*: it names a convention — where a dispatched worker leaves output for a
    human to approve — and inventing that convention for a project that has none
    would produce the `worker produced nothing` failure with no way to act on it.
    """
    if name is None:
        proposed = project_name(root).value
        name = str(proposed) if proposed else root.name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    env = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "PROJECT"

    out = []
    assets = [d for d in (root / name / "assets", root / "assets") if d.is_dir()]
    if assets:
        rel = assets[0].relative_to(root).as_posix()
        out.append(Proposal("worker.harvest_dirs", [f"{rel}/.tmp"], HIGH,
                            f"{rel}/ exists, so generated output has an obvious staging dir"))
    else:
        out.append(Proposal("worker.harvest_dirs", None, HIGH,
                            "no assets directory — nothing for a worker to harvest, which "
                            "is the right answer for a code-only project"))
    out.append(Proposal("worker.fence_env", f"{env}_FENCE_ALLOW", HIGH,
                        "derived from the project name"))
    out.append(Proposal("worker.integration_checkout_dir", f".{slug}-integration", HIGH,
                        "derived from the project name; a sibling of the repo"))
    return out


# --- [tiers] ----------------------------------------------------------------


_TIER_BLOCK = re.compile(r"^```tier-binding[ \t]*$", re.MULTILINE)
# `init`'s substitution tokens. Their presence means the file is a template.
_UNRENDERED = re.compile(r"\{\{[a-z_]+\}\}")


def tier_binding_doc(root: Path) -> Proposal:
    """A document already carrying a ```tier-binding block, if there is one.

    Searched rather than defaulted, because the default is a path into *Dungeoneer's*
    plan directory — the coupling that made a fresh repo refuse to run with a message
    about a file it had never heard of (`deferral-note-nobody-collected`).
    """
    for doc in sorted(root.glob("**/*.md")):
        parts = doc.relative_to(root).parts
        # `.claude/` and `.ai/` are where this kind of document actually lives; every
        # other dot-directory is a virtualenv or a cache and searching it is a waste.
        if any(p.startswith(".") and p not in (".ai", ".claude") for p in parts):
            continue
        # A file under `templates/` is a document *for another repo*, waiting to be
        # copied or substituted — not this repo's own. `nightshift`'s own checkout
        # is the case that found this: `nightshift/templates/tier-binding.md`
        # carries the block by design, and proposing it made `doctor` report
        # permanent drift against a file nothing in this repo reads. Skipping is the
        # safe direction for a *proposal*: worst case `init` writes a new one.
        if "templates" in parts:
            continue
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:
            continue
        # Belt and braces: an unrendered substitution token means the same thing
        # wherever the file happens to sit.
        if _UNRENDERED.search(text):
            continue
        if _TIER_BLOCK.search(text):
            return Proposal("tiers.binding_doc", doc.relative_to(root).as_posix(), HIGH,
                            "already carries a ```tier-binding block")
    return Proposal("tiers.binding_doc", None, HIGH,
                    "no document carries a ```tier-binding block yet — init writes one")


# --- [memory] ---------------------------------------------------------------


_MEMORY_LINK = re.compile(r"`([^`]+\.md)`|\[[^\]]+\]\(([^)]+\.md)\)")


def memory_orientation(root: Path, docs: list[str] | None = None) -> Proposal:
    """The memory files `CLAUDE.md` names — medium confidence, and it says so.

    "Named by CLAUDE.md" is a decent proxy for "loaded every session" and not the
    same thing, so this is a starting list to edit rather than an answer.
    """
    names = docs if docs is not None else ["CLAUDE.md"]
    found: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _MEMORY_LINK.finditer(text):
            rel = match.group(1) or match.group(2)
            if rel and "memory" in rel and (root / rel).is_file() and rel not in found:
                found.append(rel)
    if not found:
        return Proposal("memory.orientation", None, HIGH,
                        "no memory files named by the doc files")
    return Proposal("memory.orientation", sorted(found), CONFIRM,
                    "named by the project's doc files — a proxy for 'loaded every "
                    "session', which is the thing the budget is actually about")


def budget(root: Path) -> Proposal:
    """**Always proposes the gate off.** Never the current size.

    Doc 10 §4: a budget picked to fit whatever is there today is the move the gate
    exists to stop, and this function is the single place where that rule could be
    quietly broken by a plausible-looking `sum(sizes)`. So it does not compute one,
    and the `why` says what to do instead.
    """
    return Proposal("memory.budget_bytes", None, HIGH,
                    "left unset, which disables the orientation-budget gate. Set it to a "
                    "round number you are willing to defend out loud — NOT to what the "
                    "files happen to total today, which is the failure the gate exists "
                    "to catch")


# --- [layering] -------------------------------------------------------------


def layering(root: Path, dirs: list[str] | None = None) -> Proposal:
    """Import-direction rules the tree **already** satisfies.

    Safe by construction: a rule is only proposed when no module currently breaks
    it, so accepting the whole list can never turn the gate red on the day it is
    written. That also makes it honest about what it is — a snapshot of the
    architecture as built, offered as something to keep true, not an opinion about
    what the architecture should be.
    """
    if dirs is None:
        proposed = source_dirs(root).value
        dirs = list(proposed) if isinstance(proposed, list) else []
    if not dirs:
        return Proposal("layering.forbid", None, HIGH, "no source dirs to analyse")

    edges: set[tuple[str, str]] = set()
    subpackages: set[str] = set()
    for top in dirs:
        base = root / top
        for path in base.glob("**/*.py"):
            rel = path.relative_to(root).with_suffix("")
            owner = ".".join(rel.parts[:2])
            if len(rel.parts) < 2:
                continue
            subpackages.add(owner)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module]
                for name in names:
                    parts = name.split(".")
                    if len(parts) >= 2 and parts[0] == top:
                        target = ".".join(parts[:2])
                        if target != owner:
                            edges.add((owner, target))

    # Propose only the pairs with no edge in either direction *and* at least one
    # in the other, i.e. a real one-way dependency worth pinning. A pair with no
    # traffic at all is not a layering rule, it is two strangers.
    rules: list[dict[str, str]] = []
    for a in sorted(subpackages):
        for b in sorted(subpackages):
            if a >= b:
                continue
            if (a, b) in edges and (b, a) not in edges:
                rules.append({"importer": b, "imports": a})
            elif (b, a) in edges and (a, b) not in edges:
                rules.append({"importer": a, "imports": b})
    if not rules:
        return Proposal("layering.forbid", None, HIGH,
                        "no one-way dependency between subpackages to pin")
    if len(rules) > _LAYERING_READABLE:
        # Measured on Dungeoneer, 2026-08-02: 62 one-way pairs. Writing all of them
        # would be the D4 defect in its purest form — a generated list, accepted
        # unread, that then has to be maintained. "Do not retype a generated list"
        # applies just as much when the generator is this function.
        return Proposal("layering.forbid", None, HIGH,
                        f"{len(rules)} one-way dependencies exist between subpackages — too "
                        f"many to read, so none are proposed. A layering rule is a claim "
                        f"about the architecture and has to be chosen; run "
                        f"`nightshift init --layering` to see the list and pick from it")
    return Proposal("layering.forbid", rules, CONFIRM,
                    f"{len(rules)} direction(s) the tree already satisfies — accepting them "
                    f"cannot turn the gate red today, but each one is a claim about the "
                    f"architecture, so read them")


# --- hosts.json -------------------------------------------------------------


def hostname() -> Proposal:
    """Run on the box, so this one is simply true."""
    return Proposal("hosts.hostname", socket.gethostname(), HIGH, "socket.gethostname()")


def capabilities() -> Proposal:
    """**Never inferred.** Proposed empty, always.

    `hosts.json`' own reasoning, Karel 2026-07-23: a probe answers "is the stack
    present?", and the honest response to "no" from an agent with initiative is to
    install it — 20 GB of weights onto a laptop at 3 AM, which no gate would catch.
    Proposing `[]` is not a limitation of this function; it is the rule.
    """
    return Proposal("hosts.capabilities", [], NEVER,
                    "never inferred — what this box is ALLOWED to do is a decision, and a "
                    "probe would answer a different question")


def permission_mode() -> Proposal:
    """**Never inferred**, and the warning travels with it."""
    return Proposal("hosts.permission_mode", "default", NEVER,
                    "never inferred. `bypassPermissions` is what a code card needs, and IT "
                    "IS THE WHOLE MACHINE, NOT A SANDBOX — a dispatched worker running under "
                    "it can do anything you can. Set it deliberately or not at all")


# --- the three silent-failure checks §5 requires ----------------------------


def gitattributes(root: Path) -> Proposal:
    """`* text=auto eol=lf`, or Windows makes every tool-written file dirty forever.

    Not a preference. Git ships `core.autocrlf=true` on Windows, so a fresh clone
    without this line fails `line_endings` immediately — and it reproduced on the
    very first commit of the empty-repo test on 2026-08-02, before anything else
    had run.
    """
    path = root / ".gitattributes"
    wanted = "* text=auto eol=lf"
    if path.is_file():
        try:
            if any(line.strip() == wanted for line in path.read_text(encoding="utf-8").splitlines()):
                return Proposal("gitattributes", None, HIGH, f".gitattributes declares {wanted}")
        except OSError:
            pass
    return Proposal("gitattributes", wanted, HIGH,
                    "missing — on Windows, git's core.autocrlf=true makes every tool-written "
                    "file permanently dirty without it")


def crlf_worktree(root: Path) -> Proposal:
    """CRLF files already on disk — invisible to git, so nothing else reports them.

    Distinct from the check above and not fixed by it: a checkout predating
    `.gitattributes` has CRLF files whose LF blob normalises to the same content, so
    `git status` shows nothing to commit. The fix is per-machine and syncs nowhere.
    """
    from nightshift.gates import line_endings

    violations = line_endings.check(root)
    # The missing-.gitattributes violation is the other check's; don't double-report.
    files = sorted({v.file for v in violations if v.file != ".gitattributes"})
    if not files:
        return Proposal("crlf_worktree", None, HIGH, "index and working tree are LF")
    return Proposal("crlf_worktree", files, HIGH,
                    f"{len(files)} file(s) are CRLF on disk. Invisible to git status — the "
                    f"blob and the worktree file normalise the same — and the fix is "
                    f"per-machine, so there is nothing to commit and nothing to sync")


# --- the whole sweep --------------------------------------------------------


def survey(root: Path) -> list[Proposal]:
    """Every proposal, in the order `init` presents them.

    One pass, threaded: `source_dirs` feeds `layering`, `project.name` feeds
    `[worker]`, `stable` feeds `integration`, `doc_files` feeds `memory`. Computed
    once and passed down rather than each function re-deriving it, so the survey
    cannot contradict itself — `doctor` diffs this list against a written manifest
    and a self-inconsistent survey would report drift that is not there.
    """
    name = project_name(root)
    dirs = source_dirs(root)
    docs = doc_files(root)
    stable = stable_branch(root)

    dir_list = list(dirs.value) if isinstance(dirs.value, list) else []
    doc_list = list(docs.value) if isinstance(docs.value, list) else []
    stable_name = stable.value if isinstance(stable.value, str) else None

    return [
        name,
        dirs,
        docs,
        tests_dir(root),
        tests_parallel(root),
        stable,
        integration_branch(root, stable_name),
        *worker_config(root, str(name.value) if name.value else None),
        tier_binding_doc(root),
        memory_orientation(root, doc_list),
        budget(root),
        layering(root, dir_list),
        hostname(),
        capabilities(),
        permission_mode(),
        gitattributes(root),
        crlf_worktree(root),
    ]


def as_manifest_tables(proposals: list[Proposal]) -> dict[str, dict]:
    """The subset of a survey that belongs in `.ai/manifest.toml`, grouped by table.

    Drops `None` values (discovery had nothing — writing an empty value would read
    as a decision nobody made) and everything that is not a manifest field: the
    `hosts.*` proposals go to `hosts.json`, and the two environment checks go to
    the operator.
    """
    tables: dict[str, dict] = {}
    for proposal in proposals:
        if proposal.value is None or "." not in proposal.key:
            continue
        table, _, field = proposal.key.partition(".")
        if table in ("hosts",):
            continue
        tables.setdefault(table, {})[field] = proposal.value
    return tables
