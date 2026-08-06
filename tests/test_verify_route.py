"""Tests for `verify:` — the card's own declaration of how it gets verified,
and therefore of where a reviewed card lands (`unplayable-cards-still-land-in-testing`).

`testing/` used to be the only exit from a successful dispatch, so a card with no
player-visible surface — a gate, a deletion, inner wiring — waited on Karel at the
keyboard for a verification he had no way to perform. Eleven of the sixteen cards
sitting there on 2026-08-06 were that kind, and the five he genuinely should play
were buried among them.

The field is author-owned and decided at triage, modelled on `tier:` for the same
reason that one works: written when the card is, enforced by `card_schema`,
consumed by the runner, and no caller ever guesses. Absent means `play` at every
read site — the default has to fall toward his desk, because a card reaching
`done/` because someone forgot a field is the direction nothing recovers from.

The block-form `tags:` half is here too, and not by accident of filing: it is the
same file, the same class of defect, and the same failure mode — a field the
parser could not see disabled every rule keyed on it while the gate reported green.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

from nightshift import board, digest, runner  # noqa: E402
from nightshift.gates import card_schema  # noqa: E402

_FRONT = """\
---
id: a-card
title: "A card"
state: {lane}
tier: worker
worker: code-thread
recipe: none
unattended: true
created: 2026-08-02
{extra}---

## Intent

Something.

## Approach

One paragraph.

## Acceptance

- it works

## Steps

1. do it

## Open questions

