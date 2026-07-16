from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from linux_toolchain.build_tools import DEFAULT_CMAKE_VERSION, BuildToolsSpec
from linux_toolchain.container import linux_platform_for_architecture
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.integrations import ConanSettings, IntegrationName
from linux_toolchain.managed import (
    MANAGED_SPEC_FORMAT,
    MANAGED_SPEC_SCHEMA,
    ManagedSpec,
)
from linux_toolchain.managed.contracts import MANAGED_DEFAULT_HOST_GLIBC_FLOOR
from linux_toolchain.schema import canonical_json_sha256, read_json_object
from linux_toolchain.schema import object_value as _object
from linux_toolchain.schema import sha256_digest as _sha256
from linux_toolchain.schema import single_line_string as _string
from linux_toolchain.versions import AbiVersion

SETUP_CONFIG_SCHEMA = "linux-toolchain-setup"
SETUP_CONFIG_FORMAT = 1
PREPARED_SETUP_SCHEMA = "linux-toolchain-prepared-setup"
PREPARED_SETUP_FORMAT = 1

_COMPILER = re.compile(r"(gcc|clang)@([0-9]+(?:\.[0-9]+)*)")
_LIBSTDCXX = re.compile(r"gcc@([0-9]+(?:\.[0-9]+)*)")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
_INTEGRATIONS = ("cmake", "shell", "conan")
DEFAULT_CLANG_LIBSTDCXX = "gcc@12"


def _optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _string(value, context)


def _absolute_path(value: object, context: str) -> Path:
    result = Path(_string(value, context))
    if not result.is_absolute():
        raise ConfigurationError(f"{context} must be an absolute path")
    return result


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


@dataclass(frozen=True)
class SetupTarget:
    arch: str
    glibc_floor: str

    @classmethod
    def from_dict(cls, value: object) -> "SetupTarget":
        data = _object(
            value,
            required={"arch", "glibc_floor"},
            context="setup config.target",
        )
        result = cls(
            arch=_string(data["arch"], "setup config.target.arch"),
            glibc_floor=_string(data["glibc_floor"], "setup config.target.glibc_floor"),
        )
        if result.arch not in {"x86_64", "aarch64"}:
            raise ConfigurationError(
                "setup config.target.arch must be x86_64 or aarch64"
            )
        floor = AbiVersion.parse(result.glibc_floor)
        if result.arch == "aarch64" and floor < AbiVersion.parse("2.17"):
            raise ConfigurationError(
                "setup AArch64 targets require glibc 2.17 or newer"
            )
        return result

    def to_dict(self) -> dict[str, str]:
        return {"arch": self.arch, "glibc_floor": self.glibc_floor}


@dataclass(frozen=True)
class SetupConan:
    cppstd: str | None = None
    build_type: str = "Release"
    build_profile: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> "SetupConan":
        data = _object(
            value,
            required=set(),
            allowed={"cppstd", "build_type", "build_profile"},
            context="setup config.conan",
        )
        cppstd = _optional_string(data.get("cppstd"), "setup config.conan.cppstd")
        result = cls(
            cppstd=cppstd,
            build_type=_string(
                data.get("build_type", "Release"),
                "setup config.conan.build_type",
            ),
            build_profile=_optional_string(
                data.get("build_profile"),
                "setup config.conan.build_profile",
            ),
        )
        # Reuse the public adapter validation for cppstd and build type.
        ConanSettings(cppstd=result.cppstd, build_type=result.build_type)
        if (
            result.build_profile is not None
            and _IDENTIFIER.fullmatch(result.build_profile) is None
        ):
            raise ConfigurationError(
                "setup config.conan.build_profile must be a profile name"
            )
        return result

    def to_dict(self) -> dict[str, str]:
        result = {"build_type": self.build_type}
        if self.cppstd is not None:
            result["cppstd"] = self.cppstd
        if self.build_profile is not None:
            result["build_profile"] = self.build_profile
        return result


