from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from linux_toolchain.build_tools import (
    BUILD_TOOLS_FORMAT,
    BUILD_TOOLS_SCHEMA,
    BuildToolsArtifact,
    BuildToolsSpec,
    build_tools_producer_identity,
    build_tools_script,
    build_tools_source_directories,
    build_tools_sources,
    expected_build_tool_records,
    load_build_tools,
)
from linux_toolchain.container import (
    BuilderHost,
    BuilderImage,
    ContainerIdentityFiles,
    ContainerMount,
    container_run_command,
    linux_platform_for_architecture,
    require_non_root_builder,
    temporary_container_owner,
    temporary_container_run,
    validate_native_docker_daemon,
    write_container_identity_files,
)
from linux_toolchain.elf.reader import resolve_readelf_candidates
from linux_toolchain.errors import ConfigurationError, LinuxToolchainError
from linux_toolchain.host_artifact import (
    audit_host_artifact,
    validate_artifact_symlinks,
)
from linux_toolchain.licenses import license_evidence
from linux_toolchain.models import SdkSpec
from linux_toolchain.process import read_log_tail, run_logged
from linux_toolchain.publication import replace_directory, write_json_atomic
from linux_toolchain.sdk.crosstool_ng import (
    FULL_BUILD_GOAL,
    acquire_workspace_builder_image,
    load_workspace,
    workspace_satisfies_build_goal,
)
from linux_toolchain.source_archive import (
    PinnedArchive,
    download_pinned_archive,
    validate_tar_archive,
)

_WORKSPACE_SCHEMA = "linux-toolchain-build-tools-workspace"
_WORKSPACE_FORMAT = 1
_WORKSPACE_MARKER = ".linux-toolchain-build-tools-workspace"
_WORKSPACE_MARKER_CONTENT = "format=1\n"
_WORKSPACE_MANIFEST = "workspace.json"
_ARTIFACT_DIRECTORY = "artifact"
_STAGING_DIRECTORY = ".artifact.staging"
_BUILD_LOG_REFRESH_SECONDS = 1.0
ProgressCallback = Callable[[str], None]
TransferProgressCallback = Callable[[int, int], None]


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _workspace_value(identity: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": _WORKSPACE_SCHEMA,
        "format": _WORKSPACE_FORMAT,
        "identity": dict(identity),
    }


def _prepare_workspace(
    path: Path,
    *,
    identity: Mapping[str, object],
) -> Path:
    raw = path.expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"build tools workspace cannot be a symlink: {raw}")
    root = raw.resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid build tools workspace: {root}")
    if root.exists():
        if not root.is_dir():
            raise ConfigurationError(
                f"build tools workspace is not a directory: {root}"
            )
        if next(root.iterdir(), None) is not None:
            marker = root / _WORKSPACE_MARKER
            manifest = root / _WORKSPACE_MANIFEST
            if (
                not _regular_file(marker)
                or marker.read_text(encoding="utf-8") != _WORKSPACE_MARKER_CONTENT
                or not _regular_file(manifest)
            ):
                raise ConfigurationError(
                    f"refusing to use unowned build tools workspace: {root}"
                )
            try:
                current = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ConfigurationError(
                    f"cannot read build tools workspace selection: {error}"
                ) from error
            if current != _workspace_value(identity):
                raise ConfigurationError(
                    "build tools workspace belongs to a different selection"
                )
            return root
    root.mkdir(parents=True, exist_ok=True)
    (root / _WORKSPACE_MARKER).write_text(
        _WORKSPACE_MARKER_CONTENT,
        encoding="utf-8",
    )
    write_json_atomic(
        root / _WORKSPACE_MANIFEST,
        _workspace_value(identity),
        replace=False,
    )
    return root


def _validate_compiler_backend(
    spec: BuildToolsSpec,
    compiler_backend: SdkSpec,
    workspace: Path,
) -> None:
    if load_workspace(workspace) != compiler_backend:
        raise ConfigurationError(
            "build tools compiler backend workspace has a different selection"
        )
    build_tools_producer_identity(spec, compiler_backend)
    if not workspace_satisfies_build_goal(
        compiler_backend,
        workspace,
        FULL_BUILD_GOAL,
    ):
        raise ConfigurationError(
            "build tools require a completed compiler backend workspace"
        )


