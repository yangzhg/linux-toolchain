from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from linux_toolchain.errors import ConfigurationError, ExternalToolError
from linux_toolchain.integrity import file_sha256
from linux_toolchain.process import run
from linux_toolchain.schema import canonical_json_sha256

BUILDER_CONTRACT_LABEL = "org.linux-toolchain.builder-contract-sha256"
BUILDER_DOCKERFILE_NAME = "builder.Dockerfile"
MANAGED_BUILDER_TARGET = "managed"
SDK_BUILDER_TARGET = "crosstool-ng"
TEMPORARY_CONTAINER_LABEL = "org.linux-toolchain.temporary-container-owner"
UBUNTU_BUILDER_SNAPSHOT_ENV = "LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT"
UBUNTU_22_04_BASE_IMAGE = (
    "ubuntu:22.04@sha256:"
    "0e0a0fc6d18feda9db1590da249ac93e8d5abfea8f4c3c0c849ce512b5ef8982"
)

_LINUX_PLATFORM_BY_ARCHITECTURE = {
    "x86_64": "linux/amd64",
    "aarch64": "linux/arm64",
}
_LINUX_ARCHITECTURE_BY_PLATFORM = {
    value: key for key, value in _LINUX_PLATFORM_BY_ARCHITECTURE.items()
}

_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_NAME = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
_BUILD_TARGET = re.compile(r"^[A-Za-z0-9_.-]+$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_OWNER_ID = re.compile(r"^[0-9a-f]{64}$")
_UBUNTU_SNAPSHOT = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


@dataclass(frozen=True)
class BuilderHost:
    uid: int
    gid: int


@dataclass(frozen=True)
class ContainerIdentityFiles:
    passwd: Path
    group: Path
    uid: int
    gid: int


def ubuntu_builder_snapshot() -> str:
    """Return the optional Ubuntu archive snapshot selected for builder images."""

    value = os.environ.get(UBUNTU_BUILDER_SNAPSHOT_ENV, "").strip()
    if value and _UBUNTU_SNAPSHOT.fullmatch(value) is None:
        raise ConfigurationError(
            f"{UBUNTU_BUILDER_SNAPSHOT_ENV} must be empty or an Ubuntu snapshot "
            "timestamp such as 20260701T000000Z"
        )
    return value


def require_non_root_builder(context: str) -> BuilderHost:
    uid = os.getuid()
    if uid == 0:
        raise ConfigurationError(f"{context} cannot run as uid 0")
    return BuilderHost(uid=uid, gid=os.getgid())


def docker_endpoint(docker: str) -> str:
    configured = os.environ.get("DOCKER_HOST")
    if configured and not os.environ.get("DOCKER_CONTEXT"):
        return configured.strip()
    try:
        return run(
            [
                docker,
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
            ]
        ).stdout.strip()
    except ExternalToolError as error:
        raise ConfigurationError("cannot inspect the active Docker context") from error


def validate_native_docker_daemon(
    docker: str,
    expected_platform: str,
    *,
    context: str,
) -> None:
    endpoint = docker_endpoint(docker)
    if not endpoint.startswith("unix://"):
        raise ConfigurationError(
            f"{context} requires a local Unix Docker daemon, got {endpoint!r}"
        )
    try:
        server_platform = run(
            [docker, "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]
        ).stdout.strip()
    except ExternalToolError as error:
        raise ConfigurationError(
            f"Docker cannot reach the local daemon required by {context}"
        ) from error
    if server_platform != expected_platform:
        raise ConfigurationError(
            f"{context} requires a native Docker daemon matching "
            f"{expected_platform!r}, got {server_platform!r}"
        )


def validate_packaged_dockerfile(
    dockerfile: Path,
    expected_sha256: str,
    *,
    provenance: str,
) -> str:
    actual = file_sha256(dockerfile)
    if actual != expected_sha256:
        raise ConfigurationError(
            f"custom {provenance} cannot be verified; use the packaged Dockerfile "
            "or a byte-identical copy"
        )
    return actual


def docker_build_command(
    *,
    dockerfile: Path,
    context: Path,
    image: str,
    build_args: Mapping[str, str],
    contract_digest: str,
    platform: str,
    target: str,
) -> list[str]:
    if _IMAGE_NAME.fullmatch(image) is None:
        raise ConfigurationError("builder image name is invalid")
    if _BUILD_TARGET.fullmatch(target) is None:
        raise ConfigurationError("builder image target is invalid")
    command = [
        "docker",
        "build",
        "--platform",
        platform,
        "--target",
        target,
        "--file",
        os.fspath(dockerfile),
        "--tag",
        image,
        "--label",
        f"{BUILDER_CONTRACT_LABEL}={contract_digest}",
    ]
    for name, value in build_args.items():
        command.extend(("--build-arg", f"{name}={value}"))
    command.append(os.fspath(context))
    return command


def _write_identity_file(path: Path, content: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError(
            f"container identity file must be a regular file: {path}"
        )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(content)
        temporary = Path(temporary_name)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"cannot write container identity file {path}"
        ) from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def write_container_identity_files(
    workspace: Path,
    host: BuilderHost,
    *,
    account_description: str,
    home: str,
    shell: str,
) -> ContainerIdentityFiles:
    try:
        workspace_root = workspace.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"cannot access container workspace {workspace}: {error}"
        ) from error
    build_dir = workspace_root / "build"
    if build_dir.is_symlink():
        raise ConfigurationError(
            f"container build directory cannot be a symlink: {build_dir}"
        )
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
        canonical_build_dir = build_dir.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"cannot prepare container build directory {build_dir}: {error}"
        ) from error
    if canonical_build_dir != build_dir:
        raise ConfigurationError(
            f"container build directory is not canonical: {build_dir}"
        )
    identity_dir = build_dir / "container-identity"
    if identity_dir.is_symlink():
        raise ConfigurationError(
            f"container identity directory cannot be a symlink: {identity_dir}"
        )
    try:
        identity_dir.mkdir(parents=True, exist_ok=True)
        canonical_identity_dir = identity_dir.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"cannot prepare container identity directory {identity_dir}: {error}"
        ) from error
    if canonical_identity_dir != identity_dir:
        raise ConfigurationError(
            f"container identity directory is not canonical: {identity_dir}"
        )
    passwd = identity_dir / "passwd"
    group = identity_dir / "group"
    _write_identity_file(
        passwd,
        "root:x:0:0:root:/root:/bin/sh\n"
        f"builder:x:{host.uid}:{host.gid}:{account_description}:{home}:{shell}\n",
    )
    _write_identity_file(group, f"root:x:0:\nbuilder:x:{host.gid}:builder\n")
    return ContainerIdentityFiles(
        passwd=passwd.resolve(),
        group=group.resolve(),
        uid=host.uid,
        gid=host.gid,
    )


