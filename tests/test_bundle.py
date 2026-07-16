import errno
import io
import json
import os
import platform
import pty
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linux_toolchain.build_tools import (
    DEFAULT_CMAKE_VERSION,
    BuildToolsArtifact,
    BuildToolsSpec,
    expected_build_tool_records,
)
from linux_toolchain.bundle import (
    create_bundle,
    create_setup_bundle,
    publish_installation,
)
from linux_toolchain.bundle_installer import (
    PREFIX_TOKEN,
    SHELL_INIT,
    SHELL_INIT_RELATIVE_PATH,
    LauncherExecutionLayout,
    default_conan_home_name,
    default_runtime_state_file,
    relocate_binding_links,
    render_installer_header,
    render_launcher,
    write_payload_archive,
)
from linux_toolchain.elf import AuditPolicy
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.integrations import ConanSettings
from linux_toolchain.managed import ManagedHostSpec
from linux_toolchain.setup import SetupConfig, create_prepared_bundle


def _managed_lock(compiler: str = "gcc@12") -> object:
    config = SetupConfig.from_dict(
        {
            "schema": "linux-toolchain-setup",
            "format": 1,
            "compiler": compiler,
            "target": {"arch": "x86_64", "glibc_floor": "2.19"},
            "integration": "shell",
            "host_glibc_floor": "2.19",
            "cmake_version": DEFAULT_CMAKE_VERSION,
        }
    )
    from linux_toolchain.managed import resolve_lock

    return resolve_lock(config.managed_spec())


def _runtime_set(root: Path, variant: object) -> SimpleNamespace:
    publication = SimpleNamespace(
        manifest=SimpleNamespace(locations={"library_dirs": ("runtime",)})
    )
    return SimpleNamespace(
        root=root,
        variant=variant,
        publication=lambda _kind: publication,
    )


def _launcher_execution(
    runtimes: tuple[str, ...] = ("libstdc++",),
    *,
    arch: str = "x86_64",
    glibc: str = "2.19",
) -> LauncherExecutionLayout:
    interpreter = {
        "x86_64": "/lib64/ld-linux-x86-64.so.2",
        "aarch64": "/lib/ld-linux-aarch64.so.1",
    }[arch]
    runtime_names = {"libstdc++": "libstdcxx", "libc++": "libcxx"}
    return LauncherExecutionLayout(
        target_arch=arch,
        glibc_version=glibc,
        sdk_root="artifacts/sdk/sysroot",
        runtime_root="artifacts/runtime",
        loader=f"artifacts/sdk/sysroot{interpreter}",
        interpreter=interpreter,
        sdk_library_dirs=("artifacts/sdk/sysroot/lib64",),
        runtime_library_dirs={
            kind: (f"artifacts/runtime/runtimes/{runtime_names[kind]}/runtime/lib64",)
            for kind in runtimes
        },
    )


