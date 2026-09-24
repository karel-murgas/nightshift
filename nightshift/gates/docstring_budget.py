"""Gate: a module docstring stays under a line budget, ratcheted by a baseline.

docstring-and-manifest-diet (2026-09-23 framework review) found 43% of nightshift's
lines are prose — 13.8k lines of docstrings, mostly dated incident narrative that
the staleness gates never look at because they scan `docs/`, not docstrings. Slice 1
fixed the sites that were factually wrong (`digest.py`, since deleted, surviving as a
live-sounding reference, plus false counts) and added `prose_reference_liveness` to
keep dead references from coming back. Neither stops a docstring from simply being
long — a module docstring
can cite only real things and still run to 60 lines of history nobody needs to load
to use the module. This gate is the guard for *that*: a module docstring should say
what the module is, what it guarantees, and what not to do, in about `BUDGET_LINES`.

**Function and class docstrings are out of scope.** House style here is long,
discursive module docstrings; a function's or a method's is a different kind of
prose and was never the thing measured.

## The ratchet, not a hard cap

A codebase this size cannot go from "43% prose" to "every docstring under budget"
in one commit, and a gate that turns every existing over-budget module red on
arrival gets appealed into silence within a day (`.ai/recipes/verify-before-
shipping-a-rule.md` step 3's whole argument). So the rule is a ratchet, not a cap:

* A module **not** in the baseline, over budget → violation. New prose earns its
  length or stays under it; it does not get to start long.
* A module **in** the baseline, docstring line count grew past the recorded
  count → violation ("grown"). The baseline is a snapshot of where things stood
  when the ratchet was switched on, not a ceiling anyone may spend up to again.
* A module in the baseline whose docstring is now at or under `BUDGET_LINES` →
  violation ("remove from baseline"). The chore that shortens a module is not
  finished until the baseline stops carrying it — leaving the entry behind would
  let it silently regrow back toward the old count with no gate noticing.
* A module in the baseline, still over budget, but not grown past its recorded
  count → no violation. Shrinking it further is encouraged, not required by this
  gate; `docstring-diet-<group>` chore cards are where that work happens.

The baseline can be regenerated (`python -m nightshift.gates.docstring_budget
--refresh`), and the regeneration itself only ever lowers a recorded count (never
raises one, even if the measured count is higher) — the invariant the module name
promises. A count that needs to go up is a deliberate, visible manifest-style edit
in its own commit, the same bargain `orientation_budget`'s `budget_bytes` offers,
not something a bulk refresh should ever do by accident.

## Two baselines, because there are two kinds of tooling tree

`nightshift.gates.scope.tooling_dirs` says which directories are tooling in a
given repo: `.ai/` always, plus whatever `[project].tooling_dirs` declares (empty
for almost every consuming project; `["nightshift"]` in this package's own repo,
which is what finally puts the framework's own source inside its own rules —
`scope.py`'s docstring tells that story). A baseline is a promise about *this
package's* line counts, so it has to live with the package regardless of which
repo happens to be scanning it, while a baseline for `path/to/repo/.ai/gates/*.py`
has to live in that repo. Hence two homes rather than one:

* **`nightshift/gates/data/docstring_budget_baseline.json`** — this file, next to
  this module, ships with the package and covers every `.py` under the declared
  `nightshift`-shaped tooling dir (only reachable when the repo being scanned is
  this package's own checkout, since that is the only place `tooling_dirs`
  currently names anything besides `.ai/`).
* **`<repo>/.ai/gates/data/docstring_budget_baseline.json`** — a consuming
  project's own, covering its `.ai/` tree. `nightshift init` does not write one;
  a project earns it the day this gate finds something over budget worth
  grandfathering, the same way `.ai/gates/` itself starts empty.

Keys are repo-relative POSIX paths, matching `Violation.file`.

## Dated narrative, on the side

Slice 1's motivating failure was largely dated incident narrative wandering into
prose (`"deleted 2026-09-02"`, `"measured 2026-08-03"`, ...). A docstring citing a
handful of dates for real, load-bearing reasons (a number that was actually
measured on a date, cited once) is fine; a docstring built mostly out of dated
sentences is the shape `08_staleness.md` calls a log, at module-docstring scale.
Cheap to add on top of the line count — same AST walk, one more regex — so it is
flagged too, but **only for a module not already baselined**: every baselined
module is over budget for close to this exact reason, and flagging all of them
unconditionally the moment this gate ships would be the "not low-noise" case
this feature is explicitly optional against (measured: 12 of 84 baselined core
modules, at `DATE_THRESHOLD`, the day the baseline was generated). Paying that
down is what the slice-2 `docstring-diet-<group>` chores are for; a module not
yet over budget citing `DATE_THRESHOLD` or more ISO dates is still checked, so
new prose cannot re-earn the failure the baseline is grandfathering away.
"""
from __future__ import annotations

SUBJECT = "nightshift/gates/data/docstring_budget_baseline.json"

import argparse
import ast
import json
import re
import sys as _sys
from pathlib import Path

