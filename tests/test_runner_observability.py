"""Tests for the three defects the runner's first real night exposed — 2026-07-23.

That night ran three cards and produced one usable result. The other two were
recorded as failures they had not earned, and the reason each was invisible
matters as much as the bug:

* **`ice-damage`** did complete, correct work — a commit, 1533 tests passing —
  and was filed as failed with the reason `"gates: "`. Nothing after the colon.
  A gate had crashed on a `UnicodeDecodeError` and the traceback went to stderr,
  which `_run_gates` never read.
* **The dispatch order** ignored the Kanban column entirely, so the cards dragged
  to the top were not the cards that ran.
* **Nothing reported progress**, so a 28-minute card and a hung card looked
  identical from outside, and the numbers that would have explained either were
  downloaded and thrown away.

So these tests are about *observability under nobody watching* rather than about
features. Each one is anchored to something that actually happened.

The gate-harness tests drive `_run_gates` through the real subprocess boundary
with a stand-in `run.py`, because the whole defect lived in how a real process's
exit code and streams were read — a mock of that boundary would have agreed with
the broken version.

**Provenance.** Written in Dungeoneer's `tests/` and moved here by
`framework-tests-live-in-the-wrong-repo` (2026-08-08). Two of the original 22
tests stayed behind under the same filename: they run `subprocess_encoding` and
`deletion_sweep` against that repo's real tree, whose `core/i18n.py` carries the
245 unmappable bytes the incident was about. Those are claims about a project and
would assert nothing here; the mechanism — including that the gate catches every
way into text mode, and that a failed read cannot take the parser down — is
below.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

# The gate modules import their flat neighbours the way `gates/run.py` arranges,
# so the gate directory has to be importable before one is imported by bare name.
_CORE_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_CORE_GATES) not in sys.path:
    sys.path.insert(0, str(_CORE_GATES))

from nightshift import board  # noqa: E402
from nightshift import run_record  # noqa: E402
from nightshift import runner  # noqa: E402
from nightshift import hostconfig, outcome, settle, telemetry, verify

# The card and repo fixtures are the sibling suite's; rebuilding them here would
# be a second copy of the card template that could drift from the schema.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner_helpers import _card, _repo  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Dispatch order — the Kanban column is the priority
# ---------------------------------------------------------------------------

def test_dispatch_order_follows_the_kanban_column(tmp_path):
    """Karel drags a card to the top of `tasks/`; that becomes the dispatch order.

    Base Board writes `kanban_order` on every drag-reorder. Nothing read it
    until 2026-07-23 — `03_board.md` §2 filed it as "written by Obsidian, read
    by nobody here" — so the runner dispatched alphabetically by filename and
    the only prioritisation gesture the GUI offers did nothing at all.
    """
    _card(tmp_path, "tasks", "zebra", kanban_order="V0")
    _card(tmp_path, "tasks", "alpha", kanban_order="V1")
    _card(tmp_path, "tasks", "middle", kanban_order="V0l")

    order = [c.id for c in board.cards(tmp_path, "tasks")]

    # Alphabetically this would be alpha, middle, zebra. The point is that the
    # column wins, and that a fractional index sorts correctly as a plain string.
    assert order == ["zebra", "middle", "alpha"]


def test_cards_never_dragged_sort_after_the_ordered_ones(tmp_path):
    """A card only acquires `kanban_order` by being dragged, so its absence means
    "never prioritised". The runner writes fix-cards itself, and those must never
    jump ahead of work Karel deliberately moved to the top."""
    _card(tmp_path, "tasks", "dragged-last", kanban_order="V9")
    _card(tmp_path, "tasks", "aaa-never-dragged")
    _card(tmp_path, "tasks", "bbb-never-dragged")

    order = [c.id for c in board.cards(tmp_path, "tasks")]

    assert order == ["dragged-last", "aaa-never-dragged", "bbb-never-dragged"]


def test_order_is_stable_across_rescans(tmp_path):
    """The property filename-order was originally chosen for, and which the new
    key must not cost: the same order on every boot, so a crash-and-resume
    cannot reshuffle the night's work."""
    for n, order in (("a", "V2"), ("b", "V1"), ("c", "V3")):
        _card(tmp_path, "tasks", n, kanban_order=order)

    assert [c.id for c in board.cards(tmp_path, "tasks")] == \
           [c.id for c in board.cards(tmp_path, "tasks")] == ["b", "a", "c"]


