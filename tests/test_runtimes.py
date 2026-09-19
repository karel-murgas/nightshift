"""Tests for `nightshift/runtimes.py` — the `runtime` axis, cloud | local.

The invariant everything below defends is `04_local_runtime.md` §4's, and it is a
*safety* claim rather than a feature one:

> **cloud is the ground state.** Anything unresolvable, unavailable, disabled,
> misconfigured or unmeasured runs on cloud, so a bug in the local path produces a
> cloud dispatch rather than a failure to dispatch.

So most of what is here is denial paths. A test suite for this module that mostly
proved local *works* would be testing the easy half: the expensive failure is not
"local did not run", it is "local ran when something said it must not", and every
one of those somethings gets its own test below.

`tier` is deliberately absent from this file. The two axes are orthogonal (§4) and
a test that coupled them here would be the first step back toward the `local` tier
§4 refuses.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nightshift import runtimes

_BLOCK = {
    "runtime": "opencode",
    "base_url": "http://127.0.0.1:8082/v1",
    "model": "llamacpp-ornith/ornith-1.5-35b-a3b",
    "launcher": r"E:\AI\llm\run_ornith.bat",
    "context_limit": 131072,
    "agents": ["chore-thread", "code-thread"],
}


def _repo(tmp_path: Path, block: dict | None = _BLOCK, *, host: str = "box") -> Path:
    """A repo whose *own* hostname resolves to `block`.

    Writes `.ai/host.json` — the untracked per-machine override `host_config`
    prefers over `hosts.json` — rather than keying `hosts.json` on the real
    `socket.gethostname()`. The override is read wholesale, so this exercises the
    same reader the real path uses without the test depending on what the machine
    running it happens to be called.
    """
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "myapp"\n', encoding="utf-8")
    entry: dict = {"capabilities": []}
    if block is not None:
        entry[runtimes.HOST_KEY] = block
    (tmp_path / ".ai" / "host.json").write_text(json.dumps(entry), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def up(monkeypatch):
    """A local endpoint that answers. The probe is the one genuinely external
    thing in this module, so it is stubbed everywhere rather than left to whether
    the machine running the suite happens to have llama-server up."""
    monkeypatch.setattr(runtimes, "reachable", lambda *a, **k: True)


@pytest.fixture()
def down(monkeypatch):
    monkeypatch.setattr(runtimes, "reachable", lambda *a, **k: False)


# --------------------------------------------------------------------------
# The permanent off switch: no block, no local model. Ever.
# --------------------------------------------------------------------------

def test_a_machine_with_no_block_never_resolves_local(tmp_path, up):
    """The card's first acceptance criterion, and the laptop's correct answer.

    `up` is deliberately applied: even with the endpoint answering, a machine that
    has not *declared* a local model does not use one. Reachability was never the
    question — §4's declared/probed split says "may this machine" is declared, and
    a probe cannot promote a box that never opted in.
    """
    root = _repo(tmp_path, block=None)
    assert runtimes.local_model(root) is None
    for agent in ("chore-thread", "code-thread", "anything-at-all"):
        assert runtimes.resolve(root, agent) == runtimes.CLOUD


def test_a_block_missing_either_required_field_is_no_block(tmp_path, up):
    """A half-written block is a cloud machine, not a crash at 3 AM.

    `.ai/hosts.json` is hand-edited per machine and no gate validates it, so a
    typo here is the realistic failure. It must be the cheap one.
    """
    for broken in ({**_BLOCK, "base_url": ""}, {**_BLOCK, "model": ""},
                   {**_BLOCK, "agents": "chore-thread"}):
        root = _repo(tmp_path / str(id(broken)), broken)
        assert runtimes.local_model(root) is None
        assert runtimes.resolve(root, "chore-thread") == runtimes.CLOUD


# --------------------------------------------------------------------------
# The allowlist: §7's enforcement point
# --------------------------------------------------------------------------

def test_only_allowlisted_charters_resolve_local(tmp_path, up):
    root = _repo(tmp_path)
    assert runtimes.resolve(root, "chore-thread") == runtimes.LOCAL
    assert runtimes.resolve(root, "code-thread") == runtimes.LOCAL
    # The two roles measured and rejected (§9d 2-of-5 recall, §9f's 12 errors),
    # plus the lead-tier charters that were never candidates.
    for refused in ("stale-hunter", "classifier", "code-reviewer", "triage", "scribe"):
        assert runtimes.resolve(root, refused) == runtimes.CLOUD


def test_an_empty_allowlist_permits_nothing(tmp_path, up):
    """`"agents": []` means *not yet*, never *everything*.

    The inversion this guards against would promote every charter on the box the
    day someone wrote an empty list meaning the opposite — and it would do it
    silently, because an empty allowlist looks like the safe value.
    """
    root = _repo(tmp_path, {**_BLOCK, "agents": []})
    assert runtimes.resolve(root, "chore-thread") == runtimes.CLOUD
    assert runtimes.permits(runtimes.local_model(root), "chore-thread") is False


def test_no_agent_name_resolves_local(tmp_path, up):
    """A card with `worker: none` has no charter to allowlist, so it cannot be
    allowlisted. Guarded because `""` is the kind of value that slips through a
    membership test written the other way round."""
    root = _repo(tmp_path)
    assert runtimes.resolve(root, "") == runtimes.CLOUD


# --------------------------------------------------------------------------
# The probe, and the two revocable switches
# --------------------------------------------------------------------------

def test_an_unreachable_endpoint_falls_back_to_cloud(tmp_path, down):
    """The card's fourth criterion: unreachable, disabled or unmeasured falls back
    to cloud **rather than failing the dispatch**. The assertion is as much that
    nothing raises as that the value is `cloud`."""
    root = _repo(tmp_path)
    assert runtimes.resolve(root, "chore-thread") == runtimes.CLOUD


def test_probe_false_answers_the_declared_question_only(tmp_path, down):
    """The panel's row chips render many rows per page load and must not open a
    socket each. `probe=False` therefore answers "is this eligible", which is why
    a chip can say `local` on a box whose server is down."""
    root = _repo(tmp_path)
    assert runtimes.resolve(root, "chore-thread", probe=False) == runtimes.LOCAL
    assert runtimes.resolve(root, "stale-hunter", probe=False) == runtimes.CLOUD


def test_reachable_swallows_every_transport_failure(tmp_path):
    """Connection refused, DNS, timeout, a bad scheme — all one answer.

    Catching broadly is correct *here* precisely because the consequence of being
    wrong is the ground state. A probe that raised would turn a stopped server
    into a failed night.
    """
    for url in ("http://127.0.0.1:1/v1", "http://no-such-host.invalid/v1",
                "not-a-url", "file:///etc/passwd"):
        model = runtimes.LocalModel(base_url=url, model="m", agents=("a",))
        assert runtimes.reachable(model, timeout=0.25) is False


def test_disabled_beats_everything(tmp_path, up):
    """`enabled=False` is the runner's `--no-local` and the panel's toggle folded
    into one argument, and it short-circuits before the host block is even read.

    The card's third criterion: a runner flag forces cloud for that run regardless
    of host block or toggle state.
    """
    root = _repo(tmp_path)
    assert runtimes.resolve(root, "chore-thread", enabled=False) == runtimes.CLOUD
    assert runtimes.resolve(root, "chore-thread", enabled=False,
                            probe=False) == runtimes.CLOUD


def test_the_three_switches_are_independent(tmp_path, up):
    """The card's judgment criterion, made machine-checkable: a reviewer should be
    able to disable local three different ways **without touching the other two
    mechanisms**. So each is exercised with the other two left permitting.
    """
    permissive = _repo(tmp_path / "permissive")
    assert runtimes.resolve(permissive, "chore-thread") == runtimes.LOCAL

    # 1. the host block, alone
    assert runtimes.resolve(_repo(tmp_path / "noblock", block=None),
                            "chore-thread") == runtimes.CLOUD
    # 2. the revocable switch, alone
    assert runtimes.resolve(permissive, "chore-thread", enabled=False) == runtimes.CLOUD
    # 3. the allowlist, alone
    assert runtimes.resolve(_repo(tmp_path / "empty", {**_BLOCK, "agents": []}),
                            "chore-thread") == runtimes.CLOUD


# --------------------------------------------------------------------------
# The panel's asymmetry, which is the rule `TierChoice` is amended to explain
# --------------------------------------------------------------------------

def test_the_toggle_can_always_force_cloud(tmp_path, up):
    root = _repo(tmp_path)
    for agent in ("chore-thread", "code-thread", "stale-hunter", ""):
        assert runtimes.resolve(root, agent, enabled=False) == runtimes.CLOUD


def test_the_toggle_cannot_permit_a_charter_outside_the_allowlist(tmp_path, up):
    """§6's asymmetry, and the card's second acceptance criterion.

    *Attempting to turn it on for a `worker:` absent from the allowlist has no
    effect.* There is no argument to `resolve` that expresses "on" more strongly
    than `enabled=True` — which is the design: the panel's most permissive
    possible request still has to clear `permits`, so there is no code path from
    the UI to a charter §7 has not admitted.
    """
    root = _repo(tmp_path)
    assert runtimes.resolve(root, "stale-hunter", enabled=True) == runtimes.CLOUD
    assert runtimes.resolve(root, "classifier", enabled=True, probe=False) == runtimes.CLOUD


# --------------------------------------------------------------------------
# Driving OpenCode
# --------------------------------------------------------------------------

def test_worker_argv_names_the_agent_the_model_and_json(tmp_path):
    model = runtimes.LocalModel(base_url="http://x/v1", model="prov/m",
                                agents=("chore-thread",), server_port=4096)
    argv = runtimes.worker_argv(model, "chore-thread")
    assert argv[1:] == ["run", "--agent", "chore-thread", "--model", "prov/m",
                        "--format", "json", "--attach", "http://127.0.0.1:4096"]
    # The prompt is never an argv element: it goes on stdin, which is what lets a
    # whole card body through without meeting Windows' command-line limit. The
    # `prompt_not_in_argv` gate enforces the same rule for the cloud path.
    assert not any("card" in part.lower() for part in argv)


def test_worker_argv_continues_a_session(tmp_path):
    model = runtimes.LocalModel(base_url="http://x/v1", model="m", agents=("a",))
    assert runtimes.worker_argv(model, "a", "ses_123")[-2:] == ["--session", "ses_123"]


def test_worker_argv_points_the_attached_server_at_the_worktree(tmp_path):
    """`--dir` is how an attached dispatch says which checkout it is working in.

    Load-bearing because the server is shared across the night and rooted
    wherever it was started, while each card runs in its own worktree. Verified
    against a live server 2026-09-19: attach + `--dir <worktree>` completes with
    tool calls and the server survives it.
    """
    model = runtimes.LocalModel(base_url="http://x/v1", model="m", agents=("a",))
    argv = runtimes.worker_argv(model, "a", cwd=tmp_path)
    assert "--dir" in argv and argv[argv.index("--dir") + 1] == str(tmp_path.resolve())
    # ...and omitted entirely when there is no worktree to name, rather than
    # passed as an empty string the CLI would read as a directory called "".
    assert "--dir" not in runtimes.worker_argv(model, "a")


# --------------------------------------------------------------------------
# The OpenCode server — the second of the two servers a local dispatch needs
# --------------------------------------------------------------------------

def test_the_two_servers_are_not_the_same_thing():
    """`base_url` is llama-server (the model); `server_url` is OpenCode (the
    harness). Confusing them would make a probe of one report on the other."""
    model = runtimes.LocalModel(base_url="http://127.0.0.1:8082/v1", model="m",
                                agents=("a",), server_port=4096)
    assert model.probe_url == "http://127.0.0.1:8082/v1/models"
    assert model.server_url == "http://127.0.0.1:4096"
    assert model.server_probe_url == "http://127.0.0.1:4096/config"


def test_the_server_port_defaults_without_being_declared():
    model = runtimes.LocalModel(base_url="http://x/v1", model="m", agents=("a",))
    assert model.server_url == f"http://127.0.0.1:{runtimes.SERVER_PORT}"


def test_the_server_probe_reads_the_body_not_just_the_status(monkeypatch):
    """A 200 is not enough. Everything on this box shares 127.0.0.1 and only the
    port keeps it apart from the Command Center (8765), llama-server (8082) and
    ComfyUI (8188) — so a port collision would otherwise read as success and
    every dispatch would attach to something that is not OpenCode."""

    class _Answer:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self, _n=None):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    model = runtimes.LocalModel(base_url="http://x/v1", model="m", agents=("a",))

    monkeypatch.setattr(runtimes.urllib.request, "urlopen",
                        lambda *a, **k: _Answer(b'{"$schema":"https://opencode.ai/config.json"}'))
    assert runtimes.server_reachable(model) is True

    # a neighbour holding the port: answers 200, is not OpenCode
    monkeypatch.setattr(runtimes.urllib.request, "urlopen",
                        lambda *a, **k: _Answer(b"<!doctype html><title>something else</title>"))
    assert runtimes.server_reachable(model) is False


def test_ensure_server_reuses_one_that_is_already_up(monkeypatch):
    """Started once per night, not once per card — and never started twice."""
    started = []
    monkeypatch.setattr(runtimes, "server_reachable", lambda *a, **k: True)
    monkeypatch.setattr(runtimes.subprocess, "Popen",
                        lambda *a, **k: started.append(a) or None)
    assert runtimes.ensure_server(runtimes.LocalModel("http://x/v1", "m", ("a",)),
                                  Path(".")) is True
    assert started == [], "a reachable server must not be started a second time"


def test_ensure_server_is_false_rather_than_raising_when_it_cannot_start(monkeypatch):
    """One more false term in the conjunction, never an exception: a box whose
    OpenCode will not start runs its cards on cloud, which is the ground state."""
    monkeypatch.setattr(runtimes, "server_reachable", lambda *a, **k: False)
    monkeypatch.setattr(runtimes, "binary", lambda: None)
    assert runtimes.ensure_server(runtimes.LocalModel("http://x/v1", "m", ("a",)),
                                  Path(".")) is False

    monkeypatch.setattr(runtimes, "binary", lambda: "opencode")
    monkeypatch.setattr(runtimes.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert runtimes.ensure_server(runtimes.LocalModel("http://x/v1", "m", ("a",)),
                                  Path(".")) is False


def test_ensure_server_never_detaches_the_process(monkeypatch):
    """The finding this test exists to pin (measured 2026-09-19).

    `DETACHED_PROCESS | CREATE_NO_WINDOW` yields a server that **boots** — it
    answers `/config`, so every health check passes — and then resets the
    connection on the first real message. OpenCode needs the console it was
    started with, and no probe can see the difference. So the flags must stay
    off, and a future tidy-up that "properly detaches the daemon" must fail here
    rather than in a night where every card silently falls back to cloud.
    """
    seen = {}
    monkeypatch.setattr(runtimes, "server_reachable",
                        lambda *a, **k: bool(seen))          # False, then True
    monkeypatch.setattr(runtimes, "binary", lambda: "opencode")

    def _popen(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return None

    monkeypatch.setattr(runtimes.subprocess, "Popen", _popen)
    runtimes.ensure_server(runtimes.LocalModel("http://x/v1", "m", ("a",),
                                               server_port=4096), Path("."))
    assert "creationflags" not in seen
    assert "start_new_session" not in seen
    assert seen["argv"][1:] == ["serve", "--port", "4096"]


def test_stream_facts_normalises_an_opencode_stream():
    """OpenCode's `sessionID`/`step_finish` mapped onto the keys the runner
    already reads, so warm resume and the run record need no second vocabulary.

    The shape below is copied from a real `opencode run --format json` stream
    (1.18.31), not invented — including the second `step_finish` whose `input` is
    tiny because the prefix was cached, which is why the totals are taken from the
    richest event rather than the last one.
    """
    stream = "\n".join([
        json.dumps({"type": "step_start", "sessionID": "ses_abc", "part": {}}),
        json.dumps({"type": "text", "sessionID": "ses_abc",
                    "part": {"type": "text", "text": "DONE"}}),
        json.dumps({"type": "step_finish", "sessionID": "ses_abc",
                    "part": {"tokens": {"total": 17601, "input": 17447,
                                        "output": 154, "reasoning": 0},
                             "cost": 0}}),
        json.dumps({"type": "step_finish", "sessionID": "ses_abc",
                    "part": {"tokens": {"total": 17685, "input": 69, "output": 16},
                             "cost": 0}}),
    ])
    facts = runtimes.stream_facts(stream)
    assert facts["session_id"] == "ses_abc"
    assert facts["usage"]["total"] == 17685
    # Zero is the truth on a local model, not a parse failure: a local dispatch
    # spends wall clock, not tokens.
    assert facts["total_cost_usd"] == 0.0


def test_stream_facts_survives_a_cut_stream():
    """A killed worker leaves a half-written last line. It must yield what it can
    rather than raise — the stream is also the only record of the attempt."""
    stream = (json.dumps({"type": "step_start", "sessionID": "ses_z", "part": {}})
              + '\n{"type": "step_fin')
    assert runtimes.stream_facts(stream)["session_id"] == "ses_z"
    assert runtimes.stream_facts("") == {}


def test_dispatchable_requires_both_halves(tmp_path, monkeypatch):
    """Allowlisted **and** actually dispatchable by OpenCode.

    §9c Result 4: a wrong `--agent` name does not error, it silently runs the
    default `build` agent, and under `--format json` there is not even a banner to
    notice. So a charter the runtime does not know about is a cloud dispatch,
    never a `build` dispatch wearing its name.
    """
    model = runtimes.local_model(_repo(tmp_path))
    monkeypatch.setattr(runtimes, "available_agents",
                        lambda *a, **k: frozenset({"chore-thread", "build"}))
    assert runtimes.dispatchable(model, "chore-thread", tmp_path) is True
    # allowlisted, but OpenCode cannot see it — the projection never landed
    assert runtimes.dispatchable(model, "code-thread", tmp_path) is False
    # dispatchable, but not allowlisted — §7 still governs
    assert runtimes.dispatchable(model, "build", tmp_path) is False


def test_available_agents_is_empty_when_the_binary_is_missing(tmp_path, monkeypatch):
    """"Cannot confirm" and "confirmed absent" are the same answer here, and both
    mean cloud."""
    monkeypatch.setattr(runtimes, "binary", lambda: None)
    assert runtimes.available_agents(tmp_path) == frozenset()


def test_agent_listing_is_parsed_from_opencodes_own_format(monkeypatch):
    """The listing prints `<name> (primary|subagent)` with each agent's permission
    JSON indented after it — so the parser must not pick names out of that JSON.
    Sample copied from `opencode agent list` 1.18.31."""
    listing = (
        "build (primary)\n"
        "  [\n"
        "  {\n"
        '    "permission": "external_directory",\n'
        '    "action": "allow"\n'
        "  }\n"
        "  ]\n"
        "explore (subagent)\n"
        "chore-thread (primary)\n"
        "code-thread (primary)\n"
    )
    found = frozenset(m.group(1) for m in runtimes._AGENT_LINE.finditer(listing))
    assert found == {"build", "explore", "chore-thread", "code-thread"}


def test_describe_is_empty_without_a_block(tmp_path):
    """The panel renders nothing rather than the words "no local model", which
    would imply a setting someone forgot rather than the ordinary state of every
    machine."""
    assert runtimes.describe(_repo(tmp_path, block=None)) == ""
    assert "chore-thread" in runtimes.describe(_repo(tmp_path / "b"))


# --------------------------------------------------------------------------
# llama-server: the model itself, and who is allowed to stop it
# --------------------------------------------------------------------------

class _Killed:
    """What `taskkill` returns. A real object rather than `None` because
    `stop_started_servers` reads `returncode` — a non-zero one means the process
    was already gone, which is the outcome we wanted but not a port *this call*
    stopped, and the run log must not claim an action nobody took."""

    def __init__(self, returncode: int):
        self.returncode = returncode

def test_a_server_already_up_is_used_but_never_recorded(monkeypatch):
    """The asymmetry the end-of-run teardown depends on.

    A llama-server the maintainer started — because they were using it, or it has
    been up since breakfast — is not the runner's to kill at 4 AM. Only servers
    *this process* started go in `_STARTED_PORTS`, so "only stop what you started"
    is structural rather than a rule each call site has to remember.
    """
    runtimes._STARTED_PORTS.clear()
    monkeypatch.setattr(runtimes, "reachable", lambda *a, **k: True)
    model = runtimes.LocalModel("http://127.0.0.1:8082/v1", "m", ("a",),
                                launcher="anything")
    assert runtimes.ensure_model_server(model) is True
    assert runtimes._STARTED_PORTS == set(), \
        "a server we did not start must never be a server we may stop"


def test_no_launcher_means_no_start_and_a_cloud_dispatch(monkeypatch, tmp_path):
    """A host block may name a model without naming how to start it. That is a
    machine that runs cards on cloud whenever the server happens to be down — not
    an error, and not a guess at a command line."""
    runtimes._STARTED_PORTS.clear()
    monkeypatch.setattr(runtimes, "reachable", lambda *a, **k: False)
    assert runtimes.ensure_model_server(
        runtimes.LocalModel("http://x:8082/v1", "m", ("a",))) is False
    # a launcher that is declared but is not there is the same answer
    assert runtimes.ensure_model_server(
        runtimes.LocalModel("http://x:8082/v1", "m", ("a",),
                            launcher=str(tmp_path / "gone.bat"))) is False


def test_a_started_server_is_recorded_and_then_stoppable(monkeypatch, tmp_path):
    launcher = tmp_path / "run_it.bat"
    launcher.write_text("echo hi\n", encoding="utf-8")
    runtimes._STARTED_PORTS.clear()
    calls = {"reachable": 0}

    def _reachable(*_a, **_k):
        calls["reachable"] += 1
        return calls["reachable"] > 1          # down, then up

    monkeypatch.setattr(runtimes, "reachable", _reachable)
    monkeypatch.setattr(runtimes.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(runtimes.time, "sleep", lambda _s: None)
    model = runtimes.LocalModel("http://127.0.0.1:8082/v1", "m", ("a",),
                                launcher=str(launcher))
    assert runtimes.ensure_model_server(model) is True
    assert runtimes._STARTED_PORTS == {8082}

    killed = []
    monkeypatch.setattr(runtimes, "_pid_on_port", lambda port: 4321)
    monkeypatch.setattr(runtimes.subprocess, "run",
                        lambda argv, **k: killed.append(argv) or _Killed(0))
    monkeypatch.setattr(runtimes.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert runtimes.stop_started_servers() == [8082]
    assert killed, "the recorded port's listener must actually be signalled"
    assert runtimes._STARTED_PORTS == set(), "the set must not survive a teardown"


def test_teardown_stops_by_port_never_by_image_name(monkeypatch):
    """Doc 04 §9e, and it cost a day: every wrapper written for the trials killed
    llama-server by image name, so two overlapping runs killed each other's server
    and the failure surfaced in the *innocent* one as `Cannot connect to API`
    about 1 s in. Three separate "bugs" that day were this."""
    runtimes._STARTED_PORTS.clear()
    runtimes._STARTED_PORTS.add(8082)
    seen = []
    monkeypatch.setattr(runtimes, "_pid_on_port", lambda port: 999)
    monkeypatch.setattr(runtimes.subprocess, "run",
                        lambda argv, **k: seen.append(argv) or _Killed(0))
    monkeypatch.setattr(runtimes.os, "kill", lambda pid, sig: seen.append(pid))
    runtimes.stop_started_servers()
    flat = " ".join(str(part) for call in seen for part in
                    (call if isinstance(call, (list, tuple)) else [call]))
    assert "999" in flat, "the PID that owns the port is what gets signalled"
    for name in ("llama-server", "llama-server.exe", "opencode", "opencode.exe"):
        assert name not in flat


def test_teardown_is_a_no_op_when_nothing_was_started():
    runtimes._STARTED_PORTS.clear()
    assert runtimes.stop_started_servers() == []


# --------------------------------------------------------------------------
# `prepare` — the one entry point both dispatchers use
# --------------------------------------------------------------------------

def test_prepare_is_the_single_answer_for_runner_and_chores(tmp_path, monkeypatch):
    """`runner` and `chores` must not drift into two answers to "does this card go
    local" — and the chore batch is the path most likely to take the local branch,
    because `chore-thread` is one of the two allowlisted charters."""
    root = _repo(tmp_path)
    monkeypatch.setattr(runtimes, "ensure_model_server", lambda *a, **k: True)
    monkeypatch.setattr(runtimes, "dispatchable", lambda *a, **k: True)
    monkeypatch.setattr(runtimes, "ensure_server", lambda *a, **k: True)

    runtime, local = runtimes.prepare(root, "chore-thread", tmp_path)
    assert runtime == runtimes.LOCAL and local is not None
    runtime, local = runtimes.prepare(root, "code-thread", tmp_path)
    assert runtime == runtimes.LOCAL and local is not None


def test_prepare_returns_no_model_alongside_cloud(tmp_path, monkeypatch):
    """`(CLOUD, None)` and never `(CLOUD, <model>)`. The caller hands the pair to
    `run_producer`, so a `None` model is what makes a half-resolved local dispatch
    structurally impossible rather than merely unlikely."""
    root = _repo(tmp_path)
    monkeypatch.setattr(runtimes, "ensure_model_server", lambda *a, **k: True)
    monkeypatch.setattr(runtimes, "dispatchable", lambda *a, **k: True)
    monkeypatch.setattr(runtimes, "ensure_server", lambda *a, **k: True)

    for runtime, local in (
            runtimes.prepare(root, "chore-thread", tmp_path, enabled=False),
            runtimes.prepare(root, "stale-hunter", tmp_path),
            runtimes.prepare(_repo(tmp_path / "bare", block=None), "chore-thread",
                             tmp_path)):
        assert (runtime, local) == (runtimes.CLOUD, None)


def test_prepare_falls_back_to_cloud_at_each_stage(tmp_path, monkeypatch):
    """Every one of the three things that must be brought up is an independent
    reason to run on cloud, and none of them raises."""
    root = _repo(tmp_path)
    stages = ("ensure_model_server", "dispatchable", "ensure_server")
    for failing in stages:
        for name in stages:
            monkeypatch.setattr(runtimes, name,
                                (lambda *a, **k: False) if name == failing
                                else (lambda *a, **k: True))
        runtime, local = runtimes.prepare(root, "chore-thread", tmp_path)
        assert (runtime, local) == (runtimes.CLOUD, None), f"{failing} did not fall back"


def test_prepare_checks_declared_facts_before_starting_anything(tmp_path, monkeypatch):
    """A machine with no host block must reach cloud without opening a socket or
    starting a process. Cheap terms first is not an optimisation here — it is what
    keeps the laptop from launching a model it does not have."""
    started = []
    monkeypatch.setattr(runtimes, "ensure_model_server",
                        lambda *a, **k: started.append("model") or True)
    monkeypatch.setattr(runtimes, "ensure_server",
                        lambda *a, **k: started.append("opencode") or True)
    assert runtimes.prepare(_repo(tmp_path, block=None), "chore-thread",
                            tmp_path) == (runtimes.CLOUD, None)
    assert started == []
