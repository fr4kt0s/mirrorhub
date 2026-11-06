"""Bounded log reads and completed-job viewer behavior."""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.models import Job, Repository
from mirrorhub.services import logs
from mirrorhub.settings import Settings


def test_tail_reads_only_bounded_window(tmp_path: Path) -> None:
    path = tmp_path / "large.log"
    path.write_bytes(b"x" * (2 * 1024 * 1024))

    lines, offset = logs.tail_snapshot(path, max_bytes=4096)

    assert sum(len(line) for line in lines) <= 4096
    assert offset == 2 * 1024 * 1024


def test_tail_returns_last_lines(tmp_path: Path) -> None:
    path = tmp_path / "job.log"
    path.write_text("first\nsecond\nthird\n", encoding="utf-8")

    lines, offset = logs.tail_snapshot(path, lines=2)

    assert lines == ["second", "third"]
    assert offset == path.stat().st_size


def test_stream_reads_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "job.log"
    path.write_bytes(b"a" * 10000)

    text, offset, eof = logs.read_chunk(path, 0, max_bytes=1024)

    assert len(text) == 1024
    assert offset == 1024
    assert not eof


def test_stream_offset_does_not_replay_initial_tail(tmp_path: Path) -> None:
    path = tmp_path / "job.log"
    path.write_text("old output\n", encoding="utf-8")
    _lines, offset = logs.tail_snapshot(path)

    with path.open("a", encoding="utf-8") as handle:
        handle.write("new output\n")

    text, _offset, eof = logs.read_chunk(path, offset)

    assert text == "new output\n"
    assert eof


def test_completed_job_does_not_open_sse(
    admin: TestClient,
    configured: Settings,
) -> None:
    with Session(get_engine()) as db:
        repo = Repository(slug="log-test", name="Log test")
        db.add(repo)
        db.flush()
        assert repo.id is not None

        job = Job(
            repo_id=repo.id,
            status="succeeded",
            tool="debmirror",
        )
        db.add(job)
        db.flush()
        assert job.id is not None
        job_id = job.id

        path = logs.job_log_path(job_id, configured)
        path.write_text("completed output\n", encoding="utf-8")
        job.log_path = str(path)
        db.add(job)
        db.commit()

    page = admin.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert "completed output" in page.text
    assert "data-sse=" not in page.text

    stream = admin.get(f"/jobs/{job_id}/events")
    assert stream.status_code == 204
