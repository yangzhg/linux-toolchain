import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linux_toolchain.bundle import (
    create_bundle,
    create_setup_bundle,
    publish_installation,
)
from linux_toolchain.bundle_installer import (
    PREFIX_TOKEN,
    default_conan_home_name,
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


def _managed_lock() -> object:
    config = SetupConfig.from_dict(
        {
            "schema": "linux-toolchain-setup",
            "format": 1,
            "compiler": "gcc@12",
            "target": {"arch": "x86_64", "glibc_floor": "2.19"},
            "integration": "shell",
            "host_glibc_floor": "2.19",
        }
    )
    from linux_toolchain.managed import resolve_lock

    return resolve_lock(config.managed_spec())


def _payload(root: Path) -> Path:
    payload = root / "payload"
    environment = payload / "binding/env/toolchain.env"
    environment.parent.mkdir(parents=True)
    environment.write_text(
        "export TEST_PREFIX=@LINUX_TOOLCHAIN_PREFIX@\n",
        encoding="utf-8",
    )
    (payload / "binding/env/toolchain.info").write_text(
        "compiler.family=gcc\nlibc.family=glibc\n",
        encoding="utf-8",
    )
    launcher = payload / "bin/lxtc"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(render_launcher(conan=False), encoding="utf-8")
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
    target_arch: str | None = None,
    target_floor: str = "2.17",
) -> Path:
    archive = output.with_suffix(".tar.gz")
    payload_entries = write_payload_archive(payload, archive)
    output.write_bytes(
        render_installer_header(
            host_arch=_host_arch(),
            host_floor="2.17",
            target_arch=target_arch or _host_arch(),
            target_floor=target_floor,
            bundle_id="test-toolchain",
            conan=conan,
            payload_entries=payload_entries,
        )
        + archive.read_bytes()
    )
    output.chmod(0o755)
    return output


