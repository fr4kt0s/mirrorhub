"""Retry and backoff arithmetic. Pure functions, no database access."""

from __future__ import annotations

from typing import Any


def backoff_seconds(policy: dict[str, Any], attempt: int) -> int:
    """Delay before the next attempt. `attempt` is the attempt that just failed (1-based)."""
    base = int(policy.get("base_seconds", 60))
    ceiling = int(policy.get("max_seconds", 3600))
    kind = policy.get("backoff", "exponential")
    if kind == "fixed":
        delay = base
    elif kind == "linear":
        delay = base * max(attempt, 1)
    else:
        delay = base * 2 ** max(attempt - 1, 0)
    return max(0, min(delay, ceiling))


def is_retryable(policy: dict[str, Any], exit_code: int | None) -> bool:
    if exit_code is None:
        return False
    codes = policy.get("retryable_exit_codes") or []
    return int(exit_code) in {int(code) for code in codes}


def attempts_left(policy: dict[str, Any], attempt: int) -> bool:
    return attempt < int(policy.get("max_attempts", 1))
