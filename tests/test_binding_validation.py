import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linux_toolchain.compiler.binding import (
    GccRuntimeLinkEvidence,
    LlvmRuntimeLinkEvidence,
    _verify_archive_tools,
    _verify_binding_links,
    _verify_target_tools,
)
from linux_toolchain.errors import ExternalToolError
from linux_toolchain.process import CommandResult


class ArchiveToolVerificationTest(unittest.TestCase):
    @staticmethod
    def metadata(
        arch: str,
        *,
        elf_type: str,
        interpreter: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            machine=arch,
            elf_class="ELF64",
            endianness="little",
            elf_type=elf_type,
            interpreter=interpreter,
        )

    @staticmethod
    def successful_run(commands: list[tuple[str, ...]], member_name: str):
        def fake_run(argv, *, env=None):
            command = tuple(os.fspath(argument) for argument in argv)
            commands.append(command)
            if len(command) >= 2 and command[1] == "t":
                return CommandResult(f"{member_name}\n", "")
            map_argument = next(
                (argument for argument in command if argument.startswith("-Wl,-Map,")),
                None,
            )
            if map_argument is not None:
                map_path = Path(map_argument.removeprefix("-Wl,-Map,"))
                archive = next(
                    Path(argument) for argument in command if argument.endswith(".a")
                )
                map_path.write_text(f"{archive}({member_name})\n", encoding="utf-8")
            stderr = (
                "linux-toolchain: controlled-linker-trace\n" if env is not None else ""
            )
            return CommandResult("", stderr)

        return fake_run

    def test_rejects_wrong_machine_archive_member_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "binding"
            output.mkdir()
            with (
                patch(
                    "linux_toolchain.compiler.binding.run",
                    side_effect=self.successful_run(
                        [], "linux-toolchain-archive-member.o"
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.return_value = self.metadata(
                    "aarch64", elf_type="REL"
                )
                inspector_type.return_value.inspect_archive.return_value = (
                    self.metadata("x86_64", elf_type="REL"),
                )
                with self.assertRaisesRegex(
                    ExternalToolError, "archive probe member.*x86_64"
                ):
                    _verify_archive_tools(
                        cc_wrapper=output / "bin" / "cc",
                        ar_wrapper=output / "bin" / "ar",
                        ranlib_wrapper=output / "bin" / "ranlib",
                        output=output,
                        target_arch="aarch64",
                        expected_interpreter="/lib/ld-linux-aarch64.so.1",
                    )

            self.assertFalse((output / ".archive-validation").exists())

    def test_proves_compiler_selected_target_tools_for_aarch64(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "binding"
            output.mkdir()
            wrappers = {
                name: output / "bin" / name
                for name in ("as", "nm", "strip", "objcopy", "objdump")
            }
            commands: list[tuple[str, ...]] = []

            def successful_run(argv):
                command = tuple(os.fspath(argument) for argument in argv)
                commands.append(command)
                stdout = (
                    "00000000 T linux_toolchain_assembler_probe\n"
                    if command[0] == str(wrappers["nm"])
                    else ""
                )
                return CommandResult(stdout, "")

            with (
                patch(
                    "linux_toolchain.compiler.binding.run", side_effect=successful_run
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.return_value = self.metadata(
                    "aarch64", elf_type="REL"
                )
                result = _verify_target_tools(
                    wrappers=wrappers,
                    output=output,
                    target_arch="aarch64",
                )

            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                [Path(command[0]).name for command in commands],
                ["as", "nm", "objdump", "objcopy", "strip"],
            )
            self.assertFalse((output / ".target-tool-validation").exists())

    def test_rejects_native_assembler_fallback_for_aarch64(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "binding"
            output.mkdir()
            wrappers = {
                name: output / "bin" / name
                for name in ("as", "nm", "strip", "objcopy", "objdump")
            }
            with (
                patch(
                    "linux_toolchain.compiler.binding.run",
                    return_value=CommandResult("", ""),
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.return_value = self.metadata(
                    "x86_64", elf_type="REL"
                )
                with self.assertRaisesRegex(
                    ExternalToolError, "assembler probe object.*x86_64"
                ):
                    _verify_target_tools(
                        wrappers=wrappers,
                        output=output,
                        target_arch="aarch64",
                    )

            self.assertFalse((output / ".target-tool-validation").exists())


class BindingLinkVerificationTest(unittest.TestCase):
    def verification_paths(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        output = root / "binding"
        output.mkdir()
        sysroot = root / "sdk" / "sysroot"
        (sysroot / "usr" / "lib64").mkdir(parents=True)
        overlay = output / "glibc-startfiles"
        overlay.mkdir()
        cc_wrapper = output / "bin" / "cc"
        cxx_wrapper = output / "bin" / "c++"
        return output, sysroot, overlay, cc_wrapper, cxx_wrapper

    def map_writing_run(
        self,
        *,
        sysroot: Path,
        overlay: Path,
        commands: list[tuple[str, ...]],
        sources: dict[str, str],
        host_libc_for: str | None = None,
        runtime_root: Path | None = None,
        runtime_gcc_dir: Path | None = None,
        runtime_library_dir: Path | None = None,
        linker_wrapper: Path | None = None,
        extra_host_input_for: str | None = None,
        extra_target_input: Path = Path("/usr/lib/x86_64-linux-gnu/libhost-only.so"),
        extra_trace_input: Path | None = None,
    ):
        def fake_run(argv, *, env=None):
            command = tuple(os.fspath(argument) for argument in argv)
            commands.append(command)
            if "-print-prog-name=ld" in command:
                if linker_wrapper is None:
                    raise AssertionError("linker selection was not expected")
                return CommandResult(f"{linker_wrapper}\n", "")
            source = next(
                (
                    Path(argument)
                    for argument in command
                    if argument.endswith((".c", ".cc"))
                ),
                None,
            )
            if source is not None:
                sources[source.name] = source.read_text(encoding="utf-8")
            if "-c" in command:
                return CommandResult("", "")
            map_argument = next(
                argument for argument in command if argument.startswith("-Wl,-Map,")
            )
            map_path = Path(map_argument.removeprefix("-Wl,-Map,"))
            check_name = map_path.stem
            lines = [
                f"LOAD {overlay / 'crti.o'}",
                f"LOAD {overlay / 'crtn.o'}",
            ]
            if "shared" not in check_name:
                lines.append(f"LOAD {overlay / 'Scrt1.o'}")
            if check_name == host_libc_for:
                lines.append("LOAD /usr/lib/x86_64-linux-gnu/libc.so.6")
            else:
                libc = "libc.a" if "static" in check_name else "libc.so"
                lines.append(f"LOAD {sysroot / 'usr/lib64' / libc}")
            if check_name == extra_host_input_for:
                lines.append(f"LOAD {extra_target_input}")
            if runtime_root is not None:
                assert runtime_gcc_dir is not None
                assert runtime_library_dir is not None
                lines.extend(
                    (
                        "LOAD "
                        + str(
                            runtime_gcc_dir
                            / (
                                "crtbeginT.o"
                                if "static" in check_name
                                else "crtbeginS.o"
                            )
                        ),
                        f"LOAD {runtime_gcc_dir / 'crtendS.o'}",
                        f"LOAD {runtime_gcc_dir / 'libgcc.a'}",
                    )
                )
                if "cxx" in check_name:
                    library = (
                        "libstdc++.a" if "static" in check_name else "libstdc++.so.6"
                    )
                    lines.append(f"LOAD {runtime_library_dir / library}")
                lines.append("/DISCARD/")
            elif check_name == "cxx-executable":
                lines.append("LOAD /compiler/runtime/libstdc++.so.6")
            map_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            stderr_lines = []
            if linker_wrapper is not None:
                stderr_lines.append(
                    f"{linker_wrapper}: warning: controlled-linker-diagnostic"
                )
            if env is not None:
                stderr_lines.append("linux-toolchain: controlled-linker-trace")
            stderr = "\n".join(stderr_lines)
            if stderr:
                stderr += "\n"
            stdout = f"{extra_trace_input}\n" if extra_trace_input is not None else ""
            return CommandResult(stdout, stderr)

        return fake_run

    def llvm_runtime_evidence(self, root: Path) -> LlvmRuntimeLinkEvidence:
        runtime_root = root / "llvm-runtime" / "runtime"
        library_dir = runtime_root / "lib"
        resource_lib = runtime_root / "lib/clang/22/lib/linux"
        library_dir.mkdir(parents=True)
        resource_lib.mkdir(parents=True)
        libraries = tuple(
            library_dir / f"{name}.so.1"
            for name in ("libc++", "libc++abi", "libunwind")
        )
        static_libraries = tuple(
            library_dir / f"{name}.a" for name in ("libc++", "libc++abi", "libunwind")
        )
        builtins = resource_lib / "libclang_rt.builtins-x86_64.a"
        crt_objects = tuple(
            resource_lib / f"clang_rt.crt{kind}-x86_64.o" for kind in ("begin", "end")
        )
        return LlvmRuntimeLinkEvidence(
            runtime_root=runtime_root,
            library_dirs=(library_dir,),
            shared_libraries=libraries,
            static_libraries=static_libraries,
            builtins=builtins,
            crt_objects=crt_objects,
            forbidden_sonames=("libgcc_s.so.1", "libstdc++.so.6"),
        )

    def llvm_map_writing_run(
        self,
        *,
        sysroot: Path,
        overlay: Path,
        runtime: LlvmRuntimeLinkEvidence,
        linker_wrapper: Path,
        commands: list[tuple[str, ...]],
    ):
        def fake_run(argv, *, env=None):
            command = tuple(os.fspath(argument) for argument in argv)
            commands.append(command)
            if "-print-prog-name=ld" in command:
                return CommandResult(f"{linker_wrapper}\n", "")
            if "-c" in command:
                return CommandResult("", "")
            map_argument = next(
                argument for argument in command if argument.startswith("-Wl,-Map,")
            )
            map_path = Path(map_argument.removeprefix("-Wl,-Map,"))
            check_name = map_path.stem
            lines = [
                f"LOAD {overlay / 'crti.o'}",
                f"LOAD {overlay / 'crtn.o'}",
                "LOAD "
                + str(
                    sysroot
                    / "usr/lib64"
                    / ("libc.a" if "static" in check_name else "libc.so")
                ),
                f"LOAD {runtime.builtins}",
                *(f"LOAD {path}" for path in runtime.crt_objects),
            ]
            if "shared" not in check_name:
                lines.append(f"LOAD {overlay / 'Scrt1.o'}")
            if "static" in check_name:
                components = ("libc++.a", "libunwind.a") if "cxx" in check_name else ()
                lines.extend(
                    f"LOAD {next(path for path in runtime.static_libraries if path.name == name)}"
                    for name in components
                )
            elif "cxx" in check_name:
                lines.extend(f"LOAD {library}" for library in runtime.shared_libraries)
            map_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            stderr = (
                "linux-toolchain: controlled-linker-trace\n" if env is not None else ""
            )
            return CommandResult("", stderr)

        return fake_run

    def test_rejects_a_link_map_that_selects_host_libc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, sysroot, overlay, cc_wrapper, cxx_wrapper = self.verification_paths(
                root
            )
            commands: list[tuple[str, ...]] = []
            sources: dict[str, str] = {}
            loader = "/lib64/ld-linux-x86-64.so.2"
            metadata = SimpleNamespace(
                machine="x86_64",
                elf_class="ELF64",
                endianness="little",
                interpreter=loader,
            )

            with (
                patch(
                    "linux_toolchain.compiler.binding.run",
                    side_effect=self.map_writing_run(
                        sysroot=sysroot,
                        overlay=overlay,
                        commands=commands,
                        sources=sources,
                        host_libc_for="c-shared-library",
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.return_value = metadata
                with self.assertRaisesRegex(
                    ExternalToolError, "c-shared-library.*SDK libc"
                ):
                    _verify_binding_links(
                        cc_wrapper=cc_wrapper,
                        cxx_wrapper=cxx_wrapper,
                        output=output,
                        sysroot=sysroot,
                        overlay=overlay,
                        target_arch="x86_64",
                        expected_interpreter=loader,
                    )

            self.assertEqual(len(commands), 4)
            self.assertEqual(inspector_type.return_value.inspect.call_count, 1)
            self.assertFalse((output / ".link-validation").exists())

    def test_aarch64_runtime_verification_covers_exception_library_and_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, sysroot, overlay, cc_wrapper, cxx_wrapper = self.verification_paths(
                root
            )
            runtime_root = root / "runtime-export" / "runtime"
            runtime_gcc_dir = runtime_root / "lib/gcc/aarch64-linux-gnu/12.5.0"
            runtime_library_dir = runtime_root / "lib64"
            runtime_gcc_dir.mkdir(parents=True)
            runtime_library_dir.mkdir()
            compiler_bin = root / "compiler-kit" / "compiler" / "bin"
            compiler_bin.mkdir(parents=True)
            linker_binary = compiler_bin / "ld.bfd"
            linker_binary.touch()
            selected_linker = compiler_bin / "aarch64-portable-linux-gnu-ld"
            selected_linker.symlink_to(linker_binary.name)
            commands: list[tuple[str, ...]] = []
            sources: dict[str, str] = {}
            loader = "/lib/ld-linux-aarch64.so.1"

            def inspect(binary: Path):
                static = "static" in binary.name
                return SimpleNamespace(
                    machine="aarch64",
                    elf_class="ELF64",
                    endianness="little",
                    elf_type="DYN" if binary.suffix == ".so" else "EXEC",
                    interpreter=(None if binary.suffix == ".so" or static else loader),
                    rpath=(),
                    runpath=(),
                )

            with (
                patch(
                    "linux_toolchain.compiler.binding.run",
                    side_effect=self.map_writing_run(
                        sysroot=sysroot,
                        overlay=overlay,
                        commands=commands,
                        sources=sources,
                        runtime_root=runtime_root,
                        runtime_gcc_dir=runtime_gcc_dir,
                        runtime_library_dir=runtime_library_dir,
                        # GNU ld may resolve its selected symlink before using
                        # its executable path as a diagnostic prefix.
                        linker_wrapper=linker_binary,
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.side_effect = inspect
                result = _verify_binding_links(
                    cc_wrapper=cc_wrapper,
                    cxx_wrapper=cxx_wrapper,
                    output=output,
                    sysroot=sysroot,
                    overlay=overlay,
                    target_arch="aarch64",
                    expected_interpreter=loader,
                    runtime=GccRuntimeLinkEvidence(
                        runtime_root=runtime_root,
                        gcc_runtime_dir=runtime_gcc_dir,
                        library_dirs=(runtime_library_dir,),
                    ),
                    linker_executable=selected_linker,
                )

            self.assertEqual(
                result["checks"],
                [
                    "c-executable",
                    "c-shared-library",
                    "cxx-executable",
                    "cxx-shared-exception",
                    "c-static-executable",
                    "cxx-static-exception",
                ],
            )
            self.assertEqual(len(commands), 12)
            self.assertIn("std::runtime_error", sources["cxx-executable.cc"])
            self.assertIn("catch (const std::exception&)", sources["cxx-executable.cc"])
            self.assertIn("std::runtime_error", sources["cxx-shared-exception.cc"])
            self.assertIn("std::runtime_error", sources["cxx-static-exception.cc"])
            link_commands = [command for command in commands if "-c" not in command]
            self.assertEqual(len(link_commands), 6)
            self.assertTrue(all("-Wl,-t" in command for command in link_commands))

    def test_llvm_runtime_link_evidence_covers_shared_and_static_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, sysroot, overlay, cc_wrapper, cxx_wrapper = self.verification_paths(
                root
            )
            runtime = self.llvm_runtime_evidence(root)
            linker_wrapper = output / "linker-bin/ld"
            linker_wrapper.parent.mkdir()
            linker_wrapper.touch()
            commands: list[tuple[str, ...]] = []
            loader = "/lib64/ld-linux-x86-64.so.2"

            def inspect(binary: Path):
                static = "static" in binary.name
                needed = ("libc++.so.1",) if "cxx" in binary.name and not static else ()
                return SimpleNamespace(
                    machine="x86_64",
                    elf_class="ELF64",
                    endianness="little",
                    elf_type="DYN" if binary.suffix == ".so" else "EXEC",
                    interpreter=(None if binary.suffix == ".so" or static else loader),
                    rpath=(),
                    runpath=(),
                    needed=needed,
                )

            with (
                patch(
                    "linux_toolchain.compiler.binding.run",
                    side_effect=self.llvm_map_writing_run(
                        sysroot=sysroot,
                        overlay=overlay,
                        runtime=runtime,
                        linker_wrapper=linker_wrapper,
                        commands=commands,
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.side_effect = inspect
                result = _verify_binding_links(
                    cc_wrapper=cc_wrapper,
                    cxx_wrapper=cxx_wrapper,
                    output=output,
                    sysroot=sysroot,
                    overlay=overlay,
                    target_arch="x86_64",
                    expected_interpreter=loader,
                    runtime=runtime,
                )

            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                result["checks"],
                [
                    "c-executable",
                    "c-shared-library",
                    "cxx-executable",
                    "cxx-shared-exception",
                    "c-static-executable",
                    "cxx-static-exception",
                ],
            )
            self.assertTrue(
                all(
                    "-Wl,-t" in command
                    for command in commands
                    if "-Wl,-Map," in " ".join(command)
                )
            )
            static_links = [
                command
                for command in commands
                if any(
                    argument.startswith("-Wl,-Map,") and "static" in argument
                    for argument in command
                )
            ]
            self.assertEqual(len(static_links), 2)
            self.assertTrue(all("-static" in command for command in static_links))

    def test_llvm_runtime_link_rejects_forbidden_gcc_soname(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, sysroot, overlay, cc_wrapper, cxx_wrapper = self.verification_paths(
                root
            )
            runtime = self.llvm_runtime_evidence(root)
            linker_wrapper = output / "linker-bin/ld"
            linker_wrapper.parent.mkdir()
            linker_wrapper.touch()
            loader = "/lib64/ld-linux-x86-64.so.2"
            metadata = SimpleNamespace(
                machine="x86_64",
                elf_class="ELF64",
                endianness="little",
                elf_type="EXEC",
                interpreter=loader,
                rpath=(),
                runpath=(),
                needed=("libgcc_s.so.1",),
            )

            with (
                patch(
                    "linux_toolchain.compiler.binding.run",
                    side_effect=self.llvm_map_writing_run(
                        sysroot=sysroot,
                        overlay=overlay,
                        runtime=runtime,
                        linker_wrapper=linker_wrapper,
                        commands=[],
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.return_value = metadata
                with self.assertRaisesRegex(
                    ExternalToolError, "forbidden GCC runtime SONAMEs"
                ):
                    _verify_binding_links(
                        cc_wrapper=cc_wrapper,
                        cxx_wrapper=cxx_wrapper,
                        output=output,
                        sysroot=sysroot,
                        overlay=overlay,
                        target_arch="x86_64",
                        expected_interpreter=loader,
                        runtime=runtime,
                    )

    def test_runtime_verification_rejects_any_host_target_input(self) -> None:
        for outside_input in (
            Path("/usr/lib/x86_64-linux-gnu/libhost-only.so"),
            Path("/lib64/libhost-only.so"),
            Path("/opt/compiler/lib/libhost-only.so"),
            Path("/home/user/runtime/libhost-only.so"),
        ):
            with (
                self.subTest(outside_input=outside_input),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                output, sysroot, overlay, cc_wrapper, cxx_wrapper = (
                    self.verification_paths(root)
                )
                runtime_root = root / "runtime-export" / "runtime"
                runtime_gcc_dir = runtime_root / "lib/gcc/x86_64-linux-gnu/13.4.0"
                runtime_library_dir = runtime_root / "lib64"
                runtime_gcc_dir.mkdir(parents=True)
                runtime_library_dir.mkdir()
                if outside_input == Path("/lib64/libhost-only.so"):
                    outside_target = root / "outside-sdk" / "libhost-only.so"
                    outside_target.parent.mkdir()
                    outside_target.touch()
                    (sysroot / "lib64").mkdir()
                    (sysroot / "lib64/libhost-only.so").symlink_to(outside_target)
                linker_wrapper = output / "linker-bin" / "ld"
                linker_wrapper.parent.mkdir()
                linker_wrapper.touch()
                commands: list[tuple[str, ...]] = []
                sources: dict[str, str] = {}
                loader = "/lib64/ld-linux-x86-64.so.2"
                metadata = SimpleNamespace(
                    machine="x86_64",
                    elf_class="ELF64",
                    endianness="little",
                    elf_type="EXEC",
                    interpreter=loader,
                    rpath=(),
                    runpath=(),
                )

                with (
                    patch(
                        "linux_toolchain.compiler.binding.run",
                        side_effect=self.map_writing_run(
                            sysroot=sysroot,
                            overlay=overlay,
                            commands=commands,
                            sources=sources,
                            runtime_root=runtime_root,
                            runtime_gcc_dir=runtime_gcc_dir,
                            runtime_library_dir=runtime_library_dir,
                            linker_wrapper=linker_wrapper,
                            extra_host_input_for="c-executable",
                            extra_target_input=outside_input,
                        ),
                    ),
                    patch(
                        "linux_toolchain.compiler.binding.ReadElfInspector"
                    ) as inspector_type,
                ):
                    inspector_type.return_value.inspect.return_value = metadata
                    with self.assertRaisesRegex(
                        ExternalToolError, "build-host target input"
                    ):
                        _verify_binding_links(
                            cc_wrapper=cc_wrapper,
                            cxx_wrapper=cxx_wrapper,
                            output=output,
                            sysroot=sysroot,
                            overlay=overlay,
                            target_arch="x86_64",
                            expected_interpreter=loader,
                            runtime=GccRuntimeLinkEvidence(
                                runtime_root=runtime_root,
                                gcc_runtime_dir=runtime_gcc_dir,
                                library_dirs=(runtime_library_dir,),
                            ),
                        )

                self.assertFalse((output / ".link-validation").exists())

    def test_runtime_verification_accepts_sysroot_absolute_linker_script_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, sysroot, overlay, cc_wrapper, cxx_wrapper = self.verification_paths(
                root
            )
            runtime_root = root / "runtime-export" / "runtime"
            runtime_gcc_dir = runtime_root / "lib/gcc/aarch64-linux-gnu/12.5.0"
            runtime_library_dir = runtime_root / "lib64"
            runtime_gcc_dir.mkdir(parents=True)
            runtime_library_dir.mkdir()
            (sysroot / "lib64").mkdir()
            (sysroot / "lib64/libc.so.6").touch()
            linker_wrapper = output / "linker-bin" / "ld"
            linker_wrapper.parent.mkdir()
            linker_wrapper.touch()
            commands: list[tuple[str, ...]] = []
            sources: dict[str, str] = {}
            loader = "/lib/ld-linux-aarch64.so.1"

            def inspect(binary: Path):
                return SimpleNamespace(
                    machine="aarch64",
                    elf_class="ELF64",
                    endianness="little",
                    elf_type="DYN" if binary.suffix == ".so" else "EXEC",
                    interpreter=(
                        None
                        if binary.suffix == ".so" or "static" in binary.name
                        else loader
                    ),
                    rpath=(),
                    runpath=(),
                )

            with (
                patch(
                    "linux_toolchain.compiler.binding.run",
                    side_effect=self.map_writing_run(
                        sysroot=sysroot,
                        overlay=overlay,
                        commands=commands,
                        sources=sources,
                        runtime_root=runtime_root,
                        runtime_gcc_dir=runtime_gcc_dir,
                        runtime_library_dir=runtime_library_dir,
                        linker_wrapper=linker_wrapper,
                        extra_host_input_for="c-executable",
                        extra_target_input=Path("/lib64/libc.so.6"),
                        extra_trace_input=linker_wrapper,
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.binding.ReadElfInspector"
                ) as inspector_type,
            ):
                inspector_type.return_value.inspect.side_effect = inspect
                result = _verify_binding_links(
                    cc_wrapper=cc_wrapper,
                    cxx_wrapper=cxx_wrapper,
                    output=output,
                    sysroot=sysroot,
                    overlay=overlay,
                    target_arch="aarch64",
                    expected_interpreter=loader,
                    runtime=GccRuntimeLinkEvidence(
                        runtime_root=runtime_root,
                        gcc_runtime_dir=runtime_gcc_dir,
                        library_dirs=(runtime_library_dir,),
                    ),
                    linker_executable=linker_wrapper,
                )

            self.assertEqual(result["status"], "passed")
            self.assertFalse((output / ".link-validation").exists())


if __name__ == "__main__":
    unittest.main()
