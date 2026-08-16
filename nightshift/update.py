#!/usr/bin/env python3
"""`nightshift update` — bring the files an install wrote up to today's templates.

**The gap this closes.** `init` never overwrites: a file that already exists is reported
`kept` and left exactly as it was. That rule is right — it is what makes `init` safe to
re-run and what stops it eating a `CLAUDE.md` somebody wrote — but it means a project
installed in March is still running March's agent charters, March's skills, March's
`Board/README.md`, forever, and nothing anywhere says so. `doctor.drift()` compares the
*manifest* against the tree; nothing compared the *templates* against their installed
copies. Found 2026-08-16, while answering a question about a different gap entirely.

**What makes this safe is the receipt, and specifically its hashes.** A file that differs
from today's template is one of two completely different things — one the operator edited,
or one whose template moved — and they want opposite treatment. The disk cannot tell them
apart; it has no memory of what it used to hold. `init` records what it wrote
(`init.content_hash`, LF-normalised), so this can:

    disk == recorded, template moved     ->  stale     overwrite; nothing of yours is there
    disk != recorded, template unchanged ->  yours     leave it; you edited it, we did not
    disk != recorded, template moved     ->  conflict  never overwritten, ever

**The conflict case is the whole design, not an edge.** It is the one an evolving framework
produces constantly — you customised a charter, and the charter's template also grew a
section — and the wrong answers are "clobber it" and "shrug". So a conflict is never
resolved by this module guessing. It is *reported*, and four verbs resolve it:
`--diff` to see it, `--take` to accept the template, `--keep` to record that you looked at
this version and chose your own, `--merge` to hand both to an agent. `--keep` writes the
template's hash to the receipt's `declined` map, so the same question stays answered until
the template moves again — a report that re-asks something you already decided is a report
you stop reading.

**Three files are frozen and are never candidates.** `.ai/manifest.toml`, `.ai/hosts.json`
and `.ai/corrections.log` are *records* — of an interview, of a machine, of what went
wrong here — not copies of a template. The manifest says "edit freely, it is yours now" in
its own header. The corrections log is the one artefact in the tree that cannot be
regenerated, which is why `uninstall` already refuses to delete one with entries in it.

**Report by default; `--apply` writes.** Same shape as `uninstall`, and for the same
reason: a command whose first invocation — the one you type to find out what it does —
rewrites files in your repo is the wrong default.

**Receipt version 2 is required.** Version 1 recorded paths without content, so `stale` and
`yours` are indistinguishable under it and every answer would be a guess. Refused outright
rather than guessed at; there are two installs in the world that predate it and they are
being updated by hand.

No LLM in this module except `--merge`, which dispatches one through `runner._run_worker`
exactly as `fix` does — the one place the Claude CLI is executed.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import init, runner, textio, tiers, uninstall
from nightshift.manifest import AI_DIR, MANIFEST_NAME, ManifestError, find_root

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

#: Records, not template copies. Each is written once from answers or accumulated by
#: hand, and re-rendering any of them would overwrite the thing that makes it useful.
#: gate-ok(source_reference_liveness): all three name files in the project being
#: updated, never this framework's own checkout, which has no hosts.json or install
#: receipt of its own and whose manifest is not written by `init`.
FROZEN = (f"{AI_DIR}/{MANIFEST_NAME}", f"{AI_DIR}/hosts.json", f"{AI_DIR}/corrections.log")

#: Written beside a conflicted file by `--take` before it overwrites, so the version
#: being replaced is recoverable for as long as it takes to notice.
BACKUP_SUFFIX = ".nightshift-old"

#: How long a merge agent may run. Merging two versions of one document is small,
#: bounded work — far below `fix`'s rounds — but it is a real session and a network.
MERGE_TIMEOUT_S = 600

#: Verdicts, in the order a report should show them: what changed, then what needs you,
#: then what is fine.
STALE, MISSING, CONFLICT, DECLINED, YOURS, CURRENT, UNTRACKED, FROZEN_V = (
    "stale", "missing", "conflict", "declined", "yours", "current", "untracked", "frozen")

#: The verdicts `--apply` acts on. Deliberately short: everything else is either
#: nothing to do, or something only a person can decide.
APPLIES = (STALE, MISSING)


@dataclass
class Finding:
    """One path, judged.

    Deliberately does not carry the recorded hash. The first draft did, and nothing
    ever read it — the classification is the *answer* to the question that hash asks,
    so keeping it alongside is a second copy of a fact already spent. This codebase
    names that shape three times over (`schema-field-nothing-read`).
    """
    rel: str
    verdict: str
    #: What the template says today. `None` for a path we no longer ship.
    staged: str | None = None

    @property
    def actionable(self) -> bool:
        return self.verdict in APPLIES


@dataclass
class Survey:
    """Every finding, plus what the settings merge would change."""
    root: Path
    findings: list[Finding] = field(default_factory=list)
    #: New `.claude/settings.json` content, if our hook entries are out of date.
    settings: str | None = None
    settings_added: int = 0
    settings_removed: int = 0

    def by(self, *verdicts: str) -> list[Finding]:
        return [f for f in self.findings if f.verdict in verdicts]

    @property
    def changes(self) -> int:
        """How many things `--apply` would actually do."""
        return len([f for f in self.findings if f.actionable]) + bool(self.settings)


class UpdateError(Exception):
    """A refusal with a reason the operator can act on."""


# --------------------------------------------------------------------------
# Reading the project back
# --------------------------------------------------------------------------


def manifest_tables(root: Path) -> dict[str, dict]:
    """`.ai/manifest.toml` as the plain table dict `stage_templates` renders from.

    Read raw rather than through `manifest.load()`, which returns typed objects with
    defaults filled in. What is wanted here is *what the file says* — the tokens a
    template interpolates come from declared values, and a default silently standing in
    for an absent one would re-render every template against a fact the project never
    stated.

    **Discovery is deliberately not re-run.** `init` surveys the repo to *propose* these
    values; by now they are answered and committed. Re-surveying could return a different
    `[project].name` after a rename and quietly rewrite every template around it — an
    update is not the place to relitigate the install interview.
    """
    path = root / AI_DIR / MANIFEST_NAME
    try:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    except OSError as exc:
        raise UpdateError(f"cannot read {AI_DIR}/{MANIFEST_NAME}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise UpdateError(
            f"{AI_DIR}/{MANIFEST_NAME} is not valid TOML ({exc}). Fix it and re-run — "
            f"every template is rendered from it, so an update against a broken manifest "
            f"would write nonsense into a dozen files.") from exc
    return {key: value for key, value in loaded.items() if isinstance(value, dict)}


def require_receipt(root: Path) -> dict:
    """The install receipt, or a refusal naming what to do instead."""
    receipt = init.read_receipt(root)
    if receipt is None:
        raise UpdateError(
            f"no {init.RECEIPT} here, so there is no record of what an install wrote — "
            f"and without that, every file in the tree looks equally like yours and like "
            f"ours. Run `nightshift init` (it never overwrites) to install what is "
            f"missing and lay down a receipt.")
    version = receipt.get("version")
    if version != init.RECEIPT_VERSION:
        raise UpdateError(
            f"{init.RECEIPT} is version {version!r}, and update needs "
            f"{init.RECEIPT_VERSION}. Version 1 recorded which files were written but "
            f"not what was written into them, so `you edited this` and `the template "
            f"moved` cannot be told apart — every answer would be a guess. Re-install "
            f"over it (`nightshift uninstall --yes` then `nightshift init`) to get a "
            f"receipt this can read.")
    return receipt


def _on_disk(path: Path) -> str | None:
    """The file's text, or `None` if it is absent or not text."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------
