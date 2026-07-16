from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable, Iterable, Sequence

from linux_toolchain.elf.models import ElfMetadata, VersionNeed
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.process import run

ELF_MAGIC = b"\x7fELF"


def _walk_directory(root: Path, *, recursive: bool) -> tuple[Path, ...]:
    if not recursive:
        try:
            return tuple(path for path in root.iterdir() if path.is_file())
        except OSError as error:
            raise ExternalToolError(
                f"cannot walk audit input {root}: {error}"
            ) from error

    files: list[Path] = []

    def fail_walk(error: OSError) -> None:
        raise ExternalToolError(f"cannot walk audit input {root}: {error}") from error

    try:
        for directory, dirnames, filenames in os.walk(
            root,
            topdown=True,
            onerror=fail_walk,
            followlinks=False,
        ):
            directory_path = Path(directory)
            for name in dirnames:
                candidate = directory_path / name
                if candidate.is_symlink():
                    # Following blindly can escape the artifact tree or loop;
                    # silently skipping can produce a false PASS.  Require the
                    # caller to audit the canonical directory explicitly.
                    raise ExternalToolError(
                        "audit input contains a directory symlink; refusing "
                        f"an incomplete traversal: {candidate}"
                    )
            files.extend(directory_path / name for name in filenames)
    except ExternalToolError:
        raise
    except OSError as error:
        raise ExternalToolError(f"cannot walk audit input {root}: {error}") from error
    return tuple(files)


def is_elf(path: Path | str) -> bool:
    candidate = Path(path)
    try:
        with candidate.open("rb") as stream:
            return stream.read(len(ELF_MAGIC)) == ELF_MAGIC
    except OSError as error:
        raise ExternalToolError(f"cannot inspect {candidate}: {error}") from error


def discover_elf_files(
    paths: Iterable[Path | str] | Path | str,
    *,
    recursive: bool = True,
) -> tuple[Path, ...]:
    if isinstance(paths, (str, os.PathLike)):
        inputs = [Path(paths)]
    else:
        inputs = [Path(path) for path in paths]

    discovered: dict[Path, Path] = {}
    for input_path in inputs:
        try:
            root = input_path.expanduser().resolve(strict=True)
        except OSError as error:
            raise ConfigurationError(
                f"cannot access audit input {input_path}: {error}"
            ) from error

        if root.is_file():
            candidates = (root,)
        elif root.is_dir():
            candidates = _walk_directory(root, recursive=recursive)
        else:
            raise ConfigurationError(f"audit input is not a file or directory: {root}")

        for candidate in candidates:
            if not is_elf(candidate):
                continue
            try:
                canonical = candidate.resolve(strict=True)
            except OSError as error:
                raise ExternalToolError(
                    f"cannot resolve ELF file {candidate}: {error}"
                ) from error
            discovered.setdefault(canonical, canonical)

    return tuple(sorted(discovered.values(), key=lambda path: path.as_posix()))


def _normalize_machine(raw: str, elf_class: str) -> str:
    upper = raw.upper()
    if "EM_X86_64" in upper or "X86-64" in upper or "X86_64" in upper:
        return "x86_64"
    if "EM_386" in upper or "INTEL 80386" in upper:
        return "x86"
    if "EM_AARCH64" in upper or "AARCH64" in upper:
        return "aarch64"
    if "EM_ARM" in upper or upper == "ARM":
        return "arm"
    if "RISCV" in upper or "RISC-V" in upper:
        return "riscv64" if elf_class == "ELF64" else "riscv32"
    if "EM_PPC64" in upper or "POWERPC64" in upper:
        return "ppc64"
    return raw.strip()


def _normalize_elf_type(raw: str) -> str:
    upper = raw.upper()
    if upper.startswith("REL") or "RELOCATABLE" in upper:
        return "REL"
    if upper.startswith("EXEC") or "EXECUTABLE" in upper:
        return "EXEC"
    if upper.startswith("DYN") or "SHAREDOBJECT" in upper or "SHARED OBJECT" in upper:
        return "DYN"
    if upper.startswith("CORE"):
        return "CORE"
    return "UNKNOWN"


