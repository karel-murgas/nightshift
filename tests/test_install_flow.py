"""The install is a thing a person does once, badly, and wants to redo.

Written after the first real install by someone who had not built it (the origin
project's maintainer, 2026-08-02). It got as far as `nightshift init` and stopped, because
`init` finished by printing a section headed `your call:` that named three
decisions and gave no way to make any of them:

    ! hosts.permission_mode: never inferred. ...
    ! memory.budget_bytes: left unset. Set it to a round number you are
      willing to defend out loud

Verbatim: *"there are some 'your calls' in the console and I have no idea how to
make these calls"* and *"'say max size for your files' is no good. I need a
proposition."* Both are correct, and both are the same defect — a *decision* was
being reported as *advice*. The rule those notes were protecting (a field that
bounds behaviour is declared, never probed) is satisfied by asking, and asking is
strictly better: the consequence is on screen at the moment of choosing.

So the tests here cover the interview, the answers reaching the files they belong
in, and the round trip that makes a bad first install recoverable.
"""
from __future__ import annotations

import builtins
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import discover, init, uninstall


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, encoding="utf-8", errors="replace")


def _repo(tmp_path: Path, name: str = "proj") -> Path:
    """A plausible fresh project: one package, one test, two branches."""
    repo = tmp_path / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "core.py").write_text("def run(n):\n    return n * 2\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "from pkg.core import run\n\n\ndef test_run():\n    assert run(2) == 4\n",
        encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    _git(repo, "checkout", "-q", "-b", "dev-work")
    return repo


@pytest.fixture
def answers(monkeypatch):
    """Queue keystrokes for the interview. Exhausted queue behaves like EOF."""
    def queue(*values: str):
        it = iter(values)
        monkeypatch.setattr(builtins, "input", lambda _="": next(it, ""))
    return queue


# --- the interview answers, and where they land --------------------------------


def test_permission_mode_defaults_to_the_safe_answer(answers):
    """Pressing Enter must never arm the machine. This is the whole reason asking
    is an acceptable substitute for a hand-written declaration."""
    answers("")
    assert init.ask_permission_mode(interactive=True) == "default"


def test_permission_mode_can_be_chosen_by_number(answers):
    answers("3")
    assert init.ask_permission_mode(interactive=True) == "bypassPermissions"


def test_permission_mode_can_be_chosen_by_name(answers):
    """A value typed instead of an index — the operator who already knows the
    vocabulary should not have to count menu entries."""
    answers("acceptEdits")
    assert init.ask_permission_mode(interactive=True) == "acceptEdits"


def test_a_nonsense_answer_is_re_asked_not_accepted(answers, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers("banana", "2")
    assert init.ask_permission_mode(interactive=True) == "acceptEdits"


def test_non_interactive_never_arms_the_machine():
    assert init.ask_permission_mode(interactive=False) == "default"


def test_the_budget_offers_a_round_number_never_the_measured_one(answers):
    """`memory.budget_bytes` proposes 100 KB by default.

    The rule (doc 10 §4) is that a budget must not be measured off the tree as
    found, because such a budget can only ever be satisfied. It does *not* require
    refusing to propose anything at all, which is what made the old note useless.
    A round number is defensible precisely because it is arbitrary.
    """
    answers("")
    assert init.ask_budget(interactive=True) == 100_000


def test_the_budget_can_be_switched_off(answers):
    answers("3")
    assert init.ask_budget(interactive=True) is None


def test_capabilities_default_to_empty_and_parse_a_list(answers):
    answers("")
    assert init.ask_capabilities(interactive=True) == []
    answers(" gpu-box , audio-stack ")
    assert init.ask_capabilities(interactive=True) == ["gpu-box", "audio-stack"]


def test_the_permission_answer_reaches_hosts_json(tmp_path):
    """The template used to be copied verbatim, so every install got
    `permission_mode: "default"` plus a note telling the operator to change it."""
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work",
                           permission_mode="bypassPermissions",
                           capabilities=["gpu-box"])
    hosts = json.loads(plan.writes[".ai/hosts.json"])
    entry = next(v for k, v in hosts.items() if not k.startswith("_"))
    assert entry["permission_mode"] == "bypassPermissions"
    assert entry["capabilities"] == ["gpu-box"]


def test_the_budget_answer_reaches_the_manifest(tmp_path):
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work", budget_bytes=100_000)
    assert "budget_bytes = 100000" in plan.writes[".ai/manifest.toml"]


def test_no_budget_means_no_key_at_all(tmp_path):
    """Absence is the documented off switch, so it must be absence and not a zero."""
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work", budget_bytes=None)
    assert "budget_bytes" not in plan.writes[".ai/manifest.toml"]


# --- the closing screen --------------------------------------------------------


def test_next_steps_is_a_numbered_list_of_commands(tmp_path, capsys):
    """What replaced `your call:`. The test is about shape rather than wording:
    numbered, and every item carries a runnable command."""
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work")
    init.next_steps(plan, integration="dev-work", permission_mode="default")

    out = capsys.readouterr().out
    for n in ("1.", "2.", "3.", "4.", "5."):
        assert n in out
    assert "nightshift doctor" in out
    assert "python -m nightshift.gates.run" in out
    assert "python -m nightshift.preflight" in out
    assert "your call" not in out.lower()


def test_next_steps_says_dispatch_is_off_when_it_is(tmp_path, capsys):
    """The commonest first-install confusion: a card that will not run because the
    safe permission mode cannot execute Bash. Say so on the closing screen rather
    than letting them find out by burning an attempt."""
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work")
    init.next_steps(plan, integration="dev-work", permission_mode="default")
    out = capsys.readouterr().out
    assert "bypassPermissions" in out and "dispatch is off" in out


def test_next_steps_refuses_to_pretend_without_an_integration_branch(tmp_path, capsys):
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration=None)
    init.next_steps(plan, integration=None, permission_mode="default")
    out = capsys.readouterr().out
    assert "STOP" in out and "[branches]" in out


