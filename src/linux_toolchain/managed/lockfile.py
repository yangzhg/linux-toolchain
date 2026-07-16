from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.catalog import (
    CompilerRelease,
    available_releases,
    resolve_release,
    resolve_releases,
)
from linux_toolchain.managed.contracts import MANAGED_RUNTIME_DIRECTORY_NAMES
from linux_toolchain.managed.models import (
    ManagedHostSpec,
    ManagedSpec,
    ManagedTargetSpec,
)
from linux_toolchain.publication import write_json_atomic
from linux_toolchain.schema import canonical_json_sha256, read_json_object
from linux_toolchain.schema import non_empty_string as _string
from linux_toolchain.schema import object_value as _object
from linux_toolchain.versions import AbiVersion

MANAGED_LOCK_SCHEMA = "linux-toolchain-managed-lock"
MANAGED_LOCK_FORMAT = 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA512_RE = re.compile(r"^[0-9a-f]{128}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{context} must be an array")
    return value


def _identifier(value: object, context: str) -> str:
    result = _string(value, context)
    if not _ID_RE.fullmatch(result):
        raise ConfigurationError(f"{context} is not a valid artifact identifier")
    return result


def _compiler_kit_identity(
    family: str,
    version: str,
    host: ManagedHostSpec,
    target_arch: str,
) -> str:
    return (
        f"compiler-{family}-{version}-{host.arch}-"
        f"glibc-{host.glibc_floor}-to-{target_arch}"
    )


def _runtime_identity(
    provider: str,
    version: str,
    target: ManagedTargetSpec,
) -> str:
    return f"runtime-{provider}-{version}-{target.arch}-glibc-{target.glibc_floor}"


@dataclass(frozen=True)
class SourceLock:
    id: str
    family: str
    version: str
    kind: str
    url: str
    sha512: str

    @classmethod
    def from_release(cls, release: CompilerRelease) -> "SourceLock":
        return cls(
            id=release.source_id,
            family=release.family,
            version=release.version,
            kind=release.source_kind,
            url=release.source_url,
            sha512=release.archive_sha512,
        )

    @classmethod
    def from_dict(cls, value: object, index: int) -> "SourceLock":
        context = f"managed lock.sources[{index}]"
        if not isinstance(value, dict):
            raise ConfigurationError(f"{context} must be an object")
        data = _object(
            value,
            {"id", "family", "version", "kind", "url", "sha512"},
            context,
        )
        result = cls(
            id=_identifier(data["id"], f"{context}.id"),
            family=_string(data["family"], f"{context}.family"),
            version=_string(data["version"], f"{context}.version"),
            kind=_string(data["kind"], f"{context}.kind"),
            url=_string(data["url"], f"{context}.url"),
            sha512=_string(data["sha512"], f"{context}.sha512"),
        )
        result.validate(context=context)
        return result

    def validate(self, *, context: str = "managed source lock") -> None:
        _identifier(self.id, f"{context}.id")
        if self.family not in {"gcc", "clang"}:
            raise ConfigurationError(f"{context}.family must be gcc or clang")
        AbiVersion.parse(self.version)
        if self.id != f"{self.family}-{self.version}":
            raise ConfigurationError(f"{context}.id is inconsistent")
        if not self.url.startswith("https://"):
            raise ConfigurationError(f"{context}.url must use HTTPS")
        if self.kind != "archive":
            raise ConfigurationError(f"{context}.kind must be archive")
        if not _SHA512_RE.fullmatch(self.sha512):
            raise ConfigurationError(f"{context} has an invalid archive pin")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "family": self.family,
            "version": self.version,
            "kind": self.kind,
            "url": self.url,
            "sha512": self.sha512,
        }


