"""Tests for `runner.unadopted_artefacts` — a card whose candidates nobody
installed owes a **pick**, and `needs-decision/` is the lane that says so.

`verify:` decides where finished work lands (`test_verify_route.py`), and it can
only ever say one of two things: this has a surface to exercise, or it has none.
Neither is true of an attempt whose whole output is four PNGs in a gitignored
scratch directory. `stun-grenade-visuals` (2026-09-06) declared `verify: play`
because eventually there *will* be something to play; it generated four blue
grenade icons into `assets/.tmp/`, installed none of them, passed its checker,
passed its diff reviewer — whose own notes reasoned that "`verify: play` means he
sees it at testing/" — and arrived in `testing/` asking Karel to go and look at a
picture that was not in the game. His answer, 2026-09-08:

    We have a problem. Art card end up being "generated, you have to pick" in
    "testing" with "no testing scenario". [...] It should ended in "needs
    decision" and give me a way to show and pick these results. [...] It should
    end up in testing only after it is wired up in game.

So the routing is derived from what the attempt actually did rather than from
what its author expected: artefacts harvested, and nothing written to the
directory they get promoted into. That pair is also what keeps the *audio* shape
landing in `testing/` where it belongs — a card that generates takes and commits
a wired fallback among them has installed something, and there is a program to
run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from nightshift import board, decide, run_record, runner

import _runner_helpers as helpers


def _repo(tmp_path: Path) -> Path:
    return helpers._repo(tmp_path)


# --- the predicate -----------------------------------------------------------

def test_candidates_with_nothing_installed_are_unadopted(tmp_path):
    """The reported case: four icons harvested, and the diff touched only tooling
    outside the asset tree."""
    root = _repo(tmp_path)
    assert runner.unadopted_artefacts(
        root, 4, ["scripts/object_recolor.py", ".ai/memory-fragments/x.md"]) == 4


def test_installing_one_of_them_is_not_a_pick_owed(tmp_path):
    """The audio shape — takes harvested *and* one committed into the asset tree,
    so there is something in the program to exercise and `verify:` is right."""
    root = _repo(tmp_path)
    assert runner.unadopted_artefacts(
        root, 14, ["dungeoneer/assets/audio/sfx/recharge.wav", "pkg/sound.py"]) == 0


def test_no_artefacts_is_never_a_pick(tmp_path):
    """Every ordinary code card. The deliverable is the diff."""
    root = _repo(tmp_path)
    assert runner.unadopted_artefacts(root, 0, ["pkg/thing.py"]) == 0


def test_writing_inside_the_scratch_dir_is_not_adopting(tmp_path):
    """`assets/.tmp/` is where the candidates already are; a file there is not an
    installed asset. Gitignored in practice, so this is the rule reading as what
    it means rather than leaning on that."""
    root = _repo(tmp_path)
    assert runner.unadopted_artefacts(
        root, 2, ["dungeoneer/assets/.tmp/cand_a.png"]) == 2


def test_a_project_with_no_harvest_dir_keeps_the_old_routing(tmp_path):
    """Nothing to reason about, and the safe direction is the pre-existing one."""
    root = _repo(tmp_path)
    (root / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "p"\nsource_dirs = ["pkg"]\n\n'
        '[branches]\nintegration = "development_team"\nstable = "main"\n',
        encoding="utf-8")
    assert runner.unadopted_artefacts(root, 4, ["a.txt"]) == 0


def test_windows_path_separators_are_read_the_same(tmp_path):
    """`gitpaths.changed` is the caller, but a backslash reaching here must not
    read as "nothing was installed" and park a card that is genuinely finished."""
    root = _repo(tmp_path)
    changed = ["dungeoneer" + chr(92) + "assets" + chr(92) + "items"
               + chr(92) + "grenade_stun_grenade.png"]
    assert runner.unadopted_artefacts(root, 3, changed) == 0


# --- where it lands ----------------------------------------------------------

def _settle(tmp_path, monkeypatch, result: runner.Dispatch, *, commits: bool = True,
            verify: str = "play") -> tuple[Path, dict]:
    root = _repo(tmp_path)
    helpers._card(root, "tasks", "icon", worker="art", checker="art-reviewer",
                  verify=verify)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "board"], cwd=root, check=True,
                   capture_output=True)
    calls: dict = {"merged": 0}

    def fake_merge(*a, **k):
        calls["merged"] += 1
        return True, ""

    monkeypatch.setattr(runner, "rebase_and_merge", fake_merge)
    monkeypatch.setattr(runner, "branch_has_commits", lambda *a, **k: commits)
    monkeypatch.setattr(runner, "default_base", lambda root_: "development_team")
    monkeypatch.setattr(runner, "read_telemetry", lambda *a, **k: None)
    calls["landed"] = runner.settle(root, "icon", result)
    return root, calls


def test_a_pick_lands_in_needs_decision_not_testing(tmp_path, monkeypatch):
    """The whole complaint, in one assertion: the card says `verify: play`, and it
    does **not** go to `testing/`."""
    root, calls = _settle(tmp_path, monkeypatch,
                          runner.Dispatch("pick", "ok", unadopted=4))
    assert (root / "Board" / "needs-decision" / "icon.md").is_file()
    assert not (root / "Board" / "testing" / "icon.md").exists()
    assert "needs-decision/" in calls["landed"]


def test_the_pick_question_says_what_is_owed_and_how_it_resumes(tmp_path, monkeypatch):
    root, _ = _settle(tmp_path, monkeypatch,
                      runner.Dispatch("pick", "ok", unadopted=4))
    card = board.Card.load(root / "Board" / "needs-decision" / "icon.md",
                           "needs-decision")
    question = board.section(card.text, "Question")
    assert "4 candidate(s)" in question
    assert ".ai/runs/icon/attempt-" in question
    assert card.fields["after_answer"] == board.AFTER_ANSWER_TASKS
    # The options are written in the shape the answer form and the digest both
    # read, so answering is two clicks rather than an editor.
    options = [o.text for sub in decide.parse(card.text) for o in sub.options]
    assert any("Adopt one" in o for o in options), options
    assert any("re-roll" in o for o in options), options


def test_the_diff_still_merges_before_the_card_parks(tmp_path, monkeypatch):
    """A park that left the commits on the branch would have them shunted to a
    rescue ref by the next cold start (`prepare_worktree`), and the second pass
    would then have to write the same tooling again."""
    _, calls = _settle(tmp_path, monkeypatch,
                       runner.Dispatch("pick", "ok", unadopted=4))
    assert calls["merged"] == 1
    assert "merged" in calls["landed"]


def test_an_artefact_only_attempt_has_nothing_to_merge(tmp_path, monkeypatch):
    """No commit on the branch at all — the candidates *are* the output — so the
    merge machinery is not asked to run over an empty branch."""
    root, calls = _settle(tmp_path, monkeypatch,
                          runner.Dispatch("pick", "ok", unadopted=4), commits=False)
    assert calls["merged"] == 0
    assert (root / "Board" / "needs-decision" / "icon.md").is_file()
    assert "nothing to merge" in calls["landed"]


def test_a_reviewed_card_with_nothing_unadopted_still_lands_in_testing(tmp_path,
                                                                       monkeypatch):
    """The rule must not swallow the ordinary landing it sits next to."""
    root, _ = _settle(tmp_path, monkeypatch,
                      runner.Dispatch("reviewed", "ok", how_to_test="Open the game."))
    landed = root / "Board" / "testing" / "icon.md"
    assert landed.is_file()
    assert "Open the game." in landed.read_text(encoding="utf-8")


def test_a_picked_card_is_reported_as_a_decision_not_as_landed(tmp_path):
    """The morning digest splits the night by outcome, and `pick` in the landed
    set would list a card that is waiting on him under "these are done"."""
    assert "pick" in run_record.DECISION_OUTCOMES
    assert "pick" not in run_record.LANDED_OUTCOMES


# --- the stage that produces it ----------------------------------------------

def _reviewed(tmp_path, monkeypatch, verdict: dict, *, unadopted: int,
              commits: bool = True) -> runner.Dispatch:
    root = helpers._worktree_repo(tmp_path)
    helpers._tier_binding(root)
    helpers._card(root, "tasks", "icon", worker="art", checker="art-reviewer")
    card = board.Card.load(root / "Board" / "tasks" / "icon.md", "tasks")
    monkeypatch.setattr(runner, "branch_has_commits", lambda *a, **k: commits)
    monkeypatch.setattr(runner, "review_branch", lambda *a, **k: (verdict, 0.1, None))
    return runner.review_stage(
        root, card, runner.Dispatch("review", "made four candidates", 0.1,
                                    unadopted=unadopted),
        "development_team", 0.0, 120)


def test_an_ok_review_over_unadopted_candidates_becomes_a_pick(tmp_path, monkeypatch):
    """`ok` on the diff is a true statement about the diff, and not one about the
    card, once the card's real output never entered the diff."""
    result = _reviewed(tmp_path, monkeypatch, {"verdict": "ok"}, unadopted=4)
    assert result.outcome == "pick"
    assert result.unadopted == 4


def test_an_ok_review_with_everything_installed_is_still_reviewed(tmp_path,
                                                                  monkeypatch):
    result = _reviewed(tmp_path, monkeypatch, {"verdict": "ok"}, unadopted=0)
    assert result.outcome == "reviewed"


def test_an_artefact_only_card_no_longer_waits_in_review(tmp_path, monkeypatch):
    """There is no diff to show a reviewer, so nothing about this card is queued
    for Claude — and `review/` is the lane that means it is."""
    result = _reviewed(tmp_path, monkeypatch, {"verdict": "ok"}, unadopted=4,
                       commits=False)
    assert result.outcome == "pick"


def test_an_artefact_only_card_with_no_candidates_left_is_untouched(tmp_path,
                                                                    monkeypatch):
    """Everything it produced is installed, so there is no question to ask and
    the pre-existing degradation stands."""
    result = _reviewed(tmp_path, monkeypatch, {"verdict": "ok"}, unadopted=0,
                       commits=False)
    assert result.outcome == "review"
