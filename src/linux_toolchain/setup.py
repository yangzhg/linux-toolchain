from __future__ import annotations

import hashlib
import os
import platform
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

from linux_toolchain.build_tools import (
    DEFAULT_CMAKE_VERSION,
    BuildToolsArtifact,
    build_tools_producer_identity,
    load_build_tools,
)
from linux_toolchain.build_tools_builder import build_build_tools
from linux_toolchain.bundle import (
    ValidatedPayloadInputs,
    create_bundle_from_validated_inputs,
    publish_installation_from_validated_inputs,
)
from linux_toolchain.compiler.managed import validate_current_host
from linux_toolchain.conan.settings import write_settings_user
from linux_toolchain.container import packaged_builder_dockerfile
from linux_toolchain.diagnostics import run_diagnostics
from linux_toolchain.errors import ConfigurationError, LinuxToolchainError
from linux_toolchain.managed import (
    ManagedLock,
    resolve_lock,
    write_lockfile,
)
from linux_toolchain.managed.assemble import assemble_variant, variant_artifact_paths
from linux_toolchain.managed.contracts import managed_compiler_backend_spec
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimeSetPublication,
    load_managed_compiler_artifact,
    load_managed_runtime_set_publication,
)
from linux_toolchain.models import SdkSpec, normalize_architecture
from linux_toolchain.process import run as run_process
from linux_toolchain.producer_store import (
    ProducerStore,
)
from linux_toolchain.publication import file_lock, write_json_atomic
from linux_toolchain.recipes import get_recipe
from linux_toolchain.sdk.crosstool_ng import (
    FULL_BUILD_GOAL,
    SDK_BUILD_GOAL,
    BuildGoal,
    build_with_docker,
    export_sdk,
    load_workspace,
    render_workspace,
    sdk_producer_identity,
    validate_sdk,
    workspace_satisfies_build_goal,
)
from linux_toolchain.setup_models import (
    SETUP_CONFIG_FORMAT,
    SETUP_CONFIG_SCHEMA,
    ConanRunConfig,
    PreparedSetup,
    SetupConfig,
)
from linux_toolchain.smoke import (
    SMOKE_EVIDENCE,
    SmokeFailure,
    SmokeRequest,
    load_smoke_result,
    run_smoke,
)

DEFAULT_CONFIG_NAME = "setup.json"
DEFAULT_STATE_DIRECTORY = "state"

_ROOT_MARKER = ".linux-toolchain-setup-root"
_STATE_MARKER = ".linux-toolchain-setup-state"
_STATE_LOCK = ".linux-toolchain-setup.lock"
ProgressCallback = Callable[[str], None]
TransferProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class PreparedSetupInputs:
    lock: ManagedLock
    sdk: Path
    build_tools: Path
    compiler_kit: Path
    runtime: Path
    binding: Path


@dataclass(frozen=True)
class LeasedSetupInputs(PreparedSetupInputs):
    build_tools_artifact: BuildToolsArtifact
    compiler_artifact: ManagedCompilerArtifact
    runtime_set_publication: ManagedRuntimeSetPublication


class _PreparedProducerInputsChanged(ConfigurationError):
    """The setup selection is unchanged but its generated producer inputs moved."""


def _payload_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    inputs: LeasedSetupInputs,
    *,
    bundle_id: str | None = None,
) -> ValidatedPayloadInputs:
    return ValidatedPayloadInputs.from_artifacts(
        sdk=inputs.sdk,
        lock=inputs.lock,
        variant=prepared.variant,
        bundle_id=bundle_id,
        integrations=config.selected_integrations,
        conan=config.conan_settings(),
        build_tools_artifact=inputs.build_tools_artifact,
        compiler_artifact=inputs.compiler_artifact,
        runtime_set_publication=inputs.runtime_set_publication,
    )


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _emit(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _state_root(config_path: Path, state_directory: Path | None) -> Path:
    raw = (
        state_directory.expanduser()
        if state_directory is not None
        else config_path.parent / DEFAULT_STATE_DIRECTORY
    )
    if raw.is_symlink():
        raise ConfigurationError(f"setup state directory cannot be a symlink: {raw}")
    root = raw.resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid setup state directory: {root}")
    return root


def _setup_root(prefix: Path | str) -> Path:
    raw = Path(prefix).expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"setup prefix cannot be a symlink: {raw}")
    root = raw.resolve()
    if root in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid setup prefix: {root}")
    return root


def _user_cache_base() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".cache"
    if not base.is_absolute():
        raise ConfigurationError("XDG_CACHE_HOME must be an absolute path")
    return base


def _default_work_directory(cache_base: Path, installation: Path) -> Path:
    normalized = installation.resolve(strict=False)
    digest = hashlib.sha256(str(normalized).encode("utf-8")).hexdigest()[:12]
    basename = normalized.name or "root"
    return cache_base / "linux-toolchain" / f"{basename}-{digest}"


def _prepare_setup_root(root: Path) -> None:
    if root.exists():
        if not _directory(root):
            raise ConfigurationError(f"setup prefix is not a directory: {root}")
        if next(root.iterdir(), None) is not None:
            marker = root / _ROOT_MARKER
            if (
                not _regular_file(marker)
                or marker.read_text(encoding="utf-8") != "format=1\n"
            ):
                raise ConfigurationError(
                    f"refusing to use unowned setup prefix: {root}"
                )
            return
    root.mkdir(parents=True, exist_ok=True)
    (root / _ROOT_MARKER).write_text("format=1\n", encoding="utf-8")


