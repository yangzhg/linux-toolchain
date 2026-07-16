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

from pathlib import Path

from linux_toolchain.integrations.models import IntegrationContext


def bracket_argument(value: Path) -> str:
    """Quote a path as a collision-free CMake bracket argument."""

    text = str(value)
    delimiter = "="
    while f"]{delimiter}]" in text:
        delimiter += "="
    return f"[{delimiter}[{text}]{delimiter}]"


def tool_pins_text(
    context: IntegrationContext,
    *,
    include_compilers: bool,
    include_sysroot: bool,
) -> str:
    """Render the target tools selected by the binding."""

    variables = {
        "CMAKE_AR": context.tools["ar"],
        "CMAKE_RANLIB": context.tools["ranlib"],
        "CMAKE_ASM_COMPILER": context.cc,
        "CMAKE_NM": context.tools["nm"],
        "CMAKE_STRIP": context.tools["strip"],
        "CMAKE_OBJCOPY": context.tools["objcopy"],
        "CMAKE_OBJDUMP": context.tools["objdump"],
        "CMAKE_C_COMPILER_AR": context.tools["ar"],
        "CMAKE_CXX_COMPILER_AR": context.tools["ar"],
        "CMAKE_ASM_COMPILER_AR": context.tools["ar"],
        "CMAKE_C_COMPILER_RANLIB": context.tools["ranlib"],
        "CMAKE_CXX_COMPILER_RANLIB": context.tools["ranlib"],
        "CMAKE_ASM_COMPILER_RANLIB": context.tools["ranlib"],
    }
    if include_compilers:
        variables["CMAKE_C_COMPILER"] = context.cc
        variables["CMAKE_CXX_COMPILER"] = context.cxx
    if context.linker is not None:
        variables["CMAKE_LINKER"] = context.linker
    if include_sysroot:
        variables.update(
            {
                "CMAKE_SYSROOT": context.sysroot,
                "CMAKE_SYSROOT_COMPILE": context.sysroot,
                "CMAKE_SYSROOT_LINK": context.sysroot,
            }
        )
    return (
        "# Target tools selected by linux-toolchain.\n"
        + "\n".join(
            f"set({variable} {bracket_argument(path)} CACHE FILEPATH "
            '"Target tool selected by linux-toolchain" FORCE)'
            for variable, path in variables.items()
        )
        + "\n"
    )
