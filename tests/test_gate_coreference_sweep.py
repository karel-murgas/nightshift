"""Tests for the `coreference_sweep` core gate.

The gate replaces a reviewer finding, so the cases that matter are the ones the
reviewer actually returned. Both real 2026-08-25 defects are replayed verbatim:
a vault curve rewritten in one memory file and left standing in another, and a
constant deleted from the test suite while a recipe still taught it.

The rest of the file is about the other half of a gate's job — not firing. A
gate that reports a card's own honest edits gets appealed into uselessness, so
every near-miss here is a thing the corpus really does: a token moved within one
file, a dated history doc whose job is to name dead values, a section marked
`stale-ok`, and a deleted file (whose every token is "removed" and which belongs
to `deletion_sweep`).

Real git repos throughout — the gate's whole input is `git diff` against a merge
base, so a fixture that only wrote files to disk would exercise nothing. Cut
from `_fixtures.git_init`'s cached skeleton rather than `git init`-ed per test,
per the `fixture_rebuild` rule (135 ms against 18 ms, and this file wants ten).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import nightshift.gates as _nightshift_gates

_GATES = Path(_nightshift_gates.__file__).resolve().parent
if str(_GATES) not in sys.path:
    sys.path.insert(0, str(_GATES))

import coreference_sweep  # noqa: E402
import doc_scan  # noqa: E402

import _fixtures  # noqa: E402

CURVE_OLD = "77/251/500/815/1192"
CURVE_NEW = "120/295/500/727/971"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A repo on `main` carrying `files`, ready for a card branch."""
    root = _fixtures.git_init(tmp_path / "repo", branch="main")
    (root / ".ai").mkdir(exist_ok=True)
    (root / ".ai" / "manifest.toml").write_text(
        '[project]\nname = "demo"\nsource_dirs = ["demo"]\n'
        'doc_files = ["NOTES.md", "DESIGN.md", "HISTORY.md"]\n\n'
        '[branches]\nstable = "main"\n', encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    doc_scan.clear_caches()
    return root


def _card(root: Path, changes: dict[str, str | None]) -> None:
    """A card branch that applies `changes` — `None` deletes the file."""
    _git(root, "switch", "-qc", "ai/card")
    for rel, text in changes.items():
        path = root / rel
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "the card")
    doc_scan.clear_caches()


# --- the two findings this gate was built from -------------------------------

def test_a_numeric_series_left_standing_in_another_doc_is_caught(tmp_path):
    """`economy-early-late-balance`, 2026-08-25. The diff lowered
    `CONTRACT_VAULT_EXP` and rewrote the curve in `design_detail.md`, leaving
    `arch_detail.md:142` describing a curve the code no longer produces. The
    reviewer found it, returned `needs_fix`, and the card lost an attempt to a
    one-substring correction."""
    root = _repo(tmp_path, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.7\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_OLD}.\n",
        "NOTES.md": f"# Notes\n\n`vault_reward` yields {CURVE_OLD} today.\n",
    })
    _card(root, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.3\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_NEW}.\n",
    })

    violations = coreference_sweep.check(root)

    assert [v.file for v in violations] == ["NOTES.md"]
    assert CURVE_OLD in violations[0].rule
    assert "DESIGN.md" in violations[0].rule      # names where it changed


def test_a_symbol_the_diff_deleted_but_a_doc_still_teaches_is_caught(tmp_path):
    """`catalog-registry-and-guards`, 2026-08-25 — the card that spent all three
    attempts on this class and was then filed to `needs-decision/` as though the
    fix kept recurring. `doc_reference_liveness` was satisfied because the token
    survived inside comments; the question that finds it is not "does this symbol
    resolve" but "did this diff just stop defining it"."""
    root = _repo(tmp_path, {
        "demo/roster.py": "EXPECTED_IDS = (1, 2)\nOTHER = 3\n",
        "NOTES.md": "# Notes\n\nThe roster is pinned by EXPECTED_IDS.\n",
    })
    _card(root, {"demo/roster.py": "OTHER = 3\n"})

    violations = coreference_sweep.check(root)

    assert [(v.file, v.line) for v in violations] == [("NOTES.md", 3)]
    assert "EXPECTED_IDS" in violations[0].rule


# --- and the ones it must stay quiet about -----------------------------------

