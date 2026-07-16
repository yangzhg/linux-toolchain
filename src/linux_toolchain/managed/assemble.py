from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
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
    runtime_set_action,
)
from linux_toolchain.managed.lockfile import (
    ManagedLock,
    VariantLock,
    VariantRuntimeLock,
)
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimeArtifact,
    ManagedRuntimePublication,
    load_managed_compiler_artifact,
    load_managed_runtime_artifact,
    load_managed_runtime_publication,
    publish_managed_runtime_publication,
    publish_managed_runtime_set,
)
from linux_toolchain.managed.selection import ManagedBuildSelection, select_artifact
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
    compiler_kit: Path
    runtimes: tuple["RuntimeArtifactPaths", ...]
    runtime_set: Path

    def runtime(self, artifact_id: str) -> "RuntimeArtifactPaths":
        matches = tuple(
            runtime for runtime in self.runtimes if runtime.artifact_id == artifact_id
        )
        if len(matches) != 1:
            raise ConfigurationError(
                f"managed variant has no unique runtime path for {artifact_id}"
            )
        return matches[0]


@dataclass(frozen=True)
class RuntimeArtifactPaths:
    artifact_id: str
    workspace: Path
    raw: Path
    publication: Path


class ArtifactDisposition(str, Enum):
    REUSE = "reuse"
    BUILD = "build"
    REBUILD = "rebuild"


class BuildMode(str, Enum):
    CREATE = "create"
    REBUILD = "rebuild"
    PRESERVE = "preserve"


class PublicationDisposition(str, Enum):
    REUSE = "reuse"
    PUBLISH = "publish"
    REPLACE = "replace"


class PairAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CompilerAssemblyState:
    artifact_id: str
    disposition: ArtifactDisposition


@dataclass(frozen=True)
class RuntimeAssemblyState:
    runtime: VariantRuntimeLock
    provider_compiler_kit_id: str
    artifact: ArtifactDisposition
    publication: PublicationDisposition
    pairing: PairAvailability

    def __post_init__(self) -> None:
        if (
            self.publication is PublicationDisposition.REUSE
            and self.artifact is not ArtifactDisposition.REUSE
        ):
            raise ConfigurationError(
                "a reusable runtime publication requires a reusable runtime artifact"
            )


@dataclass(frozen=True)
class PairedBuildAction:
    compiler_kit_id: str
    runtime: VariantRuntimeLock
    compiler_mode: BuildMode
    runtime_mode: BuildMode


@dataclass(frozen=True)
class StandaloneBuildAction:
    artifact_id: str
    mode: BuildMode

    def __post_init__(self) -> None:
        if self.mode is BuildMode.PRESERVE:
            raise ConfigurationError("a standalone build cannot preserve an artifact")


@dataclass(frozen=True)
class RuntimePublicationAction:
    artifact_id: str
    disposition: PublicationDisposition

    def __post_init__(self) -> None:
        if self.disposition is PublicationDisposition.REUSE:
            raise ConfigurationError(
                "a publication action cannot reuse an existing publication"
            )

    @property
    def force(self) -> bool:
        return self.disposition is PublicationDisposition.REPLACE


@dataclass(frozen=True)
class AssemblyPlan:
    builds: tuple[PairedBuildAction | StandaloneBuildAction, ...]
    publications: tuple[RuntimePublicationAction, ...]

    @property
    def needs_producer(self) -> bool:
        return bool(self.builds)


@dataclass(frozen=True)
class _CompilerArtifactContext:
    workspace: Path
    state: CompilerAssemblyState


@dataclass(frozen=True)
class _AssemblyContext:
    lock: ManagedLock
    variant: VariantLock
    kit_selection: ManagedBuildSelection
    sdk: Path
    target_tools: Path
    compiler_backend: Path
    workspace: Path
    output: Path
    source_cache: Path
    paths: VariantArtifactPaths
    target_sdk_spec: SdkSpec
    compiler_backend_spec: SdkSpec


