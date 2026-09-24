---
doc_scope: history
---

# Research notes — local models & harness (gathered 2026-07-21)

Recorded so this is not re-researched. Facts age; re-verify anything load-bearing
before acting on it, but do not start from zero.

## Hardware target

Karel's box: Ryzen 5 5600X / RTX 3060 12 GB / 32 GB DDR4 / NVMe. **Already runs ComfyUI.**

| Class | Example | Speed on this box | Notes |
|---|---|---|---|
| 30B MoE coder, Q4_K_M, CPU offload | Qwen3-Coder-30B-A3B (~3.3B active of 30.5B, 128 experts) | **~12–15 tok/s** | RAM bandwidth is the bottleneck, not the GPU |
| dense 7–9B, fully in VRAM | Qwen3-8B class | **~35–40 tok/s** | fits with room for context |

## Tool-calling reliability (the number that drives the architecture)

- **90%+** well-formed calls on simple single-call workloads.
- **80–90% end-to-end** on multi-step real workflows (selection + argument errors compound).
- **Q4_K_M is the production floor.** Q3/Q2 degrade tool-calling *before* they degrade
  chat quality — so a model that still "sounds fine" may already be broken as an agent.
- **&lt;7B: do not bother.** Low-to-zero tool invocation, confabulated answers in place of
  tool use, catastrophic failure on multi-step chains.
- Models reported as reliably tool-calling (May 2026): Gemma 4 27B, GLM-4.7 32B,
  Qwen3 32B, **Qwen3-Coder 30B** (best for code-shaped tool work), Llama 3.3 70B.
- Published benchmarks do **not** predict behaviour on a specific codebase. Validate on a
  golden set of real tasks from this repo. This is stated explicitly by multiple sources.

## Harness compatibility — the keystone

**Since Jan 2026, llama.cpp / Ollama / LM Studio natively serve the Anthropic Messages
API.** No translation proxy needed (this was the enabling change; pre-2026 guides all
describe proxies — ignore them).

Point Claude Code at a local endpoint with:

```
ANTHROPIC_BASE_URL=http://<box>:11434
ANTHROPIC_API_KEY=<any non-empty string>
ANTHROPIC_AUTH_TOKEN=<any non-empty string>
ANTHROPIC_DEFAULT_SONNET_MODEL=<local model tag>
ANTHROPIC_DEFAULT_HAIKU_MODEL=<local model tag>
ANTHROPIC_DEFAULT_OPUS_MODEL=<local model tag>
```

LM Studio added `/v1/messages` in 0.4.1. llama.cpp has had it longer. Run ≥32K context.

**Consequence:** a worker defined as a Claude Code subagent + skill runs unchanged on
either cloud Claude or the local box. Fallback is a config toggle, not a second code path.
This is what makes "must work without local models" free.

## Subagent definition facts

- `.claude/agents/<name>.md`, YAML frontmatter + body-as-system-prompt.
- Scope: project (`.claude/agents/`) or user (`~/.claude/agents/`).
- Only `name` and `description` are required.
- `tools:` restricts the tool set — use it to bound blast radius.
- `model:` overrides per agent. `CLAUDE_CODE_SUBAGENT_MODEL` sets the default.
- **Not confirmed:** a per-agent *custom endpoint* field. Endpoint appears to be
  process-level (env vars), so local-vs-cloud is likely a per-*run* choice, not
  per-agent-within-a-run. **Verify before designing routing that assumes otherwise.**

## Orchestration patterns others converged on

- **Git worktree per agent/task** is the consensus isolation primitive.
- Headless Claude Code: capture `session_id` from JSON output, `--resume <id>` to continue.
- Overnight loops: cron/scheduler + task decomposition + **deterministic verification**.
  Everyone who reports success has a machine verifier; everyone who reports piles of
  garbage does not.

## Sources

- <https://www.promptquorum.com/power-local-llm/best-local-models-tool-calling-2026>
- <https://braindetox.kr/en/posts/local_llm_agentic_coding_2026.html>
- <https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/>
- <https://www.runlocalai.co/guides/claude-code-with-local-models>
- <https://ollama.com/blog/claude>
- <https://renezander.com/guides/claude-code-local-llm-anthropic-base-url/>
- <https://unsloth.ai/docs/models/tutorials/qwen3-coder-how-to-run-locally>
- <https://apxml.com/models/qwen3-30b-a3b>
- <https://amux.io/guides/claude-code-headless/>
- <https://code.claude.com/docs/en/sub-agents>
- <https://www.augmentcode.com/tools/open-source-agent-orchestrators>

---

# Audio generation research (gathered 2026-07-29)