def _preflight(spec: BuildToolsSpec) -> BuilderHost:
    host = require_non_root_builder("build tools production")
    docker = shutil.which("docker")
    if docker is None:
        raise ConfigurationError("Docker CLI is required for build tools production")
    if not resolve_readelf_candidates(resolver=shutil.which):
        raise ConfigurationError("readelf is required for build tools audits")
    validate_native_docker_daemon(
        docker,
        linux_platform_for_architecture(spec.arch),
        context="build tools production",
    )
    return host


def _download_sources(
    spec: BuildToolsSpec,
    workspace: Path,
    *,
    source_cache: Path,
    progress: ProgressCallback | None,
    source_progress: TransferProgressCallback | None,
) -> dict[str, tuple[PinnedArchive, Path]]:
    downloaded: dict[str, tuple[PinnedArchive, Path]] = {}
    directories = build_tools_source_directories(spec)
    for name, archive in build_tools_sources(spec).items():
        _emit(progress, f"build tools: acquiring {name} {archive.filename}")
        path = download_pinned_archive(
            archive,
            workspace,
            description=f"{name} source archive",
            source_cache=source_cache,
            progress=source_progress,
        )
        validate_tar_archive(
            path,
            top_directory=directories[name],
            context=f"{name} source archive",
        )
        downloaded[name] = (archive, path)
    return downloaded


