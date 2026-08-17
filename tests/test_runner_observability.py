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
from nightshift import runner  # noqa: E402

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
    """Point `runner.GATE_ARGV` at a scripted stand-in for the gate suite.

    This used to write the stub to `.ai/gates/run.py`, which is how the suite
    stayed green while every real dispatch died: step 3 moved the gate runner
    into the `nightshift` package, and the fixture kept creating the file
    production no longer had. Substituting the argv follows the callee.
    """
    stub = root / "gate_stub.py"
    stub.write_text(body, encoding="utf-8")
    monkeypatch.setattr(runner, "GATE_ARGV", [sys.executable, str(stub)])


def test_a_crashing_gate_is_not_reported_as_the_card_failing(tmp_path, monkeypatch):
    """The `ice-damage` defect, reproduced.

    `run.py` exits 1 both for "violations found" and for an uncaught exception,
    so the exit code alone cannot separate them — and the traceback is on
    stderr, which the old `_run_gates` never read. The card was blamed, an
    attempt was spent, and its `## Error` recorded an empty string.
    """
    _fake_gates(monkeypatch, tmp_path, "raise TypeError('compile() arg 1 must be a string')\n")

    status, why = runner._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt")

    assert status == runner.GATE_CRASH
    assert why.strip() and why != "gates: ", "the reason must not be empty"
    assert "TypeError" in why
    # The full diagnosis has to reach disk too, not only the summary line.
    assert "Traceback" in (tmp_path / "gates.txt").read_text(encoding="utf-8")


def test_a_gate_reporting_violations_is_still_the_cards_problem(tmp_path, monkeypatch):
    """The other half of the split. If a real violation were excused as
    infrastructure the gates would stop enforcing anything at all."""
    _fake_gates(monkeypatch, tmp_path,
                "print('some/file.py:1 - a_gate: a real violation')\nraise SystemExit(1)\n")

    status, why = runner._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt")

    assert status == runner.GATE_VIOLATION
    assert "a real violation" in why


def test_a_gate_exiting_nonzero_in_silence_is_a_crash_not_a_verdict(tmp_path, monkeypatch):
    """Non-zero with nothing said is not a judgement about the card, and
    guessing that it is spends one of its three attempts for free."""
    _fake_gates(monkeypatch, tmp_path, "raise SystemExit(3)\n")

    status, _ = runner._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt")

    assert status == runner.GATE_CRASH


def test_clean_gates_pass(tmp_path, monkeypatch):
    _fake_gates(monkeypatch, tmp_path, "print('All clear')\n")

    assert runner._run_gates(tmp_path, tmp_path, tmp_path / "gates.txt") == \
           (runner.GATE_PASS, "")


def test_blocked_gives_the_attempt_back_and_keeps_the_branch(tmp_path):
    """A broken harness is not a fact about the card, so it costs no attempt.

    The branch must survive the rewind: unlike a usage limit — where the worker
    never ran — a blocked card's worker *did* run and commit, so discarding the
    branch would throw the work away along with the blame.
    """
    root = _repo(tmp_path)
    _card(root, "tasks", "probe")
    board.find(root, "probe").write(
        {"attempts": "1", "branch": "ai/probe", "started": runner._now()})

    note = runner.settle(root, "probe", runner.Dispatch("blocked", "the gate harness crashed"))

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

    tel = runner.read_telemetry(out)

    assert tel["rounds"] == 2
    assert tel["turns"] == 87
    assert tel["cost_usd"] == 4.0
    assert tel["wall_s"] == 1200.0
    assert tel["cache_read_tokens"] == 8_000_000
    assert tel["models"] == ["claude-sonnet-5"]

    rendered = runner.telemetry_markdown(tel, 1)
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

    assert runner.read_telemetry(out)["rounds"] == 1


def test_telemetry_is_empty_when_the_worker_never_reported(tmp_path):
    """A killed worker writes no JSON — that is exactly how
    `menu-hub-grid-layout` ended. It must degrade to "no telemetry" rather than
    raising inside `settle` and losing the real outcome."""
    out = tmp_path / "attempt-1"
    out.mkdir()

    assert runner.read_telemetry(out) == {}


def test_unparseable_worker_output_does_not_take_the_run_down(tmp_path):
    out = tmp_path / "attempt-1"
    out.mkdir()
    (out / "worker-1.json").write_text("not json at all", encoding="utf-8")

    assert runner.read_telemetry(out) == {}


def test_a_permission_denial_is_surfaced_and_a_zero_is_not(tmp_path):
    """A denial explains an attempt that otherwise looks inexplicably bad: the
    worker asked for a tool and was refused, with nobody to ask in `-p` mode. A
    permanent "none" line would train the eye to skip the line that matters."""
    out = tmp_path / "attempt-1"
    out.mkdir()
    _worker_json(out, 1, permission_denials=[{"tool": "Bash"}, {"tool": "Edit"}])
    assert "2 permission denial(s)" in runner.telemetry_markdown(
        runner.read_telemetry(out), 1)

    _worker_json(out, 1, permission_denials=[])
    assert "denial" not in runner.telemetry_markdown(runner.read_telemetry(out), 1)


def test_settle_writes_telemetry_onto_the_card(tmp_path):
    """It lives on the **card**, not only in `.ai/runs/`: the card is committed
    and syncs to Karel's other machine, while the run directory is gitignored
    and machine-local."""
    root = _repo(tmp_path)
    _card(root, "tasks", "probe")
    board.find(root, "probe").write({"attempts": "1", "started": runner._now()})
    out = runner.run_dir(root, board.find(root, "probe"), 1)
    _worker_json(out, 1)

    runner.settle(root, "probe", runner.Dispatch("review", "done"))

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
    runner._status(tmp_path, phase="worker", card="probe", attempt=2,
                   model="sonnet", since=runner._now())

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
    monkeypatch.setattr(runner, "_RUN_LOG", tmp_path / "no-such-dir" / "x.log")

    runner._log("still printed")

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
