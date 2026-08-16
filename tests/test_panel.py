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
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nightshift import board, panel

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
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    pid = panel.spawn_background("runner", ["--card", "x"], root)
    assert pid == 4242
    assert captured["argv"] == [sys.executable, "-m", "nightshift.runner", "--card", "x"]
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == "/does/not/exist/main"


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
