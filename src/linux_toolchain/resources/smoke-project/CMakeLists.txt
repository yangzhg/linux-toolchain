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

cmake_minimum_required(VERSION 3.25)
project(linux_toolchain_smoke LANGUAGES CXX ASM)

find_package(Threads REQUIRED)

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/artifacts")
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/artifacts")

add_library(
  linux_toolchain_smoke_library SHARED src/smoke_library.cpp src/smoke_asm.S
)
target_include_directories(
  linux_toolchain_smoke_library PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/src"
)
set_target_properties(
  linux_toolchain_smoke_library
  PROPERTIES CXX_STANDARD 17
             CXX_STANDARD_REQUIRED YES
             CXX_EXTENSIONS NO
             OUTPUT_NAME linux_toolchain_smoke
)

add_executable(linux_toolchain_smoke src/smoke_main.cpp)
target_link_libraries(
  linux_toolchain_smoke
  PRIVATE linux_toolchain_smoke_library Threads::Threads ${CMAKE_DL_LIBS}
)
set_target_properties(
  linux_toolchain_smoke
  PROPERTIES CXX_STANDARD 17
             CXX_STANDARD_REQUIRED YES
             CXX_EXTENSIONS NO
             BUILD_RPATH "$ORIGIN"
             BUILD_RPATH_USE_ORIGIN YES
)
