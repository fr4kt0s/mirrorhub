"""Following log files a tool writes itself (ftpsync): worker/followlog.py."""

from __future__ import annotations

import os
from pathlib import Path

from mirrorhub.worker.followlog import FollowedLog, LogFollower, archived_path


def _write(path: Path, text: str, mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(text)


def test_only_lines_of_this_run_are_copied(tmp_path: Path) -> None:
    log = tmp_path / "ftpsync.log"
    _write(log, "old run 1\nold run 2\n")
    follower = LogFollower((FollowedLog((log,), "[ftpsync]"),))
    assert follower.poll() == []
    _write(log, "new 1\nnew ")  # second line still being written
    assert follower.poll() == ["[ftpsync] new 1"]
    _write(log, "2\n")
    assert follower.poll() == ["[ftpsync] new 2"]
    _write(log, "last line without newline")
    assert follower.finish() == ["[ftpsync] last line without newline"]


def test_a_rotated_file_is_read_from_the_start(tmp_path: Path) -> None:
    log = tmp_path / "rsync.log"
    _write(log, "previous run\n")
    follower = LogFollower((FollowedLog((log,), "[rsync]"),))
    log.rename(tmp_path / "rsync.log.0")  # savelog at the start of the run
    _write(log, "first\nsecond\n", "w")
    assert follower.poll() == ["[rsync] first", "[rsync] second"]
    assert follower.finish() == []


def test_a_file_that_appears_later_and_the_second_name(tmp_path: Path) -> None:
    follower = LogFollower(
        (FollowedLog((tmp_path / "x.error", tmp_path / "x.error.log"), "[rsync error]"),)
    )
    assert follower.poll() == []
    _write(tmp_path / "x.error.log", "rsync: failed\n")
    assert follower.poll() == ["[rsync error] rsync: failed"]


def test_long_files_are_shortened_with_the_closing_summary(tmp_path: Path) -> None:
    log = tmp_path / "rsync.log"
    follower = LogFollower((FollowedLog((log,), "[rsync]", live_lines=3),))
    _write(log, "".join(f"file {n}\n" for n in range(1000)))
    _write(log, "Number of files: 1,000\nTotal file size: 1 MB\nsent 1 bytes\n")
    lines = follower.poll() + follower.finish()
    assert lines[:3] == ["[rsync] file 0", "[rsync] file 1", "[rsync] file 2"]
    assert lines[3].startswith("[rsync] ... further lines are not shown here")
    assert lines[4] == "[rsync] ... 1000 lines not shown; end of the log:"
    assert lines[5:] == [
        "[rsync] Number of files: 1,000",
        "[rsync] Total file size: 1 MB",
        "[rsync] sent 1 bytes",
    ]


def test_summary_without_marker_shows_the_last_lines(tmp_path: Path) -> None:
    log = tmp_path / "rsync.log"
    follower = LogFollower((FollowedLog((log,), "[rsync]", live_lines=1),))
    _write(log, "".join(f"line {n}\n" for n in range(100)))
    lines = follower.finish()
    assert lines[-1] == "[rsync] line 99" and lines[-20] == "[rsync] line 80"
    assert "[rsync] line 79" not in lines


def test_the_file_of_this_run_is_kept_with_the_job(tmp_path: Path) -> None:
    log = tmp_path / "rsync.log"
    job_log = tmp_path / "jobs" / "42.log"
    job_log.parent.mkdir()
    spec = FollowedLog((log,), "[rsync]", live_lines=1, archive_as="rsync")

    follower = LogFollower((spec,))
    _write(log, "a\nb\n")
    follower.finish()
    assert follower.archive(job_log) == [archived_path(job_log, "rsync")]
    assert (tmp_path / "jobs" / "42.rsync.log").read_text() == "a\nb\n"
    assert not log.exists()

    retry = LogFollower((spec,))  # second attempt of the same job: appended
    _write(log, "c\n")
    retry.finish()
    retry.archive(job_log)
    assert (tmp_path / "jobs" / "42.rsync.log").read_text() == "a\nb\nc\n"


def test_an_untouched_earlier_file_is_not_kept(tmp_path: Path) -> None:
    log = tmp_path / "rsync.log"
    _write(log, "from an earlier run\n")
    os.utime(log, (1, 1))
    follower = LogFollower((FollowedLog((log,), "[rsync]", archive_as="rsync"),))
    assert follower.finish() == []
    assert follower.archive(tmp_path / "7.log") == []
    assert log.exists()
