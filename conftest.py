"""Root conftest — make a bare `python -m pytest` here parallel, safely.

The framework's own suite had no parallel default while the project it serves has
had one for weeks. Measured 2026-09-03: a bare `python -m pytest` in this repo was
still running after 50 minutes and was killed without finishing; the same suite
under `suite.parallel_args()` is 2174 tests in under four minutes. Nothing was
wrong except that the flags CLAUDE.md documents for Dungeoneer were never typed
here — and a default that exists only in someone's fingers is not a default.

**Why this is code and not `addopts` in `pyproject.toml`.** That was the obvious
version and it reintroduces a correction already paid for once
(`xdist-assumed-not-probed`, 2026-07-27). pytest-xdist is an **optional** extra here
(`[project.optional-dependencies].dev`), and `-n auto` without it is not a slow run
— it is `error: unrecognized arguments: -n`, exit 4, no JUnit report written. A
static ini value cannot ask whether the plugin is installed. `suite.parallel_args()`
can, and returns `()` when it is not, which is the only safe direction to fall back
in. Going through that call is also what keeps the policy in one place: CLAUDE.md's
rule is that `suite.py` owns these flags and no caller re-spells them, and a TOML
literal would be a second spelling `gates.pytest_invocation` cannot even see.
"""
from __future__ import annotations

import os

import pytest

from nightshift import suite

# How each flag `suite.parallel_args()` can emit reaches xdist as a parsed option.
# A mapping rather than three assignments so that a flag added to that policy and
# not to this table is a hard failure here instead of a flag silently dropped —
# `tests/test_conftest_parallel_default.py` asserts the table covers everything the
# policy emits, which is the check that keeps the two from drifting apart.
_OPTION_FOR = {
    "-n": "numprocesses",
    "--numprocesses": "numprocesses",
    "--dist": "dist",
    "--max-worker-restart": "maxworkerrestart",
}


def parsed_options(flags: tuple[str, ...]) -> dict[str, object]:
    """`suite.parallel_args()`'s argv as `{option_name: value}`.

    Handles both spellings the policy uses — a two-token `-n 5` and a joined
    `--max-worker-restart=0` — and coerces a numeric worker count to `int`, because
    xdist's own parser produces an int and code downstream compares against one.
    `"auto"` and `"logical"` pass through as the strings xdist expects.

    Raises on a flag with no entry in `_OPTION_FOR`, deliberately: silently ignoring
    it would run the suite with a policy nobody chose, which is the failure this
    whole file is replacing.
    """
    out: dict[str, object] = {}
    pending: str | None = None
    for token in flags:
        if pending is not None:
            out[pending] = int(token) if token.isdigit() else token
            pending = None
            continue
        name, _, inline = token.partition("=")
        if name not in _OPTION_FOR:
            raise ValueError(f"conftest cannot map {name!r} to an xdist option; "
                             f"add it to _OPTION_FOR")
        if inline:
            out[_OPTION_FOR[name]] = int(inline) if inline.isdigit() else inline
        else:
            pending = _OPTION_FOR[name]
    if pending is not None:
        raise ValueError(f"{flags[-1]!r} was given no value")
    return out


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config) -> None:
    """Apply the policy to this run unless the run already has an opinion.

    **Why here, and not either of the two more obvious places.** Both were tried and
    measured on 2026-09-03:

    * `pytest_load_initial_conftests` is the documented hook for editing argv, and it
      does not fire from a *rootdir conftest* — printing from it produced nothing
      while the suite went on running serially. By the time this file is importable
      the argv it would have edited is already parsed.
    * `pytest_configure` is too late by one hook. xdist turns `numprocesses` into the
      `tx` specs that actually decide distribution inside its own
      `pytest_cmdline_main`, and `_is_distribution_mode` then asks for a non-empty
      `tx`. Setting `numprocesses` after that reads back correctly and distributes
      nothing — the run says `collected N items` with no worker line, which is
      exactly what it did here before this moved.

    So: `pytest_cmdline_main`, `tryfirst`. xdist's own impl is `tryfirst` too, and
    among equals pluggy calls the most recently registered first — a conftest is
    registered after the installed plugins, so this writes `numprocesses` and xdist
    reads it on the very next call. The hook is `firstresult`; returning `None` is
    what lets the run continue to pytest's own main.

    **The `PYTEST_XDIST_WORKER` guard is load-bearing.** Every xdist worker builds
    its own config and runs this file again. Without it each of five workers would
    set `numprocesses` on itself and try to spawn five more — a fork bomb on the one
    box whose memory ceiling is the reason the cap exists at all.

    A count or a `--dist` someone typed is a decision, not a default in need of
    correcting, so injection stands down whole rather than merging with it.
    `-p no:xdist` leaves the options absent entirely, which `hasattr` catches: a run
    just told to skip the plugin must not be handed flags for it.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    if not hasattr(config.option, "numprocesses"):
        return
    if config.option.numprocesses is not None:
        return
    if getattr(config.option, "dist", "no") != "no":
        return
    flags = suite.parallel_args()
    if not flags:
        return
    for name, value in parsed_options(flags).items():
        setattr(config.option, name, value)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config) -> int | None:
    """Resolve a hand-typed `-n auto` through the memory probe, not the CPU count.

    The hook above already carries a capped count, so this covers the paths that go
    around it: someone typing `pytest -n auto` themselves, and the
    `suite.XDIST_ARGS` fallback `parallel_args()` returns when the probe has no
    opinion about this box. Dungeoneer's root conftest has the same hook for the same
    reason, and the failure it prevents was measured there (2026-08-10): xdist's own
    `auto` is the CPU count and knows nothing about memory, so on a 12-core box it
    spawned 12 workers wanting ~16.8 GB of commit against 13.4 GB of headroom.
    Overshooting commit on Windows does not fail the suite — it fails whichever
    process allocates next, so the editor and the file manager died while the tests
    carried on and Task Manager showed memory at ~52%.

    `None` means "no opinion" and hands the decision back to xdist, which is a real
    answer for a box the probe cannot read. The hook is `firstresult` and a conftest
    implementation is consulted before xdist's own, so `None` declines rather than
    forcing a number.

    `optionalhook=True` is not decoration. Without it `pytest -p no:xdist` dies on
    `PluginValidationError: unknown hook 'pytest_xdist_auto_num_workers'` before a
    single test runs — the hookspec comes from the plugin that has just been
    disabled, and pluggy rejects an implementation of a spec that does not exist.
    Measured here on 2026-09-03 while checking that disabling the plugin still
    worked; it did not.
    """
    return suite.probed_worker_count()