For Session J / doc 11. **Research only — no install, no pipeline, no card was executed.**
Same warning as above: facts age, re-verify anything load-bearing. Every license claim below
was checked against the model card on Hugging Face, not only the project's README, because
the first strong candidate failed exactly there (see "The licensing screen").

## The brief

Karel, 2026-07-29, answering the three pickers on the audio-generation-pipeline card:

1. **Licensing bar: Apache-2.0 / MIT only**, same as the image stack. This is a screen
   applied *before* quality, not a tiebreaker.
2. **Narrator: two targets.** Primary = a model covering cs + es (+ en). Secondary = the best
   English-only model as a fallback. **A different voice per language is acceptable.**
3. **Integration: best tool per job.** Unify under ComfyUI where that is genuinely the best
   tool; separate installs are fine where it is not.

## Machine (re-measured 2026-07-29, not inherited from the note above)

Hostname **MITHLOND** — RTX 3060 12 GB (driver 610.62), 32 GB RAM, E: 1.35 TB free.
Matches the box described in the hardware section above. Relevant new fact: every audio
model shortlisted here is 0.6B–4B, i.e. **far smaller than the coder models** the GPU-lock
discussion was sized around. Audio and image models contend, but audio is the light tenant.

<!-- stale-ok: this section is a dated snapshot ("gathered 2026-07-21") of the repo's
     state at research time, not a live description. `sound_events.py` and its 10-case
     enum were real then; the module was deleted as dead code by
     [[sound-event-enum-dead]] on 2026-08-01. Narrating what the repo looked like before
     that deletion is the whole point of the section — it explains why SFX needed the
     file-loading path built afterward. -->
## Starting state in this repo

- Music: five ~5 MB MP3s in the music assets folder, hardcoded by filename in
  `music_manager.py`. Two of them (menu, hacking) are wired ad hoc.
- SFX: **there are no SFX files at all.** `audio_manager.py` synthesises every sound from
  numpy waveforms at startup ("No audio files required"), keyed off a flat 10-case enum in
  `sound_events.py`.
- **Consequence, and it is the one most likely to be underestimated:** "regenerate the ingame
  sounds" is not an asset swap. It replaces a procedural synthesiser with a file-loading
  path that does not exist yet. Music is asset-only; SFX is asset + code.

## The licensing screen — and the two candidates it killed

Both were the obvious pick before the licence was checked. Both are cited all over the 2026
write-ups as free-for-commercial-use, and both are not.

| Candidate | Why it looked right | Why it is out |
|---|---|---|
| **OmniVoice** (k2-fsa, Mar 2026) | 0.6B, 600+ languages incl. cs/es, zero-shot cloning, ~3 GB VRAM in fp16, RTF 0.025, has a ComfyUI node, *and its GitHub says Apache-2.0* | **Weights are CC-BY-NC.** Code is Apache-2.0; the model card states the pre-trained model is CC-BY-NC because of its training data (Emilia). Split licence — the repo badge is about the code only. Several secondary sources report it as "Apache 2.0, free for commercial use". They are wrong. |
| **XTTS-v2** (Coqui) | The long-standing answer for Czech cross-lingual cloning, 17 languages | Weights are **CPML**, non-commercial. |

That is the whole value of asking the licensing question first: the best-on-paper multilingual
TTS of 2026 is disqualified, and it is disqualified by a line on the model card that neither
the repo badge nor most blog coverage reflects.

**The OmniVoice contradiction, resolved 2026-07-29 — do not re-research this.** Karel asked
how omnivoice.app can sell the model with unconditional commercial rights if the weights are
NC. Answer: **that site is not the k2-fsa project.** Its own Terms say "OmniVoice is an
independent service and is not affiliated with, endorsed by, or sponsored by any third-party
AI model providers", it names no legal entity, and it sells $9.90–$49.90 credit tiers. k2-fsa
is Xiaomi's Next-generation Kaldi team and ships on GitHub/HF/arXiv, not a credits SaaS.

The mechanism that spreads the error is worth remembering, because it will recur:

- The GitHub repo has an Apache-2.0 licence file covering the **code**, and **no License
  section in its README at all** — so GitHub renders an "Apache-2.0" badge that is technically
  correct and practically misleading.
- Blogs, X posts and resellers copy the badge; only the HF model card states the weights are
  CC-BY-NC 4.0.
- **Therefore: a repo badge is not a weights licence.** Check the model card frontmatter or an
  explicit weights-licence sentence. Every licence in this shortlist was verified that way.

Residual nuance, for the record and not as a recommendation: CC-BY-NC binds the weights and
derivatives of them, and whether *generated audio* is a derivative work of model weights is
legally unsettled. A future retrain on clean data could also be permissive — the NC here is
data-derived (Emilia), not ideological. Neither changes the answer under the stated bar.

