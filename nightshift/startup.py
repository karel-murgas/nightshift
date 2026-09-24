"""Start-of-run checks: the kill switch, workspace trust, and the schema
sweep -- what a run verifies about its own environment before it dispatches
anything, once per run.

Named `startup_checks`/`StartupChecks`, not `preflight`/`Preflight`, on
purpose: `nightshift.preflight` is a different thing entirely (the per-branch
merge-readiness check a worker runs on its own work), and the two shared a
name only because this module used to be `runner.py` too. `recover` is the
crash-recovery sweep that runs alongside it, over attempts a prior run left
mid-flight.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board, git, textio, tiers
from nightshift import hostconfig

def claude_binary() -> str | None:
    """`claude` is not always on PATH — on this box it lives in
    `%USERPROFILE%\\.local\\bin`. Checked rather than assumed, because a runner
    that discovers this at 3 AM has already moved a card and bumped `attempts`."""
    if override := os.environ.get("CLAUDE_BIN"):
        return override if Path(override).is_file() else None
    if found := shutil.which("claude"):
        return found
    fallback = Path(os.path.expanduser("~")) / ".local" / "bin" / (
        "claude.exe" if os.name == "nt" else "claude")
    return str(fallback) if fallback.is_file() else None


def ensure_workspace_trusted(root: Path) -> None:
    """Mark the repo trusted in `~/.claude.json` before any worker is dispatched.

    Headless `claude -p` has no trust dialog, so an untrusted workspace does not
    stop — it *degrades silently*: the worker drops every `permissions.allow` and
    `additionalDirectories` entry (2026-07-25: a whole run's dispatches ran with
    149 allow + 9 dir entries ignored, the warning buried in each worker's
    stderr). Trust is deliberately machine-local — it lives in `~/.claude.json`,
    never the repo, because a repo cannot vouch for itself — so the only fix that
    travels to every machine is code that writes the flag itself.

    Keys off the resolved repo root (Claude resolves a worktree's trust to its
    git root, so this covers every `.<project>-worktrees/<card>` checkout too).
    Idempotent: writes at most once per machine, then no-ops, which also keeps it
    from racing another Claude process's writes to the same file. Defensive: a
    missing file, unreadable JSON or an unwritable home degrades to the same
    dropped-permissions warning it is trying to prevent — not worth failing a
    night over — so every failure is swallowed.
    """
    config = Path.home() / ".claude.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    target = str(root.resolve()).replace("\\", "/")
    projects = data.setdefault("projects", {})
    keys = [k for k in projects if k.replace("\\", "/").lower() == target.lower()] \
        or [target]
    changed = False
    for key in keys:
        proj = projects.setdefault(key, {})
        if proj.get("hasTrustDialogAccepted") is not True:
            proj["hasTrustDialogAccepted"] = True
            changed = True
    if not changed:
        return
    try:
        textio.write_text_lf(config, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        hostconfig._log("marked the workspace trusted in ~/.claude.json — headless `-p` has no "
             "trust dialog, and an untrusted workspace silently drops the worker's "
             "permissions.allow / additionalDirectories entries")
    except OSError:
        pass


@dataclass
class StartupChecks:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def startup_checks(root: Path, base: str, dry_run: bool, *, named_card: bool = False) -> StartupChecks:
    reasons: list[str] = []

    if (root / hostconfig.STOP_FILE).is_file():
        note = (root / hostconfig.STOP_FILE).read_text(encoding="utf-8").strip()
        # The switch's job is to interrupt a run in progress (`_stop_requested`
        # consumes it there too, immediately, the moment it does that job) — not
        # to permanently block the next one from starting. A plain start clears a
        # stale one and proceeds rather than making Karel `rm` it by hand every
        # time (Karel, 2026-08-22: "runner should clear at the start of the
        # run"). `--dry-run` still only reports it (no writes, ever), and a named
        # `--card` still refuses and leaves it — naming a card is explicit enough
        # that it must not silently swallow a switch someone may have dropped
        # moments ago for a still-relevant reason ("card start should not delete
        # STOP").
        if dry_run or named_card:
            reasons.append(f"kill switch: {hostconfig.STOP_FILE.as_posix()} exists" + (f" — {note}" if note else ""))
        else:
            try:
                (root / hostconfig.STOP_FILE).unlink()
            except OSError:
                pass
            hostconfig._log("clearing stale kill switch at start: " + hostconfig.STOP_FILE.as_posix()
                 + (f" — {note}" if note else ""))

    forbidden = hostconfig.forbidden_bases(root)
    if base in forbidden:
        reasons.append(f"base branch `{base}` is forbidden — the runner never builds on {sorted(forbidden)}")
    elif git.run(root, "rev-parse", "--verify", base).returncode != 0:
        reasons.append(f"base branch `{base}` does not exist")

    # The launch checkout being dirty only matters when the runner will operate
    # *in it* — the in-place fallback, when Karel is still on the integration
    # branch. Once he has moved to his own branch (the new topology), the runner
    # works in a dedicated checkout and his uncommitted edits are none of its
    # business — that separation is the whole point of runner-hardening #3. The
    # dedicated checkout's own cleanliness is checked in `run()`, after it is cut.
    on_base = hostconfig.current_branch(root) == base
    if on_base and (dirty := hostconfig.dirty_outside_board(root)):
        shown = ", ".join(dirty[:4]) + (f" (+{len(dirty) - 4} more)" if len(dirty) > 4 else "")
        note = f"working tree is dirty outside Board/: {shown}"
        # A dry run writes nothing and cuts no worktree, so a dirty tree is
        # information, not an obstacle — and refusing would make the one command
        # meant for "show me the board" unusable exactly when you are editing.
        if dry_run:
            hostconfig._log(f"note — {note}")
        else:
            reasons.append(note)

    if not dry_run and claude_binary() is None:
        reasons.append("the `claude` CLI was not found (set CLAUDE_BIN, or put it on PATH)")

    try:
        tiers.binding(root)
    except tiers.TierError as exc:
        reasons.append(str(exc))

    return StartupChecks(ok=not reasons, reasons=reasons)


def schema_violations(root: Path) -> dict[str, list[str]]:
    """`card_schema` violations, grouped by card id.

    A malformed card is not dispatched. It is not a failure either — it is a
    card that is not finished being written, and the panel is where it should
    surface. Running the gate rather than reimplementing its rules is the point:
    the schema has exactly one owner.
    """
    out = subprocess.run(
        [sys.executable, "-m", "nightshift.gates.run", "card_schema"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    grouped: dict[str, list[str]] = {}
    for line in (out.stdout or "").splitlines():
        if "Board/" not in line:
            continue
        location = line.split(" — ", 1)[0]
        card_id = Path(location.split(":", 1)[0]).stem
        grouped.setdefault(card_id, []).append(line.strip())
    return grouped


# --------------------------------------------------------------------------
# Crash recovery
# --------------------------------------------------------------------------

def recover(root: Path) -> list[str]:
    """A card with `started:` and no `finished:` was interrupted.

    Nothing else can produce that combination once the lock is ours: the runner
    clears `started:` on every exit path it controls, so what is left is a
    machine that went away mid-dispatch. `attempts` was already incremented
    before the worker started, which is what stops a reboot loop from retrying
    forever — recovery does not need to guess how far it got.
    """
    notes: list[str] = []
    for card in board.cards(root, "tasks"):
        if not card.fields.get("started") or card.fields.get("finished"):
            continue
        started = card.fields["started"]
        # Same bucket a plain `failed` verdict earns in `settle` — a card whose
        # machine vanished mid-dispatch is not proven broken, but it is not
        # proven healthy either, and it should not be first back in line ahead
        # of cards that never had trouble.
        card.write({"started": None, "finished": hostconfig._now(), "last_outcome": "failed"})
        card.write_section(
            "Error",
            f"Attempt {card.attempts} was interrupted — the runner or the machine went "
            f"away after `started: {started}` and never recorded a result. Recovered on "
            f"the next boot; `attempts` had already been incremented, so this counts.",
        )
        notes.append(f"{card.id}: recovered interrupted attempt {card.attempts}")
    if notes:
        board.commit_board(root, f"board: recover {len(notes)} interrupted attempt(s)")
    return notes
