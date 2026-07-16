from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence, cast

from linux_toolchain.build_tools import (
    DEFAULT_CMAKE_VERSION,
    BuildToolsArtifact,
    load_build_tools,
)
from linux_toolchain.bundle_installer import (
    CONAN_DEFAULT_BUILD_PROFILE,
    CONAN_DEFAULT_PROFILE,
    DEFAULT_LAUNCHER_NAME,
    PREFIX_TOKEN,
    SHELL_INIT,
    SHELL_INIT_RELATIVE_PATH,
    LauncherExecutionLayout,
    default_conan_home_name,
    default_runtime_state_file,
    relocate_binding_links,
    render_installer_header,
    render_launcher,
    template_binding,
    write_payload_archive,
)
from linux_toolchain.compiler.binding import (
    BINDING_FORMAT,
    BINDING_SCHEMA,
    sdk_library_dirs,
)
from linux_toolchain.compiler.managed import validate_current_host
from linux_toolchain.compiler.managed_binding import (
    create_managed_binding,
    create_managed_binding_from_artifacts,
)
from linux_toolchain.conan.settings import SETTINGS_USER_YAML
from linux_toolchain.elf import load_policy
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.integrations import (
    DEFAULT_INTEGRATIONS,
    SUPPORTED_INTEGRATIONS,
    ConanSettings,
    IntegrationName,
)
from linux_toolchain.managed import ManagedLock
from linux_toolchain.managed.contracts import MANAGED_RUNTIME_DIRECTORY_NAMES
from linux_toolchain.managed.lockfile import VariantLock
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimeSetPublication,
    load_managed_compiler_artifact,
    load_managed_runtime_set_publication,
)
from linux_toolchain.publication import replace_directory
from linux_toolchain.schema import canonical_json_bytes, object_value, read_json_object
from linux_toolchain.versions import AbiVersion

BUNDLE_SCHEMA = "linux-toolchain-bundle"
BUNDLE_FORMAT = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
_INSTALL_PREFIX = re.compile(r"/[A-Za-z0-9/._+@=-]+")
ProgressCallback = Callable[[str], None]
ArchiveProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class ValidatedPayloadInputs:
    sdk: Path
    lock: ManagedLock
    variant: VariantLock
    host: Mapping[str, str]
    bundle_id: str
    integrations: tuple[IntegrationName, ...]
    conan: ConanSettings | None
    build_tools_artifact: BuildToolsArtifact
    compiler_artifact: ManagedCompilerArtifact
    runtime_set_publication: ManagedRuntimeSetPublication

    @property
    def build_tools(self) -> Path:
        return self.build_tools_artifact.root

    @property
    def compiler_kit(self) -> Path:
        return self.compiler_artifact.root

    @property
    def runtime(self) -> Path:
        return self.runtime_set_publication.root

    @classmethod
    def from_artifacts(
        cls,
        *,
        sdk: Path,
        lock: ManagedLock,
        variant: str,
        bundle_id: str | None,
        integrations: Sequence[IntegrationName],
        conan: ConanSettings | None,
        build_tools_artifact: BuildToolsArtifact,
        compiler_artifact: ManagedCompilerArtifact,
        runtime_set_publication: ManagedRuntimeSetPublication,
    ) -> "ValidatedPayloadInputs":
        selected_integrations = tuple(integrations)
        if ("conan" in selected_integrations) != (conan is not None):
            raise ConfigurationError(
                "Conan settings are required exactly when Conan integration is selected"
            )
        selected = lock.variant(variant)
        if (
            compiler_artifact.selection.artifact_id != selected.compiler_kit_id
            or runtime_set_publication.variant != selected
        ):
            raise ConfigurationError(
                "validated managed artifacts do not match the variant"
            )
        assert compiler_artifact.selection.host is not None
        host = compiler_artifact.selection.host
        if build_tools_artifact.spec.arch != host.arch or AbiVersion.parse(
            build_tools_artifact.spec.glibc_floor
        ) != AbiVersion.parse(host.glibc_floor):
            raise ConfigurationError(
                "build tools do not match the managed compiler host platform"
            )
        return cls(
            sdk=sdk.expanduser().resolve(),
            lock=lock,
            variant=selected,
            host=host.to_dict(),
            bundle_id=_identifier(
                bundle_id or f"{lock.name}-{selected.id}",
                "bundle id",
            ),
            integrations=selected_integrations,
            conan=conan,
            build_tools_artifact=build_tools_artifact,
            compiler_artifact=compiler_artifact,
            runtime_set_publication=runtime_set_publication,
        )


@dataclass(frozen=True)
class _InstalledBundleManifest:
    document: dict[str, object]
    bundle_id: str
    variant: str
    integrations: tuple[IntegrationName, ...]
    conan: ConanSettings | None


def _runtime_records(inputs: ValidatedPayloadInputs) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    runtime_locks = {runtime.id: runtime for runtime in inputs.lock.runtimes}
    for runtime in inputs.variant.runtimes:
        selected = runtime_locks[runtime.runtime_id]
        records.append(
            {
                "kind": runtime.kind,
                "provider": selected.provider_family,
                "version": selected.provider_version,
            }
        )
    return records


def _default_installation_name(inputs: ValidatedPayloadInputs) -> str:
    compiler_major = inputs.variant.version.split(".", 1)[0]
    parts = [
        f"{inputs.variant.family}{compiler_major}",
        f"glibc{inputs.variant.target.glibc_floor.replace('.', '')}",
        inputs.variant.target.arch,
    ]
    if inputs.variant.family == "clang":
        runtimes = {runtime["kind"]: runtime for runtime in _runtime_records(inputs)}
        libstdcxx = runtimes["libstdc++"]
        runtime_major = libstdcxx["version"].split(".", 1)[0]
        parts.append(f"{libstdcxx['provider']}{runtime_major}")
    cmake_version = inputs.build_tools_artifact.spec.cmake_version
    if cmake_version != DEFAULT_CMAKE_VERSION:
        parts.append(f"cmake{cmake_version.replace('.', '')}")
    return _identifier("-".join(parts), "default installation name")


def _bundle_manifest(inputs: ValidatedPayloadInputs) -> dict[str, object]:
    conan = (
        None
        if inputs.conan is None
        else {
            "cppstd": inputs.conan.cppstd,
            "libcxx": inputs.conan.libcxx,
            "build_type": inputs.conan.build_type,
        }
    )
    return {
        "schema": BUNDLE_SCHEMA,
        "format": BUNDLE_FORMAT,
        "id": inputs.bundle_id,
        "variant": inputs.variant.id,
        "compiler": {
            "family": inputs.variant.family,
            "version": inputs.variant.version,
        },
        "target": {
            "arch": inputs.variant.target.arch,
            "glibc_floor": inputs.variant.target.glibc_floor,
        },
        "host": dict(inputs.host),
        "build_tools": {
            "selection": inputs.build_tools_artifact.spec.to_dict(),
            "tools": inputs.build_tools_artifact.tools,
        },
        "cxx_runtimes": {
            "default": inputs.variant.default_cxx_runtime,
            "available": _runtime_records(inputs),
        },
        "binding": {
            "integrations": list(inputs.integrations),
            "conan": conan,
        },
    }


