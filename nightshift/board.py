"""The card model — one place that knows how to read and write a card.

`digest.py` and `reconcile.py` used to each carry their own frontmatter regex,
which was the duplication `board-parser-convergence` converged: both now
import this module. `digest.py` subclasses `Card` for its own read-only
judgments (`is_visual`, `waited`, `link`) and calls `parse_fields`/`section`
through it; `reconcile.py` keeps `read_state`/`stamp_state` as its own public
API (`Board/README.md` names them) but splices against `FRONTMATTER` from here
rather than its own copy. `card_schema.py` keeps an independent fourth copy on
purpose — a gate must stay importable and runnable with no siblings on the
path, stated in its own docstring and in `run.py`'s.

Two rules the writer obeys, and both matter more than they look:

* **Field order is preserved and values are not requoted.** Karel hand-writes
  frontmatter and Obsidian rewrites it; a writer that normalised quoting would
  produce a diff on every touch and the board's git history would stop being
  readable.
* **Runner-owned fields are appended, never interleaved.** `03_board.md` §2
  splits the schema by owner precisely so a runner that dies mid-card has
  rewritten only its own half.

No LLM anywhere in here (`00_architecture.md` §12) — it is a file parser.

**Where the board lives is the only project fact in this module**, and it is a
weak one: `Board/` is a framework convention, so `[board].root` is an override
rather than a prerequisite (07_portability.md §4, "override only with a
reason"). Everything else here — the lane names, the frontmatter grammar, the
runner-owned field list — is framework config that a project does not get to
redefine, because the runner, the reconciler, the digest and `card_schema` all
have to agree on it (D5).
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nightshift import manifest as _manifest
from nightshift.manifest import ManifestError
from nightshift.textio import write_text_lf  # cards are committed; write_text would CRLF them


def board_rel(root: Path) -> Path:
    """Where the board sits, relative to the repo root.

    Relative because two callers need it that way: `git add -A <path>` wants a
    pathspec inside the repo, and the runner hands the path to a worker's
    `--add-dir`. `Board` when nothing is declared — the same literal this
    replaced, so a project that has never written a `[board]` table sees no
    change at all (07_portability.md §8 step 4's first checklist item).
    """
    try:
        return Path(_manifest.load(root).board.root)
    except ManifestError:
        return Path(_manifest.Board().root)


def board_dir(root: Path) -> Path:
    """The board's absolute directory."""
    return root / board_rel(root)


# The lanes the runner may move a card between. `ideas/` is absent for the same
# reason it is absent from `reconcile.LANES`: it is Karel's private lane and no
# judgment actor — the runner included — enumerates it.
LANES: tuple[str, ...] = (
    "inbox",
    "tasks",
    "needs-decision",
    "review",
    "testing",
    "done",
    "failed",
)

# The private lane's name, owned here because the board's vocabulary is owned here —
# `reconcile` and `card_schema` each used to carry their own copy of the string.
#
# **Absent from `LANES` and it must stay absent**: `ideas_fence` derives "private" from a
# board subdirectory not being in that tuple, which is how the rule gets written down
# once instead of twice. This constant is for the code that has to NAME the directory
# (create it, or permit the one reader), never for deciding whether it is private.
#
# Absent from the list is not the same as absent from disk, and `init` conflated the two
# until 2026-08-03: it made lanes by iterating `LANES`, so the one lane belonging to the
# maintainer was the one lane nobody created for them — while `Board/README.md`,
# `CLAUDE.md` and `reconcile` all named it as the first step of the flow.
PRIVATE_LANE = "ideas"

# The reports the tooling writes at the repo root — which is the vault root, so these
# are what the maintainer actually reads. Owned here beside `LANES` because they are
# board vocabulary: three modules write one each (`digest`, `ingest`, `chores`) and
# three more have to recognise the whole set.
#
# **They are committed, and that is the point of the tuple.** Board state that exists
# on one machine only is a decision the other machine cannot see, and a generated file
# is not exempt from that — being regenerable is not a reason to leave it out of git.
# So every consumer needs the same list, and there are three of them: `commit_board`
# stages them, `runner.dirty_outside_board` exempts them (they are rewritten on every
# run, and a dirty tree refuses a dispatch), and `hooks.correction_prompt` ignores them.
#
# Written down once because it was written down once before, wrong: the exemption and
# the commit list each named `Digest.md` alone, so `ingest` and `chores` shipped a new
# view each and joined neither list — and two untracked reports at the root made the
# tree dirty and refused every batch outright. The commit list and the exemption list
# are the same list; a view on one and not the other breaks in one of those two ways.
DIGEST_VIEW = "Digest.md"
ROUTING_VIEW = "Routing.md"
CHORES_VIEW = "Chores.md"
GENERATED_VIEWS: tuple[str, ...] = (DIGEST_VIEW, ROUTING_VIEW, CHORES_VIEW)

# 03_board.md §2. Appended in this order when absent.
RUNNER_FIELDS: tuple[str, ...] = ("attempts", "branch", "started", "finished")

# The one non-default `kind:`, owned here for the same reason `LANES` is: it is the
# board's vocabulary, and the schema gate, the dispatcher and the batch driver all
# have to agree on the spelling. Absent means a full card, so no card written
# before this existed changes meaning.
KIND_CHORE = "chore"

# The one frontmatter-block regex for the board (`board-parser-convergence`).
# `digest.py` calls through `parse_fields`/`section` below and never needs this
# directly. `reconcile.py` still hand-splices just the `state:` line — its
# `read_state`/`stamp_state` stay the public API `Board/README.md` names — so it
# imports this constant rather than compiling its own copy. `card_schema.py`
# keeps an independent fourth copy on purpose (its own docstring and
# `run.py`'s: a gate must stay importable and runnable with no siblings on the
# path) and is not part of that count.
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_FIELD = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_BLOCK_ITEM = re.compile(r"^[ \t]+-[ \t]*(.+?)[ \t]*$")


def parse_fields(text: str) -> dict[str, str]:
    """Flat `key: value` frontmatter. Values stay raw strings — the schema has
    no nested fields, and a YAML dependency in code that must stay importable
    in isolation is not worth it (same call `card_schema` made).

    **YAML's block-list form is flattened to the inline one**, so `tags:`
    followed by indented `- item` lines reads as `[item]` rather than as the
    empty string. That is not a nicety: it is the form Obsidian writes by
    default, and `card_schema._frontmatter` has folded it since
    `tag-parser-blind-to-block-form` — which means until now the board's own
    model and the gate checking it disagreed about the contents of the same
    file, with this side reporting *absent* for a value that was plainly there.
    A parser that cannot see an input form does not report on it; it approves
    it, and every rule keyed on that field goes quietly dead.
    """
    block = FRONTMATTER.match(text)
    if not block:
        return {}
    fields: dict[str, str] = {}
    key = ""
    items: list[str] = []

    def flush() -> None:
        if key and items:
            fields[key] = "[" + ", ".join(items) + "]"

    for line in block.group(1).splitlines():
        item = _BLOCK_ITEM.match(line)
        if item and key and not fields.get(key):
            items.append(item.group(1).strip('"').strip("'"))
            continue
        found = _FIELD.match(line)
        if found:
            flush()
            key, items = found.group(1), []
            fields[key] = found.group(2).strip().strip('"').strip("'")
    flush()
    return fields


def set_fields(text: str, values: dict[str, str | None]) -> str:
    """Set or remove frontmatter fields in place, preserving everything else.

    A `None` value removes the key. A key that is absent is appended at the end
    of the block, which is why the runner's fields cluster together at the
    bottom of a card that has run.
    """
    block = FRONTMATTER.match(text)
    if not block:
        raise ValueError("card has no frontmatter block")
    lines = block.group(1).splitlines()
    rest = text[block.end():]

    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}:[ \t]*.*$")
        index = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
        if value is None:
            if index is not None:
                del lines[index]
        elif index is None:
            lines.append(f"{key}: {value}")
        else:
            lines[index] = f"{key}: {value}"

    return "---\n" + "\n".join(lines) + "\n---\n" + rest