@dataclass(frozen=True)
class _AssemblyBuildOptions:
    jobs: int
    dockerfile: Path | None
    image: str | None
    progress: ProgressCallback | None
    source_progress: TransferProgressCallback | None


@dataclass(frozen=True)
class _AssemblyState:
    compiler_artifact: ManagedCompilerArtifact | None
    compiler_contexts: dict[str, _CompilerArtifactContext]
    runtime_states: tuple[RuntimeAssemblyState, ...]
    loaded_publications: dict[str, ManagedRuntimePublication]


@dataclass(frozen=True)
class _BuiltArtifacts:
    compiler_kit: Path
    runtimes: dict[str, Path]
    paired_runtime_ids: frozenset[str]


def _build_mode(disposition: ArtifactDisposition) -> BuildMode:
    if disposition is ArtifactDisposition.REUSE:
        return BuildMode.PRESERVE
    if disposition is ArtifactDisposition.REBUILD:
        return BuildMode.REBUILD
    return BuildMode.CREATE


def plan_assembly(
    selected_compiler_kit_id: str,
    compiler_states: tuple[CompilerAssemblyState, ...],
    runtime_states: tuple[RuntimeAssemblyState, ...],
) -> AssemblyPlan:
    """Return the deterministic build and publication work for one variant."""

    compiler_by_id = {state.artifact_id: state for state in compiler_states}
    if len(compiler_by_id) != len(compiler_states):
        raise ConfigurationError("managed assembly compiler states are not unique")
    if selected_compiler_kit_id not in compiler_by_id:
        raise ConfigurationError(
            "managed assembly is missing the selected Compiler Kit state"
        )

    runtime_ids = [state.runtime.runtime_id for state in runtime_states]
    if len(set(runtime_ids)) != len(runtime_ids):
        raise ConfigurationError("managed assembly runtime states are not unique")
    for state in runtime_states:
        if (
            state.artifact is not ArtifactDisposition.REUSE
            and state.provider_compiler_kit_id not in compiler_by_id
        ):
            raise ConfigurationError(
                "managed assembly is missing runtime provider Compiler Kit state"
            )

    compiler_ready = {
        artifact_id: state.disposition is ArtifactDisposition.REUSE
        for artifact_id, state in compiler_by_id.items()
    }
    runtime_ready = {
        state.runtime.runtime_id: state.artifact is ArtifactDisposition.REUSE
        for state in runtime_states
    }
    builds: list[PairedBuildAction | StandaloneBuildAction] = []

    for state in runtime_states:
        if state.provider_compiler_kit_id != selected_compiler_kit_id:
            continue
        runtime_id = state.runtime.runtime_id
        if (
            state.publication is PublicationDisposition.REUSE
            or (compiler_ready[selected_compiler_kit_id] and runtime_ready[runtime_id])
            or state.pairing is PairAvailability.UNAVAILABLE
        ):
            continue
        compiler_state = compiler_by_id[selected_compiler_kit_id]
        builds.append(
            PairedBuildAction(
                compiler_kit_id=selected_compiler_kit_id,
                runtime=state.runtime,
                compiler_mode=(
                    BuildMode.PRESERVE
                    if compiler_ready[selected_compiler_kit_id]
                    else _build_mode(compiler_state.disposition)
                ),
                runtime_mode=(
                    BuildMode.PRESERVE
                    if runtime_ready[runtime_id]
                    else _build_mode(state.artifact)
                ),
            )
        )
        compiler_ready[selected_compiler_kit_id] = True
        runtime_ready[runtime_id] = True

    if not compiler_ready[selected_compiler_kit_id]:
        builds.append(
            StandaloneBuildAction(
                artifact_id=selected_compiler_kit_id,
                mode=_build_mode(compiler_by_id[selected_compiler_kit_id].disposition),
            )
        )
        compiler_ready[selected_compiler_kit_id] = True

    for state in runtime_states:
        runtime_id = state.runtime.runtime_id
        provider_id = state.provider_compiler_kit_id
        if (
            runtime_ready[runtime_id]
            or provider_id == selected_compiler_kit_id
            or state.pairing is PairAvailability.UNAVAILABLE
        ):
            continue
        provider_state = compiler_by_id[provider_id]
        builds.append(
            PairedBuildAction(
                compiler_kit_id=provider_id,
                runtime=state.runtime,
                compiler_mode=(
                    BuildMode.PRESERVE
                    if compiler_ready[provider_id]
                    else _build_mode(provider_state.disposition)
                ),
                runtime_mode=_build_mode(state.artifact),
            )
        )
        compiler_ready[provider_id] = True
        runtime_ready[runtime_id] = True

    for state in runtime_states:
        runtime_id = state.runtime.runtime_id
        if runtime_ready[runtime_id]:
            continue
        builds.append(
            StandaloneBuildAction(
                artifact_id=runtime_id,
                mode=_build_mode(state.artifact),
            )
        )
        runtime_ready[runtime_id] = True

    return AssemblyPlan(
        builds=tuple(builds),
        publications=tuple(
            RuntimePublicationAction(
                artifact_id=state.runtime.runtime_id,
                disposition=state.publication,
            )
            for state in runtime_states
            if state.publication is not PublicationDisposition.REUSE
        ),
    )