def _write_script(
    workspace: Path,
    *,
    spec: BuildToolsSpec,
    compiler_backend: SdkSpec,
) -> Path:
    path = workspace / "build" / "build.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = build_tools_script(spec, compiler_backend.target.triplet)
    if path.exists() or path.is_symlink():
        if not _regular_file(path) or path.read_text(encoding="utf-8") != expected:
            raise ConfigurationError(
                "build tools workspace contains a different build script"
            )
    else:
        path.write_text(expected, encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def _write_identity(
    workspace: Path,
    host: BuilderHost,
) -> ContainerIdentityFiles:
    return write_container_identity_files(
        workspace,
        host,
        account_description="Build tools producer",
        home="/work/home",
        shell="/bin/bash",
    )


def _prepare_directory(path: Path, *, context: str) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ConfigurationError(f"{context} is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _prepare_staging_directory(workspace: Path) -> Path:
    path = workspace / _STAGING_DIRECTORY
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ConfigurationError(f"build tools staging output is invalid: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()
    return path.resolve()


def _docker_run_command(
    *,
    image: BuilderImage,
    platform: str,
    compiler_backend_workspace: Path,
    downloads: Mapping[str, tuple[PinnedArchive, Path]],
    script: Path,
    identity: ContainerIdentityFiles,
    work: Path,
    output: Path,
    jobs: int,
) -> list[str]:
    if not isinstance(jobs, int) or isinstance(jobs, bool) or not 1 <= jobs <= 256:
        raise ConfigurationError("build tools jobs must be between 1 and 256")
    mounts = [
        ContainerMount(compiler_backend_workspace, "/compiler-backend", read_only=True),
        ContainerMount(script, "/build/build.sh", read_only=True),
        ContainerMount(work, "/work"),
        ContainerMount(output, "/output"),
    ]
    mounts.extend(
        ContainerMount(path, f"/sources/{archive.filename}", read_only=True)
        for archive, path in downloads.values()
    )
    return container_run_command(
        image=image.image_id,
        platform=platform,
        identity=identity,
        home="/work/home",
        workdir="/work",
        mounts=mounts,
        environment={"LINUX_TOOLCHAIN_JOBS": str(jobs)},
        argv=("/bin/bash", "/build/build.sh"),
    )


def _validate_outputs(root: Path, spec: BuildToolsSpec) -> None:
    for name, record in expected_build_tool_records(spec).items():
        path = root / str(record["path"])
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o111 == 0:
            raise ConfigurationError(
                f"build tools production did not create {name}: {path}"
            )


def _finalize_artifact(
    staging: Path,
    *,
    spec: BuildToolsSpec,
    identity: Mapping[str, object],
    image: BuilderImage,
) -> None:
    _validate_outputs(staging, spec)
    validate_artifact_symlinks(staging, context="build tools artifact")
    audit = audit_host_artifact(
        staging,
        arch=spec.arch,
        glibc_floor=spec.glibc_floor,
        context="build tools",
    )
    manifest = {
        "schema": BUILD_TOOLS_SCHEMA,
        "format": BUILD_TOOLS_FORMAT,
        "identity": dict(identity),
        "tools": expected_build_tool_records(spec),
        "builder_image": image.to_dict(),
        "elf_audit": audit,
        "licenses": license_evidence(staging, context="build tools"),
    }
    write_json_atomic(staging / "manifest.json", manifest)


def build_build_tools(
    spec: BuildToolsSpec,
    compiler_backend: SdkSpec,
    compiler_backend_workspace: Path | str,
    workspace: Path | str,
    *,
    source_cache: Path,
    jobs: int = 1,
    force: bool = False,
    progress: ProgressCallback | None = None,
    source_progress: TransferProgressCallback | None = None,
) -> BuildToolsArtifact:
    """Build and publish one architecture-specific host build-tools artifact."""

    spec.validate()
    backend_workspace = Path(compiler_backend_workspace).expanduser().resolve()
    _validate_compiler_backend(spec, compiler_backend, backend_workspace)
    identity = build_tools_producer_identity(spec, compiler_backend)
    root = _prepare_workspace(Path(workspace), identity=identity)
    destination = root / _ARTIFACT_DIRECTORY
    if destination.exists():
        try:
            existing = load_build_tools(
                destination,
                expected_identity=identity,
            )
        except LinuxToolchainError:
            if not force:
                raise
        else:
            _emit(progress, "build tools: using validated existing artifact")
            return existing

    host = _preflight(spec)
    downloads = _download_sources(
        spec,
        root,
        source_cache=source_cache,
        progress=progress,
        source_progress=source_progress,
    )
    script = _write_script(
        root,
        spec=spec,
        compiler_backend=compiler_backend,
    )
    image = acquire_workspace_builder_image(
        compiler_backend,
        backend_workspace,
        jobs=jobs,
        progress=progress,
        source_cache=source_cache,
        source_progress=source_progress,
    )
    work = _prepare_directory(root / "build" / "work", context="build tools work")
    staging = _prepare_staging_directory(root)
    container_identity = _write_identity(root, host)
    command = _docker_run_command(
        image=image,
        platform=linux_platform_for_architecture(spec.arch),
        compiler_backend_workspace=backend_workspace,
        downloads=downloads,
        script=script,
        identity=container_identity,
        work=work,
        output=staging,
        jobs=jobs,
    )
    cidfile = root / "build" / "container.cid"
    owner = temporary_container_owner(root, "build-tools")
    log = root / "build" / "build.log"

    def heartbeat(elapsed: float) -> None:
        message = (
            f"build tools: building CMake and native tools; elapsed: {int(elapsed)}s"
        )
        _emit(progress, "\n".join((message, *read_log_tail(log))))

    _emit(progress, f"build tools: building CMake and native tools (log: {log})")
    with temporary_container_run(
        command,
        cidfile=cidfile,
        owner=owner,
    ) as (guarded, cancel):
        run_logged(
            guarded,
            log,
            heartbeat=heartbeat if progress is not None else None,
            heartbeat_interval=_BUILD_LOG_REFRESH_SECONDS,
            cancel=cancel,
        )
    _finalize_artifact(
        staging,
        spec=spec,
        identity=identity,
        image=image,
    )

    def validate(published: Path) -> BuildToolsArtifact:
        return load_build_tools(published, expected_identity=identity)

    result = replace_directory(staging, destination, validate=validate)
    assert result is not None
    _emit(progress, "build tools: artifact ready")
    return result
