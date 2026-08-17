"""The framework is subject to its own gates, and the scopes follow the code.

**The one test in this file that matters most is the first**, and it is here
because of what the 2026-08-02 review found: this package had no `.ai/`, so
nothing gated it, and five of the gates it ships were still scoped to `.ai/` —
the directory the orchestrator lived in before 07_portability.md §8 moved 16,800
lines out of it. The result was a gate suite that reported green in three
different repos while reading almost nothing:

* `run_stop_recorded` targeted `.ai/runner.py`, deleted at step 4. Its `check()`
  opens with "no such file → no violations", so it had been passing without
  reading a byte, everywhere, for as long as it had shipped.
* `deletion_sweep` filtered deletions on the literal prefix `dungeoneer/`, and
  `doc_signature_drift` built its symbol table from the literal `("dungeoneer",)`.
  Both are no-ops in any other repo — silently, because a filter that matches
  nothing yields no findings rather than an error.
* the four AST discipline gates covered a consuming project's own `.ai/gates/`
  and nothing else.

Every one of those is the `check-stopped-checking` class, and every one would
have been caught the day this file existed. So the tests below are less about
the individual fixes than about the property that produced them: **a scope is a
question about the manifest, never a literal, and the framework is one of the
repos being asked.**
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import manifest as _manifest

REPO = Path(__file__).resolve().parent.parent

# Gates import their flat neighbours (`appeal_markers`) the way `run.py` arranges,
# so the core gate directory has to be importable before they are imported. Same
# preamble as `test_gate_dead_code.py` and friends.
_GATES = REPO / "nightshift" / "gates"
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

from nightshift.gates import (card_schema, deletion_sweep,  # noqa: E402
                              doc_signature_drift, run_stop_recorded, scope)

import _fixtures  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, encoding="utf-8", errors="replace")


def _repo(tmp_path: Path, manifest: str, name: str = "proj") -> Path:
    """A synthetic project with a manifest, a package and one commit."""
    repo = tmp_path / name
    (repo / ".ai").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / ".ai" / "manifest.toml").write_text(manifest, encoding="utf-8", newline="\n")
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    _fixtures.git_init(repo, autocrlf="false")
    return repo


def _commit(repo: Path, message: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# --- the framework gates itself ------------------------------------------------


def test_the_framework_repo_is_green_under_its_own_gates():
    """`python -m nightshift.gates.run`, in this checkout.

    A subprocess rather than an in-process call, because that is how it is really
    invoked — by the on-save hook, by `preflight`, by a person — and because gate
    discovery mutates `sys.path`, which a test must not do to the suite it is part
    of.

    If this fails, read the violations: they are about *this package*, and they are
    the first thing that has ever been able to say so.
    """
    done = subprocess.run([sys.executable, "-m", "nightshift.gates.run"],
                          cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_framework_declares_itself_as_tooling():
    """`[project].tooling_dirs = ["nightshift"]` is what puts the orchestrator back
    inside the five discipline gates it wrote for itself. Asserted rather than
    assumed: dropping the field turns four gates into no-ops here, and nothing else
    would say so."""
    manifest = _manifest.load(REPO)
    assert manifest.project.tooling_dirs == ("nightshift",)
    dirs = scope.tooling_dirs(REPO)
    assert REPO / "nightshift" in dirs


def test_the_discipline_gates_actually_read_the_package_here():
    """Scope, measured rather than declared: the file count has to be in the
    hundreds, not the single digits it was when `.ai/` was the whole answer."""
    files = scope.tooling_files(REPO)
    assert len(files) > 30, f"only {len(files)} tooling file(s) in scope"
    names = {f.name for f in files}
    assert "runner.py" in names and "preflight.py" in names


def test_run_stop_recorded_finds_the_runner_it_polices():
    """It targeted `.ai/runner.py` — a path that stopped existing at step 4 — and
    `check()` returns [] for a missing file, so it was green on nothing."""
    targets = [f for f in scope.tooling_files(REPO) if f.name == "runner.py"]
    assert targets, "the gate has no runner to read"
    assert run_stop_recorded.check(REPO) == []


# --- tooling scope is config, not a literal -------------------------------------


def test_tooling_scope_is_ai_plus_declared_dirs(tmp_path):
    repo = _repo(tmp_path, '[project]\nsource_dirs = ["pkg"]\ntooling_dirs = ["pkg"]\n')
    (repo / ".ai" / "gates").mkdir()
    (repo / ".ai" / "gates" / "mine.py").write_text("", encoding="utf-8")
    assert scope.tooling_dirs(repo) == (repo / ".ai", repo / "pkg")


def test_tooling_scope_is_just_ai_when_nothing_is_declared(tmp_path):
    """The default every consuming project gets, and it must be byte-identical to
    the literal it replaced — step 4's first checklist item."""
    repo = _repo(tmp_path, '[project]\nsource_dirs = ["pkg"]\n')
    assert scope.tooling_dirs(repo) == (repo / ".ai",)


