"""Tests for `nightshift/gates/doc_scan.py` and the three gates built on it —
`doc_reference_liveness`, `doc_signature_drift`, `deletion_sweep`.

Travelled from Dungeoneer's `tests/test_staleness_gates.py` at 07_portability.md
§8 step 4, same split as `test_suite.py`: the mechanism is here, the grounding
stayed there. Grounding means the handful of assertions that are *about that
repo* — "these eight names deleted in e2e2f51 still do not resolve", "all three
gates are clean on HEAD", "no board lane is in the doc scope". None of those can
be written in the framework, and all of them are the reason the mechanism is
trusted in that tree.

Everything here is synthetic, which was already this suite's rule before the
move: a mechanism test pinned to a real doc tree fails whenever someone
legitimately edits a doc, and a gate that fails on legitimate edits gets muted.
"""
from __future__ import annotations

from pathlib import Path

from nightshift.gates import deletion_sweep
from nightshift.gates import doc_reference_liveness
from nightshift.gates import doc_scan
from nightshift.gates import doc_signature_drift

import _fixtures


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


# --- scope declarations ---------------------------------------------------

def test_missing_frontmatter_defaults_to_current():
    """§3.1: 'Missing frontmatter defaults to current — the safe direction.'"""
    assert doc_scan.read_scope("# Just a heading\n") == "current"
    assert not doc_scan.is_exempt_file("# Just a heading\n")


def test_history_and_external_scopes_are_exempt_but_detail_is_not():
    for scope, exempt in (("history", True), ("external", True),
                          ("detail", False), ("current", False)):
        text = f"---\nname: x\ndoc_scope: {scope}\n---\n\nbody\n"
        assert doc_scan.read_scope(text) == scope
        assert doc_scan.is_exempt_file(text) is exempt


def test_history_scoped_file_yields_no_references(tmp_path):
    doc = tmp_path / "h.md"
    doc.write_text(
        "---\ndoc_scope: history\n---\n\n`TotallyDeadSymbol` in `gone/file.py`\n",
        encoding="utf-8",
    )
    assert doc_scan.extract_references(doc, tmp_path) == []


# --- stale-ok markers -----------------------------------------------------

def test_marker_covers_to_the_next_heading_only():
    text = "\n".join([
        "line1",                        # 1
        "<!-- stale-ok: a reason -->",  # 2
        "covered",                      # 3
        "## Heading",                   # 4  <- ends coverage
        "not covered",                  # 5
    ])
    assert doc_scan.exempt_lines(text) == {2, 3}


def test_marker_above_a_heading_claims_that_heading_and_its_section():
    text = "\n".join([
        "<!-- stale-ok: a reason -->",  # 1
        "## Heading",                   # 2  claimed
        "covered",                      # 3
        "## Next",                      # 4  ends coverage
        "no",                           # 5
    ])
    assert doc_scan.exempt_lines(text) == {1, 2, 3}


def test_marker_may_wrap_across_lines():
    text = "\n".join([
        "<!-- stale-ok: a long reason",  # 1
        "     continued here -->",       # 2
        "covered",                       # 3
        "# H",                           # 4
    ])
    assert 3 in doc_scan.exempt_lines(text)


def test_reason_is_mandatory():
    """A bare marker with no reason must exempt nothing — §3.1 makes the
    reason the whole point of preferring markers over an allowlist."""
    assert doc_scan.exempt_lines("<!-- stale-ok -->\ncovered?\n") == set()


def test_a_backticked_mention_of_the_syntax_is_not_a_marker():
    """Regression: docs that *explain* the mechanism write the marker inside a
    code span. Parsing that as a real marker both inflated the debt count and
    silently exempted the rest of the section — caught by the full suite on
    2026-07-22 when that file's own prose started exempting `state_history.md`."""
    text = "\n".join([
        "A `<!-- stale-ok: reason -->` marker exempts one section.",  # 1: prose
        "`SomeDeadThing` must still be flagged on line 2",            # 2
    ])
    assert doc_scan.exempt_lines(text) == set()


def test_explicit_end_marker_closes_early():
    text = "\n".join([
        "<!-- stale-ok: r -->",   # 1
        "covered",                # 2
        "<!-- /stale-ok -->",     # 3
        "not covered",            # 4
    ])
    assert doc_scan.exempt_lines(text) == {1, 2, 3}


# --- reference extraction -------------------------------------------------

def _refs(tmp_path, body: str):
    doc = tmp_path / "d.md"
    doc.write_text(body, encoding="utf-8")
    return doc_scan.extract_references(doc, tmp_path)


def test_only_code_shaped_backticks_become_symbols(tmp_path):
    """§3.1's three shapes: ClassName, function_name(), module.attr.
    Backticked English prose must not be flagged."""
    refs = _refs(tmp_path, "`worker` `recipe` `judgment` `ClassName` `snake_name` `mod.attr`\n")
    got = {r.text for r in refs if r.kind == "symbol"}
    assert got == {"ClassName", "snake_name", "mod.attr"}


