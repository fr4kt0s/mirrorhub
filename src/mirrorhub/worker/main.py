"""Worker loop for sync and export jobs."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import logging
import os
import secrets
import signal
import socket
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import FrameType

from sqlmodel import Session

from mirrorhub.db import get_engine, immediate_session, wait_for_schema
from mirrorhub.enums import Tool
from mirrorhub.export import job as export_engine
from mirrorhub.export.manifest import ExportError
from mirrorhub.healthcheck import WORKER_ALIVE_FILE
from mirrorhub.logging_conf import configure_logging
from mirrorhub.models import Job, RepoLock, Repository, utcnow
from mirrorhub.queue import claim
from mirrorhub.queue import retry as retry_policy
from mirrorhub.queue.claim import ClaimedJob
from mirrorhub.security.validators import validate_slug
from mirrorhub.services import audit, export_jobs, logs
from mirrorhub.settings import Settings, get_settings
from mirrorhub.sync.adapters import DebmirrorAdapter, FtpsyncAdapter, RsyncAdapter
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.factory import build_sync_plan
from mirrorhub.sync.plan import DebmirrorPlan, FtpsyncPlan, RsyncPlan
from mirrorhub.sync.result import SyncResult, SyncStatus
from mirrorhub.worker import builders
from mirrorhub.worker.runner import Runner

log = logging.getLogger("mirrorhub.worker")
SYNC_TOOLS = (Tool.FTPSYNC, Tool.RSYNC, Tool.DEBMIRROR, Tool.WGET)
TOOLS = ("debmirror", "rsync", "wget", "ftpsync", "export")
POLL_SECONDS = 3.0


class LeaseLost(RuntimeError):
    """The database no longer assigns the job to this worker."""


class ExportCancelled(RuntimeError):
    """An operator cancelled an export before publication."""


@contextlib.contextmanager
def repository_guard(settings: Settings, repo_id: int) -> Iterator[bool]:
    directory = settings.state_dir / "repo-guards"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{repo_id}.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Lease:
    """Maintain queue ownership independently of tool output."""

    def __init__(
        self,
        job_id: int,
        worker_id: str,
        settings: Settings,
        stopping: threading.Event,
    ) -> None:
        self.job_id = job_id
        self.worker_id = worker_id
        self.settings = settings
        self.stopping = stopping
        self.lost = threading.Event()
        self.closed = threading.Event()
        self.publication_started = False
        self._progress_lock = threading.Lock()
        self._pct: float | None = None
        self._text = ""
        self._last_cancel_check = 0.0
        self._cancelled = False
        self.thread = threading.Thread(
            target=self._loop,
            name=f"job-{job_id}-lease",
            daemon=True,
        )

    def _loop(self) -> None:
        while not self.closed.is_set():
            try:
                with self._progress_lock:
                    pct, text = self._pct, self._text
                owned = claim.heartbeat(
                    self.job_id,
                    self.worker_id,
                    progress_pct=pct,
                    progress_text=text,
                )
                if not owned:
                    self.lost.set()
                    return
            except Exception:
                log.exception("job heartbeat failed")
                self.lost.set()
                return

            self.closed.wait(self.settings.heartbeat_interval_s)

    def cancelled(self) -> bool:
        if self.lost.is_set():
            return True
        if self.publication_started:
            return False
        if self.stopping.is_set():
            return True

        now = time.monotonic()
        if now - self._last_cancel_check >= 1:
            self._last_cancel_check = now
            self._cancelled = claim.should_cancel(self.job_id, self.worker_id)
        return self._cancelled

    def update(self, pct: float | None, text: str) -> None:
        if self.lost.is_set():
            raise LeaseLost("job ownership was lost")
        with self._progress_lock:
            self._pct = pct
            self._text = text[:500]

    def export_pulse(self, pct: float | None, text: str) -> None:
        self.update(pct, text)
        if self.cancelled():
            raise ExportCancelled("export cancelled before publication")
        if pct is not None and pct >= 98:
            self.publication_started = True

    def __enter__(self) -> Lease:
        if not claim.heartbeat(self.job_id, self.worker_id):
            raise LeaseLost("job ownership was lost before execution")
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.closed.set()
        self.thread.join(timeout=self.settings.heartbeat_interval_s + 35)


def _load(claimed: ClaimedJob) -> tuple[Job, Repository]:
    with Session(get_engine()) as db:
        job = db.get(Job, claimed.id)
        repo = db.get(Repository, claimed.repo_id)
        if job is None or repo is None:
            raise ExportError("job or repository is missing")

        detached_job = Job(**job.model_dump())
        detached_repo = Repository(**repo.model_dump())

    if validate_slug(detached_repo.slug):
        raise ExportError("repository slug is invalid")
    return detached_job, detached_repo


def finish(
    job_id: int,
    worker_id: str,
    *,
    status: str,
    exit_code: int | None,
    summary: str = "",
    retry_delay: int | None = None,
    defer: bool = False,
    chain_snapshot: Path | None = None,
) -> bool:
    """Finalize a job and release its repository lock in one transaction."""
    with immediate_session() as db:
        job = db.get(Job, job_id)
        if (
            job is None
            or job.claimed_by != worker_id
            or job.status not in ("running", "cancelling")
        ):
            return False

        lock = db.get(RepoLock, job.repo_id)
        if lock is None or lock.job_id != job_id or lock.worker_id != worker_id:
            return False

        job.status = status
        job.exit_code = exit_code
        job.error_summary = summary[:500]
        job.claimed_by = None
        job.heartbeat_at = None

        if retry_delay is not None:
            if not defer:
                job.attempt += 1
                job.trigger = "retry"
            job.next_run_at = utcnow() + timedelta(seconds=retry_delay)
            job.started_at = None
            job.finished_at = None
            job.cancel_requested = False
        else:
            job.finished_at = utcnow()
            if status == "succeeded":
                job.progress_pct = 100.0

        db.add(job)
        db.delete(lock)

        if status == "succeeded" and chain_snapshot is not None:
            repo = db.get(Job, job.repo_id)
            if repo is None:
                raise ExportError("repository disappeared while chaining export")
            chained = Job(
                repo_id=job.repo_id,
                type="export",
                tool="export",
                trigger="chain",
                parent_job_id=job_id,
                requested_by="worker",
                params={
                    "mode": repo.export_mode,
                    "sync_snapshot": str(chain_snapshot),
                },
                max_attempts=int(repo.retry_policy.get("max_attempts", 1)),
            )
            db.add(chained)
            db.flush()
            audit.record(
                db,
                actor="worker",
                action="export.enqueue",
                target_type="job",
                target_id=str(chained.id),
                detail={"parent_job_id": job_id, "repo": repo.slug},
                commit=False,
            )

        audit.record(
            db,
            actor="worker",
            action=f"job.{status}",
            target_type="job",
            target_id=str(job_id),
            detail={
                "exit_code": exit_code,
                "retry_delay": retry_delay,
                "deferred": defer,
            },
            commit=False,
        )
        return True


def _failed(
    claimed: ClaimedJob,
    worker_id: str,
    repo: Repository,
    exit_code: int,
    summary: str,
) -> None:
    policy = dict(repo.retry_policy)
    retryable = claimed.attempt < int(policy.get("max_attempts", 1)) and retry_policy.is_retryable(
        policy, exit_code
    )
    delay = retry_policy.backoff_seconds(policy, claimed.attempt) if retryable else None
    finish(
        claimed.id,
        worker_id,
        status="queued" if retryable else "failed",
        exit_code=exit_code,
        summary=summary,
        retry_delay=delay,
    )


def _build_sync_plan(
    job: Job,
    repo: Repository,
    settings: Settings,
) -> (
    tuple[
        builders.BuildPlan,
        DebmirrorAdapter | FtpsyncAdapter | RsyncAdapter,
        DebmirrorPlan | FtpsyncPlan | RsyncPlan,
        datetime,
    ]
    | None
):
    """Build an adapter-based plan for supported sync methods."""
    if job.type != "sync" or repo.tool not in (Tool.FTPSYNC, Tool.RSYNC, Tool.DEBMIRROR):
        return None

    sync_plan = build_sync_plan(job, repo, settings)
    if isinstance(sync_plan, FtpsyncPlan):
        adapter: DebmirrorAdapter | FtpsyncAdapter | RsyncAdapter = FtpsyncAdapter()
    elif isinstance(sync_plan, RsyncPlan):
        adapter = RsyncAdapter()
    elif isinstance(sync_plan, DebmirrorPlan):
        adapter = DebmirrorAdapter()
    else:
        raise ExportError("sync plan factory returned an unexpected plan type")

    adapter.validate_plan(sync_plan)

    context = SyncExecutionContext(
        settings=settings,
        job=job,
        repository=repo,
        target=sync_plan.target,
        work_dir=sync_plan.work_dir,
        log_dir=sync_plan.log_dir,
    )

    return (
        adapter.build_plan(sync_plan, context),
        adapter,
        sync_plan,
        job.started_at or utcnow(),
    )


def _execute(
    claimed: ClaimedJob,
    worker_id: str,
    settings: Settings,
    stopping: threading.Event,
) -> None:
    job, repo = _load(claimed)
    log_path = logs.job_log_path(claimed.id, settings)

    with immediate_session() as db:
        current = db.get(Job, claimed.id)
        if current is None or current.claimed_by != worker_id:
            raise LeaseLost("job ownership was lost")
        current.log_path = str(log_path)
        db.add(current)
        audit.record(
            db,
            actor="worker",
            action="job.start",
            target_type="job",
            target_id=str(claimed.id),
            detail={"repo": repo.slug, "type": job.type, "attempt": job.attempt},
            commit=False,
        )

    with repository_guard(settings, claimed.repo_id) as acquired:
        if not acquired:
            finish(
                claimed.id,
                worker_id,
                status="queued",
                exit_code=None,
                summary="waiting for previous process to release repository guard",
                retry_delay=10,
                defer=True,
            )
            return

        with Lease(claimed.id, worker_id, settings, stopping) as lease:
            try:
                if job.type == "export":
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(f"# export attempt {job.attempt}\n")
                        recovered = export_jobs.published_for_job(job.id or 0, settings)
                        if recovered:
                            export_id = recovered
                            handle.write(f"# recovered published export {export_id}\n")
                        else:

                            def pulse(pct: float | None, text: str) -> None:
                                lease.export_pulse(pct, text)
                                if pct is not None:
                                    handle.write(f"# {pct:.0f}% {text}\n")
                                    handle.flush()

                            export_id = export_engine.execute(job, repo, settings, pulse)
                            handle.write(f"# published {export_id}\n")

                    finish(
                        claimed.id,
                        worker_id,
                        status="succeeded",
                        exit_code=0,
                    )
                    return

                adapter_plan = _build_sync_plan(job, repo, settings)
                if adapter_plan is None:
                    plan = builders.build(repo, job, settings)
                    adapter = None
                    sync_plan = None
                    started_at = job.started_at or utcnow()
                else:
                    plan, adapter, sync_plan, started_at = adapter_plan

                environment = dict(os.environ)
                environment["GNUPGHOME"] = str(settings.gnupg_dir)
                environment["LC_ALL"] = "C"

                result = Runner(
                    plan,
                    log_path,
                    tick=lease.update,
                    is_cancelled=lease.cancelled,
                    env=environment,
                ).run()

                if lease.lost.is_set():
                    raise LeaseLost("job ownership was lost")

                sync_result: SyncResult | None = None
                if adapter is not None and sync_plan is not None:
                    sync_result = adapter.interpret_result(
                        sync_plan,
                        result,
                        started_at,
                        utcnow(),
                    )

                if result.cancelled:
                    finish(
                        claimed.id,
                        worker_id,
                        status="cancelled",
                        exit_code=result.exit_code,
                        summary="cancelled by operator or worker shutdown",
                    )
                    return

                if result.exit_code:
                    summary = (
                        sync_result.message
                        if sync_result is not None and sync_result.message
                        else f"tool exited {result.exit_code}"
                    )
                    _failed(
                        claimed,
                        worker_id,
                        repo,
                        result.exit_code,
                        summary,
                    )
                    return

                if sync_result is not None and sync_result.status not in {
                    SyncStatus.SUCCEEDED,
                    SyncStatus.SUCCEEDED_WITH_WARNINGS,
                }:
                    _failed(
                        claimed,
                        worker_id,
                        repo,
                        result.exit_code or 1,
                        sync_result.message or "sync adapter reported failure",
                    )
                    return

                snapshot_path: Path | None = None
                if not job.dry_run:
                    captured = export_engine.capture_sync(
                        settings,
                        repo,
                        job,
                        lease.export_pulse,
                    )
                    should_chain = bool(job.params.get("chain_export", repo.chain_export))
                    if should_chain:
                        snapshot_path = captured

                finish(
                    claimed.id,
                    worker_id,
                    status="succeeded",
                    exit_code=0,
                    chain_snapshot=snapshot_path,
                )
            except ExportCancelled:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write("# export cancelled before publication\n")
                finish(
                    claimed.id,
                    worker_id,
                    status="cancelled",
                    exit_code=None,
                    summary="cancelled before publication",
                )
            except LeaseLost:
                raise
            except Exception:
                log.exception("job execution failed", extra={"job_id": claimed.id})
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write("# execution failed; inspect worker diagnostics\n")
                _failed(
                    claimed,
                    worker_id,
                    repo,
                    1,
                    "execution failed; inspect worker diagnostics",
                )


def _health_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        WORKER_ALIVE_FILE.touch()
        stop.wait(5)


def _parse_tools(value: str) -> tuple[Tool, ...]:
    try:
        tools = tuple(Tool(name.strip()) for name in value.split(",") if name.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"unsupported tool: {exc}") from exc

    if not tools:
        raise argparse.ArgumentTypeError("at least one tool is required")
    return tools


def main() -> int:
    parser = argparse.ArgumentParser(prog="mirrorhub-worker")
    parser.add_argument(
        "--tool",
        choices=TOOLS,
        help="Run a single-tool worker (deprecated; use --tools instead).",
    )
    parser.add_argument(
        "--tools",
        type=_parse_tools,
        default="ftpsync,rsync,debmirror,wget",
        help="Comma-separated sync tools handled by this worker.",
    )
    arguments = parser.parse_args()

    tools = (Tool(arguments.tool),) if arguments.tool else arguments.tools
    worker_concurrency = int(os.getenv("MIRRORHUB_WORKER_CONCURRENCY", "1"))

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(settings.log_format, settings.log_level)
    stopping = threading.Event()
    health_stop = threading.Event()

    def handle_signal(_signum: int, _frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    wait_for_schema()
    health = threading.Thread(
        target=_health_loop,
        args=(health_stop,),
        daemon=True,
    )
    health.start()
    capabilities = "-".join(sorted(tool.value for tool in tools))
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{capabilities}:{secrets.token_hex(3)}"

    try:
        while not stopping.is_set():
            claim.recover_stale(stale_after_s=settings.heartbeat_stale_s)
            claimed = claim.claim_next(
                worker_id=worker_id,
                tools=tools,
                limit=worker_concurrency,
            )
            if claimed is None:
                stopping.wait(POLL_SECONDS)
                continue
            try:
                _execute(claimed, worker_id, settings, stopping)
            except LeaseLost:
                log.warning(
                    "job lease lost; old worker stopped execution",
                    extra={"job_id": claimed.id},
                )
            except Exception:
                log.exception(
                    "worker operation failed",
                    extra={"job_id": claimed.id},
                )
                finish(
                    claimed.id,
                    worker_id,
                    status="failed",
                    exit_code=None,
                    summary="worker operation failed",
                )
    finally:
        health_stop.set()
        health.join(timeout=6)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
