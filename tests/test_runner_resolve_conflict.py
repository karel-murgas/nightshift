"""Tests for `runner._resolve_conflict` — the merge resolver's guardrails.

**What is under test is the containment, not the resolving.** The agent is a
subprocess boundary and is stubbed here, exactly as every other worker is in this
suite. What matters on this side of it is that a resolution the runner accepts is
one that passed every mechanical check, and that a resolution it rejects leaves the
worktree exactly as it found it — because the failure mode this whole feature could
introduce is a confidently-wrong unattended merge, and the checks are the only thing
standing between the two.

So each test drives a real `git rebase` into a real conflict, hands the paused
worktree to a stub playing one kind of resolver, and asserts the runner's verdict
and the state it left behind.

Written for `merge-conflict-has-no-owner` (2026-08-23), after two reviewed-ok cards
in one night failed to land on a collision no human judgment was needed for.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from nightshift import board, gitmerge, runner

import _fixtures  # noqa: E402

# A newest-first register: exactly the shape both of the 2026-08-23 conflicts had.
BASE_LOG = "# Log\n\n- older entry\n"
OURS_LOG = "# Log\n\n- the card's own entry\n- older entry\n"
THEIRS_LOG = "# Log\n\n- a sibling card's entry\n- older entry\n"
BOTH_LOG = "# Log\n\n- the card's own entry\n- a sibling card's entry\n- older entry\n"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _conflicted(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo whose `feature` branch will not replay onto `main`.

    Both branches rewrite the same first line of the same log, which is what makes
    it a textual conflict rather than an auto-merge.
    """
    repo = _fixtures.git_init(tmp_path / "repo", branch="main")
    (repo / "log.md").write_bytes(BASE_LOG.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "log.md").write_bytes(OURS_LOG.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the card")

    _git(repo, "checkout", "-q", "main")
    (repo / "log.md").write_bytes(THEIRS_LOG.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the sibling")

    _git(repo, "checkout", "-q", "feature")
    return repo, "feature", "main"


def _pause_rebase(repo: Path, base: str) -> None:
    """Start the rebase and leave it stopped on the conflict, as the runner does."""
    out = _git(repo, "rebase", *gitmerge.STRATEGY_ARGS, base)
    assert out.returncode != 0, "fixture must actually conflict"
    assert runner._unmerged_paths(repo), "fixture must leave an unmerged path"


def _verdict_path(prompt: str) -> Path:
    """Where the prompt told the resolver to write its verdict.

    Read out of the prompt rather than reconstructed, so the stub agrees with the
    runner by construction — the same reason `_fake_worker` does it this way.
    """
    found = re.search(r"`([^`]+\.json)`", prompt)
    assert found, "the prompt must name a verdict path"
    return Path(found.group(1))


def _card() -> board.Card:
    return board.Card(path=Path("probe.md"), lane="review",
                      fields={"id": "probe", "title": "a probe card"},
                      text="# probe\n")


def _resolver(monkeypatch, *, write: str | None, verdict: dict | None,
              stage: bool = True, also_touch: str | None = None):
    """Stand in for the agent: optionally rewrite the conflicted file, optionally
    stage it, optionally scribble somewhere it was not asked to, and report."""
    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        tree = Path(cwd)
        if write is not None:
            (tree / "log.md").write_bytes(write.encode("utf-8"))
            if stage:
                _git(tree, "add", "log.md")
        if also_touch is not None:
            (tree / also_touch).write_bytes(b"a stray edit\n")
            _git(tree, "add", also_touch)
        if verdict is not None:
            _verdict_path(prompt).write_text(json.dumps(verdict), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "host_setting",
                        lambda root, key, default=None: default)


def _run(repo: Path, tmp_path: Path, branch: str, base: str) -> tuple[bool, str]:
    return runner._resolve_conflict(repo, repo, _card(), branch, base, tmp_path / "out",
                                    model="test-model", timeout=60, card_budget=0.0)


def test_a_kept_both_resolution_replays_and_lands(tmp_path, monkeypatch):
    """The ordinary case, and the whole reason this exists: two cards appended to
    the same newest-first log. Keeping both is the correct resolution, the rebase
    continues, and the result carries both entries."""
    repo, branch, base = _conflicted(tmp_path)
    _pause_rebase(repo, base)
    _resolver(monkeypatch, write=BOTH_LOG,
              verdict={"resolved": True, "summary": "kept both entries"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert replayed, detail
    assert "kept both entries" in detail
    assert (repo / "log.md").read_text(encoding="utf-8") == BOTH_LOG
    assert not runner._unmerged_paths(repo)
    # The rebase finished: `feature` now sits on top of the sibling's commit.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "HEAD"


def test_a_declined_resolution_aborts_and_reports_the_reason(tmp_path, monkeypatch):
    """`resolved: false` is a success for the agent and a stop for the runner. The
    summary is what Karel reads on the card in `blocked/`, so it must survive."""
    repo, branch, base = _conflicted(tmp_path)
    _pause_rebase(repo, base)
    _resolver(monkeypatch, write=None,
              verdict={"resolved": False,
                       "summary": "both sides set the same constant to different values"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert "declined" in detail
    assert "same constant" in detail
    assert not runner._unmerged_paths(repo), "the rebase must have been aborted"


def test_a_resolution_that_leaves_a_conflict_marker_is_thrown_away(tmp_path, monkeypatch):
    """The 2026-08-09 failure, now caught before it can commit rather than a day
    after it shipped. The agent claims success and the content even looks resolved;
    the trailing marker is the whole defect."""
    repo, branch, base = _conflicted(tmp_path)
    _pause_rebase(repo, base)
    _resolver(monkeypatch,
              write=BOTH_LOG + ">>>>>>> abc1234 (the sibling)\n",
              verdict={"resolved": True, "summary": "kept both"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert "conflict marker" in detail
    assert not runner._unmerged_paths(repo)


def test_a_resolver_that_edits_outside_the_conflict_is_thrown_away(tmp_path, monkeypatch):
    """An unattended agent with a whole worktree writable needs its blast radius
    asserted, not requested. The gates and tests that follow would catch a *broken*
    stray edit; they cannot catch a plausible one."""
    repo, branch, base = _conflicted(tmp_path)
    _pause_rebase(repo, base)
    _resolver(monkeypatch, write=BOTH_LOG, also_touch="unrelated.md",
              verdict={"resolved": True, "summary": "kept both, tidied a neighbour"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert "unrelated.md" in detail
    assert "not part of the conflict" in detail


def test_an_unstaged_resolution_is_thrown_away(tmp_path, monkeypatch):
    """Editing the file is not resolving it — git still calls the path unmerged, and
    `rebase --continue` would refuse. Caught here with the reason, rather than as a
    confusing git error two steps later."""
    repo, branch, base = _conflicted(tmp_path)
    _pause_rebase(repo, base)
    _resolver(monkeypatch, write=BOTH_LOG, stage=False,
              verdict={"resolved": True, "summary": "kept both"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert "still unmerged" in detail


def test_a_missing_verdict_counts_as_a_decline(tmp_path, monkeypatch):
    """No verdict file at all — the agent crashed, or hit a wall mid-turn. It must
    read as "did not resolve", never as silent success: `_read_verdict` returns `{}`
    and `{}` has no `resolved` key."""
    repo, branch, base = _conflicted(tmp_path)
    _pause_rebase(repo, base)
    _resolver(monkeypatch, write=BOTH_LOG, verdict=None)

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert "declined" in detail
    assert not runner._unmerged_paths(repo)


def test_the_resolver_is_bounded(tmp_path, monkeypatch):
    """A resolver that stages nothing and keeps claiming success must not loop for
    the night. `MAX_RESOLVE_ROUNDS` is the ceiling; this asserts the bound exists
    rather than its value."""
    repo, branch, base = _conflicted(tmp_path)
    _pause_rebase(repo, base)
    calls: list[int] = []

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        calls.append(1)
        _verdict_path(prompt).write_text(
            json.dumps({"resolved": True, "summary": "done"}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "host_setting", lambda root, key, default=None: default)

    replayed, _ = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert len(calls) <= runner.MAX_RESOLVE_ROUNDS


# --- git's own auto-merge is not the resolver's edit (`taser-cyberware`) -------
#
# The guard above asserts the resolver's blast radius by asking `git status` which
# tracked paths are dirty. That question has a wrong premise: a paused rebase does
# not leave only its conflicts dirty. Git stops on the *commit*, and everything else
# in that commit which it could merge by itself is already staged — reported as
# modified against HEAD, and indistinguishable from an agent's edit unless somebody
# looked before the agent ran.
#
# On 2026-09-03 `taser-cyberware` stopped on a four-file commit with one conflict.
# The resolver read that conflict, judged it correctly and staged that one file, and
# was told it had edited the other three. The rebase was aborted and a card that had
# already passed gates, tests and review landed in `blocked/` with a merge note
# accusing it of something git had done. Every conflict on a multi-file commit hit
# this, so the check was strictest exactly where it was least entitled to be.

BASE_SETTINGS = "TEMPO = 1\n"
OURS_SETTINGS = "TEMPO = 2\n"
BASE_TESTS = "def test_old(): pass\n"
OURS_TESTS = "def test_old(): pass\ndef test_new(): pass\n"


def _conflicted_multifile(tmp_path: Path) -> tuple[Path, str, str]:
    """`taser-cyberware`'s shape: the commit that conflicts touches four files, and
    only one of them is the conflict.

    The other three are ordinary changes git merges without help — which is what
    puts them in `git status` with the conflict, and what the baseline has to tell
    apart from an agent's stray edit.
    """
    repo = _fixtures.git_init(tmp_path / "repo", branch="main")
    (repo / "log.md").write_bytes(BASE_LOG.encode("utf-8"))
    (repo / "settings.py").write_bytes(BASE_SETTINGS.encode("utf-8"))
    (repo / "tests.py").write_bytes(BASE_TESTS.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "log.md").write_bytes(OURS_LOG.encode("utf-8"))
    (repo / "settings.py").write_bytes(OURS_SETTINGS.encode("utf-8"))
    (repo / "tests.py").write_bytes(OURS_TESTS.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the card: four files, one of them contested")

    # The sibling touches only the log, so only the log can conflict.
    _git(repo, "checkout", "-q", "main")
    (repo / "log.md").write_bytes(THEIRS_LOG.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the sibling")

    _git(repo, "checkout", "-q", "feature")
    return repo, "feature", "main"


def test_git_s_own_auto_merged_files_are_not_called_stray_edits(tmp_path, monkeypatch):
    """The `taser-cyberware` regression, end to end through a real rebase.

    The resolver touches nothing but the conflict, and the three files git staged by
    itself must not be read as its doing. Before the baseline this returned
    `False` with a note naming all three, and the card was blocked for it.
    """
    repo, branch, base = _conflicted_multifile(tmp_path)
    _pause_rebase(repo, base)
    # The premise the old check got wrong: git has these dirty already, before any
    # agent has run. If this ever stops being true the test below proves nothing.
    dirty = set(runner._porcelain_paths(repo))
    assert {"settings.py", "tests.py"} <= dirty, dirty

    _resolver(monkeypatch, write=BOTH_LOG,
              verdict={"resolved": True, "summary": "kept both entries"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert replayed, detail
    assert not runner._unmerged_paths(repo)
    # The replayed commit still carries all four files' changes — the point of not
    # aborting is that the card's own work survives intact.
    assert (repo / "log.md").read_text(encoding="utf-8") == BOTH_LOG
    assert (repo / "settings.py").read_text(encoding="utf-8") == OURS_SETTINGS
    assert (repo / "tests.py").read_text(encoding="utf-8") == OURS_TESTS


def test_an_auto_merged_file_the_resolver_rewrites_is_still_a_stray_edit(tmp_path,
                                                                        monkeypatch):
    """The other half, and why the baseline compares content rather than just
    allowing the paths git had already staged.

    A resolver quietly rewriting an auto-merged file is the plausible stray edit this
    guard exists for — the gates and tests that follow would not catch it if it
    happened to be valid. Being in the baseline buys a path nothing; being *unchanged
    since* the baseline is what buys it.
    """
    repo, branch, base = _conflicted_multifile(tmp_path)
    _pause_rebase(repo, base)
    assert "settings.py" in runner._porcelain_paths(repo)

    _resolver(monkeypatch, write=BOTH_LOG, also_touch="settings.py",
              verdict={"resolved": True, "summary": "kept both, and retuned the tempo"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert "settings.py" in detail
    assert "not part of the conflict" in detail
    assert not runner._unmerged_paths(repo), "the rebase must have been aborted"


def test_a_file_dropped_into_an_already_untracked_directory_is_still_a_stray(
        tmp_path, monkeypatch):
    """The blind spot the baseline could have opened, closed before it shipped.

    `git status --porcelain` collapses an untracked directory to one `?? dir/` record,
    so a baseline that digested only the record — or only the directory's name — would
    read identically before and after a resolver dropped a file inside it. Before the
    baseline existed, every untracked path was a stray unconditionally; narrowing that
    by evidence is the intent, narrowing it by blind spot is not.
    """
    repo, branch, base = _conflicted(tmp_path)
    scratch = repo / "scratch"
    scratch.mkdir()
    (scratch / "already-here.txt").write_bytes(b"present before the resolver ran\n")
    _pause_rebase(repo, base)
    # git really does collapse it, so the digest is the only thing that can tell.
    assert "scratch/" in runner._porcelain_paths(repo)

    _resolver(monkeypatch, write=BOTH_LOG, also_touch="scratch/snuck-in.txt",
              verdict={"resolved": True, "summary": "kept both"})

    replayed, detail = _run(repo, tmp_path, branch, base)

    assert not replayed
    assert "scratch" in detail
    assert "not part of the conflict" in detail
