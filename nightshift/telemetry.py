"""Run telemetry and usage accounting: what one worker attempt cost, and what
its terminal JSON verdict said.

`_terminal_result` is the one reader of a Claude CLI run's final JSON blob
(single-shot or `stream-json`, live or rehydrated from disk); `read_telemetry`/
`usage_breakdown`/`record_usage` turn that and the run's other on-disk
artefacts into the cost/usage figures `costreport` and the card's own record
carry. `verdict_survives_a_wall` and the `*_STAGE` names are the shared
vocabulary every pipeline stage (producer, checker, reviewer, stale-hunter,
repair, resolver) uses to say which of its own verdicts counts as finished.
Imports only `hostconfig`; every other split module imports this one.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from nightshift import board, run_record, tiers
from nightshift import hostconfig

def _terminal_result(stdout: str) -> dict:
    """The one JSON object that carries the run's cost/usage/verdict fields,
    out of a worker's raw stdout — whether that stdout is a single
    `--output-format json` blob (the old shape, and every existing test
    double) or a `--output-format stream-json` JSONL stream whose *last* line
    is the terminal `result` event (the new shape).

    Reads from the end on purpose: under the old shape the whole string is one
    line, so the last line *is* the only line and this agrees with a plain
    `json.loads` exactly. Under the new shape, scanning backwards also survives
    a stream cut short mid-line (a killed worker) — the true last *complete*
    line is still found instead of the parse just failing on trailing garbage.
    `{}` — not an exception — when nothing on the whole stream parses as a JSON
    object, matching `_read_verdict`'s "always a lookup" contract (§12).
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def run_dir(root: Path, card: board.Card, attempt: int) -> Path:
    path = root / hostconfig.RUNS / card.id / f"attempt-{attempt}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_verdict(path: Path) -> dict:
    """A worker's structured report, or `{}`.

    Always a lookup, never an interpretation (§12). `{}` is a meaningful answer —
    for the producer it means "judge me by the gates", which is what keeps
    charters written before the runner existed working unchanged.
    """
    if not path.is_file():
        return {}
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


# The pipeline stages that produce a verdict, named so the wall-handling at each
# spawn site says which predicate it means rather than inlining a set of strings.
PRODUCER_STAGE, CHECKER_STAGE = "producer", "checker"
REVIEWER_STAGE, STALE_STAGE = "reviewer", "stale-hunter"
#: The drift repair (drift-should-not-end-the-night). Unlike every other stage its
#: "verdict" is not a file the agent wrote — it is the gate suite's own answer,
#: re-run over the repaired tree. That makes its wall predicate the strongest of
#: the five: a walled repair whose gates come back clean has *demonstrably* done
#: its job, whatever the wrap-up call did afterwards.
REPAIR_STAGE = "repair"
#: The rebase-conflict resolver (`_resolve_conflict`), the fifth judge stage.
RESOLVER_STAGE = "merge-resolver"


