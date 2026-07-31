"""Gate: flag specific denylisted base-language words leaking into a target
language's prose.

Behind the adapter interface (07_portability.md §2b) — see `i18n_parity`'s
docstring for what that means. The denylist path comes from the project's
manifest (`i18n.loanwords_denylist`); it is a project's own content, not
core's, and deliberately narrow rather than a general dictionary scan — a
broad word-overlap scan was tried in Dungeoneer and produced mostly legitimate
cognates and loanwords, re-classified as needing linguistic judgment rather
than a Tier-1 gate.

Matching is case-sensitive and whole-word, so an ALL-CAPS literal reference to
something named after the denylisted word (Dungeoneer's `MELEE` help-catalog
tab, referenced by name in running prose) is not treated as a leak while a
lowercase `melee` is.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import i18n_adapter_loader
from nightshift.gates.base import Violation

NAME = "i18n_loanwords"
FAST = True
DESCRIPTION = "denylisted base-language terms must not appear (case-sensitive, whole-word) in target values"


def _load_denylist(repo_root: Path, rel: str) -> dict[str, dict]:
    if not rel:
        return {}
    path = repo_root / rel
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["terms"]


def check(repo_root: Path) -> list[Violation]:
    loaded = i18n_adapter_loader.load(repo_root)
    if loaded is None:
        return []
    config, adapter = loaded.config, loaded.adapter

    strings = adapter.load_strings(repo_root)
    lines = adapter.zone_key_lines(repo_root)
    rel = str(adapter.i18n_path(repo_root).relative_to(repo_root))
    denylist = _load_denylist(repo_root, config.loanwords_denylist)

    violations: list[Violation] = []
    for lang in config.targets:
        d = strings[lang]
        for term, meta in denylist.items():
            pattern = re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])")
            for key, value in d.items():
                if pattern.search(value):
                    ln = lines[lang].get(key, 0)
                    suggestion = meta.get(lang)
                    hint = f" — use {suggestion!r} instead" if suggestion else ""
                    violations.append(
                        Violation(
                            rel,
                            ln,
                            f"i18n_loanwords: {lang}['{key}'] contains banned term '{term}'{hint} ({value!r})",
                        )
                    )
    return violations
