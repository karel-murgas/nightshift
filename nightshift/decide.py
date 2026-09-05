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

#: Boundary line `reopen` appends to `## Thread` every time a card is (re)parked.
#: `has_maintainer_answer` only searches after the *last* one — see `reopen`'s
#: docstring for the bug this closes (a stale answer to a previous round's question
#: being read as an answer to the one just parked).
_REOPENED_MARKER = "<!-- decide: reopened -->"

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


def close_parked(root: Path, card_id: str, picks: list[str], note: str,
                 *, today: dt.date | None = None) -> str:
    """Record an answer, if there is one, and flip `state:` to `done`.

    **Sets the field, does not move the file.** Same split as `api/tasks`'s handling
    of `write_answer`: this function is a pure text edit, and the caller runs
    `reconcile --apply` to make the folder catch up — reconcile is the one thing that
    moves a card, everywhere on the board, and duplicating that here would be a second
    place a move could go wrong.

    **Why this doesn't inherit `write_answer`'s park-over-promote policy.** That
    policy is about `tasks/`: promoting there hands the answer to a worker who will
    write code from it, so a wrong promotion produces confident wrong work that costs
    more to undo than the card waiting one more cycle. Closing a card as
    not-applicable / already-satisfied carries no such risk — there is no code to get
    wrong, only a card to stop tracking, and a wrong close is undone by moving the
    file back. So the two steps `write_answer` deliberately keeps separate (record,
    then a further click to move) are one click here, because the answer *is* "this
    is finished."

    Restricted to `needs-decision/`: a card anywhere else has its own move path
    already (`tasks/` dispatch, the runner, `Send to tasks`), and this button only
    ever appears on the decide page.
    """
    card = board.find(root, card_id)
    if card is None:
        raise DecideError(f"no card `{card_id}` on the board")
    if card.lane != "needs-decision":
        raise DecideError(f"`{card_id}` is in {card.lane}/, not needs-decision/ — "
                          f"nothing for this button to close")
    if any(picks) or note.strip():
        write_answer(root, card_id, picks, note, today=today)
        card = board.find(root, card_id)
    text = re.sub(r"^state:.*$", "state: done", card.text, count=1, flags=re.MULTILINE)
    textio.write_text_lf(card.path, text)
    return f"{card_id} closed → done/"


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
    """Whether `## Open questions` says `none` — `board`'s rule, not a second one.

    The precondition on offering "Send to tasks": `card_schema` refuses any card in
    `tasks/` whose open questions are not `none`, so a button that moved the card
    without this would simply turn the board red on the next gate run. Kept as a
    name here because the panel and the digest both reach for it through this
    module, but the rule itself is `board.open_questions_settled` — see its
    docstring for the drift this delegation ended.
    """
    return board.open_questions_settled(card_text)


#: A dated maintainer answer's heading, in the shape `compose` writes it above:
#: `### <ISO date> · <attributor>`, optionally with a ` — note` tail. Concatenated
#: with the escaped token rather than `.format()`ed — the `{3,6}` and `{4}` here are
#: format placeholders and `str.format` raises `KeyError: '3,6'` on them.
_ATTRIBUTED_ANSWER_PREFIX = r"^#{3,6}[ \t]+\d{4}-\d{2}-\d{2}[ \t]*·[ \t]*"


def answer_pattern(attributor: str) -> re.Pattern[str] | None:
    """The regex that recognises `attributor`'s own answer, or `None` for no token.

    `None` disables every check built on it rather than falling back to a guess: a
    project that never declared `[board].decision_attributor` has no token to match,
    and inventing one would report a check that cannot fire.

    Matched narrowly beyond the date shape: the token must end on a word boundary,
    so a discussion-style entry like `### <date> · interactive session (Karel +
    Claude)` does not trip it — that is a conversation, not a recorded decision, and
    everything reading this is advisory, so it under-flags by design. Nor can the
    token be replaced by a shape rule: over the origin project's 62 `· <token>`
    headings the bare single-word attributors are `karel` (24), `triage` (10),
    `code-thread` and `claude`, so "a single bare word" would read three agents'
    notes as a human decision. The name is doing the work.
    """
    if not attributor.strip():
        return None
    return re.compile(_ATTRIBUTED_ANSWER_PREFIX + re.escape(attributor.strip()) + r"\b",
                      re.IGNORECASE | re.MULTILINE)


