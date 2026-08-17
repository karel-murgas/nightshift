"""Tests for the `source_reference_liveness` core gate — a string literal that
looks like a repo path must resolve to something real.

The four cases the card names are each a real incident, so each is pinned here
rather than described: a hook AST-parsing `.ai/runner.py` after the module moved
into the package, a `Path(".claude/plans/ai_team/00_architecture.md")` constant
naming a doc that deliberately does not port, a `<worktree>/.ai/gates/run.py`
invocation, and a fence whose target moved while the fence went on failing open.
Every one of them failed *quietly*: the stale side returned an empty fallback
that read as a real verdict, which is why arriving at "no violations" here has
to be proved rather than assumed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

from nightshift.gates import doc_scan  # noqa: E402
from nightshift.gates import source_reference_liveness as gate  # noqa: E402

import _fixtures  # noqa: E402


def _tree(tmp_path: Path, source: str, *, name: str = "newthing.py",
          real: tuple[str, ...] = ()) -> Path:
    """A repo holding one scanned module under `.ai/`, plus whatever files the
    case needs to exist. `.ai/` is `tooling_dirs`' unconditional member, so this
    needs no manifest — the same shape `test_gate_pytest_invocation` uses."""
    ai = tmp_path / ".ai"
    ai.mkdir(exist_ok=True)
    (ai / name).write_text(source, encoding="utf-8")
    for rel in real:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("real\n", encoding="utf-8")
    doc_scan.clear_caches()
    return tmp_path


def _messages(violations) -> str:
    return " ".join(v.rule for v in violations)


# --- the four historical instances, each reintroduced ------------------------

def test_a_hook_ast_parsing_a_module_that_moved_is_caught(tmp_path):
    """`hook-parsed-a-module-that-moved`, 2026-08-02: `worktree_fence` parsed
    `.ai/runner.py` for a constant after `runner.py` had moved into the package,
    and — failing open by design — went on returning an empty fence."""
    root = _tree(tmp_path, 'import ast\nast.parse(open(".ai/runner.py").read())\n')
    violations = gate.check(root)
    assert violations, "a parse target that no longer exists must be reported"
    assert ".ai/runner.py" in _messages(violations)


def test_a_constant_naming_a_doc_that_does_not_port_is_caught(tmp_path):
    """`deferral-note-nobody-collected`: `tiers.py` held this exact constant, so
    every second project got `refusing to run — …00_architecture.md is missing`
    from the runner's first check."""
    root = _tree(tmp_path, (
        "from pathlib import Path\n"
        'ARCHITECTURE = Path(".claude/plans/ai_team/00_architecture.md")\n'
    ))
    assert gate.check(root), "a Path() constant naming a missing doc must be reported"


def test_a_subprocess_argv_naming_a_moved_script_is_caught(tmp_path):
    """`ran-the-fix-from-a-checkout-without-it`: the gate runner was invoked at
    `<worktree>/.ai/gates/run.py` after it became `python -m nightshift.gates.run`."""
    root = _tree(tmp_path, (
        "import subprocess, sys\n"
        'subprocess.run([sys.executable, ".ai/gates/run.py"])\n'
    ))
    assert gate.check(root)


def test_a_reference_that_resolves_is_not_a_violation(tmp_path):
    """The control. The same literal, with the file actually present, is silent —
    without this the three cases above would pass on a gate that flagged
    everything."""
    root = _tree(tmp_path, 'TARGET = ".ai/runner.py"\n', real=(".ai/runner.py",))
    assert gate.check(root) == []


# --- what must NOT fire ------------------------------------------------------

def test_a_directory_only_claim_resolves_on_its_contents(tmp_path):
    """`.ai/gates` names no file, so a suffix index cannot answer it; a real path
    starting with the claim is the evidence that the directory is live."""
    root = _tree(tmp_path, 'ROOT = ".ai/gates"\n', real=(".ai/gates/card_schema.py",))
    assert gate.check(root) == []


