"""`nightshift.hooks.pre` / `.post` — every fence in one process per tool call — and
the wiring that replaces the per-fence settings entries."""
from __future__ import annotations

import importlib
import io
import json
import sys
import types

import pytest

from nightshift import init, update
from nightshift.hooks import post, pre, tool_economy


def test_every_rule_names_a_module_with_a_check():
    for name, tools in pre.RULES:
        module = importlib.import_module(f"nightshift.hooks.{name}")
        assert callable(module.check), name
        assert tools, name


def _fake_rules(monkeypatch, verdicts: dict[str, object]) -> None:
    """Replace the fences with stubs: a string denies, None allows, an exception raises."""
    rules = []
    for name, verdict in verdicts.items():
        module = types.ModuleType(f"nightshift.hooks._fake_{name}")

        def check(payload, verdict=verdict):
            if isinstance(verdict, Exception):
                raise verdict
            return verdict
        module.check = check
        monkeypatch.setitem(sys.modules, f"nightshift.hooks._fake_{name}", module)
        rules.append((f"_fake_{name}", frozenset({"Bash"})))
    monkeypatch.setattr(pre, "RULES", tuple(rules))


def test_all_denials_are_reported_together(monkeypatch):
    _fake_rules(monkeypatch, {"a": "no A", "b": None, "c": "no C"})
    assert pre.denials({"tool_name": "Bash"}) == ["no A", "no C"]


def test_a_fence_that_raises_is_skipped_not_fatal(monkeypatch, capsys):
    _fake_rules(monkeypatch, {"a": RuntimeError("boom"), "b": "no B"})
    assert pre.denials({"tool_name": "Bash"}) == ["no B"]
    assert "boom" in capsys.readouterr().err


def test_a_fence_only_sees_the_tools_it_judges(monkeypatch):
    _fake_rules(monkeypatch, {"a": "no A"})
    assert pre.denials({"tool_name": "Read"}) == []


def test_main_emits_one_deny_decision(monkeypatch, capsys):
    _fake_rules(monkeypatch, {"a": "no A", "b": "no B"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    assert pre.main() == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == "no A\n\nno B"


def test_main_says_nothing_on_an_allowed_call(monkeypatch, capsys):
    _fake_rules(monkeypatch, {"a": None})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
    pre.main()
    assert capsys.readouterr().out == ""


def test_the_real_fences_deny_through_the_dispatcher(monkeypatch):
    """End to end on a real rule: the slow-command guard, which needs no repo."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "python -m nightshift.preflight"}}
    assert any("[tool_economy]" in reason for reason in pre.denials(payload))


# --- post ---------------------------------------------------------------------


def test_post_runs_the_gates_then_the_project_scripts_with_the_payload(tmp_path, monkeypatch,
                                                                         capsys):
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text('[project]\nname = "p"\n',
                                                     encoding="utf-8")
    script = tmp_path / "hint.py"
    script.write_text("import json, sys\nprint('hint saw', json.load(sys.stdin)['tool_name'])\n"
                      "sys.exit(3)\n", encoding="utf-8")
    monkeypatch.setattr(post.gates_on_edit, "run", lambda root, session: "gates: ok")
    payload = json.dumps({"tool_name": "Edit", "cwd": str(tmp_path)})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert post.main(["hint.py"]) == 0
    out = capsys.readouterr().out
    assert out.index("gates: ok") < out.index("hint saw Edit")


def test_post_reports_a_missing_script_and_still_exits_0(tmp_path, monkeypatch, capsys):
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "manifest.toml").write_text('[project]\nname = "p"\n',
                                                     encoding="utf-8")
    monkeypatch.setattr(post.gates_on_edit, "run", lambda root, session: "gates: ok")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    assert post.main(["nope.py"]) == 0
    assert "does not exist" in capsys.readouterr().out


# --- the wiring ------------------------------------------------------------------


def _template() -> dict:
    fragment = json.loads((init.TEMPLATES / "settings.hooks.json").read_text(encoding="utf-8"))
    fragment.pop("_comment", None)
    return fragment


def test_the_template_wires_one_dispatcher_per_event_and_no_superseded_fence():
    commands = [hook["command"] for groups in _template()["hooks"].values()
                for group in groups for hook in group["hooks"]]
    assert commands.count("python -m nightshift.hooks.pre") == 1
    assert commands.count("python -m nightshift.hooks.post") == 1
    for module in pre.SUPERSEDED | post.SUPERSEDED:
        assert not any(c.endswith(module) for c in commands), module


def test_a_superseded_fence_entry_is_dead_to_update():
    for module in pre.SUPERSEDED | post.SUPERSEDED:
        assert update.dead_hook({"command": f"python -m {module}"}), module
    assert not update.dead_hook({"command": "python -m nightshift.hooks.pre"})
    assert not update.dead_hook({"command": "python -m nightshift.hooks.preflight_guard"})


def test_a_project_may_pass_arguments_to_a_dispatcher_without_a_duplicate_being_merged():
    existing = {"hooks": {"PostToolUse": [{"matcher": "Write|Edit", "hooks": [
        {"type": "command", "command": "python -m nightshift.hooks.post .ai/recipes/hint.py"}]}]}}
    merged, _ = init.merge_hooks(existing, _template())
    posts = [hook["command"] for group in merged["hooks"]["PostToolUse"]
             for hook in group["hooks"] if "hooks.post" in hook["command"]]
    assert posts == ["python -m nightshift.hooks.post .ai/recipes/hint.py"]


# --- tool_economy reads the program, not the text (slow-command-guard-matched-a-grep-pattern)


@pytest.mark.parametrize("command", [
    'grep -E "FAIL|preflight |receipt" .ai/runs/_preflight/pytest.txt',
    'grep -E "FAIL|python -m nightshift.preflight|x" f.txt',
    'until grep -q "receipt" out.txt; do sleep 10; done',
    "grep -c pytest tests/",
    'git commit -m "run python -m nightshift.preflight first"',
    "cat > probe.py <<'EOF'\npython -m nightshift.preflight\npytest -q\nEOF",
    "python probe.py -m nightshift.preflight",
    "echo python -m pytest",
])
def test_a_command_that_only_mentions_a_slow_one_is_allowed(command):
    assert tool_economy._slow_verdict(command, run_in_background=False) is None


@pytest.mark.parametrize("command", [
    "python -m nightshift.preflight",
    "py -X utf8 -m nightshift.runner --dry-run",
    "cd x && python -m pytest -q",
    "FOO=1 pytest",
    "grep x f.txt\npython -m nightshift.preflight --full-tests",
    "until pytest; do sleep 1; done",
    "python -m pytest -q > out.txt 2>&1",
])
def test_a_slow_command_run_in_the_foreground_is_still_denied(command):
    assert tool_economy._slow_verdict(command, run_in_background=False)


def test_a_worker_reading_a_file_through_a_pipe_is_allowed_and_directly_is_not():
    assert tool_economy._verdict("git log | head -20") is None
    assert tool_economy._verdict("head -20 src/x.py")
    assert tool_economy._verdict('grep -n "a|b" src/x.py')
    assert tool_economy._verdict('echo "a | head -3 x.py"') is None


def test_the_shared_heredoc_stripper_is_the_preflight_guards_too():
    from nightshift.hooks import preflight_guard, shellwords
    assert preflight_guard._strip_heredocs is shellwords.strip_heredocs
