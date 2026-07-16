from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import platform
import shutil
import sys
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Callable, Iterator

from linux_toolchain.bundle import create_bundle, publish_installation
from linux_toolchain.compiler.managed import validate_current_host
from linux_toolchain.conan.settings import write_settings_user
from linux_toolchain.container import BUILDER_DOCKERFILE_NAME
from linux_toolchain.diagnostics import run_diagnostics
from linux_toolchain.errors import ConfigurationError, LinuxToolchainError
from linux_toolchain.managed import (
    ManagedLock,
    resolve_lock,
    write_lockfile,
)
from linux_toolchain.managed.assemble import assemble_variant
from linux_toolchain.managed.contracts import managed_compiler_backend_spec
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimePublication,
    load_managed_compiler_artifact,
    load_managed_runtime_publication,
)
from linux_toolchain.models import SdkSpec
from linux_toolchain.process import run as run_process
from linux_toolchain.producer_store import (
    ProducerStore,
    sdk_build_identity,
)
from linux_toolchain.publication import write_json_atomic
from linux_toolchain.recipes import get_recipe
from linux_toolchain.sdk.crosstool_ng import (
    FULL_BUILD_GOAL,
    SDK_BUILD_GOAL,
    BuildGoal,
    build_with_docker,
    export_sdk,
    load_workspace,
    render_workspace,
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
    _EVIDENCE,
    SmokeFailure,
    load_smoke_result,
)
from linux_toolchain.smoke import (
    run as run_smoke,
)

DEFAULT_CONFIG_NAME = "setup.json"
DEFAULT_STATE_DIRECTORY = "state"

_ROOT_MARKER = ".linux-toolchain-setup-root"
_STATE_MARKER = ".linux-toolchain-setup-state"
_STATE_LOCK = ".linux-toolchain-setup.lock"
_NATIVE_ARCHITECTURE_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}

