from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from linux_toolchain.compiler.runtime_binding import (
    GccRuntimeBinding,
    GccRuntimeLinkEvidence,
    LlvmRuntimeBinding,
    LlvmRuntimeLinkEvidence,
    RuntimeBinding,
    RuntimeLinkEvidence,
    _load_runtime_binding,
    _runtime_link_evidence,
)
from linux_toolchain.compiler.toolchain import (
    ArchiveTool,
    CompilerInfo,
    ExecutableIdentity,
    TargetTools,
    _capture_executable_identity,
    _ManagedCompilerToolchain,
    _resolve_compiler_target_tools,
    _resolve_compiler_tool,
    _target_arch,
    _target_tools,
)
from linux_toolchain.elf.compatibility import GLIBC_DT_RELR_MIN_VERSION
from linux_toolchain.elf.models import AuditPolicy
from linux_toolchain.elf.reader import ReadElfInspector
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.integrations import (
    DEFAULT_INTEGRATIONS,
    SUPPORTED_INTEGRATIONS,
    ConanIntegrationConfig,
    ConanSettings,
    IntegrationContext,
    IntegrationName,
    ShellIntegrationConfig,
    render_integrations,
)
from linux_toolchain.licenses import (
    require_license_files,
    sdk_required_license_paths,
    validate_license_evidence,
)
from linux_toolchain.models import SDK_MANIFEST_FORMAT, SDK_MANIFEST_SCHEMA, TargetSpec
from linux_toolchain.process import run
from linux_toolchain.publication import replace_directory
from linux_toolchain.runtime.llvm_models import llvm_runtime_component
from linux_toolchain.versions import AbiVersion

BINDING_SCHEMA = "linux-toolchain-binding"
BINDING_FORMAT = 1
_ARCHIVE_VALIDATION_CHECKS = (
    "target-object",
    "archive-create",
    "archive-index",
    "archive-member-machine",
    "archive-link",
)
_TARGET_TOOL_VALIDATION_CHECKS = (
    "assembler-target-machine",
    "nm-target-object",
    "objdump-target-object",
    "objcopy-target-machine",
    "strip-target-machine",
)
_LINK_VALIDATION_CHECKS = (
    "c-executable",
    "c-shared-library",
    "cxx-executable",
)
_RUNTIME_LINK_VALIDATION_CHECKS = (
    *_LINK_VALIDATION_CHECKS,
    "cxx-shared-exception",
    "c-static-executable",
    "cxx-static-exception",
)


@dataclass(frozen=True)
class _WrapperDriverFlags:
    """Fixed driver arguments split by the phases that consume them.

    ``always`` contains compiler/driver selection and header-policy arguments.
    ``link_only`` contains startup-file, linker, and runtime-library selection
    arguments that Clang diagnoses as unused for compile-only invocations.
    ``static_link_suffix`` contains static-runtime dependencies that must follow
    consumer inputs.
    """

    always: tuple[str, ...]
    link_only: tuple[str, ...]
    static_link_suffix: tuple[str, ...] = ()

    @property
    def link_invocation(self) -> tuple[str, ...]:
        return (*self.always, *self.link_only)


@dataclass(frozen=True)
class _BindingTools:
    cc: ExecutableIdentity
    cxx: ExecutableIdentity
    target_tools: TargetTools
    linker: ArchiveTool | None
    selected_target_tools: Mapping[str, ArchiveTool]
    selected_tools: Mapping[str, ArchiveTool]


@dataclass(frozen=True)
class _BindingIntegrationInputs:
    context: IntegrationContext
    shell: ShellIntegrationConfig
    conan: ConanIntegrationConfig | None


def _executable_identity_manifest(
    identity: ExecutableIdentity,
) -> dict[str, str]:
    return {
        "invocation_path": str(identity.invocation_path),
    }


def _compiler_tool_manifest(tool: ArchiveTool, wrapper: Path) -> dict[str, str]:
    return {
        "reported_program": tool.reported_program,
        "invocation_path": str(tool.invocation_path),
        "wrapper": str(wrapper),
    }


