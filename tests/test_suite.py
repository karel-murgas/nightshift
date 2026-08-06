"""Tests for `nightshift/suite.py` — the game/system test-selection wall, how
the suite is parallelised, and how its result is judged.

Travelled from Dungeoneer's `tests/test_board_suite.py` at 07_portability.md §8
step 4. What stayed there is the half that is *about that repo* — "the real
tree's system tests really do classify as system" — which is a project's own
grounding and cannot be written here. What is here is the mechanism, and it is
deliberately exercised against a project called `myapp` rather than
`dungeoneer`: a suite that only ever proves the selector works on the repo it
was written in would not have caught `source_dirs` being ignored.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nightshift import suite

_MANIFEST = """
[project]
name = "myapp"
source_dirs = ["myapp"]

[tests]
dir = "tests"

[board]
root = "Board"
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project that has declared where its code, tests and board live."""
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(_MANIFEST, encoding="utf-8")
    (tmp_path / "myapp").mkdir()
    (tmp_path / "tests").mkdir()
    return tmp_path


def _tests_dir(root: Path, *names: str, body: str = "") -> Path:
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    for name in names:
        (tests / name).write_text(body, encoding="utf-8")
    return tests


# --- classify ----------------------------------------------------------------

@pytest.mark.parametrize("path, kind", [
    ("myapp/combat/action.py", suite.GAME),
    ("myapp/rendering/hud.py", suite.GAME),
    (".ai/runner.py", suite.SYSTEM),
    (".ai/gates/card_schema.py", suite.SYSTEM),
    ("Board/tasks/x.md", suite.BOARD),
    ("Board/review/y.md", suite.BOARD),
    ("Board/done/z.md", suite.BOARD),
    ("Board/needs-decision/q.md", suite.BOARD),
    ("Board/README.md", suite.BOARD),
    ("Board/ideas/secret.md", suite.NOTE),
    ("Board/inbox/rough.md", suite.NOTE),
    ("tests/test_combat.py", suite.GAME),
    ("tests/test_board_runner.py", suite.SYSTEM),
    ("tests/test_gate_dispatch_reachability.py", suite.SYSTEM),
    ("tests/test_staleness_gates.py", suite.SYSTEM),
    (".claude/memory/state.md", "other"),
    ("CLAUDE.md", "other"),
    ("Digest.md", "other"),
])
def test_classify(repo, path, kind):
    assert suite.classify(path, repo) == kind


def test_classify_normalises_backslashes_and_dot_prefix(repo):
    assert suite.classify("myapp\\combat\\action.py", repo) == suite.GAME
    assert suite.classify("./.ai/runner.py", repo) == suite.SYSTEM


# --- the manifest is what makes the project half knowable ---------------------


def test_the_source_dirs_come_from_the_manifest_not_a_hardcoded_name(tmp_path):
    """The regression this move could have introduced: a `dungeoneer/` literal
    left in `classify` would keep passing every test written against Dungeoneer
    and silently classify every other project's code as `other`."""
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nsource_dirs = ["src/engine", "plugins"]\n', encoding="utf-8")

    assert suite.classify("src/engine/core.py", tmp_path) == suite.GAME
    assert suite.classify("plugins/thing.py", tmp_path) == suite.GAME
    assert suite.classify("dungeoneer/combat.py", tmp_path) == "other"


def test_a_declared_tests_dir_and_board_root_are_honoured(tmp_path):
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nsource_dirs = ["src"]\n\n[tests]\ndir = "spec"\n\n'
        '[board]\nroot = "kanban"\n', encoding="utf-8")

    assert suite.classify("spec/test_gate_x.py", tmp_path) == suite.SYSTEM
    assert suite.classify("kanban/tasks/x.md", tmp_path) == suite.BOARD
    assert suite.classify("kanban/ideas/x.md", tmp_path) == suite.NOTE
    # ...and the old spellings are now just files nobody claims.
    assert suite.classify("tests/test_gate_x.py", tmp_path) == "other"


def test_no_manifest_classifies_nothing_as_project_code_and_so_selects_all(tmp_path):
    """The safe direction, and the reason `layout` falls back rather than
    raising: a preflight in a half-configured repo should run *too many* tests,
    never refuse to run. With no `source_dirs`, no path is GAME, so `select`
    reaches its "no classifiable code" default."""
    assert suite.classify("anything/at/all.py", tmp_path) == "other"
    assert suite.select({"anything/at/all.py"}, tmp_path).bucket == suite.ALL
    # The framework's own conventions still hold with no manifest at all.
    assert suite.classify(".ai/runner.py", tmp_path) == suite.SYSTEM
    assert suite.classify("Board/tasks/x.md", tmp_path) == suite.BOARD
    assert suite.classify("tests/test_gate_x.py", tmp_path) == suite.SYSTEM


