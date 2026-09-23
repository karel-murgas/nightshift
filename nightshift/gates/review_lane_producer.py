"""Gate: every place that files a card in `review/` says why THIS site's review is
owed and obtainable.

`one-lane-for-owed-and-unobtainable` (2026-08-25) put the invariant on
`runner.review_stage`: a card is left in `review/` only when a review is genuinely
owed and can still be had, everything else settles to `blocked/` through the shared
`_unreviewable()` helper. That closed the five paths the correction found — but the
correction's own text named the gap it left: *"nothing enforces the invariant from
OUTSIDE `review_stage` itself, so a future degradation path added elsewhere would not
be caught by any gate."* `chores._hand_over` is exactly that — a second, legitimate
producer that never runs a reviewer at all and reaches `review/` by a road
`review_stage` does not cover, for a reason of its own (a batch survivor whose own
diff is green and unreviewed). Nothing stopped a *third* site from doing the same
without the reasoning to back it.

One `_unreviewable()` helper cannot be forced on every future caller — Python has no
way to seal `board.move`'s `"review"` argument to one function, and `board.move`
itself is deliberately generic across every lane a project's own board uses
(`board.py`: "no LLM anywhere in here... it is a file parser"). So the invariant is
asserted where Python cannot: at the call site, by a mandatory, named appeal. A
`board.move(..., "review")` (or `to_lane="review"`) with no
`# gate-ok(review_lane_producer): <reason>` within three lines is refused — not
because the site is wrong, but because nobody has yet written down why it is right.
The two sites that exist today (`runner._settle_impl`, reached only after
`review_stage` has already decided; `chores._hand_over`) each carry one, and a third
site inherits the same obligation rather than the same silence.

Appeals: `# gate-ok(review_lane_producer): <reason>` — see `appeal_markers.py` and
`10_self_improvement.md` §4. This is the one gate where an appeal is not an escape
hatch to use sparingly; it is the whole mechanism. A reason that does not actually
argue the review is owed and obtainable is an appeal that passed the gate for the
wrong reason — read at review time, not caught here.
"""
from __future__ import annotations

import ast
from pathlib import Path

import appeal_markers
from nightshift.gates import corpus
from nightshift.gates import scope
from nightshift.gates.base import Violation

NAME = "review_lane_producer"
FAST = True
DESCRIPTION = "a card moved into review/ carries a gate-ok saying the review is owed and obtainable"


def _moves_to_review(node: ast.Call) -> bool:
    """Is this a `<something>.move(..., "review", ...)` call?

    Matched on the attribute name and a literal `"review"` argument rather than on
    `board.move`'s import path, the same trade-off `git_path_lists` makes for
    `git(...)` calls: every site in this package spells it `board.move(...)`, and
    matching the shape rather than the name survives an import alias this package
    has never used without missing anything it does use today.
    """
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "move"):
        return False
    args = list(node.args) + [kw.value for kw in node.keywords]
    return any(isinstance(a, ast.Constant) and a.value == "review" for a in args)


def check(repo_root: Path) -> list[Violation]:
    appeals = appeal_markers.scan(repo_root)
    violations: list[Violation] = []
    for path in scope.tooling_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        tree = corpus.tree(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _moves_to_review(node):
                continue
            if appeal_markers.exempt(appeals, NAME, rel, node.lineno):
                continue
            violations.append(Violation(
                rel, node.lineno,
                "files a card in review/ with no `# gate-ok(review_lane_producer): "
                "<reason>` proving a review is owed AND obtainable here — "
                "one-lane-for-owed-and-unobtainable's residual gap: `review_stage` "
                "asserts this for its own path, nothing asserted it for any other. "
                "Justify this site or route the card to blocked/ instead.",
            ))
    return sorted(violations, key=lambda v: (v.file, v.line))


if __name__ == "__main__":
    from nightshift.manifest import find_root

    found = check(find_root())
    for violation in found:
        print(str(violation))
    raise SystemExit(1 if found else 0)
