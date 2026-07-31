"""Gate: `set(base) == set(target)` for every target language in a project's
`[i18n]` manifest table.

Behind the adapter interface (07_portability.md §2b): a project supplies
`load_strings`/`zone_key_lines`/`i18n_path`, and this gate is the same set
algebra regardless of whether those strings live in a Python dict literal,
`.po` files, or JSON-per-language. In Dungeoneer, where this gate was earned:
01_audit_findings.md row 5 — no global parity check existed before it, only
hand-listed key tuples in ~8 scattered tests covering a fraction of ~1351 keys.

A project with no `[i18n]` table gets nothing from this gate — not a crash,
not a false pass, just silence, because there is nothing to compare.
"""
from __future__ import annotations

from pathlib import Path

import i18n_adapter_loader
from nightshift.gates.base import Violation

NAME = "i18n_parity"
FAST = True
DESCRIPTION = "every declared target language's key set must match the base language's exactly"


def check(repo_root: Path) -> list[Violation]:
    loaded = i18n_adapter_loader.load(repo_root)
    if loaded is None:
        return []
    config, adapter = loaded.config, loaded.adapter

    strings = adapter.load_strings(repo_root)
    lines = adapter.zone_key_lines(repo_root)
    rel = str(adapter.i18n_path(repo_root).relative_to(repo_root))

    base_keys = set(strings[config.base])
    violations: list[Violation] = []

    for lang in config.targets:
        lang_keys = set(strings[lang])

        for key in sorted(base_keys - lang_keys):
            ln = lines[config.base].get(key, 0)
            violations.append(
                Violation(rel, ln, f"i18n_parity: key '{key}' exists in {config.base} but missing from {lang}")
            )
        for key in sorted(lang_keys - base_keys):
            ln = lines[lang].get(key, 0)
            violations.append(
                Violation(rel, ln, f"i18n_parity: key '{key}' exists in {lang} but missing from {config.base}")
            )

    return violations
