from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.versions import AbiVersion

SDK_SPEC_SCHEMA = "linux-toolchain-sdk-spec"
SDK_SPEC_FORMAT = 1
SDK_WORKSPACE_SCHEMA = "linux-toolchain-sdk-workspace"
SDK_WORKSPACE_FORMAT = 1
SDK_MANIFEST_SCHEMA = "linux-toolchain-sdk"
SDK_MANIFEST_FORMAT = 1
SUPPORTED_ARCHITECTURES = ("x86_64", "aarch64")
ARCHITECTURE_MINIMUM_KERNELS = {
    "x86_64": "3.2.0",
    "aarch64": "3.7.0",
}
_TARGET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")

TargetClassificationPolicy = Literal["external", "strict"]


def classify_linux_glibc_target(
    value: object,
    *,
    policy: TargetClassificationPolicy,
    expected_architecture: str | None = None,
    context: str,
) -> str:
    """Classify a supported Linux/glibc target under an explicit policy."""

    if policy not in {"external", "strict"}:
        raise ConfigurationError(f"unknown target classification policy: {policy!r}")
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{context} must be a non-empty string")
    candidate = value.strip()
    if not candidate or not _TARGET_TOKEN_RE.fullmatch(candidate):
        raise ConfigurationError(f"{context} has invalid characters")
    if policy == "strict" and candidate != value:
        raise ConfigurationError(f"{context} has invalid characters")

    normalized = candidate.lower()
    components = normalized.split("-")
    forbidden = ("android", "darwin", "mingw", "musl", "uclibc", "freebsd")
    if "gnux32" in normalized or "ilp32" in normalized:
        raise ConfigurationError(f"{context} uses an unsupported ILP32 or x86 x32 ABI")
    if "linux" not in components or any(token in normalized for token in forbidden):
        raise ConfigurationError(
            f"{context} {value!r} is incompatible with a supported Linux glibc target"
        )

    if normalized.startswith("x86_64-"):
        architecture = "x86_64"
    elif normalized.startswith("aarch64-"):
        architecture = "aarch64"
    else:
        raise ConfigurationError(
            f"{context} {value!r} is not supported; expected x86_64 or aarch64"
        )
    if policy == "strict" and not normalized.endswith("-linux-gnu"):
        raise ConfigurationError(
            f"{context} {value!r} is incompatible with {architecture} Linux glibc"
        )
    if expected_architecture is not None and architecture != expected_architecture:
        raise ConfigurationError(
            f"{context} {value!r} is incompatible with "
            f"{expected_architecture} Linux glibc"
        )
    return architecture


def _require_keys(value: dict[str, Any], required: set[str], context: str) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise ConfigurationError(f"{context} is missing: {', '.join(missing)}")


def _reject_unknown_keys(
    value: dict[str, Any], allowed: set[str], context: str
) -> None:
    unknown = sorted(value.keys() - allowed)
    if unknown:
        raise ConfigurationError(f"{context} has unknown keys: {', '.join(unknown)}")


@dataclass(frozen=True)
class TargetSpec:
    arch: str
    vendor: str
    libc: str
    libc_version: str
    linux_headers: str
    minimum_kernel: str
    cpu: str

    @property
    def triplet(self) -> str:
        if self.arch == "x86_64":
            return f"x86_64-{self.vendor}-linux-gnu"
        if self.arch == "aarch64":
            return f"aarch64-{self.vendor}-linux-gnu"
        raise ConfigurationError(f"unsupported target architecture: {self.arch}")

    def validate(self) -> None:
        for field_name, value in (
            ("arch", self.arch),
            ("vendor", self.vendor),
            ("libc", self.libc),
            ("libc_version", self.libc_version),
            ("linux_headers", self.linux_headers),
            ("minimum_kernel", self.minimum_kernel),
            ("cpu", self.cpu),
        ):
            if not isinstance(value, str):
                raise ConfigurationError(f"target.{field_name} must be a string")
        if self.arch not in SUPPORTED_ARCHITECTURES:
            raise ConfigurationError(
                "supported target architectures: " + ", ".join(SUPPORTED_ARCHITECTURES)
            )
        if self.libc != "glibc":
            raise ConfigurationError("only glibc is supported")
        AbiVersion.parse(self.libc_version)
        AbiVersion.parse(self.linux_headers)
        AbiVersion.parse(self.minimum_kernel)
        if not _TARGET_TOKEN_RE.fullmatch(self.vendor):
            raise ConfigurationError(f"invalid target vendor: {self.vendor!r}")
        if not _TARGET_TOKEN_RE.fullmatch(self.cpu):
            raise ConfigurationError(f"invalid target CPU: {self.cpu!r}")
        classify_linux_glibc_target(
            self.triplet,
            policy="strict",
            expected_architecture=self.arch,
            context="SDK target triplet",
        )
        expected_cpu = {"x86_64": "x86-64", "aarch64": "armv8-a"}.get(self.arch)
        if expected_cpu is not None and self.cpu != expected_cpu:
            raise ConfigurationError(
                f"target CPU for {self.arch} must be {expected_cpu!r}"
            )
        minimum_kernel = ARCHITECTURE_MINIMUM_KERNELS[self.arch]
        if AbiVersion.parse(self.minimum_kernel) < AbiVersion.parse(minimum_kernel):
            raise ConfigurationError(
                f"{self.arch} Linux requires minimum_kernel {minimum_kernel} or newer"
            )