def append_section(text: str, heading: str, body: str) -> str:
    """Replace the named `## <heading>` section, or append it at the end.

    Replacing rather than stacking is deliberate: the runner writes `## Error`
    on every failed attempt, and three attempts must leave one current error
    rather than three stale ones. The attempt history lives in
    `.ai/runs/<id>/`, which is where run output belongs (`Board/README.md`).
    """
    section = f"## {heading}\n\n{body.rstrip()}\n"
    pattern = re.compile(
        rf"^##[ \t]+{re.escape(heading)}[ \t]*$.*?(?=^##[ \t]|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if pattern.search(text):
        return pattern.sub(section, text, count=1)
    return text.rstrip() + "\n\n" + section


@dataclass
class Card:
    """One card, identified by where it sits. Nothing is cached across a
    transition — the runner rescans, which is what makes it resumable."""

    path: Path
    lane: str
    fields: dict[str, str]
    text: str

    @classmethod
    def load(cls, path: Path, lane: str) -> "Card":
        text = path.read_text(encoding="utf-8")
        return cls(path=path, lane=lane, fields=parse_fields(text), text=text)

    @property
    def id(self) -> str:
        return self.fields.get("id", self.path.stem)

    @property
    def title(self) -> str:
        return self.fields.get("title", self.path.stem)

    @property
    def tier(self) -> str:
        return self.fields.get("tier", "")

    @property
    def worker(self) -> str:
        return self.fields.get("worker", "none")

    @property
    def checker(self) -> str:
        """The agent that judges this card's output, or `none`.

        Its presence is what turns a dispatch into a producer→checker loop
        (`00_architecture.md` §16, "the second seam"). Declared per card rather
        than inferred from the worker, because whether an artefact needs a
        second pair of eyes is a property of the deliverable, not of who made it.
        """
        return self.fields.get("checker", "none")

    @property
    def kind(self) -> str:
        """What sort of work item this is — `chore`, or `""` for a full card.

        A property rather than a `fields.get` at each site because three separate
        mechanisms branch on it (the dispatcher skips chores, the attempt limit
        differs, the worker prompt gains a bounce instruction), and a misspelled
        literal in any one of them would silently take the full-card path.
        """
        return self.fields.get("kind", "")

    @property
    def tags(self) -> list[str]:
        """The card's own classification, in either YAML form.

        The one tag with operational meaning is `nightshift` — a card whose
        deliverable lands in the framework repo, which the runner cannot cut a
        worktree for. Surfaced here so a reader (a report, a panel) can say so
        without re-deriving the grammar; `card_schema` keeps its own copy for
        the isolation reason its docstring gives.
        """
        raw = self.fields.get("tags", "").strip()
        if not raw:
            return []
        return [t.strip().strip('"').strip("'").lstrip("#")
                for t in raw.strip("[]").split(",") if t.strip()]

    @property
    def surface(self) -> str:
        """Where in the running application this change shows up — free text the
        project supplies, never a vocabulary this package invents. The batch
        checklist groups by it so one pass through the app covers several items."""
        return self.fields.get("surface", "")

    @property
    def recipe(self) -> str:
        return self.fields.get("recipe", "none")

    @property
    def unattended(self) -> bool:
        return self.fields.get("unattended", "false").lower() == "true"

    @property
    def verify(self) -> str:
        """How this card gets verified once it is finished — `play` or `review`.

        `play` means it has a surface Karel can exercise, so a reviewed card
        lands in `testing/` carrying a `## How to test`. `review` means it has
        none — a gate, a deletion, inner wiring — and the reviewer's `ok` is the
        acceptance, so it lands in `done/`. The reviewer still runs on both:
        its job is catching *"I didn't ask for that"*, and it is the only
        independent look between a worker's self-assessment and `done/`.

        **Anything unrecognised, including absent, reads as `play`.** The
        default has to fall toward Karel's desk: a card that reaches `done/`
        because a field was forgotten or misspelled is the one direction nothing
        recovers from. `card_schema` is what stops a `tasks/` card from relying
        on the default.
        """
        return "review" if self.fields.get("verify") == "review" else "play"

    @property
    def requires(self) -> str:
        return self.fields.get("requires", "")

    @property
    def attempts(self) -> int:
        try:
            return int(self.fields.get("attempts", "0"))
        except ValueError:
            return 0

    def write(self, values: dict[str, str | None]) -> None:
        self.text = set_fields(self.text, values)
        write_text_lf(self.path, self.text)
        self.fields = parse_fields(self.text)

    def write_section(self, heading: str, body: str) -> None:
        self.text = append_section(self.text, heading, body)
        write_text_lf(self.path, self.text)


def section(text: str, heading: str) -> str:
    """The body of one `## <heading>` section, verbatim.

    The runner uses this to hand a checker the card's acceptance criteria
    *without* handing it the card — §16's seam only works if the checker never
    sees how the artefact was meant to be made.
    """
    found = re.search(
        rf"^##[ \t]+{re.escape(heading)}[ \t]*$(.*?)(?=^##[ \t]|\Z)",
        text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return found.group(1).strip() if found else ""


def dispatch_order(card: Card) -> tuple[int, str, str]:
    """Karel's column order first, filename second.

    `kanban_order` is Base Board's fractional index, written into a card's
    frontmatter every time he drags it within a column. It sorts
    lexicographically by construction — that is what a fractional index is *for*
    — so honouring it costs one sort key and keeps the property filename order
    was originally chosen for: the same order on every boot, so a
    crash-and-resume cannot reshuffle the night's work.

    Cards without the field sort **after** every card that has one, then
    alphabetically. A card only acquires `kanban_order` by being dragged, so its
    absence means "never prioritised" — and the runner writes fix-cards itself,
    which must never jump ahead of work Karel deliberately moved to the top.

    Until 2026-07-23 nothing read this field. `03_board.md` §2 filed it as
    "written by Obsidian, read by nobody here", which was decided while making
    `card_schema` tolerate the unknown key and quietly answered a second question
    nobody had asked — whether dragging a card up the column means anything. It
    does: it is the only prioritisation gesture the GUI offers.
    """
    order = card.fields.get("kanban_order", "")
    return (0, order, card.path.name) if order else (1, "", card.path.name)


def cards(root: Path, lane: str, card_cls: type[Card] = Card) -> list[Card]:
    """Every card in one lane, in dispatch order (see `dispatch_order`).

    `card_cls` lets a caller with its own `Card` subclass reuse this scan
    without duplicating the glob-and-filter loop — `digest.Card` adds judgments
    (`is_visual`, `waited`, `link`) that belong to the digest, not to the shared
    model, so it subclasses rather than growing this one.

    The ordering applies whatever the class: the digest then lists a lane in the
    order the runner would actually take it, which is the order Karel put it in.
    That is the useful reading of a queue, and it comes free — `dispatch_order`
    only touches `fields` and `path`, which every `Card` has.
    """
    lane_dir = board_dir(root) / lane
    if not lane_dir.is_dir():
        return []
    return sorted(
        (card_cls.load(p, lane)
         for p in lane_dir.glob("*.md")
         if p.name.upper() != "README.MD"),
        key=dispatch_order,
    )


def find(root: Path, card_id: str) -> Card | None:
    """The card with this id, in whichever lane it currently sits.

    This is the rescan primitive. A worker may move its own card — parking it in
    `needs-decision/` is a success state (`00_architecture.md` §13) — so after a
    dispatch the runner must ask the disk where the card went rather than assume
    it is where it left it.
    """
    for lane in LANES:
        for card in cards(root, lane):
            if card.id == card_id:
                return card
    return None


def _warn_if_failed(result: subprocess.CompletedProcess, what: str) -> None:
    """Say so when a staging call fails, instead of letting the commit no-op.

    `git add` failing is the shape of the 2026-07-23 `git-add-pathspec-noop`
    bug: nothing is staged, the commit that follows succeeds at committing
    nothing, `attempts` never reaches disk, and a crashed card retries forever.
    The original fix was an existence check on `Digest.md`, which covers exactly
    one of the reasons `git add` can fail; this covers the rest. It warns rather
    than raising on purpose — a board move failing hard at 3 AM is worse than a
    board move that is loud about being incomplete, and the runner's stdout is
    the run log.
    """
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        first = detail[0] if detail else f"exit {result.returncode}"
        print(f"  ! board: {what} failed — {first}; the commit after it will be a no-op", flush=True)


# `git commit` says one of these when the index is empty. That is the *expected*
# outcome for a board commit — a move that changed no bytes, or a previous run
# that already committed it — and it is the only non-zero exit that is benign.
_NOTHING_STAGED = re.compile(
    r"nothing to commit|no changes added to commit|nothing added to commit",
    re.IGNORECASE,
)


def _git_commit(root: Path, message: str, what: str) -> bool:
    """Commit the staged board changes. Returns whether a commit was created.

    Replaces three `# gate-ok(subprocess_result_checked)` appeals, and fixes the
    latent bug they were shielding. The appeals were right that an empty index
    exits non-zero and is normal here; they were wrong that this makes the
    result unreadable. Passing `check=False` and looking at nothing swallows
    *every other* reason a commit fails too — a rejecting pre-commit hook, a
    stale `index.lock`, gpg signing — and on the runner's card-move path that is
    the worst possible thing to be quiet about: the card moves on disk, the move
    never reaches git, and "resumable from disk alone" silently stops being true.

    So: separate the one benign non-zero from the rest, and be loud about the
    rest. Warns rather than raising, for the same reason `_warn_if_failed` does
    — a board move failing hard at 3 AM is worse than one that is loud about
    being incomplete.
    """
    result = subprocess.run(
        ["git", "commit", "-m", message], cwd=root, check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        return True
    if _NOTHING_STAGED.search(f"{result.stdout or ''}\n{result.stderr or ''}"):
        return False
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    first = detail[0] if detail else f"exit {result.returncode}"
    print(f"  ! board: committing {what} failed — {first}", flush=True)
    return False


def move(root: Path, card: Card, to_lane: str, commit: bool = True) -> Card:
    """`git mv` + rewrite `state:` + commit, as one commit (`03_board.md` §4).

    The two writes are ordered so that a crash between them leaves the lane and
    `state:` disagreeing, which `card_schema` reports as a half-done move. That
    is the crash-consistency check the denormalised `state:` field exists for,
    and it is strictly better than a crash that leaves no trace.
    """
    if to_lane not in LANES:
        raise ValueError(f"{to_lane!r} is not a lane")
    target = board_dir(root) / to_lane / card.path.name
    target.parent.mkdir(parents=True, exist_ok=True)

    if subprocess.run(["git", "mv", str(card.path), str(target)], cwd=root,
                      capture_output=True).returncode != 0:
        card.path.rename(target)

    moved = Card.load(target, to_lane)
    moved.write({"state": to_lane})

    if commit:
        staged = subprocess.run(["git", "add", "-A", str(board_rel(root))], cwd=root, check=False,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        _warn_if_failed(staged, f"staging the move of {card.id}")
        _git_commit(root, f"board: {card.id} {card.lane} → {to_lane}",
                    f"the move of {card.id}")
    return moved


def commit_board(root: Path, message: str, extra_paths: tuple[str, ...] = ()) -> None:
    """Commit whatever changed under `Board/`, and every `GENERATED_VIEWS` file present.

    Never `-a`: the runner must not be able to sweep up someone else's work in
    progress. The existence check on each view is not defensive noise — `git
    add` fails the *whole* pathspec when one entry matches nothing, so naming a
    file that is not there yet silently stages nothing at all and the commit
    becomes a no-op. Found by a test asserting `attempts` reaches disk before
    the worker starts, which is the one bookkeeping guarantee that bounds a
    reboot loop; it would otherwise have surfaced on the first night in a fresh
    clone, as a card retried forever. It matters more now than when `Digest.md`
    was the only view: a repo that has run `digest` but never `ingest` has one
    of the three on disk, which is the normal case rather than the edge.

    `extra_paths` is for the rare other committed, non-Board file a caller needs
    swept into the same commit (e.g. `stale_sweep.STATUS`) — each is checked to
    exist first, for the same reason the views are: a missing path would zero
    out the whole `git add`.
    """
    paths = ([str(board_rel(root))]
            + [v for v in GENERATED_VIEWS if (root / v).exists()]
            + [p for p in extra_paths if (root / p).exists()])
    staged = subprocess.run(["git", "add", "-A", "--", *paths], cwd=root,
                            check=False, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    _warn_if_failed(staged, f"staging {', '.join(paths)}")
    _git_commit(root, message, ", ".join(paths))
    _normalize_after_write(root)


def _normalize_after_write(root: Path) -> None:
    """Re-materialise any CRLF the write left in the working tree.

    **The line-ending problem has been fixed three times, at three different
    writers, and a fourth writer keeps arriving.** `python-write-text-crlfs-the-
    file-on-windows` (2026-08-14) fixed `Path.write_text`; `fresh-worktree-never-
    gets-normalized` (2026-08-13) fixed `prepare_worktree`; on 2026-08-17 it was
    an agent's own file tools and a human's editor, neither of which this package
    can reach. Karel, that day: *"It's like we fixed this 7 times or so already."*

    Chasing writers cannot converge, because any tool that opens a file in text
    mode on Windows is a new one. So this is fixed at the **funnel** instead: every
    board write in the framework ends here — each `boardcmd` verb, `board.move`,
    `chores`, the runner's card moves, `ingest`'s cards and views — and whatever
    put CRLF on disk, it is on disk by now.

    The condition is worth removing rather than tolerating, but not because it
    breaks anything: a CRLF card costs a `git status` that reads dirty with an
    empty diff, and an on-disk size that disagrees with the size the runner
    measures by a few bytes. What it actually costs is a **blocked preflight** and
    a manual remedy, which is a real toll for a harmless state.

    Asked before acted on, because the answer is almost always "nothing to do" and
    that answer costs one `git ls-files --eol`. Best-effort and never fatal: a
    board move failing at 3 AM over line endings would be far worse than the line
    endings, which is the same trade `_warn_if_failed` makes one line up.
    """
    try:
        from nightshift import normalize_worktree
        if normalize_worktree.worktree_crlf_files(root):
            normalize_worktree.normalize(root)
    except Exception as exc:                      # noqa: BLE001 — see the docstring
        print(f"warning: could not normalise line endings after the board write ({exc})")
