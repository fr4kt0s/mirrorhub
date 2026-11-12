"""Rsync adapter: translates a validated plan into executable steps."""

from __future__ import annotations

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters.base import SyncAdapter
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import RsyncPlan, SyncPlan
from mirrorhub.worker.builders import BuildPlan
from mirrorhub.worker.builders.rsync import build as build_rsync


class RsyncAdapter(SyncAdapter):
    """Adapter for generic and Arch repository mirrors synchronized with rsync."""

    adapter = Tool.RSYNC
    result_label = "rsync"

    def validate_plan(self, plan: SyncPlan) -> None:
        if not isinstance(plan, RsyncPlan):
            raise SyncConfigurationError("rsync adapter requires an RsyncPlan")

        if plan.source_protocol != "rsync":
            raise SyncConfigurationError("rsync adapter requires the rsync source protocol")

    def build_plan(
        self,
        plan: SyncPlan,
        context: SyncExecutionContext,
    ) -> BuildPlan:
        self.validate_plan(plan)

        return build_rsync(
            repo=context.repository,
            job=context.job,
            target=context.target,
            cwd=context.work_dir,
        )
