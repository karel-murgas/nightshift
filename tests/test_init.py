"""Tests for `nightshift init` — 07_portability.md §8 step 5.

The acceptance criterion these serve is D6's: **an empty repo, initialised, must
pass the gates and dispatch a card.** That is asserted end to end below, against a
real git repo in `tmp_path`, because the failures this step is most likely to have
are the ones a mocked filesystem cannot produce — a template shipping a backticked
path that dangles in the target tree, a token left unrendered, a file overwritten
that should have been kept.

The safety properties are asserted separately and matter more than the happy path:
nothing is overwritten, `settings.json` is merged rather than replaced, and
`--yes` does not cover the one field that is never guessed.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from nightshift import discover, init


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)],
                   check=True, capture_output=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Alex Rivera")
    (tmp_path / "myapp").mkdir()
    # `newline=""` deliberately: on Windows `write_text` emits CRLF, and a fixture
    # committing a CRLF file before `.gitattributes` exists reproduces the exact
    # per-machine problem `init` reports and cannot fix — which would make
    # `test_an_initialised_empty_repo_passes_the_gates` fail for a reason that has
    # nothing to do with init.
    (tmp_path / "myapp" / "__init__.py").write_text("X = 1\n", encoding="utf-8", newline="")
    (tmp_path / "tests").mkdir()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def _init(repo: Path, integration: str | None = "main") -> init.Plan:
    plan = init.build_plan(repo, integration=integration)
    init.apply(plan)
    return plan


# --- D6: the empty repo works ------------------------------------------------


def test_an_initialised_empty_repo_passes_the_gates(repo):
    """The bar. Everything else in this file is about how it gets there."""
    from nightshift.gates import run as gates

    _init(repo)
    violations = [v for gate in gates.discover(repo).values() for v in gate.check(repo)]
    assert not violations, "\n".join(str(v) for v in violations)


def test_an_initialised_repo_can_hold_a_dispatchable_card(repo):
    """One step past the gates: `card_schema` accepts a card, which means the
    charter it names exists and the lane it sits in does too. Those are the two
    things a fresh install has no way to provide by itself."""
    from nightshift import board
    from nightshift.gates import card_schema

    _init(repo)
    card = repo / "Board" / "tasks" / "hello.md"
    card.write_text(
        "---\nid: hello\ntitle: Add a docstring\nrecipe: none\nstate: tasks\n"
        "worker: code-thread\ntier: worker\nunattended: true\ncreated: 2026-08-02\n"
        "verify: review\n---\n\n"
        "## Intent\n\nThe package has no docstring.\n\n"
        "## Goal\n\nAdd one.\n\n"
        "## Approach\n\nOne line at the top of the module.\n\n"
        "## Acceptance\n\n- the module has a docstring\n\n"
        "## Open Questions\n\nNone.\n", encoding="utf-8", newline="\n")

    assert card_schema.check(repo) == []
    assert board.board_dir(repo).is_dir()


def test_the_tier_binding_resolves_after_init(repo):
    """The coupling that made a fresh repo refuse to start: the runner's first
    check needs a document carrying the binding block, and no `pip install`
    provided one (`deferral-note-nobody-collected`)."""
    from nightshift import tiers

    _init(repo)
    assert tiers.resolve(repo, "worker")
    assert tiers.resolve(repo, "lead")


def test_every_lane_the_board_declares_gets_a_directory(repo):
    from nightshift.board import LANES

    _init(repo)
    for lane in LANES:
        assert (repo / "Board" / lane).is_dir(), lane


# --- rendering ---------------------------------------------------------------


def test_no_token_survives_rendering(repo):
    """A charter shipped with a literal `{{package}}` in it reads as a broken
    install, and it would be found by whoever reads the file rather than by
    anything that runs."""
    _init(repo)
    leftovers = [
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and path.suffix in (".md", ".json", ".toml")
        and "{{" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert leftovers == []


def test_the_maintainer_token_comes_from_git(repo):
    _init(repo)
    charter = (repo / ".claude" / "agents" / "triage.md").read_text(encoding="utf-8")
    assert "Alex Rivera" in charter


def test_dated_quotes_keep_their_original_attribution(repo):
    """The templates carry verbatim quotes from the origin project, and
    re-signing one with whoever installs this would be a lie inside a document
    whose subject is honest records."""
    _init(repo)
    skill = (repo / ".claude" / "skills" / "log-a-correction" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "the origin project's maintainer, 2026-07-23" in skill
    assert not re.search(r"Alex Rivera,\s*20\d\d-\d\d-\d\d", skill)


# --- nothing is overwritten --------------------------------------------------


def test_a_second_run_writes_nothing_and_keeps_everything(repo):
    """`init` on a configured repo must be safe, because that is also how a
    half-configured repo gets the pieces it is missing."""
    first = _init(repo)
    assert first.writes

    second = init.build_plan(repo, integration="main")
    assert second.writes == {}
    assert set(second.kept) >= set(first.writes)


def test_an_existing_file_is_kept_verbatim(repo):
    """A memory stub the project already wrote is left exactly alone — `init` has an
    opinion about the file existing and none about its contents."""
    memory = repo / ".claude" / "memory"
    memory.mkdir(parents=True)
    (memory / "arch.md").write_text("# my own module map\n", encoding="utf-8")
    plan = _init(repo)
    assert ".claude/memory/arch.md" in plan.kept
    assert (memory / "arch.md").read_text(encoding="utf-8") == "# my own module map\n"


def test_an_existing_claude_md_keeps_every_byte_it_had(repo):
    """The four files where the *content* is the requirement are appended to rather
    than skipped (see `build_plan.merge`), so "verbatim" becomes "prefix-preserved":
    their bytes are still there, unchanged, with a marked block after them."""
    (repo / "CLAUDE.md").write_text("# my own rules\n", encoding="utf-8")
    plan = _init(repo)

    assert "CLAUDE.md" not in plan.writes, "never overwritten — that rule is unchanged"
    assert "CLAUDE.md" in plan.appends
    text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith("# my own rules\n")
    assert init.strip_block(text) == "# my own rules\n", "and the block lifts back out"


def test_an_existing_manifest_is_never_rewritten(repo):
    ai = repo / ".ai"
    ai.mkdir()
    (ai / "manifest.toml").write_text('[project]\nname = "hand-written"\n', encoding="utf-8")
    _init(repo)
    assert "hand-written" in (ai / "manifest.toml").read_text(encoding="utf-8")


# --- settings.json is a merge ------------------------------------------------


def test_hooks_are_merged_into_an_existing_settings_file(repo):
    """A project's `permissions` block is its own and must survive. Overwriting
    `settings.json` to add hooks would take it with them."""
    settings = repo / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git status)"]}}), encoding="utf-8")

    _init(repo)
    written = json.loads((settings / "settings.json").read_text(encoding="utf-8"))
    assert written["permissions"]["allow"] == ["Bash(git status)"]
    assert written["hooks"]["PreToolUse"], "hooks were not added"


def test_merging_twice_adds_nothing_the_second_time(repo):
    fragment = json.loads(
        (init.TEMPLATES / "settings.hooks.json").read_text(encoding="utf-8"))
    fragment.pop("_comment", None)

    once, added = init.merge_hooks({}, fragment)
    assert added > 0
    twice, again = init.merge_hooks(once, fragment)
    assert again == 0
    assert twice == once


def test_a_project_may_retune_a_timeout_without_the_hook_being_re_added(repo):
    """Matching is on the command, not the whole entry: `timeout` and
    `statusMessage` are a project's to tune, and re-adding a duplicate every run
    would punish having tuned them."""
    fragment = json.loads(
        (init.TEMPLATES / "settings.hooks.json").read_text(encoding="utf-8"))
    fragment.pop("_comment", None)
    once, _ = init.merge_hooks({}, fragment)
    for group in once["hooks"]["PreToolUse"]:
        for hook in group["hooks"]:
            hook["timeout"] = 99

    _, again = init.merge_hooks(once, fragment)
    assert again == 0


def test_an_unreadable_settings_file_is_reported_not_replaced(repo):
    settings = repo / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text("{ not json", encoding="utf-8")

    plan = init.build_plan(repo, integration="main")
    assert ".claude/settings.json" not in plan.writes
    assert any("unreadable" in note for note in plan.notes)
    init.apply(plan)
    assert (settings / "settings.json").read_text(encoding="utf-8") == "{ not json"


# --- the interview -----------------------------------------------------------


def test_yes_does_not_cover_the_integration_branch(repo, monkeypatch, capsys):
    """`--yes` is for the twenty high-confidence fields. A caller that genuinely
    wants no interaction passes `--integration`, and has therefore stated the
    answer rather than had it guessed."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    proposal = discover.integration_branch(repo, "main")
    answer = init.confirm_integration(repo, proposal, assume_yes=True, given=None)
    assert answer is None
    assert "--integration" in capsys.readouterr().out


