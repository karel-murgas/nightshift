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

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

from nightshift.gates import doc_scan  # noqa: E402
from nightshift.gates import source_reference_liveness as gate  # noqa: E402


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


def test_the_gate_names_the_appeal_form_in_its_message(tmp_path):
    """A violation nobody knows how to answer becomes a violation somebody
    silences."""
    root = _tree(tmp_path, 'TARGET = ".ai/runner.py"\n')
    assert "gate-ok(source_reference_liveness)" in _messages(gate.check(root))
