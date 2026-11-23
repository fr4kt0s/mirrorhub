"""Architecture filter for wget directory trees: names, validation, generated regex."""

import re

import pytest

from mirrorhub import archfilter

DOCKER = "https://download.docker.com/linux/ubuntu/dists/noble"


@pytest.mark.parametrize(
    ("url", "rejected"),
    [
        (f"{DOCKER}/stable/binary-arm64/Packages", True),
        (f"{DOCKER}/stable/binary-amd64/Packages", False),
        (f"{DOCKER}/stable/binary-i386/Packages", False),
        (f"{DOCKER}/pool/stable/arm64/", True),
        (f"{DOCKER}/pool/stable/ppc64el/containerd.io_1.7_ppc64el.deb", True),
        (f"{DOCKER}/pool/stable/s390x/", True),
        (f"{DOCKER}/pool/stable/amd64/docker-ce_27.0_amd64.deb", False),
        (f"{DOCKER}/Release", False),
        (f"{DOCKER}/InRelease", False),
        ("https://example.org/el9/aarch64/foo-1.0.aarch64.rpm", True),
        ("https://example.org/el9/x86_64/foo-1.0.x86_64.rpm", False),
        ("https://example.org/el9/noarch/foo-1.0.noarch.rpm", False),
        ("https://example.org/pool/main/f/foo/foo_1.0_all.deb", False),
        ("https://example.org/tool-1.0-linux-arm64.tar.gz", True),
        ("https://example.org/tool-1.0-linux-i686.tar.gz", False),
        ("https://example.org/armv7hl/", True),
    ],
)
def test_keep_amd64_and_i386(url: str, rejected: bool) -> None:
    regex = archfilter.reject_regex(["amd64", "i386"])
    assert bool(re.search(regex, url)) is rejected


def test_ppc64_does_not_reject_ppc64el() -> None:
    regex = archfilter.reject_regex(["ppc64el"])
    assert not re.search(regex, f"{DOCKER}/stable/binary-ppc64el/Packages")
    assert re.search(regex, "https://example.org/ppc64/foo.rpm")


def test_empty_keep_list_keeps_everything() -> None:
    assert archfilter.reject_regex([]) == ""
    assert archfilter.combine_reject("", []) == ""
    assert archfilter.combine_reject(r"\.sig$", []) == r"\.sig$"


def test_aliases_and_case() -> None:
    assert archfilter.canonical("x86_64") == "amd64"
    assert archfilter.canonical("AArch64") == "arm64"
    assert archfilter.reject_regex(["x86_64"]) == archfilter.reject_regex(["amd64"])


def test_combined_with_user_regex() -> None:
    combined = archfilter.combine_reject(r"\.sig$", ["amd64"])
    assert combined.startswith(r"(\.sig$)|(")
    assert re.search(combined, "https://example.org/a.sig")
    assert re.search(combined, "https://example.org/arm64/a.deb")
    assert not re.search(combined, "https://example.org/amd64/a.deb")


def test_validate_keep() -> None:
    assert archfilter.validate_keep([]) is None
    assert archfilter.validate_keep(["amd64", "i686"]) is None
    error = archfilter.validate_keep(["amd64", "sparc"])
    assert error is not None
    assert "unknown 'sparc'" in error
    assert archfilter.validate_keep(list(archfilter.ARCH_GROUPS)) is not None
