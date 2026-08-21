#!/usr/bin/env python3
"""Per-machine preconditions: the state git cannot carry, checked where every
push already goes through.

**The gap this closes.** A gate reads the tree, a test reads the tree, a review
reads the diff — so all three are blind to anything that is true of *this
machine* rather than of the repo. Every CRLF incident this project has had is one
instance of that blind spot: a worktree full of CRLF files is invisible to `git
status` (the LF blob and the CRLF file normalise to the same content), the fix is
per-machine and produces nothing to commit, and so nothing that reads the
repository can tell a fixed box from a broken one. `claude` missing from PATH, a
hostname absent from `.ai/hosts.json`, and a `.ai/` that no longer carries the
modules the framework reaches back for are the same shape: real, silent, and
per-checkout.

`07_portability.md` §5 puts this class of check in `nightshift doctor`, alongside
`nightshift init`, and `preflight` runs them **first**, ahead of the gates. First
because these failures are the ones that disguise themselves as something else: a
CRLF worktree fails the `line_endings` gate three hundred times over, and no other
gate's signal is legible underneath that.

**`drift()` is the other half, added at step 5.** It re-runs `discover.survey()`
against a repo that already has a manifest and reports where the two disagree — a
renamed package, a moved test directory, an integration branch that no longer
exists. Cheap, because it is the same function `init` proposes from, which is the
whole reason the two must not grow separate copies: a manifest is a hand-written
config, and every hand-written config in this tree has gone stale at least once.

Drift is **reported, never fixed**, and it is not a preflight check. A branch that
was renamed on purpose is drift too, and a doctor that failed the push over it —
or worse, quietly rewrote the manifest — would be answering a question nobody
asked. `preflight` keeps running only `CHECKS`.

**Nothing here fixes anything.** Every failure names the exact command to run and
stops. A check that repaired the tree would be a check nobody reads the output of.

**Reuse over re-implementation.** Each check is a call to the predicate that
already owns the question — `gates.line_endings.check`, the project's
`runner.claude_binary`, `preflight.integration_base`. A
second implementation of "is `claude` installed" would be a second thing to keep
true.
"""
from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import nightshift
from nightshift import freshness, gitpaths, preflight, runner
from nightshift.gates import line_endings
from nightshift.manifest import AI_DIR, ManifestError
from nightshift.preflight import Check

__all__ = ["checks", "drift", "Drift", "lf_worktree", "claude_on_path", "hosts_entry",
           "preflight_config", "framework_version", "framework_freshness",
           "paired_branches", "worktree_headroom",
           "worst_relative", "worst_worktree_name", "headroom",
           "GREEN", "WARN", "FAIL"]

_HOSTS = f"{AI_DIR}/hosts.json"
_HOST_OVERRIDE = f"{AI_DIR}/host.json"

# Windows' `MAX_PATH`, 260, minus the trailing NUL every Win32 file API needs —
# see `worktree_headroom` below and `nightshift-worktree-paths-not-defensive-
# on-windows`, the card this whole block answers.
_MAX_PATH = 259
# A stated judgment, not a measurement (the card's own words): roughly one
# extra nested package directory plus a longer card id — the shape of drift
# that eats headroom without anyone noticing.
_WARN_BELOW = 30
GREEN, WARN, FAIL = "green", "warn", "fail"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    """`None` when git cannot answer at all — no binary, or `cwd` is gone. The
    callers here must survive that: a doctor that raises is worse than no doctor,
    because it takes the whole preflight down with a traceback about itself."""
    try:
        # `encoding=` is required — see the note in `.ai/gates/deletion_sweep.py`.
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None


def lf_worktree(root: Path) -> Check:
    """No CRLF in the index or the working tree — `gates.line_endings`' own answer.

    The gate suite already runs this predicate, so nothing new is *detected* here.
    What is new is *where*: as one line at the top of the preflight rather than as
    one violation per file several hundred lines down. The 2026-07-30 incident had
    304 of them.
    """
    violations = line_endings.check(root)
    if not violations:
        return Check("lf-worktree", True, "index and working tree are LF")
    return Check("lf-worktree", False,
                 f"{len(violations)} line-ending violation(s) — first: {violations[0]}")