def test_a_repo_with_no_tooling_at_all_yields_nothing(tmp_path):
    """`()` rather than a crash: there is no tooling here to have an opinion about."""
    repo = tmp_path / "bare"
    repo.mkdir()
    assert scope.tooling_dirs(repo) == ()
    assert scope.tooling_files(repo) == []


# --- the two doc gates follow the manifest --------------------------------------


def test_deletion_sweep_fires_on_a_project_that_is_not_dungeoneer(tmp_path):
    """The regression that made it a no-op everywhere else: a literal
    `"dungeoneer/"` prefix filter."""
    repo = _repo(tmp_path, '[project]\nsource_dirs = ["pkg"]\ndoc_files = ["CLAUDE.md"]\n')
    (repo / "pkg" / "engine.py").write_text("def run(n):\n    return n\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("The engine is `pkg/engine.py`.\n", encoding="utf-8")
    _commit(repo)

    _git(repo, "rm", "-q", "pkg/engine.py")
    violations = deletion_sweep.check(repo)

    assert violations, "a deleted, still-documented module must be reported"
    assert any("engine" in v.rule for v in violations)


def test_doc_signature_drift_fires_on_a_project_that_is_not_dungeoneer(tmp_path):
    """Same shape, the other gate: its symbol table was built from the literal
    `("dungeoneer",)`, so it resolved nothing anywhere else and dropped every
    finding as unresolvable."""
    repo = _repo(tmp_path, '[project]\nsource_dirs = ["pkg"]\ndoc_files = ["CLAUDE.md"]\n')
    (repo / "pkg" / "panel.py").write_text("def draw(n):\n    return n\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(
        "## API\n\n```python\ndef draw(count, style):\n    ...\n```\n", encoding="utf-8")
    _commit(repo)

    violations = doc_signature_drift.check(repo)

    assert violations, "a doc'd signature that does not match the real one must be reported"
    assert "count, style" in violations[0].rule


# --- card_schema follows [board].root -------------------------------------------


CARD = """---
id: a-card
title: "A card"
state: tasks
tier: {tier}
worker: code-thread
recipe: none
unattended: true
created: 2026-08-02
verify: review
---

## Intent

Something.

## Approach

One line.

## Steps

1. Do it.

## Acceptance

- done

## Open Questions

None.
"""


@pytest.mark.parametrize("board_root", ["Board", "Kanban"])
def test_card_schema_reads_the_declared_board_root(tmp_path, board_root):
    """It hardcoded `Path("Board")`, and `check()` returns [] when that is absent.

    So a project that declared `[board].root` — a field `board.py`, `reconcile.py`,
    the runner and `ideas_fence` all honour — got a gate scanning a directory that
    was not there. Measured 2026-08-02: a card with an invalid `tier:` reported
    **All clear** while the runner, reading the real board, called it dispatchable.
    The gate guarding the dispatcher went quiet and the dispatcher did not.
    """
    manifest = '[project]\nsource_dirs = ["pkg"]\n'
    if board_root != "Board":
        manifest += f'\n[board]\nroot = "{board_root}"\n'
    repo = _repo(tmp_path, manifest)
    lane = repo / board_root / "tasks"
    lane.mkdir(parents=True)
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "code-thread.md").write_text("x", encoding="utf-8")

    (lane / "a-card.md").write_text(CARD.format(tier="worker"), encoding="utf-8", newline="\n")
    assert card_schema.check(repo) == [], "a well-formed card must pass"

    (lane / "a-card.md").write_text(CARD.format(tier="nonsense"), encoding="utf-8", newline="\n")
    violations = card_schema.check(repo)
    assert violations, f"an invalid tier must be reported under board root {board_root!r}"
    assert board_root in violations[0].file
