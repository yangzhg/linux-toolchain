import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_toolchain.elf.models import ElfMetadata, VersionNeed
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.licenses import license_evidence
from linux_toolchain.process import CommandResult
from linux_toolchain.runtime import (
    GccRuntimeManifest,
    import_gcc_runtime,
    load_runtime_manifest,
    validate_runtime_manifest,
)
from linux_toolchain.runtime.importer import (
    _copy_file,
    _GccInstallation,
)


class FakeInspector:
    def __init__(
        self,
        *,
        arch: str = "x86_64",
        glibc: str = "2.17",
        glibc_name: str | None = None,
        private: bool = False,
        crt_arch: str | None = None,
        archive_arch: str | None = None,
        has_dt_relr: bool = False,
        abi_dt_relr: bool = False,
        runpath: tuple[str, ...] = (),
        soname: str | None = "auto",
    ) -> None:
        self.arch = arch
        self.glibc = glibc
        self.glibc_name = glibc_name
        self.private = private
        self.crt_arch = crt_arch
        self.archive_arch = archive_arch
        self.has_dt_relr = has_dt_relr
        self.abi_dt_relr = abi_dt_relr
        self.runpath = runpath
        self.soname = soname
        self.inspected: list[Path] = []

    def inspect(self, path: Path) -> ElfMetadata:
        self.inspected.append(path)
        needs = [
            VersionNeed(
                library="libc.so.6",
                name=self.glibc_name or f"GLIBC_{self.glibc}",
            ),
            VersionNeed(library="libgcc_s.so.1", name="GCC_3.0"),
        ]
        if self.private:
            needs.append(VersionNeed(library="libc.so.6", name="GLIBC_PRIVATE"))
        if self.abi_dt_relr:
            needs.append(VersionNeed(library="libc.so.6", name="GLIBC_ABI_DT_RELR"))
        relocatable = path.name.endswith(".o")
        if self.soname == "auto":
            if path.name.startswith("libstdc++.so"):
                soname = "libstdc++.so.6"
            elif path.name.startswith("libgcc_s.so"):
                soname = "libgcc_s.so.1"
            elif path.name.startswith("libatomic.so"):
                soname = "libatomic.so.1"
            elif path.name.startswith("libquadmath.so"):
                soname = "libquadmath.so.0"
            else:
                soname = None
        else:
            soname = self.soname
        return ElfMetadata(
            path=path,
            elf_class="ELF64",
            endianness="little",
            elf_type="REL" if relocatable else "DYN",
            machine=(self.crt_arch or self.arch) if relocatable else self.arch,
            interpreter=None,
            needed=("libc.so.6",),
            rpath=(),
            runpath=self.runpath,
            has_dt_relr=self.has_dt_relr,
            version_needs=tuple(needs),
            soname=soname,
        )

    def inspect_archive(self, path: Path) -> tuple[ElfMetadata, ...]:
        self.inspected.append(path)
        return (
            ElfMetadata(
                path=Path(f"{path}(member.o)"),
                elf_class="ELF64",
                endianness="little",
                elf_type="REL",
                machine=self.archive_arch or self.arch,
                interpreter=None,
                needed=(),
                rpath=(),
                runpath=(),
                has_dt_relr=False,
                version_needs=(),
            ),
        )


