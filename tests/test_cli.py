import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linux_toolchain.cli import main
from linux_toolchain.diagnostics import DiagnosticCheck, DiagnosticReport
from linux_toolchain.errors import ConfigurationError
from linux_toolchain.integrations import ConanSettings
from linux_toolchain.managed.assemble import AssemblyResult


class CliRoutingTest(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_sdk_catalog_json_contract(self) -> None:
        status, stdout, _ = self.invoke(["sdk", "list", "--json"])

        self.assertEqual(status, 0)
        document = json.loads(stdout)
        self.assertEqual(document["schema"], "linux-toolchain-sdk-catalog")
        self.assertEqual(document["format"], 1)
        self.assertTrue(document["recipes"])
        self.assertIn("crosstool-ng", document["recipes"][0])

    def test_setup_routes_the_selected_toolchain(self) -> None:
        launcher = Path("/opt/toolchain/bin/lxtc")
        with patch(
            "linux_toolchain.cli.setup_toolchain", return_value=launcher
        ) as producer:
            status, _, _ = self.invoke(
                [
                    "setup",
                    "gcc@12",
                    "--glibc",
                    "2.19",
                    "--prefix",
                    "/opt/toolchain",
                    "--store-dir",
                    "/work/store",
                    "--no-path-instructions",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(producer.call_args.args, ("gcc@12",))
        options = producer.call_args.kwargs
        self.assertEqual(options["glibc_floor"], "2.19")
        self.assertIsNone(options["host_glibc_floor"])
        self.assertEqual(options["prefix"], Path("/opt/toolchain"))
        self.assertEqual(options["store_dir"], Path("/work/store"))
        self.assertTrue(options["install"])

    def test_bundle_create_routes_prepared_state(self) -> None:
        installer = Path("/release/toolchain.run")
        with patch(
            "linux_toolchain.cli.create_prepared_bundle",
            return_value=installer,
        ) as creator:
            status, _, _ = self.invoke(
                [
                    "bundle",
                    "create",
                    "--config",
                    "/work/setup.json",
                    "--state-directory",
                    "/work/state",
                    "--output",
                    str(installer),
                ]
            )

        self.assertEqual(status, 0)
        options = creator.call_args.kwargs
        self.assertEqual(options["config"], Path("/work/setup.json"))
        self.assertEqual(options["state_directory"], Path("/work/state"))
        self.assertEqual(options["output"], installer)

    def test_artifact_bundle_defaults_to_every_integration(self) -> None:
        installer = Path("/release/toolchain.run")
        with (
            patch("linux_toolchain.cli._load_managed_lock", return_value=object()),
            patch(
                "linux_toolchain.cli.create_bundle",
                return_value=installer,
            ) as creator,
        ):
            status, _, _ = self.invoke(
                [
                    "bundle",
                    "create-artifacts",
                    "--sdk",
                    "/artifacts/sdk",
                    "--compiler-kit",
                    "/artifacts/compiler-kit",
                    "--runtime",
                    "/artifacts/runtime",
                    "--lock",
                    "/artifacts/managed.lock.json",
                    "--variant",
                    "variant",
                    "--output",
                    str(installer),
                ]
            )

        self.assertEqual(status, 0)
        options = creator.call_args.kwargs
        self.assertEqual(options["integrations"], ("cmake", "shell", "conan"))
        self.assertEqual(options["conan"], ConanSettings())

    def test_doctor_json_contract_and_exit_status(self) -> None:
        passing = DiagnosticReport(
            checks=(), workflow="consumer", integrations=("conan",)
        )
        with patch(
            "linux_toolchain.cli.run_diagnostics", return_value=passing
        ) as diagnostics:
            status, stdout, _ = self.invoke(
                [
                    "doctor",
                    "--workflow",
                    "consumer",
                    "--integration",
                    "conan",
                    "--json",
                ]
            )

        self.assertEqual(status, 0)
        diagnostics.assert_called_once_with("consumer", ["conan"])
        document = json.loads(stdout)
        self.assertEqual(document["schema"], "linux-toolchain-doctor")
        self.assertEqual(document["format"], 1)
        self.assertTrue(document["passed"])

        failing = DiagnosticReport(
            checks=(DiagnosticCheck("docker", "required", "fail", "missing"),)
        )
        with patch("linux_toolchain.cli.run_diagnostics", return_value=failing):
            status, stdout, _ = self.invoke(["doctor", "--json"])

        self.assertEqual(status, 1)
        self.assertFalse(json.loads(stdout)["passed"])

    def test_external_binding_routes_compiler_and_runtime_policy(self) -> None:
        compiler = object()
        manifest = Path("/binding/binding.json")
        with (
            patch(
                "linux_toolchain.cli.detect_compiler", return_value=compiler
            ) as detector,
            patch(
                "linux_toolchain.cli.create_binding", return_value=manifest
            ) as creator,
        ):
            status, _, _ = self.invoke(
                [
                    "bind",
                    "external",
                    "--sdk",
                    "/sdk",
                    "--cc",
                    "gcc",
                    "--cxx",
                    "g++",
                    "--allow-unpinned-runtime",
                    "--output",
                    "/binding",
                ]
            )

        self.assertEqual(status, 0)
        detector.assert_called_once_with("gcc", "g++")
        self.assertEqual(
            creator.call_args.args, (Path("/sdk"), Path("/binding"), compiler)
        )
        self.assertIsNone(creator.call_args.kwargs["runtime"])

    def test_managed_binding_routes_pinned_components(self) -> None:
        lock = object()
        manifest = Path("/binding/binding.json")
        with (
            patch(
                "linux_toolchain.cli._load_managed_lock", return_value=lock
            ) as loader,
            patch(
                "linux_toolchain.cli.create_managed_binding", return_value=manifest
            ) as creator,
        ):
            status, _, _ = self.invoke(
                [
                    "bind",
                    "managed",
                    "--sdk",
                    "/sdk",
                    "--compiler-kit",
                    "/compiler-kit",
                    "--lock",
                    "/managed.lock.json",
                    "--variant",
                    "toolchain-clang-22",
                    "--runtime",
                    "/runtime",
                    "--output",
                    "/binding",
                ]
            )

        self.assertEqual(status, 0)
        loader.assert_called_once_with(Path("/managed.lock.json"))
        self.assertEqual(
            creator.call_args.args,
            (Path("/sdk"), Path("/binding"), Path("/compiler-kit")),
        )
        self.assertIs(creator.call_args.kwargs["lock"], lock)
        self.assertEqual(creator.call_args.kwargs["variant"], "toolchain-clang-22")
        self.assertEqual(creator.call_args.kwargs["runtime"], Path("/runtime"))

    def test_sdk_create_routes_render_build_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            dockerfile = Path(directory) / "builder.Dockerfile"
            dockerfile.touch()
            spec = object()
            exported = workspace / "sdk"
            with (
                patch("linux_toolchain.cli._sdk_spec", return_value=spec),
                patch("linux_toolchain.cli.render_workspace") as renderer,
                patch("linux_toolchain.cli.build_with_docker") as builder,
                patch(
                    "linux_toolchain.cli.export_sdk", return_value=exported
                ) as exporter,
            ):
                status, _, _ = self.invoke(
                    [
                        "sdk",
                        "create",
                        "--glibc",
                        "2.19",
                        "--arch",
                        "x86_64",
                        "--workspace",
                        str(workspace),
                        "--dockerfile",
                        str(dockerfile),
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(renderer.call_args.args, (spec, workspace.resolve()))
        self.assertEqual(builder.call_args.args, (spec, workspace.resolve()))
        self.assertEqual(builder.call_args.kwargs["dockerfile"], dockerfile.resolve())
        exporter.assert_called_once_with(spec, workspace.resolve())

    def test_managed_assemble_json_contract_and_route(self) -> None:
        lock = object()
        assembly = AssemblyResult(
            variant_id="toolchain-gcc-13",
            compiler_kit=Path("/work/compiler"),
            runtime=Path("/work/runtime"),
            binding_manifest=Path("/out/binding/binding.json"),
        )
        with (
            patch("linux_toolchain.cli._load_managed_lock", return_value=lock),
            patch(
                "linux_toolchain.cli.assemble_variant", return_value=assembly
            ) as assembler,
        ):
            status, stdout, _ = self.invoke(
                [
                    "managed",
                    "assemble",
                    "--lock",
                    "/work/managed.lock.json",
                    "--variant",
                    "toolchain-gcc-13",
                    "--sdk-workspace",
                    "/work/sdk",
                    "--compiler-backend-workspace",
                    "/work/compiler-backend",
                    "--workspace",
                    "/work/managed",
                    "--output",
                    "/out/binding",
                    "--json",
                ]
            )

        self.assertEqual(status, 0)
        document = json.loads(stdout)
        self.assertEqual(document["schema"], "linux-toolchain-managed-assembly")
        self.assertEqual(document["format"], 1)
        self.assertEqual(
            assembler.call_args.args,
            (
                lock,
                "toolchain-gcc-13",
                Path("/work/sdk"),
                Path("/work/compiler-backend"),
                Path("/work/managed"),
                Path("/out/binding"),
            ),
        )

    def test_smoke_routes_to_packaged_runner(self) -> None:
        with patch("linux_toolchain.cli.run_smoke", return_value=0) as runner:
            status, _, _ = self.invoke(
                ["smoke", "--binding", "/binding", "--build-dir", "/build"]
            )

        self.assertEqual(status, 0)
        self.assertEqual(runner.call_args.args[0].binding, Path("/binding"))

    def test_gcc_runtime_import_routes_source_and_probe(self) -> None:
        manifest = Path("/runtime/manifest.json")
        with patch(
            "linux_toolchain.runtime.import_gcc_runtime", return_value=manifest
        ) as importer:
            status, _, _ = self.invoke(
                [
                    "runtime",
                    "import-gcc",
                    "--prefix",
                    "/prefix",
                    "--probe-gxx",
                    "/build/xg++",
                    "--glibc-floor",
                    "2.19",
                    "--arch",
                    "x86_64",
                    "--output",
                    "/runtime",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            importer.call_args.args,
            (Path("/prefix"), "2.19", "x86_64", Path("/runtime")),
        )
        self.assertEqual(importer.call_args.kwargs["probe_gxx"], Path("/build/xg++"))

    def test_llvm_runtime_import_routes_source_proof(self) -> None:
        manifest = Path("/runtime/manifest.json")
        evidence = object()
        with (
            patch(
                "linux_toolchain.managed.publication._load_managed_llvm_source_evidence",
                return_value=evidence,
            ) as loader,
            patch(
                "linux_toolchain.runtime.import_llvm_runtime", return_value=manifest
            ) as importer,
        ):
            status, _, _ = self.invoke(
                [
                    "runtime",
                    "import-llvm",
                    "--prefix",
                    "/prefix",
                    "--llvm-version",
                    "22.1.8",
                    "--glibc-floor",
                    "2.24",
                    "--arch",
                    "x86_64",
                    "--target",
                    "x86_64-portable-linux-gnu",
                    "--provenance",
                    "/artifact.json",
                    "--output",
                    "/runtime",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            importer.call_args.args,
            (
                Path("/prefix"),
                "22.1.8",
                "2.24",
                "x86_64",
                "x86_64-portable-linux-gnu",
                Path("/runtime"),
            ),
        )
        self.assertEqual(
            loader.call_args.args,
            (Path("/artifact.json"), Path("/prefix")),
        )
        self.assertEqual(importer.call_args.kwargs["source_evidence"], evidence)

    def test_managed_catalog_and_lock_routes(self) -> None:
        status, stdout, _ = self.invoke(
            ["managed", "catalog", "--family", "clang", "--json"]
        )
        self.assertEqual(status, 0)
        document = json.loads(stdout)
        self.assertEqual(document["schema"], "linux-toolchain-managed-release-index")
        self.assertEqual(document["format"], 1)
        self.assertTrue(all(row["family"] == "clang" for row in document["releases"]))

        spec = object()
        lock = object()
        lockfile = Path("/work/managed.lock.json")
        with (
            patch("linux_toolchain.cli.ManagedSpec.load", return_value=spec) as loader,
            patch("linux_toolchain.cli.resolve_lock", return_value=lock) as resolver,
            patch(
                "linux_toolchain.cli.write_lockfile", return_value=lockfile
            ) as writer,
        ):
            status, _, _ = self.invoke(
                [
                    "managed",
                    "lock",
                    "--spec",
                    "/work/managed.json",
                    "--output",
                    str(lockfile),
                ]
            )

        self.assertEqual(status, 0)
        loader.assert_called_once_with(Path("/work/managed.json"))
        resolver.assert_called_once_with(spec)
        writer.assert_called_once_with(lock, lockfile, force=False)

    def test_managed_fetch_and_build_route_artifact_workspace(self) -> None:
        lock = object()
        lockfile = Path("/work/managed.lock.json")
        workspace = Path("/work/artifact")
        with (
            patch("linux_toolchain.cli._load_managed_lock", return_value=lock),
            patch(
                "linux_toolchain.cli.fetch_managed_source",
                return_value=workspace / "sources/gcc.tar.xz",
            ) as fetcher,
        ):
            status, _, _ = self.invoke(
                [
                    "managed",
                    "fetch",
                    "--lock",
                    str(lockfile),
                    "--artifact",
                    "runtime-gcc-13",
                    "--workspace",
                    str(workspace),
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            fetcher.call_args.args,
            (lock, "runtime-gcc-13", workspace.resolve()),
        )

        with (
            patch("linux_toolchain.cli._load_managed_lock", return_value=lock),
            patch(
                "linux_toolchain.cli.build_managed_with_docker",
                return_value=workspace / "output/artifact.json",
            ) as builder,
        ):
            status, _, _ = self.invoke(
                [
                    "managed",
                    "build",
                    "--lock",
                    str(lockfile),
                    "--artifact",
                    "runtime-gcc-13",
                    "--workspace",
                    str(workspace),
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            builder.call_args.args,
            (lock, "runtime-gcc-13", workspace.resolve()),
        )

    def test_domain_error_is_clean_and_returns_two(self) -> None:
        with (
            patch("linux_toolchain.cli._load_managed_lock", return_value=object()),
            patch(
                "linux_toolchain.cli.fetch_managed_source",
                side_effect=ConfigurationError("unknown managed artifact: missing"),
            ),
        ):
            status, stdout, stderr = self.invoke(
                [
                    "managed",
                    "fetch",
                    "--lock",
                    "/work/managed.lock.json",
                    "--artifact",
                    "missing",
                    "--workspace",
                    "/work/artifact",
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("missing", stderr)
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
