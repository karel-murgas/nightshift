#!/usr/bin/env python3
"""The board's write verbs, as commands — CLI first, panel second.

Five gestures a person makes at a board have had no command behind them:
reordering a card within its lane, marking one verified, promoting a private
note into the inbox, writing a new note, and replacing a note's body. Every one
of them was previously a session — an agent reading a file, editing it and
committing it — which is the most expensive possible way to move one line of
frontmatter.

The standing rule for the control panel is that **a button may only ever POST to
a verb that already works from a command line**. The panel is a launcher, and a
launcher that owns logic is a second implementation of the board. So these land
here first, and the panel's job shrinks to calling them.

**Nothing here parses frontmatter.** Every write goes through
`board.set_fields`/`board.move` and commits through `board.commit_board`. That
module preserves field order and does not requote values, deliberately, because
a board is hand-written in one editor and machine-rewritten in another; a writer
that normalised quoting would produce a diff on every touch and the board's git
history would stop being readable. A regex over a card in this file would be a
second parser with none of that care.

**The private lane may be moved, never read.** `promote` relocates a file and
`edit` replaces a body it was handed; neither summarises, parses or reasons
about what is inside. `edit` splices at the frontmatter boundary — it never
looks at the body it drops, and it never prints it — which is the same boundary
`reconcile` keeps when it reads one field and nothing else. `hooks.ideas_fence`
derives "private" from a lane's absence from `board.LANES` and permits exactly
the git subcommands that relocate without printing; a helper here that read a
private note to decide anything is precisely what that fence exists to stop.

That fence is also *why* `edit` exists as a command. An agent cannot open a
private note — correctly — so editing one has to be an operation the human
drives, with the text arriving from their editor rather than from anything that
read the file.

**Scriptable, never interactive.** No prompts, no confirmations, no `input()`,
and a body always arrives as an argument, a file or stdin that was named
explicitly. The caller is a request handler with nobody to ask. That is also
what makes every verb testable.

No LLM anywhere in here (`00_architecture.md` §12) — it is a file mover.

    python -m nightshift.boardcmd reorder <card-id> <order>
    python -m nightshift.boardcmd verified <card-id>
    python -m nightshift.boardcmd promote <note.md>
    python -m nightshift.boardcmd note <note.md> --body-file -
    python -m nightshift.boardcmd edit <path-under-the-board> --body-file -

The sixth verb the panel needs — reviewing a card sitting in `review/` — is not
here. It is a dispatch rather than a board write, and it belongs to
`nightshift.drain`, which owns the lane's missing exit.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from nightshift import board, reconcile, textio
from nightshift.manifest import find_root

#: Where a promoted note lands. The inbox is the only lane that takes an
#: unclassified note, and `ingest` is what routes it from there.
INBOX = "inbox"

#: The lane `verified` accepts a card from. Deliberately exactly one: reaching
#: `done/` is the moment a human signed off after playing the thing, and the
#: lane that means "waiting to be played" is `testing/`. A card anywhere else
#: reaching `done/` through this verb would be a lane transition nobody made.
VERIFIED_FROM = "testing"


class BoardCommandError(RuntimeError):
    """A verb refused. Carries the sentence the caller should print.

    Refusals are values rather than tracebacks because the eventual caller is an
    HTTP handler: it needs a reason it can render, and a stack trace is not one.
    """


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, check=False, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _bare_name(name: str) -> str:
    """`name` as a single markdown filename, or a refusal.

    A separator in a name written by a request handler is the only way any verb
    here could write outside the lane it names, so it is refused rather than
    normalised — `..` normalises to something perfectly plausible.
    """
    if not name.strip():
        raise BoardCommandError("no filename given")
    if any(sep in name for sep in ("/", "\\")) or name in (".", ".."):
        raise BoardCommandError(
            f"{name!r} is a path, not a filename — a verb writes into one lane and "
            f"names the file inside it")
    return name if name.lower().endswith(".md") else f"{name}.md"


def _move_file(root: Path, source: Path, target: Path) -> None:
    """Relocate a tracked-or-untracked board file, without opening it.

    `git mv` keeps the rename in the index; the fallback covers a file git does
    not track yet, which is the ordinary state of a note nobody has committed.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    moved = _git(root, "mv", str(source), str(target))
    if moved.returncode != 0:
        source.rename(target)


