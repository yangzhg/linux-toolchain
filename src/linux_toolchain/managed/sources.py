from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from linux_toolchain import __version__
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.managed.lockfile import SourceLock

_SHA512 = re.compile(r"^[0-9a-f]{128}$")
_GCC_SOURCE_HOSTS = {"gcc.gnu.org", "ftp.gnu.org", "ftpmirror.gnu.org"}

TransferProgressCallback = Callable[[int, int], None]


@contextmanager
def _source_cache_lock(destination: Path, identity: str) -> Iterator[None]:
    lock_directory = destination.parent / ".locks"
    if lock_directory.is_symlink():
        raise ConfigurationError(
            f"managed source cache lock directory cannot be a symlink: {lock_directory}"
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_directory.mkdir(exist_ok=True)
    except OSError as error:
        raise ConfigurationError(
            f"cannot prepare managed source cache lock directory: {error}"
        ) from error
    lock_path = lock_directory / f"{identity}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ConfigurationError(
            f"cannot open managed source cache lock {lock_path}: {error}"
        ) from error
    with os.fdopen(descriptor, "r+", encoding="ascii") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise ConfigurationError(
                f"cannot lock managed source cache identity {identity}: {error}"
            ) from error
        yield


def validate_source_archive(source: SourceLock) -> str:
    if source.kind != "archive" or not _SHA512.fullmatch(source.sha512):
        raise ConfigurationError("managed source archive pin is invalid")
    parsed = urllib.parse.urlparse(source.url)
    clean_https_url = (
        parsed.scheme == "https"
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )
    if source.family == "gcc":
        valid_location = (
            parsed.hostname in _GCC_SOURCE_HOSTS
            and Path(parsed.path).name == f"gcc-{source.version}.tar.xz"
        )
    elif source.family == "clang":
        valid_location = source.url == (
            "https://github.com/llvm/llvm-project/releases/download/"
            f"llvmorg-{source.version}/llvm-project-{source.version}.src.tar.xz"
        )
    else:
        valid_location = False
    if not clean_https_url or not valid_location:
        raise ConfigurationError(
            "managed source must be the exact official release tar.xz"
        )
    return source.sha512


def file_sha512(
    path: Path,
    progress: TransferProgressCallback | None = None,
) -> str:
    digest = hashlib.sha512()
    try:
        total = path.stat().st_size
        completed = 0
        if progress is not None:
            progress(0, total)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                completed += len(chunk)
                if progress is not None:
                    progress(completed, total)
    except OSError as error:
        raise ConfigurationError(
            f"cannot hash managed source cache entry {path}: {error}"
        ) from error
    return digest.hexdigest()


def download_source_archive(
    source: SourceLock,
    destination: Path,
    progress: TransferProgressCallback | None = None,
) -> Path:
    expected = validate_source_archive(source)
    with _source_cache_lock(destination, f"sha512-{expected}"):
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise ConfigurationError(
                    f"managed source cache entry is not a regular file: {destination}"
                )
            actual = file_sha512(destination, progress)
            if actual != expected:
                raise ConfigurationError(
                    "cached managed source SHA-512 mismatch: "
                    f"expected {expected}, got {actual}"
                )
            return destination

        request = urllib.request.Request(
            source.url, headers={"User-Agent": f"linux-toolchain/{__version__}"}
        )
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.part-",
                dir=destination.parent,
            )
        except OSError as error:
            raise ConfigurationError(
                f"cannot create managed source temporary file: {error}"
            ) from error
        temporary = Path(temporary_name)
        digest = hashlib.sha512()
        try:
            with (
                os.fdopen(descriptor, "wb") as stream,
                urllib.request.urlopen(request, timeout=60) as response,
            ):
                headers = getattr(response, "headers", None)
                content_length = (
                    headers.get("Content-Length") if headers is not None else None
                )
                try:
                    total = int(content_length) if content_length is not None else None
                except ValueError:
                    total = None
                completed = 0
                if progress is not None and total is not None and total > 0:
                    progress(0, total)
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    stream.write(chunk)
                    completed += len(chunk)
                    if progress is not None and total is not None and total > 0:
                        progress(completed, total)
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, urllib.error.URLError) as error:
            temporary.unlink(missing_ok=True)
            raise ExternalToolError(
                f"cannot download managed source: {error}"
            ) from error
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        actual = digest.hexdigest()
        if actual != expected:
            temporary.unlink(missing_ok=True)
            raise ExternalToolError(
                "downloaded managed source SHA-512 mismatch: "
                f"expected {expected}, got {actual}"
            )
        try:
            temporary.chmod(0o644)
            os.replace(temporary, destination)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise ExternalToolError(
                f"cannot publish managed source: {error}"
            ) from error
        return destination
