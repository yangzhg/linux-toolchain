from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable

from linux_toolchain.container import (
    BUILDER_DOCKERFILE_NAME,
    MANAGED_BUILDER_TARGET,
    BuilderHost,
    BuilderImage,
    ContainerIdentityFiles,
    builder_image_contract_digest,
    docker_build_command,
    linux_architecture_for_platform,
    require_non_root_builder,
    resolve_builder_image,
    temporary_container_owner,
    temporary_container_run,
    ubuntu_builder_snapshot,
    validate_native_docker_daemon,
    validate_packaged_dockerfile,
    write_container_identity_files,
)
from linux_toolchain.elf.reader import resolve_readelf_candidates
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.integrity import file_sha256
from linux_toolchain.licenses import (
    require_license_files,
    sdk_required_license_paths,
    validate_license_evidence,
)
from linux_toolchain.managed.artifacts import finalize_artifact as _finalize_artifact
from linux_toolchain.managed.contracts import (
    MANAGED_BUILDER_BASE_IMAGE,
    MANAGED_COMPILER_BACKEND_GCC,
    MANAGED_COMPILER_BACKEND_SUPPLEMENTAL_SOURCES,
    MANAGED_COMPILER_BACKEND_VERSION,
    MANAGED_TARGET_TOOL_NAMES,
    MANAGED_WORKSPACE_FORMAT,
    MANAGED_WORKSPACE_SCHEMA,
)
from linux_toolchain.managed.identity import (
    artifact_action_selection,
    managed_artifact_action,
    managed_builder_contract,
    render_action_script,
    script_identity,
    source_content_pin,
    target_tools_action,
)
from linux_toolchain.managed.lockfile import ManagedLock, SourceLock
from linux_toolchain.managed.scripts import render_build_script
from linux_toolchain.managed.selection import (
    ManagedBuildSelection,
    select_artifact,
)
from linux_toolchain.managed.selection import (
    managed_lock as _managed_lock,
)
from linux_toolchain.managed.sources import (
    download_source_archive as _download_source_archive,
)
from linux_toolchain.models import (
    SDK_MANIFEST_FORMAT,
    SDK_MANIFEST_SCHEMA,
    SDK_SPEC_FORMAT,
    SDK_SPEC_SCHEMA,
    SdkSpec,
    classify_linux_glibc_target,
)
from linux_toolchain.process import run_logged, run_streaming
from linux_toolchain.publication import replace_directory
from linux_toolchain.schema import read_json_object as _read_json_object
from linux_toolchain.sdk.crosstool_ng import (
    COMPONENT_SHA256,
    CROSSTOOL_NG_RELEASES,
    sdk_producer_identity,
    validate_portable_target_tools,
)
from linux_toolchain.sdk.crosstool_ng import (
    load_workspace as load_sdk_workspace,
)
from linux_toolchain.versions import AbiVersion

_WORKSPACE_MARKER = ".linux-toolchain-managed-workspace"
_WORKSPACE_MARKER_CONTENT = "format=1\n"
_SOURCE_CACHE_MARKER = ".linux-toolchain-source-cache"
_OUTPUT_MARKER = ".linux-toolchain-managed-output"
_WORKSPACE_FIELDS = {
    "schema",
    "format",
    "build_input",
    "sdk",
    "target_tools",
    "compiler_backend",
    "build_script",
    "source_cache",
}
_COMPILER_BACKEND_SOURCES = MANAGED_COMPILER_BACKEND_SUPPLEMENTAL_SOURCES
_TARGET_TOOLS = MANAGED_TARGET_TOOL_NAMES
_BUILD_LOG_TAIL_BYTES = 64 * 1024
_BUILD_LOG_TAIL_LINES = 3
_BUILD_LOG_REFRESH_SECONDS = 1.0
ProgressCallback = Callable[[str], None]
TransferProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class SdkEvidence:
    root: Path
    sysroot: Path
    arch: str
    glibc_version: str
    triplet: str
    identity: dict[str, object]


@dataclass(frozen=True)
class TargetToolsEvidence:
    root: Path
    identity: dict[str, object]


@dataclass(frozen=True)
class CompilerBackendEvidence:
    workspace: Path
    toolchain: Path
    sources: Path
    source_evidence: dict[str, object]
    triplet: str
    glibc_version: str
    gcc_version: str
    identity: dict[str, object]


ProducerEvidence = tuple[
    SdkEvidence,
    TargetToolsEvidence,
    CompilerBackendEvidence,
]


def _build_log_tail(path: Path) -> tuple[str, ...]:
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            size = log.tell()
            start = max(0, size - _BUILD_LOG_TAIL_BYTES)
            log.seek(start)
            content = log.read()
    except OSError:
        return ()

    if start:
        newline = content.find(b"\n")
        if newline >= 0:
            content = content[newline + 1 :]
    lines = (
        "".join(
            character if character.isprintable() else " " for character in line
        ).rstrip()
        for line in content.decode("utf-8", errors="replace").splitlines()
    )
    return tuple(line for line in lines if line)[-_BUILD_LOG_TAIL_LINES:]


def _existing_directory(path: Path | str, *, context: str) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"{context} cannot be a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(f"cannot access {context} {raw}: {error}") from error
    if not resolved.is_dir():
        raise ConfigurationError(f"{context} is not a directory: {resolved}")
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _validate_target_triplet(value: object, arch: str) -> str:
    classify_linux_glibc_target(
        value,
        policy="strict",
        expected_architecture=arch,
        context="managed SDK target triplet",
    )
    assert isinstance(value, str)
    return value


