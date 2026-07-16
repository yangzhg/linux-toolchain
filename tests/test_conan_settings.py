import tempfile
import unittest
from pathlib import Path

from linux_toolchain.conan.settings import SETTINGS_USER_YAML, write_settings_user
from linux_toolchain.errors import ConfigurationError


class ConanSettingsTest(unittest.TestCase):
    def test_extends_linux_abi_settings(self) -> None:
        self.assertIn("libc_version:\n      - null\n      - ANY", SETTINGS_USER_YAML)
        self.assertIn(
            "kernel_headers_version:\n      - null\n      - ANY",
            SETTINGS_USER_YAML,
        )
        self.assertIn(
            "minimum_kernel_version:\n      - null\n      - ANY",
            SETTINGS_USER_YAML,
        )
        for compiler in ("gcc", "clang"):
            with self.subTest(compiler=compiler):
                section = SETTINGS_USER_YAML.split(f"  {compiler}:\n", 1)[1]
                if compiler == "gcc":
                    section = section.split("  clang:\n", 1)[0]
                self.assertIn("version:\n      - ANY", section)

    def test_existing_different_settings_are_preserved_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings_user.yml"
            original = "custom: true\n"
            path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigurationError, "refusing to overwrite existing Conan settings"
            ):
                write_settings_user(path)

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_force_overwrites_existing_different_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings_user.yml"
            path.write_text("custom: true\n", encoding="utf-8")

            self.assertEqual(write_settings_user(path, force=True), path)

            self.assertEqual(path.read_text(encoding="utf-8"), SETTINGS_USER_YAML)


if __name__ == "__main__":
    unittest.main()
