"""Session H (2026-07-23) — the self-improvement loop.

Covers the three gates this session earned from the correction log, plus the
appeal mechanism they share. Two things here are deliberately assertions about
the system's *own bookkeeping* rather than about any consuming project: Sessions
D and G both recorded that those are the assertions that caught their real bugs
(`gate-self-bug`, `git-add-pathspec-noop`), and this session's first run of
`gate_appeals` repeated `gate-self-bug` exactly — the gate flagged the marker
written inside its own module's docstring.

Moved here from Project Tigress's `tests/test_self_improvement.py`
(framework-text-not-copied, phase 2b): these are the framework-mechanism tests
that built their own `tmp_path` world and asserted nothing about that project's
own tree — `appeal_markers`, `corrections.py`, `gate_appeals`, `subprocess_result_checked`,
`stale.stale_phase`, `preflight`'s receipt/slice/reuse machinery. The four
`test_guard_*` cases and their `_guard` helper did not travel: this package's own
`tests/test_hook_preflight_guard.py` already covers `preflight_guard`'s
allow/deny logic far more thoroughly, over synthetic `tmp_path` repos rather
than a subprocess against the caller's real checkout. The tests that stayed in
Project Tigress are the ones that read its own `.ai/gates/`, `.ai/hosts.json`
or `.claude/memory/` — `framework_test_grounding`'s own line.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import audit
from nightshift import stale, verify
from nightshift import corrections, preflight

REPO = Path(__file__).resolve().parent.parent

# Gates import their flat neighbours (`appeal_markers`) the way `run.py` arranges,
# so the core gate directory has to be importable before they are imported. Same
# preamble as `test_self_gating.py` and `test_gate_dead_code.py`.
_GATES = REPO / "nightshift" / "gates"
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import appeal_markers  # noqa: E402
import corrections_log as corrections_log_gate  # noqa: E402
import gate_appeals  # noqa: E402
import subprocess_result_checked  # noqa: E402

import _fixtures


def _log(tmp_path: Path, body: str) -> Path:
    log = tmp_path / ".ai" / "corrections.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(body, encoding="utf-8")
    return log

_NOTE = "a note long enough to clear the forty character minimum here"

def _scan(tmp_path: Path, source: str) -> list[appeal_markers.Appeal]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return appeal_markers.scan_file(path, tmp_path)

def _check_snippet(tmp_path: Path, source: str):
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "gates").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "gates" / "subprocess_result_checked.py").write_text("", encoding="utf-8")
    (tmp_path / ".ai" / "thing.py").write_text(source, encoding="utf-8")
    return subprocess_result_checked.check(tmp_path)

def _git_repo(tmp_path: Path) -> Path:
    """A throwaway repo with the .ai/ scaffolding the preflight needs."""
    def g(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)

    g("init", "-b", "development_team")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    # Mirror a real repo, which gitignores these — otherwise `git add -A` in a test
    # sweeps the preflight's own run artefacts and __pycache__ into the branch diff,
    # and those (a .pyc classifies as `other`) would contaminate every fingerprint.
    (tmp_path / ".gitignore").write_text(".ai/runs/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "corrections.log").write_text("# log\n", encoding="utf-8")
    # A manifest gives this synthetic tree its own source_dirs/tests-dir/branch
    # layout, entirely independent of any consuming project's real one —
    # `source_dirs` is what makes a `project_tigress/` path classify as GAME here;
    # without it every diff widens to ALL and the slice tests cannot observe a
    # selection at all. The name is arbitrary; only its consistent use matters.
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nsource_dirs = ["project_tigress"]\n\n'
        '[tests]\ndir = "tests"\n\n'
        '[branches]\nintegration = "development_team"\n', encoding="utf-8")
    g("add", "-A")
    g("commit", "-m", "base")
    return tmp_path

def _with_origin(repo: Path) -> Path:
    """Give `repo` an `origin` holding today's tip of the integration branch."""
    origin = repo.parent / f"{repo.name}-origin.git"
    # gate-ok(fixture_rebuild): a bare origin, which the shared template does not build.
    subprocess.run(["git", "init", "--bare", str(origin)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)],
                   cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "push", "-q", "origin", "development_team"],
                   cwd=repo, capture_output=True, check=True)
    return origin

def _log_a_correction(repo: Path) -> None:
    log = repo / ".ai" / "corrections.log"
    log.write_text(log.read_text(encoding="utf-8") +
                   "2026-07-31 | s | silent-noop | claude | n/a | a real note about a real thing here\n",
                   encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "log it"], cwd=repo, capture_output=True)

