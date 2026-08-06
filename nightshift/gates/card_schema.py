"""Gate: every card on the board matches the schema (03_board.md).

The board is `Board/<lane>/<id>.md`; a transition is a file move plus a
commit, so the two things worth checking mechanically are (a) the frontmatter
the dispatcher reads is present and resolvable, and (b) `state:` still agrees
with the lane the file is actually sitting in — a disagreement is a half-done
move, which is exactly the failure a crash-safe board is supposed to make
visible.

Unknown frontmatter keys are violations on purpose. 03_board.md §2 dropped two
fields that were written twice and read never; a schema that silently tolerates
extras grows those back, and the runner would drop them without a word.

Two lanes are not fully schema'd, for different reasons (03_board.md §1):

* `ideas/` is **the maintainer's private lane** and is not scanned at all. Not
  "scanned leniently" — not enumerated. Triage must never read it, and the
  cheapest way to guarantee that is for the scripts never to glob it.
* `inbox/` is the ready-for-triage lane. A bare note is dropped there, so
  frontmatter is optional; but a *partly* triaged card whose frontmatter is
  wrong is a real defect, so if frontmatter is present it must be valid. The
  actionable sections only start being required at `tasks/`.

`Board/` and the lane names are a framework convention, not a per-project
fact (07_portability.md §1), which is why they are still literal constants
here rather than manifest fields.
"""
from __future__ import annotations

import re
from pathlib import Path

from nightshift import board as _board
from nightshift.gates.base import Violation

NAME = "card_schema"
FAST = True
DESCRIPTION = "cards on the board match the card schema, and `state:` agrees with the lane"

# Where the board sits comes from `[board].root`, via the module that owns the
# fact. It was `Path("Board")` — a second home for a manifest field `board.py`,
# `reconcile.py`, the runner and `ideas_fence` all already honoured. A project that
# renamed its board therefore got a gate scanning a directory that was not there,
# and `check()` opens with "no board → no violations": measured 2026-08-02, a card
# with an invalid `tier:` and a missing required section reported **All clear**
# while the runner, reading the real board, called the same card dispatchable. The
# gate guarding the dispatcher went quiet and the dispatcher did not.
#
# `LANES` below stays an independent copy on purpose — that reasoning is in the
# module docstring and is unaffected: the lane *names* are framework config no
# project may redefine, whereas the board's *location* is a declared project fact.

# `ideas/` is the maintainer's and is absent on purpose — see the module docstring.
LANES: tuple[str, ...] = (
    "inbox",
    "tasks",
    "needs-decision",
    "review",
    "testing",
    "done",
    "failed",
)
# Lanes where the card is meant to be actionable, i.e. carries steps and
# acceptance criteria. `inbox/` deliberately does not.
_ACTIONABLE = frozenset(LANES) - {"inbox"}
# Lanes where a card may legitimately have no frontmatter at all yet.
_UNTRIAGED = frozenset({"inbox"})

# Lanes with dispatch behind them. `unattended:` governs one decision — may the
# runner start this card — so once that decision has been taken the field is
# history, and so is prose about it. Rewriting a shipped card to agree with a rule
# clarified afterwards falsifies the record that made the clarification necessary,
# which is the same reason `doc_scope: history` exists.
_DISPATCHED = frozenset({"testing", "done", "failed"})

_AUTHOR_FIELDS: tuple[str, ...] = (
    "id",
    "title",
    "state",
    "tier",
    "worker",
    "recipe",
    "unattended",
    "created",
)
_OPTIONAL_FIELDS = frozenset({"requires", "checker"})
_RUNNER_FIELDS = frozenset({"attempts", "branch", "started", "finished"})
# Written by Base Board when a card is dragged within a column, never by us.
# It has to be allowed or the first reorder turns the whole board red, and a
# gate that fires on the maintainer using their own board is a gate that gets
# muted. Nothing on our side reads it — see 03_board.md §2.
_TOOL_FIELDS = frozenset({"kanban_order", "tags"})

