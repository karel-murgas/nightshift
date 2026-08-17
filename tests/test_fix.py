"""`nightshift fix` — the loop that replaced the closing checklist.

The origin project's maintainer, 2026-08-03: *"It's not meant to be used by a human, but
used by AI. I would like the init to end with LLM running the diagnosis and fixing bugs
that it found. Not human, who checks gates manually now."*

Two things are worth testing here and they are not the dispatch. The **stopping
conditions**, because an unbounded loop pointed at a repo with `bypassPermissions` is the
most expensive bug this framework could ship — so `loop()` takes its dispatcher as an
argument and every exit path is exercised without a CLI. And the **prompt**, because its
prohibitions are the whole difference between fixing a defect and deleting the check that
found it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightshift import board, fix, init, preflight
from nightshift.gates import card_schema

import _fixtures


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, encoding="utf-8", errors="replace")


def _build(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text("def run(n):\n    return n * 2\n",
                                          encoding="utf-8")
    (root / "tests" / "test_core.py").write_text(
        "from pkg.core import run\n\n\ndef test_run():\n    assert run(2) == 4\n",
        encoding="utf-8")
    _fixtures.git_init(root, branch="main", autocrlf="false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    _git(root, "checkout", "-q", "-b", "dev-work")
    init.apply(init.build_plan(root, integration="dev-work"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An installed repo: a package, a test, a board, a manifest."""
    return _fixtures.repo_copy("installed-dev-work", tmp_path / "proj", _build)


def _failing(*names: str) -> fix.Diagnosis:
    return fix.Diagnosis(failed=[preflight.Check(n, False, f"{n} is unhappy")
                                 for n in names], dirty="")


# --- the prompt ----------------------------------------------------------------


def test_the_prompt_forbids_every_cheap_way_to_turn_red_green(tmp_path):
    """The substance of the module. An agent told to make checks pass will make them
    pass, and weakening the check is always the cheapest route."""
    text = fix.prompt(tmp_path, _failing("gates", "pytest"))

    for forbidden in ("Do not edit, delete, weaken, narrow or skip a gate",
                      "budget_bytes", "corrections_vocab.json", "--no-corrections",
                      "skip`/`xfail", "Do not commit, push, merge"):
        assert forbidden in text, forbidden


def test_the_prompt_carries_the_command_that_reproduces_each_failure(tmp_path):
    text = fix.prompt(tmp_path, _failing("gates", "audit-matrix"))
    assert "python -m nightshift.gates.run" in text
    assert "python -m nightshift.audit --check" in text


def test_the_prompt_asks_for_an_escalation_section_by_name(tmp_path):
    """`decisions()` parses that heading, so the prompt has to name it or the parse is
    looking for something nobody was asked to write."""
    text = fix.prompt(tmp_path, _failing("gates"))
    assert fix.DECISION_HEADING in text
    assert fix.REPORT_HEADING in text


def test_the_prompt_keeps_one_violation_per_line(tmp_path):
    """A gate reports one violation per line with a path on it. Flattening those into a
    paragraph destroys the only part an agent can act on directly."""
    diagnosis = fix.Diagnosis(failed=[preflight.Check(
        "gates", False, "a.py:1 — first thing\nb.py:2 — second thing")])
    text = fix.prompt(tmp_path, diagnosis)
    assert "a.py:1 — first thing" in text.splitlines()
    assert "b.py:2 — second thing" in text.splitlines()


# --- refusing to dispatch ------------------------------------------------------


@pytest.mark.parametrize("mode", ["default", "acceptEdits"])
def test_a_mode_that_cannot_run_bash_is_refused_before_dispatch(repo, mode, monkeypatch):
    """A session that cannot run Bash cannot run a gate, so it cannot fix one. Checked
    before spending a round and a budget on finding that out."""
    monkeypatch.setattr(fix.runner, "claude_binary", lambda: "/usr/bin/claude")
    reason = fix.can_dispatch(repo, mode)
    assert mode in reason and "Bash" in reason


def test_a_missing_cli_is_refused_with_where_it_looked(repo, monkeypatch):
    monkeypatch.setattr(fix.runner, "claude_binary", lambda: None)
    reason = fix.can_dispatch(repo, "bypassPermissions")
    assert "CLAUDE_BIN" in reason


def test_bypass_with_a_cli_present_is_allowed(repo, monkeypatch):
    monkeypatch.setattr(fix.runner, "claude_binary", lambda: "/usr/bin/claude")
    assert fix.can_dispatch(repo, "bypassPermissions") == ""


