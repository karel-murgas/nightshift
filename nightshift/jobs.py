"""What the Command Center started in the background, and how it went.

**Earned by an observed silence, not by being tidy.** On 2026-08-17 the panel's
"Classify all" button was pressed and appeared to do nothing. It had in fact done
everything: `ingest` classified thirteen notes and rewrote `Routing.md` four
minutes later. Nothing said so, because the panel's `spawn_background` sent the
command's `stdout` and `stderr` to `DEVNULL` and kept no record that it had ever
run. Every line `ingest` prints — the meters, `classifying with sonnet ...`, the
per-route counts, and, on the refusal path, `REFUSED before classifying` — went
nowhere. A verb that succeeded and a verb that died on its first line looked
identical from the browser: a toast, and then nothing.

So this module owns the one fact the panel could not otherwise state: **a process
was started, here is its output, and here is how it ended.** Three properties are
what make that fact trustworthy:

* **A record exists before the process does.** `record()` writes the JSON, then
  the caller spawns. A wrapper that never starts still leaves a row saying what
  was meant to run, rather than nothing at all.
* **The exit code is recorded by something that outlives the panel.** The spawned
  process is `python -m nightshift.jobs --run <id>`, which runs the real command
  as its child and writes `finished` and `exit_code` back in a `finally`. Closing
  the panel must not kill a night (`panel.spawn_background`'s own rule), so the
  panel cannot be the thing that waits — and a status derived from the pid alone
  is the trap `panel.run_is_live` documents at length: pids are recycled.
* **The log is the command's own output, unedited.** Nothing here parses it,
  summarises it or decides what it meant. `run_command` already treats a command's
  stdout as the answer; this is the same contract for the commands that take
  minutes instead of seconds.

**Everything lives under `.ai/runs/`**, next to the run records and the day logs,
because `init` already ignores that directory in the projects it installs into: a
job log is machine-local noise, and a panel that dirtied the working tree every
time a button was pressed would block the very dispatches it exists to start.

No LLM, no board, no git. Writing a JSON file, starting one subprocess, and
reading the file back.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nightshift import textio
# Same import, same reason as `panel`: "is this pid still alive" carries a
# Windows-specific subtlety (`tasklist`, not a signal) that `runner` already got
# right, and a second copy would be the same question answered twice.
from nightshift.runner import EXIT_STOPPED, _pid_alive

#: Under `.ai/runs/` on purpose — see the module docstring. The leading underscore
#: keeps it out of the way of the per-card attempt directories that are its
#: siblings, matching `_preflight`/`_merge-check`.
JOBS_DIR = Path(".ai") / "runs" / "_panel"

#: How long a record with no finish written is still believed to be running.
#: The wrapper writes `finished` in a `finally`, so the only way past this is a
#: hard kill — a machine that went to sleep, a terminated console. Past it, the
#: honest answer is "nobody knows how that ended", not "still going": a row that
#: claims a dispatch has been running for three days is the same class of lie
#: `run_is_live` exists to prevent.
TRUSTED_FOR = dt.timedelta(hours=12)

#: How many records are kept. Each is a few hundred bytes plus its log, and the
#: panel only ever renders a handful — but a directory nothing prunes is a
#: directory that eventually holds a year of them.
KEEP = 80

RUNNING, DONE, FAILED, LOST, STOPPED = "running", "done", "failed", "lost", "stopped"


@dataclass
class Job:
    """One background command the panel started.

    `pid` is the *wrapper's* pid, written by the wrapper itself rather than by the
    panel that spawned it. The panel could only report what `Popen` handed back,
    and writing that after the fact races a job fast enough to finish first —
    `ingest` refused for money in under a second, which is precisely the case
    this module was written to make visible.
    """

    ident: str
    label: str
    argv: list[str] = field(default_factory=list)
    started: str = ""
    pid: int = 0
    finished: str = ""
    exit_code: int | None = None

    @property
    def started_at(self) -> dt.datetime | None:
        return _parse(self.started)

    @property
    def finished_at(self) -> dt.datetime | None:
        return _parse(self.finished)

    @property
    def command(self) -> str:
        """The command as a person would have typed it, for the log's header and
        the job's own page. `sys.executable` is an absolute path nobody needs to
        read, so it renders as `python`."""
        parts = list(self.argv)
        if parts and parts[0] == sys.executable:
            parts[0] = "python"
        return " ".join(parts)


def state(job: Job, *, now: dt.datetime | None = None) -> str:
    """`running`, `done`, `stopped`, `failed` or `lost`.

    `lost` is the one worth spelling out: the record was written, no finish was
    ever recorded, and either the pid is gone or the record is older than
    `TRUSTED_FOR`. It means the panel cannot say how it ended — which is a
    different statement from "it failed", and both are different from the silence
    this module replaced.

    `stopped` is `runner.EXIT_STOPPED` (from `nightshift.panel --dispatch-cards`,
    the "run several queued cards in sequence" job): the kill switch was already
    down when the sequence tried to start its next card, so that card's own
    runner process refused before doing anything. Nonzero, so `dispatch_cards`
    still stops the sequence there — but it is Karel's own `.ai/STOP`, not a
    defect, and reporting it as `failed` said the wrong one of those two things.
    """
    if job.finished:
        if job.exit_code == 0:
            return DONE
        return STOPPED if job.exit_code == EXIT_STOPPED else FAILED
    started = job.started_at
    now = now or dt.datetime.now()
    if started is None or now - started > TRUSTED_FOR:
        return LOST
    # A record whose wrapper has not written its pid yet is seconds old at most —
    # `record()` is immediately followed by the spawn. Believing it for that
    # window is what keeps a just-clicked button from rendering as `lost`.
    if not job.pid:
        return RUNNING
    return RUNNING if _pid_alive(job.pid) else LOST


def elapsed(job: Job, *, now: dt.datetime | None = None) -> str:
    """How long it ran, or has been running. Empty when that cannot be said."""
    started = job.started_at
    if started is None:
        return ""
    end = job.finished_at or (now or dt.datetime.now())
    seconds = int((end - started).total_seconds())
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {(seconds % 3600) // 60} min"


def _parse(stamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ the record


def jobs_dir(root: Path) -> Path:
    return root / JOBS_DIR


def record_path(root: Path, ident: str) -> Path:
    return jobs_dir(root) / f"{ident}.json"


def log_path(root: Path, ident: str) -> Path:
    return jobs_dir(root) / f"{ident}.log"


def record(root: Path, label: str, argv: list[str], *,
           now: dt.datetime | None = None) -> Job:
    """Write the record for a command about to be started, and return it.

    Called **before** the spawn, so a wrapper that cannot start at all still
    leaves the panel something to render. The identifier is the start time then
    the label, which reads as itself in a URL and sorts chronologically as a
    filename — the ordering `read_all` relies on.

    **The microseconds are what make that ordering true**, not precision for its
    own sake. With second resolution, two jobs started in the same second sorted
    by the *label* that follows the timestamp, so clicking Classify and then
    Chores listed Chores as the newer of the two. A collision suffix survives
    below as a backstop for a clock that repeats itself.
    """
    now = now or dt.datetime.now()
    stem = f"{now:%Y%m%d-%H%M%S-%f}-{_slug(label)}"
    ident, bump = stem, 1
    while record_path(root, ident).exists():
        bump += 1
        ident = f"{stem}-{bump}"
    job = Job(ident=ident, label=label, argv=list(argv), started=now.isoformat(timespec="seconds"))
    save(root, job)
    prune(root)
    return job


def _slug(label: str) -> str:
    """A label reduced to what is safe in a filename and a URL.

    `gates.run` keeps its dot, because a module name is what a label usually is
    and `gates-run` would not be one. A *doubled* dot does not survive: the id
    becomes a path segment in `/log/<id>`, and while `load()` refuses traversal
    outright, a name that cannot express `..` in the first place is the cheaper
    half of that guarantee.
    """
    safe = "".join(c if (c.isalnum() or c in "._-") else "-" for c in label)
    while ".." in safe:
        safe = safe.replace("..", "-")
    return safe or "job"


def save(root: Path, job: Job) -> None:
    path = record_path(root, job.ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    textio.write_text_lf(path, json.dumps({
        "ident": job.ident, "label": job.label, "argv": job.argv,
        "started": job.started, "pid": job.pid,
        "finished": job.finished, "exit_code": job.exit_code,
    }, indent=2) + "\n")


def load(root: Path, ident: str) -> Job | None:
    """One record, or `None` — including for an `ident` that is not one.

    The identifier arrives from a URL, so it is checked against the directory
    listing rather than pasted into a path: `..` in a job id must not read a file
    somewhere else, exactly as `panel.read_body` confines a board read.
    """
    path = record_path(root, ident)
    if path.parent.resolve() != jobs_dir(root).resolve() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("exit_code")
    return Job(
        ident=str(data.get("ident") or ident),
        label=str(data.get("label") or ""),
        argv=[str(a) for a in data.get("argv") or []],
        started=str(data.get("started") or ""),
        pid=int(data.get("pid") or 0),
        finished=str(data.get("finished") or ""),
        exit_code=code if isinstance(code, int) else None,
    )


def read_all(root: Path, *, limit: int = KEEP) -> list[Job]:
    """Every record, newest first. A directory that is not there is not an error —
    a repo where no button has ever been pressed simply has no jobs."""
    directory = jobs_dir(root)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.json"), reverse=True)[:limit]:
        job = load(root, path.stem)
        if job is not None:
            found.append(job)
    return found


def prune(root: Path, *, keep: int = KEEP) -> None:
    """Drop the oldest records and their logs once there are more than `keep`."""
    directory = jobs_dir(root)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json"), reverse=True)[keep:]:
        for target in (path, directory / f"{path.stem}.log"):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass


#: How much of a log the panel will render. A worker's transcript is not in here —
#: this is a command's console output — but `runner` on a full night prints a line
#: per phase per card, and a browser handed ten megabytes of `<pre>` is a browser
#: that stops responding. The *tail* is kept rather than the head: what a job did
#: last is what you came to read.
LOG_TAIL_BYTES = 400_000


def read_log(root: Path, ident: str, *, tail: int = LOG_TAIL_BYTES) -> str:
    """The job's output as it was written, or an honest sentence about why not."""
    path = log_path(root, ident)
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail:
                handle.seek(size - tail)
            raw = handle.read()
    except OSError as exc:
        return f"(the log could not be read: {exc})"
    text = raw.decode("utf-8", errors="replace")
    if size > tail:
        text = f"... (first {size - tail} bytes not shown)\n{text}"
    return text


