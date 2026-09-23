"""The staleness sweep phase: spend the tail of a run's budget checking
`stale_sweep.select()`'s ranked docs against current source, one `stale-hunter`
call per doc.

`run_stale_check` spawns the checker and parses its fenced-JSON verdict
(`_verdict_from_message`, shared with the review checker's own parsing via
`review`); `stale_phase` is the loop the runner calls, budget- and
wall-aware, that writes a fix-card per real finding and updates the ledger
`stale_sweep` reads next time.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from nightshift import board, limits, run_record, stale_sweep, textio
from nightshift import hostconfig, review, startup, telemetry, worker

_STALE_PROMPT = """\
Check exactly ONE document for staleness, at **tier: worker** (resolved to model `{model}`). \
Follow your charter: quote-or-drop, cite a real `file:line`, never edit, `unverifiable` is a \
success.

The document:
  {doc}
Its named source lives under the repo root you have been given; read the doc, then read the \
current source of the modules it names, and report only sentences that are no longer true.

Report your verdict as **the last thing in your final message**: one ```json fenced block, \
nothing after it. You are a read-only checker and have no Write tool — do not try to create a \
file, and do not describe the verdict in prose instead of emitting the block.
  {{"complete": true,
    "findings": [{{"claim": "<verbatim quote from the doc>", "cite": "<file:line in source>", \
"why": "<what the source actually says now>"}}],
    "summary": "<one line: how many real drifts, or 'no drift found'>"}}

`complete` must be `true` only if you finished reading the whole document. If you ran out of \
room, set it `false` — the runner will re-check this doc next time rather than record a \
verification that did not happen. An empty `findings` list with `complete: true` is the good \
outcome: the doc is accurate.
"""

def stale_run_dir(root: Path, doc_rel: str) -> Path:
    slug = doc_rel.replace("/", "__").replace(".md", "")
    path = root / hostconfig.RUNS / "_stale" / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_stale_check(root: Path, doc_rel: str, out_dir: Path, model: str,
                    card_budget: float, timeout: int) -> tuple[dict, float, "limits.Wall | None"]:
    """Dispatch `stale-hunter` on ONE doc. Returns (verdict, cost, wall).

    The runner owns this the way it owns the producer→checker loop (§16): the
    checker's context is built here, not by the thing being checked, and its tier
    goes through the dispatcher so a nested spawn cannot inherit the wrong model.
    The verdict is a lookup, never an interpretation — `{}` degrades to
    "not verified", which re-checks next time rather than recording a lie.

    The verdict arrives in the agent's **final message**, not a file:
    `stale-hunter` is read-only by charter and has no Write tool, so a file
    handover could never work (see `_verdict_from_message`). `verdict.json` is
    still honoured if present, so a future checker that does write one keeps
    working, but the message is the channel that actually carries it.
    """
    verdict_path = out_dir / "verdict.json"
    prompt = _STALE_PROMPT.format(model=model, doc=doc_rel,
                                  verdict_path=verdict_path.resolve().as_posix())
    textio.write_text_lf(out_dir / "prompt.md", prompt)

    binary = startup.claude_binary()
    if not binary:
        return {}, 0.0, None
    argv = [
        binary, "-p",
        "--agent", "stale-hunter",
        "--model", model,
        *worker._STREAM_ARGV,
        *worker._budget_argv(card_budget),
        *worker._STRICT_MCP_ARGV,
        "--permission-mode", str(hostconfig.host_setting(root, "permission_mode", "acceptEdits")),
        "--add-dir", str(root.resolve()),
    ]
    cost = 0.0
    try:
        proc = worker._run_worker(argv, root, timeout, out_dir / "stream.jsonl",
                           prompt=prompt)
        textio.write_text_lf(out_dir / "run.log", proc.stdout + proc.stderr)
        result = telemetry._terminal_result(proc.stdout)
        try:
            cost = float(result.get("total_cost_usd", 0.0))
        except (ValueError, AttributeError, TypeError):
            pass
        wall = limits.detect(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return {}, cost, None
    verdict = telemetry._read_verdict(verdict_path)
    if not verdict:
        verdict = review._verdict_from_message(str(result.get("result") or ""))
        if verdict:
            # Persisted so the run dir stays the full record of the check even
            # though the checker could not write it itself.
            textio.write_text_lf(verdict_path, json.dumps(verdict, indent=2))
    return verdict, cost, wall


def _quote_safe(text: str) -> str:
    """Neutralise backticks in tool output before it is written into a card.

    The runner copies gate and worker output verbatim into tracked markdown
    (`## Error`). Any backticked token in that output therefore becomes a real
    reference in a real doc — and `doc_reference_liveness` scans `Board/`. On
    2026-07-30 that closed a loop: one dangling reference failed a card, the
    failure message naming it was written into the card, and the next two cards
    failed on *that* text. One root cause, three cards, and the third message
    quoted the second quoting the first.

    So tool output is data, not markup: backticks become straight quotes on the
    way in. Cheap, lossless for a reader, and it makes the card's own error text
    inert to every gate that reads docs.
    """
    return text.replace("`", '"')


def _stale_slug(doc_rel: str) -> str:
    """A clean card id/filename stem from a doc path — no leading dot, no slash,
    so `card_schema`'s id==stem rule holds and the file is not hidden."""
    import re
    body = re.sub(r"[^a-z0-9]+", "-", doc_rel.lower().removesuffix(".md")).strip("-")
    return f"fix-stale-{body}"


