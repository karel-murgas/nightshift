"""Change-ordered selection for the Tier-2 staleness sweep.

`stale-hunter` costs ~1 token per byte of doc-plus-source (measured 2026-07-22:
~115k for a 113 KB doc), and the tree in scope is ~1.1 MB across 72 docs. A full
sweep is on the order of a million tokens, so it can never be alphabetical or
calendar-driven — it has to spend the budget where staleness can actually be.

The whole idea, and it is deterministic (no LLM here — §12): **a doc whose named
source files have not moved since it was last verified cannot have gone stale.**
So the selector reads, per doc, the source files it references, asks git whether
any changed since the ledger's last-verified commit for that doc, and ranks by
how many did. Zero-churn docs are skipped entirely; unverified docs sort to the
top because everything they name is "new to the ledger".

This is the piece Karel asked the runner to own — *"if there are any changes in
the documents"* — and putting it here, testable and standalone, is what lets the
runner phase stay a thin loop over `select()`.

The ledger (`.ai/stale_ledger.json`) maps each doc to the commit SHA at which it
was last **completely** verified. It is gitignored and rebuildable: losing it
just means the next sweep treats everything as unverified, which is safe (it
over-checks, never under-checks). Karel's rule holds — a doc is written to the
ledger only when its checker returns a *complete* verdict, so a sweep cut off
mid-doc re-checks that doc next time rather than recording a lie.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from nightshift import gitpaths
from nightshift import textio  # `stale_status.json` is committed; write_text would CRLF it
from nightshift.gates import doc_scan  # the reference extractor and doc scope live here
from nightshift.manifest import AI_DIR

LEDGER = Path(".ai") / "stale_ledger.json"

# The ledger above is gitignored (per-machine, rebuildable) and answers "is this
# doc verified against current source". This file answers a different question
# — "did a sweep run at all, and when" — which Digest.md needs to be able to
# show truthfully on any machine, so it must be a real, committed fact rather
# than something that goes silent the moment the ledger is missing or cleared.
# gate-ok(source_reference_liveness): committed once a `--stale` sweep writes
# it (write_status below), not before -- this repo has never run one against
# itself yet, and read_status()'s own contract treats that absence as "never
# run", the correct current fact, not staleness to paper over with a stub.
STATUS = Path(".ai") / "stale_status.json"


def _package_prefixes(repo_root: Path) -> tuple[str, ...]:
    """Prefixes a package-relative reference may be resolved under.

    House style writes `core/i18n.py` for `dungeoneer/core/i18n.py`, so a
    reference is tried bare and then under each of the project's source dirs.
    Was the hardcoded `("", "dungeoneer/")` until 07_portability.md §8 step 4;
    a project that declares no `source_dirs` gets `("",)`, which is the same
    answer the bare half always gave.
    """
    dirs = doc_scan.source_dirs(repo_root)
    return ("", *(f"{d}/" for d in dirs if d != AI_DIR))


# --- which source files a doc is about ------------------------------------


@lru_cache(maxsize=1)
def _symbol_index(repo_root_str: str) -> dict[str, frozenset[str]]:
    """`{symbol_name: {relpath, ...}}` for classes, functions and module-level
    assignments across the source dirs. A name may be defined in more than one
    file; all are returned, because a doc naming it is about all of them."""
    repo_root = Path(repo_root_str)
    index: dict[str, set[str]] = {}
    for rel in doc_scan.source_dirs(repo_root):
        base = repo_root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            relpath = path.relative_to(repo_root).as_posix()
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.append(node.name)
                elif isinstance(node, ast.Assign):
                    names += [t.id for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names.append(node.target.id)
                for name in names:
                    index.setdefault(name, set()).add(relpath)
    return {name: frozenset(paths) for name, paths in index.items()}


def named_source_files(doc: Path, repo_root: Path) -> set[str]:
    """The source relpaths a doc is about: files it names by path, plus files
    that define the symbols it names. Only real source files — a reference that
    resolves to nothing is Tier 1's problem, not this selector's."""
    index = _symbol_index(str(repo_root.resolve()))
    files: set[str] = set()
    for ref in doc_scan.extract_references(doc, repo_root):
        if ref.kind == "path":
            for candidate in (ref.text, *ref.alts):
                token = candidate.strip().lstrip("./")
                if not token.endswith(".py"):
                    # Package-relative shorthand (`core/i18n.py` → <pkg>/…)
                    continue
                for prefix in _package_prefixes(repo_root):
                    p = repo_root / (prefix + token)
                    if p.is_file():
                        files.add((prefix + token))
        else:
            for candidate in (ref.text, *ref.alts):
                files |= index.get(candidate.strip(), frozenset())
    return files


# --- churn since last verified --------------------------------------------


def _changed_files(repo_root: Path, since_sha: str) -> set[str]:
    """Source relpaths changed between `since_sha` and HEAD (posix)."""
    return set(gitpaths.changed(repo_root, f"{since_sha}..HEAD"))


@dataclass(frozen=True)
class Candidate:
    doc: str            # repo-relative posix
    churn: int          # named source files that moved since last verified
    verified_at: str | None
    named: int          # how many source files this doc is about at all


def load_ledger(repo_root: Path) -> dict[str, str]:
    path = repo_root / LEDGER
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_ledger(repo_root: Path, ledger: dict[str, str]) -> None:
    textio.write_text_lf(repo_root / LEDGER,
                         json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def write_status(repo_root: Path, date_iso: str, checked: int, verified: int, carded: int) -> None:
    """Record that a sweep ran, for `digest.py` to read back (`.ai/stale_status.json`,
    committed — see `STATUS` above). Called once per `--stale` invocation that got far
    enough to resolve a model, regardless of whether it found anything to check:
    "ran, nothing to do" and "never run" are different facts, and only writing this on
    a nonzero `checked` would collapse them back into the same silence this exists to
    fix. Takes the date as a parameter rather than reading the clock itself, so this
    stays a plain, deterministically testable function."""
    path = repo_root / STATUS
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(
        path,
        json.dumps({"last_run": date_iso, "checked": checked, "verified": verified,
                    "carded": carded}, indent=2, sort_keys=True) + "\n")


def read_status(repo_root: Path) -> dict | None:
    """The last-written sweep record, or `None` if a sweep has never run here (or the
    file is unreadable — treated the same as never run, not as an error)."""
    path = repo_root / STATUS
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def head_sha(repo_root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def mark_verified(repo_root: Path, doc: str, ledger: dict[str, str]) -> None:
    """Record that `doc` was completely verified at the current HEAD. Only ever
    called on a *complete* verdict — Karel's rule (a cut-off sweep re-checks)."""
    ledger[doc] = head_sha(repo_root)
    save_ledger(repo_root, ledger)