# ----------------------------------------------------------------- the wrapper


def run(root: Path, ident: str) -> int:
    """`--run <id>`: run the recorded command as a child, and record how it ended.

    This process **is** the job as far as the panel is concerned: its pid is the
    one the record carries, and it lives exactly as long as the work does. Its own
    stdout and stderr are already the log file — the panel opened it and handed it
    over — so the child inherits them and its output lands in the same place, in
    order, with nothing in between to parse or lose it.
    """
    job = load(root, ident)
    if job is None:
        print(f"jobs: no record named {ident!r} under {JOBS_DIR.as_posix()}", flush=True)
        return 2
    job.pid = os.getpid()
    save(root, job)

    print(f"$ {job.command}", flush=True)
    print(f"# started {job.started} in {root}", flush=True)
    code = 0
    try:
        # No `capture_output`: inheriting is the whole point (see the docstring).
        # `PYTHONUNBUFFERED` is what makes the log readable *while* it runs rather
        # than in whatever chunks the child's buffer happens to flush, and
        # `PYTHONIOENCODING` stops a child that prints a `→` from dying on cp1252
        # the way `subprocess_encoding`'s gate documents.
        done = subprocess.run(  # gate-ok(subprocess_encoding): the child writes to
                                # the inherited log handle as bytes; nothing is
                                # decoded here, and PYTHONIOENCODING fixes the codec
                                # at the writing end where it belongs
            job.argv, cwd=root, stdin=subprocess.DEVNULL, check=False,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        code = done.returncode
    except OSError as exc:
        print(f"# could not start it: {exc}", flush=True)
        code = 127
    finally:
        job.finished = dt.datetime.now().isoformat(timespec="seconds")
        job.exit_code = code
        save(root, job)
        print(f"# exit {code} after {elapsed(job)}", flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one recorded Command Center job and record how it ended.")
    parser.add_argument("--root", type=Path, required=True, help="the repo root")
    parser.add_argument("--run", metavar="ID", required=True,
                        help="the job id to run, as written by `record()`")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    return run(args.root.resolve(), args.run)


if __name__ == "__main__":
    raise SystemExit(main())
