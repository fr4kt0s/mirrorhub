from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Export, Job, Repository
from mirrorhub.services import export_jobs
from mirrorhub.settings import Settings


def test_failed_and_stale_exports_are_removed(configured: Settings) -> None:
    root = configured.export_dir / "files"
    root.mkdir(parents=True)
    with Session(get_engine()) as db:
        repo = Repository(
            slug="files", name="Files", kind="generic", tool="wget", export_format="tar"
        )
        db.add(repo)
        db.commit()
        db.refresh(repo)
        running = Job(repo_id=repo.id, type="export", tool="export", status="running")
        db.add(running)
        db.commit()
        db.refresh(running)

        failed = "20261101T000000Z-full"
        stale = "20261102T000000Z-incr"
        live = "20261103T000000Z-incr"
        done = "20261104T000000Z-full"
        for export_id, status, job_id in (
            (failed, "failed", None),
            (stale, "building", None),
            (live, "building", running.id),
            (done, "failed", None),
        ):
            db.add(
                Export(
                    export_id=export_id, repo_id=repo.id, mode="full", status=status, job_id=job_id
                )
            )
        (root / f"{failed}.tmp").mkdir()
        (root / f"{stale}.tmp").mkdir()
        (root / f"{live}.tmp").mkdir()
        (root / done).mkdir()
        (root / done / ".done").write_text(done + "\n")
        db.commit()

        removed = export_jobs.delete_failed(db, configured, actor="tester")
        remaining = sorted(row.export_id for row in db.exec(select(Export)).all())

    assert sorted(removed) == [f"files/{failed}", f"files/{stale}"]
    assert remaining == [live, done]
    assert not (root / f"{failed}.tmp").exists()
    assert (root / f"{live}.tmp").exists()
    assert (root / done / ".done").is_file()
