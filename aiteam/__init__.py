"""The AI-team framework, extracted from Dungeoneer's `.ai/` (07_portability.md).

Deliberately empty of re-exports. Every module here is imported by its own name
(`from aiteam import textio`, `from aiteam.gates.base import Violation`) so that
importing one costs nothing from the others — `textio` in particular is imported
by almost everything and must stay dependency-free and import-order-safe.
"""

__version__ = "0.1.0"
