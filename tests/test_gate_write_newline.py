r"""Tests for the `write_newline` core gate.

Built against synthetic trees, for the reason every gate test in this suite
gives: a test pinned to a real project's source fails whenever someone
legitimately edits it, and a gate whose test is muted is worse than no gate.
A consuming project's own baseline ("the real tree is clean", "the gate is
registered") stays in that project, not here — this suite only owns the
mechanism.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import write_newline  # noqa: E402
from nightshift import textio  # noqa: E402


def _tree(tmp_path: Path, body: str, name: str = "m.py") -> Path:
    """A synthetic repo root holding one `.ai/` module."""
    (tmp_path / ".ai").mkdir(exist_ok=True)
    # newline="" so the fixture's own bytes are not what the gate reads about.
    (tmp_path / ".ai" / name).write_text(body, encoding="utf-8", newline="")
    return tmp_path


def _lines(tmp_path: Path) -> list[int]:
    return sorted(v.line for v in write_newline.check(tmp_path))


# --- what the gate must catch -----------------------------------------------------

@pytest.mark.parametrize("call", [
    'p.write_text(text, encoding="utf-8")',
    'p.write_text(text)',
    '(base / "out.md").write_text(text, encoding="utf-8")',
    'self.path.write_text(text, encoding="utf-8")',
])
def test_write_text_without_newline_is_flagged(tmp_path, call):
    """`Path.write_text` is always text mode — there is no mode argument to read,
    so the absence of `newline=` is the whole defect."""
    root = _tree(tmp_path, f"def f(p, base, text, self):\n    {call}\n")
    assert _lines(root) == [2]


@pytest.mark.parametrize("call", [
    'p.open("w", encoding="utf-8")',
    'p.open("a", encoding="utf-8")',
    'p.open(mode="w", encoding="utf-8")',
    'open(p, "w", encoding="utf-8")',
    'open(p, "a", encoding="utf-8", errors="replace")',
])
def test_text_mode_open_without_newline_is_flagged(tmp_path, call):
    """Both spellings of open, and both the positional and the keyword mode."""
    root = _tree(tmp_path, f"def f(p):\n    fh = {call}\n")
    assert _lines(root) == [2]


def test_the_real_defect_shape_is_caught(tmp_path):
    """The read-modify-write round trip that actually shipped: five `.ai/` modules
    read a committed file, edited it, and wrote it back through `write_text`."""
    root = _tree(tmp_path, (
        "def write(self, values):\n"
        "    self.text = set_fields(self.text, values)\n"
        '    self.path.write_text(self.text, encoding="utf-8")\n'
    ))
    assert _lines(root) == [3]


# --- what the gate must NOT catch ------------------------------------------------

@pytest.mark.parametrize("call", [
    # Pinned explicitly — the rule is "pin it", not "use the helper".
    'p.write_text(text, encoding="utf-8", newline="")',
    'p.write_text(text, encoding="utf-8", newline="\\n")',
    'p.write_text(text, encoding="utf-8", newline=None)',
    'p.open("w", encoding="utf-8", newline="")',
    'open(p, "a", encoding="utf-8", newline="")',
    # The house idiom.
    'textio.write_text_lf(p, text)',
    'textio.append_text_lf(p, text)',
    # Binary — `newline=` is not even legal there.
    'p.write_bytes(data)',
    'p.open("wb")',
    'open(p, "wb")',
    'open(p, "rb")',
    # Reads.
    'p.read_text(encoding="utf-8")',
    'p.open("r", encoding="utf-8")',
    'open(p, "r", encoding="utf-8")',
    'p.open()',
    # A computed mode is not inspected: guessing would fire on code that already
    # decided correctly, exactly as `subprocess_encoding` reasons about `text=`.
    'open(p, mode, encoding="utf-8")',
])
def test_pinned_binary_and_read_calls_are_clean(tmp_path, call):
    root = _tree(tmp_path, f"def f(p, text, data, mode, textio):\n    {call}\n")
    assert _lines(root) == []


def test_a_module_outside_ai_is_out_of_remit(tmp_path):
    """Scope is `.ai/` only. Game code writes no committed text file, and widening
    this past `.ai/` would be a different rule with a different corpus."""
    (tmp_path / "dungeoneer").mkdir()
    (tmp_path / "dungeoneer" / "g.py").write_text(
        'def f(p, t):\n    p.write_text(t, encoding="utf-8")\n',
        encoding="utf-8", newline="")
    assert write_newline.check(tmp_path) == []


def test_missing_ai_directory_is_not_an_error(tmp_path):
    """A fresh repo that has not run `nightshift init` yet may not have `.ai/`."""
    assert write_newline.check(tmp_path) == []


def test_unparseable_module_is_skipped_not_crashed(tmp_path):
    """A gate that dies on a syntax error takes `gates/run.py` down with it, and
    a file mid-edit is the normal case, not the exotic one."""
    root = _tree(tmp_path, "def f(:\n")
    assert write_newline.check(root) == []


# --- the appeal path -------------------------------------------------------------

def test_an_appeal_suppresses_only_its_own_site(tmp_path):
    """`# gate-ok(write_newline): <reason>` exempts one line. Spaced well apart,
    because `appeal_markers.exempt` deliberately covers ±3 lines around the
    marker's span and a denser fixture would be testing that window instead."""
    filler = "\n".join(f"    x{i} = {i}" for i in range(8))
    body = (
        "def f(p, text):\n"
        '    p.write_text(text, encoding="utf-8")'
        "  # gate-ok(write_newline): scratch file, read only by a text editor\n"
        f"{filler}\n"
        '    open(p, "w", encoding="utf-8").write(text)\n'
    )
    root = _tree(tmp_path, body)
    remaining = _lines(root)
    assert len(remaining) == 1, remaining
    assert remaining[0] > 10, "the appealed site should be the suppressed one"


def test_an_appeal_naming_another_gate_does_not_count(tmp_path):
    root = _tree(tmp_path, (
        "def f(p, text):\n"
        '    p.write_text(text, encoding="utf-8")'
        "  # gate-ok(subprocess_encoding): wrong gate for this defect\n"
    ))
    assert _lines(root) == [2]


def test_the_helper_the_gate_points_at_actually_pins_lf(tmp_path):
    """The gate's advice has to be true, or it just moves the defect. Asserted on
    bytes, over the read-modify-write round trip that is the shape of the bug."""
    target = tmp_path / "card.md"
    target.write_bytes(b"---\nstate: tasks\n---\n\nbody\n")
    textio.write_text_lf(target, target.read_text(encoding="utf-8"))
    assert target.read_bytes() == b"---\nstate: tasks\n---\n\nbody\n"

    textio.append_text_lf(target, "appended\n")
    assert target.read_bytes().endswith(b"body\nappended\n")

    # The control: the stdlib call the gate forbids is what produces the bug here.
    if os.linesep == "\r\n":
        naive = tmp_path / "naive.md"
        naive.write_text("a\nb\n", encoding="utf-8")
        assert naive.read_bytes() == b"a\r\nb\r\n", (
            "Path.write_text no longer translates newlines on this platform — if "
            "that is a real change, the gate's rationale needs revisiting, not this "
            "assert"
        )
