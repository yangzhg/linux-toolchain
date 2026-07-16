from __future__ import annotations

import fcntl
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.models import SdkSpec
from linux_toolchain.schema import canonical_json_sha256
from linux_toolchain.sdk.crosstool_ng import sdk_producer_identity

_MARKER = ".linux-toolchain-producer-store"
_MARKER_CONTENT = "format=1\n"


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _sdk_identity(spec: SdkSpec) -> dict[str, object]:
    return sdk_producer_identity(spec)


def _managed_identity(
    target_sdk: SdkSpec,
    compiler_backend: SdkSpec,
) -> dict[str, object]:
    return {
        "kind": "managed",
        "target_sdk": _sdk_identity(target_sdk),
        "compiler_backend": _sdk_identity(compiler_backend),
    }


def _label(spec: SdkSpec) -> str:
    version = spec.target.libc_version.replace(".", "")
    return f"{spec.target.arch}-glibc{version}"


@dataclass(frozen=True)
class ProducerStore:
    """Shared, content-addressed workspaces for expensive producer inputs."""

    root: Path

    @classmethod
    def prepare(cls, path: Path | str) -> "ProducerStore":
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise ConfigurationError(f"producer store cannot be a symlink: {raw}")
        root = raw.resolve()
        if root in {Path("/"), Path.home().resolve()}:
            raise ConfigurationError(f"invalid producer store: {root}")
        if root.exists():
            if not root.is_dir():
                raise ConfigurationError(f"producer store is not a directory: {root}")
            if next(root.iterdir(), None) is not None:
                cls._validate_marker(root)
                return cls(root)
        root.mkdir(parents=True, exist_ok=True)
        marker = root / _MARKER
        try:
            marker.write_text(_MARKER_CONTENT, encoding="utf-8")
            marker.chmod(0o644)
        except OSError as error:
            raise ConfigurationError(
                f"cannot initialize producer store {root}: {error}"
            ) from error
        return cls(root)

    @classmethod
    def load(cls, path: Path | str) -> "ProducerStore":
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise ConfigurationError(f"producer store cannot be a symlink: {raw}")
        root = raw.resolve()
        if not root.is_dir():
            raise ConfigurationError(f"producer store does not exist: {root}")
        cls._validate_marker(root)
        return cls(root)

    @staticmethod
    def _validate_marker(root: Path) -> None:
        marker = root / _MARKER
        try:
            valid = (
                _regular_file(marker)
                and marker.read_text(encoding="utf-8") == _MARKER_CONTENT
            )
        except OSError as error:
            raise ConfigurationError(
                f"cannot read producer store marker {marker}: {error}"
            ) from error
        if not valid:
            raise ConfigurationError(f"refusing to use unowned producer store: {root}")

    def sdk_workspace(self, spec: SdkSpec) -> Path:
        digest = canonical_json_sha256(_sdk_identity(spec))
        return self.root / "sdk" / f"{_label(spec)}-{digest[:16]}"

    def managed_workspace(
        self,
        target_sdk: SdkSpec,
        compiler_backend: SdkSpec,
    ) -> Path:
        digest = canonical_json_sha256(_managed_identity(target_sdk, compiler_backend))
        return self.root / "managed" / f"{_label(target_sdk)}-{digest[:16]}"

    @property
    def source_cache(self) -> Path:
        return self.root / "sources"

    @property
    def sdk_source_cache(self) -> Path:
        return self.root / "sdk-sources"

    @contextmanager
    def lock(
        self,
        namespace: str,
        identity: Mapping[str, object],
        *,
        shared: bool = False,
    ) -> Iterator[None]:
        """Lease one shared build identity for reading or exclusive publication."""

        if not namespace or not namespace.replace("-", "").isalnum():
            raise ConfigurationError("producer store lock namespace is invalid")
        digest = canonical_json_sha256(identity)
        lock_directory = self.root / "locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        lock_path = lock_directory / f"{namespace}-{digest}.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise ConfigurationError(
                f"cannot open producer store lock {lock_path}: {error}"
            ) from error
        with os.fdopen(descriptor, "r+", encoding="ascii") as stream:
            try:
                fcntl.flock(
                    stream.fileno(),
                    fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
                )
            except OSError as error:
                raise ConfigurationError(
                    f"cannot lock producer store identity {lock_path}: {error}"
                ) from error
            yield

    @contextmanager
    def lock_many(
        self,
        identities: tuple[tuple[str, Mapping[str, object]], ...],
        *,
        shared: bool = False,
    ) -> Iterator[None]:
        """Lease SDK then managed-artifact identities in one canonical order."""

        ranks = {"sdk": 0, "managed-artifact": 1}
        ordered: dict[tuple[int, str], tuple[str, Mapping[str, object]]] = {}
        for namespace, identity in identities:
            try:
                rank = ranks[namespace]
            except KeyError as error:
                raise ConfigurationError(
                    f"unsupported multi-lock namespace: {namespace}"
                ) from error
            digest = canonical_json_sha256(identity)
            ordered[(rank, digest)] = (namespace, identity)
        with ExitStack() as stack:
            for key in sorted(ordered):
                namespace, identity = ordered[key]
                stack.enter_context(self.lock(namespace, identity, shared=shared))
            yield


def sdk_build_identity(spec: SdkSpec) -> dict[str, object]:
    return _sdk_identity(spec)
