"""What a hook has already told this session, so it can say a thing once.

A PostToolUse hook's stdout is appended to the tool result the model reads, and it
stays in context for every later turn. Measured on real worker transcripts
(`token-economy.md` §1): 50–143k characters of gate-hook output per card — mostly
the same all-clear line and the same violations, repeated after every Edit — and
7–10k of one recipe hint printed on every edit to the same file. None of the
repeats told the model anything the first copy had not.

The memory is a small JSON file per `(session, channel)` in the system temp
directory, keyed by the `session_id` the harness puts in every hook payload.
**Temp, not the repo:** it is a property of one conversation, worthless after it,
and must never be something git sees or a worktree fence has an opinion about.

**No session id means no memory**, and every item is reported as new. A hook run
by hand, or by a harness that sends no id, gets the old say-everything behaviour
rather than a silence nobody asked for.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Iterable

_DIR = Path(tempfile.gettempdir()) / "nightshift-hooks"


def _path(session_id: str, channel: str) -> Path | None:
    if not session_id:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:80]
    return _DIR / f"{safe}-{channel}.json"


def _load(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def _save(path: Path | None, items: Iterable[str]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(set(items))), encoding="utf-8", newline="\n")
    except OSError:
        pass  # a memory that cannot be written degrades to repeating, never to failing


def unseen(session_id: str, channel: str, items: Iterable[str]) -> list[str]:
    """Those of `items` this session has not been told on `channel` — and from now
    on, it has. Order is preserved. For advice that stays true once given."""
    path = _path(session_id, channel)
    told = set(_load(path))
    fresh = [item for item in dict.fromkeys(items) if item not in told]
    if fresh and path is not None:
        _save(path, told | set(fresh))
    return fresh if path is not None else list(dict.fromkeys(items))


def swap(session_id: str, channel: str, current: Iterable[str]) -> set[str] | None:
    """Record `current` as what is open now; return what was open at the last call,
    or `None` when there is no previous call to compare with.

    For state that can go away and come back — a violation fixed and then
    reintroduced is news again, which `unseen` would swallow.
    """
    path = _path(session_id, channel)
    if path is None:
        return None
    previous = set(_load(path)) if path.exists() else None
    _save(path, current)
    return previous
