"""Execute tool steps with progress callbacks, log capture and cancellation."""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from mirrorhub.worker.builders import BuildPlan, Step
from mirrorhub.worker.progress import ProgressParser

log = logging.getLogger("mirrorhub.worker.runner")

CANCEL_GRACE_S = 20.0
TICK_INTERVAL_S = 2.0


@dataclass
class RunResult:
    exit_code: int
    cancelled: bool
    line_count: int = 0
    last_line: str = ""


class Runner:
    def __init__(
        self,
        plan: BuildPlan,
        log_path: Path,
        *,
        tick: Callable[[float | None, str], None],
        is_cancelled: Callable[[], bool],
        env: dict[str, str] | None = None,
        tick_interval: float = TICK_INTERVAL_S,
    ) -> None:
        self.plan = plan
        self.log_path = log_path
        self._tick = tick
        self._is_cancelled = is_cancelled
        self._env = env
        self._tick_interval = tick_interval
        self._parser = ProgressParser()
        self._lines = 0
        self._last_line = ""
        self._cancelled = False

    def run(self) -> RunResult:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.plan.cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.exception(
                "could not create sync work directory",
                extra={"cwd": str(self.plan.cwd)},
            )
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"error: could not create work directory "
                    f"{self.plan.cwd}: {exc}\n"
                )
                log_file.write("# finished with exit code 127\n")
            return RunResult(
                exit_code=127,
                cancelled=self._cancelled,
                line_count=self._lines,
                last_line=f"could not create work directory {self.plan.cwd}: {exc}",
            )

        exit_code = 0

        with self.log_path.open("a", encoding="utf-8") as log_file:
            for step in self.plan.steps:
                if self._is_cancelled():
                    self._cancelled = True
                    break

                log_file.write("$ " + shlex.join(step.argv) + "\n")
                log_file.flush()

                exit_code = self._run_step(step, log_file)
                if self._cancelled or exit_code != 0:
                    break

            log_file.write(f"# finished with exit code {exit_code}\n")
            log_file.flush()

        return RunResult(
            exit_code=exit_code,
            cancelled=self._cancelled,
            line_count=self._lines,
            last_line=self._last_line,
        )

    def _signal(self, proc: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)

    def _run_step(self, step: Step, log_file: TextIO) -> int:
        executable = shutil.which(step.argv[0])
        if executable is None:
            log_file.write(f"error: {step.argv[0]} is not installed in this image\n")
            log_file.flush()
            return 127

        argv = [executable, *step.argv[1:]]
        try:
            proc = subprocess.Popen(  # noqa: S603
                argv,
                cwd=str(self.plan.cwd),
                env=self._env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
                shell=False,
            )
        except OSError as exc:
            log.exception(
                "could not start tool",
                extra={
                    "tool": step.argv[0],
                    "cwd": str(self.plan.cwd),
                    "argv": step.argv,
                },
            )
            log_file.write(f"error: could not start {step.argv[0]}: {exc}\n")
            log_file.flush()
            return 127

        stream: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            try:
                if proc.stdout is not None:
                    for raw in proc.stdout:
                        stream.put(raw.rstrip("\r\n"))
            finally:
                stream.put(None)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()

        last_tick = 0.0
        cancel_sent_at: float | None = None
        kill_sent = False

        try:
            while True:
                try:
                    line = stream.get(timeout=0.5)
                    drained = line is None
                except queue.Empty:
                    line = None
                    drained = False

                now = time.monotonic()
                if now - last_tick >= self._tick_interval:
                    last_tick = now

                    if self._is_cancelled() and cancel_sent_at is None:
                        self._cancelled = True
                        cancel_sent_at = now
                        log_file.write("# cancel requested: SIGTERM to process group\n")
                        log_file.flush()
                        self._signal(proc, signal.SIGTERM)

                    if (
                        cancel_sent_at is not None
                        and now - cancel_sent_at > CANCEL_GRACE_S
                        and proc.poll() is None
                        and not kill_sent
                    ):
                        kill_sent = True
                        log_file.write("# cancel grace period expired: SIGKILL\n")
                        log_file.flush()
                        self._signal(proc, signal.SIGKILL)

                    self._tick(self._parser.pct, self._parser.text)

                if line is not None:
                    self._lines += 1
                    self._last_line = line[:300]
                    log_file.write(line + "\n")
                    log_file.flush()
                    self._parser.feed(line)

                if drained and proc.poll() is not None:
                    break

            return proc.wait()
        finally:
            if proc.poll() is None:
                self._signal(proc, signal.SIGKILL)
                proc.wait()
            reader.join(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
            log_file.flush()
