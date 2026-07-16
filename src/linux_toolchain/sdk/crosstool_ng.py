from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from linux_toolchain.container import (
    BUILDER_DOCKERFILE_NAME,
    SDK_BUILDER_TARGET,
    UBUNTU_22_04_BASE_IMAGE,
    BuilderHost,
    BuilderImage,
    ContainerIdentityFiles,
    builder_image_contract_digest,
    docker_build_command,
    linux_platform_for_architecture,
    require_non_root_builder,
    resolve_builder_image,
    temporary_container_owner,
    temporary_container_run,
    ubuntu_builder_snapshot,
    validate_native_docker_daemon,
    validate_packaged_dockerfile,
    write_container_identity_files,
)
from linux_toolchain.elf.reader import ReadElfInspector, resolve_readelf_candidates
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.integrity import file_sha256
from linux_toolchain.licenses import extract_component_licenses, license_evidence
from linux_toolchain.models import (
    SDK_MANIFEST_FORMAT,
    SDK_MANIFEST_SCHEMA,
    SDK_WORKSPACE_FORMAT,
    SDK_WORKSPACE_SCHEMA,
    SdkSpec,
)
from linux_toolchain.process import run, run_streaming
from linux_toolchain.publication import replace_directory, write_json_atomic
from linux_toolchain.recipes import available_families
from linux_toolchain.versions import AbiVersion

_TOOLCHAIN_READY_SCHEMA = "linux-toolchain-crosstool-ng-toolchain-ready"
_TOOLCHAIN_READY_FORMAT = 1
_TOOLCHAIN_READY_FILE = "toolchain-ready.json"
_SDK_EXPORT_REVISION = 1

