from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from linux_toolchain.compiler.managed import (
    COMPILER_KIT_MANIFEST_FORMAT,
    COMPILER_KIT_MANIFEST_SCHEMA,
    TARGET_TOOL_NAMES,
    load_compiler_kit,
)
from linux_toolchain.errors import ConfigurationError


class CompilerKitTest(unittest.TestCase):
    @staticmethod
    def executable(path: Path, text: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or f"#!/bin/sh\n# {path.name}\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def create_kit(
        self,
        root: Path,
        *,
        provider: str = "gcc",
        version: str | None = None,
        host_arch: str = "x86_64",
        target_arch: str = "x86_64",
    ) -> tuple[Path, dict[str, object]]:
        version = version or ("13.4.0" if provider == "gcc" else "22.1.0")
        kit = root / "kit"
        payload = kit / "compiler"
        target = f"{target_arch}-portable-linux-gnu"
        cc_name = f"{target}-gcc" if provider == "gcc" else "clang"
        cxx_name = f"{target}-g++" if provider == "gcc" else "clang++"
        cc = self.executable(payload / "bin" / cc_name)
        cxx = self.executable(payload / "bin" / cxx_name)
        tools = {
            name: self.executable(payload / "bin" / f"{target}-{name}")
            for name in TARGET_TOOL_NAMES
        }
        manifest: dict[str, object] = {
            "schema": COMPILER_KIT_MANIFEST_SCHEMA,
            "format": COMPILER_KIT_MANIFEST_FORMAT,
            "provider": {
                "name": provider,
                "version": version,
                "major": int(version.split(".", 1)[0]),
            },
            "host": {
                "os": "linux",
                "arch": host_arch,
                "glibc_floor": "2.17",
            },
            "target": {"arch": target_arch, "triplet": target},
            "locations": {
                "cc": cc.relative_to(kit).as_posix(),
                "cxx": cxx.relative_to(kit).as_posix(),
                "target_tools": {
                    name: path.relative_to(kit).as_posix()
                    for name, path in tools.items()
                },
            },
        }
        self.write_manifest(kit, manifest)
        return kit, manifest

    @staticmethod
    def write_manifest(kit: Path, manifest: dict[str, object]) -> None:
        kit.mkdir(parents=True, exist_ok=True)
        (kit / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_loads_gcc_and_clang_kit_entrypoints(self) -> None:
        for provider, version in (("gcc", "10.5.0"), ("clang", "22.1.0")):
            with (
                self.subTest(provider=provider),
                tempfile.TemporaryDirectory() as directory,
            ):
                kit, _ = self.create_kit(
                    Path(directory), provider=provider, version=version
                )
                loaded = load_compiler_kit(kit, check_host=False)

                self.assertEqual(loaded.root, kit.resolve())
                self.assertEqual(loaded.manifest.provider["name"], provider)
                self.assertEqual(loaded.cc.relative_path.split("/", 1)[0], "compiler")
                self.assertEqual(loaded.cc.invocation_path.parent, kit / "compiler/bin")
                self.assertEqual(tuple(loaded.target_tools), TARGET_TOOL_NAMES)
                self.assertIsInstance(loaded.target_tools, MappingProxyType)

    def test_rejects_unknown_missing_schema_and_format_fields(self) -> None:
        cases = (
            ("unknown keys", lambda value: value.update({"extra": True})),
            ("is missing", lambda value: value.pop("target")),
            ("schema", lambda value: value.update({"schema": "other"})),
            ("format", lambda value: value.update({"format": 2})),
            ("format", lambda value: value.update({"format": True})),
        )
        for expected, mutate in cases:
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as directory,
            ):
                kit, manifest = self.create_kit(Path(directory))
                mutate(manifest)
                self.write_manifest(kit, manifest)
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_compiler_kit(kit, check_host=False)

    def test_rejects_invalid_provider_and_version_contracts(self) -> None:
        cases = (
            ("must be gcc or clang", {"name": "zig", "version": "13.0", "major": 13}),
            (
                "invalid numeric version",
                {"name": "gcc", "version": "latest", "major": 13},
            ),
            ("inconsistent", {"name": "gcc", "version": "13.4.0", "major": 12}),
            (
                "unsupported managed gcc",
                {"name": "gcc", "version": "9.5.0", "major": 9},
            ),
            (
                "unsupported managed clang",
                {"name": "clang", "version": "15.0.7", "major": 15},
            ),
        )
        for expected, provider in cases:
            with (
                self.subTest(provider=provider),
                tempfile.TemporaryDirectory() as directory,
            ):
                kit, manifest = self.create_kit(Path(directory))
                manifest["provider"] = provider
                self.write_manifest(kit, manifest)
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_compiler_kit(kit, check_host=False)

    def test_rejects_invalid_host_contract_and_current_host_mismatch(self) -> None:
        structural_cases = (
            (
                "os must be linux",
                {"os": "darwin", "arch": "x86_64", "glibc_floor": "2.17"},
            ),
            ("arch must be", {"os": "linux", "arch": "riscv64", "glibc_floor": "2.17"}),
            (
                "invalid numeric version",
                {"os": "linux", "arch": "x86_64", "glibc_floor": "old"},
            ),
        )
        for expected, host in structural_cases:
            with self.subTest(host=host), tempfile.TemporaryDirectory() as directory:
                kit, manifest = self.create_kit(Path(directory))
                manifest["host"] = host
                self.write_manifest(kit, manifest)
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_compiler_kit(kit, check_host=False)

        with tempfile.TemporaryDirectory() as directory:
            kit, _ = self.create_kit(Path(directory), host_arch="aarch64")
            with (
                patch(
                    "linux_toolchain.compiler.managed._current_host",
                    return_value=("linux", "x86_64", "2.36"),
                ),
                self.assertRaisesRegex(ConfigurationError, "build host mismatch"),
            ):
                load_compiler_kit(kit)

        with tempfile.TemporaryDirectory() as directory:
            kit, manifest = self.create_kit(Path(directory))
            host = manifest["host"]
            assert isinstance(host, dict)
            host["glibc_floor"] = "2.36"
            self.write_manifest(kit, manifest)
            with (
                patch(
                    "linux_toolchain.compiler.managed._current_host",
                    return_value=("linux", "x86_64", "2.28"),
                ),
                self.assertRaisesRegex(ConfigurationError, "glibc is too old"),
            ):
                load_compiler_kit(kit)

    def test_rejects_invalid_target_architecture_and_triplet(self) -> None:
        cases = (
            ("arch must be", {"arch": "armv7", "triplet": "arm-linux-gnueabihf"}),
            (
                "incompatible",
                {"arch": "aarch64", "triplet": "x86_64-portable-linux-gnu"},
            ),
            (
                "incompatible",
                {"arch": "x86_64", "triplet": "x86_64-portable-linux-musl"},
            ),
            ("invalid characters", {"arch": "x86_64", "triplet": "x86_64 linux gnu"}),
        )
        for expected, target in cases:
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as directory,
            ):
                kit, manifest = self.create_kit(Path(directory))
                manifest["target"] = target
                self.write_manifest(kit, manifest)
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_compiler_kit(kit, check_host=False)

    def test_rejects_invalid_or_non_payload_entrypoint_paths(self) -> None:
        cases = (
            ("normalized relative path", "/usr/bin/gcc"),
            ("normalized relative path", "compiler/../outside/gcc"),
            ("normalized relative path", "compiler/bin/./gcc"),
            ("POSIX path separators", "compiler\\bin\\gcc"),
            ("below compiler/", "tools/gcc"),
            ("identify a payload entry", "compiler"),
        )
        for expected, path in cases:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                kit, manifest = self.create_kit(Path(directory))
                locations = manifest["locations"]
                assert isinstance(locations, dict)
                locations["cc"] = path
                self.write_manifest(kit, manifest)
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_compiler_kit(kit, check_host=False)

    def test_rejects_missing_and_non_executable_tools(self) -> None:
        for mutation, expected in (
            ("missing", "does not exist"),
            ("mode", "not an executable"),
            ("escape", "escapes compiler/"),
        ):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                kit, manifest = self.create_kit(Path(directory))
                locations = manifest["locations"]
                assert isinstance(locations, dict)
                tools = locations["target_tools"]
                assert isinstance(tools, dict)
                tool = kit / str(tools["strip"])
                if mutation == "missing":
                    tool.unlink()
                elif mutation == "mode":
                    tool.chmod(0o644)
                else:
                    tool.unlink()
                    outside = kit / "outside-strip"
                    outside.write_text("#!/bin/sh\n", encoding="utf-8")
                    outside.chmod(0o755)
                    tool.symlink_to(outside)
                with self.assertRaisesRegex(ConfigurationError, expected):
                    load_compiler_kit(kit, check_host=False)


if __name__ == "__main__":
    unittest.main()
