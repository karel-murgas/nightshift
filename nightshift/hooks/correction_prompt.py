#!/usr/bin/env python3
"""UserPromptSubmit hook — ask, at the only moment it can be asked, whether the
message that just arrived was a correction.

**The problem this solves.** `.ai/corrections.log` is the instrument every §15
metric depends on, and it can be fed by recall alone: a header saying entries
are "inferred at review time" is a prose rule with no enforcement point, which
is the class Session H measured at 4 entries directly and 12 more downstream
(`10_self_improvement.md` §1). A log fed only by memory is written faithfully
*when someone remembers* — exactly the failure mode this whole project exists
to remove.

**Why this event and not another.** A correction is always a user prompt, so
`UserPromptSubmit` is the only event with 100% coverage of the thing being
captured. `Stop` was considered and rejected in Dungeoneer — it is used for
other purposes there, and a session that ends without it firing loses the
lesson silently (2026-07-23).

**What this hook does NOT do, and it is the important part.** It captures
nothing. Deciding whether a message is a correction is judgment, and §12 forbids
an LLM inside a hook, so the judgment stays in the agent's turn where it belongs.
This script is a *trigger with guaranteed delivery* — one line of context, and
the recording is a tool call the agent makes. Hook = trigger, skill = procedure,
log = artefact, `corrections_log` gate = validation. One job each.

**Why it is state-conditional.** Firing on every prompt makes it wallpaper — a
hook that fires on every edit gets tuned out, which costs more than it saves.
So it arms only when there is *work in the tree newer than the last log entry*:
something exists that could be being corrected, and it has not been recorded.
Appending an entry disarms it; the next edit re-arms it. That tracks the
natural correct → record → fix cycle and stays silent the rest of the time.

Never blocks, never raises, always exits 0. A hook on every prompt that can fail
the prompt is worse than no hook at all.

**Unwired by default.** In Dungeoneer this hook shipped and was deliberately
left out of `.claude/settings.json` — see `.claude/memory/ai_team.md` for the
call and its reasoning. It moves to core with the other three
(07_portability.md §8 step 3) so a project that *does* want it wired only has
to add the `UserPromptSubmit` entry, not carry its own copy of the file.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from nightshift import board  # GENERATED_VIEWS — the one home for the report names
from nightshift import gitpaths

_MESSAGE = (
    "[correction-capture] There is work in the tree newer than the last "
    "`.ai/corrections.log` entry. If the message above points out a mistake, "
    "corrects an assumption, reverses a decision, or asks a question whose answer "
    "turns out to be \"no, I got that wrong\" — invoke the `log-a-correction` "
    "skill and append the entry in the same turn as the fix, not after it. "
    "If it is not a correction, ignore this line silently and do not mention it."
)

# Paths whose churn says nothing about whether work happened. The generated reports
# come from `board`, which is the one home for their names — a second copy here is how
# `Routing.md` and `Chores.md` came to be missing from three lists at once. Imported at
# module level rather than lazily like `find_root` below: that one is guarded because a
# repo may legitimately have no manifest, whereas a `nightshift.board` that will not
# import means this file could not have been located either.
_IGNORED = (("__pycache__", ".ai/corrections.log", ".ai/runs/")
            + board.GENERATED_VIEWS)


def _repo_root() -> Path:
    """Best-effort repo root. Falls back to the working directory — this hook
    must never raise, so a manifest that cannot be found degrades to "scan
    from here" rather than crashing the prompt."""
    try:
        from nightshift.manifest import find_root

        return find_root()
    except Exception:
        return Path.cwd()


def _changed_files(repo_root: Path) -> list[Path]:
    """Working-tree paths git considers modified or untracked."""
    paths: list[Path] = []
    for _, rel in gitpaths.status(repo_root):
        if any(part in rel for part in _IGNORED):
            continue
        path = repo_root / rel
        if path.is_file():
            paths.append(path)
    return paths


def armed(repo_root: Path) -> bool:
    """Is there unrecorded work — i.e. a change newer than the last entry?"""
    log = repo_root / ".ai" / "corrections.log"
    if not log.is_file():
        return True  # no log at all is the strongest possible case for asking
    log_mtime = log.stat().st_mtime
    for path in _changed_files(repo_root):
        try:
            if path.stat().st_mtime > log_mtime:
                return True
        except OSError:
            continue
    return False


def main() -> int:
    try:
        sys.stdin.read()  # drain the event payload; nothing here needs it
        if not armed(_repo_root()):
            return 0
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": _MESSAGE,
            },
            # No need to show this on every prompt; the artefact that matters
            # is the log, not the reminder that produced it.
            "suppressOutput": True,
        }))
    except Exception:
        # Deliberately broad. This runs before every single prompt; any failure
        # here must cost nothing at all.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