def test_a_stated_integration_branch_is_taken_without_asking(repo):
    proposal = discover.integration_branch(repo, "main")
    assert init.confirm_integration(
        repo, proposal, assume_yes=False, given="development") == "development"


def test_without_an_integration_branch_the_manifest_omits_the_key(repo):
    """Not written empty: `[branches].integration` has no default on purpose, and
    an empty value would read as a decision rather than an absence."""
    plan = init.build_plan(repo, integration=None)
    assert "integration" not in plan.tables.get("branches", {})
    section = plan.writes[".ai/manifest.toml"].split("[branches]")[1].split("\n[")[0]
    assert "integration =" not in section


def test_the_manifest_explains_why_integration_has_no_default(repo):
    plan = init.build_plan(repo, integration="main")
    text = plan.writes[".ai/manifest.toml"]
    assert "No default, ever" in text, (
        "a generated config with no explanation is how the next person deletes the "
        "line to make an error go away")


# --- what init decides, and what it only reports ------------------------------


def test_the_undecidable_things_are_asked_rather_than_noted(repo):
    """**This test used to assert the opposite**, and the opposite was the defect.

    `permission_mode`, `capabilities` and `budget_bytes` were reported as notes
    under a heading reading `your call:`, on the grounds that a field which bounds
    behaviour must be declared rather than probed. That rule is right; the
    conclusion was wrong. The first person to install this from scratch got as far
    as the notes and stopped — *"I have no idea how to make these calls"* — because
    a note names a decision without offering a way to take it.

    Asking satisfies the rule better than a note does: the consequence is on screen
    at the moment of choosing, and `test_install_flow` locks that the *safe* answer
    is what Enter selects. So the three are now interview questions whose answers
    land in files, and `plan.notes` is reserved for the one thing writing a file
    cannot fix — a CRLF working tree, which needs a command run on this box.
    """
    plan = init.build_plan(repo, integration="main")
    notes = " ".join(plan.notes)
    for asked in ("hosts.capabilities", "hosts.permission_mode", "memory.budget_bytes"):
        assert asked not in notes, f"{asked} is an interview question now, not a note"

    hosts = json.loads(plan.writes[".ai/hosts.json"])
    entry = next(v for k, v in hosts.items() if not k.startswith("_"))
    assert "permission_mode" in entry and "capabilities" in entry, (
        "whatever was answered has to land in the file, or the question was theatre")