@dataclass(frozen=True)
class SetupConfig:
    compiler_family: str
    compiler_version: str
    target: SetupTarget
    smoke_integration: IntegrationName
    cmake_version: str = DEFAULT_CMAKE_VERSION
    libstdcxx: str | None = None
    host_glibc_floor: str = MANAGED_DEFAULT_HOST_GLIBC_FLOOR
    jobs: int = 1
    conan: SetupConan | None = None
    runner: str | None = None

    @classmethod
    def load(cls, path: Path | str) -> "SetupConfig":
        candidate = Path(path).expanduser()
        return cls.from_dict(read_json_object(candidate, "setup config"))

    @classmethod
    def from_dict(cls, value: object) -> "SetupConfig":
        data = _object(
            value,
            required={
                "schema",
                "format",
                "compiler",
                "target",
                "integration",
                "host_glibc_floor",
                "cmake_version",
            },
            allowed={
                "schema",
                "format",
                "compiler",
                "libstdcxx",
                "target",
                "integration",
                "host_glibc_floor",
                "cmake_version",
                "jobs",
                "conan",
                "runner",
            },
            context="setup config",
        )
        if data["schema"] != SETUP_CONFIG_SCHEMA:
            raise ConfigurationError(
                f"unsupported setup config schema: {data['schema']!r}"
            )
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != SETUP_CONFIG_FORMAT
        ):
            raise ConfigurationError(
                f"unsupported setup config format: {data['format']!r}"
            )
        compiler_text = _string(data["compiler"], "setup config.compiler")
        compiler_match = _COMPILER.fullmatch(compiler_text)
        if compiler_match is None:
            raise ConfigurationError(
                "setup config.compiler must use gcc@VERSION or clang@VERSION"
            )
        family, version = compiler_match.groups()
        AbiVersion.parse(version)
        integration = _string(data["integration"], "setup config.integration")
        if integration not in _INTEGRATIONS:
            raise ConfigurationError(
                "setup config.integration must be cmake, shell, or conan"
            )
        libstdcxx = _optional_string(data.get("libstdcxx"), "setup config.libstdcxx")
        if family == "gcc":
            if libstdcxx is not None:
                raise ConfigurationError(
                    "managed GCC uses its matching libstdc++; "
                    "setup config.libstdcxx must be omitted"
                )
        else:
            libstdcxx = libstdcxx or DEFAULT_CLANG_LIBSTDCXX
        if libstdcxx is not None and _LIBSTDCXX.fullmatch(libstdcxx) is None:
            raise ConfigurationError("managed Clang libstdcxx must use gcc@VERSION")

        jobs = data.get("jobs", 1)
        if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
            raise ConfigurationError("setup config.jobs must be a positive integer")
        host_floor = _string(
            data["host_glibc_floor"],
            "setup config.host_glibc_floor",
        )
        AbiVersion.parse(host_floor)
        runner = _optional_string(data.get("runner"), "setup config.runner")
        raw_conan = data.get("conan")
        if (
            integration != "conan"
            and isinstance(raw_conan, dict)
            and "build_profile" in raw_conan
        ):
            raise ConfigurationError(
                "setup config.conan.build_profile requires Conan verification"
            )
        conan = (
            SetupConan.from_dict({} if raw_conan is None else raw_conan)
            if integration == "conan" or raw_conan is not None
            else None
        )
        result = cls(
            compiler_family=family,
            compiler_version=version,
            target=SetupTarget.from_dict(data["target"]),
            smoke_integration=cast(IntegrationName, integration),
            cmake_version=_string(
                data["cmake_version"],
                "setup config.cmake_version",
            ),
            libstdcxx=libstdcxx,
            host_glibc_floor=host_floor,
            jobs=jobs,
            conan=conan,
            runner=runner,
        )
        result.build_tools_spec()
        return result

    @property
    def compiler(self) -> str:
        return f"{self.compiler_family}@{self.compiler_version}"

    @property
    def selected_integrations(self) -> tuple[IntegrationName, ...]:
        # High-level setup and its portable bundles carry every supported
        # adapter. The persisted ``integration`` field selects only the
        # producer verification path.
        return cast(tuple[IntegrationName, ...], _INTEGRATIONS)

    def managed_spec(self) -> ManagedSpec:
        if self.compiler_family == "gcc":
            runtimes = [{"kind": "libstdc++"}]
        else:
            assert self.libstdcxx is not None
            match = _LIBSTDCXX.fullmatch(self.libstdcxx)
            assert match is not None
            runtimes = [
                {"kind": "libstdc++", "gcc_version": match.group(1)},
                {"kind": "libc++"},
            ]
        return ManagedSpec.from_dict(
            {
                "schema": MANAGED_SPEC_SCHEMA,
                "format": MANAGED_SPEC_FORMAT,
                "name": "setup",
                "build_platform": linux_platform_for_architecture(self.target.arch),
                "host": {
                    "os": "linux",
                    "arch": self.target.arch,
                    "glibc_floor": self.host_glibc_floor,
                },
                "targets": [self.target.to_dict()],
                "compilers": [
                    {
                        "family": self.compiler_family,
                        "versions": [self.compiler_version],
                        "runtimes": runtimes,
                    }
                ],
            }
        )

    def build_tools_spec(self) -> BuildToolsSpec:
        result = BuildToolsSpec(
            arch=self.target.arch,
            glibc_floor=self.host_glibc_floor,
            cmake_version=self.cmake_version,
        )
        result.validate()
        return result

    def conan_settings(self) -> ConanSettings:
        if self.conan is None:
            return ConanSettings()
        return ConanSettings(
            cppstd=self.conan.cppstd,
            build_type=self.conan.build_type,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": SETUP_CONFIG_SCHEMA,
            "format": SETUP_CONFIG_FORMAT,
            "compiler": self.compiler,
            "target": self.target.to_dict(),
            "integration": self.smoke_integration,
            "host_glibc_floor": self.host_glibc_floor,
            "cmake_version": self.cmake_version,
        }
        if self.libstdcxx is not None:
            result["libstdcxx"] = self.libstdcxx
        if self.jobs != 1:
            result["jobs"] = self.jobs
        if self.conan is not None:
            result["conan"] = self.conan.to_dict()
        if self.runner is not None:
            result["runner"] = self.runner
        return result

    def selection_dict(self) -> dict[str, object]:
        """Return immutable setup choices without execution-only options."""

        result = self.to_dict()
        result.pop("jobs", None)
        return result

    @property
    def selection_sha256(self) -> str:
        return canonical_json_sha256(self.selection_dict())


