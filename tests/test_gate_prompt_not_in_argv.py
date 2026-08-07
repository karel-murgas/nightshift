"""Tests for the `prompt_not_in_argv` core gate — a worker prompt travels on the
child's stdin, never on the command line.

**The tests that matter are the ones that reintroduce the defect.** A gate asserted
only against a clean tree is indistinguishable from a gate that returns `[]`
unconditionally, and this suite has already paid for that lesson twice
(`run_stop_recorded` read a deleted path and reported green for its whole life;
`deletion_sweep` filtered on a literal `dungeoneer/` prefix and was a no-op
everywhere else). So the central case here takes the **real `runner.py` and puts
the 2026-08-06 crash back into it**, byte for byte, and requires the gate to go red
on all four of its call sites.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import prompt_not_in_argv  # noqa: E402

REPO = Path(_nightshift_gates.__file__).resolve().parent.parent.parent
REAL_RUNNER = REPO / "nightshift" / "runner.py"
REAL_FIX = REPO / "nightshift" / "fix.py"


def _tree(tmp_path: Path, name: str, source: str) -> Path:
    ai = tmp_path / ".ai"
    ai.mkdir(exist_ok=True)
    (ai / name).write_text(source, encoding="utf-8")
    return tmp_path


# --- the gate fires on the real defect, not just on a toy ------------------------


def test_reintroducing_the_crash_into_the_real_runner_goes_red(tmp_path):
    """The proof the gate works: take today's `runner.py`, put the prompt back on
    `-p` exactly as it was written before `worker-prompt-off-argv`, and require a
    violation for each of the four dispatch sites.

    Four, not five: the same replacement also lands inside `_run_worker`'s
    docstring, which quotes the defective line to explain it. That one is prose and
    must stay invisible — a gate that flagged its own documentation would be muted
    within a week, which is why this walks sequence literals instead of text.
    """
    source = REAL_RUNNER.read_text(encoding="utf-8")
    defective = source.replace('binary, "-p",', 'binary, "-p", prompt,')
    assert defective != source, (
        "the argv spelling in runner.py moved — this test can no longer put the "
        "defect back, so it is no longer proving anything. Re-point it."
    )

    root = _tree(tmp_path, "runner_copy.py", defective)
    violations = prompt_not_in_argv.check(root)

    assert len(violations) == 4, \
        f"expected the four dispatch sites, got {[str(v) for v in violations]}"
    assert all("WinError 206" in v.rule for v in violations)
    assert all("prompt=" in v.rule for v in violations)


def test_reintroducing_the_crash_into_the_real_fix_module_goes_red(tmp_path):
    """The fifth call site, which lives in another module entirely — the reason
    this is a gate over a directory and not a check inside `runner.py`."""
    source = REAL_FIX.read_text(encoding="utf-8")
    defective = source.replace('binary, "-p", "--model"', 'binary, "-p", text, "--model"')
    assert defective != source

    root = _tree(tmp_path, "fix_copy.py", defective)
    assert len(prompt_not_in_argv.check(root)) == 1


def test_the_shipped_tree_is_clean():
    """Green here is only meaningful because the two tests above show red. Kept
    anyway: it is what fails the day a sixth call site is written the old way."""
    assert prompt_not_in_argv.check(REPO) == []


# --- the shapes, one at a time ---------------------------------------------------


@pytest.mark.parametrize("argv", [
    'ARGV = [binary, "-p", prompt, "--agent", "code-thread"]',      # a name
    'ARGV = [binary, "--print", prompt]',                           # the long form
    'ARGV = [binary, "-p", "do the thing", "--model", model]',      # an inline literal
    'ARGV = [binary, "-p", build_prompt(card), "--model", model]',  # a call
    'ARGV = [binary, "-p", f"do {thing}"]',                         # an f-string
    'ARGV = (binary, "-p", prompt)',                                # a tuple
])
def test_a_value_after_the_flag_is_caught(tmp_path, argv):
    assert prompt_not_in_argv.check(_tree(tmp_path, "newthing.py", argv + "\n"))


@pytest.mark.parametrize("argv", [
    'ARGV = [binary, "-p", "--agent", agent, "--model", model]',    # the fixed shape
    'ARGV = [binary, "--print", "--model", model]',                 # long form, fixed
    'ARGV = [binary, "--model", model, "-p"]',                      # trailing, bare
    'ARGV = [binary, "-p", *STREAM_ARGV, *budget_argv(cap)]',       # a flag-group splat
])
def test_the_corrected_shape_stays_green(tmp_path, argv):
    assert prompt_not_in_argv.check(_tree(tmp_path, "newthing.py", argv + "\n")) == []


def test_prose_about_the_flag_is_not_a_violation(tmp_path):
    """Docstrings, comments and prompt text discussing `-p` mode are documentation,
    not invocations. Only sequence literals are inspected."""
    root = _tree(tmp_path, "newthing.py", (
        '"""It used to be [binary, "-p", prompt, ...] and that crashed."""\n'
        '# argv = [binary, "-p", prompt]\n'
        'NOTE = "in -p mode there is nobody to ask, so a denied tool stalls"\n'
    ))
    assert prompt_not_in_argv.check(root) == []


def test_an_appeal_marker_exempts_a_line(tmp_path):
    root = _tree(tmp_path, "newthing.py", (
        '# gate-ok(prompt_not_in_argv): this is `git log -p <path>`, not the CLI\n'
        'ARGV = ["git", "log", "-p", str(path)]\n'
    ))
    assert prompt_not_in_argv.check(root) == []


def test_an_unparseable_file_is_not_this_gates_problem(tmp_path):
    assert prompt_not_in_argv.check(_tree(tmp_path, "broken.py", "def nope(:\n")) == []


def test_no_ai_dir_is_not_a_crash(tmp_path):
    assert prompt_not_in_argv.check(tmp_path) == []
