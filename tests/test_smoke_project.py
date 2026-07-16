# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from linux_toolchain._runtime_runner import main as runtime_runner_main
from linux_toolchain.smoke import (
    RuntimeContext,
    SmokeFailure,
    _capture,
    _kernel_loader_command,
    _prepare_build_directory,
    _requires_kernel_loader_start,
    build_commands,
    parse_args,
    require_pinned_runtime,
    run,
    verify_loader_closure,
)


class SmokeProjectTest(unittest.TestCase):
    def test_runtime_runner_sets_sdk_library_path_only_for_target_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader = root / "ld.so"
            interpreter = root / "interpreter"
            program = root / "program"
            for path in (loader, interpreter, program):
                path.touch()
            with (
                patch("linux_toolchain._runtime_runner.run") as process_run,
                patch("linux_toolchain._runtime_runner.os.execve") as execve,
            ):
                runtime_runner_main(
                    [
                        str(loader),
                        str(interpreter),
                        "/sdk/lib:/runtime/lib",
                        str(program),
                        "argument",
                    ]
                )

            self.assertEqual(process_run.call_count, 2)
            exec_arguments = execve.call_args.args
            self.assertEqual(exec_arguments[1], (str(program), "argument"))
            self.assertEqual(
                exec_arguments[2]["LD_LIBRARY_PATH"], "/sdk/lib:/runtime/lib"
            )

    def test_old_aarch64_runtime_uses_isolated_kernel_loader_start(self) -> None:
        old = RuntimeContext(
            loader=Path("/sdk/lib/ld.so"),
            interpreter=Path("/lib/ld-linux-aarch64.so.1"),
            target_arch="aarch64",
            glibc_version="2.19",
            library_dirs=(),
            allowed_roots=(),
        )
        self.assertTrue(_requires_kernel_loader_start(old))
        self.assertFalse(
            _requires_kernel_loader_start(
                RuntimeContext(
                    loader=old.loader,
                    interpreter=old.interpreter,
                    target_arch="x86_64",
                    glibc_version="2.17",
                    library_dirs=(),
                    allowed_roots=(),
                )
            )
        )
        command = _kernel_loader_command(
            old, Path("/artifacts/probe"), (Path("/artifacts/libprobe.so"),)
        )
        self.assertIn("--map-root-user", command)
        self.assertIn("linux_toolchain._runtime_runner", command)
        self.assertEqual(command[-2:], ("/artifacts/probe", "/artifacts/libprobe.so"))

    def test_capture_preserves_combined_evidence_on_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status in (0, 7):
                with self.subTest(status=status):
                    evidence = root / f"capture-{status}.txt"
                    stderr = StringIO()
                    command = [
                        sys.executable,
                        "-c",
                        "import sys; print('out'); print('err', file=sys.stderr); "
                        f"sys.exit({status})",
                    ]
                    with redirect_stderr(stderr):
                        if status:
                            with self.assertRaisesRegex(
                                SmokeFailure, rf"command failed \({status}\)"
                            ):
                                _capture(command, env={}, evidence=evidence)
                        else:
                            output = _capture(command, env={}, evidence=evidence)
                            self.assertEqual(
                                output, evidence.read_text(encoding="utf-8")
                            )
                    lines = evidence.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(sorted(lines), ["err", "out"])
                    self.assertIn("out", stderr.getvalue())
                    self.assertIn("err", stderr.getvalue())

    def test_build_plan_keeps_conan_host_and_build_contexts_separate(self) -> None:
        plan = build_commands(
            source=Path("/source"),
            binding=Path("/binding"),
            build_profile="native-profile",
            build_dir=Path("/output"),
            conan="selected-conan",
            cmake="selected-cmake",
            build_type="Release",
            jobs=3,
        )
        self.assertIn("--profile:build=native-profile", plan.conan_install)
        self.assertIn("--profile:host=/binding/conan/host.profile", plan.conan_install)
        self.assertIn("--build=never", plan.conan_install)
        self.assertIn("--no-remote", plan.conan_install)
        self.assertIn(
            "--conf=tools.cmake.cmaketoolchain:user_presets=",
            plan.conan_install,
        )
        self.assertIn(
            "-DCMAKE_TOOLCHAIN_FILE=/output/conan/conan_toolchain.cmake",
            plan.configure,
        )
        self.assertIn("Unix Makefiles", plan.configure)
        self.assertIn("-DCMAKE_MAKE_PROGRAM=make", plan.configure)
        self.assertIn("-DCMAKE_BUILD_TYPE=Release", plan.configure)
        self.assertEqual(plan.build[-2:], ("--parallel", "3"))

        direct = build_commands(
            source=Path("/source"),
            binding=Path("/binding"),
            build_profile=None,
            build_dir=Path("/output"),
            conan="unused-conan",
            cmake="selected-cmake",
            build_type="Release",
            jobs=None,
            integration="cmake",
        )
        self.assertIsNone(direct.conan_install)
        self.assertIn(
            "-DCMAKE_TOOLCHAIN_FILE=/binding/cmake/toolchain.cmake",
            direct.configure,
        )

        environment = build_commands(
            source=Path("/source"),
            binding=Path("/binding"),
            build_profile=None,
            build_dir=Path("/output"),
            conan="unused-conan",
            cmake="unused-cmake",
            make="selected-make",
            build_type="Release",
            jobs=2,
            integration="shell",
        )
        self.assertIsNone(environment.conan_install)
        self.assertIsNone(environment.configure)
        self.assertIn("/binding/env/toolchain.env", environment.build)
        self.assertIn("selected-make", environment.build)
        self.assertIn("BUILD_TYPE=Release", environment.build)
        self.assertEqual(environment.build[-2:], ("--jobs", "2"))

    def test_build_directory_requires_an_ownership_marker_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            build_dir = Path(directory) / "smoke"
            _prepare_build_directory(build_dir)
            (build_dir / "conan").mkdir()
            (build_dir / "conan" / "generated").write_text(
                "generated\n", encoding="utf-8"
            )

            _prepare_build_directory(build_dir)
            self.assertFalse((build_dir / "conan").exists())

            unowned = Path(directory) / "unowned"
            unowned.mkdir()
            (unowned / "keep").write_text("user data\n", encoding="utf-8")
            with self.assertRaisesRegex(SmokeFailure, "unowned non-empty"):
                _prepare_build_directory(unowned)
            self.assertTrue((unowned / "keep").is_file())

    def test_build_directory_rejects_symlinked_control_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_dir = root / "smoke"
            _prepare_build_directory(build_dir)

            marker = build_dir / ".linux-toolchain-smoke-build.json"
            external_marker = root / "external-marker.json"
            external_marker.write_text(marker.read_text(encoding="utf-8"))
            marker.unlink()
            marker.symlink_to(external_marker)
            with self.assertRaisesRegex(SmokeFailure, "invalid smoke build marker"):
                _prepare_build_directory(build_dir)

            marker.unlink()
            _prepare_build_directory(build_dir)
            external_home = root / "external-conan-home"
            external_home.mkdir()
            (build_dir / "conan-home").symlink_to(
                external_home, target_is_directory=True
            )
            with self.assertRaisesRegex(SmokeFailure, "invalid managed Conan home"):
                _prepare_build_directory(build_dir)

    def test_loader_closure_rejects_missing_and_host_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "runtime"
            outside = root / "host"
            allowed.mkdir()
            outside.mkdir()
            allowed_library = allowed / "libc.so.6"
            outside_library = outside / "libstdc++.so.6"
            allowed_library.touch()
            outside_library.touch()

            verify_loader_closure(f"libc.so.6 => {allowed_library} (0x1)\n", (allowed,))
            with self.assertRaisesRegex(SmokeFailure, "escaped"):
                verify_loader_closure(
                    f"libstdc++.so.6 => {outside_library} (0x2)\n", (allowed,)
                )
            with self.assertRaisesRegex(SmokeFailure, "incomplete"):
                verify_loader_closure("libgcc_s.so.1 => not found\n", (allowed,))

    def test_external_runtime_is_rejected_before_a_build_is_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binding = Path(directory)
            (binding / "binding.json").write_text(
                json.dumps({"cxx_runtime": {"policy": "external-unpinned"}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                SmokeFailure, "requires a pinned compiler runtime"
            ):
                require_pinned_runtime(binding)

    def test_gcc_and_llvm_pinned_runtimes_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binding = Path(directory)
            manifest = binding / "binding.json"
            for policy in ("pinned-gcc-runtime", "pinned-llvm-runtime"):
                with self.subTest(policy=policy):
                    manifest.write_text(
                        json.dumps({"cxx_runtime": {"policy": policy}}),
                        encoding="utf-8",
                    )
                    require_pinned_runtime(binding)

    def test_runner_rejects_top_level_build_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_build = root / "real-build"
            real_build.mkdir()
            linked_build = root / "linked-build"
            linked_build.symlink_to(real_build, target_is_directory=True)

            with self.assertRaisesRegex(SmokeFailure, "cannot be a symlink"):
                run(
                    parse_args(
                        [
                            "--binding",
                            str(root / "unused-binding"),
                            "--integration",
                            "conan",
                            "--build-dir",
                            str(linked_build),
                        ]
                    )
                )

    def test_runner_rejects_external_conan_home_inside_cleaned_build_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build_dir = root / "build"
            conan_home = build_dir / "conan"
            conan_home.mkdir(parents=True)
            sentinel = conan_home / "keep"
            sentinel.write_text("user data\n", encoding="utf-8")

            with self.assertRaisesRegex(SmokeFailure, "must not overlap"):
                run(
                    parse_args(
                        [
                            "--binding",
                            str(root / "unused-binding"),
                            "--integration",
                            "conan",
                            "--build-dir",
                            str(build_dir),
                            "--conan-home",
                            str(conan_home),
                            "--build-profile",
                            "native",
                        ]
                    )
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data\n")


if __name__ == "__main__":
    unittest.main()