def test_main_refuses_rather_than_dispatching_and_exits_two(repo, monkeypatch, capsys):
    monkeypatch.setattr(fix.runner, "claude_binary", lambda: None)
    monkeypatch.setattr(fix, "loop", lambda *a, **k: pytest.fail("must not dispatch"))
    code = fix.main(["--root", str(repo), "--skip-tests",
                     "--permission-mode", "bypassPermissions"])
    assert code == 2
    assert "cannot dispatch" in capsys.readouterr().out


# --- the stopping conditions ---------------------------------------------------


def test_a_green_repo_dispatches_nothing(repo, monkeypatch):
    monkeypatch.setattr(fix, "diagnose", lambda *a, **k: fix.Diagnosis())

    def never(*a, **k):
        pytest.fail("dispatched against a green repo")

    assert fix.loop(repo, permission_mode="bypassPermissions", dispatcher=never) == 0


def test_it_stops_when_a_round_changes_nothing(repo, monkeypatch):
    """The condition that makes this safe to leave running: no check resolved *and* not
    one byte changed on disk means the next round is this round."""
    monkeypatch.setattr(fix, "diagnose",
                        lambda *a, **k: fix.Diagnosis(
                            failed=[preflight.Check("gates", False, "same")], dirty="X"))
    rounds = []

    def lazy(root, text, round_no, **k):
        rounds.append(round_no)
        return 0, "did nothing"

    assert fix.loop(repo, permission_mode="bypassPermissions", dispatcher=lazy) == 1
    assert rounds == [1], "one wasted round, not three"


def test_it_returns_zero_the_round_the_checks_go_green(repo, monkeypatch):
    states = [_failing("gates"), fix.Diagnosis()]
    monkeypatch.setattr(fix, "diagnose", lambda *a, **k: states.pop(0))
    calls = []
    assert fix.loop(repo, permission_mode="bypassPermissions",
                    dispatcher=lambda *a, **k: (calls.append(1), (0, "fixed"))[1]) == 0
    assert len(calls) == 1


def test_a_nonzero_agent_exit_stops_the_loop(repo, monkeypatch):
    """Dispatching again on top of a failed run compounds whatever went wrong."""
    monkeypatch.setattr(fix, "diagnose", lambda *a, **k: _failing("gates"))
    calls = []
    code = fix.loop(repo, permission_mode="bypassPermissions",
                    dispatcher=lambda *a, **k: (calls.append(1), (1, ""))[1])
    assert code == 1 and len(calls) == 1


def test_the_round_cap_is_honoured(repo, monkeypatch):
    """Progress every round, never green: the cap is the only thing that ends it."""
    seen = {"n": 0}

    def churn(*a, **k):
        seen["n"] += 1
        return fix.Diagnosis(failed=[preflight.Check("gates", False, "x")],
                             dirty=f"change-{seen['n']}")

    monkeypatch.setattr(fix, "diagnose", churn)
    calls = []
    code = fix.loop(repo, permission_mode="bypassPermissions", max_rounds=2,
                    dispatcher=lambda *a, **k: (calls.append(1), (0, ""))[1])
    assert code == 1 and len(calls) == 2


def test_the_loop_never_commits(repo, monkeypatch):
    """The diff is left dirty on purpose — the operator reading it is where this stays
    honest. Asserted against the real repo rather than the prompt text."""
    before = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout
    monkeypatch.setattr(fix, "diagnose", lambda *a, **k: _failing("gates"))
    fix.loop(repo, permission_mode="bypassPermissions",
             dispatcher=lambda *a, **k: (0, "touched nothing"))
    after = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout
    assert before == after


# --- escalation becomes a card -------------------------------------------------


def test_decisions_are_parsed_out_of_the_named_section():
    final = ("## Report\n\nFixed the gate.\n\n"
             "## Needs decision\n\n"
             "- Whether `dev` is the integration branch or the stable one\n"
             "- The layering rule between ui and core,\n  which I could not infer\n")
    assert fix.decisions(final) == [
        "Whether `dev` is the integration branch or the stable one",
        "The layering rule between ui and core, which I could not infer",
    ]


def test_no_escalation_section_means_nothing_to_escalate():
    assert fix.decisions("## Report\n\nAll fixed, nothing ambiguous.\n") == []


def test_a_following_section_ends_the_list():
    final = "## Needs decision\n\n- one thing\n\n## Notes\n\n- not a decision\n"
    assert fix.decisions(final) == ["one thing"]


