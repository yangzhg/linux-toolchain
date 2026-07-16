import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from linux_toolchain.diagnostics import DiagnosticReport
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed import resolve_lock, write_lockfile
from linux_toolchain.managed.assemble import (
    AssemblyResult,
    variant_artifact_paths,
)
from linux_toolchain.managed.contracts import MANAGED_DEFAULT_HOST_GLIBC_FLOOR
from linux_toolchain.managed.publication import (
    ManagedCompilerArtifact,
    ManagedRuntimePublication,
)
from linux_toolchain.models import SdkSpec
from linux_toolchain.producer_store import ProducerStore
from linux_toolchain.recipes import get_recipe
from linux_toolchain.sdk.crosstool_ng import FULL_BUILD_GOAL, SDK_BUILD_GOAL
from linux_toolchain.setup import (
    ConanRunConfig,
    PreparedSetup,
    PreparedSetupInputs,
    SetupConfig,
    _ensure_sdk,
    load_prepared_setup_state,
    lock_prepared_setup_inputs,
    prepare_setup,
    setup_toolchain,
)
from linux_toolchain.smoke import SmokeFailure


def setup_config(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "linux-toolchain-setup",
        "format": 1,
        "compiler": "gcc@12",
        "target": {"arch": "x86_64", "glibc_floor": "2.19"},
        "integration": "conan",
        "host_glibc_floor": MANAGED_DEFAULT_HOST_GLIBC_FLOOR,
    }
    value.update(overrides)
    return value


def create_conan_run_config(home: Path, build_profile: Path) -> ConanRunConfig:
    settings = home / "settings_user.yml"
    settings.write_text("settings\n", encoding="utf-8")
    return ConanRunConfig(home=home, build_profile=build_profile)


def validated_artifacts() -> tuple[
    ManagedCompilerArtifact,
    ManagedRuntimePublication,
]:
    return (
        cast(ManagedCompilerArtifact, object()),
        cast(ManagedRuntimePublication, object()),
    )


def write_smoke_fixture(
    result: Path,
    *,
    binding: Path,
    integration: str,
    glibc_floor: str = "2.19",
    build_type: str = "Release",
    conan: ConanRunConfig | None = None,
) -> None:
    result.parent.mkdir(parents=True, exist_ok=True)
    artifacts = result.parent / "cmake" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    outputs = (
        artifacts / "linux_toolchain_smoke",
        artifacts / "liblinux_toolchain_smoke.so",
    )
    for output in outputs:
        output.touch()
    evidence = ("audit-report.json", "loader-closure.txt", "runtime-output.txt")
    for filename in evidence:
        (result.parent / filename).touch()
    value: dict[str, object] = {
        "schema": "linux-toolchain-smoke-result",
        "format": 1,
        "status": "passed",
        "binding": str(binding),
        "integration": integration,
        "build_type": build_type,
        "glibc": {"policy_floor": glibc_floor, "observed_maximum": glibc_floor},
        "artifacts": [str(output) for output in outputs],
        "evidence": list(evidence),
    }
    if integration == "conan":
        assert conan is not None
        value["conan_home"] = str(conan.home)
        value["build_profile"] = str(conan.build_profile)
    write_json(result, value)


def write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sdk_spec(
    config: SetupConfig,
    *,
    arch: str | None = None,
    glibc_floor: str | None = None,
) -> SdkSpec:
    selected_arch = config.target.arch if arch is None else arch
    selected_floor = config.target.glibc_floor if glibc_floor is None else glibc_floor
    return get_recipe(selected_arch, selected_floor).to_spec(
        name=f"setup-{selected_arch}-glibc-{selected_floor}",
    )


def compiler_backend_spec(config: SetupConfig) -> SdkSpec:
    return sdk_spec(
        config,
        arch=config.target.arch,
        glibc_floor=config.host_glibc_floor,
    )