def _compiler_kit_paths(
    lock: ManagedLock,
    artifact_id: str,
    workspace: Path,
    target_sdk: SdkSpec,
    compiler_backend: SdkSpec,
) -> tuple[Path, Path]:
    selection = select_artifact(lock, artifact_id)
    if selection.artifact_kind != "compiler-kit":
        raise ConfigurationError(f"{artifact_id} is not a Compiler Kit")
    identity = managed_action_sha256(
        managed_artifact_action_for_specs(
            selection,
            target_sdk,
            compiler_backend,
        )
    )
    root = workspace / (
        f"compiler-{selection.family}-{selection.version}-"
        f"{selection.target_arch}-{identity[:16]}"
    )
    return root, root / "output" / "artifacts"


def variant_artifact_paths(
    lock: ManagedLock,
    variant_id: str,
    workspace: Path,
    target_sdk: SdkSpec,
    compiler_backend: SdkSpec,
) -> VariantArtifactPaths:
    variant = lock.variant(variant_id)
    kit_workspace, kit = _compiler_kit_paths(
        lock,
        variant.compiler_kit_id,
        workspace,
        target_sdk,
        compiler_backend,
    )
    runtime_paths: list[RuntimeArtifactPaths] = []
    publication_identities: dict[str, str] = {}
    for runtime_ref in variant.runtimes:
        runtime = select_artifact(lock, runtime_ref.runtime_id)
        runtime_identity = managed_action_sha256(
            managed_artifact_action_for_specs(
                runtime,
                target_sdk,
                compiler_backend,
            )
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
        runtime_paths.append(
            RuntimeArtifactPaths(
                artifact_id=runtime_ref.runtime_id,
                workspace=runtime_workspace,
                raw=runtime_workspace / "output" / "artifacts",
                publication=publication,
            )
        )
        publication_identities[runtime_ref.runtime_id] = publication_identity
    runtime_set_identity = managed_action_sha256(
        runtime_set_action(
            lock.sha256,
            variant.to_dict(),
            publication_identities,
        )
    )
    return VariantArtifactPaths(
        compiler_kit_workspace=kit_workspace,
        compiler_kit=kit,
        runtimes=tuple(runtime_paths),
        runtime_set=(
            workspace
            / "published"
            / f"runtime-set-{variant.family}-{variant.version}-{runtime_set_identity[:16]}"
        ),
    )


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


def _artifact_disposition(
    artifact: ManagedCompilerArtifact | ManagedRuntimeArtifact | None,
    *,
    rebuild: bool,
) -> ArtifactDisposition:
    if artifact is not None:
        return ArtifactDisposition.REUSE
    if rebuild:
        return ArtifactDisposition.REBUILD
    return ArtifactDisposition.BUILD


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
    context: _AssemblyContext,
    options: _AssemblyBuildOptions,
    artifact_id: str,
    workspace: Path,
    *,
    producer: ProducerEvidence,
    rebuild: bool,
) -> Path:
    manifest = workspace / "workspace.json"
    if rebuild or not manifest.is_file():
        _emit(options.progress, f"artifact: rendering {artifact_id}")
        render_workspace(
            context.lock,
            artifact_id,
            workspace,
            sdk=context.sdk,
            target_tools=context.target_tools,
            compiler_backend=context.compiler_backend,
            source_cache=context.source_cache,
            force=rebuild,
            _producer=producer,
        )
    else:
        _emit(options.progress, f"artifact: resuming {artifact_id}")

    built_manifest = build_with_docker(
        context.lock,
        artifact_id,
        workspace,
        dockerfile=options.dockerfile,
        image=options.image,
        jobs=options.jobs,
        progress=options.progress,
        source_progress=options.source_progress,
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
    context: _AssemblyContext,
    options: _AssemblyBuildOptions,
    compiler_kit_id: str,
    runtime_ref: VariantRuntimeLock,
    kit_workspace: Path,
    runtime_workspace: Path,
    *,
    producer: ProducerEvidence,
    rebuild_primary: bool,
    rebuild_runtime: bool,
    preserve_primary: bool,
    preserve_runtime: bool,
) -> tuple[Path, Path]:
    kit_manifest = kit_workspace / "workspace.json"
    runtime_manifest = runtime_workspace / "workspace.json"
    if rebuild_primary or not kit_manifest.is_file():
        _emit(options.progress, f"compiler kit: rendering {compiler_kit_id}")
        render_workspace(
            context.lock,
            compiler_kit_id,
            kit_workspace,
            sdk=context.sdk,
            target_tools=context.target_tools,
            compiler_backend=context.compiler_backend,
            source_cache=context.source_cache,
            force=rebuild_primary,
            paired_runtime=True,
            _producer=producer,
        )
    else:
        _emit(options.progress, f"compiler kit: resuming {compiler_kit_id}")
    if rebuild_runtime or not runtime_manifest.is_file():
        _emit(options.progress, f"runtime: rendering {runtime_ref.runtime_id}")
        render_workspace(
            context.lock,
            runtime_ref.runtime_id,
            runtime_workspace,
            sdk=context.sdk,
            target_tools=context.target_tools,
            compiler_backend=context.compiler_backend,
            source_cache=context.source_cache,
            force=rebuild_runtime,
            _producer=producer,
        )
    else:
        _emit(options.progress, f"runtime: resuming {runtime_ref.runtime_id}")

    build_with_docker(
        context.lock,
        compiler_kit_id,
        kit_workspace,
        dockerfile=options.dockerfile,
        image=options.image,
        jobs=options.jobs,
        progress=options.progress,
        source_progress=options.source_progress,
        paired_runtime_id=runtime_ref.runtime_id,
        paired_runtime_workspace=runtime_workspace,
        preserve_primary=preserve_primary,
        preserve_runtime=preserve_runtime,
        _producer=producer,
    )
    kit_root = kit_workspace / "output" / "artifacts"
    runtime_root = runtime_workspace / "output" / "artifacts"
    return kit_root, runtime_root