## Shortlist (all licence-verified on the model card)

### Music — ACE-Step 1.5

- **MIT**, weights at `ACE-Step/Ace-Step1.5` on HF, confirmed `license: mit` in frontmatter.
- Hybrid LM-planner + diffusion-renderer. 48 kHz **stereo**, 10 s to 10 minutes, 50+ languages
  for lyrics, instrumental when no lyrics are supplied.
- Variants: DiT base / sft / turbo / turbo-rl, plus LM planners at 0.6B / 1.7B / 4B. The
  turbo path runs in well under 6 GB; the 4B XL path needs offload at 12 GB.
- Reported ~10 s for a full 4-minute song on an RTX 3090 — so a 3060 is in the
  tens-of-seconds range per track, not minutes. Cheap enough to iterate hard.
- **Natively supported in ComfyUI since 2026-02-03**, single all-in-one checkpoint or split
  model/text-encoder/VAE. Cover and Repaint are *not* in the native nodes; a community node
  (ComfyUI-AceMusic) implements the full feature set including Extend and Retake.
- Also supports LoRA fine-tuning from ~8 songs (~1 h on a 3090) — the direct analogue of the
  pixel-art LoRA, and the obvious route to one consistent soundtrack identity.

Runner-up: **HeartMuLa-oss-3B** (Jan 2026) — repo and weights relicensed to Apache-2.0,
LM-only autoregressive over codec tokens, ~15.8 GB download, RTF ≈ 1.0, ComfyUI node exists.
Quality is reported in the same band as ACE-Step 1.5. It is the second opinion to score
against, not the default: bigger, slower, and its stated strength is lyric controllability,
which is the axis a mostly-instrumental cyberpunk soundtrack cares least about.

### SFX — MOSS-SoundEffect v2.0

- **Apache-2.0** (family-wide, weights included), 1.3B DiT with flow matching, released
  2026-05-26.
- **48 kHz**, up to 30 s, ~3 GB VRAM. Covers environmental / urban / biological / musical
  fragments.
- 48 kHz is above the 44.1 kHz most tools emit, and above the 44.1 kHz the game's mixer is
  initialised at — so downsampling is a pipeline step, not an option.
- ComfyUI support via the TTS-Audio-Suite custom node (dedicated 48 kHz engine, crossfades,
  deterministic seeds, caching).

There is no strong Apache/MIT alternative here. Stable Audio Open, Tango 2 and AudioLDM 2 —
the names most SFX articles lead with — all fail the licence screen. This is a single-candidate
category, which is worth knowing before the eval harness is designed around comparing two.

### Narrator, primary (cs + es + en) — MOSS-TTS family

Same family as the SFX model, which is the main reason to prefer it: one install, one licence,
one set of conventions covering two of the three use cases.

- **Apache-2.0 on the HF model card**, verified in the frontmatter — no non-commercial clause.
- Languages: **Czech and Spanish are both explicitly supported** (~20–31 depending on variant),
  alongside en/zh/de/fr/it/pl/pt/ru/etc.
- Relevant variants for a 12 GB box:
  - MOSS-TTS-Local-Transformer-v1.5 — 4B, ~8 GB VRAM. The realistic main workhorse.
  - MOSS-TTS-v1.5 — 8B, ~16 GB. **Does not fit**; only via GGUF/staged loading.
  - **MOSS-VoiceGenerator — 1.7B, ~4 GB: designs a speaker timbre from a free-form text
    description, with no reference audio.** This is the direct answer to "the narrator is a
    synthetic female tigress voice" — the voice can be *written* rather than cast or cloned
    from a real person, which also sidesteps every consent/likeness question in a shipped game.
  - MOSS-TTS-Nano — 0.1B, CPU-capable. Irrelevant for quality, useful as a smoke test.
- Zero-shot voice cloning from a short reference is supported, so the intended chain is:
  design the voice once → keep that clip as the canonical reference → clone it into cs and es.
  Karel has already allowed a per-language voice, so this chain is an optimisation, not a
  requirement.
- **The gap that matters: there is no published per-language quality data.** The evaluation
  tables are English and Mandarin only. Czech is listed as supported and is otherwise
  unmeasured. Treat Czech narrator quality as **unknown until heard**, not as a spec claim.

### Narrator, fallback / second opinion — Chatterbox Multilingual v3

- **MIT**, 0.5B Llama backbone, Resemble AI. 25 languages in v3 — **Czech is in the list**
  (it is *not* in the v2 23-language list, which is why several sources contradict each other
  on this; v3 added it).
- First open TTS with emotion-exaggeration control; benchmarks favourably against ElevenLabs
  in the vendor's own blind tests (discount accordingly).
