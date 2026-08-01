"""Gate: no `subprocess` call under `.ai/` decodes with the locale codec.

**Earned by an observed failure, not by being easy** (`00_architecture.md` §15).
On the runner's first real night, 2026-07-23, the `ice-damage` card did complete,
correct work — a commit, 1533 tests passing, its own gates green — and was filed
as *failed* with the reason `"gates: "`. Nothing after the colon.

The chain, because every link is reusable and none of it is about git:

1. `deletion_sweep._git` ran `git show <base>:dungeoneer/core/i18n.py` with
   `text=True` and no `encoding=`. `text=True` alone decodes using the **locale**
   codec, which is `cp1252` on this machine.
2. `i18n.py` carries 245 bytes cp1252 has no mapping for — the cs and es
   translations, which `CLAUDE.md` *requires* every user-visible string to have.
3. The `UnicodeDecodeError` was raised inside subprocess's **reader thread**,
   where `run()` cannot observe it. So it did not propagate: `.stdout` simply
   came back `None`.
4. `ast.parse(None)` raised `TypeError`, which `_top_level_names` did not catch,
   which took `gates/run.py` down with a traceback on **stderr**.
5. The runner built its failure reason from stdout only, so the card's `## Error`
   recorded an empty string.

The rule is therefore: **`text=True` without `encoding=` is a latent crash whose
trigger is somebody adding a non-Latin-1 string to the repo.** Every one of the
18 sites under `.ai/` had it. It fired on the one that happened to read the
biggest file with the most diacritics, and it would have fired on the others in
turn — a card that touches `i18n.py` is the *normal* case here, not the exotic
one, which is why this cost a whole night's queue rather than one card.

Scope: `.ai/` only — the orchestrator, where a silent failure costs a night, and
where §12 asks the code to be dumb and predictable. Game code under
`dungeoneer/` does not shell out.

Appeals: `# gate-ok(subprocess_encoding): <reason>` — see `appeal_markers.py` and
`10_self_improvement.md` §4. A genuine appeal is rare: if output really is
binary, pass neither `text=True` nor `encoding=` and handle bytes.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates import corpus
from nightshift.gates.base import Violation

NAME = "subprocess_encoding"
FAST = True
DESCRIPTION = "no subprocess call under .ai/ decodes output with the locale codec"

_ROOT = Path(".ai")
_WATCHED = frozenset({"run", "call", "Popen", "check_output"})
# Both spellings select text mode. `universal_newlines` is the older alias and is
# still accepted, so a gate that only knew `text` would miss half the ways in.
_TEXT_KWARGS = frozenset({"text", "universal_newlines"})


def _subprocess_attr(node: ast.Call) -> str | None:
    """The attribute name if `node` is `subprocess.<watched>(...)`."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _WATCHED:
        value = func.value
        if isinstance(value, ast.Name) and value.id == "subprocess":
            return func.attr
    return None


def _asks_for_text(node: ast.Call) -> str | None:
    """The kwarg that puts this call in text mode, if any is literally `True`.

    Only a literal `True` counts. A variable could be either, and a gate that
    guessed would produce false positives on code that already decided
    correctly — the failure this gate exists for was written as a plain
    `text=True`, and so is every other site under `.ai/`.
    """
    for kw in node.keywords:
        if kw.arg in _TEXT_KWARGS and isinstance(kw.value, ast.Constant) \
                and kw.value.value is True:
            return kw.arg
    return None


def _has_encoding(node: ast.Call) -> bool:
    """`encoding=` present at all. Its *value* is not inspected: anything
    explicit is a decision, and the defect is the absence of one."""
    return any(kw.arg == "encoding" for kw in node.keywords)


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []
    base = repo_root / _ROOT
    if not base.is_dir():
        return violations

    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(repo_root).as_posix()
        tree = corpus.tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            attr = _subprocess_attr(node)
            if attr is None:
                continue
            kwarg = _asks_for_text(node)
            if kwarg is None or _has_encoding(node):
                continue
            if appeal_markers.exempt(appeals, NAME, rel, node.lineno):
                continue
            violations.append(Violation(
                rel, node.lineno,
                f"subprocess.{attr}(..., {kwarg}=True) without `encoding=` decodes "
                f"with the locale codec (cp1252 on Windows); a byte it cannot map "
                f"makes .stdout None inside the reader thread instead of raising. "
                f"Pass encoding=\"utf-8\", errors=\"replace\", or appeal with "
                f"`# gate-ok({NAME}): <reason>`",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
