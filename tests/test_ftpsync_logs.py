"""ftpsync log files in the job log, kept with the job, downloadable, deleted with it."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus
from mirrorhub.models import Job, Repository
from mirrorhub.services import logs
from mirrorhub.settings import Settings, get_settings
from mirrorhub.worker.builders import BuildPlan, FollowedLog, Step
from mirrorhub.worker.runner import Runner
from test_builders import _build, _job, _repo

FAKE_FTPSYNC = textwrap.dedent(
    """
    import os, sys, time
    log_dir = sys.argv[1]
    def path(name):
        return os.path.join(log_dir, name)
    # savelog at the start of a run
    for name in ("ftpsync-mint.log", "rsync-ftpsync-mint.log"):
        if os.path.exists(path(name)):
            os.replace(path(name), path(name) + ".0")
    with open(path("ftpsync-mint.log"), "a") as log:
        log.write("Mirrorsync start\\n"); log.flush()
        with open(path("rsync-ftpsync-mint.log"), "a") as rsync:
            for n in range(1200):
                rsync.write(f"pool/main/p/pkg{n}.deb\\n")
            rsync.write("Number of files: 1,200\\nTotal transferred file size: 9 MB\\n")
        time.sleep(1)
        with open(path("rsync-ftpsync-mint.error"), "a") as error:
            error.write("rsync: some files vanished\\n")
        log.write("Mirrorsync done with errors\\n")
    print("ftpsync finished")
    sys.exit(int(sys.argv[2]))
    """
)


def _fake_plan(tmp_path: Path, log_dir: Path, exit_code: int = 0) -> BuildPlan:
    script = tmp_path / "fake_ftpsync.py"
    script.write_text(FAKE_FTPSYNC)
    follow = (
        FollowedLog(
            (log_dir / "rsync-ftpsync-mint.log",), "[rsync]", live_lines=500, archive_as="rsync"
        ),
        FollowedLog(
            (log_dir / "rsync-ftpsync-mint.error", log_dir / "rsync-ftpsync-mint.error.log"),
            "[rsync error]",
        ),
        FollowedLog((log_dir / "ftpsync-mint.log",), "[ftpsync]"),
    )
    argv = (sys.executable, str(script), str(log_dir), str(exit_code))
    return BuildPlan(steps=[Step(argv, "ftpsync", follow)], cwd=tmp_path, target=tmp_path)


def _run(plan: BuildPlan, job_log: Path) -> int:
    runner = Runner(plan, job_log, tick=lambda _p, _t: None, is_cancelled=lambda: False)
    return runner.run().exit_code


def test_ftpsync_files_appear_in_the_job_log_and_rsync_is_kept(tmp_path: Path) -> None:
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "ftpsync-mint.log").write_text("line of the previous run\n")
    job_log = tmp_path / "jobs" / "5.log"
    job_log.parent.mkdir()

    assert _run(_fake_plan(tmp_path, log_dir, exit_code=1), job_log) == 1
    text = job_log.read_text()
    assert "previous run" not in text
    assert "[ftpsync] Mirrorsync start" in text and "[ftpsync] Mirrorsync done with errors" in text
    assert "[rsync error] rsync: some files vanished" in text
    assert "[rsync] pool/main/p/pkg499.deb" in text and "pkg500.deb" not in text
    assert "further lines are not shown here" in text
    assert "[rsync] ... 702 lines not shown; end of the log:" in text
    assert "[rsync] Number of files: 1,200" in text
    assert "# complete log kept as 5.rsync.log" in text
    kept = tmp_path / "jobs" / "5.rsync.log"
    assert kept.read_text().count("\n") == 1202
    assert not (log_dir / "rsync-ftpsync-mint.log").exists()


def test_builder_follows_the_ftpsync_files(configured: Settings) -> None:
    repo = _repo(slug="mint", tool="ftpsync", method="rsync")
    plan = _build(repo, _job(repo), get_settings())
    follow = plan.steps[0].follow
    log_dir = logs.ftpsync_log_dir("mint")
    assert [spec.prefix for spec in follow] == ["[rsync]", "[rsync error]", "[ftpsync]"]
    assert follow[0].paths == (log_dir / "rsync-ftpsync-mint.log",)
    assert follow[0].live_lines == 500 and follow[0].archive_as == logs.RSYNC_LOG
    assert follow[2].paths == (log_dir / "ftpsync-mint.log",)


def _ftpsync_job(status: str) -> tuple[int, str]:
    with Session(get_engine()) as db:
        repo = Repository(slug="mint", name="Mint", tool="ftpsync", method="rsync")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        assert repo.id is not None
        job = Job(repo_id=repo.id, tool="ftpsync", status=status)
        db.add(job)
        db.commit()
        db.refresh(job)
        assert job.id is not None
        return job.id, repo.slug


def test_download_and_delete_with_the_job(
    admin: TestClient, csrf_token: str, configured: Settings
) -> None:
    job_id, _slug = _ftpsync_job(JobStatus.SUCCEEDED.value)
    assert "Download rsync log" not in admin.get(f"/jobs/{job_id}").text
    assert admin.get(f"/jobs/{job_id}/rsync-log").status_code == 404

    logs.job_log_path(job_id).write_text("job log\n")
    kept = logs.kept_log_path(job_id, logs.RSYNC_LOG)
    kept.write_text("pool/a.deb\npool/b.deb\n")
    assert "Download rsync log" in admin.get(f"/jobs/{job_id}").text
    response = admin.get(f"/jobs/{job_id}/rsync-log")
    assert response.status_code == 200 and response.text == "pool/a.deb\npool/b.deb\n"
    assert f'filename="job-{job_id}-rsync-mint.log"' in response.headers["content-disposition"]

    deleted = admin.post(f"/jobs/{job_id}/delete", headers={"X-CSRF-Token": csrf_token})
    assert deleted.status_code in (200, 303)
    assert not kept.exists() and not logs.job_log_path(job_id).exists()


def test_running_job_offers_the_file_being_written(admin: TestClient, configured: Settings) -> None:
    job_id, slug = _ftpsync_job(JobStatus.RUNNING.value)
    assert "Download rsync log" in admin.get(f"/jobs/{job_id}").text
    assert admin.get(f"/jobs/{job_id}/rsync-log").status_code == 404  # nothing written yet
    live = logs.ftpsync_rsync_log(slug)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("pool/now.deb\n")
    assert admin.get(f"/jobs/{job_id}/rsync-log").text == "pool/now.deb\n"


def test_remove_job_logs_counts_jobs(configured: Settings) -> None:
    for name in ("9.log", "9.rsync.log", "10.log"):
        (logs.job_log_path(9).parent / name).write_text("x")
    assert logs.remove_job_logs([9, 10, 11]) == 2
    assert not list(logs.job_log_path(9).parent.glob("9.*"))
