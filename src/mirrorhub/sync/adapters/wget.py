"""Wget adapter: translates a validated plan into executable steps."""

from __future__ import annotations

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters.base import SyncAdapter
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import SyncPlan, WgetPlan
from mirrorhub.worker.builders import BuildPlan
from mirrorhub.worker.builders.wget import build as build_wget


class WgetAdapter(SyncAdapter):
    """Adapter for HTTP directory trees synchronized with wget."""

    adapter = Tool.WGET
    result_label = "wget"

    def validate_plan(self, plan: SyncPlan) -> None:
        if not isinstance(plan, WgetPlan):
            raise SyncConfigurationError("wget adapter requires a WgetPlan")

        if plan.source_protocol not in ("http", "https"):
            raise SyncConfigurationError("wget adapter requires http or https")

    def build_plan(
        self,
        plan: SyncPlan,
        context: SyncExecutionContext,
    ) -> BuildPlan:
        self.validate_plan(plan)

        return build_wget(
            repo=context.repository,
            job=context.job,
            target=context.target,
            cwd=context.work_dir,
        )
