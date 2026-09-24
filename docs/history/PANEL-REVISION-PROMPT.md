---
doc_scope: history
---

# Prompt: bring the Command Center up to its approved design

`nightshift.panel` shipped, and it works — but its front end is a 4 KB skeleton against a
design that was agreed in detail and approved before any of it was built. This session is
about the front end. **Read `.claude/plans/dispatch-cost-and-control-panel.md` §3.4 first; it
is the spec, and the approved mockup is at**
`https://claude.ai/code/artifact/70bd0a1e-57f8-4ceb-b2ea-e681ef4161f8` — open it and match it.

## What is already right — do not rebuild any of this

The server and the verbs are done and I do not want them touched beyond the one change named
in §3 below. Verified in the tree before writing this:

- Five pages exist and are correct as a set: `now`, `verify`, `inbox`, `ideas`, `run`.
- Per-card **Dispatch**, **Mark OK** (`/api/verified`), **Promote**, **Review**, classify,
  start the night, freshness refresh/pull, account select, "talk to this one" — all present
  and wired.
- All six board verbs exist in `nightshift.boardcmd`: `reorder`, `verified`, `promote`,
  `note`, `edit`, plus the review drain in `nightshift.drain`.

**So the work is almost entirely `panel_static/app.html` and the `_render_*` functions in
`panel.py`.** Do not add a dependency, a build step, a framework or a CSS library.

## 1. Two endpoints exist and nothing calls them

This is the quickest win and it is pure front end:

- **`/api/reorder`** → `boardcmd reorder`. The panel has no drag-and-drop at all — zero
  occurrences of `draggable` or `dragstart` in the tree. **Tonight** and **Ideas** rows must
  be draggable to reorder within their lane, renumbering as they go, POSTing the new
  `kanban_order`. This shares Obsidian's own ordering field on purpose, so dragging in either
  place is the same act.
- **`/api/edit`** → `boardcmd edit`. Today you can only *create* a note or an idea. Every
  inbox note and every idea needs an **Edit** button opening an inline `<textarea>` with the
  current body, saving through that endpoint. Ideas especially — I want to sharpen a half-
  thought in place, not just add new ones.

## 2. Selecting what a run takes

Missing entirely: there are no checkboxes and no slider anywhere. On **Tonight**:

- A checkbox per queued card. These are the selection.
- A **"take first N"** slider that *sets* those checkboxes rather than selecting in parallel —
  one selection model, two ways to drive it. Two competing selections make it unclear what a
  button will actually run.
- The run button acts on the ticked set.

## 3. The one backend change

`/api/night` currently spawns the whole runner with no arguments, so a subset cannot be
expressed. Extend it to accept the selected card ids and run those, in the panel's order.
Reuse the runner's existing per-card path rather than growing a second dispatch route, and
keep the money-rule guard exactly where it is.

## 4. Layout — it is a different shape from the design

Measured against the mockup:

- **A fixed left rail, not a top nav.** Page names stacked, each with its count, and a small
  machine block at the bottom: hostname, capabilities, the branch and the framework SHA.
- **A status rail across the top of the content**, in two columns: the live run on the left
  (card id, worker, model, attempt, elapsed, turns, then the five phases as connected pills
  with the current one filled), the allowance meters on the right as thin bars with reset
  times and the paid-overage chip.
- **One tally line under the run**: `3 landed · 1 back in tasks · 3 queued`, with a **Run
  detail** button that jumps to the Run page. The full roster stays on its own page — I tried
  it in the rail and it blocked every other page's content.
- **Sections are full width.** `main` is capped at `60em`, which wastes the screen for what is
  a register you scan.
- **Rows are a grid, not `<li>`**: drag grip · checkbox · marker · body · actions, with the
  actions right-aligned. The body is card id in mono green, title in prose, then a meta line
  of small mono facts (worker, verify mode, diff stat, chips like `requires gpu-box`).
- **Group labels inside sections** — `Combat — 4`, `Hub and menus — 2` on Verify (grouped by
  `surface:`), `Cards the night cannot take` on Now.
- **The Run page** carries the roster as a real table (card · lane it landed in · wall time ·
  cost · what the reviewer said), then *Not taken* with a reason per card, then earlier runs.
- **An amber callout** when a card is stuck in `review/` with no reviewer scheduled.

## 5. Style — close, but the two decisions that matter got lost

Keep black and green. Two rules make it readable rather than a parody:

- **Green is the data colour, not the text colour.** Prose is pale grey-green; phosphor green
  is only for numbers, statuses, card ids and anything clickable. Warnings are amber and
  refusals dim red — so *"paid overage enabled"* can never render as though it were fine.
- **`--data: #39ff8f` is too hot.** Use `#4FE88A` on a `#070A08` ground with `#C4D2C6` prose;
  the current values are a neon-on-black that is tiring to read for long.

Smaller, all measured:

- Square hairline borders, no `border-radius` — this should read as equipment, not as cards.
- `font-variant-numeric: tabular-nums` everywhere digits line up in a column.
- Monospace for labels *and* data, sans for prose only; uppercase labels get letter-spacing.

## What not to do

Do not redesign anything, do not rename the pages, do not touch `boardcmd` or `drain`, and do
not add a dependency. If something in the mockup genuinely cannot work against the real data,
say so and leave it — do not substitute your own idea for it silently.

`python -m nightshift.preflight` before you push, in whichever repo you are pushing. This is a
framework change, so its tests live in `nightshift`.

---