ProgressCallback = Callable[[str], None]
TransferProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class PreparedSetupInputs:
    lock: ManagedLock
    sdk: Path
    compiler_kit: Path
    runtime: Path
    binding: Path
    compiler_artifact: ManagedCompilerArtifact | None = None
    runtime_publication: ManagedRuntimePublication | None = None

    def validated_artifacts(
        self,
    ) -> tuple[ManagedCompilerArtifact, ManagedRuntimePublication]:
        if self.compiler_artifact is None or self.runtime_publication is None:
            raise ConfigurationError(
                "prepared producer artifacts are not held under a validated lease"
            )
        return self.compiler_artifact, self.runtime_publication


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
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ConfigurationError(
            f"cannot open setup state lock {path}: {error}"
        ) from error
    with os.fdopen(descriptor, "r+", encoding="ascii") as stream:
        try:
            fcntl.flock(
                stream.fileno(),
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
        except OSError as error:
            raise ConfigurationError(
                f"cannot lock setup state {path}: {error}"
            ) from error
        yield


def setup_toolchain(
    compiler: str,
    *,
    prefix: Path | str | None,
    work_dir: Path | str | None = None,
    store_dir: Path | str | None = None,
    arch: str | None,
    glibc_floor: str,
    integration: str,
    runtime: str | None = None,
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
    config_file = root / DEFAULT_CONFIG_NAME
    machine = platform.machine().lower()
    host_arch = _NATIVE_ARCHITECTURE_ALIASES.get(machine, machine)
    if host_arch not in {"x86_64", "aarch64"}:
        raise ConfigurationError(
            "managed setup requires an x86_64 or AArch64 Linux host"
        )
    target_arch = arch
    if target_arch is None:
        target_arch = host_arch
    if target_arch != host_arch:
        raise ConfigurationError(
            "managed setup supports native production only; target architecture "
            f"{target_arch} does not match host architecture {host_arch}"
        )
    selected_host_floor = glibc_floor if host_glibc_floor is None else host_glibc_floor
    value: dict[str, object] = {
        "schema": SETUP_CONFIG_SCHEMA,
        "format": SETUP_CONFIG_FORMAT,
        "compiler": compiler,
        "target": {"arch": target_arch, "glibc_floor": glibc_floor},
        "integration": integration,
        "host_glibc_floor": selected_host_floor,
        "jobs": jobs,
    }
    if runtime is not None:
        value["runtime"] = runtime
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
        if config_file.exists() or config_file.is_symlink():
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
        else:
            write_json_atomic(config_file, requested.to_dict(), replace=False)
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
            compiler_artifact, runtime_publication = stable_inputs.validated_artifacts()
            return publish_installation(
                sdk=stable_inputs.sdk,
                compiler_kit=stable_inputs.compiler_kit,
                runtime=stable_inputs.runtime,
                lock=stable_inputs.lock,
                variant=prepared.variant,
                prefix=installation,
                integrations=requested.selected_integrations,
                conan=requested.conan_settings(),
                conan_home=None,
                conan_build_profile=None,
                binding_template=stable_inputs.binding,
                force=force,
                progress=progress,
                _compiler_artifact=compiler_artifact,
                _runtime_publication=runtime_publication,
            )


def _diagnose(config: SetupConfig) -> None:
    consumer = run_diagnostics("consumer", (config.integration,))
    if not consumer.passed:
        raise ConfigurationError(
            "consumer workflow prerequisites failed:\n" + consumer.to_text()
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
    dockerfile = Path(
        str(files("linux_toolchain.resources").joinpath(BUILDER_DOCKERFILE_NAME))
    ).resolve()
    build_with_docker(
        spec,
        workspace,
        dockerfile=dockerfile,
        image=None,
        jobs=config.jobs,
        progress=progress,
        source_cache=source_cache,
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
) -> Path:
    backend_spec = managed_compiler_backend_spec(
        config.target.arch,
        config.host_glibc_floor,
    )
    if backend_spec == target_sdk_spec:
        return target_sdk_workspace
    workspace = store.sdk_workspace(backend_spec)
    with store.lock("sdk", sdk_build_identity(backend_spec)):
        return _ensure_sdk(
            config,
            workspace,
            source_cache=store.sdk_source_cache,
            arch=config.target.arch,
            glibc_floor=config.host_glibc_floor,
            goal=FULL_BUILD_GOAL,
            force=force,
            progress=progress,
        )


def _prepare_conan(config: SetupConfig, binding: Path) -> ConanRunConfig:
    assert config.conan is not None
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
            config.conan.build_profile,
            "--exist-ok",
        ],
        env=environment,
    )
    profile_text = run_process(
        [executable, "profile", "path", config.conan.build_profile],
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


def _smoke_namespace(
    config: SetupConfig,
    binding: Path,
    build_dir: Path,
    conan: ConanRunConfig | None,
) -> argparse.Namespace:
    return argparse.Namespace(
        binding=binding,
        build_profile=(str(conan.build_profile) if conan is not None else None),
        build_dir=build_dir,
        integration=config.integration,
        build_type=(config.conan.build_type if config.conan is not None else "Release"),
        conan=os.environ.get("CONAN", "conan"),
        cmake=os.environ.get("CMAKE", "cmake"),
        make=os.environ.get("MAKE", "make"),
        conan_home=(conan.home if conan is not None else None),
        runner=config.runner,
        jobs=config.jobs,
    )


def _validate_prepared_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    state: Path,
) -> PreparedSetupInputs:
    if prepared.config_sha256 != config.selection_sha256:
        raise ConfigurationError(
            "prepared setup state does not match setup.json; "
            "rerun linux-toolchain setup --force"
        )
    if prepared.integration != config.integration:
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
    expected_smoke = state / f"smoke-{config.integration}" / "result.json"
    if prepared.smoke_result is not None and prepared.smoke_result != expected_smoke:
        raise ConfigurationError(
            "prepared setup smoke result does not match its state directory"
        )
    try:
        managed_workspace = prepared.managed_workspace.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            "prepared setup managed workspace cannot be resolved"
        ) from error
    if managed_workspace != prepared.managed_workspace:
        raise ConfigurationError(
            "prepared setup managed workspace is not a canonical path"
        )
    for field in ("compiler_kit", "runtime"):
        path = getattr(prepared, field)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ConfigurationError(
                f"prepared setup {field} cannot be resolved"
            ) from error
        if (
            resolved != path
            or resolved == managed_workspace
            or managed_workspace not in resolved.parents
        ):
            raise ConfigurationError(
                f"prepared setup {field} is outside its managed workspace"
            )

    if not _regular_file(prepared.lock):
        raise ConfigurationError(f"prepared managed lock is missing: {prepared.lock}")
    prepared_lock = ManagedLock.load(prepared.lock)
    expected_lock = resolve_lock(config.managed_spec())
    if prepared_lock.sha256 != expected_lock.sha256:
        raise ConfigurationError(
            "prepared managed lock does not match the setup configuration"
        )
    if prepared.variant not in {variant.id for variant in prepared_lock.variants}:
        raise ConfigurationError(
            "prepared managed variant does not exist in its managed lock"
        )

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
    if prepared.sdk_workspace != expected_sdk_workspace:
        raise ConfigurationError(
            "prepared setup SDK workspace does not match its producer store identity"
        )
    if prepared.managed_workspace != expected_managed_workspace:
        raise ConfigurationError(
            "prepared setup managed workspace does not match its producer store identity"
        )
    for path in (
        prepared.binding,
        prepared.sdk_workspace,
        prepared.sdk_workspace / "sdk",
        prepared.managed_workspace,
        prepared.compiler_kit,
        prepared.runtime,
    ):
        if not _directory(path):
            raise ConfigurationError(f"prepared setup directory is missing: {path}")
    if prepared.smoke_result is not None and not _regular_file(prepared.smoke_result):
        raise ConfigurationError(
            f"prepared smoke result is missing: {prepared.smoke_result}"
        )
    if prepared.smoke_result is not None:
        if config.integration == "conan" and prepared.conan is None:
            raise ConfigurationError("prepared setup is missing Conan run state")
        try:
            smoke_result = load_smoke_result(prepared.smoke_result)
        except SmokeFailure as error:
            raise ConfigurationError(
                f"prepared smoke result is invalid: {error}"
            ) from error
        namespace = _smoke_namespace(
            config,
            prepared.binding,
            prepared.smoke_result.parent,
            prepared.conan,
        )
        expected_values = {
            "binding": str(prepared.binding),
            "integration": config.integration,
            "build_type": namespace.build_type,
        }
        for field, expected in expected_values.items():
            if smoke_result[field] != expected:
                raise ConfigurationError(
                    f"prepared smoke result {field} does not match setup state"
                )
        glibc = smoke_result["glibc"]
        assert isinstance(glibc, dict)
        if glibc["policy_floor"] != config.target.glibc_floor:
            raise ConfigurationError(
                "prepared smoke result glibc policy floor does not match setup config"
            )
        expected_evidence = list(_EVIDENCE)
        if smoke_result["evidence"] != expected_evidence:
            raise ConfigurationError(
                "prepared smoke result evidence does not match the smoke inputs"
            )
        for filename in expected_evidence:
            if not _regular_file(prepared.smoke_result.parent / filename):
                raise ConfigurationError(
                    "prepared smoke evidence is missing: "
                    f"{prepared.smoke_result.parent / filename}"
                )
        artifact_directory = prepared.smoke_result.parent / "cmake" / "artifacts"
        expected_artifacts = [
            str(artifact_directory / "linux_toolchain_smoke"),
            str(artifact_directory / "liblinux_toolchain_smoke.so"),
        ]
        if smoke_result["artifacts"] != expected_artifacts:
            raise ConfigurationError(
                "prepared smoke result artifacts do not match the smoke build"
            )
        for artifact in expected_artifacts:
            if not _regular_file(Path(artifact)):
                raise ConfigurationError(
                    f"prepared smoke artifact is missing: {artifact}"
                )
        if config.integration == "conan":
            assert prepared.conan is not None
            if smoke_result["conan_home"] != str(prepared.conan.home):
                raise ConfigurationError(
                    "prepared smoke result Conan home does not match setup state"
                )
            if smoke_result["build_profile"] != str(prepared.conan.build_profile):
                raise ConfigurationError(
                    "prepared smoke result build profile does not match setup state"
                )
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
    }[config.integration]
    if not _regular_file(selected):
        raise ConfigurationError(f"prepared integration file is missing: {selected}")
    if config.integration == "conan":
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
    return PreparedSetupInputs(
        lock=prepared_lock,
        sdk=prepared.sdk_workspace / "sdk",
        compiler_kit=prepared.compiler_kit,
        runtime=prepared.runtime,
        binding=prepared.binding,
    )


