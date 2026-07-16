from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from linux_toolchain.elf.reader import resolve_readelf_candidates
from linux_toolchain.errors import (
    ConfigurationError,
    ExternalToolError,
    LinuxToolchainError,
)
from linux_toolchain.integrations import SUPPORTED_INTEGRATIONS, IntegrationName
from linux_toolchain.process import run
from linux_toolchain.terminal import BOLD, CYAN, GREEN, RED, YELLOW, style

DiagnosticLevel = Literal["required", "optional"]
DiagnosticStatus = Literal["pass", "fail", "warn"]
DoctorWorkflow = Literal["all", "sdk", "managed", "external", "consumer"]
DoctorIntegration = IntegrationName

DOCTOR_SCHEMA = "linux-toolchain-doctor"
DOCTOR_FORMAT = 1
_DOCKER_PROBE_TIMEOUT_SECONDS = 5.0
DOCTOR_WORKFLOWS: tuple[DoctorWorkflow, ...] = (
    "all",
    "sdk",
    "managed",
    "external",
    "consumer",
)
DOCTOR_INTEGRATIONS: tuple[DoctorIntegration, ...] = SUPPORTED_INTEGRATIONS

_REQUIRED_WORKFLOWS: dict[str, frozenset[DoctorWorkflow]] = {
    "platform": frozenset(("sdk", "managed", "external", "consumer")),
    "python": frozenset(("sdk", "managed", "external", "consumer")),
    "user": frozenset(("sdk", "managed")),
    "docker-cli": frozenset(("sdk", "managed")),
    "docker-daemon": frozenset(("sdk", "managed")),
    "readelf": frozenset(("sdk", "managed", "external", "consumer")),
    "external-compiler": frozenset(("external",)),
}


@dataclass(frozen=True)
class DiagnosticCheck:
    id: str
    level: DiagnosticLevel
    status: DiagnosticStatus
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "level": self.level,
            "status": self.status,
            "message": self.message,
        }


