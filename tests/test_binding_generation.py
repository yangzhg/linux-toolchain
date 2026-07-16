import json
import os
import platform
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linux_toolchain.compiler.binding import (
    _create_binding,
    _load_runtime_binding,
    create_binding,
)
from linux_toolchain.compiler.managed import (
    COMPILER_KIT_MANIFEST_FORMAT,
    COMPILER_KIT_MANIFEST_SCHEMA,
    TARGET_TOOL_NAMES,
    load_compiler_kit,
)
from linux_toolchain.compiler.managed_binding import create_managed_binding
from linux_toolchain.compiler.toolchain import (
    ArchiveTool,
    CompilerInfo,
    ExecutableIdentity,
    TargetTools,
    _managed_compiler_info,
    _managed_toolchain,
)
from linux_toolchain.elf.models import POLICY_FORMAT, POLICY_SCHEMA
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.integrations import ConanSettings
from linux_toolchain.licenses import license_evidence
from linux_toolchain.managed import ManagedSpec, resolve_lock
from linux_toolchain.managed.contracts import (
    MANAGED_COMPILER_BACKEND_GCC,
    MANAGED_COMPILER_BACKEND_SUPPLEMENTAL_SOURCES,
    MANAGED_TARGET_TOOL_NAMES,
    managed_compiler_backend_spec,
)
from linux_toolchain.managed.identity import (
    managed_action_sha256,
    managed_artifact_action,
    render_action_script,
    runtime_publication_action,
    script_identity,
)
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimePublication,
)
from linux_toolchain.managed.selection import ManagedBuildSelection, select_artifact
from linux_toolchain.recipes import get_recipe
from linux_toolchain.runtime import (
    LLVM_RUNTIME_MANIFEST_SCHEMA,
    RUNTIME_MANIFEST_SCHEMA,
)
from linux_toolchain.sdk.crosstool_ng import sdk_producer_identity
from tests.binding_fixtures import sdk_manifest


