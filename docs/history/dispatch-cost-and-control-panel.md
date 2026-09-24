---
doc_scope: history
---

# Dispatch cost, routing, and the control panel

**Note, not a card — this is a programme that will spawn several.** Written from a design
session with Karel on 2026-08-14, prompted by one measurement: a
triage run took **~1 hour, most of a session window, and produced 2 cards**. The runner's
own telemetry on `menu-art-start-run` shows the worker that consumed one of those cards
cost **8.8 min on haiku+sonnet, $1.20 equivalent**. Briefing costs ~50× the work. That
inversion is the problem this doc exists to fix.

Nothing here is implemented yet. This is the plan, in order, with the evidence already
gathered so a cold session does not re-derive it.

## 1. What is actually wrong

**Triage fuses two unrelated jobs** — *investigate the codebase to find the fork* and
*emit a well-formed card* — and every note pays for both. Only the first needs a lead-tier
model, and most notes do not need it at all.

**`triage.md` has no `model:` field**, only `tier: lead`, so it inherits the session model
(Opus) and then follows an unbudgeted exploration mandate (*"Run the second-order lens on
every card: what does this ripple into?"*). That is where the hour goes.

**Triage cost lands on the wrong resource.** The limit is a *rate*, and triage and the
night draw the same bucket serially with triage first — so triage does not merely cost
tokens, it *delays the night*, which is the only capacity that costs Karel nothing.

**Measured waste on the board (2026-08-14).** `no-ammo-sound-to-silent.md` is 13.7 KB with
`worker: none, unattended: false` — the runner cannot dispatch it, ever. Both
`fix-stale-claude-memory-*` cards are `unattended: false` likewise. Three of eleven
`tasks/` cards received full triage output and can never run at night.

## 2. The routing model

Two independent questions, not one axis. Karel had been treating them as one:

- **Does the note contain a fork only Karel can resolve?** → decides whether *triage* pays.
- **Is the execution long and unattended-shaped?** → decides whether *the night* pays.

|              | short execution                | long execution                                    |
|--------------|--------------------------------|---------------------------------------------------|
| **no fork**  | inline, or a one-line card      | card, but **skip triage's judgment** — a thin brief is enough (asset generation lives here) |
| **fork**     | **inline** — ask, decide, do    | triage → night. The real customer, maybe a third of the inbox |

Three routes out of `inbox/`: **inline** / **scribe** / **triage**.

**Bias hard toward inline.** Misrouting to inline costs nothing — Karel is present, so a
surfaced fork gets answered in seconds. Misrouting to triage costs an hour and a window.

**Inline notes never become cards.** A card for ten minutes' work is the overhead being
removed: it would need a lane move, a close-out, a review entry. They stay as notes; the
classifier's report is the worklist; the note is deleted or moved to `done/` with the work.

## 3. The work, in order

### 3.1 The scribe charter + the classifier

**Classifier.** Reads every note in `inbox/` — **notes only, no source** — and buckets each
into inline / scribe / triage with one line of reasoning. Reports *before spending anything*
so Karel can override; he wrote the notes, so he is the best judge of which hide a fork.
Cheapest useful single output is **"can the runner actually dispatch this?"** — an
`unattended: false` / `worker: none` outcome should never reach triage, and that needs no
source reading at all.

**Scribe.** Takes an already-elaborated note, emits frontmatter + `## Intent` +
`## Approach`/`## Subject` + card-specific acceptance. Reads the note and the schema,
**reads no source**. Pinned to haiku or sonnet explicitly. Minutes, not an hour.

**Scribe must be able to bounce.** If acceptance criteria cannot be written without knowing
how the code works, it stops and re-routes to triage rather than inventing plausible
criteria. A confidently wrong acceptance block is worse than no card — it runs overnight and
produces a green diff nobody meant. The bounce happens before any expensive reading, which
is what makes biasing toward scribe safe.

**Triage stays as-is** but becomes rare, and is never auto-dispatched by ingest — it emits a
queue Karel launches when and where he chooses (§3.4).

### 3.2 Charter cleanups

**Move generic acceptance into the charters.** `menu-art-start-run.md` carries "clean pixel
art: no anti-aliased fringing, no off-grid pixels, clean alpha edges" — true of *every* art
card. That belongs in `art-reviewer.md` once. Same exercise for `code-reviewer.md`. Cards
keep only what is specific to the one asset or change.

**Enforce the rule triage already has.** `triage.md` §"What the gates already prove — do not
hand-check these" is currently advisory prose losing to several sections that demand
thoroughness. `menu-art-start-run.md`'s `## Acceptance` hand-lists "exactly 160×96, with
transparency, lands in `.tmp/` first" — all three checked by `asset_hygiene`. Its own output
violates its own rule.

### 3.3 Recalibrate the findings bar

`triage.md` says *"a finding earns its place by saving the worker a tool call."* That bar was
set when the worker would be a local model, where a tool call was expensive and might come
back wrong. It is now wrong in a specific, expensive way: **the worker must open the files to
edit them regardless**, so an enumeration of call sites is discovered twice and written down
once for nothing. `grid-distance-metric.md` is 31 KB and its two largest sections are exactly
that enumeration.

New bar: **a finding earns its place if the worker would plausibly get it *wrong*, not merely
spend a call to get it.** "The metric is Chebyshev because Karel decided so on 2026-08-14"
survives. "Here are the 14 call sites" does not — grep finds those and grep does not
hallucinate them. Triage may *name* files; it must not transcribe or enumerate what a grep
answers.

### 3.4 Control panel v1

**Why it is worth building at all:** Karel's current interface to the machinery *is a Claude
session*. "Run the classifier", "start the night", "move that card" each cost session window
for pure mechanism. A panel button costs zero tokens. Given that the bottleneck is limits
and not capability, that is the whole case.

**It is a launcher, a registry and a tail — never a chat client.** The moment it hosts a
conversation it needs an API key and the subscription question becomes real; by not going
there, it never does. `runner.py` already captures `session_id` from each worker's result
JSON and already has a `claude --resume <session_id>` path, so "talk to this one" is a button
that hands over the resume command.

**Its name is the Command Center**, American spelling, settled by Karel 2026-08-14 after
trying both. *Control* was rejected on meaning rather than taste: a control panel is where
you adjust a running system's settings, and this is a launcher — the verbs are decide,
dispatch, play, send tonight. The module should **not** be called `command`, which collides
with CLI vocabulary in a framework whose whole surface is commands; `nightshift.panel`.

#### What it is, technically — settled 2026-08-14

**One Python file using stdlib `http.server`, one HTML file, and a `.bat` that opens the
browser.** No dependencies, no npm, no framework. Karel's constraint was *"simplicity is
welcomed (more simple = more likely to be used)"*, and he asked whether a plain HTML page
with JS could do it. It cannot: browser JS has no way to run Python or read `Board/` — that
is the sandbox, not a missing library — so buttons that actually dispatch need a process
outside the browser. A local server is the smallest thing that has one. The alternatives
were weighed and rejected: a Textual TUI has a lower aesthetic ceiling for the same work,
and a Python desktop app means either tkinter (cannot be made to look good) or pywebview (a
dependency that still renders the same HTML).

**The server owns no logic.** Every button POSTs to a verb that already exists as a CLI
command — that is the point of "CLI first, panel second", and it is what keeps the panel
thin enough to be worth having.

#### Five pages, and why it is pages rather than sections

Sections competed for one screen, and worse, they mixed two modes Karel keeps separate
(*"my working and testing sessions are usually quite separated"*).

| Page | Holds | Actions |
|---|---|---|
| **Now** | Decide (`needs-decision/`) · Do now · Tonight · Waiting on triage | answer, start a session, dispatch, launch triage |
| **Verify** | `testing/` grouped by `surface:`, plus anything stuck in `review/` | diff, **Mark OK** (one card, immediately), tick many + save, launch the game |
| **Inbox** | unclassified notes *and* the routing view | new note, edit a note, classify, write the card |
| **Ideas** | the private lane, names only | new, **edit in place**, promote to inbox |
| **Run** | the live roster and earlier runs | open the log, stop after this card |

The words *triage*, *runner*, *scribe*, *classifier* never appear as page names. The rest of
the settled detail:

- **The live run is ambient; the roster is not.** A one-line tally (`3 landed · 1 back in
  tasks · 3 queued`) plus the current card, model, attempt, elapsed and phase sits above
  every page, because that is the question Karel asks most. The full per-card roster moved
  to its own page after being tried in the rail: *"I need this data only from time to time,
  not always visible"* — it blocked the content of every other page.
- **The roster is the digest, live.** Card, lane it landed in, wall time, cost, and what the
  reviewer said. A failure names its tests and whether it gets another attempt tonight,
  which is precisely what a morning digest tells you too late.
- **Do now absorbs the cards the night cannot take.** Karel, 2026-08-14: *"the cards not to
  be dispatched — they do belong to the inline bucket, don't they?"* Yes. `unattended: false`
  and `worker: none` mean "needs you present", which is the same list as the inline notes.
  One distinction survives: `requires: gpu-box` on the laptop is not inline, it is *Tonight,
  on the other machine*. So Tonight shows only what will really run here, and says what it is
  leaving out — the three cards that had been invisible on the board for weeks.
- **Drag to reorder, within a lane only.** It writes `kanban_order`, which exists in
  `card_schema` *because* Obsidian's Bases writes it when a card is dragged — so the panel
  and Obsidian share one ordering field rather than growing two that can disagree.
- **Per-card dispatch, plus a subset.** Every queued row has its own dispatch button, and
  checkboxes choose what a run takes. A "take first N" slider *sets* the checkboxes rather
  than selecting in parallel: one selection model, two ways to drive it, because two
  competing selections make it unclear what a button will run.
- **Three meters per account** (§4), and dispatch gated on headroom, with one override
  checkbox covering both waivers — `--allow-paid` and the account exception — since both are
  per-click and neither is persisted.
- **Framework freshness is ambient, not a page.** §3.6a already says the panel shows it; the
  page table above never carried it, which is exactly how it would have been lost by whoever
  builds from the table. It belongs beside the run tally in the always-visible rail, because
  it is a precondition for every dispatch on the page below it, and because a failing
  `paired-branches` means the panel is looking at a repo whose green suite means nothing.
  `freshness.describe()` is already **one line** and already declines to nag, so it fits the
  rail as-is; the pull offer is a button on that line. **Do not fetch on every page load** —
  `freshness.read()` fetches with a 15 s timeout, so the rail reads `fetch=False` (whatever
  the last fetch left) and a Refresh button does the fetching. That split is not just
  performance: it puts §3.6's read-only/decision boundary on screen, where fetch is a click
  and pull is a different click.
- **Replaces the digest.** Do **not** invest in a digest text rewrite first — the *content*
  thinking transfers unchanged; only the rendering would be thrown away.

#### The account — say which one, and let it be switched (added 2026-08-14)

<!-- stale-ok: same class as §4 — every name below is EXTERNAL. `oauthAccount.*` are keys in
     Anthropic's own `~/.claude.json`, and `CLAUDE_CONFIG_DIR` is the CLI's env var, read out
     of the shipped extension bundle. None of it can exist in this source tree. Resolves when
     item 13 lands a real reader here and these names become ours. -->

Neither of these was in the design; both were asked for after reading it, and the second one
**falsifies §5's provisional answer.**

**The panel always says which account it is on, on every page — identity, not meters.** The
three meters answer *how much is left*; they never answer *left on what*, and with a hard
exclusion in force (below) the account in play is the first thing that has to be visible. It
is a **local read and needs no network**: `~/.claude.json` → `oauthAccount.{emailAddress,
accountUuid, organizationUuid, hasExtraUsageEnabled}`. `usage.Snapshot.subscription` carries
the plan tier and no identity, so this is a second read beside it, not a field to add there.

**`hasExtraUsageEnabled` is the API-spend signal, offline.** It is the same fact as the
endpoint's `extra_usage.is_enabled` (§4) — `true` on this account, matching the €72.12 the
probe found — but readable from a local file, before any HTTP call and before any dispatch. So
the account §3.4 says must never receive automated work can be identified even when the usage
endpoint is unreachable, which is the case where §5b's fail-open leaves the money rule with
nothing to say.

**Switching: `CLAUDE_CONFIG_DIR` exists, so §5's "account ≈ machine" is wrong.** Verified
2026-08-14, ten occurrences in the shipped extension bundle. Point it at an alternate config
directory and the CLI reads that directory's credential — an account *is* selectable per
invocation. §5 named this exact env var as the thing that would change its answer and closed
the question provisionally without looking; it is there, so the conclusion it guarded — *"Karel
clicks on the machine he meant"* — does not hold, and **the panel needs an explicit selector**.

**Both halves are already reachable with no framework changes.** `usage.read(credentials=...)`
takes a path, so a second account's meters are one call away — only `usage.main()` hardcodes
the default, which is a CLI limitation, not a module one. And `runner._worker_env` returns
`{**os.environ, ...}`, so a `CLAUDE_CONFIG_DIR` set in the panel's own process propagates to
every dispatched worker untouched. Nothing in `runner.py` has to learn about accounts.

**A selector satisfies `feedback_account_dispatch`; it does not bend it.** The rule is that a
meter may *veto* and may never *select*. A selector is Karel selecting, which is the rule's
whole point. What stays forbidden is unchanged and worth restating next to the control that
makes it easy: no default derived from headroom, no fallback to another account on a wall, and
the account in force shown **at the moment of dispatch**, not only on a settings page — a
switcher whose current value is off-screen when the button is clicked is the same failure as
having no switcher.

**What is missing is the config, and it is a prerequisite.** There is no accounts table
anywhere — grep of `manifest.py` and both `manifest.toml` files, 2026-08-14: `dispatch: never`
exists only in `feedback_account_dispatch` and in this doc's prose. **The exclusion is
currently enforced by Karel remembering it.** The smallest thing that makes the veto real is an
`[accounts]` block naming each config dir, its label, and its `dispatch:` stance; it is also
what lets the panel render more than one meter column, since without it there is no list of
accounts to iterate. Note the asymmetry that keeps this honest: config may only ever *exclude*.
`hasExtraUsageEnabled` may veto an account config forgot to mark, and may never promote one.

#### Style — settled 2026-08-14

Black and green, Karel's call, with one rule that makes it readable rather than a parody:
**green is the data colour, not the text colour.** Near-black ground with a green cast,
prose in pale grey-green, phosphor green reserved for numbers, statuses and anything
clickable, and amber and dim red held back for warnings and refusals — so *"paid overage
enabled"* can never render as though it were fine. Monospace for labels and all data, a
humanist sans for prose only; that split does the same work as the colour split. Mockup
approved 2026-08-14 (*"I love the design"*).

#### Cut from v1

The job supervisor and "review this run" as a session launcher — the only parts needing real
process handling. §5 found `claude agents --json --all --cwd` makes them cheap to add later
without redesign. Reading a run's aftermath *is* judgment and is not removable in general;
it just does not have to be in the first version.

**Account selection is Karel's final word, always.** The meters *inform*; they never
*decide*. One account has API spend enabled and must carry `dispatch: never` in config — a
hard exclusion, not a low-priority default, so no amount of headroom can pull work onto it.
That account is for inline work.

### 3.5 Chores — batched one-prompters

**The gap this fills.** §2's routing had three routes and the wrong axes. "Not worth a card"
and "needs Karel present" are different properties, and collapsing them hid a whole class of
work: items that would take one inline prompt each, need no decision, and are only inline
because **the runner's unit of work is a card**, so anything not worth a card cannot be
automated. Karel, 2026-08-14: *"I would like to be able to dispatch batch of small
one-prompters ... without starting another every 5-10 minutes."*

So there is a fourth route: **chore**. The classifier can emit one straight from a short note
— no scribe, no triage, because there is nothing to elaborate. The note *is* the prompt. By
count that is most of the inbox (the 150–500 byte notes).

**A chore is defined by the shape of the request, not the size of the diff.** A note that
says what to change and what the result should be, without asking anything; one obvious place
to make the change, or a mechanically enumerable set; a wrong result that is visible rather
than subtle. **A line-count cap was proposed and rejected** (Karel: *"This doesn't sound
right, it can still be simple"*) — a mechanical edit across three i18n dicts is 200 lines and
trivial, a subtle off-by-one is four lines and hard. Output size does not track simplicity.

**The bounce is the enforcement.** The worker is the only actor that opens the code, so it is
the one that discovers the truth: *if this needs a design decision, or the change is not where
the note implies, stop and park with what you found.* A parked chore costs one dispatch and
tells you the classifier misrouted — which is the feedback that makes the classifier tunable.
Same rule as §3.1's scribe bounce, for the same reason.

**If a mechanical backstop is wanted, measure effort, not output** — a turn or wall-clock
budget per chore. Forty turns means it was not a chore whatever the diff looks like, and the
runner already records turns and wall time per attempt. It doubles as batch protection: a
runaway item cannot eat the night. **One attempt, no retry** — a failed one-prompter is worth
Karel's eye, not a second dispatch, and it keeps the arithmetic honest (8 chores × 3 attempts
is a night; × 1 is an hour).

#### The batch lifecycle — where the saving actually is

Karel, 2026-08-14: *"The preflight takes often more time then handling the task. With 8 small
tasks, that is unefficient."* Verified independently, 8 chores cost 8 full suite runs **and**
8 preflights, because each merge to the integration branch needs its own. That is the cost to
remove.

1. **Per chore — cheap and parallel.** Own worktree as now. On finishing, run only the gates
   (~7 s) and the tests touching the changed files. **Not the suite.** A failure here parks
   the item and drops it from the batch.
2. **Per batch — once.** Merge the survivors serially onto one batch branch, then run the full
   suite **once** over the combined result. One preflight, one merge.
3. **Review once, over the whole batch diff** — not per item. For chores the reviewer's real
   question is *"did any of these do something needing Karel's judgment"*, which is answerable
   across the set, and a batch-wide reviewer can see interactions between items that per-item
   review structurally cannot.

**This is an optimistic scheme and that is a bet.** All-green costs 1 suite run against 8;
one failure costs 1 + a bisect (~4 for eight items, 9 worst case). It wins if batches are
usually green and loses modestly if they are not. On red the runner bisects over the
individual merge commits it already has — it does not guess. **If batches come back red
often, the finding is that the classifier is routing too liberally, not that batching was
wrong.**

#### Verification — aggregated, on the integration branch

**The batch merges to the integration branch and is played there** (Karel, 2026-08-14: *"I
want to test on test"*). That is consistent with the standing rule that the integration branch
is the only place all new features exist at once and is therefore the thing to play —
verification happens over the branch, not card by card.

- **A `review`-verified chore** — a gate, an encoding fix, inner wiring — auto-advances to
  `done/` on green and **never appears on the checklist**. That is what keeps the list short,
  and it is what the existing schema already says.
- **A `play`-verified chore** becomes one row in the panel's *Play/verify* section: what it
  claims to do, its diff stat, a link to its diff, and a checkbox.
- **The batch lands as one entry, and rows group by where in the game you would see them** —
  hub, combat, hack, menus — not by card id. That is what lets one run-through cover several
  items, which is what Karel already does inline.
- Tick what you saw working → `done/`. An unticked row plus a note comes back as a new chore
  carrying the note.

**Cost per batch is one playtest and one checklist, whether the batch held three items or
ten.** Karel's cost stops scaling with item count — the same property that makes triage worth
batching at all.

#### Known weakness

**Attribution under batching.** A chore that reddens the suite only *in combination* with
another is harder to pin than one verified in isolation. The bisect finds which merge did it;
"which of the two is wrong" stays a judgment. Judged acceptable and rare, but it is the real
price of the optimistic scheme.

#### Enabling changes

- `card_schema`: a `kind: chore` that drops the `## Approach` requirement — for a one-prompter
  the intent *is* the approach. Everything else about the card stays.
- The runner: the two-phase verification above, the effort budget, one-attempt, batch-wide
  review, and the bisect-on-red path.
- The worker charter: the bounce instruction, conditioned on the chore kind.

**Most of this is buildable today and does not depend on the panel or the classifier** — thin
cards can be hand-written in the time it takes to describe them. That makes §3.5 the cheapest
high-value item here.

### 3.6 Framework freshness — see it, offer it, never do it silently

**The real problem, Karel 2026-08-14:** *"I now tend to forget to update nightshift when
switching computers."* Two machines, one framework repo per machine, and nothing tells you
which one is behind.

**The instinct was "every automation should pull nightshift first". Do not build that**, for a
reason already on the record. The install is editable, so a framework commit is live in every
consumer the moment it exists — no pull needed at all — and `doctor.framework_version`'s
docstring names the matching failure: *"the failure class that took out a whole night's run
when `nightshift:main` moved under it."* A per-run auto-pull does not prevent the framework
moving under a run; it schedules it. `07_portability.md` §3 is on record with the opposite
plan (*"pin to a tag once it settles"*). And concretely, an auto-pull of the default branch
today would check out away from a paired feature branch and make the new modules vanish
mid-run.

**Nothing pulls today, including the project repo.** Verified 2026-08-14: `fetch` appears only
for targeted branch operations — delete-safety and a pre-push freshness check. There is no
`git pull` of the integration branch anywhere. So "it already pulls the project repo" was not
the case, and the gap is real; it is just not the gap auto-pull fills.

Build instead:

**a. Fetch to know, pull only on acceptance.** Fetching is read-only and cannot move the
working tree, so it is safe to do every time; pulling is the decision. The panel shows whether
the framework checkout is behind its remote, by how many commits, and offers a pull that can be
**accepted or refused**. Refusing is a first-class answer and must not nag.

**b. Refuse to pull into a state that is not a fast-forward.** Dirty tree, or a checkout on a
non-default branch, means the pull is not the routine one — say so and stop rather than
resolving it. This is where a helpful auto-pull would do damage.

**c. A paired-branch check — the one that catches real damage.** `cross-repo-half-landed-alone`
is about state *invisible from either repo*: the framework on a feature branch while the
project is on a different one. Preflight already reads both branch names and reports the
framework SHA; comparing them and refusing an unpaired combination makes that state visible.
Auto-pull would paper over it instead.

**d. Keep the SHA report as it is.** It stays report-only by Karel's own 2026-08-01 call
(`per-machine-preconditions-are-unchecked`); freshness is a *new* signal beside it, not a
change to it.

CLI first, panel second — as with everything else here, the command has to work from a
remote-controlled session before it gets a button.

## 4. The usage endpoint — VERIFIED 2026-08-14

<!-- stale-ok: every name in this section is EXTERNAL — response fields of Anthropic's own
     usage endpoint, minified symbols inside the shipped VS Code extension bundle, and a
     probe script that lives in the session scratchpad, not the repo. None of it can ever
     exist in this source tree. Resolves if/when the panel lands and a real parser here
     carries these names. -->

Found in `~/.vscode/extensions/anthropic.claude-code-2.1.232-win32-x64/extension.js`
(functions `R8t` / `L3`, near the string `/api/oauth/usage`) and then **probed live on
2026-08-14 — it answers.**

```
GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <claudeAiOauth.accessToken from ~/.claude/.credentials.json>
    Content-Type: application/json      # 5s timeout is the client's own choice
```

Live response (this account, 2026-08-14 ~09:40 UTC):

```
five_hour  { utilization: 20.0, resets_at: <iso>, limit_dollars/used_dollars/remaining_dollars: null }
seven_day  { utilization: 15.0, resets_at: <iso>, ...same three null }
extra_usage { is_enabled: true, ... }
spend      { used: { amount_minor: 7212, currency: EUR, exponent: 2 }, limit: null,
             percent: 0, enabled: true, can_purchase_credits: false }
~12 further buckets, all null on this account
```

Gated on `authMethod === "claudeai"` — the fallback string is *"Usage tracking is only
available for Claude AI subscribers"*, so this is the subscription path, not an API-key
feature.

This gives the **denominator**, which `limits.py` deliberately does not have: that module is
*reactive* by design, recognising a wall by regex after a non-zero exit. It stays as the
mid-run backstop; the panel can now avoid most walls up front.

**Three corrections the probe forced on this doc's earlier draft:**

- **`seven_day_sonnet` is `null` on this account.** An earlier draft claimed §3.1's model pin
  would have a payoff watchable in that bucket. It will not — the per-model buckets are not in
  use here. The pin is still right, its effect just shows up in `five_hour`/`seven_day` like
  everything else.
- **Do not hardcode the bucket list.** The response carries roughly a dozen further keys,
  most null, several with internal codenames that mean nothing outside Anthropic
  (`nimbus_quill`, `tangelo`, `iguana_necktie`, `amber_ladder`, `cinder_cove`,
  `omelette_promotional`, `seven_day_cowork`, `seven_day_omelette`, `seven_day_oauth_apps`,
  `seven_day_opus`). **Iterate whatever keys come back, skip the nulls, render what is left.**
  A fixed list of three would have shown a blank panel the day the names shift.
- **The credential file carries more than the token:** `claudeAiOauth.{accessToken,
  refreshToken, expiresAt, refreshTokenExpiresAt, scopes, subscriptionType, rateLimitTier}`.
  `expiresAt` lets the panel pre-empt a 401 instead of discovering it mid-dispatch.

**`extra_usage.is_enabled` / `spend.enabled` is the API-spend signal, and it is `true` on the
account this session ran on**, with €72.12 already recorded under `spend.used`. That is
almost certainly the account §3.4 says must never receive automated work. It means the panel
can **detect** the risky account instead of relying on config alone — but per
`feedback_account_dispatch`, detection may only drive a **veto**, never a selection. Config
stays the source of truth; the endpoint is the safety net for when config is missing or wrong.

Probe script lives in the session scratchpad (`probe_usage.py`), and reads the credential
itself so no token is ever printed. Parse the response in **exactly one function** with an
"if this breaks, re-grep extension.js for the usage path" comment — the same posture
`limits.py` takes about its phrase list.

## 5. Resolved, and what is still open

**RESOLVED — the job supervisor is mostly free.** `claude agents --json` prints active
sessions as a JSON array and its own help says *"for scripting; does not require a TTY"*;
`--all` includes completed ones and `--cwd <path>` filters to sessions started under a
directory. So the panel launches with the background flag and enumerates with
`claude agents --json --all --cwd <repo>`. **The pid table, the polling and the status
plumbing budgeted as "the real engineering" in §3.4 do not need building.** The same
subcommand also takes `--model`, `--effort`, `--permission-mode`, `--agent` and `--settings`
for dispatched sessions — which is how §3.1's model pin and §3.5's effort budget get applied
per dispatch, with no wrapper of our own.

**~~RESOLVED, provisionally — the account is per config dir, not per invocation.~~
OVERTURNED 2026-08-14.** The original reading: nothing in the CLI *surface* selects an account
for one call, and the credential file holds a single OAuth object (§4), so account ≈ machine
and §3.4's final-word requirement is satisfied structurally — Karel clicks on the machine he
meant. It was recorded as not exhaustively checked, naming the one thing that would overturn
it: *"an env var pointing at an alternate config dir would change this, and if one exists the
panel needs an explicit selector instead."*

<!-- stale-ok: `CLAUDE_CONFIG_DIR` is the CLI's own env var, external to this tree — same
     class as §4. Resolves when item 13 lands a reader here. -->
**It exists.** `CLAUDE_CONFIG_DIR`, ten occurrences in the shipped extension bundle. The
consequent therefore applies as written: **the panel needs an explicit selector**, structural
satisfaction is gone, and §3.4's *"The account — say which one"* replaces this entry. Worth
noting how cheap the falsifying check was — one grep of a file §4 had already located and
opened — against a conclusion that would have shaped the panel's whole account story. A
provisional resolution that names its own falsifier and then does not run it is a decision
made, not a decision deferred.
<!-- /stale-ok -->

Still open:

- Two machines means **two panels**, each rendering the same `Board/` via git and dispatching
  locally. Simpler than a remote agent, but "one control surface" is really "the same surface
  in two places" — and a panel that makes card-writing easy makes git conflicts likelier.
  **The selector cuts across this**: accounts and machines are now independent axes, so a
  panel can dispatch on an account that is not "this machine's". The freshness rail is the
  reason that is not free — the *code* a dispatch runs is whatever the local checkouts have
  out, whichever account pays for it.
- **What the classifier keys on for §3.5.** The chore/scribe/triage boundary is a judgment
  with no formula, and the only honest way to calibrate it is to run a few batches and watch
  the bounce rate.

## 5b. The money rule — Karel, 2026-08-14

*"I would love the default option to be 'stop, when free usage runs out'. With optional
'continue nevertheless' decision."*

**Built.** Framework-side as `nightshift.usage`, on the paired branch `ai/dispatch-cost`:
reads this config directory's meters, and refuses a dispatch when the plan allowance is spent
and continuing would draw paid credits. Only an explicit per-invocation opt-in flips it —
**no persisted setting**, because a config field meaning "always spend money" outlives the
intent behind it. Recorded as audit matrix row 73 and in `feedback_account_dispatch`.

Three decisions inside it worth remembering, because each is a trade rather than an obvious
choice:

- **It fails open.** A missing, moved or unreachable endpoint yields *allow, unmetered*, not a
  refusal. Failing closed on an undocumented dependency would let one upstream rename stop
  every overnight run — worse than the outcome being prevented. The reactive wall detector
  stays the backstop, and the verdict always carries "ran without a meter" so a log never
  implies a check that did not happen.
- **The start margin guards money, not attempts.** A dispatch cannot be un-started, so one
  begun at 98% crosses into paid credits mid-run — hence a refusal *below* the limit. But with
  paid overage off, walling is already handled reactively, and refusing early there would add
  a stop with no new protection.
- **Bucket names are never hardcoded** (see §4).

## 6. Order of execution

1. ~~Probe the usage endpoint~~ — **done 2026-08-14, it answers.** See §4, including three
   corrections it forced on this doc.
2. ~~`claude agents --help`~~ — **done 2026-08-14.** See §5; v1 shrinks.
3. ~~The money rule~~ — **done 2026-08-14**, §5b. `nightshift.usage`, 29 tests.
4. ~~The `classifier` and `scribe` charters~~ — **done 2026-08-14**, `.claude/agents/`. The
   charters exist; **what does not yet exist is the thing that dispatches them** — see 5.
5. ~~The ingest command~~ — **done 2026-08-14.** `python -m nightshift.ingest`, 29 tests.
   Collects the notes, checks the money rule **before every dispatch** (a fan-out that starts
   with headroom can lose it partway), classifies the whole lane in one dispatch, writes
   the routing view at the vault root beside the digest (generated on first run, so it is not
   in the tree yet — which is why it is not named here), and fans `scribe` out on `--scribe`.
   Triage is only ever *listed*, never dispatched. Models pinned to short aliases, not dated ids.
6. ~~**§3.5 chores**~~ — **done 2026-08-14.** `kind: chore` + `surface:` in `card_schema`;
   `nightshift.chores` carrying selection-with-a-reason, the effort budget, one-attempt, the
   bisect and the aggregated checklist — **and now the execution half**: `python -m
   nightshift.chores` dispatches the batch and lands it. Phase 1 per chore is the gates plus
   `suite.touched` (the tests whose import graph reaches the diff, transitively) in its own
   worktree; phase 2 merges the survivors onto one `chores/<stamp>` branch and runs the gates
   and the **whole** suite once; phase 3 is one review over the combined diff, then one merge.
   Red bisects the per-chore merge commits, drops the culprit and re-verifies, twice at most —
   a second red is a routing finding, not a bisecting one. 24 execution tests + 12 on
   `suite.touched` + 5 runner-side.

   Four things worth knowing before touching it:

   - **The narrow phase-1 slice is only sound because phase 2 runs everything**, so
     `suite.touched` is offered and never defaulted: `runner.dispatch` takes a `test_selector`
     defaulting to `suite.select`, and `chores` is its only non-default caller. Recorded as
     audit matrix row 74. What it structurally cannot see is a test that reaches the change
     through a string — the batch suite is the answer to that, and the test suite reproduces
     exactly that case.
   - **A chore leaves `tasks/` on every path.** Selection refuses a spent chore *and* the
     night skips `kind: chore` entirely, so a chore left in `tasks/` after its one attempt is
     invisible to both queues at once. `runner.attempt_limit()` is the single home for the
     limit, and every drop — failed, bounced, refused merge, bisected culprit — settles the
     card.
   - **One `needs_decision` stops the whole batch.** Splitting on the reviewer's say-so would
     mean guessing which items its question was about. The branch survives, so the answer is a
     merge by hand rather than lost work.
   - **The bounce instruction lives in the framework worker prompt, not in a charter.** Its
     condition is the card's `kind:`, which a charter cannot see, and a per-charter copy would
     be one more thing to remember when a worker is added.
7. ~~**§3.6 framework freshness**~~ — **done 2026-08-14.** `nightshift.freshness`: `read()`
   fetches (read-only, cannot move a tree) and reports branch / ahead / behind / dirty;
   `pull()` is a separate `--ff-only` act that `refuse_pull()` declines for a dirty tree, a
   non-default branch, or local commits the remote lacks; `describe()` is **one line**,
   because refusing the offer is a first-class answer. Unknown — wheel install, no upstream,
   failed fetch — never reads as fine. 17 tests against real clones.

   Wired into `doctor`, so the question is asked where every push already goes: a reporting
   `nightshift-fresh` line, and a **failing** `paired-branches` check — the framework on a
   feature branch beside a repo on a different one is the state neither repo can see, and it
   is local HEADs only, no network. The SHA report is untouched per (d). The instinct this
   refuses (auto-pull) is argued down in the module's own docstring rather than left as an
   absence, since the next reader will have it too.

   `python -m nightshift.freshness [--pull] [--no-fetch] [--against BRANCH]`.
8. ~~§3.2 charter cleanups~~ — **done 2026-08-14.** Generic acceptance now lives in the
   charters that own it: `art-reviewer.md` has **four standing criteria** and
   `code-reviewer.md` **four standing questions**, each stated as *"yours on every card, never
   written on the card"* and applied whether or not a card mentions them — so both charters'
   "quote the criterion" rule had to be widened to count the standing set as stated, or the
   two rules would contradict. Each names the boilerplate a card must not carry and says a
   card carrying it anyway is an ordinary card, not a `needs_decision`.

   The rule triage already had now **binds**: its section opens by claiming precedence over
   the thoroughness sections, on a stated axis — *exhaustive about the judgment, silent about
   the mechanics* — and lists three things not to write (hand-check a gate, restate a charter,
   enumerate a grep) with the measured violation quoted as the example. **Precedence, not
   rewording, was the fix**: the rule was already correct, and it lost because every competing
   instruction in the file is phrased as a mandate. Logged as
   `advisory-rule-lost-to-mandatory-prose`.

   `menu-art-start-run.md` is the corrected card — the `asset_hygiene` block is one line, the
   standing pixel-art and "belongs with its neighbours" criteria are gone, and **the "no shadow
   of any kind" constraint Karel actually rejected attempt 1 over moved up into `## Acceptance`
   from `## Thread`**, where the reviewer never saw it. Four sibling cards in `done/` carry the
   same two redundant blocks; left alone, since they are shipped.

   Stale branch names fixed by removing the names: `code-thread.md`'s *"never commit to `dev`"*
   and `triage.md`'s *"before it reaches `dev`"* now both point at
   `.ai/manifest.toml`'s `[branches].integration` and name no branch at all, which is the only
   form that cannot go stale a fourth time. **Note the gap this exposed:**
   `branch_role_prose`'s `_DOCS` covers `CLAUDE.md` and the session log, **not
   `.claude/agents/`** — so these two sat wrong for two renames with a gate for exactly this
   running green. Not extended here (framework code, and naming the manifest instead makes it
   moot for these files), but any charter that names a branch literally in future is ungated.

   One more staleness in the same class while in there: `code-thread.md` claimed **2181 tests
   and ~4.2 min**, both from before the framework suite left this repo on 2026-08-08 — against
   1937 tests today. Fixed by deleting the numbers and pointing at `CLAUDE.md`'s *How to run*,
   on the charter's own second-home-that-drifts reasoning.
9. ~~§3.3 recalibrate the findings bar~~ — **done 2026-08-14.** `triage.md`'s *"a finding earns
   its place by saving the worker a tool call"* is replaced by **"a finding earns its place if
   the worker would plausibly get it wrong, not merely spend a call getting it"**, with the old
   bar's provenance recorded (calibrated for a local-model worker) and the reason it fails now
   stated as reliability rather than cost: **the worker opens those files to edit them
   regardless, and grep is strictly more accurate than triage is.**

   The charter says what the replacement **looks like**, not only what is forbidden — three
   qualifying shapes, each one or two lines rather than a section: a **decision** with owner and
   date; an **invariant** the worker cannot see from one site; and a **trap**, where the obvious
   action is wrong, which is the highest-value kind because it is *defined* by the worker
   getting it wrong.

   `grid-distance-metric.md` is the worked example, in the charter and in the card. Finding 8
   (*"Tests that will break, by file"* — nine files of names and line numbers) became **three
   traps**: the FOV test that is a specification to re-point rather than delete, the razor test
   whose docstring is stale while its assertion stays true, and the Mk3 test asserting a flag
   being deleted. Group A's thirteen-row site table became the theorem plus the **two rows that
   are decisions rather than conversions** (the sword's literal `1.5`, Mk3 held at `2.0`), with
   *"grep for the arithmetic rather than working from a list"* in place of the list. Finding 3
   lost its line numbers and kept the trap. The generic *"gates green, full pytest green"*
   acceptance bullet is gone per item 8.

   **The card barely shrank — 30.9 KB to 30.8 KB — and that is the honest result**: the fix is
   "write the other thing", not "write less", and the first draft of the replacement was
   *longer* than the enumeration it removed before being tightened. If size is the goal it
   needs its own pass; what changed here is that nothing in those sections is now something
   grep would answer better.
10. ~~§3.4 panel — discuss before building~~ — **discussed 2026-08-14 and the design is
    settled**; §3.4 above is the result, and the mockup is approved. Nothing is built.

11. ~~**The board verbs the panel needs**~~ — **done 2026-08-14.** `python -m
    nightshift.boardcmd`, one home with five subcommands: `reorder` (write `kanban_order`),
    `verified` (`testing/` → `done/` *and* the reconcile), `promote` (one note out of the
    private lane into `inbox/`), `note` (a new bare note) and `edit` (replace a body,
    frontmatter byte-identical). 27 tests. The sixth verb went to item 12, which owns it.

    Three things worth knowing before touching it:

    - **Every write goes through `board.set_fields`/`board.move` and commits through
      `board.commit_board`, and a test asserts the module contains no `re` at all.** A fifth
      copy of the frontmatter grammar inside a CLI verb would be justified nowhere, and "no
      second parser" is exactly the kind of constraint that is true the day it is written and
      quietly false a year later.
    - **The private lane is moved, never read.** `promote` relocates by name; `edit` splices
      at the frontmatter boundary and never looks at the body it drops — the same boundary
      `reconcile` keeps. The tests assert the note's own text reaches neither stdout nor the
      commit message, because the failure mode there is not a refusal, it is a helpful summary
      nobody asked for. `edit` exists as a command *because* `ideas_fence` correctly stops an
      agent from opening a private note, so editing one has to be an act the human drives.
    - **`verified`'s reconcile is asserted through a second, unrelated card.** Asserting it on
      the marked card proves nothing — `board.move` stamps `state:` itself — so the fixture
      carries a card whose `state:` and lane disagree, which only a real reconcile pass moves.

    Nothing prompts and nothing needs a TTY: a body arrives as `--body`, `--body-file` or
    stdin that was named explicitly, and an unnamed body is a usage error rather than a
    blocked read. The caller this is for is a request handler with nobody to ask.

12. ~~**`review/` has no drain**~~ — **done 2026-08-14.** `python -m nightshift.drain` takes
    each card at rest in the lane that has commits on its branch, runs `runner.review_stage`
    — reused, never reimplemented, so the criteria, the reviewer agent, the tier resolution,
    the throwaway-checkout blindness and every degradation path come along unchanged — and
    routes on the verdict. `--card <id>` is the panel's per-row button and item 11's sixth
    verb; `--dry-run` looks first. 17 tests.

    The two shape choices the card deliberately left open, both recorded in the module's own
    docstring rather than only here:

    - **The night does not drain the lane.** A card in `review/` is already green and nothing
      about it decays overnight, so the urgency of concluding it belongs to the person waiting
      on it, not to the machine — and a night that spent its window on yesterday's leftovers
      before today's work would pay for that urgency with the one resource this whole plan is
      about. The lane is also unbounded while the night's accounting (attempts, backoff,
      rescue branches, the session/wall arithmetic, the run record) hangs off a `Candidate`
      from `tasks/`; joining the two means restructuring that arithmetic inside a module that
      runs unattended with ~320 tests around it. The docstring states what would have to
      become true to revisit it.
    - **An `ok` verdict merges, through `runner.settle`** — so `rebase_and_merge`, the
      re-verification of the replayed result, and `testing/` or `done/` per the card's own
      `verify:`, with the failure path free (a branch that will not rebase goes back to
      `review/` carrying a `## Merge` note that says why). A drain that only moved the card
      would leave a merge nobody performed; one with its own merge would be a second
      implementation of the most dangerous step in the system.

    An artefact-only card is left alone *without being reviewed at all* — one `git rev-list`
    before any spend, so running the drain twice a day never bills for the card in the lane
    nobody asked about. The money rule is checked before each review rather than once for the
    pass; a wall or a refusal stops the pass, and every card it did not reach is **named** as
    not reached, because "still here" and "never looked at" are different facts. The pass
    takes the runner's lock, since it merges into the integration branch and a night doing
    the same thing at the same time is the one way it could damage rather than merely fail.

    **Run on real work the same day**, on `tools-console-encoding` — the card sitting in the
    lane that prompted this. `code-reviewer` returned `ok` in 94 s for $0.82, and the branch
    then would not rebase onto `test`: a genuine conflict in `01_audit_findings.md`, so the
    card went back to `review/` with `## Merge`. The designed failure path, on first contact.
    It also exposed a gap that would have cost that $0.82 on *every* subsequent pass — a card
    reviewed `ok` and blocked on a human merge was still a sweep candidate — fixed in
    `nightshift@ba69954` by skipping a card carrying `## Merge` unless it is named with
    `--card`, and then in `nightshift@8299832` by making that skip *one* predicate both the
    pass and `--dry-run` consult, since the first fix had reached only one of them and a
    report that disagrees with the pass is worse than no report. The one thing still open is
    the conflict itself, which is Karel's to resolve.

13. ~~**The accounts axis — identity, config, selector.**~~ **done 2026-08-15**,
    `nightshift@8a928a5`. Framework-side, CLI first as always, and it was **a prerequisite
    for the panel rather than a feature of it**: without a list of accounts there was
    nothing for §3.4's meter columns to iterate and nothing for its selector to select.
    Carded first (`Board/done/accounts-axis-identity-config-selector.md` — item 13 needed
    carding and none existed). Three pieces, in dependency order:

<!-- stale-ok: `oauthAccount.*` and `CLAUDE_CONFIG_DIR` are external — Anthropic's own config
     file and the CLI's env var. Same class as §4. -->

    - **Identity, read locally.** `usage.Identity` / `usage.read_identity()`:
      `oauthAccount.{emailAddress, accountUuid, organizationUuid, hasExtraUsageEnabled}` out
      of `~/.claude.json`, beside `usage.Snapshot` rather than inside it — a different file,
      a different failure mode, and no network. Verified against the shipped VS Code
      extension bundle (2.1.232) that the override join is **asymmetric**:
      `.credentials.json` joins `CLAUDE_CONFIG_DIR` **or** `~/.claude`; `.claude.json` joins
      `CLAUDE_CONFIG_DIR` **or** `Path.home()` directly, with no implicit `.claude/` segment.
      Getting that wrong would have read the wrong account's identity while the meters kept
      reading the right one, silently.
    - **An `[[accounts]]` config block** (`manifest.Account`), so `dispatch: never` stops
      being prose. Per entry: `label`, `config_dir`, `dispatch` (`"always"` default,
      `"never"` to exclude). `config_dir` deliberately is **not** existence-checked by
      `validate()` — an account's directory legitimately exists on only one of Karel's two
      machines at a time, and the manifest is shared between them. Config may only ever
      **exclude** — `hasExtraUsageEnabled` vetoes an account config forgot to mark, and
      never promotes one.
    - **The selector.** `usage.main()` gains `--config-dir PATH` (raw) and `--account LABEL`
      (resolved through `[[accounts]]`), mutually exclusive, surfacing identity and the
      account's configured `dispatch` stance beside the meters in both plain and `--json`
      output. Confirmed **`runner.py` needed zero changes**: `_worker_env` already inherits
      the environment wholesale, so selecting an account for an actual dispatch is whatever
      *calls* `runner.dispatch` setting `CLAUDE_CONFIG_DIR` in its own process — the panel's
      job (item 14), not the runner's.

<!-- /stale-ok -->

    **Not done here, on purpose:** gating a dispatch on an account's `dispatch: never`
    stance, and any panel UI — that assembly is item 14's, once the primitives exist.
    The waiver shape is settled and must not be re-litigated: **per invocation, never
    persisted**, same as `--allow-paid`, for the reason `feedback_account_dispatch` gives.

14. ~~§3.4 panel v1 itself~~ — **done 2026-08-15.** `nightshift.panel`: one stdlib
    `http.server` module, one HTML file (`panel_static/app.html`, black and green per the
    approved mockup), `command-center.bat` at this repo's root. Five pages (Now/Verify/
    Inbox/Ideas/Run) plus the ambient rail — live run tally, account identity, framework
    freshness (fetched only on an explicit Refresh, never on page load). Every write/
    dispatch verb shells out to `python -m nightshift.<module> <args>` through one
    command-running helper; a test asserts the module never imports `boardcmd`, `chores`
    or `runner.dispatch`/`settle` directly, mechanising "the panel will not import this
    module — it will run it" — nightshift's own boardcmd test suite's stated precedent.
    `boardcmd.create_note` gained an optional `lane` parameter (`ideas` alongside the
    default `inbox`) so the Ideas page's "new" action has a real verb, rather than
    reusing `edit` against a file that does not exist yet.

    **One real defect, found only by running the panel against this repo's own board.**
    `_guard_dispatch_account` first checked only the configured `[[accounts]]`
    `dispatch:` stance — and this repo's manifest has no `[[accounts]]` entries at all
    yet (the config gap §3.4 itself named as still missing). The rail correctly rendered
    "API spend ENABLED on this account" from the live `usage.read_identity()` read, and a
    dispatch would have gone through anyway, because the guard never consulted that
    signal — reaching exactly the account `feedback_account_dispatch` exists to protect.
    Fixed: the guard now also vetoes on the live `hasExtraUsageEnabled` read
    unconditionally, matching what this doc's own §3.4/item-13 prose already said the
    signal was for ("`hasExtraUsageEnabled` may veto an account config forgot to mark").
    Logged as `account-veto-checked-config-only-not-live-signal` in nightshift's own
    `.ai/corrections.log`.

    Framework-side on `nightshift`'s `ai/command-center-panel`, merged to `main` at
    `a67a029`, pushed, branch deleted. 60 new/changed tests; nightshift's 22 gates and
    full 1552-test suite green; preflight passed on both the pre-merge branch tip and
    the merge commit. `Board/done/command-center-panel.md` is the closing card.

**The programme — items 1 through 14 — is complete.** What is left is the two threads
recorded under "Worth knowing, not blocking" below (the chore batch's phase 2/3 still
unproven on real work; the `[[accounts]]` config gap this item's own defect exposed,
which is Karel's to fill in with his actual account labels and config directories), plus
whatever using the panel day to day turns up.

**Calibrated once, on real work — 2026-08-14.** The first classifier run over 12 notes gave
4 chore / 3 scribe / 4 triage / 1 inline, flagging its own low-confidence calls. Of its four
chores, two needed `gpu-box` and two hid a small decision each, so **zero were runnable
here** — the chore route's real supply on this machine is thinner than the count suggests.
The first batch then ran one hand-marked chore: 7 minutes on sonnet, 48 turns, gates clean,
correct work. Against the hour-per-triage this whole programme was written to fix, the cost
inversion is answered. Two findings came out of it: a card adding a gate plus an audit row is
not a one-prompter, so the chore boundary is tighter than it reads on paper; and the effort
budget that caught it was measuring the wrong thing entirely (item 6, now removed).

**Phase 2 and phase 3 remain unproven.** Nothing survived to merge in that batch, so the
one-suite-run-instead-of-eight saving — the whole economic claim of §3.5 — has still never
been exercised on real work.

**Cross-repo rule applies to all of this.** The framework half is on nightshift's
`ai/dispatch-cost`, the project half on this repo's `ai/dispatch-cost`. Neither half is valid
alone; nightshift merges to its `main` **first**, then this repo's half lands on the
integration branch. Do not land one side and leave the other on a branch.

**The `[[accounts]]` config gap, still open (2026-08-15).** Item 13 built the
`[[accounts]]` schema; nobody has since added a row to this repo's `.ai/manifest.toml`
naming Karel's actual accounts and their `config_dir`/`dispatch` stance. Item 14's own
defect (above) is the direct consequence: with no config to check, the panel's account
selector has nothing to iterate, and the live `hasExtraUsageEnabled` veto is carrying
the whole weight of `feedback_account_dispatch` alone. That veto works and is tested,
so nothing is unsafe — but "Karel picks the dispatching account" (§3.4) is not yet true
in the sense of *offering a choice*; today there is only the ambient account plus a
refusal if it turns out to be the wrong one. Filling in `[[accounts]]` is a config edit,
not a code change, and it is Karel's to make once he decides on the label and
`config_dir` for each machine/account pair.
