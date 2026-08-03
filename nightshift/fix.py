#!/usr/bin/env python3
"""`nightshift fix` — run every check, then have an agent fix what failed.

**Why this exists.** `init` used to end with a numbered checklist: run the doctor, run
the gates, run preflight, fix what they report. That is the right list of next actions
and the wrong actor. The origin project's maintainer, 2026-08-03: *"It's not meant to be
used by a human, but used by AI. I would like the init to end with LLM running the
diagnosis and fixing bugs that it found. Not human, who checks gates manually now. In
this project, it is you, who uses the nightshift, does the preflight and fixes bugs that
gates find."* Exactly so — in the origin project every gate violation for three months
was read and fixed by an agent, and the framework shipped that loop as a paragraph of
advice rather than a command.

So: diagnose, dispatch one agent at the `lead` tier with the failures and the commands
that reproduce them, re-diagnose, stop when green — or when a round changes nothing,
which is the honest end of an automated attempt.

**The prohibitions are the substance of this module.** An agent told to make the checks
pass will make the checks pass, and the cheapest route is always to weaken the check:
delete the gate, raise the budget, mark the test `xfail`, pass `--no-corrections`. Every
one of those has been done in this codebase by an agent acting in good faith. They are
listed with their reasons in `PROHIBITIONS`; a fix loop without that section is a
machine for turning red into green while the defect stays.

**What it leaves alone.** It does not commit, push or merge — the diff stays dirty,
because the operator reading it is where this stays honest. What it decides it should
not fix becomes a card in `needs-decision/` instead, so a judgment call arrives as
something to review rather than as a line in a log nobody re-reads.
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import board, preflight, runner, textio, tiers
from nightshift.manifest import AI_DIR, ManifestError, find_root

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Where a round's prompt and stream land. Under `.ai/runs/`, which `init` now teaches a
# consuming project to ignore, so a fix pass leaves nothing to commit.
RUNS = f"{AI_DIR}/runs/_fix"

# Three is less a tuning constant than an observation: a check still failing after three
# attempts is failing for a reason the fourth will not find either, and the useful
# output at that point is the transcript.
MAX_ROUNDS = 3
ROUND_TIMEOUT_S = 3600

# How to reproduce each check, so the agent reads real output rather than trusting a
# summary line — and re-reads it after its own edit. Keyed by `preflight.Check.name`.
REPRODUCE = {
    "gates": "python -m nightshift.gates.run",
    "audit-matrix": "python -m nightshift.audit --check",
    "pytest": "python -m nightshift.preflight   # runs the slice; see nightshift/suite.py",
    "lf-worktree": "python -m nightshift.doctor",
    "claude-bin": "python -m nightshift.doctor",
    "hosts-json": "python -m nightshift.doctor",
    "preflight-config": "python -m nightshift.doctor",
    "framework-version": "python -m nightshift.doctor",
    "corrections": "python -m nightshift.corrections",
}

PROHIBITIONS = """\
## What you may not do

Each of these makes a check pass without fixing anything, and each has been done in this
codebase by an agent acting in good faith. That is why they are written down rather than
left to judgment.

- **Do not edit, delete, weaken, narrow or skip a gate to make it pass.** A gate is
  sometimes wrong; the answer is the appeal path in the `gate_appeals` gate's own
  docstring, which leaves a record. A deleted gate leaves a hole, and a narrowed one
  reports fewer violations — indistinguishable from success.
- **Do not raise `[memory].budget_bytes` to satisfy `orientation_budget`.** Trim the
  documents. A budget raised to fit what is already there can only ever be satisfied,
  which is the exact failure that gate exists to prevent.
- **Do not add a value to `.ai/gates/data/corrections_vocab.json`** except as part of
  writing a real correction entry that uses it. That list is closed on purpose.
- **Do not reach for `--no-corrections`** unless the work in this diff genuinely carries
  no lesson worth one line. If it carries one, write the entry.
- **Do not mark a test `skip`/`xfail`, delete it, or loosen an assertion** to make
  pytest pass. If a test is genuinely wrong, fix the test properly and say so.
- **Do not commit, push, merge or open a PR.** Leave the working tree dirty; the
  operator reads the diff.
- **Do not install packages, download anything, or edit files outside this repo.**

