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

import json
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import nightshift
from nightshift import preflight, runner
from nightshift.gates import line_endings
from nightshift.manifest import AI_DIR, ManifestError
from nightshift.preflight import Check

__all__ = ["checks", "drift", "Drift", "lf_worktree", "claude_on_path", "hosts_entry",
           "preflight_config", "framework_version"]

_HOSTS = f"{AI_DIR}/hosts.json"
_HOST_OVERRIDE = f"{AI_DIR}/host.json"


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


def claude_on_path(root: Path) -> Check:
    """The runner can find the `claude` binary it dispatches every worker with.

    Through the project's own resolver rather than a second `shutil.which`: it
    already knows about `CLAUDE_BIN` and about this box's `~/.local/bin`, and two
    answers to "where is claude" is exactly the drift worth not having. A night
    that discovers this at 03:00 has already moved a card and spent an attempt.
    """
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
    try:
        preflight.integration_base(root)
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
    status = _git(checkout, "status", "--porcelain")
    dirty = [line for line in (status.stdout if status else "").splitlines() if line.strip()]
    state = f"{len(dirty)} uncommitted change(s)" if dirty else "clean"
    return Check("nightshift", True, f"{sha} ({state}) at {checkout}")


# The order they are reported in: cheapest and most self-explanatory first, and
# the two that need the project's `.ai/` last, so a repo the framework was just
# installed into says something useful before it says something confusing.
CHECKS = (lf_worktree, claude_on_path, hosts_entry, preflight_config, framework_version)


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
        print(f"  [{'OK  ' if check.ok else 'FAIL'}] {check.name:<12} {check.detail}")

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