def _prepare_state_directory(root: Path) -> None:
    if root.exists():
        if not root.is_dir():
            raise ConfigurationError(f"setup state path is not a directory: {root}")
        nonempty = next(root.iterdir(), None) is not None
        if nonempty:
            marker = root / _STATE_MARKER
            if (
                not _regular_file(marker)
                or marker.read_text(encoding="utf-8") != "format=1\n"
            ):
                raise ConfigurationError(
                    f"refusing to use unowned setup state directory: {root}"
                )
            return
    root.mkdir(parents=True, exist_ok=True)
    (root / _STATE_MARKER).write_text("format=1\n", encoding="utf-8")


def _require_state_directory(root: Path) -> None:
    if not root.is_dir():
        raise ConfigurationError(f"setup state directory does not exist: {root}")
    marker = root / _STATE_MARKER
    if not _regular_file(marker) or marker.read_text(encoding="utf-8") != "format=1\n":
        raise ConfigurationError(f"invalid setup state ownership marker: {marker}")


@contextmanager
def _state_file_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    _require_state_directory(root)
    path = root / _STATE_LOCK
    with file_lock(
        path,
        shared=not exclusive,
        context="setup state",
    ):
        yield


def _setup_locations(
    *,
    prefix: Path | str | None,
    work_dir: Path | str | None,
    store_dir: Path | str | None,
    install: bool,
) -> tuple[Path | None, Path, Path]:
    installation = Path(prefix).expanduser() if prefix is not None else None
    if install and installation is None:
        raise ConfigurationError("setup installation requires --prefix")
    cache_base = _user_cache_base() if work_dir is None or store_dir is None else None
    if work_dir is None:
        if installation is None:
            raise ConfigurationError(
                "setup requires --work-dir when --prefix is omitted"
            )
        assert cache_base is not None
        root = _setup_root(_default_work_directory(cache_base, installation))
    else:
        root = _setup_root(work_dir)
    if store_dir is None:
        assert cache_base is not None
        producer_store = cache_base / "linux-toolchain" / "store"
    else:
        producer_store = Path(store_dir).expanduser()
    return installation, root, producer_store


def _native_target_architecture(requested: str | None) -> str:
    machine = platform.machine().lower()
    host = normalize_architecture(machine)
    if host not in {"x86_64", "aarch64"}:
        raise ConfigurationError(
            "managed setup requires an x86_64 or AArch64 Linux host"
        )
    target = host if requested is None else requested
    if target != host:
        raise ConfigurationError(
            "managed setup supports native production only; target architecture "
            f"{target} does not match host architecture {host}"
        )
    return target


def _write_setup_selection(config_file: Path, requested: SetupConfig) -> None:
    if not (config_file.exists() or config_file.is_symlink()):
        write_json_atomic(config_file, requested.to_dict(), replace=False)
        return
    if not _regular_file(config_file):
        raise ConfigurationError(
            f"setup configuration is not a regular file: {config_file}"
        )
    current = SetupConfig.load(config_file)
    if current.selection_dict() != requested.selection_dict():
        raise ConfigurationError(
            "setup work directory selects a different toolchain; use a "
            "different --work-dir"
        )
    if current.jobs != requested.jobs:
        write_json_atomic(config_file, requested.to_dict())


def setup_toolchain(
    compiler: str,
    *,
    prefix: Path | str | None,
    work_dir: Path | str | None = None,
    store_dir: Path | str | None = None,
    arch: str | None,
    glibc_floor: str,
    integration: str,
    cmake_version: str = DEFAULT_CMAKE_VERSION,
    libstdcxx: str | None = None,
    host_glibc_floor: str | None = None,
    jobs: int = 1,
    runner: str | None = None,
    conan_cppstd: str | None = None,
    conan_build_type: str | None = None,
    conan_build_profile: str | None = None,
    install: bool = True,
    force: bool = False,
    progress: ProgressCallback | None = None,
    source_progress: TransferProgressCallback | None = None,
) -> Path:
    """Build and install one machine-local managed toolchain selection."""

    installation, root, producer_store = _setup_locations(
        prefix=prefix,
        work_dir=work_dir,
        store_dir=store_dir,
        install=install,
    )
    config_file = root / DEFAULT_CONFIG_NAME
    target_arch = _native_target_architecture(arch)
    selected_host_floor = glibc_floor if host_glibc_floor is None else host_glibc_floor
    value: dict[str, object] = {
        "schema": SETUP_CONFIG_SCHEMA,
        "format": SETUP_CONFIG_FORMAT,
        "compiler": compiler,
        "target": {"arch": target_arch, "glibc_floor": glibc_floor},
        "integration": integration,
        "host_glibc_floor": selected_host_floor,
        "cmake_version": cmake_version,
        "jobs": jobs,
    }
    if libstdcxx is not None:
        value["libstdcxx"] = libstdcxx
    if runner is not None:
        value["runner"] = runner
    conan_values = {
        "cppstd": conan_cppstd,
        "build_type": conan_build_type,
        "build_profile": conan_build_profile,
    }
    if any(item is not None for item in conan_values.values()):
        value["conan"] = {
            key: item for key, item in conan_values.items() if item is not None
        }
    requested = SetupConfig.from_dict(value)
    validate_current_host(requested.managed_spec().host.to_dict())
    _prepare_setup_root(root)
    state = root / DEFAULT_STATE_DIRECTORY
    _prepare_state_directory(state)
    with _state_file_lock(state, exclusive=True):
        _write_setup_selection(config_file, requested)
        prepared = _prepare_setup_unlocked(
            config_file,
            state_directory=state,
            store_directory=producer_store,
            force=force,
            progress=progress,
            source_progress=source_progress,
        )
        if not install:
            return state / "prepared.json"
        assert installation is not None
        with _lock_prepared_producer_inputs(
            requested,
            prepared,
            state=state,
        ) as stable_inputs:
            return publish_installation_from_validated_inputs(
                _payload_inputs(requested, prepared, stable_inputs),
                prefix=installation,
                conan_home=None,
                conan_build_profile=None,
                binding_template=stable_inputs.binding,
                force=force,
                progress=progress,
            )


