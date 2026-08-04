#!/usr/bin/env python3
"""PreToolUse hook: `git commit` must name what it is committing.

The failure this exists to prevent (Dungeoneer, 2026-07-28, `commit-swept-staged-index`):
a session committing a digest fix ran `git add .ai/digest.py tests/test_board_digest.py`
and then a separate `git commit`, and read that as "commit only those two files". It is
not what git does — **`git commit` commits the whole index**, and the index already
carried three board-card renames staged by Karel in Obsidian while the session ran. The
commit moved three cards into `tasks/done` with `state:` still `needs-decision`, no
`## Approach` and live open questions: seven `card_schema` violations that were absent
from the parent commit. It was caught by an isolated-worktree preflight before any push
and undone with `git reset --soft HEAD~1`, but only because the validation happened in a
clean worktree — run against the dirty main tree, the same violations would have read as
Karel's in-progress cards and been waved through.

The precondition is not exotic. Karel edits the board in Obsidian while sessions run, so
"the index may hold someone else's staged changes" is the **normal** state of this repo,
not an edge case. The correction was classed `GATE: yes` and nothing was built for it,
which is why the rule lived for three days as a sentence in a log.

**The rule: a `git commit` that does not say what it commits is refused.** Two accepted
forms, both explicit —

    git commit -m "..." -- path/one path/two     # a pathspec: commits exactly these
    git add -- path/one && git commit -m "..." -- path/one

— and the denial text lists the index contents, so the reply to a refusal is a
copy-paste rather than an investigation.

**`-a` is refused too, and that is the load-bearing decision.** `git commit -a` sweeps
every tracked modification in the working tree, which in the concurrent-editing scenario
above is a strictly *larger* blast radius than the index sweep that caused the incident.
Allowing it would leave the easiest workaround for a denial sitting immediately next to
the thing being denied — an escape hatch that makes the outcome worse is not an escape
hatch.

**What is allowed through, and why each is not the defect:**

* an **empty index** — nothing to sweep, and the commit will fail on its own terms;
* a **merge, cherry-pick, revert or rebase in progress** — git populates the index
  itself and a pathspec is rejected outright by `git commit` in that state, so requiring
  one would make conflict resolution impossible;
* `--amend` with an empty index — a message-only amend adds no content.

Fails **open** on anything it cannot determine: an unparseable payload, no git, not a
repository, a `git` call that errors. A guard that wedges a session on confusion is
worse than none, which is the same direction `worktree_fence` takes.

No LLM (`00_architecture.md` §12): string inspection plus two `git` reads.

Wired as `python -m nightshift.hooks.commit_pathspec` in a consuming project's
`.claude/settings.json` under `PreToolUse`/`Bash`, with **no `if:` prefix filter**. A
filter like `Bash(git commit:*)` matches on how the command *starts*, so the compound
form `git add -- x && git commit -m "..."` — which is the exact shape of the incident —
would walk straight past it. The hook returns immediately when the command contains no
`commit`, so the cost of matching every Bash call is one cheap string test.

Runnable by hand:
    echo '{"tool_name":"Bash","tool_input":{"command":"git commit -m x"}}' \\
      | python -m nightshift.hooks.commit_pathspec
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

NAME = "commit_pathspec"

_SEPARATORS = re.compile(r"&&|\|\||[;&|\n]")

# Sequencer state directories/files: git builds the index itself and refuses a pathspec.
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                "rebase-merge", "rebase-apply")

_SWEEPS_WORKTREE = ("-a", "--all")


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        try:
            return shlex.split(segment, posix=False)
        except ValueError:
            return []


def _git(args: list[str], cwd: Path | None) -> str | None:
    """`git <args>` stdout, or None if git could not answer. `encoding=` is mandatory —
    `text=True` alone decodes with the locale codec and returns None on the first byte
    cp1252 cannot map, which is the `subprocess_encoding` defect."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def commit_segments(command: str) -> list[list[str]]:
    """Token lists for each `git commit` invocation in `command`."""
    found: list[list[str]] = []
    for segment in _SEPARATORS.split(command):
        tokens = _tokens(segment)
        if not tokens or Path(tokens[0]).name not in ("git", "git.exe"):
            continue
        # Skip git's own global options to find the subcommand.
        try:
            index = tokens.index("commit")
        except ValueError:
            continue
        # `commit` must be the subcommand, not a value like `git config x commit`.
        if index == 0:
            continue
        found.append(tokens)
    return found


