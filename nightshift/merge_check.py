"""The mechanical half of review — does this card's branch actually merge?

Written 2026-07-24, the same night the runner's first real dispatch produced
two branches in `review/`/`testing/` that both needed a human (Claude) to merge
by hand. Doing that by hand for the second one found a real merge conflict —
`ai/board-parser-convergence` and the runner's own fixes both touched
`board.cards()` — and the reviewer had to check `git merge-tree` before trusting
either branch. That check is exactly the kind of thing `00_architecture.md` §12
asks to be a file-state lookup rather than a judgment call: **does branch X
merge cleanly onto today's integration branch, and do the gates and the test
suite still pass on the result?**

None of that is a review in the sense `03_board.md` §3's `review/` lane means —
whether the judgment calls were right, whether the acceptance criteria were
actually met in spirit, whether a docstring's confident sentence matches what
the code does. Those needed a human or an LLM reading the diff, which is what
happened for both branches merged tonight. What this script replaces is only
the part that was already mechanical: cutting a throwaway worktree, merging,
and re-running the suite — busywork that scales badly once `review/` holds more
than one or two cards, and the exact thing a stale answer ("it was green when
it was dispatched") gets wrong the moment two branches land the same night.

**Read-only with respect to the real repository.** Every check happens in a
detached worktree outside the repo (same reasoning as
`runner.worktree_root` — a worktree inside `Board/`'s tree would be walked by
`doc_scan`/`card_schema` and every card would appear twice); it is torn down
whether the merge succeeds or fails, and nothing is ever pushed, committed to
`{base}`, or left checked out. Running this can be done at any time, on a dirty
working tree, mid-dispatch, from a second terminal — it never touches the
primary checkout at all.

No LLM anywhere in this file, same rule as the gates and the runner (§12):
every branch below is a git exit code or a file read.

Usage (from anywhere inside the repo):
    python -m nightshift.merge_check                    # every branch in review/ + testing/
    python -m nightshift.merge_check --card ice-damage   # just one card, any lane
    python -m nightshift.merge_check --lane review       # only one lane
    python -m nightshift.merge_check --skip-tests        # gates only, faster

**The least portable module in this package, and the one that says so.**
07_portability.md §1 measured coupling as *project-specific content* — named
constants — and by that measure this file scores zero, which is why §8 puts it in
step 2. What that measure could not see is that this module is almost entirely
*calls into other modules* — `board`, `branches`, `runner`, `suite`. All four
were project-side when this was written and reached through a `bridge`; step 4
moved every one of them into this package, so the seam is gone and the imports
are plain.
"""
from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nightshift import board, branches, gitmerge, gitpaths, runner, suite
from nightshift.manifest import find_root

# Every possible outcome of one branch's check. Deliberately more than
# pass/fail: "why" a branch is not mergeable-and-green is the whole point of
# running this before spending a human's attention on it.
CLEAN = "clean"                  # merges with no conflict, gates green, tests green (or skipped)
CONFLICT = "conflict"            # the merge itself does not apply cleanly
GATE_CRASH = "gate_crash"        # the gate harness fell over — see runner.GATE_CRASH
GATE_VIOLATION = "gate_violation"
TESTS_FAILED = "tests_failed"
NO_BRANCH = "no_branch"          # the card carries no `branch:` field to check
BRANCH_MISSING = "branch_missing"  # `branch:` names a ref that no longer exists
CHECKOUT_FAILED = "checkout_failed"  # could not even check out `base`

_LANES: tuple[str, ...] = ("review", "testing")


# --- the four project modules this file is made of (see the module docstring) ---
#
@dataclass(frozen=True)
class MergeCheck:
    card_id: str
    branch: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == CLEAN


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    # `encoding=` is required under `.ai/` — see `subprocess_encoding` and the
    # note in `.ai/gates/deletion_sweep.py._git` for the night this was learned.
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _check_root(root: Path) -> Path:
    """Where this script's own throwaway worktrees live — a sibling of the
    runner's dispatch worktrees, never inside them, so a card actively being
    dispatched and the same card being merge-checked cannot collide."""
    path = runner.worktree_root(root) / "_merge-check"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_dir(root: Path, card_id: str) -> Path:
    path = root / runner.RUNS / "_merge-check" / card_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _conflicted_paths(entries: list[tuple[str, str]]) -> list[str]:
    """Paths `gitpaths.status` marks as unmerged. `UU`/`AA`/`DD` are the two-sided
    conflict codes; `AU`/`UA`/`DU`/`UD` are the one-sided ones (added or deleted on
    only one side) — all of them need a human, so all are listed.

    Takes the parsed entries rather than the raw text: a path with a space in it
    read out of `--porcelain` by hand is the defect `gitpaths` exists for, and a
    conflict report that names half a filename is a report nobody can act on."""
    unmerged = {"UU", "AA", "DD", "AU", "UA", "DU", "UD"}
    return [path for code, path in entries if code in unmerged]


