"""One test per finding of the 2026-08-02 review, so none of them can come back.

They have almost nothing in common as *code*, and everything in common as
*failure*: each was a check or a command that had quietly stopped being about the
repo it was pointed at, and every one of them reported success while doing so.
That is `check-stopped-checking`, the second-largest class in the corrections log,
and the reason it keeps recurring is that the failing direction is silence.

Grouped by finding rather than by module for that reason — the shape is the
subject, and reading them in a row is the point.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import digest, discover, init, manifest as _manifest, reconcile, stale_sweep
from nightshift.gates import import_layering, memory_freshness, orientation_budget

REPO = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, encoding="utf-8", errors="replace")


def _project(tmp_path: Path, manifest: str = "", *, board: bool = True) -> Path:
    repo = tmp_path / "proj"
    (repo / ".ai").mkdir(parents=True)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / ".ai" / "manifest.toml").write_text(
        manifest or '[project]\nsource_dirs = ["pkg"]\n', encoding="utf-8", newline="\n")
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    if board:
        (repo / "Board" / "tasks").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


# --- the entry points operated on the framework repo, not yours -----------------
#
# `Path(__file__).resolve().parent.parent`. Correct while the modules lived in the
# project's `.ai/`; installed as a package it is a directory inside the framework's
# own checkout. `reconcile` then reported "Board is consistent" from inside every
# consuming project — it fails *open*, no board found, no actions, exit 0 — and
# `digest` wrote `Digest.md` into the framework repo. `manifest.find_root`'s own
# docstring is the standing warning; these three were missed by the sweep that
# wrote it.


CARD = """---
id: a-card
state: review
---

