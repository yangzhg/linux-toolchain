from __future__ import annotations

import re
from dataclasses import dataclass

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.versions import AbiVersion

SUPPORTED_COMPILER_FAMILIES = ("gcc", "clang")

_SHA512_RE = re.compile(r"^[0-9a-f]{128}$")
_MAJOR_SELECTOR_RE = re.compile(r"^(0|[1-9][0-9]*)$")


@dataclass(frozen=True)
class CompilerRelease:
    family: str
    version: str
    source_url: str
    archive_sha512: str

    @property
    def major(self) -> int:
        return AbiVersion.parse(self.version).parts[0]

    @property
    def source_kind(self) -> str:
        return "archive"

    @property
    def source_id(self) -> str:
        return f"{self.family}-{self.version}"

    def validate(self) -> None:
        if self.family not in SUPPORTED_COMPILER_FAMILIES:
            raise ConfigurationError(
                f"unsupported managed compiler family: {self.family!r}"
            )
        AbiVersion.parse(self.version)
        if not self.source_url.startswith("https://"):
            raise ConfigurationError(
                f"managed compiler source URL must use HTTPS: {self.source_url!r}"
            )

        if not _SHA512_RE.fullmatch(self.archive_sha512):
            raise ConfigurationError(f"{self.source_id} archive SHA-512 is invalid")

    def to_source_dict(self) -> dict[str, object]:
        return {
            "id": self.source_id,
            "family": self.family,
            "version": self.version,
            "kind": self.source_kind,
            "url": self.source_url,
            "sha512": self.archive_sha512,
        }


_GCC_SHA512 = {
    "10.5.0": "d86dbc18b978771531f4039465e7eb7c19845bf607dc513c97abf8e45ffe1086a99d98f83dfb7b37204af22431574186de9d5ff80c8c3c3a98dbe3983195bffd",
    "11.5.0": "88f17d5a5e69eeb53aaf0a9bc9daab1c4e501d145b388c5485ebeb2cc36178fbb2d3e49ebef4a8c007a05e88471a06b97cf9b08870478249f77fbfa3d4abd9a8",
    "12.5.0": "c76020e4c844b53485502cb8a4e295221c9d37487d66c9f4559031fb14c85de20602e6387310005386cb0ef25e55067d2cfef141423bb445f3b77e7456a23533",
    "13.4.0": "9b4b83ecf51ef355b868608b8d257b2fa435c06d2719cb86657a7c2c2a0828ff4ce04e9bac1055bbcad8ed5b4da524cafaef654785e23a50233d95d89201e35f",
    "14.4.0": "725ed8bdd43ef1726ffe8b5e8615a13e247fac9575b7626ae013a2975d000ea213212dc414b2f2631ac4785c1c8beca85555222faf9904d3b2fa6a3807a83a15",
    "15.3.0": "0de9e296153b52c021b1c7e63c9c62151d7a0ac03f23ce6e9f772c1b0eb783f6acdd81cc4567bfe4128a6f64968c2cfc8eff40b36229cba7425349f7d637c654",
    "16.1.0": "b3454958891ab47e1e5b6cb9396c0ad3b04f32fe2a7bf1153a143f21013fdb6b295ca94c98964698a688e4c1d7555ffd8ffbc20187507cce6b1c32cbcc09897a",
}

