"""Everything `test_runner_*.py` builds its world out of — the dispatch loop's
fixtures, and the shared home for what the three modules have in common.

Tests for `runner.py`, `board.py` and `tiers`. The runner is the one component
that runs with nobody watching, so the things worth testing hard are the ones
whose failure is invisible until morning:

* **Selection** — every reason a card is *not* dispatched. A runner that
  silently skips work looks exactly like a quiet night, and telling those two
  apart is the first question anyone asks. → `test_runner_selection.py`
* **Crash recovery** — `attempts` is committed before the worker starts, so a
  reboot loop is bounded. This is the property the whole "resumable from disk
  alone" claim rests on. → `test_runner_selection.py`
* **The tier binding** — it is parsed out of the document `[tiers].binding_doc`
  names precisely so it cannot be restated in code, and a test that hard-coded a
  model alias would reintroduce the second copy the design exists to prevent. So
  the tests assert the *mechanism* (a card resolves to whatever the block says)
  and the invariants (every `card_schema` tier resolves; a missing block raises
  rather than guessing), never the value.
* **The forbidden bases** — an unattended process is exactly who would commit to
  a branch it must never build on.

No test spawns a worker. The dispatch call is a subprocess boundary; what
matters on this side of it is that every outcome settles the board correctly,
which is tested directly against `settle()`.

Every fixture below builds its own `tmp_path` project — a git repo, a manifest,
a `Board/` and a charter directory — so nothing reads a real tree. The manifests
say `name = "dungeoneer"` and declare `dungeoneer/assets/.tmp` as a harvest
directory because that is the project the incidents happened in; the value is
fixture content, and the tests prove the runner reads it from the manifest rather
than knowing the word.

**Why the tests are in three files and the fixtures are here.** This was one
5,884-line `test_runner.py` holding 315 tests and 260 s of runtime. Under
`-n auto --dist loadfile` an entire file is pinned to one worker, so that single
file *was* the suite's critical path: total work was 1,141 s across six workers,
which packs into ~190 s, and the suite took 267 s because one worker was busy for
260 of them. Nothing else could move the wall clock while that was true — a
change removing 48 s of CPU elsewhere moved it by four seconds.

So the split is by concern, and the fixtures came here rather than being copied
three times. **The autouse fixtures are imported by name in each module**, which
is what registers them with pytest; nothing references them — that is what
autouse means — so an import list built from what the tests mention drops them
silently. It did, on the first attempt: 27 tests failed with the real gate suite
running against a temp worktree because `_gates_pass_by_default` had not come
along.

**Provenance.** Written in Dungeoneer's `tests/` between 2026-07-23 and
2026-08-08 and moved here whole by `framework-tests-live-in-the-wrong-repo`. The
~9 assertions it made about *that* repo's real board, `hosts.json` and
tier-binding document stayed behind, in a file of the same name: they are claims
about a project, and a claim about a project asserts nothing once it is read
against the framework.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from nightshift import board
from nightshift import limits
from nightshift import run_record
from nightshift import runner

import _fixtures



# The runner's own source, for the AST guards below. Read off the installed
# module rather than `.ai/runner.py`: 07_portability.md §8 step 4 moved it into
# the package, and a guard that reads a path instead of the module it is about
# is the `fixture-created-the-file-production-lacked` shape.
_RUNNER_SOURCE = Path(runner.__file__)


CARD = """\
---
id: {id}
title: "{id} probe"
state: {lane}
tier: {tier}
worker: {worker}
recipe: none
unattended: {unattended}
verify: {verify}
created: 2026-07-23
{extra}---

## Intent

A probe card.

## Approach

Probe the thing directly, since that is what the card is for.

## Acceptance

- machine: the gates are green.

## Open questions

