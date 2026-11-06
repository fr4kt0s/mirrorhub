"""Backoff arithmetic and retryable exit codes."""

from mirrorhub.queue import retry


def _policy(**overrides):
    base = {
        "max_attempts": 4,
        "backoff": "exponential",
        "base_seconds": 60,
        "max_seconds": 1000,
        "retryable_exit_codes": [1, 2, 255],
    }
    base.update(overrides)
    return base


def test_exponential_backoff_doubles() -> None:
    policy = _policy()
    assert retry.backoff_seconds(policy, 1) == 60
    assert retry.backoff_seconds(policy, 2) == 120
    assert retry.backoff_seconds(policy, 3) == 240


def test_backoff_is_capped() -> None:
    policy = _policy(max_seconds=100)
    assert retry.backoff_seconds(policy, 5) == 100


def test_linear_and_fixed() -> None:
    assert retry.backoff_seconds(_policy(backoff="linear"), 3) == 180
    assert retry.backoff_seconds(_policy(backoff="fixed"), 3) == 60


def test_retryable_exit_codes() -> None:
    policy = _policy()
    assert retry.is_retryable(policy, 1)
    assert retry.is_retryable(policy, 255)
    assert not retry.is_retryable(policy, 0)
    assert not retry.is_retryable(policy, 42)
    assert not retry.is_retryable(policy, None)


def test_attempts_left() -> None:
    policy = _policy(max_attempts=3)
    assert retry.attempts_left(policy, 1)
    assert retry.attempts_left(policy, 2)
    assert not retry.attempts_left(policy, 3)
