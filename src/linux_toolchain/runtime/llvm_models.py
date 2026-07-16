from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.models import classify_linux_glibc_target
from linux_toolchain.runtime._manifest_common import (
    parse_symbol_report,
    relative_paths,
    serialize_symbol_reports,
)
from linux_toolchain.schema import (
    non_empty_string as _string,
)
from linux_toolchain.schema import (
    object_value as _object,
)
from linux_toolchain.schema import (
    positive_integer as _integer,
)
from linux_toolchain.schema import read_json_object
from linux_toolchain.schema import (
    relative_posix_path as _relative_path,
)
from linux_toolchain.versions import AbiVersion

LLVM_RUNTIME_MANIFEST_SCHEMA = "linux-toolchain-llvm-runtime"
LLVM_RUNTIME_MANIFEST_FORMAT = 1
LLVM_RUNTIME_FORBIDDEN_SONAMES = ("libgcc_s.so.1", "libstdc++.so.6")
LLVM_RUNTIME_COMPONENTS = ("libc++abi", "libc++", "libunwind")
LLVM_EXPERIMENTAL_STATIC_LIBRARY = "libc++experimental.a"

_SHA512_RE = re.compile(r"^[0-9a-f]{128}$")
_BUILTINS_RE = re.compile(r"^libclang_rt\.builtins(?:-(?:x86_64|aarch64))?\.a$")
_COMPILER_RT_CRT_RE = re.compile(
    r"^clang_rt\.crt(begin|end)(?:-(?:x86_64|aarch64))?\.o$"
)


def llvm_runtime_component(name: str) -> str | None:
    for component in LLVM_RUNTIME_COMPONENTS:
        if name == f"{component}.a" or name.startswith(f"{component}.so"):
            return component
    return None


def _source_evidence(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict):
        raise ConfigurationError("LLVM runtime manifest.source must be an object")
    kind = value.get("kind")
    if kind == "managed-artifact":
        data = _object(
            value,
            required={
                "kind",
                "version",
                "target",
                "url",
                "sha512",
            },
            context="LLVM runtime manifest.source",
        )
        result = {
            key: _string(raw, f"LLVM runtime manifest.source.{key}")
            for key, raw in data.items()
        }
        if not result["url"].startswith("https://"):
            raise ConfigurationError("LLVM runtime managed source URL must use HTTPS")
        if not _SHA512_RE.fullmatch(result["sha512"]):
            raise ConfigurationError(
                "LLVM runtime managed source SHA-512 must be lowercase hexadecimal"
            )
        classify_linux_glibc_target(
            result["target"],
            policy="strict",
            context="LLVM runtime managed source target",
        )
        return MappingProxyType(result)
    if kind == "clang-probe":
        data = _object(
            value,
            required={
                "kind",
                "version",
                "target",
            },
            context="LLVM runtime manifest.source",
        )
        result = {
            key: _string(raw, f"LLVM runtime manifest.source.{key}")
            for key, raw in data.items()
        }
        AbiVersion.parse(result["version"])
        classify_linux_glibc_target(
            result["target"],
            policy="strict",
            context="LLVM runtime Clang probe target",
        )
        return MappingProxyType(result)
    raise ConfigurationError(
        "LLVM runtime manifest.source.kind must be managed-artifact or clang-probe"
    )


@dataclass(frozen=True)
class LlvmRuntimeSourceEvidence:
    """Validated, relocatable source evidence for an LLVM runtime export."""

    values: Mapping[str, str]

    @classmethod
    def from_dict(cls, value: object) -> "LlvmRuntimeSourceEvidence":
        return cls(values=_source_evidence(value))

    def to_dict(self) -> dict[str, str]:
        return dict(self.values)


def _validation_evidence(value: object) -> Mapping[str, str]:
    data = _object(
        value,
        required={"payload", "final_link"},
        context="LLVM runtime manifest.validation",
    )
    result = {
        key: _string(raw, f"LLVM runtime manifest.validation.{key}")
        for key, raw in data.items()
    }
    if result["payload"] != "passed":
        raise ConfigurationError(
            "LLVM runtime manifest.validation.payload must be passed"
        )
    if result["final_link"] != "binding-required":
        raise ConfigurationError(
            "LLVM runtime final link validation must remain binding-required"
        )
    return MappingProxyType(result)