# ---------------------------------------------------------------------------
# 2. A crashing gate is not the card's fault
# ---------------------------------------------------------------------------

def _fake_gates(monkeypatch, root: Path, body: str) -> None:
    """Point `verify.GATE_ARGV` at a scripted stand-in for the gate suite.

    This used to write the stub to `.ai/gates/run.py`, which is how the suite
    stayed green while every real dispatch died: step 3 moved the gate runner
    into the `nightshift` package, and the fixture kept creating the file
    production no longer had. Substituting the argv follows the callee.
    """
    stub = root / "gate_stub.py"
    stub.write_text(body, encoding="utf-8")
    monkeypatch.setattr(verify, "GATE_ARGV", [sys.executable, str(stub)])


def test_a_crashing_gate_is_not_reported_as_the_card_failing(tmp_path, monkeypatch):
    """The `ice-damage` defect, reproduced.

    `run.py` exits 1 both for "violations found" and for an uncaught exception,
    so the exit code alone cannot separate them — and the traceback is on
    stderr, which the old `_run_gates` never read. The card was blamed, an
    attempt was spent, and its `## Error` recorded an empty string.
    """
    _fake_gates(monkeypatch, tmp_path, "raise TypeError('compile() arg 1 must be a string')\n")

    status, why = verify._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt")

    assert status == verify.GATE_CRASH
    assert why.strip() and why != "gates: ", "the reason must not be empty"
    assert "TypeError" in why
    # The full diagnosis has to reach disk too, not only the summary line.
    assert "Traceback" in (tmp_path / "gates.txt").read_text(encoding="utf-8")


def test_a_gate_reporting_violations_is_still_the_cards_problem(tmp_path, monkeypatch):
    """The other half of the split. If a real violation were excused as
    infrastructure the gates would stop enforcing anything at all."""
    _fake_gates(monkeypatch, tmp_path,
                "print('some/file.py:1 - a_gate: a real violation')\nraise SystemExit(1)\n")

    status, why = verify._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt")

    assert status == verify.GATE_VIOLATION
    assert "a real violation" in why


def test_a_gate_exiting_nonzero_in_silence_is_a_crash_not_a_verdict(tmp_path, monkeypatch):
    """Non-zero with nothing said is not a judgement about the card, and
    guessing that it is spends one of its three attempts for free."""
    _fake_gates(monkeypatch, tmp_path, "raise SystemExit(3)\n")

    status, _ = verify._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt")

    assert status == verify.GATE_CRASH


def test_clean_gates_pass(tmp_path, monkeypatch):
    _fake_gates(monkeypatch, tmp_path, "print('All clear')\n")

    assert verify._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt") == \
           (verify.GATE_PASS, "")


def test_blocked_gives_the_attempt_back_and_keeps_the_branch(tmp_path):
    """A broken harness is not a fact about the card, so it costs no attempt.

    The branch must survive the rewind: unlike a usage limit — where the worker
    never ran — a blocked card's worker *did* run and commit, so discarding the
    branch would throw the work away along with the blame.
    """
    root = _repo(tmp_path)
    _card(root, "tasks", "probe")
    board.find(root, "probe").write(
        {"attempts": "1", "branch": "ai/probe", "started": hostconfig._now()})

    note = settle.settle(root, "probe", outcome.Dispatch("blocked", "the gate harness crashed"))

    after = board.find(root, "probe")
    assert after.lane == "tasks", "a blocked card must not move lanes"
    assert after.attempts == 0, "the attempt must be given back"
    assert after.fields.get("branch") == "ai/probe", "the work must survive the rewind"
    assert "given back" in note