def _stale_card_text(doc_rel: str, verdict: dict, report_dir: Path) -> str:
    """A fix-card for a doc stale-hunter flagged. It carries the findings but not
    the fix: choosing the fix is Tier 3 (a doc may be right and the *code* drifted),
    so this parks the evidence for a code-thread worker or Karel, which is the
    producer/checker seam holding — the checker reports, it does not repair.

    report_dir must already be relative to the repo root (callers pass
    `out_dir.relative_to(root)`) — an absolute path bakes in this machine's
    checkout location and reads as broken on any other machine."""
    findings = verdict.get("findings") or []
    lines = [f"- **{f.get('claim', '?')}** — {f.get('cite', '?')}: {f.get('why', '')}"
             for f in findings if isinstance(f, dict)]
    body = "\n".join(lines) or "(see the run log)"
    return f"""---
id: {_stale_slug(doc_rel)}
title: "Staleness: {doc_rel} names source that has drifted"
tier: worker
worker: code-thread
recipe: none
unattended: false
verify: review
created: {dt.date.today().isoformat()}
---

## Intent

`stale-hunter` checked `{doc_rel}` against current source and reported drift. Each finding
quotes the doc verbatim and cites where the source now disagrees. **Choosing the fix is not
mechanical** — a doc may be right and the *code* may have drifted, or the doc may be the one
that is wrong. That judgement is why this is a card, not an auto-edit.

Full report: `{report_dir.as_posix()}`

## Approach

Reconcile the doc with the code it describes, one finding at a time: for each, decide whether
the *doc* drifted (correct the doc) or the *code* did (leave the doc, flag that separately) —
this card edits the doc, never code. Prefer deleting dead material over annotating it.

## Findings

{body}

## Acceptance

- Each finding above is resolved: the doc corrected, or the finding dismissed with a written
  reason (the source was right, or the claim is `unverifiable` intent). Prefer deleting dead
  material over annotating it (`feedback_doc_leanness`).
- `python -m nightshift.gates.run` stays clean.

## Open questions

none
"""


