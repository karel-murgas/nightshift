"""Gate: a target-language value byte-identical to the base language must be
allowlisted, or it is flagged as an untranslated leftover.

Behind the adapter interface (07_portability.md §2b) — see `i18n_parity`'s
docstring for what that means. The allowlist path comes from the project's
manifest (`i18n.untranslated_allowlist`), because a value list that ships
inside the gate gets edited to make a run pass; a project's exemptions are
its own content, not core's.

In Dungeoneer, where this gate was earned: 01_audit_findings.md row 5 note —
measuring the real codebase found 65 cs / 66 es identical-value keys, mostly
single-letter hotkey labels, auto-exempted below by a short-residue rule
rather than needing a named allowlist entry each. The remaining ones needed a
human call: a key is allowlisted only when every target language independently
kept it identical to the base (proper nouns, established loanwords — a
convention translators agreed on), never when only one language did, which is
strong evidence of a genuine miss.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import i18n_adapter_loader
from nightshift.gates.base import Violation

NAME = "i18n_untranslated"
FAST = True
DESCRIPTION = "a target-language value identical to the base must be allowlisted, or it's untranslated"

_PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")
_STRIP_RE = re.compile(r"[0-9¥%×—\-–/.,+:*\[\]()\s?▪]")


def _residue(value: str) -> str:
    """value with {placeholders}, digits, and punctuation/symbols removed.

    A residue of length <=2 is treated as a bare hotkey/abbreviation label and
    is exempt without needing an allowlist entry; anything longer needs one.
    """
    v = _PLACEHOLDER_RE.sub("", value)
    v = _STRIP_RE.sub("", v)
    return v


def _load_allowlist(repo_root: Path, rel: str) -> set[str]:
    if not rel:
        return set()
    path = repo_root / rel
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data["allow"])


def check(repo_root: Path) -> list[Violation]:
    loaded = i18n_adapter_loader.load(repo_root)
    if loaded is None:
        return []
    config, adapter = loaded.config, loaded.adapter

    strings = adapter.load_strings(repo_root)
    lines = adapter.zone_key_lines(repo_root)
    rel = str(adapter.i18n_path(repo_root).relative_to(repo_root))
    allow = _load_allowlist(repo_root, config.untranslated_allowlist)
    base = strings[config.base]

    violations: list[Violation] = []
    for lang in config.targets:
        d = strings[lang]
        for key, base_value in base.items():
            if key not in d:
                continue  # i18n_parity's job
            value = d[key]
            if value != base_value:
                continue
            if len(_residue(base_value)) <= 2:
                continue
            if key in allow:
                continue
            ln = lines[lang].get(key, lines[config.base].get(key, 0))
            violations.append(
                Violation(
                    rel,
                    ln,
                    f"i18n_untranslated: {lang}['{key}'] is byte-identical to {config.base} "
                    f"({base_value!r}) and not on the allowlist",
                )
            )
    return violations
