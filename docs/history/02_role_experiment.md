---
doc_scope: history
---

# Doc 02 — Multi-role experiment: `hand_razors` (2026-07-21)

First feature built by narrow cooperating roles. **Shipped and merged** to
`development_team`; Karel play-tested it and it works. This doc records what the *process*
did, which was the actual deliverable.

## Setup

One triage pass (Claude/Opus, with Karel) produced `Board/done/hand-razors.md`.
Then four roles, each given **only its own slice** of the previous role's handoff:

1. **implementer** — catalog, settings, activation, combat path
2. **i18n** — the two perk keys, en/cs/es          ┐
3. **UI** — help-catalog entry + shop verification  ├ run in parallel
4. **test-writer** — tests                          ┘

All four on **Opus** — a cost error, see below.

## Cost

| Role | Tokens | Tool calls |
|---|---|---|
| implementer | 96k | 40 |
| test-writer | 84k | 28 |
| UI | 54k | 26 |
| i18n | 47k | 19 |
| **total** | **~280k** | 113 |

Solo estimate for the same feature: 60–80k. So **~3–4× more expensive** — but that number
is unfair to the architecture, because two separate mistakes inflated it (below).

## What worked

- **Three roles independently caught an error in the card.** It claimed razors "inherit all
  melee perk interactions including `protocol_sword`". False: `protocol_sword`'s bonuses
  *and* its untrained penalty both key on `weapon.id == "energy_sword"`. The implementer
  found it, the UI role refused to write it into help text, the test-writer confirmed there
  was nothing to assert. **Claude's error, caught three times, unprompted.**
- **Independent verification beat code-reading.** The UI role ran the shop overlay headlessly
  rather than reasoning about it — the only reason we know the missing icon makes the HUD
  hotbar fall back to a 7-character text abbreviation.
- **Test quality exceeded solo work.** 45 tests, using the real `GameScene` methods. The
  test-writer also volunteered that `test_trap_perk.py` mirrors production logic instead of
  calling it — a pre-existing test-quality problem it had no obligation to report.
- **Scope discipline was perfect.** Every role refused to act outside its scope, every time.
- **Emergent terminology alignment** — the i18n role found the UI role's Czech term already
  on disk and matched it. Real coordination through the artifact, with nobody instructing it.

## What failed — and these are the valuable part

### 1. Concurrent roles produce *false* bug reports (worst finding)

The test-writer reported the `es` dict was missing both razor keys, with correct-sounding
reasoning about consequences. **It was wrong.** It read `i18n.py` while the i18n role was
still writing it and reported a stale snapshot as a defect.

This is worse than a lost edit, because a lost edit shows up as a missing key while a stale
read shows up as a *plausible finding that survives into review*. Trusting it would have
meant "fixing" a non-bug and duplicating keys.

**Rule:** a role must never read a file another in-flight role owns. Exclusive ownership +
a barrier between waves, or a re-verify pass after all roles land.

### 2. Nobody owned the spine tail

Three roles each flagged spine work outside their scope and correctly declined it:
`state.md`, `ref_i18n.md`, the icon asset. All three were still undone at the end. Every
role behaved correctly and the work still fell through. → needs a **closer** role, or gates.

### 3. Roles didn't read the project's own terminology file

Nobody read `ref_i18n.md`, because nobody was told to. Result: **an English word shipped
inside a Czech string** — `help_catalog.melee.4.5` = "všechny **melee** bonusy". This is
*exactly* the failure class Karel named from his own experience. Caught only by Claude
reading the memory file during review.

Related discovery: `ref_i18n.md`'s "locked" Czech term `útok na blízko` is **contradicted by
the codebase** — actual usage is `zblízka` in 17 keys vs `na blízko` in 6. The memory file
is stale. A pre-existing leak also survives at `help.desc.shoot` ("melee nebo na dálku") —
left for Karel to rule on.

### 4. Massive duplicated discovery across the process boundary

The test-writer's own words: it *"reverse-engineered the attribute set from the source"* —
rediscovering `catalog.py`, `weapon.py`, `game_scene.py`, `damage.py`, `action_resolver.py`,
all of which the implementer had just read. ~50–70k of pure re-discovery, spent because a
process boundary was placed between two jobs that share nearly all their context.

### 5. Wrong model tier

All four ran on **Opus**. Implementation should be Sonnet; Opus belongs on triage and review.

## Gates this experiment earned

| Gate | Evidence |
|---|---|
| global key parity `set(en)==set(cs)==set(es)` | the only thing that catches lost edits from concurrent writers — a failure mode now demonstrated, not hypothesised |
| cs/es value byte-identical to en | needs an allowlist — measured 14 cs / 9 es legitimate matches (`skills.xp_format`, `hud.exposure_level`, `cheat.*`) |
| English-loanword scan over cs/es | would have caught #3. Must exclude `{placeholders}` and deliberate keeps (`heat`, es `planta`) or it is ~50% false positives |
| `i18n.py` touched ⇒ `ref_i18n.md` touched | audit row 6; would have caught #2 |

## Verdict

Quality: **better than solo.** Cost: **worse than solo**, though two fixable mistakes
(Opus everywhere, boundary in the wrong place) account for most of the gap.

The process findings — not the perk — were the return on this run.