def _stable_sdk_identity(
    manifest: Mapping[str, object], *, context: str = "managed SDK"
) -> dict[str, object]:
    target = manifest.get("target")
    builder = manifest.get("builder")
    sources = manifest.get("sources")
    environment = manifest.get("build_environment")
    if not all(
        isinstance(value, Mapping) for value in (target, builder, sources, environment)
    ):
        raise ConfigurationError(
            f"{context} manifest is missing stable build-input evidence"
        )
    assert isinstance(target, Mapping)
    assert isinstance(builder, Mapping)
    assert isinstance(sources, Mapping)
    assert isinstance(environment, Mapping)
    public_target = {key: value for key, value in target.items() if key != "triplet"}
    spec = SdkSpec.from_dict(
        {
            "schema": SDK_SPEC_SCHEMA,
            "format": SDK_SPEC_FORMAT,
            "name": "managed-sdk-input",
            "target": public_target,
            "builder": dict(builder),
        }
    )
    if target.get("triplet") != spec.target.triplet:
        raise ConfigurationError(f"{context} target triplet evidence is invalid")
    identity = sdk_producer_identity(spec)
    if manifest.get("defconfig_sha256") != identity["config_sha256"]:
        raise ConfigurationError(f"{context} build configuration evidence changed")
    required_environment = {
        "dockerfile_sha256",
        "base_image",
        "platform",
        "apt_snapshot",
    }
    if not required_environment.issubset(environment):
        raise ConfigurationError(f"{context} build-environment evidence is incomplete")
    contract = identity["builder_contract"]
    if not isinstance(contract, Mapping) or any(
        environment[key] != contract[key] for key in required_environment
    ):
        raise ConfigurationError(f"{context} builder inputs changed")
    expected_sources = {
        "crosstool-ng": (
            spec.builder.version,
            CROSSTOOL_NG_RELEASES[spec.builder.version].sha256,
        ),
        "glibc": (
            spec.target.libc_version,
            COMPONENT_SHA256[("glibc", spec.target.libc_version)],
        ),
        "linux": (
            spec.target.linux_headers,
            COMPONENT_SHA256[("linux", spec.target.linux_headers)],
        ),
        "gcc": (spec.builder.gcc, COMPONENT_SHA256[("gcc", spec.builder.gcc)]),
        "binutils": (
            spec.builder.binutils,
            COMPONENT_SHA256[("binutils", spec.builder.binutils)],
        ),
    }
    for component, (version, sha256) in expected_sources.items():
        evidence = sources.get(component)
        if not isinstance(evidence, Mapping) or (
            evidence.get("version"),
            evidence.get("sha256"),
        ) != (version, sha256):
            raise ConfigurationError(f"{context} {component} source evidence changed")
    return identity


def _sdk_evidence(sdk: Path | str, selection: ManagedBuildSelection) -> SdkEvidence:
    root = _existing_directory(sdk, context="managed SDK")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ConfigurationError(f"managed SDK manifest is missing: {manifest_path}")
    manifest = _read_json_object(manifest_path, context="managed SDK manifest")
    manifest_format = manifest.get("format")
    if (
        manifest.get("schema") != SDK_MANIFEST_SCHEMA
        or not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != SDK_MANIFEST_FORMAT
        or manifest.get("compatibility_scope") != "glibc-floor"
    ):
        raise ConfigurationError(
            "managed SDK manifest has an unsupported schema or format"
        )
    validate_license_evidence(
        root,
        manifest.get("licenses"),
        context="managed SDK",
    )
    require_license_files(
        root,
        sdk_required_license_paths(),
        context="managed SDK",
    )
    target = manifest.get("target")
    if not isinstance(target, dict):
        raise ConfigurationError("managed SDK manifest.target must be an object")
    arch = target.get("arch")
    glibc_version = target.get("libc_version")
    if target.get("libc") != "glibc":
        raise ConfigurationError("managed SDK must contain a glibc sysroot")
    if arch != selection.target_arch:
        raise ConfigurationError(
            f"managed SDK architecture {arch!r} does not match "
            f"{selection.target_arch!r}"
        )
    if not isinstance(glibc_version, str):
        raise ConfigurationError("managed SDK libc version is missing")
    AbiVersion.parse(glibc_version)
    if selection.target_glibc_floor is not None and AbiVersion.parse(
        glibc_version
    ) != AbiVersion.parse(selection.target_glibc_floor):
        raise ConfigurationError(
            "managed runtime must be built with an SDK at its exact glibc floor"
        )
    triplet = _validate_target_triplet(target.get("triplet"), selection.target_arch)
    sysroot = root / "sysroot"
    if sysroot.is_symlink() or not sysroot.is_dir():
        raise ConfigurationError(f"managed SDK sysroot is missing: {sysroot}")
    return SdkEvidence(
        root=root,
        sysroot=sysroot,
        arch=arch,
        glibc_version=glibc_version,
        triplet=triplet,
        identity=_stable_sdk_identity(manifest),
    )


