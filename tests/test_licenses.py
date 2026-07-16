import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.licenses import (
    copy_license_directory,
    extract_component_licenses,
    license_evidence,
    require_license_files,
    validate_license_evidence,
)


def write_archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, mode="w") as archive:
        root = tarfile.TarInfo("source-1.0")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        directories = sorted(
            {
                parent.as_posix()
                for relative in files
                for parent in Path(relative).parents
                if parent != Path(".")
            }
        )
        for relative in directories:
            member = tarfile.TarInfo(f"source-1.0/{relative}")
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        for relative, content in files.items():
            member = tarfile.TarInfo(f"source-1.0/{relative}")
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))


class LicenseEvidenceTest(unittest.TestCase):
    def test_extracts_required_gcc_notices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "gcc.tar"
            write_archive(
                archive,
                {
                    "COPYING": b"GPL\n",
                    "COPYING.RUNTIME": b"GCC Runtime Library Exception\n",
                    "libstdc++-v3/LICENSE": b"runtime terms\n",
                    "README": b"not license evidence\n",
                },
            )
            artifact = root / "artifact"
            artifact.mkdir()

            extract_component_licenses(archive, artifact, "gcc")
            evidence = license_evidence(artifact, context="test artifact")

            self.assertEqual(
                evidence["files"],
                [
                    "licenses/gcc/COPYING",
                    "licenses/gcc/COPYING.RUNTIME",
                    "licenses/gcc/libstdc++-v3/LICENSE",
                ],
            )
            validate_license_evidence(
                artifact,
                evidence,
                context="test artifact",
            )

    def test_archive_license_links_and_traversal_are_rejected(self) -> None:
        for entry_type, name, linkname, expected in (
            (tarfile.SYMTYPE, "source-1.0/COPYING.RUNTIME", "COPYING", "regular"),
            (tarfile.LNKTYPE, "source-1.0/COPYING.RUNTIME", "COPYING", "regular"),
            (tarfile.REGTYPE, "source-1.0/../escape", "", "invalid"),
        ):
            with self.subTest(entry_type=entry_type, name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    archive_path = root / "gcc.tar"
                    with tarfile.open(archive_path, mode="w") as archive:
                        release = tarfile.TarInfo("source-1.0")
                        release.type = tarfile.DIRTYPE
                        archive.addfile(release)
                        copying = tarfile.TarInfo("source-1.0/COPYING")
                        copying.size = 4
                        archive.addfile(copying, io.BytesIO(b"GPL\n"))
                        special = tarfile.TarInfo(name)
                        special.type = entry_type
                        special.linkname = linkname
                        if entry_type == tarfile.REGTYPE:
                            special.size = 1
                            archive.addfile(special, io.BytesIO(b"x"))
                        else:
                            archive.addfile(special)
                    artifact = root / "artifact"
                    artifact.mkdir()

                    with self.assertRaisesRegex(ExternalToolError, expected):
                        extract_component_licenses(archive_path, artifact, "gcc")

    def test_required_license_files_must_be_nonempty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            licenses = root / "licenses/gcc"
            licenses.mkdir(parents=True)
            (licenses / "COPYING").write_bytes(b"")

            with self.assertRaisesRegex(
                ConfigurationError,
                "empty required license files: gcc/COPYING",
            ):
                require_license_files(
                    root,
                    ("gcc/COPYING",),
                    context="test artifact",
                )

    def test_validation_rejects_unrecorded_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            licenses = root / "licenses/gcc"
            licenses.mkdir(parents=True)
            copying = licenses / "COPYING"
            copying.write_text("terms\n", encoding="utf-8")
            evidence = license_evidence(root, context="test artifact")

            (licenses / "NOTICE").write_text("notice\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "file list"):
                validate_license_evidence(root, evidence, context="test artifact")

    def test_publication_copies_a_validated_license_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            (source / "licenses/gcc").mkdir(parents=True)
            (source / "licenses/gcc/COPYING").write_text("terms\n", encoding="utf-8")
            destination.mkdir()

            copy_license_directory(source, destination)

            self.assertEqual(
                (destination / "licenses/gcc/COPYING").read_text(encoding="utf-8"),
                "terms\n",
            )
            validate_license_evidence(
                destination,
                license_evidence(destination, context="published artifact"),
                context="published artifact",
            )


if __name__ == "__main__":
    unittest.main()