def check_branch(root: Path, card_id: str, branch: str, base: str,
                 test_timeout: int = 600, skip_tests: bool = False) -> MergeCheck:
    """Does `branch` merge onto today's tip of `base` clean, gated, tested?

    Every exit path removes the worktree it made, success or failure alike —
    this is a question, not a step, and must leave no trace either way.
    """
    if _git(root, "rev-parse", "--verify", branch).returncode != 0:
        return MergeCheck(card_id, branch, BRANCH_MISSING,
                          f"`branch: {branch}` is on the card but the ref no longer exists")

    tree = _check_root(root) / card_id
    if tree.exists():
        _git(root, "worktree", "remove", "--force", str(tree))
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = runner._worktree_add(root, "--detach", str(tree), base)
    except runner.WorktreePathTooLong as exc:
        return MergeCheck(card_id, branch, CHECKOUT_FAILED, str(exc))
    if made.returncode != 0:
        return MergeCheck(card_id, branch, CHECKOUT_FAILED,
                          (made.stderr or made.stdout or "").strip()[:200])

    try:
        merged = _git(tree, "merge", *gitmerge.STRATEGY_ARGS, "--no-commit", "--no-ff",
                      branch)
        if merged.returncode != 0:
            conflicts = _conflicted_paths(gitpaths.status(tree))
            why = gitmerge.failure_detail(merged)
            _git(tree, "merge", "--abort")
            # Both halves: which paths, and what git said. A merge can fail with *no*
            # unmerged paths — refused before it started — and reporting only the
            # (then empty) conflict list is how a real cause goes unrecorded.
            return MergeCheck(card_id, branch, CONFLICT,
                              f"conflict in {', '.join(conflicts) or '(no unmerged paths)'}"
                              f" — {why}")

        out_dir = _run_dir(root, card_id)
        gate_status, gate_why = runner._run_gates(root, tree, out_dir / "gates.txt")
        if gate_status == runner.GATE_CRASH:
            return MergeCheck(card_id, branch, GATE_CRASH, gate_why)
        if gate_status != runner.GATE_PASS:
            return MergeCheck(card_id, branch, GATE_VIOLATION, gate_why)

        if skip_tests:
            return MergeCheck(card_id, branch, CLEAN,
                              "merges clean, gates green (tests skipped)")

        # Run only the slice this branch can affect, judged by the JUnit report —
        # the same selection the runner uses (runner-test-selection). Computed from
        # the branch's own file changes since it forked, not the merged tree, so a
        # game-only card skips the ~500 system tests it cannot touch.
        changed = set(gitpaths.changed(root, f"{base}...{branch}"))
        # Classified against `tree` (the merged result pytest will run), not `root`
        # — a changed test file's slice depends on what it imports.
        selection = suite.select(changed, tree)
        try:
            # The excerpt is dropped here: this is the interactive review helper, so
            # its reader is a terminal the maintainer is already looking at, not a
            # card that has to survive being read on another machine. `why` names the
            # first failing test (`suite.check_junit`), and the full log is in
            # `out_dir` on this box, where they are standing.
            ok, why, _ = runner._run_tests(tree, out_dir / "pytest.txt", test_timeout,
                                           out_dir / "junit.xml",
                                           selection.pytest_args(tree / suite.tests_rel(root)))
        except subprocess.TimeoutExpired:
            ok, why = False, f"pytest: timed out after {test_timeout}s"
        if not ok:
            return MergeCheck(card_id, branch, TESTS_FAILED, why)

        return MergeCheck(card_id, branch, CLEAN, "merges clean, gates green, tests green")
    finally:
        # Every path through this function leaves the worktree behind unless
        # this runs — including the two `return`s above it, which is why it is
        # a `finally` rather than repeated at each exit.
        runner.drop_worktree(root, tree)


def report(root: Path, lanes: tuple[str, ...] = _LANES, base: str | None = None,
          test_timeout: int = 600, skip_tests: bool = False,
          only: str | None = None) -> list[MergeCheck]:
    """Every card with a `branch:` in the given lanes, checked. `only` narrows
    to one card id, searched across the given lanes rather than just one — the
    same "a name is a specific request" reasoning `runner.resolve_named` uses."""
    base = base or branches.integration(root)
    results: list[MergeCheck] = []
    for lane in lanes:
        for card in board.cards(root, lane):
            if only is not None and card.id != only:
                continue
            branch = card.fields.get("branch", "")
            if not branch:
                results.append(MergeCheck(card.id, "", NO_BRANCH,
                                          "no `branch:` field on this card"))
                continue
            results.append(check_branch(root, card.id, branch, base,
                                        test_timeout, skip_tests))
    return results


def _print_report(results: list[MergeCheck]) -> int:
    if not results:
        print("nothing to check")
        return 0
    width = max(len(r.card_id) for r in results)
    bad = 0
    for r in results:
        mark = "OK  " if r.ok else "FAIL"
        if not r.ok:
            bad += 1
        print(f"{mark}  {r.card_id:<{width}}  {r.status:<16} {r.detail[:110]}")
    print(f"\n{len(results)} branch(es) checked, {len(results) - bad} clean, {bad} not")
    return 1 if bad else 0


def _parser(root: Path) -> argparse.ArgumentParser:
    """Takes the root because two of its own options are read out of the project:
    the lane names it will accept and the default base it prints in `--help`."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lane", action="append", choices=list(board.LANES),
                        help="lane(s) to check (default: review, testing)")
    parser.add_argument("--card", help="check only this card id, in any of the given lanes")
    parser.add_argument("--base",
                        help=f"branch to merge onto (default: {branches.integration(root)})")
    parser.add_argument("--test-timeout", type=int, default=600,
                        help="seconds allowed for the test suite (default 600)")
    parser.add_argument("--skip-tests", action="store_true",
                        help="stop after the merge and the gates; skip pytest")
    return parser


def main(argv: list[str] | None = None) -> int:
    root = find_root().resolve()
    args = _parser(root).parse_args(argv)
    lanes = tuple(args.lane) if args.lane else _LANES
    results = report(root, lanes, args.base, args.test_timeout, args.skip_tests, args.card)
    return _print_report(results)


if __name__ == "__main__":
    raise SystemExit(main())