def _target_tools_evidence(
    target_tools: Path | str, sdk: SdkEvidence
) -> TargetToolsEvidence:
    root = _existing_directory(target_tools, context="managed target tools")
    expected = sdk.root.parent / "toolchain" / "bin"
    try:
        expected = expected.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            "managed SDK workspace does not contain target tools"
        ) from error
    if root != expected:
        raise ConfigurationError(
            "managed target tools must come from the selected SDK workspace"
        )
    for name in _TARGET_TOOLS:
        path = root / f"{sdk.triplet}-{name}"
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError) as error:
                raise ConfigurationError(
                    f"managed target tool symlink escapes its tree: {path}"
                ) from error
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ConfigurationError(f"managed target tool is missing: {path}")
    spec = load_sdk_workspace(sdk.root.parent)
    validate_portable_target_tools(spec, sdk.root.parent)
    return TargetToolsEvidence(
        root=root,
        identity=target_tools_action(sdk.identity, sdk.triplet),
    )


def _compiler_backend_evidence(
    compiler_backend: Path | str,
    selection: ManagedBuildSelection,
    *,
    verify_sources: bool = True,
) -> CompilerBackendEvidence:
    workspace = _existing_directory(
        compiler_backend,
        context="managed compiler backend workspace",
    )
    workspace_manifest = _read_json_object(
        workspace / "workspace.json",
        context="managed compiler backend workspace manifest",
    )
    if workspace_manifest.get("state") != "built":
        raise ConfigurationError("managed compiler backend build is incomplete")
    spec = load_sdk_workspace(workspace)
    if (
        spec.target.arch != selection.build_host.arch
        or spec.builder.version != MANAGED_COMPILER_BACKEND_VERSION
        or spec.builder.gcc != MANAGED_COMPILER_BACKEND_GCC
    ):
        raise ConfigurationError(
            "managed compiler backend must be the pinned crosstool-NG 1.28.0 "
            f"{selection.build_host.arch} GCC 9.5 toolchain"
        )
    if AbiVersion.parse(spec.target.libc_version) != AbiVersion.parse(
        selection.build_host.glibc_floor
    ):
        raise ConfigurationError(
            "managed compiler backend glibc floor does not match the Compiler "
            f"Kit host floor {selection.build_host.glibc_floor}"
        )

    sdk_manifest = _read_json_object(
        workspace / "sdk" / "manifest.json",
        context="managed compiler backend SDK manifest",
    )
    serialized_spec = spec.to_manifest_dict()
    if (
        sdk_manifest.get("schema") != SDK_MANIFEST_SCHEMA
        or sdk_manifest.get("format") != SDK_MANIFEST_FORMAT
        or sdk_manifest.get("target") != serialized_spec["target"]
        or sdk_manifest.get("builder") != serialized_spec["builder"]
    ):
        raise ConfigurationError(
            "managed compiler backend SDK manifest does not match its workspace"
        )
    backend_sdk_identity = _stable_sdk_identity(
        sdk_manifest,
        context="managed compiler backend SDK",
    )
    source_evidence = sdk_manifest.get("sources")
    assert isinstance(source_evidence, Mapping)

    toolchain = _existing_directory(
        workspace / "toolchain",
        context="managed compiler backend toolchain",
    )
    for name in ("gcc", "g++", "ar", "as", "ld", "nm", "ranlib", "strip"):
        executable = toolchain / "bin" / f"{spec.target.triplet}-{name}"
        try:
            resolved = executable.resolve(strict=True)
            resolved.relative_to(toolchain)
        except (OSError, RuntimeError, ValueError) as error:
            raise ConfigurationError(
                f"managed compiler backend executable is invalid: {executable}"
            ) from error
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ConfigurationError(
                f"managed compiler backend executable is missing: {executable}"
            )

    sources = _existing_directory(
        workspace / "downloads",
        context="managed compiler backend sources",
    )
    if verify_sources:
        for filename, expected_sha256 in _COMPILER_BACKEND_SOURCES.items():
            archive = sources / filename
            if archive.is_symlink() or not archive.is_file():
                raise ConfigurationError(
                    f"managed compiler backend source is missing: {archive}"
                )
            if file_sha256(archive) != expected_sha256:
                raise ConfigurationError(
                    f"managed compiler backend source hash changed: {archive}"
                )
    return CompilerBackendEvidence(
        workspace=workspace,
        toolchain=toolchain,
        sources=sources,
        source_evidence=source_evidence,
        triplet=spec.target.triplet,
        glibc_version=spec.target.libc_version,
        gcc_version=spec.builder.gcc,
        identity={
            "sdk": backend_sdk_identity,
            "supplemental_sources": [
                {"filename": filename, "sha256": sha256}
                for filename, sha256 in sorted(_COMPILER_BACKEND_SOURCES.items())
            ],
        },
    )


def validate_producer_inputs(
    sdk: Path,
    target_tools: Path,
    compiler_backend: Path,
    *,
    sdk_selection: ManagedBuildSelection,
    backend_selection: ManagedBuildSelection,
) -> ProducerEvidence:
    """Validate shared producer inputs once for an assemble operation."""

    sdk_info = _sdk_evidence(sdk, sdk_selection)
    tools_info = _target_tools_evidence(target_tools, sdk_info)
    backend_info = _compiler_backend_evidence(
        compiler_backend,
        backend_selection,
        verify_sources=True,
    )
    return sdk_info, tools_info, backend_info


def _build_input_value(
    selection: ManagedBuildSelection,
    sdk: SdkEvidence,
    target_tools: TargetToolsEvidence,
    compiler_backend: CompilerBackendEvidence,
    *,
    script: Mapping[str, object],
) -> dict[str, object]:
    return managed_artifact_action(
        selection,
        sdk=sdk.identity,
        target_tools=target_tools.identity,
        compiler_backend=compiler_backend.identity,
        script=script,
    )


def _builder_contract(selection: ManagedBuildSelection) -> dict[str, str]:
    return managed_builder_contract(selection.build_platform)