- **Two caveats, both load-bearing:**
  1. **PerTh watermarking is embedded by default on every self-hosted output, in every
     language.** MIT licence, so this is not a legal blocker — but it means shipped voice
     assets carry an inaudible watermark unless it is explicitly disabled. That is a decision
     to make deliberately, not to discover after 3,000 lines are generated.
  2. The v3 write-up openly states Korean and Vietnamese are **not** production quality
     (>70% error rate). The long tail is uneven — which is the strongest available evidence
     that Czech must be *tested*, not assumed, on either model.
- Also in TTS-Audio-Suite, so trying it costs no separate install.

For a **best-English-only** fallback the same suite carries VibeVoice, Higgs Audio 2/3,
IndexTTS-2, F5-TTS and Qwen3-TTS. Not researched per-model here: the English-only slot is only
reached if both multilingual candidates fail on Czech, and picking it is a listening test, not
a licence question. Screen those licences individually if that branch is taken.

## Integration verdict — ComfyUI wins on the merits, not by default

Karel's answer was "best tool per job, unify if it happens to be ComfyUI". It happens to be.

- **Music:** ACE-Step 1.5 is *native* in ComfyUI core. Nothing to install beyond checkpoints.
- **SFX + TTS:** both MOSS models are covered by one custom node suite (TTS-Audio-Suite,
  19 engines including MOSS-TTS, MOSS-SoundEffect v2 and Chatterbox), which also means the
  narrator fallback candidates are one dropdown away rather than one install away.
- The existing ComfyUI already has ComfyUI-GGUF installed, which the ACE-Step GGUF builds use.
- Consequence: the same "start the server, POST a workflow, parse the result" client shape as
  the image stack. A machine-wide audio client next to the existing image client is plausible
  — but that is pipeline design, deliberately not decided here.

**The known trap:** this suite is large and pulls many engines' dependencies into one venv.
The image stack has already been broken once by a custom node (the emoji/cp1252 logging hang
recorded in the machine-wide image-stack note). Standing this up in the *same* ComfyUI install
that the art pipeline depends on is a real risk to a working system — a second ComfyUI instance
on a different port is worth costing out before choosing convenience.

## What ComfyUI does not solve

The image stack needed a post-processor (unfake) because diffusion output is not pixel art.
Audio has the exact same gap, and it is bigger:

- **Seamless looping.** A generated track is not game music until it loops without a seam.
  The state of the art is still: cut at zero crossings, beat-match the loop point, crossfade
  the tail into the head. No generator does this for you. ACE-Step's Extend/Retake (community
  node only) helps produce loop-friendly material; it does not produce a loop point.
- **Loudness normalisation.** Generated tracks arrive at wildly different levels. EBU R128 /
  ffmpeg loudnorm is the standard answer, and the repo already has an `audio_volume` gate to
  extend rather than duplicate.
- **Format and rate conversion.** 48 kHz stereo out of the models vs 44.1 kHz in the game's
  mixer, plus mp3/ogg encoding for ship.
- **Objective checks worth knowing about, because they are candidate gates:**
  - **CLAP score** — cosine similarity between text and audio embeddings; the standard
    automatic "does this clip match its prompt" metric. Caveat, and it is a serious one:
    CLAPScore is reported to correlate *poorly* with human judgement (an entire 2026 challenge,
    XACLE, exists to fix that). Usable as a cheap reject filter, not as an acceptance test.
  - **FAD** (Fréchet Audio Distance) for perceptual quality against a reference set.
  - **Whisper round-trip** for the narrator — transcribe the generated line and diff it against
    the script. That is a genuinely deterministic gate for TTS, and it directly answers the
    `03_board.md` §5 question of whether audio can be machine-checked the way art cannot.
    It catches the dominant TTS failure (wrong/garbled words), and says nothing about
    performance quality.

## Honest gaps

- **SFX has one licensed candidate**, so "score 3–4 candidates" is not achievable in that
  category on the current licence bar.
- **No golden set exists for audio**, and unlike code cards there is no corpus to build one
  from — the game currently ships five music tracks and zero SFX files.

## Installed and measured 2026-07-29 — see the machine-wide stack doc

Karel authorised installation the same day. The stack is installed on MITHLOND and
documented at **~/.claude/audiogen-stack.md** (machine-wide, the companion to the existing
image-generation stack note in the same directory) — that file is authoritative for what
exists, how to run it and the Windows traps. Headlines that change the plan above:

- **Music works and is cheap.** ACE-Step 1.5 turbo is native in ComfyUI 0.28.0; 30 s tracks
  in 12–16 s, 48 kHz stereo. Two real defects to design around: `duration` is a *canvas*, not
  a contract (measured 15–28 s of actual content inside a 30 s request, seed-dependent), and
  every track came back clipped at 0.0 dBFS.
