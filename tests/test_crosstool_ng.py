import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from linux_toolchain.compiler.binding import (
    create_binding,
)
from linux_toolchain.compiler.toolchain import ArchiveTool, CompilerInfo, TargetTools
from linux_toolchain.container import (
    BUILDER_CONTRACT_LABEL,
    BUILDER_DOCKERFILE_NAME,
    SDK_BUILDER_TARGET,
    TEMPORARY_CONTAINER_LABEL,
    UBUNTU_BUILDER_SNAPSHOT_ENV,
    BuilderImage,
    BuilderImageResolution,
    builder_image_contract_digest,
    resolve_builder_image,
    temporary_container_run,
    ubuntu_builder_snapshot,
)
from linux_toolchain.elf.models import ElfMetadata
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.models import (
    SDK_MANIFEST_FORMAT,
    SDK_MANIFEST_SCHEMA,
    SDK_WORKSPACE_FORMAT,
    SDK_WORKSPACE_SCHEMA,
    BuilderSpec,
    SdkSpec,
    TargetSpec,
)
from linux_toolchain.process import CommandResult
from linux_toolchain.publication import normalize_public_tree
from linux_toolchain.sdk.crosstool_ng import (
    COMPONENT_ARCHIVES,
    COMPONENT_SHA256,
    CROSSTOOL_NG_RELEASES,
    FULL_BUILD_GOAL,
    SDK_BUILD_GOAL,
    BuilderHost,
    BuildGoal,
    PinnedArchive,
    _component_archives,
    _download_archive,
    _packaged_builder_dockerfile_sha256,
    _record_builder_provenance,
    _source_archives,
    _toolchain_outputs_ready,
    build_with_docker,
    export_sdk,
    load_workspace,
    render_config,
    render_workspace,
    sdk_producer_identity,
    validate_portable_target_tools,
    validate_resolved_config,
    validate_sdk,
)
from linux_toolchain.versions import AbiVersion

ROOT = Path(__file__).resolve().parents[1]
TARGET_BINUTILS = (
    "ar",
    "as",
    "ld",
    "nm",
    "objcopy",
    "objdump",
    "ranlib",
    "readelf",
    "strip",
)


def glibc_spec(version: str = "2.19") -> SdkSpec:
    return SdkSpec(
        name=f"linux-toolchain-x86_64-glibc-{version}",
        target=TargetSpec(
            arch="x86_64",
            vendor="portable",
            libc="glibc",
            libc_version=version,
            linux_headers="6.12.41",
            minimum_kernel="3.2.0",
            cpu="x86-64",
        ),
        builder=BuilderSpec(
            backend="crosstool-ng",
            version="1.28.0",
            gcc="9.5.0",
            binutils="2.45",
        ),
    )


def aarch64_glibc_spec(version: str = "2.19") -> SdkSpec:
    return SdkSpec(
        name=f"linux-toolchain-aarch64-glibc-{version}",
        target=TargetSpec(
            arch="aarch64",
            vendor="portable",
            libc="glibc",
            libc_version=version,
            linux_headers="6.12.41",
            minimum_kernel="3.10.0",
            cpu="armv8-a",
        ),
        builder=BuilderSpec(
            backend="crosstool-ng",
            version="1.28.0",
            gcc="9.5.0",
            binutils="2.29.1"
            if AbiVersion.parse(version) < AbiVersion.parse("2.26")
            else "2.45",
        ),
    )


def crosstool_ng_1_28_glibc_spec(
    version: str = "2.36", *, arch: str = "x86_64"
) -> SdkSpec:
    return SdkSpec(
        name=f"linux-toolchain-{arch}-glibc-{version}",
        target=TargetSpec(
            arch=arch,
            vendor="portable",
            libc="glibc",
            libc_version=version,
            linux_headers="6.12.41",
            minimum_kernel="3.10.0" if arch == "aarch64" else "3.2.0",
            cpu="armv8-a" if arch == "aarch64" else "x86-64",
        ),
        builder=BuilderSpec(
            backend="crosstool-ng",
            version="1.28.0",
            gcc="9.5.0",
            binutils=(
                "2.29.1"
                if arch == "aarch64"
                and AbiVersion.parse(version) < AbiVersion.parse("2.26")
                else "2.45"
            ),
        ),
    )


def write_build_outputs(root: Path, spec: SdkSpec, *, full: bool) -> None:
    toolchain = root / "toolchain"
    sysroot = toolchain / spec.target.triplet / "sysroot"
    loader = {
        "x86_64": sysroot / "lib64/ld-linux-x86-64.so.2",
        "aarch64": sysroot / "lib/ld-linux-aarch64.so.1",
    }[spec.target.arch]
    regular = (
        sysroot / "usr/include/features.h",
        sysroot / "lib64/libc.so.6",
        sysroot / "usr/lib/libc.a",
        loader,
    )
    executables = tuple(
        toolchain / "bin" / f"{spec.target.triplet}-{name}" for name in TARGET_BINUTILS
    )
    if full:
        executables += (
            toolchain / "bin" / f"{spec.target.triplet}-gcc",
            toolchain / "bin" / f"{spec.target.triplet}-g++",
        )
    for path in regular:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"complete\n")
    for path in executables:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"#!/bin/sh\n")
        path.chmod(0o755)