def _repo_with_tests(tmp_path: Path) -> Path:
    """A throwaway repo that can actually run a two-file pytest suite: one game
    test, one system test, so a slice selection is observable in what runs."""
    repo = _git_repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_game_side.py").write_text(
        "def test_game():\n    assert True\n", encoding="utf-8")
    (tests / "test_board_side.py").write_text(
        "def test_system():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "tests"], cwd=repo, capture_output=True)
    # `_git_repo` checks out the integration branch itself, so without a remote
    # there is no honest basis to diff against and `_changed_paths` widens to ALL
    # by design — which would make every slice here indistinguishable. A real
    # checkout has an `origin`; give the fixture one so slice selection is
    # observable at all.
    _with_origin(repo)
    return repo

def _run_pytest_slice(root: Path, base: str, full: bool = False, fresh: bool = False):
    """Local shim for `preflight._run_pytest`, whose signature grew `changed`,
    `how` and `merge_base` (`empty-diff-preflight-runs-everything`) so the
    reuse-cache's merge-base can no longer diverge from `_changed_paths`'s own —
    a second, independent `git merge-base HEAD <base>` call was the confirmed
    cause of the card's 693-test observation. `run_checks` computes those three
    once and threads them through; these tests want the pre-existing two-arg
    call, so this shim does the same computation `run_checks` does."""
    changed, how, merge_base = preflight._changed_paths(root, base)
    return preflight._run_pytest(root, base, changed, how, merge_base, full, fresh)

def _junit_names(repo: Path) -> set[str]:
    """Which test files the last preflight run actually executed, read out of its
    own JUnit report — the artefact, not the log."""
    import xml.etree.ElementTree as ET

    root = ET.parse(repo / preflight.RUN_DIR / "junit.xml").getroot()
    return {case.get("classname", "").split(".")[-1] for case in root.iter("testcase")}

def _feature_with_game_change(repo: Path) -> None:
    """Branch off the integration base and commit a game-side edit, so the slice
    resolves to `game` and a later system-side edit is observably a second part."""
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    (repo / "project_tigress").mkdir(exist_ok=True)
    (repo / "project_tigress" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "game change"], cwd=repo, capture_output=True)

def test_a_malformed_line_is_reported_not_raised(tmp_path):
    """A reader that dies on one bad hand-appended line is a reader nobody
    runs twice."""
    log = tmp_path / ".ai" / "corrections.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "# header\n"
        "2026-07-23 | slug | silent-noop | karel | yes-now | "
        + "a note long enough to clear the minimum length check\n"
        "2026-07-23 | only | three | fields\n",
        encoding="utf-8",
    )
    entries, malformed = corrections.parse(tmp_path)
    assert len(entries) == 1
    assert len(malformed) == 1 and malformed[0][0] == 3

def test_an_entry_with_no_annotation_is_open(tmp_path):
    """The overwhelming majority of the log today — nothing here should change
    behaviour for a line that predates dispositions entirely."""
    _log(tmp_path, f"2026-07-21 | s | silent-noop | claude | n/a | {_NOTE}\n")
    entries, malformed = corrections.parse(tmp_path)
    assert malformed == []
    assert entries[0].disposition == ""
    assert entries[0].note == _NOTE

def test_a_note_containing_a_literal_pipe_still_parses(tmp_path):
    """The bug this design deliberately avoids: several real notes contain `|`
    (`[grid|classic]`, a quoted table row). A seventh pipe field would have
    split these notes in half instead of reading them as one field."""
    note = "launcher takes a [grid|classic] arg, and a | b | c table row too, all inside one note"
    _log(tmp_path, f"2026-07-21 | s | silent-noop | claude | n/a | {note}\n")
    entries, malformed = corrections.parse(tmp_path)
    assert malformed == []
    assert entries[0].note == note
    assert entries[0].disposition == ""

def test_a_disposition_annotation_is_split_out_of_the_note(tmp_path):
    _log(tmp_path, f"2026-07-21 | s | silent-noop | claude | n/a | {_NOTE}. "
                   f"[[disposition: gate: corrections_log]]\n")
    entries, malformed = corrections.parse(tmp_path)
    assert malformed == []
    assert entries[0].disposition == "gate: corrections_log"
    assert "[[disposition" not in entries[0].note
    assert entries[0].note.startswith(_NOTE)

def test_disposition_survives_alongside_a_literal_pipe_in_the_note(tmp_path):
    note = f"{_NOTE}, with a [grid|classic] arg inline. [[disposition: recipe: hint.py]]"
    _log(tmp_path, f"2026-07-21 | s | silent-noop | claude | n/a | {note}\n")
    entries, _ = corrections.parse(tmp_path)
    assert entries[0].disposition == "recipe: hint.py"
    assert "[grid|classic]" in entries[0].note
    assert "[[disposition" not in entries[0].note

def test_a_note_quoting_the_marker_syntax_still_finds_the_real_trailing_one(tmp_path):
    """The exact trap `corrections-format-widen-would-corrupt-pipes` documents, and
    which this very fix nearly repeated while shipping it (`disposition-pointer-
    quoted-its-own-syntax`): a note that quotes `[[disposition: kind: pointer]]`
    as an inline example must not have that example mistaken for the real
    trailing marker. A leftmost, end-anchored, non-greedy regex matches from the
    FIRST occurrence of `[[disposition:` through to the LAST `]]` on the line —
    silently fusing the inline example and the real marker into one bogus
    disposition and stripping the example out of the note. The fix bounds the
    captured content so it cannot itself contain `]]`, forcing a match that starts
    at an earlier occurrence to fail and the search to retry at the true trailing
    marker."""
    example = "an entry documents the format with an inline `[[disposition: kind: pointer]]` example"
    note = f"{_NOTE}. {example}. [[disposition: gate: corrections_log]]"
    _log(tmp_path, f"2026-07-21 | s | silent-noop | claude | n/a | {note}\n")
    entries, malformed = corrections.parse(tmp_path)
    assert malformed == []
    assert entries[0].disposition == "gate: corrections_log"
    # The inline example must survive intact in the note — it is documentation,
    # not a real disposition, and must not be silently swallowed or corrupted.
    assert "[[disposition: kind: pointer]]" in entries[0].note
    # Nothing beyond the preserved inline example should carry the marker prefix.
    tail_after_example = entries[0].note.split("[[disposition: kind: pointer]]", 1)[1]
    assert "[[disposition" not in tail_after_example