_LLVM_SHA512 = {
    "16.0.6": "89a67ebfbbc764cc456e8825ecfa90707741f8835b1b2adffae0b227ab1fe5ca9cce75b0efaffc9ca8431cae528dc54fd838867a56a2b645344d9e82d19ab1b7",
    "17.0.6": "6d85bf749e0d77553cc215cbfa61cec4ac4f4f652847f56f946b6a892a99a5ea40b6ab8b39a9708a035001f007986941ccf17e4635260a8b0c1fa59e78d41e30",
    "18.1.8": "25eeee9984c8b4d0fbc240df90f33cbb000d3b0414baff5c8982beafcc5e59e7ef18f6f85d95b3a5f60cb3d4cd4f877c80487b5768bc21bc833f107698ad93db",
    "19.1.7": "c7d63286d662707a9cd54758c9e3aaf52794a91900c484c4a6efa62d90bc719d5e7a345e4192feeb0c9fd11c82570d64677c781e5be1d645556b6aa018e47ec8",
    "20.1.8": "f330e72e6a1da468569049437cc0ba7a41abb816ccece7367189344f7ebfef730f4788ac7af2bef0aa8a49341c15ab1d31e941ffa782f264d11fe0dc05470773",
    "21.1.8": "cae4c44e7bf678071723da63ad5839491d717a7233e7f4791aa408207f3ea42f52de939ad15189b112c02a0770f1bb8d59bae6ad31ef53417a6eea7770fe52ab",
    "22.1.8": "2615b20ba08534f83ab8ecc7b5ba43b5f1dfcf9cdb2534a32fcdbf0ccdd9a008b46276e45ef26ed9377f65b5e4ae89ea798f3863fd034484b5715140f3a7b35c",
}


COMPILER_RELEASES = tuple(
    [
        CompilerRelease(
            family="gcc",
            version=version,
            source_url=(
                f"https://ftpmirror.gnu.org/gcc/gcc-{version}/gcc-{version}.tar.xz"
            ),
            archive_sha512=sha512,
        )
        for version, sha512 in _GCC_SHA512.items()
    ]
    + [
        CompilerRelease(
            family="clang",
            version=version,
            source_url=(
                "https://github.com/llvm/llvm-project/releases/download/"
                f"llvmorg-{version}/llvm-project-{version}.src.tar.xz"
            ),
            archive_sha512=sha512,
        )
        for version, sha512 in _LLVM_SHA512.items()
    ]
)

for _release in COMPILER_RELEASES:
    _release.validate()


def available_releases(family: str | None = None) -> tuple[CompilerRelease, ...]:
    if family is not None and family not in SUPPORTED_COMPILER_FAMILIES:
        raise ConfigurationError(
            f"unsupported managed compiler family: {family!r}; expected gcc or clang"
        )
    return tuple(
        release
        for release in COMPILER_RELEASES
        if family is None or release.family == family
    )


def resolve_release(family: str, selector: str) -> CompilerRelease:
    if family not in SUPPORTED_COMPILER_FAMILIES:
        raise ConfigurationError(
            f"unsupported managed compiler family: {family!r}; expected gcc or clang"
        )
    if not isinstance(selector, str) or not selector:
        raise ConfigurationError("managed compiler version selector must be a string")

    releases = available_releases(family)
    if _MAJOR_SELECTOR_RE.fullmatch(selector):
        major = int(selector)
        candidates = tuple(release for release in releases if release.major == major)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise ConfigurationError(
                f"managed {family} major selector {selector!r} is ambiguous; "
                "use an exact catalog version"
            )
    else:
        try:
            AbiVersion.parse(selector)
        except ConfigurationError as error:
            raise ConfigurationError(
                f"invalid managed {family} version selector: {selector!r}"
            ) from error
        for release in releases:
            if release.version == selector:
                return release

    available = ", ".join(release.version for release in releases)
    raise ConfigurationError(
        f"managed {family} version {selector!r} is not in the pinned catalog; "
        f"available exact versions: {available}"
    )


def resolve_releases(
    family: str, selectors: tuple[str, ...]
) -> tuple[CompilerRelease, ...]:
    if not selectors:
        raise ConfigurationError(f"managed {family} compiler selection cannot be empty")
    resolved = tuple(resolve_release(family, selector) for selector in selectors)
    versions = tuple(release.version for release in resolved)
    if len(versions) != len(set(versions)):
        raise ConfigurationError(
            f"managed {family} selectors resolve to duplicate exact versions"
        )
    return tuple(
        sorted(resolved, key=lambda release: AbiVersion.parse(release.version))
    )