@dataclass(frozen=True)
class CompilerKitLock:
    id: str
    family: str
    version: str
    source_id: str
    host: ManagedHostSpec
    target_arch: str

    @classmethod
    def from_dict(cls, value: object, index: int) -> "CompilerKitLock":
        context = f"managed lock.compiler_kits[{index}]"
        data = _object(
            value,
            {"id", "family", "version", "source_id", "host", "target"},
            context,
        )
        target = _object(data["target"], {"arch"}, f"{context}.target")
        result = cls(
            id=_identifier(data["id"], f"{context}.id"),
            family=_string(data["family"], f"{context}.family"),
            version=_string(data["version"], f"{context}.version"),
            source_id=_identifier(data["source_id"], f"{context}.source_id"),
            host=ManagedHostSpec.from_dict(data["host"]),
            target_arch=_string(target["arch"], f"{context}.target.arch"),
        )
        result.validate(context=context)
        return result

    def validate(self, *, context: str = "managed compiler kit lock") -> None:
        _identifier(self.id, f"{context}.id")
        if self.family not in {"gcc", "clang"}:
            raise ConfigurationError(f"{context}.family must be gcc or clang")
        AbiVersion.parse(self.version)
        if self.source_id != f"{self.family}-{self.version}":
            raise ConfigurationError(f"{context}.source_id is inconsistent")
        self.host.validate()
        if self.target_arch not in {"x86_64", "aarch64"}:
            raise ConfigurationError(f"{context}.target.arch is unsupported")
        if self.id != _compiler_kit_identity(
            self.family,
            self.version,
            self.host,
            self.target_arch,
        ):
            raise ConfigurationError(f"{context}.id is inconsistent")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "family": self.family,
            "version": self.version,
            "source_id": self.source_id,
            "host": self.host.to_dict(),
            "target": {"arch": self.target_arch},
        }


@dataclass(frozen=True)
class RuntimeLock:
    id: str
    kind: str
    provider_family: str
    provider_version: str
    source_id: str
    target: ManagedTargetSpec

    @classmethod
    def from_dict(cls, value: object, index: int) -> "RuntimeLock":
        context = f"managed lock.runtimes[{index}]"
        data = _object(
            value, {"id", "kind", "provider", "source_id", "target"}, context
        )
        provider = _object(
            data["provider"], {"family", "version"}, f"{context}.provider"
        )
        result = cls(
            id=_identifier(data["id"], f"{context}.id"),
            kind=_string(data["kind"], f"{context}.kind"),
            provider_family=_string(provider["family"], f"{context}.provider.family"),
            provider_version=_string(
                provider["version"], f"{context}.provider.version"
            ),
            source_id=_identifier(data["source_id"], f"{context}.source_id"),
            target=ManagedTargetSpec.from_dict(data["target"], index),
        )
        result.validate(context=context)
        return result

    def validate(self, *, context: str = "managed runtime lock") -> None:
        _identifier(self.id, f"{context}.id")
        expected = {"gcc-runtime": "gcc", "llvm-runtime": "llvm"}.get(self.kind)
        if expected is None or self.provider_family != expected:
            raise ConfigurationError(f"{context} has an invalid runtime provider")
        AbiVersion.parse(self.provider_version)
        source_family = "gcc" if self.provider_family == "gcc" else "clang"
        if self.source_id != f"{source_family}-{self.provider_version}":
            raise ConfigurationError(f"{context}.source_id is inconsistent")
        if self.id != _runtime_identity(
            self.provider_family,
            self.provider_version,
            self.target,
        ):
            raise ConfigurationError(f"{context}.id is inconsistent")
        self.target.validate()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "provider": {
                "family": self.provider_family,
                "version": self.provider_version,
            },
            "source_id": self.source_id,
            "target": self.target.to_dict(),
        }