def _sdk_mount_value(sdk: SdkEvidence) -> dict[str, object]:
    return {
        "path": str(sdk.root),
        "arch": sdk.arch,
        "glibc_version": sdk.glibc_version,
        "triplet": sdk.triplet,
        "identity": sdk.identity,
    }


def _target_tools_mount_value(
    target_tools: TargetToolsEvidence,
) -> dict[str, object]:
    return {"path": str(target_tools.root), "identity": target_tools.identity}


def _compiler_backend_mount_value(
    compiler_backend: CompilerBackendEvidence,
) -> dict[str, object]:
    return {
        "path": str(compiler_backend.workspace),
        "version": MANAGED_COMPILER_BACKEND_VERSION,
        "gcc": MANAGED_COMPILER_BACKEND_GCC,
        "triplet": compiler_backend.triplet,
        "glibc_version": compiler_backend.glibc_version,
        "sources": compiler_backend.source_evidence,
        "identity": compiler_backend.identity,
    }


def _workspace_path(path: Path | str) -> Path:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"managed workspace cannot be a symlink: {raw}")
    resolved = raw.resolve()
    if resolved in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid managed workspace path: {resolved}")
    return resolved


def _workspace_is_owned(workspace: Path) -> bool:
    marker = workspace / _WORKSPACE_MARKER
    manifest = workspace / "workspace.json"
    if (
        marker.is_symlink()
        or manifest.is_symlink()
        or not marker.is_file()
        or not manifest.is_file()
    ):
        return False
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        return (
            marker.read_text(encoding="utf-8") == _WORKSPACE_MARKER_CONTENT
            and isinstance(value, dict)
            and value.get("schema") == MANAGED_WORKSPACE_SCHEMA
            and type(value.get("format")) is int
            and value.get("format") == MANAGED_WORKSPACE_FORMAT
        )
    except (OSError, json.JSONDecodeError):
        return False


def _prepare_empty_workspace(
    workspace: Path, *, force: bool, build_input: Mapping[str, object]
) -> None:
    if workspace.exists():
        if not workspace.is_dir():
            raise ConfigurationError(
                f"managed workspace is not a directory: {workspace}"
            )
        nonempty = next(workspace.iterdir(), None) is not None
        if nonempty:
            if not force:
                raise ConfigurationError(
                    f"managed workspace is non-empty: {workspace}; pass --force"
                )
            if not _workspace_is_owned(workspace):
                raise ConfigurationError(
                    f"refusing to replace an unowned managed workspace: {workspace}"
                )
            previous = _read_json_object(
                workspace / "workspace.json", context="managed workspace"
            )
            if previous.get("build_input") != build_input:
                raise ConfigurationError(
                    "managed workspace build inputs changed; use a new workspace "
                    "for a different artifact, source, SDK, compiler backend, "
                    "or target-tool set"
                )
            shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / _WORKSPACE_MARKER).write_text(
        _WORKSPACE_MARKER_CONTENT, encoding="utf-8"
    )
    for relative in ("build", "output"):
        (workspace / relative).mkdir()


def _source_filename(source: SourceLock) -> str:
    return f"archive-{source.sha512}.tar.xz"


def _prepare_source_cache(root: Path) -> Path:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ConfigurationError(f"managed source cache is not a directory: {root}")
    if root.exists() and next(root.iterdir(), None) is not None:
        marker = root / _SOURCE_CACHE_MARKER
        if (
            marker.is_symlink()
            or not marker.is_file()
            or marker.read_text(encoding="utf-8") != _WORKSPACE_MARKER_CONTENT
        ):
            raise ConfigurationError(
                f"refusing to use unowned managed source cache: {root}"
            )
        return root
    root.mkdir(parents=True, exist_ok=True)
    (root / _SOURCE_CACHE_MARKER).write_text(
        _WORKSPACE_MARKER_CONTENT, encoding="utf-8"
    )
    return root


