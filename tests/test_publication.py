import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.publication import replace_directory


class DirectoryPublicationTest(unittest.TestCase):
    def test_normalizes_public_modes_under_restrictive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / ".artifact.tmp"
            destination = root / "artifact"
            previous_umask = os.umask(0o077)
            try:
                nested = staging / "nested"
                nested.mkdir(parents=True)
                data = nested / "data.json"
                data.write_text("{}\n", encoding="utf-8")
                executable = nested / "tool"
                executable.write_text("#!/bin/sh\n", encoding="utf-8")
                # Any execute bit makes a generated regular file executable.
                executable.chmod(0o610)

                replace_directory(staging, destination)
            finally:
                os.umask(previous_umask)

            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o755)
            self.assertEqual(
                stat.S_IMODE((destination / "nested").stat().st_mode), 0o755
            )
            self.assertEqual(
                stat.S_IMODE((destination / "nested" / "data.json").stat().st_mode),
                0o644,
            )
            self.assertEqual(
                stat.S_IMODE((destination / "nested" / "tool").stat().st_mode),
                0o755,
            )

    def test_normalization_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            outside_file = outside / "private"
            outside_file.write_text("private\n", encoding="utf-8")
            outside.chmod(0o700)
            outside_file.chmod(0o600)

            staging = root / ".artifact.tmp"
            destination = root / "artifact"
            staging.mkdir()
            (staging / "file-link").symlink_to(outside_file)
            (staging / "directory-link").symlink_to(outside, target_is_directory=True)

            replace_directory(staging, destination)

            self.assertEqual(os.readlink(destination / "file-link"), str(outside_file))
            self.assertEqual(os.readlink(destination / "directory-link"), str(outside))
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(outside_file.stat().st_mode), 0o600)

    def test_replaces_existing_directory_and_removes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / ".artifact.tmp"
            destination = root / "artifact"
            staging.mkdir()
            destination.mkdir()
            (staging / "payload").write_text("new\n", encoding="utf-8")
            (destination / "payload").write_text("old\n", encoding="utf-8")

            replace_directory(staging, destination)

            self.assertEqual(
                (destination / "payload").read_text(encoding="utf-8"), "new\n"
            )
            self.assertEqual(list(root.glob(".artifact.backup-*")), [])

    def test_validation_observes_final_path_and_rolls_back_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / ".artifact.tmp"
            destination = root / "artifact"
            staging.mkdir()
            destination.mkdir()
            (staging / "payload").write_text("new\n", encoding="utf-8")
            (destination / "payload").write_text("old\n", encoding="utf-8")
            observed: list[Path] = []

            def reject_published_tree(published: Path) -> None:
                observed.append(published)
                self.assertEqual(
                    (published / "payload").read_text(encoding="utf-8"),
                    "new\n",
                )
                raise ConfigurationError("injected final validation failure")

            with self.assertRaisesRegex(
                ConfigurationError, "injected final validation failure"
            ):
                replace_directory(
                    staging,
                    destination,
                    validate=reject_published_tree,
                )

            self.assertEqual(observed, [destination])
            self.assertEqual(
                (destination / "payload").read_text(encoding="utf-8"), "old\n"
            )
            self.assertEqual((staging / "payload").read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(root.glob(".artifact.backup-*")), [])

    def test_restores_existing_directory_when_publication_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / ".artifact.tmp"
            destination = root / "artifact"
            staging.mkdir()
            destination.mkdir()
            (staging / "payload").write_text("new\n", encoding="utf-8")
            (destination / "payload").write_text("old\n", encoding="utf-8")

            real_replace = os.replace
            calls = 0

            def fail_staging_publish(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publication failure")
                real_replace(source, target)

            with (
                patch(
                    "linux_toolchain.publication.os.replace",
                    side_effect=fail_staging_publish,
                ),
                self.assertRaisesRegex(ConfigurationError, "cannot publish directory"),
            ):
                replace_directory(staging, destination)

            self.assertEqual(
                (destination / "payload").read_text(encoding="utf-8"), "old\n"
            )


if __name__ == "__main__":
    unittest.main()
