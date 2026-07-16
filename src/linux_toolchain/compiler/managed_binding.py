from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from linux_toolchain.compiler.binding import create_binding_from_inputs
from linux_toolchain.compiler.managed import validate_current_host
from linux_toolchain.compiler.runtime_binding import (
    RuntimeBindingSet,
    load_runtime_binding,
)
from linux_toolchain.compiler.toolchain import (
    CompilerInfo,
    ManagedCompilerToolchain,
    managed_compiler_info,
    managed_compiler_toolchain,
)
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.integrations import (
    DEFAULT_INTEGRATIONS,
    ConanSettings,
    IntegrationName,
)
from linux_toolchain.managed.identity import managed_action_sha256
from linux_toolchain.managed.lockfile import ManagedLock
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimeSetPublication,
    load_managed_compiler_artifact,
    load_managed_runtime_set_publication,
)


@dataclass(frozen=True)
class _ManagedBindingInputs:
    compiler: CompilerInfo
    toolchain: ManagedCompilerToolchain
    runtimes: RuntimeBindingSet
    evidence: Mapping[str, object]


def _load_managed_binding_inputs(
    compiler_kit: Path,
    *,
    lock: ManagedLock | Path,
    variant: str,
    runtime: Path,
) -> _ManagedBindingInputs:
    """Load the immutable inputs that define one managed binding."""

    managed_lock = lock if isinstance(lock, ManagedLock) else ManagedLock.load(lock)
    selected_variant = managed_lock.variant(variant)
    compiler_artifact = load_managed_compiler_artifact(
        managed_lock,
        selected_variant.compiler_kit_id,
        compiler_kit,
    )
    runtime_set_publication = load_managed_runtime_set_publication(
        managed_lock,
        selected_variant.id,
        runtime,
    )
    return _managed_binding_inputs(
        managed_lock,
        variant,
        compiler_artifact,
        runtime_set_publication,
    )


def _managed_binding_inputs(
    lock: ManagedLock,
    variant: str,
    compiler_artifact: ManagedCompilerArtifact,
    runtime_set_publication: ManagedRuntimeSetPublication,
) -> _ManagedBindingInputs:
    selected_variant = lock.variant(variant)
    if (
        compiler_artifact.selection.artifact_id != selected_variant.compiler_kit_id
        or runtime_set_publication.variant != selected_variant
    ):
        raise ConfigurationError(
            "validated managed artifacts do not match the selected variant"
        )
    kit = compiler_artifact.compiler_kit
    validate_current_host(kit.manifest.host)
    compiler = managed_compiler_info(kit)
    toolchain = managed_compiler_toolchain(kit)
    runtime_bindings = RuntimeBindingSet(
        default_kind=selected_variant.default_cxx_runtime,
        bindings=tuple(
            (
                entry.kind,
                load_runtime_binding(
                    entry.publication.root,
                    entry.publication.manifest,
                ),
            )
            for entry in runtime_set_publication.entries
        ),
    )
    if (
        compiler.family != selected_variant.family
        or compiler.version != selected_variant.version
        or kit.manifest.target["arch"] != selected_variant.target.arch
    ):
        raise ConfigurationError(
            "managed Compiler Kit does not match the selected variant"
        )
    compiler_action = compiler_artifact.manifest["action"]
    if not isinstance(compiler_action, Mapping):
        raise ConfigurationError("managed compiler artifact action is invalid")
    runtime_artifacts: list[dict[str, object]] = []
    for entry in runtime_set_publication.entries:
        receipt = entry.publication.receipt
        publication_action = receipt["publication_action"]
        if not isinstance(publication_action, Mapping):
            raise ConfigurationError("managed runtime publication action is invalid")
        runtime_artifacts.append(
            {
                "kind": entry.kind,
                "artifact_id": entry.publication.selection.artifact_id,
                "raw_action_sha256": publication_action["raw_action_sha256"],
                "publication_action_sha256": receipt["publication_action_sha256"],
            }
        )
    evidence: dict[str, object] = {
        "lock_sha256": lock.sha256,
        "variant": selected_variant.to_dict(),
        "compiler_artifact": {
            "action_sha256": managed_action_sha256(compiler_action),
        },
        "runtime_artifacts": runtime_artifacts,
    }
    return _ManagedBindingInputs(
        compiler=compiler,
        toolchain=toolchain,
        runtimes=runtime_bindings,
        evidence=evidence,
    )


def _render_managed_binding(
    sdk: Path,
    output: Path,
    inputs: _ManagedBindingInputs,
    *,
    integrations: Sequence[IntegrationName],
    conan: ConanSettings | None,
    force: bool,
) -> Path:
    return create_binding_from_inputs(
        sdk,
        output,
        inputs.compiler,
        runtime=inputs.runtimes,
        toolchain=inputs.toolchain,
        managed_evidence=inputs.evidence,
        integrations=integrations,
        conan=conan,
        force=force,
    )


def create_managed_binding(
    sdk: Path,
    output: Path,
    compiler_kit: Path,
    *,
    lock: ManagedLock | Path,
    variant: str,
    runtime: Path,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    force: bool = False,
) -> Path:
    """Create a binding whose compiler and target tools come from one kit.

    The Compiler Kit manifest supplies the target and every binary-tool path.
    Only the two driver version strings are executed as an identity check; the
    managed path never asks a driver or the host PATH to discover target tools.
    """

    inputs = _load_managed_binding_inputs(
        compiler_kit,
        lock=lock,
        variant=variant,
        runtime=runtime,
    )
    return _render_managed_binding(
        sdk,
        output,
        inputs,
        integrations=integrations,
        conan=conan,
        force=force,
    )


def create_managed_binding_from_artifacts(
    sdk: Path,
    output: Path,
    *,
    lock: ManagedLock,
    variant: str,
    compiler_artifact: ManagedCompilerArtifact,
    runtime_set_publication: ManagedRuntimeSetPublication,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    force: bool = False,
) -> Path:
    """Create a binding from artifacts already validated under producer leases."""

    inputs = _managed_binding_inputs(
        lock,
        variant,
        compiler_artifact,
        runtime_set_publication,
    )
    return _render_managed_binding(
        sdk,
        output,
        inputs,
        integrations=integrations,
        conan=conan,
        force=force,
    )