def _resolved_input_path(value: Path | str, context: str) -> Path:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"{context} cannot be a symlink: {raw}")
    return raw.resolve()


def _resolve_assembly_context(
    lock: ManagedLock,
    variant_id: str,
    sdk_workspace: Path | str,
    compiler_backend_workspace: Path | str,
    workspace: Path | str,
    output: Path | str,
    source_cache: Path | str | None,
) -> _AssemblyContext:
    variant = lock.variant(variant_id)
    sdk_workspace_path = _resolved_input_path(sdk_workspace, "SDK workspace")
    compiler_backend = _resolved_input_path(
        compiler_backend_workspace,
        "compiler backend workspace",
    )
    workspace_root = _resolved_input_path(workspace, "assembly workspace")
    if workspace_root in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid assembly workspace path: {workspace_root}")

    output_path = Path(output).expanduser()
    if output_path.is_symlink():
        raise ConfigurationError(f"binding output cannot be a symlink: {output_path}")
    source_cache_path = (
        workspace_root / "sources"
        if source_cache is None
        else _resolved_input_path(source_cache, "managed source cache")
    )

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
    return _AssemblyContext(
        lock=lock,
        variant=variant,
        kit_selection=select_artifact(lock, variant.compiler_kit_id),
        sdk=sdk_workspace_path / "sdk",
        target_tools=sdk_workspace_path / "toolchain" / "bin",
        compiler_backend=compiler_backend,
        workspace=workspace_root,
        output=output_path,
        source_cache=source_cache_path,
        paths=paths,
        target_sdk_spec=target_sdk_spec,
        compiler_backend_spec=compiler_backend_spec,
    )


