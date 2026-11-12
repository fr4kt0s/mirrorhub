"""Debmirror adapter: translates a validated plan into executable steps."""

from __future__ import annotations

from datetime import datetime

from mirrorhub.enums import Tool
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import DebmirrorPlan, SyncPlan
from mirrorhub.sync.result import SyncErrorCode, SyncResult, SyncStatus
from mirrorhub.worker.builders import BuildPlan
from mirrorhub.worker.builders.debmirror import build as build_debmirror
from mirrorhub.worker.runner import RunResult


class DebmirrorAdapter:
    """Adapter for Debian and Ubuntu repositories synchronized with debmirror."""

    adapter = Tool.DEBMIRROR

    def validate_plan(self, plan: SyncPlan) -> None:
        if not isinstance(plan, DebmirrorPlan):
            raise SyncConfigurationError("debmirror adapter requires a DebmirrorPlan")

        if plan.source_protocol not in ("http", "https", "rsync"):
            raise SyncConfigurationError("debmirror adapter requires http, https, or rsync")

        if not plan.architectures:
            raise SyncConfigurationError("debmirror requires at least one architecture")

        if not plan.suites:
            raise SyncConfigurationError("debmirror requires at least one suite")

        if not plan.sections:
            raise SyncConfigurationError("debmirror requires at least one section")

    def build_plan(
        self,
        plan: SyncPlan,
        context: SyncExecutionContext,
    ) -> BuildPlan:
        self.validate_plan(plan)

        return build_debmirror(
            repo=context.repository,
            job=context.job,
            target=context.target,
            cwd=context.work_dir,
        )

    def interpret_result(
        self,
        plan: SyncPlan,
        result: RunResult,
        started_at: datetime,
        finished_at: datetime,
    ) -> SyncResult:
        if result.cancelled:
            return SyncResult(
                status=SyncStatus.CANCELLED,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=result.exit_code,
                error_code=SyncErrorCode.CANCELLED,
                retryable=False,
                message="cancelled by operator or worker shutdown",
            )

        if result.exit_code == 0:
            return SyncResult(
                status=SyncStatus.SUCCEEDED,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=0,
            )

        return SyncResult(
            status=SyncStatus.FAILED,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=result.exit_code,
            error_code=SyncErrorCode.UNKNOWN,
            retryable=True,
            message=f"debmirror exited {result.exit_code}",
        )
