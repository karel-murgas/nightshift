#!/usr/bin/env python3
"""PreToolUse hook: no judgment actor may open the board's private lane.

The failure this exists to prevent (Dungeoneer, 2026-07-27, `triage-read-private-ideas`):
the `triage` agent, dispatched on an inbox note, glob/grep'd the board for context and
read `Board/ideas/Generovani zvuku.md` — the lane its own charter says no judgment actor
may ever open ("Not avoid unless useful: never"). It self-reported afterwards and
nothing from that file reached the card it wrote, so the cost that time was zero. The
reason it happened is the part that generalises: the prohibition existed **only as
charter prose the agent has to remember**, while the agent does its own file discovery.
`reconcile.py`, the one component permitted in that lane, is hard-coded to read exactly
one field from it — a boundary, not a promise. A prose-only "never open X" rule handed to
something that greps for itself will eventually be crossed by a wide enough search.

**What counts as private is derived, never listed here.** `nightshift.board.LANES` is the
board's single home for "which lanes exist", and the private lane is defined by its
*absence* from that tuple — that is literally what `board.py`'s own comment says
(`ideas/` is absent "for the same reason it is absent from reconcile.LANES: it is
Karel's private lane and no judgment actor — the runner included — enumerates it"). So
this hook parses that constant out of the project's `board.py` and treats any board
subdirectory missing from it as private, rather than writing the word "ideas" down a
fourth time. Same technique, and the same reason, as `worktree_fence` reading
`runner.py::FENCE_ENV` instead of copying it: `roster-has-two-homes` is the correction
about what a second copy costs.

A consequence worth stating, because it is a feature rather than a surprise: a *new*
directory under `Board/` is private until it is added to `LANES`. The denial says so and
names the fix. Defaulting an unknown lane to readable would be the wrong direction for a
guard whose entire subject is a lane nobody remembered to protect.

**Coverage, honestly bounded.** It denies `Read`, `Grep`, `Glob` and `Bash` calls whose
target names a private lane. That closes the path the incident actually took — the agent
enumerated the board, then *opened the file*, and the open is the step that leaks the
content. It does **not** stop a repo-wide content grep from surfacing a matching line in
passing; blocking every unscoped search would break ordinary work, and the residual is
recorded here rather than papered over.

**Moving a private note is not reading it** (Karel, 2026-08-14: *"I want you to be able
to commit and push cards in ideas folder. I just want you to not use text from them for
any game planning, game concepts etc."*). Until then the lane was uncommittable, and not
by anyone's decision: `commit_pathspec` refuses a `git commit` that does not name its
paths, this fence refuses any `Bash` command that does — so the two guards together
forbade an operation neither was written to forbid. A `Bash` call is therefore allowed
past when **every** segment of it is a `git` invocation whose subcommand only ever moves
a path around (`_MOVE_SUBCOMMANDS`) and carries no flag that would print what is inside
it (`_SHOWS_CONTENT`). `git add`, `git commit -m … --`, `git push` go through; `git show`,
`git diff`, `git log -p`, `git grep`, and anything that is not git at all, do not.

Three properties of that carve-out are deliberate, and all three point the same way:

* **Allowlist, not denylist.** A subcommand nobody thought of lands on the denied side.
* **Every segment must be git.** `git add -- Board/ideas/x.md && cat Board/ideas/x.md`
  is one `Bash` call, and only checking the first command in it would be no check at all.
* **Conservative on shell syntax.** A command containing `$(`, a backtick, or a
  redirection is denied rather than parsed, so a substitution cannot smuggle a reader in.
  The cost is that a commit message containing those characters is refused; reword it.

The residual is the one the paragraph above already names: an unscoped `git diff` or
`git show` prints private content without naming the lane, exactly as an unscoped grep
does. Scoping those is the same trade — it would break ordinary work — and it is left
open on the same terms.

**Off if it cannot be sure.** No repo root, no `board.py`, an unparseable `LANES`, or a
`LANES` that comes back empty ⇒ the fence does not arm. The alternative — treating every
board directory as private when the parse fails — would deny all board access on a
malformed file, and a guard that bricks the board when it is confused gets removed.

**Escape hatch, for Karel and nobody else.** Setting `NIGHTSHIFT_PRIVATE_LANE_UNLOCK=1`
in the environment the harness itself was launched with turns the fence off. A dispatched
agent cannot reach it: an inline `VAR=1 cmd` prefix in a `Bash` call sets the variable for
that child process, while this hook is spawned by the harness and inherits the harness's
environment, so the only actor who can set it is the human who started the session.

No LLM (`00_architecture.md` §12): AST for the constant, string inspection for the rest.

Wired as `python -m nightshift.hooks.ideas_fence` under `PreToolUse` for
`Read|Grep|Glob|Bash`. Runnable by hand:
    echo '{"tool_name":"Read","tool_input":{"file_path":"Board/ideas/x.md"}}' \\
      | python -m nightshift.hooks.ideas_fence
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

NAME = "ideas_fence"

_UNLOCK_ENV = "NIGHTSHIFT_PRIVATE_LANE_UNLOCK"

_WATCHED = ("Read", "Grep", "Glob", "Bash")
# Where each tool names the thing it is about. Checked as strings so an absolute path, a
# repo-relative one and a shell command are all covered by one rule.
_FIELDS = ("file_path", "path", "pattern", "glob", "command", "notebook_path")

# --- the git carve-out ----------------------------------------------------------
# `git` subcommands that can name a path without printing what is inside it. An
# allowlist, so a subcommand nobody thought of is denied rather than waved through.
_MOVE_SUBCOMMANDS = frozenset({"add", "commit", "push", "mv", "rm", "restore", "reset"})

# Flags that turn one of the above into a content reader. `git add -p` and `git commit -v`
# both put the diff in front of the caller; `-F`/`-t` read a file in as the message.
_SHOWS_CONTENT = frozenset({"-p", "--patch", "-i", "--interactive", "-v", "--verbose",
                            "-e", "--edit", "-F", "--file", "-t", "--template"})

# Shell forms that can smuggle a reader past a token-level check. Matched against the raw
# string, quotes and all: deciding whether `$(…)` is quoted needs a shell parser, and a
# fence does not get to guess.
_SMUGGLERS = ("$(", "`", "<(", "${")

# Command separators. Splitting the raw string over-splits a quoted message containing
# one of these, which yields a segment that is not a git call, which denies — the safe
# direction, and the reason this is a regex over the raw text rather than a token walk.
_SEPARATORS = re.compile(r"&&|\|\||>>|[;&|<>\n]")

# git's own global options, which sit before the subcommand. `-C` and `-c` take a value.
_GLOBAL_TAKES_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path")


def _subcommand(tokens: list[str]) -> str | None:
    """The subcommand of a `git …` token list, skipping git's global options."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _GLOBAL_TAKES_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _moves_without_reading(command: str) -> bool:
    """Is `command` nothing but git calls that relocate a path without showing it?

    False on anything uncertain — an unparseable quote, a non-git segment, an unknown
    subcommand, a flag that prints content. Same direction as every other decision here.
    """
    if any(smuggler in command for smuggler in _SMUGGLERS):
        return False
    segments = [segment for segment in _SEPARATORS.split(command) if segment.strip()]
    if not segments:
        return False
    for segment in segments:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens or Path(tokens[0]).name not in ("git", "git.exe"):
            return False
        subcommand = _subcommand(tokens)
        if subcommand not in _MOVE_SUBCOMMANDS:
            return False
        # Only flags before `--`; past it every token is a pathspec by definition.
        for token in tokens[:tokens.index("--")] if "--" in tokens else tokens:
            if token in _SHOWS_CONTENT:
                return False
            # Bundled short flags: `-am`, `-pv`. Not `--patch-with-stat`, not a value.
            if re.fullmatch(r"-[A-Za-z]+", token) and len(token) > 2:
                if any(f"-{letter}" in _SHOWS_CONTENT for letter in token[1:]):
                    return False
    return True


