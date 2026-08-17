"""The Command Center — a launcher, a registry and a tail, never a chat client.

What matters here is not "does a page render". It is the properties the plan's
own standing rule depends on:

**The server owns no logic.** Every write/dispatch verb is a call to
`run_command`/`spawn_background`, which shell out to `python -m nightshift.<module>
<args>` — never a direct import of `boardcmd`, `runner.dispatch`/`settle`,
`chores.execute`, `ingest.classify`/`scribe` or `drain.drain`. Checked
mechanically (`test_panel_never_imports_a_write_or_dispatch_verb_directly`) so this
does not quietly rot the way `test_boardcmd.py`'s own precedent warns it can.

**An account with `dispatch: never` is refused server-side**, not merely hidden
in the UI — a crafted POST must fail exactly where a real click would have
declined to send one at all.

**The ambient rail never fetches freshness on page load** — only an explicit
Refresh does. And **the Ideas page enumerates filenames only, never bodies** —
the same boundary `boardcmd.promote`/`edit_body` already keep.

**A finished run does not read as a live one.** `status.json` is a file and
outlives the process that wrote it, and pids are recycled — the first look at
this panel showed a two-day-old heartbeat as a 41-hour dispatch in progress,
because the only question asked was whether the pid was alive and something else
had since been given that pid. Three tests pin the three signals.

Real git repositories throughout, and a real `ThreadingHTTPServer` on a loopback
port for the route-level tests, matching the framework's own precedent
(`test_boardcmd.py`, `test_drain.py`) of exercising the real thing rather than a
mock of it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nightshift import board, jobs, panel

CARD = """\
---
id: {id}
title: "{id}, a card"
state: {state}
tier: worker
worker: code-thread
recipe: none
unattended: {unattended}
verify: play
created: 2026-08-15
---

## Intent

One thing.

## Acceptance

- machine: green.
"""

_MANIFEST = """
[project]
name = "myapp"
source_dirs = ["myapp"]

[tests]
dir = "tests"

[branches]
integration = "main"
stable = "main"

[tiers]
binding_doc = ".claude/memory/ai_team/00_architecture.md"

[[accounts]]
label = "main"
config_dir = "/does/not/exist/main"
dispatch = "always"

[[accounts]]
label = "spendy"
config_dir = "/does/not/exist/spendy"
dispatch = "never"
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


def _repo(tmp_path: Path) -> Path:
    """A committed repo with every lane, a manifest declaring two accounts, and
    the doc `tiers.binding_doc` points at — the minimum a real command needs to
    run without erroring on missing scaffolding."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
    (root / ".ai").mkdir()
    (root / ".ai" / "manifest.toml").write_text(_MANIFEST, encoding="utf-8")
    doc = root / ".claude" / "memory" / "ai_team"
    doc.mkdir(parents=True)
    (doc / "00_architecture.md").write_text(
        "```tier-binding\nworker = sonnet\nlead = opus\n```\n", encoding="utf-8")
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-thread.md").write_text("---\nname: code-thread\n---\n", encoding="utf-8")
    for lane in board.LANES + (board.PRIVATE_LANE,):
        (root / "Board" / lane).mkdir(parents=True)
        (root / "Board" / lane / ".gitkeep").write_bytes(b"")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


def _card(root: Path, lane: str, card_id: str, *, unattended: str = "true",
          order: str = "") -> Path:
    path = root / "Board" / lane / f"{card_id}.md"
    text = CARD.format(id=card_id, state=lane, unattended=unattended)
    if order:
        text = text.replace("verify: play", f"verify: play\nkanban_order: {order}")
    path.write_text(text, encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"card {card_id}")
    return path


@pytest.fixture(autouse=True)
def _reset_account():
    """`_ACCOUNT` is process-wide, in-memory state (by design — see the module
    docstring). A test that selected one must not leak it into the next, and the
    same goes for the cached meter reading."""
    panel._ACCOUNT = panel.AccountState()
    panel._METERS = None
    yield
    panel._ACCOUNT = panel.AccountState()
    panel._METERS = None


# ------------------------------------------------------ no logic in the server


def test_panel_never_imports_a_write_or_dispatch_verb_directly():
    """Every board write or LLM dispatch is a subprocess of the CLI verb that
    already exists — never a direct Python call to the function that does it.
    `test_boardcmd.py` states the rule this makes mechanical: "the panel will
    not import this module — it will run it"."""
    source = Path(panel.__file__).read_text(encoding="utf-8")
    for forbidden in ("import boardcmd", "from nightshift import boardcmd",
                      "from nightshift.boardcmd", "import chores",
                      "from nightshift import chores", "from nightshift.chores"):
        assert forbidden not in source, (
            f"{forbidden!r} found — board writes and the chore batch must be run as "
            f"`python -m nightshift.<module>`, not imported")
    for forbidden in (".dispatch(", ".settle(", ".execute(", "drain.drain(",
                      "ingest.classify(", "ingest.scribe(", "reconcile.apply("):
        assert forbidden not in source, (
            f"{forbidden!r} found — this is a dispatch or a board write happening "
            f"in-process, which is exactly the re-implementation the panel must not do")


def test_panel_only_imports_read_helpers_from_runner():
    """`runner.py` owns `dispatch`/`settle`/`merge_branch` and a great deal more —
    the panel may read its status file and its queue-selection logic, and nothing
    that mutates a card or a branch."""
    source = Path(panel.__file__).read_text(encoding="utf-8")
    assert "from nightshift.runner import" in source
    forbidden_names = ("dispatch", "settle", "merge_branch", "rebase_and_merge",
                      "run_producer", "prepare_worktree")
    for name in forbidden_names:
        assert f" {name}," not in source and f" {name}\n" not in source and \
              f"import {name}" not in source, f"runner.{name} must not be imported"


# ------------------------------------------------------------- account state


def test_select_account_resolves_by_label(tmp_path):
    root = _repo(tmp_path)
    state = panel.select_account(root, "main")
    assert state.label == "main"
    assert state.dispatch == "always"


def test_select_account_refuses_an_unknown_label(tmp_path):
    root = _repo(tmp_path)
    with pytest.raises(panel.PanelError, match="unknown"):
        panel.select_account(root, "unknown")


def test_select_account_empty_label_returns_to_ambient(tmp_path):
    root = _repo(tmp_path)
    panel.select_account(root, "main")
    state = panel.select_account(root, "")
    assert state.label == ""
    assert state.config_dir == ""


def test_dispatch_env_is_none_with_no_account_selected():
    assert panel._dispatch_env() is None


def test_dispatch_env_adds_claude_config_dir_when_an_account_is_selected(tmp_path):
    root = _repo(tmp_path)
    panel.select_account(root, "main")
    env = panel._dispatch_env()
    assert env["CLAUDE_CONFIG_DIR"] == "/does/not/exist/main"
    # every other inherited variable survives too — an override, not a replacement
    import os
    assert env["PATH"] == os.environ["PATH"]


def test_guard_refuses_a_dispatch_never_account(tmp_path):
    root = _repo(tmp_path)
    panel.select_account(root, "spendy")
    with pytest.raises(panel.PanelError, match="dispatch: never|never"):
        panel._guard_dispatch_account()


def test_guard_allows_a_dispatch_always_account_with_no_spend_signal(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    panel.select_account(root, "main")
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=True, has_extra_usage_enabled=False))
    panel._guard_dispatch_account()  # must not raise


def test_guard_allows_the_ambient_account_with_no_spend_signal(monkeypatch):
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=True, has_extra_usage_enabled=False))
    panel._guard_dispatch_account()  # must not raise


def test_guard_refuses_when_the_live_identity_shows_spend_enabled(monkeypatch):
    """The bug this closes: `[[accounts]]` can be missing an entry for the
    account actually in force (it was, on the first live run of this module,
    against Karel's own board) — so the config check alone would let automated
    work reach exactly the account `feedback_account_dispatch` exists to protect.
    No account needs to be selected at all for this to fire; the ambient account
    is exactly the case that had no config entry."""
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=True, email="karel@example.com",
                                                          has_extra_usage_enabled=True))
    with pytest.raises(panel.PanelError, match="spend"):
        panel._guard_dispatch_account()


def test_guard_does_not_veto_on_an_identity_read_that_failed(monkeypatch):
    """Fails open, matching `usage.py`'s own posture: an unreadable identity file
    must not become a silent, permanent dispatch refusal."""
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=False, reason="no identity file"))
    panel._guard_dispatch_account()  # must not raise


# ------------------------------------------------------- the command helper


def test_module_argv_shape():
    assert panel._module_argv("boardcmd", "reorder", "x", "y") == [
        sys.executable, "-m", "nightshift.boardcmd", "reorder", "x", "y"]


