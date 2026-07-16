from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from linux_toolchain.container import (
    BuilderHost,
    BuilderImage,
    ContainerIdentityFiles,
    linux_platform_for_architecture,
    ubuntu_builder_snapshot,
)
from linux_toolchain.elf.models import ElfMetadata, VersionNeed
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.licenses import license_evidence
from linux_toolchain.managed.artifacts import _host_elf_audit, finalize_artifact
from linux_toolchain.managed.builder import (
    _COMPILER_BACKEND_SOURCES,
    _write_container_identity,
    build_with_docker,
    render_workspace,
    select_artifact,
)
from linux_toolchain.managed.contracts import (
    MANAGED_BUILDER_BASE_IMAGE,
    MANAGED_TARGET_TOOL_NAMES,
)
from linux_toolchain.managed.identity import (
    managed_artifact_action_for_specs,
    render_action_script,
    script_identity,
)
from linux_toolchain.managed.lockfile import SourceLock, resolve_lock
from linux_toolchain.managed.models import ManagedSpec
from linux_toolchain.managed.scripts import render_build_script
from linux_toolchain.managed.selection import ManagedBuildSelection
from linux_toolchain.managed.sources import (
    download_source_archive,
    validate_source_archive,
)
from linux_toolchain.process import run
from linux_toolchain.recipes import get_recipe
from linux_toolchain.sdk.crosstool_ng import (
    COMPONENT_SHA256,
    CROSSTOOL_NG_RELEASES,
    _packaged_builder_dockerfile_sha256,
    sdk_producer_identity,
)


def managed_lock(
    *,
    arch: str = "x86_64",
    glibc: str = "2.19",
    gcc: str = "13",
):
    build_platform = linux_platform_for_architecture(arch)
    return resolve_lock(
        ManagedSpec.from_dict(
            {
                "schema": "linux-toolchain-managed-spec",
                "format": 1,
                "name": "builder-test",
                "build_platform": build_platform,
                "host": {
                    "os": "linux",
                    "arch": arch,
                    "glibc_floor": glibc,
                },
                "targets": [{"arch": arch, "glibc_floor": glibc}],
                "compilers": [
                    {
                        "family": "gcc",
                        "versions": [gcc],
                        "runtimes": [{"kind": "libstdc++"}],
                    },
                    {
                        "family": "clang",
                        "versions": ["16"],
                        "runtimes": [
                            {"kind": "libstdc++", "gcc_version": "13"},
                            {"kind": "libc++"},
                        ],
                    },
                ],
            }
        )
    )


def artifact_id(lock, *, collection: str, family: str) -> str:
    entries = getattr(lock, collection)
    if collection == "compiler_kits":
        return next(entry.id for entry in entries if entry.family == family)
    provider = "gcc" if family == "gcc" else "llvm"
    return next(entry.id for entry in entries if entry.provider_family == provider)


def write_executable(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body.rstrip()}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


LLVM_LICENSES = (
    "llvm/LICENSE.TXT",
    "clang/LICENSE.TXT",
    "compiler-rt/LICENSE.TXT",
    "libcxx/LICENSE.TXT",
    "libcxxabi/LICENSE.TXT",
    "libunwind/LICENSE.TXT",
)


class ManagedBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = managed_lock()
        hashes = {
            filename: hashlib.sha256(filename.encode()).hexdigest()
            for filename in _COMPILER_BACKEND_SOURCES
        }
        backend_sources = patch.dict(_COMPILER_BACKEND_SOURCES, hashes, clear=True)
        backend_sources.start()
        self.addCleanup(backend_sources.stop)
        portable_tools = patch(
            "linux_toolchain.managed.builder.validate_portable_target_tools"
        )
        portable_tools.start()
        self.addCleanup(portable_tools.stop)

    def test_build_preflights_before_source_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "build/build.sh"
            script.parent.mkdir(parents=True)
            script.touch()
            compiler_id = artifact_id(
                self.lock,
                collection="compiler_kits",
                family="clang",
            )
            with (
                patch(
                    "linux_toolchain.managed.builder._load_workspace",
                    return_value=(root, {"build_script": {"paired_runtime": False}}),
                ),
                patch(
                    "linux_toolchain.managed.builder._validated_workspace_inputs",
                    return_value=tuple(
                        root / name for name in ("source", "sdk", "tools", "backend")
                    ),
                ),
                patch(
                    "linux_toolchain.managed.builder.require_non_root_builder",
                    return_value=BuilderHost(uid=1000, gid=1000),
                ),
                patch(
                    "linux_toolchain.managed.builder.shutil.which",
                    side_effect=lambda name: {
                        "docker": "/tools/docker",
                        "readelf": "/tools/readelf",
                    }.get(name),
                ),
                patch(
                    "linux_toolchain.managed.builder.validate_native_docker_daemon",
                    side_effect=ConfigurationError("preflight failed"),
                ),
                patch(
                    "linux_toolchain.managed.builder._download_source_archive"
                ) as download,
                self.assertRaisesRegex(ConfigurationError, "preflight failed"),
            ):
                build_with_docker(self.lock, compiler_id, root)

            download.assert_not_called()

    def test_container_identity_rejects_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            identity = workspace / "build/container-identity"
            identity.mkdir(parents=True)
            sentinel = root / "sentinel"
            sentinel.write_text("preserve\n", encoding="utf-8")
            (identity / "passwd").symlink_to(sentinel)

            with self.assertRaisesRegex(ConfigurationError, "regular file"):
                _write_container_identity(
                    workspace,
                    BuilderHost(uid=1000, gid=1000),
                )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")

    @staticmethod
    def write_sdk(
        root: Path,
        *,
        arch: str = "x86_64",
        glibc: str = "2.19",
    ) -> Path:
        sdk = root / "sdk"
        include = sdk / "sysroot" / "usr" / "include"
        include.mkdir(parents=True)
        (include / "stdint.h").write_text("/* managed SDK */\n", encoding="utf-8")
        for component, names in {
            "glibc": ("COPYING", "COPYING.LIB"),
            "linux": ("COPYING",),
            "gcc": ("COPYING", "COPYING.RUNTIME"),
            "binutils": ("COPYING",),
        }.items():
            component_root = sdk / "licenses" / component
            component_root.mkdir(parents=True)
            for name in names:
                (component_root / name).write_text(
                    f"{component} {name}\n", encoding="utf-8"
                )
        spec = get_recipe(arch, glibc).to_spec(name="test-sdk")
        serialized = spec.to_manifest_dict()
        identity = sdk_producer_identity(spec)
        release = CROSSTOOL_NG_RELEASES[spec.builder.version]
        manifest = {
            "schema": "linux-toolchain-sdk",
            "format": 1,
            "compatibility_scope": "glibc-floor",
            "target": serialized["target"],
            "builder": serialized["builder"],
            "defconfig_sha256": identity["config_sha256"],
            "build_environment": {
                "dockerfile_sha256": _packaged_builder_dockerfile_sha256(),
                "base_image": release.builder_base_image,
                "platform": identity["builder_contract"]["platform"],
                "apt_snapshot": identity["builder_contract"]["apt_snapshot"],
                "image": {},
            },
            "sources": {
                "crosstool-ng": {
                    "version": release.version,
                    "url": release.source_url,
                    "sha256": release.sha256,
                },
                **{
                    component: {
                        "version": version,
                        "sha256": COMPONENT_SHA256[(component, version)],
                    }
                    for component, version in {
                        "glibc": spec.target.libc_version,
                        "linux": spec.target.linux_headers,
                        "gcc": spec.builder.gcc,
                        "binutils": spec.builder.binutils,
                    }.items()
                },
                "download_archives": {},
            },
            "licenses": license_evidence(sdk, context="test SDK"),
        }
        (sdk / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (root / "workspace.json").write_text(
            json.dumps(
                {
                    "schema": "linux-toolchain-sdk-workspace",
                    "format": 1,
                    "state": "built",
                    "spec": spec.to_manifest_dict(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return sdk

    @staticmethod
    def write_target_tools(root: Path, triplet: str) -> Path:
        tools = root / "toolchain/bin"
        tools.mkdir(parents=True)
        for name in MANAGED_TARGET_TOOL_NAMES:
            write_executable(tools / f"{triplet}-{name}", f"# {name}\nexit 0")
        return tools

    @staticmethod
    def run_paired_script_harness(
        root: Path,
        selection: ManagedBuildSelection,
        *,
        preserve_primary: bool,
        preserve_runtime: bool,
    ) -> tuple[list[str], Path, Path]:
        target = "x86_64-portable-linux-gnu"
        backend_version = "9.5.0"
        roots = {
            "/compiler-backend-sources": root / "compiler-backend-sources",
            "/compiler-backend": root / "compiler-backend",
            "/target-tools": root / "target-tools",
            "/runtime-output": root / "runtime-output",
            "/sources": root / "sources",
            "/output": root / "output",
            "/sdk": root / "sdk",
        }
        for path in roots.values():
            path.mkdir(parents=True)
        output = roots["/output"]
        runtime_output = roots["/runtime-output"]
        for path in (output, runtime_output):
            (path / ".linux-toolchain-managed-output").write_text(
                "format=1\n", encoding="utf-8"
            )
        (roots["/sdk"] / "sysroot").mkdir()
        binutils_license = roots["/sdk"] / "licenses/binutils/COPYING"
        binutils_license.parent.mkdir(parents=True)
        binutils_license.write_text("binutils\n", encoding="utf-8")

        tools = roots["/target-tools"]
        for name in MANAGED_TARGET_TOOL_NAMES:
            body = (
                'printf \'target-tool objdump %s\\n\' "$*" >>"$HARNESS_LOG"'
                if name == "objdump"
                else "exit 0"
            )
            write_executable(tools / f"{target}-{name}", body)

        backend_bin = roots["/compiler-backend"] / "bin"
        backend_bin.mkdir()
        backend_driver = f"""
case "$*" in
  *-dumpmachine*) printf '%s\\n' '{target}' ;;
  *-dumpfullversion*) printf '%s\\n' '{backend_version}' ;;
esac
"""
        for name in ("gcc", "g++"):
            write_executable(backend_bin / f"{target}-{name}", backend_driver)
        for name in ("ar", "as", "ld", "nm", "ranlib", "strip"):
            write_executable(backend_bin / f"{target}-{name}")

        log = root / "stages.log"
        installed_driver = root / "installed-compiler-driver"
        write_executable(
            installed_driver,
            f"""
case "$*" in
  *-dumpmachine*) printf '%s\\n' '{target}' ;;
  *-dumpfullversion*) printf '%s\\n' '{selection.version}' ;;
esac
""",
        )
        fake_make = backend_bin / "make"
        write_executable(
            fake_make,
            f"""
printf 'make %s\\n' "$*" >>"$HARNESS_LOG"
destination=
for argument in "$@"; do
  case "$argument" in DESTDIR=*) destination=${{argument#DESTDIR=}} ;; esac
done
case "$*" in
  *install-gcc*)
    root="$destination/opt/linux-toolchain/managed"
    mkdir -p "$root/bin" \
      "$root/lib/gcc/{target}/{selection.version}/include" \
      "$root/lib/gcc/{target}/{selection.version}/include-fixed"
    install -m 0755 -T {installed_driver} "$root/bin/{target}-gcc"
    install -m 0755 -T {installed_driver} "$root/bin/{target}-g++"
    printf '%s\\n' staging \
      >"$root/lib/gcc/{target}/{selection.version}/include/origin"
    ;;
  *install-target-libquadmath*)
    root="$destination/opt/linux-toolchain/managed"
    mkdir -p "$root/lib/gcc/{target}/{selection.version}/include"
    printf '%s\\n' quadmath \
      >"$root/lib/gcc/{target}/{selection.version}/include/quadmath.h"
    ;;
  *install-target-libatomic*)
    root="$destination/opt/linux-toolchain/managed"
    mkdir -p "$root/lib"
    : >"$root/lib/libatomic.a"
    : >"$root/lib/libatomic.so"
    ;;
  *install-target-libgcc*|*install-target-libstdc++-v3*)
    mkdir -p "$destination/opt/linux-toolchain/managed/lib/gcc/{target}/{selection.version}"
    ;;
esac
""",
        )

        build_root = output / "build"
        source_root = output / "src"
        build_root.mkdir()
        source_root.mkdir()
        (roots["/sources"] / "source.tar.xz").touch()
        if selection.family == "gcc":
            source = source_root / "gcc"
            for path in (source / "gmp", source / "mpfr", source / "mpc"):
                path.mkdir(parents=True)
            write_executable(
                source / "configure",
                """
printf 'configure %s\\n' "$*" >>"$HARNESS_LOG"
objdump --linux-toolchain-configure-probe
touch Makefile
""",
            )
            (source / ".linux-toolchain-source-ready").write_text(
                f"format=1 source=gcc-{selection.version}\n", encoding="utf-8"
            )
            for name in ("COPYING", "COPYING.RUNTIME"):
                (source / name).write_text("staging\n", encoding="utf-8")
            build = build_root / "gcc"
            build.mkdir()
        else:
            sha512 = selection.source.sha512
            source = source_root / "llvm-project"
            (source / "llvm").mkdir(parents=True)
            (source / "llvm/CMakeLists.txt").touch()
            (source / ".linux-toolchain-source-ready").write_text(
                f"format=1 sha512={sha512}\n", encoding="utf-8"
            )
            for relative in LLVM_LICENSES:
                license_path = source / relative
                license_path.parent.mkdir(parents=True, exist_ok=True)
                license_path.write_text("staging\n", encoding="utf-8")
            build = build_root / "llvm"
            (build / "bin").mkdir(parents=True)
            resource = build / f"lib/clang/{selection.version}"
            resource.mkdir(parents=True)
            (resource / "resource.txt").touch()
            write_executable(
                build / "bin/clang",
                f"""
case "$*" in
  *-print-resource-dir*) printf '%s\\n' '{resource}' ;;
  *--version*) printf '%s\\n' 'clang version {selection.version}' ;;
  *-dumpmachine*) printf '%s\\n' '{target}' ;;
esac
""",
            )
            (build / "CMakeCache.txt").touch()
            (build / "build.ninja").touch()
            (build / ".linux-toolchain-configured").write_text(
                "format=1 "
                f"sha512={sha512} target={target} "
                f"backend={target}-{backend_version} linkage=both\n",
                encoding="utf-8",
            )
            write_executable(
                backend_bin / "cmake",
                """
printf 'cmake %s\n' "$*" >>"$HARNESS_LOG"
if test -n "${DESTDIR:-}"; then
  mkdir -p "$DESTDIR/opt/linux-toolchain/managed/lib"
  : >"$DESTDIR/opt/linux-toolchain/managed/lib/libc++.so"
fi
""",
            )

        primary = output / "artifacts"
        primary.mkdir()
        (primary / "previous").write_text("keep\n", encoding="utf-8")
        if preserve_primary:
            (primary / "artifact.json").touch()
            component = "gcc" if selection.family == "gcc" else "llvm-project"
            licenses = primary / "licenses" / component
            if selection.family == "gcc":
                licenses.mkdir(parents=True)
                for name in ("COPYING", "COPYING.RUNTIME"):
                    (licenses / name).write_text("final\n", encoding="utf-8")
                headers = primary / f"compiler/lib/gcc/{target}/{selection.version}"
                for name in ("include", "include-fixed"):
                    (headers / name).mkdir(parents=True)
                    (headers / name / "origin").write_text("final\n", encoding="utf-8")
            else:
                for relative in LLVM_LICENSES:
                    license_path = licenses / relative
                    license_path.parent.mkdir(parents=True, exist_ok=True)
                    license_path.write_text("final\n", encoding="utf-8")
        runtime = runtime_output / "artifacts"
        runtime.mkdir()
        (runtime / "previous").write_text("keep\n", encoding="utf-8")
        if preserve_runtime:
            (runtime / "artifact.json").touch()

        script = render_build_script(
            selection,
            triplet=target,
            backend_triplet=target,
            backend_version=backend_version,
            paired_runtime=True,
        )
        placeholders = {
            container_path: f"@HARNESS_ROOT_{index}@"
            for index, container_path in enumerate(roots)
        }
        for container_path, placeholder in placeholders.items():
            script = script.replace(container_path, placeholder)
        script = script.replace(
            f"$BUILD_ROOT{placeholders['/target-tools']}",
            "$BUILD_ROOT/target-tools",
        )
        for container_path, placeholder in placeholders.items():
            script = script.replace(placeholder, str(roots[container_path]))
        script_path = root / "build.sh"
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)
        run(
            [
                "env",
                f"HARNESS_LOG={log}",
                "LINUX_TOOLCHAIN_JOBS=2",
                f"LINUX_TOOLCHAIN_PRESERVE_PRIMARY={int(preserve_primary)}",
                f"LINUX_TOOLCHAIN_PRESERVE_RUNTIME={int(preserve_runtime)}",
                "bash",
                script_path,
            ]
        )
        return log.read_text(encoding="utf-8").splitlines(), output, runtime_output

    @staticmethod
    def write_compiler_backend(
        root: Path,
        *,
        arch: str = "x86_64",
        glibc: str = "2.19",
    ) -> Path:
        workspace = root / "compiler-backend"
        spec = get_recipe(arch, glibc).to_spec(name="test-backend")
        (workspace / "toolchain/bin").mkdir(parents=True)
        for name in ("gcc", "g++", "ar", "as", "ld", "nm", "ranlib", "strip"):
            write_executable(
                workspace / "toolchain/bin" / f"{spec.target.triplet}-{name}"
            )
        downloads = workspace / "downloads"
        downloads.mkdir()
        for filename in _COMPILER_BACKEND_SOURCES:
            (downloads / filename).write_bytes(filename.encode())
        release = CROSSTOOL_NG_RELEASES[spec.builder.version]
        identity = sdk_producer_identity(spec)
        source_evidence = {
            "crosstool-ng": {
                "version": release.version,
                "sha256": release.sha256,
            },
            **{
                component: {
                    "version": version,
                    "sha256": COMPONENT_SHA256[(component, version)],
                }
                for component, version in {
                    "glibc": spec.target.libc_version,
                    "linux": spec.target.linux_headers,
                    "gcc": spec.builder.gcc,
                    "binutils": spec.builder.binutils,
                }.items()
            },
        }
        sdk = workspace / "sdk"
        sdk.mkdir()
        serialized = spec.to_manifest_dict()
        (sdk / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": "linux-toolchain-sdk",
                    "format": 1,
                    "target": serialized["target"],
                    "builder": serialized["builder"],
                    "defconfig_sha256": identity["config_sha256"],
                    "build_environment": {
                        "dockerfile_sha256": identity["builder_contract"][
                            "dockerfile_sha256"
                        ],
                        "base_image": identity["builder_contract"]["base_image"],
                        "platform": identity["builder_contract"]["platform"],
                        "apt_snapshot": identity["builder_contract"]["apt_snapshot"],
                        "image": {},
                    },
                    "sources": source_evidence,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (workspace / "workspace.json").write_text(
            json.dumps(
                {
                    "schema": "linux-toolchain-sdk-workspace",
                    "format": 1,
                    "state": "built",
                    "spec": spec.to_manifest_dict(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return workspace

    @staticmethod
    def workspace_manifest(
        lock,
        *,
        triplet: str = "x86_64-portable-linux-gnu",
    ) -> dict[str, object]:
        selection = select_artifact(
            lock,
            artifact_id(lock, collection="compiler_kits", family="gcc"),
        )
        sdk = get_recipe("x86_64", "2.19").to_spec()
        return {
            "build_input": managed_artifact_action_for_specs(selection, sdk, sdk),
            "sdk": {
                "glibc_version": "2.19",
                "triplet": triplet,
            },
            "target_tools": {},
            "compiler_backend": {
                "path": "/build/backend",
                "version": "1.28.0",
                "gcc": "9.5.0",
                "triplet": "x86_64-portable-linux-gnu",
                "glibc_version": "2.19",
                "sources": {},
            },
        }

    @staticmethod
    def executable_elf(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELFmanaged-test")
        path.chmod(0o755)
        return path

    @staticmethod
    def write_managed_licenses(
        artifacts: Path,
        *,
        family: str,
        compiler_kit: bool = False,
    ) -> None:
        if family == "gcc":
            required = ("gcc/COPYING", "gcc/COPYING.RUNTIME")
        else:
            required = tuple(f"llvm-project/{path}" for path in LLVM_LICENSES)
            required = (*required, "llvm-project/lld/LICENSE.TXT")
        if compiler_kit:
            required = (*required, "binutils/COPYING")
        for relative in required:
            path = artifacts / "licenses" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{relative}\n", encoding="utf-8")

    def test_workspace_records_selected_sdk_and_target_tools(self) -> None:
        runtime_id = artifact_id(self.lock, collection="runtimes", family="gcc")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.write_sdk(root)
            tools = self.write_target_tools(root, "x86_64-portable-linux-gnu")
            backend = self.write_compiler_backend(root)
            workspace = root / "workspace"

            manifest_path = render_workspace(
                self.lock,
                runtime_id,
                workspace,
                sdk=sdk,
                target_tools=tools,
                compiler_backend=backend,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            build_input = manifest["build_input"]
            self.assertNotIn("lock_sha256", manifest)
            self.assertNotIn("lock_sha256", build_input)
            self.assertNotIn("catalog_sha256", build_input)
            self.assertEqual(build_input["sdk"], manifest["sdk"]["identity"])
            self.assertEqual(
                build_input["target_tools"], manifest["target_tools"]["identity"]
            )
            self.assertEqual(
                build_input["compiler_backend"],
                manifest["compiler_backend"]["identity"],
            )
            self.assertEqual(
                Path(manifest["source_cache"]).name,
                f"archive-{build_input['source']['sha512']}.tar.xz",
            )
            self.assertEqual(build_input["builder"]["platform"], "linux/amd64")
            self.assertEqual(
                build_input["builder"]["base_image"], MANAGED_BUILDER_BASE_IMAGE
            )
            self.assertEqual(
                build_input["builder"]["apt_snapshot"],
                ubuntu_builder_snapshot(),
            )

            # Re-rendering the same input is explicit and deterministic.
            second_spec = self.lock.spec.to_dict()
            second_spec["name"] = "same-artifact-different-lock"
            second_lock = resolve_lock(ManagedSpec.from_dict(second_spec))
            self.assertNotEqual(second_lock.sha256, self.lock.sha256)
            render_workspace(
                second_lock,
                runtime_id,
                workspace,
                sdk=sdk,
                target_tools=tools,
                compiler_backend=backend,
                force=True,
            )

            header = sdk / "sysroot/usr/include/stdint.h"
            header.write_text("/* changed SDK */\n", encoding="utf-8")
            render_workspace(
                self.lock,
                runtime_id,
                workspace,
                sdk=sdk,
                target_tools=tools,
                compiler_backend=backend,
                force=True,
            )

    def test_workspace_rejects_sdk_with_changed_build_configuration(self) -> None:
        runtime_id = artifact_id(self.lock, collection="runtimes", family="gcc")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.write_sdk(root)
            manifest_path = sdk / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["defconfig_sha256"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError, "build configuration evidence changed"
            ):
                render_workspace(
                    self.lock,
                    runtime_id,
                    root / "workspace",
                    sdk=sdk,
                    target_tools=self.write_target_tools(
                        root, "x86_64-portable-linux-gnu"
                    ),
                    compiler_backend=self.write_compiler_backend(root),
                )

    def test_host_elf_audit_rejects_the_wrong_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "compiler"
            executable.write_bytes(b"\x7fELF")
            metadata = ElfMetadata(
                path=executable,
                elf_class="ELF64",
                endianness="little",
                elf_type="EXEC",
                machine="aarch64",
                interpreter="/lib/ld-linux-aarch64.so.1",
                needed=("libc.so.6",),
                rpath=(),
                runpath=(),
                has_dt_relr=False,
                version_needs=(),
            )
            with (
                patch("linux_toolchain.managed.artifacts.is_elf", return_value=True),
                patch(
                    "linux_toolchain.managed.artifacts.ReadElfInspector"
                ) as inspector,
                self.assertRaisesRegex(ExternalToolError, "aarch64.*x86_64"),
            ):
                inspector.return_value.inspect.return_value = metadata
                _host_elf_audit(root, arch="x86_64", glibc_floor="2.19")

    def test_host_elf_audit_applies_the_dt_relr_loader_floor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "compiler"
            executable.write_bytes(b"\x7fELF")
            metadata = ElfMetadata(
                path=executable,
                elf_class="ELF64",
                endianness="little",
                elf_type="EXEC",
                machine="x86_64",
                interpreter="/lib64/ld-linux-x86-64.so.2",
                needed=("libc.so.6",),
                rpath=(),
                runpath=(),
                has_dt_relr=True,
                version_needs=(
                    VersionNeed(
                        library="libc.so.6",
                        name="GLIBC_ABI_DT_RELR",
                    ),
                ),
            )
            with (
                patch("linux_toolchain.managed.artifacts.is_elf", return_value=True),
                patch(
                    "linux_toolchain.managed.artifacts.ReadElfInspector"
                ) as inspector,
            ):
                inspector.return_value.inspect.return_value = metadata
                with self.assertRaisesRegex(ExternalToolError, "DT_RELR"):
                    _host_elf_audit(root, arch="x86_64", glibc_floor="2.35")
                report = _host_elf_audit(
                    root,
                    arch="x86_64",
                    glibc_floor="2.36",
                )

            self.assertEqual(report["audited_elf_files"], 1)

    def test_build_scripts_take_parallelism_from_the_runtime_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for family in ("gcc", "clang"):
                with self.subTest(family=family):
                    selection = select_artifact(
                        self.lock,
                        artifact_id(
                            self.lock,
                            collection="compiler_kits",
                            family=family,
                        ),
                    )
                    build_script = render_build_script(
                        selection,
                        triplet="x86_64-portable-linux-gnu",
                        backend_triplet="x86_64-portable-linux-gnu",
                        backend_version="9.5.0",
                        paired_runtime=True,
                    )
                    self.assertIn("LINUX_TOOLCHAIN_JOBS is required", build_script)
                    script = root / f"{family}-build.sh"
                    script.write_text(build_script, encoding="utf-8")
                    run(["bash", "-n", script])
                    with self.assertRaisesRegex(
                        ExternalToolError, "LINUX_TOOLCHAIN_JOBS"
                    ):
                        run(["env", "-u", "LINUX_TOOLCHAIN_JOBS", "bash", script])

    def test_llvm_build_script_enables_only_the_selected_target_backend(self) -> None:
        scripts: dict[str, str] = {}
        identities: dict[str, dict[str, str]] = {}
        for arch, glibc, backend in (
            ("x86_64", "2.19", "X86"),
            ("aarch64", "2.17", "AArch64"),
        ):
            lock = managed_lock(arch=arch, glibc=glibc)
            selection = select_artifact(
                lock,
                artifact_id(lock, collection="compiler_kits", family="clang"),
            )
            triplet = f"{arch}-portable-linux-gnu"
            script = render_action_script(
                selection,
                triplet=triplet,
                backend_triplet="x86_64-portable-linux-gnu",
                backend_version="9.5.0",
            )
            scripts[arch] = script
            identities[arch] = script_identity(script)
            self.assertIn("-DCMAKE_BUILD_TYPE=Release", script)
            self.assertIn(f"-DLLVM_TARGETS_TO_BUILD={backend}", script)
            for option in (
                "LIBUNWIND_ENABLE_SHARED",
                "LIBUNWIND_ENABLE_STATIC",
                "LIBCXXABI_ENABLE_SHARED",
                "LIBCXXABI_ENABLE_STATIC",
                "LIBCXX_ENABLE_SHARED",
                "LIBCXX_ENABLE_STATIC",
                "LIBCXX_ENABLE_STATIC_ABI_LIBRARY",
                "LIBCXX_STATICALLY_LINK_ABI_IN_STATIC_LIBRARY",
                "LIBCXX_ENABLE_ABI_LINKER_SCRIPT",
            ):
                self.assertIn(f"-D{option}=ON", script)
            self.assertIn(
                "-DLIBCXX_STATICALLY_LINK_ABI_IN_SHARED_LIBRARY=OFF",
                script,
            )
            other = "AArch64" if backend == "X86" else "X86"
            self.assertNotIn(f"-DLLVM_TARGETS_TO_BUILD={other}", script)

        self.assertNotEqual(identities["x86_64"], identities["aarch64"])

    def test_gcc_build_script_enables_quadmath_only_for_x86_64(self) -> None:
        for arch, glibc, enabled in (
            ("x86_64", "2.19", True),
            ("aarch64", "2.17", False),
        ):
            with self.subTest(arch=arch):
                lock = managed_lock(arch=arch, glibc=glibc)
                selection = select_artifact(
                    lock,
                    artifact_id(lock, collection="runtimes", family="gcc"),
                )
                triplet = f"{arch}-portable-linux-gnu"
                script = render_action_script(
                    selection,
                    triplet=triplet,
                    backend_triplet=triplet,
                    backend_version="9.5.0",
                )

                self.assertIn('GCC_RELEASE_FLAGS="-O2 -g0"', script)
                for variable in (
                    "CFLAGS",
                    "CXXFLAGS",
                    "CFLAGS_FOR_TARGET",
                    "CXXFLAGS_FOR_TARGET",
                ):
                    self.assertIn(f'{variable}="$GCC_RELEASE_FLAGS"', script)
                option = "--enable-libquadmath" if enabled else "--disable-libquadmath"
                other_option = (
                    "--disable-libquadmath" if enabled else "--enable-libquadmath"
                )
                self.assertIn(option, script)
                self.assertNotIn(other_option, script)
                self.assertEqual(
                    "all-target-libquadmath" in script,
                    enabled,
                )
                self.assertEqual(
                    "install-target-libquadmath" in script,
                    enabled,
                )

    def test_paired_gcc16_build_publishes_libatomic(self) -> None:
        lock = managed_lock(gcc="16")
        compiler = select_artifact(
            lock,
            artifact_id(lock, collection="compiler_kits", family="gcc"),
        )
        with tempfile.TemporaryDirectory() as directory:
            _, _, runtime_output = self.run_paired_script_harness(
                Path(directory),
                compiler,
                preserve_primary=False,
                preserve_runtime=False,
            )

            runtime = runtime_output / ".artifacts.staging/runtime/lib"
            self.assertTrue((runtime / "libatomic.a").is_file())
            self.assertTrue((runtime / "libatomic.so").is_file())

    def test_paired_build_scripts_execute_only_missing_stages(self) -> None:
        states = (
            (True, False),
            (False, True),
            (False, False),
        )
        for family in ("gcc", "clang"):
            compiler_id = artifact_id(
                self.lock,
                collection="compiler_kits",
                family=family,
            )
            selection = select_artifact(self.lock, compiler_id)
            for preserve_primary, preserve_runtime in states:
                with (
                    self.subTest(
                        family=family,
                        preserve_primary=preserve_primary,
                        preserve_runtime=preserve_runtime,
                    ),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    log, output, runtime_output = self.run_paired_script_harness(
                        Path(directory),
                        selection,
                        preserve_primary=preserve_primary,
                        preserve_runtime=preserve_runtime,
                    )
                    primary_artifacts = output / (
                        "artifacts" if preserve_primary else ".artifacts.staging"
                    )
                    runtime_artifacts = runtime_output / (
                        "artifacts" if preserve_runtime else ".artifacts.staging"
                    )
                    if family == "gcc":
                        self.assertIn(
                            "target-tool objdump --linux-toolchain-configure-probe",
                            log,
                        )
                        self.assertTrue(
                            any(
                                line.startswith("configure ")
                                and "--enable-libquadmath" in line
                                for line in log
                            )
                        )
                        self.assertFalse(
                            any("--disable-libquadmath" in line for line in log)
                        )
                        compiler_ran = any("all-gcc" in line for line in log)
                        runtime_ran = any("all-target-libgcc" in line for line in log)
                        quadmath_ran = any(
                            "all-target-libquadmath" in line for line in log
                        )
                        runtime_license = runtime_artifacts / "licenses/gcc/COPYING"
                        runtime_header = (
                            runtime_artifacts
                            / "runtime/lib/gcc"
                            / "x86_64-portable-linux-gnu"
                            / selection.version
                            / "include/origin"
                        )
                        runtime_quadmath = runtime_header.with_name("quadmath.h")
                    else:
                        compiler_ran = any(
                            "--target clang clang-resource-headers" in line
                            for line in log
                        )
                        runtime_ran = any("--target runtimes" in line for line in log)
                        runtime_license = (
                            runtime_artifacts / "licenses/llvm-project/llvm/LICENSE.TXT"
                        )
                        runtime_header = None
                        runtime_quadmath = None

                    self.assertEqual(compiler_ran, not preserve_primary)
                    self.assertEqual(runtime_ran, not preserve_runtime)
                    if family == "gcc":
                        self.assertEqual(quadmath_ran, not preserve_runtime)
                    self.assertTrue(primary_artifacts.is_dir())
                    self.assertTrue(runtime_artifacts.is_dir())
                    self.assertEqual(
                        (output / "artifacts/previous").read_text(encoding="utf-8"),
                        "keep\n",
                    )
                    self.assertEqual(
                        (runtime_output / "artifacts/previous").read_text(
                            encoding="utf-8"
                        ),
                        "keep\n",
                    )
                    if not preserve_runtime:
                        expected_origin = "final\n" if preserve_primary else "staging\n"
                        self.assertEqual(
                            runtime_license.read_text(encoding="utf-8"),
                            expected_origin,
                        )
                        if runtime_header is not None:
                            self.assertEqual(
                                runtime_header.read_text(encoding="utf-8"),
                                expected_origin,
                            )
                        if runtime_quadmath is not None:
                            self.assertEqual(
                                runtime_quadmath.read_text(encoding="utf-8"),
                                "quadmath\n",
                            )

    def test_aarch64_uses_a_native_arm_compiler_backend(
        self,
    ) -> None:
        lock = managed_lock(arch="aarch64", glibc="2.17")
        compiler_id = artifact_id(lock, collection="compiler_kits", family="clang")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.write_sdk(root, arch="aarch64", glibc="2.17")
            tools = self.write_target_tools(root, "aarch64-portable-linux-gnu")
            backend = self.write_compiler_backend(
                root,
                arch="aarch64",
                glibc="2.17",
            )
            manifest_path = render_workspace(
                lock,
                compiler_id,
                root / "workspace",
                sdk=sdk,
                target_tools=tools,
                compiler_backend=backend,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["sdk"]["triplet"], "aarch64-portable-linux-gnu")
            self.assertEqual(
                manifest["compiler_backend"]["triplet"],
                "aarch64-portable-linux-gnu",
            )
            self.assertEqual(manifest["compiler_backend"]["gcc"], "9.5.0")
            self.assertEqual(
                Path(manifest["source_cache"]).name,
                f"archive-{manifest['build_input']['source']['sha512']}.tar.xz",
            )

    def test_rejects_unowned_workspace_and_escaping_target_tool(self) -> None:
        runtime_id = artifact_id(self.lock, collection="runtimes", family="gcc")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.write_sdk(root)
            tools = self.write_target_tools(root, "x86_64-portable-linux-gnu")
            backend = self.write_compiler_backend(root)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "user-data").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unowned"):
                render_workspace(
                    self.lock,
                    runtime_id,
                    workspace,
                    sdk=sdk,
                    target_tools=tools,
                    compiler_backend=backend,
                    force=True,
                )
            self.assertEqual(
                (workspace / "user-data").read_text(encoding="utf-8"), "keep"
            )

            outside = root / "outside-ar"
            outside.write_text("#!/bin/sh\n", encoding="utf-8")
            outside.chmod(0o755)
            ar = tools / "x86_64-portable-linux-gnu-ar"
            ar.unlink()
            ar.symlink_to(outside)
            with self.assertRaisesRegex(ConfigurationError, "symlink escapes"):
                render_workspace(
                    self.lock,
                    runtime_id,
                    root / "other-workspace",
                    sdk=sdk,
                    target_tools=tools,
                    compiler_backend=backend,
                )

    def test_source_archive_hash_mismatch_removes_partial_file(self) -> None:
        source = SourceLock(
            id="gcc-13.4.0",
            family="gcc",
            version="13.4.0",
            kind="archive",
            url=("https://gcc.gnu.org/pub/gcc/releases/gcc-13.4.0/gcc-13.4.0.tar.xz"),
            sha512="0" * 128,
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source.tar.xz"
            response = io.BytesIO(b"wrong")
            response.headers = {"Content-Length": "5"}  # type: ignore[attr-defined]
            progress: list[tuple[int, int]] = []

            def record_progress(completed: int, total: int) -> None:
                progress.append((completed, total))

            with (
                patch(
                    "linux_toolchain.managed.sources.urllib.request.urlopen",
                    return_value=response,
                ),
                self.assertRaisesRegex(ExternalToolError, "SHA-512 mismatch"),
            ):
                download_source_archive(source, destination, record_progress)
            self.assertEqual(progress, [(0, 5), (5, 5)])
            self.assertFalse(destination.exists())
            self.assertFalse(
                tuple(destination.parent.glob(f".{destination.name}.part-*"))
            )

            invalid_url = SourceLock(
                **{
                    **source.__dict__,
                    "url": "https://example.com/gcc-13.4.0.tar.xz",
                }
            )
            with self.assertRaisesRegex(ConfigurationError, "official release"):
                download_source_archive(invalid_url, destination)

            llvm = next(item for item in self.lock.sources if item.family == "clang")
            self.assertEqual(validate_source_archive(llvm), llvm.sha512)
            invalid_llvm_url = SourceLock(
                **{
                    **llvm.__dict__,
                    "url": (
                        "https://github.com/llvm/llvm-project/archive/refs/tags/"
                        f"llvmorg-{llvm.version}.tar.gz"
                    ),
                }
            )
            with self.assertRaisesRegex(ConfigurationError, "official release"):
                validate_source_archive(invalid_llvm_url)

    def test_concurrent_source_downloads_publish_once(self) -> None:
        payload = b"managed GCC source\n"
        sha512 = hashlib.sha512(payload).hexdigest()
        source = SourceLock(
            id="gcc-13.4.0",
            family="gcc",
            version="13.4.0",
            kind="archive",
            url=("https://gcc.gnu.org/pub/gcc/releases/gcc-13.4.0/gcc-13.4.0.tar.xz"),
            sha512=sha512,
        )
        started = threading.Event()
        release = threading.Event()
        duplicate_open = threading.Event()
        call_lock = threading.Lock()
        call_count = 0

        class BlockingResponse(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def __init__(self) -> None:
                super().__init__(payload)
                self.blocked = False

            def read(self, size: int = -1) -> bytes:
                if not self.blocked:
                    self.blocked = True
                    started.set()
                    release.wait(timeout=2)
                return super().read(size)

        def open_source(*_args: object, **_kwargs: object) -> io.BytesIO:
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count
                if call_count > 1:
                    duplicate_open.set()
            response = BlockingResponse() if current_call == 1 else io.BytesIO(payload)
            response.headers = {  # type: ignore[attr-defined]
                "Content-Length": str(len(payload))
            }
            return response

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "gcc-source.tar.xz"
            barrier = threading.Barrier(3)

            def download() -> Path:
                barrier.wait()
                return download_source_archive(source, destination)

            with (
                patch(
                    "linux_toolchain.managed.sources.urllib.request.urlopen",
                    side_effect=open_source,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                first = executor.submit(download)
                second = executor.submit(download)
                barrier.wait()
                try:
                    self.assertTrue(started.wait(timeout=1))
                    self.assertFalse(duplicate_open.wait(timeout=0.1))
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=2), destination)
                self.assertEqual(second.result(timeout=2), destination)

            self.assertEqual(call_count, 1)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(
                tuple(destination.parent.glob(f".{destination.name}.part-*"))
            )
            self.assertTrue((root / ".locks" / f"sha512-{sha512}.lock").is_file())

    def test_paired_gcc_build_runs_once_with_the_immutable_image_id(self) -> None:
        compiler_id = artifact_id(self.lock, collection="compiler_kits", family="gcc")
        runtime_id = artifact_id(self.lock, collection="runtimes", family="gcc")
        image_id = "sha256:" + "2" * 64
        builder_image = BuilderImage(
            image_id=image_id,
            repo_digests=(),
            os="linux",
            architecture="amd64",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = self.write_sdk(root)
            tools = self.write_target_tools(root, "x86_64-portable-linux-gnu")
            backend = self.write_compiler_backend(root)
            source_cache = root / "sources"
            workspace = root / "compiler-workspace"
            runtime_workspace = root / "runtime-workspace"
            manifest_path = render_workspace(
                self.lock,
                compiler_id,
                workspace,
                sdk=sdk,
                target_tools=tools,
                compiler_backend=backend,
                source_cache=source_cache,
                paired_runtime=True,
            )
            render_workspace(
                self.lock,
                runtime_id,
                runtime_workspace,
                sdk=sdk,
                target_tools=tools,
                compiler_backend=backend,
                source_cache=source_cache,
            )
            workspace_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source = Path(workspace_manifest["source_cache"])
            source.write_bytes(b"verified by the mocked source validator")
            published_manifest = workspace / "published-artifact.json"
            published_runtime_manifest = runtime_workspace / "published-artifact.json"

            with (
                patch(
                    "linux_toolchain.managed.builder._download_source_archive",
                    return_value=source,
                ),
                patch("linux_toolchain.managed.builder._preflight"),
                patch(
                    "linux_toolchain.managed.builder._write_container_identity",
                    return_value=ContainerIdentityFiles(
                        uid=1000,
                        gid=1000,
                        passwd=Path("/tmp/passwd"),
                        group=Path("/tmp/group"),
                    ),
                ),
                patch(
                    "linux_toolchain.container.inspect_builder_image",
                    side_effect=(None, builder_image),
                ),
                patch(
                    "linux_toolchain.managed.builder._finalize_and_publish_artifact",
                    side_effect=(published_manifest, published_runtime_manifest),
                ) as finalizer,
                patch("linux_toolchain.managed.builder.run_streaming") as run_streaming,
                patch("linux_toolchain.managed.builder.run_logged") as run_logged,
            ):
                result = build_with_docker(
                    self.lock,
                    compiler_id,
                    workspace,
                    image="linux-toolchain-managed:mutable",
                    progress=lambda _: None,
                    paired_runtime_id=runtime_id,
                    paired_runtime_workspace=runtime_workspace,
                )

            self.assertEqual(result, published_manifest)
            run_streaming.assert_called_once()
            run_logged.assert_called_once()
            docker_run = run_logged.call_args.args[0]
            self.assertIn(image_id, docker_run)
            self.assertNotIn("linux-toolchain-managed:mutable", docker_run)
            self.assertIn("LINUX_TOOLCHAIN_PRESERVE_PRIMARY=0", docker_run)
            self.assertIn("LINUX_TOOLCHAIN_PRESERVE_RUNTIME=0", docker_run)
            self.assertIn(
                f"type=bind,src={runtime_workspace / 'output'},dst=/runtime-output",
                docker_run,
            )
            self.assertIn(
                f"type=bind,src={backend / 'toolchain'},dst=/compiler-backend,readonly",
                docker_run,
            )
            self.assertEqual(finalizer.call_count, 2)
            self.assertEqual(
                run_logged.call_args.args[1],
                workspace / "build/managed-build.log",
            )

    def test_compiler_finalize_publishes_loader_compatible_manifest(self) -> None:
        selection = select_artifact(
            self.lock,
            artifact_id(self.lock, collection="compiler_kits", family="gcc"),
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            payload = workspace / "output/artifacts/compiler"
            triplet = "x86_64-portable-linux-gnu"
            self.executable_elf(payload / "bin" / f"{triplet}-gcc")
            self.executable_elf(payload / "bin" / f"{triplet}-g++")
            for name in (
                "ar",
                "as",
                "ld",
                "nm",
                "objcopy",
                "objdump",
                "ranlib",
                "strip",
            ):
                self.executable_elf(payload / "bin" / f"{triplet}-{name}")
            self.write_managed_licenses(
                workspace / "output/artifacts",
                family="gcc",
                compiler_kit=True,
            )

            with patch(
                "linux_toolchain.managed.artifacts._host_elf_audit",
                return_value={
                    "audited_elf_files": 10,
                    "max_required_glibc": "2.35",
                },
            ):
                generic_path = finalize_artifact(
                    workspace / "output/artifacts",
                    selection,
                    manifest=self.workspace_manifest(self.lock),
                    image_provenance={
                        "id": "sha256:image",
                        "os": "linux",
                        "architecture": "amd64",
                        "repo_digests": [],
                    },
                )

            standard = json.loads(
                (workspace / "output/artifacts/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            generic = json.loads(generic_path.read_text(encoding="utf-8"))
            self.assertEqual(standard["schema"], "linux-toolchain-compiler-kit")
            self.assertEqual(standard["provider"]["version"], "13.4.0")
            self.assertEqual(standard["target"]["triplet"], triplet)
            self.assertEqual(
                set(standard["locations"]["target_tools"]),
                {"ar", "as", "ld", "nm", "objcopy", "objdump", "ranlib", "strip"},
            )
            self.assertNotIn("lock_sha256", generic)
            self.assertEqual(
                set(generic),
                {
                    "schema",
                    "format",
                    "action",
                    "action_sha256",
                    "provenance",
                    "licenses",
                    "elf_audit",
                },
            )
            self.assertEqual(generic["elf_audit"]["max_required_glibc"], "2.35")


if __name__ == "__main__":
    unittest.main()
