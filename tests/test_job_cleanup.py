from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.enums import JobStatus
from mirrorhub.models import Export, Job, ManifestSnapshot, RepoLock, Repository
from mirrorhub.services import jobs as jobsvc
from mirrorhub.settings import Settings

FINISHED = {JobStatus.CANCELLED.value}


def test_cleanup_keeps_active_jobs_and_releases_references(configured: Settings) -> None:
    with Session(get_engine()) as db:
        repo = Repository(
            slug="files", name="Files", kind="generic", tool="wget", export_format="tar"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)

        parent = Job(repo_id=repo.id, status=JobStatus.CANCELLED.value)
        active = Job(repo_id=repo.id, status=JobStatus.QUEUED.value)
        db.add(parent)
        db.add(active)
        db.commit()
        db.refresh(parent)
        db.refresh(active)

        child = Job(repo_id=repo.id, status=JobStatus.CANCELLED.value, parent_job_id=parent.id)
        db.add(child)
        db.add(Export(export_id="20261106T000000Z-full", repo_id=repo.id, job_id=parent.id))
        db.add(ManifestSnapshot(repo_id=repo.id, job_id=parent.id, path="/state/x.jsonl.zst"))
        db.add(RepoLock(repo_id=repo.id, job_id=parent.id, worker_id="w1"))
        db.commit()
        active_id = active.id

        removed = jobsvc.delete_finished(db, actor="tester", finished=FINISHED)

        assert removed == 2
        assert [job.id for job in db.exec(select(Job)).all()] == [active_id]
        assert db.exec(select(Export)).one().job_id is None
        assert db.exec(select(ManifestSnapshot)).one().job_id is None
        assert db.exec(select(RepoLock)).all() == []