def _dispatches(root: Path) -> bool:
    """Does anything ever get dispatched in this repo?

    A board is the signal, and it is the honest one: the runner takes cards from
    `Board/<lane>/`, so a repo with no board has no cards, spawns no workers and
    needs no `claude`, no host entry and no branch to build on. The alternative —
    asking every repo those three questions — makes `nightshift`'s own checkout
    permanently red for failing to be configured as its own consumer, which is the
    kind of noise that teaches people to skim a report.
    """
    from nightshift import board

    return board.board_dir(root).is_dir()


def claude_on_path(root: Path) -> Check:
    """The runner can find the `claude` binary it dispatches every worker with.

    Through the project's own resolver rather than a second `shutil.which`: it
    already knows about `CLAUDE_BIN` and about this box's `~/.local/bin`, and two
    answers to "where is claude" is exactly the drift worth not having. A night
    that discovers this at 03:00 has already moved a card and spent an attempt.
    """
    if not _dispatches(root):
        return Check("claude-bin", True, "no board here — nothing dispatches, so the "
                                         "worker CLI is not needed", skipped=True)
    found = runner.claude_binary()
    if found:
        return Check("claude-bin", True, found)
    return Check("claude-bin", False,
                 "`claude` is not on PATH and CLAUDE_BIN is unset (or points at a "
                 "file that does not exist) — install it or set CLAUDE_BIN to the "
                 "executable; no card can dispatch without it")


def hosts_entry(root: Path) -> Check:
    """This hostname is configured in `.ai/hosts.json`.

    An *unlisted* host is not a machine with no capabilities — it is a machine
    nobody has decided about, and it fails silently in the one direction that
    looks like normal operation: cards that `requires:` anything simply never
    dispatch, anywhere. `hosts.json`' own comment records the incident
    (`_TODO-desktop-hostname`: "until it was renamed, art cards never dispatched
    anywhere"), which is why an absent entry is a failure here and not an FYI.

    Key presence, not `runner.host_config`: that resolver answers "what may this
    box do", and returns `{}` both for an unlisted host and for a host whose entry
    is empty. Only the first is a problem, so the question has to be asked of the
    file. The paths still come from the runner, so there is one home for them.
    """
    if not _dispatches(root):
        return Check("hosts-json", True, "no board here — nothing dispatches, so no "
                                         "per-machine dispatch config is needed", skipped=True)
    hostname = socket.gethostname()
    if (root / runner.HOST_FILE).is_file():
        return Check("hosts-json", True,
                     f"{_HOST_OVERRIDE} overrides hosts.json on this box")

    path = root / runner.HOSTS_FILE
    if not path.is_file():
        return Check("hosts-json", False,
                     f"{_HOSTS} does not exist — the runner has no per-machine config "
                     f"at all here, so nothing with a `requires:` can dispatch")
    try:
        hosts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Check("hosts-json", False, f"{_HOSTS} is unreadable: {exc}")
    if not isinstance(hosts, dict) or hostname not in hosts:
        return Check("hosts-json", False,
                     f"this host ({hostname}) has no entry in {_HOSTS} — add one, or "
                     f"write {_HOST_OVERRIDE} as an untracked override; until then "
                     f"every card with a `requires:` silently fails to dispatch here")
    return Check("hosts-json", True, f"{hostname} configured in {_HOSTS}")


def _pytest_version() -> str | None:
    """The installed pytest's version, or `None` when it is not installed here.

    Never hardcoded — the bytecode cache name is stamped by whichever pytest
    actually runs, not by a constant that would silently drift the day the
    project upgrades. A spec lookup rather than an import, same reasoning as
    `suite.xdist_available`: this is asked on the way to building a report, and
    paying for pytest's own import to answer would be backwards. `None` is a
    real answer, not an error — `worst_relative` falls back to the tracked
    maximum alone, which under-reports but never crashes doctor over a box that
    has not installed its dev extra yet.
    """
    try:
        return metadata.version("pytest")
    except metadata.PackageNotFoundError:
        return None