# `run.py` puts this directory on `sys.path` so neighbour gates can `import
# appeal_markers` flat; a direct `python -m nightshift.gates.docstring_budget`
# (used by `--refresh`, and by hand while iterating) has not gone through that,
# so this module does its own -- harmless and idempotent when run.py already did.
_sys.path.insert(0, str(Path(__file__).resolve().parent))

import appeal_markers
from nightshift.gates import corpus, scope
from nightshift.gates.base import Violation
from nightshift.manifest import AI_DIR

NAME = "docstring_budget"
DESCRIPTION = ("a module docstring stays under a line budget unless baselined, and "
               "a baselined module now under budget must be removed from the baseline")

#: A module docstring longer than this, unbaselined, is a violation. Not a manifest
#: field (docstring-and-manifest-diet slice 3): a `[docstrings]` table would need
#: schema, `_KNOWN_TABLES`, doctor and readme_gen changes for one integer that has
#: never needed to differ per project, so a module constant is the cheap version of
#: the same override — a repo that genuinely needs a different number edits this
#: line, in a visible diff, exactly like any other constant here.
BUDGET_LINES = 15

#: Three or more ISO dates in one module docstring reads as narrative rather than a
#: fact worth one citation. Set above `orientation_shape.THRESHOLD` (3, for dated
#: *headings* in a whole file) on purpose — a single module docstring is a much
#: smaller surface than a whole memory file, so the same absolute count is a
#: stricter bar per line, which is the right direction for prose that is supposed
#: to fit in ~15 lines to begin with.
DATE_THRESHOLD = 3

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

#: This module's own directory name, relative to the installed package --
#: `gates`, with `docstring_budget.py` living directly in it. Used only to
#: build `_core_baseline_path` from a *repo_root*, never from `__file__`; see
#: that function for why the distinction matters.
_CORE_BASELINE_REL = ("nightshift", "gates", "data", "docstring_budget_baseline.json")


def _local_baseline_path(repo_root: Path) -> Path:
    return repo_root / AI_DIR / "gates" / "data" / "docstring_budget_baseline.json"


def _core_baseline_path(repo_root: Path) -> Path:
    """The framework's own baseline, resolved *inside `repo_root`* rather than
    from this module's installed `__file__`.

    The two coincide when `repo_root` is this package's own checkout -- the
    only place `nightshift/*.py` is ever in `scope.tooling_files(repo_root)`'s
    output, since that requires `[project].tooling_dirs = ["nightshift"]`,
    which only this package's own manifest declares. They do NOT coincide for
    a consuming project's `tmp_path` fixture, or for a real consuming repo:
    resolving from `__file__` there would silently read and -- worse, on
    `refresh()` -- overwrite the actual installed package's committed
    baseline as a side effect of scanning an unrelated tree. That happened
    once, mid-slice: `refresh()` on a repo with no `nightshift/` tooling dir
    computed an empty `core_new` and wrote it straight over the real file
    through the `__file__`-derived path, twice, before this fixed it -- gate-
    self-bug-shaped, and now structurally impossible, since a repo_root with
    no `nightshift/gates/` cannot resolve to any real file to overwrite.
    """
    return repo_root.joinpath(*_CORE_BASELINE_REL)


def _load_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}


def _write_baseline(path: Path, baseline: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = {k: baseline[k] for k in sorted(baseline)}
    path.write_text(json.dumps(ordered, indent=2, sort_keys=True) + "\n",
                     encoding="utf-8", newline="")


def _is_local(path: Path, repo_root: Path) -> bool:
    try:
        path.relative_to(repo_root / AI_DIR)
        return True
    except ValueError:
        return False


def _module_docstring(path: Path) -> tuple[str, int] | None:
    """`(docstring text, its closing line)`, or `None` if the module has none."""
    tree = corpus.tree(path)
    if tree is None or not tree.body:
        return None
    doc = ast.get_docstring(tree, clean=False)
    if doc is None:
        return None
    first = tree.body[0]
    if not (isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant)):
        return None
    end_line = getattr(first, "end_lineno", None) or first.lineno
    return doc, end_line


def measure(repo_root: Path) -> dict[str, tuple[int, bool]]:
    """`{rel path: (docstring line count, is_local)}` over every tooling file
    that has a module docstring. Exposed for `--refresh` and for tests that
    want the raw measurement without the violation framing."""
    out: dict[str, tuple[int, bool]] = {}
    for path in scope.tooling_files(repo_root):
        found = _module_docstring(path)
        if found is None:
            continue
        doc, _ = found
        rel = path.relative_to(repo_root).as_posix()
        out[rel] = (len(doc.splitlines()), _is_local(path, repo_root))
    return out


