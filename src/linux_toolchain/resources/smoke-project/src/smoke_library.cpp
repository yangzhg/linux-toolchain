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

#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

std::string exceptionRoundTrip(const std::string& value) {
  try {
    throw std::runtime_error(value);
  } catch (const std::runtime_error& error) {
    return error.what();
  }
}

} // namespace

extern "C" std::size_t linux_toolchain_smoke_transform(
    const char* input,
    char* output,
    std::size_t output_size) {
  if (input == nullptr || output == nullptr) {
    return 0;
  }

  const std::string source(input);
  std::vector<char> copied(source.size() + 1);
  void* (*volatile copy)(void*, const void*, std::size_t) = &std::memcpy;
  copy(copied.data(), source.c_str(), copied.size());

  const std::string result =
      "toolchain:" + exceptionRoundTrip(std::string(copied.data()));
  if (output_size <= result.size()) {
    return 0;
  }
  copy(output, result.c_str(), result.size() + 1);
  return result.size();
}