def _diagnose() -> None:
    producer = run_diagnostics("managed")
    if not producer.passed:
        raise ConfigurationError(
            "managed producer prerequisites failed:\n" + producer.to_text()
        )


def _sdk_is_ready(
    workspace: Path,
    expected: SdkSpec,
    goal: BuildGoal,
) -> bool:
    manifest = workspace / "workspace.json"
    sdk_manifest = workspace / "sdk" / "manifest.json"
    if (
        not manifest.is_file()
        or not sdk_manifest.is_file()
        or not workspace_satisfies_build_goal(expected, workspace, goal)
    ):
        return False
    actual = load_workspace(workspace)
    if actual != expected:
        return False
    validate_sdk(workspace / "sdk" / "sysroot", arch=expected.target.arch)
    return True


def _sdk_spec(
    config: SetupConfig,
    *,
    arch: str | None = None,
    glibc_floor: str | None = None,
) -> SdkSpec:
    selected_arch = config.target.arch if arch is None else arch
    selected_floor = config.target.glibc_floor if glibc_floor is None else glibc_floor
    return get_recipe(selected_arch, selected_floor).to_spec(
        name=f"setup-{selected_arch}-glibc-{selected_floor}",
    )


def _ensure_sdk(
    config: SetupConfig,
    workspace: Path,
    *,
    source_cache: Path,
    arch: str | None = None,
    glibc_floor: str | None = None,
    goal: BuildGoal,
    force: bool,
    progress: ProgressCallback | None,
    source_progress: TransferProgressCallback | None,
) -> Path:
    spec = _sdk_spec(
        config,
        arch=arch,
        glibc_floor=glibc_floor,
    )
    try:
        ready = _sdk_is_ready(workspace, spec, goal)
    except LinuxToolchainError:
        if not force:
            raise
        ready = False
    if ready:
        _emit(progress, "sdk: using validated existing SDK")
        return workspace
    manifest_exists = (workspace / "workspace.json").is_file()
    render = True
    if manifest_exists:
        existing = load_workspace(workspace)
        if existing != spec:
            raise ConfigurationError(
                "setup SDK workspace belongs to a different configuration; "
                "use its matching producer identity instead of replacing it"
            )
        if existing == spec and not force:
            render = False
            _emit(progress, "sdk: resuming existing pinned workspace")
    if render:
        _emit(progress, "sdk: rendering pinned workspace")
        render_workspace(spec, workspace, force=force or manifest_exists)
    dockerfile = packaged_builder_dockerfile().resolve()
    build_with_docker(
        spec,
        workspace,
        dockerfile=dockerfile,
        image=None,
        jobs=config.jobs,
        progress=progress,
        source_cache=source_cache,
        source_progress=source_progress,
        goal=goal,
    )
    export_sdk(spec, workspace)
    return workspace


def _ensure_compiler_backend(
    config: SetupConfig,
    store: ProducerStore,
    target_sdk_spec: SdkSpec,
    target_sdk_workspace: Path,
    *,
    force: bool,
    progress: ProgressCallback | None,
    source_progress: TransferProgressCallback | None,
) -> Path:
    backend_spec = managed_compiler_backend_spec(
        config.target.arch,
        config.host_glibc_floor,
    )
    if backend_spec == target_sdk_spec:
        return target_sdk_workspace
    workspace = store.sdk_workspace(backend_spec)
    with store.lock("sdk", sdk_producer_identity(backend_spec)):
        return _ensure_sdk(
            config,
            workspace,
            source_cache=store.source_archive_cache,
            arch=config.target.arch,
            glibc_floor=config.host_glibc_floor,
            goal=FULL_BUILD_GOAL,
            force=force,
            progress=progress,
            source_progress=source_progress,
        )


def _prepare_conan(config: SetupConfig, binding: Path) -> ConanRunConfig:
    assert config.conan is not None
    build_profile_name = config.conan.build_profile or "default"
    executable = shutil.which("conan")
    if executable is None:
        raise ConfigurationError("Conan integration requires a Conan 2 executable")
    home_text = run_process([executable, "config", "home"]).stdout.strip()
    if not home_text:
        raise ConfigurationError("Conan did not report its configuration home")
    raw_home = Path(home_text).expanduser()
    if raw_home.is_symlink():
        raise ConfigurationError(f"Conan home cannot be a symlink: {raw_home}")
    home = raw_home.resolve()
    if not _directory(home):
        raise ConfigurationError(f"Conan home is not a directory: {home}")
    settings_file = write_settings_user(home / "settings_user.yml")
    if not _regular_file(settings_file):
        raise ConfigurationError(
            f"Conan settings file is not a regular file: {settings_file}"
        )
    environment = os.environ.copy()
    environment["CONAN_HOME"] = str(home)
    run_process(
        [
            executable,
            "profile",
            "detect",
            "--name",
            build_profile_name,
            "--exist-ok",
        ],
        env=environment,
    )
    profile_text = run_process(
        [executable, "profile", "path", build_profile_name],
        env=environment,
    ).stdout.strip()
    build_profile = Path(profile_text).resolve()
    if not _regular_file(build_profile):
        raise ConfigurationError(
            f"Conan build profile is not a regular file: {build_profile}"
        )
    host_profile = binding / "conan" / "host.profile"
    if not host_profile.is_file():
        raise ConfigurationError(
            f"prepared binding has no Conan host profile: {host_profile}"
        )
    return ConanRunConfig(
        home=home,
        build_profile=build_profile,
    )


