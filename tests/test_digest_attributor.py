"""The digest's answered-but-not-moved nudge, and the literal name that broke it.

The defect (2026-08-04): `digest._KAREL_ANSWER` matched the literal token `karel`
after the `·` in a `## Thread` heading. That is the origin maintainer's handle, so
in every other repo the advisory ran, matched nothing, and reported a clean board
— `silent-noop`, and the quietest possible kind, because a nudge that never fires
is indistinguishable from a board with nothing to nudge about.

Nothing tested the advisory at all, which is why the literal survived the
extraction. These are that coverage.

**Why the token could not be replaced by a shape rule**, and why the fix is a
declared field rather than a cleverer regex: counted over the origin project's 62
`### <date> · <token>` headings, the bare single-word attributors are `karel`
(24), `triage` (10), `code-thread` and `claude`. Three of those four are agents
writing their own notes. "A single bare word" would read every one as a recorded
human decision, so the name is doing the work and the project has to say what it
is.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nightshift import board, digest


def _card(thread: str) -> board.Card:
    text = (
        "---\n"
        "id: c\n"
        "state: needs-decision\n"
        "---\n\n"
        "## Question\n\n"
        "### Decision 1 — DECIDED (2026-08-04)\n"
        "something\n\n"
        "## Thread\n\n"
        f"{thread}"
    )
    return board.Card(path=Path("c.md"), lane="needs-decision",
                      fields=board.parse_fields(text), text=text)


@pytest.mark.parametrize("heading,expected", [
    ("### 2026-08-04 · karel", True),
    ("### 2026-08-04 · karel — resolved", True),
    ("#### 2026-08-04 · Karel", True),                      # case-insensitive
    ("### 2026-08-04 · triage", False),                     # an agent's own note
    ("### 2026-08-04 · code-thread (attempt 2)", False),
    ("### 2026-08-04 · claude", False),
    ("### 2026-08-04 · interactive session (Karel + Claude)", False),  # a conversation
    ("### 2026-08-04 · karelina", False),                   # \b, not a prefix match
])
def test_only_a_decision_signed_by_the_declared_handle_counts(heading, expected):
    """Every one of these shapes is in the origin project's real board corpus."""
    card = _card(heading + "\nbody\n")
    assert digest._has_maintainer_answer(card, "karel") is expected


def test_the_handle_is_the_projects_own_not_a_baked_in_name():
    """The regression proper: another project's maintainer signs with their own
    handle, and the advisory has to see it. Before 2026-08-04 this returned False
    for every repo but one."""
    card = _card("### 2026-08-04 · alex — go with B\n")
    assert digest._has_maintainer_answer(card, "alex") is True
    assert digest._has_maintainer_answer(card, "karel") is False


def test_no_declared_handle_disables_the_nudge_rather_than_guessing():
    """Absence is meaningful. A project that never adopted the `·` convention has
    no token, and silence is the honest answer — a guessed token would restore
    exactly the silent no-op this fix removed, while looking configured."""
    card = _card("### 2026-08-04 · karel — go with B\n")
    assert digest._has_maintainer_answer(card, "") is False
    assert digest._has_maintainer_answer(card, "   ") is False
    assert digest._answer_pattern("") is None


def test_a_handle_with_regex_metacharacters_is_matched_literally():
    """`re.escape`, because a handle is a name the operator typed into a config
    file, not a pattern — and `a.b` matching `axb` is the kind of wrong nobody
    would ever look for here."""
    card = _card("### 2026-08-04 · a.b — yes\n")
    assert digest._has_maintainer_answer(card, "a.b") is True
    assert digest._has_maintainer_answer(_card("### 2026-08-04 · axb — yes\n"), "a.b") is False


def test_the_question_section_is_never_read_as_an_answer():
    """`### Decision N — DECIDED (…)` headers live in `## Question` and are the
    card *asking*. Scoping to `## Thread` is what keeps a picker from reading as a
    resolution."""
    card = _card("nothing recorded yet\n")
    assert digest._has_maintainer_answer(card, "karel") is False