# ---------------------------------------------------------------------------
# 3. Telemetry — numbers already paid for
# ---------------------------------------------------------------------------

def _worker_json(out: Path, n: int, **over: object) -> None:
    payload: dict[str, object] = {
        "duration_ms": 600_000, "duration_api_ms": 300_000, "num_turns": 40,
        "total_cost_usd": 2.0, "terminal_reason": "completed",
        "usage": {"output_tokens": 1000, "cache_read_input_tokens": 4_000_000,
                  "cache_creation_input_tokens": 50_000, "input_tokens": 76},
        "modelUsage": {"claude-sonnet-5": {}}, "permission_denials": [],
    }
    payload.update(over)
    (out / f"worker-{n}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_read_telemetry_sums_numbers_that_were_already_downloaded(tmp_path):
    """Every field here was already on disk and discarded — the runner read
    `total_cost_usd` out of this exact file and threw the rest away. Surfacing
    it costs no tokens, no API calls and no wall time."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1, num_turns=40)
    _worker_json(out, 2, num_turns=47)

    tel = telemetry.read_telemetry(out)

    assert tel["rounds"] == 2
    assert tel["turns"] == 87
    assert tel["cost_usd"] == 4.0
    assert tel["wall_s"] == 1200.0
    assert tel["cache_read_tokens"] == 8_000_000
    assert tel["models"] == ["claude-sonnet-5"]

    rendered = telemetry.telemetry_markdown(tel, 1)
    assert "87 turns" in rendered and "$4.00" in rendered
    # 8M cache reads is the number that looks alarming and is not. It has to be
    # legible at a glance rather than as eight digits.
    assert "8.00M cache-read" in rendered


def test_a_stderr_sidecar_is_not_mistaken_for_a_result(tmp_path):
    """`worker-1.stderr.txt` sits beside `worker-1.json` in the same directory.
    A glob that swept it in would count a round that never reported."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1)
    (out / "worker-1.stderr.txt").write_text("some warning\n", encoding="utf-8")

    assert telemetry.read_telemetry(out)["rounds"] == 1


def test_telemetry_is_empty_when_the_worker_never_reported(tmp_path):
    """A killed worker writes no JSON — that is exactly how
    `menu-hub-grid-layout` ended. It must degrade to "no telemetry" rather than
    raising inside `settle` and losing the real outcome."""
    out = tmp_path / "attempt-1"
    out.mkdir()

    assert telemetry.read_telemetry(out) == {}


def test_unparseable_worker_output_does_not_take_the_run_down(tmp_path):
    out = tmp_path / "attempt-1"
    out.mkdir()
    (out / "worker-1.json").write_text("not json at all", encoding="utf-8")

    assert telemetry.read_telemetry(out) == {}


def test_a_permission_denial_is_surfaced_and_a_zero_is_not(tmp_path):
    """A denial explains an attempt that otherwise looks inexplicably bad: the
    worker asked for a tool and was refused, with nobody to ask in `-p` mode. A
    permanent "none" line would train the eye to skip the line that matters."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1, permission_denials=[{"tool": "Bash"}, {"tool": "Edit"}])
    assert "2 permission denial(s)" in telemetry.telemetry_markdown(
        telemetry.read_telemetry(out), 1)

    _worker_json(out, 1, permission_denials=[])
    assert "denial" not in telemetry.telemetry_markdown(telemetry.read_telemetry(out), 1)


def _stage_log(out: Path, name: str, **over: object) -> None:
    """A checker/reviewer/repair/resolver's `*.log` — `proc.stdout + proc.stderr`,
    which for a real call is a `stream-json` JSONL blob whose last line is the
    terminal result. One line is enough here: `_terminal_result` only ever wants
    the last parseable object, and a one-line file is that trivially."""
    payload: dict[str, object] = {
        "duration_ms": 60_000, "duration_api_ms": 30_000, "num_turns": 10,
        "total_cost_usd": 0.5,
        "usage": {"output_tokens": 200, "cache_read_input_tokens": 500_000,
                  "cache_creation_input_tokens": 5_000, "input_tokens": 12},
        "modelUsage": {"claude-opus-5": {}},
    }
    payload.update(over)
    (out / name).write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_usage_breakdown_covers_every_stage_read_telemetry_does_not(tmp_path):
    """`read_telemetry`'s sibling: the worker's own numbers plus whatever the
    checker, the diff reviewer, a drift repair and a merge-conflict resolver
    left behind in the same attempt directory (`token-economy.md` phase 0.1) —
    each of those was parsed once for `total_cost_usd` at the live call and the
    rest discarded, same as the worker used to be."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1, num_turns=40)
    _stage_log(out, "review-1.log", num_turns=8, total_cost_usd=0.3)     # checker
    _stage_log(out, "review.log", num_turns=25, total_cost_usd=2.0)      # reviewer
    _stage_log(out, "repair.log", num_turns=5, total_cost_usd=0.1)
    _stage_log(out, "resolve-1.log", num_turns=3, total_cost_usd=0.05)   # resolver

    stages = {entry["stage"]: entry for entry in telemetry.usage_breakdown(out)}

    assert set(stages) == {"worker", "checker", "reviewer", "repair", "resolver"}
    assert stages["worker"]["turns"] == 40
    assert stages["checker"]["cost_usd"] == 0.3
    assert stages["reviewer"]["turns"] == 25
    assert stages["repair"]["cost_usd"] == 0.1
    assert stages["resolver"]["turns"] == 3


def test_usage_breakdown_does_not_confuse_the_checker_with_the_reviewer(tmp_path):
    """`review-1.log` (a checker round) and `review.log` (the diff reviewer) sit
    in the same directory under near-identical names — the one naming collision
    this file layout has to avoid, since the two are different agents judging
    different things."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _stage_log(out, "review-1.log", total_cost_usd=1.0)
    _stage_log(out, "review-2.log", total_cost_usd=1.0)
    _stage_log(out, "review.log", total_cost_usd=9.0)

    stages = {entry["stage"]: entry for entry in telemetry.usage_breakdown(out)}

    assert stages["checker"]["calls"] == 2
    assert stages["checker"]["cost_usd"] == 2.0
    assert stages["reviewer"]["calls"] == 1
    assert stages["reviewer"]["cost_usd"] == 9.0


def test_usage_breakdown_is_empty_for_an_attempt_that_left_nothing(tmp_path):
    out = tmp_path / "attempt-1"
    out.mkdir()
    assert telemetry.usage_breakdown(out) == []


def test_record_usage_writes_one_event_per_stage(tmp_path):
    """The single call site (`_settled`) reads whatever `usage_breakdown` found
    and writes it into the run record it already has open — nothing here
    re-parses a transcript or spawns anything."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1, num_turns=40)
    _stage_log(out, "review.log", num_turns=25, total_cost_usd=2.0)

    record = run_record.start(tmp_path, kind="run")
    telemetry.record_usage(record, out, card_id="probe", model="sonnet")

    data = run_record.read_all(tmp_path)[0]
    stages = {e["stage"] for e in data["usage"]}
    assert stages == {"worker", "reviewer"}
    assert all(e["card"] == "probe" for e in data["usage"])


def test_effort_is_recorded_per_stage_and_not_attempt_wide(tmp_path):
    """Stages run at their own tier's effort, so one attempt can carry two of
    them: a chore worker pinned to `medium` and a reviewer at the lead tier.
    An attempt-wide field would stamp one onto the other.

    `""` keeps meaning *no flag was passed, the CLI's own default applied* —
    which is a stage's honest answer for a tier the project declares no effort
    for, and is why this is asserted rather than left to a default.

    Before the field was populated from any call site, 2.2 and 3.3 had no
    observable trace in a run at all (`token-economy.md` §5, defect 4).
    """
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1, num_turns=40)
    _stage_log(out, "review.log", num_turns=25, total_cost_usd=2.0)

    record = run_record.start(tmp_path, kind="chores")
    telemetry.record_usage(record, out, card_id="probe", model="sonnet",
                        efforts={"worker": "medium"})

    by_stage = {e["stage"]: e for e in run_record.read_all(tmp_path)[0]["usage"]}
    assert by_stage["worker"]["effort"] == "medium"
    assert by_stage["reviewer"]["effort"] == "", "unnamed here, so nothing is claimed"


