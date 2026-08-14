"""`nightshift.freshness` — see it, offer it, never do it silently.

Three properties, and the first two are about restraint rather than capability:

**Nothing here moves a working tree on its own.** The instinct this module exists
to refuse is "every automation should pull the framework first"; an auto-pull does
not prevent the framework moving under a run, it schedules it. So `read()` fetches
— read-only, cannot move a tree — and `pull()` is a separate act that refuses every
state that is not the routine fast-forward.

**Unknown is never reported as fine.** A wheel install, a branch with no upstream
and a failed fetch all have to read as "could not look", because a check that could
not run must not look like a check that found nothing.

**The paired-branch rule is the one that refuses.** It is the only question here
whose answer decides whether a green suite means anything, and it is invisible from
either repo on its own, which is why it lives at the push boundary.

Real repositories with a real remote throughout: the subject is git's own
ahead/behind accounting, and a stub of it would only assert that the stub works.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift import freshness

_MANIFEST = """
[project]
name = "myapp"
source_dirs = ["myapp"]

[branches]
integration = "development_team"
stable = "main"
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A clone of a real bare remote, on `main`, up to date and clean."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / ".ai").mkdir()
    (seed / ".ai" / "manifest.toml").write_text(_MANIFEST, encoding="utf-8")
    (seed / "file.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "branch", "-M", "main")
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    return work


def _advance_remote(checkout: Path, n: int = 1) -> None:
    """Put `n` commits on the remote that this checkout does not have."""
    origin = checkout.parent / "origin.git"
    other = checkout.parent / "other"
    if not other.exists():
        _git(checkout.parent, "clone", "-q", str(origin), str(other))
        _git(other, "config", "user.email", "t@t")
        _git(other, "config", "user.name", "t")
    for i in range(n):
        (other / f"new{i}.txt").write_text("x\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", f"remote {i}")
    _git(other, "push", "-q", "origin", "main")


# --- reading ------------------------------------------------------------------


def test_a_current_checkout_reads_as_up_to_date(checkout):
    state = freshness.read(checkout)
    assert state.known and state.behind == 0 and state.ahead == 0
    assert state.branch == "main" and state.on_default
    assert "up to date" in freshness.describe(state)


def test_being_behind_is_counted_and_the_command_is_offered(checkout):
    _advance_remote(checkout, 2)
    state = freshness.read(checkout)
    assert state.behind == 2 and state.behind_remote
    described = freshness.describe(state)
    assert "2 commit(s) behind" in described
    assert "--pull" in described


def test_fetching_does_not_move_the_working_tree(checkout):
    """The whole reason a fetch is safe to do on the way to something else. If
    this ever stops being true, every caller that reads freshness becomes a caller
    that can change the code under a run."""
    _advance_remote(checkout, 1)
    before = (checkout / "file.txt").read_text(encoding="utf-8")
    head = _git(checkout, "rev-parse", "HEAD").stdout
    freshness.read(checkout)
    assert (checkout / "file.txt").read_text(encoding="utf-8") == before
    assert _git(checkout, "rev-parse", "HEAD").stdout == head
    assert not (checkout / "new0.txt").exists()


def test_a_non_git_install_reads_as_unknown_not_as_fine(tmp_path):
    state = freshness.read(tmp_path / "nowhere")
    assert not state.known and not state.behind_remote
    assert "unknown" in freshness.describe(state)


def test_a_branch_with_no_upstream_reads_as_unknown(checkout):
    _git(checkout, "switch", "-q", "-c", "local-only")
    state = freshness.read(checkout)
    assert not state.known and "upstream" in state.reason


# --- the pull, and everything it refuses --------------------------------------


def test_the_offer_is_a_fast_forward_and_it_actually_lands(checkout):
    _advance_remote(checkout, 1)
    done, detail = freshness.pull(freshness.read(checkout))
    assert done, detail
    assert (checkout / "new0.txt").exists()


def test_a_dirty_tree_is_said_rather_than_resolved(checkout):
    """Where a helpful automation does damage: someone is mid-something here, and
    finishing it for them is not maintenance."""
    _advance_remote(checkout, 1)
    (checkout / "file.txt").write_text("edited\n", encoding="utf-8")
    done, detail = freshness.pull(freshness.read(checkout))
    assert not done and "uncommitted" in detail
    assert (checkout / "file.txt").read_text(encoding="utf-8") == "edited\n"


def test_a_feature_branch_is_never_pulled_into(checkout):
    """A pull of the default branch while a paired feature branch is checked out
    would check out away from it and make the paired code vanish mid-run."""
    _advance_remote(checkout, 1)
    _git(checkout, "switch", "-q", "-c", "ai/paired")
    _git(checkout, "push", "-q", "-u", "origin", "ai/paired")
    state = freshness.read(checkout)
    assert "default branch" in freshness.refuse_pull(state)


def test_local_commits_the_remote_lacks_are_not_a_fast_forward(checkout):
    _advance_remote(checkout, 1)
    (checkout / "mine.txt").write_text("mine\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", "mine")
    done, detail = freshness.pull(freshness.read(checkout))
    assert not done and "fast-forward" in detail


def test_an_up_to_date_checkout_is_not_offered_a_pull(checkout):
    """Refusing is first-class, and so is having nothing to offer: neither may
    produce a paragraph every time something reads this."""
    state = freshness.read(checkout)
    assert freshness.refuse_pull(state) == "it is already up to date"
    assert "--pull" not in freshness.describe(state)


# --- the paired-branch rule ---------------------------------------------------


def test_a_framework_on_its_default_branch_is_always_paired(checkout):
    state = freshness.read(checkout, fetch=False)
    for here in ("main", "test", "ai/some-card", "karel/scratch"):
        assert freshness.unpaired(here, state) == "", here


def test_matching_branch_names_are_what_a_paired_change_looks_like(checkout):
    _git(checkout, "switch", "-q", "-c", "ai/dispatch-cost")
    state = freshness.read(checkout, fetch=False)
    assert freshness.unpaired("ai/dispatch-cost", state) == ""


def test_a_framework_feature_branch_beside_a_different_one_is_refused(checkout):
    """The state neither repo can see: this suite would be green or red depending
    on what a sibling directory happens to have out."""
    _git(checkout, "switch", "-q", "-c", "ai/framework-work")
    state = freshness.read(checkout, fetch=False)
    why = freshness.unpaired("test", state)
    assert why
    assert "ai/framework-work" in why and "test" in why
    assert "main" in why, "the way out has to be named, not just the problem"


def test_pairing_is_silent_when_there_is_no_branch_to_pair_with(tmp_path):
    state = freshness.read(tmp_path / "nowhere", fetch=False)
    assert freshness.unpaired("test", state) == ""


# --- the doctor checks these back --------------------------------------------


def test_the_freshness_check_reports_and_never_fails(checkout):
    from nightshift import doctor

    _advance_remote(checkout, 3)
    check = doctor.framework_freshness(checkout, checkout)
    assert check.ok, "being behind on purpose is allowed; this must not become a nag"
    assert "3 commit(s) behind" in check.detail


def test_the_paired_check_is_the_one_that_refuses(tmp_path, checkout):
    from nightshift import doctor

    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "t")
    (project / "x.txt").write_text("x\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "seed")
    _git(project, "branch", "-M", "test")

    assert doctor.paired_branches(project, checkout).ok       # framework on main
    _git(checkout, "switch", "-q", "-c", "ai/framework-work")
    assert not doctor.paired_branches(project, checkout).ok


def test_the_paired_check_does_not_touch_the_network(checkout, monkeypatch):
    """A comparison of two local HEADs. Making the push boundary depend on a
    network is how an offline box stops being able to publish anything."""
    from nightshift import doctor

    def no_fetch(cwd, *args, timeout=None):
        assert "fetch" not in args, "paired_branches fetched"
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)

    monkeypatch.setattr(freshness, "_git", no_fetch)
    doctor.paired_branches(checkout.parent / "work", checkout)