def _json_compatible(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def _single_line_value(value: str, context: str) -> str:
    if "\n" in value or "\r" in value:
        raise ConfigurationError(f"{context} cannot contain newlines")
    return value


def _wrapper_text(
    compiler: ExecutableIdentity,
    sysroot: Path,
    driver_flags: _WrapperDriverFlags,
    suffix_flags: tuple[str, ...],
) -> str:
    """Render a small driver wrapper that owns only fixed toolchain flags.

    Consumer arguments are ordinary compiler arguments and are forwarded
    unchanged.  Binding creation validates the resulting compiler, linker and
    runtime selection before publication.
    """

    compiler_arg = shlex.quote(str(compiler.invocation_path))
    sysroot_arg = shlex.quote(str(sysroot))
    compile_flags = " ".join(
        shlex.quote(flag) for flag in (*driver_flags.always, *suffix_flags)
    )
    link_flags = " ".join(
        shlex.quote(flag) for flag in (*driver_flags.link_invocation, *suffix_flags)
    )
    static_link_setup = ""
    static_link_case = ""
    static_link_exec = ""
    if driver_flags.static_link_suffix:
        static_link_flags = " ".join(
            shlex.quote(flag) for flag in driver_flags.static_link_suffix
        )
        static_link_setup = "static_link=0\n"
        static_link_case = "    -static|-static-pie|-static-libgcc) static_link=1 ;;\n"
        static_link_exec = f"""if [ "$static_link" -eq 1 ]; then
  exec {compiler_arg} {link_flags} "$@" {static_link_flags} \
--sysroot={sysroot_arg}
fi
"""
    return f"""#!/bin/sh
set -eu

unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_RUN_PATH \\
  LD_LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX CCC_OVERRIDE_OPTIONS

compile_only=0
{static_link_setup}\
for argument in "$@"; do
  case "$argument" in
    -c|-S|-E|-M|-MM|-fsyntax-only) compile_only=1 ;;
{static_link_case}\
  esac
done

if [ "$compile_only" -eq 1 ]; then
  exec {compiler_arg} {compile_flags} "$@" --sysroot={sysroot_arg}
fi
{static_link_exec}\
exec {compiler_arg} {link_flags} "$@" --sysroot={sysroot_arg}
"""


_GLIBC_STARTFILES = (
    "crt1.o",
    "Scrt1.o",
    "rcrt1.o",
    "gcrt1.o",
    "grcrt1.o",
    "Mcrt1.o",
    "crti.o",
    "crtn.o",
)


def _sdk_library_dirs(sysroot: Path) -> tuple[Path, ...]:
    input_names = {"libc.a", "libc.so", "libc.so.6", *_GLIBC_STARTFILES}
    directories: set[Path] = set()
    for path in sysroot.rglob("*"):
        if path.name in input_names and (path.is_file() or path.is_symlink()):
            directories.add(path.parent)
    preferred = ("usr/lib64", "lib64", "usr/lib", "lib")

    def sort_key(path: Path) -> tuple[int, str]:
        relative = path.relative_to(sysroot).as_posix()
        try:
            rank = preferred.index(relative)
        except ValueError:
            rank = len(preferred)
        return rank, relative

    result = tuple(sorted(directories, key=sort_key))
    if not result or not any((directory / "libc.so").exists() for directory in result):
        raise ConfigurationError("SDK has no usable libc.so linker directory")
    return result


def _create_startfile_overlay(
    overlay: Path,
    library_dirs: tuple[Path, ...],
) -> tuple[str, ...]:
    overlay.mkdir(parents=True, exist_ok=True)
    sources = _sdk_startfiles(library_dirs)
    for name, source in sources.items():
        (overlay / name).symlink_to(source)
    return tuple(sources)


def _sdk_startfiles(library_dirs: tuple[Path, ...]) -> dict[str, Path]:
    installed: dict[str, Path] = {}
    for name in _GLIBC_STARTFILES:
        source = next(
            (
                directory / name
                for directory in library_dirs
                if (directory / name).is_file()
            ),
            None,
        )
        if source is None:
            continue
        installed[name] = source
    required = {"crti.o", "crtn.o"}
    if not required.issubset(installed) or not {
        "crt1.o",
        "Scrt1.o",
        "rcrt1.o",
    }.intersection(installed):
        raise ConfigurationError(
            "SDK does not contain the required glibc startup objects"
        )
    return installed


def _isystem_flags(paths: tuple[Path, ...]) -> tuple[str, ...]:
    flags: list[str] = []
    for path in paths:
        flags.extend(("-isystem", str(path)))
    return tuple(flags)


def _runtime_wrapper_flags(
    *,
    compiler: CompilerInfo,
    runtime: RuntimeBinding,
    sysroot: Path,
    overlay: Path,
    tool_dir: Path,
    sdk_library_dirs: tuple[Path, ...],
    cxx: bool,
) -> _WrapperDriverFlags:
    runtime_target = runtime.manifest.target
    if not isinstance(runtime_target, str) or not runtime_target:
        raise ConfigurationError("runtime manifest target is invalid")

    all_library_dirs = (*runtime.library_dirs, *sdk_library_dirs)
    # This -B entry also contains the generated assembler guard and therefore
    # is active for compile-only invocations. Clang's --ld-path and the
    # startfile overlay are link-only; passing them to -c/-S/-E makes Clang
    # diagnose unused command-line arguments under -Werror.
    always_flags: list[str] = [f"-B{tool_dir}/"]
    link_only_flags: list[str] = [f"-B{overlay}/"]
    static_link_suffix: tuple[str, ...] = ()
    if compiler.family == "clang":
        link_only_flags.append(f"--ld-path={tool_dir / 'ld'}")
    if isinstance(runtime, GccRuntimeBinding) and compiler.family == "gcc":
        # -B is intentionally pointed at the imported runtime's GCC install
        # directory, not at a compiler binary prefix.  It supplies crtbegin,
        # crtend, and libgcc while the actual driver stays external.
        link_only_flags.append(f"-B{runtime.gcc_runtime_dir}/")
    elif isinstance(runtime, GccRuntimeBinding):
        # The imported GCC target may use a different vendor component (for
        # example x86_64-portable-linux-gnu) than the external Clang frontend.
        # --gcc-install-dir pins the exact CRT/runtime layout without requiring
        # a GCC executable and without changing Clang's detected target ABI.
        always_flags.extend(
            (
                f"--target={compiler.target}",
                f"--gcc-install-dir={runtime.gcc_runtime_dir}",
                f"--driver-mode={'g++' if cxx else 'gcc'}",
            )
        )
        link_only_flags.extend(("--rtlib=libgcc", "--unwindlib=libgcc"))
        if cxx:
            link_only_flags.append("-stdlib=libstdc++")
    else:
        always_flags.extend(
            (
                f"--target={compiler.target}",
                f"-resource-dir={runtime.resource_dir}",
            )
        )
        link_only_flags.extend(("--rtlib=compiler-rt", "--unwindlib=libunwind"))
        # Static libunwind uses POSIX rwlocks and dladdr. Resolve libunwind on
        # the first static-group scan so its pthread dependency precedes libc;
        # forcing dladdr unresolved makes an earlier libdl archive supply it on
        # glibc versions where libdl is still separate from libc.
        static_link_suffix = (
            "-pthread",
            "-Wl,--undefined=_Unwind_Resume",
            "-Wl,--undefined=dladdr",
            "-ldl",
        )
        if cxx:
            link_only_flags.append("-stdlib=libc++")
            always_flags.append("-nostdinc++")
            always_flags.extend(_isystem_flags(runtime.cxx_include_dirs))

    link_only_flags.extend(f"-L{directory}" for directory in all_library_dirs)
    link_only_flags.append(
        "-Wl,-rpath-link," + ":".join(str(path) for path in all_library_dirs)
    )

    if isinstance(runtime, GccRuntimeBinding) and compiler.family == "gcc":
        include_dirs = [*runtime.cxx_include_dirs] if cxx else []
        include_dirs.append(runtime.builtin_include_dir)
        if runtime.fixed_include_dir is not None:
            include_dirs.append(runtime.fixed_include_dir)
        sdk_include = sysroot / "usr" / "include"
        if not sdk_include.is_dir():
            raise ConfigurationError(
                f"SDK C include directory is missing: {sdk_include}"
            )
        include_dirs.append(sdk_include)
        always_flags.append("-nostdinc")
        always_flags.extend(_isystem_flags(tuple(include_dirs)))
    elif isinstance(runtime, GccRuntimeBinding) and cxx:
        # Keep Clang's resource headers (compiler builtins), but never let it
        # infer host libstdc++ headers from the frontend installation.
        always_flags.append("-nostdinc++")
        always_flags.extend(_isystem_flags(runtime.cxx_include_dirs))

    return _WrapperDriverFlags(
        always=tuple(always_flags),
        link_only=tuple(link_only_flags),
        static_link_suffix=static_link_suffix,
    )


def _map_uses_path(map_text: str, root: Path, filename: str) -> bool:
    root_text = str(root)
    return any(root_text in line and filename in line for line in map_text.splitlines())


def _reject_host_paths_in_link_evidence(
    name: str,
    evidence: str,
    *,
    allowed_roots: tuple[Path, ...],
    allowed_literals: tuple[str, ...] = (),
    allowed_exact_paths: tuple[Path, ...] = (),
    sysroot_alias_root: Path | None = None,
    linker_map: bool = False,
) -> None:
    resolved_roots = tuple(path.resolve() for path in allowed_roots)
    resolved_exact_paths = frozenset(
        path.resolve(strict=False) for path in allowed_exact_paths
    )
    resolved_sysroot = (
        sysroot_alias_root.resolve() if sysroot_alias_root is not None else None
    )
    for line in evidence.splitlines():
        for raw in re.findall(r"/(?:[^\s()\[\]{}]+)", line):
            candidate_text = raw.rstrip("',;:")
            if (
                linker_map
                and candidate_text == "/DISCARD/"
                and line.strip() == "/DISCARD/"
            ):
                continue
            if candidate_text in allowed_literals:
                continue
            if sysroot_alias_root is not None and resolved_sysroot is not None:
                # A GNU ld script within a sysroot may name target inputs with
                # absolute paths.  ld applies --sysroot to those names, while
                # its map can retain the original spelling (for example,
                # /lib64/libc.so.6).  Accept that spelling only when it maps to
                # an existing SDK input and cannot escape the SDK via symlinks.
                sysroot_candidate = sysroot_alias_root / candidate_text.lstrip("/")
                resolved_sysroot_candidate = sysroot_candidate.resolve(strict=False)
                if (
                    sysroot_candidate.exists()
                    and resolved_sysroot_candidate.is_relative_to(resolved_sysroot)
                ):
                    continue
            candidate = Path(candidate_text).resolve(strict=False)
            if candidate in resolved_exact_paths:
                continue
            if any(
                candidate == root or candidate.is_relative_to(root)
                for root in resolved_roots
            ):
                continue
            raise ExternalToolError(
                f"{name} link selected a build-host target input outside validated "
                f"roots: {candidate}"
            )


def _verify_target_relocatable(
    metadata: object, *, name: str, target_arch: str
) -> None:
    machine = getattr(metadata, "machine", None)
    elf_class = getattr(metadata, "elf_class", None)
    endianness = getattr(metadata, "endianness", None)
    elf_type = getattr(metadata, "elf_type", None)
    if machine != target_arch or elf_class != "ELF64":
        raise ExternalToolError(
            f"{name} is {machine}/{elf_class}, expected {target_arch}/ELF64"
        )
    if endianness != "little":
        raise ExternalToolError(
            f"{name} is {endianness}-endian, expected little-endian ELF"
        )
    if elf_type != "REL":
        raise ExternalToolError(f"{name} has ELF type {elf_type}, expected REL")


def _verify_archive_tools(
    *,
    cc_wrapper: Path,
    ar_wrapper: Path,
    ranlib_wrapper: Path,
    output: Path,
    target_arch: str,
    expected_interpreter: str,
) -> dict[str, object]:
    """Prove that the selected tools can archive and index target objects."""

    validation = output / ".archive-validation"
    validation.mkdir()
    source = validation / "linux-toolchain-archive-member.c"
    member = validation / "linux-toolchain-archive-member.o"
    archive = validation / "liblinux-toolchain-archive-probe.a"
    caller_source = validation / "linux-toolchain-archive-caller.c"
    caller = validation / "linux-toolchain-archive-caller"
    map_path = validation / "linux-toolchain-archive-caller.map"
    inspector = ReadElfInspector()
    try:
        source.write_text(
            "int linux_toolchain_archive_probe(void) { return 42; }\n", encoding="utf-8"
        )
        run([cc_wrapper, "-c", source, "-o", member])
        _verify_target_relocatable(
            inspector.inspect(member),
            name="archive probe object",
            target_arch=target_arch,
        )

        run([ar_wrapper, "qc", archive, member])
        run([ranlib_wrapper, archive])
        listing = run([ar_wrapper, "t", archive]).stdout.splitlines()
        if listing != [member.name]:
            raise ExternalToolError(
                "archive probe contains unexpected members: " + repr(listing)
            )

        members = inspector.inspect_archive(archive)
        if len(members) != 1:
            raise ExternalToolError(
                f"archive probe contains {len(members)} ELF members, expected 1"
            )
        _verify_target_relocatable(
            members[0],
            name="archive probe member",
            target_arch=target_arch,
        )

        caller_source.write_text(
            "extern int linux_toolchain_archive_probe(void);\n"
            "int main(void) { return linux_toolchain_archive_probe() == 42 ? 0 : 1; }\n",
            encoding="utf-8",
        )
        run(
            [
                cc_wrapper,
                caller_source,
                archive,
                f"-Wl,-Map,{map_path}",
                "-o",
                caller,
            ]
        )
        try:
            map_text = map_path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise ExternalToolError(
                f"archive probe link did not produce map {map_path}: {error}"
            ) from error
        if str(archive) not in map_text or member.name not in map_text:
            raise ExternalToolError(
                "archive probe link map does not show extraction of the target member"
            )
        linked = inspector.inspect(caller)
        if (
            linked.machine != target_arch
            or linked.elf_class != "ELF64"
            or linked.endianness != "little"
        ):
            raise ExternalToolError(
                "archive probe link produced "
                f"{linked.machine}/{linked.elf_class}/{linked.endianness}, expected "
                f"{target_arch}/ELF64/little"
            )
        if linked.interpreter != expected_interpreter:
            raise ExternalToolError(
                "archive probe link uses interpreter "
                f"{linked.interpreter!r}, expected {expected_interpreter!r}"
            )

    finally:
        shutil.rmtree(validation, ignore_errors=True)

    return {
        "status": "passed",
        "checks": list(_ARCHIVE_VALIDATION_CHECKS),
        "machine": target_arch,
        "elf_class": "ELF64",
        "endianness": "little",
    }


def _verify_target_tools(
    *,
    wrappers: Mapping[str, Path],
    output: Path,
    target_arch: str,
) -> dict[str, object]:
    """Prove that every compiler-selected binutil accepts target objects."""

    validation = output / ".target-tool-validation"
    validation.mkdir()
    source = validation / "linux-toolchain-assembler-probe.s"
    assembled = validation / "linux-toolchain-assembler-probe.o"
    copied = validation / "linux-toolchain-objcopy-probe.o"
    inspector = ReadElfInspector()
    try:
        source.write_text(
            ".text\n.globl linux_toolchain_assembler_probe\nlinux_toolchain_assembler_probe:\n",
            encoding="utf-8",
        )
        run([wrappers["as"], "-o", assembled, source])
        _verify_target_relocatable(
            inspector.inspect(assembled),
            name="assembler probe object",
            target_arch=target_arch,
        )

        nm_result = run([wrappers["nm"], assembled])
        if "linux_toolchain_assembler_probe" not in nm_result.stdout:
            raise ExternalToolError(
                "compiler-selected nm did not report the assembler probe symbol"
            )
        run([wrappers["objdump"], "-f", assembled])
        run([wrappers["objcopy"], assembled, copied])
        _verify_target_relocatable(
            inspector.inspect(copied),
            name="objcopy probe object",
            target_arch=target_arch,
        )
        run([wrappers["strip"], "-g", copied])
        _verify_target_relocatable(
            inspector.inspect(copied),
            name="strip probe object",
            target_arch=target_arch,
        )
    finally:
        shutil.rmtree(validation, ignore_errors=True)

    return {
        "status": "passed",
        "checks": list(_TARGET_TOOL_VALIDATION_CHECKS),
        "machine": target_arch,
        "elf_class": "ELF64",
        "endianness": "little",
    }


def _verify_binding_links(
    *,
    cc_wrapper: Path,
    cxx_wrapper: Path,
    output: Path,
    sysroot: Path,
    overlay: Path,
    target_arch: str,
    expected_interpreter: str,
    runtime: RuntimeLinkEvidence | None = None,
    linker_executable: Path | None = None,
) -> dict[str, object]:
    runtime_root = runtime.runtime_root if runtime is not None else None
    validation = output / ".link-validation"
    validation.mkdir()
    checks: list[tuple[str, Path, str, tuple[str, ...]]] = [
        ("c-executable", cc_wrapper, "int main(void) { return 0; }\n", ()),
        (
            "c-shared-library",
            cc_wrapper,
            "int linux_toolchain_probe(void) { return 0; }\n",
            ("-shared", "-fPIC"),
        ),
        ("cxx-executable", cxx_wrapper, "int main() { return 0; }\n", ()),
    ]
    if runtime_root is not None:
        checks[-1] = (
            "cxx-executable",
            cxx_wrapper,
            "#include <stdexcept>\n"
            'int main() { try { throw std::runtime_error("linux-toolchain"); } '
            "catch (const std::exception&) { return 0; } return 1; }\n",
            (),
        )
        checks.append(
            (
                "cxx-shared-exception",
                cxx_wrapper,
                "#include <stdexcept>\n"
                'extern "C" void linux_toolchain_throw() { '
                'throw std::runtime_error("linux-toolchain"); }\n',
                ("-shared", "-fPIC"),
            )
        )
        checks.extend(
            (
                (
                    "c-static-executable",
                    cc_wrapper,
                    "int main(void) { return 0; }\n",
                    ("-static",),
                ),
                (
                    "cxx-static-exception",
                    cxx_wrapper,
                    "#include <stdexcept>\n"
                    'int main() { try { throw std::runtime_error("linux-toolchain"); } '
                    "catch (const std::exception&) { return 0; } return 1; }\n",
                    ("-static",),
                ),
            )
        )
    inspector = ReadElfInspector()
    completed: list[str] = []
    try:
        for name, wrapper, source_text, extra in checks:
            static_link = "static" in name
            shared_link = "shared" in name
            suffix = ".cc" if wrapper == cxx_wrapper else ".c"
            source = validation / f"{name}{suffix}"
            object_path = validation / f"{name}.o"
            binary = validation / f"{name}.so" if shared_link else validation / name
            map_path = validation / f"{name}.map"
            source.write_text(source_text, encoding="utf-8")
            compile_flags = tuple(flag for flag in extra if flag == "-fPIC")
            link_flags = tuple(flag for flag in extra if flag != "-fPIC")
            run([wrapper, "-c", source, *compile_flags, "-o", object_path])
            result = run(
                [
                    wrapper,
                    object_path,
                    *link_flags,
                    f"-Wl,-Map,{map_path}",
                    *(("-Wl,-t",) if runtime_root is not None else ()),
                    "-o",
                    binary,
                ],
            )
            try:
                map_text = map_path.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                raise ExternalToolError(
                    f"linker did not produce validation map {map_path}: {error}"
                ) from error
            evidence = "\n".join((map_text, result.stdout, result.stderr))
            if runtime_root is not None:
                _reject_host_paths_in_link_evidence(
                    name,
                    map_text,
                    allowed_roots=(output, sysroot, runtime_root),
                    allowed_literals=(expected_interpreter,),
                    sysroot_alias_root=sysroot,
                    linker_map=True,
                )
                _reject_host_paths_in_link_evidence(
                    name,
                    result.stdout,
                    allowed_roots=(output, sysroot, runtime_root),
                    allowed_literals=(expected_interpreter,),
                    allowed_exact_paths=(
                        (linker_executable,) if linker_executable is not None else ()
                    ),
                    sysroot_alias_root=sysroot,
                )
                _reject_host_paths_in_link_evidence(
                    name,
                    result.stderr,
                    allowed_roots=(output, sysroot, runtime_root),
                    allowed_literals=(expected_interpreter,),
                    # GNU ld may prefix a warning with its own canonical path.
                    # That executable is not a target link input. Permit only
                    # the exact compiler-selected linker in diagnostics; keep
                    # linker-map inputs restricted to the artifact roots above.
                    allowed_exact_paths=(
                        (linker_executable,) if linker_executable is not None else ()
                    ),
                )
            for startfile in ("crti.o", "crtn.o"):
                if not _map_uses_path(evidence, overlay, startfile):
                    raise ExternalToolError(
                        f"{name} link did not select SDK {startfile}"
                    )
            if not shared_link and not any(
                _map_uses_path(evidence, overlay, startfile)
                for startfile in ("crt1.o", "Scrt1.o", "rcrt1.o")
            ):
                raise ExternalToolError(
                    f"{name} link did not select an SDK glibc entry startup object"
                )
            libc_input = "libc.a" if static_link else "libc.so"
            if not _map_uses_path(evidence, sysroot, libc_input):
                raise ExternalToolError(f"{name} link did not select SDK libc")
            if (
                name == "cxx-executable"
                and runtime is None
                and "libstdc++" not in evidence
            ):
                raise ExternalToolError(
                    "C++ validation link did not resolve the external libstdc++"
                )
            if isinstance(runtime, GccRuntimeLinkEvidence):
                for kind, names in (
                    ("crtbegin", ("crtbegin.o", "crtbeginS.o", "crtbeginT.o")),
                    ("crtend", ("crtend.o", "crtendS.o")),
                ):
                    if not any(
                        _map_uses_path(evidence, runtime.gcc_runtime_dir, filename)
                        for filename in names
                    ):
                        raise ExternalToolError(
                            f"{name} link did not select runtime {kind}"
                        )
                if "cxx" in name and not any(
                    _map_uses_path(evidence, directory, "libstdc++")
                    for directory in runtime.library_dirs
                ):
                    raise ExternalToolError(
                        f"{name} link did not select imported runtime libstdc++"
                    )
                if not _map_uses_path(evidence, runtime_root, "libgcc"):
                    raise ExternalToolError(
                        f"{name} link did not select imported runtime libgcc"
                    )
            elif isinstance(runtime, LlvmRuntimeLinkEvidence):
                if not _map_uses_path(
                    evidence, runtime.builtins.parent, runtime.builtins.name
                ):
                    raise ExternalToolError(
                        f"{name} link did not select imported compiler-rt builtins"
                    )
                for kind in ("crtbegin", "crtend"):
                    matching = tuple(
                        path for path in runtime.crt_objects if kind in path.name
                    )
                    if len(matching) != 1 or not _map_uses_path(
                        evidence, matching[0].parent, matching[0].name
                    ):
                        raise ExternalToolError(
                            f"{name} link did not select compiler-rt {kind}"
                        )
                expected_components: tuple[str, ...] = ()
                libraries = runtime.shared_libraries
                if static_link:
                    libraries = runtime.static_libraries
                    expected_components = (
                        ("libc++", "libunwind") if "cxx" in name else ()
                    )
                elif "cxx" in name:
                    expected_components = ("libc++", "libc++abi", "libunwind")
                for component in expected_components:
                    matching = tuple(
                        path
                        for path in libraries
                        if llvm_runtime_component(path.name) == component
                    )
                    if not matching or not any(
                        _map_uses_path(evidence, path.parent, path.name)
                        for path in matching
                    ):
                        raise ExternalToolError(
                            f"{name} link did not select imported {component}"
                        )

            metadata = inspector.inspect(binary)
            if metadata.machine != target_arch or metadata.elf_class != "ELF64":
                raise ExternalToolError(
                    f"{name} produced {metadata.machine}/{metadata.elf_class}, "
                    f"expected {target_arch}/ELF64"
                )
            if metadata.endianness != "little":
                raise ExternalToolError(
                    f"{name} produced {metadata.endianness}-endian ELF"
                )
            selected_interpreter = None if static_link else expected_interpreter
            if not shared_link and metadata.interpreter != selected_interpreter:
                raise ExternalToolError(
                    f"{name} uses interpreter {metadata.interpreter!r}, "
                    f"expected {selected_interpreter!r}"
                )
            if runtime_root is not None and (
                getattr(metadata, "rpath", ()) or getattr(metadata, "runpath", ())
            ):
                raise ExternalToolError(
                    f"{name} contains a deployment RPATH/RUNPATH; runtime selection "
                    "must remain a deployment concern"
                )
            needed = set(getattr(metadata, "needed", ()))
            if static_link and needed:
                raise ExternalToolError(
                    f"{name} is not fully static; DT_NEEDED contains: "
                    + ", ".join(sorted(needed))
                )
            if isinstance(runtime, LlvmRuntimeLinkEvidence):
                forbidden = needed.intersection(runtime.forbidden_sonames)
                if forbidden:
                    raise ExternalToolError(
                        f"{name} depends on forbidden GCC runtime SONAMEs: "
                        + ", ".join(sorted(forbidden))
                    )
                if (
                    "cxx" in name
                    and not static_link
                    and not any(soname.startswith("libc++.so") for soname in needed)
                ):
                    raise ExternalToolError(
                        f"{name} does not record the selected shared libc++ runtime"
                    )
            completed.append(name)
    finally:
        shutil.rmtree(validation, ignore_errors=True)
    expected_checks = (
        _RUNTIME_LINK_VALIDATION_CHECKS
        if runtime is not None
        else _LINK_VALIDATION_CHECKS
    )
    if tuple(completed) != expected_checks:
        raise AssertionError("binding link validation check set is inconsistent")
    return {"status": "passed", "checks": completed}


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _link_tool(path: Path, tool: ArchiveTool, *, final_bin: Path) -> None:
    """Link a binding command directly to its selected target tool."""

    path.symlink_to(os.path.relpath(tool.invocation_path, start=final_bin))


def _install_driver_aliases(
    bin_dir: Path,
    *,
    family: str,
    cc_wrapper: Path,
    cxx_wrapper: Path,
    target_tool_names: tuple[str, ...] = ("ar", "ranlib"),
) -> tuple[str, ...]:
    """Install the conventional names for the selected compiler family."""

    aliases = _driver_aliases(
        family=family,
        cc_wrapper=cc_wrapper,
        cxx_wrapper=cxx_wrapper,
    )
    for name, source in aliases.items():
        destination = bin_dir / name
        destination.symlink_to(source.name)
    return tuple(sorted({"cc", "c++", *target_tool_names, *aliases}))


def _driver_aliases(
    *,
    family: str,
    cc_wrapper: Path,
    cxx_wrapper: Path,
) -> dict[str, Path]:
    return {
        "gcc" if family == "gcc" else "clang": cc_wrapper,
        "g++" if family == "gcc" else "clang++": cxx_wrapper,
    }


def _prepare_binding_output(output: Path, *, force: bool) -> None:
    if output in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid binding output path: {output}")
    if not output.exists():
        return
    if not output.is_dir():
        raise ConfigurationError(f"binding output is not a directory: {output}")
    try:
        nonempty = next(output.iterdir(), None) is not None
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect binding output {output}: {error}"
        ) from error
    if not nonempty:
        return
    if not force:
        raise ConfigurationError(
            f"binding already exists and its output is non-empty: {output}; "
            "pass --force only for a generator-owned binding"
        )
    owner_marker = output / ".linux-toolchain-binding"
    manifest_path = output / "binding.json"
    try:
        marker_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        marker_format = (
            marker_data.get("format") if isinstance(marker_data, dict) else None
        )
        owned = (
            owner_marker.is_file()
            and not owner_marker.is_symlink()
            and marker_format == BINDING_FORMAT
            and marker_data.get("schema") == BINDING_SCHEMA
            and owner_marker.read_text(encoding="utf-8") == f"format={marker_format}\n"
            and manifest_path.is_file()
            and not manifest_path.is_symlink()
            and isinstance(marker_data, dict)
            and marker_data.get("compatibility_scope") == "glibc-floor"
            and isinstance(marker_data.get("sdk"), dict)
            and isinstance(marker_data.get("compiler"), dict)
            and isinstance(marker_data.get("glibc_binding"), dict)
            and isinstance(marker_data.get("validation"), dict)
        )
    except (OSError, json.JSONDecodeError):
        owned = False
    if not owned:
        raise ConfigurationError(
            f"refusing to replace unowned binding output: {output}"
        )


