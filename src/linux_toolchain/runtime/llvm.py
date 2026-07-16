from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from linux_toolchain.elf.compatibility import validate_dt_relr_compatibility
from linux_toolchain.elf.models import ElfMetadata
from linux_toolchain.elf.reader import ReadElfInspector, is_elf
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.licenses import (
    managed_required_license_paths,
    publish_license_directory,
    require_license_files,
    validate_license_manifest,
)
from linux_toolchain.models import classify_linux_glibc_target
from linux_toolchain.process import run
from linux_toolchain.publication import replace_directory
from linux_toolchain.runtime._import_common import (
    _OWNER_MARKER_CONTENT,
    _check_output,
    _inspect_relocatable_archive,
    _resolve_import_paths,
    _validate_relocatable_elf,
    _validate_symlinks,
    _version_symbol_report,
)
from linux_toolchain.runtime.llvm_models import (
    LLVM_RUNTIME_COMPONENTS,
    LLVM_RUNTIME_FORBIDDEN_SONAMES,
    LLVM_RUNTIME_MANIFEST_FORMAT,
    LLVM_RUNTIME_MANIFEST_SCHEMA,
    LlvmRuntimeManifest,
    LlvmRuntimeSourceEvidence,
    llvm_runtime_component,
    load_llvm_runtime_manifest,
)
from linux_toolchain.versions import AbiVersion

_FORBIDDEN_PAYLOAD_NAMES = {
    "c++",
    "cc",
    "clang",
    "clang++",
    "gcc",
    "g++",
    "ld.lld",
    "lld",
}
_RUNTIME_LIBRARY_NAME = re.compile(
    r"^(?:libc\+\+|libc\+\+abi|libunwind)(?:\.a|\.so(?:\.[0-9]+)*)$"
)
_BUILTINS_NAME = re.compile(r"^libclang_rt\.builtins(?:-(?:x86_64|aarch64))?\.a$")
_COMPILER_RT_CRT_NAME = re.compile(
    r"^clang_rt\.crt(begin|end)(?:-(x86_64|aarch64))?\.o$"
)
_CLANG_VERSION = re.compile(r"\bclang version ([0-9]+(?:\.[0-9]+)+)\b")
_LINKER_SCRIPT_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LIBCXX_LINKER_SCRIPT = re.compile(
    r"\s*INPUT\s*\(\s*"
    r"(libc\+\+\.so\.[0-9]+(?:\.[0-9]+)*)"
    r"\s+-lc\+\+abi\s+-lunwind\s*\)\s*"
)
_SYSTEM_NEEDED = re.compile(
    r"^(?:ld-linux[^/]*\.so\.[0-9]+|"
    r"lib(?:c|m|dl|pthread|rt|util|resolv)\.so\.[0-9]+)$"
)
_OWNER_MARKER = ".linux-toolchain-llvm-runtime"


def _relative_beneath(root: Path, path: Path, context: str) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ConfigurationError(
            f"{context} escapes its source prefix: {path}"
        ) from error
    if relative == Path(".") or ".." in relative.parts:
        raise ConfigurationError(f"{context} is not below its source prefix: {path}")
    return relative


def _copy_tree(root: Path, source: Path, destination: Path, context: str) -> None:
    _relative_beneath(root, source, context)
    if not source.is_dir():
        raise ConfigurationError(f"{context} is not a directory: {source}")
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def _entry_beneath(
    root: Path, relative: Path, context: str
) -> tuple[os.stat_result, str | None]:
    path = root / relative
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ConfigurationError(f"cannot inspect {context} {path}: {error}") from error
    target = os.readlink(path) if path.is_symlink() else None
    return metadata, target


