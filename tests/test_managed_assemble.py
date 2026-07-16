from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed import ManagedLock, ManagedSpec, resolve_lock
from linux_toolchain.managed.assemble import assemble_variant, variant_artifact_paths
from linux_toolchain.recipes import get_recipe


def _lock(*, libcxx: bool = False) -> ManagedLock:
    runtime = {"kind": "libc++"} if libcxx else {"kind": "libstdc++"}
    family = "clang" if libcxx else "gcc"
    version = "22" if libcxx else "13"
    return resolve_lock(
        ManagedSpec.from_dict(
            {
                "schema": "linux-toolchain-managed-spec",
                "format": 1,
                "name": "assemble-test",
                "build_platform": "linux/amd64",
                "host": {
                    "os": "linux",
                    "arch": "x86_64",
                    "glibc_floor": "2.35",
                },
                "targets": [{"arch": "x86_64", "glibc_floor": "2.19"}],
                "compilers": [
                    {
                        "family": family,
                        "versions": [version],
                        "runtimes": [runtime],
                    }
                ],
            }
        )
    )


def _runtime_publication(root: Path, artifact_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        selection=SimpleNamespace(
            artifact_id=artifact_id,
            runtime_kind="gcc-runtime",
        ),
        manifest=object(),
    )


class ManagedAssemblyTest(unittest.TestCase):
    def test_builds_variant_without_requiring_artifact_ids(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk_workspace = root / "sdk-workspace"
            compiler_backend_workspace = root / "compiler-backend-workspace"
            work = root / "managed"
            source_cache = root / "shared-source-cache"
            output = root / "binding"
            sdk = get_recipe("x86_64", "2.19").to_spec()
            paths = variant_artifact_paths(lock, variant.id, work, sdk, sdk)
            kit_artifact = paths.compiler_kit
            runtime_artifact = paths.raw_runtime
            kit_manifest = kit_artifact / "artifact.json"
            runtime_manifest = runtime_artifact / "artifact.json"
            binding_manifest = output / "binding.json"
            published_runtime = _runtime_publication(paths.runtime, variant.runtime_id)
            source_progress: list[tuple[int, int]] = []

            def record_source_progress(completed: int, total: int) -> None:
                source_progress.append((completed, total))

            def build_artifact(*args: object, **kwargs: object) -> Path:
                for manifest in (kit_manifest, runtime_manifest):
                    manifest.parent.mkdir(parents=True, exist_ok=True)
                    manifest.touch()
                return kit_manifest

            with (
                patch(
                    "linux_toolchain.managed.assemble.validate_producer_inputs",
                    return_value=(),
                ),
                patch("linux_toolchain.managed.assemble.render_workspace") as renderer,
                patch(
                    "linux_toolchain.managed.assemble.load_sdk_workspace",
                    return_value=sdk,
                ),
                patch(
                    "linux_toolchain.managed.assemble.build_with_docker",
                    side_effect=build_artifact,
                ) as builder,
                patch(
                    "linux_toolchain.managed.assemble.load_managed_compiler_artifact"
                ) as compiler_loader,
                patch("linux_toolchain.managed.assemble.load_managed_runtime_artifact"),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_runtime_publication",
                    return_value=published_runtime,
                ) as publication_loader,
                patch(
                    "linux_toolchain.managed.assemble._publish_managed_runtime_loaded",
                    return_value=published_runtime,
                ) as publisher,
                patch(
                    "linux_toolchain.compiler.managed_binding.create_managed_binding",
                    return_value=binding_manifest,
                ) as binder,
            ):
                result = assemble_variant(
                    lock,
                    variant.id,
                    sdk_workspace,
                    compiler_backend_workspace,
                    work,
                    output,
                    jobs=8,
                    source_cache=source_cache,
                    source_progress=record_source_progress,
                )

            self.assertEqual(result.binding_manifest, binding_manifest)
            self.assertEqual(renderer.call_count, 2)
            builder.assert_called_once()
            self.assertEqual(
                builder.call_args.kwargs["paired_runtime_id"], variant.runtime_id
            )
            self.assertEqual(
                builder.call_args.kwargs["paired_runtime_workspace"],
                paths.runtime_workspace,
            )
            self.assertTrue(renderer.call_args_list[0].kwargs["paired_runtime"])
            self.assertTrue(
                all(
                    call.kwargs["compiler_backend"] == compiler_backend_workspace
                    for call in renderer.call_args_list
                )
            )
            self.assertTrue(
                all(
                    call.kwargs["source_cache"] == source_cache.resolve()
                    for call in renderer.call_args_list
                )
            )
            publisher.assert_called_once_with(
                lock,
                variant.runtime_id,
                runtime_artifact,
                paths.runtime,
                force=False,
            )
            binder.assert_called_once()
            self.assertEqual(
                binder.call_args.args,
                (sdk_workspace / "sdk", output, kit_artifact),
            )
            self.assertIs(
                binder.call_args.kwargs["_compiler_artifact"],
                compiler_loader.return_value,
            )
            self.assertIs(
                binder.call_args.kwargs["_runtime_publication"],
                published_runtime,
            )
            compiler_loader.assert_called_once()
            publication_loader.assert_not_called()
            self.assertEqual(
                builder.call_args.kwargs["source_progress"], record_source_progress
            )

    def test_paired_resume_and_complete_pair_reuse(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        for complete in (False, True):
            with (
                self.subTest(complete=complete),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                work = root / "managed"
                sdk = get_recipe("x86_64", "2.19").to_spec()
                paths = variant_artifact_paths(lock, variant.id, work, sdk, sdk)
                ready = (paths.compiler_kit, paths.raw_runtime)
                if not complete:
                    for workspace in (
                        paths.compiler_kit_workspace,
                        paths.runtime_workspace,
                    ):
                        (workspace / "build").mkdir(parents=True)
                        (workspace / "workspace.json").write_text(
                            '{"build_script": {"paired_runtime": false}}\n',
                            encoding="utf-8",
                        )
                    (paths.compiler_kit_workspace / "workspace.json").write_text(
                        '{"build_script": {"paired_runtime": true}}\n',
                        encoding="utf-8",
                    )
                    ready = ready[:1]
                for artifact_root in ready:
                    manifest = artifact_root / "artifact.json"
                    manifest.parent.mkdir(parents=True)
                    manifest.touch()

                def finish_runtime(*_args: object, **_kwargs: object) -> Path:
                    manifest = paths.raw_runtime / "artifact.json"
                    manifest.parent.mkdir(parents=True)
                    manifest.touch()
                    return paths.compiler_kit / "artifact.json"

                with (
                    patch(
                        "linux_toolchain.managed.assemble.validate_producer_inputs",
                        return_value=(),
                    ),
                    patch(
                        "linux_toolchain.managed.assemble.render_workspace"
                    ) as renderer,
                    patch(
                        "linux_toolchain.managed.assemble.load_sdk_workspace",
                        return_value=sdk,
                    ),
                    patch(
                        "linux_toolchain.managed.assemble.build_with_docker",
                        side_effect=finish_runtime,
                    ) as builder,
                    patch(
                        "linux_toolchain.managed.assemble.load_managed_compiler_artifact"
                    ),
                    patch(
                        "linux_toolchain.managed.assemble.load_managed_runtime_artifact"
                    ),
                    patch(
                        "linux_toolchain.managed.assemble.load_managed_runtime_publication",
                        return_value=_runtime_publication(
                            paths.runtime, variant.runtime_id
                        ),
                    ),
                    patch(
                        "linux_toolchain.managed.assemble._publish_managed_runtime_loaded",
                        return_value=_runtime_publication(
                            paths.runtime, variant.runtime_id
                        ),
                    ),
                    patch(
                        "linux_toolchain.compiler.managed_binding.create_managed_binding",
                        return_value=root / "binding/binding.json",
                    ),
                ):
                    assemble_variant(
                        lock,
                        variant.id,
                        root / "sdk",
                        root / "backend",
                        work,
                        root / "binding",
                    )

                renderer.assert_not_called()
                if complete:
                    builder.assert_not_called()
                else:
                    builder.assert_called_once()
                    self.assertTrue(builder.call_args.kwargs["preserve_primary"])
                    self.assertFalse(builder.call_args.kwargs["preserve_runtime"])

    def test_repair_rebuilds_only_an_invalid_matching_compiler_kit(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "managed"
            sdk = get_recipe("x86_64", "2.19").to_spec()
            paths = variant_artifact_paths(lock, variant.id, work, sdk, sdk)
            (paths.compiler_kit / "artifact.json").parent.mkdir(parents=True)
            (paths.compiler_kit / "artifact.json").touch()
            (paths.runtime / "managed-publication.json").parent.mkdir(parents=True)
            (paths.runtime / "managed-publication.json").touch()

            with (
                patch(
                    "linux_toolchain.managed.assemble.load_sdk_workspace",
                    return_value=sdk,
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_compiler_artifact",
                    side_effect=(
                        ConfigurationError("invalid compiler payload"),
                        SimpleNamespace(root=paths.compiler_kit),
                    ),
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_runtime_publication",
                    return_value=_runtime_publication(
                        paths.runtime, variant.runtime_id
                    ),
                ),
                patch("linux_toolchain.runtime.validate_runtime_manifest"),
                patch(
                    "linux_toolchain.managed.assemble.validate_producer_inputs",
                    return_value=(),
                ),
                patch("linux_toolchain.managed.assemble.render_workspace") as renderer,
                patch(
                    "linux_toolchain.managed.assemble.build_with_docker",
                    return_value=paths.compiler_kit / "artifact.json",
                ) as builder,
                patch(
                    "linux_toolchain.managed.assemble._publish_managed_runtime_loaded"
                ) as publisher,
                patch(
                    "linux_toolchain.compiler.managed_binding.create_managed_binding",
                    return_value=root / "binding/binding.json",
                ),
            ):
                result = assemble_variant(
                    lock,
                    variant.id,
                    root / "sdk",
                    root / "backend",
                    work,
                    root / "binding",
                    repair=True,
                )

            self.assertEqual(result.compiler_kit, paths.compiler_kit)
            renderer.assert_called_once()
            self.assertEqual(renderer.call_args.args[1], variant.compiler_kit_id)
            self.assertTrue(renderer.call_args.kwargs["force"])
            builder.assert_called_once()
            publisher.assert_not_called()

    def test_repair_republishes_invalid_runtime_without_rebuilding(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "managed"
            sdk = get_recipe("x86_64", "2.19").to_spec()
            paths = variant_artifact_paths(lock, variant.id, work, sdk, sdk)
            for manifest in (
                paths.compiler_kit / "artifact.json",
                paths.raw_runtime / "artifact.json",
                paths.runtime / "managed-publication.json",
            ):
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.touch()

            with (
                patch(
                    "linux_toolchain.managed.assemble.load_sdk_workspace",
                    return_value=sdk,
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_compiler_artifact"
                ),
                patch("linux_toolchain.managed.assemble.load_managed_runtime_artifact"),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_runtime_publication",
                    return_value=_runtime_publication(
                        paths.runtime, variant.runtime_id
                    ),
                ),
                patch(
                    "linux_toolchain.runtime.validate_runtime_manifest",
                    side_effect=ConfigurationError("invalid runtime payload"),
                ),
                patch(
                    "linux_toolchain.managed.assemble.validate_producer_inputs"
                ) as producer,
                patch("linux_toolchain.managed.assemble.render_workspace") as renderer,
                patch("linux_toolchain.managed.assemble.build_with_docker") as builder,
                patch(
                    "linux_toolchain.managed.assemble._publish_managed_runtime_loaded",
                    return_value=_runtime_publication(
                        paths.runtime, variant.runtime_id
                    ),
                ) as publisher,
                patch(
                    "linux_toolchain.compiler.managed_binding.create_managed_binding",
                    return_value=root / "binding/binding.json",
                ),
            ):
                assemble_variant(
                    lock,
                    variant.id,
                    root / "sdk",
                    root / "backend",
                    work,
                    root / "binding",
                    repair=True,
                )

            producer.assert_not_called()
            renderer.assert_not_called()
            builder.assert_not_called()
            publisher.assert_called_once_with(
                lock,
                variant.runtime_id,
                paths.raw_runtime,
                paths.runtime,
                force=True,
            )
