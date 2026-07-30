#!/usr/bin/env python3
"""The pre-merge preflight: the one boundary where the self-improvement loop is
*mandatory* rather than advisory.

Everything else in `.ai/` is a trigger or a report — a hook that asks, a script
that counts. This is the single place that can *refuse*. It runs the checks a
session's work must pass before it leaves the branch, and it records a receipt
keyed by the commit it validated. `.ai/hooks/preflight_guard.py` (a `PreToolUse`
hook on `git push` / `git merge` / `gh pr create`) reads that receipt and denies
the operation when the commit being published has none.

**Why here and not at commit time.** Commits are cheap and frequent; a check on
every one would be tuned out within a day (`.ai/recipes/hint.py`'s own lesson).
The merge/push boundary is rare, deterministic to detect, and it is the last
moment a session's lessons still exist in context (`10_self_improvement.md` §3,
`explained-instead-of-fixed`). So the expensive checks run once, there.

**What it checks**, in cheapest-first order so a fast failure is fast:

1. `python -m nightshift.gates.run` — the whole gate suite, ~8 s.
2. `python .ai/audit.py --check` — the enforcement matrix has not drifted.
   Still a project-side script: the matrix it checks is the project's own earned
   evidence (§7 — "an *empty* audit matrix and an *empty* corrections log"), and
   `audit.py` itself does not move until step 4.
3. **A correction was recorded, or a reasoned zero was.** The branch's diff
   against the integration base must touch `.ai/corrections.log`, *or*
   `--no-corrections "<reason>"` must be given. Without the escape, the gate
   teaches you to invent entries; with it, an honest "nothing to learn here"
   is a written, dated line rather than silence (`10_self_improvement.md` §4).
4. **pytest — the slice this branch can affect, in parallel, judged by its own
   JUnit report.** Last because it is by far the most expensive, and there is no
   point running it if a gate is red.

**The pytest step reuses a prior pass when the inputs are provably identical.**
The cheap checks above (gates, audit, corrections) are the ones that fail on a
formality — a missing `--no-corrections` reason, a one-line matrix drift — and
each such failure threw away a green pytest run and forced the slowest check to
run again from scratch. So the step is now **content-addressed and per-part**: it
fingerprints the pytest inputs of each test-bearing part (`game` / `system` /
`board`) and, when a fingerprint matches a part the last run already passed, it
reuses that verdict instead of re-running the part. A part's fingerprint is a
hash of the merge-base, the environment (Python version, xdist availability) and
the *content* of every changed path that part's tests can see — so the only way
to get a hit is a byte-identical input, and the only unsafe direction (running
too few tests) cannot occur. Concretely: fixing the corrections check with
`--no-corrections` changes nothing, so **every part is reused and no pytest runs
at all**; fixing it by logging a line to `.ai/corrections.log` (a `system` path)
leaves the `game` fingerprint untouched, so **only the system tests re-run**. It
is on by default and loud — the receipt records which parts were reused and which
ran; `--fresh-tests` forces a real run, and `--full-tests` implies it.

**The pytest step runs exactly what the runner runs** (`suite`, all three
parts: `select` / `parallel_args` / `check_junit`). It did not always: this was
the last caller shelling a plain full-suite `pytest tests/ -q` and judging the
exit code, which made the *mandatory* boundary simultaneously the slowest pytest
in the project and the only one that could not tell "0 tests collected" from a
pass. Sharing the module rather than copying the flags is the point — a rule like
"0 collected is a failure" that lives in two places drifts in one.

Two things differ from the runner's use of the same module, both because the
preflight is the one caller that runs against a **live working tree** rather than
a clean worktree of a merged result:

* the changed-path set includes **uncommitted** edits (`git status --porcelain`)
  on top of the committed diff. pytest runs the tree as it is on disk, so an
  uncommitted `.ai/` edit can break a system test even when `HEAD` looks purely
  game-side. Leaving it out would narrow the slice on exactly the evidence that
  says to widen it.
* `--full-tests` forces ALL, for when you want the whole suite regardless of what
  the diff looks like. The receipt records which slice actually ran, so a
  narrowed validation is never mistaken later for a full one.

Deterministic; no LLM (`00_architecture.md` §12). It shells out to tools that
are themselves the checks — it makes no judgement of its own.

**Not standalone yet** (07_portability.md §8). `suite` and `branches` are still
project-side modules, reached through `bridge`; step 4 moves them and the bridge
goes away. `audit.py` and the project's own gates stay project-side permanently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import bridge, textio  # textio: LF-pinned writes (gate write_newline)
from nightshift.manifest import ManifestError, find_root
from nightshift.manifest import load as load_manifest

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# The receipt is per-checkout, machine-local, ephemeral state — the same
# category as `.ai/host.json` — so it is gitignored, not committed. It holds the
# last few validated commit SHAs so that validating a feature branch and then
# switching to the integration branch to merge it still resolves (the hook looks
# up the SHA being *published*, which for a merge is the source branch's tip).
RECEIPT = Path(".ai") / ".preflight"
RECEIPT_KEEP = 20
# Where the pytest run leaves its artefacts. Under `.ai/runs/`, which is already
# gitignored and is where every other run's logs go, with a leading underscore to
# match `merge_check`'s `_merge-check` — a directory that is not a card id.
RUN_DIR = Path(".ai") / "runs" / "_preflight"


def _receipt_path(root: Path) -> Path:
    """Where the receipt lives. `PREFLIGHT_RECEIPT` overrides it — the receipt is
    machine-local gitignored state, and a test that read the real one would pass or
    fail on whether *this* checkout's HEAD happened to be validated (the
    `doc-scan-resolved-against-local-disk` class of flake). The override lets a test
    point the guard at an isolated store; unset in normal use, so nothing changes."""
    override = os.environ.get("PREFLIGHT_RECEIPT")
    return Path(override) if override else root / RECEIPT


def tests_dir(root: Path) -> Path:
    """Where pytest is pointed, from the manifest. `tests/` when there is no
    manifest yet — the one place a default is right, because a repo with no
    manifest is one where nothing has been configured *at all*, and refusing to
    run the preflight there would block the very first commit that adds one."""
    try:
        return load_manifest(root).tests_path
    except ManifestError:
        return root / "tests"


def integration_base(root: Path) -> str:
    """The branch this work will merge into, from `branches.py` — never hard-coded."""
    return bridge.project_module(root, "branches", "the preflight").INTEGRATION


def _suite(root: Path):
    """`suite` — the shared select/parallelise/judge policy.

    Imported lazily, and through `bridge` because it has not been extracted yet
    (step 4). Lazily for the reason the module docstring gives: this file's job is
    to shell out to the checks, so it must not acquire an import-time dependency on
    the runner's half of the tree. `suite` itself imports nothing local.
    """
    return bridge.project_module(root, "suite", "the preflight's pytest step")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # `encoding=` is required — see the note in `.ai/gates/deletion_sweep.py`.
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def head_sha(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Result:
    checks: list[Check] = field(default_factory=list)
    # Which test slice actually ran (`suite.GAME`/`SYSTEM`/`BOARD`/`ALL`/`NONE`, or
    # `"skipped"`; `NONE` is a board-notes-only diff where no pytest applies).
    # Carried out to the receipt so a narrowed validation is never mistaken later
    # for a full one — the receipt is what unblocks a push, so what it attests to
    # has to be legible.
    tests_slice: str | None = None

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))


def _corrections_touched(root: Path, base: str) -> tuple[bool, str]:
    """Did this branch's diff against the integration base touch the log?"""
    merge_base = _git(root, "merge-base", "HEAD", base)
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        # No common ancestor resolves (a fresh clone that never fetched the
        # integration branch). Fall back to "was the log touched in HEAD's
        # commit at all" rather than silently passing — the failure direction
        # that hurts is a green light on unreviewed work.
        names = _git(root, "show", "--name-only", "--format=", "HEAD").stdout
        return (".ai/corrections.log" in names, "no merge-base; checked HEAD only")
    mb = merge_base.stdout.strip()
    names = _git(root, "diff", "--name-only", f"{mb}..HEAD").stdout
    return (".ai/corrections.log" in names, f"diff against {base} ({mb[:8]})")