def test_backlog_counts_only_open_defects(tmp_path):
    _log(tmp_path, (
        f"2026-07-01 | open1 | silent-noop | claude | n/a | {_NOTE}\n"
        f"2026-07-02 | resolved1 | silent-noop | claude | n/a | {_NOTE}. [[disposition: gate: x]]\n"
        f"2026-07-03 | baseline | process-metric | none | n/a | {_NOTE}\n"
    ))
    count, oldest = corrections.backlog(tmp_path)
    assert count == 1
    assert oldest == "2026-07-01"

def test_backlog_is_zero_with_no_log_at_all(tmp_path):
    assert corrections.backlog(tmp_path) == (0, None)

def test_backlog_is_zero_when_everything_is_resolved(tmp_path):
    _log(tmp_path, f"2026-07-01 | s | silent-noop | claude | n/a | {_NOTE}. [[disposition: gate: x]]\n")
    assert corrections.backlog(tmp_path) == (0, None)

def test_compact_moves_only_dispositioned_entries(tmp_path):
    log = _log(tmp_path, (
        f"# a header comment, untouched\n"
        f"2026-07-01 | open1 | silent-noop | claude | n/a | {_NOTE}\n"
        f"2026-07-02 | closed1 | silent-noop | claude | n/a | {_NOTE}. [[disposition: gate: x]]\n"
    ))
    moved = corrections.compact(tmp_path)
    assert moved == 1

    remaining_text = log.read_text(encoding="utf-8")
    assert "open1" in remaining_text
    assert "closed1" not in remaining_text
    assert "# a header comment, untouched" in remaining_text

    archive_text = (tmp_path / ".ai" / "corrections.archive.log").read_text(encoding="utf-8")
    assert "closed1" in archive_text
    assert "[[disposition: gate: x]]" in archive_text

def test_compact_is_idempotent_and_appends_on_a_second_pass(tmp_path):
    _log(tmp_path, f"2026-07-01 | c1 | silent-noop | claude | n/a | {_NOTE}. [[disposition: gate: x]]\n")
    assert corrections.compact(tmp_path) == 1
    assert corrections.compact(tmp_path) == 0, "nothing left to move on the second pass"

    _log(tmp_path, f"2026-07-02 | c2 | silent-noop | claude | n/a | {_NOTE}. [[disposition: gate: y]]\n")
    assert corrections.compact(tmp_path) == 1
    archived, _ = corrections.parse_archive(tmp_path)
    assert {e.slug for e in archived} == {"c1", "c2"}, "a second compaction must append, not overwrite"

def test_archive_never_contributes_to_the_backlog(tmp_path):
    _log(tmp_path, f"2026-07-01 | c1 | silent-noop | claude | n/a | {_NOTE}. [[disposition: gate: x]]\n")
    corrections.compact(tmp_path)
    assert corrections.backlog(tmp_path) == (0, None)

def test_a_marker_inside_a_docstring_is_not_an_appeal(tmp_path):
    """`gate-self-bug` (2026-07-22) repeating itself: a doc explaining a marker
    mechanism always contains the marker, and the first version of this parser
    read its own docstring as a live exemption with an 8-word reason."""
    found = _scan(tmp_path, '"""Write `# gate-ok(some_gate): like this`."""\nx = 1\n')
    assert found == []

def test_a_marker_inside_a_string_literal_is_not_an_appeal(tmp_path):
    found = _scan(tmp_path, 'MESSAGE = "appeal with # gate-ok(some_gate): a reason"\n')
    assert found == []

def test_a_real_trailing_marker_is_found(tmp_path):
    found = _scan(tmp_path, "run()  # gate-ok(some_gate): a reason long enough to count\n")
    assert len(found) == 1
    assert found[0].gate == "some_gate"
    assert found[0].line == 1

def test_a_wrapped_reason_is_joined_and_spans_its_lines(tmp_path):
    found = _scan(tmp_path, (
        "# gate-ok(some_gate): the first half of the reason\n"
        "# and the second half of it\n"
        "run()\n"
    ))
    assert len(found) == 1
    assert "first half" in found[0].reason and "second half" in found[0].reason
    assert (found[0].line, found[0].end_line) == (1, 2)

def test_a_trailing_comment_does_not_continue_a_reason(tmp_path):
    """Otherwise the next unrelated end-of-line note silently becomes part of
    the justification."""
    found = _scan(tmp_path, (
        "# gate-ok(some_gate): the actual reason for this exemption\n"
        "run()  # unrelated note about something else entirely\n"
    ))
    assert "unrelated note" not in found[0].reason

def test_exemption_is_measured_from_the_end_of_the_span(tmp_path):
    """A longer explanation must not push the marker out of range of the code
    it excuses."""
    appeal = appeal_markers.Appeal("f.py", 10, 18, "some_gate", "x" * 30)
    assert appeal_markers.exempt([appeal], "some_gate", "f.py", 20)
    assert not appeal_markers.exempt([appeal], "some_gate", "f.py", 30)
    assert not appeal_markers.exempt([appeal], "other_gate", "f.py", 20)

def test_an_appeal_naming_no_gate_is_refused(tmp_path):
    (tmp_path / ".ai" / "gates").mkdir(parents=True)
    (tmp_path / ".ai" / "gates" / "real_gate.py").write_text(
        "def check(root):\n    return []\n", encoding="utf-8")
    (tmp_path / ".ai" / "thing.py").write_text(
        "run()  # gate-ok(): blanket exemptions are not appeals at all\n", encoding="utf-8")
    problems = gate_appeals.check(tmp_path)
    assert any("names no gate" in v.rule for v in problems)

