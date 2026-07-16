from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linux_toolchain.errors import ExternalToolError
from linux_toolchain.process import run, run_logged, run_passthrough, run_streaming


class StreamingProcessTest(unittest.TestCase):
    def test_captured_command_timeout_has_a_clean_error(self) -> None:
        with (
            patch(
                "linux_toolchain.process.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["probe"], 2),
            ),
            self.assertRaisesRegex(
                ExternalToolError,
                "command timed out after 2s: probe",
            ),
        ):
            run(["probe"], timeout=2)

    def test_redirects_child_output_away_from_cli_stdout(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            run_streaming([sys.executable, "-c", "print('build progress')"])

        self.assertEqual(stderr.getvalue(), "build progress\n")

    def test_streaming_interrupt_stops_and_reaps_process_group(self) -> None:
        process = unittest.mock.Mock(pid=1234, returncode=None)
        cancel = unittest.mock.Mock()
        process.communicate.side_effect = KeyboardInterrupt
        process.poll.return_value = None
        process.wait.return_value = -signal.SIGINT
        with (
            patch(
                "linux_toolchain.process.subprocess.Popen",
                return_value=process,
            ) as popen,
            patch("linux_toolchain.process.os.killpg") as killpg,
            redirect_stderr(StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_streaming(["builder"], cancel=cancel)

        cancel.assert_called_once_with()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(1234, signal.SIGINT)
        process.wait.assert_called_once_with(timeout=30.0)

    def test_noisy_command_writes_combined_output_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            run_logged(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('out'); print('err', file=sys.stderr)",
                ],
                log,
            )

            self.assertEqual(
                sorted(log.read_text(encoding="utf-8").splitlines()),
                ["err", "out"],
            )

    def test_logged_command_reports_heartbeat_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            elapsed: list[float] = []
            run_logged(
                [
                    sys.executable,
                    "-c",
                    "import time; print('building', flush=True); time.sleep(0.08)",
                ],
                log,
                heartbeat=elapsed.append,
                heartbeat_interval=0.01,
            )

            self.assertTrue(elapsed)
            self.assertEqual(log.read_text(encoding="utf-8"), "building\n")

    def test_logged_command_failure_reports_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build.log"
            with self.assertRaisesRegex(
                ExternalToolError,
                rf"command failed \(7\); full log: {log}",
            ):
                run_logged(
                    [sys.executable, "-c", "import sys; sys.exit(7)"],
                    log,
                )

    def test_passthrough_inherits_standard_streams(self) -> None:
        completed = subprocess.CompletedProcess(["consumer"], returncode=37)
        with patch(
            "linux_toolchain.process.subprocess.run", return_value=completed
        ) as process:
            status = run_passthrough(["consumer"], env={"MODE": "managed"})

        self.assertEqual(status, 37)
        self.assertNotIn("stdout", process.call_args.kwargs)
        self.assertNotIn("stderr", process.call_args.kwargs)
        self.assertEqual(process.call_args.kwargs["env"], {"MODE": "managed"})

    def test_passthrough_normalizes_signal_status_for_a_shell(self) -> None:
        completed = subprocess.CompletedProcess(["consumer"], returncode=-9)
        with patch("linux_toolchain.process.subprocess.run", return_value=completed):
            status = run_passthrough(["consumer"])

        self.assertEqual(status, 137)
