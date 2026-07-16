from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from linux_toolchain.compiler.managed import (
    COMPILER_KIT_MANIFEST_FORMAT,
    COMPILER_KIT_MANIFEST_SCHEMA,
    TARGET_TOOL_NAMES,
    CompilerKitManifest,
    load_compiler_kit,
)
from linux_toolchain.elf.compatibility import validate_dt_relr_compatibility
from linux_toolchain.elf.reader import ReadElfInspector, is_elf
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.licenses import (
    license_evidence,
    managed_required_license_paths,
    require_license_files,
    validate_ubuntu_package_licenses,
)
from linux_toolchain.managed.contracts import (
    MANAGED_ARTIFACT_FORMAT,
    MANAGED_ARTIFACT_SCHEMA,
)
from linux_toolchain.managed.identity import managed_action_sha256
from linux_toolchain.managed.selection import ManagedBuildSelection
from linux_toolchain.versions import AbiVersion, major_version

_HOST_SYSTEM_LIBRARIES = re.compile(
    r"^(?:ld-linux[^/]*|libc|libm|libdl|libpthread|librt|libutil|libresolv)"
    r"\.so(?:\..*)?$"
)


def _validate_artifact_symlinks(root: Path) -> None:
    canonical = root.resolve()
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        target = Path(os.readlink(path))
        if target.is_absolute():
            raise ConfigurationError(
                f"managed artifact contains an absolute symlink: {path} -> {target}"
            )
        try:
            path.resolve(strict=True).relative_to(canonical)
        except (OSError, RuntimeError, ValueError) as error:
            raise ConfigurationError(
                f"managed artifact symlink escapes or dangles: {path} -> {target}"
            ) from error


def _host_elf_audit(root: Path, *, arch: str, glibc_floor: str) -> dict[str, object]:
    floor = AbiVersion.parse(glibc_floor)
    inspector = ReadElfInspector()
    audited = 0
    max_version: str | None = None
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file() or not is_elf(path):
            continue
        metadata = inspector.inspect(path)
        if metadata.machine != arch:
            raise ExternalToolError(
                f"managed compiler kit contains {metadata.machine} host ELF "
                f"for {arch}: {path}"
            )
        validate_dt_relr_compatibility(path, metadata, floor)
        audited += 1
        search_directories: list[Path] = []
        for entry in (*metadata.rpath, *metadata.runpath):
            if not entry or (entry != "$ORIGIN" and not entry.startswith("$ORIGIN/")):
                raise ExternalToolError(
                    "managed host ELF has a non-relocatable dynamic path: "
                    f"{path}: {entry}"
                )
            suffix = entry.removeprefix("$ORIGIN").removeprefix("/")
            if "$" in suffix:
                raise ExternalToolError(
                    f"managed host ELF has an unsupported dynamic token: "
                    f"{path}: {entry}"
                )
            try:
                destination = (path.parent / suffix).resolve(strict=True)
                destination.relative_to(root.resolve(strict=True))
            except (OSError, RuntimeError, ValueError) as error:
                raise ExternalToolError(
                    f"managed host ELF dynamic path escapes its compiler kit: "
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
                    "managed compiler host dependency is not in its relative "
                    f"dynamic-path closure: {path}: {needed}"
                )
        for need in metadata.version_needs:
            if need.name == "GLIBC_PRIVATE":
                raise ExternalToolError(
                    f"managed host ELF requires GLIBC_PRIVATE: {path}"
                )
            if need.name == "GLIBC_ABI_DT_RELR":
                continue
            if not need.name.startswith("GLIBC_"):
                continue
            value = need.name.removeprefix("GLIBC_")
            parsed = AbiVersion.parse(value)
            if parsed > floor:
                raise ExternalToolError(
                    f"managed compiler host ELF {path} requires {need.name}, "
                    f"above host floor GLIBC_{glibc_floor}"
                )
            if max_version is None or parsed > AbiVersion.parse(max_version):
                max_version = value
    if audited == 0:
        raise ExternalToolError(f"managed compiler kit contains no {arch} host ELF")
    return {
        "audited_elf_files": audited,
        "max_required_glibc": max_version,
    }


