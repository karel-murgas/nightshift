#!/usr/bin/env python3
"""PreToolUse hook: refuse to publish a commit the preflight has not validated.

This is the enforcement point that makes `nightshift.preflight` mandatory rather
than a script someone remembers to run (`10_self_improvement.md` §4). It fires
on `git push`, `git merge`, and `gh pr create` — the operations that carry a
branch's work outward — and denies the tool call when the commit being published
has no preflight receipt.

**It is instant, and that is the design.** The expensive checks (pytest, the
gate suite) ran once, in the preflight, and left a receipt keyed by SHA. This
hook only resolves the relevant SHA and looks it up. A hook that itself ran the
suite would run it on every push attempt, which is exactly the cost the receipt
exists to avoid.

**Which SHA it checks:**

* `git push` / `gh pr create` — `HEAD`. You are publishing the branch you are on.
* `git merge <ref>` — the tip of `<ref>`. You validate a feature branch, then
  switch to the integration branch to merge it; the receipt for the feature tip
  is what must exist, not one for the integration branch you are sitting on. The
  receipt file keeps the last several SHAs precisely so this cross-branch lookup
  resolves.

**Which REPOSITORY it checks: the one the command acts on, never the session's.**

It used to be the session's. `_repo_root()` called `manifest.find_root()`, which
answers from the *process* working directory, so a session rooted in repo A that
ran `git -C ../B push` had its receipt looked up in A — a receipt about work the
push does not contain. Observed 2026-08-03: a push went out on a commit whose
preflight had failed, from a session rooted in the consuming project doing work
in the framework repo. Driving one repo from a session rooted in another is
routine here, so that hole was open on the normal workflow, not an exotic one.

The target repository is resolved from the command text, in this order:

1. `--work-tree`, then `--git-dir` (a `…/.git` argument resolves to its parent),
2. the accumulated `git -C <path>` chain,
3. the working directory the command runs in — the session's `cwd` from the hook
   payload, moved by any `cd` earlier in the same command string.

That directory is then handed to `git rev-parse --show-toplevel`, and the receipt
is read from the repository root git itself names. Asking git beats walking up
for `.ai/`: a nested checkout would otherwise resolve to an enclosing project's
manifest, which is the same class of wrong answer this whole change is fixing.

**A repository that does not use nightshift is never blocked.** No
`.ai/manifest.toml` at the resolved root means the repo is not ours, and the push
goes through untouched. Without that, the guard would deny every push to every
unrelated repository the maintainer touches from an agent session, and a guard
that does that gets switched off — which puts the hole back and removes the
protection with it.

**A command it cannot attribute to a repository is DENIED.** This is the one
place the hook's old "when in doubt, allow" stance is inverted, and deliberately:
failing open on a command the parser could not follow restores the exact hole
above, silently. Silent-and-green is the failure mode this codebase keeps finding
(`check-stopped-checking` in the corrections log), so the cost is paid the other
way — an occasional false denial, with a message saying how to write the command
so the guard can see the repository.

Fail-open survives in the two places where it is still right: a repository that
is not a nightshift project (above), and an *unhandled exception* anywhere in the
hook (see `main`). A crashing guard must never wedge a session's git; that is a
bug in this file, not an ambiguous command, and denying every git operation until
someone edits a hook is the worse failure.

**What it still cannot see** — stated, because a parser's limit written down is
worth more than a claim of completeness:

* a guarded op behind an alias, a shell function, a `Makefile` target, or a
  variable (`$GIT push`). Nothing in the token stream says "git", so nothing
  fires. The `gh`/`git` token appearing after a wrapper (`xargs git push`) *is*
  caught, as a denial, because there the intent is legible and the repo is not.
* `pushd`/`popd`: directory stacks are not modelled, so anything after one is
  treated as an unknown directory and denied.
* a `cd` whose argument is a variable or a command substitution — the token does
  not name an existing directory, so it lands in the same denial.
* the hook's own wiring. `settings.hooks.json` matches on `Bash(git push:*)`,
  i.e. a command *starting* with `git push`; whether a compound command reaches
  this module at all is that matcher's business, not this parser's.

Deny is `permissionDecision: "deny"` with a reason; the tool call never runs.
No LLM (`00_architecture.md` §12); it reads a JSON file and shells `git`.

Wired as `python -m nightshift.hooks.preflight_guard` in a consuming project's
`.claude/settings.json` (07_portability.md §8 step 3). Exercise it by hand:

    echo '{"cwd": "/repo", "tool_input": {"command": "git -C ../other push"}}' \\
        | python -m nightshift.hooks.preflight_guard
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from nightshift.manifest import AI_DIR, MANIFEST_NAME

# Every character `shlex(punctuation_chars=True)` lexes as an operator rather than
# as part of a word. A token made only of these is shell syntax, not a command.
_PUNCTUATION = "();<>|&"

# `bash -c '<string>'` is followed rather than waved through, up to this much
# nesting. The limit stops a pathological string from spinning the hook; a
# guarded-looking command below it is denied rather than lost.
_MAX_DEPTH = 3

# Used ONLY on the paths where the structure has already been lost (unlexable
# quoting, nesting past the limit). It is generous on purpose — being wrong here
# costs a false denial, and being silent costs the hole this module exists to
# close. On a clean parse it is never consulted, so `grep -r "git push" .` is not
# a false positive: that lexes fine, and its `git push` is one quoted word.
_LOOKS_GUARDED = re.compile(
    r"\bgit\b.*?\b(?:push|merge)\b|\bgh\b.*?\bpr\b.*?\bcreate\b", re.DOTALL)


def _git(repo_root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo_root,
                         capture_output=True, text=True, timeout=10,
                         encoding="utf-8", errors="replace")
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _allow() -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _cannot_tell(what: str) -> dict:
    """Deny because the repository could not be identified — never because the
    receipt was missing. The two are different failures and must read differently:
    this one is the guard admitting it cannot see, and the fix is on the command's
    side, so the message has to say how to rewrite it."""
    return _deny(
        f"the preflight guard could not tell which repository this command acts on "
        f"({what}), so it cannot check that repository's preflight receipt. A guard "
        f"that cannot see its target refuses rather than waving the command through "
        f"— on 2026-08-03 a push went out on a commit whose preflight had failed, "
        f"because the guard was looking at the session's repo instead of the one "
        f"being pushed. Make the repository explicit and run it on its own: "
        f"`git -C <path> push …` (or `git -C <path> merge <ref>`), or run the command "
        f"from a session rooted in that repository."
    )


# --- reading the command ------------------------------------------------------


def _tokenize(command: str) -> list[str] | None:
    """Shell words plus operator tokens, or None if the string cannot be lexed.

    `shlex.split` is not enough. It splits on whitespace only, so `cd x; git push`
    lexes as `['cd', 'x;', 'git', 'push']` — the `;` disappears into a word and the
    `cd` that moved the push into another repository is invisible to anything
    reading the token list. `punctuation_chars=True` makes `;`, `&&`, `||`, `|` and
    the parentheses tokens in their own right, which is the whole basis for
    following the working directory across a compound command.

    **Backslash escaping is switched off**, the one place this lexer is deliberately
    not a shell. POSIX `shlex` consumes backslashes, so an unquoted Windows path —
    `git -C C:\\Users\\k\\ns push`, which is how it gets typed on the machine this
    framework runs on — lexes as `C:Userskns`, names no directory, and the guard
    denies a legitimate push. Quoting still works, so a path with spaces is still
    expressible. What is given up is the POSIX `foo\\ bar` spelling of a space, which
    now lexes as two tokens: `cd` then sees two arguments, calls the directory
    unknown, and denies. Both halves of the trade fail in the safe direction.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    try:
        return list(lexer)
    except ValueError:
        return None


