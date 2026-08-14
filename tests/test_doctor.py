"""Tests for `nightshift/doctor.py` — the per-machine precondition checks.

Real `git init` repos and real `.ai/` directories rather than mocks, for the
reason `test_gate_line_endings.py` gives: the defects these checks exist to
catch live in the disagreement between what git stored, what is on disk, and
what this particular box has — and a mock of any of those three is a mock of the
thing under test.

The one thing deliberately patched is `runner.claude_binary`. Doctor's contract
there is "ask the resolver that already owns the question and report its answer,
do not grow a second one"; whether that function handles `CLAUDE_BIN`, PATH and
`~/.local/bin` correctly is tested where it lives. Asserting it *here* would mean
asserting facts about whichever box the suite happens to run on, which is what
these checks exist to report rather than to require.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightshift import doctor, freshness, preflight, runner


@pytest.fixture(autouse=True)
def _pin_the_framework_reading(monkeypatch):
    """The two framework checks read the *installed* checkout — a sibling directory
    belonging to whoever is running the suite. Every test in this file is about the
    set and order of checks, so leaving them live would make the file green or red
    depending on which branch that directory happens to have out, which is the very
    state `paired_branches` exists to report. `test_freshness.py` drives the real
    logic against checkouts it builds itself."""
    monkeypatch.setattr(freshness, "read", lambda checkout=None, *, fetch=True:
                        freshness.Freshness(Path("/framework"), "main", "main",
                                            known=True))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, encoding="utf-8", errors="replace")


def _project(tmp_path: Path, *, hosts: dict | None = None, name: str = "repo") -> Path:
    """A synthetic project: a git repo with the `.ai/` the framework reaches into."""
    repo = tmp_path / name
    (repo / ".ai").mkdir(parents=True)
    _git_init(repo)

    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    # `[branches].integration` is what `preflight.integration_base` reads since
    # 07_portability.md §8 step 4 replaced `.ai/branches.py`. The
    # `preflight-config` check probes that accessor, so an empty manifest here
    # would fail it for the wrong reason.
    (repo / ".ai" / "manifest.toml").write_text(
        '[branches]\nintegration = "development_team"\n', encoding="utf-8", newline="")
    entries = {"some-other-box": {"capabilities": []}} if hosts is None else hosts
    (repo / ".ai" / "hosts.json").write_text(json.dumps(entries, indent=2) + "\n",
                                             encoding="utf-8", newline="")
    # A board, because three of the five checks are about *dispatching* and are
    # skipped where nothing dispatches (`doctor._dispatches`). A fixture testing
    # those checks has to be a repo they apply to — see
    # `test_a_repo_with_no_board_skips_the_three_dispatch_checks` for the other side.
    (repo / "Board" / "tasks").mkdir(parents=True)
    (repo / "Board" / "tasks" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _git_init(repo: Path) -> None:
    _git(repo, "init", "-q")
    # As in `test_gate_line_endings._repo`: the fixture's own bytes must be what
    # gets stored, not whatever this machine's autocrlf would make of them.
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


def _named(results: list, name: str):
    return next(check for check in results if check.name == name)


# --- LF working tree ----------------------------------------------------------


def test_a_clean_project_passes_every_check(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.socket, "gethostname", lambda: "this-box")
    repo = _project(tmp_path, hosts={"this-box": {"capabilities": []}})

    results = doctor.checks(repo)

    assert [check.name for check in results] == [
        "lf-worktree", "worktree-headroom", "claude-bin", "hosts-json",
        "preflight-config", "nightshift", "nightshift-fresh", "paired-branches"]
    assert all(check.ok for check in results), [
        (c.name, c.detail) for c in results if not c.ok]


def test_a_crlf_working_tree_fails_and_names_the_per_machine_fix(tmp_path, monkeypatch):
    """The phantom-dirty case: git holds LF, disk holds CRLF, `git status` reports
    nothing. The fix does not sync — there is nothing to commit — so the detail has
    to name the remedy that repairs *this* checkout.

    That remedy used to be `.ai/normalize_worktree.py`, which existed only in the
    origin project: every other repo was told to run a file it did not have. It
    moved into the package at 07_portability.md §8 step 5, so the detail now names
    a module, which is true wherever the package is installed."""
    monkeypatch.setattr(doctor.socket, "gethostname", lambda: "this-box")
    repo = _project(tmp_path, hosts={"this-box": {}})
    (repo / ".ai" / "hosts.json").write_bytes(b'{"this-box": {}}\r\n')

    check = _named(doctor.checks(repo), "lf-worktree")

    assert not check.ok
    assert "python -m nightshift.normalize_worktree" in check.detail
    assert ".ai/hosts.json" in check.detail


# --- claude on PATH -----------------------------------------------------------


def test_an_unresolvable_claude_binary_fails_and_names_claude_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.runner, "claude_binary", lambda: None)
    repo = _project(tmp_path)

    check = _named(doctor.checks(repo), "claude-bin")

    assert not check.ok
    assert "CLAUDE_BIN" in check.detail


def test_a_resolved_claude_binary_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.runner, "claude_binary", lambda: "/opt/bin/claude")
    repo = _project(tmp_path)

    check = _named(doctor.checks(repo), "claude-bin")

    assert check.ok and "/opt/bin/claude" in check.detail


def test_the_check_asks_the_runners_own_resolver(tmp_path, monkeypatch):
    """Doctor's contract: ask the resolver that already knows about `CLAUDE_BIN`
    and this box's `~/.local/bin`, never grow a second `shutil.which`. Asserted
    by patching that one function and watching the check follow it — which is
    also what stops the two answers drifting apart."""
    calls: list[int] = []
    monkeypatch.setattr(doctor.runner, "claude_binary",
                        lambda: calls.append(1) or "/somewhere/claude")
    repo = _project(tmp_path)

    assert _named(doctor.checks(repo), "claude-bin").detail == "/somewhere/claude"
    assert calls, "the check must go through runner.claude_binary()"


# --- hosts.json ---------------------------------------------------------------


def test_an_unlisted_hostname_fails_and_names_the_host_and_the_file(tmp_path, monkeypatch):
    """The `_TODO-desktop-hostname` incident: an unlisted box does not error, it
    just never dispatches anything that `requires:` a capability. So the check has
    to be red, and it has to print the hostname the maintainer must add."""
    monkeypatch.setattr(doctor.socket, "gethostname", lambda: "unknown-box")
    repo = _project(tmp_path, hosts={"other-box": {"capabilities": []}})

    check = _named(doctor.checks(repo), "hosts-json")

    assert not check.ok
    assert "unknown-box" in check.detail and ".ai/hosts.json" in check.detail


def test_a_listed_hostname_passes_even_with_an_empty_entry(tmp_path, monkeypatch):
    """An entry of `{}` is a configured box with no capabilities — a decision
    someone made. `runner.host_config` cannot tell it from an absent one (both
    resolve to `{}`), which is why this check asks the file for key presence."""
    monkeypatch.setattr(doctor.socket, "gethostname", lambda: "spartan-box")
    repo = _project(tmp_path, hosts={"spartan-box": {}})

    assert _named(doctor.checks(repo), "hosts-json").ok


def test_a_project_with_no_hosts_json_at_all_fails_and_names_it(tmp_path):
    repo = _project(tmp_path)
    (repo / ".ai" / "hosts.json").unlink()

    check = _named(doctor.checks(repo), "hosts-json")

    assert not check.ok
    assert ".ai/hosts.json" in check.detail


def test_an_untracked_host_override_satisfies_the_check(tmp_path, monkeypatch):
    """`.ai/host.json` overrides `hosts.json` wholesale, so a box that has one is
    configured whatever the committed file says about its hostname."""
    monkeypatch.setattr(doctor.socket, "gethostname", lambda: "unknown-box")
    repo = _project(tmp_path, hosts={"other-box": {}})
    (repo / ".ai" / "host.json").write_text('{"capabilities": []}\n',
                                            encoding="utf-8", newline="")

    assert _named(doctor.checks(repo), "hosts-json").ok


# --- what the preflight reads late --------------------------------------------


def test_an_undeclared_integration_branch_fails_and_names_the_key(tmp_path):
    """Read deep inside the preflight's pytest step, after the gates and the
    audit matrix have already run — the most expensive possible place to learn
    that the manifest is incomplete. The `ManifestError`'s own text must survive
    to the surface, not be swallowed into a generic 'config failed'."""
    repo = _project(tmp_path)
    (repo / ".ai" / "manifest.toml").write_text('[branches]\nstable = "main"\n',
                                                encoding="utf-8", newline="")

    check = _named(doctor.checks(repo), "preflight-config")

    assert not check.ok
    assert "branches.integration" in check.detail


# --- the framework version (answer A: report only) ----------------------------
#
# Karel, 2026-08-01: report the installed commit, never fail on it. A hard pin is
# what `07_portability.md` §3 explicitly defers ("pin to a tag once it settles"),
# so these tests pin the *reporting*, and one of them pins the never-fails half
# against the exact state a pin would have rejected.


def test_the_framework_check_reports_the_installed_commit(tmp_path):
    checkout = tmp_path / "framework"
    checkout.mkdir()
    _git_init(checkout)
    (checkout / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "one")
    sha = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()

    check = doctor.framework_version(tmp_path, checkout=checkout)

    assert check.ok
    assert sha[:8] in check.detail
    assert "clean" in check.detail


def test_a_dirty_framework_checkout_is_reported_but_never_fails(tmp_path):
    checkout = tmp_path / "framework"
    checkout.mkdir()
    _git_init(checkout)
    (checkout / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "one")
    (checkout / "a.py").write_text("x = 2\n", encoding="utf-8", newline="")

    check = doctor.framework_version(tmp_path, checkout=checkout)

    assert check.ok, "answer A — the version check reports, it does not enforce"
    assert "uncommitted" in check.detail


def test_a_non_git_install_still_reports_something_useful(tmp_path):
    """A wheel install into site-packages has no commit to report. That is not a
    failure — it is the normal shape of a non-editable install."""
    plain = tmp_path / "site-packages-ish"
    plain.mkdir()

    check = doctor.framework_version(tmp_path, checkout=plain)

    assert check.ok
    assert "not a git checkout" in check.detail


# --- wiring into the preflight ------------------------------------------------


def test_the_preflight_runs_the_doctor_checks_before_the_gates(tmp_path, monkeypatch):
    """Order is the whole point of putting these in the preflight rather than
    leaving them to a command nobody runs: a CRLF worktree produces one violation
    per file from `line_endings`, and on 2026-07-30 that was 304 lines. The doctor
    line has to be above them."""
    monkeypatch.setattr(doctor.socket, "gethostname", lambda: "this-box")
    repo = _project(tmp_path, hosts={"this-box": {}})

    result = preflight.run_checks(repo, "development_team",
                                  no_corrections="fixture", skip_tests=True)

    names = [check.name for check in result.checks]
    assert names[:8] == ["lf-worktree", "worktree-headroom", "claude-bin", "hosts-json",
                         "preflight-config", "nightshift", "nightshift-fresh",
                         "paired-branches"]
    assert names.index("gates") == len(doctor.CHECKS)


def test_a_repo_with_no_board_skips_the_four_dispatch_checks(tmp_path):
    """`claude`, the host entry, the integration branch and worktree headroom
    are dispatch preconditions, and a repo with no board never dispatches — it
    never cuts a worktree either, so the headroom check has nothing to measure.

    Reporting them as passes would be a lie in the reassuring direction; reporting
    them as failures makes the framework's own checkout permanently red for not
    being configured as its own consumer, which is how a report gets skimmed. So
    they are skipped, and a skip does not count against the verdict.
    """
    repo = _project(tmp_path)
    (repo / "Board" / "tasks" / ".gitkeep").unlink()
    (repo / "Board" / "tasks").rmdir()
    (repo / "Board").rmdir()

    by_name = {c.name: c for c in doctor.checks(repo)}
    for name in ("worktree-headroom", "claude-bin", "hosts-json", "preflight-config"):
        assert by_name[name].skipped, name
        assert by_name[name].ok, f"{name}: a skip must not fail the run"
        assert "no board" in by_name[name].detail
    # The two that are about the checkout rather than about dispatch still run.
    assert not by_name["lf-worktree"].skipped
    assert not by_name["nightshift"].skipped


def test_a_repo_with_a_board_still_runs_the_dispatch_checks(tmp_path):
    """The other direction, so the skip cannot silently swallow a real failure."""
    repo = _project(tmp_path)  # `_project` gives it a board
    by_name = {c.name: c for c in doctor.checks(repo)}
    for name in ("worktree-headroom", "claude-bin", "hosts-json", "preflight-config"):
        assert not by_name[name].skipped, name
    # This host is not in the fixture's hosts.json, so that check must really fail.
    assert not by_name["hosts-json"].ok


# --- worktree headroom (nightshift-worktree-paths-not-defensive-on-windows) ---
#
# The Windows `MAX_PATH` (260 chars) check. `headroom()` is pure arithmetic and
# is tested against synthetic ints, never against an actual long path — the
# working `MAX_PATH` this check exists to report on would refuse to create one.
# `worst_relative`/`worst_worktree_name` are tested against small real repos,
# same reasoning as everywhere else in this file: the defect they exist to
# catch is a disagreement between what git tracks and what a checkout would
# actually generate, and a mock of `git ls-files` is a mock of the thing under
# test.


def _card(repo: Path, lane: str, card_id: str) -> None:
    lane_dir = repo / "Board" / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    (lane_dir / f"{card_id}.md").write_text(
        f"---\nid: {card_id}\ntitle: fixture\nstate: {lane}\n---\n\n## Intent\n\nfixture\n",
        encoding="utf-8", newline="")


def test_headroom_arithmetic_green_warn_fail():
    """The 259/30 thresholds from the card's Approach: green above 30 chars of
    slack, warn 0-30, fail below 0 — computed from three plain ints, so no
    filesystem and no 300-character path is ever needed to exercise every
    branch."""
    # 259 - (10+1+10+1+10) = 227 of slack — comfortably green.
    slack, status = doctor.headroom(10, 10, 10)
    assert status == doctor.GREEN and slack == 227

    # 259 - (235+1+1+1+1) = 20: inside the 0-30 warn band.
    slack, status = doctor.headroom(235, 1, 1)
    assert slack == 20
    assert status == doctor.WARN

    # 259 - (200+1+50+1+50) = -43: already over budget.
    slack, status = doctor.headroom(200, 50, 50)
    assert status == doctor.FAIL and slack < 0


def test_worst_relative_prefers_the_pycache_equivalent_when_it_is_longer(tmp_path):
    """The measurement that decided this function needs both halves: on the
    real project the tracked maximum was 74 and the generated pytest-bytecode
    equivalent was 85 — `git ls-files` alone under-reports by 11. This fixture
    reproduces the same shape at a small scale: a short tracked `.py` file
    whose `__pycache__` name (stem + cache tag + pytest version) is longer than
    any tracked path in the repo.
    """
    repo = _project(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add a.py")

    worst_len, worst_path = doctor.worst_relative(repo)

    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"],
                             capture_output=True, text=True, check=True).stdout.splitlines()
    tracked_max = max(len(p) for p in tracked)
    assert worst_len > tracked_max, "the generated cache path must beat every tracked path"
    assert worst_path.startswith("__pycache__/a.")
    assert worst_path.endswith(".pyc")


def test_worst_relative_falls_back_to_the_tracked_maximum_without_pytest(tmp_path, monkeypatch):
    """A box that has not installed the dev extra yet still gets a real answer
    — under-reported, never a crash. `_pytest_version` is the one seam that
    knows how to ask, so it is the one patched."""
    repo = _project(tmp_path)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add a.py")
    monkeypatch.setattr(doctor, "_pytest_version", lambda: None)

    worst_len, worst_path = doctor.worst_relative(repo)

    assert "__pycache__" not in worst_path
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"],
                             capture_output=True, text=True, check=True).stdout.splitlines()
    assert worst_len == max(len(p) for p in tracked)


def test_worst_worktree_name_uses_the_longest_card_id_on_the_board(tmp_path):
    repo = _project(tmp_path)
    _card(repo, "tasks", "short")
    _card(repo, "review", "a-considerably-longer-card-id-than-the-others")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add cards")

    name_len, name = doctor.worst_worktree_name(repo)

    longest_id = "a-considerably-longer-card-id-than-the-others"
    assert name == f"_review-{longest_id}"
    assert name_len == len(f"_review-{longest_id}")


def test_worst_worktree_name_falls_back_to_merge_check_with_an_empty_board(tmp_path):
    """No cards at all: `_review-`/`_rebase-` collapse to their bare 8-char
    prefixes, shorter than `_merge-check`'s fixed 12 — so the fixed name wins,
    proving the max is really taken over all three candidates and not just
    assumed to be `_review-`/`_rebase-`."""
    repo = _project(tmp_path)  # a board with no cards in it

    name_len, name = doctor.worst_worktree_name(repo)

    assert name == "_merge-check"
    assert name_len == len("_merge-check")


def test_worktree_headroom_is_green_and_names_its_three_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "nt")
    repo = _project(tmp_path)

    check = _named(doctor.checks(repo), "worktree-headroom")

    assert check.ok and not check.skipped
    assert "worktree root" in check.detail
    assert "worktree name" in check.detail
    assert "longest path" in check.detail
    assert "slack" in check.detail


def test_worktree_headroom_fails_when_slack_is_negative(tmp_path, monkeypatch):
    """Forced with synthetic inputs, same reasoning as `test_headroom_arithmetic_
    green_warn_fail` — never by actually creating a 260-character path."""
    monkeypatch.setattr(doctor.os, "name", "nt")
    repo = _project(tmp_path)
    monkeypatch.setattr(doctor.runner, "worktree_root", lambda root: Path("x" * 200))
    monkeypatch.setattr(doctor, "worst_worktree_name", lambda root: (40, "_review-x"))
    monkeypatch.setattr(doctor, "worst_relative", lambda root: (40, "some/long/path.py"))

    check = _named(doctor.checks(repo), "worktree-headroom")

    assert not check.ok
    assert "MAX_PATH" in check.detail
    assert "LongPathsEnabled" in check.detail
    assert "core.longpaths" not in check.detail.split("Not ")[0]  # only named as the thing to avoid


def test_worktree_headroom_warns_twenty_chars_from_the_edge(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.os, "name", "nt")
    repo = _project(tmp_path)
    monkeypatch.setattr(doctor.runner, "worktree_root", lambda root: Path("x" * 10))
    # used = 10 + 1 + 10 + 1 + rel_len; rel_len=217 makes used=239, slack=20.
    monkeypatch.setattr(doctor, "worst_worktree_name", lambda root: (10, "_review-x"))
    monkeypatch.setattr(doctor, "worst_relative", lambda root: (217, "a" * 217))

    check = _named(doctor.checks(repo), "worktree-headroom")

    assert check.ok, "a warn is still a pass — only negative slack fails"
    assert "thin" in check.detail


def test_worktree_headroom_reports_not_applicable_on_posix(tmp_path, monkeypatch):
    """Calls the check function directly rather than through `doctor.checks()`
    — patching `os.name` process-wide and then running every other check
    (`claude_on_path` among them) breaks on a real Windows box, since some of
    those go through `pathlib` machinery that inspects `os.name` itself. The
    subject here is only whether this one check degrades to "not applicable"."""
    monkeypatch.setattr(doctor.os, "name", "posix")
    repo = _project(tmp_path)

    check = doctor.worktree_headroom(repo)

    assert check.skipped and check.ok
    assert "not applicable" in check.detail


# --- the Windows long-path backstop (`runner._worktree_add`) ------------------
#
# The two stderr shapes below are quoted verbatim from the 2026-08-06 sweep
# recorded on `nightshift-worktree-paths-not-defensive-on-windows`.


class _Result:
    def __init__(self, returncode: int, stderr: str = "", stdout: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_worktree_add_passes_through_a_clean_result(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_git", lambda root, *a: _Result(0))

    result = runner._worktree_add(tmp_path, "--detach", str(tmp_path), "main")

    assert result.returncode == 0


def test_worktree_add_passes_through_an_unrelated_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_git",
                        lambda root, *a: _Result(128, stderr="fatal: not a git repository"))

    result = runner._worktree_add(tmp_path, "--detach", str(tmp_path), "main")

    assert result.returncode == 128
    assert "not a git repository" in result.stderr


def test_worktree_add_raises_on_filename_too_long(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_git", lambda root, *a: _Result(
        128, stderr="fatal: unable to create file 'a/very/long/path.py': Filename too long"))
    monkeypatch.setattr(doctor, "worst_worktree_name", lambda root: (10, "_review-x"))
    monkeypatch.setattr(doctor, "worst_relative", lambda root: (20, "some/path.py"))

    with pytest.raises(runner.WorktreePathTooLong) as excinfo:
        runner._worktree_add(tmp_path, "--detach", str(tmp_path), "main")

    message = str(excinfo.value)
    assert "MAX_PATH" in message
    assert "LongPathsEnabled" in message


def test_worktree_add_raises_on_git_dir_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_git",
                        lambda root, *a: _Result(128, stderr="fatal: '$GIT_DIR' too big"))
    monkeypatch.setattr(doctor, "worst_worktree_name", lambda root: (10, "_review-x"))
    monkeypatch.setattr(doctor, "worst_relative", lambda root: (20, "some/path.py"))

    with pytest.raises(runner.WorktreePathTooLong) as excinfo:
        runner._worktree_add(tmp_path, "--detach", str(tmp_path), "main")

    assert "MAX_PATH" in str(excinfo.value)
