"""Spawning the Claude CLI worker process -- the one place it is executed.

`_run_worker` (aliased `run_cli` for `ingest.py`, its other caller) is the
`Popen` + two-draining-threads dance that runs one Claude CLI invocation to
completion and hands back a `subprocess.CompletedProcess`-shaped result; every
pipeline stage that spawns a model (producer, checker, reviewer, stale-hunter,
repair, merge-resolver) calls it, which is why it lives apart from any one of
them. `_worker_env` builds the environment (the worktree fence's allowlist);
`_budget_argv`/`_STREAM_ARGV`/`_STRICT_MCP_ARGV` are the argv fragments every
caller assembles around it.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from nightshift import board
from nightshift import hostconfig

def _budget_argv(card_budget: float) -> list[str]:
    """`--max-budget-usd`, and only when a cap was actually asked for.

    Passing `--max-budget-usd 0` would be a cap of zero, not the absence of one,
    so "no cap" has to mean "no flag"."""
    return ["--max-budget-usd", str(card_budget)] if card_budget > 0 else []
# `--output-format stream-json` emits one JSON object per event as it happens,
# instead of one blob buffered until the process exits — what turns a silent
# hour into a live feed on disk (`_run_worker`'s `stream_path`). Confirmed
# directly against the installed CLI, not assumed: a bare `--output-format
# stream-json` without `--verbose` refuses with "Error: When using --print,
# --output-format=stream-json requires --verbose". `--verbose` changes nothing
# else functionally here — every event still lands in the tee and only the
# terminal one is read back (`_terminal_result`).
_STREAM_ARGV = ["--output-format", "stream-json", "--verbose"]


# `--strict-mcp-config` says "use only the MCP servers given by `--mcp-config`",
# and no caller here passes one — so a dispatched worker or reviewer gets none at
# all, regardless of what the human's user-level config happens to hold that
# week.
#
# **This is determinism, not a saving, and the difference was measured.** The
# guess it replaces was that workers were carrying a pile of MCP tool schemas;
# the `system`/`init` event of Project Tigress's `tile-layer-surface-cache` attempt
# says otherwise — exactly one server was present ("claude.ai Google Drive",
# status `needs-auth`), contributing zero tools, and the browser server the guess
# named was never in the session at all. Trimming that is worth approximately
# nothing in tokens.
#
# What it is worth is that an unattended run's tool surface stops depending on
# the launching human's personal config. A server added for interactive work
# appears in every worker on that box the same night, unreviewed and unlogged —
# and an authenticated one would have brought real schemas and a real network
# reach into a `bypassPermissions` agent nobody is watching. A worker's context
# should be a property of the project and the card, which is the same argument
# `review_branch` already makes for the reviewer's blindness.
#
# If a card ever genuinely needs an MCP server, this is the line that has to
# grow a `--mcp-config` beside it — deliberately, in the repo, rather than by
# inheriting whatever was configured.
#
# Confirmed against the installed CLI rather than assumed, the same way
# `_STREAM_ARGV` was: a `-p` run carrying this flag and no `--mcp-config` reports
# `mcp_servers: []` in its `init` event, on a box whose user config declares one.
_STRICT_MCP_ARGV = ["--strict-mcp-config"]
def _run_worker(argv: list[str], cwd: Path, timeout: int,
                stream_path: Path | None = None,
                env: dict[str, str] | None = None,
                prompt: str = "") -> subprocess.CompletedProcess:
    """The one place the Claude CLI is executed.

    A named seam rather than an inline call, for two reasons. It is the only
    line in this file that is not deterministic, so isolating it makes the §12
    claim checkable rather than a promise (Project Tigress's
    tests/test_board_runner.py asserts no decision function reaches it). And it
    is what lets the whole dispatch
    cycle — worktree, verdict, gates, tests, card move, run record — be driven
    end to end in a test without spending a night and a budget to find out that a
    file move was wrong.

    `Popen` + two draining threads, not `subprocess.run`: reading only one of
    stdout/stderr while the other fills is a deadlock once either pipe's OS
    buffer is full, so both have to be drained concurrently regardless of
    whether anything is teed to disk. Every stdout line is appended to an
    accumulator *and*, when `stream_path` is given, written to it and flushed
    immediately — that tee is the whole card: a worker's tool calls land on
    disk as they happen instead of staying invisible until the process exits.
    The return value is still `.returncode`/`.stdout`/`.stderr` shaped, so
    nothing downstream had to change shape.

    **The drain threads' joins are bounded on every path, not only the timeout
    one.** A child that exits promptly while a grandchild it spawned (a Bash
    tool call, an MCP server) still holds the pipe's write end open leaves
    `readline()` never seeing EOF — the drain thread never finishes on its own.
    An unbounded `join()` on the success path would then block forever with no
    timeout backstop at all (the timeout guard below only covers `proc.wait()`,
    not drain-thread completion). So both paths `join(timeout=...)` and log-and-
    proceed rather than wait, matching this file's existing rule that an
    observer able to end the run is worse than none.

    **`prompt` goes down the child's stdin, never on `argv`** (worker-prompt-off-argv).
    Windows caps a `CreateProcess` command line at 32,767 characters, so the old
    `[binary, "-p", prompt, ...]` shape died with `WinError 206` — "the filename or
    extension is too long" — the moment a card grew past ~32 KB. That happened for
    real on 2026-08-06 and took the whole overnight queue with it. `claude -p`
    already reads its prompt from stdin when no prompt positional is given
    (`--input-format` defaults to `text`), so this is a change of transport with no
    change of meaning: the text is still the opening user message, and `--agent`,
    `--resume`, `--max-budget-usd` and the `stream-json` tee are untouched. No
    prompt this project can build is size-limited by the OS again.

    Three details it is easy to get wrong here:

    * **A third thread, not an inline write.** ~40 KB does not fit a pipe buffer, so
      the write blocks until the child drains it — and a child that never reads
      stdin would block it forever, *before* `proc.wait(timeout=...)` is reached.
      That would quietly convert the timeout guarantee into a hang. Feeding on its
      own daemon thread keeps the guard reachable on every path; the joins below
      bound it exactly like the drain threads.
    * **stdin is opened and closed even when `prompt` is empty.** Today the child
      inherits the runner's console stdin, which is neither wanted nor observable.
      A closed pipe is an immediate EOF, which is what a non-interactive worker
      should see.
    * **`newline=""` on the write side.** `text=True` gives a `TextIOWrapper` whose
      default translates every `\\n` to `os.linesep`, i.e. silently CRLF-ifies the
      prompt on Windows. The prompt must reach the model byte-for-byte as written
      to `prompt-N.md`, so the translation is pinned off — the same rule the
      `write_newline` gate enforces everywhere else in this tree.
    """
    stream_fh = None
    if stream_path is not None:
        try:
            stream_fh = open(stream_path, "a", encoding="utf-8", errors="replace",
                             newline="")
        except OSError:
            stream_fh = None

    try:
        # `encoding=` is not optional: the worker's JSON carries em-dashes and
        # cs/es diacritics, and the locale codec on Windows has no mapping for
        # some of those bytes. Without it the decode raises in the drain
        # thread, the accumulator stays empty, and the verdict silently reads
        # as empty.
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", env=env)
    except OSError:
        if stream_fh is not None:
            stream_fh.close()
        raise

    # Pin newline translation off on the write side; see the docstring. Guarded
    # rather than assumed: this is best-effort formatting fidelity, and losing it
    # must never be the thing that fails a dispatch.
    try:
        proc.stdin.reconfigure(newline="")
    except (AttributeError, OSError, ValueError):
        pass

    out_lines: list[str] = []
    err_lines: list[str] = []

    def _drain_stdout() -> None:
        try:
            for line in proc.stdout:
                out_lines.append(line)
                if stream_fh is not None:
                    try:
                        stream_fh.write(line)
                        stream_fh.flush()
                    except (OSError, ValueError):
                        # A tee failure (disk full, a locked file) must not
                        # silently kill accumulation of the real stdout the
                        # verdict and cost are read from — swallow it here,
                        # the same way `_log`/`_status` swallow their own.
                        pass
        finally:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                err_lines.append(line)
        finally:
            try:
                proc.stderr.close()
            except (OSError, ValueError):
                pass

    def _feed_stdin() -> None:
        try:
            if prompt:
                proc.stdin.write(prompt)
                proc.stdin.flush()
        except (OSError, ValueError):
            # A child that exited before reading breaks the pipe. That is the
            # child's failure to report, not a reason to raise from the seam —
            # its exit code and stderr are already on their way back.
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_out.start()
    t_err.start()
    # Started *after* the drains, on purpose: a big prompt only fits down the pipe
    # as fast as the child consumes it, and a child blocked on its own full stdout
    # would never get there.
    t_in = threading.Thread(target=_feed_stdin, daemon=True)
    t_in.start()

    def _close_stream() -> None:
        if stream_fh is not None:
            try:
                stream_fh.close()
            except (OSError, ValueError):
                pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        t_in.join(timeout=5)
        _close_stream()
        raise subprocess.TimeoutExpired(argv, timeout, output="".join(out_lines),
                                        stderr="".join(err_lines)) from None

    # Bounded exactly like the timeout path above, for the grandchild-holds-
    # the-pipe-open case described on the docstring: proc has exited, but the
    # drain thread may still be blocked on `readline()` with no EOF in sight.
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    t_in.join(timeout=5)
    _close_stream()

    return subprocess.CompletedProcess(argv, proc.returncode,
                                       "".join(out_lines), "".join(err_lines))


#: Supported name for the seam above, for callers outside this module.
#:
#: `ingest.py` dispatches the CLI too, and the docstring's claim — *the one place the
#: Claude CLI is executed* — is worth keeping literally true rather than nearly true.
#: A second copy of the `Popen` + two-draining-threads dance is exactly the drift this
#: avoids: that pattern is not defensive tidiness, it is the fix for a deadlock that
#: took a whole overnight queue on 2026-08-06, and a copy would not carry the reason.
#:
#: An alias rather than a move: relocating 180 lines and its logger out of this module
#: is a refactor with its own risk, and nothing needs it yet. When a third caller
#: appears, extract properly instead of adding a second alias.
run_cli = _run_worker


def _worker_env(root: Path, tree: Path, out_dir: Path) -> dict[str, str]:
    """The environment a dispatched worker runs under.

    Everything is inherited (the CLI keeps its PATH, auth and config) plus one
    addition: the fence env var (`[worker].fence_env`), the worktree fence's
    allowlist. A project that declares none gets a plain inherited environment
    and the fence stays off. The roots a worker may
    legitimately write to are exactly three — its own worktree; its run dir under
    `.ai/runs/` (where the verdict JSON lands, which is outside the worktree); and
    `Board/` (the prompt lets it append to the card's `## Thread`). Anything else
    is the wrong-checkout defect the fence exists to stop.
    """
    allow = os.pathsep.join(
        str(p.resolve()) for p in (tree, out_dir, board.board_dir(root)))
    name = hostconfig.fence_env(root)
    return {**os.environ, name: allow} if name else dict(os.environ)