def test_run_command_invokes_the_module_as_a_subprocess(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = panel.run_command("boardcmd", ["reorder", "a", "b"], root)
    assert captured["argv"] == [sys.executable, "-m", "nightshift.boardcmd",
                                "reorder", "a", "b"]
    assert captured["cwd"] == root
    assert captured["env"] is None
    assert result.stdout == "ok"


def test_spawn_background_passes_the_selected_accounts_env(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    panel.select_account(root, "main")
    captured = _capture_popen(monkeypatch)

    pid = panel.spawn_background("runner", ["--card", "x"], root)
    assert pid == 4242
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == "/does/not/exist/main"
    # The wrapper is what gets spawned; the command it will run is on the record.
    recorded = jobs.read_all(root)[0]
    assert recorded.argv == [sys.executable, "-m", "nightshift.runner", "--card", "x"]
    assert captured["argv"][:3] == [sys.executable, "-m", "nightshift.jobs"]
    assert captured["argv"][-1] == recorded.ident


def test_every_background_verb_leaves_a_record_and_a_log_to_read(tmp_path, monkeypatch):
    """The property is about *all* of them, not the one that was noticed.

    `ingest` is the verb whose silence was measured, but it was silent for a
    structural reason — the spawn threw the output away — and every other
    background verb shared the code that did it. So the test is over the call
    sites, not over `ingest`.
    """
    root = _repo(tmp_path)
    captured = _capture_popen(monkeypatch)
    for module, args in (("ingest", []), ("chores", []), ("drain", ["--card", "c"]),
                         ("preflight", []), ("fix", []), ("runner", [])):
        panel.spawn_background(module, args, root)
        recorded = jobs.read_all(root)[0]
        assert recorded.label == module
        assert recorded.argv[:3] == [sys.executable, "-m", f"nightshift.{module}"]
        # The log file is opened by the panel and handed over as the wrapper's
        # own stdout — which is what puts the command's output in it.
        assert jobs.log_path(root, recorded.ident).exists()
        assert captured["stdout"] is not None


def test_the_card_sequencer_is_recorded_the_same_way(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _capture_popen(monkeypatch)
    panel.spawn_sequence(["a", "b"], root)
    recorded = jobs.read_all(root)[0]
    assert recorded.argv[-3:] == ["--dispatch-cards", "a", "b"]


def _capture_popen(monkeypatch) -> dict:
    """Stand in for `Popen` and report what it was asked to start."""
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        captured["stdout"] = kwargs.get("stdout")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return captured


# --------------------------------------------------------------- the server


@pytest.fixture
def server(tmp_path):
    root = _repo(tmp_path)
    panel.Handler.root = root
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", root
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _get(base: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(f"{base}/{path}", timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base}/{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_root_redirects_to_now(server):
    base, _ = server
    req = urllib.request.Request(f"{base}/")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.geturl().endswith("/now")


@pytest.mark.parametrize("page", panel.PAGES)
def test_every_page_renders(server, page):
    base, _ = server
    status, text = _get(base, page)
    assert status == 200
    assert "Command Center" in text
    assert "<nav" in text


def test_now_page_lists_a_dispatchable_card(server):
    base, root = server
    _card(root, "tasks", "a-card")
    status, text = _get(base, "now")
    assert status == 200
    assert "a-card" in text


def test_ideas_page_lists_filenames_only_never_bodies(server):
    base, root = server
    (root / "Board" / board.PRIVATE_LANE / "thought.md").write_text(
        "a distinctive secret sentence: pomeranian-carburettor\n", encoding="utf-8")
    status, text = _get(base, "ideas")
    assert status == 200
    assert "thought.md" in text
    assert "pomeranian-carburettor" not in text


def test_reorder_round_trips_through_the_real_cli(server):
    base, root = server
    _card(root, "tasks", "a-card")
    status, data = _post(base, "api/reorder", {"card_id": "a-card", "order": "VZ"})
    assert status == 200, data
    assert data["ok"] is True
    text = (root / "Board" / "tasks" / "a-card.md").read_text(encoding="utf-8")
    assert "kanban_order: VZ" in text


def test_note_can_target_the_private_lane_and_the_response_carries_no_body_text(server):
    base, root = server
    status, data = _post(base, "api/note",
                         {"name": "fresh", "body": "pomeranian-carburettor", "lane": "ideas"})
    assert status == 200, data
    assert "pomeranian-carburettor" not in json.dumps(data)
    assert (root / "Board" / board.PRIVATE_LANE / "fresh.md").read_text(
        encoding="utf-8") == "pomeranian-carburettor\n"


def test_a_refused_verb_reports_400_and_the_reason(server):
    base, _ = server
    status, data = _post(base, "api/reorder", {"card_id": "ghost", "order": "VA"})
    assert status == 400
    assert data["ok"] is False
    assert "ghost" in data["message"]


def test_dispatch_is_refused_server_side_for_a_never_account(server, monkeypatch):
    base, root = server
    panel.select_account(root, "spendy")

    def _fail(*a, **k):
        raise AssertionError("spawn_background must not run for a dispatch: never account")

    monkeypatch.setattr(panel, "spawn_background", _fail)
    status, data = _post(base, "api/dispatch", {"card_id": "a-card"})
    assert status == 400
    assert data["ok"] is False
    assert "never" in data["message"]


def test_dispatch_spawns_the_runner_for_an_ordinary_account(server, monkeypatch):
    base, root = server
    _card(root, "tasks", "a-card")
    captured = {}

    def fake_spawn(module, args, root_arg):
        captured["module"], captured["args"] = module, args
        return 999

    monkeypatch.setattr(panel, "spawn_background", fake_spawn)
    # The whole point of this test is the ordinary path with nothing vetoing it —
    # the veto itself is covered above. Without this, the test reads whatever
    # `~/.claude.json` says on the machine actually running it, which is exactly
    # the non-hermetic read `test_guard_refuses_when_the_live_identity_shows_spend_enabled`
    # exists to keep out of a test that is not about that signal.
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=True, has_extra_usage_enabled=False))
    status, data = _post(base, "api/dispatch", {"card_id": "a-card"})
    assert status == 200, data
    assert captured["module"] == "runner"
    assert captured["args"] == ["--card", "a-card"]
    assert "999" in data["message"]


def test_unknown_action_is_a_404(server):
    base, _ = server
    status, data = _post(base, "api/nonexistent", {})
    assert status == 404


# --------------------------------------------------- the ambient rail's freshness rule


def test_read_rail_never_fetches_freshness_by_default(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = []
    real_read = panel.freshness.read

    def spy(*args, **kwargs):
        calls.append(kwargs.get("fetch", True))
        return real_read(*args, fetch=False)

    monkeypatch.setattr(panel.freshness, "read", spy)
    panel.read_rail(root)
    assert calls == [False]


def test_read_rail_fetches_only_when_explicitly_asked(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = []
    real_read = panel.freshness.read

    def spy(*args, **kwargs):
        calls.append(kwargs.get("fetch", True))
        return real_read(*args, fetch=False)

    monkeypatch.setattr(panel.freshness, "read", spy)
    panel.read_rail(root, fetch_freshness=True)
    assert calls == [True]


# ----------------------------------------------------- the meters are not re-fetched


def test_the_meter_reading_is_reused_rather_than_refetched_on_every_page(monkeypatch):
    """The meters are ambient, so without a cache every click is another HTTP
    call — which is how the endpoint started answering 429 while this was being
    looked at, and the rail went from numbers to an error line."""
    calls = []
    monkeypatch.setattr(panel.usage, "read",
                        lambda creds=None, **k: calls.append(1) or panel.usage.Snapshot())
    panel.read_meters(None)
    panel.read_meters(None)
    panel.read_meters(None)
    assert len(calls) == 1


def test_the_meter_reading_can_be_forced(monkeypatch):
    calls = []
    monkeypatch.setattr(panel.usage, "read",
                        lambda creds=None, **k: calls.append(1) or panel.usage.Snapshot())
    panel.read_meters(None)
    panel.read_meters(None, force=True)
    assert len(calls) == 2


def test_switching_account_throws_the_cached_meters_away(tmp_path, monkeypatch):
    """A cached reading belongs to the account it was taken for; carrying it
    across a switch would show one account's headroom under another's name."""
    root = _repo(tmp_path)
    calls = []
    monkeypatch.setattr(panel.usage, "read",
                        lambda creds=None, **k: calls.append(1) or panel.usage.Snapshot())
    panel.read_meters(None)
    panel.select_account(root, "main")
    panel.read_meters(None)
    assert len(calls) == 2


def test_the_identity_read_is_never_cached(tmp_path, monkeypatch):
    """It is what the dispatch veto reads. A safety check answering from a
    minute-old copy is not a safety check."""
    calls = []
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: calls.append(1) or panel.usage.Identity(fetched=True))
    panel._guard_dispatch_account()
    panel._guard_dispatch_account()
    assert len(calls) == 2


# ------------------------------------------------- a finished run is not a live one


def _beat(minutes_ago: float = 0.0, pid: int | None = None) -> dict:
    when = dt.datetime.now() - dt.timedelta(minutes=minutes_ago)
    return {"pid": os.getpid() if pid is None else pid, "card": "a-card",
            "phase": "worker", "updated": when.isoformat(), "since": when.isoformat()}


def test_a_finished_record_beats_a_heartbeat_nobody_cleared():
    """Nothing clears `status.json` on the way out, so the last phase of a
    completed run sits there forever. The record knows it ended."""
    beat = _beat(minutes_ago=1)
    record = {"complete": True, "finished": dt.datetime.now().isoformat()}
    assert panel.run_is_live(beat, record) is False


def test_a_heartbeat_older_than_the_trust_window_is_not_believed():
    """The case that was actually on screen: a two-day-old file whose pid had been
    recycled to an unrelated process, rendered as a 41-hour dispatch in progress.
    The pid check alone says `True` here — the age is what catches it."""
    stale = _beat(minutes_ago=panel.HEARTBEAT_TRUSTED_FOR.total_seconds() / 60 + 5)
    assert panel._pid_alive(stale["pid"]), "this test needs a pid that IS alive"
    assert panel.run_is_live(stale, {"complete": False}) is False


def test_a_fresh_heartbeat_from_a_live_pid_is_live():
    assert panel.run_is_live(_beat(minutes_ago=1), {"complete": False}) is True


def test_a_dead_pid_is_not_live():
    assert panel.run_is_live(_beat(minutes_ago=1, pid=999_999_999), {}) is False


def test_no_status_file_at_all_is_not_live():
    assert panel.run_is_live({}, {}) is False


def test_a_stale_heartbeat_does_not_render_as_running(server):
    """The whole point, end to end: the rail must not claim a run is in flight."""
    base, root = server
    _card(root, "tasks", "a-card")
    stale = _beat(minutes_ago=panel.HEARTBEAT_TRUSTED_FOR.total_seconds() / 60 + 5)
    status = root / ".ai" / "runs"
    status.mkdir(parents=True, exist_ok=True)
    (status / "status.json").write_text(json.dumps(stale), encoding="utf-8")

    _, text = _get(base, "now")

    assert "No run in progress" in text
    # The class is always in the stylesheet; what must be absent is the element.
    assert '<span class="live-dot">' not in text, "the dot claims a run is in flight"
    assert "Running now" not in text


# ------------------------------------------------------------- the roster reads


def test_the_lane_column_keeps_the_lane_and_drops_settles_whole_sentence():
    entry = {"landed": "a-card: → testing/ (reviewed ok, rebased ai/a-card onto test "
                       "and merged)", "outcome": "reviewed"}
    assert panel._landed_lane(entry) == "→ testing/"


def test_the_lane_column_falls_back_to_the_outcome_when_nothing_landed():
    assert panel._landed_lane({"outcome": "failed"}) == "failed"


def test_a_long_detail_is_cut_rather_than_stretching_the_table():
    """Rendered whole, one worker's `detail` pushed every other column of the
    roster into a two-character ribbon and ran off the side of the window."""
    entry = {"detail": "word " * 200}
    said = panel._said(entry)
    assert len(said) <= 90
    assert said.endswith("…")


def test_a_short_detail_is_left_alone():
    assert panel._said({"detail": "reviewed ok"}) == "reviewed ok"


# ------------------------------------------------------------ rows and their meta


def test_every_meta_fact_is_its_own_element():
    """`.meta` is a flex row and its `gap` only separates *children*, so two bare
    strings render as `code-threadverify: play`. Found by looking at the page."""
    assert panel._meta(["code-thread", "verify: play"]) == (
        '<div class="meta"><span>code-thread</span><span>verify: play</span></div>')


def test_meta_leaves_an_element_that_is_already_one_alone():
    assert panel._meta([panel._chip("running", "ok")]).count("<span") == 1


def test_a_section_carrying_reorderable_rows_gets_the_id_the_dragging_binds_to():
    """Without it the drag handlers and the take-first slider bind to nothing and
    the whole selection model is silently dead — which is how it first shipped."""
    assert 'id="queue"' in panel._section("Tonight", 1, "<div></div>", rows_id="queue")
    assert 'id=' not in panel._section("Decide", 1, "<div></div>")


# --------------------------------------------------- what the other machine takes


def test_a_card_needing_a_human_is_not_filed_under_the_other_machine(tmp_path):
    """`requires: gpu-box` **and** `unattended: false` needs a person wherever it
    runs, so promising that another machine will take it is a lie about both."""
    root = _repo(tmp_path)
    path = root / "Board" / "tasks" / "both.md"
    path.write_text(CARD.format(id="both", state="tasks", unattended="false").replace(
        "verify: play", "verify: play\nrequires: gpu-box"), encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "both")

    ctx = panel.read_context(root)

    assert [c.card.id for c in ctx.elsewhere] == []
    assert "both" in [c.card.id for c in ctx.do_now]


# ------------------------------------------------- running a chosen subset, in order


def test_night_with_card_ids_spawns_the_sequencer_not_the_whole_queue(server, monkeypatch):
    base, root = server
    seen = {}
    monkeypatch.setattr(panel, "spawn_sequence",
                        lambda ids, r: seen.setdefault("ids", list(ids)) or 4242)
    monkeypatch.setattr(panel, "spawn_background",
                        lambda *a, **k: pytest.fail("a subset must not start the whole queue"))
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=True,
                                                          has_extra_usage_enabled=False))

    status, data = _post(base, "api/night", {"card_ids": ["b", "a", "c"]})

    assert status == 200, data
    assert seen["ids"] == ["b", "a", "c"], "the panel's order is the run's order"


def test_night_with_no_ids_still_runs_the_whole_queue(server, monkeypatch):
    base, root = server
    seen = {}
    monkeypatch.setattr(panel, "spawn_background",
                        lambda module, args, r: seen.setdefault("call", (module, list(args)))
                        or 7)
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=True,
                                                          has_extra_usage_enabled=False))

    status, data = _post(base, "api/night", {})

    assert status == 200, data
    assert seen["call"] == ("runner", [])