def _smoke_request(
    config: SetupConfig,
    binding: Path,
    build_tools: Path,
    build_dir: Path,
    conan: ConanRunConfig | None,
) -> SmokeRequest:
    return SmokeRequest(
        binding=binding,
        build_profile=(str(conan.build_profile) if conan is not None else None),
        build_dir=build_dir,
        integration=config.smoke_integration,
        build_type=(
            config.conan.build_type
            if config.smoke_integration == "conan" and config.conan is not None
            else "Release"
        ),
        conan=os.environ.get("CONAN", "conan"),
        cmake=str(build_tools / "bin" / "cmake"),
        make=str(build_tools / "bin" / "make"),
        conan_home=(conan.home if conan is not None else None),
        runner=config.runner,
        jobs=config.jobs,
    )


def _validate_prepared_selection(
    config: SetupConfig,
    prepared: PreparedSetup,
    state: Path,
) -> None:
    if prepared.config_sha256 != config.selection_sha256:
        raise ConfigurationError(
            "prepared setup state does not match setup.json; "
            "rerun linux-toolchain setup --force"
        )
    if prepared.integration != config.smoke_integration:
        raise ConfigurationError("prepared setup integration does not match config")
    expected_paths = {
        "binding": state / "binding",
        "lock": state / "managed.lock.json",
    }
    for field, expected in expected_paths.items():
        actual = getattr(prepared, field)
        if actual != expected:
            raise ConfigurationError(
                f"prepared setup {field} does not match its state directory"
            )
    expected_smoke = state / f"smoke-{config.smoke_integration}" / "result.json"
    if prepared.smoke_result is not None and prepared.smoke_result != expected_smoke:
        raise ConfigurationError(
            "prepared setup verification result does not match its state directory"
        )


def _canonical_prepared_workspace(path: Path, *, field: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"prepared setup {field} cannot be resolved"
        ) from error
    if resolved != path:
        raise ConfigurationError(f"prepared setup {field} is not a canonical path")
    return resolved


def _validate_prepared_child(
    path: Path,
    *,
    field: str,
    workspace: Path,
    owner: str,
) -> None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"prepared setup {field} cannot be resolved"
        ) from error
    if resolved != path or resolved == workspace or workspace not in resolved.parents:
        raise ConfigurationError(f"prepared setup {field} is outside its {owner}")


def _validate_prepared_path_boundaries(prepared: PreparedSetup) -> None:
    managed_workspace = _canonical_prepared_workspace(
        prepared.managed_workspace,
        field="managed workspace",
    )
    build_tools_workspace = _canonical_prepared_workspace(
        prepared.build_tools_workspace,
        field="build tools workspace",
    )
    for field in ("compiler_kit", "runtime"):
        _validate_prepared_child(
            getattr(prepared, field),
            field=field,
            workspace=managed_workspace,
            owner="managed workspace",
        )
    _validate_prepared_child(
        prepared.build_tools,
        field="build tools artifact",
        workspace=build_tools_workspace,
        owner="workspace",
    )


def _load_prepared_lock(
    config: SetupConfig,
    prepared: PreparedSetup,
) -> ManagedLock:
    if not _regular_file(prepared.lock):
        raise ConfigurationError(f"prepared managed lock is missing: {prepared.lock}")
    prepared_lock = ManagedLock.load(prepared.lock)
    expected_lock = resolve_lock(config.managed_spec())
    if prepared_lock.sha256 != expected_lock.sha256:
        raise ConfigurationError(
            "prepared managed lock does not match the setup configuration"
        )
    prepared_lock.variant(prepared.variant)
    return prepared_lock


def _validate_prepared_producer_identity(
    config: SetupConfig,
    prepared: PreparedSetup,
    prepared_lock: ManagedLock,
) -> None:
    target_sdk_spec = _sdk_spec(config)
    compiler_backend_spec = managed_compiler_backend_spec(
        config.target.arch,
        config.host_glibc_floor,
    )
    store = ProducerStore.load(prepared.sdk_workspace.parent.parent)
    expected_sdk_workspace = store.sdk_workspace(target_sdk_spec)
    expected_managed_workspace = store.managed_workspace(
        target_sdk_spec,
        compiler_backend_spec,
    )
    expected_build_tools_workspace = store.build_tools_workspace(
        config.build_tools_spec(),
        compiler_backend_spec,
    )
    if prepared.sdk_workspace != expected_sdk_workspace:
        raise _PreparedProducerInputsChanged(
            "prepared setup SDK workspace does not match its producer store identity"
        )
    if prepared.managed_workspace != expected_managed_workspace:
        raise _PreparedProducerInputsChanged(
            "prepared setup managed workspace does not match its producer store identity"
        )
    if prepared.build_tools_workspace != expected_build_tools_workspace:
        raise _PreparedProducerInputsChanged(
            "prepared setup build tools workspace does not match its producer "
            "store identity"
        )
    if prepared.build_tools != expected_build_tools_workspace / "artifact":
        raise _PreparedProducerInputsChanged(
            "prepared setup build tools artifact does not match its producer identity"
        )
    expected_artifacts = variant_artifact_paths(
        prepared_lock,
        prepared.variant,
        expected_managed_workspace,
        target_sdk_spec,
        compiler_backend_spec,
    )
    for field, expected in (
        ("compiler_kit", expected_artifacts.compiler_kit),
        ("runtime", expected_artifacts.runtime_set),
    ):
        if getattr(prepared, field) != expected:
            raise _PreparedProducerInputsChanged(
                f"prepared setup {field} does not match its producer identity"
            )


