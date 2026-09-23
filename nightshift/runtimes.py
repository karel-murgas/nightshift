"""Where a card executes — the `runtime` axis, and why it is not a tier.

`04_local_runtime.md` §4 settles the rule this module exists to hold:

> **`tier` says how much judgment. `runtime` says where it executes. They are
> orthogonal, and §16 governs only the first.**

The obvious implementation is a `local` row in §16's ```tier-binding block, and it
is refused there in as many words. A `local` tier would make "where it runs" and
"how much judgment it gets" the same field, and then every routing decision
silently re-answers both. So this is a **second axis**, resolved here, and
`tiers.py` is left with the one job §16's rule is about.

**Near `tiers`, deliberately not inside it.** §16's argument is that the
tier->model binding lives in exactly one document; a runtime table in the same
module is an invitation to write a second binding next to the first. The file
boundary is the whole point — see the card `local-runtime-axis`.

## Cloud is the ground state, and that is a structural claim

`runtime` has exactly two values. `cloud` is what happens when anything at all is
unresolvable, unavailable, disabled, misconfigured or unmeasured — **there is no
configuration in which a card fails to dispatch *because* the local runtime is
missing.** Every early return below is `CLOUD`, and none of them raises. That is
the opposite of `tiers.binding`, which refuses to guess a model and raises
`TierError` rather than dispatch on an assumption; the asymmetry is intended. A
guessed *model* runs silently and wrongly. A guessed *runtime* of `cloud` is
simply the behaviour every card had before this module existed.

§6 puts it as the test: *"Fallback is not a fallback if it is never exercised."*
So local is not the default with a cloud escape hatch — cloud is the default and
local is an **upgrade** applied only when every term holds. A bug in here produces
a cloud dispatch, which is the failure everyone wants.

## The conjunction

§5 states the routing rule. One term of it — the card's own `local:` field — is
deliberately out of scope for the card that built this module and is **not**
represented here; when it lands it becomes a fifth term, narrowing this further
and never widening it.

> local <=> the host declares a `local_model` AND the agent is in its allowlist
> AND the run has not disabled it AND no declared conflict is resident
> AND the endpoint answers.

Any term false -> cloud, silently and at no cost.

## The mutex, and why only one of its directions is free

Two consumers on one box, neither of which fits beside the other: llama-server
under `--load-mode mlock` holds ~13.8 GB of *pinned* pages, and ComfyUI holds ~12
GB of Windows commit charge for its whole lifetime. Pinned pages cannot be
reclaimed by another process, so the second one to start does not run slowly — it
runs out of commit charge, and on Windows that kills whichever process asks for
memory next rather than the one that took it.

The two directions look symmetric and are not:

- **ComfyUI resident -> do not go local** is the conjunction term above
  (`conflict_up`). Cloud is the ground state, so this direction is free by
  construction and cannot fail a dispatch.
- **The model resident -> an art card needs the RAM** cannot be answered that
  way, because the art cards are the reason the GPU box exists and there is
  nowhere else to send them. Something has to *stop*, which is `release_for`,
  which is the only thing in this module that does. Its rule is the one
  `stop_started_servers` already had: **stop what this run started, refuse what
  it did not.**

Both directions are configured from one `conflicts` entry per neighbour, because
they are one fact seen from either end.

## Declared versus probed, and the line was drawn before this module

`.ai/hosts.json`'s own comment sets the rule (`declarative-because-of-initiative`,
Karel 2026-07-23): *a check that bounds behaviour is declarative; a check that
discovers a fact may probe.*

- **"May this machine use a local model?"** *bounds* — so it is declared, as the
  presence of the `local_model` block and the contents of its `agents` allowlist.
  A box without the block never uses a local model, whatever is running on it.
  That is the laptop's correct answer and the safe default for any new clone.
- **"Is the server answering right now?"** *discovers* — so it is probed, at
  dispatch time, and a failed probe is a fallback rather than a refusal.

Both questions exist and neither answers the other. In particular the probe is
**not** an eligibility check: a reachable server on a box that has not declared
the block is still not used, because reachability was never the question being
asked.

## The allowlist is §7's enforcement point

`00_architecture.md` §7 requires a measured golden-set pass before a role runs on
a model, and automatic demotion on an accept-rate drop. `agents` is where that
lands: **a charter not named there never runs local, whatever a card says and
whatever the panel says.** A charter is added by a measured pass and removed by a
measured drop — never by argument, and never by a control in the UI. `permits`
below is the single gate every caller goes through, which is what makes that
sentence enforceable rather than a convention.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: The two values of the axis. Strings rather than an enum so a run record, a
#: verdict and a log line can carry the value without importing this module.
CLOUD = "cloud"
LOCAL = "local"

#: Seconds to wait for the reachability probe. Short on purpose: this runs on the
#: dispatch path, and the question is "is the server up", not "is it fast". A box
#: whose llama-server is loading a 21.7 GB model answers late, and the right
#: reading of "late" here is *not right now* — the card runs on cloud and the
#: night continues, which is strictly better than blocking the queue on a model
#: load.
PROBE_TIMEOUT = 2.0

#: The host-block key. Its **absence is the permanent, per-machine off switch**
#: (§6's first switch), and the reason it is absence rather than a boolean: a new
#: clone, a fresh box and a machine nobody has thought about all resolve to cloud
#: without anyone writing anything, which no `enabled: false` default achieves.
HOST_KEY = "local_model"

#: Default port for the shared OpenCode server, overridable per machine with
#: `local_model.server_port`.
#:
#: **4096 is OpenCode's own default**, which is the argument for it: a port the
#: tool already documents is one a person can guess, and `opencode serve` with no
#: flags lands on it.
#:
#: The neighbours on this loopback, so nobody picks a colliding number — they all
#: share `127.0.0.1` and only the port keeps them apart (Karel asked, 2026-09-19):
#: **8765** the Command Center (`panel.DEFAULT_PORT`), **8082** llama-server,
#: **8188** ComfyUI. A collision does not announce itself, which is why
#: `server_probe_url` identifies the server rather than trusting a 200.
SERVER_PORT = 4096

#: Seconds to wait for a freshly started OpenCode server to answer. Generous
#: because it is paid once per night, not once per card: `ensure_server` reuses a
#: server that is already up, so this is the cold-start cost and nothing else.
#: Measured at ~2 s on this box (41 skills, project-copy refresh, watcher start);
#: the margin is for a cold filesystem cache.
SERVER_START_TIMEOUT = 30.0


#: Minutes of no inference before a runner-started llama-server is reaped. Handed
#: to the declared `watchdog`; that script has its own, longer-than-ComfyUI's
#: default and the reason for it.
WATCHDOG_IDLE_MINUTES = 30

#: How long `release_for` waits for a stopped model's memory to come back, and
#: how often it looks. Commit charge is returned by the kernel as the process
#: tears down, not at the instant `taskkill` returns — and under `--load-mode
#: mlock` there are ~13 GB of pinned pages to unpin, which is not instant.
RELEASE_TIMEOUT = 60.0
RELEASE_POLL = 2.0


@dataclass(frozen=True)
class Conflict:
    """Another process on this box that cannot be resident beside the local model.

    **Declared per machine, never inferred**, for `hosts.json`'s own
    `declarative-because-of-initiative` reason: a probe answers "is ComfyUI
    installed?", and an agent with initiative answers "no" by installing it. This
    answers "what is this box not allowed to run at the same time", which is a
    decision already made.

    `port` identifies it, because a port has exactly one listener and an image
    name has as many as there are copies running (doc 04 §9e). `required_by` is
    the other direction of the same fact: the card capabilities whose work will
    *start* this process, so the runner knows a `requires: gpu-box` card is about
    to want the RAM Ornith is holding. Both halves of the mutex from one entry —
    they are the same conflict seen from either end, and splitting them into two
    config blocks is how they drift apart.

    The framework hardcodes neither the port nor the slug: 8188 and `gpu-box` are
    facts about Mithlond, and another project's box has neither.
    """

    name: str
    port: int
    required_by: tuple[str, ...] = ()
    #: GB of headroom this process needs to start. Optional, and what makes
    #: `release_for` able to *confirm* a release rather than assume one: a
    #: stopped process is not the same as reclaimed commit charge, and under
    #: `mlock` there is no graceful degradation to fall back on. Absent means the
    #: release is confirmed only as far as "the process is gone", which the log
    #: then says in as many words rather than implying more.
    needs_gb: float = 0.0


@dataclass(frozen=True)
class LocalModel:
    """One machine's declared local model. Frozen — it is configuration, not state.

    `agents` is a tuple for the same reason: an allowlist a caller could append to
    in passing is not an allowlist. §7 admits one way in, a measured pass, and
    that way is an edit to `.ai/hosts.json` that shows up in a diff.
    """

    base_url: str
    model: str
    agents: tuple[str, ...] = ()
    runtime: str = "opencode"
    launcher: str = ""
    context_limit: int = 0
    server_port: int = 0
    conflicts: tuple[Conflict, ...] = ()
    watchdog: str = ""
    watchdog_idle_minutes: int = WATCHDOG_IDLE_MINUTES

    @property
    def model_port(self) -> int:
        """The port llama-server listens on, read off `base_url`.

        Derived rather than declared: it is already in `base_url`, and a second
        field saying the same thing is a field that can disagree with the first.
        """
        return _port_of(self.base_url)

    def conflicts_for(self, requires: str) -> tuple[Conflict, ...]:
        """The declared conflicts a card with this `requires:` will provoke.

        Empty for every card on a machine that declares no conflicts, which is
        every machine but the one this was written for — so the boundary check
        costs nothing where it does not apply.
        """
        if not requires:
            return ()
        return tuple(c for c in self.conflicts if requires in c.required_by)

    @property
    def probe_url(self) -> str:
        """The OpenAI-compatible model listing, which llama-server serves.

        `/models` rather than a completion: the question is whether the server is
        answering, and a completion would spend real wall clock on a loaded GPU to
        learn something a 200 already said.
        """
        return f"{self.base_url.rstrip('/')}/models"

    @property
    def server_url(self) -> str:
        """The OpenCode server this machine's dispatches attach to.

        **Two servers, and they are not the same thing.** `base_url` is
        llama-server — the model. This is OpenCode's own HTTP server — the
        harness. A dispatch needs both up, and either being down is an
        independent reason to run the card on cloud.

        Bound to loopback by the server itself. OpenCode warns that a server
        without `OPENCODE_SERVER_PASSWORD` is unsecured, which is true and is why
        the address is never anything but `127.0.0.1`: the thing that makes it
        safe is that nothing off this machine can reach it.
        """
        return f"http://127.0.0.1:{self.server_port or SERVER_PORT}"

    @property
    def server_probe_url(self) -> str:
        """`/config`, which answers with OpenCode's own config JSON.

        Not `/`, which serves the web UI's HTML and would report success for any
        web server that happened to hold the port. `/config` carries
        `opencode.ai/config.json` as its `$schema`, so the probe can tell *this*
        server from a neighbour — which matters on a box where 8765 is the
        Command Center and 8188 is ComfyUI, and a port collision would otherwise
        present as a dispatch attaching to something that is not OpenCode.
        """
        return f"{self.server_url}/config"


def host_block(root: Path) -> dict:
    """This machine's raw `local_model` block, or `{}`.

    A lazy import of `runner`, matching `init.py`'s: **`runner` owns the hostname
    lookup and the untracked `.ai/host.json` override**, and a second reader of
    `hosts.json` here would be a second answer to "which machine is this" the day
    the override rules change. The import is deferred because `runner` imports
    this module.
    """
    from nightshift.hostconfig import host_setting

    found = host_setting(root, HOST_KEY, None)
    return found if isinstance(found, dict) else {}


def local_model(root: Path) -> LocalModel | None:
    """This machine's declared local model, or `None` if it declares none.

    `None` on **any** malformed block, not an exception. A host file with a typo
    in it is a machine that runs everything on cloud — which is what it did
    yesterday — rather than a machine whose night dies on a `KeyError` at the
    first dispatch. The block is hand-edited per machine and never validated by a
    gate, so this is the realistic failure and it must be the cheap one.
    """
    block = host_block(root)
    base_url = str(block.get("base_url", "") or "").strip()
    model = str(block.get("model", "") or "").strip()
    if not base_url or not model:
        # Both are load-bearing: OpenCode needs a provider URL and a model id, and
        # a block carrying one without the other cannot dispatch anything.
        return None
    agents = block.get("agents", [])
    if not isinstance(agents, list):
        return None
    try:
        context_limit = int(block.get("context_limit", 0) or 0)
    except (TypeError, ValueError):
        context_limit = 0
    try:
        server_port = int(block.get("server_port", 0) or 0)
    except (TypeError, ValueError):
        server_port = 0
    try:
        idle_minutes = int(block.get("watchdog_idle_minutes", 0)
                           or WATCHDOG_IDLE_MINUTES)
    except (TypeError, ValueError):
        idle_minutes = WATCHDOG_IDLE_MINUTES
    return LocalModel(
        base_url=base_url,
        server_port=server_port,
        model=model,
        agents=tuple(str(a) for a in agents),
        runtime=str(block.get("runtime", "opencode") or "opencode"),
        launcher=str(block.get("launcher", "") or ""),
        context_limit=context_limit,
        conflicts=_conflicts(block.get("conflicts", [])),
        watchdog=str(block.get("watchdog", "") or ""),
        watchdog_idle_minutes=idle_minutes,
    )


def _conflicts(declared: object) -> tuple[Conflict, ...]:
    """The `conflicts` list, parsed. A malformed entry is **dropped, not raised**.

    Same reasoning as `local_model`'s: this block is hand-edited per machine and
    no gate validates it, so a typo must cost the machine its mutex rather than
    its night. That is the one place the module's "every failure is cloud" rule
    does not straightforwardly apply — a dropped conflict makes local *more*
    available, not less — so it is stated rather than left to be noticed:

    **A conflict that fails to parse is a conflict that is not enforced.** The
    port is the required field and the reason: an entry without one names a
    process nothing can find, and a mutex that cannot identify its other half is
    worse than no mutex, because it reads in a log as though it were guarding
    something.
    """
    if not isinstance(declared, list):
        return ()
    out: list[Conflict] = []
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        try:
            port = int(entry.get("port", 0) or 0)
        except (TypeError, ValueError):
            continue
        if port <= 0:
            continue
        required_by = entry.get("required_by", [])
        if not isinstance(required_by, list):
            required_by = []
        try:
            needs_gb = float(entry.get("needs_gb", 0) or 0)
        except (TypeError, ValueError):
            needs_gb = 0.0
        out.append(Conflict(
            name=str(entry.get("name", "") or f"port {port}"),
            port=port,
            required_by=tuple(str(r) for r in required_by),
            needs_gb=max(0.0, needs_gb),
        ))
    return tuple(out)


def permits(local: LocalModel | None, agent: str) -> bool:
    """Whether `agent`'s charter is allowlisted for the local model.

    The one gate §7 is enforced at. An empty or absent allowlist permits nothing —
    **not everything** — which is the inversion that would quietly promote every
    charter on the box the day someone wrote `"agents": []` meaning "not yet".
    """
    if local is None or not agent:
        return False
    return agent in local.agents


def reachable(local: LocalModel | None, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether the declared endpoint is answering right now.

    The probed half of §4's declared/probed split. Every failure is `False` and
    none escapes: a connection refused, a DNS failure, a timeout, a 500, a body
    that is not JSON and a URL scheme `urllib` will not open all mean the same
    thing to the caller — *not right now, run it on cloud.* Catching broadly is
    correct here precisely because the consequence of being wrong is the ground
    state rather than a failure.
    """
    if local is None:
        return False
    try:
        with urllib.request.urlopen(local.probe_url, timeout=timeout) as answer:
            return 200 <= getattr(answer, "status", 0) < 300
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def port_listening(port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether anything at all holds `127.0.0.1:port` right now.

    A TCP connect rather than an HTTP request, because the question is "is that
    process resident", not "is it healthy". ComfyUI answers `/queue` only once it
    has finished loading its models — and it is holding the ~12 GB the whole time
    it loads, which is exactly the window a health check would call "not up" and
    dispatch a local card into.

    Every failure is `False`, which here means *no conflict detected* — so a
    broken probe makes local more available rather than less. That is the
    inversion `_conflicts` flags, and it is why this is a connect that can only
    fail by the port genuinely refusing.
    """
    if port <= 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (OSError, ValueError, TimeoutError):
        return False


def conflict_up(local: LocalModel | None) -> Conflict | None:
    """The first declared conflict that is resident right now, or `None`.

    The cheap half of the mutex, and the one the note that asked for this called
    "free by construction": ComfyUI being up makes the local runtime unavailable,
    and unavailable resolves to `CLOUD`, which is what every card did before this
    module existed. It cannot fail a dispatch — there is no configuration in
    which this term turns a card into a failure rather than a cloud run.

    Both directions of the mutex live in `Conflict`, but only this one is
    symmetric-and-free. The other (`release_for`) has to *stop* something.
    """
    if local is None:
        return None
    for conflict in local.conflicts:
        if port_listening(conflict.port):
            return conflict
    return None


def resolve(root: Path, agent: str, *, enabled: bool = True,
            probe: bool = True) -> str:
    """`CLOUD` or `LOCAL` for one dispatch. Never raises.

    `enabled` is the two *revocable* off switches folded into one argument — the
    runner's `--no-local` for this run and the Command Center toggle for this
    sitting (§6). They are one parameter here and two controls at the surface
    because this module has no opinion about which of them said no; both mean
    "not this time", and either alone is sufficient. The third switch, the absent
    host block, is not a parameter at all — it is the `local_model(root)` lookup
    returning `None`, which is what makes it permanent rather than something a
    caller can pass its way past.

    **The panel's asymmetry is structural, not a second check.** §6 allows the
    toggle to always turn local *off* and to turn it *on* only for a charter
    already in the host allowlist. That falls out of the order below rather than
    being enforced separately: `enabled=False` short-circuits to cloud, while
    `enabled=True` still has to clear `permits`. So a toggle switched on for a
    charter §7 has not admitted cannot produce `LOCAL` — there is no code path
    from the panel to a non-allowlisted charter, which is a stronger guarantee
    than a rule saying there should not be.

    `probe=False` answers the *declared* question alone — "would this be eligible
    if the server were up" — for the panel's row chips, which render many rows per
    page load and must not open a socket per row. The conflict check is part of
    the probed half for the same reason: whether ComfyUI happens to be up is a
    fact about this instant, not about what the machine is configured to do, and
    a row chip that flickered with it would be reporting the wrong question.
    """
    if not enabled:
        return CLOUD
    local = local_model(root)
    if not permits(local, agent):
        return CLOUD
    if probe and conflict_up(local) is not None:
        return CLOUD
    if probe and not reachable(local):
        return CLOUD
    return LOCAL


# --------------------------------------------------------------------------
# Driving the local runtime
# --------------------------------------------------------------------------
#
# Route B (`04_local_runtime.md` §9b, settled by Karel 2026-09-18): the local
# runtime is **OpenCode**, not the `claude` CLI pointed at a local endpoint. §9c
# measured why — the startup floor drops from 42,133 tokens to 14,018, the verdict
# parses, and a session that fills its window compacts and continues instead of
# dying on a 400 the CLI cannot degrade past.
#
# Everything below was read off a real run rather than the config schema, which is
# the lesson §9c Result 4 paid for: the flag table was right about the flags and
# silent about every integration fact that actually broke a dispatch.
#
# **Where the endpoint is configured, and why it is written twice.** OpenCode
# reaches the model through a provider block in the project's own
# `.opencode/opencode.json` (an npm transport plus a `baseURL`), which nightshift
# does not write and should not. `base_url` in the host block is therefore *not*
# how the model is reached — it is what `reachable()` probes. The two must name
# the same server, and nothing checks that they do. Declared here rather than
# derived because the alternative is nightshift parsing another tool's config to
# find out where to send a health check, which couples this module to a schema
# OpenCode owns and may change.

#: The Route B binary, by name. Never passed to `subprocess` directly — see
#: `binary()`, which is the only correct way to spell it on Windows.
OPENCODE = "opencode"


def binary() -> str | None:
    """The absolute path to the OpenCode CLI, or `None` if it is not installed.

    **`shutil.which` rather than the bare name, and that is load-bearing on
    Windows.** npm installs `opencode.CMD`, and `subprocess` does not apply
    PATHEXT — `subprocess.run(["opencode", ...])` raises `FileNotFoundError`
    (WinError 2) on a box where `opencode` works perfectly from any shell.
    Measured here on 2026-09-19, and it is the same reason `startup.claude_binary`
    exists rather than every call site spelling out `claude`.

    The consequence of getting it wrong is the one this module is built to avoid
    and would have hidden well: `available_agents` swallows the exception, returns
    an empty set, `dispatchable` reads that as "cannot confirm" and every card
    quietly runs on cloud. Correct behaviour, right up until someone asks why the
    local model is never used.

    `OPENCODE_BIN` overrides, matching `CLAUDE_BIN`'s shape, for a box where the
    CLI is installed somewhere PATH does not reach.
    """
    if override := os.environ.get("OPENCODE_BIN"):
        return override if Path(override).is_file() else None
    return shutil.which(OPENCODE)

#: `opencode agent list` prints `<name> (primary)` or `<name> (subagent)`, one per
#: agent, with each agent's permission JSON indented after it.
_AGENT_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*) \((primary|subagent)\)\s*$",
                         re.MULTILINE)

#: Seconds for the agent listing. Generous next to `PROBE_TIMEOUT` because this
#: starts a Node process rather than opening a socket, and stingy next to a
#: dispatch because it must not become a reason the night stalls.
LIST_TIMEOUT = 60


def available_agents(cwd: Path, timeout: int = LIST_TIMEOUT) -> frozenset[str]:
    """The charters OpenCode can actually dispatch in `cwd`, as it reports them.

    **This exists because a wrong `--agent` name fails silently** (§9c Result 4,
    item 3). OpenCode runs the request under its default `build` agent instead of
    erroring; the only tell is a banner, and under `--format json` there is no
    banner at all. A runner reading stdout cannot see which charter actually ran,
    so a projection that was never copied, a `mode: subagent` frontmatter (which
    hangs indefinitely rather than erroring, item 2) and a typo all present as a
    dispatch that worked and produced a worthless verdict.

    So the name is asserted against the runtime's own listing before it is
    trusted. An empty set on any failure — a missing binary, a timeout, a
    non-zero exit — which the caller reads as "cannot confirm", and cannot
    confirm means cloud.
    """
    exe = binary()
    if exe is None:
        return frozenset()
    try:
        out = subprocess.run([exe, "agent", "list"], cwd=cwd,
                             capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if out.returncode != 0:
        return frozenset()
    return frozenset(m.group(1) for m in _AGENT_LINE.finditer(out.stdout or ""))


def dispatchable(local: LocalModel | None, agent: str, cwd: Path) -> bool:
    """Whether `agent` is both allowlisted *and* one OpenCode can really run here.

    The declared half and the discovered half of the same question, in the order
    §4 puts them: `permits` bounds (it is the §7 enforcement point and no listing
    can widen it), `available_agents` discovers. A charter OpenCode does not know
    about is a cloud dispatch, never a `build`-agent dispatch wearing its name.
    """
    return permits(local, agent) and agent in available_agents(cwd)


def server_reachable(local: LocalModel | None, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether *OpenCode's* server is answering, and is really OpenCode.

    The body is checked, not just the status, for the reason `server_probe_url`
    gives: a 200 from a neighbour holding the port would otherwise read as
    success and every dispatch would attach to the wrong thing.
    """
    if local is None:
        return False
    try:
        with urllib.request.urlopen(local.server_probe_url, timeout=timeout) as answer:
            if not 200 <= getattr(answer, "status", 0) < 300:
                return False
            body = answer.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    return "opencode.ai/config" in body


def ensure_server(local: LocalModel | None, cwd: Path,
                  timeout: float = SERVER_START_TIMEOUT) -> bool:
    """Make sure an OpenCode server is up for `cwd`, starting one if it is not.

    **Why the runner attaches instead of letting each dispatch boot its own.**
    Measured 2026-09-19: `opencode run` spawned directly by Python's
    `CreateProcess` dies ~1.4 s in with `UnknownError: Unexpected server error`
    and nothing in its own log — same binary, argv, cwd, environment and stdin
    that succeed when a POSIX shell `exec`s it. The failure is in the *per-run
    server boot*; `--attach` skips that boot, and a direct spawn then works. The
    alternative fix was to launch through Git for Windows' `sh.exe`, which works
    too and makes a POSIX shell an undeclared dependency of this package on every
    machine (Karel chose attach, 2026-09-19).

    Started **detached and left running**, then reused for the rest of the night:
    the cold start is ~2 s and paying it once beats paying it per card. The server
    holds no model — llama-server does — so an idle one is a cheap Node process,
    not 21.7 GB. Its lifecycle past the night belongs with the other local-server
    watchdogs (`E:\\AI\\scripts\\`, doc 04 §12), not here.

    Returns whether a server is answering when this comes back. `False` is not an
    error and never raises: it is one more false term in the conjunction, and the
    card runs on cloud.
    """
    if local is None:
        return False
    if server_reachable(local):
        return True
    exe = binary()
    if exe is None:
        return False
    try:
        # **No `DETACHED_PROCESS`, and that is the whole finding.** The obvious
        # spelling for a server the runner wants to outlive one dispatch is
        # `DETACHED_PROCESS | CREATE_NO_WINDOW`, and it produces a server that
        # *boots* — it answers `/config`, so every health check passes — and then
        # resets the connection on the first real message (`ECONNRESET` on
        # `/session/<id>/message`). Measured 2026-09-19, both spellings, same
        # everything else:
        #
        #     DETACHED_PROCESS | CREATE_NO_WINDOW   booted=True  dispatch=ERROR
        #     no flags (console inherited)          booted=True  dispatch=OK
        #
        # OpenCode needs the console it was started with. A health check cannot
        # see the difference, which is what makes the detached form dangerous
        # rather than merely wrong: it looks up and fails every card.
        #
        # The cost of not detaching is that this server is a child of whatever
        # started it and shares its console. It still outlives a single dispatch,
        # which is all the reuse needs. Its lifecycle past the night belongs with
        # the other local-server watchdogs (doc 04 §12), not here — and it holds
        # no model, so an idle one is a cheap Node process, not 21.7 GB.
        # gate-ok(subprocess_result_checked): this starts a long-lived server, so
        # there is no return code to read — a server that exits is a server that
        # failed, and one that works never returns. The check the gate asks for is
        # the `server_reachable` poll below, which is a stronger statement than a
        # zero exit anyway: it confirms the thing is *serving*, not merely that it
        # has not died yet. Holding the handle to poll it would add a second,
        # weaker liveness signal that can disagree with the first.
        subprocess.Popen(
            [exe, "serve", "--port", str(local.server_port or SERVER_PORT)],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_reachable(local):
            return True
        time.sleep(0.5)
    return False


#: Seconds to wait for llama-server to load its model and answer.
#:
#: Far longer than `SERVER_START_TIMEOUT`, and for a reason that is not caution:
#: OpenCode's server starts a Node process, while this one reads ~13 GB off disk
#: and pins it (`--load-mode mlock`). Paid once per night, never per card.
MODEL_START_TIMEOUT = 300.0

#: Ports this process started a server on, and therefore may stop.
#:
#: **The whole point is what is *not* in here.** A llama-server the maintainer
#: started by hand — because they were using it, or testing it, or it has been up
#: since breakfast — is not the runner's to kill at 4 AM. Recording the ones we
#: started makes "only stop what you started" structural rather than a rule each
#: call site has to remember, and the set is empty in every process that never
#: started anything, which is every process but a live run.
_STARTED_PORTS: set[int] = set()


def _pid_on_port(port: int) -> int:
    """The PID listening on `port`, or 0.

    **By port, never by image name.** Doc 04 §9e: every wrapper written for the
    local-runtime trials killed llama-server by image name, so two overlapping
    runs killed each other's server and the failure surfaced in the *innocent*
    one as `Cannot connect to API` about 1 s in — three separate "bugs" in one
    day were this. A port is owned by exactly one listener; an image name is
    owned by everyone running that program.
    """
    try:
        if os.name == "nt":
            # gate-ok(prompt_not_in_argv): `-p tcp` is netstat's *protocol*
            # selector, not the Claude CLI's prompt flag. No agent is being
            # invoked here and there is no prompt to put anywhere.
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, timeout=20,
                                 encoding="utf-8", errors="replace")
            for line in (out.stdout or "").splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[1].rsplit(":", 1)[-1] == str(port)
                        and parts[3].upper() == "LISTENING"):
                    return int(parts[4])
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=20,
                                 encoding="utf-8", errors="replace")
            first = (out.stdout or "").split()
            if first:
                return int(first[0])
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0
    return 0