def test_a_token_moved_within_one_file_is_not_a_survivor(tmp_path):
    """The commonest honest edit there is. A doc that rewrites the paragraph
    around a value has not stopped saying it, and reporting that would fire on
    most cards — which is how a gate gets appealed into uselessness.

    A *second* file has to legitimately carry the value too, or this passes for
    the wrong reason: with no survivor anywhere there is nothing to report
    whether or not the diff is read correctly. Measured — the first version of
    this test passed with `removed - added` mutated to `removed`.
    """
    root = _repo(tmp_path, {
        "demo/contracts.py": f"CURVE = '{CURVE_OLD}'\n",
        "DESIGN.md": f"# Design\n\nOld sentence about {CURVE_OLD}.\n",
        "NOTES.md": f"# Notes\n\nThe curve is {CURVE_OLD}, as DESIGN says.\n",
    })
    _card(root, {"DESIGN.md": f"# Design\n\nRewritten, still {CURVE_OLD}.\n"})

    assert coreference_sweep.check(root) == []


def test_the_file_that_made_the_change_may_still_mention_the_old_value(tmp_path):
    """"Was X, now Y" is the clearest way to write a change, and the file that
    made it is the one place a reader has the context to read it.

    The old value has to sit on a line the diff did *not* touch, or the token
    never reaches `gone` in the first place and this asserts nothing: a line
    carrying both values is an add and a remove of the same token, which cancel.
    So the note below is pre-existing and untouched, and only the sentence above
    it changes — which is how these files are really edited.
    """
    root = _repo(tmp_path, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.7\n",
        "DESIGN.md": (f"# Design\n\nThe curve is {CURVE_OLD}.\n\n"
                      f"> Historical note: the launch curve was {CURVE_OLD}.\n"),
    })
    _card(root, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.3\n",
        "DESIGN.md": (f"# Design\n\nThe curve is {CURVE_NEW}.\n\n"
                      f"> Historical note: the launch curve was {CURVE_OLD}.\n"),
    })

    # `CURVE_OLD` did stop being said on DESIGN.md's changed line...
    assert coreference_sweep.replaced_tokens(root).get(CURVE_OLD) == "DESIGN.md"
    # ...and the surviving mention is in that same file, so it is not reported.
    assert coreference_sweep.check(root) == []


def test_a_history_doc_may_name_a_value_that_is_no_longer_true(tmp_path):
    """`doc_scope: history` exists because dated narrative's *job* is to record
    what used to be so. The whole doc-staleness family honours it and this one
    must too, or every retune fails on the changelog."""
    root = _repo(tmp_path, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.7\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_OLD}.\n",
        "HISTORY.md": f"---\ndoc_scope: history\n---\n\n2026-01: it was {CURVE_OLD}.\n",
    })
    _card(root, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.3\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_NEW}.\n",
    })

    assert coreference_sweep.check(root) == []


def test_a_stale_ok_section_is_excused_with_its_reason_on_the_record(tmp_path):
    """The per-section escape hatch the rest of the family uses. Deliberately not
    a silent one: `gate_appeals` reads these and counts the debt."""
    root = _repo(tmp_path, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.7\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_OLD}.\n",
        "NOTES.md": ("# Notes\n\n<!-- stale-ok: quoting the pre-2026 curve on "
                     f"purpose -->\n## Old model\n\nIt yielded {CURVE_OLD}.\n"),
    })
    _card(root, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.3\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_NEW}.\n",
    })

    assert coreference_sweep.check(root) == []


def test_a_deleted_file_is_deletion_sweeps_subject_not_this_ones(tmp_path):
    """Every token in a removed file is "removed", so treating deletion as
    replacement would report the whole file's vocabulary. `deletion_sweep` already
    watches files and top-level class/def; this one watches literals inside
    surviving files."""
    root = _repo(tmp_path, {
        "demo/roster.py": "EXPECTED_IDS = (1, 2)\n",
        "demo/old.py": f"LEGACY_CURVE = '{CURVE_OLD}'\n",
        "NOTES.md": f"# Notes\n\nSee LEGACY_CURVE, {CURVE_OLD}.\n",
    })
    _card(root, {"demo/old.py": None})

    assert coreference_sweep.check(root) == []


def test_nothing_is_reported_when_the_branch_has_no_diff(tmp_path):
    """On the integration branch with a clean tree the merge base is HEAD, so
    there is nothing this gate can have an opinion about — and it must say so by
    being silent rather than by comparing against nothing."""
    root = _repo(tmp_path, {
        "demo/roster.py": "EXPECTED_IDS = (1, 2)\n",
        "NOTES.md": "# Notes\n\nEXPECTED_IDS pins the roster.\n",
    })

    assert coreference_sweep.replaced_tokens(root) == {}
    assert coreference_sweep.check(root) == []