BuildGoal = Literal["sdk", "full"]
SDK_BUILD_GOAL: BuildGoal = "sdk"
FULL_BUILD_GOAL: BuildGoal = "full"
_BUILD_GOALS = frozenset((SDK_BUILD_GOAL, FULL_BUILD_GOAL))
_GNU_MIRROR_ENV = "LINUX_TOOLCHAIN_GNU_MIRROR"
_DEFAULT_GNU_MIRROR = "https://mirrors.kernel.org/gnu"
_TARGET_BINUTILS = (
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


def _require_workspace_envelope(
    value: object,
    *,
    context: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} root must be an object")
    if value.get("schema") != SDK_WORKSPACE_SCHEMA:
        raise ConfigurationError(f"{context} has an unsupported schema")
    manifest_format = value.get("format")
    if (
        not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != SDK_WORKSPACE_FORMAT
    ):
        raise ConfigurationError(f"{context} has an unsupported format")
    return value


@dataclass(frozen=True)
class CrosstoolNgRelease:
    version: str
    config_version: str
    builder_platforms: tuple[str, ...]
    builder_base_image: str
    source_url: str
    sha256: str


@dataclass(frozen=True)
class PinnedArchive:
    filename: str
    source_url: str
    sha256: str


CROSSTOOL_NG_RELEASES = {
    "1.28.0": CrosstoolNgRelease(
        version="1.28.0",
        config_version="4",
        builder_platforms=("linux/amd64", "linux/arm64"),
        builder_base_image=UBUNTU_22_04_BASE_IMAGE,
        source_url=(
            "https://github.com/crosstool-ng/crosstool-ng/releases/download/"
            "crosstool-ng-1.28.0/crosstool-ng-1.28.0.tar.xz"
        ),
        sha256="5750e29a2bda5cd8d67900592576b1670a1987a4dcd5e4f6beae09138a1f5699",
    ),
}


def _builder_platform(spec: SdkSpec, release: CrosstoolNgRelease) -> str:
    platform = linux_platform_for_architecture(spec.target.arch)
    if platform not in release.builder_platforms:
        raise ConfigurationError(
            f"crosstool-NG {release.version} does not support producer "
            f"platform {platform}"
        )
    return platform


def _component_versions_by_release() -> dict[str, dict[str, tuple[str, ...]]]:
    """Derive crosstool-NG component selectors from the backend catalog."""

    versions: dict[str, dict[str, set[str]]] = {}
    for family in available_families():
        release = versions.setdefault(
            family.builder_version,
            {"glibc": set(), "linux": set(), "gcc": set(), "binutils": set()},
        )
        release["glibc"].update(family.glibc_versions)
        release["linux"].add(family.linux_headers)
        release["gcc"].add(family.gcc)
        for architecture in family.architectures:
            release["binutils"].add(architecture.binutils)
            release["binutils"].update(
                binutils for _, binutils in architecture.binutils_by_glibc
            )
    return {
        builder_version: {
            component: tuple(sorted(values, key=AbiVersion.parse))
            for component, values in components.items()
        }
        for builder_version, components in versions.items()
    }


# This exact, release-scoped allowlist comes from the backend catalog. The
# archive hash table below proves that every selected component is pinned.
COMPONENT_VERSIONS_BY_RELEASE = _component_versions_by_release()

ARCH_CONFIG = {
    "x86_64": (
        "CT_ARCH_X86=y",
        "CT_ARCH_64=y",
    ),
    "aarch64": (
        "CT_ARCH_ARM=y",
        "CT_ARCH_64=y",
    ),
}

# SHA-256 values are copied from packages/<component>/<version>/chksum in the
# pinned crosstool-NG 1.28.0 release archive. All selected
# component archives use the first/preferred .tar.xz format from package.desc.
COMPONENT_SHA256 = {
    (
        "binutils",
        "2.29.1",
    ): "e7010a46969f9d3e53b650a518663f98a5dde3c3ae21b7d71e5e6803bc36b577",
    (
        "glibc",
        "2.17",
    ): "6914e337401e0e0ade23694e1b2c52a5f09e4eda3270c67e7c3ba93a89b5b23e",
    (
        "glibc",
        "2.19",
    ): "2d3997f588401ea095a0b27227b1d50cdfdd416236f6567b564549d3b46ea2a2",
    (
        "glibc",
        "2.23",
    ): "94efeb00e4603c8546209cefb3e1a50a5315c86fa9b078b6fad758e187ce13e9",
    (
        "glibc",
        "2.24",
    ): "99d4a3e8efd144d71488e478f62587578c0f4e1fa0b4eed47ee3d4975ebeb5d3",
    (
        "glibc",
        "2.25",
    ): "067bd9bb3390e79aa45911537d13c3721f1d9d3769931a30c2681bfee66f23a0",
    (
        "glibc",
        "2.26",
    ): "e54e0a934cd2bc94429be79da5e9385898d2306b9eaf3c92d5a77af96190f6bd",
    (
        "glibc",
        "2.27",
    ): "5172de54318ec0b7f2735e5a91d908afe1c9ca291fec16b5374d9faadfc1fc72",
    (
        "glibc",
        "2.28",
    ): "b1900051afad76f7a4f73e71413df4826dce085ef8ddb785a945b66d7d513082",
    (
        "glibc",
        "2.29",
    ): "f3eeb8d57e25ca9fc13c2af3dae97754f9f643bc69229546828e3a240e2af04b",
    (
        "glibc",
        "2.30",
    ): "e2c4114e569afbe7edbc29131a43be833850ab9a459d81beb2588016d2bbb8af",
    (
        "glibc",
        "2.31",
    ): "9246fe44f68feeec8c666bb87973d590ce0137cca145df014c72ec95be9ffd17",
    (
        "glibc",
        "2.32",
    ): "1627ea54f5a1a8467032563393e0901077626dc66f37f10ee6363bb722222836",
    (
        "glibc",
        "2.33",
    ): "2e2556000e105dbd57f0b6b2a32ff2cf173bde4f0d85dffccfd8b7e51a0677ff",
    (
        "glibc",
        "2.34",
    ): "44d26a1fe20b8853a48f470ead01e4279e869ac149b195dda4e44a195d981ab2",
    (
        "glibc",
        "2.35",
    ): "5123732f6b67ccd319305efd399971d58592122bcc2a6518a1bd2510dd0cf52e",
    (
        "glibc",
        "2.36",
    ): "1c959fea240906226062cb4b1e7ebce71a9f0e3c0836c09e7e3423d434fcfe75",
    (
        "glibc",
        "2.37",
    ): "2257eff111a1815d74f46856daaf40b019c1e553156c69d48ba0cbfc1bb91a43",
    (
        "glibc",
        "2.38",
    ): "fb82998998b2b29965467bc1b69d152e9c307d2cf301c9eafb4555b770ef3fd2",
    (
        "glibc",
        "2.39",
    ): "f77bd47cf8170c57365ae7bf86696c118adb3b120d3259c64c502d3dc1e2d926",
    (
        "glibc",
        "2.40",
    ): "19a890175e9263d748f627993de6f4b1af9cd21e03f080e4bfb3a1fac10205a2",
    (
        "glibc",
        "2.41",
    ): "a5a26b22f545d6b7d7b3dd828e11e428f24f4fac43c934fb071b6a7d0828e901",
    (
        "glibc",
        "2.42",
    ): "d1775e32e4628e64ef930f435b67bb63af7599acb6be2b335b9f19f16509f17f",
    (
        "linux",
        "6.12.41",
    ): "6b19a3ae99423de2416964d67251d745910277af258b4c4c63e88fd87dbf0e27",
    (
        "gcc",
        "9.5.0",
    ): "27769f64ef1d4cd5e2be8682c0c93f9887983e6cfd1a927ce5a0a2915a95cf8f",
    (
        "binutils",
        "2.45",
    ): "c50c0e7f9cb188980e2cc97e4537626b1672441815587f1eab69d2a1bfbef5d2",
}

COMPONENT_ARCHIVES = {
    (component, version): f"{component}-{version}.tar.xz"
    for component, version in COMPONENT_SHA256
}


def _selected_component_versions(spec: SdkSpec) -> dict[str, str]:
    return {
        "glibc": spec.target.libc_version,
        "linux": spec.target.linux_headers,
        "gcc": spec.builder.gcc,
        "binutils": spec.builder.binutils,
    }


def _gnu_archive_url(path: str) -> str:
    base = os.environ.get(_GNU_MIRROR_ENV) or _DEFAULT_GNU_MIRROR
    return f"{base.rstrip('/')}/{path}"


def _component_archives(spec: SdkSpec) -> tuple[PinnedArchive, ...]:
    result = []
    for component, version in _selected_component_versions(spec).items():
        filename = COMPONENT_ARCHIVES[(component, version)]
        if component == "linux":
            major = version.split(".", 1)[0]
            source_url = (
                f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/{filename}"
            )
        elif component == "gcc":
            source_url = _gnu_archive_url(f"gcc/gcc-{version}/{filename}")
        else:
            source_url = _gnu_archive_url(f"{component}/{filename}")
        result.append(
            PinnedArchive(
                filename=filename,
                source_url=source_url,
                sha256=COMPONENT_SHA256[(component, version)],
            )
        )
    return tuple(result)


def _support_archives(spec: SdkSpec) -> tuple[PinnedArchive, ...]:
    if spec.builder.version != "1.28.0":
        raise ConfigurationError(
            f"no pinned support archives for crosstool-NG {spec.builder.version}"
        )
    return (
        PinnedArchive(
            filename="zlib-1.3.1.tar.gz",
            source_url="https://zlib.net/fossils/zlib-1.3.1.tar.gz",
            sha256="9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23",
        ),
        PinnedArchive(
            filename="gmp-6.3.0.tar.xz",
            source_url=_gnu_archive_url("gmp/gmp-6.3.0.tar.xz"),
            sha256="a3c2b80201b89e68616f4ad30bc66aee4927c3ce50e33929ca819d5c43538898",
        ),
        PinnedArchive(
            filename="mpfr-4.2.2.tar.xz",
            source_url=_gnu_archive_url("mpfr/mpfr-4.2.2.tar.xz"),
            sha256="b67ba0383ef7e8a8563734e2e889ef5ec3c3b898a01d00fa0a6869ad81c6ce01",
        ),
        PinnedArchive(
            filename="isl-0.27.tar.xz",
            source_url="https://libisl.sourceforge.io/isl-0.27.tar.xz",
            sha256="6d8babb59e7b672e8cb7870e874f3f7b813b6e00e6af3f8b04f7579965643d5c",
        ),
        PinnedArchive(
            filename="mpc-1.3.1.tar.gz",
            source_url=_gnu_archive_url("mpc/mpc-1.3.1.tar.gz"),
            sha256="ab642492f5cf882b74aa0cb730cd410a81edcdbec895183ce930e706c1c759b8",
        ),
        PinnedArchive(
            filename="ncurses-6.5.tar.gz",
            source_url=_gnu_archive_url("ncurses/ncurses-6.5.tar.gz"),
            sha256="136d91bc269a9a5785e5f9e980bc76ab57428f604ce3e5a5a90cebc767971cc6",
        ),
        PinnedArchive(
            filename="libiconv-1.18.tar.gz",
            source_url=_gnu_archive_url("libiconv/libiconv-1.18.tar.gz"),
            sha256="3b08f5f4f9b4eb82f151a7040bfd6fe6c6fb922efe4b1659c66ea933276965e8",
        ),
        PinnedArchive(
            filename="gettext-0.26.tar.xz",
            source_url=_gnu_archive_url("gettext/gettext-0.26.tar.xz"),
            sha256="d1fb86e260cfe7da6031f94d2e44c0da55903dbae0a2fa0fae78c91ae1b56f00",
        ),
    )


def _source_archives(spec: SdkSpec) -> tuple[PinnedArchive, ...]:
    return (*_component_archives(spec), *_support_archives(spec))


def _packaged_builder_dockerfile_sha256() -> str:
    """Return the hash of the Dockerfile whose base-image use is reviewed.

    An arbitrary Dockerfile can ignore the supplied ``BASE_IMAGE`` build arg,
    so its resulting image cannot inherit the release's pinned-base
    provenance.  Exact content matching is intentionally stricter than trying
    to parse Dockerfile syntax or trusting a self-reported label.
    """

    try:
        payload = (
            files("linux_toolchain.resources")
            .joinpath(BUILDER_DOCKERFILE_NAME)
            .read_bytes()
        )
    except OSError as error:
        raise ConfigurationError(
            f"cannot read the packaged builder Dockerfile: {error}"
        ) from error
    return hashlib.sha256(payload).hexdigest()


def _validate_builder_dockerfile(dockerfile: Path) -> str:
    return validate_packaged_dockerfile(
        dockerfile,
        _packaged_builder_dockerfile_sha256(),
        provenance="Dockerfile base-image provenance",
    )


def _component_key(
    backend_version: str,
    component: str,
    component_version: str,
) -> str:
    """Return a Kconfig key only for an exact version in a pinned release.

    crosstool-NG intentionally collapses patch releases for Linux, GCC and
    binutils Kconfig symbols. Constructing that suffix is valid only after the
    complete version has passed the release-specific allowlist.
    """

    display_names = {
        "glibc": "glibc",
        "linux": "Linux headers",
        "gcc": "GCC",
        "binutils": "binutils",
    }
    supported = COMPONENT_VERSIONS_BY_RELEASE[backend_version][component]
    if component_version not in supported:
        raise ConfigurationError(
            f"crosstool-NG {backend_version} does not have a tested "
            f"{display_names[component]} mapping for {component_version}; "
            f"supported: {', '.join(supported)}"
        )

    version_parts = component_version.split(".")
    suffix_parts = {
        "glibc": version_parts,
        "linux": version_parts[:2],
        "gcc": version_parts[:1],
        "binutils": version_parts[:2],
    }[component]
    prefix = {
        "glibc": "GLIBC",
        "linux": "LINUX",
        "gcc": "GCC",
        "binutils": "BINUTILS",
    }[component]
    return f"CT_{prefix}_V_{'_'.join(suffix_parts)}"


def render_config(spec: SdkSpec) -> str:
    spec.validate()
    release = CROSSTOOL_NG_RELEASES.get(spec.builder.version)
    if release is None:
        raise ConfigurationError(
            f"unsupported crosstool-NG version: {spec.builder.version}"
        )
    if spec.target.arch == "aarch64" and AbiVersion.parse(
        spec.target.libc_version
    ) < AbiVersion.parse("2.17"):
        raise ConfigurationError("AArch64 glibc support starts at version 2.17")
    if (
        spec.target.arch == "aarch64"
        and AbiVersion.parse(spec.target.libc_version) < AbiVersion.parse("2.26")
        and AbiVersion.parse(spec.builder.binutils) >= AbiVersion.parse("2.30")
    ):
        raise ConfigurationError(
            "crosstool-NG 1.28.0 requires binutils older than 2.30 for "
            "AArch64 with glibc older than 2.26"
        )
    glibc_key = _component_key(release.version, "glibc", spec.target.libc_version)
    linux_key = _component_key(release.version, "linux", spec.target.linux_headers)
    gcc_key = _component_key(release.version, "gcc", spec.builder.gcc)
    binutils_key = _component_key(release.version, "binutils", spec.builder.binutils)
    lines = [
        f'CT_CONFIG_VERSION="{release.config_version}"',
        "CT_OBSOLETE=y",
        *ARCH_CONFIG[spec.target.arch],
        f'CT_ARCH_ARCH="{spec.target.cpu}"',
        f'CT_TARGET_VENDOR="{spec.target.vendor}"',
        "CT_KERNEL_LINUX=y",
        f"{linux_key}=y",
        "# CT_KERNEL_LINUX_INSTALL_CHECK is not set",
        f"{binutils_key}=y",
        f"{glibc_key}=y",
        "CT_GLIBC_KERNEL_VERSION_CHOSEN=y",
        f'CT_GLIBC_MIN_KERNEL_VERSION="{spec.target.minimum_kernel}"',
        "# CT_GLIBC_ENABLE_DEBUG is not set",
        f"{gcc_key}=y",
        "CT_CC_LANG_CXX=y",
        "# CT_CC_GCC_LIBMPX is not set",
        "# CT_DEBUG_GDB is not set",
        'CT_PREFIX_DIR="/work/toolchain"',
        "# CT_PREFIX_DIR_RO is not set",
        'CT_LOCAL_TARBALLS_DIR="/downloads"',
        "CT_DOWNLOAD_AGENT_NONE=y",
        "CT_RM_RF_PREFIX_DIR=y",
        "CT_LOG_EXTRA=y",
        "# CT_LOG_PROGRESS_BAR is not set",
        "CT_STATIC_TOOLCHAIN=y",
        "CT_STRIP_HOST_TOOLCHAIN_EXECUTABLES=y",
        'CT_TOOLCHAIN_PKGVERSION="linux-toolchain compiler backend"',
    ]
    return "\n".join(lines) + "\n"


def render_workspace(spec: SdkSpec, output: Path, *, force: bool = False) -> Path:
    raw_output = output.expanduser()
    if raw_output.is_symlink():
        raise ConfigurationError(f"workspace output cannot be a symlink: {raw_output}")
    output = raw_output.resolve()
    if output in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid workspace output path: {output}")
    manifest_path = output / "workspace.json"
    if output.exists():
        if not output.is_dir():
            raise ConfigurationError(f"workspace output is not a directory: {output}")
        try:
            nonempty = next(output.iterdir(), None) is not None
        except OSError as error:
            raise ConfigurationError(
                f"cannot inspect workspace output {output}: {error}"
            ) from error
        if nonempty:
            if not force:
                raise ConfigurationError(
                    f"workspace already exists and is non-empty: {output}; "
                    "pass --force only for a generator-owned workspace"
                )
            owner_marker = output / ".linux-toolchain-workspace"
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                backend = (
                    existing.get("backend") if isinstance(existing, dict) else None
                )
                owned = (
                    owner_marker.is_file()
                    and not owner_marker.is_symlink()
                    and owner_marker.read_text(encoding="utf-8") == "format=1\n"
                    and manifest_path.is_file()
                    and not manifest_path.is_symlink()
                    and isinstance(existing, dict)
                    and existing.get("schema") == SDK_WORKSPACE_SCHEMA
                    and existing.get("format") == SDK_WORKSPACE_FORMAT
                    and existing.get("compatibility_scope") == "glibc-floor"
                    and isinstance(existing.get("spec"), dict)
                    and isinstance(backend, dict)
                    and backend.get("name") == "crosstool-ng"
                    and existing.get("publishable_sdk") == "sdk"
                    and existing.get("compiler_backend") == "toolchain"
                )
            except (OSError, json.JSONDecodeError):
                owned = False
            if not owned:
                raise ConfigurationError(
                    f"refusing to overwrite directory without a workspace marker: {output}"
                )
    crosstool_ng_dir = output / "build" / "crosstool-ng"
    crosstool_ng_dir.mkdir(parents=True, exist_ok=True)
    (output / ".linux-toolchain-workspace").write_text("format=1\n", encoding="utf-8")
    (output / "home").mkdir(parents=True, exist_ok=True)
    (output / "downloads").mkdir(parents=True, exist_ok=True)
    # crosstool-NG's mini-defconfig interface expands this intentionally small,
    # reviewable input into a complete .config.  Writing .config directly and
    # calling olddefconfig would make omitted defaults part of our public
    # public behavior and is much harder to review across backend releases.
    defconfig = render_config(spec)
    (crosstool_ng_dir / "defconfig").write_text(defconfig, encoding="utf-8")

    release = CROSSTOOL_NG_RELEASES[spec.builder.version]
    builder_platform = _builder_platform(spec, release)
    manifest = {
        "schema": SDK_WORKSPACE_SCHEMA,
        "format": SDK_WORKSPACE_FORMAT,
        "state": "rendered",
        "compatibility_scope": "glibc-floor",
        "spec": spec.to_manifest_dict(),
        "backend": {
            "name": "crosstool-ng",
            "version": release.version,
            "builder_platform": builder_platform,
            "builder_base_image": release.builder_base_image,
            "source_url": release.source_url,
            "sha256": release.sha256,
        },
        "defconfig_sha256": hashlib.sha256(defconfig.encode()).hexdigest(),
        "publishable_sdk": "sdk",
        "compiler_backend": "toolchain",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def validate_resolved_config(
    spec: SdkSpec, config_path: Path, target_tuple: str
) -> None:
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ExternalToolError(
            f"crosstool-NG did not produce a readable .config: {error}"
        ) from error
    values: dict[str, str] = {}
    for line in lines:
        if line.startswith("CT_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')

    arch_key = "CT_ARCH_X86" if spec.target.arch == "x86_64" else "CT_ARCH_ARM"
    expected = {
        arch_key: "y",
        "CT_ARCH_64": "y",
        "CT_ARCH_ARCH": spec.target.cpu,
        "CT_LINUX_VERSION": spec.target.linux_headers,
        "CT_BINUTILS_VERSION": spec.builder.binutils,
        "CT_GLIBC_VERSION": spec.target.libc_version,
        "CT_GLIBC_MIN_KERNEL_VERSION": spec.target.minimum_kernel,
        "CT_GCC_VERSION": spec.builder.gcc,
        "CT_STATIC_TOOLCHAIN": "y",
    }
    if spec.target.arch == "aarch64":
        expected["CT_ARCH_ENDIAN"] = "little"
    mismatches = [
        f"{key}: expected {wanted!r}, resolved {values.get(key)!r}"
        for key, wanted in expected.items()
        if values.get(key) != wanted
    ]
    if target_tuple != spec.target.triplet:
        mismatches.append(
            f"target tuple: expected {spec.target.triplet!r}, resolved {target_tuple!r}"
        )
    if values.get("CT_CC_GCC_LIBMPX") == "y":
        mismatches.append("CT_CC_GCC_LIBMPX: compiler runtime must be disabled")
    if mismatches:
        raise ExternalToolError(
            "crosstool-NG silently changed the requested configuration:\n"
            + "\n".join(mismatches)
        )


def _readelf_executable() -> str | None:
    candidates = resolve_readelf_candidates(resolver=shutil.which)
    return candidates[0] if candidates else None


def _preflight_builder_host(expected_platform: str) -> BuilderHost:
    host = require_non_root_builder("SDK production")
    docker = shutil.which("docker")
    if docker is None:
        raise ConfigurationError(
            "Docker CLI is required for SDK production; install Docker and "
            "configure a local Unix daemon"
        )
    if _readelf_executable() is None:
        raise ConfigurationError(
            "GNU readelf or llvm-readelf is required before SDK production; "
            "install binutils/LLVM or set LINUX_TOOLCHAIN_READELF"
        )

    validate_native_docker_daemon(
        docker,
        expected_platform,
        context="SDK production",
    )
    return host


def _write_container_identity_files(
    workspace: Path, host: BuilderHost
) -> ContainerIdentityFiles:
    return write_container_identity_files(
        workspace,
        host,
        account_description="SDK builder",
        home="/work/home",
        shell="/bin/sh",
    )


def _docker_args(
    workspace: Path,
    image: str,
    *,
    builder_platform: str,
    identity: ContainerIdentityFiles,
) -> list[str]:
    args = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--platform",
        builder_platform,
        "--user",
        f"{identity.uid}:{identity.gid}",
        "--env",
        "HOME=/work/home",
        "--env",
        "DEFCONFIG=/work/build/crosstool-ng/defconfig",
        "--volume",
        f"{workspace}:/work",
        "--volume",
        f"{workspace / 'downloads'}:/downloads",
        # crosstool-NG calls `id -un` and refuses a numeric UID without an NSS
        # name.  Mount only generated entries instead of exposing host account
        # databases to the builder.
        "--volume",
        f"{identity.passwd}:/etc/passwd:ro",
        "--volume",
        f"{identity.group}:/etc/group:ro",
        "--workdir",
        "/work/build/crosstool-ng",
        image,
    ]
    return args


def _build_goal(value: object) -> BuildGoal:
    if value == SDK_BUILD_GOAL:
        return SDK_BUILD_GOAL
    if value == FULL_BUILD_GOAL:
        return FULL_BUILD_GOAL
    raise ConfigurationError(
        f"crosstool-NG build goal must be one of: {', '.join(sorted(_BUILD_GOALS))}"
    )


def _goal_satisfies(recorded: BuildGoal, requested: BuildGoal) -> bool:
    return recorded == requested or (
        recorded == FULL_BUILD_GOAL and requested == SDK_BUILD_GOAL
    )


def _ct_ng_build_command(
    docker_args: list[str], jobs: int, goal: BuildGoal
) -> list[str]:
    command = [*docker_args, "ct-ng", f"build.{jobs}"]
    if goal == SDK_BUILD_GOAL:
        command.append("STOP=libc_main")
    return command


def _builder_build_args(
    release: CrosstoolNgRelease,
    archive: Path,
    apt_snapshot: str,
) -> dict[str, str]:
    return {
        "BASE_IMAGE": release.builder_base_image,
        "UBUNTU_SNAPSHOT": apt_snapshot,
        "CROSSTOOL_NG_VERSION": release.version,
        "CROSSTOOL_NG_SHA256": release.sha256,
        "CROSSTOOL_NG_ARCHIVE": archive.name,
    }


def _builder_contract_digest(
    release: CrosstoolNgRelease,
    dockerfile_sha256: str,
    builder_platform: str,
    apt_snapshot: str,
) -> str:
    return builder_image_contract_digest(
        dockerfile_sha256=dockerfile_sha256,
        base_image=release.builder_base_image,
        pinned_input=release.sha256,
        platform=builder_platform,
        target=SDK_BUILDER_TARGET,
        build_args=_builder_build_args(
            release,
            Path(f"crosstool-ng-{release.version}.tar.xz"),
            apt_snapshot,
        ),
    )


def sdk_producer_identity(spec: SdkSpec) -> dict[str, object]:
    """Return the relocatable inputs that determine an SDK build workspace."""

    spec.validate()
    release = CROSSTOOL_NG_RELEASES[spec.builder.version]
    builder_platform = _builder_platform(spec, release)
    dockerfile_sha256 = _packaged_builder_dockerfile_sha256()
    apt_snapshot = ubuntu_builder_snapshot()
    builder = {
        "backend": spec.builder.backend,
        "version": spec.builder.version,
        "gcc": spec.builder.gcc,
        "binutils": spec.builder.binutils,
    }
    return {
        "kind": "sdk",
        "config_sha256": hashlib.sha256(render_config(spec).encode()).hexdigest(),
        "export_revision": _SDK_EXPORT_REVISION,
        "target": spec.to_dict()["target"],
        "builder": builder,
        "backend_source": {
            "sha256": release.sha256,
        },
        "component_sources": [
            {
                "filename": archive.filename,
                "sha256": archive.sha256,
            }
            for archive in _source_archives(spec)
        ],
        "builder_contract": {
            "dockerfile_sha256": dockerfile_sha256,
            "base_image": release.builder_base_image,
            "platform": builder_platform,
            "apt_snapshot": apt_snapshot,
            "sha256": _builder_contract_digest(
                release,
                dockerfile_sha256,
                builder_platform,
                apt_snapshot,
            ),
        },
    }


def _archive_matches(path: Path, sha256: str) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and file_sha256(path) == sha256
    except OSError:
        return False


def _download_archive_file(
    archive: PinnedArchive,
    destination: Path,
    *,
    description: str,
) -> Path:
    if _archive_matches(destination, archive.sha256):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    request = urllib.request.Request(
        archive.source_url,
        headers={"User-Agent": "linux-toolchain/0.1"},
    )
    digest = hashlib.sha256()
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.tmp-",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        with (
            os.fdopen(descriptor, "wb") as stream,
            urllib.request.urlopen(request, timeout=60) as response,
        ):
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, urllib.error.URLError) as error:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise ExternalToolError(f"cannot download {description}: {error}") from error
    actual = digest.hexdigest()
    if actual != archive.sha256:
        temporary.unlink(missing_ok=True)
        raise ExternalToolError(
            f"{description} checksum mismatch: expected {archive.sha256}, got {actual}"
        )
    try:
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ExternalToolError(
            f"cannot publish downloaded {description}: {error}"
        ) from error
    return destination


