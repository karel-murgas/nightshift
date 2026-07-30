"""Shared types for gate modules.

`from nightshift.gates.base import Violation` — an installed-package import, so it
resolves the same whether the gate is core, lives in a project's `.ai/gates/`, is
driven by `run.py`, or is executed directly as a script. It used to be `from base
import Violation`, which worked only because `run.py` had put that one directory
on `sys.path` first; a gate run any other way could not import its own return type.

Gates still import their *neighbours* flat (`import doc_scan`), because those are
project modules in a directory that cannot be a package — see `run.py`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line} — {self.rule}"
