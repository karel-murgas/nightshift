"""Tests for `nightshift/tiers.py` — where the `tier: → model` binding is read from.

Written when `[tiers].binding_doc` landed (07_portability.md §8 step 4, second
pass). The module had carried its own note saying the fix belonged to step 4, and
what actually forced it was running the runner in a fresh synthetic repo: it
refused to start with `.claude/plans/ai_team/00_architecture.md is missing` — a
plan doc §7 says deliberately does not port. The framework was requiring a
document it declines to ship.

The invariant these tests defend is §16's, not the field's: **the binding lives in
exactly one document and a dispatcher never guesses a model.** Making the path
configurable must not weaken that, so most of what is below is about failing
loudly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nightshift import manifest
from nightshift import tiers

_BLOCK = """\
## 16. Tiers

```tier-binding
worker = sonnet
lead = opus
```
"""


def _repo(tmp_path: Path, *, doc: str | None = None, body: str = _BLOCK) -> Path:
    (tmp_path / ".ai").mkdir(exist_ok=True)
    config = '[project]\nname = "myapp"\n'
    if doc is not None:
        config += f'\n[tiers]\nbinding_doc = "{doc}"\n'
    (tmp_path / ".ai" / "manifest.toml").write_text(config, encoding="utf-8")
    if doc is not None:
        target = tmp_path / doc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_default_is_a_path_the_installing_repo_will_actually_have(tmp_path):
    """A project that declares nothing gets `docs/tier-binding.md` — the file
    `nightshift init` writes when no document already carries the block.

    It used to be `.claude/plans/ai_team/00_architecture.md`, a path that exists in
    exactly one repo on earth. That is the `deferral-note-nobody-collected`
    coupling: the *message* naming another project's plan doc was fixed at step 5
    and the *default value* naming it was not, so every consuming project's manifest
    table still pointed at a file it had never had."""
    _repo(tmp_path)
    assert tiers.binding_doc(tmp_path) == Path("docs/tier-binding.md")
    assert tiers.ARCHITECTURE == Path("docs/tier-binding.md")


def test_no_manifest_at_all_still_gives_the_default(tmp_path):
    """A caller with no config must get the constant this field replaced, not an
    exception — `binding()` is reached from the runner's refuse-to-start path."""
    assert tiers.binding_doc(tmp_path) == tiers.ARCHITECTURE


def test_a_project_may_point_the_binding_at_its_own_document(tmp_path):
    root = _repo(tmp_path, doc="docs/team.md")
    assert tiers.binding_doc(root) == Path("docs/team.md")
    assert tiers.binding(root) == {"worker": "sonnet", "lead": "opus"}
    assert tiers.resolve(root, "lead") == "opus"


def test_a_missing_document_names_the_key_to_set(tmp_path):
    """The whole reason the field exists. The old message named a file the reader
    had never heard of and gave them nothing to do about it."""
    _repo(tmp_path, doc="docs/team.md")
    (tmp_path / "docs" / "team.md").unlink()

    with pytest.raises(tiers.TierError) as caught:
        tiers.binding(tmp_path)
    message = str(caught.value)
    assert "docs/team.md" in message
    assert "[tiers].binding_doc" in message, "the error must say what to configure"
    assert "tier-binding" in message, "and what the document has to contain"


def test_a_document_without_the_block_is_refused(tmp_path):
    root = _repo(tmp_path, doc="docs/team.md", body="# Team\n\nWe use Opus for everything.\n")
    with pytest.raises(tiers.TierError, match="tier-binding"):
        tiers.binding(root)


def test_a_partial_block_is_refused_rather_than_half_used(tmp_path):
    """`card_schema` accepts both tiers, so a block binding one of them would let a
    card be written that can never dispatch."""
    root = _repo(tmp_path, doc="docs/team.md",
                 body="```tier-binding\nworker = sonnet\n```\n")
    with pytest.raises(tiers.TierError, match="lead"):
        tiers.binding(root)


def test_an_unbound_tier_names_the_document_it_was_looked_for_in(tmp_path):
    root = _repo(tmp_path, doc="docs/team.md")
    with pytest.raises(tiers.TierError) as caught:
        tiers.resolve(root, "architect")
    assert "docs/team.md" in str(caught.value)


def test_the_manifest_never_carries_the_models_themselves():
    """§16's rule, asserted against the schema rather than trusted: `[tiers]` may
    say *where* the binding is and nothing else. A `models` key here would be the
    second home the rule forbids, and the 2026-07-22 bug was a correct table
    nothing read."""
    assert manifest._KNOWN["tiers"] == ("binding_doc",)
