"""`nightshift.branches` — the derivation that used to be `.ai/branches.py`.

These tests travelled with the module (07_portability.md §8 step 4). The pair
that matters is the migration pair: the invariant *and* the swap. The bug this
module exists for was latent precisely because nothing exercised the
post-swap configuration, so asserting only "the integration branch is not
forbidden" against one manifest would reproduce the original blind spot.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nightshift import branches
from nightshift.manifest import ManifestError


def _repo(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai" / "manifest.toml").write_text(body, encoding="utf-8")
    return tmp_path


_DUNGEONEER = """
[branches]
integration = "development_team"
stable = "main"
forbidden_extra = ["dev", "master"]
"""

# The same project after the migration Karel described on 2026-07-23: the
# quarantine branch is retired and `dev` resumes the integration role.
_AFTER_SWAP = """
[branches]
integration = "dev"
stable = "main"
forbidden_extra = ["master"]
"""


def test_the_integration_branch_is_never_forbidden(tmp_path):
    root = _repo(tmp_path, _DUNGEONEER)
    assert branches.integration(root) == "development_team"
    assert branches.integration(root) not in branches.forbidden_bases(root)


def test_stable_and_the_extras_are_forbidden(tmp_path):
    root = _repo(tmp_path, _DUNGEONEER)
    assert branches.forbidden_bases(root) == {"main", "dev", "master"}
    assert branches.is_forbidden_base("dev", root)
    assert not branches.is_forbidden_base("development_team", root)


def test_swapping_the_integration_branch_moves_dev_out_of_the_forbidden_set(tmp_path):
    """The migration itself. `dev` is forbidden right up to the moment it holds
    the role, and the branch it replaces is not special-cased on the way out."""
    root = _repo(tmp_path, _AFTER_SWAP)
    assert "dev" not in branches.forbidden_bases(root)
    # ...and it becomes the first branch a gate diffs against.
    assert branches.merge_base_candidates(root)[0] == "dev"
    # main stays protected across the swap; that fact is not migration-dependent.
    assert "main" in branches.forbidden_bases(root)


def test_an_undeclared_integration_branch_raises_naming_the_key(tmp_path):
    """`manifest.py`'s third shape: a field that bounds behaviour is never
    guessed. The message has to name the key, because the caller that hits this
    is a runner about to pick a base branch."""
    root = _repo(tmp_path, "[branches]\nstable = \"main\"\n")
    with pytest.raises(ManifestError, match="branches.integration"):
        branches.integration(root)
    with pytest.raises(ManifestError, match="branches.integration"):
        branches.forbidden_bases(root)


def test_merge_base_candidates_degrade_instead_of_raising(tmp_path):
    """The one function that tolerates an undeclared integration branch, because
    its callers are gates asking "what changed on this branch". A gate that
    raised in a repo mid-setup would take the whole suite down; one that returns
    the stable branch alone still sees a real diff."""
    root = _repo(tmp_path, "[branches]\nstable = \"trunk\"\n")
    assert branches.merge_base_candidates(root) == ("trunk",)


def test_no_manifest_at_all_still_yields_a_diff_target(tmp_path):
    """Not the same case as above: here `load` itself fails. The fallback is the
    schema default rather than a second hardcoded "main" — one home for the
    fact, even when the fact is a default."""
    (tmp_path / ".git").mkdir()
    assert branches.merge_base_candidates(tmp_path) == ("main",)


def test_a_manifest_can_be_passed_instead_of_a_root(tmp_path):
    """`preflight` and `merge_check` have already loaded one and must not
    re-read the file between two questions about the same run."""
    from nightshift import manifest as manifest_mod

    root = _repo(tmp_path, _DUNGEONEER)
    loaded = manifest_mod.load(root)
    assert branches.integration(loaded) == "development_team"
    assert branches.forbidden_bases(loaded) == {"main", "dev", "master"}
