from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from linux_toolchain.elf.compatibility import validate_dt_relr_compatibility
from linux_toolchain.elf.reader import ReadElfInspector, is_elf
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.versions import AbiVersion

_HOST_SYSTEM_LIBRARIES = re.compile(
    r"^(?:ld-linux[^/]*|libc|libm|libdl|libpthread|librt|libutil|libresolv)"
    r"\.so(?:\..*)?$"
)
_HOST_INTERPRETERS = {
    "x86_64": "/lib64/ld-linux-x86-64.so.2",
    "aarch64": "/lib/ld-linux-aarch64.so.1",
}


def validate_artifact_symlinks(root: Path, *, context: str) -> None:
    """Reject absolute, dangling, or artifact-escaping symlinks."""

    canonical = root.resolve(strict=True)
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = path.readlink()
        if target.is_absolute():
            raise ConfigurationError(
                f"{context} contains an absolute symlink: {path} -> {target}"
            )
        try:
            path.resolve(strict=True).relative_to(canonical)
        except (OSError, RuntimeError, ValueError) as error:
            raise ConfigurationError(
                f"{context} symlink escapes or dangles: {path} -> {target}"
            ) from error


def audit_host_artifact(
    root: Path,
    *,
    arch: str,
    glibc_floor: str,
    context: str,
    inspector: ReadElfInspector | None = None,
    elf_predicate: Callable[[Path], bool] = is_elf,
) -> dict[str, object]:
    """Audit relocatable host ELF files against one architecture and glibc floor."""

    floor = AbiVersion.parse(glibc_floor)
    reader = inspector or ReadElfInspector()
    audited = 0
    max_version: str | None = None
    canonical_root = root.resolve(strict=True)
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file() or not elf_predicate(path):
            continue
        metadata = reader.inspect(path)
        if metadata.machine != arch:
            raise ExternalToolError(
                f"{context} contains {metadata.machine} host ELF for {arch}: {path}"
            )
        expected_interpreter = _HOST_INTERPRETERS[arch]
        if (
            metadata.interpreter is not None
            and metadata.interpreter != expected_interpreter
        ):
            raise ExternalToolError(
                f"{context} host ELF uses interpreter {metadata.interpreter!r}, "
                f"expected {expected_interpreter!r}: {path}"
            )
        validate_dt_relr_compatibility(path, metadata, floor)
        audited += 1
        search_directories: list[Path] = []
        for entry in (*metadata.rpath, *metadata.runpath):
            if not entry or (entry != "$ORIGIN" and not entry.startswith("$ORIGIN/")):
                raise ExternalToolError(
                    f"{context} host ELF has a non-relocatable dynamic path: "
                    f"{path}: {entry}"
                )
            suffix = entry.removeprefix("$ORIGIN").removeprefix("/")
            if "$" in suffix:
                raise ExternalToolError(
                    f"{context} host ELF has an unsupported dynamic token: "
                    f"{path}: {entry}"
                )
            try:
                destination = (path.parent / suffix).resolve(strict=True)
                destination.relative_to(canonical_root)
            except (OSError, RuntimeError, ValueError) as error:
                raise ExternalToolError(
                    f"{context} host ELF dynamic path escapes its artifact: "
                    f"{path}: {entry}"
                ) from error
            search_directories.append(destination)
        for needed in metadata.needed:
            if _HOST_SYSTEM_LIBRARIES.fullmatch(needed):
                continue
            if not any(
                (directory / needed).is_file() for directory in search_directories
            ):
                raise ExternalToolError(
                    f"{context} host dependency is not in its relative "
                    f"dynamic-path closure: {path}: {needed}"
                )
        for need in metadata.version_needs:
            if need.name == "GLIBC_PRIVATE":
                raise ExternalToolError(f"{context} requires GLIBC_PRIVATE: {path}")
            if need.name == "GLIBC_ABI_DT_RELR":
                continue
            if not need.name.startswith("GLIBC_"):
                continue
            value = need.name.removeprefix("GLIBC_")
            parsed = AbiVersion.parse(value)
            if parsed > floor:
                raise ExternalToolError(
                    f"{context} host ELF {path} requires {need.name}, above "
                    f"host floor GLIBC_{glibc_floor}"
                )
            if max_version is None or parsed > AbiVersion.parse(max_version):
                max_version = value
    if audited == 0:
        raise ExternalToolError(f"{context} contains no {arch} host ELF")
    return {
        "audited_elf_files": audited,
        "max_required_glibc": max_version,
    }
