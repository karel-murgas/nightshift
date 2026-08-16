#!/usr/bin/env python3
"""`nightshift uninstall` — take back exactly what `init` wrote.

**Why this exists.** A first install is a thing you get wrong once and want to
redo, and every file `init` writes is also a file it *refuses to overwrite* — so a
half-configured repo cannot be fixed by running `init` again. Without this the
retry loop is "delete about eight paths by hand, and remember which of the hook
entries in `settings.json` were mine", which is exactly the sort of instruction
nobody follows correctly at the end of a frustrating evening.

**It removes what this repo's `init` run actually created, and nothing else.** The
authority is `.ai/install.json`, the receipt `init` writes naming every file it
created. The first version of this module asked `init.build_plan` instead and deleted
the intersection with the disk, which is wrong in a way that only shows up in a repo
worth caring about: after a run, our files exist, so `build_plan` reports them as
`kept` — the *same list* as the `CLAUDE.md`, `.gitattributes` or
`.claude/memory/arch.md` the project already had and `init` deliberately left alone.
Uninstalling would have deleted the operator's own documents. Only the run that wrote
a file knows it wrote one; recomputing the list cannot recover that.

**Installs made before the receipt existed** still uninstall, by a narrower route: a
path is removed only if its bytes are exactly what `init` writes. That declines to
delete anything the operator wrote or has since edited, and says which and why —
conservative in the one direction that matters.

**Files the project already had get their block stripped, not deleted.** Four files
are ones `init` appends to rather than writes — `CLAUDE.md`, `.gitattributes`, the
tier-binding doc and `Board/README.md` — because for those the *content* is the
requirement. The appended block is fenced by `nightshift:begin` / `nightshift:end`
markers, and those markers are the record: this finds and removes the block by reading
them, so it works whether or not there is a receipt. The file itself is never deleted.

**Two things it deliberately will not do.**

* `.claude/settings.json` is a *merge* on the way in, so it is a merge on the way
  out: the `python -m nightshift.*` hook entries are removed and every other key —
  your permissions, your `additionalDirectories` — is left exactly as it was. If
  removing the hooks empties the file of everything else, it is still left in place.
* It never touches `.ai/corrections.log` once it has real entries in it. Those are
  earned evidence, they are the one artefact here that cannot be regenerated, and
  a tool that deletes them to tidy up has destroyed the only thing in the
  directory with any history. Reported instead, so the operator can decide.

Dry-run by default. `--yes` performs it.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import discover, init, manifest
from nightshift.manifest import AI_DIR

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Written by a *run* rather than by `init`, so `build_plan` does not know about
# them — but they are unmistakably ours, and leaving them behind means the next
# `init` inherits a stale receipt and somebody's old run logs.
RUNTIME_PATHS = (
    f"{AI_DIR}/.preflight",
    f"{AI_DIR}/runs",
    f"{AI_DIR}/host.json",
    f"{AI_DIR}/STOP",
    f"{AI_DIR}/stale_status.json",
    "Digest.md",
    init.RECEIPT,          # ours by definition, and removed last of all
)


@dataclass
class Removal:
    """What an uninstall would do, computed before any of it happens."""
    root: Path
    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    hooks_removed: int = 0
    settings: str | None = None          # new settings.json content, if it changes
    kept: list[str] = field(default_factory=list)   # ours, but not ours to delete
    receipted: bool = False              # False = pre-receipt install, matched by content
    # Directories holding nothing but our run output — removed with their contents,
    # unlike `dirs`, which are removed only if they end up empty.
    trees: list[str] = field(default_factory=list)
    # path -> its content with our marked block removed. The file itself is the
    # project's and is never deleted; only the block between the markers is ours.
    blocks: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.files or self.dirs or self.trees
                    or self.hooks_removed or self.blocks)


def _has_real_entries(path: Path) -> bool:
    """A corrections log with anything but comments and blank lines in it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.strip() and not line.lstrip().startswith("#")
               for line in text.splitlines())


def strip_hooks(existing: dict) -> tuple[dict, int]:
    """`settings.json` without our hook entries. Returns (settings, removed count).

    Matched on the command containing `nightshift.` — every entry `init` merges in
    is a `python -m nightshift.<module>` command, and that is the only property that
    identifies them without keeping a second copy of the fragment here. A group left
    with no hooks is dropped; a project's own hooks under the same matcher survive.
    """
    out = json.loads(json.dumps(existing))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out, 0

    removed = 0
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        surviving_groups = []
        for group in groups:
            if not isinstance(group, dict):
                surviving_groups.append(group)
                continue
            entries = group.get("hooks")
            if not isinstance(entries, list):
                surviving_groups.append(group)
                continue
            keep = [h for h in entries
                    if not (isinstance(h, dict) and "nightshift." in str(h.get("command", "")))]
            removed += len(entries) - len(keep)
            if keep:
                surviving_groups.append({**group, "hooks": keep})
        if surviving_groups:
            hooks[event] = surviving_groups
        else:
            del hooks[event]
    if not hooks:
        out.pop("hooks", None)
    return out, removed


