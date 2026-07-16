from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Sequence

from linux_toolchain.bundle import (
    create_bundle,
    create_setup_bundle,
)
from linux_toolchain.cli_parser import build_parser
from linux_toolchain.compiler.binding import create_binding
from linux_toolchain.compiler.managed_binding import create_managed_binding
from linux_toolchain.compiler.toolchain import detect_compiler
from linux_toolchain.conan.settings import write_settings_user
from linux_toolchain.diagnostics import run_diagnostics
from linux_toolchain.errors import ConfigurationError, LinuxToolchainError
from linux_toolchain.integrations import (
    DEFAULT_INTEGRATIONS,
    SUPPORTED_INTEGRATIONS,
    ConanSettings,
    IntegrationName,
)
from linux_toolchain.managed import (
    ManagedLock,
    ManagedSpec,
    available_releases,
    resolve_lock,
    write_lockfile,
)
from linux_toolchain.managed.assemble import assemble_variant
from linux_toolchain.managed.builder import (
    build_with_docker as build_managed_with_docker,
)
from linux_toolchain.managed.builder import (
    fetch_source as fetch_managed_source,
)
from linux_toolchain.managed.builder import (
    render_workspace as render_managed_workspace,
)
from linux_toolchain.managed.publication import publish_managed_runtime
from linux_toolchain.models import SdkSpec
from linux_toolchain.recipes import (
    apply_recipe_overrides,
    available_recipes,
    get_recipe,
)
from linux_toolchain.sdk.crosstool_ng import (
    build_with_docker,
    export_sdk,
    load_workspace,
    render_workspace,
)
from linux_toolchain.setup import create_prepared_bundle, setup_toolchain
from linux_toolchain.smoke import run as run_smoke
from linux_toolchain.terminal import (
    BOLD,
    CYAN,
    RED,
    YELLOW,
    TerminalProgressBar,
    TerminalProgressDisplay,
    progress_line,
    style,
    supports_color,
)

_CLI_JSON_FORMAT = 1
_PROGRESS_DISPLAY: TerminalProgressDisplay | None = None


def _print_table(
    rows: Sequence[dict[str, object]], columns: Sequence[tuple[str, str]]
) -> None:
    widths = [
        max(len(header), *(len(str(row[key])) for row in rows))
        for header, key in columns
    ]
    last = len(columns) - 1
    print(
        "  ".join(
            header if index == last else header.ljust(width)
            for index, ((header, _), width) in enumerate(
                zip(columns, widths, strict=True)
            )
        )
    )
    for row in rows:
        print(
            "  ".join(
                str(row[key]) if index == last else str(row[key]).ljust(width)
                for index, ((_, key), width) in enumerate(
                    zip(columns, widths, strict=True)
                )
            )
        )


def _print_json_document(schema: str, **payload: object) -> None:
    document = {"schema": schema, "format": _CLI_JSON_FORMAT}
    document.update(payload)
    print(json.dumps(document, indent=2, sort_keys=True))


def _print_progress(message: str) -> None:
    global _PROGRESS_DISPLAY
    if _PROGRESS_DISPLAY is None or not _PROGRESS_DISPLAY.uses(sys.stderr):
        _PROGRESS_DISPLAY = TerminalProgressDisplay(sys.stderr)
    live_message = _compact_builder_heartbeat(message)
    _PROGRESS_DISPLAY.write(
        message,
        replace=live_message is not None,
        live_message=live_message,
    )


def _compact_builder_heartbeat(message: str) -> str | None:
    prefix, marker, detail = message.partition("builder: compiling ")
    if not marker:
        return None
    heartbeat, _, output = detail.partition("\n")
    _, marker, elapsed = heartbeat.partition("; elapsed: ")
    if not marker:
        return None
    live_message = f"{prefix}builder: compiling; elapsed: {elapsed.partition(';')[0]}"
    return "\n".join((live_message, output)) if output else live_message


