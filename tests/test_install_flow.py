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

from nightshift import board, discover, init, tiers, uninstall
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


def test_next_steps_ends_with_two_commands_the_second_being_an_agent(tmp_path, capsys):
    """First this printed decisions with no way to act on them; then a five-step
    checklist of gate-reading and repair — which is precisely the job this framework
    exists to hand to an agent. The maintainer, 2026-08-03: *"It's not meant to be used
    by a human, but used by AI... Not human, who checks gates manually now."* So: commit,
    then `nightshift fix`."""
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work")
    init.next_steps(plan, integration="dev-work", permission_mode="bypassPermissions")

    out = capsys.readouterr().out
    assert "1." in out and "2." in out
    assert "3." not in out, "five steps of homework is what this replaced"
    assert "git commit" in out
    assert "nightshift fix" in out
    assert "your call" not in out.lower()


def test_next_steps_names_the_flag_that_lets_the_fix_pass_run(tmp_path, capsys):
    """Under the safe default a fix pass cannot run Bash, so it cannot run a gate. Say
    so with the flag attached, and say that the elevation is for that pass only —
    otherwise the honest answer looks like a dead end."""
    repo = _repo(tmp_path)
    plan = init.build_plan(repo, integration="dev-work")
    init.next_steps(plan, integration="dev-work", permission_mode="default")

    out = capsys.readouterr().out
    assert "nightshift fix --permission-mode bypassPermissions" in out
    assert "that pass only" in out
    assert "stays `default`" in out


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


# --- the third step of the earning loop ----------------------------------------
#
# `init` shipped the machinery for logging a correction and `gate_appeals` for saying a
# gate is wrong, and nothing at all for turning a written-down failure into a gate — the
# step the framework exists for. `.ai/recipes/` was never created, so `card_schema`
# validated a `recipe:` field against a directory that could not exist and the field
# could only ever be `none`. Measured on a fresh install, 2026-08-03.


SPINES = ("turn-a-correction-into-a-gate", "verify-before-shipping-a-rule",
          "write-a-test-that-earns-its-place")


def test_init_creates_the_recipes_directory_with_every_shipped_spine(tmp_path):
    """Asserted as a set equality against the template directory rather than as "the
    directory exists", so adding a template spine that `init` is not taught to install
    fails here — the same shape as the private lane, which `init` did not create because
    it iterated a list built for a different purpose."""
    repo = _repo(tmp_path)
    _install(repo)

    shipped = {p.stem for p in (init.TEMPLATES / "ai" / "recipes").glob("*.md")}
    landed = {p.stem for p in (repo / AI_DIR / "recipes").glob("*.md")}
    assert landed == shipped
    assert landed == set(SPINES), "the spines this repo is allowed to ship"


def test_a_fresh_install_can_write_a_card_that_names_a_real_recipe(tmp_path):
    """The acceptance criterion. `card_schema` resolves `recipe:` against
    `.ai/recipes/<name>.md`, so before the spines shipped the only value that could ever
    pass validation was `none` — a schema field nothing could use, which is the shape
    this project has now logged three times (`schema-field-nothing-read`)."""
    from nightshift.gates import card_schema

    repo = _repo(tmp_path)
    _install(repo)
    card = repo / "Board" / "tasks" / "harden-a-rule.md"
    card.write_text(
        "---\nid: harden-a-rule\ntitle: Gate the thing the log caught twice\n"
        "state: tasks\ntier: worker\nworker: none\n"
        "recipe: turn-a-correction-into-a-gate\nunattended: true\ncreated: 2026-08-04\n"
        "---\n\n"
        "## Intent\n\nThe same correction has been logged twice.\n\n"
        "## Approach\n\nFollow the spine; the shape is mechanically detectable.\n\n"
        "## Acceptance\n\n- the gate goes red on the original artefact\n\n"
        "## Open Questions\n\nNone.\n", encoding="utf-8", newline="\n")

    assert card_schema.check(repo) == []