def _changed_paths(root: Path, base: str) -> tuple[set[str], str]:
    """Every path this branch changes relative to `base`, committed *and* not.

    Two sources, unioned, because the preflight is the one caller of
    `suite.select` that runs pytest against a **live working tree** rather than a
    clean worktree of a merged result:

    * `merge-base..HEAD` — the branch's committed work, the same basis
      `merge_check` and the runner use.
    * `git status --porcelain` — uncommitted edits, staged or not, plus
      untracked files. pytest imports the tree as it is on disk, so an
      uncommitted `.ai/` edit really can break a system test on a branch whose
      commits are purely game-side. Selecting without it would narrow the slice
      on precisely the evidence that says to widen it.

    Failing to resolve a merge-base returns the empty set, which `suite.select`
    turns into ALL — the safe direction, and the same fallback `_corrections_touched`
    takes for the same reason (a fresh clone that never fetched the integration
    branch must not quietly validate against nothing).
    """
    changed: set[str] = set()

    merge_base = _git(root, "merge-base", "HEAD", base)
    mb = merge_base.stdout.strip() if merge_base.returncode == 0 else ""
    if mb:
        changed.update(_git(root, "diff", "--name-only", f"{mb}..HEAD").stdout.split())
        how = f"vs {base} ({mb[:8]})"
    else:
        how = f"no merge-base with {base}"

    for line in _git(root, "status", "--porcelain").stdout.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:  # a rename reports "old -> new"; the new path is what is on disk
            path = path.split(" -> ")[-1].strip()
        if path:
            changed.add(path)

    if not mb:
        return set(), how  # force ALL rather than select on the working tree alone
    return changed, how


