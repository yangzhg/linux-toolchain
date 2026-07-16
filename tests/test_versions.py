import unittest

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.versions import AbiVersion, major_version


class AbiVersionTest(unittest.TestCase):
    def test_orders_components_numerically(self) -> None:
        self.assertLess(AbiVersion.parse("2.9"), AbiVersion.parse("2.18"))
        self.assertLess(AbiVersion.parse("2.18"), AbiVersion.parse("2.19"))

    def test_rejects_non_numeric_version(self) -> None:
        for value in ("", "2.x", "2.18-rc1", ".18", "2."):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    AbiVersion.parse(value)

    def test_extracts_compiler_major(self) -> None:
        self.assertEqual(major_version("13.2.1"), 13)
        with self.assertRaises(ConfigurationError):
            major_version("release-13")


if __name__ == "__main__":
    unittest.main()
