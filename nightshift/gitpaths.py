"""Reading git's answer when the answer is a list of paths.

**Earned by a measurement, and by the second instance of it in one day**
(`00_architecture.md` §15). A board file is named the way a person names a
thought — `Stale README.md`, `Animation – attack.md`, `Regenerate soundtrack.md`
— and every reader in this package that turned git output into paths was written
against the slug-shaped names that fill its own tests. Two shapes of the same
defect:

* `.stdout.split()` splits on **whitespace**, so `Board/inbox/Stale README.md`
  becomes two paths, the second of them a file nobody has touched. Measured
  2026-08-21 on the origin project's own inbox, in `preflight._changed_paths`: a
  phantom `README.md` in the changed set. Five sites did this.
* `.splitlines()` survives a space and not the rest. Git **quotes** a path whose
  bytes are not printable ASCII (`core.quotepath`, on by default), so an en dash
  in a filename arrives as `"Board/ideas/Animation \342\200\223 attack.md"` — with
  the quotes, and with the bytes C-escaped. Stripping the quotes, as three of the
  readers did, leaves the escapes in place and the path does not exist. Six sites
  did this.

Neither ever changed an outcome, which is exactly why both survived: a fabricated
or mangled token classifies as `other`, and `other` selects no tests, refuses no
merge and hides no card. That is the `cost-only-visible-in-aggregate` shape — the
enforcement point has to be built while it still looks like over-engineering, and
the moment it stopped looking like that was `ingest.publish`, where a phantom path
would refuse a perfectly good push.

**The fix is `-z` everywhere, and the reason it lives in one module** is that
`-z` is not a style preference to be remembered per call site: it is the only
form of git's output that is unambiguous. With `-z` there is no quoting, no
escaping and no separator that can occur inside a name, because NUL cannot occur
in a path. Every function here is three lines around that one fact, and
`gates/git_path_lists.py` is what stops the eleventh site being written the old
way — it fails on a git argv that asks for paths without asking for `-z`.

Nothing here interprets a path. `suite.classify` decides what a path *is*; this
decides what the paths *are*.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

#: Porcelain codes that mean the entry carries a second path — the one it came
#: from. In `-z` output the two are separate records rather than joined by
#: ` -> `, so a reader that does not know this reads the origin path as a change
#: of its own and reports a file twice.
_TWO_PATHS = ("R", "C")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """git, captured, decoded explicitly.

    `encoding=` is not optional on Windows — `gates/subprocess_encoding.py`
    carries the night this cost. Public because a caller that needs a *different*
    git question should not grow its own copy of these three lines.
    """
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def split(blob: str) -> list[str]:
    """A NUL-separated run of paths, as paths. Empty records are dropped.

    Pure, and separately tested, because it is the one place the format is read:
    every function below is this plus an argv.
    """
    return [record for record in (blob or "").split("\0") if record]


def changed(root: Path, *args: str) -> list[str]:
    """`git diff --name-only -z <args>` — the paths a diff touches.

    An empty list when git refuses. That is what every caller did with the old
    text-splitting form (an unresolvable rev produced an empty string and an
    empty set), and the distinction that matters — "cannot tell" against
    "nothing changed" — is one the *caller* resolves at the merge-base, not here:
    see `preflight._changed_paths`, which is emphatic about it.
    """
    out = git(root, "diff", "--name-only", "-z", *args)
    return split(out.stdout) if out.returncode == 0 else []


def committed(root: Path, rev: str = "HEAD") -> list[str]:
    """The paths one commit touches.

    `diff-tree`, not `show`: plumbing takes `-z` cleanly and needs no `--format=`
    to stop it printing a commit header, and this is a question about a tree
    rather than something to show anybody.

    `--root` is what keeps that swap honest. `diff-tree` compares against a
    parent, so on a commit that has none it prints nothing at all — while `show`
    lists the whole tree. Without the flag, "which paths did this commit touch"
    would answer "none" for the first commit in a repository, which is the one
    commit where every path in it is new.
    """
    out = git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
              "--root", rev)
    return split(out.stdout) if out.returncode == 0 else []


def name_status(root: Path, *args: str) -> list[tuple[str, str]]:
    """`git diff --name-status -z <args>` as `(status, path)`.

    A second reader rather than a flag on `changed`, because `--name-status`
    interleaves a status field between the paths and **orders a rename the other
    way round from `status --porcelain`**: here git emits `R100`, then the old
    path, then the new one. Both are reported at their new path — the one that
    exists to be read — so the difference stays inside this function rather than
    being rediscovered by whoever writes the next caller.
    """
    out = git(root, "diff", "--name-status", "-z", *args)
    if out.returncode != 0:
        return []
    records = split(out.stdout)
    entries: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(records):
        code = records[index]
        if code[:1] in _TWO_PATHS and index + 2 < len(records):
            entries.append((code, records[index + 2]))
            index += 3
        else:
            entries.append((code, records[index + 1]))
            index += 2
    return entries


def status(root: Path, *args: str) -> list[tuple[str, str]]:
    """`git status --porcelain -z <args>` as `(code, path)`, renames resolved.

    The code is the two-character XY field; the path is the one that exists on
    disk now, which for a rename or a copy is the *first* of the two records git
    emits — the second is where it came from, and is dropped rather than reported
    as a change of its own.
    """
    out = git(root, "status", "--porcelain", "-z", *args)
    if out.returncode != 0:
        return []
    records = split(out.stdout)
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        code, path = record[:2], record[3:]
        if not path:
            continue
        if code[0] in _TWO_PATHS or code[1] in _TWO_PATHS:
            index += 1                      # the origin path, which is not a change
        entries.append((code, path))
    return entries


def dirty(root: Path, *args: str) -> bool:
    """Whether the working tree has anything at all to say."""
    return bool(status(root, *args))


def tracked(root: Path, *args: str) -> list[str]:
    """`git ls-files -z <args>` — what the index holds."""
    out = git(root, "ls-files", "-z", *args)
    return split(out.stdout) if out.returncode == 0 else []
