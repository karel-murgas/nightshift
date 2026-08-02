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

from nightshift import doctor, preflight

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
        "lf-worktree", "claude-bin", "hosts-json", "preflight-config", "nightshift"]
    assert all(check.ok for check in results), [
        (c.name, c.detail) for c in results if not c.ok]


def test_a_crlf_working_tree_fails_and_names_the_per_machine_fix(tmp_path, monkeypatch):
    """The phantom-dirty case: git holds LF, disk holds CRLF, `git status` reports
    nothing. The fix does not sync — there is nothing to commit — so the detail has
    to name the script that repairs *this* checkout."""
    monkeypatch.setattr(doctor.socket, "gethostname", lambda: "this-box")
    repo = _project(tmp_path, hosts={"this-box": {}})
    (repo / ".ai" / "hosts.json").write_bytes(b'{"this-box": {}}\r\n')

    check = _named(doctor.checks(repo), "lf-worktree")

    assert not check.ok
    assert ".ai/normalize_worktree.py" in check.detail
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
    assert names[:5] == ["lf-worktree", "claude-bin", "hosts-json", "preflight-config",
                         "nightshift"]
    assert names.index("gates") == 5
