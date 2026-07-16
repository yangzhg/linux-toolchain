from __future__ import annotations

from dataclasses import dataclass, replace

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.models import (
    ARCHITECTURE_MINIMUM_KERNELS,
    SUPPORTED_ARCHITECTURES,
    BuilderSpec,
    SdkSpec,
    TargetSpec,
)
from linux_toolchain.versions import AbiVersion


@dataclass(frozen=True)
class ArchitectureDefaults:
    """Architecture-specific settings within a pinned backend family."""

    arch: str
    cpu: str
    minimum_kernel: str
    binutils: str
    binutils_by_glibc: tuple[tuple[str, str], ...] = ()

    def binutils_for(self, glibc_version: str) -> str:
        normalized = AbiVersion.parse(glibc_version)
        for candidate, binutils in self.binutils_by_glibc:
            if AbiVersion.parse(candidate) == normalized:
                return binutils
        return self.binutils


@dataclass(frozen=True)
class BackendFamily:
    """A pinned crosstool-NG component family.

    ``glibc_versions`` is the project's pinned available-version allowlist for the
    crosstool-NG backend.
    """

    name: str
    builder_version: str
    gcc: str
    linux_headers: str
    glibc_versions: tuple[str, ...]
    architectures: tuple[ArchitectureDefaults, ...]

    def canonical_glibc_version(self, requested: str) -> str | None:
        normalized = AbiVersion.parse(requested)
        for candidate in self.glibc_versions:
            if AbiVersion.parse(candidate) == normalized:
                return candidate
        return None

    def architecture(self, arch: str) -> ArchitectureDefaults | None:
        return next(
            (candidate for candidate in self.architectures if candidate.arch == arch),
            None,
        )


@dataclass(frozen=True)
class SdkRecipe:
    """A resolved family/version/architecture selection.

    The crosstool-NG GCC is private producer build machinery. It builds the SDK
    and supplies the compiler backend for managed Compiler Kits.
    """

    family: str
    arch: str
    glibc_version: str
    linux_headers: str
    minimum_kernel: str
    cpu: str
    builder_version: str
    gcc: str
    binutils: str

    def to_spec(
        self,
        *,
        name: str | None = None,
        minimum_kernel: str | None = None,
    ) -> SdkSpec:
        if minimum_kernel is not None and AbiVersion.parse(
            minimum_kernel
        ) < AbiVersion.parse(self.minimum_kernel):
            raise ConfigurationError(
                "target.minimum_kernel cannot be lower than the validated "
                f"{self.family} family baseline {self.minimum_kernel}"
            )
        spec = SdkSpec(
            name=(
                f"linux-toolchain-{self.arch}-glibc-{self.glibc_version}"
                if name is None
                else name
            ),
            target=TargetSpec(
                arch=self.arch,
                vendor="portable",
                libc="glibc",
                libc_version=self.glibc_version,
                linux_headers=self.linux_headers,
                minimum_kernel=(
                    self.minimum_kernel if minimum_kernel is None else minimum_kernel
                ),
                cpu=self.cpu,
            ),
            builder=BuilderSpec(
                backend="crosstool-ng",
                version=self.builder_version,
                gcc=self.gcc,
                binutils=self.binutils,
            ),
        )
        spec.validate()
        _validate_minimum_kernel(spec)
        return spec


_CROSSTOOL_NG_1_28_GLIBC_VERSIONS = (
    "2.17",
    "2.19",
    "2.23",
    "2.24",
    "2.25",
    "2.26",
    "2.27",
    "2.28",
    "2.29",
    "2.30",
    "2.31",
    "2.32",
    "2.33",
    "2.34",
    "2.35",
    "2.36",
    "2.37",
    "2.38",
    "2.39",
    "2.40",
    "2.41",
    "2.42",
)

