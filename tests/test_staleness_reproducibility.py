"""The staleness gates must give the same answer on every checkout.

All three regressions here landed on 2026-07-30, from one overnight run that
failed three cards and then threw away $20 of correct analysis. They are separate
bugs with one shape: **a check that reads something the next machine will not
have.**

  1. `doc_scan` resolved references against a walk of the whole disk, so a
     *gitignored* local directory could satisfy a reference the project does not
     contain. A directory of manually downloaded asset packs, never committed,
     shipped a top-level licence file, which made a doc naming some other
     project's licence file resolve on that box and dangle everywhere else.
  2. `doc_reference_liveness` backticked the offending token in its own message,
     and the runner writes that message verbatim into a failed card's `## Error`.
     The message therefore became a new dangling reference in a tracked doc that
     the same gate scans — one root cause, three consecutive card failures.
  3. `stale-hunter` was told to write `verdict.json` while declared
     `tools: Read, Grep, Glob`. It cannot write files, so every verdict was
     `{}` — "not verified" — while the run reported success.

Travelled whole from Dungeoneer's `tests/test_staleness_reproducibility.py` at
07_portability.md §8 step 4, minus one half-test: the assertion that *this
project's* `stale-hunter` charter grants no `Write` is a fact about a charter
file, so it stayed there. The framework half of that pair — that `_STALE_PROMPT`
asks for a final message rather than a file — is below.

The fixtures were already synthetic, so nothing needed inventing to move them;
the source package is called `myapp` here for the reason `test_suite.py` states.
A test pinned to a real doc tree fails whenever a doc is legitimately edited,
which is the muted-gate failure mode 08_staleness.md §3.1 warns about.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightshift import runner
from nightshift.gates import doc_reference_liveness
from nightshift.gates import doc_scan

import _fixtures


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo — `_project_files` shells out to git, so a fake tree
    would exercise only the no-git fallback and prove nothing."""
    _fixtures.repo_copy("staleness-myapp", tmp_path, _build)
    doc_scan.clear_caches()
    yield tmp_path
    doc_scan.clear_caches()


def _build(tmp_path: Path) -> None:
    _fixtures.git_init(tmp_path)
    (tmp_path / "myapp").mkdir()
    (tmp_path / "myapp" / "real_module.py").write_text(
        "class RealClass:\n    pass\n", encoding="utf-8")
    # `doc_scan.source_dirs` reads `[project]` since 07_portability.md §8 step 4;
    # the tuple it replaced was hardcoded, so a fixture that declared nothing used
    # to get a source dir scanned for free and now has to say so. That is step 4's
    # first checklist item, and this fixture is exactly the caller it warns about.
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "myapp"\nsource_dirs = ["myapp"]\n', encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored_stuff/\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")


# --- 1. the disk is not the project --------------------------------------


def test_a_gitignored_file_cannot_satisfy_a_reference(repo: Path):
    """The exact 2026-07-30 shape: an ignored directory holding a file whose
    stem matches the reference. It exists on disk and is not part of the project,
    so it must not resolve."""
    junk = repo / "ignored_stuff" / "third_party_pack"
    junk.mkdir(parents=True)
    (junk / "LICENCE_FILE").write_text("Apache 2.0\n", encoding="utf-8")
    doc_scan.clear_caches()

    assert (junk / "LICENCE_FILE").is_file(), "fixture must really be on disk"
    assert not doc_scan.resolve_symbol(repo, "LICENCE_FILE")
    assert not doc_scan.resolve_path(repo, "ignored_stuff/third_party_pack/LICENCE_FILE")


def test_tracked_files_still_resolve(repo: Path):
    assert doc_scan.resolve_symbol(repo, "RealClass")
    assert doc_scan.resolve_path(repo, "myapp/real_module.py")
    assert doc_scan.resolve_path(repo, "real_module.py"), "package-relative house style"


