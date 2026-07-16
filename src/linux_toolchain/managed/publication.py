from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from linux_toolchain.container import linux_architecture_for_platform
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.licenses import (
    license_evidence,
    managed_required_license_paths,
    require_license_files,
    validate_license_evidence,
    validate_ubuntu_package_licenses,
)
from linux_toolchain.managed.catalog import resolve_release
from linux_toolchain.managed.contracts import (
    MANAGED_ARTIFACT_FORMAT,
    MANAGED_ARTIFACT_SCHEMA,
    managed_compiler_backend_spec,
)
from linux_toolchain.managed.identity import (
    managed_action_sha256,
    managed_artifact_action_for_specs,
    runtime_publication_action,
)
from linux_toolchain.managed.lockfile import ManagedLock
from linux_toolchain.managed.selection import ManagedBuildSelection, select_artifact
from linux_toolchain.models import SDK_SPEC_FORMAT, SDK_SPEC_SCHEMA, SdkSpec
from linux_toolchain.publication import replace_directory, write_json_atomic
from linux_toolchain.runtime.llvm_models import LlvmRuntimeSourceEvidence
from linux_toolchain.schema import read_json_object as _read_json_object
from linux_toolchain.sdk.crosstool_ng import (
    sdk_producer_identity,
)
from linux_toolchain.versions import AbiVersion

if TYPE_CHECKING:
    from linux_toolchain.compiler.managed import CompilerKit
    from linux_toolchain.runtime import GccRuntimeManifest, LlvmRuntimeManifest

MANAGED_PUBLICATION_SCHEMA = "linux-toolchain-managed-publication"
MANAGED_PUBLICATION_FORMAT = 1
MANAGED_PUBLICATION_FILE = "managed-publication.json"


@dataclass(frozen=True)
class ManagedRuntimeArtifact:
    """Validated view of a raw runtime produced by the managed builder."""

    root: Path
    manifest_path: Path
    payload: Path
    selection: ManagedBuildSelection
    target: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ManagedCompilerArtifact:
    """Validated managed compiler artifact and its lock selection."""

    root: Path
    manifest_path: Path
    payload: Path
    selection: ManagedBuildSelection
    target: str
    manifest: dict[str, Any]
    compiler_kit: CompilerKit


@dataclass(frozen=True)
class ManagedRuntimePublication:
    """Published runtime tied to a managed lock entry."""

    root: Path
    manifest_path: Path
    selection: ManagedBuildSelection
    receipt: dict[str, Any]
    manifest: GccRuntimeManifest | LlvmRuntimeManifest


def _artifact_manifest_path(value: Path | str) -> tuple[Path, Path]:
    raw = Path(value).expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"managed artifact cannot be a symlink: {raw}")
    candidate = raw / "artifact.json" if raw.is_dir() else raw
    if candidate.name != "artifact.json" or candidate.is_symlink():
        raise ConfigurationError(
            "managed artifact must be artifact.json or its artifacts directory"
        )
    try:
        manifest = candidate.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot access managed artifact manifest {candidate}: {error}"
        ) from error
    if not manifest.is_file():
        raise ConfigurationError(f"managed artifact manifest is not a file: {manifest}")
    return manifest.parent, manifest


