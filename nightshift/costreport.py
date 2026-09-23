"""`python -m nightshift.costreport` — the breakdown behind `token-economy.md`.

That plan's §1 table ("where the budget goes today") was built by hand: opening
`worker-N.json` files and `stream.jsonl` transcripts one at a time, one card at
a time, to answer "how much of this was cache reads", "how much was the gate
hook's own output staying in context", "did the worker read the same file
twice". This is that process, made repeatable, so the next phase of the same
plan is judged against a number instead of a memory of a number.

**Two things this reads, and neither costs anything to read.** `run_record`'s
`usage` list (`token-economy.md` phase 0.1) is the dollar/token/turn breakdown
per pipeline stage — already summed, already on disk, no transcript parsing
needed for it. `transcript_stats` is the one part that *does* walk a
`stream.jsonl` — tool-result bytes by tool name, an estimate of how much of an
Edit/Write result was the gate hook's own stdout rather than the CLI's,
whether a `Read` came back to a path it had already opened, and how much of
what was read arrived after the first edit (mid-work) versus after the last
one (close-out, `post-implementation-cleanup`'s own stage) — every one of them
a read of bytes a worker already downloaded, never a second dispatch.

**No LLM anywhere in here**, the same rule `run_record.py`'s own module
docstring states for itself: this is JSON parsing and arithmetic over what the
runner, the chore batch and a dispatched session already wrote to
`.ai/runs/`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nightshift import hostconfig, manifest, run_record, telemetry

#: A `Read`'s own tool_use_id maps back to its call through this correlation;
#: an `Edit`/`Write` tool_result under this many UTF-8 bytes is presumed to be
#: the CLI's own boilerplate ("The file X has been updated successfully...");
#: bytes past it are presumed to be a PostToolUse hook's stdout riding along on
#: the same tool result the agent reads (`token-economy.md` §1's "gate suite
#: runs after every Edit/Write and its output stays in context" finding). A
#: heuristic, not a parse of the hook protocol — named `_est` in every field it
#: feeds so a reader does not mistake it for an exact count.
_EDIT_CONFIRMATION_BUDGET = 300


def _events(stream_path: Path) -> list[dict]:
    try:
        text = stream_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _tool_calls(events: list[dict]) -> dict[str, tuple[str, dict, str]]:
    """`tool_use_id` -> `(name, input, timestamp)` for every call in the stream."""
    index: dict[str, tuple[str, dict, str]] = {}
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for block in (ev.get("message") or {}).get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if tid:
                    index[str(tid)] = (str(block.get("name", "")),
                                       block.get("input") or {},
                                       str(ev.get("timestamp", "")))
    return index


def _result_text(block: dict) -> str:
    """A tool_result's own text, whichever of the two shapes the CLI used —
    a bare string, or a list of content blocks (only the `text` ones count;
    an image block carries no size worth attributing to a tool's byte cost
    here)."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content if isinstance(item, dict) and "text" in item
            or isinstance(item, str))
    return ""


def transcript_stats(stream_path: Path) -> dict:
    """One transcript's tool-result economy — tool-result bytes by tool name,
    the hook-output estimate, re-reads, and how much of what was read arrived
    mid-work versus at close-out.

    `{}` for a file that does not exist or holds nothing parseable — the same
    "always a lookup" contract `telemetry._read_verdict` follows, so a caller sums
    a list of these without checking each one first.
    """
    events = _events(stream_path)
    if not events:
        return {}
    calls = _tool_calls(events)

    tool_bytes: dict[str, int] = {}
    tool_calls: dict[str, int] = {}
    hook_output_bytes_est = 0
    read_events: list[tuple[str, int, str]] = []   # (file path, bytes, timestamp)
    first_edit_ts = ""
    last_edit_ts = ""

    for ev in events:
        if ev.get("type") != "user":
            continue
        for block in (ev.get("message") or {}).get("content", []) or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            name, tool_input, use_ts = calls.get(str(block.get("tool_use_id")),
                                                  ("", {}, str(ev.get("timestamp", ""))))
            size = len(_result_text(block).encode("utf-8", errors="replace"))
            tool_bytes[name] = tool_bytes.get(name, 0) + size
            tool_calls[name] = tool_calls.get(name, 0) + 1
            if name in ("Edit", "Write"):
                if not first_edit_ts:
                    first_edit_ts = use_ts
                last_edit_ts = use_ts
                if size > _EDIT_CONFIRMATION_BUDGET:
                    hook_output_bytes_est += size - _EDIT_CONFIRMATION_BUDGET
            elif name == "Read":
                path = str(tool_input.get("file_path", "")).strip()
                read_events.append((path, size, use_ts))

    read_bytes_total = sum(size for _p, size, _t in read_events)
    seen: dict[str, int] = {}
    reread_bytes = 0
    for path, size, _ts in read_events:
        if not path:
            continue
        seen[path] = seen.get(path, 0) + 1
        if seen[path] > 1:
            reread_bytes += size

    def _share(after: str) -> tuple[int, float]:
        if not after:
            return 0, 0.0
        total = sum(size for _p, size, ts in read_events if ts >= after)
        return total, round(total / read_bytes_total, 3) if read_bytes_total else 0.0

    after_first_edit_bytes, after_first_edit_share = _share(first_edit_ts)
    closeout_bytes, closeout_share = _share(last_edit_ts)

    return {
        "tool_calls": tool_calls,
        "tool_bytes": tool_bytes,
        "hook_output_bytes_est": hook_output_bytes_est,
        "read_bytes_total": read_bytes_total,
        "reread_bytes": reread_bytes,
        "reread_share": round(reread_bytes / read_bytes_total, 3) if read_bytes_total else 0.0,
        "read_bytes_after_first_edit": after_first_edit_bytes,
        "read_share_after_first_edit": after_first_edit_share,
        "read_bytes_closeout": closeout_bytes,
        "read_share_closeout": closeout_share,
    }


