from __future__ import annotations

import json
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
from linux_toolchain.schema import (
    relative_posix_path as _relative_path,
)
from linux_toolchain.versions import AbiVersion

RUNTIME_MANIFEST_SCHEMA = "linux-toolchain-gcc-runtime"
RUNTIME_MANIFEST_FORMAT = 1


@dataclass(frozen=True)
class GccRuntimeManifest:
    """Validated, immutable view of an imported GCC runtime manifest."""

    provider: Mapping[str, object]
    arch: str
    target: str
    glibc_floor: str
    locations: Mapping[str, object]
    version_symbol_reports: tuple[Mapping[str, object], ...]
    schema: str = RUNTIME_MANIFEST_SCHEMA
    format: int = RUNTIME_MANIFEST_FORMAT

    @classmethod
    def from_dict(cls, value: object) -> "GccRuntimeManifest":
        data = _object(
            value,
            required={
                "schema",
                "format",
                "provider",
                "arch",
                "target",
                "glibc_floor",
                "locations",
                "version_symbol_reports",
            },
            context="runtime manifest",
        )
        if data["schema"] != RUNTIME_MANIFEST_SCHEMA:
            raise ConfigurationError(
                f"unsupported runtime manifest schema: {data['schema']!r}"
            )
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != RUNTIME_MANIFEST_FORMAT
        ):
            raise ConfigurationError(
                f"unsupported runtime manifest format: {data['format']!r}"
            )

        provider_data = _object(
            data["provider"],
            required={"name", "version", "major"},
            context="runtime manifest.provider",
        )
        if provider_data["name"] != "gcc":
            raise ConfigurationError("runtime manifest.provider.name must be gcc")
        provider_version = _string(
            provider_data["version"], "runtime manifest.provider.version"
        )
        AbiVersion.parse(provider_version)
        provider_major = _integer(
            provider_data["major"], "runtime manifest.provider.major"
        )
        if provider_major != AbiVersion.parse(provider_version).parts[0]:
            raise ConfigurationError(
                "runtime manifest.provider.major is inconsistent with version"
            )
        provider: Mapping[str, object] = MappingProxyType(
            {
                "name": "gcc",
                "version": provider_version,
                "major": provider_major,
            }
        )

        arch = _string(data["arch"], "runtime manifest.arch")
        if arch not in {"x86_64", "aarch64"}:
            raise ConfigurationError("runtime manifest.arch must be x86_64 or aarch64")
        target = _string(data["target"], "runtime manifest.target")
        classify_linux_glibc_target(
            target,
            policy="strict",
            expected_architecture=arch,
            context="runtime manifest target",
        )

        glibc_floor = _string(data["glibc_floor"], "runtime manifest.glibc_floor")
        AbiVersion.parse(glibc_floor)
        location_data = _object(
            data["locations"],
            required={
                "runtime",
                "cxx_include_dirs",
                "gcc_runtime_dir",
                "library_dirs",
                "crt_objects",
                "static_libraries",
                "shared_libraries",
            },
            context="runtime manifest.locations",
        )
        locations: Mapping[str, object] = MappingProxyType(
            {
                "runtime": _relative_path(
                    location_data["runtime"],
                    "runtime manifest.locations.runtime",
                ),
                "cxx_include_dirs": relative_paths(
                    location_data["cxx_include_dirs"],
                    "runtime manifest.locations.cxx_include_dirs",
                ),
                "gcc_runtime_dir": _relative_path(
                    location_data["gcc_runtime_dir"],
                    "runtime manifest.locations.gcc_runtime_dir",
                ),
                "library_dirs": relative_paths(
                    location_data["library_dirs"],
                    "runtime manifest.locations.library_dirs",
                ),
                "crt_objects": relative_paths(
                    location_data["crt_objects"],
                    "runtime manifest.locations.crt_objects",
                ),
                "static_libraries": relative_paths(
                    location_data["static_libraries"],
                    "runtime manifest.locations.static_libraries",
                ),
                "shared_libraries": relative_paths(
                    location_data["shared_libraries"],
                    "runtime manifest.locations.shared_libraries",
                ),
            }
        )
        if locations["runtime"] != "runtime":
            raise ConfigurationError(
                "runtime manifest.locations.runtime must be 'runtime'"
            )
        for key, location in locations.items():
            values = (location,) if isinstance(location, str) else location
            if any(PurePosixPath(item).parts[0] != "runtime" for item in values):
                raise ConfigurationError(
                    f"runtime manifest.locations.{key} must be under runtime/"
                )
        for key in (
            "cxx_include_dirs",
            "library_dirs",
            "crt_objects",
            "static_libraries",
            "shared_libraries",
        ):
            if not locations[key]:
                raise ConfigurationError(
                    f"runtime manifest.locations.{key} cannot be empty"
                )
        crt_names = {PurePosixPath(path).name for path in locations["crt_objects"]}
        if not any(name.startswith("crtbegin") for name in crt_names) or not any(
            name.startswith("crtend") for name in crt_names
        ):
            raise ConfigurationError(
                "runtime manifest must locate crtbegin and crtend objects"
            )
        static_names = {
            PurePosixPath(path).name for path in locations["static_libraries"]
        }
        if not {"libgcc.a", "libstdc++.a"}.issubset(static_names):
            raise ConfigurationError(
                "runtime manifest must locate static libgcc.a and libstdc++.a"
            )
        shared_names = {
            PurePosixPath(path).name for path in locations["shared_libraries"]
        }
        if not any(name.startswith("libstdc++.so") for name in shared_names) or not any(
            name.startswith("libgcc_s.so") for name in shared_names
        ):
            raise ConfigurationError(
                "runtime manifest must locate shared libstdc++ and libgcc_s"
            )

        reports_raw = data["version_symbol_reports"]
        if not isinstance(reports_raw, list):
            raise ConfigurationError(
                "runtime manifest.version_symbol_reports must be an array"
            )
        reports = tuple(
            parse_symbol_report(
                report,
                context=f"runtime manifest.version_symbol_reports[{index}]",
                require_elf64_little=False,
            )
            for index, report in enumerate(reports_raw)
        )
        report_paths = tuple(str(report["path"]) for report in reports)
        if report_paths != tuple(sorted(set(report_paths))):
            raise ConfigurationError(
                "runtime manifest.version_symbol_reports must be sorted by unique path"
            )
        if not reports:
            raise ConfigurationError(
                "runtime manifest.version_symbol_reports cannot be empty"
            )
        if any(PurePosixPath(path).parts[0] != "runtime" for path in report_paths):
            raise ConfigurationError(
                "runtime manifest.version_symbol_reports paths must be under runtime/"
            )

        return cls(
            provider=provider,
            arch=arch,
            target=target,
            glibc_floor=glibc_floor,
            locations=locations,
            version_symbol_reports=reports,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "format": self.format,
            "provider": dict(self.provider),
            "arch": self.arch,
            "target": self.target,
            "glibc_floor": self.glibc_floor,
            "locations": {
                key: list(value) if isinstance(value, tuple) else value
                for key, value in self.locations.items()
            },
            "version_symbol_reports": serialize_symbol_reports(
                self.version_symbol_reports
            ),
        }


def load_runtime_manifest(path: Path | str) -> GccRuntimeManifest:
    candidate = Path(path).expanduser()
    manifest_path = candidate / "manifest.json" if candidate.is_dir() else candidate
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read GCC runtime manifest {manifest_path}: {error}"
        ) from error
    return GccRuntimeManifest.from_dict(data)