def _load_managed_llvm_source_evidence(
    provenance: Path | str,
    prefix: Path | str,
    *,
    version: str,
    glibc_floor: str,
    arch: str,
    target: str,
) -> LlvmRuntimeSourceEvidence:
    """Validate managed LLVM provenance before entering the generic adapter."""

    root, manifest_path = _artifact_manifest_path(provenance)
    expected_payload = root / "runtime"
    if expected_payload.is_symlink():
        raise ConfigurationError("managed LLVM runtime payload cannot be a symlink")
    try:
        expected_source = expected_payload.resolve(strict=True)
        selected_source = Path(prefix).expanduser().resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot access managed LLVM runtime payload: {error}"
        ) from error
    if expected_source != selected_source:
        raise ConfigurationError(
            "managed provenance does not own the selected LLVM runtime prefix"
        )

    value = _read_json_object(manifest_path, context="managed artifact manifest")
    manifest_format = value.get("format")
    if (
        value.get("schema") != MANAGED_ARTIFACT_SCHEMA
        or not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != MANAGED_ARTIFACT_FORMAT
    ):
        raise ConfigurationError("managed LLVM artifact schema or format is invalid")
    required = {
        "schema",
        "format",
        "action",
        "action_sha256",
        "provenance",
        "licenses",
        "elf_audit",
    }
    if set(value) != required:
        raise ConfigurationError("managed LLVM artifact fields are invalid")
    action = value.get("action")
    if not isinstance(action, dict) or value.get(
        "action_sha256"
    ) != managed_action_sha256(action):
        raise ConfigurationError("managed LLVM artifact action identity is invalid")
    if set(action) != {
        "artifact",
        "source",
        "sdk",
        "target_tools",
        "compiler_backend",
        "builder",
        "script",
    }:
        raise ConfigurationError("managed LLVM artifact action fields are invalid")
    artifact = action.get("artifact")
    if artifact != {
        "kind": "runtime",
        "family": "clang",
        "version": version,
        "target": {"arch": arch, "glibc_floor": glibc_floor},
        "runtime_kind": "llvm-runtime",
    }:
        raise ConfigurationError("managed LLVM artifact selection is invalid")
    tools = action.get("target_tools")
    if not isinstance(tools, dict) or tools.get("triplet") != target:
        raise ConfigurationError(
            "managed LLVM artifact target does not match the requested target"
        )

    release = resolve_release("clang", version)
    if action.get("source") != {
        "kind": "archive",
        "sha512": release.archive_sha512,
    }:
        raise ConfigurationError(
            "managed LLVM artifact source does not match the pinned catalog"
        )
    raw_provenance = value.get("provenance")
    if not isinstance(raw_provenance, dict):
        raise ConfigurationError("managed LLVM artifact provenance is invalid")
    source_provenance = raw_provenance.get("source")
    if not isinstance(source_provenance, dict) or set(source_provenance) != {"url"}:
        raise ConfigurationError("managed LLVM source provenance is invalid")
    source_url = source_provenance.get("url")
    if not isinstance(source_url, str) or not source_url:
        raise ConfigurationError("managed LLVM source provenance is invalid")
    return LlvmRuntimeSourceEvidence.from_dict(
        {
            "kind": "managed-artifact",
            "version": version,
            "target": target,
            "url": source_url,
            "sha512": release.archive_sha512,
        }
    )


