"""Generates the marked blocks inside a README.md — the gate list, the runner's
flags, and the manifest field table — from the live code rather than by hand.

07_portability.md §8 D4: "any list in the README ... is generated from the
code that owns it, and a gate checks the README against a fresh generation."
The sweep before that session found the worker roster and the ★ backlog each
retyped in two prose homes, and both had drifted — this is the mechanism that
makes a third instance of that impossible rather than merely discouraged.

A block is a span `<!-- generated:NAME -->` ... `<!-- /generated:NAME -->`.
Free text around a span is untouched; `NAME` with no registered generator
below is left untouched too, rather than guessed at — `readme_generated`
reports the block as stale by content, not by name, so a typo'd marker name
simply never matches anything and never blocks anyone.

Every generator takes the repo root being documented, so the same functions
serve two audiences with one implementation: this package's own README (no
`.ai/gates/` of its own, no manifest — `runner_flags` falls back to a
synthetic one so the flag list can still be built) and a consuming project
that adopts the same convention for its own README, where `gate_list` then
picks up that project's own gates alongside core's.
"""
from __future__ import annotations

import argparse
import dataclasses
import re
import tempfile
from pathlib import Path

from nightshift import board as _board
from nightshift import manifest as _manifest
from nightshift import runner as _runner
from nightshift.gates.run import discover
from nightshift.manifest import ManifestError

_MARKER = re.compile(
    r"<!-- generated:(?P<name>[\w-]+) -->\n"
    r"(?P<body>.*?)"
    r"\n<!-- /generated:(?P=name) -->",
    re.DOTALL,
)


def _first_sentence(text: str | None) -> str:
    """The first `". "`-terminated sentence of a docstring or help string,
    collapsed to one line. Good enough for a reference-table cell; the rest
    of the doc is one click away in the source."""
    flat = " ".join((text or "").split())
    idx = flat.find(". ")
    return flat[: idx + 1] if idx != -1 else flat


def gate_list(root: Path) -> str:
    """One row per gate `discover(root)` finds — core plus, when `root` has
    one, that project's own `.ai/gates/` — name and its `DESCRIPTION` (or the
    first sentence of its docstring, for the couple of gates that predate that
    constant)."""
    gates = discover(root)
    lines = ["| Gate | What it checks |", "|---|---|"]
    for name in sorted(gates):
        module = gates[name]
        description = getattr(module, "DESCRIPTION", None) or _first_sentence(module.__doc__)
        lines.append(f"| `{name}` | {description} |")
    return "\n".join(lines)


def runner_flags(root: Path) -> str:
    """One row per `python -m nightshift.runner` flag, read from the real
    `argparse.ArgumentParser` rather than copied prose — `--budget` and
    `--card-budget` already changed meaning once (`_parser`'s own docstring).

    `_parser` resolves `--base`'s default through `[branches].integration`,
    which this package's own repo does not declare (it is a framework, not a
    consuming project). A synthetic manifest stands in only for that; the
    flags and their help text are the package's own regardless of `root`.
    """
    try:
        parser = _runner._parser(root)
    except ManifestError:
        with tempfile.TemporaryDirectory(prefix="nightshift-readme-") as tmp:
            synthetic = Path(tmp)
            (synthetic / ".ai").mkdir()
            (synthetic / ".ai" / "manifest.toml").write_text(
                '[branches]\nintegration = "<your-integration-branch>"\n',
                encoding="utf-8", newline="",
            )
            parser = _runner._parser(synthetic)

    lines = ["| Flag | Meaning |", "|---|---|"]
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction) or not action.option_strings:
            continue
        flags = ", ".join(f"`{opt}`" for opt in action.option_strings)
        lines.append(f"| {flags} | {_first_sentence(action.help)} |")
    return "\n".join(lines)


# Which dataclass documents which `.ai/manifest.toml` table. `layering`'s known
# key (`forbid`) is a list of `LayeringRule` rows rather than a table of its
# own scalar fields, so it is handled separately below.
_TABLE_CLASSES: dict[str, type] = {
    "project": _manifest.Project,
    "tests": _manifest.Tests,
    "branches": _manifest.Branches,
    "board": _manifest.Board,
    "worker": _manifest.Worker,
    "memory": _manifest.Memory,
    "i18n": _manifest.I18n,
    "dead_code": _manifest.DeadCode,
    "audit": _manifest.Audit,
    "tiers": _manifest.Tiers,
}


