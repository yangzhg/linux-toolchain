from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from linux_toolchain.build_tools_build_script import (
    render_build_tools_build_script,
)
from linux_toolchain.container import linux_platform_for_architecture
from linux_toolchain.elf.reader import ReadElfInspector
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.host_artifact import (
    audit_host_artifact,
    validate_artifact_symlinks,
)
from linux_toolchain.licenses import (
    require_license_files,
    validate_license_evidence,
)
from linux_toolchain.models import SUPPORTED_ARCHITECTURES, SdkSpec
from linux_toolchain.schema import object_value as _object
from linux_toolchain.schema import read_json_object
from linux_toolchain.schema import sha256_digest as _sha256_value
from linux_toolchain.schema import single_line_string as _string
from linux_toolchain.sdk.crosstool_ng import (
    sdk_producer_identity,
    validate_sdk_producer_identity,
)
from linux_toolchain.source_archive import PinnedArchive, pinned_gnu_archive
from linux_toolchain.versions import AbiVersion

BUILD_TOOLS_SCHEMA = "linux-toolchain-build-tools"
BUILD_TOOLS_FORMAT = 1
DEFAULT_CMAKE_VERSION = "3.31.12"
MAKE_VERSION = "4.4.1"
NINJA_VERSION = "1.13.2"
CCACHE_VERSION = "4.13.6"
OPENSSL_VERSION = "3.0.20"

_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_CMAKE_RELEASES = {
    "3.31.10": "cf06fadfd6d41fa8e1ade5099e54976d1d844fd1487ab99942341f91b13d3e29",
    "3.31.11": "c0a3b3f2912b2166f522d5010ffb6029d8454ee635f5ad7a3247e0be7f9a15c9",
    "3.31.12": "5f3fd5a54dfa65602bdbed64f981a72673cc19f2d304cc2955cf0dfa0cfd8272",
}
_CCACHE_SHA256 = {
    "x86_64": "156ec57c5198cc849d92834023d09910b83dc5504c6cf405d09e6ae7b208a3e5",
    "aarch64": "2098d561e4a8e36bd06a29aedce53ea90c7e365f9573a93d91c230efbf96a958",
}
_LICENSE_PATHS = (
    "cmake/Copyright.txt",
    "openssl/LICENSE.txt",
    "make/COPYING",
    "ninja/COPYING",
    "ccache/LICENSE.md",
    "ccache/GPL-3.0.txt",
)


@dataclass(frozen=True)
class BuildToolsSpec:
    arch: str
    glibc_floor: str
    cmake_version: str = DEFAULT_CMAKE_VERSION

    def validate(self) -> None:
        if self.arch not in SUPPORTED_ARCHITECTURES:
            raise ConfigurationError(
                "build tools architecture must be x86_64 or aarch64"
            )
        floor = AbiVersion.parse(self.glibc_floor)
        if self.arch == "aarch64" and floor < AbiVersion.parse("2.17"):
            raise ConfigurationError("AArch64 build tools require glibc 2.17 or newer")
        if self.cmake_version not in _CMAKE_RELEASES:
            supported = ", ".join(sorted(_CMAKE_RELEASES, key=AbiVersion.parse))
            raise ConfigurationError(
                f"unsupported CMake version {self.cmake_version!r}; "
                f"available versions: {supported}"
            )

    @classmethod
    def from_dict(cls, value: object) -> "BuildToolsSpec":
        data = _object(
            value,
            required={"arch", "glibc_floor", "cmake_version"},
            context="build tools selection",
        )
        result = cls(
            arch=_string(data["arch"], "build tools selection.arch"),
            glibc_floor=_string(
                data["glibc_floor"],
                "build tools selection.glibc_floor",
            ),
            cmake_version=_string(
                data["cmake_version"],
                "build tools selection.cmake_version",
            ),
        )
        result.validate()
        return result

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {
            "arch": self.arch,
            "glibc_floor": self.glibc_floor,
            "cmake_version": self.cmake_version,
        }