def worst_relative(root: Path) -> tuple[int, str]:
    """The longest relative path a checkout of `root` will ever have to hold —
    every tracked file, and (when pytest is installed to measure it against)
    the assertion-rewrite bytecode cache pytest builds beside every tracked
    `*.py`: `<dir>/__pycache__/<stem>.cpython-<XY>-pytest-<ver>.pyc`, built from
    the running interpreter's own cache tag and the installed pytest's own
    version — never hardcoded.

    That cache is *generated*, not tracked, so `git ls-files` alone misses it.
    On this project the gap was 11 characters (74 tracked vs. 85 generated) —
    the measurement that decided this function needs both halves, not just the
    one `git ls-files` can answer.

    Returns `(length, path)` so a report can name what is binding, not just
    how long it is.
    """
    tracked = gitpaths.tracked(root)
    if not tracked:
        return 0, "(no tracked files)"

    worst_len, worst_path = 0, tracked[0]
    for p in tracked:
        if len(p) > worst_len:
            worst_len, worst_path = len(p), p

    pytest_version = _pytest_version()
    if pytest_version is not None:
        tag = sys.implementation.cache_tag
        for p in tracked:
            if not p.endswith(".py"):
                continue
            pure = PurePosixPath(p)
            cache = str(pure.parent / "__pycache__" /
                       f"{pure.stem}.{tag}-pytest-{pytest_version}.pyc")
            if len(cache) > worst_len:
                worst_len, worst_path = len(cache), cache
    return worst_len, worst_path


def worst_worktree_name(root: Path) -> tuple[int, str]:
    """The longest worktree directory name the framework itself generates
    today: `_review-`/`_rebase-` (`runner.py`) prefixed to the longest card id
    currently on the board, and the fixed `_merge-check`
    (`merge_check._check_root`). Derived from the live board, not a constant —
    a longer card id lands as drift the next run reports rather than as a
    number nobody updates.

    `_merge-check` is compared as the literal 12-character name rather than
    `_merge-check/<card-id>` (its actual on-disk nesting, one directory
    deeper than `_review-`/`_rebase-`): the two prefixed names already carry
    the id and dominate the max at every id length seen on this board, so the
    simplification does not change the reported number here — but it does mean
    a `merge_check` cut is not the scenario this check is sized against. Noted
    on the card this answers rather than silently assumed.
    """
    from nightshift import board

    longest_id = ""
    for lane in board.LANES:
        for card in board.cards(root, lane):
            if len(card.id) > len(longest_id):
                longest_id = card.id

    candidates = {
        f"_review-{longest_id}": len(f"_review-{longest_id}"),
        f"_rebase-{longest_id}": len(f"_rebase-{longest_id}"),
        "_merge-check": len("_merge-check"),
    }
    name = max(candidates, key=candidates.get)
    return candidates[name], name


def headroom(wt_root_len: int, name_len: int, rel_len: int) -> tuple[int, str]:
    """The pure arithmetic behind `worktree_headroom`, pulled out so it is
    testable against synthetic inputs and never against an actual
    300-character path — the working `MAX_PATH` this check exists to report on
    would refuse to create one in the first place (Approach, same card).

    Slack = `259 - (wt_root_len + 1 + name_len + 1 + rel_len)`, the two `+1`s
    for the path separators `worktree_root / worktree_name / relative_path`
    actually need. Green above 30 chars of slack, warn 0-30, fail below 0.
    """
    slack = _MAX_PATH - (wt_root_len + 1 + name_len + 1 + rel_len)
    if slack < 0:
        return slack, FAIL
    if slack < _WARN_BELOW:
        return slack, WARN
    return slack, GREEN


