"""`nightshift update` — the three-way question, and the one answer it must never give.

The module exists because `init` never overwrites, so every project runs the templates
it was installed with until somebody notices. The hard part is not finding the drift; it
is that a file differing from today's template is one of two opposite things — one the
operator edited, or one whose template moved — and only the receipt's recorded hash can
separate them.

So most of what is asserted below is a *refusal*: a file you edited is not touched, a
conflict is not resolved, a frozen record is not rewritten, and nothing at all happens
without `--apply`. The one thing that must be positively true is that a file you never
touched picks up its template. Everything else is this module keeping its hands off.

The CRLF test is not a Windows nicety. `textio.write_text_lf` writes LF and a checkout
with `* text=auto eol=lf` hands the same bytes back as CRLF, so a hash taken over raw
bytes reports every file on this machine as locally edited — and `update` would then
report nothing but conflicts, forever, on the platform it was written on.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nightshift import init, update

import _fixtures


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True, encoding="utf-8", errors="replace")


def _build(root: Path) -> None:
    """The 467 ms this file used to pay 23 times. `init.apply` is 247 ms of it."""
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    _fixtures.git_init(root, branch="main", autocrlf="false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    init.apply(init.build_plan(root, integration="main"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh project with nightshift installed and nothing drifted yet.

    Built once per process and copied — the installed tree names no absolute
    path (checked: none of its 68 files mentions the directory it was built in),
    so a copy is indistinguishable from a rebuild.
    """
    return _fixtures.repo_copy("installed-main", tmp_path / "proj", _build)


#: A file `init` writes verbatim from a template, so a test can move "the template"
#: by moving what `stage_templates` would produce. Chosen because nothing else in the
#: tree reads it, so drifting it cannot break another assertion.
TRACKED = ".claude/agents/stale-hunter.md"


def _move_template(monkeypatch, rel: str, suffix: str = "\n## moved upstream\n") -> None:
    """Make `stage_templates` produce a different body for `rel`, without touching disk.

    Patching the renderer rather than editing a file in the installed package: a test
    that writes into `nightshift/templates/` mutates the checkout it is running from,
    and a failure between the write and the restore leaves the repo dirty.
    """
    real = init.stage_templates

    def moved(plan, root, tables, **kw):
        real(plan, root, tables, **kw)
        if rel in plan.staged:
            plan.staged[rel] = plan.staged[rel] + suffix
    monkeypatch.setattr(init, "stage_templates", moved)


def _edit(repo: Path, rel: str, suffix: str = "\n## my own note\n") -> str:
    path = repo / rel
    body = path.read_text(encoding="utf-8") + suffix
    path.write_text(body, encoding="utf-8")
    return body


# --- the three-way classification ---------------------------------------------


def test_a_fresh_install_has_nothing_to_do(repo):
    """The baseline that makes every other verdict meaningful. If a just-installed
    repo reports work, the whole report is noise and nobody will read the real one."""
    found = update.survey(repo)

    assert found.changes == 0, [f.rel for f in found.findings if f.actionable]
    assert not found.by(update.CONFLICT)
    assert not found.by(update.YOURS)


def test_a_file_you_never_touched_takes_the_new_template(repo, monkeypatch):
    _move_template(monkeypatch, TRACKED)

    found = update.survey(repo)
    assert [f.rel for f in found.by(update.STALE)] == [TRACKED]

    update.apply(found)
    assert (repo / TRACKED).read_text(encoding="utf-8").endswith("## moved upstream\n")


def test_a_file_you_edited_is_left_alone_when_the_template_has_not_moved(repo):
    mine = _edit(repo, TRACKED)

    found = update.survey(repo)
    assert [f.rel for f in found.by(update.YOURS)] == [TRACKED]

    update.apply(found)
    assert (repo / TRACKED).read_text(encoding="utf-8") == mine


def test_a_file_you_edited_whose_template_moved_is_a_conflict_and_is_never_written(
        repo, monkeypatch):
    """The case the module exists for. Both sides changed, so neither may win by
    default — `--apply` must walk straight past it."""
    _move_template(monkeypatch, TRACKED)
    mine = _edit(repo, TRACKED)

    found = update.survey(repo)
    assert [f.rel for f in found.by(update.CONFLICT)] == [TRACKED]
    assert not [f for f in found.findings if f.rel == TRACKED and f.actionable]

    update.apply(found)
    assert (repo / TRACKED).read_text(encoding="utf-8") == mine


def test_a_file_of_ours_deleted_from_disk_is_written_back(repo):
    (repo / TRACKED).unlink()

    found = update.survey(repo)
    assert [f.rel for f in found.by(update.MISSING)] == [TRACKED]

    update.apply(found)
    assert (repo / TRACKED).is_file()


