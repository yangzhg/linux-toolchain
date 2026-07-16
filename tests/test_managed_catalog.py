import unittest

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.catalog import (
    available_releases,
    resolve_release,
    resolve_releases,
)


class ManagedCompilerCatalogTest(unittest.TestCase):
    def test_catalog_contains_the_available_gcc_and_clang_ranges(self) -> None:
        gcc = available_releases("gcc")
        clang = available_releases("clang")

        self.assertEqual((gcc[0].version, gcc[-1].version), ("10.5.0", "16.1.0"))
        self.assertEqual(
            (clang[0].version, clang[-1].version),
            ("16.0.6", "22.1.8"),
        )
        self.assertEqual(len(gcc), 7)
        self.assertEqual(len(clang), 7)

    def test_representative_sources_have_immutable_official_identities(self) -> None:
        gcc = resolve_release("gcc", "13")
        self.assertEqual(gcc.source_kind, "archive")
        self.assertEqual(len(gcc.archive_sha512), 128)
        self.assertEqual(
            gcc.source_url,
            "https://ftpmirror.gnu.org/gcc/gcc-13.4.0/gcc-13.4.0.tar.xz",
        )

        clang = resolve_release("clang", "22")
        self.assertEqual(clang.source_kind, "archive")
        self.assertEqual(len(clang.archive_sha512), 128)
        self.assertEqual(
            clang.source_url,
            "https://github.com/llvm/llvm-project/releases/download/"
            "llvmorg-22.1.8/llvm-project-22.1.8.src.tar.xz",
        )

    def test_major_and_exact_selectors_resolve_to_the_same_pin(self) -> None:
        self.assertEqual(resolve_release("gcc", "13"), resolve_release("gcc", "13.4.0"))
        self.assertEqual(
            resolve_release("clang", "22"), resolve_release("clang", "22.1.8")
        )

    def test_multiple_selectors_are_resolved_and_sorted(self) -> None:
        releases = resolve_releases("gcc", ("16", "10", "13.4.0"))
        self.assertEqual(
            tuple(release.version for release in releases),
            ("10.5.0", "13.4.0", "16.1.0"),
        )

    def test_duplicate_major_and_exact_selection_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "duplicate exact versions"):
            resolve_releases("gcc", ("13", "13.4.0"))

    def test_unknown_or_malformed_versions_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "not in the pinned catalog"):
            resolve_release("gcc", "17")
        with self.assertRaisesRegex(ConfigurationError, "invalid managed clang"):
            resolve_release("clang", "latest")
        with self.assertRaisesRegex(ConfigurationError, "expected gcc or clang"):
            available_releases("msvc")


if __name__ == "__main__":
    unittest.main()