# The survey
# --------------------------------------------------------------------------


def survey(root: Path) -> Survey:
    """Judge every path the framework owns. Reads only; writes nothing.

    This is the panel's entry point as well as the CLI's, which is why it returns
    structure rather than printing.
    """
    receipt = require_receipt(root)
    recorded = init.receipt_created(receipt)
    declined = receipt.get("declined")
    declined = declined if isinstance(declined, dict) else {}

    tables = manifest_tables(root)
    plan = init.Plan(root=root, tables=tables)
    init.stage_templates(plan, root, tables)

    out = Survey(root=root)
    for rel in sorted(plan.staged):
        staged = plan.staged[rel]
        if rel in FROZEN:
            out.findings.append(Finding(rel, FROZEN_V, staged))
            continue
        # `.claude/settings.json` is a merge on both sides and never a whole-file
        # comparison — handled below, out of the staged loop.
        if rel == init.SETTINGS:
            continue
        was = recorded.get(rel)
        if was is None:
            # Either the project's own file that `init` deliberately left alone, or one
            # of the four it appends a block to. Never ours to rewrite either way.
            out.findings.append(Finding(rel, UNTRACKED, staged))
            continue
        current = _on_disk(root / rel)
        if current is None:
            out.findings.append(Finding(rel, MISSING, staged))
            continue
        now = init.content_hash(current)
        ahead = init.content_hash(staged)
        if now == was:
            out.findings.append(
                Finding(rel, CURRENT if ahead == was else STALE, staged))
        elif ahead == was:
            out.findings.append(Finding(rel, YOURS, staged))
        else:
            verdict = DECLINED if declined.get(rel) == ahead else CONFLICT
            out.findings.append(Finding(rel, verdict, staged))

    _survey_settings(out, root)
    return out