def test_dispatch_cards_runs_the_runners_own_per_card_path_in_order(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert panel.dispatch_cards(["first", "second"], root) == 0
    assert [c[-1] for c in calls] == ["first", "second"]
    assert all("nightshift.runner" in c for c in calls), \
        "each card must go through the runner, not through anything this module owns"


def test_dispatch_cards_stops_at_the_first_failure(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert panel.dispatch_cards(["first", "second"], root) == 1
    assert len(calls) == 1, "the second card ran after the first had failed"


# ------------------------------------------------------------------ drag persistence


def test_reordering_writes_only_the_cards_whose_order_actually_moved(server):
    """A drag posts every row. Writing them all would commit the whole lane every
    time one card moves."""
    base, root = server
    _card(root, "tasks", "one", order="V0001")
    _card(root, "tasks", "two", order="V0002")

    status, data = _post(base, "api/reorder-many", {"writes": [
        {"card_id": "one", "order": "V0001"},      # unchanged
        {"card_id": "two", "order": "V0009"},      # moved
    ]})

    assert status == 200, data
    assert "1 card" in data["message"]
    assert "kanban_order: V0001" in (root / "Board" / "tasks" / "one.md").read_text(
        encoding="utf-8")
    assert "kanban_order: V0009" in (root / "Board" / "tasks" / "two.md").read_text(
        encoding="utf-8")


def test_reordering_nothing_says_so_rather_than_committing(server):
    base, root = server
    _card(root, "tasks", "one", order="V0001")
    status, data = _post(base, "api/reorder-many",
                         {"writes": [{"card_id": "one", "order": "V0001"}]})
    assert status == 200
    assert "unchanged" in data["message"]


# ------------------------------------------------------- reading a body to edit it


def test_a_body_can_be_read_back_for_the_person_editing_it(tmp_path):
    root = _repo(tmp_path)
    note = root / "Board" / "inbox" / "thought.md"
    note.write_text("half a thought\n", encoding="utf-8")
    assert panel.read_body(root, "Board/inbox/thought.md") == "half a thought\n"


def test_an_idea_can_be_read_back_too_because_the_editor_is_the_persons_own(tmp_path):
    """`ideas_fence` stops an *agent* opening a private note; `boardcmd edit` exists
    because editing one has to be an act the human drives, "with the text arriving
    from their editor". A textarea in their browser is that editor, and it cannot
    be prefilled without this read."""
    root = _repo(tmp_path)
    idea = root / "Board" / board.PRIVATE_LANE / "spark.md"
    idea.write_text("pomeranian-carburettor\n", encoding="utf-8")
    assert panel.read_body(root, f"Board/{board.PRIVATE_LANE}/spark.md") == \
        "pomeranian-carburettor\n"


def test_reading_a_body_outside_the_board_is_refused(tmp_path):
    root = _repo(tmp_path)
    (root / "secrets.txt").write_text("no\n", encoding="utf-8")
    with pytest.raises(panel.PanelError, match="not inside the board"):
        panel.read_body(root, "secrets.txt")


def test_reading_a_body_outside_the_board_is_refused_over_http(server):
    base, root = server
    (root / "secrets.txt").write_text("no\n", encoding="utf-8")
    request = urllib.request.Request(f"{base}/api/body?path=../secrets.txt")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read())
    assert payload.get("ok") is False


@pytest.mark.parametrize("page, name", [
    ("inbox", "Regenerate soundtrack.md"),
    ("ideas", "Animation – attack.md"),
])
def test_an_editor_id_survives_a_real_filename(server, page, name):
    """Real note names carry spaces, capitals and en dashes. An id with a space in
    it is not a valid id, and `getElementById` never finds it — so the editor
    would silently refuse to open for exactly the notes most in need of editing."""
    base, root = server
    lane = "inbox" if page == "inbox" else board.PRIVATE_LANE
    (root / "Board" / lane / name).write_text("body\n", encoding="utf-8")

    _, text = _get(base, page)

    ids = re.findall(r'<div class="editor" id="([^"]+)"', text)
    assert ids, "the page rendered no editor at all"
    for found in ids:
        assert " " not in found, f"id {found!r} contains a space"
        assert found == found.strip()


def test_the_ideas_page_still_shows_no_bodies_even_though_one_can_be_fetched(server):
    base, root = server
    (root / "Board" / board.PRIVATE_LANE / "spark.md").write_text(
        "pomeranian-carburettor\n", encoding="utf-8")
    _, text = _get(base, "ideas")
    assert "spark.md" in text
    assert "pomeranian-carburettor" not in text


# ------------------------------------------------------- a card reads as a card


def test_markdown_renders_the_constructs_a_card_uses():
    out = panel.markdown("## Intent\n\nDo **the** thing with `code`.\n\n- one\n- two\n")
    assert "<h3>Intent</h3>" in out
    assert "<b>the</b>" in out and "<code>code</code>" in out
    assert out.count("<li>") == 2 and "<ul>" in out


def test_markdown_never_lets_a_card_inject_html():
    """Escaping happens once, before any tag is introduced."""
    out = panel.markdown("<script>alert(1)</script>\n\n<img src=x onerror=y>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_a_wrapped_list_item_does_not_restart_the_numbering():
    """An indented continuation line closed the list, so the next item opened a
    fresh `<ol>` — every step in a card's `## Steps` rendered as "1."."""
    out = panel.markdown("1. first step\n   continued here\n2. second step\n")
    assert out.count("<ol>") == 1, out
    assert "continued here" in out
    assert out.count("<li>") == 2


def test_a_fenced_block_keeps_its_markup_literal():
    out = panel.markdown("```\n**not bold**\n```\n")
    assert "<pre>**not bold**</pre>" in out


def test_frontmatter_is_split_off_rather_than_rendered_as_prose():
    """Rendered as markdown the block is neither: the fences become rules and the
    fields collapse into one run-on paragraph at the top of every card."""
    fields, body = panel.split_frontmatter(
        "---\nid: a-card\nstate: tasks\n---\n\n## Intent\n\nOne thing.\n")
    assert fields["id"] == "a-card"
    assert body.lstrip().startswith("## Intent")
    assert "id: a-card" not in panel.markdown(body)


def test_a_card_page_shows_its_fields_and_its_prose(server):
    base, root = server
    _card(root, "tasks", "a-card")
    _, text = _get(base, "card/a-card")
    assert '<div class="fields">' in text
    assert "<h3>Intent</h3>" in text
    assert "Command Center" in text, "a card opens inside the panel, not as a bare dump"


# ------------------------------------------------------------------ the tag shows


def test_a_nightshift_card_says_so_on_its_row(server):
    """The tag is the difference between a card the night can take and one it
    never will, so it belongs where the queue is read."""
    base, root = server
    path = root / "Board" / "tasks" / "framework.md"
    path.write_text(CARD.format(id="framework", state="tasks", unattended="false").replace(
        "verify: play", "verify: play\ntags:\n  - nightshift"), encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "framework card")

    _, text = _get(base, "now")

    assert "nightshift" in text
    assert '<span class="chip warn">nightshift</span>' in text


# ------------------------------------------------------------------- the meters


def _bucket(name, util):
    return panel.usage.Bucket(name=name, utilization=util, resets_at=None)


def test_only_session_and_weekly_get_a_permanent_meter():
    """The endpoint returns a dozen-odd windows, most of them null and several
    with internal codenames that mean nothing here."""
    snapshot = panel.usage.Snapshot(buckets=(
        _bucket("five_hour", 30.0), _bucket("nimbus_quill", 0.0),
        _bucket("seven_day", 49.0), _bucket("tangelo", 12.0)), fetched=True)
    assert [b.name for b in panel.shown_buckets(snapshot)] == ["five_hour", "seven_day"]


def test_a_bucket_that_is_actually_spent_is_shown_whatever_it_is_called():
    """`usage.check` refuses on the worst bucket, so an exhausted one that the
    rail hid would leave a refusal with nothing on screen explaining it."""
    snapshot = panel.usage.Snapshot(buckets=(
        _bucket("five_hour", 30.0), _bucket("iguana_necktie", 100.0)), fetched=True)
    assert [b.name for b in panel.shown_buckets(snapshot)] == ["five_hour", "iguana_necktie"]


def test_the_meters_are_labelled_in_the_words_the_plan_uses(server):
    base, _ = server
    _, text = _get(base, "now")
    assert "nimbus" not in text.lower()


# --------------------------------------------------------- the run page is honest


def test_an_old_record_is_not_presented_as_this_run(server):
    """The newest record can be weeks old. Shown under "This run" with a start
    time and no date, it reads as this morning — which is what it did."""
    base, root = server
    records = root / ".ai" / "runs" / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / "20260801-101700.json").write_text(json.dumps({
        "started": "2026-08-01T10:17:00", "finished": "2026-08-01T11:00:00",
        "complete": True, "kind": "night", "host": "somebox", "cost_usd": 3.0,
        "dispatched": [{"card": "old-card", "outcome": "reviewed", "landed": "old-card: → done/"}],
        "skipped": [{"card": "already-finished", "reason": "attempts: 3 — at the limit"}],
    }), encoding="utf-8")

    _, text = _get(base, "run")

    assert "Last run" in text
    assert "This run" not in text
    assert "2026-08-01" in text, "the date is what stops it reading as today"


def test_not_taken_reads_the_board_not_a_stale_skip_list(server):
    """The record's `skipped` belongs to whichever run wrote it — two weeks ago
    here — so it confidently listed cards that had since been finished."""
    base, root = server
    records = root / ".ai" / "runs" / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / "20260801-101700.json").write_text(json.dumps({
        "started": "2026-08-01T10:17:00", "complete": True, "kind": "night",
        "dispatched": [],
        "skipped": [{"card": "already-finished", "reason": "was skipped a fortnight ago"}],
    }), encoding="utf-8")
    _card(root, "tasks", "needs-a-human", unattended="false")

    _, text = _get(base, "run")

    assert "already-finished" not in text, "a card from a stale run's skip list"
    assert "needs-a-human" in text, "the board's own undispatchable card"


