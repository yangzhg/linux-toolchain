#!/usr/bin/env python3
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Mapping, Sequence

from linux_toolchain.compiler.binding import BINDING_FORMAT, BINDING_SCHEMA
from linux_toolchain.errors import ExternalToolError, LinuxToolchainError
from linux_toolchain.integrations import SUPPORTED_INTEGRATIONS
from linux_toolchain.process import run_logged, run_streaming
from linux_toolchain.publication import write_json_atomic
from linux_toolchain.schema import object_value, read_json_object
from linux_toolchain.versions import AbiVersion

_MARKER = ".linux-toolchain-smoke-build.json"
_EVIDENCE = ("audit-report.json", "loader-closure.txt", "runtime-output.txt")
_SMOKE_RESULT_SCHEMA = "linux-toolchain-smoke-result"
_SMOKE_RESULT_FORMAT = 1


class SmokeFailure(LinuxToolchainError):
    """The smoke project could not be built, audited, or executed correctly."""


def smoke_project_path() -> Path:
    """Return the installed smoke project's filesystem path."""

    resource = files("linux_toolchain.resources").joinpath("smoke-project")
    project = Path(str(resource))
    if not project.is_dir():
        raise SmokeFailure("packaged smoke project is missing")
    return project.resolve()


PROJECT = smoke_project_path()