@dataclass(frozen=True)
class BuildToolsArtifact:
    root: Path
    spec: BuildToolsSpec
    identity: dict[str, object]
    tools: dict[str, dict[str, object]]


def _compiler_backend_identity(
    value: object,
    *,
    spec: BuildToolsSpec,
) -> tuple[dict[str, object], SdkSpec]:
    identity, backend = validate_sdk_producer_identity(
        value,
        context="build tools identity.compiler_backend",
    )
    if backend.target.arch != spec.arch or AbiVersion.parse(
        backend.target.libc_version
    ) != AbiVersion.parse(spec.glibc_floor):
        raise ConfigurationError(
            "build tools compiler backend does not match their host platform"
        )
    return identity, backend


def _validate_identity(
    value: object,
) -> tuple[dict[str, object], BuildToolsSpec]:
    data = _object(
        value,
        required={
            "kind",
            "format",
            "selection",
            "sources",
            "compiler_backend",
            "build_script_sha256",
        },
        context="build tools identity",
    )
    identity_format = data["format"]
    if (
        data["kind"] != "build-tools"
        or not isinstance(identity_format, int)
        or isinstance(identity_format, bool)
        or identity_format != BUILD_TOOLS_FORMAT
    ):
        raise ConfigurationError("build tools identity kind or format is invalid")
    spec = BuildToolsSpec.from_dict(data["selection"])
    if data["sources"] != build_tools_source_evidence(spec):
        raise ConfigurationError("build tools source identity is inconsistent")
    _, backend = _compiler_backend_identity(data["compiler_backend"], spec=spec)
    script_sha256 = _sha256_value(
        data["build_script_sha256"],
        "build tools identity.build_script_sha256",
    )
    expected_script = hashlib.sha256(
        build_tools_script(spec, backend.target.triplet).encode("utf-8")
    ).hexdigest()
    if script_sha256 != expected_script:
        raise ConfigurationError("build tools build-script identity is inconsistent")
    return dict(data), spec


def build_tools_sources(spec: BuildToolsSpec) -> dict[str, PinnedArchive]:
    spec.validate()
    cmake_filename = f"cmake-{spec.cmake_version}.tar.gz"
    ccache_filename = f"ccache-{CCACHE_VERSION}-linux-{spec.arch}-musl-static.tar.xz"
    return {
        "cmake": PinnedArchive(
            filename=cmake_filename,
            source_url=(f"https://cmake.org/files/v3.31/{cmake_filename}"),
            sha256=_CMAKE_RELEASES[spec.cmake_version],
        ),
        "openssl": PinnedArchive(
            filename=f"openssl-{OPENSSL_VERSION}.tar.gz",
            source_url=(
                "https://github.com/openssl/openssl/releases/download/"
                f"openssl-{OPENSSL_VERSION}/openssl-{OPENSSL_VERSION}.tar.gz"
            ),
            sha256="c80a01dfc70ece4dc21168932c37739042d404d46ccc81a5986dd75314ecda6f",
        ),
        "make": pinned_gnu_archive(
            filename=f"make-{MAKE_VERSION}.tar.gz",
            path=f"make/make-{MAKE_VERSION}.tar.gz",
            sha256="dd16fb1d67bfab79a72f5e8390735c49e3e8e70b4945a15ab1f81ddb78658fb3",
        ),
        "ninja": PinnedArchive(
            filename=f"ninja-{NINJA_VERSION}.tar.gz",
            source_url=(
                "https://github.com/ninja-build/ninja/archive/refs/tags/"
                f"v{NINJA_VERSION}.tar.gz"
            ),
            sha256="974d6b2f4eeefa25625d34da3cb36bdcebe7fbce40f4c16ac0835fd1c0cbae17",
        ),
        "ccache": PinnedArchive(
            filename=ccache_filename,
            source_url=(
                "https://github.com/ccache/ccache/releases/download/"
                f"v{CCACHE_VERSION}/{ccache_filename}"
            ),
            sha256=_CCACHE_SHA256[spec.arch],
        ),
    }


