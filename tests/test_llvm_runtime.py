from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_toolchain.elf.models import ElfMetadata, VersionNeed
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.licenses import license_evidence
from linux_toolchain.managed.catalog import resolve_release
from linux_toolchain.managed.identity import managed_action_sha256
from linux_toolchain.runtime.llvm import (
    import_llvm_runtime,
    validate_llvm_runtime_manifest,
)
from linux_toolchain.runtime.llvm_models import (
    LLVM_RUNTIME_MANIFEST_FORMAT,
    LLVM_RUNTIME_MANIFEST_SCHEMA,
    LlvmRuntimeManifest,
    LlvmRuntimeSourceEvidence,
    load_llvm_runtime_manifest,
)


class FakeInspector:
    def __init__(
        self,
        *,
        extra_needed: str | None = None,
        machine: str = "x86_64",
        rpath: tuple[str, ...] = (),
        soname_overrides: dict[str, str] | None = None,
    ) -> None:
        self.extra_needed = extra_needed
        self.machine = machine
        self.rpath = rpath
        self.soname_overrides = soname_overrides or {}

    def inspect(self, path: Path) -> ElfMetadata:
        if path.name.startswith("clang_rt.crt"):
            return ElfMetadata(
                path=path,
                elf_class="ELF64",
                endianness="little",
                elf_type="REL",
                machine=self.machine,
                interpreter=None,
                needed=(),
                rpath=(),
                runpath=(),
                has_dt_relr=False,
                version_needs=(),
            )
        names = {
            "libc++.so.1.0": "libc++.so.1",
            "libc++abi.so.1.0": "libc++abi.so.1",
            "libunwind.so.1.0": "libunwind.so.1",
        }
        needed = ("libc.so.6",)
        if self.extra_needed is not None:
            needed += (self.extra_needed,)
        return ElfMetadata(
            path=path,
            elf_class="ELF64",
            endianness="little",
            elf_type="DYN",
            machine=self.machine,
            interpreter=None,
            needed=needed,
            rpath=self.rpath,
            runpath=(),
            has_dt_relr=False,
            version_needs=(VersionNeed(library="libc.so.6", name="GLIBC_2.18"),),
            soname=self.soname_overrides.get(path.name, names[path.name]),
        )

    def inspect_archive(self, path: Path) -> tuple[ElfMetadata, ...]:
        return (
            ElfMetadata(
                path=Path(f"{path}(member.o)"),
                elf_class="ELF64",
                endianness="little",
                elf_type="REL",
                machine=self.machine,
                interpreter=None,
                needed=(),
                rpath=(),
                runpath=(),
                has_dt_relr=False,
                version_needs=(),
            ),
        )