# ------------------------------------------------- the waiver the rule requires


def _spend_on(monkeypatch):
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(
                            fetched=True, email="k@example.com", has_extra_usage_enabled=True))


def test_the_veto_is_waived_by_an_explicit_human_override(monkeypatch):
    """`feedback_account_dispatch`: the exclusion is "a default, enforced against
    tooling, waived only by a human". A veto with no override would be the tool
    deciding, which is the thing the rule forbids."""
    _spend_on(monkeypatch)
    with pytest.raises(panel.PanelError):
        panel._guard_dispatch_account()
    panel._guard_dispatch_account(waived=True)   # must not raise


def test_a_dispatch_never_account_is_also_waivable(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    panel.select_account(root, "spendy")
    with pytest.raises(panel.PanelError):
        panel._guard_dispatch_account()
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(fetched=True))
    panel._guard_dispatch_account(waived=True)   # must not raise


def test_the_override_travels_with_the_request_and_is_never_stored(server, monkeypatch):
    """Per invocation, never persisted — so the next request without the tick is
    refused again."""
    base, root = server
    _spend_on(monkeypatch)
    monkeypatch.setattr(panel, "spawn_background", lambda *a, **k: 11)

    allowed, _ = _post(base, "api/dispatch", {"card_id": "x", "allow_paid": True})
    refused, _ = _post(base, "api/dispatch", {"card_id": "x"})

    assert allowed == 200
    assert refused == 400, "the waiver outlived the click that granted it"


def test_the_override_reaches_the_commands_that_check_the_money_rule(server, monkeypatch):
    base, root = server
    _spend_on(monkeypatch)
    seen = {}
    monkeypatch.setattr(panel, "spawn_background",
                        lambda module, args, r: seen.setdefault("args", list(args)) or 5)

    status, data = _post(base, "api/chores/run", {"allow_paid": True})

    assert status == 200, data
    assert seen["args"] == ["--allow-paid"]


# ------------------------------------------- switching by signing in, not by config


def test_meters_are_cached_per_account_not_just_per_minute(monkeypatch):
    """`claude auth login` swaps the identity under the *same* config directory,
    so the credential path never changes. A cache keyed on time alone would serve
    the previous account's headroom under the new account's name."""
    calls = []
    monkeypatch.setattr(panel.usage, "read",
                        lambda creds=None, **k: calls.append(1) or panel.usage.Snapshot())
    panel.read_meters(None, account_key="account-a")
    panel.read_meters(None, account_key="account-a")
    assert len(calls) == 1
    panel.read_meters(None, account_key="account-b")
    assert len(calls) == 2, "the reading for one account was reused for another"


def test_switching_account_launches_the_sign_in_and_does_not_perform_it(server, monkeypatch):
    """The panel is a launcher. A browser sign-in is not something it can carry
    out, and it must not pretend to."""
    base, root = server
    opened = {}
    monkeypatch.setattr(panel, "open_terminal",
                        lambda r, *cmd: opened.setdefault("cmd", list(cmd)))

    status, data = _post(base, "api/switch-account", {})

    assert status == 200, data
    assert opened["cmd"] == ["claude", "auth", "login"]
    assert "reload" in data["message"]


def test_switching_account_drops_the_cached_meters(server, monkeypatch):
    base, root = server
    monkeypatch.setattr(panel, "open_terminal", lambda r, *cmd: None)
    panel._METERS = (dt.datetime.now(), "old-account", panel.usage.Snapshot())
    _post(base, "api/switch-account", {})
    assert panel._METERS is None


def test_the_rail_offers_the_switch_even_with_no_accounts_configured(server):
    """One config directory is the ordinary case; the dropdown is for the other
    one and must not be the only way to change account."""
    base, root = server
    _, text = _get(base, "now")
    assert "Switch account" in text


# ------------------------------------------------- the System page (install, update)


def test_the_panel_renders_in_a_repo_with_no_install(tmp_path, monkeypatch):
    """The whole panel-first install rests on this. `bootstrap` writes the launchers
    *before* `init`, so the first thing a new project does is open this panel in a repo
    with no `.ai/manifest.toml` — and the Setup page is what performs the install.

    It did not hold when first written: `read_context` reaches `default_base`, which
    goes through `branches.integration` and raises rather than guessing. Caught by
    running it, not by reading it.
    """
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q")
    from nightshift import init as _init
    _init.apply(_init.bootstrap_plan(root))

    assert not panel.installed(root), "a bootstrap receipt is not an install"
    html = panel.render_page("system", root)
    assert "Set up nightshift" in html
    assert "/api/setup" in html
    # ...and the ordinary pages must not explode either; the rail is on all of them.
    assert panel.render_page("now", root)