def candidates(repo_root: Path, ledger: dict[str, str] | None = None) -> list[Candidate]:
    """Every in-scope doc with its churn since last verification, most-churned
    first. History-scoped docs are dropped — they are *supposed* to describe the
    past, so a moved source file is not a defect there (stale-hunter's own rule)."""
    ledger = load_ledger(repo_root) if ledger is None else ledger
    # `git diff` from a missing/absent SHA returns nothing; an unverified doc
    # then has churn 0, which would wrongly skip it. So for unverified docs we
    # count all their named files as churn — everything about them is unchecked.
    changed_cache: dict[str, set[str]] = {}
    out: list[Candidate] = []
    for path in doc_scan.doc_files(repo_root):
        text = doc_scan._read(path)
        if doc_scan.is_exempt_file(text):
            continue  # doc_scope: history / external — not this sweep's job
        rel = doc_scan.relpath(path, repo_root)
        named = named_source_files(path, repo_root)
        if not named:
            continue  # a doc about no source cannot go stale against source
        sha = ledger.get(rel)
        if sha is None:
            churn = len(named)
        else:
            if sha not in changed_cache:
                changed_cache[sha] = _changed_files(repo_root, sha)
            churn = len(named & changed_cache[sha])
        out.append(Candidate(rel, churn, sha, len(named)))
    # Most churn first; among equals, never-verified before verified, then name.
    out.sort(key=lambda c: (-c.churn, c.verified_at is not None, c.doc))
    return out


def select(repo_root: Path, n: int, ledger: dict[str, str] | None = None) -> list[Candidate]:
    """The top `n` docs worth checking — churn > 0 only. `n <= 0` selects none;
    `n` larger than the eligible set returns all of it (checking every doc that
    could plausibly have gone stale, which is the sensible 'run it dry' default)."""
    eligible = [c for c in candidates(repo_root, ledger) if c.churn > 0]
    return eligible if n >= len(eligible) else eligible[:max(0, n)]


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from nightshift.manifest import find_root

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="python -m nightshift.stale_sweep",
        description="Change-ordered staleness selection (deterministic).")
    parser.add_argument("--pick", type=int, default=0,
                        help="show the top N churned docs (0 = all eligible)")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to survey (default: found from the working directory)")
    args = parser.parse_args(argv)
    # Was `_HERE.parent` — a name deleted with the `__file__`-derived roots, whose
    # one remaining use was here, so this entry point raised `NameError` on every
    # invocation. The runner's `--stale` path passes an explicit root and was
    # unaffected, which is exactly why nothing noticed.
    repo_root = (args.root or find_root()).resolve()
    chosen = select(repo_root, args.pick or 10_000)
    if not chosen:
        print("nothing to check — every in-scope doc is verified at its named source's current state")
        return 0
    print(f"{len(chosen)} doc(s) worth a Tier-2 check, most-churned first:\n")
    for c in chosen:
        seen = c.verified_at[:8] if c.verified_at else "never"
        print(f"  churn {c.churn:>3} / {c.named:<3} named   last-verified {seen:<8}  {c.doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