def _publish_binding(
    staging: Path,
    output: Path,
    *,
    validate: Callable[[Path], None] | None = None,
) -> None:
    replace_directory(staging, output, validate=validate)


def _pkg_config_directories(
    sysroot: Path,
    library_dirs: tuple[Path, ...],
) -> tuple[Path, ...]:
    candidates = (
        sysroot / "lib/pkgconfig",
        sysroot / "usr/lib/pkgconfig",
        *(directory / "pkgconfig" for directory in library_dirs),
        sysroot / "usr/share/pkgconfig",
        sysroot / "share/pkgconfig",
    )
    result: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir() and candidate not in result:
            result.append(candidate)
    return tuple(result)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


@dataclass(frozen=True)
class _BindingSdk:
    root: Path
    sysroot: Path
    target: Mapping[str, object]
    spec: TargetSpec


def _load_binding_sdk(path: Path) -> _BindingSdk:
    root = path.expanduser().resolve()
    manifest_path = root / "manifest.json"
    sysroot = root / "sysroot"
    if not manifest_path.is_file() or not sysroot.is_dir():
        raise ConfigurationError(f"not a built Linux toolchain glibc SDK: {root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"invalid SDK manifest {manifest_path}: {error}"
        ) from error
    manifest_format = manifest.get("format") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != SDK_MANIFEST_SCHEMA
        or not isinstance(manifest_format, int)
        or isinstance(manifest_format, bool)
        or manifest_format != SDK_MANIFEST_FORMAT
    ):
        raise ConfigurationError("unsupported or invalid SDK manifest schema or format")
    if manifest.get("compatibility_scope") != "glibc-floor":
        raise ConfigurationError("SDK does not declare the glibc-floor policy")

    validate_license_evidence(root, manifest.get("licenses"), context="SDK")
    require_license_files(root, sdk_required_license_paths(), context="SDK")

    target = manifest.get("target")
    if not isinstance(target, dict):
        raise ConfigurationError("SDK manifest target must be an object")
    required_target = {
        "arch",
        "vendor",
        "libc",
        "libc_version",
        "linux_headers",
        "minimum_kernel",
        "cpu",
        "triplet",
    }
    missing = sorted(required_target.difference(target))
    if missing:
        raise ConfigurationError(
            "SDK manifest target is missing: " + ", ".join(missing)
        )
    spec = TargetSpec(
        arch=target["arch"],
        vendor=target["vendor"],
        libc=target["libc"],
        libc_version=target["libc_version"],
        linux_headers=target["linux_headers"],
        minimum_kernel=target["minimum_kernel"],
        cpu=target["cpu"],
    )
    spec.validate()
    if target["triplet"] != spec.triplet:
        raise ConfigurationError("SDK manifest target triplet is inconsistent")

    return _BindingSdk(
        root=root,
        sysroot=sysroot,
        target=target,
        spec=spec,
    )