def stale_phase(root: Path, count: int, model: str, deadline, card_budget: float,
                timeout: int, record: run_record.Record | None = None) -> tuple[int, int, int]:
    """After the cards: spend leftover budget on the highest-churn docs.

    Deterministic selection (`stale_sweep`, no LLM), then one Tier-2 check per
    doc. A doc is written to the ledger only on a *complete* verdict, so a phase
    the deadline or kill switch cuts short re-checks that doc next time — Karel's
    rule. Findings become one fix-card per doc; the runner never edits a doc.

    Returns (checked, verified, carded) — the three numbers `record.stale()`
    has always carried. `record`, when given, additionally receives the two this
    triple cannot express and that Karel's morning needs: how many docs were
    *selected* (58, against 0 verified, is a broken sweep; 1 against 1 is a quiet
    one, and the old triple renders both as "nothing to report"), and how many
    verdicts came back incomplete. Flushed inside the loop, not after it, so a
    sweep killed at 05:57 still says what it got through.
    """
    ledger = stale_sweep.load_ledger(root)
    chosen = stale_sweep.select(root, count, ledger)
    if not chosen:
        hostconfig._log("stale sweep: nothing has changed since last verification — skipping")
        if record is not None:
            record.stale(selected=0, checked=0, verified=0, carded=0)
        return 0, 0, 0
    hostconfig._log(f"stale sweep: {len(chosen)} doc(s) selected by churn, most-churned first")

    checked = verified = carded = incomplete = 0
    carded_cards: list[str] = []

    def _flush() -> None:
        if record is not None:
            record.stale(selected=len(chosen), checked=checked, verified=verified,
                         carded=carded, incomplete=incomplete, cards=carded_cards)

    _flush()
    for cand in chosen:
        if deadline and dt.datetime.now() >= deadline:
            hostconfig._log(f"stale sweep: stopping — past {deadline:%H:%M}")
            break
        if hostconfig._stop_requested():
            hostconfig._log("stale sweep: stopping — kill switch appeared")
            break
        out_dir = stale_run_dir(root, cand.doc)
        hostconfig._status(root, phase="stale-sweep", doc=cand.doc, model=model, since=hostconfig._now())
        verdict, cost, wall = run_stale_check(root, cand.doc, out_dir, model, card_budget, timeout)
        checked += 1
        # The artefact decides, not the process's exit
        # (`wall-on-review-wrapup-discards-a-verdict`). `stale-hunter` is a pure
        # judge — its verdict *is* its entire output — so one that already says
        # `complete: true` means the doc was read to the end and the wall landed
        # on the wrap-up call after it. Honour it: the findings get carded and the
        # doc ledgered below, and only *then* does the sweep stop. A wall with
        # nothing complete written still leaves the ledger untouched, exactly as
        # before, so the doc is re-checked next time rather than recorded as
        # verified on a check that never finished.
        if wall is not None and not telemetry.verdict_survives_a_wall(telemetry.STALE_STAGE, verdict):
            hostconfig._log(f"  {cand.doc}: usage limit during the sweep — leaving the ledger untouched")
            _flush()
            break
        if not verdict.get("complete"):
            hostconfig._log(f"  {cand.doc}: no complete verdict — will re-check next time (not ledgered)")
            incomplete += 1
            _flush()
            continue

        # Order matters, and it is card → commit → ledger. The ledger is what
        # stops a doc being re-checked, so marking it before the findings are
        # durable is how a night can lose findings permanently: the doc reads
        # "verified" forever while the card carrying its drifts was never
        # committed. Committing per doc is the same guarantee the card loop above
        # gets from its per-card `publish` ("a cloud container can be killed
        # between cards") — this loop was written without it, and one killed
        # sweep is all it takes. A doc with no findings needs no commit: its only
        # record is the gitignored, per-machine, rebuildable ledger, so losing
        # that costs a re-check and nothing else.
        # Quote-or-drop, enforced rather than asked for. A claim that is not in
        # the doc is not a finding however true its `why` reads — see
        # `stale_sweep.quote_checked`.
        try:
            doc_text = (root / cand.doc).read_text(encoding="utf-8", errors="replace")
            quote_checkable = True
        except OSError:
            # Cannot adjudicate, so do not pretend to: keep every finding (they are
            # leads either way) and refuse the ledger. Dropping them all here would
            # turn an unreadable doc into silent data loss.
            doc_text, quote_checkable = "", False
        if quote_checkable:
            findings, spliced = stale_sweep.quote_checked(
                doc_text, verdict.get("findings") or [])
        else:
            findings, spliced = list(verdict.get("findings") or []), []
        if spliced:
            hostconfig._log(f"  {cand.doc}: dropped {len(spliced)} finding(s) — the quoted claim is "
                 f"not in the doc (quote-or-drop)")
        ok_to_ledger, why_not = stale_sweep.may_ledger(verdict, spliced)
        if ok_to_ledger and not quote_checkable:
            ok_to_ledger, why_not = False, f"could not read {cand.doc} to check its quotes"
        if findings:
            card_path = board.board_dir(root) / "tasks" / f"{_stale_slug(cand.doc)}.md"
            # Relative to root, not out_dir as-is: out_dir is anchored to this
            # machine's checkout, and Karel works this repo from more than one
            # machine with different absolute paths above the repo root.
            textio.write_text_lf(
                card_path, _stale_card_text(cand.doc, verdict, out_dir.relative_to(root)))
            carded += 1
            carded_cards.append(card_path.stem)
            # Committed per doc rather than at the end of the sweep, so "a sweep got
            # this far" survives a run that never reaches its wrap-up commit.
            board.commit_board(root, f"stale: {cand.doc} — {len(findings)} drift(s) carded")
            hostconfig._log(f"  {cand.doc}: {len(findings)} drift(s) — carded {card_path.name}")
        else:
            hostconfig._log(f"  {cand.doc}: {verdict.get('summary', 'no drift')}")
        if ok_to_ledger:
            stale_sweep.mark_verified(root, cand.doc, ledger, authoritative=True)
            verified += 1
        else:
            # Carded what it found, but the doc is NOT recorded as checked: it
            # comes back round next sweep. `complete` is not `exhaustive`.
            hostconfig._log(f"  {cand.doc}: not ledgered — {why_not}")
            incomplete += 1
        _flush()
        if wall is not None:
            # This doc's verdict was honoured and is now durable — card committed,
            # ledger marked. The window is still shut, so the sweep stops here
            # rather than spawning the next `stale-hunter` into it.
            hostconfig._log(f"  {cand.doc}: the checker walled on its wrap-up after a complete "
                 f"verdict — honoured and ledgered; stopping the sweep there")
            break
    _flush()
    return checked, verified, carded
