"""Settle: what a `Dispatch` outcome does to the board.

`settle`/`_settle_impl` are the last stage of a dispatch -- given the outcome
`dispatch.dispatch()` and `review.review_stage()` produced, move the card to
its lane, write its `## Error`/`## Summary`/`## Question` sections, prune the
worktree and rescue branches, and commit the board. Downstream of every other
split module; nothing imports this one back.
"""
from __future__ import annotations

import socket
from pathlib import Path

from nightshift import board, branches, decide, git, landing, textio
from nightshift import dispatch, hostconfig, outcome, review, stale, telemetry, worktree

# Settle — what the outcome does to the board
# --------------------------------------------------------------------------

# The exemption that lets machine-written evidence live in a card.
#
# **Belt and braces since 2026-08-01, not load-bearing.** `Board/` is no longer a
# `doc_scan.DOC_ROOTS` entry at all, so no lane is read by `doc_reference_liveness`
# and a pytest excerpt cannot trip it wherever the card sits.
#
# It was load-bearing, and the reason is worth keeping because it is what finally
# argued the scope change: every line of a retrying card's `## Error` used to be read
# by that gate, which asserts each path and backticked symbol still exists. A pytest
# excerpt is full of both, and the ones most likely to dangle are the interesting
# ones — a test file the failed attempt created on a branch that was then discarded.
# Unexempted, one card's failure became a repo-wide violation and the next card in
# the night was filed failed for someone else's bug: the 2026-07-23 shape
# `GATE_CRASH` exists to prevent. Needing this marker at all was the evidence that
# the gate's scope was wrong rather than the cards.
#
# Kept rather than deleted: it costs one comment line in a failed card, it states a
# true thing about the block ("quoted evidence is not a claim"), and it keeps the
# card honest if `Board/` is ever put back in scope. A marker covers from its own
# line to the next `##` heading (`doc_scan.exempt_lines`), which is precisely the
# rest of this section, and the reason is written out because
# `test_every_exemption_carries_a_written_reason` requires one.
_EVIDENCE_EXEMPTION = (
    "<!-- stale-ok: pytest and the gates wrote the block below while this attempt "
    "was failing. It is quoted evidence about that attempt, not a claim about the "
    "current tree, so liveness must not judge the paths and symbols in it. -->"
)


def _error_section(card_id: str, attempt: int, result: outcome.Dispatch, *,
                   retiring: bool) -> str:
    """The `## Error` body: what failed, the evidence, and where the rest of it is.

    Written to be readable from a machine that did not run the card, which is the
    thing the previous version could not do. It said `Full output:
    .ai/runs/<id>/attempt-N/` and nothing else, and that pointer is empty on every
    host but the one that ran the night (`.ai/runs/` is gitignored). Karel went
    looking for a failure reason on 2026-07-31 and there was nowhere it could have
    been.

    `retiring` is whether this attempt is the card's last. It used to also mean
    "and the directory is gone" — `prune_run_dir` ran two lines after this text
    was written — but as of `resume-open-inline` (2026-08-28) a card retired via
    the attempt limit keeps its run dir specifically so it stays there: every
    attempt that led here did real work, so it is what Talk/`Open inline` resume
    from when the maintainer reopens it. Only the *other* retiring path — the
    no-progress breaker in `settle` — still prunes immediately, and writes its
    own `## Error` text directly rather than through this function, precisely
    because that one *is* gone the moment it is written.
    """
    head = f"Attempt {attempt} failed — {stale._quote_safe(result.detail)}"
    parts = [head]
    if result.evidence:
        parts += [_EVIDENCE_EXEMPTION, result.evidence]
    # Naming the host is the other half of the fix. The pointer is only meaningful
    # on one machine, so the text says which — `.ai/hosts.json` has two, and "the
    # logs are not here" reads as "the logs are gone" without it.
    where = f"`.ai/runs/{card_id}/attempt-{attempt}/` on host `{socket.gethostname()}`"
    if retiring:
        parts.append(
            f"The full log is at {where} — kept, not pruned, because this card can "
            f"still be reopened: the Command Center's Talk/`Open inline` resume from "
            f"exactly this directory. `.ai/runs/` is gitignored, so it is only on "
            f"that host; it is finally pruned once this card reaches `done/`.")
    else:
        parts.append(
            f"Full output: {where} — gitignored, so it is only on that machine, and "
            f"it is deleted if this card is later filed as stuck (no working-tree "
            f"progress across {hostconfig.NO_PROGRESS_STOP} resumes); reaching {hostconfig.MAX_ATTEMPTS} "
            f"attempts and retiring to `failed/` no longer deletes it. This attempt's "
            f"commits are not lost either way: they stay on `ai/{card_id}` and, once "
            f"this card is dispatched again, are preserved as a rescue branch "
            f"(`ai/{card_id}@failed-N` — `prune_rescue_branches`) rather than deleted at "
            f"the next cold start, until the card leaves `tasks/` for good.")
    return "\n\n".join(parts)


