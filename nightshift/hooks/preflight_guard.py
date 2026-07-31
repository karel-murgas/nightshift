#!/usr/bin/env python3
"""PreToolUse hook: refuse to publish a commit the preflight has not validated.

This is the enforcement point that makes `nightshift.preflight` mandatory rather
than a script someone remembers to run (`10_self_improvement.md` §4). It fires
on `git push`, `git merge`, and `gh pr create` — the operations that carry a
branch's work outward — and denies the tool call when the commit being published
has no preflight receipt.

**It is instant, and that is the design.** The expensive checks (pytest, the
gate suite) ran once, in the preflight, and left a receipt keyed by SHA. This
hook only resolves the relevant SHA and looks it up. A hook that itself ran the
suite would run it on every push attempt, which is exactly the cost the receipt
exists to avoid.

**Which SHA it checks:**

* `git push` / `gh pr create` — `HEAD`. You are publishing the branch you are on.
* `git merge <ref>` — the tip of `<ref>`. You validate a feature branch, then
  switch to the integration branch to merge it; the receipt for the feature tip
  is what must exist, not one for the integration branch you are sitting on. The
  receipt file keeps the last several SHAs precisely so this cross-branch lookup
  resolves.

Deny is `permissionDecision: "deny"` with a reason; the tool call never runs.
Anything it cannot parse confidently, it **allows** — a guard that blocks on
confusion gets disabled, and the preflight is a safety net here, not a vault
door. No LLM (`00_architecture.md` §12); it reads a JSON file and shells `git`.

Wired as `python -m nightshift.hooks.preflight_guard` in a consuming project's
`.claude/settings.json` (07_portability.md §8 step 3).
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path | None:
    """The repo root, or None if it cannot be found. A guard that cannot locate
    the repo must fail open (see `main`), never crash the tool call."""
    try:
        from nightshift.manifest import find_root

        return find_root()
    except Exception:
        return None


def _git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo_root,
                         capture_output=True, text=True, timeout=10,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _allow() -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _target_sha(repo_root: Path, argv: list[str]) -> str | None:
    """The SHA the command would publish, or None if this is not a guarded op."""
    if not argv:
        return None
    # gh pr create ...
    if argv[0] == "gh" and argv[1:3] == ["pr", "create"]:
        return _git(repo_root, "rev-parse", "HEAD") or None
    if argv[0] != "git":
        return None
    # Skip global -c/-C options to find the subcommand.
    rest = argv[1:]
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in ("-c", "-C") else 1
    if i >= len(rest):
        return None
    sub = rest[i]
    tail = rest[i + 1:]
    if sub == "push":
        return _git(repo_root, "rev-parse", "HEAD") or None
    if sub == "merge":
        # First non-flag argument is the ref being merged in.
        ref = next((a for a in tail if not a.startswith("-")), None)
        if ref is None:
            return None  # `git merge` with no ref (e.g. --continue/--abort) — not a publish
        return _git(repo_root, "rev-parse", ref) or None
    return None


def _decision(command: str, repo_root: Path) -> dict:
    try:
        argv = shlex.split(command)
    except ValueError:
        return _allow()  # unparseable quoting — do not block on confusion
    sha = _target_sha(repo_root, argv)
    if sha is None:
        return _allow()

    from nightshift import preflight

    if preflight.is_validated(repo_root, sha):
        return _allow()
    return _deny(
        f"preflight has not validated {sha[:8]}. Run `python -m nightshift.preflight` "
        f"(or `--no-corrections \"<reason>\"` if this branch has no lesson to log) "
        f"and let it pass before pushing/merging — it runs the gates, the audit and "
        f"the full test suite once, so the loop's lessons are recorded before the "
        f"work leaves the branch (10_self_improvement.md §4)."
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        command = payload.get("tool_input", {}).get("command", "")
        if not command:
            print(json.dumps(_allow()))
            return 0
        repo_root = _repo_root()
        if repo_root is None:
            print(json.dumps(_allow()))  # cannot find the repo — fail open
            return 0
        print(json.dumps(_decision(command, repo_root)))
    except Exception:
        # A guard that crashes must fail open — never wedge a session's git.
        print(json.dumps(_allow()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