def render_workspace(
    lock_value: ManagedLock | Mapping[str, object] | object,
    artifact_id: str,
    workspace: Path | str,
    *,
    sdk: Path | str,
    target_tools: Path | str,
    compiler_backend: Path | str,
    source_cache: Path | str | None = None,
    force: bool = False,
    paired_runtime: bool = False,
    _producer: ProducerEvidence | None = None,
) -> Path:
    lock = _managed_lock(lock_value)
    selection = select_artifact(lock, artifact_id)
    if lock.build_platform != selection.build_platform:
        raise ConfigurationError("managed builder platform selection is inconsistent")
    if selection.artifact_kind == "compiler-kit":
        if selection.host is None:
            raise ConfigurationError("managed Compiler Kit has no host selection")
        if (
            selection.host.os != "linux"
            or selection.host.arch
            != linux_architecture_for_platform(selection.build_platform)
        ):
            raise ConfigurationError(
                "managed builder platform does not match the Compiler Kit host"
            )
        AbiVersion.parse(selection.host.glibc_floor)
    workspace_path = _workspace_path(workspace)
    if _producer is None:
        sdk_info = _sdk_evidence(sdk, selection)
        tools_info = _target_tools_evidence(target_tools, sdk_info)
        backend_info = _compiler_backend_evidence(
            compiler_backend,
            selection,
            verify_sources=False,
        )
    else:
        sdk_info, tools_info, backend_info = _producer
    if any(
        _paths_overlap(workspace_path, path)
        for path in (sdk_info.root, tools_info.root, backend_info.workspace)
    ):
        raise ConfigurationError(
            "managed workspace must not overlap its read-only build inputs"
        )
    action_script = render_action_script(
        selection,
        triplet=sdk_info.triplet,
        backend_triplet=backend_info.triplet,
        backend_version=backend_info.gcc_version,
    )
    action_script_identity = script_identity(action_script)
    build_input = _build_input_value(
        selection,
        sdk_info,
        tools_info,
        backend_info,
        script=action_script_identity,
    )
    _prepare_empty_workspace(
        workspace_path,
        force=force,
        build_input=build_input,
    )
    source_root = _prepare_source_cache(
        workspace_path / "sources"
        if source_cache is None
        else _workspace_path(source_cache)
    )
    if any(
        _paths_overlap(source_root, path)
        for path in (sdk_info.root, tools_info.root, backend_info.workspace)
    ):
        raise ConfigurationError(
            "managed source cache must not overlap read-only build inputs"
        )
    execution_script = render_build_script(
        selection,
        triplet=sdk_info.triplet,
        backend_triplet=backend_info.triplet,
        backend_version=backend_info.gcc_version,
        paired_runtime=paired_runtime,
    )
    script_path = workspace_path / "build" / "build.sh"
    script_path.write_text(execution_script, encoding="utf-8")
    script_path.chmod(0o755)
    (workspace_path / "output" / _OUTPUT_MARKER).write_text(
        _WORKSPACE_MARKER_CONTENT, encoding="utf-8"
    )

    manifest = {
        "schema": MANAGED_WORKSPACE_SCHEMA,
        "format": MANAGED_WORKSPACE_FORMAT,
        "build_input": build_input,
        "sdk": _sdk_mount_value(sdk_info),
        "target_tools": _target_tools_mount_value(tools_info),
        "compiler_backend": _compiler_backend_mount_value(backend_info),
        "build_script": {
            "path": "build/build.sh",
            "sha256": hashlib.sha256(execution_script.encode("utf-8")).hexdigest(),
            "paired_runtime": paired_runtime,
        },
        "source_cache": str(source_root / _source_filename(selection.source)),
    }
    manifest_path = workspace_path / "workspace.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _load_workspace(workspace: Path | str) -> tuple[Path, dict[str, Any]]:
    root = _workspace_path(workspace)
    if not _workspace_is_owned(root):
        raise ConfigurationError(f"invalid managed workspace: {root}")
    manifest = _read_json_object(root / "workspace.json", context="managed workspace")
    if (
        manifest.get("schema") != MANAGED_WORKSPACE_SCHEMA
        or type(manifest.get("format")) is not int
        or manifest.get("format") != MANAGED_WORKSPACE_FORMAT
        or set(manifest) != _WORKSPACE_FIELDS
    ):
        raise ConfigurationError("unsupported managed workspace manifest")
    return root, manifest


def _verify_workspace_selection(
    selection: ManagedBuildSelection,
    manifest: Mapping[str, object],
) -> None:
    if "lock_sha256" in manifest or "catalog_sha256" in manifest:
        raise ConfigurationError("managed workspace contains a lock-bound identity")
    build_input = manifest.get("build_input")
    sdk = manifest.get("sdk")
    tools = manifest.get("target_tools")
    backend = manifest.get("compiler_backend")
    if not isinstance(build_input, Mapping):
        raise ConfigurationError("managed workspace build inputs are missing")
    if not isinstance(sdk, Mapping) or build_input.get("sdk") != sdk.get("identity"):
        raise ConfigurationError("managed workspace SDK selection is inconsistent")
    if not isinstance(tools, Mapping) or build_input.get("target_tools") != tools.get(
        "identity"
    ):
        raise ConfigurationError("managed workspace target tools are inconsistent")
    if not isinstance(backend, Mapping) or build_input.get(
        "compiler_backend"
    ) != backend.get("identity"):
        raise ConfigurationError("managed workspace compiler backend is inconsistent")
    builder_contract = _builder_contract(selection)
    if (
        "lock_sha256" in build_input
        or "catalog_sha256" in build_input
        or build_input.get("artifact") != artifact_action_selection(selection)
        or build_input.get("source") != source_content_pin(selection)
        or build_input.get("builder") != builder_contract
    ):
        raise ConfigurationError("managed workspace builder identity is stale")


def _source_cache_path(root: Path, value: object, source: SourceLock) -> Path:
    if not isinstance(value, str):
        raise ConfigurationError("managed workspace source cache path is invalid")
    path = Path(value)
    if path.name != _source_filename(source) or ".." in path.parts:
        raise ConfigurationError("managed workspace source cache path is invalid")
    destination = path if path.is_absolute() else root / path
    try:
        cache_root = destination.parent.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise ConfigurationError("managed source cache is inaccessible") from error
    marker = cache_root / _SOURCE_CACHE_MARKER
    if (
        marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != _WORKSPACE_MARKER_CONTENT
    ):
        raise ConfigurationError(f"invalid managed source cache marker: {marker}")
    return destination


