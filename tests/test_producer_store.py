from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.producer_store import ProducerStore
from linux_toolchain.recipes import get_recipe


def _hold_reader(root: Path, acquired: object, release: object) -> None:
    store = ProducerStore.load(root)
    with store.lock("sdk", {"identity": "shared"}, shared=True):
        acquired.set()
        release.wait()


def _wait_for_writer(root: Path, attempting: object, acquired: object) -> None:
    store = ProducerStore.load(root)
    attempting.set()
    with store.lock("sdk", {"identity": "shared"}):
        acquired.set()


class ProducerStoreTest(unittest.TestCase):
    def test_assigns_stable_content_addressed_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            store = ProducerStore.prepare(root)
            target = get_recipe("x86_64", "2.24").to_spec()
            backend = get_recipe("x86_64", "2.19").to_spec()

            sdk_workspace = store.sdk_workspace(target)
            managed_workspace = store.managed_workspace(
                target,
                backend,
            )
            loaded = ProducerStore.load(root)

            self.assertEqual(sdk_workspace.parent, root / "sdk")
            self.assertEqual(managed_workspace.parent, root / "managed")
            self.assertEqual(loaded.sdk_workspace(target), sdk_workspace)
            self.assertEqual(
                loaded.managed_workspace(target, backend),
                managed_workspace,
            )
            self.assertEqual(store.source_cache, root / "sources")
            self.assertEqual(store.sdk_source_cache, root / "sdk-sources")

    def test_rejects_an_unowned_nonempty_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            root.mkdir()
            user_file = root / "keep.txt"
            user_file.write_text("user data\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigurationError, "unowned producer store"):
                ProducerStore.prepare(root)

            self.assertEqual(user_file.read_text(encoding="utf-8"), "user data\n")

    def test_writer_waits_for_the_matching_reader_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            ProducerStore.prepare(root)
            processes = multiprocessing.get_context("fork")
            reader_acquired = processes.Event()
            release_reader = processes.Event()
            writer_attempting = processes.Event()
            writer_acquired = processes.Event()
            reader = processes.Process(
                target=_hold_reader,
                args=(root, reader_acquired, release_reader),
            )
            writer = processes.Process(
                target=_wait_for_writer,
                args=(root, writer_attempting, writer_acquired),
            )
            reader.start()
            self.assertTrue(reader_acquired.wait(5))
            writer.start()
            self.assertTrue(writer_attempting.wait(5))
            self.assertFalse(writer_acquired.wait(0.1))
            release_reader.set()
            self.assertTrue(writer_acquired.wait(5))
            reader.join(5)
            writer.join(5)
            self.assertEqual(reader.exitcode, 0)
            self.assertEqual(writer.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
