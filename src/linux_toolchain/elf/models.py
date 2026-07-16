from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.versions import AbiVersion

POLICY_SCHEMA = "linux-toolchain-elf-audit-policy"
POLICY_FORMAT = 1
REPORT_SCHEMA = "linux-toolchain-elf-audit-report"
REPORT_FORMAT = 1
VERSION_NAMESPACES = ("GLIBC", "GLIBCXX", "CXXABI", "GCC")


def _string_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{context} must be an array of strings")
    if any(not item for item in value):
        raise ConfigurationError(f"{context} cannot contain empty strings")
    return tuple(value)


@dataclass(frozen=True)
class AuditPolicy:
    """The stable, serializable compatibility policy used by the ELF auditor."""

    machine: str
    max_required_versions: Mapping[str, str | None]
    elf_class: str = "ELF64"
    endianness: str = "little"
    forbidden_versions: tuple[str, ...] = ("GLIBC_PRIVATE",)
    allowed_interpreters: tuple[str, ...] = ()
    format: int = field(default=POLICY_FORMAT, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.machine, str) or not self.machine:
            raise ConfigurationError("policy.machine must be a non-empty string")
        if self.elf_class not in {"ELF32", "ELF64"}:
            raise ConfigurationError("policy.elf_class must be ELF32 or ELF64")
        if self.endianness not in {"little", "big"}:
            raise ConfigurationError("policy.endianness must be little or big")

        versions = dict(self.max_required_versions)
        unknown_namespaces = sorted(set(versions) - set(VERSION_NAMESPACES))
        if unknown_namespaces:
            raise ConfigurationError(
                "policy.max_required_versions has unknown namespaces: "
                + ", ".join(unknown_namespaces)
            )
        if "GLIBC" not in versions or versions["GLIBC"] is None:
            raise ConfigurationError("policy.max_required_versions.GLIBC is required")

        normalized: dict[str, str | None] = {}
        for namespace in VERSION_NAMESPACES:
            value = versions.get(namespace)
            if value is not None:
                if not isinstance(value, str):
                    raise ConfigurationError(
                        f"policy.max_required_versions.{namespace} must be a "
                        "numeric string or null"
                    )
                AbiVersion.parse(value)
            normalized[namespace] = value

        if not all(isinstance(item, str) and item for item in self.forbidden_versions):
            raise ConfigurationError(
                "policy.forbidden_versions must contain non-empty strings"
            )
        if not all(
            isinstance(item, str) and item for item in self.allowed_interpreters
        ):
            raise ConfigurationError(
                "policy.allowed_interpreters must contain non-empty strings"
            )

        # GLIBC_PRIVATE is an implementation detail, never a portable ABI. It is
        # an unconditional rule even if a hand-written policy forgets to list it.
        forbidden = tuple(sorted(set(self.forbidden_versions) | {"GLIBC_PRIVATE"}))
        interpreters = tuple(dict.fromkeys(self.allowed_interpreters))
        object.__setattr__(self, "max_required_versions", MappingProxyType(normalized))
        object.__setattr__(self, "forbidden_versions", forbidden)
        object.__setattr__(self, "allowed_interpreters", interpreters)

    @property
    def glibc_floor(self) -> str:
        value = self.max_required_versions["GLIBC"]
        assert value is not None
        return value

    @classmethod
    def for_glibc_floor(
        cls,
        glibc_floor: str,
        *,
        machine: str = "x86_64",
        glibcxx: str | None = None,
        cxxabi: str | None = None,
        gcc: str | None = None,
        forbidden_versions: Sequence[str] = ("GLIBC_PRIVATE",),
        allowed_interpreters: Sequence[str] | None = None,
        elf_class: str = "ELF64",
        endianness: str = "little",
    ) -> "AuditPolicy":
        if allowed_interpreters is None:
            defaults = {
                "x86_64": ("/lib64/ld-linux-x86-64.so.2",),
                "aarch64": ("/lib/ld-linux-aarch64.so.1",),
            }
            allowed_interpreters = defaults.get(machine, ())
        return cls(
            machine=machine,
            elf_class=elf_class,
            endianness=endianness,
            max_required_versions={
                "GLIBC": glibc_floor,
                "GLIBCXX": glibcxx,
                "CXXABI": cxxabi,
                "GCC": gcc,
            },
            forbidden_versions=tuple(forbidden_versions),
            allowed_interpreters=tuple(allowed_interpreters),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": POLICY_SCHEMA,
            "format": self.format,
            "machine": self.machine,
            "elf_class": self.elf_class,
            "endianness": self.endianness,
            "max_required_versions": {
                namespace: self.max_required_versions[namespace]
                for namespace in VERSION_NAMESPACES
            },
            "forbidden_versions": list(self.forbidden_versions),
            "allowed_interpreters": list(self.allowed_interpreters),
        }


