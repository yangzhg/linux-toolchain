#!/usr/bin/env python3

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

"""Build and audit a caller-provided Bolt checkout as a consumer test."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

_MAKE_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
_MAKE_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class AcceptanceError(Exception):
    """An invalid acceptance configuration or local input."""


class CommandFailure(Exception):
    def __init__(self, step: str, returncode: int) -> None:
        super().__init__(f"{step} failed with exit status {returncode}")
        self.step = step
        self.returncode = returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build an external Bolt checkout with a generated binding, then "
            "audit the selected Bolt artifacts."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--bolt-checkout",
        required=True,
        type=Path,
        help="existing Bolt source checkout supplied by the caller",
    )
    parser.add_argument(
        "--binding",
        required=True,
        type=Path,
        help="generated compiler binding to exercise",
    )
    parser.add_argument(
        "--conan-home",
        type=Path,
        help=(
            "caller-prepared Conan home used by the Bolt build; required unless "
            "--skip-build is selected"
        ),
    )
    parser.add_argument(
        "--audit-path",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help=(
            "Bolt file or directory to audit; relative paths are resolved below "
            "the checkout and may be repeated"
        ),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help=(
            "acceptance scratch directory; relative paths are resolved below "
            "the Bolt checkout"
        ),
    )
    parser.add_argument(
        "--make-target",
        default="release",
        help="public Bolt Makefile target to invoke",
    )
    parser.add_argument(
        "--make-variable",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "additional Bolt Makefile variable; may be repeated (PROFILE is "
            "owned by this harness)"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        help="set Bolt's NUM_THREADS Makefile variable",
    )
    parser.add_argument(
        "--smoke-integration",
        choices=("cmake", "shell", "conan"),
        default="cmake",
        help="generic binding integration checked before the Bolt build",
    )
    parser.add_argument(
        "--build-type",
        choices=("Debug", "Release", "RelWithDebInfo", "MinSizeRel"),
        default="Release",
        help="consumer build configuration passed to the generic smoke project",
    )
    parser.add_argument(
        "--runner",
        help="optional target runner passed to the generic smoke command",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="skip the generic binding smoke test",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="audit an existing Bolt build without invoking Make",
    )
    parser.add_argument(
        "--toolchain-cli",
        default="linux-toolchain",
        metavar="PROGRAM",
        help="installed toolchain-generator CLI executable",
    )
    parser.add_argument(
        "--make",
        default=os.environ.get("MAKE", "make"),
        metavar="PROGRAM",
        help="Make executable used for the Bolt build",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the complete command plan without checking paths or running it",
    )
    return parser


def _absolute(path: Path, *, base: Path | None = None) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve(strict=False)


def _make_variables(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    names: set[str] = set()
    for value in values:
        if "=" not in value:
            raise AcceptanceError(
                f"invalid --make-variable {value!r}; expected NAME=VALUE"
            )
        name, assigned = value.split("=", 1)
        if not _MAKE_VARIABLE.fullmatch(name):
            raise AcceptanceError(f"invalid Makefile variable name: {name!r}")
        if name == "PROFILE":
            raise AcceptanceError(
                "PROFILE is selected from --binding and cannot be overridden"
            )
        if name in names:
            raise AcceptanceError(f"duplicate Makefile variable: {name}")
        if "\0" in assigned or "\n" in assigned or "\r" in assigned:
            raise AcceptanceError(f"invalid control character in {name}")
        names.add(name)
        result.append(value)
    return tuple(result)


def _require_directory(path: Path, description: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise AcceptanceError(f"{description} is not a directory: {path}")


def _require_file(path: Path, description: str) -> None:
    if not path.is_file():
        raise AcceptanceError(f"{description} is not a file: {path}")


def _run(
    step: str,
    command: Sequence[str],
    *,
    dry_run: bool,
    conan_home: Path | None = None,
) -> None:
    prefix = (
        f"CONAN_HOME={shlex.quote(str(conan_home))} " if conan_home is not None else ""
    )
    print(f"[{step}] {prefix}{shlex.join(command)}", flush=True)
    if dry_run:
        return
    environment = None
    if conan_home is not None:
        environment = os.environ.copy()
        environment["CONAN_HOME"] = str(conan_home)
    try:
        completed = subprocess.run(command, check=False, env=environment)
    except OSError as error:
        raise AcceptanceError(
            f"cannot start {command[0]!r} for {step}: {error}"
        ) from error
    if completed.returncode:
        raise CommandFailure(step, completed.returncode)


def _commands(
    args: argparse.Namespace,
) -> tuple[list[tuple[str, list[str]]], list[Path], Path | None]:
    checkout = _absolute(args.bolt_checkout)
    binding = _absolute(args.binding)
    work_dir = _absolute(
        args.work_dir or Path("_build/toolchain-consumer-acceptance"),
        base=checkout,
    )
    audit_paths = [
        _absolute(path, base=checkout)
        for path in (args.audit_path or [Path("_build/Release")])
    ]
    conan_home = _absolute(args.conan_home) if args.conan_home is not None else None

    if not _MAKE_TARGET.fullmatch(args.make_target):
        raise AcceptanceError(f"invalid Makefile target: {args.make_target!r}")
    if args.jobs is not None and args.jobs < 1:
        raise AcceptanceError("--jobs must be a positive integer")
    make_variables = list(_make_variables(args.make_variable))
    if args.jobs is not None:
        if any(value.startswith("NUM_THREADS=") for value in make_variables):
            raise AcceptanceError("--jobs and NUM_THREADS cannot both be specified")
        make_variables.append(f"NUM_THREADS={args.jobs}")

    if not args.dry_run:
        if not args.skip_build:
            if conan_home is None:
                raise AcceptanceError(
                    "--conan-home is required for the Bolt build; initialize its "
                    "settings_user.yml with 'linux-toolchain conan settings'"
                )
            _require_directory(conan_home, "Conan home")
            _require_file(
                conan_home / "settings_user.yml",
                "Conan settings extension",
            )
        _require_directory(checkout, "Bolt checkout")
        _require_file(checkout / "Makefile", "Bolt Makefile")
        _require_file(checkout / "CMakeLists.txt", "Bolt CMake project")
        _require_directory(binding, "binding")
        _require_file(binding / "binding.json", "binding manifest")
        _require_file(binding / "audit-policy.json", "binding audit policy")
        if not args.skip_smoke:
            smoke_entry = {
                "cmake": binding / "cmake/toolchain.cmake",
                "shell": binding / "env/toolchain.env",
                "conan": binding / "conan/host.profile",
            }[args.smoke_integration]
            _require_file(
                smoke_entry,
                f"{args.smoke_integration} integration entry point",
            )
        if not args.skip_build:
            _require_file(
                binding / "conan/host.profile",
                "Conan host profile (create the binding with --integration conan)",
            )

    required_integrations: set[str] = set()
    if not args.skip_smoke:
        required_integrations.add(args.smoke_integration)
    if not args.skip_build:
        required_integrations.add("conan")
    commands: list[tuple[str, list[str]]] = []
    if required_integrations:
        doctor = [args.toolchain_cli, "doctor", "--workflow", "consumer"]
        for integration in sorted(required_integrations):
            doctor.extend(("--integration", integration))
        commands.append(("doctor", doctor))
    if not args.skip_smoke:
        smoke = [
            args.toolchain_cli,
            "smoke",
            "--integration",
            args.smoke_integration,
            "--build-type",
            args.build_type,
            "--binding",
            str(binding),
            "--build-dir",
            str(work_dir / "smoke"),
        ]
        if args.runner:
            smoke.extend(("--runner", args.runner))
        commands.append(("binding smoke", smoke))
    if not args.skip_build:
        commands.append(
            (
                "Bolt build",
                [
                    args.make,
                    "-C",
                    str(checkout),
                    args.make_target,
                    f"PROFILE={binding / 'conan/host.profile'}",
                    *make_variables,
                ],
            )
        )
    commands.append(
        (
            "Bolt artifact audit",
            [
                args.toolchain_cli,
                "audit",
                "--policy",
                str(binding / "audit-policy.json"),
                "--recursive",
                *(str(path) for path in audit_paths),
            ],
        )
    )
    return commands, audit_paths, conan_home


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        commands, audit_paths, conan_home = _commands(args)
        for step, command in commands:
            if step == "Bolt artifact audit" and not args.dry_run:
                for path in audit_paths:
                    if not path.exists():
                        raise AcceptanceError(f"audit path does not exist: {path}")
            _run(
                step,
                command,
                dry_run=args.dry_run,
                conan_home=(conan_home if step == "Bolt build" else None),
            )
    except CommandFailure as error:
        print(
            f"bolt-consumer-acceptance: {error.step} failed with exit status "
            f"{error.returncode}",
            file=sys.stderr,
        )
        return error.returncode if 0 < error.returncode < 126 else 2
    except AcceptanceError as error:
        print(f"bolt-consumer-acceptance: error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("bolt-consumer-acceptance: interrupted", file=sys.stderr)
        return 130

    result = "command plan is valid" if args.dry_run else "acceptance passed"
    print(f"bolt-consumer-acceptance: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