def _target_runtime_elf_audit(
    root: Path, *, arch: str, glibc_floor: str
) -> dict[str, object]:
    floor = AbiVersion.parse(glibc_floor)
    inspector = ReadElfInspector()
    audited = 0
    shared = 0
    max_version: str | None = None
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file() or not is_elf(path):
            continue
        metadata = inspector.inspect(path)
        if metadata.machine != arch:
            raise ExternalToolError(
                f"managed runtime contains {metadata.machine} ELF for {arch}: {path}"
            )
        audited += 1
        if metadata.elf_type == "DYN":
            shared += 1
        if metadata.rpath or metadata.runpath:
            raise ExternalToolError(
                f"managed target runtime must not carry RPATH or RUNPATH: {path}"
            )
        validate_dt_relr_compatibility(path, metadata, floor)
        for need in metadata.version_needs:
            if need.name == "GLIBC_PRIVATE":
                raise ExternalToolError(
                    f"managed target runtime requires GLIBC_PRIVATE: {path}"
                )
            if need.name == "GLIBC_ABI_DT_RELR":
                continue
            if not need.name.startswith("GLIBC_"):
                continue
            value = need.name.removeprefix("GLIBC_")
            parsed = AbiVersion.parse(value)
            if parsed > floor:
                raise ExternalToolError(
                    f"managed target runtime {path} requires {need.name}, above "
                    f"target floor GLIBC_{glibc_floor}"
                )
            if max_version is None or parsed > AbiVersion.parse(max_version):
                max_version = value
    if audited == 0 or shared == 0:
        raise ExternalToolError(
            "managed runtime contains no auditable target shared-library ELF"
        )
    return {
        "audited_elf_files": audited,
        "audited_shared_libraries": shared,
        "max_required_glibc": max_version,
    }