class BundleCreationTest(unittest.TestCase):
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

    def test_publication_rejects_conan_path_overlap_and_selector_recursion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = SimpleNamespace(conan=ConanSettings(), bundle_id="test")
            arguments = {
                "sdk": root / "sdk",
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
                "linux_toolchain.bundle._resolve_payload_inputs",
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
            lock = _managed_lock()
            variant = lock.variants[0]
            compiler = SimpleNamespace(
                root=kit,
                selection=SimpleNamespace(
                    artifact_id=variant.compiler_kit_id,
                    host=ManagedHostSpec(
                        os="linux", arch=_host_arch(), glibc_floor="2.17"
                    ),
                ),
            )
            publication = SimpleNamespace(
                root=runtime,
                selection=SimpleNamespace(
                    artifact_id=variant.runtime_id,
                    runtime_kind="gcc-runtime",
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
                for relative in ("binding.json", "audit-policy.json"):
                    (output / relative).touch()
                return output / "binding.json"

            with (
                patch(
                    "linux_toolchain.bundle.load_managed_compiler_artifact",
                    side_effect=lambda _lock, artifact_id, path: SimpleNamespace(
                        root=Path(path),
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            host=compiler.selection.host,
                        ),
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.load_managed_runtime_publication",
                    side_effect=lambda _lock, artifact_id, path: SimpleNamespace(
                        root=Path(path),
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            runtime_kind=publication.selection.runtime_kind,
                        ),
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.create_managed_binding",
                    side_effect=create_binding,
                ),
            ):
                first = create_bundle(
                    sdk=sdk,
                    compiler_kit=kit,
                    runtime=runtime,
                    lock=lock,
                    variant=variant.id,
                    output=root / "first.run",
                    bundle_id="deterministic",
                )
                second = create_bundle(
                    sdk=sdk,
                    compiler_kit=kit,
                    runtime=runtime,
                    lock=lock,
                    variant=variant.id,
                    output=root / "second.run",
                    bundle_id="deterministic",
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
                        compiler_kit=kit,
                        runtime=runtime,
                        lock=lock,
                        variant=variant.id,
                        output=occupied,
                        bundle_id="deterministic",
                    )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(occupied.read_text(encoding="utf-8"), "other producer\n")
            archive = first.read_bytes().split(
                b"__LINUX_TOOLCHAIN_PAYLOAD_BELOW__\n", 1
            )[1]
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as payload:
                names = payload.getnames()
                self.assertIn("payload/bin/lxtc", names)
                self.assertIn("payload/artifacts/sdk/content", names)
                self.assertIn("payload/artifacts/compiler-kit/content", names)
                self.assertIn("payload/artifacts/runtime/content", names)
                environment = payload.extractfile("payload/binding/env/toolchain.env")
                manifest_file = payload.extractfile("payload/manifest.json")
                assert environment is not None
                assert manifest_file is not None
                content = environment.read()
                manifest = json.load(manifest_file)

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
                    "runtime_kind",
                    "binding",
                },
            )
            self.assertEqual(manifest["schema"], "linux-toolchain-bundle")

    def test_archive_install_relocates_generated_and_reused_binding_links(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "producer/sdk"
            kit = root / "producer/compiler-kit"
            runtime = root / "producer/runtime"
            sdk_startfile = sdk / "sysroot/usr/lib64/crti.o"
            ar = kit / "compiler/bin/x86_64-portable-linux-gnu-ar"
            ld = kit / "compiler/bin/x86_64-portable-linux-gnu-ld"
            runtime_library = runtime / "runtime/lib64/libstdc++.so.6"
            for path in (sdk_startfile, ar, ld, runtime_library):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{path.name}\n", encoding="utf-8")
            ar.chmod(0o755)
            ld.chmod(0o755)

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
                        / f"x86_64-portable-linux-gnu-{name}"
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
                                                / (f"x86_64-portable-linux-gnu-{name}")
                                            ),
                                            "wrapper": str(binding / "bin" / name),
                                        }
                                        for name in ("ar", "ld")
                                    },
                                },
                            },
                            "cxx_runtime": {"path": str(binding_runtime)},
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
                selection=SimpleNamespace(
                    artifact_id=variant.compiler_kit_id,
                    host=ManagedHostSpec(
                        os="linux", arch=_host_arch(), glibc_floor="2.17"
                    ),
                ),
            )
            publication = SimpleNamespace(
                root=runtime,
                selection=SimpleNamespace(
                    artifact_id=variant.runtime_id,
                    runtime_kind="gcc-runtime",
                ),
            )

            def create_binding(*args: object, **kwargs: object) -> Path:
                return write_binding(
                    Path(args[1]),
                    Path(args[0]),
                    Path(args[2]),
                    Path(kwargs["runtime"]),
                )

            def assert_installed_links(prefix: Path) -> None:
                links = {
                    "binding/bin/ar": (
                        "artifacts/compiler-kit/compiler/bin/"
                        "x86_64-portable-linux-gnu-ar"
                    ),
                    "binding/bin/ld": (
                        "artifacts/compiler-kit/compiler/bin/"
                        "x86_64-portable-linux-gnu-ld"
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
                            "linux_toolchain.bundle.load_managed_runtime_publication",
                            return_value=publication,
                        ),
                        patch(
                            "linux_toolchain.bundle.create_managed_binding",
                            side_effect=create_binding,
                        ),
                    ):
                        create_bundle(
                            sdk=sdk,
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
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            host=compiler.selection.host,
                        ),
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.load_managed_runtime_publication",
                    side_effect=lambda _lock, artifact_id, path: SimpleNamespace(
                        root=Path(path),
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            runtime_kind=publication.selection.runtime_kind,
                        ),
                    ),
                ),
                patch(
                    "linux_toolchain.bundle.create_managed_binding",
                    side_effect=create_binding,
                ),
            ):
                publish_installation(
                    sdk=sdk,
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
                "cxx_runtime": {"path": str(runtime)},
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
                    "linux_toolchain.bundle.load_managed_runtime_publication",
                    side_effect=lambda _lock, artifact_id, path: SimpleNamespace(
                        root=Path(path),
                        selection=SimpleNamespace(
                            artifact_id=artifact_id,
                            runtime_kind="gcc-runtime",
                        ),
                        manifest=SimpleNamespace(
                            locations={"library_dirs": ("runtime",)}
                        ),
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
                manifest["runtime_kind"] = "llvm-runtime"
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
                        ) -> tuple[str, ...]:
                            templates = _instantiate_payload(
                                payload,
                                destination,
                                conan_home=conan_home,
                                conan_build_profile=conan_build_profile,
                            )
                            if injected is not None:
                                environment_path = payload / "binding/env/toolchain.env"
                                environment_path.write_text(
                                    environment_path.read_text(encoding="utf-8")
                                    + f"# {injected}\n",
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
                {"artifacts", "binding", "bin", "manifest.json"},
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
                compiler_kit=root / "store/compiler-kit",
                runtime=root / "store/runtime",
                binding=state / "binding",
                validated_artifacts=lambda: (object(), object()),
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
                    "linux_toolchain.setup.create_bundle", side_effect=create
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
            prefix = root / "installed"

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
            environment = (prefix / "binding/env/toolchain.env").read_text(
                encoding="utf-8"
            )
            self.assertIn(f"TEST_PREFIX={prefix}", environment)
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
                "compiler.family=gcc\nlibc.family=glibc\n",
            )

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
            launcher = payload / "bin/lxtc"
            launcher.write_text(render_launcher(conan=True), encoding="utf-8")
            launcher.chmod(0o755)
            installer = _installer(payload, root / "toolchain.run", conan=True)

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            home = root / "home"
            home.mkdir()
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

            invoked = subprocess.run(
                [
                    prefix / "bin/lxtc",
                    "/bin/sh",
                    "-c",
                    'printf "%s\\n%s\\n%s" "$CONAN_HOME" '
                    '"$CONAN_DEFAULT_PROFILE" "$CONAN_DEFAULT_BUILD_PROFILE"',
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
                [str(conan_home), "default", "lxtc-build"],
            )
            info = subprocess.run(
                [prefix / "bin/lxtc", "info"],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(info.returncode, 0, info.stderr)
            self.assertIn(f"conan.home={conan_home}\n", info.stdout)
            self.assertIn("conan.host_profile=default\n", info.stdout)
            self.assertIn("conan.build_profile=lxtc-build\n", info.stdout)

    def test_installer_accepts_explicit_conan_paths_without_conan(self) -> None:
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
            installer = _installer(
                payload,
                root / "toolchain.run",
                conan=True,
                target_arch=("aarch64" if _host_arch() == "x86_64" else "x86_64"),
                target_floor="999.0",
            )
            conan_home = root / "custom-conan-home"
            build_profile = root / "profiles/native"
            prefix = root / "installed"

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
            launcher.write_text(render_launcher(conan=True), encoding="utf-8")
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