def test_an_uncommitted_edit_counts_the_same_as_a_committed_one(tmp_path):
    """The on-save hook and the runner's post-attempt check want the same answer,
    so the diff is taken against the merge base with the working tree included."""
    root = _repo(tmp_path, {
        "demo/roster.py": "EXPECTED_IDS = (1, 2)\nOTHER = 3\n",
        "NOTES.md": "# Notes\n\nThe roster is pinned by EXPECTED_IDS.\n",
    })
    _git(root, "switch", "-qc", "ai/card")
    (root / "demo" / "roster.py").write_text("OTHER = 3\n", encoding="utf-8")
    doc_scan.clear_caches()

    assert [v.file for v in coreference_sweep.check(root)] == ["NOTES.md"]


# --- the token shapes are narrow on purpose ----------------------------------

def test_short_or_undivided_tokens_are_not_treated_as_distinctive(tmp_path):
    """A two-number pair is a resolution, a date or a fraction; a bare integer is
    a line number, a year or a test count. Both shapes would have caught more of
    the 2026-08-25 findings — `18 of 1351 en keys` among them — and both would
    fire constantly. The module docstring says which finding is given up and why.

    Two of these earn their place by being the near-miss rather than the obvious
    miss. `1024/768` is multi-digit on both sides, so only the *three-or-more*
    rule keeps it out. `MODE` is deleted outright — not changed, so it cannot
    cancel against an added line — and only `_SYMBOL`'s required underscore keeps
    a bare uppercase word from being read as a symbol; without it every `OK`,
    `TODO` and `HTTP` in prose becomes a citation.
    """
    root = _repo(tmp_path, {
        "demo/roster.py": "N = 1351\nSCREEN = '1024/768'\nRATIO = '3/4'\nMODE = 1\n",
        "NOTES.md": ("# Notes\n\n18 of 1351 keys, 1024/768 window, ratio 3/4, "
                     "and the MODE flag.\n"),
    })
    _card(root, {"demo/roster.py": "N = 1349\nSCREEN = '1280/800'\nRATIO = '5/8'\n"})

    assert coreference_sweep.check(root) == []


def test_a_citation_moved_to_another_doc_is_not_a_survivor(tmp_path):
    """A documentation restructure relocates prose between files, and the symbols
    it cites go with it. Nothing was replaced — the constant is untouched and
    still live — so every doc that still names it is correct.

    Measured 2026-09-06 on Dungeoneer's `state-md-is-a-changelog-not-a-state`,
    which moved a per-card register out of the always-loaded `state.md` into a
    companion: the gate reported 100 violations, every one a live constant that
    had merely changed file, and every one on a doc the branch never touched. The
    per-file rule was written for a citation moving between paragraphs of one
    doc; it read a cross-file move as a deletion.

    The third file matters, as in the within-one-file case above: without a
    legitimate survivor elsewhere there is nothing to report, and the test would
    pass whether or not the diff is read correctly.
    """
    root = _repo(tmp_path, {
        "demo/roster.py": "EXPECTED_IDS = (1, 2)\n",
        "DESIGN.md": "# Design\n\nThe roster is pinned by EXPECTED_IDS.\n",
        "HISTORY.md": "# History\n\nEmpty for now.\n",
        "NOTES.md": "# Notes\n\nEXPECTED_IDS is the roster pin.\n",
    })
    _card(root, {
        # The citation leaves DESIGN.md and lands in HISTORY.md, unchanged.
        "DESIGN.md": "# Design\n\nThe roster is pinned; see the history.\n",
        "HISTORY.md": "# History\n\nThe roster is pinned by EXPECTED_IDS.\n",
    })

    assert coreference_sweep.check(root) == []


def test_a_replacement_is_still_caught_when_another_doc_gains_the_new_value(tmp_path):
    """The guard against over-correcting. Ignoring diff-wide additions must key
    on the *same* token being re-added, not on the diff having added anything at
    all — otherwise the vault-curve finding dies the moment the same branch
    writes the new curve into a second file, which is exactly what a real
    rebalance does."""
    root = _repo(tmp_path, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.7\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_OLD}.\n",
        "HISTORY.md": "# History\n\nEmpty for now.\n",
        "NOTES.md": f"# Notes\n\n`vault_reward` yields {CURVE_OLD} today.\n",
    })
    _card(root, {
        "demo/contracts.py": "CONTRACT_VAULT_EXP = 1.3\n",
        "DESIGN.md": f"# Design\n\nThe curve is {CURVE_NEW}.\n",
        "HISTORY.md": f"# History\n\nThe curve became {CURVE_NEW}.\n",
    })

    violations = coreference_sweep.check(root)

    assert [v.file for v in violations] == ["NOTES.md"]
    assert CURVE_OLD in violations[0].rule