def _compiler_artifact_state(
    context: _AssemblyContext,
    artifact_id: str,
    workspace: Path,
    root: Path,
    *,
    rebuild: bool,
    repair: bool,
) -> tuple[_CompilerArtifactContext, ManagedCompilerArtifact | None]:
    loaded: ManagedCompilerArtifact | None = None
    if rebuild:
        disposition = ArtifactDisposition.REBUILD
    else:
        loaded, needs_rebuild = _artifact_readiness(
            load_managed_compiler_artifact,
            context.lock,
            artifact_id,
            root,
            repair=repair,
        )
        disposition = _artifact_disposition(loaded, rebuild=needs_rebuild)
    return (
        _CompilerArtifactContext(
            workspace=workspace,
            state=CompilerAssemblyState(
                artifact_id=artifact_id,
                disposition=disposition,
            ),
        ),
        loaded,
    )


def _discover_assembly_state(
    context: _AssemblyContext,
    *,
    rebuild: bool,
    repair: bool,
) -> _AssemblyState:
    variant = context.variant
    paths = context.paths
    selected_context, compiler_artifact = _compiler_artifact_state(
        context,
        variant.compiler_kit_id,
        paths.compiler_kit_workspace,
        paths.compiler_kit,
        rebuild=rebuild,
        repair=repair,
    )
    compiler_contexts = {variant.compiler_kit_id: selected_context}

    loaded_publications: dict[str, ManagedRuntimePublication] = {}
    publication_dispositions: dict[str, PublicationDisposition] = {}
    runtime_dispositions: dict[str, ArtifactDisposition] = {}
    for runtime_ref in variant.runtimes:
        artifact_id = runtime_ref.runtime_id
        runtime_paths = paths.runtime(artifact_id)
        ready = (
            not rebuild
            and (runtime_paths.publication / "managed-publication.json").is_file()
        )
        replace_publication = rebuild
        if ready:
            loaded = load_managed_runtime_publication(
                context.lock,
                artifact_id,
                runtime_paths.publication,
            )
            if repair:
                try:
                    _validate_runtime_publication_payload(loaded)
                except LinuxToolchainError:
                    ready = False
                    replace_publication = True
                else:
                    loaded_publications[artifact_id] = loaded
            else:
                loaded_publications[artifact_id] = loaded
        if ready:
            publication_dispositions[artifact_id] = PublicationDisposition.REUSE
            runtime_dispositions[artifact_id] = ArtifactDisposition.REUSE
            continue
        if rebuild:
            publication_dispositions[artifact_id] = PublicationDisposition.REPLACE
            runtime_dispositions[artifact_id] = ArtifactDisposition.REBUILD
            continue

        loaded_runtime, needs_rebuild = _artifact_readiness(
            load_managed_runtime_artifact,
            context.lock,
            artifact_id,
            runtime_paths.raw,
            repair=repair,
        )
        publication_dispositions[artifact_id] = (
            PublicationDisposition.REPLACE
            if replace_publication
            else PublicationDisposition.PUBLISH
        )
        runtime_dispositions[artifact_id] = _artifact_disposition(
            loaded_runtime,
            rebuild=needs_rebuild,
        )

    runtime_states: list[RuntimeAssemblyState] = []
    for runtime_ref in variant.runtimes:
        artifact_id = runtime_ref.runtime_id
        provider_id = context.lock.provider_compiler_kit(artifact_id).id
        runtime_paths = paths.runtime(artifact_id)
        if (
            runtime_dispositions[artifact_id] is not ArtifactDisposition.REUSE
            and provider_id not in compiler_contexts
        ):
            provider_workspace, provider_root = _compiler_kit_paths(
                context.lock,
                provider_id,
                context.workspace,
                context.target_sdk_spec,
                context.compiler_backend_spec,
            )
            provider_context, _ = _compiler_artifact_state(
                context,
                provider_id,
                provider_workspace,
                provider_root,
                rebuild=rebuild,
                repair=repair,
            )
            compiler_contexts[provider_id] = provider_context

        provider_context = compiler_contexts.get(provider_id)
        pairing = PairAvailability.UNAVAILABLE
        if provider_context is not None and (
            rebuild
            or (
                not (provider_context.workspace / "workspace.json").exists()
                and not (runtime_paths.workspace / "workspace.json").exists()
            )
            or _workspace_uses_paired_build(provider_context.workspace)
        ):
            pairing = PairAvailability.AVAILABLE
        runtime_states.append(
            RuntimeAssemblyState(
                runtime=runtime_ref,
                provider_compiler_kit_id=provider_id,
                artifact=runtime_dispositions[artifact_id],
                publication=publication_dispositions[artifact_id],
                pairing=pairing,
            )
        )

    return _AssemblyState(
        compiler_artifact=compiler_artifact,
        compiler_contexts=compiler_contexts,
        runtime_states=tuple(runtime_states),
        loaded_publications=loaded_publications,
    )