def _find(root: Path, card_id: str) -> board.Card:
    card = board.find(root, card_id)
    if card is None:
        raise BoardCommandError(f"no card with id {card_id!r} in any lane")
    return card


def reorder(root: Path, card_id: str, order: str) -> str:
    """Verb 1 — set a card's `kanban_order`, leaving every other field alone.

    The field already exists in the schema *because* Obsidian's Bases writes it
    when a card is dragged within a column, and `board.dispatch_order` already
    reads it. So the panel's drag and the Kanban's drag write the same field:
    one ordering, two ways to drive it, rather than two that can disagree.
    """
    if not order.strip():
        raise BoardCommandError("an empty kanban_order would read as 'never prioritised' — "
                                "pass the value to write")
    card = _find(root, card_id)
    before = card.fields.get("kanban_order", "")
    card.write({"kanban_order": order})
    board.commit_board(root, f"board: {card.id} kanban_order "
                             f"{before or '(unset)'} → {order}")
    return (f"{card.id}: kanban_order {before or '(unset)'} → {order} "
            f"(in {card.lane}/)")


def mark_verified(root: Path, card_id: str) -> str:
    """Verb 2 — `testing/` → `done/`, with the reconcile, as one act.

    The reconcile is not decoration. A card reaching `done/` is the point at
    which a human signed off, and `reconcile` is what makes the board's own view
    agree — Base Board groups on the `state:` property and never moves a file,
    so a lane change that skips it leaves the Kanban showing a card in a lane it
    has left. That is the same half-done transition the digest's attributed-
    answer nudge exists to catch, and it is invisible from the lane alone.
    """
    card = _find(root, card_id)
    if card.lane != VERIFIED_FROM:
        raise BoardCommandError(
            f"{card.id} is in {card.lane}/, not {VERIFIED_FROM}/ — this verb is the "
            f"sign-off on a card that was waiting to be played, and any other lane "
            f"reaching done/ through it would be a transition nobody made")
    board.move(root, card, "done")
    actions = reconcile.plan(root)
    errors = [a for a in actions if a.kind == "error"]
    reconcile.apply(root, actions, commit=True)
    tail = ""
    if actions:
        tail = f"; reconciled {len(actions) - len(errors)} further change(s)"
    if errors:
        tail += f", {len(errors)} of which need a human"
    return f"{card.id}: {VERIFIED_FROM}/ → done/{tail}"


def promote(root: Path, name: str) -> str:
    """Verb 3 — move one note out of the private lane and into `inbox/`.

    Reads nothing. The file is relocated and committed by name; what is inside
    it becomes visible to the routing machinery only because it is now in a lane
    that machinery is allowed to open. Nothing here opens it, prints it, or
    stamps a `state:` into it — the next `reconcile` does that, exactly as it
    does for a note typed straight into the inbox.
    """
    filename = _bare_name(name)
    source = board.board_dir(root) / board.PRIVATE_LANE / filename
    target = board.board_dir(root) / INBOX / filename
    if not source.is_file():
        raise BoardCommandError(f"no note called {filename!r} in "
                                f"{board.PRIVATE_LANE}/")
    if target.exists():
        raise BoardCommandError(f"{INBOX}/{filename} already exists — rename one of them")
    _move_file(root, source, target)
    board.commit_board(root, f"board: promoted {filename} to {INBOX}/")
    return f"{filename}: {board.PRIVATE_LANE}/ → {INBOX}/"


def create_note(root: Path, name: str, body: str) -> str:
    """Verb 4 — write a new bare note into `inbox/`.

    Bare on purpose: no frontmatter, no `state:`, no schema. That is what a note
    typed into the lane by hand looks like (`03_board.md` §1), and a note that
    arrived through a panel must be indistinguishable from one that did not —
    otherwise the classifier is looking at two shapes of the same thing. The
    next `reconcile` stamps `state: inbox` on it, same as any other.
    """
    filename = _bare_name(name)
    target = board.board_dir(root) / INBOX / filename
    if target.exists():
        raise BoardCommandError(f"{INBOX}/{filename} already exists — "
                                f"use `edit` to replace its body")
    target.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(target, _ends_with_newline(body))
    board.commit_board(root, f"board: new note {filename} in {INBOX}/")
    return f"{filename}: written to {INBOX}/ ({len(body)} B)"