def _installed_branch(root: Path) -> str:
    """The integration branch this install was configured with, if it can be read.

    A best effort by design: it only affects how faithfully the content fallback can
    re-render, and the receipt path does not consult it at all. An unreadable or
    hand-broken manifest degrades to keeping a few more files than necessary, which is
    the correct direction to fail in.
    """
    try:
        return manifest.load(root).branches.integration or "placeholder"
    except Exception:
        return "placeholder"


def _receipt(root: Path) -> dict | None:
    """The install receipt, or None if this install predates receipts."""
    return init.read_receipt(root)


def _ours_by_content(root: Path, rel: str, staged: str) -> bool:
    """Whether the file on disk is byte-for-byte what `init` writes.

    Compared after normalising line endings, because `init` writes LF through
    `textio.write_text_lf` and a checkout on Windows can hand the same content back
    with CRLF. Comparing raw bytes would call every file on a Windows clone the
    operator's and refuse to remove any of it.
    """
    try:
        actual = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return actual.replace("\r\n", "\n") == staged.replace("\r\n", "\n")


def plan(root: Path) -> Removal:
    """Everything an uninstall would touch, from the receipt `init` left behind."""
    out = Removal(root=root)

    # `init`'s plan for this repo names every path it is *capable* of writing, with the
    # content it would give each one. Used two ways: as a whitelist the receipt is
    # checked against, and — when there is no receipt — as the content to compare.
    #
    # Rebuilt with the branch the *installed* manifest records, not a placeholder. Six
    # templates interpolate `{{integration}}`, so a placeholder makes every one of them
    # differ from its own output and the content fallback keeps files it should remove.
    proposals = discover.survey(root)
    written = init.build_plan(root, integration=_installed_branch(root), proposals=proposals)
    owned = written.staged

    receipt = _receipt(root)
    if receipt is not None:
        out.receipted = True
        # Paths only. `created` is a path→hash map from receipt version 2 on and a bare
        # list before it; `receipt_created` flattens both, and what matters here is
        # unchanged either way — uninstall deletes by path, and never asked what the
        # content was. Reading it through the shared helper is what keeps this working
        # against an install made by any version, which is the one thing an uninstall
        # must do: it is what you reach for when an old install is in the way.
        claimed = list(init.receipt_created(receipt))
        # Intersected with `owned` rather than trusted: the receipt is a committed file
        # like any other, and a hand-edited one naming `src/` must not turn this into an
        # arbitrary delete. A claim outside what `init` can write is reported, not obeyed.
        for rel in sorted(set(claimed)):
            if rel not in owned:
                out.kept.append(f"{rel} — claimed by the receipt but not a path `init` "
                                f"writes; left alone")
                continue
            if (root / rel).is_file():
                out.files.append(rel)
    else:
        for rel, content in sorted(owned.items()):
            # gate-ok(source_reference_liveness): `rel` is joined onto `root`, the
            # project being uninstalled from — never this checkout's own tree.
            if rel == init.SETTINGS or not (root / rel).is_file():
                continue
            if _ours_by_content(root, rel, content):
                out.files.append(rel)
            else:
                out.kept.append(f"{rel} — differs from what `init` writes, so it is "
                                f"yours or you have edited it")

    for rel in list(out.files):
        if rel.endswith("corrections.log") and _has_real_entries(root / rel):
            out.files.remove(rel)
            out.kept.append(f"{rel} — has real entries; earned evidence, not deleted")

    # Files the project already had, to which `init` appended a marked block. The file
    # stays; the block comes out. Driven by the markers in the file rather than by the
    # receipt, so it works on a pre-receipt install too — writing the marker into the
    # file is what makes the append self-describing.
    claimed_appends = set(receipt.get("appended", [])) if receipt else set()
    for rel in sorted(set(owned) | claimed_appends):
        # gate-ok(source_reference_liveness): same `root`-relative comparison as above.
        if rel in out.files or rel == init.SETTINGS:
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if init.BLOCK_BEGIN not in text:
            continue
        if init.BLOCK_END not in text:
            # A truncated block: stripping to end-of-file would take the operator's
            # content with it. Reported, so they can look rather than be surprised.
            out.kept.append(f"{rel} — has a nightshift:begin marker with no matching "
                            f"end; remove the block by hand")
            continue
        out.blocks[rel] = init.strip_block(text)

    for rel in RUNTIME_PATHS:
        path = root / rel
        if path.is_dir():
            # Contents and all. These hold run logs and transcripts we wrote, so the
            # remove-only-if-empty rule does not apply — it left `.ai/runs/` behind after
            # every uninstall that followed a preflight, which is most of them.
            out.trees.append(rel)
        elif path.exists():
            out.files.append(rel)

    # Directories that exist only to hold what we are removing. `apply` removes one
    # only when it is empty afterwards, so listing a directory the operator also
    # keeps their own work in costs nothing.
    # `.ai/recipes` is listed like every other directory here — removed only if empty
    # afterwards — which is exactly what leaves a project's own recipes, and the
    # directory holding them, alone. The shipped spines are named in the receipt and go
    # with the rest; anything the project wrote is not, and keeps the directory alive.
    dirs = [f"{AI_DIR}/gates/data", f"{AI_DIR}/gates", f"{AI_DIR}/recipes", AI_DIR,
            written.tables.get("board", {}).get("root", "Board"),
            ".claude/agents", ".claude/skills", ".claude/memory", ".claude"]
    # The tier-binding document's own directory, derived rather than assumed: the
    # path is a manifest field, so `docs/` is only its default.
    binding = written.tables.get("tiers", {}).get("binding_doc")
    if binding and "/" in binding:
        dirs.append(binding.rsplit("/", 1)[0])
    for rel in dirs:
        if (root / rel).is_dir():
            out.dirs.append(rel)

    settings_path = root / ".claude" / "settings.json"
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # gate-ok(source_reference_liveness): names `settings_path`, a file under
            # `root` — the project being uninstalled from, never this checkout, which
            # carries no `.claude/` of its own.
            out.kept.append(".claude/settings.json — unreadable; hooks left alone")
        else:
            merged, removed = strip_hooks(existing)
            if removed:
                out.hooks_removed = removed
                # Deleted only when the receipt says `init` created the file and nothing
                # but our hooks is left in it. Emptiness alone is not enough evidence: a
                # project whose settings held only hooks of its own, or a bare `{}`
                # somebody committed, would look identical from here.
                if not merged and receipt is not None and receipt.get("settings_created"):
                    # gate-ok(source_reference_liveness): the same `root`-relative
                    # settings file as above, queued for removal from the project
                    # being uninstalled from.
                    out.files.append(init.SETTINGS)
                else:
                    out.settings = json.dumps(merged, indent=2) + "\n"
    return out