# --- the per-part pytest reuse cache ------------------------------------------
#
# The cache lives beside the JUnit report, under the already-gitignored
# `_preflight` run dir. It is the same category of state as the receipt —
# machine-local, ephemeral, keyed by content — and never committed.
_CACHE_NAME = "pytest_cache.json"


def _cache_path(root: Path) -> Path:
    return root / RUN_DIR / _CACHE_NAME


def _load_pytest_cache(root: Path) -> dict:
    """The last run's per-part verdicts: `{part: {fp, ok, total, when}}`. A missing
    or corrupt file reads as empty — the fail-safe direction, since an empty cache
    just means nothing is reused and every target part runs."""
    path = _cache_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_pytest_cache(root: Path, cache: dict) -> None:
    path = _cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, json.dumps(cache, indent=2) + "\n")


def _env_tag(merge_base: str, suite) -> str:
    """The non-file inputs a part's pytest verdict depends on. The merge-base pins
    the baseline the slice is computed against; the interpreter version and whether
    xdist is present change *how* pytest runs (serial vs parallel), so a run under
    one must not be reused as if it were the other."""
    return (f"{merge_base}|py{sys.version_info.major}.{sys.version_info.minor}"
            f"|xdist={suite.xdist_available()}")


def _part_fingerprint(root: Path, changed: set[str], part: str, suite, env_tag: str) -> str:
    """A content hash of everything `part`'s tests can be affected by.

    The env tag, plus the path and byte-content of every changed file that
    `suite.classify` puts in this part, in the straddling `all` bucket, or in
    `other` (docs, `.claude/`, memory — folded into *every* part because a test
    may read such a file at runtime and the slice classifier does not track that).
    `note` paths (ideas/inbox) carry no tests and are excluded. A file that is
    unchanged relative to the base is not in `changed`, so its content is by
    definition identical to what any earlier cache entry saw — only changed files
    need hashing for two runs against the same tree to be compared correctly.
    """
    h = hashlib.sha256()
    h.update(env_tag.encode("utf-8"))
    h.update(b"|part=" + part.encode("utf-8"))
    for path in sorted(changed):
        if suite.classify(path, root) not in (part, suite.ALL, "other"):
            continue
        h.update(b"\0" + path.encode("utf-8") + b"\0")
        try:
            h.update((root / path).read_bytes())
        except OSError:
            h.update(b"<absent>")  # a deleted path — its absence is part of the state
    return h.hexdigest()