If a failure needs a decision only the maintainer can make — a branch role, an
architectural choice, a rule that may itself be wrong — **stop, and say so under
`## Needs decision` in your report.** That is a successful outcome for this pass, and it
becomes a card they review rather than a silent guess.
"""

REPORT_HEADING = "## Report"
DECISION_HEADING = "## Needs decision"


@dataclass
class Diagnosis:
    """What is failing now, and what the tree looked like when we asked."""
    failed: list[preflight.Check] = field(default_factory=list)
    dirty: str = ""                      # `git status --porcelain`, for progress checks

    @property
    def green(self) -> bool:
        return not self.failed

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(c.name for c in self.failed))


def _dirty(root: Path) -> str:
    done = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    return done.stdout if done.returncode == 0 else ""


def _gate_violations(root: Path, limit: int = 30) -> str:
    """The gate runner's violation lines, without its summary.

    `preflight` records the *last* line of that output, which is the count plus all 21
    gate names — 400 characters that never say which gate failed. For a person reading a
    receipt that is fine; for an agent being handed the work it is the wrong 400
    characters, so this asks the runner again and keeps the lines that name paths.
    """
    done = subprocess.run([sys.executable, "-m", "nightshift.gates.run"], cwd=root,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    lines = [line for line in (done.stdout or "").splitlines()
             if line.strip() and "violation(s) across" not in line]
    if len(lines) > limit:
        lines = lines[:limit] + [f"... and {len(lines) - limit} more; run it yourself."]
    return "\n".join(lines)


def diagnose(root: Path, *, skip_tests: bool = False,
             no_corrections: str | None = None) -> Diagnosis:
    """Every failing check, from `preflight.run_checks` — the same authority the push
    guard consults, so this can never call green something preflight would refuse.

    `skipped` is not failure: on a repo with no board, three of the doctor's dispatch
    preconditions do not apply, and handing those to an agent would send it installing a
    CLI nobody asked for.
    """
    base = preflight.integration_base(root)
    result = preflight.run_checks(root, base, no_corrections, skip_tests=skip_tests)
    failed = [c for c in result.checks if not c.ok and not c.skipped]
    for check in failed:
        if check.name == "gates" and (violations := _gate_violations(root)):
            check.detail = violations
    return Diagnosis(failed=failed, dirty=_dirty(root))


def prompt(root: Path, diagnosis: Diagnosis) -> str:
    """The instruction for one round.

    Names the failures and how to reproduce them, and deliberately does not paste their
    output: the agent has a shell, and output it gathered itself is the only output it
    can trust after its own edit.
    """
    lines = [
        f"You are fixing a nightshift installation in `{root.as_posix()}`.",
        "",
        "The checks below are failing. For each: run the command, read the real output,",
        "fix the **cause**, re-run it. Work through all of them before you finish.",
        "",
        "## Failing checks",
        "",
    ]
    for check in diagnosis.failed:
        # Line structure preserved, not flattened: a gate reports one violation per line
        # with a path on it, and joining those into a paragraph destroys the only part an
        # agent can act on directly.
        kept = check.detail.strip().splitlines()[:30] or ["(no detail reported)"]
        lines += [f"### {check.name}", "", *kept, ""]
        if how := REPRODUCE.get(check.name):
            lines += [f"    {how}", ""]
    lines += [
        "`python -m nightshift.preflight` runs all of it and is what the push guard",
        "consults. That is the thing which has to end green.",
        "",
        PROHIBITIONS,
        "",
        f"Finish with a section headed `{REPORT_HEADING}`: what you changed and why,",
        "which checks now pass, and what you deliberately left alone. Be specific about",
        "what you did not fix — an unfixed check named plainly is worth more than a",
        "green summary.",
    ]
    return "\n".join(lines)


def _permission_mode(root: Path, override: str | None) -> str:
    return override or str(runner.host_setting(root, "permission_mode", "default"))


def can_dispatch(root: Path, permission_mode: str) -> str:
    """Empty string if a fix pass can run; otherwise the reason it cannot.

    Checked before dispatching, not after: a session that cannot run Bash cannot run a
    gate, so it cannot fix one — it would spend a round and a budget discovering that.
    """
    if permission_mode != "bypassPermissions":
        return (f"permission_mode is `{permission_mode}`, which cannot run Bash — the "
                f"agent could not run a gate, let alone fix one. Add "
                f"`--permission-mode bypassPermissions` for this pass only, or change it "
                f"in {AI_DIR}/hosts.json to make it standing.")
    if runner.claude_binary() is None:
        return ("the `claude` CLI was not found (checked $CLAUDE_BIN, PATH and "
                "~/.local/bin). Install it, or set CLAUDE_BIN.")
    return ""


def _model(root: Path) -> str:
    """The `lead` tier's model — this is judgment about a repo, not bounded work.

    Falls back to a default when the binding cannot be read, which is precisely the
    state of a repo whose tier document is one of the things needing repair.
    """
    try:
        return tiers.binding(root).get("lead", "") or "opus"
    except Exception:
        return "opus"


def dispatch(root: Path, text: str, round_no: int, *, permission_mode: str,
             timeout: int = ROUND_TIMEOUT_S) -> tuple[int, str]:
    """One round. Returns (exit code, the agent's final text).

    Goes through `runner._run_worker`, which is documented as the one place the Claude
    CLI is executed. A second call site would make that claim false, along with the test
    asserting it.
    """
    out_dir = root / RUNS / f"round-{round_no}"
    out_dir.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(out_dir / "prompt.md", text)

    binary = runner.claude_binary()
    assert binary, "can_dispatch() checked this"
    argv = [binary, "-p", text, "--model", _model(root),
            *runner._STREAM_ARGV, "--permission-mode", permission_mode]
    done = runner._run_worker(argv, root, timeout, out_dir / "stream.jsonl")
    result = runner._terminal_result(done.stdout or "")
    return done.returncode, str(result.get("result") or "").strip()


def decisions(final: str) -> list[str]:
    """The paragraphs an agent filed under `## Needs decision`, one per item.

    Parsed rather than inferred: the prompt asks for that heading by name, so its
    absence means the agent had nothing to escalate — which is a result, not a gap.
    """
    _, sep, tail = final.partition(DECISION_HEADING)
    if not sep:
        return []
    out: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            break                                   # the next section ends the list
        if not stripped:
            continue
        if stripped[0] in "-*" or (stripped[:2].rstrip(".").isdigit()):
            out.append(stripped.lstrip("-*0123456789. ").strip())
        elif out:
            out[-1] = f"{out[-1]} {stripped}"        # a wrapped continuation line
        else:
            out.append(stripped)
    return [item for item in out if item]


def file_cards(root: Path, items: list[str], *, round_no: int) -> list[Path]:
    """One card per escalated item, in `needs-decision/`.

    The maintainer's own answer to what a fix pass should do with a judgment call
    (2026-08-03): *"creating cards for what to fix, so the user can decide."* The board
    already has a lane whose whole meaning is "a human has to choose", and a finding
    that lands there gets read; the same finding in a run log does not.

    Each card carries an `## Approach` section, because that is the section the
    maintainer reads to decide whether work may proceed — left explicitly empty here,
    since the point of the lane is that nobody has chosen yet.
    """
    lane = board.board_dir(root) / "needs-decision"
    if not lane.is_dir():
        return []                        # no board in this repo: nothing to file into
    today = datetime.date.today().isoformat()
    written: list[Path] = []
    for n, item in enumerate(items, start=1):
        title = " ".join(item.split())
        slug = "-".join(
            "".join(ch if ch.isalnum() else " " for ch in title.lower()).split()
        )[:48].strip("-") or f"item-{n}"
        card = lane / f"fix-{slug}.md"
        if card.exists():
            continue                     # already filed by an earlier pass
        # Every field `card_schema` requires of a card outside `inbox/`, and the four
        # sections an actionable lane requires. Written in full rather than minimally:
        # a fix pass that files an invalid card has created the class of violation it
        # was dispatched to clear, and `tests/test_fix.py` runs the real gate over this.
        textio.write_text_lf(card, "\n".join([
            "---",
            f"id: fix-{slug}",
            f"title: {title[:100]}",
            "state: needs-decision",
            "tier: lead",
            "worker: none",
            "recipe: none",
            "unattended: false",
            f"created: {today}",
            "---",
            "",
            f"# {title[:100]}",
            "",
            "## Intent",
            "",
            f"Raised by `nightshift fix` (round {round_no}), which found this while "
            f"repairing the checks and declined to decide it alone.",
            "",
            item,
            "",
            "## Approach",
            "",
            "_Not chosen — that is what this lane means. Write the approach here, then "
            "move the card to `tasks/`._",
            "",
            "## Question",
            "",
            f"{item}",
            "",
            "**What was attempted.** `nightshift fix` ran the checks and repaired what it "
            "could. This one it did not touch.",
            "",
            "**What is ambiguous.** Stated above, in the agent's own words. It is quoted "
            "rather than paraphrased, because a paraphrase of an ambiguity resolves half "
            "of it by accident.",
            "",
            "**Candidate answers, and what each implies.** Not enumerated — the pass that "
            "raised this declined to, and a list invented afterwards would look like "
            "analysis while being a guess. Fill this in when you decide, or ask for the "
            "options with `python -m nightshift.fix --dry-run`.",
            "",
            "## Steps",
            "",
            "1. Decide. Record the decision and its reason in this card.",
            "2. If it implies work, write the approach above and move the card to "
            "`tasks/`.",
            "3. If it implies none, say why and move the card to `done/`.",
            "",
            "## Acceptance",
            "",
            "- The decision is written down here, with its reason.",
            "- Whatever it implies is either done or filed as its own card.",
            "",
            "## Open questions",
            "",
            f"- {item}",
            "",
        ]))
        written.append(card)
    return written


def report(diagnosis: Diagnosis) -> None:
    if diagnosis.green:
        print("  every check passes.")
        return
    print(f"  {len(diagnosis.failed)} failing check(s):")
    for check in diagnosis.failed:
        detail = " ".join(check.detail.split())
        print(f"    [FAIL] {check.name:<18} {detail}"[:110])


def loop(root: Path, *, permission_mode: str, skip_tests: bool = False,
         max_rounds: int = MAX_ROUNDS, no_corrections: str | None = None,
         dispatcher=dispatch) -> int:
    """Diagnose, dispatch, re-diagnose, until green or out of progress.

    `dispatcher` is injected so every stopping condition can be tested without a CLI, a
    budget or a night. Each of them is a decision, and a decision only a real dispatch
    can exercise is a decision nobody has checked.
    """
    diagnosis = diagnose(root, skip_tests=skip_tests, no_corrections=no_corrections)
    print("\n  diagnosis:")
    report(diagnosis)
    if diagnosis.green:
        return 0

    for round_no in range(1, max_rounds + 1):
        print(f"\n  round {round_no}/{max_rounds} — dispatching at the lead tier "
              f"({_model(root)})...")
        code, final = dispatcher(root, prompt(root, diagnosis), round_no,
                                 permission_mode=permission_mode)
        if final:
            print(f"\n{final}\n")
        for card in file_cards(root, decisions(final), round_no=round_no):
            print(f"  filed for your decision: {card.relative_to(root).as_posix()}")
        if code != 0:
            print(f"  the agent exited {code}; stopping rather than dispatching again "
                  f"on top of a failed run.")
            return 1

        after = diagnose(root, skip_tests=skip_tests, no_corrections=no_corrections)
        print(f"\n  after round {round_no}:")
        report(after)
        if after.green:
            print("\n  green. The tree is dirty on purpose — read the diff, then commit.")
            return 0
        # No check resolved *and* not one byte changed: the next round is this round.
        if after.names == diagnosis.names and after.dirty == diagnosis.dirty:
            print(f"\n  no progress and nothing changed on disk — stopping. The "
                  f"transcript is in\n  {RUNS}/round-{round_no}/stream.jsonl; this needs "
                  f"a human.")
            return 1
        diagnosis = after

    print(f"\n  still failing after {max_rounds} round(s). What is left is above; the "
          f"transcripts\n  are under {RUNS}/.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightshift fix",
        description="Run every check, then dispatch an agent to fix what failed. "
                    "Repeats until green, out of progress, or out of rounds.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to fix (default: the enclosing repository)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the diagnosis and the prompt; dispatch nothing")
    parser.add_argument("--skip-tests", action="store_true",
                        help="diagnose without running pytest — faster, less honest")
    parser.add_argument("--rounds", type=int, default=MAX_ROUNDS,
                        help=f"maximum dispatch rounds (default {MAX_ROUNDS})")
    parser.add_argument("--permission-mode", default=None,
                        help="override the host's permission_mode for this pass only")
    parser.add_argument("--no-corrections", metavar="REASON", default=None,
                        help="record an honest zero for the corrections check")
    args = parser.parse_args(argv)

    root = (args.root or find_root()).resolve()
    print(f"\n  nightshift fix — {root}")

    if args.dry_run:
        diagnosis = diagnose(root, skip_tests=args.skip_tests,
                             no_corrections=args.no_corrections)
        print("\n  diagnosis:")
        report(diagnosis)
        if diagnosis.green:
            return 0
        print(f"\n{'─' * 66}\n{prompt(root, diagnosis)}\n{'─' * 66}")
        print("\n  --dry-run: nothing dispatched.")
        return 0

    mode = _permission_mode(root, args.permission_mode)
    if reason := can_dispatch(root, mode):
        print(f"\n  cannot dispatch a fix pass: {reason}")
        print("\n  `--dry-run` still works, and prints exactly what would be asked.")
        return 2

    return loop(root, permission_mode=mode, skip_tests=args.skip_tests,
                max_rounds=args.rounds, no_corrections=args.no_corrections)


if __name__ == "__main__":
    raise SystemExit(main())