def _cache_directory(path: Path, *, context: str = "SDK source cache") -> Path:
    raw = path.expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"{context} cannot be a symlink: {raw}")
    try:
        resolved = raw.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigurationError(f"cannot prepare {context} {raw}: {error}") from error
    if resolved.is_symlink() or not resolved.is_dir():
        raise ConfigurationError(f"{context} is not a directory: {resolved}")
    return resolved


@contextmanager
def _source_cache_lock(cache: Path, sha256: str) -> Iterator[None]:
    lock_directory = _cache_directory(
        cache / "locks",
        context="SDK source cache lock directory",
    )
    lock_path = lock_directory / f"{sha256}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ConfigurationError(
            f"cannot open SDK source cache lock {lock_path}: {error}"
        ) from error
    with os.fdopen(descriptor, "r+", encoding="ascii") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise ConfigurationError(
                f"cannot lock SDK source cache object {sha256}: {error}"
            ) from error
        yield


def _publish_archive_file(source: Path, destination: Path) -> None:
    temporary: Path | None = None
    try:
        if source.is_symlink() or not source.is_file():
            raise ExternalToolError(f"SDK source archive is invalid: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.tmp-",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
        temporary.unlink()
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    except (OSError, ExternalToolError) as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if isinstance(error, ExternalToolError):
            raise
        raise ExternalToolError(
            f"cannot publish SDK source archive {destination}: {error}"
        ) from error


def _download_archive(
    archive: PinnedArchive,
    workspace: Path,
    *,
    description: str,
    source_cache: Path | None = None,
) -> Path:
    destination = workspace / "downloads" / archive.filename
    if source_cache is None:
        return _download_archive_file(
            archive,
            destination,
            description=description,
        )

    cache = _cache_directory(source_cache)
    object_directory = _cache_directory(
        cache / "sha256",
        context="SDK source cache object directory",
    )
    cached = object_directory / archive.sha256
    with _source_cache_lock(cache, archive.sha256):
        destination_ready = _archive_matches(destination, archive.sha256)
        same_object = False
        if destination_ready:
            try:
                same_object = cached.is_file() and os.path.samefile(destination, cached)
            except OSError:
                pass
        if not same_object and not _archive_matches(cached, archive.sha256):
            if destination_ready:
                _publish_archive_file(destination, cached)
            else:
                _download_archive_file(
                    archive,
                    cached,
                    description=description,
                )
        if not destination_ready:
            _publish_archive_file(cached, destination)
    return destination


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _backend_archive(
    release: CrosstoolNgRelease,
    workspace: Path,
    *,
    source_cache: Path | None = None,
) -> Path:
    return _download_archive(
        PinnedArchive(
            filename=f"crosstool-ng-{release.version}.tar.xz",
            source_url=release.source_url,
            sha256=release.sha256,
        ),
        workspace,
        description=f"crosstool-NG {release.version}",
        source_cache=source_cache,
    )


def _record_builder_provenance(
    workspace: Path,
    *,
    dockerfile_sha256: str,
    image_name: str,
    image: BuilderImage,
    base_image: str,
    builder_platform: str,
    apt_snapshot: str,
) -> None:
    if image.platform != builder_platform:
        raise ExternalToolError(
            f"builder image platform is {image.platform!r}; expected "
            f"{builder_platform!r}"
        )

    manifest_path = workspace / "workspace.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data = _require_workspace_envelope(
            data,
            context="workspace manifest",
        )
        data["builder_dockerfile_sha256"] = dockerfile_sha256
        data["builder_base_image"] = base_image
        data["builder_platform"] = builder_platform
        data["builder_apt_snapshot"] = apt_snapshot
        data["builder_image"] = {
            "name": image_name,
            "id": image.image_id,
            "repo_digests": list(image.repo_digests),
            "os": image.os,
            "architecture": image.architecture,
            "platform": image.platform,
        }
        manifest_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot record builder provenance in {manifest_path}: {error}"
        ) from error


