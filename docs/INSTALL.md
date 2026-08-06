# Installing nightshift with an agent

The install instructions for an agent are **the `install-nightshift` skill**, not this
file. Two commands put it in your repo:

```bash
pip install -e <path-to-nightshift>
nightshift bootstrap
```

`bootstrap` writes exactly one file — `.claude/skills/install-nightshift/SKILL.md` — and
nothing else. Then, inside Claude Code in your project:

```
/install-nightshift
```

## Why this file is a pointer and not a copy

It used to be a copy. Two documents carrying the same install steps is the duplication
this framework spends most of its gates preventing: they agree on the day they are
written and drift from the first change afterwards, and the one that drifts is always
the one nobody executes. The skill is the one that gets executed, so the skill is the
one that is true.

If you cannot run `bootstrap` — no shell, a sandbox, reading this on a phone — the same
content is at
[`nightshift/templates/skills/install-nightshift/SKILL.md`](../nightshift/templates/skills/install-nightshift/SKILL.md).
Read it and do what it says; it is written to be followed by an agent, in order.

## What the skill will ask you

Two questions, and only two. Everything else it answers itself with the recommended
default.

1. **The integration branch** — the branch feature work is cut from and merged back
   into. Never guessed, and the origin project is the proof: its integration branch has
   been `dev`, then `development_team`, and is now `test` — while `dev` itself became a
   *forbidden* base, so the branch whose name looks most like the answer is the one that
   would break the runner.
2. **`permission_mode`** — what a dispatched worker may do on this machine. `default` is
   safe and cannot run Bash; `bypassPermissions` is what a code card actually needs and
   is the whole machine, not a sandbox.

## What it does without asking

- Runs `nightshift init` with those answers.
- Commits.
- Runs `nightshift fix`, which runs every check and repairs what failed — up to three
  rounds, never weakening a check to make it pass, never committing.
- Reads your repo and files cards for what it found: an approach to review in `tasks/`,
  a rough note in `inbox/` for anything it could not scope, and a stated choice in
  `needs-decision/` for anything that is yours to make.

## The failures you will actually hit

| Symptom | Cause | Fix |
|---|---|---|
| `git add` warns "LF will be replaced by CRLF" | Windows, and `.gitattributes` arrived after files were staged | `python -m nightshift.normalize_worktree`, once, on this machine |
| gates fail on `line_endings` but `git status` is clean | CRLF already on disk; blob and file normalise the same, so git sees nothing | same command — per-machine, nothing to commit |
| `nightshift fix` refuses to dispatch | `permission_mode` cannot run Bash, so it could not run a gate | `nightshift fix --permission-mode bypassPermissions` — that pass only |
| `doctor` reports `claude-bin` failing | the CLI is not on `PATH` | install it, or set `CLAUDE_BIN`. Only needed for dispatch |
| `preflight` fails on `corrections` | by design — it wants a lesson logged, or an explicit zero | `--no-corrections "fresh install, nothing learned yet"` |
| `preflight` fails on `pytest` | the suite was already failing, or `[tests].dir` is wrong | check `.ai/manifest.toml` against reality |
| `dead_code` says there is nothing to scan | discovery could not infer `[project].source_dirs` | declare the real package(s) in the manifest |

## Starting over

`init` never overwrites, so a half-configured repo cannot be repaired by re-running it.
Undo first:

```bash
nightshift uninstall          # lists what it would remove; changes nothing
nightshift uninstall --yes    # performs it
```

It removes only what the install created, from the `.ai/install.json` receipt. A file the
project already had is not in it and cannot be deleted here. Five files —
`CLAUDE.md`, `.gitattributes`, `.gitignore`, the tier-binding doc and `Board/README.md` —
are appended to rather than written when they already exist, because for those the
content is the requirement; uninstall strips the marked block and keeps the file.

**Before running it with `--yes` in a repo with real work in it**, run it without `--yes`
and read the list. It is scoped, but it is still a delete.
