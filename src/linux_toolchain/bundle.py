from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence, cast

from linux_toolchain.bundle_installer import (
    CONAN_DEFAULT_BUILD_PROFILE,
    CONAN_DEFAULT_PROFILE,
    DEFAULT_LAUNCHER_NAME,
    PREFIX_TOKEN,
    default_conan_home_name,
    relocate_binding_links,
    render_installer_header,
    render_launcher,
    template_binding,
    write_payload_archive,
)
from linux_toolchain.compiler.binding import BINDING_FORMAT, BINDING_SCHEMA
from linux_toolchain.compiler.managed import validate_current_host
from linux_toolchain.compiler.managed_binding import create_managed_binding
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
from linux_toolchain.managed.lockfile import VariantLock
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimePublication,
    load_managed_compiler_artifact,
    load_managed_runtime_publication,
)
from linux_toolchain.publication import replace_directory
from linux_toolchain.schema import canonical_json_bytes, read_json_object
from linux_toolchain.versions import AbiVersion

BUNDLE_SCHEMA = "linux-toolchain-bundle"
BUNDLE_FORMAT = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
_INSTALL_PREFIX = re.compile(r"/[A-Za-z0-9/._+@=-]+")
ProgressCallback = Callable[[str], None]
ArchiveProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class _PayloadInputs:
    sdk: Path
    compiler_kit: Path
    runtime: Path
    lock: ManagedLock
    variant: VariantLock
    host: Mapping[str, str]
    runtime_kind: str
    bundle_id: str
    integrations: tuple[IntegrationName, ...]
    conan: ConanSettings | None
    compiler_artifact: ManagedCompilerArtifact
    runtime_publication: ManagedRuntimePublication


def _bundle_manifest(inputs: _PayloadInputs) -> dict[str, object]:
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
        "runtime_kind": inputs.runtime_kind,
        "binding": {
            "integrations": list(inputs.integrations),
            "conan": conan,
        },
    }


def _bundle_info(
    inputs: _PayloadInputs,
    *,
    binding: Path,
    sdk: Path,
) -> str:
    target_triplet = getattr(
        inputs.compiler_artifact,
        "target",
        f"{inputs.variant.target.arch}-portable-linux-gnu",
    )
    if not isinstance(target_triplet, str) or not target_triplet:
        raise ConfigurationError("bundle compiler target is invalid")
    runtimes = tuple(
        runtime
        for runtime in inputs.lock.runtimes
        if runtime.id == inputs.variant.runtime_id
    )
    if len(runtimes) != 1:
        raise ConfigurationError("bundle runtime selection is invalid")
    runtime = runtimes[0]
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
        ("cxx_runtime.kind", inputs.variant.cxx_runtime),
        ("cxx_runtime.provider", runtime.provider_family),
        ("cxx_runtime.version", runtime.provider_version),
        ("integrations", ",".join(inputs.integrations)),
        ("conan.enabled", "true" if inputs.conan is not None else "false"),
    ]
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


def _prepare_conan_home(home: Path, binding: Path) -> None:
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
    _write_conan_configuration(
        home / "settings_user.yml",
        (conan / "settings_user.yml").read_text(encoding="utf-8"),
    )
    _write_conan_configuration(
        profiles / "default",
        (conan / "default.profile").read_text(encoding="utf-8"),
    )
    _write_conan_configuration(
        profiles / "lxtc-build",
        (conan / "lxtc-build.profile").read_text(encoding="utf-8"),
    )


