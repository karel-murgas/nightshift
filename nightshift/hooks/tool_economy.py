#!/usr/bin/env python3
"""PreToolUse hook: nobody pays twice for the same answer.

`worker_prompt.TOOL_ECONOMY` has told workers to batch their searches and stop
re-reading files since it was written, and the dispatch prompt has told them how to
run the suite for just as long. Measured on Project Tigress's `tile-layer-surface-cache`
(2026-08-29), a card that reviewed `ok` on its first attempt and was in every other
respect a well-behaved run:

  - the full suite ran **three** times — once as
    `pytest tests/ -n auto --dist loadfile`, then twice more as a bare
    `python -m pytest -q`, serially, at ~5.4 min each. About eleven minutes of
    wall time bought nothing, and the runner re-runs the authoritative slice
    over the branch afterwards regardless.
  - 39 of the attempt's 102 tool calls were Bash, several of them
    `sed -n '1345,1355p' <file>` and `grep -n <pattern> <file>` — the two things
    the project's own instructions say to do with Read and Grep.

Prose in the prompt did not stop either one. That is the whole reason this is a
hook: the instruction was present, correct, and ignored, and an instruction
that is ignored is indistinguishable from one that was never written. Same move
`worktree_fence` makes for the wrong-checkout write — make the expensive thing
*impossible* rather than merely discouraged.

**Four rules, three scopes.**

* *A dispatched worker* (the project's fence env var is set, which only a
  worker's environment carries) may not run the whole suite at all — serial or
  parallel — nor read a file through `cat`/`sed`/`grep`. The suite rule tightened
  on 2026-09-15 (`token-economy.md` phase 1): the parallel full run was allowed
  while it was the only way for a worker to check its branch, and it still cost
  one to three whole-suite runs per card that decided nothing, because the runner
  judges by its own slice. `python -m nightshift.suite slice` now runs exactly
  that slice, so the whole suite is pure duplication for a worker.
* *Every session* — worker, reviewer, a person at a prompt — may not `Read` a
  file over `BIG_FILE_BYTES` whole. One read of a 64 kB UI module is ~55k
  characters carried in context for every later turn, and the same 284 kB scene
  file was read whole twelve times in one card. Unlike the Bash rules this is not
  a style preference with an equivalent: a ranged Read after a Grep gets the same
  answer at a fraction of the context, for anyone, which is why it is not gated
  on the env var. Images, PDFs and notebooks are exempt — the Read tool renders
  those rather than dumping text.
* *Every session* also may not run a small, named list of known-slow commands —
  a bare pytest invocation of the whole suite, `python -m nightshift.preflight`,
  `python -m nightshift.runner` — in the foreground without `run_in_background`
  set. Closes `blocked-the-session-on-a-foreground-long-command` (2026-08-29): a
  foreground long command renders no output between call and return, so a
  session running one is indistinguishable from a hung one, and until now that
  was only prose in the Bash tool's own description and in `CLAUDE.md`, re-decided
  per call. Not gated on the env var — a person at a prompt gets exactly the same
  hang, and the harness cannot tell the two apart either. Not a general slowness
  heuristic: a named list, the same trade the suite rule above makes.

**Denies, and says why.** A `deny` decision returns its reason to the model,
which then retries differently — it is feedback, not a wedge. That is the
distinction from an over-tight `--allowed-tools` list (see `runner._argv`),
where a tool the card needs is refused with nobody to ask and the attempt is
simply spent. Every rule here has a stated cheaper alternative that does the
same job, so there is always a next move.

Fails **open** on anything it cannot parse — a guard that wedges a worker on
confusion is worse than none. No LLM (`00_architecture.md` §12): string and
file-size inspection only.

Wired as `python -m nightshift.hooks.tool_economy` in a consuming project's
`.claude/settings.json` under a `Read|Bash` matcher. Runnable by hand:
    DUNGEONEER_FENCE_ALLOW=/abs/worktree \\
      echo '{"tool_name":"Bash","tool_input":{"command":"python -m pytest -q"}}' \\
      | python -m nightshift.hooks.tool_economy
"""
from __future__ import annotations

SUBJECT = "pytest"

import json
import os
import re
import sys
from pathlib import Path

from nightshift.hooks import shellwords

NAME = "tool_economy"

# What nothing sets when a project declares no `[worker].fence_env` — the Bash
# rules then never arm, the correct behaviour for an unconfigured project.
_DEFAULT_ENV_NAME = "NIGHTSHIFT_FENCE_ALLOW"