def _version_needs(output: str) -> tuple[VersionNeed, ...]:
    """Parse only .gnu.version_r; version definitions are intentionally ignored."""

    in_needs = False
    library: str | None = None
    needs: set[VersionNeed] = set()

    for line in output.splitlines():
        if re.match(r"^\s*Version needs section\b", line, re.IGNORECASE) or re.match(
            r"^\s*VersionRequirements\s*\[", line, re.IGNORECASE
        ):
            in_needs = True
            library = None
            continue

        if in_needs and (
            re.match(
                r"^\s*Version (?:definition|definitions|symbols) section\b",
                line,
                re.IGNORECASE,
            )
            or re.match(r"^\s*VersionDefinitions\s*\[", line, re.IGNORECASE)
        ):
            break
        if not in_needs:
            continue

        file_match = re.search(r"\b(?:File|FileName):\s*([^\s\]]+)", line)
        if file_match:
            library = file_match.group(1)
        name_match = re.search(r"\bName:\s*([A-Za-z][A-Za-z0-9_.+-]*)", line)
        if name_match:
            needs.add(VersionNeed(library=library, name=name_match.group(1)))

    return tuple(sorted(needs, key=lambda need: ((need.library or ""), need.name)))


_DYNAMIC_LINE = re.compile(
    r"^\s*0x[0-9a-fA-F]+\s+\(?([A-Z][A-Z0-9_]*)\)?(?:\s+|$)(.*)$"
)


def _dynamic_metadata(
    output: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None, bool]:
    needed: list[str] = []
    rpath: list[str] = []
    runpath: list[str] = []
    soname: str | None = None
    has_dt_relr = False

    for line in output.splitlines():
        match = _DYNAMIC_LINE.match(line)
        if not match:
            continue
        tag = match.group(1)
        if tag.startswith("DT_"):
            tag = tag[3:]
        value = match.group(2)

        if tag == "NEEDED":
            library_match = re.search(r"\[([^]]+)\]", value)
            if library_match:
                needed.append(library_match.group(1))
        elif tag in {"RPATH", "RUNPATH"}:
            path_match = re.search(r"\[([^]]*)\]", value)
            if path_match:
                # Empty entries are meaningful: the dynamic loader treats them
                # as the process working directory, so the policy must see them.
                entries = tuple(path_match.group(1).split(":"))
                (rpath if tag == "RPATH" else runpath).extend(entries)
        elif tag == "SONAME":
            soname_match = re.search(r"\[([^]]+)\]", value)
            if soname_match:
                soname = soname_match.group(1)
        elif tag == "RELR":
            has_dt_relr = True

    return (
        tuple(dict.fromkeys(needed)),
        tuple(rpath),
        tuple(runpath),
        soname,
        has_dt_relr,
    )


def parse_readelf_output(path: Path | str, output: str) -> ElfMetadata:
    elf_class_match = re.search(
        r"^\s*Class:\s*(ELF(?:32|64))\s*$", output, re.MULTILINE
    )
    machine_match = re.search(r"^\s*Machine:\s*(.+?)\s*$", output, re.MULTILINE)
    data_match = re.search(
        r"^\s*(?:Data|DataEncoding):\s*(.+?)\s*$",
        output,
        re.MULTILINE,
    )
    if not elf_class_match or not machine_match or not data_match:
        raise ExternalToolError(f"readelf returned an incomplete ELF header for {path}")

    elf_class = elf_class_match.group(1)
    elf_type_match = re.search(r"^\s*Type:\s*(.+?)\s*$", output, re.MULTILINE)
    elf_type = (
        _normalize_elf_type(elf_type_match.group(1)) if elf_type_match else "UNKNOWN"
    )
    machine = _normalize_machine(machine_match.group(1), elf_class)
    raw_data = data_match.group(1).lower()
    if "little" in raw_data:
        endianness = "little"
    elif "big" in raw_data:
        endianness = "big"
    else:
        raise ExternalToolError(
            f"readelf returned an unknown ELF data encoding for {path}: "
            f"{data_match.group(1)!r}"
        )
    interpreter_match = re.search(
        r"Requesting program interpreter:\s*([^\]\r\n]+)", output
    )
    needed, rpath, runpath, soname, has_dt_relr = _dynamic_metadata(output)

    return ElfMetadata(
        path=Path(path),
        elf_class=elf_class,
        endianness=endianness,
        elf_type=elf_type,
        machine=machine,
        interpreter=(interpreter_match.group(1).strip() if interpreter_match else None),
        needed=needed,
        soname=soname,
        rpath=rpath,
        runpath=runpath,
        has_dt_relr=has_dt_relr,
        version_needs=_version_needs(output),
    )