@dataclass(frozen=True)
class VariantRuntimeLock:
    kind: str
    runtime_id: str

    @classmethod
    def from_dict(
        cls, value: object, *, variant_index: int, runtime_index: int
    ) -> "VariantRuntimeLock":
        context = f"managed lock.variants[{variant_index}].runtimes[{runtime_index}]"
        data = _object(value, {"kind", "runtime_id"}, context)
        result = cls(
            kind=_string(data["kind"], f"{context}.kind"),
            runtime_id=_identifier(data["runtime_id"], f"{context}.runtime_id"),
        )
        result.validate(context=context)
        return result

    def validate(self, *, context: str = "managed variant runtime") -> None:
        if self.kind not in {"libstdc++", "libc++"}:
            raise ConfigurationError(f"{context}.kind is unsupported")
        _identifier(self.runtime_id, f"{context}.runtime_id")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "runtime_id": self.runtime_id}


@dataclass(frozen=True)
class VariantLock:
    id: str
    compiler_kit_id: str
    runtimes: tuple[VariantRuntimeLock, ...]
    default_cxx_runtime: str
    family: str
    version: str
    target: ManagedTargetSpec

    @classmethod
    def from_dict(cls, value: object, index: int) -> "VariantLock":
        context = f"managed lock.variants[{index}]"
        data = _object(
            value,
            {
                "id",
                "compiler_kit_id",
                "runtimes",
                "default_cxx_runtime",
                "family",
                "version",
                "target",
            },
            context,
        )
        raw_runtimes = _array(data["runtimes"], f"{context}.runtimes")
        result = cls(
            id=_identifier(data["id"], f"{context}.id"),
            compiler_kit_id=_identifier(
                data["compiler_kit_id"], f"{context}.compiler_kit_id"
            ),
            runtimes=tuple(
                VariantRuntimeLock.from_dict(
                    item,
                    variant_index=index,
                    runtime_index=runtime_index,
                )
                for runtime_index, item in enumerate(raw_runtimes)
            ),
            default_cxx_runtime=_string(
                data["default_cxx_runtime"], f"{context}.default_cxx_runtime"
            ),
            family=_string(data["family"], f"{context}.family"),
            version=_string(data["version"], f"{context}.version"),
            target=ManagedTargetSpec.from_dict(data["target"], index),
        )
        result.validate(context=context)
        return result

    def validate(self, *, context: str = "managed variant lock") -> None:
        _identifier(self.id, f"{context}.id")
        _identifier(self.compiler_kit_id, f"{context}.compiler_kit_id")
        if self.family not in {"gcc", "clang"}:
            raise ConfigurationError(f"{context}.family must be gcc or clang")
        AbiVersion.parse(self.version)
        self.target.validate()
        if not self.runtimes:
            raise ConfigurationError(f"{context}.runtimes cannot be empty")
        for runtime in self.runtimes:
            runtime.validate(context=f"{context}.runtimes")
        kinds = tuple(runtime.kind for runtime in self.runtimes)
        runtime_ids = tuple(runtime.runtime_id for runtime in self.runtimes)
        if len(kinds) != len(set(kinds)) or len(runtime_ids) != len(set(runtime_ids)):
            raise ConfigurationError(
                f"{context}.runtimes must have unique kinds and artifact ids"
            )
        if self.default_cxx_runtime not in kinds:
            raise ConfigurationError(f"{context}.default_cxx_runtime is not available")
        expected_order = tuple(
            sorted(
                self.runtimes,
                key=lambda runtime: (
                    runtime.kind != self.default_cxx_runtime,
                    runtime.kind,
                    runtime.runtime_id,
                ),
            )
        )
        if self.runtimes != expected_order:
            raise ConfigurationError(f"{context}.runtimes are not canonical")
        if self.family == "gcc" and kinds != ("libstdc++",):
            raise ConfigurationError("managed GCC variants require libstdc++")

    @property
    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(runtime.runtime_id for runtime in self.runtimes)

    @property
    def cxx_runtimes(self) -> tuple[str, ...]:
        return tuple(runtime.kind for runtime in self.runtimes)

    def runtime_id(self, kind: str) -> str:
        matches = tuple(
            runtime.runtime_id for runtime in self.runtimes if runtime.kind == kind
        )
        if len(matches) != 1:
            raise ConfigurationError(
                f"managed variant {self.id} has no unique {kind} runtime"
            )
        return matches[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "compiler_kit_id": self.compiler_kit_id,
            "runtimes": [runtime.to_dict() for runtime in self.runtimes],
            "default_cxx_runtime": self.default_cxx_runtime,
            "family": self.family,
            "version": self.version,
            "target": self.target.to_dict(),
        }