def test_the_seed_vocabulary_is_empty(repo):
    """Copying the origin project's twelve classes produces one violation per
    unused class — 18 of them, measured. The seed has to be empty, and a class
    arrives with the first entry that needs it."""
    _init(repo)
    vocab = json.loads(
        (repo / ".ai" / "gates" / "data" / "corrections_vocab.json").read_text(
            encoding="utf-8"))
    assert vocab["class"] == {}
    assert vocab["channel"] == {}
    assert vocab["gate"], "the framework's own axes are not usage-checked and do ship"


def test_nothing_is_written_before_every_decision_is_made(repo):
    """`build_plan` is pure. A run interrupted mid-interview must leave nothing:
    a half-initialised repo is worse than an uninitialised one, because the gates
    half-run and the failure looks like a framework bug."""
    init.build_plan(repo, integration="main")
    assert not (repo / ".ai").exists()
    assert not (repo / "Board").exists()
    assert not (repo / ".claude").exists()


def test_written_files_are_lf(repo):
    """The framework's own `.gitattributes` rule, applied to the files it writes."""
    _init(repo)
    for rel in (".gitattributes", "CLAUDE.md", ".ai/manifest.toml",
                ".claude/agents/code-thread.md"):
        assert b"\r\n" not in (repo / rel).read_bytes(), rel