@dataclass(frozen=True)
class DiagnosticReport:
    checks: tuple[DiagnosticCheck, ...]
    workflow: DoctorWorkflow = "all"
    integrations: tuple[DoctorIntegration, ...] = ()

    @property
    def passed(self) -> bool:
        return all(
            check.status == "pass" for check in self.checks if check.level == "required"
        )

    def to_dict(self) -> dict[str, object]:
        required = tuple(check for check in self.checks if check.level == "required")
        optional = tuple(check for check in self.checks if check.level == "optional")
        return {
            "schema": DOCTOR_SCHEMA,
            "format": DOCTOR_FORMAT,
            "workflow": self.workflow,
            "integrations": list(self.integrations),
            "passed": self.passed,
            "summary": {
                "required": {
                    "passed": sum(check.status == "pass" for check in required),
                    "total": len(required),
                },
                "optional": {
                    "available": sum(check.status == "pass" for check in optional),
                    "total": len(optional),
                },
            },
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_text(self, *, color: bool = False) -> str:
        required = tuple(check for check in self.checks if check.level == "required")
        optional = tuple(check for check in self.checks if check.level == "optional")
        width = max((len(check.id) for check in self.checks), default=0)
        lines = [
            style("linux-toolchain doctor", BOLD, CYAN, enabled=color),
            f"Workflow: {self.workflow}",
        ]
        if self.integrations:
            lines.append("Integrations: " + ", ".join(self.integrations))
        for title, checks in (("Required", required), ("Optional", optional)):
            lines.extend(("", style(f"{title} checks:", BOLD, enabled=color)))
            for check in checks:
                marker = (
                    "PASS"
                    if check.status == "pass"
                    else ("FAIL" if check.level == "required" else "WARN")
                )
                marker_color = {
                    "PASS": GREEN,
                    "FAIL": RED,
                    "WARN": YELLOW,
                }[marker]
                rendered_marker = style(
                    f"{marker:<4}", BOLD, marker_color, enabled=color
                )
                lines.append(
                    f"  {rendered_marker}  {check.id:<{width}}  {check.message}"
                )
        required_passed = sum(check.status == "pass" for check in required)
        optional_available = sum(check.status == "pass" for check in optional)
        lines.extend(
            (
                "",
                f"Required: {required_passed}/{len(required)} passed",
                f"Optional: {optional_available}/{len(optional)} available",
                "Overall: "
                + style(
                    "PASS" if self.passed else "FAIL",
                    BOLD,
                    GREEN if self.passed else RED,
                    enabled=color,
                ),
            )
        )
        return "\n".join(lines)


def _check(
    check_id: str,
    level: DiagnosticLevel,
    passed: bool,
    message: str,
) -> DiagnosticCheck:
    return DiagnosticCheck(
        id=check_id,
        level=level,
        status="pass" if passed else ("fail" if level == "required" else "warn"),
        message=" ".join(message.splitlines()),
    )


def _level_for(
    check_id: str,
    workflow: DoctorWorkflow,
    integrations: tuple[DoctorIntegration, ...],
) -> DiagnosticLevel:
    required_by = _REQUIRED_WORKFLOWS.get(check_id, frozenset())
    if workflow == "all":
        workflow_level: DiagnosticLevel = "required" if required_by else "optional"
    else:
        workflow_level = "required" if workflow in required_by else "optional"
    integration_tools = {
        # The packaged CMake qualification uses the Unix Makefiles backend.
        "cmake": frozenset(("cmake", "make")),
        "shell": frozenset(("make",)),
        "conan": frozenset(("conan", "cmake", "make")),
    }
    required_tools = frozenset().union(
        *(integration_tools[name] for name in integrations)
    )
    return "required" if check_id in required_tools else workflow_level


def _for_workflow(
    check: DiagnosticCheck,
    workflow: DoctorWorkflow,
    integrations: tuple[DoctorIntegration, ...],
) -> DiagnosticCheck:
    level = _level_for(check.id, workflow, integrations)
    passed = check.status == "pass"
    return DiagnosticCheck(
        id=check.id,
        level=level,
        status="pass" if passed else ("fail" if level == "required" else "warn"),
        message=check.message,
    )


def _resolve_first(*candidates: str | None) -> str | None:
    return next(
        (
            resolved
            for candidate in candidates
            if candidate and (resolved := shutil.which(candidate))
        ),
        None,
    )


def _platform_check() -> DiagnosticCheck:
    system = platform.system()
    return _check(
        "platform",
        "required",
        system == "Linux",
        f"detected {system or 'unknown'}; Linux is required",
    )


def _python_check() -> DiagnosticCheck:
    version = sys.version_info
    rendered = f"{version.major}.{version.minor}.{version.micro}"
    return _check(
        "python",
        "required",
        (version.major, version.minor) >= (3, 10),
        f"Python {rendered}; 3.10 or newer is required",
    )


def _user_check() -> DiagnosticCheck:
    if not hasattr(os, "getuid"):
        return _check("user", "required", False, "cannot determine the Unix user ID")
    uid = os.getuid()
    return _check(
        "user",
        "required",
        uid != 0,
        (
            f"uid {uid} is non-root"
            if uid != 0
            else "uid 0 cannot run SDK or managed compiler builds"
        ),
    )


def _readelf_check() -> DiagnosticCheck:
    candidates = resolve_readelf_candidates(resolver=shutil.which)
    executable = candidates[0] if candidates else None
    return _check(
        "readelf",
        "required",
        executable is not None,
        (
            f"found {executable}"
            if executable
            else "GNU readelf or llvm-readelf not found"
        ),
    )


def _docker_checks(*, probe_daemon: bool) -> tuple[DiagnosticCheck, DiagnosticCheck]:
    docker = _resolve_first("docker")
    cli = _check(
        "docker-cli",
        "required",
        docker is not None,
        f"found {docker}" if docker else "Docker CLI not found",
    )
    if docker is None:
        return cli, _check(
            "docker-daemon",
            "required",
            False,
            "not checked because the Docker CLI is unavailable",
        )
    if not probe_daemon:
        return cli, _check(
            "docker-daemon",
            "required",
            False,
            "not checked because this workflow does not require Docker",
        )

    endpoint = os.environ.get("DOCKER_HOST")
    if not endpoint or os.environ.get("DOCKER_CONTEXT"):
        try:
            endpoint = run(
                [
                    docker,
                    "context",
                    "inspect",
                    "--format",
                    "{{.Endpoints.docker.Host}}",
                ],
                timeout=_DOCKER_PROBE_TIMEOUT_SECONDS,
            ).stdout.strip()
        except ExternalToolError:
            return cli, _check(
                "docker-daemon",
                "required",
                False,
                "cannot inspect the active Docker context",
            )
    endpoint = endpoint.strip()
    if not endpoint.startswith("unix://"):
        return cli, _check(
            "docker-daemon",
            "required",
            False,
            f"local Unix endpoint required; detected {endpoint or 'none'}",
        )
    try:
        server_platform = run(
            [docker, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"],
            timeout=_DOCKER_PROBE_TIMEOUT_SECONDS,
        ).stdout.strip()
    except ExternalToolError:
        return cli, _check(
            "docker-daemon",
            "required",
            False,
            "cannot reach the Docker daemon",
        )
    return cli, _check(
        "docker-daemon",
        "required",
        server_platform.startswith("linux/"),
        (
            f"local Unix daemon; server {server_platform}"
            if server_platform.startswith("linux/")
            else (
                f"Linux Docker daemon required; detected {server_platform or 'unknown'}"
            )
        ),
    )


def _optional_program(check_id: str, program: str) -> DiagnosticCheck:
    executable = _resolve_first(program)
    return _check(
        check_id,
        "optional",
        executable is not None,
        f"found {executable}" if executable else f"{program} not found",
    )


def _external_compiler_check(*, validate: bool) -> DiagnosticCheck:
    candidates = (
        (os.environ.get("CC"), os.environ.get("CXX")),
        ("gcc", "g++"),
        ("clang", "clang++"),
        ("cc", "c++"),
    )
    pairs: list[tuple[str, str]] = []
    for cc_name, cxx_name in candidates:
        if not cc_name or not cxx_name:
            continue
        cc = _resolve_first(cc_name)
        cxx = _resolve_first(cxx_name)
        if cc is not None and cxx is not None and (cc, cxx) not in pairs:
            pairs.append((cc, cxx))
    if not pairs:
        return _check(
            "external-compiler",
            "required",
            False,
            "no complete CC/CXX, GCC/G++, or Clang/Clang++ pair found",
        )
    if not validate:
        return _check(
            "external-compiler",
            "required",
            True,
            f"found {pairs[0][0]} and {pairs[0][1]}; not probed for this workflow",
        )

    # Reuse binding validation so supported families, minimum versions and
    # matching C/C++ targets cannot drift between doctor and bind external.
    from linux_toolchain.compiler.toolchain import detect_compiler

    failures: list[str] = []
    for cc, cxx in pairs:
        try:
            compiler = detect_compiler(cc, cxx)
        except LinuxToolchainError as error:
            failures.append(f"{cc} + {cxx}: {error}")
            continue
        return _check(
            "external-compiler",
            "required",
            True,
            (
                f"found {compiler.family} {compiler.version} for "
                f"{compiler.target}: {cc} and {cxx}"
            ),
        )
    return _check(
        "external-compiler",
        "required",
        False,
        "no supported matching compiler pair; " + "; ".join(failures),
    )


def run_diagnostics(
    workflow: DoctorWorkflow = "all",
    integrations: Sequence[DoctorIntegration] | None = None,
) -> DiagnosticReport:
    if workflow not in DOCTOR_WORKFLOWS:
        raise ConfigurationError(f"unsupported doctor workflow: {workflow!r}")
    selected_integrations = tuple(
        ("cmake",)
        if workflow == "consumer" and integrations is None
        else integrations or ()
    )
    unknown_integrations = sorted(
        set(selected_integrations).difference(DOCTOR_INTEGRATIONS)
    )
    if unknown_integrations:
        raise ConfigurationError(
            "unsupported doctor integration: " + ", ".join(unknown_integrations)
        )
    if len(set(selected_integrations)) != len(selected_integrations):
        raise ConfigurationError("doctor integrations must not contain duplicates")
    docker_cli, docker_daemon = _docker_checks(
        probe_daemon=workflow in {"all", "sdk", "managed"}
    )
    raw_checks = (
        _platform_check(),
        _python_check(),
        _user_check(),
        docker_cli,
        docker_daemon,
        _readelf_check(),
        _external_compiler_check(validate=workflow in {"all", "external"}),
        _optional_program("cmake", "cmake"),
        _optional_program("conan", "conan"),
        _optional_program("ninja", "ninja"),
        _optional_program("make", "make"),
        _optional_program("gcc", "gcc"),
        _optional_program("clang", "clang"),
        _optional_program("pkg-config", "pkg-config"),
    )
    checks = tuple(
        _for_workflow(
            check,
            workflow,
            selected_integrations,
        )
        for check in raw_checks
    )
    return DiagnosticReport(
        checks=checks,
        workflow=workflow,
        integrations=selected_integrations,
    )
