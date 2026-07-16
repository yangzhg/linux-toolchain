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

import json
from pathlib import Path, PurePosixPath

from linux_toolchain.integrations._cmake import bracket_argument, tool_pins_text
from linux_toolchain.integrations._io import write_rendered_files
from linux_toolchain.integrations.models import (
    ConanIntegrationConfig,
    IntegrationContext,
    RenderedPaths,
)

CONAN_PROFILE_PATH = PurePosixPath("conan/host.profile")
CONAN_CMAKE_TOOLCHAIN_PATH = PurePosixPath("conan/cmake-toolchain.cmake")
CONAN_CMAKE_LATE_PATH = PurePosixPath("conan/cmake-late.cmake")


def _early_toolchain(context: IntegrationContext) -> str:
    return f"""# Included early by Conan's generated CMakeToolchain.
{tool_pins_text(context, include_compilers=False, include_sysroot=False)}
set(_LINUX_TOOLCHAIN_CONAN_LATE
  "${{CMAKE_CURRENT_LIST_DIR}}/{CONAN_CMAKE_LATE_PATH.name}")
list(FIND CMAKE_PROJECT_TOP_LEVEL_INCLUDES
  "${{_LINUX_TOOLCHAIN_CONAN_LATE}}" _LINUX_TOOLCHAIN_CONAN_LATE_INDEX)
if(_LINUX_TOOLCHAIN_CONAN_LATE_INDEX EQUAL -1)
  list(APPEND CMAKE_PROJECT_TOP_LEVEL_INCLUDES
    "${{_LINUX_TOOLCHAIN_CONAN_LATE}}")
endif()
unset(_LINUX_TOOLCHAIN_CONAN_LATE)
unset(_LINUX_TOOLCHAIN_CONAN_LATE_INDEX)
"""


def _late_toolchain(context: IntegrationContext) -> str:
    return f"""# Applied after Conan's complete generated toolchain has been read.
{tool_pins_text(context, include_compilers=True, include_sysroot=True)}
get_filename_component(_LINUX_TOOLCHAIN_CONAN_GENERATORS
  "${{CMAKE_TOOLCHAIN_FILE}}" DIRECTORY)
list(PREPEND CMAKE_FIND_ROOT_PATH
  {bracket_argument(context.sysroot)}
  "${{_LINUX_TOOLCHAIN_CONAN_GENERATORS}}")
# Conan's absolute dependency paths are explicit target inputs. Admit those
# exact paths without enabling unrestricted host fallback.
foreach(_LINUX_TOOLCHAIN_CONAN_ROOT IN LISTS
    CMAKE_PREFIX_PATH
    CMAKE_LIBRARY_PATH
    CMAKE_INCLUDE_PATH)
  if(IS_ABSOLUTE "${{_LINUX_TOOLCHAIN_CONAN_ROOT}}" AND
      EXISTS "${{_LINUX_TOOLCHAIN_CONAN_ROOT}}")
    list(APPEND CMAKE_FIND_ROOT_PATH
      "${{_LINUX_TOOLCHAIN_CONAN_ROOT}}")
  endif()
endforeach()
list(REMOVE_DUPLICATES CMAKE_FIND_ROOT_PATH)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
unset(_LINUX_TOOLCHAIN_CONAN_GENERATORS)
unset(_LINUX_TOOLCHAIN_CONAN_ROOT)
"""


def _host_profile(context: IntegrationContext, config: ConanIntegrationConfig) -> str:
    architecture = {"x86_64": "x86_64", "aarch64": "armv8"}[context.architecture]
    executables = json.dumps(
        {
            "c": str(context.cc),
            "cpp": str(context.cxx),
            # ASM uses the compiler driver so preprocessed .S inputs receive
            # the same target and sysroot policy as C and C++.
            "asm": str(context.cc),
        },
        separators=(",", ":"),
    )
    user_toolchain = json.dumps(
        [str(context.binding_root / CONAN_CMAKE_TOOLCHAIN_PATH)],
        separators=(",", ":"),
    )
    linker_environment = f"LD={context.linker}\n" if context.linker is not None else ""
    return f"""[settings]
os=Linux
arch={architecture}
build_type={config.build_type}
os.libc=gnu
os.libc_version={config.glibc_version}
os.kernel_headers_version={config.linux_headers}
os.minimum_kernel_version={config.minimum_kernel}
compiler={config.compiler_family}
compiler.version={config.compiler_version}
compiler.cppstd={config.cppstd}
compiler.libcxx={config.libcxx}

[conf]
tools.build:sysroot={context.sysroot}
tools.build:compiler_executables={executables}
tools.cmake.cmaketoolchain:user_toolchain={user_toolchain}

[buildenv]
CC={context.cc}
CXX={context.cxx}
AR={context.tools["ar"]}
RANLIB={context.tools["ranlib"]}
AS={context.tools["as"]}
NM={context.tools["nm"]}
STRIP={context.tools["strip"]}
OBJCOPY={context.tools["objcopy"]}
OBJDUMP={context.tools["objdump"]}
{linker_environment}PATH=+(path){context.cc.parent}
"""


def render_conan(
    destination: Path,
    context: IntegrationContext,
    config: ConanIntegrationConfig,
) -> RenderedPaths:
    """Render an optional Conan host profile and its CMake composition files."""

    files = {
        CONAN_PROFILE_PATH: _host_profile(context, config),
        CONAN_CMAKE_TOOLCHAIN_PATH: _early_toolchain(context),
        CONAN_CMAKE_LATE_PATH: _late_toolchain(context),
    }
    write_rendered_files(destination, files)
    return {
        "conan_host_profile": CONAN_PROFILE_PATH,
        "conan_cmake_toolchain": CONAN_CMAKE_TOOLCHAIN_PATH,
        "conan_cmake_late": CONAN_CMAKE_LATE_PATH,
    }
