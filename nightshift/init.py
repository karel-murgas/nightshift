#!/usr/bin/env python3
"""`nightshift init` — stand the framework up in a repo that has none of it.

`07_portability.md` §5 and §8 step 5. Discovery first, then confirmation, then
writes: `discover.survey()` proposes, this module presents, the operator decides,
and only then does anything land on disk.

**What makes this safe is the same rule that makes `hosts.json` safe** — a check
that discovers a fact may probe; a check that bounds behaviour must be declared.
So every `HIGH` proposal is accepted unless objected to, `branches.integration` is
asked about **every time** including under `--yes`, and `capabilities` /
`permission_mode` are never inferred at all. `discover` enforces those; this module
must not quietly widen them.

**Nothing is overwritten.** A file that already exists is reported as `kept` and
left exactly as it was, with two deliberate exceptions that are merges rather than
overwrites: `.claude/settings.json` gains the hook entries it lacks, and
`.ai/manifest.toml` is written only if absent. `init` on a configured repo is
therefore safe to run, and that is the point — it is also how you add the pieces a
half-configured repo is missing. Use `doctor` to see drift; use `init` to fill gaps.

**Why writing is a separate pass from deciding.** Every decision is collected
before the first file is created, so a run that is interrupted mid-interview leaves
nothing behind. A half-initialised repo is worse than an uninitialised one: the
gates half-run, and the failure looks like a bug in the framework rather than an
incomplete install.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import discover
from nightshift import textio
from nightshift.manifest import AI_DIR, MANIFEST_NAME

TEMPLATES = Path(__file__).resolve().parent / "templates"

# Board lanes come from the one place that owns them, never a list retyped here.
from nightshift.board import LANES  # noqa: E402


@dataclass
class Plan:
    """Every decision, made before anything is written.

    `writes` maps a repo-relative path to its finished content. Holding the whole
    plan in memory is what makes `--dry-run` honest: it prints exactly the bytes a
    real run would write, rather than a description of them.
    """
    root: Path
    tables: dict[str, dict] = field(default_factory=dict)
    writes: dict[str, str] = field(default_factory=dict)
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --- rendering ---------------------------------------------------------------


def tokens(root: Path, tables: dict[str, dict]) -> dict[str, str]:
    """The substitutions the templates declare. See `templates/README.md`."""
    project = tables.get("project", {})
    packages = project.get("source_dirs") or []
    name = project.get("name") or root.name
    return {
        "{{package}}": str(packages[0]) if packages else name,
        "{{project}}": str(name),
        "{{integration}}": str(tables.get("branches", {}).get("integration") or "the integration branch"),
        "{{maintainer}}": _git_user(root),
        "{{hostname}}": socket.gethostname(),
    }


def render(text: str, values: dict[str, str]) -> str:
    """Plain replacement, no template engine.

    A template that needs a dependency to read is a template nobody audits, and
    every one of these is a document a human is expected to read and edit after it
    lands. `str.replace` is also the only thing that cannot mangle the markdown.
    """
    for token, value in values.items():
        text = text.replace(token, value)
    return text


def _git_user(root: Path) -> str:
    try:
        done = subprocess.run(["git", "-C", str(root), "config", "user.name"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return "the maintainer"
    return done.stdout.strip() or "the maintainer"


# --- the manifest ------------------------------------------------------------


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value))


def manifest_text(tables: dict[str, dict]) -> str:
    """`.ai/manifest.toml`, hand-shaped rather than dumped.

    Written by hand because the manifest is the one file whose *comments* are
    load-bearing: `[branches].integration` has no default on purpose, and a
    generated file with no explanation of that is how the next person deletes the
    line to make an error go away.
    """
    out = [
        "# nightshift project configuration. Schema: nightshift/manifest.py.",
        "# Written by `nightshift init`; edit freely, it is yours now.",
        "#",
        "# `nightshift doctor` re-runs discovery against this file and reports drift",
        "# — a renamed package, a moved test dir, an integration branch that is gone.",
        "",
    ]
    # `[layering]` is a list of tables and `[audit]`/`[i18n]` are opt-in, so the
    # order here is the order a reader wants: identity, then policy, then extras.
    for table in ("project", "tests", "branches", "board", "tiers", "worker",
                  "memory", "dead_code", "audit"):
        fields = tables.get(table)
        if not fields:
            continue
        out.append(f"[{table}]")
        if table == "branches":
            out.append("# No default, ever: forbidden_bases() is built from this, and a wrong")
            out.append("# answer means every card is built on a branch nobody wanted.")
        for key, value in fields.items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    rules = tables.get("layering", {}).get("forbid")
    if rules:
        for rule in rules:
            out.append("[[layering.forbid]]")
            out.append(f'importer = {json.dumps(rule["importer"])}')
            out.append(f'imports = {json.dumps(rule["imports"])}')
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# --- settings.json -----------------------------------------------------------


def merge_hooks(existing: dict, fragment: dict) -> tuple[dict, int]:
    """Add the hook entries a project's `settings.json` lacks. Returns (merged, added).

    A merge and not a write, because `settings.json` also carries `permissions` and
    `additionalDirectories`, which are the project's and must survive. Matching is
    on the hook *command*: the same command under the same matcher is already wired,
    whatever its timeout or status message says — those are a project's to tune.
    """
    merged = json.loads(json.dumps(existing))  # deep copy; these are small
    hooks = merged.setdefault("hooks", {})
    added = 0
    for event, groups in fragment.get("hooks", {}).items():
        current = hooks.setdefault(event, [])
        wired = {
            (group.get("matcher"), hook.get("command"), hook.get("if"))
            for group in current if isinstance(group, dict)
            for hook in group.get("hooks", []) if isinstance(hook, dict)
        }
        for group in groups:
            wanted = [
                hook for hook in group.get("hooks", [])
                if (group.get("matcher"), hook.get("command"), hook.get("if")) not in wired
            ]
            if wanted:
                current.append({**group, "hooks": wanted})
                added += len(wanted)
    return merged, added


# --- planning ----------------------------------------------------------------


def _accept(root: Path, proposals: list[discover.Proposal],
            integration: str | None, optional: set[str] | None = None) -> dict[str, dict]:
    """Every `HIGH` proposal, plus the `CONFIRM` ones the operator said yes to.

    `as_manifest_tables` used to take the whole survey, which quietly wrote both
    optional `CONFIRM` fields — see `discover`'s docstring. Accepting them by name
    keeps "a guess is never written unasked" a property of the code rather than of
    whoever reads the report.
    """
    accepted = set(optional or ())
    tables = discover.as_manifest_tables(
        [p for p in proposals
         if not p.needs_confirmation or p.key in accepted or p.key == "branches.integration"])
    if integration:
        tables.setdefault("branches", {})["integration"] = integration
    else:
        tables.get("branches", {}).pop("integration", None)
        if not tables.get("branches"):
            tables.pop("branches", None)
    return tables


def build_plan(root: Path, *, integration: str | None,
               proposals: list[discover.Proposal] | None = None,
               optional: set[str] | None = None) -> Plan:
    """Every file `init` would create, with its final content. Writes nothing.

    `optional` is the set of non-required `CONFIRM` keys the operator accepted.
    Omitted means none, which is the correct non-interactive answer.
    """
    proposals = proposals if proposals is not None else discover.survey(root)
    tables = _accept(root, proposals, integration, optional)
    plan = Plan(root=root, tables=tables)
    values = tokens(root, tables)
    by_key = {p.key: p for p in proposals}

    def stage(rel: str, content: str) -> None:
        if (root / rel).exists():
            plan.kept.append(rel)
        else:
            plan.writes[rel] = content

    stage(".gitattributes", (TEMPLATES / "gitattributes").read_text(encoding="utf-8"))
    # CLAUDE.md carries the framework's half of the rules — how to run, the branch-role
    # source of truth, the board and corrections contracts. `branch_role_prose` reads it,
    # the charters point at it, and `project.doc_files` defaults to it, so a repo without
    # one has three things quietly pointing at nothing.
    stage("CLAUDE.md", render((TEMPLATES / "CLAUDE.md").read_text(encoding="utf-8"), values))
    stage(f"{AI_DIR}/corrections.log",
          (TEMPLATES / "ai" / "corrections.log").read_text(encoding="utf-8"))
    stage(f"{AI_DIR}/gates/data/corrections_vocab.json",
          (TEMPLATES / "ai" / "gates" / "data" / "corrections_vocab.json").read_text(encoding="utf-8"))
    stage(f"{AI_DIR}/hosts.json",
          render((TEMPLATES / "ai" / "hosts.json").read_text(encoding="utf-8"), values))

    board_root = tables.get("board", {}).get("root", "Board")
    stage(f"{board_root}/README.md",
          render((TEMPLATES / "board" / "README.md").read_text(encoding="utf-8"), values))
    for lane in LANES:
        stage(f"{board_root}/{lane}/.gitkeep", "")

    # Memory stubs, so `CLAUDE.md`'s table points at files that exist. A table naming
    # four missing files is the first thing a session learns to ignore.
    for memory in sorted((TEMPLATES / "memory").glob("*.md")):
        stage(f".claude/memory/{memory.name}",
              render(memory.read_text(encoding="utf-8"), values))

    for charter in sorted((TEMPLATES / "agents").glob("*.md")):
        stage(f".claude/agents/{charter.name}",
              render(charter.read_text(encoding="utf-8"), values))
    for skill in sorted((TEMPLATES / "skills").glob("*/SKILL.md")):
        stage(f".claude/skills/{skill.parent.name}/SKILL.md",
              render(skill.read_text(encoding="utf-8"), values))

    # The tier binding: a document is written only if none already carries the
    # block. Writing a second one would create the exact duplicate §16 forbids.
    binding = tables.get("tiers", {}).get("binding_doc") or "docs/tier-binding.md"
    tables.setdefault("tiers", {})["binding_doc"] = binding
    stage(binding, render((TEMPLATES / "tier-binding.md").read_text(encoding="utf-8"), values))

    # The manifest is staged LAST, because `binding_doc` above is decided during the
    # pass and the file has to record the decision. Staged through `stage()` like
    # everything else: an earlier version wrote it directly to get the late value in,
    # and that silently overwrote a hand-written manifest — caught by
    # `test_an_existing_manifest_is_never_rewritten`, which is the whole reason the
    # "nothing is overwritten" rule is asserted rather than stated.
    stage(f"{AI_DIR}/{MANIFEST_NAME}", manifest_text(tables))

    # settings.json is the one merge.
    fragment = json.loads((TEMPLATES / "settings.hooks.json").read_text(encoding="utf-8"))
    fragment.pop("_comment", None)
    settings_path = root / ".claude" / "settings.json"
    existing: dict = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            plan.notes.append(
                f".claude/settings.json is unreadable ({exc}) — hooks NOT wired. Fix the "
                f"file and re-run; overwriting it would take your permissions with it.")
            existing = {}
            settings_path = None  # type: ignore[assignment]
    if settings_path is not None:
        merged, added = merge_hooks(existing, fragment)
        if added:
            plan.writes[".claude/settings.json"] = json.dumps(merged, indent=2) + "\n"
            plan.notes.append(f"{added} hook entr{'y' if added == 1 else 'ies'} wired into "
                              f".claude/settings.json (existing keys untouched)")
        else:
            plan.kept.append(".claude/settings.json")

    # A guess that was not accepted is worth one line, or the operator never learns
    # the gate behind it is switched off — which is the quiet half of "absence is
    # meaningful". Says what it would have written, so adding it by hand is a copy.
    for proposal in proposals:
        if not proposal.needs_confirmation or proposal.value is None:
            continue
        table, _, field_name = proposal.key.partition(".")
        if tables.get(table, {}).get(field_name) is not None:
            continue
        if proposal.key == "branches.integration":
            continue  # its absence gets its own, louder message in `main`
        plan.notes.append(
            f"{proposal.key}: not written. Discovery suggests "
            f"{'; '.join(_describe(proposal.value))} — add it to "
            f"{AI_DIR}/{MANIFEST_NAME} by hand if you agree. Until then the gate "
            f"that reads it does not run, which is a fine day-one state.")

    # The two environment problems that fix nothing by being written down.
    crlf = by_key.get("crlf_worktree")
    if crlf and crlf.value:
        plan.notes.append(f"CRLF working tree: {crlf.why}")
    for key in ("hosts.capabilities", "hosts.permission_mode", "memory.budget_bytes"):
        proposal = by_key.get(key)
        if proposal:
            plan.notes.append(f"{key}: {proposal.why}")
    return plan


def apply(plan: Plan) -> None:
    """Write the plan. Creates parents; never touches a path in `kept`."""
    for rel, content in sorted(plan.writes.items()):
        path = plan.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if content == "":
            path.touch()
        else:
            textio.write_text_lf(path, content)


# --- the interview -----------------------------------------------------------


def _ask(prompt: str, default: str | None) -> str | None:
    suffix = f" [{default}]" if default else " [none]"
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def confirm_integration(root: Path, proposal: discover.Proposal,
                        *, assume_yes: bool, given: str | None) -> str | None:
    """The one field that is asked about even under `--yes`.

    `--yes` is for the twenty high-confidence fields; it is not a licence to guess
    this one. A caller that genuinely wants no interaction passes `--integration`
    and has therefore stated the answer, which is the whole distinction.
    """
    if given:
        return given
    print(f"\n  branches.integration — {proposal.why}")
    print("  This is the branch every card is built on. It is never guessed:")
    print("  forbidden_bases() is derived from it, and a wrong answer produces")
    print("  work, commits and merges on a branch nobody wanted.")
    if assume_yes and not sys.stdin.isatty():
        print("  --yes does NOT cover this field. Re-run with --integration <branch>.")
        return None
    value = proposal.value if isinstance(proposal.value, str) else None
    answer = _ask("  integration branch", value)
    return answer if answer and answer != "none" else None


def confirm_optional(proposals: list[discover.Proposal], *,
                     assume_yes: bool, interactive: bool) -> set[str]:
    """The non-required `CONFIRM` fields the operator accepts. Default: none.

    `--yes` does not cover these either, and for the same reason it does not cover
    the integration branch: each is a claim about the project, not a fact read off
    it. The difference is what "no" means — the integration branch has no working
    default so `init` says so and exits non-zero, while an omitted
    `[memory].orientation` or `[layering]` simply leaves the gate that reads it
    switched off, which is a fine state for a repo on day one.
    """
    optional = [p for p in proposals
                if p.needs_confirmation and p.key != "branches.integration"
                and p.value is not None]
    if not optional or not interactive:
        return set()

    accepted: set[str] = set()
    for proposal in optional:
        print(f"\n  {proposal.key} — {proposal.why}")
        for line in _describe(proposal.value):
            print(f"      {line}")
        default = "y" if assume_yes else "n"
        answer = (_ask("  write this to the manifest? [y/N]", default) or "n").lower()
        if answer.startswith("y"):
            accepted.add(proposal.key)
    return accepted


def _describe(value: object) -> list[str]:
    """A proposal's value as lines a person can actually read before saying yes."""
    if isinstance(value, list):
        return [
            (f"{v['importer']} must not import {v['imports']}"
             if isinstance(v, dict) and "importer" in v else str(v))
            for v in value
        ]
    return [str(value)]


