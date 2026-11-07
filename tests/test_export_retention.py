from pathlib import Path

from sqlmodel import Session, select

from mirrorhub.db import get_engine
from mirrorhub.models import Export, Repository, Setting
from mirrorhub.services import export_jobs
from mirrorhub.settings import Settings

IDS = [
    ("20261101T000000Z-full", "full"),
    ("20261102T000000Z-incr", "incr"),
    ("20261103T000000Z-full", "full"),
    ("20261104T000000Z-incr", "incr"),
]


def _setup(settings: Settings, keep: int) -> int:
    with Session(get_engine()) as db:
        repo = Repository(
            slug="files", name="Files", kind="generic", tool="wget", export_format="tar"
        )
        db.add(repo)
        db.add(Setting(key=export_jobs.RETENTION_KEY, value=keep))
        db.commit()
        db.refresh(repo)
        previous = None
        for export_id, mode in IDS:
            directory = settings.export_dir / "files" / export_id
            directory.mkdir(parents=True)
            (directory / ".done").write_text(export_id + "\n")
            (directory / "data.tar").write_bytes(b"x" * 10)
            db.add(
                Export(
                    export_id=export_id,
                    repo_id=repo.id,
                    mode=mode,
                    base_export_id=None if mode == "full" else previous,
                    path=str(directory),
                    status="published",
                    total_bytes=10,
                )
            )
            previous = export_id
        db.commit()
        return repo.id


def test_old_chains_are_removed_and_newest_is_kept(configured: Settings) -> None:
    _setup(configured, keep=1)
    with Session(get_engine()) as db:
        removed, freed = export_jobs.apply_retention(db, configured, actor="tester")
        remaining = [row.export_id for row in db.exec(select(Export)).all()]

    assert removed == ["files/20261101T000000Z-full", "files/20261102T000000Z-incr"]
    assert freed == 20
    assert remaining == ["20261103T000000Z-full", "20261104T000000Z-incr"]
    root = configured.export_dir / "files"
    assert not (root / "20261101T000000Z-full").exists()
    assert (root / "20261104T000000Z-incr" / ".done").is_file()


def test_zero_keeps_everything(configured: Settings) -> None:
    _setup(configured, keep=0)
    with Session(get_engine()) as db:
        removed, freed = export_jobs.apply_retention(db, configured, actor="tester")
        assert (removed, freed) == ([], 0)
        assert len(db.exec(select(Export)).all()) == 4


def test_single_chain_is_never_deleted(configured: Settings) -> None:
    _setup(configured, keep=2)
    with Session(get_engine()) as db:
        plan = export_jobs.retention_plan(db, 2)
    assert plan.exports == []