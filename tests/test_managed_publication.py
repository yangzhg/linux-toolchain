from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.licenses import license_evidence
from linux_toolchain.managed.contracts import managed_compiler_backend_spec
from linux_toolchain.managed.identity import (
    managed_action_sha256,
    managed_artifact_action_for_specs,
    runtime_publication_action,
)
from linux_toolchain.managed.lockfile import resolve_lock
from linux_toolchain.managed.models import ManagedSpec
from linux_toolchain.managed.publication import (
    MANAGED_PUBLICATION_FILE,
    _load_managed_llvm_source_evidence,
    load_managed_runtime_artifact,
    load_managed_runtime_publication,
    publish_managed_runtime,
)
from linux_toolchain.managed.selection import select_artifact
from linux_toolchain.publication import normalize_public_tree, replace_directory
from linux_toolchain.recipes import get_recipe


def managed_lock():
    return resolve_lock(
        ManagedSpec.from_dict(
            {
                "schema": "linux-toolchain-managed-spec",
                "format": 1,
                "name": "publication-test",
                "build_platform": "linux/amd64",
                "host": {
                    "os": "linux",
                    "arch": "x86_64",
                    "glibc_floor": "2.35",
                },
                "targets": [{"arch": "x86_64", "glibc_floor": "2.19"}],
                "compilers": [
                    {
                        "family": "gcc",
                        "versions": ["13"],
                        "runtimes": [{"kind": "libstdc++"}],
                    },
                    {
                        "family": "clang",
                        "versions": ["22"],
                        "runtimes": [{"kind": "libc++"}],
                    },
                ],
            }
        )
    )


class ManagedPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = managed_lock()

    def runtime_id(self, kind: str) -> str:
        return next(entry.id for entry in self.lock.runtimes if entry.kind == kind)

    def equivalent_lock(self):
        spec = self.lock.spec.to_dict()
        spec["name"] = "publication-test-second-lock"
        return resolve_lock(ManagedSpec.from_dict(spec))

    @staticmethod
    def write_licenses(
        artifacts: Path,
        *,
        family: str,
    ) -> dict[str, object]:
        required = (
            ("gcc/COPYING", "gcc/COPYING.RUNTIME")
            if family == "gcc"
            else (
                "llvm-project/llvm/LICENSE.TXT",
                "llvm-project/clang/LICENSE.TXT",
                "llvm-project/compiler-rt/LICENSE.TXT",
                "llvm-project/libcxx/LICENSE.TXT",
                "llvm-project/libcxxabi/LICENSE.TXT",
                "llvm-project/libunwind/LICENSE.TXT",
            )
        )
        for relative in required:
            path = artifacts / "licenses" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{relative}\n", encoding="utf-8")
        return license_evidence(artifacts, context="test managed artifact")

    def write_artifact(self, root: Path, artifact_id: str) -> Path:
        selection = select_artifact(self.lock, artifact_id)
        artifacts = root / artifact_id
        runtime = artifacts / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "payload.txt").write_text("runtime\n", encoding="utf-8")
        licenses = self.write_licenses(artifacts, family=selection.family)
        sdk = get_recipe(
            selection.target_arch,
            selection.target_glibc_floor,
        ).to_spec()
        action = managed_artifact_action_for_specs(
            selection,
            sdk,
            managed_compiler_backend_spec(
                selection.build_host.arch,
                selection.build_host.glibc_floor,
            ),
        )
        manifest = {
            "schema": "linux-toolchain-managed-build-artifact",
            "format": 1,
            "action": action,
            "action_sha256": managed_action_sha256(action),
            "provenance": {
                "source": {"url": selection.source.url},
                "builder_image": {
                    "id": "sha256:" + "a" * 64,
                    "os": "linux",
                    "architecture": "amd64",
                    "repo_digests": [],
                },
                "execution_script": {
                    "path": "build/build.sh",
                    "sha256": "b" * 64,
                    "paired_runtime": False,
                },
            },
            "licenses": licenses,
            "elf_audit": {
                "audited_elf_files": 1,
                "audited_shared_libraries": 1,
                "max_required_glibc": "2.19",
            },
        }
        (artifacts / "artifact.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return artifacts

    @staticmethod
    def write_published_runtime(output: Path, version: str) -> Path:
        runtime = output / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "payload.txt").write_text("published\n", encoding="utf-8")
        normalize_public_tree(runtime)
        licenses = output / "licenses"
        licenses.mkdir()
        (licenses / "NOTICE").write_text("test notice\n", encoding="utf-8")
        namespaces = ("GLIBC", "GLIBCXX", "CXXABI", "GCC")
        locations = {
            "runtime": "runtime",
            "cxx_include_dirs": ["runtime/include/c++"],
            "gcc_runtime_dir": "runtime/lib/gcc",
            "library_dirs": ["runtime/lib"],
            "crt_objects": ["runtime/lib/crtbegin.o", "runtime/lib/crtend.o"],
            "static_libraries": ["runtime/lib/libgcc.a", "runtime/lib/libstdc++.a"],
            "shared_libraries": [
                "runtime/lib/libgcc_s.so.1",
                "runtime/lib/libstdc++.so.6",
            ],
        }
        symbol_report = {
            "path": "runtime/lib/libstdc++.so.6",
            "machine": "x86_64",
            "elf_class": "ELF64",
            "endianness": "little",
            "required_versions": {key: [] for key in namespaces},
            "max_required_versions": {key: None for key in namespaces},
        }
        manifest = output / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": "linux-toolchain-gcc-runtime",
                    "format": 1,
                    "provider": {
                        "name": "gcc",
                        "version": version,
                        "major": int(version.split(".")[0]),
                    },
                    "arch": "x86_64",
                    "target": "x86_64-portable-linux-gnu",
                    "glibc_floor": "2.19",
                    "locations": locations,
                    "version_symbol_reports": [symbol_report],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return manifest

    def gcc_publication_fixture(self, root: Path):
        artifact_id = self.runtime_id("gcc-runtime")
        artifacts = self.write_artifact(root, artifact_id)
        output = root / "published"
        selection = select_artifact(self.lock, artifact_id)

        def import_gcc(*args, **_kwargs):
            return self.write_published_runtime(Path(args[3]), selection.version)

        return artifact_id, artifacts, output, selection, import_gcc

    def test_loads_a_runtime_that_matches_its_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_id = self.runtime_id("gcc-runtime")
            artifacts = self.write_artifact(root, artifact_id)
            manifest_path = artifacts / "artifact.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["provenance"]["source"]["url"] = (
                "https://mirror.invalid/gcc.tar.xz"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            loaded = load_managed_runtime_artifact(
                self.lock, artifact_id, manifest_path
            )

            self.assertEqual(loaded.root, artifacts)
            self.assertEqual(loaded.payload, artifacts / "runtime")
            self.assertEqual(loaded.target, "x86_64-portable-linux-gnu")
            self.assertNotIn("host", loaded.selection.to_dict())

            second_lock = self.equivalent_lock()
            self.assertNotEqual(second_lock.sha256, self.lock.sha256)
            reused = load_managed_runtime_artifact(
                second_lock, artifact_id, artifacts / "artifact.json"
            )
            self.assertEqual(reused.root, artifacts)

    def test_rejects_unknown_fields_and_changed_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_id = self.runtime_id("gcc-runtime")
            second_root = root / "second"
            artifacts = self.write_artifact(second_root, artifact_id)
            manifest_path = artifacts / "artifact.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["catalog_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ConfigurationError, "manifest fields"):
                load_managed_runtime_artifact(self.lock, artifact_id, artifacts)

            del manifest["catalog_sha256"]
            manifest["action"]["artifact"]["version"] = "0.0"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_managed_runtime_artifact(self.lock, artifact_id, artifacts)

    def test_publishes_gcc_with_locked_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_id, artifacts, output, selection, import_gcc = (
                self.gcc_publication_fixture(root)
            )
            result = output / "manifest.json"

            with (
                patch(
                    "linux_toolchain.runtime.importer._materialize_gcc_runtime",
                    side_effect=import_gcc,
                ) as importer,
                patch(
                    "linux_toolchain.runtime.importer.validate_runtime_manifest"
                ) as validate,
                patch(
                    "linux_toolchain.managed.publication.replace_directory",
                    wraps=replace_directory,
                ) as replace,
            ):
                actual = publish_managed_runtime(
                    self.lock, artifact_id, artifacts, output
                )

            self.assertEqual(actual, result)
            self.assertEqual(importer.call_count, 1)
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(validate.call_args.args, (output,))
            call = importer.call_args
            self.assertEqual(call.args[:3], (artifacts / "runtime", "2.19", "x86_64"))
            staging = call.args[3]
            self.assertEqual(staging.parent, output.parent)
            self.assertTrue(staging.name.startswith(f".{output.name}.staging-"))
            self.assertEqual(
                call.kwargs,
                {
                    "provider_version": selection.version,
                    "target": "x86_64-portable-linux-gnu",
                    "licenses": artifacts,
                },
            )
            self.assertTrue((output / MANAGED_PUBLICATION_FILE).is_file())
            publication = load_managed_runtime_publication(
                self.lock, artifact_id, output
            )
            self.assertEqual(publication.root, output.resolve())
            self.assertEqual(
                publication.receipt["publication_action"]["raw_action_sha256"],
                json.loads((artifacts / "artifact.json").read_text())["action_sha256"],
            )
            second_lock = self.equivalent_lock()
            reused = load_managed_runtime_publication(second_lock, artifact_id, output)
            self.assertEqual(reused.root, output.resolve())

    def test_managed_llvm_source_evidence_checks_the_catalog_pin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_id = self.runtime_id("llvm-runtime")
            artifacts = self.write_artifact(root, artifact_id)
            manifest_path = artifacts / "artifact.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["action"]["source"]["sha512"] = "0" * 128
            manifest["action_sha256"] = managed_action_sha256(manifest["action"])
            manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
            selection = select_artifact(self.lock, artifact_id)

            with self.assertRaisesRegex(ConfigurationError, "pinned catalog"):
                _load_managed_llvm_source_evidence(
                    manifest_path,
                    artifacts / "runtime",
                    version=selection.version,
                    glibc_floor=selection.target_glibc_floor,
                    arch=selection.target_arch,
                    target="x86_64-portable-linux-gnu",
                )

    def test_receipt_failure_leaves_no_partial_output_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_id, artifacts, output, _, import_gcc = (
                self.gcc_publication_fixture(root)
            )

            with (
                patch(
                    "linux_toolchain.runtime.importer._materialize_gcc_runtime",
                    side_effect=import_gcc,
                ) as importer,
                patch("linux_toolchain.runtime.importer.validate_runtime_manifest"),
            ):
                with (
                    patch(
                        "linux_toolchain.managed.publication._write_publication_receipt",
                        side_effect=ConfigurationError(
                            "synthetic receipt write failure"
                        ),
                    ),
                    self.assertRaisesRegex(
                        ConfigurationError,
                        "receipt write failure",
                    ),
                ):
                    publish_managed_runtime(
                        self.lock,
                        artifact_id,
                        artifacts,
                        output,
                    )

                self.assertFalse(output.exists())
                self.assertEqual(
                    list(root.glob(f".{output.name}.staging-*")),
                    [],
                )
                result = publish_managed_runtime(
                    self.lock,
                    artifact_id,
                    artifacts,
                    output,
                )

            self.assertEqual(importer.call_count, 2)
            self.assertEqual(result, output / "manifest.json")
            load_managed_runtime_publication(self.lock, artifact_id, output)

    def test_publication_receipt_rejects_unknown_fields_and_wrong_raw_action(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_id, artifacts, output, _, import_gcc = (
                self.gcc_publication_fixture(root)
            )

            with (
                patch(
                    "linux_toolchain.runtime.importer._materialize_gcc_runtime",
                    side_effect=import_gcc,
                ),
                patch("linux_toolchain.runtime.importer.validate_runtime_manifest"),
            ):
                publish_managed_runtime(self.lock, artifact_id, artifacts, output)

            receipt_path = output / MANAGED_PUBLICATION_FILE
            valid_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt = {**valid_receipt, "catalog_sha256": "0" * 64}
            receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError,
                "unknown: catalog_sha256",
            ):
                load_managed_runtime_publication(self.lock, artifact_id, output)

            receipt = dict(valid_receipt)
            wrong_raw_identity = "0" * 64
            publication_action = runtime_publication_action(
                wrong_raw_identity,
                adapter="import_gcc_runtime",
            )
            receipt["publication_action"] = publication_action
            receipt["publication_action_sha256"] = managed_action_sha256(
                publication_action
            )
            receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigurationError,
                "raw action does not match its lock",
            ):
                load_managed_runtime_publication(self.lock, artifact_id, output)


if __name__ == "__main__":
    unittest.main()
