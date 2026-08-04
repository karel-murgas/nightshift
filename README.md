# nightshift

The machinery for running a board-driven AI team against a Python repo: the gate
runner, the hooks, the board, preflight, the corrections log, the overnight
dispatcher. **You get the machinery for earning gates, not an earned gate suite**
— a new repo starts with the generic gates, an empty audit matrix and an empty
corrections log, and adds rules as its own failures earn them.

**Requirements:** Python 3.11+, a git repo, and the `claude` CLI on your `PATH`
if you want unattended runs. That is all.

<!-- stale-ok: `.ai/hosts.json` and the other paths below are files `nightshift
     init` writes into a CONSUMING project. This package is not its own consumer,
     so none of them resolve here. -->
## Install — three commands, and an agent does the rest

The framework is meant to be operated by an agent, so the install is too. There are
three things a human types, and then Claude drives.

```
git clone https://github.com/karel-murgas/nightshift.git   # next to your project
cd your-project && pip install -e ../nightshift
nightshift bootstrap
```

`bootstrap` writes exactly one file — `.claude/skills/install-nightshift/SKILL.md` —
and nothing else. Then, inside Claude Code in your project:

```
/install-nightshift
```

It establishes the four facts that have to hold, asks you **two questions** (the
integration branch, and what a worker may do on this machine), runs the installer,
commits, then **runs the checks and fixes what they report** — and files a card in
`Board/needs-decision/` for anything that needs your judgment instead of guessing. You
read a diff at the end.

That last part is the point. A first install is usually red in a place or two: a
`source_dirs` that discovery could not infer, a corrections check that wants an honest
zero. Reading gate output and repairing what it names is the job this whole framework
exists to give to an agent, and an install that ends by printing that work as homework
is the framework declining to use itself.

> **No `claude` on this machine?** The manual path below still works, and
> `nightshift fix --dry-run` prints the diagnosis and exactly what would have been
> asked — including the list of things a fix pass may not do.

<details>
<summary><b>The manual install, step by step</b> — same thing, typed by hand</summary>

### 1. Get the code

```
git clone https://github.com/karel-murgas/nightshift.git
```

Put it next to the project you want to use it in, not inside it. It stays a
separate repo so a fix you make while using it is a fix for every project.

### 2. Install it into the same Python your project uses

```
cd your-project
pip install -e ../nightshift
```

`-e` is deliberate: while this is still changing, an editable install means a
`git pull` in `../nightshift` is live in every project at once, with no
reinstall. Check it worked:

```
nightshift --help
```

### 3. Decide one thing before you start

**Which branch does finished work merge into?** Not your stable branch — the
one you cut feature branches from and merge them back to. `dev`,
`development`, `integration`, whatever you call it. If you only have `main`,
make one now; `init` will not invent it, and nothing can dispatch without it.

### 4. Run the installer

```
nightshift init --integration your-branch-name
```

It asks **four questions**, each with a recommended answer you can accept by
pressing Enter:

| # | It asks | Say this if unsure |
|---|---|---|
| 1 | Which branch does work merge into? | the one from step 3 |
| 2 | What may a dispatched worker do on this machine? | **`default`** — safe, and lets you look around before anything can run commands |
| 3 | Cap the size of the always-loaded docs? | **100 KB** — a round number, not measured from your repo |
| 4 | Pin any architecture rules it found? | **no** — read them later, adopt them when you agree |

Then it writes ~27 files: the manifest, the board, four agent charters, three
operator skills, the hook wiring, a `CLAUDE.md`, memory stubs, an empty
corrections log and `.gitattributes`. **Nothing existing is ever overwritten.**

It finishes with two commands: commit, then `nightshift fix`. Step 6 is that
second one.

### 5. Commit it

```
git add -A && git commit -m "nightshift init"
```

The board and the manifest are meant to be tracked, and the next steps assume a
clean tree.

<!-- stale-ok: `.ai/hosts.json` is a consuming project's per-machine config, written by
     `init` into the repo being configured. This package is not its own consumer. -->
### 6. Commission it

```
nightshift fix --permission-mode bypassPermissions
```

Runs the doctor, the gates, the audit matrix and your test suite, then dispatches an
agent to fix what failed — up to three rounds, stopping early if a round changes
nothing. It never commits, never weakens a check to make it pass, and files a card in
`Board/needs-decision/` for what needs your judgment. `--dry-run` shows the diagnosis
and the prompt without dispatching.