def _finish_progress() -> None:
    if _PROGRESS_DISPLAY is not None and _PROGRESS_DISPLAY.uses(sys.stderr):
        _PROGRESS_DISPLAY.finish()


def _print_warning(message: str) -> None:
    _finish_progress()
    color = supports_color(sys.stderr)
    print(
        f"{style('warning:', BOLD, YELLOW, enabled=color)} {message}",
        file=sys.stderr,
    )


def _print_error(message: object) -> None:
    _finish_progress()
    color = supports_color(sys.stderr)
    prefix = style("linux-toolchain: error:", BOLD, RED, enabled=color)
    print(f"{prefix} {message}", file=sys.stderr)


def _doctor(args: argparse.Namespace) -> int:
    report = run_diagnostics(args.workflow, args.integration)
    color = supports_color(sys.stdout)
    if args.json:
        print(report.to_json())
    elif args.summary and report.passed:
        print(progress_line("doctor: PASS", color=color))
    else:
        print(report.to_text(color=color))
    return 0 if report.passed else 1


def _path_command(launcher: Path) -> str:
    return f'export PATH={shlex.quote(str(launcher.parent))}:"$PATH"'


def _print_path_instructions(launcher: Path) -> None:
    command = _path_command(launcher)
    quoted_command = shlex.quote(command)
    color = supports_color(sys.stderr)
    heading = style("Add launcher to PATH:", BOLD, CYAN, enabled=color)
    print(heading, file=sys.stderr)
    current = style("Current shell:", BOLD, enabled=color)
    print(f"  {current}", file=sys.stderr)
    print(f"    {command}", file=sys.stderr)
    for shell, startup_file in (("Bash", ".bashrc"), ("Zsh", ".zshrc")):
        quoted_file = f'"$HOME/{startup_file}"'
        label = style(f"{shell} (~/{startup_file}):", BOLD, enabled=color)
        print(f"  {label}", file=sys.stderr)
        print(
            f"    printf '\\n%s\\n' {quoted_command} >> {quoted_file}",
            file=sys.stderr,
        )


def _setup_toolchain(args: argparse.Namespace) -> int:
    progress_bar = TerminalProgressBar(sys.stderr)
    try:
        launcher = setup_toolchain(
            args.compiler,
            prefix=args.prefix,
            work_dir=args.work_dir,
            store_dir=args.store_dir,
            arch=args.arch,
            glibc_floor=args.glibc,
            integration=args.integration,
            runtime=args.runtime,
            host_glibc_floor=args.host_glibc_floor,
            jobs=args.jobs,
            runner=args.runner,
            conan_cppstd=args.conan_cppstd,
            conan_build_type=args.conan_build_type,
            conan_build_profile=args.conan_build_profile,
            install=not args.prepare_only,
            force=args.force,
            progress=_print_progress,
            source_progress=progress_bar.update,
        )
    finally:
        progress_bar.close()
    if not args.prepare_only and not args.no_path_instructions:
        _print_path_instructions(launcher)
    print(launcher)
    return 0


def _selected_integrations(
    args: argparse.Namespace,
    *,
    default: tuple[IntegrationName, ...] = DEFAULT_INTEGRATIONS,
) -> tuple[IntegrationName, ...]:
    selected = tuple(args.integration or default)
    duplicates = sorted({name for name in selected if selected.count(name) > 1})
    if duplicates:
        raise ConfigurationError(
            "duplicate integration selection: " + ", ".join(duplicates)
        )
    return selected


def _conan_settings(
    args: argparse.Namespace,
    integrations: tuple[IntegrationName, ...],
) -> ConanSettings | None:
    values = (
        args.conan_cppstd,
        getattr(args, "conan_libcxx", None),
        args.conan_build_type,
    )
    if "conan" not in integrations:
        if any(value is not None for value in values):
            raise ConfigurationError("--conan-* options require --integration conan")
        return None

    return ConanSettings(
        cppstd=args.conan_cppstd,
        libcxx=getattr(args, "conan_libcxx", None),
        build_type=args.conan_build_type or "Release",
    )


