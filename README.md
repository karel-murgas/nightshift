# nightshift

The machinery for running a board-driven AI team against a Python repo — the gate
runner, the hooks, the board and card schema, the preflight, the corrections log,
the overnight runner. **You get the machinery for earning gates, not an earned gate
suite:** a new repo starts with the generic gates, an empty audit matrix and an
empty corrections log, and adds rules as its own failures earn them.

> **This README is a placeholder.** The real one — page-one quickstart, then
> reference, with generated blocks checked by a `readme_generated` gate — is
> step 6 of `07_portability.md` §8. Steps 1 and 2 are what exists so far.

## Status

| Step | State |
|---|---|
| 1. repo, `pyproject.toml`, `nightshift/manifest.py` | done |
| 2. the zero-coupling modules move | done |
| 3. the 16 universal gates and the 4 hooks | not started |
| 3b. the three i18n gates behind an adapter | not started |
| 4. the constant-holders behind the manifest | not started |
| 5. `nightshift init` + `doctor` | not started |
| 6. this README, for real | not started |
| 7. stand up in a second project | not started |
| 8. ship to one colleague | not started |

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
  bridge.py       # TEMPORARY — imports modules still living in the project's .ai/
  textio.py       # LF-pinned text writes
  limits.py       # recognising the usage-limit wall
  tiers.py        # the tier -> model lookup
  run_record.py   # a run's structured account of itself
  corrections.py  # the corrections log reader
  preflight.py    # the mandatory pre-merge check
  merge_check.py  # does this card's branch merge clean, gated, tested?
  gates/
    base.py       # the Violation type every gate returns
    run.py        # gate discovery and the runner
```

`bridge.py` is a seam, not a feature. `preflight` and `merge_check` still reach
back into the consuming project's `.ai/` for `suite`, `branches`, `board` and
`runner`, because those modules do not move until step 4. Until then this package
is not standalone: it runs against a project that still carries them.
