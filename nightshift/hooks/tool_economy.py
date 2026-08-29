#!/usr/bin/env python3
"""PreToolUse hook: a dispatched worker does not pay twice for the same answer.

`worker_prompt.TOOL_ECONOMY` has told workers to batch their searches and stop
re-reading files since it was written, and the dispatch prompt has told them to
run the suite as `-n auto --dist loadfile` for just as long. Measured on
Dungeoneer's `tile-layer-surface-cache` (2026-08-29), a card that reviewed `ok`
on its first attempt and was in every other respect a well-behaved run:

  - the full suite ran **three** times — once correctly as
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

**Off unless the runner turns it on.** Like `worktree_fence`, this fires only
when the project's fence env var is set, which only a dispatched worker's
environment carries. An interactive session never sets it, so a human at a
prompt can still `cat` a file or run a serial pytest without argument. That
matters: the rules here are economics for an unattended 100-turn agent, not
style rules for a person, and enforcing them on a human would be a nuisance
with no measured saving behind it.

**Denies, and says why.** A `deny` decision returns its reason to the model,
which then retries differently — it is feedback, not a wedge. That is the
distinction from an over-tight `--allowed-tools` list (see `runner._argv`),
where a tool the card needs is refused with nobody to ask and the attempt is
simply spent. Every rule here has a stated cheaper alternative that does the
same job, so there is always a next move.

Fails **open** on anything it cannot parse — a guard that wedges a worker on
confusion is worse than none. No LLM (`00_architecture.md` §12): string
inspection only.

Wired as `python -m nightshift.hooks.tool_economy` in a consuming project's
`.claude/settings.json` under a `Bash` matcher. Runnable by hand:
    DUNGEONEER_FENCE_ALLOW=/abs/worktree \\
      echo '{"tool_name":"Bash","tool_input":{"command":"python -m pytest -q"}}' \\
      | python -m nightshift.hooks.tool_economy
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

NAME = "tool_economy"

# What nothing sets when a project declares no `[worker].fence_env` — this hook
# then never arms, the correct behaviour for an unconfigured project.
_DEFAULT_ENV_NAME = "NIGHTSHIFT_FENCE_ALLOW"

#: Splits a command line into the segments that are separate invocations, so
#: `cd foo && pytest` is judged on the `pytest`, and a pipeline's downstream
#: `head` is judged separately from its upstream producer.
_SEGMENT = re.compile(r"\s*(?:\|\||&&|[;|])\s*")

#: xdist is on iff one of these appears — `-n 4`, `-nauto`, `--numprocesses=6`,
#: or the plugin named directly. `-n` is the only spelling seen in practice; the
#: others are here so a worker that reaches for the long form is not punished
#: for being explicit.
_XDIST = re.compile(r"(?:^|\s)(?:-n\s*\S|--numprocesses(?:[=\s]\S)|-p\s+xdist)")

#: A pytest invocation naming no path at all, or naming only the tests root, is
#: the whole suite. `pytest tests/test_one.py` names a file and is exempt: a
#: single file runs in seconds and paying xdist's startup for it is the *worse*
#: trade, which is exactly why this rule is about the full suite and not about
#: pytest in general.
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
#: this on `bar.py`; `grep -n foo` (reading stdin) does not.
_PATHISH = re.compile(r"[/\\]|\.\w{1,4}$")


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


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=False)
    except ValueError:
        return segment.split()


def _is_full_suite_pytest(words: list[str]) -> bool:
    """`pytest` / `python -m pytest` over everything, with no xdist."""
    joined = " ".join(words)
    if not re.search(r"(?:^|[/\\\s])(?:pytest|py\.test)(?:$|\s)", joined):
        return False
    if _XDIST.search(joined):
        return False
    # Any positional argument that is a path other than the tests root means the
    # worker narrowed the run itself — the behaviour we want, not the one we are
    # pricing. `-k`/`-m` selections are flags and fall out below.
    skip_next = False
    for w in words:
        bare = w.strip("\"'")
        if skip_next:
            skip_next = False
            continue
        if bare.startswith("-"):
            skip_next = bare in ("-k", "-m", "-p", "-o", "--deselect", "--ignore")
            continue
        if "pytest" in bare or bare in ("python", "python3", "py"):
            continue
        if _TESTS_ROOT.match(bare):
            continue
        if _PATHISH.search(bare) or "::" in bare:
            return False
    return True


def _file_reader(words: list[str], piped_into: bool) -> str | None:
    """The reader command's name, when this segment reads a file off disk.

    `piped_into` is the exemption that makes this safe to deny on: a segment
    downstream of a pipe is consuming the previous command's output, and
    `... | head -20` is the ordinary, correct way to keep a chatty command's
    result small. Only a reader with a file operand of its own is a Read in
    disguise.
    """
    if piped_into or not words:
        return None
    cmd = Path(words[0].strip("\"'")).name.lower()
    if cmd.endswith(".exe"):
        cmd = cmd[:-4]
    if cmd not in _TOOL_FOR:
        return None
    # The first bare operand of `grep`/`rg` is the *pattern*, not a path, so it
    # is skipped before looking for a file — otherwise `grep foo.bar x` would
    # trip on its own pattern.
    operands = [w.strip("\"'") for w in words[1:] if not w.startswith("-")]
    if cmd in ("grep", "rg") and operands:
        operands = operands[1:]
    return cmd if any(_PATHISH.search(o) for o in operands) else None


def _verdict(command: str) -> str | None:
    """The reason to deny, or None to allow."""
    segments = _SEGMENT.split(command)
    for i, seg in enumerate(segments):
        words = _words(seg)
        if not words:
            continue
        if _is_full_suite_pytest(words):
            return (  # gate-ok(source_reference_liveness): `tests/test_x.py` below is a placeholder in advice shown to a worker, not a reference to any file in this repo.
                "Run the full suite in parallel: `python -m pytest tests/ "
                "-n auto --dist loadfile`. A bare serial pytest over the whole "
                "suite costs several times what the parallel run does, and the "
                "runner re-runs the authoritative slice over your branch "
                "afterwards regardless — so you need only satisfy yourself the "
                "area you changed is green. To run one file serially, name it: "
                "`pytest tests/test_x.py`."  # gate-ok(source_reference_liveness): a placeholder filename inside advice shown to a worker, not a reference to any file in this repo.
            )
        reader = _file_reader(words, piped_into=i > 0)
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    if payload.get("tool_name") != "Bash" or not _armed():
        return 0
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not command.strip():
        return 0
    try:
        reason = _verdict(command)
    except Exception:
        return 0  # fail open
    if reason:
        json.dump({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"[{NAME}] {reason}",
        }}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