def _resolve_conan_paths(
    inputs: _PayloadInputs,
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


def _resolve_payload_inputs(
    *,
    sdk: Path,
    compiler_kit: Path,
    runtime: Path,
    lock: ManagedLock | Path,
    variant: str,
    bundle_id: str | None,
    integrations: Sequence[IntegrationName],
    conan: ConanSettings | None,
    compiler_artifact: ManagedCompilerArtifact | None = None,
    runtime_publication: ManagedRuntimePublication | None = None,
) -> _PayloadInputs:
    selected_integrations = tuple(integrations)
    if ("conan" in selected_integrations) != (conan is not None):
        raise ConfigurationError(
            "Conan settings are required exactly when Conan integration is selected"
        )
    managed_lock = lock if isinstance(lock, ManagedLock) else ManagedLock.load(lock)
    variants = tuple(item for item in managed_lock.variants if item.id == variant)
    if len(variants) != 1:
        raise ConfigurationError(f"managed variant {variant!r} does not exist")
    selected = variants[0]
    compiler = compiler_artifact or load_managed_compiler_artifact(
        managed_lock, selected.compiler_kit_id, compiler_kit
    )
    runtime_publication = runtime_publication or load_managed_runtime_publication(
        managed_lock, selected.runtime_id, runtime
    )
    if compiler.root != compiler_kit.expanduser().resolve():
        raise ConfigurationError("validated Compiler Kit path changed")
    if runtime_publication.root != runtime.expanduser().resolve():
        raise ConfigurationError("validated runtime publication path changed")
    if (
        compiler.selection.artifact_id != selected.compiler_kit_id
        or runtime_publication.selection.artifact_id != selected.runtime_id
    ):
        raise ConfigurationError("validated managed artifacts do not match the variant")
    assert compiler.selection.host is not None
    return _PayloadInputs(
        sdk=sdk.resolve(),
        compiler_kit=compiler.root,
        runtime=runtime_publication.root,
        lock=managed_lock,
        variant=selected,
        host=compiler.selection.host.to_dict(),
        runtime_kind=runtime_publication.selection.runtime_kind,
        bundle_id=_identifier(
            bundle_id or f"{managed_lock.name}-{selected.id}", "bundle id"
        ),
        integrations=selected_integrations,
        conan=conan,
        compiler_artifact=compiler,
        runtime_publication=runtime_publication,
    )


def _conan_build_profile(inputs: _PayloadInputs, binding: Path, runtime: Path) -> str:
    raw_library_dirs = inputs.runtime_publication.manifest.locations.get("library_dirs")
    if not isinstance(raw_library_dirs, tuple) or not raw_library_dirs:
        raise ConfigurationError(
            "managed runtime has no library directories for the Conan build profile"
        )
    library_dirs = tuple(runtime / str(relative) for relative in raw_library_dirs)
    return (
        "# Conan build requirements use this bundle's managed native toolchain.\n"
        f"include({binding / 'conan' / 'host.profile'})\n\n"
        "[buildenv]\n"
        f"LD_LIBRARY_PATH=+(path){':'.join(str(path) for path in library_dirs)}\n"
    )


def _write_payload_metadata(
    payload: Path,
    inputs: _PayloadInputs,
    *,
    sdk: Path,
    compiler_kit: Path,
    runtime: Path,
    artifact_paths: Mapping[Path, str] | None = None,
    binding_template: Path | None = None,
    progress: ProgressCallback | None,
    compiler_artifact: ManagedCompilerArtifact | None = None,
    runtime_publication: ManagedRuntimePublication | None = None,
) -> None:
    artifacts = payload / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    lock_path = artifacts / "managed.lock.json"
    lock_path.write_bytes(canonical_json_bytes(inputs.lock.to_dict()))

    _emit(progress, "bundle: creating binding template")
    binding = payload / "binding"
    template_paths = dict(artifact_paths or {})
    if binding_template is None:
        create_managed_binding(
            sdk,
            binding,
            compiler_kit,
            lock=inputs.lock,
            variant=inputs.variant.id,
            runtime=runtime,
            integrations=inputs.integrations,
            conan=inputs.conan,
            _compiler_artifact=compiler_artifact,
            _runtime_publication=runtime_publication,
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
        (binding / "conan" / "settings_user.yml").write_text(
            SETTINGS_USER_YAML,
            encoding="utf-8",
        )
        (binding / "conan" / "default.profile").write_text(
            CONAN_DEFAULT_PROFILE,
            encoding="utf-8",
        )
        (binding / "conan" / "lxtc-build.profile").write_text(
            CONAN_DEFAULT_BUILD_PROFILE,
            encoding="utf-8",
        )
        (binding / "conan" / "build.profile").write_text(
            _conan_build_profile(inputs, binding, runtime),
            encoding="utf-8",
        )
    (binding / "env" / "toolchain.info").write_text(
        _bundle_info(inputs, binding=binding, sdk=sdk),
        encoding="utf-8",
    )

    launcher = payload / "bin" / DEFAULT_LAUNCHER_NAME
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        render_launcher(conan=inputs.conan is not None), encoding="utf-8"
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
    inputs: _PayloadInputs,
    *,
    binding_template: Path | None = None,
    progress: ProgressCallback | None,
) -> None:
    artifacts = payload / "artifacts"
    artifacts.mkdir(parents=True)

    _emit(progress, "bundle: copying portable artifacts")
    _copy_tree(inputs.sdk, artifacts / "sdk")
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


def _load_installation(
    prefix: Path,
) -> _PayloadInputs:
    if not _directory(prefix) or {path.name for path in prefix.iterdir()} != {
        "artifacts",
        "binding",
        "bin",
        "manifest.json",
    }:
        raise ConfigurationError(
            f"installed toolchain has an invalid top-level layout: {prefix}"
        )
    manifest_path = prefix / "manifest.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read installed toolchain manifest {manifest_path}: {error}"
        ) from error
    expected = {
        "schema",
        "format",
        "id",
        "variant",
        "compiler",
        "target",
        "host",
        "runtime_kind",
        "binding",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ConfigurationError("installed toolchain manifest has invalid keys")
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
    binding = value["binding"]
    if not isinstance(binding, dict) or set(binding) != {"integrations", "conan"}:
        raise ConfigurationError("installed toolchain binding record is invalid")
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
        if not isinstance(raw_conan, dict) or set(raw_conan) != {
            "cppstd",
            "libcxx",
            "build_type",
        }:
            raise ConfigurationError("installed toolchain Conan record is invalid")
        conan = ConanSettings(
            cppstd=raw_conan["cppstd"],
            libcxx=raw_conan["libcxx"],
            build_type=raw_conan["build_type"],
        )
    if ("conan" in integrations) != (conan is not None):
        raise ConfigurationError("installed toolchain Conan selection is inconsistent")
    lock = ManagedLock.load(prefix / "artifacts" / "managed.lock.json")
    inputs = _resolve_payload_inputs(
        sdk=prefix / "artifacts" / "sdk",
        compiler_kit=prefix / "artifacts" / "compiler-kit",
        runtime=prefix / "artifacts" / "runtime",
        lock=lock,
        variant=variant,
        bundle_id=bundle_id,
        integrations=integrations,
        conan=conan,
    )
    if value != _bundle_manifest(inputs):
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
    )
    if any(not _regular_file(path) for path in required_binding):
        raise ConfigurationError("installed toolchain binding is incomplete")
    if conan is not None:
        required_conan = (
            prefix / "binding" / "conan" / "host.profile",
            prefix / "binding" / "conan" / "build.profile",
            prefix / "binding" / "conan" / "settings_user.yml",
            prefix / "binding" / "conan" / "default.profile",
            prefix / "binding" / "conan" / "lxtc-build.profile",
            prefix / "binding" / "conan" / "conan-home",
            prefix / "binding" / "conan" / "build-profile",
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


def _validate_binding_paths(
    prefix: Path,
    inputs: _PayloadInputs,
    *,
    conan_home: Path | None,
    conan_build_profile: Path | None,
) -> None:
    binding_root = prefix / "binding"
    sdk_root = prefix / "artifacts" / "sdk"
    compiler_kit_root = prefix / "artifacts" / "compiler-kit"
    runtime_root = prefix / "artifacts" / "runtime"
    manifest = read_json_object(binding_root / "binding.json", "installed binding")
    manifest_format = manifest.get("format")
    if (
        manifest.get("schema") != BINDING_SCHEMA
        or not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != BINDING_FORMAT
    ):
        raise ConfigurationError("installed binding manifest is unsupported")

    sdk = _object_field(manifest.get("sdk"), "installed binding.sdk")
    _require_path(sdk.get("path"), sdk_root, "installed binding.sdk.path")
    compiler = _object_field(manifest.get("compiler"), "installed binding.compiler")
    toolchain = _object_field(
        compiler.get("toolchain"), "installed binding.compiler.toolchain"
    )
    if toolchain.get("mode") != "managed":
        raise ConfigurationError("installed binding compiler is not managed")
    _require_path(
        toolchain.get("path"),
        compiler_kit_root,
        "installed binding.compiler.toolchain.path",
    )
    _require_path(
        toolchain.get("manifest_path"),
        compiler_kit_root / "manifest.json",
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
            (compiler_kit_root,),
            f"installed binding.compiler.drivers.{language}.invocation_path",
        )
        _require_path_below(
            driver.get("wrapper"),
            (binding_root,),
            f"installed binding.compiler.drivers.{language}.wrapper",
            resolved_roots=(binding_root, compiler_kit_root),
        )
    tools = _object_field(compiler.get("tools"), "installed binding.compiler.tools")
    for name, raw_tool in tools.items():
        if name == "selection":
            continue
        tool = _object_field(raw_tool, f"installed binding.compiler.tools.{name}")
        _require_path_below(
            tool.get("invocation_path"),
            (compiler_kit_root,),
            f"installed binding.compiler.tools.{name}.invocation_path",
        )
        _require_path_below(
            tool.get("wrapper"),
            (binding_root,),
            f"installed binding.compiler.tools.{name}.wrapper",
            resolved_roots=(binding_root, compiler_kit_root),
        )

    runtime = _object_field(
        manifest.get("cxx_runtime"), "installed binding.cxx_runtime"
    )
    _require_path(
        runtime.get("path"), runtime_root, "installed binding.cxx_runtime.path"
    )
    _require_path(
        manifest.get("audit_policy"),
        binding_root / "audit-policy.json",
        "installed binding.audit_policy",
    )
    glibc = _object_field(
        manifest.get("glibc_binding"), "installed binding.glibc_binding"
    )
    _require_path(
        glibc.get("startfile_overlay"),
        binding_root / "glibc-startfiles",
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
            (sdk_root, runtime_root, binding_root),
            f"installed binding.glibc_binding.library_dirs[{index}]",
        )

    integrations = _object_field(
        manifest.get("integrations"), "installed binding.integrations"
    )
    if set(integrations) != set(inputs.integrations):
        raise ConfigurationError("installed binding integrations are inconsistent")
    integration_paths = {
        "cmake": {"toolchain": binding_root / "cmake" / "toolchain.cmake"},
        "shell": {"environment": binding_root / "env" / "toolchain.env"},
        "conan": {
            "host_profile": binding_root / "conan" / "host.profile",
            "cmake_toolchain": binding_root / "conan" / "cmake-toolchain.cmake",
            "cmake_late": binding_root / "conan" / "cmake-late.cmake",
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

    policy = load_policy(binding_root / "audit-policy.json")
    if (
        policy.machine != inputs.variant.target.arch
        or policy.glibc_floor != inputs.variant.target.glibc_floor
    ):
        raise ConfigurationError("installed audit policy does not match the target")
    if inputs.conan is not None:
        if conan_home is None or conan_build_profile is None:
            raise ConfigurationError(
                "installed Conan paths are required for Conan integration"
            )
        conan_root = binding_root / "conan"
        if (conan_root / "conan-home").read_text(encoding="utf-8") != (
            f"{conan_home}\n"
        ):
            raise ConfigurationError("installed Conan home is inconsistent")
        if (conan_root / "build-profile").read_text(encoding="utf-8") != (
            f"{conan_build_profile}\n"
        ):
            raise ConfigurationError("installed Conan build profile is inconsistent")


def _validate_installation_relocation(
    published: Path,
    installed: _PayloadInputs,
    *,
    producer_inputs: _PayloadInputs,
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
    _compiler_artifact: ManagedCompilerArtifact | None = None,
    _runtime_publication: ManagedRuntimePublication | None = None,
) -> Path:
    inputs = _resolve_payload_inputs(
        sdk=sdk,
        compiler_kit=compiler_kit,
        runtime=runtime,
        lock=lock,
        variant=variant,
        bundle_id=bundle_id,
        integrations=integrations,
        conan=conan,
        compiler_artifact=_compiler_artifact,
        runtime_publication=_runtime_publication,
    )
    return _create_bundle_from_inputs(
        inputs,
        output=output,
        binding_template=binding_template,
        force=force,
        progress=progress,
        archive_progress=archive_progress,
    )


def _create_bundle_from_inputs(
    inputs: _PayloadInputs,
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
            compiler_artifact=inputs.compiler_artifact,
            runtime_publication=inputs.runtime_publication,
        )

        _emit(progress, "bundle: writing self-extracting installer")
        trees = (
            (inputs.sdk, "artifacts/sdk"),
            (inputs.compiler_kit, "artifacts/compiler-kit"),
            (inputs.runtime, "artifacts/runtime"),
        )
        _write_installer(
            destination,
            payload,
            trees=trees,
            progress=archive_progress,
            header=lambda payload_entries: render_installer_header(
                host_arch=inputs.host["arch"],
                host_floor=inputs.host["glibc_floor"],
                target_arch=inputs.variant.target.arch,
                target_floor=inputs.variant.target.glibc_floor,
                bundle_id=inputs.bundle_id,
                conan=inputs.conan is not None,
                payload_entries=payload_entries,
            ),
            force=force,
        )
    return destination


def publish_installation(
    *,
    sdk: Path,
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
    _compiler_artifact: ManagedCompilerArtifact | None = None,
    _runtime_publication: ManagedRuntimePublication | None = None,
) -> Path:
    inputs = _resolve_payload_inputs(
        sdk=sdk,
        compiler_kit=compiler_kit,
        runtime=runtime,
        lock=lock,
        variant=variant,
        bundle_id=bundle_id,
        integrations=integrations,
        conan=conan,
        compiler_artifact=_compiler_artifact,
        runtime_publication=_runtime_publication,
    )
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
        if installed_conan_home is not None:
            _prepare_conan_home(installed_conan_home, staging / "binding")
        template_files = _instantiate_payload(
            staging,
            destination,
            conan_home=installed_conan_home,
            conan_build_profile=installed_build_profile,
        )

        def validate(published: Path) -> None:
            installed = _load_installation(published)
            if (
                installed.lock.sha256 != inputs.lock.sha256
                or installed.variant.id != inputs.variant.id
                or installed.integrations != inputs.integrations
                or installed.conan != inputs.conan
                or installed.bundle_id != inputs.bundle_id
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
    return _create_bundle_from_inputs(
        inputs,
        output=output,
        binding_template=installation / "binding",
        force=force,
        progress=progress,
        archive_progress=archive_progress,
    )
