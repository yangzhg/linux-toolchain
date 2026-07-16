from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.models import classify_linux_glibc_target
from linux_toolchain.schema import (
    non_empty_string as _string,
)
from linux_toolchain.schema import (
    object_value as _object,
)
from linux_toolchain.schema import (
    positive_integer as _positive_integer,
)
from linux_toolchain.schema import (
    relative_posix_path,
)
from linux_toolchain.versions import AbiVersion

COMPILER_KIT_MANIFEST_SCHEMA = "linux-toolchain-compiler-kit"
COMPILER_KIT_MANIFEST_FORMAT = 1
COMPILER_KIT_PAYLOAD_DIRECTORY = "compiler"

TARGET_TOOL_NAMES = (
    "ar",
    "as",
    "ld",
    "nm",
    "objcopy",
    "objdump",
    "ranlib",
    "strip",
)

_SUPPORTED_HOST_ARCHITECTURES = {"x86_64", "aarch64"}
_SUPPORTED_TARGET_ARCHITECTURES = {"x86_64", "aarch64"}
_SUPPORTED_PROVIDER_MINIMUMS = {"gcc": 10, "clang": 16}
_HOST_ARCHITECTURE_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}


def _relative_payload_path(value: object, context: str) -> str:
    text = relative_posix_path(value, context)
    path = PurePosixPath(text)
    if not path.parts or path.parts[0] != COMPILER_KIT_PAYLOAD_DIRECTORY:
        raise ConfigurationError(
            f"{context} must be below {COMPILER_KIT_PAYLOAD_DIRECTORY}/"
        )
    if len(path.parts) == 1:
        raise ConfigurationError(f"{context} must identify a payload entry")
    return text


def _provider(value: object) -> Mapping[str, object]:
    context = "compiler kit manifest.provider"
    data = _object(
        value,
        required={"name", "version", "major"},
        context=context,
    )
    name = _string(data["name"], f"{context}.name")
    if name not in _SUPPORTED_PROVIDER_MINIMUMS:
        raise ConfigurationError(f"{context}.name must be gcc or clang, got {name!r}")
    version = _string(data["version"], f"{context}.version")
    parsed_version = AbiVersion.parse(version)
    major = _positive_integer(data["major"], f"{context}.major")
    if parsed_version.parts[0] != major:
        raise ConfigurationError(
            f"{context}.major is inconsistent with version {version}"
        )
    minimum = _SUPPORTED_PROVIDER_MINIMUMS[name]
    if major < minimum:
        raise ConfigurationError(
            f"unsupported managed {name} major {major}; expected {minimum} or newer"
        )
    return MappingProxyType({"name": name, "version": version, "major": major})


def _host(value: object) -> Mapping[str, str]:
    context = "compiler kit manifest.host"
    data = _object(
        value,
        required={"os", "arch", "glibc_floor"},
        context=context,
    )
    operating_system = _string(data["os"], f"{context}.os")
    if operating_system != "linux":
        raise ConfigurationError(f"{context}.os must be linux")
    arch = _string(data["arch"], f"{context}.arch")
    if arch not in _SUPPORTED_HOST_ARCHITECTURES:
        raise ConfigurationError(
            f"{context}.arch must be x86_64 or aarch64, got {arch!r}"
        )
    glibc_floor = _string(data["glibc_floor"], f"{context}.glibc_floor")
    AbiVersion.parse(glibc_floor)
    return MappingProxyType(
        {"os": operating_system, "arch": arch, "glibc_floor": glibc_floor}
    )


def _target(value: object) -> Mapping[str, str]:
    context = "compiler kit manifest.target"
    data = _object(
        value,
        required={"arch", "triplet"},
        context=context,
    )
    arch = _string(data["arch"], f"{context}.arch")
    if arch not in _SUPPORTED_TARGET_ARCHITECTURES:
        raise ConfigurationError(
            f"{context}.arch must be x86_64 or aarch64, got {arch!r}"
        )
    triplet = _string(data["triplet"], f"{context}.triplet")
    classify_linux_glibc_target(
        triplet,
        policy="strict",
        expected_architecture=arch,
        context=f"{context}.triplet",
    )
    return MappingProxyType({"arch": arch, "triplet": triplet})


def _locations(value: object) -> Mapping[str, object]:
    context = "compiler kit manifest.locations"
    data = _object(
        value,
        required={"cc", "cxx", "target_tools"},
        context=context,
    )
    tool_data = _object(
        data["target_tools"],
        required=set(TARGET_TOOL_NAMES),
        context=f"{context}.target_tools",
    )
    target_tools = MappingProxyType(
        {
            name: _relative_payload_path(
                tool_data[name], f"{context}.target_tools.{name}"
            )
            for name in TARGET_TOOL_NAMES
        }
    )
    return MappingProxyType(
        {
            "cc": _relative_payload_path(data["cc"], f"{context}.cc"),
            "cxx": _relative_payload_path(data["cxx"], f"{context}.cxx"),
            "target_tools": target_tools,
        }
    )


