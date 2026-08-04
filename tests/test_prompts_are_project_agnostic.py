"""Nothing this package says out loud may come from the project it was extracted from.

The defect these tests exist to prevent (2026-08-04,
`nightshift-prompts-name-the-origin-project`): `runner.py` sends five prompts to a
model, and four of them carried the origin project. The worker prompt opened
*"Execute one card from the Dungeoneer board"* — in every consuming project, on
every card. The review prompt routed on a named person who is not the maintainer,
and told the reviewer not to re-check *"parity, imports, help-catalog overflow,
asset hygiene"*: a game's gate list, handed to a reviewer that will never see one
of them. All four cited `00_architecture.md §16` as the authority for the tier
they were running at — a design note that deliberately does not ship, so the
citation named a file the agent could not open.

**Why it survived so long.** Every one of those strings is *true* here. The
framework repo is the one place where naming the origin project reads as correct,
so no run, no gate and no reviewer inside it could see the problem; it is only
visible from a repo that does not have that name. Same shape as
`origin-repo-had-it-by-hand`, one layer over: not a property the origin repo
satisfies by hand, but a property only the origin repo satisfies at all.

**Why a test here and not a gate that ships.** A gate would run in consuming
projects, where the rule is meaningless — their code may name whatever they like,
and the string this rule is about is *ours*. Shipping it would put a permanent
no-op in every install, which is `gate-scope-outlived-its-directory` written
deliberately. The rule is about this package, so it lives in this package's
suite.

**Scope: runtime strings only, never docstrings or comments.** 141 of the 187
mentions counted on 2026-08-04 were provenance — *"The failure this exists to
prevent (Dungeoneer, 2026-07-27, `triage-read-private-ideas`)"* — a rule naming
the incident that earned it. That is the corrections discipline working and it is
why these modules are readable. The distinction: prose saying *where a rule came
from* is provenance and stays; a string saying *how the framework behaves now* in
origin-project terms is stale, and only a string an agent or an operator actually
receives can do that.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "nightshift"

# The origin project and its maintainer. Deliberately literal: the point is that
# these names must not reach a consuming project, and a test that computed them
# from the repo would go quiet the moment the repo was renamed.
ORIGIN = re.compile(r"dungeoneer|Karel", re.IGNORECASE)

# `00_architecture.md §16`, `03_board.md`, `07_portability.md §8` — the design
# notes. They are real files in *this* repo and are excluded from the sdist on
# purpose, so a shipped string may never cite one. `templates/README.md` states
# the rule the shipped charters already follow: every rule a non-shipping doc is
# cited for must be stated in full where it is cited.
DESIGN_NOTE = re.compile(r"\b\d\d_[a-z_]+\.md")


def _runtime_strings(path: Path) -> list[tuple[int, str]]:
    """Every string constant in a module that is not a docstring.

    Docstrings are found by position — the first statement of a module, class or
    function — rather than by looking for triple quotes, because the shape that
    matters is "does a reader of this package see it" vs "does a consumer of it".
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _modules() -> list[Path]:
    # `templates/` is data, not code — it is rendered at install time and has its
    # own coverage in test_init.py, including the maintainer token.
    return sorted(p for p in PACKAGE.rglob("*.py") if "templates" not in p.parts)


def _offences(pattern: re.Pattern[str]) -> list[str]:
    found = []
    for module in _modules():
        for lineno, text in _runtime_strings(module):
            for line in text.splitlines():
                if pattern.search(line):
                    rel = module.relative_to(PACKAGE.parent).as_posix()
                    found.append(f"{rel}:{lineno}  {line.strip()[:100]}")
    return found


def test_no_runtime_string_names_the_origin_project():
    """An operator reading a hook's denial, or an agent reading a prompt, must
    never be told about a repo that is not theirs."""
    assert _offences(ORIGIN) == []


def test_no_runtime_string_cites_a_document_that_does_not_ship():
    """A citation an agent cannot follow is worse than no citation: it reads as
    authority and resolves to nothing. State the rule where it is cited."""
    assert _offences(DESIGN_NOTE) == []


# --- the four prompts specifically -------------------------------------------
#
# The tests above cover the package. These pin the four strings the defect was
# actually found in, so a regression names the prompt rather than a line number.

PROMPTS = ("_PROMPT", "_CHECKER_PROMPT", "_REVIEW_PROMPT", "_STALE_PROMPT")


@pytest.mark.parametrize("name", PROMPTS)
def test_a_dispatched_prompt_is_project_agnostic(name):
    from nightshift import runner

    prompt = getattr(runner, name)
    assert not ORIGIN.search(prompt), f"{name} names the origin project"
    assert not DESIGN_NOTE.search(prompt), f"{name} cites a design note that does not ship"


def test_the_review_prompt_does_not_hand_over_a_game_s_gate_list():
    """It used to name parity, imports, help-catalog overflow and asset hygiene as
    the things already covered — four gates a consuming project will not have. The
    replacement tells the reviewer to *run the suite* and read the list, which is
    correct in every repo and stays correct as the list grows."""
    from nightshift import runner

    for gate in ("parity", "help-catalog", "asset hygiene"):
        assert gate not in runner._REVIEW_PROMPT
    assert "nightshift.gates.run" in runner._REVIEW_PROMPT


def test_the_worker_prompt_still_states_the_rules_it_stopped_citing():
    """Removing a §-citation must not remove the rule with it — the whole reason
    the citations were replaceable is that the rule can be said in one clause."""
    from nightshift import runner

    # the tier rule, formerly "from 00_architecture.md §16"
    assert "running above it is a defect" in runner._PROMPT
    # parking, formerly "a success state (00_architecture.md §13)"
    assert "success state" in runner._PROMPT
    assert "what each would imply" in runner._PROMPT
