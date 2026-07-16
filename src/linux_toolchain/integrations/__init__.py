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

"""Build-system adapters for generated Linux compiler bindings."""

from linux_toolchain.integrations.cmake import render_cmake
from linux_toolchain.integrations.conan import render_conan
from linux_toolchain.integrations.models import (
    DEFAULT_INTEGRATIONS,
    SUPPORTED_INTEGRATIONS,
    ConanIntegrationConfig,
    ConanSettings,
    IntegrationContext,
    IntegrationName,
    RenderedPaths,
    ShellIntegrationConfig,
)
from linux_toolchain.integrations.render import render_integrations
from linux_toolchain.integrations.shell import render_shell

__all__ = [
    "DEFAULT_INTEGRATIONS",
    "SUPPORTED_INTEGRATIONS",
    "ConanIntegrationConfig",
    "ConanSettings",
    "IntegrationContext",
    "IntegrationName",
    "RenderedPaths",
    "ShellIntegrationConfig",
    "render_cmake",
    "render_conan",
    "render_integrations",
    "render_shell",
]