def test_a_card_naming_a_recipe_that_does_not_exist_is_still_refused(tmp_path):
    """The other half: shipping the directory must not turn the check into a rubber
    stamp. A field that accepts anything is the same defect as a field that accepts
    only `none`, one direction over."""
    from nightshift.gates import card_schema

    repo = _repo(tmp_path)
    _install(repo)
    card = repo / "Board" / "tasks" / "invented.md"
    card.write_text(
        "---\nid: invented\ntitle: Names a spine nobody wrote\nstate: tasks\n"
        "tier: worker\nworker: none\nrecipe: ship-a-subsystem\nunattended: true\n"
        "created: 2026-08-04\n---\n\n"
        "## Intent\n\nx\n\n## Approach\n\ny\n\n## Acceptance\n\n- z\n\n"
        "## Open Questions\n\nNone.\n", encoding="utf-8", newline="\n")

    violations = card_schema.check(repo)
    assert any("ship-a-subsystem" in v.rule for v in violations), violations


def test_uninstall_takes_back_the_spines_and_leaves_a_projects_own_recipe(tmp_path):
    """`.ai/recipes/` is where a project writes its own spines, so uninstall must be able
    to tell the two apart. The receipt names what `init` created; the directory is removed
    only if it ends up empty, which is what keeps a recipe the project wrote — and the
    directory holding it — alive."""
    repo = _repo(tmp_path)
    _install(repo)
    mine = repo / AI_DIR / "recipes" / "add-a-perk.md"
    mine.write_text("# Mine\n", encoding="utf-8")

    uninstall.apply(uninstall.plan(repo))

    assert mine.is_file(), "a recipe the project wrote is not ours to delete"
    for spine in SPINES:
        assert not (repo / AI_DIR / "recipes" / f"{spine}.md").exists(), spine


def test_uninstall_removes_the_recipes_directory_when_only_ours_was_in_it(tmp_path):
    """The mirror case, and the one `test_uninstall_removes_exactly_what_init_wrote`
    covers implicitly — named here because an empty directory left behind is what makes
    a second `init` report a state nobody chose."""
    repo = _repo(tmp_path)
    _install(repo)
    uninstall.apply(uninstall.plan(repo))
    assert not (repo / AI_DIR / "recipes").exists()


def test_the_gate_spine_lists_every_value_the_corrections_vocabulary_defines(tmp_path):
    """The spine opens by triaging on the `GATE` field, and that table is a second copy
    of a closed vocabulary. Read back from the shipped JSON rather than retyped, so a
    value added to the vocabulary cannot leave the triage silently incomplete — which is
    the failure the vocabulary file's own comment is about."""
    vocab = json.loads(
        (init.TEMPLATES / "ai" / "gates" / "data" / "corrections_vocab.json")
        .read_text(encoding="utf-8"))
    spine = (init.TEMPLATES / "ai" / "recipes"
             / "turn-a-correction-into-a-gate.md").read_text(encoding="utf-8")

    for value in vocab["gate"]:
        assert f"`{value}`" in spine, f"the GATE value {value!r} is triaged nowhere"