def test_root_goes_to_system_when_there_is_no_install(tmp_path):
    """`/now` in an uninstalled repo is five empty sections and no hint that the
    install never happened — which is exactly the state a first visitor is in."""
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q")

    panel.Handler.root = root
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        # Followed, like `test_root_redirects_to_now` — where it *lands* is the claim,
        # and asserting on the 302 itself would pass just as well if /system 404'd.
        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            assert resp.geturl().endswith("/system")
            assert resp.status == 200
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def test_setup_launches_the_install_skill_and_does_not_perform_it(server, monkeypatch):
    """Two of the install's four questions are never guessed by policy, and a headless
    `-p` agent cannot ask them. So the button opens an interactive session running the
    one install driver there has ever been — it does not become a second one."""
    base, root = server
    opened = {}
    monkeypatch.setattr(panel, "open_terminal",
                        lambda r, *cmd: opened.setdefault("cmd", list(cmd)))

    status, data = _post(base, "api/setup", {})

    assert status == 200, data
    assert opened["cmd"] == ["claude", "/install-nightshift"]


def test_work_on_a_card_carries_the_card_the_model_and_the_charter(server, monkeypatch):
    """The whole point of the rename. `Start session` opened a bare CLI, so the
    person had to tell it which card, which model and which agent — three things the
    caller already knew (Karel, 2026-08-17: *"It is not related to the card, nor my
    actually used account and I guess the model is default too"*)."""
    base, root = server
    _card(root, "tasks", "stun-grenade")
    opened = []
    monkeypatch.setattr(panel, "open_terminal",
                        lambda r, *cmd: opened.append(list(cmd)))

    status, data = _post(base, "api/work", {"card": "stun-grenade"})

    assert status == 200, data
    argv = opened[0]
    assert argv[-1].startswith("Work this card"), "the prompt is not the trailing arg"
    assert "stun-grenade" in argv[-1], "the card body never reached the prompt"
    # sonnet, from the tier-binding doc — not a literal in this module and not the
    # CLI's default.
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--agent") + 1] == "code-thread"
    assert argv[argv.index("--name") + 1] == "stun-grenade"
    assert "-p" not in argv, "an interactive session must not be a print dispatch"
    assert "stun-grenade" in data["message"] and "worker tier" in data["message"]


def test_work_on_a_card_tells_it_the_branch_to_cut_and_the_base_to_leave_alone(
        server, monkeypatch):
    base, root = server
    _card(root, "tasks", "stun-grenade")
    opened = []
    monkeypatch.setattr(panel, "open_terminal", lambda r, *cmd: opened.append(list(cmd)))

    _post(base, "api/work", {"card": "stun-grenade"})

    prompt = opened[0][-1]
    assert "ai/stun-grenade" in prompt, "no branch named — the session would work on the base"
    assert "Do not move the card between lanes" in prompt
    assert "verdict" in prompt.lower(), "the interactive prompt must cancel the verdict file"


def test_work_on_a_note_gets_the_note_and_no_charter(server, monkeypatch):
    """A note routed to a human is the route that exists *because* no agent was
    dispatched — so it must not arrive wearing one."""
    base, root = server
    (root / "Board" / "inbox" / "taser.md").write_text("Taser should stun.\n",
                                                       encoding="utf-8", newline="")
    opened = []
    monkeypatch.setattr(panel, "open_terminal", lambda r, *cmd: opened.append(list(cmd)))

    status, data = _post(base, "api/work", {"note": "taser.md"})

    assert status == 200, data
    argv = opened[0]
    assert "--agent" not in argv, "a note is not a card and has no worker"
    assert "Taser should stun." in argv[-1]
    assert argv[argv.index("--model") + 1] == "opus", "notes run at the lead tier"


def test_work_refuses_a_card_or_note_that_is_not_there(server, monkeypatch):
    base, _ = server
    monkeypatch.setattr(panel, "open_terminal",
                        lambda *a, **k: pytest.fail("a session was opened anyway"))

    for body, wanted in (({"card": "ghost"}, "no card"),
                         ({"note": "ghost.md"}, "no note"),
                         ({}, "no card or note")):
        status, data = _post(base, "api/work", body)
        assert status == 400, (body, data)
        assert wanted in data["message"], (body, data)


def test_the_interactive_permission_mode_is_not_the_headless_one(server, monkeypatch):
    """`permission_mode` answers "what may a worker do with nobody to ask", and on
    Karel's box that is `bypassPermissions`. Reusing it here would hand every session
    the panel opens fewer guardrails than the same person gets in their own editor."""
    base, root = server
    # `.ai/host.json`, the untracked per-machine override — read unconditionally.
    # `hosts.json` is keyed by `socket.gethostname()`, so a `{"default": ...}` entry
    # there is read by nothing and this test would pass without the code under test.
    (root / ".ai" / "host.json").write_text(
        json.dumps({"permission_mode": "bypassPermissions"}), encoding="utf-8", newline="")
    _card(root, "tasks", "stun-grenade")
    opened = []
    monkeypatch.setattr(panel, "open_terminal", lambda r, *cmd: opened.append(list(cmd)))

    _post(base, "api/work", {"card": "stun-grenade"})

    argv = opened[0]
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_remote_control_is_on_by_default_and_never_eats_the_session_name(
        server, monkeypatch):
    """`--remote-control` takes an *optional* positional name, so the token after it
    must start with `-` or the name is swallowed and the prompt shifts."""
    base, root = server
    _card(root, "tasks", "stun-grenade")
    opened = []
    monkeypatch.setattr(panel, "open_terminal", lambda r, *cmd: opened.append(list(cmd)))

    _post(base, "api/work", {"card": "stun-grenade"})

    argv = opened[0]
    assert "--remote-control" in argv
    assert argv[argv.index("--remote-control") + 1].startswith("-")


def test_remote_control_can_be_turned_off_in_the_host_config(server, monkeypatch):
    base, root = server
    (root / ".ai" / "host.json").write_text(
        json.dumps({"remote_control": False}), encoding="utf-8", newline="")
    _card(root, "tasks", "stun-grenade")
    opened = []
    monkeypatch.setattr(panel, "open_terminal", lambda r, *cmd: opened.append(list(cmd)))

    _post(base, "api/work", {"card": "stun-grenade"})

    assert "--remote-control" not in opened[0]


def test_the_general_session_button_survives_and_carries_no_work(server, monkeypatch):
    """Karel asked to keep exactly one: *"there can be a one button for that (so I
    don't have to use VSC for general stuff)"*."""
    base, _ = server
    opened = []
    monkeypatch.setattr(panel, "open_terminal", lambda r, *cmd: opened.append(list(cmd)))

    status, data = _post(base, "api/session", {})

    assert status == 200, data
    argv = opened[0]
    assert "--agent" not in argv and "--model" not in argv
    assert argv[argv.index("--name") + 1] == "general"
    assert "no card" in data["message"]


def test_no_row_still_offers_a_session_that_does_nothing(server):
    """The label described what the panel did, not what the row was for — and all
    three rows posted the same empty endpoint."""
    base, root = server
    _card(root, "tasks", "stun-grenade")
    (root / "Board" / "inbox" / "taser.md").write_text("x\n", encoding="utf-8", newline="")

    status, html = _get(base, "")

    assert status == 200
    assert "Start session" not in html
    assert "Work on this" in html
    # The one exception, and it says so.
    assert html.count("post('/api/session',{})") == 1


def test_every_launcher_passes_the_selected_account(monkeypatch, tmp_path):
    """`spawn_background` passed `_dispatch_env()` and this did not, so the account
    dropdown was a lie for every button that opens a terminal."""
    spawned = []
    monkeypatch.setattr(panel, "claude_binary", lambda: "claude.exe")
    monkeypatch.setattr(panel.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(kw.get("env")) or _FakeProc())
    panel._ACCOUNT = panel.AccountState(label="alt", config_dir=str(tmp_path / "alt"),
                                        dispatch="always")

    panel.open_terminal(tmp_path, "claude")

    assert spawned[0] is not None, "the terminal inherited the server's own environment"
    assert spawned[0]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "alt")


def test_open_terminal_resolves_claude_instead_of_trusting_the_new_window_s_path(
        monkeypatch, tmp_path):
    """The CLI is not always on PATH — on Karel's box it lives in
    `%USERPROFILE%\\.local\\bin`. Passing the bare name meant every launcher button
    opened a console window reading `'claude' is not recognized` (2026-08-17)."""
    spawned = []
    monkeypatch.setattr(panel, "claude_binary", lambda: r"C:\Users\k\.local\bin\claude.exe")
    monkeypatch.setattr(panel.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or _FakeProc())

    panel.open_terminal(tmp_path, "claude", "--resume", "abc123")

    argv = spawned[0]
    assert "claude" not in argv, "the bare name was passed through to the new window"
    assert r"C:\Users\k\.local\bin\claude.exe" in argv
    assert argv[-2:] == ["--resume", "abc123"] or argv[-1].endswith("--resume abc123")