def test_markdown_link_label_is_not_scanned_for_paths(tmp_path):
    refs = _refs(tmp_path, "See [Meziherní menu.md](../notes/real.md) here\n")
    assert "menu.md" not in {r.text for r in refs}


def test_suffix_continuation_shorthand_expands(tmp_path):
    """`FOO_BAR_ONE`/`_TWO` — the second backtick carries only the tail."""
    refs = _refs(tmp_path, "`pending_crit_chain_ranged`/`_melee` flags\n")
    tail = next(r for r in refs if r.text == "_melee")
    assert "pending_crit_chain_melee" in tail.alts


def test_suffix_continuation_needs_an_earlier_token():
    assert doc_scan._suffix_continuations("_melee", []) == ()
    assert doc_scan._suffix_continuations("NotUnderscored", ["A_B"]) == ()


# --- resolution -----------------------------------------------------------

def test_paths_resolve_package_relative(tmp_path):
    """House style writes `core/i18n.py` for `<package>/core/i18n.py`, so a
    reference is tried both ways. Declared here rather than inherited: with no
    manifest the scanned source dirs are the defaults, and a test that relied on
    those would be the step-4 checklist's first item all over again."""
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "myapp"\nsource_dirs = ["myapp"]\n', encoding="utf-8")
    (tmp_path / "myapp" / "core").mkdir(parents=True)
    (tmp_path / "myapp" / "core" / "i18n.py").write_text("X = 1\n", encoding="utf-8")
    doc_scan.clear_caches()

    assert doc_scan.resolve_path(tmp_path, "core/i18n.py")
    assert doc_scan.resolve_path(tmp_path, "myapp/core/i18n.py")
    assert not doc_scan.resolve_path(tmp_path, "core/no_such_module_xyz.py")
    doc_scan.clear_caches()


def test_gitignored_runner_artefacts_do_not_resolve(tmp_path):
    """2026-07-24: `.ai/runs/` is the runner's own gitignored working state. A
    card correctly names paths inside it (a worker's `prompt-1.md`,
    `stream.jsonl`) that will never exist in the source tree — that is what
    gitignored means. Before this fix they resolved anyway whenever the
    reviewer's own machine happened to have dispatched that exact card, which
    made this gate **non-reproducible**: green on a machine with real
    `.ai/runs/` history, red on a fresh clone or a clean worktree — the exact
    shape `merge_check.py` was built to catch, and did, on its first real run.
    """
    doc_scan.clear_caches()
    runs = tmp_path / ".ai" / "runs" / "some-card" / "attempt-1"
    runs.mkdir(parents=True)
    (runs / "prompt-1.md").write_text("x", encoding="utf-8")

    assert not doc_scan.resolve_path(tmp_path, "prompt-1.md")
    assert not doc_scan.resolve_path(tmp_path, ".ai/runs/some-card/attempt-1/prompt-1.md")
    doc_scan.clear_caches()


def test_the_ai_runs_exclusion_is_a_prefix_not_a_bare_component(tmp_path):
    """The exclusion is the two-segment prefix `.ai/runs`, not the bare
    component `runs` anywhere in a path — a real tracked file elsewhere under
    `.ai/` must still resolve normally. The file used to be `.ai/runner.py`;
    since step 4 that module ships in the package, so the fixture names a
    project-owned gate instead, which is what `.ai/` still holds."""
    doc_scan.clear_caches()
    (tmp_path / ".ai" / "gates").mkdir(parents=True)
    (tmp_path / ".ai" / "gates" / "project_gate.py").write_text("x", encoding="utf-8")

    assert doc_scan.resolve_path(tmp_path, ".ai/gates/project_gate.py")
    doc_scan.clear_caches()


# --- what is in scope -----------------------------------------------------

# Both tests below need a path that resolves to nothing. It must be one that
# CANNOT later become real: they were written on 2026-08-01 naming
# `nightshift/doctor.py` — the file the card in `tasks/` that motivated them
# proposed to build — and it was built the same day, whereupon the negative
# fixture stopped being negative. One test went red; the other kept passing for
# the opposite of its stated reason. So: a name nobody will ever ship.
_NEVER_EXISTS = "nightshift/no_such_module_ever.py"


def test_a_card_naming_something_that_does_not_exist_is_not_a_violation(tmp_path):
    """A card is a plan or a record, never a description of the tree as it
    stands, so asking whether its references resolve is the wrong question of
    the wrong document (`doc_scan.DOC_ROOTS`, 2026-08-01). The case reproduced:
    a `tasks/` card whose `## Approach` names the file it proposes to create.
    Every one of the 19 live violations that day was this shape, and not one was
    a staleness defect."""
    (tmp_path / "Board" / "tasks").mkdir(parents=True)
    (tmp_path / ".claude" / "memory").mkdir(parents=True)
    (tmp_path / "Board" / "tasks" / "c.md").write_text(
        f"---\nid: c\n---\n\n## Approach\n\nBuild `{_NEVER_EXISTS}` and call "
        "`no_such_module_ever.check_worktree()` from preflight.\n", encoding="utf-8")
    doc_scan.clear_caches()
    assert doc_reference_liveness.check(tmp_path) == []
    doc_scan.clear_caches()


