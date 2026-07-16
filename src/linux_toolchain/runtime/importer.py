from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

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
from linux_toolchain.runtime.models import (
    RUNTIME_MANIFEST_FORMAT,
    RUNTIME_MANIFEST_SCHEMA,
    GccRuntimeManifest,
    load_runtime_manifest,
)
from linux_toolchain.versions import AbiVersion, major_version

_OWNER_MARKER = ".linux-toolchain-runtime"
_LINKER_SCRIPT_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LIBRARY_NAME = re.compile(
    r"^(?:(?:libstdc\+\+|libgcc_s|libatomic|libquadmath)"
    r"(?:\.so(?:\.[0-9]+)*|\.a)|libgcc_s_asneeded\.so|"
    r"libatomic_asneeded\.(?:so|a))$"
)
_LIBRARY_ENTRYPOINT = re.compile(
    r"^(?:(?:libstdc\+\+|libgcc_s|libatomic|libquadmath)"
    r"(?:\.so(?:\.[0-9]+)?|\.a)|libgcc_s_asneeded\.so|"
    r"libatomic_asneeded\.(?:so|a))$"
)
_SAFE_RUNTIME_LINKER_SCRIPTS = {
    "libgcc_s.so": (
        re.compile(r"\s*GROUP\s*\(\s*libgcc_s\.so\.1\s+-lgcc\s*\)\s*"),
        frozenset({"libgcc.a"}),
        frozenset({"libgcc_s.so.1"}),
    ),
    "libgcc_s_asneeded.so": (
        re.compile(r"\s*INPUT\s*\(\s*AS_NEEDED\s*\(\s*-lgcc_s\s*\)\s*\)\s*"),
        frozenset(),
        frozenset({"libgcc_s.so"}),
    ),
    "libatomic_asneeded.so": (
        re.compile(r"\s*INPUT\s*\(\s*AS_NEEDED\s*\(\s*-latomic\s*\)\s*\)\s*"),
        frozenset(),
        frozenset({"libatomic.so"}),
    ),
}
_FORBIDDEN_PAYLOAD_NAMES = {
    "cc",
    "c++",
    "cpp",
    "gcc",
    "g++",
    "collect2",
    "lto1",
}


@dataclass(frozen=True)
class _GccInstallation:
    version: str
    major: int
    target: str
    gcc_runtime_dir: Path


