"""Build the fixture world once per process, and hand every test a copy.

This suite's job is git orchestration, so it shells out to real git and should
keep doing so — a mocked git would be testing the mock, and the two bugs found
on 2026-08-16 (the install receipt, and `update` offering to delete a live
`project_script` hook) were only reachable against a real tree. The cost is not
real git. It is *reconstructing the world once per assertion*.

Measured 2026-08-17, per fixture, median of five:

    write the four project files        1 ms
    git init + three git config       135 ms
    git add + git commit               85 ms
    init.apply(build_plan), 36 files  247 ms
    ------------------------------------------
    the whole thing                   467 ms

    shutil.copytree of the result      68 ms
    git clone --local of the result   160 ms

`test_update.py` spent 11.5 of its 13.3 seconds in `setup`, and every one of
the eight slowest entries pytest reported was setup rather than a test body.
`copytree` beats `clone --local`, which still spawns git twice and checks the
tree out — the hardlinked object store does not pay for that.

Two entry points, because the fixtures come in two shapes:

`repo_copy` is for a builder that takes no arguments — same tree every time, so
build it once and copy the finished thing. That is the full 467 → 68 ms.

`git_init` is for the builders that *are* parameterised (`_repo(tmp_path,
*cards)`, `_project(tmp_path, manifest=...)`). Their files differ per call so
the tree cannot be copied, but `git init` plus the identity config is identical
in all of them, and a copied `.git` skeleton costs 18 ms against 135.

**The template is never handed out.** A test that mutated a shared template in
place would be a worse failure than a slow suite: it would redden a different
test, in a different file, depending on execution order. So neither entry point
can return a template — both copy — and `_template` re-walks it on every call
and refuses to serve one that has changed since it was built.

Not everything can be copied, and the exceptions are deliberate:

  - `test_freshness.py` clones a real bare origin. The clone's `.git/config`
    holds an absolute `url`, so every copy would point back at the *template's*
    origin and two tests would be pushing into one remote.
  - the worktree paths in `test_runner.py` record absolute `gitdir` paths under
    `.git/worktrees/`.

Both build genuinely, and should stay that way.
"""
from __future__ import annotations

import atexit
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

_ROOT: Path | None = None
_BUILT: dict[str, Path] = {}
#: template name -> the walk taken the moment it finished building
_FINGERPRINT: dict[str, tuple] = {}


def _root() -> Path:
    """One temp directory per process, holding every template built into it.

    Per *process*, not per session: under `-n auto --dist loadfile` each xdist
    worker is its own interpreter and builds its own, which is what we want —
    they must not share a directory they would both be writing into.
    """
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="nightshift-fixture-templates-"))
        atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
    return _ROOT


def _walk(root: Path) -> tuple:
    """Everything under `root` as (relative path, size, mtime), sorted.

    Enough to catch a file rewritten, truncated, added or removed in place,
    without hashing ~70 files on a path whose whole purpose is to be fast.
    """
    return tuple(
        (str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
    )


def _template(name: str, build: Callable[[Path], None]) -> Path:
    """The built template for `name`, building it the first time it is asked for.

    Private on purpose — see the module docstring. Nothing outside this file
    can get hold of a path that is not already a copy.
    """
    if name not in _BUILT:
        target = _root() / name
        build(target)
        _BUILT[name] = target
        _FINGERPRINT[name] = _walk(target)
    elif _walk(_BUILT[name]) != _FINGERPRINT[name]:
        raise AssertionError(
            f"the {name!r} fixture template changed after it was built. Some "
            "test is working on the template itself instead of its copy, which "
            "reddens a different test depending on execution order. Find the "
            "caller that kept the path, and give it a copy."
        )
    return _BUILT[name]


def repo_copy(name: str, dest: Path, build: Callable[[Path], None]) -> Path:
    """A private copy at `dest` of the tree `build` produces, built once.

    `build` is handed the directory to create and must be deterministic: it
    runs for the first caller only, and every later caller gets that tree.
    """
    shutil.copytree(_template(name, build), dest, dirs_exist_ok=True, symlinks=True)
    return dest


def git_init(root: Path, *, branch: str | None = None,
             email: str = "t@example.com", name: str = "t",
             autocrlf: str | None = None,
             extra_config: tuple[tuple[str, str], ...] = ()) -> Path:
    """`git init` plus this suite's identity config, delivered as a copy.

    The arguments mirror what each fixture already said rather than settling on
    one house style, because several of them are testing the difference.
    `branch=None` is a bare `git init -q` — whatever this box's
    `init.defaultBranch` is, which some tests then override with `branch -M` —
    and `autocrlf=None` leaves the setting alone, because a fixture that never
    set it is relying on the machine's answer and this must not quietly change
    what it tests.

    One skeleton is built per distinct combination, a handful per process, and
    copied thereafter.
    """
    # Hashed, not spelled out: the key is a directory name, and Windows would
    # serve one skeleton for both `user.name = t` and `user.name = T` — which
    # `test_a_different_config_is_a_different_skeleton` caught.
    settings = (branch, email, name, autocrlf, extra_config)
    key = f"gitdir-{branch or 'default'}-" + hashlib.sha1(
        repr(settings).encode("utf-8")).hexdigest()[:12]

    def build(target: Path) -> None:
        target.mkdir(parents=True)
        _run(target, "init", "-q", *(["-b", branch] if branch else []))
        if autocrlf is not None:
            _run(target, "config", "core.autocrlf", autocrlf)
        _run(target, "config", "user.email", email)
        _run(target, "config", "user.name", name)
        for setting, value in extra_config:
            _run(target, "config", setting, value)

    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_template(key, build) / ".git", root / ".git")
    return root


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")