def worktree_headroom(root: Path) -> Check:
    """How much of Windows' 260-char `MAX_PATH` a worktree cut on this
    checkout would still have left — the framework cuts one on every dispatch,
    review, merge-check and rebase, and until this check existed there was no
    warning before it failed mid-night.

    Report only, never fixed — see the module docstring. `core.longpaths` is
    deliberately not among the remedies: the 2026-08-06 sweep on
    `nightshift-worktree-paths-not-defensive-on-windows` found it makes git
    succeed and then Python fail to open a file that visibly exists
    (`FileNotFoundError`), and at greater depth git fails anyway with a worse
    message (`'$GIT_DIR' too big`) — strictly worse than doing nothing, not
    merely unrecommended. Nothing here can prevent the underlying constraint
    either (`repo_root + relative + worktree_overhead <= 259` is arithmetic,
    not a bug); the job is to say the number early, not to make it larger.

    Windows only (`os.name == "nt"`) — POSIX has no `MAX_PATH`, so it is
    reported "not applicable", never a bare `ok=True`, per the
    `hook-killed-at-timeout-read-as-clean` rule that a check which cannot run
    must not look like a check that found nothing. Skipped where nothing
    dispatches either, same reasoning as `claude_on_path`/`hosts_entry`: a repo
    with no board never cuts a worktree.
    """
    if os.name != "nt":
        return Check("worktree-headroom", True,
                     "not applicable — MAX_PATH is a Windows constraint",
                     skipped=True)
    if not _dispatches(root):
        return Check("worktree-headroom", True,
                     "no board here — nothing cuts a worktree", skipped=True)

    wt_root = runner.worktree_root(root)
    wt_root_len = len(str(wt_root))
    name_len, name = worst_worktree_name(root)
    rel_len, rel = worst_relative(root)
    slack, status = headroom(wt_root_len, name_len, rel_len)

    detail = (f"worktree root {wt_root_len} chars + worktree name {name_len} "
             f"({name!r}) + longest path {rel_len} ({rel!r}) = "
             f"{wt_root_len + 1 + name_len + 1 + rel_len}/{_MAX_PATH} chars — "
             f"{slack} of slack")
    if status == FAIL:
        return Check("worktree-headroom", False,
                     f"{detail}; a worktree cut here can already exceed "
                     f"MAX_PATH. Remedies: enable LongPathsEnabled (admin, the "
                     f"real fix), move this checkout nearer the drive root, or "
                     f"`subst`/`mklink /J` a short alias for it. Not "
                     f"`core.longpaths` — see this module's docstring.")
    if status == WARN:
        return Check("worktree-headroom", True,
                     f"{detail} — thin; one more nested package directory or "
                     f"a longer card id could tip this negative")
    return Check("worktree-headroom", True, detail)


def preflight_config(root: Path) -> Check:
    """What the preflight reads late enough to fail expensively.

    This was the `bridge` check: `preflight` reached back into a project's
    `.ai/` for `branches` and `suite`, and the first attempt at either happened
    deep inside the pytest step — after the gates and the audit matrix had
    already run — so an incomplete `.ai/` failed slowly, with an error about
    pytest. 07_portability.md §8 step 4 moved both into the package, which
    removes the *import* half of that problem and leaves the half that was
    always the real one: `[branches].integration` is the field `manifest.py`
    refuses to guess, and it is still read late.

    So the check narrows rather than disappearing, and it is named for what it
    now verifies. `bridge` is gone entirely: step 4 moved the last module it
    reached for.
    """
    if not _dispatches(root):
        return Check("preflight-config", True,
                     "no board here — nothing builds on an integration branch. Gates and "
                     "tests still run; `merge_base_candidates()` falls back to stable",
                     skipped=True)
    # `branches.integration` directly, not `preflight.integration_base`: that one
    # now falls back to `stable` so a non-dispatching repo can still preflight, and
    # a check whose subject is "is this field declared" must not be satisfied by the
    # fallback it exists to warn about.
    try:
        from nightshift import branches

        branches.integration(root)
    except ManifestError as exc:
        return Check("preflight-config", False, str(exc))
    return Check("preflight-config", True, "[branches].integration is declared")


