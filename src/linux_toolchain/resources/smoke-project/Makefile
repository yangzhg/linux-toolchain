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

BUILD_DIR ?= build
BUILD_TYPE ?= Release
ARTIFACT_DIR := $(BUILD_DIR)/artifacts
OBJECT_DIR := $(BUILD_DIR)/objects

ifeq ($(BUILD_TYPE),Debug)
  BUILD_TYPE_FLAGS := -O0 -g
else ifeq ($(BUILD_TYPE),Release)
  BUILD_TYPE_FLAGS := -O3 -DNDEBUG
else ifeq ($(BUILD_TYPE),RelWithDebInfo)
  BUILD_TYPE_FLAGS := -O2 -g -DNDEBUG
else ifeq ($(BUILD_TYPE),MinSizeRel)
  BUILD_TYPE_FLAGS := -Os -DNDEBUG
else
  $(error unsupported BUILD_TYPE: $(BUILD_TYPE))
endif

CPPFLAGS := -Isrc
CXXFLAGS := -std=c++17 -fPIC -pthread $(BUILD_TYPE_FLAGS)
LDFLAGS := -pthread

LIBRARY := $(ARTIFACT_DIR)/liblinux_toolchain_smoke.so
EXECUTABLE := $(ARTIFACT_DIR)/linux_toolchain_smoke
OBJECTS := $(OBJECT_DIR)/smoke_library.o $(OBJECT_DIR)/smoke_asm.o

.PHONY: all
all: $(LIBRARY) $(EXECUTABLE)

$(OBJECT_DIR) $(ARTIFACT_DIR):
	mkdir -p "$@"

$(OBJECT_DIR)/smoke_library.o: src/smoke_library.cpp src/smoke_library.h | $(OBJECT_DIR)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c "$<" -o "$@"

$(OBJECT_DIR)/smoke_asm.o: src/smoke_asm.S | $(OBJECT_DIR)
	$(CC) $(CPPFLAGS) -fPIC -c "$<" -o "$@"

$(LIBRARY): $(OBJECTS) | $(ARTIFACT_DIR)
	$(CXX) -shared $(LDFLAGS) $(OBJECTS) -o "$@"

$(EXECUTABLE): src/smoke_main.cpp src/smoke_library.h $(LIBRARY) | $(ARTIFACT_DIR)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) $(LDFLAGS) "$<" -L"$(ARTIFACT_DIR)" \
		-llinux_toolchain_smoke -ldl -Wl,-rpath,'$$ORIGIN' -o "$@"