def temporary_container_owner(workspace: Path, purpose: str) -> str:
    if not purpose:
        raise ConfigurationError("temporary container purpose cannot be empty")
    return canonical_json_sha256(
        {
            "workspace": os.fspath(workspace.resolve()),
            "purpose": purpose,
        }
    )


def _discard_cidfile(cidfile: Path) -> None:
    if cidfile.is_symlink():
        raise ConfigurationError(
            f"temporary container cidfile cannot be a symlink: {cidfile}"
        )
    try:
        cidfile.unlink(missing_ok=True)
    except OSError as error:
        raise ExternalToolError(
            f"cannot remove temporary container cidfile {cidfile}: {error}"
        ) from error


def _container_id(cidfile: Path) -> str | None:
    if cidfile.is_symlink():
        raise ConfigurationError(
            f"temporary container cidfile cannot be a symlink: {cidfile}"
        )
    try:
        value = cidfile.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(
            f"cannot read temporary container cidfile {cidfile}: {error}"
        ) from error
    if _CONTAINER_ID.fullmatch(value) is None:
        raise ConfigurationError(f"temporary container cidfile is malformed: {cidfile}")
    return value


def _remove_owned_container(cidfile: Path, owner: str) -> None:
    container_id = _container_id(cidfile)
    if container_id is None:
        return
    try:
        inspection = run(["docker", "container", "inspect", container_id], timeout=10)
    except ExternalToolError as inspect_error:
        listing = run(
            ["docker", "container", "ls", "--all", "--no-trunc", "--quiet"],
            timeout=10,
        )
        if container_id in listing.stdout.splitlines():
            raise inspect_error
        _discard_cidfile(cidfile)
        return
    try:
        values = json.loads(inspection.stdout)
        value = values[0]
        labels = value["Config"]["Labels"] or {}
        recorded_owner = labels.get(TEMPORARY_CONTAINER_LABEL)
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ) as error:
        raise ConfigurationError(
            f"temporary container metadata is malformed: {container_id}"
        ) from error
    if recorded_owner != owner:
        raise ConfigurationError(
            "refusing to remove a container not owned by this producer workspace: "
            f"{container_id}"
        )
    run(["docker", "container", "rm", "--force", container_id], timeout=30)
    _discard_cidfile(cidfile)


