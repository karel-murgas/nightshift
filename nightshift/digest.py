"""The morning digest — a deterministic report on the unattended work.

Every number here is a file-state lookup. That is the whole design constraint:
the digest is read at 7 AM to decide what to do with the day, so it has to be
trustworthy without being audited, and the way you make a report trustworthy is
to make every claim in it a fact about a file rather than a judgment. **There is
no LLM anywhere in here** — same rule as the gates (`00_architecture.md` §12).

**Restructured 2026-07-30 (`digest-reports-the-run`), and the reason matters.**
The previous two designs both reported *the board*: they diffed lane membership
against the previous `digest:` commit and described what had moved. Karel's use
case is not the board — it is "I come back in the morning after a long night run;
what failed, what needs my decision, what passed, what did the stale hunter
find?" A lane diff cannot answer any of those, and on 2026-07-30 it failed
visibly: it reported **"0 failed · Nothing failed"** for a night in which three
cards were dispatched, all three failed on one broken gate, the run aborted on
`CONSECUTIVE_FAILURE_STOP`, and the sweep then spent two and a half hours
returning zero usable verdicts. A failed attempt bounces the card back to
`tasks/` — the lane it came from — so the diff was empty. The same run reported
"3 finished", two of which were Karel's own daytime edits swept up because the
baseline was the last *digest* rather than the last *run*.

So the digest now reads `.ai/runs/records/` — each run's own account of itself
(`run_record.py`) — and the file has two halves that must not be confused:

1. **What happened, per run, newest first.** Sourced entirely from run records.
   Inside each run, Karel's questions in Karel's order: Failed → Decide →
   Passed → Stale hunter → Skipped. A run that was killed or aborted says so in
   its header. Every run since the last digest gets its own block, because a
   night that dies without writing a digest would otherwise never be reported at
   all — which is exactly how the 02:14 abort stayed invisible.
2. **Still waiting on you** — the standing board state, explicitly labelled as
   carry-over, and deliberately terse. Open decisions from earlier runs (the only
   queue that unblocks tonight, so it keeps its question inline), the `failed/`
   lane, a one-line count for `testing/`+`review/`, the starving queue, and the
   advisory nudges. The old design listed every `testing/` card with its summary
   and cost here; that is six paragraphs of things Karel already knows about,
   duplicating a Kanban lane he is looking at anyway, and it pushed the one new
   card off the top of the report. It is a count now.

**Daytime work is absent from half 1 by construction** (Karel, 2026-07-30). A
card he moves at 15:00 appears in no run record, so nothing can report it as
unattended work. It still shows up in half 2 if it is waiting on him, which is
the honest place for it. One consequence worth stating: `triage` is not a runner
phase — it runs at the lead tier in an interactive session — so triage's new
cards are daytime work and no longer get a "Proposed" section. The stale sweep's
fix-cards *are* run output and are reported under their run's Stale hunter.

Emitted to `Digest.md` at the repo root, which is the Obsidian vault root, so
Karel reads it in the same place he reads the board. **It is overwritten every
run** — nothing typed into it survives, which is why answers go in a card's
`## Thread`, never here.
"""
from __future__ import annotations

import datetime as _dt
import functools
import re
import subprocess
from pathlib import Path

from nightshift import board  # the single card model (`board-parser-convergence`)
from nightshift import corrections  # backlog() for the harvest-nudge line (12_corrections_lifecycle.md)
from nightshift import run_record  # the run's own account of itself — the source for half 1
from nightshift import stale_sweep  # read_status() for the fallback Staleness line
from nightshift import textio  # Digest.md is committed; write_text would CRLF it on Windows

OUT = Path("Digest.md")

# Workers whose output only a human can sign off — 03_board.md §5. A card owned
# by one of these is framed "look at this", never "done".
_VISUAL_WORKERS = frozenset({"art", "audio"})

# A card in `tasks/` that no runner has ever touched is *waiting*; one that has
# been waiting this long is *starving* — the board is being filled faster than
# the nights empty it, and no other section of this report would say so. Seven
# days is a week of nights: long enough that one busy stretch does not trip it,
# short enough that the card is still something Karel remembers writing.
_STARVING_DAYS = 7

# The oldest few, not the whole queue. A forty-line list of everything on the
# board is a section that gets skipped, which is the failure this exists to fix.
_QUEUE_SHOWN = 8

# Run blocks shown in full. More runs than this between two digests means the
# runner is being driven by hand all day, and the older ones have stopped being
# "the unattended work I came back to" — they collapse to a one-line tail.
_RUNS_SHOWN = 4

# A correction with no `[[disposition: ...]]` is a lesson nobody has acted on
# yet (12_corrections_lifecycle.md). Either signal alone is enough to nudge:
# a pile that is merely BIG should get harvested before it grows further; a
# pile that is merely OLD should get harvested before Karel forgets writing it.
# 20 is comfortably more than one session's worth of fresh corrections; 30
# days is a month of mornings this line never fired.
_HARVEST_BACKLOG_COUNT = 20
_HARVEST_BACKLOG_DAYS = 30

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LIST_ITEM = re.compile(r"^[ \t]*[-*][ \t]+(\S.*)$")