#: `python -m nightshift.some.module` inside a hook command.
_HOOK_MODULE = re.compile(r"-m\s+(nightshift(?:\.[A-Za-z0-9_]+)+)")


def dead_hook(hook: object) -> bool:
    """Whether a hook names a nightshift module that no longer exists.

    **The predicate `update` strips on, and it is deliberately much narrower than
    `uninstall.is_ours`.** The first version of this reused that one — remove every
    `nightshift.` hook, then merge today's template back — which converges, and which
    quietly deletes any hook the *project* added that runs through this package. That is
    not hypothetical: the first real repo it ran against had

        python -m nightshift.hooks.project_script .ai/recipes/hint.py

    where `project_script` exists precisely so a project can reach its own script from
    any working directory. It is in no template, it is entirely the project's, and
    "not in today's template" would have called it dead and removed it.

    So deadness is *checked* rather than inferred from absence: the module is looked up
    with `find_spec`, which resolves it without importing it. A renamed or deleted module
    fails that lookup and is genuinely firing nothing; a live one is left alone, whoever
    put it there. A command with no `-m nightshift...` in it is not ours to judge at all.
    """
    if not isinstance(hook, dict):
        return False
    match = _HOOK_MODULE.search(str(hook.get("command", "")))
    if not match:
        return False
    try:
        return importlib.util.find_spec(match.group(1)) is None
    except (ImportError, ValueError):
        return True


def _survey_settings(out: Survey, root: Path) -> None:
    """What the hook merge would change in `.claude/settings.json`.

    **Strip then merge, rather than merge alone.** `init` only ever adds the entries a
    project lacks, which cannot remove one whose command changed — a hook pointing at a
    module that was renamed sits in the file forever, firing nothing. Taking the dead
    ones out first and putting today's back is the only pass that converges. What counts
    as dead is `dead_hook`, and the narrowness of it is the whole safety of this.
    """
    path = root / init.SETTINGS
    if not path.is_file():
        return                       # `init` writes it; there is nothing here to merge
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return                       # `init` already reports this one; do not double up
    if not isinstance(existing, dict):
        return
    fragment = json.loads(
        (init.TEMPLATES / "settings.hooks.json").read_text(encoding="utf-8"))
    fragment.pop("_comment", None)

    stripped, _ = uninstall.strip_hooks(existing, dead_hook)
    merged, _ = init.merge_hooks(stripped, fragment)
    # Compared as parsed structures, not as text. The file is the project's and they
    # may have reindented it; a textual comparison would report a change on every run
    # and `--apply` would reformat somebody's settings for no reason.
    if merged == existing:
        return
    # The counts the *strip and merge* return are the sizes of each half, not the
    # delta: stripping takes out all eleven of our hooks and merging puts today's
    # eleven back, so reporting those would say "11 to add" on a file that needed one
    # entry removed. What a reader wants is what actually changes, so it is the two
    # sets differenced.
    before, after = _hook_identities(existing), _hook_identities(merged)
    out.settings = json.dumps(merged, indent=2) + "\n"
    out.settings_added = len(after - before)
    out.settings_removed = len(before - after)