none
"""


def _card(root: Path, lane: str, card_id: str, *, tier: str = "worker",
          worker: str = "code-thread", unattended: str = "true",
          verify: str = "play", **extra: str) -> Path:
    lane_dir = root / "Board" / lane
    lane_dir.mkdir(parents=True, exist_ok=True)
    path = lane_dir / f"{card_id}.md"
    path.write_text(
        CARD.format(id=card_id, lane=lane, tier=tier, worker=worker,
                    unattended=unattended, verify=verify,
                    extra="".join(f"{k}: {v}\n" for k, v in extra.items())),
        encoding="utf-8")
    return path


def _charter(root: Path, name: str) -> None:
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")


def _build_repo(tmp_path: Path) -> None:
    _fixtures.git_init(tmp_path, email="t@t")
    (tmp_path / "Board").mkdir(exist_ok=True)
    # A manifest, because the fixture is a *consumer* of one. `[branches]` moved
    # out of `.ai/branches.py` in 07_portability.md §8 step 4, and the step's own
    # checklist names this trap: a hardcoded default moved behind a manifest field
    # changes behaviour for every caller that supplies no config, and a synthetic
    # fixture is exactly such a caller. Without this, `runner.preflight` raises
    # ManifestError instead of judging the base branch.
    _write_manifest(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=tmp_path, check=True)


def _repo(tmp_path: Path) -> Path:
    """A git repo with a board, so `git mv` and the commits are real.

    Built once per process and copied. The worktree fixtures below build on top
    of this copy and cut their worktrees per test — a *cut* worktree records an
    absolute gitdir and could never be copied, but the repo it is cut from can.
    """
    return _fixtures.repo_copy("runner-board-seed", tmp_path, _build_repo)


def _write_manifest(root: Path, integration: str = "development_team",
                    stable: str = "main",
                    forbidden_extra: tuple[str, ...] = ("dev", "master")) -> None:
    """The tables the runner reads. `[branches]` is parameterised so a test can
    simulate the branch-role migration by writing a different one, which is what
    the swap used to be tested by monkeypatching a module constant.

    `[worker]` mirrors this repo's real manifest because the dispatch fixtures
    below write art candidates into `dungeoneer/assets/.tmp` and expect
    `harvest()` to rescue them. Since 07_portability.md §8 step 4 that path is
    declared rather than hardcoded, so a fixture that omitted it would report
    "the worker produced nothing" — the exact confusing failure `harvest` exists
    to prevent, reproduced by the config rather than by the code.
    """
    extra = ", ".join(f'"{b}"' for b in forbidden_extra)
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "manifest.toml").write_text(
        f'[project]\nname = "dungeoneer"\nsource_dirs = ["dungeoneer"]\n\n'
        f'[branches]\nintegration = "{integration}"\nstable = "{stable}"\n'
        f"forbidden_extra = [{extra}]\n\n"
        f'[worker]\nharvest_dirs = ["dungeoneer/assets/.tmp"]\n'
        f'fence_env = "DUNGEONEER_FENCE_ALLOW"\n\n'
        # Declared, because the core default stopped being this project's plan doc
        # on 2026-08-02: it was `.claude/plans/ai_team/00_architecture.md` (the docs
        # moved to `.claude/memory/ai_team/` on 2026-08-06), a path
        # no other repo has — the `deferral-note-nobody-collected` coupling, fixed
        # in the *message* at step 5 and left in the *value*. A fixture that writes
        # the plan doc and relies on the default is exactly the "hardcoded default
        # moved behind a manifest field" caller §8 step 4's checklist warns about,
        # and this is the fourth time that item has fired.
        f'[tiers]\nbinding_doc = ".claude/memory/ai_team/00_architecture.md"\n',
        encoding="utf-8")


def _ignore_runs(root: Path) -> None:
    """Match production's `.gitignore:42` (`.ai/runs/`). Needed only by tests
    that write a run-dir file directly into the fixture and then call the real
    `runner.run()` — a real `dispatch()` never trips this because `.ai/runs/`
    files it writes are gitignored in the real repo; the bare test fixture has
    no `.gitignore` at all, so an unignored `.ai/runs/` would misread as
    `dirty_outside_board` and `preflight` would refuse to run."""
    (root / ".gitignore").write_text(".ai/runs/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore .ai/runs/"], cwd=root, check=True)


def _select(root: Path, capabilities: set[str] | None = None,
            bad: dict | None = None) -> dict[str, runner.Candidate]:
    return {c.card.id: c for c in runner.select(root, capabilities or set(), bad or {})}


@pytest.fixture(autouse=True)
def _no_xdist_in_fixtures(monkeypatch):
    """The dispatch tests run a real one-test acceptance suite in a tmp worktree.
    Spinning up xdist workers for a single test is pure per-invocation overhead
    (it turned this file's runtime from ~3 to ~5 min), so blank the parallel flags
    for fixtures. `test_run_tests_passes_the_parallel_and_junit_flags` restores
    them to prove production still parallelises."""
    monkeypatch.setattr(runner, "_PYTEST_PARALLEL", (), raising=False)
    _fixtures.serial_child_pytest(monkeypatch)


# --- the tier binding is read from §16, never restated ----------------------


def _binding_repo(tmp_path: Path, body: str) -> None:
    """A repo whose manifest points at a plan doc holding — or not holding — a block.

    The manifest line is load-bearing, not scaffolding. `[tiers].binding_doc`
    defaulted to this project's own plan doc until 2026-08-02 and now defaults to
    `docs/tier-binding.md`, so a fixture that writes the plan doc and declares
    nothing is asserting about a file nothing reads: both tests below would then
    pass on "the binding document is missing" and would keep passing if the block
    parser were deleted outright.
    """
    doc = tmp_path / ".claude" / "memory" / "ai_team"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "00_architecture.md").write_text(body, encoding="utf-8")
    (tmp_path / ".ai").mkdir(exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[tiers]\nbinding_doc = ".claude/memory/ai_team/00_architecture.md"\n',
        encoding="utf-8")


# --- a card grown past useful worker input ----------------------------------
#
# The card *is* the worker's opening message, so its size is the size of the
# input a worker is handed, and nothing bounds it since `worker-prompt-off-argv`
# retired the argv limit. The signal is deliberately advisory: gates here have
# one severity, so the blocking version would turn the board red on Karel using
# his own board, and that gate would get muted.

def _grow(path: Path, size: int) -> Path:
    """Pad a card past `size` bytes the way real cards grow — appended prose in
    a section, not a blob. Pure ASCII, so one character is one byte."""
    text = path.read_text(encoding="utf-8")
    filler = "Superseded history a worker still has to read first. "
    text += "\n## Thread\n\n" + filler * (2 + max(0, size - len(text)) // len(filler))
    path.write_text(text, encoding="utf-8")
    return path


# --- host capabilities ------------------------------------------------------

def _hosts(root: Path, mapping: dict) -> None:
    (root / ".ai").mkdir(exist_ok=True)
    (root / runner.HOSTS_FILE).write_text(json.dumps(mapping), encoding="utf-8")


# --- the whole cycle, end to end --------------------------------------------
#
# These drive a real dispatch — worktree, argv, verdict, gate/test run, card
# move, commit — with the CLI itself replaced at `_run_worker`, the one
# non-deterministic line in the file. Everything else is the production path.
# The point is that a wrong file move or a mis-settled outcome is found here, in
# eight seconds, rather than at 7 AM after a night that produced nothing.

def _gate_stub(monkeypatch, tmp_path: Path, body: str = "import sys; sys.exit(0)\n") -> Path:
    """Point `runner.GATE_ARGV` at a scripted stand-in for the gate suite.

    The fixture repo used to stub the harness by *writing* `.ai/gates/run.py`, and
    that is precisely why the suite could not see step 3 break production: the
    fixture created the very file the extraction had deleted, so the tests kept
    exercising a path that no longer existed in a real worktree. Substituting the
    argv keeps the seam pointed at whatever `_run_gates` actually invokes.

    Written *beside* the repo rather than inside it, for the same reason. It used
    to land in `tmp_path`, which is the fixture repo's root, and went unnoticed
    only because `_repo` ran `git add -A` afterwards and committed the harness's
    own scratch file along with the fixture. Once the repo became a copy of a
    template built before the stub existed, it showed up as an untracked file and
    the four `dirty_outside_board` tests started reporting `['gate_stub.py']` —
    the stub was never meant to be in the tree they inspect.
    """
    stub = tmp_path.parent / f"{tmp_path.name}-gate-stub.py"
    stub.write_text(body, encoding="utf-8")
    monkeypatch.setattr(runner, "GATE_ARGV", [sys.executable, str(stub)])
    return stub


@pytest.fixture(autouse=True)
def _gates_pass_by_default(monkeypatch, tmp_path):
    """Green gates unless a test says otherwise — what the `.ai/gates/run.py`
    stub in `_worktree_repo` used to provide. A test that wants red gates calls
    `_gate_stub(monkeypatch, tmp_path, <failing body>)` to replace this."""
    _gate_stub(monkeypatch, tmp_path)


def _worktree_repo(tmp_path: Path) -> Path:
    """A repo with a `development_team` branch, so `dispatch` can cut a real
    worktree and run its real acceptance step. The gate harness is stubbed by
    `_gates_pass_by_default` substituting `runner.GATE_ARGV` — deliberately NOT
    by writing `.ai/gates/run.py` here, which is what previously hid the gate
    runner's move behind a file only the fixture created.

    **The repo goes in a subdirectory of `tmp_path`, and that is load-bearing.**
    `runner.worktree_root` is `root.parent / f".{project}-worktrees"` — outside the
    repo on purpose, so `doc_scan`, `card_schema` and the digest do not walk it. Put
    the repo *at* `tmp_path` and that resolves to `tmp_path.parent`, which pytest
    shares between every test in the session (serially) or on the worker (under
    xdist). The project name is `dungeoneer` in every fixture manifest and the card
    id is `probe` in nearly every dispatch test, so all three components were
    constant and **every dispatch test in the suite resolved to one absolute path**:
    `tmp_path.parent/.dungeoneer-worktrees/probe`. `tmp_path` isolated the repo and
    not the worktree, because the worktree is deliberately not in the repo.

    Nothing failed while every test cleaned up after itself. One that did not —
    `test_an_api_error_before_verification_gives_the_attempt_back_and_hands_over`
    ends by calling `prepare_worktree` for an assertion about the returned *mode*
    and drops the checkout — left that shared path occupied, and then every later
    `probe` dispatch on the same worker died in `prepare_worktree`'s first lines:
    `git worktree add` refuses a non-empty directory, so `dispatch` returned
    `failed` before the worker ran, whatever the test was actually about. Measured
    2026-08-18: 21 failures in `test_runner_dispatch.py`, all on `gw1`, every one
    reporting `failed` including the cases expecting `limited` and `interrupted`.

    Invisible serially, because files run alphabetically and `dispatch` sorts before
    `review`, so the leak always landed after its victims. `--dist loadfile` hands a
    file to whichever worker frees up, which made it a coin flip per run.

    One extra directory level is the whole fix: `root.parent` becomes `tmp_path`,
    which pytest already guarantees is unique per test.
    """
    root = _repo(tmp_path / "repo")
    subprocess.run(["git", "branch", "-M", "development_team"], cwd=root, check=True)
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    # Match production: art candidates and run dirs are gitignored, so an art
    # card's `.tmp/` output never registers as an uncommitted diff. Without this
    # the WIP-rescue path would stage those candidates in the fixture and the
    # artefact-counts-as-output test would reach review via a commit instead.
    (root / ".gitignore").write_text(
        "dungeoneer/assets/.tmp/\n.ai/runs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "scaffold"], cwd=root, check=True)
    return root


def _fake_worker(monkeypatch, *, verdict: dict | None = None, commit: bool = True,
                 returncode: int = 0, cost: float = 0.11, stderr: str = ""):
    """Stand in for the CLI: optionally commit on the branch, optionally write a
    verdict, and report a cost the way `--output-format json` does."""
    seen: dict = {}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        seen["argv"] = argv
        seen["cwd"] = Path(cwd)
        if commit:
            (Path(cwd) / "worked.txt").write_text("done\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
            subprocess.run(["git", "commit", "-qm", "worker: did the thing"], cwd=cwd, check=True)
        if verdict is not None:
            target = next(a for i, a in enumerate(argv) if a.endswith(".json")) \
                if any(a.endswith(".json") for a in argv) else None
            path = Path(target) if target else None
            if path is None:  # the path is named in the prompt, not the argv
                path = Path(next(l.strip() for l in prompt.splitlines()
                                 if l.strip().endswith(".json")))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(verdict), encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, returncode, json.dumps({"total_cost_usd": cost}), stderr)

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    return seen


# --- the night's stopping conditions ----------------------------------------
#
# These drive `run()` with `dispatch` replaced, because what is under test is the
# loop's arithmetic — how many windows have been spent, whether the same card is
# retried, when the night ends — and not what any one dispatch does.

def _tier_binding(root: Path) -> None:
    """The §16 block `tiers.resolve` reads. Real, not stubbed — the binding being
    parsed out of the doc rather than hardcoded is the guarantee that no caller
    names a model, and a test that patched it away would stop checking that."""
    doc = root / ".claude" / "memory" / "ai_team"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "00_architecture.md").write_text(
        "```tier-binding\nworker = sonnet\nlead = opus\n```\n", encoding="utf-8")


def _loaded_board(tmp_path: Path, *card_ids: str) -> Path:
    """A committed repo with a tier binding, a charter and some ready cards."""
    root = _worktree_repo(tmp_path)
    _charter(root, "code-thread")
    _tier_binding(root)
    for card_id in card_ids:
        _card(root, "tasks", card_id)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "cards"], cwd=root, check=True)
    return root


def _night(monkeypatch, root: Path, outcomes: list[runner.Dispatch]) -> list[str]:
    """Run one night with a scripted sequence of dispatch outcomes. Returns the
    card ids dispatched, in order — the last one repeating means a wall sent the
    loop back to the same card, which is the property most of these check."""
    calls: list[str] = []
    script = list(outcomes)

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        return script.pop(0) if script else runner.Dispatch("review", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "_sleep_until", lambda when: True)
    monkeypatch.setattr(runner, "settle", lambda r, cid, result: f"{cid}: {result.outcome}")
    return calls


def _wall(scope: str = limits.SESSION) -> runner.Dispatch:
    return runner.Dispatch("limited", "usage limit reached", 0.0, 1,
                           limits.Wall(scope, None, "usage limit reached"))


# --- a failed attempt's commits are preserved, not deleted (failed-attempt- ---
# --- work-is-deleted-not-resumed) ----------------------------------------- --
#
# Where the work used to die: not at the moment of failure (`drop_worktree`
# already kept the branch) but at the *next* dispatch's cold start, where
# `git branch -D` discarded it before this card existed. A test that only
# checks the failing attempt would pass against the old code too — the
# assertion has to be made after a second dispatch.

def _rev(root: Path, ref: str) -> str:
    return subprocess.run(["git", "rev-parse", ref], cwd=root,
                          capture_output=True, text=True).stdout.strip()


# A rescue ref lives under `refs/heads/ai/`, which is exactly the namespace
# `publish` pushes wholesale — so on a host with `publish_remote` set every
# rescue branch gets a copy on the remote, and a reap that only deleted the
# local half would leave up to `MAX_ATTEMPTS` orphaned `origin/ai/<id>@failed-N`
# refs per retired card. That is the gap `rebase_and_merge` closed for card
# branches on 2026-08-09, re-opened by a new ref shape. Against a real bare
# remote, per the 2026-07-28 lesson.

def _host_publishes_to_origin(root: Path) -> None:
    (root / ".ai").mkdir(parents=True, exist_ok=True)
    (root / ".ai" / "host.json").write_bytes(
        json.dumps({"publish_remote": "origin"}).encode("utf-8"))


# --- repo drift vs the card's own doing (failed-attempt-work-is-deleted-not-resumed) --
#
# A gate violation whose every violating path lies outside this attempt's own
# diff is not a fact about the card — it is the same category as a crashed
# harness, one notch less severe, and gets the same `blocked` give-back. The
# classifier reads `Violation`'s structured `file`/`line`/`rule` via
# `nightshift.gates.run --json`, never a regex over `gates.txt` and never the
# gate's name — these stub gates are deliberately unnamed/unknown ones, to
# prove the classifier does not special-case a particular gate.

_JSON_AWARE_GATE = '''
import json, sys
if "--json" in sys.argv:
    print(json.dumps({{"violations": [{{"gate": "stub", "file": "{file}", "line": 1,
                                       "rule": "{rule}"}}], "total": 1, "gates": ["stub"]}}))
else:
    print("{file}:1 — {rule}")
sys.exit(1)
'''


# --- the producer/checker loop (00_architecture.md §16, "the second seam") ---
#
# The loop lives in the runner rather than inside the producer, because every
# step of it is a file-state lookup or an integer comparison — which is exactly
# the work §5 says belongs there. These tests pin the four properties that move
# gained: the bound is a `range()` and not a sentence in a charter; the checker's
# blindness is structural; both tiers go through the dispatcher; and feedback
# reaches the next round.

def _fake_loop(monkeypatch, verdicts: list[str], *, notes: str = "too dark"):
    """Drive N rounds: the producer writes a candidate, the checker returns the
    next verdict in the list. Records what each side was actually shown."""
    seen: dict = {"producer_prompts": [], "checker_prompts": [], "calls": []}
    pending = list(verdicts)

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        agent = argv[argv.index("--agent") + 1]
        seen["calls"].append(agent)
        if agent == "art":
            seen["producer_prompts"].append(prompt)
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "cand.png").write_bytes(b"\x89PNG")
        else:
            seen["checker_prompts"].append(prompt)
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(
                {"verdict": pending.pop(0), "best": "cand.png", "notes": notes}),
                encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": 0.1}), "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    return seen


def _art_card(root: Path, card_id: str = "icon") -> None:
    _charter(root, "art")
    _charter(root, "art-reviewer")
    _card(root, "tasks", card_id, worker="art", checker="art-reviewer")


# --- the guarantee the whole design rests on --------------------------------

def _functions_spawning(source: str, callee: str) -> set[str]:
    """Names of the top-level functions that build an argv starting with
    `callee`. AST rather than text search, so a mention in a docstring or a
    comment does not count — the question is what the code *executes*."""
    import ast

    tree = ast.parse(source)
    found: set[str] = set()
    for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
            if not (isinstance(node, ast.List) and node.elts):
                continue
            first = node.elts[0]
            if isinstance(first, ast.Name) and first.id == callee:
                found.add(function.name)
    return found


# Spawn sites whose wall path is allowed NOT to consult
# `verdict_survives_a_wall`. Explicit and empty on purpose: a stage that should
# never honour a pre-wall artefact is a real possibility, but it has to be
# written down here with a reason rather than left as an omission nobody notices.
_WALL_HANDLING_EXEMPT: frozenset = frozenset()


def _functions_calling(source: str, callee: str) -> dict:
    """How many times each top-level function calls `callee` by name. AST, so a
    mention in a docstring or a comment does not count."""
    import ast

    tree = ast.parse(source)
    counts: dict = {}
    for function in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        found = sum(
            1 for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == callee)
        if found:
            counts[function.name] = found
    return counts


# --- warm resume of a limit-interrupted worker (runner-worker-handover) -------
#
# Before this, a worker walled mid-stream lost everything: the worktree was
# force-removed the instant a wall was seen, it had committed nothing, and the
# next attempt cold-started from a blank prompt. These drive the three
# preservation paths — resume the session, re-enter the kept worktree, recover
# from a WIP commit — plus the progress gate that stops a card that cannot
# advance from being given its attempt back forever. All over synthetic state;
# none spawns a real worker.

def _walls_dirty(monkeypatch, *, session_id: str = "sess-1", content: str = "draft",
                 record: list | None = None):
    """A worker that does real, uncommitted work (writes a file into the worktree)
    and *then* hits a usage-limit wall — the 45-turns-then-walled case the card
    was written for. Its result JSON carries the `session_id`, exactly as the CLI
    reports it, so the runner can resume it."""
    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        if record is not None:
            record.append({"argv": argv, "prompt": prompt})
        (Path(cwd) / "wip.txt").write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 1, json.dumps({"total_cost_usd": 0.5, "session_id": session_id}),
            "Claude AI usage limit reached")
    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")


def _finishing_worker(monkeypatch, *, record: list | None = None,
                      resume_returncode: int = 0, resume_stderr: str = "",
                      session_id: str = "sess-2"):
    """A worker that finishes the card: commits, writes a `done` verdict. If
    `resume_returncode` is non-zero it fails *only* the `--resume` invocation (a
    dead session) and succeeds on the fresh re-entry, to drive the fallback."""
    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        if record is not None:
            record.append({"argv": argv, "prompt": prompt})
        if "--resume" in argv and resume_returncode != 0:
            return subprocess.CompletedProcess(
                argv, resume_returncode, json.dumps({"total_cost_usd": 0.05}), resume_stderr)
        (Path(cwd) / "wip.txt").write_text("final", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-qm", "worker: finished"], cwd=cwd, check=True)
        verdict = Path(next(l.strip() for l in prompt.splitlines()
                            if l.strip().endswith(".json")))
        verdict.parent.mkdir(parents=True, exist_ok=True)
        verdict.write_text(json.dumps({"outcome": "done", "summary": "finished"}),
                           encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"total_cost_usd": 0.2, "session_id": session_id}), "")
    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")


def _seed_wip_branch(root: Path, tmp_path: Path, card_id: str, content: str) -> None:
    """Synthetic disk-cap demotion state: branch ai/<id> carries a `wip:` commit,
    a handover marker is on disk, and the worktree directory is absent — exactly
    what runner-prune-run-dirs's demotion leaves behind."""
    seed = tmp_path / f"seed-{card_id}"
    subprocess.run(["git", "worktree", "add", "-b", f"ai/{card_id}", str(seed),
                    "development_team"], cwd=root, check=True)
    (seed / "wip.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-qm", f"wip: {card_id} interrupted"], cwd=seed, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(seed)], cwd=root, check=True)
    runner.write_handover(root, card_id,
                          runner.Handover(session_id="", diff_hash="stale", no_progress=0))