def test_crlf_on_disk_is_not_mistaken_for_a_local_edit(repo):
    """A Windows checkout hands LF content back as CRLF. Hashing raw bytes would call
    every file here locally modified, and every answer this module gives would be
    `conflict` — on the platform it was written on."""
    path = repo / TRACKED
    path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))

    found = update.survey(repo)

    assert [f.rel for f in found.by(update.YOURS, update.CONFLICT)] == []
    assert TRACKED in [f.rel for f in found.by(update.CURRENT)]


# --- the frozen three ----------------------------------------------------------


@pytest.mark.parametrize("rel", update.FROZEN)
def test_a_record_is_never_a_candidate_however_far_it_differs(repo, rel):
    """The manifest, the host config and the corrections log are records of decisions
    and of what went wrong — not copies of a template. The corrections log especially:
    it is the one artefact in the tree that cannot be regenerated."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# nothing like the template\n", encoding="utf-8")

    found = update.survey(repo)

    assert [f.verdict for f in found.findings if f.rel == rel] == [update.FROZEN_V]
    update.apply(found)
    assert path.read_text(encoding="utf-8") == "# nothing like the template\n"


# --- resolving a conflict ------------------------------------------------------


def test_keep_silences_this_version_and_only_this_version(repo, monkeypatch):
    """`--keep` records the *template's* hash, so the question reopens exactly when
    there is something new to decide, and not before."""
    _move_template(monkeypatch, TRACKED)
    _edit(repo, TRACKED)

    found = update.survey(repo)
    update.keep(found, update.find(found, TRACKED))

    assert [f.rel for f in update.survey(repo).by(update.DECLINED)] == [TRACKED]
    assert not update.survey(repo).by(update.CONFLICT)

    # ... and it comes back the moment the template moves again.
    _move_template(monkeypatch, TRACKED, "\n## moved a second time\n")
    assert [f.rel for f in update.survey(repo).by(update.CONFLICT)] == [TRACKED]


def test_take_overwrites_but_keeps_what_it_replaced(repo, monkeypatch):
    _move_template(monkeypatch, TRACKED)
    mine = _edit(repo, TRACKED)

    found = update.survey(repo)
    update.take(found, update.find(found, TRACKED))

    assert (repo / TRACKED).read_text(encoding="utf-8").endswith("## moved upstream\n")
    backup = repo / (TRACKED + update.BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8") == mine, "the overwrite was not recoverable"
    assert not update.survey(repo).by(update.CONFLICT), "taking it did not settle it"


def test_an_unknown_path_is_refused_with_the_list_of_real_ones(repo):
    found = update.survey(repo)
    with pytest.raises(update.UpdateError) as caught:
        update.find(found, "pkg/core.py")
    assert "not a file nightshift manages" in str(caught.value)


# --- the receipt ---------------------------------------------------------------


def test_version_one_is_refused_rather_than_guessed_at(repo):
    """v1 recorded which files were written but not what was written into them, so
    `you edited this` and `the template moved` are indistinguishable under it. Guessing
    would mean overwriting somebody's work on a coin flip."""
    path = repo / init.RECEIPT
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["version"] = 1
    receipt["created"] = sorted(receipt["created"])
    path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(update.UpdateError) as caught:
        update.survey(repo)
    assert "version" in str(caught.value)


def test_no_receipt_at_all_is_refused(tmp_path):
    root = tmp_path / "bare"
    (root / ".ai").mkdir(parents=True)
    (root / ".ai" / "manifest.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")

    with pytest.raises(update.UpdateError) as caught:
        update.survey(root)
    assert "install.json" in str(caught.value)


def test_applying_records_what_it_wrote_so_the_next_run_is_quiet(repo, monkeypatch):
    """Without this the same file reports `stale` forever: the receipt would still
    hold the hash from the original install."""
    _move_template(monkeypatch, TRACKED)
    update.apply(update.survey(repo))

    assert not update.survey(repo).by(update.STALE)
    recorded = init.receipt_created(init.read_receipt(repo))
    assert recorded[TRACKED] == init.content_hash(
        (repo / TRACKED).read_text(encoding="utf-8"))


# --- the hook merge ------------------------------------------------------------


def test_a_hook_whose_module_was_renamed_is_taken_out_not_left_beside_the_new_one(repo):
    """`init` can only ever add, so a hook pointing at a module that no longer exists
    sits in settings.json forever, firing nothing. Strip-then-merge is the only pass
    that converges."""
    path = repo / init.SETTINGS
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["hooks"].setdefault("PostToolUse", []).append(
        {"matcher": "Write", "hooks": [
            {"type": "command", "command": "python -m nightshift.gone_away"}]})
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    found = update.survey(repo)
    assert found.settings, "the dead hook was not noticed"
    update.apply(found)

    assert "gone_away" not in path.read_text(encoding="utf-8")