def _hook_identities(settings: dict) -> set[tuple]:
    """Our hook entries as `(event, matcher, command)`, the key both halves match on."""
    found = set()
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                if isinstance(hook, dict) and "nightshift." in str(hook.get("command", "")):
                    found.add((event, group.get("matcher"), hook.get("command")))
    return found


# --------------------------------------------------------------------------
# Acting
# --------------------------------------------------------------------------


def apply(found: Survey) -> list[str]:
    """Write every safe update. Returns the paths touched, in order.

    Only `stale` and `missing`. A conflict is never written by this function — resolving
    one is `--take`, `--keep` or `--merge`, each of which is a person saying which
    version wins.
    """
    done: list[str] = []
    written: dict[str, str] = {}
    for finding in found.findings:
        if not finding.actionable or finding.staged is None:
            continue
        path = found.root / finding.rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if finding.staged == "":
            path.touch()
        else:
            textio.write_text_lf(path, finding.staged)
        init.make_executable(path)
        written[finding.rel] = init.content_hash(finding.staged)
        done.append(finding.rel)
    if found.settings:
        textio.write_text_lf(found.root / init.SETTINGS, found.settings)
        # Not recorded in `created`: the settings file is a merge, and claiming its
        # content as ours would let a later uninstall delete a file full of the
        # project's own permissions.
        done.append(init.SETTINGS)
    _record(found.root, written)
    return done


def _record(root: Path, hashes: dict[str, str], *,
            declined: dict[str, str] | None = None) -> None:
    """Update the receipt in place — the hashes of what we just wrote, and `declined`.

    Read-modify-write rather than regenerating: only this run knows what it did, and the
    rest of the receipt (`appended`, `settings_created`, every path an earlier run wrote)
    is not ours to recompute.
    """
    receipt = init.read_receipt(root) or {}
    created = init.receipt_created(receipt)
    created.update(hashes)
    receipt["created"] = {rel: h for rel, h in sorted(created.items()) if h is not None}
    if declined is not None:
        stored = receipt.get("declined")
        stored = dict(stored) if isinstance(stored, dict) else {}
        stored.update(declined)
        receipt["declined"] = dict(sorted(stored.items()))
    textio.write_text_lf(root / init.RECEIPT, json.dumps(receipt, indent=2) + "\n")


def find(found: Survey, rel: str) -> Finding:
    """One finding by path, or a refusal listing what the verbs apply to."""
    wanted = rel.replace("\\", "/").strip()
    for finding in found.findings:
        if finding.rel == wanted:
            return finding
    resolvable = [f.rel for f in found.by(CONFLICT, DECLINED, YOURS, STALE)]
    raise UpdateError(
        f"{rel} is not a file nightshift manages here. The ones it does, and which you "
        f"can act on:\n" + "\n".join(f"    {r}" for r in resolvable[:20]))


def diff(finding: Finding, root: Path) -> str:
    """Unified diff, yours -> the template's. Empty when there is nothing between them."""
    current = _on_disk(root / finding.rel) or ""
    staged = finding.staged or ""
    return "".join(difflib.unified_diff(
        current.replace("\r\n", "\n").splitlines(keepends=True),
        staged.replace("\r\n", "\n").splitlines(keepends=True),
        fromfile=f"{finding.rel} (yours)", tofile=f"{finding.rel} (nightshift)"))