# A dated maintainer answer inside a card's `## Thread`, in the exact attributor
# shape `manage-board` mandates for recording one: `### <ISO date> · <attributor>`,
# optionally with a ` — note` tail. The whole point of that convention is that an
# answer is written into the Thread and *then* the card is re-triaged and moved out
# of needs-decision/; a parked card that already carries one is a half-done
# transition — the answer landed but the move never happened (the 2026-07-24
# `needs-decision-card-not-moved-after-answer` correction).
#
# **The token is `[board].decision_attributor`, not a literal.** It was the literal
# `karel` until 2026-08-04, which made this a permanent silent no-op in every repo
# but the origin's: the advisory ran, matched nothing, and reported a clean board.
#
# It cannot be replaced by a shape rule. Counted over the origin project's 62
# `· <token>` headings: the bare single-word attributors are `karel` (24),
# `triage` (10), `code-thread` and `claude` — so "a single bare word" reads three
# agents' own notes as a human decision. The name is doing the work.
#
# Matched narrowly on purpose beyond that: the token must be followed by a word
# boundary, so a discussion-style entry like
# `### <date> · interactive session (Karel + Claude)` does NOT trip it — that is a
# conversation, not a recorded decision, and this is advisory, so the guidance is
# to under-flag rather than over-flag. `·` is U+00B7, the separator the real
# corpus and the skill both use.
# Concatenated, never `.format()`ed: the pattern's own `{3,6}` and `{4}` are
# format placeholders to `str.format` and it raises `KeyError: '3,6'`.
_ATTRIBUTED_ANSWER_PREFIX = r"^#{3,6}[ \t]+\d{4}-\d{2}-\d{2}[ \t]*·[ \t]*"


def _plain(s: str) -> str:
    """Strip the markdown that would render as literal ** and ` in the digest,
    and neutralise angle brackets.

    A card summary that names `ai/<id>` (or any `<placeholder>`) otherwise reads
    to Obsidian as an unclosed HTML tag, and an unclosed tag swallows the
    rendering of every list item after it — one such token in the first Done
    card turned the whole rest of the list into raw source (Karel, 2026-07-28).
    `&lt;`/`&gt;` render as literal `<`/`>` and are inert."""
    s = _INLINE_CODE.sub(r"\1", _BOLD.sub(r"\1", s))
    return s.replace("<", "&lt;").replace(">", "&gt;")


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


# Abbreviations whose full stop is not a sentence end — the false positives that
# would make a summary stop at "…, i.e." mid-thought.
_ABBREV = ("i.e.", "e.g.", "vs.", "etc.", "cf.", "al.", "no.", "fig.", "eq.", "resp.")


def _clip_clean(s: str, n: int) -> str:
    """Clip to <= n, ending on a sentence boundary when a real one fits.

    A one-line summary that stops mid-clause reads as broken (Karel, 2026-07-27).
    So if the text overruns the budget but a genuine sentence terminator sits at
    least `_CLIP_FLOOR` chars in, cut at the last such terminator and drop the
    dangling partial — no ellipsis, because a full sentence is not a truncation.
    Abbreviations (`i.e.`, `e.g.`, …) are not terminators. Only when no real
    sentence ends within the budget do we word-clip with an ellipsis."""
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    ends: list[int] = []
    for m in re.finditer(r"[.!?](?=\s)", s[:n]):
        if not s[:m.end()].lower().endswith(_ABBREV):
            ends.append(m.end())
    if ends and ends[-1] >= _CLIP_FLOOR:
        return s[:ends[-1]]
    return s[:n].rsplit(" ", 1)[0] + "…"


# A sentence-boundary cut shorter than this wastes too much of the budget; below
# it, show more text with an ellipsis instead of a very short clean fragment.
_CLIP_FLOOR = 60


# Frontmatter parsing and `## heading` extraction are `board.parse_fields` /
# `board.section` now — this module used to carry its own copies of both
# (`board-parser-convergence`).
_section = board.section

# Every lane a card can be reported from. `ideas/` is excluded on principle, not
# by accident of scope: no judgment actor may read that folder (03_board.md §…),
# and the digest is read as a judgment about what to do next.
_LANES = ("inbox", "tasks", "needs-decision", "review", "testing", "done", "failed")


class Card(board.Card):
    """A `board.Card` plus the digest's own read-only judgments.

    `is_visual`, `waited` and `link` are digest-specific — nothing else needs
    to know which cards need a human's eye or how long one has been waiting —
    so they stay here rather than growing the shared model. `id`, `title`,
    `worker`, `attempts`, and the frontmatter parsing behind them, come from
    `board.Card` unchanged.
    """

    @property
    def is_visual(self) -> bool:
        return self.worker in _VISUAL_WORKERS

    def waited(self, today: _dt.date) -> int | None:
        """Days since `created:`, or `None` if the card does not say.

        Unknown rather than zero on a missing or malformed date: zero would rank
        the card as the newest thing on the board and sort it out of sight, and
        the entire point of this section is that the cards nobody looks at are
        the ones that stop being visible.
        """
        try:
            return (today - _dt.date.fromisoformat(self.fields.get("created", ""))).days
        except ValueError:
            return None

    def link(self) -> str:
        # Obsidian resolves [[stem]] by filename; keep a readable alias.
        return f"[[{self.path.stem}|{self.title}]]"


def _cards(root: Path, lane: str) -> list[Card]:
    return board.cards(root, lane, card_cls=Card)


def _index(root: Path) -> dict[str, tuple[Card, str]]:
    """`{card_id: (card, lane)}` across every readable lane.

    A run record names cards by id; the digest wants their title, their prose
    and their `[[wikilink]]`, and it must find them wherever the card has since
    been moved to — including `done/`, if Karel played a card and filed it
    before reading the digest. A card that has been deleted outright is simply
    absent, and the caller falls back to the id (`_named`).
    """
    out: dict[str, tuple[Card, str]] = {}
    for lane in _LANES:
        for card in _cards(root, lane):
            out.setdefault(card.id, (card, lane))
    return out


def _first_question_line(card: Card) -> str:
    """The question sentence: the first prose line, up to its '?'. Not the first
    bold span — a picker bolds its options and often a word mid-sentence, so
    'first bold' grabs the wrong thing."""
    for line in _section(card.text, "Question").splitlines():
        s = line.strip()
        # Skip blanks, list items and headings/quotes — but not a **bold**
        # question line, which also starts with '*'.
        if not s or _LIST_ITEM.match(line) or s[0] in "#>":
            continue
        s = _plain(s)
        end = re.search(r"[?.](\s|$)", s)  # cut at the first sentence, not mid-clip
        return _clip(s[: end.start() + 1] if end else s, 100)
    return "(see the card)"


