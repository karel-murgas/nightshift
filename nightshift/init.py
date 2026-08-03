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

# At import, not inside `main()`. The interview's headings and prompts carry box
# drawing and arrows, and Windows' console is cp1252 — so any caller that reaches a
# prompt without going through `main()` (a test, a skill driving one question, the
# `nightshift` console script before it dispatches) used to die with a
# UnicodeEncodeError about its own help text. Same trap as `reconcile --help`,
# found the same way: by running it rather than reading it.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

TEMPLATES = Path(__file__).resolve().parent / "templates"

# What a run wrote, so `uninstall` can take back our files without touching the
# project's. Committed on purpose: the install is a property of the repo, not of the
# machine that ran it, and the operator who undoes it need not be the one who did it.
RECEIPT = f"{AI_DIR}/install.json"

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
    # Every path `init` has an opinion about, with the content it would give it —
    # `writes` plus `kept`, and the reason it is a separate field is `uninstall`. Once
    # a run has happened, everything `init` wrote *exists*, so a second `build_plan`
    # files it under `kept` alongside the operator's own pre-existing files. Anything
    # deciding what to delete needs the content to tell those two apart.
    staged: dict[str, str] = field(default_factory=dict)
    # `.claude/settings.json` is the one merge, so "did we write it" and "did we create
    # it" are different questions and only the second licenses a delete.
    settings_created: bool = False
    # Two lists, because conflating them is what put "11 hook entries wired" under a
    # heading reading "your call:". `notes` is work the operator still owes; `info` is
    # work `init` already did that they should know about.
    notes: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)


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
               optional: set[str] | None = None,
               permission_mode: str = "default",
               capabilities: list[str] | None = None,
               budget_bytes: int | None = None) -> Plan:
    """Every file `init` would create, with its final content. Writes nothing.

    `optional` is the set of non-required `CONFIRM` keys the operator accepted.
    Omitted means none, which is the correct non-interactive answer.

    The last three are the interview's answers to the questions that used to be
    printed as advice: they land in `.ai/hosts.json` and `[memory].budget_bytes`
    rather than in a note telling the operator to edit a file.
    """
    proposals = proposals if proposals is not None else discover.survey(root)
    tables = _accept(root, proposals, integration, optional)
    if budget_bytes is not None:
        tables.setdefault("memory", {})["budget_bytes"] = budget_bytes
    plan = Plan(root=root, tables=tables)
    values = tokens(root, tables)
    by_key = {p.key: p for p in proposals}

    def stage(rel: str, content: str) -> None:
        plan.staged[rel] = content
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
          hosts_text(values, permission_mode, list(capabilities or [])))

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
    plan.settings_created = not settings_path.is_file()
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
            plan.info.append(f"{added} hook entr{'y' if added == 1 else 'ies'} wired into "
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

    # The one environment problem that writing a file does not fix — it needs a
    # command run on this box, so it is a note rather than a decision.
    crlf = by_key.get("crlf_worktree")
    if crlf and crlf.value:
        plan.notes.append(
            f"{len(crlf.value)} file(s) are CRLF on disk and invisible to `git status`. "
            f"Fix this checkout with `python -m nightshift.normalize_worktree` — there "
            f"is nothing to commit afterwards, and it is per-machine.")
    return plan


def receipt_text(plan: Plan) -> str:
    """The record of what this run created, for `uninstall` to read back.

    **Why a file rather than a recomputation.** `uninstall` used to ask `build_plan`
    what `init` writes and delete the intersection with the disk. That is wrong in one
    specific and destructive way: after a run, our own files exist, so they arrive as
    `kept` — the same list as a `CLAUDE.md`, `.gitattributes` or `.claude/memory/arch.md`
    the project already had and `init` deliberately left alone. Deleting that
    intersection deletes the operator's work. Only the run that wrote a file knows it
    wrote it, so the run says so.

    `settings_created` is separate because that path is a merge, not a write: if the
    file was already there, uninstall takes its hook entries back out and leaves the
    rest, and only a file `init` itself created may be removed outright.

    A second `init` on the same repo has almost nothing left to write, so the receipt
    *accumulates* rather than replacing: it records every file nightshift ever created
    here. Overwriting would leave a repo whose receipt truthfully says this run created
    nothing, and therefore an uninstall with nothing to remove.
    """
    settings = ".claude/settings.json"
    created = {rel for rel in plan.writes if rel != settings}
    settings_created = settings in plan.writes and plan.settings_created
    try:
        before = json.loads((plan.root / RECEIPT).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    else:
        if isinstance(before, dict):
            created |= {rel for rel in before.get("created", []) if isinstance(rel, str)}
            settings_created = settings_created or bool(before.get("settings_created"))
    return json.dumps({
        "tool": "nightshift",
        "version": 1,
        "created": sorted(created),
        "settings_created": settings_created,
    }, indent=2) + "\n"


def apply(plan: Plan) -> None:
    """Write the plan. Creates parents; never touches a path in `kept`.

    The receipt is written last and describes the rest, so a crash halfway through
    leaves no receipt claiming files that were never created.
    """
    for rel, content in sorted(plan.writes.items()):
        path = plan.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if content == "":
            path.touch()
        else:
            textio.write_text_lf(path, content)
    path = plan.root / RECEIPT
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, receipt_text(plan))


# --- the interview -----------------------------------------------------------


def _ask(prompt: str, default: str | None) -> str | None:
    suffix = f" [{default}]" if default else " [none]"
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _step(n: int, total: int, title: str) -> None:
    """A numbered heading, so the interview reads as a finite sequence.

    The first version of this printed the same decisions as loose paragraphs and
    the operator could not tell how many were left, or which of them still needed
    an answer versus which were being reported. Numbering is the cheapest fix for
    both (the origin project's maintainer, 2026-08-02: *"I have no idea how to make these calls"*).
    """
    print(f"\n{'─' * 66}\n  STEP {n}/{total} — {title}\n{'─' * 66}")


def _choose(prompt: str, options: list[tuple[str, str, str]], default: str) -> str:
    """A numbered menu. `options` is `(value, label, consequence)`; returns a value.

    Menus rather than free text wherever the answer is one of a known few. A prompt
    that asks for a *value* assumes the operator already knows the vocabulary, which
    is exactly what a first install does not.
    """
    for i, (value, label, why) in enumerate(options, start=1):
        mark = "  ← default" if value == default else ""
        print(f"    {i}. {label}{mark}")
        print(f"       {why}")
    valid = {str(i): opt[0] for i, opt in enumerate(options, start=1)}
    valid.update({opt[0]: opt[0] for opt in options})
    while True:
        answer = _ask(prompt, default)
        if answer is None:
            return default
        answer = answer.strip()
        if answer in valid:
            return valid[answer]
        if not sys.stdin.isatty():
            return default
        print(f"    ? not one of {', '.join(str(i) for i in range(1, len(options) + 1))} — try again")


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


# --- the hosts.json interview -------------------------------------------------
#
# These two were printed as advice and never asked, which is the gap that made a
# first install stall: `init` said "permission_mode: never inferred" and left the
# operator to find the file, guess the key and guess the value. **Asking is not a
# weakening of the never-inferred rule.** That rule exists so the answer is a
# decision rather than a probe — and an answer typed at a prompt that quotes the
# consequence is a better declaration than a JSON edit made three days later,
# because the warning is on screen at the moment of choosing. What must not
# change, and does not: the *safe* option is the default, so pressing Enter can
# never arm the machine.

PERMISSION_MODES = [
    ("default", "default — ask before anything",
     "Safest, and cannot run Bash. Code cards will stall: no pytest, no git.\n"
     "       Right for a first look at the board and the gates."),
    ("acceptEdits", "acceptEdits — file edits without asking, still no Bash",
     "Fine for docs and board work. A code card still cannot run its tests."),
    ("bypassPermissions", "bypassPermissions — what a code card actually needs",
     "THE WHOLE MACHINE, NOT A SANDBOX. A dispatched worker can do anything\n"
     "       you can. What contains it: a throwaway worktree on its own branch, and\n"
     "       a runner that refuses to build on your stable branch. Nothing else."),
]


def ask_permission_mode(*, interactive: bool) -> str:
    if not interactive:
        return "default"
    print("\n  How much may a dispatched worker do on this machine?")
    print("  (`.ai/hosts.json` → permission_mode. Change it any time.)\n")
    return _choose("  choose 1-3", PERMISSION_MODES, "default")


def ask_capabilities(*, interactive: bool) -> list[str]:
    """Hardware/stack slugs this box is *allowed* to be used for. Empty is normal.

    Only meaningful once cards carry `requires:`, so a first install should leave it
    empty — and the prompt says so rather than implying a gap.
    """
    if not interactive:
        return []
    print("\n  Does this machine have special hardware a card might require?")
    print("  Slugs like `gpu-box` or `audio-stack`, matched against a card's")
    print("  `requires:`. Almost every install starts with none — leave it blank.")
    answer = _ask("  capabilities (comma-separated, blank for none)", "")
    return [s.strip() for s in (answer or "").split(",") if s.strip()]


def ask_budget(*, interactive: bool) -> int | None:
    """`[memory].budget_bytes` — a *round* number or off. Never the current total.

    The rule it protects is doc 10 §4's: a budget measured off the tree as found is
    the move the gate exists to stop, because it can only ever be satisfied. The
    rule does **not** require refusing to propose anything, which is how this read
    before — "set it to a number you are willing to defend out loud" is a correct
    principle and useless instruction. A round 100 KB is defensible precisely
    because it is arbitrary: it was not derived from what happens to be there.
    """
    if not interactive:
        return None
    print("\n  Cap the size of the docs an agent loads at the start of every")
    print("  session? Over the cap, the gate tells you to move detail into an")
    print("  on-demand file. The number is deliberately NOT measured from your")
    print("  repo — a budget that fits what is already there can never fail.\n")
    choice = _choose("  choose 1-3", [
        ("100000", "100 KB — a round number, recommended",
         "Roughly 25k words of orientation. Generous for a new repo."),
        ("50000", "50 KB — tighter",
         "Sensible if you want the pressure on from day one."),
        ("off", "off — no cap",
         "The gate does not run. Turn it on later by setting the key."),
    ], "100000")
    return None if choice == "off" else int(choice)


def hosts_text(values: dict[str, str], permission_mode: str,
               capabilities: list[str]) -> str:
    """`.ai/hosts.json` with the interview's answers actually in it.

    The template used to be copied verbatim, so every install got
    `permission_mode: "default"` and a note telling the operator to go and change
    it. Rendering the answer is the difference between a decision and a homework
    assignment.
    """
    raw = json.loads(render((TEMPLATES / "ai" / "hosts.json").read_text(encoding="utf-8"),
                            values))
    host = values["{{hostname}}"]
    entry = raw.get(host)
    if isinstance(entry, dict):
        entry["capabilities"] = capabilities
        entry["permission_mode"] = permission_mode
    return json.dumps(raw, indent=2) + "\n"


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
        # Derived from that list rather than staged in it, so it needs its own line or
        # `--dry-run` under-reports by exactly one file — and a dry run that does not
        # name every path it will write is the one thing it must not be.
        print(f"    + {RECEIPT}")
        print(f"      (the list above, so `nightshift uninstall` can take back these "
              f"files and nothing else)")
    if plan.kept:
        print("\nkeep (already present, untouched):")
        for rel in sorted(plan.kept):
            print(f"    = {rel}")
    if plan.info:
        print("\nalso done:")
        for note in plan.info:
            print(f"    * {note}")
    if plan.notes:
        print("\nneeds a command from you:")
        for note in plan.notes:
            print(f"    ! {note}")


def next_steps(plan: Plan, *, integration: str | None, permission_mode: str) -> None:
    """A numbered checklist of the next commands, in order, with what each proves.

    This replaced a section headed "your call:" that listed decisions without
    saying how to act on any of them. The origin project's maintainer, first install, 2026-08-02:
    *"there are some 'your calls' in the console and I have no idea how to make
    these calls."* A closing screen should leave exactly one obvious next command.
    """
    print(f"\n{'═' * 66}\n  DONE — wrote {len(plan.writes) + 1} file(s)\n{'═' * 66}")

    if not integration:
        print("\n  ⚠ STOP. [branches].integration is not set, and nothing can")
        print("    dispatch without it. Open .ai/manifest.toml, add:")
        print("\n        [branches]")
        print('        integration = "your-branch-name"\n')
        print("    ...then re-run `nightshift init`.")
        return

    steps = [
        ("Commit what init wrote",
         "git add -A && git commit -m \"nightshift init\"",
         "The board and the manifest are meant to be tracked. Do this first so\n"
         "     the next steps have a clean tree to talk about."),
        ("Check this machine",
         "nightshift doctor",
         "Line-endings, whether `claude` is on PATH, whether this host is\n"
         "     configured. Everything should be [OK] or [SKIP]."),
        ("Run the gates",
         "python -m nightshift.gates.run",
         "Should be green on a fresh repo. This also runs automatically after\n"
         "     every edit Claude makes, via the hooks init just wired."),
        ("Run the mandatory pre-push check",
         "python -m nightshift.preflight",
         "Gates + your test suite + a receipt. A hook refuses `git push`\n"
         "     without one, so this is the command you will type most."),
    ]
    if permission_mode == "bypassPermissions":
        steps.append((
            "Try one card end to end",
            "python -m nightshift.runner --dry-run",
            "Reports what would dispatch and why not. Write a card into\n"
            "     Board/tasks/ first — `Board/README.md` has the shape. Then:\n"
            "     python -m nightshift.runner --card <id> --max-cards 1"))
    else:
        steps.append((
            "Explore the board (dispatch is off for now)",
            "python -m nightshift.runner --dry-run",
            f"Safe: it writes nothing. Real dispatch needs permission_mode\n"
            f"     `bypassPermissions` — you chose `{permission_mode}`. Change it in\n"
            f"     .ai/hosts.json when you want to try a code card."))

    print("\n  Do these in order:\n")
    for i, (title, command, why) in enumerate(steps, start=1):
        print(f"  {i}. {title}")
        print(f"     $ {command}")
        print(f"     {why}\n")

    print("  Read next: Board/README.md (what a card looks like) and the")
    print("  `manage-board` / `run-the-runner` skills init put in .claude/skills/.")


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
    parser.add_argument("--non-interactive", action="store_true",
                        help="ask nothing. Every optional decision is left unmade and "
                             "reported; permission_mode stays `default`. For scripts")
    parser.add_argument("--permission-mode", default=None,
                        choices=[m[0] for m in PERMISSION_MODES],
                        help="state it rather than being asked. `bypassPermissions` is "
                             "the whole machine, not a sandbox — see the prompt text")
    args = parser.parse_args(argv)   # stdout is already UTF-8: reconfigured at import

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

    interactive = (not args.non_interactive and not args.dry_run
                   and sys.stdin.isatty())

    print(f"\n{'═' * 66}")
    print("  nightshift init")
    print(f"  {root}")
    print(f"{'═' * 66}")
    print("\n  Four questions, then it writes. Every answer goes in a file you")
    print("  own and can edit afterwards; nothing here is permanent.")
    if not interactive:
        print("\n  (non-interactive: defaults everywhere, and it says what it skipped)")

    proposals = discover.survey(root)
    by_key = {p.key: p for p in proposals}

    _step(1, 4, "which branch does work merge into?")
    integration = confirm_integration(
        root, by_key["branches.integration"],
        assume_yes=args.yes, given=args.integration)
    if args.integration:
        # `confirm_integration` returns early when the answer was given, so without
        # this the step prints a heading and nothing at all — which reads as broken.
        print(f"  Using `{args.integration}`, as given on the command line.")
        print("  Work is cut from it and merged back into it; the runner refuses to")
        print("  build on anything else.")

    _step(2, 4, "what may a worker do on this machine?")
    permission_mode = (args.permission_mode
                       or ask_permission_mode(interactive=interactive))
    capabilities = ask_capabilities(interactive=interactive)

    _step(3, 4, "cap the size of the always-loaded docs?")
    budget = ask_budget(interactive=interactive)

    _step(4, 4, "pin any architecture rules discovery found?")
    offerable = [p for p in proposals
                 if p.needs_confirmation and p.key != "branches.integration"
                 and p.value is not None]
    optional = confirm_optional(proposals, assume_yes=args.yes, interactive=interactive)
    if not offerable:
        print("  Nothing to offer — discovery found no one-way dependency worth")
        print("  pinning, and no memory files named by your docs. Both are normal on a")
        print("  fresh repo; `nightshift init --layering` can show the list later.")
    elif not interactive:
        print(f"  {len(offerable)} suggestion(s) skipped — nothing is written that was")
        print("  not asked for. Re-run in a terminal, or see `nightshift init --layering`.")

    plan = build_plan(root, integration=integration, proposals=proposals,
                      optional=optional, permission_mode=permission_mode,
                      capabilities=capabilities, budget_bytes=budget)
    report(plan, proposals)

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0
    apply(plan)
    next_steps(plan, integration=integration, permission_mode=permission_mode)
    return 0 if integration else 1


if __name__ == "__main__":
    raise SystemExit(main())