- **Fill percentage is not musical quality — a measured metric picked the wrong winner.** A
  180 s request filled 94 % against a 45 s request's 67 %, so I recommended long requests.
  Karel's ear disagreed: the 45 s take is the better combat track, the long one has the wrong
  beat, and its intro is dead weight for a loop. Third instance in this session of an
  objective metric measuring the wrong thing (with WER-vs-accent and CLAP). Use these numbers
  to *reject* failures, never to *select* winners.
- **The Czech accent was fixed by Karel's carrier-phrase idea: WER 0.25 → 0.00.** Prepend a
  throwaway Czech sentence so the model settles out of English phonetics, then cut it at the
  silence gap. The counter-intuitive part: the apparently stronger fix — bootstrap a Czech
  reference and do Czech→Czech continuation — is *worse*, because a reference teaches
  pronunciation as well as timbre and so propagates its own errors verbatim.
- **The "one designed voice cloned into all three languages" chain does not survive Czech.**
  MOSS's cross-lingual clone into cs scored **WER 0.50**; MOSS *direct* cs scored 0.08 but
  takes no reference, so it cannot be voice-matched. OmniVoice holds one identity across
  en/cs/es at cs **WER 0.17** — but is CC-BY-NC and cannot ship.
- **Chatterbox v3 cannot do Czech**, despite the vendor page claiming it. The shipped model
  rejects `cs` and lists the 23 v2 languages. Second instance of vendor-page-vs-artifact drift
  in this same shortlist.
- **The Whisper round-trip is a screening gate, not an acceptance gate** (Karel, 2026-07-29,
  correcting an earlier overstatement here). It reliably catches *gross* failure — hallucinated
  speech, truncation, wrong language, empty file — and that is what exposed the Czech garble.
  It is not evidence a line is pronounced correctly: Whisper is partly a language model and
  will snap a slurred word onto the expected one, flattering the score, and it is weakest on
  Czech, which is precisely where the TTS is weakest too — so judge and subject fail together
  rather than independently. Use it to rank clips for the ear; a human decides acceptance.
  Same shape as the CLAP caveat for music: cheap reject filter, never an acceptance test.
- **SFX works after a one-line fix.** MOSS-SoundEffect v2's ~11.2 GB of weights all sit on
  the GPU at once, which thrashes a 12 GB card — a 2 s clip did not finish in 12 minutes. The
  staging machinery is already written and already wired at every call site; it is just
  switched off (its VRAM-management flag defaults to false). Enabling it gives
  **>12 min → 35–40 s per effect** and 0.00 GB held between generations. Three Project Tigress
  SFX generated and verified.
- **The obvious follow-up optimisation is a trap.** The pipeline denoises a fixed 30 s latent
  and crops; shrinking that canvas to the requested duration produces flat noise, because the
  fixed size is a trained invariant. Measured and reverted — recorded so it is not retried.
- **Generated SFX are not onset-aligned** — measured onsets pistol 0.99 s, reload 0.20 s,
  footstep 0.08 s inside a 2 s window. Trimming is a required post-step, alongside the
  normalisation every generator here needs; both are now in `scripts/audio_postprocess.py`.
- **"One family, one licence" did not mean one environment**: MOSS-SoundEffect v2 and
  MOSS-TTS pin mutually exclusive `transformers`/`numpy` versions and need separate venvs.

## Sources (audio)

- <https://github.com/ace-step/ACE-Step-1.5>
- <https://huggingface.co/ACE-Step/Ace-Step1.5>
- <https://docs.comfy.org/tutorials/audio/ace-step/ace-step-v1-5>
- <https://comfyui.org/en/ace-step-15-is-now-available-in-comfyui>
- <https://github.com/HeartMuLa/heartlib>
- <https://github.com/OpenMOSS/MOSS-TTS>
- <https://huggingface.co/OpenMOSS-Team/MOSS-TTS/blob/main/README.md>
- <https://github.com/OpenMOSS/MOSS-TTS/blob/main/moss_soundeffect_v2/README.md>
- <https://studio.aifilms.ai/blog/moss-soundeffect-v2-open-source-sound-design>
- <https://github.com/diodiogod/TTS-Audio-Suite>
- <https://github.com/k2-fsa/OmniVoice>
- <https://huggingface.co/k2-fsa/OmniVoice>
- <https://www.resemble.ai/resources/chatterbox-multilingual-v3-tts-with-embedded-watermarking-for-25-languages>
- <https://www.resemble.ai/learn/models/chatterbox-multilingual>
- <https://www.promptquorum.com/power-local-llm/local-tts-voice-cloning-piper-coqui-xtts>
- <https://www.siliconflow.com/articles/en/best-open-source-models-for-sound-design>
- <https://www.it-jim.com/blog/best-open-source-ai-music-generator/>
- <https://www.summerengine.com/blog/ai-game-music-generator>
- <https://arxiv.org/html/2601.02900> (XACLE / CLAPScore correlation)

