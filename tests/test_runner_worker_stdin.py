"""`_run_worker` hands the prompt to the child on **stdin**, not on `argv`.

The bug these tests close (`worker-prompt-off-argv`): Windows caps a
`CreateProcess` command line at 32,767 characters, so a card big enough to push the
built prompt past that never started a process at all — `subprocess.Popen` raised
`FileNotFoundError: [WinError 206] The filename or extension is too long`. Hit for
real on 2026-08-06 launching the overnight run, with a 40,716-byte prompt.

**A real `Popen` against a real child on every test here, deliberately.** A double
would prove the parameter is passed and nothing about whether the OS accepts the
payload, and the size limit is precisely an OS property. Nothing here spawns the
`claude` CLI, spends anything, or needs a network — the child is `sys.executable`
echoing its own stdin.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from nightshift import runner

# ~80 KB: comfortably over the 64 KB the card asks for, over the 32,767-character
# Windows cap that caused the crash, and far past any pipe buffer — so the write
# genuinely blocks until the child drains it, which is the case the feeder thread
# exists for. Non-ASCII on purpose: a real prompt carries em-dashes and cs/es
# diacritics, and the encoding on the write side has to be pinned like the read side.
_LINE = "— ěščřžýáíé na řádku, 0123456789 abcdefghijklmnopqrstuvwxyz\n"
BIG_PROMPT = _LINE * 1400

# Byte-for-byte echo. `sys.stdin.read()` in the child would decode with the child's
# *locale* codec (cp1252 here), which mangles the diacritics before the test can
# see them — the same defect `subprocess_encoding` exists for, one process further
# down. Going through `.buffer` keeps the assertion about transport rather than
# about the stub's own decoding.
_ECHO = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"


def test_the_prompt_is_bigger_than_the_limit_that_caused_the_crash():
    """States what the fixture is for, so a later shrink is a failing test rather
    than a quietly weaker suite."""
    assert len(BIG_PROMPT) > 32_767
    assert len(BIG_PROMPT.encode("utf-8")) > 64 * 1024


def test_an_80_kb_prompt_round_trips_through_a_real_child(tmp_path):
    """The assertion that actually proves the OS limit is gone rather than moved.

    Exact equality, not a substring: newline translation on the write side would
    silently CRLF-ify every line on Windows, and a prompt the model receives is not
    the prompt written to `prompt-N.md`.
    """
    proc = runner._run_worker([sys.executable, "-c", _ECHO], tmp_path, 120,
                              prompt=BIG_PROMPT)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == BIG_PROMPT


def test_the_same_prompt_on_argv_is_what_used_to_fail(tmp_path):
    """The other half of the proof, and the reason the transport had to change.

    Windows refuses the command line outright; POSIX `ARG_MAX` is ~2 MB so it
    simply works there, which is exactly why this went unnoticed for the life of
    the runner. Skipped rather than deleted off Windows: the asymmetry is the
    finding.
    """
    if sys.platform != "win32":
        pytest.skip("only Windows caps the command line this low (ARG_MAX is ~2 MB)")

    with pytest.raises(OSError):
        subprocess.Popen([sys.executable, "-c", "pass", BIG_PROMPT],
                         cwd=tmp_path, stdout=subprocess.PIPE)


def test_stdin_is_closed_even_when_there_is_no_prompt(tmp_path):
    """Before this change the child inherited the runner's console stdin. A
    non-interactive worker should see EOF, and a caller that passes no prompt (every
    existing direct call in the suites) must not hang waiting for one."""
    stub = "import sys; sys.stdout.write(repr(sys.stdin.read()))"
    proc = runner._run_worker([sys.executable, "-c", stub], tmp_path, 30)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "''"


def test_a_child_that_never_reads_stdin_still_returns_promptly(tmp_path):
    """The deadlock the feeder thread prevents. 80 KB does not fit a pipe buffer,
    so an inline write would block until somebody drained it — and nobody here ever
    will. The broken pipe is the child's business; the seam must return its exit
    code either way."""
    stub = "import sys; sys.stdout.write('done')"
    started = time.monotonic()
    proc = runner._run_worker([sys.executable, "-c", stub], tmp_path, 60,
                              prompt=BIG_PROMPT)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "done"
    assert elapsed < 30, f"took {elapsed:.1f}s — the stdin write is blocking the seam"


def test_the_timeout_still_fires_when_the_child_ignores_a_huge_prompt(tmp_path):
    """The guarantee an inline write would have destroyed silently: a child that
    neither reads stdin nor exits would block the write *before* `proc.wait(timeout)`
    was ever reached, turning the timeout into a hang. `TimeoutExpired` still carries
    the accumulated stdout."""
    stub = ("import sys, time\n"
            "sys.stdout.write('working\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        runner._run_worker([sys.executable, "-c", stub], tmp_path, 3, prompt=BIG_PROMPT)
    elapsed = time.monotonic() - started

    assert "working" in (caught.value.output or "")
    assert elapsed < 25, f"took {elapsed:.1f}s — the timeout path is not bounded"


def test_the_tee_still_receives_the_stream_with_a_prompt_attached(tmp_path):
    """`stream_path` is the live feed the whole observability card rests on, and it
    is on the same side of the pipe as the change. Asserted together with a prompt so
    a regression in one is not hidden by the other."""
    stream = tmp_path / "stream.jsonl"
    proc = runner._run_worker([sys.executable, "-c", _ECHO], tmp_path, 60,
                              stream, prompt=_LINE * 20)

    assert proc.returncode == 0, proc.stderr
    assert stream.read_text(encoding="utf-8") == _LINE * 20
