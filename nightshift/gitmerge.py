"""Merge policy: the strategy options every card merge uses, and how a failed one is
reported.

One home for both, because there is more than one caller — `merge_check` rehearses a
merge in a throwaway worktree, and the runner performs the real one after a rebase — and
a policy stated twice drifts in one place. When `runner.py` moves into this package
(07_portability.md §8 step 4) it keeps importing the same names.

**`-Xrenormalize`, and only that.** Two classes of merge conflict here are noise rather
than disagreement, and exactly one of them can be dissolved safely:

* **Line endings.** A file written CRLF on one branch and LF on another conflicts on
  *every* line while the content is identical. `-Xrenormalize` re-normalizes both sides
  through `.gitattributes` before comparing, so the class disappears deterministically —
  no judgment, no LLM, provably the same content. This project's `.gitattributes` pins
  `* text=auto eol=lf`, which is what makes it correct here.
* **Whitespace.** `-Xignore-space-change` would dissolve the other noisy class and is
  **deliberately not used.** In a Python codebase indentation *is* syntax: a merge that
  ignores whitespace change can silently reparent a block into the wrong `if`, and the
  result compiles and may well pass tests. That is precisely the semantic conflict a human
  review exists to catch, so it must not be dissolved by a flag. A merge is allowed to be
  wrong about formatting; it is not allowed to be wrong about scope.

**Why the reporting half lives here too.** The Dungeoneer runner returned
`detail[-1]` of `(stdout or stderr)`, which is the worst possible selection: git prints
the specific cause *first* and a generic summary *last*, and puts errors on **stderr**
while `or` stops at a non-empty stdout. On 2026-08-01 a merge failed and the only record
anywhere was `"Merge with strategy ort failed."` — a line that says a merge failed, which
was already known. The cause was never written down, so the failure could not be
diagnosed at all. Keeping the *first* informative lines of *both* streams is the whole
fix, and it belongs next to the policy rather than being re-derived per caller.
"""
from __future__ import annotations

import subprocess

__all__ = ["STRATEGY_ARGS", "failure_detail"]

# Passed to `git merge` and `git rebase` alike — rebase forwards `-X` to the merge
# backend, and a rebase is where a card's conflicts actually surface first.
STRATEGY_ARGS: tuple[str, ...] = ("-Xrenormalize",)

# Git's own trailing summaries. Never the cause, always present, and they crowd out the
# lines that are — dropped only when something more specific survives.
_SUMMARY_PREFIXES = (
    "merge with strategy",
    "automatic merge failed",
    "error: could not apply",
    "hint:",
)


def failure_detail(result: subprocess.CompletedProcess, *,
                   max_lines: int = 6, limit: int = 400) -> str:
    """A one-line, bounded reason for a failed merge or rebase.

    **stderr first, and both streams** — git writes the diagnosis to stderr, and the
    `stdout or stderr` idiom silently discards it whenever stdout is non-empty (which a
    merge's progress output makes likely).

    **First informative lines, not the last one.** `CONFLICT (content): Merge conflict in
    X`, `error: Your local changes to the following files would be overwritten`, and
    `The following untracked working tree files would be overwritten` all appear before
    the generic summary git ends with.
    """
    lines: list[str] = []
    for stream in (result.stderr, result.stdout):
        for raw in (stream or "").splitlines():
            line = raw.strip()
            if line and line not in lines:
                lines.append(line)
    if not lines:
        return "merge did not apply cleanly, and git printed nothing"

    specific = [l for l in lines if not l.lower().startswith(_SUMMARY_PREFIXES)]
    # Keep the summary only when it is all there is — better a weak reason than none.
    chosen = specific or lines
    return " | ".join(chosen[:max_lines])[:limit]