def _validated_workspace_inputs(
    selection: ManagedBuildSelection,
    root: Path,
    manifest: Mapping[str, object],
    *,
    producer: ProducerEvidence | None = None,
) -> tuple[Path, SdkEvidence, TargetToolsEvidence, CompilerBackendEvidence]:
    _verify_workspace_selection(selection, manifest)
    source = _source_cache_path(root, manifest.get("source_cache"), selection.source)
    if source.exists() and (not source.is_file() or source.is_symlink()):
        raise ConfigurationError("managed source cache entry is not a regular file")

    sdk_value = manifest.get("sdk")
    tools_value = manifest.get("target_tools")
    backend_value = manifest.get("compiler_backend")
    if (
        not isinstance(sdk_value, Mapping)
        or not isinstance(tools_value, Mapping)
        or not isinstance(backend_value, Mapping)
    ):
        raise ConfigurationError("managed workspace mount evidence is incomplete")
    sdk_path = sdk_value.get("path")
    target_tools_path = tools_value.get("path")
    backend_path = backend_value.get("path")
    if (
        not isinstance(sdk_path, str)
        or not isinstance(target_tools_path, str)
        or not isinstance(backend_path, str)
    ):
        raise ConfigurationError("managed workspace mount paths are invalid")
    if producer is None:
        sdk_info = _sdk_evidence(sdk_path, selection)
        tools_info = _target_tools_evidence(target_tools_path, sdk_info)
        backend_info = _compiler_backend_evidence(backend_path, selection)
    else:
        sdk_info, tools_info, backend_info = producer
        if sdk_info.arch != selection.target_arch or (
            selection.target_glibc_floor is not None
            and AbiVersion.parse(sdk_info.glibc_version)
            != AbiVersion.parse(selection.target_glibc_floor)
        ):
            raise ConfigurationError("paired runtime does not match the validated SDK")
    expected_sdk = _sdk_mount_value(sdk_info)
    if dict(sdk_value) != expected_sdk:
        raise ConfigurationError("managed SDK changed after workspace render")
    expected_tools = _target_tools_mount_value(tools_info)
    if dict(tools_value) != expected_tools:
        raise ConfigurationError("managed target tools changed after workspace render")
    expected_backend = _compiler_backend_mount_value(backend_info)
    if dict(backend_value) != expected_backend:
        raise ConfigurationError(
            "managed compiler backend changed after workspace render"
        )
    recorded_build_input = manifest.get("build_input")
    if not isinstance(recorded_build_input, Mapping):
        raise ConfigurationError("managed workspace build inputs are missing")
    recorded_script = recorded_build_input.get("script")
    if not isinstance(recorded_script, Mapping):
        raise ConfigurationError("managed workspace action script is missing")
    action_script = render_action_script(
        selection,
        triplet=sdk_info.triplet,
        backend_triplet=backend_info.triplet,
        backend_version=backend_info.gcc_version,
    )
    current_build_input = _build_input_value(
        selection,
        sdk_info,
        tools_info,
        backend_info,
        script=script_identity(action_script),
    )
    if manifest.get("build_input") != current_build_input:
        raise ConfigurationError("managed workspace build inputs do not match")
    build_script = manifest.get("build_script")
    script_path = root / "build" / "build.sh"
    if (
        not isinstance(build_script, Mapping)
        or build_script.get("path") != "build/build.sh"
        or not isinstance(build_script.get("paired_runtime"), bool)
        or build_script.get("sha256") != file_sha256(script_path)
    ):
        raise ConfigurationError("managed workspace execution script changed")
    return source, sdk_info, tools_info, backend_info


def fetch_source(
    lock_value: ManagedLock | Mapping[str, object] | object,
    artifact_id: str,
    workspace: Path | str,
    *,
    progress: ProgressCallback | None = None,
    transfer_progress: TransferProgressCallback | None = None,
) -> Path:
    lock = _managed_lock(lock_value)
    selection = select_artifact(lock, artifact_id)
    root, manifest = _load_workspace(workspace)
    _verify_workspace_selection(selection, manifest)
    destination = _source_cache_path(
        root, manifest.get("source_cache"), selection.source
    )
    if progress is not None:
        progress(
            "verifying cached managed source"
            if destination.exists()
            else "downloading and verifying managed source"
        )
    return _download_source_archive(
        selection.source, destination, progress=transfer_progress
    )


def _packaged_dockerfile_bytes() -> bytes:
    try:
        return (
            files("linux_toolchain.resources")
            .joinpath(BUILDER_DOCKERFILE_NAME)
            .read_bytes()
        )
    except OSError as error:
        raise ConfigurationError(
            f"cannot read packaged managed builder Dockerfile: {error}"
        ) from error


def _validate_dockerfile(dockerfile: Path) -> str:
    return validate_packaged_dockerfile(
        dockerfile,
        hashlib.sha256(_packaged_dockerfile_bytes()).hexdigest(),
        provenance="managed Dockerfile provenance",
    )


def _builder_build_args(apt_snapshot: str) -> dict[str, str]:
    return {
        "BASE_IMAGE": MANAGED_BUILDER_BASE_IMAGE,
        "UBUNTU_SNAPSHOT": apt_snapshot,
    }


def _write_container_identity(
    workspace: Path,
    host: BuilderHost,
) -> ContainerIdentityFiles:
    return write_container_identity_files(
        workspace,
        host,
        account_description="Managed builder",
        home="/output/home",
        shell="/bin/bash",
    )