def verdict_survives_a_wall(stage: str, verdict: dict, *, rounds_left: int = 0) -> bool:
    """Whether a verdict already written is *terminal for its stage*, and so
    survives the wall that landed on the call that was wrapping it up
    (`wall-on-review-wrapup-discards-a-verdict`).

    **The defect this closes is an ordering, not a durability problem.** Every
    spawn function returns `(verdict, cost, wall)`, and at each site the wall
    branch returned before the verdict was ever looked at — so a stage that had
    written a complete, usable verdict and *then* walled on its own wrap-up call
    was recorded byte-identically to one that died before writing anything
    (2026-08-09, `menu-art-cyberware`: `review-2.json` said `pass`, and the run
    log said "not attempted, attempt given back"). Nothing has to be read back
    off disk to fix that; the parsed verdict is already a local variable next to
    the wall. This function is asked *first*, and only then is the process's exit
    consulted.

    **There is no threshold here, deliberately.** "Complete" is not a new
    judgment — it is the predicate the no-wall path a few lines below each site
    already applies, and a truncated file degrades for free because
    `_read_verdict` returns `{}` on a `JSONDecodeError`. Per stage:

    * `PRODUCER_STAGE` — `outcome: parked` only. A park is terminal by
      construction: the round loop already `break`s on it and no re-roll resolves
      an ambiguity, and losing one costs Karel a whole night's question. Any
      other producer verdict keeps the old give-back, because a verdict attests
      the *worker's* account, not that the tree is finished — honouring a
      half-written tree would turn a free give-back into a spent attempt.
    * `CHECKER_STAGE` — `pass`, or any complete verdict once the rounds are
      spent. **The `rounds_left` guard is not optional.** A checker that walls
      after writing `revise` at round 1 of 3 must still be `limited`: the loop
      cannot spend rounds 2-3 in a closed window, and the path below it would
      file the card as *"did not pass this in 1 round(s)"* — parking a card that
      had two rounds it never received.
    * `REVIEWER_STAGE` — the three routings the reviewer exists to produce. Its
      whole output *is* the verdict file, so a complete one means it finished.
    * `STALE_STAGE` — its own `complete: true`, which the prompt already defines
      as "I finished reading the whole document".

    An unrecognised stage is `False`: today's behaviour, never a guess. A fifth
    judge stage therefore has to state its own predicate here to be honoured, and
    `test_every_spawn_sites_wall_path_routes_through_the_shared_helper` is what
    stops it quietly copying the wrong ordering from its neighbours instead.
    """
    if not isinstance(verdict, dict) or not verdict:
        return False
    if stage == PRODUCER_STAGE:
        return str(verdict.get("outcome", "")).lower() == "parked"
    if stage == CHECKER_STAGE:
        called = str(verdict.get("verdict", "")).lower()
        if called == "pass":
            return True
        return rounds_left <= 0 and called in ("revise", "reject")
    if stage == REVIEWER_STAGE:
        return str(verdict.get("verdict", "")).lower() in ("ok", "needs_fix", "needs_decision")
    if stage == STALE_STAGE:
        return bool(verdict.get("complete"))
    if stage == REPAIR_STAGE:
        # Not the agent's account of itself — the gates', re-run after it. A repair
        # that walled on its wrap-up but left a tree the whole suite passes is
        # finished by the only measure this stage has, and discarding it would end
        # the night over drift that is no longer there.
        return bool(verdict.get("gates_clean"))
    if stage == RESOLVER_STAGE:
        # Either answer is terminal for this stage — `resolved: false` is a real,
        # useful decline that lands on the card in `blocked/`, not a non-result. The
        # only thing that must not survive is a verdict with neither key, which the
        # `in (…)` fails and `{}` already failed above.
        #
        # Terminal for the STAGE is not terminal for the FUNCTION: a rebase can pause
        # again on the next replayed commit, and `_resolve_conflict` still stops its
        # loop on a wall rather than spawning a second resolver into a closed window.
        return verdict.get("resolved") in (True, False)
    return False


def _session_id(out_dir: Path, round_no: int) -> str:
    """The walled worker's session id, read from the result JSON it already wrote
    (`worker-N.json`, `runner.py`'s producer). Nothing had to capture it — the CLI
    reports it under `--output-format json` and the runner writes the whole blob to
    disk — so warm resume only has to *use* a fact that was already on disk."""
    data = hostconfig._load_json(out_dir / f"worker-{round_no}.json")
    return str(data.get("session_id", "") or "")


def _api_error_interruption(out_dir: Path, round_no: int) -> bool:
    """Whether this round's non-zero exit was an API disconnection that never
    reached verification, rather than a genuine self-reported failure
    (failed-attempt-work-is-deleted-not-resumed, the `api_error` slice).

    Keys on `terminal_reason` from the worker's own result JSON plus the
    *absence* of a gate or pytest log — never on the exit code, which is `1`
    for both this and an ordinary failure (`corridor-generation-redesign`
    attempt 1: 121 turns, a disconnected socket, and a 1,237-line rescue
    nobody would have resumed). A worker that *did* reach verification and
    failed it writes `gates.txt`/`pytest.txt` before this is ever called (they
    are written later in `dispatch`, after `run_producer` returns) — that path
    returns `failed` through its own branch and never reaches this check, so
    the two are told apart by construction, not by guessing at intent.
    """
    data = hostconfig._load_json(out_dir / f"worker-{round_no}.json")
    if str(data.get("terminal_reason", "")) != "api_error":
        return False
    return not (out_dir / "gates.txt").exists() and not (out_dir / "pytest.txt").exists()


def _si(n: float) -> str:
    """1234567 -> `1.23M`. Token counts are unreadable at full width."""
    for limit, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= limit:
            return f"{n / limit:.2f}{suffix}"
    return str(int(n))


