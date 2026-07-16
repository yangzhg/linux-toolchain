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
from typing import Mapping

from linux_toolchain.errors import ConfigurationError


def validate_relative_path(path: PurePosixPath) -> None:
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConfigurationError(f"invalid integration output path: {path}")


def write_rendered_files(destination: Path, files: Mapping[PurePosixPath, str]) -> None:
    """Write rendered integration files below ``destination``."""

    root = Path(destination).expanduser()
    try:
        if root.exists() and not root.is_dir():
            raise ConfigurationError(
                f"integration destination is not a directory: {root}"
            )
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        for relative in files:
            validate_relative_path(relative)
        for relative, content in files.items():
            path = root.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not path.is_file():
                raise ConfigurationError(f"integration output is not a file: {path}")
            path.write_text(content, encoding="utf-8", newline="\n")
            path.chmod(0o644)
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError(
            f"cannot render integrations below {root}: {error}"
        ) from error


def create_rendered_directory(destination: Path, relative: PurePosixPath) -> None:
    """Create one integration directory below a staging root."""

    validate_relative_path(relative)
    root = Path(destination).expanduser()
    try:
        if root.exists() and not root.is_dir():
            raise ConfigurationError(
                f"integration destination is not a directory: {root}"
            )
        root.joinpath(*relative.parts).mkdir(parents=True, exist_ok=True, mode=0o755)
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError(
            f"cannot create integration directory below {root}: {error}"
        ) from error
