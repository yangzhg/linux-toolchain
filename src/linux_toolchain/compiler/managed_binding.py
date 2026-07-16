from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from linux_toolchain.compiler.binding import _create_binding
from linux_toolchain.compiler.managed import validate_current_host
from linux_toolchain.compiler.runtime_binding import (
    RuntimeBinding,
    _load_runtime_binding,
)
from linux_toolchain.compiler.toolchain import (
    CompilerInfo,
    _managed_compiler_info,
    _managed_toolchain,
    _ManagedCompilerToolchain,
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
    ManagedRuntimePublication,
    load_managed_compiler_artifact,
    load_managed_runtime_publication,
)


@dataclass(frozen=True)
class _ManagedBindingInputs:
    compiler: CompilerInfo
    toolchain: _ManagedCompilerToolchain
    runtime: RuntimeBinding
    evidence: Mapping[str, object]


def _load_managed_binding_inputs(
    compiler_kit: Path,
    *,
    lock: ManagedLock | Path,
    variant: str,
    runtime: Path,
    compiler_artifact: ManagedCompilerArtifact | None = None,
    runtime_publication: ManagedRuntimePublication | None = None,
) -> _ManagedBindingInputs:
    """Load the immutable inputs that define one managed binding."""

    managed_lock = lock if isinstance(lock, ManagedLock) else ManagedLock.load(lock)
    variants = tuple(entry for entry in managed_lock.variants if entry.id == variant)
    if len(variants) != 1:
        raise ConfigurationError(
            f"managed variant {variant!r} does not exist in the selected lock"
        )
    selected_variant = variants[0]
    if compiler_artifact is None:
        compiler_artifact = load_managed_compiler_artifact(
            managed_lock,
            selected_variant.compiler_kit_id,
            compiler_kit,
        )
    elif compiler_artifact.root != compiler_kit.expanduser().resolve():
        raise ConfigurationError("validated Compiler Kit path changed")
    if runtime_publication is None:
        runtime_publication = load_managed_runtime_publication(
            managed_lock,
            selected_variant.runtime_id,
            runtime,
        )
    elif runtime_publication.root != runtime.expanduser().resolve():
        raise ConfigurationError("validated runtime publication path changed")
    if (
        compiler_artifact.selection.artifact_id != selected_variant.compiler_kit_id
        or runtime_publication.selection.artifact_id != selected_variant.runtime_id
    ):
        raise ConfigurationError(
            "validated managed artifacts do not match the selected variant"
        )
    kit = compiler_artifact.compiler_kit
    validate_current_host(kit.manifest.host)
    compiler = _managed_compiler_info(kit)
    toolchain = _managed_toolchain(kit)
    runtime_binding = _load_runtime_binding(
        runtime_publication.root,
        runtime_publication.manifest,
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
    receipt = runtime_publication.receipt
    publication_action = receipt["publication_action"]
    if not isinstance(publication_action, Mapping):
        raise ConfigurationError("managed runtime publication action is invalid")
    evidence: dict[str, object] = {
        "lock_sha256": managed_lock.sha256,
        "variant": selected_variant.to_dict(),
        "compiler_artifact": {
            "action_sha256": managed_action_sha256(compiler_action),
        },
        "runtime_artifact": {
            "raw_action_sha256": publication_action["raw_action_sha256"],
            "publication_action_sha256": receipt["publication_action_sha256"],
        },
    }
    return _ManagedBindingInputs(
        compiler=compiler,
        toolchain=toolchain,
        runtime=runtime_binding,
        evidence=evidence,
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
    _compiler_artifact: ManagedCompilerArtifact | None = None,
    _runtime_publication: ManagedRuntimePublication | None = None,
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
        compiler_artifact=_compiler_artifact,
        runtime_publication=_runtime_publication,
    )
    return _create_binding(
        sdk,
        output,
        inputs.compiler,
        runtime=inputs.runtime,
        toolchain=inputs.toolchain,
        managed_evidence=inputs.evidence,
        integrations=integrations,
        conan=conan,
        force=force,
    )