def test_an_untracked_but_unignored_file_resolves(repo: Path):
    """Deliberately NOT tracked-only. A doc naming a file the author just created
    and has not `git add`ed is a normal workflow, not staleness — so the file list
    is `--cached --others --exclude-standard`, i.e. what `git status` talks about.
    """
    (repo / "myapp" / "brand_new.py").write_text("X = 1\n", encoding="utf-8")
    doc_scan.clear_caches()
    assert doc_scan.resolve_path(repo, "myapp/brand_new.py")


def test_no_git_falls_back_instead_of_reporting_an_empty_project(tmp_path: Path):
    """A non-repo checkout must degrade to the disk walk. Reporting "no files"
    would dangle every reference in every doc at once — a gate that fails
    everything is a gate that gets muted."""
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "myapp"\nsource_dirs = ["myapp"]\n', encoding="utf-8")
    (tmp_path / "myapp").mkdir()
    (tmp_path / "myapp" / "mod.py").write_text("Y = 2\n", encoding="utf-8")
    doc_scan.clear_caches()
    assert doc_scan._project_files(tmp_path) is None
    assert doc_scan.resolve_path(tmp_path, "myapp/mod.py")
    doc_scan.clear_caches()


# --- 2. the gate's own output must be inert -------------------------------


def test_the_violation_message_does_not_backtick_the_token(repo: Path):
    """The runner copies this text into a tracked `## Error`. A backticked token
    there is a fresh dangling reference the same gate then reports."""
    docs = repo / ".claude" / "memory"
    docs.mkdir(parents=True)
    (docs / "note.md").write_text(
        "# Note\n\nSee `NoSuchSymbolAnywhere` for details.\n", encoding="utf-8")
    doc_scan.clear_caches()

    violations = doc_reference_liveness.check(repo)
    assert violations, "fixture should dangle"
    message = str(violations[0])
    assert "NoSuchSymbolAnywhere" in message
    assert "`" not in message, f"message must be backtick-free, got: {message}"


def test_runner_neutralises_backticks_before_writing_a_card():
    detail = "gates: doc_reference_liveness: symbol `Ghost` does not exist"
    safe = runner._quote_safe(detail)
    assert "`" not in safe
    assert "Ghost" in safe, "sanitising must not destroy the evidence"


# --- 3. the verdict channel must be one the agent can use -----------------


def _stream(final_message: str) -> str:
    """A stream-json stdout whose terminal event carries `final_message`."""
    return "\n".join([
        json.dumps({"type": "assistant", "message": {"content": []}}),
        json.dumps({"type": "result", "subtype": "success",
                    "result": final_message, "total_cost_usd": 0.42}),
    ])


def test_a_fenced_verdict_in_the_final_message_is_read():
    """`stale-hunter` has no Write tool, so its reply is the only channel."""
    verdict = {"complete": True,
               "findings": [{"claim": "c", "cite": "f.py:1", "why": "w"}],
               "summary": "1 drift"}
    text = f"I finished reading.\n\n```json\n{json.dumps(verdict)}\n```"
    got = runner._verdict_from_message(text)
    assert got["complete"] is True
    assert len(got["findings"]) == 1


def test_an_unfenced_verdict_is_still_read():
    got = runner._verdict_from_message('Verdict: {"complete": true, "findings": []}')
    assert got["complete"] is True


def test_the_last_json_block_wins():
    """A checker that thinks aloud in JSON before its verdict must not be
    mis-read from its first block."""
    text = ('```json\n{"complete": false, "summary": "draft"}\n```\n'
            'On reflection:\n```json\n{"complete": true, "summary": "final"}\n```')
    assert runner._verdict_from_message(text)["summary"] == "final"


def test_prose_without_a_verdict_is_not_verified():
    """`{}` must stay the answer for "no verdict" — recording a verification that
    did not happen is the one outcome worse than re-checking."""
    assert runner._verdict_from_message("I could not finish the document.") == {}
    assert runner._verdict_from_message("") == {}


