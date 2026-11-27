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
        """Require an FtpsyncPlan; ftpsync has no dry run."""
        if not isinstance(plan, FtpsyncPlan):
            raise SyncConfigurationError("ftpsync adapter requires an FtpsyncPlan")

        if plan.dry_run:
            raise SyncConfigurationError("ftpsync does not support dry runs")

        # Empty ARCH_INCLUDE and ARCH_EXCLUDE means "all architectures" (form hint,
        # builder); the plan model rejects both-set and "none".
        if plan.architectures and plan.arch_exclude:
            raise SyncConfigurationError("ftpsync supports either ARCH_INCLUDE or ARCH_EXCLUDE")

    def build_plan(
        self,
        plan: SyncPlan,
        context: SyncExecutionContext,
    ) -> BuildPlan:
        """Build the ftpsync configuration and command for the plan."""
        self.validate_plan(plan)

        return build_ftpsync(
            repo=context.repository,
            job=context.job,
            target=context.target,
            cwd=context.work_dir,
            settings=context.settings,
        )
