from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from linux_toolchain.errors import ConfigurationError

_PUBLIC_DIRECTORY_MODE = 0o755
_PUBLIC_FILE_MODE = 0o644
_PUBLIC_EXECUTABLE_MODE = 0o755


def write_json_atomic(
    path: Path,
    value: Mapping[str, object],
    *,
    replace: bool = True,
) -> Path:
    """Write deterministic JSON without exposing a partial destination."""

    if path.is_symlink():
        raise ConfigurationError(f"JSON output cannot be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.tmp-",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary = Path(temporary_name)
        temporary.chmod(_PUBLIC_FILE_MODE)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
    except (OSError, TypeError, ValueError) as error:
        raise ConfigurationError(f"cannot write JSON output {path}: {error}") from error
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return path


def normalize_public_tree(root: Path) -> None:
    """Normalize modes in a generated artifact without following symlinks."""

    if not root.is_dir() or root.is_symlink():
        raise ConfigurationError(f"public artifact root is not a directory: {root}")
    try:
        root.chmod(_PUBLIC_DIRECTORY_MODE)
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                continue
            if path.is_dir():
                path.chmod(_PUBLIC_DIRECTORY_MODE)
            elif path.is_file():
                executable = path.stat().st_mode & 0o111
                path.chmod(_PUBLIC_EXECUTABLE_MODE if executable else _PUBLIC_FILE_MODE)
            else:
                raise ConfigurationError(
                    f"public artifact contains an unsupported file: {path}"
                )
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError(
            f"cannot normalize public artifact permissions for {root}: {error}"
        ) from error


def replace_directory(
    staging: Path,
    destination: Path,
    *,
    validate: Callable[[Path], None] | None = None,
) -> None:
    """Publish sibling staging with final validation and ordinary rollback.

    Coordinated producer readers use store leases; this filesystem operation
    does not promise lock-free consistency to arbitrary external readers.
    """

    if not staging.is_dir() or staging.is_symlink():
        raise ConfigurationError(
            f"publication staging path is not a directory: {staging}"
        )
    if staging.parent.resolve() != destination.parent.resolve():
        raise ConfigurationError(
            "publication staging and destination must have the same parent"
        )
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ConfigurationError(
            f"publication destination is not a directory: {destination}"
        )
    if staging.resolve() == destination.resolve():
        raise ConfigurationError(
            "publication staging and destination must be different directories"
        )

    normalize_public_tree(staging)
    backup: Path | None = None
    try:
        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.backup-", dir=destination.parent
                )
            )
            backup.rmdir()
            os.replace(destination, backup)
        os.replace(staging, destination)
        if validate is not None:
            validate(destination)
    except BaseException as error:
        try:
            if destination.exists() and not staging.exists():
                os.replace(destination, staging)
            if backup is not None and backup.exists():
                os.replace(backup, destination)
        except OSError as restore_error:
            raise ConfigurationError(
                f"cannot publish {destination} or restore its previous version: "
                f"{restore_error}"
            ) from restore_error
        if isinstance(error, OSError):
            raise ConfigurationError(
                f"cannot publish directory {destination}: {error}"
            ) from error
        raise

    if backup is not None:
        try:
            shutil.rmtree(backup)
        except OSError as error:
            raise ConfigurationError(
                f"published {destination}, but cannot remove {backup}: {error}"
            ) from error
