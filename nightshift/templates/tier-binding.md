# Tier binding

Which model each card tier dispatches at. `.ai/manifest.toml`'s
`[tiers].binding_doc` points here, and `nightshift.tiers` **parses the fenced
block below** rather than restating it anywhere in code.

That indirection is the whole design. The binding may be written down in exactly
one place, and no caller ever names a model — a dict inside the dispatcher would
be a second place, and it would go stale the day a tier is rebound to a local
runtime. The failure this was written after was a correct table nothing read.

## The block

Edit the block, not the prose. One `<tier> = <model alias>` line per tier; both
tiers must be bound, because `card_schema` accepts both and a card written at an
unbound tier can never dispatch.

```tier-binding
worker = sonnet
lead = opus
```

## What each tier is for

**`worker`** — executes one card end to end against stated acceptance criteria:
implement, test, wire up, close out. Bounded work with a written definition of
done.

**`lead`** — judgment about work rather than the work itself: triage turning a
rough note into a card, review deciding whether a finished diff needs the
maintainer's decision. Reads more context, writes less.

A dispatcher never guesses a model. If this document goes missing or the block
is unparseable, `nightshift.tiers` raises rather than falling back — silently
running a card at the wrong tier is the outcome that costs money and looks fine.
