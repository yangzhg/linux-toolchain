from __future__ import annotations

import json
import os
import re
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
    RuntimeBindingSet,
    RuntimeLinkEvidence,
    load_runtime_binding,
    runtime_link_evidence,
    single_runtime_binding_set,
)
from linux_toolchain.compiler.toolchain import (
    ArchiveTool,
    CompilerInfo,
    ExecutableIdentity,
    ManagedCompilerToolchain,
    TargetTools,
    capture_executable_identity,
    resolve_compiler_target_tools,
    resolve_compiler_tool,
    target_architecture,
    target_tool_mapping,
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
from linux_toolchain.schema import read_json_object
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
_LINKER_TOOL_NAMES = {
    "default": "ld",
    "bfd": "ld.bfd",
    "gold": "ld.gold",
    "mold": "ld.mold",
    "lld": "ld.lld",
}


@dataclass(frozen=True)
class _WrapperDriverFlags:
    """Fixed driver arguments split by the phases that consume them.

    ``always`` contains compiler/driver selection and header-policy arguments.
    ``link_only`` contains startup-file, linker, and runtime-library selection
    arguments that Clang diagnoses as unused for non-link invocations.
    """

    always: tuple[str, ...]
    link_only: tuple[str, ...]

    def wrapper_arguments(
        self,
        compiler_family: str,
        suffix_flags: tuple[str, ...],
    ) -> tuple[str, ...]:
        arguments = list(self.always)
        if compiler_family == "clang" and self.link_only:
            arguments.extend(
                (
                    "--start-no-unused-arguments",
                    *self.link_only,
                    "--end-no-unused-arguments",
                )
            )
        else:
            arguments.extend(self.link_only)
        arguments.extend(suffix_flags)
        return tuple(arguments)


@dataclass(frozen=True)
class _BindingTools:
    cc: ExecutableIdentity
    cxx: ExecutableIdentity
    target_tools: TargetTools
    linkers: Mapping[str, ArchiveTool]
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


def _shell_literal(value: str) -> str:
    """Quote a generated shell word even when its current path needs no quoting.

    Bundle creation replaces producer paths with a relocation token after the
    wrapper has been rendered.  Keeping every generated word quoted makes the
    installed wrapper correct when the eventual installation prefix contains
    shell whitespace.
    """

    return "'" + value.replace("'", "'\"'\"'") + "'"


def _wrapper_text(
    compiler: ExecutableIdentity,
    compiler_family: str,
    sysroot: Path,
    driver_flags: _WrapperDriverFlags,
    suffix_flags: tuple[str, ...],
    *,
    selectable_cxx_runtimes: Sequence[str] = (),
) -> str:
    """Render a small driver wrapper that owns only fixed toolchain flags.

    Consumer arguments are ordinary compiler arguments and are forwarded
    unchanged.  Binding creation validates the resulting compiler, linker and
    runtime selection before publication.
    """

    compiler_arg = _shell_literal(str(compiler.invocation_path))
    sysroot_arg = _shell_literal(str(sysroot))
    driver_arguments = " ".join(
        _shell_literal(flag)
        for flag in driver_flags.wrapper_arguments(compiler_family, suffix_flags)
    )
    runtime_selection = ""
    if {"libstdc++", "libc++"}.issubset(selectable_cxx_runtimes):
        if compiler_family != "clang":
            raise AssertionError("C++ runtime switching requires Clang")
        runtime_selection = """case "${LINUX_TOOLCHAIN_CXX_RUNTIME-}" in
  ""|libstdc++) ;;
  libc++)
    set -- \\
      --start-no-unused-arguments \\
      -stdlib=libc++ \\
      --rtlib=compiler-rt \\
      --unwindlib=libunwind \\
      --end-no-unused-arguments \\
      "$@"
    ;;
  *)
    echo "linux-toolchain: unsupported C++ runtime: $LINUX_TOOLCHAIN_CXX_RUNTIME" >&2
    exit 2 ;;
esac

"""
    return f"""#!/bin/sh
set -eu

unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_RUN_PATH \\
  LD_LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX CCC_OVERRIDE_OPTIONS

{runtime_selection}exec {compiler_arg} {driver_arguments} "$@" --sysroot={sysroot_arg}
"""


def _linker_wrapper_text(
    linker: Path,
    library_dirs: tuple[Path, ...],
) -> str:
    """Apply binding-owned linker search paths only during a real link."""

    if not library_dirs:
        raise ConfigurationError("linker wrapper requires library directories")
    linker_arg = _shell_literal(str(linker))
    search_arguments = " ".join(
        f"-rpath-link {_shell_literal(str(path))}" for path in library_dirs
    )
    return f"""#!/bin/sh
set -eu

exec {linker_arg} {search_arguments} "$@"
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


def sdk_library_dirs(sysroot: Path) -> tuple[Path, ...]:
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
    runtimes: RuntimeBindingSet,
    sysroot: Path,
    overlay: Path,
    tool_dir: Path,
    runtime_link_dir: Path | None,
    sdk_library_dirs: tuple[Path, ...],
    cxx: bool,
) -> _WrapperDriverFlags:
    all_library_dirs = (
        *((runtime_link_dir,) if runtime_link_dir is not None else ()),
        *runtimes.library_dirs,
        *sdk_library_dirs,
    )
    always_flags: list[str] = [f"-B{tool_dir.parent / 'libexec'}/", f"-B{tool_dir}/"]
    link_only_flags: list[str] = [f"-B{overlay}/"]

    gcc_runtime = runtimes.find("libstdc++")
    llvm_runtime = runtimes.find("libc++")
    if gcc_runtime is not None and not isinstance(gcc_runtime, GccRuntimeBinding):
        raise AssertionError("libstdc++ runtime has the wrong binding type")
    if llvm_runtime is not None and not isinstance(llvm_runtime, LlvmRuntimeBinding):
        raise AssertionError("libc++ runtime has the wrong binding type")

    if compiler.family == "gcc":
        if not isinstance(gcc_runtime, GccRuntimeBinding):
            raise ConfigurationError("GCC binding requires its libstdc++ runtime")
        # -B is intentionally pointed at the imported runtime's GCC install
        # directory, not at a compiler binary prefix.  It supplies crtbegin,
        # crtend, and libgcc while the actual driver stays external.
        link_only_flags.append(f"-B{gcc_runtime.gcc_runtime_dir}/")
    else:
        if gcc_runtime is not None:
            # Anchor Clang's native Linux default (libstdc++ and libgcc) to the
            # bundled GCC runtime instead of allowing host GCC discovery.
            always_flags.append(f"--gcc-install-dir={gcc_runtime.gcc_runtime_dir}")
        if llvm_runtime is not None:
            # Let Clang process -stdlib=libc++ itself. The binding publishes
            # include/c++/v1 beside its driver directory and fixes only the
            # resource directory needed for compiler-rt.
            always_flags.extend(
                (
                    "-ccc-install-dir",
                    str(tool_dir),
                    "-resource-dir",
                    str(llvm_runtime.resource_dir),
                )
            )
            if gcc_runtime is None:
                link_only_flags.extend(("--rtlib=compiler-rt", "--unwindlib=libunwind"))
                if cxx:
                    always_flags.append("-stdlib=libc++")

    link_only_flags.extend(f"-L{directory}" for directory in all_library_dirs)

    if compiler.family == "gcc":
        assert isinstance(gcc_runtime, GccRuntimeBinding)
        include_dirs = [*gcc_runtime.cxx_include_dirs] if cxx else []
        include_dirs.append(gcc_runtime.builtin_include_dir)
        if gcc_runtime.fixed_include_dir is not None:
            include_dirs.append(gcc_runtime.fixed_include_dir)
        sdk_include = sysroot / "usr" / "include"
        if not sdk_include.is_dir():
            raise ConfigurationError(
                f"SDK C include directory is missing: {sdk_include}"
            )
        include_dirs.append(sdk_include)
        always_flags.append("-nostdinc")
        always_flags.extend(_isystem_flags(tuple(include_dirs)))
    elif cxx and gcc_runtime is None and runtimes.default_kind == "libstdc++":
        raise ConfigurationError(
            "Clang's default libstdc++ selection has no bundled GCC runtime"
        )

    return _WrapperDriverFlags(
        always=tuple(always_flags),
        link_only=tuple(link_only_flags),
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


def _cxx_runtime_header_check(runtime: RuntimeLinkEvidence | None) -> str:
    if isinstance(runtime, GccRuntimeLinkEvidence):
        expected, runtime_name, forbidden = (
            "__GLIBCXX__",
            "libstdc++",
            "_LIBCPP_VERSION",
        )
    elif isinstance(runtime, LlvmRuntimeLinkEvidence):
        expected, runtime_name, forbidden = (
            "_LIBCPP_VERSION",
            "libc++",
            "__GLIBCXX__",
        )
    else:
        return ""
    return (
        "#include <stdexcept>\n"
        f"#ifndef {expected}\n"
        f'#error "expected {runtime_name} headers"\n'
        "#endif\n"
        f"#ifdef {forbidden}\n"
        '#error "mixed libc++ and libstdc++ headers"\n'
        "#endif\n"
    )


@dataclass(frozen=True)
class _LinkQualification:
    cc_wrapper: Path
    cxx_wrapper: Path
    output: Path
    sysroot: Path
    overlay: Path
    target_arch: str
    expected_interpreter: str
    runtime: RuntimeLinkEvidence | None = None
    linker_executable: Path | None = None
    cxx_compile_flags: tuple[str, ...] = ()
    cxx_link_flags: tuple[str, ...] = ()
    environment: Mapping[str, str] | None = None
    redundant_clang_stdlib: str | None = None
    additional_runtime_roots: tuple[Path, ...] = ()
    forbidden_runtime_roots: tuple[Path, ...] = ()
    cxx_only: bool = False
    validation_suffix: str = ""

    @property
    def runtime_root(self) -> Path | None:
        return self.runtime.runtime_root if self.runtime is not None else None


@dataclass(frozen=True)
class _LinkProbeSpec:
    name: str
    wrapper: Path
    source: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class _LinkedProbe:
    spec: _LinkProbeSpec
    binary: Path
    map_text: str
    stdout: str
    stderr: str

    @property
    def static(self) -> bool:
        return "static" in self.spec.name

    @property
    def shared(self) -> bool:
        return "shared" in self.spec.name

    @property
    def evidence(self) -> str:
        return "\n".join((self.map_text, self.stdout, self.stderr))


def _link_probe_specs(
    qualification: _LinkQualification,
) -> tuple[_LinkProbeSpec, ...]:
    runtime_header_check = _cxx_runtime_header_check(qualification.runtime)
    checks: list[_LinkProbeSpec] = []
    if not qualification.cxx_only:
        checks.extend(
            (
                _LinkProbeSpec(
                    "c-executable",
                    qualification.cc_wrapper,
                    "int main(void) { return 0; }\n",
                    (),
                ),
                _LinkProbeSpec(
                    "c-shared-library",
                    qualification.cc_wrapper,
                    "int linux_toolchain_probe(void) { return 0; }\n",
                    ("-shared", "-fPIC"),
                ),
            )
        )
    cxx_source = "int main() { return 0; }\n"
    if qualification.runtime is not None:
        cxx_source = (
            runtime_header_check
            + 'int main() { try { throw std::runtime_error("linux-toolchain"); } '
            "catch (const std::exception&) { return 0; } return 1; }\n"
        )
    checks.append(
        _LinkProbeSpec(
            "cxx-executable",
            qualification.cxx_wrapper,
            cxx_source,
            (),
        )
    )
    if qualification.runtime is not None:
        checks.append(
            _LinkProbeSpec(
                "cxx-shared-exception",
                qualification.cxx_wrapper,
                runtime_header_check + 'extern "C" void linux_toolchain_throw() { '
                'throw std::runtime_error("linux-toolchain"); }\n',
                ("-shared", "-fPIC"),
            )
        )
        if not qualification.cxx_only:
            checks.append(
                _LinkProbeSpec(
                    "c-static-executable",
                    qualification.cc_wrapper,
                    "int main(void) { return 0; }\n",
                    ("-static",),
                )
            )
        checks.append(
            _LinkProbeSpec(
                "cxx-static-exception",
                qualification.cxx_wrapper,
                runtime_header_check
                + 'int main() { try { throw std::runtime_error("linux-toolchain"); } '
                "catch (const std::exception&) { return 0; } return 1; }\n",
                ("-static",),
            )
        )
    return tuple(checks)


def _verify_redundant_stdlib_argument(
    qualification: _LinkQualification,
    validation: Path,
) -> None:
    kind = qualification.redundant_clang_stdlib
    if kind is None:
        return
    if qualification.runtime is None or kind not in {"libstdc++", "libc++"}:
        raise AssertionError("redundant stdlib check requires a C++ runtime")
    source = validation / "cxx-redundant-stdlib.cc"
    object_path = validation / "cxx-redundant-stdlib.o"
    selector = f"-stdlib={kind}"
    source.write_text(
        _cxx_runtime_header_check(qualification.runtime) + "int main() { return 0; }\n",
        encoding="utf-8",
    )
    run(
        [
            qualification.cxx_wrapper,
            *qualification.cxx_compile_flags,
            selector,
            selector,
            "-c",
            source,
            "-o",
            object_path,
        ],
        env=qualification.environment,
    )


def _run_link_probe(
    qualification: _LinkQualification,
    validation: Path,
    spec: _LinkProbeSpec,
) -> _LinkedProbe:
    suffix = ".cc" if spec.wrapper == qualification.cxx_wrapper else ".c"
    source = validation / f"{spec.name}{suffix}"
    object_path = validation / f"{spec.name}.o"
    binary = (
        validation / f"{spec.name}.so"
        if "shared" in spec.name
        else validation / spec.name
    )
    map_path = validation / f"{spec.name}.map"
    source.write_text(spec.source, encoding="utf-8")
    compile_flags = tuple(flag for flag in spec.flags if flag == "-fPIC")
    link_flags = tuple(flag for flag in spec.flags if flag != "-fPIC")
    selected_compile_flags = (
        qualification.cxx_compile_flags
        if spec.wrapper == qualification.cxx_wrapper
        else ()
    )
    selected_link_flags = (
        qualification.cxx_link_flags
        if spec.wrapper == qualification.cxx_wrapper
        else ()
    )
    run(
        [
            spec.wrapper,
            *selected_compile_flags,
            "-c",
            source,
            *compile_flags,
            "-o",
            object_path,
        ],
        env=qualification.environment,
    )
    result = run(
        [
            spec.wrapper,
            *selected_link_flags,
            object_path,
            *link_flags,
            f"-Wl,-Map,{map_path}",
            *(("-Wl,-t",) if qualification.runtime is not None else ()),
            "-o",
            binary,
        ],
        env=qualification.environment,
    )
    try:
        map_text = map_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ExternalToolError(
            f"linker did not produce validation map {map_path}: {error}"
        ) from error
    return _LinkedProbe(
        spec=spec,
        binary=binary,
        map_text=map_text,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _verify_link_evidence_roots(
    qualification: _LinkQualification,
    probe: _LinkedProbe,
) -> None:
    runtime_root = qualification.runtime_root
    if runtime_root is None:
        return
    allowed_roots = (
        qualification.output,
        qualification.sysroot,
        runtime_root,
        *qualification.additional_runtime_roots,
    )
    _reject_host_paths_in_link_evidence(
        probe.spec.name,
        probe.map_text,
        allowed_roots=allowed_roots,
        allowed_literals=(qualification.expected_interpreter,),
        sysroot_alias_root=qualification.sysroot,
        linker_map=True,
    )
    allowed_linker = (
        (qualification.linker_executable,)
        if qualification.linker_executable is not None
        else ()
    )
    _reject_host_paths_in_link_evidence(
        probe.spec.name,
        probe.stdout,
        allowed_roots=allowed_roots,
        allowed_literals=(qualification.expected_interpreter,),
        allowed_exact_paths=allowed_linker,
        sysroot_alias_root=qualification.sysroot,
    )
    _reject_host_paths_in_link_evidence(
        probe.spec.name,
        probe.stderr,
        allowed_roots=allowed_roots,
        allowed_literals=(qualification.expected_interpreter,),
        # GNU ld may prefix a warning with its own canonical path. That
        # executable is not a target input, so permit only this exact path.
        allowed_exact_paths=allowed_linker,
    )
    for forbidden_root in qualification.forbidden_runtime_roots:
        if str(forbidden_root) in probe.evidence:
            raise ExternalToolError(
                f"{probe.spec.name} link selected a conflicting runtime: "
                f"{forbidden_root}"
            )


def _verify_sdk_link_inputs(
    qualification: _LinkQualification,
    probe: _LinkedProbe,
) -> None:
    name = probe.spec.name
    for startfile in ("crti.o", "crtn.o"):
        if not _map_uses_path(probe.evidence, qualification.overlay, startfile):
            raise ExternalToolError(f"{name} link did not select SDK {startfile}")
    if not probe.shared and not any(
        _map_uses_path(probe.evidence, qualification.overlay, startfile)
        for startfile in ("crt1.o", "Scrt1.o", "rcrt1.o")
    ):
        raise ExternalToolError(
            f"{name} link did not select an SDK glibc entry startup object"
        )
    libc_input = "libc.a" if probe.static else "libc.so"
    if not _map_uses_path(probe.evidence, qualification.sysroot, libc_input):
        raise ExternalToolError(f"{name} link did not select SDK libc")
    if (
        name == "cxx-executable"
        and qualification.runtime is None
        and "libstdc++" not in probe.evidence
    ):
        raise ExternalToolError(
            "C++ validation link did not resolve the external libstdc++"
        )


def _verify_gcc_runtime_inputs(
    runtime: GccRuntimeLinkEvidence,
    probe: _LinkedProbe,
) -> None:
    name = probe.spec.name
    for kind, names in (
        ("crtbegin", ("crtbegin.o", "crtbeginS.o", "crtbeginT.o")),
        ("crtend", ("crtend.o", "crtendS.o")),
    ):
        if not any(
            _map_uses_path(probe.evidence, runtime.gcc_runtime_dir, filename)
            for filename in names
        ):
            raise ExternalToolError(f"{name} link did not select runtime {kind}")
    if "cxx" in name and not any(
        _map_uses_path(probe.evidence, directory, "libstdc++")
        for directory in runtime.library_dirs
    ):
        raise ExternalToolError(
            f"{name} link did not select imported runtime libstdc++"
        )
    if not _map_uses_path(probe.evidence, runtime.runtime_root, "libgcc"):
        raise ExternalToolError(f"{name} link did not select imported runtime libgcc")


def _verify_llvm_runtime_inputs(
    runtime: LlvmRuntimeLinkEvidence,
    probe: _LinkedProbe,
) -> None:
    name = probe.spec.name
    if not _map_uses_path(
        probe.evidence,
        runtime.builtins.parent,
        runtime.builtins.name,
    ):
        raise ExternalToolError(
            f"{name} link did not select imported compiler-rt builtins"
        )
    for kind in ("crtbegin", "crtend"):
        matching = tuple(path for path in runtime.crt_objects if kind in path.name)
        if len(matching) != 1 or not _map_uses_path(
            probe.evidence,
            matching[0].parent,
            matching[0].name,
        ):
            raise ExternalToolError(f"{name} link did not select compiler-rt {kind}")
    libraries = runtime.shared_libraries
    expected_components: tuple[str, ...] = ()
    if probe.static:
        libraries = runtime.static_libraries
        expected_components = ("libc++", "libunwind") if "cxx" in name else ()
    elif "cxx" in name:
        expected_components = ("libc++", "libc++abi", "libunwind")
    for component in expected_components:
        matching = tuple(
            path for path in libraries if llvm_runtime_component(path.name) == component
        )
        if not matching or not any(
            _map_uses_path(probe.evidence, path.parent, path.name) for path in matching
        ):
            raise ExternalToolError(f"{name} link did not select imported {component}")


def _verify_runtime_link_inputs(
    qualification: _LinkQualification,
    probe: _LinkedProbe,
) -> None:
    if isinstance(qualification.runtime, GccRuntimeLinkEvidence):
        _verify_gcc_runtime_inputs(qualification.runtime, probe)
    elif isinstance(qualification.runtime, LlvmRuntimeLinkEvidence):
        _verify_llvm_runtime_inputs(qualification.runtime, probe)


def _verify_linked_probe(
    qualification: _LinkQualification,
    probe: _LinkedProbe,
    inspector: ReadElfInspector,
) -> None:
    name = probe.spec.name
    metadata = inspector.inspect(probe.binary)
    if metadata.machine != qualification.target_arch or metadata.elf_class != "ELF64":
        raise ExternalToolError(
            f"{name} produced {metadata.machine}/{metadata.elf_class}, "
            f"expected {qualification.target_arch}/ELF64"
        )
    if metadata.endianness != "little":
        raise ExternalToolError(f"{name} produced {metadata.endianness}-endian ELF")
    selected_interpreter = None if probe.static else qualification.expected_interpreter
    if not probe.shared and metadata.interpreter != selected_interpreter:
        raise ExternalToolError(
            f"{name} uses interpreter {metadata.interpreter!r}, "
            f"expected {selected_interpreter!r}"
        )
    if qualification.runtime is not None and (
        getattr(metadata, "rpath", ()) or getattr(metadata, "runpath", ())
    ):
        raise ExternalToolError(
            f"{name} contains a deployment RPATH/RUNPATH; runtime selection "
            "must remain a deployment concern"
        )
    needed = set(getattr(metadata, "needed", ()))
    if probe.static and needed:
        raise ExternalToolError(
            f"{name} is not fully static; DT_NEEDED contains: "
            + ", ".join(sorted(needed))
        )
    if isinstance(qualification.runtime, LlvmRuntimeLinkEvidence):
        forbidden = needed.intersection(qualification.runtime.forbidden_sonames)
        if forbidden:
            raise ExternalToolError(
                f"{name} depends on forbidden GCC runtime SONAMEs: "
                + ", ".join(sorted(forbidden))
            )
        if (
            "cxx" in name
            and not probe.static
            and not any(soname.startswith("libc++.so") for soname in needed)
        ):
            raise ExternalToolError(
                f"{name} does not record the selected shared libc++ runtime"
            )


def _expected_link_checks(
    qualification: _LinkQualification,
) -> tuple[str, ...]:
    if qualification.cxx_only:
        return (
            "cxx-executable",
            "cxx-shared-exception",
            "cxx-static-exception",
        )
    if qualification.runtime is not None:
        return _RUNTIME_LINK_VALIDATION_CHECKS
    return _LINK_VALIDATION_CHECKS


def _qualify_binding_links(
    qualification: _LinkQualification,
) -> dict[str, object]:
    validation = (
        qualification.output / f".link-validation{qualification.validation_suffix}"
    )
    validation.mkdir()
    completed: list[str] = []
    inspector = ReadElfInspector()
    try:
        _verify_redundant_stdlib_argument(qualification, validation)
        for spec in _link_probe_specs(qualification):
            probe = _run_link_probe(qualification, validation, spec)
            _verify_link_evidence_roots(qualification, probe)
            _verify_sdk_link_inputs(qualification, probe)
            _verify_runtime_link_inputs(qualification, probe)
            _verify_linked_probe(qualification, probe, inspector)
            completed.append(spec.name)
    finally:
        shutil.rmtree(validation, ignore_errors=True)
    if tuple(completed) != _expected_link_checks(qualification):
        raise AssertionError("binding link validation check set is inconsistent")
    return {"status": "passed", "checks": completed}


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
    cxx_compile_flags: tuple[str, ...] = (),
    cxx_link_flags: tuple[str, ...] = (),
    environment: Mapping[str, str] | None = None,
    redundant_clang_stdlib: str | None = None,
    additional_runtime_roots: tuple[Path, ...] = (),
    forbidden_runtime_roots: tuple[Path, ...] = (),
    cxx_only: bool = False,
    validation_suffix: str = "",
) -> dict[str, object]:
    return _qualify_binding_links(
        _LinkQualification(
            cc_wrapper=cc_wrapper,
            cxx_wrapper=cxx_wrapper,
            output=output,
            sysroot=sysroot,
            overlay=overlay,
            target_arch=target_arch,
            expected_interpreter=expected_interpreter,
            runtime=runtime,
            linker_executable=linker_executable,
            cxx_compile_flags=cxx_compile_flags,
            cxx_link_flags=cxx_link_flags,
            environment=environment,
            redundant_clang_stdlib=redundant_clang_stdlib,
            additional_runtime_roots=additional_runtime_roots,
            forbidden_runtime_roots=forbidden_runtime_roots,
            cxx_only=cxx_only,
            validation_suffix=validation_suffix,
        )
    )


def _verify_alternate_runtime_links(
    *,
    compiler: CompilerInfo,
    cxx_wrapper: Path,
    output: Path,
    sysroot: Path,
    overlay: Path,
    target_arch: str,
    expected_interpreter: str,
    runtimes: RuntimeBindingSet,
    linker_executable: Path | None,
) -> dict[str, object]:
    if compiler.family != "clang":
        return {"status": "passed", "choices": []}
    all_evidence = {
        kind: runtime_link_evidence(runtime) for kind, runtime in runtimes.bindings
    }
    completed: dict[str, object] = {}
    compatible_target = f"{target_arch}-pc-linux"
    for kind, runtime in runtimes.bindings:
        if kind == runtimes.default_kind:
            continue
        evidence = all_evidence[kind]
        other_roots = tuple(
            other.runtime_root
            for other_kind, other in all_evidence.items()
            if other_kind != kind
        )
        completed[kind] = _verify_binding_links(
            cc_wrapper=cxx_wrapper,
            cxx_wrapper=cxx_wrapper,
            output=output,
            sysroot=sysroot,
            overlay=overlay,
            target_arch=target_arch,
            expected_interpreter=expected_interpreter,
            runtime=evidence,
            linker_executable=linker_executable,
            cxx_compile_flags=(
                "-Werror=unused-command-line-argument",
                f"--target={compatible_target}",
            ),
            cxx_link_flags=(f"--target={compatible_target}",),
            environment={**os.environ, "LINUX_TOOLCHAIN_CXX_RUNTIME": kind},
            redundant_clang_stdlib=kind,
            additional_runtime_roots=other_roots,
            forbidden_runtime_roots=other_roots,
            cxx_only=True,
            validation_suffix=(
                "-libcxx" if isinstance(runtime, LlvmRuntimeBinding) else "-libstdcxx"
            ),
        )
    return {"status": "passed", "choices": completed}


def _verify_linker_choices(
    *,
    compiler: CompilerInfo,
    cc_wrapper: Path,
    output: Path,
    sysroot: Path,
    overlay: Path,
    target_arch: str,
    expected_interpreter: str,
    choices: tuple[str, ...],
) -> dict[str, object]:
    """Link one C executable with each non-default published linker."""

    if not choices:
        return {"status": "passed", "choices": []}
    validation = output / ".linker-validation"
    validation.mkdir()
    source = validation / "main.c"
    object_path = validation / "main.o"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    completed: list[str] = []
    inspector = ReadElfInspector()
    try:
        run([cc_wrapper, "-c", source, "-o", object_path])
        for name in choices:
            binary = validation / name
            map_path = validation / f"{name}.map"
            linker_driver = cc_wrapper
            selector = (f"-fuse-ld={name}",)
            if name == "mold" and compiler.family == "gcc" and compiler.major < 12:
                linker_driver = output / "bin" / "cc-mold"
                selector = ()
            trace_option = "-Wl,--trace" if name == "mold" else "-Wl,-t"
            result = run(
                [
                    linker_driver,
                    *selector,
                    object_path,
                    f"-Wl,-Map,{map_path}",
                    trace_option,
                    "-o",
                    binary,
                ]
            )
            try:
                evidence = map_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError as error:
                raise ExternalToolError(
                    f"{name} did not produce validation map {map_path}: {error}"
                ) from error
            evidence = "\n".join((evidence, result.stdout, result.stderr))
            for startfile in ("crti.o", "crtn.o"):
                if not _map_uses_path(evidence, overlay, startfile):
                    raise ExternalToolError(
                        f"{name} link did not select SDK {startfile}"
                    )
            if not _map_uses_path(evidence, sysroot, "libc.so"):
                raise ExternalToolError(f"{name} link did not select SDK libc")
            metadata = inspector.inspect(binary)
            if (
                metadata.machine != target_arch
                or metadata.elf_class != "ELF64"
                or metadata.endianness != "little"
                or metadata.interpreter != expected_interpreter
            ):
                raise ExternalToolError(
                    f"{name} did not produce the expected target executable"
                )
            completed.append(name)
    finally:
        shutil.rmtree(validation, ignore_errors=True)
    return {"status": "passed", "choices": completed}


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _linker_script_path(path: Path) -> str:
    value = _single_line_value(str(path), "linker script path")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _create_llvm_linker_entries(
    output: Path,
    runtime: LlvmRuntimeBinding,
) -> None:
    """Render libunwind entry points with its static transitive dependencies."""

    static_unwind = next(
        (path for path in runtime.static_libraries if path.name == "libunwind.a"),
        None,
    )
    shared_unwind = next(
        (path for path in runtime.shared_libraries if path.name == "libunwind.so"),
        None,
    )
    if static_unwind is None or shared_unwind is None:
        raise ConfigurationError(
            "LLVM runtime has no libunwind static and shared linker entry points"
        )
    output.mkdir(parents=True)
    (output / "libunwind.a").write_text(
        "EXTERN(_Unwind_Resume)\n"
        "EXTERN(dladdr)\n"
        "GROUP (\n"
        f"  {_linker_script_path(static_unwind)}\n"
        "  -lpthread\n"
        "  -ldl\n"
        ")\n",
        encoding="utf-8",
    )
    (output / "libunwind.so").write_text(
        f"INPUT (\n  {_linker_script_path(shared_unwind)}\n)\n",
        encoding="utf-8",
    )


def _create_clang_runtime_layout(
    output: Path,
    runtime: LlvmRuntimeBinding,
) -> None:
    """Expose libc++ where Clang's native -stdlib=libc++ lookup expects it."""

    if len(runtime.cxx_include_dirs) != 1:
        raise ConfigurationError(
            "LLVM runtime must publish one canonical libc++ include directory"
        )
    include_root = output / "include" / "c++"
    include_root.mkdir(parents=True)
    (include_root / "v1").symlink_to(runtime.cxx_include_dirs[0])


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


def _install_gcc_mold_drivers(
    staging: Path,
    output: Path,
    compiler: CompilerInfo,
    tools: _BindingTools,
    sdk: _BindingSdk,
    cc_flags: _WrapperDriverFlags,
    cxx_flags: _WrapperDriverFlags,
    suffix_flags: tuple[str, ...],
) -> tuple[str, ...]:
    """Provide a fixed Mold selector for GCC versions predating -fuse-ld=mold."""

    if compiler.family != "gcc" or compiler.major >= 12 or "mold" not in tools.linkers:
        return ()

    selector = staging / "libexec" / "mold"
    selector.mkdir()
    for name in ("ld", f"{compiler.target}-ld"):
        (selector / name).symlink_to("../ld.mold")

    final_selector = output / "libexec" / "mold"
    mold_cc_flags = _WrapperDriverFlags(
        always=(f"-B{final_selector}/", *cc_flags.always),
        link_only=cc_flags.link_only,
    )
    mold_cxx_flags = _WrapperDriverFlags(
        always=(f"-B{final_selector}/", *cxx_flags.always),
        link_only=cxx_flags.link_only,
    )
    bin_dir = staging / "bin"
    _write_executable(
        bin_dir / "cc-mold",
        _wrapper_text(
            tools.cc,
            compiler.family,
            sdk.sysroot,
            mold_cc_flags,
            suffix_flags,
        ),
    )
    _write_executable(
        bin_dir / "c++-mold",
        _wrapper_text(
            tools.cxx,
            compiler.family,
            sdk.sysroot,
            mold_cxx_flags,
            suffix_flags,
        ),
    )
    return ("c++-mold", "cc-mold")


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
    manifest = read_json_object(manifest_path, "SDK manifest")
    manifest_format = manifest.get("format")
    if (
        manifest.get("schema") != SDK_MANIFEST_SCHEMA
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
    runtimes: RuntimeBindingSet | None,
    toolchain: ManagedCompilerToolchain | None,
) -> None:
    if output in {Path("/"), Path.home().resolve()}:
        raise ConfigurationError(f"invalid binding output path: {output}")
    protected = [("SDK", sdk.root)]
    if runtimes is not None:
        protected.extend(
            (f"runtime export {index}", root)
            for index, root in enumerate(runtimes.export_roots)
        )
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
    if toolchain is not None and runtimes is not None:
        for root in runtimes.export_roots:
            if _paths_overlap(toolchain.kit.root, root):
                raise ConfigurationError(
                    "Compiler Kit and runtime export directories must not "
                    "contain one another"
                )

    for context, path in (
        ("SDK path", sdk.root),
        ("binding output path", output),
        *(
            tuple(
                (f"runtime export path {index}", root)
                for index, root in enumerate(runtimes.export_roots)
            )
            if runtimes is not None
            else ()
        ),
    ):
        _single_line_value(str(path), context)


def _validate_binding_compatibility(
    sdk: _BindingSdk,
    compiler: CompilerInfo,
    runtimes: RuntimeBindingSet | None,
    toolchain: ManagedCompilerToolchain | None,
) -> None:
    target = sdk.target
    target_arch = sdk.spec.arch
    if target_architecture(compiler.target) != target_arch:
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

    if runtimes is None:
        return
    if (
        compiler.family == "clang"
        and len(runtimes.bindings) > 1
        and runtimes.default_kind != "libstdc++"
    ):
        raise ConfigurationError(
            "Clang bindings with both C++ runtimes use libstdc++ by default"
        )
    for _, runtime in runtimes.bindings:
        _validate_runtime_compatibility(
            sdk,
            compiler,
            runtime,
            toolchain,
        )


def _validate_runtime_compatibility(
    sdk: _BindingSdk,
    compiler: CompilerInfo,
    runtime: RuntimeBinding,
    toolchain: ManagedCompilerToolchain | None,
) -> None:
    target = sdk.target
    target_arch = sdk.spec.arch
    manifest = runtime.manifest
    if (
        manifest.arch != target_arch
        or target_architecture(manifest.target) != target_arch
    ):
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
    runtimes: RuntimeBindingSet | None,
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
    selected_libcxx = requested.libcxx
    if (
        selected_libcxx is None
        and runtimes is not None
        and runtimes.default_kind == "libc++"
    ):
        selected_libcxx = "libc++"
    if selected_libcxx == "libc++":
        if runtimes is None or runtimes.find("libc++") is None:
            raise ConfigurationError(
                "Conan libcxx='libc++' requires a published LLVM runtime"
            )
        libcxx = selected_libcxx
    else:
        if selected_libcxx not in {None, "libstdc++", "libstdc++11"}:
            raise ConfigurationError(
                "GCC-compatible runtime requires a libstdc++ Conan ABI setting"
            )
        if runtimes is not None and runtimes.find("libstdc++") is None:
            raise ConfigurationError("Conan libstdc++ requires a published GCC runtime")
        libcxx = selected_libcxx or "libstdc++11"
    return selected, replace(requested, libcxx=libcxx)


def _runtime_manifest_entry(runtime: RuntimeBinding) -> dict[str, object]:
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
        return {
            "policy": "pinned-gcc-runtime",
            "kind": "libstdc++",
            **common,
        }
    return {
        "policy": "pinned-llvm-runtime",
        "kind": "libc++",
        **common,
        "source": dict(manifest.source),
        "abi": _json_compatible(manifest.abi),
        "forbidden_sonames": list(manifest.forbidden_sonames),
        "validation": _json_compatible(manifest.validation),
    }


def _runtime_manifest_data(
    runtimes: RuntimeBindingSet | None,
) -> dict[str, object]:
    if runtimes is None:
        return {
            "default": "compiler-default",
            "available": [
                {
                    "policy": "external-unpinned",
                    "kind": "compiler-default",
                    "note": (
                        "C++ runtime symbol requirements are audited but are not "
                        "bounded by this glibc-floor binding."
                    ),
                }
            ],
        }
    return {
        "default": runtimes.default_kind,
        "available": [
            _runtime_manifest_entry(runtime) for _, runtime in runtimes.bindings
        ],
    }


def _compiler_toolchain_manifest(
    toolchain: ManagedCompilerToolchain | None,
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
    glibc_floor = str(target["libc_version"])
    forbidden_versions = ["GLIBC_PRIVATE"]
    if AbiVersion.parse(glibc_floor) < GLIBC_DT_RELR_MIN_VERSION:
        forbidden_versions.append("GLIBC_ABI_DT_RELR")
    return AuditPolicy.for_glibc_floor(
        glibc_floor,
        machine=target_arch,
        forbidden_versions=forbidden_versions,
    )


def _binding_tools(
    compiler: CompilerInfo,
    runtimes: RuntimeBindingSet | None,
    toolchain: ManagedCompilerToolchain | None,
) -> _BindingTools:
    if toolchain is None:
        cc = capture_executable_identity(compiler.cc, "C compiler driver")
        cxx = capture_executable_identity(compiler.cxx, "C++ compiler driver")
        target_tools = resolve_compiler_target_tools(compiler)
        linkers = (
            {"default": resolve_compiler_tool(compiler, "ld")}
            if runtimes is not None
            else {}
        )
    else:
        cc = ExecutableIdentity(invocation_path=toolchain.kit.cc.invocation_path)
        cxx = ExecutableIdentity(invocation_path=toolchain.kit.cxx.invocation_path)
        target_tools = toolchain.target_tools
        linkers = dict(toolchain.linkers) if runtimes is not None else {}

    selected_target_tools = target_tool_mapping(target_tools)
    selected_tools: dict[str, ArchiveTool] = {
        "ar": target_tools.ar,
        "ranlib": target_tools.ranlib,
        **selected_target_tools,
    }
    selected_tools.update(
        {_LINKER_TOOL_NAMES[name]: linker for name, linker in linkers.items()}
    )
    return _BindingTools(
        cc=cc,
        cxx=cxx,
        target_tools=target_tools,
        linkers=linkers,
        selected_target_tools=selected_target_tools,
        selected_tools=selected_tools,
    )


def _binding_driver_flags(
    *,
    compiler: CompilerInfo,
    runtimes: RuntimeBindingSet | None,
    sdk: _BindingSdk,
    output: Path,
    library_dirs: tuple[Path, ...],
) -> tuple[_WrapperDriverFlags, _WrapperDriverFlags, tuple[str, ...]]:
    final_bin = output / "bin"
    final_overlay = output / "glibc-startfiles"
    if runtimes is None:
        common = _WrapperDriverFlags(
            always=(f"-B{final_bin}/",),
            link_only=(
                f"-B{final_overlay}/",
                *(f"-L{directory}" for directory in library_dirs),
            ),
        )
        cc_flags = cxx_flags = common
    else:
        llvm_runtime = runtimes.find("libc++")
        runtime_link_dir = (
            output / "runtime-link"
            if isinstance(llvm_runtime, LlvmRuntimeBinding)
            else None
        )
        cc_flags = _runtime_wrapper_flags(
            compiler=compiler,
            runtimes=runtimes,
            sysroot=sdk.sysroot,
            overlay=final_overlay,
            tool_dir=final_bin,
            runtime_link_dir=runtime_link_dir,
            sdk_library_dirs=library_dirs,
            cxx=False,
        )
        cxx_flags = _runtime_wrapper_flags(
            compiler=compiler,
            runtimes=runtimes,
            sysroot=sdk.sysroot,
            overlay=final_overlay,
            tool_dir=final_bin,
            runtime_link_dir=runtime_link_dir,
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
    runtimes: RuntimeBindingSet | None,
    toolchain: ManagedCompilerToolchain | None,
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
                "c": [*cc_flags.always, *cc_flags.link_only, *suffix_flags],
                "cxx": [*cxx_flags.always, *cxx_flags.link_only, *suffix_flags],
            },
        },
        "cxx_runtimes": _runtime_manifest_data(runtimes),
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


@dataclass(frozen=True)
class _BindingPlan:
    sdk: _BindingSdk
    output: Path
    compiler: CompilerInfo
    runtimes: RuntimeBindingSet | None
    toolchain: ManagedCompilerToolchain | None
    managed_evidence: Mapping[str, object] | None
    integrations: tuple[IntegrationName, ...]
    conan: ConanSettings | None
    tools: _BindingTools
    library_dirs: tuple[Path, ...]
    cc_flags: _WrapperDriverFlags
    cxx_flags: _WrapperDriverFlags
    suffix_flags: tuple[str, ...]


@dataclass(frozen=True)
class _BindingMaterialization:
    manifest: Mapping[str, object]
    interpreter: str


def _plan_binding(
    sdk: Path,
    output: Path,
    compiler: CompilerInfo,
    *,
    runtime: Path | RuntimeBinding | RuntimeBindingSet | None = None,
    toolchain: ManagedCompilerToolchain | None = None,
    managed_evidence: Mapping[str, object] | None = None,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    force: bool = False,
) -> _BindingPlan:
    sdk_input = _load_binding_sdk(sdk)
    raw_output = output.expanduser()
    if raw_output.is_symlink():
        raise ConfigurationError(f"binding output cannot be a symlink: {raw_output}")
    publish_output = raw_output.resolve()
    runtime_binding = (
        runtime
        if isinstance(runtime, (GccRuntimeBinding, LlvmRuntimeBinding))
        else load_runtime_binding(runtime)
        if runtime is not None and not isinstance(runtime, RuntimeBindingSet)
        else None
    )
    runtime_inputs = (
        runtime
        if isinstance(runtime, RuntimeBindingSet)
        else single_runtime_binding_set(runtime_binding)
        if runtime_binding is not None
        else None
    )
    if managed_evidence is not None and toolchain is None:
        raise ConfigurationError(
            "managed binding evidence requires a managed Compiler Kit"
        )
    _validate_binding_layout(publish_output, sdk_input, runtime_inputs, toolchain)
    _validate_binding_compatibility(
        sdk_input,
        compiler,
        runtime_inputs,
        toolchain,
    )
    selected_integrations, conan_settings = _resolve_binding_integrations(
        integrations,
        conan,
        runtime_inputs,
    )
    _prepare_binding_output(publish_output, force=force)

    tools = _binding_tools(compiler, runtime_inputs, toolchain)
    library_dirs = sdk_library_dirs(sdk_input.sysroot)
    cc_flags, cxx_flags, suffix_flags = _binding_driver_flags(
        compiler=compiler,
        runtimes=runtime_inputs,
        sdk=sdk_input,
        output=publish_output,
        library_dirs=library_dirs,
    )
    return _BindingPlan(
        sdk=sdk_input,
        output=publish_output,
        compiler=compiler,
        runtimes=runtime_inputs,
        toolchain=toolchain,
        managed_evidence=managed_evidence,
        integrations=selected_integrations,
        conan=conan_settings,
        tools=tools,
        library_dirs=library_dirs,
        cc_flags=cc_flags,
        cxx_flags=cxx_flags,
        suffix_flags=suffix_flags,
    )


def _materialize_binding(
    staging: Path,
    plan: _BindingPlan,
) -> _BindingMaterialization:
    bin_dir = staging / "bin"
    final_bin = plan.output / "bin"
    bin_dir.mkdir(parents=True)
    (staging / ".linux-toolchain-binding").write_text(
        f"format={BINDING_FORMAT}\n",
        encoding="utf-8",
    )

    final_tool_paths = {name: final_bin / name for name in plan.tools.selected_tools}
    for name, tool in plan.tools.selected_tools.items():
        _link_tool(bin_dir / name, tool, final_bin=final_bin)

    startfiles = _create_startfile_overlay(
        staging / "glibc-startfiles",
        plan.library_dirs,
    )
    if plan.runtimes is not None:
        if "default" not in plan.tools.linkers:
            raise AssertionError("runtime binding has no default linker")
        for name in plan.tools.linkers:
            command = _LINKER_TOOL_NAMES[name]
            _write_executable(
                staging / "libexec" / command,
                _linker_wrapper_text(
                    final_bin / command,
                    (*plan.runtimes.library_dirs, *plan.library_dirs),
                ),
            )
        llvm_runtime = plan.runtimes.find("libc++")
        if isinstance(llvm_runtime, LlvmRuntimeBinding):
            _create_llvm_linker_entries(
                staging / "runtime-link",
                llvm_runtime,
            )
            _create_clang_runtime_layout(staging, llvm_runtime)

    _write_executable(
        bin_dir / "cc",
        _wrapper_text(
            plan.tools.cc,
            plan.compiler.family,
            plan.sdk.sysroot,
            plan.cc_flags,
            plan.suffix_flags,
        ),
    )
    _write_executable(
        bin_dir / "c++",
        _wrapper_text(
            plan.tools.cxx,
            plan.compiler.family,
            plan.sdk.sysroot,
            plan.cxx_flags,
            plan.suffix_flags,
            selectable_cxx_runtimes=(
                tuple(kind for kind, _ in plan.runtimes.bindings)
                if plan.runtimes is not None
                else ()
            ),
        ),
    )
    mold_driver_names = _install_gcc_mold_drivers(
        staging,
        plan.output,
        plan.compiler,
        plan.tools,
        plan.sdk,
        plan.cc_flags,
        plan.cxx_flags,
        plan.suffix_flags,
    )
    alias_names = _install_driver_aliases(
        bin_dir,
        family=plan.compiler.family,
        cc_wrapper=bin_dir / "cc",
        cxx_wrapper=bin_dir / "c++",
        target_tool_names=tuple(final_tool_paths),
    )
    alias_names = tuple(sorted((*alias_names, *mold_driver_names)))

    interpreter = _write_audit_policy(
        staging / "audit-policy.json",
        plan.sdk.target,
        plan.sdk.spec.arch,
    )
    integration_inputs = _binding_integration_inputs(
        sdk=plan.sdk,
        output=plan.output,
        compiler=plan.compiler,
        tools=plan.tools,
        library_dirs=plan.library_dirs,
        conan=plan.conan,
    )
    integration_paths = render_integrations(
        staging,
        integration_inputs.context,
        integrations=plan.integrations,
        shell=integration_inputs.shell,
        conan=integration_inputs.conan,
    )

    return _BindingMaterialization(
        manifest=_binding_manifest(
            sdk=plan.sdk,
            output=plan.output,
            compiler=plan.compiler,
            runtimes=plan.runtimes,
            toolchain=plan.toolchain,
            managed_evidence=plan.managed_evidence,
            tools=plan.tools,
            cc_flags=plan.cc_flags,
            cxx_flags=plan.cxx_flags,
            suffix_flags=plan.suffix_flags,
            aliases=alias_names,
            integrations=plan.integrations,
            integration_paths=integration_paths,
            conan=integration_inputs.conan,
            library_dirs=plan.library_dirs,
            startfiles=startfiles,
        ),
        interpreter=interpreter,
    )


def _qualify_binding(
    published: Path,
    plan: _BindingPlan,
    materialized: _BindingMaterialization,
) -> None:
    archive = _verify_archive_tools(
        cc_wrapper=published / "bin" / "cc",
        ar_wrapper=published / "bin" / "ar",
        ranlib_wrapper=published / "bin" / "ranlib",
        output=published,
        target_arch=plan.sdk.spec.arch,
        expected_interpreter=materialized.interpreter,
    )
    target_validation = _verify_target_tools(
        wrappers={
            name: published / "bin" / name for name in plan.tools.selected_target_tools
        },
        output=published,
        target_arch=plan.sdk.spec.arch,
    )
    default_linker = (
        plan.tools.linkers["default"].invocation_path
        if "default" in plan.tools.linkers
        else None
    )
    default_runtime = (
        runtime_link_evidence(plan.runtimes.default)
        if plan.runtimes is not None
        else None
    )
    other_runtime_roots = (
        tuple(
            runtime.runtime_root
            for kind, runtime in plan.runtimes.bindings
            if kind != plan.runtimes.default_kind
        )
        if plan.runtimes is not None
        else ()
    )
    default_compile_flags: tuple[str, ...] = ()
    default_link_flags: tuple[str, ...] = ()
    if plan.compiler.family == "clang" and plan.runtimes is not None:
        compatible_target = f"{plan.sdk.spec.arch}-pc-linux"
        default_compile_flags = (
            "-Werror=unused-command-line-argument",
            f"--target={compatible_target}",
        )
        default_link_flags = (f"--target={compatible_target}",)
    links = _verify_binding_links(
        cc_wrapper=published / "bin" / "cc",
        cxx_wrapper=published / "bin" / "c++",
        output=published,
        sysroot=plan.sdk.sysroot,
        overlay=published / "glibc-startfiles",
        target_arch=plan.sdk.spec.arch,
        expected_interpreter=materialized.interpreter,
        runtime=default_runtime,
        linker_executable=default_linker,
        cxx_compile_flags=default_compile_flags,
        cxx_link_flags=default_link_flags,
        redundant_clang_stdlib=(
            plan.runtimes.default_kind
            if plan.compiler.family == "clang" and plan.runtimes is not None
            else None
        ),
        additional_runtime_roots=other_runtime_roots,
        forbidden_runtime_roots=other_runtime_roots,
    )
    runtime_choices = (
        _verify_alternate_runtime_links(
            compiler=plan.compiler,
            cxx_wrapper=published / "bin" / "c++",
            output=published,
            sysroot=plan.sdk.sysroot,
            overlay=published / "glibc-startfiles",
            target_arch=plan.sdk.spec.arch,
            expected_interpreter=materialized.interpreter,
            runtimes=plan.runtimes,
            linker_executable=default_linker,
        )
        if plan.runtimes is not None
        else {"status": "passed", "choices": {}}
    )
    linker_choices = _verify_linker_choices(
        compiler=plan.compiler,
        cc_wrapper=published / "bin" / "cc",
        output=published,
        sysroot=plan.sdk.sysroot,
        overlay=published / "glibc-startfiles",
        target_arch=plan.sdk.spec.arch,
        expected_interpreter=materialized.interpreter,
        choices=tuple(name for name in plan.tools.linkers if name != "default"),
    )
    manifest = {
        **materialized.manifest,
        "validation": {
            "status": "passed",
            "links": links,
            "runtime_choices": runtime_choices,
            "linker_choices": linker_choices,
            "archive": archive,
            "target_tools": target_validation,
        },
    }
    manifest_path = published / "binding.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o644)


def create_binding_from_inputs(
    sdk: Path,
    output: Path,
    compiler: CompilerInfo,
    *,
    runtime: Path | RuntimeBinding | RuntimeBindingSet | None = None,
    toolchain: ManagedCompilerToolchain | None = None,
    managed_evidence: Mapping[str, object] | None = None,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    conan: ConanSettings | None = None,
    force: bool = False,
) -> Path:
    """Create a binding from already resolved compiler and runtime inputs."""

    plan = _plan_binding(
        sdk,
        output,
        compiler,
        runtime=runtime,
        toolchain=toolchain,
        managed_evidence=managed_evidence,
        integrations=integrations,
        conan=conan,
        force=force,
    )

    try:
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        staging_owner = tempfile.TemporaryDirectory(
            prefix=f".{plan.output.name}.staging-",
            dir=plan.output.parent,
        )
    except OSError as error:
        raise ConfigurationError(
            f"cannot create binding staging directory beside {plan.output}: {error}"
        ) from error

    try:
        staging = Path(staging_owner.name)
        materialized = _materialize_binding(staging, plan)
        _publish_binding(
            staging,
            plan.output,
            validate=lambda published: _qualify_binding(
                published,
                plan,
                materialized,
            ),
        )
    finally:
        staging_owner.cleanup()
    return plan.output / "binding.json"


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

    return create_binding_from_inputs(
        sdk,
        output,
        compiler,
        runtime=runtime,
        integrations=integrations,
        conan=conan,
        force=force,
    )