def _split(tokens: list[str]) -> list[tuple[str, object]]:
    """`[("cmd", argv) | ("op", token)]` in order: simple commands, separated by
    the operator tokens between them. Redirections split a command too, which is
    harmless — the redirect target becomes a one-word "command" that matches
    nothing."""
    out: list[tuple[str, object]] = []
    buf: list[str] = []
    for tok in tokens:
        if tok and all(ch in _PUNCTUATION for ch in tok):
            if buf:
                out.append(("cmd", buf))
                buf = []
            out.append(("op", tok))
        else:
            buf.append(tok)
    if buf:
        out.append(("cmd", buf))
    return out


def _join(base: Path | None, rel: str) -> Path | None:
    """`rel` resolved against `base`, or None when it cannot be. An absolute `rel`
    needs no base; a relative one against an unknown base stays unknown, and that
    unknown is what the caller turns into a denial."""
    path = Path(rel).expanduser()
    if path.is_absolute():
        return path
    return None if base is None else base / path


def _cd(args: list[str], cwd: Path | None) -> Path | None:
    """Where a `cd` lands, or None for anything not modelled — no argument (home),
    `cd -` (the previous directory), several arguments. None propagates into a
    denial for whatever follows, which is the right direction: a `cd` the guard
    cannot follow is precisely how a push ends up in an unexamined repo."""
    paths = [a for a in args if not a.startswith("-")]
    if len(paths) != 1 or paths[0] == "-":
        return None
    return _join(cwd, paths[0])


