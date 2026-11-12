"""Adapter contract and registry for method-specific sync implementations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mirrorhub.enums import Tool
from mirrorhub.sync.context import SyncExecutionContext
from mirrorhub.sync.plan import SyncPlan
from mirrorhub.sync.result import SyncResult
from mirrorhub.worker.builders import BuildPlan, RunResult


@runtime_checkable
class SyncAdapter(Protocol):
    """A method-specific translator between a SyncPlan and executable steps."""

    adapter: Tool

    def validate_plan(self, plan: SyncPlan) -> None:
        """Raise SyncConfigurationError when the plan is invalid for this adapter."""
        ...

    def build_plan(
        self,
        plan: SyncPlan,
        context: SyncExecutionContext,
    ) -> BuildPlan:
        """Translate a validated sync plan into an executable build plan."""
        ...

    def interpret_result(
        self,
        plan: SyncPlan,
        result: RunResult,
        started_at: object,
        finished_at: object,
    ) -> SyncResult:
        """Translate a runner result into a normalized sync result."""
        ...


class SyncAdapterRegistry:
    """Maps supported sync tools to their adapter implementations."""

    def __init__(self) -> None:
        self._adapters: dict[Tool, SyncAdapter] = {}

    def register(self, adapter: SyncAdapter) -> None:
        if adapter.adapter in self._adapters:
            raise ValueError(f"adapter already registered: {adapter.adapter}")
        self._adapters[adapter.adapter] = adapter

    def get(self, adapter: Tool) -> SyncAdapter:
        try:
            return self._adapters[adapter]
        except KeyError as exc:
            raise LookupError(f"no adapter registered for {adapter}") from exc

    def supported_tools(self) -> frozenset[Tool]:
        return frozenset(self._adapters)