def test_an_unnamed_stage_records_no_effort_rather_than_inheriting_its_neighbour(tmp_path):
    """What a project declaring no `[tiers].effort` gets: `stage_efforts` resolves
    every tier to `""`, nothing passes `--effort`, and the record says so rather
    than borrowing the value from the stage next to it."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1, num_turns=40)

    record = run_record.start(tmp_path, kind="run")
    telemetry.record_usage(record, out, card_id="probe", model="sonnet")

    assert [e["effort"] for e in run_record.read_all(tmp_path)[0]["usage"]] == [""]


def test_settle_writes_telemetry_onto_the_card(tmp_path):
    """It lives on the **card**, not only in `.ai/runs/`: the card is committed
    and syncs to Karel's other machine, while the run directory is gitignored
    and machine-local."""
    root = _repo(tmp_path)
    _card(root, "tasks", "probe")
    board.find(root, "probe").write({"attempts": "1", "started": hostconfig._now()})
    out = telemetry.run_dir(root, board.find(root, "probe"), 1)
    _worker_json(out, 1)

    settle.settle(root, "probe", outcome.Dispatch("review", "done"))

    card = board.find(root, "probe")
    assert card.lane == "review"
    assert "## Telemetry" in card.text
    assert "40 turns" in card.text


# ---------------------------------------------------------------------------
# 4. The heartbeat
# ---------------------------------------------------------------------------

def test_status_is_readable_without_taking_the_lock(tmp_path, capsys):
    """`--status` is the question asked *while* a night is in flight, so it must
    not touch the lock, git or the board. A status command that could interfere
    with what it reports on would be worse than none."""
    hostconfig._status(tmp_path, phase="worker", card="probe", attempt=2,
                   model="sonnet", since=hostconfig._now())

    assert runner.print_status(tmp_path) == 0

    out = capsys.readouterr().out
    assert "worker" in out and "probe" in out
    assert not (tmp_path / ".ai" / "runs" / ".lock").exists()


def test_status_says_so_when_the_run_is_over(tmp_path, capsys):
    """A stale heartbeat is the normal state between runs, and `phase: worker`
    from a process that died an hour ago is precisely the misreading this
    command exists to prevent."""
    (tmp_path / ".ai" / "runs").mkdir(parents=True)
    (tmp_path / ".ai" / "runs" / "status.json").write_text(
        json.dumps({"pid": 999_999, "phase": "worker", "card": "probe"}), encoding="utf-8")

    runner.print_status(tmp_path)

    assert "NOT RUNNING" in capsys.readouterr().out


def test_status_before_any_run_is_not_an_error(tmp_path, capsys):
    assert runner.print_status(tmp_path) == 0
    assert "no status yet" in capsys.readouterr().out


def test_the_log_tee_survives_an_unwritable_path(tmp_path, monkeypatch, capsys):
    """The console is the primary record. A full disk or a locked file must not
    be the thing that ends a night."""
    monkeypatch.setattr(hostconfig, "_RUN_LOG", tmp_path / "no-such-dir" / "x.log")

    hostconfig._log("still printed")

    assert "still printed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5. The encoding class, as a property of the tree
# ---------------------------------------------------------------------------


def test_the_gate_catches_every_way_into_text_mode(tmp_path):
    """`universal_newlines=True` is the older alias and still works, so a gate
    that only knew `text=` would miss half the ways in."""
    import subprocess_encoding

    probe = tmp_path / ".ai"
    probe.mkdir()
    (probe / "probe.py").write_text(
        "import subprocess\n"
        "a = subprocess.run(['git'], text=True)\n"
        "b = subprocess.run(['git'], universal_newlines=True)\n"
        "c = subprocess.Popen(['git'], text=True)\n"
        "ok1 = subprocess.run(['git'], text=True, encoding='utf-8')\n"
        "ok2 = subprocess.run(['git'], capture_output=True)\n",
        encoding="utf-8")

    assert sorted(v.line for v in subprocess_encoding.check(tmp_path)) == [2, 3, 4]


def test_top_level_names_survives_what_a_failed_read_returns():
    """`ast.parse(None)` raises `TypeError`, which the original only-`SyntaxError`
    handler let escape — turning one bad decode into the whole gate run exiting
    non-zero with its diagnosis on stderr."""
    import deletion_sweep

    assert deletion_sweep._top_level_names(None) == set()
    assert deletion_sweep._top_level_names(b"def f(): pass") == set()
    assert deletion_sweep._top_level_names("def (") == set()


# --- one answer for both the spawn and the record (`token-economy.md` 3.3/4a) --
#
# Defect 4a was not a missing flag. It was a missing *shared* answer: `--effort`
# was chosen at the spawn site and the record was written at another, so a run
# could report an effort no stage received, or receive one it never reported, and
# a finished run gave no way to tell which. `stage_efforts` is the one answer both
# read, and these pin that it stays one.


def _effort_root(tmp_path, table: str = "") -> Path:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "t"\n\n[tiers]\nbinding_doc = "docs/tb.md"\n' + table,
        encoding="utf-8")
    doc = tmp_path / "docs" / "tb.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("```tier-binding\nworker = sonnet\nlead = opus\n```\n",
                   encoding="utf-8")
    return tmp_path


def test_stage_efforts_reads_each_stage_from_the_tier_it_actually_runs_at(tmp_path):
    """The checker and the drift repair are handed the *card's* model by their
    caller, so they take the card's effort; both resolvers and the diff reviewer
    resolve `lead` themselves, so they take the lead tier's."""
    root = _effort_root(tmp_path, '\n[tiers.effort]\nworker = "medium"\nlead = "high"\n')

    assert telemetry.stage_efforts(root, "worker") == {
        "worker": "medium", "checker": "medium", "repair": "medium",
        "reviewer": "high", "resolver": "high",
    }


