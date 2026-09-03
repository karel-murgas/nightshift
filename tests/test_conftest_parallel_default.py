"""Tests for the root `conftest.py` — this suite's own parallel default.

Measured 2026-09-03: a bare `python -m pytest` here was still running after 50
minutes and was killed unfinished, while `suite.parallel_args()` does the same 2174
tests in under four. Nothing was broken; the flags CLAUDE.md documents for the
consuming project were simply never typed in this one, and a default that exists
only in someone's fingers is not a default.

**What is worth testing here and what is not.** That xdist actually distributes is
proved by every other run in this file's own suite — the worker line is either there
or it is not. What these cover is the part that is easy to get subtly wrong and
silent when it is: the policy reaching xdist through `suite` rather than a second
spelling, the guards that must stand down, and the two hook-plumbing facts that took
three attempts to find and would be invisible if they regressed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import suite

import conftest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def outside_a_worker(monkeypatch):
    """Speak as a top-level pytest, not as the xdist worker this test is running in.

    Every test in this file runs *inside* a worker, so `PYTEST_XDIST_WORKER` is
    already set and the injection correctly stands down — which made the two
    positive assertions below fail on first run, for the right reason. Clearing it
    is how a test asks the question it means to ask.

    The same leak is a feature outside the tests: the variable is inherited by any
    child process, so a pytest spawned from within a worker — as several runner and
    preflight tests do — runs serially rather than starting its own fan-out inside
    one. That is the behaviour wanted on a box with this one's memory ceiling, and
    it is why the guard is deliberately not narrowed to "am I literally an xdist
    worker process".
    """
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)


class _Option:
    """A stand-in for `config.option` — a bare namespace, which is what it is."""

    def __init__(self, **kwargs):
        self.numprocesses = None
        self.dist = "no"
        self.maxworkerrestart = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Config:
    def __init__(self, **kwargs):
        self.option = _Option(**kwargs)


# --- the policy arrives through suite, not a second spelling of it -----------

def test_every_flag_the_policy_can_emit_maps_to_an_xdist_option():
    """The drift check, and the reason `_OPTION_FOR` is a table.

    `suite.parallel_args()` owns these flags (CLAUDE.md: never hand-roll a parallel
    pytest invocation, call that function). A flag added there and not here would not
    fail loudly — it would be dropped, and the suite would run under a policy nobody
    chose. So the mapping must cover everything the policy emits, in both spellings
    it uses.
    """
    for flags in (suite.XDIST_ARGS,
                  suite.parallel_args(available=True, workers=None),
                  suite.parallel_args(available=True, workers=7)):
        parsed = conftest.parsed_options(flags)
        assert parsed, flags
        # Round-trips to the same set of decisions whichever spelling was used.
        assert set(parsed) <= {"numprocesses", "dist", "maxworkerrestart"}


def test_the_worker_count_is_an_int_and_auto_stays_a_string():
    """xdist's own parser yields an int for a number and the literal `"auto"` for
    `auto`; downstream code compares against both. Handing it `"7"` would be a
    string where an int is expected, in a place nothing would notice until a
    comparison quietly failed."""
    assert conftest.parsed_options(("-n", "7"))["numprocesses"] == 7
    assert conftest.parsed_options(("-n", "auto"))["numprocesses"] == "auto"


def test_a_joined_flag_is_parsed_as_well_as_a_split_one():
    """`--max-worker-restart=0` is one token; `-n 5` is two. The policy emits both
    shapes in the same tuple."""
    assert conftest.parsed_options(("--max-worker-restart=0",)) == {
        "maxworkerrestart": 0}
    assert conftest.parsed_options(("--dist", "loadfile")) == {"dist": "loadfile"}


def test_an_unmapped_flag_raises_rather_than_being_ignored():
    """Silently dropping a flag is the failure this file exists to prevent, so the
    unmapped case is loud."""
    with pytest.raises(ValueError, match="--brand-new-flag"):
        conftest.parsed_options(("--brand-new-flag", "1"))


def test_a_flag_with_no_value_raises():
    with pytest.raises(ValueError, match="-n"):
        conftest.parsed_options(("-n",))


# --- the guards, each of which must stand the injection down -----------------

def test_a_bare_run_gets_the_policy(outside_a_worker):
    config = _Config()

    conftest.pytest_cmdline_main(config)

    assert config.option.numprocesses == suite.probed_worker_count()
    assert config.option.dist == "loadfile"


def test_a_typed_worker_count_is_left_alone(outside_a_worker):
    """A number someone typed is a decision, not a default in need of correcting."""
    config = _Config(numprocesses=2)

    conftest.pytest_cmdline_main(config)

    assert config.option.numprocesses == 2
    assert config.option.dist == "no", "injection must stand down whole, not merge"


def test_a_typed_dist_mode_is_left_alone(outside_a_worker):
    config = _Config(dist="loadscope")

    conftest.pytest_cmdline_main(config)

    assert config.option.dist == "loadscope"
    assert config.option.numprocesses is None


def test_a_worker_does_not_inject_into_itself(monkeypatch):
    """The fork bomb this guard prevents. Every xdist worker builds its own config
    and runs the root conftest again; without the check each of five workers would
    set `numprocesses` on itself and try to spawn five more — on the one box whose
    memory ceiling is the reason the cap exists."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    config = _Config()

    conftest.pytest_cmdline_main(config)

    assert config.option.numprocesses is None
    assert config.option.dist == "no"