---

# Local coding/checker models — Session I research (gathered 2026-07-30)

**Session I was researched, costed, and then parked by Karel the same day.** Nothing was
installed; no endpoint was stood up; doc 04 was not written. The verdict and the reasoning are
below so the next session starts from a decision rather than from zero. Same warning as every
section here: facts age, re-verify anything load-bearing.

> **Karel's ruling, 2026-07-30:** *"Too much work for too little effect. Creating assets or
> story is much better spend of resources right now."* The two roles worth doing when it
> reopens are carded in the ideas lane (local-model-stale-hunter, local-model-translator).

## The measurement that decides it — and it was already on disk

Session I's runbook lists two blockers ("no desktop row in the host config", "no paid dispatch
has ever been run"). **Both were already cleared** and nobody had noticed: the desktop row
landed with Session J, and four cards had gone through the real CLI spawn, leaving full cost
and token telemetry in the per-run directories the runner writes.

Extracted from those four runs (worker tier, cloud Sonnet):

| Card | Cost | Turns | Unique context | Cumulative prefix | Output |
|---|---|---|---|---|---|
| fullscreen-after-os-maximize-noop | $0.84 | 41 | 40k | 1.4M | 12,598 |
| heavy-guard-should-bleed | $1.87 | 63 | 90k | 3.3M | 22,394 |
| bleed-regen-delayed-tick | $3.70 | 87 | 144k | 7.1M | 46,587 |
| stair-remnants-removal | $9.00 | 229 | 214k | 21.4M | 74,629 |
| *(reviewer, on card 3)* | *$0.69* | *4* | *53k* | *0.1M* | *5,950* |

Four cards = **$15.41/night**, mean $3.85. Five findings follow, in order of how much they
constrain the design:

1. **Cost is cache-reads, not output.** Spend tracks cumulative prefix almost linearly.
   Uncached input was 82 tokens on a $3.70 card. This is the single most load-bearing fact
   here, because **prompt caching is the thing local inference does not have**: cloud bills a
   re-read prefix at 0.1x, local must *recompute* it unless the server's slot cache holds.
2. **Context varies 5x across four cards** — 40k to 214k. So "can a local model run a code
   card" has no single answer; it is a per-card question against a VRAM budget.
3. **Checking costs ~16% of implementing** ($0.69 against $3.70). That answers Karel's
   redundancy test — the checker layer is not redundant, but its ceiling is modest. The
   argument for it is that stale-hunting and exemption review *currently do not happen at
   all*, not that they save money.
4. **Cloud's effective end-to-end throughput is only 14-33 tok/s**, because most wall time is
   tool execution and prefill, not generation. A 30B-A3B on a 3060 generates at ~12-15 tok/s,
   so local worker dispatch is plausibly 2-5x slower wall-clock, not 10x. Overnight that is
   survivable — the blocker is VRAM, not speed.
5. **The 12 GB box cannot hold the median card.** 144k of context plus a 30B-A3B does not fit.
   And the 7.1M-token cumulative prefix means that if slot caching fails, a single card costs
   2.5-6.6 hours of pure prefill at realistic 3060 rates.

## TurboQuant — researched on request, and the answer is no

KV-cache compression (3-bit keys / 2-bit values, Walsh-Hadamard rotation plus a Lloyd-Max
codebook), Google Research, ICLR 2026. It buys long context, not model quality.