none
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / ".ai").mkdir(parents=True)
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "code-thread.md").write_text("x", encoding="utf-8")
    (repo / ".ai" / "manifest.toml").write_text('[project]\nsource_dirs = ["pkg"]\n',
                                                encoding="utf-8", newline="\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _card(repo: Path, lane: str, extra: str = "", body: str = "") -> Path:
    path = repo / "Board" / lane / "a-card.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _FRONT.format(lane=lane, extra=extra) + body
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# --- the schema --------------------------------------------------------------

def test_a_tasks_card_without_verify_is_a_violation(tmp_path):
    """The whole mechanism rests on the field being there to read, and `tasks/` is
    the lane where the decision is still ahead of the card."""
    repo = _repo(tmp_path)
    _card(repo, "tasks")
    assert any("verify" in v.rule for v in card_schema.check(repo))


def test_both_values_are_accepted_and_nothing_else_is(tmp_path):
    repo = _repo(tmp_path)
    for value in ("play", "review"):
        _card(repo, "tasks", extra=f"verify: {value}\n")
        assert card_schema.check(repo) == [], value
    _card(repo, "tasks", extra="verify: maybe\n")
    assert any("verify: maybe" in v.rule for v in card_schema.check(repo))


def test_an_archived_card_without_verify_is_not_retroactively_reddened(tmp_path):
    """The field arrived on 2026-08-06 and the board already held dozens of cards.
    Requiring it everywhere would make the gate's first act a demand that history
    be rewritten — the same call `_DISPATCHED` already makes for `unattended:`."""
    repo = _repo(tmp_path)
    for lane in ("done", "testing", "failed", "review"):
        _card(repo, lane)
    assert card_schema.check(repo) == []


def test_a_play_card_in_testing_must_say_how_to_test_it(tmp_path):
    """A claim on Karel's time has to say what to do with it."""
    repo = _repo(tmp_path)
    _card(repo, "testing", extra="verify: play\n")
    assert any("How to test" in v.rule for v in card_schema.check(repo))

    _card(repo, "testing", extra="verify: play\n",
          body="\n## How to test\n\nStart a run, open the hotbar, expect two slots.\n")
    assert card_schema.check(repo) == []


def test_a_review_card_in_testing_needs_no_scenario(tmp_path):
    """Nobody has to invent a scenario for a gate, and an invented one is worse
    than none."""
    repo = _repo(tmp_path)
    _card(repo, "testing", extra="verify: review\n")
    assert card_schema.check(repo) == []


def test_the_scenario_is_not_demanded_in_tasks(tmp_path):
    """At triage the work does not exist yet, so nobody could write it."""
    repo = _repo(tmp_path)
    _card(repo, "tasks", extra="verify: play\n")
    assert card_schema.check(repo) == []


# --- absent means play, at every read site -----------------------------------

def test_an_absent_field_reads_as_play(tmp_path):
    repo = _repo(tmp_path)
    path = _card(repo, "testing")
    assert board.Card.load(path, "testing").verify == "play"


def test_an_unrecognised_value_reads_as_play_too(tmp_path):
    """`card_schema` catches the typo; the read site must not fail *open* toward
    `done/` in the window before anyone runs it."""
    repo = _repo(tmp_path)
    path = _card(repo, "testing", extra="verify: reviewd\n")
    assert board.Card.load(path, "testing").verify == "play"


def test_the_digest_reads_the_declared_field_instead_of_the_lane(tmp_path):
    """It used to infer the label from the lane, which could only repeat the
    lane's own assumption back at him."""
    repo = _repo(tmp_path)
    path = _card(repo, "testing", extra="verify: review\n")
    card = digest.Card.load(path, "testing")
    assert digest._landed_tag({"outcome": "reviewed"}, card, "testing") == "review"

    path = _card(repo, "testing", extra="verify: play\n")
    card = digest.Card.load(path, "testing")
    assert digest._landed_tag({"outcome": "reviewed"}, card, "testing") == "play"


# --- the routing -------------------------------------------------------------

def _settled(tmp_path, monkeypatch, verify: str, how_to_test: str = "Open the game.") -> Path:
    repo = _repo(tmp_path)
    _card(repo, "tasks", extra=f"verify: {verify}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "board")
    monkeypatch.setattr(runner, "rebase_and_merge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(runner, "default_base", lambda root: "main")
    monkeypatch.setattr(runner, "read_telemetry", lambda *a, **k: None)
    runner.settle(repo, "a-card",
                  runner.Dispatch("reviewed", "ok", how_to_test=how_to_test))
    return repo


def test_a_review_card_lands_in_done_without_passing_through_testing(tmp_path, monkeypatch):
    repo = _settled(tmp_path, monkeypatch, "review")
    assert (repo / "Board" / "done" / "a-card.md").is_file()
    assert not (repo / "Board" / "testing" / "a-card.md").exists()


def test_a_play_card_lands_in_testing_carrying_its_scenario(tmp_path, monkeypatch):
    repo = _settled(tmp_path, monkeypatch, "play")
    landed = repo / "Board" / "testing" / "a-card.md"
    assert landed.is_file()
    assert "## How to test" in landed.read_text(encoding="utf-8")
    assert "Open the game." in landed.read_text(encoding="utf-8")


def test_a_play_card_whose_worker_wrote_no_scenario_says_so(tmp_path, monkeypatch):
    """Silence here would land a bare card in `testing/` and leave him guessing —
    the state this whole card exists to end."""
    repo = _settled(tmp_path, monkeypatch, "play", how_to_test="")
    text = (repo / "Board" / "testing" / "a-card.md").read_text(encoding="utf-8")
    assert "recorded no scenario" in text


def test_a_review_card_gets_no_how_to_test_section(tmp_path, monkeypatch):
    repo = _settled(tmp_path, monkeypatch, "review")
    text = (repo / "Board" / "done" / "a-card.md").read_text(encoding="utf-8")
    assert "## How to test" not in text


# --- the tag parser, the same defect one field over ---------------------------

def test_a_block_form_tag_list_is_parsed(tmp_path):
    """Obsidian writes this form by default. The parser saw an empty value, which
    reads as *absent*, so every rule keyed on tags was a no-op while the gate
    reported green (`tag-parser-blind-to-block-form`)."""
    repo = _repo(tmp_path)
    _card(repo, "tasks", extra="verify: review\ntags:\n  - nightshift\n")
    violations = card_schema.check(repo)
    assert any("unattended" in v.rule and "nightshift" in v.rule for v in violations), violations


def test_the_inline_form_still_works(tmp_path):
    repo = _repo(tmp_path)
    _card(repo, "tasks", extra="verify: review\ntags: [nightshift]\n")
    violations = card_schema.check(repo)
    assert any("unattended" in v.rule and "nightshift" in v.rule for v in violations)


def test_a_block_form_tag_list_on_a_correct_card_is_silent(tmp_path):
    """Parsing the form must not mean flagging it — the shape is legitimate, and a
    gate that fired on the maintainer's own editor's output would get muted."""
    repo = _repo(tmp_path)
    text = _FRONT.format(lane="tasks", extra="verify: review\ntags:\n  - nightshift\n")
    path = repo / "Board" / "tasks" / "a-card.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.replace("unattended: true", "unattended: false"),
                    encoding="utf-8", newline="\n")
    assert card_schema.check(repo) == []
