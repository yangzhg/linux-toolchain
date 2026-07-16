from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.lockfile import (
    CompilerKitLock,
    ManagedLock,
    RuntimeLock,
    SourceLock,
)
from linux_toolchain.managed.models import ManagedHostSpec

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.+-]+$")


@dataclass(frozen=True)
class ManagedBuildSelection:
    artifact_id: str
    artifact_kind: str
    family: str
    version: str
    source: SourceLock
    build_platform: str
    build_host: ManagedHostSpec
    host: ManagedHostSpec | None
    target_arch: str
    target_glibc_floor: str | None
    runtime_kind: str | None = None

    @property
    def payload_name(self) -> str:
        return "compiler" if self.artifact_kind == "compiler-kit" else "runtime"

    def to_dict(self) -> dict[str, object]:
        target: dict[str, str] = {"arch": self.target_arch}
        if self.target_glibc_floor is not None:
            target["glibc_floor"] = self.target_glibc_floor
        result: dict[str, object] = {
            "id": self.artifact_id,
            "kind": self.artifact_kind,
            "family": self.family,
            "version": self.version,
            "source_id": self.source.id,
            "target": target,
        }
        if self.host is not None:
            result["host"] = self.host.to_dict()
        if self.runtime_kind is not None:
            result["runtime_kind"] = self.runtime_kind
        return result


def managed_lock(value: ManagedLock | Mapping[str, object] | object) -> ManagedLock:
    if isinstance(value, ManagedLock):
        value.validate()
        return value
    if isinstance(value, Mapping):
        return ManagedLock.from_dict(dict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        rendered = to_dict()
        if not isinstance(rendered, Mapping):
            raise ConfigurationError("managed lock to_dict() must return an object")
        return ManagedLock.from_dict(dict(rendered))
    raise ConfigurationError("managed builder requires a ManagedLock or lock object")


def select_artifact(
    lock_value: ManagedLock | Mapping[str, object] | object,
    artifact_id: str,
) -> ManagedBuildSelection:
    lock = managed_lock(lock_value)
    if not isinstance(artifact_id, str) or not _SAFE_ID.fullmatch(artifact_id):
        raise ConfigurationError("managed artifact id is invalid")
    sources = {source.id: source for source in lock.sources}
    kits = tuple(kit for kit in lock.compiler_kits if kit.id == artifact_id)
    runtimes = tuple(runtime for runtime in lock.runtimes if runtime.id == artifact_id)
    if len(kits) + len(runtimes) != 1:
        raise ConfigurationError(
            f"managed artifact id {artifact_id!r} must identify one compiler kit "
            "or runtime"
        )

    if kits:
        kit: CompilerKitLock = kits[0]
        source = sources.get(kit.source_id)
        if source is None:
            raise ConfigurationError(f"managed compiler kit {kit.id} has no source")
        return ManagedBuildSelection(
            artifact_id=kit.id,
            artifact_kind="compiler-kit",
            family=kit.family,
            version=kit.version,
            source=source,
            build_platform=lock.build_platform,
            build_host=lock.host,
            host=kit.host,
            target_arch=kit.target_arch,
            target_glibc_floor=None,
        )

    runtime: RuntimeLock = runtimes[0]
    source = sources.get(runtime.source_id)
    if source is None:
        raise ConfigurationError(f"managed runtime {runtime.id} has no source")
    return ManagedBuildSelection(
        artifact_id=runtime.id,
        artifact_kind="runtime",
        family="gcc" if runtime.provider_family == "gcc" else "clang",
        version=runtime.provider_version,
        source=source,
        build_platform=lock.build_platform,
        build_host=lock.host,
        host=None,
        target_arch=runtime.target.arch,
        target_glibc_floor=runtime.target.glibc_floor,
        runtime_kind=runtime.kind,
    )
