"""Shared machinery for the three i18n gates: resolve a project's `[i18n]`
manifest table and load its adapter module (07_portability.md §2b).

The adapter is the one seam in this whole system where a project's *data
model* differs rather than its paths — some projects keep translations in a
Python dict literal (Dungeoneer's `core/i18n.py`), others in `.po` files or
JSON-per-language. Three functions are the entire contract:

  * `load_strings(repo_root) -> {lang: {key: value}}` — the authoritative values.
  * `zone_key_lines(repo_root) -> {lang: {key: line}}` — for `file:line` reporting.
  * `i18n_path(repo_root) -> Path` — the file a violation's `file:line` is against.

Loaded by path (`importlib.util.spec_from_file_location`), not by package
import: the adapter is a project file named in the manifest
(`i18n.adapter = ".ai/i18n_adapter.py"`), and `.ai` cannot be a Python package
(a leading dot is not an identifier) — the same reason `bridge.py` and the
gate directories themselves are imported flat rather than as packages.

A project with no `[i18n]` table gets `None` from `load` — not an empty
config, an absence. `i18n_parity`, `i18n_untranslated` and `i18n_loanwords`
all treat that as "nothing to check" and return no violations, which is how a
project with no localisation gets these three gates for free without ever
seeing them fire.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from nightshift.manifest import I18n, ManifestError
from nightshift.manifest import load as load_manifest


@dataclass(frozen=True)
class Loaded:
    config: I18n
    adapter: ModuleType


def _load_adapter(repo_root: Path, config: I18n) -> ModuleType:
    path = repo_root / config.adapter
    spec = importlib.util.spec_from_file_location("_nightshift_i18n_adapter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load(repo_root: Path) -> Loaded | None:
    """`None` when this project has no manifest, or no `[i18n]` table."""
    try:
        manifest = load_manifest(repo_root)
    except ManifestError:
        return None
    if manifest.i18n is None:
        return None
    return Loaded(manifest.i18n, _load_adapter(repo_root, manifest.i18n))
