"""Is the framework checkout current, and does it belong to the same work as this repo?

Two machines, one framework checkout each, and until now nothing told you which one
was behind. The occasion, 2026-08-14: *"I now tend to forget to update nightshift
when switching computers."*

**The instinct is to make every automation pull the framework first. Do not.** The
install is editable, so a framework commit is live in every consumer the moment it
exists — there is no pull to forget in the sense that word usually has. What a
per-run auto-pull would do is *move the framework under a run that is already
using it*, which is the failure class `doctor.framework_version` names in its own
docstring: the night that died when the default branch moved beneath it. It does
not prevent that failure; it schedules it. And concretely, an auto-pull of the
default branch while a paired feature branch is checked out would check out away
from it and make half the code vanish mid-run.

So the split is between the half that is safe to do always and the half that is a
decision:

* **Fetching is read-only.** It cannot move the working tree, so it is safe to do
  on the way to answering a question, and it is the only way to *know* anything.
* **Pulling is the decision**, and it stays a human's. What ships is the number and
  the exact command; taking the offer is a separate act, and refusing it is a
  first-class answer that must not be nagged about. That is why nothing here is a
  failing check and why the report is one line.

**A pull is refused into any state that is not the routine one** — a dirty tree, a
non-default branch, or local commits the remote does not have. Those all mean the
pull is not the boring fast-forward the offer is about, and resolving them is
exactly where a helpful automation would do damage.

**The paired-branch check is the half that catches real damage.** A framework
checkout on a feature branch while the consuming repo is on a different one is
state that is *invisible from either repo*: the suite here is green or red
depending on which branch a sibling directory happens to have out, and no gate in
either repo can see it. Measured once at 39 failed / 198 passed. That one is a
refusal, not a report, because it is a fact about whether the tests just run mean
anything.

The SHA report (`doctor.framework_version`) is untouched and stays report-only by
its own 2026-08-01 call: freshness is a *new* signal beside it, not a change to it.

No LLM: a fetch, two rev-list counts and a string comparison.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import nightshift
from nightshift.manifest import ManifestError, find_root

#: Seconds a fetch may take before it is abandoned. A freshness reading is a
#: convenience on the way to something else, so a slow or absent network must cost
#: a few seconds and then be reported as unknown — never block the command that
#: asked. `describe` says "could not fetch" rather than implying it looked.
FETCH_TIMEOUT_S = 15

#: The branch names to fall back on when the framework checkout has no manifest and
#: git will not say what its remote's HEAD is. Ordered: the first that exists wins.
_DEFAULT_GUESSES = ("main", "master")


def _git(cwd: Path, *args: str,
         timeout: int | None = None) -> subprocess.CompletedProcess | None:
    """`None` when git cannot answer at all. Every caller here must survive that:
    this module is consulted on the way to a push, and a freshness reading that
    raises would take down the boundary it was trying to inform."""
    try:
        # `encoding=` is required on Windows — see the note in `doctor._git`.
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def framework_checkout() -> Path:
    """Where the installed framework actually lives.

    `nightshift/__init__.py` → the package directory → the checkout it was
    installed from. An editable install points at a real checkout; a wheel install
    points into site-packages, where the git questions below simply find nothing
    and every answer degrades to "not a git checkout". Same derivation as
    `doctor.framework_version`, so the two cannot disagree about which directory
    they are talking about.
    """
    return Path(nightshift.__file__).resolve().parent.parent


def default_branch(checkout: Path) -> str:
    """The framework's own default branch — what a routine pull is a pull *of*.

    Its manifest is asked first, because `[branches].stable` is the field that
    exists to answer this and is the same answer the framework's own preflight
    uses. Falls back to the remote's HEAD, then to the first of `main`/`master`
    that exists locally, then to `main` — a guess, but one that only ever affects
    a report and a refusal-to-pull, never a merge.
    """
    try:
        from nightshift import branches

        return branches.stable(checkout)
    except (ManifestError, Exception):        # noqa: BLE001 — a report must not raise
        pass
    head = _git(checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head is not None and head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip().split("/", 1)[-1]
    for guess in _DEFAULT_GUESSES:
        found = _git(checkout, "rev-parse", "--verify", f"refs/heads/{guess}")
        if found is not None and found.returncode == 0:
            return guess
    return _DEFAULT_GUESSES[0]


@dataclass(frozen=True)
class Freshness:
    """What the framework checkout is, and how far it is from its remote."""

    checkout: Path
    branch: str = ""
    default: str = ""
    behind: int = 0
    ahead: int = 0
    dirty: int = 0
    #: Whether the counts below mean anything. False for a non-git install, a
    #: branch with no upstream, or a fetch that did not come back — all reported as
    #: "unknown", never as "up to date", because a check that could not run must
    #: not look like a check that found nothing.
    known: bool = False
    reason: str = ""

    @property
    def behind_remote(self) -> bool:
        return self.known and self.behind > 0

    @property
    def on_default(self) -> bool:
        return bool(self.branch) and self.branch == self.default


def read(checkout: Path | None = None, *, fetch: bool = True) -> Freshness:
    """Fetch (read-only) and report where the framework checkout stands.

    `fetch=False` reads whatever the last fetch left behind — for a caller that
    has just fetched, or one that must not touch the network at all.
    """
    checkout = checkout or framework_checkout()

    head = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD")
    if head is None or head.returncode != 0:
        return Freshness(checkout, reason="not a git checkout (installed non-editable?)")
    branch = head.stdout.strip()
    default = default_branch(checkout)

    status = _git(checkout, "status", "--porcelain")
    dirty = len([l for l in (status.stdout if status else "").splitlines() if l.strip()])

    if fetch:
        fetched = _git(checkout, "fetch", "--quiet", timeout=FETCH_TIMEOUT_S)
        if fetched is None or fetched.returncode != 0:
            return Freshness(checkout, branch, default, dirty=dirty,
                             reason="could not fetch — offline, or no remote configured")

    counted = _git(checkout, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counted is None or counted.returncode != 0:
        return Freshness(checkout, branch, default, dirty=dirty,
                         reason=f"`{branch}` has no upstream to compare against")
    parts = counted.stdout.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return Freshness(checkout, branch, default, dirty=dirty,
                         reason="git did not report a usable ahead/behind count")
    return Freshness(checkout, branch, default, behind=int(parts[0]),
                     ahead=int(parts[1]), dirty=dirty, known=True)


def run_is_live(project_root: Path) -> bool:
    """Whether a runner is mid-flight in the consuming repo right now.

    Two conditions, not one: the status file has to name a phase that is not finished
    **and** its pid has to still be alive. A status file alone is not evidence — a run
    killed at the terminal leaves the last phase it reached sitting on disk forever
    (`live-pid-is-not-a-live-run`, logged in this repo's own corrections).

    `runner` is imported here rather than at module scope on purpose. It pulls in the
    board, the digest, the merge machinery and half the package; this module is
    consulted on the way to a push and on every panel page load, and neither should pay
    for that import to answer a question they will usually not ask.
    """
    from nightshift import runner

    try:
        status = json.loads((project_root / runner.STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(status, dict):
        return False
    if str(status.get("phase") or "") in ("finished", "digest", ""):
        return False
    try:
        pid = int(status.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    return runner._pid_alive(pid)


def refuse_pull(state: Freshness, project_root: Path | None = None) -> str:
    """Why pulling would not be the routine fast-forward, or `""` if it would be.

    Every one of these means the same thing: someone is in the middle of something
    here, and finishing it for them is where a helpful automation does damage. Said
    rather than resolved.

    **The live-run refusal is the one that protects real work, and it lives here rather
    than in the caller for a reason.** Moving the framework under a run that is already
    using it is the exact failure `doctor.framework_version` names in its own docstring
    — the night that died when `nightshift:main` shifted beneath it. The Command Center
    is what made this reachable by a click, but a rule that only the panel enforced
    would be one the command line could walk straight past, so the check belongs to the
    verb and every caller inherits it. `project_root` is optional because a bare reading
    has no consuming repo in hand; when it is absent, this refusal simply cannot fire.
    """
    if not state.known:
        return state.reason or "nothing is known about this checkout"
    if project_root is not None and run_is_live(project_root):
        return ("a run is live in this repo — pulling now would move the framework out "
                "from under a night that is already using it, which is how a run dies "
                "half-finished. Wait for it, or stop it first")
    if state.dirty:
        return (f"{state.dirty} uncommitted change(s) in {state.checkout} — a pull "
                f"here is not the routine one; commit or stash them first")
    if not state.on_default:
        return (f"it is on `{state.branch}`, not its default branch `{state.default}` "
                f"— pulling a feature branch is a decision, not maintenance")
    if state.ahead:
        return (f"it has {state.ahead} commit(s) the remote does not — that is not a "
                f"fast-forward, and merging or rebasing it is a human's call")
    if not state.behind:
        return "it is already up to date"
    return ""


def pull(state: Freshness, project_root: Path | None = None) -> tuple[bool, str]:
    """Take the offer. Fast-forward only, and only from a state `refuse_pull` clears.

    `--ff-only` is the whole safety of this: it cannot create a merge commit, it
    cannot rewrite anything, and it fails loudly if the situation changed between
    the reading and the act.
    """
    why = refuse_pull(state, project_root)
    if why:
        return False, why
    merged = _git(state.checkout, "merge", "--ff-only", "@{upstream}")
    if merged is None or merged.returncode != 0:
        detail = (merged.stderr or merged.stdout or "").strip().splitlines() if merged else []
        return False, f"the fast-forward failed: {detail[-1][:150] if detail else 'git said nothing'}"
    return True, f"fast-forwarded `{state.branch}` by {state.behind} commit(s)"


def unpaired(project_branch: str, state: Freshness) -> str:
    """Why this pair of checkouts is a combination nothing can see, or `""`.

    The rule is one sentence: **a framework checkout on a feature branch must be on
    the same branch as the repo being checked.** Its default branch is always fine
    — that is the state everything is judged against — and matching names are fine,
    because that is what a paired change looks like. Anything else means the tests
    about to run are green or red depending on what a sibling directory happens to
    have out, which is the state `cross-repo-half-landed-alone` is about and the one
    neither repo's gates can reach.

    Silent when nothing is known about the framework checkout: a wheel install has
    no branch and is not the hazard this describes.
    """
    if not state.branch or not state.default:
        return ""
    if state.on_default or state.branch == project_branch:
        return ""
    return (f"the framework checkout is on `{state.branch}` while this repo is on "
            f"`{project_branch}`. Neither repo can see that combination, so a green "
            f"suite here says nothing: it would be green or red depending on which "
            f"branch {state.checkout} happens to have out. Put them on the same "
            f"branch, or return the framework to `{state.default}`")


def describe(state: Freshness) -> str:
    """One line: where it is, and — only when there is one — the offer.

    One line and no repetition on purpose. Refusing a pull is a first-class answer,
    so a checkout that is deliberately behind must not produce a paragraph every
    time something reads this.
    """
    where = f"{state.checkout}"
    if not state.known:
        return f"framework at {where} — freshness unknown ({state.reason})"
    if not state.behind:
        extra = f", {state.ahead} ahead" if state.ahead else ""
        return f"framework `{state.branch}` is up to date with its remote{extra}"
    offer = refuse_pull(state)
    line = f"framework `{state.branch}` is {state.behind} commit(s) behind its remote"
    if offer:
        return f"{line} — not offering a pull: {offer}"
    return f"{line} — `python -m nightshift.freshness --pull` to fast-forward"


def main(argv: list[str] | None = None) -> int:
    """`python -m nightshift.freshness` — the reading, and optionally the pull.

    Exit 0 for a reading, 2 for a `--pull` that was refused or failed. A refusal
    is not an error state of this repo, so a bare reading is always 0 however far
    behind the checkout is: being behind on purpose is allowed.
    """
    parser = argparse.ArgumentParser(
        description="Is the framework checkout current, and would a pull be routine?")
    parser.add_argument("--pull", action="store_true",
                        help="accept the offer: fast-forward the framework checkout")
    parser.add_argument("--no-fetch", action="store_true",
                        help="read what the last fetch left; touch no network")
    parser.add_argument("--against", default="",
                        help="a branch name to check pairing against (default: none)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    state = read(fetch=not args.no_fetch)
    print(f"  {describe(state)}")
    if args.against:
        why = unpaired(args.against, state)
        print(f"  {why}" if why else f"  paired with `{args.against}`")

    if not args.pull:
        return 0
    # The consuming repo, so the live-run refusal has something to look at. A reading
    # taken outside any repo is still a reading; it just cannot answer that question.
    try:
        here = find_root()
    except ManifestError:
        here = None
    done, detail = pull(state, here)
    print(f"  {'pulled' if done else 'not pulled'} - {detail}")
    return 0 if done else 2


if __name__ == "__main__":
    raise SystemExit(main())
