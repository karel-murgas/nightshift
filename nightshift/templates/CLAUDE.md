# Claude instructions — {{project}}

<!-- Written by `nightshift init`. Everything above the "Project rules" line is the
     framework's half and is the same in every project that installs it; below the line
     is yours. Keep the split, or the next `nightshift doctor` cannot tell what drifted. -->

## How to run

```bash
command-center.bat                    # the Command Center (./command-center.sh on unix)
python -m nightshift.gates.run        # the gate suite (also runs on save, via a hook)
python -m nightshift.preflight        # MANDATORY before push/merge — writes a receipt
python -m nightshift.runner           # dispatch cards from Board/tasks/; run backgrounded
python -m nightshift.reconcile        # inbox notes -> cards; digest: nightshift.digest
python -m nightshift.doctor           # the per-machine preconditions git cannot carry
python -m nightshift.update           # bring this repo's nightshift files up to date
pytest                                # the test suite
```

**The Command Center is the front door.** A local web panel — a launcher, a registry and
a tail, never a chat client. Its *System* page carries the framework's own maintenance:
what this repo's nightshift files are missing or behind on, `doctor`, the gates,
preflight, the `fix` pass. Every button shells out to the command above that it names,
so nothing there is a second implementation of anything.

**The AI-team tooling is an installed dependency, not part of this repo.** A fresh clone
needs it before any gate, preflight or runner command works:

```bash
pip install -e path/to/nightshift
```

**`python -m nightshift.update` after pulling a newer nightshift.** The framework's code
is live immediately (the install is editable), but the files it wrote *into this repo* —
the agent charters, the skills, the board README, the launchers — are copies, and `init`
never overwrites them. `update` reports what moved and what you edited, and writes only
what you never touched; a file that changed on both sides is a conflict it refuses to
resolve on its own (`--diff`, `--take`, `--keep`, `--merge`).

**`python -m nightshift.preflight` before every push, merge or PR.** It runs the doctor
checks, the gates, the audit matrix, the corrections check and the test slice your branch
can affect, then writes a receipt; `nightshift.hooks.preflight_guard` denies the push
without one. `--full-tests` runs everything, `--skip-tests` skips pytest while you iterate
on the rest, `--no-corrections "<reason>"` records an honest zero.

Never hand-roll a parallel pytest invocation in tooling — `nightshift/suite.py` is the one
place that policy lives, and `suite.parallel_args()` is how you ask for it.

## Git workflow

- Work on a branch off the integration branch, and merge back to it.
- **`.ai/manifest.toml`'s `[branches].integration` is the source of truth**, not this line
  and not any other prose. Branch roles migrate; a name written into a doc does not follow.
  Read the manifest rather than trusting a sentence — `nightshift.gates.branch_role_prose`
  exists because exactly that drifted in the origin project, leaving the highest-authority
  instruction file telling every session to commit to a branch the runner refuses to build
  on.
- `forbidden_bases()` derives from that field. A branch it rejects is rejected for a
  reason someone wrote down; do not work around it.

## The board

Cards live in `Board/<lane>/<id>.md`. The lane contract is `Board/README.md`; the
operator skills are `manage-board` and `run-the-runner` in `.claude/skills/`.

`Board/ideas/` is the maintainer's private lane. **No judgment actor may open it** —
`nightshift.hooks.ideas_fence` enforces that, and `nightshift.reconcile` is the one
permitted reader.

## Corrections

`.ai/corrections.log` is one line per correction that would otherwise recur. It ships
empty, and so does its vocabulary: a class arrives with the first entry that needs it.
Append through the `log-a-correction` skill, which carries the rules about when a
correction is systematic enough to be worth the line. Read the distribution with
`python -m nightshift.corrections`.

The rule the whole thing rests on: **the cost of a defect here is the maintainer's
attention.** A defect they catch twice is pure waste, and the only way anyone knows it
happened twice is that the first one was written down.

## Gates

A gate is earned by an observed failure *in this repo*, never adopted because it sounds
prudent. `nightshift` ships the generic ones; yours go in `.ai/gates/` and never move.
An empty `.ai/gates/` is the correct starting state.

Turning a logged correction into a gate is the third step of the loop and the one that
goes wrong quietly: `.ai/recipes/turn-a-correction-into-a-gate.md` is the spine, with the
four failure modes it exists to prevent. Its companion,
`.ai/recipes/verify-before-shipping-a-rule.md`, covers the case where the thing you are
shipping is a rule rather than a check. A third,
`.ai/recipes/write-a-test-that-earns-its-place.md`, is about the tests the preflight
receipt, the push guard and the runner all assume and none of them can inspect.

When a gate is wrong, appeal it rather than deleting it — a `# gate-ok(<gate>): <reason>`
marker, which `gate_appeals` validates and counts. An appeal leaves a record where a
deletion, or a quiet narrowing of the gate's scope, leaves a hole.

<!-- ------------------------------------------------------------------------------- -->
## Project rules

Everything below here is {{project}}'s own. The framework has no opinion about it.

- Add the rules a session must not violate, especially the ones no gate enforces yet.
- Prefer one line per rule with the reason attached. A rule without its reason is the
  first thing someone works around.
- When a rule earns a gate, keep the line and point it at the gate.

### Memory

Memory files live in `.claude/memory/`. Read `.claude/memory/MEMORY.md` at the start of
every conversation and load what it points at.

Keep orientation files small and current; put dated narrative in a companion file that is
loaded on demand. The failure this prevents is measured: an orientation file that
accumulates session history reaches a size where reading it costs more than re-reading
the code, at which point nobody reads either.

| Changed | Update |
|---|---|
| New module or file | `.claude/memory/arch.md` |
| Feature merged or phase completed | `.claude/memory/state.md` |
| Design decision made or changed | `.claude/memory/design.md` |
| New rule from the maintainer | a `feedback_<topic>.md`, listed in `MEMORY.md` |
