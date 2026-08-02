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
| 5. `nightshift init` + `doctor` | half — `doctor.py` exists and preflight calls it; `init` and the CLI entry points do not |
| 6. this README, for real | not started |
| 7. stand up in a second project | not started |
| 8. ship to one colleague | not started |

**Not self-bootable yet, and the gap is specific.** The Python half is complete and
standalone: nothing in the package imports back into a consuming project. What is
*not* here is everything under `.claude/` — the hook wiring in `settings.json`, the
agent charters the runner dispatches by name, the operator skills, and the
`CLAUDE.md` prose two gates read. A new project cannot obtain those by installing
this package; today they are copied by hand. Steps 5–7 are what closes that.

## Install

```
pip install -e path/to/nightshift
```

A consuming project then keeps `.ai/manifest.toml` (see `nightshift/manifest.py` for
the schema), its own gates under `.ai/gates/`, its own `.ai/corrections.log`, its
own `Board/`, and its own `.claude/`.

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
  worker_prompt.py  run_record.py  tiers.py  limits.py  textio.py  gitmerge.py
  gates/          # base.py, run.py, corpus.py, doc_scan.py + 19 shipped gates
  hooks/          # 7 PreToolUse / PostToolUse hooks
tests/            # 357 tests, every one against a synthetic project
```

Every module is standalone: the package reaches a consuming project only through
`.ai/manifest.toml` and the paths that manifest declares. `bridge.py` — the seam
that used to import back into the project's `.ai/` — was deleted along with the
last module it reached for.

The tests are deliberately written against a project called `myapp`, not against
Dungeoneer. A framework suite that only ever proves the machinery works on the repo
it was extracted from would not catch a manifest field being ignored, which is
exactly what it caught during step 4.
