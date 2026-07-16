from __future__ import annotations

import json
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath
from typing import Mapping

from linux_toolchain.errors import ConfigurationError, ExternalToolError

LICENSE_DIRECTORY = "licenses"
LICENSE_MANIFEST_FILE = "license-manifest.json"
LICENSE_EVIDENCE_FORMAT = 1

_LICENSE_NAME = re.compile(
    r"^(?:copying|copyright|licen[cs]e|notice)(?:[._-].*)?$",
    re.IGNORECASE,
)
_COMPONENT_REQUIREMENTS = {
    "binutils": ("COPYING",),
    "gcc": ("COPYING", "COPYING.RUNTIME"),
    "glibc": ("COPYING", "COPYING.LIB"),
    "linux": ("COPYING",),
}
_MANAGED_LLVM_REQUIREMENTS = (
    "llvm-project/llvm/LICENSE.TXT",
    "llvm-project/clang/LICENSE.TXT",
    "llvm-project/compiler-rt/LICENSE.TXT",
    "llvm-project/libcxx/LICENSE.TXT",
    "llvm-project/libcxxabi/LICENSE.TXT",
    "llvm-project/libunwind/LICENSE.TXT",
)


def _relative_path(value: object, *, context: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ConfigurationError(f"{context} must be a non-empty POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ConfigurationError(f"{context} must be a normalized relative path")
    return value


def _license_path(path: PurePosixPath) -> bool:
    return any(part.lower() == "licenses" for part in path.parts[:-1]) or bool(
        _LICENSE_NAME.fullmatch(path.name)
    )


def _archive_relative_path(member_name: str) -> PurePosixPath | None:
    path = PurePosixPath(member_name.removeprefix("./"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ExternalToolError(
            f"source archive contains an invalid member path: {member_name!r}"
        )
    if len(path.parts) < 2:
        return None
    return PurePosixPath(*path.parts[1:])


def extract_component_licenses(
    archive: Path,
    destination: Path,
    component: str,
) -> tuple[Path, ...]:
    """Extract license files from one already verified source archive."""

    requirements = _COMPONENT_REQUIREMENTS.get(component)
    if requirements is None:
        raise ConfigurationError(f"unsupported license component: {component}")
    if not archive.is_file():
        raise ConfigurationError(f"license source archive is missing: {archive}")
    component_root = destination / LICENSE_DIRECTORY / component
    if component_root.exists():
        raise ConfigurationError(
            f"license destination already exists: {component_root}"
        )
    component_root.mkdir(parents=True)
    copied: list[Path] = []
    try:
        with tarfile.open(archive, mode="r:*") as source:
            for member in sorted(source.getmembers(), key=lambda item: item.name):
                relative = _archive_relative_path(member.name)
                if relative is None or member.isdir() or not _license_path(relative):
                    continue
                if not member.isreg():
                    raise ExternalToolError(
                        f"source archive license entry is not a regular file: {member.name}"
                    )
                stream = source.extractfile(member)
                if stream is None:
                    raise ExternalToolError(
                        f"cannot read source archive license file: {member.name}"
                    )
                output = component_root.joinpath(*relative.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                if output.exists():
                    raise ExternalToolError(
                        f"source archive has a duplicate license path: {relative}"
                    )
                with stream, output.open("wb") as target:
                    shutil.copyfileobj(stream, target)
                output.chmod(0o644)
                copied.append(output)
    except (OSError, tarfile.TarError) as error:
        raise ExternalToolError(
            f"cannot extract license material from {archive}: {error}"
        ) from error

    missing = tuple(
        name for name in requirements if not (component_root / name).is_file()
    )
    if missing:
        raise ExternalToolError(
            f"{component} source archive is missing required license files: "
            + ", ".join(missing)
        )
    return tuple(copied)


def _license_files(root: Path, *, context: str) -> tuple[str, ...]:
    licenses = root / LICENSE_DIRECTORY
    if not licenses.is_dir():
        raise ConfigurationError(f"{context} has no licenses directory")
    files = tuple(
        f"{LICENSE_DIRECTORY}/{path.relative_to(licenses).as_posix()}"
        for path in sorted(licenses.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    )
    if not files:
        raise ConfigurationError(f"{context} licenses directory is empty")
    return files


def license_evidence(root: Path, *, context: str) -> dict[str, object]:
    return {
        "format": LICENSE_EVIDENCE_FORMAT,
        "directory": LICENSE_DIRECTORY,
        "files": list(_license_files(root, context=context)),
    }


def managed_required_license_paths(
    family: str,
    *,
    compiler_kit: bool,
) -> tuple[str, ...]:
    if family == "gcc":
        required = ("gcc/COPYING", "gcc/COPYING.RUNTIME")
    elif family == "clang":
        required = _MANAGED_LLVM_REQUIREMENTS
    else:
        raise ConfigurationError(f"unsupported managed license family: {family}")
    return (*required, "binutils/COPYING") if compiler_kit else required


def sdk_required_license_paths() -> tuple[str, ...]:
    return tuple(
        f"{component}/{path}"
        for component, paths in _COMPONENT_REQUIREMENTS.items()
        for path in paths
    )


def require_license_files(
    root: Path,
    relative_paths: tuple[str, ...],
    *,
    context: str,
) -> None:
    missing: list[str] = []
    empty: list[str] = []
    for relative in relative_paths:
        path = (
            root
            / LICENSE_DIRECTORY
            / _relative_path(relative, context=f"{context} required license path")
        )
        if not path.is_file():
            missing.append(relative)
        elif path.stat().st_size == 0:
            empty.append(relative)
    if missing:
        raise ConfigurationError(
            f"{context} is missing required license files: " + ", ".join(missing)
        )
    if empty:
        raise ConfigurationError(
            f"{context} has empty required license files: " + ", ".join(empty)
        )


def validate_ubuntu_package_licenses(
    artifact_root: Path,
    compiler_payload: Path,
    *,
    context: str,
) -> None:
    """Require package copyright evidence for vendored host libraries."""

    host_libraries = compiler_payload / "lib/linux-toolchain-host"
    names = (
        {path.name for path in host_libraries.iterdir() if path.is_file()}
        if host_libraries.is_dir()
        else set()
    )
    dependencies = artifact_root / LICENSE_DIRECTORY / "ubuntu/dependencies.tsv"
    if names and not dependencies.is_file():
        raise ConfigurationError(
            f"{context} has vendored host libraries without Ubuntu package "
            "copyright evidence"
        )
    recorded: set[str] = set()
    if dependencies.is_file():
        for line in dependencies.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) != 5 or any(not field for field in fields):
                raise ConfigurationError(
                    f"{context} Ubuntu package license index is invalid"
                )
            library, _, _, _, copyright_path = fields
            if not (artifact_root / copyright_path).is_file():
                raise ConfigurationError(
                    f"{context} Ubuntu package copyright file is missing"
                )
            recorded.add(library)
    if recorded != names:
        raise ConfigurationError(
            f"{context} Ubuntu package license index does not cover every "
            "vendored host library"
        )


def validate_license_evidence(
    root: Path,
    value: object,
    *,
    context: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "format",
        "directory",
        "files",
    }:
        raise ConfigurationError(f"{context} license evidence is invalid")
    raw_files = value.get("files")
    if (
        value.get("format") != LICENSE_EVIDENCE_FORMAT
        or value.get("directory") != LICENSE_DIRECTORY
        or not isinstance(raw_files, list)
        or not all(isinstance(item, str) for item in raw_files)
    ):
        raise ConfigurationError(f"{context} license evidence format is invalid")
    actual = license_evidence(root, context=context)
    if raw_files != actual["files"]:
        raise ConfigurationError(f"{context} license file list does not match")
    return actual


def copy_license_directory(source_root: Path, destination_root: Path) -> None:
    source = source_root / LICENSE_DIRECTORY
    destination = destination_root / LICENSE_DIRECTORY
    if not source.is_dir():
        raise ConfigurationError("source artifact has no licenses directory")
    if destination.exists():
        raise ConfigurationError(
            f"license publication destination already exists: {destination}"
        )
    shutil.copytree(source, destination)


def publish_license_directory(
    source: Path | str,
    destination_root: Path,
    required_paths: tuple[str, ...],
    *,
    context: str,
) -> Path:
    source_root = Path(source).expanduser().resolve()
    if not source_root.is_dir():
        raise ConfigurationError(f"{context} license source is not a directory")
    require_license_files(source_root, required_paths, context=context)
    copy_license_directory(source_root, destination_root)
    return write_license_manifest(destination_root, context=context)


def write_license_manifest(root: Path, *, context: str) -> Path:
    evidence = license_evidence(root, context=context)
    path = root / LICENSE_MANIFEST_FILE
    if path.exists():
        raise ConfigurationError(f"{context} license manifest already exists")
    path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o644)
    return path


def validate_license_manifest(root: Path, *, context: str) -> dict[str, object]:
    path = root / LICENSE_MANIFEST_FILE
    if not path.is_file():
        raise ConfigurationError(f"{context} license manifest is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            f"cannot read {context} license manifest: {error}"
        ) from error
    return validate_license_evidence(root, value, context=context)
