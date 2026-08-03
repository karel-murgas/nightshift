# Installing nightshift — the version you can hand to Claude

Point Claude Code at this file from inside the project you want to set up:

> *Read `../nightshift/docs/INSTALL.md` and install nightshift in this repo.*

Everything below is written for an agent doing the install with the operator in
the room. A human can follow it too, but the README's numbered walkthrough is
shorter if that is all you want.

---

## Before you touch anything

Establish these four facts and **say them back to the operator** before running a
command that writes. Getting any of them wrong produces a repo that looks
configured and is not.

| Fact | How to establish it | If you cannot |
|---|---|---|
| The project is a git repo | `git rev-parse --show-toplevel` | stop — nothing here works without one |
| Python is 3.11+ | `python --version` | stop — `tomllib` is stdlib from 3.11 |
| Which branch work merges into | **ask** — `git branch -a` is context, not an answer | stop and ask. It is never guessed |
| Where the nightshift checkout is | `ls ../nightshift` or ask | stop and ask |

**The integration branch is the one thing that is never inferred.** Not the
stable branch — the one feature branches are cut from and merged back into. If
the project has only `main`, say so and ask whether to create one; do not pick.

---

## Step 1 — install the package

```bash
pip install -e <path-to-nightshift>
nightshift --help
```

If `nightshift --help` fails, the package went into a different interpreter than
the project uses. Resolve that before continuing — check `pip -V` against
`python -V`.

## Step 2 — run the interview

```bash
nightshift init --integration <branch>
```

**Let the operator answer the questions.** Do not pipe input to it and do not use
`--non-interactive` unless they ask: the four questions are the point of the
command, and two of them are decisions only they can make.

If you are in a context where you cannot pass their keystrokes through, run
`nightshift init --integration <branch> --dry-run` first, show them the plan,
relay the four questions yourself, and then run it with the answers stated:

```bash
nightshift init --integration <branch> --permission-mode <their answer> --non-interactive
```

### What the four questions mean, so you can answer follow-ups

1. **Integration branch** — already stated on the command line.

2. **`permission_mode`** — what a dispatched worker may do on this machine.
   - `default` — cannot run Bash. Code cards will stall. **Recommend this for a
     first install**; it lets them see the board and the gates with nothing at
     risk.
   - `acceptEdits` — file edits without prompting, still no Bash.
   - `bypassPermissions` — what a code card actually needs, and **it is the whole
     machine, not a sandbox**. If they choose it, say the warning out loud rather
     than letting the prompt text carry it alone.

3. **`[memory].budget_bytes`** — a cap on the docs an agent loads every session.
   Recommend the offered 100 KB. If they ask why it is not measured from their
   repo: a budget that fits what is already there can only ever be satisfied,
   which is the failure the gate exists to catch.

4. **`[layering].forbid`** — import-direction rules discovery found the tree
   already satisfies. Recommend **no** on a first install. Each row is a claim
   about their architecture, and they should read them before adopting them
   (`nightshift init --layering` prints the list any time).

## Step 3 — follow the printed checklist

`init` ends with a numbered list of commands. Run them in order and report each
result. Do not skip the commit — the rest assumes a clean tree.

```bash
git add -A && git commit -m "nightshift init"
nightshift doctor
python -m nightshift.gates.run
python -m nightshift.preflight
```

Expected: `doctor` all `[OK]` or `[SKIP]`, gates green, preflight green with a
receipt written.

### The failures you will actually hit

| Symptom | Cause | Fix |
|---|---|---|
| `git add` warns "LF will be replaced by CRLF" | Windows, and `.gitattributes` landed after files were already staged | `python -m nightshift.normalize_worktree`, once, on this machine |
| gates fail on `line_endings` but `git status` is clean | CRLF already on disk; the blob and the file normalise the same, so git sees nothing | same command — it is per-machine and produces nothing to commit |
| `doctor` reports `claude-bin` failing | the CLI is not on `PATH` | install it, or set `CLAUDE_BIN`. Only needed for dispatch |
| `preflight` fails on `corrections` | by design — it wants a lesson logged, or an explicit zero | `python -m nightshift.preflight --no-corrections "fresh install, nothing learned yet"` |
| `preflight` fails on `pytest` | their suite was already failing, or `tests.dir` is wrong | check `.ai/manifest.toml`'s `[tests].dir` against reality |

## Step 4 — hand it over

Tell them, in this order:

1. **Gates now run after every edit you make.** That is the inline half working;
   they do not have to use the board at all to benefit.
2. **`python -m nightshift.preflight` before every push.** A hook refuses the
   push without a receipt, so this is the command they will type most.
3. **The board is optional and additive.** `Board/README.md` is the card format.
   `.claude/skills/manage-board/` and `.claude/skills/run-the-runner/` are the
   two skills that explain driving it.
4. **What they did *not* get:** an earned gate suite. The audit matrix and the
   corrections log ship empty on purpose. Rules arrive from their own observed
   failures, never by import.

---

## Starting over

`init` never overwrites, so a half-configured repo cannot be repaired by
re-running it. Undo first:

```bash
nightshift uninstall          # lists what it would remove; changes nothing
nightshift uninstall --yes    # performs it
```

It removes only what the install created, from `.ai/install.json` — the receipt
`init` writes naming its own files. A document the project already had (`CLAUDE.md`,
`.gitattributes`, `.claude/memory/arch.md`, `docs/tier-binding.md` are the four that
collide) was kept by `init`, is not in the receipt, and cannot be removed by this.
With no receipt it falls back to content comparison and keeps anything that differs.

It also refuses to delete a corrections log with real entries, and un-merges the hook
entries from `.claude/settings.json` while leaving every other key alone. The package
stays installed; `pip uninstall nightshift` is a separate and usually unnecessary step.

**Before running it with `--yes` in a repo with real work in it**, run it without
`--yes` and read the list to the operator. It is scoped, but it is still a delete.