def _download_archive_evidence(workspace: Path) -> dict[str, dict[str, object]]:
    downloads = workspace / "downloads"
    try:
        archives = tuple(
            sorted(
                (path for path in downloads.iterdir() if path.is_file()),
                key=lambda path: path.name,
            )
        )
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect downloaded source archives: {error}"
        ) from error
    return {
        path.name: {"sha256": file_sha256(path), "size": path.stat().st_size}
        for path in archives
    }


def _verify_source_archives(
    spec: SdkSpec,
    evidence: dict[str, dict[str, object]],
) -> None:
    release = CROSSTOOL_NG_RELEASES[spec.builder.version]
    expected_archives = {
        f"crosstool-ng-{release.version}.tar.xz": release.sha256,
        **{archive.filename: archive.sha256 for archive in _source_archives(spec)},
    }
    for filename, expected in expected_archives.items():
        actual = evidence.get(filename, {}).get("sha256")
        if actual != expected:
            raise ExternalToolError(
                f"source archive {filename} has SHA-256 {actual!r}; expected {expected}"
            )


def _redact_proxy_values(log_path: Path) -> None:
    secrets = {
        value
        for variable in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "NO_PROXY",
            "no_proxy",
        )
        if (value := os.environ.get(variable))
    }
    if not secrets or not log_path.is_file():
        return
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        for secret in secrets:
            text = text.replace(secret, "<redacted-proxy>")
        log_path.write_text(text, encoding="utf-8")
    except OSError:
        # Redaction must never hide the original build failure.  The SDK export
        # itself never includes build.log.
        pass


