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

## 5. Analyse the repo and file cards

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

## 6. Hand over in four sentences

1. What you installed, and which files got a block appended rather than written.
2. What the checks say now, naming anything still red and why.
3. What is in `needs-decision/` waiting on them, and what is in `tasks/` with an
   approach to review.
4. That gates now run after every edit, that `python -m nightshift.preflight` is
   required before any push, and that the board is optional — the inline half works
   without it.

Do not close by listing commands for them to run. If something needs running, run it.