@dataclass(frozen=True)
class ManagedLock:
    name: str
    build_platform: str
    host: ManagedHostSpec
    spec: ManagedSpec
    spec_sha256: str
    catalog_sha256: str
    sources: tuple[SourceLock, ...]
    compiler_kits: tuple[CompilerKitLock, ...]
    runtimes: tuple[RuntimeLock, ...]
    variants: tuple[VariantLock, ...]
    schema: str = MANAGED_LOCK_SCHEMA
    format: int = MANAGED_LOCK_FORMAT

    @classmethod
    def from_dict(cls, value: object) -> "ManagedLock":
        data = _object(
            value,
            {
                "schema",
                "format",
                "name",
                "build_platform",
                "host",
                "spec",
                "spec_sha256",
                "catalog_sha256",
                "sources",
                "compiler_kits",
                "runtimes",
                "variants",
            },
            "managed lock",
        )
        if data["schema"] != MANAGED_LOCK_SCHEMA:
            raise ConfigurationError(
                f"unsupported managed lock schema: {data['schema']!r}"
            )
        if (
            not isinstance(data["format"], int)
            or isinstance(data["format"], bool)
            or data["format"] != MANAGED_LOCK_FORMAT
        ):
            raise ConfigurationError(
                f"unsupported managed lock format: {data['format']!r}"
            )
        result = cls(
            name=_string(data["name"], "managed lock.name"),
            build_platform=_string(
                data["build_platform"], "managed lock.build_platform"
            ),
            host=ManagedHostSpec.from_dict(data["host"]),
            spec=ManagedSpec.from_dict(data["spec"]),
            spec_sha256=_string(data["spec_sha256"], "managed lock.spec_sha256"),
            catalog_sha256=_string(
                data["catalog_sha256"], "managed lock.catalog_sha256"
            ),
            sources=tuple(
                SourceLock.from_dict(item, index)
                for index, item in enumerate(
                    _array(data["sources"], "managed lock.sources")
                )
            ),
            compiler_kits=tuple(
                CompilerKitLock.from_dict(item, index)
                for index, item in enumerate(
                    _array(data["compiler_kits"], "managed lock.compiler_kits")
                )
            ),
            runtimes=tuple(
                RuntimeLock.from_dict(item, index)
                for index, item in enumerate(
                    _array(data["runtimes"], "managed lock.runtimes")
                )
            ),
            variants=tuple(
                VariantLock.from_dict(item, index)
                for index, item in enumerate(
                    _array(data["variants"], "managed lock.variants")
                )
            ),
        )
        result.validate()
        return result

    @classmethod
    def load(cls, path: Path | str) -> "ManagedLock":
        candidate = Path(path).expanduser()
        return cls.from_dict(read_json_object(candidate, "managed lockfile"))

    def validate(self) -> None:
        if self.name != self.spec.name:
            raise ConfigurationError("managed lock name is inconsistent with its spec")
        if self.build_platform != self.spec.build_platform:
            raise ConfigurationError(
                "managed lock build_platform is inconsistent with its spec"
            )
        if self.host != self.spec.host:
            raise ConfigurationError("managed lock host is inconsistent with its spec")
        if not _SHA256_RE.fullmatch(
            self.spec_sha256
        ) or self.spec_sha256 != canonical_json_sha256(self.spec.to_dict()):
            raise ConfigurationError("managed lock spec_sha256 is inconsistent")
        if not _SHA256_RE.fullmatch(self.catalog_sha256):
            raise ConfigurationError("managed lock catalog_sha256 is invalid")

        for collection_name, entries in (
            ("sources", self.sources),
            ("compiler_kits", self.compiler_kits),
            ("runtimes", self.runtimes),
            ("variants", self.variants),
        ):
            ids = tuple(entry.id for entry in entries)
            if not ids:
                raise ConfigurationError(
                    f"managed lock {collection_name} cannot be empty"
                )
            if ids != tuple(sorted(set(ids))):
                raise ConfigurationError(
                    f"managed lock {collection_name} must be sorted by unique id"
                )

        sources = {source.id: source for source in self.sources}
        kits = {kit.id: kit for kit in self.compiler_kits}
        runtimes = {runtime.id: runtime for runtime in self.runtimes}
        for kit in self.compiler_kits:
            source = sources.get(kit.source_id)
            if source is None or (source.family, source.version) != (
                kit.family,
                kit.version,
            ):
                raise ConfigurationError(
                    f"managed compiler kit {kit.id} has an invalid source reference"
                )
        for runtime in self.runtimes:
            source = sources.get(runtime.source_id)
            expected_family = "gcc" if runtime.provider_family == "gcc" else "clang"
            if source is None or (source.family, source.version) != (
                expected_family,
                runtime.provider_version,
            ):
                raise ConfigurationError(
                    f"managed runtime {runtime.id} has an invalid source reference"
                )
            self.provider_compiler_kit(runtime.id)
        for variant in self.variants:
            kit = kits.get(variant.compiler_kit_id)
            if kit is None:
                raise ConfigurationError(
                    f"managed variant {variant.id} has a missing artifact reference"
                )
            if (kit.family, kit.version, kit.target_arch) != (
                variant.family,
                variant.version,
                variant.target.arch,
            ):
                raise ConfigurationError(
                    f"managed variant {variant.id} does not match its compiler kit"
                )
            for runtime_ref in variant.runtimes:
                runtime = runtimes.get(runtime_ref.runtime_id)
                if runtime is None:
                    raise ConfigurationError(
                        f"managed variant {variant.id} has a missing runtime reference"
                    )
                if runtime.target != variant.target:
                    raise ConfigurationError(
                        f"managed variant {variant.id} does not match its runtime target"
                    )
                if runtime_ref.kind == "libstdc++":
                    if runtime.provider_family != "gcc":
                        raise ConfigurationError(
                            f"managed variant {variant.id} requires a GCC runtime"
                        )
                    if (
                        variant.family == "gcc"
                        and runtime.provider_version != variant.version
                    ):
                        raise ConfigurationError(
                            f"managed GCC variant {variant.id} runtime version is mismatched"
                        )
                elif (
                    variant.family != "clang"
                    or runtime.provider_family != "llvm"
                    or runtime.provider_version != variant.version
                ):
                    raise ConfigurationError(
                        f"managed libc++ variant {variant.id} requires matching LLVM runtime"
                    )

    def provider_compiler_kit(self, runtime_id: str) -> CompilerKitLock:
        """Return the Compiler Kit that builds one runtime artifact."""

        runtimes = tuple(
            runtime for runtime in self.runtimes if runtime.id == runtime_id
        )
        if len(runtimes) != 1:
            raise ConfigurationError(
                f"managed runtime {runtime_id!r} does not exist in the selected lock"
            )
        runtime = runtimes[0]
        provider_family = "gcc" if runtime.provider_family == "gcc" else "clang"
        matches = tuple(
            kit
            for kit in self.compiler_kits
            if (
                kit.family,
                kit.version,
                kit.host,
                kit.target_arch,
            )
            == (
                provider_family,
                runtime.provider_version,
                self.host,
                runtime.target.arch,
            )
        )
        if len(matches) != 1:
            raise ConfigurationError(
                f"managed runtime {runtime.id} has no unique provider Compiler Kit"
            )
        return matches[0]

    def variant(self, variant_id: str) -> VariantLock:
        """Return one variant selected by its public identifier."""

        matches = tuple(
            variant for variant in self.variants if variant.id == variant_id
        )
        if len(matches) != 1:
            raise ConfigurationError(
                f"managed variant {variant_id!r} does not exist in the selected lock"
            )
        return matches[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "format": self.format,
            "name": self.name,
            "build_platform": self.build_platform,
            "host": self.host.to_dict(),
            "spec": self.spec.to_dict(),
            "spec_sha256": self.spec_sha256,
            "catalog_sha256": self.catalog_sha256,
            "sources": [source.to_dict() for source in self.sources],
            "compiler_kits": [kit.to_dict() for kit in self.compiler_kits],
            "runtimes": [runtime.to_dict() for runtime in self.runtimes],
            "variants": [variant.to_dict() for variant in self.variants],
        }

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())


