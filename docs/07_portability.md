# Portability — extracting the AI team into `nightshift` (doc 07, Session K)

**Phase 7, shipped.** §8 steps 1–7 are done (2026-07-31 → 2026-08-05); step 8 — the colleague
handoff — is **postponed on purpose**, not forgotten. `00_architecture.md` §8 sketched this as
two directories. That sketch predates the evidence and is superseded here; §8 stays as the
record of what was assumed. This doc is now the reasoning behind a built thing; each step in
§8 carries its own outcome.

Karel's answers, 2026-07-30, which set the scope:

- **There is a second project**, and it is another Python repo. So the full mechanism ports,
  including `suite.py` and the AST-based staleness gates — the two things a non-Python repo
  would have lost.
- **Colleagues are on Python too, and unattended runs are acceptable** in their environment.
  So `runner.py` ships to them rather than being held back; this is not the corporate-policy
  case that would have cut the deliverable in half.
- **The deliverable is a package**, not a documented copy procedure.
- **The interview must discover and propose, not interrogate.**
- **A human-facing `README.md` is part of the deliverable**, covering install and the manual
  operations: runner flags, appending to the corrections log by hand, running triage by hand.

---

## 1. What was measured

The reason §8's directory split is wrong is not aesthetic. Project coupling was counted per
module, and it is tiny, concentrated in named constants at the top of files, and nowhere
tangled through logic:

| Module | Lines | Project Tigress-specific content |
|---|---|---|
| `runner.py` | 3,597 | 3 constants — the harvest dirs, the fence env var, the integration checkout dir (all `[worker]` since step 4) |
| `preflight.py` | 555 | none |
| `corrections.py` | 287 | none |
| `limits.py`, `tiers.py`, `textio.py` | 375 | none |
| `board.py` | 383 | `Path("Board")` + the eight lane names — framework conventions, not project facts |
| `suite.py` | 517 | one prefix (`project_tigress/` → GAME); the rest of `classify` is framework-owned |
| `gates/doc_scan.py` | 650 | its source-dir and doc-file tables (now `[project]` in the manifest) |

The orchestrator is ~97% project-free. Against that, a directory split would fork ~10,500
lines of `.ai/` Python **and** the ~7,000 lines of system tests under `tests/`
(`test_board_runner.py` alone is 4,075) in order to isolate roughly forty lines of tables.

Two findings that change the decision list:

**`suite.py`'s selection is already derived, not listed.** `classify` is four prefix checks
and only `project_tigress/` is ours; `split_test_files` and `board_test_files` read the real tree
and parse imports. The per-part test globs D2 expected to find hardcoded are globs *by
design*, for the reason the module documents: a newly added `test_gate_*.py` must be
classified without anyone remembering a list. So the manifest field is a source-dir list, and
nothing more.

**This repo has no packaging metadata at all** — no pyproject, no setup script, no setup or
pytest config file. Discovery cannot rest on it. It must infer from the filesystem and treat
metadata as corroboration when present.

---

## 2. The seven decisions, answered

