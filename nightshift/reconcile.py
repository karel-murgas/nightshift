"""Reconcile the board's two copies of a card's state.

The lane directory is the truth (`03_board.md` §1: one state = which directory
a card is in). `state:` in frontmatter is a denormalised copy, and it exists
because Obsidian's Base Board groups on a property, not a folder — and because
the gate uses the disagreement between the two as a crash-consistency check.

Base Board **never moves files.** A drag writes the new value into `state:` and
stops. So something has to make the folder catch up, and that something is this
script. There is no LLM anywhere in here, same rule as the gates
(`00_architecture.md` §12): every decision below is a file-state lookup.

The subtlety is that "folder and `state:` disagree" happens for two opposite
reasons and wants the opposite fix. The disambiguator is whether `state:` is
there at all:

    no `state:`              a note Karel just placed. The folder is the
                             intent -> stamp `state:` to match the folder.

    `state:` set, ≠ folder   a drag in the Kanban, or Karel flagging an idea
                             as ready. The state is the intent -> move the
                             file to match it.

That rule imposes exactly one habit: once a card is on the board, move it by
dragging in the Kanban, not by dragging the file in the sidebar. A manual file
move leaves the old `state:` behind, which reads as a drag, and the card gets
pulled back.

`ideas/` and the privacy boundary
---------------------------------
This is the only code in the repo permitted to look inside `Board/ideas/`, and
it reads exactly one field. It never reads the body, never judges, and does
exactly one thing: relocate a note Karel has explicitly flagged by setting a
`state:`. No judgment actor — triage, any subagent, the digest — may read that
folder at all. A note with no `state:` is invisible to this script beyond the
absence of the field.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from nightshift import board  # the single card model (`board-parser-convergence`)
from nightshift import textio  # cards are committed; write_text would CRLF them on Windows
from nightshift.manifest import find_root

# Where the board sits is `board.board_rel`'s answer since 07_portability.md §8
# step 4 put it behind `[board].root`. This module used to carry its own
# `Path("Board")` — a second home for the same fact, in a file whose entire job
# is to make the board agree with itself.
# Re-exported under this module's own name; the string is `board`'s.
PRIVATE = board.PRIVATE_LANE

# The lanes a card may be moved into. `ideas/` is deliberately absent: this
# script moves notes *out* of it when flagged, and never into it. Promoting a
# card back to private is Karel's to do by hand. Same tuple as `board.LANES`
# (for the same reason, stated there) — bound here under this module's own
# name because callers and tests already name it `reconcile.LANES`.
LANES: tuple[str, ...] = board.LANES

# `read_state`/`stamp_state` stay this module's public API (`Board/README.md`
# names them), but the frontmatter-block regex behind them is `board.FRONTMATTER`
# — the one copy across board.py/digest.py/reconcile.py.
_FRONTMATTER = board.FRONTMATTER
_STATE = re.compile(r"^(state:[ \t]*)(.*?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Action:
    kind: str  # "stamp" | "move" | "error"
    path: Path
    detail: str

    def __str__(self) -> str:
        return f"{self.path.as_posix()} — {self.kind}: {self.detail}"


def read_state(text: str) -> str | None:
    """The `state:` value, or None if there is no frontmatter or no such key."""
    block = _FRONTMATTER.match(text)
    if not block:
        return None
    found = _STATE.search(block.group(1))
    if not found:
        return None
    return found.group(2).strip().strip('"').strip("'") or None


def stamp_state(text: str, lane: str) -> str:
    """Write `state: <lane>`, adding a frontmatter block if there is none.

    A bare note is legal in `inbox/` (03_board.md §1) — Karel never writes
    frontmatter. Giving it a `state:` does not make it a triaged card; it has
    no `tier:` or `worker:` yet. It only makes it group correctly on the board.
    """
    block = _FRONTMATTER.match(text)
    if not block:
        return f"---\nstate: {lane}\n---\n\n{text.lstrip()}"
    body = block.group(1)
    if _STATE.search(body):
        new_body = _STATE.sub(rf"\g<1>{lane}", body, count=1)
    else:
        new_body = f"{body}\nstate: {lane}"
    return f"---\n{new_body}\n---\n" + text[block.end():]


def _cards(root: Path) -> list[tuple[Path, str]]:
    """Every card on the board, with the folder it currently sits in.

    `ideas/` is included, because a flagged note there is the whole point of
    this script. Only its `state:` field is ever read.
    """
    board_root = board.board_dir(root)
    out: list[tuple[Path, str]] = []
    if not board_root.is_dir():
        return out
    for path in sorted(board_root.rglob("*.md")):
        if path.name.upper() == "README.MD":
            continue
        parts = path.relative_to(board_root).parts
        out.append((path, parts[0] if len(parts) > 1 else ""))
    return out


def plan(root: Path) -> list[Action]:
    actions: list[Action] = []
    for path, lane in _cards(root):
        state = read_state(path.read_text(encoding="utf-8"))

        if not lane:
            actions.append(Action("error", path.relative_to(root),
                                  "card sits at the board root, in no lane — move it into one"))
            continue

        if state is None:
            if lane == PRIVATE:
                continue  # private and unflagged. Nothing here has been read.
            actions.append(Action("stamp", path.relative_to(root),
                                  f"no `state:` — the folder is the intent, stamping `{lane}`"))
            continue

        if state == lane:
            continue

        if state not in LANES:
            actions.append(Action("error", path.relative_to(root),
                                  f"`state: {state}` is not a lane — expected one of {', '.join(LANES)}"))
            continue

        why = "flagged ready" if lane == PRIVATE else "dragged"
        actions.append(Action("move", path.relative_to(root),
                              f"{why}: `state: {state}` but the file is in `{lane}/` — moving to `{state}/`"))
    return actions


def apply(root: Path, actions: list[Action], commit: bool) -> int:
    moved = 0
    for action in actions:
        source = root / action.path
        if action.kind == "stamp":
            lane = action.path.parts[1]
            textio.write_text_lf(source, stamp_state(source.read_text(encoding="utf-8"), lane))
        elif action.kind == "move":
            state = read_state(source.read_text(encoding="utf-8"))
            target = board.board_dir(root) / str(state) / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                print(f"  ! {action.path.as_posix()} — {target.relative_to(root).as_posix()} already exists, skipped")
                continue
            if subprocess.run(["git", "mv", str(source), str(target)], cwd=root,
                              capture_output=True).returncode != 0:
                source.rename(target)  # not tracked by git yet; a plain move is correct
            moved += 1
    if commit and any(a.kind in ("stamp", "move") for a in actions):
        staged = subprocess.run(["git", "add", "-A", str(board.board_rel(root))], cwd=root, check=False,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if staged.returncode != 0:
            detail = (staged.stderr or staged.stdout or "").strip().splitlines()
            print(f"  ! staging the reconciled board failed — "
                  f"{detail[0] if detail else f'exit {staged.returncode}'}; "
                  f"the commit after it will be a no-op")
        board._git_commit(root, f"board: reconcile {moved} move(s)",
                          f"reconcile of {moved} move(s)")
    return moved


def main(argv: list[str] | None = None) -> int:
    # Before `parse_args`, not after: `--help` prints this module's docstring, which
    # contains `≠`, and Windows' cp1252 console cannot encode it — so `--help` died
    # with a UnicodeEncodeError instead of printing help.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="python -m nightshift.reconcile", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="perform the changes (default: report only)")
    parser.add_argument("--commit", action="store_true", help="with --apply, commit the result")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to reconcile (default: found from the working directory)")
    args = parser.parse_args(argv)

    # **Not `Path(__file__).parent.parent`.** Installed as a package that is the
    # framework's own checkout, which has no board — so this reported "Board is
    # consistent" from inside every consuming project, whatever their board said.
    # It fails *open*, which is the worst direction for the one tool whose job is
    # to notice a half-finished move.
    root = (args.root or find_root()).resolve()
    actions = plan(root)
    if not actions:
        print("Board is consistent — every card's `state:` matches its lane.")
        return 0

    for action in actions:
        print(action)
    errors = sum(1 for a in actions if a.kind == "error")
    if args.apply:
        apply(root, actions, args.commit)
        print(f"\nApplied {len(actions) - errors} change(s); {errors} need a human.")
    else:
        print(f"\n{len(actions)} pending; {errors} need a human. Re-run with --apply.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
