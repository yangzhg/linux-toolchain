from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed import ManagedLock, ManagedSpec, resolve_lock
from linux_toolchain.managed.assemble import (
    ArtifactDisposition,
    BuildMode,
    CompilerAssemblyState,
    PairAvailability,
    PairedBuildAction,
    PublicationDisposition,
    RuntimeAssemblyState,
    StandaloneBuildAction,
    assemble_variant,
    plan_assembly,
    variant_artifact_paths,
)
from linux_toolchain.managed.selection import select_artifact
from linux_toolchain.recipes import get_recipe


def _lock(*, family: str = "clang") -> ManagedLock:
    compiler = {
        "family": family,
        "versions": ["22" if family == "clang" else "13"],
        "runtimes": (
            [
                {"kind": "libstdc++", "gcc_version": "12"},
                {"kind": "libc++"},
            ]
            if family == "clang"
            else [{"kind": "libstdc++"}]
        ),
    }
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
                "compilers": [compiler],
            }
        )
    )


def _runtime_publication(
    lock: ManagedLock,
    artifact_id: str,
    root: Path,
) -> SimpleNamespace:
    selection = select_artifact(lock, artifact_id)
    return SimpleNamespace(
        root=root,
        selection=SimpleNamespace(
            artifact_id=artifact_id,
            runtime_kind=selection.runtime_kind,
        ),
        manifest=object(),
    )