def test_pytest_args_follow_the_declared_tests_dir(tmp_path):
    """The pathspec handed to pytest is relative to the repo it runs in, so a
    project whose tests live in `spec/` must be told `spec/`, not `tests/`."""
    spec = tmp_path / "spec"
    spec.mkdir()
    for name in ("test_combat.py", "test_gate_x.py"):
        (spec / name).write_text("", encoding="utf-8")

    args = suite.Selection(suite.GAME, "").pytest_args(spec)

    assert "spec/" in args
    assert "--ignore=spec/test_gate_x.py" in args


# --- select -------------------------------------------------------------------

def test_game_only_diff_selects_game(repo):
    assert suite.select({"myapp/combat/action.py"}, repo).bucket == suite.GAME


def test_system_only_diff_selects_system(repo):
    assert suite.select({".ai/runner.py", "tests/test_board_runner.py"},
                        repo).bucket == suite.SYSTEM


def test_a_diff_touching_both_halves_selects_all(repo):
    assert suite.select({"myapp/x.py", ".ai/runner.py"}, repo).bucket == suite.ALL


def test_a_game_diff_that_also_edits_memory_docs_still_selects_game(repo):
    """The load-bearing case: nearly every game card also edits `.claude/memory/*.md`
    per the update rules. That must NOT drag it up to ALL."""
    changed = {"myapp/combat/action.py", ".claude/memory/state.md", "CLAUDE.md"}
    assert suite.select(changed, repo).bucket == suite.GAME


def test_a_docs_only_diff_selects_all_to_be_safe(repo):
    assert suite.select({".claude/memory/state.md"}, repo).bucket == suite.ALL


def test_an_empty_diff_selects_none(repo):
    """No paths at all is a different fact than paths that classify as nothing:
    nothing changed, so nothing needs testing (`empty-diff-preflight-runs-
    everything`) — distinct from the unclassifiable-but-nonempty case below,
    which still takes the ALL fallback."""
    selection = suite.select(set(), repo)
    assert selection.bucket == suite.NONE
    assert "nothing changed" in selection.reason


def test_an_unclassifiable_nonempty_diff_still_selects_all(repo):
    """The ALL fallback this card leaves untouched: paths were seen and none of
    them classified as project code, which is "I don't know" and must still run
    everything — not to be confused with the empty-diff case above."""
    selection = suite.select({"README.md"}, repo)
    assert selection.bucket == suite.ALL
    assert "nothing changed" not in selection.reason


# --- the board slice: card content vs. freeform notes -------------------------
#
# A card's validity is enforced by the card_schema *gate*, not by pytest, so a
# diff of card content needs only the board's own tests — and a diff of ideas/
# or inbox/ notes (which the gate handles or never scans) needs no pytest at all.


def test_board_card_only_diff_selects_the_board_slice(repo):
    """The load-bearing case Karel flagged: a card moved between lanes must NOT
    drag the whole ~500-test system suite along."""
    assert suite.select({"Board/tasks/hand-razors.md"}, repo).bucket == suite.BOARD
    assert suite.select({"Board/review/x.md", "Board/done/y.md"}, repo).bucket == suite.BOARD


def test_ideas_or_inbox_only_diff_selects_none(repo):
    """`ideas/`/`inbox/` are notes the gate covers (or never reads) — no test."""
    assert suite.select({"Board/ideas/mine.md"}, repo).bucket == suite.NONE
    assert suite.select({"Board/inbox/rough.md"}, repo).bucket == suite.NONE
    assert suite.select({"Board/ideas/a.md", "Board/inbox/b.md"}, repo).bucket == suite.NONE


def test_a_triage_move_from_inbox_to_a_card_selects_the_board_slice(repo):
    """Triage deletes an inbox note and writes a `tasks/` card — a NOTE path and a
    BOARD path together. The card needs its tests; the pair resolves to BOARD."""
    assert suite.select({"Board/inbox/x.md", "Board/tasks/x.md"}, repo).bucket == suite.BOARD


def test_a_board_change_riding_with_ai_code_stays_system(repo):
    """BOARD's tests are a subset of SYSTEM's, so a `.ai/` change plus a card edit
    is fully covered by the system slice — no drag up to ALL, no coverage lost."""
    assert suite.select({".ai/runner.py", "Board/tasks/x.md"}, repo).bucket == suite.SYSTEM