# --- the review stage (automate-review-step) --------------------------------
#
# A new, distinct always-on stage that runs after gates+tests pass: the runner
# builds the diff reviewer's context, spawns it, and turns the two-way verdict
# into a routed lane — needs-decision/ (a choice for Karel) or merge → testing/
# (nothing needs his judgment first). These drive the routing over synthetic
# state: the reviewer is stubbed (no live dispatch) and the merge is stubbed (no
# live merge), the way every other runner test replaces its one non-deterministic
# seam. The safety property the whole card turns on — nothing reaches done/ from
# here, only via testing/ — has its own test at the end.

def _reviewed_branch(root: Path, tmp_path: Path, card_id: str = "probe",
                     *, worker: str = "code-thread", commit: bool = True,
                     verify: str = "play") -> board.Card:
    """A card in tasks/ whose branch `ai/<id>` carries a real commit — the state a
    card is in when the review stage runs (dispatch has finished, settle has not).
    With `commit=False` the branch is level with base, standing in for an
    artefact-only card (art) that has no diff to review."""
    _charter(root, worker)
    _card(root, "tasks", card_id, worker=worker, verify=verify)
    subprocess.run(["git", "branch", f"ai/{card_id}", "development_team"], cwd=root, check=True)
    if commit:
        seed = tmp_path / f"seed-{card_id}"
        subprocess.run(["git", "worktree", "add", str(seed), f"ai/{card_id}"],
                       cwd=root, check=True)
        (seed / "feature.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=seed, check=True)
        subprocess.run(["git", "commit", "-qm", "worker: did the thing"], cwd=seed, check=True)
        subprocess.run(["git", "worktree", "remove", "--force", str(seed)], cwd=root, check=True)
    card = board.find(root, card_id)
    card.write({"branch": f"ai/{card_id}", "attempts": "1"})
    return board.find(root, card_id)


def _stub_reviewer(monkeypatch, verdict: dict, cost: float = 0.2,
                   wall: "limits.Wall | None" = None) -> list:
    spawned: list = []

    def fake(root, label, out_dir, model, base, branch, card_budget, timeout,
             *, criteria, intent):
        spawned.append(branch)
        return verdict, cost, wall

    monkeypatch.setattr(runner, "review_branch", fake)
    return spawned


# --- a wall on a stage's wrap-up must not discard the verdict it already wrote -
#
# `wall-on-review-wrapup-discards-a-verdict`. The measured instance (2026-08-09,
# `menu-art-cyberware`): the round-2 art-reviewer wrote `review-2.json` — verdict
# `pass`, best `menu_card_cyberware_s30.png`, with its reasoning — and *then* hit
# a usage wall on its own wrap-up call. The runner logged "not attempted, attempt
# given back", byte-identical to what it logs when a reviewer dies before writing
# anything, and the verdict was recovered by hand out of a gitignored directory.
#
# The defect is an ordering, not durability: every spawn function returns
# `(verdict, cost, wall)` and each site asked "did the process end cleanly?"
# before "did this stage produce a usable verdict?", with the verdict already
# parsed into a local variable on the return path that ignored it. So these tests
# never read anything back off `.ai/runs/` — if a fix ever needs to, it has
# inherited the parent card's durability problem and is the wrong fix.

def _fake_loop_walling(monkeypatch, verdicts: list[str], *, wall_at: int = 1,
                       write_verdict: bool = True, notes: str = "looks right"):
    """`_fake_loop`, except the checker's call on round `wall_at` returns the
    CLI's usage-limit exit — *after* writing its verdict, which is the shape that
    was measured. `write_verdict=False` is the other half: a checker that walled
    before writing anything, which must still give the attempt back.

    Driven through `_run_worker` rather than by stubbing `run_checker`, so the
    real `_read_verdict` and the real `limits.detect` are both on the path: a
    wall genuinely *is* a non-zero exit here, which is what made it read as the
    card's fault in the first place.
    """
    seen: dict = {"calls": []}
    pending = list(verdicts)
    rounds = {"checker": 0}

    def fake(argv, cwd, timeout, stream_path=None, env=None, prompt=""):
        agent = argv[argv.index("--agent") + 1]
        seen["calls"].append(agent)
        if agent == "art":
            tmp = Path(cwd) / "dungeoneer" / "assets" / ".tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            (tmp / "cand.png").write_bytes(b"\x89PNG")
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"total_cost_usd": 0.1}), "")
        rounds["checker"] += 1
        walled = rounds["checker"] == wall_at
        if write_verdict or not walled:
            target = Path(next(l.strip() for l in prompt.splitlines()
                               if l.strip().endswith(".json")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(
                {"verdict": pending.pop(0), "best": "cand.png", "notes": notes}),
                encoding="utf-8")
        return subprocess.CompletedProcess(
            argv, 1 if walled else 0, json.dumps({"total_cost_usd": 0.1}),
            "Claude AI usage limit reached" if walled else "")

    monkeypatch.setattr(runner, "_run_worker", fake)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    return seen


# --- the night still treats an honoured wall as a wall ------------------------

def _landed_wall(scope: str = limits.SESSION) -> runner.Dispatch:
    """A card that *landed* — gates and tests passed, the verdict was honoured —
    on a dispatch that nonetheless met the wall."""
    return runner.Dispatch("review", "ok", 0.0, 1,
                           limits.Wall(scope, None, "usage limit reached"))


# --- ensure_workspace_trusted -------------------------------------------------
# Headless `claude -p` has no trust dialog, so an untrusted workspace does not
# refuse — it silently drops the worker's permissions.allow / additionalDirectories
# entries (2026-07-25). The invisible-failure shape this whole file exists to
# guard, so the flag-writing is tested directly.

def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path / ".claude.json"


# --- the worktree fence: a worker writes only in its worktree -----------------
#
# Prevention half (`.ai/hooks/worktree_fence.py::evaluate`) plus the runner's
# guarantee half (`assert_integration_unmoved`). Together they answer the
# 2026-07-25 wrong-checkout defect: a worker that commits game code onto the
# shared integration branch from the canonical checkout.

def _roots(*paths: Path) -> list[Path]:
    return [p.resolve() for p in paths]


# --- the JUnit-report verdict (runner-test-selection) -------------------------
#
# Karel's design: the python suite writes the record, the runner checks it. The
# XML is authoritative, and a zero-collected report is a failure, not a pass —
# the silent-green a mis-selection would otherwise produce.

def _junit(path: Path, *, tests: int, failures: int = 0, errors: int = 0) -> Path:
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="pytest" tests="{tests}" '
        f'failures="{failures}" errors="{errors}" skipped="0"></testsuite></testsuites>',
        encoding="utf-8")
    return path