def test_a_projects_own_hooks_survive_the_merge(repo):
    path = repo / init.SETTINGS
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["hooks"].setdefault("PostToolUse", []).append(
        {"matcher": "Write", "hooks": [
            {"type": "command", "command": "make lint"}]})
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    found = update.survey(repo)
    if found.settings:
        update.apply(found)

    assert "make lint" in path.read_text(encoding="utf-8")


# --- the default ---------------------------------------------------------------


def test_reporting_writes_absolutely_nothing(repo, monkeypatch):
    """`update` with no flags is what you type to find out what it does. Same shape as
    `uninstall`, and for the same reason."""
    _move_template(monkeypatch, TRACKED)
    (repo / TRACKED).unlink()
    before = {p: p.read_bytes() for p in repo.rglob("*")
              if p.is_file() and ".git" not in p.parts}

    assert update.main(["--root", str(repo)]) == 0

    after = {p: p.read_bytes() for p in repo.rglob("*")
             if p.is_file() and ".git" not in p.parts}
    assert after == before


def test_the_launchers_are_installed_and_tracked(repo):
    """The Command Center had no way into it that was not a module path, and nothing
    in any document said it existed."""
    recorded = init.receipt_created(init.read_receipt(repo))
    for name in init.LAUNCHERS:
        assert (repo / name).is_file(), f"{name} was not written"
        assert name in recorded, f"{name} is not in the receipt, so update cannot see it"


def test_the_hook_report_counts_the_delta_not_the_size_of_each_half(repo):
    """Stripping takes out all of our hooks and merging puts today's back, so the
    counts those two return are half-sizes, not changes. Reporting them would say
    "11 to add" about a file that needed one entry removed."""
    path = repo / init.SETTINGS
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["hooks"].setdefault("PostToolUse", []).append(
        {"matcher": "Write", "hooks": [
            {"type": "command", "command": "python -m nightshift.gone_away"}]})
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    found = update.survey(repo)

    assert found.settings_removed == 1
    assert found.settings_added == 0, "hooks already present were counted as additions"


def test_a_merge_refuses_before_it_spends_anything(repo, monkeypatch):
    """`permission_mode: default` cannot edit files, so the agent would read both
    versions and write neither — a whole session bought to discover that. Refused up
    front, and nothing of the attempt is left on disk."""
    _move_template(monkeypatch, TRACKED)
    mine = _edit(repo, TRACKED)
    found = update.survey(repo)

    with pytest.raises(update.UpdateError) as caught:
        update.merge(found, update.find(found, TRACKED), permission_mode="default")

    assert "cannot edit files" in str(caught.value)
    assert not (repo / (TRACKED + ".nightshift-new")).exists(), "scratch file left behind"
    assert (repo / TRACKED).read_text(encoding="utf-8") == mine


def test_a_projects_own_hook_through_project_script_is_not_stripped(repo):
    """Found by running the updater against the first real repo it ever saw.

    `nightshift.hooks.project_script` exists precisely so a project can reach its OWN
    script from any working directory. It is in no template, so "strip every nightshift
    hook, then merge today's template back" — which converges, and which was the first
    implementation — deletes it. The live repo had exactly that hook, pointing at a
    script that exists, and the updater offered to remove it.
    """
    path = repo / init.SETTINGS
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["hooks"].setdefault("PostToolUse", []).append(
        {"matcher": "Write|Edit", "hooks": [
            {"type": "command",
             "command": "python -m nightshift.hooks.project_script .ai/recipes/hint.py; exit 0"}]})
    path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    found = update.survey(repo)
    if found.settings:
        update.apply(found)

    assert "project_script" in path.read_text(encoding="utf-8"), (
        "a live hook the project added was removed as though it were dead")


def test_deadness_is_checked_not_inferred_from_absence_in_the_template():
    """The distinction the fix turns on: a module that no longer resolves is dead; one
    that resolves is live, whoever put the hook there and whether or not we ship it."""
    live = {"command": "python -m nightshift.hooks.project_script .ai/recipes/hint.py"}
    gone = {"command": "python -m nightshift.hooks.no_such_module_here"}
    unrelated = {"command": "make lint"}

    assert not update.dead_hook(live)
    assert update.dead_hook(gone)
    assert not update.dead_hook(unrelated), "a non-nightshift command is not ours to judge"