def _review_finding_section(card_id: str, finding: str) -> str:
    """The `## Review Finding` body written on a `needs_fix` verdict.

    Deliberately not `## Error` — the gates and the full test suite had already
    passed; nothing crashed. And not `## Question` — nothing here needs the
    maintainer's judgment, only a fix. The next attempt is the reader: `finding`
    is the reviewer's own account of the defect and its correct answer, precise
    enough (by the reviewer's prompt) to apply without re-deriving it.

    The closing paragraph used to promise the opposite of what happened. It said
    the commits were "preserved as a rescue branch" and told the worker to "apply
    this fix directly" — while `prepare_worktree` renamed the branch away and cut
    a fresh tree from `base`, so there was nothing to apply the fix *to*. The
    branch is now checked out for the next attempt (FROM_REVIEW), which is what
    makes this paragraph true rather than merely encouraging.
    """
    return "\n\n".join([
        "The reviewer found a fixable defect — not a judgment call — in a diff whose "
        "gates and tests had already passed:",
        finding,
        f"The rest of this card is **already implemented and already green** on "
        f"`ai/{card_id}`, and the next attempt starts from that branch with those "
        f"commits checked out. Apply this finding on top of them and stop: it does not "
        f"need re-deriving, and the card does not need re-implementing.",
    ])


def _card_text_on_branch(root: Path, branch: str, relpath: str) -> str | None:
    """The card file as the worker last committed it on its own branch, or
    `None` if it cannot be read (branch gone, path renamed, not a git
    failure the caller should surface — settle() falls back to the
    pre-dispatch text it already has).

    Exists because the worktree is already dropped by the time `settle()`
    runs (drop_worktree happens before this is called), so the worker's
    committed content is reachable only through git, not the filesystem.
    """
    done = git.run(root, "show", f"{branch}:{relpath}")
    return done.stdout if done.returncode == 0 else None


def _park_for_pick(root: Path, card: board.Card, result: outcome.Dispatch,
                   *, merged_from: str = "", integration: str = "") -> str:
    """File a card whose candidates are waiting to be chosen between, and say so.

    The `pick` half of `settle`'s landing (`unadopted_artefacts`): the attempt is
    over and whatever diff it had is already on the integration branch, but its
    real output is a set of candidates in the run directory that nobody has
    adopted. That is a question, so the card goes to `needs-decision/` carrying
    one — and `after_answer: tasks`, because answering it does not reshape the
    card, it releases the second pass that installs the pick.

    The question is written in the shape `decide.parse` reads, so the panel's
    answer form offers the options rather than only quoting the prose around
    them.
    """
    _write_pick_question(card, result)
    board.move(root, card, "needs-decision")
    return _pick_message(card, result, merged_from=merged_from, integration=integration)


def _pick_message(card: board.Card, result: outcome.Dispatch, *, merged_from: str = "",
                  integration: str = "") -> str:
    landed = (f", rebased {merged_from} onto {integration} and merged"
              if merged_from else ", nothing to merge")
    return (f"{card.id}: → needs-decision/ ({result.unadopted} candidate(s) produced, none "
            f"installed{landed})")