def test_no_shipped_template_carries_a_trace_of_the_project_it_came_from():
    """Every one of these was ported out of the origin project, where each incident has
    a name, a repo and a person attached. The templates carry the origin project's
    *reasoning* and none of its *identity* — a file that names somebody else's package
    reads as one that was copied rather than written.

    Widened from `ai/recipes/` to the whole template tree on 2026-08-04, because the
    narrow version was green while `skills/manage-board/SKILL.md` told every project to
    sign its board answers `### <date> · karel`. That one was not cosmetic: it is the
    *write* side of the convention `digest` reads, so a project following the skill
    literally would sign with a handle its own manifest does not declare, and the
    answered-but-not-moved nudge would never fire.
    """
    offenders = []
    for path in sorted(init.TEMPLATES.rglob("*")):
        if not path.is_file() or path.suffix.lower() in {".png", ".ico"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        for name in ("dungeoneer", "karel"):
            if name in text:
                offenders.append(f"{path.relative_to(init.TEMPLATES).as_posix()} names {name}")
    assert offenders == []


def test_every_doc_that_offers_the_kanban_names_the_plugin_that_provides_it():
    """`type: kanban` is not a core Bases view type — it comes from the **Base Board**
    community plugin, and with only Bases installed Obsidian says `Unknown view type:
    kanban` on a file `init` just told the operator it wrote.

    Reported by the origin project's maintainer, 2026-08-04, on their second install:
    they enabled Bases, opened the board, and got exactly that. The shipped README had
    said "It needs the **Bases** core plugin", which is true and not sufficient — the
    origin checkout has had Base Board enabled since before the extraction, so the doc
    written from it recorded only the half its author had to think about. Fourth
    instance of `origin-repo-had-it-by-hand`, and the second one about this same view.

    Asserted on the error string too, because that is what someone pastes into a search
    box, and a doc that explains the fix without naming the symptom is not found by the
    person who has the symptom.
    """
    docs = {
        "board README": init.TEMPLATES / "board" / "README.md",
        "the install skill": init.TEMPLATES / "skills" / "install-nightshift" / "SKILL.md",
    }
    for label, path in docs.items():
        text = path.read_text(encoding="utf-8")
        assert "Base Board" in text, f"{label} offers the Kanban without naming Base Board"
        assert "Unknown view type: kanban" in text, (
            f"{label} does not name the error the operator actually sees")


def test_the_base_file_is_not_counted_on_to_explain_itself():
    """`board.base` carries the same explanation and is **not** in the list above, because
    its comments do not reach the reader.

    Bases rewrites the file every time the vault opens and its normalisation drops YAML
    comments — measured 2026-08-04 in the origin project, whose live `Board.base` has 0
    comment lines against this template's 12. So a test asserting the template explains
    itself would be green while the delivered artifact explains nothing: the wrong
    artifact verified.

    The header is kept anyway (it is free, and true until first launch), and it now says
    outright that it is expendable and names the durable copy — which is the only claim
    worth pinning here.
    """
    text = (init.TEMPLATES / "board.base").read_text(encoding="utf-8")
    assert "DO NOT SURVIVE" in text, "the header must warn that it is about to vanish"
    assert "README.md" in text, "and name where the durable copy lives"


# --- the Obsidian vault: merged, never overwritten -----------------------------


def _vault(repo: Path, *, community=None, core=None, plugin_installed=False) -> Path:
    vault = repo / ".obsidian"
    vault.mkdir(parents=True, exist_ok=True)
    if community is not None:
        (vault / "community-plugins.json").write_text(
            json.dumps(community, indent=2) + chr(10), encoding="utf-8")
    if core is not None:
        (vault / "core-plugins.json").write_text(
            json.dumps(core, indent=2) + chr(10), encoding="utf-8")
    if plugin_installed:
        (vault / "plugins" / init.BOARD_KANBAN_PLUGIN).mkdir(parents=True)
    return vault


def test_the_kanban_plugin_is_appended_and_the_operators_plugins_survive(tmp_path):
    """The whole ask, in one assertion: *"a user can already have Obsidian over the
    folder, so we can't just overwrite, we need to append during install"* (the origin
    project's maintainer, 2026-08-04)."""
    repo = _repo(tmp_path)
    _vault(repo, community=["dataview", "templater"])

    plan = _install(repo)

    written = json.loads(plan.writes[".obsidian/community-plugins.json"])
    assert written == ["dataview", "templater", init.BOARD_KANBAN_PLUGIN]


def test_enabling_it_twice_is_not_a_write_at_all(tmp_path):
    """A second `init`, or a vault that already had it, must not churn the file."""
    repo = _repo(tmp_path)
    _vault(repo, community=[init.BOARD_KANBAN_PLUGIN])

    plan = _install(repo)

    assert ".obsidian/community-plugins.json" not in plan.writes
    assert ".obsidian/community-plugins.json" in plan.kept


def test_the_core_bases_plugin_is_switched_on_without_touching_the_others(tmp_path):
    """`core-plugins.json` maps EVERY core plugin to a boolean, so a merge that is not
    surgical here turns somebody's file explorer off."""
    repo = _repo(tmp_path)
    _vault(repo, core={"file-explorer": True, "graph": False, "bases": False})

    plan = _install(repo)

    written = json.loads(plan.writes[".obsidian/core-plugins.json"])
    assert written == {"file-explorer": True, "graph": False, "bases": True}


def test_a_core_plugin_map_is_never_invented(tmp_path):
    """An absent `core-plugins.json` means Obsidian has not written one yet. Creating a
    partial one is not an append — it asserts a state for every core plugin nobody asked
    about — so it is refused, out loud."""
    repo = _repo(tmp_path)
    _vault(repo, community=[])

    plan = _install(repo)

    assert ".obsidian/core-plugins.json" not in plan.writes
    assert any("core-plugins.json" in note for note in plan.notes), plan.notes


def test_an_empty_community_list_is_safe_to_create(tmp_path):
    """The asymmetry with `core-plugins.json`: this file is a list of what is ENABLED, so
    an absent one means "none", and writing our single id adds without claiming anything
    about plugins that are not named."""
    repo = _repo(tmp_path)
    _vault(repo)  # a vault, but no plugin files at all

    plan = _install(repo)

    assert json.loads(plan.writes[".obsidian/community-plugins.json"]) == [
        init.BOARD_KANBAN_PLUGIN]


def test_enabled_but_not_installed_is_reported_as_such(tmp_path):
    """Enabling an id whose code is absent is still worth doing — it works the moment
    they fetch it — but calling that "the board is ready" would be the lie."""
    repo = _repo(tmp_path)
    _vault(repo, community=[])

    plan = _install(repo)

    line = next(i for i in plan.info if init.BOARD_KANBAN_PLUGIN in i)
    assert "NOT installed" in line and "Browse" in line


def test_an_installed_plugin_is_enabled_without_the_scolding(tmp_path):
    repo = _repo(tmp_path)
    _vault(repo, community=[], plugin_installed=True)

    plan = _install(repo)

    line = next(i for i in plan.info if init.BOARD_KANBAN_PLUGIN in i)
    assert "NOT installed" not in line


def test_no_vault_means_no_vault_files_and_one_note(tmp_path):
    """Manufacturing `.obsidian/` for someone who may keep their vault elsewhere — or may
    not use Obsidian — is deciding something that is theirs."""
    repo = _repo(tmp_path)

    plan = _install(repo)

    assert not [rel for rel in plan.writes if rel.startswith(".obsidian/")]
    assert any(".obsidian/" in note for note in plan.notes), plan.notes


def test_unreadable_vault_config_is_never_rewritten(tmp_path):
    """Same rule as `settings.json`: overwriting a file we cannot parse would take the
    operator's other plugins with it."""
    repo = _repo(tmp_path)
    vault = _vault(repo)
    (vault / "community-plugins.json").write_text("{not json", encoding="utf-8")

    plan = _install(repo)

    assert ".obsidian/community-plugins.json" not in plan.writes
    assert any("unreadable" in note for note in plan.notes), plan.notes


def test_uninstall_never_touches_the_vault(tmp_path):
    """By the time anyone uninstalls, "did nightshift enable Bases or did they" is
    unanswerable — and switching off a plugin somebody has used for months to tidy up
    after ourselves is the worse error. So the receipt does not claim these."""
    repo = _repo(tmp_path)
    _vault(repo, community=["dataview"], core={"bases": False})

    plan = _install(repo)
    receipt = json.loads(init.receipt_text(plan))

    assert not [rel for rel in receipt["created"] if rel.startswith(".obsidian/")]


# --- taking a block back out ---------------------------------------------------


def test_init_creates_every_lane_including_the_private_one(tmp_path):
    """`ideas/` is deliberately absent from `board.LANES` — `ideas_fence` derives
    "private" from a board subdirectory not being in that tuple, which writes the rule
    down once instead of twice. `init` made lanes by iterating `LANES`, so the one lane
    belonging to the maintainer was the one lane nobody created for them, while
    `Board/README.md`, `CLAUDE.md` and `reconcile` all named it as the first step of the
    flow. Reported by the maintainer, 2026-08-03: *"It does not install `ideas` folder,
    is that true?"* It was.

    Asserted as a set equality rather than "ideas exists", so the next lane added to the
    board fails here too if the installer is not taught about it.
    """
    repo = _repo(tmp_path)
    _install(repo)

    made = {p.name for p in (repo / "Board").iterdir() if p.is_dir()}
    assert made == set(board.LANES) | {board.PRIVATE_LANE}


def test_the_private_lane_is_still_absent_from_lanes(tmp_path):
    """The fix must not be "add ideas to LANES". That tuple is what the fence reads to
    decide what is private, and what `reconcile` reads to decide where a card may be
    moved — putting `ideas` in it would open the lane rather than create it."""
    assert board.PRIVATE_LANE not in board.LANES


def test_the_board_readme_names_the_lanes_that_exist(tmp_path):
    """Doc and installer, checked against each other: a lane documented as part of the
    flow and never created is exactly the failure above."""
    repo = _repo(tmp_path)
    _install(repo)
    readme = (repo / "Board" / "README.md").read_text(encoding="utf-8")

    for lane in (*board.LANES, board.PRIVATE_LANE):
        assert f"{lane}/" in readme, f"{lane}/ exists on disk but the README never says so"


def test_init_writes_an_obsidian_view_of_the_board(tmp_path):
    """The lanes are directories, which is the whole design — but a directory tree is
    not a board. The origin project has had an Obsidian Bases view since long before the
    extraction, so no run there could notice `init` never produced one. Reported by the
    maintainer, 2026-08-03, who opened a freshly installed repo in Obsidian and found no
    kanban."""
    repo = _repo(tmp_path)
    _install(repo)

    view = repo / "Board.base"
    assert view.is_file()
    text = view.read_text(encoding="utf-8")
    assert "{{" not in text, "an unrendered token"
    assert "type: kanban" in text


def test_the_board_view_columns_are_generated_from_the_lane_list(tmp_path):
    """A hand-written column list is a second copy of the lane set that nothing checks,
    and a view silently missing a lane is worse than no view: a card in the missing
    column is invisible rather than obviously absent."""
    repo = _repo(tmp_path)
    _install(repo)

    lines = (repo / "Board.base").read_text(encoding="utf-8").splitlines()
    start = lines.index("    boardColumns:") + 1
    columns = []
    for line in lines[start:]:
        if not line.startswith("      - "):
            break
        columns.append(line.removeprefix("      - "))
    assert tuple(columns) == board.LANES


def test_the_board_view_hides_the_private_lane_unless_a_note_is_flagged(tmp_path):
    """Filtered out, with one exception: a note carrying a `state:` is asking to leave
    the lane, and seeing that on the board is the whole point of flagging it."""
    repo = _repo(tmp_path)
    _install(repo)

    text = (repo / "Board.base").read_text(encoding="utf-8")
    assert f'!file.inFolder("Board/{board.PRIVATE_LANE}")' in text
    assert "!state.isEmpty()" in text


def test_the_board_view_follows_a_relocated_board(tmp_path):
    """`[board].root` is configurable, so every path inside the view is rendered rather
    than assumed — including the private-lane exclusion, which is the one that fails
    silently if it goes stale."""
    values = init.tokens(tmp_path, {"board": {"root": "kanban"}})
    rendered = init.render(
        (init.TEMPLATES / "board.base").read_text(encoding="utf-8"), values)

    assert 'file.inFolder("kanban")' in rendered
    assert f'!file.inFolder("kanban/{board.PRIVATE_LANE}")' in rendered

    # Scoped to the YAML body, not the whole file: the header comment names the
    # **Base Board** plugin, and that `Board` is a product name rather than a path.
    # Asserting over the comments too would make the file's prose unwritable — and
    # this test is about *paths* that were hardcoded to the default root.
    config = "\n".join(line for line in rendered.splitlines()
                       if not line.lstrip().startswith("#"))
    assert "Board" not in config, "no path hardcoded to the default"


def test_an_existing_board_view_is_kept_and_reported(tmp_path):
    """Not a merge: a `.base` is YAML config, and appending a marked block to it would
    corrupt the file. So it is kept — and said out loud, because "kept, untouched" with
    no note is how a stale column list survives an install."""
    repo = _repo(tmp_path)
    (repo / "Board.base").write_text("views: []\n", encoding="utf-8")

    plan = _install(repo)

    assert "Board.base" in plan.kept
    assert (repo / "Board.base").read_text(encoding="utf-8") == "views: []\n"
    assert any("boardColumns" in note for note in plan.notes)


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