def test_a_run_without_xdist_options_at_all_is_left_alone(outside_a_worker):
    """`-p no:xdist` leaves the options absent entirely. A run just told to skip the
    plugin must not be handed flags for it."""
    config = _Config()
    del config.option.numprocesses

    conftest.pytest_cmdline_main(config)      # must not raise

    assert not hasattr(config.option, "numprocesses")


def test_nothing_is_injected_when_xdist_is_missing(monkeypatch, outside_a_worker):
    """The correction this file is careful not to reintroduce
    (`xdist-assumed-not-probed`, 2026-07-27). xdist is an optional extra here, and
    `-n auto` without it is `error: unrecognized arguments: -n`, exit 4, no JUnit
    report — which is why the flags come from a function that can probe rather than
    from a static `addopts` that cannot."""
    monkeypatch.setattr(suite, "xdist_available", lambda: False)
    config = _Config()

    conftest.pytest_cmdline_main(config)

    assert config.option.numprocesses is None
    assert config.option.dist == "no"


# --- the two hook-plumbing facts that took three attempts to find ------------

def test_disabling_the_plugin_does_not_crash_the_run():
    """`optionalhook=True` on `pytest_xdist_auto_num_workers`, asserted through a
    real pytest rather than by reading the decorator.

    Without it `-p no:xdist` dies on `PluginValidationError: unknown hook` before a
    single test runs: the hookspec belongs to the plugin just disabled, and pluggy
    rejects an implementation of a spec that does not exist. Both this repo's and the
    consuming project's conftest had that hole on 2026-09-03, because neither had
    ever been run with the plugin off.
    """
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_suite.py",
         "-p", "no:xdist", "-q", "--no-header"],
        cwd=_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300,
    )

    assert "INTERNALERROR" not in done.stdout + done.stderr, done.stdout[-2000:]
    assert done.returncode == 0, done.stdout[-2000:]
    # And it really did run serially — no worker line to be found.
    assert "workers" not in done.stdout


def test_the_default_run_really_distributes():
    """The end-to-end fact, and the one that regressed twice while being written.

    `pytest_load_initial_conftests` does not fire from a rootdir conftest, and
    `pytest_configure` is one hook too late — xdist turns `numprocesses` into the
    `tx` specs that decide distribution inside its own `pytest_cmdline_main`, so a
    later write reads back correctly and distributes nothing. Both wrong versions
    passed every unit test above; only spawning a real pytest tells them apart, so
    that is what this does.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_XDIST_WORKER"}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_suite.py", "--no-header"],
        cwd=_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600, env=env,
    )

    assert done.returncode == 0, done.stdout[-2000:]
    expected = suite.probed_worker_count()
    if expected is None:                      # a box the probe cannot read
        pytest.skip("no memory probe on this box; the count is xdist's own `auto`")
    assert f"{expected} workers" in done.stdout, done.stdout[:2000]