class BindingGenerationTest(unittest.TestCase):
    def setUp(self) -> None:
        # These are generation-policy unit tests. Real compiler/linker validation
        # is covered separately against a built SDK; keep these fixtures tiny.
        self.target_tools = TargetTools(
            ar=ArchiveTool("target-ar", Path("/opt/binutils/bin/target-ar")),
            ranlib=ArchiveTool(
                "target-ranlib", Path("/opt/binutils/bin/target-ranlib")
            ),
            assembler=ArchiveTool("target-as", Path("/opt/binutils/bin/target-as")),
            nm=ArchiveTool("target-nm", Path("/opt/binutils/bin/target-nm")),
            strip=ArchiveTool("target-strip", Path("/opt/binutils/bin/target-strip")),
            objcopy=ArchiveTool(
                "target-objcopy", Path("/opt/binutils/bin/target-objcopy")
            ),
            objdump=ArchiveTool(
                "target-objdump", Path("/opt/binutils/bin/target-objdump")
            ),
        )
        self.linker_tool = ArchiveTool(
            "target-ld",
            Path("/opt/binutils/bin/target-ld"),
        )
        resolver = patch(
            "linux_toolchain.compiler.binding._resolve_compiler_target_tools",
            return_value=self.target_tools,
        )
        resolver.start()
        self.addCleanup(resolver.stop)
        identity = patch(
            "linux_toolchain.compiler.binding._capture_executable_identity",
            side_effect=self.executable_identity,
        )
        identity.start()
        self.addCleanup(identity.stop)

        def machine_evidence(*, target_arch: str, **_: object) -> dict[str, object]:
            return {
                "status": "passed",
                "checks": [
                    "target-object",
                    "archive-create",
                    "archive-index",
                    "archive-member-machine",
                    "archive-link",
                ],
                "machine": target_arch,
                "elf_class": "ELF64",
                "endianness": "little",
            }

        def target_tool_evidence(*, target_arch: str, **_: object) -> dict[str, object]:
            return {
                "status": "passed",
                "checks": [
                    "assembler-target-machine",
                    "nm-target-object",
                    "objdump-target-object",
                    "objcopy-target-machine",
                    "strip-target-machine",
                ],
                "machine": target_arch,
                "elf_class": "ELF64",
                "endianness": "little",
            }

        def link_evidence(*, runtime: object, **_: object) -> dict[str, object]:
            checks = ["c-executable", "c-shared-library", "cxx-executable"]
            if runtime is not None:
                checks.extend(
                    (
                        "cxx-shared-exception",
                        "c-static-executable",
                        "cxx-static-exception",
                    )
                )
            return {"status": "passed", "checks": checks}

        archive_verifier = patch(
            "linux_toolchain.compiler.binding._verify_archive_tools",
            side_effect=machine_evidence,
        )
        archive_verifier.start()
        self.addCleanup(archive_verifier.stop)
        target_tool_verifier = patch(
            "linux_toolchain.compiler.binding._verify_target_tools",
            side_effect=target_tool_evidence,
        )
        target_tool_verifier.start()
        self.addCleanup(target_tool_verifier.stop)
        verifier = patch(
            "linux_toolchain.compiler.binding._verify_binding_links",
            side_effect=link_evidence,
        )
        self.link_verifier = verifier.start()
        self.addCleanup(verifier.stop)
        linker = patch(
            "linux_toolchain.compiler.binding._resolve_compiler_tool",
            return_value=self.linker_tool,
        )
        linker.start()
        self.addCleanup(linker.stop)

    @staticmethod
    def executable_identity(value: str | Path, context: str) -> ExecutableIdentity:
        del context
        invocation = Path(os.path.abspath(Path(value).expanduser()))
        return ExecutableIdentity(invocation)

    def create_sdk(
        self,
        root: Path,
        *,
        manifest: dict[str, object] | None = None,
    ) -> Path:
        sdk = root / "sdk"
        sysroot = sdk / "sysroot"
        library = sysroot / "usr" / "lib64"
        library.mkdir(parents=True)
        (sysroot / "usr" / "include").mkdir()
        for name in ("libc.a", "libc.so", "libc.so.6", "crt1.o", "crti.o", "crtn.o"):
            (library / name).touch()
        for component, names in {
            "glibc": ("COPYING", "COPYING.LIB"),
            "linux": ("COPYING",),
            "gcc": ("COPYING", "COPYING.RUNTIME"),
            "binutils": ("COPYING",),
        }.items():
            license_root = sdk / "licenses" / component
            license_root.mkdir(parents=True)
            for name in names:
                (license_root / name).write_text(
                    f"{component} {name}\n", encoding="utf-8"
                )
        manifest_data = json.loads(json.dumps(manifest or sdk_manifest()))
        manifest_data["licenses"] = license_evidence(sdk, context="test SDK")
        (sdk / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")
        return sdk

    def create_runtime(
        self,
        root: Path,
        *,
        arch: str = "x86_64",
        target: str = "x86_64-linux-gnu",
        glibc_floor: str = "2.18",
        major: int = 13,
    ) -> tuple[Path, SimpleNamespace]:
        export = root / "gcc-runtime"
        runtime = export / "runtime"
        version = f"{major}.4.0"
        gcc_dir = runtime / "lib" / "gcc" / target / version
        (gcc_dir / "include").mkdir(parents=True)
        (gcc_dir / "include-fixed").mkdir()
        cxx = runtime / "include" / "c++" / version
        cxx_target = cxx / target
        cxx_backward = cxx / "backward"
        cxx_target.mkdir(parents=True)
        cxx_backward.mkdir()
        library = runtime / "lib64"
        library.mkdir()
        (export / "manifest.json").write_text(
            json.dumps({"schema": RUNTIME_MANIFEST_SCHEMA}) + "\n",
            encoding="utf-8",
        )
        locations = {
            "runtime": "runtime",
            "gcc_runtime_dir": str(gcc_dir.relative_to(export)),
            "library_dirs": (str(library.relative_to(export)),),
            "cxx_include_dirs": (
                str(cxx.relative_to(export)),
                str(cxx_target.relative_to(export)),
                str(cxx_backward.relative_to(export)),
            ),
        }
        manifest = SimpleNamespace(
            provider={"name": "gcc", "version": version, "major": major},
            arch=arch,
            target=target,
            glibc_floor=glibc_floor,
            locations=locations,
            version_symbol_reports=(
                {
                    "path": "runtime/lib64/libstdc++.so.6.0.32",
                    "max_required_versions": {
                        "GLIBC": glibc_floor,
                        "GLIBCXX": None,
                        "CXXABI": None,
                        "GCC": "3.0",
                    },
                },
            ),
        )
        return export, manifest

    def create_compiler_kit(
        self,
        root: Path,
        *,
        provider: str = "gcc",
        version: str = "13.4.0",
        reported_version: str | None = None,
        target: str = "x86_64-portable-linux-gnu",
    ) -> tuple[Path, dict[str, object]]:
        kit = root / "compiler-kit"
        compiler = kit / "compiler"
        bin_dir = compiler / "bin"
        bin_dir.mkdir(parents=True)
        reported_version = reported_version or version

        def executable(name: str, body: str = "exit 0") -> Path:
            path = bin_dir / name
            path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
            path.chmod(0o755)
            return path

        if provider == "gcc":
            cc_name, cxx_name = f"{target}-gcc", f"{target}-g++"
            version_text = f"{target}-gcc (GCC) {reported_version}"
        else:
            cc_name, cxx_name = "clang", "clang++"
            version_text = f"clang version {reported_version}"
        cc = executable(cc_name, f"printf '%s\\n' '{version_text}'")
        cxx = executable(cxx_name, f"printf '%s\\n' '{version_text}'")
        tools = {name: executable(f"{target}-{name}") for name in TARGET_TOOL_NAMES}
        host_machine = platform.machine().lower()
        host_arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            host_machine, host_machine
        )
        manifest: dict[str, object] = {
            "schema": COMPILER_KIT_MANIFEST_SCHEMA,
            "format": COMPILER_KIT_MANIFEST_FORMAT,
            "provider": {
                "name": provider,
                "version": version,
                "major": int(version.split(".", 1)[0]),
            },
            "host": {
                "os": "linux",
                "arch": host_arch,
                "glibc_floor": "2.17",
            },
            "target": {"arch": "x86_64", "triplet": target},
            "locations": {
                "cc": cc.relative_to(kit).as_posix(),
                "cxx": cxx.relative_to(kit).as_posix(),
                "target_tools": {
                    name: path.relative_to(kit).as_posix()
                    for name, path in tools.items()
                },
            },
        }
        (kit / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return kit, manifest

    def create_fixture_managed_binding(
        self,
        sdk: Path,
        output: Path,
        compiler_kit: Path,
        *,
        runtime: Path,
        cppstd: str = "gnu17",
        libcxx: str | None = None,
        build_type: str = "Release",
        managed_evidence: dict[str, object] | None = None,
        force: bool = False,
    ) -> Path:
        """Exercise binding generation after managed provenance is validated.

        Lock, artifact, and publication-receipt validation has dedicated tests;
        these policy fixtures focus on wrapper/profile generation with tiny fake
        executables and runtime manifests.
        """

        kit = load_compiler_kit(compiler_kit)
        return _create_binding(
            sdk,
            output,
            _managed_compiler_info(kit),
            runtime=runtime,
            toolchain=_managed_toolchain(kit),
            managed_evidence=managed_evidence,
            integrations=("cmake", "shell", "conan"),
            conan=ConanSettings(
                cppstd=cppstd,
                libcxx=libcxx,
                build_type=build_type,
            ),
            force=force,
        )

    def create_managed_binding_fixture(self, root: Path) -> SimpleNamespace:
        sdk_data = sdk_manifest()
        target = sdk_data["target"]
        assert isinstance(target, dict)
        target["libc_version"] = "2.19"
        sdk = self.create_sdk(root, manifest=sdk_data)
        kit, _ = self.create_compiler_kit(root, version="13.4.0")
        runtime, runtime_manifest = self.create_runtime(
            root,
            target="x86_64-portable-linux-gnu",
            glibc_floor="2.19",
        )
        lock = resolve_lock(
            ManagedSpec.from_dict(
                {
                    "schema": "linux-toolchain-managed-spec",
                    "format": 1,
                    "name": "binding-template-test",
                    "build_platform": "linux/amd64",
                    "host": {
                        "os": "linux",
                        "arch": "x86_64",
                        "glibc_floor": "2.17",
                    },
                    "targets": [{"arch": "x86_64", "glibc_floor": "2.19"}],
                    "compilers": [
                        {
                            "family": "gcc",
                            "versions": ["13"],
                            "runtimes": [{"kind": "libstdc++"}],
                        }
                    ],
                }
            )
        )
        variant = lock.variants[0]
        compiler_selection = select_artifact(lock, variant.compiler_kit_id)
        runtime_selection = select_artifact(lock, variant.runtime_id)

        selected_sdk = get_recipe("x86_64", "2.19").to_spec()
        backend_spec = managed_compiler_backend_spec("x86_64", "2.17")
        compiler_backend = {
            "sdk": sdk_producer_identity(backend_spec),
            "supplemental_sources": [
                {"filename": filename, "sha256": sha256}
                for filename, sha256 in sorted(
                    MANAGED_COMPILER_BACKEND_SUPPLEMENTAL_SOURCES.items()
                )
            ],
        }

        def raw_action(selection: ManagedBuildSelection) -> dict[str, object]:
            return managed_artifact_action(
                selection,
                sdk=sdk_producer_identity(selected_sdk),
                target_tools={
                    "triplet": selected_sdk.target.triplet,
                    "tools": list(MANAGED_TARGET_TOOL_NAMES),
                },
                compiler_backend=compiler_backend,
                script=script_identity(
                    render_action_script(
                        selection,
                        triplet=selected_sdk.target.triplet,
                        backend_triplet=backend_spec.target.triplet,
                        backend_version=MANAGED_COMPILER_BACKEND_GCC,
                    )
                ),
            )

        compiler_action = raw_action(compiler_selection)
        runtime_action = raw_action(runtime_selection)
        raw_runtime_identity = managed_action_sha256(runtime_action)
        publication_action = runtime_publication_action(
            raw_runtime_identity,
            adapter="import_gcc_runtime",
        )
        runtime_receipt = {
            "publication_action": publication_action,
            "publication_action_sha256": managed_action_sha256(publication_action),
        }
        runtime_evidence = {
            "raw_action_sha256": raw_runtime_identity,
            "publication_action_sha256": runtime_receipt["publication_action_sha256"],
        }
        expected_evidence = {
            "lock_sha256": lock.sha256,
            "variant": variant.to_dict(),
            "compiler_artifact": {
                "action_sha256": managed_action_sha256(compiler_action)
            },
            "runtime_artifact": runtime_evidence,
        }
        compiler_artifact = ManagedCompilerArtifact(
            root=kit.resolve(),
            manifest_path=(kit / "artifact.json").resolve(),
            payload=(kit / "compiler").resolve(),
            selection=compiler_selection,
            target=selected_sdk.target.triplet,
            manifest={"action": compiler_action},
            compiler_kit=load_compiler_kit(kit, check_host=False),
        )
        runtime_publication = ManagedRuntimePublication(
            root=runtime.resolve(),
            manifest_path=(runtime / "manifest.json").resolve(),
            selection=runtime_selection,
            receipt=runtime_receipt,
            manifest=runtime_manifest,
        )
        output = root / "managed-binding"
        with (
            patch(
                "linux_toolchain.compiler.managed_binding.load_managed_compiler_artifact",
                return_value=compiler_artifact,
            ),
            patch(
                "linux_toolchain.compiler.managed_binding.load_managed_runtime_publication",
                return_value=runtime_publication,
            ),
            patch(
                "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                return_value=runtime_manifest,
            ),
        ):
            create_managed_binding(
                sdk,
                output,
                kit,
                lock=lock,
                variant=variant.id,
                runtime=runtime,
                integrations=("cmake", "shell", "conan"),
                conan=ConanSettings(cppstd="gnu17", build_type="Release"),
            )
        return SimpleNamespace(
            binding=output,
            evidence=expected_evidence,
        )

    def test_managed_binding_records_raw_and_publication_action_identities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.create_managed_binding_fixture(Path(directory))

            manifest = json.loads(
                (fixture.binding / "binding.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["managed"], fixture.evidence)

    def create_llvm_runtime(
        self,
        root: Path,
        *,
        version: str = "22.1.8",
        target: str = "x86_64-portable-linux-gnu",
        glibc_floor: str = "2.18",
    ) -> tuple[Path, SimpleNamespace]:
        export = root / "llvm-runtime"
        runtime = export / "runtime"
        cxx_include = runtime / "include/c++/v1"
        resource_dir = runtime / f"lib/clang/{version}"
        library_dir = runtime / "lib"
        builtins = resource_dir / "lib/linux/libclang_rt.builtins-x86_64.a"
        cxx_include.mkdir(parents=True)
        (resource_dir / "include").mkdir(parents=True)
        library_dir.mkdir(parents=True, exist_ok=True)
        builtins.parent.mkdir(parents=True)
        builtins.touch()
        crt_objects = tuple(
            resource_dir / "lib/linux" / f"clang_rt.crt{kind}-x86_64.o"
            for kind in ("begin", "end")
        )
        for crt_object in crt_objects:
            crt_object.touch()
        libraries = tuple(
            library_dir / f"{name}.so.1"
            for name in ("libc++", "libc++abi", "libunwind")
        )
        for library in libraries:
            library.touch()
        static_libraries = tuple(
            library_dir / f"{name}.a" for name in ("libc++", "libc++abi", "libunwind")
        )
        for library in static_libraries:
            library.touch()
        report_path = libraries[0].relative_to(export).as_posix()
        locations = {
            "runtime": "runtime",
            "cxx_include_dirs": (cxx_include.relative_to(export).as_posix(),),
            "resource_dir": resource_dir.relative_to(export).as_posix(),
            "library_dirs": (library_dir.relative_to(export).as_posix(),),
            "shared_libraries": tuple(
                path.relative_to(export).as_posix() for path in libraries
            ),
            "static_libraries": tuple(
                path.relative_to(export).as_posix() for path in static_libraries
            ),
            "builtins": builtins.relative_to(export).as_posix(),
            "crt_objects": tuple(
                path.relative_to(export).as_posix() for path in crt_objects
            ),
        }
        manifest = SimpleNamespace(
            provider={
                "name": "llvm",
                "version": version,
                "major": int(version.split(".", 1)[0]),
            },
            arch="x86_64",
            target=target,
            glibc_floor=glibc_floor,
            source={
                "kind": "managed-artifact",
                "version": version,
                "target": target,
            },
            abi={
                "standard_library": "libc++",
                "cxxabi": "libc++abi",
                "unwind": "libunwind",
                "rtlib": "compiler-rt",
                "linkage": "both",
            },
            locations=locations,
            forbidden_sonames=("libgcc_s.so.1", "libstdc++.so.6"),
            version_symbol_reports=(
                {
                    "path": report_path,
                    "max_required_versions": {
                        "GLIBC": glibc_floor,
                        "GLIBCXX": None,
                        "CXXABI": None,
                        "GCC": None,
                    },
                },
            ),
            validation={
                "payload": "passed",
                "final_link": "binding-required",
            },
        )
        (export / "manifest.json").write_text(
            json.dumps({"schema": LLVM_RUNTIME_MANIFEST_SCHEMA}) + "\n",
            encoding="utf-8",
        )
        return export, manifest

    def test_runtime_dispatch_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "manifest.json").write_text(
                json.dumps({"schema": "another-runtime"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "unsupported.*schema"):
                _load_runtime_binding(runtime)

    def test_generates_selected_integrations_and_runtime_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )

            manifest_path = create_binding(
                sdk,
                output,
                compiler,
                integrations=("cmake", "shell", "conan"),
                conan=ConanSettings(),
            )
            binding_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(binding_manifest["sdk"]["glibc_version"], "2.18")

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(manifest_path.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((output / "conan").stat().st_mode), 0o755)
            self.assertEqual(
                stat.S_IMODE((output / "conan" / "host.profile").stat().st_mode),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE((output / "bin" / "cc").stat().st_mode), 0o755
            )
            profile = (output / "conan" / "host.profile").read_text(encoding="utf-8")
            self.assertNotIn("include(default)", profile)
            self.assertIn("build_type=Release", profile)
            self.assertIn("os.libc=gnu", profile)
            self.assertIn("os.libc_version=2.18", profile)
            self.assertIn("os.kernel_headers_version=3.10.108", profile)
            self.assertIn("os.minimum_kernel_version=3.2.0", profile)
            self.assertIn("compiler.version=13", profile)
            self.assertIn(
                "compiler.cppstd=gnu17",
                profile,
            )
            self.assertIn("tools.build:sysroot=", profile)
            self.assertIn("[buildenv]", profile)
            self.assertIn(f"CC={output / 'bin' / 'cc'}", profile)
            self.assertIn(f"CXX={output / 'bin' / 'c++'}", profile)
            self.assertIn(f'"asm":"{output / "bin" / "cc"}"', profile)
            self.assertNotIn(f'"asm":"{output / "bin" / "as"}"', profile)
            self.assertIn(f"AR={output / 'bin' / 'ar'}", profile)
            self.assertIn(f"RANLIB={output / 'bin' / 'ranlib'}", profile)
            for name, variable in (
                ("as", "AS"),
                ("nm", "NM"),
                ("strip", "STRIP"),
                ("objcopy", "OBJCOPY"),
                ("objdump", "OBJDUMP"),
            ):
                self.assertIn(f"{variable}={output / 'bin' / name}", profile)
            self.assertIn(f"PATH=+(path){output / 'bin'}", profile)

            wrapper = (output / "bin" / "c++").read_text(encoding="utf-8")
            self.assertIn("exec /opt/compiler/bin/g++", wrapper)
            self.assertIn(f"--sysroot={sdk / 'sysroot'}", wrapper)
            self.assertTrue((output / "bin" / "gcc").is_symlink())
            self.assertTrue((output / "bin" / "g++").is_symlink())
            self.assertFalse((output / "bin" / "clang").exists())
            self.assertFalse((output / "bin" / "clang++").exists())
            for name in ("ar", "ranlib", "as", "nm", "strip", "objcopy", "objdump"):
                self.assertTrue((output / "bin" / name).is_symlink())

            for fragment_path in (
                output / "cmake/toolchain.cmake",
                output / "conan/cmake-toolchain.cmake",
                output / "conan/cmake-late.cmake",
            ):
                fragment = fragment_path.read_text(encoding="utf-8")
                for variable in (
                    "CMAKE_AR",
                    "CMAKE_RANLIB",
                    "CMAKE_C_COMPILER_AR",
                    "CMAKE_CXX_COMPILER_AR",
                    "CMAKE_C_COMPILER_RANLIB",
                    "CMAKE_CXX_COMPILER_RANLIB",
                    "CMAKE_ASM_COMPILER",
                    "CMAKE_NM",
                    "CMAKE_STRIP",
                    "CMAKE_OBJCOPY",
                    "CMAKE_OBJDUMP",
                ):
                    self.assertIn(variable, fragment)
                self.assertIn(str(output / "bin" / "ar"), fragment)
                self.assertIn(str(output / "bin" / "ranlib"), fragment)
                self.assertIn(
                    f"set(CMAKE_ASM_COMPILER [=[{output / 'bin' / 'cc'}]=]",
                    fragment,
                )
                self.assertNotIn(".binding.staging-", fragment)

            direct_toolchain = (output / "cmake" / "toolchain.cmake").read_text(
                encoding="utf-8"
            )
            self.assertIn("set(CMAKE_SYSTEM_NAME Linux)", direct_toolchain)
            self.assertIn("set(CMAKE_SYSTEM_PROCESSOR x86_64)", direct_toolchain)
            self.assertIn("CMAKE_SYSROOT", direct_toolchain)
            self.assertIn("CMAKE_C_COMPILER", direct_toolchain)
            self.assertIn("CMAKE_CXX_COMPILER", direct_toolchain)
            self.assertIn("CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY", direct_toolchain)

            environment = (output / "env" / "toolchain.env").read_text(encoding="utf-8")
            self.assertIn(
                "export LINUX_TOOLCHAIN_TARGET=x86_64-portable-linux-gnu",
                environment,
            )
            self.assertIn(f"export CC={output / 'bin' / 'cc'}", environment)
            self.assertIn(
                f"export CMAKE_TOOLCHAIN_FILE={output / 'cmake' / 'toolchain.cmake'}",
                environment,
            )
            self.assertIn("unset PKG_CONFIG_PATH", environment)
            self.assertNotIn(".binding.staging-", environment)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["compiler"]["toolchain"],
                {"mode": "external"},
            )
            self.assertEqual(manifest["compatibility_scope"], "glibc-floor")
            self.assertEqual(manifest["schema"], "linux-toolchain-binding")
            self.assertEqual(manifest["format"], 1)
            self.assertEqual(manifest["cxx_runtime"]["policy"], "external-unpinned")
            self.assertEqual(manifest["cxx_runtime"]["kind"], "compiler-default")
            self.assertEqual(
                manifest["compiler"]["drivers"]["c"]["invocation_path"],
                "/opt/compiler/bin/gcc",
            )
            self.assertEqual(
                manifest["compiler"]["tools"]["ar"],
                {
                    "reported_program": "target-ar",
                    "invocation_path": "/opt/binutils/bin/target-ar",
                    "wrapper": str(output / "bin" / "ar"),
                },
            )
            self.assertEqual(
                manifest["integrations"],
                {
                    "cmake": {"toolchain": "cmake/toolchain.cmake"},
                    "shell": {"environment": "env/toolchain.env"},
                    "conan": {
                        "host_profile": "conan/host.profile",
                        "cmake_toolchain": "conan/cmake-toolchain.cmake",
                        "cmake_late": "conan/cmake-late.cmake",
                        "settings": {
                            "cppstd": "gnu17",
                            "libcxx": "libstdc++11",
                            "build_type": "Release",
                        },
                    },
                },
            )
            self.assertEqual(manifest["validation"]["archive"]["status"], "passed")
            self.assertEqual(
                set(manifest["compiler"]["aliases"]),
                {
                    "cc",
                    "c++",
                    "gcc",
                    "g++",
                    "ar",
                    "ranlib",
                    "as",
                    "nm",
                    "strip",
                    "objcopy",
                    "objdump",
                },
            )

    def test_managed_binding_uses_only_manifest_selected_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            kit, kit_manifest = self.create_compiler_kit(root)
            target = "x86_64-portable-linux-gnu"
            runtime, runtime_manifest = self.create_runtime(root, target=target)
            output = root / "binding"

            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                patch(
                    "linux_toolchain.compiler.binding._resolve_compiler_target_tools",
                    side_effect=AssertionError(
                        "managed binding must not discover target tools"
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.binding._resolve_compiler_tool",
                    side_effect=AssertionError(
                        "managed binding must not discover target tools"
                    ),
                ),
            ):
                manifest_path = self.create_fixture_managed_binding(
                    sdk,
                    output,
                    kit,
                    runtime=runtime,
                )

            profile = (output / "conan" / "host.profile").read_text(encoding="utf-8")
            self.assertIn("compiler.cppstd=gnu17", profile)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            provenance = manifest["compiler"]["toolchain"]
            self.assertEqual(provenance["mode"], "managed")
            self.assertEqual(provenance["path"], str(kit.resolve()))
            self.assertEqual(
                provenance["manifest_path"],
                str((kit / "manifest.json").resolve()),
            )
            self.assertEqual(provenance["provider"], kit_manifest["provider"])
            self.assertEqual(provenance["host"], kit_manifest["host"])
            self.assertEqual(provenance["target"], kit_manifest["target"])
            self.assertEqual(
                manifest["compiler"]["tools"]["selection"],
                "compiler-kit",
            )
            locations = kit_manifest["locations"]
            assert isinstance(locations, dict)
            tool_locations = locations["target_tools"]
            assert isinstance(tool_locations, dict)
            self.assertEqual(
                manifest["compiler"]["tools"]["ld"]["invocation_path"],
                str((kit / str(tool_locations["ld"])).resolve()),
            )
            self.assertEqual(
                self.link_verifier.call_args.kwargs["linker_executable"],
                (kit / str(tool_locations["ld"])).resolve(),
            )

    def test_managed_binding_rejects_driver_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            kit, _ = self.create_compiler_kit(
                root,
                version="13.4.0",
                reported_version="13.3.0",
            )
            runtime, _ = self.create_runtime(root, target="x86_64-portable-linux-gnu")

            with self.assertRaisesRegex(
                ConfigurationError, "C driver identity mismatch"
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "binding",
                    kit,
                    runtime=runtime,
                )

            self.assertFalse((root / "binding").exists())

    def test_managed_clang_can_use_a_pinned_gcc_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            kit, _ = self.create_compiler_kit(
                root,
                provider="clang",
                version="22.1.8",
            )
            runtime, runtime_manifest = self.create_runtime(
                root,
                major=13,
                target="x86_64-portable-linux-gnu",
            )
            output = root / "binding"

            with patch(
                "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                return_value=runtime_manifest,
            ):
                manifest_path = self.create_fixture_managed_binding(
                    sdk,
                    output,
                    kit,
                    runtime=runtime,
                )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["compiler"]["family"], "clang")
            self.assertEqual(
                manifest["compiler"]["toolchain"]["provider"]["version"],
                "22.1.8",
            )
            self.assertEqual(manifest["cxx_runtime"]["provider"]["version"], "13.4.0")

    def test_managed_clang_pins_shared_llvm_runtime_and_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            kit, kit_manifest = self.create_compiler_kit(
                root, provider="clang", version="22.1.8"
            )
            driver_script = (
                "#!/bin/sh\n"
                'if [ "${1-}" = --version ]; then\n'
                "  printf '%s\\n' 'clang version 22.1.8'\n"
                "else\n"
                "  printf '%s\\n' \"$@\"\n"
                "fi\n"
            )
            for name in ("cc", "cxx"):
                driver = kit / kit_manifest["locations"][name]
                driver.write_text(driver_script, encoding="utf-8")
            (kit / "manifest.json").write_text(
                json.dumps(kit_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            runtime, runtime_manifest = self.create_llvm_runtime(root)
            output = root / "binding"

            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_llvm_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                    side_effect=AssertionError(
                        "LLVM schema must not use the GCC runtime validator"
                    ),
                ),
            ):
                manifest_path = self.create_fixture_managed_binding(
                    sdk,
                    output,
                    kit,
                    runtime=runtime,
                    libcxx="libc++",
                )

            c_wrapper = (output / "bin/cc").read_text(encoding="utf-8")
            cxx_wrapper = (output / "bin/c++").read_text(encoding="utf-8")
            resource_dir = (
                runtime / runtime_manifest.locations["resource_dir"]
            ).resolve()
            cxx_include = (
                runtime / runtime_manifest.locations["cxx_include_dirs"][0]
            ).resolve()
            for expected in (
                "--target=x86_64-portable-linux-gnu",
                f"-resource-dir={resource_dir}",
                "--rtlib=compiler-rt",
                "--unwindlib=libunwind",
            ):
                self.assertIn(expected, c_wrapper)
                self.assertIn(expected, cxx_wrapper)
            self.assertIn("-stdlib=libc++", cxx_wrapper)
            self.assertIn("-nostdinc++", cxx_wrapper)
            self.assertIn(str(cxx_include), cxx_wrapper)

            cc = output / "bin/cc"
            dynamic_arguments = subprocess.run(
                [cc, "probe.o"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
            static_arguments = subprocess.run(
                [cc, "probe.o", "-static"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
            static_dependencies = (
                "-pthread",
                "-Wl,--undefined=_Unwind_Resume",
                "-Wl,--undefined=dladdr",
                "-ldl",
            )
            self.assertTrue(
                all(
                    argument not in dynamic_arguments
                    for argument in static_dependencies
                )
            )
            self.assertEqual(static_arguments[-5:-1], list(static_dependencies))

            profile = (output / "conan/host.profile").read_text(encoding="utf-8")
            self.assertIn("compiler.libcxx=libc++", profile)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pinned = manifest["cxx_runtime"]
            self.assertEqual(pinned["policy"], "pinned-llvm-runtime")
            self.assertEqual(pinned["source"], runtime_manifest.source)
            self.assertEqual(pinned["abi"], runtime_manifest.abi)
            self.assertEqual(
                pinned["forbidden_sonames"],
                list(runtime_manifest.forbidden_sonames),
            )
            self.assertEqual(pinned["validation"], runtime_manifest.validation)

    def test_llvm_runtime_requires_matching_managed_clang_and_libcxx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_llvm_runtime(root)
            clang_kit, _ = self.create_compiler_kit(
                root, provider="clang", version="22.1.8"
            )
            runtime_manifest.provider["version"] = "21.1.8"
            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_llvm_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError,
                    "Clang version 22.1.8.*runtime version 21.1.8",
                ),
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "version-mismatch",
                    clang_kit,
                    runtime=runtime,
                    libcxx="libc++",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_llvm_runtime(root)
            gcc_kit, _ = self.create_compiler_kit(root)
            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_llvm_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError, "managed Clang Compiler Kit"
                ),
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "family-mismatch",
                    gcc_kit,
                    runtime=runtime,
                    libcxx="libc++",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_llvm_runtime(root)
            clang_kit, _ = self.create_compiler_kit(
                root, provider="clang", version="22.1.8"
            )
            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_llvm_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError, "requires Conan libcxx='libc\\+\\+'"
                ),
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "libcxx-mismatch",
                    clang_kit,
                    runtime=runtime,
                    libcxx="libstdc++11",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_llvm_runtime(
                root, glibc_floor="2.17"
            )
            clang_kit, _ = self.create_compiler_kit(
                root, provider="clang", version="22.1.8"
            )
            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_llvm_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError,
                    "runtime glibc floor 2.17.*SDK glibc floor 2.18",
                ),
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "floor-mismatch",
                    clang_kit,
                    runtime=runtime,
                    libcxx="libc++",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_llvm_runtime(root)
            external_clang = CompilerInfo(
                family="clang",
                version="22.1.8",
                major=22,
                target="x86_64-portable-linux-gnu",
                cc=Path("/opt/llvm/bin/clang"),
                cxx=Path("/opt/llvm/bin/clang++"),
                version_text="clang version 22.1.8",
            )
            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_llvm_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError, "requires a managed Clang Compiler Kit"
                ),
            ):
                create_binding(
                    sdk,
                    root / "external-clang",
                    external_clang,
                    runtime=runtime,
                )

    def test_compiler_kit_must_be_disjoint_from_binding_inputs_and_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kit, _ = self.create_compiler_kit(root)
            sdk = self.create_sdk(kit)
            runtime, runtime_manifest = self.create_runtime(
                root, target="x86_64-portable-linux-gnu"
            )

            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError, "Compiler Kit and SDK directories"
                ),
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "binding",
                    kit,
                    runtime=runtime,
                )

    def test_managed_target_must_match_both_sdk_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            kit, _ = self.create_compiler_kit(root, target="x86_64-other-linux-gnu")
            runtime, runtime_manifest = self.create_runtime(
                root, target="x86_64-other-linux-gnu"
            )
            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError,
                    "Compiler Kit target does not match the selected SDK target",
                ),
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "binding-sdk-mismatch",
                    kit,
                    runtime=runtime,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            kit, _ = self.create_compiler_kit(root)
            runtime, runtime_manifest = self.create_runtime(
                root, target="x86_64-linux-gnu"
            )
            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(
                    ConfigurationError, "runtime target.*Compiler Kit target"
                ),
            ):
                self.create_fixture_managed_binding(
                    sdk,
                    root / "binding-runtime-mismatch",
                    kit,
                    runtime=runtime,
                )

    def test_gcc_binding_pins_imported_runtime_and_uses_hermetic_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_runtime(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )

            with patch(
                "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                return_value=runtime_manifest,
            ):
                manifest_path = create_binding(
                    sdk,
                    output,
                    compiler,
                    runtime=runtime,
                    integrations=("cmake", "shell", "conan"),
                    conan=ConanSettings(),
                )

            cc_wrapper = (output / "bin" / "cc").read_text(encoding="utf-8")
            cxx_wrapper = (output / "bin" / "c++").read_text(encoding="utf-8")
            gcc_dir = (
                runtime / runtime_manifest.locations["gcc_runtime_dir"]
            ).resolve()
            cxx_include = (
                runtime / runtime_manifest.locations["cxx_include_dirs"][0]
            ).resolve()
            runtime_library = (
                runtime / runtime_manifest.locations["library_dirs"][0]
            ).resolve()
            self.assertIn(f"-B{gcc_dir}/", cc_wrapper)
            self.assertIn("-nostdinc", cc_wrapper)
            self.assertIn(str((sdk / "sysroot/usr/include").resolve()), cc_wrapper)
            self.assertNotIn(str(cxx_include), cc_wrapper)
            self.assertIn(str(cxx_include), cxx_wrapper)
            self.assertIn(f"-L{runtime_library}", cxx_wrapper)
            self.assertNotIn("-Wl,-rpath,", cxx_wrapper)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            linker = output / "bin" / "ld"
            self.assertTrue(linker.is_symlink())
            linker_identity = manifest["compiler"]["tools"]["ld"]
            self.assertEqual(
                linker_identity["invocation_path"],
                "/opt/binutils/bin/target-ld",
            )
            self.assertEqual(
                set(manifest["compiler"]["aliases"]),
                {
                    "cc",
                    "c++",
                    "gcc",
                    "g++",
                    "ar",
                    "ranlib",
                    "as",
                    "nm",
                    "strip",
                    "objcopy",
                    "objdump",
                    "ld",
                },
            )
            pinned = manifest["cxx_runtime"]
            self.assertEqual(pinned["policy"], "pinned-gcc-runtime")
            self.assertEqual(pinned["provider"]["major"], 13)
            self.assertEqual(
                pinned["version_symbol_reports"],
                list(runtime_manifest.version_symbol_reports),
            )
            for generated in (
                output / "bin" / "cc",
                output / "bin" / "c++",
                output / "bin" / "gcc",
                output / "bin" / "g++",
                output / "conan" / "host.profile",
                manifest_path,
            ):
                self.assertNotIn(
                    ".binding.staging-", generated.read_text(encoding="utf-8")
                )
            self.assertEqual(list(root.glob(".binding.staging-*")), [])
            self.assertEqual(list(root.glob(".binding.backup-*")), [])

    def test_clang_binding_reuses_imported_gcc_runtime_without_gcc_frontend(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_runtime(root)
            output = root / "binding"
            fake = root / "fake-clang"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
            fake.chmod(0o755)
            compiler = CompilerInfo(
                family="clang",
                version="22.0.0",
                major=22,
                target="x86_64-linux-gnu",
                cc=fake,
                cxx=fake,
                version_text="clang version 22.0.0",
            )

            with patch(
                "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                return_value=runtime_manifest,
            ):
                create_binding(
                    sdk,
                    output,
                    compiler,
                    runtime=runtime,
                    integrations=("cmake", "shell", "conan"),
                    conan=ConanSettings(),
                )

            cxx_include = (
                runtime / runtime_manifest.locations["cxx_include_dirs"][0]
            ).resolve()
            cc_wrapper = (output / "bin" / "cc").read_text(encoding="utf-8")
            cxx_wrapper = (output / "bin" / "c++").read_text(encoding="utf-8")
            self.assertIn("--target=x86_64-linux-gnu", cc_wrapper)
            gcc_dir = (
                runtime / runtime_manifest.locations["gcc_runtime_dir"]
            ).resolve()
            self.assertIn(f"--gcc-install-dir={gcc_dir}", cc_wrapper)
            self.assertIn("--driver-mode=gcc", cc_wrapper)
            self.assertIn("--rtlib=libgcc", cc_wrapper)
            self.assertIn("--no-default-config", cc_wrapper)
            self.assertNotIn("-nostdinc", cc_wrapper)
            self.assertNotIn(str(cxx_include), cc_wrapper)
            self.assertIn("-nostdinc++", cxx_wrapper)
            self.assertIn(str(cxx_include), cxx_wrapper)
            self.assertIn("-stdlib=libstdc++", cxx_wrapper)
            self.assertIn("--driver-mode=g++", cxx_wrapper)
            self.assertIn("--no-default-config", cxx_wrapper)
            environment = {"PATH": os.environ.get("PATH", "")}
            runtime_library = (
                runtime / runtime_manifest.locations["library_dirs"][0]
            ).resolve()
            sdk_library = (sdk / "sysroot/usr/lib64").resolve()
            link_only_flags = (
                f"-B{output / 'glibc-startfiles'}/",
                f"--ld-path={output / 'bin/ld'}",
                "--rtlib=libgcc",
                "--unwindlib=libgcc",
                "-stdlib=libstdc++",
                f"-L{runtime_library}",
                f"-L{sdk_library}",
                "-Wl,-rpath-link," + ":".join((str(runtime_library), str(sdk_library))),
            )
            for mode in ("-c", "-S", "-E", "-M", "-MM", "-fsyntax-only"):
                with self.subTest(mode=mode):
                    compile_only = subprocess.run(
                        [output / "bin/c++", mode, "probe.cc"],
                        check=False,
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    self.assertEqual(compile_only.returncode, 0, compile_only.stderr)
                    compile_arguments = compile_only.stdout.splitlines()
                    for flag in link_only_flags:
                        self.assertNotIn(flag, compile_arguments)
                    self.assertIn("--target=x86_64-linux-gnu", compile_arguments)
                    self.assertIn("--driver-mode=g++", compile_arguments)
                    self.assertNotIn("-stdlib=libstdc++", compile_arguments)

            link = subprocess.run(
                [output / "bin/c++", "probe.cc"],
                check=False,
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(link.returncode, 0, link.stderr)
            link_arguments = link.stdout.splitlines()
            for flag in link_only_flags:
                self.assertIn(flag, link_arguments)

    def test_gcc_frontend_major_must_match_imported_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_runtime(root, major=12)
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )

            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(ConfigurationError, "major 13.*major 12"),
            ):
                create_binding(sdk, root / "binding", compiler, runtime=runtime)

    def test_runtime_floor_must_not_be_newer_than_sdk_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            runtime, runtime_manifest = self.create_runtime(root, glibc_floor="2.36")
            compiler = CompilerInfo(
                family="clang",
                version="22.0.0",
                major=22,
                target="x86_64-linux-gnu",
                cc=Path("/opt/llvm/bin/clang"),
                cxx=Path("/opt/llvm/bin/clang++"),
                version_text="clang version 22.0.0",
            )

            with (
                patch(
                    "linux_toolchain.compiler.runtime_binding.validate_runtime_manifest",
                    return_value=runtime_manifest,
                ),
                self.assertRaisesRegex(ConfigurationError, "2.36.*newer.*2.18"),
            ):
                create_binding(sdk, root / "binding", compiler, runtime=runtime)

    def test_late_cmake_fragment_applies_after_conan_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )
            create_binding(
                sdk,
                output,
                compiler,
                integrations=("conan",),
                conan=ConanSettings(),
            )

            early = output / "conan" / "cmake-toolchain.cmake"
            late = output / "conan" / "cmake-late.cmake"
            self.assertIn("CMAKE_PROJECT_TOP_LEVEL_INCLUDES", early.read_text())
            self.assertNotIn("CMAKE_FIND_ROOT_PATH_MODE_PACKAGE", early.read_text())
            self.assertIn("CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY", late.read_text())

            generators = root / "build" / "generators"
            generators.mkdir(parents=True)
            dependency_root = root / "fake-conan-dependency"
            dependency_root.mkdir()
            conan_prefix = root / "fake-conan-prefix"
            conan_prefix_config = conan_prefix / "lib" / "cmake" / "FakeConanPrefix"
            conan_prefix_config.mkdir(parents=True)
            conan_prefix_config.joinpath("FakeConanPrefixConfig.cmake").write_text(
                "set(FAKE_CONAN_PREFIX_LOADED TRUE)\n", encoding="utf-8"
            )
            conan_include = root / "fake-conan-include"
            conan_include.mkdir()
            conan_include.joinpath("fake_conan_header.h").write_text(
                "/* fake Conan header */\n", encoding="utf-8"
            )
            conan_library = root / "fake-conan-library"
            conan_library.mkdir()
            conan_library.joinpath("libfake_conan_library.a").write_bytes(b"")
            conan_program = root / "fake-conan-program"
            conan_program.mkdir()
            missing_conan_path = root / "missing-conan-path"
            fake_toolchain = generators / "conan_toolchain.cmake"
            generators.joinpath("FakeConanDependencyConfig.cmake").write_text(
                "set(FAKE_CONAN_DEPENDENCY_LOADED TRUE)\n", encoding="utf-8"
            )
            fake_toolchain.write_text(
                "\n".join(
                    (
                        f'include("{early.as_posix()}")',
                        f'set(CMAKE_SYSROOT "{(sdk / "sysroot").as_posix()}")',
                        f'list(APPEND CMAKE_FIND_ROOT_PATH "{dependency_root.as_posix()}")',
                        (
                            f'list(APPEND CMAKE_PREFIX_PATH "{conan_prefix.as_posix()}" '
                            f'"{missing_conan_path.as_posix()}")'
                        ),
                        f'list(APPEND CMAKE_LIBRARY_PATH "{conan_library.as_posix()}")',
                        f'list(APPEND CMAKE_INCLUDE_PATH "{conan_include.as_posix()}")',
                        f'list(APPEND CMAKE_PROGRAM_PATH "{conan_program.as_posix()}")',
                        # Simulate Conan assigning these after its user-toolchain
                        # include; the deferred fragment must win.
                        "set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY BOTH)",
                        "set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE BOTH)",
                        "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE BOTH)",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            source = root / "source"
            source.mkdir()
            result_file = root / "cmake-result.txt"
            source.joinpath("CMakeLists.txt").write_text(
                "\n".join(
                    (
                        "cmake_minimum_required(VERSION 3.25)",
                        "project(binding_order LANGUAGES NONE)",
                        "find_package(FakeConanDependency CONFIG REQUIRED)",
                        "find_package(FakeConanPrefix CONFIG REQUIRED)",
                        (
                            "find_path(FAKE_CONAN_INCLUDE "
                            "NAMES fake_conan_header.h REQUIRED)"
                        ),
                        (
                            "find_library(FAKE_CONAN_LIBRARY "
                            "NAMES fake_conan_library REQUIRED)"
                        ),
                        f'file(WRITE "{result_file.as_posix()}"',
                        '  "roots=${CMAKE_FIND_ROOT_PATH}\\n"',
                        '  "program=${CMAKE_FIND_ROOT_PATH_MODE_PROGRAM}\\n"',
                        '  "library=${CMAKE_FIND_ROOT_PATH_MODE_LIBRARY}\\n"',
                        '  "include=${CMAKE_FIND_ROOT_PATH_MODE_INCLUDE}\\n"',
                        '  "package=${CMAKE_FIND_ROOT_PATH_MODE_PACKAGE}\\n"',
                        '  "ar=${CMAKE_AR}\\n"',
                        '  "ranlib=${CMAKE_RANLIB}\\n"',
                        '  "c_ar=${CMAKE_C_COMPILER_AR}\\n"',
                        '  "cxx_ar=${CMAKE_CXX_COMPILER_AR}\\n"',
                        '  "c_ranlib=${CMAKE_C_COMPILER_RANLIB}\\n"',
                        '  "cxx_ranlib=${CMAKE_CXX_COMPILER_RANLIB}\\n"',
                        '  "dependency=${FAKE_CONAN_DEPENDENCY_LOADED}\\n"',
                        '  "prefix=${FAKE_CONAN_PREFIX_LOADED}\\n"',
                        '  "header=${FAKE_CONAN_INCLUDE}\\n"',
                        '  "archive=${FAKE_CONAN_LIBRARY}\\n")',
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            configure = subprocess.run(
                [
                    "cmake",
                    "-S",
                    source,
                    "-B",
                    root / "cmake-build",
                    f"-DCMAKE_TOOLCHAIN_FILE={fake_toolchain}",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(configure.returncode, 0, configure.stderr)

            values = dict(
                line.split("=", 1)
                for line in result_file.read_text(encoding="utf-8").splitlines()
            )
            roots = values["roots"].split(";")
            self.assertEqual(roots[0], str(sdk / "sysroot"))
            self.assertEqual(roots[1], str(generators))
            self.assertIn(str(dependency_root), roots)
            self.assertIn(str(conan_prefix), roots)
            self.assertIn(str(conan_library), roots)
            self.assertIn(str(conan_include), roots)
            self.assertNotIn(str(conan_program), roots)
            self.assertNotIn(str(missing_conan_path), roots)
            self.assertEqual(values["program"], "NEVER")
            self.assertEqual(values["library"], "ONLY")
            self.assertEqual(values["include"], "ONLY")
            self.assertEqual(values["package"], "ONLY")
            self.assertEqual(values["dependency"], "TRUE")
            self.assertEqual(values["prefix"], "TRUE")
            self.assertEqual(values["header"], str(conan_include))
            self.assertEqual(
                values["archive"],
                str(conan_library / "libfake_conan_library.a"),
            )
            self.assertEqual(values["ar"], str(output / "bin" / "ar"))
            self.assertEqual(values["ranlib"], str(output / "bin" / "ranlib"))
            self.assertEqual(values["c_ar"], str(output / "bin" / "ar"))
            self.assertEqual(values["cxx_ar"], str(output / "bin" / "ar"))
            self.assertEqual(values["c_ranlib"], str(output / "bin" / "ranlib"))
            self.assertEqual(values["cxx_ranlib"], str(output / "bin" / "ranlib"))

    def test_generates_aarch64_binding_and_conan_armv8_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(
                root,
                manifest=sdk_manifest(
                    arch="aarch64",
                    triplet="aarch64-portable-linux-gnu",
                    cpu="armv8-a",
                ),
            )
            output = root / "binding"
            compiler = CompilerInfo(
                family="clang",
                version="18.1.8",
                major=18,
                target="aarch64-linux-gnu",
                cc=Path("/opt/llvm/bin/clang"),
                cxx=Path("/opt/llvm/bin/clang++"),
                version_text="clang version 18.1.8",
            )

            manifest_path = create_binding(
                sdk,
                output,
                compiler,
                integrations=("conan",),
                conan=ConanSettings(),
            )

            profile = (output / "conan" / "host.profile").read_text(encoding="utf-8")
            self.assertIn("arch=armv8", profile)
            wrapper = (output / "bin" / "c++").read_text(encoding="utf-8")
            self.assertNotIn("--target=aarch64-portable-linux-gnu", wrapper)
            self.assertNotIn("-march=", wrapper)
            policy = json.loads(
                (output / "audit-policy.json").read_text(encoding="utf-8")
            )
            self.assertEqual(policy["schema"], POLICY_SCHEMA)
            self.assertEqual(policy["format"], POLICY_FORMAT)
            self.assertEqual(policy["machine"], "aarch64")
            self.assertEqual(policy["elf_class"], "ELF64")
            self.assertEqual(policy["endianness"], "little")
            self.assertEqual(
                policy["allowed_interpreters"],
                ["/lib/ld-linux-aarch64.so.1"],
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["sdk"]["triplet"], "aarch64-portable-linux-gnu")

    def test_rejects_x86_compiler_for_aarch64_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(
                root,
                manifest=sdk_manifest(
                    arch="aarch64",
                    triplet="aarch64-portable-linux-gnu",
                    cpu="armv8-a",
                ),
            )
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )
            with self.assertRaisesRegex(
                ConfigurationError, r"compiler.*x86_64.*aarch64"
            ):
                create_binding(sdk, root / "binding", compiler)

    def test_refuses_to_overwrite_existing_binding_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )
            create_binding(sdk, output, compiler)
            with self.assertRaisesRegex(ConfigurationError, "binding.*already exists"):
                create_binding(sdk, output, compiler)

    def test_force_replaces_a_generator_owned_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )
            create_binding(sdk, output, compiler)
            stale = output / "stale-generated-file"
            stale.touch()

            manifest = create_binding(sdk, output, compiler, force=True)

            self.assertEqual(manifest, output / "binding.json")
            self.assertTrue(manifest.is_file())
            self.assertFalse(stale.exists())

    def test_publication_revalidates_all_probes_at_the_final_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )

            def evidence(**_: object) -> dict[str, object]:
                return {"status": "passed", "checks": []}

            with (
                patch(
                    "linux_toolchain.compiler.binding._verify_archive_tools",
                    side_effect=evidence,
                ) as archive,
                patch(
                    "linux_toolchain.compiler.binding._verify_target_tools",
                    side_effect=evidence,
                ) as target_tools,
                patch(
                    "linux_toolchain.compiler.binding._verify_binding_links",
                    side_effect=evidence,
                ) as links,
            ):
                create_binding(sdk, output, compiler)

            self.assertEqual(archive.call_args_list[-1].kwargs["output"], output)
            self.assertEqual(target_tools.call_args_list[-1].kwargs["output"], output)
            self.assertEqual(links.call_args_list[-1].kwargs["output"], output)
            self.assertEqual(
                archive.call_args_list[-1].kwargs["cc_wrapper"],
                output / "bin" / "cc",
            )
            self.assertEqual(
                links.call_args_list[-1].kwargs["cxx_wrapper"],
                output / "bin" / "c++",
            )

    def test_final_path_probe_failure_restores_the_previous_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )
            create_binding(sdk, output, compiler)
            previous_manifest = (output / "binding.json").read_bytes()
            sentinel = output / "previous-publication"
            sentinel.write_text("keep\n", encoding="utf-8")

            def reject_final_path(**arguments: object) -> dict[str, object]:
                if arguments["output"] == output:
                    raise ExternalToolError("synthetic final-path failure")
                return {"status": "passed", "checks": []}

            with (
                patch(
                    "linux_toolchain.compiler.binding._verify_binding_links",
                    side_effect=reject_final_path,
                ),
                self.assertRaisesRegex(ExternalToolError, "final-path failure"),
            ):
                create_binding(sdk, output, compiler, force=True)

            self.assertEqual((output / "binding.json").read_bytes(), previous_manifest)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(list(root.glob(".binding.staging-*")), [])

    def test_force_refuses_to_delete_unowned_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "not-a-binding"
            output.mkdir()
            sentinel = output / "user-data.txt"
            sentinel.write_text("preserve me\n", encoding="utf-8")
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )

            with self.assertRaisesRegex(
                ConfigurationError, "not.*binding|marker|refus"
            ):
                create_binding(sdk, output, compiler, force=True)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")

    def test_force_refuses_symlinked_binding_owner_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            output = root / "binding"
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )
            create_binding(sdk, output, compiler)
            marker = output / ".linux-toolchain-binding"
            marker.unlink()
            marker.symlink_to(output / "binding.json")
            sentinel = output / "user-data.txt"
            sentinel.write_text("preserve me\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigurationError, "unowned"):
                create_binding(sdk, output, compiler, force=True)

            self.assertTrue(sentinel.is_file())

    def test_force_refuses_dangerous_filesystem_root_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = self.create_sdk(Path(directory))
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=Path("/opt/compiler/bin/gcc"),
                cxx=Path("/opt/compiler/bin/g++"),
                version_text="g++ (GCC) 13.2.1",
            )

            with patch(
                "linux_toolchain.compiler.binding.shutil.rmtree",
                side_effect=AssertionError(
                    "must never attempt to delete filesystem root"
                ),
            ) as remove:
                with self.assertRaisesRegex(
                    ConfigurationError, "root|dangerous|invalid|refus"
                ):
                    create_binding(sdk, Path("/"), compiler, force=True)
                remove.assert_not_called()

    def test_wrapper_preserves_user_arguments_and_appends_sdk_sysroot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            compiler_dir = root / "compiler with space"
            compiler_dir.mkdir()
            fake = compiler_dir / "fake compiler"
            fake.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8"
            )
            fake.chmod(0o755)
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=fake,
                cxx=fake,
                version_text="g++ (GCC) 13.2.1",
            )
            output = root / "binding"
            create_binding(sdk, output, compiler)
            wrapper = output / "bin" / "c++"

            result = subprocess.run(
                [
                    wrapper,
                    "argument with space",
                    "-march=skylake-avx512",
                    "--sysroot=/foreign",
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = result.stdout.splitlines()
            self.assertIn("argument with space", arguments)
            self.assertLess(
                next(
                    index
                    for index, argument in enumerate(arguments)
                    if argument.startswith("-L")
                ),
                arguments.index("argument with space"),
            )
            self.assertIn("-march=skylake-avx512", arguments)
            self.assertIn("--sysroot=/foreign", arguments)
            self.assertEqual(arguments[-1], f"--sysroot={sdk.resolve() / 'sysroot'}")

    def test_wrapper_clears_host_search_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.create_sdk(root)
            fake = root / "fake-compiler"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'compiler was invoked\\n'\n"
                "printf 'LD_LIBRARY_PATH=%s\\n' \"${LD_LIBRARY_PATH-unset}\"\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=fake,
                cxx=fake,
                version_text="g++ (GCC) 13.2.1",
            )
            output = root / "binding"
            create_binding(sdk, output, compiler)
            wrapper = output / "bin" / "c++"
            search_path_variables = (
                "CPATH",
                "C_INCLUDE_PATH",
                "CPLUS_INCLUDE_PATH",
                "LIBRARY_PATH",
                "LD_RUN_PATH",
                "COMPILER_PATH",
                "GCC_EXEC_PREFIX",
                "CCC_OVERRIDE_OPTIONS",
            )

            environment = dict(os.environ)
            for candidate in (*search_path_variables, "LD_LIBRARY_PATH"):
                environment[candidate] = "/host/path-that-must-not-leak"
            sanitized = subprocess.run(
                [wrapper, "-c", "probe.cc"],
                check=False,
                text=True,
                capture_output=True,
                env=environment,
            )
            self.assertEqual(sanitized.returncode, 0, sanitized.stderr)
            self.assertIn("compiler was invoked", sanitized.stdout)
            self.assertIn("LD_LIBRARY_PATH=unset", sanitized.stdout)


if __name__ == "__main__":
    unittest.main()