def build_tools_source_directories(spec: BuildToolsSpec) -> dict[str, str]:
    spec.validate()
    return {
        "cmake": f"cmake-{spec.cmake_version}",
        "openssl": f"openssl-{OPENSSL_VERSION}",
        "make": f"make-{MAKE_VERSION}",
        "ninja": f"ninja-{NINJA_VERSION}",
        "ccache": f"ccache-{CCACHE_VERSION}-linux-{spec.arch}-musl-static",
    }


def build_tools_source_evidence(
    spec: BuildToolsSpec,
) -> dict[str, dict[str, str]]:
    versions = {
        "cmake": spec.cmake_version,
        "openssl": OPENSSL_VERSION,
        "make": MAKE_VERSION,
        "ninja": NINJA_VERSION,
        "ccache": CCACHE_VERSION,
    }
    return {
        name: {
            "version": versions[name],
            "url": archive.source_url,
            "sha256": archive.sha256,
        }
        for name, archive in build_tools_sources(spec).items()
    }


def build_tools_script(spec: BuildToolsSpec, triplet: str) -> str:
    sources = build_tools_sources(spec)
    return render_build_tools_build_script(
        arch=spec.arch,
        triplet=triplet,
        cmake_version=spec.cmake_version,
        openssl_version=OPENSSL_VERSION,
        make_version=MAKE_VERSION,
        ninja_version=NINJA_VERSION,
        ccache_version=CCACHE_VERSION,
        archives={name: archive.filename for name, archive in sources.items()},
    )


def build_tools_producer_identity(
    spec: BuildToolsSpec,
    compiler_backend: SdkSpec,
) -> dict[str, object]:
    spec.validate()
    if compiler_backend.target.arch != spec.arch or AbiVersion.parse(
        compiler_backend.target.libc_version
    ) != AbiVersion.parse(spec.glibc_floor):
        raise ConfigurationError(
            "build tools compiler backend must match their architecture and "
            "host glibc floor"
        )
    script = build_tools_script(spec, compiler_backend.target.triplet)
    return {
        "kind": "build-tools",
        "format": BUILD_TOOLS_FORMAT,
        "selection": spec.to_dict(),
        "sources": build_tools_source_evidence(spec),
        "compiler_backend": sdk_producer_identity(compiler_backend),
        "build_script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
    }


def expected_build_tool_records(
    spec: BuildToolsSpec,
) -> dict[str, dict[str, object]]:
    return {
        name: {
            "version": (
                spec.cmake_version
                if name in {"cmake", "ctest", "cpack"}
                else {
                    "make": MAKE_VERSION,
                    "ninja": NINJA_VERSION,
                    "ccache": CCACHE_VERSION,
                }[name]
            ),
            "path": f"bin/{name}",
            "linkage": "static-musl" if name == "ccache" else "glibc-floor",
            "enabled_by_default": name != "ccache",
        }
        for name in ("cmake", "ctest", "cpack", "make", "ninja", "ccache")
    }


def _validate_image_provenance(
    value: object,
    *,
    spec: BuildToolsSpec,
) -> dict[str, object]:
    data = _object(
        value,
        required={"id", "os", "architecture", "repo_digests"},
        context="build tools builder_image",
    )
    image_id = _string(data["id"], "build tools builder_image.id")
    repo_digests = data["repo_digests"]
    expected_arch = linux_platform_for_architecture(spec.arch).split("/", 1)[1]
    if (
        _IMAGE_ID.fullmatch(image_id) is None
        or data["os"] != "linux"
        or data["architecture"] != expected_arch
        or not isinstance(repo_digests, list)
        or not all(isinstance(item, str) and item for item in repo_digests)
        or repo_digests != sorted(set(repo_digests))
    ):
        raise ConfigurationError("build tools builder-image provenance is invalid")
    return dict(data)