# --- the round trip ------------------------------------------------------------


def _install(repo: Path, **kw) -> init.Plan:
    plan = init.build_plan(repo, integration="dev-work", **kw)
    init.apply(plan)
    return plan


def test_uninstall_removes_exactly_what_init_wrote(tmp_path):
    repo = _repo(tmp_path)
    before = {p for p in repo.rglob("*") if ".git" not in p.parts}
    _install(repo)
    assert (repo / ".ai" / "manifest.toml").is_file()

    removal = uninstall.plan(repo)
    uninstall.apply(removal)

    after = {p for p in repo.rglob("*") if ".git" not in p.parts}
    assert after == before, sorted(str(p.relative_to(repo)) for p in after ^ before)


def test_uninstall_reports_before_it_removes(tmp_path):
    """Dry-run by default: it is a delete, however well scoped."""
    repo = _repo(tmp_path)
    _install(repo)
    assert uninstall.main(["--root", str(repo)]) == 0
    assert (repo / ".ai" / "manifest.toml").is_file(), "reporting must not remove"


def test_init_works_again_after_uninstall(tmp_path):
    """The point of the whole command. `init` never overwrites, so without this a
    first install that went wrong could not be redone."""
    repo = _repo(tmp_path)
    _install(repo, permission_mode="default")
    uninstall.apply(uninstall.plan(repo))

    plan = _install(repo, permission_mode="bypassPermissions")
    assert ".ai/manifest.toml" in plan.writes, "a second init must write, not keep"
    hosts = json.loads((repo / ".ai" / "hosts.json").read_text(encoding="utf-8"))
    entry = next(v for k, v in hosts.items() if not k.startswith("_"))
    assert entry["permission_mode"] == "bypassPermissions"