@dataclass(frozen=True)
class BuildCommands:
    conan_install: tuple[str, ...] | None
    configure: tuple[str, ...] | None
    build: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeContext:
    loader: Path
    interpreter: Path
    target_arch: str
    glibc_version: str
    library_dirs: tuple[Path, ...]
    allowed_roots: tuple[Path, ...]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add smoke-runner arguments to an argument parser."""

    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument(
        "--build-profile",
        help=(
            "native Conan profile name or path; defaults to an isolated "
            "detected profile"
        ),
    )
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument(
        "--integration",
        choices=SUPPORTED_INTEGRATIONS,
        default="cmake",
        help="build-system integration to exercise (default: cmake)",
    )
    parser.add_argument(
        "--build-type",
        choices=("Debug", "Release", "RelWithDebInfo", "MinSizeRel"),
        default="Release",
        help="consumer build configuration (default: Release)",
    )
    parser.add_argument("--conan", default=os.environ.get("CONAN", "conan"))
    parser.add_argument("--cmake", default=os.environ.get("CMAKE", "cmake"))
    parser.add_argument(
        "--make",
        default=os.environ.get("MAKE", "make"),
        help="Make backend used by every smoke integration",
    )
    parser.add_argument("--conan-home", type=Path)
    parser.add_argument(
        "--runner",
        help="optional target runner, for example qemu-aarch64",
    )
    parser.add_argument("--jobs", type=int)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="linux-toolchain smoke",
        description="Build and run the linux-toolchain integration smoke project",
    )
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        validate_arguments(args)
    except SmokeFailure as error:
        parser.error(str(error))
    return args


def validate_arguments(args: argparse.Namespace) -> None:
    """Validate relationships that argparse cannot express declaratively."""

    if args.jobs is not None and args.jobs < 1:
        raise SmokeFailure("--jobs must be positive")
    if args.conan_home is not None and args.build_profile is None:
        raise SmokeFailure("--conan-home requires --build-profile")
    if args.integration != "conan" and (
        args.conan_home is not None or args.build_profile is not None
    ):
        raise SmokeFailure(
            "--conan-home and --build-profile require --integration conan"
        )


def build_commands(
    *,
    source: Path,
    binding: Path,
    build_profile: str | None,
    build_dir: Path,
    conan: str,
    cmake: str,
    make: str = "make",
    build_type: str,
    jobs: int | None,
    integration: str = "conan",
) -> BuildCommands:
    conan_output = build_dir / "conan"
    cmake_output = build_dir / "cmake"
    if integration not in {"cmake", "conan", "shell"}:
        raise SmokeFailure(f"unsupported integration: {integration!r}")
    if integration == "conan" and not build_profile:
        raise SmokeFailure("Conan integration requires a native build profile")
    toolchain = conan_output / "conan_toolchain.cmake"
    if integration == "cmake":
        toolchain = binding / "cmake" / "toolchain.cmake"
    if integration == "shell":
        build = [
            "/bin/sh",
            "-c",
            '. "$1"; shift; exec "$@"',
            "linux-toolchain-env",
            str(binding / "env" / "toolchain.env"),
            make,
            "-C",
            str(source),
            f"BUILD_DIR={cmake_output}",
            f"BUILD_TYPE={build_type}",
        ]
        if jobs is not None:
            build.extend(("--jobs", str(jobs)))
        configure: tuple[str, ...] | None = None
    else:
        build = [cmake, "--build", str(cmake_output)]
        if jobs is not None:
            build.extend(("--parallel", str(jobs)))
        configure = (
            cmake,
            "-S",
            str(source),
            "-B",
            str(cmake_output),
            "-G",
            "Unix Makefiles",
            f"-DCMAKE_MAKE_PROGRAM={make}",
            f"-DCMAKE_TOOLCHAIN_FILE={toolchain}",
            f"-DCMAKE_BUILD_TYPE={build_type}",
        )
    return BuildCommands(
        conan_install=(
            (
                conan,
                "install",
                str(source),
                "--output-folder",
                str(conan_output),
                f"--profile:build={build_profile}",
                f"--profile:host={binding / 'conan' / 'host.profile'}",
                "--conf=tools.cmake.cmaketoolchain:user_presets=",
                "--build=never",
                "--no-remote",
            )
            if integration == "conan"
            else None
        ),
        configure=configure,
        build=tuple(build),
    )


def _resolve_program(value: str, *, name: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.parent != Path("."):
        resolved = candidate.resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise SmokeFailure(f"{name} is not executable: {resolved}")
        return str(resolved)
    resolved = shutil.which(value)
    if resolved is None:
        raise SmokeFailure(f"cannot find {name} executable: {value}")
    return resolved


def _profile_argument(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    if candidate.parent != Path("."):
        raise SmokeFailure(f"build profile does not exist: {candidate}")
    return value


def _prepare_build_directory(build_dir: Path) -> None:
    if build_dir.is_symlink():
        raise SmokeFailure(f"build directory cannot be a symlink: {build_dir}")
    build_dir.mkdir(parents=True, exist_ok=True)
    marker = build_dir / _MARKER
    expected_marker = {"format": 1, "project": str(PROJECT.resolve())}
    entries = list(build_dir.iterdir())
    if marker.is_symlink() or (marker.exists() and not marker.is_file()):
        raise SmokeFailure(f"invalid smoke build marker: {marker}")
    if entries and not marker.is_file():
        raise SmokeFailure(
            f"refusing to reuse unowned non-empty build directory: {build_dir}"
        )
    if marker.is_file():
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SmokeFailure(f"invalid smoke build marker: {marker}") from error
        if marker_data != expected_marker:
            raise SmokeFailure(
                f"build directory belongs to another project: {build_dir}"
            )

    allowed = {
        _MARKER,
        "conan",
        "conan-home",
        "cmake",
        *_EVIDENCE,
        "result.json",
    }
    unknown = sorted(entry.name for entry in entries if entry.name not in allowed)
    if unknown:
        raise SmokeFailure(
            f"smoke build directory contains unexpected entries: {', '.join(unknown)}"
        )
    conan_home = build_dir / "conan-home"
    if conan_home.is_symlink() or (conan_home.exists() and not conan_home.is_dir()):
        raise SmokeFailure(f"invalid managed Conan home: {conan_home}")
    for name in ("conan", "cmake"):
        generated = build_dir / name
        if generated.is_symlink():
            raise SmokeFailure(f"generated build path cannot be a symlink: {generated}")
        if generated.exists():
            if not generated.is_dir():
                raise SmokeFailure(
                    f"generated build path is not a directory: {generated}"
                )
            shutil.rmtree(generated)
    for name in (*_EVIDENCE, "result.json"):
        evidence = build_dir / name
        if evidence.is_symlink() or (evidence.exists() and not evidence.is_file()):
            raise SmokeFailure(f"invalid smoke evidence path: {evidence}")
        if evidence.exists():
            evidence.unlink()
    marker.write_text(
        json.dumps(expected_marker, sort_keys=True) + "\n", encoding="utf-8"
    )


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SmokeFailure(f"cannot read JSON object: {path}") from error
    if not isinstance(value, dict):
        raise SmokeFailure(f"JSON root is not an object: {path}")
    return value


def _mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise SmokeFailure(f"field is not an object: {field}")
    return value


def _string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SmokeFailure(f"field is not a string: {field}")
    return value


def _strict_object(
    value: object,
    *,
    required: set[str],
    context: str,
) -> dict[str, object]:
    try:
        return object_value(value, required=required, context=context)
    except LinuxToolchainError as error:
        raise SmokeFailure(str(error)) from error


def load_smoke_result(path: Path | str) -> dict[str, object]:
    """Load one successful format-1 smoke result without contextual trust."""

    candidate = Path(path).expanduser()
    try:
        raw = read_json_object(candidate, "smoke result")
    except LinuxToolchainError as error:
        raise SmokeFailure(str(error)) from error
    integration = _string(raw.get("integration"), field="smoke result.integration")
    if integration not in SUPPORTED_INTEGRATIONS:
        raise SmokeFailure(f"unsupported smoke result integration: {integration!r}")
    fields = {
        "schema",
        "format",
        "status",
        "binding",
        "integration",
        "build_type",
        "glibc",
        "artifacts",
        "evidence",
    }
    if integration == "conan":
        fields.update({"conan_home", "build_profile"})
    data = _strict_object(raw, required=fields, context="smoke result")
    if data["schema"] != _SMOKE_RESULT_SCHEMA:
        raise SmokeFailure(f"unsupported smoke result schema: {data['schema']!r}")
    if (
        not isinstance(data["format"], int)
        or isinstance(data["format"], bool)
        or data["format"] != _SMOKE_RESULT_FORMAT
    ):
        raise SmokeFailure(f"unsupported smoke result format: {data['format']!r}")
    if data["status"] != "passed":
        raise SmokeFailure(f"smoke result status is not passed: {data['status']!r}")
    binding = Path(_string(data["binding"], field="smoke result.binding"))
    if not binding.is_absolute():
        raise SmokeFailure("smoke result.binding must be an absolute path")
    _string(data["build_type"], field="smoke result.build_type")
    glibc = _strict_object(
        data["glibc"],
        required={"policy_floor", "observed_maximum"},
        context="smoke result.glibc",
    )
    _string(glibc["policy_floor"], field="smoke result.glibc.policy_floor")
    observed = glibc["observed_maximum"]
    if observed is not None:
        _string(observed, field="smoke result.glibc.observed_maximum")
    artifacts = data["artifacts"]
    if not isinstance(artifacts, list) or not all(
        isinstance(item, str) and bool(item) for item in artifacts
    ):
        raise SmokeFailure("smoke result.artifacts must be a string list")
    evidence = data["evidence"]
    if not isinstance(evidence, list) or not all(
        isinstance(item, str)
        and bool(item)
        and Path(item).name == item
        and item not in {".", ".."}
        for item in evidence
    ):
        raise SmokeFailure("smoke result.evidence must be a filename list")
    if integration == "conan":
        conan_home = Path(_string(data["conan_home"], field="smoke result.conan_home"))
        if not conan_home.is_absolute():
            raise SmokeFailure("smoke result.conan_home must be an absolute path")
        _string(data["build_profile"], field="smoke result.build_profile")
    return data


def _unique_existing_directories(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        if not path.is_dir():
            continue
        resolved = path.resolve()
        if resolved not in result:
            result.append(resolved)
    return tuple(result)


def _pinned_runtime(manifest: Mapping[str, object]) -> Mapping[str, object]:
    runtime = _mapping(manifest.get("cxx_runtime"), field="cxx_runtime")
    runtime_policy = runtime.get("policy")
    if runtime_policy not in {"pinned-gcc-runtime", "pinned-llvm-runtime"}:
        raise SmokeFailure(
            "loader-closure smoke requires a pinned compiler runtime; "
            "recreate the binding with --runtime "
            f"(current policy: {runtime_policy!r})"
        )
    return runtime


def require_pinned_runtime(binding: Path) -> None:
    _pinned_runtime(_load_object(binding / "binding.json"))


def _require_integration(
    manifest: Mapping[str, object],
    requested: str,
    *,
    build_type: str | None = None,
) -> None:
    if (
        manifest.get("schema") != BINDING_SCHEMA
        or manifest.get("format") != BINDING_FORMAT
    ):
        raise SmokeFailure("smoke requires the supported binding manifest schema")
    if requested not in SUPPORTED_INTEGRATIONS:
        raise SmokeFailure(f"unsupported integration: {requested!r}")
    name = requested
    integrations = _mapping(manifest.get("integrations"), field="integrations")
    entry = _mapping(integrations.get(name), field=f"integrations.{name}")
    expected = {
        "cmake": {"toolchain": "cmake/toolchain.cmake"},
        "shell": {"environment": "env/toolchain.env"},
        "conan": {
            "host_profile": "conan/host.profile",
            "cmake_toolchain": "conan/cmake-toolchain.cmake",
            "cmake_late": "conan/cmake-late.cmake",
        },
    }[name]
    for field, path in expected.items():
        actual = _string(entry.get(field), field=f"integrations.{name}.{field}")
        if actual != path:
            raise SmokeFailure(
                f"binding integration path mismatch for {name}.{field}: "
                f"{actual!r} != {path!r}"
            )
    if name == "conan" and build_type is not None:
        settings = _mapping(entry.get("settings"), field="integrations.conan.settings")
        profile_build_type = _string(
            settings.get("build_type"),
            field="integrations.conan.settings.build_type",
        )
        if profile_build_type != build_type:
            raise SmokeFailure(
                "smoke build type does not match the Conan host profile: "
                f"{build_type!r} != {profile_build_type!r}"
            )


def conan_settings_command(*, conan_home: Path, force: bool) -> tuple[str, ...]:
    command = [
        sys.executable,
        "-m",
        "linux_toolchain",
        "conan",
        "settings",
        "--output",
        str(conan_home / "settings_user.yml"),
    ]
    if force:
        command.append("--force")
    return tuple(command)


def load_runtime_context(binding: Path, artifacts: Path) -> RuntimeContext:
    manifest = _load_object(binding / "binding.json")
    policy = binding / "audit-policy.json"
    policy_data = _load_object(policy)
    target_arch = _string(policy_data.get("machine"), field="audit policy.machine")
    limits = _mapping(
        policy_data.get("max_required_versions"),
        field="audit policy.max_required_versions",
    )
    glibc_version = _string(limits.get("GLIBC"), field="audit policy.GLIBC")
    interpreters = policy_data.get("allowed_interpreters")
    if not isinstance(interpreters, list) or not all(
        isinstance(item, str) and item.startswith("/") for item in interpreters
    ):
        raise SmokeFailure(f"audit policy has invalid interpreters: {policy}")

    sdk = _mapping(manifest.get("sdk"), field="sdk")
    sdk_root = Path(_string(sdk.get("path"), field="sdk.path")).resolve()
    sysroot = sdk_root / "sysroot"
    if not sysroot.is_dir():
        raise SmokeFailure(f"binding SDK sysroot does not exist: {sysroot}")
    loader_candidates = [sysroot / item.lstrip("/") for item in interpreters]
    loader = next(
        (item.resolve() for item in loader_candidates if item.is_file()), None
    )
    if loader is None:
        raise SmokeFailure("none of the binding's allowed dynamic loaders exists")

    library_dirs: list[Path] = [artifacts, loader.parent]
    glibc_binding = _mapping(manifest.get("glibc_binding"), field="glibc_binding")
    declared_glibc_dirs = glibc_binding.get("library_dirs")
    if isinstance(declared_glibc_dirs, list):
        library_dirs.extend(
            Path(item) for item in declared_glibc_dirs if isinstance(item, str)
        )

    allowed_roots = [artifacts.resolve(), sysroot.resolve()]
    cxx_runtime = _pinned_runtime(manifest)
    runtime_path = cxx_runtime.get("path")
    locations = cxx_runtime.get("locations")
    if isinstance(runtime_path, str) and isinstance(locations, dict):
        runtime_root = Path(runtime_path).resolve()
        if runtime_root.is_dir():
            allowed_roots.append(runtime_root)
            runtime_dirs = locations.get("library_dirs")
            if isinstance(runtime_dirs, list):
                library_dirs.extend(
                    runtime_root / item
                    for item in runtime_dirs
                    if isinstance(item, str)
                )

    resolved_library_dirs = _unique_existing_directories(library_dirs)
    if artifacts.resolve() not in resolved_library_dirs:
        raise SmokeFailure(f"artifact directory does not exist: {artifacts}")
    return RuntimeContext(
        loader=loader,
        interpreter=Path(interpreters[0]),
        target_arch=target_arch,
        glibc_version=glibc_version,
        library_dirs=resolved_library_dirs,
        allowed_roots=tuple(allowed_roots),
    )


def _requires_kernel_loader_start(context: RuntimeContext) -> bool:
    # Older AArch64 glibc loaders corrupt the initial stack when ld.so is
    # invoked as a program and then transfers control to another executable
    # (glibc BZ #23293).  Let the kernel enter the SDK loader through PT_INTERP
    # in an isolated mount namespace for those SDKs.
    return context.target_arch == "aarch64" and AbiVersion.parse(
        context.glibc_version
    ) < AbiVersion.parse("2.36")


def _kernel_loader_command(
    context: RuntimeContext, executable: Path, arguments: Sequence[Path]
) -> tuple[str, ...]:
    unshare = _resolve_program("unshare", name="unshare")
    return (
        unshare,
        "--user",
        "--map-root-user",
        "--mount",
        sys.executable,
        "-m",
        "linux_toolchain._runtime_runner",
        str(context.loader),
        str(context.interpreter),
        os.pathsep.join(str(item) for item in context.library_dirs),
        str(executable),
        *(str(argument) for argument in arguments),
    )


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def verify_loader_closure(output: str, allowed_roots: Sequence[Path]) -> None:
    escaped: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        if "not found" in line:
            raise SmokeFailure(f"dynamic-loader closure is incomplete: {line}")
        candidate = line.split("=>", 1)[1].strip() if "=>" in line else line
        candidate = candidate.split(" (", 1)[0].strip()
        if not candidate.startswith("/"):
            continue
        path = Path(candidate)
        if not path.exists():
            raise SmokeFailure(f"dynamic-loader result does not exist: {path}")
        resolved = path.resolve()
        if not _is_within(resolved, allowed_roots):
            escaped.append(str(resolved))
    if escaped:
        raise SmokeFailure(
            "dynamic-loader closure escaped the SDK, runtime, and smoke artifacts: "
            + ", ".join(sorted(set(escaped)))
        )


def _show(command: Sequence[str]) -> None:
    print(f"+ {shlex.join(command)}", file=sys.stderr, flush=True)


def _run(command: Sequence[str], *, env: Mapping[str, str]) -> None:
    _show(command)
    try:
        run_streaming(command, env=env)
    except ExternalToolError as error:
        raise SmokeFailure(str(error)) from error


def _capture(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    evidence: Path,
) -> str:
    _show(command)
    failure: ExternalToolError | None = None
    try:
        run_logged(command, evidence, env=env)
    except ExternalToolError as error:
        failure = error
    try:
        output = evidence.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        if failure is not None:
            raise SmokeFailure(str(failure)) from failure
        raise SmokeFailure(f"cannot read smoke evidence {evidence}: {error}") from error
    if output:
        print(output, end="", file=sys.stderr)
    if failure is not None:
        raise SmokeFailure(str(failure)) from failure
    return output


def _glibc_audit_summary(report: Mapping[str, object]) -> dict[str, object]:
    policy = _mapping(report.get("policy"), field="audit.policy")
    limits = _mapping(
        policy.get("max_required_versions"),
        field="audit.policy.max_required_versions",
    )
    floor = _string(limits.get("GLIBC"), field="audit.policy.GLIBC")
    versions: list[tuple[tuple[int, ...], str]] = []
    files = report.get("files")
    if not isinstance(files, list):
        raise SmokeFailure("audit report files field is not an array")
    for file_report in files:
        metadata = _mapping(file_report, field="audit.files[]")
        needs = metadata.get("version_needs")
        if not isinstance(needs, list):
            raise SmokeFailure("audit version_needs field is not an array")
        for need in needs:
            item = _mapping(need, field="audit.version_needs[]")
            name = item.get("name")
            if not isinstance(name, str) or not name.startswith("GLIBC_"):
                continue
            numeric = name.removeprefix("GLIBC_")
            parts = numeric.split(".")
            if all(part.isdigit() for part in parts):
                versions.append((tuple(int(part) for part in parts), numeric))
    observed = max(versions)[1] if versions else None
    return {"policy_floor": floor, "observed_maximum": observed}


def run(args: argparse.Namespace) -> int:
    """Build, audit, and run the packaged smoke project."""

    validate_arguments(args)
    binding = args.binding.expanduser().resolve()
    raw_build_dir = args.build_dir.expanduser()
    if raw_build_dir.is_symlink():
        raise SmokeFailure(f"build directory cannot be a symlink: {raw_build_dir}")
    build_dir = raw_build_dir.resolve()
    conan_home: Path | None = None
    explicit_conan_home = args.conan_home is not None
    if args.integration == "conan":
        if explicit_conan_home:
            assert args.conan_home is not None
            raw_conan_home = args.conan_home.expanduser()
            if raw_conan_home.is_symlink():
                raise SmokeFailure(f"Conan home cannot be a symlink: {raw_conan_home}")
            conan_home = raw_conan_home.resolve()
            if _is_within(conan_home, (build_dir,)) or _is_within(
                build_dir, (conan_home,)
            ):
                raise SmokeFailure(
                    "an explicit Conan home must not overlap the smoke build "
                    f"directory: {conan_home} and {build_dir}"
                )
        else:
            conan_home = build_dir / "conan-home"

    required_files = [binding / "binding.json", binding / "audit-policy.json"]
    for required in required_files:
        if not required.is_file():
            raise SmokeFailure(f"binding file does not exist: {required}")
    binding_manifest = _load_object(binding / "binding.json")
    _require_integration(
        binding_manifest,
        args.integration,
        build_type=args.build_type,
    )
    _pinned_runtime(binding_manifest)

    integration_files: list[Path] = []
    if args.integration == "cmake":
        integration_files.append(binding / "cmake" / "toolchain.cmake")
    if args.integration == "conan":
        integration_files.extend(
            (
                binding / "conan" / "host.profile",
                binding / "conan" / "cmake-toolchain.cmake",
                binding / "conan" / "cmake-late.cmake",
            )
        )
    if args.integration == "shell":
        integration_files.append(binding / "env" / "toolchain.env")
    for required in integration_files:
        if not required.is_file():
            raise SmokeFailure(f"binding file does not exist: {required}")
    build_type = args.build_type

    cmake = (
        _resolve_program(args.cmake, name="CMake")
        if args.integration in {"cmake", "conan"}
        else args.cmake
    )
    make = _resolve_program(args.make, name="Make")
    conan = (
        _resolve_program(args.conan, name="Conan")
        if args.integration == "conan"
        else args.conan
    )
    runner = (
        _resolve_program(args.runner, name="target runner") if args.runner else None
    )
    _prepare_build_directory(build_dir)

    environment = os.environ.copy()
    build_profile: str | None = None
    if args.integration == "conan":
        assert conan_home is not None
        conan_home.mkdir(parents=True, exist_ok=True)
        environment["CONAN_HOME"] = str(conan_home)
        _run(
            conan_settings_command(
                conan_home=conan_home,
                force=not explicit_conan_home,
            ),
            env=environment,
        )
        if args.build_profile is None:
            build_profile = "smoke-build"
            _run(
                (
                    conan,
                    "profile",
                    "detect",
                    "--name",
                    build_profile,
                    "--force",
                ),
                env=environment,
            )
        else:
            build_profile = _profile_argument(args.build_profile)
    commands = build_commands(
        source=PROJECT.resolve(),
        binding=binding,
        build_profile=build_profile,
        build_dir=build_dir,
        conan=conan,
        cmake=cmake,
        make=make,
        build_type=build_type,
        jobs=args.jobs,
        integration=args.integration,
    )
    if commands.conan_install is not None:
        _run(commands.conan_install, env=environment)
        toolchain = build_dir / "conan" / "conan_toolchain.cmake"
        if not toolchain.is_file():
            raise SmokeFailure(
                f"Conan did not generate its CMake toolchain: {toolchain}"
            )
    if commands.configure is not None:
        _run(commands.configure, env=environment)
    _run(commands.build, env=environment)

    artifacts = build_dir / "cmake" / "artifacts"
    executable = artifacts / "linux_toolchain_smoke"
    library = artifacts / "liblinux_toolchain_smoke.so"
    for artifact in (executable, library):
        if not artifact.is_file():
            raise SmokeFailure(f"expected smoke artifact was not built: {artifact}")

    audit_environment = environment.copy()
    audit_report = _capture(
        (
            sys.executable,
            "-m",
            "linux_toolchain",
            "audit",
            "--json",
            "--policy",
            str(binding / "audit-policy.json"),
            str(executable),
            str(library),
        ),
        env=audit_environment,
        evidence=build_dir / "audit-report.json",
    )
    try:
        audit_data = json.loads(audit_report)
    except json.JSONDecodeError as error:
        raise SmokeFailure("linux-toolchain audit did not produce JSON") from error
    if not isinstance(audit_data, dict):
        raise SmokeFailure("linux-toolchain audit JSON root is not an object")
    audit_summary = _glibc_audit_summary(audit_data)

    context = load_runtime_context(binding, artifacts)
    loader_prefix = (() if runner is None else (runner,)) + (
        str(context.loader),
        "--inhibit-cache",
        "--library-path",
        os.pathsep.join(str(item) for item in context.library_dirs),
    )
    runtime_environment = environment.copy()
    for variable in ("LD_AUDIT", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        runtime_environment.pop(variable, None)
    runtime_environment["LD_BIND_NOW"] = "1"
    closure = _capture(
        (*loader_prefix, "--list", str(executable)),
        env=runtime_environment,
        evidence=build_dir / "loader-closure.txt",
    )
    verify_loader_closure(closure, context.allowed_roots)
    runtime_command = (*loader_prefix, str(executable), str(library))
    if runner is None and _requires_kernel_loader_start(context):
        runtime_command = _kernel_loader_command(context, executable, (library,))
    runtime_output = _capture(
        runtime_command,
        env=runtime_environment,
        evidence=build_dir / "runtime-output.txt",
    )
    if "linux-toolchain-smoke: ok" not in runtime_output:
        raise SmokeFailure("smoke executable did not report success")

    result = {
        "schema": _SMOKE_RESULT_SCHEMA,
        "format": _SMOKE_RESULT_FORMAT,
        "status": "passed",
        "binding": str(binding),
        "integration": args.integration,
        "build_type": build_type,
        "glibc": audit_summary,
        "artifacts": [str(executable), str(library)],
        "evidence": list(_EVIDENCE),
    }
    if args.integration == "conan":
        result["build_profile"] = build_profile
        result["conan_home"] = str(conan_home)
    try:
        write_json_atomic(build_dir / "result.json", result)
    except LinuxToolchainError as error:
        raise SmokeFailure(str(error)) from error
    print(build_dir / "result.json")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the smoke CLI using the package-wide exit-status rules."""

    try:
        return run(parse_args(argv))
    except (OSError, SmokeFailure) as error:
        print(f"linux-toolchain: error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("linux-toolchain: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
