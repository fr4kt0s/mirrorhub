"""mirrorhub.buildinfo: which commit the code was built from."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from mirrorhub import buildinfo

SHA = "9d24408606096fe33a6bdff72856837377413324"
OTHER = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    buildinfo.commit.cache_clear()
    yield
    buildinfo.commit.cache_clear()


def test_detached_head(tmp_path: Path) -> None:
    (tmp_path / "HEAD").write_text(f"{SHA}\n")
    assert buildinfo.resolve_head(tmp_path) == SHA


def test_branch_ref_file(tmp_path: Path) -> None:
    (tmp_path / "HEAD").write_text("ref: refs/heads/master\n")
    (tmp_path / "refs/heads").mkdir(parents=True)
    (tmp_path / "refs/heads/master").write_text(f"{SHA}\n")
    assert buildinfo.resolve_head(tmp_path) == SHA


def test_packed_refs(tmp_path: Path) -> None:
    (tmp_path / "HEAD").write_text("ref: refs/heads/master\n")
    (tmp_path / "packed-refs").write_text(
        f"# pack-refs with: peeled fully-peeled sorted\n{OTHER} refs/heads/other\n"
        f"{SHA} refs/heads/master\n"
    )
    assert buildinfo.resolve_head(tmp_path) == SHA


@pytest.mark.parametrize(
    "head",
    ["", "garbage\n", "ref: refs/heads/../../etc/passwd\n", "ref: refs/tags/v1\n"],
)
def test_unusable_head_gives_none(tmp_path: Path, head: str) -> None:
    (tmp_path / "HEAD").write_text(head)
    assert buildinfo.resolve_head(tmp_path) is None


def test_missing_git_directory(tmp_path: Path) -> None:
    assert buildinfo.resolve_head(tmp_path / "nothing") is None


def test_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRRORHUB_COMMIT", "abc123def456")
    assert buildinfo.commit() == "abc123def456"
    assert buildinfo.describe().endswith("(abc123def456)")


def test_source_checkout_reports_its_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIRRORHUB_COMMIT", raising=False)
    root = Path(buildinfo.__file__).resolve().parents[2]
    try:
        expected = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    assert buildinfo.commit() == expected


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("7a3f3b243299", "7a3f3b2", True),  # GitHub short id vs. .git in the image
        ("7a3f3b2", "7a3f3b24329c995524abdf53308c385e97854784", True),
        ("7A3F3B2", "7a3f3b243299", True),
        ("7a3f3b243299", "7a3f3b343299", False),
        ("7a3f3b243299-dirty", "7a3f3b2-dirty", True),
        ("7a3f3b243299-dirty", "7a3f3b243299", False),  # local changes differ
        ("7a3f3", "7a3f3b243299", False),  # too short to compare by prefix
        ("7a3f3", "7a3f3", True),
    ],
)
def test_same_commit(first: str, second: str, same: bool) -> None:
    assert buildinfo.same_commit(first, second) is same
    assert buildinfo.same_commit(second, first) is same