def _hides_a_guarded_op(argv: list[str]) -> bool:
    """A guarded op run through some other program — `xargs git push`, `sudo git
    merge x`. The tokens say what is being done but not from where, so this is a
    denial rather than a miss. Requires a bare `git`/`gh` *token* followed by the
    subcommand, so `grep git .` and `echo "git push"` (one quoted word) do not
    match."""
    for n, tok in enumerate(argv[1:], start=1):
        after = argv[n + 1:]
        if tok == "git" and any(a in ("push", "merge") for a in after):
            return True
        if tok == "gh" and "pr" in after and "create" in after:
            return True
    return False


# --- resolving the repository -------------------------------------------------


def _target_dir(argv: list[str], cwd: Path | None) -> tuple[str | None, list[str], Path | None]:
    """`(subcommand, tail, directory)` for a `git`/`gh` command, with `subcommand`
    None when this is not a guarded op. `directory` is None when the command names
    one the guard cannot resolve.

    The `-C` skipping is not new — the old `_target_sha` already stepped over
    `-c`/`-C` pairs to find the subcommand, and then threw the path away. Keeping
    that value is most of this fix.
    """
    if argv[0] == "gh":
        # `gh` has no working-directory option; `-R owner/name` names a *remote*,
        # not a path, so the local repo is always the one the command runs in.
        if argv[1:3] != ["pr", "create"]:
            return None, [], None
        return "push", [], cwd
    if argv[0] != "git":
        return None, [], None

    rest = argv[1:]
    i = 0
    c_dir = cwd
    git_dir: str | None = None
    work_tree: str | None = None
    while i < len(rest) and rest[i].startswith("-"):
        tok = rest[i]
        if tok == "-C":
            if i + 1 >= len(rest):
                # `git -C` with nothing after it: no subcommand follows either, so
                # there is no guarded op to attribute. git will reject it itself.
                return None, [], None
            c_dir = _join(c_dir, rest[i + 1])
            i += 2
        elif tok == "-c":
            i += 2
        elif tok.startswith("--git-dir="):
            git_dir = tok.split("=", 1)[1]
            i += 1
        elif tok == "--git-dir":
            git_dir = rest[i + 1] if i + 1 < len(rest) else None
            i += 2
        elif tok.startswith("--work-tree="):
            work_tree = tok.split("=", 1)[1]
            i += 1
        elif tok == "--work-tree":
            work_tree = rest[i + 1] if i + 1 < len(rest) else None
            i += 2
        else:
            i += 1
    if i >= len(rest):
        return None, [], None
    sub, tail = rest[i], rest[i + 1:]
    if sub not in ("push", "merge"):
        return None, [], None

    # `-C` is applied by git before the other two, so they resolve against it.
    if work_tree is not None:
        target = _join(c_dir, work_tree)
    elif git_dir is not None:
        resolved = _join(c_dir, git_dir)
        target = resolved.parent if resolved is not None and resolved.name == ".git" else resolved
    else:
        target = c_dir
    return sub, tail, target