def _catalog_sha256() -> str:
    return canonical_json_sha256(
        {
            "schema": "linux-toolchain-managed-catalog",
            "format": 1,
            "sources": [release.to_source_dict() for release in available_releases()],
        }
    )


def _compiler_kit_id(release: CompilerRelease, host: ManagedHostSpec, arch: str) -> str:
    return _compiler_kit_identity(release.family, release.version, host, arch)


def _runtime_id(provider: str, version: str, target: ManagedTargetSpec) -> str:
    return _runtime_identity(provider, version, target)


def _variant_id(
    compiler: CompilerRelease,
    target: ManagedTargetSpec,
    runtimes: tuple[tuple[str, str, str], ...],
) -> str:
    suffixes: list[str] = []
    for runtime_kind, runtime_provider, runtime_version in runtimes:
        normalized = MANAGED_RUNTIME_DIRECTORY_NAMES[runtime_kind]
        coupled = (
            compiler.family == "gcc"
            and runtime_provider == "gcc"
            and runtime_version == compiler.version
        ) or (
            compiler.family == "clang"
            and runtime_provider == "llvm"
            and runtime_version == compiler.version
        )
        suffixes.append(
            normalized
            if coupled
            else f"{normalized}-{runtime_provider}-{runtime_version}"
        )
    return "-".join(
        (
            f"toolchain-{compiler.family}-{compiler.version}-{target.arch}",
            f"glibc-{target.glibc_floor}",
            *suffixes,
        )
    )


