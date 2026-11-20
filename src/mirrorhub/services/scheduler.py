"""Cron scheduler and the periodic housekeeping loop.

Runs only inside the web container (single instance). Every tick: fire due schedules,
dispatch job notifications, check disk thresholds, sweep old logs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, cast

from croniter import croniter
from sqlmodel import Session, col, select

from mirrorhub.db import get_engine
from mirrorhub.enums import Trigger
from mirrorhub.models import Repository, Schedule, utcnow
from mirrorhub.services import appsettings, export_jobs, logs, notify, storage
from mirrorhub.services import jobs as jobsvc
from mirrorhub.settings import get_settings

log = logging.getLogger("mirrorhub.scheduler")

PRESETS: dict[str, tuple[str, str]] = {
    "hourly": ("0 * * * *", "Every hour"),
    "every6h": ("0 */6 * * *", "Every 6 hours"),
    "daily": ("0 3 * * *", "Daily at 03:00 UTC"),
    "weekly": ("0 3 * * 1", "Weekly, Monday 03:00 UTC"),
    "monthly": ("0 3 1 * *", "Monthly, 1st at 03:00 UTC"),
}

TICK_SECONDS = 30.0


def next_after(expr: str, after: datetime | None = None) -> datetime:
    base = after or utcnow()
    return cast(datetime, croniter(expr, base).get_next(datetime))


def ensure_next_fire(db: Session) -> int:
    """Fill in next_fire_at for schedules that were imported without one."""
    filled = 0
    for schedule in db.exec(select(Schedule)).all():
        if schedule.next_fire_at is None and schedule.enabled:
            schedule.next_fire_at = next_after(schedule.cron_expr)
            db.add(schedule)
            filled += 1
    if filled:
        db.commit()
    return filled


def fire_due(db: Session, now: datetime | None = None, actor: str = "scheduler") -> list[int]:
    """Enqueue jobs for schedules whose next_fire_at has passed."""
    now = now or utcnow()
    created: list[int] = []
    schedules = db.exec(
        select(Schedule).where(
            col(Schedule.enabled).is_(True),
            col(Schedule.next_fire_at).is_not(None),
            col(Schedule.next_fire_at) <= now,
        )
    ).all()
    for schedule in schedules:
        repo = db.get(Repository, schedule.repo_id)
        schedule.last_fire_at = now
        schedule.next_fire_at = next_after(schedule.cron_expr, now)
        db.add(schedule)
        if repo is None or not repo.enabled:
            continue
        if jobsvc.find_active(db, repo.id or 0, schedule.job_type) is not None:
            log.info(
                "schedule skipped: job already active",
                extra={"schedule_id": schedule.id, "repo": repo.slug},
            )
            continue
        params: dict[str, Any] = {"schedule_id": schedule.id}
        if schedule.chain_export is not None:
            # Explicit override; otherwise the worker falls back to repo.chain_export.
            params["chain_export"] = schedule.chain_export
        job = jobsvc.enqueue(
            db,
            repo,
            job_type=schedule.job_type,
            trigger=Trigger.SCHEDULE.value,
            params=params,
            requested_by=actor,
        )
        created.append(job.id or 0)
        log.info(
            "schedule fired",
            extra={"schedule_id": schedule.id, "job_id": job.id, "repo": repo.slug},
        )
    db.commit()
    return created


def upcoming(db: Session, limit: int = 5) -> list[Schedule]:
    return list(
        db.exec(
            select(Schedule)
            .where(col(Schedule.enabled).is_(True), col(Schedule.next_fire_at).is_not(None))
            .order_by(col(Schedule.next_fire_at))
            .limit(limit)
        ).all()
    )


def tick() -> dict[str, int]:
    """One housekeeping pass. Blocking; run it in a worker thread."""
    summary = {
        "fired": 0,
        "jobs_notified": 0,
        "logs_removed": 0,
        "disk_alerts": 0,
        "sync_inventories_removed": 0,
        "export_metadata_pruned": 0,
        "drop_folder_removed": 0,
        "notifications_purged": 0,
        "event_files_removed": 0,
    }
    with Session(get_engine()) as db:
        ensure_next_fire(db)
        summary["fired"] = len(fire_due(db))
        summary["jobs_notified"] = len(notify.sweep_finished(db))
        retention = int(appsettings.get(db, "log_retention_days", 30))
        try:
            summary["sync_inventories_removed"] = export_jobs.purge_sync_inventories(
                db, get_settings()
            )
        except OSError:
            log.exception("sync inventory cleanup failed")
        try:
            cleanup = export_jobs.housekeeping(db, get_settings())
            summary["export_metadata_pruned"] = cleanup["metadata_pruned"]
            summary["drop_folder_removed"] = cleanup["drop_folder_removed"]
        except OSError:
            log.exception("export housekeeping failed")
        summary["notifications_purged"] = notify.purge_deliveries(db)
        try:
            summary["event_files_removed"] = notify.purge_event_files(db, get_settings())
        except OSError:
            log.exception("event file cleanup failed")
        try:
            summary["disk_alerts"] = len(notify.check_disk(db, storage.mount_usage()))
        except Exception:
            log.exception("disk check failed")
    summary["logs_removed"] = logs.retention_sweep(days=retention)
    return summary


async def run_forever() -> None:
    log.info("scheduler loop started", extra={"interval_s": TICK_SECONDS})
    while True:
        try:
            summary = await asyncio.to_thread(tick)
            if any(summary.values()):
                log.info("scheduler tick", extra=summary)
        except asyncio.CancelledError:
            log.info("scheduler loop stopped")
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)