def _validate_binding_layout(
    output: Path,
    sdk: _BindingSdk,
    runtime: RuntimeBinding | None,
    toolchain: _ManagedCompilerToolchain | None,
) -> None:
    if output in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid binding output path: {output}")
    protected = [("SDK", sdk.root)]
    if runtime is not None:
        protected.append(("runtime export", runtime.export_root))
    if toolchain is not None:
        protected.append(("Compiler Kit", toolchain.kit.root))
    for name, path in protected:
        if _paths_overlap(output, path):
            raise ConfigurationError(
                f"binding output and {name} directories must not contain one another"
            )
    if toolchain is not None and _paths_overlap(toolchain.kit.root, sdk.root):
        raise ConfigurationError(
            "Compiler Kit and SDK directories must not contain one another"
        )
    if (
        toolchain is not None
        and runtime is not None
        and _paths_overlap(toolchain.kit.root, runtime.export_root)
    ):
        raise ConfigurationError(
            "Compiler Kit and runtime export directories must not contain one another"
        )

    for context, path in (
        ("SDK path", sdk.root),
        ("binding output path", output),
        *(
            (("runtime export path", runtime.export_root),)
            if runtime is not None
            else ()
        ),
    ):
        _single_line_value(str(path), context)


def _validate_binding_compatibility(
    sdk: _BindingSdk,
    compiler: CompilerInfo,
    runtime: RuntimeBinding | None,
    toolchain: _ManagedCompilerToolchain | None,
) -> None:
    target = sdk.target
    target_arch = sdk.spec.arch
    if _target_arch(compiler.target) != target_arch:
        raise ConfigurationError(
            f"compiler target {compiler.target!r} does not match SDK "
            f"architecture {target_arch!r}"
        )
    if compiler.family not in {"gcc", "clang"}:
        raise ConfigurationError(f"unsupported compiler family: {compiler.family!r}")
    if not isinstance(compiler.major, int) or compiler.major < 1:
        raise ConfigurationError("invalid compiler major version")

    if toolchain is not None:
        kit = toolchain.kit
        provider = kit.manifest.provider
        kit_target = kit.manifest.target
        expected_compiler = (
            provider["name"],
            provider["version"],
            provider["major"],
            kit_target["triplet"],
            kit.cc.invocation_path,
            kit.cxx.invocation_path,
        )
        actual_compiler = (
            compiler.family,
            compiler.version,
            compiler.major,
            compiler.target,
            compiler.cc,
            compiler.cxx,
        )
        if actual_compiler != expected_compiler:
            raise ConfigurationError(
                "managed compiler identity does not match its Compiler Kit manifest"
            )
        if (
            kit_target["arch"] != target_arch
            or kit_target["triplet"] != target["triplet"]
        ):
            raise ConfigurationError(
                "Compiler Kit target does not match the selected SDK target"
            )

    if runtime is None:
        return
    manifest = runtime.manifest
    if manifest.arch != target_arch or _target_arch(manifest.target) != target_arch:
        raise ConfigurationError(
            f"runtime target {manifest.target!r} does not match SDK "
            f"architecture {target_arch!r}"
        )
    if AbiVersion.parse(manifest.glibc_floor) > AbiVersion.parse(
        str(target["libc_version"])
    ):
        raise ConfigurationError(
            f"runtime glibc floor {manifest.glibc_floor} is newer than "
            f"SDK glibc floor {target['libc_version']}"
        )

    provider_name = manifest.provider.get("name")
    provider_major = manifest.provider.get("major")
    provider_version = manifest.provider.get("version")
    if isinstance(runtime, GccRuntimeBinding):
        if provider_name != "gcc" or not isinstance(provider_major, int):
            raise ConfigurationError("runtime manifest has an invalid GCC provider")
        if compiler.family == "gcc" and compiler.major != provider_major:
            raise ConfigurationError(
                f"GCC frontend major {compiler.major} does not match imported "
                f"GCC runtime major {provider_major}"
            )
        if (
            toolchain is not None
            and compiler.family == "gcc"
            and provider_version != compiler.version
        ):
            raise ConfigurationError(
                f"managed GCC frontend version {compiler.version} does not match "
                f"imported GCC runtime version {provider_version}"
            )
    else:
        if compiler.family != "clang" or toolchain is None:
            raise ConfigurationError(
                "LLVM libc++ runtime requires a managed Clang Compiler Kit"
            )
        if provider_name != "llvm" or not isinstance(provider_major, int):
            raise ConfigurationError("runtime manifest has an invalid LLVM provider")
        if provider_version != compiler.version:
            raise ConfigurationError(
                f"managed Clang version {compiler.version} does not match "
                f"LLVM runtime version {provider_version}"
            )
        if AbiVersion.parse(manifest.glibc_floor) != AbiVersion.parse(
            str(target["libc_version"])
        ):
            raise ConfigurationError(
                f"LLVM runtime glibc floor {manifest.glibc_floor} does not match "
                f"SDK glibc floor {target['libc_version']}"
            )
    if toolchain is not None and manifest.target != compiler.target:
        raise ConfigurationError(
            f"managed runtime target {manifest.target!r} does not match "
            f"Compiler Kit target {compiler.target!r}"
        )


