// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "smoke_library.h"

#include <dlfcn.h>

#include <array>
#include <iostream>
#include <string>
#include <thread>

namespace {

using Transform = std::size_t (*)(const char*, char*, std::size_t);

bool transformMatches(Transform transform) {
  std::array<char, 128> output{};
  return linux_toolchain_smoke_asm() == 17 &&
      transform("smoke", output.data(), output.size()) != 0 &&
      std::string(output.data()) == "toolchain:smoke";
}

} // namespace

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: linux_toolchain_smoke <shared-library>\n";
    return 2;
  }

  bool threadPassed = false;
  std::thread worker([&threadPassed] {
    threadPassed = transformMatches(&linux_toolchain_smoke_transform);
  });
  worker.join();
  if (!threadPassed) {
    std::cerr << "direct shared-library call failed\n";
    return 3;
  }

  void* handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
  if (handle == nullptr) {
    std::cerr << "dlopen failed: " << dlerror() << '\n';
    return 4;
  }

  dlerror();
  auto transform = reinterpret_cast<Transform>(
      dlsym(handle, "linux_toolchain_smoke_transform"));
  const char* lookupError = dlerror();
  if (lookupError != nullptr || transform == nullptr ||
      !transformMatches(transform)) {
    std::cerr << "dlsym call failed: "
              << (lookupError == nullptr ? "invalid result" : lookupError)
              << '\n';
    dlclose(handle);
    return 5;
  }

  if (dlclose(handle) != 0) {
    std::cerr << "dlclose failed: " << dlerror() << '\n';
    return 6;
  }

  std::cout << "linux-toolchain-smoke: ok\n";
  return 0;
}
