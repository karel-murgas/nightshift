---
name: install-nightshift
description: Install and commission nightshift in this repository — establish the facts, ask the two questions only the maintainer can answer, run init, then diagnose and fix until the checks are green and file cards for whatever needs a decision. Use when the repo has nightshift bootstrapped but not installed, or when an install needs redoing.
---

# Install nightshift in this repository

You are doing the install. The maintainer answers two questions and reads a diff at the
end; everything else is yours. Do not hand them a checklist of commands to run — that
was the previous version of this process and it is the thing this skill replaces.

Work in order. Report each step's real result, not that you attempted it.

## 1. Establish four facts before writing anything

| Fact | How | If you cannot |
|---|---|---|
| this is a git repo | `git rev-parse --show-toplevel` | stop; nothing here works without one |
| Python is 3.11+ | `python --version` | stop; `tomllib` is stdlib from 3.11 |
| the package is importable | `nightshift --help` | `pip install -e <path-to-nightshift>`, then retry |
| the tree is clean | `git status --porcelain` | ask whether to commit or stash first |

State these back before continuing. Getting one wrong produces a repo that looks
configured and is not.

## 2. Ask the two questions that are genuinely theirs

Ask both in one message, with your recommendation, and wait.

**The integration branch.** The branch feature work is cut from and merged back into.
`git branch -a` is context, never an answer — **do not guess this one**. In the origin
project `dev` is *stable* and is a forbidden base, which no heuristic would ever get
right. If the repo has only `main`, say so and ask whether to create an integration
branch or use `main` as both.

**`permission_mode`** — what a dispatched worker may do on this machine:

- `default` — cannot run Bash. Safe, and code cards will stall. Recommend it if they
  want to look around first.
- `acceptEdits` — file edits without prompting, still no Bash.
- `bypassPermissions` — what a code card actually needs, and **the whole machine, not a
  sandbox**. Say that in your own words if they choose it; do not let the menu text
  carry the warning alone.

Everything else `init` would ask, answer yourself with the recommended defaults: the
memory budget is a round **100 KB** (never a number measured off their tree — a budget
that fits what is already there can only ever be satisfied), and layering rules are
**declined** on a first install, because each row is a claim about their architecture
they have not read yet.

## 3. Run the install

```bash
nightshift init --integration <their-branch> --permission-mode <their-answer> --non-interactive
```

Read the output. Three sections matter:

- **write** — files created. Fine.
- **append to** — files they already had, which got a marked block appended
  (`CLAUDE.md`, `.gitignore`, `.gitattributes`, the tier-binding doc, `Board/README.md`).
  Their content is untouched. If `CLAUDE.md` was theirs, the framework's rules are in
  `.ai/CLAUDE.md` and their file points at it — **read `.ai/CLAUDE.md` now**, because
  those rules bind you for the rest of this session.
- **needs a command from you** — usually the CRLF case on Windows. Run
  `python -m nightshift.normalize_worktree` if it appears; it is per-machine and leaves
  nothing to commit.

Then commit, with an explicit pathspec (a bare `git commit -a` is refused by a hook):

```bash
git add -A && git commit -m "nightshift init"
```

## 4. Commission it — diagnose and fix

```bash
nightshift fix --permission-mode bypassPermissions
```

This runs every check and dispatches an agent to fix what failed, up to three rounds. It
is allowed to elevate for that pass alone; the standing `permission_mode` in
`.ai/hosts.json` is unchanged.

If dispatch is not available on this machine (no `claude` on PATH), do the same work
yourself, in this order, and obey the same prohibitions — run
`nightshift fix --dry-run` and read them; they are the point:

```bash
python -m nightshift.doctor
python -m nightshift.gates.run
python -m nightshift.preflight
```

**A first install is usually red in one or two places, and that is normal.** The two
you should expect:

- `dead_code` complaining there is nothing to scan — `[project].source_dirs` did not
  survive discovery. Look at the repo, declare the real package(s) in
  `.ai/manifest.toml`, re-run.
