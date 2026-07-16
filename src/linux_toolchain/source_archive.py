from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from linux_toolchain import __version__
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.integrity import file_sha256
from linux_toolchain.publication import file_lock

TransferProgressCallback = Callable[[int, int], None]

GNU_ARCHIVE_BASE_URLS = (
    "https://mirrors.kernel.org/gnu",
    "https://ftpmirror.gnu.org",
    "https://ftp.gnu.org/gnu",
)


@dataclass(frozen=True)
class PinnedArchive:
    filename: str
    source_url: str
    sha256: str
    mirrors: tuple[str, ...] = ()

    @property
    def source_urls(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.source_url, *self.mirrors)))


def gnu_archive_urls(
    path: str,
    *,
    preferred_base_url: str | None = None,
) -> tuple[str, ...]:
    """Return the ordered source URLs for one GNU release archive."""

    base_urls = (
        (preferred_base_url, *GNU_ARCHIVE_BASE_URLS)
        if preferred_base_url
        else GNU_ARCHIVE_BASE_URLS
    )
    relative_path = path.lstrip("/")
    return tuple(
        dict.fromkeys(
            f"{base_url.rstrip('/')}/{relative_path}" for base_url in base_urls
        )
    )


def pinned_gnu_archive(
    *,
    filename: str,
    path: str,
    sha256: str,
    preferred_base_url: str | None = None,
) -> PinnedArchive:
    source_urls = gnu_archive_urls(
        path,
        preferred_base_url=preferred_base_url,
    )
    return PinnedArchive(
        filename=filename,
        source_url=source_urls[0],
        sha256=sha256,
        mirrors=source_urls[1:],
    )


def _archive_member_path(value: str, *, context: str) -> PurePosixPath:
    path = PurePosixPath(value.removeprefix("./"))
    if not path.parts or path.is_absolute() or ".." in path.parts or "\0" in value:
        raise ExternalToolError(f"{context} contains an invalid path: {value!r}")
    return path


def _archive_link_target(
    value: str,
    *,
    member: PurePosixPath,
    hard_link: bool,
    expected: PurePosixPath,
    context: str,
) -> PurePosixPath:
    link = PurePosixPath(value)
    if not link.parts or link.is_absolute() or "\0" in value:
        raise ExternalToolError(f"{context} contains an invalid link target: {value!r}")
    target = link if hard_link else member.parent / link
    normalized = PurePosixPath(posixpath.normpath(target.as_posix()))
    if (
        not normalized.parts
        or normalized.is_absolute()
        or normalized.parts[0] != expected.name
    ):
        raise ExternalToolError(
            f"{context} link escapes {expected}: {member!s} -> {value!r}"
        )
    return normalized


def validate_tar_archive(
    archive: Path,
    *,
    top_directory: str,
    context: str,
) -> None:
    """Reject archive entries that could escape their expected source directory."""

    expected = _archive_member_path(
        top_directory,
        context=f"{context} top directory",
    )
    if len(expected.parts) != 1:
        raise ConfigurationError(
            f"{context} top directory must contain exactly one path component"
        )
    seen: set[PurePosixPath] = set()
    try:
        with tarfile.open(archive, mode="r:*") as source:
            members = source.getmembers()
            if not members:
                raise ExternalToolError(f"{context} is empty")
            for member in members:
                path = _archive_member_path(
                    member.name,
                    context=f"{context} member",
                )
                if path.parts[0] != expected.name:
                    raise ExternalToolError(
                        f"{context} member is outside {expected}: {member.name!r}"
                    )
                if path in seen:
                    raise ExternalToolError(
                        f"{context} contains a duplicate member: {member.name!r}"
                    )
                seen.add(path)
                if member.isreg() or member.isdir():
                    continue
                if not (member.issym() or member.islnk()):
                    raise ExternalToolError(
                        f"{context} contains an unsupported member: {member.name!r}"
                    )
                _archive_link_target(
                    member.linkname,
                    member=path,
                    hard_link=member.islnk(),
                    expected=expected,
                    context=context,
                )
    except (OSError, tarfile.TarError) as error:
        raise ExternalToolError(
            f"cannot inspect {context} {archive}: {error}"
        ) from error


def archive_matches(path: Path, sha256: str) -> bool:
    try:
        return not path.is_symlink() and path.is_file() and file_sha256(path) == sha256
    except OSError:
        return False


def _cache_directory(path: Path, *, context: str) -> Path:
    raw = path.expanduser()
    if raw.is_symlink():
        raise ConfigurationError(f"{context} cannot be a symlink: {raw}")
    try:
        resolved = raw.resolve()
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ConfigurationError(f"cannot prepare {context} {raw}: {error}") from error
    if resolved.is_symlink() or not resolved.is_dir():
        raise ConfigurationError(f"{context} is not a directory: {resolved}")
    return resolved