def _resolve_binding_integrations(
    integrations: Sequence[IntegrationName],
    conan: ConanSettings | None,
    runtime: RuntimeBinding | None,
) -> tuple[tuple[IntegrationName, ...], ConanSettings | None]:
    selected = tuple(integrations)
    if not selected:
        raise ConfigurationError("at least one integration must be selected")
    unsupported = sorted(set(selected).difference(SUPPORTED_INTEGRATIONS))
    if unsupported:
        raise ConfigurationError("unsupported integration: " + ", ".join(unsupported))
    duplicates = sorted(name for name in set(selected) if selected.count(name) > 1)
    if duplicates:
        raise ConfigurationError(
            "duplicate integration selection: " + ", ".join(duplicates)
        )
    if "conan" not in selected:
        if conan is not None:
            raise ConfigurationError("Conan settings require the conan integration")
        return selected, None

    requested = conan or ConanSettings()
    if isinstance(runtime, LlvmRuntimeBinding):
        if requested.libcxx not in {None, "libc++"}:
            raise ConfigurationError("LLVM runtime requires Conan libcxx='libc++'")
        libcxx = "libc++"
    else:
        if requested.libcxx not in {None, "libstdc++", "libstdc++11"}:
            raise ConfigurationError(
                "GCC-compatible runtime requires a libstdc++ Conan ABI setting"
            )
        libcxx = requested.libcxx or "libstdc++11"
    return selected, replace(requested, libcxx=libcxx)