def _validate_prepared_directories(prepared: PreparedSetup) -> None:
    for path in (
        prepared.binding,
        prepared.sdk_workspace,
        prepared.sdk_workspace / "sdk",
        prepared.build_tools_workspace,
        prepared.build_tools,
        prepared.managed_workspace,
        prepared.compiler_kit,
        prepared.runtime,
    ):
        if not _directory(path):
            raise ConfigurationError(f"prepared setup directory is missing: {path}")
    if prepared.smoke_result is not None and not _regular_file(prepared.smoke_result):
        raise ConfigurationError(
            f"prepared verification result is missing: {prepared.smoke_result}"
        )


def _validate_prepared_smoke_evidence(
    smoke_result: dict[str, object],
    smoke_directory: Path,
) -> None:
    expected = list(SMOKE_EVIDENCE)
    if smoke_result["evidence"] != expected:
        raise ConfigurationError(
            "prepared verification evidence does not match its inputs"
        )
    for filename in expected:
        path = smoke_directory / filename
        if not _regular_file(path):
            raise ConfigurationError(
                f"prepared verification evidence is missing: {path}"
            )


def _validate_prepared_smoke_artifacts(
    smoke_result: dict[str, object],
    smoke_directory: Path,
) -> None:
    artifact_directory = smoke_directory / "cmake" / "artifacts"
    expected = [
        str(artifact_directory / "linux_toolchain_smoke"),
        str(artifact_directory / "liblinux_toolchain_smoke.so"),
    ]
    if smoke_result["artifacts"] != expected:
        raise ConfigurationError(
            "prepared verification artifacts do not match its build"
        )
    for value in expected:
        if not _regular_file(Path(value)):
            raise ConfigurationError(
                f"prepared verification artifact is missing: {value}"
            )


def _validate_prepared_smoke_conan(
    smoke_result: dict[str, object],
    conan: ConanRunConfig,
) -> None:
    if smoke_result["conan_home"] != str(conan.home):
        raise ConfigurationError(
            "prepared verification Conan home does not match setup state"
        )
    if smoke_result["build_profile"] != str(conan.build_profile):
        raise ConfigurationError(
            "prepared verification build profile does not match setup state"
        )


def _validate_prepared_smoke(
    config: SetupConfig,
    prepared: PreparedSetup,
) -> None:
    if prepared.smoke_result is None:
        return
    if config.smoke_integration == "conan" and prepared.conan is None:
        raise ConfigurationError("prepared setup is missing Conan run state")
    try:
        smoke_result = load_smoke_result(prepared.smoke_result)
    except SmokeFailure as error:
        raise ConfigurationError(
            f"prepared verification result is invalid: {error}"
        ) from error
    request = _smoke_request(
        config,
        prepared.binding,
        prepared.build_tools,
        prepared.smoke_result.parent,
        prepared.conan,
    )
    expected_values = {
        "binding": str(prepared.binding),
        "integration": config.smoke_integration,
        "build_type": request.build_type,
    }
    for field, expected in expected_values.items():
        if smoke_result[field] != expected:
            raise ConfigurationError(
                f"prepared verification result {field} does not match setup state"
            )
    glibc = smoke_result["glibc"]
    assert isinstance(glibc, dict)
    if glibc["policy_floor"] != config.target.glibc_floor:
        raise ConfigurationError(
            "prepared verification GLIBC policy floor does not match setup config"
        )
    _validate_prepared_smoke_evidence(smoke_result, prepared.smoke_result.parent)
    _validate_prepared_smoke_artifacts(smoke_result, prepared.smoke_result.parent)
    if config.smoke_integration == "conan":
        assert prepared.conan is not None
        _validate_prepared_smoke_conan(smoke_result, prepared.conan)


def _validate_prepared_binding(
    config: SetupConfig,
    prepared: PreparedSetup,
) -> None:
    required = (
        prepared.binding / "binding.json",
        prepared.binding / "audit-policy.json",
        prepared.binding / "env" / "toolchain.env",
    )
    for path in required:
        if not _regular_file(path):
            raise ConfigurationError(f"prepared binding file is missing: {path}")
    selected = {
        "cmake": prepared.binding / "cmake" / "toolchain.cmake",
        "shell": prepared.binding / "env" / "toolchain.env",
        "conan": prepared.binding / "conan" / "host.profile",
    }[config.smoke_integration]
    if not _regular_file(selected):
        raise ConfigurationError(f"prepared integration file is missing: {selected}")
    if config.smoke_integration == "conan":
        if prepared.conan is None:
            raise ConfigurationError("prepared setup is missing Conan run state")
        if not _regular_file(prepared.conan.build_profile):
            raise ConfigurationError(
                f"prepared Conan build profile is missing: {prepared.conan.build_profile}"
            )
        if not _directory(prepared.conan.home):
            raise ConfigurationError(
                f"prepared Conan home is missing: {prepared.conan.home}"
            )
        settings_file = prepared.conan.home / "settings_user.yml"
        if not _regular_file(settings_file):
            raise ConfigurationError(
                f"prepared Conan settings are missing: {settings_file}"
            )
    elif prepared.conan is not None:
        raise ConfigurationError("non-Conan prepared setup contains Conan state")


