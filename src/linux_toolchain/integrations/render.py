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

from collections.abc import Sequence
from pathlib import Path

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.integrations.cmake import CMAKE_TOOLCHAIN_PATH, render_cmake
from linux_toolchain.integrations.conan import render_conan
from linux_toolchain.integrations.models import (
    DEFAULT_INTEGRATIONS,
    SUPPORTED_INTEGRATIONS,
    ConanIntegrationConfig,
    IntegrationContext,
    IntegrationName,
    RenderedPaths,
    ShellIntegrationConfig,
)
from linux_toolchain.integrations.shell import render_shell


def render_integrations(
    destination: Path,
    context: IntegrationContext,
    *,
    integrations: Sequence[IntegrationName] = DEFAULT_INTEGRATIONS,
    shell: ShellIntegrationConfig = ShellIntegrationConfig(),
    conan: ConanIntegrationConfig | None = None,
) -> RenderedPaths:
    """Render selected adapters and return their artifact-relative entry points."""

    selected = tuple(integrations)
    unknown = sorted(set(selected).difference(SUPPORTED_INTEGRATIONS))
    if unknown:
        raise ConfigurationError("unknown integrations: " + ", ".join(unknown))
    if len(set(selected)) != len(selected):
        raise ConfigurationError("integrations must not contain duplicates")
    if "conan" in selected and conan is None:
        raise ConfigurationError("Conan integration requires a complete config")
    if "conan" not in selected and conan is not None:
        raise ConfigurationError(
            "Conan config was supplied without selecting the Conan integration"
        )

    result: RenderedPaths = {}
    for name in selected:
        if name == "cmake":
            rendered = render_cmake(destination, context)
        elif name == "shell":
            rendered = render_shell(
                destination,
                context,
                config=shell,
                cmake_toolchain=(CMAKE_TOOLCHAIN_PATH if "cmake" in selected else None),
            )
        else:
            if conan is None:  # Narrow the type after validation above.
                raise AssertionError("Conan integration config is missing")
            rendered = render_conan(destination, context, conan)
        duplicates = sorted(set(result).intersection(rendered))
        if duplicates:
            raise ConfigurationError(
                "integration entry-point names collide: " + ", ".join(duplicates)
            )
        used_paths = set(result.values())
        collisions = sorted(
            (path for path in rendered.values() if path in used_paths), key=str
        )
        if collisions:
            raise ConfigurationError(
                "integration output paths collide: "
                + ", ".join(str(path) for path in collisions)
            )
        result.update(rendered)
    return result