def _repo_dir(tokens: list[str], cwd: Path) -> Path:
    """`git -C <path> commit` operates over there, so the index to inspect is there."""
    for i, token in enumerate(tokens):
        if token == "-C" and i + 1 < len(tokens):
            candidate = Path(tokens[i + 1])
            return candidate if candidate.is_absolute() else cwd / candidate
    return cwd


def _sequencer_busy(repo: Path) -> bool:
    git_dir = _git(["rev-parse", "--git-dir"], repo)
    if git_dir is None:
        return False
    base = Path(git_dir.strip())
    if not base.is_absolute():
        base = repo / base
    return any((base / marker).exists() for marker in _IN_PROGRESS)


def _staged(repo: Path) -> list[str] | None:
    out = _git(["diff", "--cached", "--name-only"], repo)
    if out is None:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def _has_untrack_in_index(repo: Path) -> bool:
    """Is a staged deletion of a file that still exists on disk — i.e. `git rm --cached`?

    This is the one intent git offers **no pathspec form** for, so the guard must let it
    past or it forbids an operation outright rather than scoping it. `git commit -- <path>`
    rebuilds that path from the *working tree*, and the whole point of `--cached` is that
    the file is still there — so naming it in a pathspec silently re-adds it and the
    untrack is undone. Found the hard way on 2026-08-01 untracking `.obsidian/`: the
    pathspec commit reported success, changed only `.gitignore`, and left all 16 files
    tracked. The denial text was actively wrong there, telling the caller to do the one
    thing that could not work.

    Narrow on purpose. A staged deletion whose file is *also* gone from disk still needs a
    pathspec, because `git commit -- <path>` records that deletion correctly.
    """
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=D"], repo)
    if not out:
        return False
    return any((repo / line.strip()).exists() for line in out.splitlines() if line.strip())


def _flags(tokens: list[str]) -> tuple[bool, bool, bool]:
    """`(has explicit pathspec, sweeps the worktree, is an amend)`."""
    after_commit = tokens[tokens.index("commit") + 1:]
    has_pathspec = "--" in after_commit and after_commit.index("--") < len(after_commit) - 1
    amend = "--amend" in after_commit
    sweeps = False
    for token in after_commit:
        if token == "--":
            break
        if token in _SWEEPS_WORKTREE:
            sweeps = True
        # Bundled short flags: `-am`, `-av`. Not `--author`, not a value.
        elif re.fullmatch(r"-[A-Za-z]+", token) and "a" in token[1:]:
            sweeps = True
    return has_pathspec, sweeps, amend


def evaluate(payload: dict, cwd: Path | None = None) -> str | None:
    """Return a denial reason, or None to allow."""
    if payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if "commit" not in command:
        return None

    here = cwd or Path.cwd()
    for tokens in commit_segments(command):
        repo = _repo_dir(tokens, here)
        has_pathspec, sweeps, amend = _flags(tokens)

        if sweeps:
            return _deny_sweep()
        if has_pathspec:
            continue

        staged = _staged(repo)
        if staged is None:          # not a repo, or git unavailable — fail open
            continue
        if not staged:              # nothing to sweep
            continue
        if amend and not staged:
            continue
        if _sequencer_busy(repo):   # git owns the index; a pathspec is rejected
            continue
        if _has_untrack_in_index(repo):  # `git rm --cached` has no pathspec form
            continue
        return _deny_index(staged)
    return None


def _deny_index(staged: list[str]) -> str:
    shown = "\n".join(f"  {path}" for path in staged[:40])
    more = f"\n  ... and {len(staged) - 40} more" if len(staged) > 40 else ""
    return (
        f"Blocked: `git commit` with no pathspec commits the WHOLE INDEX, not just what "
        f"you last `git add`ed.\n"
        f"The index currently holds {len(staged)} path(s):\n{shown}{more}\n\n"
        f"If that list is exactly what you mean to commit, say so explicitly:\n"
        f"    git commit -m \"...\" -- <the paths you want>\n"
        f"Anything in the list you did not stage is someone else's work in progress — "
        f"typically the maintainer editing the board in their editor while a session "
        f"runs. Committing it swept up seven half-edited cards once, which is why this is "
        f"refused rather than suggested.\n"
        f"(nightshift.hooks.commit_pathspec)"
    )


def _deny_sweep() -> str:
    return (
        "Blocked: `git commit -a` commits every tracked modification in the working "
        "tree, which is a wider sweep than the index sweep this guard exists to stop.\n"
        "Name what you are committing instead:\n"
        "    git add -- <paths> && git commit -m \"...\" -- <paths>\n"
        "(nightshift.hooks.commit_pathspec)"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse
    reason = evaluate(payload if isinstance(payload, dict) else {})
    if reason:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