@dataclass(frozen=True)
class CompilerKitManifest:
    provider: Mapping[str, object]
    host: Mapping[str, str]
    target: Mapping[str, str]
    locations: Mapping[str, object]
    schema: str = COMPILER_KIT_MANIFEST_SCHEMA
    format: int = COMPILER_KIT_MANIFEST_FORMAT

    @classmethod
    def from_dict(cls, value: object) -> "CompilerKitManifest":
        data = _object(
            value,
            required={
                "schema",
                "format",
                "provider",
                "host",
                "target",
                "locations",
            },
            context="compiler kit manifest",
        )
        if data["schema"] != COMPILER_KIT_MANIFEST_SCHEMA:
            raise ConfigurationError(
                f"unsupported compiler kit manifest schema: {data['schema']!r}"
            )
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != COMPILER_KIT_MANIFEST_FORMAT
        ):
            raise ConfigurationError(
                f"unsupported compiler kit manifest format: {data['format']!r}"
            )
        return cls(
            provider=_provider(data["provider"]),
            host=_host(data["host"]),
            target=_target(data["target"]),
            locations=_locations(data["locations"]),
        )

    def to_dict(self) -> dict[str, object]:
        tools = self.locations["target_tools"]
        assert isinstance(tools, Mapping)
        return {
            "schema": self.schema,
            "format": self.format,
            "provider": dict(self.provider),
            "host": dict(self.host),
            "target": dict(self.target),
            "locations": {
                "cc": self.locations["cc"],
                "cxx": self.locations["cxx"],
                "target_tools": {name: tools[name] for name in TARGET_TOOL_NAMES},
            },
        }


@dataclass(frozen=True)
class CompilerKitExecutable:
    relative_path: str
    invocation_path: Path


@dataclass(frozen=True)
class CompilerKit:
    root: Path
    manifest_path: Path
    manifest: CompilerKitManifest
    cc: CompilerKitExecutable
    cxx: CompilerKitExecutable
    target_tools: Mapping[str, CompilerKitExecutable]


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _validate_payload_tree(compiler_root: Path) -> None:
    if not compiler_root.is_dir() or compiler_root.is_symlink():
        raise ConfigurationError(
            f"compiler kit payload is not a directory: {compiler_root}"
        )


def _load_executable(
    root: Path,
    compiler_root: Path,
    relative_path: object,
    context: str,
) -> CompilerKitExecutable:
    relative = _relative_payload_path(relative_path, context)
    invocation_path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved_path = invocation_path.resolve(strict=True)
        resolved_root = compiler_root.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"{context} does not exist in the compiler kit: {relative}"
        ) from error
    if not _is_within(resolved_path, resolved_root):
        raise ConfigurationError(f"{context} escapes compiler/: {relative}")
    if not resolved_path.is_file() or not os.access(resolved_path, os.X_OK):
        raise ConfigurationError(
            f"{context} is not an executable regular file: {relative}"
        )
    return CompilerKitExecutable(
        relative_path=relative,
        invocation_path=invocation_path,
    )


def _current_host() -> tuple[str, str, str]:
    operating_system = platform.system().lower()
    machine = platform.machine().lower()
    arch = _HOST_ARCHITECTURE_ALIASES.get(machine, machine)
    try:
        libc_text = os.confstr("CS_GNU_LIBC_VERSION")
    except (OSError, ValueError):
        libc_text = None
    if not libc_text or not libc_text.startswith("glibc "):
        raise ConfigurationError(
            "managed compiler kits require a Linux glibc build host"
        )
    glibc_version = libc_text.removeprefix("glibc ").strip()
    AbiVersion.parse(glibc_version)
    return operating_system, arch, glibc_version


def validate_current_host(host: Mapping[str, str]) -> None:
    """Validate the current machine against a Compiler Kit host selection."""

    operating_system, arch, glibc_version = _current_host()
    if operating_system != host["os"] or arch != host["arch"]:
        raise ConfigurationError(
            "compiler kit build host mismatch: kit requires "
            f"{host['os']}/{host['arch']}, current host is "
            f"{operating_system}/{arch}"
        )
    if AbiVersion.parse(glibc_version) < AbiVersion.parse(host["glibc_floor"]):
        raise ConfigurationError(
            "current host glibc is too old for the selected Compiler Kit: "
            "kit requires "
            f"{host['glibc_floor']}, current host provides {glibc_version}"
        )


def _manifest_path(path: Path | str) -> tuple[Path, Path]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ConfigurationError(f"compiler kit path cannot be a symlink: {candidate}")
    if candidate.is_dir():
        root = candidate.resolve()
        manifest_path = root / "manifest.json"
    else:
        if candidate.name != "manifest.json":
            raise ConfigurationError(
                "compiler kit manifest file must be named manifest.json"
            )
        if candidate.is_symlink():
            raise ConfigurationError(
                f"compiler kit manifest cannot be a symlink: {candidate}"
            )
        manifest_path = candidate.resolve()
        root = manifest_path.parent
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"compiler kit root is not a directory: {root}")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ConfigurationError(
            f"compiler kit manifest does not exist: {manifest_path}"
        )
    return root, manifest_path


def load_compiler_kit(
    path: Path | str,
    *,
    check_host: bool = True,
) -> CompilerKit:
    root, manifest_path = _manifest_path(path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read compiler kit manifest {manifest_path}: {error}"
        ) from error
    manifest = CompilerKitManifest.from_dict(data)
    if check_host:
        validate_current_host(manifest.host)

    compiler_root = root / COMPILER_KIT_PAYLOAD_DIRECTORY
    _validate_payload_tree(compiler_root)
    cc = _load_executable(
        root,
        compiler_root,
        manifest.locations["cc"],
        "compiler kit C driver",
    )
    cxx = _load_executable(
        root,
        compiler_root,
        manifest.locations["cxx"],
        "compiler kit C++ driver",
    )
    tool_locations = manifest.locations["target_tools"]
    assert isinstance(tool_locations, Mapping)
    target_tools = MappingProxyType(
        {
            name: _load_executable(
                root,
                compiler_root,
                tool_locations[name],
                f"compiler kit target tool {name}",
            )
            for name in TARGET_TOOL_NAMES
        }
    )
    return CompilerKit(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        cc=cc,
        cxx=cxx,
        target_tools=target_tools,
    )