# --- runner-hardening #3: the dedicated integration checkout ----------------
#
# The topology: the runner keeps `development_team` in its own sibling worktree so
# Karel's launch checkout can sit on his own branch and he can keep coding during a
# run. The board and every git operation happen in that dedicated checkout; the
# control plane (STOP, status, lock, log) stays with the launch checkout, where he
# reaches it. Reviewed-ok cards are rebased onto the current integration tip and
# re-verified before they merge, so a same-night sibling edit surfaces as a
# conflict a human resolves rather than a silent clobber.

def _split_repo(tmp_path: Path, *card_ids: str,
                work_branch: str = "karel/work") -> tuple[Path, Path]:
    """A repo whose board lives (committed) on `development_team` while the launch
    checkout has moved to `work_branch` — the #3 topology. Nested one level under
    `tmp_path` so the sibling `.dungeoneer-integration` / `.dungeoneer-worktrees`
    dirs are unique per test. Returns (launch_root, dedicated_checkout_path); the
    dedicated checkout is NOT created here — `run()`/`ensure_integration_checkout`
    does that."""
    main = tmp_path / "main"
    main.mkdir()
    root = _worktree_repo(main)          # on development_team, with gate stub + tests
    _charter(root, "code-thread")
    _tier_binding(root)
    for cid in card_ids:
        _card(root, "tasks", cid)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "board + config on development_team"],
                   cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", work_branch], cwd=root, check=True)
    return root, runner.integration_checkout_path(root)