def test_an_appeal_naming_an_unknown_gate_is_refused(tmp_path):
    (tmp_path / ".ai" / "gates").mkdir(parents=True)
    (tmp_path / ".ai" / "gates" / "real_gate.py").write_text(
        "def check(root):\n    return []\n", encoding="utf-8")
    (tmp_path / ".ai" / "thing.py").write_text(
        "run()  # gate-ok(typo_gate): believing you are covered when you are not\n",
        encoding="utf-8")
    problems = gate_appeals.check(tmp_path)
    assert any("not a gate" in v.rule for v in problems)

def test_a_reasonless_appeal_is_refused(tmp_path):
    (tmp_path / ".ai" / "gates").mkdir(parents=True)
    (tmp_path / ".ai" / "gates" / "real_gate.py").write_text(
        "def check(root):\n    return []\n", encoding="utf-8")
    (tmp_path / ".ai" / "thing.py").write_text(
        "run()  # gate-ok(real_gate): nope\n", encoding="utf-8")
    problems = gate_appeals.check(tmp_path)
    assert any("reason is" in v.rule for v in problems)

def test_a_discarded_result_is_a_violation(tmp_path):
    found = _check_snippet(tmp_path, "import subprocess\nsubprocess.run(['git', 'add', '-A'])\n")
    assert len(found) == 1 and found[0].line == 2

def test_the_observed_bug_reproduces(tmp_path):
    """`git-add-pathspec-noop`, 2026-07-23 — the entry that earned this gate."""
    found = _check_snippet(tmp_path, (
        "import subprocess\n"
        "def commit_board(root, message):\n"
        "    subprocess.run(['git', 'add', '-A', '--', 'Board/', 'Routing.md'], cwd=root, check=False)\n"
        "    subprocess.run(['git', 'commit', '-m', message], cwd=root, check=False)\n"
    ))
    assert [v.line for v in found] == [3, 4]

def test_an_assigned_result_passes(tmp_path):
    assert _check_snippet(tmp_path, "import subprocess\nout = subprocess.run(['git', 'status'])\n") == []

def test_a_returned_result_passes(tmp_path):
    assert _check_snippet(tmp_path, (
        "import subprocess\n"
        "def _git(*args):\n"
        "    return subprocess.run(['git', *args])\n"
    )) == []

def test_an_inline_returncode_test_passes(tmp_path):
    assert _check_snippet(tmp_path, (
        "import subprocess\n"
        "if subprocess.run(['git', 'mv', 'a', 'b']).returncode != 0:\n"
        "    pass\n"
    )) == []

def test_check_output_is_out_of_scope(tmp_path):
    """It raises on failure, so discarding it is not silent."""
    assert _check_snippet(tmp_path, "import subprocess\nsubprocess.check_output(['git', 'status'])\n") == []

def test_an_appeal_suppresses_the_violation(tmp_path):
    assert _check_snippet(tmp_path, (
        "import subprocess\n"
        "# gate-ok(subprocess_result_checked): commit exits non-zero with nothing staged\n"
        "subprocess.run(['git', 'commit', '-m', 'x'])\n"
    )) == []

def test_an_appeal_naming_a_different_gate_does_not_suppress(tmp_path):
    found = _check_snippet(tmp_path, (
        "import subprocess\n"
        "# gate-ok(card_schema): a marker for an entirely different gate here\n"
        "subprocess.run(['git', 'commit', '-m', 'x'])\n"
    ))
    assert len(found) == 1

def test_board_warns_when_staging_fails(capsys, tmp_path):
    """The `git add` sites are the ones whose failure is silent and costly.
    Asserting the warning rather than the call is the same choice Session G
    recorded: check the property, not the code path."""
    from nightshift import board

    completed = subprocess.CompletedProcess(
        args=["git", "add"], returncode=128, stdout="", stderr="fatal: pathspec did not match\n")
    board._warn_if_failed(completed, "staging Board/")
    printed = capsys.readouterr().out
    assert "failed" in printed and "no-op" in printed

def test_board_is_silent_when_staging_succeeds(capsys):
    from nightshift import board

    board._warn_if_failed(subprocess.CompletedProcess(args=["git", "add"], returncode=0), "x")
    assert capsys.readouterr().out == ""

def test_a_mention_outside_the_matrix_does_not_count_as_a_row(tmp_path, monkeypatch):
    """§F is a gate-*replay* table, not the rule matrix. A doc naming a feedback
    file only there, with no §A row, is the same class of drift this script
    exists to catch, in the script itself (fixed 2026-07-24)."""
    doc = tmp_path / ".claude" / "memory" / "ai_team" / "01_audit_findings.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "## A. Rule enforcement matrix\n\n"
        "| 1 | some rule | feedback_real | prose | easy |\n\n"
        "## F. Gate replay\n\n"
        "| `feedback_ghost.md` | HIT | mentioned only here |\n",
        encoding="utf-8",
    )
    memory = tmp_path / ".claude" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    for name in ("feedback_real", "feedback_ghost"):
        (memory / f"{name}.md").write_text("rule\n", encoding="utf-8")
    (tmp_path / ".ai" / "gates").mkdir(parents=True, exist_ok=True)
    # Where the matrix lives is `[audit].matrix`; without it `audit` reads no
    # matrix at all and *every* feedback file reports as uninventoried, which
    # would pass this test for the wrong reason if it only asserted that
    # `feedback_ghost` is in the list.
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[audit]\nmatrix = ".claude/memory/ai_team/01_audit_findings.md"\n',
        encoding="utf-8")

    assert audit.drift(tmp_path)["feedback_files_with_no_row"] == ["feedback_ghost"]