#: A whole-file Read above this is denied. Sized from the files that were read
#: whole in the measured transcripts (64 kB, 284 kB, 387 kB) against the ordinary
#: module a Read is for, which is well under it.
BIG_FILE_BYTES = 60_000

#: Rendered by the Read tool rather than dumped as text, so size says nothing
#: about context cost.
_RENDERED = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf", ".ipynb"})

#: A pytest invocation naming no path at all, or naming only the tests root, is
#: the whole suite. `pytest tests/test_one.py` names a file and is exempt: a
#: single file runs in seconds, and running what you touched is exactly the
#: behaviour wanted.
_TESTS_ROOT = re.compile(r"^tests?[/\\]?$")

#: Shell commands that read a file the Read/Grep tools were built to read. Only
#: matched when a *file argument* is present: `foo | head -20` truncates a
#: pipeline's output and is the legitimate use, while `head -20 foo.py` is a
#: Read with extra steps and no line numbers.
_TOOL_FOR = {
    "cat": "Read", "head": "Read", "tail": "Read", "sed": "Read",
    "less": "Read", "more": "Read", "grep": "Grep", "rg": "Grep",
}

#: A bare word that looks like a path operand rather than a pattern or a flag
#: value — it has a separator or a short extension. `grep -n foo bar.py` trips
#: this on the *bar.py* operand; `grep -n foo` (reading stdin) does not.
_PATHISH = re.compile(r"[/\\]|\.\w{1,4}$")

#: The two module invocations the Approach names as "no fence covers": long
#: enough to make a foreground session look hung, and not already denied to a
#: worker the way the whole suite is. Matched on the interpreter's own `-m`
#: argument, so a grep pattern or a commit message naming one cannot trip it
#: (`slow-command-guard-matched-a-grep-pattern`).
_SLOW_MODULES = frozenset({"nightshift.preflight", "nightshift.runner"})

_PYTHONS = frozenset({"python", "python3", "py", "pythonw"})
#: Interpreter options that take a value, so the value is not read as a script.
_PYTHON_VALUED = frozenset({"-X", "-W", "-Q"})


def _repo_root() -> Path | None:
    try:
        from nightshift.manifest import find_root

        return find_root()
    except Exception:
        return None


def _env_name() -> str:
    """The activation switch's name, from `[worker].fence_env`.

    Deliberately the *same* switch `worktree_fence` reads rather than a second
    one: both answer the identical question — "is this a dispatched worker?" —
    and a project that had to arm them separately would sooner or later arm one
    and not the other. Falls back to a name nothing sets, leaving this off.
    """
    root = _repo_root()
    if root is not None:
        try:
            from nightshift.manifest import load

            name = load(root).worker.fence_env
            if name:
                return str(name)
        except Exception:
            pass
    return _DEFAULT_ENV_NAME


def _armed() -> bool:
    return bool(os.environ.get(_env_name(), "").strip())


def _run_module(cmd: shellwords.Command) -> tuple[str, tuple[str, ...]]:
    """`(module, its arguments)` when `cmd` is `python -m <module> ...`, else
    `("", ())`. A script named before any `-m` owns the rest of the line."""
    if cmd.program not in _PYTHONS:
        return "", ()
    words = cmd.words
    skip = False
    for at in range(1, len(words)):
        word = words[at]
        if skip:
            skip = False
        elif word == "-m":
            return (words[at + 1], words[at + 2:]) if at + 1 < len(words) else ("", ())
        elif word in _PYTHON_VALUED:
            skip = True
        elif not word.startswith("-"):
            return "", ()
    return "", ()


def _is_full_suite_pytest(cmd: shellwords.Command) -> bool:
    """`pytest` / `python -m pytest` over everything, parallel or not — judged on the
    command's own executable, never on a word elsewhere in the line."""
    if cmd.program in ("pytest", "py.test"):
        args = cmd.words[1:]
    else:
        module, args = _run_module(cmd)
        if module != "pytest":
            return False
    # Any positional argument that is a path other than the tests root means the
    # run was narrowed — the behaviour we want, not the one we are pricing.
    # `-k`/`-m` selections and `-n <workers>` are flags and fall out below.
    skip_next = False
    for bare in args:
        if skip_next:
            skip_next = False
            continue
        if bare.startswith("-"):
            # gate-ok(pytest_invocation): these flag names are parsed out of a command a worker typed, never built into an argv this code runs.
            skip_next = bare in ("-k", "-m", "-p", "-o", "-n", "--dist", "--deselect",
                                 "--ignore")
            continue
        if _TESTS_ROOT.match(bare):
            continue
        if _PATHISH.search(bare) or "::" in bare:
            return False
    return True