@dataclass(frozen=True)
class ConanRunConfig:
    home: Path
    build_profile: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "home": str(self.home),
            "build_profile": str(self.build_profile),
        }


@dataclass(frozen=True)
class PreparedSetup:
    config_sha256: str
    binding: Path
    lock: Path
    variant: str
    sdk_workspace: Path
    build_tools_workspace: Path
    managed_workspace: Path
    compiler_kit: Path
    runtime: Path
    build_tools: Path
    integration: IntegrationName
    smoke_result: Path | None
    conan: ConanRunConfig | None

    @classmethod
    def load(cls, path: Path | str) -> "PreparedSetup":
        candidate = Path(path).expanduser()
        value = read_json_object(candidate, "prepared setup state")
        data = _object(
            value,
            required={
                "schema",
                "format",
                "config_sha256",
                "binding",
                "lock",
                "variant",
                "sdk_workspace",
                "build_tools_workspace",
                "managed_workspace",
                "compiler_kit",
                "runtime",
                "build_tools",
                "integration",
                "smoke_result",
                "conan",
            },
            context="prepared setup state",
        )
        if data["schema"] != PREPARED_SETUP_SCHEMA:
            raise ConfigurationError(
                f"unsupported prepared setup schema: {data['schema']!r}"
            )
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != PREPARED_SETUP_FORMAT
        ):
            raise ConfigurationError(
                f"unsupported prepared setup format: {data['format']!r}"
            )
        integration = _string(data["integration"], "prepared setup.integration")
        if integration not in _INTEGRATIONS:
            raise ConfigurationError(
                f"unsupported prepared setup integration: {integration!r}"
            )
        raw_conan = data["conan"]
        conan = None
        if raw_conan is not None:
            conan_data = _object(
                raw_conan,
                required={"home", "build_profile"},
                context="prepared setup.conan",
            )
            conan = ConanRunConfig(
                home=_absolute_path(conan_data["home"], "prepared setup.conan.home"),
                build_profile=_absolute_path(
                    conan_data["build_profile"],
                    "prepared setup.conan.build_profile",
                ),
            )
        raw_smoke = data["smoke_result"]
        smoke_result = (
            None
            if raw_smoke is None
            else _absolute_path(raw_smoke, "prepared setup.smoke_result")
        )
        variant = _string(data["variant"], "prepared setup.variant")
        if _IDENTIFIER.fullmatch(variant) is None:
            raise ConfigurationError("prepared setup.variant is invalid")
        return cls(
            config_sha256=_sha256(
                data["config_sha256"], "prepared setup.config_sha256"
            ),
            binding=_absolute_path(data["binding"], "prepared setup.binding"),
            lock=_absolute_path(data["lock"], "prepared setup.lock"),
            variant=variant,
            sdk_workspace=_absolute_path(
                data["sdk_workspace"], "prepared setup.sdk_workspace"
            ),
            build_tools_workspace=_absolute_path(
                data["build_tools_workspace"],
                "prepared setup.build_tools_workspace",
            ),
            managed_workspace=_absolute_path(
                data["managed_workspace"], "prepared setup.managed_workspace"
            ),
            compiler_kit=_absolute_path(
                data["compiler_kit"], "prepared setup.compiler_kit"
            ),
            runtime=_absolute_path(data["runtime"], "prepared setup.runtime"),
            build_tools=_absolute_path(
                data["build_tools"],
                "prepared setup.build_tools",
            ),
            integration=cast(IntegrationName, integration),
            smoke_result=smoke_result,
            conan=conan,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PREPARED_SETUP_SCHEMA,
            "format": PREPARED_SETUP_FORMAT,
            "config_sha256": self.config_sha256,
            "binding": str(self.binding),
            "lock": str(self.lock),
            "variant": self.variant,
            "sdk_workspace": str(self.sdk_workspace),
            "build_tools_workspace": str(self.build_tools_workspace),
            "managed_workspace": str(self.managed_workspace),
            "compiler_kit": str(self.compiler_kit),
            "runtime": str(self.runtime),
            "build_tools": str(self.build_tools),
            "integration": self.integration,
            "smoke_result": (
                str(self.smoke_result) if self.smoke_result is not None else None
            ),
            "conan": self.conan.to_dict() if self.conan is not None else None,
        }
