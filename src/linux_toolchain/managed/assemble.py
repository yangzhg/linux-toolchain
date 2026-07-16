from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from linux_toolchain.errors import ConfigurationError, LinuxToolchainError
from linux_toolchain.integrations import (
    DEFAULT_INTEGRATIONS,
    ConanSettings,
    IntegrationName,
)
from linux_toolchain.managed.builder import (
    ProducerEvidence,
    build_with_docker,
    render_workspace,
    validate_producer_inputs,
)
from linux_toolchain.managed.identity import (
    managed_action_sha256,
    managed_artifact_action_for_specs,
    runtime_publication_action,
)
from linux_toolchain.managed.lockfile import ManagedLock, VariantLock
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimeArtifact,
    ManagedRuntimePublication,
    _publish_managed_runtime_loaded,
    load_managed_compiler_artifact,
    load_managed_runtime_artifact,
    load_managed_runtime_publication,
)
from linux_toolchain.managed.selection import select_artifact
from linux_toolchain.models import SdkSpec
from linux_toolchain.sdk.crosstool_ng import load_workspace as load_sdk_workspace

ProgressCallback = Callable[[str], None]
TransferProgressCallback = Callable[[int, int], None]
_Artifact = TypeVar("_Artifact", ManagedCompilerArtifact, ManagedRuntimeArtifact)


@dataclass(frozen=True)
class AssemblyResult:
    """Validated artifacts produced for one managed lock variant."""

    variant_id: str
    compiler_kit: Path
    runtime: Path
    binding_manifest: Path

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "linux-toolchain-managed-assembly",
            "format": 1,
            "status": "ready",
            "variant": self.variant_id,
            "compiler_kit": str(self.compiler_kit),
            "runtime": str(self.runtime),
            "binding_manifest": str(self.binding_manifest),
        }


@dataclass(frozen=True)
class VariantArtifactPaths:
    compiler_kit_workspace: Path
    runtime_workspace: Path
    compiler_kit: Path
    raw_runtime: Path
    runtime: Path


def variant_artifact_paths(
    lock: ManagedLock,
    variant_id: str,
    workspace: Path,
    target_sdk: SdkSpec,
    compiler_backend: SdkSpec,
) -> VariantArtifactPaths:
    variant = _select_variant(lock, variant_id)
    kit = select_artifact(lock, variant.compiler_kit_id)
    runtime = select_artifact(lock, variant.runtime_id)
    kit_identity = managed_action_sha256(
        managed_artifact_action_for_specs(kit, target_sdk, compiler_backend)
    )
    runtime_identity = managed_action_sha256(
        managed_artifact_action_for_specs(runtime, target_sdk, compiler_backend)
    )
    publication_identity = managed_action_sha256(
        runtime_publication_action(
            runtime_identity,
            adapter=(
                "import_gcc_runtime"
                if runtime.runtime_kind == "gcc-runtime"
                else "import_llvm_runtime"
            ),
        )
    )
    kit_workspace = workspace / (
        f"compiler-{kit.family}-{kit.version}-{kit.target_arch}-{kit_identity[:16]}"
    )
    runtime_workspace = workspace / (
        f"runtime-{runtime.family}-{runtime.version}-{runtime.target_arch}-"
        f"{runtime_identity[:16]}"
    )
    publication = (
        workspace
        / "published"
        / (
            f"runtime-{runtime.family}-{runtime.version}-{runtime.target_arch}-"
            f"{publication_identity[:16]}"
        )
    )
    return VariantArtifactPaths(
        compiler_kit_workspace=kit_workspace,
        runtime_workspace=runtime_workspace,
        compiler_kit=kit_workspace / "output" / "artifacts",
        raw_runtime=runtime_workspace / "output" / "artifacts",
        runtime=publication,
    )


def _select_variant(lock: ManagedLock, variant_id: str) -> VariantLock:
    matches = tuple(item for item in lock.variants if item.id == variant_id)
    if len(matches) != 1:
        raise ConfigurationError(
            f"managed variant {variant_id!r} does not exist in the selected lock"
        )
    return matches[0]


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _artifact_readiness(
    loader: Callable[[ManagedLock, str, Path], _Artifact],
    lock: ManagedLock,
    artifact_id: str,
    artifact_root: Path,
    *,
    repair: bool,
) -> tuple[_Artifact | None, bool]:
    manifest = artifact_root / "artifact.json"
    if not manifest.exists():
        return None, False
    try:
        loaded = loader(lock, artifact_id, artifact_root)
    except LinuxToolchainError:
        if not repair:
            raise
        return None, True
    return loaded, False


