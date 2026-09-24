# Doc 04 — The local runtime (Session I reopened, 2026-09-17)

The spec `00_architecture.md` §10 has carried as ⬜ **next** since 2026-07-21. Session I ran
its research pass on 2026-07-30, and Karel parked it the same day: *"Too much work for too
little effect."* Nothing was installed, and doc 04 was never written.

**What reopens it is not a change of mind — it is that five of the six facts the park rested
on are now measurably false.** A local coding stack was installed on Mithlond on 2026-09-16
and benchmarked on the box (`~/.claude/local-llm-stack.md`, machine-wide, not this repo's).
This document is the spec written against *that* stack and against this repo's own run
telemetry, replacing the researched numbers with measured ones as the Session I runbook
required.

**Read `~/.claude/local-llm-stack.md` first.** Every hardware fact — models, quants, tok/s,
VRAM, RAM, the `--n-cpu-moe` knob, the rejected alternatives — lives there and is **not**
copied here. That file is machine-wide and shared with every project on this box; this one is
about what nightshift does with it. A number in both places is a number that will drift in
one of them.

---

## 1. What the park rested on, and what is left of it

`research_notes.md` (2026-07-30, under "Local coding/checker models") records six findings.
Re-checked 2026-09-17 against the installed stack and this repo's `.ai/runs/`:

| # | 2026-07-30 finding | Status now | Evidence |
|---|---|---|---|
| 1 | *"Prompt caching is the thing local inference does not have — local must recompute the prefix."* | **False within a session.** llama-server keeps its KV cache between requests and prefix-matches. | 15,290-token prefix: 1st call 15,290 tokens / 41.2 s; 2nd identical call **4 tokens / 0.6 s**; 3rd (prefix + new turn) 34 tokens / 0.9 s. |
| 2 | *"A 30B-A3B on a 3060 generates at ~12–15 tok/s."* | **Off by 3×.** | Ornith-1.5-35B-A3B Q4_K_M: **42.9 tok/s** at `-ncmoe 26`, 46.9 at 22. |
| 3 | *"The 12 GB box cannot hold the median card (144k context)."* | **False.** Hybrid attention — 30 of 40 layers are linear-attention — makes context ~4× cheaper than the naive formula. | 128K ctx = 10,142 MiB used, 2,146 MiB free. The full 262K window fits at `-ncmoe 30`. |
| 4 | *"Leading local candidate is SWE-bench 59.2 against Sonnet-class ~70+."* | **Superseded.** Ornith's card claims SWE-bench Verified 79 / Terminal-Bench 2.1 67.8 and explicitly targets OpenCode. Vendor numbers at BF16 on the vendor's harness — **not** evidence about this repo. | §7 still governs: nothing goes local without a golden set. |
| 5 | *"Claude Code sends 27–33k tokens before the user's prompt; OpenCode ~7k."* | **Confirmed and sharpened.** | Measured startup context across 25 dispatched attempts: **median 42,133**, max 60,526. Of that, nightshift controls ~8k (CLAUDE.md 8.4 KB + `.ai/CLAUDE.md` 5.5 KB + charter ~5 KB + prompt template ~12.5 KB). The remaining ~34k is harness. |
| 6 | *"TurboQuant is not in upstream llama.cpp."* | **Still true.** Not re-researched; do not plan around it. | — |

Finding 1 is the one that mattered. The park's load-bearing sentence was *"the number cloud
bills at 0.1× is the number local must recompute"*, and llama-server's slot cache means that
is wrong for every turn after the first **as long as the prefix is stable**. What still
invalidates it — and this is now the constraint to design against, not caching's absence:
**auto-compaction rewriting history, restarting the server, and switching models.** A cold
128k fill costs ~6 minutes at the measured 376 tok/s prompt rate. Paying that once per card is
fine. Paying it three times because the session compacted is not.

## 2. The context arithmetic, measured on this repo

Peak context per dispatched attempt, from every `stream.jsonl` under `.ai/runs/` (25 attempts;
max over assistant turns of `input + cache_read + cache_creation`):

| | tokens |
|---|---|
| Startup floor, median / max | 42,133 / 60,526 |
| Peak, median | 108,793 |
| Peak, mean | 133,699 |
| Attempts over 64k | 23 / 25 |
| Attempts over 100k | 14 / 25 |
| **Attempts over 128k** | **9 / 25** |
| Attempts over 200k | 6 / 25 |

Read this as two separate facts, because they have different fixes:

- **The floor is mostly harness, and the harness is choosable.** ~34k of the 42k median is
  Claude Code's system prompt, tool schemas and skill listing — re-transmitted every turn, and
  on a 128k window that is a quarter of the budget spent before the card is read. Under
  OpenCode the same floor is ~7–15k. That is the single biggest lever available, and it is
  only reachable by changing runtime.
- **The peak is the card, and the card is selectable.** 9 of 25 attempts would overflow 128k
  *on Claude Code's floor*. On a ~12k floor the same attempts have ~30k more room, which moves
  perhaps three of the nine. It does not move the six that passed 200k. **Those cards are not
  going local on this hardware, and no amount of tuning changes that** — which is why §5 makes
  card selection a declared property rather than a hope.

## 3. The three routes

### Route A — keep `claude`, repoint the endpoint

`ANTHROPIC_BASE_URL` → llama-server. The Session I runbook assumed this, and
`00_architecture.md` §16's block comment assumes it too (*"this becomes the endpoint's model
name and `ANTHROPIC_BASE_URL` does the rest — still one edit, still here"*).

**The assumption it rests on is that llama-server speaks the Anthropic Messages API.**
Re-checked 2026-09-17 against the installed build (`llama-server-impl.dll`, b10998): the
strings `/v1/messages`, `/v1/messages/count_tokens`, `cache_read_input_tokens` and
`anthropic-billing-header` are all present. That is **string-level evidence, not a live
response** — §9 exists to turn it into one, and nothing should be built on Route A until it
has.

Framework change: approximately zero. Every hook, charter, skill, verdict contract,
`stream-json` parser, `limits.detect`, the gate/test pipeline and the whole panel keep
working. Three known behaviours to design around, all from `research_notes.md` and all still
unverified against this build:

- **Ghost Haiku calls.** Internal housekeeping routes to a Haiku model regardless of base URL —
  confirmed in this repo's own telemetry (every run's per-model usage block lists a Haiku
  entry). That tier override must point at a served local tag or the calls 404.
- **Prefix-cache defeat.** A per-request hash injected into the system prompt changes the
  prefix every request. Given §1 finding 1, this is not a tuning detail — it is the difference
  between viable and not.
- **Concurrent request flooding** segfaults a single-slot server; run with parallel slots.

### Route B — OpenCode as a second runtime

Native for Ornith (its card explicitly targets OpenCode), a ~7k system prompt, and the model
was post-trained against this tool-calling shape. Verified against the installed OpenCode
1.18.31 and its config schema:

| nightshift needs | `claude` | `opencode` |
|---|---|---|
| headless run | `-p`, prompt on stdin | `run [message..]` |
| charter | `--agent` → `.claude/agents/*.md` | `--agent` → `.opencode/agent/*.md` |
| model | `--model <alias>` | `-m provider/model` |
| effort | `--effort` | `--variant` |
| permission posture | `--permission-mode` | `--auto`, plus a declarative `permission` block (`ask`/`allow`/`deny` per tool, with path patterns) |
| machine-readable stream | `--output-format stream-json` | `--format json` |
| resume | `--resume <id>` | `-s <id>` / `-c` |
| MCP determinism | `--strict-mcp-config` | `--pure` |
| extra dirs | `--add-dir` | **none** — `--dir` sets cwd only |
| spend cap | `--max-budget-usd` | none (free); `steps` caps agentic iterations |

It also has skills (`skills.paths`), an `instructions: []` list, `compaction` tuning and a
subagent-depth cap. Hooks are JS plugins in `.opencode/plugin/` — `tool.execute.before`/`after`,
`permission.ask`, `chat.params` and `chat.message` are all present in the binary. nightshift's
hooks are Python modules reading JSON on stdin, so one shim module bridges all of them (§8).

Cost: nightshift grows a runtime abstraction. The coupling is narrower than 8,398 lines of
`runner.py` suggests — **11 argv-construction sites across 5 modules** (`runner.py` ×7,
`ingest.py`, `fix.py`, `update.py`, `panel.py`), plus `claude_binary()`, `_terminal_result()`,
`limits.detect()` and one `.claude/agents/<worker>.md` existence check at `runner.py:956`.

`research_notes.md` warns **"Do not migrate the whole system to OpenCode"**, and that warning
stands unqualified: Phases 0–4 are Claude-Code-shaped in ways that are not cosmetic. Route B
means OpenCode as a *second* runtime for a *named subset* of agents, never a replacement.

### Route C — a thin client, on record since 2026-07-30

`research_notes.md`: *"a thin client (~80 lines) posting to the local Messages endpoint, with
grammar / JSON-schema constrained decoding so a malformed verdict is structurally impossible.
That is a bigger reliability lever than any model choice, and it has no cloud equivalent."*

Correct, and it is the right answer for exactly one shape: an agent whose entire output is a
structured verdict and whose input is a payload rather than a repo — `translator`,
`classifier`, `stale-hunter`. It is the wrong answer for anything needing a tool loop, because
then the thin client is a coding agent and it is no longer thin.

### The pick

**A first, as a measuring instrument. B for whatever survives. C only for `translator` and
`classifier`, and only once A has shown the models can hold a schema at all.**

A is nearly free to try and answers the only question that matters right now — *can Ornith do
any of this work* — without committing to a refactor. If A works and the ceiling turns out to
be the 34k floor, B is the upgrade and the measurement will say so. Doing B first means
building a runtime abstraction to discover the model was never good enough.

## 4. Runtime is a new axis, not a new tier

