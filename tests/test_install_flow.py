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

from nightshift import discover, init, tiers, uninstall
from nightshift.manifest import AI_DIR


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


# --- uninstall in a repo that was not empty ------------------------------------
#
# The regression this section exists for. `uninstall.plan` used to ask `build_plan`
# what `init` writes and delete the intersection with the disk — but after a run our
# files exist, so they come back as `kept`, the same list as the documents the project
# already had and `init` deliberately left alone. It deleted the operator's CLAUDE.md.
# Reported by the origin project's maintainer, 2026-08-03, before he ran it: *"the
# uninstall deletes claude.md? And architecture? This doesn't sound right."*


OWN_DOCS = {
    "CLAUDE.md": "# My project\n\nMy own hand-written rules.\n",
    # No LF rule: a project that has never heard of it is the realistic case, and it
    # exercises append-then-strip rather than the `satisfied` shortcut.
    ".gitattributes": "*.png binary\n",
    ".claude/memory/arch.md": "# Architecture\n\nMy own notes.\n",
    "docs/tier-binding.md": "# Tiers\n\nMy own document.\n",
}


def _with_own_docs(repo: Path) -> None:
    """Every path `init` also has a template for, written by the project first."""
    for rel, text in OWN_DOCS.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the project's own documents")


def test_uninstall_never_deletes_a_document_the_project_already_had(tmp_path):
    repo = _repo(tmp_path)
    _with_own_docs(repo)
    plan = _install(repo)
    assert not (set(OWN_DOCS) & set(plan.writes)), "these are theirs; never overwritten"

    removal = uninstall.plan(repo)
    assert not (set(OWN_DOCS) & set(removal.files)), removal.files
    uninstall.apply(removal)

    for rel, text in OWN_DOCS.items():
        assert (repo / rel).read_text(encoding="utf-8") == text, rel


def test_the_receipt_records_what_the_run_created_and_not_what_it_kept(tmp_path):
    repo = _repo(tmp_path)
    _with_own_docs(repo)
    _install(repo)

    receipt = json.loads((repo / init.RECEIPT).read_text(encoding="utf-8"))
    created = set(receipt["created"])
    assert ".ai/manifest.toml" in created
    assert not (set(OWN_DOCS) & created)
    # Theirs, appended to — a distinct list, because these may never be deleted.
    assert set(receipt["appended"]) == set(OWN_DOCS) - {".claude/memory/arch.md"}


# --- four files where the content is the requirement ----------------------------
#
# The other half of the same report. `init` used to `stage()` these, so a project that
# already had one got "keep (already present, untouched)" and the requirement was never
# installed: no rules in CLAUDE.md, no LF line in `.gitattributes`, no fenced block for
# `nightshift.tiers`. Measured 2026-08-03 in a repo with three of them: two gate
# violations on the first run, and a tier binding nothing could parse.


def test_an_existing_claude_md_gets_the_rules_rather_than_being_skipped(tmp_path):
    repo = _repo(tmp_path)
    theirs = "# My project\n\n## How to run\n\npython -m src.app\n"
    (repo / "CLAUDE.md").write_text(theirs, encoding="utf-8")

    plan = _install(repo)

    assert "CLAUDE.md" in plan.appends
    assert f"{AI_DIR}/CLAUDE.md" in plan.writes, "the substance goes in a file we own"
    after = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert after.startswith(theirs), "their file is prefix-preserved"
    assert init.BLOCK_BEGIN in after and init.BLOCK_END in after
    assert f"{AI_DIR}/CLAUDE.md" in after, "and it points at the rules"


def test_the_framework_half_stands_alone_when_it_is_its_own_file(tmp_path):
    """It carries the rules that matter, and not the template's description of a
    two-halves-in-one-file split that no longer exists."""
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
    _install(repo)

    half = (repo / AI_DIR / "CLAUDE.md").read_text(encoding="utf-8")
    assert "nightshift.preflight" in half
    assert "[branches].integration" in half
    assert "## Project rules" not in half
    assert "below the line is yours" not in half


def test_an_existing_gitattributes_keeps_its_rules_and_gains_the_lf_line(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")

    _install(repo)

    text = (repo / ".gitattributes").read_text(encoding="utf-8")
    assert "*.png binary" in text
    assert any(line.strip() == init.LF_ATTRIBUTE for line in text.splitlines()), \
        "the line `line_endings` requires"


def test_the_gitattributes_block_has_no_uncommented_prose(tmp_path):
    """A `.gitattributes` has no prose. An unprefixed sentence is a PATTERN with
    attributes — the first draft of this block declared `text` on files named
    `Normalise`, which git parsed without complaint."""
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
    plan = _install(repo)

    for line in plan.appends[".gitattributes"].splitlines():
        if not line.strip():
            continue
        assert line.startswith("#") or line.strip() == init.LF_ATTRIBUTE, line


def test_a_gitattributes_that_already_has_the_rule_is_left_alone(tmp_path):
    """`satisfied`: a file already meeting the requirement its own way gets no block."""
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text(f"{init.LF_ATTRIBUTE}\n*.png binary\n",
                                         encoding="utf-8")
    plan = _install(repo)

    assert ".gitattributes" not in plan.appends
    assert ".gitattributes" in plan.kept


def test_an_existing_binding_doc_gains_the_block_tiers_actually_parses(tmp_path):
    repo = _repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "tier-binding.md").write_text(
        "# Tier binding\n\nI already had a doc here.\n", encoding="utf-8")

    plan = _install(repo)

    assert "docs/tier-binding.md" in plan.appends
    binding = tiers.binding(repo)
    assert binding["worker"] and binding["lead"], binding


