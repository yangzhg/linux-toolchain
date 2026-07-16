from __future__ import annotations

import hashlib
import re
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.lockfile import SourceLock
from linux_toolchain.publication import file_lock
from linux_toolchain.source_archive import (
    download_verified_file_from_sources,
    gnu_archive_urls,
)

_SHA512 = re.compile(r"^[0-9a-f]{128}$")
_GCC_SOURCE_HOSTS = {
    "gcc.gnu.org",
    "ftp.gnu.org",
    "ftpmirror.gnu.org",
    "mirrors.kernel.org",
}

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
    with file_lock(
        lock_path,
        shared=False,
        context=f"managed source cache identity {identity}",
    ):
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


def _source_urls(source: SourceLock) -> tuple[str, ...]:
    if source.family == "gcc":
        filename = f"gcc-{source.version}.tar.xz"
        return gnu_archive_urls(f"gcc/gcc-{source.version}/{filename}")
    return (source.url,)


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

        return download_verified_file_from_sources(
            _source_urls(source),
            destination,
            expected_digest=expected,
            hash_name="sha512",
            description="managed source",
            progress=progress,
        )
