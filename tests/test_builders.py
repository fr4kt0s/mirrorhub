"""Command builders: argument lists, no shell, dry-run targets, path containment.

No database is involved: builders are pure functions over model objects, so the repository
and job are plain instances with no session behind them.
"""

from pathlib import Path

import pytest

from mirrorhub.models import Job, Repository
from mirrorhub.settings import get_settings
from mirrorhub.worker import builders
from mirrorhub.worker.builders import BuildError
from mirrorhub.worker.progress import ProgressParser


def _repo(**overrides: object) -> Repository:
    values: dict[str, object] = {
        "slug": "debian",
        "name": "Debian",
        "kind": "apt",
        "tool": "debmirror",
        "method": "https",
        "upstream_host": "deb.debian.org",
        "upstream_root": "/debian",
        "suites": ["bookworm"],
        "sections": ["main"],
        "architectures": ["amd64"],
    }
    values.update(overrides)
    return Repository(**values)


def _job(repo: Repository, *, dry_run: bool = False, job_id: int = 1) -> Job:
    return Job(id=job_id, repo_id=1, tool=repo.tool, dry_run=dry_run)


def test_debmirror_passes_arch_suites_and_sections(configured) -> None:
    repo = _repo(architectures=["amd64", "arm64"], suites=["bookworm", "bookworm-updates"])
    plan = builders.build(repo, _job(repo), get_settings())
    argv = list(plan.steps[0].argv)
    assert argv[0] == "debmirror"
    assert "--arch=amd64,arm64" in argv
    assert "--dist=bookworm,bookworm-updates" in argv
    assert "--section=main" in argv
    assert "--nosource" in argv
    assert "--source" not in argv
    assert "--diff=use" in argv
    assert "--progress" in argv and "--verbose" in argv
    # --postcleanup is the debmirror default and must never be passed explicitly
    assert "--postcleanup" not in argv
    assert argv[-1] == str(plan.target)


def test_debmirror_source_and_cleanup_modes(configured) -> None:
    repo = _repo(include_source=True, cleanup_mode="none")
    argv = list(builders.build(repo, _job(repo), get_settings()).steps[0].argv)
    assert "--source" in argv
    assert "--nosource" not in argv
    assert "--nocleanup" in argv


def test_debmirror_architecture_none_is_explicit(configured) -> None:
    repo = _repo(architectures=["none"])
    argv = list(builders.build(repo, _job(repo), get_settings()).steps[0].argv)
    assert "--arch=none" in argv


def test_debmirror_requires_architectures(configured) -> None:
    repo = _repo(architectures=[])
    with pytest.raises(BuildError):
        builders.build(repo, _job(repo), get_settings())


def test_debmirror_dry_run_uses_state_volume(configured) -> None:
    repo = _repo()
    job = _job(repo, dry_run=True)
    plan = builders.build(repo, job, get_settings())
    assert "--dry-run" in plan.steps[0].argv
    settings = get_settings()
    assert settings.tmp_dir in plan.target.parents
    assert settings.data_dir not in plan.target.parents


def test_signature_disable_adds_ignore_flag(configured) -> None:
    repo = _repo(verify_signatures=False)
    argv = list(builders.build(repo, _job(repo), get_settings()).steps[0].argv)
    assert "--ignore-release-gpg" in argv


def test_arch_rsync_two_passes_with_database_last(configured) -> None:
    repo = _repo(
        slug="archlinux",
        kind="arch",
        tool="rsync",
        method="rsync",
        upstream_host="geo.mirror.pkgbuild.com",
        upstream_root="/",
        suites=["core"],
        sections=[],
        architectures=["x86_64"],
    )
    plan = builders.build(repo, _job(repo), get_settings())
    assert len(plan.steps) == 2
    packages, databases = plan.steps
    assert packages.argv[0] == "rsync"
    assert "rsync://geo.mirror.pkgbuild.com/core/os/x86_64/" in packages.argv
    assert "--delete-after" in packages.argv
    assert "--exclude=*.db" in packages.argv
    assert "--delay-updates" in databases.argv
    assert "--delete" in databases.argv
    assert "--exclude=*" in databases.argv


def test_rsync_requires_module(configured) -> None:
    repo = _repo(
        slug="archlinux",
        kind="arch",
        tool="rsync",
        method="rsync",
        upstream_root="/",
        suites=["core"],
        sections=[],
        architectures=["x86_64"],
    )
    with pytest.raises(BuildError):
        builders.build(repo, _job(repo), get_settings())


def test_rsync_dry_run_flag(configured) -> None:
    repo = _repo(slug="generic", kind="generic", tool="rsync", method="rsync")
    plan = builders.build(repo, _job(repo, dry_run=True), get_settings())
    assert "--dry-run" in plan.steps[0].argv


def test_wget_limit_rate_and_destination(configured) -> None:
    repo = _repo(
        slug="files",
        kind="generic",
        tool="wget",
        method="https",
        bwlimit_kbps=500,
        suites=[],
        sections=[],
        extra_files=[{"url": "https://example.org/archive.asc", "path": "keys/archive.asc"}],
    )
    plan = builders.build(repo, _job(repo), get_settings())
    argv = list(plan.steps[0].argv)
    assert argv[0] == "wget"
    assert "--limit-rate=500k" in argv
    assert "-O" in argv
    assert argv[-1] == "https://example.org/archive.asc"
    assert (plan.target / "keys").is_dir()


def test_wget_rejects_path_traversal(configured) -> None:
    repo = _repo(
        slug="files",
        kind="generic",
        tool="wget",
        extra_files=[{"url": "https://example.org/x", "path": "../escape"}],
    )
    with pytest.raises(BuildError):
        builders.build(repo, _job(repo), get_settings())


def test_progress_parser_reads_percentages() -> None:
    parser = ProgressParser()
    pct, text = parser.feed("  1,234,567  45%   12.34MB/s    0:00:05")
    assert pct == 45.0
    assert "45%" in text
    pct, _ = parser.feed("Getting: pool/main/a/apt/apt_2.6_amd64.deb")
    assert pct == 45.0
    pct, _ = parser.feed("  99% done")
    assert pct == 99.0


def test_no_step_uses_a_shell(configured) -> None:
    repo = _repo()
    plan = builders.build(repo, _job(repo), get_settings())
    for step in plan.steps:
        assert isinstance(step.argv, tuple)
        assert all(isinstance(part, str) for part in step.argv)
        assert not any((";" in part or "|" in part or "&&" in part) for part in step.argv)
        assert Path(step.argv[0]).name == step.argv[0]