def parse_readelf_archive_headers(
    path: Path | str, output: str
) -> tuple[ElfMetadata, ...]:
    """Parse every ELF member header emitted by ``readelf -h archive.a``."""

    archive = Path(path)
    members: list[ElfMetadata] = []
    parts = re.split(r"(?m)^File:\s+", output)
    for part in parts[1:]:
        member_name, separator, header = part.partition("\n")
        if not separator or not member_name.strip():
            continue
        label = member_name.strip()
        member_path = Path(label if "(" in label else f"{archive}({label})")
        members.append(parse_readelf_output(member_path, header))
    if not members:
        raise ExternalToolError(
            f"readelf returned no ELF archive member headers for {archive}"
        )
    return tuple(members)


def resolve_readelf_candidates(
    tools: Sequence[str | os.PathLike[str]] | None = None,
    *,
    resolver: Callable[[str], str | None] | None = None,
) -> tuple[str, ...]:
    """Resolve available readelf implementations in the product-wide order."""

    candidates = (
        tuple(os.fspath(tool) for tool in tools)
        if tools is not None
        else tuple(
            candidate
            for candidate in (
                os.environ.get("LINUX_TOOLCHAIN_READELF"),
                os.environ.get("READELF"),
                "readelf",
                "llvm-readelf",
            )
            if candidate
        )
    )
    which = resolver or shutil.which
    available: list[str] = []
    seen_candidates: set[str] = set()
    for candidate in candidates:
        if candidate in seen_candidates:
            continue
        seen_candidates.add(candidate)
        resolved = which(candidate)
        if resolved and resolved not in available:
            available.append(resolved)
    return tuple(available)


class ReadElfInspector:
    """Inspect ELF files with GNU readelf, falling back to llvm-readelf."""

    def __init__(self, tools: Sequence[str | os.PathLike[str]] | None = None) -> None:
        self._tools = resolve_readelf_candidates(tools)

    def inspect(self, path: Path | str) -> ElfMetadata:
        elf_path = Path(path)
        if not is_elf(elf_path):
            raise ConfigurationError(f"not an ELF file: {elf_path}")

        if not self._tools:
            raise ExternalToolError(
                "neither GNU readelf nor llvm-readelf is available; "
                "set LINUX_TOOLCHAIN_READELF to an executable"
            )

        failures: list[str] = []
        for tool in self._tools:
            try:
                environment = dict(os.environ)
                environment["LC_ALL"] = "C"
                result = run(
                    [tool, "-W", "-h", "-l", "-d", "-V", elf_path],
                    env=environment,
                )
                return parse_readelf_output(elf_path, result.stdout)
            except ExternalToolError as error:
                failures.append(f"{tool}: {error}")
        raise ExternalToolError(
            f"all readelf implementations failed for {elf_path}:\n"
            + "\n".join(failures)
        )

    def inspect_archive(self, path: Path | str) -> tuple[ElfMetadata, ...]:
        archive = Path(path)
        if not archive.is_file():
            raise ConfigurationError(f"not an archive file: {archive}")

        if not self._tools:
            raise ExternalToolError(
                "neither GNU readelf nor llvm-readelf is available; "
                "set LINUX_TOOLCHAIN_READELF to an executable"
            )

        failures: list[str] = []
        for tool in self._tools:
            try:
                environment = dict(os.environ)
                environment["LC_ALL"] = "C"
                result = run([tool, "-W", "-h", archive], env=environment)
                return parse_readelf_archive_headers(archive, result.stdout)
            except ExternalToolError as error:
                failures.append(f"{tool}: {error}")
        raise ExternalToolError(
            f"all readelf implementations failed for archive {archive}:\n"
            + "\n".join(failures)
        )