- `corrections` failing — by design. It wants a lesson logged or an honest zero. On a
  fresh install the zero is honest:
  `python -m nightshift.preflight --no-corrections "fresh install, nothing learned yet"`.

Never make a check pass by weakening it. `nightshift fix --dry-run` prints the full
list of forbidden moves and why each one has been made before.

## 5. Offer to restructure the project's documents

**Ask first, and do nothing if they decline.** These are their documents. Say what you
would change and roughly how much moves, then wait.

The structure below was arrived at by measurement in the origin project, not taste. Its
`state.md` reached **196 KB** by accumulating one reasonable dated section per session,
until reading it cost more than re-reading the code — at which point nobody read either.
Everything here follows from that.

**The rule: a document a session always loads says what is true *now*. Dated narrative
goes in a companion loaded only on demand.**

Applied:

| Always loaded | Loaded when a question sends you there |
|---|---|
| `CLAUDE.md` — rules, how to run, what must not be violated | — |
| `.claude/memory/MEMORY.md` — the index, one line per file | — |
| `arch.md` — module map, one row each | `arch_detail.md` — full per-module notes |
| `state.md` — current phase, current state | `state_history.md` — the per-session shipped/fixed log |
| `design.md` — decisions in force | `design_detail.md` — the reasoning and tuning records |

What this means in practice, and each of these is a mistake worth naming to them:

- **`CLAUDE.md` is rules, not history.** No changelog, no "we tried X and it did not
  work", no session notes. A rule with its reason attached, one per line. If it has
  accumulated narrative, that narrative goes to a memory companion or gets deleted.
- **`CLAUDE.md` is not the architecture document either.** A module map belongs in
  `arch.md`, which is loaded from `MEMORY.md`.
- **One line per shipped thing in `state.md`; the story in `state_history.md`.** The
  moment a section starts with a date, it belongs in the companion.
- **`MEMORY.md` is an index, never content.** One line per file with a hook, so a session
  can decide what to open. Content in the index means the index is loaded content.
- **A rule the maintainer gave you gets its own `feedback_<topic>.md`**, listed in
  `MEMORY.md`, with the reason it exists.

Do the work in this order:

1. **Read what is there.** `CLAUDE.md` and every file in `.claude/memory/`, plus any
   `docs/`, `ARCHITECTURE.md`, `NOTES.md` the repo already has.
2. **Say what you propose**, with sizes: what moves where, what gets deleted, what stays.
   Deleting is allowed and often right — a note that was true two refactors ago is worse
   than no note. Ask before deleting anything you are not certain about.
3. **Move, do not rewrite.** Their words, relocated. Rewriting someone's notes loses the
   thing only they knew, and it is not your call to make prose decisions about their
   project while reorganising it.
4. **Update `MEMORY.md`** so every file is indexed, and `CLAUDE.md`'s memory table so it
   names files that exist.
5. **Check the budget binds.** `.ai/manifest.toml` should declare `[memory].orientation`
   (the always-loaded set) and `[memory].budget_bytes`. `init` declares the stubs it
   wrote; add any of *their* orientation documents you just identified. Then run
   `python -m nightshift.gates.run` — `orientation_budget` and `orientation_shape` are
   the two that enforce all of the above.

**If `orientation_budget` fires, trim the documents. Never raise the budget.** A budget
raised to fit what is there can only ever be satisfied, which is the failure it exists to
prevent.

**Do not invent memory files with nothing in them.** A `design.md` that says "decisions
go here" is worse than no file: it costs a read and teaches a session that memory files
are empty. Leave the stub as the stub until there is something true to put in it.

## 6. Sweep the code for what is already stale, and file it as cards

The gates that ship are tuned for a repo that is already clean — they are there to stop
new mess, not to report the accumulated kind. A repo that existed before this install has
accumulated some, and nobody has ever been shown it in one list.

