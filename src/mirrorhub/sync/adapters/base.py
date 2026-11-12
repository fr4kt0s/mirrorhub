"""Shared behavior for concrete sync adapters."""

from __future__ import annotations

from datetime import datetime

from mirrorhub.enums import Tool
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import SyncPlan
from mirrorhub.sync.result import SyncErrorCode, SyncResult, SyncStatus
from mirrorhub.worker.builders import BuildPlan
from mirrorhub.worker.runner import RunResult


class SyncAdapter:
    """Base class for adapters that turn validated plans into executable work."""

    adapter: Tool
    result_label: str

    def validate_plan(self, plan: SyncPlan) -> None:
        raise NotImplementedError

    def build_plan(
        self,
        plan: SyncPlan,
        context: SyncExecutionContext,
    ) -> BuildPlan:
        raise NotImplementedError

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
            message=f"{self.result_label} exited {result.exit_code}",
        )