def _write_pick_question(card: board.Card, result: outcome.Dispatch) -> None:
    """The pick question `_park_for_pick` files — without the move, for a card
    `landing.land` moves once its diff has merged."""
    attempt = card.attempts
    count = result.unadopted
    card.write_section(
        "Question",
        f"**{count} candidate(s) were produced and none of them installed**, so what "
        f"this card is waiting on is a choice, not a play-through — there is nothing "
        f"in the program yet to exercise.\n\n"
        f"They are in `{(hostconfig.RUNS / card.id / f'attempt-{attempt}' / 'artefacts').as_posix()}`, "
        f"and this card's own page in the Command Center shows them side by side with "
        f"whatever the `{card.checker or 'checker'}` said about each. Picking one there "
        f"records it as the answer to this question.\n\n"
        f"Answering sends the card back to `tasks/` for a second pass that installs the "
        f"pick and wires it up; it reaches `testing/` after that, once there is "
        f"something on screen to look at.\n\n"
        f"- Adopt one of the candidates — name it, or pick it in the Command Center\n"
        f"- None of them are right — re-roll, and say what to change\n")
    card.write({"after_answer": board.AFTER_ANSWER_TASKS})
    # Same reopening as the `parked` and `needs_decision` paths — see `decide.reopen`.
    card.text = decide.reopen(card.text)
    textio.write_text_lf(card.path, card.text)


def settle(root: Path, card_id: str, result: outcome.Dispatch) -> str:
    """Apply one dispatch's outcome, then reap rescue branches if the card just
    left `tasks/` for good.

    A thin wrapper around `_settle_impl` rather than a call inside it: that
    function already has a dozen outcome branches, several returning early
    straight after their own `board.move(...)`, so a call sprinkled into each
    one is exactly the shape that gets a new branch added later and misses it
    — which is how `rescue-branches-only-swept-on-failure` (2026-08-13)
    happened in the first place (only the two `failed/`-retirement branches
    called `prune_rescue_branches`; the far more common success-into-`testing/`
    path never did, and neither did park-into-`needs-decision/` or
    bounce-into-`review/`). Checking the card's lane once, after the real
    work, covers every outcome uniformly and cannot be missed by a future one.
    A card still in `tasks/` (retried, or vanished) is left alone — its rescue
    branches are still what a future retry recovers from."""
    message = _settle_impl(root, card_id, result)
    card = board.find(root, card_id)
    if card is not None and card.lane != "tasks":
        worktree.prune_rescue_branches(root, card_id)
    return message


