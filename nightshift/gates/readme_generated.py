"""Gate: README.md's generated blocks match a fresh generation.

07_portability.md §8 D4's rule, enforced rather than intended: "any list in
the README — gate names, runner flags, lane names, manifest fields — is
generated from the code that owns it, and a gate checks the README against a
fresh generation." Free text around a generated block is fine; a retyped list
inside one is a defect — the same D4 failure `branch_role_prose` exists to
catch for the integration-branch line, at the scale of a whole reference
table instead of one sentence.

Ships in core: the mechanism (`nightshift.readme_gen`) needs only the repo
root, so a project that has never adopted the marker convention gets no
violations from this gate — there is nothing in its README for `stale_blocks`
to find. It is load-bearing wherever a README opts in, which today is this
package's own.
"""
from __future__ import annotations

from pathlib import Path

from nightshift import readme_gen
from nightshift.gates.base import Violation

NAME = "readme_generated"
FAST = True
DESCRIPTION = ("README.md's generated blocks (gate list, runner flags, manifest fields) "
              "match a fresh generation")


def check(repo_root: Path) -> list[Violation]:
    path = repo_root / "README.md"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return [
        Violation(
            "README.md", line,
            f"{NAME}: the `{name}` generated block is stale — run "
            f"`python -m nightshift.readme_gen --write` and commit the diff",
        )
        for name, line in readme_gen.stale_blocks(repo_root, text)
    ]