def test_a_board_change_with_game_code_runs_each_parts_tests(repo):
    """Board tests are not in the game slice, so game + board needs both — the game
    slice plus the board's tests — without dragging in the rest of the system suite
    that neither part can affect. That is the GAME_BOARD composite bucket."""
    assert suite.select({"myapp/x.py", "Board/tasks/x.md"}, repo).bucket == suite.GAME_BOARD
    # game + system, on the other hand, really is the whole suite.
    assert suite.select({"myapp/x.py", ".ai/runner.py"}, repo).bucket == suite.ALL
    assert suite.select({"myapp/x.py", ".ai/x.py", "Board/tasks/x.md"}, repo).bucket == suite.ALL


def test_game_board_args_run_game_tests_and_board_tests_only(tmp_path):
    """The composite slice runs the tests dir but keeps the board files in while
    ignoring every *other* system-only file — so a game test and a board test run,
    a gate test does not."""
    tests = _tests_dir(tmp_path, "test_combat.py", "test_board_schema.py",
                       "test_board_runner.py", "test_gate_x.py", "test_stale_y.py")
    args = suite.Selection(suite.GAME_BOARD, "").pytest_args(tests)
    assert "tests/" in args
    # board files are NOT ignored (they run alongside the game tests)...
    assert "--ignore=tests/test_board_schema.py" not in args
    assert "--ignore=tests/test_board_runner.py" not in args
    # ...a game test is not ignored either...
    assert "--ignore=tests/test_combat.py" not in args
    # ...but the non-board system tests are.
    assert "--ignore=tests/test_gate_x.py" in args
    assert "--ignore=tests/test_stale_y.py" in args


def test_a_game_change_with_an_inbox_note_stays_game(repo):
    """A NOTE path carries no test weight, so it must not push a game diff off the
    game slice — an improvement over the old rule that made every Board/ path system."""
    assert suite.select({"myapp/x.py", "Board/inbox/note.md"}, repo).bucket == suite.GAME


def test_board_notes_plus_docs_stay_safe_rather_than_none(repo):
    """NONE is *only* for a purely-notes diff. Add a memory doc and it is no longer
    'nothing but notes', so it takes the conservative ALL that any docs-only diff
    takes — the NONE optimisation is deliberately not widened to cover docs."""
    assert suite.select({"Board/inbox/n.md", ".claude/memory/state.md"},
                        repo).bucket == suite.ALL


# --- the selection is a union of parts, not a fixed table of combinations -----


def test_every_bucket_label_round_trips_through_its_parts():
    """The label is *derived* from the parts (`_label`), and `_BUCKET_PARTS` is its
    inverse for the bare-constructor path. If they ever disagree, a `--full-tests`
    Selection or a hand-built one would run a different set than its name claims."""
    for label, parts in suite._BUCKET_PARTS.items():
        assert suite._label(parts) == label, (label, suite._label(parts))
        assert suite.Selection(label, "")._parts() == suite._reduce(parts), label


def test_board_folds_into_system_and_game_plus_system_is_all():
    """The two reductions the union rests on, asserted directly rather than trusted:
    board is redundant once system is present, and game+system is every file."""
    assert suite._reduce({suite.SYSTEM, suite.BOARD}) == frozenset({suite.SYSTEM})
    assert suite._label({suite.SYSTEM, suite.BOARD}) == suite.SYSTEM
    assert suite._label({suite.GAME, suite.SYSTEM}) == suite.ALL
    assert suite._label({suite.GAME, suite.SYSTEM, suite.BOARD}) == suite.ALL


