import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from linux_toolchain.compiler.toolchain import (
    CompilerInfo,
    _resolve_compiler_target_tools,
    detect_compiler,
)
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.process import CommandResult


class CompilerDetectionTest(unittest.TestCase):
    def test_detects_supported_gcc_without_copying_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cc = root / "gcc"
            cxx = root / "g++"
            cc.touch()
            cxx.touch()
            with patch(
                "linux_toolchain.compiler.toolchain.run",
                side_effect=[
                    CommandResult("g++ (GCC) 13.2.1\n", ""),
                    CommandResult("x86_64-linux-gnu\n", ""),
                    CommandResult("gcc (GCC) 13.2.1\n", ""),
                    CommandResult("x86_64-linux-gnu\n", ""),
                ],
            ):
                compiler = detect_compiler(cc, cxx)
            self.assertEqual(compiler.family, "gcc")
            self.assertEqual(compiler.major, 13)
            self.assertEqual(compiler.cc, cc.resolve())

    def test_rejects_gcc_older_than_linux_toolchain_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cc = root / "gcc"
            cxx = root / "g++"
            cc.touch()
            cxx.touch()
            with patch(
                "linux_toolchain.compiler.toolchain.run",
                side_effect=[
                    CommandResult("g++ (GCC) 9.5.0\n", ""),
                    CommandResult("x86_64-linux-gnu\n", ""),
                    CommandResult("gcc (GCC) 9.5.0\n", ""),
                    CommandResult("x86_64-linux-gnu\n", ""),
                ],
            ):
                with self.assertRaisesRegex(ConfigurationError, "gcc 10 or newer"):
                    detect_compiler(cc, cxx)

    def test_preserves_clang_and_clangxx_symlink_invocation_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = root / "clang-driver"
            driver.touch()
            cc = root / "clang"
            cxx = root / "clang++"
            cc.symlink_to(driver.name)
            cxx.symlink_to(driver.name)
            with patch(
                "linux_toolchain.compiler.toolchain.run",
                side_effect=[
                    CommandResult("clang version 22.0.0\n", ""),
                    CommandResult("x86_64-pc-linux-gnu\n", ""),
                    CommandResult("clang version 22.0.0\n", ""),
                    CommandResult("x86_64-pc-linux-gnu\n", ""),
                ],
            ):
                compiler = detect_compiler(cc, cxx)

            self.assertEqual(compiler.cc, cc.absolute())
            self.assertEqual(compiler.cxx, cxx.absolute())
            self.assertEqual(compiler.cc.name, "clang")
            self.assertEqual(compiler.cxx.name, "clang++")
            self.assertNotEqual(compiler.cxx, driver.resolve())

    def test_rejects_mismatched_c_and_cxx_drivers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cc = root / "gcc"
            cxx = root / "g++"
            cc.touch()
            cxx.touch()
            with patch(
                "linux_toolchain.compiler.toolchain.run",
                side_effect=[
                    CommandResult("g++ (GCC) 13.2.1\n", ""),
                    CommandResult("x86_64-linux-gnu\n", ""),
                    CommandResult("gcc (GCC) 12.3.0\n", ""),
                    CommandResult("x86_64-linux-gnu\n", ""),
                ],
            ):
                with self.assertRaisesRegex(ConfigurationError, "drivers do not match"):
                    detect_compiler(cc, cxx)

    def test_rejects_non_glibc_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cc = root / "gcc"
            cxx = root / "g++"
            cc.touch()
            cxx.touch()
            with patch(
                "linux_toolchain.compiler.toolchain.run",
                side_effect=[
                    CommandResult("g++ (GCC) 13.2.1\n", ""),
                    CommandResult("aarch64-linux-musl\n", ""),
                ],
            ):
                with self.assertRaisesRegex(ConfigurationError, "glibc target"):
                    detect_compiler(cc, cxx)

    def test_rejects_x86_64_x32_abi_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cc = root / "gcc"
            cxx = root / "g++"
            cc.touch()
            cxx.touch()
            with patch(
                "linux_toolchain.compiler.toolchain.run",
                side_effect=[
                    CommandResult("g++ (GCC) 13.2.1\n", ""),
                    CommandResult("x86_64-linux-gnux32\n", ""),
                    CommandResult("gcc (GCC) 13.2.1\n", ""),
                    CommandResult("x86_64-linux-gnux32\n", ""),
                ],
            ):
                with self.assertRaisesRegex(ConfigurationError, "x32"):
                    detect_compiler(cc, cxx)


