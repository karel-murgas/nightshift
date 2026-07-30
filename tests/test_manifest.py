"""Tests for `aiteam/manifest.py` — the per-project config (07_portability.md §4).

Three properties carry the weight, and each is a failure mode the plan names by
name rather than a generic round-trip:

* **absence is meaningful** — `[i18n]` omitted must disable the i18n gates, not
  default them into existence against a `core/i18n.py` that is not there;
* **a bounding field is never guessed** — `branches.integration` has no default,
  because `forbidden_bases()` depends on it and a plausible-looking wrong answer
  means the runner builds on a branch nobody chose;
* **a typo is loud** — an unknown key is a config-shaped `derived-not-verified`:
  the maintainer believes the fact is declared and every reader disagrees.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from aiteam import manifest as m


def _write(root: Path, text: str) -> Path:
    (root / m.AI_DIR).mkdir(parents=True, exist_ok=True)
    path = m.manifest_path(root)
    path.write_text(text, encoding="utf-8", newline="")
    return path


FULL = """
[project]
name = "dungeoneer"
source_dirs = ["dungeoneer"]
extra_source_dirs = ["tests", "tools"]
doc_files = ["CLAUDE.md"]

[tests]
dir = "tests"
parallel = true

[branches]
integration = "development_team"
stable = "main"
forbidden_extra = ["dev"]

[board]
root = "Board"

[worker]
harvest_dirs = ["dungeoneer/assets/.tmp"]
fence_env = "DUNGEONEER_FENCE_ALLOW"
integration_checkout_dir = ".dungeoneer-integration"

[memory]
orientation = ["MEMORY.md", "arch.md"]
budget_bytes = 104857600
freshness = [
  { touches = "dungeoneer/core/i18n.py", requires = ".claude/memory/ref_i18n.md" },
]

[layering]
forbid = [{ importer = "dungeoneer.combat", imports = "dungeoneer.rendering" }]