def ensure_model_server(local: LocalModel | None,
                        timeout: float = MODEL_START_TIMEOUT) -> bool:
    """Make sure llama-server is up, starting it from the declared `launcher`.

    The third and largest of the things a local dispatch needs — the model
    itself. Without this the whole axis is inert unless someone remembered to
    start Ornith by hand, and a night where they forgot runs silently on cloud
    (Karel, 2026-09-19: *"How is the runner supposed to work with Ornith, if it
    doesn't start llama.cpp server?"*).

    A server that is **already up is left alone and not recorded**, so the
    end-of-run teardown cannot touch it. That asymmetry is deliberate: the
    maintainer's own llama-server, started for their own reasons, must survive a
    night run that merely used it.

    `launcher` is spawned rather than waited on — it is a blocking script that
    runs the server in the foreground — and the readiness signal is the probe,
    not the process. Every failure returns `False`, which is one more false term
    in the conjunction and a cloud dispatch.
    """
    if local is None:
        return False
    if reachable(local):
        return True
    if not local.launcher or not Path(local.launcher).is_file():
        return False
    try:
        # gate-ok(subprocess_result_checked): a launcher that blocks for the
        # server's whole lifetime has no return code to wait for — `reachable`
        # below is the readiness check, and it tests the thing we actually need
        # (the model answering) rather than the script not having failed yet.
        #
        # No detach flags, for the reason `ensure_server` documents at length.
        subprocess.Popen([local.launcher], cwd=str(Path(local.launcher).parent),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, shell=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reachable(local):
            port = _port_of(local.base_url)
            _STARTED_PORTS.add(port)
            start_watchdog(local, port)
            return True
        time.sleep(2.0)
    return False


def start_watchdog(local: LocalModel, port: int) -> bool:
    """Start the declared idle watchdog for a server **this process just started**.

    `comfy_server_teardown` enforces the same bargain for ComfyUI by reading the
    instruction docs: a document that launches the server must also start the
    watchdog, because the pair was split once and nothing noticed for a month.
    Here the launcher is not a document — it is this function — so the bargain is
    kept by construction instead, and there is nothing for a gate to read. That is
    the better version of the same guarantee, and it is why no gate was written
    for it (`.ai/CLAUDE.md`: a gate is earned by an observed failure).

    **Only ever for a server we started**, which is the caller's invariant and not
    one this function can check — `ensure_model_server` calls it on exactly the
    path that adds the port to `_STARTED_PORTS`. A hand-started llama-server gets
    no watchdog, because a script that reaps the maintainer's own session after
    half an hour of them thinking is a worse failure than the one being prevented
    (Karel, 2026-09-19).

    `--pid` binds it to this listener, so a server restarted on the same port
    later tonight is not reaped by its predecessor's watchdog. Failure to start it
    is logged nowhere and returns `False`: the server is up and the card can run,
    and refusing a working dispatch because its janitor did not start would trade
    a real capability for a tidiness the end-of-run teardown already provides.
    """
    if not local.watchdog or not Path(local.watchdog).is_file():
        return False
    pid = _pid_on_port(port)
    if pid <= 0:
        return False
    log_path = Path(local.launcher).parent / "llama_idle_watchdog.log" \
        if local.launcher else Path(local.watchdog).with_suffix(".log")
    try:
        # The watchdog's own log is the "a human can read it the morning after"
        # half of this — a file, not a console nobody was watching at 4 AM. Same
        # shape as the ComfyUI watchdog's `-RedirectStandardOutput`.
        # Binary append: this handle is never written to from here, only handed
        # to the child as its stdout, so text mode would buy newline translation
        # and an encoding for a stream this process does not touch. The watchdog
        # flushes its own lines.
        handle = open(log_path, "ab")  # noqa: SIM115
    except OSError:
        handle = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        # gate-ok(subprocess_result_checked): a watchdog that returns has already
        # done its job or given up, and either way this call is over by then.
        # There is no readiness signal to poll and nothing downstream depends on
        # it having started, which is the whole reason its failure is tolerated.
        subprocess.Popen(
            [sys.executable, local.watchdog,
             "--port", str(port),
             "--pid", str(pid),
             "--idle-minutes", str(local.watchdog_idle_minutes)],
            stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return True


def _port_of(url: str) -> int:
    """The port in `url`, or 0. Used to record what we started."""
    try:
        return int(url.rstrip("/").rsplit(":", 1)[-1].split("/")[0])
    except (ValueError, IndexError):
        return 0


def stop_started_servers() -> list[int]:
    """Stop every server *this process* started, and say which ports it stopped.

    Called once at the end of a run. Bounding the lifetime to the run is what
    keeps a 13.8 GB pinned, non-reclaimable model from outliving the night —
    which is the shape of `asset-generation-processes-dont-shut-down`, where two
    orphaned ComfyUI servers OOM-killed two dispatches days after the run that
    left them.

    Servers the maintainer started are never in `_STARTED_PORTS` and so are never
    touched. Failures are swallowed: a teardown that raises would turn a finished
    night into a failed one, and the worst case is a process someone can close.
    """
    stopped = [port for port in sorted(_STARTED_PORTS) if _stop_port(port)]
    _STARTED_PORTS.clear()
    return stopped


def _stop_port(port: int) -> bool:
    """Stop whatever is listening on `port`. True only if this call did it.

    **The one kill in this module**, shared by the end-of-run teardown and the
    mid-night release so they cannot drift into two answers. Neither caller may
    reach it for a port outside `_STARTED_PORTS`; that check belongs to them
    because they have different things to say about refusing.
    """
    pid = _pid_on_port(port)
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # /T so the launcher's child dies with it: `run_ornith.bat` is a
            # cmd wrapper, and killing only the wrapper orphans the server.
            done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                  capture_output=True, timeout=30, check=False)
            # A non-zero code here means the process was already gone — which
            # is the outcome we wanted — but it is *not* a port this call
            # stopped, and reporting it as one would put a line in the run log
            # claiming an action nobody took.
            return done.returncode == 0
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def release_for(root: Path, requires: str,
                log: Callable[[str], None] | None = None) -> bool:
    """Free the box for a card that `requires:` something the local model blocks.

    The hard half of the mutex, and the only place in this module that *stops*
    anything mid-night. `conflict_up` is free because its answer is "run this card
    on cloud"; there is no equivalent here — the art cards are the reason the GPU
    box exists, so "run it somewhere else" is not an option and something has to
    give up the RAM.

    Returns whether the card may now be dispatched. **False is never a failure**:
    the caller logs it and moves to the next card, which stays in `tasks/` and is
    picked up by the next run, exactly as a card whose capability this host does
    not declare already behaves.

    ## Stop what you started; refuse what you did not

    `_STARTED_PORTS` is the whole rule, and it is the same one the end-of-run
    teardown uses — this extends it from the end of the night to a boundary inside
    it, rather than introducing a new kind of initiative. A llama-server the
    maintainer started, because they are *using* it, is not the runner's to kill
    at 4 AM; `hosts.json`'s `declarative-because-of-initiative` comment is about
    exactly this class of thing, and an unattended process taking a machine-wide
    action on a human's running work is the version of it that actually costs
    something. So the runner skips the card and says why (Karel, 2026-09-19).

    ## A stopped process is not reclaimed memory

    Under `--load-mode mlock` the model's ~13 GB are *pinned*, which the stack doc
    puts as trading graceful degradation for a predictable ceiling: they cannot be
    paged out for whoever needs them next, so a ComfyUI that starts too early does
    not run slowly, it runs out of commit charge. And `mlock` may silently not
    have been taken at all — Windows `VirtualLock` needs a privilege it can fall
    back from without logging anything. Both are reasons the confirmation below
    reads *measured headroom* (`suite.available_memory_gb`, the scarcer of free
    physical memory and commit headroom) rather than the footprint the model was
    supposed to have.
    """
    say = log or (lambda _msg: None)
    local = local_model(root)
    if local is None:
        return True
    blocked = local.conflicts_for(requires)
    if not blocked:
        return True
    port = local.model_port
    if port <= 0 or not port_listening(port):
        return True

    names = ", ".join(c.name for c in blocked)
    if port not in _STARTED_PORTS:
        say(f"    {local.model} is resident on port {port} and this run did not "
            f"start it, so it is not this run's to stop — a card needing {names} "
            f"cannot have the memory tonight. Stop it by hand and re-run, or let "
            f"its idle watchdog reap it.")
        return False

    say(f"    stopping {local.model} on port {port} — the next card needs {names}")
    if not _stop_port(port):
        say(f"    port {port} could not be stopped; skipping this card rather than "
            f"dispatching it into a box that has no room for it")
        return False
    _STARTED_PORTS.discard(port)
    return _await_headroom(port, max((c.needs_gb for c in blocked), default=0.0),
                           names, say)


def _await_headroom(port: int, needs_gb: float, names: str,
                    say: Callable[[str], None]) -> bool:
    """Wait for a stopped server's memory to actually come back.

    Two questions, and the second is the one the note that asked for this cared
    about: has the process gone, and is the headroom it held available to whoever
    needs it next. The first alone would be the assumption `mlock` makes wrong.

    `needs_gb` of 0 means nothing declared how much room it wants, so this
    confirms only the process. That is a weaker guarantee and the log says so
    instead of implying the stronger one.
    """
    from nightshift import suite

    deadline = time.monotonic() + RELEASE_TIMEOUT
    # Look before checking the clock, both times below. A budget that has already
    # run out is not a reason to ignore a port that is *already* free — and on a
    # box fast enough to tear the process down inside one poll, the clock-first
    # spelling reports a failure that did not happen.
    while True:
        if not port_listening(port):
            break
        if time.monotonic() >= deadline:
            say(f"    port {port} is still held {RELEASE_TIMEOUT:.0f}s after "
                f"being stopped; skipping this card")
            return False
        time.sleep(RELEASE_POLL)

    if needs_gb <= 0:
        say(f"    port {port} is free. Nothing declares how much memory {names} "
            f"needs, so this is confirmed only as far as the process being gone")
        return True

    while True:
        free = suite.available_memory_gb()
        if free is None:
            say(f"    port {port} is free, but this platform reports no memory "
                f"figure, so the headroom {names} needs ({needs_gb:.1f} GB) is "
                f"unconfirmed — dispatching anyway, which is what happened before "
                f"this check existed")
            return True
        if free >= needs_gb:
            say(f"    {free:.1f} GB available, {names} needs {needs_gb:.1f} GB — "
                f"released")
            return True
        if time.monotonic() >= deadline:
            say(f"    {free:.1f} GB available {RELEASE_TIMEOUT:.0f}s after "
                f"stopping the model, and {names} needs {needs_gb:.1f} GB. "
                f"Something else on this box is holding it; skipping this card "
                f"rather than OOM-killing the dispatch.")
            return False
        time.sleep(RELEASE_POLL)


def worker_argv(local: LocalModel, agent: str, session: str = "",
                cwd: Path | None = None) -> list[str]:
    """The argv for one local worker round. The prompt goes on **stdin**.

    Verified against `opencode` 1.18.31 rather than assumed:

    * `run` takes its message as positionals *or* on stdin, and stdin is what
      lets the runner hand over a whole card body without meeting Windows'
      ~32k command-line limit. `_run_worker` already writes the prompt to the
      child's stdin for the cloud path, so this needed nothing new.
    * `--format json` emits newline-delimited events on stdout and leaves stderr
      empty, which is the same shape `_run_worker`'s tee already expects.
    * `--session <id>` continues a session, which is `--resume`'s counterpart for
      the warm-resume path.
    * `--model` is passed explicitly so the host block is the one place the model
      tag is named for dispatch. The projected charters carry a `model:` of their
      own and this overrides it — deliberately, so promoting a machine to a new
      quantisation is an edit to `.ai/hosts.json` and not to four charters.

    There is no `--add-dir`: the worker's writable roots are the worktree it runs
    in plus whatever `.opencode/opencode.json` allows, and the verdict lands
    outside the worktree only because that config sets `external_directory:
    allow` (§9c Result 4, item 4 — in a worktree `.git` is a file, so OpenCode
    classes the worktree's own contents as external).
    """
    exe = binary() or OPENCODE
    return [
        exe, "run",
        "--agent", agent,
        "--model", local.model,
        "--format", "json",
        "--attach", local.server_url,
        *(["--dir", str(cwd.resolve())] if cwd is not None else []),
        *(["--session", session] if session else []),
    ]


def stream_facts(stdout: str) -> dict:
    """`worker-N.json`-shaped facts out of an OpenCode event stream.

    Normalised to the keys the runner already reads off a Claude Code run, so
    `_session_id` and the cost accounting keep working without learning a second
    vocabulary — `sessionID` becomes `session_id`, and the `step_finish` event's
    `tokens`/`cost` become `total_cost_usd` and `usage`.

    **`cost` is genuinely zero on a local model and that is not a parse failure.**
    A local dispatch spends wall clock, not tokens, which is the whole argument
    for the runtime axis; a run record showing `0.0` for one is telling the truth.

    Scans the whole stream rather than the last line: OpenCode's terminal event is
    `step_finish`, there may be several (one per step), and the last one carries
    only that step's input count — so the totals are taken from the richest
    `step_finish` seen rather than the final one.
    """
    facts: dict = {}
    best_total = -1
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if session := event.get("sessionID"):
            facts["session_id"] = str(session)
        part = event.get("part")
        if event.get("type") != "step_finish" or not isinstance(part, dict):
            continue
        tokens = part.get("tokens")
        if isinstance(tokens, dict) and int(tokens.get("total", 0) or 0) > best_total:
            best_total = int(tokens.get("total", 0) or 0)
            facts["usage"] = tokens
        try:
            facts["total_cost_usd"] = facts.get("total_cost_usd", 0.0) + \
                float(part.get("cost", 0.0) or 0.0)
        except (TypeError, ValueError):
            facts.setdefault("total_cost_usd", 0.0)
    return facts


def prepare(root: Path, agent: str, tree: Path, *, enabled: bool = True,
            log: "Callable[[str], None] | None" = None) -> tuple[str, LocalModel | None]:
    """Resolve the runtime for one dispatch and bring up what it needs.

    **The one entry point every dispatcher uses**, so `runner` and `chores` cannot
    drift into two different answers to "does this card go local" (Karel,
    2026-09-19: *"Make sure it works for both runner and chore dispatcher."*).
    The chore batch is not an edge case here — `chore-thread` is one of the two
    allowlisted charters, so it is the path most likely to take the local branch.

    Eight terms, checked in cost order — declared facts first, because a laptop
    with no host block must reach `CLOUD` without opening a socket or starting
    anything:

    1. the run has not disabled it (`--no-local`, or the panel toggle)
    2. the host declares a `local_model`
    3. the agent is in its allowlist (§7)
    4. no declared conflict is resident — ComfyUI being up means this box has no
       room for a 13.8 GB pinned model, and the card runs on cloud
    5. llama-server is up, or the declared `launcher` can bring it up
    6. OpenCode knows this charter (a wrong `--agent` runs the default agent
       *silently*, so this is asserted rather than assumed)
    7. OpenCode's own server is up, or can be started
    8. …and only then, `LOCAL`

    **Term 4 is before term 5 on purpose.** Both orders give the same answer, and
    only this one gives it without first spending five minutes reading 13 GB off
    disk into a box that has no room for it.

    Returns `(CLOUD, None)` for any false term, and **never raises**. The caller
    passes the pair straight to `run_producer`; a `None` model is what makes the
    cloud path structurally unreachable from a half-resolved local one.
    """
    say = log or (lambda _msg: None)
    if not enabled:
        return CLOUD, None
    local = local_model(root)
    if not permits(local, agent):
        return CLOUD, None
    assert local is not None  # `permits` is False for None
    if (blocker := conflict_up(local)) is not None:
        say(f"    local: {blocker.name} is resident on port {blocker.port} and "
            f"cannot share this box with {local.model} — running on cloud")
        return CLOUD, None
    if not ensure_model_server(local):
        say(f"    local: {local.model} is not answering at {local.base_url} and "
            f"{'no launcher is declared' if not local.launcher else 'the launcher did not bring it up'}"
            f" — running on cloud")
        return CLOUD, None
    if not dispatchable(local, agent, tree):
        say(f"    local: OpenCode cannot dispatch `{agent}` in {tree.name} — "
            f"running on cloud")
        return CLOUD, None
    if not ensure_server(local, root):
        say(f"    local: no OpenCode server answered at {local.server_url} and one "
            f"could not be started — running on cloud")
        return CLOUD, None
    say(f"    local: {agent} on {local.model} via {local.server_url}")
    return LOCAL, local


def describe(root: Path) -> str:
    """One line for the panel tooltip and the run log, naming the endpoint and the
    allowlisted charters. `""` when this box declares nothing — a caller renders
    nothing rather than the words "no local model", which would imply a setting
    someone forgot rather than the ordinary state of every machine."""
    local = local_model(root)
    if local is None:
        return ""
    agents = ", ".join(local.agents) or "no agents allowlisted"
    return f"{local.runtime} -> {local.model} at {local.base_url} ({agents})"


if __name__ == "__main__":
    import sys

    from nightshift.manifest import find_root

    root = find_root()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    found = local_model(root)
    if found is None:
        print("no local model declared for this machine — everything runs on cloud")
        raise SystemExit(0)
    print(describe(root))
    print(f"reachable: {'yes' if reachable(found) else 'no (cards resolve to cloud)'}")
    for agent in found.agents:
        print(f"  {agent:16} -> {resolve(root, agent)}")