def _object(value: object, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must be an object")
    return value


def _sdk_from_action(value: object) -> SdkSpec:
    sdk = _object(value, context="managed artifact action.sdk")
    required = {
        "kind",
        "config_sha256",
        "export_revision",
        "target",
        "builder",
        "backend_source",
        "component_sources",
        "builder_contract",
    }
    if set(sdk) != required or sdk.get("kind") != "sdk":
        raise ConfigurationError("managed artifact SDK action fields are invalid")
    spec = SdkSpec.from_dict(
        {
            "schema": SDK_SPEC_SCHEMA,
            "format": SDK_SPEC_FORMAT,
            "name": "managed-action-sdk",
            "target": sdk.get("target"),
            "builder": sdk.get("builder"),
        }
    )
    if sdk != sdk_producer_identity(spec):
        raise ConfigurationError("managed artifact SDK action inputs changed")
    return spec


def _validate_build_action(
    value: object,
    selection: ManagedBuildSelection,
) -> tuple[dict[str, Any], str]:
    action = _object(value, context="managed artifact action")
    required = {
        "artifact",
        "source",
        "sdk",
        "target_tools",
        "compiler_backend",
        "builder",
        "script",
    }
    if set(action) != required:
        raise ConfigurationError("managed artifact action fields are invalid")
    sdk = _sdk_from_action(action.get("sdk"))
    if sdk.target.arch != selection.target_arch:
        raise ConfigurationError("managed artifact SDK action target changed")
    if (
        selection.target_glibc_floor is not None
        and sdk.target.libc_version != selection.target_glibc_floor
    ):
        raise ConfigurationError("managed artifact SDK action floor changed")
    target = sdk.target.triplet
    backend = managed_compiler_backend_spec(
        selection.build_host.arch,
        selection.build_host.glibc_floor,
    )
    if action != managed_artifact_action_for_specs(selection, sdk, backend):
        raise ConfigurationError("managed artifact action changed")
    return action, target


def _validate_provenance(
    value: object,
    selection: ManagedBuildSelection,
) -> None:
    provenance = _object(value, context="managed artifact provenance")
    if set(provenance) != {"source", "builder_image", "execution_script"}:
        raise ConfigurationError("managed artifact provenance fields are invalid")
    source = _object(provenance.get("source"), context="managed source provenance")
    source_url = source.get("url")
    if set(source) != {"url"} or not isinstance(source_url, str) or not source_url:
        raise ConfigurationError("managed source provenance is invalid")
    image = _object(
        provenance.get("builder_image"), context="managed builder image provenance"
    )
    if set(image) != {"id", "os", "architecture", "repo_digests"}:
        raise ConfigurationError("managed builder image provenance fields are invalid")
    if (
        not isinstance(image.get("id"), str)
        or not isinstance(image.get("os"), str)
        or not isinstance(image.get("architecture"), str)
        or not isinstance(image.get("repo_digests"), list)
        or not all(isinstance(item, str) for item in image["repo_digests"])
    ):
        raise ConfigurationError("managed builder image provenance is invalid")
    if image["architecture"] != selection.build_platform.removeprefix("linux/"):
        raise ConfigurationError(
            "managed builder image architecture does not match its build platform"
        )
    if (
        linux_architecture_for_platform(selection.build_platform)
        != selection.build_host.arch
    ):
        raise ConfigurationError(
            "managed build platform does not match its Compiler Kit host"
        )
    execution = _object(
        provenance.get("execution_script"),
        context="managed artifact execution script provenance",
    )
    if set(execution) != {"path", "sha256", "paired_runtime"}:
        raise ConfigurationError(
            "managed execution script provenance fields are invalid"
        )
    execution_sha = execution.get("sha256")
    if (
        execution.get("path") != "build/build.sh"
        or not isinstance(execution.get("paired_runtime"), bool)
        or not isinstance(execution_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", execution_sha) is None
    ):
        raise ConfigurationError("managed artifact execution script is invalid")


def _validate_elf_audit(
    value: object,
    selection: ManagedBuildSelection,
) -> None:
    audit = _object(value, context="managed artifact elf_audit")
    required = {"audited_elf_files", "max_required_glibc"}
    if selection.artifact_kind == "runtime":
        required.add("audited_shared_libraries")
    if set(audit) != required:
        raise ConfigurationError("managed artifact ELF audit fields are invalid")
    for key in required - {"max_required_glibc"}:
        count = audit.get(key)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ConfigurationError("managed artifact ELF audit count is invalid")
    maximum = audit.get("max_required_glibc")
    if maximum is not None:
        if not isinstance(maximum, str):
            raise ConfigurationError("managed artifact ELF audit version is invalid")
        parsed = AbiVersion.parse(maximum)
        floor = (
            selection.host.glibc_floor
            if selection.host is not None
            else selection.target_glibc_floor
        )
        assert floor is not None
        if parsed > AbiVersion.parse(floor):
            raise ConfigurationError("managed artifact ELF audit exceeds its floor")


def _validate_common_artifact(
    lock: ManagedLock,
    artifact_id: str,
    artifact: Path | str,
    *,
    expected_kind: str,
) -> tuple[
    Path,
    Path,
    Path,
    ManagedBuildSelection,
    str,
    dict[str, Any],
]:
    lock.validate()
    selection = select_artifact(lock, artifact_id)
    if selection.artifact_kind != expected_kind:
        label = "compiler kit" if expected_kind == "compiler-kit" else "runtime"
        raise ConfigurationError(f"managed artifact {artifact_id!r} is not a {label}")
    root, manifest_path = _artifact_manifest_path(artifact)
    value = _read_json_object(manifest_path, context="managed artifact manifest")
    manifest_format = value.get("format")
    if (
        value.get("schema") != MANAGED_ARTIFACT_SCHEMA
        or not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != MANAGED_ARTIFACT_FORMAT
    ):
        raise ConfigurationError("managed artifact schema or format is invalid")
    required = {
        "schema",
        "format",
        "action",
        "action_sha256",
        "provenance",
        "licenses",
        "elf_audit",
    }
    if set(value) != required:
        raise ConfigurationError("managed artifact manifest fields are invalid")
    action, target = _validate_build_action(value.get("action"), selection)
    if value.get("action_sha256") != managed_action_sha256(action):
        raise ConfigurationError("managed artifact action identity is invalid")
    _validate_provenance(value.get("provenance"), selection)
    _validate_elf_audit(value.get("elf_audit"), selection)
    payload = root / selection.payload_name
    if payload.is_symlink() or not payload.is_dir():
        raise ConfigurationError(f"managed artifact payload is missing: {payload}")
    validate_license_evidence(
        root,
        value.get("licenses"),
        context="managed artifact",
    )
    require_license_files(
        root,
        managed_required_license_paths(
            selection.family,
            compiler_kit=selection.artifact_kind == "compiler-kit",
        ),
        context="managed artifact",
    )
    if selection.artifact_kind == "compiler-kit":
        validate_ubuntu_package_licenses(
            root,
            payload,
            context="managed compiler kit",
        )
    return root, manifest_path, payload, selection, target, value


def load_managed_runtime_artifact(
    lock: ManagedLock,
    artifact_id: str,
    artifact: Path | str,
) -> ManagedRuntimeArtifact:
    """Load a raw runtime only when it still matches its immutable lock entry."""

    root, manifest_path, payload, selection, target, value = _validate_common_artifact(
        lock,
        artifact_id,
        artifact,
        expected_kind="runtime",
    )
    if selection.runtime_kind is None:
        raise ConfigurationError(f"managed artifact {artifact_id!r} is not a runtime")
    return ManagedRuntimeArtifact(
        root=root,
        manifest_path=manifest_path,
        payload=payload,
        selection=selection,
        target=target,
        manifest=value,
    )


def load_managed_compiler_artifact(
    lock: ManagedLock,
    artifact_id: str,
    artifact: Path | str,
) -> ManagedCompilerArtifact:
    """Load a Compiler Kit only when its raw artifact matches the lock."""

    from linux_toolchain.compiler.managed import load_compiler_kit

    root, manifest_path, payload, selection, target, value = _validate_common_artifact(
        lock,
        artifact_id,
        artifact,
        expected_kind="compiler-kit",
    )
    compiler_kit = load_compiler_kit(root, check_host=False)
    if (
        compiler_kit.manifest.provider.get("name") != selection.family
        or compiler_kit.manifest.provider.get("version") != selection.version
        or dict(compiler_kit.manifest.host) != selection.host.to_dict()
        or compiler_kit.manifest.target.get("arch") != selection.target_arch
        or compiler_kit.manifest.target.get("triplet") != target
    ):
        raise ConfigurationError("Compiler Kit manifest does not match its action")
    return ManagedCompilerArtifact(
        root=root,
        manifest_path=manifest_path,
        payload=payload,
        selection=selection,
        target=target,
        manifest=value,
        compiler_kit=compiler_kit,
    )


def _publication_root(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise ConfigurationError(
            f"managed runtime publication cannot be a symlink: {candidate}"
        )
    root = candidate if candidate.is_dir() else candidate.parent
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot access managed runtime publication {candidate}: {error}"
        ) from error
    if not root.is_dir() or root.is_symlink():
        raise ConfigurationError(
            f"managed runtime publication is not a directory: {root}"
        )
    return root


def _write_publication_receipt(
    loaded: ManagedRuntimeArtifact,
    published_manifest: Path,
) -> Path:
    root = published_manifest.parent.resolve(strict=True)
    runtime = root / "runtime"
    if runtime.is_symlink() or not runtime.is_dir():
        raise ConfigurationError(
            "published managed runtime has no regular runtime directory"
        )
    licenses = license_evidence(root, context="published managed runtime")
    adapter = (
        "import_gcc_runtime"
        if loaded.selection.runtime_kind == "gcc-runtime"
        else "import_llvm_runtime"
    )
    raw_identity = loaded.manifest["action_sha256"]
    if not isinstance(raw_identity, str):
        raise ConfigurationError("managed runtime raw build identity is missing")
    raw_action = loaded.manifest["action"]
    if not isinstance(raw_action, dict):
        raise ConfigurationError("managed runtime raw build action is missing")
    publication_input = runtime_publication_action(
        raw_identity,
        adapter=adapter,
    )
    receipt = {
        "schema": MANAGED_PUBLICATION_SCHEMA,
        "format": MANAGED_PUBLICATION_FORMAT,
        "raw_action": raw_action,
        "publication_action": publication_input,
        "publication_action_sha256": managed_action_sha256(publication_input),
        "licenses": licenses,
    }
    path = root / MANAGED_PUBLICATION_FILE
    write_json_atomic(path, receipt)
    return path


def load_managed_runtime_publication(
    lock: ManagedLock,
    artifact_id: str,
    publication: Path | str,
) -> ManagedRuntimePublication:
    """Load a runtime publication for the selected managed lock entry."""

    lock.validate()
    selection = select_artifact(lock, artifact_id)
    if selection.artifact_kind != "runtime" or selection.runtime_kind is None:
        raise ConfigurationError(f"managed artifact {artifact_id!r} is not a runtime")
    root = _publication_root(publication)
    receipt_path = root / MANAGED_PUBLICATION_FILE
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ConfigurationError(
            f"managed runtime publication receipt is missing: {receipt_path}"
        )
    receipt = _read_json_object(
        receipt_path,
        context="managed runtime publication receipt",
    )
    required = {
        "schema",
        "format",
        "raw_action",
        "publication_action",
        "publication_action_sha256",
        "licenses",
    }
    missing = sorted(required - receipt.keys())
    unknown = sorted(receipt.keys() - required)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unknown:
            details.append("unknown: " + ", ".join(unknown))
        raise ConfigurationError(
            "managed runtime publication receipt has invalid fields"
            + (": " + "; ".join(details) if details else "")
        )
    if (
        receipt.get("schema") != MANAGED_PUBLICATION_SCHEMA
        or type(receipt.get("format")) is not int
        or receipt.get("format") != MANAGED_PUBLICATION_FORMAT
    ):
        raise ConfigurationError("managed runtime publication schema is invalid")
    validate_license_evidence(
        root,
        receipt.get("licenses"),
        context="published managed runtime",
    )
    raw_action, target = _validate_build_action(receipt.get("raw_action"), selection)
    recorded_publication_action = receipt.get("publication_action")
    raw_identity = (
        recorded_publication_action.get("raw_action_sha256")
        if isinstance(recorded_publication_action, dict)
        else None
    )
    if (
        not isinstance(raw_identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", raw_identity) is None
    ):
        raise ConfigurationError("managed runtime publication raw action is invalid")
    if raw_identity != managed_action_sha256(raw_action):
        raise ConfigurationError(
            "managed runtime publication raw action does not match its lock"
        )
    adapter = (
        "import_gcc_runtime"
        if selection.runtime_kind == "gcc-runtime"
        else "import_llvm_runtime"
    )
    publication_input = runtime_publication_action(
        raw_identity,
        adapter=adapter,
    )
    if recorded_publication_action != publication_input or receipt.get(
        "publication_action_sha256"
    ) != managed_action_sha256(publication_input):
        raise ConfigurationError("managed runtime publication identity changed")
    manifest_path = root / "manifest.json"
    if selection.runtime_kind == "gcc-runtime":
        from linux_toolchain.runtime.models import load_runtime_manifest

        runtime_manifest = load_runtime_manifest(manifest_path)
        provider_name = "gcc"
        runtime_target = runtime_manifest.target
        runtime_arch = runtime_manifest.arch
        runtime_floor = runtime_manifest.glibc_floor
        runtime_version = runtime_manifest.provider.get("version")
        actual_provider = runtime_manifest.provider.get("name")
    else:
        from linux_toolchain.runtime.llvm_models import load_llvm_runtime_manifest

        runtime_manifest = load_llvm_runtime_manifest(manifest_path)
        provider_name = "llvm"
        runtime_target = runtime_manifest.target
        runtime_arch = runtime_manifest.arch
        runtime_floor = runtime_manifest.glibc_floor
        runtime_version = runtime_manifest.provider.get("version")
        actual_provider = runtime_manifest.provider.get("name")
    if (
        actual_provider != provider_name
        or runtime_version != selection.version
        or runtime_arch != selection.target_arch
        or runtime_target != target
        or runtime_floor != selection.target_glibc_floor
    ):
        raise ConfigurationError("managed runtime manifest does not match its lock")
    return ManagedRuntimePublication(
        root=root,
        manifest_path=manifest_path,
        selection=selection,
        receipt=receipt,
        manifest=runtime_manifest,
    )


def _publication_destination(output: Path | str) -> Path:
    raw = Path(output).expanduser()
    if raw.is_symlink():
        raise ConfigurationError(
            f"managed runtime publication cannot be a symlink: {raw}"
        )
    raw.parent.mkdir(parents=True, exist_ok=True)
    destination = raw.parent.resolve(strict=True) / raw.name
    if destination in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(
            f"invalid managed runtime publication path: {destination}"
        )
    return destination


def _existing_publication(
    lock: ManagedLock,
    artifact_id: str,
    destination: Path,
) -> ManagedRuntimePublication | None:
    if not destination.exists():
        return None
    if destination.is_symlink() or not destination.is_dir():
        raise ConfigurationError(
            f"managed runtime publication is not a regular directory: {destination}"
        )
    try:
        nonempty = next(destination.iterdir(), None) is not None
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect managed runtime publication {destination}: {error}"
        ) from error
    if not nonempty:
        return None
    return load_managed_runtime_publication(lock, artifact_id, destination)


def _publish_managed_runtime_loaded(
    lock: ManagedLock,
    artifact_id: str,
    artifact: Path | str,
    output: Path | str,
    *,
    force: bool = False,
) -> ManagedRuntimePublication:
    """Convert a raw managed runtime into a validated binding input."""

    loaded = load_managed_runtime_artifact(lock, artifact_id, artifact)
    selection = loaded.selection
    if selection.target_glibc_floor is None:
        raise ConfigurationError("managed runtime has no target glibc floor")
    if selection.runtime_kind not in {"gcc-runtime", "llvm-runtime"}:
        raise ConfigurationError(
            f"unsupported managed runtime kind: {selection.runtime_kind}"
        )

    destination = _publication_destination(output)
    existing = _existing_publication(lock, artifact_id, destination)
    if existing is not None and not force:
        return existing

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.staging-",
            dir=destination.parent,
        )
    )

    try:
        if selection.runtime_kind == "gcc-runtime":
            from linux_toolchain.runtime.importer import (
                _materialize_gcc_runtime,
                validate_runtime_manifest,
            )

            _materialize_gcc_runtime(
                loaded.payload,
                selection.target_glibc_floor,
                selection.target_arch,
                staging,
                provider_version=selection.version,
                target=loaded.target,
                licenses=loaded.root,
            )
            validate_runtime = validate_runtime_manifest
        else:
            assert selection.runtime_kind == "llvm-runtime"
            from linux_toolchain.runtime.llvm import (
                _materialize_llvm_runtime,
                validate_llvm_runtime_manifest,
            )

            evidence = _load_managed_llvm_source_evidence(
                loaded.manifest_path,
                loaded.payload,
                version=selection.version,
                glibc_floor=selection.target_glibc_floor,
                arch=selection.target_arch,
                target=loaded.target,
            )
            _materialize_llvm_runtime(
                loaded.payload,
                selection.version,
                selection.target_glibc_floor,
                selection.target_arch,
                loaded.target,
                staging,
                licenses=loaded.root,
                source_evidence=evidence,
            )
            validate_runtime = validate_llvm_runtime_manifest
        published = staging / "manifest.json"
        _write_publication_receipt(loaded, published)

        current = _existing_publication(lock, artifact_id, destination)
        if current is not None and not force:
            raise ConfigurationError(
                f"managed runtime publication appeared while staging: {destination}"
            )

        validated: ManagedRuntimePublication | None = None

        def validate_final(publication: Path) -> None:
            nonlocal validated
            validate_runtime(publication)
            validated = load_managed_runtime_publication(lock, artifact_id, publication)

        replace_directory(staging, destination, validate=validate_final)
        assert validated is not None
        return validated
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def publish_managed_runtime(
    lock: ManagedLock,
    artifact_id: str,
    artifact: Path | str,
    output: Path | str,
    *,
    force: bool = False,
) -> Path:
    """Convert a raw managed runtime into a validated binding input."""

    return _publish_managed_runtime_loaded(
        lock,
        artifact_id,
        artifact,
        output,
        force=force,
    ).manifest_path
