"""The one way to run git in this package.

UTF-8, `errors="replace"` — `text=True` alone decodes with the locale codec and
silently turns a byte it cannot map into a `None` `.stdout` deep inside
subprocess's reader thread (see `gates/subprocess_encoding.py` for the incident
this closed). `run()` never raises on a non-zero exit; `run_safe()` is for a
caller that must survive git being entirely unreachable (no binary, `cwd`
gone). Path-list readers (`changed`, `committed`, `name_status`, `status`,
`dirty`, `tracked`) all ask git for `-z` output and split on NUL, because a
newline- or quote-based split mangles a filename git is free to contain either
(`gates/git_path_lists.py` is the regression guard). Absorbs what was
`nightshift/gitpaths.py` (since removed).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

#: Porcelain codes that mean the entry carries a second path — the one it came
#: from. In `-z` output the two are separate records rather than joined by
#: ` -> `, so a reader that does not know this reads the origin path as a change
#: of its own and reports a file twice.
_TWO_PATHS = ("R", "C")


def run(cwd: Path, *args: str, timeout: float | None = None,
        input: str | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """git, captured, decoded explicitly. `check=False` always — a non-zero exit
    is an ordinary answer, not a raise, and callers read `.returncode` for it.

    `input`/`env` are passed through as-is, for the few callers that need
    stdin (a `--stdin` argv over a long path list) or an environment override
    (`GIT_EDITOR=true`, so a replayed rebase does not try to open an editor
    this process has no terminal for) — genuine subprocess-level needs, not a
    reason to grow a second git runner."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False,
                          timeout=timeout, input=input, env=env)


def run_safe(cwd: Path, *args: str,
             timeout: float | None = None) -> subprocess.CompletedProcess | None:
    """Like `run()`, but `None` when git cannot answer at all — no binary, or
    `cwd` is gone — instead of raising. For a caller that must survive that: a
    doctor or a freshness reading that dies on a missing git takes down the
    thing consulting it."""
    try:
        return run(cwd, *args, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def text(cwd: Path, *args: str) -> str | None:
    """stdout, stripped — or `None` if git could not answer, or exited non-zero.
    Never raises."""
    result = run_safe(cwd, *args)
    return result.stdout.strip() if result and result.returncode == 0 else None


def text_or_empty(cwd: Path, *args: str, timeout: float | None = None) -> str:
    """stdout, stripped, or `""` on a non-zero exit. Unlike `text()`, does not
    catch a missing git binary — for call sites (hooks) that already assume one
    exists and want the exception to surface if it does not."""
    result = run(cwd, *args, timeout=timeout)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


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
    "nothing changed" — is one the *caller* resolves at the merge-base, not here.
    """
    out = run(root, "diff", "--name-only", "-z", *args)
    return split(out.stdout) if out.returncode == 0 else []


def committed(root: Path, rev: str = "HEAD") -> list[str]:
    """The paths one commit touches.

    `diff-tree`, not `show`: plumbing takes `-z` cleanly and needs no `--format=`
    to stop it printing a commit header, and this is a question about a tree
    rather than something to show anybody.

    `--root` is what keeps that swap honest: without it, `diff-tree` compares
    against a parent and prints nothing at all for a commit that has none — the
    one commit where every path in it is new.
    """
    out = run(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
             "--root", rev)
    return split(out.stdout) if out.returncode == 0 else []


def name_status(root: Path, *args: str) -> list[tuple[str, str]]:
    """`git diff --name-status -z <args>` as `(status, path)`.

    A second reader rather than a flag on `changed`, because `--name-status`
    interleaves a status field between the paths and orders a rename the other
    way round from `status --porcelain`: here git emits `R100`, then the old
    path, then the new one. Both are reported at their new path.
    """
    out = run(root, "diff", "--name-status", "-z", *args)
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
    emits — the second is where it came from, and is dropped rather than
    reported as a change of its own.
    """
    out = run(root, "status", "--porcelain", "-z", *args)
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
    out = run(root, "ls-files", "-z", *args)
    return split(out.stdout) if out.returncode == 0 else []
