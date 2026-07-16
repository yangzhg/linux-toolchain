from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from linux_toolchain.container import (
    BUILDER_TARGET,
    builder_image_build_args,
    builder_image_contract_digest,
    packaged_builder_dockerfile_sha256,
    ubuntu_builder_snapshot,
)
from linux_toolchain.managed.contracts import (
    MANAGED_BUILDER_BASE_IMAGE,
    MANAGED_COMPILER_BACKEND_SUPPLEMENTAL_SOURCES,
    MANAGED_COMPILER_BACKEND_VERSION,
    MANAGED_TARGET_TOOL_NAMES,
)
from linux_toolchain.managed.scripts import render_build_script
from linux_toolchain.managed.selection import ManagedBuildSelection
from linux_toolchain.models import SdkSpec
from linux_toolchain.schema import canonical_json_sha256
from linux_toolchain.sdk.crosstool_ng import (
    CROSSTOOL_NG_RELEASES,
    packaged_crosstool_ng_patch,
    sdk_producer_identity,
)

_RUNTIME_ADAPTER_REVISION = 1
_RUNTIME_SET_ADAPTER_REVISION = 1


def managed_builder_contract(platform: str) -> dict[str, str]:
    """Return the stable inputs that define the managed builder image."""

    release = CROSSTOOL_NG_RELEASES[MANAGED_COMPILER_BACKEND_VERSION]
    apt_snapshot = ubuntu_builder_snapshot()
    dockerfile_sha256 = packaged_builder_dockerfile_sha256()
    archive = f"crosstool-ng-{release.version}.tar.xz"
    backend_patch = packaged_crosstool_ng_patch(release.version)
    build_args = builder_image_build_args(
        base_image=MANAGED_BUILDER_BASE_IMAGE,
        apt_snapshot=apt_snapshot,
        crosstool_ng_version=release.version,
        crosstool_ng_sha256=release.sha256,
        crosstool_ng_archive=archive,
        crosstool_ng_patch=Path(backend_patch.relative_path).name,
        crosstool_ng_patch_sha256=backend_patch.sha256,
    )
    return {
        "platform": platform,
        "base_image": MANAGED_BUILDER_BASE_IMAGE,
        "apt_snapshot": apt_snapshot,
        "dockerfile_sha256": dockerfile_sha256,
        "target": BUILDER_TARGET,
        "crosstool_ng_version": release.version,
        "crosstool_ng_sha256": release.sha256,
        "sha256": builder_image_contract_digest(
            dockerfile_sha256=dockerfile_sha256,
            base_image=MANAGED_BUILDER_BASE_IMAGE,
            pinned_input=release.sha256,
            platform=platform,
            build_args=build_args,
            target=BUILDER_TARGET,
        ),
    }


def render_action_script(
    selection: ManagedBuildSelection,
    *,
    triplet: str,
    backend_triplet: str,
    backend_version: str,
) -> str:
    """Render the script whose semantics determine one artifact's output."""

    return render_build_script(
        selection,
        triplet=triplet,
        backend_triplet=backend_triplet,
        backend_version=backend_version,
        paired_runtime=False,
    )


def script_identity(script: str) -> dict[str, str]:
    return {"sha256": hashlib.sha256(script.encode("utf-8")).hexdigest()}


def target_tools_action(sdk: Mapping[str, object], triplet: str) -> dict[str, object]:
    """Tie target-tool provenance to the SDK workspace that produced it."""

    return {
        "sdk": dict(sdk),
        "triplet": triplet,
        "tools": list(MANAGED_TARGET_TOOL_NAMES),
    }


def managed_artifact_action(
    selection: ManagedBuildSelection,
    *,
    sdk: Mapping[str, object],
    target_tools: Mapping[str, object],
    compiler_backend: Mapping[str, object],
    script: Mapping[str, object],
) -> dict[str, object]:
    """Return the relocatable inputs that can change a managed artifact."""

    return {
        "artifact": artifact_action_selection(selection),
        "source": source_content_pin(selection),
        "sdk": dict(sdk),
        "target_tools": dict(target_tools),
        "compiler_backend": dict(compiler_backend),
        "builder": managed_builder_contract(selection.build_platform),
        "script": dict(script),
    }


def managed_artifact_action_for_specs(
    selection: ManagedBuildSelection,
    target_sdk: SdkSpec,
    compiler_backend: SdkSpec,
) -> dict[str, object]:
    """Build an artifact identity from pinned producer specifications."""

    sdk_identity = sdk_producer_identity(target_sdk)
    return managed_artifact_action(
        selection,
        sdk=sdk_identity,
        target_tools=target_tools_action(sdk_identity, target_sdk.target.triplet),
        compiler_backend={
            "sdk": sdk_producer_identity(compiler_backend),
            "supplemental_sources": [
                {"filename": filename, "sha256": sha256}
                for filename, sha256 in sorted(
                    MANAGED_COMPILER_BACKEND_SUPPLEMENTAL_SOURCES.items()
                )
            ],
        },
        script=script_identity(
            render_action_script(
                selection,
                triplet=target_sdk.target.triplet,
                backend_triplet=compiler_backend.target.triplet,
                backend_version=compiler_backend.builder.gcc,
            )
        ),
    )


def artifact_action_selection(
    selection: ManagedBuildSelection,
) -> dict[str, object]:
    target: dict[str, str] = {"arch": selection.target_arch}
    if selection.target_glibc_floor is not None:
        target["glibc_floor"] = selection.target_glibc_floor
    result: dict[str, object] = {
        "kind": selection.artifact_kind,
        "family": selection.family,
        "version": selection.version,
        "target": target,
    }
    if selection.host is not None:
        result["host"] = selection.host.to_dict()
    if selection.runtime_kind is not None:
        result["runtime_kind"] = selection.runtime_kind
    return result


def source_content_pin(selection: ManagedBuildSelection) -> dict[str, object]:
    source = selection.source
    return {"kind": "archive", "sha512": source.sha512}


def managed_action_sha256(action: Mapping[str, object]) -> str:
    return canonical_json_sha256(dict(action))


def runtime_publication_action(
    raw_action_sha256: str,
    *,
    adapter: str,
) -> dict[str, object]:
    """Return the inputs that determine a published runtime overlay."""

    return {
        "raw_action_sha256": raw_action_sha256,
        "adapter": adapter,
        "adapter_revision": _RUNTIME_ADAPTER_REVISION,
    }


def runtime_set_action(
    lock_sha256: str,
    variant: Mapping[str, object],
    runtime_publications: Mapping[str, str],
) -> dict[str, object]:
    """Return the inputs that determine one composed runtime publication."""

    return {
        "lock_sha256": lock_sha256,
        "variant": dict(variant),
        "runtime_publications": dict(sorted(runtime_publications.items())),
        "adapter_revision": _RUNTIME_SET_ADAPTER_REVISION,
    }