def test_a_lead_tier_card_moves_its_own_stages_but_not_the_reviewer(tmp_path):
    """`tier: lead` on a card is about the work, not the review — the reviewer is
    the lead tier by definition either way."""
    root = _effort_root(tmp_path, '\n[tiers.effort]\nworker = "medium"\nlead = "high"\n')

    efforts = telemetry.stage_efforts(root, "lead")

    assert efforts["worker"] == "high"
    assert efforts["reviewer"] == "high"


def test_a_worker_override_moves_only_the_card_tier_stages(tmp_path):
    """The chore batch's case: it pins its own effort by name so a hand-edited
    `tier:` cannot move it, and that pin must not reach the reviewer — which is
    the stamping bug `record_usage`'s per-stage shape exists to prevent."""
    root = _effort_root(tmp_path, '\n[tiers.effort]\nworker = "high"\nlead = "high"\n')

    efforts = telemetry.stage_efforts(root, "worker", worker="low")

    assert efforts["worker"] == efforts["checker"] == efforts["repair"] == "low"
    assert efforts["reviewer"] == "high"


def test_a_project_with_no_effort_table_gets_no_flags_anywhere(tmp_path):
    """Every stage inherits, exactly as before 3.3. The safe-to-ship case."""
    root = _effort_root(tmp_path)

    assert set(telemetry.stage_efforts(root, "worker").values()) == {""}
