from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from linux_toolchain.compiler.managed import CompilerKit
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.models import classify_linux_glibc_target
from linux_toolchain.process import run
from linux_toolchain.versions import major_version


@dataclass(frozen=True)
class CompilerInfo:
    family: str
    version: str
    major: int
    target: str
    cc: Path
    cxx: Path
    version_text: str


@dataclass(frozen=True)
class ExecutableIdentity:
    """Invocation path for one externally supplied executable."""

    invocation_path: Path


@dataclass(frozen=True)
class ArchiveTool:
    """One compiler-selected target tool.

    ``invocation_path`` deliberately preserves the selected symlink name.  Some
    LLVM binutils are multicall binaries whose behavior depends on argv[0].
    """

    reported_program: str
    invocation_path: Path


@dataclass(frozen=True)
class ArchiveTools:
    ar: ArchiveTool
    ranlib: ArchiveTool


@dataclass(frozen=True)
class TargetTools(ArchiveTools):
    """Complete compiler-selected target binary-tool set."""

    assembler: ArchiveTool
    nm: ArchiveTool
    strip: ArchiveTool
    objcopy: ArchiveTool
    objdump: ArchiveTool


@dataclass(frozen=True)
class _ManagedCompilerToolchain:
    """Manifest-backed target tools selected for a managed binding.

    External bindings select target tools through the compiler driver. Managed
    bindings populate this exclusively from a validated Compiler Kit, so target
    tools never fall back to the host PATH.
    """

    kit: CompilerKit
    target_tools: TargetTools
    linker: ArchiveTool


def _target_arch(target: str) -> str:
    """Classify an external compiler target for existing binding callers."""

    return classify_linux_glibc_target(
        target,
        policy="external",
        context="compiler target",
    )


def _resolve_executable(value: str | Path) -> Path:
    raw = str(value)
    candidate = shutil.which(raw) if "/" not in raw else raw
    if not candidate:
        raise ConfigurationError(f"compiler executable not found: {raw}")
    # Preserve the invocation basename.  In particular, clang++ is commonly a
    # symlink to clang; resolving that symlink would silently switch the C++
    # wrapper back to C-driver mode and omit libstdc++ from final links.
    path = Path(os.path.abspath(Path(candidate).expanduser()))
    if not path.is_file():
        raise ConfigurationError(f"compiler executable is not a file: {path}")
    return path


def _capture_executable_identity(value: str | Path, context: str) -> ExecutableIdentity:
    invocation_path = Path(os.path.abspath(Path(value).expanduser()))
    if not invocation_path.is_file() or not os.access(invocation_path, os.X_OK):
        raise ConfigurationError(
            f"{context} is not an executable file: {invocation_path}"
        )
    return ExecutableIdentity(invocation_path=invocation_path)


def _resolve_compiler_tool(compiler: CompilerInfo, program: str) -> ArchiveTool:
    result = run([compiler.cc, f"-print-prog-name={program}"])
    raw = result.stdout.strip()
    if not raw or "\n" in raw or "\r" in raw:
        raise ConfigurationError(
            f"compiler did not report one {program} executable: {raw!r}"
        )
    candidate = shutil.which(raw) if "/" not in raw else raw
    if not candidate:
        raise ConfigurationError(f"compiler {program} executable not found: {raw}")

    # Preserve the invocation path. Resolving llvm-ranlib to llvm-ar before
    # execution would change its multicall mode.
    invocation_path = Path(os.path.abspath(Path(candidate).expanduser()))
    if not invocation_path.is_file() or not os.access(invocation_path, os.X_OK):
        raise ConfigurationError(
            f"compiler {program} executable is not an executable file: "
            f"{invocation_path}"
        )
    return ArchiveTool(
        reported_program=raw,
        invocation_path=invocation_path,
    )


def _resolve_compiler_target_tools(compiler: CompilerInfo) -> TargetTools:
    """Resolve the complete target tool set selected by the compiler driver."""

    return TargetTools(
        ar=_resolve_compiler_tool(compiler, "ar"),
        ranlib=_resolve_compiler_tool(compiler, "ranlib"),
        assembler=_resolve_compiler_tool(compiler, "as"),
        nm=_resolve_compiler_tool(compiler, "nm"),
        strip=_resolve_compiler_tool(compiler, "strip"),
        objcopy=_resolve_compiler_tool(compiler, "objcopy"),
        objdump=_resolve_compiler_tool(compiler, "objdump"),
    )


def _target_tools(tools: TargetTools) -> dict[str, ArchiveTool]:
    return {
        "as": tools.assembler,
        "nm": tools.nm,
        "strip": tools.strip,
        "objcopy": tools.objcopy,
        "objdump": tools.objdump,
    }


