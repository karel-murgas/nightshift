---
doc_scope: history
---

# The dispatch-cost programme is complete

There is no seeded prompt to copy this time. `.claude/plans/dispatch-cost-and-control-panel.md`
items 1–14 are all done, merged and pushed in both repos; nothing is on a branch. The last
piece, item 14 (the Command Center panel, `nightshift.panel`), shipped 2026-08-15 — read the
plan doc's own §6 item 14 and the "Worth knowing, not blocking" section at its end before
touching any of this, rather than re-deriving it.

**To use the panel:** run `command-center.bat` at this repo's root (or `python -m
nightshift.panel` from anywhere inside it), which opens `http://127.0.0.1:8765/now` in the
default browser.

## What is genuinely still open

Both are recorded in the plan doc's own "Worth knowing, not blocking" section — read that
before starting on either, since each has a reasoned trade-off behind why it wasn't just done:

1. **The `[[accounts]]` config gap.** `.ai/manifest.toml` has no `[[accounts]]` rows yet, so
   the panel's account selector has nothing to iterate — today there is only the ambient
   account plus the live `hasExtraUsageEnabled` veto (which works and is tested on its own).
   Filling this in is a config edit naming each machine's label, `config_dir` and `dispatch`
   stance — Karel's to make, not a code change.
2. **Chore batch phase 2/3 remain unproven on real work.** Nothing has yet survived phase 1
   to reach the "one suite run instead of eight" merge — the whole economic claim of §3.5.

## If you are picking new work instead

There is no next seeded card. Check `Board/needs-decision/` and `Board/tasks/` for whatever
is queued, or run the panel's Now page to see it rendered. `python -m nightshift.freshness`
still answers "is the sibling nightshift checkout current" in one line before you start
anything that touches it.
