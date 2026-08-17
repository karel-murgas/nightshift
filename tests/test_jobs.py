"""`nightshift.jobs` — what the Command Center started, and how it went.

The module exists because of one measured silence: on 2026-08-17 "Classify all"
ran `ingest` to completion — thirteen notes routed, `Routing.md` rewritten — and
the panel said nothing, because `spawn_background` sent the command's output to
`DEVNULL` and kept no record. So the properties asserted here are the ones that
turn "a process was started" into something that can be reported:

**A record exists before the process does**, so a spawn that fails outright still
leaves a row saying what was meant to run.

**The exit code is recorded by something that outlives the panel** — the wrapper,
not the server, and in a `finally`, so a command that raises still ends up with a
status rather than an eternal "running".

**Nothing is inferred from the pid while a finish is on file.** `panel.run_is_live`
documents the recycled-pid trap at length; a job that recorded how it ended must
never be re-judged by asking the operating system about a number.

Real subprocesses throughout for the lifecycle test — the thing being tested is
that a detached process writes a file back, and a mock of `Popen` cannot fail the
way the real one can.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from nightshift import jobs, panel


def _now() -> dt.datetime:
    return dt.datetime(2026, 8, 17, 9, 47, 0)


# ------------------------------------------------------------------ the record


def test_the_record_is_written_before_anything_is_spawned(tmp_path):
    """`record()` is the panel's first act, not its last. A wrapper that never
    starts still leaves the row that says what was meant to run."""
    job = jobs.record(tmp_path, "ingest", ["python", "-m", "nightshift.ingest"])
    assert jobs.record_path(tmp_path, job.ident).is_file()
    assert jobs.load(tmp_path, job.ident).argv == ["python", "-m", "nightshift.ingest"]
    assert job.finished == "" and job.exit_code is None


def test_two_jobs_in_the_same_second_do_not_overwrite_each_other(tmp_path):
    first = jobs.record(tmp_path, "ingest", ["a"], now=_now())
    second = jobs.record(tmp_path, "ingest", ["b"], now=_now())
    assert first.ident != second.ident
    assert jobs.load(tmp_path, first.ident).argv == ["a"]
    assert jobs.load(tmp_path, second.ident).argv == ["b"]


def test_a_label_that_is_not_filename_safe_still_produces_one(tmp_path):
    job = jobs.record(tmp_path, "gates.run/../etc", ["x"])
    assert "/" not in job.ident and ".." not in job.ident
    assert jobs.load(tmp_path, job.ident) is not None


def test_an_ident_from_a_url_cannot_read_outside_the_jobs_directory(tmp_path):
    """The id arrives in `/log/<id>`. Traversal is refused the same way
    `panel.read_body` confines a board read."""
    (tmp_path / "secret.json").write_text('{"ident": "secret"}', encoding="utf-8")
    assert jobs.load(tmp_path, "../../secret") is None


def test_records_are_read_back_newest_first(tmp_path):
    old = jobs.record(tmp_path, "one", ["a"], now=dt.datetime(2026, 8, 17, 9, 0))
    new = jobs.record(tmp_path, "two", ["b"], now=dt.datetime(2026, 8, 17, 11, 0))
    assert [j.ident for j in jobs.read_all(tmp_path)] == [new.ident, old.ident]


def test_a_repo_where_no_button_was_ever_pressed_has_no_jobs(tmp_path):
    assert jobs.read_all(tmp_path) == []


def test_pruning_drops_the_oldest_records_and_their_logs(tmp_path):
    made = [jobs.record(tmp_path, "j", ["x"], now=dt.datetime(2026, 8, 17, 9, 0, second=s))
            for s in range(6)]
    for job in made:
        jobs.log_path(tmp_path, job.ident).write_text("out", encoding="utf-8")
    jobs.prune(tmp_path, keep=2)
    left = {j.ident for j in jobs.read_all(tmp_path)}
    assert left == {made[-1].ident, made[-2].ident}
    assert not jobs.log_path(tmp_path, made[0].ident).exists()


# ------------------------------------------------------------------- the state


def test_a_finished_job_reports_its_outcome_and_is_never_asked_about_its_pid(tmp_path):
    """The recycled-pid trap, closed by ordering: a recorded finish is the
    answer, so a pid that now belongs to something else is never consulted."""
    job = jobs.Job(ident="i", label="l", started=_now().isoformat(), pid=1,
                   finished=_now().isoformat(), exit_code=0)
    assert jobs.state(job, now=_now()) == jobs.DONE
    job.exit_code = 3
    assert jobs.state(job, now=_now()) == jobs.FAILED


def test_a_record_with_no_pid_yet_is_running_not_lost(tmp_path):
    """`record()` is immediately followed by the spawn, so the window in which no
    pid is on file is seconds wide — and rendering a just-clicked button as
    `lost` would be the same false report in the other direction."""
    job = jobs.Job(ident="i", label="l", started=_now().isoformat())
    assert jobs.state(job, now=_now() + dt.timedelta(seconds=2)) == jobs.RUNNING


def test_a_record_older_than_the_trust_window_is_lost_rather_than_running(tmp_path):
    job = jobs.Job(ident="i", label="l", started=_now().isoformat(), pid=1)
    later = _now() + jobs.TRUSTED_FOR + dt.timedelta(minutes=1)
    assert jobs.state(job, now=later) == jobs.LOST


def test_a_job_whose_process_is_gone_without_a_finish_is_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    job = jobs.Job(ident="i", label="l", started=_now().isoformat(), pid=999999)
    assert jobs.state(job, now=_now()) == jobs.LOST


def test_elapsed_measures_to_the_finish_once_there_is_one():
    job = jobs.Job(ident="i", label="l", started=_now().isoformat(),
                   finished=(_now() + dt.timedelta(minutes=4)).isoformat(), exit_code=0)
    assert jobs.elapsed(job, now=_now() + dt.timedelta(hours=9)) == "4 min"


def test_elapsed_says_nothing_rather_than_something_wrong_about_a_broken_stamp():
    assert jobs.elapsed(jobs.Job(ident="i", label="l", started="not a date")) == ""


# --------------------------------------------------------------------- the log


def test_the_log_is_the_commands_own_output_and_the_exit_code_is_recorded(tmp_path):
    """The whole lifecycle, with a real detached process: output lands in the
    log, and the wrapper writes back how it ended after the panel has stopped
    caring."""
    root = tmp_path / "repo"
    root.mkdir()
    panel.spawn_job(root, "demo", [
        sys.executable, "-c", "import sys; print('working'); sys.exit(3)"])

    job = _await_finish(root)
    assert jobs.state(job) == jobs.FAILED
    assert job.exit_code == 3
    log = jobs.read_log(root, job.ident)
    assert "working" in log, log
    assert "$ python -c" in log, "the log says what was run"
    assert "# exit 3" in log, log


def test_a_command_that_cannot_start_is_recorded_as_failed_not_left_running(tmp_path):
    """The case the old `DEVNULL` spawn could not distinguish from success."""
    root = tmp_path / "repo"
    root.mkdir()
    panel.spawn_job(root, "nope", [str(tmp_path / "no-such-binary"), "--go"])

    job = _await_finish(root)
    assert jobs.state(job) == jobs.FAILED
    assert "could not start it" in jobs.read_log(root, job.ident)


def test_only_the_tail_of_a_very_long_log_is_served(tmp_path):
    job = jobs.record(tmp_path, "big", ["x"])
    jobs.log_path(tmp_path, job.ident).write_text("A" * 500 + "END", encoding="utf-8")
    text = jobs.read_log(tmp_path, job.ident, tail=100)
    assert text.endswith("END")
    assert "not shown" in text
    assert len(text) < 400


def test_reading_the_log_of_a_job_that_has_not_written_one_is_empty_not_an_error(tmp_path):
    job = jobs.record(tmp_path, "quiet", ["x"])
    assert jobs.read_log(tmp_path, job.ident) == ""


def test_the_wrapper_refuses_an_ident_it_has_no_record_for(tmp_path, capsys):
    assert jobs.run(tmp_path, "20260817-000000-nothing") == 2
    assert "no record" in capsys.readouterr().out


def _await_finish(root: Path, *, timeout: float = 60.0) -> jobs.Job:
    """Wait for the one job in `root` to record a finish.

    Polling a file rather than waiting on a handle is the point: the panel has no
    handle — the process is detached so that closing the browser cannot kill a
    night — and the file is the only channel back.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = jobs.read_all(root)
        if found and found[0].finished:
            return found[0]
        time.sleep(0.1)
    found = jobs.read_all(root)
    raise AssertionError(
        f"no finish recorded within {timeout}s: "
        f"{json.dumps([f.__dict__ for f in found], default=str)}")