def _compiler_family(version_text: str) -> str:
    lowered = version_text.lower()
    if "clang" in lowered:
        return "clang"
    if "gcc" in lowered or "g++" in lowered:
        return "gcc"
    raise ConfigurationError("only GCC and Clang bindings are supported")


def _extract_version(version_text: str, family: str) -> str:
    patterns = (
        [r"(?:clang version|clang\s+version)\s+([0-9]+(?:\.[0-9]+)+)"]
        if family == "clang"
        else [r"\)\s+([0-9]+(?:\.[0-9]+)+)", r"\b([0-9]+(?:\.[0-9]+)+)\b"]
    )
    for pattern in patterns:
        match = re.search(pattern, version_text, re.IGNORECASE)
        if match:
            return match.group(1)
    raise ConfigurationError(f"cannot parse {family} version")


def detect_compiler(cc: str | Path, cxx: str | Path) -> CompilerInfo:
    cc_path = _resolve_executable(cc)
    cxx_path = _resolve_executable(cxx)

    def probe(path: Path) -> tuple[str, str, str, str]:
        version_result = run([path, "--version"])
        version_text = (version_result.stdout or version_result.stderr).strip()
        family = _compiler_family(version_text)
        version = _extract_version(version_text, family)
        target = run([path, "-dumpmachine"]).stdout.strip()
        _target_arch(target)
        return family, version, target, version_text

    family, version, target, version_text = probe(cxx_path)
    cc_family, cc_version, cc_target, _ = probe(cc_path)
    if (cc_family, cc_version, cc_target) != (family, version, target):
        raise ConfigurationError(
            "C and C++ compiler drivers do not match: "
            f"cc=({cc_family}, {cc_version}, {cc_target}), "
            f"cxx=({family}, {version}, {target})"
        )
    major = major_version(version)
    minimum = 10 if family == "gcc" else 16
    if major < minimum:
        raise ConfigurationError(
            f"Linux toolchain requires {family} {minimum} or newer; detected {version}"
        )
    return CompilerInfo(
        family=family,
        version=version,
        major=major,
        target=target,
        cc=cc_path,
        cxx=cxx_path,
        version_text=version_text,
    )


def _managed_compiler_info(kit: CompilerKit) -> CompilerInfo:
    """Verify Compiler Kit drivers without rediscovering their target."""

    provider = kit.manifest.provider
    expected_family = provider["name"]
    expected_version = provider["version"]
    expected_major = provider["major"]
    if (
        not isinstance(expected_family, str)
        or not isinstance(expected_version, str)
        or not isinstance(expected_major, int)
    ):
        raise ConfigurationError("compiler kit provider is invalid")

    def probe(path: Path, driver: str) -> str:
        result = run([path, "--version"])
        version_text = (result.stdout or result.stderr).strip()
        family = _compiler_family(version_text)
        version = _extract_version(version_text, family)
        if family != expected_family or version != expected_version:
            raise ConfigurationError(
                f"compiler kit {driver} identity mismatch: manifest declares "
                f"{expected_family} {expected_version}, driver reports "
                f"{family} {version}"
            )
        return version_text

    probe(kit.cc.invocation_path, "C driver")
    version_text = probe(kit.cxx.invocation_path, "C++ driver")
    return CompilerInfo(
        family=expected_family,
        version=expected_version,
        major=expected_major,
        target=kit.manifest.target["triplet"],
        cc=kit.cc.invocation_path,
        cxx=kit.cxx.invocation_path,
        version_text=version_text,
    )


def _managed_archive_tool(kit: CompilerKit, name: str) -> ArchiveTool:
    executable = kit.target_tools[name]
    return ArchiveTool(
        reported_program=executable.relative_path,
        invocation_path=executable.invocation_path,
    )


def _managed_toolchain(kit: CompilerKit) -> _ManagedCompilerToolchain:
    target_tools = TargetTools(
        ar=_managed_archive_tool(kit, "ar"),
        ranlib=_managed_archive_tool(kit, "ranlib"),
        assembler=_managed_archive_tool(kit, "as"),
        nm=_managed_archive_tool(kit, "nm"),
        strip=_managed_archive_tool(kit, "strip"),
        objcopy=_managed_archive_tool(kit, "objcopy"),
        objdump=_managed_archive_tool(kit, "objdump"),
    )
    return _ManagedCompilerToolchain(
        kit=kit,
        target_tools=target_tools,
        linker=_managed_archive_tool(kit, "ld"),
    )
