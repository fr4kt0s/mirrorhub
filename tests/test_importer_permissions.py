"""Issue #60: permission check before changing the tree, clear messages, warnings."""

from __future__ import annotations

import os
import pwd
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mirrorhub.importer import permissions, tree
from mirrorhub.importer.apply import Options, import_export
from mirrorhub.importer.cli import main
from mirrorhub.importer.config import Config
from mirrorhub.importer.permissions import PermissionFailure
from mirrorhub.importer.state import StateStore
from test_importer import ID1, ID2, T1, T2, Tree, build_export, config, fnode, read_tree

__all__ = ["config"]  # fixture from test_importer

ROOT = os.geteuid() == 0


def _deny(monkeypatch: pytest.MonkeyPatch, *denied: Path) -> None:
    """The running user may not change these directories (works as root, too)."""
    blocked = {path.resolve() for path in denied}

    def writable(directory: Path) -> bool:
        return directory.resolve() not in blocked

    monkeypatch.setattr(permissions, "writable", writable)


def _full(config: Config) -> Path:
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    assert import_export(export, config, Options()).status == "applied"
    return export


def _snapshot(config: Config) -> dict[str, str]:
    return read_tree(config.mirror_root / "ubuntu")


def test_preflight_stops_before_anything_changes(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _full(config)
    before = _snapshot(config)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    pool = config.mirror_root / "ubuntu" / "pool"
    _deny(monkeypatch, pool)

    with pytest.raises(PermissionFailure) as raised:
        import_export(incr, config, Options())

    message = str(raised.value)
    assert raised.value.exit_code == 9
    assert "permission check failed" in message and "nothing was changed" in message
    assert f"{pool}: owner " in message and "delete files" in message
    assert "e.g. pool/b.deb" in message and "e.g. pool/c.deb" in message
    assert "chown -R" in message and str(config.mirror_root / "ubuntu") in message
    assert _snapshot(config) == before
    assert not (config.staging_dir / "ubuntu").exists()
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id == ID1

    monkeypatch.undo()
    assert import_export(incr, config, Options()).status == "applied"


def test_new_directories_need_the_nearest_existing_parent(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _full(config)
    tree: Tree = {**T1, "pool/main/n/new.deb": fnode("new")}
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", tree, T1, base=ID1)
    _deny(monkeypatch, config.mirror_root / "ubuntu" / "pool")
    with pytest.raises(PermissionFailure, match="create directories") as raised:
        import_export(incr, config, Options())
    assert "e.g. pool/main/n/new.deb" in str(raised.value)


def test_dry_run_and_staging_directory_are_checked(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _full(config)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    _deny(monkeypatch, config.mirror_root / "ubuntu" / "pool")
    with pytest.raises(PermissionFailure, match=r"^dry run: permission check failed"):
        import_export(incr, config, Options(dry_run=True))

    _deny(monkeypatch, config.staging_dir)
    config.staging_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(PermissionFailure, match="create the staging directory"):
        import_export(incr, config, Options())


def test_a_late_permission_error_explains_how_to_resume(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _full(config)
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    real_replace = os.replace

    def replace(source: Any, destination: Any) -> None:
        if str(destination).endswith("pool/c.deb"):
            raise PermissionError(13, "Permission denied", str(destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(PermissionFailure) as raised:
        import_export(incr, config, Options())
    message = str(raised.value)
    assert "permission denied while installing into the mirror tree" in message
    assert "running as " in message and "directory " in message
    assert "partly updated" in message and "run the same import again" in message
    assert (config.staging_dir / "ubuntu" / "journal.json").is_file()  # resumable
    monkeypatch.undo()

    _deny(monkeypatch, config.mirror_root / "ubuntu" / "pool")
    with pytest.raises(PermissionFailure, match="permission check failed"):
        import_export(incr, config, Options())  # resuming is checked as well
    monkeypatch.undo()

    assert import_export(incr, config, Options()).status == "applied"
    assert read_tree(config.mirror_root / "ubuntu")["pool/c.deb"] == "ccc"
    assert not (config.staging_dir / "ubuntu").exists()


def test_staging_leftovers_that_cannot_be_removed_are_reported(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    leftover = config.staging_dir / "ubuntu" / "files" / "pool"
    leftover.mkdir(parents=True)
    (leftover / "old.deb").write_text("x")
    export = build_export(config.export_dir, "20261101T000000Z", "full", T1)
    real_rmtree = shutil.rmtree

    def rmtree(path: Any, *args: Any, onexc: Callable[..., Any] | None = None, **kw: Any) -> None:
        if onexc is None:
            real_rmtree(path, *args, **kw)
            return
        onexc(os.unlink, str(leftover / "old.deb"), PermissionError(13, "Permission denied"))

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with pytest.raises(PermissionFailure) as raised:
        import_export(export, config, Options())
    message = str(raised.value)
    assert "clearing the staging directory of an earlier run" in message
    assert str(leftover / "old.deb") in message
    assert f"sudo rm -rf {config.staging_dir / 'ubuntu'}" in message

    monkeypatch.undo()
    assert import_export(export, config, Options()).status == "applied"  # removable again


def test_owner_without_a_user_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_user(uid: int) -> Any:
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", no_user)
    described = permissions.describe(tmp_path)
    assert "(no user on this host), d" in described
    assert permissions.who() == f"uid {os.geteuid()}"


def test_check_and_adopt_warn_about_directories_this_user_cannot_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = tmp_path / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    build_export(tmp_path / "in", "20261101T000000Z", "full", T1)
    base = ["--config", str(settings)]
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    assert main([*base, "check", "--slug", "ubuntu"]) == 0
    assert "warning" not in capsys.readouterr().out

    _deny(monkeypatch, tmp_path / "out" / "ubuntu" / "pool")

    def no_second_walk(root: Path) -> Any:
        raise AssertionError(f"tree listed a second time: {root}")

    # the warning reuses the directories of the tree scan instead of listing again
    monkeypatch.setattr(permissions, "_walk", no_second_walk)
    assert main([*base, "check", "--slug", "ubuntu"]) == 0  # a warning, no failure
    out = capsys.readouterr().out
    assert "cannot change 1 directory" in out and f"{tmp_path / 'out' / 'ubuntu' / 'pool'}:" in out
    assert "fix: sudo chown -R" in out

    (tmp_path / "st" / "ubuntu.json").unlink()
    assert main([*base, "adopt", "--slug", "ubuntu", "--auto"]) == 0
    assert "cannot change 1 directory" in capsys.readouterr().out


def test_tree_scan_hands_over_its_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    (root / "pool" / "main").mkdir(parents=True)
    (root / "dists").mkdir()
    (root / ".mirrorhub").mkdir()  # skipped like in the scan
    (root / "link").symlink_to(root / "pool")  # not followed
    (root / "pool" / "main" / "a.deb").write_text("a")
    walked: list[Path] = []
    entries = tree.scan_tree(root, directories=walked)
    assert "pool/main/a.deb" in entries
    assert sorted(walked) == sorted([root, root / "pool", root / "pool" / "main", root / "dists"])
    assert permissions.unwritable_dirs(root) == (0, [])
    _deny(monkeypatch, root / "pool" / "main")
    assert permissions.unwritable_dirs(root) == permissions.unwritable_dirs(
        root, directories=walked
    )
    assert permissions.unwritable_dirs(root, directories=walked) == (1, [root / "pool" / "main"])


def test_cli_exit_code_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = tmp_path / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    inbox = tmp_path / "in"
    build_export(inbox, "20261101T000000Z", "full", T1)
    base = ["--config", str(settings)]
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    build_export(inbox, "20261102T000000Z", "incr", T2, T1, base=ID1)
    _deny(monkeypatch, tmp_path / "out" / "ubuntu" / "pool")
    capsys.readouterr()
    assert main([*base, "import", "--slug", "ubuntu"]) == 9
    err = capsys.readouterr().err
    assert "error: permission check failed" in err and "nothing was changed" in err
    assert StateStore(tmp_path / "st", "ubuntu").load().last_export_id == ID1


@pytest.mark.skipif(ROOT, reason="root may write everywhere; runs in CI as a normal user")
def test_real_read_only_directory(config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    _full(config)
    pool = config.mirror_root / "ubuntu" / "pool"
    incr = build_export(config.export_dir, "20261102T000000Z", "incr", T2, T1, base=ID1)
    pool.chmod(0o555)
    try:
        before = _snapshot(config)
        with pytest.raises(PermissionFailure, match="delete files"):
            import_export(incr, config, Options())
        assert _snapshot(config) == before
    finally:
        pool.chmod(0o755)
    assert import_export(incr, config, Options()).status == "applied"
    assert StateStore(config.state_dir, "ubuntu").load().last_export_id == ID2