def _sdk_spec(args: argparse.Namespace) -> SdkSpec:
    if args.spec is not None:
        if args.arch is not None:
            raise ConfigurationError("--arch can only be used together with --glibc")
        spec = SdkSpec.load(args.spec.expanduser().resolve())
        return apply_recipe_overrides(
            spec,
            name=args.name,
            minimum_kernel=args.minimum_kernel,
        )
    if args.arch is None:
        raise ConfigurationError("--arch is required when using --glibc")
    return get_recipe(args.arch, args.glibc).to_spec(
        name=args.name,
        minimum_kernel=args.minimum_kernel,
    )


def _render_sdk(args: argparse.Namespace) -> int:
    spec = _sdk_spec(args)
    manifest = render_workspace(
        spec,
        args.workspace.expanduser(),
        force=args.force,
    )
    print(manifest)
    return 0


def _list_sdks(args: argparse.Namespace) -> int:
    recipes = tuple(
        recipe
        for recipe in available_recipes()
        if args.arch is None or recipe.arch == args.arch
    )
    rows = [
        {
            "arch": recipe.arch,
            "glibc": recipe.glibc_version,
            "family": recipe.family,
            "crosstool-ng": recipe.builder_version,
            "linux_headers": recipe.linux_headers,
            "gcc": recipe.gcc,
            "binutils": recipe.binutils,
            "minimum_kernel": recipe.minimum_kernel,
        }
        for recipe in recipes
    ]
    if args.json:
        _print_json_document("linux-toolchain-sdk-catalog", recipes=rows)
        return 0

    columns = (
        ("ARCH", "arch"),
        ("GLIBC", "glibc"),
        ("FAMILY", "family"),
        ("CROSSTOOL-NG", "crosstool-ng"),
        ("LINUX_HEADERS", "linux_headers"),
        ("GCC", "gcc"),
        ("BINUTILS", "binutils"),
        ("MIN_KERNEL", "minimum_kernel"),
    )
    _print_table(rows, columns)
    return 0


def _build_sdk(args: argparse.Namespace) -> int:
    workspace = args.workspace.expanduser().resolve()
    dockerfile = args.dockerfile.expanduser().resolve()
    if not dockerfile.is_file():
        raise ConfigurationError(
            f"crosstool-NG builder Dockerfile does not exist: {dockerfile}"
        )
    spec = load_workspace(workspace)
    build_with_docker(
        spec,
        workspace,
        dockerfile=dockerfile,
        image=args.image,
        jobs=args.jobs,
        progress=_print_progress,
    )
    print(export_sdk(spec, workspace))
    return 0


def _create_sdk(args: argparse.Namespace) -> int:
    workspace = args.workspace.expanduser().resolve()
    dockerfile = args.dockerfile.expanduser().resolve()
    if not dockerfile.is_file():
        raise ConfigurationError(
            f"crosstool-NG builder Dockerfile does not exist: {dockerfile}"
        )
    spec = _sdk_spec(args)
    _print_progress("sdk: rendering pinned workspace")
    render_workspace(spec, workspace, force=args.force)
    build_with_docker(
        spec,
        workspace,
        dockerfile=dockerfile,
        image=args.image,
        jobs=args.jobs,
        progress=_print_progress,
    )
    print(export_sdk(spec, workspace))
    return 0


def _create_binding(args: argparse.Namespace) -> int:
    if args.allow_unpinned_runtime:
        _print_warning(
            "creating a development binding without a pinned libstdc++/libgcc runtime"
        )
    compiler = detect_compiler(args.cc, args.cxx)
    integrations = _selected_integrations(args)
    manifest = create_binding(
        args.sdk,
        args.output,
        compiler,
        runtime=args.runtime,
        integrations=integrations,
        conan=_conan_settings(args, integrations),
        force=args.force,
    )
    print(manifest)
    return 0


