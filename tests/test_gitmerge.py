"""Tests for `nightshift/gitmerge.py` — the merge policy both merge callers share.

The reporting half exists because of a real, undiagnosable failure. On 2026-08-01 a
Dungeoneer card was reviewed `ok`, rebased, re-verified green (31 gates, 1386 tests) and
then would not merge. The only record anywhere — in the run log, the card's `## Merge`
note and the digest — was `"Merge with strategy ort failed."`, which says a merge failed.
That was already known. The cause was never written down, so the failure could not be
investigated at all.

The code had selected for exactly that: `detail[-1]` of `(stdout or stderr)`. Git prints
the specific cause *first* and a generic summary *last*, and writes errors to **stderr**
while `or` stops at a non-empty stdout. Both halves of that are asserted below, with real
git output as the fixtures.
"""
from __future__ import annotations

import subprocess

from nightshift import gitmerge


def _result(stdout: str = "", stderr: str = "", code: int = 1):
    return subprocess.CompletedProcess(["git", "merge"], code, stdout, stderr)


# --- strategy ------------------------------------------------------------------


def test_renormalize_is_the_strategy():
    """Line endings are the one noisy conflict class that can be dissolved without
    judgment: `.gitattributes` decides, both sides are normalized, the content compared
    is identical."""
    assert gitmerge.STRATEGY_ARGS == ("-Xrenormalize",)


def test_whitespace_is_deliberately_not_ignored():
    """`-Xignore-space-change` would dissolve the other noisy class and must not be
    added: in Python, indentation is syntax, so a whitespace-blind merge can reparent a
    block into the wrong `if` and still compile. That is the semantic conflict human
    review exists to catch — a flag must not hide it. Asserted so the reasoning has to be
    re-read before anyone adds it."""
    assert not any("space" in arg for arg in gitmerge.STRATEGY_ARGS)


# --- reporting -----------------------------------------------------------------


def test_the_cause_survives_and_not_only_the_summary():
    """The 2026-08-01 shape, verbatim: git names the blocking file first and ends with
    the generic line the old code kept."""
    result = _result(stderr=(
        "error: Your local changes to the following files would be overwritten by merge:\n"
        "\tBoard/tasks/hack-end-summary.md\n"
        "Please commit your changes or stash them before you merge.\n"
        "Merge with strategy ort failed.\n"))
    detail = gitmerge.failure_detail(result)
    assert "would be overwritten by merge" in detail
    assert "hack-end-summary.md" in detail
    assert not detail.startswith("Merge with strategy")


def test_stderr_is_read_even_when_stdout_is_non_empty():
    """`stdout or stderr` discards the diagnosis whenever a merge printed any progress
    at all, which is the common case."""
    result = _result(stdout="Auto-merging dungeoneer/core/i18n.py\n",
                     stderr="CONFLICT (content): Merge conflict in dungeoneer/core/i18n.py\n")
    assert "CONFLICT (content)" in gitmerge.failure_detail(result)


def test_conflict_paths_are_named():
    result = _result(stdout=("Auto-merging a.py\n"
                             "CONFLICT (content): Merge conflict in a.py\n"
                             "Automatic merge failed; fix conflicts and then commit the result.\n"))
    detail = gitmerge.failure_detail(result)
    assert "Merge conflict in a.py" in detail
    assert "Automatic merge failed" not in detail, "the summary crowded out the cause"


def test_the_summary_survives_when_it_is_all_git_said():
    """A weak reason beats none — dropping every summary line unconditionally would
    reintroduce the empty-reason bug from the other direction."""
    result = _result(stderr="Merge with strategy ort failed.\n")
    assert "Merge with strategy ort failed." in gitmerge.failure_detail(result)


def test_silence_is_reported_as_silence():
    assert "git printed nothing" in gitmerge.failure_detail(_result())


def test_the_detail_is_bounded_and_single_line():
    """It lands in a card's `## Merge` note and in the run log, both of which are read as
    one line per failure."""
    result = _result(stderr="\n".join(f"error: line {i}" for i in range(200)))
    detail = gitmerge.failure_detail(result)
    assert "\n" not in detail
    assert len(detail) <= 400


def test_duplicate_lines_are_not_repeated():
    """git repeats itself across the two streams often enough to waste the budget."""
    same = "CONFLICT (content): Merge conflict in a.py\n"
    detail = gitmerge.failure_detail(_result(stdout=same, stderr=same))
    assert detail.count("Merge conflict in a.py") == 1