def _repo_root() -> Path | None:
    try:
        from nightshift.manifest import find_root

        return find_root()
    except Exception:
        return None


def _board_root(repo_root: Path) -> str:
    try:
        from nightshift.manifest import load

        return load(repo_root).board.root
    except Exception:
        return "Board"


def known_lanes(repo_root: Path) -> tuple[str, ...]:
    """The lanes the board machinery enumerates — `board.LANES`.

    Parsed out of `.ai/board.py` until 07_portability.md §8 step 4, because
    `.ai/` is not an importable package and a hook must not depend on it being
    one. `board` now ships in this package, so the honest move is to import the
    one definition instead of keeping a parser that could disagree with it.

    `()` on any failure, which turns the fence off — the same safe direction the
    parser took, and the same one every other path in this file takes.
    """
    try:
        from nightshift.board import LANES

        return tuple(LANES)
    except Exception:
        return ()


def private_lanes(repo_root: Path) -> tuple[str, ...]:
    """Board subdirectories absent from `LANES`. Empty ⇒ the fence does not arm."""
    lanes = known_lanes(repo_root)
    if not lanes:
        return ()
    board = repo_root / _board_root(repo_root)
    if not board.is_dir():
        return ()
    return tuple(sorted(
        d.name for d in board.iterdir()
        if d.is_dir() and d.name not in lanes and not d.name.startswith(".")
    ))


