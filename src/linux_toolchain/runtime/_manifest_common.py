from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from linux_toolchain.elf.models import VERSION_NAMESPACES
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.schema import non_empty_string as _string
from linux_toolchain.schema import object_value as _object
from linux_toolchain.schema import relative_posix_path as _relative_path
from linux_toolchain.versions import AbiVersion


def relative_paths(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{context} must be an array")
    paths = tuple(
        _relative_path(item, f"{context}[{index}]") for index, item in enumerate(value)
    )
    if paths != tuple(sorted(set(paths))):
        raise ConfigurationError(f"{context} must be sorted and unique")
    return paths


def _version_map(value: object, context: str) -> Mapping[str, tuple[str, ...]]:
    data = _object(value, required=set(VERSION_NAMESPACES), context=context)
    result: dict[str, tuple[str, ...]] = {}
    for namespace in VERSION_NAMESPACES:
        raw = data[namespace]
        if not isinstance(raw, list):
            raise ConfigurationError(f"{context}.{namespace} must be an array")
        versions = tuple(
            _string(item, f"{context}.{namespace}[{index}]")
            for index, item in enumerate(raw)
        )
        for version in versions:
            AbiVersion.parse(version)
        if versions != tuple(sorted(set(versions), key=AbiVersion.parse)):
            raise ConfigurationError(f"{context}.{namespace} must be sorted and unique")
        result[namespace] = versions
    return MappingProxyType(result)


def _maximum_version_map(value: object, context: str) -> Mapping[str, str | None]:
    data = _object(value, required=set(VERSION_NAMESPACES), context=context)
    result: dict[str, str | None] = {}
    for namespace in VERSION_NAMESPACES:
        version = data[namespace]
        if version is not None:
            version = _string(version, f"{context}.{namespace}")
            AbiVersion.parse(version)
        result[namespace] = version
    return MappingProxyType(result)


def parse_symbol_report(
    value: object,
    *,
    context: str,
    require_elf64_little: bool,
) -> Mapping[str, object]:
    data = _object(
        value,
        required={
            "path",
            "machine",
            "elf_class",
            "endianness",
            "required_versions",
            "max_required_versions",
        },
        context=context,
    )
    required = _version_map(data["required_versions"], f"{context}.required_versions")
    maximum = _maximum_version_map(
        data["max_required_versions"], f"{context}.max_required_versions"
    )
    for namespace in VERSION_NAMESPACES:
        expected = required[namespace][-1] if required[namespace] else None
        if maximum[namespace] != expected:
            raise ConfigurationError(
                f"{context}.max_required_versions.{namespace} is inconsistent"
            )

    elf_class = _string(data["elf_class"], f"{context}.elf_class")
    endianness = _string(data["endianness"], f"{context}.endianness")
    if require_elf64_little:
        if elf_class != "ELF64":
            raise ConfigurationError(f"{context}.elf_class must be ELF64")
        if endianness != "little":
            raise ConfigurationError(f"{context}.endianness must be little")
    else:
        if elf_class not in {"ELF32", "ELF64"}:
            raise ConfigurationError(f"{context}.elf_class must be ELF32 or ELF64")
        if endianness not in {"little", "big"}:
            raise ConfigurationError(f"{context}.endianness must be little or big")

    return MappingProxyType(
        {
            "path": _relative_path(data["path"], f"{context}.path"),
            "machine": _string(data["machine"], f"{context}.machine"),
            "elf_class": elf_class,
            "endianness": endianness,
            "required_versions": required,
            "max_required_versions": maximum,
        }
    )


def serialize_symbol_reports(
    reports: tuple[Mapping[str, object], ...],
) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for report in reports:
        required = report["required_versions"]
        maximum = report["max_required_versions"]
        assert isinstance(required, Mapping)
        assert isinstance(maximum, Mapping)
        serialized.append(
            {
                "path": report["path"],
                "machine": report["machine"],
                "elf_class": report["elf_class"],
                "endianness": report["endianness"],
                "required_versions": {
                    namespace: list(required[namespace])
                    for namespace in VERSION_NAMESPACES
                },
                "max_required_versions": {
                    namespace: maximum[namespace] for namespace in VERSION_NAMESPACES
                },
            }
        )
    return serialized
