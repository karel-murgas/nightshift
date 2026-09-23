"""A Bash command line as the simple commands a shell would run — for the hooks that
judge a command by *which program it runs*, not by what text it contains.

A guard that searches the whole string denies commands that merely mention what it
guards: `grep -E "FAIL|preflight"` split on the `|` inside its quotes, and a heredoc
body read as commands (`slow-command-guard-matched-a-grep-pattern`). This lexes
quote-aware, drops heredoc bodies and redirect targets, and names each command's
executable.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

#: Characters that form operator tokens. Newline is one: it separates commands.
_PUNCTUATION = "();<>|&\n"

#: Words that open a compound command rather than name a program.
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "time",
                       "!", "{", "}"})

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
#: `<<<word`: a here-string, data like a heredoc body.
_HERESTRING = re.compile(r"<<<\s*(?:'[^']*'|\"[^\"]*\"|\S+)")


def strip_heredocs(command: str) -> str:
    """`command` with every here-document body (and its closing delimiter) removed.

    A heredoc body is stdin data, never command text; reading it as commands makes a
    commit message that mentions `git push` look like a push.
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


@dataclass(frozen=True)
class Command:
    """One simple command: its words (keywords and `VAR=x` prefixes dropped) and
    whether its stdin is the previous command's pipe."""
    words: tuple[str, ...]
    piped: bool

    @property
    def program(self) -> str:
        """The executable's bare name, lower-cased, without `.exe`."""
        if not self.words:
            return ""
        name = Path(self.words[0]).name.lower()
        return name[:-4] if name.endswith(".exe") else name


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.escape = ""          # an unquoted Windows path keeps its backslashes
    try:
        return list(lexer)
    except ValueError:
        return None


def commands(command: str) -> list[Command] | None:
    """The simple commands in `command`, in order, or None when its quoting does not
    balance (a caller that cannot lex a command should fail open)."""
    tokens = _tokens(strip_heredocs(command))
    if tokens is None:
        return None
    out: list[Command] = []
    words: list[str] = []
    piped = False
    drop_next = False
    for tok in tokens:
        if tok and all(ch in _PUNCTUATION for ch in tok):
            if "<" in tok or ">" in tok:
                drop_next = True
                continue            # a redirect: the command goes on, its target does not
            if words:
                out.append(Command(tuple(words), piped))
                words = []
            piped = tok in ("|", "|&")
            continue
        if drop_next:
            drop_next = False
            continue
        if not words and (tok in _KEYWORDS or _ASSIGNMENT.match(tok)):
            continue
        words.append(tok)
    if words:
        out.append(Command(tuple(words), piped))
    return out
