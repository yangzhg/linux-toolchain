import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from linux_toolchain.elf import (
    AuditPolicy,
    ReadElfInspector,
    audit_metadata,
    audit_paths,
    discover_elf_files,
    load_policy,
    parse_readelf_archive_headers,
    parse_readelf_output,
)
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.process import CommandResult

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "readelf"
POLICIES = Path(__file__).resolve().parent / "fixtures" / "policies"


class ReadElfParserTest(unittest.TestCase):
    def parse_fixture(self, name: str):
        output = (FIXTURES / name).read_text(encoding="utf-8")
        return parse_readelf_output(Path("/tmp/probe"), output)

    def test_parses_gnu_version_needs_not_definitions(self) -> None:
        metadata = self.parse_fixture("gnu-x86_64.txt")
        self.assertEqual(metadata.machine, "x86_64")
        self.assertEqual(metadata.elf_class, "ELF64")
        self.assertEqual(metadata.endianness, "little")
        self.assertEqual(metadata.interpreter, "/lib64/ld-linux-x86-64.so.2")
        self.assertEqual(metadata.needed, ("libstdc++.so.6", "libc.so.6"))
        self.assertEqual(metadata.runpath, ("$ORIGIN/../lib",))
        names = {need.name for need in metadata.version_needs}
        self.assertIn("GLIBC_2.18", names)
        self.assertIn("GLIBCXX_3.4.31", names)
        self.assertNotIn("GLIBC_9.99", names)

    def test_parses_and_rejects_path_valued_soname(self) -> None:
        output = (FIXTURES / "gnu-x86_64.txt").read_text(encoding="utf-8")
        output = output.replace(
            "0x000000000000001d (RUNPATH)",
            "0x000000000000000e (SONAME)            Library soname: "
            "[/host/lib/libbad.so.1]\n 0x000000000000001d (RUNPATH)",
        )
        metadata = parse_readelf_output("/tmp/probe", output)
        self.assertEqual(metadata.soname, "/host/lib/libbad.so.1")
        policy = load_policy(POLICIES / "x86_64-glibc-2.18.json")
        self.assertIn(
            "absolute_soname",
            {item.code for item in audit_metadata(metadata, policy).violations},
        )

    def test_parses_aarch64_machine_loader_and_version_needs(self) -> None:
        metadata = self.parse_fixture("gnu-aarch64.txt")
        self.assertEqual(metadata.machine, "aarch64")
        self.assertEqual(metadata.elf_class, "ELF64")
        self.assertEqual(metadata.endianness, "little")
        self.assertEqual(metadata.interpreter, "/lib/ld-linux-aarch64.so.1")
        self.assertEqual(metadata.needed, ("libc.so.6",))
        self.assertEqual(
            {(need.library, need.name) for need in metadata.version_needs},
            {("libc.so.6", "GLIBC_2.18")},
        )
        self.assertNotIn("GLIBC_99.0", {need.name for need in metadata.version_needs})

    def test_parses_every_archive_member_header(self) -> None:
        header = (FIXTURES / "gnu-x86_64.txt").read_text(encoding="utf-8")
        output = (
            "File: runtime.a(first.o)\n"
            + header
            + "\nFile: runtime.a(second.o)\n"
            + header
        )
        members = parse_readelf_archive_headers("runtime.a", output)
        self.assertEqual(len(members), 2)
        self.assertTrue(all(member.machine == "x86_64" for member in members))
        self.assertEqual(
            [member.path.name for member in members],
            ["runtime.a(first.o)", "runtime.a(second.o)"],
        )


class ReadElfResolutionTest(unittest.TestCase):
    def test_resolves_once_per_instance_and_falls_back_at_runtime(self) -> None:
        fixture = (FIXTURES / "gnu-x86_64.txt").read_text(encoding="utf-8")

        def execute(argv, **_kwargs):
            if argv[0] == "/tools/configured-readelf":
                raise ExternalToolError("configured readelf failed")
            return CommandResult(fixture, "")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"LINUX_TOOLCHAIN_READELF": "/tools/configured-readelf"},
                clear=True,
            ),
            patch(
                "linux_toolchain.elf.reader.shutil.which",
                side_effect=lambda name: {
                    "/tools/configured-readelf": "/tools/configured-readelf",
                    "readelf": "/tools/readelf",
                }.get(name),
            ) as which,
            patch("linux_toolchain.elf.reader.run", side_effect=execute) as run,
        ):
            root = Path(directory)
            first = root / "first.so"
            second = root / "second.so"
            first.write_bytes(b"\x7fELFfirst")
            second.write_bytes(b"\x7fELFsecond")
            inspector = ReadElfInspector()
            inspector.inspect(first)
            inspector.inspect(second)

        self.assertEqual(
            which.call_args_list,
            [
                call("/tools/configured-readelf"),
                call("readelf"),
                call("llvm-readelf"),
            ],
        )
        self.assertEqual(
            [invocation.args[0][0] for invocation in run.call_args_list],
            [
                "/tools/configured-readelf",
                "/tools/readelf",
                "/tools/configured-readelf",
                "/tools/readelf",
            ],
        )


class AuditPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_policy(POLICIES / "x86_64-glibc-2.18.json")
        self.gnu_output = (FIXTURES / "gnu-x86_64.txt").read_text(encoding="utf-8")

    def test_rejects_required_glibc_above_floor(self) -> None:
        metadata = parse_readelf_output(
            "/tmp/probe",
            self.gnu_output.replace("GLIBC_2.18", "GLIBC_2.19"),
        )
        violations = audit_metadata(metadata, self.policy).violations
        self.assertIn("glibc_floor_exceeded", {item.code for item in violations})

    def test_rejects_elf32_even_when_machine_matches(self) -> None:
        metadata = parse_readelf_output(
            "/tmp/probe",
            self.gnu_output.replace(
                "Class:                             ELF64",
                "Class:                             ELF32",
            ),
        )
        self.assertEqual(metadata.machine, self.policy.machine)
        self.assertEqual(metadata.elf_class, "ELF32")
        violations = audit_metadata(metadata, self.policy).violations
        self.assertIn("elf_class_mismatch", {item.code for item in violations})

    def test_glibc_private_is_unconditionally_forbidden(self) -> None:
        metadata = parse_readelf_output(
            "/tmp/probe",
            self.gnu_output.replace("GLIBC_2.18", "GLIBC_PRIVATE"),
        )
        policy = AuditPolicy.for_glibc_floor(
            "2.18", forbidden_versions=(), allowed_interpreters=(metadata.interpreter,)
        )
        violations = audit_metadata(metadata, policy).violations
        self.assertIn("forbidden_version_required", {item.code for item in violations})

    def test_rejects_unknown_non_numeric_glibc_symbol_version(self) -> None:
        metadata = parse_readelf_output(
            "/tmp/probe",
            self.gnu_output.replace("GLIBC_2.18", "GLIBC_FUTURE_ABI"),
        )
        violations = audit_metadata(metadata, self.policy).violations
        self.assertIn(
            "unknown_glibc_version_required",
            {item.code for item in violations},
        )

    def test_glibc_abi_dt_relr_is_explicitly_allowed_at_2_36(self) -> None:
        metadata = parse_readelf_output(
            "/tmp/probe",
            self.gnu_output.replace("GLIBC_2.18", "GLIBC_ABI_DT_RELR"),
        )
        policy = AuditPolicy.for_glibc_floor(
            "2.36", allowed_interpreters=(metadata.interpreter,)
        )
        self.assertTrue(audit_metadata(metadata, policy).passed)

    def test_rejects_dt_relr_and_absolute_runpath_for_old_floor(self) -> None:
        output = self.gnu_output.replace(
            "0x000000000000001d (RUNPATH)            Library runpath: [$ORIGIN/../lib]",
            "0x0000000000000024 (RELR)               0x680\n"
            " 0x000000000000001d (RUNPATH)            Library runpath: [/host/lib]",
        )
        metadata = parse_readelf_output("/tmp/probe", output)
        codes = {item.code for item in audit_metadata(metadata, self.policy).violations}
        self.assertIn("dt_relr_unsupported", codes)
        self.assertIn("absolute_runpath", codes)

    def test_rejects_plain_relative_rpath_and_runpath(self) -> None:
        original = (
            "0x000000000000001d (RUNPATH)            Library runpath: [$ORIGIN/../lib]"
        )
        for tag in ("RPATH", "RUNPATH"):
            with self.subTest(tag=tag):
                output = self.gnu_output.replace(
                    original,
                    f"0x000000000000001d ({tag})            Library search path: [lib]",
                )
                metadata = parse_readelf_output("/tmp/probe", output)
                entries = metadata.rpath if tag == "RPATH" else metadata.runpath
                self.assertEqual(entries, ("lib",))
                violations = audit_metadata(metadata, self.policy).violations
                self.assertIn(
                    f"relative_{tag.lower()}",
                    {item.code for item in violations},
                )

    def test_origin_relative_runpath_is_allowed(self) -> None:
        metadata = parse_readelf_output("/tmp/probe", self.gnu_output)
        self.assertEqual(metadata.runpath, ("$ORIGIN/../lib",))
        codes = {item.code for item in audit_metadata(metadata, self.policy).violations}
        self.assertNotIn("relative_runpath", codes)
        self.assertTrue(audit_metadata(metadata, self.policy).passed)

    def test_rejects_dt_needed_entries_containing_slash(self) -> None:
        cases = (
            ("/opt/host/lib/libstdc++.so.6", "absolute_needed"),
            ("private/libstdc++.so.6", "relative_needed_path"),
        )
        for needed, expected_code in cases:
            with self.subTest(needed=needed):
                output = self.gnu_output.replace(
                    "Shared library: [libstdc++.so.6]",
                    f"Shared library: [{needed}]",
                )
                metadata = parse_readelf_output("/tmp/probe", output)
                self.assertIn(needed, metadata.needed)
                violations = audit_metadata(metadata, self.policy).violations
                self.assertIn(
                    expected_code,
                    {item.code for item in violations},
                )

    def test_policy_rejects_unknown_schema_or_format(self) -> None:
        cases = (
            ("schema", {"schema": "another-policy"}),
            ("format", {"format": 2}),
            ("format", {"format": True}),
        )
        for message, replacement in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "policy.json"
                value = self.policy.to_dict()
                value.update(replacement)
                path.write_text(json.dumps(value), encoding="utf-8")

                with self.assertRaisesRegex(ConfigurationError, message):
                    load_policy(path)


