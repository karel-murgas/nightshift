"""The shared corpus cache: one read/parse per file version, per run.

The tests that matter here are not the hit-rate ones — those only prove it is
fast. The load-bearing tests are the *invalidation* ones, because the whole
risk of adding a cache to a checker is that a gate starts answering about a
file as it used to be. A gate that passes on a stale parse is the
`check-stopped-checking` class, and it would be invisible: the run goes green,
which is what everyone expected anyway.
"""
from __future__ import annotations

import ast
import os

import pytest

from nightshift.gates import corpus


@pytest.fixture(autouse=True)
def _clean_cache():
    corpus.clear()
    yield
    corpus.clear()


def _write(path, text: str):
    path.write_text(text, encoding="utf-8", newline="")
    return path


# --- the point of the whole thing --------------------------------------------

def test_the_same_file_is_parsed_once_for_many_readers(tmp_path):
    src = _write(tmp_path / "m.py", "x = 1\n")
    first = corpus.tree(src)
    for _ in range(6):
        assert corpus.tree(src) is first  # the same object, not an equal one
    hits, misses, _ = corpus.stats()
    assert (hits, misses) == (6, 1)


def test_different_namespaces_do_not_collide(tmp_path):
    src = _write(tmp_path / "m.py", "x = 1\n")
    assert corpus.read_text(src) == "x = 1\n"
    assert isinstance(corpus.tree(src), ast.Module)


def test_compute_is_not_called_again_on_a_hit(tmp_path):
    src = _write(tmp_path / "m.py", "x = 1\n")
    calls = []
    for _ in range(3):
        corpus.cached("probe", src, lambda p: calls.append(p) or "v")
    assert len(calls) == 1


# --- invalidation: the tests this cache exists to survive ---------------------

def test_an_edited_file_is_re_read_not_served_from_cache(tmp_path):
    """The gate-test shape: check, fix the file, check again in one process."""
    src = _write(tmp_path / "m.py", "x = 1\n")
    assert corpus.read_text(src) == "x = 1\n"
    _bump(src, "x = 2\n")
    assert corpus.read_text(src) == "x = 2\n"


def test_an_edited_file_is_re_parsed(tmp_path):
    src = _write(tmp_path / "m.py", "def a(): pass\n")
    assert _names(corpus.tree(src)) == ["a"]
    _bump(src, "def b(): pass\n")
    assert _names(corpus.tree(src)) == ["b"]


def test_a_same_length_edit_still_invalidates(tmp_path):
    """Size alone would not catch this; mtime is why the key has both."""
    src = _write(tmp_path / "m.py", "x = 1\n")
    assert corpus.read_text(src) == "x = 1\n"
    _bump(src, "x = 9\n")
    assert corpus.read_text(src) == "x = 9\n"


def test_a_file_that_becomes_unparseable_stops_returning_the_old_tree(tmp_path):
    src = _write(tmp_path / "m.py", "x = 1\n")
    assert corpus.tree(src) is not None
    _bump(src, "def (:\n")
    assert corpus.tree(src) is None


def test_two_files_with_identical_content_are_separate_entries(tmp_path):
    a = _write(tmp_path / "a.py", "x = 1\n")
    b = _write(tmp_path / "b.py", "x = 1\n")
    assert corpus.tree(a) is not corpus.tree(b)


# --- failure modes ------------------------------------------------------------

def test_a_missing_file_reads_as_none_and_is_not_cached(tmp_path):
    missing = tmp_path / "gone.py"
    assert corpus.read_text(missing) is None
    assert corpus.tree(missing) is None
    # Uncacheable rather than cached-as-None: the file may appear later, and a
    # cached absence keyed on a stat that failed has no version to invalidate on.
    assert corpus.stats()[2] == 0


def test_a_file_created_after_a_miss_is_seen(tmp_path):
    path = tmp_path / "later.py"
    assert corpus.tree(path) is None
    _write(path, "y = 2\n")
    assert corpus.tree(path) is not None


def test_unparseable_python_is_none_not_an_exception(tmp_path):
    src = _write(tmp_path / "bad.py", "def (:\n")
    assert corpus.tree(src) is None
    assert corpus.read_text(src) == "def (:\n"  # text is still available


def test_a_nul_byte_does_not_raise(tmp_path):
    """`ast.parse` rejects NUL with ValueError, not SyntaxError."""
    (tmp_path / "nul.py").write_bytes(b"x = 1\x00\n")
    assert corpus.tree(tmp_path / "nul.py") is None


def test_undecodable_bytes_do_not_raise(tmp_path):
    (tmp_path / "latin.py").write_bytes(b"# caf\xe9\nx = 1\n")
    assert isinstance(corpus.read_text(tmp_path / "latin.py"), str)


# --- bookkeeping --------------------------------------------------------------

def test_clear_empties_the_cache_and_the_counters(tmp_path):
    corpus.tree(_write(tmp_path / "m.py", "x = 1\n"))
    corpus.clear()
    assert corpus.stats() == (0, 0, 0)


def test_the_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "_MAX_ENTRIES", 4)
    for i in range(10):
        corpus.read_text(_write(tmp_path / f"m{i}.py", f"x = {i}\n"))
    assert corpus.stats()[2] <= 4


def test_eviction_drops_the_oldest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "_MAX_ENTRIES", 2)
    paths = [_write(tmp_path / f"m{i}.py", f"x = {i}\n") for i in range(3)]
    for path in paths:
        corpus.read_text(path)
    hits_before = corpus.stats()[0]
    corpus.read_text(paths[-1])          # newest: still resident
    assert corpus.stats()[0] == hits_before + 1
    corpus.read_text(paths[0])           # oldest: evicted, so a miss
    assert corpus.stats()[0] == hits_before + 1


def _names(tree) -> list[str]:
    return [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


def _bump(path, text: str):
    """Rewrite `path` with a guaranteed-different mtime.

    Filesystem timestamp granularity is coarser than a test's write loop on
    some platforms, so an edit within the same tick could otherwise look
    unchanged. Real edits are seconds apart; this only forces the condition the
    test is actually about.
    """
    path.write_text(text, encoding="utf-8", newline="")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