def test_uninstall_keeps_a_corrections_log_with_real_entries(tmp_path):
    """Earned evidence, and the only artefact here that cannot be regenerated. A
    tidy-up that deletes it has destroyed the one thing with history in it."""
    repo = _repo(tmp_path)
    _install(repo)
    log = repo / ".ai" / "corrections.log"
    log.write_text(
        "# a comment\n2026-08-02 | a-slug | some-class | maintainer | yes-now | "
        + "x" * 60 + "\n", encoding="utf-8")

    uninstall.apply(uninstall.plan(repo))

    assert log.is_file(), "a log with real entries must survive"
    assert ".ai/corrections.log" not in uninstall.plan(repo).files


def test_uninstall_leaves_an_untouched_seed_log_alone_to_delete(tmp_path):
    """The mirror case: a log that is still only the shipped header has nothing to
    preserve, so it goes with the rest."""
    repo = _repo(tmp_path)
    _install(repo)
    assert ".ai/corrections.log" in uninstall.plan(repo).files


def test_uninstall_keeps_the_projects_own_settings_keys(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")
    _install(repo)

    uninstall.apply(uninstall.plan(repo))

    settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings == {"permissions": {"allow": ["Bash(ls)"]}}


def test_uninstall_deletes_a_settings_file_that_held_only_our_hooks(tmp_path):
    """`init` created it, and `{}` carries no information — leaving it behind is a
    file whose only content is that something used to be here."""
    repo = _repo(tmp_path)
    _install(repo)
    uninstall.apply(uninstall.plan(repo))
    assert not (repo / ".claude" / "settings.json").exists()


def test_uninstall_never_removes_a_directory_holding_the_operators_work(tmp_path):
    repo = _repo(tmp_path)
    _install(repo)
    (repo / "Board" / "tasks" / "my-card.md").write_text("mine\n", encoding="utf-8")

    uninstall.apply(uninstall.plan(repo))

    assert (repo / "Board" / "tasks" / "my-card.md").is_file()


def test_uninstall_on_a_repo_with_no_install_says_so(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert uninstall.main(["--root", str(repo)]) == 0
    assert "Nothing to remove" in capsys.readouterr().out


def test_strip_hooks_leaves_a_projects_own_hook_under_the_same_matcher(tmp_path):
    """Matching is on `nightshift.` in the command, so a project's own PostToolUse
    hook on Write|Edit survives while ours is removed."""
    existing = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Write|Edit", "hooks": [
                    {"type": "command", "command": "python -m nightshift.gates.run; exit 0"},
                    {"type": "command", "command": "npm run lint"},
                ]},
            ]
        }
    }
    merged, removed = uninstall.strip_hooks(existing)
    assert removed == 1
    assert merged["hooks"]["PostToolUse"][0]["hooks"] == [
        {"type": "command", "command": "npm run lint"}]


# --- the interview is reachable the way a person reaches it --------------------


def test_init_help_does_not_die_on_a_windows_console():
    """The interview's headings carry box drawing and arrows, and the stdout
    reconfigure used to happen after `parse_args` — so any path reaching a prompt
    without going through `main()` died with a UnicodeEncodeError about its own
    help text. Same trap as `reconcile --help`, found the same way."""
    done = subprocess.run([sys.executable, "-m", "nightshift.init", "--help"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr
    assert "--permission-mode" in done.stdout
    assert "--non-interactive" in done.stdout


def test_survey_still_offers_the_two_optional_confirms(tmp_path):
    """The interview asks about these; if discovery stopped proposing them the
    questions would silently vanish rather than fail."""
    repo = _repo(tmp_path)
    (repo / "pkg" / "ui").mkdir()
    (repo / "pkg" / "ui" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "ui" / "panel.py").write_text("import pkg.core\n", encoding="utf-8")

    keys = {p.key for p in discover.survey(repo) if p.needs_confirmation}
    assert "branches.integration" in keys
    assert "layering.forbid" in keys