def _execute_build_plan(
    context: _AssemblyContext,
    state: _AssemblyState,
    plan: AssemblyPlan,
    options: _AssemblyBuildOptions,
) -> _BuiltArtifacts:
    compiler_kit = context.paths.compiler_kit
    runtimes = {runtime.artifact_id: runtime.raw for runtime in context.paths.runtimes}
    if not plan.needs_producer:
        return _BuiltArtifacts(
            compiler_kit=compiler_kit,
            runtimes=runtimes,
            paired_runtime_ids=frozenset(),
        )

    producer = validate_producer_inputs(
        context.sdk,
        context.target_tools,
        context.compiler_backend,
        sdk_selection=context.kit_selection,
        backend_selection=context.kit_selection,
    )
    paired_runtime_ids: set[str] = set()
    for action in plan.builds:
        if isinstance(action, PairedBuildAction):
            runtime_id = action.runtime.runtime_id
            runtime_paths = context.paths.runtime(runtime_id)
            compiler_context = state.compiler_contexts[action.compiler_kit_id]
            built_compiler, runtimes[runtime_id] = _build_pair(
                context,
                options,
                action.compiler_kit_id,
                action.runtime,
                compiler_context.workspace,
                runtime_paths.workspace,
                producer=producer,
                rebuild_primary=action.compiler_mode is BuildMode.REBUILD,
                rebuild_runtime=action.runtime_mode is BuildMode.REBUILD,
                preserve_primary=action.compiler_mode is BuildMode.PRESERVE,
                preserve_runtime=action.runtime_mode is BuildMode.PRESERVE,
            )
            if action.compiler_kit_id == context.variant.compiler_kit_id:
                compiler_kit = built_compiler
            paired_runtime_ids.add(runtime_id)
            continue

        artifact_id = action.artifact_id
        artifact_workspace = (
            context.paths.compiler_kit_workspace
            if artifact_id == context.variant.compiler_kit_id
            else context.paths.runtime(artifact_id).workspace
        )
        built = _build_artifact(
            context,
            options,
            artifact_id,
            artifact_workspace,
            producer=producer,
            rebuild=action.mode is BuildMode.REBUILD,
        )
        if artifact_id == context.variant.compiler_kit_id:
            compiler_kit = built
        else:
            runtimes[artifact_id] = built

    return _BuiltArtifacts(
        compiler_kit=compiler_kit,
        runtimes=runtimes,
        paired_runtime_ids=frozenset(paired_runtime_ids),
    )