def _runtime_manifest_data(runtime: RuntimeBinding | None) -> dict[str, object]:
    if runtime is None:
        return {
            "policy": "external-unpinned",
            "kind": "compiler-default",
            "note": (
                "C++ runtime symbol requirements are audited but are not bounded "
                "by this glibc-floor binding."
            ),
        }
    manifest = runtime.manifest
    common: dict[str, object] = {
        "path": str(runtime.export_root),
        "provider": _json_compatible(manifest.provider),
        "arch": manifest.arch,
        "target": manifest.target,
        "glibc_floor": manifest.glibc_floor,
        "locations": _json_compatible(manifest.locations),
        "version_symbol_reports": _json_compatible(manifest.version_symbol_reports),
    }
    if isinstance(runtime, GccRuntimeBinding):
        return {"policy": "pinned-gcc-runtime", "kind": "libstdc++", **common}
    return {
        "policy": "pinned-llvm-runtime",
        "kind": "libc++",
        **common,
        "source": dict(manifest.source),
        "abi": _json_compatible(manifest.abi),
        "forbidden_sonames": list(manifest.forbidden_sonames),
        "validation": _json_compatible(manifest.validation),
    }


def _compiler_toolchain_manifest(
    toolchain: _ManagedCompilerToolchain | None,
) -> tuple[dict[str, object], str]:
    if toolchain is None:
        return {"mode": "external"}, "compiler-driver"
    kit = toolchain.kit
    return (
        {
            "mode": "managed",
            "path": str(kit.root),
            "manifest_path": str(kit.manifest_path),
            "provider": _json_compatible(kit.manifest.provider),
            "host": _json_compatible(kit.manifest.host),
            "target": _json_compatible(kit.manifest.target),
        },
        "compiler-kit",
    )