def apply(removal: Removal) -> None:
    """Perform it. Files first, then directories, deepest first so they are empty."""
    import shutil

    from nightshift import textio

    for rel in removal.trees:
        shutil.rmtree(removal.root / rel, ignore_errors=True)

    for rel in removal.files:
        path = removal.root / rel
        if path.is_file():
            path.unlink()

    if removal.settings is not None:
        textio.write_text_lf(removal.root / ".claude" / "settings.json", removal.settings)

    for rel, content in sorted(removal.blocks.items()):
        textio.write_text_lf(removal.root / rel, content)

    # Deepest first, and only when empty: a directory the operator has put their own
    # work in is theirs, and an uninstall that took it out with the rest would be
    # the destructive kind of tidy.
    for rel in sorted(set(removal.dirs), key=lambda r: -r.count("/")):
        path = removal.root / rel
        for sub in sorted(path.rglob("*"), key=lambda p: -len(p.parts)):
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def report(removal: Removal) -> None:
    if removal.empty:
        print("\n  Nothing to remove — this repo has no nightshift install.")
        return
    if removal.receipted:
        print(f"  Reading {init.RECEIPT} — the list of files that install created.")
    else:
        print(f"  No {init.RECEIPT}: this install predates the receipt. Falling back to")
        print("  removing only files whose content is exactly what `init` writes, so")
        print("  anything you wrote or edited is kept and listed below.")
    if removal.files:
        print(f"\n  delete {len(removal.files)} file(s):")
        for rel in removal.files:
            print(f"    - {rel}")
    if removal.trees:
        print("\n  delete these directories and their contents (our run output):")
        for rel in removal.trees:
            print(f"    - {rel}/")
    if removal.dirs:
        print("\n  remove these directories, if empty afterwards:")
        for rel in sorted(set(removal.dirs)):
            print(f"    - {rel}/")
    if removal.blocks:
        print(f"\n  strip nightshift's marked block from {len(removal.blocks)} file(s) "
              f"of yours\n  (the file stays; the rest of it is untouched):")
        for rel in sorted(removal.blocks):
            print(f"    ~ {rel}")
    if removal.hooks_removed:
        # gate-ok(source_reference_liveness): describing the operator's own
        # `.claude/settings.json` in the project being uninstalled from, printed
        # for them to read — not a path in this checkout.
        print(f"\n  unmerge {removal.hooks_removed} hook entr"
              f"{'y' if removal.hooks_removed == 1 else 'ies'} from "
              f".claude/settings.json (every other key kept; the file is "
              f"re-serialised, so its formatting may change)")
    if removal.kept:
        print("\n  kept on purpose:")
        for note in removal.kept:
            print(f"    = {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightshift uninstall",
        description="Remove what `nightshift init` wrote into this repo, and nothing "
                    "else. Reports by default; --yes performs it.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to clean (default: the working directory)")
    parser.add_argument("--yes", action="store_true", help="actually remove")
    args = parser.parse_args(argv)

    root = (args.root or Path.cwd()).resolve()
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 2

    print(f"\n  nightshift uninstall — {root}")
    removal = plan(root)
    report(removal)
    if removal.empty:
        return 0

    if not args.yes:
        print("\n  Nothing done. Re-run with --yes to perform it.")
        print("  The package itself stays installed; `pip uninstall nightshift`")
        print("  is the separate step, and you do not need it to re-run `init`.")
        return 0

    apply(removal)
    print(f"\n  Removed. `nightshift init` will now start from scratch here.")
    print("  Anything you had committed is still in git — check `git status`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