def _numeric_version_directories(prefix: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for root in (prefix / "lib" / "gcc", prefix / "lib64" / "gcc"):
        if not root.is_dir():
            continue
        for target in root.iterdir():
            if not target.is_dir():
                continue
            for version in target.iterdir():
                if not version.is_dir():
                    continue
                try:
                    version.resolve(strict=True).relative_to(prefix)
                except (OSError, RuntimeError, ValueError):
                    continue
                try:
                    AbiVersion.parse(version.name)
                except ConfigurationError:
                    continue
                result.append(version)
    return tuple(sorted(result, key=lambda path: path.as_posix()))


def _driver_candidates(prefix: Path, targets: tuple[str, ...]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for target in targets:
        candidates.extend(
            (
                prefix / "bin" / f"{target}-g++",
                prefix / target / "bin" / "g++",
            )
        )
    candidates.append(prefix / "bin" / "g++")
    result: list[Path] = []
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            try:
                resolved.relative_to(prefix)
            except ValueError:
                continue
            if resolved not in result:
                result.append(resolved)
    return tuple(result)


def _select_version_directory(
    candidates: tuple[Path, ...], target: str, version: str, major: int
) -> Path:
    matching = [path for path in candidates if path.parent.name == target]
    exact = [path for path in matching if path.name == version]
    if len(exact) == 1:
        return exact[0]
    major_matches = [
        path for path in matching if AbiVersion.parse(path.name).parts[0] == major
    ]
    if len(major_matches) == 1:
        return major_matches[0]
    if not matching:
        raise ConfigurationError(
            f"GCC prefix has no lib/gcc/{target}/<version> directory"
        )
    raise ConfigurationError(
        f"cannot uniquely match GCC {version} to lib/gcc/{target}: "
        + ", ".join(path.name for path in matching)
    )


def _resolve_probe_driver(probe_gxx: Path | str) -> Path:
    raw_driver = Path(probe_gxx).expanduser()
    if not raw_driver.is_absolute():
        raise ConfigurationError(
            f"GCC probe driver must be an absolute path: {raw_driver}"
        )
    try:
        driver = raw_driver.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"GCC probe driver does not resolve to an executable file: {raw_driver}"
        ) from error
    if not driver.is_file() or not os.access(driver, os.X_OK):
        raise ConfigurationError(
            f"GCC probe driver is not an executable file: {driver}"
        )
    return driver


def _probe_gcc(prefix: Path, probe_gxx: Path | str | None = None) -> _GccInstallation:
    version_directories = _numeric_version_directories(prefix)
    if not version_directories:
        raise ConfigurationError(
            f"GCC prefix has no lib/gcc/<target>/<version> installation: {prefix}"
        )
    targets = tuple(sorted({path.parent.name for path in version_directories}))
    failures: list[str] = []
    drivers = (
        (_resolve_probe_driver(probe_gxx),)
        if probe_gxx is not None
        else _driver_candidates(prefix, targets)
    )
    for driver in drivers:
        try:
            target = run([driver, "-dumpmachine"]).stdout.strip()
            classify_linux_glibc_target(
                target,
                policy="strict",
                context="GCC target",
            )
            version_output = run(
                [driver, "-dumpfullversion", "-dumpversion"]
            ).stdout.strip()
            version = version_output.splitlines()[0].strip()
            AbiVersion.parse(version)
            major = major_version(version)
            runtime_dir = _select_version_directory(
                version_directories, target, version, major
            )
            return _GccInstallation(
                version=version,
                major=major,
                target=target,
                gcc_runtime_dir=runtime_dir,
            )
        except (ConfigurationError, ExternalToolError, IndexError) as error:
            failures.append(f"{driver}: {error}")
    detail = "\n".join(failures) if failures else "no executable g++ driver found"
    raise ConfigurationError(f"cannot identify GCC installation in {prefix}:\n{detail}")


def _installation_from_metadata(
    prefix: Path,
    *,
    version: str,
    target: str,
) -> _GccInstallation:
    """Select a managed GCC runtime without executing a build-tree driver."""

    parsed_version = AbiVersion.parse(version)
    classify_linux_glibc_target(
        target,
        policy="strict",
        context="managed GCC target",
    )
    matches = tuple(
        path
        for path in _numeric_version_directories(prefix)
        if path.parent.name == target and path.name == version
    )
    if len(matches) != 1:
        found = (
            ", ".join(
                sorted(
                    f"{path.parent.name}/{path.name}"
                    for path in _numeric_version_directories(prefix)
                )
            )
            or "none"
        )
        raise ConfigurationError(
            f"managed GCC runtime has no unique lib/gcc/{target}/{version}; "
            f"found {found}"
        )
    return _GccInstallation(
        version=version,
        major=parsed_version.parts[0],
        target=target,
        gcc_runtime_dir=matches[0],
    )


def _relative_to_output(path: Path, output: Path) -> str:
    return path.relative_to(output).as_posix()


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        raise AssertionError("symlinks must be copied with _copy_library_chain")
    shutil.copy2(source, destination)


def _copy_library_chain(
    source: Path,
    *,
    prefix: Path,
    runtime: Path,
    copied: set[Path],
) -> None:
    pending = [source]
    while pending:
        current = pending.pop()
        lexical = current.absolute()
        if lexical in copied:
            continue
        try:
            relative = lexical.relative_to(prefix)
        except ValueError as error:
            raise ExternalToolError(
                f"GCC runtime library escapes its prefix: {current}"
            ) from error
        if not _LIBRARY_NAME.fullmatch(lexical.name):
            raise ExternalToolError(
                f"GCC runtime library symlink resolves to an unexpected payload: {current}"
            )
        destination = runtime / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if lexical.is_symlink():
            try:
                resolved = lexical.resolve(strict=True)
                resolved.relative_to(prefix)
            except (OSError, RuntimeError, ValueError) as error:
                raise ExternalToolError(
                    f"GCC runtime library symlink escapes its prefix or is dangling: "
                    f"{lexical} -> {os.readlink(lexical)}"
                ) from error
            if not _LIBRARY_NAME.fullmatch(resolved.name):
                raise ExternalToolError(
                    "GCC runtime library symlink resolves to a non-runtime file: "
                    f"{lexical} -> {resolved}"
                )
            target_destination = runtime / resolved.relative_to(prefix)
            relative_target = os.path.relpath(target_destination, destination.parent)
            if destination.exists() or destination.is_symlink():
                destination.unlink()
            destination.symlink_to(relative_target)
            pending.append(resolved)
        elif lexical.is_file():
            _copy_file(lexical, destination)
        else:
            raise ExternalToolError(f"GCC runtime library is not a file: {lexical}")
        copied.add(lexical)


def _validate_runtime_linker_script(
    path: Path,
    *,
    static_names: set[str],
    shared_names: set[str],
) -> bool:
    specification = _SAFE_RUNTIME_LINKER_SCRIPTS.get(path.name)
    if specification is None:
        return False
    pattern, required_static, required_shared = specification
    try:
        script = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise ExternalToolError(
            "GCC runtime linker input is neither ELF, an archive, nor a "
            f"supported linker script: {path}"
        ) from error
    normalized = _LINKER_SCRIPT_COMMENT.sub("", script)
    if (
        not pattern.fullmatch(normalized)
        or not required_static.issubset(static_names)
        or not required_shared.issubset(shared_names)
    ):
        raise ExternalToolError(
            f"GCC runtime linker input has an unsupported linker script: {path}"
        )
    return True


def _matching_version_directory(parent: Path, version: str, major: int) -> Path | None:
    if not parent.is_dir():
        return None
    candidates = [parent / version, parent / str(major)]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    compatible: list[Path] = []
    for candidate in parent.iterdir():
        if not candidate.is_dir():
            continue
        try:
            if AbiVersion.parse(candidate.name).parts[0] == major:
                compatible.append(candidate)
        except ConfigurationError:
            continue
    return compatible[0] if len(compatible) == 1 else None


def _copy_cxx_headers(
    prefix: Path,
    runtime: Path,
    installation: _GccInstallation,
) -> tuple[str, ...]:
    parents = (
        prefix / "include" / "c++",
        prefix / "include" / installation.target / "c++",
        prefix / installation.target / "include" / "c++",
    )
    copied: list[str] = []
    for parent in parents:
        source = _matching_version_directory(
            parent, installation.version, installation.major
        )
        if source is None:
            continue
        destination = runtime / source.relative_to(prefix)
        _copy_tree(source, destination)
        copied.append(_relative_to_output(destination, runtime.parent))
    if not copied:
        raise ConfigurationError(
            f"GCC prefix has no C++ headers for GCC {installation.version}"
        )
    if not any(
        candidate.is_file()
        for path in copied
        for candidate in (runtime.parent / path).rglob("c++config.h")
    ):
        # The generator needs the target-specific configuration headers, not
        # merely the target-independent standard library declarations.
        raise ConfigurationError("GCC C++ headers are missing bits/c++config.h")
    return tuple(sorted(set(copied)))


def _copy_gcc_runtime_dir(
    prefix: Path,
    runtime: Path,
    installation: _GccInstallation,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    source = installation.gcc_runtime_dir
    destination = runtime / source.relative_to(prefix)
    destination.mkdir(parents=True, exist_ok=True)
    if not (source / "include").is_dir():
        raise ConfigurationError("GCC runtime is missing its include directory")
    for directory_name in ("include", "include-fixed"):
        include = source / directory_name
        if include.is_dir():
            _copy_tree(include, destination / directory_name)
    missing_builtin_headers = tuple(
        name
        for name in ("stdarg.h", "stddef.h")
        if not (destination / "include" / name).is_file()
    )
    if missing_builtin_headers:
        raise ConfigurationError(
            "GCC runtime is missing compiler builtin headers: "
            + ", ".join(missing_builtin_headers)
        )
    # libquadmath is a GCC-owned runtime component, but a standalone or
    # version-specific installation may place its public headers at the
    # prefix include root rather than in lib/gcc/<target>/<version>/include.
    for header_name in ("quadmath.h", "quadmath_weak.h"):
        header = prefix / "include" / header_name
        if header.is_file() and not header.is_symlink():
            _copy_file(header, destination / "include" / header_name)
    for pattern in ("crtbegin*.o", "crtend*.o", "libgcc*.a"):
        for path in sorted(source.glob(pattern)):
            if path.is_file() and not path.is_symlink():
                _copy_file(path, destination / path.name)

    crt_objects = tuple(
        _relative_to_output(path, runtime.parent)
        for path in sorted(destination.glob("crtbegin*.o"))
        + sorted(destination.glob("crtend*.o"))
    )
    static_libraries = tuple(
        _relative_to_output(path, runtime.parent)
        for path in sorted(destination.glob("libgcc*.a"))
    )
    crt_names = {Path(path).name for path in crt_objects}
    if not any(name.startswith("crtbegin") for name in crt_names) or not any(
        name.startswith("crtend") for name in crt_names
    ):
        raise ConfigurationError("GCC runtime is missing crtbegin/crtend objects")
    if not any(Path(path).name == "libgcc.a" for path in static_libraries):
        raise ConfigurationError("GCC runtime is missing libgcc.a")
    return (
        _relative_to_output(destination, runtime.parent),
        crt_objects,
        static_libraries,
    )


def _copy_libraries(
    prefix: Path,
    runtime: Path,
    installation: _GccInstallation,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    sources: list[Path] = []
    # Deliberately enumerate only the selected GCC runtime directory, the
    # target's multiarch directories, and top-level lib directories.  A broad
    # recursive walk would accidentally import other GCC versions or x86
    # multilib (32/x32) payloads from a distribution prefix such as /usr.
    roots = (
        installation.gcc_runtime_dir,
        prefix / "lib",
        prefix / "lib64",
        prefix / "lib" / installation.target,
        prefix / "lib64" / installation.target,
        prefix / installation.target / "lib",
        prefix / installation.target / "lib64",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if (path.is_file() or path.is_symlink()) and _LIBRARY_ENTRYPOINT.fullmatch(
                path.name
            ):
                sources.append(path)
    copied: set[Path] = set()
    for source in sorted(set(sources), key=lambda path: path.as_posix()):
        _copy_library_chain(
            source,
            prefix=prefix,
            runtime=runtime,
            copied=copied,
        )

    selected = tuple(
        path
        for path in sorted(runtime.rglob("*"), key=lambda path: path.as_posix())
        if (path.is_file() or path.is_symlink()) and _LIBRARY_NAME.fullmatch(path.name)
    )
    shared = tuple(
        _relative_to_output(path, runtime.parent)
        for path in selected
        if ".so" in path.name
    )
    static = tuple(
        _relative_to_output(path, runtime.parent)
        for path in selected
        if path.name.endswith(".a")
    )
    library_dirs = tuple(
        sorted({_relative_to_output(path.parent, runtime.parent) for path in selected})
    )
    if not any(Path(path).name.startswith("libstdc++.so") for path in shared):
        raise ConfigurationError("GCC prefix is missing shared libstdc++")
    if not any(Path(path).name.startswith("libgcc_s.so") for path in shared):
        raise ConfigurationError("GCC prefix is missing shared libgcc_s")
    if not any(Path(path).name == "libstdc++.a" for path in static):
        raise ConfigurationError("GCC prefix is missing static libstdc++.a")
    return library_dirs, static, shared


def _require_managed_gcc_linker_inputs(
    installation: _GccInstallation,
    static_libraries: tuple[str, ...],
    shared_libraries: tuple[str, ...],
) -> None:
    if installation.major < 16:
        return

    static_names = {Path(relative).name for relative in static_libraries}
    shared_names = {Path(relative).name for relative in shared_libraries}
    required_static = {"libatomic.a", "libatomic_asneeded.a"}
    required_shared = {
        "libatomic.so",
        "libatomic_asneeded.so",
        "libgcc_s.so",
        "libgcc_s_asneeded.so",
    }
    missing = sorted(
        (required_static - static_names) | (required_shared - shared_names)
    )
    if missing:
        raise ConfigurationError(
            "managed GCC 16 or newer runtime is missing required linker inputs: "
            + ", ".join(missing)
        )


def _require_quadmath_inputs(
    root: Path,
    gcc_runtime_dir: str,
    static_libraries: tuple[str, ...],
    shared_libraries: tuple[str, ...],
) -> None:
    include = root / gcc_runtime_dir / "include"
    static_names = {Path(path).name for path in static_libraries}
    shared_names = {Path(path).name for path in shared_libraries}
    missing = [
        header
        for header in ("quadmath.h", "quadmath_weak.h")
        if not (include / header).is_file()
    ]
    if "libquadmath.a" not in static_names:
        missing.append("libquadmath.a")
    if "libquadmath.so" not in shared_names:
        missing.append("libquadmath.so")
    if missing:
        raise ConfigurationError(
            "managed GCC runtime is missing required libquadmath inputs: "
            + ", ".join(missing)
        )


def _validate_payload_filter(runtime: Path) -> None:
    for path in runtime.rglob("*"):
        relative = path.relative_to(runtime)
        lowered_parts = tuple(part.lower() for part in relative.parts)
        name = path.name.lower()
        if "bin" in lowered_parts or "plugin" in lowered_parts:
            raise ConfigurationError(
                f"forbidden compiler payload in runtime: {relative}"
            )
        if (path.is_file() or path.is_symlink()) and (
            name in _FORBIDDEN_PAYLOAD_NAMES or name.startswith("cc1")
        ):
            raise ConfigurationError(
                f"forbidden compiler executable in runtime: {relative}"
            )


def _validate_dynamic_runtime_metadata(
    path: Path, metadata: ElfMetadata, floor: AbiVersion
) -> None:
    validate_dt_relr_compatibility(path, metadata, floor)
    for tag, entries in (("RPATH", metadata.rpath), ("RUNPATH", metadata.runpath)):
        for entry in entries:
            if not entry or (entry != "$ORIGIN" and not entry.startswith("$ORIGIN/")):
                raise ExternalToolError(
                    f"{path} has non-relocatable {tag} entry: {entry!r}"
                )
    for needed in metadata.needed:
        if "/" in needed:
            raise ExternalToolError(f"{path} has path-valued DT_NEEDED entry: {needed}")


def _inspect_relocatable_inputs(
    *,
    root: Path,
    crt_paths: tuple[str, ...],
    static_paths: tuple[str, ...],
    arch: str,
    inspector: ReadElfInspector,
) -> None:
    for relative in crt_paths:
        path = root / relative
        _validate_relocatable_elf(path, inspector.inspect(path), arch)

    inspected_archives: set[Path] = set()
    static_names = {Path(relative).name for relative in static_paths}
    for relative in static_paths:
        path = root / relative
        resolved = path.resolve(strict=True)
        if resolved in inspected_archives:
            continue
        if _validate_runtime_linker_script(
            resolved,
            static_names=static_names,
            shared_names=set(),
        ):
            continue
        _inspect_relocatable_archive(
            path=path,
            arch=arch,
            inspector=inspector,
            description="GCC runtime archive",
        )
        inspected_archives.add(resolved)


def _inspect_shared_libraries(
    *,
    root: Path,
    shared_paths: tuple[str, ...],
    static_paths: tuple[str, ...],
    arch: str,
    glibc_floor: str,
    inspector: ReadElfInspector,
) -> tuple[dict[str, object], ...]:
    floor = AbiVersion.parse(glibc_floor)
    canonical: dict[Path, Path] = {}
    soname_owners: dict[str, Path] = {}
    shared_names = {Path(relative).name for relative in shared_paths}
    static_names = {Path(relative).name for relative in static_paths}
    for relative in shared_paths:
        path = root / relative
        if not path.is_file():
            raise ConfigurationError(f"runtime shared library is missing: {relative}")
        resolved = path.resolve(strict=True)
        if not is_elf(resolved):
            if not _validate_runtime_linker_script(
                resolved,
                static_names=static_names,
                shared_names=shared_names,
            ):
                raise ExternalToolError(
                    "GCC runtime shared library payload is not an ELF file and "
                    f"has an unsupported linker script: {relative}"
                )
            continue
        canonical.setdefault(resolved, resolved)
    reports: list[dict[str, object]] = []
    for path in sorted(canonical.values(), key=lambda item: item.as_posix()):
        metadata = inspector.inspect(path)
        if metadata.machine != arch:
            raise ExternalToolError(
                f"{path} has machine {metadata.machine}, expected {arch}"
            )
        if metadata.elf_type != "DYN":
            raise ExternalToolError(
                f"{path} has ELF type {metadata.elf_type}, expected DYN"
            )
        if metadata.elf_class != "ELF64" or metadata.endianness != "little":
            raise ExternalToolError(
                f"{path} must be little-endian ELF64, got "
                f"{metadata.elf_class}/{metadata.endianness}"
            )
        if (
            metadata.soname is None
            or "/" in metadata.soname
            or metadata.soname not in shared_names
            or metadata.soname.split(".so", 1)[0] != path.name.split(".so", 1)[0]
        ):
            raise ExternalToolError(
                f"{path} has missing, path-valued, or unexported DT_SONAME: "
                f"{metadata.soname!r}"
            )
        previous_owner = soname_owners.setdefault(metadata.soname, path)
        if previous_owner != path:
            raise ExternalToolError(
                "GCC runtime contains multiple canonical shared libraries for "
                f"DT_SONAME {metadata.soname}: {previous_owner}, {path}"
            )
        _validate_dynamic_runtime_metadata(path, metadata, floor)
        reports.append(
            _version_symbol_report(path=path, root=root, metadata=metadata, floor=floor)
        )
    if not reports:
        raise ExternalToolError("GCC runtime has no inspectable shared ELF libraries")
    names = {Path(str(report["path"])).name for report in reports}
    if not any(name.startswith("libstdc++.so") for name in names):
        raise ExternalToolError("GCC runtime has no inspectable shared libstdc++ ELF")
    if not any(name.startswith("libgcc_s.so") for name in names):
        raise ExternalToolError("GCC runtime has no inspectable shared libgcc_s ELF")
    return tuple(sorted(reports, key=lambda report: str(report["path"])))


def _manifest_root(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        return candidate
    if candidate.name != "manifest.json":
        raise ConfigurationError(
            "GCC runtime manifest input must be a runtime directory or a file "
            "named manifest.json"
        )
    return candidate.parent


def _validate_location_paths(root: Path, manifest: GccRuntimeManifest) -> None:
    directory_keys = ("cxx_include_dirs", "gcc_runtime_dir", "library_dirs")
    file_keys = (
        "crt_objects",
        "static_libraries",
        "shared_libraries",
    )
    for key in directory_keys:
        raw = manifest.locations[key]
        values = (raw,) if isinstance(raw, str) else raw
        for relative in values:
            if not (root / str(relative)).is_dir():
                raise ConfigurationError(
                    f"runtime manifest location is not a directory: {relative}"
                )
    for key in file_keys:
        raw = manifest.locations[key]
        assert not isinstance(raw, str)
        for relative in raw:
            if not (root / relative).is_file():
                raise ConfigurationError(
                    f"runtime manifest location is not a file: {relative}"
                )


def validate_runtime_manifest(
    path: Path | str,
    manifest: GccRuntimeManifest | None = None,
    *,
    inspector: ReadElfInspector | None = None,
) -> GccRuntimeManifest:
    """Validate a runtime tree and return its parsed manifest.

    Validation deliberately repeats ELF inspection instead of trusting the
    recorded symbol report.  Bindings can therefore treat this function as the
    boundary between imported content and compiler/linker configuration.
    """

    root = _manifest_root(path).resolve(strict=True)
    loaded = manifest or load_runtime_manifest(root)
    runtime = root / str(loaded.locations["runtime"])
    if not runtime.is_dir():
        raise ConfigurationError(f"GCC runtime payload is missing: {runtime}")
    _validate_symlinks(runtime, runtime_name="GCC runtime")
    _validate_payload_filter(runtime)
    _validate_location_paths(root, loaded)
    validate_license_manifest(root, context="GCC runtime")
    require_license_files(
        root,
        managed_required_license_paths("gcc", compiler_kit=False),
        context="GCC runtime",
    )
    elf_inspector = inspector or ReadElfInspector()
    crt_paths = loaded.locations["crt_objects"]
    static_paths = loaded.locations["static_libraries"]
    assert isinstance(crt_paths, tuple)
    assert isinstance(static_paths, tuple)
    _inspect_relocatable_inputs(
        root=root,
        crt_paths=crt_paths,
        static_paths=static_paths,
        arch=loaded.arch,
        inspector=elf_inspector,
    )
    shared_paths = loaded.locations["shared_libraries"]
    assert isinstance(shared_paths, tuple)
    actual_reports = _inspect_shared_libraries(
        root=root,
        shared_paths=shared_paths,
        static_paths=static_paths,
        arch=loaded.arch,
        glibc_floor=loaded.glibc_floor,
        inspector=elf_inspector,
    )
    actual = GccRuntimeManifest.from_dict(
        {
            **loaded.to_dict(),
            "version_symbol_reports": list(actual_reports),
        }
    )
    if (
        actual.to_dict()["version_symbol_reports"]
        != loaded.to_dict()["version_symbol_reports"]
    ):
        raise ConfigurationError(
            "GCC runtime version symbol report does not match its ELF files"
        )
    return loaded


def _materialize_gcc_runtime(
    prefix: Path,
    glibc_floor: str,
    arch: str,
    staging: Path,
    *,
    licenses: Path | str | None = None,
    probe_gxx: Path | str | None = None,
    provider_version: str | None = None,
    target: str | None = None,
) -> GccRuntimeManifest:
    """Write one complete GCC runtime into caller-owned empty staging."""

    floor = AbiVersion.parse(glibc_floor)
    if arch not in {"x86_64", "aarch64"}:
        raise ConfigurationError("GCC runtime arch must be x86_64 or aarch64")
    if staging.is_symlink() or not staging.is_dir() or next(staging.iterdir(), None):
        raise ConfigurationError("GCC runtime staging must be an empty directory")
    if (provider_version is None) != (target is None):
        raise ConfigurationError(
            "managed GCC import requires provider_version and target together"
        )
    if provider_version is not None and probe_gxx is not None:
        raise ConfigurationError(
            "managed GCC metadata and --probe-gxx are mutually exclusive"
        )
    installation = (
        _installation_from_metadata(
            prefix,
            version=provider_version,
            target=target,
        )
        if provider_version is not None and target is not None
        else _probe_gcc(prefix, probe_gxx=probe_gxx)
    )
    detected_arch = classify_linux_glibc_target(
        installation.target,
        policy="strict",
        context="GCC target",
    )
    if detected_arch != arch:
        raise ConfigurationError(
            f"GCC prefix targets {detected_arch}, but import requested {arch}"
        )

    runtime = staging / "runtime"
    runtime.mkdir()
    cxx_include_dirs = _copy_cxx_headers(prefix, runtime, installation)
    gcc_runtime_dir, crt_objects, gcc_static = _copy_gcc_runtime_dir(
        prefix, runtime, installation
    )
    library_dirs, library_static, shared_libraries = _copy_libraries(
        prefix, runtime, installation
    )
    static_libraries = tuple(sorted({*gcc_static, *library_static}))
    if provider_version is not None:
        _require_managed_gcc_linker_inputs(
            installation,
            static_libraries,
            shared_libraries,
        )
    if provider_version is not None and arch == "x86_64":
        _require_quadmath_inputs(
            staging,
            gcc_runtime_dir,
            static_libraries,
            shared_libraries,
        )
    _validate_symlinks(runtime, runtime_name="GCC runtime")
    _validate_payload_filter(runtime)
    reports = _inspect_shared_libraries(
        root=staging,
        shared_paths=shared_libraries,
        static_paths=static_libraries,
        arch=arch,
        glibc_floor=str(floor),
        inspector=ReadElfInspector(),
    )
    publish_license_directory(
        prefix if licenses is None else licenses,
        staging,
        managed_required_license_paths("gcc", compiler_kit=False),
        context="GCC runtime",
    )
    manifest = GccRuntimeManifest.from_dict(
        {
            "schema": RUNTIME_MANIFEST_SCHEMA,
            "format": RUNTIME_MANIFEST_FORMAT,
            "provider": {
                "name": "gcc",
                "version": installation.version,
                "major": installation.major,
            },
            "arch": arch,
            "target": installation.target,
            "glibc_floor": str(floor),
            "locations": {
                "runtime": "runtime",
                "cxx_include_dirs": list(cxx_include_dirs),
                "gcc_runtime_dir": gcc_runtime_dir,
                "library_dirs": list(library_dirs),
                "crt_objects": list(crt_objects),
                "static_libraries": list(static_libraries),
                "shared_libraries": list(shared_libraries),
            },
            "version_symbol_reports": list(reports),
        }
    )
    (staging / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (staging / _OWNER_MARKER).write_text(_OWNER_MARKER_CONTENT, encoding="utf-8")
    return manifest


def import_gcc_runtime(
    prefix: Path | str,
    glibc_floor: str,
    arch: str,
    output: Path | str,
    *,
    licenses: Path | str | None = None,
    force: bool = False,
    probe_gxx: Path | str | None = None,
    provider_version: str | None = None,
    target: str | None = None,
) -> Path:
    """Import a filtered, relocatable GCC C++ runtime from an installation."""

    source_prefix, destination = _resolve_import_paths(
        prefix,
        output,
        prefix_context="GCC prefix",
        output_context="GCC runtime output",
        reject_prefix_symlink=False,
    )
    _check_output(
        destination,
        force=force,
        output_context="GCC runtime output",
        owner_description="a generator-owned GCC runtime",
        owner_marker=_OWNER_MARKER,
        load_manifest=load_runtime_manifest,
    )
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent)
    )
    try:
        expected = _materialize_gcc_runtime(
            source_prefix,
            glibc_floor,
            arch,
            temporary,
            licenses=licenses,
            probe_gxx=probe_gxx,
            provider_version=provider_version,
            target=target,
        )

        def validate_final(published: Path) -> None:
            loaded = load_runtime_manifest(published)
            if loaded.to_dict() != expected.to_dict():
                raise ConfigurationError("published GCC runtime manifest changed")
            validate_runtime_manifest(published, loaded)

        replace_directory(temporary, destination, validate=validate_final)
        return destination / "manifest.json"
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