@contextmanager
def lock_prepared_setup_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    *,
    state: Path,
) -> Iterator[PreparedSetupInputs]:
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
    variant = next(item for item in inputs.lock.variants if item.id == prepared.variant)

    def managed_identity(artifact: str) -> dict[str, object]:
        return {
            "workspace": prepared.managed_workspace.name,
            "artifact": artifact,
        }

    return (
        ("sdk", sdk_build_identity(_sdk_spec(config))),
        (
            "managed-artifact",
            managed_identity(variant.compiler_kit_id),
        ),
        (
            "managed-artifact",
            managed_identity(variant.runtime_id),
        ),
    )


@contextmanager
def _lock_prepared_producer_inputs(
    config: SetupConfig,
    prepared: PreparedSetup,
    *,
    state: Path,
) -> Iterator[PreparedSetupInputs]:
    """Acquire producer read leases, then revalidate before consuming inputs."""

    initial = _validate_prepared_inputs(config, prepared, state)
    store = ProducerStore.load(prepared.sdk_workspace.parent.parent)
    identities = _producer_lease_identities(config, prepared, initial)
    with store.lock_many(identities, shared=True):
        stable = _validate_prepared_inputs(config, prepared, state)
        compiler_artifact, runtime_publication = _validate_leased_producer_artifacts(
            config,
            prepared,
            stable,
        )
        yield replace(
            stable,
            compiler_artifact=compiler_artifact,
            runtime_publication=runtime_publication,
        )