def _merge_stats(entries: list[dict]) -> dict:
    """Several transcripts' `transcript_stats`, summed into one.

    Byte/count fields add; the two share fields are re-derived from their own
    summed numerator and the summed `read_bytes_total`, rather than averaged —
    averaging shares from transcripts of very different sizes would weight a
    two-read stub the same as a two-hundred-read card.
    """
    out = {"tool_calls": {}, "tool_bytes": {}, "hook_output_bytes_est": 0,
          "read_bytes_total": 0, "reread_bytes": 0,
          "read_bytes_after_first_edit": 0, "read_bytes_closeout": 0}
    for entry in entries:
        if not entry:
            continue
        for name, n in entry.get("tool_calls", {}).items():
            out["tool_calls"][name] = out["tool_calls"].get(name, 0) + n
        for name, n in entry.get("tool_bytes", {}).items():
            out["tool_bytes"][name] = out["tool_bytes"].get(name, 0) + n
        for key in ("hook_output_bytes_est", "read_bytes_total", "reread_bytes",
                    "read_bytes_after_first_edit", "read_bytes_closeout"):
            out[key] += entry.get(key, 0)
    total = out["read_bytes_total"]
    out["reread_share"] = round(out["reread_bytes"] / total, 3) if total else 0.0
    out["read_share_after_first_edit"] = round(
        out["read_bytes_after_first_edit"] / total, 3) if total else 0.0
    out["read_share_closeout"] = round(out["read_bytes_closeout"] / total, 3) if total else 0.0
    return out


def card_transcript_stats(root: Path, card_id: str) -> dict:
    """Every transcript under `.ai/runs/<card_id>/attempt-*/`, summed.

    Globs `*.jsonl` rather than naming `stream.jsonl`/`review-stream.jsonl`/
    `repair-stream.jsonl` one at a time, so a stage added later is picked up
    without this needing to learn its filename. One exception worth knowing:
    the producer and its checker share one `stream.jsonl` per round
    (`run_checker`'s own call to `_run_worker` reuses the producer's path), so
    this reports the attempt's transcript economy as a whole rather than
    splitting worker from checker — `run_record`'s `usage` list is where that
    split already lives (see `usage_breakdown`), because it reads the clean
    per-stage result files instead of the shared transcript.
    """
    attempts_dir = root / hostconfig.RUNS / card_id
    stats = []
    for attempt_dir in sorted(attempts_dir.glob("attempt-*")):
        for stream_path in sorted(attempt_dir.glob("*.jsonl")):
            stats.append(transcript_stats(stream_path))
    return _merge_stats(stats)


def _find_record(root: Path, ref: str) -> dict | None:
    """The run record `ref` names, or the most recent one when `ref` is empty.

    `read_all` does not carry the filename stamp on the dict it returns (the
    record's own content has no need of it), so an exact/prefix match is done
    against the directory directly rather than by filtering `read_all`'s
    output.
    """
    if not ref:
        records = run_record.read_all(root)
        return records[0] if records else None
    matches = sorted((root / run_record.DIR).glob(f"{ref}*.json"), reverse=True)
    for path in matches:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("started"):
            return data
    return None


