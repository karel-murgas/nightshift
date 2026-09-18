"""Quote-or-drop enforcement and the ledger guard for `stale-hunter` verdicts.

Both exist because of one measured dispatch (Project Tigress
`.claude/memory/ai_team/04_local_runtime.md` §9d, 2026-09-18): a local checker
returned `complete: true`, found 2 of 5 known drifts, and of its three findings one
quoted a claim the document does not make. The old code would have carded that
false positive and recorded the doc as verified.
"""
from __future__ import annotations

import pytest

from nightshift import stale_sweep

BT = chr(96)
DOC = (
    "The deadline is round(route_len " + BT + "DEADLINE_DIST_MULT=2.0" + BT + ") plus a buffer.\n"
    "CATALOG dict[str,SkillDef] with 31 entries, base_xp read from settings.\n"
    "The resolver takes resolve_move(node, action, floor) as its first parameter.\n"
)


def test_verbatim_claim_is_kept():
    kept, spliced = stale_sweep.quote_checked(DOC, [{"claim": "CATALOG dict[str,SkillDef] with 31 entries"}])
    assert [f["claim"] for f in kept] == ["CATALOG dict[str,SkillDef] with 31 entries"]
    assert spliced == []


def test_quote_survives_a_hard_wrap_and_backticks():
    """A doc is hard-wrapped and a checker re-fences symbols; neither is a defect."""
    kept, spliced = stale_sweep.quote_checked(
        DOC, [{"claim": "plus a buffer. CATALOG dict[str,SkillDef]"},
              {"claim": BT + "DEADLINE_DIST_MULT=2.0" + BT}])
    assert len(kept) == 2
    assert spliced == []


def test_spliced_claim_is_dropped():
    """The real 2026-09-18 false positive: two clauses of one sentence fused into a
    claim the doc never makes. Its reasoning was sound, which is why a human misses it."""
    kept, spliced = stale_sweep.quote_checked(
        DOC, [{"claim": "resolve_move(node, action, floor) with 31 entries",
               "why": "reads plausibly and is entirely fiction"}])
    assert kept == []
    assert len(spliced) == 1


def test_empty_claim_is_not_a_finding():
    kept, spliced = stale_sweep.quote_checked(DOC, [{"claim": ""}, {}])
    assert kept == []
    assert len(spliced) == 2


def test_may_ledger_requires_complete():
    ok, why = stale_sweep.may_ledger({"complete": False}, [])
    assert not ok and "complete" in why


def test_may_ledger_refuses_after_a_spliced_finding():
    ok, why = stale_sweep.may_ledger({"complete": True}, [{"claim": "not in the doc"}])
    assert not ok and "quote-or-drop" in why


def test_may_ledger_allows_a_clean_complete_verdict():
    ok, why = stale_sweep.may_ledger({"complete": True}, [])
    assert ok and why == ""


def test_mark_verified_refuses_to_decide_for_its_caller(tmp_path):
    """`authoritative` has no default: a call site must answer, not delegate."""
    with pytest.raises(ValueError, match="authoritative=False"):
        stale_sweep.mark_verified(tmp_path, "doc.md", {}, authoritative=False)


def test_mark_verified_has_no_default_for_authoritative():
    """Guards the signature itself — a default would let the question be skipped."""
    import inspect

    param = inspect.signature(stale_sweep.mark_verified).parameters["authoritative"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