def _mentions(text: str, board: str, lane: str) -> bool:
    """Does `text` name `<board>/<lane>` as a path, in any separator or case?

    Case-insensitive because Windows paths are, and a fence that a different casing
    walks straight through is not a fence.
    """
    needle = f"{board}/{lane}".lower()
    return needle in text.replace("\\", "/").lower()


def evaluate(payload: dict, board: str, lanes: tuple[str, ...]) -> str | None:
    """Return a denial reason, or None to allow. `lanes` empty ⇒ fence off."""
    if not lanes:
        return None
    if payload.get("tool_name") not in _WATCHED:
        return None
    tool_input = payload.get("tool_input") or {}
    haystack = " ".join(str(tool_input.get(field, "")) for field in _FIELDS)
    if not haystack.strip():
        return None
    for lane in lanes:
        if _mentions(haystack, board, lane):
            # Staging, committing and pushing a private note moves it without opening it,
            # and the lane was uncommittable until this existed — see the docstring.
            if (payload.get("tool_name") == "Bash"
                    and _moves_without_reading(str(tool_input.get("command", "")))):
                return None
            return _deny_text(board, lane)
    return None


def _deny_text(board: str, lane: str) -> str:
    return (
        f"Blocked: `{board}/{lane}/` is a private lane and no judgment actor may open "
        f"it.\n"
        f"It is private because it is absent from `nightshift.board.LANES`, which is the "
        f"board's single list of working lanes. The only component permitted in there is "
        f"`reconcile.py`, which reads one frontmatter field and never the body.\n"
        f"If you need context for a card, use `{board}/inbox/`, the card itself, and the "
        f"codebase — that is what the note you were given was built from.\n"
        f"Committing and pushing the lane *is* allowed — `git add`/`commit`/`push` naming "
        f"these paths goes through, because moving a note is not reading it. What is "
        f"refused is anything that prints the contents: `git show`, `git diff`, `git log "
        f"-p`, `cat`.\n"
        f"If `{lane}` is in fact a working lane, the fix is to add it to `nightshift.board.LANES`, "
        f"not to read around this.\n"
        f"(nightshift.hooks.{NAME})"
    )


def main() -> int:
    if os.environ.get(_UNLOCK_ENV):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot parse
    root = _repo_root()
    if root is None:
        return 0
    reason = evaluate(payload if isinstance(payload, dict) else {},
                      _board_root(root), private_lanes(root))
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