# --- rebase-then-merge (decision #2) ----------------------------------------

def _branch_with_file(root: Path, tmp_path: Path, branch: str, name: str, body: str) -> None:
    """Create `branch` off development_team carrying one file, via a throwaway worktree."""
    subprocess.run(["git", "branch", branch, "development_team"], cwd=root, check=True)
    wt = tmp_path / f"seed-{branch.replace('/', '-')}"
    subprocess.run(["git", "worktree", "add", str(wt), branch], cwd=root, check=True)
    (wt / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", f"{branch}: {name}"], cwd=wt, check=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=root, check=True)


def _commit_on_base(root: Path, tmp_path: Path, name: str, body: str) -> None:
    """Advance development_team with one file, in the checkout it is on."""
    (root / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", f"development_team: {name}"], cwd=root, check=True)


# --- ...and deleting the remote copy too (Karel, 2026-08-09) ----------------
#
# `publish` creates `<remote>/ai/<id>`; until this existed, nothing ever removed
# it, so with `publish_remote` set every merged card orphaned a remote branch
# forever (`origin/ai/cyberware-next-level-preview` is the live instance).
# Against a real bare remote, per the 2026-07-28 lesson that a mocked push hid
# the behaviour that mattered.

def _remote_has(bare: Path, branch: str) -> bool:
    return runner._git(bare, "rev-parse", "--verify", f"refs/heads/{branch}").returncode == 0


def _advance_on_remote(bare: Path, tmp_path: Path, branch: str, name: str, body: str) -> str:
    """Another machine pushes a commit onto `branch` after this checkout last
    fetched — work the local branch has never contained."""
    clone = tmp_path / f"other-machine-{branch.replace('/', '-')}"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    subprocess.run(["git", "checkout", "-q", branch], cwd=clone, check=True)
    (clone / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-qm", f"{branch}: {name} (elsewhere)"],
                   cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=clone, check=True)
    return runner._git(bare, "rev-parse", f"refs/heads/{branch}").stdout.strip()


# --- publish: pushing so a cloud run is pullable anywhere -------------------
#
# The runner commits everything locally — the board, merged cards on
# `development_team`, and each card's `ai/<id>` branch — which was always
# enough on Karel's laptop, where the checkout *is* his repo. In the cloud
# topology (`night.py`) the checkout is an ephemeral clone: a card parked in
# `needs-decision/` leaves its branch stranded in a container he cannot reach.
# `publish()` closes that gap; these tests are against a bare `origin` so a
# push is a real, checkable git operation, not a mock.

def _bare_origin(root: Path, tmp_path: Path, name: str = "origin.git") -> Path:
    """A bare repo wired as `origin`, so `publish()`'s pushes are real and their
    result can be inspected independently of the working checkout."""
    bare = tmp_path / name
    # gate-ok(fixture_rebuild): a *bare* repo, which the shared template does not
    # offer — `_fixtures.git_init` builds a working tree, and a bare origin has no
    # tree to build. Nor would copying one help: each caller wires it as its own
    # remote, which is the absolute path a copy could not carry.
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=root, check=True)
    return bare


def _remote_tip(bare: Path, ref: str) -> str:
    return runner._git(bare, "rev-parse", ref).stdout.strip()


# --------------------------------------------------------------------------
# The run records itself (`digest-reports-the-run`, 2026-07-30)
# --------------------------------------------------------------------------
#
# The digest reports runs, not lane movement, so what the run writes down is now
# load-bearing: a dispatch the record misses is a dispatch that never happened as
# far as the morning is concerned. These are the end-to-end checks that `run()`
# actually fills the record in — `test_board_run_record.py` covers the module.


def _only_record(root: Path) -> dict:
    files = sorted((root / run_record.DIR).glob("*.json"))
    assert len(files) == 1, f"expected one record, got {[f.name for f in files]}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# --append-digest (Karel, 2026-07-30): "for when I don't have time to read or
# use a scheduler over weekend" — a run can write its digest without moving the
# read baseline, so a run started later still reports back to the last time the
# baseline genuinely advanced. `test_board_digest.py` pins the windowing
# contract (`_previous_digest` skipping a non-matching commit message); these
# pin that `runner.py` actually produces that commit shape.
# --------------------------------------------------------------------------


def _last_commit_subject(root: Path) -> str:
    out = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=root,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _fake_review_run(monkeypatch, root: Path, card_id: str) -> None:
    """One card, dispatched by name and landed as a real `reviewed` outcome —
    genuinely moved to `testing/` via `runner.settle`, not the `_night` helper's
    no-op settle stub (which never moves a card, and would make an "is this card
    still individually named in the report" assertion meaningless). `dispatch`
    returns `reviewed` directly rather than `review`, so `run()`'s own
    `if result.outcome == "review": result = review_stage(...)` step — which
    would otherwise spawn a real reviewer subprocess — never triggers.
    `rebase_and_merge` is stubbed to succeed, same as
    `test_settle_reviewed_merges_and_lands_in_testing`.
    """
    monkeypatch.setattr(runner, "dispatch",
                        lambda root_, card, base, model, card_budget, test_timeout:
                        runner.Dispatch("reviewed", "ok"))
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    monkeypatch.setattr(runner, "rebase_and_merge",
                        lambda r, card, branch, base, test_timeout=600, remote="":
                        (True, "merged"))


# --------------------------------------------------------------------------
# A crash in one card's pipeline is that card's failure
# (`one-crashed-card-kills-the-night`, 2026-08-07)
# --------------------------------------------------------------------------
#
# An exception raised while dispatching one card used to propagate out of
# `dispatch` → `run` → `main` and exit 1, abandoning every remaining card: on
# 2026-08-06 one `FileNotFoundError: [WinError 206]` from an oversized card's
# `Popen` cost the whole night. These drive `run()` with a stage made to *raise*,
# because a test that only asserts the tree is currently fine proves nothing
# about containment.


def _crashing_night(monkeypatch, root: Path, boom: BaseException,
                    on_card: str = "b", *, real_settle: bool = False) -> list[str]:
    """One night in which `dispatch` raises `boom` for `on_card`. Returns the ids
    it was called with, so the assertion is "the loop carried on past the crash"
    rather than "nothing blew up"."""
    calls: list[str] = []

    def fake_dispatch(root_, card, base, model, card_budget, test_timeout):
        calls.append(card.id)
        # What the real dispatch commits before the worker starts. The attempt is
        # spent by then, so a crash past that point has spent it whatever the
        # handler decides — and `settle`/the record both read it off this object.
        card.write({"attempts": str(card.attempts + 1), "started": "now"})
        if card.id == on_card:
            raise boom
        return runner.Dispatch("review", "ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    monkeypatch.setattr(runner, "claude_binary", lambda: "claude")
    if not real_settle:
        monkeypatch.setattr(runner, "settle",
                            lambda r, cid, result: f"{cid}: {result.outcome}")
    return calls


# --------------------------------------------------------------------------
# The permission modes that cannot write a file. Named as a category on
# 2026-08-17, after `plan` turned out to be a second member of what had read as
# a single literal everywhere it was consulted.
# --------------------------------------------------------------------------


def _host_mode(root: Path, mode: str) -> None:
    (root / ".ai").mkdir(parents=True, exist_ok=True)
    (root / ".ai" / "hosts.json").write_text(
        json.dumps({socket.gethostname(): {"permission_mode": mode}}), encoding="utf-8")