@contextmanager
def _source_cache_lock(cache: Path, sha256: str) -> Iterator[None]:
    lock_directory = _cache_directory(
        cache / "locks",
        context="source cache lock directory",
    )
    lock_path = lock_directory / f"{sha256}.lock"
    with file_lock(
        lock_path,
        shared=False,
        context=f"source cache object {sha256}",
    ):
        yield


def _download_archive_file(
    archive: PinnedArchive,
    destination: Path,
    *,
    description: str,
    progress: TransferProgressCallback | None,
) -> Path:
    if archive_matches(destination, archive.sha256):
        return destination
    return download_verified_file_from_sources(
        archive.source_urls,
        destination,
        expected_digest=archive.sha256,
        hash_name="sha256",
        description=description,
        progress=progress,
    )


def download_verified_file(
    source_url: str,
    destination: Path,
    *,
    expected_digest: str,
    hash_name: Literal["sha256", "sha512"],
    description: str,
    progress: TransferProgressCallback | None = None,
) -> Path:
    """Download one file, verify its digest, and publish it atomically."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    request = urllib.request.Request(
        source_url,
        headers={"User-Agent": f"linux-toolchain/{__version__}"},
    )
    digest_label, digest = {
        "sha256": ("SHA-256", hashlib.sha256),
        "sha512": ("SHA-512", hashlib.sha512),
    }[hash_name]
    hasher = digest()
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.part-",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
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
                hasher.update(chunk)
                stream.write(chunk)
                completed += len(chunk)
                if progress is not None and total is not None and total > 0:
                    progress(completed, total)
            stream.flush()
            os.fsync(stream.fileno())
    except (OSError, urllib.error.URLError) as error:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise ExternalToolError(f"cannot download {description}: {error}") from error
    except BaseException:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
        raise
    actual = hasher.hexdigest()
    if actual != expected_digest:
        temporary.unlink(missing_ok=True)
        raise ExternalToolError(
            f"{description} {digest_label} mismatch: "
            f"expected {expected_digest}, got {actual}"
        )
    try:
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ExternalToolError(
            f"cannot publish downloaded {description}: {error}"
        ) from error
    return destination


def download_verified_file_from_sources(
    source_urls: tuple[str, ...],
    destination: Path,
    *,
    expected_digest: str,
    hash_name: Literal["sha256", "sha512"],
    description: str,
    progress: TransferProgressCallback | None = None,
) -> Path:
    """Try equivalent source URLs in order, verifying the same pinned digest."""

    candidates = tuple(dict.fromkeys(source_urls))
    if not candidates:
        raise ConfigurationError(f"{description} has no source URLs")
    failures: list[tuple[str, ExternalToolError]] = []
    for source_url in candidates:
        try:
            return download_verified_file(
                source_url,
                destination,
                expected_digest=expected_digest,
                hash_name=hash_name,
                description=description,
                progress=progress,
            )
        except ExternalToolError as error:
            failures.append((source_url, error))
    details = "; ".join(f"{url}: {error}" for url, error in failures)
    raise ExternalToolError(
        f"cannot acquire {description} from its pinned sources: {details}"
    ) from failures[-1][1]


def publish_archive_file(source: Path, destination: Path) -> None:
    temporary: Path | None = None
    try:
        if source.is_symlink() or not source.is_file():
            raise ExternalToolError(f"source archive is invalid: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            source.open("rb") as input_stream,
            tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.tmp-",
                dir=destination.parent,
                delete=False,
            ) as stream,
        ):
            temporary = Path(stream.name)
            shutil.copyfileobj(input_stream, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    except (OSError, ExternalToolError) as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if isinstance(error, ExternalToolError):
            raise
        raise ExternalToolError(
            f"cannot publish source archive {destination}: {error}"
        ) from error


def download_pinned_archive(
    archive: PinnedArchive,
    workspace: Path,
    *,
    description: str,
    source_cache: Path | None = None,
    progress: TransferProgressCallback | None = None,
) -> Path:
    destination = workspace / "downloads" / archive.filename
    if source_cache is None:
        return _download_archive_file(
            archive,
            destination,
            description=description,
            progress=progress,
        )

    cache = _cache_directory(source_cache, context="source cache")
    object_directory = _cache_directory(
        cache / "sha256",
        context="source cache object directory",
    )
    cached = object_directory / archive.sha256
    with _source_cache_lock(cache, archive.sha256):
        destination_ready = archive_matches(destination, archive.sha256)
        if not archive_matches(cached, archive.sha256):
            if destination_ready:
                publish_archive_file(destination, cached)
            else:
                _download_archive_file(
                    archive,
                    cached,
                    description=description,
                    progress=progress,
                )
        if not destination_ready:
            publish_archive_file(cached, destination)
    return destination