**D2 — config-level, and the config lives outside the project.** Answered first because
everything follows from it. §8's copy fails both use cases, for different reasons: a copy
cannot merge (Karel's stated want is "improve at both and merge"), and a copy has no version,
so a colleague can neither be told they are behind nor pull a fix. One repo, consumed as a
pinned dependency, with a per-project manifest. `.ai/` in a consuming project shrinks to a
manifest plus that project's own gates.

**D1 — core ships the seam; the checker is optional and declared.** The producer/checker loop
is not universal: audio came out two-part because a blind listener cannot judge a clip
(`03_board.md` §5). But the *machinery* already is — `art` appears nowhere in the runner's
logic, `[worker].harvest_dirs` points at a convention rather than a path, and a regression test locks
that generality. So core ships the loop with `checker:` as a per-worker declaration whose
absence means "hard-route to `needs-decision/`", which is exactly the shape Karel's
2026-07-30 correction described for `audio`. A project adds a checker by naming one, not by
adding a code path.

**D3 — promote a *larger* line than `_INFRA_GATES`, and note that it is not already drawn.**
`audit.py::_INFRA_GATES` answers "is this rule about `.ai/` or about `project_tigress/`". Portability
asks "is this rule about *any* project or about *this* one". Those differ:
`line_endings`, `deletion_sweep`, `doc_reference_liveness`, `doc_signature_drift`,
`memory_freshness` and `branch_role_prose` are all project-*facing* — hence excluded from
`_INFRA_GATES` — yet each enforces a universal rule. `_INFRA_GATES` is a strict subset of the
portable set, so it can be *reused* but not *renamed*.

**The four-class table below was rebuilt 2026-07-30 by reading all thirty gate modules.** The
first version classified them from their names and their truncated docstrings, and was wrong
about **five of thirty, in both directions** — see §2b for what that cost and §2c for the
mechanism it missed. The counts here are read, not derived.

| Class | Count | Gates |
|---|---|---|
| **Config-free** — runs identically in any repo | 11 | `subprocess_result_checked`, `subprocess_encoding`, `write_newline`, `pytest_invocation`, `corrections_log`, `gate_appeals`, `run_stop_recorded`, `line_endings`, `card_schema`, `branch_role_prose`, `appeal_markers` (helper) |
| **Manifest table** — generic logic, project table | 10 | `doc_reference_liveness`, `doc_signature_drift`, `deletion_sweep` (all three via `doc_scan`'s source-dir and doc-file tables), `memory_freshness`, `orientation_budget`, `dispatch_reachability`, `import_layering`, `asset_hygiene`, `audio_hygiene`, `sources_reference` |
| **Adapter** — needs a project code module | 3 | `i18n_parity`, `i18n_untranslated`, `i18n_loanwords` — §2b |
| **Stays** — encodes a project architecture decision | 7 | `cheat_give_item`, `bus_subscribe_discipline`, `fx_real_enemy_art`, `help_catalog_shape`, `render_codepoints`, `skill_gating`, `audio_volume` |

**23 of 30 travel, not 19.** `_INFRA_GATES` (8 rows, not the 7 D3 quotes — `write_newline`
joined it after that count was taken) is still a strict subset of the portable set, so it can
be *reused* as a seed but not renamed to mean this.

Four gates moved on reading, and every one had a tell that was available and ignored:

- **`dispatch_reachability`** — "every concrete subclass of a tracked base is named somewhere
  as a load-bearing reference, not merely imported, abstracts skipped" is a rule any project
  with command or plugin base classes wants. Its project part is two dict entries
  (`{"Scene": "core/scene.py", "Action": "combat/action.py"}`).
- **`memory_freshness`** and **`orientation_budget`** — both were pure logic over a hardcoded
  project table (a four-row path→doc list; a four-file tuple plus a byte budget). The tell: §4
  of this very document already specifies `[memory].freshness` and `[memory].orientation` as
  manifest fields. **The manifest contradicted the table, in a document written in one pass.**
  It then contradicted it for another three days: step 4 moved neither gate, and the review of
  2026-08-02 found both tables in the schema with nothing reading them. Both are core now.
  <!-- stale-ok: the hardcoded constants named in this paragraph are what the promotion
       deleted; naming them is how the finding is stated. -->
- **`sources_reference`** — two rules, both generic in shape: "code must not reference paths
  under X" and "X must never be tracked in git". Every project has a `sources/` equivalent
  (downloads, vendor dumps, scratch), and the git-tracking half is worth having on its own.

And two confirmed as staying, for a reason worth stating because it is the actual test:
**`fx_real_enemy_art`** and **`help_catalog_shape`** encode an *architecture decision* — a
real-asset registry with a procedural fallback, and one specific nested-literal schema — not
just a project's paths. That is the line: a gate stays when the rule presupposes how this
project is built, not when it merely mentions where.

### 2b. The i18n family needs a third mechanism: an adapter, not a table

**Corrected 2026-07-30 after Karel asked how the three-language rules would port** — *"Is this
considered projects specific or general?"* The first version of the table above put all four
`i18n_*` modules in "stay here", classified from the shared name prefix without opening one of
them. That is the `derived-not-verified` class doc 10 §3 names as the largest in the log.

The family splits three ways, and only the middle layer is ours:

| Layer | Portable | Here |
|---|---|---|
| **The rules** — every key in every language; a translation differs from the source; no source-language loanwords in target prose | general | `i18n_parity` (43 lines of set algebra), `i18n_untranslated` (85), `i18n_loanwords` (57) |
| **The reader** — how a project stores strings | **project** | `i18n_adapter_loader` (64 lines, named i18n_common when this was written): imports `project_tigress/core/i18n.py` and line-scans its `_STRINGS` dict literal |
| **The exemptions** — what is legitimately identical or legitimately a loanword | project content | `data/i18n_untranslated_allowlist.json`, `data/i18n_loanwords_denylist.json` |

**Superseded in part, 2026-08-03** — [[nightshift-i18n-gates-in-a-generic-suite]].
The top row's "general" verdict did not survive the question *what does a fresh install
actually deliver?* The three rules are portable in the sense this section means — they
presuppose nothing about a game — but "generic" was decided to mean **broadly applicable**,
not merely domain-free, and a repo with no translations read three inapplicable gate names in
every run forever. All three gates moved to `.ai/gates/` here, alongside the reader and the
exemptions, so the whole family is now one project's. The rest of this section stands: the
adapter seam is what was actually load-bearing, it is still in place, and a second localised
project copies four files and writes ~30 lines rather than starting from nothing.

The adapter interface is the three functions `i18n_adapter_loader` already exposed —
`load_strings` returning `{lang: {key: value}}`, `zone_key_lines` returning
`{lang: {key: line}}` for `file:line` reporting, and `i18n_path`. Nothing else. The one
change the gates need is that `i18n_parity` hardcodes `"en"` and `("cs", "es")`; base and
target languages become manifest fields.

Why this matters more than the three gates themselves: **a project storing strings in gettext,
`.po` files or JSON-per-language writes ~30 lines and gets all three.** A project with no
localisation declares no adapter, the gates never register, and nothing is lost. Compare what
row 5 of `01_audit_findings.md` records about the state before `i18n_parity` existed here —
no global parity check anywhere, only hand-listed key tuples in ~8 tests sampling a fraction
of ~1351 keys. That gate is one of the most reusable things this project has built, and the
name-based classification would have left it behind.

Two related pieces are already handled elsewhere and need no new mechanism: the
source-area-to-doc rule for `core/i18n.py` is a `[memory].freshness` row (§4), and the
`translator` charter plus the `localization-naturalness` skill are project-side templates
(D5) — the skill is 98 lines with 5 project references, so it is mostly reusable prose.

### 2c. Five gates are one shape — and core should ship the helper, not a DSL

Found by the same read-through, and it was in none of D1–D7. Five gates are instances of a
single pattern: **inside scope S, pattern P is forbidden.** Four of the seven "stays", plus
`sources_reference`, which the table above places in the manifest-table class because its
*second* half — "this directory must never be tracked in git" — is independently portable while
its first half shares this shape. (Corrected 2026-07-30: this section first claimed "five of
the seven stays", which contradicts the table four paragraphs above it. Same failure as the
one §2b records, at smaller scale.)

| Gate | Scope | Forbidden | Is the *rule* portable? |
|---|---|---|---|
| `cheat_give_item` | one file, one function (`_apply_cheat`) | `.add()` on an `.inventory` chain; a Store to `.ammo_reserves` | No — but the principle is general: a secondary path must funnel through the same domain service as the primary one, so it inherits its side effects for free |
| `bus_subscribe_discipline` | one file, every method *except* three | `bus.subscribe` / `bus.unsubscribe` | No — but general to **any** project with an event bus, and leaked subscriptions between runs is one of that architecture's standard bug classes |
| `audio_volume` (first half) | the whole package except `core/settings.py` | `.set_volume(<numeric literal>)` | No — the second half generalises better: do not re-derive a helper's formula at the call site |
| `skill_gating` | the whole package | `bool(*.skills)` / `if *.skills:` | **No, and least of the five** — it encodes one class's quirk (`flush_to_profile` banks sub-threshold entries), so it cannot be written without knowing that quirk |
| `sources_reference` (first half) | the whole package | a string literal matching `sources/` | Partly — "code must not reference paths under X" is a manifest row |

Five instances is not speculative generalisation — §15's rule is that gate selection follows
observed failures, and this shape has been re-implemented five times by hand.

**What actually ships to a second project is not any of these five.** §15 forbids it: a gate is
earned by an observed failure *there*. What ships is (a) the helper that makes writing one
cheap, and (b) the **catalogue of shapes as a prompt** — "here are the failure shapes that
recurred in a real project, and the incident behind each; has yours had one of these?" That
catalogue is worth more to a colleague than the code, and it belongs in the README's reference
half rather than in `core/gates/`.

**But the obvious move is the wrong one.** A single declarative gate driven by a manifest table
would need a scope selector (file glob + function or method predicate + exclusion list) *and* a
pattern selector (AST call, attribute store, literal-argument type, or a regex fallback). That
is a mini-language, and a DSL nobody can read is worse than five readable fifty-line files —
each of which currently carries the incident that earned it in its own docstring, which a table
row cannot.

**So the recommendation is a third mechanism alongside table and adapter: core ships
*helpers*, not a rule engine.** Functions like `forbidden_call_in_scope(...)` that a project's
own twenty-line gate calls, keeping the gate file — and its docstring, and its evidence — in
the project. Portability by reuse rather than by configuration. This is a genuine open call
rather than a settled one: build one helper against two of the five and see whether the third
fits before committing to it.

**D4 — one home per fact, and every list in the README is generated.** The sweep before this
session found the worker roster and the ★ backlog each written in two prose homes, and both
drifted. Extraction multiplies that, so the rule is enforced rather than intended: any *list*
in the README — gate names, runner flags, lane names, manifest fields — is generated from the
code that owns it, and a gate checks the README against a fresh generation. This is the same
shape as `branch_role_prose`, which already makes prose agree with `branches.py`. Free text
around the generated blocks is fine; a retyped list is a defect.

**D5 — "generic" means the charter carries no project nouns *of its own*; nouns arrive by
substitution.** On that definition `triage` naming `Board/` lanes is still generic (lanes are
framework config) and `code-reviewer` naming the integration branch is still generic (the
branch is a manifest field). What makes a charter project-bound is a project-specific
*procedure*. That gives four core charters — `triage`, `code-thread`, `code-reviewer`,
`stale-hunter` — and three that stay: `translator`, `art`, `art-reviewer`. `code-thread`
moves to core against D5's own first reading; verify it during extraction rather than
trusting this line, because its skill references (`implement-here`, `write-tests-here`,
`wire-ui-here`) are per-project templates and if the charter cannot be written without naming
their contents it belongs on the project side.

**D6 — the bar, not a demo.** In a repo containing zero files of either project: gates run
and pass; `nightshift init` produces a manifest that `nightshift doctor` accepts; a card moves
`inbox → tasks → review → testing` with a commit per move; the runner refuses a forbidden
base; a preflight passes and writes a receipt. `suite.select` returning NONE for a
code-free diff, and both call sites treating an empty arg list as a judged pass, means an
empty repo with no tests is a case the tooling already handles.

**D7 — answered: yes, there is a second project, and it is Python.** The deferral branch of
D7 is closed.

---

<!-- stale-ok: this section specifies the `nightshift` repo's layout — every path in it is a
     DELIVERABLE of this session, in a repo that does not exist yet and will never be resolvable
     against project_tigress/. Naming them is the instruction; the same precedent as Session F's
     runbook naming `.claude/agents/triage.md` before triage existed. -->
## 3. Target shape

```
nightshift/                        # its own git repo, sibling to the projects that use it
  pyproject.toml               # installable; this repo has none, that is the point
  README.md                    # human-facing (§6); generated lists, checked by a gate
  nightshift/
    manifest.py                # schema, load, defaults, validation
    board.py reconcile.py digest.py runner.py night.py
    suite.py preflight.py merge_check.py
    corrections.py limits.py tiers.py textio.py branches.py run_record.py
    stale_sweep.py audit.py
    gates/                     # base, run, + the 16 universal gates
    hooks/                     # tier_guard, preflight_guard, worktree_fence, correction_prompt
    templates/                 # charters, skills, manifest, the CLAUDE.md fragment
    cli.py                     # nightshift init | doctor | docs
  tests/                       # the ~7,000 lines — one home, not one per project
```

A consuming project keeps:

```
.ai/manifest.toml              # the ~40 lines that were hardcoded
.ai/gates/                     # only its own gates
.ai/corrections.log            # its own earned evidence
Board/                         # its own board
.claude/                       # its own charters, skills, settings.json
```

`.ai` cannot be a Python package — a leading dot is not an identifier — so its modules are
already imported flat after a `sys.path` insert. There are no intra-package absolute imports
to rewrite. The conversion is entry points and the hook command strings in
`.claude/settings.json` (`python .ai/hooks/tier_guard.py` → `python -m nightshift.hooks.tier_guard`),
not module bodies.

**Consumption.** `pip install -e ../nightshift` in both projects while the framework is still
changing daily — an improvement in one is live in the other with no merge at all. Pin to a
tag once it settles. That is the mechanism that makes Karel's "improve at both and merge"
free rather than a cherry-pick job in two directions forever.

**Repo visibility is Karel's call.** Recommendation: private first. Public costs a licence
decision, a contribution policy and a support surface, and none of that is needed to hand a
colleague a git URL.

---

<!-- stale-ok: the fields below are TOML keys in a manifest format this session defines, and
     `nightshift/manifest.py` is the module that will own it. Neither is a project_tigress/ symbol; the
     gate resolves backticked names against the source tree and these have no referent there
     by construction. -->
## 4. The manifest

One file, `.ai/manifest.toml`, holding every fact that is currently a constant in a core
module. Sketch, not final — the authority is `nightshift/manifest.py`, and §6's generation rule
means this table is regenerated into the README rather than retyped:

```toml
[project]
name = "project_tigress"
source_dirs = ["project_tigress"]           # suite.classify prefixes; doc_scan.SOURCE_DIRS
extra_source_dirs = ["tests", "tools"] # scanned for symbols, not part of the code slice
doc_files = ["CLAUDE.md"]

[tests]
dir = "tests"
parallel = true                        # -n auto --dist loadfile when xdist is installed

[branches]
integration = "development_team"
stable = "main"
forbidden_extra = ["dev"]              # bases forbidden beyond stable — the dev-is-stable case

[board]
root = "Board"                         # lanes default; override only with a reason

[worker]
harvest_dirs = ["project_tigress/assets/.tmp"]
fence_env = "PROJECT_TIGRESS_FENCE_ALLOW"
integration_checkout_dir = ".project_tigress-integration"

[memory]
orientation = ["MEMORY.md", "arch.md", "state.md", "design.md"]
budget_bytes = 104857600               # orientation_budget; see the warning below
freshness = [                          # memory_freshness rows
  { touches = "project_tigress/core/i18n.py", requires = ".claude/memory/ref_i18n.md" },
]

[layering]                             # import_layering, once it moves to core
forbid = [{ importer = "project_tigress.combat", imports = "project_tigress.rendering" }]

[i18n]                                 # omit the whole table and the 3 gates never register
adapter = ".ai/i18n_adapter.py"        # supplies load_strings / zone_key_lines / i18n_path (§2b)
base = "en"
targets = ["cs", "es"]
untranslated_allowlist = ".ai/gates/data/i18n_untranslated_allowlist.json"
loanwords_denylist = ".ai/gates/data/i18n_loanwords_denylist.json"
```

`.ai/hosts.json` stays a separate file, unchanged and still committed. It is per-*machine*,
not per-project, and merging it into a per-project manifest would put the same host in N
places.

---

<!-- stale-ok: the discovery table's left column is manifest keys this session defines, and the
     right column names files in *other* repos — the ones being discovered. Neither resolves
     against project_tigress/, which is the whole point of a discovery table. -->
## 5. `nightshift init` — discovery first, then confirmation

Karel's requirement, and the design constraint that makes it safe is his own rule from
`hosts.json`: **a check that discovers a fact may probe; a check that bounds behaviour must be
declared.** So discovery proposes every field it can infer, and two fields are never inferred.

| Field | How it is discovered | Confidence |
|---|---|---|
| `project.name`, `source_dirs` | top-level dirs containing `__init__.py`; corroborated by `pyproject.toml` when present | high |
| `tests.dir` | `tests/`, `test/`, or a pytest config section | high |
| `tests.parallel` | `importlib.util.find_spec("xdist")` | high — a fact, not a preference |
| `doc_files` | `CLAUDE.md` at the root | high |
| `branches.stable` | `git symbolic-ref refs/remotes/origin/HEAD` | high |
| `branches.integration` | candidates `dev`/`develop`/`integration`, else the branch furthest ahead of stable | **medium — must be confirmed** |
| `worker.*` | derived from `project.name` — `<pkg>/assets/.tmp` if an assets dir exists, `<PKG>_FENCE_ALLOW`, `.<pkg>-integration` | high |
| `memory.orientation` | the files `CLAUDE.md` names | medium |
| `layering.forbid` | build the import graph and propose only the pairs the tree **already** satisfies | medium, and safe by construction |
| `hosts.json` hostname | `socket.gethostname()`, run **on the box** | high |
| `hosts.json` capabilities | **never inferred.** Proposed empty, always | — |
| `hosts.json` permission_mode | **never inferred.** Proposed as the default, with the sandbox warning quoted | — |

Two of those rows are the interesting ones.

`branches.integration` must be confirmed rather than accepted, because `forbidden_bases()`
depends on it and a wrong answer means the runner builds on a branch nobody wanted. This
repo is the cautionary case: `dev` is *stable* here and is a forbidden base, which no
heuristic would guess.

Capabilities stay declared for the reason `hosts.json` already documents: a probe answers
"is the stack present?", and the honest response to "no" from an agent with initiative is to
install it — 20 GB of weights onto a laptop at 3 AM, which no gate would catch. Discovery
that proposes `capabilities: []` is not a limitation; it is the rule.

**Three things `init` must also check and offer to fix**, because all are silent failures on
Karel's platform:

1. **`.gitattributes` without `* text=auto eol=lf`.** On Windows, Git ships
   `core.autocrlf=true`, so a fresh clone without that line makes `line_endings` fire
   immediately. Detect, explain, offer to write it.
2. **CRLF working-tree files.** A checkout that pre-dates `.gitattributes`'s
   `* text=auto eol=lf` landing has CRLF files on disk. Unlike the missing-attributes case,
   these are *invisible* to git: the LF blob normalises to the same content as the CRLF
   worktree file, so `git diff` shows nothing and `git status` reports nothing to commit.
   The fix is per-machine and does not sync (there is nothing to commit). Detect via
   `line_endings.check`, offer to run `nightshift.normalize_worktree` or its `nightshift`
   equivalent.
3. **`memory.budget_bytes`.** Do **not** propose the current size as the budget. Doc 10 §4's
   lesson is that a budget picked to fit is the move the gate exists to stop. Propose the
   gate *disabled* with a note, or propose a round number the maintainer states out loud.

**`nightshift doctor`** is the same discovery code run against an existing manifest, reporting
drift: a renamed package, a moved test dir, an integration branch that no longer exists.
Cheap, because it is the same function, and it is what stops a manifest going stale the way
every other hand-written config in this tree has.

**Five of those checks are already live — in `preflight`, not in a CLI** (`nightshift/doctor.py`,
2026-08-01, `per-machine-preconditions-are-unchecked`). The rest of step 5 is unbuilt, but the
precondition half was useful before the `init`/`doctor` commands exist, and a check nobody has a
command to run is a check nobody runs — so `preflight.run_checks` calls `doctor.checks(root)`
**first, ahead of the gates**, and these five report there on every push:

1. **LF working tree** — `gates.line_endings.check`, i.e. item 2 above, now enforced rather
   than only offered at `init` time. First in the list because a CRLF worktree fails the gate
   once per file, and no other signal is legible under 304 of those.
2. **`claude` on PATH** — the project's own `runner.claude_binary()` through `bridge`.
3. **This hostname is in `.ai/hosts.json`** — an unlisted box is the `_TODO-desktop-hostname`
   incident: nothing errors, and every card with a `requires:` silently stops dispatching.
4. **The bridged project modules resolve** — `preflight.integration_base` and `preflight._suite`,
   probed up front instead of failing deep inside the pytest step. Goes away with `bridge` in §8 step 4.
5. **Which `nightshift` commit is installed** — reported, never enforced. Karel's call, 2026-08-01:
   a hard pin is what §3 above defers ("pin to a tag once it settles"), so the SHA and the
   checkout's dirty state are printed and nothing fails on them.

When the CLI does arrive, it calls this module rather than growing a second copy.

### What `init` actually has to write — measured, 2026-08-02

Not estimated. A synthetic empty repo (`myapp`, one module, one commit) was taken from
`pip install` to *one dispatchable card* by hand, and this is the complete list of what
had to be written for that. Six files and one directory tree; nothing else was missing.

| # | What | Why it blocks |
|---|---|---|
| 1 | `.ai/manifest.toml` — `[project].source_dirs`, `[branches].integration` + `stable`, `[tiers].binding_doc` | `integration` is never defaulted by design |
| 2 | `.gitattributes` with `* text=auto eol=lf` | fires on the **first commit** on Windows — `git add` warned "LF will be replaced by CRLF" before anything else ran |
| 3 | `.ai/corrections.log` | `corrections_log` fails on its absence: "every §15 metric depends on it" |
| 4 | `.ai/gates/data/corrections_vocab.json` | ditto. **Must be the empty seed `{"class": {}, "channel": {}, "gate": {}, "disposition": {}}`** — copying this repo's vocabulary produces 18 violations, because the gate requires every declared class to be *used*. That is §7's "do not ship this project's rules" enforced mechanically, and it means the seed is empty by construction: a class arrives with the first entry that needs it |
| 5 | a document with a ```` ```tier-binding ```` block | the runner refuses to start without one, and must — §16 forbids guessing a model |
| 6 | `Board/<lane>/` for every lane in `board.LANES`, plus `.claude/agents/<worker>.md` for each worker a card names | `card_schema` checks the charter exists; the runner checks the lane does |

Two results worth keeping. **The gate suite is green on an empty repo** once 1–4 are
there: 17 of the shipped gates run, 0 violations, the i18n family correctly inert with no
adapter. And **every refusal on the way was legible** — each one named the file and the
missing thing, except the tier binding, which named a Project Tigress plan doc; that is now
`[tiers].binding_doc` and the message names the key (`deferral-note-nobody-collected`).

What `init` cannot write, and what therefore keeps the system from being self-bootable
today, is the `.claude/` half: the hook wiring in `settings.json` (7 entries, all
`python -m nightshift.hooks.*`), the four core charters, and the three operator skills.
None of them ship in the package, so a second project obtains them by copying out of this
one. **They are close to portable already** — measured the same day: `log-a-correction`
names Project Tigress zero times, `code-reviewer.md` zero, `run-the-runner` and `manage-board`
twice each, `code-thread.md` and `stale-hunter.md` once each. So this is a `templates/`
directory and a handful of substitutions, not a rewrite — but it is step 5 work, and until
it exists "install the package" is not the whole install.

---

<!-- stale-ok: `readme_generated` is the gate this section specifies. A forward reference in a
     spec is not staleness — naming the gate is how the section states what to build. -->
## 6. `README.md`

Human-facing, and distinct from the skills. The skills (`run-the-runner`, `manage-board`,
`log-a-correction`) are instructions **for the agent**; the README is for **the person who
just installed this and has to operate it**. That distinction is what keeps it from being a
D4 duplicate — but only if the lists are generated.

**The shape is a one-page quickstart, then reference.** Karel's constraint, 2026-07-30: *"If
each of the 15 topics is longer than 1 paragraph, no one will read anything from it. Most
important info must fit into one page at the beginning."* He is right, and the failure mode is
specific — a reader who has to scroll to find `pip install` concludes the thing is heavy before
finding out whether it is. So:

### Page one — the whole of it, above the fold

Five blocks, **no block longer than a short paragraph or a five-line code fence**. The test:
someone who reads *only* this page can install, run one card, and stop — without scrolling.

1. **One sentence on what it is**, and one on what it is not: you get the machinery for
   earning gates, not an earned gate suite.
2. **Install** — the literal commands. `pip install`, then `nightshift init`, then what the
   interview will ask you to confirm (the integration branch, and the two things it will never
   guess: capabilities and permission mode).
3. **Use** — the three commands that are the whole daily loop:
   gates run themselves on save via the hooks, `preflight` before every push, `runner` for a
   night. One line each.
4. **First real run** — `--dry-run`, then `--card <id> --max-cards 1`. Two lines, because this
   is the step where a wrong first move costs money.
5. **The one warning** — `permission_mode: bypassPermissions` is what a code card needs, and
   *it is the whole machine, not a sandbox*. Quoted verbatim from `hosts.json`, on page one and
   not buried in a reference section, because it is the only item where not reading it has a
   cost that is not recoverable.

Then a table of contents into the reference, and nothing else.

### Reference — below the fold, and allowed to be long

Nobody reads these until they need exactly one of them, so depth is free and being scannable
matters more than being short. In this order, because it is roughly the order questions arrive:
the board and what a card looks like; running triage by hand on one `inbox/` note (and why
parking in `needs-decision/` is a success state); every runner flag; `hosts.json` in full;
budgets and stop conditions (`--budget`, `--card-budget`, the consecutive-failure and
no-progress stops, `.ai/STOP`); reading the digest and answering `needs-decision/`; the
corrections log — format, appending by hand, clustering; the gate list, writing your own, and
the three-level appeal process; the staleness sweep; what you do not get (asset generation, a
pre-earned gate suite, non-Python support); and troubleshooting — the CRLF case, an unlisted
hostname, a `permission_mode` too tight to run Bash.

<!-- stale-ok: `readme_generated` is the gate this section specifies — a forward reference in a
     spec, re-marked here because §6's sub-headings end the marker above it. -->
**Generated blocks:** the runner's flags, the shipped gate list, and the manifest field table.
A new gate, `readme_generated`, regenerates and diffs them; it is an `_INFRA_GATES` row and it
ships with core. Page one is hand-written prose and is *not* generated — it is the one part
where wording is the product.

---

## 7. What ports, and what does not

Stated plainly because it is what a colleague needs to hear before installing, and what Karel
needs before planning project #2.

**Works on day one in a Python repo:** the gate runner and its 16 universal gates; the four
hooks; the board, card schema, reconciler and digest; preflight and merge_check; the
corrections log; the four core charters; the runner, given `hosts.json` and a budget.

**Works once someone answers the interview:** the test slice, doc scanning, memory freshness,
layering. Minutes, not days — but not zero.

**Works once someone writes ~30 lines:** the three i18n gates, behind the §2b adapter. The
only place in the system where a project's *data model* differs rather than its paths, and
therefore the only place a manifest field is not enough.

**Does not port, deliberately:**

- **The gate suite as a value proposition.** `00_architecture.md` §15's rule is that gate
  selection follows observed failures. A new repo starts with 16 generic gates, an *empty*
  audit matrix and an *empty* corrections log. The 55-rule matrix and 58-entry log here are
  this project's earned evidence, and shipping them would be shipping someone else's history
  as someone else's rules.
- **Asset generation.** ComfyUI, the Nunchaku models, the machine-wide generation client under
  E:\AI\scripts, `~/.claude/imagegen-stack.md` and `~/.claude/audiogen-stack.md` are all *already*
  machine-wide, in user scope, outside this repo. What is project-bound is the style block
  and the output layout inside `pixelart-asset` (17 project references in 270 lines) and
  `audio-asset` (18 in 300). **Consequence worth acting on: porting asset generation to
  Karel's next project needs nothing from this session** — it is copying a skill and
  rewriting its style section. Ship those two as `templates/`, and do not model them as part
  of the framework.
- **The plan docs.** `.claude/memory/ai_team/` is ~5,600 lines of *record*, not product. A
  colleague gets the README; the reasoning stays here.

---

<!-- stale-ok: the steps name the `nightshift` files each one creates. Same reason as §3 — a spec's
     step list is a list of things that do not exist yet. -->
## 8. Order of work

Sequenced so the framework is proven against a second consumer before this repo depends on
it, and so nothing is deleted here until its replacement passes the same tests.

1. **Stand up `nightshift`** — repo, `pyproject.toml`, `nightshift/manifest.py` with the schema and
   defaults. Nothing moved yet.
2. **Move the zero-coupling modules first** — `corrections.py`, `limits.py`, `tiers.py`,
   `textio.py`, `preflight.py`, `merge_check.py`, `run_record.py`, plus `gates/base.py` and
   `gates/run.py`. Their tests move with them. Project Tigress installs the package and imports
   from it; `.ai/` copies are deleted only once the suite is green.
3. **Move the 16 gates and the 4 hooks.** Rewrite the `settings.json` commands. `_INFRA_GATES`
   becomes the core-gate registry it already almost is.
3b. **Move the three i18n gates behind the §2b adapter**, leaving `i18n_common` here as
   Project Tigress's adapter. Do this early: it is the one seam that is an interface rather than a
   table, so if the interface is wrong it is better to find out on the cheap gates than on the
   runner.
4. **Move the constant-holders behind the manifest** — `suite.py`, `doc_scan.py`,
   `branches.py`, `board.py`, `reconcile.py`, `digest.py`, `runner.py`, `night.py`,
   `stale_sweep.py`, `audit.py`. This is where the ~40 lines become manifest fields.
   **Done 2026-08-02.** All ten moved, plus the three doc gates that depend on
   `doc_scan`; `bridge.py` deleted with the last module it reached for. The new
   tables are `[branches]`, `[worker]` and `[audit]`; `[board]`, `[memory]` and
   `[layering]` are still unwritten because nothing reads them yet, which is the
   rule §4 states. Two findings worth the space:
   - **The checklist's first item fired four times, exactly as predicted** — in
     `test_board_runner._repo`, `test_self_improvement._git_repo`,
     `test_staleness_reproducibility.repo` and `test_gate_branch_role_prose._repo`.
     Every one was a fixture that had been getting a hardcoded default for free and
     now had to declare it. The `_repo` case is the sharpest: without
     `[worker].harvest_dirs` the art-card tests reported *"the worker produced
     nothing"* — the exact confusing failure `harvest()` exists to prevent,
     reproduced by the config rather than by the code.
   - **Two hooks were parsing a module that moved.** `worktree_fence` AST-parsed
     `runner.py::FENCE_ENV` and `ideas_fence` AST-parsed `board.py::LANES`, both
     for the same stated reason — "`.ai/` is not an importable package, and a hook
     must not depend on it being one". That reason expires the moment the module
     ships in the package, so both now read the real thing (`[worker].fence_env`
     and `board.LANES`). Neither was in the checklist and neither was found by a
     type checker: `ideas_fence` surfaced only through its own test, and it fails
     *open*, so in production it would have silently disarmed the fence.
   - **Their tests moved too**, on a second pass the same day. The mechanism halves
     of `test_staleness_gates.py`, `test_staleness_reproducibility.py` and
     `test_hook_ideas_fence.py` now live in the framework suite, split the same way
     `test_board_suite.py` was: synthetic mechanism to `nightshift`, real-tree
     grounding left in this repo. The grounding is the half that cannot move and the
     half most worth keeping — "the gates are clean on HEAD", "the eight names
     deleted in e2e2f51 still do not resolve", "the fence really arms here". Without
     it every framework test would pass just as well against a scanner that had
     stopped seeing this tree. 357 tests there, 2110 here.
   Two things bit on the first module through this step and will bite on every one of
   these, so they are checklist items rather than advice:
   - **A hardcoded default moved behind a manifest field changes behaviour for every
     caller that supplies no config** — and a synthetic test fixture is exactly such a
     caller. `appeal_markers.scan()` narrowed from four source dirs to `.ai` alone when no
     `manifest.toml` was present, which silently unhooked `test_gate_dispatch_reachability`'s
     marker (`appeal-scan-default-narrowed-behind-manifest`). Either keep the no-manifest
     fallback behaviour-identical to the constant it replaced, or find every fixture relying
     on the old implicit default and make it declare the config.
   - **Grep for callers that build the module's path as a STRING** before moving it. Neither
     the type checker nor the suite sees those; it bit twice on 2026-07-31
     (`fixture-created-the-file-production-lacked`, `ran-the-fix-from-a-checkout-without-it`).
5. **`nightshift init` + `doctor`**, discovery first, against **three** subjects: an empty repo
   (D6), the second project, and this one — where init must reproduce the hand-written
   manifest from step 4, and any field it gets wrong is a finding about the discovery rather
   than a field to hardcode.
   **Done 2026-08-02.** `discover.py` proposes, `init.py` confirms and writes,
   `doctor.drift()` re-runs the same survey against a written manifest, and
   `nightshift init` / `nightshift doctor` are a console script. The second project
   does not exist yet, so the third subject is `nightshift`'s own checkout — a real
   Python repo with a pyproject and a package whose name is not the directory's.
   Discovery reproduces **every** high-confidence field of this repo's hand-written
   manifest; the one it gets wrong is `branches.integration`, which is the one field
   it is never allowed to answer alone. Four findings:
   - **The `.claude/` half turned out to be the deliverable**, not a footnote to it.
     `templates/` ships the four core charters, the three operator skills, the seven
     hook entries, a `CLAUDE.md`, the board contract, the memory stubs, the seed
     corrections log and vocabulary. Rendered by `str.replace` over five tokens; the
     maintainer's name is one of them, but **dated verbatim quotes keep their
     attribution to the origin project** — re-signing one with whoever installs this
     would be a lie inside a document whose subject is honest records.
   - **A core gate was prescribing a remedy only this repo had.** `line_endings`
     told every consuming project to run `.ai/normalize_worktree.py`, once per CRLF
     file. The module is pure framework — zero project references in 232 lines — so
     it moved, and the message names `python -m nightshift.normalize_worktree`. Same
     shape as the tier-binding coupling, found the same way: by initialising an empty
     repo and reading what it actually said.
   - **The templates had to survive their own gates**, and did not at first. A
     charter's worked examples are another tree's `file:line` citations, which is
     precisely what makes them dangle in the target — so they carry `stale-ok`
     markers with reasons. The template header backticked `{{project}}`, which
     renders to a project name and dangles wherever the package is not the directory.
     Both caught by `test_an_initialised_empty_repo_passes_the_gates`, which is the
     test worth having: the gates run on what init writes, in the repo init wrote it
     into.
   - **`init` overwrote a hand-written manifest**, briefly, because `[tiers].binding_doc`
     is decided mid-pass and an earlier version wrote the file directly to capture the
     late value instead of going through the same "keep what exists" path as everything
     else. Caught by `test_an_existing_manifest_is_never_rewritten` — which is why the
     "nothing is overwritten" rule is asserted rather than stated.
6. **The README**, with the generated sections and the `readme_generated` gate.
   **Done 2026-08-02.** `README.md` is now the page-one-then-reference shape
   §6 specifies: one screen of prose (what it is, install, the three-command
   daily loop, first real run, the one warning), a table of contents, then
   twelve reference sections in the order §6 lists them. Four findings:
   - **Four generated blocks shipped, not three.** §6 named gate names, runner
     flags and the manifest field table; lane names is D4's own fourth example
     and cost nothing extra once the mechanism existed, so `board-lanes`
     joined `gate-list`, `runner-flags` and `manifest-fields`.
   - **Every block's source of truth already existed and needed no new
     hand-maintained list.** `nightshift.gates.run.discover()` for the gate
     table, `board.LANES` for the lane table, and a new `manifest.schema()`
     (the one addition — `_KNOWN`/`_KNOWN_TABLES` were already the real
     schema, just private) for the manifest table. The only genuine
     workaround: `runner._parser()` resolves `--base`'s default through
     `[branches].integration`, which this package's own checkout — a
     framework, not a consuming project — does not declare, so
     `readme_gen.runner_flags()` falls back to a synthetic one-line manifest
     in a temp dir when the real root has none.
   - **The mechanism generalises past this package's own README on the first
     try, not by design intent alone.** `gate_list(root)` takes the real repo
     root rather than a synthetic empty one specifically so that a consuming
     project adopting the same marker convention gets its *own* `.ai/gates/`
     entries in the table alongside core's — `test_gate_list_picks_up_a_projects_own_gate`
     locks this rather than merely asserting it works on `nightshift` itself.
   - **`readme_generated` is config-free in the sense D3 actually defines,
     not merely in name.** It scans for `<!-- generated:NAME -->` markers and
     no-ops when none are found, so it runs identically — and silently — in
     the second project and in an empty repo (D6) until either adopts the
     convention. Today only this package's own README does.
6b. **Review before step 7** (2026-08-02, unplanned). The whole system was read and *run*
   — a synthetic second project taken from `pip install` to a dispatchable card — before
   trusting it against a real one. Fifteen findings, and they were not fifteen mistakes;
   they were **one mistake with fifteen faces**, which is the finding worth keeping:

   > **The framework had no `.ai/`, so nothing gated the framework.** Every consuming
   > project was checked by rules that lived in a repo those rules did not apply to.

   Seven of the fifteen fall straight out of that. Five gates still carried
   `_ROOT = Path(".ai")` — the directory 16,800 lines had moved *out* of — so in
   Project Tigress they covered 19 small files instead of the orchestrator that earned them,
   in a consuming project only that project's own gates, and in `nightshift` nothing at
   all. `run_stop_recorded` targeted `.ai/runner.py`, deleted at step 4, and its
   "no such file → no violations" opener had it green while reading zero bytes
   everywhere. `deletion_sweep` filtered on the literal `"project_tigress/"` and
   `doc_signature_drift` built its symbol table from `("project_tigress",)`: both provable
   no-ops in any other repo. Three CLIs — `reconcile`, `digest`, `stale_sweep` — took the
   repo root from `__file__`, so the two documented board commands operated on the
   *framework* checkout from inside any project; `reconcile` answered "Board is
   consistent" for a board it had never looked at.

   Every one of those is `check-stopped-checking`, and every one fails **open**. That is
   the through-line: a scope that stops matching reports fewer violations, and fewer
   violations is indistinguishable from success. Three corrections were logged
   (`gate-scope-outlived-its-directory`, `entry-point-root-derived-from-file`,
   `schema-field-nothing-read`).

   Two findings are about this document rather than the code. `[memory]` and
   `[layering]` sat in the schema, validated and documented, with **nothing reading
   them** — while §2 above had already caught that contradiction as it was being written
   ("the manifest contradicted the table, in a document written in one pass") and filed
   it as evidence rather than as work. And `init` was *writing* a `[[layering.forbid]]`
   rule into fresh repos unasked, because two of the three `CONFIRM` fields were being
   accepted silently by a function whose docstring said there was only one.

   What shipped: `[project].tooling_dirs` + `gates/scope.py`; the three manifest-table
   gates promoted to core; `card_schema` reading `[board].root`; `Tiers.binding_doc`
   defaulting to a path a consuming repo will actually have; the shipped operator skill
   detokenised; plan-doc citations out of every user-facing string; and — the fix that
   subsumes the rest — **`nightshift` has its own `.ai/manifest.toml` and is gated by
   itself**, 21 gates green, with `tests/test_self_gating.py` asserting both that it
   passes and that the scope really reaches the package. `doctor` skips the three
   dispatch preconditions where there is no board, which is what lets the framework be
   its own subject without pretending to be its own consumer.

   **Do not read this as step 7 being unnecessary.** Everything above was found by
   reading and by a synthetic repo. Steps 7 and 8 are where a *real* second project and
   a *real* colleague find the things neither of those can.

7. **Stand it up in the second project for real** — one card end to end, `--card --max-cards 1`.
   **Done 2026-08-05**, from both directions on the same day, which is what makes it
   credible rather than anecdotal:
   - **The real second project.** Karel installed `nightshift` into his own second repo and
     ran a real card through a full cycle. It completed, and it returned three findings this
     repo could not have produced: the `.obsidian/workspace.json` dirty-tree blocker
     (`looking-at-it-broke-it`, fixed the same day), stray CRLF in that project's own
     `tools/trace_*.py`, and a corrections-log pattern worth a gate. The first of those is
     the step's whole point — a vault-as-repo-root assumption that is invisible from inside
     the origin project, because here the file was already committed.
   - **A watched, attributable account** of the same cycle, run in a controlled synthetic
     repo (`Board/done/nightshift-runner-unproven-end-to-end.md`; the transition-by-transition
     narrative is in `.claude/memory/ai_team.md`). Every invocation of `runner.py` before this
     had been `--dry-run`, so the largest claim in the framework was the least exercised code
     in it. It held through dispatch, wip-banking, gates, the test slice, the card's move to
     `review/` and the digest, and failed at the code-reviewer's worktree cut — root-caused to
     a missing `__pycache__` ignore plus Windows `MAX_PATH`, filed rather than fixed in place
     as `nightshift-worktrees-never-ignore-pycache` and
     `nightshift-worktree-paths-not-defensive-on-windows`. `merge_check` reproduced the same
     failure independently, which is what proved it lives in worktree creation generally and
     not in the runner's reviewer path.
8. **Ship to one colleague** and watch the install without helping. Whatever they get stuck
   on is the README's next section.
   **Postponed on purpose (Karel, 2026-08-06)** — deferred, not dropped, and deliberately not
   counted against "the plan is done". The two open worktree cards above are the honest reason
   to hold it: handing someone an install whose known Windows failure mode is already carded
   spends the one-shot observation on a defect we can name ourselves. Step 8's value is the
   things *neither* the origin project nor a second install of Karel's own can see, and that
   value keeps.

Cost, stated rather than discovered: this is larger than one session. Steps 1–3 are
mechanical and safe. Step 4 is the real work. Steps 7–8 are where the design is actually
tested, and they cannot be compressed.

---

<!-- stale-ok: acceptance criteria name this session's own deliverables — `nightshift init`, the
     manifest, the `readme_generated` gate. Unresolvable by construction until K ships. -->
## Acceptance

- `nightshift` installs from a git URL and `nightshift init` runs in a repo with no manifest.
- Discovery proposes every high-confidence field correctly on all three subjects, and
  **never** proposes a capability or a permission mode.
- D6's bar met in an empty repo: gates pass, a card moves through four lanes with a commit
  per move, the runner refuses a forbidden base, a preflight writes a receipt.
- `nightshift init` reproduces this repo's hand-written manifest, with every mismatch reported.
- Project Tigress's `.ai/` contains only the manifest, its own 11 gates, its corrections log —
  and its suite is green with no copies of the moved modules left behind.
- One card runs end to end in the second project.
- `README.md`'s page one fits on one screen and is sufficient to install and run one card
  without scrolling — verified by having someone do exactly that, not by counting lines. Its
  generated blocks match a fresh generation, enforced by `readme_generated`.
- The charters name which side they are on (D5), all seven of them.

<!-- stale-ok: the prohibitions name this session's deliverables (`readme_generated`) and §8's
     superseded `.ai/core/` sketch — a path that deliberately does not and will not exist. -->
## Do not

- Do not copy `.ai/core/` into a second project. That is §8's sketch and it is the thing this
  doc replaces; a copy cannot merge and has no version.
- Do not ship this project's corrections log, audit matrix, or `feedback_*.md` files as
  another project's rules.
- Do not let discovery infer `capabilities` or `permission_mode` (`hosts.json`, Karel
  2026-07-23).
- Do not propose the current orientation size as the budget (doc 10 §4).
- Do not retype a generated list into the README — the `readme_generated` gate exists to make
  that fail. That is the D4 defect, and it has already happened twice in this tree.
- Do not model asset generation as part of the framework. It is a skill template over a
  machine-wide stack that already lives in user scope.
- Do not delete a module from `.ai/` before its `nightshift` replacement passes the tests that
  travelled with it.