def _toolchain_receipt_path(workspace: Path) -> Path:
    return workspace / "build" / "crosstool-ng" / _TOOLCHAIN_READY_FILE


def _toolchain_output_paths(
    spec: SdkSpec,
    workspace: Path,
    goal: BuildGoal,
) -> tuple[tuple[Path, bool], ...]:
    toolchain = workspace / "toolchain"
    sysroot = toolchain / spec.target.triplet / "sysroot"
    loader = {
        "x86_64": sysroot / "lib64" / "ld-linux-x86-64.so.2",
        "aarch64": sysroot / "lib" / "ld-linux-aarch64.so.1",
    }[spec.target.arch]
    target_tools = tuple(
        (toolchain / "bin" / f"{spec.target.triplet}-{name}", True)
        for name in _TARGET_BINUTILS
    )
    sdk_outputs = (
        (sysroot / "usr" / "include" / "features.h", False),
        (sysroot / "lib64" / "libc.so.6", False),
        (sysroot / "usr" / "lib" / "libc.a", False),
        (loader, False),
        *target_tools,
    )
    if goal == SDK_BUILD_GOAL:
        return sdk_outputs
    return (
        *sdk_outputs,
        (toolchain / "bin" / f"{spec.target.triplet}-gcc", True),
        (toolchain / "bin" / f"{spec.target.triplet}-g++", True),
    )


def _toolchain_outputs_ready(
    spec: SdkSpec,
    workspace: Path,
    goal: BuildGoal,
) -> bool:
    for path, executable in _toolchain_output_paths(spec, workspace, goal):
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
            if executable and not os.access(path, os.X_OK):
                return False
        except OSError:
            return False
    return True


