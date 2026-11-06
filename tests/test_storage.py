"""Disk usage helpers and per-architecture bucketing."""

from pathlib import Path

from sqlmodel import Session

from mirrorhub.db import get_engine
from mirrorhub.models import Repository, StorageCache
from mirrorhub.services import storage


def test_mount_usage_reads_statvfs(configured) -> None:
    usage = storage.mount_usage(configured.data_dir)
    assert usage.total_bytes > 0
    assert 0.0 <= usage.percent_used <= 100.0
    assert usage.path == str(configured.data_dir)


def test_scan_repo_buckets_by_architecture(configured, tmp_path: Path) -> None:
    root = tmp_path / "mirror"
    files = {
        "dists/bookworm/main/binary-amd64/Packages": 1000,
        "dists/bookworm/main/binary-arm64/Packages": 3000,
        "pool/main/a/apt/apt_2.6_amd64.deb": 5000,
        "core/os/x86_64/core.db": 700,
        "dists/bookworm/Release": 100,
        "partial/junk": 99999,
    }
    for relative, size in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    total, per_arch = storage.scan_repo(root, ["amd64", "arm64", "x86_64"])
    assert total == 1000 + 3000 + 5000 + 700 + 100  # partial/ is skipped
    assert per_arch["amd64"] == 6000
    assert per_arch["arm64"] == 3000
    assert per_arch["x86_64"] == 700
    assert per_arch["all"] == 100


def test_refresh_all_writes_cache(configured) -> None:
    repo_dir = configured.data_dir / "debian"
    (repo_dir / "pool").mkdir(parents=True, exist_ok=True)
    (repo_dir / "pool" / "pkg_amd64.deb").write_bytes(b"y" * 2048)

    with Session(get_engine()) as db:
        db.add(Repository(slug="debian", name="Debian", architectures=["amd64"]))
        db.commit()

    assert storage.refresh_all(configured) == 1
    with Session(get_engine()) as db:
        row = db.get(StorageCache, 1)
        assert row is not None
        assert row.bytes == 2048
        assert row.per_arch_bytes["amd64"] == 2048
