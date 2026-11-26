"""#43 on the receiver: repair requests written with reports, repair exports applied."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from mirrorhub.importer import repair
from mirrorhub.importer.cli import main
from mirrorhub.importer.state import StateStore
from test_importer import BASE_MTIME, ID1, T1, T2, build_export, fnode, inventory_of, read_tree

REPAIR = {"request_sha256": "0" * 64, "requested_at": "2026-11-26T09:00:00+00:00"}


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, list[str]]:
    settings = tmp_path / "importer.toml"
    settings.write_text(
        'export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\nreports_dir = "rep"\n'
    )
    return tmp_path, ["--config", str(settings)]


def _first_import(tmp_path: Path, base: list[str]) -> Path:
    build_export(tmp_path / "in", "20261101T000000Z", "full", T1)
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    return tmp_path / "out" / "ubuntu"


def _damage(tree: Path) -> None:
    (tree / "pool/a.deb").unlink()  # missing
    (tree / "pool/b.deb").write_text("bbbb")  # size
    (tree / "pool/x.deb").write_text("x")  # extra


def _request(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "rep" / "ubuntu" / "repair-ubuntu.json"
    data = path.read_bytes()
    line = (path.parent / "repair-ubuntu.json.sha256").read_text()
    assert line == f"{hashlib.sha256(data).hexdigest()}  repair-ubuntu.json\n"  # sha256sum -c
    document: dict[str, Any] = json.loads(data)
    return document


def test_check_writes_a_request_and_removes_it_once_the_tree_matches(
    setup: tuple[Path, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path, base = setup
    tree = _first_import(tmp_path, base)
    assert not (tmp_path / "rep" / "ubuntu" / "repair-ubuntu.json").exists()
    _damage(tree)
    capsys.readouterr()

    assert main([*base, "check", "--slug", "ubuntu"]) == 11
    out = capsys.readouterr().out
    assert "repair request (2 to resend, 1 to delete)" in out and "Repair export" in out
    document = _request(tmp_path)
    assert document["format"] == "mirrorhub-repair-request" and document["version"] == 1
    assert document["slug"] == "ubuntu" and document["base_export_id"] == ID1
    assert document["resend"] == ["pool/a.deb", "pool/b.deb"]
    assert document["delete"] == ["pool/x.deb"]
    assert document["check"] == "quick" and document["skipped"] == 0

    (tree / "pool/a.deb").write_text("aaa")
    (tree / "pool/b.deb").write_text("bbb")
    for name in ("pool/a.deb", "pool/b.deb"):
        os.utime(tree / name, ns=(BASE_MTIME, BASE_MTIME))
    (tree / "pool/x.deb").unlink()
    assert main([*base, "check", "--slug", "ubuntu"]) == 0
    assert not list((tmp_path / "rep" / "ubuntu").glob("repair-*"))


def test_hash_check_requests_damaged_contents(setup: tuple[Path, list[str]]) -> None:
    tmp_path, base = setup
    tree = _first_import(tmp_path, base)
    path = tree / "pool/a.deb"
    stat = path.stat()
    path.write_text("AAA")  # same size
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert main([*base, "check", "--slug", "ubuntu"]) == 0  # quick check sees nothing
    assert main([*base, "check", "--slug", "ubuntu", "--hash"]) == 11
    document = _request(tmp_path)
    assert document["resend"] == ["pool/a.deb"] and document["check"] == "quick+sha256"


def test_paths_that_cannot_travel_are_counted_not_requested() -> None:
    document = repair.build_request(
        "ubuntu",
        ID1,
        ["extra: pool/bad\nname", "extra: pool/caf\udce9.deb", "extra: pool/ok.deb"],
        hashed=False,
    )
    assert document is not None
    assert document["delete"] == ["pool/ok.deb"] and document["skipped"] == 2
    assert "2 not transferable" in repair.count(document)
    assert repair.build_request("ubuntu", ID1, [], hashed=False) is None


def test_a_repair_export_restores_the_tree(
    setup: tuple[Path, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path, base = setup
    tree = _first_import(tmp_path, base)
    _damage(tree)
    assert main([*base, "check", "--slug", "ubuntu"]) == 11
    request = _request(tmp_path)
    capsys.readouterr()

    # the sender's answer: incremental since ID1 (nothing changed there) plus the request
    build_export(
        tmp_path / "in",
        "20261103T000000Z",
        "incr",
        T1,
        previous=T1,
        base=ID1,
        resend=tuple(request["resend"]),
        delete=tuple(request["delete"]),
        repair=REPAIR,
    )
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    out = capsys.readouterr().out
    assert "applied 20261103T000000Z-incr (repair export, whole tree checked)" in out
    assert "1 added, 1 changed, 1 deleted" in out
    assert read_tree(tree) == read_tree_of(T1, tmp_path / "expected")
    assert not list((tmp_path / "rep" / "ubuntu").glob("repair-*"))
    state = StateStore(tmp_path / "st", "ubuntu").load()
    assert state.history[-1]["repair"] is True
    assert main([*base, "check", "--slug", "ubuntu"]) == 0


def read_tree_of(source: dict[str, Any], scratch: Path) -> dict[str, str]:
    """The tree an import of `source` produces, for comparison."""
    build_export(scratch / "in", "20261101T000000Z", "full", source)
    settings = scratch / "importer.toml"
    settings.write_text('export_dir = "in"\nmirror_root = "out"\nstate_dir = "st"\n')
    assert main(["--config", str(settings), "import", "--slug", "ubuntu"]) == 0
    return read_tree(scratch / "out" / "ubuntu")


def test_repair_with_changes_since_the_base_and_kept_extra_files(
    setup: tuple[Path, list[str]],
) -> None:
    tmp_path, base = setup
    tree = _first_import(tmp_path, base)
    _damage(tree)
    assert main([*base, "check", "--slug", "ubuntu"]) == 11
    request = _request(tmp_path)

    # the sender moved on to T2 meanwhile; the operator kept the extra file
    build_export(
        tmp_path / "in",
        "20261103T000000Z",
        "incr",
        T2,
        previous=T1,
        base=ID1,
        resend=tuple(request["resend"]),
        repair={**REPAIR, "keep_extra": True},
    )
    assert main([*base, "import", "--slug", "ubuntu"]) == 11  # the kept extra file differs
    result = read_tree(tree)
    assert result["pool/a.deb"] == "aaa" and result["pool/c.deb"] == "ccc"
    assert "pool/b.deb" not in result and result["pool/x.deb"] == "x"
    left = _request(tmp_path)  # the next request only asks to delete the kept file
    assert left["base_export_id"] == "20261103T000000Z-incr"
    assert left["resend"] == [] and left["delete"] == ["pool/x.deb"]
    assert (tmp_path / "rep" / "ubuntu" / "extra.txt").read_text() == "pool/x.deb\n"


def test_a_pending_export_on_the_same_base_is_superseded_by_the_repair(
    setup: tuple[Path, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path, base = setup
    tree = _first_import(tmp_path, base)
    _damage(tree)
    build_export(tmp_path / "in", "20261102T000000Z", "incr", T2, previous=T1, base=ID1)
    build_export(
        tmp_path / "in",
        "20261103T000000Z",
        "incr",
        T2,
        previous=T1,
        base=ID1,
        resend=("pool/a.deb", "pool/b.deb"),
        delete=("pool/x.deb",),
        repair=REPAIR,
    )
    capsys.readouterr()
    assert main([*base, "import", "--slug", "ubuntu"]) == 0
    out = capsys.readouterr().out
    assert "superseded 20261102T000000Z-incr" in out and "repair export" in out
    assert set(read_tree(tree)) == set(inventory_of(T2))


def test_the_whole_tree_must_match_after_a_repair(
    setup: tuple[Path, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_path, base = setup
    tree = _first_import(tmp_path, base)
    _damage(tree)
    # a repair export that forgot pool/a.deb: the post-check of the whole tree fails
    build_export(
        tmp_path / "in",
        "20261103T000000Z",
        "incr",
        T1,
        previous=T1,
        base=ID1,
        resend=("pool/b.deb",),
        delete=("pool/x.deb",),
        repair=REPAIR,
    )
    assert main([*base, "import", "--slug", "ubuntu"]) == 10
    assert "missing: pool/a.deb" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["yes", ["x"]])
def test_a_malformed_repair_field_is_refused(setup: tuple[Path, list[str]], value: object) -> None:
    tmp_path, base = setup
    _first_import(tmp_path, base)
    build_export(
        tmp_path / "in",
        "20261103T000000Z",
        "incr",
        {**T1, "pool/n.deb": fnode("n")},
        previous=T1,
        base=ID1,
        repair=value,
    )
    assert main([*base, "import", "--slug", "ubuntu"]) == 3