def test_a_templated_path_is_not_a_claim(tmp_path):
    """`templates/` ships paths carrying `{{project}}` and f-strings carry
    `{recipe}`; both resolve nowhere by construction. A match straddling a
    formatted slot is dropped — the literal half of `.ai/recipes/{recipe}.md`
    is a parameterised path, not a stale one."""
    root = _tree(tmp_path, (
        "recipe = 'add-a-perk'\n"
        'SPINE = f".ai/recipes/{recipe}.md"\n'
    ))
    assert gate.check(root) == []


def test_a_static_path_inside_an_f_string_is_still_caught(tmp_path):
    """The other half of the same rule: dropping every f-string would be an
    exemption wide enough to hide the defect."""
    root = _tree(tmp_path, (
        "n = 3\n"
        'MSG = f"{n} violations — see .ai/gates/gone.py"\n'
    ))
    assert gate.check(root)


def test_an_appealed_reference_is_exempt(tmp_path):
    """The card's exemption mechanism: the existing `# gate-ok(<gate>): <reason>`
    marker, counted as debt by `gate_appeals` — never a per-path allowlist file."""
    root = _tree(tmp_path, (
        "# gate-ok(source_reference_liveness): a dated verbatim quote of the\n"
        "# 2026-08-02 error message, correct as a record and not to be rewritten.\n"
        'QUOTED = "refusing to run — .claude/plans/ai_team/00_architecture.md is missing"\n'
    ))
    assert gate.check(root) == []


def test_an_appeal_naming_a_different_gate_does_not_cover_this_one(tmp_path):
    """One marker, one gate. A blanket exemption is how an awkward line ends up
    muting checks nobody was thinking about."""
    root = _tree(tmp_path, (
        "# gate-ok(subprocess_result_checked): a reason long enough to be accepted.\n"
        'TARGET = ".ai/runner.py"\n'
    ))
    assert gate.check(root)


def test_a_bare_docstring_is_not_scanned(tmp_path):
    """A module/class/function docstring is prose about paths, not a claim on
    one. Every historical instance was a live literal — a parse target, a
    `Path(...)`, an argv — so excluding narration costs nothing against the
    evidence and removes a false-positive class that would otherwise need one
    appeal per discursive paragraph."""
    root = _tree(tmp_path, (
        '"""Reads .ai/runner.py, which moved into nightshift/runner.py in 2026-08."""\n'
        "X = 1\n"
    ))
    assert gate.check(root) == []