def validate_portable_target_tools(
    spec: SdkSpec,
    workspace: Path,
    *,
    inspector: ReadElfInspector | None = None,
) -> None:
    """Require the private target tools copied into Compiler Kits to be static."""

    reader = inspector or ReadElfInspector()
    for name in _TARGET_BINUTILS:
        path = workspace / "toolchain" / "bin" / f"{spec.target.triplet}-{name}"
        metadata = reader.inspect(path)
        if (
            metadata.machine != spec.target.arch
            or metadata.elf_class != "ELF64"
            or metadata.endianness != "little"
        ):
            raise ExternalToolError(
                "crosstool-NG target tool has the wrong build-host architecture: "
                f"{path}: {metadata.machine}/{metadata.elf_class}/"
                f"{metadata.endianness}"
            )
        if (
            metadata.interpreter is not None
            or metadata.needed
            or metadata.version_needs
            or metadata.rpath
            or metadata.runpath
        ):
            raise ExternalToolError(
                "crosstool-NG target tool is dynamically linked to the builder "
                f"runtime: {path}"
            )


def _toolchain_receipt(
    spec: SdkSpec,
    workspace: Path,
    *,
    defconfig_sha256: str,
    target_tuple: str,
    builder_contract_sha256: str,
    goal: BuildGoal,
) -> dict[str, object]:
    return {
        "schema": _TOOLCHAIN_READY_SCHEMA,
        "format": _TOOLCHAIN_READY_FORMAT,
        "goal": goal,
        "spec": spec.to_manifest_dict(),
        "defconfig_sha256": defconfig_sha256,
        "resolved_config_sha256": file_sha256(
            workspace / "build" / "crosstool-ng" / ".config"
        ),
        "target_tuple": target_tuple,
        "builder_contract_sha256": builder_contract_sha256,
    }


def workspace_satisfies_build_goal(
    spec: SdkSpec,
    workspace: Path,
    goal: BuildGoal,
    *,
    dockerfile_sha256: str | None = None,
    builder_contract_sha256: str | None = None,
) -> bool:
    requested_goal = _build_goal(goal)
    receipt_path = _toolchain_receipt_path(workspace)
    workspace_path = workspace / "workspace.json"
    defconfig_path = workspace / "build" / "crosstool-ng" / "defconfig"
    if (
        receipt_path.is_symlink()
        or not receipt_path.is_file()
        or workspace_path.is_symlink()
        or not workspace_path.is_file()
        or defconfig_path.is_symlink()
        or not defconfig_path.is_file()
    ):
        return False
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
        workspace_value = _require_workspace_envelope(
            json.loads(workspace_path.read_text(encoding="utf-8")),
            context="workspace manifest",
        )
        if not isinstance(value, dict):
            return False
        recorded_goal = _build_goal(value.get("goal"))
        release = CROSSTOOL_NG_RELEASES[spec.builder.version]
        builder_platform = _builder_platform(spec, release)
        apt_snapshot = ubuntu_builder_snapshot()
        requested_dockerfile_sha256 = (
            _packaged_builder_dockerfile_sha256()
            if dockerfile_sha256 is None
            else dockerfile_sha256
        )
        requested_contract_sha256 = (
            _builder_contract_digest(
                release,
                requested_dockerfile_sha256,
                builder_platform,
                apt_snapshot,
            )
            if builder_contract_sha256 is None
            else builder_contract_sha256
        )
        defconfig_sha256 = file_sha256(defconfig_path)
        if (
            workspace_value.get("builder_dockerfile_sha256")
            != requested_dockerfile_sha256
            or workspace_value.get("builder_base_image") != release.builder_base_image
            or workspace_value.get("builder_platform") != builder_platform
            or workspace_value.get("builder_apt_snapshot") != apt_snapshot
            or workspace_value.get("defconfig_sha256") != defconfig_sha256
        ):
            return False
        expected = _toolchain_receipt(
            spec,
            workspace,
            defconfig_sha256=defconfig_sha256,
            target_tuple=spec.target.triplet,
            builder_contract_sha256=requested_contract_sha256,
            goal=recorded_goal,
        )
        validate_resolved_config(
            spec,
            workspace / "build" / "crosstool-ng" / ".config",
            spec.target.triplet,
        )
    except (
        ConfigurationError,
        ExternalToolError,
        OSError,
        TypeError,
        json.JSONDecodeError,
    ):
        return False
    if not (
        value == expected
        and _goal_satisfies(recorded_goal, requested_goal)
        and _toolchain_outputs_ready(spec, workspace, requested_goal)
    ):
        return False
    try:
        validate_portable_target_tools(spec, workspace)
    except (ConfigurationError, ExternalToolError, OSError):
        return False
    return True


def _remove_toolchain_receipt(workspace: Path) -> None:
    path = _toolchain_receipt_path(workspace)
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise ExternalToolError(
            f"cannot invalidate toolchain receipt {path}: {error}"
        ) from error


def _reset_toolchain_for_builder_contract(workspace: Path) -> None:
    """Discard outputs that may have been produced by another builder identity."""

    build = workspace / "build" / "crosstool-ng"
    defconfig = build / "defconfig"
    try:
        payload = defconfig.read_bytes()
        for path in (workspace / "toolchain", workspace / "home", build):
            if path.is_symlink():
                raise ConfigurationError(
                    f"cannot reset symlinked crosstool-NG build path: {path}"
                )
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                raise ConfigurationError(
                    f"cannot reset non-directory crosstool-NG build path: {path}"
                )
        build.mkdir(parents=True)
        defconfig.write_bytes(payload)
        (workspace / "home").mkdir()
    except OSError as error:
        raise ExternalToolError(
            f"cannot reset crosstool-NG outputs after a builder change: {error}"
        ) from error


def _write_toolchain_receipt(workspace: Path, value: dict[str, object]) -> None:
    path = _toolchain_receipt_path(workspace)
    try:
        write_json_atomic(path, value)
    except ConfigurationError as error:
        raise ExternalToolError(f"cannot write toolchain receipt {path}") from error