def refresh(repo_root: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Regenerate both baselines from the tree as it stands now.

    Only ever lowers a recorded count — `min(measured, already recorded)` — never
    raises one, even for a module that grew: growth is what the gate exists to
    catch, and a refresh that silently absorbed it would be the exact hole
    `.ai/recipes/turn-a-correction-into-a-gate.md`'s failure modes warn about
    (a gate that quietly narrows itself until it checks nothing). Raising a
    number is a deliberate manifest-style edit in its own commit, done by hand.
    A module now at or under `BUDGET_LINES` is dropped from the baseline outright
    — the ratchet's "remove from baseline" step, applied by the tool that would
    otherwise leave the entry to rot.

    The core baseline is written only when `repo_root` actually holds
    `nightshift/gates/` — i.e. only in this package's own checkout, per
    `_core_baseline_path`. Anywhere else `scope.tooling_files` never returns a
    non-`.ai/` file to begin with, so there is nothing to record; writing an
    empty (or any) core baseline into an unrelated repo would create a stray
    `nightshift/gates/data/` directory that repo has no reason to have.
    """
    measured = measure(repo_root)
    local_path = _local_baseline_path(repo_root)
    core_path = _core_baseline_path(repo_root)
    write_core = (repo_root / "nightshift" / "gates").is_dir()
    local_existing = _load_baseline(local_path)
    core_existing = _load_baseline(core_path) if write_core else {}

    local_new: dict[str, int] = {}
    core_new: dict[str, int] = {}
    for rel, (n, is_local) in measured.items():
        if n <= BUDGET_LINES:
            continue
        existing = local_existing if is_local else core_existing
        recorded = existing.get(rel)
        value = n if recorded is None else min(n, recorded)
        (local_new if is_local else core_new)[rel] = value

    _write_baseline(local_path, local_new)
    if not write_core:
        return local_new, core_new
    _write_baseline(core_path, core_new)
    return local_new, core_new


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    local_baseline = _load_baseline(_local_baseline_path(repo_root))
    core_baseline = _load_baseline(_core_baseline_path(repo_root))

    violations: list[Violation] = []
    for path in scope.tooling_files(repo_root):
        found = _module_docstring(path)
        if found is None:
            continue
        doc, line = found
        rel = path.relative_to(repo_root).as_posix()
        is_local = _is_local(path, repo_root)
        baseline = local_baseline if is_local else core_baseline
        baseline_rel = (_local_baseline_path(repo_root) if is_local
                        else _core_baseline_path(repo_root))
        n = len(doc.splitlines())
        recorded = baseline.get(rel)

        def _report(message: str, *, _rel: str = rel, _line: int = line) -> None:
            # `_rel`/`_line` are bound as defaults at definition time, not looked
            # up from the enclosing scope at call time -- B023's whole complaint
            # (a closure over a loop variable) does not apply once the value is
            # captured this way, and `_report` still reads like a closure at
            # every call site below.
            if appeal_markers.exempt(appeals, NAME, _rel, _line):
                return
            violations.append(Violation(_rel, _line, f"{NAME}: {message}"))

        if recorded is None:
            if n > BUDGET_LINES:
                _report(
                    f"module docstring is {n} lines, over the {BUDGET_LINES}-line "
                    f"budget. Shorten it to what the module is, what it guarantees, "
                    f"and what not to do; move incident narrative to "
                    f".ai/corrections.log or git history. If it needs grandfathering "
                    f"for now, add it to {baseline_rel.name} at its current count.")
            # Dated narrative is checked only for a module not already baselined --
            # every baselined module is over budget *because* of exactly this kind of
            # prose, and flagging all of them unconditionally the day this gate ships
            # would be the "not low-noise" case the module docstring rules out. A
            # `docstring-diet-<group>` chore brings a module under budget by moving
            # its dated narrative out, which is this same rule, paid down by the
            # ratchet rather than red on arrival.
            dates = _DATE.findall(doc)
            if len(dates) >= DATE_THRESHOLD:
                _report(
                    f"module docstring names {len(dates)} dates — reads as incident "
                    f"narrative rather than what the module is/guarantees/must not do. "
                    f"Move the dated story to .ai/corrections.log or git history and "
                    f"cite the correction slug instead.")
        elif n > recorded:
            _report(
                f"module docstring grew from {recorded} to {n} lines (baselined in "
                f"{baseline_rel.name}). The baseline is a ratchet, not a budget to "
                f"spend back up to — shorten it back down, or lower the growth to a "
                f"deliberate, reviewed edit of {baseline_rel.name} in its own commit.")
        elif n <= BUDGET_LINES:
            _report(
                f"module docstring is now {n} lines, at or under the "
                f"{BUDGET_LINES}-line budget — remove {rel} from {baseline_rel.name}. "
                f"Baselines only shrink; a chore that shortens a docstring is not "
                f"finished until its entry is gone too.")

    return sorted(violations, key=lambda v: (v.file, v.line))


def main(argv: list[str] | None = None) -> int:
    import sys

    from nightshift.manifest import find_root

    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--refresh", action="store_true",
                        help="regenerate both baselines from the tree as it stands "
                             "(never raises a recorded count -- see the module docstring)")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)

    root = (args.root or find_root()).resolve()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.refresh:
        local_new, core_new = refresh(root)
        print(f"local baseline: {len(local_new)} module(s) -> "
              f"{_local_baseline_path(root)}")
        print(f"core baseline: {len(core_new)} module(s) -> {_core_baseline_path(root)}")
        return 0

    found = check(root)
    for violation in found:
        print(str(violation))
    return 1 if found else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