def _write_sdk_execution_files(sdk: Path) -> None:
    sysroot = sdk / "sysroot"
    loader = sysroot / "lib64/ld-linux-x86-64.so.2"
    libc = sysroot / "usr/lib64/libc.so"
    for path in (loader, libc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    loader.chmod(0o755)


def _build_tools_artifact(root: Path) -> BuildToolsArtifact:
    spec = BuildToolsSpec(
        arch=_host_arch(),
        glibc_floor="2.17",
        cmake_version=DEFAULT_CMAKE_VERSION,
    )
    identity: dict[str, object] = {
        "kind": "test-build-tools",
        "selection": spec.to_dict(),
    }
    return BuildToolsArtifact(
        root=root.resolve(),
        spec=spec,
        identity=identity,
        tools=expected_build_tool_records(spec),
    )


def _payload(root: Path) -> Path:
    payload = root / "payload"
    environment = payload / "binding/env/toolchain.env"
    environment.parent.mkdir(parents=True)
    environment.write_text(
        "export TEST_PREFIX='@LINUX_TOOLCHAIN_PREFIX@'\n",
        encoding="utf-8",
    )
    (payload / "binding/env/toolchain.info").write_text(
        "compiler.family=gcc\nlibc.family=glibc\n",
        encoding="utf-8",
    )
    shell_init = payload / "binding" / SHELL_INIT_RELATIVE_PATH
    shell_init.parent.mkdir(parents=True)
    shell_init.write_text(SHELL_INIT, encoding="utf-8")
    launcher = payload / "bin/lxtc"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        render_launcher(
            bundle_id="test-toolchain",
            conan=False,
            execution=_launcher_execution(),
            cxx_runtimes=("libstdc++",),
            default_cxx_runtime="libstdc++",
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    (payload / "template-files").write_text(
        "bin/lxtc\nbinding/env/toolchain.env\nbinding/env/toolchain.info\n",
        encoding="utf-8",
    )
    return payload


def _host_arch() -> str:
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine(), platform.machine()
    )


def _installer(
    payload: Path,
    output: Path,
    *,
    conan: bool = False,
    cxx_runtimes: tuple[str, ...] = ("libstdc++",),
    default_installation_name: str = "test-toolchain",
    target_arch: str | None = None,
    target_floor: str = "2.17",
) -> Path:
    archive = output.with_suffix(".tar.gz")
    write_payload_archive(payload, archive)
    output.write_bytes(
        render_installer_header(
            host_arch=_host_arch(),
            host_floor="2.17",
            target_arch=target_arch or _host_arch(),
            target_floor=target_floor,
            bundle_id="test-toolchain",
            default_installation_name=default_installation_name,
            conan=conan,
            cxx_runtimes=cxx_runtimes,
            default_cxx_runtime="libstdc++",
            payload_bytes=archive.stat().st_size,
        )
        + archive.read_bytes()
    )
    output.chmod(0o755)
    return output


class BundleCreationTest(unittest.TestCase):
    def setUp(self) -> None:
        loader = patch(
            "linux_toolchain.bundle.load_build_tools",
            side_effect=lambda path: _build_tools_artifact(Path(path)),
        )
        loader.start()
        self.addCleanup(loader.stop)

    def test_default_conan_home_name_is_short_and_bundle_specific(self) -> None:
        first = default_conan_home_name(
            "setup-toolchain-gcc-12.5.0-x86_64-glibc-2.19-libstdcxx"
        )
        second = default_conan_home_name(
            "setup-toolchain-gcc-13.4.0-x86_64-glibc-2.19-libstdcxx"
        )

        self.assertEqual(first, ".conan2_lxtc_3a3ae0861c0dfc07")
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), len(".conan2_lxtc_") + 16)

    def test_payload_archive_reports_processed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.mkdir()
            content = b"x" * (128 * 1024)
            (payload / "large-file").write_bytes(content)
            (payload / "large-file-link").symlink_to("large-file")
            archive = root / "payload.tar.gz"
            updates: list[tuple[int, int]] = []

            write_payload_archive(
                payload,
                archive,
                progress=lambda completed, total: updates.append((completed, total)),
            )

            self.assertEqual(updates[0][0], 0)
            self.assertEqual(updates[0][1], len(content))
            self.assertTrue(any(0 < completed < total for completed, total in updates))
            self.assertEqual(updates[-1][0], updates[-1][1])
            self.assertEqual(updates[-1][1], updates[0][1])
            self.assertEqual(updates, sorted(updates))
            with tarfile.open(archive, mode="r:gz") as output:
                link = output.getmember("payload/large-file-link")
            self.assertTrue(link.issym())
            self.assertEqual(link.linkname, "large-file")

    def test_payload_archive_header_receives_compressed_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.mkdir()
            (payload / "content").write_bytes(os.urandom(128 * 1024))
            archive = root / "installer"
            header = b"test-header\n"
            observed: list[int] = []

            write_payload_archive(
                payload,
                archive,
                header=lambda payload_bytes: (observed.append(payload_bytes) or header),
            )

            self.assertEqual(len(observed), 2)
            self.assertEqual(observed[0], 0)
            self.assertEqual(observed[-1], archive.stat().st_size - len(header))
            with tarfile.open(
                fileobj=io.BytesIO(archive.read_bytes()[len(header) :]),
                mode="r:gz",
            ) as payload_archive:
                self.assertIn("payload/content", payload_archive.getnames())

    def test_publication_rejects_conan_path_overlap_and_selector_recursion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = SimpleNamespace(conan=ConanSettings(), bundle_id="test")
            arguments = {
                "sdk": root / "sdk",
                "build_tools": root / "build-tools",
                "compiler_kit": root / "compiler-kit",
                "runtime": root / "runtime",
                "lock": root / "managed.lock.json",
                "variant": "variant",
                "integrations": ("conan",),
                "conan": ConanSettings(),
            }
            overlaps = (
                (root / "installed", root / "installed/conan-home"),
                (root / "conan-home/installed", root / "conan-home"),
            )
            with patch(
                "linux_toolchain.bundle._load_payload_inputs",
                return_value=inputs,
            ):
                for prefix, conan_home in overlaps:
                    with (
                        self.subTest(prefix=prefix, conan_home=conan_home),
                        self.assertRaisesRegex(ConfigurationError, "cannot overlap"),
                    ):
                        publish_installation(
                            **arguments,
                            prefix=prefix,
                            conan_home=conan_home,
                            conan_build_profile=root / "native.profile",
                        )

                conan_home = root / "dedicated-conan-home"
                with self.assertRaisesRegex(ConfigurationError, "selector itself"):
                    publish_installation(
                        **arguments,
                        prefix=root / "separate-installation",
                        conan_home=conan_home,
                        conan_build_profile=conan_home / "profiles/lxtc-build",
                    )

    def test_binding_link_relocation_rejects_an_undeclared_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-binding"
            destination = root / "payload/binding"
            outside = root / "outside/tool"
            outside.parent.mkdir(parents=True)
            outside.touch()
            (source / "bin").mkdir(parents=True)
            (source / "bin/ar").symlink_to(outside)
            destination.parent.mkdir(parents=True)
            shutil.copytree(source, destination, symlinks=True)

            with self.assertRaisesRegex(ConfigurationError, "outside"):
                relocate_binding_links(
                    root / "payload",
                    destination,
                    source_binding=source,
                    artifact_paths={},
                )

    def test_bundle_is_deterministic_and_contains_a_relocatable_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            kit = root / "kit"
            runtime = root / "runtime"
            for path in (sdk, kit, runtime):
                path.mkdir()
                (path / "content").write_text(path.name, encoding="utf-8")
            _write_sdk_execution_files(sdk)
            lock = _managed_lock("clang@19")
            variant = lock.variants[0]
            conan = ConanSettings(libcxx="libstdc++11")
            compiler = SimpleNamespace(
                root=kit,
                target="x86_64-unknown-linux-gnu",
                selection=SimpleNamespace(
                    artifact_id=variant.compiler_kit_id,
                    host=ManagedHostSpec(
                        os="linux", arch=_host_arch(), glibc_floor="2.17"
                    ),
                ),
            )

            def create_binding(*args: object, **kwargs: object) -> Path:
                output = Path(args[1])
                environment = output / "env" / "toolchain.env"
                environment.parent.mkdir(parents=True)
                environment.write_text(
                    f"export TEST_SDK={args[0]}\n",
                    encoding="utf-8",
                )
                conan_directory = output / "conan"
                conan_directory.mkdir()
                (conan_directory / "host.profile").write_text(
                    "[settings]\ncompiler.libcxx=libstdc++11\n",
                    encoding="utf-8",
                )
                (output / "binding.json").touch()
                (output / "audit-policy.json").write_text(
                    json.dumps(AuditPolicy.for_glibc_floor("2.19").to_dict()),
                    encoding="utf-8",
                )
                return output / "binding.json"

            with (
                patch(
                    "linux_toolchain.bundle.load_managed_compiler_artifact",
                    side_effect=lambda _lock, artifact_id, path: SimpleNamespace(
                        root=Path(path),
                        target="x86_64-unknown-linux-gnu",
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            host=compiler.selection.host,
                        ),
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.load_managed_runtime_set_publication",
                    side_effect=lambda _lock, _variant_id, path: _runtime_set(
                        Path(path), variant
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.create_managed_binding",
                    side_effect=create_binding,
                ),
                patch(
                    "linux_toolchain.bundle.create_managed_binding_from_artifacts",
                    side_effect=create_binding,
                ),
            ):
                first = create_bundle(
                    sdk=sdk,
                    build_tools=sdk,
                    compiler_kit=kit,
                    runtime=runtime,
                    lock=lock,
                    variant=variant.id,
                    output=root / "first.run",
                    bundle_id="deterministic",
                    integrations=("conan",),
                    conan=conan,
                )
                second = create_bundle(
                    sdk=sdk,
                    build_tools=sdk,
                    compiler_kit=kit,
                    runtime=runtime,
                    lock=lock,
                    variant=variant.id,
                    output=root / "second.run",
                    bundle_id="deterministic",
                    integrations=("conan",),
                    conan=conan,
                )
                occupied = root / "occupied.run"

                def occupy_output(*args: object, **kwargs: object) -> int:
                    entries = write_payload_archive(*args, **kwargs)
                    occupied.write_text("other producer\n", encoding="utf-8")
                    return entries

                with (
                    patch(
                        "linux_toolchain.bundle.write_payload_archive",
                        side_effect=occupy_output,
                    ),
                    self.assertRaisesRegex(ConfigurationError, "cannot write"),
                ):
                    create_bundle(
                        sdk=sdk,
                        build_tools=sdk,
                        compiler_kit=kit,
                        runtime=runtime,
                        lock=lock,
                        variant=variant.id,
                        output=occupied,
                        bundle_id="deterministic",
                        integrations=("conan",),
                        conan=conan,
                    )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(occupied.read_text(encoding="utf-8"), "other producer\n")
            installer_header = first.read_bytes().split(
                b"__LINUX_TOOLCHAIN_PAYLOAD_BELOW__\n", 1
            )[0]
            self.assertIn(
                b"DEFAULT_INSTALL_NAME=clang19-glibc219-x86_64-gcc12\n",
                installer_header,
            )
            archive = first.read_bytes().split(
                b"__LINUX_TOOLCHAIN_PAYLOAD_BELOW__\n", 1
            )[1]
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as payload:
                names = payload.getnames()
                self.assertIn("payload/bin/lxtc", names)
                self.assertIn("payload/tools/content", names)
                self.assertIn("payload/artifacts/sdk/content", names)
                self.assertIn("payload/artifacts/compiler-kit/content", names)
                self.assertIn("payload/artifacts/runtime/content", names)
                environment = payload.extractfile("payload/binding/env/toolchain.env")
                manifest_file = payload.extractfile("payload/manifest.json")
                default_profile = payload.extractfile(
                    "payload/binding/conan/default.profile"
                )
                libcxx_profile = payload.extractfile(
                    "payload/binding/conan/lxtc-libcxx.profile"
                )
                libstdcxx_profile = payload.extractfile(
                    "payload/binding/conan/lxtc-libstdcxx.profile"
                )
                build_profile = payload.extractfile(
                    "payload/binding/conan/build.profile"
                )
                libcxx_build_profile = payload.extractfile(
                    "payload/binding/conan/build-libcxx.profile"
                )
                libstdcxx_build_profile = payload.extractfile(
                    "payload/binding/conan/build-libstdcxx.profile"
                )
                assert environment is not None
                assert manifest_file is not None
                assert default_profile is not None
                assert libcxx_profile is not None
                assert libstdcxx_profile is not None
                assert build_profile is not None
                assert libcxx_build_profile is not None
                assert libstdcxx_build_profile is not None
                content = environment.read()
                manifest = json.load(manifest_file)
                default = default_profile.read().decode()
                libcxx = libcxx_profile.read().decode()
                libstdcxx = libstdcxx_profile.read().decode()
                default_build = build_profile.read().decode()
                libcxx_build = libcxx_build_profile.read().decode()
                libstdcxx_build = libstdcxx_build_profile.read().decode()

            self.assertIn(PREFIX_TOKEN.encode(), content)
            self.assertNotIn(str(root).encode(), content)
            self.assertEqual(
                set(manifest),
                {
                    "schema",
                    "format",
                    "id",
                    "variant",
                    "compiler",
                    "target",
                    "host",
                    "build_tools",
                    "cxx_runtimes",
                    "binding",
                },
            )
            self.assertEqual(manifest["schema"], "linux-toolchain-bundle")
            self.assertEqual(manifest["build_tools"]["selection"]["arch"], _host_arch())
            self.assertEqual(
                manifest["build_tools"]["tools"]["cmake"]["version"],
                DEFAULT_CMAKE_VERSION,
            )
            self.assertIn("LINUX_TOOLCHAIN_CONAN_HOST_PROFILE", default)
            self.assertNotIn("LINUX_TOOLCHAIN_CXX_RUNTIME", default)
            self.assertIn("build-libstdcxx.profile", default_build)
            for generated, runtime_kind, libcxx_setting, runtime_directory in (
                (libcxx, "libc++", "libc++", "libcxx"),
                (
                    libstdcxx,
                    "libstdc++",
                    "libstdc++11",
                    "libstdcxx",
                ),
            ):
                with self.subTest(runtime=runtime_kind):
                    self.assertIn(
                        'os.getenv("LINUX_TOOLCHAIN_BINDING")',
                        generated,
                    )
                    self.assertIn(
                        "include({{ binding }}/conan/host.profile)",
                        generated,
                    )
                    self.assertNotIn("include(default)", generated)
                    self.assertIn(f"compiler.libcxx={libcxx_setting}", generated)
                    self.assertIn(
                        f"LINUX_TOOLCHAIN_CXX_RUNTIME={runtime_kind}", generated
                    )
                    self.assertIn(
                        f"artifacts/runtime/runtimes/{runtime_directory}/runtime",
                        generated,
                    )
            for generated, runtime_kind, libcxx_setting, runtime_directory in (
                (libcxx_build, "libc++", "libc++", "libcxx"),
                (
                    libstdcxx_build,
                    "libstdc++",
                    "libstdc++11",
                    "libstdcxx",
                ),
            ):
                with self.subTest(build_runtime=runtime_kind):
                    self.assertIn(f"compiler.libcxx={libcxx_setting}", generated)
                    self.assertIn(
                        f"LINUX_TOOLCHAIN_CXX_RUNTIME={runtime_kind}", generated
                    )
                    self.assertIn(
                        f"artifacts/runtime/runtimes/{runtime_directory}/runtime",
                        generated,
                    )

    def test_archive_install_relocates_generated_and_reused_binding_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "producer/sdk"
            kit = root / "producer/compiler-kit"
            runtime = root / "producer/runtime"
            sdk_startfile = sdk / "sysroot/usr/lib64/crti.o"
            ar = kit / "compiler/bin/x86_64-unknown-linux-gnu-ar"
            ld = kit / "compiler/bin/x86_64-unknown-linux-gnu-ld"
            runtime_library = runtime / "runtime/lib64/libstdc++.so.6"
            for path in (sdk_startfile, ar, ld, runtime_library):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{path.name}\n", encoding="utf-8")
            ar.chmod(0o755)
            ld.chmod(0o755)
            _write_sdk_execution_files(sdk)

            def write_binding(
                binding: Path,
                binding_sdk: Path,
                binding_kit: Path,
                binding_runtime: Path,
            ) -> Path:
                bin_dir = binding / "bin"
                overlay = binding / "glibc-startfiles"
                environment = binding / "env/toolchain.env"
                bin_dir.mkdir(parents=True)
                overlay.mkdir()
                environment.parent.mkdir()
                cc = bin_dir / "cc"
                cc.write_text(
                    f"#!/bin/sh\n# {binding_sdk}\nexit 0\n",
                    encoding="utf-8",
                )
                cc.chmod(0o755)
                (bin_dir / "gcc").symlink_to("cc")
                (bin_dir / "c++").symlink_to("cc")
                for name in ("ar", "ld"):
                    target = (
                        binding_kit
                        / "compiler/bin"
                        / f"x86_64-unknown-linux-gnu-{name}"
                    )
                    (bin_dir / name).symlink_to(os.path.relpath(target, start=bin_dir))
                (overlay / "crti.o").symlink_to(
                    binding_sdk / "sysroot/usr/lib64/crti.o"
                )
                environment.write_text(
                    f"export TEST_SDK={binding_sdk}\nexport TEST_BINDING={binding}\n",
                    encoding="utf-8",
                )
                cmake = binding / "cmake/toolchain.cmake"
                cmake.parent.mkdir()
                cmake.write_text(f"set(TEST_SDK {binding_sdk})\n", encoding="utf-8")
                (binding / "binding.json").write_text(
                    json.dumps(
                        {
                            "schema": "linux-toolchain-binding",
                            "format": 1,
                            "sdk": {"path": str(binding_sdk)},
                            "compiler": {
                                "toolchain": {
                                    "mode": "managed",
                                    "path": str(binding_kit),
                                    "manifest_path": str(binding_kit / "manifest.json"),
                                },
                                "drivers": {
                                    "c": {
                                        "invocation_path": str(
                                            binding_kit / "compiler/bin/cc"
                                        ),
                                        "wrapper": str(binding / "bin/cc"),
                                    },
                                    "cxx": {
                                        "invocation_path": str(
                                            binding_kit / "compiler/bin/c++"
                                        ),
                                        "wrapper": str(binding / "bin/c++"),
                                    },
                                },
                                "tools": {
                                    "selection": "compiler-kit",
                                    **{
                                        name: {
                                            "invocation_path": str(
                                                binding_kit
                                                / "compiler/bin"
                                                / (f"x86_64-unknown-linux-gnu-{name}")
                                            ),
                                            "wrapper": str(binding / "bin" / name),
                                        }
                                        for name in ("ar", "ld")
                                    },
                                },
                            },
                            "cxx_runtimes": {
                                "default": "libstdc++",
                                "available": [
                                    {
                                        "kind": "libstdc++",
                                        "path": str(
                                            binding_runtime / "runtimes/libstdcxx"
                                        ),
                                    }
                                ],
                            },
                            "integrations": {
                                "cmake": {"toolchain": str(cmake)},
                                "shell": {"environment": str(environment)},
                            },
                            "audit_policy": str(binding / "audit-policy.json"),
                            "glibc_binding": {
                                "startfile_overlay": str(binding / "glibc-startfiles"),
                                "library_dirs": [
                                    str(binding_sdk / "sysroot/usr/lib64"),
                                    str(binding_runtime / "runtime/lib64"),
                                ],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                (binding / "audit-policy.json").write_text(
                    json.dumps(AuditPolicy.for_glibc_floor("2.19").to_dict()),
                    encoding="utf-8",
                )
                (binding / ".linux-toolchain-binding").write_text(
                    "format=1\n", encoding="utf-8"
                )
                return binding / "binding.json"

            source_binding = root / "producer/binding"
            write_binding(source_binding, sdk, kit, runtime)
            lock = _managed_lock()
            variant = lock.variants[0]
            compiler = SimpleNamespace(
                root=kit,
                target="x86_64-unknown-linux-gnu",
                selection=SimpleNamespace(
                    artifact_id=variant.compiler_kit_id,
                    host=ManagedHostSpec(
                        os="linux", arch=_host_arch(), glibc_floor="2.17"
                    ),
                ),
            )
            runtime_set = _runtime_set(runtime, variant)

            def create_binding(*args: object, **kwargs: object) -> Path:
                return write_binding(
                    Path(args[1]),
                    Path(args[0]),
                    Path(args[2]),
                    Path(kwargs["runtime"]),
                )

            def create_binding_from_artifacts(
                *args: object,
                **kwargs: object,
            ) -> Path:
                return write_binding(
                    Path(args[1]),
                    Path(args[0]),
                    compiler.root,
                    runtime_set.root,
                )

            def assert_installed_links(prefix: Path) -> None:
                links = {
                    "binding/bin/ar": (
                        "artifacts/compiler-kit/compiler/bin/"
                        "x86_64-unknown-linux-gnu-ar"
                    ),
                    "binding/bin/ld": (
                        "artifacts/compiler-kit/compiler/bin/"
                        "x86_64-unknown-linux-gnu-ld"
                    ),
                    "binding/glibc-startfiles/crti.o": (
                        "artifacts/sdk/sysroot/usr/lib64/crti.o"
                    ),
                }
                for link, target in links.items():
                    self.assertEqual(
                        (prefix / link).resolve(strict=True), prefix / target
                    )
                self.assertEqual(os.readlink(prefix / "binding/bin/gcc"), "cc")
                for link in links:
                    self.assertNotIn(str(root / "producer"), os.readlink(prefix / link))

            for reuse_template in (False, True):
                with self.subTest(reuse_template=reuse_template):
                    output = root / f"toolchain-{int(reuse_template)}.run"
                    prefix = root / f"installed-{int(reuse_template)}"

                    with (
                        patch(
                            "linux_toolchain.bundle.load_managed_compiler_artifact",
                            return_value=compiler,
                        ),
                        patch(
                            "linux_toolchain.bundle.load_managed_runtime_set_publication",
                            return_value=runtime_set,
                        ),
                        patch(
                            "linux_toolchain.bundle.create_managed_binding",
                            side_effect=create_binding,
                        ),
                        patch(
                            "linux_toolchain.bundle."
                            "create_managed_binding_from_artifacts",
                            side_effect=create_binding_from_artifacts,
                        ),
                    ):
                        create_bundle(
                            sdk=sdk,
                            build_tools=sdk,
                            compiler_kit=kit,
                            runtime=runtime,
                            lock=lock,
                            variant=variant.id,
                            output=output,
                            binding_template=(
                                source_binding if reuse_template else None
                            ),
                        )

                    installed = subprocess.run(
                        [output, "--prefix", prefix],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    assert_installed_links(prefix)

            installed_prefix = root / "published-installation"
            with (
                patch(
                    "linux_toolchain.bundle.load_managed_compiler_artifact",
                    side_effect=lambda _lock, artifact_id, path: SimpleNamespace(
                        root=Path(path),
                        target="x86_64-unknown-linux-gnu",
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            host=compiler.selection.host,
                        ),
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.load_managed_runtime_set_publication",
                    side_effect=lambda _lock, _variant_id, path: _runtime_set(
                        Path(path), variant
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.create_managed_binding",
                    side_effect=create_binding,
                ),
            ):
                publish_installation(
                    sdk=sdk,
                    build_tools=sdk,
                    compiler_kit=kit,
                    runtime=runtime,
                    lock=lock,
                    variant=variant.id,
                    prefix=installed_prefix,
                )
            assert_installed_links(installed_prefix)

    def test_setup_publishes_only_the_installed_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "work" / "state"
            store = root / "store"
            sdk = store / "sdk" / "identity" / "sdk"
            kit = store / "managed" / "identity" / "compiler-kit"
            runtime = store / "managed" / "identity" / "runtime"
            for path in (sdk, kit, runtime):
                path.mkdir(parents=True)
                (path / "content").write_text(path.name, encoding="utf-8")
            _write_sdk_execution_files(sdk)
            binding = state / "binding"
            environment = binding / "env" / "toolchain.env"
            environment.parent.mkdir(parents=True)
            environment.write_text(
                f"export TEST_SDK={sdk}\nexport TEST_BINDING={binding}\n",
                encoding="utf-8",
            )
            for relative in ("binding.json", "audit-policy.json"):
                (binding / relative).touch()
            wrapper = binding / "bin/cc"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            (binding / "bin/c++").symlink_to("cc")
            (binding / "glibc-startfiles").mkdir()
            cmake = binding / "cmake/toolchain.cmake"
            cmake.parent.mkdir()
            cmake.write_text(f"set(TEST_SDK {sdk})\n", encoding="utf-8")
            conan = ConanSettings(libcxx="libstdc++11")
            source_conan_home = store / "conan/home"
            source_build_profile = store / "conan/profiles/build"
            conan_directory = binding / "conan"
            conan_directory.mkdir()
            (conan_directory / "host.profile").write_text(
                "[settings]\n", encoding="utf-8"
            )
            (conan_directory / "build.profile").write_text(
                f"include({conan_directory / 'host.profile'})\n",
                encoding="utf-8",
            )
            for filename in ("cmake-toolchain.cmake", "cmake-late.cmake"):
                (conan_directory / filename).write_text(f"# {sdk}\n", encoding="utf-8")
            (conan_directory / "conan-home").write_text(
                f"{source_conan_home}\n", encoding="utf-8"
            )
            (conan_directory / "build-profile").write_text(
                f"{source_build_profile}\n", encoding="utf-8"
            )
            write_binding_manifest = {
                "schema": "linux-toolchain-binding",
                "format": 1,
                "sdk": {"path": str(sdk)},
                "compiler": {
                    "toolchain": {
                        "mode": "managed",
                        "path": str(kit),
                        "manifest_path": str(kit / "manifest.json"),
                    },
                    "drivers": {
                        "c": {
                            "invocation_path": str(kit / "compiler/bin/cc"),
                            "wrapper": str(binding / "bin/cc"),
                        },
                        "cxx": {
                            "invocation_path": str(kit / "compiler/bin/c++"),
                            "wrapper": str(binding / "bin/c++"),
                        },
                    },
                    "tools": {"selection": "compiler-kit"},
                },
                "cxx_runtimes": {
                    "default": "libstdc++",
                    "available": [
                        {
                            "kind": "libstdc++",
                            "path": str(runtime / "runtimes/libstdcxx"),
                        }
                    ],
                },
                "integrations": {
                    "cmake": {"toolchain": str(cmake)},
                    "shell": {"environment": str(environment)},
                    "conan": {
                        "host_profile": str(conan_directory / "host.profile"),
                        "cmake_toolchain": str(
                            conan_directory / "cmake-toolchain.cmake"
                        ),
                        "cmake_late": str(conan_directory / "cmake-late.cmake"),
                    },
                },
                "audit_policy": str(binding / "audit-policy.json"),
                "glibc_binding": {
                    "startfile_overlay": str(binding / "glibc-startfiles"),
                    "library_dirs": [str(sdk / "sysroot"), str(runtime / "runtime")],
                },
            }
            (binding / "binding.json").write_text(
                json.dumps(write_binding_manifest), encoding="utf-8"
            )
            (binding / "audit-policy.json").write_text(
                json.dumps(AuditPolicy.for_glibc_floor("2.19").to_dict()),
                encoding="utf-8",
            )
            lock = _managed_lock()
            variant = lock.variants[0]
            prefix = root / "installed"
            installed_conan_home = root / "installed-conan/home"
            installed_build_profile = root / "installed-conan/profiles/build"
            integrations = ("cmake", "shell", "conan")
            publication = {
                "sdk": sdk,
                "build_tools": sdk,
                "compiler_kit": kit,
                "runtime": runtime,
                "lock": lock,
                "variant": variant.id,
                "prefix": prefix,
                "integrations": integrations,
                "conan": conan,
                "conan_home": installed_conan_home,
                "conan_build_profile": installed_build_profile,
                "binding_template": binding,
            }
            with (
                patch(
                    "linux_toolchain.bundle.load_managed_compiler_artifact",
                    side_effect=lambda _lock, artifact_id, path: SimpleNamespace(
                        root=Path(path),
                        target="x86_64-unknown-linux-gnu",
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            host=ManagedHostSpec(
                                os="linux",
                                arch=_host_arch(),
                                glibc_floor="2.17",
                            ),
                        ),
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.load_managed_runtime_set_publication",
                    side_effect=lambda _lock, _variant_id, path: _runtime_set(
                        Path(path), variant
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.create_managed_binding",
                ),
            ):
                launcher = publish_installation(**publication)
                installer = create_setup_bundle(
                    prefix=prefix,
                    output=root / "toolchain.run",
                )
                runtime_library = (
                    prefix / "artifacts/runtime/runtimes/libstdcxx/runtime"
                )
                for profile in (
                    prefix / "binding/conan/default.profile",
                    prefix / "binding/conan/build.profile",
                ):
                    with self.subTest(conan_profile=profile.name):
                        generated = profile.read_text(encoding="utf-8")
                        self.assertIn(
                            "LINUX_TOOLCHAIN_CXX_RUNTIME=libstdc++",
                            generated,
                        )
                        self.assertIn(
                            f"LD_LIBRARY_PATH=+(path){runtime_library}",
                            generated,
                        )

                binding_manifest_path = prefix / "binding/binding.json"
                original_binding_manifest = binding_manifest_path.read_bytes()
                binding_manifest = json.loads(original_binding_manifest)
                binding_manifest["compiler"]["drivers"]["c"]["invocation_path"] = str(
                    prefix / "artifacts/compiler-kit" / ".." / ".." / "outside-driver"
                )
                binding_manifest_path.write_text(
                    json.dumps(binding_manifest), encoding="utf-8"
                )
                with self.assertRaisesRegex(ConfigurationError, "canonical"):
                    publish_installation(**publication)
                binding_manifest_path.write_bytes(original_binding_manifest)

                manifest_path = prefix / "manifest.json"
                original_manifest = manifest_path.read_bytes()
                manifest = json.loads(original_manifest)
                manifest["cxx_runtimes"]["default"] = "libc++"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(
                    ConfigurationError, "manifest does not match"
                ):
                    publish_installation(**publication)
                manifest_path.write_bytes(original_manifest)

                from linux_toolchain.bundle import _instantiate_payload

                original_environment = (
                    prefix / "binding/env/toolchain.env"
                ).read_bytes()
                for name, injected in (
                    ("template token", PREFIX_TOKEN),
                    ("producer path", str(sdk)),
                    ("wrong binding root", None),
                ):
                    with self.subTest(relocation_failure=name):

                        def tamper(
                            payload: Path,
                            destination: Path,
                            *,
                            conan_home: Path | None,
                            conan_build_profile: Path | None,
                            injected_value: str | None = injected,
                        ) -> tuple[str, ...]:
                            templates = _instantiate_payload(
                                payload,
                                destination,
                                conan_home=conan_home,
                                conan_build_profile=conan_build_profile,
                            )
                            if injected_value is not None:
                                environment_path = payload / "binding/env/toolchain.env"
                                environment_path.write_text(
                                    environment_path.read_text(encoding="utf-8")
                                    + f"# {injected_value}\n",
                                    encoding="utf-8",
                                )
                            else:
                                binding_manifest_path = payload / "binding/binding.json"
                                binding_manifest = json.loads(
                                    binding_manifest_path.read_text(encoding="utf-8")
                                )
                                binding_manifest["sdk"]["path"] = "/wrong/sdk"
                                binding_manifest_path.write_text(
                                    json.dumps(binding_manifest), encoding="utf-8"
                                )
                            return templates

                        with (
                            patch(
                                "linux_toolchain.bundle._instantiate_payload",
                                side_effect=tamper,
                            ),
                            self.assertRaises(ConfigurationError),
                        ):
                            publish_installation(
                                **publication,
                                force=True,
                            )
                        self.assertEqual(manifest_path.read_bytes(), original_manifest)
                        self.assertEqual(
                            (prefix / "binding/env/toolchain.env").read_bytes(),
                            original_environment,
                        )

            self.assertEqual(launcher, prefix / "bin/lxtc")
            self.assertTrue(installer.is_file())
            self.assertEqual(
                set(path.name for path in prefix.iterdir()),
                {"artifacts", "binding", "bin", "manifest.json", "tools"},
            )
            self.assertFalse((prefix / "template-files").exists())
            environment = (prefix / "binding/env/toolchain.env").read_text(
                encoding="utf-8"
            )
            self.assertIn(str(prefix / "artifacts/sdk"), environment)
            self.assertNotIn("@LINUX_TOOLCHAIN_", environment)
            self.assertNotIn(str(state), environment)
            self.assertNotIn(str(store), environment)
            conan_build_profile = (prefix / "binding/conan/build.profile").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                f"include({prefix / 'binding/conan/host.profile'})",
                conan_build_profile,
            )
            self.assertIn(
                str(prefix / "artifacts/runtime/runtime"),
                conan_build_profile,
            )
            self.assertIn(
                "LINUX_TOOLCHAIN_CXX_RUNTIME=libstdc++",
                conan_build_profile,
            )
            self.assertIn(
                "LINUX_TOOLCHAIN_CXX_RUNTIME=libstdc++",
                (prefix / "binding/conan/default.profile").read_text(encoding="utf-8"),
            )
            installed_info = subprocess.run(
                [launcher, "info"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(installed_info.returncode, 0, installed_info.stderr)
            self.assertEqual(
                (installed_conan_home / "lxtc.info").read_text(encoding="utf-8"),
                installed_info.stdout,
            )
            forbidden = tuple(
                str(path).encode()
                for path in (
                    state,
                    store,
                    installed_conan_home,
                    installed_build_profile,
                )
            )
            archive = installer.read_bytes().split(
                b"__LINUX_TOOLCHAIN_PAYLOAD_BELOW__\n", 1
            )[1]
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as payload:
                for member in payload.getmembers():
                    if not member.isfile():
                        continue
                    generated = payload.extractfile(member)
                    assert generated is not None
                    content = generated.read()
                    if b"\0" in content:
                        continue
                    for producer_path in forbidden:
                        self.assertNotIn(producer_path, content, member.name)

    def test_prepared_bundle_reuses_the_validated_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "work" / "setup.json"
            state = root / "work" / "state"
            output = root / "toolchain.run"
            config = SetupConfig.from_dict(
                {
                    "schema": "linux-toolchain-setup",
                    "format": 1,
                    "compiler": "gcc@12",
                    "target": {"arch": "x86_64", "glibc_floor": "2.19"},
                    "integration": "shell",
                    "host_glibc_floor": "2.19",
                    "cmake_version": DEFAULT_CMAKE_VERSION,
                }
            )
            lock = _managed_lock()
            variant = lock.variants[0]
            prepared = SimpleNamespace(
                variant=variant.id,
                smoke_result=state / "smoke-shell/result.json",
            )
            prepared_inputs = SimpleNamespace(
                lock=lock,
                sdk=root / "store/sdk",
                build_tools=root / "store/build-tools",
                compiler_kit=root / "store/compiler-kit",
                runtime=root / "store/runtime",
                binding=state / "binding",
            )
            lease_active = False

            @contextmanager
            def hold_inputs(*args: object, **kwargs: object):
                nonlocal lease_active
                lease_active = True
                try:
                    yield prepared_inputs
                finally:
                    lease_active = False

            def create(*args: object, **kwargs: object) -> Path:
                self.assertTrue(lease_active)
                return output

            with (
                patch(
                    "linux_toolchain.setup.load_prepared_setup_state",
                    return_value=(config, prepared),
                ) as loader,
                patch(
                    "linux_toolchain.setup.lock_prepared_setup_inputs",
                    side_effect=hold_inputs,
                ),
                patch(
                    "linux_toolchain.setup._payload_inputs",
                    return_value=object(),
                ),
                patch(
                    "linux_toolchain.setup.create_bundle_from_validated_inputs",
                    side_effect=create,
                ) as creator,
            ):
                result = create_prepared_bundle(
                    config=config_path,
                    state_directory=state,
                    output=output,
                )

            self.assertEqual(result, output)
            loader.assert_called_once_with(
                config_path,
                state_directory=state,
            )
            self.assertEqual(
                creator.call_args.kwargs["binding_template"], prepared_inputs.binding
            )


class ShellInstallerTest(unittest.TestCase):
    def test_generated_shell_programs_have_valid_posix_syntax(self) -> None:
        programs = {
            "installer": render_installer_header(
                host_arch=_host_arch(),
                host_floor="2.17",
                target_arch=_host_arch(),
                target_floor="2.17",
                bundle_id="test-toolchain",
                default_installation_name="test-toolchain",
                conan=True,
                cxx_runtimes=("libstdc++", "libc++"),
                default_cxx_runtime="libstdc++",
                payload_bytes=1,
            ).decode("utf-8"),
            "launcher": render_launcher(
                bundle_id="test-toolchain",
                conan=True,
                execution=_launcher_execution(("libstdc++", "libc++")),
                cxx_runtimes=("libstdc++", "libc++"),
                default_cxx_runtime="libstdc++",
            ),
            "launcher-aarch64": render_launcher(
                bundle_id="test-toolchain-aarch64",
                conan=False,
                execution=_launcher_execution(
                    ("libstdc++", "libc++"),
                    arch="aarch64",
                    glibc="2.19",
                ),
                cxx_runtimes=("libstdc++", "libc++"),
                default_cxx_runtime="libstdc++",
            ),
        }
        for name, program in programs.items():
            with self.subTest(program=name):
                syntax = subprocess.run(
                    ["sh", "-n"],
                    input=program,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_installer_uses_the_embedded_default_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            installer = _installer(
                _payload(root),
                root / "renamed.run",
                default_installation_name="gcc12-glibc219-x86_64",
            )
            environment = {**os.environ, "HOME": str(home)}

            result = subprocess.run(
                [installer],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            prefix = home / ".local/lib/linux-toolchain/gcc12-glibc219-x86_64"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(prefix / "bin/lxtc"))
            self.assertIn(
                f"TEST_PREFIX='{prefix}'",
                (prefix / "binding/env/toolchain.env").read_text(encoding="utf-8"),
            )

            environment.pop("HOME")
            missing_home = subprocess.run(
                [installer],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(missing_home.returncode, 2)
            self.assertIn(
                "HOME is required when --prefix is omitted",
                missing_home.stderr,
            )

    def test_live_extraction_progress_follows_payload_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _payload(root)
            large_file = payload / "artifacts/content"
            large_file.parent.mkdir(parents=True)
            large_file.write_bytes(os.urandom(2 * 1024 * 1024))
            installer = _installer(payload, root / "toolchain.run")
            prefix = root / "installed"

            real_tar = shutil.which("tar")
            self.assertIsNotNone(real_tar)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            throttled_tar = fake_bin / "tar"
            throttled_tar.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                f"process = subprocess.Popen([{real_tar!r}, *sys.argv[1:]], "
                "stdin=subprocess.PIPE)\n"
                "assert process.stdin is not None\n"
                "while chunk := sys.stdin.buffer.read(65536):\n"
                "    process.stdin.write(chunk)\n"
                "    process.stdin.flush()\n"
                "    time.sleep(0.02)\n"
                "process.stdin.close()\n"
                "raise SystemExit(process.wait())\n",
                encoding="utf-8",
            )
            throttled_tar.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "TERM": "xterm",
            }
            master, slave = pty.openpty()
            try:
                process = subprocess.Popen(
                    [installer, "--prefix", prefix],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=slave,
                )
                os.close(slave)
                slave = -1
                stdout, _ = process.communicate(timeout=15)
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(master)
                if slave >= 0:
                    os.close(slave)

            stderr = b"".join(chunks)
            self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            self.assertEqual(stdout.decode().strip(), str(prefix / "bin/lxtc"))
            percentages = [int(value) for value in re.findall(rb"(\d+)%", stderr)]
            self.assertIn(0, percentages)
            self.assertIn(100, percentages)
            self.assertTrue(any(0 < value < 100 for value in percentages))
            self.assertNotIn(b"files", stderr)

    def test_launcher_prefers_packaged_build_tools_over_the_host_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _payload(root)
            packaged_cmake = payload / "tools/bin/cmake"
            packaged_cmake.parent.mkdir(parents=True)
            packaged_cmake.write_text(
                "#!/bin/sh\nprintf packaged\n",
                encoding="utf-8",
            )
            packaged_cmake.chmod(0o755)
            host_bin = root / "host-bin"
            host_bin.mkdir()
            host_cmake = host_bin / "cmake"
            host_cmake.write_text(
                "#!/bin/sh\nprintf host\n",
                encoding="utf-8",
            )
            host_cmake.chmod(0o755)
            installer = _installer(payload, root / "toolchain.run")
            prefix = root / "installed"
            installed = subprocess.run(
                [installer, "--prefix", prefix],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            environment = {**os.environ, "PATH": str(host_bin)}
            result = subprocess.run(
                [prefix / "bin/lxtc", "cmake"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "packaged")

    def test_shell_reports_selection_and_preserves_prompt_context(self) -> None:
        for (
            compiler,
            version,
            libc_version,
            shell_name,
            runtimes,
            arguments,
            prompt,
            runtime,
        ) in (
            (
                "gcc",
                "12.5.0",
                "2.17",
                "bash",
                ("libstdc++",),
                ("shell",),
                "(lxtc gcc-12.5.0):~/workspace/blint (binlog_format)$ ",
                "libstdc++",
            ),
            (
                "clang",
                "19.1.7",
                "2.19",
                "zsh",
                ("libstdc++", "libc++"),
                ("--runtime", "libc++", "shell"),
                "(lxtc clang-19.1.7 libc++):~/workspace/blint (binlog_format)$ ",
                "libc++",
            ),
        ):
            with self.subTest(compiler=compiler):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    payload = _payload(root)
                    (payload / "binding/env/toolchain.info").write_text(
                        f"compiler.family={compiler}\n"
                        f"compiler.version={version}\n"
                        "target.triplet=x86_64-unknown-linux-gnu\n"
                        "libc.family=glibc\n"
                        f"libc.version={libc_version}\n",
                        encoding="utf-8",
                    )
                    launcher = payload / "bin/lxtc"
                    launcher.write_text(
                        render_launcher(
                            bundle_id="test-toolchain",
                            conan=False,
                            execution=_launcher_execution(runtimes),
                            cxx_runtimes=runtimes,
                            default_cxx_runtime="libstdc++",
                        ),
                        encoding="utf-8",
                    )
                    installer = _installer(
                        payload,
                        root / "toolchain.run",
                        cxx_runtimes=runtimes,
                    )
                    prefix = root / "installed"
                    home = root / "home"
                    home.mkdir()
                    (home / f".{shell_name}rc").write_text(
                        "PS1='yangzhengguo@n37-112-200:~/workspace/blint "
                        "(binlog_format)$ '\n"
                        "export TEST_PREFIX=from-user-rc\n"
                        "export PATH=/user/rc/bin\n",
                        encoding="utf-8",
                    )
                    fake_bin = root / "fake-bin"
                    fake_bin.mkdir()
                    fake_shell = fake_bin / shell_name
                    fake_shell.write_text(
                        "#!/bin/sh\n"
                        'case "${0##*/}" in\n'
                        "  bash)\n"
                        '    [ "$#" -eq 3 ] && [ "$1" = --rcfile ] && '
                        '[ "$3" = -i ] || exit 64\n'
                        "    init=$2 ;;\n"
                        "  zsh)\n"
                        '    [ "$#" -eq 1 ] && [ "$1" = -i ] || exit 64\n'
                        "    init=$ZDOTDIR/.zshrc ;;\n"
                        "  *) exit 64 ;;\n"
                        "esac\n"
                        '. "$init"\n'
                        "printf 'prompt=%s\\nruntime=%s\\nprefix=%s\\n"
                        "launcher=%s\\npath=%s\\nld_library_path=%s\\n' \\\n"
                        '  "$PS1" "$LINUX_TOOLCHAIN_CXX_RUNTIME" '
                        '"$TEST_PREFIX" "$(command -v lxtc)" "$PATH" '
                        '"${LD_LIBRARY_PATH-}"\n',
                        encoding="utf-8",
                    )
                    fake_shell.chmod(0o755)
                    environment = {
                        **os.environ,
                        "HOME": str(home),
                        "SHELL": str(fake_shell),
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "LD_LIBRARY_PATH": "/user/lib",
                    }
                    installed = subprocess.run(
                        [installer, "--prefix", prefix],
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    self.assertEqual(installed.returncode, 0, installed.stderr)

                    result = subprocess.run(
                        [prefix / "bin/lxtc", *arguments],
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(
                        result.stdout.splitlines(),
                        [
                            f"prompt={prompt}",
                            f"runtime={runtime}",
                            f"prefix={prefix}",
                            f"launcher={prefix / 'bin/lxtc'}",
                            "path="
                            f"{prefix / 'binding/bin'}:"
                            f"{prefix / 'tools/bin'}:"
                            f"{prefix / 'bin'}:/user/rc/bin",
                            "ld_library_path="
                            f"{prefix / 'artifacts/runtime/runtimes'}"
                            f"/{'libcxx' if runtime == 'libc++' else 'libstdcxx'}"
                            "/runtime/lib64:/user/lib",
                        ],
                    )
                    self.assertIn(
                        f"    compiler: {compiler} {version}\n",
                        result.stderr,
                    )
                    self.assertIn(
                        f"    libc:     glibc {libc_version}\n",
                        result.stderr,
                    )
                    self.assertIn(
                        f"    runtime:  {runtime}\n",
                        result.stderr,
                    )

    def test_run_uses_sdk_loader_and_only_the_selected_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _payload(root)
            runtimes = ("libstdc++", "libc++")
            launcher = payload / "bin/lxtc"
            launcher.write_text(
                render_launcher(
                    bundle_id="test-toolchain",
                    conan=False,
                    execution=_launcher_execution(runtimes),
                    cxx_runtimes=runtimes,
                    default_cxx_runtime="libstdc++",
                ),
                encoding="utf-8",
            )
            loader = payload / "artifacts/sdk/sysroot/lib64/ld-linux-x86-64.so.2"
            loader.parent.mkdir(parents=True)
            loader.write_text(
                "#!/bin/sh\n"
                'if [ "$1" != --inhibit-cache ] || '
                '[ "$2" != --library-path ]; then exit 64; fi\n'
                "lxtc_test_library_path=$3\n"
                "shift 3\n"
                'if [ "${1-}" = --list ]; then\n'
                "  printf '%s\\n' \"$LXTC_TEST_CLOSURE\"\n"
                "  exit 0\n"
                "fi\n"
                "lxtc_test_program=$1\n"
                "shift\n"
                'if [ "${LD_LIBRARY_PATH+x}" = x ]; then '
                "lxtc_test_ld_library_path=$LD_LIBRARY_PATH; "
                "else lxtc_test_ld_library_path=unset; fi\n"
                'if [ "${LD_PRELOAD+x}" = x ]; then '
                "lxtc_test_ld_preload=$LD_PRELOAD; "
                "else lxtc_test_ld_preload=unset; fi\n"
                "printf 'runtime=%s\\nlibrary_path=%s\\nprogram=%s\\n"
                "args=%s,%s\\nld_library_path=%s\\nld_preload=%s\\n' \\\n"
                '  "$LINUX_TOOLCHAIN_CXX_RUNTIME" "$lxtc_test_library_path" '
                '"$lxtc_test_program" "$1" "$2" '
                '"$lxtc_test_ld_library_path" "$lxtc_test_ld_preload"\n'
                "exit 7\n",
                encoding="utf-8",
            )
            loader.chmod(0o755)
            for runtime in ("libstdcxx", "libcxx"):
                (payload / f"artifacts/runtime/runtimes/{runtime}/runtime/lib64").mkdir(
                    parents=True
                )
            installer = _installer(
                payload,
                root / "toolchain.run",
                cxx_runtimes=runtimes,
            )
            prefix = root / "installed"
            installed = subprocess.run(
                [installer, "--prefix", prefix],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            program = root / "app"
            program.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            program.chmod(0o755)
            libcxx_dir = prefix / "artifacts/runtime/runtimes/libcxx/runtime/lib64"
            sdk_dir = prefix / "artifacts/sdk/sysroot/lib64"
            library_path = f"{libcxx_dir}:{sdk_dir}:{root}"
            environment = {
                **os.environ,
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "LD_LIBRARY_PATH": "/host/runtime",
                "LD_AUDIT": "",
                "LD_PRELOAD": "",
                "LXTC_TEST_CLOSURE": (
                    f"libc++.so.1 => {libcxx_dir}/libc++.so.1 (0x1)\n"
                    f"libc.so.6 => {sdk_dir}/libc.so.6 (0x2)"
                ),
            }
            (root / "home").mkdir()

            result = subprocess.run(
                [
                    prefix / "bin/lxtc",
                    "--runtime",
                    "libc++",
                    "run",
                    "./app",
                    "one",
                    "two",
                ],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "runtime=libc++",
                    f"library_path={library_path}",
                    f"program={program}",
                    "args=one,two",
                    "ld_library_path=unset",
                    "ld_preload=unset",
                ],
            )
            self.assertNotIn("libstdcxx", result.stdout)

            escaped = subprocess.run(
                [
                    prefix / "bin/lxtc",
                    "--runtime",
                    "libc++",
                    "run",
                    program,
                ],
                env={
                    **environment,
                    "LXTC_TEST_CLOSURE": (
                        "libstdc++.so.6 => /usr/lib64/libstdc++.so.6 (0x1)"
                    ),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(escaped.returncode, 1)
            self.assertIn(
                "loader closure escaped the selected SDK/runtime",
                escaped.stderr,
            )
            self.assertIn("/usr/lib64/libstdc++.so.6", escaped.stderr)

    def test_default_conan_build_context_requires_a_native_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_arch = "aarch64" if _host_arch() == "x86_64" else "x86_64"
            installer = _installer(
                _payload(root),
                root / "toolchain.run",
                conan=True,
                target_arch=target_arch,
            )

            result = subprocess.run(
                [installer, "--prefix", root / "installed"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("requires a native target", result.stderr)

    def test_default_conan_build_context_requires_the_target_glibc_floor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = _installer(
                _payload(root),
                root / "toolchain.run",
                conan=True,
                target_floor="999.0",
            )

            result = subprocess.run(
                [installer, "--prefix", root / "installed"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("requires glibc 999.0", result.stderr)

    def test_installer_instantiates_binding_and_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = _installer(_payload(root), root / "toolchain.run")
            prefix = root / "installed toolchain"

            result = subprocess.run(
                [installer, "--prefix", prefix],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(prefix / "bin/lxtc"))
            self.assertFalse((prefix / "template-files").exists())
            stderr_lines = result.stderr.splitlines()
            current_shell = stderr_lines[stderr_lines.index("  Current shell:") + 1]
            path_probe = subprocess.run(
                ["/bin/sh", "-c", f"{current_shell.strip()}\nprintf '%s' \"$PATH\""],
                env={"PATH": "/host/bin"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(path_probe.returncode, 0, path_probe.stderr)
            self.assertEqual(path_probe.stdout, f"{prefix / 'bin'}:/host/bin")
            shell_home = root / "shell-home"
            shell_home.mkdir()
            bash_instruction = stderr_lines[
                stderr_lines.index("  Bash (~/.bashrc):") + 1
            ]
            append_result = subprocess.run(
                ["/bin/sh", "-c", bash_instruction.strip()],
                env={"HOME": str(shell_home)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(append_result.returncode, 0, append_result.stderr)
            self.assertEqual(
                (shell_home / ".bashrc").read_text(encoding="utf-8"),
                f'\nexport PATH="{prefix / "bin"}:$PATH"\n',
            )
            environment = (prefix / "binding/env/toolchain.env").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"TEST_PREFIX='{prefix}'", environment)
            self.assertNotIn("@LINUX_TOOLCHAIN_", environment)

            invoked = subprocess.run(
                [
                    prefix / "bin/lxtc",
                    "/bin/sh",
                    "-c",
                    'printf "%s" "$TEST_PREFIX"',
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertEqual(invoked.stdout, str(prefix))

            info = subprocess.run(
                [prefix / "bin/lxtc", "info"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(info.returncode, 0, info.stderr)
            self.assertEqual(
                info.stdout,
                "compiler.family=gcc\n"
                "libc.family=glibc\n"
                "cxx_runtime.selected=libstdc++\n",
            )
            shown = subprocess.run(
                [prefix / "bin/lxtc", "runtime", "show"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(shown.stdout, "libstdc++\n")
            rejected = subprocess.run(
                [prefix / "bin/lxtc", "runtime", "set", "libc++"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("switching is not available", rejected.stderr)
            conan_init = subprocess.run(
                [prefix / "bin/lxtc", "conan-init"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(conan_init.returncode, 2)
            self.assertIn("Conan integration is not installed", conan_init.stderr)

    def test_installer_supports_launcher_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = _installer(_payload(root), root / "toolchain.run")
            prefix = root / "installed"

            result = subprocess.run(
                [installer, "--prefix", prefix, "--launcher-name", "gcc12"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((prefix / "bin/gcc12").is_file())
            self.assertFalse((prefix / "bin/lxtc").exists())

    def test_runtime_selection_does_not_require_conan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _payload(root)
            launcher = payload / "bin/lxtc"
            launcher.write_text(
                render_launcher(
                    bundle_id="test-toolchain",
                    conan=False,
                    execution=_launcher_execution(("libstdc++", "libc++")),
                    cxx_runtimes=("libstdc++", "libc++"),
                    default_cxx_runtime="libstdc++",
                ),
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            installer = _installer(
                payload,
                root / "toolchain.run",
                cxx_runtimes=("libstdc++", "libc++"),
            )
            prefix = root / "installed"
            environment = {
                **os.environ,
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
            }
            (root / "home").mkdir()

            installed = subprocess.run(
                [installer, "--prefix", prefix],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            selected = subprocess.run(
                [prefix / "bin/lxtc", "runtime", "set", "libc++"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(selected.returncode, 0, selected.stderr)
            invoked = subprocess.run(
                [
                    prefix / "bin/lxtc",
                    "/bin/sh",
                    "-c",
                    'printf "%s" "$LINUX_TOOLCHAIN_CXX_RUNTIME"',
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertEqual(invoked.stdout, "libc++")

    def test_installer_prepares_conan_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _payload(root)
            conan = payload / "binding/conan"
            conan.mkdir()
            (conan / "settings_user.yml").write_text("settings\n", encoding="utf-8")
            (conan / "host.profile").write_text("[settings]\n", encoding="utf-8")
            (conan / "build.profile").write_text(
                "include(host.profile)\n", encoding="utf-8"
            )
            (conan / "default.profile").write_text(
                "include({{ os.getenv('LINUX_TOOLCHAIN_CONAN_HOST_PROFILE') }})\n",
                encoding="utf-8",
            )
            (conan / "lxtc-build.profile").write_text(
                "include({{ os.getenv('LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE') }})\n",
                encoding="utf-8",
            )
            (conan / "lxtc-libcxx.profile").write_text(
                '{% set binding = os.getenv("LINUX_TOOLCHAIN_BINDING") %}\n'
                "include({{ binding }}/conan/host.profile)\n\n"
                "[settings]\ncompiler.libcxx=libc++\n",
                encoding="utf-8",
            )
            (conan / "lxtc-libstdcxx.profile").write_text(
                '{% set binding = os.getenv("LINUX_TOOLCHAIN_BINDING") %}\n'
                "include({{ binding }}/conan/host.profile)\n\n"
                "[settings]\ncompiler.libcxx=libstdc++11\n",
                encoding="utf-8",
            )
            (conan / "build-libcxx.profile").write_text(
                "[settings]\ncompiler.libcxx=libc++\n",
                encoding="utf-8",
            )
            (conan / "build-libstdcxx.profile").write_text(
                "[settings]\ncompiler.libcxx=libstdc++11\n",
                encoding="utf-8",
            )
            launcher = payload / "bin/lxtc"
            launcher.write_text(
                render_launcher(
                    bundle_id="test-toolchain",
                    conan=True,
                    execution=_launcher_execution(("libstdc++", "libc++")),
                    cxx_runtimes=("libstdc++", "libc++"),
                    default_cxx_runtime="libstdc++",
                ),
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            installer = _installer(
                payload,
                root / "toolchain.run",
                conan=True,
                cxx_runtimes=("libstdc++", "libc++"),
            )

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            home = root / "home"
            home.mkdir()
            config_home = root / "config"
            conan_was_called = root / "conan-was-called"
            fake_conan = fake_bin / "conan"
            fake_conan.write_text(
                f'#!/bin/sh\nprintf called >"{conan_was_called}"\nexit 99\n',
                encoding="utf-8",
            )
            fake_conan.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            environment["HOME"] = str(home)
            environment["XDG_CONFIG_HOME"] = str(config_home)
            prefix = root / "installed"

            result = subprocess.run(
                [installer, "--prefix", prefix],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            conan_home = home / default_conan_home_name("test-toolchain")
            runtime_state = default_runtime_state_file(
                "test-toolchain",
                environment=environment,
            )
            assert runtime_state is not None
            build_profile = prefix / "binding/conan/build.profile"
            self.assertFalse(conan_was_called.exists())
            self.assertEqual(
                (prefix / "binding/conan/conan-home").read_text(encoding="utf-8"),
                f"{conan_home}\n",
            )
            self.assertEqual(
                (prefix / "binding/conan/build-profile").read_text(encoding="utf-8"),
                f"{build_profile}\n",
            )
            self.assertEqual(
                (conan_home / "settings_user.yml").read_text(encoding="utf-8"),
                "settings\n",
            )
            self.assertEqual(
                (conan_home / "profiles/default").read_text(encoding="utf-8"),
                (conan / "default.profile").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (conan_home / "profiles/lxtc-build").read_text(encoding="utf-8"),
                (conan / "lxtc-build.profile").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (conan_home / "profiles/lxtc-libcxx").read_text(encoding="utf-8"),
                (conan / "lxtc-libcxx.profile").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (conan_home / "profiles/lxtc-libstdcxx").read_text(encoding="utf-8"),
                (conan / "lxtc-libstdcxx.profile").read_text(encoding="utf-8"),
            )

            def invoke_launcher(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [prefix / "bin/lxtc", *arguments],
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

            invoked = invoke_launcher(
                "/bin/sh",
                "-c",
                'printf "%s\\n%s\\n%s\\n%s\\n%s\\n%s" "$CONAN_HOME" '
                '"$CONAN_DEFAULT_PROFILE" "$CONAN_DEFAULT_BUILD_PROFILE" '
                '"$LINUX_TOOLCHAIN_CONAN_HOST_PROFILE" '
                '"$LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE" '
                '"$LINUX_TOOLCHAIN_CXX_RUNTIME"',
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertEqual(
                invoked.stdout.splitlines(),
                [
                    str(conan_home),
                    "lxtc-libstdcxx",
                    "lxtc-build",
                    str(prefix / "binding/conan/lxtc-libstdcxx.profile"),
                    str(prefix / "binding/conan/build-libstdcxx.profile"),
                    "libstdc++",
                ],
            )
            info = invoke_launcher("info")
            self.assertEqual(info.returncode, 0, info.stderr)
            self.assertIn(f"conan.home={conan_home}\n", info.stdout)
            self.assertIn("conan.host_profile=lxtc-libstdcxx\n", info.stdout)
            self.assertIn("conan.build_profile=lxtc-build\n", info.stdout)
            self.assertIn("cxx_runtime.selected=libstdc++\n", info.stdout)
            self.assertEqual(
                (conan_home / "lxtc.info").read_text(encoding="utf-8"),
                info.stdout,
            )

            shown = invoke_launcher("runtime", "show")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(shown.stdout, "libstdc++\n")

            for runtime, profile, build in (
                ("libstdc++", "lxtc-libstdcxx", "build-libstdcxx.profile"),
                ("libc++", "lxtc-libcxx", "build-libcxx.profile"),
            ):
                with self.subTest(runtime=runtime):
                    selected = invoke_launcher(
                        "--runtime",
                        runtime,
                        "/bin/sh",
                        "-c",
                        'printf "%s\\n%s\\n%s\\n%s" "$LINUX_TOOLCHAIN_CXX_RUNTIME" '
                        '"$CONAN_DEFAULT_PROFILE" '
                        '"$LINUX_TOOLCHAIN_CONAN_HOST_PROFILE" '
                        '"$LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE"',
                    )
                    self.assertEqual(selected.returncode, 0, selected.stderr)
                    self.assertEqual(
                        selected.stdout.splitlines(),
                        [
                            runtime,
                            profile,
                            str(prefix / f"binding/conan/{profile}.profile"),
                            str(prefix / f"binding/conan/{build}"),
                        ],
                    )

                    selected_info = invoke_launcher(
                        "--runtime",
                        runtime,
                        "info",
                    )
                    self.assertEqual(
                        selected_info.returncode,
                        0,
                        selected_info.stderr,
                    )
                    self.assertIn(
                        f"cxx_runtime.selected={runtime}\n",
                        selected_info.stdout,
                    )
                    self.assertIn(
                        f"conan.host_profile={profile}\n",
                        selected_info.stdout,
                    )

            configured = invoke_launcher("conan-init", "libc++")
            self.assertEqual(configured.returncode, 2)
            self.assertIn("usage:", configured.stderr)
            self.assertFalse(runtime_state.exists())

            configured = invoke_launcher("runtime", "set", "libc++")
            self.assertEqual(configured.returncode, 0, configured.stderr)
            self.assertEqual(configured.stdout, "libc++\n")
            self.assertEqual(
                runtime_state.read_text(encoding="utf-8"),
                "libc++\n",
            )

            configured_info = invoke_launcher("info")
            self.assertEqual(configured_info.returncode, 0, configured_info.stderr)
            self.assertIn(
                "cxx_runtime.selected=libc++\n",
                configured_info.stdout,
            )
            self.assertIn(
                "conan.host_profile=lxtc-libcxx\n",
                configured_info.stdout,
            )
            self.assertEqual(
                (conan_home / "lxtc.info").read_text(encoding="utf-8"),
                configured_info.stdout,
            )
            configured_environment = invoke_launcher(
                "/bin/sh",
                "-c",
                'printf "%s\\n%s\\n%s" "$CONAN_DEFAULT_PROFILE" '
                '"$LINUX_TOOLCHAIN_CONAN_HOST_PROFILE" '
                '"$LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE"',
            )
            self.assertEqual(
                configured_environment.returncode,
                0,
                configured_environment.stderr,
            )
            self.assertEqual(
                configured_environment.stdout.splitlines(),
                [
                    "lxtc-libcxx",
                    str(prefix / "binding/conan/lxtc-libcxx.profile"),
                    str(prefix / "binding/conan/build-libcxx.profile"),
                ],
            )

            inherited_environment = {
                **environment,
                "LINUX_TOOLCHAIN_CXX_RUNTIME": "libstdc++",
            }
            inherited = subprocess.run(
                [
                    prefix / "bin/lxtc",
                    "/bin/sh",
                    "-c",
                    'printf "%s\\n%s\\n%s" "$LINUX_TOOLCHAIN_CXX_RUNTIME" '
                    '"$CONAN_DEFAULT_PROFILE" '
                    '"$LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE"',
                ],
                env=inherited_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(inherited.returncode, 0, inherited.stderr)
            self.assertEqual(
                inherited.stdout.splitlines(),
                [
                    "libstdc++",
                    "lxtc-libstdcxx",
                    str(prefix / "binding/conan/build-libstdcxx.profile"),
                ],
            )

            overridden_info = invoke_launcher(
                "--runtime",
                "libstdc++",
                "info",
            )
            self.assertEqual(overridden_info.returncode, 0, overridden_info.stderr)
            self.assertIn(
                "cxx_runtime.selected=libstdc++\n",
                overridden_info.stdout,
            )
            self.assertIn(
                "conan.host_profile=lxtc-libstdcxx\n",
                overridden_info.stdout,
            )
            self.assertEqual(
                (conan_home / "lxtc.info").read_text(encoding="utf-8"),
                configured_info.stdout,
            )

            reset = invoke_launcher("runtime", "reset")
            self.assertEqual(reset.returncode, 0, reset.stderr)
            self.assertEqual(reset.stdout, "libstdc++\n")
            self.assertFalse(runtime_state.exists())
            configured_info = invoke_launcher("info")
            self.assertIn(
                "cxx_runtime.selected=libstdc++\n",
                configured_info.stdout,
            )
            self.assertEqual(
                (conan_home / "lxtc.info").read_text(encoding="utf-8"),
                configured_info.stdout,
            )

            configured = invoke_launcher("runtime", "set", "libc++")
            self.assertEqual(configured.returncode, 0, configured.stderr)

            second_prefix = root / "installed-again"
            reinstalled = subprocess.run(
                [installer, "--prefix", second_prefix],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
            reinstalled_info = subprocess.run(
                [second_prefix / "bin/lxtc", "info"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                reinstalled_info.returncode,
                0,
                reinstalled_info.stderr,
            )
            self.assertIn(
                "cxx_runtime.selected=libc++\n",
                reinstalled_info.stdout,
            )
            self.assertEqual(
                (conan_home / "lxtc.info").read_text(encoding="utf-8"),
                reinstalled_info.stdout,
            )

            shutil.rmtree(conan_home)
            repaired = invoke_launcher("conan-init")
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            self.assertEqual(repaired.stdout.strip(), str(conan_home))
            for path in (
                conan_home / "settings_user.yml",
                conan_home / "profiles/default",
                conan_home / "profiles/lxtc-build",
                conan_home / "profiles/lxtc-libcxx",
                conan_home / "profiles/lxtc-libstdcxx",
            ):
                self.assertTrue(path.is_file(), path)

            repaired_info = invoke_launcher("info")
            self.assertEqual(repaired_info.returncode, 0, repaired_info.stderr)
            self.assertIn(
                "cxx_runtime.selected=libc++\n",
                repaired_info.stdout,
            )
            self.assertEqual(
                (conan_home / "lxtc.info").read_text(encoding="utf-8"),
                repaired_info.stdout,
            )

            generated_profile = conan_home / "profiles/default"
            generated_profile.write_text("user configuration\n", encoding="utf-8")
            conflict = invoke_launcher("conan-init")
            self.assertEqual(conflict.returncode, 2)
            self.assertIn("refusing to replace different", conflict.stderr)
            self.assertEqual(
                generated_profile.read_text(encoding="utf-8"),
                "user configuration\n",
            )

    def test_explicit_conan_build_profile_is_not_changed_by_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _payload(root)
            conan = payload / "binding/conan"
            conan.mkdir()
            (conan / "settings_user.yml").write_text("settings\n", encoding="utf-8")
            (conan / "host.profile").write_text("[settings]\n", encoding="utf-8")
            (conan / "build.profile").write_text(
                "include(host.profile)\n", encoding="utf-8"
            )
            (conan / "default.profile").write_text(
                "include(profile)\n", encoding="utf-8"
            )
            (conan / "lxtc-build.profile").write_text(
                "include(build-profile)\n", encoding="utf-8"
            )
            for name in (
                "lxtc-libcxx.profile",
                "lxtc-libstdcxx.profile",
                "build-libcxx.profile",
                "build-libstdcxx.profile",
            ):
                (conan / name).write_text(f"{name}\n", encoding="utf-8")
            launcher = payload / "bin/lxtc"
            launcher.write_text(
                render_launcher(
                    bundle_id="test-toolchain",
                    conan=True,
                    execution=_launcher_execution(("libstdc++", "libc++")),
                    cxx_runtimes=("libstdc++", "libc++"),
                    default_cxx_runtime="libstdc++",
                ),
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            installer = _installer(
                payload,
                root / "toolchain.run",
                conan=True,
                cxx_runtimes=("libstdc++", "libc++"),
                target_arch=("aarch64" if _host_arch() == "x86_64" else "x86_64"),
                target_floor="999.0",
            )
            conan_home = root / "custom-conan-home"
            build_profile = root / "profiles/native"
            prefix = root / "installed"
            home = root / "home"
            home.mkdir()
            environment = {
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "config"),
            }

            result = subprocess.run(
                [
                    installer,
                    "--prefix",
                    prefix,
                    "--conan-home",
                    conan_home,
                    "--conan-build-profile",
                    build_profile,
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("not present yet", result.stderr)
            self.assertEqual(
                (prefix / "binding/conan/conan-home").read_text(encoding="utf-8"),
                f"{conan_home}\n",
            )
            self.assertEqual(
                (prefix / "binding/conan/build-profile").read_text(encoding="utf-8"),
                f"{build_profile}\n",
            )
            invoked = subprocess.run(
                [
                    prefix / "bin/lxtc",
                    "--runtime",
                    "libc++",
                    "/bin/sh",
                    "-c",
                    'printf "%s\\n%s" "$LINUX_TOOLCHAIN_CONAN_HOST_PROFILE" '
                    '"$LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE"',
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stderr)
            self.assertEqual(
                invoked.stdout.splitlines(),
                [
                    str(prefix / "binding/conan/lxtc-libcxx.profile"),
                    str(build_profile),
                ],
            )

    def test_installer_rejects_conan_path_overlap_and_selector_recursion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = _installer(
                _payload(root),
                root / "toolchain.run",
                conan=True,
            )
            home = root / "home"
            home.mkdir()
            environment = {**os.environ, "HOME": str(home)}
            cases = (
                (
                    root / "inside-prefix/installed",
                    root / "inside-prefix/installed/conan-home",
                    None,
                    "cannot overlap",
                ),
                (
                    root / "inside-home/conan-home/installed",
                    root / "inside-home/conan-home",
                    None,
                    "cannot overlap",
                ),
                (
                    root / "selector/install",
                    root / "selector/conan-home",
                    root / "selector/conan-home/profiles/lxtc-build",
                    "selector itself",
                ),
            )
            for prefix, conan_home, build_profile, error in cases:
                command = [
                    installer,
                    "--prefix",
                    prefix,
                    "--conan-home",
                    conan_home,
                ]
                if build_profile is not None:
                    command.extend(("--conan-build-profile", build_profile))
                result = subprocess.run(
                    command,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                with self.subTest(prefix=prefix, conan_home=conan_home):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(error, result.stderr)

    def test_installer_can_override_only_the_conan_cppstd_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = _payload(root)
            conan = payload / "binding/conan"
            conan.mkdir()
            files = {
                "settings_user.yml": "settings\n",
                "host.profile": "[settings]\ncompiler=gcc\n",
                "build.profile": "include(host.profile)\n",
                "default.profile": "include(host-profile)\n",
                "lxtc-build.profile": "include(build-profile)\n",
            }
            for name, content in files.items():
                (conan / name).write_text(content, encoding="utf-8")
            launcher = payload / "bin/lxtc"
            launcher.write_text(
                render_launcher(
                    bundle_id="test-toolchain",
                    conan=True,
                    execution=_launcher_execution(()),
                ),
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            installer = _installer(payload, root / "toolchain.run", conan=True)
            home = root / "home"
            home.mkdir()
            prefix = root / "installed"
            environment = {**os.environ, "HOME": str(home)}

            result = subprocess.run(
                [
                    installer,
                    "--prefix",
                    prefix,
                    "--conan-cppstd",
                    "gnu20",
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            default_profile = (
                home / default_conan_home_name("test-toolchain") / "profiles/default"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                default_profile,
                "include(host-profile)\n\n[settings]\ncompiler.cppstd=gnu20\n",
            )
            self.assertEqual(
                (prefix / "binding/conan/default.profile").read_text(encoding="utf-8"),
                default_profile,
            )
            self.assertEqual(
                (prefix / "binding/conan/host.profile").read_text(encoding="utf-8"),
                "[settings]\ncompiler=gcc\n",
            )

    def test_installer_rejects_an_occupied_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = _installer(_payload(root), root / "toolchain.run")
            prefix = root / "occupied"
            prefix.mkdir()
            (prefix / "user-data").touch()
            result = subprocess.run(
                [installer, "--prefix", prefix],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("absent or empty", result.stderr)


if __name__ == "__main__":
    unittest.main()
