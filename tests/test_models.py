import json
import tempfile
import unittest
from pathlib import Path

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.models import SdkSpec, classify_linux_glibc_target


def valid_spec_data() -> dict[str, object]:
    return {
        "schema": "linux-toolchain-sdk-spec",
        "format": 1,
        "name": "linux-toolchain-x86_64-glibc-2.19",
        "target": {
            "arch": "x86_64",
            "vendor": "portable",
            "libc": "glibc",
            "libc_version": "2.19",
            "linux_headers": "6.12.41",
            "minimum_kernel": "3.2.0",
            "cpu": "x86-64",
        },
        "builder": {
            "backend": "crosstool-ng",
            "version": "1.28.0",
            "gcc": "9.5.0",
            "binutils": "2.45",
        },
    }


class SdkSpecTest(unittest.TestCase):
    def load(self, data: object) -> SdkSpec:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sdk.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return SdkSpec.load(path)

    def test_loads_strict_spec_and_derives_triplet(self) -> None:
        spec = self.load(valid_spec_data())
        self.assertEqual(spec.target.triplet, "x86_64-portable-linux-gnu")
        self.assertEqual(spec.target.libc_version, "2.19")
        self.assertEqual(SdkSpec.from_dict(spec.to_dict()), spec)
        self.assertNotIn("triplet", spec.to_dict()["target"])
        self.assertEqual(
            spec.to_manifest_dict()["target"]["triplet"], spec.target.triplet
        )

    def test_loads_aarch64_spec_and_derives_triplet(self) -> None:
        data = valid_spec_data()
        data["name"] = "linux-toolchain-aarch64-glibc-2.19"
        target = data["target"]
        assert isinstance(target, dict)
        target["arch"] = "aarch64"
        target["cpu"] = "armv8-a"
        target["minimum_kernel"] = "3.10.0"
        spec = self.load(data)

        self.assertEqual(spec.target.triplet, "aarch64-portable-linux-gnu")
        self.assertEqual(spec.target.arch, "aarch64")

    def test_rejects_unknown_key(self) -> None:
        data = valid_spec_data()
        data["surprise"] = True
        with self.assertRaisesRegex(ConfigurationError, "unknown keys: surprise"):
            self.load(data)

    def test_rejects_missing_or_unsupported_envelope(self) -> None:
        cases = (
            ("is missing: schema", lambda value: value.pop("schema")),
            (
                "unsupported SDK spec schema",
                lambda value: value.update({"schema": "another-schema"}),
            ),
            (
                "unsupported SDK spec format",
                lambda value: value.update({"format": 2}),
            ),
        )
        for message, mutate in cases:
            with self.subTest(message=message):
                data = valid_spec_data()
                mutate(data)
                with self.assertRaisesRegex(ConfigurationError, message):
                    self.load(data)

    def test_rejects_unsupported_architecture(self) -> None:
        data = valid_spec_data()
        data["target"]["arch"] = "armv7"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "x86_64.*aarch64"):
            self.load(data)

    def test_rejects_cpu_from_another_architecture(self) -> None:
        data = valid_spec_data()
        target = data["target"]
        assert isinstance(target, dict)
        target["cpu"] = "armv8-a"
        with self.assertRaisesRegex(ConfigurationError, "CPU for x86_64"):
            self.load(data)

    def test_rejects_aarch64_kernel_below_architecture_baseline(self) -> None:
        data = valid_spec_data()
        target = data["target"]
        assert isinstance(target, dict)
        target["arch"] = "aarch64"
        target["cpu"] = "armv8-a"
        target["minimum_kernel"] = "3.6.0"
        with self.assertRaisesRegex(ConfigurationError, r"3\.7"):
            self.load(data)

    def test_rejects_vendor_that_can_escape_target_name(self) -> None:
        data = valid_spec_data()
        data["target"]["vendor"] = "../host"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "invalid target vendor"):
            self.load(data)

    def test_linux_glibc_target_policy_table(self) -> None:
        accepted = (
            (
                "external",
                "x86_64-redhat-linux",
                None,
                "x86_64",
            ),
            (
                "strict",
                "aarch64-portable-linux-gnu",
                "aarch64",
                "aarch64",
            ),
        )
        for policy, target, expected_arch, expected in accepted:
            with self.subTest(policy=policy, target=target):
                classified = classify_linux_glibc_target(
                    target,
                    policy=policy,  # type: ignore[arg-type]
                    expected_architecture=expected_arch,
                    context="test target",
                )
                self.assertEqual(classified, expected)

        rejected = (
            ("x86_64-redhat-linux", "x86_64"),
            ("x86_64-linux-musl", "x86_64"),
            ("x86_64-w64-mingw32", "x86_64"),
            ("x86_64-linux-gnux32", "x86_64"),
            (" x86_64-linux-gnu", "x86_64"),
            ("aarch64-linux-gnu", "x86_64"),
        )
        for target, expected_arch in rejected:
            with self.subTest(policy="strict", target=target):
                with self.assertRaises(ConfigurationError):
                    classify_linux_glibc_target(
                        target,
                        policy="strict",
                        expected_architecture=expected_arch,
                        context="test target",
                    )


if __name__ == "__main__":
    unittest.main()