def create_prepared(
    root: Path,
    *,
    integration: str = "shell",
    qualified: bool,
) -> tuple[Path, Path, Path, PreparedSetup]:
    config_path = root / "setup.json"
    write_json(config_path, setup_config(integration=integration))
    config = SetupConfig.load(config_path)
    state = root / "state"
    state.mkdir()
    (state / ".linux-toolchain-setup-state").write_text("format=1\n", encoding="utf-8")
    store = ProducerStore.prepare(state / "producer")
    target_sdk = sdk_spec(config)
    compiler_backend = compiler_backend_spec(config)
    resolved_lock = resolve_lock(config.managed_spec())
    sdk_workspace = store.sdk_workspace(target_sdk)
    managed_workspace = store.managed_workspace(
        target_sdk,
        compiler_backend,
    )
    (sdk_workspace / "sdk").mkdir(parents=True)
    managed_workspace.mkdir(parents=True)
    compiler_kit = managed_workspace / "compiler-kit"
    runtime = managed_workspace / "runtime"
    compiler_kit.mkdir()
    runtime.mkdir()
    binding = state / "binding"
    required = [
        "binding.json",
        "audit-policy.json",
        "env/toolchain.env",
        "cmake/toolchain.cmake",
        "conan/host.profile",
        "conan/cmake-toolchain.cmake",
        "conan/cmake-late.cmake",
    ]
    for relative in required:
        path = binding / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    lock = state / "managed.lock.json"
    write_lockfile(resolved_lock, lock)
    conan = None
    if integration == "conan":
        conan_home = root / "conan-home"
        build_profile = conan_home / "profiles" / "default"
        build_profile.parent.mkdir(parents=True)
        build_profile.touch()
        conan = create_conan_run_config(conan_home, build_profile)
    smoke_result = state / f"smoke-{integration}/result.json" if qualified else None
    if smoke_result is not None:
        write_smoke_fixture(
            smoke_result,
            binding=binding,
            integration=integration,
            conan=conan,
        )
    prepared = PreparedSetup(
        config_sha256=config.selection_sha256,
        binding=binding,
        lock=lock,
        variant=resolved_lock.variants[0].id,
        sdk_workspace=sdk_workspace,
        managed_workspace=managed_workspace,
        compiler_kit=compiler_kit,
        runtime=runtime,
        integration=integration,
        smoke_result=smoke_result,
        conan=conan,
    )
    write_json(state / "prepared.json", prepared.to_dict())
    return config_path, state, binding, prepared


