"""Review: the checker loop, the reviewer, and turning either outcome into a
merged, folded, board-moved result -- rebase, merge and hand-resolved conflict
recovery included.

`run_checker` is the producer-loop checker (`checker_contract: producer-loop`
charters); `review_branch` spawns the reviewer agent and reads its verdict;
`review_stage` is what the runner calls once a dispatch attempt is done --
it rebases or merges the branch (`rebase_and_merge`, `_resolve_conflict`,
`_merge_with_resolver`), folds the card's memory record, and returns the
`Dispatch` the runner settles. Imports `worker` for the reviewer/resolver's
own worker spawns and `verify` to re-run gates/tests after a rebase or a
review-requested fix.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from types import CodeType
from pathlib import Path

from nightshift import (board, branches, conflictmarkers, git, gitmerge, landing,
                        limits, memoryfold, reviewdiff, suite, textio, tiers,
                        worker_prompt)
from nightshift import hostconfig, outcome, startup, telemetry, verify, worker, worktree

# One fixed prompt, sent to whichever charter a card names as `checker:` — there is
# no per-checker template selection. `gates.card_schema` requires that charter's own
# frontmatter carry `checker_contract: producer-loop` before it may be named here at
# all (`checker-dispatch-wrong-template`, 2026-09-16); if this prompt's shape ever
# changes, every charter that declares that contract (art-reviewer today) has to
# change with it, or the gate is vouching for a promise the runner no longer keeps.
_CHECKER_PROMPT = """\
Judge the artefacts below against the acceptance criteria, at **tier: {tier}** (resolved to \
model `{model}`).

The artefacts are in:
  {artefacts}
Neighbouring assets already shipped, for the "does it belong?" judgement:
  {neighbours}

You are **not** told how these were made, and you must not go looking. No prompt, no \
pipeline settings, no generator reasoning, no card file. An agent that knows what was \
intended sees what was intended; you are here to see what is actually there.

Write your verdict to:
  {verdict_path}
as JSON, exactly these keys:
  {{"verdict": "pass" | "revise" | "reject",
    "best": "<filename of the strongest candidate, even if none passed>",
    "notes": "<one or two concrete, visual sentences the producer can act on \
without seeing the artefact>"}}

`pass` means it is good enough to ship, not that it is perfect. Name a `best` every time — \
whoever picks this up wants a ranking, not a tie.

--- acceptance criteria, verbatim from the card ---
{criteria}
"""


# The diff reviewer's prompt (automate-review-step). Same structural-blindness
# principle as `_CHECKER_PROMPT`: the runner builds this context, so there is no
# path by which the worker's prompt, transcript or reasoning reaches the reviewer
# — it sees the diff, the criteria and the repo, and nothing about how the change
# was made (§16). The verdict is a routing decision, not a quality score.
#
# Three-way since `reviewer-needs-fix-verdict` (2026-08-19). The original two-way
# contract (`ok` / `needs_decision`) forced every non-`ok` finding through the same
# lane whether or not it actually needed the maintainer's judgment — a concrete,
# verifiable defect with one correct fix (a wrong date, a misattributed commit, a
# name that does not match the code) got the identical "wake a human" treatment as
# a genuine ambiguity only they could resolve. `needs_decision` sat on the board
# waiting for an answer that was really "go check `git log`", the maintainer's time
# spent on something the next attempt could have applied directly. `needs_fix`
# closes that gap: it is drain.py's own principle ("'review this diff' is not a
# decision — it is work") extended one step further — *fixing* a diff whose defect
# has one verifiable correct answer is not a decision either.
# The three-way rubric, shared verbatim between a single card's review and a batch's —
# it is the definition of `ok`/`needs_fix`/`needs_decision` and nothing about *how many*
# items are being judged, so a batch judging each item independently (`_BATCH_REVIEW_PROMPT`)
# applies the exact same bar per item rather than a paraphrase of it. One string, quoted by
# both templates, is what keeps a future edit to the bar from drifting the two apart.
_REVIEW_RUBRIC = """\
- `ok` — nothing needs the maintainer's judgment before it merges. This is the common case. \
`ok` does not mean perfect; every `ok` card is still tested by the maintainer at testing/. \
Style you dislike or a cleaner refactor you can imagine is neither of the other two verdicts.
- `needs_fix` — the diff has a concrete, **objectively verifiable** defect with one correct \
answer: a fact you can confirm yourself (check `git log`, read the code it describes, run a \
calculation) rather than a preference. A claim the diff makes that is simply wrong; a value \
computed incorrectly; a docstring or comment that does not match what the code does; a \
reference to the wrong commit, date or symbol. State the defect **and** its correct fix — \
precise enough that a worker who never saw your reasoning can apply it without re-deriving \
it. This is not a lesser `needs_decision`: if you are choosing between the two because you \
are unsure whether your fix is *right*, it is `needs_decision`; choose `needs_fix` only when \
you would bet on the fix yourself.
- `needs_decision` — merging this needs the maintainer's judgment first: an ambiguity in the \
card the worker resolved by guessing, a design judgment call with more than one defensible \
answer, a behaviour that may not be what they want, or a value/name/rule the card left open \
and the worker picked. If two people could disagree about the right answer, this is it — \
`needs_fix` is reserved for the case where nobody reasonable would.

**The one case where you fix it yourself: prose.** If every defect you found is in \
text — a wrong number or name in a doc, comment, docstring, memory entry or recipe; a \
symbol this diff deleted still cited somewhere; a sentence describing the design the \
card asked for rather than the one that landed — then **apply the correction, commit it, \
and return `ok`** with `fixed` listing the files and `finding` still stating what was \
wrong. You have already read the tree and established the right answer; sending that back \
for someone else to type is the most expensive way to change a sentence.

The boundary is strict, and it is about *what you may edit*, not about how confident you \
feel:

- **Never change executable content.** No constant, no condition, no call, no test \
assertion, no statement moved or added. Comments, docstrings and Markdown only. The runner \
verifies this by compiling every `.py` you touched before and after — each side with its \
docstrings removed, so rewording one is prose, and unparsed from its syntax tree, so a \
comment that shifts the lines below it is prose too — and requiring identical bytecode. A \
behavioural edit is refused and the card round-trips as `needs_fix` anyway, so there is \
nothing to gain by trying. A string that is not a docstring is not prose: an assigned \
triple-quoted string is a value the program uses, and editing it is refused.
- **Fix every copy of a claim, not the first one.** A wrong sentence is usually wrong in \
more than one place — the docstring, the comment that paraphrases it, the memory fragment \
that records it. Before you commit (or write a `needs_fix` finding), grep the tree for the \
claim itself and list every occurrence. A correction that lands on three of four copies \
costs the card another whole round for the fourth.
- **Commit on top of what you reviewed.** An ordinary commit; never amend, reset or \
rebase — the worker's commits must stay exactly as they are underneath yours.
- **If any defect is not prose, the whole verdict is `needs_fix`.** Do not fix the text \
half and report the code half; one card, one route.\
"""

#: The re-review addendum (`review-reviews-only-the-fix`): non-empty whenever
#: `review_branch` was given a `prior_finding` — see its docstring. Substituted
#: into `_REVIEW_PROMPT` as `{prior_review}`; empty string leaves that paragraph
#: blank for an ordinary first review, so the template needs no separate variant.
_PRIOR_REVIEW_BLOCK = """
--- your own prior review of this branch ---
You already reviewed an earlier point on this branch and returned `needs_fix`. The diff \
above is only what changed since then — everything before it was already reviewed and is \
not repeated here. Verify that the finding below was actually fixed, and that the fix \
introduces nothing new; you do not need to re-verify code you already approved unless this \
diff touches it.

{finding}
"""

#: The same addendum for a fix round whose incremental base was **not** usable — the
#: branch was rebuilt since, so the diff above really is the whole branch
#: (`review-anchor-survives-the-replay`). It must not repeat the "only what changed"
#: sentence, which would be a flat lie about the diff the reviewer is holding; what it
#: can still say is the half that does not depend on the diff's near side, and that
#: half is worth saying. A reviewer told nothing at all cannot tell it is a fix round:
#: it re-asks a design question a previous round considered and declined, and re-derives
#: verifications a previous round already established — both measured, both expensive.
_PRIOR_REVIEW_FULL_BLOCK = """
--- your own prior review of this branch ---
You already reviewed this branch once and returned the `needs_fix` finding below; the
attempt since then was dispatched to apply it. The branch was rebuilt or replayed in the
meantime, so the diff above is the **whole** branch again, not only the fix — read it as
such. Two things follow, and they are the point of telling you:

- **Verify the finding below was actually fixed**, and that the fix introduced nothing new.
  That is the first call on your attention this round.
- **A design question a previous round of this review already considered is closed**, unless
  this diff changed the facts underneath it. If the last verdict was a `needs_decision` that
  the maintainer has since answered, or a `needs_fix` whose notes record a judgment call left
  deliberately unescalated, do not re-open it: it was already routed once, and routing it
  twice costs another round and another interruption for an answer that already exists.

{finding}
"""

_REVIEW_PROMPT = """\
Review the finished diff below against the card's acceptance criteria and the surrounding \
code, at **tier: lead** (resolved to model `{model}`).

The change is on branch `{branch}`. Its diff {diff_desc} {diff_where}
The repository it changes is rooted at:
  {repo}
Read the diff, then read whatever surrounding code you need to judge whether the change \
does what the card asked and touched nothing it should not have.
{prior_review}
You are **not** told how this was made, and you must not go looking: no worker prompt, no \
transcript, no reasoning. The gates and the full test suite have **already passed** on this \
exact branch — do not re-check anything they cover. {gates}Spend your attention only on what \
no script can see: whether the change does what the card asked, whether it touched what it \
should not have, and whether any acceptance criterion no test covers actually holds.

Your verdict is exactly three-way, and it is a routing decision:
{rubric}

Write your verdict to:
  {verdict_path}
as JSON, exactly these keys:
  {{"verdict": "ok" | "needs_fix" | "needs_decision",
    "fixed": ["<paths you corrected and committed yourself, prose only. [] otherwise.>"],
    "finding": "<if needs_fix: the defect, verified, and its correct fix, precise enough to \
apply without re-deriving it. If you fixed prose yourself: what was wrong and what you \
changed, so it can be checked after the fact. Empty string otherwise.>",
    "question": "<if needs_decision: what was attempted, what is ambiguous, the candidate \
answers, and what each would imply — those four parts are what makes the question \
answerable. Markdown in the shape below, line breaks written as `\\n`. Empty string \
otherwise.>",
    "notes": "<one or two sentences of reasoning the runner can log>"}}

On `needs_fix` the `finding` goes back to a worker for another attempt, with no human in the \
loop — so it must be self-contained enough to act on alone. On `needs_decision` the \
`question` is put in front of the maintainer verbatim, with no human in between, so it must \
stand alone and be answerable in fifteen seconds from a phone. You never edit, fix or merge \
— you report, the runner routes.

{question_format}

Your `question` is a JSON string, so write its line breaks as `\\n`: a question squeezed \
onto one line cannot hold that shape, and reaches the maintainer as prose.

--- acceptance criteria, verbatim from the card ---
{criteria}

--- what the card set out to do (its intent) ---
{intent}
{diff_inline}"""

# The chore batch's variant (chores.py `_review`): one diff bundling several independent
# one-prompter chores, judged **per item** in the one call rather than as a single
# pass/fail — so a batch review keeps its whole reason to exist (one reviewer call sees
# every item and can catch an interaction between two of them) without also making every
# survivor pay for one item's defect. Before this template, the batch produced one verdict
# for the combined diff and every survivor was handed the same finding regardless of which
# item it was actually about — correct for nothing, since either the clean items were
# wrongly told they had a defect, or the reviewer's own attribution ("only the X half needs
# a change") was sitting right there in `finding` and simply discarded by the caller.
_BATCH_REVIEW_PROMPT = """\
Review the finished diff below, at **tier: lead** (resolved to model `{model}`).

The diff bundles several **independent one-prompter chores**, merged onto one throwaway \
branch so they can be reviewed and landed in a single pass. Each is listed below, numbered \
and attributed to its own card id — the acceptance criteria and intent sections are grouped \
the same way. Judge each one **independently**: an item's own diff, against its own criteria \
and intent, decides its own verdict. You may certainly note an interaction *between* two \
items — that is the one thing a combined review can see that reviewing each alone cannot — \
but say so inside the specific item(s) it implicates rather than failing every item for one \
item's fault.

The change is on branch `{branch}`. Its diff against the integration branch — what this \
branch added since it forked — {diff_where}
The repository it changes is rooted at:
  {repo}
