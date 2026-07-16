import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from linux_toolchain.build_tools import (
    BUILD_TOOLS_FORMAT,
    BUILD_TOOLS_SCHEMA,
    CCACHE_VERSION,
    DEFAULT_CMAKE_VERSION,
    BuildToolsSpec,
    build_tools_producer_identity,
    build_tools_script,
    build_tools_sources,
    expected_build_tool_records,
    load_build_tools,
)
from linux_toolchain.build_tools_builder import build_build_tools
from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.licenses import license_evidence
from linux_toolchain.recipes import get_recipe
from linux_toolchain.source_archive import (
    PinnedArchive,
    download_pinned_archive,
    publish_archive_file,
    validate_tar_archive,
)

_LICENSE_PATHS = (
    "cmake/Copyright.txt",
    "openssl/LICENSE.txt",
    "make/COPYING",
    "ninja/COPYING",
    "ccache/LICENSE.md",
    "ccache/GPL-3.0.txt",
)


def _write_build_tools_fixture(
    root: Path, arch: str
) -> tuple[dict[str, object], dict[str, object]]:
    spec = BuildToolsSpec(arch=arch, glibc_floor="2.19")
    backend = get_recipe(arch, "2.19").to_spec(name=f"build-tools-{arch}")
    identity = build_tools_producer_identity(spec, backend)
    records = expected_build_tool_records(spec)
    for record in records.values():
        executable = root / str(record["path"])
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("test executable\n", encoding="utf-8")
        executable.chmod(0o755)
    for relative in _LICENSE_PATHS:
        license_file = root / "licenses" / relative
        license_file.parent.mkdir(parents=True, exist_ok=True)
        license_file.write_text(f"{relative}\n", encoding="utf-8")
    audit: dict[str, object] = {
        "audited_elf_files": len(records),
        "max_required_glibc": "2.19",
    }
    manifest: dict[str, object] = {
        "schema": BUILD_TOOLS_SCHEMA,
        "format": BUILD_TOOLS_FORMAT,
        "identity": identity,
        "tools": records,
        "builder_image": {
            "id": f"sha256:{'a' * 64}",
            "os": "linux",
            "architecture": "amd64" if arch == "x86_64" else "arm64",
            "repo_digests": [],
        },
        "elf_audit": audit,
        "licenses": license_evidence(root, context="build tools fixture"),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    return manifest, audit


class BuildToolsTest(unittest.TestCase):
    def test_sources_and_build_script_are_architecture_specific(self) -> None:
        expectations = (
            ("x86_64", "linux-x86_64"),
            ("aarch64", "linux-aarch64"),
        )
        for arch, openssl_target in expectations:
            with self.subTest(arch=arch):
                spec = BuildToolsSpec(
                    arch=arch,
                    glibc_floor="2.19",
                )
                backend = get_recipe(arch, "2.19").to_spec(
                    name=f"build-tools-{arch}",
                )
                sources = build_tools_sources(spec)
                script = build_tools_script(spec, backend.target.triplet)

                self.assertEqual(
                    sources["ccache"].filename,
                    f"ccache-{CCACHE_VERSION}-linux-{arch}-musl-static.tar.xz",
                )
                self.assertIn(f"readonly OPENSSL_TARGET={openssl_target}", script)
                self.assertIn(backend.target.triplet, script)
                self.assertIn("-static-libstdc++ -static-libgcc", script)
                self.assertIn('export CFLAGS="-O2 -g0"', script)
                self.assertIn("no-module no-shared no-tests", script)
                self.assertIn('make -j"$JOBS" build_libs', script)
                self.assertIn("make install_dev", script)
                self.assertNotIn("no-apps", script)
                self.assertNotIn("no-docs", script)
                self.assertNotIn("make install_sw", script)
                self.assertNotIn("-march=native", script)
                syntax = subprocess.run(
                    ["bash", "-n"],
                    input=script,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(syntax.returncode, 0, syntax.stderr)
                self.assertEqual(
                    build_tools_producer_identity(spec, backend)["selection"],
                    {
                        "arch": arch,
                        "glibc_floor": "2.19",
                        "cmake_version": DEFAULT_CMAKE_VERSION,
                    },
                )
                self.assertEqual(
                    sources["make"].source_urls,
                    (
                        "https://mirrors.kernel.org/gnu/make/make-4.4.1.tar.gz",
                        "https://ftpmirror.gnu.org/make/make-4.4.1.tar.gz",
                        "https://ftp.gnu.org/gnu/make/make-4.4.1.tar.gz",
                    ),
                )

        self.assertNotEqual(
            build_tools_sources(BuildToolsSpec(arch="x86_64", glibc_floor="2.19"))[
                "ccache"
            ].sha256,
            build_tools_sources(BuildToolsSpec(arch="aarch64", glibc_floor="2.19"))[
                "ccache"
            ].sha256,
        )

    def test_inventory_includes_build_drivers_without_enabling_ccache(self) -> None:
        records = expected_build_tool_records(
            BuildToolsSpec(arch="x86_64", glibc_floor="2.19")
        )

        self.assertEqual(
            tuple(records),
            ("cmake", "ctest", "cpack", "make", "ninja", "ccache"),
        )
        self.assertEqual(records["cmake"]["version"], DEFAULT_CMAKE_VERSION)
        self.assertEqual(records["ctest"]["version"], DEFAULT_CMAKE_VERSION)
        self.assertEqual(records["cpack"]["version"], DEFAULT_CMAKE_VERSION)
        self.assertEqual(records["ccache"]["linkage"], "static-musl")
        self.assertFalse(records["ccache"]["enabled_by_default"])
        self.assertTrue(records["ninja"]["enabled_by_default"])

    def test_selection_rejects_unsupported_platforms_and_cmake_versions(self) -> None:
        invalid = (
            BuildToolsSpec(arch="riscv64", glibc_floor="2.19"),
            BuildToolsSpec(arch="aarch64", glibc_floor="2.16"),
            BuildToolsSpec(
                arch="x86_64",
                glibc_floor="2.19",
                cmake_version="3.31.9",
            ),
        )
        for spec in invalid:
            with self.subTest(spec=spec), self.assertRaises(ConfigurationError):
                spec.validate()

    def test_manifest_loader_validates_both_architectures(self) -> None:
        for arch in ("x86_64", "aarch64"):
            with self.subTest(arch=arch), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, audit = _write_build_tools_fixture(root, arch)
                static = SimpleNamespace(interpreter=None, needed=())
                with (
                    patch(
                        "linux_toolchain.build_tools.audit_host_artifact",
                        return_value=audit,
                    ),
                    patch("linux_toolchain.build_tools.ReadElfInspector") as inspector,
                ):
                    inspector.return_value.inspect.return_value = static
                    artifact = load_build_tools(root)

                self.assertEqual(artifact.spec.arch, arch)
                self.assertEqual(artifact.tools["cmake"]["path"], "bin/cmake")

    def test_manifest_loader_rejects_unknown_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, _ = _write_build_tools_fixture(root, "x86_64")
            identity = dict(manifest["identity"])  # type: ignore[arg-type]
            identity["unexpected"] = True
            manifest["identity"] = identity
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigurationError, "unknown keys"):
                load_build_tools(root)

    def test_builder_reuses_a_valid_artifact_when_force_is_enabled(self) -> None:
        spec = BuildToolsSpec(arch="x86_64", glibc_floor="2.19")
        backend = get_recipe("x86_64", "2.19").to_spec(name="build-tools")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "artifact").mkdir()
            existing = SimpleNamespace(root=workspace / "artifact")
            with (
                patch("linux_toolchain.build_tools_builder._validate_compiler_backend"),
                patch(
                    "linux_toolchain.build_tools_builder._prepare_workspace",
                    return_value=workspace,
                ),
                patch(
                    "linux_toolchain.build_tools_builder.load_build_tools",
                    return_value=existing,
                ),
                patch("linux_toolchain.build_tools_builder._preflight") as preflight,
            ):
                result = build_build_tools(
                    spec,
                    backend,
                    workspace / "compiler-backend",
                    workspace,
                    source_cache=workspace / "sources",
                    force=True,
                )

            self.assertIs(result, existing)
            preflight.assert_not_called()

    def test_builder_heartbeat_reports_recent_log_output(self) -> None:
        spec = BuildToolsSpec(arch="x86_64", glibc_floor="2.19")
        backend = get_recipe("x86_64", "2.19").to_spec(name="build-tools")
        updates: list[str] = []
        source_progress = Mock()

        def report_then_stop(
            _command: object,
            log: Path,
            **options: object,
        ) -> None:
            log.write_text(
                "old output\nconfiguring\nbuilding\nlinking\n",
                encoding="utf-8",
            )
            heartbeat = options["heartbeat"]
            assert callable(heartbeat)
            heartbeat(2.9)
            raise RuntimeError("stop after heartbeat")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("linux_toolchain.build_tools_builder._validate_compiler_backend"),
                patch(
                    "linux_toolchain.build_tools_builder._preflight",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "linux_toolchain.build_tools_builder._download_sources",
                    return_value={},
                ),
                patch(
                    "linux_toolchain.build_tools_builder.acquire_workspace_builder_image",
                    return_value=SimpleNamespace(),
                ) as acquire_image,
                patch(
                    "linux_toolchain.build_tools_builder._write_identity",
                    return_value=SimpleNamespace(),
                ),
                patch(
                    "linux_toolchain.build_tools_builder._docker_run_command",
                    return_value=["docker", "run"],
                ),
                patch(
                    "linux_toolchain.build_tools_builder.temporary_container_owner",
                    return_value=root / "container-owner.json",
                ),
                patch(
                    "linux_toolchain.build_tools_builder.temporary_container_run",
                    return_value=nullcontext((["docker", "run"], lambda: None)),
                ),
                patch(
                    "linux_toolchain.build_tools_builder.run_logged",
                    side_effect=report_then_stop,
                ) as logged,
                self.assertRaisesRegex(RuntimeError, "stop after heartbeat"),
            ):
                build_build_tools(
                    spec,
                    backend,
                    root / "compiler-backend",
                    root / "workspace",
                    source_cache=root / "sources",
                    jobs=7,
                    progress=updates.append,
                    source_progress=source_progress,
                )

        self.assertEqual(
            updates[-1],
            "build tools: building CMake and native tools; elapsed: 2s\n"
            "configuring\nbuilding\nlinking",
        )
        self.assertEqual(logged.call_args.kwargs["heartbeat_interval"], 1.0)
        self.assertEqual(acquire_image.call_args.kwargs["jobs"], 7)
        self.assertIs(
            acquire_image.call_args.kwargs["source_progress"],
            source_progress,
        )

    def test_manifest_loader_rejects_escaping_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "artifact"
            root.mkdir()
            _write_build_tools_fixture(root, "x86_64")
            outside = Path(directory) / "outside"
            outside.write_text("host input\n", encoding="utf-8")
            (root / "host-input").symlink_to("../outside")

            with self.assertRaisesRegex(ConfigurationError, "escapes or dangles"):
                load_build_tools(root)

    def test_source_archive_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            with tarfile.open(archive, mode="w:gz") as output:
                member = tarfile.TarInfo("../escape")
                content = b"escape\n"
                member.size = len(content)
                output.addfile(member, io.BytesIO(content))

            with self.assertRaises(ExternalToolError):
                validate_tar_archive(
                    archive,
                    top_directory="source",
                    context="test source archive",
                )

    def test_source_archive_allows_contained_relative_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            with tarfile.open(archive, mode="w:gz") as output:
                target = tarfile.TarInfo("source/lib/tool")
                content = b"tool\n"
                target.size = len(content)
                output.addfile(target, io.BytesIO(content))
                link = tarfile.TarInfo("source/bin/tool")
                link.type = tarfile.SYMTYPE
                link.linkname = "../lib/tool"
                output.addfile(link)

            validate_tar_archive(
                archive,
                top_directory="source",
                context="test source archive",
            )

    def test_source_archive_publication_preserves_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tar.gz"
            destination = root / "cache" / "source.tar.gz"
            source.write_bytes(b"archive\n")
            source.chmod(0o600)

            publish_archive_file(source, destination)

            self.assertEqual(source.stat().st_mode & 0o777, 0o600)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertEqual(destination.read_bytes(), b"archive\n")

    def test_pinned_archive_uses_ordered_mirrors_with_the_same_digest(self) -> None:
        payload = b"verified source archive\n"
        archive = PinnedArchive(
            filename="source.tar.gz",
            source_url="https://primary.example/source.tar.gz",
            sha256=hashlib.sha256(payload).hexdigest(),
            mirrors=("https://mirror.example/source.tar.gz",),
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "linux_toolchain.source_archive.urllib.request.urlopen",
                side_effect=(
                    urllib.error.URLError("primary unavailable"),
                    io.BytesIO(payload),
                ),
            ) as urlopen:
                result = download_pinned_archive(
                    archive,
                    Path(directory),
                    description="test source",
                )

            self.assertEqual(result.read_bytes(), payload)
            self.assertEqual(
                tuple(call.args[0].full_url for call in urlopen.call_args_list),
                archive.source_urls,
            )


if __name__ == "__main__":
    unittest.main()