A card sitting in `tasks/` while claiming `review`.
"""


def test_reconcile_reads_the_working_directorys_repo(tmp_path, monkeypatch):
    repo = _project(tmp_path)
    (repo / "Board" / "tasks" / "a-card.md").write_text(CARD, encoding="utf-8", newline="\n")

    actions = reconcile.plan(repo)

    assert [a.kind for a in actions] == ["move"], (
        "a card whose state: disagrees with its lane is the one thing this "
        "reports; silence here is the bug it shipped with")

    monkeypatch.chdir(repo)
    assert reconcile.main([]) == 0
    assert reconcile.main(["--root", str(repo)]) == 0


def test_reconcile_help_does_not_die_on_its_own_docstring():
    """`--help` prints the module docstring, which contains `≠`. The stdout
    reconfigure happened *after* `parse_args`, so on a cp1252 console `--help`
    raised UnicodeEncodeError instead of printing help."""
    done = subprocess.run([sys.executable, "-m", "nightshift.reconcile", "--help"],
                          cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr
    assert "--apply" in done.stdout


def test_digest_writes_into_the_project_not_the_framework(tmp_path, monkeypatch):
    repo = _project(tmp_path)
    monkeypatch.chdir(repo)

    assert digest.main([]) == 0

    assert (repo / digest.OUT).is_file(), "the digest belongs to the project"
    assert not (REPO / digest.OUT).exists(), "and never to the framework checkout"


def test_digest_help_writes_nothing():
    """It had no argparse at all, so `--help` was parsed as "no arguments" and
    *wrote the file* — which is how the wrong-root bug was caught."""
    done = subprocess.run([sys.executable, "-m", "nightshift.digest", "--help"],
                          cwd=REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0
    assert "--stdout" in done.stdout
    assert not (REPO / digest.OUT).exists()


def test_stale_sweep_has_a_working_entry_point(tmp_path, monkeypatch):
    """It raised `NameError: _HERE` on every invocation — a name deleted with the
    `__file__`-derived roots whose one remaining use was in `_main`. The runner's
    `--stale` path passes an explicit root and was unaffected, which is exactly why
    nothing noticed."""
    repo = _project(tmp_path)
    monkeypatch.chdir(repo)
    assert stale_sweep._main([]) == 0


# --- [memory] and [layering] were schema nobody read ----------------------------
#
# Both tables were validated, documented in the generated README table, and
# written by `init` — while the three gates that should have read them sat in the
# origin project with hardcoded tables. 07_portability.md §2 caught that
# contradiction when it was written ("the manifest contradicted the table, in a
# document written in one pass") and it survived another three days anyway.


def test_orientation_budget_is_off_until_a_budget_is_declared(tmp_path):
    """`budget_bytes = None` means the gate does not run, and that is the
    recommended starting value — a budget picked to fit today's tree is the move
    the gate exists to stop."""
    repo = _project(tmp_path, '[project]\nsource_dirs = ["pkg"]\n'
                              '\n[memory]\norientation = ["NOTES.md"]\n')
    (repo / "NOTES.md").write_text("x" * 5_000, encoding="utf-8")
    assert orientation_budget.check(repo) == []


def test_orientation_budget_fires_over_the_declared_budget(tmp_path):
    repo = _project(tmp_path, '[project]\nsource_dirs = ["pkg"]\n'
                              '\n[memory]\norientation = ["NOTES.md"]\nbudget_bytes = 100\n')
    (repo / "NOTES.md").write_text("x" * 5_000, encoding="utf-8")
    violations = orientation_budget.check(repo)
    assert len(violations) == 1
    assert "5,000" in violations[0].rule and "100" in violations[0].rule


def test_memory_freshness_fires_and_accepts_any_alternative(tmp_path):
    """Rows sharing a `touches` are alternatives — a memory doc split into an
    orientation file plus history companions means updating either is a real
    update, and demanding the first every time teaches a no-op edit."""
    manifest = ('[project]\nsource_dirs = ["pkg"]\n\n'
                '[[memory.freshness]]\ntouches = "pkg/"\nrequires = "NOTES.md"\n\n'
                '[[memory.freshness]]\ntouches = "pkg/"\nrequires = "HISTORY.md"\n')
    repo = _project(tmp_path, manifest)
    (repo / "NOTES.md").write_text("n\n", encoding="utf-8")
    (repo / "HISTORY.md").write_text("h\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs")

    (repo / "pkg" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    assert memory_freshness.check(repo), "touching pkg/ with neither doc must fire"

    (repo / "HISTORY.md").write_text("h\nmore\n", encoding="utf-8")
    assert memory_freshness.check(repo) == [], "the second alternative satisfies it"


def test_memory_freshness_added_only_rules_ignore_an_edit(tmp_path):
    """`when = "added"` is the index shape: a NEW feedback file must reach the
    index; editing an existing one need not, or the row is too loud to keep."""
    manifest = ('[project]\nsource_dirs = ["pkg"]\n\n'
                '[[memory.freshness]]\ntouches = "notes/entry_"\n'
                'requires = "INDEX.md"\nwhen = "added"\n')
    repo = _project(tmp_path, manifest)
    (repo / "notes").mkdir()
    (repo / "notes" / "entry_one.md").write_text("one\n", encoding="utf-8")
    (repo / "INDEX.md").write_text("- one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "notes")

    (repo / "notes" / "entry_one.md").write_text("one, revised\n", encoding="utf-8")
    assert memory_freshness.check(repo) == [], "an edit is not an add"

    (repo / "notes" / "entry_two.md").write_text("two\n", encoding="utf-8")
    assert memory_freshness.check(repo), "a new entry must reach the index"


def test_import_layering_reads_the_manifest_and_resolves_relative_imports(tmp_path):
    """The version that shipped matched relative imports with
    `startswith("rendering")` on the raw module string — right for one level and
    wrong for two, and silently right-looking either way."""
    manifest = ('[project]\nsource_dirs = ["pkg"]\n\n'
                '[[layering.forbid]]\nimporter = "pkg"\nimports = "pkg.ui"\n'
                'exempt = ["pkg.ui"]\n')
    repo = _project(tmp_path, manifest)
    for sub in ("core", "ui", "deep"):
        (repo / "pkg" / sub).mkdir()
        (repo / "pkg" / sub / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "deep" / "inner").mkdir()
    (repo / "pkg" / "deep" / "inner" / "__init__.py").write_text("", encoding="utf-8")

    (repo / "pkg" / "ui" / "panel.py").write_text("from pkg.ui import x\n", encoding="utf-8")
    (repo / "pkg" / "core" / "a.py").write_text("import pkg.ui.panel\n", encoding="utf-8")
    (repo / "pkg" / "core" / "b.py").write_text("from ..ui import panel\n", encoding="utf-8")
    (repo / "pkg" / "deep" / "inner" / "c.py").write_text(
        "from ...ui import panel\n", encoding="utf-8")
    (repo / "pkg" / "core" / "ok.py").write_text("from pkg.core import a\n", encoding="utf-8")

    hits = {v.file for v in import_layering.check(repo)}

    assert hits == {"pkg/core/a.py", "pkg/core/b.py", "pkg/deep/inner/c.py"}, hits
    assert "pkg/ui/panel.py" not in hits, "`exempt` carves the layer back out"


def test_layering_exempt_covers_a_package_added_later(tmp_path):
    """Why `exempt` and not one row per importer: a list of importers has to be
    extended by hand, and the package that gets forgotten is exactly the new one
    nobody has thought about yet."""
    manifest = ('[project]\nsource_dirs = ["pkg"]\n\n'
                '[[layering.forbid]]\nimporter = "pkg"\nimports = "pkg.ui"\n'
                'exempt = ["pkg.ui"]\n')
    repo = _project(tmp_path, manifest)
    (repo / "pkg" / "ui").mkdir()
    (repo / "pkg" / "ui" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "brand_new").mkdir()
    (repo / "pkg" / "brand_new" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "brand_new" / "x.py").write_text("import pkg.ui\n", encoding="utf-8")

    assert [v.file for v in import_layering.check(repo)] == ["pkg/brand_new/x.py"]


# --- init wrote guesses nobody was asked about ----------------------------------


def test_init_writes_no_unconfirmed_claim(tmp_path):
    """`layering.forbid` and `memory.orientation` carry `CONFIRM`, and
    `as_manifest_tables` used to take the whole survey — so a fresh repo got a
    `[[layering.forbid]]` rule its operator had never seen, enforced by nothing."""
    repo = _project(tmp_path, board=False)
    (repo / "pkg" / "core").mkdir()
    (repo / "pkg" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "ui").mkdir()
    (repo / "pkg" / "ui" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "ui" / "panel.py").write_text("import pkg.core\n", encoding="utf-8")

    proposals = discover.survey(repo)
    assert any(p.key == "layering.forbid" and p.value for p in proposals), (
        "the fixture must actually give discovery something to propose")

    text = init.manifest_text(init.build_plan(repo, integration="dev",
                                              proposals=proposals).tables)
    assert "layering" not in text

    text = init.manifest_text(init.build_plan(repo, integration="dev", proposals=proposals,
                                              optional={"layering.forbid"}).tables)
    assert "[[layering.forbid]]" in text


def test_declining_a_guess_is_reported_rather_than_silent(tmp_path):
    """The quiet half of "absence is meaningful": if nothing says the rule was not
    written, the operator never learns the gate behind it is switched off."""
    repo = _project(tmp_path, board=False)
    (repo / "pkg" / "core").mkdir()
    (repo / "pkg" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "ui").mkdir()
    (repo / "pkg" / "ui" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "ui" / "panel.py").write_text("import pkg.core\n", encoding="utf-8")

    plan = init.build_plan(repo, integration="dev")

    assert any("layering.forbid: not written" in note for note in plan.notes), plan.notes


def test_confirm_is_three_fields_and_init_asks_about_each(tmp_path, monkeypatch):
    """`discover`'s docstring said "exactly one field is here". There are three,
    and two of them were being written unasked."""
    repo = _project(tmp_path, board=False)
    (repo / "pkg" / "core").mkdir()
    (repo / "pkg" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "ui").mkdir()
    (repo / "pkg" / "ui" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "ui" / "panel.py").write_text("import pkg.core\n", encoding="utf-8")
    # A CLAUDE.md naming a memory file, so `memory.orientation` has something to
    # propose — otherwise it reports `None` and never reaches CONFIRM at all.
    (repo / "memory").mkdir()
    (repo / "memory" / "state.md").write_text("state\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("Read `memory/state.md` first.\n", encoding="utf-8")

    proposals = discover.survey(repo)
    confirm = {p.key for p in proposals if p.needs_confirmation}
    assert confirm == {"branches.integration", "memory.orientation", "layering.forbid"}

    monkeypatch.setattr("builtins.input", lambda _="": "y")
    accepted = init.confirm_optional(proposals, assume_yes=False, interactive=True)
    assert "layering.forbid" in accepted
    assert "branches.integration" not in accepted, (
        "the required field has its own prompt, which is asked even under --yes")


# --- discovery must not mistake a template for a document -----------------------


def test_discovery_ignores_templates(tmp_path):
    """`nightshift/templates/tier-binding.md` carries a ```tier-binding block by
    design. Proposing it made `doctor` report permanent drift in this package's own
    checkout, against a file nothing here reads."""
    repo = _project(tmp_path, board=False)
    (repo / "pkg" / "templates").mkdir()
    (repo / "pkg" / "templates" / "tier-binding.md").write_text(
        "```tier-binding\nworker = sonnet\n```\n", encoding="utf-8")

    assert discover.tier_binding_doc(repo).value is None

    (repo / "docs").mkdir()
    (repo / "docs" / "tiers.md").write_text(
        "```tier-binding\nworker = sonnet\n```\n", encoding="utf-8")
    assert discover.tier_binding_doc(repo).value == "docs/tiers.md"


def test_the_framework_repo_reports_no_drift():
    """The whole point of `doctor`: it must be quiet when nothing is wrong, or it
    stops being read. Permanent drift in the framework's own checkout — from the
    template above — is exactly that failure."""
    from nightshift import doctor

    assert doctor.drift(REPO) == []