class ManagedAssemblyTest(unittest.TestCase):
    def test_plan_reuses_a_ready_compiler_and_repairs_publications(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        libcxx = next(
            runtime for runtime in variant.runtimes if runtime.kind == "libc++"
        )
        libstdcxx = next(
            runtime for runtime in variant.runtimes if runtime.kind == "libstdc++"
        )
        gcc_provider = lock.provider_compiler_kit(libstdcxx.runtime_id).id

        plan = plan_assembly(
            variant.compiler_kit_id,
            (
                CompilerAssemblyState(
                    artifact_id=variant.compiler_kit_id,
                    disposition=ArtifactDisposition.REUSE,
                ),
            ),
            (
                RuntimeAssemblyState(
                    runtime=libcxx,
                    provider_compiler_kit_id=variant.compiler_kit_id,
                    artifact=ArtifactDisposition.REBUILD,
                    publication=PublicationDisposition.PUBLISH,
                    pairing=PairAvailability.AVAILABLE,
                ),
                RuntimeAssemblyState(
                    runtime=libstdcxx,
                    provider_compiler_kit_id=gcc_provider,
                    artifact=ArtifactDisposition.REUSE,
                    publication=PublicationDisposition.REPLACE,
                    pairing=PairAvailability.UNAVAILABLE,
                ),
            ),
        )

        self.assertEqual(len(plan.builds), 1)
        paired = plan.builds[0]
        self.assertIsInstance(paired, PairedBuildAction)
        assert isinstance(paired, PairedBuildAction)
        self.assertIs(paired.compiler_mode, BuildMode.PRESERVE)
        self.assertIs(paired.runtime_mode, BuildMode.REBUILD)
        self.assertEqual(
            [(action.artifact_id, action.force) for action in plan.publications],
            [(libcxx.runtime_id, False), (libstdcxx.runtime_id, True)],
        )

    def test_plan_falls_back_to_standalone_builds_without_a_pair_workspace(
        self,
    ) -> None:
        lock = _lock()
        variant = lock.variants[0]
        compiler_states = [
            CompilerAssemblyState(
                artifact_id=variant.compiler_kit_id,
                disposition=ArtifactDisposition.BUILD,
            )
        ]
        runtime_states = []
        for runtime in variant.runtimes:
            provider = lock.provider_compiler_kit(runtime.runtime_id).id
            if provider != variant.compiler_kit_id:
                compiler_states.append(
                    CompilerAssemblyState(
                        artifact_id=provider,
                        disposition=ArtifactDisposition.BUILD,
                    )
                )
            runtime_states.append(
                RuntimeAssemblyState(
                    runtime=runtime,
                    provider_compiler_kit_id=provider,
                    artifact=ArtifactDisposition.BUILD,
                    publication=PublicationDisposition.PUBLISH,
                    pairing=PairAvailability.UNAVAILABLE,
                )
            )

        plan = plan_assembly(
            variant.compiler_kit_id,
            tuple(compiler_states),
            tuple(runtime_states),
        )

        self.assertTrue(plan.needs_producer)
        self.assertTrue(
            all(isinstance(action, StandaloneBuildAction) for action in plan.builds)
        )
        self.assertEqual(
            [action.artifact_id for action in plan.builds],
            [
                variant.compiler_kit_id,
                *(runtime.runtime_id for runtime in variant.runtimes),
            ],
        )
        self.assertTrue(all(action.mode is BuildMode.CREATE for action in plan.builds))

    def test_compilers_and_matching_runtimes_share_builds(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        llvm_runtime_id = variant.runtime_id("libc++")
        gcc_runtime_id = variant.runtime_id("libstdc++")
        gcc_kit_id = next(
            kit.id
            for kit in lock.compiler_kits
            if kit.family == "gcc" and kit.version == "12.5.0"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "managed"
            sdk = get_recipe("x86_64", "2.19").to_spec()
            paths = variant_artifact_paths(lock, variant.id, work, sdk, sdk)
            other_lock_paths = variant_artifact_paths(
                replace(lock, catalog_sha256="0" * 64),
                variant.id,
                work,
                sdk,
                sdk,
            )
            self.assertEqual(other_lock_paths.compiler_kit, paths.compiler_kit)
            self.assertEqual(
                tuple(runtime.publication for runtime in other_lock_paths.runtimes),
                tuple(runtime.publication for runtime in paths.runtimes),
            )
            self.assertNotEqual(other_lock_paths.runtime_set, paths.runtime_set)
            publications: dict[str, SimpleNamespace] = {}
            runtime_set = SimpleNamespace(root=paths.runtime_set, variant=variant)
            binding_manifest = root / "binding/binding.json"

            def build(*args: object, **kwargs: object) -> Path:
                artifact_id = str(args[1])
                paired_runtime_id = kwargs.get("paired_runtime_id")
                if paired_runtime_id is not None:
                    self.assertIn(
                        (artifact_id, paired_runtime_id),
                        {
                            (variant.compiler_kit_id, llvm_runtime_id),
                            (gcc_kit_id, gcc_runtime_id),
                        },
                    )
                    compiler_manifest = (
                        Path(args[2]) / "output" / "artifacts" / "artifact.json"
                    )
                    runtime_manifest = (
                        Path(str(kwargs["paired_runtime_workspace"]))
                        / "output"
                        / "artifacts"
                        / "artifact.json"
                    )
                    for manifest in (compiler_manifest, runtime_manifest):
                        manifest.parent.mkdir(parents=True, exist_ok=True)
                        manifest.touch()
                    return compiler_manifest
                self.fail(f"unexpected standalone build: {artifact_id}")

            def publish_component(
                _lock: ManagedLock,
                artifact_id: str,
                _raw: Path,
                destination: Path,
                *,
                force: bool,
            ) -> SimpleNamespace:
                self.assertFalse(force)
                publication = _runtime_publication(lock, artifact_id, Path(destination))
                publications[artifact_id] = publication
                return publication

            with (
                patch(
                    "linux_toolchain.managed.assemble.load_sdk_workspace",
                    return_value=sdk,
                ),
                patch(
                    "linux_toolchain.managed.assemble.validate_producer_inputs",
                    return_value=SimpleNamespace(),
                ) as producer,
                patch("linux_toolchain.managed.assemble.render_workspace") as renderer,
                patch(
                    "linux_toolchain.managed.assemble.build_with_docker",
                    side_effect=build,
                ) as builder,
                patch(
                    "linux_toolchain.managed.assemble.load_managed_compiler_artifact",
                    return_value=SimpleNamespace(root=paths.compiler_kit),
                ),
                patch(
                    "linux_toolchain.managed.assemble.publish_managed_runtime_publication",
                    side_effect=publish_component,
                ),
                patch(
                    "linux_toolchain.managed.assemble.publish_managed_runtime_set",
                    return_value=runtime_set,
                ) as set_publisher,
                patch(
                    "linux_toolchain.compiler.managed_binding."
                    "create_managed_binding_from_artifacts",
                    return_value=binding_manifest,
                ) as binder,
            ):
                result = assemble_variant(
                    lock,
                    variant.id,
                    root / "sdk-workspace",
                    root / "sdk-workspace",
                    work,
                    root / "binding",
                    jobs=16,
                    source_cache=root / "sources",
                )

            self.assertEqual(result.binding_manifest, binding_manifest)
            producer.assert_called_once()
            self.assertEqual(builder.call_count, 2)
            self.assertEqual(
                {
                    (call.args[1], call.kwargs["paired_runtime_id"])
                    for call in builder.call_args_list
                },
                {
                    (variant.compiler_kit_id, llvm_runtime_id),
                    (gcc_kit_id, gcc_runtime_id),
                },
            )
            self.assertEqual(
                {call.args[1] for call in renderer.call_args_list},
                {
                    variant.compiler_kit_id,
                    llvm_runtime_id,
                    gcc_kit_id,
                    gcc_runtime_id,
                },
            )
            set_publisher.assert_called_once_with(
                lock,
                variant.id,
                publications,
                paths.runtime_set,
                force=False,
            )
            self.assertIs(
                binder.call_args.kwargs["runtime_set_publication"],
                runtime_set,
            )

    def test_valid_component_publications_skip_all_builds(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "managed"
            sdk = get_recipe("x86_64", "2.19").to_spec()
            paths = variant_artifact_paths(lock, variant.id, work, sdk, sdk)
            compiler = SimpleNamespace(root=paths.compiler_kit)
            publications = {
                runtime.runtime_id: _runtime_publication(
                    lock,
                    runtime.runtime_id,
                    paths.runtime(runtime.runtime_id).publication,
                )
                for runtime in variant.runtimes
            }
            (paths.compiler_kit / "artifact.json").parent.mkdir(parents=True)
            (paths.compiler_kit / "artifact.json").touch()
            for runtime in variant.runtimes:
                receipt = (
                    paths.runtime(runtime.runtime_id).publication
                    / "managed-publication.json"
                )
                receipt.parent.mkdir(parents=True)
                receipt.touch()
            runtime_set = SimpleNamespace(root=paths.runtime_set, variant=variant)

            with (
                patch(
                    "linux_toolchain.managed.assemble.load_sdk_workspace",
                    return_value=sdk,
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_compiler_artifact",
                    return_value=compiler,
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_runtime_publication",
                    side_effect=lambda _lock, artifact_id, _path: publications[
                        artifact_id
                    ],
                ),
                patch(
                    "linux_toolchain.managed.assemble.validate_producer_inputs"
                ) as producer,
                patch("linux_toolchain.managed.assemble.render_workspace") as renderer,
                patch("linux_toolchain.managed.assemble.build_with_docker") as builder,
                patch(
                    "linux_toolchain.managed.assemble.publish_managed_runtime_publication"
                ) as component_publisher,
                patch(
                    "linux_toolchain.managed.assemble.publish_managed_runtime_set",
                    return_value=runtime_set,
                ) as set_publisher,
                patch(
                    "linux_toolchain.compiler.managed_binding."
                    "create_managed_binding_from_artifacts",
                    return_value=root / "binding/binding.json",
                ),
            ):
                assemble_variant(
                    lock,
                    variant.id,
                    root / "sdk-workspace",
                    root / "sdk-workspace",
                    work,
                    root / "binding",
                )

            producer.assert_not_called()
            renderer.assert_not_called()
            builder.assert_not_called()
            component_publisher.assert_not_called()
            set_publisher.assert_called_once_with(
                lock,
                variant.id,
                publications,
                paths.runtime_set,
                force=False,
            )

    def test_repair_republishes_only_the_invalid_component(self) -> None:
        lock = _lock()
        variant = lock.variants[0]
        invalid_runtime_id = variant.runtime_id("libstdc++")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = root / "managed"
            sdk = get_recipe("x86_64", "2.19").to_spec()
            paths = variant_artifact_paths(lock, variant.id, work, sdk, sdk)
            publications = {
                runtime.runtime_id: _runtime_publication(
                    lock,
                    runtime.runtime_id,
                    paths.runtime(runtime.runtime_id).publication,
                )
                for runtime in variant.runtimes
            }
            (paths.compiler_kit / "artifact.json").parent.mkdir(parents=True)
            (paths.compiler_kit / "artifact.json").touch()
            for runtime in variant.runtimes:
                runtime_paths = paths.runtime(runtime.runtime_id)
                receipt = runtime_paths.publication / "managed-publication.json"
                receipt.parent.mkdir(parents=True)
                receipt.touch()
            invalid_raw_manifest = (
                paths.runtime(invalid_runtime_id).raw / "artifact.json"
            )
            invalid_raw_manifest.parent.mkdir(parents=True)
            invalid_raw_manifest.touch()
            repaired = _runtime_publication(
                lock,
                invalid_runtime_id,
                paths.runtime(invalid_runtime_id).publication,
            )

            def validate(publication: SimpleNamespace) -> None:
                if publication.selection.artifact_id == invalid_runtime_id:
                    raise ConfigurationError("invalid runtime publication")

            with (
                patch(
                    "linux_toolchain.managed.assemble.load_sdk_workspace",
                    return_value=sdk,
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_compiler_artifact",
                    return_value=SimpleNamespace(root=paths.compiler_kit),
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_runtime_publication",
                    side_effect=lambda _lock, artifact_id, _path: publications[
                        artifact_id
                    ],
                ),
                patch(
                    "linux_toolchain.managed.assemble._validate_runtime_publication_payload",
                    side_effect=validate,
                ),
                patch(
                    "linux_toolchain.managed.assemble.load_managed_runtime_artifact",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "linux_toolchain.managed.assemble.validate_producer_inputs"
                ) as producer,
                patch("linux_toolchain.managed.assemble.render_workspace") as renderer,
                patch("linux_toolchain.managed.assemble.build_with_docker") as builder,
                patch(
                    "linux_toolchain.managed.assemble.publish_managed_runtime_publication",
                    return_value=repaired,
                ) as component_publisher,
                patch(
                    "linux_toolchain.managed.assemble.publish_managed_runtime_set",
                    return_value=SimpleNamespace(
                        root=paths.runtime_set,
                        variant=variant,
                    ),
                ),
                patch(
                    "linux_toolchain.compiler.managed_binding."
                    "create_managed_binding_from_artifacts",
                    return_value=root / "binding/binding.json",
                ),
            ):
                assemble_variant(
                    lock,
                    variant.id,
                    root / "sdk-workspace",
                    root / "sdk-workspace",
                    work,
                    root / "binding",
                    repair=True,
                )

            producer.assert_not_called()
            renderer.assert_not_called()
            builder.assert_not_called()
            component_publisher.assert_called_once_with(
                lock,
                invalid_runtime_id,
                paths.runtime(invalid_runtime_id).raw,
                paths.runtime(invalid_runtime_id).publication,
                force=True,
            )
