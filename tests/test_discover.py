"""Tests for `nightshift/discover.py` — the discovery half of `init` and `doctor`.

07_portability.md §8 step 5. The spec's design constraint is the rule from
`hosts.json`: **a check that discovers a fact may probe; a check that bounds
behaviour must be declared.** Most of what is asserted here is that second half —
the fields discovery must NOT answer, and the one it must never answer without
being asked.

Synthetic repos throughout, plus one test against `nightshift`'s own checkout,
which is a real Python project with a pyproject, a package and a test dir and is
therefore a free second subject. §8 step 5 names three; the third is Dungeoneer,
whose hand-written manifest discovery must reproduce, and that test lives there
because the manifest does.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift import discover

_NIGHTSHIFT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal but real Python project: one package, one test dir, one commit."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)],
                   check=True, capture_output=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Alex Rivera")
    (tmp_path / "myapp").mkdir()
    (tmp_path / "myapp" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


# --- the two that are never inferred -----------------------------------------


def test_capabilities_is_always_empty_and_says_why():
    """Not a limitation of the function — the rule itself.

    A probe answers "is the stack present?", and the honest response to "no" from
    an agent with initiative is to install it: 20 GB of weights onto a laptop at
    3 AM, which no gate would catch because nothing about it breaks a rule.
    """
    proposal = discover.capabilities()
    assert proposal.value == []
    assert proposal.confidence == discover.NEVER
    assert "never inferred" in proposal.why


def test_permission_mode_is_never_inferred_and_carries_the_warning():
    proposal = discover.permission_mode()
    assert proposal.confidence == discover.NEVER
    assert "NOT A SANDBOX" in proposal.why, (
        "the one item where not reading it has an unrecoverable cost must state that "
        "where it is proposed, not in a reference section")


def test_no_survey_ever_proposes_a_capability_or_a_permission_mode(repo):
    """The rule asserted at the level that matters: whatever else `survey` grows,
    these two must stay `NEVER`, because a future field added carelessly beside
    them is exactly how the rule would be lost."""
    never = {p.key for p in discover.survey(repo) if p.confidence == discover.NEVER}
    assert never == {"hosts.capabilities", "hosts.permission_mode"}


def test_the_budget_is_never_the_current_size(repo):
    """Doc 10 §4: a budget picked to fit whatever is there today is the move the
    gate exists to stop. This function is the one place that rule could be broken
    by a plausible-looking `sum(sizes)`, so it is asserted rather than trusted."""
    memory = repo / ".claude" / "memory"
    memory.mkdir(parents=True)
    (memory / "big.md").write_text("x" * 50_000, encoding="utf-8")

    proposal = discover.budget(repo)
    assert proposal.value is None
    assert "NOT to what the files happen to total today" in proposal.why


# --- the one that is always confirmed ----------------------------------------


def test_integration_is_confirm_even_when_a_branch_is_obviously_named(repo):
    """`dev` existing is not evidence. Dungeoneer is the case that proves it: `dev`
    is *stable* there and is a forbidden base, and no heuristic would guess that."""
    _git(repo, "branch", "dev")
    proposal = discover.integration_branch(repo, "main")
    assert proposal.value == "dev"
    assert proposal.confidence == discover.CONFIRM
    assert proposal.needs_confirmation
    assert "GUESS" in proposal.why


def test_integration_falls_back_to_the_branch_furthest_ahead(repo):
    (repo / "myapp" / "a.py").write_text("A = 1\n", encoding="utf-8")
    _git(repo, "checkout", "-q", "-b", "feature-work")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")

    proposal = discover.integration_branch(repo, "main")
    assert proposal.value == "feature-work"
    assert proposal.confidence == discover.CONFIRM


def test_integration_proposes_nothing_rather_than_the_stable_branch(repo):
    """With one branch and nothing ahead of it, the answer is "there may not be one
    yet" — proposing `main` would nominate a branch `forbidden_bases()` should be
    rejecting."""
    proposal = discover.integration_branch(repo, "main")
    assert proposal.value is None
    assert proposal.confidence == discover.CONFIRM


# --- the high-confidence fields ----------------------------------------------


def test_source_dirs_finds_the_package_and_skips_the_obvious_non_source(repo):
    for name in ("tests", "docs", "scripts"):
        (repo / name).mkdir(exist_ok=True)
        (repo / name / "__init__.py").write_text("", encoding="utf-8")
    assert discover.source_dirs(repo).value == ["myapp"]


def test_source_dirs_defers_to_pyproject_when_it_disagrees(repo):
    (repo / "vendored").mkdir()
    (repo / "vendored" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\n\n'
        '[tool.setuptools.packages.find]\ninclude = ["myapp*"]\n', encoding="utf-8")

    proposal = discover.source_dirs(repo)
    assert proposal.value == ["myapp"], "a stray __init__.py must not become a source dir"
    assert "pyproject" in proposal.why


def test_project_name_prefers_pyproject_to_the_directory(repo):
    assert discover.project_name(repo).value == repo.name
    (repo / "pyproject.toml").write_text('[project]\nname = "declared-name"\n',
                                         encoding="utf-8")
    assert discover.project_name(repo).value == "declared-name"


def test_project_name_survives_a_linked_worktree(repo, tmp_path_factory):
    """A worktree the runner cuts for a card is named after the card, not the
    project (`.dungeoneer-worktrees/<card-id>`) — a project with no
    `pyproject.toml` must not let that directory name win, or every `[worker]`
    field derived from it (fence_env, integration_checkout_dir, harvest_dirs)
    misreports itself the moment discovery runs inside one."""
    worktree = tmp_path_factory.mktemp("wt") / "some-card-slug"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "some-card-slug")
    assert discover.project_name(worktree).value == repo.name


def test_tests_dir_prefers_a_declared_testpaths(repo):
    assert discover.tests_dir(repo).value == "tests"
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["suite"]\n', encoding="utf-8")
    assert discover.tests_dir(repo).value == "suite"


def test_worker_fields_derive_from_the_project_name(repo):
    fields = {p.key: p.value for p in discover.worker_config(repo, "My App")}
    assert fields["worker.fence_env"] == "MY_APP_FENCE_ALLOW"
    assert fields["worker.integration_checkout_dir"] == ".my-app-integration"


def test_harvest_dirs_is_absent_rather_than_invented(repo):
    """Proposing a staging directory for a project that has no assets is how the
    art-card tests came to report "the worker produced nothing" — a confusing
    failure produced by config rather than by code."""
    assert discover.worker_config(repo, "myapp")[0].value is None
    (repo / "myapp" / "assets").mkdir()
    assert discover.worker_config(repo, "myapp")[0].value == ["myapp/assets/.tmp"]


def test_the_tier_binding_doc_is_found_not_defaulted(repo):
    assert discover.tier_binding_doc(repo).value is None
    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "team.md").write_text("```tier-binding\nworker = sonnet\n```\n",
                                  encoding="utf-8")
    assert discover.tier_binding_doc(repo).value == "docs/team.md"


def test_the_tier_binding_search_looks_inside_dot_claude(tmp_path):
    """`.claude/` is where this kind of document actually lives, and skipping every
    dot-directory made discovery miss Dungeoneer's on the first run."""
    (tmp_path / ".claude" / "plans").mkdir(parents=True)
    (tmp_path / ".claude" / "plans" / "arch.md").write_text(
        "```tier-binding\nworker = sonnet\n```\n", encoding="utf-8")
    assert discover.tier_binding_doc(tmp_path).value == ".claude/plans/arch.md"


