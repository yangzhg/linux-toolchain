import unittest

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.models import ManagedSpec


def valid_managed_spec() -> dict[str, object]:
    return {
        "schema": "linux-toolchain-managed-spec",
        "format": 1,
        "name": "linux-toolchain-release",
        "build_platform": "linux/amd64",
        "host": {
            "os": "linux",
            "arch": "x86_64",
            "glibc_floor": "2.35",
        },
        "targets": [
            {"arch": "x86_64", "glibc_floor": "2.19"},
            {"arch": "x86_64", "glibc_floor": "2.24"},
        ],
        "compilers": [
            {
                "family": "gcc",
                "versions": ["10", "13.4.0", "16"],
                "runtimes": [{"kind": "libstdc++"}],
            },
            {
                "family": "clang",
                "versions": ["16", "22.1.8"],
                "runtimes": [
                    {"kind": "libstdc++", "gcc_version": "13"},
                    {"kind": "libc++"},
                ],
            },
        ],
    }


class ManagedSpecTest(unittest.TestCase):
    def test_strict_spec_supports_multiple_versions_targets_and_runtimes(self) -> None:
        spec = ManagedSpec.from_dict(valid_managed_spec())

        self.assertEqual(spec.build_platform, "linux/amd64")
        self.assertEqual(
            tuple(target.arch for target in spec.targets), ("x86_64", "x86_64")
        )
        self.assertEqual(spec.compilers[0].versions, ("10", "13.4.0", "16"))
        self.assertEqual(
            {
                (runtime.kind, runtime.gcc_version)
                for runtime in spec.compilers[1].runtimes
            },
            {("libstdc++", "13"), ("libc++", None)},
        )
        self.assertEqual(ManagedSpec.from_dict(spec.to_dict()), spec)

    def test_serialization_is_canonical_across_input_order(self) -> None:
        left = valid_managed_spec()
        right = valid_managed_spec()
        right["targets"] = list(reversed(right["targets"]))  # type: ignore[arg-type]
        right["compilers"] = list(reversed(right["compilers"]))  # type: ignore[arg-type]
        clang = right["compilers"][0]  # type: ignore[index]
        clang["versions"] = list(reversed(clang["versions"]))  # type: ignore[index]
        clang["runtimes"] = list(reversed(clang["runtimes"]))  # type: ignore[index]

        self.assertEqual(
            ManagedSpec.from_dict(left).to_dict(),
            ManagedSpec.from_dict(right).to_dict(),
        )

    def test_schema_and_unknown_fields_are_rejected(self) -> None:
        root = valid_managed_spec()
        root["surprise"] = True
        with self.assertRaisesRegex(ConfigurationError, "unknown keys: surprise"):
            ManagedSpec.from_dict(root)

        runtime = valid_managed_spec()
        runtime["compilers"][0]["runtimes"][0]["surprise"] = True  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "unknown keys: surprise"):
            ManagedSpec.from_dict(runtime)

        schema = valid_managed_spec()
        schema["schema"] = "another-schema"
        with self.assertRaisesRegex(ConfigurationError, "schema"):
            ManagedSpec.from_dict(schema)

        representation = valid_managed_spec()
        representation["format"] = 2
        with self.assertRaisesRegex(ConfigurationError, "format"):
            ManagedSpec.from_dict(representation)

    def test_only_certified_build_and_host_platforms_are_accepted(self) -> None:
        build = valid_managed_spec()
        build["build_platform"] = "linux/arm64"
        with self.assertRaisesRegex(ConfigurationError, "must match"):
            ManagedSpec.from_dict(build)

        native_arm = valid_managed_spec()
        native_arm["build_platform"] = "linux/arm64"
        native_arm["host"]["arch"] = "aarch64"  # type: ignore[index]
        native_arm["targets"] = [{"arch": "aarch64", "glibc_floor": "2.17"}]
        self.assertEqual(
            ManagedSpec.from_dict(native_arm).host.arch,
            "aarch64",
        )

    def test_aarch64_rejects_a_pre_architecture_glibc_floor(self) -> None:
        data = valid_managed_spec()
        data["build_platform"] = "linux/arm64"
        data["host"]["arch"] = "aarch64"  # type: ignore[index]
        data["targets"] = [{"arch": "aarch64", "glibc_floor": "2.16"}]
        with self.assertRaisesRegex(ConfigurationError, "2.17"):
            ManagedSpec.from_dict(data)

    def test_aarch64_rejects_gcc_below_the_supported_minimum(self) -> None:
        data = valid_managed_spec()
        data["build_platform"] = "linux/arm64"
        data["host"]["arch"] = "aarch64"  # type: ignore[index]
        data["targets"] = [{"arch": "aarch64", "glibc_floor": "2.19"}]
        data["compilers"][0]["versions"] = ["9.5.0"]  # type: ignore[index]

        with self.assertRaisesRegex(ConfigurationError, "GCC 10 or newer"):
            ManagedSpec.from_dict(data)

    def test_clang_libstdcxx_requires_an_explicit_gcc_selector(self) -> None:
        data = valid_managed_spec()
        data["compilers"][1]["runtimes"][0].pop("gcc_version")  # type: ignore[index,union-attr]
        with self.assertRaisesRegex(ConfigurationError, "explicit gcc_version"):
            ManagedSpec.from_dict(data)

    def test_libcxx_rejects_a_gcc_selector(self) -> None:
        data = valid_managed_spec()
        data["compilers"][1]["runtimes"][1]["gcc_version"] = "13"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "allowed only for libstdc"):
            ManagedSpec.from_dict(data)

    def test_gcc_requires_its_implicit_matching_runtime(self) -> None:
        data = valid_managed_spec()
        data["compilers"][0]["runtimes"] = [{"kind": "libc++"}]  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "managed GCC requires"):
            ManagedSpec.from_dict(data)

    def test_duplicate_compiler_family_and_runtime_are_rejected(self) -> None:
        compiler = valid_managed_spec()
        compiler["compilers"].append(compiler["compilers"][0])  # type: ignore[union-attr,index]
        with self.assertRaisesRegex(ConfigurationError, "one entry per family"):
            ManagedSpec.from_dict(compiler)

        runtime = valid_managed_spec()
        runtime["compilers"][1]["runtimes"].append(  # type: ignore[index,union-attr]
            {"kind": "libc++"}
        )
        with self.assertRaisesRegex(
            ConfigurationError, "runtimes cannot contain duplicates"
        ):
            ManagedSpec.from_dict(runtime)


if __name__ == "__main__":
    unittest.main()