def take(found: Survey, finding: Finding) -> str:
    """Overwrite with the template, keeping the replaced file beside it.

    The backup is not belt-and-braces — this is the one verb here that destroys work, and
    it is reached by a button as well as a flag. `.nightshift-old` is gitignored by the
    template `init` writes.
    """
    if finding.staged is None:
        raise UpdateError(f"{finding.rel} has no template to take.")
    path = found.root / finding.rel
    current = _on_disk(path)
    if current is not None:
        textio.write_text_lf(path.with_name(path.name + BACKUP_SUFFIX), current)
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, finding.staged)
    init.make_executable(path)
    _record(found.root, {finding.rel: init.content_hash(finding.staged)})
    return f"{finding.rel} — took the template; yours is at {path.name}{BACKUP_SUFFIX}"


def keep(found: Survey, finding: Finding) -> str:
    """Record that this template version was seen and declined. Writes no file.

    The hash stored is the *template's*, not yours — so editing your copy afterwards
    changes nothing, and the question reopens exactly when the template moves again,
    which is the only time there is something new to decide.
    """
    if finding.staged is None:
        raise UpdateError(f"{finding.rel} has no template to decline.")
    _record(found.root, {}, declined={finding.rel: init.content_hash(finding.staged)})
    return (f"{finding.rel} — kept yours. Silent until the template changes again.")


MERGE_PROMPT = """\
You are merging one file in the repo at {root}.

`{rel}` was installed by the nightshift framework and then edited locally. The
framework's version of it has since changed. Both changes are wanted: produce a single
file that keeps the local customisations AND picks up what moved in the template.

The file as it stands locally is on disk at `{rel}`. The framework's current version is
at `{new}`.

Rules:
  - Write the merged result to `{rel}`. Leave `{new}` alone; it is a scratch copy.
  - Keep every local customisation unless the template's change directly contradicts it.
  - Do not commit, do not stage, do not touch any other file.
  - If a genuine conflict cannot be resolved without a human choosing, keep the local
    version of that part and say so in your final message.

Finish with one short paragraph: what you took from the template, what you kept, and
anything you left for a human.
"""


def merge(found: Survey, finding: Finding, *, permission_mode: str,
          timeout: int = MERGE_TIMEOUT_S) -> tuple[int, str]:
    """Hand both versions to an agent. Returns (exit code, its final message).

    Dispatched through `runner._run_worker` — documented as the one place the Claude CLI
    is executed, and a second call site would make that claim false along with the test
    asserting it. The prompt goes in on **stdin**, never argv (`prompt_not_in_argv`).

    Like `fix`, it does not commit: the result lands dirty in the working tree, where the
    diff is the thing that keeps an automated merge honest.
    """
    if finding.staged is None:
        raise UpdateError(f"{finding.rel} has no template to merge.")
    binary = runner.claude_binary()
    if binary is None:
        raise UpdateError(
            "the `claude` CLI was not found (checked $CLAUDE_BIN, PATH and "
            "~/.local/bin). Install it, or set CLAUDE_BIN — a merge is an agent.")
    if permission_mode == "default":
        raise UpdateError(
            "permission_mode is `default`, which cannot edit files — the agent could "
            "read both versions and write neither. Pass `--permission-mode acceptEdits` "
            f"for this merge, or set it in {AI_DIR}/hosts.json.")

    out_dir = found.root / runner.RUNS / "merge"
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = found.root / (finding.rel + ".nightshift-new")
    scratch.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(scratch, finding.staged)

    text = MERGE_PROMPT.format(root=found.root, rel=finding.rel,
                               new=scratch.relative_to(found.root).as_posix())
    textio.write_text_lf(out_dir / "prompt.md", text)
    argv = [binary, "-p", "--model", _model(found.root),
            *runner._STREAM_ARGV, "--permission-mode", permission_mode]
    done = runner._run_worker(argv, found.root, timeout, out_dir / "stream.jsonl",
                             prompt=text)
    result = runner._terminal_result(done.stdout or "")
    scratch.unlink(missing_ok=True)
    if done.returncode == 0:
        merged = _on_disk(found.root / finding.rel)
        if merged is not None:
            _record(found.root, {finding.rel: init.content_hash(merged)})
    return done.returncode, str(result.get("result") or "").strip()