def _bullets(lines: list[str]) -> list[str]:
    """Each top-level list item in `lines`, with its wrapped plain continuation
    lines joined in — so an option that ran onto a second source line is one
    whole string, not a fragment cut at the wrap (Karel's "random end of line",
    2026-07-28). A following *bullet* starts a new item and is never folded in,
    which keeps a card that nests bullets under an option from swallowing them."""
    out: list[str] = []
    current: str | None = None
    for line in lines:
        if _LIST_ITEM.match(line):
            if current is not None:
                out.append(current)
            current = _LIST_ITEM.match(line).group(1)
        elif current is not None:
            if line.strip():
                current += " " + line.strip()
            else:
                out.append(current)
                current = None
    if current is not None:
        out.append(current)
    return out


def _candidates(card: Card) -> list[str]:
    """Each option with its implication kept, wrapped lines joined and the clip
    landing on a clean boundary — so the digest carries the actual choice, whole,
    not a label or a fragment cut at a source line-break."""
    return [_clip_clean(_plain(b), 120)
            for b in _bullets(_section(card.text, "Question").splitlines())]


def _decision_lines(card: Card, indent: str = "    ") -> list[str]:
    """The indented markdown lines to list under a parked card.

    A multi-decision card structures its Question as bold sub-question headers
    (`**1 — …**`) with options under each; those are shown nested — each
    sub-question, then its options, wrapped lines joined and clipped cleanly. A
    single-picker card has no such headers, so its options sit at one indent."""
    lines = _section(card.text, "Question").splitlines()
    headers = [
        i for i, line in enumerate(lines)
        if (s := line.strip()).startswith("**") and not _LIST_ITEM.match(line)
    ]
    if len(headers) < 2:
        return [f"{indent}- {c}" for c in _candidates(card)]

    out: list[str] = []
    for n, start in enumerate(headers):
        end = headers[n + 1] if n + 1 < len(headers) else len(lines)
        seg = lines[start:end]
        first = next((i for i, line in enumerate(seg) if _LIST_ITEM.match(line)), len(seg))
        header = " ".join(line.strip() for line in seg[:first] if line.strip())
        out.append(f"{indent}- {_clip_clean(_plain(header), 120)}")
        for opt in _bullets(seg[first:]):
            out.append(f"{indent}    - {_clip_clean(_plain(opt), 120)}")
    return out


def _decision_attributor(root: Path) -> str:
    """`[board].decision_attributor`, or `""` when the manifest cannot be read.

    A digest that cannot find a manifest still has a board to report on, so this
    degrades to "no advisory" rather than raising — the advisory is a nudge, not a
    number anyone counts on.
    """
    try:
        from nightshift import manifest as _manifest
        return _manifest.load(root).board.decision_attributor
    except Exception:
        return ""


@functools.lru_cache(maxsize=8)
def _answer_pattern(attributor: str) -> re.Pattern[str] | None:
    """The compiled `### <date> · <attributor>` matcher, or `None` if undeclared.

    `None` disables the advisory rather than falling back to a guess: a project
    that never adopted the convention has no token to match, and inventing one
    would report a check that cannot fire (see `[board].decision_attributor`).
    """
    if not attributor.strip():
        return None
    return re.compile(_ATTRIBUTED_ANSWER_PREFIX + re.escape(attributor.strip()) + r"\b",
                      re.IGNORECASE | re.MULTILINE)


def _has_maintainer_answer(card: Card, attributor: str) -> bool:
    """True when the card's `## Thread` already carries a dated maintainer answer.

    Scoped to the `## Thread` section only, never the whole card: the
    `### Decision N — DECIDED (…)` headers a picker uses live in `## Question`,
    and those are the card *asking*, not the maintainer having answered in the
    recorded shape. A hit here means the close-out flow stalled after the answer
    went in — re-triage, `Open questions: none`, and the move never happened — so
    the Decide entry is flagged rather than silently listed as still-open.
    """
    pattern = _answer_pattern(attributor)
    if pattern is None:
        return False
    return bool(pattern.search(_section(card.text, "Thread")))


def _last_error(card: Card) -> str:
    err = _section(card.text, "Error")
    if not err:
        return ""
    # Escape too: a traceback names `<module>` / `<lambda>`, the same HTML-tag
    # hazard as the summaries (see _plain).
    return _clip(_plain(" ".join(err.split())), 200)


# --- The window: which runs has Karel not been shown yet? --------------------
#
# The runner writes a `digest: YYYY-MM-DD` commit at the end of every run, so
# that commit's timestamp is a faithful "the last time a report was put in front
# of him". Every run record started after it is unreported — normally one, and
# two on 2026-07-30 precisely because the aborted night never reached its own
# digest. Derived from the commit rather than from a marker file this module
# writes, so re-rendering is idempotent: `python -m nightshift.digest` twice must not
# make the second run claim that nothing happened.
#
# **Two commit shapes, one baseline** (`--append-digest`, 2026-07-30). A `digest:
# <date>` commit means "Karel was shown this" and the window resets there. A run
# started with `runner.py --append-digest` (the default for `night.py`'s
# unattended path — Task Scheduler over a weekend he won't be at the keyboard for)
# still writes `Digest.md` and still commits it, but under the *different* message
# `f"{APPEND_COMMIT_PREFIX}: <date>"`, which `_DIGEST_COMMIT` deliberately does not
# match. `_previous_digest`'s walk skips straight past it and keeps looking for
# the last real `digest:` commit — so the window silently keeps growing across
# any number of append runs, with no separate "unread" marker file to keep in
# sync. It closes the moment a run is invoked *without* the flag: that commit is
# a plain `digest:` line, and everything accumulated since the last one — a whole
# unattended weekend, say — lands in that one render before the baseline resets.
# The assumption baked into `night.py` defaulting to `--append-digest`: a
# scheduled run has nobody watching it, but a run Karel starts by hand is a run
# he is about to look at.