**Findings become cards. Do not fix them now.** Two reasons, and the second is the real
one: a cleanup diff mixed into an install diff is unreviewable, and the maintainer has
not agreed to any of this yet. The one exception is below.

Run these and read the output yourself:

```bash
# Unused functions, classes, methods, imports and variables. Confidence 60, NOT the
# gate's 80 — the gate runs high to stay actionable day to day; this pass runs low
# on purpose, because it is the one time anybody looks at the whole list.
python -m vulture <source_dirs> <entry-point.py> --min-confidence 60

python -m nightshift.gates.run          # what the shipped rules already object to
```

Pass the entry point (`main.py`, `cli.py`, whatever constructs the app) alongside the
package. Leaving it out reports everything only it uses as dead — the application class,
the logging setup, every `if TYPE_CHECKING:` import — and produces a list nobody believes.
Leave `tests/` out: pytest injects fixtures by decorator and no static tool can see it.

**One card per cluster, never one per finding.** Thirty unused symbols in one legacy
module is one card ("retire or revive `legacy/exporter.py`"), not thirty. A card the
maintainer cannot decide in one sitting is a card that sits.

Each card needs a real `## Approach` — what you would actually do, and what you are
unsure of. "Remove the dead code" is not an approach. Say which symbols, whether anything
reaches them dynamically (`getattr`, a registry, a plugin entry point, a template), and
what you would check before deleting.

**The one thing you may fix in place: unused imports.** They are unambiguous, vulture
rates them at 90%+, and the test suite proves it. Say how many you removed. Anything that
could be reached dynamically — an unused function, a class, a method — is a card, because
a static tool cannot see a registry and you have not read this codebase before today.

If the sweep finds nothing, say so plainly. A clean repo is a real result and worth one
sentence; inventing work to look thorough is worse than reporting zero.

## 7. Analyse the repo and file cards

The install is done; the useful part is what the repo needs. Read it — the module
layout, the test suite, `CLAUDE.md`, the obvious rough edges — and file what you find as
cards. This is the maintainer's own instruction: *cards for what to fix, so the user can
decide.*

Rules for this pass:

- **One card per thing.** `Board/README.md` is the card format; the `manage-board`
  skill is the lane contract.
- **A card in `tasks/` needs an `## Approach`** — the paragraph the maintainer reads to
  decide whether it may run. Write it as a real proposal, not a restatement of the title.
- **A finding you cannot scope goes to `Board/inbox/`** as a rough note. Triage turns
  notes into cards later; a half-scoped card in `tasks/` is worse than a note.
- **A finding that needs their choice goes to `needs-decision/`**, with the choice stated
  and the options named.
- **Do not invent work to look thorough.** Five real cards beat twenty speculative ones,
  and every card is attention they have to spend.
- **Do not write gates.** A gate is earned by an observed failure in *this* repo. An
  empty `.ai/gates/` is the correct state on day one, and a card proposing a gate for a
  failure that has not happened yet is the thing the doctrine forbids.

Then run `python -m nightshift.gates.run` once more — `card_schema` validates what you
just wrote — and commit the cards.

## 8. Hand over in four sentences

1. What you installed, and which files got a block appended rather than written.
2. What the checks say now, naming anything still red and why.
3. What you restructured, if anything, and what is in `needs-decision/` waiting on
   them versus in `tasks/` with an approach to review.
4. That gates now run after every edit, that `python -m nightshift.preflight` is
   required before any push, and that the board is optional — the inline half works
   without it.

**The one thing to tell them to do by hand**, because it is a click in another
application and no command you can run will do it: to *see* the board, open the repo
as an Obsidian vault and install the **Base Board** community plugin. *Bases* is core
and renders the two tables; the Kanban view type is Base Board's, and with only Bases
installed Obsidian reports **`Unknown view type: kanban`** on `Board.base`. Say the
error string — it is what they will otherwise search for. Nothing in the framework
reads that view, so a project that never installs it loses nothing but the picture.

Do not close by listing commands for them to run. If something needs running, run it.