def _model(root: Path) -> str:
    """The `lead` tier. Merging two versions of a document is judgment, not typing."""
    try:
        return tiers.binding(root).get("lead", "") or "opus"
    except Exception:
        return "opus"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

_HEADINGS = {
    STALE: ("will update (the template moved; you never touched these)", "+"),
    MISSING: ("will write (ours, and gone from disk)", "+"),
    CONFLICT: ("needs you (you edited it, and the template moved)", "!"),
    YOURS: ("yours (edited here; the template has not moved)", "="),
    DECLINED: ("declined (you kept yours against this exact version)", "="),
}


def report(found: Survey) -> None:
    for verdict, (heading, mark) in _HEADINGS.items():
        rows = found.by(verdict)
        if not rows:
            continue
        print(f"\n{heading}:")
        for finding in rows:
            print(f"    {mark} {finding.rel}")
        if verdict == CONFLICT:
            print("\n      Nothing above is overwritten. For each one:")
            print("        python -m nightshift.update --diff  <path>   see it")
            print("        python -m nightshift.update --take  <path>   take the template")
            print("        python -m nightshift.update --keep  <path>   keep yours")
            print("        python -m nightshift.update --merge <path>   have an agent merge")

    if found.settings:
        bits = []
        if found.settings_added:
            bits.append(f"{found.settings_added} to add")
        if found.settings_removed:
            bits.append(f"{found.settings_removed} to remove (a renamed module leaves a "
                        f"hook firing nothing)")
        print(f"\nhook entries in {init.SETTINGS}: {', '.join(bits) or 'changed'}")

    current = len(found.by(CURRENT))
    frozen = len(found.by(FROZEN_V))
    print(f"\n{current} file(s) already current. {frozen} frozen "
          f"(manifest, hosts, corrections — records, never templates).")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightshift update",
        description="Bring the files an install wrote up to today's templates. "
                    "Reports by default; --apply writes.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to update (default: found from the working directory)")
    parser.add_argument("--apply", action="store_true",
                        help="take every safe update. Never touches a conflict")
    parser.add_argument("--diff", metavar="PATH", default=None,
                        help="print a unified diff of one file, yours against ours")
    parser.add_argument("--take", metavar="PATH", default=None,
                        help="overwrite one file with the template, keeping a backup")
    parser.add_argument("--keep", metavar="PATH", default=None,
                        help="record that you chose your version of one file")
    parser.add_argument("--merge", metavar="PATH", default=None,
                        help="dispatch an agent to merge one file's two versions")
    parser.add_argument("--permission-mode", default=None,
                        help="what the merge agent may do. Needs at least acceptEdits")
    args = parser.parse_args(argv)

    try:
        root = (args.root or find_root()).resolve()
    except ManifestError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 2

    try:
        found = survey(root)
    except UpdateError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    try:
        if args.diff:
            text = diff(find(found, args.diff), root)
            print(text if text else "  identical — nothing between the two versions")
            return 0
        if args.take:
            print(f"  {take(found, find(found, args.take))}")
            return 0
        if args.keep:
            print(f"  {keep(found, find(found, args.keep))}")
            return 0
        if args.merge:
            mode = args.permission_mode or str(
                runner.host_setting(root, "permission_mode", "default"))
            code, final = merge(found, find(found, args.merge), permission_mode=mode)
            print(final or "  (the agent said nothing)")
            return code
    except UpdateError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    print(f"\n{'═' * 66}\n  nightshift update\n  {root}\n{'═' * 66}")
    report(found)
    if not args.apply:
        if found.changes:
            print(f"\n  {found.changes} change(s) available — `--apply` to take them.\n")
        else:
            print("\n  Nothing to do.\n")
        return 0
    done = apply(found)
    print(f"\n  updated {len(done)} file(s).")
    if found.by(CONFLICT):
        print(f"  {len(found.by(CONFLICT))} conflict(s) left alone — see above.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