def _settle_impl(root: Path, card_id: str, result: outcome.Dispatch) -> str:
    """Apply one dispatch's outcome. Rescans first: the card may have moved."""
    card = board.find(root, card_id)
    if card is None:
        return f"{card_id}: vanished from the board — nothing settled"

    # Before the outcome branches, so the numbers land on the card whatever
    # happened. A *failed* attempt's telemetry is the most useful kind: it is
    # what distinguishes "ran 40 minutes and hit something hard" from "died in
    # four turns", and that is exactly the attempt nobody has other evidence for.
    reading = telemetry.read_telemetry(telemetry.run_dir(root, card, card.attempts))
    if reading:
        card.write_section("Telemetry", telemetry.telemetry_markdown(reading, card.attempts))

    if result.outcome == "limited" and result.stuck:
        # The no-progress breaker (runner-worker-handover Decision 2). A warm
        # resume that leaves the working tree byte-identical for NO_PROGRESS_STOP
        # windows is not the plan running out — it is a per-card failure a wall
        # phrase matched by mistake, or a resume that never persists progress.
        # Giving the attempt back again would loop it at the head of the queue
        # forever, so it is filed to failed/ instead. Its last state is banked on
        # the branch first, for the post-mortem.
        tree = worktree.worktree_root(root) / card_id
        if tree.exists():
            worktree.commit_wip(root, tree, card_id)
            worktree.drop_worktree(root, tree)          # also clears the handover
        else:
            worktree.clear_handover(root, card_id)
        card.write({"started": None, "finished": hostconfig._now()})
        card.write_section(
            "Error",
            f"Attempt {card.attempts} was interrupted by a usage limit "
            f"{hostconfig.NO_PROGRESS_STOP} times with no change to the working tree between them, so "
            f"it was filed as stuck rather than resumed forever "
            f"(runner-worker-handover Decision 2). This usually means a per-card failure a "
            f"wall phrase matched by mistake, or a resume that never persists progress. "
            f"The log was `.ai/runs/{card_id}/` on host `{socket.gethostname()}` and was "
            f"deleted when this card was retired (`prune_run_dir`); it was gitignored, so "
            f"it never left that machine either.")
        board.move(root, card, "failed")
        worktree.prune_run_dir(root, card_id)
        worktree.prune_rescue_branches(root, card_id)
        return (f"{card_id}: → failed/ (no working-tree progress across "
                f"{hostconfig.NO_PROGRESS_STOP} resumes)")

    if result.outcome == "limited" and result.kept and not result.progressed:
        # Under the breaker but not moving: spend the attempt (do NOT rewind) and
        # keep the warm state for one more resume. Spending is the give-back's
        # backstop — even if the breaker never trips, attempts drain toward the
        # limit rather than the card being retried for free forever.
        card.write({"started": None, "finished": hostconfig._now()})
        board.commit_board(
            root, f"board: {card_id} resumed with no progress — attempt {card.attempts} spent")
        return (f"{card_id}: resumed but the working tree did not move — attempt "
                f"{card.attempts} spent, worktree kept for one more resume")

    if result.outcome in ("limited", "blocked", "interrupted"):
        # The outcomes that give the attempt back, because none of them is a
        # fact about the card. `attempts` is committed before the worker starts
        # so a crash cannot retry forever, and that is right for every other
        # exit — but charging a card for the plan running out, this machine's
        # gate harness being broken, or a socket dropping mid-response would
        # mean one bad night quietly pushed every card it touched closer to
        # `failed/`.
        back = card.attempts - 1
        rewind: dict[str, str | None] = {
            "attempts": str(back) if back > 0 else None,
            "started": None, "finished": None,
        }
        # A branch is dropped only in the empty first-call case — a wall at the
        # very first API call, with nothing done and no worktree kept, where the
        # branch records nothing. When the worktree is kept for a warm resume, its
        # branch is preserved state (commits, and any `wip:` commit), and
        # `blocked`/`interrupted` ran and committed — so neither drops it.
        if back <= 0 and result.outcome == "limited" and not result.kept:
            rewind["branch"] = None
        card.write(rewind)
        # Three different facts can all give the attempt back, and the morning
        # log must say which: a crashed harness is this machine's problem, repo
        # drift is a gate a *different* card (or a commit already on `base`)
        # made stale, an interruption is a dropped connection that never
        # reached verification — all three stop nothing being judged unfairly,
        # but only naming the right one saves a hunt
        # (failed-attempt-work-is-deleted-not-resumed).
        why = ("usage limit" if result.outcome == "limited"
              else "worker interrupted (api_error)" if result.outcome == "interrupted"
              else "repo drift" if result.repo_drift
              else "gate harness crashed")
        kept = " — worktree kept for warm resume" if result.kept else ""
        # Repo drift specifically: the branch this dispatch judged is real work
        # that a repo-wide fact, not this diff, made unjudgeable. It stays on
        # `ai/<card_id>` and is preserved as a rescue branch at the next cold
        # start, same as an ordinary `failed` attempt's commits.
        rescued = (f" — its commits stay on `ai/{card_id}` and will be preserved as a "
                  f"rescue branch (`ai/{card_id}@failed-N`) once this card is "
                  f"dispatched again" if result.repo_drift else "")
        # Repo drift that survived a repair goes to `blocked/`, never back to
        # `tasks/` (Karel, 2026-09-04: *"it either can be finished (and should be in
        # the same run) or can't (and should stay in blocked)"*). Leaving it in
        # `tasks/` said two contradictory things at once — the card advertised
        # itself as ready to pick up, while the only process that could pick it up
        # had just stopped over the very thing blocking it. The attempt is still
        # given back; `blocked/` is about what the board claims, not about blame.
        if result.repo_drift:
            card.write_section(
                "Blocked",
                f"Not attempted — the gates were already red on `{hostconfig.default_base(root)}` for a "
                f"reason outside this card's diff, and the repair could not clear "
                f"it:\n\n{result.detail}\n\nThe attempt was given back, so this "
                f"costs the card nothing. Fix the drift on the integration branch "
                f"— `python -m nightshift.gates.run` names it — then move this card "
                f"back to `tasks/`.")
            board.move(root, card, board.BLOCKED_LANE)
            return (f"{card_id}: → {board.BLOCKED_LANE}/ (not attempted, attempt given "
                    f"back — {result.detail}{kept}{rescued})")
        board.commit_board(root, f"board: {card_id} not attempted — {why}")
        return f"{card_id}: not attempted, attempt given back — {result.detail}{kept}{rescued}"

    card.write({"started": None, "finished": hostconfig._now()})

    if result.outcome == "review":
        # The worker's own one/two-sentence account of what it did, persisted onto
        # the card rather than only living in this return string — so a card that
        # reaches testing/ carries a brief "what happened" a human can read without
        # opening `.ai/runs/` or the verbose `## Thread` prose (menu-summary-on-card).
        card.write_section("Summary", result.detail)
        board.move(root, card, "review", review_owed=(
            "reached only via `review_stage`'s own `Dispatch(\"review\")`, which "
            "asserted a review is owed and obtainable: the window is open, a diff "
            "exists, and no verdict landed yet (or the reviewer is the maintainer)"))
        return f"{card_id}: → review/ ({result.detail})"

    if result.outcome == "parked":
        # The worker prompt tells it to write `## Question` directly onto the
        # card (§13). `result.detail` is only the verdict's `summary` field —
        # a different, shorter field meant for `## Summary` — truncated to
        # 300 chars. Only fall back to it when the worker genuinely left no
        # question; otherwise this clobbers a real, carefully-written
        # question with a mid-sentence-truncated attempt summary.
        #
        # What "genuinely left no question" means is read from the WORKER'S
        # branch, never from `card.text` alone (the pre-dispatch snapshot this
        # process already had in memory) — a presence check on `card.text`
        # cannot distinguish "never answered" from "answered already, and the
        # worker wrote a NEW question on top of that history", so a card whose
        # `## Question` already had content (fully answered, sent to tasks/,
        # then redispatched and parked again) silently discarded everything
        # the worker actually wrote (parked-settle-trusts-stale-question-over-
        # worker-branch, second occurrence 2026-08-12, cross-language-text-
        # overflow-guard attempt 5 — found by hand again because the symptom
        # is invisible from the card alone: `attempts`/`finished` update, but
        # `## Question` just doesn't move).
        branch = branches.work_branch(card_id, card.fields.get("branch", ""))
        relpath = str(card.path.relative_to(root)).replace("\\", "/")
        branch_text = _card_text_on_branch(root, branch, relpath)
        branch_question = board.section(branch_text, "Question") if branch_text else ""
        pre_dispatch_question = board.section(card.text, "Question")
        if branch_question and branch_question != pre_dispatch_question:
            # Prepend rather than replace — `write_section` overwrites the
            # whole section, and the pre-dispatch content is real history
            # (prior pickers, prior parks), not a stale placeholder to drop.
            combined = (branch_question if not pre_dispatch_question
                       else f"{branch_question}\n\n---\n\n{pre_dispatch_question}")
            card.write_section("Question", combined)
        elif not pre_dispatch_question:
            card.write_section("Question", result.detail or
                               "The worker parked this card but recorded no question — "
                               "that is itself a defect; see `.ai/runs/` for the attempt.")
        elif not decide.has_live_picker(card.text):
            # The worker parked, wrote no new question, and the only question on the
            # card has already been answered and retired (`decide.mark_decided`). That
            # is the re-park loop with its false signal removed: the card would go to
            # `needs-decision/` asking nothing, and the maintainer would open it to
            # find their own answer looking back at them.
            #
            # Not converted to a failure — the attempt may have found something real
            # and merely failed to write it as a picker — but it must not reach the
            # board looking like an ordinary park, because the two need opposite
            # responses: one wants an answer, this one wants the run record read.
            card.write_section("Question", (
                f"**This park asked nothing.** The worker parked without writing a new "
                f"question, and every `### Decide:` on this card was already answered "
                f"and retired before it was dispatched — so there is no decision here "
                f"waiting on you.\n\nThat is a defect in the attempt, not a question: "
                f"either it re-asked something already settled, or it had a finding and "
                f"did not write it in the `### Decide:` shape. Read "
                f"`.ai/runs/{card_id}/` before answering anything, and send the card "
                f"back to `tasks/` rather than adding a second answer to the "
                f"first.\n\n---\n\n{pre_dispatch_question}"))
        # This card was dispatchable when the night picked it up, so its `## Approach`
        # and `## Acceptance` still describe the work — the answer settles one point
        # inside a scoped card rather than scoping it. That is `after_answer: tasks`,
        # and declaring it is what lets the panel offer the resume as one click
        # (`board.AFTER_ANSWER`).
        card.write({"after_answer": board.AFTER_ANSWER_TASKS})
        # `## Open questions` was settled (or simply born `none`, the common case
        # for a card that was dispatchable when the night picked it up) and nothing
        # else reopens it — `decide.reopen` also boundary-marks `## Thread` so a
        # prior round's answer, kept as history, is never read as answering the
        # question just parked (`enemy-position-knowledge`, 2026-09-05). Written to
        # disk directly (not `write_section`, which only knows one section at a
        # time) — `board.move` reloads the card from disk next, so an in-memory-only
        # change here would be silently discarded.
        card.text = decide.reopen(card.text)
        textio.write_text_lf(card.path, card.text)
        board.move(root, card, "needs-decision")
        return f"{card_id}: → needs-decision/ (parked)"

    if result.outcome == "needs_fix":
        # A concrete, verifiable defect — not a decision. Same principle drain.py
        # already states for a card that merely could not be reviewed ("'review
        # this diff' is not a decision — it is work"), one step further: applying
        # a fix with one correct answer is not a decision either. It goes back to
        # tasks/ for another attempt, bounded by the same attempt_limit as an
        # ordinary `failed` retry — never straight to needs-decision/, which
        # would spend Karel's judgment on something the next attempt can just do.
        finding = result.detail or "The reviewer flagged a fixable defect but recorded none."
        if card.attempts >= dispatch.attempt_limit(card):
            # The same class of "fixable" defect survived every attempt this
            # card gets — that is no longer mechanical, whatever the reviewer
            # called it. Escalate rather than retry forever.
            card.write_section(
                "Question",
                f"The reviewer found a fixable-looking defect across all {card.attempts} "
                f"attempts this card gets, and it kept recurring rather than getting fixed "
                f"— something about it is not as mechanical as it looked, or a later attempt "
                f"reintroduced it. Most recent finding:\n\n{finding}")
            # Same reasoning as the parked path: a card that has been dispatched this
            # many times is scoped, and what it needs is a decision inside that scope.
            card.write({"after_answer": board.AFTER_ANSWER_TASKS})
            # Same reopening as the `parked` path just above, and for the same
            # reason: this card may already have been through needs-decision/ once
            # (a fix that "recurred across all attempts" implies at least one prior
            # retry cycle), so `## Open questions`/`## Thread` can carry a stale,
            # already-settled round that would otherwise read as answering this one.
            card.text = decide.reopen(card.text)
            textio.write_text_lf(card.path, card.text)
            board.move(root, card, "needs-decision")
            return (f"{card_id}: → needs-decision/ (a reviewer-flagged fix recurred across "
                    f"{card.attempts} attempts)")
        card.write_section("Review Finding", _review_finding_section(card_id, finding))
        # `board.dispatch_order`'s front bucket, so an unattended queue reaches
        # this card before anything Karel merely dragged higher — see the
        # constant retired above for why a clock is not what this needed.
        card.write({"last_outcome": "needs_fix"})
        # The next attempt continues from this branch instead of cold-starting —
        # `prepare_worktree`'s FROM_REVIEW path, and the reason the finding can
        # honestly say "apply this directly". Written after the section above so
        # a crash between them leaves the card readable rather than the flag
        # pointing at a finding nobody recorded. `dispatch` clears it by dropping
        # the worktree, so it survives exactly one attempt: if that attempt fails
        # outright, the ordinary cold-start retry takes over.
        branch = branches.work_branch(card_id, card.fields.get("branch", ""))
        if worktree._branch_exists(root, branch):
            # Captured now, before the next attempt adds a single commit on top —
            # this is the exact point the reviewer just diffed against, and the
            # only moment that sha is still the branch tip. `review_stage` uses it
            # to diff only the fix next time instead of the whole branch again
            # (`review-reviews-only-the-fix`).
            reviewed_sha = git.run(root, "rev-parse", branch).stdout.strip()
            worktree.write_handover(root, card_id, worktree.Handover(
                review_fix=True, reviewed_sha=reviewed_sha, review_finding=finding))
        if card.lane == "tasks":
            board.commit_board(
                root, f"board: {card_id} attempt {card.attempts} needs a fix, will retry")
        else:
            board.move(root, card, "tasks")
        return f"{card_id}: attempt {card.attempts} needs a fix, will retry ({finding[:80]})"

    # The three outcomes the review stage produces (automate-review-step,
    # reviewer-needs-fix-verdict). Where a `reviewed` one lands is the card's own
    # declaration, `verify:` — `play` for a card with a surface Karel can exercise,
    # `review` for one with none, where the reviewer's `ok` *is* the acceptance
    # (unplayable-cards-still-land-in-testing). Until 2026-08-06 every finished card
    # went to testing/ regardless, so eleven of the sixteen cards waiting there were
    # waiting on a verification he had no way to perform, and the five he should
    # actually play were buried among them. `needs_decision` still diverts to
    # needs-decision/ from either value; `needs_fix` is handled above it, back to
    # tasks/ rather than needs-decision/, because it never needed Karel at all.
    if result.outcome == "unreviewable":
        # Finished work whose review cannot be obtained here. `blocked/`, not
        # `review/`: there is an operation for a person, and `blocked/`'s whole
        # contract is "nothing is being asked of you; something is being
        # blocked". Leaving it in `review/` is what made finished work read as
        # queued Claude work — Karel's complaint, for the fourth time
        # (2026-08-25). Not `needs-decision/` either: nobody has a question, and
        # `drain.py` already states the principle — "'review this diff' is not a
        # decision, it is work".
        branch = branches.work_branch(card_id, card.fields.get("branch", ""))
        card.write_section(
            "Review",
            f"Gates and tests passed on `{branch}`, but the diff reviewer could not "
            f"be run to a verdict here: {result.detail}.\n\n"
            f"**Nothing is being asked of you.** Once the cause is fixed (the worker "
            f"CLI on this host, the `lead` tier binding in `.ai/manifest.toml`, or "
            f"whatever `.ai/runs/{card_id}/attempt-{card.attempts}/review.log` "
            f"reports), the review is one command:\n\n"
            f"    python -m nightshift.drain --card {card_id}\n\n"
            f"That routes it onward exactly as a dispatch would have. Reviewing the "
            f"diff yourself and moving the card by hand is the other way.")
        board.move(root, card, board.BLOCKED_LANE)
        return f"{card_id}: → {board.BLOCKED_LANE}/ (no review obtainable — {result.detail[:60]})"

    if result.outcome == "needs_decision":
        card.write_section("Question", result.detail or
                           "The reviewer flagged this for your decision but recorded no "
                           "question — see `.ai/runs/` for the diff and the review.")
        # There is a reviewed branch behind this card, so it is as scoped as a card
        # gets: the answer decides one point about work that already exists.
        card.write({"after_answer": board.AFTER_ANSWER_TASKS})
        # Same reopening as the `parked` path above — see `decide.reopen`.
        card.text = decide.reopen(card.text)
        textio.write_text_lf(card.path, card.text)
        board.move(root, card, "needs-decision")
        return f"{card_id}: → needs-decision/ (reviewer flagged a decision)"

    # `pick` rides the `reviewed` path deliberately: it is the same landing — the
    # same rebase, the same conflict escalation, the same branch deletion — and
    # only the final lane differs. Its diff merges *before* the card parks, which
    # is the whole reason it is not simply a `needs_decision`: a card that parks
    # with commits still on its branch is cold-started on the next attempt, and
    # `prepare_worktree` shunts that branch aside as a rescue ref rather than
    # continuing it. The tooling this attempt wrote would then be reachable but
    # unlanded, and the second pass would have to write it again.
    if result.outcome in ("reviewed", "pick"):
        branch = branches.work_branch(card_id, card.fields.get("branch", ""))
        integration = hostconfig.default_base(root)
        # The host's `publish_remote`, resolved here rather than inside
        # `rebase_and_merge`, for the same reason `integration` is: the merge
        # step stays a function of its arguments. Empty on a host that never
        # opted into pushing, and then nothing is deleted on any remote.
        remote = str(hostconfig.host_setting(root, "publish_remote", "")).strip()
        if result.outcome == "pick" and not review.branch_has_commits(root, integration, branch):
            # An artefact-only attempt: its entire output is the candidates, so
            # there is no diff to land and nothing for the merge machinery to do.
            return _park_for_pick(root, card, result)
        if result.outcome == "pick":
            plan = landing.Plan("needs-decision",
                                lambda landed: _write_pick_question(landed, result))
        else:
            lane = landing.finished_lane(root, card, branch, integration)

            def _how_to_test(landed: board.Card) -> None:
                # Written before the move, so the card carries its scenario into the
                # lane rather than arriving there bare.
                if landed.verify == "play":
                    landed.write_section("How to test", result.how_to_test or
                                         "The worker recorded no scenario — that is "
                                         "itself a defect on a `verify: play` card; the "
                                         f"diff is on `{branch}`.")
                elif lane == "testing" and result.how_to_test:
                    landed.write_section("How to test", result.how_to_test)

            plan = landing.Plan(lane, _how_to_test)
        merged, why = review.rebase_and_merge(root, card, branch, integration, remote=remote,
                                       plan=plan)
        if merged:
            if result.outcome == "pick":
                return _pick_message(card, result, merged_from=branch,
                                     integration=integration)
            return (f"{card_id}: → {plan.lane}/ (reviewed ok, rebased {branch} onto "
                    f"{integration} and merged)")
        # Reviewed ok but the branch will not rebase-and-merge, and `_resolve_conflict`
        # could not settle it either — it conflicts with what has landed on the
        # integration branch since (a sibling card, a same-night memory edit) in a way
        # the resolver declined or could not verify, or the replayed result no longer
        # passes. Never silently into testing/ as if it had merged, and never a guessed
        # resolution (§12).
        #
        # `blocked/`, not `review/` (2026-08-23). This card was reviewed `ok`; parking
        # it in the reviewer's lane made finished work read as outstanding Claude work
        # on the NOW page, which is exactly what Karel objected to. See
        # `board.BLOCKED_LANE` for why this is neither `needs-decision/` nor `failed/`.
        card.write_section(
            "Merge",
            f"Reviewed `ok`, but `{branch}` could not be rebased onto "
            f"`{integration}` and merged: {why}. The {hostconfig.RESOLVER_AGENT} could not settle "
            f"it either, so this one needs a person; then it can go to testing/. The "
            f"reviewed diff is in `.ai/runs/{card_id}/attempt-{card.attempts}/`.")
        board.move(root, card, board.BLOCKED_LANE)
        return (f"{card_id}: → {board.BLOCKED_LANE}/ (reviewed ok but {branch} will not "
                f"rebase-merge — {why})")

    retiring = card.attempts >= dispatch.attempt_limit(card)
    card.write_section("Error", _error_section(card_id, card.attempts, result,
                                              retiring=retiring))
    if retiring:
        board.move(root, card, "failed")
        # **Not `prune_run_dir` here.** Unlike the no-progress retirement above,
        # every attempt that led here did real, distinct work — review kept
        # sending it back with a concrete finding, not looping on a stuck resume
        # — so the last session is exactly what a maintainer picking this card
        # back up via Talk would want, not a cold re-read of the card. Left for
        # `sweep_terminal_cards` to judge instead, which now keeps it too
        # (Karel, 2026-08-28: "do both" — `failed/` should get the same
        # resume-durability `testing/` does, except for this retirement's own
        # no-progress sibling above, which prunes immediately on purpose).
        worktree.prune_rescue_branches(root, card_id)
        return f"{card_id}: → failed/ after {card.attempts} attempts ({result.detail})"
    # `board.dispatch_order`'s back bucket — this card gets another attempt, but
    # only after every card that has not just failed has had its turn.
    card.write({"last_outcome": "failed"})
    board.commit_board(root, f"board: {card_id} attempt {card.attempts} failed")
    return f"{card_id}: attempt {card.attempts} failed, will retry ({result.detail})"