def test_a_generated_fix_card_passes_the_schema(tmp_path):
    """A card the runner writes from findings must satisfy card_schema, or the
    next board scan rejects the runner's own output."""
    from nightshift.gates import card_schema

    verdict = {"complete": True, "summary": "1 drift",
               "findings": [{"claim": "X is 2.0", "cite": "settings.py:261", "why": "it is 3.0"}]}
    doc = ".claude/memory/design.md"
    text = stale._stale_card_text(doc, verdict, Path(".ai/runs/_stale/x"))
    card_dir = tmp_path / "Board" / "tasks"
    card_dir.mkdir(parents=True)
    (card_dir / f"{stale._stale_slug(doc)}.md").write_text(text, encoding="utf-8")
    # card_schema resolves worker: against .claude/agents/; the card names
    # code-thread, so the stub has to exist in this throwaway tree.
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-thread.md").write_text("---\nname: code-thread\n---\n", encoding="utf-8")
    assert card_schema.check(tmp_path) == [], "generated fix-card fails card_schema"

def test_stale_phase_ledgers_only_on_a_complete_verdict(tmp_path, monkeypatch):
    """Karel's rule: a doc is recorded verified only on a complete verdict, so a
    sweep cut off mid-doc re-checks rather than recording a lie."""
    from nightshift import stale_sweep

    (tmp_path / "Board" / "tasks").mkdir(parents=True)
    cand = stale_sweep.Candidate("d.md", churn=3, verified_at=None, named=3)
    verified: list[str] = []
    monkeypatch.setattr(stale.stale_sweep, "load_ledger", lambda root: {})
    monkeypatch.setattr(stale.stale_sweep, "select", lambda root, n, ledger: [cand])
    monkeypatch.setattr(stale.stale_sweep, "mark_verified",
                        lambda root, doc, ledger, **kw: verified.append(doc))
    monkeypatch.setattr(stale, "stale_run_dir", lambda root, doc: tmp_path)

    # Incomplete verdict → not ledgered, not carded.
    monkeypatch.setattr(stale, "run_stale_check", lambda *a, **k: ({"complete": False}, 0.0, None))
    checked, ver, carded = stale.stale_phase(tmp_path, 1, "m", None, 0.0, 600)
    assert (checked, ver, carded) == (1, 0, 0)
    assert verified == []

def test_stale_phase_cards_findings_and_verifies_clean(tmp_path, monkeypatch):
    from nightshift import stale_sweep

    (tmp_path / "Board" / "tasks").mkdir(parents=True)
    # Quote-or-drop is enforced now: the doc must exist and must contain the finding's
    # claim, or the finding is dropped and the ledger is refused (the fail-safe
    # direction). This fixture used to name a doc it never created.
    (tmp_path / "drift.md").write_text("c is documented here.\n", encoding="utf-8")
    monkeypatch.setattr(stale.stale_sweep, "load_ledger", lambda root: {})
    monkeypatch.setattr(stale.stale_sweep, "mark_verified", lambda root, doc, ledger, **kw: None)
    monkeypatch.setattr(stale, "stale_run_dir", lambda root, doc: tmp_path)

    # A doc with drift → verified AND carded.
    drift = stale_sweep.Candidate("drift.md", 5, None, 5)
    monkeypatch.setattr(stale.stale_sweep, "select", lambda root, n, ledger: [drift])
    monkeypatch.setattr(stale, "run_stale_check",
                        lambda *a, **k: ({"complete": True, "summary": "1",
                                          "findings": [{"claim": "c", "cite": "f:1", "why": "w"}]}, 0.0, None))
    _, ver, carded = stale.stale_phase(tmp_path, 1, "m", None, 0.0, 600)
    assert (ver, carded) == (1, 1)
    assert (tmp_path / "Board" / "tasks" / f"{stale._stale_slug('drift.md')}.md").is_file()

def test_stale_phase_stops_on_a_wall_without_ledgering(tmp_path, monkeypatch):
    from nightshift import stale_sweep
    from nightshift import limits

    (tmp_path / "Board" / "tasks").mkdir(parents=True)
    cand = stale_sweep.Candidate("d.md", 3, None, 3)
    monkeypatch.setattr(stale.stale_sweep, "load_ledger", lambda root: {})
    monkeypatch.setattr(stale.stale_sweep, "select", lambda root, n, ledger: [cand])
    called: list[str] = []
    monkeypatch.setattr(stale.stale_sweep, "mark_verified", lambda root, doc, ledger, **kw: called.append(doc))
    monkeypatch.setattr(stale, "stale_run_dir", lambda root, doc: tmp_path)
    wall = limits.Wall(scope=limits.SESSION, resets_at=None, evidence="usage limit")
    monkeypatch.setattr(stale, "run_stale_check", lambda *a, **k: ({}, 0.0, wall))
    checked, ver, carded = stale.stale_phase(tmp_path, 5, "m", None, 0.0, 600)
    assert ver == 0 and called == [], "a wall must not record a verification"