class ElfDiscoveryTest(unittest.TestCase):
    def test_discovers_by_magic_not_suffix_or_executable_bit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elf = root / "shared-object-without-suffix"
            elf.write_bytes(b"\x7fELFpayload")
            elf.chmod(0o600)
            (root / "looks-like.so").write_text("not ELF", encoding="utf-8")
            self.assertEqual(discover_elf_files(root), (elf.resolve(),))

    def test_audit_rejects_empty_input_instead_of_vacuous_pass(self) -> None:
        policy = load_policy(POLICIES / "x86_64-glibc-2.18.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README").write_text("no ELF here", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "no auditable final ELF"):
                audit_paths(root, policy)

    def test_audit_skips_relocatable_objects(self) -> None:
        policy = load_policy(POLICIES / "x86_64-glibc-2.18.json")
        fixture = (FIXTURES / "gnu-x86_64.txt").read_text(encoding="utf-8")
        metadata = parse_readelf_output(
            "/tmp/object.o",
            fixture.replace(
                "DYN (Position-Independent Executable file)",
                "REL (Relocatable file)",
            ),
        )

        class Inspector:
            def inspect(self, _path: Path):
                return metadata

        with tempfile.TemporaryDirectory() as directory:
            object_file = Path(directory) / "object.o"
            object_file.write_bytes(b"\x7fELFpayload")
            with self.assertRaisesRegex(
                ConfigurationError, "ET_REL objects are skipped"
            ):
                audit_paths(object_file, policy, inspector=Inspector())  # type: ignore[arg-type]

    def test_recursive_audit_rejects_directory_symlinks(self) -> None:
        policy = load_policy(POLICIES / "x86_64-glibc-2.18.json")
        fixture = (FIXTURES / "gnu-x86_64.txt").read_text(encoding="utf-8")
        metadata = parse_readelf_output("/tmp/local-elf", fixture)

        class Inspector:
            def inspect(self, _path: Path):
                return metadata

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "audit-root"
            root.mkdir()
            (root / "local-elf").write_bytes(b"\x7fELFpayload")
            linked_directory = base / "linked-directory"
            linked_directory.mkdir()
            (linked_directory / "hidden-elf").write_bytes(b"\x7fELFpayload")
            (root / "directory-link").symlink_to(
                linked_directory, target_is_directory=True
            )

            with self.assertRaisesRegex(ExternalToolError, "symbolic link|symlink"):
                audit_paths(root, policy, inspector=Inspector())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
