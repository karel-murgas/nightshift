"""Tests for the `prose_reference_liveness` core gate — a backticked module or
file name inside a docstring or comment in the tooling source must resolve to
something real.

Unlike `source_reference_liveness` (live-code path literals), this gate's whole
job is prose: a docstring or `#` comment naming `` `nightshift.<module>` ``, a
bare `` `name.py` ``, or `` `path/x.py` `` that used to exist and does not any
more -- `digest.py` surviving as a live-sounding reference in a dozen
docstrings after it was deleted is the motivating incident
(docstring-and-manifest-diet, slice 1).
"""
from __future__ import annotations

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

from nightshift.gates import corpus  # noqa: E402
from nightshift.gates import prose_reference_liveness as gate  # noqa: E402


def _repo(tmp_path: Path, source: str, *, name: str = "somegate.py") -> Path:
    """A repo holding one scanned module under `.ai/` -- the same shape
    `test_gate_source_reference_liveness.py` uses, since `.ai/` is one of this
    gate's two scanned trees regardless of manifest."""
    ai = tmp_path / ".ai"
    ai.mkdir(exist_ok=True)
    (ai / name).write_text(source, encoding="utf-8")
    corpus.clear()
    return tmp_path


def _messages(violations) -> str:
    return " ".join(v.rule for v in violations)


# --- hit -----------------------------------------------------------------

def test_a_backticked_dead_module_in_a_docstring_is_caught(tmp_path):
    """The motivating shape: a module docstring naming a file that does not
    exist anywhere in the scanned trees."""
    root = _repo(tmp_path, (
        '"""Some gate.\n\n'
        'See `does_not_exist_anywhere.py` for the old approach.\n"""\n'
    ))
    violations = gate.check(root)
    assert violations, "a dead backticked .py reference in a docstring must be reported"
    assert "does_not_exist_anywhere.py" in _messages(violations)


def test_a_backticked_dead_path_in_a_comment_is_caught(tmp_path):
    """The same shape, in a `#` comment rather than a docstring."""
    root = _repo(tmp_path, (
        '"""Some gate."""\n'
        "# See `.ai/gates/no_such_gate.py` for the old approach.\n"
    ))
    violations = gate.check(root)
    assert violations, "a dead backticked path in a comment must be reported"
    assert ".ai/gates/no_such_gate.py" in _messages(violations)


# --- miss ------------------------------------------------------------------

def test_a_backticked_reference_to_a_real_file_is_not_reported(tmp_path):
    """A reference that resolves is not a violation -- the gate's whole
    purpose is telling a dead reference from a live one, not flagging every
    backtick-quoted filename."""
    root = _repo(tmp_path, (
        '"""Some gate.\n\n'
        'See `.ai/somegate.py` for the shared helper.\n"""\n'
    ))
    corpus.clear()
    violations = gate.check(root)
    assert not violations, f"a live reference must not be reported: {_messages(violations)}"


def test_a_bare_word_with_no_backticks_is_not_scanned(tmp_path):
    """Only the three backticked shapes are matched -- prose that merely
    mentions a filename in passing, unquoted, is not a claim this gate checks."""
    root = _repo(tmp_path, (
        '"""Some gate.\n\n'
        "Mentions does_not_exist_anywhere.py without backticks.\n\"\"\"\n"
    ))
    violations = gate.check(root)
    assert not violations, f"an unquoted mention must not be reported: {_messages(violations)}"


# --- exemption ---------------------------------------------------------------

def test_since_removed_nearby_exempts_a_dead_reference(tmp_path):
    """A historical mention that says its own piece -- "since removed" within
    the same neighbourhood of text -- is not a violation."""
    root = _repo(tmp_path, (
        '"""Some gate.\n\n'
        'Used to call `does_not_exist_anywhere.py` (since removed).\n"""\n'
    ))
    violations = gate.check(root)
    assert not violations, f"an exempted historical mention must not be reported: {_messages(violations)}"


def test_deleted_nearby_exempts_a_dead_reference(tmp_path):
    """Same exemption, the other vocabulary word."""
    root = _repo(tmp_path, (
        '"""Some gate.\n\n'
        '`does_not_exist_anywhere.py` was deleted last month.\n"""\n'
    ))
    violations = gate.check(root)
    assert not violations, f"an exempted historical mention must not be reported: {_messages(violations)}"


def test_gate_ok_appeal_exempts_a_comment_hit(tmp_path):
    """The standard `# gate-ok(<gate>): <reason>` appeal, same as every other
    AST gate here, covers what the exemption vocabulary does not reach."""
    root = _repo(tmp_path, (
        '"""Some gate."""\n'
        "# gate-ok(prose_reference_liveness): illustrative example, not a real file, twenty chars.\n"
        "# See `.ai/gates/no_such_gate.py` for the old approach.\n"
    ))
    violations = gate.check(root)
    assert not violations, f"an appealed reference must not be reported: {_messages(violations)}"