def test_write_and_read_receipt_roundtrip(tmp_path):
    (tmp_path / ".ai").mkdir()
    _fixtures.git_init(tmp_path, branch="main")
    assert not preflight.is_validated(tmp_path, "deadbeef")
    # write_receipt reads HEAD's branch name; on a repo with no commit that is fine.
    preflight.write_receipt(tmp_path, "deadbeef", None)
    assert preflight.is_validated(tmp_path, "deadbeef")
    assert not preflight.is_validated(tmp_path, "cafef00d")

def test_receipt_is_bounded(tmp_path):
    (tmp_path / ".ai").mkdir()
    _fixtures.git_init(tmp_path)
    for i in range(preflight.RECEIPT_KEEP + 5):
        preflight.write_receipt(tmp_path, f"sha{i:04d}", None)
    entries = preflight._load_receipt(tmp_path)
    assert len(entries) == preflight.RECEIPT_KEEP, "receipt must not grow without bound"
    assert not preflight.is_validated(tmp_path, "sha0000"), "oldest SHA should have rolled off"

def test_corrections_check_sees_a_committed_log_change(tmp_path):
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    # A branch that never touches the log fails the check.
    (repo / "other.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "unrelated"], cwd=repo, capture_output=True)
    touched, _ = preflight._corrections_touched(repo, "development_team")
    assert touched is False

    # One that appends to the log passes it.
    log = repo / ".ai" / "corrections.log"
    log.write_text(log.read_text(encoding="utf-8") + "2026-07-23 | s | silent-noop | claude | n/a | a real note about a real thing here\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "log it"], cwd=repo, capture_output=True)
    touched, _ = preflight._corrections_touched(repo, "development_team")
    assert touched is True

def test_corrections_check_on_the_integration_branch_resolves_against_origin(tmp_path):
    """Committing plan/board work straight to the integration branch is allowed,
    and there `merge-base HEAD development_team` is HEAD itself — a resolving
    merge-base and a vacuously empty diff, so a real correction read as none.
    `origin/<base>` is the honest basis: what this checkout has and the remote
    does not."""
    repo = _git_repo(tmp_path)          # _git_repo inits ON development_team
    _with_origin(repo)
    _log_a_correction(repo)

    touched, how = preflight._corrections_touched(repo, "development_team")

    assert touched is True, f"self-diff false negative — basis was {how!r}"
    assert "origin/development_team" in how

def test_corrections_check_on_the_integration_branch_still_fails_a_branch_that_logged_nothing(tmp_path):
    """The fix must not become a way to pass. Same self-diff situation, but the
    commit does not touch the log — the check still says no."""
    repo = _git_repo(tmp_path)
    _with_origin(repo)
    (repo / "other.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "unrelated"], cwd=repo, capture_output=True)

    touched, _ = preflight._corrections_touched(repo, "development_team")

    assert touched is False

def test_corrections_check_without_an_origin_keeps_the_head_only_fallback(tmp_path):
    """A checkout with no remote (or one that never fetched) has no `origin/base`
    to resolve. That must take the pre-existing no-merge-base path, not invent a
    third behaviour and not silently pass."""
    repo = _git_repo(tmp_path)          # on development_team, no remote at all
    _log_a_correction(repo)

    touched, how = preflight._corrections_touched(repo, "development_team")

    assert touched is True
    assert "no merge-base" in how, f"expected the existing fallback, got {how!r}"

def test_changed_paths_on_the_integration_branch_sees_committed_work(tmp_path):
    """`_changed_paths` had the same self-diff exposure. Its failure direction was
    already safe (empty set → ALL), but two functions answering "am I self-diffing"
    two different ways is the duplication worth not having."""
    repo = _git_repo(tmp_path)
    _with_origin(repo)
    _log_a_correction(repo)

    changed, how, merge_base = preflight._changed_paths(repo, "development_team")

    assert ".ai/corrections.log" in changed, f"basis was {how!r}"
    assert "origin/development_team" in how
    assert merge_base, "a real merge-base resolved through the origin/ redirect"

def test_no_corrections_escape_is_an_explicit_zero(tmp_path):
    repo = _git_repo(tmp_path)
    # No log change on the branch, but an explicit reason clears the check.
    result = preflight.run_checks(repo, "development_team",
                                  no_corrections="pure refactor, nothing learned", skip_tests=True)
    entry = next(c for c in result.checks if c.name == "corrections")
    assert entry.ok
    assert "explicit zero" in entry.detail

def test_changed_paths_includes_uncommitted_working_tree_edits(tmp_path):
    """The preflight is the only `suite.select` caller that runs against a live
    working tree, so an uncommitted edit has to widen the slice — pytest imports
    the tree as it is on disk, not as HEAD describes it."""
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    (repo / "project_tigress").mkdir()
    (repo / "project_tigress" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "game change"], cwd=repo, capture_output=True)

    changed, _, _ = preflight._changed_paths(repo, "development_team")
    assert "project_tigress/thing.py" in changed

    # An UNCOMMITTED .ai/ edit must show up too — this is the case that would
    # otherwise select GAME and skip the system tests it can break.
    (repo / ".ai" / "helper.py").write_text("y = 2\n", encoding="utf-8")
    changed, _, _ = preflight._changed_paths(repo, "development_team")
    assert ".ai/helper.py" in changed

def test_changed_paths_returns_empty_without_a_merge_base(tmp_path):
    """`None`, not the empty set: `empty-diff-preflight-runs-everything` made an
    empty set mean "confirmed nothing changed" (`suite.select` reads it as NONE),
    so "cannot tell what changed" — a fresh clone that never fetched the
    integration branch — has to answer differently or it would wrongly select
    NONE instead of running everything. `run_checks`/`_run_pytest` build the ALL
    selection directly when they see `None`, never handing it to `suite.select`."""
    repo = _git_repo(tmp_path)
    changed, how, merge_base = preflight._changed_paths(repo, "no-such-branch")
    assert changed is None
    assert "no merge-base" in how
    assert merge_base == ""

def test_pytest_step_runs_only_the_selected_slice(tmp_path):
    """A `.ai/`-only diff runs the system file and not the game one."""
    repo = _repo_with_tests(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    (repo / ".ai" / "helper.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "system change"], cwd=repo, capture_output=True)

    ok, detail, bucket = _run_pytest_slice(repo, "development_team", full=False)
    assert ok, detail
    assert bucket == "system"
    assert _junit_names(repo) == {"test_board_side"}, "the game test must not have run"

def test_a_board_notes_only_diff_runs_no_pytest_at_all(tmp_path):
    """`ideas/`/`inbox/` notes are the card_schema gate's job, not pytest's. The
    preflight must report a pass on the `none` slice without running — or judging —
    any tests: no report is written, so 'no report' cannot be mistaken for a fail."""
    repo = _repo_with_tests(tmp_path)
    junit = repo / preflight.RUN_DIR / "junit.xml"
    junit.unlink(missing_ok=True)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    (repo / "Board" / "inbox").mkdir(parents=True)
    (repo / "Board" / "inbox" / "rough.md").write_text("a rough idea, no frontmatter\n",
                                                       encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "inbox note"], cwd=repo, capture_output=True)

    ok, detail, bucket = _run_pytest_slice(repo, "development_team", full=False)
    assert ok, detail
    assert bucket == "none"
    assert "no tests apply" in detail
    assert not junit.exists(), "the none slice must not run pytest, so no report is written"

def test_run_tests_treats_an_empty_selection_as_a_pass_without_running(tmp_path):
    """The runner's (and merge_check's) side of the same NONE contract: an empty
    `pytest_args` is a pass with no run. Running pytest with no path would collect
    the whole tree from cwd — the opposite of 'no tests apply'."""
    log = tmp_path / "pytest.txt"
    junit = tmp_path / "junit.xml"
    ok, why, evidence = verify._run_tests(tmp_path, log, 60, junit, [])
    assert ok and why == ""
    assert evidence == "", "a pass has no failure to quote onto the card"
    assert not junit.exists(), "no JUnit report means pytest genuinely did not run"
    assert "board notes only" in log.read_text(encoding="utf-8")

def test_full_tests_forces_the_whole_suite(tmp_path):
    """`--full-tests` overrides the selection — both files run."""
    repo = _repo_with_tests(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, capture_output=True)
    (repo / ".ai" / "helper.py").write_text("y = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "system change"], cwd=repo, capture_output=True)

    ok, detail, bucket = _run_pytest_slice(repo, "development_team", full=True)
    assert ok, detail
    assert bucket == "all"
    assert _junit_names(repo) == {"test_board_side", "test_game_side"}

def test_a_failing_test_fails_the_pytest_step(tmp_path):
    repo = _repo_with_tests(tmp_path)
    (repo / "tests" / "test_board_side.py").write_text(
        "def test_system():\n    assert False\n", encoding="utf-8")
    ok, detail, _ = _run_pytest_slice(repo, "development_team", full=True)
    assert not ok
    assert "failure(s)" in detail

def test_a_stale_junit_report_cannot_pass_a_run_that_never_ran(tmp_path):
    """The preflight reuses one report path, so it must delete the previous run's
    XML first. Otherwise a pytest that dies before writing anything (a collection
    error, a missing plugin) is judged by the LAST run's green report — precisely
    the silent-green `check_junit` exists to prevent."""
    repo = _repo_with_tests(tmp_path)
    ok, _, _ = _run_pytest_slice(repo, "development_team", full=True)
    assert ok, "sanity: the first run should be green and leave a report"

    # Now make pytest fail before it can write a report at all.
    (repo / "tests" / "test_board_side.py").write_text("import nonexistent_module_xyz\n",
                                                       encoding="utf-8")
    ok, detail, _ = _run_pytest_slice(repo, "development_team", full=True)
    assert not ok, f"a run that collected nothing was judged green: {detail}"

def test_the_pytest_step_reports_the_slice_and_the_mode(tmp_path):
    repo = _repo_with_tests(tmp_path)
    ok, detail, _ = _run_pytest_slice(repo, "development_team", full=True)
    assert ok, detail
    assert "all slice" in detail
    assert "2 test(s)" in detail
    assert ("parallel" in detail) or ("serial" in detail)

def test_run_checks_records_the_slice_and_the_receipt_carries_it(tmp_path):
    """The receipt is what unblocks a push, so what it attests to has to be
    legible — a narrowed validation must not read later as a full one."""
    repo = _repo_with_tests(tmp_path)
    preflight.write_receipt(repo, "abc123", None, "game")
    entry = next(e for e in preflight._load_receipt(repo) if e["sha"] == "abc123")
    assert entry["tests"] == "game"

    # And the skipped path says so rather than leaving the field absent.
    result = preflight.run_checks(repo, "development_team",
                                 no_corrections="nothing learned", skip_tests=True)
    assert result.tests_slice == "skipped"

def test_write_receipt_still_works_without_a_slice(tmp_path):
    """Three-positional-arg callers (and receipts written before slices existed)
    must keep working — the guard hook reads only `sha`."""
    (tmp_path / ".ai").mkdir()
    _fixtures.git_init(tmp_path)
    preflight.write_receipt(tmp_path, "deadbeef", None)
    assert preflight.is_validated(tmp_path, "deadbeef")

def test_reuse_skips_pytest_entirely_when_the_tree_is_unchanged(tmp_path):
    """The `--no-corrections` case: nothing on disk changes between the failed run
    and the fix, so every target part is reused and pytest does not run at all."""
    repo = _repo_with_tests(tmp_path)
    _feature_with_game_change(repo)

    ok, _, bucket = _run_pytest_slice(repo, "development_team", full=False)
    assert ok and bucket == "game"

    junit = repo / preflight.RUN_DIR / "junit.xml"
    junit.unlink()  # delete it so a re-run would have to recreate it to have run

    ok, detail, bucket = _run_pytest_slice(repo, "development_team", full=False)
    assert ok and bucket == "game"
    assert "reused" in detail
    assert not junit.exists(), "an unchanged tree must reuse the verdict, not re-run pytest"

def test_reuse_reruns_only_the_stale_part_after_a_corrections_log_append(tmp_path):
    """The `.ai/corrections.log` case: logging a line is a `system` change, so the
    `game` fingerprint is untouched and only the system tests re-run."""
    repo = _repo_with_tests(tmp_path)
    _feature_with_game_change(repo)

    ok, _, bucket = _run_pytest_slice(repo, "development_team", full=False)
    assert ok and bucket == "game", "the game part is cached by this first run"

    log = repo / ".ai" / "corrections.log"
    log.write_text(log.read_text(encoding="utf-8")
                   + "2026-07-28 | s | silent-noop | claude | n/a | a real note about a real thing here\n",
                   encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "log it"], cwd=repo, capture_output=True)

    ok, detail, bucket = _run_pytest_slice(repo, "development_team", full=False)
    assert ok
    assert bucket == "all", "the diff now spans game + system"
    assert "game reused" in detail and "system ran" in detail
    assert _junit_names(repo) == {"test_board_side"}, "only the stale system part may re-run"

def test_fresh_tests_bypasses_the_reuse_cache(tmp_path):
    """`--fresh-tests` forces a real run even when the tree is provably unchanged."""
    repo = _repo_with_tests(tmp_path)
    _feature_with_game_change(repo)

    ok, _, _ = _run_pytest_slice(repo, "development_team", full=False)
    assert ok
    junit = repo / preflight.RUN_DIR / "junit.xml"
    junit.unlink()

    ok, detail, _ = _run_pytest_slice(repo, "development_team", full=False, fresh=True)
    assert ok
    assert junit.exists(), "--fresh-tests must re-run pytest, not reuse the cached pass"
    assert "reused" not in detail

def test_a_failed_part_is_not_cached_for_reuse(tmp_path):
    """Only a pass is ever cached, so a fix (which moves the fingerprint) always
    re-runs. A failing run must leave no reusable entry behind."""
    repo = _repo_with_tests(tmp_path)
    (repo / "tests" / "test_game_side.py").write_text(
        "def test_game():\n    assert False\n", encoding="utf-8")

    ok, detail, bucket = _run_pytest_slice(repo, "development_team", full=False)
    assert not ok and bucket == "game", detail

    cache = preflight._load_pytest_cache(repo)
    assert not preflight._cache_hit(cache.get("game"), ""), "a failed part must not be cached"
    assert cache.get("game", {}).get("ok") is not True

def test_part_fingerprint_tracks_the_part_a_change_belongs_to(tmp_path):
    """The safety property in miniature: editing a `system` path moves the system
    fingerprint and leaves the game one identical; editing a `game` path moves the
    game one. This is what lets a system-side fix reuse the game verdict."""
    repo = _repo_with_tests(tmp_path)
    (repo / "project_tigress").mkdir(exist_ok=True)
    (repo / "project_tigress" / "foo.py").write_text("a\n", encoding="utf-8")
    (repo / ".ai" / "bar.py").write_text("b\n", encoding="utf-8")
    changed = {"project_tigress/foo.py", ".ai/bar.py"}

    game1 = preflight._part_fingerprint(repo, changed, "game", "env")
    system1 = preflight._part_fingerprint(repo, changed, "system", "env")

    (repo / ".ai" / "bar.py").write_text("b changed\n", encoding="utf-8")
    assert preflight._part_fingerprint(repo, changed, "game", "env") == game1
    assert preflight._part_fingerprint(repo, changed, "system", "env") != system1

    (repo / "project_tigress" / "foo.py").write_text("a changed\n", encoding="utf-8")
    assert preflight._part_fingerprint(repo, changed, "game", "env") != game1

@pytest.mark.parametrize("module", ["board"])
def test_every_git_add_in_the_board_machinery_is_checked(module):
    """The class the gate exists for, asserted directly: no `git add` in the
    board machinery may throw its result away, appeal or no appeal."""
    import ast
    import importlib

    source = Path(importlib.import_module(f"nightshift.{module}").__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        args = node.value.args
        if not args or not isinstance(args[0], ast.List):
            continue
        literals = [e.value for e in args[0].elts if isinstance(e, ast.Constant)]
        assert literals[:2] != ["git", "add"], (
            f"{module}.py:{node.lineno} discards the result of a `git add` — "
            "that is the git-add-pathspec-noop bug's exact shape"
        )