def test_a_memory_doc_naming_something_that_does_not_exist_still_is(tmp_path):
    """The other direction, so the change is a re-scoping and not a hole: the docs
    that *do* describe the current tree are still checked. Without this, the test
    above would pass just as well against a gate that had stopped working."""
    (tmp_path / ".claude" / "memory").mkdir(parents=True)
    (tmp_path / ".claude" / "memory" / "arch.md").write_text(
        f"Rendering goes through `{_NEVER_EXISTS}`.\n", encoding="utf-8")
    doc_scan.clear_caches()
    violations = doc_reference_liveness.check(tmp_path)
    assert len(violations) == 1
    assert "no_such_module_ever.py" in str(violations[0])
    doc_scan.clear_caches()


# --- signature drift ------------------------------------------------------

def test_signature_parser_reads_names_in_order_and_ignores_types():
    params = doc_signature_drift._parse_params(
        "(app, params: HackGridParams, on_complete: Callable[[bool, int], None] = None, *args)"
    )
    assert params == ["app", "params", "on_complete", "*args"]


def test_signature_parser_drops_a_non_parameter_list():
    """Quote-or-drop: prose inside the parens is not a signature."""
    assert doc_signature_drift._parse_params("(see the notes above)") is None


# --- deletion sweep -------------------------------------------------------

def test_deletion_sweep_reports_a_dead_name_still_named_by_a_live_doc(tmp_path, monkeypatch):
    """The gate's whole value is firing at deletion time (§3.4), so prove the
    doc-scanning half independently of git: given a dead name, a current-scoped
    doc that still mentions it must fail, and a history-scoped one must not."""
    (tmp_path / ".claude" / "memory").mkdir(parents=True)
    (tmp_path / ".claude" / "memory" / "live.md").write_text(
        "The `WidgetFactory` builds widgets.\n", encoding="utf-8")
    (tmp_path / ".claude" / "memory" / "old.md").write_text(
        "---\ndoc_scope: history\n---\n\n`WidgetFactory` was removed.\n", encoding="utf-8")

    monkeypatch.setattr(
        deletion_sweep, "removed_names",
        lambda root: {"WidgetFactory": "deleted in this branch (w.py)"},
    )
    doc_scan.clear_caches()
    violations = deletion_sweep.check(tmp_path)
    files = {v.file for v in violations}
    assert ".claude/memory/live.md" in files
    assert ".claude/memory/old.md" not in files
    doc_scan.clear_caches()


def test_a_file_edited_then_deleted_does_not_crash_the_sweep(tmp_path):
    """`removed_names` reads a modified file from disk, and a path modified in the
    committed range can be *gone* — the normal mid-change state of any removal.

    Observed 2026-08-17: splitting `test_runner.py` into three modules meant the
    branch carried edits to it and then `git rm`-ed it, and the unguarded
    `read_text` raised FileNotFoundError out of `check()`. That does not fail the
    gate, it takes the whole run down with a traceback — all 23 gates stopped
    reporting because of one file in a state git considers ordinary.

    A real repo rather than a stub, because the bug is in what git says about the
    tree versus what is on disk, which is the one thing a stub cannot disagree
    about. The other `deletion_sweep` test patches `removed_names` out and so
    could never have seen this.
    """
    root = _fixtures.git_init(tmp_path / "proj", branch="main")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8", newline="")
    (pkg / "thing.py").write_text("def alpha():\n    pass\n",
                                  encoding="utf-8", newline="")
    (root / ".ai").mkdir()
    (root / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "proj"\nsource_dirs = ["pkg"]\n',
        encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "work")

    # Edit it, commit that, then delete it — the shape the split produced.
    (pkg / "thing.py").write_text("def alpha():\n    return 1\n",
                                  encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "edit the file")
    _git(root, "rm", "-q", "pkg/thing.py")

    doc_scan.clear_caches()
    dead = deletion_sweep.removed_names(root)   # must not raise
    assert "alpha" in dead, "the deleted file's definitions are still buried"
    assert "thing.py" in dead
    doc_scan.clear_caches()


# --- the marker's own discipline ------------------------------------------

def test_every_exemption_carries_a_written_reason(tmp_path):
    """§3.1 rejects a per-symbol allowlist precisely because it 'encodes no
    reason'. A consuming project asserts this over its own docs; what the
    framework can assert is that the reporting API a project would use exists
    and reports the reason text."""
    (tmp_path / ".claude" / "memory").mkdir(parents=True)
    (tmp_path / ".claude" / "memory" / "m.md").write_text(
        "<!-- stale-ok: a reason long enough to count -->\n"
        "`GhostSymbol` is exempt here.\n", encoding="utf-8")
    doc_scan.clear_caches()
    markers = list(doc_scan.stale_ok_markers(tmp_path))
    assert markers, "the marker in the fixture must be reported"
    for path, line, reason in markers:
        assert len(reason) >= 10, f"{path}:{line} — marker reason too thin: {reason!r}"
    doc_scan.clear_caches()
