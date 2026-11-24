"""wget builder for complete HTTP directory-tree mirrors and extra files."""

from __future__ import annotations

from pathlib import Path

from mirrorhub import archfilter, wgetcmd
from mirrorhub.models import Job, Repository
from mirrorhub.security import validators

from .base import BuildError, BuildPlan, Step


def _safe_destination(target: Path, relative_path: str) -> Path:
    if not relative_path or relative_path.startswith("/"):
        raise BuildError("extra file path must be relative to the mirror target")

    destination = (target / relative_path).resolve()
    try:
        destination.relative_to(target.resolve())
    except ValueError as exc:
        raise BuildError("extra file path escapes the mirror target") from exc
    return destination


def build(repo: Repository, job: Job, target: Path, cwd: Path) -> BuildPlan:
    error = validators.validate_host(repo.upstream_host) or validators.validate_root(
        repo.upstream_root
    )
    if error:
        raise BuildError(error)
    if repo.method not in ("http", "https"):
        raise BuildError("wget supports http and https only")
    error = validators.validate_extra_args("wget", repo.extra_args)
    if error:
        raise BuildError(error)

    if repo.extra_files:
        error = validators.validate_extra_files(repo.extra_files)
        if error:
            raise BuildError(error)

        steps: list[Step] = []
        for entry in repo.extra_files:
            url = entry.get("url", "").strip()
            relative_path = entry.get("path", "").strip()
            if not url or not relative_path:
                raise BuildError("extra files require both a URL and a relative path")

            destination = _safe_destination(target, relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)

            argv = [
                "wget",
                "--no-verbose",
                "--tries=3",
                "--timeout=60",
            ]
            if repo.bwlimit_kbps:
                argv.append(f"--limit-rate={repo.bwlimit_kbps}k")
            argv.extend(repo.extra_args)
            if job.dry_run:
                argv.append("--spider")
            argv.extend(["-O", str(destination), url])

            steps.append(Step(tuple(argv), f"download {url}"))

        return BuildPlan(steps=steps, cwd=cwd, target=target)

    url = wgetcmd.tree_url(repo.method, repo.upstream_host, repo.upstream_root)
    custom_args = list(repo.wget_custom_args or [])
    if custom_args:
        error = wgetcmd.validate_args(custom_args)
        if error:
            raise BuildError(error)
        options = custom_args
    else:
        error = archfilter.validate_keep(list(repo.wget_keep_arches or []))
        if error:
            raise BuildError(error)
        options = wgetcmd.generated_args(
            upstream_root=repo.upstream_root,
            include_regex=repo.include_regex,
            exclude_regex=repo.exclude_regex,
            keep_arches=list(repo.wget_keep_arches or []),
            bwlimit_kbps=repo.bwlimit_kbps,
            extra_args=repo.extra_args,
        )
    argv = wgetcmd.full_argv(options, target=str(target), url=url, dry_run=job.dry_run)

    tree_steps: list[Step] = []
    # full_argv adds --timestamping to a sync: only files that changed upstream are fetched.
    if not job.dry_run and target.is_dir():
        # With --timestamping wget neither re-downloads nor re-parses an unchanged
        # directory listing, so the walk would stop at the first stored index.html
        # and new files below it would never be found. Listings are small: drop
        # them so every run reads them fresh; files keep their timestamps.
        tree_steps.append(
            Step(
                ("find", str(target), "-type", "f", "-name", "index.html", "-delete"),
                "drop stored directory listings",
            )
        )
    tree_steps.append(Step(tuple(argv), f"mirror tree {repo.upstream_host}{repo.upstream_root}"))

    return BuildPlan(
        steps=tree_steps,
        cwd=cwd,
        target=target,
    )