APPEND_COMMIT_PREFIX = "digest-append"
_DIGEST_COMMIT = re.compile(r"^digest: (\d{4}-\d{2}-\d{2})$")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _previous_digest(root: Path) -> tuple[str | None, str | None]:
    """`(sha, date)` of the commit at which `Digest.md` was last generated.
    `(None, None)` with no git repo here, or if no digest has ever been
    committed (this is the first one)."""
    out = _git(root, "log", "--format=%H %s")
    if out.returncode != 0:
        return None, None
    for line in out.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        m = _DIGEST_COMMIT.match(subject)
        if m:
            return sha, m.group(1)
    return None, None


def _commit_time(root: Path, sha: str) -> str | None:
    """The commit's local ISO timestamp, to compare against a record's `started`.

    `%cI` is the *committer* date, which is the one that moves when a commit is
    rebased — and the runner rebases card branches onto the integration branch
    routinely. The digest commit itself is never rebased, but using the committer
    date means a rewritten history cannot leave this reading a timestamp from
    before work that has already been reported.
    """
    out = _git(root, "log", "-1", "--format=%cI", sha)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    # `2026-07-30T13:10:51+02:00` → `2026-07-30T13:10:51`; run records are naive
    # local time, so the offset has to come off before a string comparison.
    return out.stdout.strip()[:19]


# --- Reading a card's own prose back for the human-readable summary ----------
#
# Never make Karel open a card to know what happened. `## Summary` is the
# worker's own account `settle()` writes on a green run (menu-summary-on-card);
# `## Intent` is always present and is the fallback for a card that has not run
# yet or predates that convention.

_TELEM_MIN = re.compile(r"([\d.]+)\s*min wall")
# HTML comments carry `stale-ok` reasons and forward-reference notes; they are
# markup for the gates, never prose Karel should see in a one-line summary.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _lead_sentence(text: str, n: int) -> str:
    """The first contiguous prose paragraph of `text`, cut at its first sentence
    and clipped to `n`. Skips blanks, list items and headings; returns "" when
    the section has no prose (only a bullet list, say)."""
    text = _COMMENT.sub("", text)
    prose: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if prose:
                break
            continue
        if _LIST_ITEM.match(line) or s[0] in "#>":
            if prose:
                break
            continue
        prose.append(s)
    if not prose:
        return ""
    return _clip_clean(_plain(" ".join(prose)), n)


def _account(card: Card, n: int = 240) -> str:
    """The human-readable 'what happened / what this is' line for a finished or
    proposed card: the worker's `## Summary` when it has run, else the first
    sentence of `## Intent`."""
    summary = _COMMENT.sub("", _section(card.text, "Summary"))
    if summary.strip():
        return _clip_clean(_plain(" ".join(summary.split())), n)
    # No worker summary (a card that predates the field, or has not run): fall
    # back to the Intent's first sentence at the *full* budget, so a whole
    # sentence fits and ends on its period rather than being clipped mid-clause.
    return _lead_sentence(_section(card.text, "Intent"), n)


def _wall_minutes(card: Card) -> str:
    """`57 min` from the card's `## Telemetry`, summed across attempts — or "".

    Cost and model come from the run record now (they are facts about the run,
    and the card's block accumulates every attempt from every run), but wall
    time is only ever written on the card, and "it ran for an hour" is the
    difference between a card that hit something hard and one that died in four
    turns."""
    mins = sum(float(m) for m in _TELEM_MIN.findall(_section(card.text, "Telemetry")))
    return f"{round(mins)} min" if mins else ""


def _first_bullet(sec: str) -> str:
    """The first list item in `sec`, with its wrapped continuation lines joined
    in so the sentence is whole — the same join `_candidates` does, so a clip
    lands on a word rather than on a source line-break. "" when there is none."""
    lines = _COMMENT.sub("", sec).splitlines()
    for i, line in enumerate(lines):
        m = _LIST_ITEM.match(line)
        if not m:
            continue
        parts = [m.group(1)]
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s or _LIST_ITEM.match(nxt) or s[0] in "#>":
                break
            parts.append(s)
        return " ".join(parts)
    return ""


def _approach(card: Card, n: int = 200) -> str:
    """The core of what a card will do — the *how*, to sit under the Intent's
    *what*. Karel asked for this directly (the `heavy-guard-should-bleed`
    example: "there is an `is_drone` var, we add a new `is_mechanical` var").

    Read for the stale-hunter's fix-cards, which are the one kind of card a run
    *creates*: seeing that a doc drifted is only half the news, the other half is
    what the queued fix intends to do about it.

    - A **code** card states it in `## Approach` — a mandated one-paragraph
      section triage writes for exactly this, enforced by `card_schema`.
    - An **art/audio** card's substance is what the picture *depicts*, which its
      `## Subject` states.

    The fallback chain (first `## Technical`/`## Acceptance` bullet) is for cards
    triaged before `## Approach` was mandated — best-effort. Returns "" when a
    card carries none of these."""
    if card.is_visual:
        return _lead_sentence(_section(card.text, "Subject"), n)
    explicit = _COMMENT.sub("", _section(card.text, "Approach"))
    if explicit.strip():
        return _clip_clean(_plain(" ".join(explicit.split())), n)
    for heading in ("Technical", "Acceptance"):  # legacy fallback (pre-Approach)
        bullet = _first_bullet(_section(card.text, heading))
        if bullet:
            return _clip_clean(_plain(bullet), n)
    return _lead_sentence(_section(card.text, "Technical"), n)