def _integration_manifest(
    selected: tuple[IntegrationName, ...],
    paths: Mapping[str, Path],
    conan: ConanIntegrationConfig | None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    if "cmake" in selected:
        result["cmake"] = {"toolchain": str(paths["cmake_toolchain"])}
    if "shell" in selected:
        result["shell"] = {"environment": str(paths["environment"])}
    if "conan" in selected:
        assert conan is not None
        result["conan"] = {
            "host_profile": str(paths["conan_host_profile"]),
            "cmake_toolchain": str(paths["conan_cmake_toolchain"]),
            "cmake_late": str(paths["conan_cmake_late"]),
            "settings": {
                "cppstd": conan.cppstd,
                "libcxx": conan.libcxx,
                "build_type": conan.build_type,
            },
        }
    return result


def _write_audit_policy(
    path: Path,
    target: Mapping[str, object],
    target_arch: str,
) -> str:
    policy = _audit_policy(target, target_arch)
    path.write_text(
        json.dumps(policy.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return policy.allowed_interpreters[0]


def _audit_policy(
    target: Mapping[str, object],
    target_arch: str,
) -> AuditPolicy:
    interpreter = {
        "x86_64": "/lib64/ld-linux-x86-64.so.2",
        "aarch64": "/lib/ld-linux-aarch64.so.1",
    }[target_arch]
    forbidden_versions = ["GLIBC_PRIVATE"]
    if AbiVersion.parse(str(target["libc_version"])) < GLIBC_DT_RELR_MIN_VERSION:
        forbidden_versions.append("GLIBC_ABI_DT_RELR")
    return AuditPolicy(
        machine=target_arch,
        elf_class="ELF64",
        endianness="little",
        max_required_versions={
            "GLIBC": str(target["libc_version"]),
            "GLIBCXX": None,
            "CXXABI": None,
            "GCC": None,
        },
        forbidden_versions=tuple(forbidden_versions),
        allowed_interpreters=(interpreter,),
    )


def _binding_tools(
    compiler: CompilerInfo,
    runtime: RuntimeBinding | None,
    toolchain: _ManagedCompilerToolchain | None,
) -> _BindingTools:
    if toolchain is None:
        cc = _capture_executable_identity(compiler.cc, "C compiler driver")
        cxx = _capture_executable_identity(compiler.cxx, "C++ compiler driver")
        target_tools = _resolve_compiler_target_tools(compiler)
        linker = _resolve_compiler_tool(compiler, "ld") if runtime is not None else None
    else:
        cc = ExecutableIdentity(invocation_path=toolchain.kit.cc.invocation_path)
        cxx = ExecutableIdentity(invocation_path=toolchain.kit.cxx.invocation_path)
        target_tools = toolchain.target_tools
        linker = toolchain.linker if runtime is not None else None

    selected_target_tools = _target_tools(target_tools)
    selected_tools: dict[str, ArchiveTool] = {
        "ar": target_tools.ar,
        "ranlib": target_tools.ranlib,
        **selected_target_tools,
    }
    if linker is not None:
        selected_tools["ld"] = linker
    return _BindingTools(
        cc=cc,
        cxx=cxx,
        target_tools=target_tools,
        linker=linker,
        selected_target_tools=selected_target_tools,
        selected_tools=selected_tools,
    )


def _binding_driver_flags(
    *,
    compiler: CompilerInfo,
    runtime: RuntimeBinding | None,
    sdk: _BindingSdk,
    output: Path,
    library_dirs: tuple[Path, ...],
) -> tuple[_WrapperDriverFlags, _WrapperDriverFlags, tuple[str, ...]]:
    final_bin = output / "bin"
    final_overlay = output / "glibc-startfiles"
    if runtime is None:
        common = _WrapperDriverFlags(
            always=(f"-B{final_bin}/",),
            link_only=(
                f"-B{final_overlay}/",
                *(f"-L{directory}" for directory in library_dirs),
                "-Wl,-rpath-link," + ":".join(str(path) for path in library_dirs),
            ),
        )
        cc_flags = cxx_flags = common
    else:
        cc_flags = _runtime_wrapper_flags(
            compiler=compiler,
            runtime=runtime,
            sysroot=sdk.sysroot,
            overlay=final_overlay,
            tool_dir=final_bin,
            sdk_library_dirs=library_dirs,
            cxx=False,
        )
        cxx_flags = _runtime_wrapper_flags(
            compiler=compiler,
            runtime=runtime,
            sysroot=sdk.sysroot,
            overlay=final_overlay,
            tool_dir=final_bin,
            sdk_library_dirs=library_dirs,
            cxx=True,
        )
    suffix_flags = (
        ("-fno-lto", "-fno-use-linker-plugin")
        if compiler.family == "gcc"
        else ("-fno-lto", "--no-default-config")
    )
    return cc_flags, cxx_flags, suffix_flags


def _binding_integration_inputs(
    *,
    sdk: _BindingSdk,
    output: Path,
    compiler: CompilerInfo,
    tools: _BindingTools,
    library_dirs: tuple[Path, ...],
    conan: ConanSettings | None,
) -> _BindingIntegrationInputs:
    final_bin = output / "bin"
    final_tool_paths = {name: final_bin / name for name in tools.selected_tools}
    pkg_config_dirs = _pkg_config_directories(sdk.sysroot, library_dirs) or (
        output / "env" / "empty-pkgconfig",
    )
    context = IntegrationContext(
        binding_root=output,
        target=str(sdk.target["triplet"]),
        architecture=sdk.spec.arch,
        sysroot=sdk.sysroot,
        cc=final_bin / "cc",
        cxx=final_bin / "c++",
        tools={
            name: final_tool_paths[name]
            for name in ("ar", "ranlib", *tools.selected_target_tools)
        },
        linker=final_tool_paths.get("ld"),
    )
    conan_config = (
        ConanIntegrationConfig(
            glibc_version=str(sdk.target["libc_version"]),
            linux_headers=str(sdk.target["linux_headers"]),
            minimum_kernel=str(sdk.target["minimum_kernel"]),
            compiler_family=compiler.family,
            compiler_version=compiler.major,
            settings=conan,
        )
        if conan is not None
        else None
    )
    return _BindingIntegrationInputs(
        context=context,
        shell=ShellIntegrationConfig(pkg_config_dirs=pkg_config_dirs),
        conan=conan_config,
    )


def _binding_manifest(
    *,
    sdk: _BindingSdk,
    output: Path,
    compiler: CompilerInfo,
    runtime: RuntimeBinding | None,
    toolchain: _ManagedCompilerToolchain | None,
    managed_evidence: Mapping[str, object] | None,
    tools: _BindingTools,
    cc_flags: _WrapperDriverFlags,
    cxx_flags: _WrapperDriverFlags,
    suffix_flags: tuple[str, ...],
    aliases: tuple[str, ...],
    integrations: tuple[IntegrationName, ...],
    integration_paths: Mapping[str, object],
    conan: ConanIntegrationConfig | None,
    library_dirs: tuple[Path, ...],
    startfiles: tuple[str, ...],
) -> dict[str, object]:
    final_bin = output / "bin"
    final_tool_paths = {name: final_bin / name for name in tools.selected_tools}
    toolchain_manifest, tool_selection = _compiler_toolchain_manifest(toolchain)
    manifest: dict[str, object] = {
        "schema": BINDING_SCHEMA,
        "format": BINDING_FORMAT,
        "compatibility_scope": "glibc-floor",
        "sdk": {
            "path": str(sdk.root),
            "glibc_version": sdk.target["libc_version"],
            "triplet": sdk.target["triplet"],
            "cpu": sdk.spec.cpu,
        },
        "compiler": {
            "family": compiler.family,
            "version": compiler.version,
            "major": compiler.major,
            "target": compiler.target,
            "version_text": compiler.version_text,
            "toolchain": toolchain_manifest,
            "drivers": {
                "c": {
                    **_executable_identity_manifest(tools.cc),
                    "wrapper": str(final_bin / "cc"),
                },
                "cxx": {
                    **_executable_identity_manifest(tools.cxx),
                    "wrapper": str(final_bin / "c++"),
                },
            },
            "tools": {
                "selection": tool_selection,
                **{
                    name: _compiler_tool_manifest(tool, final_tool_paths[name])
                    for name, tool in tools.selected_tools.items()
                },
            },
            "aliases": list(aliases),
            "compile_flags": {
                "c": [*cc_flags.always, *suffix_flags],
                "cxx": [*cxx_flags.always, *suffix_flags],
            },
            "link_flags": {
                "c": [*cc_flags.link_invocation, *suffix_flags],
                "cxx": [*cxx_flags.link_invocation, *suffix_flags],
            },
        },
        "cxx_runtime": _runtime_manifest_data(runtime),
        "integrations": _integration_manifest(integrations, integration_paths, conan),
        "audit_policy": str(output / "audit-policy.json"),
        "glibc_binding": {
            "startfile_overlay": str(output / "glibc-startfiles"),
            "startfiles": list(startfiles),
            "library_dirs": [str(path) for path in library_dirs],
        },
    }
    if managed_evidence is not None:
        manifest["managed"] = _json_compatible(managed_evidence)
    return manifest


def _create_binding(
    sdk: Path,
    output: Path,
    compiler: CompilerInfo,
    *,
    runtime: Path | RuntimeBinding | None = None,
    toolchain: _ManagedCompilerToolchain | None = None,
    managed_evidence: Mapping[str, object] | None = None,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    force: bool = False,
) -> Path:
    sdk_input = _load_binding_sdk(sdk)
    raw_output = output.expanduser()
    if raw_output.is_symlink():
        raise ConfigurationError(f"binding output cannot be a symlink: {raw_output}")
    publish_output = raw_output.resolve()
    runtime_input = (
        runtime
        if isinstance(runtime, (GccRuntimeBinding, LlvmRuntimeBinding))
        else _load_runtime_binding(runtime)
        if runtime is not None
        else None
    )
    if managed_evidence is not None and toolchain is None:
        raise ConfigurationError(
            "managed binding evidence requires a managed Compiler Kit"
        )
    _validate_binding_layout(publish_output, sdk_input, runtime_input, toolchain)
    _validate_binding_compatibility(
        sdk_input,
        compiler,
        runtime_input,
        toolchain,
    )
    selected_integrations, conan_settings = _resolve_binding_integrations(
        integrations,
        conan,
        runtime_input,
    )
    _prepare_binding_output(publish_output, force=force)

    tools = _binding_tools(compiler, runtime_input, toolchain)

    try:
        publish_output.parent.mkdir(parents=True, exist_ok=True)
        staging_owner = tempfile.TemporaryDirectory(
            prefix=f".{publish_output.name}.staging-",
            dir=publish_output.parent,
        )
    except OSError as error:
        raise ConfigurationError(
            f"cannot create binding staging directory beside {publish_output}: {error}"
        ) from error

    try:
        staging = Path(staging_owner.name)
        bin_dir = staging / "bin"
        final_bin = publish_output / "bin"
        bin_dir.mkdir(parents=True)
        (staging / ".linux-toolchain-binding").write_text(
            f"format={BINDING_FORMAT}\n",
            encoding="utf-8",
        )

        final_tool_paths = {name: final_bin / name for name in tools.selected_tools}
        for name, tool in tools.selected_tools.items():
            _link_tool(bin_dir / name, tool, final_bin=final_bin)

        library_dirs = _sdk_library_dirs(sdk_input.sysroot)
        startfiles = _create_startfile_overlay(
            staging / "glibc-startfiles",
            library_dirs,
        )
        cc_flags, cxx_flags, suffix_flags = _binding_driver_flags(
            compiler=compiler,
            runtime=runtime_input,
            sdk=sdk_input,
            output=publish_output,
            library_dirs=library_dirs,
        )
        _write_executable(
            bin_dir / "cc",
            _wrapper_text(
                tools.cc,
                sdk_input.sysroot,
                cc_flags,
                suffix_flags,
            ),
        )
        _write_executable(
            bin_dir / "c++",
            _wrapper_text(
                tools.cxx,
                sdk_input.sysroot,
                cxx_flags,
                suffix_flags,
            ),
        )
        alias_names = _install_driver_aliases(
            bin_dir,
            family=compiler.family,
            cc_wrapper=bin_dir / "cc",
            cxx_wrapper=bin_dir / "c++",
            target_tool_names=tuple(final_tool_paths),
        )

        interpreter = _write_audit_policy(
            staging / "audit-policy.json",
            sdk_input.target,
            sdk_input.spec.arch,
        )
        integration_inputs = _binding_integration_inputs(
            sdk=sdk_input,
            output=publish_output,
            compiler=compiler,
            tools=tools,
            library_dirs=library_dirs,
            conan=conan_settings,
        )
        integration_paths = render_integrations(
            staging,
            integration_inputs.context,
            integrations=selected_integrations,
            shell=integration_inputs.shell,
            conan=integration_inputs.conan,
        )

        manifest = _binding_manifest(
            sdk=sdk_input,
            output=publish_output,
            compiler=compiler,
            runtime=runtime_input,
            toolchain=toolchain,
            managed_evidence=managed_evidence,
            tools=tools,
            cc_flags=cc_flags,
            cxx_flags=cxx_flags,
            suffix_flags=suffix_flags,
            aliases=alias_names,
            integrations=selected_integrations,
            integration_paths=integration_paths,
            conan=integration_inputs.conan,
            library_dirs=library_dirs,
            startfiles=startfiles,
        )

        def validate_published_binding(published: Path) -> None:
            archive = _verify_archive_tools(
                cc_wrapper=published / "bin" / "cc",
                ar_wrapper=published / "bin" / "ar",
                ranlib_wrapper=published / "bin" / "ranlib",
                output=published,
                target_arch=sdk_input.spec.arch,
                expected_interpreter=interpreter,
            )
            target_validation = _verify_target_tools(
                wrappers={
                    name: published / "bin" / name
                    for name in tools.selected_target_tools
                },
                output=published,
                target_arch=sdk_input.spec.arch,
            )
            links = _verify_binding_links(
                cc_wrapper=published / "bin" / "cc",
                cxx_wrapper=published / "bin" / "c++",
                output=published,
                sysroot=sdk_input.sysroot,
                overlay=published / "glibc-startfiles",
                target_arch=sdk_input.spec.arch,
                expected_interpreter=interpreter,
                runtime=(
                    _runtime_link_evidence(runtime_input)
                    if runtime_input is not None
                    else None
                ),
                linker_executable=(
                    tools.linker.invocation_path if tools.linker is not None else None
                ),
            )
            manifest["validation"] = {
                "status": "passed",
                "links": links,
                "archive": archive,
                "target_tools": target_validation,
            }
            manifest_path = published / "binding.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_path.chmod(0o644)

        _publish_binding(
            staging,
            publish_output,
            validate=validate_published_binding,
        )
    finally:
        staging_owner.cleanup()
    return publish_output / "binding.json"


def create_binding(
    sdk: Path,
    output: Path,
    compiler: CompilerInfo,
    *,
    runtime: Path | None = None,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    force: bool = False,
) -> Path:
    """Create a glibc-floor binding for externally supplied compilers."""

    return _create_binding(
        sdk,
        output,
        compiler,
        runtime=runtime,
        integrations=integrations,
        conan=conan,
        force=force,
    )