def edit_body(root: Path, target: str | Path, body: str) -> str:
    """Verb 5 — replace a file's body, leaving its frontmatter byte-identical.

    The splice is the whole implementation: `board.FRONTMATTER` says where the
    block ends, everything before that point is kept verbatim, everything after
    it is replaced by the text handed in. The old body is never parsed, never
    printed and never judged — which is what makes this verb safe to point at a
    private note, and it is the same boundary `reconcile` keeps.

    A file with no frontmatter is replaced whole; that is the ordinary shape of
    a bare inbox note, and inventing a block for it would be this module writing
    frontmatter, which is exactly what it does not do.
    """
    path = _under_the_board(root, target)
    text = path.read_text(encoding="utf-8")
    block = board.FRONTMATTER.match(text)
    head = text[:block.end()] if block else ""
    textio.write_text_lf(path, head + _ends_with_newline(body))
    relative = path.relative_to(root).as_posix()
    board.commit_board(root, f"board: edited the body of {relative}")
    return (f"{relative}: body replaced ({len(body)} B), "
            f"frontmatter {'preserved' if block else 'absent, whole file replaced'}")


def _under_the_board(root: Path, target: str | Path) -> Path:
    """`target` as an existing file inside the board, or a refusal.

    The containment check is the guard, and it is checked against the resolved
    path rather than the string handed in: an eventual caller is a request
    handler, and "the panel wrote outside the board" is the one failure that
    could not be undone from the board.
    """
    path = Path(target)
    path = (path if path.is_absolute() else root / path).resolve()
    lanes = board.board_dir(root).resolve()
    if lanes not in path.parents:
        raise BoardCommandError(f"{target} is not inside {board.board_rel(root).as_posix()}/ "
                                f"— this verb writes board files only")
    if not path.is_file():
        raise BoardCommandError(f"{target} does not exist")
    return path


def _ends_with_newline(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"


def _body(args: argparse.Namespace) -> str:
    """The body text for `note`/`edit`, from wherever it was explicitly named.

    There is no implicit source and no prompt. A verb that fell back to reading
    a terminal would hang the request handler that called it, and one that
    defaulted to an empty body would silently blank a note — so an unnamed body
    is a usage error, which argparse already knows how to report.
    """
    if args.body is not None:
        return args.body
    if args.body_file == "-":
        return sys.stdin.read()
    path = Path(args.body_file)
    if not path.is_file():
        raise BoardCommandError(f"no such body file: {args.body_file}")
    return path.read_text(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nightshift.boardcmd", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to write in (default: found from the working directory)")
    subs = parser.add_subparsers(dest="verb", required=True)

    order = subs.add_parser("reorder", help="set a card's kanban_order within its lane")
    order.add_argument("card_id")
    order.add_argument("order", help="the fractional index to write; it sorts "
                                     "lexicographically, as Obsidian's own writes do")

    verified = subs.add_parser("verified", help=f"{VERIFIED_FROM}/ → done/, and reconcile")
    verified.add_argument("card_id")

    moved = subs.add_parser("promote", help=f"move one note into {INBOX}/ without reading it")
    moved.add_argument("name", help="the note's filename, no path")

    note = subs.add_parser("note", help=f"write a new bare note into {INBOX}/")
    note.add_argument("name", help="the note's filename, no path")

    edit = subs.add_parser("edit", help="replace a board file's body, keeping its frontmatter")
    edit.add_argument("path", help="path to the file, absolute or relative to the repo root")

    for sub in (note, edit):
        body = sub.add_mutually_exclusive_group(required=True)
        body.add_argument("--body", help="the body text itself")
        body.add_argument("--body-file", help="read the body from this file, or `-` for stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Before `parse_args`: `--help` prints this module's docstring, which carries
    # em-dashes and arrows, and a console defaulting to cp1252 cannot encode them —
    # so help would die with a UnicodeEncodeError instead of printing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    args = _parser().parse_args(argv)
    root = (args.root or find_root()).resolve()
    try:
        if args.verb == "reorder":
            print(reorder(root, args.card_id, args.order))
        elif args.verb == "verified":
            print(mark_verified(root, args.card_id))
        elif args.verb == "promote":
            print(promote(root, args.name))
        elif args.verb == "note":
            print(create_note(root, args.name, _body(args)))
        elif args.verb == "edit":
            print(edit_body(root, args.path, _body(args)))
    except BoardCommandError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