def _copy_regular_beneath(
    root: Path, relative: Path, destination: Path, context: str
) -> None:
    source = root / relative
    if not source.is_file() or source.is_symlink():
        raise ConfigurationError(f"{context} is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _walk_regular_files(root: Path, start: Path, context: str) -> tuple[Path, ...]:
    directory = root / start
    if not directory.is_dir():
        raise ConfigurationError(f"{context} is not a directory: {directory}")
    return tuple(
        path.relative_to(root)
        for path in sorted(directory.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file() and not path.is_symlink()
    )


def _directory_entries_beneath(
    root: Path, relative: Path, context: str
) -> tuple[tuple[str, os.stat_result], ...]:
    directory = root / relative
    if not directory.is_dir():
        raise ConfigurationError(f"{context} is not a directory: {directory}")
    try:
        return tuple(
            (entry.name, entry.lstat())
            for entry in sorted(directory.iterdir(), key=lambda item: item.name)
        )
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect {context} {directory}: {error}"
        ) from error


def _manifest_root(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        return candidate
    if candidate.name != "manifest.json":
        raise ConfigurationError(
            "LLVM runtime input must be a runtime directory or a file named "
            "manifest.json"
        )
    return candidate.parent


def _validate_payload_filter(runtime: Path, forbidden_sonames: tuple[str, ...]) -> None:
    forbidden_names = set(forbidden_sonames)
    for path in runtime.rglob("*"):
        relative = path.relative_to(runtime)
        lowered_parts = tuple(part.lower() for part in relative.parts)
        name = path.name.lower()
        if "bin" in lowered_parts or "libexec" in lowered_parts:
            raise ConfigurationError(
                f"forbidden compiler payload in LLVM runtime: {relative}"
            )
        if (path.is_file() or path.is_symlink()) and (
            name in _FORBIDDEN_PAYLOAD_NAMES
            or path.name in forbidden_names
            or name.startswith(("libstdc++.so", "libgcc_s.so"))
        ):
            raise ConfigurationError(f"forbidden payload in LLVM runtime: {relative}")


def _validate_location_paths(root: Path, manifest: LlvmRuntimeManifest) -> None:
    for key in ("cxx_include_dirs", "resource_dir", "library_dirs"):
        raw = manifest.locations[key]
        values = (raw,) if isinstance(raw, str) else raw
        for relative in values:
            path = root / str(relative)
            if not path.is_dir() or path.is_symlink():
                raise ConfigurationError(
                    f"LLVM runtime manifest location is not a directory: {relative}"
                )
    for key in (
        "shared_libraries",
        "static_libraries",
        "builtins",
        "crt_objects",
    ):
        raw = manifest.locations[key]
        values = (raw,) if isinstance(raw, str) else raw
        for relative in values:
            if not (root / str(relative)).is_file():
                raise ConfigurationError(
                    f"LLVM runtime manifest location is not a file: {relative}"
                )
    include_dirs = manifest.locations["cxx_include_dirs"]
    resource_dir = manifest.locations["resource_dir"]
    assert isinstance(include_dirs, tuple)
    assert isinstance(resource_dir, str)
    if not any(
        all(
            (root / directory / header).is_file()
            for header in ("__config", "cstddef", "vector")
        )
        for directory in include_dirs
    ):
        raise ConfigurationError(
            "LLVM runtime libc++ headers are missing __config, cstddef, or vector"
        )
    if not any(
        (root / directory / "__config_site").is_file() for directory in include_dirs
    ):
        raise ConfigurationError(
            "LLVM runtime libc++ headers are missing __config_site"
        )
    if not (root / resource_dir / "include" / "stddef.h").is_file():
        raise ConfigurationError(
            "LLVM runtime Clang resource headers are missing stddef.h"
        )


def _inspect_builtins_archive(
    *,
    root: Path,
    relative: str,
    arch: str,
    inspector: ReadElfInspector,
) -> None:
    path = root / relative
    _inspect_relocatable_archive(
        path=path,
        arch=arch,
        inspector=inspector,
        description="compiler-rt builtins archive",
    )


def _inspect_static_libraries(
    *,
    root: Path,
    paths: tuple[str, ...],
    arch: str,
    inspector: ReadElfInspector,
) -> None:
    selected: dict[str, Path] = {}
    for relative in paths:
        path = root / relative
        component = llvm_runtime_component(path.name)
        if component is None or path.name != f"{component}.a":
            raise ExternalToolError(
                f"unexpected LLVM static runtime archive: {relative}"
            )
        previous = selected.setdefault(component, path)
        if previous != path:
            raise ExternalToolError(
                f"LLVM static runtime component {component} has multiple archives"
            )
    missing = sorted(set(LLVM_RUNTIME_COMPONENTS) - set(selected))
    if missing:
        raise ExternalToolError(
            "LLVM static runtime is missing archives: " + ", ".join(missing)
        )
    for component, path in sorted(selected.items()):
        _inspect_relocatable_archive(
            path=path,
            arch=arch,
            inspector=inspector,
            description=f"{component} static runtime archive",
        )


def _inspect_shared_libraries(
    *,
    root: Path,
    paths: tuple[str, ...],
    arch: str,
    glibc_floor: str,
    forbidden_sonames: tuple[str, ...],
    inspector: ReadElfInspector,
) -> tuple[dict[str, object], ...]:
    floor = AbiVersion.parse(glibc_floor)
    if not paths:
        return ()
    listed_names = {Path(relative).name for relative in paths}
    required_entrypoints = {"libc++.so", "libc++abi.so", "libunwind.so"}
    if not required_entrypoints.issubset(listed_names):
        missing = ", ".join(sorted(required_entrypoints - listed_names))
        raise ExternalToolError(
            f"LLVM shared runtime is missing linker entry points: {missing}"
        )
    forbidden = set(forbidden_sonames)
    canonical: dict[Path, Path] = {}
    libcxx_script: tuple[Path, str] | None = None
    for relative in paths:
        path = root / relative
        resolved = path.resolve(strict=True)
        resolved_metadata = resolved.stat()
        if not stat.S_ISREG(resolved_metadata.st_mode):
            raise ExternalToolError(
                f"LLVM shared runtime must resolve to a regular file: {path}"
            )
        if not is_elf(resolved):
            if path.name == "libc++.so" and not path.is_symlink():
                try:
                    if resolved.stat().st_size > 4096:
                        raise ConfigurationError(
                            "libc++ linker script exceeds the 4 KiB policy limit"
                        )
                    content = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as error:
                    raise ConfigurationError(
                        f"cannot read libc++ linker script {path}: {error}"
                    ) from error
                normalized = _LINKER_SCRIPT_COMMENT.sub(" ", content)
                match = _LIBCXX_LINKER_SCRIPT.fullmatch(normalized)
                if match is None:
                    raise ConfigurationError(
                        "libc++.so must be an ELF, a symlink to one, or the exact "
                        "LLVM INPUT(libc++.so.N -lc++abi -lunwind) linker script"
                    )
                libcxx_script = (path, match.group(1))
                continue
            raise ExternalToolError(
                f"LLVM shared runtime payload is not ELF: {relative} -> {resolved}"
            )
        canonical.setdefault(resolved, path)

    soname_owners: dict[str, Path] = {}
    component_owners: dict[str, Path] = {}
    inspected: list[tuple[Path, Path, ElfMetadata]] = []
    for resolved, display_path in sorted(
        canonical.items(), key=lambda item: item[1].as_posix()
    ):
        metadata = inspector.inspect(resolved)
        if metadata.machine != arch:
            raise ExternalToolError(
                f"{display_path} has machine {metadata.machine}, expected {arch}"
            )
        if metadata.elf_type != "DYN":
            raise ExternalToolError(
                f"{display_path} has ELF type {metadata.elf_type}, expected DYN"
            )
        if metadata.elf_class != "ELF64" or metadata.endianness != "little":
            raise ExternalToolError(
                f"{display_path} must be little-endian ELF64, got "
                f"{metadata.elf_class}/{metadata.endianness}"
            )
        soname = metadata.soname
        component = llvm_runtime_component(resolved.name)
        soname_component = llvm_runtime_component(soname or "")
        if (
            soname is None
            or "/" in soname
            or soname not in listed_names
            or component is None
            or soname_component != component
        ):
            raise ExternalToolError(
                f"{display_path} has missing, path-valued, unexported, or "
                "component-mismatched SONAME: "
                f"{soname!r}"
            )
        previous = soname_owners.setdefault(soname, resolved)
        if previous != resolved:
            raise ExternalToolError(
                f"LLVM runtime SONAME {soname!r} has multiple payload owners"
            )
        previous_component = component_owners.setdefault(component, resolved)
        if previous_component != resolved:
            raise ExternalToolError(
                f"LLVM runtime component {component} has multiple canonical owners"
            )
        validate_dt_relr_compatibility(display_path, metadata, floor)
        if metadata.rpath or metadata.runpath:
            raise ExternalToolError(f"{display_path} must not contain RPATH or RUNPATH")
        inspected.append((resolved, display_path, metadata))

    if set(component_owners) != set(LLVM_RUNTIME_COMPONENTS):
        missing = ", ".join(
            sorted(set(LLVM_RUNTIME_COMPONENTS) - set(component_owners))
        )
        raise ExternalToolError(
            f"LLVM shared runtime has no independent owner for: {missing}"
        )

    if libcxx_script is not None:
        script_path, soname = libcxx_script
        soname_path = script_path.with_name(soname)
        try:
            owner = soname_path.resolve(strict=True)
        except OSError as error:
            raise ConfigurationError(
                f"libc++ linker script references a missing SONAME entry: {soname}"
            ) from error
        if owner != component_owners["libc++"]:
            raise ConfigurationError(
                "libc++ linker script SONAME does not resolve to the exported "
                "libc++ shared library"
            )

    exported_sonames = set(soname_owners)
    reports: list[dict[str, object]] = []
    for _, display_path, metadata in inspected:
        for needed in metadata.needed:
            if "/" in needed:
                raise ExternalToolError(
                    f"{display_path} has path-valued DT_NEEDED entry: {needed}"
                )
            if needed in forbidden:
                raise ExternalToolError(
                    f"{display_path} depends on forbidden runtime {needed}"
                )
            if needed not in exported_sonames and not _SYSTEM_NEEDED.fullmatch(needed):
                raise ExternalToolError(
                    f"{display_path} depends on unexported runtime {needed}"
                )
        reports.append(
            _version_symbol_report(
                path=display_path,
                root=root,
                metadata=metadata,
                floor=floor,
            )
        )
    return tuple(sorted(reports, key=lambda report: str(report["path"])))


def validate_llvm_runtime_manifest(
    path: Path | str,
    manifest: LlvmRuntimeManifest | None = None,
    *,
    inspector: ReadElfInspector | None = None,
) -> LlvmRuntimeManifest:
    """Validate a libc++ runtime export without trusting recorded ELF evidence."""

    root = _manifest_root(path).resolve(strict=True)
    loaded = manifest or load_llvm_runtime_manifest(root)
    runtime = root / str(loaded.locations["runtime"])
    if not runtime.is_dir() or runtime.is_symlink():
        raise ConfigurationError(f"LLVM runtime payload is missing: {runtime}")
    _validate_symlinks(runtime, runtime_name="LLVM runtime")
    _validate_payload_filter(runtime, loaded.forbidden_sonames)
    _validate_location_paths(root, loaded)
    validate_license_manifest(root, context="LLVM runtime")
    require_license_files(
        root,
        managed_required_license_paths("clang", compiler_kit=False),
        context="LLVM runtime",
    )

    elf_inspector = inspector or ReadElfInspector()
    shared_paths = loaded.locations["shared_libraries"]
    static_paths = loaded.locations["static_libraries"]
    builtins = loaded.locations["builtins"]
    crt_objects = loaded.locations["crt_objects"]
    assert isinstance(shared_paths, tuple)
    assert isinstance(static_paths, tuple)
    assert isinstance(builtins, str)
    assert isinstance(crt_objects, tuple)
    _inspect_static_libraries(
        root=root,
        paths=static_paths,
        arch=loaded.arch,
        inspector=elf_inspector,
    )
    _inspect_builtins_archive(
        root=root,
        relative=builtins,
        arch=loaded.arch,
        inspector=elf_inspector,
    )
    for relative in crt_objects:
        path = root / relative
        _validate_relocatable_elf(
            path,
            elf_inspector.inspect(path),
            loaded.arch,
        )
    reports = _inspect_shared_libraries(
        root=root,
        paths=shared_paths,
        arch=loaded.arch,
        glibc_floor=loaded.glibc_floor,
        forbidden_sonames=loaded.forbidden_sonames,
        inspector=elf_inspector,
    )
    actual = LlvmRuntimeManifest.from_dict(
        {**loaded.to_dict(), "version_symbol_reports": list(reports)}
    )
    if (
        actual.to_dict()["version_symbol_reports"]
        != loaded.to_dict()["version_symbol_reports"]
    ):
        raise ConfigurationError(
            "LLVM runtime version symbol report does not match its ELF files"
        )
    return loaded


def _copy_library_chain(source: Path, *, prefix: Path, runtime: Path) -> None:
    pending = [source]
    copied: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in copied:
            continue
        relative = current
        if relative.is_absolute() or ".." in relative.parts:
            raise ExternalToolError(
                f"LLVM runtime library is not beneath its prefix: {relative}"
            )
        if not _RUNTIME_LIBRARY_NAME.fullmatch(current.name):
            raise ExternalToolError(
                f"unexpected LLVM runtime library payload: {prefix / current}"
            )
        destination = runtime / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata, target = _entry_beneath(prefix, relative, "LLVM runtime library")
        if stat.S_ISLNK(metadata.st_mode):
            assert target is not None
            target_path = PurePosixPath(target)
            if target_path.is_absolute():
                raise ExternalToolError(
                    f"LLVM runtime symlink must be relative: {prefix / relative}"
                )
            normalized = PurePosixPath(
                posixpath.normpath(
                    (PurePosixPath(relative.parent.as_posix()) / target_path).as_posix()
                )
            )
            if normalized.is_absolute() or ".." in normalized.parts:
                raise ExternalToolError(
                    "LLVM runtime symlink escapes its prefix: "
                    f"{prefix / relative} -> {target}"
                )
            target_relative = Path(*normalized.parts)
            component = llvm_runtime_component(relative.name)
            target_component = llvm_runtime_component(target_relative.name)
            if component is None or target_component != component:
                raise ExternalToolError(
                    "LLVM runtime symlink crosses runtime components: "
                    f"{prefix / relative} -> {target}"
                )
            if destination.is_symlink():
                if os.readlink(destination) != target:
                    raise ExternalToolError(
                        f"conflicting LLVM runtime symlink: {destination}"
                    )
            elif destination.exists():
                raise ExternalToolError(
                    f"conflicting LLVM runtime library destination: {destination}"
                )
            else:
                destination.symlink_to(target)
            pending.append(target_relative)
        elif stat.S_ISREG(metadata.st_mode):
            if destination.is_symlink():
                raise ExternalToolError(
                    f"conflicting LLVM runtime library destination: {destination}"
                )
            if not destination.exists():
                _copy_regular_beneath(
                    prefix,
                    relative,
                    destination,
                    "LLVM runtime library",
                )
        else:
            raise ExternalToolError(
                f"LLVM runtime library is not a regular file: {prefix / relative}"
            )
        copied.add(current)


def _select_resource_directory(prefix: Path, version: str) -> Path:
    requested = AbiVersion.parse(version)
    parsed: list[tuple[Path, AbiVersion]] = []
    for base in (Path("lib/clang"), Path("lib64/clang")):
        if not (prefix / base).is_dir():
            continue
        for name, metadata in _directory_entries_beneath(
            prefix, base, "Clang resource directory root"
        ):
            path = base / name
            if stat.S_ISLNK(metadata.st_mode):
                raise ConfigurationError(
                    f"Clang resource directory cannot be a symlink: {prefix / path}"
                )
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            try:
                parsed.append((path, AbiVersion.parse(name)))
            except ConfigurationError:
                continue
    exact = [path for path, candidate in parsed if candidate == requested]
    major = [
        path for path, candidate in parsed if candidate.parts[0] == requested.parts[0]
    ]
    selected = exact if exact else major
    if len(selected) != 1:
        choices = ", ".join(sorted(path.name for path, _ in parsed)) or "none"
        raise ConfigurationError(
            f"cannot uniquely select Clang {version} resource directory; "
            f"found {choices}"
        )
    resource = selected[0]
    if not (prefix / resource / "include").is_dir():
        raise ConfigurationError(
            f"Clang resource directory is missing builtin headers: {prefix / resource}"
        )
    return prefix / resource


def _select_builtins(prefix: Path, resource: Path, arch: str) -> Path:
    resource_relative = _relative_beneath(prefix, resource, "Clang resource directory")
    candidates = tuple(
        relative
        for relative in _walk_regular_files(
            prefix, resource_relative, "Clang resource directory"
        )
        if _BUILTINS_NAME.fullmatch(relative.name)
    )
    exact_suffixes = {
        "x86_64": ("-x86_64.a", "builtins.a"),
        "aarch64": ("-aarch64.a", "builtins.a"),
    }[arch]
    matching = tuple(path for path in candidates if path.name.endswith(exact_suffixes))
    if len(matching) != 1:
        choices = (
            ", ".join(
                sorted(
                    path.relative_to(resource_relative).as_posix()
                    for path in candidates
                )
            )
            or "none"
        )
        raise ConfigurationError(
            f"cannot uniquely select compiler-rt builtins for {arch}; found {choices}"
        )
    return prefix / matching[0]


def _select_compiler_rt_crt(
    prefix: Path, resource: Path, arch: str
) -> tuple[Path, Path]:
    resource_relative = _relative_beneath(prefix, resource, "Clang resource directory")
    selected: dict[str, list[Path]] = {"begin": [], "end": []}
    for relative in _walk_regular_files(
        prefix, resource_relative, "Clang resource directory"
    ):
        match = _COMPILER_RT_CRT_NAME.fullmatch(relative.name)
        if match is None:
            continue
        kind, suffix_arch = match.groups()
        if suffix_arch is None or suffix_arch == arch:
            selected[kind].append(relative)
    for kind, candidates in selected.items():
        if len(candidates) != 1:
            choices = ", ".join(path.as_posix() for path in candidates) or "none"
            raise ConfigurationError(
                f"cannot uniquely select compiler-rt crt{kind} for {arch}; "
                f"found {choices}"
            )
    return prefix / selected["begin"][0], prefix / selected["end"][0]


def _runtime_library_sources(prefix: Path, target: str) -> tuple[Path, ...]:
    roots = (
        Path("lib") / target,
        Path("lib64") / target,
        Path(target) / "lib",
        Path(target) / "lib64",
        Path("lib"),
        Path("lib64"),
    )
    selected: set[Path] = set()
    for root in roots:
        if not (prefix / root).is_dir():
            continue
        for name, metadata in _directory_entries_beneath(
            prefix, root, "LLVM runtime library root"
        ):
            path = root / name
            if _RUNTIME_LIBRARY_NAME.fullmatch(name):
                if not (
                    stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                ):
                    raise ConfigurationError(
                        f"LLVM runtime library is not a file: {prefix / path}"
                    )
                selected.add(path)
    return tuple(sorted(selected, key=lambda path: path.as_posix()))


def _clang_probe_evidence(
    probe_clang: Path | str,
    *,
    resource: Path,
    source: Path,
    version: str,
    target: str,
) -> LlvmRuntimeSourceEvidence:
    raw = Path(probe_clang).expanduser()
    if not raw.is_absolute():
        raise ConfigurationError("LLVM Clang probe path must be absolute")
    try:
        executable = raw.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(f"cannot access Clang probe {raw}: {error}") from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ConfigurationError(f"Clang probe is not executable: {raw}")
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    common = [executable, "--no-default-config"]
    version_result = run([*common, "--version"], env=environment)
    found_versions = set(_CLANG_VERSION.findall(version_result.stdout))
    if found_versions != {version}:
        found = ", ".join(sorted(found_versions)) or "none"
        raise ConfigurationError(
            f"Clang probe version mismatch: expected {version}, found {found}"
        )
    target_result = run(
        [*common, f"--target={target}", "-dumpmachine"], env=environment
    )
    if target_result.stdout.strip() != target or target_result.stderr.strip():
        raise ConfigurationError(
            "Clang probe did not report the exact requested target triplet"
        )
    resource_result = run(
        [*common, f"--target={target}", "-print-resource-dir"], env=environment
    )
    resource_text = resource_result.stdout.strip()
    if not resource_text or resource_result.stderr.strip():
        raise ConfigurationError("Clang probe did not report one resource directory")
    try:
        probed_resource = Path(resource_text).resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            "Clang probe resource directory is inaccessible"
        ) from error
    if probed_resource != resource.resolve(strict=True):
        raise ConfigurationError(
            "Clang probe resource directory does not match the runtime prefix"
        )
    resource_relative = _relative_beneath(
        source, resource, "Clang probe resource directory"
    )
    _walk_regular_files(source, resource_relative, "Clang probe resource directory")
    return LlvmRuntimeSourceEvidence.from_dict(
        {
            "kind": "clang-probe",
            "version": version,
            "target": target,
        }
    )


def _materialize_llvm_runtime(
    prefix: Path,
    llvm_version: str,
    glibc_floor: str,
    arch: str,
    target: str,
    staging: Path,
    *,
    licenses: Path | str | None = None,
    source_evidence: LlvmRuntimeSourceEvidence | None = None,
    probe_clang: Path | str | None = None,
) -> LlvmRuntimeManifest:
    """Write one complete LLVM runtime into caller-owned empty staging.

    This validates the selected payload.  A binding must still perform a real
    final shared and static links against the selected runtime.
    """

    parsed_version = AbiVersion.parse(llvm_version)
    floor = AbiVersion.parse(glibc_floor)
    if arch not in {"x86_64", "aarch64"}:
        raise ConfigurationError("LLVM runtime arch must be x86_64 or aarch64")
    target_arch = classify_linux_glibc_target(
        target,
        policy="strict",
        context="LLVM runtime target",
    )
    if target_arch != arch:
        raise ConfigurationError(
            f"LLVM runtime target {target!r} does not match architecture {arch}"
        )
    if (source_evidence is None) == (probe_clang is None):
        raise ConfigurationError(
            "select exactly one LLVM source proof: source_evidence or probe_clang"
        )
    if arch == "aarch64" and floor < AbiVersion.parse("2.17"):
        raise ConfigurationError("LLVM AArch64 runtimes require glibc 2.17 or newer")
    if staging.is_symlink() or not staging.is_dir() or next(staging.iterdir(), None):
        raise ConfigurationError("LLVM runtime staging must be an empty directory")
    source = prefix

    cxx_headers = source / "include" / "c++" / "v1"
    if not cxx_headers.is_dir():
        raise ConfigurationError(
            f"LLVM runtime prefix is missing libc++ headers: {cxx_headers}"
        )
    target_cxx_headers = source / "include" / target / "c++" / "v1"
    if target_cxx_headers.is_dir():
        if not (target_cxx_headers / "__config_site").is_file():
            raise ConfigurationError(
                "LLVM runtime target-specific libc++ headers are missing __config_site"
            )
    elif not (cxx_headers / "__config_site").is_file():
        raise ConfigurationError(
            "LLVM runtime libc++ headers are missing __config_site"
        )
    resource = _select_resource_directory(source, llvm_version)
    builtins = _select_builtins(source, resource, arch)
    compiler_rt_crt = _select_compiler_rt_crt(source, resource, arch)
    library_sources = _runtime_library_sources(source, target)
    if not library_sources:
        raise ConfigurationError(
            f"LLVM runtime prefix contains no libc++ runtime libraries: {source}"
        )
    canonical_version = str(parsed_version)
    if source_evidence is None:
        assert probe_clang is not None
        source_evidence = _clang_probe_evidence(
            probe_clang,
            resource=resource,
            source=source,
            version=canonical_version,
            target=target,
        )

    runtime = staging / "runtime"
    runtime.mkdir()
    copied_headers = runtime / cxx_headers.relative_to(source)
    _copy_tree(source, cxx_headers, copied_headers, "libc++ headers")
    copied_cxx_headers = [copied_headers]
    if target_cxx_headers.is_dir():
        copied_target_headers = runtime / target_cxx_headers.relative_to(source)
        _copy_tree(
            source,
            target_cxx_headers,
            copied_target_headers,
            "target-specific libc++ headers",
        )
        copied_cxx_headers.append(copied_target_headers)

    copied_resource = runtime / resource.relative_to(source)
    _copy_tree(
        source,
        resource / "include",
        copied_resource / "include",
        "Clang resource headers",
    )
    copied_builtins = runtime / builtins.relative_to(source)
    _copy_regular_beneath(
        source,
        builtins.relative_to(source),
        copied_builtins,
        "compiler-rt builtins",
    )
    copied_crt: list[Path] = []
    for crt_object in compiler_rt_crt:
        copied = runtime / crt_object.relative_to(source)
        _copy_regular_beneath(
            source,
            crt_object.relative_to(source),
            copied,
            "compiler-rt CRT object",
        )
        copied_crt.append(copied)

    for library in library_sources:
        _copy_library_chain(library, prefix=source, runtime=runtime)
    selected_libraries = tuple(
        path
        for path in sorted(runtime.rglob("*"), key=lambda item: item.as_posix())
        if (path.is_file() or path.is_symlink())
        and _RUNTIME_LIBRARY_NAME.fullmatch(path.name)
    )
    shared = tuple(
        path.relative_to(staging).as_posix()
        for path in selected_libraries
        if ".so" in path.name
    )
    static = tuple(
        path.relative_to(staging).as_posix()
        for path in selected_libraries
        if path.name.endswith(".a")
    )
    if not shared:
        raise ConfigurationError("LLVM runtime prefix has no shared libraries")
    if not static:
        raise ConfigurationError("LLVM runtime prefix has no static libraries")
    library_dirs = tuple(
        sorted(
            {path.parent.relative_to(staging).as_posix() for path in selected_libraries}
        )
    )
    _validate_symlinks(runtime, runtime_name="LLVM runtime")
    _validate_payload_filter(runtime, LLVM_RUNTIME_FORBIDDEN_SONAMES)
    inspector = ReadElfInspector()
    reports = _inspect_shared_libraries(
        root=staging,
        paths=shared,
        arch=arch,
        glibc_floor=str(floor),
        forbidden_sonames=LLVM_RUNTIME_FORBIDDEN_SONAMES,
        inspector=inspector,
    )
    _inspect_builtins_archive(
        root=staging,
        relative=copied_builtins.relative_to(staging).as_posix(),
        arch=arch,
        inspector=inspector,
    )
    for crt_object in copied_crt:
        _validate_relocatable_elf(
            crt_object,
            inspector.inspect(crt_object),
            arch,
        )
    publish_license_directory(
        source if licenses is None else licenses,
        staging,
        managed_required_license_paths("clang", compiler_kit=False),
        context="LLVM runtime",
    )
    manifest = LlvmRuntimeManifest.from_dict(
        {
            "schema": LLVM_RUNTIME_MANIFEST_SCHEMA,
            "format": LLVM_RUNTIME_MANIFEST_FORMAT,
            "provider": {
                "name": "llvm",
                "version": str(parsed_version),
                "major": parsed_version.parts[0],
            },
            "arch": arch,
            "target": target,
            "glibc_floor": str(floor),
            "source": source_evidence.to_dict(),
            "abi": {
                "standard_library": "libc++",
                "cxxabi": "libc++abi",
                "unwind": "libunwind",
                "rtlib": "compiler-rt",
                "linkage": "both",
            },
            "locations": {
                "runtime": "runtime",
                "cxx_include_dirs": sorted(
                    path.relative_to(staging).as_posix() for path in copied_cxx_headers
                ),
                "resource_dir": copied_resource.relative_to(staging).as_posix(),
                "library_dirs": list(library_dirs),
                "shared_libraries": list(shared),
                "static_libraries": list(static),
                "builtins": copied_builtins.relative_to(staging).as_posix(),
                "crt_objects": [
                    path.relative_to(staging).as_posix() for path in copied_crt
                ],
            },
            "forbidden_sonames": list(LLVM_RUNTIME_FORBIDDEN_SONAMES),
            "version_symbol_reports": list(reports),
            "validation": {
                "payload": "passed",
                "final_link": "binding-required",
            },
        }
    )
    (staging / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (staging / _OWNER_MARKER).write_text(_OWNER_MARKER_CONTENT, encoding="utf-8")
    return manifest


def import_llvm_runtime(
    prefix: Path | str,
    llvm_version: str,
    glibc_floor: str,
    arch: str,
    target: str,
    output: Path | str,
    *,
    licenses: Path | str | None = None,
    source_evidence: LlvmRuntimeSourceEvidence | None = None,
    probe_clang: Path | str | None = None,
    force: bool = False,
) -> Path:
    """Filter a proven LLVM runtime prefix into a relocatable export."""

    source, destination = _resolve_import_paths(
        prefix,
        output,
        prefix_context="LLVM runtime prefix",
        output_context="LLVM runtime output",
        reject_prefix_symlink=True,
    )
    _check_output(
        destination,
        force=force,
        output_context="LLVM runtime output",
        owner_description="a generator-owned LLVM runtime",
        owner_marker=_OWNER_MARKER,
        load_manifest=load_llvm_runtime_manifest,
    )
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
    )
    try:
        expected = _materialize_llvm_runtime(
            source,
            llvm_version,
            glibc_floor,
            arch,
            target,
            temporary,
            licenses=licenses,
            source_evidence=source_evidence,
            probe_clang=probe_clang,
        )

        def validate_final(published: Path) -> None:
            loaded = load_llvm_runtime_manifest(published)
            if loaded.to_dict() != expected.to_dict():
                raise ConfigurationError("published LLVM runtime manifest changed")
            validate_llvm_runtime_manifest(published, loaded)

        replace_directory(temporary, destination, validate=validate_final)
        return destination / "manifest.json"
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