def _cache_hit(record: dict | None, fp: str) -> bool:
    """A part is reusable only if the last run *passed* it against this exact
    fingerprint. A failed part is never cached, so this can only ever green-light
    a re-proven pass."""
    return bool(record) and record.get("ok") is True and record.get("fp") == fp


def _run_subset(root: Path, suite, parts: frozenset, reason: str) -> tuple[bool, int, str, str]:
    """Run pytest over exactly `parts` (a non-empty part set) and judge it by its
    own JUnit report. Returns `(ok, total, why, mode)`.

    The one place a pytest subprocess is actually launched — the classic
    whole-slice run and a stale-only subset both go through here, so they select,
    parallelise and judge identically. Everything about *what* runs and *how it is
    judged* comes from `.ai/suite.py`, the same run the runner and `merge_check`
    perform (see the module docstring for why that sharing is the point).
    """
    selection = suite.Selection.for_parts(parts, reason)
    argv_paths = selection.pytest_args(tests_dir(root))

    out_dir = root / RUN_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    junit = out_dir / "junit.xml"
    # A stale report from the previous preflight must never be mistaken for this
    # run's. `check_junit` treats a missing file as a failure ("cannot confirm the
    # suite ran"), which is only true if the file is guaranteed to be *this* run's
    # — the runner gets that free from a fresh per-attempt directory; this one
    # reuses a path, so it has to clear it.
    junit.unlink(missing_ok=True)

    parallel = suite.parallel_args()
    argv = [sys.executable, "-m", "pytest", *argv_paths,
            "-q", *parallel, f"--junitxml={junit}"]
    tests = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    stdout, stderr = tests.stdout or "", tests.stderr or ""
    textio.write_text_lf(out_dir / "pytest.txt", " ".join(argv) + "\n\n" + stdout + stderr)

    ok, why = suite.check_junit(junit)
    if ok and tests.returncode != 0:
        # A clean report plus a non-zero exit is a collection or internal error —
        # the per-test XML would carry no trace of it. Same cross-check the runner makes.
        failed = [l for l in stdout.splitlines() if l.startswith("FAILED")]
        ok = False
        why = "pytest: " + ("; ".join(failed[:4]) or
                            f"exited {tests.returncode} with a clean report — "
                            "a collection or internal error")

    mode = "parallel" if parallel else "serial (pytest-xdist not installed)"
    return ok, suite.junit_total(junit), why, mode


def _pytest_detail(selection, reused: list[str], stale: list[str],
                   cache: dict, ran_total: int | None, mode: str | None) -> str:
    """The human-facing one-liner for the pytest step.

    With nothing reused it keeps the pre-cache shape (`<bucket> slice, N test(s),
    <mode> — <reason>`) so the receipt and log read exactly as before. When a part
    was reused it says so per part, with the count and time of the run it is
    trusting — a reuse is always visible, never silent.
    """
    if not reused:
        return f"{selection.bucket} slice, {ran_total} test(s), {mode} — {selection.reason}"
    frags = []
    for part in reused:
        record = cache.get(part, {})
        total = record.get("total")
        count = f"{total} test(s), " if total is not None else ""
        frags.append(f"{part} reused ({count}tree unchanged since {record.get('when', '?')})")
    if stale:
        frags.append(f"{'+'.join(stale)} ran ({ran_total} test(s), {mode})")
    return f"{selection.bucket} slice — {'; '.join(frags)} — {selection.reason}"


