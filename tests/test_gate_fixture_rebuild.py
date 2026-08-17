"""Tests for the `fixture_rebuild` core gate — a test must not build a git repo
from scratch where a shared template exists.

Half of what is asserted here is that the gate stays *quiet*. It reads test code,
which is the noisiest possible corpus for a rule about git: fixtures mention
`init` in commit messages, in docstrings, in branch names. The first draft of
this gate flagged `_git(root, "commit", "-qm", "init")` in three of this suite's
own files — a commit *message*, not a subcommand — and a gate whose first act is
to cry wolf on the fixtures it is meant to protect gets muted within the week.
So `test_a_commit_message_of_init_is_not_a_git_init` is the load-bearing test
here, not the one that catches the real thing.

The other half is the self-activation. This is a core gate and runs in every
consuming project, almost none of which have this convention; it must find the
template module before it has any opinion at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

from nightshift.gates import fixture_rebuild  # noqa: E402

_TEMPLATE = '''\
"""The shared fixture template."""


def git_init(root, **kwargs):
    return root


def repo_copy(name, dest, build):
    return dest
'''


def _project(tmp_path: Path, body: str, *, template: str | None = _TEMPLATE) -> Path:
    """A project with a tests dir, a manifest naming it, and one test module."""
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "proj"\nsource_dirs = ["pkg"]\nextra_source_dirs = ["tests"]\n'
        '\n[tests]\ndir = "tests"\n',
        encoding="utf-8", newline="")
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    if template is not None:
        (tests / "_fixtures.py").write_text(template, encoding="utf-8", newline="")
    (tests / "test_thing.py").write_text(body, encoding="utf-8", newline="")
    return tmp_path


# --- what it must catch --------------------------------------------------------


def test_a_helper_call_spelling_init_is_caught(tmp_path):
    """`_git(repo, "init", "-q")` — the dominant shape across this suite's 22
    converted files, and the one that cost 135 ms every time it ran."""
    root = _project(tmp_path, 'def f(repo):\n    _git(repo, "init", "-q", "-b", "main")\n')
    violations = fixture_rebuild.check(root)
    assert len(violations) == 1
    assert violations[0].file == "tests/test_thing.py"
    assert "tests/_fixtures.py" in violations[0].rule


def test_a_hand_built_argv_is_caught(tmp_path):
    root = _project(tmp_path, (
        "import subprocess\n"
        'subprocess.run(["git", "init", "-q", "-b", "main"], check=True)\n'
    ))
    assert len(fixture_rebuild.check(root)) == 1


def test_the_dash_C_form_is_caught_too(tmp_path):
    """`["git", "-C", str(repo), "init"]` — the path is not a literal, so the
    subcommand is still the first plain word the walk sees."""
    root = _project(tmp_path, (
        "import subprocess\n"
        "def f(repo):\n"
        '    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)\n'
    ))
    assert len(fixture_rebuild.check(root)) == 1


# --- what it must NOT catch ----------------------------------------------------


def test_a_commit_message_of_init_is_not_a_git_init(tmp_path):
    """The false positive that actually happened, in three files at once. `init`
    here is what the commit is *called*; reading position rather than presence is
    the whole difference between a gate people keep and one they switch off."""
    root = _project(tmp_path, (
        "def f(repo):\n"
        '    _git(repo, "add", "-A")\n'
        '    _git(repo, "commit", "-qm", "init")\n'
    ))
    assert fixture_rebuild.check(root) == []


def test_other_git_subcommands_are_not_touched(tmp_path):
    root = _project(tmp_path, (
        "def f(repo):\n"
        '    _git(repo, "branch", "-M", "init")\n'
        '    _git(repo, "checkout", "-q", "-b", "init")\n'
        '    _git(repo, "rev-parse", "HEAD")\n'
    ))
    assert fixture_rebuild.check(root) == []


def test_prose_about_git_init_is_not_an_invocation(tmp_path):
    """`pytest_invocation`'s lesson, inherited: a gate with a false positive on
    its own documentation is a gate nobody reads twice."""
    root = _project(tmp_path, (
        '"""This fixture used to run git init plus three config calls."""\n'
        "MESSAGE = 'run git init yourself if you need a bare repo'\n"
    ))
    assert fixture_rebuild.check(root) == []


def test_calling_the_template_is_the_point_and_is_never_flagged(tmp_path):
    root = _project(tmp_path, (
        "import _fixtures\n"
        "def f(repo):\n"
        '    _fixtures.git_init(repo, branch="main")\n'
    ))
    assert fixture_rebuild.check(root) == []


def test_the_template_module_itself_may_spell_it(tmp_path):
    """Somebody has to run `git init`; the gate's whole claim is that it should
    happen once. Excluded by name, as `pytest_invocation` excludes `suite.py`."""
    root = _project(tmp_path, "X = 1\n", template=_TEMPLATE + (
        "\n\ndef _build(target):\n"
        '    _git(target, "init", "-q")\n'
    ))
    assert fixture_rebuild.check(root) == []


# --- self-activation -----------------------------------------------------------


def test_a_project_with_no_template_is_left_alone(tmp_path):
    """The reason this can be a core gate. A consuming project that never adopted
    the convention gets no opinion — not a violation it cannot act on."""
    root = _project(tmp_path, 'def f(repo):\n    _git(repo, "init", "-q")\n',
                    template=None)
    assert fixture_rebuild.check(root) == []


def test_a_template_without_git_init_does_not_activate_it(tmp_path):
    """A `_fixtures.py` of unrelated helpers is not this convention, and the gate
    must not point at a function that is not there to call."""
    root = _project(tmp_path, 'def f(repo):\n    _git(repo, "init", "-q")\n',
                    template="def make_card(text):\n    return text\n")
    assert fixture_rebuild.check(root) == []


def test_it_activates_the_moment_the_template_grows_one(tmp_path):
    """The other side of the same switch — otherwise the two tests above would
    pass on a gate that never fires at all."""
    root = _project(tmp_path, 'def f(repo):\n    _git(repo, "init", "-q")\n',
                    template="def make_card(text):\n    return text\n")
    assert fixture_rebuild.check(root) == []
    (root / "tests" / "_fixtures.py").write_text(_TEMPLATE, encoding="utf-8", newline="")
    assert len(fixture_rebuild.check(root)) == 1


# --- appeals -------------------------------------------------------------------


def test_an_appeal_exempts_the_line(tmp_path):
    """Expected rather than rare here: a bare origin, or a repo whose `.git`
    records an absolute path, genuinely cannot be copied."""
    root = _project(tmp_path, (
        "import subprocess\n"
        "# gate-ok(fixture_rebuild): a bare origin, which the template does not\n"
        "# offer and a copy could not carry.\n"
        'subprocess.run(["git", "init", "-q", "--bare", "origin.git"], check=True)\n'
    ))
    assert fixture_rebuild.check(root) == []


def test_an_appeal_naming_another_gate_does_not_cover_this_one(tmp_path):
    root = _project(tmp_path, (
        "import subprocess\n"
        "# gate-ok(pytest_invocation): a reason long enough to be accepted here.\n"
        'subprocess.run(["git", "init", "-q"], check=True)\n'
    ))
    assert fixture_rebuild.check(root)
