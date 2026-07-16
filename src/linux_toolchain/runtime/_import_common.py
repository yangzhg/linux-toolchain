from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from linux_toolchain.elf.models import VERSION_NAMESPACES, ElfMetadata
from linux_toolchain.elf.reader import ReadElfInspector
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.versions import AbiVersion

_OWNER_MARKER_CONTENT = "format=1\n"
_ARCHIVE_MAGIC = b"!<arch>\n"
_THIN_ARCHIVE_MAGIC = b"!<thin>\n"
_VERSIONED_SYMBOL = re.compile(r"^(GLIBC|GLIBCXX|CXXABI|GCC)_([0-9]+(?:\.[0-9]+)*)$")


def _resolve_import_paths(
    prefix: Path | str,
    output: Path | str,
    *,
    prefix_context: str,
    output_context: str,
    reject_prefix_symlink: bool,
) -> tuple[Path, Path]:
    raw_prefix = Path(prefix).expanduser()
    if reject_prefix_symlink and raw_prefix.is_symlink():
        raise ConfigurationError(f"{prefix_context} cannot be a symlink: {raw_prefix}")
    try:
        resolved_prefix = raw_prefix.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot access {prefix_context} {prefix}: {error}"
        ) from error
    if not resolved_prefix.is_dir():
        raise ConfigurationError(
            f"{prefix_context} is not a directory: {resolved_prefix}"
        )

    raw_output = Path(output).expanduser()
    if raw_output.is_symlink():
        raise ConfigurationError(f"{output_context} cannot be a symlink: {raw_output}")
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output = raw_output.parent.resolve(strict=True) / raw_output.name
    if resolved_output in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid {output_context} path: {resolved_output}")
    for outer, inner, message in (
        (resolved_prefix, resolved_output, "cannot be inside its source prefix"),
        (resolved_output, resolved_prefix, "cannot contain its source prefix"),
    ):
        try:
            inner.relative_to(outer)
        except ValueError:
            continue
        raise ConfigurationError(f"{output_context} {message}")
    return resolved_prefix, resolved_output


def _check_output(
    output: Path,
    *,
    force: bool,
    output_context: str,
    owner_description: str,
    owner_marker: str,
    load_manifest: Callable[[Path], object],
) -> None:
    if output in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid {output_context} path: {output}")
    if not output.exists():
        return
    if not output.is_dir():
        raise ConfigurationError(f"{output_context} is not a directory: {output}")
    try:
        nonempty = next(output.iterdir(), None) is not None
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect {output_context} {output}: {error}"
        ) from error
    if not nonempty:
        return
    if not force:
        raise ConfigurationError(
            f"{output_context} is non-empty: {output}; pass --force only for "
            f"{owner_description}"
        )
    try:
        marker = output / owner_marker
        owned = (
            marker.is_file()
            and not marker.is_symlink()
            and marker.read_text(encoding="utf-8") == _OWNER_MARKER_CONTENT
        )
        manifest = load_manifest(output)
    except (OSError, ConfigurationError):
        owned = False
        manifest = None
    if not owned or manifest is None:
        raise ConfigurationError(
            f"refusing to replace unowned {output_context}: {output}"
        )


def _validate_symlinks(runtime: Path, *, runtime_name: str) -> None:
    root = runtime.resolve(strict=True)
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = Path(os.readlink(path))
        if target.is_absolute():
            raise ExternalToolError(
                f"{runtime_name} contains an absolute symlink: {path} -> {target}"
            )
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, RuntimeError, ValueError) as error:
            raise ExternalToolError(
                f"{runtime_name} symlink escapes the runtime or is dangling: "
                f"{path} -> {target}"
            ) from error


def _version_symbol_report(
    *, path: Path, root: Path, metadata: ElfMetadata, floor: AbiVersion
) -> dict[str, object]:
    required: dict[str, set[str]] = {
        namespace: set() for namespace in VERSION_NAMESPACES
    }
    for need in metadata.version_needs:
        if need.name == "GLIBC_PRIVATE":
            raise ExternalToolError(f"{path} requires forbidden GLIBC_PRIVATE")
        if (
            need.name.startswith("GLIBC_")
            and need.name != "GLIBC_ABI_DT_RELR"
            and not _VERSIONED_SYMBOL.fullmatch(need.name)
        ):
            raise ExternalToolError(
                f"{path} requires unknown glibc symbol version {need.name}"
            )
        match = _VERSIONED_SYMBOL.fullmatch(need.name)
        if match is None:
            continue
        namespace, version = match.groups()
        required[namespace].add(version)
        if namespace == "GLIBC" and AbiVersion.parse(version) > floor:
            raise ExternalToolError(
                f"{path} requires GLIBC_{version}, above configured floor {floor}"
            )
    normalized = {
        namespace: sorted(versions, key=AbiVersion.parse)
        for namespace, versions in required.items()
    }
    return {
        "path": path.relative_to(root).as_posix(),
        "machine": metadata.machine,
        "elf_class": metadata.elf_class,
        "endianness": metadata.endianness,
        "required_versions": normalized,
        "max_required_versions": {
            namespace: versions[-1] if versions else None
            for namespace, versions in normalized.items()
        },
    }


def _validate_relocatable_elf(path: Path, metadata: ElfMetadata, arch: str) -> None:
    if metadata.machine != arch:
        raise ExternalToolError(
            f"{path} has machine {metadata.machine}, expected {arch}"
        )
    if metadata.elf_type != "REL":
        raise ExternalToolError(
            f"{path} has ELF type {metadata.elf_type}, expected REL"
        )
    if metadata.elf_class != "ELF64" or metadata.endianness != "little":
        raise ExternalToolError(
            f"{path} must be little-endian ELF64, got "
            f"{metadata.elf_class}/{metadata.endianness}"
        )


def _inspect_relocatable_archive(
    *,
    path: Path,
    arch: str,
    inspector: ReadElfInspector,
    description: str,
) -> None:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        with resolved.open("rb") as stream:
            magic = stream.read(len(_ARCHIVE_MAGIC))
    except OSError as error:
        raise ExternalToolError(
            f"cannot inspect {description} {path}: {error}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ExternalToolError(f"{description} must be a regular file: {path}")
    if magic == _THIN_ARCHIVE_MAGIC:
        raise ExternalToolError(
            f"{description} must be self-contained, not thin: {path}"
        )
    if magic != _ARCHIVE_MAGIC:
        raise ExternalToolError(f"{description} is not a regular ar archive: {path}")
    members = inspector.inspect_archive(resolved)
    if not members:
        raise ExternalToolError(f"{description} has no ELF members: {path}")
    for member in members:
        _validate_relocatable_elf(member.path, member, arch)
