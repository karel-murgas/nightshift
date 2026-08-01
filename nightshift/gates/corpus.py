#!/usr/bin/env python3
"""One read, one parse, one tokenize per file — shared by every gate in a run.

**The waste this exists to remove.** The gates are independent modules by
design (`00_architecture.md` §12): each is importable and runnable on its own,
and none may assume another ran first. That independence was quietly paid for
in the only currency nobody was measuring — the same files, read and parsed
from scratch by every gate that needed them. On Dungeoneer, 2026-08-01:

* `appeal_markers.scan()` tokenizes every `.py` file in the appeal scan roots
  (276 of them here). **Seven** gates call it, because seven gates support
  `# gate-ok(...)` appeals. That is ~5.0 seconds of an 8.3-second suite spent
  tokenizing the same tree seven times and throwing six answers away.
* Nineteen `ast.parse` call sites across seventeen gates parse overlapping
  file sets. `deletion_sweep`, `dispatch_reachability`, `fx_real_enemy_art` and
  `doc_signature_drift` are each ~90% `ast.walk` over trees their neighbours
  had just built and discarded.

None of that is a wrong check. It is the same defect `line_endings` had the
same day — deriving, N times, what could be derived once — and the fix is the
same shape: ask once, keep the answer.

**Why a version-keyed cache and not `functools.lru_cache` on `repo_root`.**
The obvious cache — memoize per repo — is wrong in the one process that
matters most, the test suite. A gate test's natural shape is *build a tree,
`check()` and expect a violation, fix the file, `check()` again and expect
clean*, both calls against the same `tmp_path`. A repo-keyed cache serves the
first answer to the second call and the test passes for the wrong reason,
which is precisely the `check-stopped-checking` class this project keeps
logging. Keying on `(path, mtime_ns, size)` makes an edited file a cache
*miss* by construction, so correctness does not depend on anyone remembering
to invalidate.

`clear()` exists anyway, for a caller that wants to bound memory rather than
correctness — nothing needs it for correctness.

**Bounded, because a long-lived process is a real caller.** A one-shot
`python -m nightshift.gates.run` touches ~1400 files and exits. Pytest runs
hundreds of gate tests in one process, each against its own fixture tree, and
an unbounded dict of parsed ASTs would grow for the length of the session.
Eviction is oldest-first (dicts are insertion-ordered) at `_MAX_ENTRIES`,
which is set well above any single repo's file count so the real run never
evicts (Dungeoneer's full 31-gate run holds ~900 entries).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable, TypeVar

__all__ = ["cached", "read_text", "tree", "clear", "stats"]

T = TypeVar("T")

# Comfortably above a single run's entry count (Dungeoneer: ~900 across all
# namespaces), so a one-shot gate run never evicts and the cap only bites a
# pytest session accumulating fixture trees.
_MAX_ENTRIES = 8192

# `{(namespace, resolved path, mtime_ns, size): value}`. The version fields are
# what make an edited file a miss; see the module docstring.
_CACHE: dict[tuple[str, str, int, int], object] = {}
_HITS = 0
_MISSES = 0


def _key(namespace: str, path: Path) -> tuple[str, str, int, int] | None:
    """Cache key for this *version* of the file, or None if it cannot be stat'd.

    An unstattable path (deleted, permission denied, a broken symlink) is not
    an error here — it is simply uncacheable, and the caller's own compute
    function gets to fail in whatever way it already failed. Returning None
    rather than raising keeps this helper from changing any gate's error
    behaviour.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (namespace, str(path), stat.st_mtime_ns, stat.st_size)


def cached(namespace: str, path: Path, compute: Callable[[Path], T]) -> T:
    """`compute(path)`, computed once per version of `path` per namespace.

    The namespace separates derivations of the same file — text, AST, appeal
    markers — so two gates asking for different things about one file do not
    collide. `compute` is called at most once per (namespace, file version);
    whatever it returns, including None and including an empty list, is the
    cached answer. It is *not* called at all on a cache hit, so it must be a
    pure function of the file — a `compute` that also reports, counts or logs
    would report once and then go quiet, which is the reason this takes a
    function rather than being spelled as a decorator on gate internals.
    """
    global _HITS, _MISSES
    key = _key(namespace, path)
    if key is None:
        return compute(path)
    if key in _CACHE:
        _HITS += 1
        return _CACHE[key]  # type: ignore[return-value]
    _MISSES += 1
    value = compute(path)
    if len(_CACHE) >= _MAX_ENTRIES:
        # Oldest-first. Dropping one at a time keeps the eviction cost flat
        # rather than paying a large clear at an unpredictable moment.
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = value
    return value


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_text(path: Path) -> str | None:
    """The file's text, decoded UTF-8 with replacement, or None if unreadable.

    `errors="replace"` rather than strict, and None rather than an exception,
    because every caller is a gate: a file it cannot decode is a file it has
    nothing to say about, not a reason to take the whole harness down.
    """
    return cached("text", path, _read)


def _parse(path: Path) -> ast.Module | None:
    source = _read(path)
    if source is None:
        return None
    try:
        return ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError):
        # ValueError covers source containing a NUL byte, which `ast.parse`
        # rejects separately from a syntax error.
        return None


def tree(path: Path) -> ast.Module | None:
    """The file's parsed AST, or None if it does not read or does not parse.

    **The returned tree is shared, not a copy.** Every gate holding this path
    gets the same object, so a caller that mutates it corrupts its neighbours'
    view. No gate has any reason to — they all walk and read — and copying
    would give back most of what the cache saves, so the contract is
    read-only by agreement rather than by defensive copying.
    """
    return cached("ast", path, _parse)


def clear() -> None:
    """Drop everything. Not needed for correctness — see the module docstring."""
    global _HITS, _MISSES
    _CACHE.clear()
    _HITS = _MISSES = 0


def stats() -> tuple[int, int, int]:
    """`(hits, misses, entries)` — for tests and for measuring, not for gates."""
    return (_HITS, _MISSES, len(_CACHE))
