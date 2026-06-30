# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Process manager — manages splice setup/run as subprocesses."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_STEP_RE = re.compile(r"\[(\d+)/(\d+)\]\s*(.*)")


@dataclass
class ProcessState:
    """Current state of the managed subprocess."""

    status: str = "stopped"  # "stopped" | "running" | "setup" | "crashed"
    pid: int | None = None
    started_at: float | None = None
    exit_code: int | None = None
    setup_step: str = ""
    server_ready: bool = False


class ProcessManager:
    """Manages one splice subprocess at a time."""

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._state = ProcessState()
        self._output_buffer: list[str] = []
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._mode: str = ""  # "setup" or "run"
        self._process_gen: int = 0  # generation counter to detect stale reader threads

    def start_setup(self, config_path: str) -> bool:
        """Start `splice --config <path> setup`. Returns False if busy."""
        return self._start(config_path, "setup")

    def start_run(self, config_path: str) -> bool:
        """Start `splice --config <path> run`. Returns False if busy."""
        return self._start(config_path, "run")

    def start_simulate(self, profile: str = "openroadm", config_path: str | None = None) -> bool:
        """Start `splice --simulate <profile> run`. Returns False if busy."""
        return self._start(config_path, "run", simulate=profile)

    def stop(self) -> None:
        """Gracefully stop the running process (SIGTERM then SIGKILL)."""
        proc = self._process
        if proc is None or proc.poll() is not None:
            return

        log.debug("Stopping process PID %d", proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.debug("Process did not stop, sending SIGKILL")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        # Wait for reader thread to finish
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)
            self._reader_thread = None

        with self._lock:
            self._process_gen += 1  # invalidate any lingering reader
            self._state.status = "stopped"
            self._state.exit_code = proc.returncode
            self._state.server_ready = False

    def get_state(self) -> ProcessState:
        """Return current process state, polling for exit if needed."""
        with self._lock:
            if self._process is not None:
                rc = self._process.poll()
                if rc is not None and self._state.status not in ("stopped", "crashed"):
                    self._state.exit_code = rc
                    self._state.status = "crashed" if rc != 0 else "stopped"
                    self._state.server_ready = False
            # Return a copy
            return ProcessState(
                status=self._state.status,
                pid=self._state.pid,
                started_at=self._state.started_at,
                exit_code=self._state.exit_code,
                setup_step=self._state.setup_step,
                server_ready=self._state.server_ready,
            )

    def read_output(self) -> list[str]:
        """Drain buffered output lines (non-blocking)."""
        with self._lock:
            lines = self._output_buffer
            self._output_buffer = []
            return lines

    def shutdown(self) -> None:
        """Clean up on app exit — stop any running process."""
        if self.is_running:
            self.stop()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def mode(self) -> str:
        return self._mode

    # -- Internal ------------------------------------------------------------

    def _start(self, config_path: str | None, mode: str, simulate: str | None = None) -> bool:
        if self.is_running:
            return False

        self._mode = mode
        cmd = [sys.executable, "-m", "splice.cli"]
        if simulate:
            cmd.extend(["--simulate", simulate])
        if config_path:
            cmd.extend(["--config", config_path])
        cmd.append(mode)

        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            log.error("Failed to start subprocess: %s", exc)
            with self._lock:
                self._state = ProcessState(status="crashed", exit_code=-1)
            return False

        with self._lock:
            self._process_gen += 1
            self._state = ProcessState(
                status=mode if mode == "setup" else "running",
                pid=self._process.pid,
                started_at=time.time(),
            )
            self._output_buffer = []

        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"proc-reader-{mode}"
        )
        self._reader_thread.start()
        return True

    def _read_loop(self) -> None:
        """Read subprocess stdout line by line until EOF."""
        proc = self._process
        if proc is None or proc.stdout is None:
            return

        gen = self._process_gen  # capture generation at start

        try:
            for line in iter(proc.stdout.readline, ""):
                with self._lock:
                    if self._process_gen != gen:
                        return  # newer process started, abandon this reader
                    self._output_buffer.append(line)

                    # Parse setup progress
                    m = _STEP_RE.search(line)
                    if m:
                        self._state.setup_step = line.strip()

                    # Detect server readiness
                    if "gNMI server listening on" in line:
                        self._state.server_ready = True
        except Exception:
            log.debug("Error in process reader loop", exc_info=True)
        finally:
            # Process ended
            rc = proc.wait()
            with self._lock:
                if self._process_gen != gen:
                    return  # newer process started, don't overwrite state
                self._state.exit_code = rc
                if self._state.status not in ("stopped",):
                    self._state.status = "stopped" if rc == 0 else "crashed"
                self._state.server_ready = False