def test_a_second_init_appends_nothing_to_the_files_it_wrote_itself(tmp_path):
    """On a re-run, a file THIS TOOL wrote is simply a file that exists. Without the
    identical-content check, `Board/README.md` got the board README appended to itself."""
    repo = _repo(tmp_path)
    _install(repo)

    plan = init.build_plan(repo, integration="dev-work")

    assert not plan.appends, plan.appends
    assert not plan.writes, plan.writes


def test_a_second_init_does_not_append_a_second_block(tmp_path):
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
    _install(repo)
    _install(repo)

    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.count(init.BLOCK_BEGIN) == 1


# --- taking a block back out ---------------------------------------------------


def test_a_fresh_install_ignores_its_own_runtime_output(tmp_path):
    """Without these entries a consuming project commits its run logs, its preflight
    receipt and its per-machine `host.json` — and a committed receipt unblocks a push on
    a machine that never ran the checks. The framework's own repo had the entries from
    the start, which is why nothing noticed `init` never wrote them."""
    repo = _repo(tmp_path)
    _install(repo)

    text = (repo / ".gitignore").read_text(encoding="utf-8")
    for pattern in (f"{AI_DIR}/runs/", f"{AI_DIR}/.preflight",
                    f"{AI_DIR}/host.json", f"{AI_DIR}/STOP"):
        assert pattern in text, pattern


def test_the_gitignore_block_puts_comments_on_their_own_line(tmp_path):
    """In a `.gitignore` a `#` only starts a comment at the beginning of a line — one
    appended after a pattern becomes part of the pattern."""
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    plan = _install(repo)

    for line in plan.appends[".gitignore"].splitlines():
        assert "#" not in line.lstrip("#"), line