def _create_managed_binding(args: argparse.Namespace) -> int:
    lock = _load_managed_lock(args.lock)
    integrations = _selected_integrations(args)
    manifest = create_managed_binding(
        args.sdk,
        args.output,
        args.compiler_kit,
        lock=lock,
        variant=args.variant,
        runtime=args.runtime,
        integrations=integrations,
        conan=_conan_settings(args, integrations),
        force=args.force,
    )
    print(manifest)
    return 0


def _create_artifact_bundle(args: argparse.Namespace) -> int:
    integrations = _selected_integrations(
        args,
        default=SUPPORTED_INTEGRATIONS,
    )
    progress_bar = TerminalProgressBar(sys.stderr)
    try:
        result = create_bundle(
            sdk=args.sdk,
            compiler_kit=args.compiler_kit,
            runtime=args.runtime,
            lock=_load_managed_lock(args.lock),
            variant=args.variant,
            output=args.output,
            bundle_id=args.id,
            integrations=integrations,
            conan=_conan_settings(args, integrations),
            force=args.force,
            progress=_print_progress,
            archive_progress=progress_bar.update,
        )
    finally:
        progress_bar.close()
    print(result)
    return 0


def _create_setup_bundle(args: argparse.Namespace) -> int:
    if args.prefix is not None and args.state_directory is not None:
        raise ConfigurationError("--state-directory requires --config")
    progress_bar = TerminalProgressBar(sys.stderr)
    try:
        if args.prefix is not None:
            result = create_setup_bundle(
                prefix=args.prefix,
                output=args.output,
                bundle_id=args.id,
                force=args.force,
                progress=_print_progress,
                archive_progress=progress_bar.update,
            )
        else:
            result = create_prepared_bundle(
                config=args.config,
                state_directory=args.state_directory,
                output=args.output,
                bundle_id=args.id,
                force=args.force,
                progress=_print_progress,
                archive_progress=progress_bar.update,
            )
    finally:
        progress_bar.close()
    print(result)
    return 0


def _assemble_managed(args: argparse.Namespace) -> int:
    dockerfile = (
        args.dockerfile.expanduser().resolve() if args.dockerfile is not None else None
    )
    integrations = _selected_integrations(args)
    progress_bar = TerminalProgressBar(sys.stderr)
    try:
        result = assemble_variant(
            _load_managed_lock(args.lock),
            args.variant,
            args.sdk_workspace,
            args.compiler_backend_workspace,
            args.workspace,
            args.output,
            jobs=args.jobs,
            integrations=integrations,
            conan=_conan_settings(args, integrations),
            dockerfile=dockerfile,
            image=args.image,
            rebuild=args.rebuild,
            force=args.force,
            progress=lambda message: _print_progress(f"managed: {message}"),
            source_progress=progress_bar.update,
        )
    finally:
        progress_bar.close()
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(result.binding_manifest)
    return 0


def _smoke(args: argparse.Namespace) -> int:
    return int(run_smoke(args))


def _import_gcc_runtime(args: argparse.Namespace) -> int:
    from linux_toolchain.runtime import import_gcc_runtime

    print(
        import_gcc_runtime(
            args.prefix,
            args.glibc_floor,
            args.arch,
            args.output,
            licenses=args.licenses,
            force=args.force,
            probe_gxx=args.probe_gxx,
        )
    )
    return 0


def _import_llvm_runtime(args: argparse.Namespace) -> int:
    from linux_toolchain.managed.publication import (
        _load_managed_llvm_source_evidence,
    )
    from linux_toolchain.runtime import import_llvm_runtime

    source_evidence = (
        _load_managed_llvm_source_evidence(
            args.provenance,
            args.prefix,
            version=args.llvm_version,
            glibc_floor=args.glibc_floor,
            arch=args.arch,
            target=args.target,
        )
        if args.provenance is not None
        else None
    )
    print(
        import_llvm_runtime(
            args.prefix,
            args.llvm_version,
            args.glibc_floor,
            args.arch,
            args.target,
            args.output,
            licenses=args.licenses,
            source_evidence=source_evidence,
            probe_clang=args.probe_clang,
            force=args.force,
        )
    )
    return 0