def _provider_evidence(value: object) -> tuple[Mapping[str, object], str]:
    data = _object(
        value,
        required={"name", "version", "major"},
        context="LLVM runtime manifest.provider",
    )
    if data["name"] != "llvm":
        raise ConfigurationError("LLVM runtime manifest.provider.name must be llvm")
    version = _string(data["version"], "LLVM runtime manifest.provider.version")
    parsed_version = AbiVersion.parse(version)
    major = _integer(data["major"], "LLVM runtime manifest.provider.major")
    if major != parsed_version.parts[0]:
        raise ConfigurationError(
            "LLVM runtime manifest.provider.major is inconsistent with version"
        )
    return MappingProxyType(
        {"name": "llvm", "version": version, "major": major}
    ), version


def _abi_evidence(value: object) -> Mapping[str, str]:
    data = _object(
        value,
        required={"standard_library", "cxxabi", "unwind", "rtlib", "linkage"},
        context="LLVM runtime manifest.abi",
    )
    expected = {
        "standard_library": "libc++",
        "cxxabi": "libc++abi",
        "unwind": "libunwind",
        "rtlib": "compiler-rt",
    }
    for key, required in expected.items():
        if data[key] != required:
            raise ConfigurationError(
                f"LLVM runtime manifest.abi.{key} must be {required}"
            )
    linkage = _string(data["linkage"], "LLVM runtime manifest.abi.linkage")
    if linkage != "both":
        raise ConfigurationError("LLVM runtime manifest.abi.linkage must be both")
    return MappingProxyType({**expected, "linkage": linkage})


def _runtime_locations(
    value: object,
    *,
    managed_source: bool,
) -> Mapping[str, object]:
    data = _object(
        value,
        required={
            "runtime",
            "cxx_include_dirs",
            "resource_dir",
            "library_dirs",
            "shared_libraries",
            "static_libraries",
            "builtins",
            "crt_objects",
        },
        context="LLVM runtime manifest.locations",
    )
    locations: Mapping[str, object] = MappingProxyType(
        {
            "runtime": _relative_path(
                data["runtime"], "LLVM runtime manifest.locations.runtime"
            ),
            "cxx_include_dirs": relative_paths(
                data["cxx_include_dirs"],
                "LLVM runtime manifest.locations.cxx_include_dirs",
            ),
            "resource_dir": _relative_path(
                data["resource_dir"],
                "LLVM runtime manifest.locations.resource_dir",
            ),
            "library_dirs": relative_paths(
                data["library_dirs"],
                "LLVM runtime manifest.locations.library_dirs",
            ),
            "shared_libraries": relative_paths(
                data["shared_libraries"],
                "LLVM runtime manifest.locations.shared_libraries",
            ),
            "static_libraries": relative_paths(
                data["static_libraries"],
                "LLVM runtime manifest.locations.static_libraries",
            ),
            "builtins": _relative_path(
                data["builtins"], "LLVM runtime manifest.locations.builtins"
            ),
            "crt_objects": relative_paths(
                data["crt_objects"],
                "LLVM runtime manifest.locations.crt_objects",
            ),
        }
    )
    if locations["runtime"] != "runtime":
        raise ConfigurationError(
            "LLVM runtime manifest.locations.runtime must be 'runtime'"
        )
    for key, raw in locations.items():
        paths = (raw,) if isinstance(raw, str) else raw
        if any(PurePosixPath(path).parts[0] != "runtime" for path in paths):
            raise ConfigurationError(
                f"LLVM runtime manifest.locations.{key} must be under runtime/"
            )
    for key in ("cxx_include_dirs", "library_dirs"):
        if not locations[key]:
            raise ConfigurationError(
                f"LLVM runtime manifest.locations.{key} cannot be empty"
            )
    shared = {PurePosixPath(path).name for path in locations["shared_libraries"]}
    if not {"libc++.so", "libc++abi.so", "libunwind.so"}.issubset(shared):
        raise ConfigurationError(
            "shared LLVM runtime must contain unversioned libc++, libc++abi, "
            "and libunwind linker entry points"
        )
    static = {PurePosixPath(path).name for path in locations["static_libraries"]}
    required_static = {"libc++.a", "libc++abi.a", "libunwind.a"}
    allowed_static = required_static | {LLVM_EXPERIMENTAL_STATIC_LIBRARY}
    if not required_static.issubset(static) or not static.issubset(allowed_static):
        raise ConfigurationError(
            "static LLVM runtime must contain libc++.a, libc++abi.a, and "
            "libunwind.a, with only libc++experimental.a permitted in addition"
        )
    if managed_source and LLVM_EXPERIMENTAL_STATIC_LIBRARY not in static:
        raise ConfigurationError(
            "managed LLVM runtime must contain libc++experimental.a"
        )
    if not _BUILTINS_RE.fullmatch(PurePosixPath(str(locations["builtins"])).name):
        raise ConfigurationError(
            "LLVM runtime builtins location must name a clang_rt.builtins archive"
        )
    crt_names = {PurePosixPath(path).name for path in locations["crt_objects"]}
    crt_kinds = {
        match.group(1)
        for name in crt_names
        if (match := _COMPILER_RT_CRT_RE.fullmatch(name)) is not None
    }
    if len(crt_names) != 2 or crt_kinds != {"begin", "end"}:
        raise ConfigurationError(
            "LLVM runtime must contain one compiler-rt crtbegin and crtend object"
        )
    return locations