def _prepared_producer_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    state: Path,
) -> PreparedSetupInputs:
    _validate_prepared_selection(config, prepared, state)
    _validate_prepared_path_boundaries(prepared)
    prepared_lock = _load_prepared_lock(config, prepared)
    _validate_prepared_producer_identity(config, prepared, prepared_lock)
    return PreparedSetupInputs(
        lock=prepared_lock,
        sdk=prepared.sdk_workspace / "sdk",
        build_tools=prepared.build_tools,
        compiler_kit=prepared.compiler_kit,
        runtime=prepared.runtime,
        binding=prepared.binding,
    )


def _validate_prepared_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    state: Path,
) -> PreparedSetupInputs:
    inputs = _prepared_producer_inputs(config, prepared, state)
    _validate_prepared_directories(prepared)
    _validate_prepared_smoke(config, prepared)
    _validate_prepared_binding(config, prepared)
    return inputs


@contextmanager
def lock_prepared_setup_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    *,
    state: Path,
) -> Iterator[LeasedSetupInputs]:
    """Validate and hold one prepared state stable while it is consumed."""

    expected_state = state.expanduser().resolve()
    with _state_file_lock(expected_state, exclusive=False):
        prepared_path = expected_state / "prepared.json"
        if not _regular_file(prepared_path):
            raise ConfigurationError(
                f"prepared setup state is missing: {prepared_path}; "
                "rerun linux-toolchain setup"
            )
        current = PreparedSetup.load(prepared_path)
        if current != prepared:
            raise ConfigurationError(
                "prepared setup state changed while waiting for its lock"
            )
        with _lock_prepared_producer_inputs(
            config,
            current,
            state=expected_state,
        ) as inputs:
            yield inputs


def _producer_lease_identities(
    config: SetupConfig,
    prepared: PreparedSetup,
    inputs: PreparedSetupInputs,
) -> tuple[tuple[str, dict[str, object]], ...]:
    variant = inputs.lock.variant(prepared.variant)

    def managed_identity(artifact: str) -> dict[str, object]:
        return {
            "workspace": prepared.managed_workspace.name,
            "artifact": artifact,
        }

    return (
        ("sdk", sdk_producer_identity(_sdk_spec(config))),
        (
            "build-tools",
            build_tools_producer_identity(
                config.build_tools_spec(),
                managed_compiler_backend_spec(
                    config.target.arch,
                    config.host_glibc_floor,
                ),
            ),
        ),
        (
            "managed-artifact",
            managed_identity(variant.compiler_kit_id),
        ),
        *(
            ("managed-artifact", managed_identity(runtime_id))
            for runtime_id in variant.runtime_ids
        ),
        (
            "managed-artifact",
            managed_identity(f"runtime-set-{variant.id}"),
        ),
    )


@contextmanager
def _lock_prepared_producer_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    *,
    state: Path,
) -> Iterator[LeasedSetupInputs]:
    """Acquire producer read leases, then revalidate before consuming inputs."""

    initial = _prepared_producer_inputs(config, prepared, state)
    store = ProducerStore.load(prepared.sdk_workspace.parent.parent)
    identities = _producer_lease_identities(config, prepared, initial)
    with store.lock_many(identities, shared=True):
        stable = _validate_prepared_inputs(config, prepared, state)
        build_tools, compiler_artifact, runtime_set = (
            _validate_leased_producer_artifacts(
                config,
                prepared,
                stable,
            )
        )
        yield LeasedSetupInputs(
            lock=stable.lock,
            sdk=stable.sdk,
            build_tools=stable.build_tools,
            compiler_kit=stable.compiler_kit,
            runtime=stable.runtime,
            binding=stable.binding,
            build_tools_artifact=build_tools,
            compiler_artifact=compiler_artifact,
            runtime_set_publication=runtime_set,
        )


def _validate_leased_producer_artifacts(
    config: SetupConfig,
    prepared: PreparedSetup,
    inputs: PreparedSetupInputs,
) -> tuple[
    BuildToolsArtifact,
    ManagedCompilerArtifact,
    ManagedRuntimeSetPublication,
]:
    target_spec = _sdk_spec(config)
    backend_spec = managed_compiler_backend_spec(
        config.target.arch,
        config.host_glibc_floor,
    )
    target_goal = FULL_BUILD_GOAL if target_spec == backend_spec else SDK_BUILD_GOAL
    if not _sdk_is_ready(prepared.sdk_workspace, target_spec, target_goal):
        raise ConfigurationError("prepared target SDK is no longer ready")
    build_tools = load_build_tools(
        inputs.build_tools,
        expected_identity=build_tools_producer_identity(
            config.build_tools_spec(),
            backend_spec,
        ),
    )
    variant = inputs.lock.variant(prepared.variant)
    compiler_artifact = load_managed_compiler_artifact(
        inputs.lock,
        variant.compiler_kit_id,
        inputs.compiler_kit,
    )
    runtime_set = load_managed_runtime_set_publication(
        inputs.lock,
        variant.id,
        inputs.runtime,
    )
    return build_tools, compiler_artifact, runtime_set