def _list_managed_catalog(args: argparse.Namespace) -> int:
    releases = available_releases(args.family)
    rows = [
        {
            "family": release.family,
            "major": release.major,
            "version": release.version,
            "source": release.source_kind,
        }
        for release in releases
    ]
    if args.json:
        _print_json_document(
            "linux-toolchain-managed-release-index",
            releases=rows,
        )
        return 0

    columns = (
        ("FAMILY", "family"),
        ("MAJOR", "major"),
        ("VERSION", "version"),
        ("SOURCE", "source"),
    )
    _print_table(rows, columns)
    return 0


def _lock_managed(args: argparse.Namespace) -> int:
    spec = ManagedSpec.load(args.spec)
    lock = resolve_lock(spec)
    print(write_lockfile(lock, args.output, force=args.force))
    return 0


def _load_managed_lock(path: Path) -> ManagedLock:
    return ManagedLock.load(path.expanduser().resolve())


def _list_managed_artifacts(args: argparse.Namespace) -> int:
    lock = _load_managed_lock(args.lock)
    if args.json:
        _print_json_document(
            "linux-toolchain-managed-lock-artifacts",
            compiler_kits=[entry.to_dict() for entry in lock.compiler_kits],
            runtimes=[entry.to_dict() for entry in lock.runtimes],
            variants=[entry.to_dict() for entry in lock.variants],
        )
        return 0

    sections: tuple[
        tuple[str, list[dict[str, object]], tuple[tuple[str, str], ...]], ...
    ] = (
        (
            "COMPILER KITS",
            [
                {
                    "id": entry.id,
                    "family": entry.family,
                    "version": entry.version,
                    "target": entry.target_arch,
                    "host_glibc": entry.host.glibc_floor,
                }
                for entry in lock.compiler_kits
            ],
            (
                ("ID", "id"),
                ("FAMILY", "family"),
                ("VERSION", "version"),
                ("TARGET", "target"),
                ("HOST_GLIBC", "host_glibc"),
            ),
        ),
        (
            "RUNTIMES",
            [
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "provider": (f"{entry.provider_family}-{entry.provider_version}"),
                    "target": entry.target.arch,
                    "glibc": entry.target.glibc_floor,
                }
                for entry in lock.runtimes
            ],
            (
                ("ID", "id"),
                ("KIND", "kind"),
                ("PROVIDER", "provider"),
                ("TARGET", "target"),
                ("GLIBC", "glibc"),
            ),
        ),
        (
            "VARIANTS",
            [
                {
                    "id": entry.id,
                    "compiler": f"{entry.family}-{entry.version}",
                    "runtime": entry.cxx_runtime,
                    "target": entry.target.arch,
                    "glibc": entry.target.glibc_floor,
                }
                for entry in lock.variants
            ],
            (
                ("ID", "id"),
                ("COMPILER", "compiler"),
                ("CXX_RUNTIME", "runtime"),
                ("TARGET", "target"),
                ("GLIBC", "glibc"),
            ),
        ),
    )
    for index, (title, rows, columns) in enumerate(sections):
        if index:
            print()
        print(title)
        _print_table(rows, columns)
    return 0


def _render_managed(args: argparse.Namespace) -> int:
    lock = _load_managed_lock(args.lock)
    print(
        render_managed_workspace(
            lock,
            args.artifact,
            args.workspace.expanduser().resolve(),
            sdk=args.sdk.expanduser().resolve(),
            target_tools=args.target_tools.expanduser().resolve(),
            compiler_backend=args.compiler_backend_workspace.expanduser().resolve(),
            force=args.force,
        )
    )
    return 0


