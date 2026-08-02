"""Gate: a declared one-way dependency between packages stays one-way.

One row of `[layering].forbid` says `importer` must not import `imports`, both
dotted module prefixes:

    [[layering.forbid]]
    importer = "myapp.core"     # nothing under myapp.core …
    imports  = "myapp.ui"       # … may import anything under myapp.ui

Architecture rules of this shape are the ones every project writes in prose and
nothing enforces — "game logic must not import rendering", "the domain layer must
not import the web layer". They are also trivially checkable from the AST, which is
exactly `00_architecture.md` §12's line: what Python can do, Python should do.

**Relative imports are resolved, not pattern-matched.** `from ..rendering import x`
inside `myapp/combat/melee.py` is `myapp.rendering`, and the version of this gate
that shipped in Dungeoneer approximated that with a `startswith("rendering")` test
on the raw module string — which is right for one level and wrong for two, and
silently right-looking either way. Resolving properly costs four lines.

**`nightshift init --layering` proposes rows the tree already satisfies**, so
adopting them can never turn the gate red on the day it is written. That makes the
rows a snapshot of the architecture as built, offered as something to keep true —
not an opinion about what it should be. Read them anyway: each one is a claim.

Portable since 07_portability.md §2 classified it as manifest-table. It stayed
project-side one session too long, with the `[layering]` schema table it should have
been reading present in `manifest.py` and read by nothing.
"""
from __future__ import annotations

import ast
from pathlib import Path

from nightshift.gates import corpus
from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError
from nightshift.manifest import load as load_manifest

NAME = "import_layering"
FAST = True
DESCRIPTION = "a declared one-way dependency between packages stays one-way"


def _module_name(rel: Path) -> str:
    """`a/b/c.py` → `a.b.c`; `a/b/__init__.py` → `a.b`."""
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _targets(node: ast.AST, module: str) -> list[str]:
    """Absolute dotted names a single import statement reaches for.

    A relative import is resolved against the *containing package* of `module`:
    level 1 is that package, each further level strips one more component. `from
    . import x` names no module of its own, so each imported alias is treated as a
    submodule — otherwise `from . import rendering` slips through a rule written
    against `pkg.rendering`.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    where = node.module or ""
    if not node.level:
        return [where] if where else []
    package = module.split(".")[:-1]
    base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
    if where:
        return [".".join([*base, where])]
    return [".".join([*base, alias.name]) for alias in node.names]


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def check(repo_root: Path) -> list[Violation]:
    try:
        manifest = load_manifest(repo_root)
    except ManifestError:
        return []
    rules = manifest.layering
    if not rules:
        return []

    violations: list[Violation] = []
    for source_dir in manifest.project.source_dirs:
        base = repo_root / source_dir
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo_root)
            module = _module_name(rel)
            applicable = [r for r in rules
                          if _under(module, r.importer)
                          and not any(_under(module, e) for e in r.exempt)]
            if not applicable:
                continue
            tree = corpus.tree(path)
            if tree is None:
                continue
            rel_str = rel.as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for target in _targets(node, module):
                    for rule in applicable:
                        if _under(target, rule.imports):
                            violations.append(Violation(
                                rel_str, node.lineno,
                                f"{NAME}: `{rule.importer}` must not import "
                                f"`{rule.imports}` — this reaches `{target}`. The "
                                f"dependency is declared one-way in "
                                f"`[layering].forbid`; invert the call (an event, a "
                                f"callback, a parameter) or change the rule "
                                f"deliberately.",
                            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