def resolve_lock(spec: ManagedSpec) -> ManagedLock:
    spec.validate()
    sources: dict[str, SourceLock] = {}
    kits: dict[str, CompilerKitLock] = {}
    runtimes: dict[str, RuntimeLock] = {}
    variants: dict[str, VariantLock] = {}

    for compiler_spec in spec.compilers:
        compiler_releases = resolve_releases(
            compiler_spec.family, compiler_spec.versions
        )
        for compiler_release in compiler_releases:
            sources[compiler_release.source_id] = SourceLock.from_release(
                compiler_release
            )
            for target in spec.targets:
                kit_id = _compiler_kit_id(compiler_release, spec.host, target.arch)
                kits.setdefault(
                    kit_id,
                    CompilerKitLock(
                        id=kit_id,
                        family=compiler_release.family,
                        version=compiler_release.version,
                        source_id=compiler_release.source_id,
                        host=spec.host,
                        target_arch=target.arch,
                    ),
                )
                variant_runtimes: list[tuple[str, str, str, str]] = []
                for runtime_spec in compiler_spec.runtimes:
                    if compiler_release.family == "gcc":
                        runtime_release = compiler_release
                        runtime_provider = "gcc"
                        runtime_artifact_kind = "gcc-runtime"
                    elif runtime_spec.kind == "libstdc++":
                        assert runtime_spec.gcc_version is not None
                        runtime_release = resolve_release(
                            "gcc", runtime_spec.gcc_version
                        )
                        runtime_provider = "gcc"
                        runtime_artifact_kind = "gcc-runtime"
                    else:
                        runtime_release = compiler_release
                        runtime_provider = "llvm"
                        runtime_artifact_kind = "llvm-runtime"

                    sources[runtime_release.source_id] = SourceLock.from_release(
                        runtime_release
                    )
                    provider_kit_id = _compiler_kit_id(
                        runtime_release,
                        spec.host,
                        target.arch,
                    )
                    kits.setdefault(
                        provider_kit_id,
                        CompilerKitLock(
                            id=provider_kit_id,
                            family=runtime_release.family,
                            version=runtime_release.version,
                            source_id=runtime_release.source_id,
                            host=spec.host,
                            target_arch=target.arch,
                        ),
                    )
                    runtime_id = _runtime_id(
                        runtime_provider, runtime_release.version, target
                    )
                    runtimes.setdefault(
                        runtime_id,
                        RuntimeLock(
                            id=runtime_id,
                            kind=runtime_artifact_kind,
                            provider_family=runtime_provider,
                            provider_version=runtime_release.version,
                            source_id=runtime_release.source_id,
                            target=target,
                        ),
                    )
                    variant_runtimes.append(
                        (
                            runtime_spec.kind,
                            runtime_id,
                            runtime_provider,
                            runtime_release.version,
                        )
                    )
                default_runtime = (
                    "libstdc++"
                    if any(kind == "libstdc++" for kind, *_ in variant_runtimes)
                    else variant_runtimes[0][0]
                )
                ordered_runtimes = tuple(
                    sorted(
                        variant_runtimes,
                        key=lambda runtime: (
                            runtime[0] != default_runtime,
                            runtime[0],
                            runtime[1],
                        ),
                    )
                )
                variant_id = _variant_id(
                    compiler_release,
                    target,
                    tuple(
                        (kind, provider, version)
                        for kind, _, provider, version in ordered_runtimes
                    ),
                )
                variants[variant_id] = VariantLock(
                    id=variant_id,
                    compiler_kit_id=kit_id,
                    runtimes=tuple(
                        VariantRuntimeLock(kind=kind, runtime_id=runtime_id)
                        for kind, runtime_id, _, _ in ordered_runtimes
                    ),
                    default_cxx_runtime=default_runtime,
                    family=compiler_release.family,
                    version=compiler_release.version,
                    target=target,
                )

    result = ManagedLock(
        name=spec.name,
        build_platform=spec.build_platform,
        host=spec.host,
        spec=spec,
        spec_sha256=canonical_json_sha256(spec.to_dict()),
        catalog_sha256=_catalog_sha256(),
        sources=tuple(sources[key] for key in sorted(sources)),
        compiler_kits=tuple(kits[key] for key in sorted(kits)),
        runtimes=tuple(runtimes[key] for key in sorted(runtimes)),
        variants=tuple(variants[key] for key in sorted(variants)),
    )
    result.validate()
    return result


def write_lockfile(lock: ManagedLock, path: Path | str, *, force: bool = False) -> Path:
    lock.validate()
    destination = Path(path).expanduser()
    if destination.is_symlink():
        raise ConfigurationError(
            f"managed lockfile output cannot be a symlink: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(lock.to_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    if destination.exists():
        if not destination.is_file():
            raise ConfigurationError(
                f"managed lockfile output is not a file: {destination}"
            )
        try:
            existing = destination.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigurationError(
                f"cannot read existing managed lockfile {destination}: {error}"
            ) from error
        if existing == serialized:
            return destination
        if not force:
            raise ConfigurationError(
                f"managed lockfile already exists with different content: {destination}"
            )

    return write_json_atomic(destination, lock.to_dict())