def load_policy(path: Path | str) -> AuditPolicy:
    policy_path = Path(path)
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read audit policy {policy_path}: {error}"
        ) from error

    if not isinstance(data, dict):
        raise ConfigurationError("audit policy root must be an object")
    required = {
        "schema",
        "format",
        "machine",
        "elf_class",
        "endianness",
        "max_required_versions",
        "forbidden_versions",
        "allowed_interpreters",
    }
    missing = sorted(required - data.keys())
    unknown = sorted(data.keys() - required)
    if missing:
        raise ConfigurationError("audit policy is missing: " + ", ".join(missing))
    if unknown:
        raise ConfigurationError("audit policy has unknown keys: " + ", ".join(unknown))
    if data["schema"] != POLICY_SCHEMA:
        raise ConfigurationError(f"unsupported audit policy schema: {data['schema']!r}")
    if (
        not isinstance(data["format"], int)
        or isinstance(data["format"], bool)
        or data["format"] != POLICY_FORMAT
    ):
        raise ConfigurationError(f"unsupported audit policy format: {data['format']!r}")
    if not isinstance(data["max_required_versions"], dict):
        raise ConfigurationError("policy.max_required_versions must be an object")

    return AuditPolicy(
        machine=data["machine"],
        elf_class=data["elf_class"],
        endianness=data["endianness"],
        max_required_versions=data["max_required_versions"],
        forbidden_versions=_string_list(
            data["forbidden_versions"], "policy.forbidden_versions"
        ),
        allowed_interpreters=_string_list(
            data["allowed_interpreters"], "policy.allowed_interpreters"
        ),
    )


@dataclass(frozen=True, order=True)
class VersionNeed:
    library: str | None
    name: str

    def to_dict(self) -> dict[str, str | None]:
        return {"library": self.library, "name": self.name}


@dataclass(frozen=True)
class ElfMetadata:
    path: Path
    elf_class: str
    endianness: str
    elf_type: str
    machine: str
    interpreter: str | None
    needed: tuple[str, ...]
    rpath: tuple[str, ...]
    runpath: tuple[str, ...]
    has_dt_relr: bool
    version_needs: tuple[VersionNeed, ...]
    soname: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "elf_class": self.elf_class,
            "endianness": self.endianness,
            "elf_type": self.elf_type,
            "machine": self.machine,
            "interpreter": self.interpreter,
            "needed": list(self.needed),
            "soname": self.soname,
            "rpath": list(self.rpath),
            "runpath": list(self.runpath),
            "has_dt_relr": self.has_dt_relr,
            "version_needs": [need.to_dict() for need in self.version_needs],
        }


@dataclass(frozen=True, order=True)
class AuditViolation:
    code: str
    message: str
    actual: str | None = None
    limit: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "actual": self.actual,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class FileAudit:
    metadata: ElfMetadata
    violations: tuple[AuditViolation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        result = self.metadata.to_dict()
        result["passed"] = self.passed
        result["violations"] = [violation.to_dict() for violation in self.violations]
        return result


@dataclass(frozen=True)
class AuditReport:
    policy: AuditPolicy
    files: tuple[FileAudit, ...]
    format: int = field(default=REPORT_FORMAT, init=False)

    @property
    def passed(self) -> bool:
        return all(file.passed for file in self.files)

    @property
    def has_violations(self) -> bool:
        return not self.passed

    @property
    def violation_count(self) -> int:
        return sum(len(file.violations) for file in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "format": self.format,
            "policy": self.policy.to_dict(),
            "summary": {
                "passed": self.passed,
                "elf_files": len(self.files),
                "violations": self.violation_count,
            },
            "files": [file.to_dict() for file in self.files],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    def to_text(self) -> str:
        lines: list[str] = []
        for file in self.files:
            state = "PASS" if file.passed else "FAIL"
            lines.append(f"{state} {file.metadata.path}")
            for violation in file.violations:
                lines.append(f"  [{violation.code}] {violation.message}")
        state = "PASS" if self.passed else "FAIL"
        lines.append(
            f"Summary: {state}; {len(self.files)} ELF file(s), "
            f"{self.violation_count} violation(s)"
        )
        return "\n".join(lines)