def _queue(root: Path, today: _dt.date) -> tuple[list[tuple[Card, int | None]], list[Card]]:
    """`tasks/` split into never-dispatched (oldest first) and already-attempted.

    Sorted oldest-waiting first, with unknown ages last and the id as the final
    tiebreak so the digest is byte-identical across two runs on the same board —
    the same determinism `board.cards()` sorts for, and for the same reason: a
    report that reshuffles itself cannot be diffed against yesterday's.
    """
    tasks = _cards(root, "tasks")
    waiting = [(c, c.waited(today)) for c in tasks if c.attempts == 0]
    waiting.sort(key=lambda pair: (pair[1] is None, -(pair[1] or 0), pair[0].id))
    return waiting, [c for c in tasks if c.attempts > 0]


# --- Rendering one run -------------------------------------------------------

# What a stop reason means for the header word. Order matters: the first match
# wins, and "the gate harness is broken" must beat the generic limit wording.
#
# `aborted` is reserved for a run that stopped because something was *wrong* —
# the distinction Karel needs at a glance, because an aborted night means the
# queue did not get worked and the reason is probably not the cards. `cut short`
# is a run that ran out of the budget it was given, which is normal. `finished`
# is a run that did what it was asked. The kill switch gets its own word: Karel
# dropped `.ai/STOP` himself, so the run not finishing is the switch working, not
# a defect — and it must not be counted among the runs that "did not finish
# cleanly" in the banner, which is a line meant to make him look.
_ABORT_MARKERS = ("failed in a row", "gate harness is broken", "transient rate limits")
_CUT_SHORT_MARKERS = ("session limit", "limit;", "window reopens", "budget", "past ")
_STOPPED_MARKERS = ("kill switch",)


def _run_status(record: dict) -> tuple[str, str]:
    """`(word, why)` for a run's header — the one-glance verdict and its reason."""
    reason = str(record.get("stop_reason") or "")
    if not record.get("complete"):
        return "killed", ("the run never reached its own end — no digest was written "
                          "for it, so it is reported here" + (f" (last: {reason})" if reason else ""))
    if not reason:
        return "finished", "worked the queue to the end"
    low = reason.lower()
    if any(m in low for m in _STOPPED_MARKERS):
        return "stopped by you", reason
    if any(m in low for m in _ABORT_MARKERS):
        return "aborted", reason
    if any(m in low for m in _CUT_SHORT_MARKERS):
        return "cut short", reason
    return "finished", reason


# A leading `path/to/file.md:145 — ` on a gate violation. Stripped before two
# failures are compared, because the same broken gate reports a different file
# for every card it fires on, and comparing the raw strings would call one
# infrastructure bug three unrelated failures.
_PATH_PREFIX = re.compile(r"^\S+?:\d+\s+—\s+")


# `suite.check_junit`'s pytest verdict: counts, then the first failing test after
# `— first: `. The counts are a fact about the *slice that ran*, not about the bug,
# and they differ between two cards that failed identically because the selector
# gave them different slices — so grouping on them is grouping on noise, and the
# clause after them is the actual cause.
_PYTEST_FIRST = re.compile(r"—\s*first:\s*(.+)$")


def _failure_signature(detail: str) -> str:
    """The root cause of a failure, normalised enough that two cards failing the
    same way produce the same string.

    Takes the first clause (violations are `;`-joined, worst first) and drops the
    file:line prefix. On the night this was written, three cards produced three
    different-looking details that all reduced to `doc_reference_liveness: symbol
    LICENSE does not exist in the source tree` — one bug, and the digest said
    nothing at all about any of them.

    A pytest verdict is the one case where the *front* of the string is the noise:
    `pytest: 2 failure(s), 0 error(s) across 1860 test(s)` is about the slice, and
    two cards that broke the same test report different totals. So when the verdict
    names a failing test, that becomes the signature — which both groups correctly
    and stops the 110-char clip from landing on `— first:…`, cutting the string off
    exactly where it started being useful.
    """
    collapsed = " ".join(str(detail).split())
    named = _PYTEST_FIRST.search(collapsed)
    if named:
        return _clip(_plain(named.group(1)), 110)
    first = re.split(r";\s*", collapsed)[0]
    return _clip(_plain(_PATH_PREFIX.sub("", first)), 110)


# How many lines of a failure's evidence the digest quotes. Smaller than the
# card's own budget on purpose: the card is where you go to work one failure, the
# digest is a morning scan of the whole night, and a digest that opens with forty
# lines of traceback is one Karel stops reading.
_EVIDENCE_LINES = 6


def _evidence_lines(failure: dict) -> list[str]:
    """The recorded failure evidence, as an indented code block under its bullet.

    This is the half of the fix that needs no card open at all: `Digest.md` is
    committed, so quoting the assertion here means the reason survives the trip to
    a machine that never ran the night — which is the case `.ai/runs/` cannot serve
    and, for a retired card, cannot serve anywhere (`runner.prune_run_dir`).

    Already indented four spaces by `suite.failure_excerpt`; four more nest it
    inside the list item, which is what makes it render as a code block rather than
    as mangled prose. Deliberately not passed through `_plain`: escaping is for
    text that renders as markdown, and doing it inside a code fence would corrupt
    the very `<module>` and `assert x == y` lines it is quoting.
    """
    block = str(failure.get("evidence") or "").strip("\n")
    if not block:
        return []
    kept = [line for line in block.splitlines() if line.strip()][:_EVIDENCE_LINES]
    return [f"    {line}" for line in kept]


def _named(card_id: str, index: dict[str, tuple[Card, str]]) -> str:
    """A card's wikilink if it is still on the board, else its bare id.

    Not an error case: a card can be deleted or archived between the run and the
    morning, and the run still happened. Reporting the id is the honest fallback.
    """
    entry = index.get(card_id)
    return entry[0].link() if entry else f"`{card_id}`"


