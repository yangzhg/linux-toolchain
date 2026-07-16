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

from pathlib import Path, PurePosixPath

from linux_toolchain.integrations._cmake import bracket_argument, tool_pins_text
from linux_toolchain.integrations._io import write_rendered_files
from linux_toolchain.integrations.models import IntegrationContext, RenderedPaths

CMAKE_TOOLCHAIN_PATH = PurePosixPath("cmake/toolchain.cmake")


def render_cmake(destination: Path, context: IntegrationContext) -> RenderedPaths:
    """Render a complete standalone CMake toolchain file."""

    processor = {"x86_64": "x86_64", "aarch64": "aarch64"}[context.architecture]
    content = f"""# Complete CMake toolchain for direct, standalone use.
if(DEFINED CMAKE_SYSTEM_NAME AND NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR
    "linux-toolchain: CMAKE_SYSTEM_NAME was overridden: ${{CMAKE_SYSTEM_NAME}}")
endif()
if(DEFINED CMAKE_SYSTEM_PROCESSOR AND
   NOT CMAKE_SYSTEM_PROCESSOR STREQUAL "{processor}")
  message(FATAL_ERROR
    "linux-toolchain: CMAKE_SYSTEM_PROCESSOR was overridden: "
    "${{CMAKE_SYSTEM_PROCESSOR}} != {processor}")
endif()
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR {processor})
{tool_pins_text(context, include_compilers=True, include_sysroot=True)}
list(PREPEND CMAKE_FIND_ROOT_PATH {bracket_argument(context.sysroot)})
list(REMOVE_DUPLICATES CMAKE_FIND_ROOT_PATH)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
"""
    write_rendered_files(destination, {CMAKE_TOOLCHAIN_PATH: content})
    return {"cmake_toolchain": CMAKE_TOOLCHAIN_PATH}