def manifest_fields() -> str:
    """One row per `.ai/manifest.toml` table, read from `manifest.schema()`
    and the dataclass that owns it — so a field `validate()` stops accepting,
    or a new one it starts accepting, cannot silently leave this table wrong."""
    lines = ["| Table | Fields | Purpose |", "|---|---|---|"]
    for table, known_fields in _manifest.schema().items():
        table_label = f"[{table}]"
        if table == "layering":
            row_fields = [f.name for f in dataclasses.fields(_manifest.LayeringRule)]
            cells = f"`forbid = [{{{', '.join(row_fields)}}}]`"
            purpose = _first_sentence(_manifest.LayeringRule.__doc__)
        elif table == "accounts":
            table_label = "[[accounts]]"
            row_fields = [f.name for f in dataclasses.fields(_manifest.Account)]
            cells = f"one row per account: `{{{', '.join(row_fields)}}}`"
            purpose = _first_sentence(_manifest.Account.__doc__)
        else:
            cls = _TABLE_CLASSES[table]
            by_name = {f.name: f for f in dataclasses.fields(cls)}
            parts = []
            for name in known_fields:
                f = by_name[name]
                if f.default is dataclasses.MISSING:
                    parts.append(f"`{name}` (required)")
                else:
                    parts.append(f"`{name}={f.default!r}`")
            cells = ", ".join(parts)
            purpose = _first_sentence(cls.__doc__)
        lines.append(f"| `{table_label}` | {cells} | {purpose} |")
    return "\n".join(lines)


def board_lanes() -> str:
    """One row per `board.LANES` entry, in dispatch order. Lane names are
    framework config, not a project fact (07_portability.md §8 D5), which is
    exactly why they belong here rather than in a per-project manifest table."""
    lines = ["| Lane | Order |", "|---|---|"]
    lines.extend(f"| `{lane}` | {i} |" for i, lane in enumerate(_board.LANES, 1))
    return "\n".join(lines)


def _generate(name: str, root: Path) -> str | None:
    if name == "gate-list":
        return gate_list(root)
    if name == "runner-flags":
        return runner_flags(root)
    if name == "manifest-fields":
        return manifest_fields()
    if name == "board-lanes":
        return board_lanes()
    return None


def block_names(text: str) -> list[str]:
    return [m.group("name") for m in _MARKER.finditer(text)]


def stale_blocks(root: Path, text: str) -> list[tuple[str, int]]:
    """`(block name, 1-based line of its opening marker)` for every generated
    block whose committed content no longer matches a fresh generation. A
    marker naming no registered generator is skipped, not reported — see the
    module docstring."""
    stale: list[tuple[str, int]] = []
    for m in _MARKER.finditer(text):
        name = m.group("name")
        fresh = _generate(name, root)
        if fresh is None:
            continue
        if m.group("body") != fresh:
            stale.append((name, text.count("\n", 0, m.start()) + 1))
    return stale


def render(root: Path, text: str) -> str:
    """`text` with every generated block's body replaced by a fresh
    generation. A marker naming no registered generator is left byte-for-byte
    as it was."""
    def _sub(match: re.Match) -> str:
        name = match.group("name")
        fresh = _generate(name, root)
        if fresh is None:
            return match.group(0)
        return f"<!-- generated:{name} -->\n{fresh}\n<!-- /generated:{name} -->"

    return _MARKER.sub(_sub, text)


def main(argv: list[str] | None = None) -> int:
    import argparse as _argparse

    parser = _argparse.ArgumentParser(
        description="Regenerate README.md's generated blocks from the code that owns each list.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo whose README.md to check (default: found from cwd)")
    parser.add_argument("--write", action="store_true",
                        help="write the regenerated file in place; default only reports")
    args = parser.parse_args(argv)

    root = (args.root or _manifest.find_root()).resolve()
    path = root / "README.md"
    if not path.is_file():
        print(f"{path} does not exist — nothing to generate")
        return 0
    text = path.read_text(encoding="utf-8")
    stale = stale_blocks(root, text)

    if args.write:
        if stale:
            path.write_text(render(root, text), encoding="utf-8", newline="")
        print(f"regenerated {len(stale)} block(s)" if stale else "already current")
        return 0

    if stale:
        for name, line in stale:
            print(f"README.md:{line} — `{name}` is stale")
        return 1
    print("README.md's generated blocks are current")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