def test_the_configured_test_directory_is_out_of_scope(tmp_path):
    """Test modules name paths their fixtures create, or deliberately never
    create. Asking whether those resolve against `repo_root` is the wrong
    question — the same call `doc_scan` makes for `Board/`."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text('MISSING = "dungeoneer/does_not_exist.py"\n',
                                     encoding="utf-8")
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "x"\nsource_dirs = ["src"]\nextra_source_dirs = ["tests"]\n'
        '[tests]\ndir = "tests"\n',
        encoding="utf-8")
    doc_scan.clear_caches()
    assert gate.check(tmp_path) == []


# --- `Path(...) / "seg" / "seg"` chains (path-literals-split-across-segments) -

def test_a_stale_div_chain_is_caught(tmp_path):
    """`gate-doc-list-outlived-the-doc-move`, 2026-08-06, reintroduced exactly:
    `branch_role_prose._DOCS` held this literal, spelled as a `/`-chain, months
    after the doc it names had moved — and the gate built the same day for
    stale path literals could not see it, because no single string constant in
    the chain contains a `/`."""
    root = _tree(tmp_path, (
        "from pathlib import Path\n"
        'SESSIONS = Path(".claude") / "plans" / "ai_team" / "SESSIONS.md"\n'
    ))
    violations = gate.check(root)
    assert violations, "a stale Path()/-chain must be reported"
    assert ".claude/plans/ai_team/SESSIONS.md" in _messages(violations)


def test_a_resolving_div_chain_is_not_a_violation(tmp_path):
    """The control: the identical chain, naming a file that is actually there,
    is silent."""
    root = _tree(tmp_path, (
        "from pathlib import Path\n"
        'SESSIONS = Path(".claude") / "memory" / "ai_team" / "SESSIONS.md"\n'
    ), real=(".claude/memory/ai_team/SESSIONS.md",))
    assert gate.check(root) == []


def test_a_div_chain_with_a_non_literal_leaf_is_not_a_claim(tmp_path):
    """`Path(root) / "gates"` — `root` is a variable, not a string constant, so
    the chain is parameterised the same way `f"{worktree}/..."` is: not a
    concrete claim on any one location."""
    root = _tree(tmp_path, (
        "from pathlib import Path\n"
        "root = get_root()\n"
        'GATES = Path(root) / "gates" / "does_not_exist.py"\n'
    ))
    assert gate.check(root) == []


def test_the_real_docs_shape_is_the_regression_case(tmp_path):
    """The exact shape of `branch_role_prose._DOCS` as shipped after the
    2026-08-06 fix: two historical homes for the same doc, one stale, one
    live. The stale entry alone must fail; reverting the fix (only the stale
    entry present) must fail too — that is the shape this card exists for."""
    root = _tree(tmp_path, (
        "from pathlib import Path\n"
        "_DOCS = (\n"
        '    Path(".claude") / "plans" / "ai_team" / "SESSIONS.md",\n'
        '    Path(".claude") / "memory" / "ai_team" / "SESSIONS.md",\n'
        ")\n"
    ), real=(".claude/memory/ai_team/SESSIONS.md",))
    violations = gate.check(root)
    assert violations, "the still-present stale entry must be reported"
    assert ".claude/plans/ai_team/SESSIONS.md" in _messages(violations)
    assert ".claude/memory/ai_team/SESSIONS.md" not in _messages(violations)


def test_the_gate_names_the_appeal_form_in_its_message(tmp_path):
    """A violation nobody knows how to answer becomes a violation somebody
    silences."""
    root = _tree(tmp_path, 'TARGET = ".ai/runner.py"\n')
    assert "gate-ok(source_reference_liveness)" in _messages(gate.check(root))


# --- the gitignore pass, against a real repo ---------------------------------

def _ignoring_repo(tmp_path: Path, source: str, pattern: str) -> Path:
    """A real `git init` repo whose `.gitignore` holds `pattern`, with the
    ignored path deliberately **absent from disk**.

    Real git rather than a mock, for the same reason `test_gate_line_endings`
    uses one: the behaviour under test is `git check-ignore`'s own, and mocking
    it would mock the thing that was wrong.
    """
    repo = tmp_path / "repo"
    (repo / ".ai").mkdir(parents=True)
    _fixtures.git_init(repo)
    (repo / ".gitignore").write_text(pattern + "\n", encoding="utf-8")
    (repo / ".ai" / "newthing.py").write_text(source, encoding="utf-8")
    doc_scan.clear_caches()
    return repo


def test_a_runtime_dir_matches_its_directory_only_gitignore_pattern(tmp_path):
    """The ten-violation regression, 2026-08-07. `.gitignore` carries `.ai/runs/`
    with a trailing slash, which matches **directories only** — and
    `git check-ignore` cannot tell a path is a directory when nothing exists at
    it, which is the normal case for a path created at runtime. So probing the
    candidate exactly as the source wrote it (`.ai/runs`, no slash) missed, and
    the gate reported ten violations in `runner.py`, `digest.py` and
    `hooks/correction_prompt.py` — on the very path its own module docstring
    names as the structural case."""
    root = _ignoring_repo(tmp_path, 'LOGS = ".ai/runs"\n', ".ai/runs/")
    assert not gate.check(root), (
        "a gitignored runtime directory must resolve structurally, even when the "
        "pattern is directory-only and the directory does not exist yet")


def test_the_directory_form_of_a_candidate_still_resolves(tmp_path):
    """The probe added for the case above must not break the spelling that
    already worked — a candidate written with its trailing slash."""
    root = _ignoring_repo(tmp_path, 'LOGS = ".ai/runs/"\n', ".ai/runs/")
    assert not gate.check(root)


def test_a_path_no_gitignore_covers_is_still_a_violation(tmp_path):
    """The probe widens what resolves, so pin what must not: adding the
    directory form must not make every absent path look ignored."""
    root = _ignoring_repo(tmp_path, 'TARGET = ".ai/runner.py"\n', ".ai/runs/")
    assert gate.check(root), "an absent path no pattern covers is still debt"
    assert ".ai/runner.py" in _messages(gate.check(root))