@dataclass(frozen=True)
class BuilderSpec:
    backend: str
    version: str
    gcc: str
    binutils: str

    def validate(self) -> None:
        for field_name, value in (
            ("backend", self.backend),
            ("version", self.version),
            ("gcc", self.gcc),
            ("binutils", self.binutils),
        ):
            if not isinstance(value, str):
                raise ConfigurationError(f"builder.{field_name} must be a string")
        if self.backend != "crosstool-ng":
            raise ConfigurationError("only the crosstool-ng backend is supported")


@dataclass(frozen=True)
class SdkSpec:
    name: str
    target: TargetSpec
    builder: BuilderSpec

    @classmethod
    def load(cls, path: Path) -> "SdkSpec":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"cannot read SDK spec {path}: {error}") from error
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: object) -> "SdkSpec":
        """Parse the strict public SDK schema from an already decoded object."""

        if not isinstance(data, dict):
            raise ConfigurationError("SDK spec root must be an object")
        envelope = {"schema", "format", "name", "target", "builder"}
        _require_keys(data, envelope, "SDK spec")
        _reject_unknown_keys(
            data,
            envelope,
            "SDK spec",
        )
        if data["schema"] != SDK_SPEC_SCHEMA:
            raise ConfigurationError(f"unsupported SDK spec schema: {data['schema']!r}")
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != SDK_SPEC_FORMAT
        ):
            raise ConfigurationError(f"unsupported SDK spec format: {data['format']!r}")

        target_data = data["target"]
        builder_data = data["builder"]
        if not isinstance(target_data, dict) or not isinstance(builder_data, dict):
            raise ConfigurationError("target and builder must be objects")

        target_keys = {
            "arch",
            "vendor",
            "libc",
            "libc_version",
            "linux_headers",
            "minimum_kernel",
            "cpu",
        }
        builder_keys = {"backend", "version", "gcc", "binutils"}
        _require_keys(target_data, target_keys, "target")
        _reject_unknown_keys(target_data, target_keys, "target")
        _require_keys(builder_data, builder_keys, "builder")
        _reject_unknown_keys(builder_data, builder_keys, "builder")

        if not isinstance(data["name"], str):
            raise ConfigurationError("SDK spec name must be a string")
        target = TargetSpec(**target_data)
        builder = BuilderSpec(**builder_data)
        spec = cls(name=data["name"], target=target, builder=builder)
        spec.validate()
        return spec

    def validate(self) -> None:
        if not isinstance(self.name, str):
            raise ConfigurationError("SDK spec name must be a string")
        if not self.name or any(char in self.name for char in "/\\"):
            raise ConfigurationError(f"invalid SDK name: {self.name!r}")
        self.target.validate()
        self.builder.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SDK_SPEC_SCHEMA,
            "format": SDK_SPEC_FORMAT,
            "name": self.name,
            "target": {
                "arch": self.target.arch,
                "vendor": self.target.vendor,
                "libc": self.target.libc,
                "libc_version": self.target.libc_version,
                "linux_headers": self.target.linux_headers,
                "minimum_kernel": self.target.minimum_kernel,
                "cpu": self.target.cpu,
            },
            "builder": {
                "backend": self.builder.backend,
                "version": self.builder.version,
                "gcc": self.builder.gcc,
                "binutils": self.builder.binutils,
            },
        }

    def to_manifest_dict(self) -> dict[str, Any]:
        """Serialize the public spec plus fields derived for build evidence."""

        result = self.to_dict()
        target = cast(dict[str, Any], result["target"])
        target["triplet"] = self.target.triplet
        return result
