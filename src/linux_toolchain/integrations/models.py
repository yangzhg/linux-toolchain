# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias

from linux_toolchain.errors import ConfigurationError

Architecture: TypeAlias = Literal["x86_64", "aarch64"]
IntegrationName: TypeAlias = Literal["cmake", "shell", "conan"]
ConanLibcxx: TypeAlias = Literal["libstdc++", "libstdc++11", "libc++"]
ConanBuildType: TypeAlias = Literal["Debug", "Release", "RelWithDebInfo", "MinSizeRel"]
RenderedPaths: TypeAlias = dict[str, PurePosixPath]

DEFAULT_INTEGRATIONS: tuple[IntegrationName, ...] = ("cmake", "shell")
SUPPORTED_INTEGRATIONS: tuple[IntegrationName, ...] = ("cmake", "shell", "conan")

REQUIRED_TARGET_TOOLS = frozenset(
    {"ar", "ranlib", "as", "nm", "strip", "objcopy", "objdump"}
)

_SUPPORTED_ARCHITECTURES = frozenset({"x86_64", "aarch64"})
_SUPPORTED_CPP_STANDARDS = frozenset(
    {
        "98",
        "gnu98",
        "11",
        "gnu11",
        "14",
        "gnu14",
        "17",
        "gnu17",
        "20",
        "gnu20",
        "23",
        "gnu23",
    }
)
_SUPPORTED_LIBCXX = frozenset({"libstdc++", "libstdc++11", "libc++"})
_SUPPORTED_BUILD_TYPES = frozenset({"Debug", "Release", "RelWithDebInfo", "MinSizeRel"})
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")


def _conan_default_cppstd(
    compiler_family: Literal["gcc", "clang"], compiler_version: int
) -> str:
    """Return the compiler default modeled by Conan 2 profile detection."""

    if compiler_family == "gcc":
        if compiler_version >= 16:
            return "gnu20"
        if compiler_version >= 11:
            return "gnu17"
    elif compiler_version >= 16:
        return "gnu17"
    if compiler_version >= 6:
        return "gnu14"
    return "gnu98"


def _text(value: str, *, field: str) -> str:
    if not value or "\n" in value or "\r" in value:
        raise ConfigurationError(f"{field} must be a non-empty single-line value")
    return value


def _version(value: str, *, field: str) -> str:
    value = _text(value, field=field)
    if not _VERSION.fullmatch(value):
        raise ConfigurationError(f"{field} is not a numeric dotted version: {value!r}")
    return value


def _absolute_path(value: Path, *, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{field} must be an absolute path: {path}")
    return path


@dataclass(frozen=True)
class IntegrationContext:
    """Build-system-neutral inputs used to render consumer entry points.

    ``binding_root`` is the final published root whose paths are embedded in
    generated files. Renderers may write those files into a separate staging
    directory.
    """

    binding_root: Path
    target: str
    architecture: Architecture
    sysroot: Path
    cc: Path
    cxx: Path
    tools: Mapping[str, Path]
    linker: Path | None

    def __post_init__(self) -> None:
        if self.architecture not in _SUPPORTED_ARCHITECTURES:
            raise ConfigurationError(
                f"unsupported integration architecture: {self.architecture!r}"
            )
        object.__setattr__(self, "target", _text(self.target, field="target"))
        for name in ("binding_root", "sysroot", "cc", "cxx"):
            object.__setattr__(
                self,
                name,
                _absolute_path(getattr(self, name), field=name.replace("_", " ")),
            )
        if self.linker is not None:
            object.__setattr__(
                self,
                "linker",
                _absolute_path(self.linker, field="linker"),
            )

        normalized_tools: dict[str, Path] = {}
        for name, path in self.tools.items():
            if not isinstance(name, str) or not name or not name.isascii():
                raise ConfigurationError(f"invalid target tool name: {name!r}")
            normalized_tools[name] = _absolute_path(
                Path(path), field=f"target tool {name}"
            )
        missing = sorted(REQUIRED_TARGET_TOOLS.difference(normalized_tools))
        if missing:
            raise ConfigurationError(
                "integration context is missing target tools: " + ", ".join(missing)
            )
        object.__setattr__(self, "tools", MappingProxyType(normalized_tools))


@dataclass(frozen=True)
class ShellIntegrationConfig:
    """Shell-adapter settings that are not part of the shared compiler model."""

    pkg_config_dirs: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(
            _absolute_path(path, field="pkg-config directory")
            for path in self.pkg_config_dirs
        )
        if len(set(normalized)) != len(normalized):
            raise ConfigurationError(
                "shell integration contains duplicate pkg-config directories"
            )
        object.__setattr__(self, "pkg_config_dirs", normalized)


@dataclass(frozen=True)
class ConanSettings:
    """Consumer choices that are meaningful only to a Conan host profile.

    ``libcxx`` may be omitted at the binding API boundary. The binding must
    infer it from the validated runtime before constructing a complete Conan
    renderer configuration.
    """

    cppstd: str | None = None
    libcxx: ConanLibcxx | None = None
    build_type: ConanBuildType = "Release"

    def __post_init__(self) -> None:
        if self.cppstd is not None and self.cppstd not in _SUPPORTED_CPP_STANDARDS:
            raise ConfigurationError(f"unsupported Conan C++ standard: {self.cppstd!r}")
        if self.libcxx is not None and self.libcxx not in _SUPPORTED_LIBCXX:
            raise ConfigurationError(f"unsupported Conan libcxx value: {self.libcxx!r}")
        if self.build_type not in _SUPPORTED_BUILD_TYPES:
            raise ConfigurationError(
                f"unsupported Conan build type: {self.build_type!r}"
            )


@dataclass(frozen=True)
class ConanIntegrationConfig:
    """Complete Conan-specific metadata for one validated binding."""

    settings: ConanSettings
    glibc_version: str
    linux_headers: str
    minimum_kernel: str
    compiler_family: Literal["gcc", "clang"]
    compiler_version: int

    def __post_init__(self) -> None:
        if self.compiler_family not in {"gcc", "clang"}:
            raise ConfigurationError(
                f"unsupported Conan compiler family: {self.compiler_family!r}"
            )
        if not isinstance(self.compiler_version, int) or self.compiler_version < 1:
            raise ConfigurationError("Conan compiler version must be a positive major")
        if self.settings.libcxx is None:
            raise ConfigurationError(
                "complete Conan integration config requires an inferred libcxx value"
            )
        if self.settings.cppstd is None:
            object.__setattr__(
                self,
                "settings",
                replace(
                    self.settings,
                    cppstd=_conan_default_cppstd(
                        self.compiler_family, self.compiler_version
                    ),
                ),
            )
        object.__setattr__(
            self,
            "glibc_version",
            _version(self.glibc_version, field="Conan glibc version"),
        )
        object.__setattr__(
            self,
            "linux_headers",
            _version(self.linux_headers, field="Conan Linux headers version"),
        )
        object.__setattr__(
            self,
            "minimum_kernel",
            _version(self.minimum_kernel, field="Conan minimum kernel version"),
        )

    @property
    def cppstd(self) -> str:
        value = self.settings.cppstd
        if value is None:  # Guard the type boundary after constructor resolution.
            raise AssertionError("Conan cppstd was not resolved")
        return value

    @property
    def libcxx(self) -> ConanLibcxx:
        value = self.settings.libcxx
        if value is None:  # Guard the type boundary after constructor validation.
            raise AssertionError("Conan libcxx was not resolved")
        return value

    @property
    def build_type(self) -> ConanBuildType:
        return self.settings.build_type