[i18n]
adapter = ".ai/i18n_adapter.py"
base = "en"
targets = ["cs", "es"]
"""


# --- reading the whole thing -------------------------------------------------

def test_every_section_of_the_documented_sketch_round_trips(tmp_path):
    """§4's example manifest is the acceptance shape: if the doc's own sketch does
    not load, the doc and the schema have already diverged on day one."""
    _write(tmp_path, FULL)
    manifest = m.load(tmp_path)

    assert manifest.project.name == "dungeoneer"
    assert manifest.project.source_dirs == ("dungeoneer",)
    assert manifest.project.extra_source_dirs == ("tests", "tools")
    assert manifest.tests.dir == "tests" and manifest.tests.parallel is True
    assert manifest.branches.integration == "development_team"
    assert manifest.branches.forbidden_extra == ("dev",)
    assert manifest.board.root == "Board"
    assert manifest.worker.fence_env == "DUNGEONEER_FENCE_ALLOW"
    assert manifest.memory.budget_bytes == 104857600
    assert manifest.memory.freshness[0].requires == ".claude/memory/ref_i18n.md"
    assert manifest.layering[0].imports == "dungeoneer.rendering"
    assert manifest.i18n is not None and manifest.i18n.targets == ("cs", "es")


def test_paths_resolve_against_the_root_the_manifest_was_read_from(tmp_path):
    """The root is carried on the manifest rather than passed alongside it, so that
    no caller can pair these relative paths with a different tree."""
    _write(tmp_path, FULL)
    manifest = m.load(tmp_path)
    assert manifest.tests_path == tmp_path / "tests"
    assert manifest.board_path == tmp_path / "Board"
    assert manifest.project_gates_path == tmp_path / ".ai" / "gates"


def test_an_empty_manifest_is_valid_and_all_defaults(tmp_path):
    """D6's bar is a repo containing zero files of either project. An empty file is
    the manifest such a repo has, and it must load rather than raise."""
    _write(tmp_path, "")
    manifest = m.load(tmp_path)
    assert manifest.tests.dir == "tests"
    assert manifest.branches.stable == "main"
    assert manifest.board.root == "Board"
    assert manifest.project.name == tmp_path.name


# --- absence is meaningful ---------------------------------------------------

def test_omitting_the_i18n_table_leaves_no_i18n_config_at_all(tmp_path):
    """§4: "omit the whole table and the 3 gates never register". `None` rather than
    a defaulted `I18n` is what makes that check a one-liner at the registration
    site instead of a truthiness question about a half-filled dataclass."""
    _write(tmp_path, "[project]\nname = 'x'\n")
    assert m.load(tmp_path).i18n is None


def test_an_i18n_table_without_an_adapter_is_refused(tmp_path):
    """The adapter is the whole seam (§2b) — the gates cannot read a project's
    strings without one, so a table declaring languages but no reader is a
    half-configuration that would fail later, at gate time, with a worse message."""
    _write(tmp_path, "[i18n]\nbase = 'en'\ntargets = ['cs']\n")
    with pytest.raises(m.ManifestError, match="adapter"):
        m.load(tmp_path)


def test_the_orientation_budget_is_off_unless_someone_states_a_number(tmp_path):
    """doc 10 §4: a budget picked to fit the current size is the move the gate
    exists to stop. `None` — the gate does not run — is the honest default."""
    _write(tmp_path, "[memory]\norientation = ['MEMORY.md']\n")
    assert m.load(tmp_path).memory.budget_bytes is None


# --- a bounding field is never guessed ---------------------------------------

def test_the_integration_branch_has_no_default(tmp_path):
    _write(tmp_path, "[branches]\nstable = 'main'\n")
    assert m.load(tmp_path).branches.integration is None


def test_reading_an_undeclared_bounding_field_raises_with_the_key_and_the_reason(tmp_path):
    """`require` exists so the failure names the manifest key and what breaks
    without it — the alternative is a default that is indistinguishable from a
    declared value at exactly the point where the difference matters."""
    _write(tmp_path, "[branches]\nstable = 'main'\n")
    manifest = m.load(tmp_path)
    with pytest.raises(m.ManifestError) as exc:
        m.require(manifest, "branches.integration", "the runner needs a base to build on")
    assert "branches.integration" in str(exc.value)
    assert "the runner needs a base to build on" in str(exc.value)
    assert str(m.manifest_path(tmp_path)) in str(exc.value)


def test_require_returns_the_value_when_it_is_declared(tmp_path):
    _write(tmp_path, FULL)
    assert m.require(m.load(tmp_path), "branches.integration", "why") == "development_team"


# --- a typo is loud ----------------------------------------------------------

def test_an_unknown_key_is_an_error_not_a_shrug(tmp_path):
    path = _write(tmp_path, "[tests]\ndirectory = 'tests'\n")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    problems = m.validate(m.parse(data, tmp_path), data)
    assert any(p.level == "error" and p.where == "tests.directory" for p in problems)


def test_an_unknown_table_is_an_error(tmp_path):
    path = _write(tmp_path, "[branch]\nintegration = 'dev'\n")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    problems = m.validate(m.parse(data, tmp_path), data)
    assert any(p.level == "error" and p.where == "branch" for p in problems)


def test_a_wrongly_typed_list_raises_rather_than_being_coerced(tmp_path):
    _write(tmp_path, "[project]\nsource_dirs = 'dungeoneer'\n")
    with pytest.raises(m.ManifestError, match="source_dirs"):
        m.load(tmp_path)


# --- validation catches the things a path can be wrong about -----------------

def test_a_path_that_escapes_the_repo_root_is_an_error(tmp_path):
    _write(tmp_path, "[tests]\ndir = '../elsewhere'\n")
    problems = m.validate(m.load(tmp_path))
    assert any(p.level == "error" and "escapes" in p.message for p in problems)


def test_an_absolute_path_is_an_error(tmp_path):
    absolute = (tmp_path / "tests").as_posix()
    _write(tmp_path, f"[tests]\ndir = '{absolute}'\n")
    problems = m.validate(m.load(tmp_path))
    assert any(p.level == "error" and "absolute" in p.message for p in problems)


def test_a_declared_but_missing_directory_is_a_warning_not_an_error(tmp_path):
    """A manifest written before the directory exists is a normal state during
    setup; a manifest pointing outside the repo never is. The two must not carry
    the same weight, or `doctor` becomes noise."""
    _write(tmp_path, "[tests]\ndir = 'tests'\n")
    problems = m.validate(m.load(tmp_path))
    assert [p.level for p in problems if p.where == "tests.dir"] == ["warning"]


def test_an_integration_branch_equal_to_stable_is_flagged(tmp_path):
    _write(tmp_path, "[branches]\nintegration = 'main'\nstable = 'main'\n")
    problems = m.validate(m.load(tmp_path))
    assert any(p.where == "branches.integration" for p in problems)


def test_errors_sort_before_warnings(tmp_path):
    _write(tmp_path, "[tests]\ndir = '../nope'\n[memory]\norientation = ['gone.md']\n")
    problems = m.validate(m.load(tmp_path))
    assert [p.level for p in problems] == sorted((p.level for p in problems), key=lambda l: l != "error")


# --- finding the root --------------------------------------------------------

def test_the_root_is_found_by_the_manifest_before_the_ai_dir_or_the_git_dir(tmp_path):
    """Falling order of confidence. A nested checkout with its own `.ai/` inside a
    repo that has one too must resolve to the nested one, or every path in the
    manifest is joined to the wrong tree — silently."""
    outer = tmp_path / "outer"
    inner = outer / "sub" / "inner"
    inner.mkdir(parents=True)
    (outer / ".git").mkdir()
    _write(inner, "")
    deeper = inner / "pkg" / "sub"
    deeper.mkdir(parents=True)
    assert m.find_root(deeper) == inner.resolve()


def test_a_bare_git_checkout_still_resolves(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pkg").mkdir()
    assert m.find_root(tmp_path / "pkg") == tmp_path.resolve()


def test_no_root_at_all_raises_rather_than_falling_back_to_the_cwd(tmp_path):
    """The fallback is the dangerous direction: a preflight would validate an empty
    tree, find nothing wrong, and write a receipt that unblocks a push."""
    with pytest.raises(m.ManifestError):
        m.find_root(tmp_path)


def test_loading_a_root_with_no_manifest_names_the_fix(tmp_path):
    (tmp_path / ".ai").mkdir()
    with pytest.raises(m.ManifestError, match="aiteam init"):
        m.load(tmp_path)