def _parse_result_file(path: Path, *, is_json: bool) -> dict:
    """One stage's terminal result, off disk, in whichever shape it was written.

    `is_json` is `True` for a `worker-N.json` — a clean `json.dumps` of exactly
    the terminal event (`run_producer._once`'s own comment on why) — and `False`
    for a `*.log` file, which is `proc.stdout + proc.stderr` and needs
    `_terminal_result`'s same backward scan the live call already used, so a
    re-parse here finds the identical object rather than a slightly different
    one. `{}` on anything unreadable or not an object, matching every other
    "always a lookup" reader in this module (§12).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if is_json:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
    return _terminal_result(text)


def _sum_result_files(paths: list[Path], *, is_json: bool) -> dict:
    """One stage's totals over however many result files it left — the shared
    arithmetic behind both `read_telemetry` (the worker only) and
    `usage_breakdown` (every stage). See `_parse_result_file` for the two shapes
    a caller may be summing.
    """
    total = {
        "calls": 0, "wall_s": 0.0, "api_s": 0.0, "turns": 0, "cost_usd": 0.0,
        "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "input_tokens": 0, "denials": 0, "models": [], "ended": "",
    }
    for path in paths:
        data = _parse_result_file(path, is_json=is_json)
        if not data:
            continue
        total["calls"] += 1
        total["wall_s"] += float(data.get("duration_ms") or 0) / 1000
        total["api_s"] += float(data.get("duration_api_ms") or 0) / 1000
        total["turns"] += int(data.get("num_turns") or 0)
        total["cost_usd"] += float(data.get("total_cost_usd") or 0)
        usage = data.get("usage")
        if isinstance(usage, dict):
            total["output_tokens"] += int(usage.get("output_tokens") or 0)
            total["cache_read_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
            total["cache_write_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)
            total["input_tokens"] += int(usage.get("input_tokens") or 0)
        denials = data.get("permission_denials")
        if isinstance(denials, list):
            total["denials"] += len(denials)
        models = data.get("modelUsage")
        if isinstance(models, dict):
            for name in models:
                if name not in total["models"]:
                    total["models"].append(name)
        total["ended"] = str(data.get("terminal_reason") or data.get("stop_reason") or "")
    return total


def read_telemetry(out_dir: Path) -> dict:
    """What the CLI already reported about one attempt's producer, summed over
    its rounds.

    **Every number here was already downloaded and thrown away.** The runner
    writes each round's `worker-N.json` to disk and used to read exactly one
    key out of it — `total_cost_usd` — discarding wall time, API time, turn
    count, the four token counters, the per-model split and the permission
    denials. Surfacing them costs no tokens, no API calls and no wall time: it
    is a read of bytes already paid for, which is why this is a lookup in the
    runner rather than anything cleverer (§5, §12).

    Kept as a plain dict of totals rather than a dataclass because it is written
    straight to a card and to `status.json`, and both want JSON. Only the
    producer's own rounds — `usage_breakdown` is the sibling that covers every
    other stage the same way.
    """
    total = _sum_result_files(sorted(out_dir.glob("worker-*.json")), is_json=True)
    total["rounds"] = total.pop("calls")
    return total if total["rounds"] else {}


#: Which files under one attempt's `out_dir` hold a stage's raw terminal
#: result(s), and how to find and parse them — the file-naming contract every
#: spawn site in this module already follows (`run_producer`/`run_checker`/
#: `review_branch`/`repair_drift`/`_resolve_conflict`). `is_json` distinguishes
#: a clean `worker-N.json` from a `*.log` that needs `_terminal_result`'s scan
#: (`_parse_result_file`). Checker and reviewer share the `review*` prefix but
#: never the same file: a checker round is always `review-<digits>.log`, the
#: diff reviewer's is always the bare `review.log` — the glob below matches only
#: the first, and the `is_file()` check below only the second.
_USAGE_SOURCES: tuple[tuple[str, bool, Callable[[Path], list[Path]]], ...] = (
    ("worker", True, lambda d: sorted(d.glob("worker-*.json"))),
    ("checker", False, lambda d: sorted(d.glob("review-[0-9]*.log"))),
    ("reviewer", False, lambda d: [d / "review.log"] if (d / "review.log").is_file() else []),
    ("repair", False, lambda d: [d / "repair.log"] if (d / "repair.log").is_file() else []),
    ("resolver", False, lambda d: sorted(d.glob("resolve-[0-9]*.log"))
                                  + ([d / "merge-resolve.log"]
                                     if (d / "merge-resolve.log").is_file() else [])),
)


def usage_breakdown(out_dir: Path) -> list[dict]:
    """Every LLM call one attempt already paid for, split out by pipeline stage.

    `read_telemetry`'s sibling: that function answers "what did the worker
    cost", already written onto the card; this answers "what did *every* stage
    cost" — worker, checker, reviewer, repair, resolver — for a caller that
    wants the breakdown rather than one folded total (`record_usage`, and
    `nightshift.costreport`). Same source, same "already downloaded and thrown
    away" reasoning (§5, §12): nothing here re-runs anything or opens a new
    process.

    One dict per stage that actually ran, `stage` included, its rounds already
    summed (`calls`) — never one entry per round, at the grain the plan's own
    cost table already reports at (`token-economy.md` §1).
    """
    out = []
    for stage, is_json, finder in _USAGE_SOURCES:
        total = _sum_result_files(finder(out_dir), is_json=is_json)
        if total["calls"]:
            total["stage"] = stage
            out.append(total)
    return out


#: Which tier each pipeline stage is dispatched at. The stages of `_USAGE_SOURCES`,
#: mapped to the tier whose model they already resolve through — `run_checker` and
#: `repair_drift` are both handed the *card's* model by their caller, and both
#: resolvers resolve `lead` themselves. Written down once because
#: `token-economy.md` defect 4a was precisely this table existing only as a habit:
#: `--effort` reached one stage and the run record described all five.
_STAGE_TIER: dict[str, str] = {
    "worker": "", "checker": "", "repair": "",   # "" = the card's own tier
    "reviewer": "lead", "resolver": "lead",
}


def stage_efforts(root: Path, card_tier: str, *, worker: str = "") -> dict[str, str]:
    """`{stage: --effort}` for one card's whole pipeline.

    **The single answer to "what effort does this stage run at", used by both
    the spawn and the record.** That is the whole design: `token-economy.md`
    defect 4a was not a missing flag but a missing *shared* answer — `--effort`
    was passed at one call site and the run record was written at another, so a
    dispatched effort could be true and unrecorded, or recorded and untrue, with
    no way to tell which from a finished run. Both now read this.

    `worker` overrides the card-tier stages for a caller that dispatches by name
    rather than off the card — the chore batch, which pins its tier so a
    hand-edited `tier:` cannot pull a batch onto the expensive model, and pins
    its effort for the same reason.

    A tier that `[tiers].effort` does not name resolves to `""`, and every
    consumer treats that as *pass no flag / inherited*, never as a default
    (`tiers.effort`).
    """
    own = worker or tiers.effort(root, card_tier)
    return {stage: (tiers.effort(root, tier) if tier else own)
            for stage, tier in _STAGE_TIER.items()}


def record_usage(record: run_record.Record, out_dir: Path, *, card_id: str,
                 model: str, efforts: dict[str, str] | None = None) -> None:
    """Write this attempt's whole usage breakdown into the run record, one
    `Record.usage()` event per stage that actually ran.

    Called once, from the single place a card's `Dispatch` already turns into a
    `record.dispatched()` event, rather than threaded through the dispatch loop
    itself: the loop already collapses every stage's cost into one running
    float (`dispatch`'s own `cost +=`), and this reads the same directory that
    loop just finished writing to instead of asking it to carry more state
    through every return path. `model` is `usage_breakdown`'s own per-stage
    `models` list when the CLI reported one (a checker or reviewer can resolve
    to a different tier than the worker); the caller's `model` is the fallback
    for a stage whose result carried none.

    `efforts` maps a stage name to the `--effort` that stage was actually
    spawned with, and **is per stage because effort is** — `stage_efforts` is
    where that map comes from, and callers are expected to hand it through
    rather than build one: it is the same map the stages were spawned from, so
    the record cannot claim an effort the CLI never saw. Recording one
    attempt-wide effort instead would have stamped the chore worker's `medium`
    onto a reviewer that never got the flag.

    A stage the caller does not name is still recorded as `""`, which reads as
    *inherited the CLI default* — the honest answer, and a different claim from
    `"medium"`.

    This field is the instrument 2.2 and 3.3 are checked with: nothing else
    writes an effort anywhere a run can be read back from, so before this an
    `--effort` could be passed and have no observable trace at all
    (`token-economy.md` §5, defect 4). Until 3.3 only `run_producer` took one,
    so the map had a single live key and the other four stages recorded `""`
    truthfully — they really did inherit. Now every stage resolves an effort
    from its tier, so a `""` here means the project declared none for that tier,
    not that the runner forgot to pass one.
    """
    efforts = efforts or {}
    for entry in usage_breakdown(out_dir):
        stage = entry.pop("stage")
        models = entry.pop("models")
        entry.pop("ended", None)
        record.usage(stage, card_id=card_id, model=", ".join(models) or model,
                     effort=efforts.get(stage, ""), **entry)


# How many lines of the CLI's own error text reach the card. Enough to carry a
# stack-less API error or a refusal message; short enough that `## Error` stays
# readable in Obsidian.
_WORKER_EVIDENCE_LINES = 12


def worker_exit_evidence(out_dir: Path) -> str:
    """Why the worker process itself failed, as a bounded block for `## Error`.

    The third of three evidence sources, and the one that was missing. `Dispatch`
    carries `evidence` for exactly this purpose, and the gates path fills it with the
    violation lines while the tests path fills it with `suite.failure_excerpt` — but
    the `code != 0` path constructed `Dispatch("failed", f"worker exited {code}")`
    and passed nothing. So a card whose *worker* died recorded a reason that named
    only the exit status, and the pointer beside it was to a directory
    `prune_run_dir` deletes on the retiring attempt.

    That is how `stair-remnants-removal` retired on 2026-08-01 saying "worker exited
    1" and nothing else. The actual cause — `ended api_error`, one permission denial
    — *had* been parsed, by `read_telemetry`, and written to `## Telemetry`: a
    different section, by a different code path, that happens to sit above this one.
    Reading the two together was luck, not design, and on any other day the numbers
    would have been in the same place while the cause was not.

    So this reuses `read_telemetry` rather than parsing `worker-*.json` a second
    time — the facts were already downloaded and are already on disk (§5, §12); what
    was missing was routing them to the section a human actually reads for a reason.
    Indented four spaces like every other excerpt, so a `#` in the CLI's own message
    cannot match `doc_scan._HEADING`.
    """
    tel = read_telemetry(out_dir)
    lines: list[str] = []
    if tel.get("ended"):
        lines.append(f"    worker ended: {tel['ended']}")
    if tel.get("denials"):
        lines.append(f"    permission denials: {tel['denials']} — the worker asked for a "
                     f"tool and was refused, with nobody to ask in -p mode")
    if tel.get("turns"):
        lines.append(f"    {tel['turns']} turns · {tel['wall_s'] / 60:.1f} min wall "
                     f"before it exited")

    # The CLI's own message, when it reported one. `is_error` is the flag it sets on
    # a terminal failure; `result` is where it puts the text.
    for path in sorted(out_dir.glob("worker-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("is_error"):
            continue
        text = str(data.get("result") or data.get("error") or "").strip()
        if not text:
            continue
        for line in text.splitlines()[:_WORKER_EVIDENCE_LINES]:
            lines.append(f"    {line[:200]}")
        break
    return "\n".join(lines)


def telemetry_markdown(tel: dict, attempt: int) -> str:
    """The `## Telemetry` block. Lives on the **card**, not only in `.ai/runs/`,
    because the card is committed and syncs to Karel's other machine while the
    run directory is gitignored and machine-local."""
    denials = tel["denials"]
    lines = [
        f"- **attempt {attempt}** · {tel['wall_s'] / 60:.1f} min wall · "
        f"{tel['api_s'] / 60:.1f} min in API · {tel['turns']} turns · "
        f"${tel['cost_usd']:.2f} equivalent",
        f"- **tokens** · {_si(tel['output_tokens'])} out · "
        f"{_si(tel['cache_write_tokens'])} cache-write · "
        f"{_si(tel['cache_read_tokens'])} cache-read · {_si(tel['input_tokens'])} in",
        f"- **model** · {', '.join(tel['models']) or 'unrecorded'}"
        + (f" · ended `{tel['ended']}`" if tel["ended"] else "")
        + (f" · {tel['rounds']} rounds" if tel["rounds"] > 1 else ""),
    ]
    # Only when non-zero. A denial is a real explanation for an attempt that
    # looks inexplicably bad — the worker wanted a tool and was refused, with
    # nobody to ask in `-p` mode — and a permanent "none" line trains the eye
    # to skip exactly the line that matters on the night it is not "none".
    if denials:
        lines.append(f"- **⚠ {denials} permission denial(s)** — the worker was refused a "
                     f"tool it asked for; see `.ai/runs/`")
    return "\n".join(lines)