def _validate_leased_producer_artifacts(
    config: SetupConfig,
    prepared: PreparedSetup,
    inputs: PreparedSetupInputs,
) -> tuple[ManagedCompilerArtifact, ManagedRuntimePublication]:
    target_spec = _sdk_spec(config)
    backend_spec = managed_compiler_backend_spec(
        config.target.arch,
        config.host_glibc_floor,
    )
    target_goal = FULL_BUILD_GOAL if target_spec == backend_spec else SDK_BUILD_GOAL
    if not _sdk_is_ready(prepared.sdk_workspace, target_spec, target_goal):
        raise ConfigurationError("prepared target SDK is no longer ready")
    variant = next(item for item in inputs.lock.variants if item.id == prepared.variant)
    compiler_artifact = load_managed_compiler_artifact(
        inputs.lock,
        variant.compiler_kit_id,
        inputs.compiler_kit,
    )
    runtime_publication = load_managed_runtime_publication(
        inputs.lock,
        variant.runtime_id,
        inputs.runtime,
    )
    return compiler_artifact, runtime_publication


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
            "prepared setup has not passed its consumer smoke test; "
            "rerun linux-toolchain setup"
        )
    state = _state_root(Path(config).expanduser().resolve(), state_directory)
    with lock_prepared_setup_inputs(
        setup_config,
        prepared,
        state=state,
    ) as prepared_inputs:
        compiler_artifact, runtime_publication = prepared_inputs.validated_artifacts()
        return create_bundle(
            sdk=prepared_inputs.sdk,
            compiler_kit=prepared_inputs.compiler_kit,
            runtime=prepared_inputs.runtime,
            lock=prepared_inputs.lock,
            variant=prepared.variant,
            output=output,
            bundle_id=bundle_id,
            integrations=setup_config.selected_integrations,
            conan=setup_config.conan_settings(),
            binding_template=prepared_inputs.binding,
            force=force,
            progress=progress,
            archive_progress=archive_progress,
            _compiler_artifact=compiler_artifact,
            _runtime_publication=runtime_publication,
        )


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
    existing_prepared: PreparedSetup | None = None
    if prepared_path.exists() or prepared_path.is_symlink():
        if not _regular_file(prepared_path):
            raise ConfigurationError(
                f"invalid prepared setup state file: {prepared_path}"
            )
        try:
            existing_prepared = PreparedSetup.load(prepared_path)
        except ConfigurationError:
            pass
    if existing_prepared is not None:
        recorded_store = existing_prepared.sdk_workspace.parent.parent
        if store_directory is not None:
            requested_store = store_directory.expanduser().resolve()
            if requested_store != recorded_store:
                raise ConfigurationError(
                    "setup state belongs to a different producer store; "
                    f"reuse {recorded_store} or use a new --work-dir"
                )
        if force:
            write_json_atomic(
                prepared_path,
                replace(existing_prepared, smoke_result=None).to_dict(),
            )
    store = ProducerStore.prepare(
        state / "producer" if store_directory is None else store_directory
    )
    if prepared_path.exists() or prepared_path.is_symlink():
        if not force:
            try:
                prepared = (
                    existing_prepared
                    if existing_prepared is not None
                    else PreparedSetup.load(prepared_path)
                )
                _validate_prepared_inputs(config, prepared, state)
            except ConfigurationError as error:
                raise ConfigurationError(
                    "prepared setup state is invalid; rerun setup with --force "
                    f"or use a new --work-dir: {error}"
                ) from error
            if prepared.smoke_result is not None:
                _emit(progress, "setup: using validated prepared state ... DONE")
                return prepared
            _emit(progress, "setup: completing unqualified prepared state")

    _diagnose(config)
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
    with store.lock("sdk", sdk_build_identity(target_sdk_spec)):
        sdk_workspace = _ensure_sdk(
            config,
            sdk_workspace,
            source_cache=store.sdk_source_cache,
            goal=target_goal,
            force=force,
            progress=progress,
        )
    compiler_backend_workspace = _ensure_compiler_backend(
        config,
        store,
        target_sdk_spec,
        sdk_workspace,
        force=force,
        progress=progress,
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
        ("sdk", sdk_build_identity(target_sdk_spec)),
        ("sdk", sdk_build_identity(compiler_backend_spec)),
    )
    artifact_writers = tuple(
        (
            "managed-artifact",
            {
                "workspace": managed_workspace.name,
                "artifact": artifact_id,
            },
        )
        for artifact_id in {variant.compiler_kit_id, variant.runtime_id}
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
                source_cache=store.source_cache,
                # The binding belongs to this selection-specific state and may be
                # regenerated from the validated immutable artifacts.
                force=True,
                repair=force,
                progress=lambda message: _emit(progress, f"managed: {message}"),
                source_progress=source_progress,
            )
    conan = _prepare_conan(config, binding) if config.conan is not None else None
    unqualified = PreparedSetup(
        config_sha256=config.selection_sha256,
        binding=result.binding_manifest.parent.resolve(),
        lock=lock_path.resolve(),
        variant=variant.id,
        sdk_workspace=sdk_workspace.resolve(),
        managed_workspace=managed_workspace.resolve(),
        compiler_kit=result.compiler_kit.resolve(),
        runtime=result.runtime.resolve(),
        integration=config.integration,
        smoke_result=None,
        conan=conan,
    )
    with _lock_prepared_producer_inputs(config, unqualified, state=state):
        _emit(progress, f"setup: validating {config.integration} integration")
        smoke_directory = state / f"smoke-{config.integration}"
        # The smoke command reports its result path on stdout. ``setup`` owns
        # stdout, so forward that progress to stderr with the other build logs.
        with redirect_stdout(sys.stderr):
            run_smoke(_smoke_namespace(config, binding, smoke_directory, conan))
        smoke_result = smoke_directory / "result.json"
        if not smoke_result.is_file():
            raise ConfigurationError(
                f"smoke validation did not produce its result: {smoke_result}"
            )
        prepared = replace(unqualified, smoke_result=smoke_result.resolve())
        _validate_prepared_inputs(config, prepared, state)
        write_json_atomic(prepared_path, prepared.to_dict())
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
