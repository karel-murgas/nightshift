"""Gate: `vulture` reports no dead code in the project's own source.

**Generic, and therefore core** (`00_architecture.md` §15 read from the other
end). Most gates here are *earned* — they exist because one repo hit one
failure, and they encode that repo's convention. Dead code is the other kind:
no project earns it through a specific incident, every Python repo wants it on
day one, and re-deriving it per project is the manual step this package exists
to remove. The dividing test is whether the rule mentions anything about the
project. "An unused import is unused" does not; "an `Event` subclass must reach
`bus.post`" does, and that one lives in the project that earned it.

That is also why `vulture` is a real dependency of this package rather than an
optional extra: an extra *is* the step somebody has to remember, and a gate
whose tool is optional is a gate that silently checks nothing on half the repos
it ships to. If the tool is missing anyway, this gate reports a violation. It
never returns an empty list on a tool it could not run — green-and-wrong is the
one outcome a dead-code check must not have, because nobody ever investigates a
gate that passes.

**Two things about the invocation, measured on Dungeoneer with vulture 2.16 on
2026-08-01, that cost more to find than the gate did to write.**

| invocation | conf 60 | conf 80 | verdict |
|---|---|---|---|
| `vulture <package>/` | 82 | 14 | wrong — 8 of those are artefacts of the missing entry point |
| `vulture <package>/ main.py` | 74 | 8 | correct, and with **no whitelist file at all** |
| `… main.py tests/` | 107 | 30 | never — ~25 pytest fixtures vulture cannot see are used |

1. **Pass the entry point, do not write a whitelist.** A module that nothing
   under the package imports — the `main.py` that constructs the app — is the
   sole consumer of a good deal of the package, and leaving it out reports all
   of it as dead: six `if TYPE_CHECKING:` imports, the application class, the
   logging setup. The instinct is to whitelist those. Adding the entry point to
   the scanned paths resolves every one of them and leaves nothing to maintain,
   which is why `[dead_code] paths` is its own field rather than reusing
   `project.source_dirs`.
2. **Keep the test directory out.** `extra_source_dirs` exists for symbol
   resolution and includes `tests/`; feeding it to vulture adds ~25 findings for
   fixtures that pytest injects by decorator, which no static tool can see. So
   this gate cannot reuse that field either — hence a table of its own.

Runtime: 0.93 s over ~40k lines.

**`min_confidence` defaults to 80, and that is a floor to be lowered, not a
natural constant.** vulture's genuinely interesting findings — an orphaned
class, a function nothing calls — are reported at 60; 80 is unused imports and
unused locals, which is what an unswept tree can go green on today. The staging
is deliberate: a project turns this on at 80, clears the 60-band as its own
piece of work, then lowers the number to what the tree actually supports. Set it
from what the tree is *after* a sweep — a threshold picked to make the current
tree pass is a gate weakened until it passed.

Config is `[dead_code]` in `.ai/manifest.toml`, and **the table is an override,
never a prerequisite**: with no table at all this runs on `project.source_dirs`
at confidence 80. A repo that has configured nothing still gets the check.

    [dead_code]
    paths = ["mypackage", "main.py"]
    min_confidence = 80

Appeals are vulture's own, not this package's: a `# noqa`-style marker would be a
second mechanism for something the tool already does. Name the symbol in a
`whitelist.py` that vulture scans, or — better, and the reason this gate is worth
having — delete it.
"""
from __future__ import annotations

import re
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

from nightshift.gates.base import Violation
from nightshift.manifest import ManifestError, manifest_path
from nightshift.manifest import load as load_manifest

NAME = "dead_code"
FAST = True
DESCRIPTION = "vulture reports no dead code in the project's source at the configured confidence"

# vulture's exit codes. 1 is invalid input (a path that does not exist, a file it
# cannot parse) and 2 is a CLI error; both mean the check did not happen, which
# this gate reports rather than reads as silence.
_CLEAN = 0
_DEAD_CODE_FOUND = 3

# `path:line: unused variable 'x' (100% confidence)`.
#
# The file group is non-greedy and the line group is anchored to `\d+:`, which is
# what keeps a Windows drive letter (`C:\repo\x.py:25:`) inside the path instead
# of ending it at the first colon.
_FINDING = re.compile(r"^(?P<file>.*?):(?P<line>\d+):\s*(?P<message>.+)$")


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Seam, so a test can simulate a vulture that fails in a way this machine
    will not reproduce on demand. `encoding=` is explicit because a finding can
    name an identifier the locale codec cannot represent, and text mode without
    it returns `None` for stdout from inside a reader thread rather than
    raising — the failure `subprocess_encoding` was earned by."""
    return subprocess.run(argv, cwd=cwd, capture_output=True,
                          encoding="utf-8", errors="replace", check=False)


def _relative(raw: str, repo_root: Path) -> str:
    """vulture prints the paths it was given, so these are already repo-relative
    and in the OS's separator. Normalised to posix for the same reason every
    other gate does: a violation's text should not depend on which machine ran
    it."""
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
        # No manifest: this is not a repo anybody set nightshift up in, and
        # scanning a guessed set of directories is how a tool ends up reporting
        # against the wrong tree. `manifest.load` refuses the same guess.
        return []

    rel_manifest = manifest_path(repo_root).relative_to(repo_root).as_posix()
    paths = manifest.dead_code_paths
    if not paths:
        # Configured, but with nothing to look at. Silence here would be a gate
        # that passes forever without reading a line of the project.
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: nothing to scan — declare `project.source_dirs`, or "
            f"`paths` in a `[dead_code]` table if this project's source lives "
            f"somewhere else. A dead-code gate with no paths reports success "
            f"without checking anything.",
        )]

    if find_spec("vulture") is None:
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: vulture is not installed, so nothing was checked. It is a "
            f"declared dependency of nightshift — `pip install -e "
            f"path/to/nightshift` installs it. This is a violation rather than a "
            f"skip on purpose: a dead-code gate that no-ops when its tool is "
            f"absent is green and wrong forever.",
        )]

    done = _run(
        [sys.executable, "-m", "vulture", *paths,
         "--min-confidence", str(manifest.dead_code.min_confidence)],
        repo_root,
    )

    if done.returncode not in (_CLEAN, _DEAD_CODE_FOUND):
        detail = (done.stderr or done.stdout or "").strip().replace("\n", " ")
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: vulture exited {done.returncode} without checking "
            f"({'invalid input — a path in this table may not exist' if done.returncode == 1 else 'invocation error'}): "
            f"{detail or 'no output'}",
        )]

    violations: list[Violation] = []
    unparsed: list[str] = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        found = _FINDING.match(line)
        if found is None:
            unparsed.append(line)
            continue
        violations.append(Violation(
            _relative(found["file"], repo_root),
            int(found["line"]),
            f"{NAME}: {found['message']}",
        ))

    # vulture said it found something and this gate could not read a word of it:
    # the output format changed under us. Reporting nothing would turn a real
    # finding into a pass.
    if done.returncode == _DEAD_CODE_FOUND and not violations:
        return [Violation(
            rel_manifest, 0,
            f"{NAME}: vulture reported dead code but none of its output parsed "
            f"as `file:line: message` — its format has changed. Output was: "
            f"{(done.stdout or '').strip()[:400] or 'empty'}",
        )]
    for line in unparsed:
        violations.append(Violation(rel_manifest, 0,
                                    f"{NAME}: unparsed vulture output: {line}"))

    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
