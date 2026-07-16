from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from linux_toolchain.container import (
    linux_architecture_for_platform,
)
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.contracts import MANAGED_MIN_AARCH64_GCC
from linux_toolchain.schema import non_empty_string as _string
from linux_toolchain.schema import object_value as _object
from linux_toolchain.versions import AbiVersion

MANAGED_SPEC_SCHEMA = "linux-toolchain-managed-spec"
MANAGED_SPEC_FORMAT = 1
SUPPORTED_BUILD_PLATFORMS = ("linux/amd64", "linux/arm64")
SUPPORTED_HOST_OS = "linux"
SUPPORTED_HOST_ARCHITECTURES = ("x86_64", "aarch64")
SUPPORTED_TARGET_ARCHITECTURES = ("x86_64", "aarch64")
SUPPORTED_RUNTIME_KINDS = ("libstdc++", "libc++")

_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{context} must be an array")
    if not value:
        raise ConfigurationError(f"{context} cannot be empty")
    return value


@dataclass(frozen=True)
class ManagedHostSpec:
    os: str
    arch: str
    glibc_floor: str

    @classmethod
    def from_dict(cls, value: object) -> "ManagedHostSpec":
        data = _object(
            value,
            required={"os", "arch", "glibc_floor"},
            context="managed spec.host",
        )
        result = cls(
            os=_string(data["os"], "managed spec.host.os"),
            arch=_string(data["arch"], "managed spec.host.arch"),
            glibc_floor=_string(data["glibc_floor"], "managed spec.host.glibc_floor"),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if (
            self.os != SUPPORTED_HOST_OS
            or self.arch not in SUPPORTED_HOST_ARCHITECTURES
        ):
            raise ConfigurationError(
                "managed compiler kits require host linux/x86_64 or linux/aarch64"
            )
        AbiVersion.parse(self.glibc_floor)

    def to_dict(self) -> dict[str, str]:
        return {
            "os": self.os,
            "arch": self.arch,
            "glibc_floor": self.glibc_floor,
        }


@dataclass(frozen=True)
class ManagedTargetSpec:
    arch: str
    glibc_floor: str

    @classmethod
    def from_dict(cls, value: object, index: int) -> "ManagedTargetSpec":
        context = f"managed spec.targets[{index}]"
        data = _object(
            value,
            required={"arch", "glibc_floor"},
            context=context,
        )
        result = cls(
            arch=_string(data["arch"], f"{context}.arch"),
            glibc_floor=_string(data["glibc_floor"], f"{context}.glibc_floor"),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if self.arch not in SUPPORTED_TARGET_ARCHITECTURES:
            raise ConfigurationError(
                "managed target architecture must be x86_64 or aarch64"
            )
        floor = AbiVersion.parse(self.glibc_floor)
        if self.arch == "aarch64" and floor < AbiVersion.parse("2.17"):
            raise ConfigurationError(
                "managed AArch64 targets require glibc 2.17 or newer"
            )

    def to_dict(self) -> dict[str, str]:
        return {"arch": self.arch, "glibc_floor": self.glibc_floor}


@dataclass(frozen=True)
class ManagedRuntimeSpec:
    kind: str
    gcc_version: str | None = None

    @classmethod
    def from_dict(
        cls, value: object, *, compiler_index: int, runtime_index: int
    ) -> "ManagedRuntimeSpec":
        context = f"managed spec.compilers[{compiler_index}].runtimes[{runtime_index}]"
        data = _object(
            value,
            required={"kind"},
            allowed={"kind", "gcc_version"},
            context=context,
        )
        kind = _string(data["kind"], f"{context}.kind")
        gcc_version = data.get("gcc_version")
        if gcc_version is not None:
            gcc_version = _string(gcc_version, f"{context}.gcc_version")
        result = cls(kind=kind, gcc_version=gcc_version)
        result.validate(context=context)
        return result

    def validate(self, *, context: str = "managed runtime") -> None:
        if self.kind not in SUPPORTED_RUNTIME_KINDS:
            raise ConfigurationError(f"{context}.kind must be libstdc++ or libc++")
        if self.kind == "libstdc++":
            if self.gcc_version is not None:
                AbiVersion.parse(self.gcc_version)
        elif self.gcc_version is not None:
            raise ConfigurationError(
                f"{context}.gcc_version is allowed only for libstdc++"
            )

    def to_dict(self) -> dict[str, str]:
        result = {"kind": self.kind}
        if self.gcc_version is not None:
            result["gcc_version"] = self.gcc_version
        return result


@dataclass(frozen=True)
class ManagedCompilerSpec:
    family: str
    versions: tuple[str, ...]
    runtimes: tuple[ManagedRuntimeSpec, ...]

    @classmethod
    def from_dict(cls, value: object, index: int) -> "ManagedCompilerSpec":
        context = f"managed spec.compilers[{index}]"
        data = _object(
            value,
            required={"family", "versions", "runtimes"},
            context=context,
        )
        family = _string(data["family"], f"{context}.family")
        versions = tuple(
            _string(item, f"{context}.versions[{item_index}]")
            for item_index, item in enumerate(
                _array(data["versions"], f"{context}.versions")
            )
        )
        runtimes = tuple(
            ManagedRuntimeSpec.from_dict(
                item, compiler_index=index, runtime_index=runtime_index
            )
            for runtime_index, item in enumerate(
                _array(data["runtimes"], f"{context}.runtimes")
            )
        )
        result = cls(
            family=family,
            versions=tuple(sorted(versions, key=AbiVersion.parse)),
            runtimes=tuple(
                sorted(
                    runtimes,
                    key=lambda runtime: (runtime.kind, runtime.gcc_version or ""),
                )
            ),
        )
        result.validate(context=context)
        return result

    def validate(self, *, context: str = "managed compiler") -> None:
        if self.family not in {"gcc", "clang"}:
            raise ConfigurationError(f"{context}.family must be gcc or clang")
        if not self.versions:
            raise ConfigurationError(f"{context}.versions cannot be empty")
        for index, version in enumerate(self.versions):
            if not isinstance(version, str):
                raise ConfigurationError(
                    f"{context}.versions[{index}] must be a string"
                )
            AbiVersion.parse(version)
        if len(self.versions) != len(set(self.versions)):
            raise ConfigurationError(f"{context}.versions cannot contain duplicates")
        if not self.runtimes:
            raise ConfigurationError(f"{context}.runtimes cannot be empty")
        runtime_keys = tuple(
            (runtime.kind, runtime.gcc_version) for runtime in self.runtimes
        )
        if len(runtime_keys) != len(set(runtime_keys)):
            raise ConfigurationError(f"{context}.runtimes cannot contain duplicates")
        for runtime in self.runtimes:
            runtime.validate(context=f"{context}.runtimes")

        if self.family == "gcc":
            if runtime_keys != (("libstdc++", None),):
                raise ConfigurationError(
                    "managed GCC requires exactly one matching libstdc++ runtime "
                    "without gcc_version"
                )
        else:
            for runtime in self.runtimes:
                if runtime.kind == "libstdc++" and runtime.gcc_version is None:
                    raise ConfigurationError(
                        "managed Clang libstdc++ requires an explicit gcc_version"
                    )

    def to_dict(self) -> dict[str, object]:
        versions = sorted(self.versions, key=AbiVersion.parse)
        runtimes = sorted(
            self.runtimes,
            key=lambda runtime: (runtime.kind, runtime.gcc_version or ""),
        )
        return {
            "family": self.family,
            "versions": versions,
            "runtimes": [runtime.to_dict() for runtime in runtimes],
        }


@dataclass(frozen=True)
class ManagedSpec:
    name: str
    build_platform: str
    host: ManagedHostSpec
    targets: tuple[ManagedTargetSpec, ...]
    compilers: tuple[ManagedCompilerSpec, ...]
    schema: str = MANAGED_SPEC_SCHEMA
    format: int = MANAGED_SPEC_FORMAT

    @classmethod
    def load(cls, path: Path | str) -> "ManagedSpec":
        candidate = Path(path).expanduser()
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(
                f"cannot read managed compiler spec {candidate}: {error}"
            ) from error
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: object) -> "ManagedSpec":
        data = _object(
            value,
            required={
                "schema",
                "format",
                "name",
                "build_platform",
                "host",
                "targets",
                "compilers",
            },
            context="managed spec",
        )
        if data["schema"] != MANAGED_SPEC_SCHEMA:
            raise ConfigurationError(
                f"unsupported managed spec schema: {data['schema']!r}"
            )
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != MANAGED_SPEC_FORMAT
        ):
            raise ConfigurationError(
                f"unsupported managed spec format: {data['format']!r}"
            )
        targets = tuple(
            ManagedTargetSpec.from_dict(item, index)
            for index, item in enumerate(
                _array(data["targets"], "managed spec.targets")
            )
        )
        compilers = tuple(
            ManagedCompilerSpec.from_dict(item, index)
            for index, item in enumerate(
                _array(data["compilers"], "managed spec.compilers")
            )
        )
        target_order = {"x86_64": 0, "aarch64": 1}
        family_order = {"gcc": 0, "clang": 1}
        result = cls(
            name=_string(data["name"], "managed spec.name"),
            build_platform=_string(
                data["build_platform"], "managed spec.build_platform"
            ),
            host=ManagedHostSpec.from_dict(data["host"]),
            targets=tuple(
                sorted(
                    targets,
                    key=lambda target: (
                        target_order.get(target.arch, 99),
                        AbiVersion.parse(target.glibc_floor),
                    ),
                )
            ),
            compilers=tuple(
                sorted(
                    compilers,
                    key=lambda compiler: family_order.get(compiler.family, 99),
                )
            ),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ConfigurationError(f"invalid managed spec name: {self.name!r}")
        if self.build_platform not in SUPPORTED_BUILD_PLATFORMS:
            raise ConfigurationError(
                "managed compiler builds require build_platform linux/amd64 "
                "or linux/arm64"
            )
        self.host.validate()
        if linux_architecture_for_platform(self.build_platform) != self.host.arch:
            raise ConfigurationError(
                "managed build_platform must match the Compiler Kit host architecture"
            )
        if not self.targets:
            raise ConfigurationError("managed spec.targets cannot be empty")
        target_keys = tuple(
            (target.arch, target.glibc_floor) for target in self.targets
        )
        if len(target_keys) != len(set(target_keys)):
            raise ConfigurationError("managed spec.targets cannot contain duplicates")
        for target in self.targets:
            target.validate()
            if target.arch != self.host.arch:
                raise ConfigurationError(
                    "managed production supports native targets only; target "
                    "architecture must match the Compiler Kit host"
                )
        if not self.compilers:
            raise ConfigurationError("managed spec.compilers cannot be empty")
        families = tuple(compiler.family for compiler in self.compilers)
        if len(families) != len(set(families)):
            raise ConfigurationError(
                "managed spec.compilers can contain at most one entry per family"
            )
        for compiler in self.compilers:
            compiler.validate()
            if self.host.arch == "aarch64" and compiler.family == "gcc":
                minimum = AbiVersion.parse(MANAGED_MIN_AARCH64_GCC)
                if any(
                    AbiVersion.parse(version) < minimum for version in compiler.versions
                ):
                    raise ConfigurationError(
                        "managed AArch64 targets require GCC "
                        f"{MANAGED_MIN_AARCH64_GCC} or newer"
                    )
            if self.host.arch == "aarch64":
                minimum = AbiVersion.parse(MANAGED_MIN_AARCH64_GCC)
                for runtime in compiler.runtimes:
                    if (
                        runtime.gcc_version is not None
                        and AbiVersion.parse(runtime.gcc_version) < minimum
                    ):
                        raise ConfigurationError(
                            "managed AArch64 targets require GCC runtime "
                            f"{MANAGED_MIN_AARCH64_GCC} or newer"
                        )

    def to_dict(self) -> dict[str, object]:
        target_order = {"x86_64": 0, "aarch64": 1}
        targets = sorted(
            self.targets,
            key=lambda target: (
                target_order[target.arch],
                AbiVersion.parse(target.glibc_floor),
            ),
        )
        family_order = {"gcc": 0, "clang": 1}
        compilers = sorted(
            self.compilers, key=lambda compiler: family_order[compiler.family]
        )
        return {
            "schema": self.schema,
            "format": self.format,
            "name": self.name,
            "build_platform": self.build_platform,
            "host": self.host.to_dict(),
            "targets": [target.to_dict() for target in targets],
            "compilers": [compiler.to_dict() for compiler in compilers],
        }