def _publish_runtime_components(
    context: _AssemblyContext,
    state: _AssemblyState,
    built: _BuiltArtifacts,
    plan: AssemblyPlan,
    progress: ProgressCallback | None,
) -> dict[str, ManagedRuntimePublication]:
    publications = dict(state.loaded_publications)
    for runtime_state in state.runtime_states:
        if runtime_state.publication is PublicationDisposition.REUSE:
            _emit(
                progress,
                "runtime: using validated publication "
                f"{runtime_state.runtime.runtime_id}",
            )
    for action in plan.publications:
        artifact_id = action.artifact_id
        _emit(progress, f"runtime: publishing validated overlay {artifact_id}")
        runtime_paths = context.paths.runtime(artifact_id)
        publications[artifact_id] = publish_managed_runtime_publication(
            context.lock,
            artifact_id,
            built.runtimes[artifact_id],
            runtime_paths.publication,
            force=action.force,
        )
    return publications


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
    context = _resolve_assembly_context(
        lock,
        variant_id,
        sdk_workspace,
        compiler_backend_workspace,
        workspace,
        output,
        source_cache,
    )
    options = _AssemblyBuildOptions(
        jobs=jobs,
        dockerfile=dockerfile,
        image=image,
        progress=progress,
        source_progress=source_progress,
    )
    state = _discover_assembly_state(context, rebuild=rebuild, repair=repair)
    plan = plan_assembly(
        context.variant.compiler_kit_id,
        tuple(item.state for item in state.compiler_contexts.values()),
        state.runtime_states,
    )
    built = _execute_build_plan(context, state, plan, options)
    loaded_publications = _publish_runtime_components(
        context,
        state,
        built,
        plan,
        progress,
    )

    compiler_artifact = state.compiler_artifact
    if compiler_artifact is None:
        compiler_artifact = load_managed_compiler_artifact(
            lock,
            context.variant.compiler_kit_id,
            built.compiler_kit,
        )
    if built.paired_runtime_ids:
        _emit(
            progress,
            "compiler kit and matching runtime reused one compiler build tree",
        )
    runtime_set = publish_managed_runtime_set(
        lock,
        context.variant.id,
        loaded_publications,
        context.paths.runtime_set,
        force=rebuild or repair,
    )

    _emit(progress, "binding: validating and rendering integrations")
    # Import at call time so catalog and lockfile workflows do not initialize
    # the compiler-binding subsystem.
    from linux_toolchain.compiler.managed_binding import (
        create_managed_binding_from_artifacts,
    )

    binding_manifest = create_managed_binding_from_artifacts(
        context.sdk,
        context.output,
        lock=lock,
        variant=context.variant.id,
        compiler_artifact=compiler_artifact,
        runtime_set_publication=runtime_set,
        integrations=integrations,
        conan=conan,
        force=force,
    )
    return AssemblyResult(
        variant_id=context.variant.id,
        compiler_kit=built.compiler_kit,
        runtime=context.paths.runtime_set,
        binding_manifest=binding_manifest,
    )
