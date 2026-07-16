from __future__ import annotations

import re

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.build_script_common import BuildSelection
from linux_toolchain.managed.gcc_build_script import render_gcc_build_script
from linux_toolchain.managed.llvm_build_script import render_llvm_build_script


def render_build_script(
    selection: BuildSelection,
    *,
    triplet: str,
    backend_triplet: str,
    backend_version: str,
    paired_runtime: bool = False,
) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", triplet):
        raise ConfigurationError("managed build target triplet is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", backend_triplet):
        raise ConfigurationError("managed compiler backend triplet is invalid")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", backend_version):
        raise ConfigurationError("managed compiler backend version is invalid")
    if paired_runtime and selection.artifact_kind != "compiler-kit":
        raise ConfigurationError(
            "paired runtime output requires a Compiler Kit selection"
        )
    if selection.family == "gcc":
        return render_gcc_build_script(
            selection,
            triplet,
            backend_triplet,
            backend_version,
            paired_runtime=paired_runtime,
        )
    if selection.family == "clang":
        return render_llvm_build_script(
            selection,
            triplet,
            backend_triplet,
            backend_version,
            paired_runtime=paired_runtime,
        )
    raise ConfigurationError(f"unsupported managed compiler family: {selection.family}")
