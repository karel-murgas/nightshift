"""Gate: the board view's oversize formula must agree with `runner.py`.

The board's Obsidian Bases view (`<board.root>.base`) marks a card in the
dispatch lane that has grown past `runner.CARD_COMFORT_BYTES`, by computing
`file.size` on every render. Nothing is written into any card, which is the
whole point of doing it in the view: there is no writer to keep in step and no
stored value to go stale, and compacting a card by hand clears its mark
immediately.

What that shape costs is two constants restated in YAML — the byte threshold
and the lane the filter names — which no import can reach across the
Python/Bases boundary. This gate is the import that cannot exist, in the same
shape `branch_role_prose` already uses to keep prose in step with
`[branches].integration`.

**The comment that would normally carry this cannot live in the `.base` file.**
Bases rewrites that file whenever the vault opens and its normalisation drops
YAML comments, so an explanation next to the literal is gone after the first
launch — the template's own header says so. A gate and `Board/README.md` are
therefore the only two places the reasoning can survive.

## What it checks

For each board view it can find:

* exactly one formula whose expression reads `file.size`;
* the threshold it compares against is `runner.CARD_COMFORT_BYTES`, and the
  comparison is the same strict `>` that `oversize_note` uses (`<=` returns no
  note), so the two agree on the boundary byte rather than only near it;
* the folders its `inFolder(...)` arguments name are exactly the lanes
  `oversize_note` actually marks, under the board root — **probed by calling
  it**, not restated here. Restating `"tasks"` in this module would make three
  copies of the lane rule out of two. The probe is what stops a lane added to
  `board.LANES` later from being silently uncovered by the view. Whole folders
  are compared rather than trailing lane names, because `inFolder("tasks")` and
  `inFolder("Board/tasks")` select different directories in Bases and a check
  that stripped the root would call them equal;
* the formula is referenced by name somewhere else in the file. A formula no
  view lists in its `order:` is computed and never drawn, which is the silent
  half of this feature failing.

## Which files

The consuming project's own view, at `<board.root>.base` from
`.ai/manifest.toml` — not a hardcoded `Board.base`, since the board root is
project-configurable and `init` names the view after it.

Plus `templates/board.base` **when the installed package lives inside the repo
being gated** — that is, when the framework is gating itself. The template and
a project's copy are not the same file and neither renders the other: `init`
stages the template once, so a fix to one never reaches the other. Checking
only whichever half is in front of you is how the two drift; checking the
template from a consuming project would instead reach across into somebody
else's checkout, which is why the test is containment rather than existence.

A repo with neither file is a no-op, not a failure. A project that does not
use Obsidian has no board view to keep honest.
"""
from __future__ import annotations

import re
from pathlib import Path

from nightshift import board, init, manifest, runner
from nightshift.gates.base import Violation

NAME = "board_view_sync"
FAST = True
DESCRIPTION = "the board view's oversize formula must agree with runner.CARD_COMFORT_BYTES"

_TEMPLATE = Path(init.__file__).resolve().parent / "templates" / "board.base"

# `{{board}}` is the template's unrendered board root — `init.render` substitutes it.
# Naming it here is what lets the template be checked for the same folders as a
# rendered copy, instead of being exempted for the one difference that is expected.
_BOARD_PLACEHOLDER = "{{board}}"

_THRESHOLD = re.compile(r"file\.size\s*>\s*(?P<bytes>\d+)")
_IN_FOLDER = re.compile(r'inFolder\(\s*"(?P<folder>[^"]+)"\s*\)')
_ENTRY = re.compile(r"^\s+(?P<name>[A-Za-z_][\w-]*)\s*:\s*(?P<value>\S.*?)\s*$")


def _drawn(text: str, name: str) -> bool:
    """Whether some view lists `formula.<name>` in its `order:`.

    A **list item**, not the substring anywhere in the file. `properties:` also
    keys the formula by its full name to give it a `displayName`, so a substring
    search is satisfied by the display config alone — it would report a formula
    as drawn while every `order:` list had lost it, which is exactly the state
    this check exists to find.
    """
    return re.search(rf"^\s*-\s*formula\.{re.escape(name)}\s*$", text, re.M) is not None


def check(repo_root: Path) -> list[Violation]:
    root = _board_root(repo_root)
    if root is None:
        return []

    violations: list[Violation] = []
    for path, board_root in _targets(repo_root, root):
        violations.extend(_check_file(path, repo_root, board_root))
    return violations


def _board_root(repo_root: Path) -> str | None:
    """The board directory name, or `None` when the manifest cannot be read.

    `None` is a no-op rather than a guessed `"Board"`: this gate checks a file
    named after a declared fact, and with no fact declared there is nothing here
    to name.

    Note what that covers, since it is wider than "no manifest": `load` raises
    for a defect in *any* table, so a broken `[worker]` silences this gate over a
    board view that is fine. That is `branch_role_prose._roles`' bargain too, and
    it is deliberately not special-cased here — a gate that re-read the file its
    own way would disagree with `doctor` about what a valid manifest is, which is
    a worse failure than being quiet while the manifest is already known-broken.
    """
    try:
        return manifest.load(repo_root).board.root
    except manifest.ManifestError:
        return None


