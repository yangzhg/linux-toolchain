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

import os
import shlex
from pathlib import Path, PurePosixPath

from linux_toolchain.integrations._io import (
    create_rendered_directory,
    validate_relative_path,
    write_rendered_files,
)
from linux_toolchain.integrations.models import (
    IntegrationContext,
    RenderedPaths,
    ShellIntegrationConfig,
)

SHELL_ENVIRONMENT_PATH = PurePosixPath("env/toolchain.env")
SHELL_EMPTY_PKG_CONFIG_PATH = PurePosixPath("env/empty-pkgconfig")


def render_shell(
    destination: Path,
    context: IntegrationContext,
    *,
    config: ShellIntegrationConfig = ShellIntegrationConfig(),
    cmake_toolchain: PurePosixPath | None = None,
) -> RenderedPaths:
    """Render the POSIX shell environment for Make and configure-style builds."""

    empty_pkg_config = context.binding_root / SHELL_EMPTY_PKG_CONFIG_PATH
    pkg_config_dirs = config.pkg_config_dirs or (empty_pkg_config,)
    if empty_pkg_config in pkg_config_dirs:
        create_rendered_directory(destination, SHELL_EMPTY_PKG_CONFIG_PATH)

    values = {
        "LINUX_TOOLCHAIN_TARGET": context.target,
        "LINUX_TOOLCHAIN_SYSROOT": str(context.sysroot),
        "CC": str(context.cc),
        "CXX": str(context.cxx),
        "AR": str(context.tools["ar"]),
        "RANLIB": str(context.tools["ranlib"]),
        "AS": str(context.tools["as"]),
        "NM": str(context.tools["nm"]),
        "STRIP": str(context.tools["strip"]),
        "OBJCOPY": str(context.tools["objcopy"]),
        "OBJDUMP": str(context.tools["objdump"]),
        "PKG_CONFIG_SYSROOT_DIR": str(context.sysroot),
        "PKG_CONFIG_LIBDIR": os.pathsep.join(str(path) for path in pkg_config_dirs),
    }
    if cmake_toolchain is not None:
        validate_relative_path(cmake_toolchain)
        values["CMAKE_TOOLCHAIN_FILE"] = str(context.binding_root / cmake_toolchain)
    if context.linker is not None:
        values["LD"] = str(context.linker)
    linker_reset = "" if context.linker is not None else "unset LD\n"
    assignments = "\n".join(
        f"export {name}={shlex.quote(value)}" for name, value in values.items()
    )
    quoted_bin = shlex.quote(str(context.binding_root / "bin"))
    content = f"""# Source this file in a POSIX shell before invoking a target build.
{assignments}
{linker_reset}if [ -n "${{PATH-}}" ]; then
  export PATH={quoted_bin}:"$PATH"
else
  export PATH={quoted_bin}
fi
unset PKG_CONFIG_PATH
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_RUN_PATH
unset COMPILER_PATH GCC_EXEC_PREFIX CCC_OVERRIDE_OPTIONS
"""
    write_rendered_files(destination, {SHELL_ENVIRONMENT_PATH: content})
    return {"environment": SHELL_ENVIRONMENT_PATH}
