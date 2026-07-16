import copy
import tempfile
import unittest
from pathlib import Path

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.lockfile import (
    ManagedLock,
    canonical_json_sha256,
    resolve_lock,
    write_lockfile,
)
from linux_toolchain.managed.models import ManagedSpec
from tests.test_managed_models import valid_managed_spec


class ManagedLockfileTest(unittest.TestCase):
    def setUp(self) -> None:
        data = valid_managed_spec()
        data["compilers"][0]["versions"] = ["10", "13"]  # type: ignore[index]
        self.spec = ManagedSpec.from_dict(data)

    def test_resolution_builds_a_deduplicated_artifact_dag(self) -> None:
        lock = resolve_lock(self.spec)

        self.assertEqual(len(lock.sources), 4)
        self.assertEqual(len(lock.compiler_kits), 4)
        self.assertEqual(len(lock.runtimes), 8)
        self.assertEqual(len(lock.variants), 12)
        self.assertEqual(
            {source.id for source in lock.sources},
            {"gcc-10.5.0", "gcc-13.4.0", "clang-16.0.6", "clang-22.1.8"},
        )
        self.assertEqual(len(lock.catalog_sha256), 64)
        self.assertEqual(lock.spec_sha256, canonical_json_sha256(self.spec.to_dict()))

    def test_compiler_kit_identity_includes_the_host_glibc_floor(self) -> None:
        first = resolve_lock(self.spec)
        second_spec = self.spec.to_dict()
        second_spec["host"]["glibc_floor"] = "2.28"  # type: ignore[index]
        second = resolve_lock(ManagedSpec.from_dict(second_spec))

        self.assertNotEqual(
            {kit.id for kit in first.compiler_kits},
            {kit.id for kit in second.compiler_kits},
        )
        self.assertEqual(
            {runtime.id for runtime in first.runtimes},
            {runtime.id for runtime in second.runtimes},
        )
        self.assertTrue(all("-glibc-2.35-to-" in kit.id for kit in first.compiler_kits))

    def test_clang_runtime_modes_resolve_to_the_correct_exact_sources(self) -> None:
        lock = resolve_lock(self.spec)
        clang_variants = [
            variant for variant in lock.variants if variant.family == "clang"
        ]

        for variant in clang_variants:
            runtime = next(
                item for item in lock.runtimes if item.id == variant.runtime_id
            )
            if variant.cxx_runtime == "libstdc++":
                self.assertEqual(runtime.provider_family, "gcc")
                self.assertEqual(runtime.provider_version, "13.4.0")
            else:
                self.assertEqual(runtime.provider_family, "llvm")
                self.assertEqual(runtime.provider_version, variant.version)

    def test_gcc_runtime_follows_each_exact_compiler_version(self) -> None:
        lock = resolve_lock(self.spec)
        for variant in lock.variants:
            if variant.family != "gcc":
                continue
            runtime = next(
                item for item in lock.runtimes if item.id == variant.runtime_id
            )
            self.assertEqual(runtime.provider_family, "gcc")
            self.assertEqual(runtime.provider_version, variant.version)
            self.assertEqual(
                variant.id,
                f"toolchain-gcc-{variant.version}-{variant.target.arch}-"
                f"glibc-{variant.target.glibc_floor}-libstdcxx",
            )

    def test_variant_id_names_only_an_independent_runtime_release(self) -> None:
        lock = resolve_lock(self.spec)
        clang_libcxx = next(
            item
            for item in lock.variants
            if item.family == "clang" and item.cxx_runtime == "libc++"
        )
        clang_libstdcxx = next(
            item
            for item in lock.variants
            if item.family == "clang" and item.cxx_runtime == "libstdc++"
        )
        self.assertTrue(clang_libcxx.id.endswith("-libcxx"))
        self.assertTrue(clang_libstdcxx.id.endswith("-libstdcxx-gcc-13.4.0"))

    def test_lock_round_trips_through_strict_json(self) -> None:
        lock = resolve_lock(self.spec)
        self.assertEqual(ManagedLock.from_dict(lock.to_dict()), lock)

        with tempfile.TemporaryDirectory() as directory:
            path = write_lockfile(lock, Path(directory) / "managed.lock.json")
            self.assertEqual(ManagedLock.load(path), lock)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_lock_output_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("unchanged", encoding="utf-8")
            output = root / "managed.lock.json"
            output.symlink_to(target)
            with self.assertRaisesRegex(ConfigurationError, "cannot be a symlink"):
                write_lockfile(resolve_lock(self.spec), output, force=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

    def test_unknown_keys_and_modified_spec_hash_are_rejected(self) -> None:
        lock_data = resolve_lock(self.spec).to_dict()
        lock_data["surprise"] = True
        with self.assertRaisesRegex(ConfigurationError, "unknown keys: surprise"):
            ManagedLock.from_dict(lock_data)

        changed = resolve_lock(self.spec).to_dict()
        changed["spec"]["name"] = "changed"  # type: ignore[index]
        changed["name"] = "changed"
        with self.assertRaisesRegex(ConfigurationError, "spec_sha256"):
            ManagedLock.from_dict(changed)

    def test_missing_and_mismatched_artifact_references_are_rejected(self) -> None:
        missing = resolve_lock(self.spec).to_dict()
        missing["variants"][0]["runtime_id"] = "runtime-missing"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "missing artifact reference"):
            ManagedLock.from_dict(missing)

        mismatched = copy.deepcopy(resolve_lock(self.spec).to_dict())
        first = mismatched["variants"][0]  # type: ignore[index]
        other_runtime = next(
            runtime
            for runtime in mismatched["runtimes"]  # type: ignore[union-attr]
            if runtime["target"] != first["target"]
        )
        first["runtime_id"] = other_runtime["id"]
        with self.assertRaisesRegex(ConfigurationError, "runtime target"):
            ManagedLock.from_dict(mismatched)

    def test_artifact_ids_must_match_their_full_selections(self) -> None:
        compiler = resolve_lock(self.spec).to_dict()
        compiler["compiler_kits"][0]["id"] = "compiler-gcc-10.5.0-x86_64"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, r"compiler_kits\[0\]\.id"):
            ManagedLock.from_dict(compiler)

        runtime = resolve_lock(self.spec).to_dict()
        runtime["runtimes"][0]["id"] = "runtime-gcc-10.5.0-x86_64"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, r"runtimes\[0\]\.id"):
            ManagedLock.from_dict(runtime)


if __name__ == "__main__":
    unittest.main()
