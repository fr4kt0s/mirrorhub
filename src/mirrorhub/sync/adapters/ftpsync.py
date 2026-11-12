"""FTPSync adapter: translates a validated plan into executable steps."""

from __future__ import annotations

from datetime import datetime

from mirrorhub.enums import Tool
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan, SyncPlan
from mirrorhub.sync.result import SyncErrorCode, SyncResult, SyncStatus
from mirrorhub.worker.builders import BuildPlan
from mirrorhub.worker.builders.ftpsync import build as build_ftpsync
from mirrorhub.worker.runner import RunResult


class FtpsyncAdapter:
    """Adapter for Debian-style archive mirrors synchronized with ftpsync."""

    adapter = Tool.FTPSYNC

    def validate_plan(self, plan: SyncPlan) -> None:
        if not isinstance(plan, FtpsyncPlan):
            raise SyncConfigurationError("ftpsync adapter requires an FtpsyncPlan")

        if plan.dry_run:
            raise SyncConfigurationError("ftpsync does not support dry runs")

        if not plan.architectures or "none" in plan.architectures:
            raise SyncConfigurationError("ftpsync requires at least one architecture")

    def build_plan(
        self,
        plan: SyncPlan,
        context: SyncExecutionContext,
    ) -> BuildPlan:
        self.validate_plan(plan)

        return build_ftpsync(
            repo=context.repository,
            job=context.job,
            target=context.target,
            cwd=context.work_dir,
            settings=context.settings,
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
            message=f"ftpsync exited {result.exit_code}",
        )