def _validate_runtime_publication_payload(
    loaded: ManagedRuntimePublication,
) -> None:
    if loaded.selection.runtime_kind == "gcc-runtime":
        from linux_toolchain.runtime import validate_runtime_manifest

        validate_runtime_manifest(loaded.root, loaded.manifest)
    elif loaded.selection.runtime_kind == "llvm-runtime":
        from linux_toolchain.runtime import validate_llvm_runtime_manifest

        validate_llvm_runtime_manifest(loaded.root, loaded.manifest)
    else:
        raise ConfigurationError(
            "managed runtime publication has an unsupported runtime kind"
        )


def _build_artifact(
    lock: ManagedLock,
    artifact_id: str,
    workspace: Path,
    *,
    sdk: Path,
    target_tools: Path,
    compiler_backend: Path,
    source_cache: Path,
    producer: ProducerEvidence,
    jobs: int,
    dockerfile: Path | None,
    image: str | None,
    rebuild: bool,
    progress: ProgressCallback | None,
    source_progress: TransferProgressCallback | None,
) -> Path:
    manifest = workspace / "workspace.json"
    if rebuild or not manifest.is_file():
        _emit(progress, f"artifact: rendering {artifact_id}")
        render_workspace(
            lock,
            artifact_id,
            workspace,
            sdk=sdk,
            target_tools=target_tools,
            compiler_backend=compiler_backend,
            source_cache=source_cache,
            force=rebuild,
            _producer=producer,
        )
    else:
        _emit(progress, f"artifact: resuming {artifact_id}")

    built_manifest = build_with_docker(
        lock,
        artifact_id,
        workspace,
        dockerfile=dockerfile,
        image=image,
        jobs=jobs,
        progress=progress,
        source_progress=source_progress,
        _producer=producer,
    )
    return built_manifest.parent


def _workspace_uses_paired_build(workspace: Path) -> bool:
    try:
        value = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
        build_script = value.get("build_script") if isinstance(value, dict) else None
        return (
            isinstance(build_script, dict)
            and build_script.get("paired_runtime") is True
        )
    except (OSError, json.JSONDecodeError):
        return False


def _build_pair(
    lock: ManagedLock,
    variant: VariantLock,
    kit_workspace: Path,
    runtime_workspace: Path,
    *,
    sdk: Path,
    target_tools: Path,
    compiler_backend: Path,
    source_cache: Path,
    producer: ProducerEvidence,
    jobs: int,
    dockerfile: Path | None,
    image: str | None,
    rebuild_primary: bool,
    rebuild_runtime: bool,
    preserve_primary: bool,
    preserve_runtime: bool,
    progress: ProgressCallback | None,
    source_progress: TransferProgressCallback | None,
) -> tuple[Path, Path]:
    kit_manifest = kit_workspace / "workspace.json"
    runtime_manifest = runtime_workspace / "workspace.json"
    if rebuild_primary or not kit_manifest.is_file():
        _emit(progress, f"compiler kit: rendering {variant.compiler_kit_id}")
        render_workspace(
            lock,
            variant.compiler_kit_id,
            kit_workspace,
            sdk=sdk,
            target_tools=target_tools,
            compiler_backend=compiler_backend,
            source_cache=source_cache,
            force=rebuild_primary,
            paired_runtime=True,
            _producer=producer,
        )
    else:
        _emit(progress, f"compiler kit: resuming {variant.compiler_kit_id}")
    if rebuild_runtime or not runtime_manifest.is_file():
        _emit(progress, f"runtime: rendering {variant.runtime_id}")
        render_workspace(
            lock,
            variant.runtime_id,
            runtime_workspace,
            sdk=sdk,
            target_tools=target_tools,
            compiler_backend=compiler_backend,
            source_cache=source_cache,
            force=rebuild_runtime,
            _producer=producer,
        )
    else:
        _emit(progress, f"runtime: resuming {variant.runtime_id}")

    build_with_docker(
        lock,
        variant.compiler_kit_id,
        kit_workspace,
        dockerfile=dockerfile,
        image=image,
        jobs=jobs,
        progress=progress,
        source_progress=source_progress,
        paired_runtime_id=variant.runtime_id,
        paired_runtime_workspace=runtime_workspace,
        preserve_primary=preserve_primary,
        preserve_runtime=preserve_runtime,
        _producer=producer,
    )
    kit_root = kit_workspace / "output" / "artifacts"
    runtime_root = runtime_workspace / "output" / "artifacts"
    return kit_root, runtime_root


