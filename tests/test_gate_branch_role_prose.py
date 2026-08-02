"""Tests for the `branch_role_prose` core gate.

Synthetic repos — the mechanism travels to core even though `_DOCS` still
names two Dungeoneer-specific paths (07_portability.md §8 D3: it degrades to
a no-op on a project that has neither, rather than crashing). A consuming
project's own "are my docs in sync" baseline stays in that project.
"""
from __future__ import annotations

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import branch_role_prose  # noqa: E402


def _repo(tmp_path: Path, claude_md: str, integration: str = "development_team") -> Path:
    manifest = tmp_path / ".ai" / "manifest.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "[branches]\n"
        f'integration = "{integration}"\n'
        'stable = "main"\n'
        'forbidden_extra = ["dev", "master"]\n',
        encoding="utf-8",
    )
    (tmp_path / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    return tmp_path


# --- the drift this gate exists for ---------------------------------------

def test_catches_a_stale_claude_md(tmp_path):
    """The exact shape of the Dungeoneer 2026-07-24 incident: `CLAUDE.md` told
    every session to commit to a branch `forbidden_bases()` rejected as stable."""
    v = branch_role_prose.check(_repo(tmp_path, "## Git workflow\n\n- Always commit to `dev`\n"))
    assert len(v) == 1
    assert "forbidden_bases()" in v[0].rule
    assert "development_team" in v[0].rule


def test_catches_a_wrong_non_stable_branch(tmp_path):
    v = branch_role_prose.check(
        _repo(tmp_path, "- Active integration branch: `feature/old-thing`\n")
    )
    assert len(v) == 1
    assert "integration is `development_team`" in v[0].rule


def test_accepts_the_correct_branch(tmp_path):
    assert branch_role_prose.check(
        _repo(tmp_path, "- Active integration branch: **`development_team`**\n")
    ) == []


# --- the migration `nightshift.branches` was designed for -----------------

def test_flipping_the_manifest_flips_what_is_correct(tmp_path):
    """`nightshift.branches`'s promise: retiring the integration branch is a
    one-line edit. This gate is what makes the prose half of that promise real —
    after the flip, the doc that still names the old branch is the one that
    fails."""
    doc = "- Active integration branch: **`development_team`**\n"
    assert branch_role_prose.check(_repo(tmp_path, doc, integration="development_team")) == []

    v = branch_role_prose.check(_repo(tmp_path, doc, integration="dev"))
    assert len(v) == 1
    assert "integration is `dev`" in v[0].rule


# --- the false-positive brake ---------------------------------------------

def test_a_line_deferring_to_the_source_of_truth_is_always_fine(tmp_path):
    """The pattern the gate wants docs to adopt: name the source, not the
    branch."""
    doc = (
        "- Work on a branch off the **integration branch**; the branch holding\n"
        "  that role is `.ai/manifest.toml`'s `[branches].integration` (today\n"
        "  `development_team`)\n"
    )
    assert branch_role_prose.check(_repo(tmp_path, doc, integration="dev")) == []


def test_deferring_to_the_deleted_branches_py_is_no_longer_an_excuse(tmp_path):
    """`branches.py` was deleted by 07_portability.md §8 step 4. Prose pointing
    at a file that no longer exists is a dangling pointer, not deference, and
    exempting it would let the stalest lines in the tree stay green."""
    doc = "- Active integration branch: `dev` — see `.ai/branches.py`\n"
    v = branch_role_prose.check(_repo(tmp_path, doc, integration="development_team"))
    assert len(v) == 1


def test_discussing_a_branch_without_claiming_a_role_is_not_flagged(tmp_path):
    """Docs discuss branch history all the time. A gate that fired on a retired
    branch's name would be muted within a week."""
    doc = (
        "## History\n\n"
        "- `dev` was the working branch until the quarantine began\n"
        "- PRs go to `main` when they have been tested\n"
        "- We deleted `nightshift-session-b` after merging\n"
    )
    assert branch_role_prose.check(_repo(tmp_path, doc)) == []


def test_an_undeclared_integration_branch_reports_nothing(tmp_path):
    """No declared fact, nothing to check. The gate must not invent one — the
    third shape in `manifest.py`'s docstring."""
    (tmp_path / "CLAUDE.md").write_text("- Always commit to `dev`\n", encoding="utf-8")
    assert branch_role_prose.check(tmp_path) == []

    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[branches]\nstable = "main"\n', encoding="utf-8")
    assert branch_role_prose.check(tmp_path) == []


def test_missing_docs_report_nothing(tmp_path):
    """A project with no `CLAUDE.md`/`SESSIONS.md` at those exact paths is a
    no-op, not a crash — the honest degradation for the one part of this gate
    that is not yet manifest-driven (07_portability.md §8 D3)."""
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[branches]\nintegration = "trunk"\n', encoding="utf-8")
    assert branch_role_prose.check(tmp_path) == []
