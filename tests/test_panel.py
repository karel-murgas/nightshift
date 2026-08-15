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

Real git repositories throughout, and a real `ThreadingHTTPServer` on a loopback
port for the route-level tests, matching the framework's own precedent
(`test_boardcmd.py`, `test_drain.py`) of exercising the real thing rather than a
mock of it.
"""
from __future__ import annotations

import json
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


def _card(root: Path, lane: str, card_id: str, *, unattended: str = "true") -> Path:
    path = root / "Board" / lane / f"{card_id}.md"
    path.write_text(CARD.format(id=card_id, state=lane, unattended=unattended),
                    encoding="utf-8", newline="")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"card {card_id}")
    return path


@pytest.fixture(autouse=True)
def _reset_account():
    """`_ACCOUNT` is process-wide, in-memory state (by design — see the module
    docstring). A test that selected one must not leak it into the next."""
    panel._ACCOUNT = panel.AccountState()
    yield
    panel._ACCOUNT = panel.AccountState()


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