The elevation is for that pass only; your standing `permission_mode` in
`.ai/hosts.json` is unchanged.

If you would rather do it yourself, that is the same three commands the fix pass runs:

```
nightshift doctor                  # this machine: line endings, claude on PATH, host config
python -m nightshift.gates.run     # the gate suite
python -m nightshift.preflight     # gates + your tests + a receipt
```

</details>

<!-- stale-ok: `.claude/settings.json` is the consuming project's own settings file,
     which `init` merges hook entries into and `uninstall` merges them out of. Not a
     file of this package's. -->
### Starting over

`init` never overwrites, so a half-configured repo cannot be fixed by running it
again. Undo it instead:

```
nightshift uninstall          # lists what it would remove, changes nothing
nightshift uninstall --yes    # does it
```

It removes only what `init` wrote, leaves your own files alone, keeps a
corrections log that has real entries in it, and un-merges its hook entries from
`.claude/settings.json` without touching your other settings. The package stays
installed — you do not need to `pip uninstall` to try again.

<!-- stale-ok: `Board/README.md` is written into the CONSUMING project by `init`
     (from `templates/board/README.md`). This package has no board. -->
## Then what?

Gates now run themselves after every edit Claude makes. That is the whole of the
inline half, and you can stop here and still be better off.

For the overnight half: write a card into `Board/tasks/` (`Board/README.md` has
the shape), then

```
python -m nightshift.runner --dry-run                   # what would dispatch, and why not
python -m nightshift.runner --card <id> --max-cards 1    # one named card, then stop
```

`--dry-run` first, always — it changes nothing and does not mind a dirty tree.
Then one card by name before an unattended run: a wrong first move here costs
tokens, not time.

<!-- stale-ok: `.ai/hosts.json` is a consuming project's per-machine config, written
     by `init` into the repo being configured — never into this one. -->
## The one warning

**`permission_mode: bypassPermissions` in `.ai/hosts.json` is what a code card
needs to run, and it is the whole machine, not a sandbox.** A dispatched worker
under it can do anything you can. `init` asks before setting it and defaults to
the safe answer, so you only get it by choosing it — on the machine you meant it
for, having read this paragraph.

---

## Reference