def assemble_variant(
    lock: ManagedLock,
    variant_id: str,
    sdk_workspace: Path | str,
    compiler_backend_workspace: Path | str,
    workspace: Path | str,
    output: Path | str,
    *,
    jobs: int = 1,
    integrations: tuple[IntegrationName, ...] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    dockerfile: Path | None = None,
    image: str | None = None,
    source_cache: Path | str | None = None,
    rebuild: bool = False,
    force: bool = False,
    repair: bool = False,
    progress: ProgressCallback | None = None,
    source_progress: TransferProgressCallback | None = None,
) -> AssemblyResult:
    """Build, publish and bind one variant selected from a managed lock.

    Completed artifacts are reused only after their manifests and payloads pass
    the same validation used by binding creation. An interrupted build resumes
    from its persistent source and build trees; ``rebuild`` recreates owned
    artifact workspaces. ``repair`` recreates only a same-selection artifact
    whose payload fails validation.
    """

    lock.validate()
    variant = _select_variant(lock, variant_id)

    raw_sdk_workspace = Path(sdk_workspace).expanduser()
    if raw_sdk_workspace.is_symlink():
        raise ConfigurationError(
            f"SDK workspace cannot be a symlink: {raw_sdk_workspace}"
        )
    sdk_workspace_path = raw_sdk_workspace.resolve()
    sdk = sdk_workspace_path / "sdk"
    target_tools = sdk_workspace_path / "toolchain" / "bin"
    raw_compiler_backend = Path(compiler_backend_workspace).expanduser()
    if raw_compiler_backend.is_symlink():
        raise ConfigurationError(
            f"compiler backend workspace cannot be a symlink: {raw_compiler_backend}"
        )
    compiler_backend = raw_compiler_backend.resolve()
    raw_workspace = Path(workspace).expanduser()
    if raw_workspace.is_symlink():
        raise ConfigurationError(
            f"assembly workspace cannot be a symlink: {raw_workspace}"
        )
    workspace_root = raw_workspace.resolve()
    if workspace_root in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid assembly workspace path: {workspace_root}")
    output_path = Path(output).expanduser()
    if output_path.is_symlink():
        raise ConfigurationError(f"binding output cannot be a symlink: {output_path}")
    kit_selection = select_artifact(lock, variant.compiler_kit_id)
    runtime_selection = select_artifact(lock, variant.runtime_id)
    target_sdk_spec = load_sdk_workspace(sdk_workspace_path)
    compiler_backend_spec = (
        target_sdk_spec
        if compiler_backend == sdk_workspace_path
        else load_sdk_workspace(compiler_backend)
    )
    paths = variant_artifact_paths(
        lock,
        variant.id,
        workspace_root,
        target_sdk_spec,
        compiler_backend_spec,
    )
    kit_workspace = paths.compiler_kit_workspace
    runtime_workspace = paths.runtime_workspace
    publication = paths.runtime
    if source_cache is None:
        source_cache_path = workspace_root / "sources"
    else:
        raw_source_cache = Path(source_cache).expanduser()
        if raw_source_cache.is_symlink():
            raise ConfigurationError(
                f"managed source cache cannot be a symlink: {raw_source_cache}"
            )
        source_cache_path = raw_source_cache.resolve()
    kit_root = paths.compiler_kit
    runtime_root = paths.raw_runtime
    matching_pair = kit_selection.family == runtime_selection.family
    compiler_artifact: ManagedCompilerArtifact | None = None
    if rebuild:
        kit_ready, rebuild_kit = False, True
    else:
        loaded_kit, rebuild_kit = _artifact_readiness(
            load_managed_compiler_artifact,
            lock,
            variant.compiler_kit_id,
            kit_root,
            repair=repair,
        )
        compiler_artifact = loaded_kit
        kit_ready = compiler_artifact is not None
    receipt = publication / "managed-publication.json"
    publication_ready = not rebuild and receipt.is_file()
    repair_publication = False
    loaded_publication: ManagedRuntimePublication | None = None
    if publication_ready:
        loaded_publication = load_managed_runtime_publication(
            lock,
            variant.runtime_id,
            publication,
        )
        if repair:
            try:
                _validate_runtime_publication_payload(loaded_publication)
            except LinuxToolchainError:
                publication_ready = False
                repair_publication = True
                loaded_publication = None
    if publication_ready:
        runtime_ready, rebuild_runtime = True, False
    elif rebuild:
        runtime_ready, rebuild_runtime = False, True
    else:
        loaded_runtime, rebuild_runtime = _artifact_readiness(
            load_managed_runtime_artifact,
            lock,
            variant.runtime_id,
            runtime_root,
            repair=repair,
        )
        runtime_ready = loaded_runtime is not None
    producer = (
        validate_producer_inputs(
            sdk,
            target_tools,
            compiler_backend,
            sdk_selection=runtime_selection,
            backend_selection=kit_selection,
        )
        if not kit_ready or not runtime_ready
        else None
    )
    fresh_pair = (
        not (kit_workspace / "workspace.json").exists()
        and not (runtime_workspace / "workspace.json").exists()
    )
    resume_pair = _workspace_uses_paired_build(kit_workspace)

    if matching_pair and kit_ready and runtime_ready:
        kit = kit_root
        runtime_artifact = runtime_root
    elif (
        not publication_ready
        and matching_pair
        and (rebuild or fresh_pair or resume_pair)
    ):
        assert producer is not None
        kit, runtime_artifact = _build_pair(
            lock,
            variant,
            kit_workspace,
            runtime_workspace,
            sdk=sdk,
            target_tools=target_tools,
            compiler_backend=compiler_backend,
            source_cache=source_cache_path,
            producer=producer,
            jobs=jobs,
            dockerfile=dockerfile,
            image=image,
            rebuild_primary=rebuild_kit,
            rebuild_runtime=rebuild_runtime,
            preserve_primary=kit_ready,
            preserve_runtime=runtime_ready,
            progress=progress,
            source_progress=source_progress,
        )
    else:
        kit = kit_root
        if not kit_ready:
            assert producer is not None
            kit = _build_artifact(
                lock,
                variant.compiler_kit_id,
                kit_workspace,
                sdk=sdk,
                target_tools=target_tools,
                compiler_backend=compiler_backend,
                source_cache=source_cache_path,
                producer=producer,
                jobs=jobs,
                dockerfile=dockerfile,
                image=image,
                rebuild=rebuild_kit,
                progress=progress,
                source_progress=source_progress,
            )
        runtime_artifact = runtime_root
        if not runtime_ready:
            assert producer is not None
            runtime_artifact = _build_artifact(
                lock,
                variant.runtime_id,
                runtime_workspace,
                sdk=sdk,
                target_tools=target_tools,
                compiler_backend=compiler_backend,
                source_cache=source_cache_path,
                producer=producer,
                jobs=jobs,
                dockerfile=dockerfile,
                image=image,
                rebuild=rebuild_runtime,
                progress=progress,
                source_progress=source_progress,
            )

    if publication_ready:
        _emit(progress, "runtime: using validated publication")
    else:
        _emit(progress, "runtime: publishing validated runtime overlay")
        loaded_publication = _publish_managed_runtime_loaded(
            lock,
            variant.runtime_id,
            runtime_artifact,
            publication,
            force=rebuild or repair_publication,
        )

    if compiler_artifact is None:
        compiler_artifact = load_managed_compiler_artifact(
            lock,
            variant.compiler_kit_id,
            kit,
        )
    assert loaded_publication is not None

    _emit(progress, "binding: validating and rendering integrations")
    # Import at call time so catalog and lockfile workflows do not initialize
    # the compiler-binding subsystem.
    from linux_toolchain.compiler.managed_binding import create_managed_binding

    binding_manifest = create_managed_binding(
        sdk,
        output_path,
        kit,
        lock=lock,
        variant=variant.id,
        runtime=publication,
        integrations=integrations,
        conan=conan,
        force=force,
        _compiler_artifact=compiler_artifact,
        _runtime_publication=loaded_publication,
    )
    return AssemblyResult(
        variant_id=variant.id,
        compiler_kit=kit,
        runtime=publication,
        binding_manifest=binding_manifest,
    )