def _forbidden_sonames(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(
            "LLVM runtime manifest.forbidden_sonames must be an array"
        )
    forbidden = tuple(
        _string(item, f"LLVM runtime manifest.forbidden_sonames[{index}]")
        for index, item in enumerate(value)
    )
    if forbidden != tuple(sorted(set(forbidden))):
        raise ConfigurationError(
            "LLVM runtime manifest.forbidden_sonames must be sorted and unique"
        )
    if not set(LLVM_RUNTIME_FORBIDDEN_SONAMES).issubset(forbidden):
        raise ConfigurationError(
            "LLVM runtime must forbid libstdc++.so.6 and libgcc_s.so.1"
        )
    return forbidden


def _version_symbol_reports(
    value: object,
    *,
    arch: str,
    shared_libraries: object,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ConfigurationError(
            "LLVM runtime manifest.version_symbol_reports must be an array"
        )
    reports = tuple(
        parse_symbol_report(
            report,
            context=f"LLVM runtime manifest.version_symbol_reports[{index}]",
            require_elf64_little=True,
        )
        for index, report in enumerate(value)
    )
    paths = tuple(str(report["path"]) for report in reports)
    if paths != tuple(sorted(set(paths))):
        raise ConfigurationError(
            "LLVM runtime manifest.version_symbol_reports must use sorted unique paths"
        )
    for report in reports:
        report_path = PurePosixPath(str(report["path"]))
        if report_path.parts[0] != "runtime":
            raise ConfigurationError(
                "LLVM runtime symbol reports must be under runtime/"
            )
        if report["machine"] != arch:
            raise ConfigurationError(
                "LLVM runtime symbol report machine does not match arch"
            )
    if not reports:
        raise ConfigurationError(
            "shared LLVM runtime must contain version symbol reports"
        )
    report_components = {
        llvm_runtime_component(PurePosixPath(path).name) for path in paths
    }
    if len(reports) != 3 or report_components != {
        "libc++",
        "libc++abi",
        "libunwind",
    }:
        raise ConfigurationError(
            "shared LLVM runtime must report independent libc++, libc++abi, "
            "and libunwind ELF owners"
        )
    if not isinstance(shared_libraries, tuple):
        raise ConfigurationError(
            "LLVM runtime manifest.locations.shared_libraries is invalid"
        )
    if not set(paths).issubset(set(shared_libraries)):
        raise ConfigurationError(
            "LLVM runtime symbol reports must identify published libraries"
        )
    return reports


@dataclass(frozen=True)
class LlvmRuntimeManifest:
    """Relocatable libc++/compiler-rt runtime selected by a managed Clang."""

    provider: Mapping[str, object]
    arch: str
    target: str
    glibc_floor: str
    source: Mapping[str, str]
    abi: Mapping[str, str]
    locations: Mapping[str, object]
    forbidden_sonames: tuple[str, ...]
    version_symbol_reports: tuple[Mapping[str, object], ...]
    validation: Mapping[str, str]
    schema: str = LLVM_RUNTIME_MANIFEST_SCHEMA
    format: int = LLVM_RUNTIME_MANIFEST_FORMAT

    @classmethod
    def from_dict(cls, value: object) -> "LlvmRuntimeManifest":
        data = _object(
            value,
            required={
                "schema",
                "format",
                "provider",
                "arch",
                "target",
                "glibc_floor",
                "source",
                "abi",
                "locations",
                "forbidden_sonames",
                "version_symbol_reports",
                "validation",
            },
            context="LLVM runtime manifest",
        )
        if data["schema"] != LLVM_RUNTIME_MANIFEST_SCHEMA:
            raise ConfigurationError(
                f"unsupported LLVM runtime manifest schema: {data['schema']!r}"
            )
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != LLVM_RUNTIME_MANIFEST_FORMAT
        ):
            raise ConfigurationError(
                f"unsupported LLVM runtime manifest format: {data['format']!r}"
            )

        provider, version = _provider_evidence(data["provider"])

        arch = _string(data["arch"], "LLVM runtime manifest.arch")
        if arch not in {"x86_64", "aarch64"}:
            raise ConfigurationError(
                "LLVM runtime manifest.arch must be x86_64 or aarch64"
            )
        target = _string(data["target"], "LLVM runtime manifest.target")
        classify_linux_glibc_target(
            target,
            policy="strict",
            expected_architecture=arch,
            context="LLVM runtime target",
        )
        glibc_floor = _string(data["glibc_floor"], "LLVM runtime manifest.glibc_floor")
        parsed_floor = AbiVersion.parse(glibc_floor)
        if arch == "aarch64" and parsed_floor < AbiVersion.parse("2.17"):
            raise ConfigurationError(
                "LLVM AArch64 runtimes require glibc 2.17 or newer"
            )
        source = LlvmRuntimeSourceEvidence.from_dict(data["source"]).values
        if source["version"] != version or source["target"] != target:
            raise ConfigurationError(
                "LLVM runtime source evidence does not match provider or target"
            )

        abi = _abi_evidence(data["abi"])

        locations = _runtime_locations(
            data["locations"],
            managed_source=source["kind"] == "managed-artifact",
        )

        forbidden = _forbidden_sonames(data["forbidden_sonames"])
        reports = _version_symbol_reports(
            data["version_symbol_reports"],
            arch=arch,
            shared_libraries=locations["shared_libraries"],
        )
        validation = _validation_evidence(data["validation"])

        return cls(
            provider=provider,
            arch=arch,
            target=target,
            glibc_floor=glibc_floor,
            source=source,
            abi=abi,
            locations=locations,
            forbidden_sonames=forbidden,
            version_symbol_reports=reports,
            validation=validation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "format": self.format,
            "provider": dict(self.provider),
            "arch": self.arch,
            "target": self.target,
            "glibc_floor": self.glibc_floor,
            "source": dict(self.source),
            "abi": dict(self.abi),
            "locations": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in self.locations.items()
            },
            "forbidden_sonames": list(self.forbidden_sonames),
            "version_symbol_reports": serialize_symbol_reports(
                self.version_symbol_reports
            ),
            "validation": dict(self.validation),
        }


def load_llvm_runtime_manifest(path: Path | str) -> LlvmRuntimeManifest:
    candidate = Path(path).expanduser()
    manifest_path = candidate / "manifest.json" if candidate.is_dir() else candidate
    return LlvmRuntimeManifest.from_dict(
        read_json_object(manifest_path, "LLVM runtime manifest")
    )