def has_maintainer_answer(card_text: str, attributor: str) -> bool:
    """Whether `## Thread` already carries a dated answer signed by `attributor`,
    answering the question currently parked rather than a previous round's.

    Scoped to `## Thread` only, never the whole card: the `### Decision N — DECIDED
    (…)` headers a picker uses live in `## Question`, and those are the card
    *asking*, not the maintainer having answered in the recorded shape.

    **Scoped to after the last `reopen` marker, when there is one.** A card parked
    a second time still carries the first round's dated answer as Thread history,
    and without this a re-parked card reads as already answered the instant it is
    parked — `_decide_state` then tells you "nothing here is waiting on you" while a
    brand-new, unanswered `## Question` sits right above (found on
    `enemy-position-knowledge`, 2026-09-05: attempt 2 parked a new question, and the
    panel reported the card fully answered on the strength of attempt 1's already-
    settled one). A card with no marker — everything before `reopen` existed —
    searches the whole section exactly as before, so this is backward compatible
    with the existing corpus.

    Two readers use it, for the same fact at different distances. The digest flags an
    answered-but-never-moved card in tomorrow's report (the 2026-07-24
    `needs-decision-card-not-moved-after-answer` correction). The panel's decide page
    says it on the page itself, so an answer that already landed is visible while you
    are looking at the card rather than a day later. It lives here, beside `compose`,
    because the writer of a shape should own recognising it — the digest had the only
    copy until 2026-08-23 and the panel could not reach it without importing the
    reporting layer.
    """
    pattern = answer_pattern(attributor)
    if pattern is None:
        return False
    thread = board.section(card_text, "Thread")
    marker_at = thread.rfind(_REOPENED_MARKER)
    if marker_at != -1:
        thread = thread[marker_at + len(_REOPENED_MARKER):]
    return bool(pattern.search(thread))


def reopen(card_text: str) -> str:
    """Mark a (re)parked card's decision state live again — the counterpart to
    `promote_to_tasks`'s `settle_open_questions`, which nothing previously called
    in the other direction.

    A worker's park writes `## Question` and `after_answer:` itself (`runner._park`),
    but two things only this closes:

    * `## Open questions` is populated at triage or settled by `promote_to_tasks`,
      and is otherwise never touched — so an `after_answer: tasks` card, scoped and
      settled (`none`) the moment it was created, parks its first ever question
      without this section ever leaving `none`. That makes `_decide_state`'s
      dedicated `after_answer: tasks` banner (`not settled` — "waiting on your
      answer" / "answered · ready to dispatch") unreachable, and every such card
      falls through to a banner built for a different case ("no open question...
      reporting, not asking").
    * `## Thread` keeps a re-parked card's prior answer as history, which is what it
      should do — but `has_maintainer_answer` needs a boundary to tell that history
      apart from an answer to the question just parked. Appending `_REOPENED_MARKER`
      here is that boundary; see its use there.

    Idempotent to call on every park, including the first: reopening an already-live
    `## Open questions` is a no-op re-render of the same text, and a second marker in
    `## Thread` only moves where `has_maintainer_answer` starts looking, never what
    it can see.
    """
    text = board.append_section(card_text, "Open questions",
                                "live — see `## Question` above.")
    return _append_to_thread(text, _REOPENED_MARKER)


