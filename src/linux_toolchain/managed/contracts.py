from linux_toolchain.container import UBUNTU_22_04_BASE_IMAGE
from linux_toolchain.models import SdkSpec
from linux_toolchain.recipes import get_recipe

MANAGED_WORKSPACE_SCHEMA = "linux-toolchain-managed-workspace"
MANAGED_WORKSPACE_FORMAT = 1
MANAGED_ARTIFACT_SCHEMA = "linux-toolchain-managed-build-artifact"
MANAGED_ARTIFACT_FORMAT = 1
MANAGED_BUILDER_BASE_IMAGE = UBUNTU_22_04_BASE_IMAGE
MANAGED_DEFAULT_HOST_GLIBC_FLOOR = "2.19"
MANAGED_MIN_AARCH64_GCC = "10"
MANAGED_TARGET_TOOL_NAMES = (
    "ar",
    "as",
    "ld",
    "nm",
    "objcopy",
    "objdump",
    "ranlib",
    "readelf",
    "strip",
)

_COMPILER_BACKEND_RECIPE = get_recipe("x86_64", "2.19")
MANAGED_COMPILER_BACKEND_VERSION = _COMPILER_BACKEND_RECIPE.builder_version
MANAGED_COMPILER_BACKEND_GCC = _COMPILER_BACKEND_RECIPE.gcc
MANAGED_COMPILER_BACKEND_SUPPLEMENTAL_SOURCES = {
    "gmp-6.3.0.tar.xz": (
        "a3c2b80201b89e68616f4ad30bc66aee4927c3ce50e33929ca819d5c43538898"
    ),
    "mpfr-4.2.2.tar.xz": (
        "b67ba0383ef7e8a8563734e2e889ef5ec3c3b898a01d00fa0a6869ad81c6ce01"
    ),
    "mpc-1.3.1.tar.gz": (
        "ab642492f5cf882b74aa0cb730cd410a81edcdbec895183ce930e706c1c759b8"
    ),
}


def managed_compiler_backend_spec(arch: str, glibc_floor: str) -> SdkSpec:
    """Select the native bootstrap compiler for one Compiler Kit host."""

    recipe = get_recipe(arch, glibc_floor)
    if (
        recipe.builder_version != MANAGED_COMPILER_BACKEND_VERSION
        or recipe.gcc != MANAGED_COMPILER_BACKEND_GCC
    ):
        raise RuntimeError("managed compiler backend catalog is inconsistent")
    return recipe.to_spec(name=f"setup-{arch}-glibc-{glibc_floor}")