def _bundle_info(
    inputs: ValidatedPayloadInputs,
    *,
    binding: Path,
    sdk: Path,
) -> str:
    target_triplet = inputs.compiler_artifact.target
    if not isinstance(target_triplet, str) or not target_triplet:
        raise ConfigurationError("bundle compiler target is invalid")
    runtime_records = _runtime_records(inputs)
    runtime_by_kind = {runtime["kind"]: runtime for runtime in runtime_records}
    default_runtime = runtime_by_kind[inputs.variant.default_cxx_runtime]
    values = [
        ("bundle.id", inputs.bundle_id),
        ("bundle.variant", inputs.variant.id),
        ("installation.prefix", str(binding.parent)),
        ("compiler.family", inputs.variant.family),
        ("compiler.version", inputs.variant.version),
        ("compiler.cc", str(binding / "bin" / "cc")),
        ("compiler.cxx", str(binding / "bin" / "c++")),
        ("target.triplet", target_triplet),
        ("target.arch", inputs.variant.target.arch),
        ("target.sysroot", str(sdk / "sysroot")),
        ("libc.family", "glibc"),
        ("libc.version", inputs.variant.target.glibc_floor),
        ("build_tools.arch", inputs.build_tools_artifact.spec.arch),
        (
            "build_tools.glibc_floor",
            inputs.build_tools_artifact.spec.glibc_floor,
        ),
        ("cxx_runtime.kind", inputs.variant.default_cxx_runtime),
        ("cxx_runtime.provider", default_runtime["provider"]),
        ("cxx_runtime.version", default_runtime["version"]),
        (
            "cxx_runtime.available",
            ",".join(runtime["kind"] for runtime in runtime_records),
        ),
        ("integrations", ",".join(inputs.integrations)),
        ("conan.enabled", "true" if inputs.conan is not None else "false"),
    ]
    tools = binding.parent / "tools"
    for name, record in inputs.build_tools_artifact.tools.items():
        values.extend(
            (
                (f"tools.{name}.version", str(record["version"])),
                (f"tools.{name}.path", str(tools / str(record["path"]))),
                (f"tools.{name}.linkage", str(record["linkage"])),
                (
                    f"tools.{name}.enabled_by_default",
                    "true" if record["enabled_by_default"] else "false",
                ),
            )
        )
    if "cmake" in inputs.integrations:
        values.append(("cmake.toolchain", str(binding / "cmake" / "toolchain.cmake")))
    return "".join(f"{key}={value}\n" for key, value in values)


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _identifier(value: str, context: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ConfigurationError(f"{context} has invalid characters")
    return value


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _write_installer(
    path: Path,
    payload: Path,
    *,
    trees: Sequence[tuple[Path, str]],
    header: Callable[[int], bytes],
    progress: ArchiveProgressCallback | None,
    force: bool,
) -> None:
    if path.is_symlink() or (path.exists() and not _regular_file(path)):
        raise ConfigurationError(f"output is not a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.tmp-",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
        temporary = Path(temporary_name)
        write_payload_archive(
            payload,
            temporary,
            trees=trees,
            progress=progress,
            header=header,
        )
        temporary.chmod(0o755)
        if force:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
    except OSError as error:
        raise ConfigurationError(f"cannot write {path}: {error}") from error
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _copy_tree(source: Path, destination: Path) -> None:
    if not _directory(source):
        raise ConfigurationError(f"artifact is not a directory: {source}")
    shutil.copytree(source, destination, symlinks=True)


def _remove_conan_machine_state(binding: Path) -> None:
    for name in ("conan-home", "build-profile"):
        path = binding / "conan" / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ConfigurationError(
                f"binding Conan installation state is not a regular file: {path}"
            )
        path.unlink(missing_ok=True)


def _write_conan_configuration(path: Path, content: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError(f"Conan configuration is not a regular file: {path}")
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ConfigurationError(
                f"refusing to replace different Conan configuration: {path}"
            )
        return
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(content)
        temporary = Path(temporary_name)
        temporary.chmod(0o644)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.read_text(encoding="utf-8") != content
            ):
                raise ConfigurationError(
                    f"refusing to replace different Conan configuration: {path}"
                ) from None
    except OSError as error:
        raise ConfigurationError(
            f"cannot write Conan configuration {path}: {error}"
        ) from error
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _conan_runtime_selection(
    *,
    bundle_id: str,
    cxx_runtimes: Sequence[str],
    default_cxx_runtime: str,
) -> tuple[str, str]:
    if not {"libstdc++", "libc++"}.issubset(cxx_runtimes):
        return default_cxx_runtime, "default"
    profiles = {
        "libc++": "lxtc-libcxx",
        "libstdc++": "lxtc-libstdcxx",
    }
    selection = default_runtime_state_file(bundle_id)
    if selection is None:
        return default_cxx_runtime, profiles[default_cxx_runtime]
    if selection.is_symlink() or (selection.exists() and not selection.is_file()):
        raise ConfigurationError(
            f"C++ runtime selection is not a regular file: {selection}"
        )
    if not selection.exists():
        return default_cxx_runtime, profiles[default_cxx_runtime]
    try:
        first_line, separator, _ = selection.read_text(encoding="utf-8").partition("\n")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(
            f"cannot read C++ runtime selection: {selection}"
        ) from error
    if not separator:
        raise ConfigurationError(f"cannot read C++ runtime selection: {selection}")
    profile = profiles.get(first_line)
    if profile is None:
        raise ConfigurationError(f"unsupported persisted C++ runtime: {first_line}")
    return first_line, profile


def _write_conan_info(
    home: Path,
    binding: Path,
    *,
    runtime: str,
    host_profile: str,
) -> None:
    source = binding / "env" / "toolchain.info"
    destination = home / "lxtc.info"
    if not _regular_file(source):
        raise ConfigurationError(
            f"installed toolchain info is not a regular file: {source}"
        )
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ConfigurationError(
            f"Conan toolchain info is not a regular file: {destination}"
        )
    temporary_name: str | None = None
    try:
        content = source.read_text(encoding="utf-8")
        content += f"cxx_runtime.selected={runtime}\n"
        content += (
            f"conan.home={home}\n"
            f"conan.host_profile={host_profile}\n"
            "conan.build_profile=lxtc-build\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".lxtc-info.",
            dir=home,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(content)
        temporary = Path(temporary_name)
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(
            f"cannot write Conan toolchain info {destination}: {error}"
        ) from error
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _prepare_conan_home(
    home: Path,
    binding: Path,
    *,
    bundle_id: str,
    cxx_runtimes: Sequence[str],
    default_cxx_runtime: str,
) -> None:
    if home in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"unsafe Conan home: {home}")
    if home.is_symlink() or (home.exists() and not home.is_dir()):
        raise ConfigurationError(f"Conan home is not a directory: {home}")
    profiles = home / "profiles"
    if profiles.is_symlink() or (profiles.exists() and not profiles.is_dir()):
        raise ConfigurationError(f"Conan profiles path is not a directory: {profiles}")
    try:
        profiles.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigurationError(f"cannot create Conan home {home}: {error}") from error
    conan = binding / "conan"
    configuration = [
        (conan / "settings_user.yml", home / "settings_user.yml"),
        (conan / "default.profile", profiles / "default"),
        (conan / "lxtc-build.profile", profiles / "lxtc-build"),
    ]
    libcxx_profile = conan / "lxtc-libcxx.profile"
    if libcxx_profile.is_file():
        configuration.extend(
            (
                (libcxx_profile, profiles / "lxtc-libcxx"),
                (
                    conan / "lxtc-libstdcxx.profile",
                    profiles / "lxtc-libstdcxx",
                ),
            )
        )
    for source, destination in configuration:
        _write_conan_configuration(
            destination,
            source.read_text(encoding="utf-8"),
        )
    runtime, host_profile = _conan_runtime_selection(
        bundle_id=bundle_id,
        cxx_runtimes=cxx_runtimes,
        default_cxx_runtime=default_cxx_runtime,
    )
    _write_conan_info(
        home,
        binding,
        runtime=runtime,
        host_profile=host_profile,
    )


def _resolve_conan_paths(
    inputs: ValidatedPayloadInputs,
    *,
    prefix: Path,
    conan_home: Path | None,
    conan_build_profile: Path | None,
) -> tuple[Path | None, Path | None]:
    if inputs.conan is None:
        if conan_home is not None or conan_build_profile is not None:
            raise ConfigurationError(
                "Conan installation paths require Conan integration"
            )
        return None, None
    raw_home = (
        Path.home() / default_conan_home_name(inputs.bundle_id)
        if conan_home is None
        else conan_home.expanduser()
    )
    if raw_home.is_symlink():
        raise ConfigurationError(f"Conan home cannot be a symlink: {raw_home}")
    home = raw_home.resolve()
    if home in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"unsafe Conan home: {home}")
    if home == prefix or prefix in home.parents or home in prefix.parents:
        raise ConfigurationError(
            f"Conan home and installation prefix cannot overlap: {home} and {prefix}"
        )
    if conan_build_profile is None:
        if inputs.host["arch"] != inputs.variant.target.arch:
            raise ConfigurationError(
                "default lxtc Conan build profile requires a native managed target; "
                "supply an explicit Conan build profile for a cross target"
            )
        required_floor = max(
            AbiVersion.parse(inputs.host["glibc_floor"]),
            AbiVersion.parse(inputs.variant.target.glibc_floor),
        )
        validate_current_host(
            {
                "os": inputs.host["os"],
                "arch": inputs.host["arch"],
                "glibc_floor": str(required_floor),
            }
        )
        build_profile = prefix / "binding" / "conan" / "build.profile"
    else:
        build_profile = conan_build_profile.expanduser().resolve()
        if build_profile == home / "profiles" / "lxtc-build":
            raise ConfigurationError(
                "Conan build profile cannot select the generated lxtc-build "
                "selector itself"
            )
    return home, build_profile


def _load_payload_inputs(
    *,
    sdk: Path,
    build_tools: Path,
    compiler_kit: Path,
    runtime: Path,
    lock: ManagedLock | Path,
    variant: str,
    bundle_id: str | None,
    integrations: Sequence[IntegrationName],
    conan: ConanSettings | None,
) -> ValidatedPayloadInputs:
    managed_lock = lock if isinstance(lock, ManagedLock) else ManagedLock.load(lock)
    selected = managed_lock.variant(variant)
    tools = load_build_tools(build_tools)
    compiler = load_managed_compiler_artifact(
        managed_lock, selected.compiler_kit_id, compiler_kit
    )
    runtime_set_publication = load_managed_runtime_set_publication(
        managed_lock,
        selected.id,
        runtime,
    )
    return ValidatedPayloadInputs.from_artifacts(
        sdk=sdk,
        lock=managed_lock,
        variant=selected.id,
        bundle_id=bundle_id,
        integrations=integrations,
        conan=conan,
        build_tools_artifact=tools,
        compiler_artifact=compiler,
        runtime_set_publication=runtime_set_publication,
    )


def _runtime_library_dirs(
    inputs: ValidatedPayloadInputs,
    runtime: Path,
    kind: str,
) -> tuple[Path, ...]:
    publication = inputs.runtime_set_publication.publication(kind)
    raw_library_dirs = publication.manifest.locations.get("library_dirs")
    if not isinstance(raw_library_dirs, tuple) or not raw_library_dirs:
        raise ConfigurationError("managed runtime has no library directories")
    component = runtime / "runtimes" / MANAGED_RUNTIME_DIRECTORY_NAMES[kind]
    return tuple(component / str(relative) for relative in raw_library_dirs)


def _bundle_artifact_path(
    source: Path,
    destination: str,
    path: Path,
    context: str,
) -> str:
    try:
        relative = path.relative_to(source)
    except ValueError as error:
        raise ConfigurationError(f"{context} is outside its artifact root") from error
    return (Path(destination) / relative).as_posix()


def _launcher_execution_layout(
    inputs: ValidatedPayloadInputs,
    *,
    binding: Path,
    sdk: Path,
    runtime: Path,
) -> LauncherExecutionLayout:
    policy = load_policy(binding / "audit-policy.json")
    if len(policy.allowed_interpreters) != 1:
        raise ConfigurationError(
            "managed bundle requires exactly one target dynamic loader"
        )
    interpreter = policy.allowed_interpreters[0]
    sysroot = sdk / "sysroot"
    loader = sysroot / interpreter.lstrip("/")
    if not loader.is_file():
        raise ConfigurationError(f"SDK dynamic loader is missing: {loader}")
    runtime_dirs = {
        kind: tuple(
            _bundle_artifact_path(
                runtime,
                "artifacts/runtime",
                path,
                f"{kind} runtime library directory",
            )
            for path in _runtime_library_dirs(inputs, runtime, kind)
        )
        for kind in inputs.variant.cxx_runtimes
    }
    return LauncherExecutionLayout(
        target_arch=inputs.variant.target.arch,
        glibc_version=inputs.variant.target.glibc_floor,
        sdk_root=_bundle_artifact_path(sdk, "artifacts/sdk", sysroot, "SDK sysroot"),
        runtime_root="artifacts/runtime",
        loader=_bundle_artifact_path(
            sdk, "artifacts/sdk", loader, "SDK dynamic loader"
        ),
        interpreter=interpreter,
        sdk_library_dirs=tuple(
            _bundle_artifact_path(sdk, "artifacts/sdk", path, "SDK library directory")
            for path in sdk_library_dirs(sysroot)
        ),
        runtime_library_dirs=runtime_dirs,
    )


def _conan_build_environment(kind: str, library_dirs: Sequence[Path]) -> str:
    return (
        "[buildenv]\n"
        f"LINUX_TOOLCHAIN_CXX_RUNTIME={kind}\n"
        f"LD_LIBRARY_PATH=+(path){':'.join(str(path) for path in library_dirs)}\n"
    )


def _conan_build_profile(
    binding: Path,
    *,
    kind: str,
    libcxx: str,
    library_dirs: Sequence[Path],
) -> str:
    return (
        "# Conan build requirements use this bundle's managed native toolchain.\n"
        f"include({binding / 'conan' / 'host.profile'})\n\n"
        "[settings]\n"
        f"compiler.libcxx={libcxx}\n\n"
        f"{_conan_build_environment(kind, library_dirs)}"
    )


def _conan_runtime_kind(settings: ConanSettings) -> str:
    return "libc++" if settings.libcxx == "libc++" else "libstdc++"


def _conan_runtime_libcxx(settings: ConanSettings, kind: str) -> str:
    if kind == "libc++":
        return "libc++"
    if settings.libcxx in {"libstdc++", "libstdc++11"}:
        return settings.libcxx
    return "libstdc++11"


def _conan_default_build_profile(binding: Path, kind: str) -> str:
    filename = {
        "libc++": "build-libcxx.profile",
        "libstdc++": "build-libstdcxx.profile",
    }[kind]
    return (
        "# Default managed native Conan build context.\n"
        f"include({binding / 'conan' / filename})\n"
    )


def _conan_default_profile(
    settings: ConanSettings,
    library_dirs: Sequence[Path],
) -> str:
    return (
        CONAN_DEFAULT_PROFILE
        + "\n"
        + _conan_build_environment(
            _conan_runtime_kind(settings),
            library_dirs,
        )
    )


def _conan_runtime_profile(
    kind: str,
    libcxx: str,
    library_dirs: Sequence[Path],
) -> str:
    return (
        f"# Conan target context for the {kind} runtime.\n"
        '{% set binding = os.getenv("LINUX_TOOLCHAIN_BINDING") %}\n'
        "include({{ binding }}/conan/host.profile)\n\n"
        "[settings]\n"
        f"compiler.libcxx={libcxx}\n\n"
        f"{_conan_build_environment(kind, library_dirs)}"
    )


def _write_payload_metadata(
    payload: Path,
    inputs: ValidatedPayloadInputs,
    *,
    sdk: Path,
    compiler_kit: Path,
    runtime: Path,
    artifact_paths: Mapping[Path, str] | None = None,
    binding_template: Path | None = None,
    progress: ProgressCallback | None,
) -> None:
    artifacts = payload / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    lock_path = artifacts / "managed.lock.json"
    lock_path.write_bytes(canonical_json_bytes(inputs.lock.to_dict()))

    _emit(progress, "bundle: creating binding template")
    binding = payload / "binding"
    template_paths = dict(artifact_paths or {})
    if binding_template is None:
        if (
            compiler_kit.resolve() == inputs.compiler_artifact.root
            and runtime.resolve() == inputs.runtime_set_publication.root
        ):
            create_managed_binding_from_artifacts(
                sdk,
                binding,
                lock=inputs.lock,
                variant=inputs.variant.id,
                compiler_artifact=inputs.compiler_artifact,
                runtime_set_publication=inputs.runtime_set_publication,
                integrations=inputs.integrations,
                conan=inputs.conan,
            )
        else:
            create_managed_binding(
                sdk,
                binding,
                compiler_kit,
                lock=inputs.lock,
                variant=inputs.variant.id,
                runtime=runtime,
                integrations=inputs.integrations,
                conan=inputs.conan,
            )
    else:
        _copy_tree(binding_template, binding)
        template_paths[binding_template] = "binding"
    if inputs.conan is not None:
        _remove_conan_machine_state(binding)
    relocate_binding_links(
        payload,
        binding,
        source_binding=binding if binding_template is None else binding_template,
        artifact_paths=artifact_paths or {},
    )
    if inputs.conan is not None:
        runtime_switch = {"libstdc++", "libc++"}.issubset(inputs.variant.cxx_runtimes)
        runtime_library_dirs = {
            kind: _runtime_library_dirs(inputs, runtime, kind)
            for kind in inputs.variant.cxx_runtimes
        }
        (binding / "conan" / "settings_user.yml").write_text(
            SETTINGS_USER_YAML,
            encoding="utf-8",
        )
        (binding / "conan" / "default.profile").write_text(
            (
                CONAN_DEFAULT_PROFILE
                if runtime_switch
                else _conan_default_profile(
                    inputs.conan,
                    runtime_library_dirs[_conan_runtime_kind(inputs.conan)],
                )
            ),
            encoding="utf-8",
        )
        (binding / "conan" / "lxtc-build.profile").write_text(
            CONAN_DEFAULT_BUILD_PROFILE,
            encoding="utf-8",
        )
        if runtime_switch:
            libstdcxx = _conan_runtime_libcxx(inputs.conan, "libstdc++")
            (binding / "conan" / "lxtc-libcxx.profile").write_text(
                _conan_runtime_profile(
                    "libc++",
                    "libc++",
                    runtime_library_dirs["libc++"],
                ),
                encoding="utf-8",
            )
            (binding / "conan" / "lxtc-libstdcxx.profile").write_text(
                _conan_runtime_profile(
                    "libstdc++",
                    libstdcxx,
                    runtime_library_dirs["libstdc++"],
                ),
                encoding="utf-8",
            )
            for kind, libcxx in (
                ("libc++", "libc++"),
                ("libstdc++", libstdcxx),
            ):
                filename = (
                    "build-libcxx.profile"
                    if kind == "libc++"
                    else "build-libstdcxx.profile"
                )
                (binding / "conan" / filename).write_text(
                    _conan_build_profile(
                        binding,
                        kind=kind,
                        libcxx=libcxx,
                        library_dirs=runtime_library_dirs[kind],
                    ),
                    encoding="utf-8",
                )
            (binding / "conan" / "build.profile").write_text(
                _conan_default_build_profile(
                    binding,
                    inputs.variant.default_cxx_runtime,
                ),
                encoding="utf-8",
            )
        else:
            kind = _conan_runtime_kind(inputs.conan)
            (binding / "conan" / "build.profile").write_text(
                _conan_build_profile(
                    binding,
                    kind=kind,
                    libcxx=_conan_runtime_libcxx(inputs.conan, kind),
                    library_dirs=runtime_library_dirs[kind],
                ),
                encoding="utf-8",
            )
    (binding / "env" / "toolchain.info").write_text(
        _bundle_info(inputs, binding=binding, sdk=sdk),
        encoding="utf-8",
    )
    shell_init = binding / SHELL_INIT_RELATIVE_PATH
    shell_init.parent.mkdir(parents=True, exist_ok=True)
    shell_init.write_text(SHELL_INIT, encoding="utf-8")

    launcher = payload / "bin" / DEFAULT_LAUNCHER_NAME
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        render_launcher(
            bundle_id=inputs.bundle_id,
            conan=inputs.conan is not None,
            execution=_launcher_execution_layout(
                inputs,
                binding=binding,
                sdk=sdk,
                runtime=runtime,
            ),
            cxx_runtimes=inputs.variant.cxx_runtimes,
            default_cxx_runtime=inputs.variant.default_cxx_runtime,
        ),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    template_files = [
        *template_binding(payload, binding, artifact_paths=template_paths),
        launcher.relative_to(payload).as_posix(),
    ]
    (payload / "template-files").write_text(
        "".join(f"{path}\n" for path in sorted(template_files)),
        encoding="utf-8",
    )

    (payload / "manifest.json").write_bytes(
        canonical_json_bytes(_bundle_manifest(inputs))
    )


def _write_payload(
    payload: Path,
    inputs: ValidatedPayloadInputs,
    *,
    binding_template: Path | None = None,
    progress: ProgressCallback | None,
) -> None:
    artifacts = payload / "artifacts"
    artifacts.mkdir(parents=True)

    _emit(progress, "bundle: copying portable artifacts")
    _copy_tree(inputs.sdk, artifacts / "sdk")
    _copy_tree(inputs.build_tools, payload / "tools")
    _copy_tree(inputs.compiler_kit, artifacts / "compiler-kit")
    _copy_tree(inputs.runtime, artifacts / "runtime")
    _write_payload_metadata(
        payload,
        inputs,
        sdk=artifacts / "sdk",
        compiler_kit=artifacts / "compiler-kit",
        runtime=artifacts / "runtime",
        artifact_paths=(
            {
                artifacts / "sdk": "artifacts/sdk",
                artifacts / "compiler-kit": "artifacts/compiler-kit",
                artifacts / "runtime": "artifacts/runtime",
            }
            if binding_template is None
            else {
                inputs.sdk: "artifacts/sdk",
                inputs.compiler_kit: "artifacts/compiler-kit",
                inputs.runtime: "artifacts/runtime",
            }
        ),
        binding_template=binding_template,
        progress=progress,
    )


def _read_installed_bundle_manifest(prefix: Path) -> _InstalledBundleManifest:
    value = object_value(
        read_json_object(prefix / "manifest.json", "installed toolchain manifest"),
        {
            "schema",
            "format",
            "id",
            "variant",
            "compiler",
            "target",
            "host",
            "build_tools",
            "cxx_runtimes",
            "binding",
        },
        "installed toolchain manifest",
    )
    manifest_format = value["format"]
    if (
        value["schema"] != BUNDLE_SCHEMA
        or not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != BUNDLE_FORMAT
    ):
        raise ConfigurationError("installed toolchain manifest is unsupported")

    bundle_id_value = value["id"]
    if not isinstance(bundle_id_value, str):
        raise ConfigurationError("installed toolchain id is invalid")
    bundle_id = _identifier(bundle_id_value, "installed toolchain id")
    variant = value["variant"]
    if not isinstance(variant, str) or _IDENTIFIER.fullmatch(variant) is None:
        raise ConfigurationError("installed toolchain variant is invalid")

    binding = object_value(
        value["binding"],
        {"integrations", "conan"},
        "installed toolchain binding record",
    )
    raw_integrations = binding["integrations"]
    if (
        not isinstance(raw_integrations, list)
        or not raw_integrations
        or any(item not in SUPPORTED_INTEGRATIONS for item in raw_integrations)
        or len(set(raw_integrations)) != len(raw_integrations)
    ):
        raise ConfigurationError("installed toolchain integrations are invalid")
    integrations = cast(tuple[IntegrationName, ...], tuple(raw_integrations))

    raw_conan = binding["conan"]
    conan = None
    if raw_conan is not None:
        conan_record = object_value(
            raw_conan,
            {"cppstd", "libcxx", "build_type"},
            "installed toolchain Conan record",
        )
        conan = ConanSettings(
            cppstd=conan_record["cppstd"],
            libcxx=conan_record["libcxx"],
            build_type=conan_record["build_type"],
        )
    if ("conan" in integrations) != (conan is not None):
        raise ConfigurationError("installed toolchain Conan selection is inconsistent")
    return _InstalledBundleManifest(
        document=value,
        bundle_id=bundle_id,
        variant=variant,
        integrations=integrations,
        conan=conan,
    )


def _load_installation(
    prefix: Path,
) -> ValidatedPayloadInputs:
    if not _directory(prefix) or {path.name for path in prefix.iterdir()} != {
        "artifacts",
        "binding",
        "bin",
        "manifest.json",
        "tools",
    }:
        raise ConfigurationError(
            f"installed toolchain has an invalid top-level layout: {prefix}"
        )
    manifest = _read_installed_bundle_manifest(prefix)
    lock = ManagedLock.load(prefix / "artifacts" / "managed.lock.json")
    inputs = _load_payload_inputs(
        sdk=prefix / "artifacts" / "sdk",
        build_tools=prefix / "tools",
        compiler_kit=prefix / "artifacts" / "compiler-kit",
        runtime=prefix / "artifacts" / "runtime",
        lock=lock,
        variant=manifest.variant,
        bundle_id=manifest.bundle_id,
        integrations=manifest.integrations,
        conan=manifest.conan,
    )
    if manifest.document != _bundle_manifest(inputs):
        raise ConfigurationError(
            "installed toolchain manifest does not match its artifacts"
        )
    launcher = prefix / "bin" / DEFAULT_LAUNCHER_NAME
    if not _regular_file(launcher) or not os.access(launcher, os.X_OK):
        raise ConfigurationError(f"installed toolchain launcher is missing: {launcher}")
    required_binding = (
        prefix / "binding" / "binding.json",
        prefix / "binding" / "audit-policy.json",
        prefix / "binding" / "env" / "toolchain.env",
        prefix / "binding" / SHELL_INIT_RELATIVE_PATH,
    )
    if any(not _regular_file(path) for path in required_binding):
        raise ConfigurationError("installed toolchain binding is incomplete")
    if manifest.conan is not None:
        required_conan = (
            prefix / "binding" / "conan" / "host.profile",
            prefix / "binding" / "conan" / "build.profile",
            prefix / "binding" / "conan" / "settings_user.yml",
            prefix / "binding" / "conan" / "default.profile",
            prefix / "binding" / "conan" / "lxtc-build.profile",
            prefix / "binding" / "conan" / "conan-home",
            prefix / "binding" / "conan" / "build-profile",
            *(
                (
                    prefix / "binding" / "conan" / "lxtc-libcxx.profile",
                    prefix / "binding" / "conan" / "lxtc-libstdcxx.profile",
                    prefix / "binding" / "conan" / "build-libcxx.profile",
                    prefix / "binding" / "conan" / "build-libstdcxx.profile",
                )
                if {"libstdc++", "libc++"}.issubset(inputs.variant.cxx_runtimes)
                else ()
            ),
        )
        if any(not _regular_file(path) for path in required_conan):
            raise ConfigurationError(
                "installed toolchain Conan configuration is incomplete"
            )
    return inputs


def _instantiate_payload(
    payload: Path,
    prefix: Path,
    *,
    conan_home: Path | None,
    conan_build_profile: Path | None,
) -> tuple[str, ...]:
    template_list = payload / "template-files"
    template_files = tuple(template_list.read_text(encoding="utf-8").splitlines())
    for relative in template_files:
        path = payload / relative
        if not _regular_file(path):
            raise ConfigurationError(f"installation template is not a file: {path}")
        content = path.read_bytes()
        path.write_bytes(content.replace(PREFIX_TOKEN.encode(), str(prefix).encode()))
    template_list.unlink()
    if (conan_home is None) != (conan_build_profile is None):
        raise ConfigurationError("installed Conan paths must be provided together")
    if conan_home is not None and conan_build_profile is not None:
        conan_dir = payload / "binding" / "conan"
        (conan_dir / "conan-home").write_text(f"{conan_home}\n", encoding="utf-8")
        (conan_dir / "build-profile").write_text(
            f"{conan_build_profile}\n", encoding="utf-8"
        )
    return template_files


def _object_field(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} is not an object")
    return value


def _string_field(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{context} is not a non-empty string")
    return value


def _absolute_path(value: object, context: str) -> Path:
    path = Path(_string_field(value, context))
    if not path.is_absolute():
        raise ConfigurationError(f"{context} is not an absolute path")
    if path != Path(os.path.normpath(str(path))):
        raise ConfigurationError(f"{context} is not a canonical absolute path")
    return path


def _require_path(value: object, expected: Path, context: str) -> None:
    path = _absolute_path(value, context)
    try:
        canonical_expected = expected.resolve()
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"cannot resolve {context}") from error
    if path != expected or canonical_expected != expected:
        raise ConfigurationError(f"{context} points outside its installed root")


def _require_path_below(
    value: object,
    roots: tuple[Path, ...],
    context: str,
    *,
    resolved_roots: tuple[Path, ...] | None = None,
) -> None:
    path = _absolute_path(value, context)
    if not any(path == root or root in path.parents for root in roots):
        raise ConfigurationError(f"{context} points outside its installed roots")
    allowed_resolved_roots = roots if resolved_roots is None else resolved_roots
    try:
        canonical_path = path.resolve()
        canonical_roots = tuple(root.resolve() for root in roots)
        canonical_allowed_roots = tuple(
            root.resolve() for root in allowed_resolved_roots
        )
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(f"cannot resolve {context}") from error
    if any(
        root != canonical
        for root, canonical in zip(roots, canonical_roots, strict=True)
    ):
        raise ConfigurationError(f"{context} has a non-canonical installed root")
    if any(
        root != canonical
        for root, canonical in zip(
            allowed_resolved_roots,
            canonical_allowed_roots,
            strict=True,
        )
    ):
        raise ConfigurationError(f"{context} has a non-canonical installed root")
    if not any(
        canonical_path == root or root in canonical_path.parents
        for root in canonical_allowed_roots
    ):
        raise ConfigurationError(f"{context} points outside its installed roots")


@dataclass(frozen=True)
class _InstalledBindingRoots:
    binding: Path
    sdk: Path
    compiler_kit: Path
    runtime: Path


def _validate_installed_compiler(
    manifest: Mapping[str, object],
    roots: _InstalledBindingRoots,
) -> None:
    sdk = _object_field(manifest.get("sdk"), "installed binding.sdk")
    _require_path(sdk.get("path"), roots.sdk, "installed binding.sdk.path")
    compiler = _object_field(manifest.get("compiler"), "installed binding.compiler")
    toolchain = _object_field(
        compiler.get("toolchain"), "installed binding.compiler.toolchain"
    )
    if toolchain.get("mode") != "managed":
        raise ConfigurationError("installed binding compiler is not managed")
    _require_path(
        toolchain.get("path"),
        roots.compiler_kit,
        "installed binding.compiler.toolchain.path",
    )
    _require_path(
        toolchain.get("manifest_path"),
        roots.compiler_kit / "manifest.json",
        "installed binding.compiler.toolchain.manifest_path",
    )

    drivers = _object_field(
        compiler.get("drivers"), "installed binding.compiler.drivers"
    )
    for language in ("c", "cxx"):
        driver = _object_field(
            drivers.get(language),
            f"installed binding.compiler.drivers.{language}",
        )
        _require_path_below(
            driver.get("invocation_path"),
            (roots.compiler_kit,),
            f"installed binding.compiler.drivers.{language}.invocation_path",
        )
        _require_path_below(
            driver.get("wrapper"),
            (roots.binding,),
            f"installed binding.compiler.drivers.{language}.wrapper",
            resolved_roots=(roots.binding, roots.compiler_kit),
        )

    tools = _object_field(compiler.get("tools"), "installed binding.compiler.tools")
    for name, raw_tool in tools.items():
        if name == "selection":
            continue
        tool = _object_field(raw_tool, f"installed binding.compiler.tools.{name}")
        _require_path_below(
            tool.get("invocation_path"),
            (roots.compiler_kit,),
            f"installed binding.compiler.tools.{name}.invocation_path",
        )
        _require_path_below(
            tool.get("wrapper"),
            (roots.binding,),
            f"installed binding.compiler.tools.{name}.wrapper",
            resolved_roots=(roots.binding, roots.compiler_kit),
        )


def _validate_installed_runtimes(
    manifest: Mapping[str, object],
    roots: _InstalledBindingRoots,
    inputs: ValidatedPayloadInputs,
) -> None:
    runtimes = _object_field(
        manifest.get("cxx_runtimes"), "installed binding.cxx_runtimes"
    )
    if set(runtimes) != {"default", "available"}:
        raise ConfigurationError("installed binding C++ runtime selection is invalid")
    available = runtimes.get("available")
    if not isinstance(available, list) or not available:
        raise ConfigurationError("installed binding C++ runtime list is invalid")
    if runtimes.get("default") != inputs.variant.default_cxx_runtime:
        raise ConfigurationError("installed binding C++ runtime default is invalid")

    actual_runtime_kinds: list[str] = []
    for index, raw_runtime in enumerate(available):
        runtime = _object_field(
            raw_runtime,
            f"installed binding.cxx_runtimes.available[{index}]",
        )
        kind = _string_field(
            runtime.get("kind"),
            f"installed binding.cxx_runtimes.available[{index}].kind",
        )
        actual_runtime_kinds.append(kind)
        expected_directory = MANAGED_RUNTIME_DIRECTORY_NAMES.get(kind)
        if expected_directory is None:
            raise ConfigurationError("installed binding C++ runtime kind is invalid")
        _require_path(
            runtime.get("path"),
            roots.runtime / "runtimes" / expected_directory,
            f"installed binding.cxx_runtimes.available[{index}].path",
        )
    if tuple(actual_runtime_kinds) != inputs.variant.cxx_runtimes:
        raise ConfigurationError("installed binding C++ runtime list is invalid")


def _validate_installed_glibc_paths(
    manifest: Mapping[str, object],
    roots: _InstalledBindingRoots,
) -> None:
    _require_path(
        manifest.get("audit_policy"),
        roots.binding / "audit-policy.json",
        "installed binding.audit_policy",
    )
    glibc = _object_field(
        manifest.get("glibc_binding"), "installed binding.glibc_binding"
    )
    _require_path(
        glibc.get("startfile_overlay"),
        roots.binding / "glibc-startfiles",
        "installed binding.glibc_binding.startfile_overlay",
    )
    library_dirs = glibc.get("library_dirs")
    if not isinstance(library_dirs, list):
        raise ConfigurationError(
            "installed binding.glibc_binding.library_dirs is not an array"
        )
    for index, path in enumerate(library_dirs):
        _require_path_below(
            path,
            (roots.sdk, roots.runtime, roots.binding),
            f"installed binding.glibc_binding.library_dirs[{index}]",
        )


def _validate_installed_integrations(
    manifest: Mapping[str, object],
    roots: _InstalledBindingRoots,
    inputs: ValidatedPayloadInputs,
) -> None:
    integrations = _object_field(
        manifest.get("integrations"), "installed binding.integrations"
    )
    if set(integrations) != set(inputs.integrations):
        raise ConfigurationError("installed binding integrations are inconsistent")
    integration_paths = {
        "cmake": {"toolchain": roots.binding / "cmake" / "toolchain.cmake"},
        "shell": {"environment": roots.binding / "env" / "toolchain.env"},
        "conan": {
            "host_profile": roots.binding / "conan" / "host.profile",
            "cmake_toolchain": roots.binding / "conan" / "cmake-toolchain.cmake",
            "cmake_late": roots.binding / "conan" / "cmake-late.cmake",
        },
    }
    for integration in inputs.integrations:
        record = _object_field(
            integrations.get(integration),
            f"installed binding.integrations.{integration}",
        )
        for field, expected in integration_paths[integration].items():
            _require_path(
                record.get(field),
                expected,
                f"installed binding.integrations.{integration}.{field}",
            )
            if not _regular_file(expected):
                raise ConfigurationError(
                    f"installed integration file is missing: {expected}"
                )


def _validate_installed_conan_paths(
    roots: _InstalledBindingRoots,
    inputs: ValidatedPayloadInputs,
    conan_home: Path | None,
    conan_build_profile: Path | None,
) -> None:
    if inputs.conan is None:
        return
    if conan_home is None or conan_build_profile is None:
        raise ConfigurationError(
            "installed Conan paths are required for Conan integration"
        )
    conan_root = roots.binding / "conan"
    if (conan_root / "conan-home").read_text(encoding="utf-8") != (f"{conan_home}\n"):
        raise ConfigurationError("installed Conan home is inconsistent")
    if (conan_root / "build-profile").read_text(encoding="utf-8") != (
        f"{conan_build_profile}\n"
    ):
        raise ConfigurationError("installed Conan build profile is inconsistent")


def _validate_binding_paths(
    prefix: Path,
    inputs: ValidatedPayloadInputs,
    *,
    conan_home: Path | None,
    conan_build_profile: Path | None,
) -> None:
    roots = _InstalledBindingRoots(
        binding=prefix / "binding",
        sdk=prefix / "artifacts" / "sdk",
        compiler_kit=prefix / "artifacts" / "compiler-kit",
        runtime=prefix / "artifacts" / "runtime",
    )
    manifest = read_json_object(roots.binding / "binding.json", "installed binding")
    manifest_format = manifest.get("format")
    if (
        manifest.get("schema") != BINDING_SCHEMA
        or not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != BINDING_FORMAT
    ):
        raise ConfigurationError("installed binding manifest is unsupported")
    _validate_installed_compiler(manifest, roots)
    _validate_installed_runtimes(manifest, roots, inputs)
    _validate_installed_glibc_paths(manifest, roots)
    _validate_installed_integrations(manifest, roots, inputs)

    policy = load_policy(roots.binding / "audit-policy.json")
    if (
        policy.machine != inputs.variant.target.arch
        or policy.glibc_floor != inputs.variant.target.glibc_floor
    ):
        raise ConfigurationError("installed audit policy does not match the target")
    _validate_installed_conan_paths(
        roots,
        inputs,
        conan_home,
        conan_build_profile,
    )


def _validate_installation_relocation(
    published: Path,
    installed: ValidatedPayloadInputs,
    *,
    producer_inputs: ValidatedPayloadInputs,
    template_files: tuple[str, ...],
    source_binding: Path | None,
    staging_root: Path,
    conan_home: Path | None,
    conan_build_profile: Path | None,
) -> None:
    _validate_binding_paths(
        published,
        installed,
        conan_home=conan_home,
        conan_build_profile=conan_build_profile,
    )
    forbidden = {
        PREFIX_TOKEN.encode(),
        str(producer_inputs.sdk).encode(),
        str(producer_inputs.build_tools).encode(),
        str(producer_inputs.compiler_kit).encode(),
        str(producer_inputs.runtime).encode(),
        str(staging_root).encode(),
    }
    if source_binding is not None:
        forbidden.add(str(source_binding.resolve()).encode())
    for relative in template_files:
        path = published / relative
        if not _regular_file(path):
            raise ConfigurationError(f"installed template file is missing: {path}")
        content = path.read_bytes()
        for value in forbidden:
            if value and value in content:
                raise ConfigurationError(
                    f"installed template retains an unrelocated path: {path}"
                )


def create_bundle(
    *,
    sdk: Path,
    build_tools: Path,
    compiler_kit: Path,
    runtime: Path,
    lock: ManagedLock | Path,
    variant: str,
    output: Path,
    bundle_id: str | None = None,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    binding_template: Path | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    archive_progress: ArchiveProgressCallback | None = None,
) -> Path:
    inputs = _load_payload_inputs(
        sdk=sdk,
        build_tools=build_tools,
        compiler_kit=compiler_kit,
        runtime=runtime,
        lock=lock,
        variant=variant,
        bundle_id=bundle_id,
        integrations=integrations,
        conan=conan,
    )
    return create_bundle_from_validated_inputs(
        inputs,
        output=output,
        binding_template=binding_template,
        force=force,
        progress=progress,
        archive_progress=archive_progress,
    )


def create_bundle_from_validated_inputs(
    inputs: ValidatedPayloadInputs,
    *,
    output: Path,
    binding_template: Path | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    archive_progress: ArchiveProgressCallback | None = None,
) -> Path:
    raw_output = output.expanduser()
    if raw_output.is_symlink() or (
        raw_output.exists() and not _regular_file(raw_output)
    ):
        raise ConfigurationError(f"bundle output is not a regular file: {raw_output}")
    if raw_output.exists() and not force:
        raise ConfigurationError(f"bundle output already exists: {raw_output}")
    destination = raw_output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.build-", dir=destination.parent
    ) as directory:
        workspace = Path(directory)
        payload = workspace / "payload"
        payload.mkdir()
        _write_payload_metadata(
            payload,
            inputs,
            sdk=inputs.sdk,
            compiler_kit=inputs.compiler_kit,
            runtime=inputs.runtime,
            artifact_paths={
                inputs.sdk: "artifacts/sdk",
                inputs.compiler_kit: "artifacts/compiler-kit",
                inputs.runtime: "artifacts/runtime",
            },
            binding_template=binding_template,
            progress=progress,
        )

        _emit(progress, "bundle: writing self-extracting installer")
        trees = (
            (inputs.sdk, "artifacts/sdk"),
            (inputs.build_tools, "tools"),
            (inputs.compiler_kit, "artifacts/compiler-kit"),
            (inputs.runtime, "artifacts/runtime"),
        )
        _write_installer(
            destination,
            payload,
            trees=trees,
            progress=archive_progress,
            header=lambda payload_bytes: render_installer_header(
                host_arch=inputs.host["arch"],
                host_floor=inputs.host["glibc_floor"],
                target_arch=inputs.variant.target.arch,
                target_floor=inputs.variant.target.glibc_floor,
                bundle_id=inputs.bundle_id,
                default_installation_name=_default_installation_name(inputs),
                conan=inputs.conan is not None,
                cxx_runtimes=inputs.variant.cxx_runtimes,
                default_cxx_runtime=inputs.variant.default_cxx_runtime,
                payload_bytes=payload_bytes,
            ),
            force=force,
        )
    return destination