def _run_block(root: Path, record: dict, index: dict[str, tuple[Card, str]],
               heading: str) -> list[str]:
    """One run's report — Karel's four questions, in Karel's order, then skips."""
    word, why = _run_status(record)
    L: list[str] = []
    L.append(f"## {heading} {run_record.window(record)} — {word}")
    L.append("")

    label = str(record.get("label") or "").strip()
    dispatched = record.get("dispatched") or []
    facts = [f"{len(dispatched)} dispatched"]
    if record.get("cost_usd"):
        facts.append(f"${float(record['cost_usd']):.2f}")
    if record.get("walls"):
        facts.append(f"{record['walls']} usage wall(s)")
    L.append(f"*{label or 'run'} · " + " · ".join(facts) + f" · {_plain(why)}*")
    L.append("")

    failures = run_record.failures(record)
    decisions = run_record.decisions(record)
    landed = run_record.landed(record)

    # 1. Failed — the first question of the morning, and the one the previous two
    #    digest designs could not answer at all.
    L.append(f"### Failed — {len(failures)}")
    L.append("")
    if not failures:
        L.append("*Nothing failed.*")
    else:
        signatures = {_failure_signature(f.get("detail", "")) for f in failures}
        shared = signatures.pop() if len(signatures) == 1 else ""
        if shared and len(failures) > 1:
            # One cause, N casualties. Said once, at the top, because the useful
            # unit of work here is "fix the gate", not "read three cards".
            L.append(f"**All {len(failures)} failed the same way** — {shared}")
            L.append("")
            for f in failures:
                L.append(f"- {_named(f['card'], index)} — {_plain(_retry_note(f))}")
            L.extend(_evidence_lines(failures[0]))
        else:
            for f in failures:
                L.append(f"- {_named(f['card'], index)} — {_plain(_retry_note(f))}")
                L.append(f"    - {_failure_signature(f.get('detail', ''))}")
                L.extend(_evidence_lines(f))
    L.append("")

    # 2. Decide — what the run could not settle without him. Question inline.
    L.append(f"### Needs your decision — {len(decisions)}")
    L.append("")
    if not decisions:
        L.append("*Nothing was parked for you.*")
    else:
        L.append("Answer in each card's `## Thread`, then move it out of `needs-decision/`.")
        L.append("")
        for d in decisions:
            entry = index.get(d["card"])
            L.append(f"- {_named(d['card'], index)}"
                     + (f" — {_first_question_line(entry[0])}" if entry else ""))
            if entry:
                L.extend(_decision_lines(entry[0]))
    L.append("")

    # 3. Passed — what landed, and what he does with it.
    L.append(f"### Passed — {len(landed)}")
    L.append("")
    if not landed:
        L.append("*Nothing landed.*")
    else:
        for d in landed:
            entry = index.get(d["card"])
            card, lane = entry if entry else (None, "")
            tag = _landed_tag(d, card, lane)
            bits = [b for b in (str(d.get("model") or ""),
                                f"${float(d.get('cost_usd') or 0):.2f}"
                                if d.get("cost_usd") else "",
                                _wall_minutes(card) if card else "") if b]
            L.append(f"- {_named(d['card'], index)} — **{tag}**"
                     + (f" · {' · '.join(bits)}" if bits else ""))
            if card:
                acct = _account(card)
                if acct:
                    L.append(f"    - {acct}")
    L.append("")

    # 4. Stale hunter — what it checked and what it actually found. `selected`
    #    against `verified` is the whole point: 58 selected and 0 verified is a
    #    broken sweep, and the old three-number status rendered it as silence.
    stale = record.get("stale")
    L.append("### Stale hunter")
    L.append("")
    if not stale:
        L.append("*Did not run — a run needs `--stale` on the command line "
                 "(`run-the-runner`); it does not happen on its own.*")
    elif not stale.get("selected"):
        L.append("*Ran; nothing had changed since its last verification, so there was "
                 "nothing to check.*")
    else:
        line = (f"{stale['selected']} doc(s) selected · {stale.get('checked', 0)} checked · "
                f"{stale.get('verified', 0)} verified · {stale.get('carded', 0)} carded")
        if stale.get("incomplete"):
            line += f" · **{stale['incomplete']} returned no usable verdict**"
        L.append(line)
        if stale.get("incomplete") and not stale.get("verified"):
            L.append("")
            L.append("**The sweep produced nothing.** Every verdict came back incomplete, "
                     "so no doc was ledgered and all of them will be re-checked next "
                     "time — the hours it spent bought nothing. See `.ai/runs/_stale/`.")
        for cid in stale.get("cards") or []:
            entry = index.get(cid)
            L.append("")
            L.append(f"- {_named(cid, index)}")
            if entry:
                acct = _account(entry[0], 200)
                if acct:
                    L.append(f"    - {acct}")
                how = _approach(entry[0], 200)
                if how and how != acct:
                    L.append(f"    - → {how}")
    L.append("")

    # 5. Skipped — last, because it is the one section that is usually the same
    #    every night. Grouped by reason: five art cards blocked on a capability
    #    this host lacks is one fact about the host, not five about the cards.
    skipped = record.get("skipped") or []
    L.append(f"### Skipped — {len(skipped)}")
    L.append("")
    if not skipped:
        L.append("*Every card on the board was dispatchable.*")
    else:
        groups: dict[str, list[str]] = {}
        for s in skipped:
            groups.setdefault(str(s.get("reason", "(no reason recorded)")), []).append(s["card"])
        for reason in sorted(groups):
            ids = ", ".join(f"`{cid}`" for cid in sorted(groups[reason]))
            L.append(f"- **{len(groups[reason])}** — {_plain(reason)}")
            L.append(f"    - {ids}")
    L.append("")
    return L


def _retry_note(failure: dict) -> str:
    """What the failure *means* for the card, from `settle`'s own account.

    `outcome: failed` alone does not say whether the card is back in the queue
    for another attempt or has reached `failed/` and is dead until Karel looks —
    which is the difference between "ignore this" and "this needs you".
    """
    landed = str(failure.get("landed") or "")
    if "→ failed/" in landed:
        return f"**out of attempts — in `failed/`** (attempt {failure.get('attempt', '?')})"
    return f"attempt {failure.get('attempt', '?')} failed, back in the queue"