def promote_to_tasks(root: Path, card_id: str, *, today: dt.date | None = None) -> str:
    """Send a parked card to `tasks/`, settling its questions if that is its route.

    The `state:` flip only — the caller runs `reconcile --apply` to move the file,
    same split as `close_parked`, because reconcile is the one thing on the board that
    moves a card.

    **Three ways in, and the middle one is the point of `after_answer:`.**

    * `## Open questions` already reads `none`: nothing to settle, flip and go. This
      is every card parked by a worker on a report rather than a question.
    * `after_answer: tasks` with the answer on record: the card declared that an
      answer makes it dispatchable, and the answer is there — so the promotion settles
      `## Open questions` (`board.settle_open_questions`, which keeps the old text as
      history) and flips. Without this the card is stuck saying "dispatch me" at a
      button that refuses, and clearing the section is a manual step nothing owns.
    * anything else: refused, with the reason naming the route the card declared.

    **A route is not a lock.** `after_answer: triage` with the questions already
    settled is *allowed* through — the field records what the parker expected, and the
    maintainer looking at the answer may reasonably conclude the card is dispatchable
    after all. What the field must never do is refuse a move a person deliberately
    chose while looking at more information than the parker had.

    **A chore that parked has already spent its one attempt, and this does not give
    it back.** `chores.py` charges the attempt the moment a chore bounces to
    `needs-decision/` — parking is not free there, by design (`run_one`'s docstring:
    "One attempt, so it is a human's to read"). So a promoted chore still reads
    `attempts >= CHORE_MAX_ATTEMPTS` afterwards, which means `chores.eligible()` will
    keep refusing it and the ordinary runner skips `kind: chore` cards outright — the
    card sits in `tasks/` looking dispatchable and is not. Found 2026-08-26: the panel's
    "move to tasks" button flipped `docs-production` straight past this with no word
    to the maintainer, who only found out when it never ran. The move itself is still
    allowed — refusing it would block the legitimate case of answering now and
    dispatching by name later (`--card`, which waives both restrictions on purpose) —
    but the return message says so, so the fact surfaces at the moment of the click
    rather than in a digest line easy to miss.
    """
    card = board.find(root, card_id)
    if card is None:
        raise DecideError(f"no card `{card_id}` on the board")

    text = card.text
    if not board.open_questions_settled(text):
        if card.after_answer != board.AFTER_ANSWER_TASKS:
            route = (f"it declares `after_answer: {card.after_answer}`, so the next step "
                     f"is re-triage — which rescopes the card around your answer and "
                     f"clears that section"
                     if card.after_answer == board.AFTER_ANSWER_TRIAGE else
                     "it does not declare an `after_answer:` route, so nothing here can "
                     "tell whether the answer scopes the card or settles a point inside "
                     "it — re-triage it, or declare the route on the card")
            raise DecideError(
                f"`{card_id}`'s `## Open questions` does not read `none`, and "
                f"`card_schema` refuses a card in tasks/ with a live question. {route}")
        if not has_maintainer_answer(text, attributor(root)):
            raise DecideError(
                f"`{card_id}` says `after_answer: tasks`, so answering it is what makes "
                f"it dispatchable — but no answer of yours is on record in `## Thread` "
                f"yet. Record one and this will settle `## Open questions` for you")
        text = board.settle_open_questions(
            text, on=(today or dt.date.today()).isoformat())

    text = re.sub(r"^state:.*$", "state: tasks", text, count=1, flags=re.MULTILINE)
    textio.write_text_lf(card.path, text)

    note = ""
    if card.kind == board.KIND_CHORE:
        from nightshift.runner import CHORE_MAX_ATTEMPTS  # local: keeps this module's
        # imports to board/manifest/textio at module scope; see the module docstring.
        if card.attempts >= CHORE_MAX_ATTEMPTS:
            note = (f" — heads up: this chore already used its {card.attempts} attempt "
                    f"(a chore gets {CHORE_MAX_ATTEMPTS}), so the chore batch will refuse "
                    f"it again and the ordinary runner skips `kind: chore` cards. Dispatch "
                    f"it by name when you're ready: `python -m nightshift.runner --card "
                    f"{card_id}`")
    return f"{card_id} → tasks/{note}"
