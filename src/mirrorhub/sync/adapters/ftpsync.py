"""FTPSync adapter: translates a validated plan into executable steps."""

from __future__ import annotations

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters.base import SyncAdapter
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import FtpsyncPlan, SyncPlan
from mirrorhub.worker.builders import BuildPlan
from mirrorhub.worker.builders.ftpsync import build as build_ftpsync


class FtpsyncAdapter(SyncAdapter):
    """Adapter for Debian-style archive mirrors synchronized with ftpsync."""

    adapter = Tool.FTPSYNC
    result_label = "ftpsync"

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