def docker_run_command(
    *,
    image: str,
    source: Path,
    sdk: Path,
    target_tools: Path,
    compiler_backend: Path,
    compiler_backend_sources: Path,
    output: Path,
    script: Path,
    identity: ContainerIdentityFiles,
    platform: str,
    jobs: int = 1,
    runtime_output: Path | None = None,
    preserve_primary: bool = False,
    preserve_runtime: bool = False,
) -> list[str]:
    if not isinstance(jobs, int) or isinstance(jobs, bool) or not 1 <= jobs <= 256:
        raise ConfigurationError("managed build jobs must be between 1 and 256")
    if preserve_runtime and runtime_output is None:
        raise ConfigurationError("preserving a runtime requires paired output")
    mounts = [
        (source, "/sources/source.tar.xz", True),
        (sdk, "/sdk", True),
        (target_tools, "/target-tools", True),
        (compiler_backend, "/compiler-backend", True),
        (compiler_backend_sources, "/compiler-backend-sources", True),
        (script, "/build/build.sh", True),
        (identity.passwd, "/etc/passwd", True),
        (identity.group, "/etc/group", True),
        (output, "/output", False),
    ]
    if runtime_output is not None:
        mounts.append((runtime_output, "/runtime-output", False))
    command = [
        "docker",
        "run",
        "--rm",
        "--platform",
        platform,
        "--network=none",
        "--user",
        f"{identity.uid}:{identity.gid}",
        "--env",
        "HOME=/output/home",
        "--env",
        "LC_ALL=C",
        "--env",
        "LANG=C",
        "--env",
        "TZ=UTC",
        "--env",
        f"LINUX_TOOLCHAIN_JOBS={jobs}",
        "--env",
        f"LINUX_TOOLCHAIN_PRESERVE_PRIMARY={int(preserve_primary)}",
        "--env",
        f"LINUX_TOOLCHAIN_PRESERVE_RUNTIME={int(preserve_runtime)}",
        "--workdir",
        "/output",
    ]
    for source_path, destination, readonly in mounts:
        option = f"type=bind,src={source_path},dst={destination}"
        if readonly:
            option += ",readonly"
        command.extend(("--mount", option))
    command.extend((image, "/bin/bash", "/build/build.sh"))
    return command


def _preflight(build_platform: str) -> BuilderHost:
    host = require_non_root_builder("managed compiler production")
    docker = shutil.which("docker")
    if docker is None:
        raise ConfigurationError("Docker CLI is required for managed compiler builds")
    if not resolve_readelf_candidates(resolver=shutil.which):
        raise ConfigurationError("readelf is required for managed artifact audits")
    validate_native_docker_daemon(
        docker,
        build_platform,
        context="managed builds",
    )
    return host


def _image_provenance(image: BuilderImage) -> dict[str, object]:
    return {
        "id": image.image_id,
        "os": image.os,
        "architecture": image.architecture,
        "repo_digests": list(image.repo_digests),
    }


def _finalize_and_publish_artifact(
    lock: ManagedLock,
    root: Path,
    selection: ManagedBuildSelection,
    *,
    manifest: Mapping[str, object],
    image_provenance: Mapping[str, object],
    execution_script: Mapping[str, object] | None = None,
) -> Path:
    output = root / "output"
    staging = output / ".artifacts.staging"
    destination = output / "artifacts"
    _finalize_artifact(
        staging,
        selection,
        manifest=manifest,
        image_provenance=image_provenance,
        execution_script=execution_script,
    )

    def validate_final(published: Path) -> None:
        from linux_toolchain.managed.publication import (
            load_managed_compiler_artifact,
            load_managed_runtime_artifact,
        )

        if selection.artifact_kind == "compiler-kit":
            load_managed_compiler_artifact(lock, selection.artifact_id, published)
        else:
            load_managed_runtime_artifact(lock, selection.artifact_id, published)

    replace_directory(staging, destination, validate=validate_final)
    return destination / "artifact.json"


