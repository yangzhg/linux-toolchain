from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Iterable

from linux_toolchain.elf.compatibility import GLIBC_DT_RELR_MIN_VERSION
from linux_toolchain.elf.models import (
    AuditPolicy,
    AuditReport,
    AuditViolation,
    ElfMetadata,
    FileAudit,
)
from linux_toolchain.elf.reader import ReadElfInspector, discover_elf_files
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.versions import AbiVersion

_VERSION_NAME = re.compile(r"^(GLIBCXX|GLIBC|CXXABI|GCC)_(\d+(?:\.\d+)*)$")


def _greater_than(left: str, right: str) -> bool:
    return AbiVersion.parse(left) > AbiVersion.parse(right)


def _maximum(values: Iterable[str]) -> str | None:
    return max(values, key=AbiVersion.parse, default=None)


def _evaluate(metadata: ElfMetadata, policy: AuditPolicy) -> tuple[AuditViolation, ...]:
    violations: list[AuditViolation] = []

    if metadata.machine != policy.machine:
        violations.append(
            AuditViolation(
                code="machine_mismatch",
                message=(
                    f"ELF machine {metadata.machine!r} does not match policy "
                    f"machine {policy.machine!r}"
                ),
                actual=metadata.machine,
                limit=policy.machine,
            )
        )

    if metadata.elf_class != policy.elf_class:
        violations.append(
            AuditViolation(
                code="elf_class_mismatch",
                message=(
                    f"ELF class {metadata.elf_class!r} does not match policy "
                    f"class {policy.elf_class!r}"
                ),
                actual=metadata.elf_class,
                limit=policy.elf_class,
            )
        )
    if metadata.endianness != policy.endianness:
        violations.append(
            AuditViolation(
                code="endianness_mismatch",
                message=(
                    f"ELF endianness {metadata.endianness!r} does not match "
                    f"policy endianness {policy.endianness!r}"
                ),
                actual=metadata.endianness,
                limit=policy.endianness,
            )
        )

    if (
        metadata.interpreter is not None
        and metadata.interpreter not in policy.allowed_interpreters
    ):
        violations.append(
            AuditViolation(
                code="interpreter_not_allowed",
                message=f"ELF interpreter is not allowed: {metadata.interpreter}",
                actual=metadata.interpreter,
                limit=",".join(policy.allowed_interpreters),
            )
        )

    needs_by_name = {need.name for need in metadata.version_needs}
    for name in sorted(needs_by_name & set(policy.forbidden_versions)):
        violations.append(
            AuditViolation(
                code="forbidden_version_required",
                message=f"ELF requires forbidden symbol version {name}",
                actual=name,
            )
        )

    numeric_needs: dict[str, list[str]] = {
        namespace: [] for namespace in policy.max_required_versions
    }
    for name in sorted(needs_by_name):
        match = _VERSION_NAME.fullmatch(name)
        if match and match.group(1) in numeric_needs:
            numeric_needs[match.group(1)].append(match.group(2))
        elif name.startswith("GLIBC_") and name not in {
            "GLIBC_PRIVATE",
            "GLIBC_ABI_DT_RELR",
        }:
            violations.append(
                AuditViolation(
                    code="unknown_glibc_version_required",
                    message=f"ELF requires unknown glibc symbol version {name}",
                    actual=name,
                    limit=policy.glibc_floor,
                )
            )

    for namespace, limit in policy.max_required_versions.items():
        if limit is None:
            continue
        actual = _maximum(numeric_needs[namespace])
        if actual is not None and _greater_than(actual, limit):
            code = (
                "glibc_floor_exceeded"
                if namespace == "GLIBC"
                else f"{namespace.lower()}_ceiling_exceeded"
            )
            violations.append(
                AuditViolation(
                    code=code,
                    message=(
                        f"maximum required {namespace} version {actual} exceeds "
                        f"the policy limit {limit}"
                    ),
                    actual=actual,
                    limit=limit,
                )
            )

    old_glibc_floor = AbiVersion.parse(policy.glibc_floor) < GLIBC_DT_RELR_MIN_VERSION
    abi_dt_relr_is_forbidden = "GLIBC_ABI_DT_RELR" in policy.forbidden_versions
    if (
        old_glibc_floor
        and "GLIBC_ABI_DT_RELR" in needs_by_name
        and not abi_dt_relr_is_forbidden
    ):
        violations.append(
            AuditViolation(
                code="glibc_abi_dt_relr_unsupported",
                message=(
                    "GLIBC_ABI_DT_RELR is not supported by the requested "
                    f"glibc floor {policy.glibc_floor}"
                ),
                actual="GLIBC_ABI_DT_RELR",
                limit=policy.glibc_floor,
            )
        )
    if old_glibc_floor and metadata.has_dt_relr:
        violations.append(
            AuditViolation(
                code="dt_relr_unsupported",
                message=(
                    "DT_RELR relocations are not supported by the requested "
                    f"glibc floor {policy.glibc_floor}"
                ),
                actual="DT_RELR",
                limit=policy.glibc_floor,
            )
        )

    for tag, entries in (("rpath", metadata.rpath), ("runpath", metadata.runpath)):
        for entry in entries:
            if not entry:
                violations.append(
                    AuditViolation(
                        code=f"empty_{tag}",
                        message=(
                            f"empty {tag.upper()} entry searches the process "
                            "working directory"
                        ),
                    )
                )
            elif PurePosixPath(entry).is_absolute():
                violations.append(
                    AuditViolation(
                        code=f"absolute_{tag}",
                        message=(
                            f"absolute {tag.upper()} entry is not portable: {entry}"
                        ),
                        actual=entry,
                    )
                )
            elif entry != "$ORIGIN" and not entry.startswith("$ORIGIN/"):
                violations.append(
                    AuditViolation(
                        code=f"relative_{tag}",
                        message=(
                            f"relative {tag.upper()} entry is not anchored at "
                            f"$ORIGIN: {entry}"
                        ),
                        actual=entry,
                    )
                )

    for needed in metadata.needed:
        if PurePosixPath(needed).is_absolute():
            violations.append(
                AuditViolation(
                    code="absolute_needed",
                    message=f"absolute DT_NEEDED entry is not portable: {needed}",
                    actual=needed,
                )
            )
        elif "/" in needed:
            violations.append(
                AuditViolation(
                    code="relative_needed_path",
                    message=(
                        "relative DT_NEEDED path depends on the process working "
                        f"directory: {needed}"
                    ),
                    actual=needed,
                )
            )

    if metadata.soname is not None:
        if PurePosixPath(metadata.soname).is_absolute():
            violations.append(
                AuditViolation(
                    code="absolute_soname",
                    message=(
                        "absolute DT_SONAME can create non-portable DT_NEEDED "
                        f"entries: {metadata.soname}"
                    ),
                    actual=metadata.soname,
                )
            )
        elif "/" in metadata.soname:
            violations.append(
                AuditViolation(
                    code="relative_soname_path",
                    message=(
                        "path-valued DT_SONAME can create working-directory "
                        f"dependent DT_NEEDED entries: {metadata.soname}"
                    ),
                    actual=metadata.soname,
                )
            )

    return tuple(sorted(set(violations)))


def audit_metadata(metadata: ElfMetadata, policy: AuditPolicy) -> FileAudit:
    return FileAudit(metadata=metadata, violations=_evaluate(metadata, policy))


def audit_paths(
    paths: Iterable[Path | str] | Path | str,
    policy: AuditPolicy,
    *,
    recursive: bool = True,
    inspector: ReadElfInspector | None = None,
) -> AuditReport:
    reader = inspector or ReadElfInspector()
    files = discover_elf_files(paths, recursive=recursive)
    metadata = tuple(reader.inspect(path) for path in files)
    final_metadata = tuple(item for item in metadata if item.elf_type != "REL")
    if not final_metadata:
        raise ConfigurationError(
            "no auditable final ELF files found (ET_REL objects are skipped)"
        )
    audits = tuple(audit_metadata(item, policy) for item in final_metadata)
    return AuditReport(policy=policy, files=audits)
