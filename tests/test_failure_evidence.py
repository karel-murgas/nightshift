"""Tests for the failure-evidence path — `runner._error_section`, the record field
and the digest block (2026-07-31).

The hole these close. When a card failed, the only thing written anywhere the
maintainer could read was `check_junit`'s count string plus `Full output:
.ai/runs/<id>/attempt-N/`, and that pointer could not be followed from either
direction:

  * `.ai/runs/` is gitignored — machine-local by design — so a night run on the
    desktop leaves nothing at all behind on the laptop the board is read from; and
  * `runner.prune_run_dir` deletes the card's whole run directory the moment the
    card is retired to `failed/`, two lines after the pointer to it is written — so
    for a terminal failure the pointer was dead on the producing host too.

Dungeoneer's maintainer went looking on 2026-07-31 for why three cards failed
overnight and there was nowhere the answer could have been. The fix is that the
*committed* artefacts — the card and `Digest.md` — carry a bounded excerpt of the
real failure, captured before anything is pruned.

**Provenance.** Written in that repo's `tests/` and moved here whole by
`framework-tests-live-in-the-wrong-repo` (2026-08-08). Two tests use the repo
root, and both use it the same way — to name a path that really is *absent*, so
that the liveness exemption is proved to be covering something that would
genuinely dangle. That is a synthetic test leaning on a negative fact any repo
supplies, not a claim about a project, so it travels with the mechanism and the
root it leans on is now this one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightshift.gates import doc_scan
from nightshift import runner
from nightshift import suite

from nightshift import run_record

_REPO = Path(__file__).resolve().parent.parent


_EVIDENCE = ("    tests/test_stairs.py::test_remnants_cleared — assert 3 == 0\n"
             "    E       assert 3 == 0")


def _failed(evidence: str = _EVIDENCE, detail: str = "pytest: 1 failure(s)") -> runner.Dispatch:
    return runner.Dispatch("failed", detail, evidence=evidence)


# --- the card's `## Error` ----------------------------------------------------


def test_the_error_section_carries_the_evidence_inline():
    """The point of the whole change: the reason is *in* the card, not behind a
    pointer to a directory that may not exist on this machine."""
    body = runner._error_section("stair-remnants-removal", 1, _failed(), retiring=False)
    assert "tests/test_stairs.py::test_remnants_cleared" in body
    assert "assert 3 == 0" in body


def test_a_retrying_card_is_told_where_the_full_log_is_and_on_which_host():
    """`.ai/runs/` is meaningful on exactly one machine, so the text says which.
    Without the hostname "the logs are not here" reads as "the logs are gone"."""
    import socket
    body = runner._error_section("card-x", 1, _failed(), retiring=False)
    assert "`.ai/runs/card-x/attempt-1/`" in body
    assert socket.gethostname() in body
    assert "gitignored" in body


def test_a_retired_card_does_not_promise_a_directory_that_was_just_deleted():
    """`settle` calls `prune_run_dir` immediately after writing this section when
    the card is retired. Offering the path as somewhere to look is the original
    bug; the section has to say it is gone instead."""
    body = runner._error_section("card-x", 3, _failed(), retiring=True)
    assert "deleted" in body
    assert "Full output:" not in body


def test_the_section_survives_having_no_evidence_at_all():
    """A timeout produces no JUnit report, so there is nothing to quote. The
    section must still be the ordinary two-paragraph shape, with no stray
    exemption marker sitting over an empty block."""
    body = runner._error_section("card-x", 1,
                                 _failed(evidence="", detail="pytest: timed out after 3600s"),
                                 retiring=False)
    assert "timed out" in body
    assert "stale-ok" not in body


# --- the gate-exemption safety property ---------------------------------------


def _card_text(body: str) -> str:
    return ("---\nid: card-x\n---\n\n## Intent\n\nDo a thing.\n\n"
            "## Approach\n\nSomehow.\n\n## Error\n\n" + body + "\n")


def test_evidence_naming_a_file_that_does_not_exist_is_exempt_from_liveness():
    """Belt and braces since 2026-08-01, when `Board/` left `doc_scan.DOC_ROOTS`
    entirely — no lane is scanned now, so a pytest excerpt cannot trip liveness
    wherever the card sits.

    Kept because it asserts the marker is still emitted and still covers the
    evidence, which is what makes the card honest if `Board/` is ever put back in
    scope. The original reason, which is what argued that scope change: every line
    of a retrying card's `## Error` used to be read by `doc_reference_liveness`, and
    the paths most likely to dangle were the interesting ones — a test file the
    failed attempt created on a branch that was then discarded. Unexempted, one
    card's failure became a repo-wide violation and the *next* card in the night was
    filed failed for it, the 2026-07-23 shape `GATE_CRASH` exists to prevent.
    """
    dangling = "tests/test_never_committed.py"
    assert not (_REPO / dangling).exists(), "pick a name that really is absent"

    evidence = f"    {dangling}::test_thing — assert 3 == 0\n    E       assert 3 == 0"
    text = _card_text(runner._error_section("card-x", 1, _failed(evidence), retiring=False))

    exempt = doc_scan.exempt_lines(text)
    offending = [i for i, line in enumerate(text.splitlines(), start=1)
                 if dangling in line]
    assert offending, "the evidence should be in the text at all"
    assert all(i in exempt for i in offending)


def test_the_evidence_would_still_dangle_if_it_were_scanned():
    """Guards the test above from passing for the wrong reason: it proves the
    marker is covering something that really would be flagged, rather than
    decorating a line the extractor never looked at.

    Now that `Board/` is out of scope this is the *only* thing keeping the
    exemption honest — the gate no longer reaches these lines, so nothing else
    would notice if `_PATH_RE` stopped extracting them."""
    line = "    tests/test_never_committed.py::test_thing — assert 3 == 0"
    extracted = [m.group(0) for m in doc_scan._PATH_RE.finditer(line)]
    assert "tests/test_never_committed.py" in extracted
    assert not doc_scan.resolve_path(_REPO, "tests/test_never_committed.py")


def _worker_json(out_dir: Path, **fields) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"duration_ms": 510_000, "num_turns": 29, "total_cost_usd": 0.96}
    payload.update(fields)
    (out_dir / "worker-1.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_dead_worker_puts_its_reason_in_the_error_section(tmp_path):
    """The `stair-remnants-removal` case, 2026-08-01: the card retired saying only
    "worker exited 1", while the cause — `ended api_error`, one permission denial —
    sat in `## Telemetry`, written by a different code path that happens to print
    above it. Reading the two together was luck. `## Error` must carry the reason."""
    _worker_json(tmp_path, terminal_reason="api_error",
                 permission_denials=[{"tool": "Bash"}], is_error=True,
                 result="API error: 500 Internal Server Error")

    evidence = runner.worker_exit_evidence(tmp_path)
    assert "api_error" in evidence
    assert "permission denials: 1" in evidence
    assert "500 Internal Server Error" in evidence

    text = runner._error_section("stair-remnants-removal", 3,
                                 runner.Dispatch("failed", "worker exited 1", 0.96, 1,
                                                 evidence=evidence),
                                 retiring=True)
    assert "api_error" in text, "the retiring card must still say WHY, not just that"
    assert "500 Internal Server Error" in text


def test_worker_evidence_lines_are_indented_like_every_other_excerpt(tmp_path):
    """Four spaces, so a `#` in the CLI's own message cannot match
    `doc_scan._HEADING` and truncate the exemption that covers the block."""
    _worker_json(tmp_path, terminal_reason="api_error", is_error=True,
                 result="# fatal: something\nsecond line")
    for line in runner.worker_exit_evidence(tmp_path).splitlines():
        assert line.startswith("    "), line


def test_worker_evidence_is_bounded(tmp_path):
    """A worker can die printing a very long message; the card is read in Obsidian."""
    _worker_json(tmp_path, is_error=True,
                 result="\n".join(f"line {i}" for i in range(200)))
    body = [ln for ln in runner.worker_exit_evidence(tmp_path).splitlines()
            if ln.strip().startswith("line ")]
    assert len(body) == runner._WORKER_EVIDENCE_LINES


def test_no_worker_json_yields_no_evidence_rather_than_a_crash(tmp_path):
    """An attempt that died before the CLI wrote anything is the one case where
    there genuinely is nothing to say. It must degrade, not raise."""
    assert runner.worker_exit_evidence(tmp_path) == ""


def test_a_clean_exit_record_still_reports_what_it_knows(tmp_path):
    """No `is_error` and no terminal reason — a worker killed by a timeout, say.
    Turn count and wall time are still worth more than silence."""
    _worker_json(tmp_path)
    evidence = runner.worker_exit_evidence(tmp_path)
    assert "29 turns" in evidence


def test_the_exemption_marker_carries_a_written_reason():
    """`test_every_exemption_carries_a_written_reason` scans the whole repo for
    markers and requires a reason of at least ten characters. A machine-written
    marker with a thin reason would fail that gate on every failed card."""
    found = doc_scan._STALE_OK_OPEN.search(runner._EVIDENCE_EXEMPTION)
    assert found and len((found.group(1) or "").strip()) >= 10


def test_the_exemption_covers_the_rest_of_the_section_but_not_the_next_one():
    """A marker runs to the next `##` heading. It must not leak past `## Error`
    and quietly exempt a following section from liveness for good."""
    text = (_card_text(runner._error_section("card-x", 1, _failed(), retiring=False))
            + "\n## Thread\n\nSee `dungeoneer/does_not_exist.py` for the rest.\n")
    exempt = doc_scan.exempt_lines(text)
    leaked = [i for i, line in enumerate(text.splitlines(), start=1)
              if "does_not_exist" in line]
    assert leaked and not any(i in exempt for i in leaked)


def test_a_hash_in_pytest_output_cannot_end_the_exemption_early():
    """`doc_scan._HEADING` is anchored at column 0, and every excerpt line is
    indented four spaces — so a `#` pytest printed cannot be read as a heading and
    truncate the exemption over the lines that follow it."""
    evidence = ("    tests/test_a.py::test_x — boom\n"
                "    ## pytest printed this\n"
                "    tests/test_never_committed.py::test_y — boom")
    text = _card_text(runner._error_section("card-x", 1, _failed(evidence), retiring=False))
    exempt = doc_scan.exempt_lines(text)
    offending = [i for i, line in enumerate(text.splitlines(), start=1)
                 if "test_never_committed" in line]
    assert offending and all(i in exempt for i in offending)


# --- the record and the digest ------------------------------------------------


def test_the_record_stores_the_evidence(tmp_path):
    record = run_record.start(tmp_path, kind="test")
    record.dispatched("card-x", worker="code-thread", model="sonnet", attempt=1,
                      outcome="failed", detail="pytest: 1 failure(s)",
                      evidence=_EVIDENCE, landed="attempt 1 failed, will retry")
    stored = run_record.failures(run_record.read_all(tmp_path)[0])
    assert stored and stored[0]["evidence"] == _EVIDENCE


def test_a_record_written_without_evidence_still_reads(tmp_path):
    """Records from before this field existed are still on disk and inside the
    digest's window; the digest must not raise on them."""
    record = run_record.start(tmp_path, kind="test")
    record.dispatched("card-x", worker="code-thread", model="sonnet", attempt=1,
                      outcome="failed", detail="pytest: 1 failure(s)")
    stored = run_record.failures(run_record.read_all(tmp_path)[0])
    assert stored and stored[0]["evidence"] == ""


