#!/usr/bin/env python3
"""Environment-adaptive launcher for the overnight runner.

Both schedulers call *this*, never `runner.py` directly:

  * Windows Task Scheduler on Karel's box  — local models, his git creds, deps
    already installed.
  * a claude.ai routine in an ephemeral cloud container — mobile-triggered, a
    fresh `git clone` with nothing set up.

The two environments differ in exactly four ways, and this script *is* those
four differences and nothing else. Every argument is forwarded to `runner.py`
untouched, so `python .ai/night.py --sessions 1 --max-cards 8 --stale` means the
same thing on both.

  1. **Dependencies.** The laptop has them; a fresh cloud clone does not. An
     unknown host gets `pip install -r requirements.txt` first — and it must
     happen here, before anything imports `runner`, because `runner` pulls in
     `board`/`digest`/… which pull in the game deps.

  2. **Permission posture.** `runner.py` reads it from `.ai/hosts.json`, keyed by
     hostname. A cloud hostname is not a key there, so it would fall back to
     `acceptEdits` — which cannot run Bash, so every worker stalls and every card
     burns an attempt doing *nothing*. An unknown host gets a gitignored
     `.ai/host.json` declaring `bypassPermissions` (the same posture the laptop
     already carries in `hosts.json`).

  3. **The `claude` CLI.** Verified present early and *loudly*. Its absence is
     the whole difference between "refused with a clear reason in the transcript"
     and "a routine that looks like it did nothing" — the failure that started
     this (2026-07-28: the first cloud run dispatched into a container with no
     deps, no posture and no CLI, and produced no visible output at all).

  4. **Publishing.** The laptop's checkout *is* Karel's repo, so a card's branch
     is already somewhere he can reach it — `runner.py` never pushes there. A
     cloud container is ephemeral: everything it commits (the board, merged
     cards, a parked card's `ai/<id>` branch) dies with it unless it is pushed
     before the container ends. An unknown host gets `publish_remote: "origin"`
     in the same override this writes for posture, so `runner.publish()` pushes
     after every settled card and once more at the end of the night — see
     `runner.py`'s "Publish" section for why and what it pushes.

"Known host" = `socket.gethostname()` is a key in `.ai/hosts.json`. That single
test routes local vs cloud; nothing is probed (the `hosts.json` design note
explains why declaration beats detection here). Only the stdlib is imported,
because on the cloud path this runs *before* the dependencies exist.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from nightshift import textio  # LF-pinned writes (gate write_newline); stdlib-only,
# so importing it does not break this module's run-before-pip-install contract.
from nightshift.manifest import find_root

# Windows' console defaults to cp1252, which mangles the em-dashes and ellipses
# in the log lines below into replacement characters once the output is piped
# (Task Scheduler, a routine transcript). Force UTF-8 like the runner does.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# The consuming project, found rather than derived: since 07_portability.md §8
# step 4 this module lives in an installed package, so `__file__`'s grandparent
# is a directory inside the virtualenv. A scheduler invokes this as
# `python -m nightshift.night` from the project's checkout, which is what
# `find_root` walks up from.
ROOT = find_root()
HOSTS_FILE = ROOT / ".ai" / "hosts.json"
HOST_OVERRIDE = ROOT / ".ai" / "host.json"          # gitignored, per-machine
REQUIREMENTS = ROOT / "requirements.txt"

# The canonical night, used when the launcher is called with no runner flags:
# eight cards in one session window, then the Tier-2 staleness sweep on whatever
# window is left. Any explicit argument replaces this wholesale.
#
# `--append-digest` (2026-07-30, Karel: "for when I don't have time to read or
# use a scheduler over weekend") — this is specifically the unattended path, so
# nobody is necessarily reading tonight's `Digest.md` before tomorrow night's run
# writes over it. Appending means a run Karel does not get to for a few nights
# still shows every one of them, newest first, when he finally does look; the
# read baseline only advances again once *he* runs the runner by hand.
DEFAULT_ARGS = ["--sessions", "1", "--max-cards", "8", "--stale", "--append-digest"]


def _log(msg: str) -> None:
    print(f"[night] {msg}", flush=True)


def _known_host() -> bool:
    """Is this machine configured in the committed `hosts.json`?

    Keys beginning with `_` are documentation/placeholders (`_comment`,
    `_TODO-desktop-hostname`), not real hosts, so they never count as a match.
    """
    try:
        data = json.loads(HOSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    real = {k for k in data if not k.startswith("_")}
    return socket.gethostname() in real


def _claude_present() -> bool:
    """Mirror of `runner.claude_binary()` so the two never disagree about where
    the CLI lives: `$CLAUDE_BIN`, then PATH, then `~/.local/bin/claude[.exe]`."""
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return Path(override).is_file()
    if shutil.which("claude"):
        return True
    fallback = Path(os.path.expanduser("~")) / ".local" / "bin" / (
        "claude.exe" if os.name == "nt" else "claude")
    return fallback.is_file()


def _pip_install() -> None:
    _log(f"installing dependencies from {REQUIREMENTS.name} …")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        cwd=ROOT,
    )
    if result.returncode != 0:
        _log("pip install FAILED — the runner cannot import its deps; aborting.")
        sys.exit(result.returncode)


def _write_host_override() -> None:
    """Give an unknown host the posture `runner.py` needs to dispatch real work.

    Only written when absent, so a hand-placed `.ai/host.json` (the documented
    one-off override) is never clobbered. Capabilities are deliberately omitted:
    the override replaces the `hosts.json` entry wholesale, so an empty set means
    a cloud box declares no `gpu-box` — art cards stay put instead of trying to
    pull 20 GB of weights into a throwaway container.

    `publish_remote: "origin"` rides along with the posture for the same reason:
    a cloud container is the one place a night's commits would otherwise die
    unpublished (see the module docstring's fourth difference). `runner.publish()`
    treats an absent/empty `publish_remote` as "don't push" — the laptop's
    `hosts.json` entry carries none, so this is the one place that needs setting.
    """
    if HOST_OVERRIDE.exists():
        _log(f"{HOST_OVERRIDE.name} already present — leaving it as-is.")
        return
    textio.write_text_lf(
        HOST_OVERRIDE,
        json.dumps({"permission_mode": "bypassPermissions", "publish_remote": "origin"}, indent=2) + "\n")
    _log(f"wrote {HOST_OVERRIDE.name} (permission_mode=bypassPermissions, "
         f"publish_remote=origin).")


def _prepare_cloud() -> None:
    _log(f"host {socket.gethostname()!r} is not in hosts.json — cloud path.")
    _pip_install()
    _write_host_override()


def main() -> int:
    args = sys.argv[1:] or list(DEFAULT_ARGS)
    if not sys.argv[1:]:
        _log(f"no flags given — using the default night: {' '.join(DEFAULT_ARGS)}")

    if _known_host():
        _log(f"host {socket.gethostname()!r} is configured — local path, no prep.")
    else:
        _prepare_cloud()

    # A dispatching run needs the CLI; `--dry-run`/`--status` do not, and the
    # runner waives the same check for them — so the launcher does too, or a
    # cloud dry-run would fail for a reason that does not apply to it.
    dispatches = not ({"--dry-run", "--status"} & set(args))
    if dispatches and not _claude_present():
        _log("the `claude` CLI was not found (checked $CLAUDE_BIN, PATH, "
             "~/.local/bin). The runner cannot spawn workers without it — "
             "aborting here so the reason is visible rather than silent.")
        return 1

    cmd = [sys.executable, "-m", "nightshift.runner", *args]
    _log(f"launching runner: {' '.join(cmd[1:])}")
    # No capture: stdout/stderr inherit this process's streams so the run is
    # readable live in the Task Scheduler log or the routine transcript.
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