class CrosstoolNgConfigTest(unittest.TestCase):
    def test_sdk_identity_uses_content_and_builder_contract_not_mirror_url(
        self,
    ) -> None:
        spec = glibc_spec()
        original = CROSSTOOL_NG_RELEASES[spec.builder.version]
        with patch.dict(os.environ, {UBUNTU_BUILDER_SNAPSHOT_ENV: ""}):
            baseline = sdk_producer_identity(spec)
            self.assertEqual(baseline["builder_contract"]["apt_snapshot"], "")
            with patch.dict(
                CROSSTOOL_NG_RELEASES,
                {
                    spec.builder.version: replace(
                        original,
                        source_url="https://mirror.invalid/crosstool-ng.tar.xz",
                    )
                },
            ):
                self.assertEqual(sdk_producer_identity(spec), baseline)
            with patch.dict(
                os.environ,
                {"LINUX_TOOLCHAIN_GNU_MIRROR": "https://mirror.example/gnu"},
            ):
                self.assertEqual(sdk_producer_identity(spec), baseline)
            with patch.dict(
                CROSSTOOL_NG_RELEASES,
                {spec.builder.version: replace(original, sha256="0" * 64)},
            ):
                self.assertNotEqual(sdk_producer_identity(spec), baseline)
            with patch.dict(
                os.environ,
                {UBUNTU_BUILDER_SNAPSHOT_ENV: "20260702T000000Z"},
            ):
                self.assertNotEqual(sdk_producer_identity(spec), baseline)
            with patch(
                "linux_toolchain.sdk.crosstool_ng._packaged_builder_dockerfile_sha256",
                return_value="1" * 64,
            ):
                self.assertNotEqual(sdk_producer_identity(spec), baseline)
        with patch.dict(
            os.environ,
            {UBUNTU_BUILDER_SNAPSHOT_ENV: "latest"},
        ):
            with self.assertRaisesRegex(
                ConfigurationError,
                "must be empty or an Ubuntu snapshot timestamp",
            ):
                sdk_producer_identity(spec)

    def test_build_goal_requires_sdk_outputs_and_full_compilers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for spec in (glibc_spec(), aarch64_glibc_spec()):
                workspace = root / spec.target.arch
                with self.subTest(arch=spec.target.arch):
                    write_build_outputs(workspace, spec, full=False)
                    self.assertTrue(
                        _toolchain_outputs_ready(spec, workspace, SDK_BUILD_GOAL)
                    )
                    self.assertFalse(
                        _toolchain_outputs_ready(spec, workspace, FULL_BUILD_GOAL)
                    )

                    write_build_outputs(workspace, spec, full=True)
                    self.assertTrue(
                        _toolchain_outputs_ready(spec, workspace, FULL_BUILD_GOAL)
                    )

                    target_ar = (
                        workspace / "toolchain/bin" / f"{spec.target.triplet}-ar"
                    )
                    target_ar.unlink()
                    self.assertFalse(
                        _toolchain_outputs_ready(spec, workspace, SDK_BUILD_GOAL)
                    )

    def test_explicit_source_archives_share_a_content_addressed_cache(self) -> None:
        payload = b"one pinned source archive\n"
        sha256 = hashlib.sha256(payload).hexdigest()
        archive = PinnedArchive(
            filename="gcc-test.tar.xz",
            source_url="https://example.invalid/gcc-test.tar.xz",
            sha256=sha256,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "store/sdk-sources"
            first_workspace = root / "target-sdk"
            second_workspace = root / "compiler-backend-sdk"
            with patch(
                "linux_toolchain.sdk.crosstool_ng.urllib.request.urlopen",
                return_value=io.BytesIO(payload),
            ) as download:
                first = _download_archive(
                    archive,
                    first_workspace,
                    description="test GCC",
                    source_cache=cache,
                )
                second = _download_archive(
                    archive,
                    second_workspace,
                    description="test GCC",
                    source_cache=cache,
                )

            cached = cache / "sha256" / sha256
            download.assert_called_once()
            self.assertEqual(first.read_bytes(), payload)
            self.assertEqual(second.read_bytes(), payload)
            self.assertTrue(os.path.samefile(first, cached))
            self.assertTrue(os.path.samefile(second, cached))
            self.assertFalse(tuple(cache.rglob("*.tmp-*")))

    def test_core_archives_use_direct_https_mirrors(self) -> None:
        archives = {
            archive.filename: archive for archive in _component_archives(glibc_spec())
        }
        linux = archives["linux-6.12.41.tar.xz"]
        self.assertEqual(
            linux.source_url,
            "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.41.tar.xz",
        )
        self.assertEqual(linux.sha256, COMPONENT_SHA256[("linux", "6.12.41")])
        self.assertEqual(
            archives["gcc-9.5.0.tar.xz"].source_url,
            "https://mirrors.kernel.org/gnu/gcc/gcc-9.5.0/gcc-9.5.0.tar.xz",
        )
        self.assertTrue(archives["glibc-2.19.tar.xz"].source_url.startswith("https://"))
        self.assertTrue(
            archives["binutils-2.45.tar.xz"].source_url.startswith("https://")
        )

    def test_renders_exact_glibc_floor_and_complete_backend(self) -> None:
        config = render_config(glibc_spec())
        self.assertIn("CT_GLIBC_V_2_19=y", config)
        self.assertIn('CT_GLIBC_MIN_KERNEL_VERSION="3.2.0"', config)
        self.assertIn("# CT_GLIBC_ENABLE_DEBUG is not set", config)
        self.assertIn("CT_CC_LANG_CXX=y", config)
        self.assertIn('CT_PREFIX_DIR="/work/toolchain"', config)
        self.assertIn("CT_STATIC_TOOLCHAIN=y", config)
        self.assertIn("# CT_PREFIX_DIR_RO is not set", config)
        self.assertIn("CT_DOWNLOAD_AGENT_NONE=y", config)
        self.assertNotIn("CT_MIRROR_BASE_URL", config)
        self.assertNotIn("CT_GLIBC_V_2_23=y", config)

    def test_rejects_target_tools_linked_to_the_builder_runtime(self) -> None:
        metadata = ElfMetadata(
            path=Path("target-ar"),
            elf_class="ELF64",
            endianness="little",
            elf_type="EXEC",
            machine="x86_64",
            interpreter="/lib64/ld-linux-x86-64.so.2",
            needed=("libc.so.6",),
            rpath=(),
            runpath=(),
            has_dt_relr=False,
            version_needs=(),
        )
        inspector = Mock()
        inspector.inspect.return_value = metadata

        with self.assertRaisesRegex(ExternalToolError, "builder runtime"):
            validate_portable_target_tools(
                glibc_spec(), Path("/workspace"), inspector=inspector
            )

    def test_rejects_glibc_outside_selected_backend_family(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, r"1\.28\.0.*2\.18"):
            render_config(glibc_spec("2.18"))

    def test_crosstool_ng_1_28_family_renders_exact_component_keys(self) -> None:
        config = render_config(crosstool_ng_1_28_glibc_spec("2.36"))
        self.assertIn('CT_CONFIG_VERSION="4"', config)
        self.assertIn("CT_GLIBC_V_2_36=y", config)
        self.assertIn("CT_LINUX_V_6_12=y", config)
        self.assertIn("CT_GCC_V_9=y", config)
        self.assertIn("CT_BINUTILS_V_2_45=y", config)
        self.assertNotIn("CT_GLIBC_V_2_35=y", config)

    def test_crosstool_ng_1_28_supports_pinned_upper_bound_on_aarch64(self) -> None:
        config = render_config(crosstool_ng_1_28_glibc_spec("2.42", arch="aarch64"))
        self.assertIn("CT_ARCH_ARM=y", config)
        self.assertIn("CT_GLIBC_V_2_42=y", config)

    def test_backend_rejects_a_different_gcc(self) -> None:
        crosstool_ng_1_28 = crosstool_ng_1_28_glibc_spec()
        incompatible = SdkSpec(
            name=crosstool_ng_1_28.name,
            target=crosstool_ng_1_28.target,
            builder=BuilderSpec(
                backend="crosstool-ng",
                version="1.28.0",
                gcc="15.2.0",
                binutils="2.45",
            ),
        )
        with self.assertRaisesRegex(ConfigurationError, r"GCC.*15\.2\.0"):
            render_config(incompatible)

    def test_aarch64_rejects_pre_port_glibc(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "starts at version 2.17"):
            render_config(aarch64_glibc_spec("2.16"))

    def test_renders_aarch64_without_x86_architecture_flags(self) -> None:
        config = render_config(aarch64_glibc_spec())
        self.assertIn("CT_ARCH_ARM=y", config)
        self.assertIn("CT_ARCH_64=y", config)
        self.assertIn('CT_ARCH_ARCH="armv8-a"', config)
        self.assertIn("CT_GLIBC_V_2_19=y", config)
        self.assertIn("CT_BINUTILS_V_2_29=y", config)
        self.assertNotIn("CT_ARCH_X86=y", config)

    def test_rejects_new_binutils_with_old_aarch64_glibc(self) -> None:
        incompatible = replace(
            aarch64_glibc_spec(),
            builder=replace(aarch64_glibc_spec().builder, binutils="2.45"),
        )
        with self.assertRaisesRegex(ConfigurationError, "older than 2.30"):
            render_config(incompatible)

    def test_release_specific_builder_bases_are_digest_pinned(self) -> None:
        base = CROSSTOOL_NG_RELEASES["1.28.0"].builder_base_image
        self.assertIn("ubuntu:22.04@sha256:", base)
        self.assertEqual(len(base.rsplit(":", 1)[-1]), 64)
        self.assertEqual(
            {release.builder_platforms for release in CROSSTOOL_NG_RELEASES.values()},
            {("linux/amd64", "linux/arm64")},
        )

    def test_builder_image_cache_requires_the_exact_contract(self) -> None:
        release = CROSSTOOL_NG_RELEASES["1.28.0"]
        build_args = {
            "BASE_IMAGE": release.builder_base_image,
            "UBUNTU_SNAPSHOT": ubuntu_builder_snapshot(),
            "CROSSTOOL_NG_VERSION": release.version,
            "CROSSTOOL_NG_SHA256": release.sha256,
            "CROSSTOOL_NG_ARCHIVE": "crosstool-ng-1.28.0.tar.xz",
        }
        contract = builder_image_contract_digest(
            dockerfile_sha256="a" * 64,
            base_image=release.builder_base_image,
            pinned_input=release.sha256,
            platform="linux/amd64",
            build_args=build_args,
            target=SDK_BUILDER_TARGET,
        )
        image = {
            "Id": "sha256:" + "b" * 64,
            "RepoDigests": [],
            "Os": "linux",
            "Architecture": "amd64",
            "Config": {"Labels": {BUILDER_CONTRACT_LABEL: contract}},
        }
        reuse = Mock()
        with patch(
            "linux_toolchain.container.run",
            return_value=CommandResult(json.dumps([image]), ""),
        ):
            cached = resolve_builder_image(
                "builder:test",
                contract_digest=contract,
                platform="linux/amd64",
                build=reuse,
            )

        reuse.assert_not_called()
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.image.image_id, image["Id"])

        replacement = {
            **image,
            "Config": {"Labels": {BUILDER_CONTRACT_LABEL: "c" * 64}},
        }
        rebuild = Mock()
        with patch(
            "linux_toolchain.container.run",
            side_effect=(
                CommandResult(json.dumps([image]), ""),
                CommandResult(json.dumps([replacement]), ""),
            ),
        ):
            refreshed = resolve_builder_image(
                "builder:test",
                contract_digest="c" * 64,
                platform="linux/amd64",
                build=rebuild,
            )

        rebuild.assert_called_once_with()
        self.assertFalse(refreshed.cache_hit)
        self.assertEqual(refreshed.image.image_id, replacement["Id"])

    def test_cancelled_temporary_container_removes_owned_writer(self) -> None:
        owner = "a" * 64
        container_id = "b" * 64
        inspection = [
            {
                "Config": {
                    "Labels": {TEMPORARY_CONTAINER_LABEL: owner},
                }
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            cidfile = Path(directory) / "build.cid"
            with (
                patch(
                    "linux_toolchain.container.run",
                    side_effect=(
                        CommandResult(json.dumps(inspection), ""),
                        CommandResult(container_id + "\n", ""),
                    ),
                ) as docker,
                self.assertRaisesRegex(RuntimeError, "cancel build"),
            ):
                with temporary_container_run(
                    ["docker", "run", "builder:image", "build"],
                    cidfile=cidfile,
                    owner=owner,
                ) as (command, _):
                    self.assertIn(str(cidfile), command)
                    self.assertIn(f"{TEMPORARY_CONTAINER_LABEL}={owner}", command)
                    cidfile.write_text(container_id + "\n", encoding="ascii")
                    raise RuntimeError("cancel build")

            self.assertFalse(cidfile.exists())
            self.assertEqual(
                docker.call_args_list[-1].args[0],
                ["docker", "container", "rm", "--force", container_id],
            )

    def test_stale_cidfile_cannot_remove_an_unowned_container(self) -> None:
        owner = "a" * 64
        container_id = "b" * 64
        inspection = [
            {
                "Config": {
                    "Labels": {TEMPORARY_CONTAINER_LABEL: "c" * 64},
                }
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            cidfile = Path(directory) / "build.cid"
            cidfile.write_text(container_id + "\n", encoding="ascii")
            with (
                patch(
                    "linux_toolchain.container.run",
                    return_value=CommandResult(json.dumps(inspection), ""),
                ) as docker,
                self.assertRaisesRegex(ConfigurationError, "not owned"),
            ):
                with temporary_container_run(
                    ["docker", "run", "builder:image", "build"],
                    cidfile=cidfile,
                    owner=owner,
                ):
                    self.fail("unowned container was accepted")

            docker.assert_called_once()
            self.assertTrue(cidfile.exists())

    def test_build_rejects_custom_dockerfile_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = glibc_spec()
            render_workspace(spec, root)
            custom = root / "custom.Dockerfile"
            custom.write_text("FROM scratch\n", encoding="utf-8")

            with (
                patch("linux_toolchain.sdk.crosstool_ng._backend_archive") as archive,
                patch(
                    "linux_toolchain.sdk.crosstool_ng.run_streaming"
                ) as run_streaming,
                self.assertRaisesRegex(
                    ConfigurationError, "base-image provenance cannot be verified"
                ),
            ):
                build_with_docker(spec, root, dockerfile=custom)

            archive.assert_not_called()
            run_streaming.assert_not_called()
            workspace = json.loads(
                (root / "workspace.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("builder_dockerfile_sha256", workspace)
            self.assertNotIn("builder_base_image", workspace)

    def test_build_runs_host_preflight_before_downloads(self) -> None:
        packaged = (
            ROOT / "src" / "linux_toolchain" / "resources" / BUILDER_DOCKERFILE_NAME
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = glibc_spec()
            render_workspace(spec, root)

            with (
                patch(
                    "linux_toolchain.sdk.crosstool_ng._preflight_builder_host",
                    side_effect=ConfigurationError("Docker is unavailable"),
                ) as preflight,
                patch("linux_toolchain.sdk.crosstool_ng._backend_archive") as archive,
                self.assertRaisesRegex(ConfigurationError, "Docker is unavailable"),
            ):
                build_with_docker(spec, root, dockerfile=packaged, jobs=4)

            preflight.assert_called_once_with("linux/amd64")
            archive.assert_not_called()

    def test_build_reuses_only_a_matching_complete_toolchain(self) -> None:
        packaged = (
            ROOT / "src" / "linux_toolchain" / "resources" / BUILDER_DOCKERFILE_NAME
        )
        spec = glibc_spec()
        first_image = BuilderImage(
            image_id="sha256:" + "1" * 64,
            repo_digests=(),
            os="linux",
            architecture="amd64",
        )
        second_image = BuilderImage(
            image_id="sha256:" + "2" * 64,
            repo_digests=(),
            os="linux",
            architecture="amd64",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            render_workspace(spec, root)
            archive = root / "downloads" / "crosstool-ng-1.28.0.tar.xz"
            archive.write_bytes(b"mock crosstool-NG source")

            def run_crosstool_ng(command: list[str]) -> CommandResult:
                if command[-1] == "defconfig":
                    (root / "build/crosstool-ng/.config").write_text(
                        "\n".join(
                            (
                                "CT_ARCH_X86=y",
                                "CT_ARCH_64=y",
                                'CT_ARCH_ARCH="x86-64"',
                                'CT_LINUX_VERSION="6.12.41"',
                                'CT_BINUTILS_VERSION="2.45"',
                                'CT_GLIBC_VERSION="2.19"',
                                'CT_GLIBC_MIN_KERNEL_VERSION="3.2.0"',
                                'CT_GCC_VERSION="9.5.0"',
                                "CT_STATIC_TOOLCHAIN=y",
                            )
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return CommandResult("", "")
                self.assertEqual(command[-1], "show-tuple")
                return CommandResult(f"{spec.target.triplet}\n", "")

            def write_toolchain(command: list[str], **_: object) -> None:
                write_build_outputs(
                    root,
                    spec,
                    full=command[-1] != "STOP=libc_main",
                )

            with (
                patch(
                    "linux_toolchain.sdk.crosstool_ng._preflight_builder_host",
                    return_value=BuilderHost(uid=1000, gid=1000),
                ),
                patch(
                    "linux_toolchain.sdk.crosstool_ng._backend_archive",
                    return_value=archive,
                ),
                patch(
                    "linux_toolchain.sdk.crosstool_ng._download_archive"
                ) as download_archive,
                patch(
                    "linux_toolchain.sdk.crosstool_ng._publish_archive_file",
                    side_effect=lambda source, destination: (
                        destination.write_bytes(source.read_bytes())
                    ),
                ),
                patch(
                    "linux_toolchain.sdk.crosstool_ng.resolve_builder_image",
                    return_value=BuilderImageResolution(first_image, cache_hit=True),
                ) as resolve_image,
                patch(
                    "linux_toolchain.sdk.crosstool_ng.run",
                    side_effect=run_crosstool_ng,
                ),
                patch(
                    "linux_toolchain.sdk.crosstool_ng.run_streaming",
                    side_effect=write_toolchain,
                ) as run_build,
                patch(
                    "linux_toolchain.sdk.crosstool_ng.validate_portable_target_tools"
                ),
                patch.dict(
                    os.environ,
                    {"LINUX_TOOLCHAIN_GNU_MIRROR": "https://mirror.example/gnu"},
                ),
            ):

                def build(goal: BuildGoal = SDK_BUILD_GOAL) -> None:
                    build_with_docker(
                        spec,
                        root,
                        dockerfile=packaged,
                        goal=goal,
                    )

                build()
                receipt_path = root / "build/crosstool-ng/toolchain-ready.json"
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["goal"], SDK_BUILD_GOAL)
                self.assertEqual(len(receipt["builder_contract_sha256"]), 64)
                self.assertNotIn("builder_image_id", receipt)
                self.assertEqual(
                    resolve_image.call_args_list[0].args[0],
                    "linux-toolchain-crosstool-ng:"
                    f"1.28.0-amd64-{receipt['builder_contract_sha256'][:16]}",
                )
                run_build.assert_called_once()
                downloaded = {
                    call.args[0].filename: call.args[0]
                    for call in download_archive.call_args_list
                }
                self.assertEqual(
                    downloaded["zlib-1.3.1.tar.gz"].source_url,
                    "https://zlib.net/fossils/zlib-1.3.1.tar.gz",
                )
                self.assertEqual(
                    downloaded["gmp-6.3.0.tar.xz"].source_url,
                    "https://mirror.example/gnu/gmp/gmp-6.3.0.tar.xz",
                )
                self.assertEqual(
                    run_build.call_args.args[0][-3:],
                    ["ct-ng", "build.1", "STOP=libc_main"],
                )
                build_command = run_build.call_args.args[0]
                network_option = build_command.index("--network")
                self.assertEqual(build_command[network_option + 1], "none")

                resolve_image.return_value = BuilderImageResolution(
                    second_image, cache_hit=True
                )
                build()
                run_build.assert_called_once()

                build(FULL_BUILD_GOAL)
                self.assertEqual(run_build.call_count, 2)
                self.assertEqual(
                    run_build.call_args.args[0][-2:],
                    ["ct-ng", "build.1"],
                )
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["goal"], FULL_BUILD_GOAL)

                build()
                self.assertEqual(run_build.call_count, 2)

                (root / "toolchain/bin" / f"{spec.target.triplet}-g++").unlink()
                build()
                self.assertEqual(run_build.call_count, 2)
                build(FULL_BUILD_GOAL)
                self.assertEqual(run_build.call_count, 3)
                self.assertFalse(
                    tuple(receipt_path.parent.glob(".toolchain-ready.json.tmp-*"))
                )

                changed_dockerfile_sha256 = "f" * 64
                with (
                    patch(
                        "linux_toolchain.sdk.crosstool_ng._validate_builder_dockerfile",
                        return_value=changed_dockerfile_sha256,
                    ),
                    patch(
                        "linux_toolchain.sdk.crosstool_ng._packaged_builder_dockerfile_sha256",
                        return_value=changed_dockerfile_sha256,
                    ),
                ):
                    build(FULL_BUILD_GOAL)
                self.assertEqual(run_build.call_count, 4)

                receipt_path.unlink()
                workspace_path = root / "workspace.json"
                interrupted_workspace = json.loads(
                    workspace_path.read_text(encoding="utf-8")
                )
                interrupted_workspace["builder_dockerfile_sha256"] = "0" * 64
                workspace_path.write_text(
                    json.dumps(interrupted_workspace), encoding="utf-8"
                )
                stale = root / "toolchain/stale-output"
                stale.write_text("old state\n", encoding="utf-8")
                build(FULL_BUILD_GOAL)
                self.assertFalse(stale.exists())
                self.assertEqual(run_build.call_count, 5)

    def test_workspace_records_pinned_backend_and_rendered_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = render_workspace(glibc_spec(), root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], SDK_WORKSPACE_SCHEMA)
            self.assertEqual(manifest["format"], SDK_WORKSPACE_FORMAT)
            self.assertEqual(manifest["state"], "rendered")
            self.assertEqual(manifest["compatibility_scope"], "glibc-floor")
            self.assertEqual(manifest["backend"]["version"], "1.28.0")
            self.assertEqual(
                manifest["backend"]["builder_base_image"],
                CROSSTOOL_NG_RELEASES["1.28.0"].builder_base_image,
            )
            self.assertEqual(
                manifest["backend"]["builder_platform"],
                "linux/amd64",
            )
            self.assertEqual(len(manifest["backend"]["sha256"]), 64)
            config_files = [
                path
                for path in (root / "build" / "crosstool-ng").iterdir()
                if "config" in path.name
            ]
            self.assertEqual(len(config_files), 1)
            self.assertIn(
                "CT_GLIBC_V_2_19=y",
                config_files[0].read_text(encoding="utf-8"),
            )

    def test_refuses_nonempty_unowned_workspace_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "not-a-workspace"
            root.mkdir()
            sentinel = root / "user-data.txt"
            sentinel.write_text("preserve me\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigurationError, "not.*workspace|non-empty|refus"
            ):
                render_workspace(glibc_spec(), root)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")
            self.assertFalse((root / "workspace.json").exists())

    def test_force_rerenders_generator_owned_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = render_workspace(glibc_spec(), root)
            first_manifest = json.loads(first.read_text(encoding="utf-8"))

            second = render_workspace(glibc_spec("2.23"), root, force=True)
            second_manifest = json.loads(second.read_text(encoding="utf-8"))

            self.assertEqual(second, first)
            self.assertEqual(first_manifest["spec"]["target"]["libc_version"], "2.19")
            self.assertEqual(second_manifest["spec"]["target"]["libc_version"], "2.23")

    def test_rejects_backend_that_silently_changes_requested_version(self) -> None:
        spec = aarch64_glibc_spec()
        resolved = "\n".join(
            (
                "CT_ARCH_ARM=y",
                "CT_ARCH_64=y",
                'CT_ARCH_ARCH="armv8-a"',
                'CT_LINUX_VERSION="6.12.41"',
                'CT_BINUTILS_VERSION="2.44"',
                'CT_GLIBC_VERSION="2.19"',
                'CT_GLIBC_MIN_KERNEL_VERSION="3.10.0"',
                'CT_GCC_VERSION="9.5.0"',
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".config"
            config.write_text(resolved, encoding="utf-8")
            with self.assertRaisesRegex(ExternalToolError, "silently changed"):
                validate_resolved_config(spec, config, spec.target.triplet)


class WorkspaceProvenanceTest(unittest.TestCase):
    def source_archive_evidence(self, spec: SdkSpec) -> dict[str, dict[str, object]]:
        release = CROSSTOOL_NG_RELEASES[spec.builder.version]
        evidence = {
            f"crosstool-ng-{release.version}.tar.xz": {
                "sha256": release.sha256,
                "size": 1,
            },
        }
        evidence.update(
            {
                archive.filename: {
                    "sha256": archive.sha256,
                    "size": index + 10,
                }
                for index, archive in enumerate(_source_archives(spec))
            }
        )
        return evidence

    def export_with_evidence(
        self,
        spec: SdkSpec,
        root: Path,
        evidence: dict[str, dict[str, object]] | None = None,
        *,
        validate_side_effect: object | None = None,
    ) -> Path:
        archive_evidence = (
            self.source_archive_evidence(spec) if evidence is None else evidence
        )

        def write_license_fixture(
            _archive: Path, destination: Path, component: str
        ) -> tuple[Path, ...]:
            required = {
                "glibc": ("COPYING", "COPYING.LIB"),
                "linux": ("COPYING",),
                "gcc": ("COPYING", "COPYING.RUNTIME"),
                "binutils": ("COPYING",),
            }[component]
            root = destination / "licenses" / component
            root.mkdir(parents=True)
            result = []
            for name in required:
                path = root / name
                path.write_text(f"{component} {name}\n", encoding="utf-8")
                result.append(path)
            return tuple(result)

        with (
            patch(
                "linux_toolchain.sdk.crosstool_ng.validate_sdk",
                side_effect=validate_side_effect,
            ),
            patch(
                "linux_toolchain.sdk.crosstool_ng._download_archive_evidence",
                return_value=archive_evidence,
            ),
            patch(
                "linux_toolchain.sdk.crosstool_ng.extract_component_licenses",
                side_effect=write_license_fixture,
            ),
        ):
            return export_sdk(spec, root)

    def test_export_final_validation_uses_destination_and_restores_previous_sdk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.prepare_export_source(root)
            self.record_builder(root)
            previous = root / "sdk"
            previous.mkdir()
            sentinel = previous / "previous"
            sentinel.write_text("keep\n", encoding="utf-8")
            observed: list[Path] = []

            def validate(sysroot: Path, *, arch: str) -> None:
                observed.append(sysroot)
                if sysroot == root / "sdk" / "sysroot":
                    raise ExternalToolError("injected final SDK validation failure")

            with self.assertRaisesRegex(
                ExternalToolError, "injected final SDK validation failure"
            ):
                self.export_with_evidence(
                    spec,
                    root,
                    validate_side_effect=validate,
                )

            self.assertIn(root / "sdk" / "sysroot", observed)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def record_builder(
        self,
        root: Path,
        image_name: str = "linux-toolchain-crosstool-ng:1.28.0",
        *,
        dockerfile_sha256: str | None = None,
    ) -> dict[str, object]:
        release_version = image_name.rsplit(":", 1)[-1]
        release = CROSSTOOL_NG_RELEASES[release_version]
        base_image = release.builder_base_image
        image = {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": ["linux-toolchain-crosstool-ng@sha256:" + "b" * 64],
            "Os": "linux",
            "Architecture": "amd64",
        }
        _record_builder_provenance(
            root,
            dockerfile_sha256=(
                _packaged_builder_dockerfile_sha256()
                if dockerfile_sha256 is None
                else dockerfile_sha256
            ),
            image_name=image_name,
            image=BuilderImage(
                image_id=image["Id"],
                repo_digests=tuple(image["RepoDigests"]),
                os=image["Os"],
                architecture=image["Architecture"],
            ),
            base_image=base_image,
            builder_platform="linux/amd64",
            apt_snapshot=ubuntu_builder_snapshot(),
        )
        return image

    def prepare_export_source(
        self,
        root: Path,
        *,
        spec: SdkSpec | None = None,
        with_symlink: bool = False,
    ) -> SdkSpec:
        spec = spec or glibc_spec()
        render_workspace(spec, root)
        source = root / "toolchain" / spec.target.triplet / "sysroot"
        source.mkdir(parents=True)
        source.chmod(0o700)
        payload = source / "foo"
        payload.write_bytes(b"portable sysroot payload\n")
        payload.chmod(0o600)
        library_dir = source / "lib64"
        library_dir.mkdir()
        for name in ("libc.a", "libc.so", "libc.so.6", "crt1.o", "crti.o", "crtn.o"):
            (library_dir / name).touch()
        if with_symlink:
            (source / "alias").symlink_to("./foo")
        (root / "build" / "crosstool-ng" / ".config").write_text(
            f'CT_GLIBC_VERSION="{spec.target.libc_version}"\n', encoding="utf-8"
        )
        return spec

    def test_export_carries_builder_and_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.prepare_export_source(root)
            image = self.record_builder(root)
            archive_evidence = self.source_archive_evidence(spec)

            sdk = self.export_with_evidence(spec, root, archive_evidence)

            self.assertEqual(stat.S_IMODE(sdk.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((sdk / "sysroot").stat().st_mode), 0o755)
            self.assertEqual(
                stat.S_IMODE((sdk / "manifest.json").stat().st_mode), 0o644
            )
            self.assertEqual(
                stat.S_IMODE((sdk / "sysroot" / "foo").stat().st_mode), 0o644
            )
            manifest = json.loads((sdk / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], SDK_MANIFEST_SCHEMA)
            self.assertEqual(manifest["format"], SDK_MANIFEST_FORMAT)
            self.assertEqual(
                manifest["build_environment"],
                {
                    "dockerfile_sha256": _packaged_builder_dockerfile_sha256(),
                    "base_image": CROSSTOOL_NG_RELEASES["1.28.0"].builder_base_image,
                    "platform": "linux/amd64",
                    "apt_snapshot": ubuntu_builder_snapshot(),
                    "image": {
                        "name": "linux-toolchain-crosstool-ng:1.28.0",
                        "id": image["Id"],
                        "repo_digests": image["RepoDigests"],
                        "os": "linux",
                        "architecture": "amd64",
                        "platform": "linux/amd64",
                    },
                },
            )
            self.assertEqual(manifest["sources"]["download_archives"], archive_evidence)
            self.assertEqual(manifest["licenses"]["directory"], "licenses")
            self.assertEqual(manifest["licenses"]["format"], 1)
            self.assertIn(
                "licenses/binutils/COPYING",
                manifest["licenses"]["files"],
            )

    def test_export_rejects_workspace_without_builder_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.prepare_export_source(root)

            with self.assertRaisesRegex(ConfigurationError, "no builder provenance"):
                self.export_with_evidence(spec, root)

    def test_export_rejects_wrong_builder_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.prepare_export_source(root)
            self.record_builder(root)
            manifest_path = root / "workspace.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["builder_platform"] = "linux/arm64"
            manifest["builder_image"].update(
                {
                    "architecture": "arm64",
                    "platform": "linux/arm64",
                }
            )
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ConfigurationError, "builder platform does not match"
            ):
                self.export_with_evidence(spec, root)

    def test_export_rejects_unreviewed_builder_dockerfile_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.prepare_export_source(root)
            self.record_builder(root, dockerfile_sha256="c" * 64)

            with self.assertRaisesRegex(
                ConfigurationError,
                "Dockerfile base-image provenance cannot be verified",
            ):
                self.export_with_evidence(spec, root)

    def test_export_rejects_wrong_core_archive_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.prepare_export_source(root)
            self.record_builder(root)
            archive_evidence = self.source_archive_evidence(spec)
            glibc_archive = COMPONENT_ARCHIVES[("glibc", spec.target.libc_version)]
            archive_evidence[glibc_archive] = {
                "sha256": "0" * 64,
                "size": 1,
            }

            with self.assertRaisesRegex(
                ExternalToolError,
                rf"{glibc_archive}.*expected",
            ):
                self.export_with_evidence(spec, root, archive_evidence)

    def test_export_preserves_raw_symlink_text_and_binding_accepts_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.prepare_export_source(root, with_symlink=True)
            source = root / "toolchain" / spec.target.triplet / "sysroot"
            normalize_public_tree(source)
            self.record_builder(root)

            sdk = self.export_with_evidence(spec, root)

            exported_sysroot = sdk / "sysroot"
            self.assertEqual(os.readlink(exported_sysroot / "alias"), "./foo")
            self.assertEqual(
                (exported_sysroot / "foo").read_text(encoding="utf-8"),
                (source / "foo").read_text(encoding="utf-8"),
            )

            compiler_root = root / "compiler"
            compiler_root.mkdir()
            cc = compiler_root / "gcc"
            cxx = compiler_root / "g++"
            for driver in (cc, cxx):
                driver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                driver.chmod(0o755)
            compiler = CompilerInfo(
                family="gcc",
                version="13.2.1",
                major=13,
                target="x86_64-linux-gnu",
                cc=cc,
                cxx=cxx,
                version_text="g++ (GCC) 13.2.1",
            )
            with (
                patch(
                    "linux_toolchain.compiler.binding._verify_binding_links",
                    return_value={"status": "passed", "checks": []},
                ),
                patch(
                    "linux_toolchain.compiler.binding._verify_archive_tools",
                    return_value={"status": "passed", "checks": []},
                ),
                patch(
                    "linux_toolchain.compiler.binding._verify_target_tools",
                    return_value={"status": "passed", "checks": []},
                ),
                patch(
                    "linux_toolchain.compiler.binding._resolve_compiler_target_tools",
                    return_value=TargetTools(
                        ar=ArchiveTool("ar", Path("/usr/bin/ar")),
                        ranlib=ArchiveTool("ranlib", Path("/usr/bin/ranlib")),
                        assembler=ArchiveTool("as", Path("/usr/bin/as")),
                        nm=ArchiveTool("nm", Path("/usr/bin/nm")),
                        strip=ArchiveTool("strip", Path("/usr/bin/strip")),
                        objcopy=ArchiveTool("objcopy", Path("/usr/bin/objcopy")),
                        objdump=ArchiveTool("objdump", Path("/usr/bin/objdump")),
                    ),
                ),
            ):
                binding = create_binding(sdk, root / "binding", compiler)
            self.assertTrue(binding.is_file())

    def test_load_workspace_rejects_unknown_schema_or_format(self) -> None:
        cases = (
            ("schema", {"schema": "another-workspace"}),
            ("format", {"format": 2}),
        )
        for message, replacement in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                manifest_path = render_workspace(glibc_spec(), root)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.update(replacement)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(ConfigurationError, message):
                    load_workspace(root)


class SdkValidationTest(unittest.TestCase):
    def make_minimal_sysroot(self, root: Path) -> None:
        (root / "usr" / "include").mkdir(parents=True)
        (root / "lib64").mkdir(parents=True)
        (root / "lib64" / "libc.so.6").touch()
        (root / "lib64" / "libc.so").touch()
        (root / "lib64" / "libc.a").touch()
        (root / "lib64" / "ld-linux-x86-64.so.2").touch()
        for runtime_object in ("crt1.o", "crti.o", "crtn.o"):
            (root / "lib64" / runtime_object).touch()

    def test_accepts_minimal_glibc_sysroot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_minimal_sysroot(root)
            validate_sdk(root)

    def test_rejects_compiler_runtime_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_minimal_sysroot(root)
            (root / "usr" / "lib").mkdir(parents=True)
            (root / "usr" / "lib" / "libstdc++.so.6").touch()
            with self.assertRaisesRegex(ExternalToolError, "compiler runtimes leaked"):
                validate_sdk(root)

    def test_rejects_cxx_standard_library_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_minimal_sysroot(root)
            (root / "usr" / "include" / "c++").mkdir()
            with self.assertRaisesRegex(ExternalToolError, r"C\+\+.*headers leaked"):
                validate_sdk(root)

    def test_requires_dynamic_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_minimal_sysroot(root)
            (root / "lib64" / "ld-linux-x86-64.so.2").unlink()
            with self.assertRaisesRegex(ExternalToolError, "dynamic loader"):
                validate_sdk(root)

    def test_requires_aarch64_loader_in_architecture_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_minimal_sysroot(root)
            with self.assertRaisesRegex(ExternalToolError, "[Aa]rch64.*layout"):
                validate_sdk(root, arch="aarch64")
            (root / "lib").mkdir()
            (root / "lib" / "ld-linux-aarch64.so.1").touch()

            class Inspector:
                def inspect(self, _path: Path):
                    return type("Metadata", (), {"machine": "aarch64"})()

            validate_sdk(root, arch="aarch64", inspector=Inspector())  # type: ignore[arg-type]

    def test_rejects_absolute_symlink_that_escapes_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_minimal_sysroot(root)
            (root / "usr" / "lib").mkdir(parents=True)
            (root / "usr" / "lib" / "escaped.so").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ExternalToolError, "absolute symlink"):
                validate_sdk(root)


if __name__ == "__main__":
    unittest.main()