def report(plan: Plan, proposals: list[discover.Proposal]) -> None:
    print("\ndiscovered:")
    for proposal in proposals:
        if proposal.confidence == discover.NEVER:
            continue
        value = proposal.value
        note = ""
        # A confirmed field reports what will be WRITTEN, not what was guessed —
        # otherwise the one line an operator most needs to check shows the
        # rejected proposal and the accepted answer appears nowhere.
        table, _, key = proposal.key.partition(".")
        decided = plan.tables.get(table, {}).get(key)
        if proposal.needs_confirmation and decided != value:
            shown_guess = value if value is not None else "—"
            note = (f"   (not written — discovery guessed: {shown_guess})"
                    if decided is None
                    else f"   (discovery proposed: {shown_guess})")
            value = decided
        if isinstance(value, list) and len(value) > 3:
            value = value[:3] + [f"...({len(value)})"]
        mark = "?" if proposal.needs_confirmation else " "
        shown = value if value is not None else "—"
        print(f"  {mark} {proposal.key:<32} {shown}{note}")
    if plan.writes:
        print("\nwrite:")
        for rel in sorted(plan.writes):
            print(f"    + {rel}")
    if plan.kept:
        print("\nkeep (already present, untouched):")
        for rel in sorted(plan.kept):
            print(f"    = {rel}")
    if plan.notes:
        print("\nyour call:")
        for note in plan.notes:
            print(f"    ! {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightshift init",
        description="Stand nightshift up in this repo. Discovers, asks, then writes.")
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to initialise (default: the working directory)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the exact plan and write nothing")
    parser.add_argument("--yes", action="store_true",
                        help="accept every high-confidence proposal. Does NOT cover "
                             "branches.integration — use --integration for that")
    parser.add_argument("--integration", default=None,
                        help="the integration branch, stated rather than guessed")
    parser.add_argument("--layering", action="store_true",
                        help="print the one-way dependencies discovery found and exit, "
                             "so layering rules can be chosen rather than accepted")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    root = (args.root or Path.cwd()).resolve()
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 2

    if args.layering:
        found = discover.layering(root)
        rules = found.value if isinstance(found.value, list) else []
        if not rules:
            # The >12 case returns None with the count in `why`, so recompute raw.
            print(found.why)
            return 0
        for rule in rules:
            print(f"  {rule['importer']} must not import {rule['imports']}")
        return 0

    print(f"nightshift init — {root}")
    proposals = discover.survey(root)
    by_key = {p.key: p for p in proposals}

    integration = confirm_integration(
        root, by_key["branches.integration"],
        assume_yes=args.yes, given=args.integration)
    optional = confirm_optional(
        proposals, assume_yes=args.yes,
        interactive=not args.dry_run and sys.stdin.isatty())

    plan = build_plan(root, integration=integration, proposals=proposals,
                      optional=optional)
    report(plan, proposals)

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0
    apply(plan)
    print(f"\nwrote {len(plan.writes)} file(s). Next:")
    print("    python -m nightshift.doctor        # the per-machine preconditions")
    print("    python -m nightshift.gates.run     # should be green on an empty repo")
    if not integration:
        print("\n  [branches].integration is UNSET — nothing can dispatch until you")
        print("  declare it in .ai/manifest.toml.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