def _targets(repo_root: Path, board_root: str) -> list[tuple[Path, str]]:
    """`(file, the board root it should name)` for every board view in this repo."""
    targets: list[tuple[Path, str]] = []
    view = repo_root / f"{board_root}.base"
    if view.is_file():
        targets.append((view, board_root))
    if _TEMPLATE.is_file() and _TEMPLATE.is_relative_to(repo_root.resolve()):
        targets.append((_TEMPLATE, _BOARD_PLACEHOLDER))
    return targets


def _check_file(path: Path, repo_root: Path, board_root: str) -> list[Violation]:
    try:
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:  # pragma: no cover - _targets only yields contained paths
        rel = path.name
    text = path.read_text(encoding="utf-8")
    formulas = _formulas(text)
    sized = [f for f in formulas if "file.size" in f[2]]

    if not sized:
        # Distinguished, because the two have different fixes and the wrong message
        # would send the reader looking for a formula that is sitting right there.
        # `file.size` in the text but in no parsed entry means the `formulas:` block
        # is not the flat `name: expression` shape this reads — a block scalar, say.
        unparsed = "file.size" in text
        return [Violation(rel, 1, (
            f"{NAME}: `file.size` appears here but in no entry of a `formulas:` block "
            f"this can read, so the threshold cannot be checked. Keep the formula as a "
            f"single quoted line") if unparsed else (
            f"{NAME}: no formula here reads `file.size`, so the board shows no mark on a "
            f"card over {runner.CARD_COMFORT_BYTES} bytes. Restore the `oversize` formula "
            f"from nightshift/templates/board.base"))]
    if len(sized) > 1:
        names = ", ".join(sorted(name for _, name, _ in sized))
        return [Violation(rel, sized[1][0], (
            f"{NAME}: {len(sized)} formulas read `file.size` ({names}). One is the size "
            f"mark; a second copy is the drift this gate exists to prevent"))]

    lineno, name, expr = sized[0]
    violations: list[Violation] = []

    sizes = {int(m.group("bytes")) for m in _THRESHOLD.finditer(expr)}
    if sizes != {runner.CARD_COMFORT_BYTES}:
        found = ", ".join(str(s) for s in sorted(sizes)) or "no `file.size > N` comparison"
        violations.append(Violation(rel, lineno, (
            f"{NAME}: formula `{name}` compares against {found}, but "
            f"runner.CARD_COMFORT_BYTES is {runner.CARD_COMFORT_BYTES}. The board would "
            f"mark a different set of cards than the runner reports")))

    folders = frozenset(m.group("folder") for m in _IN_FOLDER.finditer(expr))
    expected = frozenset(f"{board_root}/{lane}" for lane in _marked_lanes())
    if folders != expected:
        violations.append(Violation(rel, lineno, (
            f"{NAME}: formula `{name}` selects {_show(folders)}, but "
            f"runner.oversize_note marks {_show(expected)}. Name the same folder(s), or "
            f"the board is silent about a lane the runner is not")))

    if not _drawn(text, name):
        violations.append(Violation(rel, lineno, (
            f"{NAME}: no view lists `- formula.{name}` in its `order:`, so nothing draws "
            f"it. The formula is computed and thrown away")))

    return violations


def _formulas(text: str) -> list[tuple[int, str, str]]:
    """`(line number, name, expression)` for every entry of the `formulas:` block.

    Hand-parsed rather than loaded with a YAML library, because the framework
    takes no dependency it does not need and the block is one level of flat
    `name: expression` pairs. The expression is returned with its surrounding
    YAML quotes stripped; nothing here interprets it further.
    """
    out: list[tuple[int, str, str]] = []
    inside = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            inside = line.strip() == "formulas:"
            continue
        if not inside:
            continue
        m = _ENTRY.match(line)
        if m:
            out.append((lineno, m.group("name"), _unquote(m.group("value"))))
    return out


def _unquote(value: str) -> str:
    """A single-line YAML scalar, with its quoting removed.

    The `\\"` case is not hypothetical tidiness: this file is committed with the
    expression in a *single*-quoted scalar, and Obsidian rewrites it on every
    vault open. If a rewrite ever picks double quotes, every `inFolder("...")`
    inside becomes `inFolder(\\"...\\")` — a semantically identical formula that
    a naive read would report as naming no folder at all.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        inner = value[1:-1]
        return inner.replace('\\"', '"') if value[0] == '"' else inner.replace("''", "'")
    return value


def _marked_lanes() -> frozenset[str]:
    """The lanes `runner.oversize_note` actually marks, by asking it.

    Probed rather than restated. `oversize_note` deliberately holds the lane rule
    inside the helper so a second caller cannot get it wrong, and a gate that
    re-typed `"tasks"` would be exactly that second caller.
    """
    oversized = "x" * (runner.CARD_COMFORT_BYTES + 1)
    return frozenset(
        lane for lane in board.LANES
        if runner.oversize_note(
            board.Card(path=Path(lane) / "probe.md", lane=lane, fields={}, text=oversized))
    )


def _show(folders: frozenset[str]) -> str:
    return ", ".join(f"`{folder}`" for folder in sorted(folders)) or "no folder"