def _landed_tag(dispatch: dict, card: Card | None, lane: str) -> str:
    """What Karel does with a landed card: eyeball art/audio ('look'), play a
    merged code card at the keyboard ('play'), or read a diff he cannot play
    ('review').

    **The card declares this now** (`verify:`), so the label stops being a guess.
    It used to be inferred from the lane, which could only ever repeat the lane's
    own assumption back at him: everything in `testing/` read as 'play', including
    the eleven of sixteen cards there with no player-visible surface at all.
    `board.Card.verify` carries the absent-means-play default, so a card written
    before the field existed reads exactly as it did before.
    """
    if card is not None and card.is_visual:
        return "look"
    if card is not None:
        return "play" if card.verify == "play" else "review"
    # No card to read — it was archived, or vanished mid-run. Fall back to the
    # lane, then to what the run itself observed.
    if lane in ("testing", "review"):
        return "play" if lane == "testing" else "review"
    return "play" if dispatch.get("outcome") == "reviewed" else "review"


def render(root: Path) -> str:
    day = _dt.date.today()
    today = day.isoformat()

    # Empty when the project never declared one, which disables the answered-but-
    # not-moved advisory rather than guessing a token that would match nothing.
    attributor = _decision_attributor(root)

    prev_sha, prev_date = _previous_digest(root)
    since = _commit_time(root, prev_sha) if prev_sha else None
    records = run_record.records_since(root, since)
    index = _index(root)

    waiting, retried = _queue(root, day)
    starving = [(c, age) for c, age in waiting if age is not None and age >= _STARVING_DAYS]
    decide = _cards(root, "needs-decision")
    testing = _cards(root, "testing")
    review = _cards(root, "review")
    failed = _cards(root, "failed")
    tasks_all = [c for c, _ in waiting] + retried

    # `unattended: false` is read as a string, not a bool — board.Card keeps
    # frontmatter values as written, and the runner's own check compares text.
    stale_unattended = sorted(
        (c for c in tasks_all
         if str(c.fields.get("unattended", "")).strip().lower().startswith("false")
         and _has_maintainer_answer(c, attributor)),
        key=lambda c: c.id,
    )

    # Cards this window's runs parked. They are reported inside their run block,
    # so the standing Decide section lists only what was already open — no card
    # appears twice, and "new" needs no marker because the two are separate
    # sections with different headings.
    parked_now = {d["card"] for r in records for d in run_record.decisions(r)}

    backlog_count, backlog_since = corrections.backlog(root)
    backlog_age = None
    if backlog_since:
        try:
            backlog_age = (day - _dt.date.fromisoformat(backlog_since)).days
        except ValueError:
            backlog_age = None
    harvest_due = (backlog_count >= _HARVEST_BACKLOG_COUNT
                   or (backlog_age is not None and backlog_age >= _HARVEST_BACKLOG_DAYS))

    counts = {lane: len(_cards(root, lane)) for lane in _LANES}

    n_failed = sum(len(run_record.failures(r)) for r in records)
    n_landed = sum(len(run_record.landed(r)) for r in records)
    n_parked = sum(len(run_record.decisions(r)) for r in records)
    n_bad = sum(1 for r in records if _run_status(r)[0] in ("aborted", "killed"))
    spent = sum(float(r.get("cost_usd") or 0) for r in records)

    L: list[str] = []
    L.append(f"# Digest — {today}")
    L.append("")

    # The banner — what happened, in one glance, before any detail.
    if not records:
        if since is None:
            L.append("> **First digest** — no prior run on this machine to compare against.")
        else:
            L.append(f"> **No run has recorded itself since the {prev_date} digest.** "
                     f"Either nothing ran, or the runs that did predate "
                     f"`.ai/runs/records/` — older runs are only in `.ai/runs/*.log`.")
    else:
        headline = (f"{len(records)} run(s) since the {prev_date} digest"
                    if prev_date else f"{len(records)} recorded run(s)")
        bits = [f"{n_landed} landed", f"{n_failed} failed", f"{n_parked} for you to decide"]
        if n_bad:
            bits.append(f"**{n_bad} did not finish cleanly**")
        if spent:
            bits.append(f"${spent:.2f}")
        L.append(f"> **{headline}:** " + " · ".join(bits) + ".")
    L.append("")
    L.append("*Generated by `python -m nightshift.digest`, overwritten every run. "
             "Answers go in a card's `## Thread`, never here.*")
    L.append("")

    # --- Half 1: what happened, newest run first -----------------------------
    if records:
        for n, record in enumerate(records[:_RUNS_SHOWN]):
            heading = "Last run" if n == 0 else "Earlier run"
            stamp = run_record.day(record)
            if stamp != today:
                heading = f"{heading} {stamp}"
            L.extend(_run_block(root, record, index, heading))
        if len(records) > _RUNS_SHOWN:
            older = records[_RUNS_SHOWN:]
            L.append(f"## {len(older)} older run(s) since the last digest")
            L.append("")
            for record in older:
                word, _ = _run_status(record)
                L.append(f"- {run_record.day(record)} {run_record.window(record)} — {word} · "
                         f"{len(run_record.landed(record))} landed · "
                         f"{len(run_record.failures(record))} failed")
            L.append("")
    else:
        # No records, but the sweep's committed status file may still know
        # something — the one piece of a pre-record run that survives in git.
        status = stale_sweep.read_status(root)
        if status is not None:
            L.append("## Last recorded staleness sweep")
            L.append("")
            L.append(f"Last swept {status.get('last_run', '?')}: "
                     f"{status.get('checked', 0)} doc(s) checked, "
                     f"{status.get('verified', 0)} verified, "
                     f"{status.get('carded', 0)} finding(s) carded to `tasks/`.")
            L.append("")

    # --- Half 2: standing board state, terse and labelled as carry-over ------
    L.append("## Still waiting on you")
    L.append("")
    L.append("*Carry-over from earlier runs — the board's own lanes are the full list.*")
    L.append("")

    older_decide = [c for c in decide if c.id not in parked_now]
    L.append(f"**Decide — {len(older_decide)} still open"
             + ("** · these block tonight" if older_decide else "**"))
    if older_decide:
        L.append("")
        for c in older_decide:
            L.append(f"- {c.link()} — {_first_question_line(c)}")
            # Advisory nudge: a parked card that already carries a dated Karel
            # answer in its `## Thread` is a stalled close-out — the answer
            # landed but the card never moved out of needs-decision/.
            if _has_maintainer_answer(c, attributor):
                L.append("    - **⚠ answered in `## Thread` — should this have "
                         "moved? Re-check `Open questions`, then move it.**")
            # The one place in the standing half that keeps its detail inline.
            # Everything else here is a count, because Karel is looking at the
            # same Kanban lanes anyway — but a decision is the thing he *answers*
            # from the digest, and an answer needs the options in front of him.
            L.extend(_decision_lines(c))
    L.append("")

    if failed:
        L.append(f"**In `failed/` — {len(failed)}** · out of attempts, dead until you look")
        L.append("")
        for c in failed:
            err = _last_error(c)
            L.append(f"- {c.link()}" + (f" — {err}" if err else " — *(no error recorded)*"))
        L.append("")

    # A count, not a list. Six cards with their summaries and costs is what the
    # previous design put here, and it duplicated a Kanban lane Karel is already
    # looking at while pushing the actual news off the top of the report.
    if testing or review:
        parts = []
        if testing:
            parts.append(f"{len(testing)} in `testing/` to play")
        if review:
            parts.append(f"{len(review)} in `review/` to read")
        L.append("**" + " · ".join(parts) + "** — each carries its own `## Summary`; "
                 "the board's lanes are the list.")
        L.append("")

    L.append(f"**Queue — {counts.get('tasks', 0)} in `tasks/`**"
             + (f", {len(waiting)} never dispatched" if waiting else ""))
    L.append("")
    if not waiting and not retried:
        L.append("*Empty — nothing waiting to be dispatched.*")
        L.append("")
    else:
        if starving:
            card, age = starving[0]
            L.append(f"- **{len(starving)} card(s) have waited {_STARVING_DAYS}+ days** — "
                     f"oldest created {card.fields.get('created')} ({age} days ago). "
                     f"The board is filling faster than the nights empty it; reorder the "
                     f"column, or the bottom of the queue never runs.")
        for card, age in waiting[:_QUEUE_SHOWN]:
            waited = f"waiting {age} day{'s' if age != 1 else ''}" if age is not None \
                else "no `created:` date"
            L.append(f"- {card.link()} — {waited}")
        if len(waiting) > _QUEUE_SHOWN:
            L.append(f"- *…and {len(waiting) - _QUEUE_SHOWN} more.*")
        if retried:
            L.append(f"- *{len(retried)} card(s) have been attempted and are back in the "
                     f"queue — see each card's `## Error`.*")
        L.append("")

    # The two advisories that explain why work is NOT happening. Last, because
    # neither is about last night, but present because nothing else says them.
    #
    # `unattended: false` on a card whose question has been answered: the night
    # skips it silently and for free, every single night, with nothing anywhere
    # saying why. Found 2026-07-30, when all three such cards turned out to have
    # been answered on 2026-07-27 — Karel: "This flag keeps causing a lot of
    # false positives. Maybe time to get rid of it?" The flag earns its keep (it
    # is the difference between "work this at 3 AM" and "do not"); what was
    # missing is anything that notices the answer has landed. Advisory, not
    # automatic: a dated answer is strong evidence the question closed, not proof.
    if stale_unattended:
        L.append(f"**Flag may be stale:** {len(stale_unattended)} card(s) in `tasks/` are "
                 f"`unattended: false` but already carry a dated answer from you in "
                 f"`## Thread` — so every run skips them for a question that looks "
                 f"answered: " + ", ".join(f"`{c.id}`" for c in stale_unattended) +
                 ". Clear the flag to let them run, or say what is still open.")
        L.append("")
    if harvest_due:
        age_clause = (f", oldest from {backlog_since} ({backlog_age} days ago)"
                      if backlog_age is not None else "")
        L.append(f"**Harvest backlog:** {backlog_count} correction(s) in `.ai/corrections.log` "
                 f"carry no disposition{age_clause} — consider a learn-now pass "
                 f"(`python -m nightshift.corrections`).")
        L.append("")

    L.append("**Board:** " + " · ".join(f"{n} {lane}" for lane, n in counts.items() if n))

    return "\n".join(L).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    """Render the digest for the project the working directory is in.

    **Not `Path(__file__).parent.parent`.** Installed as a package that answers
    with the framework's own checkout, so `python -m nightshift.digest` run from a
    consuming project wrote `Digest.md` into `nightshift/` and rendered a board
    that was not there — silently, because an absent board is an empty one. Same
    correction as `reconcile` and `stale_sweep`; `manifest.find_root`'s docstring
    is the standing warning.

    `argv` is parsed rather than ignored for the reason the bug above was found:
    `--help` used to be accepted as "no arguments" and *wrote the file*.
    """
    import argparse
    import sys

    from nightshift.manifest import find_root

    parser = argparse.ArgumentParser(
        prog="python -m nightshift.digest",
        description="Write Digest.md for this project — a deterministic report on "
                    "the unattended work. Overwritten on every run; answers belong "
                    "in a card's `## Thread`, never here.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to report on (default: found from the working directory)")
    parser.add_argument("--stdout", action="store_true",
                        help="print the digest and write nothing")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = (args.root or find_root()).resolve()
    text = render(root)
    if not args.stdout:
        textio.write_text_lf(root / OUT, text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
