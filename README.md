# nightshift

The machinery for running a board-driven AI team against a Python repo — the gate
runner, the hooks, the board and card schema, the preflight, the corrections log,
the overnight runner. **You get the machinery for earning gates, not an earned gate
suite:** a new repo starts with the generic gates, an empty audit matrix and an
empty corrections log, and adds rules as its own failures earn them.

> **This README is a placeholder.** The real one — page-one quickstart, then
> reference, with generated blocks checked by a `readme_generated` gate — is
> step 6 of `07_portability.md` §8.

## Status

| Step | State |
|---|---|
| 1. repo, `pyproject.toml`, `nightshift/manifest.py` | done |
| 2. the zero-coupling modules move | done |
| 3. the universal gates and the hooks | done |
| 3b. the three i18n gates behind an adapter | done |
| 4. the constant-holders behind the manifest | done — 2026-08-02, `bridge.py` deleted with them |
| 5. `nightshift init` + `doctor` | done — 2026-08-02, with `templates/` for the `.claude/` half |
| 6. this README, for real | not started |
| 7. stand up in a second project | not started |
| 8. ship to one colleague | not started |

## Install

```
pip install -e path/to/nightshift
nightshift init --integration <your-integration-branch>
```

That is the whole install. `init` discovers what it can, asks about the one thing it
must never guess, and writes the rest: the manifest, the board, the four core
charters, the three operator skills, the hook wiring, `CLAUDE.md`, memory stubs, an
empty corrections log and a `.gitattributes`. Nothing existing is overwritten, so
running it again is how a half-configured repo gets what it is missing.

Measured on an empty repo: green gate suite and one dispatchable card, no hand
editing. Then `nightshift doctor` for the per-machine preconditions git cannot carry,
and for drift between the manifest and the tree.

**The one warning, and it is on page one because not reading it has a cost you cannot
undo:** `permission_mode: bypassPermissions` in `.ai/hosts.json` is what a code card
needs to run, and *it is the whole machine, not a sandbox.* A dispatched worker under
it can do anything you can. `init` will never set it for you.

A consuming project owns `.ai/manifest.toml` (schema: `nightshift/manifest.py`), its
own gates under `.ai/gates/`, its own `.ai/corrections.log`, its `Board/` and its
`.claude/`.

## What is here now

```
nightshift/
  manifest.py     # the per-project config: schema, load, validate
  branches.py     # which branch is integration, which are forbidden bases
  board.py        # lanes, card read/write, the board commit
  suite.py        # the ONE home for which test slice runs, and how
  runner.py       # the overnight dispatcher
  night.py        # the unattended entry point around it
  reconcile.py    # inbox notes -> cards
  digest.py       # the morning report
  stale_sweep.py  # which docs are due a staleness check
  preflight.py    # the mandatory pre-merge check
  merge_check.py  # does this card's branch merge clean, gated, tested?
  doctor.py       # the preconditions git cannot carry
  audit.py        # the rule matrix, checked against the tree
  corrections.py  # the corrections log reader
  discover.py     # what a repo can be asked about itself
  init.py         # stand it up: discover, confirm, write
  cli.py          # the `nightshift` command — init and doctor, nothing else
  normalize_worktree.py  # the per-machine CRLF remedy line_endings names
  worker_prompt.py  run_record.py  tiers.py  limits.py  textio.py  gitmerge.py
  gates/          # base.py, run.py, corpus.py, doc_scan.py + 19 shipped gates
  hooks/          # 7 PreToolUse / PostToolUse hooks
  templates/      # what init writes into a project — see templates/README.md
tests/            # 416 tests, every one against a synthetic project
```

Every module is standalone: the package reaches a consuming project only through
`.ai/manifest.toml` and the paths that manifest declares. `bridge.py` — the seam
that used to import back into the project's `.ai/` — was deleted along with the
last module it reached for.

The tests are deliberately written against a project called `myapp`, not against
Dungeoneer. A framework suite that only ever proves the machinery works on the repo
it was extracted from would not catch a manifest field being ignored, which is
exactly what it caught during step 4.