def _toplevel(directory: Path) -> Path | None:
    """The repository root git itself reports for `directory`, or None if that
    directory is not in a git work tree. Not a walk up looking for `.ai/`: a
    checkout nested inside another project would find the enclosing project's
    manifest, which is the same wrong-repo answer this module is fixing."""
    out = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=10,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        return None
    line = (out.stdout or "").strip()
    return Path(line).resolve() if line else None


def _sha(root: Path, sub: str, tail: list[str]) -> str | None:
    if sub == "push":
        return _git(root, "rev-parse", "HEAD") or None
    ref = next((a for a in tail if not a.startswith("-")), None)
    if ref is None:
        return None   # `git merge --continue` / `--abort` — not a publish
    return _git(root, "rev-parse", ref) or None


def _check(argv: list[str], cwd: Path | None) -> dict | None:
    """The verdict for one `git`/`gh` command, or None when there is nothing to say
    — not a guarded op, not a git repo, not a nightshift project, or already
    validated."""
    sub, tail, directory = _target_dir(argv, cwd)
    if sub is None:
        return None
    if directory is None:
        return _cannot_tell("no working directory could be resolved for it")
    try:
        directory = directory.resolve()
    except OSError:
        return _cannot_tell(f"{directory} could not be resolved")
    if not directory.is_dir():
        return _cannot_tell(f"{directory} is not an existing directory")

    root = _toplevel(directory)
    if root is None:
        return None   # not a work tree; the command cannot succeed there anyway
    if not (root / AI_DIR / MANIFEST_NAME).is_file():
        return None   # not a nightshift project — not ours to guard

    sha = _sha(root, sub, tail)
    if sha is None:
        return None

    from nightshift import preflight

    if preflight.is_validated(root, sha):
        return None
    return _deny(
        f"preflight has not validated {sha[:8]} in {root}. Run "
        f"`python -m nightshift.preflight --root {root}` (or `--no-corrections "
        f"\"<reason>\"` if this branch has no lesson to log) and let it pass before "
        f"pushing/merging — it runs the gates, the audit and the test slice once, so "
        f"the loop's lessons are recorded before the work leaves the branch. Note the "
        f"repository named above: the receipt checked is the one belonging to the repo "
        f"this command acts on, not the one the session is rooted in."
    )


# --- walking the command string -----------------------------------------------


def _walk(items: list[tuple[str, object]], cwd: Path | None, depth: int) -> dict | None:
    """The first denial the command earns, or None.

    Carries a working directory across the whole string, so `cd ../other && git
    push` is checked against `../other`. `(` and `)` push and pop it, because a
    subshell's `cd` is undone when it closes — `(cd a && git push) && git push`
    checks `a` and then the original directory, which is what actually happens.
    """
    stack: list[Path | None] = []
    cur = cwd
    for kind, item in items:
        if kind == "op":
            # Punctuation runs lex as one token (`)&&`), so read them character by
            # character rather than comparing whole tokens.
            for ch in str(item):
                if ch == "(":
                    stack.append(cur)
                elif ch == ")":
                    cur = stack.pop() if stack else None
            continue
        argv = _strip_assignments(list(item))  # type: ignore[arg-type]
        if not argv:
            continue
        head = argv[0]
        if head in ("bash", "sh", "zsh") and len(argv) >= 3 and argv[1] == "-c":
            verdict = _evaluate(argv[2], cur, depth + 1)
            if verdict is not None:
                return verdict
            continue
        if head == "cd":
            cur = _cd(argv[1:], cur)
            continue
        if head in ("pushd", "popd"):
            cur = None
            continue
        if head in ("git", "gh"):
            verdict = _check(argv, cur)
            if verdict is not None:
                return verdict
            continue
        if _hides_a_guarded_op(argv):
            return _cannot_tell(f"`{head}` runs git for it, and the token stream does "
                                f"not say from which directory")
    return None