def test_the_stale_prompt_asks_for_a_final_message_not_a_file():
    """The bug itself: the prompt demanded a handover the agent's tool grant
    forbids, and 25 docs' worth of correct analysis was discarded in silence.

    The other half of this pair — that the charter really does withhold `Write` —
    is an assertion about a project's own `.claude/agents/stale-hunter.md`, so it
    stays in the consuming project. Here the invariant is one-sided and still
    worth pinning: whatever a project grants, this prompt must not ask for a
    write, because the read-only grant is the charter that ships as the template.
    """
    prompt = runner._STALE_PROMPT
    assert "final message" in prompt
    assert "Write your verdict to" not in prompt


# --- 4. a sweep killed mid-run must not lose findings ---------------------


def _board_repo(tmp_path: Path) -> Path:
    """A git repo shaped enough for `board.commit_board` to really commit."""
    _fixtures.git_init(tmp_path, email="t@t")
    (tmp_path / "Board" / "tasks").mkdir(parents=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def test_findings_are_committed_before_the_doc_is_ledgered(tmp_path, monkeypatch):
    """The window that could lose a night's findings for good.

    `mark_verified` is what stops a doc being re-checked. It used to run *before*
    the card was written, and the card was only committed at the very end of the
    whole sweep — so a run killed mid-sweep (Windows rebooting at 3 AM is the
    documented case) left docs permanently marked verified with their drift cards
    uncommitted. This asserts the order is card → commit → ledger, by recording
    whether the card was already committed at the moment the ledger was touched.
    """
    from nightshift import stale_sweep

    root = _board_repo(tmp_path)
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "code-thread.md").write_text(
        "---\nname: code-thread\n---\n", encoding="utf-8")

    committed_when_ledgered: list[bool] = []
    slug = runner._stale_slug("drift.md")

    def _spy_mark_verified(r, doc, ledger):
        tracked = subprocess.run(
            ["git", "-C", str(r), "ls-files", f"Board/tasks/{slug}.md"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        committed_when_ledgered.append(bool(tracked.stdout.strip()))

    monkeypatch.setattr(runner.stale_sweep, "load_ledger", lambda r: {})
    monkeypatch.setattr(runner.stale_sweep, "select", lambda r, n, ledger: [
        stale_sweep.Candidate("drift.md", 5, None, 5)])
    monkeypatch.setattr(runner.stale_sweep, "mark_verified", _spy_mark_verified)
    monkeypatch.setattr(runner, "stale_run_dir", lambda r, doc: root)
    monkeypatch.setattr(runner, "run_stale_check", lambda *a, **k: (
        {"complete": True, "summary": "1 drift",
         "findings": [{"claim": "c", "cite": "f.py:1", "why": "w"}]}, 0.0, None))

    _, verified, carded = runner.stale_phase(root, 1, "m", None, 0.0, 600)

    assert (verified, carded) == (1, 1)
    assert committed_when_ledgered == [True], (
        "the drift card must be committed BEFORE the doc is ledgered — otherwise a "
        "killed sweep marks the doc verified forever and the findings never land"
    )


def test_a_swept_doc_records_progress_even_if_the_run_never_finishes(tmp_path, monkeypatch):
    """`stale_status.json` is the committed "a sweep ran" fact the digest reads.
    Written per carded doc, not only after the loop, so a killed run still says so.
    """
    from nightshift import stale_sweep

    root = _board_repo(tmp_path)
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "code-thread.md").write_text(
        "---\nname: code-thread\n---\n", encoding="utf-8")

    monkeypatch.setattr(runner.stale_sweep, "load_ledger", lambda r: {})
    monkeypatch.setattr(runner.stale_sweep, "select", lambda r, n, ledger: [
        stale_sweep.Candidate("drift.md", 5, None, 5)])
    monkeypatch.setattr(runner.stale_sweep, "mark_verified", lambda r, d, l: None)
    monkeypatch.setattr(runner, "stale_run_dir", lambda r, doc: root)
    monkeypatch.setattr(runner, "run_stale_check", lambda *a, **k: (
        {"complete": True, "summary": "1 drift",
         "findings": [{"claim": "c", "cite": "f.py:1", "why": "w"}]}, 0.0, None))

    runner.stale_phase(root, 1, "m", None, 0.0, 600)

    status = stale_sweep.read_status(root)
    assert status is not None and status["carded"] == 1
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", str(stale_sweep.STATUS).replace("\\", "/")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert tracked.stdout.strip(), "status must be committed, not left dirty"


def test_a_stale_checker_that_walls_after_a_complete_verdict_is_honoured(
        tmp_path, monkeypatch):
    """`wall-on-review-wrapup-discards-a-verdict`, the sweep's share of it.

    `stale-hunter` is a pure judge — its verdict *is* its whole output — so a
    `complete: true` written before the wall means the doc was read to the end
    and the wall landed on the wrap-up call after it. The sweep used to `break`
    on the wall first, "leaving the ledger untouched", which threw away a
    finished check and guaranteed the next run would redo it identically. Now the
    card is written and committed and the doc ledgered, and only *then* does the
    sweep stop — card → commit → ledger, the same order the killed-sweep test
    above pins, with the stop moved after it instead of before.
    """
    from nightshift import limits, stale_sweep

    root = _board_repo(tmp_path)
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "code-thread.md").write_text(
        "---\nname: code-thread\n---\n", encoding="utf-8")

    ledgered: list[str] = []
    monkeypatch.setattr(runner.stale_sweep, "load_ledger", lambda r: {})
    monkeypatch.setattr(runner.stale_sweep, "select", lambda r, n, ledger: [
        stale_sweep.Candidate("drift.md", 5, None, 5),
        stale_sweep.Candidate("other.md", 4, None, 4)])
    monkeypatch.setattr(runner.stale_sweep, "mark_verified",
                        lambda r, d, l: ledgered.append(d))
    monkeypatch.setattr(runner, "stale_run_dir", lambda r, doc: root)
    monkeypatch.setattr(runner, "run_stale_check", lambda *a, **k: (
        {"complete": True, "summary": "1 drift",
         "findings": [{"claim": "c", "cite": "f.py:1", "why": "w"}]},
        0.0, limits.Wall(limits.SESSION, None, "usage limit reached")))

    checked, verified, carded = runner.stale_phase(root, 2, "m", None, 0.0, 600)

    assert (checked, verified, carded) == (1, 1, 1), (
        "the walled doc's complete verdict must be honoured — and the sweep must "
        "still stop there rather than spawning the next checker into a shut window"
    )
    assert ledgered == ["drift.md"]
    card = root / "Board" / "tasks" / f"{runner._stale_slug('drift.md')}.md"
    assert card.is_file(), "the findings card must be written before the sweep stops"
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", f"Board/tasks/{card.stem}.md"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert tracked.stdout.strip(), "and committed — .ai/runs/ is not a rescue"


def test_a_stale_checker_that_walls_with_nothing_complete_leaves_the_ledger_alone(
        tmp_path, monkeypatch):
    """The regression guard. An incomplete verdict plus a wall is still "not
    verified": the doc is re-checked next time rather than being recorded as
    verified on a check that never finished."""
    from nightshift import limits, stale_sweep

    root = _board_repo(tmp_path)
    ledgered: list[str] = []
    monkeypatch.setattr(runner.stale_sweep, "load_ledger", lambda r: {})
    monkeypatch.setattr(runner.stale_sweep, "select", lambda r, n, ledger: [
        stale_sweep.Candidate("drift.md", 5, None, 5)])
    monkeypatch.setattr(runner.stale_sweep, "mark_verified",
                        lambda r, d, l: ledgered.append(d))
    monkeypatch.setattr(runner, "stale_run_dir", lambda r, doc: root)
    monkeypatch.setattr(runner, "run_stale_check", lambda *a, **k: (
        {"complete": False}, 0.0,
        limits.Wall(limits.SESSION, None, "usage limit reached")))

    checked, verified, carded = runner.stale_phase(root, 1, "m", None, 0.0, 600)

    assert (checked, verified, carded) == (1, 0, 0)
    assert ledgered == []