def _run_pytest(root: Path, base: str, full: bool, fresh: bool = False) -> tuple[bool, str, str]:
    """The pytest check: the slice this branch needs, reusing any part a prior run
    already proved against an identical tree. Returns `(ok, detail, slice)`.

    Reuse is on unless `full` (run the whole suite) or `fresh` (bypass the cache)
    is set — both force every target part to actually run. See the module
    docstring for the fingerprint contract that makes reuse provably safe.
    """
    suite = _suite(root)
    changed, how = _changed_paths(root, base)
    if full:
        selection = suite.Selection(suite.ALL, "--full-tests given")
        how = "forced (--full-tests)"
    else:
        selection = suite.select(changed, root)

    if not selection.pytest_args(tests_dir(root)):
        # `suite.NONE` — a board-notes-only diff (ideas/inbox). No pytest applies;
        # card validity is the card_schema gate's job, already run above. Report a
        # pass without inventing a 0-collected run (which `check_junit` would, and
        # should, fail), and without touching the cache. The receipt records the
        # `none` slice, so a narrowed validation is never later mistaken for a full one.
        return True, f"no tests apply — {selection.reason}", selection.bucket

    # `selection.parts` is set for every `suite.select` result; the `--full-tests`
    # path builds a bare ALL selection with no parts, which stands for game+system.
    target_parts = frozenset(selection.parts) or frozenset({suite.GAME, suite.SYSTEM})

    merge_base = _git(root, "merge-base", "HEAD", base).stdout.strip()
    # Reuse needs a merge-base. Without one, `_changed_paths` returns the empty set
    # and the selection is forced to ALL — and a fingerprint over "nothing changed"
    # would match across genuinely different trees. Fall back to always-run there,
    # the same safe direction `_changed_paths` itself takes.
    reuse = not (full or fresh) and bool(merge_base)
    env_tag = _env_tag(merge_base, suite)
    fps = {part: _part_fingerprint(root, changed, part, suite, env_tag) for part in target_parts}

    # Load even when not reusing, so an untouched part's entry is preserved rather
    # than wiped when this run repopulates only the parts it ran.
    cache = _load_pytest_cache(root)
    reused = sorted(part for part in target_parts if reuse and _cache_hit(cache.get(part), fps[part]))
    stale = sorted(target_parts - set(reused))

    ran_total = mode = None
    if stale:
        ok, ran_total, why, mode = _run_subset(root, suite, frozenset(stale),
                                               f"preflight stale part(s): {'+'.join(stale)}")
        if not ok:
            # A failed part is not cached — the tree that broke it must be re-run,
            # and a fingerprint change (the fix) is what triggers that next time.
            return False, f"{why} [{selection.bucket} slice, {how}]", selection.bucket
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        # A single-part run has an exact per-part total; a multi-part run's total is
        # the union, not attributable per part, so record None rather than a number
        # that would misreport when that part is later reused on its own.
        per_total = ran_total if len(stale) == 1 else None
        for part in stale:
            cache[part] = {"fp": fps[part], "ok": True, "total": per_total, "when": now}
        _save_pytest_cache(root, cache)

    return True, _pytest_detail(selection, reused, stale, cache, ran_total, mode), selection.bucket