Read the diff, then read whatever surrounding code you need to judge whether each item does \
what its own card asked and touched nothing it should not have.

You are **not** told how any of this was made, and you must not go looking: no worker \
prompt, no transcript, no reasoning. The gates and the full test suite have **already \
passed** on this exact branch, with every item merged — do not re-check anything they \
cover. {gates}Spend your attention only on what no script can see.

For **each** numbered item below, apply this exact three-way verdict — it is a routing \
decision, made once per item:
{rubric}

Write your verdict to:
  {verdict_path}
as JSON, exactly these keys:
  {{"items": [
      {{"id": "<the card id exactly as numbered below>",
        "verdict": "ok" | "needs_fix" | "needs_decision",
        "finding": "<if needs_fix: same contract as the rubric above, for this item alone. \
Empty string otherwise.>",
        "question": "<if needs_decision: same contract as the rubric above, for this item \
alone. Empty string otherwise.>"}},
      ...
    ],
    "notes": "<one or two sentences of overall reasoning the runner can log>"}}
Include **exactly one entry per numbered item**, in any order, each carrying its exact card \
id — an id you omit is read as "could not be judged on its own" and is treated the same as \
`needs_decision`.

On an item's `needs_fix` the `finding` goes back to a worker for another attempt, with no \
human in the loop — so it must be self-contained enough to act on alone. On `needs_decision` \
the `question` is put in front of the maintainer verbatim, with no human in between, so it \
must stand alone and be answerable in fifteen seconds from a phone. You never edit, fix or \
merge — you report, the runner routes each item on its own verdict.

{question_format}

Your `question` is a JSON string, so write its line breaks as `\\n`: a question squeezed \
onto one line cannot hold that shape, and reaches the maintainer as prose.

--- acceptance criteria, per item, verbatim from each card ---
{criteria}

--- what each item set out to do (its intent) ---
{intent}
{diff_inline}"""
def run_checker(root: Path, card: board.Card, out_dir: Path, round_no: int,
                model: str, card_budget: float, timeout: int, *,
                effort: str = "") -> tuple[dict, float, limits.Wall | None]:
    """Spawn the card's checker on the artefacts of one round.

    **The checker's context is constructed here, and that is the whole point.**
    §16 says an agent reviewing its own output sees what it intended rather than
    what it made, so the reviewer must be denied the prompt, the pipeline and the
    reasoning. While the producer spawned its own reviewer that was a charter
    instruction — a rule the producer could quietly break. Built by the runner,
    the blindness is structural: there is no path by which the producer's prompt
    reaches the checker, because the runner never puts it there.

    It also puts the checker's tier through the dispatcher (§16's landing place
    2), which a nested spawn could not do: a subagent with no explicit model
    inherits its parent's, and `tier_guard` cannot see a spawn that names no
    card path.
    """
    artefacts = out_dir / "artefacts"
    verdict_path = out_dir / f"review-{round_no}.json"
    criteria = board.section(card.text, "Acceptance") or \
        board.section(card.text, "Acceptance criteria") or "(none stated on the card)"

    neighbours = hostconfig.neighbours_dir(root)
    prompt = _CHECKER_PROMPT.format(
        tier="worker", model=model, artefacts=artefacts.resolve().as_posix(),
        neighbours=(neighbours.resolve().as_posix() if neighbours else
                    "(this project declares no harvest dir, so there are none to compare against)"),
        verdict_path=verdict_path.resolve().as_posix(), criteria=criteria,
    )
    textio.write_text_lf(out_dir / f"review-{round_no}-prompt.md", prompt)

    binary = startup.claude_binary()
    assert binary
    argv = [
        binary, "-p",
        "--agent", card.checker,
        "--model", model,
        *(["--effort", effort] if effort else []),
        *worker._STREAM_ARGV,
        *worker._budget_argv(card_budget),
        *worker._STRICT_MCP_ARGV,
        "--permission-mode", str(hostconfig.host_setting(root, "permission_mode", "acceptEdits")),
        "--add-dir", str(artefacts.resolve()),
        *(("--add-dir", str(neighbours.resolve())) if neighbours else ()),
    ]
    cost = 0.0
    try:
        proc = worker._run_worker(argv, out_dir, timeout, out_dir / "stream.jsonl",
                           prompt=prompt)
        textio.write_text_lf(out_dir / f"review-{round_no}.log", proc.stdout + proc.stderr)
        result = telemetry._terminal_result(proc.stdout)
        try:
            cost = float(result.get("total_cost_usd", 0.0))
        except (ValueError, AttributeError, TypeError):
            pass
        # Checked before the verdict is read: a checker that never ran left no
        # verdict, and "no verdict" is a park (§13). Parking a card because the
        # night ran out of window would put a question to Karel that has nothing
        # to do with the card.
        wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return {"verdict": "revise", "notes": "the checker timed out"}, cost, None
    return telemetry._read_verdict(verdict_path), cost, wall


def card_review_context(card: board.Card) -> tuple[str, str]:
    """The `(criteria, intent)` a single card hands the diff reviewer.

    Lifted verbatim off the card — never summarised, never rephrased — because
    the reviewer's whole job is judging the diff against what was *asked for*,
    and a paraphrase is already a judgment about that.
    """
    criteria = board.section(card.text, "Acceptance") or \
        board.section(card.text, "Acceptance criteria") or "(none stated on the card)"
    return criteria, board.section(card.text, "Intent") or "(none stated on the card)"


def _behaviour_is_unchanged(root: Path, tree: Path, old: str, new: str) -> tuple[bool, str]:
    """Did the reviewer's commits change only prose — no executable behaviour?

    The check that makes a reviewer allowed to edit at all. For every `.py` file
    the reviewer touched, compile the old blob and the new one and require the
    **code objects to be equal**. Comments, blank lines and reflow do not survive
    into bytecode, so they compare equal; a changed literal, a moved call or a
    flipped condition does not. Every other file — Markdown, JSON, recipes — is
    prose by construction and passes.

    **Docstrings are prose and are compared as prose** (`reviewer-may-correct-a-
    docstring`). They do reach the code object, as `co_consts`, so a raw comparison
    refuses a reworded one — and that priced a whole lead-tier round at a rewritten
    sentence. Measured on `triage-findings-have-a-shelf-life` (2026-09-16): all three
    of round 3's findings were text, two of them in a gate's docstring, so the
    reviewer that had already established the right wording had to route the card
    back to a worker to type it; round 4 then cost $3.44 to land 29 lines of prose.
    So each side is normalised before it is compared — every docstring blanked, in
    the `Module`/`FunctionDef`/`AsyncFunctionDef`/`ClassDef` slot where a docstring
    is the syntactic first statement — and only then compiled.

    The same normalisation closes a false refusal this docstring used to claim it did
    not have. Comments and blank lines *do* survive into a nested code object's
    `co_firstlineno`, which `co_consts` compares, so adding a comment line shifted
    every function below it and was refused too — "comments and reflow compare equal"
    was simply not true. Normalising through `ast.unparse` drops comments and
    renumbers, so now it is.

    What stays refused is everything executable, and the boundary is the AST rather
    than the text: a changed literal, a moved call, a flipped condition, a reordered
    body, and a docstring turned into an assigned string (`y = "..."` is not the
    docstring slot). Anything that will not parse, unparse or compile is refused as
    well — a file this cannot check is a file it cannot vouch for.

    **The one residual exposure, and what covers it.** A docstring can reach runtime
    as data: `argparse(description=__doc__)` in nine modules across the two repos, and
    `readme_gen._first_sentence(module.__doc__)` for a gate with no `DESCRIPTION`. For
    the argparse cases a corrected sentence is the entire point. For the generated
    README it is real drift — caught by the `readme_generated` gate in the gate-and-test
    run the runner does on the *rebased* branch after the reviewer's commit and before
    the merge, which is the same net that covers every other way a reviewer's commit
    could surprise the tree.

    Returns `(ok, why)`; `why` is empty when ok. Any failure to read or compile is
    **not** ok — a file this cannot check is a file it cannot vouch for.
    """
    # `git.git`, not `git.changed`: that helper flattens "git refused"
    # and "nothing changed" into the same empty list, and here they must not be the
    # same answer — a diff this cannot read is a diff it cannot vouch for, while an
    # empty one is genuinely harmless. `-z` because a path git would quote or
    # C-escape in the newline-separated form is exactly what `git_path_lists`
    # exists to stop callers parsing by hand.
    listed = git.run(root, "diff", "--name-only", "-z", f"{old}..{new}")
    if listed.returncode != 0:
        return False, "could not list what the reviewer changed"
    for rel in git.split(listed.stdout):
        if not rel.endswith(".py"):
            continue
        before = git.run(root, "show", f"{old}:{rel}")
        after = git.run(root, "show", f"{new}:{rel}")
        if before.returncode != 0 or after.returncode != 0:
            return False, f"could not read both versions of {rel}"
        try:
            a = _compiled_without_docstrings(before.stdout, rel)
            b = _compiled_without_docstrings(after.stdout, rel)
        except SyntaxError as exc:
            return False, f"{rel} would not compile ({exc.msg})"
        except (ValueError, RecursionError, MemoryError) as exc:
            # `ast.unparse` refusing the tree, or a literal it cannot round-trip.
            # Unknown is not ok: the point of this guard is that it can vouch.
            return False, f"{rel} could not be normalised for comparison ({exc})"
        if a.co_code != b.co_code or a.co_consts != b.co_consts:
            return False, f"{rel} changed executable content, not only prose"
    return True, ""


def _compiled_without_docstrings(source: str, rel: str) -> CodeType:
    """`source` compiled with every docstring removed — the comparable form
    `_behaviour_is_unchanged` judges a reviewer's `.py` edit in.

    Two normalisations, both of things that are prose by definition:

    * **Docstrings** are dropped wherever one is the syntactic first statement of a
      module, function or class — `Expr(Constant(str))` in `body[0]`, which is what
      *makes* it a docstring rather than an expression that happens to be a string.
      An assigned string, a parenthesised one, or a string passed to a call is not
      in that slot and is left exactly where it is.
    * **Comments, blank lines and formatting** disappear through `ast.unparse`,
      which also renumbers every node — so a comment inserted above a function can
      no longer shift the `co_firstlineno` that `co_consts` compares.

    **Removed, not blanked**, because blanking still records *that there was one*:
    a function with no docstring has `None` in that const slot and one with a
    blanked docstring has `''`, so adding or deleting a docstring would still read
    as a change. Dropping the statement makes the three cases — absent, present,
    reworded — one case, which is the point. A body left empty by the drop gets a
    `Pass`, and that is sound rather than a patch over a hole: a function, class or
    module whose entire body *was* its docstring does exactly what `pass` does.

    Nothing executable is normalised, because nothing executable is outside the AST:
    whatever `unparse` drops was never in the tree, and whatever was in the tree is
    unparsed and recompiled. Raises `SyntaxError` on a file that will not parse, and
    `ValueError` on a tree `unparse` will not round-trip; both are refusals upstream.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = node.body[0] if node.body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            node.body.pop(0)
            # A module may legally be empty; a function or class body may not.
            if not node.body and not isinstance(node, ast.Module):
                node.body.append(ast.Pass())
    return compile(ast.unparse(tree), rel, "exec")


def _checkout_holding(root: Path, branch: str) -> Path | None:
    """The worktree that currently has `branch` checked out, if any.

    `git branch --force` refuses to move a branch a worktree is sitting on, and at
    review time the card's own worktree usually still is — the review runs in a
    *second*, detached checkout, it does not replace the first. Caught by
    `test_a_markdown_only_fix_lands_on_the_branch` before it could ever reach a
    real run: "fatal: cannot force update the branch 'ai/probe' used by worktree".
    """
    listed = git.run(root, "worktree", "list", "--porcelain").stdout
    where: Path | None = None
    for line in listed.splitlines():
        if line.startswith("worktree "):
            where = Path(line[len("worktree "):].strip())
        elif line.strip() == f"branch refs/heads/{branch}" and where is not None:
            return where
    return None


def _fast_forward(root: Path, branch: str, head: str) -> str:
    """Move `branch` forward to `head`. Returns "" on success, else why not.

    Two routes, because the branch may or may not be checked out somewhere. When a
    worktree holds it, the move has to happen *there* (`merge --ff-only`) so that
    checkout's HEAD, index and files stay consistent with the ref — updating the
    ref behind a live worktree's back would leave it reporting a dirty tree full of
    reversions it never made. Otherwise the plain `branch --force` is enough.
    """
    holder = _checkout_holding(root, branch)
    if holder is not None and holder.exists():
        done = git.run(root, "-C", str(holder), "merge", "--ff-only", head)
    else:
        done = git.run(root, "branch", "--force", branch, head)
    return "" if done.returncode == 0 else \
        f"the branch would not move ({done.stderr.strip()[:80]})"