def framework_version(root: Path, checkout: Path | None = None) -> Check:
    """Which `nightshift` this checkout is actually running. **Reports; never fails.**

    Karel's call, 2026-08-01 (`per-machine-preconditions-are-unchecked`): report
    only. The install is editable (`pip install -e ../nightshift`), so a commit in
    the framework repo is live in every consumer at once with no merge — which is
    the point while it is still changing daily, and also the failure class that
    took out a whole night's run when `nightshift:main` moved under it. A hard pin
    would catch that, and `07_portability.md` §3 is explicitly on record with the
    other plan: *"pin to a tag once it settles"*. Pinning now would be building
    against the stated policy, so what ships is the honest middle — the SHA is
    always visible, and whoever reads a broken run's output can see what moved.

    `root` is unused: the framework's version is a property of the install, not of
    the project being checked. `checkout` overrides where that install is looked
    for, for tests.
    """
    if checkout is None:
        # `nightshift/__init__.py` → the package dir → the repo it was installed
        # from. Editable installs point at a real checkout; a wheel install points
        # into site-packages, where the git lookup below simply finds nothing.
        checkout = Path(nightshift.__file__).resolve().parent.parent

    head = _git(checkout, "rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return Check("nightshift", True,
                     f"v{nightshift.__version__} at {checkout} — not a git checkout, "
                     f"so no commit to report (installed non-editable?)")
    sha = head.stdout.strip()[:8]
    dirty = gitpaths.status(checkout)
    state = f"{len(dirty)} uncommitted change(s)" if dirty else "clean"
    return Check("nightshift", True, f"{sha} ({state}) at {checkout}")


def framework_freshness(root: Path, checkout: Path | None = None) -> Check:
    """Whether the framework checkout is behind its remote. **Reports; never fails.**

    Behind on purpose is allowed — refusing a pull is a first-class answer and this
    must not become a nag — so the verdict is always `ok`. What it buys is that the
    question gets *asked*, here, where every push already passes, instead of being
    remembered. The occasion (2026-08-14): two machines, one framework checkout
    each, and nothing anywhere said which was behind.

    The fetch is read-only and cannot move a working tree, which is what makes it
    safe to do on the way to a push; the pull it names is a separate, explicit act
    (`nightshift.freshness`). Nothing here pulls, for the reason that module's
    docstring gives at length: an automatic pull does not prevent the framework
    moving under a run, it schedules it.

    `root` is unused for the same reason it is unused in `framework_version` — this
    is a property of the install, not of the project being checked.
    """
    state = freshness.read(checkout)
    return Check("nightshift-fresh", True, freshness.describe(state),
                 skipped=not state.known)


def paired_branches(root: Path, checkout: Path | None = None) -> Check:
    """The framework and this repo are on branches that belong to the same work.

    **The one freshness question that is a refusal**, and the only check here whose
    subject is neither repo but the pair of them. A framework checkout on a feature
    branch while this repo is on a different one is state *invisible from either
    side*: the suite here would be green or red depending on which branch a sibling
    directory happens to have out, and no gate in either repo can reach the other.
    Measured once at 39 failed / 198 passed (`cross-repo-half-landed-alone`).

    That is exactly what a preflight is for — the boundary where "the suite is
    green" is about to be treated as evidence — so this fails rather than reporting.
    The framework on its own default branch is always fine, and matching branch
    names are fine, because that is what a paired change looks like.

    No fetch: this is a comparison of two local HEADs and must not depend on a
    network. Skipped where the framework is not a git checkout at all.
    """
    state = freshness.read(checkout, fetch=False)
    if not state.branch:
        return Check("paired-branches", True,
                     f"framework at {state.checkout} is not a git checkout — no branch "
                     f"to pair with", skipped=True)
    here = runner.current_branch(root)
    if not here:
        return Check("paired-branches", True, "this repo reports no branch (detached?)",
                     skipped=True)
    why = freshness.unpaired(here, state)
    if why:
        return Check("paired-branches", False, why)
    return Check("paired-branches", True,
                 f"both on `{here}`" if state.branch == here
                 else f"framework on its default `{state.branch}`, this repo on `{here}`")


# The order they are reported in: cheapest and most self-explanatory first, and
# the two that need the project's `.ai/` last, so a repo the framework was just
# installed into says something useful before it says something confusing.
# `worktree_headroom` sits with `lf_worktree` — both are about the checkout's
# own shape rather than about dispatch config — ahead of the three that are.
#
# The two framework checks come last and in this order: the SHA (what is
# installed), then how fresh it is, then whether it belongs to the same work as
# this repo. Each is a strictly stronger claim than the one before, and only the
# last can fail.
CHECKS = (lf_worktree, worktree_headroom, claude_on_path, hosts_entry,
          preflight_config, framework_version, framework_freshness, paired_branches)