def _fetch_managed(args: argparse.Namespace) -> int:
    lock = _load_managed_lock(args.lock)
    progress_bar = TerminalProgressBar(sys.stderr)
    try:
        result = fetch_managed_source(
            lock,
            args.artifact,
            args.workspace.expanduser().resolve(),
            progress=lambda message: _print_progress(f"managed: {message}"),
            transfer_progress=progress_bar.update,
        )
    finally:
        progress_bar.close()
    print(result)
    return 0


def _build_managed(args: argparse.Namespace) -> int:
    lock = _load_managed_lock(args.lock)
    dockerfile = (
        args.dockerfile.expanduser().resolve() if args.dockerfile is not None else None
    )
    progress_bar = TerminalProgressBar(sys.stderr)
    try:
        result = build_managed_with_docker(
            lock,
            args.artifact,
            args.workspace.expanduser().resolve(),
            dockerfile=dockerfile,
            image=args.image,
            jobs=args.jobs,
            progress=_print_progress,
            source_progress=progress_bar.update,
        )
    finally:
        progress_bar.close()
    print(result)
    return 0


def _publish_managed_runtime(args: argparse.Namespace) -> int:
    lock = _load_managed_lock(args.lock)
    print(
        publish_managed_runtime(
            lock,
            args.artifact,
            args.artifact_dir.expanduser().resolve(),
            args.output.expanduser().resolve(),
            force=args.force,
        )
    )
    return 0


def _write_conan_settings(args: argparse.Namespace) -> int:
    print(
        write_settings_user(
            args.output.expanduser().resolve(),
            force=args.force,
        )
    )
    return 0


def _audit(args: argparse.Namespace) -> int:
    # Imported lazily so configuration-only commands remain usable on systems
    # where readelf is not installed.
    from linux_toolchain.elf.audit import audit_paths
    from linux_toolchain.elf.models import load_policy

    policy = load_policy(args.policy.expanduser().resolve())
    report = audit_paths(args.paths, policy, recursive=args.recursive)
    if args.json:
        print(report.to_json())
    else:
        print(report.to_text())
    return 1 if report.has_violations else 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    try:
        handlers = {
            "setup_toolchain": _setup_toolchain,
            "doctor": _doctor,
            "list_sdks": _list_sdks,
            "render_sdk": _render_sdk,
            "create_sdk": _create_sdk,
            "build_sdk": _build_sdk,
            "create_binding": _create_binding,
            "create_managed_binding": _create_managed_binding,
            "create_setup_bundle": _create_setup_bundle,
            "create_artifact_bundle": _create_artifact_bundle,
            "import_gcc_runtime": _import_gcc_runtime,
            "import_llvm_runtime": _import_llvm_runtime,
            "list_managed_catalog": _list_managed_catalog,
            "lock_managed": _lock_managed,
            "list_managed_artifacts": _list_managed_artifacts,
            "assemble_managed": _assemble_managed,
            "render_managed": _render_managed,
            "fetch_managed": _fetch_managed,
            "build_managed": _build_managed,
            "publish_managed_runtime": _publish_managed_runtime,
            "write_conan_settings": _write_conan_settings,
            "smoke": _smoke,
            "audit": _audit,
        }
        return int(handlers[args.handler](args))
    except LinuxToolchainError as error:
        _print_error(error)
        print(
            "Try 'linux-toolchain --help' for more information.",
            file=sys.stderr,
        )
        return 2
    except OSError as error:
        # Filesystem failures are operational/configuration errors, not ABI
        # policy violations. Keep the documented exit-code behavior and do
        # not expose a Python traceback for read-only or inaccessible paths.
        _print_error(error)
        print(
            "Try 'linux-toolchain --help' for more information.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        _finish_progress()
        color = supports_color(sys.stderr)
        print(
            style("linux-toolchain: interrupted", BOLD, YELLOW, enabled=color),
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