def test_open_terminal_refuses_rather_than_opening_a_window_that_cannot_work(
        monkeypatch, tmp_path):
    """A window whose only content is `not recognized` is worse than a sentence the
    page can render."""
    monkeypatch.setattr(panel, "claude_binary", lambda: None)
    monkeypatch.setattr(panel.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("a terminal was opened anyway"))

    with pytest.raises(panel.PanelError, match="CLAUDE_BIN"):
        panel.open_terminal(tmp_path, "claude")


def test_open_terminal_leaves_a_non_claude_command_alone(monkeypatch, tmp_path):
    """The resolver is for the one name that needs it, not a rewrite of every argv."""
    spawned = []
    monkeypatch.setattr(panel, "claude_binary",
                        lambda: pytest.fail("resolved a command that is not the CLI"))
    monkeypatch.setattr(panel.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv) or _FakeProc())

    panel.open_terminal(tmp_path, "git", "status")

    assert "git" in spawned[0]


class _FakeProc:
    pid = 4242


def test_every_update_write_is_a_subprocess_not_an_import(server, monkeypatch):
    """The panel may *read* the survey to render the page — that is arithmetic and
    file I/O, the same licence `board` and `usage` have. Every verb that writes goes
    out as the command a person would have typed."""
    base, root = server
    seen = []
    monkeypatch.setattr(panel, "run_command",
                        lambda module, args, r, **kw: seen.append((module, args))
                        or subprocess.CompletedProcess([], 0, "ok", ""))

    _post(base, "api/update/apply", {})
    _post(base, "api/update/take", {"path": ".claude/agents/code-thread.md"})
    _post(base, "api/update/keep", {"path": ".claude/agents/code-thread.md"})

    assert seen == [("update", ["--apply"]),
                    ("update", ["--take", ".claude/agents/code-thread.md"]),
                    ("update", ["--keep", ".claude/agents/code-thread.md"])]


def test_the_panel_does_not_resolve_a_conflict_in_process():
    """`survey`, `find` and `diff` are reads and are allowed. `apply`, `take`, `keep`
    and `merge` change the tree, and the panel owns no logic that does that."""
    source = Path(panel.__file__).read_text(encoding="utf-8")
    for forbidden in ("update.apply(", "update.take(", "update.keep(", "update.merge("):
        assert forbidden not in source, (
            f"{forbidden!r} found — resolving a conflict must go out as "
            f"`python -m nightshift.update`, not happen inside the server")


def test_merging_a_file_answers_to_the_account_veto(server, monkeypatch):
    """A merge is a real agent session on a real file, so it spends — and a spending
    verb is refused server-side, not merely hidden in the UI."""
    base, root = server
    monkeypatch.setattr(panel, "_guard_dispatch_account",
                        lambda waived=False: (_ for _ in ()).throw(
                            panel.PanelError("account refused")))

    status, data = _post(base, "api/update/merge", {"path": "x.md"})

    assert status == 400, data
    assert "account refused" in data["message"]


def test_uninstall_needs_the_project_name_typed_before_it_does_anything(server, monkeypatch):
    """The dialog is a courtesy; this is the guard. A crafted POST must fail exactly
    where a browser click would have declined to send one."""
    base, root = server
    seen = []
    monkeypatch.setattr(panel, "run_command",
                        lambda module, args, r, **kw: seen.append((module, args))
                        or subprocess.CompletedProcess([], 0, "ok", ""))

    _post(base, "api/system/uninstall", {})
    _post(base, "api/system/uninstall", {"confirm": "not-the-name"})
    assert seen == [("uninstall", []), ("uninstall", [])], "a dry run is all that may run"

    _post(base, "api/system/uninstall", {"confirm": root.name})
    assert seen[-1] == ("uninstall", ["--yes"])


def test_the_repair_pass_answers_to_the_account_veto(server, monkeypatch):
    base, root = server
    monkeypatch.setattr(panel, "_guard_dispatch_account",
                        lambda waived=False: (_ for _ in ()).throw(
                            panel.PanelError("account refused")))

    status, data = _post(base, "api/system/fix", {})

    assert status == 400, data


# --------------------------------------------------------------------------
# Saying what happened. On 2026-08-17 "Classify all" ran `ingest` to completion
# — thirteen notes routed, `Routing.md` rewritten — and the panel reported none
# of it: the spawn discarded the output, the rail only knew about the runner's
# own heartbeat, and the inbox page listed every routed note as unclassified.
# Three surfaces, one complaint, and each is pinned here.
# --------------------------------------------------------------------------


def _finished_job(root: Path, label: str, *, code: int = 0,
                  ago: dt.timedelta = dt.timedelta(minutes=3)) -> jobs.Job:
    started = dt.datetime.now() - ago
    job = jobs.record(root, label, ["python", "-m", f"nightshift.{label}"], now=started)
    job.finished = (started + dt.timedelta(seconds=90)).isoformat(timespec="seconds")
    job.exit_code = code
    jobs.save(root, job)
    return job


def test_a_running_job_is_on_every_page_because_the_rail_is(server):
    base, root = server
    job = jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])
    for page in panel.PAGES:
        _, text = _get(base, page)
        assert "ingest" in text, f"{page} does not say what is running"
        assert f"/log/{job.ident}" in text, f"{page} does not link the output"


def test_a_job_that_failed_says_so_rather_than_saying_nothing(server):
    base, root = server
    _finished_job(root, "ingest", code=3)
    _, text = _get(base, "now")
    assert "failed" in text
    assert "exit 3" in text


def test_a_finished_job_leaves_the_rail_once_it_stops_being_news():
    """The rail is a status line, not a history page — and a red line that
    outlives the moment it described is the one people learn to scroll past."""
    now = dt.datetime.now()
    fresh = jobs.Job(ident="a", label="ingest",
                     started=(now - dt.timedelta(minutes=5)).isoformat(),
                     finished=(now - dt.timedelta(minutes=4)).isoformat(), exit_code=0)
    old = jobs.Job(ident="b", label="chores",
                   started=(now - dt.timedelta(hours=9)).isoformat(),
                   finished=(now - dt.timedelta(hours=8)).isoformat(), exit_code=1)
    assert [j.ident for j, _ in panel.shown_jobs([fresh, old], now=now)] == ["a"]


def test_a_jobs_output_is_readable_from_the_browser(server):
    base, root = server
    job = _finished_job(root, "ingest")
    jobs.log_path(root, job.ident).write_text(
        "  classifying with sonnet ...\n  wrote Routing.md\n", encoding="utf-8")

    status, text = _get(base, f"log/{job.ident}")
    assert status == 200
    assert "classifying with sonnet" in text
    assert "wrote Routing.md" in text


def test_a_job_id_that_is_not_one_is_a_page_saying_so_not_a_crash(server):
    base, _ = server
    status, text = _get(base, "log/20260817-000000-nothing")
    assert status == 200
    assert "no such job" in text


# ------------------------------------------------------------- the inbox page


_ROUTED = """\
# Routing - 2026-08-17 09:51

2 note(s) in `inbox/`.

## Do now - inline (1)

You, at the keyboard.

- **needs-you.md** - the note itself says to wait for a direction

## Chores - batch overnight (0)

_none_

## Scribe - needs the envelope only (1)

Already elaborated.

- **ready.md** - already worked out, only the envelope is missing

## Waiting on triage (0)

_none_
"""


def _notes(root: Path, **bodies: str) -> None:
    lane = root / "Board" / "inbox"
    for name, body in bodies.items():
        (lane / name).write_text(body, encoding="utf-8")


