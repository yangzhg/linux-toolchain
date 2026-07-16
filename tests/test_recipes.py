import unittest

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.recipes import (
    available_families,
    available_recipes,
    get_recipe,
)
from linux_toolchain.sdk.crosstool_ng import (
    COMPONENT_VERSIONS_BY_RELEASE,
    CROSSTOOL_NG_RELEASES,
)


class RecipeCatalogTest(unittest.TestCase):
    def test_catalog_exposes_one_pinned_backend(self) -> None:
        families = available_families()
        self.assertEqual([family.name for family in families], ["crosstool-ng-1.28.0"])
        self.assertEqual(families[0].builder_version, "1.28.0")
        self.assertEqual(families[0].gcc, "9.5.0")
        self.assertEqual(families[0].glibc_versions[0], "2.17")
        self.assertEqual(families[0].glibc_versions[-1], "2.42")
        self.assertGreater(len(available_recipes()), 4)

    def test_all_representative_versions_resolve(self) -> None:
        expectations = {
            "2.17": ("crosstool-ng-1.28.0", "1.28.0", "9.5.0", "6.12.41"),
            "2.19": ("crosstool-ng-1.28.0", "1.28.0", "9.5.0", "6.12.41"),
            "2.29": ("crosstool-ng-1.28.0", "1.28.0", "9.5.0", "6.12.41"),
            "2.36": ("crosstool-ng-1.28.0", "1.28.0", "9.5.0", "6.12.41"),
            "2.42": ("crosstool-ng-1.28.0", "1.28.0", "9.5.0", "6.12.41"),
        }
        for version, expected in expectations.items():
            with self.subTest(version=version):
                recipe = get_recipe("x86_64", version)
                self.assertEqual(
                    (
                        recipe.family,
                        recipe.builder_version,
                        recipe.gcc,
                        recipe.linux_headers,
                    ),
                    expected,
                )

    def test_backend_families_match_crosstool_ng_component_allowlists(self) -> None:
        for family in available_families():
            with self.subTest(family=family.name):
                self.assertIn(family.builder_version, CROSSTOOL_NG_RELEASES)
                components = COMPONENT_VERSIONS_BY_RELEASE[family.builder_version]
                self.assertEqual(family.glibc_versions, components["glibc"])
                self.assertIn(family.linux_headers, components["linux"])
                self.assertIn(family.gcc, components["gcc"])
                for architecture in family.architectures:
                    self.assertIn(architecture.binutils, components["binutils"])
                    for _, binutils in architecture.binutils_by_glibc:
                        self.assertIn(binutils, components["binutils"])

    def test_aarch64_starts_at_glibc_2_17(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError, r"predates AArch64 support.*2\.17"
        ):
            get_recipe("aarch64", "2.16")

        spec = get_recipe("aarch64", "2.17").to_spec()
        self.assertEqual(spec.target.minimum_kernel, "3.7.0")
        self.assertEqual(spec.target.cpu, "armv8-a")
        self.assertEqual(spec.builder.binutils, "2.29.1")
        self.assertEqual(get_recipe("aarch64", "2.25").binutils, "2.29.1")
        self.assertEqual(get_recipe("aarch64", "2.26").binutils, "2.45")
        self.assertEqual(get_recipe("x86_64", "2.19").binutils, "2.45")

    def test_recipe_accepts_supported_overrides(self) -> None:
        spec = get_recipe("x86_64", "2.19").to_spec(
            name="release-floor", minimum_kernel="4.4.0"
        )
        self.assertEqual(spec.name, "release-floor")
        self.assertEqual(spec.target.minimum_kernel, "4.4.0")
        self.assertEqual(spec.builder.gcc, "9.5.0")

    def test_unknown_future_glibc_explains_how_to_extend_the_catalog(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError,
            r"no pinned backend family is available for x86_64/glibc-2\.45.*"
            r"validate it before claiming release qualification",
        ):
            get_recipe("x86_64", "2.45")

    def test_rejects_minimum_kernel_newer_than_headers(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "cannot be newer"):
            get_recipe("x86_64", "2.19").to_spec(minimum_kernel="7.0")

    def test_rejects_minimum_kernel_below_family_baseline(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "cannot be lower"):
            get_recipe("x86_64", "2.19").to_spec(minimum_kernel="2.6.32")


if __name__ == "__main__":
    unittest.main()
