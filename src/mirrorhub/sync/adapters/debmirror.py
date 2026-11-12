"""Debmirror adapter: translates a validated plan into executable steps."""

from __future__ import annotations

from mirrorhub.enums import Tool
from mirrorhub.sync.adapters.base import SyncAdapter
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.errors import SyncConfigurationError
from mirrorhub.sync.plan import DebmirrorPlan, SyncPlan
from mirrorhub.worker.builders import BuildPlan
from mirrorhub.worker.builders.debmirror import build as build_debmirror


class DebmirrorAdapter(SyncAdapter):
    """Adapter for Debian and Ubuntu repositories synchronized with debmirror."""

    adapter = Tool.DEBMIRROR
    result_label = "debmirror"

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