def run_checks(root: Path, base: str, no_corrections: str | None,
               skip_tests: bool = False, full_tests: bool = False,
               fresh_tests: bool = False) -> Result:
    result = Result()

    gates = subprocess.run([sys.executable, "-m", "nightshift.gates.run"],
                           cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    result.add("gates", gates.returncode == 0,
               gates.stdout.strip().splitlines()[-1] if gates.stdout.strip() else "")

    audit = subprocess.run([sys.executable, str(root / ".ai" / "audit.py"), "--check"],
                           cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    result.add("audit-matrix", audit.returncode == 0,
               "matrix and tree agree" if audit.returncode == 0 else "drift — see `python .ai/audit.py`")

    if no_corrections is not None:
        result.add("corrections", True, f"explicit zero: {no_corrections}")
    else:
        touched, how = _corrections_touched(root, base)
        result.add("corrections", touched,
                   f"log touched ({how})" if touched
                   else f"no correction logged on this branch and no --no-corrections reason ({how})")

    if skip_tests:
        result.add("pytest", True, "SKIPPED (--skip-tests)")
        result.tests_slice = "skipped"
    else:
        ok, detail, bucket = _run_pytest(root, base, full_tests, fresh_tests)
        result.add("pytest", ok, detail)
        result.tests_slice = bucket

    return result


def _load_receipt(root: Path) -> list[dict]:
    path = _receipt_path(root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def write_receipt(root: Path, sha: str, no_corrections: str | None,
                  tests_slice: str | None = None, tests_detail: str | None = None) -> None:
    """Record that `sha` passed. `tests_slice` names the slice pytest actually ran;
    `tests_detail` is the human line, which now records *which parts were reused and
    which ran* — so a push unblocked partly on a reused verdict is auditable, never
    a silent green.

    Keyword-optional rather than required: the guard hook reads only `sha`, and an
    older receipt written before slices or reuse existed stays readable.
    """
    entries = [e for e in _load_receipt(root) if e.get("sha") != sha]
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    entries.append({
        "sha": sha,
        "branch": branch,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "no_corrections": no_corrections,
        "tests": tests_slice,
        "tests_detail": tests_detail,
    })
    textio.write_text_lf(_receipt_path(root), json.dumps(entries[-RECEIPT_KEEP:], indent=2) + "\n")


def is_validated(root: Path, sha: str) -> bool:
    """Does a receipt exist for exactly this commit? Used by the guard hook."""
    return any(e.get("sha") == sha for e in _load_receipt(root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-merge preflight (10_self_improvement.md §4).")
    parser.add_argument("--no-corrections", metavar="REASON",
                        help="record an explicit, reasoned zero when this branch has no "
                             "lesson worth logging — the honest alternative to inventing one")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the pytest run (for iterating on the other checks; the "
                             "receipt still records that tests were skipped)")
    parser.add_argument("--full-tests", action="store_true",
                        help="run the whole suite instead of the slice this branch's diff "
                             "selects — the escape hatch for when you want everything "
                             "(implies --fresh-tests: every part actually runs)")
    parser.add_argument("--fresh-tests", action="store_true",
                        help="bypass the per-part pytest reuse cache and re-run the selected "
                             "slice from scratch, even if a prior preflight already validated "
                             "an identical tree")
    parser.add_argument("--base", help="integration branch to diff against (default: branches.INTEGRATION)")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to check (default: found from the working directory)")
    args = parser.parse_args(argv)

    # Not `Path(__file__).parent.parent` any more: installed into site-packages
    # that answer is a directory inside the virtualenv. `find_root` raises rather
    # than falling back to the working directory, because the silent direction here
    # is validating an empty tree, finding nothing wrong, and writing a receipt
    # that unblocks the push.
    root = (args.root or find_root()).resolve()
    base = args.base or integration_base(root)
    sha = head_sha(root)
    print(f"preflight for {sha[:8]} against {base}\n")

    result = run_checks(root, base, args.no_corrections, args.skip_tests,
                        args.full_tests, args.fresh_tests)
    for check in result.checks:
        mark = "OK  " if check.ok else "FAIL"
        print(f"  [{mark}] {check.name:<12} {check.detail}")

    if result.ok:
        pytest_detail = next((c.detail for c in result.checks if c.name == "pytest"), None)
        write_receipt(root, sha, args.no_corrections, result.tests_slice, pytest_detail)
        print(f"\npreflight passed — receipt written for {sha[:8]}. "
              f"push / merge / PR is unblocked for this commit.")
        return 0
    print("\npreflight FAILED — no receipt written. Fix the above and re-run; "
          "the push/merge guard will refuse until it passes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