def publish_installation(
    *,
    sdk: Path,
    build_tools: Path,
    compiler_kit: Path,
    runtime: Path,
    lock: ManagedLock | Path,
    variant: str,
    prefix: Path,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    conan_home: Path | None = None,
    conan_build_profile: Path | None = None,
    binding_template: Path | None = None,
    bundle_id: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> Path:
    inputs = _load_payload_inputs(
        sdk=sdk,
        build_tools=build_tools,
        compiler_kit=compiler_kit,
        runtime=runtime,
        lock=lock,
        variant=variant,
        bundle_id=bundle_id,
        integrations=integrations,
        conan=conan,
    )
    return publish_installation_from_validated_inputs(
        inputs,
        prefix=prefix,
        conan_home=conan_home,
        conan_build_profile=conan_build_profile,
        binding_template=binding_template,
        force=force,
        progress=progress,
    )


def publish_installation_from_validated_inputs(
    inputs: ValidatedPayloadInputs,
    *,
    prefix: Path,
    conan_home: Path | None = None,
    conan_build_profile: Path | None = None,
    binding_template: Path | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> Path:
    raw_prefix = prefix.expanduser()
    if raw_prefix.is_symlink() or (raw_prefix.exists() and not raw_prefix.is_dir()):
        raise ConfigurationError(
            f"installation prefix is not a directory: {raw_prefix}"
        )
    destination = raw_prefix.resolve()
    if (
        destination in {Path("/"), Path.home().resolve()}
        or _INSTALL_PREFIX.fullmatch(str(destination)) is None
    ):
        raise ConfigurationError(f"invalid installation prefix: {destination}")
    installed_conan_home, installed_build_profile = _resolve_conan_paths(
        inputs,
        prefix=destination,
        conan_home=conan_home,
        conan_build_profile=conan_build_profile,
    )
    if destination.exists() and next(destination.iterdir(), None) is not None:
        current = _load_installation(destination)
        if (
            current.lock.sha256 != inputs.lock.sha256
            or current.variant.id != inputs.variant.id
            or current.integrations != inputs.integrations
            or current.conan != inputs.conan
            or current.bundle_id != inputs.bundle_id
            or current.build_tools_artifact.identity
            != inputs.build_tools_artifact.identity
        ):
            raise ConfigurationError(
                "installation prefix selects a different toolchain; use a new prefix"
            )
        if not force:
            _validate_binding_paths(
                destination,
                current,
                conan_home=installed_conan_home,
                conan_build_profile=installed_build_profile,
            )
            _emit(progress, "setup: using validated installation ... DONE")
            return destination / "bin" / DEFAULT_LAUNCHER_NAME

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)
    )
    try:
        _emit(progress, "setup: publishing final installation")
        _write_payload(
            staging,
            inputs,
            binding_template=binding_template,
            progress=None,
        )
        template_files = _instantiate_payload(
            staging,
            destination,
            conan_home=installed_conan_home,
            conan_build_profile=installed_build_profile,
        )
        if installed_conan_home is not None:
            _prepare_conan_home(
                installed_conan_home,
                staging / "binding",
                bundle_id=inputs.bundle_id,
                cxx_runtimes=inputs.variant.cxx_runtimes,
                default_cxx_runtime=inputs.variant.default_cxx_runtime,
            )

        def validate(published: Path) -> None:
            installed = _load_installation(published)
            if (
                installed.lock.sha256 != inputs.lock.sha256
                or installed.variant.id != inputs.variant.id
                or installed.integrations != inputs.integrations
                or installed.conan != inputs.conan
                or installed.bundle_id != inputs.bundle_id
                or installed.build_tools_artifact.identity
                != inputs.build_tools_artifact.identity
            ):
                raise ConfigurationError(
                    "published toolchain selection is inconsistent"
                )
            _validate_installation_relocation(
                published,
                installed,
                producer_inputs=inputs,
                template_files=template_files,
                source_binding=binding_template,
                staging_root=staging,
                conan_home=installed_conan_home,
                conan_build_profile=installed_build_profile,
            )

        replace_directory(staging, destination, validate=validate)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    _emit(progress, "setup: installation ready ... DONE")
    return destination / "bin" / DEFAULT_LAUNCHER_NAME


def create_setup_bundle(
    *,
    prefix: Path,
    output: Path,
    bundle_id: str | None = None,
    force: bool = False,
    progress: ProgressCallback | None = None,
    archive_progress: ArchiveProgressCallback | None = None,
) -> Path:
    installation = prefix.expanduser().resolve()
    inputs = _load_installation(installation)
    if bundle_id is not None:
        inputs = replace(
            inputs,
            bundle_id=_identifier(bundle_id, "bundle id"),
        )
    return create_bundle_from_validated_inputs(
        inputs,
        output=output,
        binding_template=installation / "binding",
        force=force,
        progress=progress,
        archive_progress=archive_progress,
    )