def _land_review_fix(root: Path, tree: Path, branch: str,
                     verdict: dict, tip: str) -> dict:
    """Move `branch` onto a prose-only fix the reviewer applied itself.

    **Why the reviewer is allowed to edit here, having been forbidden to
    everywhere else.** A census of every `needs_fix` on Dungeoneer's record
    (2026-08-29) found 11 of 14 were not defective code but false prose — a number
    re-tuned after the sentence was written, a symbol the diff deleted still cited
    in a recipe, a comment naming a mechanism the change replaced. The reviewer had
    already read the tree, verified the defect and written the correct replacement
    out in full; the finding then went back to a fresh worker attempt whose entire
    job was to type it in. That round-trip is roughly 44% of everything the board
    has ever spent.

    The independence being protected by "the reviewer never edits" is real, but it
    is about *judgment of the work*, and it is not what a text correction consumes.
    Three things keep it honest:

      * **The reviewer still declares the defect.** `finding` is required even when
        it fixes, so the fix is reviewable after the fact rather than silent.
      * **The runner, not the reviewer, decides the fix may land.** The reviewer
        commits in its own detached checkout and can move nothing; this function
        checks the result and fast-forwards the branch, or does not.
      * **Nothing behavioural gets through** (`_behaviour_is_unchanged`), and
        `rebase_and_merge` re-runs the gates and the full suite over the result
        before it reaches the integration branch regardless.

    A refused fix is not lost: the verdict is rewritten to `needs_fix` carrying the
    reviewer's own finding, which is exactly the path it would have taken before.
    Degrading to the ordinary round-trip is always available, which is what makes
    this safe to attempt.
    """
    if str(verdict.get("verdict", "")).lower() != "ok" or not verdict.get("fixed"):
        return verdict

    def refuse(why: str) -> dict:
        hostconfig._log(f"    reviewer's prose fix refused ({why}) — routing as needs_fix")
        out = dict(verdict)
        out["verdict"] = "needs_fix"
        out["fixed"] = []
        out["notes"] = (str(verdict.get("notes", "")) +
                        f" [runner refused the reviewer's own fix: {why}]").strip()
        return out

    if not str(verdict.get("finding", "")).strip():
        return refuse("it described no finding, so nothing could be re-checked")
    head = git.run(root, "-C", str(tree), "rev-parse", "HEAD").stdout.strip()
    if not head or head == tip:
        return refuse("it reported a fix but committed nothing")
    # It must have built *on* the reviewed tip, never rewritten it: an amend or a
    # reset would carry the worker's own commits away with the correction.
    if git.run(root, "merge-base", "--is-ancestor", tip, head).returncode != 0:
        return refuse("its commit does not descend from the branch it reviewed")
    ok, why = _behaviour_is_unchanged(root, tree, tip, head)
    if not ok:
        return refuse(why)

    moved = _fast_forward(root, branch, head)
    if moved:
        return refuse(moved)
    files = ", ".join(str(f) for f in verdict.get("fixed", [])[:4])
    hostconfig._log(f"    reviewer fixed prose itself and it landed on {branch} — {files}")
    return verdict


def _gates_block(out_dir: Path) -> str:
    """The gate report the runner *already produced*, quoted into the reviewer's
    prompt — instead of the sentence that used to tell it to run the suite again.

    Both review templates carried "run `python -m nightshift.gates.run` once to
    see what that is rather than assuming", and the reasoning was sound: the
    reviewer must not guess at which rules are already enforced, because the gate
    list is the project's and grows. But the runner has just run that exact suite
    over this exact branch and written the report to `gates.txt` in `out_dir`
    (`_gate_report`), so the instruction bought a subprocess, a wait, and a
    lead-tier turn to re-derive a file sitting on disk. Handing it over closes
    that: same knowledge, no round-trip, and it arrives inside the cached prompt
    prefix rather than as a tool result the reviewer pays to carry forward.

    Not a licence to trust less. The reviewer is still told not to re-check what
    the gates cover — this only makes "what they cover" cheap to know, which is
    the half that was expensive.

    Degrades to the old behaviour if the report is missing or unreadable: a
    reviewer that cannot see the list is better off re-running it than assuming,
    and this must not be the thing that turns an unreadable file into a skipped
    check. Returns a block ending in a space, so the template reads correctly
    either way.
    """
    report = out_dir / "gates.txt"
    try:
        text = report.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        return ("Run `python -m nightshift.gates.run` once to see what they cover, "
                "rather than assuming — the gate list is this project's and grows "
                "as it earns rules. ")
    return (
        "Here is that gate run's own report, so you need not re-run it:\n\n"
        f"```\n{text}\n```\n\n"
    )


#: Appended to the reviewer's `intent` block when a drift repair rode along in the
#: diff (drift-should-not-end-the-night). Without it the reviewer meets a change
#: that matches none of the card's criteria and is *right* to object — the whole
#: point of `_REVIEW_RUBRIC` is that unexplained scope is a finding. This does not
#: exempt the repair from review: it says what the commit is for and asks for it to
#: be judged on its own terms, which is a narrower question than the card's.
_REPAIRED_BLOCK = """

--- a drift repair rode along in this diff, and it is sanctioned ---
The gates were red on the integration branch before this card's work existed, for a
reason outside its diff, so the runner had the drift fixed on this same branch rather
than losing the night to it: {repaired}

Judge that commit — it is a change like any other and may be wrong — but judge it
against *making the drifted gate honest*, not against the card's criteria, which it
was never meant to meet. Do not treat it as out-of-scope work by the card's worker. A
repair that instead loosened a gate, raised a budget to fit what was already there, or
allowlisted its way to green is exactly the defect worth a `needs_fix`."""


