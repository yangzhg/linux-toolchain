from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.runtime import (
    LLVM_RUNTIME_MANIFEST_SCHEMA,
    RUNTIME_MANIFEST_SCHEMA,
    GccRuntimeManifest,
    LlvmRuntimeManifest,
    validate_llvm_runtime_manifest,
    validate_runtime_manifest,
)


@dataclass(frozen=True)
class GccRuntimeBinding:
    """Validated, absolute paths needed to bind an imported GCC runtime."""

    export_root: Path
    manifest_path: Path
    manifest: GccRuntimeManifest
    runtime_root: Path
    gcc_runtime_dir: Path
    library_dirs: tuple[Path, ...]
    cxx_include_dirs: tuple[Path, ...]
    builtin_include_dir: Path
    fixed_include_dir: Path | None


@dataclass(frozen=True)
class LlvmRuntimeBinding:
    """Validated paths for one libc++/compiler-rt runtime export."""

    export_root: Path
    manifest_path: Path
    manifest: LlvmRuntimeManifest
    runtime_root: Path
    library_dirs: tuple[Path, ...]
    cxx_include_dirs: tuple[Path, ...]
    resource_dir: Path
    shared_libraries: tuple[Path, ...]
    static_libraries: tuple[Path, ...]
    builtins: Path
    crt_objects: tuple[Path, ...]


RuntimeBinding = GccRuntimeBinding | LlvmRuntimeBinding


@dataclass(frozen=True)
class GccRuntimeLinkEvidence:
    runtime_root: Path
    gcc_runtime_dir: Path
    library_dirs: tuple[Path, ...]


@dataclass(frozen=True)
class LlvmRuntimeLinkEvidence:
    runtime_root: Path
    library_dirs: tuple[Path, ...]
    shared_libraries: tuple[Path, ...]
    static_libraries: tuple[Path, ...]
    builtins: Path
    crt_objects: tuple[Path, ...]
    forbidden_sonames: tuple[str, ...]


RuntimeLinkEvidence = GccRuntimeLinkEvidence | LlvmRuntimeLinkEvidence


def _runtime_location(
    export_root: Path,
    locations: Mapping[str, object],
    name: str,
) -> Path:
    return export_root / str(locations[name])


def _runtime_location_list(
    export_root: Path,
    locations: Mapping[str, object],
    name: str,
) -> tuple[Path, ...]:
    return tuple(export_root / str(value) for value in locations[name])


def _load_gcc_runtime_binding(
    runtime_input: Path,
    manifest_path: Path,
    manifest: GccRuntimeManifest | None = None,
) -> GccRuntimeBinding:
    manifest = validate_runtime_manifest(runtime_input, manifest)
    export_root = manifest_path.parent.resolve()
    locations = manifest.locations
    runtime_root = _runtime_location(export_root, locations, "runtime")
    gcc_runtime_dir = _runtime_location(export_root, locations, "gcc_runtime_dir")
    library_dirs = _runtime_location_list(export_root, locations, "library_dirs")
    cxx_roots = _runtime_location_list(export_root, locations, "cxx_include_dirs")
    cxx_include_dirs_list: list[Path] = []
    for root in cxx_roots:
        for candidate in (root, root / manifest.target, root / "backward"):
            if candidate.is_dir() and candidate not in cxx_include_dirs_list:
                cxx_include_dirs_list.append(candidate)
    builtin_include_dir = gcc_runtime_dir / "include"
    if not builtin_include_dir.is_dir():
        raise ConfigurationError(
            f"runtime GCC builtin include directory is missing: {builtin_include_dir}"
        )
    fixed_candidate = gcc_runtime_dir / "include-fixed"
    return GccRuntimeBinding(
        export_root=export_root,
        manifest_path=manifest_path,
        manifest=manifest,
        runtime_root=runtime_root,
        gcc_runtime_dir=gcc_runtime_dir,
        library_dirs=library_dirs,
        cxx_include_dirs=tuple(cxx_include_dirs_list),
        builtin_include_dir=builtin_include_dir,
        fixed_include_dir=(fixed_candidate if fixed_candidate.is_dir() else None),
    )


def _load_llvm_runtime_binding(
    runtime_input: Path,
    manifest_path: Path,
    manifest: LlvmRuntimeManifest | None = None,
) -> LlvmRuntimeBinding:
    manifest = validate_llvm_runtime_manifest(runtime_input, manifest)
    export_root = manifest_path.parent.resolve()
    locations = manifest.locations
    return LlvmRuntimeBinding(
        export_root=export_root,
        manifest_path=manifest_path,
        manifest=manifest,
        runtime_root=_runtime_location(export_root, locations, "runtime"),
        library_dirs=_runtime_location_list(export_root, locations, "library_dirs"),
        cxx_include_dirs=_runtime_location_list(
            export_root, locations, "cxx_include_dirs"
        ),
        resource_dir=_runtime_location(export_root, locations, "resource_dir"),
        shared_libraries=_runtime_location_list(
            export_root, locations, "shared_libraries"
        ),
        static_libraries=_runtime_location_list(
            export_root, locations, "static_libraries"
        ),
        builtins=_runtime_location(export_root, locations, "builtins"),
        crt_objects=_runtime_location_list(export_root, locations, "crt_objects"),
    )


def _load_runtime_binding(
    runtime: Path,
    manifest: GccRuntimeManifest | LlvmRuntimeManifest | None = None,
) -> RuntimeBinding:
    runtime_input = runtime.expanduser()
    manifest_path = (
        runtime_input / "manifest.json" if runtime_input.is_dir() else runtime_input
    ).resolve()
    if isinstance(manifest, GccRuntimeManifest):
        return _load_gcc_runtime_binding(runtime_input, manifest_path, manifest)
    if isinstance(manifest, LlvmRuntimeManifest):
        return _load_llvm_runtime_binding(runtime_input, manifest_path, manifest)
    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read runtime manifest {manifest_path}: {error}"
        ) from error
    schema = manifest_data.get("schema") if isinstance(manifest_data, dict) else None
    if schema == RUNTIME_MANIFEST_SCHEMA:
        return _load_gcc_runtime_binding(runtime_input, manifest_path)
    if schema == LLVM_RUNTIME_MANIFEST_SCHEMA:
        return _load_llvm_runtime_binding(runtime_input, manifest_path)
    raise ConfigurationError(f"unsupported runtime manifest schema: {schema!r}")


def _runtime_link_evidence(runtime: RuntimeBinding) -> RuntimeLinkEvidence:
    if isinstance(runtime, GccRuntimeBinding):
        return GccRuntimeLinkEvidence(
            runtime_root=runtime.runtime_root,
            gcc_runtime_dir=runtime.gcc_runtime_dir,
            library_dirs=runtime.library_dirs,
        )
    return LlvmRuntimeLinkEvidence(
        runtime_root=runtime.runtime_root,
        library_dirs=runtime.library_dirs,
        shared_libraries=runtime.shared_libraries,
        static_libraries=runtime.static_libraries,
        builtins=runtime.builtins,
        crt_objects=runtime.crt_objects,
        forbidden_sonames=runtime.manifest.forbidden_sonames,
    )
