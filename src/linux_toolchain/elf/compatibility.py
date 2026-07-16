from __future__ import annotations

from pathlib import Path

from linux_toolchain.elf.models import ElfMetadata
from linux_toolchain.errors import ExternalToolError
from linux_toolchain.versions import AbiVersion

# glibc 2.36 is the first released glibc loader with DT_RELR support.
GLIBC_DT_RELR_MIN_VERSION = AbiVersion.parse("2.36")


def validate_dt_relr_compatibility(
    path: Path,
    metadata: ElfMetadata,
    floor: AbiVersion,
) -> None:
    if floor >= GLIBC_DT_RELR_MIN_VERSION:
        return
    if metadata.has_dt_relr:
        raise ExternalToolError(
            f"{path} uses DT_RELR, unsupported by glibc floor {floor}"
        )
    if any(need.name == "GLIBC_ABI_DT_RELR" for need in metadata.version_needs):
        raise ExternalToolError(
            f"{path} requires GLIBC_ABI_DT_RELR, unsupported by glibc floor {floor}"
        )