def test_an_escalated_item_becomes_a_card_the_schema_accepts(repo):
    """A fix pass that files an invalid card has created the class of violation it was
    dispatched to clear, so this runs the real gate over what it wrote."""
    written = fix.file_cards(repo, ["Whether dev is integration or stable"], round_no=1)

    assert len(written) == 1
    card = written[0]
    assert card.parent == board.board_dir(repo) / "needs-decision"
    assert card_schema.check(repo) == []

    text = card.read_text(encoding="utf-8")
    assert "## Approach" in text, "the section the maintainer reads to decide"
    assert "Not chosen" in text, "and it must not pretend to have chosen"


def test_filing_is_idempotent_across_rounds(repo):
    fix.file_cards(repo, ["one ambiguous thing"], round_no=1)
    again = fix.file_cards(repo, ["one ambiguous thing"], round_no=2)
    assert again == [], "a second round must not duplicate the card"


def test_filing_no_ops_without_a_board(tmp_path):
    """`init` on a repo can be board-less; a fix pass there still has to work."""
    assert fix.file_cards(tmp_path, ["something"], round_no=1) == []


def test_the_loop_files_what_the_agent_escalated(repo, monkeypatch):
    monkeypatch.setattr(fix, "diagnose", lambda *a, **k: _failing("gates"))
    final = "## Report\n\ndone what I could\n\n## Needs decision\n\n- the branch role\n"
    fix.loop(repo, permission_mode="bypassPermissions",
             dispatcher=lambda *a, **k: (0, final))

    lane = board.board_dir(repo) / "needs-decision"
    assert [p.name for p in lane.glob("*.md")] == ["fix-the-branch-role.md"]


# --- bootstrap: the one file that breaks the chicken-and-egg --------------------


def test_bootstrap_writes_the_install_skill_and_the_launchers_and_nothing_else(repo):
    """Two things break the chicken-and-egg, not one. The skill is how the install is
    driven; the launchers are how you reach the panel that offers to drive it — and the
    panel renders before there is an install, which is what makes that offer possible.

    Still an exact list. `bootstrap` is the one command that runs in a repo which has
    consented to nothing yet, and anything extra it drops is something nobody asked for.
    """
    plan = init.bootstrap_plan(repo)
    assert sorted(plan.staged) == sorted([init.INSTALL_SKILL, *init.LAUNCHERS])


def test_bootstrap_is_recorded_so_uninstall_takes_it_back(tmp_path):
    root = tmp_path / "bare"
    (root / ".git").mkdir(parents=True)
    init.apply(init.bootstrap_plan(root))

    assert (root / init.INSTALL_SKILL).is_file()
    receipt = json.loads((root / init.RECEIPT).read_text(encoding="utf-8"))
    assert sorted(receipt["created"]) == sorted([init.INSTALL_SKILL, *init.LAUNCHERS])


def test_bootstrap_does_not_claim_the_repo_is_installed(tmp_path):
    """`bootstrap` writes a receipt, so the receipt cannot be the test for `is this
    repo installed` — the panel keyed on it and reported a finished install in a repo
    with no manifest, then failed rendering on the manifest that was never written."""
    from nightshift import panel

    root = tmp_path / "bare"
    (root / ".git").mkdir(parents=True)
    init.apply(init.bootstrap_plan(root))

    assert (root / init.RECEIPT).is_file()
    assert not panel.installed(root)


def test_the_install_skill_drives_the_whole_install(repo):
    """It is the entry point now, so it has to carry the steps a person used to read off
    the closing screen — including the fix pass that replaced them."""
    text = (repo / init.INSTALL_SKILL).read_text(encoding="utf-8")
    assert "nightshift init --integration" in text
    assert "nightshift fix" in text
    assert "integration branch" in text.lower()
    assert "bypassPermissions" in text
    assert "Board/inbox/" in text, "an unscopeable finding becomes a note, not a card"
    assert "Do not write gates" in text, "a gate is earned, never imported"


def test_the_install_skill_sweeps_existing_code_into_cards(repo):
    """The shipped gates are tuned to stop new mess, not to report the accumulated kind:
    `dead_code` runs at confidence 80, where vulture reports unused imports but not
    unused functions. A repo that existed before the install has never had that list
    shown to it. The maintainer's call, 2026-08-03: *"New install should create the
    cards, so owner can check what is happening."*"""
    text = (repo / init.INSTALL_SKILL).read_text(encoding="utf-8")

    assert "--min-confidence 60" in text, "lower than the gate's, on purpose"
    assert "Do not fix them now" in text
    assert "One card per cluster" in text
    assert "unused imports" in text, "the one thing safe to fix in place"
