"""Reading a card's `## Question`, and writing the maintainer's answer back.

**Why a module rather than a form.** Answering a parked card was, until 2026-08-18,
something you did in an editor: open `needs-decision/<id>.md`, find `## Thread`, type a
dated heading in the exact shape the digest matches, and remember the two conventions
that make it count. Nothing enforced any of that, and the usual case is picking between
two options somebody has already enumerated for you — so the cost of answering was
wildly out of proportion to the decision, and the board silted up with cards whose
question had in fact been settled in Karel's head weeks earlier.

**No LLM anywhere in here**, the same rule the gates, the digest and `run_record`
follow. Every function is either a regex over a card's own prose or a string written
back into it. That matters more here than elsewhere: this is the one place the
*maintainer's own words* enter the board, and a summariser in that path would be
paraphrasing the only input on the whole board that must survive verbatim. The panel
offers a "Chat about it" button for when the question needs a conversation instead; that
is a different verb, and it deliberately does not write anything.

**Parsing is shared with the digest on purpose.** `digest` already had to know what a
picker looks like, to list a parked card's options in the morning report. Two parsers
would drift, and the drift would be invisible — the digest would offer one set of
options and the panel another, for the same card. So the parsing lives here and
`digest` calls it, keeping only its own clipping on top.

That sharing immediately paid for itself: the digest's list-item pattern matched only
`-`/`*` bullets, so a card whose options were written `1.` / `2.` parsed to *nothing*.
`command-center-back-buttons` — a real card, parked with two numbered options — reported
zero candidates, and the fallback then mistook the two bold spans in its prose
(`**What I found:**`, `**This needs a decision…**`) for the sub-questions and listed
those instead. The morning report had been quietly describing that card wrongly; a
picker built on the same parser would have offered two non-options and neither real one.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board, manifest, textio

#: Both list forms. Ordered items are not a stylistic variant to be tolerated — they
#: are what a worker writes when it parks a card having enumerated its options, which
#: is the single most common way a `## Question` comes into existence.
_LIST_ITEM = re.compile(r"^[ \t]*(?:[-*]|\d+[.)])[ \t]+(\S.*)$")

#: A bold run *opening* a line, which is how a multi-decision card heads each of its
#: sub-questions. Deliberately not `\*\*(.+?)\*\*` on one line: a real sub-question is
#: often a whole sentence and wraps, so the closing `**` lands on the next source line —
#: every one of `audio-generation-pipeline`'s three decisions is written that way, and a
#: single-line pattern found none of them.
_BOLD_LEAD = re.compile(r"^\*\*\S")

#: `*(recommended)*`, `(recommended)`, `**recommended**` — the marker triage puts on the
#: option it would take. Stripped from the option text so the recorded answer is the
#: choice rather than the annotation about it.
#:
#: **Tightly bounded, and the loose version was wrong.** A character class of "any
#: asterisks, underscores or brackets around the word" reads
#: `**B — split it** *(recommended)*` and eats the label's *closing* `**` on its way to
#: the parenthetical, leaving unbalanced markdown that renders as literal asterisks in
#: the form and in the recorded answer. Each accepted spelling is therefore written out.
_RECOMMENDED = re.compile(
    r"[ \t]*(?:\*{1,2}|_{1,2})?\([ \t]*recommended[ \t]*\)(?:\*{1,2}|_{1,2})?"
    r"|[ \t]*(?:\*{1,2}|_{1,2})recommended(?:\*{1,2}|_{1,2})",
    re.IGNORECASE)

#: The heading a recorded answer goes under. `## Thread` is where `manage-board` says
#: answers live and where `digest._has_maintainer_answer` looks for them; writing
#: anywhere else would be an answer no report can see.
THREAD = "Thread"

#: Sections a new `## Thread` must be inserted *before* if they exist, so the card keeps
#: the order the corpus uses. `## Telemetry` is appended by the runner after the fact and
#: is always last.
_AFTER_THREAD = ("Telemetry",)


@dataclass
class Option:
    """One enumerated choice.

    Two forms, because two callers want different things and collapsing them loses
    one of them. `text` has the `*(recommended)*` annotation removed: it is what the
    form offers and what gets recorded as the answer, and "recommended" is triage's
    opinion about the choice rather than part of the choice — a Thread entry reading
    *"> B — split it *(recommended)*"* records the advice, not the decision.

    `raw` is the option exactly as the card wrote it, which is what the digest prints:
    the morning report is quoting the card, and the mark saying which one triage would
    take is precisely the most useful thing on the line.
    """

    text: str
    recommended: bool = False
    raw: str = ""


@dataclass
class SubQuestion:
    """One thing being asked, and the options offered for it.

    `prompt` is empty for a card that asks one thing with a bare list of options —
    the common shape — in which case the question sentence is the card's prose above
    the list and there is nothing to repeat here.
    """

    prompt: str = ""
    options: list[Option] = field(default_factory=list)


def parse(card_text: str) -> list[SubQuestion]:
    """The pickable structure of a card's `## Question`, or `[]` if it has none.

    **Degrades rather than failing.** A card whose question is a paragraph of prose,
    or one with no `## Question` at all, returns `[]` — and every caller must treat
    that as "offer a free-text box", never as "this card cannot be answered". Most
    parked cards are picker-shaped because triage writes them that way, but the ones
    that are not are exactly the hard ones, and refusing to let the maintainer answer
    the hard ones would invert the point of the feature.
    """
    lines = board.section(card_text, "Question").splitlines()
    if not lines:
        return []

    # A bold lead-in only heads a sub-question if options actually follow it. Without
    # that test, prose that merely *starts* with a bold span — `**What I found:** the
    # code is in the other repo…` — is read as a question being asked, which is how
    # the digest came to present two pieces of narration as the choices on offer.
    heads = [i for i, line in enumerate(lines)
             if _BOLD_LEAD.match(line.strip()) and not _LIST_ITEM.match(line)]
    heads = [i for i in heads if _has_items_before_next(lines, i, heads)]
    heads = [i for i in heads if not _inside_option(lines, i)]

    if len(heads) < 2:
        options = _options(lines)
        return [SubQuestion(options=options)] if options else []

    out: list[SubQuestion] = []
    for n, start in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(lines)
        segment = lines[start:end]
        first_item = next((i for i, line in enumerate(segment) if _LIST_ITEM.match(line)),
                          len(segment))
        prompt = " ".join(line.strip() for line in segment[:first_item] if line.strip())
        out.append(SubQuestion(prompt=_unbold(_plain(prompt)),
                               options=_options(segment[first_item:])))
    return out


def _has_items_before_next(lines: list[str], start: int, heads: list[int]) -> bool:
    """Whether a bold lead-in at `start` has list items under it before the next one."""
    later = [i for i in heads if i > start]
    end = later[0] if later else len(lines)
    return any(_LIST_ITEM.match(line) for line in lines[start + 1:end])


def _inside_option(lines: list[str], index: int) -> bool:
    """Whether the line at `index` is a wrapped continuation of a list item.

    An option that wraps onto a line beginning with its own bold run — common, since
    options are usually written `- **B — split it** — because …` and the continuation
    can start with another bold span — would otherwise be read as a new sub-question
    heading, splitting one picker into several and orphaning the options after it.
    A blank line ends an item, so the test is simply "was the last non-blank line part
    of a list item".
    """
    for line in reversed(lines[:index]):
        if not line.strip():
            return False                       # a blank line ended whatever came before
        if _LIST_ITEM.match(line):
            return True                        # directly continuing a list item
        if line[:1].isspace():
            continue                           # indented wrap; keep looking back
        return False                           # flush prose: not inside an option
    return False


def _options(lines: list[str]) -> list[Option]:
    """Each top-level list item as an `Option`, wrapped lines folded in.

    The folding is lifted from `digest._bullets` and is not incidental: an option that
    ran onto a second source line would otherwise be truncated at the wrap — Karel's
    "random end of line", 2026-07-28. A following list item always starts a new option
    and is never folded in, so a card that nests detail under an option keeps it.
    """
    out: list[Option] = []
    current: str | None = None
    for line in lines:
        matched = _LIST_ITEM.match(line)
        if matched:
            if current is not None:
                out.append(_option(current))
            current = matched.group(1)
        elif current is not None:
            if line.strip():
                current += " " + line.strip()
            else:
                out.append(_option(current))
                current = None
    if current is not None:
        out.append(_option(current))
    return out


def _option(text: str) -> Option:
    recommended = bool(_RECOMMENDED.search(text))
    cleaned = _RECOMMENDED.sub(" ", text) if recommended else text
    return Option(text=_plain(cleaned), recommended=recommended, raw=_plain(text))


def _unbold(text: str) -> str:
    """Drop the `**` a sub-question heading is wrapped in.

    The markers said "this is the heading" in the source; once parsed, that is what the
    `SubQuestion` *is*. Leaving them in would have `compose` write `****heading****`
    into the answer and the form render literal asterisks.
    """
    text = text.strip()
    if text.startswith("**"):
        text = text[2:]
    if text.endswith("**"):
        text = text[:-2]
    return text.strip()


def _plain(text: str) -> str:
    """Collapse whitespace and drop a trailing separator. Markdown is left alone —
    the option is recorded verbatim, and `**bold**` in the card is bold in the answer."""
    return " ".join(text.split()).strip(" -—:")


# --- writing the answer ------------------------------------------------------


class DecideError(RuntimeError):
    """A refusal to write an answer — always with a reason a person can act on."""


def attributor(root: Path) -> str:
    """The token the maintainer signs a decision with, from the manifest.

    Not guessed, and an empty value is a refusal rather than a fallback. The digest
    matches this literally against `### <date> · <token>` to spot a card that was
    answered but never moved; a wrong guess makes that check silently never fire, and
    a check that cannot fire while reporting a clean board is worse than no check at
    all (`manifest.Board.decision_attributor`).
    """
    try:
        return manifest.load(root).board.decision_attributor.strip()
    except Exception:
        return ""


def compose(subquestions: list[SubQuestion], picks: list[str], note: str,
            *, who: str, today: dt.date | None = None) -> str:
    """The `## Thread` entry for one answer, in the shape the corpus already uses.

    `picks[i]` is the chosen option text for `subquestions[i]`, or `""` for one left
    unanswered — a partial answer is a real thing (a card can batch three decisions and
    the maintainer may only be sure of two), and recording only what was decided is
    better than inventing the rest.

    **The option text is copied, never summarised.** `manage-board` is explicit about
    why: *"a lossy paraphrase is how 'pick 3 EP' becomes 'around 3, tune to taste'"*.
    The worker reads this at 3 AM with no way to ask what was meant.

    The free-text note is blockquoted, matching how Karel's own words are recorded in
    the existing corpus (`bleed-regen-delayed-tick`, `## Thread`) — the quote is what
    marks a line as *his*, rather than an agent's account of him.
    """
    stamp = (today or dt.date.today()).isoformat()
    out = [f"### {stamp} · {who}", ""]
    for index, sub in enumerate(subquestions):
        chosen = picks[index] if index < len(picks) else ""
        if not chosen:
            continue
        if sub.prompt:
            out += [f"**{sub.prompt}**", ""]
        out += [f"> {chosen}", ""]
    if note.strip():
        out += [f"> {line}" if line.strip() else ">"
                for line in note.strip().splitlines()]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_answer(root: Path, card_id: str, picks: list[str], note: str,
                 *, today: dt.date | None = None) -> str:
    """Record an answer in `card_id`'s `## Thread`. Returns a one-line account.

    **The card is not moved, and that is the whole policy.** An answer is not a ticket
    to `tasks/`: getting one can *open* a question that could not be asked until now,
    or reshape the card enough to need re-triage. `manage-board` calls this
    park-over-promote and gives the asymmetry — a card wrongly promoted is picked up by
    a worker and produces confident wrong work that costs more to review than to redo,
    while a card wrongly left parked simply waits one more cycle. The panel offers the
    next step as a separate, deliberate click, and the digest's answered-but-not-moved
    nudge is what stops an answered card being forgotten.
    """
    who = attributor(root)
    if not who:
        raise DecideError(
            "no `[board].decision_attributor` in the manifest — the digest matches that "
            "token literally to tell your answer from an agent's note, so an answer "
            "written without one would not be recognised as yours. Declare it and retry.")
    card = board.find(root, card_id)
    if card is None:
        raise DecideError(f"no card `{card_id}` on the board")
    subquestions = parse(card.text)
    if not any(picks) and not note.strip():
        raise DecideError("nothing to record — pick an option or write something")

    entry = compose(subquestions, picks, note, who=who, today=today)
    text = _append_to_thread(card.text, entry)
    # `write_bytes`, never `write_text`: on Windows the latter rewrites the file as
    # CRLF, which reddens the `line_endings` gate and leaves the card with uncommitted
    # changes that `normalize_worktree` then refuses to help with.
    textio.write_text_lf(card.path, text)
    answered = sum(1 for p in picks if p)
    return (f"recorded in {card_id} · {answered} answer(s) "
            f"{'plus a note ' if note.strip() else ''}→ ## Thread, card left in "
            f"{card.lane}/")


def _append_to_thread(text: str, entry: str) -> str:
    """Put `entry` at the end of `## Thread`, creating the section if it is absent.

    Appended rather than prepended: the Thread is read top-to-bottom as the card's
    history, and the existing corpus has the original question at the top with dated
    answers accumulating under it.
    """
    found = re.search(r"^##[ \t]+Thread[ \t]*$", text, re.MULTILINE | re.IGNORECASE)
    if found:
        after = re.search(r"^##[ \t]+\S", text[found.end():], re.MULTILINE)
        cut = found.end() + (after.start() if after else len(text) - found.end())
        head, tail = text[:cut], text[cut:]
        return head.rstrip() + "\n\n" + entry + ("\n" + tail.lstrip("\n") if tail.strip() else "\n")

    block = f"## {THREAD}\n\n{entry}"
    for heading in _AFTER_THREAD:
        spot = re.search(rf"^##[ \t]+{re.escape(heading)}[ \t]*$", text,
                         re.MULTILINE | re.IGNORECASE)
        if spot:
            return text[:spot.start()].rstrip() + "\n\n" + block + "\n" + text[spot.start():]
    return text.rstrip() + "\n\n" + block


def open_questions_settled(card_text: str) -> bool:
    """Whether `## Open questions` says `none`.

    The precondition on offering "Send to tasks": `card_schema` refuses any card in
    `tasks/` whose open questions are not `none`, so a button that moved the card
    without this would simply turn the board red on the next gate run.
    """
    body = board.section(card_text, "Open questions")
    return body.strip().lower().rstrip(".") in ("none", "- none", "* none")