class CompilerArchiveToolResolutionTest(unittest.TestCase):
    @staticmethod
    def compiler(*, family: str, target: str, cc: Path) -> CompilerInfo:
        return CompilerInfo(
            family=family,
            version="13.2.1" if family == "gcc" else "18.1.8",
            major=13 if family == "gcc" else 18,
            target=target,
            cc=cc,
            cxx=cc.with_name("c++"),
            version_text=(
                "gcc (GCC) 13.2.1" if family == "gcc" else "clang version 18.1.8"
            ),
        )

    @staticmethod
    def executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_resolves_gcc_and_clang_tools_for_x86_and_aarch64(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            programs = ("ar", "ranlib", "as", "nm", "strip", "objcopy", "objdump")
            executables = {
                program: self.executable(root / f"target-{program}")
                for program in programs
            }
            for family in ("gcc", "clang"):
                for target in ("x86_64-linux-gnu", "aarch64-linux-gnu"):
                    with (
                        self.subTest(family=family, target=target),
                        patch.dict(os.environ, {"PATH": str(root)}),
                        patch(
                            "linux_toolchain.compiler.toolchain.run",
                            side_effect=[
                                CommandResult(f"target-{program}\n", "")
                                for program in programs
                            ],
                        ) as runner,
                    ):
                        compiler = self.compiler(
                            family=family, target=target, cc=root / f"{family}-cc"
                        )
                        tools = _resolve_compiler_target_tools(compiler)

                    selected = {
                        "ar": tools.ar,
                        "ranlib": tools.ranlib,
                        "as": tools.assembler,
                        "nm": tools.nm,
                        "strip": tools.strip,
                        "objcopy": tools.objcopy,
                        "objdump": tools.objdump,
                    }
                    for program, tool in selected.items():
                        self.assertEqual(
                            tool.invocation_path, executables[program].absolute()
                        )
                    self.assertEqual(
                        runner.call_args_list,
                        [
                            call([compiler.cc, f"-print-prog-name={program}"])
                            for program in programs
                        ],
                    )

    def test_rejects_invalid_compiler_tool_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiler = self.compiler(
                family="gcc", target="x86_64-linux-gnu", cc=root / "gcc"
            )
            non_executable = root / "not-executable"
            non_executable.touch()
            cases = (
                ("empty", [CommandResult("\n", "")], "did not report one ar"),
                (
                    "multiline",
                    [CommandResult("ar\nother\n", "")],
                    "did not report one ar",
                ),
                (
                    "missing-ar",
                    [CommandResult(str(root / "missing") + "\n", "")],
                    "ar executable.*not an executable",
                ),
                (
                    "non-executable-ar",
                    [CommandResult(f"{non_executable}\n", "")],
                    "ar executable is not an executable",
                ),
            )
            for name, results, error in cases:
                with (
                    self.subTest(name=name),
                    patch(
                        "linux_toolchain.compiler.toolchain.run", side_effect=results
                    ),
                ):
                    with self.assertRaisesRegex(ConfigurationError, error):
                        _resolve_compiler_target_tools(compiler)

            ar = self.executable(root / "ar")
            with patch(
                "linux_toolchain.compiler.toolchain.run",
                side_effect=[
                    CommandResult(f"{ar}\n", ""),
                    CommandResult(f"{root / 'missing-ranlib'}\n", ""),
                ],
            ):
                with self.assertRaisesRegex(
                    ConfigurationError, "ranlib executable.*not an executable"
                ):
                    _resolve_compiler_target_tools(compiler)


if __name__ == "__main__":
    unittest.main()