@pytest.mark.parametrize("evidence, expected", [
    (_EVIDENCE, True),
    ("", False),
    ("   \n  \n", False),
])
def test_the_digest_quotes_the_evidence_when_there_is_any(evidence, expected):
    from nightshift import digest
    lines = digest._evidence_lines({"evidence": evidence})
    assert bool(lines) is expected
    # Eight spaces total: four from `failure_excerpt`, four more to nest the block
    # inside its list item, which is what makes it render as code rather than as
    # mangled prose running into the bullet above.
    assert all(line.startswith("        ") for line in lines)


def test_the_digest_caps_how_much_it_quotes():
    """The card is where one failure is worked; the digest is a morning scan of a
    whole night, and forty lines of traceback at the top is one Karel stops
    reading."""
    from nightshift import digest
    long_block = "\n".join(f"    E  line {i}" for i in range(40))
    assert len(digest._evidence_lines({"evidence": long_block})) == digest._EVIDENCE_LINES


def test_the_digest_tolerates_a_record_with_no_evidence_key():
    from nightshift import digest
    assert digest._evidence_lines({}) == []


# --- the failure signature (grouping) -----------------------------------------


def test_a_pytest_signature_is_the_failing_test_not_the_slice_size():
    """`pytest: 2 failure(s) … across 1860 test(s)` describes the slice that ran.
    Two cards that broke the same test get different totals from the selector, so
    grouping on the counts groups on noise — and the 110-char clip used to land on
    `— first:…`, cutting the string off exactly where it became useful."""
    from nightshift import digest
    detail = ("pytest: 2 failure(s), 0 error(s) across 1860 test(s) — first: "
              "tests/test_stairs.py::test_cleared: AssertionError: assert 3 == 0")
    signature = digest._failure_signature(detail)
    assert signature.startswith("tests/test_stairs.py::test_cleared")
    assert "1860" not in signature and "first:" not in signature


def test_two_cards_that_broke_the_same_test_group_together():
    """The payoff: 'one cause, N casualties' now fires for pytest, not just gates."""
    from nightshift import digest
    same = "tests/test_stairs.py::test_cleared: AssertionError: assert 3 == 0"
    a = f"pytest: 1 failure(s), 0 error(s) across 40 test(s) — first: {same}"
    b = f"pytest: 3 failure(s), 0 error(s) across 1860 test(s) — first: {same}"
    assert digest._failure_signature(a) == digest._failure_signature(b)


def test_a_gate_violation_signature_is_unchanged():
    """Gate details are `;`-joined with a `file:line — ` prefix and must keep the
    original normalisation — that is the 2026-07-30 'three cards, one broken gate'
    case this function was written for."""
    from nightshift import digest
    detail = ("Board/tasks/x.md:145 — doc_reference_liveness: symbol LICENSE does not "
              "exist; 1 violation(s) across 26 gate(s): asset_hygiene")
    assert (digest._failure_signature(detail)
            == "doc_reference_liveness: symbol LICENSE does not exist")