# `tags` is the maintainer's own classification, surfaced on the board and in
# Obsidian's tag pane. Allowed for the same reason `kanban_order` is: it is read
# by the tools a human drives the board with, not by the runner. The one tag with
# operational meaning today is `nightshift` — a card whose deliverable lands in the
# framework repo, which the runner therefore cannot cut a worktree for; those carry
# `unattended: false` so it declines them up front instead of failing mid-dispatch.
_TAG = re.compile(r"^[a-z][a-z0-9-]*$")


def _tags(fields: dict[str, str]) -> list[str]:
    """The `tags` value as a list. Inline form only (`tags: [a, b]`), because
    `_frontmatter` is a flat one-line-per-key parser and a YAML block list would
    read as an empty value — silently tagging nothing. Returning [] for the empty
    and absent cases keeps every caller a plain membership test."""
    raw = fields.get("tags", "").strip()
    if not raw:
        return []
    return [t.strip().strip('"').strip("'").lstrip("#")
            for t in raw.strip("[]").split(",") if t.strip()]

_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")

_TIERS = frozenset({"worker", "lead"})
_BOOLS = frozenset({"true", "false"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SECTION = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)


def _frontmatter(text: str) -> tuple[dict[str, str], dict[str, int]]:
    """Flat `key: value` frontmatter. Values are kept as raw strings — the
    schema has no nested fields and pulling in a YAML dependency for a gate
    that must stay fast and importable in isolation is not worth it.
    """
    fields: dict[str, str] = {}
    lines: dict[str, int] = {}
    if not text.startswith("---"):
        return fields, lines
    for number, line in enumerate(text.splitlines()[1:], start=2):
        if line.strip() == "---":
            break
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            key, value = match.group(1), match.group(2).strip()
            fields[key] = value.strip('"').strip("'")
            lines[key] = number
    return fields, lines


_BODY_UNATTENDED = re.compile(r"`unattended:\s*(true|false)`", re.IGNORECASE)


def _body_unattended_claims(text: str) -> list[tuple[int, str]]:
    """Every `unattended: <bool>` a card asserts in its *prose*, as (line, value).

    Backticked only, deliberately. A card discussing the field in passing ("the
    unattended flag") is not making a claim about itself; a card writing
    `` `unattended: false` `` is.

    The frontmatter is skipped rather than matched-and-excluded — its `unattended:`
    line is unbackticked, so it cannot match, but slicing makes that independent of
    the frontmatter's formatting.

    **The one narrowing this rule needs, stated rather than hidden:** it cannot read
    negation, so a card explaining the value it *isn't* using ("this does not need
    `unattended: false`") reads as a contradiction. So the rule really enforced is:
    **do not backtick an `unattended:` value your frontmatter does not have** —
    narrower than "do not contradict yourself", and the narrow version is the one
    with no false positives.
    """
    body_start = 0
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            body_start = end
    claims: list[tuple[int, str]] = []
    offset = text[:body_start].count("\n")
    for number, line in enumerate(text[body_start:].splitlines(), start=offset + 1):
        for match in _BODY_UNATTENDED.finditer(line):
            claims.append((number, match.group(1).lower()))
    return claims


def _sections(text: str) -> set[str]:
    return {m.group(1).lower() for m in _SECTION.finditer(text)}


def _section_body(text: str, heading: str) -> str:
    """Text between `## <heading>` and the next `##`, lowercased and stripped."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(text)
    return match.group(1).strip().lower() if match else ""


def _has_section(sections: set[str], *prefixes: str) -> bool:
    return any(s.startswith(p) for s in sections for p in prefixes)


def _check_card(path: Path, lane: str, repo_root: Path) -> list[Violation]:
    rel = path.relative_to(repo_root).as_posix()
    text = path.read_text(encoding="utf-8")
    fields, lines = _frontmatter(text)
    out: list[Violation] = []

    def bad(key: str, rule: str) -> None:
        out.append(Violation(rel, lines.get(key, 1), f"card_schema: {rule}"))

    if not fields:
        if lane in _UNTRIAGED:
            # A bare note dropped in. Triage fits the frontmatter; nobody
            # should be hand-writing `tier:` or `unattended:`.
            return []
        return [Violation(rel, 1, "card_schema: no YAML frontmatter — every card outside inbox/ needs one")]

    if lane not in _UNTRIAGED:
        for key in _AUTHOR_FIELDS:
            if key not in fields:
                bad(key, f"missing required field `{key}`")

    known = set(_AUTHOR_FIELDS) | _OPTIONAL_FIELDS | _RUNNER_FIELDS | _TOOL_FIELDS
    for key in fields:
        if key not in known:
            bad(key, f"unknown field `{key}` — nothing reads it, so the runner would drop it silently")

    if "id" in fields and fields["id"] != path.stem:
        bad("id", f"`id: {fields['id']}` does not match the filename stem `{path.stem}`")
    if "state" in fields and fields["state"] != lane:
        bad("state", f"`state: {fields['state']}` but the card is in `{lane}/` — half-done move")
    if "tier" in fields and fields["tier"] not in _TIERS:
        bad("tier", f"`tier: {fields['tier']}` — must be one of {sorted(_TIERS)}; the dispatcher resolves the tier to a model and never guesses one")
    if "unattended" in fields and fields["unattended"].lower() not in _BOOLS:
        bad("unattended", f"`unattended: {fields['unattended']}` — must be true or false")
    tags = _tags(fields)
    for tag in tags:
        if not _TAG.match(tag):
            bad("tags", f"`{tag}` — tags are lowercase slugs; write them inline, "
                        f"e.g. `tags: [nightshift]`")
    # The one tag the runner's behaviour depends on. A `nightshift` card's
    # deliverable lands in the framework repo, and the runner only ever cuts a
    # worktree of THIS one — so an unattended dispatch cannot finish it and would
    # either fail or mutate the editable install globally, mid-run, uncommitted.
    # Enforced rather than remembered: getting this wrong costs a whole dispatch
    # to discover, which is exactly what happened on 2026-07-31.
    if "nightshift" in tags and fields.get("unattended", "").lower() != "false":
        bad("unattended", "a `nightshift`-tagged card must be `unattended: false` — its "
                          "deliverable is outside this repo, so the runner cannot cut a "
                          "worktree for it")
    for line_no, claimed in ([] if lane in _DISPATCHED else _body_unattended_claims(text)):
        if "unattended" in fields and claimed != fields["unattended"].lower():
            out.append(Violation(
                rel, line_no,
                f"card_schema: body says `unattended: {claimed}` but the frontmatter says "
                f"`unattended: {fields['unattended']}` — one of the two is wrong, and the "
                f"runner reads the frontmatter",
            ))
    if "created" in fields and not _DATE.match(fields["created"]):
        bad("created", f"`created: {fields['created']}` — must be an ISO date (YYYY-MM-DD)")
    if "requires" in fields and not _SLUG.match(fields["requires"]):
        bad("requires", f"`requires: {fields['requires']}` — must be a lowercase host-capability slug")

    worker = fields.get("worker", "none")
    if worker != "none" and not (repo_root / ".claude" / "agents" / f"{worker}.md").is_file():
        bad("worker", f"`worker: {worker}` has no charter at .claude/agents/{worker}.md")
    # The producer/checker seam (00_architecture.md §16). Resolved against a real
    # charter for the same reason `worker:` is: the runner spawns it by name, and
    # a typo would otherwise mean the loop silently never runs.
    checker = fields.get("checker", "none")
    if checker != "none":
        if not (repo_root / ".claude" / "agents" / f"{checker}.md").is_file():
            bad("checker", f"`checker: {checker}` has no charter at .claude/agents/{checker}.md")
        if checker == worker:
            bad("checker", f"`checker: {checker}` is the same agent as `worker:` — an agent "
                           f"reviewing its own output sees what it intended, not what it made (§16)")
    recipe = fields.get("recipe", "none")
    if recipe != "none" and not (repo_root / ".ai" / "recipes" / f"{recipe}.md").is_file():
        # gate-ok(source_reference_liveness): `.ai/recipes/` is a per-consuming-project
        # directory this gate reads from `repo_root` at check time; this framework's own
        # checkout carries none, since recipes are a project's, not this package's.
        bad("recipe", f"`recipe: {recipe}` has no spine at .ai/recipes/{recipe}.md")

    sections = _sections(text)
    if lane not in _UNTRIAGED and not _has_section(sections, "intent"):
        out.append(Violation(rel, 1, "card_schema: missing `## Intent` — required from tasks/ onward"))

    if lane in _ACTIONABLE:
        for required in ("acceptance", "open questions"):
            if not _has_section(sections, required):
                out.append(Violation(rel, 1, f"card_schema: a `{lane}/` card must carry a "
                                         f"`## {required.title()}` section"))
        # A card must say how the work decomposes, but it may do that by naming
        # a recipe or a worker charter that already carries the decomposition.
        if recipe == "none" and worker == "none" and not _has_section(sections, "steps"):
            out.append(
                Violation(
                    rel,
                    1,
                    "card_schema: missing `## Steps` — a card with neither `recipe:` nor "
                    "`worker:` has nothing else carrying the decomposition",
                )
            )

    # A `tasks/` card must state the core of *how* the work is done in one place a
    # human reads first — `## Approach` (the one-paragraph principle the digest
    # inlines), or `## Subject` for a visual asset whose substance is the picture,
    # not prose (03_board.md §12b). Only `tasks/`, not every actionable lane:
    # archived cards predate the rule and must not be retroactively reddened, and
    # a card carries the section forward once it advances out of `tasks/`.
    if lane == "tasks" and not _has_section(sections, "approach", "subject"):
        out.append(
            Violation(
                rel,
                1,
                "card_schema: missing `## Approach` — a `tasks/` code card must state the "
                "core of how the change works in one paragraph (or `## Subject` for an "
                "art/audio card); it is what the digest shows the maintainer",
            )
        )

    # A card in tasks/ is by definition ready to dispatch with no further human
    # input. A live open question means it is misfiled, not that the card is bad
    # -- needs-decision/ is a success state (§13).
    if lane == "tasks" and _has_section(sections, "open questions"):
        body = _section_body(text, "Open questions")
        if not body.startswith("none"):
            out.append(
                Violation(
                    rel,
                    1,
                    "card_schema: `## Open questions` is not `none` — a card with a live "
                    "question belongs in needs-decision/, not tasks/",
                )
            )

    if lane == "needs-decision" and not _has_section(sections, "question"):
        out.append(
            Violation(
                rel,
                1,
                "card_schema: parked card needs a `## Question` section carrying what was "
                "attempted, what is ambiguous, the candidate answers and what each implies (§13)",
            )
        )

    return out


def _orphans(repo_root: Path) -> list[Violation]:
    """Cards that are not in any lane at all.

    Found the hard way, 2026-07-22: a card was moved to `Board/` root and the
    gate said "All clear", because it enumerates lanes and a file in no lane is
    in no glob. That is the worst possible blind spot for this particular gate —
    a card outside every lane has no state, so it is invisible to the runner,
    absent from the digest, and silently not-done. A drag that lands slightly
    wrong produces exactly this, so it will recur.
    """
    board = _board.board_dir(repo_root)
    if not board.is_dir():
        return []
    known = set(LANES) | {_board.PRIVATE_LANE}
    out: list[Violation] = []
    for path in sorted(board.rglob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        lane = path.relative_to(board).parts[0] if len(path.relative_to(board).parts) > 1 else None
        if lane in known:
            continue
        rel = path.relative_to(repo_root).as_posix()
        where = f"`{lane}/`, which is not a lane" if lane else "the board root, outside every lane"
        out.append(
            Violation(
                rel,
                1,
                f"card_schema: card sits in {where} — it has no state, so the runner "
                f"cannot see it and the digest will not report it — one state means one lane",
            )
        )
    return out


def cards(repo_root: Path) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for lane in LANES:
        lane_dir = _board.board_dir(repo_root) / lane
        if not lane_dir.is_dir():
            continue
        for path in sorted(lane_dir.glob("*.md")):
            if path.name.upper() == "README.MD":
                continue
            found.append((path, lane))
    return found


def check(repo_root: Path) -> list[Violation]:
    violations: list[Violation] = _orphans(repo_root)
    for path, lane in cards(repo_root):
        violations.extend(_check_card(path, lane, repo_root))
    return violations


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for violation in check(find_root()):
        print(violation)