class SetupConfigTest(unittest.TestCase):
    def test_setup_separates_work_directory_from_installation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "toolchain"
            work_dir = root / "work"
            store = ProducerStore.prepare(root / "store")
            launcher = prefix / "bin" / "lxtc"
            state = work_dir / "state"
            config = SetupConfig.from_dict(
                setup_config(
                    host_glibc_floor="2.28",
                    jobs=8,
                    conan={
                        "cppstd": "gnu20",
                        "build_type": "RelWithDebInfo",
                        "build_profile": "native",
                    },
                )
            )
            lock = resolve_lock(config.managed_spec())
            target_sdk = sdk_spec(config)
            compiler_backend = compiler_backend_spec(config)
            prepared = PreparedSetup(
                config_sha256=config.selection_sha256,
                binding=state / "binding",
                lock=state / "managed.lock.json",
                variant=lock.variants[0].id,
                sdk_workspace=store.sdk_workspace(target_sdk),
                managed_workspace=store.managed_workspace(
                    target_sdk,
                    compiler_backend,
                ),
                compiler_kit=store.root / "managed/compiler-kit",
                runtime=store.root / "managed/runtime",
                integration="conan",
                smoke_result=state / "smoke-conan/result.json",
                conan=None,
            )
            compiler_artifact, runtime_publication = validated_artifacts()
            prepared_inputs = PreparedSetupInputs(
                lock=lock,
                sdk=prepared.sdk_workspace / "sdk",
                compiler_kit=prepared.compiler_kit,
                runtime=prepared.runtime,
                binding=prepared.binding,
                compiler_artifact=compiler_artifact,
                runtime_publication=runtime_publication,
            )
            lease_active = False

            @contextmanager
            def hold_producer_inputs(*args: object, **kwargs: object):
                nonlocal lease_active
                lease_active = True
                try:
                    yield prepared_inputs
                finally:
                    lease_active = False

            def publish(**kwargs: object) -> Path:
                self.assertTrue(lease_active)
                return launcher

            with (
                patch("linux_toolchain.setup.platform.machine", return_value="amd64"),
                patch(
                    "linux_toolchain.setup._prepare_setup_unlocked",
                    return_value=prepared,
                ),
                patch(
                    "linux_toolchain.setup._lock_prepared_producer_inputs",
                    side_effect=hold_producer_inputs,
                ),
                patch(
                    "linux_toolchain.setup.publish_installation",
                    side_effect=publish,
                ) as publisher,
            ):
                result = setup_toolchain(
                    "gcc@12",
                    prefix=prefix,
                    work_dir=work_dir,
                    store_dir=store.root,
                    arch=None,
                    glibc_floor="2.19",
                    integration="conan",
                    host_glibc_floor="2.28",
                    jobs=8,
                    conan_cppstd="gnu20",
                    conan_build_type="RelWithDebInfo",
                    conan_build_profile="native",
                )
            saved_config = SetupConfig.load(work_dir / "setup.json")
            self.assertEqual(result, launcher)
            self.assertEqual(saved_config, config)
            self.assertTrue((work_dir / ".linux-toolchain-setup-root").is_file())
            self.assertFalse((prefix / "setup.json").exists())
            self.assertFalse((prefix / "state").exists())
            self.assertEqual(publisher.call_args.kwargs["prefix"], prefix)
            self.assertEqual(
                publisher.call_args.kwargs["binding_template"], prepared.binding
            )
            self.assertIsNone(publisher.call_args.kwargs["conan_home"])
            self.assertIsNone(publisher.call_args.kwargs["conan_build_profile"])

            with (
                patch("linux_toolchain.setup.platform.machine", return_value="amd64"),
                self.assertRaisesRegex(ConfigurationError, "different toolchain"),
            ):
                setup_toolchain(
                    "gcc@13",
                    prefix=prefix,
                    work_dir=work_dir,
                    arch="x86_64",
                    glibc_floor="2.24",
                    integration="shell",
                )

    def test_prepare_only_does_not_publish_the_installation_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "work"
            store = root / "store"
            prepared = work_dir / "state" / "prepared.json"
            with (
                patch.dict("os.environ", {"XDG_CACHE_HOME": "relative-cache"}),
                patch("linux_toolchain.setup.platform.machine", return_value="amd64"),
                patch(
                    "linux_toolchain.setup._prepare_setup_unlocked",
                    return_value=object(),
                ),
                patch("linux_toolchain.setup.publish_installation") as publisher,
            ):
                result = setup_toolchain(
                    "gcc@12",
                    prefix=None,
                    work_dir=work_dir,
                    store_dir=store,
                    arch=None,
                    glibc_floor="2.19",
                    integration="shell",
                    install=False,
                )

            self.assertEqual(result, prepared)
            publisher.assert_not_called()

            with (
                patch("linux_toolchain.setup._prepare_setup_unlocked") as orchestrator,
                self.assertRaisesRegex(ConfigurationError, "requires --prefix"),
            ):
                setup_toolchain(
                    "gcc@12",
                    prefix=None,
                    work_dir=root / "normal-setup",
                    arch=None,
                    glibc_floor="2.19",
                    integration="shell",
                )
            orchestrator.assert_not_called()

    def test_default_work_directory_is_derived_from_the_normalized_prefix(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"

            with (
                patch.dict("os.environ", {"XDG_CACHE_HOME": str(cache)}),
                patch("linux_toolchain.setup.platform.machine", return_value="amd64"),
                patch(
                    "linux_toolchain.setup._prepare_setup_unlocked",
                    return_value=object(),
                ),
            ):
                results = tuple(
                    setup_toolchain(
                        "gcc@12",
                        prefix=prefix,
                        work_dir=None,
                        store_dir=root / "store",
                        arch=None,
                        glibc_floor="2.19",
                        integration="shell",
                        install=False,
                    )
                    for prefix in (
                        Path("/opt/team-a/toolchain"),
                        Path("/srv/team-b/toolchain"),
                        Path("/opt/team-a/../team-a/toolchain"),
                    )
                )

            work_directories = tuple(result.parents[1] for result in results)
            self.assertNotEqual(work_directories[0], work_directories[1])
            self.assertEqual(work_directories[0], work_directories[2])
            self.assertRegex(
                work_directories[0].name,
                r"^toolchain-[0-9a-f]{12}$",
            )

    def test_setup_supports_native_arm_and_rejects_cross_production(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("linux_toolchain.setup.platform.machine", return_value="ppc64le"),
                self.assertRaisesRegex(ConfigurationError, "x86_64 or AArch64"),
            ):
                setup_toolchain(
                    "gcc@12",
                    prefix=root / "unsupported-host",
                    arch=None,
                    glibc_floor="2.19",
                    integration="shell",
                )

            with (
                patch("linux_toolchain.setup.platform.machine", return_value="amd64"),
                self.assertRaisesRegex(ConfigurationError, "native production only"),
            ):
                setup_toolchain(
                    "gcc@12",
                    prefix=root / "cross",
                    arch="aarch64",
                    glibc_floor="2.19",
                    integration="shell",
                )

            arm_work = root / "arm-work"
            with (
                patch("linux_toolchain.setup.platform.machine", return_value="arm64"),
                patch(
                    "linux_toolchain.compiler.managed._current_host",
                    return_value=("linux", "aarch64", "2.39"),
                ),
                patch(
                    "linux_toolchain.setup._prepare_setup_unlocked",
                    return_value=object(),
                ),
            ):
                setup_toolchain(
                    "gcc@12",
                    prefix=None,
                    work_dir=arm_work,
                    store_dir=root / "store",
                    arch=None,
                    glibc_floor="2.19",
                    integration="shell",
                    install=False,
                )

            arm_config = SetupConfig.load(arm_work / "setup.json")
            self.assertEqual(arm_config.target.arch, "aarch64")
            self.assertEqual(arm_config.managed_spec().build_platform, "linux/arm64")

    def test_minimal_gcc_config_resolves_one_matching_runtime(self) -> None:
        config = SetupConfig.from_dict(setup_config())
        lock = resolve_lock(config.managed_spec())

        self.assertEqual(config.compiler, "gcc@12")
        self.assertEqual(config.jobs, 1)
        self.assertEqual(config.host_glibc_floor, "2.19")
        self.assertEqual(
            config.selected_integrations,
            ("cmake", "shell", "conan"),
        )
        self.assertIsNone(config.conan_settings().cppstd)
        self.assertEqual(len(lock.variants), 1)
        self.assertEqual(lock.variants[0].version, "12.5.0")
        self.assertEqual(lock.runtimes[0].provider_version, "12.5.0")

    def test_format_one_requires_explicit_host_floor(self) -> None:
        value = setup_config()
        value.pop("host_glibc_floor")

        with self.assertRaisesRegex(ConfigurationError, "host_glibc_floor"):
            SetupConfig.from_dict(value)

    def test_setup_rejects_host_floor_before_creating_work_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "work"
            with (
                patch("linux_toolchain.setup.platform.machine", return_value="amd64"),
                patch(
                    "linux_toolchain.compiler.managed._current_host",
                    return_value=("linux", "x86_64", "2.28"),
                ),
                self.assertRaisesRegex(ConfigurationError, r"requires 2\.35.*2\.28"),
            ):
                setup_toolchain(
                    "gcc@12",
                    prefix=root / "prefix",
                    work_dir=work_dir,
                    arch=None,
                    glibc_floor="2.19",
                    integration="shell",
                    host_glibc_floor="2.35",
                )

            self.assertFalse(work_dir.exists())

    def test_clang_runtime_is_explicit_and_gcc_runtime_can_be_older(self) -> None:
        config = SetupConfig.from_dict(
            setup_config(compiler="clang@22", runtime="gcc@10")
        )
        lock = resolve_lock(config.managed_spec())

        self.assertEqual(lock.variants[0].version, "22.1.8")
        self.assertEqual(lock.runtimes[0].provider_version, "10.5.0")

    def test_schema_unknown_fields_and_runtime_mismatches_fail_closed(self) -> None:
        unknown = setup_config(launcher_name="gcc12")
        with self.assertRaisesRegex(ConfigurationError, "unknown keys: launcher_name"):
            SetupConfig.from_dict(unknown)

        with self.assertRaisesRegex(ConfigurationError, "must be omitted"):
            SetupConfig.from_dict(setup_config(runtime="gcc@10"))

        with self.assertRaisesRegex(
            ConfigurationError, "requires setup config.runtime"
        ):
            SetupConfig.from_dict(setup_config(compiler="clang@22"))

        with self.assertRaisesRegex(ConfigurationError, "requires integration"):
            SetupConfig.from_dict(
                setup_config(integration="shell", conan={"cppstd": "gnu17"})
            )

        with self.assertRaisesRegex(ConfigurationError, "conan must be an object"):
            SetupConfig.from_dict(setup_config(conan=False))

        with self.assertRaisesRegex(ConfigurationError, "must be a profile name"):
            SetupConfig.from_dict(setup_config(conan={"build_profile": "../x"}))


class SetupWorkflowTest(unittest.TestCase):
    def test_prepared_state_is_bound_to_the_locked_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            _, _, _, foreign = create_prepared(first, qualified=True)
            config_path, state, _, _ = create_prepared(second, qualified=True)
            write_json(state / "prepared.json", foreign.to_dict())
            config, loaded = load_prepared_setup_state(
                config_path,
                state_directory=state,
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "binding does not match its state directory",
            ):
                with lock_prepared_setup_inputs(config, loaded, state=state):
                    self.fail("foreign prepared state was accepted")

    def test_prepared_consumer_rejects_a_stale_loaded_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, _, prepared = create_prepared(root, qualified=True)
            config = SetupConfig.load(config_path)
            write_json(
                state / "prepared.json",
                replace(prepared, smoke_result=None).to_dict(),
            )

            with self.assertRaisesRegex(ConfigurationError, "changed while waiting"):
                with lock_prepared_setup_inputs(config, prepared, state=state):
                    self.fail("stale prepared state was accepted")

    def test_prepared_artifact_rejects_a_lexical_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, _, prepared = create_prepared(root, qualified=True)
            outside = prepared.managed_workspace.parent / "outside-compiler-kit"
            outside.mkdir()
            escaped = prepared.managed_workspace / ".." / outside.name
            write_json(
                state / "prepared.json",
                replace(prepared, compiler_kit=escaped).to_dict(),
            )
            config, loaded = load_prepared_setup_state(
                config_path,
                state_directory=state,
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                "compiler_kit is outside its managed workspace",
            ):
                with lock_prepared_setup_inputs(config, loaded, state=state):
                    self.fail("escaped prepared artifact was accepted")

    def test_force_repairs_only_a_matching_invalid_sdk_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SetupConfig.from_dict(setup_config(integration="shell"))
            workspace = root / "sdk"
            workspace.mkdir()
            (workspace / "workspace.json").touch()
            expected = sdk_spec(config)

            with (
                patch(
                    "linux_toolchain.setup._sdk_is_ready",
                    side_effect=ConfigurationError("invalid SDK payload"),
                ),
                patch(
                    "linux_toolchain.setup.load_workspace",
                    return_value=expected,
                ),
                patch("linux_toolchain.setup.render_workspace") as renderer,
                patch("linux_toolchain.setup.build_with_docker") as builder,
                patch("linux_toolchain.setup.export_sdk") as exporter,
            ):
                result = _ensure_sdk(
                    config,
                    workspace,
                    source_cache=root / "sources",
                    goal=SDK_BUILD_GOAL,
                    force=True,
                    progress=None,
                )

            self.assertEqual(result, workspace)
            renderer.assert_called_once_with(expected, workspace, force=True)
            builder.assert_called_once()
            exporter.assert_called_once_with(expected, workspace)

            different = get_recipe("x86_64", "2.24").to_spec(name="different")
            with (
                patch("linux_toolchain.setup._sdk_is_ready", return_value=False),
                patch(
                    "linux_toolchain.setup.load_workspace",
                    return_value=different,
                ),
                patch("linux_toolchain.setup.render_workspace") as renderer,
                self.assertRaisesRegex(ConfigurationError, "different configuration"),
            ):
                _ensure_sdk(
                    config,
                    workspace,
                    source_cache=root / "sources",
                    goal=SDK_BUILD_GOAL,
                    force=True,
                    progress=None,
                )
            renderer.assert_not_called()

    def test_prepared_inputs_are_revalidated_inside_shared_producer_leases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, _, _ = create_prepared(root, qualified=True)
            lease_active = False
            revalidated = False

            @contextmanager
            def hold_leases(*args: object, **kwargs: object):
                nonlocal lease_active
                lease_active = True
                try:
                    yield
                finally:
                    lease_active = False

            def validate(*args: object, **kwargs: object) -> tuple[object, object]:
                nonlocal revalidated
                self.assertTrue(lease_active)
                revalidated = True
                return validated_artifacts()

            with (
                patch.object(ProducerStore, "lock_many", side_effect=hold_leases),
                patch(
                    "linux_toolchain.setup._validate_leased_producer_artifacts",
                    side_effect=validate,
                ),
            ):
                config, prepared = load_prepared_setup_state(
                    config_path,
                    state_directory=state,
                )
                with lock_prepared_setup_inputs(config, prepared, state=state):
                    pass

            self.assertTrue(revalidated)

    def test_prepared_smoke_result_fails_closed_for_representative_mismatches(
        self,
    ) -> None:
        cases = (
            ("malformed", None),
            ("wrong schema", {"schema": "wrong-smoke-result"}),
            ("failed", {"status": "failed"}),
            ("wrong integration", {"integration": "cmake"}),
            ("wrong binding", {"binding": "/tmp/other-binding"}),
        )
        for name, changes in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path, state, _, prepared = create_prepared(
                    root,
                    integration="shell",
                    qualified=True,
                )
                assert prepared.smoke_result is not None
                if changes is None:
                    prepared.smoke_result.write_text("{", encoding="utf-8")
                else:
                    value = json.loads(
                        prepared.smoke_result.read_text(encoding="utf-8")
                    )
                    value.update(changes)
                    write_json(prepared.smoke_result, value)
                config, loaded = load_prepared_setup_state(
                    config_path,
                    state_directory=state,
                )
                with self.assertRaises(ConfigurationError):
                    with lock_prepared_setup_inputs(config, loaded, state=state):
                        self.fail("invalid smoke result was accepted")

    def test_explicit_store_mismatch_does_not_touch_requested_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, _, _ = create_prepared(root, qualified=True)
            requested = root / "different-store"

            with self.assertRaisesRegex(ConfigurationError, "different producer store"):
                prepare_setup(
                    config_path,
                    state_directory=state,
                    store_directory=requested,
                )

            self.assertFalse(requested.exists())

    def test_force_failure_clears_qualification_without_rebuilding_ready_sdks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, binding, prepared = create_prepared(
                root,
                integration="shell",
                qualified=True,
            )
            with (
                patch(
                    "linux_toolchain.setup.run_diagnostics",
                    return_value=DiagnosticReport(checks=()),
                ),
                patch("linux_toolchain.setup._sdk_is_ready", return_value=True),
                patch(
                    "linux_toolchain.setup._validate_leased_producer_artifacts",
                    return_value=validated_artifacts(),
                ),
                patch("linux_toolchain.setup.build_with_docker") as sdk_builder,
                patch(
                    "linux_toolchain.setup.assemble_variant",
                    return_value=AssemblyResult(
                        variant_id=prepared.variant,
                        compiler_kit=prepared.compiler_kit,
                        runtime=prepared.runtime,
                        binding_manifest=binding / "binding.json",
                    ),
                ) as assembler,
                patch(
                    "linux_toolchain.setup.run_smoke",
                    side_effect=SmokeFailure("smoke failed"),
                ),
                self.assertRaisesRegex(SmokeFailure, "smoke failed"),
            ):
                prepare_setup(
                    config_path,
                    state_directory=state,
                    force=True,
                )

            sdk_builder.assert_not_called()
            self.assertFalse(assembler.call_args.kwargs.get("rebuild", False))
            self.assertTrue(assembler.call_args.kwargs["repair"])
            failed = PreparedSetup.load(state / "prepared.json")
            self.assertIsNone(failed.smoke_result)

    def test_shared_ready_store_skips_managed_diagnostics_and_sdk_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProducerStore.prepare(root / "store")
            config_path = root / "setup.json"
            write_json(config_path, setup_config(integration="shell"))
            config = SetupConfig.load(config_path)
            target_sdk = sdk_spec(config)
            compiler_backend = compiler_backend_spec(config)
            sdk_workspace = store.sdk_workspace(target_sdk)
            managed_workspace = store.managed_workspace(
                target_sdk,
                compiler_backend,
            )
            binding = root / "state" / "binding"
            lock = resolve_lock(config.managed_spec())
            compiler_kit = managed_workspace / "compiler-kit"
            runtime = managed_workspace / "runtime"

            def assemble(*args: object, **kwargs: object) -> AssemblyResult:
                (sdk_workspace / "sdk").mkdir(parents=True, exist_ok=True)
                compiler_kit.mkdir(parents=True, exist_ok=True)
                runtime.mkdir(parents=True, exist_ok=True)
                for relative in (
                    "binding.json",
                    "audit-policy.json",
                    "env/toolchain.env",
                ):
                    path = binding / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                return AssemblyResult(
                    variant_id=lock.variants[0].id,
                    compiler_kit=compiler_kit,
                    runtime=runtime,
                    binding_manifest=binding / "binding.json",
                )

            diagnostics_seen: list[str] = []

            def diagnostics(workflow: str, *args: object, **kwargs: object) -> object:
                diagnostics_seen.append(workflow)
                return DiagnosticReport(checks=())

            def smoke(args: object) -> int:
                write_smoke_fixture(
                    Path(args.build_dir) / "result.json",
                    binding=Path(args.binding),
                    integration=args.integration,
                    build_type=args.build_type,
                )
                return 0

            with (
                patch("linux_toolchain.setup.run_diagnostics", side_effect=diagnostics),
                patch("linux_toolchain.setup._sdk_is_ready", return_value=True),
                patch(
                    "linux_toolchain.setup._validate_leased_producer_artifacts",
                    return_value=validated_artifacts(),
                ),
                patch("linux_toolchain.setup.build_with_docker") as sdk_builder,
                patch("linux_toolchain.setup.assemble_variant", side_effect=assemble),
                patch("linux_toolchain.setup.run_smoke", side_effect=smoke),
            ):
                prepare_setup(
                    config_path,
                    state_directory=root / "state",
                    store_directory=store.root,
                )

            self.assertEqual(diagnostics_seen, ["consumer"])
            sdk_builder.assert_not_called()

    def test_prepare_orchestrates_one_variant_and_writes_machine_local_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "setup.json"
            config_path.write_text(json.dumps(setup_config()), encoding="utf-8")
            config = SetupConfig.load(config_path)
            state = root / "state"
            state.mkdir()
            (state / ".linux-toolchain-setup-state").write_text(
                "format=1\n", encoding="utf-8"
            )
            store = ProducerStore.prepare(root / "store")
            target_sdk = sdk_spec(config)
            compiler_backend = compiler_backend_spec(config)
            lock = resolve_lock(config.managed_spec())
            sdk_workspace = store.sdk_workspace(target_sdk)
            managed_workspace = store.managed_workspace(
                target_sdk,
                compiler_backend,
            )
            (sdk_workspace / "sdk").mkdir(parents=True)
            managed_workspace.mkdir(parents=True)
            binding = state / "binding"
            binding.mkdir(parents=True)
            binding_manifest = binding / "binding.json"
            binding_manifest.touch()
            for relative in (
                "audit-policy.json",
                "env/toolchain.env",
                "conan/host.profile",
            ):
                path = binding / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            conan_home = root / "conan-home"
            build_profile = conan_home / "profiles" / "default"
            build_profile.parent.mkdir(parents=True)
            build_profile.touch()
            smoke_result = state / "smoke-conan" / "result.json"

            def smoke(args: object) -> int:
                write_smoke_fixture(
                    smoke_result,
                    binding=binding,
                    integration="conan",
                    conan=create_conan_run_config(conan_home, build_profile),
                )
                return 0

            passed = DiagnosticReport(checks=())
            artifact_paths = variant_artifact_paths(
                lock,
                lock.variants[0].id,
                managed_workspace,
                target_sdk,
                compiler_backend,
            )
            artifact_paths.compiler_kit.mkdir(parents=True)
            artifact_paths.runtime.mkdir(parents=True)
            with (
                patch("linux_toolchain.setup.run_diagnostics", return_value=passed),
                patch(
                    "linux_toolchain.setup._ensure_sdk",
                    return_value=sdk_workspace,
                ),
                patch("linux_toolchain.setup._sdk_is_ready", return_value=True),
                patch(
                    "linux_toolchain.setup._validate_leased_producer_artifacts",
                    return_value=validated_artifacts(),
                ),
                patch("linux_toolchain.setup.assemble_variant") as assembler,
                patch("linux_toolchain.setup._prepare_conan") as conan,
                patch("linux_toolchain.setup.run_smoke", side_effect=smoke),
            ):
                assembler.return_value = AssemblyResult(
                    variant_id="variant",
                    compiler_kit=artifact_paths.compiler_kit,
                    runtime=artifact_paths.runtime,
                    binding_manifest=binding_manifest,
                )
                conan.return_value = create_conan_run_config(conan_home, build_profile)
                prepared_path = prepare_setup(
                    config_path,
                    state_directory=state,
                    store_directory=store.root,
                )

            prepared = PreparedSetup.load(state / "prepared.json")
            self.assertEqual(prepared_path, state / "prepared.json")
            self.assertEqual(prepared.binding, binding.resolve())
            self.assertEqual(prepared.sdk_workspace, sdk_workspace)
            self.assertEqual(prepared.managed_workspace, managed_workspace)
            self.assertEqual(prepared.compiler_kit, artifact_paths.compiler_kit)
            self.assertEqual(prepared.runtime, artifact_paths.runtime)
            self.assertEqual(prepared.integration, "conan")
            self.assertEqual(prepared.smoke_result, smoke_result.resolve())
            self.assertEqual(prepared.conan.build_profile, build_profile)

    def test_prepare_shares_producer_inputs_across_compiler_selections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProducerStore.prepare(root / "store")
            selections = (
                setup_config(
                    integration="shell",
                    target={"arch": "x86_64", "glibc_floor": "2.24"},
                ),
                setup_config(
                    compiler="clang@22",
                    runtime="libc++",
                    integration="shell",
                    target={"arch": "x86_64", "glibc_floor": "2.24"},
                ),
            )
            sdk_calls: list[tuple[Path, object, Path]] = []
            assembly_calls: list[tuple[Path, Path, Path, Path]] = []

            def ensure_sdk(
                _config: SetupConfig,
                workspace: Path,
                *,
                source_cache: Path,
                **kwargs: object,
            ) -> Path:
                (workspace / "sdk").mkdir(parents=True, exist_ok=True)
                sdk_calls.append((workspace, kwargs["goal"], source_cache))
                return workspace

            def assemble(
                _lock: object,
                variant_id: str,
                target_workspace: Path,
                compiler_backend_workspace: Path,
                managed_workspace: Path,
                binding: Path,
                **kwargs: object,
            ) -> AssemblyResult:
                assembly_calls.append(
                    (
                        target_workspace,
                        compiler_backend_workspace,
                        managed_workspace,
                        kwargs["source_cache"],
                    )
                )
                managed_workspace.mkdir(parents=True, exist_ok=True)
                compiler_kit = managed_workspace / "compiler-kit"
                runtime = managed_workspace / "runtime"
                compiler_kit.mkdir(exist_ok=True)
                runtime.mkdir(exist_ok=True)
                for relative in (
                    "binding.json",
                    "audit-policy.json",
                    "env/toolchain.env",
                ):
                    path = binding / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch()
                return AssemblyResult(
                    variant_id=variant_id,
                    compiler_kit=compiler_kit,
                    runtime=runtime,
                    binding_manifest=binding / "binding.json",
                )

            with (
                patch(
                    "linux_toolchain.setup.run_diagnostics",
                    return_value=DiagnosticReport(checks=()),
                ),
                patch(
                    "linux_toolchain.setup._ensure_sdk",
                    side_effect=ensure_sdk,
                ),
                patch("linux_toolchain.setup._sdk_is_ready", return_value=True),
                patch(
                    "linux_toolchain.setup._validate_leased_producer_artifacts",
                    return_value=validated_artifacts(),
                ),
                patch(
                    "linux_toolchain.setup.assemble_variant",
                    side_effect=assemble,
                ),
                patch(
                    "linux_toolchain.setup.run_smoke",
                    side_effect=lambda args: (
                        write_smoke_fixture(
                            Path(args.build_dir) / "result.json",
                            binding=Path(args.binding),
                            integration=args.integration,
                            build_type=args.build_type,
                            glibc_floor=selection["target"]["glibc_floor"],
                        )
                        or 0
                    ),
                ),
            ):
                for index, selection in enumerate(selections):
                    work = root / f"selection-{index}"
                    work.mkdir()
                    config_path = work / "setup.json"
                    write_json(config_path, selection)
                    prepare_setup(
                        config_path,
                        state_directory=work / "state",
                        store_directory=store.root,
                    )

            config = SetupConfig.from_dict(selections[0])
            target_sdk = sdk_spec(config)
            compiler_backend = compiler_backend_spec(config)
            expected_sdk = store.sdk_workspace(target_sdk)
            expected_backend = store.sdk_workspace(compiler_backend)
            expected_managed = store.managed_workspace(
                target_sdk,
                compiler_backend,
            )

            self.assertEqual(
                sdk_calls,
                [
                    (expected_sdk, SDK_BUILD_GOAL, store.sdk_source_cache),
                    (expected_backend, FULL_BUILD_GOAL, store.sdk_source_cache),
                ]
                * 2,
            )
            self.assertEqual(
                assembly_calls,
                [
                    (
                        expected_sdk,
                        expected_backend,
                        expected_managed,
                        store.source_cache,
                    )
                ]
                * 2,
            )

    def test_prepare_reuses_matching_prepared_state_without_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, _, prepared = create_prepared(
                root,
                integration="conan",
                qualified=True,
            )

            with patch("linux_toolchain.setup.run_diagnostics") as diagnostics:
                result = prepare_setup(config_path, state_directory=state)

            self.assertEqual(result, state / "prepared.json")
            diagnostics.assert_not_called()

    def test_prepared_state_remains_valid_when_only_jobs_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, _, prepared = create_prepared(
                root,
                integration="shell",
                qualified=True,
            )
            value = json.loads(config_path.read_text(encoding="utf-8"))
            value["jobs"] = 8
            write_json(config_path, value)

            config, loaded = load_prepared_setup_state(
                config_path,
                state_directory=state,
            )

            self.assertEqual(config.jobs, 8)
            self.assertEqual(loaded, prepared)

    def test_normal_prepare_does_not_reuse_skipped_smoke_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path, state, _, _ = create_prepared(root, qualified=False)

            with (
                patch(
                    "linux_toolchain.setup._diagnose",
                    side_effect=ConfigurationError("diagnostics reached"),
                ) as diagnostics,
                self.assertRaisesRegex(ConfigurationError, "diagnostics reached"),
            ):
                prepare_setup(config_path, state_directory=state)

            diagnostics.assert_called_once()

    def test_invalid_prepared_state_requests_an_explicit_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "setup.json"
            write_json(config_path, setup_config(integration="shell"))
            state = root / "state"
            state.mkdir()
            (state / ".linux-toolchain-setup-state").write_text(
                "format=1\n", encoding="utf-8"
            )
            write_json(
                state / "prepared.json",
                {
                    "schema": "linux-toolchain-prepared-setup",
                    "format": 1,
                },
            )

            with self.assertRaisesRegex(
                ConfigurationError,
                r"rerun setup with --force or use a new --work-dir",
            ):
                prepare_setup(config_path, state_directory=state)

    def test_prepared_state_rejects_a_binding_outside_the_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "setup.json"
            config_path.write_text(json.dumps(setup_config()), encoding="utf-8")
            config = SetupConfig.load(config_path)
            state = root / "state"
            state.mkdir()
            (state / ".linux-toolchain-setup-state").write_text(
                "format=1\n", encoding="utf-8"
            )
            outside = root / "outside-binding"
            prepared = PreparedSetup(
                config_sha256=config.selection_sha256,
                binding=outside,
                lock=state / "managed.lock.json",
                variant="variant",
                sdk_workspace=state / "sdk",
                managed_workspace=state / "managed",
                compiler_kit=state / "managed/compiler-kit",
                runtime=state / "managed/runtime",
                integration="conan",
                smoke_result=None,
                conan=None,
            )
            write_json(state / "prepared.json", prepared.to_dict())

            with self.assertRaisesRegex(
                ConfigurationError, "binding does not match its state directory"
            ):
                prepare_setup(config_path, state_directory=state)

    def test_prepared_state_requires_an_integer_format(self) -> None:
        value = {
            "schema": "linux-toolchain-prepared-setup",
            "format": 1.0,
            "config_sha256": "0" * 64,
            "binding": "/tmp/binding",
            "lock": "/tmp/lock",
            "variant": "variant",
            "sdk_workspace": "/tmp/sdk",
            "managed_workspace": "/tmp/managed",
            "compiler_kit": "/tmp/managed/compiler-kit",
            "runtime": "/tmp/managed/runtime",
            "integration": "shell",
            "smoke_result": None,
            "conan": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "prepared.json"
            state.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unsupported.*format"):
                PreparedSetup.load(state)


if __name__ == "__main__":
    unittest.main()
