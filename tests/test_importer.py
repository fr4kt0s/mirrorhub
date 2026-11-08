"""Tests for the receiver-side importer, built on hand-made exports (export contract)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mirrorhub.importer.apply import Options, import_export
from mirrorhub.importer.cli import main
from mirrorhub.importer.config import Config, load_config
from mirrorhub.importer.errors import (
    ChainError,
    ConfigError,
    IoFailure,
    LockError,
    ToolMissingError,
    UnsafeArchiveError,
    VerificationFailed,
)
from mirrorhub.importer.state import StateStore

HAS_ZSTD = shutil.which("zstd") is not None
needs_zstd = pytest.mark.skipif(not HAS_ZSTD, reason="zstd binary not installed")
BASE_MTIME = 1_700_000_000_000_000_000


@dataclass(frozen=True)
class Node:
    data: bytes = b""
    target: str | None = None
    mtime_ns: int = BASE_MTIME


def fnode(data: str, mtime_ns: int = BASE_MTIME) -> Node:
    return Node(data=data.encode(), mtime_ns=mtime_ns)


def lnode(target: str) -> Node:
    return Node(target=target)


Tree = dict[str, Node]


def inventory_of(tree: Tree) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path, node in sorted(tree.items()):
        if node.target is not None:
            raw = node.target.encode()
            result[path] = {
                "type": "symlink",
                "target": node.target,
                "size": len(raw),
                "mtime_ns": node.mtime_ns,
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        else:
            result[path] = {
                "type": "file",
                "size": len(node.data),
                "mtime_ns": node.mtime_ns,
                "sha256": hashlib.sha256(node.data).hexdigest(),
            }
    return result


def build_export(
    inbox: Path,
    stamp: str,
    mode: str,
    tree: Tree,
    previous: Tree | None = None,
    base: str | None = None,
    slug: str = "ubuntu",
    compress: bool = False,
    split: int = 0,
    extra_members: tuple[tuple[str, bytes], ...] = (),
    wrong_content: tuple[str, ...] = (),
    done: bool = True,
) -> Path:
    export_id = f"{stamp}-{mode}"
    directory = inbox / slug / export_id
    directory.mkdir(parents=True)
    inventory = inventory_of(tree)
    before = inventory_of(previous or {})
    payload = (
        sorted(inventory)
        if mode == "full"
        else sorted(p for p in inventory if before.get(p) != inventory[p])
    )
    deleted = [] if mode == "full" else sorted(set(before) - set(inventory))

    archive = directory / "data.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        for path in payload:
            node = tree[path]
            info = tarfile.TarInfo(path)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = node.mtime_ns // 1_000_000_000
            if node.target is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = node.target
                info.mode = 0o777
                tar.addfile(info)
            else:
                data = node.data
                if path in wrong_content:
                    data = bytes(b ^ 1 for b in data)
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        for name, data in extra_members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    names = ["data.tar"]
    if compress:
        compressed = directory / "data.tar.zst"
        with compressed.open("xb") as out:
            subprocess.run(["zstd", "-q", "-T1", "-c", str(archive)], stdout=out, check=True)
        archive.unlink()
        archive = compressed
        names = ["data.tar.zst"]
    if split:
        content = archive.read_bytes()
        archive.unlink()
        chunks = [content[i : i + split] for i in range(0, len(content), split)] or [b""]
        names = []
        for index, chunk in enumerate(chunks):
            name = f"{archive.name}.{index:05d}"
            (directory / name).write_bytes(chunk)
            names.append(name)

    (directory / "files.json").write_text(json.dumps(inventory, sort_keys=True))
    (directory / "deleted.txt").write_text("".join(f"{p}\n" for p in deleted))
    manifest = {
        "format_version": 1,
        "mode": mode,
        "export_id": export_id,
        "repo": slug,
        "base_export_id": base,
        "created_at": "2026-11-08T00:00:00Z",
        "archive_format": "tar.zst" if compress else "tar",
        "archive_parts": names,
        "payload_paths": payload,
        "counts": {"inventory": len(inventory), "payload": len(payload), "deleted": len(deleted)},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    lines = []
    for item in sorted(p.name for p in directory.iterdir()):
        digest = hashlib.sha256((directory / item).read_bytes()).hexdigest()
        lines.append(f"{digest}  {item}\n")
    (directory / "MANIFEST.sha256").write_text("".join(lines))
    if done:
        (directory / ".done").write_text(export_id)
    return directory


def refresh(directory: Path) -> None:
    """Rewrite MANIFEST.sha256 so that it matches the current directory contents."""
    lines = []
    for item in sorted(directory.iterdir()):
        if item.name in {".done", "MANIFEST.sha256", "MANIFEST.sha256.sig"}:
            continue
        lines.append(f"{hashlib.sha256(item.read_bytes()).hexdigest()}  {item.name}\n")
    (directory / "MANIFEST.sha256").write_text("".join(lines))


def read_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = f"-> {os.readlink(path)}"
        elif path.is_file():
            result[relative] = path.read_text()
    return result


def export_snapshot(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        export_dir=tmp_path / "inbox",
        mirror_root=tmp_path / "mirror",
        state_dir=tmp_path / "state",
        staging_dir=tmp_path / "mirror" / ".mirrorhub-import",
    )


T1: Tree = {
    "dists/Release": fnode("r1"),
    "pool/a.deb": fnode("aaa"),
    "pool/b.deb": fnode("bbb"),
    "latest": lnode("pool/a.deb"),
}
T2: Tree = {
    "dists/Release": fnode("r2", BASE_MTIME + 5),
    "pool/a.deb": fnode("aaa"),
    "pool/c.deb": fnode("ccc"),
    "latest": lnode("pool/c.deb"),
}
ID1 = "20261101T000000Z-full"
ID2 = "20261102T000000Z-incr"
ID3 = "20261103T000000Z-incr"


def test_full_import(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    outcome = import_export(export, config, Options())
    assert outcome.status == "applied"
    tree = read_tree(config.mirror_root / "ubuntu")
    assert tree["pool/a.deb"] == "aaa" and tree["latest"] == "-> pool/a.deb"
    info = (config.mirror_root / "ubuntu/dists/Release").stat()
    assert info.st_mtime_ns == BASE_MTIME
    state = StateStore(config.state_dir, "ubuntu").load()
    assert state.last_export_id == ID1
    assert not (config.staging_dir / "ubuntu").exists()


def test_incremental_chain_and_deletes(config: Config) -> None:
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    import_export(full, config, Options())
    import_export(incr, config, Options())
    root = config.mirror_root / "ubuntu"
    tree = read_tree(root)
    assert "pool/b.deb" not in tree
    assert tree["pool/c.deb"] == "ccc" and tree["dists/Release"] == "r2"
    assert tree["latest"] == "-> pool/c.deb"
    assert (root / "dists/Release").stat().st_mtime_ns == BASE_MTIME + 5


def test_empty_directories_are_pruned_and_type_change(config: Config) -> None:
    first: Tree = {"a/b/c.txt": fnode("x"), "keep": fnode("k")}
    second: Tree = {"a": fnode("now a file"), "keep": fnode("k")}
    full = build_export(config.export_dir, "20261101T000000Z", "full", first)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", second, first, base=ID1)
    import_export(full, config, Options())
    import_export(incr, config, Options())
    assert read_tree(config.mirror_root / "ubuntu") == {"a": "now a file", "keep": "k"}


def test_repeated_import_is_noop(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    import_export(export, config, Options())
    before = read_tree(config.mirror_root / "ubuntu")
    again = import_export(export, config, Options())
    assert again.status == "noop"
    assert read_tree(config.mirror_root / "ubuntu") == before
    assert len(StateStore(config.state_dir, "ubuntu").load().history) == 1


def test_export_directory_is_never_modified(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    before = export_snapshot(export)
    import_export(export, config, Options())
    import_export(export, config, Options(dry_run=True))
    assert export_snapshot(export) == before


def test_dry_run_writes_nothing(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    outcome = import_export(export, config, Options(dry_run=True))
    assert outcome.status == "dry-run" and len(outcome.plan.adds) == 4
    assert not config.mirror_root.exists() and not config.state_dir.exists()


def test_dry_run_plans_changes_and_deletes(config: Config) -> None:
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    import_export(full, config, Options())
    plan = import_export(incr, config, Options(dry_run=True)).plan
    assert plan.adds == ["pool/c.deb"]
    assert plan.changes == ["dists/Release", "latest"]
    assert plan.deletes == ["pool/b.deb"]


def test_tampered_archive_fails_checksum(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    archive = export / "data.tar"
    raw = bytearray(archive.read_bytes())
    raw[600] ^= 0xFF
    archive.write_bytes(bytes(raw))
    with pytest.raises(VerificationFailed, match="checksum mismatch"):
        import_export(export, config, Options())
    assert not config.mirror_root.exists()


def test_tampered_manifest_checksum(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    (export / "deleted.txt").write_text("evil\n")
    with pytest.raises(VerificationFailed):
        import_export(export, config, Options())


def test_content_not_matching_inventory_is_rejected(config: Config) -> None:
    export = build_export(
        config.export_dir, "20261101T000000Z", "full", T1, wrong_content=("pool/a.deb",)
    )
    with pytest.raises(UnsafeArchiveError, match="content does not match"):
        import_export(export, config, Options())
    assert not (config.staging_dir / "ubuntu").exists()
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id is None


@pytest.mark.parametrize("name", ["../evil", "/abs/evil", "a\\b", "x/../../y"])
def test_path_traversal_member_is_rejected(config: Config, name: str, tmp_path: Path) -> None:
    export = build_export(
        config.export_dir, "20261101T000000Z", "full", T1, extra_members=((name, b"x"),)
    )
    with pytest.raises(UnsafeArchiveError):
        import_export(export, config, Options())
    assert not (tmp_path / "evil").exists()
    assert list(config.mirror_root.rglob("evil")) == []


def test_hardlink_member_is_rejected(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", {"a": fnode("1")})
    archive = export / "data.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("a")
        info.type = tarfile.LNKTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    refresh(export)
    with pytest.raises(UnsafeArchiveError, match="unsupported member type"):
        import_export(export, config, Options())


def test_missing_done_marker(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1, done=False)
    with pytest.raises(VerificationFailed):
        import_export(export, config, Options())


def test_wrong_base_and_gap(config: Config) -> None:
    t3: Tree = {**T2, "pool/d.deb": fnode("ddd")}
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    incr2 = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    incr3 = build_export(config.export_dir, "20261103T000000Z", "incr", t3, T2, base=ID2)
    bogus = build_export(
        config.export_dir, "20261104T000000Z", "incr", t3, T2, base="20260101T000000Z-full"
    )
    with pytest.raises(ChainError, match="no export has been applied"):
        import_export(incr2, config, Options())
    import_export(full, config, Options())
    with pytest.raises(ChainError, match=r"gap|base mismatch"):
        import_export(incr3, config, Options())
    with pytest.raises(ChainError, match="base mismatch"):
        import_export(bogus, config, Options())
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id == ID1


def test_full_over_state_needs_rebase(config: Config) -> None:
    first = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    second = build_export(config.export_dir, "20261105T000000Z", "full", T2)
    import_export(first, config, Options())
    with pytest.raises(ChainError, match="--rebase"):
        import_export(second, config, Options())
    import_export(second, config, Options(rebase=True))
    assert set(read_tree(config.mirror_root / "ubuntu")) >= {"pool/c.deb"}
    assert "pool/b.deb" not in read_tree(config.mirror_root / "ubuntu")


def test_first_import_into_non_empty_tree_needs_rebase(config: Config) -> None:
    (config.mirror_root / "ubuntu").mkdir(parents=True)
    (config.mirror_root / "ubuntu" / "stray").write_text("x")
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    with pytest.raises(ChainError):
        import_export(export, config, Options())
    import_export(export, config, Options(rebase=True))
    assert not (config.mirror_root / "ubuntu" / "stray").exists()


def test_split_parts_are_reassembled(config: Config) -> None:
    big: Tree = {f"f{i}": fnode(str(i) * 700) for i in range(10)}
    export = build_export(config.export_dir, "20261101T000000Z", "full", big, split=1500)
    assert len(list(export.glob("data.tar.0*"))) > 2
    import_export(export, config, Options())
    assert read_tree(config.mirror_root / "ubuntu")["f7"] == "7" * 700


def test_non_contiguous_parts_are_rejected(config: Config) -> None:
    big: Tree = {f"f{i}": fnode(str(i) * 700) for i in range(10)}
    export = build_export(config.export_dir, "20261101T000000Z", "full", big, split=1500)
    manifest = json.loads((export / "manifest.json").read_text())
    last = manifest["archive_parts"][-1]
    (export / last).rename(export / "data.tar.00099")
    manifest["archive_parts"][-1] = "data.tar.00099"
    (export / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    refresh(export)
    with pytest.raises(VerificationFailed, match="contiguous"):
        import_export(export, config, Options())


@needs_zstd
def test_zstd_export(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1, compress=True)
    import_export(export, config, Options())
    assert read_tree(config.mirror_root / "ubuntu")["pool/b.deb"] == "bbb"


@needs_zstd
def test_zstd_split_export(config: Config) -> None:
    big: Tree = {f"f{i}": fnode(str(i) * 900) for i in range(8)}
    export = build_export(
        config.export_dir, "20261101T000000Z", "full", big, compress=True, split=400
    )
    import_export(export, config, Options())
    assert read_tree(config.mirror_root / "ubuntu")["f3"] == "3" * 900


def test_zstd_missing_is_reported(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    (export / "data.tar").rename(export / "data.tar.zst")
    manifest = json.loads((export / "manifest.json").read_text())
    manifest["archive_format"] = "tar.zst"
    manifest["archive_parts"] = ["data.tar.zst"]
    (export / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    refresh(export)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ToolMissingError, match="zstd"):
        import_export(export, config, Options())
    assert not (config.staging_dir / "ubuntu").exists()


def test_interrupted_apply_is_resumed(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    real_replace = os.replace
    calls = {"count": 0}

    def flaky(src: Any, dst: Any) -> None:
        if ".mirrorhub-import" in str(src) and "files" in str(src):
            calls["count"] += 1
            if calls["count"] == 3:
                raise OSError("simulated crash")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(IoFailure):
        import_export(export, config, Options())
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id is None
    assert (config.staging_dir / "ubuntu" / "journal.json").exists()
    monkeypatch.setattr(os, "replace", real_replace)
    assert import_export(export, config, Options()).status == "applied"
    assert read_tree(config.mirror_root / "ubuntu")["pool/a.deb"] == "aaa"
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id == ID1


def test_concurrent_import_is_refused(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    store = StateStore(config.state_dir, "ubuntu")
    with store.locked(), pytest.raises(LockError):
        import_export(export, config, Options())


def test_staging_inside_tree_is_refused(config: Config) -> None:
    inside = config.mirror_root / "ubuntu" / "s"
    bad = Config(config.export_dir, config.mirror_root, config.state_dir, inside)
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    with pytest.raises(ConfigError):
        import_export(export, bad, Options())


def test_config_file_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inbox = tmp_path / "in"
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    build_export(inbox, "20261103T000000Z", "incr", T2, T2, base=ID2, done=False)
    (inbox / "ubuntu" / "20261104T000000Z-incr.tmp").mkdir()
    settings = tmp_path / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    base = ["--config", str(settings)]
    assert load_config(settings).export_dir == tmp_path / "in"
    assert main([*base, "import", "--slug", "ubuntu", "--dry-run"]) == 0
    assert not (tmp_path / "out").exists()
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    assert "pool/b.deb" not in read_tree(tmp_path / "out" / "ubuntu")
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    assert "nothing to import" in capsys.readouterr().out
    assert main([*base, "status"]) == 0
    assert StateStore(tmp_path / "st", "ubuntu").load().last_export_id == ID2


def test_cli_exit_codes(tmp_path: Path) -> None:
    settings = tmp_path / "importer.toml"
    assert main(["--config", str(settings), "import", "--slug", "x"]) == 2
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\nbogus = 1\n')
    assert main(["--config", str(settings), "import", "--slug", "x"]) == 2
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    broken = build_export(tmp_path / "in", "20261101T000000Z", "full", T1, done=False)
    assert main(["--config", str(settings), "import", "--export", str(broken)]) == 3
    good = build_export(tmp_path / "in", "20261102T000000Z", "incr", T2, T1, base=ID1)
    assert main(["--config", str(settings), "import", "--export", str(good)]) == 5


def test_import_all_continues_after_a_failing_slug(tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    build_export(inbox, "20261101T000000Z", "full", T1, slug="arch")
    build_export(inbox, "20261101T000000Z", "full", T1, slug="ubuntu")
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1, slug="broken")
    (inbox / "lost+found").mkdir()
    settings = tmp_path / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    base = ["--config", str(settings)]
    assert main([*base, "import", "--all"]) == 5
    assert read_tree(tmp_path / "out" / "arch")["pool/a.deb"] == "aaa"
    assert read_tree(tmp_path / "out" / "ubuntu")["pool/b.deb"] == "bbb"
    assert StateStore(tmp_path / "st", "broken").load().last_export_id is None
    assert main([*base, "import", "--all", "--rebase"]) == 2