BACKEND_FAMILIES = (
    BackendFamily(
        name="crosstool-ng-1.28.0",
        builder_version="1.28.0",
        gcc="9.5.0",
        linux_headers="6.12.41",
        glibc_versions=_CROSSTOOL_NG_1_28_GLIBC_VERSIONS,
        architectures=(
            ArchitectureDefaults(
                arch="x86_64",
                cpu="x86-64",
                minimum_kernel=ARCHITECTURE_MINIMUM_KERNELS["x86_64"],
                binutils="2.45",
            ),
            ArchitectureDefaults(
                arch="aarch64",
                cpu="armv8-a",
                minimum_kernel=ARCHITECTURE_MINIMUM_KERNELS["aarch64"],
                binutils="2.45",
                binutils_by_glibc=tuple(
                    (version, "2.29.1")
                    for version in ("2.17", "2.19", "2.23", "2.24", "2.25")
                ),
            ),
        ),
    ),
)


def available_families() -> tuple[BackendFamily, ...]:
    return BACKEND_FAMILIES


def available_glibc_versions() -> tuple[str, ...]:
    """Return canonical releases accepted by the preferred-family resolver."""

    versions: dict[AbiVersion, str] = {}
    for family in BACKEND_FAMILIES:
        for version in family.glibc_versions:
            versions.setdefault(AbiVersion.parse(version), version)
    return tuple(versions[key] for key in sorted(versions))


def available_recipes() -> tuple[SdkRecipe, ...]:
    """Return resolved selections, derived from families rather than hand-written."""

    recipes = []
    for version in available_glibc_versions():
        for arch in SUPPORTED_ARCHITECTURES:
            if arch == "aarch64" and AbiVersion.parse(version) < AbiVersion.parse(
                "2.17"
            ):
                continue
            recipes.append(get_recipe(arch, version))
    return tuple(recipes)


def get_recipe(arch: str, glibc_version: str) -> SdkRecipe:
    if arch not in SUPPORTED_ARCHITECTURES:
        raise ConfigurationError(
            f"unsupported target architecture {arch!r}; supported architectures: "
            + ", ".join(SUPPORTED_ARCHITECTURES)
        )

    normalized_version = AbiVersion.parse(glibc_version)
    if arch == "aarch64" and normalized_version < AbiVersion.parse("2.17"):
        raise ConfigurationError(
            f"glibc {glibc_version} predates AArch64 support; "
            "AArch64 requires glibc 2.17 or newer"
        )

    for family in BACKEND_FAMILIES:
        canonical_version = family.canonical_glibc_version(glibc_version)
        architecture = family.architecture(arch)
        if canonical_version is None or architecture is None:
            continue
        return SdkRecipe(
            family=family.name,
            arch=arch,
            glibc_version=canonical_version,
            linux_headers=family.linux_headers,
            minimum_kernel=architecture.minimum_kernel,
            cpu=architecture.cpu,
            builder_version=family.builder_version,
            gcc=family.gcc,
            binutils=architecture.binutils_for(canonical_version),
        )

    available = ", ".join(available_glibc_versions())
    raise ConfigurationError(
        f"no pinned backend family is available for {arch}/glibc-{glibc_version}; "
        f"available glibc catalog entries: {available}. Add a backend family "
        "with a pinned crosstool-NG release and compatible components, "
        "then validate it before claiming release qualification"
    )


def apply_recipe_overrides(
    spec: SdkSpec,
    *,
    name: str | None = None,
    minimum_kernel: str | None = None,
) -> SdkSpec:
    """Override operational fields without changing pinned components."""

    target = spec.target
    if minimum_kernel is not None:
        if AbiVersion.parse(minimum_kernel) < AbiVersion.parse(target.minimum_kernel):
            raise ConfigurationError(
                "target.minimum_kernel override cannot lower the declared "
                f"baseline {target.minimum_kernel}"
            )
        target = replace(target, minimum_kernel=minimum_kernel)
    result = replace(
        spec,
        name=spec.name if name is None else name,
        target=target,
    )
    result.validate()
    _validate_minimum_kernel(result)
    return result


def _validate_minimum_kernel(spec: SdkSpec) -> None:
    if AbiVersion.parse(spec.target.minimum_kernel) > AbiVersion.parse(
        spec.target.linux_headers
    ):
        raise ConfigurationError(
            "target.minimum_kernel cannot be newer than target.linux_headers"
        )
