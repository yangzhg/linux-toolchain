import unittest
from types import SimpleNamespace
from unittest.mock import patch

from linux_toolchain.container import BuilderHost
from linux_toolchain.diagnostics import _readelf_check, run_diagnostics
from linux_toolchain.elf.reader import ReadElfInspector
from linux_toolchain.managed.builder import _preflight
from linux_toolchain.process import CommandResult
from linux_toolchain.sdk.crosstool_ng import _readelf_executable


class DiagnosticsTest(unittest.TestCase):
    @staticmethod
    def command_result(
        argv: list[str],
        *,
        timeout: float | None = None,
    ) -> CommandResult:
        if argv[0].endswith("docker"):
            if timeout != 5.0:
                raise AssertionError("Docker diagnostics must use a bounded timeout")
        if argv[1:3] == ["context", "inspect"]:
            return CommandResult("unix:///var/run/docker.sock\n", "")
        if argv[1:3] == ["version", "--format"]:
            return CommandResult("linux/amd64\n", "")
        raise AssertionError(f"unexpected diagnostic command: {argv}")

    def test_required_checks_pass_when_optional_tools_are_missing(self) -> None:
        required = {
            name: f"/tools/{name}"
            for name in (
                "docker",
                "readelf",
            )
        }

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("linux_toolchain.diagnostics.platform.system", return_value="Linux"),
            patch("linux_toolchain.diagnostics.os.getuid", return_value=1000),
            patch(
                "linux_toolchain.diagnostics.shutil.which",
                side_effect=lambda name: required.get(name),
            ),
            patch(
                "linux_toolchain.diagnostics.run",
                side_effect=self.command_result,
            ),
        ):
            report = run_diagnostics("managed")

        self.assertTrue(report.passed)
        checks = {check.id: check for check in report.checks}
        self.assertNotIn("git", checks)

    def test_readelf_environment_override_is_consistent_across_workflows(self) -> None:
        configured = "/tools/configured-readelf"

        def which(name: str) -> str | None:
            return {
                configured: configured,
                "docker": "/tools/docker",
            }.get(name)

        with (
            patch.dict(
                "os.environ",
                {"LINUX_TOOLCHAIN_READELF": configured},
                clear=True,
            ),
            patch("linux_toolchain.diagnostics.shutil.which", side_effect=which),
            patch("linux_toolchain.sdk.crosstool_ng.shutil.which", side_effect=which),
            patch("linux_toolchain.managed.builder.shutil.which", side_effect=which),
            patch("linux_toolchain.elf.reader.shutil.which", side_effect=which),
            patch(
                "linux_toolchain.managed.builder.require_non_root_builder",
                return_value=BuilderHost(uid=1000, gid=1000),
            ),
            patch("linux_toolchain.managed.builder.validate_native_docker_daemon"),
        ):
            self.assertEqual(_readelf_check().status, "pass")
            self.assertEqual(_readelf_executable(), configured)
            _preflight("linux/amd64")
            inspector = ReadElfInspector()

        self.assertEqual(inspector._tools, (configured,))

    def test_external_workflow_does_not_require_docker(self) -> None:
        available = {
            name: f"/tools/{name}"
            for name in (
                "readelf",
                "gcc",
                "g++",
            )
        }

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("linux_toolchain.diagnostics.platform.system", return_value="Linux"),
            patch("linux_toolchain.diagnostics.os.getuid", return_value=1000),
            patch(
                "linux_toolchain.diagnostics.shutil.which",
                side_effect=lambda name: available.get(name),
            ),
            patch(
                "linux_toolchain.diagnostics.run",
                side_effect=self.command_result,
            ),
            patch(
                "linux_toolchain.compiler.toolchain.detect_compiler",
                return_value=SimpleNamespace(
                    family="gcc",
                    version="13.2.0",
                    target="x86_64-linux-gnu",
                ),
            ) as detector,
        ):
            report = run_diagnostics("external")

        self.assertTrue(report.passed)
        checks = {check.id: check for check in report.checks}
        self.assertEqual(checks["docker-cli"].level, "optional")
        self.assertEqual(checks["docker-cli"].status, "warn")
        self.assertEqual(checks["external-compiler"].level, "required")
        self.assertEqual(checks["external-compiler"].status, "pass")
        self.assertEqual(
            report.to_dict()["summary"]["required"],
            {"passed": 4, "total": 4},
        )
        detector.assert_called_once_with("/tools/gcc", "/tools/g++")

    def test_consumer_workflow_does_not_contact_an_optional_docker_daemon(
        self,
    ) -> None:
        available = {
            name: f"/tools/{name}"
            for name in (
                "docker",
                "readelf",
                "cmake",
                "make",
            )
        }
        commands: list[list[str]] = []

        def run_probe(
            argv: list[str],
            *,
            timeout: float | None = None,
        ) -> CommandResult:
            commands.append(argv)
            return self.command_result(argv, timeout=timeout)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("linux_toolchain.diagnostics.platform.system", return_value="Linux"),
            patch("linux_toolchain.diagnostics.os.getuid", return_value=1000),
            patch(
                "linux_toolchain.diagnostics.shutil.which",
                side_effect=lambda name: available.get(name),
            ),
            patch("linux_toolchain.diagnostics.run", side_effect=run_probe),
        ):
            report = run_diagnostics("consumer")

        self.assertTrue(report.passed)
        self.assertEqual(report.integrations, ("cmake",))
        checks = {check.id: check for check in report.checks}
        self.assertEqual(checks["cmake"].level, "required")
        self.assertFalse(any(command[0] == "/tools/docker" for command in commands))

    def test_conan_consumer_requires_conan_and_cmake(self) -> None:
        available = {
            name: f"/tools/{name}"
            for name in (
                "readelf",
                "cmake",
            )
        }
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("linux_toolchain.diagnostics.platform.system", return_value="Linux"),
            patch("linux_toolchain.diagnostics.os.getuid", return_value=1000),
            patch(
                "linux_toolchain.diagnostics.shutil.which",
                side_effect=lambda name: available.get(name),
            ),
            patch(
                "linux_toolchain.diagnostics.run",
                side_effect=self.command_result,
            ),
        ):
            report = run_diagnostics("consumer", ("conan",))

        checks = {check.id: check for check in report.checks}
        self.assertFalse(report.passed)
        self.assertEqual(checks["cmake"].level, "required")
        self.assertEqual(checks["cmake"].status, "pass")
        self.assertEqual(checks["conan"].level, "required")
        self.assertEqual(checks["conan"].status, "fail")


if __name__ == "__main__":
    unittest.main()