# --- the three silent-failure checks §5 requires -----------------------------


def test_gitattributes_is_reported_missing_and_satisfied_when_present(repo):
    proposal = discover.gitattributes(repo)
    assert proposal.value == "* text=auto eol=lf"
    assert "autocrlf" in proposal.why

    (repo / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    assert discover.gitattributes(repo).value is None


def test_the_crlf_check_does_not_double_report_the_missing_attributes_file(repo):
    """Two distinct problems with one cause: `.gitattributes` is its own proposal,
    so the worktree check must not repeat it or the operator sees one issue twice
    and fixes neither."""
    proposal = discover.crlf_worktree(repo)
    files = proposal.value or []
    assert ".gitattributes" not in files


# --- a survey holds together -------------------------------------------------


def test_the_survey_is_internally_consistent(repo):
    """`source_dirs` feeds `layering`, `project.name` feeds `[worker]`, `stable`
    feeds `integration`. Threaded through one pass rather than each function
    re-deriving it, because `doctor` diffs this list against a written manifest and
    a self-contradicting survey reports drift that is not there."""
    (repo / "pyproject.toml").write_text('[project]\nname = "widget"\n', encoding="utf-8")
    by_key = {p.key: p.value for p in discover.survey(repo)}
    assert by_key["project.name"] == "widget"
    assert by_key["worker.fence_env"] == "WIDGET_FENCE_ALLOW"
    assert by_key["worker.integration_checkout_dir"] == ".widget-integration"


def test_as_manifest_tables_drops_what_discovery_could_not_find(repo):
    """A `None` written out as an empty value reads as a decision somebody made."""
    tables = discover.as_manifest_tables(discover.survey(repo))
    assert "harvest_dirs" not in tables.get("worker", {})
    assert "hosts" not in tables, "hosts.json fields are not manifest fields"


def test_a_directory_that_is_not_a_git_repo_still_surveys(tmp_path):
    """Discovery runs in a directory that is not set up yet — that is its whole
    job — so nothing here may raise on a missing `.git`."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    proposals = discover.survey(tmp_path)
    assert {p.key for p in proposals}, "survey produced nothing"
    assert next(p for p in proposals if p.key == "branches.stable").value is None


# --- the second subject: this repo -------------------------------------------


def test_discovery_reads_nightshift_itself_correctly():
    """§8 step 5 wants three subjects. This is the cheapest of them, and it is a
    real project rather than a fixture: a pyproject with a declared name, a package
    that is not called the same as the checkout directory, and a test dir."""
    by_key = {p.key: p.value for p in discover.survey(_NIGHTSHIFT)}
    assert by_key["project.name"] == "nightshift"
    assert by_key["project.source_dirs"] == ["nightshift"]
    assert by_key["tests.dir"] == "tests"
    assert by_key["tests.parallel"] is True
    assert by_key["worker.fence_env"] == "NIGHTSHIFT_FENCE_ALLOW"
    assert by_key["worker.harvest_dirs"] is None, "no assets dir here, so nothing to harvest"
