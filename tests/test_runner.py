"""Runner: step completion and cancellation, independent of the tools' output."""

import time
from pathlib import Path

from mirrorhub.worker.builders import BuildPlan, Step
from mirrorhub.worker.runner import Runner, RunResult


def _run(tmp_path: Path, *argvs: tuple[str, ...], cancel_after: float | None = None) -> RunResult:
    plan = BuildPlan(steps=[Step(argv, argv[0]) for argv in argvs], cwd=tmp_path, target=tmp_path)
    start = time.monotonic()

    def is_cancelled() -> bool:
        return cancel_after is not None and time.monotonic() - start > cancel_after

    runner = Runner(
        plan,
        tmp_path / "job.log",
        tick=lambda _pct, _text: None,
        is_cancelled=is_cancelled,
        tick_interval=0.1,
    )
    return runner.run()


def test_silent_fast_steps_finish(tmp_path: Path) -> None:
    """Regression (#51): a step without output that exits at once could hang forever.

    The process closes stdout a moment before it exits; the end of output and the exit
    were only compared within one loop iteration, so the window was missed.
    """
    for _ in range(50):
        start = time.monotonic()
        result = _run(tmp_path, ("find", str(tmp_path), "-name", "nothing", "-delete"), ("true",))
        assert result.exit_code == 0
        assert time.monotonic() - start < 10


def test_step_that_closes_stdout_before_exiting(tmp_path: Path) -> None:
    result = _run(tmp_path, ("sh", "-c", "exec >&- 2>&-; sleep 1; exit 3"))
    assert result.exit_code == 3


def test_cancel_stops_a_silent_long_step(tmp_path: Path) -> None:
    start = time.monotonic()
    result = _run(tmp_path, ("sleep", "60"), cancel_after=0.5)
    assert result.cancelled
    assert time.monotonic() - start < 15
    assert "# cancel requested" in (tmp_path / "job.log").read_text()