def review_branch(root: Path, label: str, out_dir: Path, model: str, base: str,
                  branch: str, card_budget: float, timeout: int, *,
                  criteria: str, intent: str, since: str = "", prior_finding: str = "",
                  repaired: str = "", effort: str = "",
                  template: str = _REVIEW_PROMPT) -> tuple[dict, float, limits.Wall | None]:
    """Spawn the diff reviewer on a finished branch (automate-review-step).

    `template` is `_REVIEW_PROMPT` (a single verdict) unless the caller passes
    `_BATCH_REVIEW_PROMPT` (one verdict per numbered item) — the chore batch's own
    case. Both quote `_REVIEW_RUBRIC` for the actual ok/needs_fix/needs_decision bar
    and take the same `{model, branch, diff, repo, verdict_path, criteria, intent}`
    substitutions, so this is the one place either is ever formatted.

    **The reviewer's context is constructed here, and that is the whole point** —
    the same structural blindness as `run_checker` (§16). It is given the diff
    (`git diff base...branch`, on disk), the acceptance criteria and intent of
    whatever produced the branch, and the code to read surrounding context. It is
    *not* given the worker's prompt, transcript or reasoning.

    Blindness made structural rather than promised: the review runs inside a
    **throwaway detached checkout of the branch**, and that is the only directory
    it is handed. Because `.ai/runs/` is gitignored, a fresh checkout cannot
    contain the worker's `prompt-N.md` or `worker-N.json` — so there is no path on
    disk from which the reviewer could read how the change was made, not merely a
    charter rule telling it not to look. Same worktree discipline as `merge_check`.

    **A branch, not a card.** One card's branch is the ordinary case
    (`review_stage`, with `card_review_context` supplying the two blocks), but the
    chore batch hands over a branch carrying several cards' work and reviews the
    combined diff once — which is the only way to see an interaction between two
    items that per-item review structurally cannot. `label` names the throwaway
    worktree and the log lines: a card id in the first case, a batch name in the
    second.

    **`since`/`prior_finding` (`review-reviews-only-the-fix`).** Every review used
    to diff `base...branch` unconditionally — first review and every re-review
    after a `needs_fix` alike — so a card that round-tripped through the reviewer
    twice paid for the *whole* branch's verification twice, at lead-tier prices,
    to approve a one-line fix. `review_stage` now passes `since` (the branch tip
    its own previous review actually saw, only once it has confirmed that point
    is still an ancestor of `branch`) and `prior_finding` (that review's own
    finding) whenever this is a `needs_fix` continuation. When `since` is given,
    the diff handed to the reviewer is `since...branch` — only the fix — and the
    prompt tells it what it already approved, instead of the ordinary
    `base...branch` full-branch diff. The reviewer still has the whole checked-out
    branch to read for context; only the *foregrounded* diff narrows. Empty
    `since` (the default, and every non-continuation call) is the original
    behaviour, byte-for-byte.

    **The two arrive independently** (`review-anchor-survives-the-replay`). They
    used to be one condition, which read a usable anchor as a precondition for
    mentioning the previous round at all — so a fix round whose anchor a replay had
    rewritten was handed the full diff *and* no word that it was a fix round.
    `since` governs the diff's near side and nothing else. A `prior_finding` with no
    `since` is the ordinary full diff plus `_PRIOR_REVIEW_FULL_BLOCK`: the finding to
    check, and the standing instruction not to re-route a question a previous round
    of this same review already routed.

    Returns `(verdict, cost, wall)`, all lookups — `{}` (no verdict file, a
    timeout, no CLI, or a worktree that would not cut) degrades in the caller to
    a human's eye, never to a guessed routing (§12).
    """
    binary = startup.claude_binary()
    if not binary:
        return {}, 0.0, None

    tree = worktree.worktree_root(root) / f"_review-{label}"
    if tree.exists() or worktree._worktree_registered(root, tree):
        git.run(root, "worktree", "remove", "--force", str(tree))
    git.run(root, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    # The branch tip as it stood before the reviewer saw it — the parent every
    # reviewer-applied prose fix must build on (`_land_review_fix`).
    tip = git.run(root, "rev-parse", branch).stdout.strip()
    try:
        made = worktree._worktree_add(root, "--detach", str(tree), branch)
    except worktree.WorktreePathTooLong as exc:
        hostconfig._log(f"    review worktree for {label} would not cut — {exc}")
        return {}, 0.0, None
    if made.returncode != 0:
        hostconfig._log(f"    review worktree for {label} would not cut — {made.stderr.strip()[:120]}")
        return {}, 0.0, None

    try:
        # Diff and verdict live *inside* the throwaway checkout, so the only
        # directory handed to the reviewer holds the change and nothing about how
        # it was made. Archived copies go to out_dir (in the main repo, never shown
        # to the reviewer) for the post-mortem.
        #
        # `since`, when given, replaces `base` as the diff's near side — the
        # incremental re-review (`review-reviews-only-the-fix`). The reviewer
        # still gets the whole checked-out branch to read; only what is
        # foregrounded as "the diff" narrows to what changed since its own last
        # look.
        diff = git.run(root, "diff", f"{since or base}...{branch}")
        # What the reviewer is shown is filtered (binaries and generated board
        # views only — each listed, never silently dropped) and, when it fits,
        # inlined at the end of the prompt so no turn is spent reading it back off
        # disk (`reviewdiff`). The unfiltered patch stays beside it for the reviewer
        # to open if a listed omission matters after all.
        shown = reviewdiff.filter_diff(diff.stdout)
        diff_path = tree / ".review-diff.patch"
        textio.write_text_lf(diff_path, shown.text)
        full_path = tree / ".review-diff-full.patch"
        if shown.omitted:
            textio.write_text_lf(full_path, diff.stdout)
        omitted = shown.omitted_note(full_path.resolve().as_posix())
        if len(shown.text) <= reviewdiff.INLINE_MAX_CHARS:
            diff_where = ("is at the very end of this prompt, verbatim — work from that "
                          "copy; there is no need to read it from disk.")
            diff_inline = (f"\n--- the diff, verbatim, to the end of this prompt ---\n"
                           f"{omitted}{shown.text}")
        else:
            diff_where = (f"is too large to inline, so it is in:\n  "
                          f"{diff_path.resolve().as_posix()}\n{omitted}")
            diff_inline = ""
        verdict_path = tree / ".review-verdict.json"

        # Three shapes, not two. `since` decides what the diff *is*, so it alone
        # picks `diff_desc`; `prior_finding` decides whether there is a prior round
        # to speak of, and it arrives on every fix round now — including the ones
        # whose anchor was unusable (`review-anchor-survives-the-replay`). The
        # middle case is that pair coming apart: a fix round on a full diff, which
        # gets the finding with the incremental sentence stripped out of it.
        if since and prior_finding:
            diff_desc = ("since your own last review of this branch — everything before "
                        "that point was already reviewed and is not repeated below")
            prior_review = _PRIOR_REVIEW_BLOCK.format(finding=prior_finding)
        elif prior_finding:
            diff_desc = "against the integration branch — what this branch added since it forked"
            prior_review = _PRIOR_REVIEW_FULL_BLOCK.format(finding=prior_finding)
        else:
            diff_desc = "against the integration branch — what this branch added since it forked"
            prior_review = ""

        # Appended to `intent` rather than given its own `{repaired}` slot, so
        # `_BATCH_REVIEW_PROMPT` — which never carries one — needs no change and
        # cannot raise a KeyError on a template that does not mention it.
        told = intent + (_REPAIRED_BLOCK.format(repaired=repaired) if repaired else "")
        prompt = template.format(
            model=model, branch=branch, diff_where=diff_where, diff_inline=diff_inline,
            repo=tree.resolve().as_posix(),
            verdict_path=verdict_path.resolve().as_posix(),
            criteria=criteria, intent=told, rubric=_REVIEW_RUBRIC,
            diff_desc=diff_desc, prior_review=prior_review,
            gates=_gates_block(out_dir),
            question_format=worker_prompt.QUESTION_FORMAT,
        )
        textio.write_text_lf(out_dir / "review-prompt.md", prompt)
        textio.write_text_lf(out_dir / "review-diff.patch", diff.stdout)

        argv = [
            binary, "-p",
            "--agent", hostconfig.REVIEWER_AGENT,
            "--model", model,
            *(["--effort", effort] if effort else []),
            # Streamed, like the worker's, so a review is auditable after the
            # fact. It was `--output-format json`, which leaves `review.log`
            # holding exactly one line — the terminal result — and that is the
            # reason a 2026-08-29 attempt to work out what the reviewer had spent
            # its 34 turns on could measure the worker turn-by-turn and the
            # reviewer not at all. Narrowing a reviewer you cannot watch is
            # guesswork; `_terminal_result` already reads either shape, so this
            # costs nothing but disk.
            *worker._STREAM_ARGV,
            *worker._budget_argv(card_budget),
            *worker._STRICT_MCP_ARGV,
            "--permission-mode", str(hostconfig.host_setting(root, "permission_mode", "acceptEdits")),
            "--add-dir", str(tree.resolve()),
        ]
        cost = 0.0
        try:
            proc = worker._run_worker(argv, tree, timeout,
                               out_dir / "review-stream.jsonl", prompt=prompt)
            textio.write_text_lf(out_dir / "review.log", proc.stdout + proc.stderr)
            try:
                cost = float(telemetry._terminal_result(proc.stdout).get("total_cost_usd", 0.0))
            except (ValueError, AttributeError, TypeError):
                pass
            wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            return {}, cost, None
        verdict = telemetry._read_verdict(verdict_path)
        if verdict:
            verdict = _land_review_fix(root, tree, branch, verdict, tip)
            textio.write_text_lf(out_dir / "review-verdict.json", json.dumps(verdict, indent=2))
        return verdict, cost, wall
    finally:
        git.run(root, "worktree", "remove", "--force", str(tree))
        git.run(root, "worktree", "prune")

# ```json ... ``` anywhere in the final message, last one wins. Tolerates a bare
# {...} block too, since a model that skips the fence is still answering.
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _verdict_from_message(text: str) -> dict:
    """Parse a checker's verdict out of its final chat message.

    Why this exists (2026-07-30): `stale-hunter` is declared
    `tools: Read, Grep, Glob` — deliberately, because a checker that can write is
    no longer a checker (§16's producer/checker seam). The prompt nonetheless told
    it to *write* `verdict.json`, a file it has no tool to create. So all 26 docs
    in the first `--stale` night ran to `subtype: success` with zero permission
    denials, each printing a well-formed verdict into its transcript, and every
    one was read back as `{}` — "no complete verdict", re-check next time. ~$15 of
    correct analysis discarded, and the ledger stayed empty so the next run would
    redo it identically.

    The lesson is the shape, not the typo: **a handover channel the agent's own
    tool grant forbids is a guaranteed silent no-op.** The channel is now the
    agent's reply, which needs no tool at all.
    """
    if not text:
        return {}
    blocks = _FENCED_JSON.findall(text)
    if not blocks:
        start, end = text.find("{"), text.rfind("}")
        blocks = [text[start:end + 1]] if 0 <= start < end else []
    for block in reversed(blocks):
        try:
            found = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(found, dict):
            return found
    return {}
# Review stage (automate-review-step) — the LLM half of §11's review step
# --------------------------------------------------------------------------
#
# A new, distinct always-on pipeline stage, NOT the `checker:` producer/retry
# loop. It sits between "gates+tests pass" and the card's final lane: the runner
# builds the reviewer's context, spawns it, and turns the two-way verdict into a
# routed outcome. Every branch here is a file-state lookup on the verdict — the
# only LLM call is the reviewer itself (§12). The routing decisions live in
# `settle`; this function only produces the outcome that drives them.


def _unmerged_paths(tree: Path) -> list[str]:
    """Paths git marks as unmerged (a rebase/merge conflict), for the review note."""
    return git.changed(tree, "--diff-filter=U")


_RESOLVE_PROMPT = """\
A rebase of `{branch}` onto `{base}` has stopped on a conflict. Resolve it, at \
**tier: lead** (resolved to model `{model}`). Follow your charter.

The repository is at `{repo}`, checked out in a throwaway worktree with the rebase \
**paused mid-flight**. Card: `{card_id}` — {title}

Conflicted paths ({count}), and you may edit **no others**:
{conflicts}

What is being replayed here is a card that has already passed gates, tests and code \
review against the tip it forked from. The other side of the conflict is whatever \
landed on `{base}` since. So the ordinary case is not a disagreement at all — it is \
two cards that each appended to the same log or list and collided on the anchor, and \
the correct resolution keeps **both** contributions.

Do not run `git rebase --continue`, `--abort`, `--skip`, or commit anything. Resolve \
the file contents and `git add` each path; the runner drives the rebase and re-runs \
the gates and the affected tests over your result before anything lands.

Write your verdict to `{verdict_path}` as JSON:

{{"resolved": true|false, "summary": "<what you kept from each side, 1-3 lines>"}}

`false` is a success, not a failure, and it is the right answer whenever the two \
sides genuinely disagree about the same thing and picking one would be a judgment \
about what the project should do. Say so in `summary` and the card goes to a human \
with your reading of it attached. Never guess between two intents.
"""


def _porcelain_paths(tree: Path) -> list[str]:
    """Every tracked path `git status` reports as dirty in `tree`, in its order.

    Split out of `_dirty_outside` so the before-and-after snapshots that function now
    compares are parsed by one piece of code rather than by two that could disagree.

    `-z` is NOT the NUL-for-newline swap it looks like: a rename or copy emits **two**
    records, `R  <new>NUL<old>NUL`, where the second carries no `XY ` prefix at all.
    (Only the non-`-z` form uses the single `R  old -> new` line.) Slicing every
    record at [3:] therefore turns the bare `a.md` into `.md` and reports a stray
    edit that does not exist — verified against git before this was written, which
    is the only reason it is not still here.
    """
    out = git.run(tree, "status", "--porcelain", "-z")
    records = [r for r in (out.stdout or "").split("\0") if r]
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status, rel = record[:2], record[3:].strip()
        if status[0] in ("R", "C") or status[1] in ("R", "C"):
            # Consume the source path; it is a path something moved *from*, so it
            # counts too, and it must not be re-read as a status record.
            if index < len(records):
                source = records[index].strip()
                index += 1
                if source and source not in paths:
                    paths.append(source)
        if rel and rel not in paths:
            paths.append(rel)
    return paths


def _blob_digest(path: Path) -> str:
    """A digest of `path`'s content, or `"<gone>"` if it is not there.

    Bytes, not `st_mtime` or `st_size`: an agent that rewrites a file to the same
    length inside one filesystem timestamp tick is exactly the case a cheap stat
    comparison would wave through, and this digest is the whole basis on which an
    auto-merged file is later called untouched.

    **A directory is digested recursively**, because `git status --porcelain` collapses
    an untracked directory to a single `?? dir/` record. Digesting only the name would
    make that record identical before and after — so a resolver could drop a file into
    a directory that was already untracked and the comparison would call it unchanged.
    The pre-baseline check reported every untracked path as a stray unconditionally,
    and the baseline must narrow that by *evidence*, never by blind spot.
    """
    if path.is_dir():
        digest = hashlib.sha256()
        for item in sorted(path.rglob("*")):
            if item.is_file():
                digest.update(item.relative_to(path).as_posix().encode("utf-8"))
                digest.update(_blob_digest(item).encode("utf-8"))
        return digest.hexdigest()
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<gone>"


def _dirty_fingerprint(tree: Path) -> dict[str, str]:
    """What `tree`'s dirty paths hold *before* the resolver is let near them.

    Taken while a rebase or merge is paused, which is the only moment it is true.
    See `_dirty_outside` for why the comparison cannot be made without it.
    """
    return {rel: _blob_digest(tree / rel) for rel in _porcelain_paths(tree)}


def _dirty_outside(tree: Path, allowed: set[str],
                   baseline: dict[str, str] | None = None) -> list[str]:
    """Tracked paths the resolver touched that were not its to touch.

    The agent is told which files it may edit; this is the check that it did. An
    unattended resolver with write access to a whole worktree is exactly the shape
    that needs its blast radius asserted rather than requested — the gates and tests
    that follow would catch a *broken* stray edit, but not a plausible one.

    **`baseline` is not an optimisation; without it this function is wrong.** It used
    to read `git status` alone and call every dirty path outside `allowed` a stray,
    on the assumption that a paused rebase leaves only its conflicts dirty. It does
    not. Git stops on the *commit*, not on the file: everything else in that commit
    which it could merge by itself is already **staged**, and so reported as modified
    against HEAD. `taser-cyberware` (2026-09-03) died of exactly this — it stopped on
    a four-file commit, one of which conflicted:

        e61b8f6  Pin the taser's tempo against the real TurnManager
          .ai/memory-fragments/taser-cyberware.md | 2 +-
          docs/catalog_report.md                  | 3 +-   <- the conflict
          dungeoneer/core/settings.py             | 2 +-
          tests/test_taser_perk.py                | 58 +++++

    The resolver read the conflict, judged it correctly and staged that one file. It
    was told it had edited the other three, the rebase was aborted, and a card that
    had already passed gates, tests and review landed in `blocked/` with a merge note
    accusing it of something git had done. Every conflict on a multi-file commit hit
    this, so the guard was strictest exactly where it was least entitled to be.

    So a path is a stray only if it is outside `allowed` **and** its bytes are not
    what they were when the pause began. A path git itself staged and the resolver
    left alone matches its baseline and is not reported; one the resolver edited,
    deleted or newly dirtied does not match — or is absent from the baseline — and
    still is. Comparing content rather than merely allowing the baseline's *paths* is
    the point: an auto-merged file quietly rewritten is the plausible stray edit this
    guard exists for, and it stays caught.

    `baseline` omitted means "nothing was dirty when we started" — the behaviour
    before 2026-09-03, correct only for a caller that truly begins from a clean tree.
    """
    baseline = baseline or {}
    touched: list[str] = []
    for rel in _porcelain_paths(tree):
        if rel in allowed or rel in touched:
            continue
        if rel in baseline and baseline[rel] == _blob_digest(tree / rel):
            continue
        touched.append(rel)
    return touched


def _resolve_conflict(root: Path, tree: Path, card: board.Card, branch: str,
                      base: str, out_dir: Path, *, model: str = "", timeout: int = 600,
                      card_budget: float = 0.0) -> tuple[bool, str]:
    """Hand a paused rebase to the resolver agent until it replays or it cannot.

    **Why this exists.** `rebase_and_merge` used to return `(False, "a human needs to
    resolve it")` on any conflict, and that sentence was usually false. Karel,
    2026-08-23, after resolving two of them by hand in one session: *"I didn't have to
    resolve it, you did"* — both were two cards each prepending a register entry to the
    same append-only log, which is bookkeeping, not judgment. The same class is in the
    corrections log twice before that (2026-08-09, 2026-08-13). A merge path with no
    resolver escalates every conflict at the difficulty of the hardest one.

    **Why an agent and not a merge strategy.** `-Xunion` or a `.gitattributes` driver
    would dissolve the log case declaratively and cheaply — and silently union two
    branches that genuinely edited the same prose, with nothing able to tell the two
    apart. The rule this function keeps instead is the one `gitmerge` already states:
    dissolve a class only when it is *provably* noise. Nothing here is proved by the
    resolution; it is proved by what follows it, which is why the caller's existing
    gates-and-tests re-verification is not optional and is not duplicated here.

    §12's "no LLM decides" is intact. The agent produces a *candidate tree*; every
    accept/reject after that is an exit code — markers, stray edits, `rebase
    --continue`, then the caller's gates and test slice. A resolver that declines, or
    one that returns a tree that fails any of those, lands the card in `blocked/` with
    the reason, which is where it would have gone anyway.

    Returns `(replayed, detail)`. On `False` the rebase has been aborted and the
    worktree is back where it started, so the caller's failure path is unchanged.
    """
    binary = startup.claude_binary()
    # Resolved here rather than by the caller, so that a caller which never reaches
    # this function never pays for it — `rebase_and_merge` runs on every merged card
    # and all but a few of them have no conflict at all, and `tiers.resolve` raises
    # on a project whose binding document is missing. Judgment about whether two
    # sides can both be kept is lead work; the charter says so too.
    model = model or tiers.resolve(root, "lead")
    # Same reasoning, same tier, one line later: the effort a tier is dispatched
    # with is resolved wherever its model is (`tiers.effort`), never carried
    # separately — a stage whose model says `lead` and whose effort says nothing
    # is exactly the split `token-economy.md` defect 4a was.
    effort = tiers.effort(root, "lead")
    # Defensive, though every production caller hands in a `run_dir()` that exists:
    # an exception raised here escapes with the rebase still paused, which is the one
    # state this function must never leave behind — the worktree is torn down by the
    # caller's `finally`, but `branch` would keep a `rebase-merge/` directory.
    out_dir.mkdir(parents=True, exist_ok=True)
    rounds = 0
    kept: list[str] = []
    while rounds < hostconfig.MAX_RESOLVE_ROUNDS:
        conflicts = _unmerged_paths(tree)
        if not conflicts:
            break
        rounds += 1
        # Per round, not once for the function: `rebase --continue` below advances to
        # the next commit, whose own auto-merged files are staged fresh, so a baseline
        # from a previous round would describe a tree that no longer exists.
        baseline = _dirty_fingerprint(tree)
        verdict_path = tree / ".resolve-verdict.json"
        verdict_path.unlink(missing_ok=True)
        prompt = _RESOLVE_PROMPT.format(
            branch=branch, base=base, model=model, repo=tree.resolve().as_posix(),
            card_id=card.id, title=card.title, count=len(conflicts),
            conflicts="\n".join(f"- `{path}`" for path in conflicts),
            verdict_path=verdict_path.resolve().as_posix(),
        )
        textio.write_text_lf(out_dir / f"resolve-{rounds}-prompt.md", prompt)

        argv = [
            binary, "-p",
            "--agent", hostconfig.RESOLVER_AGENT,
            "--model", model,
            *(["--effort", effort] if effort else []),
            "--output-format", "json",
            *worker._budget_argv(card_budget),
            *worker._STRICT_MCP_ARGV,
            "--permission-mode", str(hostconfig.host_setting(root, "permission_mode", "acceptEdits")),
            "--add-dir", str(tree.resolve()),
        ]
        try:
            proc = worker._run_worker(argv, tree, timeout, prompt=prompt)
        except subprocess.TimeoutExpired:
            git.run(tree, "rebase", "--abort")
            return False, (f"the {hostconfig.RESOLVER_AGENT} timed out after {timeout}s on "
                           f"{', '.join(conflicts)}")
        textio.write_text_lf(out_dir / f"resolve-{rounds}.log", proc.stdout + proc.stderr)

        verdict = telemetry._read_verdict(verdict_path)
        summary = str(verdict.get("summary", "")).strip()[:300]
        # The verdict is asked about FIRST, the process's exit second
        # (`wall-on-review-wrapup-discards-a-verdict`, 2026-08-09): a resolver that
        # wrote a complete answer and then walled on its own wrap-up turn has done
        # its job, and reading the exit first would throw that answer away and record
        # it identically to a resolver that died before writing anything.
        wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
        if wall and not telemetry.verdict_survives_a_wall(telemetry.RESOLVER_STAGE, verdict):
            git.run(tree, "rebase", "--abort")
            return False, (f"the {hostconfig.RESOLVER_AGENT} hit the {wall.scope} usage limit "
                           f"before it answered — not this card's fault, and the "
                           f"conflict is unexamined rather than judged")
        if not verdict.get("resolved"):
            git.run(tree, "rebase", "--abort")
            return False, (f"the {hostconfig.RESOLVER_AGENT} declined {', '.join(conflicts)}"
                           + (f": {summary}" if summary else " and gave no reason"))
        verdict_path.unlink(missing_ok=True)

        # Three exit-code checks, in cost order, before the rebase is allowed on.
        markers = conflictmarkers.scan(tree, conflicts)
        if markers:
            git.run(tree, "rebase", "--abort")
            return False, ("the resolution left a conflict marker behind — "
                           + "; ".join(markers[:3]))
        strays = _dirty_outside(tree, set(conflicts), baseline)
        if strays:
            git.run(tree, "rebase", "--abort")
            return False, (f"the {hostconfig.RESOLVER_AGENT} edited {', '.join(strays[:5])}, which "
                           f"was not part of the conflict")
        still = _unmerged_paths(tree)
        if still:
            git.run(tree, "rebase", "--abort")
            return False, f"{', '.join(still)} is still unmerged after the resolver ran"

        if summary:
            kept.append(summary)
        # `GIT_EDITOR=true` so the replayed commit keeps its existing message without
        # opening an editor this process has no terminal for.
        cont = git.run(tree, "rebase", "--continue",
                       env={**os.environ, "GIT_EDITOR": "true"})
        if cont.returncode != 0 and not _unmerged_paths(tree):
            git.run(tree, "rebase", "--abort")
            return False, (f"`git rebase --continue` failed after the resolution: "
                           f"{gitmerge.failure_detail(cont)}")
        if wall and _unmerged_paths(tree):
            # This round's answer was complete and was honoured, but the window is
            # shut: the next replayed commit conflicts too and spawning into a closed
            # window buys nothing. Stop with the reason, rather than burning the
            # remaining rounds on calls that cannot run.
            git.run(tree, "rebase", "--abort")
            return False, (f"the {hostconfig.RESOLVER_AGENT} resolved one conflict and then hit "
                           f"the {wall.scope} usage limit with more still to replay")

    if _unmerged_paths(tree):
        git.run(tree, "rebase", "--abort")
        return False, (f"still conflicting after {hostconfig.MAX_RESOLVE_ROUNDS} resolver "
                       f"round(s) — this one wants a human")
    # "No unmerged paths" is not "the rebase finished". Git also stops mid-rebase with
    # a clean index — an emptied commit, an `edit` stop — and the caller's very next
    # move is to read `HEAD` as the rebased tip, which would then be a partial replay
    # merged as if it were the whole branch. Ask git directly instead of inferring.
    if _rebase_in_progress(tree):
        git.run(tree, "rebase", "--abort")
        return False, ("the rebase stopped without a conflict to resolve (an emptied "
                       "commit, most likely) — this one wants a human")
    return True, " | ".join(kept) or "resolved with no summary"


_MERGE_RESOLVE_PROMPT = """\
A rebase of `{branch}` onto `{base}` could not be settled (see below), so the runner is \
retrying it as a plain merge instead — `git merge --no-ff {branch}` into `{base}`, done in \
a throwaway worktree. It has stopped on a conflict. Resolve it, at **tier: lead** \
(resolved to model `{model}`). Follow your charter.

The repository is at `{repo}`, checked out in a throwaway worktree with the merge \
**paused mid-flight** — a merge this time, not a rebase. Card: `{card_id}` — {title}

Conflicted paths ({count}), and you may edit **no others**:
{conflicts}

Why a merge and not the rebase this card was first tried as: `{base}` moved since \
`{branch}` forked, and the rebase's per-commit replay could not settle it ({rebase_reason}). \
A merge conflicts at most once per path, compared against the merge-base directly, rather \
than replaying each of `{branch}`'s commits against a `{base}` that keeps moving underneath \
them — which is exactly the shape of conflict a card's own board file produces when it \
changes lanes on `{base}` (dispatched from `tasks/`, finished in `blocked/` or `testing/`) \
while the branch still carries a commit that touches the old path. If that is what you are \
looking at, see your charter's rule on it.

Do not run `git merge --continue`, `--abort`, or commit anything. Resolve the file contents \
and `git add` each path — for a path deleted on one side, that may mean `git rm` rather than \
writing content. The runner checks your result and commits the merge itself.

Write your verdict to `{verdict_path}` as JSON:

{{"resolved": true|false, "summary": "<what you kept from each side, 1-3 lines>"}}

`false` is a success, not a failure, and it is the right answer whenever the two sides \
genuinely disagree about the same thing and picking one would be a judgment about what \
the project should do. Say so in `summary` and the card goes to a human with your reading \
of it attached. Never guess between two intents.
"""


def _resolve_merge_conflict(root: Path, tree: Path, card: board.Card, branch: str,
                            base: str, out_dir: Path, *, rebase_reason: str = "",
                            model: str = "", timeout: int = 600,
                            card_budget: float = 0.0) -> tuple[bool, str]:
    """Hand a paused **merge** (not rebase) to the resolver agent, once.

    Sibling of `_resolve_conflict`, for the retry `_merge_with_resolver` drives after
    a rebase-based resolution failed. A merge conflicts at most once — there is no
    per-commit replay to cascade through the way a rebase has — so this is a single
    dispatch rather than `_resolve_conflict`'s bounded round loop; `MAX_RESOLVE_ROUNDS`
    does not apply here.

    Same agent (`RESOLVER_AGENT`), same charter, same exit-code discipline (markers,
    stray edits, still-unmerged) as `_resolve_conflict` — only the situation and the
    finishing move differ: a rebase is driven onward with `git rebase --continue`; a
    merge has nothing to continue and is committed by the caller once this returns
    `True`.

    Returns `(resolved, detail)`. On `False` the merge has been aborted and the
    worktree is back at its pre-merge HEAD, matching `_resolve_conflict`'s contract.
    """
    binary = startup.claude_binary()
    model = model or tiers.resolve(root, "lead")
    effort = tiers.effort(root, "lead")
    out_dir.mkdir(parents=True, exist_ok=True)
    conflicts = _unmerged_paths(tree)
    if not conflicts:
        return True, "no conflicts to resolve"
    # A conflicted `git merge` stages everything it could merge alone, exactly as a
    # paused rebase does, so the stray-edit check below needs the same baseline.
    baseline = _dirty_fingerprint(tree)
    verdict_path = tree / ".resolve-verdict.json"
    verdict_path.unlink(missing_ok=True)
    prompt = _MERGE_RESOLVE_PROMPT.format(
        branch=branch, base=base, model=model, repo=tree.resolve().as_posix(),
        card_id=card.id, title=card.title, count=len(conflicts),
        conflicts="\n".join(f"- `{path}`" for path in conflicts),
        rebase_reason=rebase_reason or "the same paths",
        verdict_path=verdict_path.resolve().as_posix(),
    )
    textio.write_text_lf(out_dir / "merge-resolve-prompt.md", prompt)

    argv = [
        binary, "-p",
        "--agent", hostconfig.RESOLVER_AGENT,
        "--model", model,
        *(["--effort", effort] if effort else []),
        "--output-format", "json",
        *worker._budget_argv(card_budget),
        *worker._STRICT_MCP_ARGV,
        "--permission-mode", str(hostconfig.host_setting(root, "permission_mode", "acceptEdits")),
        "--add-dir", str(tree.resolve()),
    ]
    try:
        proc = worker._run_worker(argv, tree, timeout, prompt=prompt)
    except subprocess.TimeoutExpired:
        git.run(tree, "merge", "--abort")
        return False, (f"the {hostconfig.RESOLVER_AGENT} timed out after {timeout}s on "
                       f"{', '.join(conflicts)}")
    textio.write_text_lf(out_dir / "merge-resolve.log", proc.stdout + proc.stderr)

    verdict = telemetry._read_verdict(verdict_path)
    summary = str(verdict.get("summary", "")).strip()[:300]
    wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
    if wall and not telemetry.verdict_survives_a_wall(telemetry.RESOLVER_STAGE, verdict):
        git.run(tree, "merge", "--abort")
        return False, (f"the {hostconfig.RESOLVER_AGENT} hit the {wall.scope} usage limit before it "
                       f"answered — the merge conflict is unexamined rather than judged")
    if not verdict.get("resolved"):
        git.run(tree, "merge", "--abort")
        return False, (f"the {hostconfig.RESOLVER_AGENT} declined {', '.join(conflicts)}"
                       + (f": {summary}" if summary else " and gave no reason"))
    verdict_path.unlink(missing_ok=True)

    markers = conflictmarkers.scan(tree, conflicts)
    if markers:
        git.run(tree, "merge", "--abort")
        return False, ("the resolution left a conflict marker behind — "
                       + "; ".join(markers[:3]))
    strays = _dirty_outside(tree, set(conflicts), baseline)
    if strays:
        git.run(tree, "merge", "--abort")
        return False, (f"the {hostconfig.RESOLVER_AGENT} edited {', '.join(strays[:5])}, which "
                       f"was not part of the conflict")
    still = _unmerged_paths(tree)
    if still:
        git.run(tree, "merge", "--abort")
        return False, f"{', '.join(still)} is still unmerged after the resolver ran"

    return True, summary or "resolved with no summary"


def _rebase_in_progress(tree: Path) -> bool:
    """Whether `tree` is still mid-rebase, asked of git rather than inferred.

    Both state directories are checked: `rebase-merge/` is today's, `rebase-apply/`
    the older `--am` backend's, and which one exists depends on flags this function
    does not control.
    """
    return any(
        (tree / ".git" / name).exists()
        or Path(git.run(tree, "rev-parse", "--git-path", name).stdout.strip() or "\0").exists()
        for name in ("rebase-merge", "rebase-apply")
    )


def _reconcile_dirty_bookkeeping(root: Path) -> tuple[bool, str]:
    """Fold `root`'s own uncommitted changes into a commit before anything merges
    into it — but only when every one of them is board or memory bookkeeping.

    **Why this exists** (aim-crit-display-desync, 2026-08-29). `landing.land`'s merge and
    `_merge_with_resolver`'s final `--ff-only` both update `root`'s working tree
    directly, and git refuses either outright — `"error: Your local changes to
    the following files would be overwritten by merge"` — the instant a path it
    needs to touch already carries an uncommitted diff, *before* it ever attempts
    a real three-way resolution. That refusal leaves no unmerged paths at all
    (`_unmerged_paths` reads empty), so it is textually indistinguishable from a
    genuinely broken checkout — and on this card it merged a rebase that had just
    replayed and re-verified clean (51 gates, 2243 tests), only to have the *next*
    step fail on `root` carrying an uncommitted edit to the card's own board file.

    `root` is the runner's dedicated integration checkout; nothing else writes to
    it between dispatches. A stray uncommitted change sitting there is the
    runner's own prior write, not caught up in a commit yet — folding it in
    before the merge is what would already be true had that step committed it
    itself, and carries no more judgment than that. A dirty path that is *not*
    bookkeeping is left exactly as found: that could be real, uncommitted work
    (Karel's own checkout, in the in-place-fallback topology) and this function
    has no business deciding what to do with it (§12).

    Returns `(reconciled, detail)`. `False` and `root` untouched when there was
    nothing to do (already clean) or a dirty path fell outside board/memory —
    the caller proceeds exactly as it did before this existed, so a real problem
    still surfaces at the merge step it always did. `detail` carries a commit
    failure's reason, for the caller's own log.
    """
    dirty = git.changed(root, "HEAD")
    if not dirty:
        return False, ""
    frag_prefix = memoryfold.fragment_dir(root).relative_to(root).as_posix() + "/"

    def _is_bookkeeping(rel: str) -> bool:
        norm = rel.replace("\\", "/")
        if norm.startswith(frag_prefix) or norm.startswith(_MEMORY_PREFIX):
            return True
        return suite.classify(norm, root) in _BOOKKEEPING_CLASSES

    if not all(_is_bookkeeping(p) for p in dirty):
        return False, ""
    committed = git.run(root, "commit", "-am",
                     "board: fold a pending bookkeeping write before merging "
                     "(runner recovery)")
    if committed.returncode != 0:
        detail = (committed.stderr or committed.stdout or "").strip()[:150]
        hostconfig._log(f"  ! root had uncommitted bookkeeping changes ({', '.join(dirty)}) that "
             f"could not be committed — {detail}")
        return False, detail
    hostconfig._log(f"    root had uncommitted bookkeeping changes ({', '.join(dirty)}) — folded "
         f"them into a commit before merging")
    return True, ""


def _bookkeeping_merge_fallback(root: Path, card: board.Card, branch: str, base: str,
                                out_dir: Path, *, why: str, test_timeout: int,
                                remote: str,
                                plan: landing.Plan | None = None) -> tuple[bool, str]:
    """If `base`'s own divergence since the merge-base is provably confined to
    board/memory bookkeeping, retry landing `branch` as a plain merge
    (`_merge_with_resolver`) instead of giving up on it.

    Shared by every place `rebase_and_merge` can fail without a human actually
    needing to weigh in: a rebase conflict the resolver declined (the original
    `stun-animation` case, 2026-08-27), a rebase that refused before producing any
    conflict markers at all (git's "local changes would be overwritten"), and
    `landing.land`'s own failure landing an already-reverified rebase result
    (both aim-crit-display-desync, 2026-08-29). All three are the same
    underlying situation surfacing through a different git error: `base` moved in
    bookkeeping only, so nothing about the card's actual work disagrees with it.

    Returns `(landed, detail)`. `landed=False` with `detail=""` means `base` has
    moved in real production code since the fork — the narrow-scope guard holds
    and the fallback is not even attempted, exactly as before this existed.
    `landed=False` with a non-empty `detail` means the plain-merge attempt was
    tried and failed too; `why` (the caller's own original failure reason) is
    still the right thing to report alongside it.
    """
    merge_base = git.run(root, "merge-base", branch, base).stdout.strip()
    production = (_bookkeeping_divergence(root, base, merge_base)
                 if merge_base else ["(no merge-base found)"])
    if production:
        return False, ""
    hostconfig._log(f"    {branch} did not land against {base} ({why}), but {base} has only "
         f"moved in board/memory bookkeeping since it forked — retrying as a "
         f"plain merge")
    landed, why2 = _merge_with_resolver(
        root, card, branch, base, out_dir, rebase_reason=why,
        test_timeout=test_timeout, remote=remote, plan=plan)
    if landed:
        hostconfig._log(f"    landed as a plain merge — {why2}")
    return landed, why2


def rebase_and_merge(root: Path, card: board.Card, branch: str, base: str,
                     test_timeout: int = 600, remote: str = "",
                     plan: landing.Plan | None = None) -> tuple[bool, str]:
    """Rebase a reviewed-ok card's branch onto the current integration tip,
    re-verify gates + the affected test slice, then merge (runner-hardening #3).

    The recurring problem this fixes: two cards edited the same night — memory docs
    especially — pass review each against the tip they forked from, then collide
    when the second tries to land. Rebasing `branch` onto today's `base` *before*
    merging replays the card's commits on the current tip, so a textual conflict
    surfaces here and a clean replay is re-checked before it lands.

    **A conflict is handed to `_resolve_conflict`, not straight to a human**
    (2026-08-23). It used to be the latter, and the message said so — "a human needs
    to resolve it" — which was usually untrue: the collision is nearly always two
    cards appending to the same log, and Karel's objection was precisely that the
    session resolving it by hand had made no judgment worth a person's time. Only a
    resolution that is *declined, marker-ridden, out of bounds, or fails the replayed
    gates and tests* reaches a human now, and it reaches them in `blocked/` rather
    than in `review/`, because the card is not awaiting review — it was reviewed `ok`
    and is awaiting a merge.

    **A rebase-based resolution that still fails gets one more try, as a plain
    merge, before it reaches a human** (`_merge_with_resolver`, 2026-08-27). The
    `stun-animation` incident: a card's own board file changes lanes on `base`
    while the branch still carries a commit touching the old path, and a *rebase*
    replays that commit against a moving target and conflicts on paths the two
    sides never actually disagree about (verified by hand that night — Karel:
    *"I didn't have to resolve it, you did... it automatically runs a subagent,
    that handles it"*). A plain merge conflicts once, against the merge-base
    directly, and is retried **only** when `base`'s own divergence since the
    merge-base is provably confined to board/memory bookkeeping
    (`_bookkeeping_divergence`) — narrow scope, Karel's own choice: this is not a
    second chance at a genuine production-code conflict, which still goes straight
    to a human exactly as before.

    **The same fallback (`_bookkeeping_merge_fallback`) now covers two more
    shapes of "no real disagreement" that used to go straight to a human**
    (aim-crit-display-desync, 2026-08-29): a rebase that refuses before
    producing any conflict markers at all — git's "your local changes... would
    be overwritten," textually indistinguishable from a genuinely broken
    checkout — and `landing.land`'s own failure landing a rebase that had
    already replayed and re-verified clean. Both were previously dead ends with
    no retry whatsoever, even when `base` had moved in bookkeeping only.
    `_reconcile_dirty_bookkeeping` additionally folds a stray uncommitted
    bookkeeping write already sitting in `root` into a commit *before* either
    merge attempt runs, since that is what actually produces the first shape on
    `root`'s own dedicated checkout — nothing else writes there between
    dispatches, so an uncommitted diff found there is the runner's own, not a
    disagreement to adjudicate.

    Done entirely in a throwaway detached worktree, so the real `ai/<id>` branch is
    never rewritten while this runs, and `base` is untouched until the verified,
    rebased result merges. Reuses `merge_check`'s worktree discipline. §12 holds
    across the resolver too: it produces a candidate tree, and every accept/reject
    after it — markers, stray edits, `rebase --continue`, gates, tests, the merge —
    is an exit code.

    Once the rebased result actually merges, `branch` is deleted — locally, and
    on `remote` too. It was the deliverable back when Karel merged cards by hand
    (Session G) — nothing reads it once this function has merged automatically,
    and keeping it forever only produces stale refs that `git branch --merged`
    cannot even recognise as merged (the commit that lands is the rebased copy,
    not `branch`'s own tip). A failed merge leaves both copies in place, since a
    human still needs the branch to resolve the conflict.

    `remote` is the host's `publish_remote`, threaded in by `settle` the same way
    `base` is (`default_base`) rather than read from `host_setting` here: this
    stays a function of its arguments — testable against a bare remote without
    planting a host config — and it matches `publish(root, remote, base, …)`, so
    both halves of a card branch's remote lifetime take the remote the same way.
    The `""` default is the schema default and means "no remote deletion", so
    every caller that has not opted into publishing keeps today's behaviour
    exactly. `landing.delete_remote_branch` carries the guard and the reasoning; note
    only that it must run **before** the local `-D`, because that guard is an
    ancestry test against the local branch's own tip.

    Returns `(merged, detail)`. Any failure — the branch gone, a conflict the
    resolver could not settle, or gates/tests failing on the replayed result —
    returns `(False, reason)` with `base` unmoved, and `settle` routes the card to
    `blocked/` with the reason. Never a guess (decision #3).

    `plan` (`landing.Plan`) is the lane the card moves to once it has landed, as part
    of the same `landing.land` call; `None` leaves the card where it is.
    """
    if git.run(root, "rev-parse", "--verify", branch).returncode != 0:
        return False, f"`{branch}` no longer exists"

    _reconcile_dirty_bookkeeping(root)

    tree = worktree.worktree_root(root) / f"_rebase-{card.id}"
    if tree.exists() or worktree._worktree_registered(root, tree):
        git.run(root, "worktree", "remove", "--force", str(tree))
    git.run(root, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = worktree._worktree_add(root, "--detach", str(tree), branch)
    except worktree.WorktreePathTooLong as exc:
        return False, f"could not cut a rebase worktree for {branch}: {exc}"
    if made.returncode != 0:
        return False, (f"could not cut a rebase worktree for {branch}: "
                       f"{(made.stderr or made.stdout or '').strip()[:150]}")

    out_dir = telemetry.run_dir(root, card, card.attempts)
    try:
        rebased = git.run(tree, "rebase", *gitmerge.STRATEGY_ARGS, base)
        if rebased.returncode != 0:
            conflicts = _unmerged_paths(tree)
            why = gitmerge.failure_detail(rebased)
            if not conflicts:
                # A rebase that failed without leaving unmerged paths carries no
                # content conflict for a resolver to look at — but it is not
                # necessarily a dead end either. Git's merge backend can refuse a
                # replay outright ("your local changes... would be overwritten",
                # rather than a CONFLICT marker) the instant a path it needs to
                # touch already differs from HEAD, and `base` having only moved
                # in board/memory bookkeeping since the fork is exactly the
                # `_bookkeeping_merge_fallback` case below — it just surfaced
                # here instead of past `_resolve_conflict`. Abort first: the tree
                # must be clean before anything else touches it.
                git.run(tree, "rebase", "--abort")
                landed, why2 = _bookkeeping_merge_fallback(
                    root, card, branch, base, out_dir, why=why,
                    test_timeout=test_timeout, remote=remote, plan=plan)
                if landed:
                    return True, why2
                detail = f"; retried as a plain merge and {why2}" if why2 else ""
                return False, f"rebasing {branch} onto {base} failed: {why}{detail}"
            hostconfig._log(f"    {len(conflicts)} conflict(s) rebasing {branch} onto {base} — "
                 f"handing them to {hostconfig.RESOLVER_AGENT}")
            resolved, detail = _resolve_conflict(
                root, tree, card, branch, base, out_dir,
                timeout=test_timeout, card_budget=0.0)
            if not resolved:
                landed, why2 = _bookkeeping_merge_fallback(
                    root, card, branch, base, out_dir, why=detail,
                    test_timeout=test_timeout, remote=remote, plan=plan)
                if landed:
                    return True, why2
                if why2:
                    detail = f"{detail}; retried as a plain merge and {why2}"
                return False, (f"rebasing {branch} onto {base} conflicts in "
                               f"{', '.join(conflicts)} and {detail} — a human needs to "
                               f"resolve it: {why}")
            hostconfig._log(f"    {hostconfig.RESOLVER_AGENT} resolved it — {detail}")
        rebased_sha = git.run(tree, "rev-parse", "HEAD").stdout.strip()

        # Re-verify the *replayed* result, not the branch as reviewed (decision #2).
        status, why = verify._run_gates(root, tree, out_dir / "rebase-gates.txt")
        if status != verify.GATE_PASS:
            return False, f"after rebasing onto {base}, {why}"
        changed = set(git.changed(tree, f"{base}...HEAD"))
        selection = suite.select(changed, tree)
        try:
            # The excerpt is dropped here on purpose: this path's reason lands in
            # `## Merge` on a card bound for `review/`, where the question is "will
            # it replay onto the base", not "which assertion broke". `why` already
            # names the first failing test (`suite.check_junit`), which is the right
            # granularity for a merge note.
            ok, why, _ = verify._run_tests(tree, out_dir / "rebase-pytest.txt", test_timeout,
                                    out_dir / "rebase-junit.xml",
                                    selection.pytest_args(tree / suite.tests_rel(root)))
        except subprocess.TimeoutExpired:
            ok, why = False, f"pytest: timed out after {test_timeout}s"
        if not ok:
            return False, f"after rebasing onto {base}, {why}"

        # Land the verified rebased commits — merge, fold, branch cleanup and the
        # lane move are one call (`landing.land`). The throwaway worktree still holds
        # HEAD at `rebased_sha`, keeping it referenced; the `finally` drops it after.
        # `-D`: what lands is the rebased copy, so the branch's own tip is never an
        # ancestor of `base`.
        merged, why = landing.land(root, card, branch=branch, base=base, plan=plan,
                                   remote=remote, ref=rebased_sha, label=branch,
                                   force_delete=True)
        if not merged:
            # The replay re-verified green; the failure is landing it onto `root`.
            # Same narrow-scope retry as the conflict shapes above
            # (aim-crit-display-desync, 2026-08-29).
            landed, why2 = _bookkeeping_merge_fallback(
                root, card, branch, base, out_dir, why=why,
                test_timeout=test_timeout, remote=remote, plan=plan)
            if landed:
                return True, why2
            if why2:
                why = f"{why}; retried as a plain merge and {why2}"
            return False, (f"rebasing {branch} onto {base} replayed and verified "
                           f"cleanly, but landing it failed: {why}")
        return merged, why
    finally:
        git.run(root, "worktree", "remove", "--force", str(tree))
        git.run(root, "worktree", "prune")


_BOOKKEEPING_CLASSES = frozenset({suite.BOARD, suite.NOTE})
_MEMORY_PREFIX = ".claude/memory/"


def _bookkeeping_divergence(root: Path, base: str, merge_base: str) -> list[str]:
    """Paths `base` gained since `merge_base` that are production code, not board or
    memory bookkeeping — empty means the narrow-scope condition for
    `_merge_with_resolver` holds.

    **Deliberately narrower than `suite.classify`'s own `"other"` bucket.** `other`
    is that function's catch-all for "docs, memory, `.claude/` config — anything no
    pytest asserts on" (its own docstring), which is the right answer for deciding
    which *tests* to run but the wrong one here: it also covers a bare top-level
    production file outside every declared `source_dir` (a `main.py`, a
    `pyproject.toml`), and this function's whole job is telling those apart from
    board and memory bookkeeping. So this checks `BOARD`/`NOTE` — the same
    board-lane split `suite.select` already trusts — plus two explicit
    bookkeeping prefixes rather than `classify`'s broad default: `.claude/memory/`
    (`CLAUDE.md`'s own memory directory, never production code by construction)
    and the memory-fragment directory (which `classify` would otherwise call
    `SYSTEM`, since gates and tooling live under `.ai/` too, and a per-card
    fragment is neither of those things — see `memoryfold.FRAGMENT_DIR`'s own
    docstring on why it lives under `.ai/` rather than the board).
    """
    changed = git.changed(root, f"{merge_base}...{base}")
    frag_prefix = memoryfold.fragment_dir(root).relative_to(root).as_posix() + "/"
    production = []
    for rel in changed:
        norm = rel.replace("\\", "/")
        if norm.startswith(frag_prefix) or norm.startswith(_MEMORY_PREFIX):
            continue
        if suite.classify(norm, root) not in _BOOKKEEPING_CLASSES:
            production.append(rel)
    return production


def _merge_with_resolver(root: Path, card: board.Card, branch: str, base: str,
                         out_dir: Path, *, rebase_reason: str = "",
                         test_timeout: int = 600, remote: str = "",
                         plan: landing.Plan | None = None) -> tuple[bool, str]:
    """The narrow-scope escalation `rebase_and_merge` reaches for — via
    `_bookkeeping_merge_fallback` — whenever a rebase-based landing of `branch`
    onto `base` did not settle (a conflict the resolver declined, a rebase that
    refused before producing conflict markers at all, or the final merge onto
    `root` failing after a clean, re-verified replay), provided `base` has only
    moved in board/memory bookkeeping since `branch` forked
    (`_bookkeeping_divergence` empty). Retries as a plain `git merge --no-ff
    branch` instead of a rebase replay.

    **Why this exists** (`stun-animation`, 2026-08-27). A card's own board file
    moves lanes on `base` — dispatched from `Board/tasks/`, finished in
    `Board/blocked/` or `Board/testing/` — while the branch still carries a
    commit that touches the old path. A *rebase* replays that commit against a
    `base` that has already moved the file, and conflicts on it even though
    nothing about the card's actual work disagrees with anything on `base`. A
    *merge* compares `branch` and `base` against their real merge-base directly,
    conflicts on that one path once, and — when the check above holds — touches
    nothing a human needs to weigh in on. Watching a human resolve exactly this
    by hand, Karel asked for the automatic version: *"if there is such a problem
    with rebase, it automatically runs a subagent, that handles it... It is
    obviously something you can handle, so it does not need human attention"* —
    full autonomy, landing the card end to end, but narrowly scoped to provable
    non-production divergence (his own choice, offered as the safer of two, over
    every conflict the rebase path could produce).

    **§12 still holds.** The resolver (`_resolve_merge_conflict`, same
    `RESOLVER_AGENT`, same charter) only produces a candidate tree; the merge is
    committed in a throwaway worktree and re-verified with the *same*
    gates-and-tests re-run `rebase_and_merge` already does for the ordinary path
    — before anything touches `root`. Landing into `root` itself is a bare
    `git merge --ff-only`, never `--no-ff`: the throwaway worktree's merge commit
    already has `root`'s current `base` tip as its first parent (nothing else can
    have moved `base` mid-call), so fast-forwarding is the correct move and the
    only one that does not wrap one merge commit inside another.

    Because this *is* a real merge (unlike the rebase path, whose `rebased_sha`
    is a linear replay with no merge commit of its own), `branch`'s own tip
    becomes a genuine ancestor of `base` once this lands — so the branch delete
    below uses the safe `-d`, not the rebase path's `-D`, matching the same
    reasoning `manage-board`'s inline-merge flow already documents.

    Returns `(landed, detail)`. `True` means the merge is on `base`, `branch` is
    deleted (local and, if `remote` is configured, there too), and the card's
    memory fragment has been folded — exactly what the ordinary merged path in
    `rebase_and_merge` does. `False` leaves `root` and `branch` untouched for a
    human, and the caller's existing failure message still fires.
    """
    tree = worktree.worktree_root(root) / f"_merge-{card.id}"
    if tree.exists() or worktree._worktree_registered(root, tree):
        git.run(root, "worktree", "remove", "--force", str(tree))
    git.run(root, "worktree", "prune")
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        made = worktree._worktree_add(root, "--detach", str(tree), base)
    except worktree.WorktreePathTooLong as exc:
        return False, f"could not cut a merge worktree for {branch}: {exc}"
    if made.returncode != 0:
        return False, (f"could not cut a merge worktree for {branch}: "
                       f"{(made.stderr or made.stdout or '').strip()[:150]}")

    try:
        merge = git.run(tree, "merge", *gitmerge.STRATEGY_ARGS, "--no-ff", "--no-commit",
                     branch)
        if merge.returncode != 0:
            conflicts = _unmerged_paths(tree)
            if not conflicts:
                # No unmerged paths but a non-zero exit is not a content conflict —
                # a dirty tree, a missing branch, a hook refusal. Nothing to resolve.
                git.run(tree, "merge", "--abort")
                return False, (f"merging {branch} into {base} failed: "
                               f"{gitmerge.failure_detail(merge)}")
            hostconfig._log(f"    {len(conflicts)} conflict(s) merging {branch} into {base} — "
                 f"handing them to {hostconfig.RESOLVER_AGENT}")
            resolved, detail = _resolve_merge_conflict(
                root, tree, card, branch, base, out_dir,
                rebase_reason=rebase_reason, timeout=test_timeout)
            if not resolved:
                return False, (f"merging {branch} into {base} conflicts in "
                               f"{', '.join(conflicts)} and {detail}")
            hostconfig._log(f"    {hostconfig.RESOLVER_AGENT} resolved it — {detail}")

        committed = git.run(tree, "commit", "-m",
                         f"merge {branch}: reviewed ok by the runner (a rebase-based "
                         f"landing did not settle on bookkeeping-only paths of {base}; "
                         f"resolved as a plain merge)")
        if committed.returncode != 0:
            git.run(tree, "merge", "--abort")
            return False, (f"could not commit the resolved merge of {branch}: "
                           f"{(committed.stderr or committed.stdout or '').strip()[:150]}")
        merged_sha = git.run(tree, "rev-parse", "HEAD").stdout.strip()

        # Re-verify the *merged* result, exactly as the rebase path re-verifies its
        # replay, before this touches `root` at all (decision #2).
        status, why = verify._run_gates(root, tree, out_dir / "merge-gates.txt")
        if status != verify.GATE_PASS:
            return False, f"after merging into {base}, {why}"
        changed = set(git.changed(tree, f"{base}...HEAD"))
        selection = suite.select(changed, tree)
        try:
            ok, why, _ = verify._run_tests(tree, out_dir / "merge-pytest.txt", test_timeout,
                                    out_dir / "merge-junit.xml",
                                    selection.pytest_args(tree / suite.tests_rel(root)))
        except subprocess.TimeoutExpired:
            ok, why = False, f"pytest: timed out after {test_timeout}s"
        if not ok:
            return False, f"after merging into {base}, {why}"

        # `ff_only`: the merge commit above already has `base`'s tip as its first
        # parent. `-d` is safe afterwards — a real merge makes `branch` an ancestor.
        landed, why = landing.land(root, card, branch=branch, base=base, plan=plan,
                                   remote=remote, ref=merged_sha, ff_only=True)
        if not landed:
            return False, (f"verified the merge but could not fast-forward {base} onto "
                           f"it: {why}")
        return True, ("resolved as a plain merge — a rebase-based landing did not "
                      "settle on bookkeeping-only paths")
    finally:
        git.run(root, "worktree", "remove", "--force", str(tree))
        git.run(root, "worktree", "prune")


def _fold_instruction(root: Path, card: board.Card) -> str:
    """The paragraph telling a worker to write a memory *fragment*, or `""`.

    Built from the project's own `[[memory.fold]]` rows rather than written into the
    prompt as prose, so the keys a worker is told to use and the keys the fold reads
    cannot drift — which is the failure this whole mechanism would otherwise just
    move rather than remove. Empty for a project that declares no targets, so its
    prompt is byte-identical to before.

    `nightshift.hooks.fold_fence` refuses the direct edit; this is the half that says
    what to do instead. Both are needed: a fence that only refuses teaches nothing,
    and an instruction that only asks is one a worker can forget.
    """
    declared = memoryfold.targets(root)
    if not declared:
        return ""
    rows = "\n".join(f"  `## {t.key}` -> {t.path}" for t in declared)
    fragment = (memoryfold.fragment_path(root, card.id)
                .relative_to(root).as_posix())
    return f"""
**Your memory record goes in a fragment, not in the shared log.** Write it to:
  {fragment}
as markdown with one section per target:
{rows}
Omit a section you have nothing for. The runner folds this into those files after your \
branch merges, one card at a time — which is the point: every card appends at the same \
anchor in them, so editing them on your branch conflicts with any sibling card that \
finishes tonight. Editing them directly is refused by a hook.
"""


def branch_has_commits(root: Path, base: str, branch: str) -> bool:
    """Does `branch` carry anything `base` does not — i.e. is there a diff to review?

    The negative answer covers three states that all mean "there is nothing here
    for a diff reviewer": an artefact-only card (`art`, whose deliverable is a
    file a human looks at rather than a commit), a branch that was never cut, and
    a branch already merged. All three are the same fact to every caller, which is
    why they share one predicate rather than each testing `rev-list` for itself —
    `drain` asks it about a card at rest in `review/` for exactly the reason
    `review_stage` asks it about a card passing through.
    """
    count = git.run(root, "rev-list", "--count", f"{base}..{branch}").stdout.strip()
    return count not in ("", "0")


def review_stage(root: Path, card: board.Card, result: outcome.Dispatch, base: str,
                 card_budget: float, timeout: int) -> outcome.Dispatch:
    """Run the diff reviewer on a card whose gates+tests just passed, and route.

    Called for a `review` outcome only. Returns a new `Dispatch`:

    * `needs_decision` — the reviewer flagged a choice for Karel; settle files it
      to needs-decision/ with the reviewer's question.
    * `needs_fix` — the reviewer found a concrete, verifiable defect with one
      correct answer; settle sends the card back to tasks/ for another attempt
      carrying the finding, bounded by the same attempt_limit as an ordinary
      `failed` retry.
    * `reviewed` — the reviewer said `ok`; settle merges and lands it in testing/.
    * `pick` — the reviewer said `ok` (or there was no diff to show it) *and* the
      attempt left candidates that nobody installed; settle merges whatever diff
      there is and files the card in needs-decision/ asking which candidate to
      adopt. `unadopted_artefacts` carries the reasoning.
    * `review` (unchanged from the input) — **a review is still owed and can
      still be had.** Either the card is artefact-only (art: no commit to review,
      and Karel is its reviewer) or the window closed before a verdict was
      written. `review/` is the lane for exactly this and nothing else.
    * `unreviewable` — **the review will not happen by itself.** No CLI, no tier
      binding, a worktree that would not cut, a timeout, or a reviewer that ran
      and produced nothing readable. Settle files it to `blocked/` carrying the
      command that unsticks it.

    **The split between those last two is the point** (2026-08-25). Every
    degradation used to return `review`, each with a log line saying "leaving it
    in review/ for a manual look" — so the lane meant both *"queued for Claude"*
    and *"Claude could not, a human must"*, and a card in the second state was
    indistinguishable from one merely waiting its turn. Karel, on finding one:
    *"we again got into 'human needs to resolve it' being in review — that is not
    what review is for."* That is the same complaint `blocked/` was created for on
    2026-08-23, arriving by a different road, and the fourth time it has been
    raised. Degrading to "a human looks" is still always safe; what was not safe
    was degrading into a lane that does not say so.

    The returned outcome's `cost_usd` carries the dispatch cost forward plus the
    reviewer's own, so the run loop accounts for it with one `spent +=`.
    """
    branch = branches.work_branch(card.id, card.fields.get("branch", ""))

    # The window is already known to be closed: an earlier stage walled and had
    # its verdict honoured (`wall-on-review-wrapup-discards-a-verdict`), so the
    # card reached here on its merits but the plan has run out. Spawning the
    # reviewer now would call into a window already proven shut, burn a
    # subprocess and degrade to review/ anyway — so degrade straight away, with
    # the wall still on the returned Dispatch so the night stops or sleeps.
    if result.wall is not None:
        hostconfig._log(f"    a usage limit already closed this window — not spawning "
             f"{hostconfig.REVIEWER_AGENT} for {card.id}; its review is still owed")
        return result

    # Nothing to review as a diff: an artefact-only card has no commit on its
    # branch, and its human gate is the maintainer, not a diff reviewer. A dead
    # branch lands here too.
    #
    # Which lane that gate sits in is the one thing that changed on 2026-09-08. An
    # attempt whose candidates are all still uninstalled owes a *pick*, and
    # `needs-decision/` is the lane that says so; `review/` means "queued for
    # Claude", and filing a human's decision there is the complaint `blocked/` was
    # created for, arriving by a fifth road. An artefact-only card with nothing
    # unadopted — everything it made is already installed — has no question left
    # and keeps the old behaviour.
    if not branch_has_commits(root, base, branch):
        if result.unadopted:
            return outcome.Dispatch("pick", result.detail, result.cost_usd, result.rounds,
                            result.wall, unadopted=result.unadopted)
        return result

    try:
        model = tiers.resolve(root, "lead")
    except tiers.TierError as exc:
        # Not a wall and not a card defect — this host cannot resolve the lead
        # tier at all, so no amount of waiting produces a verdict.
        return _unreviewable(card, f"the `lead` tier does not resolve on this host: {exc}",
                             result)

    out_dir = telemetry.run_dir(root, card, card.attempts)
    hostconfig._status(root, phase="review", card=card.id, branch=branch, model=model, since=hostconfig._now())
    hostconfig._log(f"    reviewing {card.id} → {hostconfig.REVIEWER_AGENT} @ {model}")
    criteria, intent = card_review_context(card)

    # A `needs_fix` continuation (`review_fix` in the handover, `prepare_worktree`'s
    # FROM_REVIEW) means this exact branch was already reviewed once, and the only
    # thing that changed since is the fix. Reusing that point as the diff base
    # (`review-reviews-only-the-fix`) is what keeps a re-review from re-deriving
    # everything it already approved, at lead-tier prices, every single round. The
    # ancestor check is the safety net: a card that instead cold-started (its
    # `needs_fix` retry ran out its attempts down the ordinary path, or the branch
    # was otherwise rebuilt) no longer contains that commit, and reusing it then
    # would silently hide the rebuilt diff rather than fail loud — so it falls back
    # to a full review exactly as before.
    #
    # `prior_finding` is deliberately **not** gated on that ancestor check
    # (`review-anchor-survives-the-replay`). The two answer different questions:
    # `since` is "may the diff be narrowed", which needs the anchor to be provably
    # reachable; the finding is "what did you object to last round", which is true
    # whatever the branch did in between. Gating them together meant a fix round
    # whose anchor was gone got neither — and a reviewer with neither cannot even
    # tell it is a fix round, so it re-asks questions a previous round already
    # declined and re-verifies what one already confirmed. Measured on
    # `triage-findings-have-a-shelf-life` (2026-09-16): round 4's reviewer
    # reconstructed the prior finding out of `git log` and `git show`, spending
    # turns to rediscover a string the runner was holding in this very file.
    handover = worktree.read_handover(root, card.id)
    since = ""
    prior_finding = handover.review_finding if handover.review_fix else ""
    if (handover.review_fix and handover.reviewed_sha
            and worktree._is_ancestor(root, handover.reviewed_sha, branch)):
        since = handover.reviewed_sha

    verdict, cost, wall = review_branch(root, card.id, out_dir, model, base, branch,
                                        card_budget, timeout * 3,
                                        criteria=criteria, intent=intent,
                                        since=since, prior_finding=prior_finding,
                                        repaired=result.repaired,
                                        effort=tiers.effort(root, "lead"))
    total = result.cost_usd + cost

    # The artefact before the process's exit. A reviewer's *entire* output is its
    # verdict file, so one that says `ok` or `needs_decision` means the review
    # genuinely finished and the wall landed on the wrap-up call after it — the
    # measured shape of `wall-on-review-wrapup-discards-a-verdict`. It routes on
    # that verdict; an incomplete or missing one still degrades to review/ exactly
    # as before. Either way the wall rides home on the returned Dispatch, because
    # the plan running out is a fact about the *night* whatever this card did.
    if wall is not None and not telemetry.verdict_survives_a_wall(telemetry.REVIEWER_STAGE, verdict):
        # The plan ran out mid-review with nothing usable written. Gates+tests
        # already passed, so this is not the card's fault and the review is still
        # perfectly obtainable — it just needs a window. `review/`, genuinely
        # meaning "a review is owed", and the end-of-night drain (or the next
        # one) is what comes back for it.
        hostconfig._log(f"    review hit a usage limit — {card.id}'s review is still owed")
        return outcome.Dispatch("review", result.detail, total, result.rounds, wall,
                        how_to_test=result.how_to_test)
    if wall is not None:
        hostconfig._log(f"    {hostconfig.REVIEWER_AGENT} walled on its wrap-up after writing a complete verdict "
             f"— honouring it; the night's window is still closed")

    called = str(verdict.get("verdict", "")).lower()
    hostconfig._log(f"    {hostconfig.REVIEWER_AGENT}: {called or '(no verdict)'} — "
         f"{str(verdict.get('notes', ''))[:80]}")
    if called == "needs_fix":
        finding = str(verdict.get("finding", "")).strip()
        return outcome.Dispatch("needs_fix", finding or
                        "The reviewer flagged a fixable defect but recorded no finding — "
                        "treat that as itself needing a look; the diff and the review are "
                        f"in `.ai/runs/{card.id}/attempt-{card.attempts}/`.",
                        total, result.rounds, wall)
    if called == "needs_decision":
        question = str(verdict.get("question", "")).strip()
        return outcome.Dispatch("needs_decision", question or
                        "The reviewer flagged this for your decision but stated no question "
                        "— treat that as itself needing a look; the diff and the review are "
                        f"in `.ai/runs/{card.id}/attempt-{card.attempts}/`.",
                        total, result.rounds, wall)
    if called == "ok":
        # `ok` on the diff is not the same claim as "this card is finished" when the
        # attempt's real output never entered the diff. A reviewer looking at
        # `stun-grenade-visuals` said `ok` and was right to — the tooling commit was
        # sound — and reasoned in its own notes that "`verify: play` means he sees it
        # at testing/", which is exactly the inference `unadopted_artefacts` now
        # makes for it instead of leaving to whoever reads the card next.
        return outcome.Dispatch("pick" if result.unadopted else "reviewed",
                        str(verdict.get("notes", "reviewed ok"))[:300],
                        total, result.rounds, wall, how_to_test=result.how_to_test,
                        unadopted=result.unadopted)
    # The reviewer ran to completion and wrote nothing this can route on — no
    # verdict file, an unparseable one, a verdict naming none of the three words,
    # a timeout, a worktree that would not cut, or no CLI on this host at all
    # (`review_branch` returns an empty verdict for every one of those). None of
    # them gets better by waiting, so this is not `review/`: re-running the same
    # reviewer would produce the same nothing.
    return _unreviewable(
        card, "the reviewer produced no usable verdict — no verdict file, an "
        "unreadable one, or one naming none of `ok` / `needs_fix` / "
        "`needs_decision`", result, cost=total, wall=wall)


def _unreviewable(card: board.Card, why: str, result: outcome.Dispatch, *,
                  cost: float | None = None,
                  wall: limits.Wall | None = None) -> outcome.Dispatch:
    """A review that will not happen by itself, on a card that is otherwise done.

    Kept to one helper so every such path phrases it the same way and lands in
    the same lane — the old code had five sites each degrading to `review/` with
    their own wording, which is how two different states ended up sharing a lane
    name for four months.
    """
    hostconfig._log(f"    {card.id} cannot be reviewed here — {why}; → {board.BLOCKED_LANE}/")
    return outcome.Dispatch("unreviewable", why,
                    result.cost_usd if cost is None else cost,
                    result.rounds, wall, how_to_test=result.how_to_test)