def _compiler_kit_locations(
    payload: Path,
    selection: ManagedBuildSelection,
    triplet: str,
) -> dict[str, object]:
    if selection.family == "gcc":
        cc = f"compiler/bin/{triplet}-gcc"
        cxx = f"compiler/bin/{triplet}-g++"
    else:
        cc = "compiler/bin/clang"
        cxx = "compiler/bin/clang++"
    tools = {name: f"compiler/bin/{triplet}-{name}" for name in TARGET_TOOL_NAMES}
    for context, relative in (
        ("C driver", cc),
        ("C++ driver", cxx),
        *((f"target tool {name}", relative) for name, relative in tools.items()),
    ):
        candidate = payload.parent / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(payload.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as error:
            raise ExternalToolError(
                f"managed compiler kit {context} escapes or is missing: {candidate}"
            ) from error
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ExternalToolError(
                f"managed compiler kit {context} is not executable: {candidate}"
            )
        if not is_elf(resolved):
            raise ExternalToolError(
                f"managed compiler kit {context} is not an ELF executable: {candidate}"
            )
    return {"cc": cc, "cxx": cxx, "target_tools": tools}


def _write_compiler_kit_manifest(
    artifacts: Path,
    payload: Path,
    selection: ManagedBuildSelection,
    *,
    triplet: str,
) -> Path:
    if selection.host is None:
        raise ConfigurationError("managed Compiler Kit has no host selection")
    locations = _compiler_kit_locations(payload, selection, triplet)
    value = {
        "schema": COMPILER_KIT_MANIFEST_SCHEMA,
        "format": COMPILER_KIT_MANIFEST_FORMAT,
        "provider": {
            "name": selection.family,
            "version": selection.version,
            "major": major_version(selection.version),
        },
        "host": selection.host.to_dict(),
        "target": {"arch": selection.target_arch, "triplet": triplet},
        "locations": locations,
    }
    # Parse through the public model before publication. This keeps the
    # builder and the managed-binding loader on one strict schema.
    manifest = CompilerKitManifest.from_dict(value)
    manifest_path = artifacts / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _validate_payload_separation(
    artifacts: Path, selection: ManagedBuildSelection
) -> Path:
    expected = artifacts / selection.payload_name
    unexpected = artifacts / (
        "runtime" if selection.payload_name == "compiler" else "compiler"
    )
    if not expected.is_dir() or expected.is_symlink():
        raise ExternalToolError(f"managed build did not produce {expected}")
    if unexpected.exists() or unexpected.is_symlink():
        raise ExternalToolError(
            f"managed build mixed compiler and runtime payloads: {unexpected}"
        )
    if selection.artifact_kind == "runtime":
        for forbidden in (expected / "bin", expected / "libexec"):
            if forbidden.exists():
                raise ExternalToolError(
                    f"managed runtime contains compiler payload: {forbidden}"
                )
    return expected


def _managed_license_evidence(
    artifacts: Path,
    selection: ManagedBuildSelection,
) -> dict[str, object]:
    compiler_kit = selection.artifact_kind == "compiler-kit"
    require_license_files(
        artifacts,
        managed_required_license_paths(
            selection.family,
            compiler_kit=compiler_kit,
        ),
        context="managed artifact",
    )
    if selection.artifact_kind == "compiler-kit":
        validate_ubuntu_package_licenses(
            artifacts,
            artifacts / "compiler",
            context="managed compiler kit",
        )

    return license_evidence(artifacts, context="managed artifact")


def _source_provenance(selection: ManagedBuildSelection) -> dict[str, str]:
    """Record where pinned source content was acquired, not its identity."""

    return {"url": selection.source.url}


def finalize_artifact(
    artifacts: Path,
    selection: ManagedBuildSelection,
    *,
    manifest: Mapping[str, object],
    image_provenance: Mapping[str, object],
    execution_script: Mapping[str, object] | None = None,
) -> Path:
    payload = _validate_payload_separation(artifacts, selection)
    _validate_artifact_symlinks(payload)
    host_audit: dict[str, object] | None = None
    target_audit: dict[str, object] | None = None
    if selection.artifact_kind == "compiler-kit":
        if selection.host is None:
            raise ConfigurationError("managed Compiler Kit has no host selection")
        host_audit = _host_elf_audit(
            payload,
            arch=selection.host.arch,
            glibc_floor=selection.host.glibc_floor,
        )
    else:
        if selection.target_glibc_floor is None:
            raise ConfigurationError("managed runtime has no target glibc floor")
        target_audit = _target_runtime_elf_audit(
            payload,
            arch=selection.target_arch,
            glibc_floor=selection.target_glibc_floor,
        )
    licenses = _managed_license_evidence(artifacts, selection)
    sdk_value = manifest.get("sdk")
    if not isinstance(sdk_value, Mapping):
        raise ConfigurationError("managed workspace SDK evidence is missing")
    triplet = sdk_value.get("triplet")
    if not isinstance(triplet, str):
        raise ConfigurationError("managed workspace SDK triplet evidence is missing")
    if selection.artifact_kind == "compiler-kit":
        _write_compiler_kit_manifest(
            artifacts,
            payload,
            selection,
            triplet=triplet,
        )
    tools_value = manifest.get("target_tools")
    if not isinstance(tools_value, Mapping):
        raise ConfigurationError("managed workspace target-tool evidence is missing")
    backend_value = manifest.get("compiler_backend")
    if not isinstance(backend_value, Mapping):
        raise ConfigurationError(
            "managed workspace compiler backend evidence is missing"
        )
    action = manifest.get("build_input")
    if not isinstance(action, Mapping):
        raise ConfigurationError("managed workspace build action is missing")
    artifact_manifest: dict[str, object] = {
        "schema": MANAGED_ARTIFACT_SCHEMA,
        "format": MANAGED_ARTIFACT_FORMAT,
        "action": dict(action),
        "action_sha256": managed_action_sha256(action),
        "provenance": {
            "source": _source_provenance(selection),
            "builder_image": dict(image_provenance),
            "execution_script": (
                dict(execution_script)
                if execution_script is not None
                else manifest.get("build_script")
            ),
        },
        "licenses": licenses,
        "elf_audit": host_audit if host_audit is not None else target_audit,
    }
    manifest_path = artifacts / "artifact.json"
    manifest_path.write_text(
        json.dumps(artifact_manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if selection.artifact_kind == "compiler-kit":
        load_compiler_kit(artifacts, check_host=False)
    return manifest_path