class RuntimeImportTest(unittest.TestCase):
    def make_prefix(
        self,
        root: Path,
        *,
        arch: str = "x86_64",
        target: str | None = None,
        version: str = "13.2.1",
        version_directory: str = "13",
        include_libatomic: bool = True,
    ) -> tuple[Path, _GccInstallation]:
        target = target or f"{arch}-linux-gnu"
        prefix = root / "gcc"
        (prefix / "bin").mkdir(parents=True)
        compiler = prefix / "bin" / "g++"
        compiler.write_text("compiler must not be copied\n", encoding="utf-8")
        compiler.chmod(0o755)

        cxx = prefix / "include" / "c++" / version_directory
        (cxx / target / "bits").mkdir(parents=True)
        (cxx / "vector").write_text("// vector\n", encoding="utf-8")
        (cxx / target / "bits" / "c++config.h").write_text(
            "// target config\n", encoding="utf-8"
        )

        gcc_runtime = prefix / "lib" / "gcc" / target / version_directory
        (gcc_runtime / "include").mkdir(parents=True)
        (gcc_runtime / "include-fixed").mkdir()
        (gcc_runtime / "include" / "stddef.h").write_text(
            "// compiler header\n", encoding="utf-8"
        )
        (gcc_runtime / "include" / "stdarg.h").write_text(
            "// compiler header\n", encoding="utf-8"
        )
        (gcc_runtime / "include-fixed" / "limits.h").write_text(
            "// fixed header\n", encoding="utf-8"
        )
        (prefix / "include").mkdir(exist_ok=True)
        (prefix / "include" / "quadmath.h").write_text(
            "// GCC quadmath API\n", encoding="utf-8"
        )
        (prefix / "include" / "quadmath_weak.h").write_text(
            "// GCC weak quadmath API\n", encoding="utf-8"
        )
        for name in ("crtbegin.o", "crtbeginS.o", "crtend.o", "crtendS.o"):
            (gcc_runtime / name).write_bytes(b"object")
        (gcc_runtime / "libgcc.a").write_bytes(b"!<arch>\n")
        (gcc_runtime / "libgcc_eh.a").write_bytes(b"!<arch>\n")
        (gcc_runtime / "cc1").write_text("compiler\n", encoding="utf-8")
        (gcc_runtime / "plugin").mkdir()
        (gcc_runtime / "plugin" / "lto1").write_text(
            "compiler plugin\n", encoding="utf-8"
        )

        library = prefix / "lib"
        (library / "libstdc++.a").write_bytes(b"!<arch>\n")
        (library / "libstdc++.so.6.0.32").write_bytes(b"\x7fELFstdc++")
        (library / "libstdc++.so.6.0.99").write_bytes(b"\x7fELForphan")
        (library / "libstdc++.so.6").symlink_to("libstdc++.so.6.0.32")
        (library / "libstdc++.so").symlink_to("libstdc++.so.6")
        (library / "libgcc_s.so.1").write_bytes(b"\x7fELFlibgcc")
        (library / "libgcc_s.so").symlink_to("libgcc_s.so.1")
        if include_libatomic:
            (library / "libatomic.a").write_bytes(b"!<arch>\n")
            (library / "libatomic.so.1.2.0").write_bytes(b"\x7fELFatomic")
            (library / "libatomic.so.1").symlink_to("libatomic.so.1.2.0")
            (library / "libatomic.so").symlink_to("libatomic.so.1")
        (library / "libquadmath.a").write_bytes(b"!<arch>\n")
        (library / "libquadmath.so.0.0.0").write_bytes(b"\x7fELFquadmath")
        (library / "libquadmath.so.0").symlink_to("libquadmath.so.0.0.0")
        (library / "libquadmath.so").symlink_to("libquadmath.so.0")
        (library / "libgomp.so.1").write_bytes(b"\x7fELFgomp")
        (library / "libgomp.so").symlink_to("libgomp.so.1")
        (library / "libasan.so.8").write_bytes(b"\x7fELFasan")
        (library / "libasan.so").symlink_to("libasan.so.8")
        (library / "libstdc++.so.6.0.32-gdb.py").write_text(
            "raise RuntimeError('must not be imported')\n", encoding="utf-8"
        )
        for name in ("libstdc++.a", "libstdc++.so", "libgcc_s.so"):
            (gcc_runtime / name).symlink_to(
                os.path.relpath(library / name, gcc_runtime)
            )
        licenses = prefix / "licenses/gcc"
        licenses.mkdir(parents=True)
        (licenses / "COPYING").write_text("GPL\n", encoding="utf-8")
        (licenses / "COPYING.RUNTIME").write_text(
            "GCC Runtime Library Exception\n", encoding="utf-8"
        )

        return prefix, _GccInstallation(
            version=version,
            major=int(version.split(".", 1)[0]),
            target=target,
            gcc_runtime_dir=gcc_runtime,
        )

    @staticmethod
    def add_gcc16_asneeded_inputs(
        prefix: Path,
    ) -> dict[str, Path]:
        inputs = {
            "libgcc_s_asneeded.so": prefix / "lib" / "libgcc_s_asneeded.so",
            "libatomic_asneeded.so": prefix / "lib" / "libatomic_asneeded.so",
            "libatomic_asneeded.a": prefix / "lib" / "libatomic_asneeded.a",
        }
        inputs["libgcc_s_asneeded.so"].write_text(
            "/* GNU ld script\n"
            "   Add DT_NEEDED entry for libgcc_s.so only if needed.  */\n"
            "INPUT ( AS_NEEDED ( -lgcc_s ) )\n",
            encoding="ascii",
        )
        inputs["libatomic_asneeded.so"].write_text(
            "/* GNU ld script\n"
            "   Add DT_NEEDED entry for -latomic only if needed.  */\n"
            "INPUT ( AS_NEEDED ( -latomic ) )\n",
            encoding="ascii",
        )
        inputs["libatomic_asneeded.a"].symlink_to("libatomic.a")
        return inputs

    def import_fixture(
        self,
        root: Path,
        *,
        inspector: FakeInspector | None = None,
    ) -> tuple[Path, GccRuntimeManifest, FakeInspector]:
        prefix, installation = self.make_prefix(root)
        output = root / "imported"
        fake = inspector or FakeInspector()
        with (
            patch(
                "linux_toolchain.runtime.importer._probe_gcc",
                return_value=installation,
            ),
            patch(
                "linux_toolchain.runtime.importer.ReadElfInspector",
                return_value=fake,
            ),
        ):
            manifest_path = import_gcc_runtime(prefix, "2.18", "x86_64", output)
        return output, load_runtime_manifest(manifest_path), fake

    def test_managed_import_uses_exact_metadata_without_executing_a_driver(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, _ = self.make_prefix(
                root,
                version="13.4.0",
                version_directory="13.4.0",
            )
            output = root / "managed-runtime"
            fake = FakeInspector()
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    side_effect=AssertionError("managed import must not execute g++"),
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=fake,
                ),
            ):
                manifest_path = import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    output,
                    provider_version="13.4.0",
                    target="x86_64-linux-gnu",
                )

            manifest = load_runtime_manifest(manifest_path)
            self.assertEqual(manifest.provider["version"], "13.4.0")
            self.assertEqual(manifest.target, "x86_64-linux-gnu")

    def test_managed_import_requires_complete_quadmath_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, _ = self.make_prefix(
                root,
                version="13.4.0",
                version_directory="13.4.0",
            )
            for header in ("quadmath.h", "quadmath_weak.h"):
                (prefix / "include" / header).unlink()
            for library in (prefix / "lib").glob("libquadmath*"):
                library.unlink()
            with self.assertRaisesRegex(
                ConfigurationError,
                "quadmath.h.*libquadmath.a.*libquadmath.so",
            ):
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    root / "managed-runtime",
                    provider_version="13.4.0",
                    target="x86_64-linux-gnu",
                )

    def test_managed_aarch64_import_does_not_require_quadmath(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, _ = self.make_prefix(
                root,
                arch="aarch64",
                version="13.4.0",
                version_directory="13.4.0",
            )
            for header in ("quadmath.h", "quadmath_weak.h"):
                (prefix / "include" / header).unlink()
            for library in (prefix / "lib").glob("libquadmath*"):
                library.unlink()
            output = root / "managed-runtime"

            with patch(
                "linux_toolchain.runtime.importer.ReadElfInspector",
                return_value=FakeInspector(arch="aarch64"),
            ):
                manifest_path = import_gcc_runtime(
                    prefix,
                    "2.17",
                    "aarch64",
                    output,
                    provider_version="13.4.0",
                    target="aarch64-linux-gnu",
                )

            manifest = load_runtime_manifest(manifest_path)
            self.assertEqual(manifest.arch, "aarch64")
            self.assertFalse(
                any(
                    "quadmath" in path
                    for path in manifest.locations["static_libraries"]
                )
            )
            self.assertFalse(any(output.rglob("*quadmath*")))

    def test_managed_import_requires_exact_version_and_complete_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, _ = self.make_prefix(
                root,
                version="13.4.0",
                version_directory="13.4.0",
            )
            with self.assertRaisesRegex(ConfigurationError, "together"):
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    root / "incomplete",
                    provider_version="13.4.0",
                )
            with self.assertRaisesRegex(ConfigurationError, "13.3.0"):
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    root / "wrong-version",
                    provider_version="13.3.0",
                    target="x86_64-linux-gnu",
                )

    def test_imports_only_filtered_runtime_and_records_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, manifest, inspector = self.import_fixture(root)

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((output / "runtime").stat().st_mode), 0o755)
            self.assertEqual(
                stat.S_IMODE((output / "manifest.json").stat().st_mode), 0o644
            )
            self.assertEqual(manifest.provider["name"], "gcc")
            self.assertEqual(manifest.provider["version"], "13.2.1")
            self.assertEqual(manifest.provider["major"], 13)
            self.assertEqual(manifest.arch, "x86_64")
            self.assertEqual(manifest.target, "x86_64-linux-gnu")
            self.assertEqual(manifest.glibc_floor, "2.18")
            self.assertEqual(manifest.locations["runtime"], "runtime")
            self.assertTrue(manifest.locations["cxx_include_dirs"])
            self.assertTrue(manifest.locations["crt_objects"])
            self.assertIn(
                "runtime/lib/gcc/x86_64-linux-gnu/13/libgcc.a",
                manifest.locations["static_libraries"],
            )
            self.assertTrue(manifest.version_symbol_reports)
            for report in manifest.version_symbol_reports:
                self.assertLessEqual(report["max_required_versions"]["GLIBC"], "2.18")

            runtime = output / "runtime"
            self.assertTrue((runtime / "include" / "c++" / "13" / "vector").is_file())
            self.assertTrue(
                (
                    runtime
                    / "lib"
                    / "gcc"
                    / "x86_64-linux-gnu"
                    / "13"
                    / "include-fixed"
                    / "limits.h"
                ).is_file()
            )
            self.assertFalse((runtime / "bin").exists())
            self.assertFalse((runtime / "lib" / "libstdc++.so.6.0.32-gdb.py").exists())
            self.assertFalse((runtime / "lib" / "libstdc++.so.6.0.99").exists())
            self.assertFalse(
                runtime.joinpath("lib", "gcc", "x86_64-linux-gnu", "13", "cc1").exists()
            )
            self.assertTrue(
                runtime.joinpath(
                    "lib", "gcc", "x86_64-linux-gnu", "13", "include", "quadmath.h"
                ).is_file()
            )
            self.assertTrue((runtime / "lib" / "libquadmath.so.0.0.0").is_file())
            self.assertTrue((runtime / "lib" / "libatomic.so.1.2.0").is_file())
            self.assertFalse((runtime / "lib" / "libgomp.so").exists())
            self.assertFalse((runtime / "lib" / "libasan.so").exists())
            self.assertIn(
                "runtime/lib/libquadmath.a",
                manifest.locations["static_libraries"],
            )
            self.assertIn(
                "runtime/lib/libatomic.a",
                manifest.locations["static_libraries"],
            )
            self.assertTrue(
                any(
                    report["path"] == "runtime/lib/libatomic.so.1.2.0"
                    for report in manifest.version_symbol_reports
                )
            )
            self.assertFalse(
                runtime.joinpath(
                    "lib", "gcc", "x86_64-linux-gnu", "13", "plugin"
                ).exists()
            )
            for path in runtime.rglob("*"):
                if path.is_symlink():
                    self.assertFalse(Path(os.readlink(path)).is_absolute())
                    path.resolve(strict=True).relative_to(runtime.resolve())
            self.assertGreaterEqual(len(inspector.inspected), 4)
            self.assertEqual(
                validate_runtime_manifest(output, inspector=FakeInspector()),
                manifest,
            )
            self.assertTrue((output / "license-manifest.json").is_file())
            (output / "licenses/gcc/COPYING.RUNTIME").unlink()
            evidence = license_evidence(output, context="test GCC runtime")
            (output / "license-manifest.json").write_text(
                json.dumps(evidence), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ConfigurationError,
                "missing required license files.*COPYING.RUNTIME",
            ):
                validate_runtime_manifest(output, inspector=FakeInspector())

    def test_copies_each_canonical_library_once_and_preserves_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            output = root / "imported"
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=FakeInspector(),
                ),
                patch(
                    "linux_toolchain.runtime.importer._copy_file",
                    wraps=_copy_file,
                ) as copy_file,
            ):
                import_gcc_runtime(prefix, "2.18", "x86_64", output)

            canonical = prefix / "lib/libstdc++.so.6.0.32"
            self.assertEqual(
                sum(call.args[0] == canonical for call in copy_file.call_args_list),
                1,
            )
            self.assertTrue((output / "runtime/lib/libstdc++.so").is_symlink())
            self.assertTrue((output / "runtime/lib/libstdc++.so.6").is_symlink())
            self.assertTrue(
                (
                    output / "runtime/lib/gcc/x86_64-linux-gnu/13/libstdc++.so"
                ).is_symlink()
            )

    def test_external_probe_must_match_runtime_target_and_major(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, _ = self.make_prefix(root)
            probe = root / "xg++"
            probe.write_text("temporary build driver\n", encoding="utf-8")
            probe.chmod(0o755)

            def probe_result(argv, **_kwargs):
                if "-dumpmachine" in argv:
                    return CommandResult("x86_64-linux-gnu\n", "")
                return CommandResult("14.1.0\n", "")

            with patch(
                "linux_toolchain.runtime.importer.run",
                side_effect=probe_result,
            ):
                with self.assertRaisesRegex(
                    ConfigurationError, "cannot uniquely match GCC 14.1.0"
                ):
                    import_gcc_runtime(
                        prefix,
                        "2.18",
                        "x86_64",
                        root / "imported",
                        probe_gxx=probe,
                    )

    def test_rejects_shared_runtime_above_glibc_floor_and_cleans_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            output = root / "imported"
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=FakeInspector(glibc="2.19"),
                ),
            ):
                with self.assertRaisesRegex(ExternalToolError, "GLIBC_2.19"):
                    import_gcc_runtime(prefix, "2.18", "x86_64", output)
            self.assertFalse(output.exists())
            self.assertFalse(tuple(root.glob(".imported.tmp-*")))

    def test_rejects_glibc_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=FakeInspector(private=True),
                ),
            ):
                with self.assertRaisesRegex(ExternalToolError, "GLIBC_PRIVATE"):
                    import_gcc_runtime(prefix, "2.18", "x86_64", root / "imported")

    def test_rejects_wrong_shared_library_machine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=FakeInspector(arch="aarch64"),
                ),
            ):
                with self.assertRaisesRegex(ExternalToolError, "expected x86_64"):
                    import_gcc_runtime(prefix, "2.18", "x86_64", root / "imported")

    def test_rejects_missing_or_path_valued_runtime_soname(self) -> None:
        for expected, soname in (
            ("missing", None),
            ("path-valued", "/host/lib/libstdc++.so.6"),
        ):
            with (
                self.subTest(soname=soname),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                prefix, installation = self.make_prefix(root)
                with (
                    patch(
                        "linux_toolchain.runtime.importer._probe_gcc",
                        return_value=installation,
                    ),
                    patch(
                        "linux_toolchain.runtime.importer.ReadElfInspector",
                        return_value=FakeInspector(soname=soname),
                    ),
                ):
                    with self.assertRaisesRegex(ExternalToolError, expected):
                        import_gcc_runtime(prefix, "2.18", "x86_64", root / "imported")

    def test_rejects_dt_relr_and_nonrelocatable_runpath_in_old_runtime(self) -> None:
        for keyword, fake in (
            ("DT_RELR", FakeInspector(has_dt_relr=True)),
            ("RUNPATH", FakeInspector(runpath=("/host/lib",))),
            ("GLIBC_ABI_DT_RELR", FakeInspector(abi_dt_relr=True)),
        ):
            with self.subTest(kind=keyword), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prefix, installation = self.make_prefix(root)
                with (
                    patch(
                        "linux_toolchain.runtime.importer._probe_gcc",
                        return_value=installation,
                    ),
                    patch(
                        "linux_toolchain.runtime.importer.ReadElfInspector",
                        return_value=fake,
                    ),
                ):
                    with self.assertRaisesRegex(ExternalToolError, keyword):
                        import_gcc_runtime(prefix, "2.18", "x86_64", root / "imported")

    def test_rejects_library_symlink_escaping_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            escaped = root / "libstdc++.so.6.0.32"
            escaped.write_bytes(b"\x7fELFoutside")
            link = prefix / "lib" / "libstdc++.so.6"
            link.unlink()
            link.symlink_to(escaped)
            with patch(
                "linux_toolchain.runtime.importer._probe_gcc",
                return_value=installation,
            ):
                with self.assertRaisesRegex(ExternalToolError, "escapes its prefix"):
                    import_gcc_runtime(prefix, "2.18", "x86_64", root / "imported")

    def test_accepts_only_the_self_contained_libgcc_linker_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            script = prefix / "lib" / "libgcc_s.so"
            script.unlink()
            script.write_text(
                "/* GNU ld script */\nGROUP ( libgcc_s.so.1 -lgcc )\n",
                encoding="ascii",
            )
            output = root / "imported"
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=FakeInspector(),
                ),
            ):
                import_gcc_runtime(prefix, "2.18", "x86_64", output)
            self.assertEqual(
                (output / "runtime" / "lib" / "libgcc_s.so").read_text(
                    encoding="ascii"
                ),
                "/* GNU ld script */\nGROUP ( libgcc_s.so.1 -lgcc )\n",
            )

    def test_managed_gcc16_requires_and_preserves_asneeded_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(
                root,
                version="16.1.0",
                version_directory="16.1.0",
            )
            output = root / "imported"
            inspector = FakeInspector()
            with patch(
                "linux_toolchain.runtime.importer.ReadElfInspector",
                return_value=inspector,
            ):
                with self.assertRaisesRegex(
                    ConfigurationError,
                    r"missing required linker inputs: .*asneeded",
                ):
                    import_gcc_runtime(
                        prefix,
                        "2.18",
                        "x86_64",
                        output,
                        provider_version=installation.version,
                        target=installation.target,
                    )

                inputs = self.add_gcc16_asneeded_inputs(prefix)
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    output,
                    provider_version=installation.version,
                    target=installation.target,
                )

            manifest = load_runtime_manifest(output)
            expected: dict[str, Path] = {}
            for name, paths in (
                ("libgcc_s_asneeded.so", manifest.locations["shared_libraries"]),
                ("libatomic_asneeded.so", manifest.locations["shared_libraries"]),
                ("libatomic_asneeded.a", manifest.locations["static_libraries"]),
            ):
                matches = tuple(Path(path) for path in paths if Path(path).name == name)
                self.assertEqual(len(matches), 1)
                expected[name] = matches[0]
            for name in ("libgcc_s_asneeded.so", "libatomic_asneeded.so"):
                relative = expected[name]
                self.assertEqual(
                    (output / relative).read_text(encoding="ascii"),
                    inputs[name].read_text(encoding="ascii"),
                )
            static_alias = output / expected["libatomic_asneeded.a"]
            self.assertTrue(static_alias.is_symlink())
            self.assertEqual(os.readlink(static_alias), "libatomic.a")
            self.assertEqual(
                static_alias.resolve(strict=True),
                (static_alias.parent / "libatomic.a").resolve(strict=True),
            )
            self.assertFalse(
                any("asneeded" in path.name for path in inspector.inspected)
            )
            self.assertEqual(
                validate_runtime_manifest(output, inspector=FakeInspector()),
                manifest,
            )

    def test_rejects_modified_gcc16_asneeded_linker_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(
                root,
                version="16.1.0",
                version_directory="16.1.0",
            )
            inputs = self.add_gcc16_asneeded_inputs(prefix)
            inputs["libatomic_asneeded.so"].write_text(
                "INPUT ( /host/lib/libatomic.so )\n",
                encoding="ascii",
            )
            with (
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=FakeInspector(),
                ),
                self.assertRaisesRegex(ExternalToolError, "unsupported linker script"),
            ):
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    root / "imported",
                    provider_version=installation.version,
                    target=installation.target,
                )

    def test_rejects_thin_static_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            (prefix / "lib" / "libstdc++.a").write_bytes(b"!<thin>\n")
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=FakeInspector(),
                ),
            ):
                with self.assertRaisesRegex(ExternalToolError, "not thin"):
                    import_gcc_runtime(prefix, "2.18", "x86_64", root / "imported")

    def test_validate_detects_invalid_archive_and_symbol_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, manifest, _ = self.import_fixture(root)
            libgcc = (
                output / "runtime" / "lib" / "gcc" / manifest.target / "13" / "libgcc.a"
            )
            libgcc.write_bytes(b"not an archive")
            with self.assertRaisesRegex(ExternalToolError, "regular ar archive"):
                validate_runtime_manifest(output, inspector=FakeInspector())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, _, _ = self.import_fixture(root)
            manifest_path = output / "manifest.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["version_symbol_reports"][0]["required_versions"]["GLIBC"] = []
            data["version_symbol_reports"][0]["max_required_versions"]["GLIBC"] = None
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "symbol report"):
                validate_runtime_manifest(output, inspector=FakeInspector())

    def test_validate_rejects_runtime_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, _, _ = self.import_fixture(root)
            link = next((output / "runtime").rglob("libstdc++.so"))
            link.unlink()
            link.symlink_to("../../../../outside")
            with self.assertRaisesRegex(ExternalToolError, "escapes.*dangling"):
                validate_runtime_manifest(output, inspector=FakeInspector())

    def test_validate_rejects_ambiguous_manifest_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, _, _ = self.import_fixture(root)
            alternate = output / "alternate.json"
            alternate.write_text(
                (output / "manifest.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "named manifest.json"):
                validate_runtime_manifest(alternate, inspector=FakeInspector())

    def test_force_only_replaces_owned_runtime_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            unowned = root / "unowned"
            unowned.mkdir()
            (unowned / "important").write_text("keep\n", encoding="utf-8")
            with patch(
                "linux_toolchain.runtime.importer._probe_gcc",
                return_value=installation,
            ):
                with self.assertRaisesRegex(ConfigurationError, "unowned"):
                    import_gcc_runtime(
                        prefix,
                        "2.18",
                        "x86_64",
                        unowned,
                        force=True,
                    )
            self.assertTrue((unowned / "important").is_file())

            output = root / "owned"
            fake = FakeInspector()
            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=fake,
                ),
            ):
                import_gcc_runtime(prefix, "2.18", "x86_64", output)
                (output / "old-generated-file").write_text("old\n", encoding="utf-8")
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    output,
                    force=True,
                )
            self.assertFalse((output / "old-generated-file").exists())
            self.assertTrue((output / "manifest.json").is_file())

    def test_final_validation_failure_restores_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, installation = self.make_prefix(root)
            output = root / "owned"
            fake = FakeInspector()
            common = (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=fake,
                ),
            )
            with common[0], common[1]:
                import_gcc_runtime(prefix, "2.18", "x86_64", output)
            marker = output / "previous"
            marker.write_text("keep\n", encoding="utf-8")

            with (
                patch(
                    "linux_toolchain.runtime.importer._probe_gcc",
                    return_value=installation,
                ),
                patch(
                    "linux_toolchain.runtime.importer.ReadElfInspector",
                    return_value=fake,
                ),
                patch(
                    "linux_toolchain.runtime.importer.validate_runtime_manifest",
                    side_effect=ConfigurationError("synthetic final failure"),
                ) as validate,
                self.assertRaisesRegex(ConfigurationError, "synthetic final failure"),
            ):
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    output,
                    force=True,
                )

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(validate.call_args.args[0], output)

    def test_rejects_output_nested_with_source_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix, _ = self.make_prefix(root)
            with self.assertRaisesRegex(ConfigurationError, "inside.*prefix"):
                import_gcc_runtime(
                    prefix,
                    "2.18",
                    "x86_64",
                    prefix / "generated-runtime",
                )

    def test_manifest_model_rejects_absolute_payload_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, manifest, _ = self.import_fixture(root)
            data = manifest.to_dict()
            data["locations"]["runtime"] = "/runtime"
            with self.assertRaisesRegex(ConfigurationError, "relative path"):
                GccRuntimeManifest.from_dict(data)


if __name__ == "__main__":
    unittest.main()