def _usage_lines(usage_entries: list[dict]) -> list[str]:
    by_stage: dict[str, dict] = {}
    for entry in usage_entries:
        bucket = by_stage.setdefault(entry.get("stage", "?"), {
            "cost_usd": 0.0, "turns": 0, "calls": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "input_tokens": 0, "output_tokens": 0,
        })
        for key in bucket:
            bucket[key] += entry.get(key, 0)
    lines = []
    for stage in sorted(by_stage):
        b = by_stage[stage]
        lines.append(
            f"  {stage:10} ${b['cost_usd']:.2f} · {b['turns']} turns · {b['calls']} call(s) "
            f"· {telemetry._si(b['cache_read_tokens'])} cache-read "
            f"· {telemetry._si(b['cache_write_tokens'])} cache-write")
    return lines


def _transcript_lines(stats: dict) -> list[str]:
    if not stats or not stats.get("tool_bytes"):
        return ["  (no transcript found)"]
    lines = []
    for name in sorted(stats["tool_bytes"], key=lambda n: -stats["tool_bytes"][n]):
        lines.append(f"  {name:10} {telemetry._si(stats['tool_bytes'][name]):>8} bytes "
                     f"over {stats['tool_calls'][name]} call(s)")
    lines.append(f"  hook output (est.)   {telemetry._si(stats['hook_output_bytes_est'])} bytes")
    lines.append(f"  re-reads             {telemetry._si(stats['reread_bytes'])} bytes "
                 f"({stats['reread_share'] * 100:.0f}% of read bytes)")
    lines.append(f"  read after 1st edit  {stats['read_share_after_first_edit'] * 100:.0f}% "
                 f"of read bytes")
    lines.append(f"  read at close-out    {stats['read_share_closeout'] * 100:.0f}% "
                 f"of read bytes")
    return lines


def report_card(root: Path, card_id: str) -> str:
    lines = [f"# {card_id}", ""]
    attempts_dir = root / hostconfig.RUNS / card_id
    usage_entries = []
    for attempt_dir in sorted(attempts_dir.glob("attempt-*")):
        usage_entries += telemetry.usage_breakdown(attempt_dir)
    lines.append("## usage")
    lines += _usage_lines(usage_entries) if usage_entries else ["  (no result files found)"]
    lines.append("")
    lines.append("## transcript")
    lines += _transcript_lines(card_transcript_stats(root, card_id))
    return "\n".join(lines) + "\n"


def report_run(root: Path, record: dict) -> str:
    lines = [f"# run {record.get('started', '?')} ({record.get('kind', '?')})", ""]
    lines.append("## usage")
    lines += _usage_lines(record.get("usage", [])) or ["  (no usage recorded — a run from "
                                                        "before token-economy phase 0.1)"]
    lines.append("")
    lines.append("## quality")
    counters = run_record.quality_counters([record], root=root)
    for key, value in counters.items():
        lines.append(f"  {key:20} {value}")
    lines.append("")
    lines.append("## transcript (every dispatched card)")
    stats = _merge_stats([card_transcript_stats(root, d["card"])
                          for d in record.get("dispatched", [])])
    lines += _transcript_lines(stats)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The cost/usage/transcript breakdown behind token-economy.md.")
    parser.add_argument("run", nargs="?", default="",
                        help="a card id (.ai/runs/<id>/attempt-*/), or a run record's "
                             "timestamp or prefix (.ai/runs/records/<stamp>.json); "
                             "omitted means the most recent run record")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--root", metavar="PATH",
                        help="report on this repo instead of the one above the "
                             "working directory (mainly for tests)")
    args = parser.parse_args(argv)

    if args.root:
        root = Path(args.root)
    else:
        try:
            root = manifest.find_root()
        except manifest.ManifestError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.run and (root / hostconfig.RUNS / args.run).is_dir():
        if args.json:
            usage_entries = []
            for attempt_dir in sorted((root / hostconfig.RUNS / args.run).glob("attempt-*")):
                usage_entries += telemetry.usage_breakdown(attempt_dir)
            print(json.dumps({"card": args.run, "usage": usage_entries,
                              "transcript": card_transcript_stats(root, args.run)}, indent=2))
        else:
            print(report_card(root, args.run), end="")
        return 0

    record = _find_record(root, args.run)
    if record is None:
        print(f"no run record found for {args.run!r}" if args.run
              else "no run records on disk yet", file=sys.stderr)
        return 1
    if args.json:
        out = dict(record)
        out["quality"] = run_record.quality_counters([record], root=root)
        out["transcript"] = _merge_stats([card_transcript_stats(root, d["card"])
                                          for d in record.get("dispatched", [])])
        print(json.dumps(out, indent=2))
    else:
        print(report_run(root, record), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
