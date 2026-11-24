"""Tests for the receiver-side importer, built on hand-made exports (export contract)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mirrorhub import verify as verify_module
from mirrorhub.importer import apply as apply_module
from mirrorhub.importer import checks as checks_module
from mirrorhub.importer import tree as tree_module
from mirrorhub.importer.adopt import adopt
from mirrorhub.importer.apply import Options, import_export
from mirrorhub.importer.cli import main
from mirrorhub.importer.config import Config, load_config
from mirrorhub.importer.errors import (
    ChainError,
    ConfigError,
    IoFailure,
    LockError,
    PostCheckError,
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
    assert read_tree(config.mirror_root / "ubuntu") == {}
    assert not (config.staging_dir / "ubuntu").exists()
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id is None


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


# Superseded exports ("incremental since" on the sender) and adopt


T3: Tree = {
    "dists/Release": fnode("r3", BASE_MTIME + 9),
    "pool/a.deb": fnode("aaa"),
    "pool/d.deb": fnode("ddd"),
    "latest": lnode("pool/d.deb"),
}
SINCE = "20261104T000000Z-incr"


def _inbox_cli(tmp_path: Path) -> tuple[Path, list[str]]:
    settings = tmp_path / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    return tmp_path / "in", ["--config", str(settings)]


def test_export_since_skips_the_one_it_replaces(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    build_export(inbox, "20261104T000000Z", "incr", T3, T1, base=ID1)

    assert main([*base, "import", "--slug", "ubuntu"]) == 0

    tree = read_tree(tmp_path / "out" / "ubuntu")
    assert tree["pool/d.deb"] == "ddd" and "pool/c.deb" not in tree
    state = StateStore(tmp_path / "st", "ubuntu").load()
    assert state.last_export_id == SINCE
    assert [(item["export_id"], item.get("status")) for item in state.history] == [
        (ID1, None),
        (ID2, "superseded"),
        (SINCE, None),
    ]
    assert main([*base, "import", "--slug", "ubuntu"]) == 0  # nothing left, no error


def test_late_export_is_skipped_as_superseded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261104T000000Z", "incr", T3, T1, base=ID1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0

    late = build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    assert main([*base, "import", "--slug", "ubuntu", "--dry-run"]) == 0
    assert not StateStore(tmp_path / "st", "ubuntu").load().superseded(ID2)

    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    assert main([*base, "import", "--export", str(late)]) == 0
    state = StateStore(tmp_path / "st", "ubuntu").load()
    assert state.last_export_id == SINCE
    assert state.superseded(ID2) and not state.applied(ID2)
    assert sum(1 for item in state.history if item["export_id"] == ID2) == 1
    assert read_tree(tmp_path / "out" / "ubuntu")["pool/d.deb"] == "ddd"

    capsys.readouterr()
    assert main([*base, "status", "--slug", "ubuntu"]) == 0
    assert "applied=2 superseded=1" in capsys.readouterr().out


def test_explicit_rebase_of_an_older_full_is_not_superseded(config: Config) -> None:
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    newer = build_export(config.export_dir, "20261105T000000Z", "full", T2)
    import_export(newer, config, Options())

    outcome = import_export(full, config, Options(rebase=True))

    assert outcome.status == "applied"
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id == ID1


def _applied_then_state_lost(config: Config) -> Path:
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    import_export(full, config, Options())
    import_export(incr, config, Options())
    (config.state_dir / "ubuntu.json").unlink()
    return incr


def test_adopt_restores_a_lost_state_file(config: Config) -> None:
    _applied_then_state_lost(config)

    result = adopt(config, "ubuntu", export_id=ID2)

    assert result.export_id == ID2 and not result.hashed
    state = StateStore(config.state_dir, "ubuntu").load()
    assert state.last_export_id == ID2
    assert state.history[-1]["status"] == "adopted"
    nxt = build_export(config.export_dir, "20261103T000000Z", "incr", T3, T2, base=ID2)
    assert import_export(nxt, config, Options()).status == "applied"


def test_adopt_auto_picks_the_newest_matching_export(config: Config) -> None:
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    import_export(full, config, Options())
    build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    (config.state_dir / "ubuntu.json").unlink()

    assert adopt(config, "ubuntu").export_id == ID1  # the tree is at T1, not T2


def test_adopt_needs_only_the_metadata(config: Config) -> None:
    incr = _applied_then_state_lost(config)
    (incr / "data.tar").unlink()

    assert adopt(config, "ubuntu", export_id=ID2).export_id == ID2


def test_adopt_lists_differences_and_writes_nothing(config: Config) -> None:
    _applied_then_state_lost(config)
    root = config.mirror_root / "ubuntu"
    (root / "pool/a.deb").unlink()
    (root / "pool/extra.deb").write_text("x")

    with pytest.raises(PostCheckError) as caught:
        adopt(config, "ubuntu", export_id=ID2)

    assert "missing: pool/a.deb" in str(caught.value)
    assert "extra: pool/extra.deb" in str(caught.value)
    assert not (config.state_dir / "ubuntu.json").exists()


def test_adopt_hash_finds_changed_content(config: Config) -> None:
    _applied_then_state_lost(config)
    path = config.mirror_root / "ubuntu" / "pool/c.deb"
    path.write_text("cCc")  # same size
    os.utime(path, ns=(BASE_MTIME, BASE_MTIME))

    assert adopt(config, "ubuntu", export_id=ID2).export_id == ID2  # quick check passes
    (config.state_dir / "ubuntu.json").unlink()
    with pytest.raises(PostCheckError, match=r"sha256: pool/c\.deb"):
        adopt(config, "ubuntu", export_id=ID2, hash_contents=True)


def test_adopt_refuses_an_existing_state(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0

    assert main([*base, "adopt", "--slug", "ubuntu", "--export-id", ID1]) == 5
    (tmp_path / "st" / "ubuntu.json").unlink()
    assert main([*base, "adopt", "--slug", "ubuntu", "--auto", "--hash"]) == 0
    assert StateStore(tmp_path / "st", "ubuntu").load().last_export_id == ID1


def test_zipapp_runs_with_the_standard_library_alone(tmp_path: Path) -> None:
    """scripts/build_importer.py: one file, reproducible, no application dependencies."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_importer.py"
    archive = tmp_path / "mirrorhub-importer.pyz"
    again = tmp_path / "again.pyz"
    for target in (archive, again):
        subprocess.run([sys.executable, str(script), "--output", str(target)], check=True)

    assert archive.read_bytes() == again.read_bytes()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    sidecar = archive.with_name("mirrorhub-importer.pyz.sha256").read_text(encoding="ascii")
    assert sidecar == f"{digest}  mirrorhub-importer.pyz\n"
    with zipfile.ZipFile(archive) as bundle:
        files = {name for name in bundle.namelist() if not name.endswith("/")}
    assert {"__main__.py", "mirrorhub/__init__.py", "mirrorhub/verify.py"} <= files
    assert "mirrorhub/importer/cli.py" in files
    top_level = {
        "__main__.py",
        "mirrorhub/__init__.py",
        "mirrorhub/verify.py",
        "mirrorhub/buildinfo.py",
        "mirrorhub/_build.py",
    }
    assert all(name in top_level or name.startswith("mirrorhub/importer/") for name in files)

    inbox = tmp_path / "in"
    build_export(inbox, "20261101T000000Z", "full", T1)
    settings = tmp_path / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    # -S: no site-packages, so the installed application cannot be picked up.
    command = [sys.executable, "-S", "-E", str(archive), "--config", str(settings)]
    environment = {"PATH": os.environ.get("PATH", "")}

    version = subprocess.run(
        [*command[:4], "--version"], capture_output=True, text=True, env=environment
    )
    assert version.returncode == 0 and "mirrorhub.importer" in version.stdout
    if "mirrorhub/_build.py" in files:  # built from a git checkout
        head = subprocess.run(
            ["git", "-C", str(script.parents[1]), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert f"({head}" in version.stdout
    result = subprocess.run(
        [*command, "import", "--slug", "ubuntu"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert read_tree(tmp_path / "out" / "ubuntu")["pool/a.deb"] == "aaa"


def _with_sender_commit(directory: Path, commit: str) -> None:
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["tool_versions"] = {"mirrorhub": "0.1.0", "mirrorhub_commit": commit}
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    refresh(directory)


def test_version_mismatch_is_a_warning(
    config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from mirrorhub import buildinfo

    monkeypatch.setenv("MIRRORHUB_COMMIT", "aaaaaaaaaaaa")
    buildinfo.commit.cache_clear()
    try:
        same = build_export(config.export_dir, "20261101T000000Z", "full", T1)
        _with_sender_commit(same, "aaaaaaaaaaaa")
        other = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
        _with_sender_commit(other, "bbbbbbbbbbbb")

        with caplog.at_level("WARNING", logger="mirrorhub.importer"):
            assert import_export(same, config, Options()).status == "applied"
            assert "different commits" not in caplog.text
            assert import_export(other, config, Options()).status == "applied"
        assert "different commits" in caplog.text
    finally:
        buildinfo.commit.cache_clear()


# Detecting changes in the tree (#43): quick check after import, `check`


def test_import_reports_changes_outside_the_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    (tmp_path / "out" / "ubuntu" / "pool/a.deb").unlink()  # untouched by the next export
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    capsys.readouterr()

    assert main([*base, "import", "--slug", "ubuntu"]) == 11

    output = capsys.readouterr().out
    assert "applied 20261102T000000Z-incr" in output
    assert "missing: pool/a.deb" in output
    assert StateStore(tmp_path / "st", "ubuntu").load().last_export_id == ID2


def test_tree_check_can_be_skipped(config: Config) -> None:
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    import_export(full, config, Options())
    (config.mirror_root / "ubuntu" / "pool/a.deb").unlink()
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)

    outcome = import_export(incr, config, Options(tree_check=False))

    assert outcome.status == "applied" and outcome.drift == []


def test_check_finds_deleted_extra_and_changed_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    capsys.readouterr()

    assert main([*base, "check", "--slug", "ubuntu"]) == 0
    assert f"ok ubuntu: 4 entries match {ID2}" in capsys.readouterr().out

    root = tmp_path / "out" / "ubuntu"
    changed = root / "pool/c.deb"
    changed.write_text("cCc")  # same size and mtime: only --hash sees it
    os.utime(changed, ns=(BASE_MTIME, BASE_MTIME))
    assert main([*base, "check", "--slug", "ubuntu"]) == 0
    assert main([*base, "check", "--slug", "ubuntu", "--hash"]) == 11
    assert "sha256: pool/c.deb" in capsys.readouterr().out

    (root / "pool/a.deb").unlink()
    (root / "pool/stray.deb").write_text("x")
    assert main([*base, "check", "--slug", "ubuntu"]) == 11
    output = capsys.readouterr().out
    assert "missing: pool/a.deb" in output and "extra: pool/stray.deb" in output


def test_check_uses_the_local_inventory_copy(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    copies = tmp_path / "st" / "inventories" / "ubuntu"
    assert sorted(path.name for path in copies.iterdir()) == [f"{ID2}.json"]

    shutil.rmtree(inbox / "ubuntu")  # the exports have left the inbox
    assert main([*base, "check", "--slug", "ubuntu"]) == 0


def test_check_falls_back_to_the_inbox_for_older_states(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    copies = tmp_path / "st" / "inventories"
    shutil.rmtree(copies)  # as written by an importer before inventory copies

    assert main([*base, "check", "--slug", "ubuntu"]) == 0
    assert (copies / "ubuntu" / f"{ID1}.json").is_file()

    shutil.rmtree(copies)
    shutil.rmtree(inbox / "ubuntu")
    assert main([*base, "check", "--slug", "ubuntu"]) == 2


def test_check_all_and_missing_state(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1, slug="arch")
    build_export(inbox, "20261101T000000Z", "full", T1, slug="ubuntu")
    assert main([*base, "import", "--all"]) == 0
    assert main([*base, "check", "--slug", "debian"]) == 5

    assert main([*base, "check", "--all"]) == 0
    (tmp_path / "out" / "arch" / "pool/b.deb").unlink()
    assert main([*base, "check", "--all"]) == 11


def test_adopt_writes_the_inventory_copy(config: Config) -> None:
    _applied_then_state_lost(config)
    shutil.rmtree(config.state_dir / "inventories")

    adopt(config, "ubuntu", export_id=ID2)

    assert (config.state_dir / "inventories" / "ubuntu" / f"{ID2}.json").is_file()


REPORT_NAMES = [
    "extra.txt",
    "missing.txt",
    "mtime.txt",
    "sha256.txt",
    "size.txt",
    "summary.txt",
    "symlink-target.txt",
    "type.txt",
]


def test_check_writes_one_file_per_kind(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    reports = tmp_path / "reports"

    assert main([*base, "check", "--slug", "ubuntu", "--output-dir", str(reports)]) == 0
    written = reports / "ubuntu"
    assert sorted(path.name for path in written.iterdir()) == REPORT_NAMES
    lists = [name for name in REPORT_NAMES if name != "summary.txt"]
    assert all((written / name).read_text() == "" for name in lists)
    assert "total=0\n" in (written / "summary.txt").read_text()

    root = tmp_path / "out" / "ubuntu"
    (root / "pool/a.deb").unlink()
    (root / "pool/c.deb").unlink()
    (root / "pool/z.deb").write_text("z")
    (root / "pool/bad\nname.deb").write_text("x")
    grown = root / "dists/Release"
    grown.write_text("r2 and more")
    os.utime(grown, ns=(BASE_MTIME + 5, BASE_MTIME + 5))

    assert main([*base, "check", "--slug", "ubuntu", "--output-dir", str(reports)]) == 11
    assert (written / "missing.txt").read_text() == "pool/a.deb\npool/c.deb\n"
    assert (written / "extra.txt").read_text() == "pool/bad\\nname.deb\npool/z.deb\n"
    assert (written / "size.txt").read_text() == "dists/Release\n"
    assert (written / "mtime.txt").read_text() == ""
    summary = (written / "summary.txt").read_text()
    assert f"export_id={ID2}\n" in summary and "check=quick\n" in summary
    assert "total=5\n" in summary and "missing=2\n" in summary and "extra=2\n" in summary


def test_check_hash_reports_content_besides_quick_differences(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    root = tmp_path / "out" / "ubuntu"
    (root / "pool/a.deb").unlink()
    changed = root / "pool/b.deb"
    changed.write_text("bBb")  # same size and mtime
    os.utime(changed, ns=(BASE_MTIME, BASE_MTIME))
    reports = tmp_path / "reports"

    command = [*base, "check", "--slug", "ubuntu", "--hash", "--output-dir", str(reports)]
    assert main(command) == 11

    assert (reports / "ubuntu" / "missing.txt").read_text() == "pool/a.deb\n"
    assert (reports / "ubuntu" / "sha256.txt").read_text() == "pool/b.deb\n"
    assert "check=quick+sha256\n" in (reports / "ubuntu" / "summary.txt").read_text()


def test_import_writes_drift_files(tmp_path: Path) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    (tmp_path / "out" / "ubuntu" / "pool/a.deb").unlink()
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    reports = tmp_path / "drift"

    assert main([*base, "import", "--slug", "ubuntu", "--drift-output", str(reports)]) == 11

    assert (reports / "ubuntu" / "missing.txt").read_text() == "pool/a.deb\n"
    assert f"export_id={ID2}\n" in (reports / "ubuntu" / "summary.txt").read_text()


# --log-level


def _log_lines(stderr: str) -> list[tuple[str, str]]:
    lines = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    return [(line["level"], line["event"]) for line in lines]


@pytest.mark.parametrize(
    ("level", "expect_debug", "expect_info"),
    [("debug", True, True), ("info", False, True), ("warning", False, False)],
)
def test_log_level_controls_json_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    level: str,
    expect_debug: bool,
    expect_info: bool,
) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)

    assert main([*base, "--log-level", level, "import", "--slug", "ubuntu"]) == 0

    captured = capsys.readouterr()
    lines = _log_lines(captured.err)
    events = {event for _, event in lines}
    assert any(lvl == "debug" for lvl, _ in lines) is expect_debug
    assert ("applied" in events) is expect_info
    if expect_debug:
        assert {"config loaded", "export verified", "plan", "post-check passed"} <= events
    assert f"applied {ID2}" in captured.out  # stdout does not depend on the level


@pytest.mark.parametrize("command", ["import", "status", "check", "adopt"])
def test_log_level_and_config_after_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    if command == "adopt":
        (tmp_path / "st" / "ubuntu.json").unlink()  # adopt needs a lost state file
    capsys.readouterr()
    extra = {
        "import": ["--slug", "ubuntu"],
        "status": [],
        "check": ["--slug", "ubuntu"],
        "adopt": ["--slug", "ubuntu", "--auto"],
    }[command]

    assert main([command, *extra, *base, "--log-level", "DEBUG"]) == 0

    assert ("debug", "config loaded") in _log_lines(capsys.readouterr().err)


def test_log_level_after_the_command_wins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, base = _inbox_cli(tmp_path)
    assert main([*base, "--log-level", "debug", "status", "--log-level", "error"]) == 0
    assert _log_lines(capsys.readouterr().err) == []
    assert main([*base, "--log-level", "debug", "status"]) == 0
    assert ("debug", "config loaded") in _log_lines(capsys.readouterr().err)


# Performance of large first imports: one sync, one read of the archive, metadata
# post-check, one tree walk per repository


def _many_files(count: int) -> Tree:
    tree: Tree = {f"pool/{i % 7}/{i % 3}/p{i}.deb": fnode(f"data {i}") for i in range(count)}
    tree["dists/Release"] = fnode("r")
    return tree


def test_staging_is_synced_once_not_per_file(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", _many_files(60))
    fsyncs = {"count": 0}
    real_fsync = os.fsync

    def counting_fsync(descriptor: int) -> None:
        fsyncs["count"] += 1
        real_fsync(descriptor)

    syncs: list[tuple[bool, bool]] = []
    real_sync = apply_module._sync_filesystem

    def recording_sync(path: Path) -> None:
        staging = config.staging_dir / "ubuntu"
        syncs.append(((staging / "journal.json").exists(), (path / "dists/Release").is_file()))
        real_sync(path)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    monkeypatch.setattr(apply_module, "_sync_filesystem", recording_sync)

    assert import_export(export, config, Options()).status == "applied"

    assert syncs == [(False, True)]  # once, after staging, before the journal
    assert fsyncs["count"] < 10  # journal, inventory copy, state: not one per file


def test_post_check_compares_metadata_and_rehashes_only_with_full_check(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    hashed: list[str] = []
    real_digest = apply_module._file_digest
    real_content = tree_module.content_differences

    def digest(path: Path) -> str:
        hashed.append(path.name)
        return real_digest(path)

    def content(*args: Any, **kwargs: Any) -> list[str]:
        hashed.append("<tree>")
        return real_content(*args, **kwargs)

    monkeypatch.setattr(apply_module, "_file_digest", digest)
    monkeypatch.setattr(tree_module, "content_differences", content)
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)

    import_export(full, config, Options())
    import_export(incr, config, Options())
    assert hashed == []

    third = build_export(config.export_dir, "20261103T000000Z", "incr", T3, T2, base=ID2)
    import_export(third, config, Options(full_check=True))
    assert hashed == ["<tree>"]


def test_full_post_check_finds_a_file_missing_after_apply(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_apply = apply_module.apply_journal

    def losing_apply(target: Path, files: Path, journal: dict[str, Any]) -> None:
        real_apply(target, files, journal)
        (target / "pool/b.deb").unlink()

    monkeypatch.setattr(apply_module, "apply_journal", losing_apply)
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    with pytest.raises(PostCheckError, match=re.escape("missing: pool/b.deb")):
        import_export(export, config, Options())
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id is None


def test_full_check_finds_content_changed_after_apply(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_apply = apply_module.apply_journal

    def corrupting_apply(target: Path, files: Path, journal: dict[str, Any]) -> None:
        real_apply(target, files, journal)
        path = target / "pool/b.deb"
        path.write_text("bXb")  # same size and mtime: only a re-hash sees it
        os.utime(path, ns=(BASE_MTIME, BASE_MTIME))

    monkeypatch.setattr(apply_module, "apply_journal", corrupting_apply)
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    with pytest.raises(PostCheckError, match=re.escape("sha256: pool/b.deb")):
        import_export(export, config, Options(full_check=True))


def test_archive_parts_are_hashed_once_unless_verify_first(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_verify = verify_module.verify

    def counting_verify(directory: Path) -> dict[str, Any]:
        calls.append(directory.name)
        return real_verify(directory)

    monkeypatch.setattr(checks_module, "verify", counting_verify)
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1, split=40)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)

    assert import_export(full, config, Options()).status == "applied"
    assert calls == []
    assert import_export(incr, config, Options(verify_first=True)).status == "applied"
    assert calls == [ID2]


@pytest.mark.parametrize("verify_first", [False, True])
def test_damaged_file_data_is_reported_as_verification_failure(
    config: Config, verify_first: bool
) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    archive = export / "data.tar"
    raw = bytearray(archive.read_bytes())
    offset = raw.index(b"bbb")  # file data, not a tar header
    raw[offset] = ord("x")
    archive.write_bytes(bytes(raw))

    with pytest.raises(
        VerificationFailed, match=r"checksum mismatch for (archive part )?data\.tar"
    ):
        import_export(export, config, Options(verify_first=verify_first))
    assert read_tree(config.mirror_root / "ubuntu") == {}
    assert not (config.staging_dir / "ubuntu").exists()


def test_damaged_metadata_fails_before_extraction(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    with (export / "files.json").open("a") as handle:
        handle.write(" ")
    with pytest.raises(VerificationFailed, match=re.escape("checksum mismatch for files.json")):
        import_export(export, config, Options())
    assert not config.mirror_root.exists()


def test_archive_part_must_be_listed_in_manifest(config: Config) -> None:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    manifest = export / "MANIFEST.sha256"
    lines = manifest.read_text().splitlines(keepends=True)
    manifest.write_text("".join(line for line in lines if not line.endswith("data.tar\n")))
    with pytest.raises(
        VerificationFailed, match=re.escape("not covered by MANIFEST.sha256: data.tar")
    ):
        import_export(export, config, Options())


def test_tree_has_entries_stops_at_the_first_entry(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    assert not apply_module._tree_has_entries(root)
    (root / "a" / "b").mkdir(parents=True)
    (root / ".tmp").mkdir()
    (root / ".tmp" / "partial").write_text("x")
    assert not apply_module._tree_has_entries(root)  # empty dirs and skipped dirs
    (root / "link").symlink_to("a")
    assert apply_module._tree_has_entries(root)
    (root / "link").unlink()
    (root / "a" / "b" / "f").write_text("x")
    assert apply_module._tree_has_entries(root)


def test_symlinked_parent_is_refused_with_directory_cache(config: Config, tmp_path: Path) -> None:
    full = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    import_export(full, config, Options())
    pool = config.mirror_root / "ubuntu" / "pool"
    outside = tmp_path / "outside"
    shutil.move(pool, outside)
    pool.symlink_to(outside)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    with pytest.raises(IoFailure, match="unsafe or conflicting parent"):
        import_export(incr, config, Options())
    assert not (outside / "c.deb").exists()


def test_tree_check_runs_once_after_the_last_pending_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scans = {"count": 0}
    real_scan = tree_module.scan_tree

    def counting_scan(root: Path) -> Any:
        scans["count"] += 1
        return real_scan(root)

    monkeypatch.setattr(tree_module, "scan_tree", counting_scan)
    inbox, base = _inbox_cli(tmp_path)
    build_export(inbox, "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    assert scans["count"] == 1  # the full export's post-check, no extra tree check

    (tmp_path / "out" / "ubuntu" / "pool/a.deb").unlink()  # untouched by what follows
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    build_export(inbox, "20261103T000000Z", "incr", T3, T2, base=ID2)
    capsys.readouterr()
    assert (
        main([*base, "import", "--slug", "ubuntu", "--drift-output", str(tmp_path / "rep")]) == 11
    )

    assert scans["count"] == 2  # once for both incrementals
    output = capsys.readouterr().out
    assert output.count("missing: pool/a.deb") == 1
    assert f"files.json of {ID3}" in output
    assert (tmp_path / "rep" / "ubuntu" / "missing.txt").read_text() == "pool/a.deb\n"
    assert main([*base, "import", "--slug", "ubuntu", "--no-tree-check"]) == 0
    assert scans["count"] == 2