def _strip_assignments(argv: list[str]) -> list[str]:
    """Drop leading `VAR=value` prefixes so `GIT_DIR=x git push` still reads as a
    git command."""
    n = 0
    while n < len(argv) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[n]):
        n += 1
    return argv[n:]


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# A here-string: `cat <<<word`. Its content is data, like a here-document's, and it is
# removed for the same reason — but the reason it is removed *here* rather than left to
# the lexer is sharper. `<<<` lexes as an operator that `_split` does not know, and the
# command that follows it on the next line then escapes `_hides_a_guarded_op` entirely:
# `cat <<<word` then `git -C other push` was ALLOWED. A guard that fails closed on prose
# it cannot lex, and open on a real push behind an unusual redirect, has its caution
# pointed the wrong way.
_HERESTRING = re.compile(r"<<<\s*(?:'[^']*'|\"[^\"]*\"|\S+)")


def _strip_heredocs(command: str) -> str:
    """`command` with every here-document *body* removed, delimiters and all.

    A heredoc body is data the shell hands to a program's stdin; it is never parsed as
    command text. Reading it as if it were produces exactly one behaviour, and it is bad
    in both directions: a commit message that merely mentions `git push` makes the whole
    command look guarded, and an apostrophe anywhere in that prose ("the session's repo")
    then makes it unlexable — so the guard fails closed on a `git commit` that pushes
    nothing.

    Found the day the guard was rewritten, by it denying the commit of its own rewrite,
    and then denying the script written to investigate that. Both messages described the
    push incident the fix was about, in prose, with apostrophes in it.

    `<<<` is a here-string, not a here-document: the `[A-Za-z_]` after the optional quote
    declines to match it, and its content is a single word on the same line anyway.
    """
    out: list[str] = []
    lines = _HERESTRING.sub(" ", command).splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in _HEREDOC.finditer(line):
            tag = match.group(2)
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1                      # and the closing delimiter line itself
    return "".join(out)


def _evaluate(command: str, cwd: Path | None, depth: int) -> dict | None:
    command = _strip_heredocs(command)
    if depth > _MAX_DEPTH:
        return (_cannot_tell("it is nested deeper than the guard follows")
                if _LOOKS_GUARDED.search(command) else None)
    tokens = _tokenize(command)
    if tokens is None:
        return (_cannot_tell("its quoting does not balance, so it cannot be split "
                             "into commands")
                if _LOOKS_GUARDED.search(command) else None)
    return _walk(_split(tokens), cwd, depth)


def decide(command: str, cwd: Path | None) -> dict:
    """The hook's answer for one Bash command run in `cwd`. Public so tests and a
    human can ask the question without going through stdin."""
    return _evaluate(command, cwd, 0) or _allow()


def _session_cwd(payload: dict) -> Path:
    """Where the tool call would run: the session's `cwd` from the hook payload,
    falling back to this process's. The payload is the better source — the hook is
    a separate process and nothing promises it was started in the session's
    directory."""
    raw = payload.get("cwd") or ""
    return Path(raw) if raw else Path.cwd()


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        command = payload.get("tool_input", {}).get("command", "")
        if not command:
            print(json.dumps(_allow()))
            return 0
        print(json.dumps(decide(command, _session_cwd(payload))))
    except Exception:
        # A guard that crashes must fail open — never wedge a session's git. This
        # is the one fail-open left: an exception here is a bug in this file, not
        # an ambiguous command, and the ambiguous command is denied above.
        print(json.dumps(_allow()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