**It is not in upstream llama.cpp and shows no sign of getting there.** Checked against the
repository rather than the write-ups: roughly fifteen TurboQuant pull requests, **every one
closed, none merged** — including the CPU KV-cache types (#21089, closed 2026-06-03), the
4.57x-compression PR (#21131), the CUDA integration (#21186) and one literally titled "Merge
turboquant" (#23962). Feature request #20977 is still open with no maintainer commitment;
discussion #20969 has 571 comments. One PR was closed as an AI-policy violation; others report
Vulkan garbage output and ROCm crashes.

Community forks exist and some carry real CUDA benchmarks on the A3B class, but they are
unmerged forks of a fast-moving project.

**Verdict: do not build on it.** The existing 8-bit KV cache type flags cover most of what it
would have bought. Revisit only if #20977 merges.

## Harness — the keystone claim survives, with a caveat that kills it for checkers

Section 3's claim is that a worker is defined once and the runtime is a config toggle.
Mechanically that still holds as of 2026-07:

- The llama.cpp HTTP server natively serves the Anthropic Messages API, **including the
  token-counting endpoint** — which closes the most-cited failure mode from the 2026 write-ups.
  Ollama added it Jan 2026, LM Studio in 0.4.1. Tool calling needs the Jinja flag.
- Pointing Claude Code at a base URL with per-tier model overrides is an officially documented
  LLM-gateway path, not a hack.

**But the framework overhead makes it the wrong harness for a small local model.** Claude Code
sends roughly **27,000-33,000 tokens before the user's prompt** — 110+ conditionally assembled
instructions plus 27 built-in tool descriptions, re-transmitted every turn. OpenCode sends
about 7,000. There is an open Anthropic issue tracking it (anthropics/claude-code#46526).

On cloud that is nearly free: it is a stable prefix, so caching charges 0.1x. **Locally there
is no cache-read discount, only prefill time.** For a checker whose real payload is 10-25k
tokens, the framework would be larger than the job.

Three further Claude Code behaviours to design around if this reopens:

- **Ghost Haiku calls.** Internal housekeeping routes to a Haiku model regardless of base URL —
  *confirmed in this repo's own telemetry*, not just reported: every run's per-model usage
  block lists a Haiku entry alongside the worker model. The Haiku tier override must point at
  a served local tag or those calls 404.
- **Prefix-cache defeat.** Claude Code injects a per-request hash into the system prompt, which
  changes the prefix every request and destroys prefix caching. Disabling the attribution
  header is not a tuning flag here — given finding 1 above, it is the difference between
  viable and not.
- **Concurrent request flooding** segfaults a single-slot server; run the server with parallel
  slots.

**Do not migrate the whole system to OpenCode.** The 4.7x overhead is an argument against
Claude Code *for local checkers*, not an argument for abandoning a working system: Phases 0-4
are Claude-Code-shaped in ways that are not cosmetic — the tier and preflight guards are
PreToolUse hooks (section 16 calls a hook the only mechanism Claude cannot forget), and the
runner spawns workers by agent name. Migrating is a rewrite of the machinery. Anthropic also
blocked OpenCode from Claude OAuth in Jan 2026, so it would mean moving to API-key billing too.

**The shape that does work:** a thin client (~80 lines) posting to the local Messages endpoint,
with grammar / JSON-schema constrained decoding so a malformed verdict is *structurally
impossible*. That is a bigger reliability lever than any model choice, and it has no cloud
equivalent. It is **not** a second ruleset: the ruleset is the charter *body*, which is
harness-agnostic prose; the frontmatter is a ~20-line adapter.

## Model shortlist (2026-07-30) — Qwen3-Coder-30B-A3B is no longer the pick

The 2026-07-21 section above is superseded on models. Licence-wise nothing here is a blocker
the way it was for audio — a coding model produces no shipped artefact — but verify at install.

| Model | Shape | Fit on 12 GB | Notes |
|---|---|---|---|
| **GLM-4.7-Flash** | 30B-A3B MoE | ~12-14 GB at 4-bit dynamic quant, needs MoE CPU offload | SWE-bench 59.2, **tau2-Bench 79.5, Terminal-Bench 2.0 64.0**. Built for local agentic use, 200K ctx, native tool calling. The tau2/Terminal numbers measure *harness adherence*, which is the bar that matters here |
| **Qwen3.6-35B-A3B** | 35B-A3B MoE | ~21 GB, heavy offload | **SWE-bench 69.2** (better patches) but **Terminal-Bench 40.5** (24 pts worse at driving a harness). The second candidate to score, not the default |
| **Qwen3.6-27B** | dense 27B | ~17 GB — does not fit | Dense, so offload is far slower than MoE. Effectively out |
| **Gemma 4 12B** | dense 12B | ~8-9 GB, fully resident | Weaker coding, but reliable output formatting; ~35-40 tok/s. The candidate for the **checker** tier |

The GLM/Qwen split maps onto the two tiers cleanly. Section 7 still applies: these are a
shortlist, not a decision — published benchmarks do not predict behaviour on this repo.

## Hardware, if the coder tier is ever wanted

Sized against the measured context above, not against benchmarks. Model is a 30B-A3B at 4-bit
(~17-18 GB fully resident); KV cache at 8-bit is roughly 30-100 KB/token depending on attention
config — **estimates to verify, not specs**.

| Tier | VRAM | ~Cost | Buys |
|---|---|---|---|
| RTX 3090 / 4090 | 24 GB | $700-1,600 | Model resident, ~60-80 tok/s. Context caps ~64-96k → **the 40k-class card only**, 1 of the 4 measured |
| RTX 5090 | 32 GB | $2,200-2,800 | ~144k context → 3 of 4. Blackwell prefill is the real gain, and prefill dominates this workload |
| 2x RTX 3090 | 48 GB | $1,800-2,300 all-in | 200k+ → **all four cards**. Cheapest full coverage. Needs a 1,000-1,200 W PSU, two x8 slots, real airflow |
| 2x 5090 / RTX 6000 Blackwell | 64-96 GB | $5,000-9,000 | 70B dense or an 80B coder MoE at 4-bit — the tier that approaches Sonnet-class |

**Payback with its assumptions stated:** $15.41/night x 5 nights is about $4,000/year, ~84% of
it worker-tier. A 2x 3090 build pays back in roughly 7-9 months *if* four cards actually land
per night, five nights a week.

**The assumption most likely to break it:** GLM-4.7-Flash scores SWE-bench 59.2 against
Sonnet-class ~70+. A lower first-pass rate means more retries, the runner spends attempts on
retries, fewer cards land, and the savings shrink with them. **That gap is what the A/B has to
measure and what no amount of hardware fixes.**

**One benefit that is not in the payback math:** a second GPU dissolves the section 6 GPU-lock
problem. Today ComfyUI and any coder model contend for the same 12 GB, so the architecture
requires them to interleave and never overlap. Two cards means the 3060 keeps the generative
stack and the new card runs the LLM — art, audio and code run *concurrently*. That is a
scheduling constraint the runner design has been working around, gone.

## Which maintenance work could ever go local — section 14's own finding is the filter

Karel asked whether the unsexy-but-needed maintenance jobs are the right target. Section 14
already answers it: Session H chartered **one** of four maintenance roles and refused three,
all for one reason — *maintenance work is defined by comparing an artefact against the whole
context it came from*, which is the exact opposite of section 16's second seam. That is also
precisely the property that makes a job runnable on a small local model. So the filter is
already written.

**Passes** (bounded input, no codebase briefing, schema output):

1. **Translation / i18n naturalness — the strongest candidate, stronger than stale-hunter.**
   Section 16's own table lists translation as context-disjoint. ~1,351 keys across cs and es,
   2-5k tokens per call, per-key schema output, and Karel can judge Czech himself. Carded as
   local-model-translator.
2. **stale-hunter** — chartered, one doc per call, and Session D left a known-answer fixture
   (5 findings, 5 true, 0 false positives). Needs size-based routing: the orientation files are
   2.5-30 KB, but the two largest companions are 129 KB and 229 KB. Carded as
   local-model-stale-hunter.
3. **Reference-exemption review** — Session D left 52 written exemptions and nothing
   re-examines them. "Is this reason still true?" is one exemption plus current source. Same
   shape as stale-hunter. Not carded; noted here.
4. **art-reviewer** — passes the filter cleanly (image plus criteria, never the prompt) but
   needs a vision model, i.e. a separate stack. Park it.

**Fails, already ruled, do not re-litigate:** cards from the corrections log (Karel,
2026-07-26, doc 12 sections 4-5 — *"there is nothing for a worker to do that a count and an
archive-move script do not"*); rule-scout, gate-smith and recipe-keeper (Session H).

## Why doc 04 was not written

Same reasoning Session J applied to doc 11. Doc 04 is the spec for a system with hardware in
it, and no hardware was installed — so it would be a spec for a thing that does not exist,
which is the fourth-copy problem the doc-leanness feedback warns about. The content has homes
that are already right: this section for what was measured and why, and the two idea cards for
the work itself. Write doc 04 when something is actually being stood up.

## Sources (local models and harness, 2026-07-30)

- <https://github.com/ggml-org/llama.cpp/issues/20977> (TurboQuant feature request, open)
- <https://github.com/ggml-org/llama.cpp/discussions/20969> (TurboQuant discussion, 571 comments)
- <https://research.google/blog/turboquant-redefining-ai-efficiency-with-extreme-compression/>
- <https://huggingface.co/blog/ggml-org/anthropic-messages-api-in-llamacpp>
- <https://github.com/anthropics/claude-code/issues/46526> (system-prompt overhead)
- <https://www.developersdigest.tech/blog/claude-code-token-overhead-opencode-comparison>
- <https://systima.ai/blog/claude-code-vs-opencode-token-overhead>
- <https://code.claude.com/docs/en/third-party-integrations> (base-URL gateway config)
- <https://deepwiki.com/ggml-org/llama.cpp/8.1-grammar-and-structured-output> (grammars)
- <https://unsloth.ai/docs/models/tutorials/glm-4.7-flash.md>
- <https://dev.to/czmilo/qwen36-35b-a3b-complete-review-alibabas-open-source-coding-model-that-beats-frontier-giants-4382>
- <https://insiderllm.com/guides/best-local-coding-models-2026/>
- <https://www.promptquorum.com/local-llms> (VRAM-tier guide)