def build_with_docker(
    spec: SdkSpec,
    workspace: Path,
    *,
    dockerfile: Path,
    image: str | None = None,
    jobs: int | None = None,
    progress: Callable[[str], None] | None = None,
    source_cache: Path | None = None,
    goal: BuildGoal = SDK_BUILD_GOAL,
) -> None:
    selected_goal = _build_goal(goal)
    # Fail before downloads or Docker side effects.  Only the reviewed
    # Dockerfile is known to consume the digest-pinned BASE_IMAGE build arg.
    dockerfile_sha256 = _validate_builder_dockerfile(dockerfile)
    defconfig_path = workspace / "build" / "crosstool-ng" / "defconfig"
    expected_defconfig = hashlib.sha256(render_config(spec).encode()).hexdigest()
    if (
        not defconfig_path.is_file()
        or file_sha256(defconfig_path) != expected_defconfig
    ):
        raise ConfigurationError(
            "workspace defconfig does not match its SDK spec; rerun `sdk render`"
        )
    try:
        workspace_data = json.loads(
            (workspace / "workspace.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot verify workspace manifest: {error}"
        ) from error
    workspace_data = _require_workspace_envelope(
        workspace_data,
        context="workspace manifest",
    )
    if workspace_data.get("defconfig_sha256") != expected_defconfig:
        raise ConfigurationError(
            "workspace manifest does not match its defconfig; rerun `sdk render`"
        )
    release = CROSSTOOL_NG_RELEASES[spec.builder.version]
    builder_platform = _builder_platform(spec, release)
    apt_snapshot = ubuntu_builder_snapshot()
    contract_digest = _builder_contract_digest(
        release,
        dockerfile_sha256,
        builder_platform,
        apt_snapshot,
    )
    build_jobs = 1 if jobs is None else jobs
    if (
        not isinstance(build_jobs, int)
        or isinstance(build_jobs, bool)
        or build_jobs < 1
    ):
        raise ConfigurationError("SDK build jobs must be a positive integer")
    expected_builder = {
        "builder_dockerfile_sha256": dockerfile_sha256,
        "builder_base_image": release.builder_base_image,
        "builder_platform": builder_platform,
        "builder_apt_snapshot": apt_snapshot,
    }
    if any(workspace_data.get(key) != value for key, value in expected_builder.items()):
        _reset_toolchain_for_builder_contract(workspace)
    if workspace_satisfies_build_goal(
        spec,
        workspace,
        selected_goal,
        dockerfile_sha256=dockerfile_sha256,
        builder_contract_sha256=contract_digest,
    ):
        _emit(progress, f"sdk: reusing completed crosstool-NG {selected_goal} build")
        return
    host = _preflight_builder_host(builder_platform)
    identity = _write_container_identity_files(workspace, host)
    platform_suffix = builder_platform.removeprefix("linux/")
    image_name = image or (
        "linux-toolchain-crosstool-ng:"
        f"{release.version}-{platform_suffix}-{contract_digest[:16]}"
    )
    _emit(progress, "sdk: acquiring pinned source archives")
    archive = _backend_archive(release, workspace, source_cache=source_cache)
    for pinned_archive in _source_archives(spec):
        _download_archive(
            pinned_archive,
            workspace,
            description=pinned_archive.filename,
            source_cache=source_cache,
        )
    build_context = workspace / "build" / "docker-context"
    build_context.mkdir(parents=True, exist_ok=True)
    context_archive = build_context / archive.name
    if not _archive_matches(context_archive, release.sha256):
        _publish_archive_file(archive, context_archive)
    build_args = _builder_build_args(release, archive, apt_snapshot)

    def build_image() -> None:
        _emit(progress, "sdk: preparing crosstool-NG builder image")
        run_streaming(
            docker_build_command(
                dockerfile=dockerfile,
                image=image_name,
                build_args=build_args,
                contract_digest=contract_digest,
                context=build_context,
                platform=builder_platform,
                target=SDK_BUILDER_TARGET,
            )
        )

    resolution = resolve_builder_image(
        image_name,
        contract_digest=contract_digest,
        platform=builder_platform,
        build=build_image,
    )
    if resolution.cache_hit:
        _emit(progress, "sdk: using cached crosstool-NG builder image")
    builder_image = resolution.image
    _record_builder_provenance(
        workspace,
        dockerfile_sha256=dockerfile_sha256,
        image_name=image_name,
        image=builder_image,
        base_image=release.builder_base_image,
        builder_platform=builder_platform,
        apt_snapshot=apt_snapshot,
    )
    docker = _docker_args(
        workspace.resolve(),
        builder_image.image_id,
        builder_platform=builder_platform,
        identity=identity,
    )
    _remove_toolchain_receipt(workspace)
    run([*docker, "ct-ng", "defconfig"])
    tuple_result = run([*docker, "ct-ng", "show-tuple"])
    tuple_lines = tuple_result.stdout.strip().splitlines()
    if not tuple_lines:
        raise ExternalToolError("crosstool-NG show-tuple returned no target tuple")
    target_tuple = tuple_lines[-1]
    validate_resolved_config(
        spec,
        workspace / "build" / "crosstool-ng" / ".config",
        target_tuple,
    )
    try:
        message = (
            "sdk: building sysroot and target tools"
            if selected_goal == SDK_BUILD_GOAL
            else "sdk: building full compiler backend"
        )
        _emit(progress, message)
        owner = temporary_container_owner(workspace, "crosstool-ng-build")
        cidfile = workspace / "build" / "crosstool-ng" / "build.cid"
        with temporary_container_run(
            _ct_ng_build_command(docker, build_jobs, selected_goal),
            cidfile=cidfile,
            owner=owner,
        ) as (command, cancel):
            run_streaming(command, cancel=cancel)
    except ExternalToolError as error:
        log = workspace / "build" / "crosstool-ng" / "build.log"
        raise ExternalToolError(
            f"crosstool-NG SDK build failed; full log: {log}"
        ) from error
    finally:
        _redact_proxy_values(workspace / "build" / "crosstool-ng" / "build.log")
    if not _toolchain_outputs_ready(spec, workspace, selected_goal):
        expected = (
            "SDK sysroot and target tools"
            if selected_goal == SDK_BUILD_GOAL
            else "full C/C++ compiler backend"
        )
        raise ExternalToolError(f"crosstool-NG reported success without {expected}")
    validate_portable_target_tools(spec, workspace)
    _write_toolchain_receipt(
        workspace,
        _toolchain_receipt(
            spec,
            workspace,
            defconfig_sha256=expected_defconfig,
            target_tuple=target_tuple,
            builder_contract_sha256=contract_digest,
            goal=selected_goal,
        ),
    )


_FORBIDDEN_SDK_NAMES = (
    "libstdc++",
    "libgcc",
    "libatomic",
    "libasan",
    "libtsan",
    "libubsan",
    "libgomp",
    "libmpx",
    "libquadmath",
    "libssp",
    "libitm",
    "libvtv",
    "libcilkrts",
    "liboffload",
    "libsanitizer",
    "crtbegin",
    "crtend",
)


def _ignore_runtime(directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "c++"
        or (name == "gcc" and Path(directory).name in {"lib", "lib64"})
        or any(name.startswith(prefix) for prefix in _FORBIDDEN_SDK_NAMES)
    }


def _sdk_entries(sysroot: Path) -> tuple[Path, ...]:
    root = sysroot.resolve()
    entries: list[Path] = []
    for path in root.rglob("*"):
        entries.append(path)
        if not path.is_symlink():
            continue
        target = Path(os.readlink(path))
        if target.is_absolute():
            raise ExternalToolError(
                f"SDK contains a non-relocatable absolute symlink: {path} -> {target}"
            )
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as error:
            raise ExternalToolError(
                f"SDK symlink escapes the sysroot or is dangling: {path} -> {target}"
            ) from error
    return tuple(entries)


def validate_sdk(
    sysroot: Path,
    *,
    arch: str | None = None,
    inspector: ReadElfInspector | None = None,
) -> None:
    entries = _sdk_entries(sysroot)
    files = tuple(path for path in entries if path.is_file())
    names = {path.name for path in files}
    required_dir = sysroot / "usr" / "include"
    if not required_dir.is_dir():
        raise ExternalToolError(f"SDK is missing glibc headers: {required_dir}")
    if "libc.so.6" not in names:
        raise ExternalToolError("SDK is missing libc.so.6")
    if "libc.a" not in names:
        raise ExternalToolError("SDK is missing libc.a")
    if not any(path.match("ld-linux*.so*") for path in files):
        raise ExternalToolError("SDK is missing the glibc dynamic loader")
    layouts = {
        "x86_64": (
            Path("lib64") / "ld-linux-x86-64.so.2",
            Path("lib64") / "libc.so.6",
        ),
        "aarch64": (
            Path("lib") / "ld-linux-aarch64.so.1",
            Path("lib64") / "libc.so.6",
        ),
    }
    if arch in layouts:
        assert arch is not None
        display_arch = "AArch64" if arch == "aarch64" else arch
        runtime_paths = tuple(sysroot / relative for relative in layouts[arch])
        for relative, runtime_path in zip(layouts[arch], runtime_paths, strict=True):
            if not runtime_path.exists():
                raise ExternalToolError(
                    f"{display_arch} SDK has an invalid glibc layout; "
                    f"missing {relative}"
                )
        elf_reader = inspector or ReadElfInspector()
        for runtime_path in runtime_paths:
            machine = elf_reader.inspect(runtime_path).machine
            if machine != arch:
                raise ExternalToolError(
                    f"SDK runtime {runtime_path} has machine {machine!r}, "
                    f"expected {arch!r}"
                )
    for runtime_object in ("crti.o", "crtn.o"):
        if runtime_object not in names:
            raise ExternalToolError(
                f"SDK is missing glibc CRT object: {runtime_object}"
            )
    if not {"crt1.o", "Scrt1.o"}.intersection(names):
        raise ExternalToolError("SDK is missing a glibc startup CRT object")
    libc_linker_inputs = [
        path
        for path in entries
        if path.name == "libc.so" and (path.is_file() or path.is_symlink())
    ]
    if not libc_linker_inputs:
        raise ExternalToolError("SDK is missing the libc.so linker input")
    forbidden = [
        path
        for path in files
        if any(path.name.startswith(prefix) for prefix in _FORBIDDEN_SDK_NAMES)
    ]
    if forbidden:
        preview = ", ".join(str(path.relative_to(sysroot)) for path in forbidden[:5])
        raise ExternalToolError(f"compiler runtimes leaked into SDK: {preview}")
    if (sysroot / "usr" / "include" / "c++").exists():
        raise ExternalToolError("C++ standard library headers leaked into SDK")
    compiler_trees = [
        path
        for path in (
            sysroot / "usr" / "lib" / "gcc",
            sysroot / "lib" / "gcc",
            sysroot / "lib64" / "gcc",
        )
        if path.exists()
    ]
    if compiler_trees:
        raise ExternalToolError(
            f"compiler-owned GCC tree leaked into SDK: {compiler_trees[0]}"
        )


def export_sdk(spec: SdkSpec, workspace: Path) -> Path:
    source = workspace / "toolchain" / spec.target.triplet / "sysroot"
    if not source.is_dir():
        raise ExternalToolError(f"crosstool-NG sysroot was not produced: {source}")
    destination = workspace / "sdk"
    temporary = workspace / ".sdk.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    sysroot = temporary / "sysroot"
    shutil.copytree(source, sysroot, symlinks=True, ignore=_ignore_runtime)
    validate_sdk(sysroot, arch=spec.target.arch)

    workspace_manifest = workspace / "workspace.json"
    try:
        workspace_data = json.loads(workspace_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read builder provenance from {workspace_manifest}: {error}"
        ) from error
    workspace_data = _require_workspace_envelope(
        workspace_data,
        context="workspace manifest",
    )
    dockerfile_sha256 = workspace_data.get("builder_dockerfile_sha256")
    builder_base_image = workspace_data.get("builder_base_image")
    builder_platform = workspace_data.get("builder_platform")
    builder_apt_snapshot = workspace_data.get("builder_apt_snapshot")
    builder_image = workspace_data.get("builder_image")
    if (
        not isinstance(dockerfile_sha256, str)
        or not isinstance(builder_base_image, str)
        or not isinstance(builder_platform, str)
        or not isinstance(builder_apt_snapshot, str)
        or not isinstance(builder_image, dict)
    ):
        raise ConfigurationError(
            "workspace has no builder provenance; run `sdk build` before export"
        )
    image_os = builder_image.get("os")
    image_architecture = builder_image.get("architecture")
    image_platform = builder_image.get("platform")
    if (
        not isinstance(image_os, str)
        or not isinstance(image_architecture, str)
        or not isinstance(image_platform, str)
        or image_platform != f"{image_os}/{image_architecture}"
    ):
        raise ConfigurationError("workspace builder image platform is invalid")

    release = CROSSTOOL_NG_RELEASES[spec.builder.version]
    expected_builder_platform = _builder_platform(spec, release)
    if dockerfile_sha256 != _packaged_builder_dockerfile_sha256():
        raise ConfigurationError(
            "workspace builder Dockerfile base-image provenance cannot be "
            "verified; rebuild with the packaged Dockerfile"
        )
    if builder_base_image != release.builder_base_image:
        raise ConfigurationError(
            "workspace builder base image does not match the selected backend "
            f"release: expected {release.builder_base_image!r}, "
            f"recorded {builder_base_image!r}"
        )
    if builder_apt_snapshot != ubuntu_builder_snapshot():
        raise ConfigurationError(
            "workspace builder apt snapshot does not match the current "
            "package-source selection"
        )
    if (
        builder_platform != expected_builder_platform
        or image_platform != builder_platform
    ):
        raise ConfigurationError(
            "workspace builder platform does not match the selected backend "
            f"release: expected {expected_builder_platform!r}, recorded "
            f"{builder_platform!r} with image {image_platform!r}"
        )
    component_versions = _selected_component_versions(spec)
    archive_evidence = _download_archive_evidence(workspace)
    _verify_source_archives(spec, archive_evidence)
    for component, version in component_versions.items():
        extract_component_licenses(
            workspace / "downloads" / COMPONENT_ARCHIVES[(component, version)],
            temporary,
            component,
        )
    licenses = license_evidence(temporary, context="SDK")
    serialized_spec = spec.to_manifest_dict()
    sdk_manifest = {
        "schema": SDK_MANIFEST_SCHEMA,
        "format": SDK_MANIFEST_FORMAT,
        "compatibility_scope": "glibc-floor",
        "target": serialized_spec["target"],
        "builder": serialized_spec["builder"],
        "build_environment": {
            "dockerfile_sha256": dockerfile_sha256,
            "base_image": builder_base_image,
            "platform": builder_platform,
            "apt_snapshot": builder_apt_snapshot,
            "image": builder_image,
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
                for component, version in component_versions.items()
            },
            "download_archives": archive_evidence,
        },
        "licenses": licenses,
        "defconfig_sha256": file_sha256(
            workspace / "build" / "crosstool-ng" / "defconfig"
        ),
        "resolved_config_sha256": file_sha256(
            workspace / "build" / "crosstool-ng" / ".config"
        ),
        "excluded": [
            "compiler executables",
            "libstdc++",
            "libgcc_s",
            "compiler CRT (crtbegin/crtend)",
            "sanitizer and OpenMP runtimes",
        ],
    }
    (temporary / "manifest.json").write_text(
        json.dumps(sdk_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    def validate_published_sdk(published: Path) -> None:
        validate_sdk(published / "sysroot", arch=spec.target.arch)
        manifest_path = published / "manifest.json"
        try:
            published_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(
                f"cannot read published SDK manifest {manifest_path}: {error}"
            ) from error
        if published_manifest != sdk_manifest:
            raise ConfigurationError(
                "published SDK manifest does not match the selected SDK inputs"
            )

    replace_directory(temporary, destination, validate=validate_published_sdk)

    data = workspace_data
    data["state"] = "built"
    data["resolved_config_sha256"] = file_sha256(
        workspace / "build" / "crosstool-ng" / ".config"
    )
    workspace_manifest.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def load_workspace(workspace: Path) -> SdkSpec:
    manifest_path = workspace / "workspace.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"invalid workspace {workspace}: {error}") from error
    data = _require_workspace_envelope(
        data,
        context=f"workspace manifest {manifest_path}",
    )
    spec_data = data.get("spec")
    if not isinstance(spec_data, dict):
        raise ConfigurationError(f"workspace has no SDK spec: {manifest_path}")
    # workspace.json records the derived tuple for review.  Remove only that
    # derived field, then run the same strict schema parser as public specs.
    normalized = json.loads(json.dumps(spec_data))
    target = normalized.get("target")
    if not isinstance(target, dict):
        raise ConfigurationError(f"workspace SDK target is invalid: {manifest_path}")
    recorded_triplet = target.pop("triplet", None)
    spec = SdkSpec.from_dict(normalized)
    if recorded_triplet != spec.target.triplet:
        raise ConfigurationError(
            f"workspace target triplet is inconsistent: {recorded_triplet!r}"
        )
    return spec