def build_with_docker(
    lock_value: ManagedLock | Mapping[str, object] | object,
    artifact_id: str,
    workspace: Path | str,
    *,
    dockerfile: Path | str | None = None,
    image: str | None = None,
    jobs: int = 1,
    progress: ProgressCallback | None = None,
    source_progress: TransferProgressCallback | None = None,
    paired_runtime_id: str | None = None,
    paired_runtime_workspace: Path | str | None = None,
    preserve_primary: bool = False,
    preserve_runtime: bool = False,
    _producer: ProducerEvidence | None = None,
) -> Path:
    lock = _managed_lock(lock_value)
    selection = select_artifact(lock, artifact_id)
    root, manifest = _load_workspace(workspace)
    source, sdk_info, tools_info, backend_info = _validated_workspace_inputs(
        selection, root, manifest, producer=_producer
    )
    script = root / "build" / "build.sh"
    if not script.is_file():
        raise ConfigurationError("managed build script is missing")

    paired = paired_runtime_id is not None or paired_runtime_workspace is not None
    if (paired_runtime_id is None) != (paired_runtime_workspace is None):
        raise ConfigurationError(
            "paired runtime requires both an artifact ID and workspace"
        )
    if (preserve_primary or preserve_runtime) and not paired:
        raise ConfigurationError("preserve flags require a paired build")
    if preserve_primary and preserve_runtime:
        raise ConfigurationError("a fully built pair must not enter Docker")
    build_script = manifest.get("build_script")
    if (
        not isinstance(build_script, Mapping)
        or build_script.get("paired_runtime") is not paired
    ):
        raise ConfigurationError("managed workspace paired build mode changed")

    runtime_root: Path | None = None
    runtime_manifest: dict[str, Any] | None = None
    runtime_selection: ManagedBuildSelection | None = None
    runtime_output: Path | None = None
    if paired:
        assert paired_runtime_id is not None
        assert paired_runtime_workspace is not None
        runtime_selection = select_artifact(lock, paired_runtime_id)
        runtime_root, runtime_manifest = _load_workspace(paired_runtime_workspace)
        (
            runtime_source,
            runtime_sdk,
            runtime_tools,
            runtime_backend,
        ) = _validated_workspace_inputs(
            runtime_selection,
            runtime_root,
            runtime_manifest,
            producer=(sdk_info, tools_info, backend_info),
        )
        if not (
            selection.artifact_kind == "compiler-kit"
            and runtime_selection.artifact_kind == "runtime"
            and runtime_selection.family == selection.family
            and runtime_selection.runtime_kind
            == ("gcc-runtime" if selection.family == "gcc" else "llvm-runtime")
            and runtime_selection.version == selection.version
            and runtime_selection.source == selection.source
            and runtime_selection.target_arch == selection.target_arch
        ):
            raise ConfigurationError(
                "paired build requires a matching Compiler Kit and runtime"
            )
        if (
            runtime_source != source
            or runtime_sdk != sdk_info
            or runtime_tools != tools_info
            or runtime_backend != backend_info
        ):
            raise ConfigurationError(
                "paired build workspaces must use the same source, SDK and tools"
            )
        runtime_output = (runtime_root / "output").resolve(strict=True)

    host = _preflight(selection.build_platform)
    if progress is not None:
        progress(
            "builder: verifying cached managed source"
            if source.exists()
            else "builder: downloading and verifying managed source"
        )
    _download_source_archive(selection.source, source, progress=source_progress)

    default_dockerfile = Path(
        str(files("linux_toolchain.resources").joinpath(BUILDER_DOCKERFILE_NAME))
    )
    try:
        dockerfile_path = (
            default_dockerfile if dockerfile is None else Path(dockerfile).expanduser()
        ).resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot access managed builder Dockerfile: {error}"
        ) from error
    dockerfile_sha256 = _validate_dockerfile(dockerfile_path)
    identity = _write_container_identity(root, host)
    context = root / "build" / "docker-context"
    context.mkdir(parents=True, exist_ok=True)
    context_dockerfile = context / BUILDER_DOCKERFILE_NAME
    context_dockerfile.write_bytes(_packaged_dockerfile_bytes())
    context_dockerfile.chmod(0o644)
    apt_snapshot = ubuntu_builder_snapshot()
    build_args = _builder_build_args(apt_snapshot)
    contract_digest = builder_image_contract_digest(
        dockerfile_sha256=dockerfile_sha256,
        base_image=MANAGED_BUILDER_BASE_IMAGE,
        pinned_input=apt_snapshot,
        platform=selection.build_platform,
        build_args=build_args,
        target=MANAGED_BUILDER_TARGET,
    )
    image_name = image or f"linux-toolchain-managed:{contract_digest[:16]}"

    def build_image() -> None:
        if progress is not None:
            progress("builder: preparing Docker image")
        run_streaming(
            docker_build_command(
                dockerfile=context_dockerfile,
                context=context,
                image=image_name,
                build_args=build_args,
                contract_digest=contract_digest,
                platform=selection.build_platform,
                target=MANAGED_BUILDER_TARGET,
            )
        )

    resolution = resolve_builder_image(
        image_name,
        contract_digest=contract_digest,
        platform=selection.build_platform,
        build=build_image,
    )
    if resolution.cache_hit and progress is not None:
        progress("builder: using cached Docker image")
    builder_image = resolution.image
    provenance = _image_provenance(builder_image)
    build_log = root / "build" / "managed-build.log"
    build_label = selection.artifact_id
    if runtime_selection is not None:
        build_label = f"{build_label} and {runtime_selection.artifact_id}"
    if progress is not None:
        progress(f"builder: compiling {build_label}; log: {build_log}")

    def report_heartbeat(elapsed: float) -> None:
        assert progress is not None
        message = f"builder: compiling {build_label}; elapsed: {int(elapsed)}s"
        tail = _build_log_tail(build_log)
        progress("\n".join((message, *tail)))

    container_command = docker_run_command(
        image=builder_image.image_id,
        source=source.resolve(strict=True),
        sdk=sdk_info.root,
        target_tools=tools_info.root,
        compiler_backend=backend_info.toolchain,
        compiler_backend_sources=backend_info.sources,
        output=(root / "output").resolve(strict=True),
        script=script.resolve(strict=True),
        identity=identity,
        platform=selection.build_platform,
        jobs=jobs,
        runtime_output=runtime_output,
        preserve_primary=preserve_primary,
        preserve_runtime=preserve_runtime,
    )
    owner = temporary_container_owner(root, "managed-compiler-build")
    cidfile = root / "build" / "managed-build.cid"
    with temporary_container_run(
        container_command,
        cidfile=cidfile,
        owner=owner,
    ) as (command, cancel):
        run_logged(
            command,
            build_log,
            heartbeat=report_heartbeat if progress is not None else None,
            heartbeat_interval=_BUILD_LOG_REFRESH_SECONDS,
            cancel=cancel,
        )
    if progress is not None:
        progress(f"builder: validating {build_label}")
    if preserve_primary:
        result = root / "output" / "artifacts" / "artifact.json"
    else:
        result = _finalize_and_publish_artifact(
            lock,
            root,
            selection,
            manifest=manifest,
            image_provenance=provenance,
        )
    if runtime_selection is not None:
        assert runtime_root is not None
        assert runtime_manifest is not None
        if not preserve_runtime:
            _finalize_and_publish_artifact(
                lock,
                runtime_root,
                runtime_selection,
                manifest=runtime_manifest,
                image_provenance=provenance,
                execution_script=(
                    manifest.get("build_script")
                    if isinstance(manifest.get("build_script"), Mapping)
                    else None
                ),
            )
    return result