- [What is here](#what-is-here)
- [The board](#the-board)
- [Triage by hand](#triage-by-hand)
- [The runner, in full](#the-runner-in-full)
- [`hosts.json`](#hostsjson)
- [Budgets and stop conditions](#budgets-and-stop-conditions)
- [The digest](#the-digest)
- [The corrections log](#the-corrections-log)
- [Gates: the shipped set, writing your own, and appeals](#gates-the-shipped-set-writing-your-own-and-appeals)
- [The manifest](#the-manifest)
- [The staleness sweep](#the-staleness-sweep)
- [What you do not get](#what-you-do-not-get)
- [Troubleshooting](#troubleshooting)
- [Removing it](#removing-it)

### What is here

```
nightshift/
  manifest.py       # the per-project config: schema, load, validate
  branches.py       # which branch is integration, which are forbidden bases
  board.py          # lanes, card read/write, the board commit
  suite.py          # the one home for which test slice runs, and how
  runner.py         # the overnight dispatcher
  night.py          # the unattended entry point around it
  reconcile.py      # inbox notes -> cards
  digest.py         # the morning report
  stale_sweep.py    # which docs are due a staleness check
  preflight.py      # the mandatory pre-merge check
  merge_check.py    # does this card's branch merge clean, gated, tested?
  doctor.py         # the preconditions git cannot carry
  audit.py          # the rule matrix, checked against the tree
  corrections.py    # the corrections log reader
  discover.py       # what a repo can be asked about itself
  init.py           # stand it up: discover, confirm, write
  readme_gen.py     # this file's generated blocks — see below
  cli.py            # the `nightshift` command: init and doctor, nothing else
  gates/            # base.py, run.py, corpus.py, doc_scan.py, scope.py + the gates
  hooks/            # PreToolUse / PostToolUse hooks
  templates/        # what `init` writes into a project — templates/README.md
tests/              # every test against a synthetic project, never this repo's own
.ai/                # this package's own manifest — it is gated by itself
```

Every module reaches a consuming project only through `.ai/manifest.toml` and
the paths that manifest declares — there is no back-reference from core into
project code. A consuming project keeps only `.ai/manifest.toml`, its own
`.ai/gates/`, its own `.ai/corrections.log`, `.ai/recipes/`, its `Board/` and
its `.claude/`.

**This package is one of the repos its own gates run against.** It was not,
until 2026-08-02, and that single gap produced a cluster of findings in one
review: five gates were still scoped to `.ai/` — the directory 16,800 lines had
moved *out* of — and two of them had been reporting green while reading nothing
at all. A scope is a question about the manifest here, never a literal, and
`tests/test_self_gating.py` is what keeps it that way. A repo with no `Board/`
never dispatches, so `doctor` skips the three preconditions that are about
dispatching rather than failing them; that is how the framework can be its own
subject without being its own consumer.

### The board

One card = one markdown file. One state = which directory it is in — no
database, no daemon, resumable from disk alone after a reboot.

```
inbox/ → tasks/ → review/ → testing/ → done/
            ↕                             ↘ failed/
      needs-decision/
```

<!-- generated:board-lanes -->
| Lane | Order |
|---|---|
| `inbox` | 1 |
| `tasks` | 2 |
| `needs-decision` | 3 |
| `review` | 4 |
| `testing` | 5 |
| `done` | 6 |
| `failed` | 7 |
<!-- /generated:board-lanes -->

`inbox/` is where a raw note becomes an actionable card; `tasks/` is
dispatchable; `needs-decision/` is a **success state**, not a failure — a card
parks there carrying a `## Question` section (what was attempted, what is
ambiguous, the candidate answers and what each implies), and answering it is
how it moves on. `review/` and `testing/` are gates-green work waiting on a
person. `done/` and `failed/` are the archive — nothing is deleted, because the
self-improvement loop reads them as evidence.

Move a card by dragging it in whatever Kanban view you point at `Board/`, not by
moving the file — a manual move leaves the old `state:` frontmatter behind,
which reads as a drag-in-progress and gets pulled back. `python -m
nightshift.reconcile` is what makes the folder catch up with a `state:` edit;
it reports by default, `--apply` performs, `--commit` commits.

`init` writes `Board.base`, an Obsidian view of those lanes: a Kanban plus a
Live and an Archive table. It needs **two** plugins and the split trips people
up — *Bases* is core (Obsidian 1.9+) and renders the tables, while the Kanban
view type comes from the **Base Board** community plugin. With only the first,
Obsidian reports `Unknown view type: kanban`. Nothing in the framework reads
that file, so this is purely so a human can see the board.

### Triage by hand

A note dropped in `inbox/` does not need frontmatter — that is what triage adds.
Ask an agent running the `triage` charter to turn one `inbox/` note into a card;
it reads the note, the board and enough of the codebase to scope the work, and
either writes a `tasks/`-ready card or a card parked in `needs-decision/` with
an explicit question. It never invents an answer to resolve an ambiguity itself
— parking on a question is the correct outcome of an honest triage, not a
shortfall.

<!-- stale-ok: this section documents a CONSUMING project's runtime artefacts —
     `.ai/runs/status.json`, `.ai/STOP`, `Digest.md`. They are written by a run, in
     the repo being run against, and by construction none of them exists in this
     package's own checkout. Naming them is what the section is for. -->
### The runner, in full

<!-- generated:runner-flags -->
| Flag | Meaning |
|---|---|
| `--dry-run` | report what would be dispatched and why not; no LLM, no writes |
| `--status` | print where a run currently is (card, phase, elapsed) from `.ai/runs/status.json` and exit. |
| `--base` | branch to build on (default <your-integration-branch>) |
| `--card` | dispatch only this card id. |
| `--max-cards` | stop after N dispatches |
| `--until` | stop dispatching at this local time, HH:MM |
| `--max-minutes` | stop dispatching after N minutes |
| `--sessions` | how many usage-limit windows the night may spend. |
| `--budget` | optional USD cap for the whole run. |
| `--card-budget` | optional USD cap handed to each worker process; 0 (default) passes no cap at all |
| `--test-timeout` | seconds allowed for the test suite (~2 min today) |
| `--stale` | after the cards, run the Tier-2 staleness sweep on the N highest-churn docs, spending only leftover window. |
| `--append-digest` | write this run's digest without advancing the read baseline, so the next run's digest still reaches back to the last one written *without* this flag — for a stretch of runs (a scheduled weekend) nobody is there to read each one. |
<!-- /generated:runner-flags -->

Naming a card with `--card` is an explicit human request, so `unattended:
false`, the backoff wait and the attempt limit are waived for it; a missing
`requires:` capability, no charter or a schema failure are not, and the run
always says why and exits non-zero rather than doing nothing quietly.

A real run takes cards from `tasks/` only, one worktree and branch per card,
and files the result: gates green → `review/`, worker parked it →
`needs-decision/`, gates red → retried up to the failure limit, then
`failed/`. It writes `Digest.md` at the end. **Stop a run: create `.ai/STOP`**
— the runner checks for it every minute, mid-run included, even while sleeping
out a usage limit.

<!-- stale-ok: `.ai/hosts.json` and `.ai/host.json` are per-machine config files in a
     CONSUMING project — `nightshift init` writes the first and `night.py` the second.
     This package is not its own consumer, so neither resolves here. -->
### `hosts.json`

Per-machine config, keyed by hostname, committed on purpose — a box is
configured once and it syncs, instead of a setup step that is silently missing
until something quietly fails to dispatch.

* **`capabilities`** — the slugs a card's `requires:` is checked against. An
  unlisted hostname resolves to nothing, so a card needing hardware this box
  lacks simply never dispatches here, at zero token cost. **Never inferred** —
  `nightshift init` always proposes it empty.
* **`permission_mode`** — handed to `claude --permission-mode`. `bypassPermissions`
  is what a code card needs; the default cannot run Bash, so pytest and git are
  unavailable and the card burns an attempt doing nothing. **Never inferred** —
  see [the one warning](#the-one-warning).
* **`allowed_tools`** — optional, only meaningful below `bypassPermissions`. A
  tool the card needs but the list omits is denied, and in unattended mode
  there is nobody to ask, so the worker stalls for a reason that looks nothing
  like the real one.
* `.ai/host.json` (gitignored) overrides this file wholesale, for a one-off
  answer on one box you do not want to commit.

### Budgets and stop conditions

A night is measured in session windows before it is measured in dollars.
`--sessions 1` (the default) works until the plan's usage limit is reached and
stops; `--sessions 2` sleeps through the first reset and stops at the second
wall. `--budget` and `--card-budget` default to 0 (no cap) — on a subscription
plan the figure is API-equivalent rather than money spent.

Three more stops, each a fact about the run rather than a dial: a card that
fails several attempts in a row goes to `failed/`; a card that resumes with no
change to the working tree twice in a row is judged stuck and given back
rather than retried forever; and `.ai/STOP`, checked every minute, is the
manual kill switch — gitignored, so it works from a synced vault on your
phone.

<!-- stale-ok: `Digest.md` is written into a consuming project by a run; it does not
     and should not exist in this package's checkout. -->
### The digest

`python -m nightshift.digest` writes `Digest.md` — generated and overwritten on
every run, never a place to leave an answer by hand. It opens with a one-line
banner (runs since last read, landed / failed / decide counts), then one block
per run since the last digest — failed, needs-your-decision, passed, stale-hunter
findings, skipped — followed by standing board state: open decisions, the
`failed/` lane, counts for `testing/` and `review/`, and the dispatch queue.
Answer a `needs-decision/` card in its own `## Thread` section, on the board —
the digest is regenerated and any answer written there is gone.

### The corrections log

One line per correction in `.ai/corrections.log`: `DATE | SLUG | CLASS | CHANNEL
| GATE | NOTE`. `CLASS` comes from `.ai/gates/data/corrections_vocab.json`,
which ships **empty** — the first entry you write also adds its class. `NOTE`
should be at least 40 characters and say three things: what was wrong
concretely, why it was believed, and what generalises — the middle one is what
makes clustering possible, since the shape of an error is what repeats, not
its subject. `corrections_log` validates the file on every gate run, so a
malformed line cannot sit unnoticed.

```
python -m nightshift.corrections              # the distribution report
python -m nightshift.corrections --class <c>   # every entry in one class
python -m nightshift.corrections --compact     # archive old entries, keep the log short
```

### Gates: the shipped set, writing your own, and appeals

```
python -m nightshift.gates.run                    # every gate
python -m nightshift.gates.run --skip <name>       # every gate except this one (repeatable)
python -m nightshift.gates.run <name> [<name>...]  # only these
```

Core gates live in the package; a project's own live in `.ai/gates/`. A name
collision between the two is an error, not a precedence rule — rename the
project's one.

<!-- generated:gate-list -->
| Gate | What it checks |
|---|---|
| `branch_role_prose` | docs naming the integration branch must agree with .ai/manifest.toml [branches] |
| `card_schema` | cards on the board match the card schema, and `state:` agrees with the lane |
| `corrections_log` | the correction log parses and its class/channel values are in vocabulary |
| `dead_code` | vulture reports no dead code in the project's source at the configured confidence |
| `deletion_sweep` | a removed file or top-level class/def must not still be named by any live doc |
| `doc_reference_liveness` | docs must not name files or symbols that no longer exist |
| `doc_signature_drift` | a signature written into a doc must match the real parameter names, in order |
| `gate_appeals` | every `# gate-ok(...)` appeal names a real gate and carries a written reason |
| `import_layering` | a declared one-way dependency between packages stays one-way |
| `line_endings` | Line endings stay LF, in the index and in the working tree. |
| `memory_freshness` | a diff touching a declared source area must also touch its memory doc |
| `orientation_budget` | the declared orientation files stay under [memory].budget_bytes |
| `orientation_shape` | declared orientation docs hold current state, not dated history |
| `pytest_invocation` | only suite.py may spell pytest's parallel flags; callers use parallel_args() |
| `readme_generated` | README.md's generated blocks (gate list, runner flags, manifest fields) match a fresh generation |
| `run_stop_recorded` | runner.py logs a stop reason only via _stop(), which also records it |
| `subprocess_encoding` | no subprocess call in the tooling decodes output with the locale codec |
| `subprocess_result_checked` | no subprocess call in the tooling discards its result |
| `write_newline` | no text-mode write in the tooling leaves newline translation unpinned |
<!-- /generated:gate-list -->

Several gates are inert until the manifest gives them something to read, and
that is a supported state rather than a gap: `orientation_budget` needs
`[memory].budget_bytes`, `memory_freshness` needs `[memory].freshness`, and
`import_layering` needs `[layering].forbid`.
Without those they run and report nothing. `dead_code` and the doc-scan family
(`doc_reference_liveness`, `doc_signature_drift`, `deletion_sweep`) read
`[project]`; the five discipline gates read `[project].tooling_dirs` on top of
`.ai/`; the rest are config-free.

**Writing your own:** a file in `.ai/gates/` exposing `check(repo_root) ->
list[Violation]` is a gate — no registration, `run.py` discovers it by glob. A
gate needing a scope-and-pattern check ("inside file X, forbid call Y") is
almost always one of a few recurring shapes rather than a new idea; read a
shipped gate's own docstring before writing one, since the incident that
earned it is usually the fastest way to see whether yours matches.

<!-- stale-ok: the three recipe paths below are files in the repo being
     configured, written there by `init` from `templates/ai/recipes/`. This
     package has no `.ai/recipes/` of its own — it consumes its own gates, not
     its own board. -->
The discipline around that — deciding whether a logged correction is
mechanically catchable at all, and the four ways a new gate goes wrong (firing
on the word rather than the claim, failing open, narrowing its own scope,
being tested only against its author's examples) — ships as
`.ai/recipes/turn-a-correction-into-a-gate.md`, written into the project by
`init` alongside `.ai/recipes/verify-before-shipping-a-rule.md` and
`.ai/recipes/write-a-test-that-earns-its-place.md`. Those three are the only
recipes that ship: two about writing rules, one about the tests every check
here assumes, and none about any project's subject.
<!-- /stale-ok -->

They are also the reason `recipe:` on a card is usable at all: `card_schema`
resolves the field against that directory, so before they shipped the only
value that could pass validation was `none`.

**Appeals, three levels, in `gates/appeal_markers.py` / `gates/gate_appeals.py`:**

| Level | What it is | Who decides | Written where |
|---|---|---|---|
| L1 — exempt one site | this line is a false positive | the agent may, and must say why | `# gate-ok(<gate>): <reason>`, counted as debt |
| L2 — narrow the gate | the rule is wrong at the edges | proposed by the agent, approved by a person | the gate's own docstring, plus a corrections-log entry |
| L3 — retire the gate | the rule should not be enforced at all | a person, only | the project's own audit matrix, if it keeps one |

A fourth move is never legitimate: editing the rule's prose until the code
complies. If the prose is wrong, that is L2 or L3, and both go through a
person. `# gate-ok(...)` must name one real gate and carry a reason of at
least a minimum length (`appeal_markers.MIN_REASON`) — `gate_appeals` fails a
marker that names no gate, names a gate that does not exist, or gives too
short a reason. Docs have the older, equivalent `<!-- stale-ok: reason -->`
marker for the same purpose.

<!-- stale-ok: the generated table below prints each field's DEFAULT, and
     `[tiers].binding_doc` defaults to `docs/tier-binding.md` — a path in the repo
     being configured, which `init` creates there. It is not a file of this
     package's. -->
### The manifest

<!-- generated:manifest-fields -->
| Table | Fields | Purpose |
|---|---|---|
| `[project]` | `name=''`, `source_dirs=()`, `extra_source_dirs=()`, `tooling_dirs=()`, `doc_files=('CLAUDE.md',)` | What the code under test is called and where it lives. |
| `[tests]` | `dir='tests'`, `parallel=True` | `dir` is where pytest is pointed. |
| `[branches]` | `integration=None`, `stable='main'`, `forbidden_extra=()` | `integration` has no default on purpose (module docstring). |
| `[board]` | `root='Board'` | Lane names are framework config, not project facts (D5), so they are not here — only where the board lives, and who signs a decision on it. |
| `[worker]` | `harvest_dirs=()`, `fence_env=''`, `integration_checkout_dir=''` | The three constants that were the entire project-specific content of `runner.py`'s 3,597 lines. |
| `[memory]` | `orientation=()`, `budget_bytes=None`, `freshness=()` | `budget_bytes = None` means **the orientation-budget gate does not run**, and that is the recommended starting value. |
| `[layering]` | `forbid = [{importer, imports, exempt}]` | `importer` must not import `imports`. |
| `[i18n]` | `adapter` (required), `base='en'`, `targets=()`, `untranslated_allowlist=''`, `loanwords_denylist=''` | Present only if the project has localisation; `Manifest.i18n` is `None` otherwise and a project's i18n gates find nothing to check. |
| `[dead_code]` | `paths=()`, `min_confidence=80` | What `nightshift.gates.dead_code` points `vulture` at, and how sure it must be before it speaks. |
| `[audit]` | `matrix=''`, `infra_gates=()` | Where this project keeps its rule-enforcement matrix, and which of its own gates that matrix is not expected to have a row for. |
| `[tiers]` | `binding_doc='docs/tier-binding.md'` | Which document carries the `tier: → model` binding block. |
<!-- /generated:manifest-fields -->

Absence is meaningful and is not the same as a default: `[i18n]` omitted leaves
a localised project's own i18n gates with nothing to check, rather than
defaulting them against strings that are not there; `branches.integration` has
no default at all and is never guessed, because `forbidden_bases()` depends on
it and a plausible-looking wrong answer means the runner builds on a branch
nobody chose. An unknown key anywhere in the file is an error, not a silent
no-op.

`[i18n]` is the one table with no consumer inside this package. `i18n_parity`,
`i18n_untranslated` and `i18n_loanwords` shipped as core gates until 2026-08-03
and are now Dungeoneer's, under its own `.ai/gates/` — "generic" was decided to
mean *broadly applicable*, not merely domain-free, and most repos have no
translations to check. The table stays because those gates still read it, from
outside, through `manifest.load(root).i18n`: it is the vocabulary any localised
project's gates speak, and `validate()` still checks it. A project adopting the
same three gates copies them out of Dungeoneer's `.ai/gates/` and writes an
adapter.

### The staleness sweep

`--stale [N]` on the runner spends leftover window, after the night's cards,
running the Tier-2 staleness checker (`stale-hunter`) against the
highest-churn docs — the ones whose named source files have changed the most
since they were last verified. A doc whose sources have not moved since its
last complete verification is skipped entirely; a doc never verified sorts to
the top. Findings become one fix-card per doc — the sweep itself never edits a
doc — and a doc is only marked verified in the ledger on a *complete* verdict,
so a sweep cut off mid-doc re-checks that doc next time rather than recording
one it did not finish.

### What you do not get

* **The gate suite as a value proposition.** A new repo starts with the
  generic gates above, an empty audit matrix and an empty corrections log.
  Rules get added the way this project's own were: by an observed failure,
  never by import. The *method* does ship — the three recipes above — because
  a loop whose third step is undocumented stops at the second one.
* **Asset generation.** Any image, audio or other generation stack a project
  wires up is that project's own, in its own scope — nothing here assumes one
  exists.
* **Non-Python support.** The AST-based gates and `suite.py`'s test-splitting
  both assume a Python project underneath.
* **The reasoning behind any of this.** The plan documents that produced this
  package are a record of one project's decisions, not part of the product —
  this README and the code's own docstrings are what ships.

<!-- stale-ok: `.ai/hosts.json` again — a consuming project's file. Every path in
     this section names something in the repo being troubleshot. -->
### Troubleshooting

Run `nightshift doctor` first — it reports, in order: the working tree is LF
(a CRLF file is invisible to `git status` but fails the line-endings gate on
every affected file); `claude` is resolvable on `PATH` (or `CLAUDE_BIN` points
at a real executable); this hostname has an entry in `.ai/hosts.json` (an
unlisted host makes every `requires:` card silently undispatchable — no error,
just nothing happening); `[branches].integration` is declared (the field that
is never guessed); and which commit of `nightshift` is actually installed,
reported but never enforced, since the intended mode while this is still
changing daily is an editable install shared live across every consumer.

* **A fresh clone warns "LF will be replaced by CRLF" on the first commit.**
  `.gitattributes` is missing `* text=auto eol=lf` — `nightshift init` writes
  it if absent; add it by hand otherwise.
* **Files show clean in `git status` but the line-endings gate still fires.**
  A checkout that predates `.gitattributes` landing has CRLF already on disk;
  the LF blob and the CRLF working-tree file normalise to the same bytes, so
  git sees nothing to commit. Run `python -m nightshift.normalize_worktree`
  once, per machine — there is nothing to commit afterwards either, because
  the fix does not change file content.
* **A `requires:` card never dispatches, with no error.** Check `hosts.json`
  for this hostname first — an unlisted host is the single most common cause,
  and it is silent by design (see above).
* **The runner refuses to build, citing a forbidden base.** `stable` and any
  `forbidden_extra` branches are refused as a base on purpose; check them
  against `[branches]` before assuming the tooling is wrong.

<!-- stale-ok: `.ai/corrections.log` and `.claude/settings.json` here are the
     consuming project's files — the ones `uninstall` is careful with. -->
### Removing it

```
nightshift uninstall          # lists what it would remove; changes nothing
nightshift uninstall --yes    # performs it
```

**It removes files this install created, and only those.** `init` writes
`.ai/install.json` naming every file it created; `uninstall` reads that list back. A
file your project already had is not on it and cannot be deleted here.

**Five files get a block appended rather than being written**, because for those the
content is the requirement and not the file: `CLAUDE.md` (the rules a session follows),
`.gitattributes` (one line, or `line_endings` fires on everything a tool writes),
`.gitignore` (four patterns, or your run logs and preflight receipts get committed),
the `[tiers].binding_doc` (the fenced block `nightshift.tiers` parses) and
`Board/README.md` (the lane contract). If you already have one, `init` appends a block
fenced by `nightshift:begin` / `nightshift:end` markers and leaves every byte you wrote
in place — for `CLAUDE.md` the block is a short pointer, and the framework's rules go
in `.ai/CLAUDE.md`, a file init owns outright. **`uninstall` strips exactly those
blocks** and keeps the files. The markers are the record, so this works with or without
a receipt.

If the receipt is missing — an install predating it, or a deleted file — it falls
back to removing only files whose content is *exactly* what `init` writes, and
prints everything it declined and why. Conservative in the one direction that
matters: a file you wrote or have since edited is kept.

Three further refusals — it will not delete a `.ai/corrections.log` that has real
entries (earned evidence, and the one thing here that cannot be regenerated), it
will not remove a directory that still has your own work in it, and it un-merges
its hook entries from `.claude/settings.json` rather than replacing the file.

It is dry-run by default. In a repo with real work in it, read the list first.

The package itself stays installed, which is what you want: `init` never
overwrites, so `uninstall` then `init` is the supported way to redo a first
install that went wrong.
