"""Validation rules: slugs, hosts, paths, architectures, arguments, cron, retry."""

import pytest

from mirrorhub.security import validators as v


def test_slug_rules() -> None:
    assert v.validate_slug("a") is None  # single-character slugs are legal
    assert v.validate_slug("ubuntu-noble") is None
    for bad in ("Ubuntu", "-ubuntu", "ubuntu-", "ubuntu_noble", "", "x" * 64, "a--b-"):
        assert v.validate_slug(bad) is not None


def test_host_rejects_scheme_and_path() -> None:
    assert v.validate_host("archive.ubuntu.com") is None
    assert v.validate_host("mirror.local") is None
    for bad in (
        "https://deb.debian.org",
        "deb.debian.org/debian",
        "deb debian",
        "",
        "-bad.example",
    ):
        assert v.validate_host(bad) is not None


def test_root_blocks_traversal() -> None:
    assert v.validate_root("/debian") is None
    assert v.validate_root("/") is None
    for bad in ("debian", "/debian/../etc", "//debian", "/debian/", ""):
        assert v.validate_root(bad) is not None


def test_architectures() -> None:
    assert v.validate_architectures(["amd64", "arm64"]) is None
    assert v.validate_architectures(["none"]) is None
    assert v.validate_architectures([]) is not None
    assert v.validate_architectures(["none", "amd64"]) is not None
    assert v.validate_architectures(["AMD64"]) is not None


def test_extra_args_allow_list() -> None:
    assert v.validate_extra_args("debmirror", ["--progress", "--verbose"]) is None
    assert v.validate_extra_args("wget", ["--tries=5", "--timeout=30"]) is None
    assert v.validate_extra_args("debmirror", ["--ignore-release-gpg"]) is not None
    assert v.validate_extra_args("rsync", ["--delete-after"]) is not None
    assert v.validate_extra_args("rsync", ["-av"]) is not None
    assert v.validate_extra_args("wget", ["--tries=999"]) is not None
    assert v.validate_extra_args("wget", ["--continue=yes"]) is not None
    assert v.validate_extra_args("debmirror", ["--progress;rm -rf /"]) is not None


def test_cron() -> None:
    assert v.validate_cron("0 3 * * *") is None
    for bad in ("0 3 * *", "61 3 * * *", "not a cron", ""):
        assert v.validate_cron(bad) is not None


@pytest.mark.parametrize(
    "policy",
    [
        {
            "max_attempts": 0,
            "backoff": "fixed",
            "base_seconds": 60,
            "max_seconds": 3600,
            "retryable_exit_codes": [1],
        },
        {
            "max_attempts": 3,
            "backoff": "zigzag",
            "base_seconds": 60,
            "max_seconds": 3600,
            "retryable_exit_codes": [1],
        },
        {
            "max_attempts": 3,
            "backoff": "fixed",
            "base_seconds": 60,
            "max_seconds": 3600,
            "retryable_exit_codes": [],
        },
        {
            "max_attempts": 3,
            "backoff": "fixed",
            "base_seconds": 60,
            "max_seconds": 3600,
            "retryable_exit_codes": [999],
        },
    ],
)
def test_retry_policy_rejects(policy: dict) -> None:
    assert v.validate_retry_policy(policy) is not None


def test_retry_policy_accepts() -> None:
    policy = {
        "max_attempts": 3,
        "backoff": "exponential",
        "base_seconds": 60,
        "max_seconds": 3600,
        "retryable_exit_codes": [1, 2, 255],
    }
    assert v.validate_retry_policy(policy) is None


def test_rel_path_blocks_traversal() -> None:
    assert v.validate_rel_path("keys/archive.asc") is None
    for bad in ("/etc/passwd", "../../etc/passwd", "", "a/../../b"):
        assert v.validate_rel_path(bad) is not None