@contextmanager
def temporary_container_run(
    command: Sequence[str],
    *,
    cidfile: Path,
    owner: str,
) -> Iterator[tuple[list[str], Callable[[], None]]]:
    """Guard a Docker run so cancellation cannot leave a writer behind."""

    if list(command[:2]) != ["docker", "run"]:
        raise ConfigurationError(
            "temporary container command must start with docker run"
        )
    if _OWNER_ID.fullmatch(owner) is None:
        raise ConfigurationError("temporary container owner must be a SHA-256 digest")
    if not cidfile.parent.is_dir() or cidfile.parent.is_symlink():
        raise ConfigurationError(
            f"temporary container cidfile parent is invalid: {cidfile.parent}"
        )
    _remove_owned_container(cidfile, owner)
    guarded = [
        *command[:2],
        "--cidfile",
        os.fspath(cidfile),
        "--label",
        f"{TEMPORARY_CONTAINER_LABEL}={owner}",
        *command[2:],
    ]

    def cancel() -> None:
        _remove_owned_container(cidfile, owner)

    try:
        yield guarded, cancel
    except BaseException:
        try:
            cancel()
        except Exception:
            pass
        raise
    else:
        _discard_cidfile(cidfile)


def linux_platform_for_architecture(architecture: str) -> str:
    try:
        return _LINUX_PLATFORM_BY_ARCHITECTURE[architecture]
    except KeyError as error:
        raise ConfigurationError(
            f"unsupported Linux producer architecture: {architecture!r}"
        ) from error


def linux_architecture_for_platform(platform: str) -> str:
    try:
        return _LINUX_ARCHITECTURE_BY_PLATFORM[platform]
    except KeyError as error:
        raise ConfigurationError(
            f"unsupported Linux producer platform: {platform!r}"
        ) from error


@dataclass(frozen=True)
class BuilderImage:
    image_id: str
    repo_digests: tuple[str, ...]
    os: str
    architecture: str

    @property
    def platform(self) -> str:
        return f"{self.os}/{self.architecture}"


@dataclass(frozen=True)
class BuilderImageResolution:
    image: BuilderImage
    cache_hit: bool


def builder_image_contract_digest(
    *,
    dockerfile_sha256: str,
    base_image: str,
    pinned_input: str,
    platform: str,
    build_args: Mapping[str, str],
    target: str,
) -> str:
    """Identify a builder image solely from inputs consumed by Docker."""

    value = {
        "dockerfile_sha256": dockerfile_sha256,
        "base_image": base_image,
        "pinned_input": pinned_input,
        "platform": platform,
        "target": target,
        "build_args": {key: build_args[key] for key in sorted(build_args)},
    }
    return canonical_json_sha256(value)


def inspect_builder_image(
    image: str,
    *,
    contract_digest: str,
    platform: str,
) -> BuilderImage | None:
    """Return an immutable image identity only for an exact builder match."""

    try:
        result = run(["docker", "image", "inspect", image])
        values = json.loads(result.stdout)
        value = values[0]
        if not isinstance(value, dict):
            return None
        image_id = value["Id"]
        os_name = value["Os"]
        architecture = value["Architecture"]
        repo_digests = value.get("RepoDigests") or []
        config = value["Config"]
        if not isinstance(config, dict):
            return None
        labels = config.get("Labels") or {}
        if not isinstance(labels, dict):
            return None
        recorded_contract = labels.get(BUILDER_CONTRACT_LABEL)
        if (
            not isinstance(image_id, str)
            or _IMAGE_ID.fullmatch(image_id) is None
            or not isinstance(os_name, str)
            or not isinstance(architecture, str)
            or not isinstance(repo_digests, list)
            or not all(isinstance(item, str) for item in repo_digests)
            or recorded_contract != contract_digest
            or f"{os_name}/{architecture}" != platform
        ):
            return None
    except (
        ExternalToolError,
        IndexError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):
        return None
    return BuilderImage(
        image_id=image_id,
        repo_digests=tuple(sorted(repo_digests)),
        os=os_name,
        architecture=architecture,
    )


def require_builder_image(
    image: str,
    *,
    contract_digest: str,
    platform: str,
) -> BuilderImage:
    result = inspect_builder_image(
        image,
        contract_digest=contract_digest,
        platform=platform,
    )
    if result is None:
        raise ExternalToolError(
            f"Docker image {image!r} does not match its builder identity"
        )
    return result


def resolve_builder_image(
    image: str,
    *,
    contract_digest: str,
    platform: str,
    build: Callable[[], None],
) -> BuilderImageResolution:
    """Reuse an exact builder image or build and resolve its immutable ID."""

    cached = inspect_builder_image(
        image,
        contract_digest=contract_digest,
        platform=platform,
    )
    if cached is not None:
        return BuilderImageResolution(image=cached, cache_hit=True)
    build()
    return BuilderImageResolution(
        image=require_builder_image(
            image,
            contract_digest=contract_digest,
            platform=platform,
        ),
        cache_hit=False,
    )
