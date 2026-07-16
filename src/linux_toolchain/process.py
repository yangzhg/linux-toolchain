from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from linux_toolchain.errors import ExternalToolError


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


_INTERRUPT_GRACE_SECONDS = 30.0
_TERMINATE_GRACE_SECONDS = 5.0


def _signal_process_group(
    process: subprocess.Popen[bytes] | subprocess.Popen[str],
    signal_number: int,
) -> None:
    try:
        os.killpg(process.pid, signal_number)
    except ProcessLookupError:
        return
    except OSError:
        try:
            process.send_signal(signal_number)
        except OSError:
            return


def _stop_process_group(
    process: subprocess.Popen[bytes] | subprocess.Popen[str],
) -> None:
    """Stop and reap an isolated long-running command and its descendants."""

    if process.poll() is not None:
        return
    for signal_number, timeout in (
        (signal.SIGINT, _INTERRUPT_GRACE_SECONDS),
        (signal.SIGTERM, _TERMINATE_GRACE_SECONDS),
    ):
        _signal_process_group(process, signal_number)
        try:
            process.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    _signal_process_group(process, signal.SIGKILL)
    process.wait()


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    command = [os.fspath(arg) for arg in argv]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ExternalToolError(
            f"command timed out after {timeout:g}s: {' '.join(command)}"
        ) from error
    except OSError as error:
        raise ExternalToolError(f"cannot run {command[0]!r}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ExternalToolError(
            f"command failed ({result.returncode}): {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        )
    return CommandResult(result.stdout, result.stderr)


def run_streaming(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    cancel: Callable[[], None] | None = None,
) -> None:
    """Run a long command while reserving stdout for machine-readable results."""

    command = [os.fspath(arg) for arg in argv]
    try:
        sys.stderr.fileno()
        redirected = False
    except (AttributeError, OSError, ValueError):
        # StringIO-backed stderr is common for API callers and tests, but it
        # cannot be passed to subprocess. Capture in that case and forward it.
        redirected = True
    process: subprocess.Popen[str] | None = None
    try:
        if redirected:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            output, _ = process.communicate()
            if output:
                sys.stderr.write(output)
        else:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                text=True,
                stdout=sys.stderr,
                stderr=sys.stderr,
                start_new_session=True,
            )
            process.wait()
    except BaseException as error:
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        if process is not None:
            _stop_process_group(process)
        if isinstance(error, OSError):
            raise ExternalToolError(f"cannot run {command[0]!r}: {error}") from error
        raise
    if process.returncode != 0:
        raise ExternalToolError(
            f"command failed ({process.returncode}): {' '.join(command)}"
        )


def run_logged(
    argv: Sequence[str | os.PathLike[str]],
    log_path: Path,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    heartbeat: Callable[[float], None] | None = None,
    heartbeat_interval: float = 30.0,
    cancel: Callable[[], None] | None = None,
) -> None:
    """Run a noisy command with combined output in a dedicated log file."""

    command = [os.fspath(arg) for arg in argv]
    process: subprocess.Popen[bytes] | None = None
    try:
        with log_path.open("wb") as log:
            if heartbeat is not None and heartbeat_interval <= 0:
                raise ValueError("heartbeat_interval must be positive")
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            if heartbeat is None:
                returncode = process.wait()
            else:
                started = time.monotonic()
                while True:
                    try:
                        returncode = process.wait(timeout=heartbeat_interval)
                        break
                    except subprocess.TimeoutExpired:
                        heartbeat(time.monotonic() - started)
    except BaseException as error:
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass
        if process is not None:
            _stop_process_group(process)
        if isinstance(error, OSError):
            raise ExternalToolError(
                f"cannot run {command[0]!r} with log {log_path}: {error}"
            ) from error
        raise
    if returncode != 0:
        raise ExternalToolError(f"command failed ({returncode}); full log: {log_path}")


def run_passthrough(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run a consumer command and return its shell-compatible exit status."""

    command = [os.fspath(arg) for arg in argv]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            check=False,
        )
    except OSError as error:
        raise ExternalToolError(f"cannot run {command[0]!r}: {error}") from error
    if result.returncode < 0:
        return 128 - result.returncode
    return result.returncode