The obvious move is to add `local` to the ```tier-binding block. **Do not.** §16's tier is a
statement about *how much judgment the work needs*, and its argument that "the worker tier is
the measuring instrument for card quality" gets stronger, not weaker, once tokens are free. A
`local` tier would make "where it runs" and "how much judgment it gets" the same field, and
then every routing decision silently re-answers both.

So:

> **`tier` says how much judgment. `runtime` says where it executes. They are orthogonal, and
> §16 governs only the first.**

`runtime` has exactly two values, `cloud` and `local`, and **`cloud` is the ground state**:
anything unresolvable, unavailable, disabled or unmeasured runs on cloud. There is no
configuration in which a card fails to dispatch *because* the local runtime is missing.

Where each fact lives, one home each:

- **`00_architecture.md` §16's block** — unchanged. `lead = opus`, `worker = sonnet`. The
  tier→model binding stays what it is.
- **`.ai/hosts.json`, per machine** — the local endpoint, the model tag, the runtime kind and
  the agent allowlist. This is the "specifics defined by each computer" half, and the file
  already exists for exactly this purpose.
- **The card's frontmatter** — a `local:` field, author-owned, default `no` (§5).
- **`.ai/manifest.toml`** — nothing. Resist the urge: the endpoint is per-machine and the
  allowlist is earned per-machine by measurement, so a committed project-wide table would be
  wrong on the laptop the day it was written.

Sketched host row (Mithlond) — shape only; the field names are a Session-I decision, not
settled here:

```json
"Mithlond": {
  "capabilities": ["gpu-box"],
  "permission_mode": "bypassPermissions",
  "local_model": {
    "runtime": "claude-endpoint",
    "base_url": "http://127.0.0.1:8082/v1",
    "model": "ornith-1.5-35b-a3b",
    "launcher": "E:\\AI\\llm\\run_ornith.bat",
    "context_limit": 131072,
    "agents": ["classifier", "chore-thread"]
  }
}
```

**`agents` is an allowlist, and it is the enforcement point for §7.** A charter not named
there never runs local — whatever a card says, whatever the panel says. A charter is added to
that list by a measured golden-set pass and removed by a measured accept-rate drop, never by
argument. A box with no "local_model" key never uses a local model at all, which is the
laptop's correct answer and the safe default for any new clone.

**Declared vs. probed, and the line is already drawn.** `.ai/hosts.json`'s own comment sets
the rule (`declarative-because-of-initiative`): *a check that bounds behaviour is declarative;
a check that discovers a fact may probe.* "May this machine use a local model" **bounds** — it
is declared above. "Is the server answering right now" **discovers** — so it is probed, at
dispatch time, and a failed probe is a fallback to cloud rather than a refusal to dispatch.
Both questions exist; neither answers the other.

## 5. Which work goes local, and how a card says so

§16's own model table already names the landing zone: *"narrow checkers, translation,
classification | Sonnet, **or local when available**"*. Ranked by how cleanly the payload
separates from the codebase:

| Agent | Fit | Why |
|---|---|---|
| `classifier` | **Best.** Routes inbox notes; its charter forbids opening source. Pure payload-in, enum-out. | Karel's pick, and the evidence agrees. |
| `chore-thread` | **Good.** A chore is by construction work with no fork in it (`card_schema` refuses `kind: chore` + `tier: lead`). Small diffs, tight slices. | Karel's pick. 24 archived chores in `Board/done/` are the golden set. |
| `translator` | **Good on shape, unmeasured on quality.** Strings + terminology, no code context. | Karel: *"I don't know how good it is at that."* Neither does anyone — §10 is how to find out, and cs/es naturalness is the hardest of the four to score mechanically. |
| `stale-hunter` | **Good, and it has a fixture.** Reads one doc + named sources, reports only, never edits. Session D left a known-answer case: 5 findings, 5 true, 0 false positives. | The nearest thing to a pre-existing golden set. |
| `code-thread` | **Not yet — and not never.** See below. | §7. |
| `triage`, `scribe`, `code-reviewer` | **No.** Each needs either the whole codebase or the judgment being measured. `triage.md` alone is 29 KB of charter. | — |

### The worker variant is not thrown out

Karel, 2026-09-17: *"I wouldn't throw out the worker variant too… it may help a lot, if we are
able to outsource some of the tasks."* Agreed, and §2 says what it would take: of 25 attempts,
**16 stayed under 128k and 11 stayed under 100k**. Those cards exist and they are real work.
What is missing is not capability — it is a way to know *in advance* which card is which,
because peak context is only observable after the fact.

**Do not solve this by predicting.** An earlier draft of this section made a validated
pre-dispatch predictor the prerequisite for a local `code-thread`. Karel, 2026-09-17: *"I'm
also not sure about the ability to compute the max context in advance."* He is right, and the
objection goes deeper than accuracy — **prediction only earns its cost if being wrong is
expensive, and here it is not.** A local attempt spends no tokens. What it spends is wall
clock, at night, which §7 establishes is the cheap axis. So the design is to *find out by
trying* and to make the failure cheap and recoverable.

Three things make that work, and two of them already exist.

**1. A context wall is structurally the same event as a usage wall, and the handover for it is
already built.** `runner-worker-handover` exists because a `five_hour` limit lands mid-card,
and everything it does is what a context overflow wants:

- `_limit_reached` keeps the worktree, records the session id and a hash of the working tree,
  and **hands the attempt back when the tree moved** — a worker that got halfway costs nothing.
- `read_handover`/`write_handover` carry that across dispatches, and `NO_PROGRESS_STOP` stops
  a give-back loop when the tree stops moving.
- Three continuation shapes already exist, in order of how much context they keep:
  `_RESUME_PROMPT` (warm session), `_REENTER_NOTE` (fresh session, kept worktree, uncommitted
  diff is the handover) and `_WIP_NOTE` (worktree gone, work survives as a `wip:` commit).

`_REENTER_NOTE` is exactly the answer to *"start a new session when context is running out"*:
a fresh session re-enters the same worktree and reads `git status` / `git diff` to see where
the last one stopped. **The state is the diff on disk, not the transcript** — which is the
same reason the plan-file workflow in `~/.claude/local-llm-stack.md` works, and the reason a
weak model tolerates a session boundary at all.

**2. Escalation on retry, which is what makes a wrong guess free.** A full card has
`MAX_ATTEMPTS = 3`. Spend the first locally and the rest on cloud: a card the local model
cannot hold overflows, hands back a half-finished worktree, and the cloud attempt finishes
from there rather than from zero. The card lands either way; the only question is how much of
it was free.

**This needs one specific thing to be true, and it is not true today: the local attempt must
not consume the card's attempt budget** — or a local overflow silently costs a card one of its
three real tries. `attempt_budget` returns 3 for a full card and **1 for a chore**
(`CHORE_MAX_ATTEMPTS`), so a chore has no spare attempt at all and would be the first thing
broken by getting this wrong. A local attempt is a separate counter, not a discount on the
existing one.

**3. Compaction is the last resort, not the strategy.** It is free and automatic — OpenCode
auto-compacts at context-minus-output — but it invalidates the prefix cache (§1: a cold 128k
refill is ~6 minutes at the measured 376 tok/s) and the tool itself warns that repeated
compaction reduces accuracy. One compaction on a card that is nearly done is a good trade. A
card that compacts twice is a card that should have handed over, and the second compaction is
the signal to do it.

### What is still unsolved: detection, not prediction

The whole scheme above rests on **noticing the wall**, and that is genuinely open.
`limits.detect` recognises a usage wall by scanning for phrases plus the CLI's structured
`rate_limit_info`, and a context overflow presents nothing like it: under OpenCode the session
does not error at all, it compacts and keeps going. So the signal has to come from the stream
— a compaction event in `--format json`, or the turn-over-turn input-token count crossing a
threshold — and neither has been read off a real run yet.

This is still much easier than prediction: it is an observation about a run in flight rather
than an estimate about a card at rest, and it is deterministic. **It is the first thing to
build after the §9 test case passes**, and it is what the archived `stream.jsonl` corpus is
for — the detector can be written and tested against 25 real runs without dispatching
anything.

Prediction survives only as an **optimisation**: `runner.card_bytes` and `oversize_note`
already compute card size, so a cheap pre-filter can skip the local attempt on a card that is
obviously too big and save the wall clock. Getting it wrong then costs nothing, because the
fallback is the thing that was going to happen anyway.

### Should the scribe decide?

Karel: *"Maybe scribe may decide, what is outsourcable?"* — half yes.

The scribe already makes a decision of exactly this shape (`kind: chore` vs. a full card with
its own review), and it is the right actor to judge **the property of the card**: is this
scoped to a named surface, is there a fork in it, does it need the whole tree. So the scribe
writes a field:

```yaml
local: ok        # ok | no    (default: no)
```

What the scribe must **not** decide is whether the model is good enough, because it has no
evidence about that and §7 says evidence is the only admissible ground. So the routing
decision is a conjunction, of which the scribe supplies one term:

> **local ⟺ `card.local == ok` ∧ `card.worker ∈ host.local_model.agents` ∧ the endpoint probe
> succeeds ∧ not disabled for this run.**

Any term false → cloud, silently and at no cost. This keeps the scribe's judgment inside its
competence (card shape) and keeps model capability where §7 put it (measurement), and it means
a scribe that is wrong about a card costs a fallback, never a bad dispatch.

`local:` is author-owned like `tier:` and `unattended:`, so it belongs in `card_schema`'s field
list with the same validation shape (`_TIERS` is the model to copy) and in the recipe's "what a
card has to say" block.

## 6. Availability, fallback, and the three off switches

**Fallback is not a fallback if it is never exercised.** A path taken only when the server is
down is a path that is broken when the server is down. So the cloud path is not a special case
— it is the default, and `local` is the deviation. Concretely: resolve the model for the card
exactly as today, then *upgrade* to local if all four terms hold. A bug in the local
resolution then produces a cloud dispatch, which is the failure everyone wants.

Three off switches, deliberately distinct, each answering a different question:

| Switch | Scope | Where | Question it answers |
|---|---|---|---|
| "local_model" key absent | Permanent, per machine | `.ai/hosts.json` (committed) | "May this box ever use a local model?" |
| Panel checkbox | This sitting | `panel.py`, process-wide state | "Am I using it right now?" |
| `--no-local` | This run | `runner` argv | "Not for this night." |

### The Command Center control

Karel: *"I would also like the command center overwrite checkbox to work for this too… Maybe
checkbox to overwrite, maybe checkbox to 'turn off' local model."*

There is an existing control to extend and a documented rule to break, and the rule must be
broken deliberately. `TierChoice` (`panel.py`) is a dropdown plus an override tick, and its
docstring says: *"**Nothing here reaches the runner.** A dispatched card runs at the tier its
frontmatter declares, checked by `hooks/tier_guard.py`; this governs only the sessions the
panel opens for a person at the keyboard."*

That rule protects §16's measuring-instrument argument — a panel that could force `opus` onto
every dispatched card would make card quality unmeasurable. **The runtime control does not
threaten that**, because it does not change the tier, and because it is asymmetric:

> **The panel may always turn local OFF. It may only turn local ON for a card whose `worker:`
> is already in the host allowlist.**

Turning it off is safe by construction (cloud is the ground state). Turning it on cannot reach
a charter that has not passed §7. So the control is one checkbox — *Use the local model* —
server-side state like `_ACCOUNT` and `_TIER`, defaulting to on wherever "local_model" is
configured, with a tooltip naming the endpoint and the allowlisted agents. Rows whose card
would take the local path get a chip, the same way the tier chip already works, so the page and
the button beside it cannot disagree.

It does reach the runner, unlike `TierChoice`, and `TierChoice`'s docstring must be amended in
the same change to say why the two differ — or the next reader will correctly conclude that one
of them is a bug.

## 7. Process lifecycle — the part with hardware in it

Karel: *"we would definitely need to be able to turn off the llama.cpp server after work and
make sure it doesn't compete with ComfyUI."*

Three consumers, one box, 32 GB of RAM and 12 GB of VRAM, and **no two of them fit**:

| Consumer | Cost | Owner |
|---|---|---|
| llama-server (Ornith) | 21.7 GB of weights in RAM + ~11 GB VRAM | `E:\AI\llm\run_ornith.bat` |
| ComfyUI | ~12 GB of Windows commit charge for its whole lifetime, idle or not | `~/.claude/imagegen-stack.md` |
| pytest (`-n auto`) | 1.4 GB per worker + a 4 GB system reserve | `nightshift/suite.py` |

**`00_architecture.md` §6 specified the lock — "the orchestrator holds an exclusive lock per
GPU consumer; art batches and code batches interleave, never overlap" — and it went unbuilt
until 2026-09-19**, because ComfyUI was the only consumer and the runner is single-instance
anyway. What finally needed it was not the reason §6 gave: cards *are* sequential within a
run, so two dispatches never overlap. The collision is that **llama-server is a long-lived
process that stays resident across everything that follows it**, including the art card three
cards later.

<!-- stale-ok: comfy_idle_watchdog.py and llama_idle_watchdog.py both live at
     E:\AI\scripts\. They are machine-wide tools outside this repo (the same
     drive ~/.claude/imagegen-stack.md documents), so resolving them against this
     tree's source roots is a category error, not a stale claim. -->

There was a precedent to copy rather than a mechanism to invent: comfy_idle_watchdog.py
(stdlib-only, polls `/queue`, kills the server after `--idle-minutes`), plus
**`comfy_server_teardown`** in `.ai/gates/`, which enforces that any instruction doc
launching ComfyUI also starts that watchdog. Built 2026-09-19 from the inbox note
`local-server-mutex-and-idle-watchdog`, on Karel's four answers that day:

1. **`E:\AI\scripts\llama_idle_watchdog.py`** — sibling to the Comfy one, machine-wide
   because the server is. Polls `/slots` for `is_processing`, which is llama-server's
   analogue of ComfyUI's `/queue`, and stops the listener on `--port` after
   `--idle-minutes`. Two differences from its sibling, each because llama-server is not
   ComfyUI: `--pid` binds it to one process, so a server restarted on the same port is
   never reaped by its predecessor's watchdog; and **an unreadable `/slots` never kills**,
   because a build with slots disabled would otherwise look permanently idle and reaping
   on ignorance is the one failure that costs someone their session.
2. **The mutex is two asymmetric halves, configured from one `conflicts` entry** in
   `.ai/hosts.json` (`{name, port, required_by, needs_gb}`), read by
   `nightshift.runtimes`. ComfyUI resident → a new false term in the conjunction, so the
   card runs on cloud: free by construction, cannot fail a dispatch. Ornith resident and a
   `requires: gpu-box` card next → `release_for`, the only thing in that module that stops
   a process. **Identified by port, never by image name** (§9e).
3. **Stop what this run started; refuse what it did not.** `_STARTED_PORTS` already carried
   that rule for the end-of-run teardown; `release_for` applies it at a boundary inside the
   night instead of inventing a new kind of initiative. A llama-server Karel started is not
   an unattended process's to kill at 4 AM, so the runner skips the art card and logs why —
   the card stays in `tasks/`, exactly like one whose capability this host lacks. Same
   reasoning gives a hand-started server **no watchdog**: reaping someone's session after
   half an hour of thinking is worse than the failure being prevented.
4. **A release is measured, not assumed.** `_await_headroom` waits for the port to go quiet
   *and* for `suite.available_memory_gb()` — the scarcer of free physical memory and commit
   headroom — to reach the conflict's declared `needs_gb`. Under `mlock` the pages are
   pinned and cannot degrade gracefully, and `VirtualLock` may have silently not been held
   at all, so a check reading *intended* footprint would be wrong on exactly the machine
   where it matters.
5. **No gate, and that is the finding.** The bargain `comfy_server_teardown` enforces —
   whatever launches the server also starts its watchdog — is kept here **by
   construction**: the launcher is `ensure_model_server`, which starts the watchdog itself,
   so there is no document for a gate to read and no pair an author can split.
   `run_ornith.bat` is untracked in both repos besides. A gate is earned by an observed
   failure (`.ai/CLAUDE.md`); this one was designed out instead.

What is *not* built, and was not asked for: a refusal inside the launchers themselves
(item 2 of the earlier sketch). The runner covers unattended dispatch and the two asset
skills' §0 preflight covers an interactive session, which are the two ways ComfyUI
actually gets started here. A third check inside `run_ornith.bat` would be a fourth place
the same fact lives.

### pytest under memory pressure — and the floor that bypasses the cap

Karel: *"I'm still thinking about the python tests… During the night, the longer run may not be
such a problem."*

The cap that protects this already exists and is already correct in shape.
`suite.available_memory_gb()` reads **the scarcer of free physical memory and Windows commit
headroom** (changed to do so on 2026-08-10, after exactly this class of incident), and
`worker_count` subtracts a 4 GB system reserve before dividing by 1.4 GB per worker. Measured
on Mithlond right now, idle: 17.3 GB available → **9 workers**. With Ornith resident, that
figure goes to roughly zero.

**And that is where it breaks, quietly.** `worker_count` floors at `MIN_WORKERS = 2`:

```python
capped = max(MIN_WORKERS, min(cpus, affordable))
```

So at zero headroom the cap does not serialise the suite — it asks for 2 workers wanting
2.8 GB the box does not have, on top of a 21.7 GB resident model. The comment justifying the
floor says *"a probe that reports almost no free memory is more likely wrong than it is
describing a box that cannot run two interpreters"*, which was true when nothing on the box
legitimately held 21.7 GB. It is no longer true, and commit exhaustion on Windows is
system-wide: the allocation that fails belongs to whichever process asks next, which is how
2026-08-10 took out the editor rather than the suite.

Karel's instinct is right that wall clock is the cheap axis at night. So the answer is to
**spend it deliberately rather than to hope**: while the local runtime holds the box, the suite
runs serially — not floored at 2. That is one conditional in `parallel_args`.

### "Only smaller tests while coding, the full suite after the server is down"

Karel's proposal, 2026-09-17. It is the right shape, and measuring it turned up something
worse than the problem he was describing.

**There is no small per-card test run today. The "slice" is the suite.** Measured across every
`junit.xml` under `.ai/runs/`:

| | |
|---|---|
| Tests run per card, median | **2,562** |
| Test functions in the tree | 2,520 (187 files) |
| Wall clock, median / max | **78 s / 197 s** (at 9 xdist workers) |

The selector is not broken — it is doing what it says. `classify` sorts a changed path into
`game` / `system` / `board` / `note` / `other`, and in this repo **the `game` bucket is 2,381
of 2,520 test functions, 94% of everything**, because 171 of 187 test files import
`project_tigress`. A bucket that holds 94% of the suite cannot narrow anything. So today every
card — local or cloud — pays a near-full suite run, and under a resident 21.7 GB model it
would pay it serially.

Two consequences, and the first matters whether or not anything goes local:

1. **A real slice would have to be per-module, not per-bucket** — changed source files mapped
   to the test files that import them, rather than to a bucket. That is a framework change of
   real size, and it is worth doing on its own merits: it is ~78 s off every card the runner
   has ever dispatched.
2. **The batch shape Karel describes already exists, and it is the chore batch.** `chores.py`
   lands N workers on one `chores/…` batch branch, runs **one full-suite pass over the merged
   result** (`BATCH_TEST_TIMEOUT_S = 1800`, and its comment says plainly *"this is the whole
   suite by definition, not a slice"*), and bisects on red with `MAX_RED_ROUNDS = 2` plus
   ~log2(n) probes. That is exactly "cheap checks during the work, the real suite once at the
   end", already built and already proven.

**So the local stretch should be shaped as a batch, not as N independent cards.** Dispatch the
eligible cards onto a batch branch with the server up and only gates plus a narrow slice per
card; stop the server; run the full suite once over the merged branch; merge or bisect. The
memory conflict disappears, because the suite and the model are never resident together, and
the per-card cost during the stretch drops to the gates plus whatever a genuine slice costs.

### The sequencing arithmetic, now measured

Karel, 2026-09-17: *"if starting a server is one minute, we may also stop the server after
every card and run the tests"* — i.e. keep the per-card pipeline and reclaim the memory
between cards instead of batching. Three numbers were missing and all three now exist:

| Measurement | Value | How |
|---|---|---|
| Serial full suite | **288.6 s** (2,644 passed, 1 skipped) | `pytest -q -p no:xdist`, idle box, 2026-09-17 |
| Full suite at 9 workers | 78 s median | the archived `junit.xml` corpus |
| Sequential read of the 21.7 GB GGUF | **1.68 GiB/s → ~12 s** | timed read of `Ornith-1.5-35B-Q4_K_M.gguf` on the 990 EVO Plus |

Two things fall out that neither of us assumed.

**Parallel speed-up is 3.7×, not 9×.** The suite has a serial bottleneck (headless pygame
setup and fixture cost, most likely), so capping workers hurts far less than the worker count
suggests. What a *2*-worker run costs is still unmeasured, and measuring it means passing an
explicit `-n`, which `CLAUDE.md` forbids for good reasons — so it is a deliberate one-off
exception or it is not done at all.

**The restart's real cost is the shared prefix, not the weights.** Cards dispatched with the
same charter send an identical system-prompt + charter + CLAUDE.md prefix, and llama-server
prefix-matches it across requests — 4 tokens instead of 15,290 (§1). A restart throws that
away, and at the measured 376 tok/s that is **~112 s to refill a 42k Claude Code floor, ~32 s
for a 12k OpenCode floor.** So stopping the server costs ~50–150 s per card, not the ~20–40 s
of the model load. This is a second, independent argument for Route B that has nothing to do
with context headroom: a lighter floor makes restarts three times cheaper.

The comparison, per card:

| Sequencing | Cost per card |
|---|---|
| Server up, suite serial | **289 s** |
| Server stopped per card, suite at 9 workers | 78 s + 52–152 s restart = **130–230 s** |
| Server up, suite parallel *(only if §7's memory hypothesis holds)* | **78 s** |

So per-card stop/start beats keeping the server up by roughly 60–160 s, **and it keeps the
existing per-card pipeline** — no batch branch, no deferred merge decision, no bisection. That
is a real simplicity advantage over the batch shape above, and it is the reason to prefer it
if the memory collision turns out to be real.

### Whether the collision is real at all — test this before building either

Neither launcher passes `--no-mmap` or `--mlock`, so **mmap is on and page-locking is off**:
the 21.7 GB of weights are *file-backed clean pages*, not private allocations. The OS may
evict them under pressure and re-fault them from NVMe at the 1.68 GiB/s measured above.

That matters because `available_memory_gb` reads **Windows commit headroom**, and file-backed
pages largely do not charge commit. The stack file's *"4.6 GB free mid-benchmark"* is free
**physical** memory — a different meter, and exactly the distinction the 2026-08-10 fix was
about. **So the collision this whole section is designed around may be much softer than either
of us assumed.** Stated as a hypothesis with a mechanism, not a measurement.

It is fifteen minutes to settle, and it should be settled first because it can delete the
work: start Ornith, read `suite.available_memory_gb()` and `suite.probed_worker_count()`, run
the suite, watch it. If commit headroom survives, nothing needs stopping and the only cost is
slower first tokens after each test run as the model's pages fault back in.

**And whichever way that goes, the per-module slice (above) is worth more than either
sequencing.** All three rows in the table are ways of paying for 2,562 tests. A real slice
attacks the 2,562.

## 8. Hooks, charters and skills under a second runtime

Relevant to Route B only; Route A inherits all of this unchanged.

Of the eleven hooks `.claude/settings.json` wires, **six matter to a dispatched worker** —
`worktree_fence`, `fold_fence`, `ideas_fence`, `commit_pathspec`, `gates_on_edit`,
`tool_economy`. `tier_guard` matches `Agent` spawns and `preflight_guard` matches
push/merge/PR, neither of which a dispatched worker does. Those six are the safety layer that
makes `bypassPermissions` survivable, so under Route B they are not optional and not a later
phase.

They are Python modules reading a JSON payload on stdin and answering with a decision, so the
bridge is a single OpenCode plugin translating `tool.execute.before` into that payload and the
answer back into a permission result. One shim, six hooks, and the hooks themselves stay the
one implementation. **If the shim ever needs a per-hook special case, that is the finding to
report** — it means the hooks were Claude-Code-shaped in a way nobody had noticed.

Charters are the easier half, and the Session I runbook already named the acceptance test:
*"the check that matters is that a `.claude/agents/` charter runs **unchanged** against it,
because §3's whole claim is that the runtime is a config toggle rather than a second code path.
If a charter needs editing to work locally, that claim is false and that is the session's most
important finding."* The charter **body** is harness-agnostic prose; only the frontmatter
differs. `nightshift.update` already renders templates into a repo and is the right place to
render `.opencode/agent/*.md` from the same source, so there is one charter and two
projections — never two charters.

**The CLAUDE.md question Karel raised** — *"maybe doesn't need all the instruction that Claude
does"* — is real, and it is measurable rather than a matter of taste. A dispatched worker is
told, via `CLAUDE.md` + `.ai/CLAUDE.md`, roughly 3.5k tokens, a large fraction of which is
about things it never does: the board's lane transitions (the runner owns them), the git
workflow and PR path (it never merges or pushes), the memory reading protocol (the runner's
prompt already overrides it with *"do not orient from memory files"*), branch deletion, the
Command Center. What a worker genuinely needs is the localisation rule, the architecture rules,
the help-catalog rule and the assets rule — the ones that constrain the diff.

So under Route B the worker's instruction set is a **subset, selected by what constrains the
diff**, expressed as OpenCode's `instructions: []` list pointing at a narrower file. Do not
fork `CLAUDE.md`: a second copy of a rule is the drift `.ai/CLAUDE.md`'s own header warns
about. Split the file so both runtimes include the same fragment.

## 9. The test case for Route A

Karel: *"we may try A. We would need a testing case."*

The acceptance test is not "does it answer" — it is **does it produce a valid verdict file**,
because that is what the runner needs and it exercises structured output, tool use and a file
write at once. If Ornith cannot reliably write that file with the right keys, no
downstream question matters.

Four steps, each a gate on the next. Stop at the first failure and report it.

1. **Prove the endpoint contract, not the install.** `curl` the local server's `/v1/messages`
   with an Anthropic-shaped body and a trivial tool definition, and `/v1/messages/count_tokens`.
   The strings are in the binary (§3); this turns that into a response. **If `/v1/messages`
   404s, Route A is dead** and the decision moves to B or C immediately — no proxy, because a
   translating proxy in front of an unattended overnight dispatch is a second thing that can be
   down at 3 AM.

2. **One cloud dispatch first, unchanged.** `--card <id> --max-cards 1` on a real chore, on
   Sonnet. The Session I runbook's reasoning still holds: without it, a local-runtime bug and
   an ordinary dispatch bug are indistinguishable.

3. **The same card, local.** `ANTHROPIC_BASE_URL` pointed at Ornith; same charter, same prompt,
   same worktree machinery. The card: **a `kind: chore` from `Board/done/`, re-run on a scratch
   branch** — there are 24 of them, they are small, their correct outcome is known, and a chore
   is by definition work with nothing to decide. `ad-sound-for-recharge` (1.5 KB) is the
   smallest and is the right first attempt.

   What to record, in the order it will fail: (a) did a verdict file appear with the right
   keys; (b) did the gates and the test slice pass; (c) peak context against 128k, from
   `stream.jsonl`; (d) did the session compact; (e) wall clock against the cloud run; (f) did
   the ghost-Haiku calls 404.

4. **Then the checkers, not more coders.** `stale-hunter` against Session D's known-answer
   fixture (5 findings, 5 true, 0 false positives) is the one pre-existing scored case in the
   repo. §12's refinement is explicit that checkers go first and that the naive order is the
   mistake.

## 9a. Route A trial — the result (2026-09-18, `local-runtime-route-a-trial`)

§9's four steps were run. **Route A works mechanically and fails on the acceptance test.**
The endpoint is real, the CLI drives it, tools and edits land — and neither dispatch produced
a valid verdict, for two different reasons. Ornith: `Ornith-1.5-35B-A3B Q4_K_M`, `-ncmoe 26`,
`--ctx-size 131072`, port 8082.

### Step 1 — the endpoint contract: PASS

| Probe | Result |
|---|---|
| `POST /v1/messages` + a trivial tool | **200** — `stop_reason: "tool_use"`, well-formed `tool_use` block, 11.3 s |
| `POST /v1/messages/count_tokens` | **200** — `{"input_tokens":289}` in 3.6 ms, agreeing exactly with the Messages call's `usage.input_tokens` |
| Streaming | **200** — correct message_start / content_block_start / content_block_delta SSE shape |

Three corrections to §3 and to §9's expectations:

- **The endpoint strings are in `llama-server-impl.dll`, not `llama-server.exe`.** A scan of
  the `.exe` finds neither `/v1/messages` nor even `/v1/chat/completions` and reads as a 404.
- `usage` carries `cache_read_input_tokens` but **no `cache_creation_input_tokens`**, and
  `thinking.signature` is an empty string. Neither broke the CLI.
- **The ghost-Haiku calls do not 404 — and that is worse than if they did.** The server
  ignores the `model` field entirely: `claude-sonnet-5` and `claude-haiku-4-5-20251001` both
  return `"model":"ornith-1.5-35b-a3b"`. Every internal housekeeping call is silently served
  by the 35B model. §9's step-3 checklist asked whether they 404; the honest answer is that
  there is no way to tell them apart at the endpoint, so per-model routing is not observable
  under Route A and any future cost or latency accounting cannot separate them.

### Steps 2 and 3 — the same chore, cloud then local

`ad-sound-for-recharge` could not be used: its commit predates the `dungeoneer/` →
`project_tigress/` rename, so `git revert` conflicts. **`stun-notification` (2026-09-14)
reverts cleanly onto `test`** — 8 files, 168 deletions — so the undone base is a real revert
rather than a reconstruction. Both dispatches ran the same re-issued card from that base, via
`--card <id> --base trial/route-a-<variant>`, Ornith resident for both so memory pressure
matched.

| | Step 2 — cloud (Sonnet) | Step 3 — local (Ornith) |
|---|---|---|
| **Verdict file** | **`outcome` / `summary` / `how_to_test`** | **none** |
| Gates | 53 clean | not reached |
| Test slice | 2317 passed, 1 pre-existing skip | not reached |
| Reviewer | `code-reviewer @ opus` → `ok` | not reached |
| **Peak context** | **126,779 — 96.7% of 128k** | **130,621 accepted; the next request was 131,183** |
| Compaction | none | **none — it hit the wall and died** |
| Assistant turns | 149 | 106 |
| Wall clock | 22 m 53 s | 38 m 31 s |
| Cost (API-equivalent) | $3.48 | $1.24 |

Step 3 ended on:

```
API Error: 400 request (131183 tokens) exceeds the available context size (131072 tokens)
```

**111 tokens over, at 38 minutes, mid-edit.** Two things follow, and the second is the one
that matters.

- **Claude Code did not compact.** §1 named auto-compaction as the thing that invalidates the
  prefix cache and designed against it. That is not the failure mode here: the CLI has no
  compaction path for a 400 from the endpoint, so it does not degrade, it dies. §1's constraint
  should be restated — the risk is not that compaction is expensive, it is that there is none.
- **The cloud baseline needed 96.7% of Ornith's entire window for this chore.** A 1.5–2.5 KB
  chore with a known answer is at the *bottom* of §2's distribution, and it still left 4,293
  tokens of headroom. §2 read the 42k startup floor as the biggest available lever; this trial
  says the floor is not the binding term. Sonnet fit and Ornith did not, on the same card,
  through the same harness, with the same floor.

**Ornith was not wrong, it was slow.** Its two committed lines add `icon_key` to `_Entry` and
import `draw_status_icon` — the same design Sonnet's verdict describes (*"a procedural
lightning-bolt icon (`FloatingNumbers.add_icon`, reusing `entity_renderer.draw_status_icon`)"*),
reached independently. Its 3 `Edit` calls all applied. The approach was right and it ran out of
window before it could finish.

### Step 4 — `stale-hunter` against Session D's fixture

Run against a detached worktree at `7cf2a82^` (the tree the answer key was written for; the
doc has been rewritten since, so today's tree would legitimately find nothing). Same
`run_stale_check` code path, same charter, same prompt, cloud run as a control.

| | Cloud (Sonnet) | Local (Ornith) |
|---|---|---|
| **Verdict parsed by the runner** | **yes** | **no — read back as `{}`** |
| Findings vs. the 5-finding key | **6** — all 5, plus a real one the key missed | **3 true, 0 false positives** |
| Citations | accurate | **accurate** (spot-verified) |
| Assistant turns | 66 | 66 |
| **Peak context** | **59,778 — 45.6%** | **114,657 — 87.5%** |
| Tool mix | Grep 21, Read 9, Glob 9 | Read 16, Grep 7, Glob 2 |
| Wall clock | 2.6 min | 23.0 min |
| Cost (API-equivalent) | $0.62 | $0.81 |

Three results, in order of how much they change the plan:

1. **The structured-output contract is what fails, not the analysis.** Ornith's three findings
   are true positives with correct `file:line` evidence, spot-verified against the fixture
   tree. But it emitted a **bare JSON array** with keys doc_line / verdict / evidence /
   note, where the prompt spells out an **object** with `complete` / `findings` / `summary`
   and `claim` / `cite` / `why`. `_verdict_from_message` requires `isinstance(found, dict)`, so
   23 minutes of correct work was discarded as "not verified". This is a **new instance of the
   exact failure class that function's own docstring was written to prevent** — a handover
   channel that silently no-ops — and §9's premise ("if Ornith cannot reliably write a verdict
   with the right keys, nothing downstream matters") is therefore answered directly: it cannot,
   on the task the roster was most confident about.
2. **Same turns, 1.9× the context.** Both runs took exactly 66 turns; Ornith peaked at 87.5% of
   the window against Sonnet's 45.6%. The tool mix explains it — Ornith `Read`s whole files
   where Sonnet `Grep`s. That is the mechanism behind step 3's overflow, and it is a property of
   the model's habits rather than of the window: a bigger `--ctx-size` moves the ceiling without
   touching the slope.
3. **The fixture's answer key is itself one short.** The cloud control found a sixth real drift
   — `hack_grid_generator.py` documented at ~581 lines, actually 720 — which the 2026-07-22 key
   does not list. §10 cites this fixture as "the nearest thing to a golden set that exists";
   it is still that, but it is a 6-finding case recorded as a 5-finding one. Doc 10 §7's
   parenthetical "and one known drift missed" may already be this.

### What this settles, and what it does not

**Settles.** Route A's plumbing is not the obstacle — endpoint, streaming, tool calls, edits
and permissions all work through an unmodified `claude` CLI, exactly as §3 predicted, at
approximately zero framework change. The obstacles are (a) a 128k window that the *cloud*
baseline already fills to 96.7% on a small chore, with no compaction path when it overflows,
and (b) schema compliance on the verdict, which fails on the checker task too.

**Does not settle.** Whether a larger `--ctx-size` rescues step 3 — untried; only 728 MiB of
VRAM was free at `-ncmoe 26`, so it needs `-ncmoe 30`+ and costs generation speed. Whether a
schema-repair pass (a strict re-prompt, or teaching `_verdict_from_message` to accept a bare
array and normalise key aliases) would lift step 4 from "discarded" to "3/5 recorded" — that is
a framework change, and §9 deliberately measured the model with nothing else varying.

**The two cheapest levers on the context slope, both untested and both config-only.** Named
here because result 2 above invites the obvious fix and the obvious fix is the smaller of the
two:

- **Retained reasoning is the larger term.** Ornith emitted **44,796 thinking tokens** in step
  3 and **22,875** in step 4, against Sonnet's **39**. `llama-server` preserves the reasoning
  trace across the whole history by default and says so at startup (*"chat template supports
  preserving reasoning, it is enabled by default (may use more tokens, disable via
  `--no-reasoning-preserve`)"*). That is roughly **34% of the 131k window in step 3** and
  **20% in step 4** spent re-transmitting the model's own thinking. The run_ornith.bat launcher passes
  neither `--no-reasoning-preserve` nor `--reasoning-budget N`.
- **Steering the tool mix is the smaller one.** `stale-hunter`'s grant is already
  `Read, Grep, Glob`; the charter or the prompt could prefer `Grep` and require bounded `Read`
  (`offset`/`limit`). This narrows the Read-vs-Grep gap behind result 2 but leaves the
  reasoning overhead untouched.

Both change the harness or the launcher rather than the model, so neither belongs to this
trial's measurement — they are the next experiment, and the first one is a single flag.

**§7 still governs, and nothing here promotes anything.** Per §9's own acceptance test, the
trial is a **fail**: no valid verdict on either dispatch. The two candidate follow-ups above are
the measurable questions it leaves, not conclusions it reached.

## 9b. Scoping the follow-up (open, 2026-09-18)

<!-- stale-ok: quotes Karel verbatim, and the quote names agents.md - OpenCode's root instruction-file convention, not a path in this repo. The tracked file is .opencode/AGENTS.md. -->

`Board/inbox/fine-tune-ornith-own-harness.md` asks to make Ornith work on this repo's work
through a harness of its own, and to benchmark that separately from the §9a trial. This section
records what the maintainer settled at the keyboard; §9c records what was then measured.

**"Fine-tune" in that note does not mean training the model.** Karel, 2026-09-18, asked
directly: *"By fine tuning I didn't mention training the model. I meant tinkering with hooks,
agents.md, server properties and such things. Making this model work better through the
setup."* No weights are changed anywhere in this work, nothing is rented, and the hardware
question the note raised for triage does not arise. Read every "tuning" below as
configuration.

**Settled by Karel, 2026-09-18:** "own harness" means **Route B — OpenCode**, with its own
root instruction file — OpenCode's AGENTS convention, not this repo's CLAUDE.md — and its own hooks, plus the config-only optimisations §9a
named. Not Route C, not a thinner client of our own. §3's Route B table and §8 are therefore
the live design, and `research_notes.md`'s standing warning still binds: OpenCode as a
*second* runtime for a *named subset* of agents, never a replacement.

**The three asks have different costs and different evidence-value, and the note's own logic
orders them.** Recorded because doing them in the asked order would credit the expensive one
with a win the cheap one would have given for free:

1. **The launcher flags — one flag, no framework change.** `--no-reasoning-preserve` /
   `--reasoning-budget N` against the 44,796 and 22,875 retained thinking tokens §9a measured
   (34% and 20% of the window). This is the largest single context term found so far and it
   costs one edit to run_ornith.bat. It must run before anything else, on the same two
   dispatches, or its effect is unattributable.
2. **The own harness — a real framework change, and the actual test of §2's claim.** §2's
   ~7–15k floor against Claude Code's measured 42k median is still unverified, and §9a
   sharpened why it matters *less* than §2 assumed: the *cloud* baseline filled 96.7% of
   Ornith's window on the same chore through the same floor. So the harness buys ~30k, which
   §2 already predicted moves perhaps three of nine attempts. The floor is worth taking and it
   is not on its own sufficient — expect the OpenCode run to succeed or fail on the slope, not
   the intercept. Cost is enumerated in §3: 11 argv sites across 5 modules, plus the hook shim.
3. **The verdict contract — and §9c found the defect is ours, not the model's.** §9a's headline
   is that *analysis passed and schema compliance failed*. Route C already names the fix
   (*"grammar / JSON-schema constrained decoding so a malformed verdict is structurally
   impossible"*). §9c goes further: the charter and the runner's prompt specified **two
   different schemas**, and the prompt opened by saying *"Follow your charter"*. That is a
   harness defect, and it is fixed by editing the charter rather than the model.

**The fixture and its answer key are no longer the same pair — and this is the blocking
finding.** The note asks for the key to be corrected from 5 findings to 6. That is not
sufficient, because the two numbers were measured on two different trees:

| | Tree | _COL_ENEMY_PING present? |
|---|---|---|
| The key, `.ai/corrections.archive.log` 2026-07-22 | `arch_detail.md` @ **`3b2647e`** | **yes** — 1 occurrence |
| §9a step 4's run | detached worktree @ **`7cf2a82^`** = `69f82dd` | **no** — 0 occurrences |

`3b2647e` is an ancestor of `7cf2a82^`, and the two revisions of `arch_detail.md` differ (15
insertions, 4 deletions). _COL_ENEMY_PING is the marker of **known drift #2** — the
MinimapOverlay drift the archive log records stale-hunter as having *missed*. It is absent from
the tree §9a actually scored, so that drift was already fixed out of the corpus before the
trial ran. Consequences, all of them scoring consequences:

- The key's fixture (`3b2647e`) has **6** ground-truth drifts: the 5 recorded findings plus the
  missed known #2. It was never a 5-finding case; it is a 5-of-6 *recall* result written down as
  a 5-finding key.
- §9a's speculation that doc 10 §7's "and one known drift missed" *"may already be"* the
  `hack_grid_generator` finding is **wrong**. The missed drift is MinimapOverlay; the
  `hack_grid_generator` line-count drift is a separate, additional one.
- So the correction is not `5 → 6`. It is: **pin the fixture to an exact commit and enumerate
  the drifts as a list rather than a count.** A count cannot survive a corpus that moves, and
  §7 requires known-good outcomes, which a bare integer is not.
- Until that is done, both §9a numbers are uncomparable to the 2026-07-22 result — not wrong,
  just scored against a different fixture. Ornith's "3 true, 0 false positives" and the cloud
  control's "6" need re-stating against whichever tree the pinned fixture ends up being.

**Still open for triage, unchanged by the above:** whether the benchmark is a new artefact or an
extension of the §10 inventory. The fixture-pinning work above is a prerequisite either way, and
§10's prohibition on synthesising tasks to pad the set stands.

## 9c. Route B trial — the own harness, measured (2026-09-18)

§9b recorded what Karel settled; this records what was then built and what it measured. Ornith
is unchanged from §9a — `Ornith-1.5-35B-A3B Q4_K_M`, `-ncmoe 26`, `--ctx-size 131072`, port
8082 — **plus `--no-reasoning-preserve`**. No weights were touched.

### What was built

| Artefact | Where | Note |
|---|---|---|
| Project instruction file | `.opencode/AGENTS.md` | 2.0 KB, against `CLAUDE.md` + `.ai/CLAUDE.md` at 13.9 KB |
| Project config | `.opencode/opencode.json` | model, instructions, permissions; the three local providers stay in the machine-wide config |
| Projected charter | `.opencode/agent/stale-hunter.md` | carries **one** verdict schema — the one the runner parses |
| Re-pinned fixture | `.ai/fixtures/stale-hunter/arch_detail.json` | 5 enumerated drifts at one commit |

### Result 1 — the floor, and §2's claim is confirmed

| Startup floor, one dispatch | tokens |
|---|---|
| Claude Code on this repo (§2, median / max) | 42,133 / 60,526 |
| **OpenCode + project instruction file + charter** | **14,018** |

From OpenCode's own usage accounting (`opencode run --format json`), one turn, read/grep/glob
schemas declared. That is **28,115 tokens reclaimed, 67% of the floor** — and it is measured
*with this repo's own instructions loaded*, not on a bare harness. §2 predicted ~7–15k under
OpenCode and §9a said only a runtime change could test it. It tested true.

The gain is smaller than it looks against §9a's real obstacle, and §2 already said why: the
*cloud* baseline filled 96.7% of Ornith's window on one chore. 28k of headroom moves perhaps
three of the nine attempts that overflow 128k. **It does not move the six that passed 200k.**

### Result 2 — `--no-reasoning-preserve` works, and it is one flag

<!-- stale-ok: names llama-server's and OpenCode's own wire fields (reasoning_content, completion_tokens, max_tokens, usage) - a third-party API's payload, not symbols in this repo's source. -->

The flag works, and it does **less** than first recorded here. It stops the reasoning trace
being **re-sent across the whole history**, which is what §9a's **44,796** (chore) and
**22,875** (checker) were: a cumulative cost paid again every turn. llama-server confirms it
at startup by inverting its own hint (*"consider enabling it via `--reasoning-preserve`"*).

**It does not stop reasoning being generated, and OpenCode's `"reasoning": 0` does not mean
it was** (Karel, 2026-09-18: *"Reasoning is 0 loaded back, but it is still an output token,
isn't it?"* — yes). Probed directly: a single `/v1/chat/completions` call returned
`reasoning_content` of 1,758 characters beside 370 characters of `content`, and `usage`
carried one `completion_tokens: 994` with **no reasoning breakdown at all**. So the zero is an
absent field read as a measurement, and every turn still spends output budget on thinking that
never reaches the final message.

Two consequences, both of which bit later the same day: `max_tokens` bounds reasoning **plus**
content, so a thinking model needs an output cap far larger than its visible answer suggests
(§9f); and output-token counts cannot be compared against surfaced text length, which is how
a phantom "16× spread" got recorded and then withdrawn.

### Result 3 — §9a's schema failure was the harness's, not the model's

<!-- stale-ok: names the keys of the SUPERSEDED verdict schema (doc_line/verdict/evidence/note) on purpose - the whole point of the result is that they are not the accepted contract. A doc that cannot quote a wrong schema cannot explain why it was wrong. -->

**This is the finding that changes what the note asked for.** §9a recorded that Ornith
"emitted a bare JSON array with keys `doc_line`/`verdict`/`evidence`/`note` where the prompt
specifies an object". Both documents were in that prompt, and **they disagree**:

- `.claude/agents/stale-hunter.md` headlines *"## Output — one JSON array, nothing else"* and
  gives exactly `claim`/`doc_line`/`verdict`/`evidence`/`note`. The runner's object form is a
  prose paragraph two-thirds down, qualified with *"When the runner drives you"*.
- `runner.py`'s `_STALE_PROMPT` opens with *"Follow your charter"* and then specifies
  `complete`/`findings`/`summary` with `claim`/`cite`/`why`.

So Ornith emitted the schema its charter headlines, having been told to follow that charter. It
was not failing to hold a schema; it resolved a real conflict toward the document the prompt
pointed at. Sonnet happened to prefer the later, more specific instruction — which is a habit,
not a contract. **The fix is to give the charter one schema**, which the projected OpenCode
charter now does. `.claude/agents/stale-hunter.md` still carries both and should be corrected
the same way; it is a framework-side charter, so that is a separate, paired change.

This also reframes §9's premise. *"If Ornith cannot reliably write a verdict with the right
keys, nothing downstream matters"* was answered "it cannot" — on evidence that never isolated
the model from a contradictory prompt.

### Result 4 — five integration facts, each of which would have broken a Route B dispatch

<!-- stale-ok: names OpenCode's own config and permission keys (external_directory, package.json, package-lock.json) - a third-party tool's schema and its generated, gitignored files, none of which are symbols in this repo's source. -->

Found by driving it, not by reading the config schema. §3's table is right about the flags and
silent about all of this:

1. **Project config must live at `.opencode/opencode.json`.** A repo-root `opencode.json` is
   read for settings — model and instructions both applied — but does **not** enable
   `.opencode/agent/*.md` discovery. The charter simply does not exist, with no warning.
2. **A projected charter must be `mode: primary`.** `mode: subagent` is not dispatchable by
   `opencode run --agent` and **hangs indefinitely** rather than erroring. Under nightshift the
   checker *is* the top-level agent of its own headless run, so `primary` is also the honest
   description.
3. **A wrong `--agent` name fails silently.** `--agent general` (a built-in subagent) ran under
   the default `build` agent instead; the only tell is a banner, and **the banner goes to
   stderr** while the result goes to stdout. A runner parsing stdout cannot see which agent
   actually ran. Any Route B dispatcher must assert the agent name against `opencode agent
   list` before it trusts a result.
4. **OpenCode's project-root detection does not handle git worktrees.** In a detached worktree
   `.git` is a *file* pointing at the main repo, so OpenCode classes files **inside** the
   worktree as `external_directory` — which is `ask` by default, and `ask` auto-rejects in
   headless mode. The first dispatch died on
   `permission requested: external_directory (...); auto-rejecting` after a single `read`.
   nightshift dispatches exclusively in worktrees, so this is load-bearing: the config needs
   `"external_directory": "allow"`, and the sandbox is then the worktree itself.
5. **There is no `--add-dir`, so projection is a file copy.** `--dir` sets cwd only (§3 noted
   the gap; this is what it costs): the charter, the instruction file and the config must be
   copied into each worktree before dispatch. Charter projection is therefore a per-dispatch
   copy, not a one-time write.

Also: OpenCode installs `@opencode-ai/plugin` into `.opencode/node_modules` on load, creating
`package.json` and `package-lock.json` beside it. All three are generated and gitignored.

### Result 5 — the fixture had to be rebuilt, not corrected

<!-- stale-ok: every name in this section is read at commit 3b2647e, before the dungeoneer/ -> project_tigress/ rename and before the drifts were fixed. _COL_ENEMY_PING is supposed to be absent from today's tree - that is the drift being recorded. -->

§9b established that the key and the fixture were different trees. Verifying every claim
mechanically at `3b2647e` — the commit the 2026-07-22 record names — settles it further, and
the conclusion is stronger than the note's:

**The key was never reproducible, because the run it records was never pinned.** The archive
log describes `arch_detail.md@3b2647e` checked against *"CURRENT source"* — the then-live tree,
which kept moving. So neither "5 findings" nor §9a's "6" can be recovered from a commit pair.

Re-verified at `3b2647e`, doc and source both read at that one commit, the fixture holds
**5 distinct drifts**:

| id | doc claims | source at `3b2647e` says |
|---|---|---|
| `deadline-dist-mult` | `DEADLINE_DIST_MULT=2.0` | `settings.py:261` = `3.0` |
| `minimap-foothold-ping` | enemies pinged via `_COL_ENEMY_PING`, `draw(…, foothold)` | `minimap_overlay.py:97` takes `(screen, floor, player)`; symbol absent |
| `tiger-feed-affinity-mult` | `TIGER_FEED_AFFINITY_MULT`(3) | `settings.py:442` = `2` |
| `skill-catalog-count` | `CATALOG … with 31 entries` (doc:145) | `skills/catalog.py:56` holds **32** |
| `resolver-first-param` | `resolve_move/…/recharge(node,…)` (doc:31) | first param is `actor`; the same sentence later writes `actor` |

Three corrections follow, and each one would have mis-scored a future run:

- **The note's `5 → 6` is not the fix.** At this commit the ground truth is 5 distinct drifts.
  The implied 6 came from counting *repeated occurrences* of one wrong value as separate
  findings — the archive log's own `(x2)` and `(x3)` annotations — so a 5-drift fixture was
  written down as "5 found plus 1 missed".
- **The `hack_grid_generator` drift is real but belongs to a different fixture.** No line-count
  claim for that file exists in `arch_detail.md` at `3b2647e`; it appears in a later revision,
  which is the one §9a's cloud control read at `69f82dd`. §9a's guess that doc 10 §7's *"one
  known drift missed"* was this finding is wrong — that was `minimap-foothold-ping`.
- **The 2026-07-22 recall was 4 of 5, not 5 of 5.** It missed `minimap-foothold-ping`. The
  precision claim (zero false positives) survives unchanged.

**Scoring rule, now written into the artefact: score distinct drift claims, never a finding
count.** A count cannot survive a doc that repeats itself or a corpus that moves, and §7 asks
for known-good outcomes, which an integer is not.

§10 calls this fixture *"the only case with a hand-verified answer key"*. After this it is one:
before, it had a hand-verified **result**, which is not the same artefact.

## 9d. What Ornith can actually be given (scored, 2026-09-18)

<!-- stale-ok: names the superseded verdict keys and the dungeoneer-era fixture paths on
purpose, for the same reasons 9c's Result 3 and Result 5 do. -->

§9c fixed the setup. This is the scored dispatch that followed, and the answer to Karel's
question — *what work can Ornith be given in optimised conditions?*

### The dispatch

<!-- stale-ok: compaction_continue is a field in OpenCode's own JSON event stream - a third-party tool's wire format, not a symbol in this repo's source. -->

`stale-hunter` against the re-pinned fixture (`arch_detail.md` @ `3b2647e`, 5 distinct
drifts), OpenCode runtime, projected charter, `--no-reasoning-preserve`, verdict scored
mechanically against `.ai/fixtures/stale-hunter/arch_detail.json`.

| | §9a (Claude Code, `69f82dd`) | §9d (OpenCode, `3b2647e`) |
|---|---|---|
| **Verdict parsed by the runner** | **no — read back as `{}`** | **yes — `complete`/`findings`/`summary`** |
| `complete` | n/a | `true` |
| Startup floor | 42,133 (median, §2) | **14,018** |
| Reasoning *re-sent per turn* | 22,875 thinking tokens | **0** (still generated, just not re-sent) |
| Peak context | 114,657 (87.5% of window) | ~68,000, then **compacted** |
| Session outcome | survived; verdict discarded | survived; **verdict usable** |
| Tool mix | Read 16 / Grep 7 / Glob 2 | Read 45 / Grep 37 / Glob 2 |
| Findings | 3 | 3 |
| **Recall vs. the pinned key** | not comparable (other tree) | **2 of 5** |
| **Precision** | reported 0 false positives | **2 of 3; the third is mechanically rejectable** |

**Two things the harness fixed outright.** The verdict now parses — §9's whole acceptance test
— and the session **compacted and continued** (`compaction_continue: true` in the stream).
§9a's step 3 died at 131,183 because *"the CLI has no compaction path for a 400 from the
endpoint, so it does not degrade, it dies"*. OpenCode degrades. That removes the failure mode
§9a called the binding one.

### The false positive is the useful result

Finding 3 claimed `rifle Mk2 proto_pierce / Mk3 range_tiles=2+diagonal=False`. The doc
(line 37) actually reads *"rifle Mk2 `proto_pierce` / Mk3 `proto_crit_bonus`; sword Mk2
`proto_pierce` / Mk3 `range_tiles=2`+`diagonal=False`"*, which matches
`combat/damage.py:53-63` exactly. Ornith **spliced two clauses of one sentence** into a claim
the doc never makes, then correctly reasoned that its own fused claim contradicts the source.
Its `why` is accurate; its `claim` is not a quote.

**That failure is deterministic to catch.** The charter's rule 2 is quote-or-drop, and the
claim string simply does not occur in the doc. A normalised substring check over the pinned
doc rejects it without a model, without a judgment call, and at zero cost. Scored that way:

- 3 findings, **2 verbatim** and both true positives, **1 spliced** and therefore rejected.
- **Precision behind the quote check: 2 of 2. Recall: 2 of 5.**

This is the shape of every local-checker result worth having: the model supplies candidates,
a gate supplies the guarantee. §12's *"reliable because narrow"* needs one more word —
reliable because narrow **and filtered**.

### The trap this run exposed, and it would have done real damage

<!-- stale-ok: stale_ledger.json is gitignored and created at runtime, so it legitimately does not exist in a fresh clone - same reason ai_team.md already carries a marker for it. -->

`stale_sweep.py` records a doc as verified **only on a `complete` verdict** — Karel's rule
that a cut-off sweep re-checks rather than lies. Ornith returned `complete: true` with **2 of
5** drifts found. `complete` means *"I read the whole document"*; the ledger treats it as
*"this document is now checked"*. Those are not the same claim, and on a 40%-recall model the
gap is three live drifts recorded as verified and never re-examined.

**So a local `stale-hunter` must not write `stale_ledger.json`.** Until `complete` is split
from *exhaustive*, a local run is a lead generator whose output a human or a gate triages —
never an input to the verification ledger. This is not a tuning problem and no flag fixes it.

### The answer: what goes to a night run, and what does not

Ordered by *cost of being wrong*, which is the only ordering that matters for unattended work.

| Role | Verdict | Why |
|---|---|---|
| **`classifier`** | **yes — first** | Its charter forbids opening the codebase, so its input is a payload and the 128k window never binds. It reports a routing **for Karel to confirm before anything expensive runs**, so a wrong answer costs one keystroke. §10 already says its golden set builds from `Board/inbox/` history. |
| **`stale-hunter`** | **yes — as a lead generator, behind the quote check, with the ledger write off** | 2/5 recall cannot certify a doc clean, and doc 08's Tier-2 design assumes it can. But precision behind the gate was perfect, and it runs for free overnight across many docs. Findings are leads. |
| **`chore`** | **not yet — measure it next** | §9a's overflow is now arithmetically gone: the cloud baseline needed 126,779, and 28k of reclaimed floor plus a real compaction path removes both failure modes. But a chore *edits and tests*, which is the producer side, and §12 is explicit that checkers come first. One dispatch, measured, before it is given a night. |
| **`translator`** | **no** | §10 names it as the gap: no scored case exists, and scoring means Karel reading Czech. Not a candidate until there is a set. |
| **`code-thread` / full cards** | **no** | §2: six of 25 attempts passed 200k and *"are not going local on this hardware, and no amount of tuning changes that"*. 2/5 recall on a deliberately narrow task is not an argument for open-ended work. |

**Nothing here promotes anything.** §7 requires a measured threshold on a role's own golden
set, and one dispatch on one fixture is not that. What this run establishes is that the
*mechanism* now works end to end — verdict parses, session compacts, floor is a third — so the
remaining question is quality per role, which is exactly what a golden set is for.

### What is still missing, in the order it blocks things

1. **The verbatim-quote check does not exist as a gate.** It is the cheapest reliability win
   available and this run proves it pays. Scored by hand here with a scratchpad script.
2. **`complete` vs. *exhaustive*.** Until these are separate fields, no local checker may
   touch the ledger.
3. **The runtime abstraction in nightshift** — 11 argv sites across 5 modules, plus
   `claude_binary()`, `_terminal_result()`, `limits.detect()` and the charter check at
   `runner.py:956` (§3). **Not needed to measure anything**: §9a drove `claude` directly and
   §9c/§9d drove `opencode` directly. It is needed the moment the *runner* must dispatch local
   work unattended — i.e. for a night run and nothing before it. Framework half, so paired
   branches, framework-first (`CLAUDE.md`).
4. **A `classifier` fixture**, built from `Board/inbox/` history per §10. The first local role
   should be the one whose golden set is cheapest to build, and that is this one.

## 9e. Operating notes — read before driving Ornith again (2026-09-18)

<!-- stale-ok: names OpenCode's flags and llama.cpp's load modes, a third-party tool's
surface rather than symbols in this repo. -->

The *why* for each of these is one line in `.ai/corrections.log` (2026-09-18, six entries
from `model-server-lifetime-owned-by-the-job` onward). This is the short form, so a future
session does not rediscover them at the cost of a day.

**Run the server separately from the job.** Every wrapper written for these runs started
llama-server, worked, and killed it by image name. Killing by image name kills *every*
instance, so two overlapping runs kill each other's server — and the failure surfaces in the
innocent one, as `Cannot connect to API` about 1 s in. Three separate "bugs" today were this.
Start the server once, supervise it elsewhere, let jobs assume it is there. Doc 04 §12 already
assigns the idle watchdog to `E:\AI\scripts\` for the same reason.

**The launcher flags that matter**, all measured:

| flag | why |
|---|---|
| `--load-mode mlock` | the default `auto` double-buffers whenever tensors are overridden to CPU, which `-ncmoe` always does: 23.4 GB resident and climbing vs **13.8 GB**, and 39.2 vs **46.3 tok/s**. llama.cpp issue #26110 |
| `--no-reasoning-preserve` | stops re-sending the trace across history: 44,796 → 0 re-sent. Reasoning is still GENERATED and still counts against `max_tokens` |
| provider `limit.output` | comes off the top of the context before auto-compaction; 32,768 is a coder's default and wasteful for a checker |

**Keep a projected charter at or under ~7 KB.** Measured on one fixture: 3.6 KB → 2 findings;
7.3 KB → 4 findings, its best run; 9.4 KB → it emitted the schema's own placeholder text as
its answer. Instruction length is a budget. A correct instruction can still cost more than it
returns, and the failure when it does is *not* subtle degradation — it is the model copying
the template.

**Scope work to a section, not a document.** The 245-line `arch_detail.md` never finished in
67 minutes: it replanned, restarted its sweep, and emitted nothing. Halved, the same doc was
two runs of 8m46s and 14m35s, both emitting valid verdicts. This is §2's *"the peak is the
card, and the card is selectable"* applied to checkers.

**The stop conditions exist because of that** (in the projected charter): about to re-read
part of the doc you already read, about to rewrite the plan a second time, or ~80 tool calls
spent → emit now with `complete: false`. Safe only because `complete: false` is now useful
rather than wasteful — see the paired nightshift change.

**A golden set must pin its inputs, and a key from a previous run is a floor.** Both fixtures
hit this from different directions. The `stale-hunter` key scored a tree that had moved under
it; the `classifier` labels encoded charter rules that had since been rewritten (nine
revisions, two of which changed what the routes mean). Both looked like model failures until
the inputs were pinned. And a key derived from an earlier sweep inherits that sweep's blind
spots: Ornith found two real drifts the 2026-07-22 sweep *and* the 2026-09-18 rebuild both
missed. Treat an off-key finding as a candidate to verify, never as a false positive —
precision is measured by the mechanical quote check, recall by the key.

**A measurement harness must never report a rate over "no answer".** The classifier scorer
printed a tidy table with an accuracy line while every dispatch had failed to reach the model,
twice. The dangerous shape is partial: 59 dead cases and one live one reports 100%. Refuse to
score when nothing came back; state the exclusion when only some did.

**Two non-problems, recorded so they are not re-investigated.** Passing a large multi-line
prompt as argv is fine — a stdin rewrite fixed nothing, because the real cause was the server
race. And `FreePhysicalMemory` near zero is not scarcity: under mmap the OS holds reclaimable
file pages, and available memory only *looks* alarming. The number that matters is resident
set over time, not free RAM at a moment.

## 9f. The classifier, scored (2026-09-18)

<!-- stale-ok: names the classifier charter's route labels and OpenCode's config keys,
neither of which is a symbol in this repo's source. -->

§9d picked `classifier` as the first role worth a night run, on the argument that a wrong
answer costs one keystroke. This is the measurement, against
`.ai/fixtures/classifier/inbox_history.json` — 60 real notes whose labels post-date the
2026-09-01 charter rewrite, with `chore` and `scribe` scored as one class because the
charter gives that distinction to the scribe rather than the classifier.

| | |
|---|---|
| answered | **50 of 60** |
| correct | **38** |
| wrong | **12** |
| **wrong toward `inline`** | **12 of 12** |
| `inline` recall | **13 of 13** |

**Every single error landed on `inline`.** Not one fork was routed into an unattended
batch. That is the asymmetry the charter is built around — *"this is the default when you
are unsure"* — and it held without exception across 50 cases. For the role's actual
purpose the flat 76% understates it badly: the expensive mistake is routing work that
needs a decision into a batch that merges as one unit, and it made that mistake zero
times.

### Most of the 12 are the projection's fault, not the model's

Attributed by hand, and the pattern is embarrassing rather than subtle:

- **Four** are framework notes labelled `scribe` that the model routed `inline`, because
  the projected charter said *"never `chore` or `scribe`"* where the real charter says
  *"never `chore`"*. I added `or scribe` while compressing. The model obeyed the rule it
  was given. Logged as `tightened-a-rule-past-its-source`; the projection now matches.
- **Two or three** are the inverse — notes labelled `chore` that the model routed
  `inline`, where **the model is right and the label is stale**: the current charter says
  a framework note is never `chore`, and those labels predate the 2026-08-22 revision that
  established it. Charter drift *inside* the era the fixture had already filtered for, so
  the 2026-09-01 cutoff is necessary but not sufficient.
- The remaining four or five are genuine judgment calls on ambiguous notes, and every one
  of them errs toward asking a human.

Two passes, opposite errors, same cause: the first projection **dropped** the rule (four
errors), the second **over-tightened** it (more errors). The model's own discrimination
was never the variable being measured, and a scored fixture silently attributes the
difference to the model unless the projection is diffed against its source first.

### Reliability is no longer the story, but it is not solved

The first run answered 29 of 60; this one answered 50. The difference is the output cap —
§9e's entry explains why an 8,192 reserve truncated half a run. One batch of ten still
returned nothing at 422 s, with the cap now at 16,384, and that is unexplained. A batch
that silently returns nothing is survivable for a role whose output a human confirms; it
would not be for one that writes to the board unattended.

### What this does to §9d's recommendation

It stands, and for a sharper reason than §9d gave. `classifier` is not the first candidate
because it scores highest — it does not. It is first because **its errors are
systematically safe**: 13 of 13 on the route that spends a person, and 12 of 12 wrong
answers landing on "ask the human". A role whose failure mode is over-caution is the right
one to put in front of a maintainer who confirms before anything expensive runs.

**And nothing here promotes it.** §7 wants a measured threshold on a role's own golden
set; this is one run against a fixture whose labels have now twice been shown to drift.
The number to quote is not 76% — it is *"12 of 12 errors were toward inline, on a
projection that has since been corrected"*, which is a statement about safety rather than
accuracy and is the one the decision actually rests on.

## 10. The golden set — the honest number

§7: *"Nothing goes local without a golden set… 15–30 real past tasks from this repo with
known-good outcomes."* Session I reported the corpus as 3 cards and refused to score on it.
That was the right call, and the number has moved:

- **`Board/done/` now holds 24 `kind: chore` cards**, plus a much larger body of full cards.
- **`.ai/runs/` holds 25 dispatched attempts** with complete `stream.jsonl`, prompts, verdicts,
  gate logs and telemetry; `.ai/runs/records/` holds the run records.
- **`stale-hunter` has Session D's fixture** — the only case with a hand-verified answer key.
- **`.ai/testing_rejections.log`** records cards that reached `testing/` and were rejected: the
  closest thing to a labelled negative set, and worth more than any positive example.

So a `chore-thread` golden set of 15–25 exists today without inventing anything, and a
`classifier` set can be built from `Board/inbox/` history the same way. **`translator` is the
gap**: there is no scored case, and cs/es naturalness is the hardest of the four to score
mechanically, which is why §5 marks it good-on-shape and unmeasured. Scoring it means Karel
reading output, which is a different kind of cost from running a harness.

The Session I prohibition stands verbatim: **do not synthesise tasks to reach 15.** A golden set
of invented tasks measures the inventor.

## 11. Open questions

Listed because they are unanswered — not to be resolved by whoever reads this next without
measuring.

1. **Does `/v1/messages` actually respond?** §9 step 1. Everything in Route A is downstream of
   it.
2. **Does a resident mmap'd model actually cost commit headroom?** §7. Fifteen minutes, and it
   decides whether any of the sequencing work is needed. Settle it first. *(The serial-suite
   number this question used to ask for is now measured: 288.6 s.)*
3. **Does `MIN_WORKERS = 2` need a third state?** The floor is correct for a normally loaded box
   and wrong for one holding a 21.7 GB model. A change there is a framework change affecting
   every consumer, so it lands on paired branches (`CLAUDE.md`'s cross-repo rule).
4. **How is a context wall detected in flight?** §5. Not predicted — detected. A compaction
   event in the stream, or a turn-over-turn input-token threshold. Deterministic, model-free,
   and writable against the 25 archived `stream.jsonl` files without dispatching anything.
5. **Does an unmodified charter run against the local endpoint?** The Session I acceptance
   criterion, and still the most informative single result available.
6. **What is the demotion trigger?** §7 requires automatic demotion on an accept-rate drop, and
   `costreport` already computes `needs_fix_rate`, `parked_rate` and `red_after_merge_rate` per
   run. Write the demotion path in the same change as the promotion path — **a system that can
   only promote is one that silently degrades.**

## 12. What lands where, when it lands

An inventory, so the change is visible before it is made. Nothing here is committed to.

| Change | Repo | Note |
|---|---|---|
| "local_model" block in the host row | Project Tigress (`.ai/hosts.json`) | Data only; the reader is framework-side |
| `local:` card field | both | `card_schema` validation is framework; the recipe text is project |
| Runtime resolution + probe + fallback | nightshift | Near `tiers.py`, but **not in** it — §4 |
| Panel checkbox + row chip | nightshift (`panel.py`) | Amends `TierChoice`'s docstring in the same change |
| `--no-local` | nightshift (`runner`) | |
| Serial-suite conditional | nightshift (`suite.py`) | Open question 3 |
| Context-wall detector + local attempt counter | nightshift (`limits.py`, `runner`) | §5. The detector is writable against the archived streams today |
| Per-module test slice | nightshift (`suite.py`) | §7. Worth doing on its own merits, local or not |
| Local stretch as a batch | nightshift | §7. Shaped on `chores.py`, not invented |
| Idle watchdog for the LLM server | **neither** — `E:\AI\scripts\` | Machine-wide, like the Comfy watchdog. ✅ §7 names it |
| The mutex itself | nightshift (`runtimes.py`, `runner`) | ✅ §7. The runner is the thing that knows a `gpu-box` card and a local card share a night |
| `conflicts` + `watchdog` in the host row | Project Tigress (`.ai/hosts.json`) | ✅ Data only; both ports and the `gpu-box` slug are facts about Mithlond, not about the framework |
| ComfyUI-side preflight | Project Tigress (both asset skills §0) | ✅ The runner only covers unattended dispatch; an interactive art session is the collision that was reachable today |
| nightshift note in `~/.claude/local-llm-stack.md` | machine-wide | Record the integration there, not here |
| OpenCode charter projection | nightshift (`update.py`) | Route B only |
| Hook shim plugin | nightshift | Route B only |

**The cross-repo rule applies** (`CLAUDE.md`): any change spanning both repos lands on paired
branches and merges framework-first. Most rows above span both.

---

## Amendments this document requires elsewhere

Listed rather than made, so they are made deliberately and by whoever does the work:

- **`00_architecture.md` §10** — ✅ done in the same change as this file: doc 04's row moved
  from ⬜ **next** to written. The rest of this list is not done.
- **`00_architecture.md` §16** — the block comment predicts that Phase 5 rebinds the *worker
  tier* to a local runtime. §4 argues it should not; that comment needs a sentence pointing
  here, or it will be followed.
- **`research_notes.md`** — the "Local coding/checker models" section's findings 1–5 are
  superseded by §1 above. Do not delete them: a superseded measurement with a date is how
  anyone knows the park was reasonable when it was made.
- **`SESSIONS.md` Session I** — the outcome block ends "researched and parked". It needs the
  reopening and a pointer here.
