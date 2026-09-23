"""Gates and tests, run over a tree: the one place either is invoked, and the
one place a JUnit report or a gates JSON payload is read back.

`_run_gates`/`_run_tests` are the two subprocess calls (`GATE_ARGV` and
`suite`'s pytest argv); `_gate_violations_json`/`_check_junit`/
`_failing_test_ids` parse what they wrote; `_is_repo_drift`/
`_gates_failing_on_base`/`_already_failing_on_base` are the "was this already
broken on `base`" checks dispatch and review both need before blaming a card
for drift that predates it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from nightshift import git, suite, textio
from nightshift import worktree

# `_run_gates` outcomes. Three, not two, because "the gates said no" and "the
# gate harness fell over" are different facts about different things, and
# collapsing them is what blamed `ice-damage` for `deletion_sweep` crashing on
# 2026-07-23. A violation is about the card; a crash is about this machine, and
# no card can be judged until it is fixed.
GATE_PASS, GATE_VIOLATION, GATE_CRASH = "pass", "violation", "crash"

# How the gate suite is invoked. `-m`, not a path into `.ai/gates/`: step 3 of the
# extraction moved the gate runner into the installed `nightshift` package, so the
# old `<worktree>/.ai/gates/run.py` no longer exists and the module form is the one
# that resolves from any worktree — the same argv `nightshift.preflight` uses.
#
# A module-level constant rather than a literal inside `_run_gates` because the
# tests need to substitute a scripted harness. They used to do it by writing a stub
# to `.ai/gates/run.py` in the fixture repo — which is exactly why the suite stayed
# green while every real dispatch died here: the fixture created the file the
# extraction had deleted, so the tests never exercised the broken path.
GATE_ARGV: list[str] = [sys.executable, "-m", "nightshift.gates.run"]


def _run_gates(root: Path, cwd: Path, log: Path) -> tuple[str, str]:
    out = subprocess.run(GATE_ARGV, cwd=cwd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    stdout, stderr = out.stdout or "", out.stderr or ""
    textio.write_text_lf(log, stdout + stderr)
    if out.returncode == 0:
        return GATE_PASS, ""

    # The gate runner exits 1 both for "violations found" and for an uncaught exception
    # inside a gate, so the exit code alone cannot tell them apart. The
    # traceback can — and it is on **stderr**, which the previous version of this
    # function never read, so a crash produced the reason `"gates: "` with
    # nothing after it and the card carried that into its `## Error`.
    if "Traceback (most recent call last)" in stderr:
        tail = [line.strip() for line in stderr.splitlines() if line.strip()]
        return GATE_CRASH, ("the gate harness crashed, so no card can be judged on this "
                            f"machine — {tail[-1] if tail else f'exit {out.returncode}'}")

    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        # Non-zero with nothing said. Whatever this is, it is not a verdict about
        # the card, and guessing that it is spends an attempt for free.
        return GATE_CRASH, (f"the gate harness exited {out.returncode} and reported "
                            f"nothing at all — see {log.name}")
    return GATE_VIOLATION, "gates: " + "; ".join(lines[:4])


def _gate_violations_json(cwd: Path) -> list[dict] | None:
    """A second, structured read of the same gate run `_run_gates` just did —
    `nightshift.gates.run --json`'s `file`/`line`/`rule` per violation, for the
    blocked-vs-failed classifier below. `None` when the payload does not parse
    (an older or scripted gate harness that does not understand `--json`, or a
    crash between the two calls): the caller must treat that exactly like a
    violation with no usable path, never as license to guess.

    A second subprocess call rather than threading a fourth return value through
    `_run_gates` and every one of its callers: gates are deterministic Python
    with no LLM (00_architecture.md SS12), so re-running them is cheap, and it
    keeps `_run_gates`'s existing contract — and `gates.txt`'s human-readable
    text — untouched for the rebase-and-merge caller that has no use for paths.
    """
    out = subprocess.run([*GATE_ARGV, "--json"], cwd=cwd, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    try:
        data = json.loads(out.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    violations = data.get("violations") if isinstance(data, dict) else None
    return violations if isinstance(violations, list) else None


def _is_repo_drift(violations: list[dict] | None, changed: set[str]) -> bool:
    """Whether a gate violation is a fact about the repo rather than about this
    attempt's own diff — every violation names a path, and every one of those
    paths lies outside `changed` (`git diff --name-only base...branch`).

    `False` — never drift, so the caller falls back to `failed` — when the
    payload did not parse (`violations is None`) or when *any* violation is
    missing a usable path or names a path this attempt actually touched.
    Unparseable or ambiguous must never buy a free attempt (Acceptance); one
    violation inside the diff is enough to make this the card's own problem,
    even if every other violation points elsewhere.
    """
    if not violations:
        return False
    for v in violations:
        path = str(v.get("file", "")).strip()
        if not path or path in changed:
            return False
    return True


# How the runner parallelises pytest — `suite.parallel_args()`, which resolves to
# `--dist loadfile` with either `-n auto` or a memory-capped worker count, and to
# `()` when pytest-xdist is missing (see that function for why a missing optional
# plugin must degrade rather than fail the run, and `suite.worker_count` for why
# `auto` alone can exhaust RAM on a many-core box and report it as two dozen
# unrelated game-test failures). Resolved once at import and kept as a named
# module-level tuple, so a test can still blank it to skip xdist's startup when
# it is running a one-test fixture.
_PYTEST_PARALLEL: tuple[str, ...] = suite.parallel_args()


def _check_junit(path: Path) -> tuple[bool, str]:
    """The JUnit verdict — `suite.check_junit`, shared with the preflight.

    A one-line delegation rather than a second copy: the preflight judges its own
    pytest run the same way, and "0 collected is a failure" is exactly the rule
    that must not drift between two callers.
    """
    return suite.check_junit(path)


def _run_tests(cwd: Path, log: Path, timeout: int, junit: Path,
               pytest_args: list[str]) -> tuple[bool, str, str]:
    """Run the selected slice of the suite over the worktree and judge it by the
    JUnit report it writes.

    `pytest_args` is `suite.Selection.pytest_args` — the game/system slice this
    diff needs. `--dist loadfile` runs the slice across cores **by file**:
    every test in a file stays on one worker, so a file's module-level state and
    in-file ordering are preserved (the order-dependent game tests break under the
    default `--dist load`, which splits a file across workers; `loadfile` is the
    structural version of "the conflicting tests still run serially"). The verdict
    comes from `_check_junit`, not the exit code — though a non-zero exit with a
    clean-looking report is still failed, because that is a collection/internal
    error the per-test XML would not carry.

    An **empty** `pytest_args` is the `suite.NONE` selection — a board-notes-only
    diff (`ideas/`/`inbox/`), whose validity is a gate's job, not pytest's. It is a
    pass with no run: running pytest with no path argument would collect the whole
    tree from cwd, the opposite of the intent. See `suite.Selection.pytest_args`.

    Returns `(ok, why, evidence)`. `evidence` is `suite.failure_excerpt`'s block —
    which failing tests and which assertions — and it is empty on a pass. It is
    returned rather than left in `junit.xml` because `junit.xml` lives under
    `.ai/runs/`, which does not survive the card being retired and never leaves
    this host at all; the card is the artefact that does both.
    """
    if not pytest_args:
        textio.write_text_lf(
            log,
            "no pytest applies to this diff (board notes only — the " "card_schema gate is the check)\n")
        return True, "", ""
    argv = [sys.executable, "-m", "pytest", *pytest_args, "-q",
            *_PYTEST_PARALLEL, f"--junitxml={junit}"]
    out = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                         encoding="utf-8", errors="replace")
    stdout, stderr = out.stdout or "", out.stderr or ""
    textio.write_text_lf(log, " ".join(argv) + "\n\n" + stdout + stderr)
    ok, why = _check_junit(junit)
    if ok and out.returncode != 0:
        # A clean-looking report with a non-zero exit: a collection or internal
        # error, which the per-test XML cannot carry. pytest's own `FAILED` lines
        # on stdout are then the only evidence there is, so they become the block.
        failed = [line for line in stdout.splitlines() if line.startswith("FAILED")]
        why = "pytest: " + ("; ".join(failed[:4]) or f"exited {out.returncode} with a "
                            "clean report — a collection or internal error")
        return False, why, "\n".join(f"    {line}" for line in failed[:suite.EXCERPT_TESTS])
    return ok, why, ("" if ok else suite.failure_excerpt(junit))


def _failing_test_ids(junit: Path) -> list[str]:
    """The pytest node ids the report says failed, deduplicated, order kept."""
    seen: list[str] = []
    for failure in suite.junit_failures(junit):
        node = str(failure.get("test", "")).strip()
        if node and "::" in node and node not in seen:
            seen.append(node)
    return seen


def _gates_failing_on_base(root: Path, base: str, violations: list[dict],
                           label: str) -> str:
    """Do this attempt's gate violations reproduce on `base`? The reason, or `""`.

    The other half of the symmetry `_already_failing_on_base` began. `_is_repo_drift`
    asks only whether a violating path lies outside the attempt's diff, and answers
    yes in two situations that are not the same thing:

    * the repo really has drifted — someone's merge, or a sibling checkout's change,
      broke a gate on the integration branch itself; and
    * **the attempt's own tree is behind.** The gate is unhappy about a file the card
      never touched, but only because that file is an old copy. `base` is green.

    Both used to stop the night with "fix it on `base` and re-run", and for the second
    that instruction is unfollowable — there is nothing on `base` to fix. On
    2026-09-03 `menu-art-start-run` resumed a branch forked 989 commits earlier, ran
    that branch's August copy of the gate suite (42 gates, against today's 50) over
    its August memory docs, and produced nine violations naming files nightshift had
    since deleted. The night stopped 23 minutes after the usage-limit window reopened,
    pointing at an integration branch that was clean.

    `prepare_worktree` now replays a resumed branch onto `base` before the attempt
    runs, which removes the cause; this removes the *misdiagnosis*, and does it by
    measurement rather than by trusting that fix to be complete. Same shape as the
    pytest half: cut a detached worktree at `base`, run the gates there, and compare.
    Only violations that reproduce make it drift.

    Returns the reason when they reproduce — so the caller keeps today's behaviour of
    giving the attempt back and stopping the night — and `""` when `base` is clean,
    which sends the card down the ordinary `failed` path with its own gate output on
    it. `""` on any inability to answer (no worktree, unparseable payload): guessing
    "drift" would hand out a free attempt, and the pytest half made the same choice.
    """
    want = {(str(v.get("file", "")).strip(), str(v.get("rule", "")).strip())
            for v in violations if str(v.get("file", "")).strip()}
    if not want:
        return ""
    tree = worktree.worktree_root(root) / f"_gatebase-{label}"
    if tree.exists() or worktree._worktree_registered(root, tree):
        git.run(root, "worktree", "remove", "--force", str(tree))
    git.run(root, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = worktree._worktree_add(root, "--detach", str(tree), base)
    except worktree.WorktreePathTooLong:
        return ""
    if made.returncode != 0:
        return ""
    try:
        on_base = _gate_violations_json(tree)
        if on_base is None:
            return ""
        got = {(str(v.get("file", "")).strip(), str(v.get("rule", "")).strip())
               for v in on_base if str(v.get("file", "")).strip()}
        # Rule as well as path. A gate suite that has grown since the branch forked
        # can report a *different* rule against the same file, and calling that the
        # same violation is what would let a genuinely stale tree keep passing itself
        # off as repo drift.
        shared = sorted(want & got)
        if not shared:
            return ""
        shown = ", ".join(f"{path} ({rule})" if rule else path
                          for path, rule in shared[:3])
        more = f" (+{len(shared) - 3} more)" if len(shared) > 3 else ""
        return (f"{len(shared)} of the {len(want)} violation(s) fail on `{base}` "
                f"too: {shown}{more}")
    finally:
        git.run(root, "worktree", "remove", "--force", str(tree))
        git.run(root, "worktree", "prune")


def _already_failing_on_base(root: Path, base: str, junit: Path, label: str,
                             timeout: int) -> str:
    """Do this attempt's failing tests fail on `base` too? The reason, or `""`.

    The pytest half of `_is_repo_drift`, and it exists because there was none.
    A gate violation names a path, so "is this about the diff?" is a set
    membership test; a test failure names a *test*, and the same question can
    only be answered by running it somewhere else. Until this, every red test was
    the card's fault by construction — which on 2026-08-25 filed
    `catalog-registry-and-guards` to `failed/` for a `SCREEN_WIDTH`/`HEIGHT` leak
    it had not written, and then let two more cards spend an attempt each on the
    identical assertion before the consecutive-failure breaker ended the run.

    Only the failing node ids are re-run, and only those whose file exists on
    `base` — a test the card itself added cannot have a baseline, and treating a
    missing file as "fails on base too" would hand a free attempt to every card
    whose new test is simply wrong.

    **What this does not catch: an order-dependent failure.** The 2026-08-25 leak
    was one — the test failed only when a *different* file had run earlier in the
    same xdist worker — so re-running it alone on `base` passes and this returns
    `""`. That case is caught by the cross-dispatch repeat check in `run()`
    instead, which needs no re-run at all. The two are complementary and neither
    subsumes the other: this one protects the *first* card, the other one
    recognises the pattern.
    """
    nodes = _failing_test_ids(junit)
    # Which of them even exist on `base`, asked of git rather than of a checkout:
    # the common case is a card that broke a test it also wrote, and cutting a
    # worktree to discover there is nothing to compare would pay the expensive
    # half of this check on every ordinary red run.
    on_base = [n for n in nodes
               if git.run(root, "cat-file", "-e",
                       f"{base}:{n.split('::', 1)[0]}").returncode == 0]
    if not on_base:
        return ""
    tree = worktree.worktree_root(root) / f"_baseline-{label}"
    if tree.exists() or worktree._worktree_registered(root, tree):
        git.run(root, "worktree", "remove", "--force", str(tree))
    git.run(root, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = worktree._worktree_add(root, "--detach", str(tree), base)
    except worktree.WorktreePathTooLong:
        return ""
    if made.returncode != 0:
        return ""
    try:
        report = tree / ".baseline-junit.xml"
        argv = [sys.executable, "-m", "pytest", *on_base, "-q",
                f"--junitxml={report}"]
        try:
            # Serial on purpose. The set is small, and xdist would reintroduce
            # the very cross-file ordering effects this comparison is trying to
            # hold constant between the two runs.
            #
            # gate-ok(subprocess_result_checked): a non-zero exit is the EXPECTED
            # outcome here — it is what pytest returns when tests fail, which is
            # the hypothesis being tested. The verdict comes from the JUnit report
            # below, the same way `_run_tests` judges the real run, and a run that
            # died before writing one leaves `still_red` empty and returns "",
            # which blames nobody.
            subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return ""
        still_red = _failing_test_ids(report)
        if not still_red:
            return ""
        shown = ", ".join(still_red[:3])
        more = f" (+{len(still_red) - 3} more)" if len(still_red) > 3 else ""
        return (f"{len(still_red)} of the {len(on_base)} failing test(s) re-run "
                f"fail on `{base}` too: {shown}{more}")
    finally:
        git.run(root, "worktree", "remove", "--force", str(tree))
        git.run(root, "worktree", "prune")