def test_pytest_args_are_the_true_union_of_the_parts(tmp_path):
    """Drive every part combination through one synthetic tree and assert the run is
    exactly the union of the parts' files — the property that makes this general
    rather than a list of blessed pairs. `test_gate_x` (system, not board) is the
    canary: it runs iff SYSTEM is in the part set, never for BOARD alone."""
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "runner.py").write_text("", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    files = {
        "test_combat.py": "",                  # pure game
        "test_board_schema.py": "",            # board (system-only, named test_board_*)
        "test_gate_x.py": "import runner\n",   # system, not board
    }
    for name, body in files.items():
        (tests / name).write_text(body, encoding="utf-8")

    def collected(parts):
        sel = suite.Selection.for_parts(parts, "")
        args = sel.pytest_args(tests)
        if args == ["tests/"]:
            return set(files)  # everything
        ignored = {a.split("tests/")[-1] for a in args if a.startswith("--ignore=")}
        if "tests/" in args:                    # game form: tests/ minus ignores
            return set(files) - ignored
        return {a.split("tests/")[-1] for a in args}  # named form

    assert collected({suite.GAME}) == {"test_combat.py"}
    assert collected({suite.BOARD}) == {"test_board_schema.py"}
    assert collected({suite.SYSTEM}) == {"test_board_schema.py", "test_gate_x.py"}
    assert collected({suite.GAME, suite.BOARD}) == {"test_combat.py", "test_board_schema.py"}
    assert collected({suite.SYSTEM, suite.BOARD}) == {"test_board_schema.py", "test_gate_x.py"}
    assert collected({suite.GAME, suite.SYSTEM}) == set(files)


# --- pytest_args --------------------------------------------------------------

def test_game_args_ignore_every_system_test_file(tmp_path):
    tests = _tests_dir(tmp_path, "test_combat.py", "test_board_runner.py", "test_gate_x.py")
    args = suite.Selection(suite.GAME, "").pytest_args(tests)
    assert "tests/" in args
    assert "--ignore=tests/test_board_runner.py" in args
    assert "--ignore=tests/test_gate_x.py" in args
    assert "--ignore=tests/test_combat.py" not in args, "must not ignore a game test"


def test_system_args_name_only_the_system_files(tmp_path):
    tests = _tests_dir(tmp_path, "test_combat.py", "test_board_runner.py", "test_gate_x.py")
    args = suite.Selection(suite.SYSTEM, "").pytest_args(tests)
    assert set(args) == {"tests/test_board_runner.py", "tests/test_gate_x.py"}


def test_all_args_are_just_the_tests_dir(tmp_path):
    tests = _tests_dir(tmp_path)
    assert suite.Selection(suite.ALL, "").pytest_args(tests) == ["tests/"]


def test_board_args_name_only_the_board_test_files(tmp_path):
    tests = _tests_dir(tmp_path, "test_combat.py", "test_board_runner.py",
                       "test_board_schema.py", "test_gate_x.py")
    args = suite.Selection(suite.BOARD, "").pytest_args(tests)
    assert set(args) == {"tests/test_board_runner.py", "tests/test_board_schema.py"}


def test_none_args_are_empty_so_no_pytest_runs(tmp_path):
    """The empty list is the signal every run-site checks for. It is the *only*
    bucket that returns nothing — the others fall back to `['tests/']` — so a caller
    can treat `not args` as an unambiguous 'skip pytest, report a pass'."""
    tests = _tests_dir(tmp_path)
    assert suite.Selection(suite.NONE, "").pytest_args(tests) == []
    # Every other bucket returns at least something, so `not args` really is NONE.
    for bucket in (suite.GAME, suite.SYSTEM, suite.BOARD, suite.ALL):
        assert suite.Selection(bucket, "").pytest_args(tests), bucket


# --- classification by import, not only by name -------------------------------
#
# The hole this section closes: a test file that exercises `.ai/` but is not NAMED
# `test_board_*`/`test_gate_*`/`test_stale*` was classified GAME, so a `.ai/`-only
# diff selected SYSTEM and skipped it. Two real files were in that state.


def test_ai_module_names_are_read_off_the_real_tree(tmp_path):
    (tmp_path / ".ai" / "gates").mkdir(parents=True)
    (tmp_path / ".ai" / "runner.py").write_text("", encoding="utf-8")
    (tmp_path / ".ai" / "gates" / "asset_hygiene.py").write_text("", encoding="utf-8")

    names = suite.ai_module_names(tmp_path)

    assert {"runner", "asset_hygiene"} <= names
    assert "myapp" not in names


def test_the_framework_package_counts_as_a_system_import(tmp_path):
    """`nightshift` is not under `.ai/` and never will be, but a test importing it
    is a system test in exactly the same sense. Without this the extraction opened
    a silent hole: a test importing only the framework would classify GAME, so the
    SYSTEM slice would not name it and a `.ai/`-only diff would skip it."""
    assert "nightshift" in suite.ai_module_names(tmp_path)


def test_without_an_ai_dir_only_the_framework_package_remains(tmp_path):
    """The `.ai/` half is read off the real tree, so an empty tree contributes
    nothing; the framework package is not read off the tree at all."""
    assert suite.ai_module_names(tmp_path) == frozenset({"nightshift"})


def test_a_test_outside_the_globs_that_imports_ai_is_system(repo):
    """The original bug, in synthetic form: a file named outside the conventions
    that imports an `.ai/` module must still be SYSTEM."""
    (repo / ".ai" / "runner.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_self_improvement.py").write_text("import runner\n", encoding="utf-8")

    assert not suite.is_system_test("test_self_improvement.py")
    assert suite.classify("tests/test_self_improvement.py", repo) == suite.SYSTEM


def test_a_game_test_stays_game_under_the_import_check(repo):
    """The other direction matters just as much: an import-aware check that swept a
    game test into the system half would drop it from the GAME slice."""
    (repo / "tests" / "test_recoil.py").write_text("import myapp.combat\n", encoding="utf-8")
    assert suite.classify("tests/test_recoil.py", repo) == suite.GAME


def test_classify_without_a_root_is_name_only(repo):
    """The `repo_root` argument is optional, and omitting it must not start
    guessing — it falls back to the naming convention alone, so a file that is
    system only *by import* reads as game."""
    (repo / ".ai" / "runner.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_self_improvement.py").write_text("import runner\n", encoding="utf-8")
    assert suite.classify("tests/test_self_improvement.py") == suite.GAME


def test_touches_game_and_touches_ai_read_imports_not_prose(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "runner.py").write_text("", encoding="utf-8")
    ai = suite.ai_module_names(tmp_path)
    packages = frozenset({"myapp"})

    real = tests / "test_real.py"
    real.write_text("import runner\nimport myapp.core.settings\n", encoding="utf-8")
    assert suite.touches_ai(real, ai) and suite.touches_game(real, packages)

    # The word appears, but only in a docstring and a string literal — an
    # import-shaped regex would call this a system test and drop it from GAME.
    prose = tests / "test_prose.py"
    prose.write_text('"""About the runner and myapp."""\nx = "import runner"\n',
                     encoding="utf-8")
    assert not suite.touches_ai(prose, ai)
    assert not suite.touches_game(prose, packages)


def test_source_packages_are_the_import_names_not_the_directories():
    """`src/engine` is imported as `engine`; matching the directory string against
    a module name would find nothing and silently drop every straddling file into
    the system half."""
    where = suite.Layout(source_dirs=("src/engine", "plugins"))
    assert where.source_packages == frozenset({"engine", "plugins"})


def test_an_unparseable_test_file_reports_no_imports(tmp_path):
    """A file that will fail collection anyway must not take the selector down."""
    bad = tmp_path / "test_bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    assert suite.touches_ai(bad, frozenset({"runner"})) is False


def test_a_file_importing_both_halves_runs_in_both_slices(repo):
    """No such file existed when this was written; it is handled rather than
    asserted away, because the alternative is that adding one silently drops it
    from a slice."""
    (repo / ".ai" / "runner.py").write_text("", encoding="utf-8")
    tests = repo / "tests"
    (tests / "test_combat.py").write_text("import myapp\n", encoding="utf-8")
    (tests / "test_both.py").write_text("import runner\nimport myapp\n", encoding="utf-8")

    system_only, shared = suite.split_test_files(tests)
    assert [p.name for p in shared] == ["test_both.py"]
    assert "test_both.py" not in [p.name for p in system_only]

    # SYSTEM names it...
    assert "tests/test_both.py" in suite.Selection(suite.SYSTEM, "").pytest_args(tests)
    # ...and GAME does not ignore it.
    assert "--ignore=tests/test_both.py" not in suite.Selection(suite.GAME, "").pytest_args(tests)
    # And a diff touching it resolves to ALL rather than picking one side.
    assert suite.select({"tests/test_both.py"}, repo).bucket == suite.ALL


# --- tests_rel ----------------------------------------------------------------


def test_tests_rel_is_relative_so_a_worktree_caller_cannot_be_misled(repo):
    """Absolute would resolve inside the main checkout, which is how a merge check
    ends up running the tests of the tree it was trying to validate against."""
    assert suite.tests_rel(repo) == "tests"
    assert not Path(suite.tests_rel(repo)).is_absolute()
    assert suite.tests_rel(None) == "tests"


# --- parallel_args ------------------------------------------------------------


def test_parallel_args_are_loadfile_not_the_default_load():
    """`--dist load` splits a file across workers and a stateful suite flakes under
    it; `loadfile` keeps a file's tests on one worker. The distinction is the whole
    reason parallelising is safe, so it is asserted, not assumed."""
    args = suite.parallel_args(available=True, workers=None)
    assert args == ("-n", "auto", "--dist", "loadfile")
    assert args[args.index("--dist") + 1] == "loadfile", "plain `load` would reintroduce the flake"


def test_parallel_args_still_use_loadfile_when_the_worker_count_is_capped():
    """The memory cap changes *how many* workers, never how work is distributed —
    a capped run that quietly switched to `--dist load` would trade the OOM for
    the flake it was already avoiding."""
    args = suite.parallel_args(available=True, workers=6)
    assert args == ("-n", "6", "--dist", "loadfile")


# --- worker_count: the memory cap (2026-07-31) --------------------------------


def test_the_box_that_ran_out_of_memory_is_capped():
    """The real case. 22 logical CPUs, ~10 GB free: `-n auto` spawned 22 workers,
    each importing pygame/numpy/scipy/soundfile, and the suite died with
    `pygame.error: Out of memory`, two MemoryErrors and a crashed worker across 13
    unrelated files. The identical tree at `-n 6` was 2134 passed / 1 skipped."""
    capped = suite.worker_count(22, 10.3)
    assert capped is not None and capped < 22


def test_plenty_of_memory_yields_no_opinion_so_auto_is_kept():
    """This only ever narrows. When memory is not the binding constraint the argv
    must stay byte-identical to the historical `-n auto`, so that a well-provisioned
    box is not silently slowed down by a cap it does not need."""
    assert suite.worker_count(22, 31.5) is None
    assert suite.worker_count(4, 64.0) is None


def test_an_unreadable_probe_falls_back_to_auto():
    """The load-bearing degradation. A box whose memory cannot be read must behave
    exactly as it did before this existed — never `-n 0`, never serial, never an
    exception on the path that decides how to run the tests."""
    assert suite.worker_count(22, None) is None
    assert suite.worker_count(None, 10.0) is None
    assert suite.worker_count(None, None) is None
    assert suite.parallel_args(available=True, workers=None) == suite.XDIST_ARGS


def test_a_tiny_box_never_caps_below_the_floor():
    """`-n 1` is worse than serial (all the xdist overhead, none of the win) and
    `-n 0` is an error. A probe reporting almost nothing free is also more likely
    to be wrong than to be describing a box that cannot run two interpreters."""
    assert suite.worker_count(22, 0.5) == suite.MIN_WORKERS
    assert suite.worker_count(22, 0.0) == suite.MIN_WORKERS
    assert suite.MIN_WORKERS >= 2


def test_the_cap_never_exceeds_the_cpu_count():
    """Memory is a ceiling, not a licence to oversubscribe cores."""
    for cpus in (1, 2, 6, 22):
        capped = suite.worker_count(cpus, 512.0)
        assert capped is None or capped <= cpus


def test_the_memory_probe_is_either_a_positive_number_or_none():
    """Whatever this box is, the probe must not return junk — a negative or zero
    reading would drive the cap to the floor for a reason nobody could see."""
    found = suite.available_memory_gb()
    assert found is None or found > 0


def test_probed_worker_count_agrees_with_the_pure_function():
    """The probing wrapper must add no policy of its own, only the two lookups."""
    import os
    assert suite.probed_worker_count() == suite.worker_count(
        os.cpu_count(), suite.available_memory_gb())


def test_parallel_args_degrade_to_serial_without_xdist():
    """A missing optional plugin must cost speed, not correctness. `pytest -n auto`
    without xdist is `error: unrecognized arguments`, exit 4, no JUnit report —
    which in the preflight would block every push on a box that never installed it."""
    assert suite.parallel_args(available=False) == ()


def test_parallel_args_probes_the_interpreter_and_the_box_by_default():
    """Default resolves against what this box actually has — xdist importable, and
    enough memory for one worker per core — so the injected branches above cannot
    all be wrong about reality at once."""
    if not suite.xdist_available():
        assert suite.parallel_args() == ()
        return
    capped = suite.probed_worker_count()
    expected = suite.XDIST_ARGS if capped is None else ("-n", str(capped), "--dist", "loadfile")
    assert suite.parallel_args() == expected


def test_the_default_argv_is_always_a_usable_pytest_invocation():
    """Whatever this box reports, the result has to be something pytest accepts:
    either no parallel flags at all, or `-n <positive int> --dist loadfile`. This is
    the assertion that would have caught a cap of 0 or a `None` leaking into argv."""
    args = suite.parallel_args()
    if not args:
        return
    assert args[0] == "-n" and args[2:] == ("--dist", "loadfile")
    assert args[1] == "auto" or int(args[1]) >= 1


# --- check_junit --------------------------------------------------------------


def _junit(path: Path, *suites: tuple[int, int, int]) -> Path:
    body = "".join(
        f'<testsuite tests="{t}" failures="{f}" errors="{e}"></testsuite>'
        for t, f, e in suites
    )
    path.write_text(f"<testsuites>{body}</testsuites>", encoding="utf-8")
    return path


def test_check_junit_passes_a_clean_report(tmp_path):
    ok, why = suite.check_junit(_junit(tmp_path / "j.xml", (12, 0, 0)))
    assert ok and why == ""


def test_check_junit_sums_across_several_testsuite_elements(tmp_path):
    """xdist emits one `<testsuite>` per worker; the counts have to add up."""
    ok, why = suite.check_junit(_junit(tmp_path / "j.xml", (10, 0, 0), (5, 1, 0)))
    assert not ok and "1 failure(s)" in why and "15 test(s)" in why


def test_check_junit_counts_errors_as_well_as_failures(tmp_path):
    ok, why = suite.check_junit(_junit(tmp_path / "j.xml", (7, 0, 2)))
    assert not ok and "2 error(s)" in why


def test_zero_collected_is_a_failure_not_a_pass(tmp_path):
    """The silent-green this whole mechanism exists to close: a selection that
    matched nothing must never read as a clean suite."""
    ok, why = suite.check_junit(_junit(tmp_path / "j.xml", (0, 0, 0)))
    assert not ok and "0 tests collected" in why


def test_a_missing_report_is_a_failure(tmp_path):
    ok, why = suite.check_junit(tmp_path / "nope.xml")
    assert not ok and "no JUnit report" in why


def test_an_unreadable_report_is_a_failure(tmp_path):
    bad = tmp_path / "j.xml"
    bad.write_text("<testsuites>not closed", encoding="utf-8")
    ok, why = suite.check_junit(bad)
    assert not ok and "unreadable" in why


def test_junit_total_reports_the_count_and_zero_when_absent(tmp_path):
    assert suite.junit_total(_junit(tmp_path / "j.xml", (10, 0, 0), (5, 0, 0))) == 15
    assert suite.junit_total(tmp_path / "nope.xml") == 0
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites>not closed", encoding="utf-8")
    assert suite.junit_total(bad) == 0


# --- failure evidence (junit_failures / failure_excerpt / _case_id) -----------
#
# Why these exist: until 2026-07-31 the only thing that reached the card and the
# digest was `check_junit`'s count string, and the per-test detail sat unread in
# the same report. Karel went looking for the reason three cards failed overnight
# and found `pytest: 2 failure(s), 0 error(s) across 1860 test(s)` plus a pointer
# to `.ai/runs/`, which is gitignored (so absent on any other machine) and deleted
# by `runner.prune_run_dir` the moment a card is retired (so absent on that one).


def _junit_cases(path: Path, *cases: tuple[str, str, str, str, str]) -> Path:
    """`(classname, name, kind, message, body)` per testcase; kind `""` passes.

    The `<testsuite>` summary counts are derived from the cases rather than passed
    in, because pytest derives them too — a double whose header disagrees with its
    own children tests nothing real, and the first version of this helper said
    `failures="0"` over two failing cases, which made `check_junit` read green.
    """
    body = ""
    for classname, name, kind, message, text in cases:
        inner = f'<{kind} message="{message}">{text}</{kind}>' if kind else ""
        body += f'<testcase classname="{classname}" name="{name}">{inner}</testcase>'
    failures = sum(1 for case in cases if case[2] == "failure")
    errors = sum(1 for case in cases if case[2] == "error")
    path.write_text(f'<testsuites><testsuite tests="{len(cases)}" failures="{failures}" '
                    f'errors="{errors}">{body}</testsuite></testsuites>', encoding="utf-8")
    return path


def test_case_id_rebuilds_a_module_level_node_id(tmp_path):
    """pytest omits the optional `file` attribute under many configs, so the node
    id is reconstructed from the dotted classname — and it has to come back out as
    something that can be pasted after `pytest`."""
    report = _junit_cases(tmp_path / "j.xml",
                          ("tests.test_stairs", "test_cleared", "failure", "boom", "E  x"))
    assert suite.junit_failures(report)[0]["test"] == "tests/test_stairs.py::test_cleared"


def test_case_id_keeps_the_class_segment_for_a_test_in_a_class(tmp_path):
    """`tests.test_stairs.TestRemnants` splits at the capitalised segment: the
    module becomes the path, the class stays a `::` step. Getting this wrong
    yields `tests/test_stairs/TestRemnants.py`, which resolves to nothing."""
    report = _junit_cases(tmp_path / "j.xml",
                          ("tests.test_stairs.TestRemnants", "test_cleared",
                           "failure", "boom", "E  x"))
    assert (suite.junit_failures(report)[0]["test"]
            == "tests/test_stairs.py::TestRemnants::test_cleared")


def test_case_id_prefers_the_file_attribute_when_pytest_records_it(tmp_path):
    """A config that emits `file` is authoritative and must beat the inference."""
    path = tmp_path / "j.xml"
    path.write_text('<testsuites><testsuite tests="1"><testcase classname="whatever.X" '
                    'name="test_y" file="tests/test_real.py"><failure message="m">E  x'
                    '</failure></testcase></testsuite></testsuites>', encoding="utf-8")
    assert suite.junit_failures(path)[0]["test"] == "tests/test_real.py::test_y"


def test_junit_failures_reports_only_the_failing_cases(tmp_path):
    report = _junit_cases(
        tmp_path / "j.xml",
        ("tests.test_a", "test_ok", "", "", ""),
        ("tests.test_a", "test_bad", "failure", "AssertionError: assert 3 == 0", "E  assert 3 == 0"),
        ("tests.test_a", "test_broke", "error", "RuntimeError: no tileset", "E  RuntimeError"),
    )
    found = suite.junit_failures(report)
    assert [f["test"] for f in found] == ["tests/test_a.py::test_bad",
                                          "tests/test_a.py::test_broke"]
    assert [f["kind"] for f in found] == ["failure", "error"]


def test_detail_lines_prefer_the_assertion_over_the_traceback_frames():
    """`E `-marked lines are the assertion; everything above them is the frame
    listing. Head of the marked lines, because an assertion block is front-loaded."""
    repr_text = ("def test_x():\n"
                 ">       assert 3 == 0\n"
                 "E       assert 3 == 0\n"
                 "E         where 3 = len(remnants)\n"
                 "\n"
                 "tests/test_x.py:9: AssertionError\n")
    assert suite._detail_lines(repr_text, 5) == ["E       assert 3 == 0",
                                                 "E         where 3 = len(remnants)"]


def test_detail_lines_fall_back_to_the_tail_when_nothing_is_marked():
    """A collection error has no `E ` block; its message is at the *end*, so the
    fallback takes the tail. One rule for both would keep the wrong end of one."""
    assert suite._detail_lines("frame one\nframe two\nImportError: no module named x\n", 2) == [
        "frame two", "ImportError: no module named x"]


def test_failure_excerpt_indents_every_line(tmp_path):
    """Four spaces does three jobs: renders as a code block in Obsidian with no
    fence, keeps `doc_scan._HEADING` (anchored at column 0) from matching a `#`
    pytest printed — which would end the `stale-ok` exemption early and expose the
    rest of the card to the liveness gate — and marks the quoted region in the raw
    file."""
    report = _junit_cases(tmp_path / "j.xml",
                          ("tests.test_a", "test_bad", "failure", "## not a heading",
                           "E  ## also not a heading"))
    excerpt = suite.failure_excerpt(report)
    assert excerpt
    assert all(line.startswith("    ") for line in excerpt.splitlines())


def test_failure_excerpt_caps_the_number_of_tests_and_says_how_many_it_dropped(tmp_path):
    report = _junit_cases(tmp_path / "j.xml", *[
        (f"tests.test_{i}", "test_x", "failure", f"boom {i}", f"E  boom {i}")
        for i in range(7)
    ])
    excerpt = suite.failure_excerpt(report, tests=2)
    assert "tests/test_0.py" in excerpt and "tests/test_1.py" in excerpt
    assert "tests/test_2.py" not in excerpt
    assert "…and 5 more failing test(s)." in excerpt


def test_failure_excerpt_is_empty_for_a_green_or_missing_report(tmp_path):
    """Callers append it unconditionally, so "nothing failed" must be `""` rather
    than a stray heading with no content under it."""
    green = _junit_cases(tmp_path / "j.xml", ("tests.test_a", "test_ok", "", "", ""))
    assert suite.failure_excerpt(green) == ""
    assert suite.failure_excerpt(tmp_path / "nope.xml") == ""
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites>not closed", encoding="utf-8")
    assert suite.failure_excerpt(bad) == ""
    assert suite.junit_failures(bad) == []


def test_check_junit_names_the_first_failing_test_in_its_verdict(tmp_path):
    """The count is a fact about the run; the test name is the start of the bug.
    Both, because this one string is what the run log and the digest quote."""
    report = _junit_cases(
        tmp_path / "j.xml",
        ("tests.test_a", "test_ok", "", "", ""),
        ("tests.test_stairs", "test_cleared", "failure", "AssertionError: assert 3 == 0",
         "E  assert 3 == 0"),
    )
    ok, why = suite.check_junit(report)
    assert not ok
    assert "1 failure(s)" in why and "2 test(s)" in why
    assert "tests/test_stairs.py::test_cleared" in why


def test_check_junit_still_reports_counts_when_no_testcase_detail_exists(tmp_path):
    """A report with summary counts but no `<testcase>` children (older writers,
    and every existing double in this file) must not lose its verdict to the new
    clause."""
    ok, why = suite.check_junit(_junit(tmp_path / "j.xml", (15, 1, 0)))
    assert not ok and "1 failure(s)" in why and "15 test(s)" in why