def test_an_existing_gitignore_keeps_its_own_patterns(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc\nbuild/\n", encoding="utf-8")
    _install(repo)

    text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "*.pyc" in text and "build/" in text
    assert f"{AI_DIR}/runs/" in text


def test_uninstall_deletes_run_output_it_wrote(tmp_path):
    """`.ai/runs/` is unambiguously ours, so remove-only-if-empty does not apply — it
    left the directory behind after every uninstall that followed a preflight."""
    repo = _repo(tmp_path)
    _install(repo)
    runs = repo / AI_DIR / "runs" / "card-1"
    runs.mkdir(parents=True)
    (runs / "transcript.jsonl").write_text('{"x": 1}\n', encoding="utf-8")

    removal = uninstall.plan(repo)
    assert f"{AI_DIR}/runs" in removal.trees
    uninstall.apply(removal)

    assert not (repo / AI_DIR / "runs").exists()
    assert not (repo / AI_DIR).exists(), "and the parent goes with it, being empty"


def test_uninstall_strips_the_block_and_leaves_the_file(tmp_path):
    repo = _repo(tmp_path)
    theirs = "# My project\n\nMy own rules.\n"
    (repo / "CLAUDE.md").write_text(theirs, encoding="utf-8")
    _install(repo)

    removal = uninstall.plan(repo)
    assert "CLAUDE.md" in removal.blocks
    assert "CLAUDE.md" not in removal.files
    uninstall.apply(removal)

    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == theirs
    assert not (repo / AI_DIR / "CLAUDE.md").exists(), "ours outright, so deleted"


def test_the_block_comes_out_even_with_no_receipt(tmp_path):
    """Marker-driven, not receipt-driven: writing the marker into the file is what
    makes the append self-describing."""
    repo = _repo(tmp_path)
    theirs = "# My project\n\nMy own rules.\n"
    (repo / "CLAUDE.md").write_text(theirs, encoding="utf-8")
    _install(repo)
    _forget_receipt(repo)

    uninstall.apply(uninstall.plan(repo))

    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == theirs


def test_a_block_with_no_end_marker_is_reported_not_stripped(tmp_path):
    """Stripping to end-of-file would take their content with it."""
    repo = _repo(tmp_path)
    (repo / "CLAUDE.md").write_text("# Mine\n", encoding="utf-8")
    _install(repo)
    path = repo / "CLAUDE.md"
    path.write_text(path.read_text(encoding="utf-8").replace(init.BLOCK_END, "truncated"),
                    encoding="utf-8")

    removal = uninstall.plan(repo)

    assert "CLAUDE.md" not in removal.blocks
    assert any("CLAUDE.md" in note and "by hand" in note for note in removal.kept)


def test_strip_block_removes_the_block_and_the_blank_line_before_it():
    text = ("# Mine\n"
            "\n"
            "My own rules.\n"
            "\n"
            "<!-- nightshift:begin — added by init -->\n"
            "\n"
            "## nightshift\n"
            "\n"
            "<!-- nightshift:end -->\n")
    assert init.strip_block(text) == "# Mine\n\nMy own rules.\n"


def test_strip_block_leaves_a_file_without_one_untouched():
    text = "# Mine\n\nNothing of ours here.\n"
    assert init.strip_block(text) == text


def test_a_second_init_accumulates_the_receipt_rather_than_emptying_it(tmp_path):
    """A re-run has almost nothing left to write. Replacing the receipt with that
    would leave one that truthfully says this run created nothing — and an uninstall
    with nothing to remove."""
    repo = _repo(tmp_path)
    _install(repo)
    first = set(json.loads((repo / init.RECEIPT).read_text(encoding="utf-8"))["created"])
    assert first

    _install(repo)                    # writes nothing; must not forget anything either

    again = json.loads((repo / init.RECEIPT).read_text(encoding="utf-8"))
    assert set(again["created"]) == first
    assert again["settings_created"] is True
    assert ".ai/manifest.toml" in uninstall.plan(repo).files


def test_a_crash_before_the_receipt_leaves_no_receipt(tmp_path):
    """Written last, so it never claims a file that was not created."""
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work")
    assert not (repo / init.RECEIPT).exists()
    init.apply(plan)
    assert (repo / init.RECEIPT).is_file()


# --- installs made before receipts existed -------------------------------------


def _forget_receipt(repo: Path) -> None:
    (repo / init.RECEIPT).unlink()


def test_without_a_receipt_our_files_are_still_removed(tmp_path):
    """The fallback: bytes exactly as `init` writes them are ours to remove. This is
    the state of any install made before the receipt existed, including the one the
    maintainer was about to redo."""
    repo = _repo(tmp_path)
    _with_own_docs(repo)
    _install(repo)
    _forget_receipt(repo)

    removal = uninstall.plan(repo)
    assert not removal.receipted
    assert ".ai/manifest.toml" in removal.files
    assert ".claude/skills/manage-board/SKILL.md" in removal.files
    assert not (set(OWN_DOCS) & set(removal.files))


def test_the_fallback_re_renders_with_the_installed_branch(tmp_path):
    """Six templates interpolate the integration branch. Rebuilding the comparison
    with a placeholder made every one of them differ from its own output, so the
    fallback kept files it had written itself — found by running it, not reading it."""
    repo = _repo(tmp_path)
    _install(repo)
    _forget_receipt(repo)

    removal = uninstall.plan(repo)
    branded = ".claude/skills/run-the-runner/SKILL.md"
    assert "dev-work" in (repo / branded).read_text(encoding="utf-8")
    assert branded in removal.files, removal.kept


def test_the_fallback_keeps_a_file_the_operator_edited_after_installing(tmp_path):
    repo = _repo(tmp_path)
    _install(repo)
    _forget_receipt(repo)
    board = repo / "Board" / "README.md"
    board.write_text(board.read_text(encoding="utf-8") + "\n- a lane rule of my own\n",
                     encoding="utf-8")

    removal = uninstall.plan(repo)

    assert "Board/README.md" not in removal.files
    assert any("Board/README.md" in note for note in removal.kept)


def test_the_fallback_will_not_delete_a_settings_file_it_cannot_prove_it_made(tmp_path):
    """With a receipt, `settings_created` licenses the delete. Without one, emptiness
    is not evidence — a project's own bare `{}` looks identical from here."""
    repo = _repo(tmp_path)
    _install(repo)
    _forget_receipt(repo)

    uninstall.apply(uninstall.plan(repo))

    settings = repo / ".claude" / "settings.json"
    assert settings.is_file()
    assert json.loads(settings.read_text(encoding="utf-8")) == {}


def test_a_receipt_claiming_a_path_init_never_writes_is_not_obeyed(tmp_path):
    """The receipt is a committed file like any other. A hand-edited one must not
    turn this into an arbitrary delete."""
    repo = _repo(tmp_path)
    _install(repo)
    path = repo / init.RECEIPT
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["created"].append("pkg/core.py")
    path.write_text(json.dumps(receipt), encoding="utf-8")

    removal = uninstall.plan(repo)

    assert "pkg/core.py" not in removal.files
    assert any("pkg/core.py" in note for note in removal.kept)
    uninstall.apply(removal)
    assert (repo / "pkg" / "core.py").is_file()


def test_init_works_again_after_uninstalling_a_repo_that_had_its_own_docs(tmp_path):
    """The whole point, in the shape the maintainer will meet it: retry an install in
    a project that already had a CLAUDE.md."""
    repo = _repo(tmp_path)
    _with_own_docs(repo)
    _install(repo, permission_mode="default")
    uninstall.apply(uninstall.plan(repo))

    plan = _install(repo, permission_mode="bypassPermissions")

    assert ".ai/manifest.toml" in plan.writes
    assert "CLAUDE.md" not in plan.writes, "still theirs, still never overwritten"
    assert "CLAUDE.md" in plan.appends, "and it gets the pointer block back"


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