def _is_known_slow_module(cmd: shellwords.Command) -> bool:
    """`python -m nightshift.preflight` / `python -m nightshift.runner`, however the
    interpreter is spelled (`python`, `python3`, `py`)."""
    return _run_module(cmd)[0] in _SLOW_MODULES


def _slow_verdict(command: str, run_in_background: bool) -> str | None:
    """The reason to deny a foreground call to a known-slow command, or None.

    Unlike `_verdict` below, this runs for every session — a person typing the
    command gets the identical hang a dispatched worker would. `run_in_background`
    is the one thing that turns it off: the command is still allowed, just not
    silently in the foreground.
    """
    if run_in_background:
        return None
    for cmd in shellwords.commands(command) or ():
        if _is_full_suite_pytest(cmd) or _is_known_slow_module(cmd):
            return (
                "This command can run for minutes with no output in between, which "
                "makes a foreground call indistinguishable from a hung session. Set "
                "run_in_background: true and check back on it, rather than blocking "
                "the session on it."
            )
    return None


def _file_reader(cmd: shellwords.Command) -> str | None:
    """The reader command's name, when this command reads a file off disk.

    A command reading a pipe is exempt: `... | head -20` is the ordinary, correct
    way to keep a chatty command's result small. Only a reader with a file operand
    of its own is a Read in disguise.
    """
    name = cmd.program
    if cmd.piped or name not in _TOOL_FOR:
        return None
    # The first bare operand of `grep`/`rg` is the *pattern*, not a path, so it
    # is skipped before looking for a file — otherwise `grep foo.bar x` would
    # trip on its own pattern.
    operands = [w for w in cmd.words[1:] if not w.startswith("-")]
    if name in ("grep", "rg") and operands:
        operands = operands[1:]
    return name if any(_PATHISH.search(o) for o in operands) else None


def _verdict(command: str) -> str | None:
    """The reason to deny a dispatched worker's Bash command, or None to allow."""
    for cmd in shellwords.commands(command) or ():
        if _is_full_suite_pytest(cmd):
            return (  # gate-ok(source_reference_liveness): the test path below is a placeholder in advice shown to a worker, not a reference to any file in this repo.
                "Do not run the whole suite. `python -m nightshift.suite slice` runs "
                "exactly the test slice the runner will judge your branch on, in "
                "parallel — run it once before your verdict. The runner runs that "
                "same slice over your branch afterwards regardless, so a whole-suite "
                "run decides nothing. While iterating, run only the files you touched: "
                "`pytest tests/test_x.py`."  # gate-ok(source_reference_liveness): a placeholder filename inside advice shown to a worker, not a reference to any file in this repo.
            )
        reader = _file_reader(cmd)
        if reader:
            tool = _TOOL_FOR[reader]
            return (
                f"Use the {tool} tool instead of `{reader}` to read a file: one "
                f"round-trip instead of a shell spawn, with line numbers, and the "
                f"harness tracks what you have already read. Piping a command's "
                f"*output* through `{reader}` is fine — this is only about reading "
                f"a file off disk."
            )
    return None


def _read_verdict(tool_input: dict) -> str | None:
    """The reason to deny a whole-file Read of a big text file, or None."""
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return None
    raw = str(tool_input.get("file_path") or "")
    if not raw:
        return None
    path = Path(raw)
    if path.suffix.lower() in _RENDERED:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None  # a missing file is the Read tool's error to report, not ours
    if size <= BIG_FILE_BYTES:
        return None
    return (
        f"`{path.name}` is {size // 1000} kB — read whole, all of it stays in context "
        f"for every later turn. Grep -n for what you need, then Read with `offset` "
        f"and `limit` around the hits (a few hundred lines is plenty)."
    )


def check(payload: dict) -> str | None:
    """The deny reason for one PreToolUse payload, or None — run by `main` and by
    the `nightshift.hooks.pre` dispatcher. Fails open on anything unexpected."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    try:
        if tool == "Read":
            reason = _read_verdict(tool_input)
        elif tool == "Bash":
            command = str(tool_input.get("command", ""))
            reason = None
            if command.strip():
                # The worker-only denial goes first: it refuses the whole suite
                # outright, and that refusal must not be softened into "just
                # background it" by the rule below, which would allow exactly
                # what the suite rule forbids.
                if _armed():
                    reason = _verdict(command)
                if reason is None:
                    reason = _slow_verdict(command, bool(tool_input.get("run_in_background")))
        else:
            reason = None
    except Exception:
        return None
    return f"[{NAME}] {reason}" if reason else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    if not isinstance(payload, dict):
        return 0
    reason = check(payload)
    if reason:
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