def _validate_elf_audit(value: object) -> dict[str, object]:
    data = _object(
        value,
        required={"audited_elf_files", "max_required_glibc"},
        context="build tools ELF audit",
    )
    count = data["audited_elf_files"]
    maximum = data["max_required_glibc"]
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or (maximum is not None and not isinstance(maximum, str))
    ):
        raise ConfigurationError("build tools ELF audit is invalid")
    if maximum is not None:
        AbiVersion.parse(maximum)
    return dict(data)


def _build_tools_root(path: Path | str) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"build tools artifact cannot be a symlink: {raw}")
    try:
        root = raw.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot access build tools artifact {raw}: {error}"
        ) from error
    if not root.is_dir():
        raise ConfigurationError(f"build tools artifact is not a directory: {root}")
    validate_artifact_symlinks(root, context="build tools artifact")
    return root


def _validate_build_tools_inventory(
    root: Path,
    value: object,
    *,
    spec: BuildToolsSpec,
) -> dict[str, dict[str, object]]:
    expected = expected_build_tool_records(spec)
    if value != expected:
        raise ConfigurationError("build tools executable inventory is inconsistent")
    for name, record in expected.items():
        executable = root / str(record["path"])
        if (
            executable.is_symlink()
            or not executable.is_file()
            or executable.stat().st_mode & 0o111 == 0
        ):
            raise ConfigurationError(
                f"build tools executable is missing or invalid: {name}"
            )
    return expected


def _validate_build_tools_payload(
    root: Path,
    *,
    spec: BuildToolsSpec,
    recorded_audit: object,
    licenses: object,
) -> None:
    validate_license_evidence(root, licenses, context="build tools")
    require_license_files(root, _LICENSE_PATHS, context="build tools")
    expected_audit = _validate_elf_audit(recorded_audit)
    actual_audit = audit_host_artifact(
        root,
        arch=spec.arch,
        glibc_floor=spec.glibc_floor,
        context="build tools",
    )
    if actual_audit != expected_audit:
        raise ConfigurationError("build tools ELF audit evidence changed")
    ccache = ReadElfInspector().inspect(root / "bin" / "ccache")
    if ccache.interpreter is not None or ccache.needed:
        raise ExternalToolError(
            "build tools ccache must be a fully static musl executable"
        )


def load_build_tools(
    path: Path | str,
    *,
    expected_identity: Mapping[str, object] | None = None,
) -> BuildToolsArtifact:
    root = _build_tools_root(path)
    manifest_path = root / "manifest.json"
    manifest = _object(
        read_json_object(manifest_path, "build tools manifest"),
        required={
            "schema",
            "format",
            "identity",
            "tools",
            "builder_image",
            "elf_audit",
            "licenses",
        },
        context="build tools manifest",
    )
    if manifest["schema"] != BUILD_TOOLS_SCHEMA:
        raise ConfigurationError("build tools manifest schema is unsupported")
    if (
        not isinstance(manifest["format"], int)
        or isinstance(manifest["format"], bool)
        or manifest["format"] != BUILD_TOOLS_FORMAT
    ):
        raise ConfigurationError("build tools manifest format is unsupported")
    identity, spec = _validate_identity(manifest["identity"])
    if expected_identity is not None and identity != dict(expected_identity):
        raise ConfigurationError("build tools artifact identity does not match")

    expected_tools = _validate_build_tools_inventory(
        root,
        manifest["tools"],
        spec=spec,
    )
    _validate_image_provenance(manifest["builder_image"], spec=spec)
    _validate_build_tools_payload(
        root,
        spec=spec,
        recorded_audit=manifest["elf_audit"],
        licenses=manifest["licenses"],
    )
    return BuildToolsArtifact(
        root=root,
        spec=spec,
        identity=dict(identity),
        tools={name: dict(record) for name, record in expected_tools.items()},
    )
