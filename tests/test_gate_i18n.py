"""Tests for the three i18n gates (`i18n_parity`, `i18n_untranslated`,
`i18n_loanwords`) and the adapter mechanism they share (07_portability.md §2b).

A synthetic adapter stands in for a project's real one — the whole point of
the adapter interface is that these gates never touch a project's actual
string storage, only the three functions it promises to expose. Dungeoneer's
own adapter (`.ai/i18n_adapter.py`, reading a Python dict literal) is tested
in that project against its real tree; this suite only owns the contract.
"""
from __future__ import annotations

import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import i18n_loanwords  # noqa: E402
import i18n_parity  # noqa: E402
import i18n_untranslated  # noqa: E402

_ADAPTER = '''
STRINGS = {STRINGS!r}
LINES = {LINES!r}

def load_strings(repo_root):
    return STRINGS

def zone_key_lines(repo_root):
    return LINES

def i18n_path(repo_root):
    return repo_root / "strings.txt"
'''


def _project(tmp_path: Path, strings: dict, lines: dict, *, targets=("cs",),
            allowlist: str = "", denylist: str = "") -> Path:
    ai = tmp_path / ".ai"
    ai.mkdir(exist_ok=True)
    (ai / "adapter.py").write_text(
        _ADAPTER.format(STRINGS=strings, LINES=lines), encoding="utf-8")
    targets_toml = ", ".join(f'"{t}"' for t in targets)
    (ai / "manifest.toml").write_text(
        "[i18n]\n"
        'adapter = ".ai/adapter.py"\n'
        'base = "en"\n'
        f"targets = [{targets_toml}]\n"
        f'untranslated_allowlist = "{allowlist}"\n'
        f'loanwords_denylist = "{denylist}"\n',
        encoding="utf-8",
    )
    return tmp_path


def _lines_for(strings: dict) -> dict:
    """1-indexed line numbers, one per key per language, in insertion order."""
    return {
        lang: {key: i + 1 for i, key in enumerate(keys)}
        for lang, keys in strings.items()
    }


# --- no [i18n] table: silence, not a crash ---------------------------------

def test_no_manifest_at_all_is_silent(tmp_path):
    assert i18n_parity.check(tmp_path) == []
    assert i18n_untranslated.check(tmp_path) == []
    assert i18n_loanwords.check(tmp_path) == []


def test_a_manifest_with_no_i18n_table_is_silent(tmp_path):
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text("", encoding="utf-8")
    assert i18n_parity.check(tmp_path) == []
    assert i18n_untranslated.check(tmp_path) == []
    assert i18n_loanwords.check(tmp_path) == []


# --- i18n_parity -------------------------------------------------------------

def test_parity_passes_on_matching_key_sets(tmp_path):
    strings = {"en": {"a": "Alpha", "b": "Beta"}, "cs": {"a": "Alfa", "b": "Beta CS"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    assert i18n_parity.check(root) == []


def test_parity_catches_a_missing_target_key(tmp_path):
    strings = {"en": {"a": "Alpha", "b": "Beta"}, "cs": {"a": "Alfa"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    found = i18n_parity.check(root)
    assert len(found) == 1
    assert "missing from cs" in found[0].rule


def test_parity_catches_an_extra_target_key(tmp_path):
    strings = {"en": {"a": "Alpha"}, "cs": {"a": "Alfa", "ghost": "x"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    found = i18n_parity.check(root)
    assert len(found) == 1
    assert "missing from en" in found[0].rule


def test_parity_checks_every_declared_target(tmp_path):
    strings = {"en": {"a": "Alpha"}, "cs": {"a": "Alfa"}, "es": {}}
    root = _project(tmp_path, strings, _lines_for(strings), targets=("cs", "es"))
    found = i18n_parity.check(root)
    assert len(found) == 1
    assert "es" in found[0].rule


# --- i18n_untranslated --------------------------------------------------------

def test_untranslated_flags_an_identical_value(tmp_path):
    strings = {"en": {"greeting": "Welcome to the vault"}, "cs": {"greeting": "Welcome to the vault"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    found = i18n_untranslated.check(root)
    assert len(found) == 1
    assert "byte-identical" in found[0].rule


def test_untranslated_allows_a_short_residue_without_an_allowlist(tmp_path):
    """A bare hotkey label like `H` needs no allowlist entry."""
    strings = {"en": {"key.hp": "HP"}, "cs": {"key.hp": "HP"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    assert i18n_untranslated.check(root) == []


def test_untranslated_respects_the_allowlist(tmp_path):
    strings = {"en": {"brand": "Tigress"}, "cs": {"brand": "Tigress"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    (root / ".ai" / "allow.json").write_text('{"allow": ["brand"]}', encoding="utf-8")
    root = _project(tmp_path, strings, _lines_for(strings), allowlist=".ai/allow.json")
    assert i18n_untranslated.check(root) == []


# --- i18n_loanwords ------------------------------------------------------------

def test_loanwords_flags_a_denylisted_term(tmp_path):
    strings = {"en": {"desc": "Melee attack"}, "cs": {"desc": "melee útok"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    (root / ".ai" / "deny.json").write_text('{"terms": {"melee": {"cs": "boj zblízka"}}}', encoding="utf-8")
    root = _project(tmp_path, strings, _lines_for(strings), denylist=".ai/deny.json")
    found = i18n_loanwords.check(root)
    assert len(found) == 1
    assert "melee" in found[0].rule
    assert "boj zblízka" in found[0].rule


def test_loanwords_is_case_sensitive_and_whole_word(tmp_path):
    """An ALL-CAPS literal reference (e.g. to a tab named MELEE) is not a leak."""
    strings = {"en": {"desc": "See tab MELEE"}, "cs": {"desc": "viz záložka MELEE"}}
    root = _project(tmp_path, strings, _lines_for(strings))
    (root / ".ai" / "deny.json").write_text('{"terms": {"melee": {}}}', encoding="utf-8")
    root = _project(tmp_path, strings, _lines_for(strings), denylist=".ai/deny.json")
    assert i18n_loanwords.check(root) == []