def test_a_classified_note_stops_being_listed_as_unclassified(server):
    """The complaint, exactly: the routing had run, said so in a file the page
    linked, and the page went on calling every note unclassified."""
    base, root = server
    _notes(root, **{"ready.md": "a", "needs-you.md": "b"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")

    _, text = _get(base, "inbox")

    assert "Not yet classified" not in text
    assert "Scribe" in text and "Do now" in text
    assert "already worked out, only the envelope is missing" in text
    assert "Routed 17 Aug 09:51" in text


def test_a_note_added_after_the_pass_is_the_only_unclassified_one(server):
    base, root = server
    _notes(root, **{"ready.md": "a", "needs-you.md": "b", "brand-new.md": "c"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")

    _, text = _get(base, "inbox")

    assert "Not yet classified — 1" in text
    assert "1 note(s) added since" in text


def test_a_note_edited_after_it_was_routed_says_so(server):
    """The routing describes text that has since changed, and the whole value of
    the view is that it says what is true of the note as it stands."""
    base, root = server
    _notes(root, **{"ready.md": "a", "needs-you.md": "b"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")
    edited = root / "Board" / "inbox" / "ready.md"
    stamp = dt.datetime(2026, 8, 17, 18, 0).timestamp()
    os.utime(edited, (stamp, stamp))

    _, text = _get(base, "inbox")
    assert "edited since routing" in text


def test_with_no_routing_pass_the_page_says_which_button_fills_it_in(server):
    base, root = server
    _notes(root, **{"lonely.md": "a"})
    _, text = _get(base, "inbox")
    assert "No routing pass yet" in text
    assert "Not yet classified" in text


# ----------------------------------------------------------------- the chores


def _chore(root: Path, card_id: str) -> Path:
    path = root / "Board" / "tasks" / f"{card_id}.md"
    text = CARD.format(id=card_id, state="tasks", unattended="true")
    path.write_text(text.replace("tier: worker", "tier: worker\nkind: chore"),
                    encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"chore {card_id}")
    return path


def test_a_chore_is_not_filed_under_work_that_needs_you_at_the_keyboard(server):
    """`runner.select` calls this "not a refusal — a routing fact": the chore
    batch runs it, unattended. Filing it beside `unattended: false` said the
    opposite, under a chip that cut the explanation off mid-sentence."""
    base, root = server
    _chore(root, "a-chore")

    ctx = panel.read_context(root)
    assert [c.card.id for c in ctx.chores] == ["a-chore"]
    assert "a-chore" not in [c.card.id for c in ctx.do_now]

    _, text = _get(base, "now")
    head, _, rest = text.partition("Chores")
    assert "a-chore" in rest, "the chore belongs in its own section"
    assert "a-chore" not in head, "and not in the sections above it"


def test_a_chore_still_counts_towards_the_rails_now_total(server):
    base, root = server
    _chore(root, "a-chore")
    assert panel.read_context(root).counts()["now"] == 1


def test_a_chore_that_needs_another_machine_is_filed_as_that_instead(tmp_path):
    """Two true facts, and the more useful one wins: the batch here cannot take
    it either."""
    root = _repo(tmp_path)
    path = _chore(root, "gpu-chore")
    path.write_text(path.read_text(encoding="utf-8").replace(
        "kind: chore", "kind: chore\nrequires: gpu-box"), encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "requires")

    ctx = panel.read_context(root)
    assert [c.card.id for c in ctx.elsewhere] == ["gpu-chore"]
    assert ctx.chores == []


# --------------------------------------------------------------------------
# The lifecycle, from the page. Every route's next step is a button on the row
# it belongs to: before this the inbox offered Edit and Open to all four, the
# bar's two buttons ran the same classify pass twice, and "Launch triage" named
# no note for the one route whose whole discipline is choosing one deliberately.
# --------------------------------------------------------------------------


def test_a_writable_note_offers_the_verb_that_cards_it(server):
    base, root = server
    _notes(root, **{"ready.md": "a", "needs-you.md": "b"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")

    _, text = _get(base, "inbox")
    assert "Write the card" in text
    assert "/api/ingest/one" in text


def _no_veto(monkeypatch) -> None:
    """Take the account veto out of the picture for a test that is not about it.

    Without this the guard reads whatever `~/.claude.json` says on the machine
    running the suite — the same non-hermetic read
    `test_guard_refuses_when_the_live_identity_shows_spend_enabled` exists to keep
    out of tests that are about something else.
    """
    monkeypatch.setattr(panel.usage, "read_identity",
                        lambda path: panel.usage.Identity(
                            fetched=True, has_extra_usage_enabled=False))


def test_writing_one_card_runs_the_per_note_verb(server, monkeypatch):
    base, root = server
    seen = []
    monkeypatch.setattr(panel, "spawn_background",
                        lambda module, args, r: seen.append((module, args)) or 7)
    _no_veto(monkeypatch)

    status, data = _post(base, "api/ingest/one", {"note": "ready.md"})

    assert status == 200, data
    assert seen == [("ingest", ["--only", "ready.md"])]
    assert "ready.md" in data["message"]


def test_writing_one_card_needs_a_note(server, monkeypatch):
    base, _ = server
    _no_veto(monkeypatch)
    status, data = _post(base, "api/ingest/one", {})
    assert status == 400
    assert "no note given" in data["message"]


def test_the_write_button_runs_the_second_step_not_a_second_classify(server, monkeypatch):
    """The bar used to offer "Classify all" and "Classify + write cards" — the same
    first half twice, the second unable to be opt-in about spending on the second
    half, and paying for a fresh pass to re-learn what Routing.md already said."""
    base, root = server
    seen = []
    monkeypatch.setattr(panel, "spawn_background",
                        lambda module, args, r: seen.append(args) or 7)
    _no_veto(monkeypatch)

    _post(base, "api/ingest", {"write": True})
    assert seen == [["--write-cards"]]

    _post(base, "api/ingest", {})
    assert seen[-1] == [], "classify spends on the classifier and nothing else"


def test_triage_is_launched_on_the_note_you_picked(server, monkeypatch):
    base, root = server
    opened = []
    monkeypatch.setattr(panel, "open_terminal",
                        lambda r, *command: opened.append(command))

    status, data = _post(base, "api/triage", {"note": "stun-grenade.md"})

    assert status == 200, data
    argv = list(opened[0])
    assert argv[argv.index("--agent") + 1] == "triage"
    assert "Board/inbox/stun-grenade.md" in argv[-1]
    assert "stun-grenade.md" in data["message"]


def test_triage_goes_through_the_same_launcher_as_every_other_session(server, monkeypatch):
    """It used to hand-roll `["claude", "--agent", "triage"]`, which is how it came to
    be the one launch with no account, no permission mode and no session name — the
    same shape as `Start session`, one row further down the page."""
    base, _ = server
    opened = []
    monkeypatch.setattr(panel, "open_terminal",
                        lambda r, *command: opened.append(command))

    _post(base, "api/triage", {"note": "stun-grenade.md"})

    argv = list(opened[0])
    assert argv[0] != "claude", "the bare name is back — resolution was skipped"
    assert "--permission-mode" in argv
    assert argv[argv.index("--name") + 1] == "triage stun-grenade.md"


def test_triage_with_no_note_still_opens_the_charter(server, monkeypatch):
    """The old behaviour, kept: a session you drive yourself is legitimate."""
    base, _ = server
    opened = []
    monkeypatch.setattr(panel, "open_terminal",
                        lambda r, *command: opened.append(command))

    _post(base, "api/triage", {})

    argv = list(opened[0])
    assert argv[argv.index("--agent") + 1] == "triage"
    assert argv[argv.index("--name") + 1] == "triage"
    # No note means no prompt, so the argv ends on the last flag's value rather than
    # on a trailing positional. The charter still arrives — via `--agent`.
    assert argv[-2] == "--add-dir"


def test_the_triage_section_lists_the_notes_not_the_report_file(server):
    """It used to be one row for `Routing.md` and one button that named no note."""
    base, root = server
    _notes(root, **{"forky.md": "a"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED.replace(
        "## Waiting on triage (0)\n\n_none_",
        "## Waiting on triage (1)\n\nThe expensive route.\n\n"
        "- **forky.md** - the fork cannot be posed without the code"), encoding="utf-8")

    _, text = _get(base, "now")
    assert "forky.md" in text
    assert "the fork cannot be posed without the code" in text
    assert "Routing.md" not in text.split("Waiting on triage")[1]


def test_an_inline_note_is_on_the_page_about_your_own_work(server):
    """`/now` answered "what needs me" from board lanes only, and an inline-routed
    note is by definition work for Karel that will never become a card."""
    base, root = server
    _notes(root, **{"needs-you.md": "b"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")

    _, text = _get(base, "now")
    head, _, rest = text.partition("Notes routed to you")
    assert "needs-you.md" in rest
    assert "the note itself says to wait for a direction" in rest
    assert panel.read_context(root).counts()["now"] == 1


def test_a_carded_note_stops_being_counted_as_needing_you(server):
    """The count follows the lane, so carding a note removes it from both."""
    base, root = server
    _notes(root, **{"needs-you.md": "b"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")
    assert panel.read_context(root).counts()["now"] == 1

    (root / "Board" / "inbox" / "needs-you.md").unlink()
    assert panel.read_context(root).counts()["now"] == 0


def test_an_inline_note_can_be_closed_from_the_page(server):
    """The gesture the inline route had no process for. Karel, 2026-08-17: "Inline
    process should move the card when we are done."

    Runs the real `boardcmd close` — the panel's founding rule is that a button is
    a CLI verb, and a board write that only the panel can perform is a board write
    nobody can audit from a terminal.
    """
    base, root = server
    (root / "Board" / "inbox" / "tidy the hotbar.md").write_text(
        "spacing is off\n", encoding="utf-8")

    status, data = _post(base, "api/close", {"note": "tidy the hotbar.md"})

    assert status == 200, data
    assert "done/tidy-the-hotbar.md" in data["message"]
    assert not (root / "Board" / "inbox" / "tidy the hotbar.md").exists()
    assert (root / "Board" / "done" / "tidy-the-hotbar.md").is_file()


def test_closing_needs_a_note(server):
    base, _ = server
    status, data = _post(base, "api/close", {})
    assert status == 400
    assert "no note given" in data["message"]


def test_an_inline_note_carries_the_button_on_both_pages_it_appears_on(server):
    base, root = server
    _notes(root, **{"needs-you.md": "b"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")

    for page in ("inbox", "now"):
        _, text = _get(base, page)
        assert "/api/close" in text, f"{page} offers no way to close it"


def test_every_note_can_be_sent_to_triage_whatever_the_routing_said(server):
    """The route is a recommendation from an agent that never opened the codebase.

    Karel, 2026-08-17: "change the classification if needed (like run triage on non
    triaged card for example)". Overruling a cheap classifier is the design working
    — and triage is the one action that cannot be spent wrongly by accident, since
    it opens a session you are sitting in front of.
    """
    base, root = server
    _notes(root, **{"ready.md": "a", "needs-you.md": "b", "unrouted.md": "c"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED, encoding="utf-8")

    _, text = _get(base, "inbox")
    for note in ("ready.md", "needs-you.md", "unrouted.md"):
        row = text.split(note, 1)[1].split('class="row"', 1)[0]
        assert "/api/triage" in row, f"{note} cannot be sent to triage"


def test_the_scribe_is_not_offered_for_a_note_routed_to_triage(server):
    """The override that *does* stay closed. `ingest --only` refuses it and says
    why: it spends on an agent forbidden to read the code, and a confidently wrong
    `## Acceptance` is dispatchable and worse than no card."""
    base, root = server
    _notes(root, **{"forky.md": "a"})
    (root / board.ROUTING_VIEW).write_text(_ROUTED.replace(
        "## Waiting on triage (0)\n\n_none_",
        "## Waiting on triage (1)\n\nThe expensive route.\n\n"
        "- **forky.md** - a real fork"), encoding="utf-8")

    _, text = _get(base, "inbox")
    row = text.split("forky.md", 1)[1].split('class="row"', 1)[0]
    assert "/api/ingest/one" not in row


def test_a_note_with_a_route_the_page_has_no_group_for_still_renders(server):
    """Neither producer can emit one today — `classify` folds an unknown route to
    `inline`, `parse_report` only assigns routes it has headings for. The guard is
    there because a note silently missing from the page is the exact class of
    failure this pass was about, and "no producer can do that" is about today."""
    base, root = server
    _notes(root, **{"odd.md": "a"})
    (root / board.ROUTING_VIEW).write_text(
        "# Routing - 2026-08-17 09:51\n\n## Bespoke lane (1)\n\nWho knows.\n\n"
        "- **odd.md** - routed somewhere this page has never heard of\n",
        encoding="utf-8")

    _, text = _get(base, "inbox")
    assert "odd.md" in text
    assert "Not yet classified" in text


def test_the_run_page_leads_with_what_is_running_not_with_the_night(server):
    """Karel, 2026-08-17, with a live `ingest` and the page headed "Last run —
    2026-08-02": "I'm running the ingest, but run page doesn't show details about
    it, it shows last runner or something like that." A night is one kind of run
    and the page knew only that kind.
    """
    base, root = server
    job = jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])

    _, text = _get(base, "run")
    head, _, rest = text.partition("Last run")
    assert "Running now" in head
    assert "ingest" in head
    assert f"/log/{job.ident}" in head, "with a way into its output"


def test_only_the_running_job_is_at_the_top_and_the_rest_is_history(server):
    """Karel: "started from here — I don't think we need a full history at the top
    of the page ... only current run." The top of a page called Run is where "what
    is happening" belongs; a fortnight of finished jobs above the night's own
    roster pushes the thing you came for below the fold."""
    base, root = server
    done = jobs.record(root, "chores", ["python", "-c", "pass"])
    done.finished, done.exit_code = done.started, 0
    jobs.save(root, done)
    live = jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])

    _, text = _get(base, "run")
    # Scoped to the section, not to "everything above the night": the status rail
    # sits above it on every page and legitimately shows a job that finished in the
    # last couple of hours. The claim here is about which section owns which job.
    section = text.split("Running now", 1)[1].split("</section>", 1)[0]
    assert live.ident in section
    assert done.ident not in section, "a finished job is history, not the current run"
    assert done.ident in text.split("Earlier from here")[1], "and history is at the foot"


def test_a_finished_job_stays_on_the_run_page_after_it_leaves_the_rail():
    """The rail shows what is news; the Run page is where you go to ask what
    happened, so its window is `jobs.KEEP` rather than two hours."""
    now = dt.datetime.now()
    old = jobs.Job(ident="b", label="chores",
                   started=(now - dt.timedelta(hours=9)).isoformat(),
                   finished=(now - dt.timedelta(hours=8)).isoformat(), exit_code=1)
    assert panel.shown_jobs([old], now=now) == []
    assert panel.JOBS_ON_RUN_PAGE >= jobs.KEEP


def test_the_rail_does_not_claim_nothing_is_happening_while_a_job_runs(server):
    """"No run in progress" over a pulsing `ingest` is a contradiction, and it is
    one the reader resolves against us. The eyebrow is a claim about the queue."""
    base, root = server
    _, idle = _get(base, "now")
    assert "No run in progress" in idle

    jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])
    _, busy = _get(base, "now")
    assert "No card dispatching" in busy
    assert "No run in progress" not in busy


def test_a_running_ingest_is_a_roster_of_notes_not_a_log_tail(server):
    """Karel, 2026-08-17: "I would expect for the ingest (and other bulk actions)
    similar overview as for runs. Card xxx classified as XYZ, Card yyy in progress."

    A log tail is what a command happens to print; a roster is the question
    answered.
    """
    base, root = server
    job = jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])
    jobs.log_path(root, job.ident).write_text("""\
ingest: 3 note(s) to card
  [1/3] scribe: alpha.md
    -> alpha
  [2/3] scribe: beta.md
    bounced to triage - needs the code
  [3/3] scribe: gamma.md
""", encoding="utf-8")

    _, text = _get(base, "run")
    top = text.split("Last run")[0]
    assert "carded as" in top and "alpha" in top
    assert "bounced to triage" in top and "needs the code" in top
    assert "gamma.md" in top, "the one in flight is on the roster too"
    assert "2/3" in top, "and so is how far it has got"
    assert 'class="joblog"' not in top, "a verb we can parse is not shown as a tail"


def test_a_verb_whose_output_we_do_not_parse_still_shows_its_tail(server):
    """Honest about being raw rather than pretending to a structure nobody has
    written yet."""
    base, root = server
    job = jobs.record(root, "preflight", ["python", "-m", "nightshift.preflight"])
    jobs.log_path(root, job.ident).write_text("""\
  [OK  ] lf-worktree
  [OK  ] gates
""", encoding="utf-8")

    _, text = _get(base, "run")
    assert 'class="joblog"' in text
    assert "[OK  ] gates" in text


def test_a_classify_pass_says_why_it_has_no_per_note_progress(server):
    """One dispatch over the whole lane is the entire economy of the step, so
    inventing per-note progress for it would report something nobody measured."""
    base, root = server
    job = jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])
    jobs.log_path(root, job.ident).write_text("""\
ingest: 13 note(s) in Board/inbox
  classifying with sonnet ...
""", encoding="utf-8")

    _, text = _get(base, "run")
    assert "classifying" in text
    assert "no per-note progress until it lands" in text


def test_a_finished_classify_shows_the_routes_it_produced(server):
    base, root = server
    job = jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])
    jobs.log_path(root, job.ident).write_text("""\
ingest: 13 note(s) in Board/inbox
  classifying with sonnet ...
  wrote Routing.md
    chore    3
    inline   6
    triage   4
""", encoding="utf-8")

    _, text = _get(base, "run")
    assert "3 chore" in text and "6 inline" in text
    assert "the Inbox page has each one" in text


def test_a_job_with_no_output_yet_gets_no_empty_box(server):
    """Asserted on the row's class, not on the word: `.roster tr.joblog` is in the
    stylesheet on every page, so the bare substring is always present."""
    base, root = server
    jobs.record(root, "ingest", ["python", "-m", "nightshift.ingest"])
    _, text = _get(base, "run")
    assert 'class="joblog"' not in text


def test_the_history_count_counts_jobs_not_table_rows(server):
    """A row is not a job — the inlined roster is rows too, and `len(rows)` had the
    header claiming four jobs over three of them."""
    base, root = server
    for name in ("one", "two"):
        job = jobs.record(root, name, ["python", "-c", "pass"])
        job.finished, job.exit_code = job.started, 0
        jobs.save(root, job)
        jobs.log_path(root, job.ident).write_text("some output\n", encoding="utf-8")

    _, text = _get(base, "run")
    head = text.split("Earlier from here", 1)[1][:200]
    assert re.search(r'class="count">2<', head), head


def test_the_port_is_exclusive_so_two_panels_cannot_both_serve_it():
    """`ThreadingHTTPServer` sets `allow_reuse_address`, which on Windows lets a
    second live socket bind a port another process is already listening on. Both
    then serve, connections land on either, and the page you read may come from a
    process holding code from before your last change.

    Measured 2026-08-17: three restarts appeared not to take, and the fix under
    test was reported as live while the page came from the previous build. The
    panel exists to say what is true, and a duplicate of it says what *was* true.
    """
    assert panel._Server.allow_reuse_address is False


def test_a_panel_already_serving_is_opened_rather_than_refused(tmp_path, capsys,
                                                              monkeypatch):
    """The ordinary case, and refusing it broke the launcher.

    Karel, 2026-08-17: "Running the bat opens and immediately closes the window and
    no browser page opens." Someone double-clicking the launcher while a panel is
    up wants that panel — an instant exit in a window with no pause is the worst
    possible answer.
    """
    root = _repo(tmp_path)
    opened = []
    monkeypatch.setattr(panel.webbrowser, "open", opened.append)
    held = ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
    panel.Handler.root = root
    thread = threading.Thread(target=held.serve_forever, daemon=True)
    thread.start()
    try:
        port = held.server_address[1]
        panel.serve(root, port, open_browser=True)          # returns; does not raise
        said = capsys.readouterr().out
        assert "already serving" in said
        assert "holds the code it started with" in said, "and says why to restart it"
        assert opened == [f"http://127.0.0.1:{port}/now"]
    finally:
        held.shutdown()
        thread.join(timeout=5)
        held.server_close()


def test_a_port_held_by_something_else_is_still_an_error(tmp_path, capsys):
    """The other reason, kept distinct: a socket that is not a Command Center has
    nothing to open, so there is nothing to do but say so."""
    root = _repo(tmp_path)
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    try:
        with pytest.raises(SystemExit) as exit_code:
            panel.serve(root, squatter.getsockname()[1], open_browser=False)
        assert exit_code.value.code == 2
        said = capsys.readouterr().out
        assert "not a panel" in said
        assert "--port" in said, "and the way out"
    finally:
        squatter.close()


def test_already_serving_says_no_when_nothing_is_there():
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    assert panel.already_serving(port) is False