def checks(root: Path) -> list[Check]:
    """Every doctor check, run against `root`, in reporting order."""
    return [check(root) for check in CHECKS]


@dataclass(frozen=True)
class Drift:
    """One manifest field where what is written and what discovery finds disagree."""
    key: str
    declared: object
    found: object
    why: str


def drift(root: Path) -> list[Drift]:
    """Where `.ai/manifest.toml` and the tree have parted company.

    Compared only for the fields discovery can answer *confidently* — the
    `CONFIRM` ones are guesses by construction, and reporting "you wrote
    `development_team`, I guessed `dev`" every single run is how a report gets
    ignored. `NEVER` fields are not compared for the reason they exist.

    A field discovery cannot find (`value is None`) is not drift either: an absent
    assets directory does not mean `[worker].harvest_dirs` is wrong, it means
    discovery has nothing to say. Only a confident, different answer counts.
    """
    from nightshift import discover
    from nightshift.manifest import load

    try:
        written = load(root)
    except ManifestError as exc:
        return [Drift("manifest", None, None, f"unreadable: {exc}")]

    tables = {
        "project": {"name": written.project.name,
                    "source_dirs": list(written.project.source_dirs),
                    "doc_files": list(written.project.doc_files)},
        "tests": {"dir": written.tests.dir},
        "branches": {"stable": written.branches.stable},
        "tiers": {"binding_doc": written.tiers.binding_doc},
        "worker": {"fence_env": written.worker.fence_env,
                   "harvest_dirs": list(written.worker.harvest_dirs),
                   "integration_checkout_dir": written.worker.integration_checkout_dir},
    }

    out: list[Drift] = []
    for proposal in discover.survey(root):
        if proposal.confidence != discover.HIGH or proposal.value is None:
            continue
        table, _, field_name = proposal.key.partition(".")
        if table not in tables or field_name not in tables[table]:
            continue
        declared = tables[table][field_name]
        if declared in ("", [], None):
            continue  # not written down; `init` would add it, not correct it
        if declared != proposal.value:
            out.append(Drift(proposal.key, declared, proposal.value, proposal.why))

    # The integration branch is the one CONFIRM field worth a targeted check, and
    # the check is existence rather than agreement: a manifest naming a branch that
    # is gone stops every dispatch, and no heuristic is needed to notice that.
    # `_git` here returns the CompletedProcess even on a non-zero exit (`None` means
    # git could not run at all), so the verdict is the return code, not the object.
    integration = written.branches.integration
    verify = _git(root, "rev-parse", "--verify", f"refs/heads/{integration}") if integration else None
    if integration and verify is not None and verify.returncode != 0:
        out.append(Drift("branches.integration", integration, None,
                         "no such branch — nothing can dispatch until this is a branch "
                         "that exists, and forbidden_bases() is derived from it"))
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    from nightshift.manifest import find_root

    parser = argparse.ArgumentParser(
        prog="nightshift doctor",
        description="Per-machine preconditions, plus drift between the manifest and the tree.")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--no-drift", action="store_true",
                        help="preconditions only, the way preflight runs them")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    root = (args.root or find_root()).resolve()
    print(f"doctor for {root}\n")
    results = checks(root)
    for check in results:
        mark = "SKIP" if check.skipped else ("OK  " if check.ok else "FAIL")
        print(f"  [{mark}] {check.name:<12} {check.detail}")

    if not args.no_drift:
        found = drift(root)
        print()
        if not found:
            print("  no drift — the manifest and the tree agree")
        for item in found:
            print(f"  [DRIFT] {item.key}")
            print(f"          declared: {item.declared!r}")
            print(f"          found:    {item.found!r}  ({item.why})")
        if found:
            print("\n  Reported, not fixed. Drift can be intentional — a rename you meant.")
    return 0 if all(check.ok for check in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
