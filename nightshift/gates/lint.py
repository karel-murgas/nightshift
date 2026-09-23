"""Gate: `ruff` reports no lint findings in the project's own source, at the
configured rule selection.

**Generic, and therefore core**, by the same test `dead_code.py` draws: no
project earns "an unused import is unused" or "a bare `except:` swallows
everything" through a specific incident — every Python repo wants basic
lint on day one. What is project-specific is *which* rules and *which*
paths, which is exactly what `[lint]` in the manifest is for, on the same
override-not-prerequisite footing as `[dead_code]`.

**`select` is always passed explicitly, never left to ruff's own default.**
Measured on Project Tigress, 2026-09-18, with ruff 0.16.8: `ruff check` with
no `--select` at all resolved to something close to ruff's *entire* rule
catalogue — 1051 findings on a 149-file tree with a green test suite, most of
them modernisation nudges (`UP037` quoted-annotation, `I001` unsorted-imports,
`SIM102` collapsible-if) rather than bugs. Whatever a bare invocation means in
a given ruff version is not this gate's problem to inherit silently on the
next upgrade, so `manifest.Lint.select` defaults to a set nightshift chose on
purpose (`E9`, `F`, `E7`, `B` — see that class's docstring) and this gate
always passes `--select` and `--ignore` from the resolved config, never
ruff's bare invocation.

**Two things measured about scope, the same shape as `dead_code`'s findings
about `main.py` and `tests/`.**

1. Unlike vulture, ruff's rules here are file-local (no cross-module symbol
   table), so there is no `main.py`-must-be-included or `tests/`-must-be-
   excluded trap — a project can point `[lint].paths` at whatever it likes
   with no false positives from either direction.
2. `F821` (undefined name) is *not* in the default `select` for a reason worth
   recording rather than rediscovering: it fires on a `TYPE_CHECKING`-less
   forward-reference annotation (`def f(x: "Actor")` with no import of `Actor`
   anywhere, guarded only by a `# type: ignore[name-defined]` comment a type
   checker would honour but pyflakes does not) exactly as loudly as a real
   typo. A project using that pattern widely gets 100+ false positives from a
   rule that is, in the abstract, one of pyflakes' most valuable; one that
   is not gets a real check for free. Each project's `[lint].ignore` decides,
   not this default — see Project Tigress's own manifest for the case where
   it is turned off, and why.

Findings are read as JSON (`--output-format=json`), not the human-readable
text: a Windows path with a drive letter (`C:\\repo\\x.py`) breaks a naive
`file:line:col:` regex split exactly the way `dead_code`'s docstring warns
about, and JSON sidesteps it rather than fixing the regex twice.
"""
from __future__ import annotations

import json
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError, manifest_path
from nightshift.manifest import load as load_manifest

NAME = "lint"
DESCRIPTION = "ruff reports no lint findings in the project's source at the configured rule selection"

# ruff's own exit codes: 0 clean, 1 findings reported, 2 a CLI/config error
# (unknown rule selector, bad arguments) that means no file was ever checked.
_CLEAN = 0
_FINDINGS = 1


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Seam for tests, mirroring `dead_code._run`. `encoding=` explicit for the
    same reason: a finding can quote an identifier the locale codec cannot
    represent, and text mode without it silently returns `None` for stdout
    from inside a reader thread rather than raising."""
    return subprocess.run(argv, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", check=False)


def _relative(raw: str, repo_root: Path) -> str:
    """ruff's JSON `filename` is absolute; posix-normalise it the same way
    every other gate does, so a violation's text does not depend on which
    machine or OS ran it."""
    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.relative_to(repo_root)
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def check(repo_root: Path) -> list[Violation]:
    try:
        manifest = load_manifest(repo_root)
    except ManifestError:
        # No manifest: not a repo nightshift is set up in. Guessing which
        # directories to lint is how a tool ends up checking the wrong tree.
        return []

    rel_manifest = manifest_path(repo_root).relative_to(repo_root).as_posix()
    paths = manifest.lint_paths
    if not paths:
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: nothing to scan — declare `project.source_dirs`, or "
            f"`paths` in a `[lint]` table if this project's source lives "
            f"somewhere else. A lint gate with no paths reports success "
            f"without checking anything.",
        )]

    if find_spec("ruff") is None:
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: ruff is not installed, so nothing was checked. It is a "
            f"declared dependency of nightshift — `pip install -e "
            f"path/to/nightshift` installs it. This is a violation rather "
            f"than a skip on purpose: a lint gate that no-ops when its tool "
            f"is absent is green and wrong forever.",
        )]

    select = manifest.lint.select
    argv = [sys.executable, "-m", "ruff", "check", "--isolated",
            "--output-format=json", "--select", ",".join(select)]
    if manifest.lint.ignore:
        argv += ["--ignore", ",".join(manifest.lint.ignore)]
    argv += list(paths)

    done = _run(argv, repo_root)

    if done.returncode not in (_CLEAN, _FINDINGS):
        detail = (done.stderr or done.stdout or "").strip().replace("\n", " ")
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: ruff exited {done.returncode} without checking "
            f"(a `[lint].select`/`ignore` code may not exist, or a `paths` "
            f"entry may be invalid): {detail or 'no output'}",
        )]

    raw = (done.stdout or "").strip()
    if not raw:
        # A clean run still prints "[]"; truly empty stdout on exit 0/1 means
        # ruff did not speak at all, which is not the same claim as "clean".
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: ruff produced no output at all (exit {done.returncode}) "
            f"— its JSON format may have changed.",
        )]

    try:
        findings = json.loads(raw)
    except json.JSONDecodeError:
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: ruff's output did not parse as JSON — its format has "
            f"changed. Output was: {raw[:400]}",
        )]

    if not isinstance(findings, list):
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: ruff's JSON was not a list of findings — its format "
            f"has changed. Output was: {raw[:400]}",
        )]

    violations: list[Violation] = []
    for f in findings:
        try:
            violations.append(Violation(
                _relative(f["filename"], repo_root),
                int(f["location"]["row"]),
                f"{NAME}: {f['code']} {f['message']}",
            ))
        except (KeyError, TypeError, ValueError):
            # ruff said it found something and this gate could not read the
            # shape of it — its JSON schema has changed. Reporting nothing
            # would turn a real finding into a pass.
            violations.append(Violation(
                rel_manifest, 0,
                f"{NAME}: a ruff finding did not match the expected JSON "
                f"shape (filename/location.row/code/message): {str(f)[:400]}",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