def load_prepared_setup_state(
    config_path: Path | str = Path(DEFAULT_CONFIG_NAME),
    *,
    state_directory: Path | None = None,
) -> tuple[SetupConfig, PreparedSetup]:
    """Load the setup record before consuming it under its producer leases."""

    config_file = Path(config_path).expanduser().resolve()
    config = SetupConfig.load(config_file)
    state = _state_root(config_file, state_directory)
    _require_state_directory(state)
    with _state_file_lock(state, exclusive=False):
        prepared_path = state / "prepared.json"
        if not _regular_file(prepared_path):
            raise ConfigurationError(
                f"prepared setup state is missing: {prepared_path}; "
                "rerun linux-toolchain setup"
            )
        prepared = PreparedSetup.load(prepared_path)
    return config, prepared


def create_prepared_bundle(
    *,
    config: Path,
    state_directory: Path | None,
    output: Path,
    bundle_id: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    archive_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Create an installer while holding validated prepared producer inputs."""

    setup_config, prepared = load_prepared_setup_state(
        config,
        state_directory=state_directory,
    )
    if prepared.smoke_result is None:
        raise ConfigurationError(
            "prepared setup has not passed consumer verification; "
            "rerun linux-toolchain setup"
        )
    state = _state_root(Path(config).expanduser().resolve(), state_directory)
    with lock_prepared_setup_inputs(
        setup_config,
        prepared,
        state=state,
    ) as prepared_inputs:
        return create_bundle_from_validated_inputs(
            _payload_inputs(
                setup_config,
                prepared,
                prepared_inputs,
                bundle_id=bundle_id,
            ),
            output=output,
            binding_template=prepared_inputs.binding,
            force=force,
            progress=progress,
            archive_progress=archive_progress,
        )


def _read_prepared_setup(path: Path) -> PreparedSetup | None:
    if not (path.exists() or path.is_symlink()):
        return None
    if not _regular_file(path):
        raise ConfigurationError(f"invalid prepared setup state file: {path}")
    try:
        return PreparedSetup.load(path)
    except ConfigurationError:
        return None


def _check_prepared_store(
    prepared: PreparedSetup | None,
    *,
    requested_store: Path | None,
) -> None:
    if prepared is None or requested_store is None:
        return
    recorded = prepared.sdk_workspace.parent.parent
    requested = requested_store.expanduser().resolve()
    if requested != recorded:
        raise ConfigurationError(
            "setup state belongs to a different producer store; "
            f"reuse {recorded} or use a new --work-dir"
        )


def _reuse_prepared_setup(
    config: SetupConfig,
    prepared_path: Path,
    state: Path,
    *,
    loaded: PreparedSetup | None,
    progress: ProgressCallback | None,
) -> PreparedSetup | None:
    try:
        prepared = loaded if loaded is not None else PreparedSetup.load(prepared_path)
        _validate_prepared_inputs(config, prepared, state)
        if prepared.smoke_result is not None:
            with _lock_prepared_producer_inputs(
                config,
                prepared,
                state=state,
            ):
                pass
    except _PreparedProducerInputsChanged:
        write_json_atomic(
            prepared_path,
            replace(prepared, smoke_result=None).to_dict(),
        )
        _emit(
            progress,
            "setup: producer inputs changed; refreshing prepared state",
        )
        return None
    except ConfigurationError as error:
        raise ConfigurationError(
            "prepared setup state is invalid; rerun setup with --force "
            f"or use a new --work-dir: {error}"
        ) from error
    if prepared.smoke_result is not None:
        _emit(
            progress,
            f"setup: verification PASS ({config.smoke_integration}, cached)",
        )
        return prepared
    _emit(progress, "setup: completing unqualified prepared state")
    return None


def _prepare_setup_unlocked(
    config_path: Path | str = Path(DEFAULT_CONFIG_NAME),
    *,
    state_directory: Path | None = None,
    store_directory: Path | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    source_progress: TransferProgressCallback | None = None,
) -> PreparedSetup:
    config_file = Path(config_path).expanduser().resolve()
    config = SetupConfig.load(config_file)
    state = _state_root(config_file, state_directory)
    _prepare_state_directory(state)
    prepared_path = state / "prepared.json"
    existing_prepared = _read_prepared_setup(prepared_path)
    _check_prepared_store(existing_prepared, requested_store=store_directory)
    if existing_prepared is not None:
        if force:
            write_json_atomic(
                prepared_path,
                replace(existing_prepared, smoke_result=None).to_dict(),
            )
    store = ProducerStore.prepare(
        state / "producer" if store_directory is None else store_directory
    )
    if not force and (prepared_path.exists() or prepared_path.is_symlink()):
        reused = _reuse_prepared_setup(
            config,
            prepared_path,
            state,
            loaded=existing_prepared,
            progress=progress,
        )
        if reused is not None:
            return reused

    _diagnose()
    _emit(progress, "doctor: PASS")
    target_sdk_spec = _sdk_spec(config)
    compiler_backend_spec = managed_compiler_backend_spec(
        config.target.arch,
        config.host_glibc_floor,
    )
    target_goal = (
        FULL_BUILD_GOAL if target_sdk_spec == compiler_backend_spec else SDK_BUILD_GOAL
    )
    sdk_workspace = store.sdk_workspace(target_sdk_spec)
    with store.lock("sdk", sdk_producer_identity(target_sdk_spec)):
        sdk_workspace = _ensure_sdk(
            config,
            sdk_workspace,
            source_cache=store.source_archive_cache,
            goal=target_goal,
            force=force,
            progress=progress,
            source_progress=source_progress,
        )
    compiler_backend_workspace = _ensure_compiler_backend(
        config,
        store,
        target_sdk_spec,
        sdk_workspace,
        force=force,
        progress=progress,
        source_progress=source_progress,
    )
    build_tools_spec = config.build_tools_spec()
    build_tools_identity = build_tools_producer_identity(
        build_tools_spec,
        compiler_backend_spec,
    )
    build_tools_workspace = store.build_tools_workspace(
        build_tools_spec,
        compiler_backend_spec,
    )
    with (
        store.lock(
            "sdk",
            sdk_producer_identity(compiler_backend_spec),
            shared=True,
        ),
        store.lock("build-tools", build_tools_identity),
    ):
        build_tools_artifact = build_build_tools(
            build_tools_spec,
            compiler_backend_spec,
            compiler_backend_workspace,
            build_tools_workspace,
            source_cache=store.source_archive_cache,
            jobs=config.jobs,
            force=force,
            progress=progress,
            source_progress=source_progress,
        )
    lock: ManagedLock = resolve_lock(config.managed_spec())
    if len(lock.variants) != 1:
        raise ConfigurationError(
            "setup config must resolve to exactly one managed variant"
        )
    variant = lock.variants[0]
    lock_path = write_lockfile(lock, state / "managed.lock.json", force=force)
    if not _regular_file(lock_path):
        raise ConfigurationError(f"managed lockfile is not a regular file: {lock_path}")
    binding = state / "binding"
    managed_workspace = store.managed_workspace(
        target_sdk_spec,
        compiler_backend_spec,
    )
    sdk_readers = (
        ("sdk", sdk_producer_identity(target_sdk_spec)),
        ("sdk", sdk_producer_identity(compiler_backend_spec)),
    )
    artifact_writers = tuple(
        (
            "managed-artifact",
            {
                "workspace": managed_workspace.name,
                "artifact": artifact_id,
            },
        )
        for artifact_id in {
            variant.compiler_kit_id,
            *variant.runtime_ids,
            f"runtime-set-{variant.id}",
        }
    )
    with store.lock_many(sdk_readers, shared=True):
        if not _sdk_is_ready(sdk_workspace, target_sdk_spec, target_goal):
            raise ConfigurationError(
                "target SDK changed before managed assembly could consume it"
            )
        if compiler_backend_workspace != sdk_workspace and not _sdk_is_ready(
            compiler_backend_workspace,
            compiler_backend_spec,
            FULL_BUILD_GOAL,
        ):
            raise ConfigurationError(
                "compiler backend changed before managed assembly could consume it"
            )
        with store.lock_many(artifact_writers):
            result = assemble_variant(
                lock,
                variant.id,
                sdk_workspace,
                compiler_backend_workspace,
                managed_workspace,
                binding,
                jobs=config.jobs,
                integrations=config.selected_integrations,
                conan=config.conan_settings(),
                source_cache=store.managed_source_cache,
                # The binding belongs to this selection-specific state and may be
                # regenerated from the validated immutable artifacts.
                force=True,
                repair=force,
                progress=lambda message: _emit(progress, f"managed: {message}"),
                source_progress=source_progress,
            )
    conan = (
        _prepare_conan(config, binding) if config.smoke_integration == "conan" else None
    )
    unqualified = PreparedSetup(
        config_sha256=config.selection_sha256,
        binding=result.binding_manifest.parent.resolve(),
        lock=lock_path.resolve(),
        variant=variant.id,
        sdk_workspace=sdk_workspace.resolve(),
        build_tools_workspace=build_tools_workspace.resolve(),
        managed_workspace=managed_workspace.resolve(),
        compiler_kit=result.compiler_kit.resolve(),
        runtime=result.runtime.resolve(),
        build_tools=build_tools_artifact.root.resolve(),
        integration=config.smoke_integration,
        smoke_result=None,
        conan=conan,
    )
    with _lock_prepared_producer_inputs(
        config,
        unqualified,
        state=state,
    ) as prepared_inputs:
        _emit(
            progress,
            f"setup: verifying {config.smoke_integration} integration",
        )
        smoke_directory = state / f"smoke-{config.smoke_integration}"
        run_smoke(
            _smoke_request(
                config,
                binding,
                prepared_inputs.build_tools_artifact.root,
                smoke_directory,
                conan,
            )
        )
        smoke_result = smoke_directory / "result.json"
        if not smoke_result.is_file():
            raise ConfigurationError(
                f"consumer verification did not produce its result: {smoke_result}"
            )
        prepared = replace(unqualified, smoke_result=smoke_result.resolve())
        _validate_prepared_inputs(config, prepared, state)
        write_json_atomic(prepared_path, prepared.to_dict())
        _emit(
            progress,
            f"setup: verification PASS ({config.smoke_integration})",
        )
    return prepared


def prepare_setup(
    config_path: Path | str = Path(DEFAULT_CONFIG_NAME),
    *,
    state_directory: Path | None = None,
    store_directory: Path | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    source_progress: TransferProgressCallback | None = None,
) -> Path:
    config_file = Path(config_path).expanduser().resolve()
    state = _state_root(config_file, state_directory)
    _prepare_state_directory(state)
    with _state_file_lock(state, exclusive=True):
        _prepare_setup_unlocked(
            config_file,
            state_directory=state,
            store_directory=store_directory,
            force=force,
            progress=progress,
            source_progress=source_progress,
        )
    return state / "prepared.json"