class LlvmRuntimeTest(unittest.TestCase):
    version = "22.1.8"
    floor = "2.18"
    target = "x86_64-portable-linux-gnu"

    @classmethod
    def source_evidence(cls) -> LlvmRuntimeSourceEvidence:
        release = resolve_release("clang", cls.version)
        return LlvmRuntimeSourceEvidence.from_dict(
            {
                "kind": "managed-artifact",
                "version": cls.version,
                "target": cls.target,
                "url": release.source_url,
                "sha512": release.archive_sha512,
            }
        )

    @classmethod
    def write_provenance(
        cls,
        prefix: Path,
    ) -> Path:
        release = resolve_release("clang", cls.version)
        action = {
            "artifact": {
                "kind": "runtime",
                "family": "clang",
                "version": cls.version,
                "target": {"arch": "x86_64", "glibc_floor": cls.floor},
                "runtime_kind": "llvm-runtime",
            },
            "source": {
                "kind": "archive",
                "sha512": release.archive_sha512,
            },
            "sdk": {},
            "target_tools": {"triplet": cls.target, "tools": []},
            "compiler_backend": {},
            "builder": {},
            "script": {},
        }
        manifest = {
            "schema": "linux-toolchain-managed-build-artifact",
            "format": 1,
            "action": action,
            "action_sha256": managed_action_sha256(action),
            "provenance": {
                "source": {"url": release.source_url},
                "builder_image": {},
                "execution_script": {},
            },
            "licenses": {},
            "elf_audit": {},
        }
        path = prefix.parent / "artifact.json"
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    @classmethod
    def make_prefix(cls, root: Path) -> Path:
        prefix = root / "artifacts" / "runtime"
        headers = prefix / "include" / "c++" / "v1"
        headers.mkdir(parents=True)
        for name in ("__config", "cstddef", "vector"):
            (headers / name).write_text(f"// libc++ {name}\n", encoding="utf-8")
        target_headers = prefix / "include" / cls.target / "c++" / "v1"
        target_headers.mkdir(parents=True)
        (target_headers / "__config_site").write_text(
            "// generated libc++ configuration\n", encoding="utf-8"
        )

        resource = prefix / "lib" / "clang" / "22"
        (resource / "include").mkdir(parents=True)
        (resource / "include" / "stddef.h").write_text(
            "// resource header\n", encoding="utf-8"
        )
        builtins = resource / "lib" / "linux" / "libclang_rt.builtins-x86_64.a"
        builtins.parent.mkdir(parents=True)
        builtins.write_bytes(b"!<arch>\n")
        for kind in ("begin", "end"):
            (resource / "lib" / "linux" / f"clang_rt.crt{kind}-x86_64.o").write_bytes(
                b"\x7fELFcrt"
            )

        library = prefix / "lib"
        for stem in ("libc++", "libc++abi", "libunwind"):
            (library / f"{stem}.a").write_bytes(b"!<arch>\n")
            (library / f"{stem}.so.1.0").write_bytes(b"\x7fELFpayload")
            (library / f"{stem}.so.1").symlink_to(f"{stem}.so.1.0")
            if stem == "libc++":
                (library / "libc++.so").write_text(
                    "INPUT(libc++.so.1 -lc++abi -lunwind)\n",
                    encoding="utf-8",
                )
            else:
                (library / f"{stem}.so").symlink_to(f"{stem}.so.1")
        for relative in (
            "llvm/LICENSE.TXT",
            "clang/LICENSE.TXT",
            "compiler-rt/LICENSE.TXT",
            "libcxx/LICENSE.TXT",
            "libcxxabi/LICENSE.TXT",
            "libunwind/LICENSE.TXT",
        ):
            license_path = prefix / "licenses/llvm-project" / relative
            license_path.parent.mkdir(parents=True, exist_ok=True)
            license_path.write_text(f"{relative}\n", encoding="utf-8")
        cls.write_provenance(prefix)
        return prefix

    def import_fixture(
        self,
        root: Path,
        *,
        inspector: FakeInspector | None = None,
    ) -> tuple[Path, LlvmRuntimeManifest]:
        output = root / "export"
        fake = inspector or FakeInspector()
        prefix = self.make_prefix(root)
        with patch("linux_toolchain.runtime.llvm.ReadElfInspector", return_value=fake):
            manifest_path = import_llvm_runtime(
                prefix,
                self.version,
                self.floor,
                "x86_64",
                self.target,
                output,
                source_evidence=self.source_evidence(),
            )
        return output, load_llvm_runtime_manifest(manifest_path)

    def test_imports_filtered_libcxx_runtime_and_validates_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output, manifest = self.import_fixture(Path(directory))

            self.assertEqual(manifest.schema, LLVM_RUNTIME_MANIFEST_SCHEMA)
            self.assertEqual(manifest.format, LLVM_RUNTIME_MANIFEST_FORMAT)
            self.assertEqual(manifest.provider["version"], "22.1.8")
            self.assertEqual(manifest.source["kind"], "managed-artifact")
            self.assertNotIn(str(Path(directory)), json.dumps(manifest.to_dict()))
            self.assertEqual(manifest.validation["final_link"], "binding-required")
            self.assertNotIn("static_pic", manifest.validation)
            self.assertEqual(manifest.abi["standard_library"], "libc++")
            self.assertEqual(manifest.abi["rtlib"], "compiler-rt")
            self.assertEqual(manifest.abi["linkage"], "both")
            self.assertEqual(
                {Path(path).name for path in manifest.locations["static_libraries"]},
                {"libc++.a", "libc++abi.a", "libunwind.a"},
            )
            self.assertEqual(
                manifest.forbidden_sonames,
                ("libgcc_s.so.1", "libstdc++.so.6"),
            )
            self.assertFalse((output / "runtime/bin").exists())
            self.assertEqual(len(manifest.locations["cxx_include_dirs"]), 2)
            self.assertTrue(
                any(
                    (output / path / "__config_site").is_file()
                    for path in manifest.locations["cxx_include_dirs"]
                )
            )
            self.assertTrue((output / str(manifest.locations["builtins"])).is_file())
            self.assertTrue(manifest.version_symbol_reports)
            validate_llvm_runtime_manifest(output, inspector=FakeInspector())
            self.assertTrue((output / "license-manifest.json").is_file())
            (output / "licenses/llvm-project/libunwind/LICENSE.TXT").unlink()
            evidence = license_evidence(output, context="test LLVM runtime")
            (output / "license-manifest.json").write_text(
                json.dumps(evidence), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "missing required license files.*libunwind/LICENSE.TXT",
            ):
                validate_llvm_runtime_manifest(output, inspector=FakeInspector())

    def test_manifest_loading_does_not_consult_the_current_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output, _ = self.import_fixture(Path(directory))
            with patch(
                "linux_toolchain.managed.catalog.resolve_release",
                side_effect=AssertionError(
                    "manifest loading must not consult the current catalog"
                ),
            ):
                loaded = load_llvm_runtime_manifest(output)
            self.assertEqual(loaded.provider["version"], self.version)

    def test_rejects_libstdcxx_or_libgcc_dependency(self) -> None:
        for soname in ("libstdc++.so.6", "libgcc_s.so.1"):
            with (
                self.subTest(soname=soname),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                prefix = self.make_prefix(root)
                with (
                    patch(
                        "linux_toolchain.runtime.llvm.ReadElfInspector",
                        return_value=FakeInspector(extra_needed=soname),
                    ),
                    self.assertRaisesRegex(ExternalToolError, "forbidden runtime"),
                ):
                    import_llvm_runtime(
                        prefix,
                        "22.1.8",
                        "2.18",
                        "x86_64",
                        "x86_64-portable-linux-gnu",
                        root / "export",
                        source_evidence=self.source_evidence(),
                    )

    def test_requires_matching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.make_prefix(root)
            with self.assertRaisesRegex(ConfigurationError, "does not match"):
                import_llvm_runtime(
                    prefix,
                    "22.1.8",
                    "2.18",
                    "aarch64",
                    "x86_64-portable-linux-gnu",
                    root / "export",
                    source_evidence=self.source_evidence(),
                )

    def test_requires_exactly_one_trusted_source_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.make_prefix(root)
            arguments = (
                prefix,
                self.version,
                self.floor,
                "x86_64",
                self.target,
                root / "export",
            )
            with self.assertRaisesRegex(ConfigurationError, "exactly one"):
                import_llvm_runtime(*arguments)
            with self.assertRaisesRegex(ConfigurationError, "exactly one"):
                import_llvm_runtime(
                    *arguments,
                    source_evidence=self.source_evidence(),
                    probe_clang=root / "clang",
                )

    def test_accepts_exact_external_clang_probe_without_recording_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.make_prefix(root)
            resource = prefix / "lib/clang/22"
            clang = root / "clang-probe"
            clang.write_text(
                "#!/bin/sh\n"
                'case " $* " in\n'
                f"  *\" --version \"*) echo 'clang version {self.version}' ;;\n"
                f"  *\" -dumpmachine \"*) echo '{self.target}' ;;\n"
                f"  *\" -print-resource-dir \"*) echo '{resource}' ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            clang.chmod(0o755)
            with patch(
                "linux_toolchain.runtime.llvm.ReadElfInspector",
                return_value=FakeInspector(),
            ):
                manifest_path = import_llvm_runtime(
                    prefix,
                    self.version,
                    self.floor,
                    "x86_64",
                    self.target,
                    root / "export",
                    probe_clang=clang.absolute(),
                )
            manifest = load_llvm_runtime_manifest(manifest_path)
            self.assertEqual(manifest.source["kind"], "clang-probe")
            self.assertNotIn(str(root), json.dumps(dict(manifest.source)))
            self.assertEqual(set(manifest.source), {"kind", "version", "target"})

    def test_rejects_cross_component_symlink_and_missing_linker_entrypoint(
        self,
    ) -> None:
        for kind in ("cross-component", "missing-entrypoint"):
            with (
                self.subTest(kind=kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                prefix = self.make_prefix(root)
                entrypoint = prefix / "lib/libunwind.so"
                entrypoint.unlink()
                if kind == "cross-component":
                    entrypoint.symlink_to("libc++.so.1")
                self.write_provenance(prefix)
                expected = "crosses runtime components|missing linker entry"
                with self.assertRaisesRegex(ExternalToolError, expected):
                    with patch(
                        "linux_toolchain.runtime.llvm.ReadElfInspector",
                        return_value=FakeInspector(),
                    ):
                        import_llvm_runtime(
                            prefix,
                            self.version,
                            self.floor,
                            "x86_64",
                            self.target,
                            root / "export",
                            source_evidence=self.source_evidence(),
                        )

    def test_rejects_unexported_needed_rpath_soname_and_wrong_machine(self) -> None:
        cases = (
            (FakeInspector(extra_needed="libunexpected.so.1"), "unexported runtime"),
            (FakeInspector(rpath=("/tmp",)), "RPATH"),
            (
                FakeInspector(soname_overrides={"libunwind.so.1.0": "libc++abi.so.1"}),
                "component-mismatched SONAME",
            ),
            (FakeInspector(machine="aarch64"), "expected x86_64"),
        )
        for inspector, expected in cases:
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                prefix = self.make_prefix(root)
                with (
                    patch(
                        "linux_toolchain.runtime.llvm.ReadElfInspector",
                        return_value=inspector,
                    ),
                    self.assertRaisesRegex(ExternalToolError, expected),
                ):
                    import_llvm_runtime(
                        prefix,
                        self.version,
                        self.floor,
                        "x86_64",
                        self.target,
                        root / "export",
                        source_evidence=self.source_evidence(),
                    )

    def test_rejects_incomplete_static_archive_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.make_prefix(root)
            (prefix / "lib/libc++abi.a").unlink()
            with (
                patch(
                    "linux_toolchain.runtime.llvm.ReadElfInspector",
                    return_value=FakeInspector(),
                ),
                self.assertRaisesRegex(ConfigurationError, "static LLVM runtime"),
            ):
                import_llvm_runtime(
                    prefix,
                    self.version,
                    self.floor,
                    "x86_64",
                    self.target,
                    root / "export",
                    source_evidence=self.source_evidence(),
                )

    def test_force_does_not_replace_unowned_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.make_prefix(root)
            output = root / "unowned"
            output.mkdir()
            important = output / "important"
            important.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigurationError, "unowned"):
                import_llvm_runtime(
                    prefix,
                    self.version,
                    self.floor,
                    "x86_64",
                    self.target,
                    output,
                    source_evidence=self.source_evidence(),
                    force=True,
                )

            self.assertEqual(important.read_text(encoding="utf-8"), "keep\n")

    def test_final_validation_failure_restores_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.make_prefix(root)
            output = root / "owned"
            evidence = self.source_evidence()
            with patch(
                "linux_toolchain.runtime.llvm.ReadElfInspector",
                return_value=FakeInspector(),
            ):
                import_llvm_runtime(
                    prefix,
                    self.version,
                    self.floor,
                    "x86_64",
                    self.target,
                    output,
                    source_evidence=evidence,
                )
            marker = output / "previous"
            marker.write_text("keep\n", encoding="utf-8")

            with (
                patch(
                    "linux_toolchain.runtime.llvm.ReadElfInspector",
                    return_value=FakeInspector(),
                ),
                patch(
                    "linux_toolchain.runtime.llvm.validate_llvm_runtime_manifest",
                    side_effect=ConfigurationError("synthetic final failure"),
                ) as validate,
                self.assertRaisesRegex(ConfigurationError, "synthetic final failure"),
            ):
                import_llvm_runtime(
                    prefix,
                    self.version,
                    self.floor,
                    "x86_64",
                    self.target,
                    output,
                    source_evidence=evidence,
                    force=True,
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(validate.call_args.args[0], output)

    def test_rejects_aarch64_floor_below_platform_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = self.make_prefix(root)
            with self.assertRaisesRegex(ConfigurationError, "2.17"):
                import_llvm_runtime(
                    prefix,
                    self.version,
                    "2.16",
                    "aarch64",
                    "aarch64-portable-linux-gnu",
                    root / "export",
                    source_evidence=self.source_evidence(),
                )


if __name__ == "__main__":
    unittest.main()
