from __future__ import annotations

import gzip
import hashlib
import os
import re
import shlex
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Mapping, Sequence

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.versions import AbiVersion

PREFIX_TOKEN = "@LINUX_TOOLCHAIN_PREFIX@"
DEFAULT_LAUNCHER_NAME = "lxtc"
SHELL_INIT_RELATIVE_PATH = "env/lxtc-shell/.zshrc"
SHELL_INIT = """_lxtc_prompt_label=${LINUX_TOOLCHAIN_SHELL_PROMPT-}
_lxtc_user_rc=${LINUX_TOOLCHAIN_SHELL_USER_RC-}
_lxtc_runtime_library_path=${LINUX_TOOLCHAIN_SHELL_RUNTIME_LIBRARY_PATH-}

case ${LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV_SET-} in
  1)
    ENV=${LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV-}
    export ENV
    ;;
  0) unset ENV ;;
esac
case ${LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR_SET-} in
  1)
    ZDOTDIR=${LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR-}
    export ZDOTDIR
    ;;
  0) unset ZDOTDIR ;;
esac
unset \
  LINUX_TOOLCHAIN_SHELL_PROMPT \
  LINUX_TOOLCHAIN_SHELL_RUNTIME_LIBRARY_PATH \
  LINUX_TOOLCHAIN_SHELL_USER_RC \
  LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV \
  LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV_SET \
  LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR \
  LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR_SET

if [ -n "$_lxtc_user_rc" ] && [ -r "$_lxtc_user_rc" ]; then
  . "$_lxtc_user_rc"
fi
if [ -n "${LINUX_TOOLCHAIN_BINDING-}" ] &&
   [ -r "$LINUX_TOOLCHAIN_BINDING/env/toolchain.env" ]; then
  . "$LINUX_TOOLCHAIN_BINDING/env/toolchain.env"
  _lxtc_prefix=${LINUX_TOOLCHAIN_BINDING%/binding}
  case ${PATH-} in
    "$LINUX_TOOLCHAIN_BINDING/bin":*) _lxtc_path_tail=${PATH#*:} ;;
    "$LINUX_TOOLCHAIN_BINDING/bin") _lxtc_path_tail= ;;
    *) _lxtc_path_tail=${PATH-} ;;
  esac
  PATH="$LINUX_TOOLCHAIN_BINDING/bin:$_lxtc_prefix/tools/bin:$_lxtc_prefix/bin${_lxtc_path_tail:+:$_lxtc_path_tail}"
  export PATH
  unset _lxtc_path_tail _lxtc_prefix
fi
if [ -n "$_lxtc_runtime_library_path" ]; then
  LD_LIBRARY_PATH="$_lxtc_runtime_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export LD_LIBRARY_PATH
fi
case ${PS1-} in
  *:*) PS1="$_lxtc_prompt_label:${PS1#*:}" ;;
  *) PS1="$_lxtc_prompt_label:${PS1-}" ;;
esac
unset _lxtc_prompt_label _lxtc_runtime_library_path _lxtc_user_rc
"""
CONAN_DEFAULT_PROFILE = """# Selected dynamically by the installed lxtc launcher.
{% set host_profile = os.getenv("LINUX_TOOLCHAIN_CONAN_HOST_PROFILE") %}
include({{ host_profile }})
"""
CONAN_DEFAULT_BUILD_PROFILE = """# Selected dynamically by the installed lxtc launcher.
{% set build_profile = os.getenv("LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE") %}
include({{ build_profile }})
"""
_PAYLOAD_MARKER = "__LINUX_TOOLCHAIN_PAYLOAD_BELOW__"
_CONAN_HOME_PREFIX = ".conan2_lxtc_"
_BUNDLE_DIGEST_LENGTH = 16
_RUNTIME_STATE_DIRECTORY = "linux-toolchain"
_RUNTIME_STATE_FILE = "runtime"
_INSTALLATION_RELATIVE_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9/._+@=-]*")


def _installation_relative_path(value: str, context: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or _INSTALLATION_RELATIVE_PATH.fullmatch(value) is None
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ConfigurationError(f"{context} must be a canonical relative path")
    return path


@dataclass(frozen=True)
class LauncherExecutionLayout:
    """Install-relative loader and library layout embedded in ``lxtc``."""

    target_arch: str
    glibc_version: str
    sdk_root: str
    runtime_root: str
    loader: str
    interpreter: str
    sdk_library_dirs: tuple[str, ...]
    runtime_library_dirs: Mapping[str, tuple[str, ...]]

    def validate(self, cxx_runtimes: Sequence[str]) -> None:
        expected_interpreter = {
            "x86_64": "/lib64/ld-linux-x86-64.so.2",
            "aarch64": "/lib/ld-linux-aarch64.so.1",
        }.get(self.target_arch)
        if expected_interpreter is None:
            raise ConfigurationError("launcher target architecture is unsupported")
        AbiVersion.parse(self.glibc_version)
        sdk_root = _installation_relative_path(self.sdk_root, "launcher SDK root")
        runtime_root = _installation_relative_path(
            self.runtime_root, "launcher runtime root"
        )
        loader = _installation_relative_path(self.loader, "launcher SDK loader")
        if not loader.is_relative_to(sdk_root) or loader == sdk_root:
            raise ConfigurationError("launcher SDK loader is outside the SDK root")
        if self.interpreter != expected_interpreter:
            raise ConfigurationError(
                "launcher interpreter does not match the target architecture"
            )
        if not self.sdk_library_dirs or tuple(
            dict.fromkeys(self.sdk_library_dirs)
        ) != tuple(self.sdk_library_dirs):
            raise ConfigurationError(
                "launcher SDK library directories must be non-empty and unique"
            )
        for path in self.sdk_library_dirs:
            library_dir = _installation_relative_path(
                path, "launcher SDK library directory"
            )
            if not library_dir.is_relative_to(sdk_root):
                raise ConfigurationError(
                    "launcher SDK library directory is outside the SDK root"
                )
        if set(self.runtime_library_dirs) != set(cxx_runtimes):
            raise ConfigurationError(
                "launcher runtime library directories do not match available runtimes"
            )
        for kind in cxx_runtimes:
            paths = self.runtime_library_dirs[kind]
            if not paths or tuple(dict.fromkeys(paths)) != tuple(paths):
                raise ConfigurationError(
                    f"launcher {kind} library directories must be non-empty and unique"
                )
            for path in paths:
                library_dir = _installation_relative_path(
                    path, f"launcher {kind} library directory"
                )
                if not library_dir.is_relative_to(runtime_root):
                    raise ConfigurationError(
                        f"launcher {kind} library directory is outside the runtime root"
                    )


_CONAN_HOME_FUNCTIONS = """install_conan_file() {
  source_file=$1
  destination_file=$2
  if [ -L "$destination_file" ] || { [ -e "$destination_file" ] && [ ! -f "$destination_file" ]; }; then
    echo "linux-toolchain: Conan configuration is not a regular file: $destination_file" >&2
    exit 2
  fi
  if [ -e "$destination_file" ]; then
    cmp -s -- "$source_file" "$destination_file" || {
      echo "linux-toolchain: refusing to replace different Conan configuration: $destination_file" >&2
      exit 2
    }
    return
  fi
  conan_temporary_file=$(mktemp "$conan_home/.lxtc-config.XXXXXXXX") || {
    echo "linux-toolchain: cannot create temporary Conan configuration in $conan_home" >&2
    exit 2
  }
  if ! cp -- "$source_file" "$conan_temporary_file" ||
     ! chmod 0644 "$conan_temporary_file"; then
    rm -f -- "$conan_temporary_file"
    echo "linux-toolchain: cannot prepare Conan configuration: $destination_file" >&2
    exit 2
  fi
  if ! mv -- "$conan_temporary_file" "$destination_file"; then
    rm -f -- "$conan_temporary_file"
    echo "linux-toolchain: cannot install Conan configuration: $destination_file" >&2
    exit 2
  fi
  conan_temporary_file=
}

write_conan_info() {
  lxtc_info_home=$1
  lxtc_info_source=$2
  lxtc_info_runtime=$3
  lxtc_info_host_profile=$4
  lxtc_info_build_profile=$5
  lxtc_info_file=$lxtc_info_home/lxtc.info
  if [ ! -f "$lxtc_info_source" ] || [ -L "$lxtc_info_source" ]; then
    echo "linux-toolchain: installed toolchain info is not a regular file: $lxtc_info_source" >&2
    exit 2
  fi
  if [ -L "$lxtc_info_file" ] ||
     { [ -e "$lxtc_info_file" ] && [ ! -f "$lxtc_info_file" ]; }; then
    echo "linux-toolchain: Conan toolchain info is not a regular file: $lxtc_info_file" >&2
    exit 2
  fi
  conan_temporary_file=$(mktemp "$lxtc_info_home/.lxtc-info.XXXXXXXX") || {
    echo "linux-toolchain: cannot create temporary toolchain info in $lxtc_info_home" >&2
    exit 2
  }
  if ! cp -- "$lxtc_info_source" "$conan_temporary_file"; then
    rm -f -- "$conan_temporary_file"
    echo "linux-toolchain: cannot prepare Conan toolchain info: $lxtc_info_file" >&2
    exit 2
  fi
  if [ -n "$lxtc_info_runtime" ] &&
     ! printf 'cxx_runtime.selected=%s\\n' "$lxtc_info_runtime" \
       >>"$conan_temporary_file"; then
    rm -f -- "$conan_temporary_file"
    echo "linux-toolchain: cannot prepare Conan toolchain info: $lxtc_info_file" >&2
    exit 2
  fi
  if ! printf 'conan.home=%s\\nconan.host_profile=%s\\nconan.build_profile=%s\\n' \
       "$lxtc_info_home" "$lxtc_info_host_profile" "$lxtc_info_build_profile" \
       >>"$conan_temporary_file" ||
     ! chmod 0644 "$conan_temporary_file"; then
    rm -f -- "$conan_temporary_file"
    echo "linux-toolchain: cannot prepare Conan toolchain info: $lxtc_info_file" >&2
    exit 2
  fi
  if ! mv -- "$conan_temporary_file" "$lxtc_info_file"; then
    rm -f -- "$conan_temporary_file"
    echo "linux-toolchain: cannot install Conan toolchain info: $lxtc_info_file" >&2
    exit 2
  fi
  conan_temporary_file=
}

prepare_conan_home() {
  conan_binding=$1
  conan_home=$2
  installation_prefix=$3
  case "$conan_home" in
    ""|/) echo "linux-toolchain: unsafe Conan home: $conan_home" >&2; exit 2 ;;
  esac
  case "$conan_home" in
    "$installation_prefix"|"$installation_prefix"/*)
      echo "linux-toolchain: Conan home and installation prefix cannot overlap: $conan_home and $installation_prefix" >&2
      exit 2 ;;
  esac
  case "$installation_prefix" in
    "$conan_home"/*)
      echo "linux-toolchain: Conan home and installation prefix cannot overlap: $conan_home and $installation_prefix" >&2
      exit 2 ;;
  esac
  if [ -L "$conan_home" ] || { [ -e "$conan_home" ] && [ ! -d "$conan_home" ]; }; then
    echo "linux-toolchain: Conan home is not a directory: $conan_home" >&2
    exit 2
  fi
  conan_profiles=$conan_home/profiles
  if [ -L "$conan_profiles" ] || { [ -e "$conan_profiles" ] && [ ! -d "$conan_profiles" ]; }; then
    echo "linux-toolchain: Conan profiles path is not a directory: $conan_profiles" >&2
    exit 2
  fi
  mkdir -p -- "$conan_profiles" || {
    echo "linux-toolchain: cannot create Conan home: $conan_home" >&2
    exit 2
  }
  install_conan_file \
    "$conan_binding/settings_user.yml" \
    "$conan_home/settings_user.yml"
  install_conan_file \
    "$conan_binding/default.profile" \
    "$conan_profiles/default"
  install_conan_file \
    "$conan_binding/lxtc-build.profile" \
    "$conan_profiles/lxtc-build"
  if [ -f "$conan_binding/lxtc-libcxx.profile" ]; then
    install_conan_file \
      "$conan_binding/lxtc-libcxx.profile" \
      "$conan_profiles/lxtc-libcxx"
    install_conan_file \
      "$conan_binding/lxtc-libstdcxx.profile" \
      "$conan_profiles/lxtc-libstdcxx"
  fi
}
"""
_RUNTIME_STATE_READ_FUNCTIONS = """resolve_runtime_state_file() {
  lxtc_runtime_state_required=$1
  lxtc_runtime_state_root=
  if [ -n "${XDG_CONFIG_HOME-}" ]; then
    lxtc_runtime_state_root=$XDG_CONFIG_HOME
  elif [ -n "${HOME-}" ]; then
    lxtc_runtime_state_root=$HOME/.config
  fi
  if [ -z "$lxtc_runtime_state_root" ]; then
    if [ "$lxtc_runtime_state_required" -eq 1 ]; then
      echo "linux-toolchain: HOME or XDG_CONFIG_HOME is required to manage the C++ runtime selection" >&2
      exit 2
    fi
    lxtc_runtime_state_directory=
    lxtc_runtime_state_file=
    return
  fi
  case "$lxtc_runtime_state_root" in
    /*) ;;
    *)
      echo "linux-toolchain: runtime configuration root must be absolute: $lxtc_runtime_state_root" >&2
      exit 2 ;;
  esac
  lxtc_runtime_state_directory=$lxtc_runtime_state_root/linux-toolchain/$RUNTIME_STATE_ID
  lxtc_runtime_state_file=$lxtc_runtime_state_directory/runtime
}

runtime_profile_for() {
  case "$1" in
    libc++) lxtc_runtime_profile=lxtc-libcxx ;;
    libstdc++) lxtc_runtime_profile=lxtc-libstdcxx ;;
    *)
      echo "linux-toolchain: unsupported C++ runtime: $1" >&2
      exit 2 ;;
  esac
}

load_runtime_selection() {
  lxtc_runtime_persistent=$1
  runtime_profile_for "$lxtc_runtime_persistent"
  resolve_runtime_state_file 0
  [ -n "$lxtc_runtime_state_file" ] || return
  if [ -L "$lxtc_runtime_state_file" ] ||
     { [ -e "$lxtc_runtime_state_file" ] && [ ! -f "$lxtc_runtime_state_file" ]; }; then
    echo "linux-toolchain: C++ runtime selection is not a regular file: $lxtc_runtime_state_file" >&2
    exit 2
  fi
  if [ -f "$lxtc_runtime_state_file" ]; then
    IFS= read -r lxtc_runtime_persistent < "$lxtc_runtime_state_file" || {
      echo "linux-toolchain: cannot read C++ runtime selection: $lxtc_runtime_state_file" >&2
      exit 2
    }
    runtime_profile_for "$lxtc_runtime_persistent"
  fi
}
"""

_RUNTIME_STATE_WRITE_FUNCTIONS = """write_runtime_selection() {
  lxtc_runtime_value=$1
  resolve_runtime_state_file 1
  if [ -L "$lxtc_runtime_state_directory" ] ||
     { [ -e "$lxtc_runtime_state_directory" ] && [ ! -d "$lxtc_runtime_state_directory" ]; }; then
    echo "linux-toolchain: runtime configuration is not a directory: $lxtc_runtime_state_directory" >&2
    exit 2
  fi
  mkdir -p -- "$lxtc_runtime_state_directory" || {
    echo "linux-toolchain: cannot create runtime configuration: $lxtc_runtime_state_directory" >&2
    exit 2
  }
  if [ -L "$lxtc_runtime_state_file" ] ||
     { [ -e "$lxtc_runtime_state_file" ] && [ ! -f "$lxtc_runtime_state_file" ]; }; then
    echo "linux-toolchain: C++ runtime selection is not a regular file: $lxtc_runtime_state_file" >&2
    exit 2
  fi
  lxtc_runtime_temporary_file=$(mktemp "$lxtc_runtime_state_directory/.runtime.XXXXXXXX") || {
    echo "linux-toolchain: cannot create temporary C++ runtime selection in $lxtc_runtime_state_directory" >&2
    exit 2
  }
  if ! printf '%s\\n' "$lxtc_runtime_value" >"$lxtc_runtime_temporary_file" ||
     ! chmod 0644 "$lxtc_runtime_temporary_file"; then
    rm -f -- "$lxtc_runtime_temporary_file"
    echo "linux-toolchain: cannot prepare C++ runtime selection: $lxtc_runtime_state_file" >&2
    exit 2
  fi
  if ! mv -- "$lxtc_runtime_temporary_file" "$lxtc_runtime_state_file"; then
    rm -f -- "$lxtc_runtime_temporary_file"
    echo "linux-toolchain: cannot install C++ runtime selection: $lxtc_runtime_state_file" >&2
    exit 2
  fi
  lxtc_runtime_temporary_file=
}

reset_runtime_selection() {
  resolve_runtime_state_file 1
  if [ -L "$lxtc_runtime_state_file" ] ||
     { [ -e "$lxtc_runtime_state_file" ] && [ ! -f "$lxtc_runtime_state_file" ]; }; then
    echo "linux-toolchain: C++ runtime selection is not a regular file: $lxtc_runtime_state_file" >&2
    exit 2
  fi
  if [ -f "$lxtc_runtime_state_file" ]; then
    rm -- "$lxtc_runtime_state_file" || {
      echo "linux-toolchain: cannot reset C++ runtime selection: $lxtc_runtime_state_file" >&2
      exit 2
    }
  fi
}
"""


def _bundle_digest(bundle_id: str) -> str:
    return hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()[:_BUNDLE_DIGEST_LENGTH]


def default_conan_home_name(bundle_id: str) -> str:
    return f"{_CONAN_HOME_PREFIX}{_bundle_digest(bundle_id)}"


def default_runtime_state_file(
    bundle_id: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    selected_environment = os.environ if environment is None else environment
    raw_root = selected_environment.get("XDG_CONFIG_HOME")
    if raw_root is None or not raw_root:
        raw_home = selected_environment.get("HOME")
        if raw_home is None or not raw_home:
            return None
        root = Path(raw_home) / ".config"
    else:
        root = Path(raw_root)
    if not root.is_absolute():
        raise ConfigurationError(f"runtime configuration root must be absolute: {root}")
    return (
        root
        / _RUNTIME_STATE_DIRECTORY
        / _bundle_digest(bundle_id)
        / _RUNTIME_STATE_FILE
    )


class _ProgressReader:
    def __init__(self, source: BinaryIO, report: Callable[[int], None]) -> None:
        self._source = source
        self._report = report

    def read(self, size: int = -1) -> bytes:
        content = self._source.read(size)
        if content:
            self._report(len(content))
        return content


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


def write_payload_archive(
    payload: Path,
    archive: Path,
    *,
    trees: Sequence[tuple[Path, str]] = (),
    progress: Callable[[int, int], None] | None = None,
    header: Callable[[int], bytes] | None = None,
) -> int:
    entries: dict[str, Path] = {
        (
            "payload"
            if path == payload
            else (
                PurePosixPath("payload") / path.relative_to(payload).as_posix()
            ).as_posix()
        ): path
        for path in (payload, *payload.rglob("*"))
    }
    for source, destination in trees:
        relative = PurePosixPath(destination)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ConfigurationError(
                f"bundle archive destination is invalid: {destination!r}"
            )
        if not source.is_dir() or source.is_symlink():
            raise ConfigurationError(f"bundle archive tree is invalid: {source}")
        archive_root = PurePosixPath("payload") / relative
        for path in (source, *source.rglob("*")):
            name = (
                archive_root
                if path == source
                else archive_root / path.relative_to(source).as_posix()
            ).as_posix()
            if name in entries:
                raise ConfigurationError(f"duplicate bundle archive entry: {name}")
            entries[name] = path
    paths = tuple(sorted(entries.items()))
    total = 0
    for _, path in paths:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    completed = 0

    def report(size: int) -> None:
        nonlocal completed
        completed += size
        if progress is not None and completed < total:
            progress(completed, total)

    if progress is not None:
        progress(0, total)

    def write_compressed(raw: BinaryIO) -> None:
        with gzip.GzipFile(
            fileobj=raw, mode="wb", compresslevel=6, filename="", mtime=0
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.GNU_FORMAT,
                dereference=False,
            ) as output:
                for name, path in paths:
                    info = output.gettarinfo(str(path), arcname=name)
                    info = _tar_filter(info)
                    if info.isreg():
                        with path.open("rb") as source:
                            output.addfile(info, _ProgressReader(source, report))
                    else:
                        output.addfile(info)

    with archive.open("w+b") as raw:
        initial_header = header(0) if header is not None else b""
        raw.write(initial_header)
        write_compressed(raw)
        if header is not None:
            payload_bytes = raw.tell() - len(initial_header)
            final_header = header(payload_bytes)
            if len(final_header) != len(initial_header):
                raise ConfigurationError(
                    "bundle installer header must have a fixed size"
                )
            raw.seek(0)
            raw.write(final_header)
        raw.flush()
        os.fsync(raw.fileno())
    if progress is not None:
        progress(total, total)
    return len(paths)


def template_binding(
    payload: Path,
    binding: Path,
    *,
    artifact_paths: Mapping[Path, str] | None = None,
) -> tuple[str, ...]:
    replacements: list[tuple[bytes, bytes]] = [
        (str(payload.resolve()).encode(), PREFIX_TOKEN.encode())
    ]
    for source, destination in (artifact_paths or {}).items():
        relative = PurePosixPath(destination)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ConfigurationError(
                f"bundle artifact template destination is invalid: {destination!r}"
            )
        replacements.append(
            (
                str(source.resolve()).encode(),
                f"{PREFIX_TOKEN}/{relative.as_posix()}".encode(),
            )
        )
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    changed: list[str] = []
    for path in sorted(binding.rglob("*"), key=lambda item: item.as_posix()):
        if not _regular_file(path):
            continue
        content = path.read_bytes()
        updated = content
        for source, target in replacements:
            updated = updated.replace(source, target)
        if updated == content:
            continue
        if b"\0" in content:
            raise ConfigurationError(
                f"bundle path substitution reached a binary file: {path}"
            )
        path.write_bytes(updated)
        changed.append(path.relative_to(payload).as_posix())
    if not changed:
        raise ConfigurationError("bundle binding contains no relocatable paths")
    return tuple(changed)


def relocate_binding_links(
    payload: Path,
    binding: Path,
    *,
    source_binding: Path,
    artifact_paths: Mapping[Path, str],
) -> tuple[str, ...]:
    """Retarget binding symlinks from producer roots into the bundle payload."""

    if source_binding.is_symlink() or not source_binding.is_dir():
        raise ConfigurationError(
            f"bundle binding source is not a directory: {source_binding}"
        )
    if binding.is_symlink() or not binding.is_dir():
        raise ConfigurationError(
            f"bundle binding destination is not a directory: {binding}"
        )
    payload_root = payload.resolve()
    source_root = source_binding.resolve()
    binding_root = binding.resolve()
    mapped_roots: list[tuple[Path, Path]] = []
    for source, destination in artifact_paths.items():
        relative = PurePosixPath(destination)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ConfigurationError(
                f"bundle artifact link destination is invalid: {destination!r}"
            )
        if source.is_symlink() or not source.is_dir():
            raise ConfigurationError(f"bundle artifact link root is invalid: {source}")
        mapped_roots.append((source.resolve(), payload_root.joinpath(*relative.parts)))
    mapped_roots.sort(key=lambda item: len(item[0].parts), reverse=True)

    records: list[tuple[Path, str, Path]] = []
    for source_link in sorted(
        (path for path in source_root.rglob("*") if path.is_symlink()),
        key=lambda path: path.as_posix(),
    ):
        relative = source_link.relative_to(source_root)
        destination_link = binding_root / relative
        if not destination_link.is_symlink():
            raise ConfigurationError(
                f"bundle binding link was not copied as a symlink: {relative}"
            )
        try:
            raw_target = os.readlink(source_link)
            actual_target = source_link.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ConfigurationError(
                f"bundle binding contains a dangling symlink: {source_link}"
            ) from error
        records.append((relative, raw_target, actual_target))

    changed: list[str] = []
    for relative, raw_target, actual_target in records:
        source_link = source_root / relative
        destination_link = binding_root / relative
        internal_target: Path | None = None
        try:
            internal_relative = actual_target.relative_to(source_root)
        except ValueError:
            pass
        else:
            internal_target = binding_root / internal_relative

        artifact_target: Path | None = None
        if internal_target is None:
            for source_root_path, destination_root in mapped_roots:
                try:
                    artifact_relative = actual_target.relative_to(source_root_path)
                except ValueError:
                    continue
                artifact_target = destination_root / artifact_relative
                break
        if internal_target is None and artifact_target is None:
            raise ConfigurationError(
                "bundle binding symlink target is outside the binding and declared "
                f"artifact roots: {relative} -> {actual_target}"
            )

        raw_path = Path(raw_target)
        lexical_target = Path(
            os.path.abspath(
                raw_path if raw_path.is_absolute() else source_link.parent / raw_path
            )
        )
        keep_internal = not raw_path.is_absolute() and lexical_target.is_relative_to(
            source_root
        )
        if keep_internal:
            continue

        target = internal_target or artifact_target
        assert target is not None
        replacement = os.path.relpath(target, start=destination_link.parent)
        destination_link.unlink()
        destination_link.symlink_to(replacement)
        changed.append(destination_link.relative_to(payload_root).as_posix())
    return tuple(changed)


def render_launcher(
    *,
    bundle_id: str,
    conan: bool,
    execution: LauncherExecutionLayout,
    cxx_runtimes: Sequence[str] = (),
    default_cxx_runtime: str | None = None,
) -> str:
    if any(kind not in {"libstdc++", "libc++"} for kind in cxx_runtimes):
        raise ConfigurationError("launcher C++ runtime set is unsupported")
    if cxx_runtimes and default_cxx_runtime is None:
        raise ConfigurationError("launcher with C++ runtimes requires a default")
    if default_cxx_runtime is not None and default_cxx_runtime not in cxx_runtimes:
        raise ConfigurationError(
            "launcher default C++ runtime is not in the available runtime set"
        )
    runtime_switch = {"libstdc++", "libc++"}.issubset(cxx_runtimes)
    if runtime_switch and default_cxx_runtime != "libstdc++":
        raise ConfigurationError(
            "launcher runtime switching requires libstdc++ as the default"
        )
    execution.validate(cxx_runtimes)

    def installed_path(relative: str) -> str:
        return f"${{PREFIX}}/{relative}"

    sdk_loader = f'"{installed_path(execution.loader)}"'
    sdk_interpreter = shlex.quote(execution.interpreter)
    sdk_root = f'"{installed_path(execution.sdk_root)}"'
    runtime_root = f'"{installed_path(execution.runtime_root)}"'
    sdk_library_path = (
        '"'
        + ":".join(installed_path(path) for path in execution.sdk_library_dirs)
        + '"'
    )
    runtime_library_cases = []
    for kind in cxx_runtimes:
        library_path = ":".join(
            installed_path(path) for path in execution.runtime_library_dirs[kind]
        )
        runtime_library_cases.append(
            f'    {kind}) lxtc_runtime_library_path="{library_path}" ;;'
        )
    if not runtime_library_cases:
        runtime_library_cases.append("    '') lxtc_runtime_library_path= ;;")
    runtime_library_function = (
        "runtime_library_path_for() {\n"
        '  case "$1" in\n' + "\n".join(runtime_library_cases) + "\n"
        "    *)\n"
        '      echo "linux-toolchain: unsupported C++ runtime: $1" >&2\n'
        "      exit 2 ;;\n"
        "  esac\n"
        "}\n"
    )
    kernel_loader_start = execution.target_arch == "aarch64" and AbiVersion.parse(
        execution.glibc_version
    ) < AbiVersion.parse("2.36")
    runtime_functions = (
        _RUNTIME_STATE_READ_FUNCTIONS + _RUNTIME_STATE_WRITE_FUNCTIONS
        if runtime_switch
        else ""
    )
    conan_functions = _CONAN_HOME_FUNCTIONS if conan else ""
    default_runtime = shlex.quote(default_cxx_runtime or "")
    runtime_state_declaration = (
        f"readonly RUNTIME_STATE_ID={shlex.quote(_bundle_digest(bundle_id))}\n"
        if runtime_switch
        else ""
    )
    conan_environment = (
        """if [ -f "$BINDING/conan/conan-home" ]; then
  IFS= read -r CONAN_HOME < "$BINDING/conan/conan-home"
  IFS= read -r CONAN_BUILD_PROFILE < "$BINDING/conan/build-profile"
  export CONAN_HOME
  export CONAN_DEFAULT_PROFILE=default
  export CONAN_DEFAULT_BUILD_PROFILE=lxtc-build
  export LINUX_TOOLCHAIN_CONAN_HOST_PROFILE="$BINDING/conan/host.profile"
  export LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE="$CONAN_BUILD_PROFILE"
  lxtc_managed_conan_build_profile=0
  if [ "$CONAN_BUILD_PROFILE" = "$BINDING/conan/build.profile" ]; then
    lxtc_managed_conan_build_profile=1
  fi
fi
"""
        if conan
        else ""
    )
    conan_info = (
        """  printf 'conan.home=%s\\n' "$CONAN_HOME"
  printf 'conan.host_profile=%s\\n' "$CONAN_DEFAULT_PROFILE"
  printf 'conan.build_profile=%s\\n' "$CONAN_DEFAULT_BUILD_PROFILE"
"""
        if conan
        else ""
    )
    select_conan_runtime = (
        """select_conan_runtime() {
  case "$LINUX_TOOLCHAIN_CXX_RUNTIME" in
    libc++)
      export CONAN_DEFAULT_PROFILE=lxtc-libcxx
      export LINUX_TOOLCHAIN_CONAN_HOST_PROFILE="$BINDING/conan/lxtc-libcxx.profile"
      if [ "$lxtc_managed_conan_build_profile" -eq 1 ]; then
        export LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE="$BINDING/conan/build-libcxx.profile"
      fi
      ;;
    libstdc++)
      export CONAN_DEFAULT_PROFILE=lxtc-libstdcxx
      export LINUX_TOOLCHAIN_CONAN_HOST_PROFILE="$BINDING/conan/lxtc-libstdcxx.profile"
      if [ "$lxtc_managed_conan_build_profile" -eq 1 ]; then
        export LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE="$BINDING/conan/build-libstdcxx.profile"
      fi
      ;;
  esac
}
"""
        if conan and runtime_switch
        else ""
    )
    refresh_conan_info = (
        """refresh_conan_info() {
  lxtc_info_runtime=$1
  runtime_profile_for "$lxtc_info_runtime"
  if [ -d "$CONAN_HOME" ] && [ ! -L "$CONAN_HOME" ]; then
    write_conan_info \
      "$CONAN_HOME" \
      "$BINDING/env/toolchain.info" \
      "$lxtc_info_runtime" \
      "$lxtc_runtime_profile" \
      lxtc-build
  fi
}
"""
        if conan and runtime_switch
        else ""
    )
    runtime_management = (
        f"""if [ "${{1-}}" = runtime ]; then
  case "${{2-}}" in
    show)
      if [ "$#" -ne 2 ]; then
        echo "usage: $0 runtime show" >&2
        exit 2
      fi
      load_runtime_selection {default_runtime}
      printf '%s\\n' "$lxtc_runtime_persistent"
      exit 0
      ;;
    set)
      if [ "$#" -ne 3 ]; then
        echo "usage: $0 runtime set libstdc++|libc++" >&2
        exit 2
      fi
      runtime_profile_for "$3"
      write_runtime_selection "$3"
      {('refresh_conan_info "$3"' if conan else ":")}
      printf '%s\\n' "$3"
      exit 0
      ;;
    reset)
      if [ "$#" -ne 2 ]; then
        echo "usage: $0 runtime reset" >&2
        exit 2
      fi
      reset_runtime_selection
      {("refresh_conan_info " + default_runtime) if conan else ":"}
      printf '%s\\n' {default_runtime}
      exit 0
      ;;
    *)
      echo "usage: $0 runtime show|set|reset" >&2
      exit 2 ;;
  esac
fi
"""
        if runtime_switch
        else f"""if [ "${{1-}}" = runtime ]; then
  if [ "$#" -eq 2 ] && [ "$2" = show ]; then
    printf '%s\\n' {default_runtime}
    exit 0
  fi
  echo "linux-toolchain: C++ runtime switching is not available" >&2
  exit 2
fi
"""
    )
    runtime_initialization = (
        f"""load_runtime_selection {default_runtime}
if [ -z "${{LINUX_TOOLCHAIN_CXX_RUNTIME+x}}" ]; then
  LINUX_TOOLCHAIN_CXX_RUNTIME=$lxtc_runtime_persistent
  export LINUX_TOOLCHAIN_CXX_RUNTIME
fi
if [ "${{1-}}" = "--runtime" ]; then
  if [ "$#" -lt 3 ]; then
    echo "usage: $0 --runtime libstdc++|libc++ COMMAND [ARG ...]" >&2
    exit 2
  fi
  runtime_profile_for "$2"
  LINUX_TOOLCHAIN_CXX_RUNTIME=$2
  export LINUX_TOOLCHAIN_CXX_RUNTIME
  shift 2
fi
case "$LINUX_TOOLCHAIN_CXX_RUNTIME" in
  libstdc++|libc++) ;;
  *)
    echo "linux-toolchain: unsupported C++ runtime: $LINUX_TOOLCHAIN_CXX_RUNTIME" >&2
    exit 2 ;;
esac
{("select_conan_runtime" if conan else "")}
"""
        if runtime_switch
        else """if [ "${1-}" = "--runtime" ]; then
  echo "linux-toolchain: C++ runtime switching is not available" >&2
  exit 2
fi
"""
    )
    runtime_info = ""
    if default_cxx_runtime is not None:
        selected_runtime = (
            '"$LINUX_TOOLCHAIN_CXX_RUNTIME"' if runtime_switch else default_cxx_runtime
        )
        runtime_info = f"  printf 'cxx_runtime.selected=%s\\n' {selected_runtime}\n"
    if conan:
        persistent_runtime = (
            '"$lxtc_runtime_persistent"' if runtime_switch else default_runtime
        )
        persistent_profile = '"$lxtc_runtime_profile"' if runtime_switch else "default"
        conan_init = f"""if [ "${{1-}}" = conan-init ]; then
  if [ "$#" -ne 1 ]; then
    echo "usage: $0 conan-init" >&2
    exit 2
  fi
  prepare_conan_home "$BINDING/conan" "$CONAN_HOME" "$PREFIX"
  write_conan_info \
    "$CONAN_HOME" \
    "$BINDING/env/toolchain.info" \
    {persistent_runtime} \
    {persistent_profile} \
    lxtc-build
  printf '%s\\n' "$CONAN_HOME"
  exit 0
fi
"""
    else:
        conan_init = """if [ "${1-}" = conan-init ]; then
  echo "linux-toolchain: Conan integration is not installed" >&2
  exit 2
fi
"""
    run_runtime = (
        '"$LINUX_TOOLCHAIN_CXX_RUNTIME"' if runtime_switch else default_runtime
    )
    kernel_run = (
        """  if [ ! -f "$SDK_INTERPRETER" ]; then
    echo "linux-toolchain: host interpreter mount point is missing: $SDK_INTERPRETER" >&2
    exit 2
  fi
  lxtc_run_unshare=$(command -v unshare 2>/dev/null) || {
    echo "linux-toolchain: lxtc run requires unshare for this AArch64 glibc" >&2
    exit 2
  }
  lxtc_run_mount=$(command -v mount 2>/dev/null) || {
    echo "linux-toolchain: lxtc run requires mount for this AArch64 glibc" >&2
    exit 2
  }
  lxtc_run_shell=$(command -v sh 2>/dev/null) || {
    echo "linux-toolchain: lxtc run requires a POSIX shell" >&2
    exit 2
  }
  exec "$lxtc_run_unshare" --user --map-root-user --mount \
    "$lxtc_run_shell" -eu -c '
      lxtc_mount=$1
      lxtc_loader=$2
      lxtc_interpreter=$3
      lxtc_library_path=$4
      lxtc_program=$5
      shift 5
      "$lxtc_mount" --make-rprivate /
      "$lxtc_mount" --bind "$lxtc_loader" "$lxtc_interpreter"
      LD_LIBRARY_PATH=$lxtc_library_path
      export LD_LIBRARY_PATH
      exec "$lxtc_program" "$@"
    ' lxtc-runner "$lxtc_run_mount" "$SDK_LOADER" "$SDK_INTERPRETER" \
      "$lxtc_run_library_path" "$lxtc_run_program" "$@"
"""
        if kernel_loader_start
        else """  exec "$SDK_LOADER" --inhibit-cache --library-path \
    "$lxtc_run_library_path" "$lxtc_run_program" "$@"
"""
    )
    run_command = f"""if [ "${{1-}}" = run ]; then
  if [ "$#" -lt 2 ]; then
    echo "usage: $0 run EXECUTABLE [ARG ...]" >&2
    exit 2
  fi
  shift
  lxtc_run_program=$1
  shift
  case "$lxtc_run_program" in
    */*) ;;
    *)
      lxtc_run_resolved=$(command -v "$lxtc_run_program" 2>/dev/null) || {{
        echo "linux-toolchain: executable is not available: $lxtc_run_program" >&2
        exit 2
      }}
      case "$lxtc_run_resolved" in
        */*) lxtc_run_program=$lxtc_run_resolved ;;
        *)
          echo "linux-toolchain: command is not an executable file: $lxtc_run_program" >&2
          exit 2 ;;
      esac
      ;;
  esac
  lxtc_run_name=${{lxtc_run_program##*/}}
  lxtc_run_directory=${{lxtc_run_program%/*}}
  if [ -z "$lxtc_run_directory" ]; then
    lxtc_run_directory=/
  fi
  lxtc_run_directory=$(CDPATH= cd -P "$lxtc_run_directory" 2>/dev/null && pwd) || {{
    echo "linux-toolchain: cannot resolve executable directory: $lxtc_run_program" >&2
    exit 2
  }}
  case "$lxtc_run_directory" in
    *:*)
      echo "linux-toolchain: executable directory cannot contain ':': $lxtc_run_directory" >&2
      exit 2 ;;
  esac
  lxtc_run_program=$lxtc_run_directory/$lxtc_run_name
  if [ ! -f "$lxtc_run_program" ] || [ ! -x "$lxtc_run_program" ]; then
    echo "linux-toolchain: executable is not a regular executable file: $lxtc_run_program" >&2
    exit 2
  fi

  lxtc_run_runtime={run_runtime}
  runtime_library_path_for "$lxtc_run_runtime"
  if [ -n "$lxtc_run_runtime" ]; then
    LINUX_TOOLCHAIN_CXX_RUNTIME=$lxtc_run_runtime
    export LINUX_TOOLCHAIN_CXX_RUNTIME
  fi
  lxtc_run_library_path="${{lxtc_runtime_library_path:+$lxtc_runtime_library_path:}}$SDK_LIBRARY_PATH:$lxtc_run_directory"
  unset LD_AUDIT LD_LIBRARY_PATH LD_PRELOAD

  if ! lxtc_run_closure=$("$SDK_LOADER" --inhibit-cache --library-path \
      "$lxtc_run_library_path" --list "$lxtc_run_program" 2>&1); then
    echo "linux-toolchain: SDK loader cannot resolve the executable" >&2
    printf '%s\n' "$lxtc_run_closure" >&2
    exit 1
  fi
  lxtc_run_escape=
  while IFS= read -r lxtc_run_line; do
    case "$lxtc_run_line" in
      *"not found"*) lxtc_run_escape=$lxtc_run_line; break ;;
      *" => "*) lxtc_run_candidate=${{lxtc_run_line#* => }} ;;
      *) lxtc_run_candidate=$lxtc_run_line ;;
    esac
    lxtc_run_candidate=${{lxtc_run_candidate#"${{lxtc_run_candidate%%[![:space:]]*}}"}}
    lxtc_run_candidate=${{lxtc_run_candidate%%" ("*}}
    case "$lxtc_run_candidate" in
      /*) ;;
      *) continue ;;
    esac
    case "$lxtc_run_candidate" in
      "$SDK_SYSROOT"|"$SDK_SYSROOT"/*) continue ;;
    esac
    lxtc_run_selected=0
    lxtc_run_saved_ifs=$IFS
    IFS=:
    for lxtc_run_root in $lxtc_runtime_library_path; do
      case "$lxtc_run_candidate" in
        "$lxtc_run_root"|"$lxtc_run_root"/*)
          lxtc_run_selected=1
          break ;;
      esac
    done
    IFS=$lxtc_run_saved_ifs
    if [ "$lxtc_run_selected" -eq 1 ]; then
      continue
    fi
    case "$lxtc_run_candidate" in
      "$MANAGED_RUNTIME_ROOT"|"$MANAGED_RUNTIME_ROOT"/*|\
      /lib*/*|/usr/lib*/*|/usr/local/lib*/*)
        lxtc_run_escape=$lxtc_run_line
        break ;;
    esac
  done <<EOF
$lxtc_run_closure
EOF
  if [ -n "$lxtc_run_escape" ]; then
    echo "linux-toolchain: loader closure escaped the selected SDK/runtime" >&2
    printf '%s\n' "$lxtc_run_escape" >&2
    exit 1
  fi
{kernel_run}fi
"""
    shell_runtime = (
        '"$LINUX_TOOLCHAIN_CXX_RUNTIME"' if runtime_switch else default_runtime
    )
    shell_command = f"""if [ "${{1-}}" = shell ]; then
  if [ "$#" -ne 1 ]; then
    echo "usage: $0 shell" >&2
    exit 2
  fi
  lxtc_info_value() {{
    lxtc_info_lookup=$1
    while IFS='=' read -r lxtc_info_name lxtc_info_result; do
      if [ "$lxtc_info_name" = "$lxtc_info_lookup" ]; then
        printf '%s\\n' "$lxtc_info_result"
        return 0
      fi
    done <"$BINDING/env/toolchain.info"
    return 1
  }}
  lxtc_shell_compiler=$(lxtc_info_value compiler.family) || {{
    echo "linux-toolchain: installed toolchain info has no compiler family" >&2
    exit 2
  }}
  lxtc_shell_compiler_version=$(lxtc_info_value compiler.version) || {{
    echo "linux-toolchain: installed toolchain info has no compiler version" >&2
    exit 2
  }}
  lxtc_shell_target=$(lxtc_info_value target.triplet) || {{
    echo "linux-toolchain: installed toolchain info has no target triplet" >&2
    exit 2
  }}
  lxtc_shell_libc=$(lxtc_info_value libc.family) || {{
    echo "linux-toolchain: installed toolchain info has no libc family" >&2
    exit 2
  }}
  lxtc_shell_libc_version=$(lxtc_info_value libc.version) || {{
    echo "linux-toolchain: installed toolchain info has no libc version" >&2
    exit 2
  }}
  lxtc_shell_runtime={shell_runtime}
  if [ -n "$lxtc_shell_runtime" ]; then
    LINUX_TOOLCHAIN_CXX_RUNTIME=$lxtc_shell_runtime
    export LINUX_TOOLCHAIN_CXX_RUNTIME
  fi
  runtime_library_path_for "$lxtc_shell_runtime"
  LINUX_TOOLCHAIN_SHELL_RUNTIME_LIBRARY_PATH=$lxtc_runtime_library_path
  export LINUX_TOOLCHAIN_SHELL_RUNTIME_LIBRARY_PATH
  lxtc_shell_runtime_display=${{lxtc_shell_runtime:-none}}
  case "$lxtc_shell_compiler" in
    clang)
      if [ -n "$lxtc_shell_runtime" ]; then
        lxtc_shell_prompt="(lxtc $lxtc_shell_compiler-$lxtc_shell_compiler_version $lxtc_shell_runtime)"
      else
        lxtc_shell_prompt="(lxtc $lxtc_shell_compiler-$lxtc_shell_compiler_version)"
      fi
      ;;
    *)
      lxtc_shell_prompt="(lxtc $lxtc_shell_compiler-$lxtc_shell_compiler_version)"
      ;;
  esac

  lxtc_shell=${{SHELL:-/bin/sh}}
  case "$lxtc_shell" in
    */*) ;;
    *)
      lxtc_shell_path=$(command -v "$lxtc_shell") || {{
        echo "linux-toolchain: interactive shell is not available: $lxtc_shell" >&2
        exit 2
      }}
      lxtc_shell=$lxtc_shell_path
      ;;
  esac
  if [ ! -f "$lxtc_shell" ] || [ ! -x "$lxtc_shell" ]; then
    echo "linux-toolchain: interactive shell is not executable: $lxtc_shell" >&2
    exit 2
  fi
  lxtc_shell_init="$BINDING/{SHELL_INIT_RELATIVE_PATH}"
  if [ ! -f "$lxtc_shell_init" ]; then
    echo "linux-toolchain: interactive shell initialization is missing: $lxtc_shell_init" >&2
    exit 2
  fi

  printf 'LXTC shell\\n' >&2
  printf '    compiler: %s %s\\n' \
    "$lxtc_shell_compiler" "$lxtc_shell_compiler_version" >&2
  printf '    target:   %s\\n' "$lxtc_shell_target" >&2
  printf '    libc:     %s %s\\n' \
    "$lxtc_shell_libc" "$lxtc_shell_libc_version" >&2
  printf '    runtime:  %s\\n\\n' "$lxtc_shell_runtime_display" >&2
  printf "  Type 'exit' to leave.\\n\\n" >&2

  LINUX_TOOLCHAIN_SHELL_PROMPT=$lxtc_shell_prompt
  export LINUX_TOOLCHAIN_SHELL_PROMPT
  lxtc_shell_name=${{lxtc_shell##*/}}
  case "$lxtc_shell_name" in
    bash)
      LINUX_TOOLCHAIN_SHELL_USER_RC=
      if [ -n "${{HOME-}}" ]; then
        LINUX_TOOLCHAIN_SHELL_USER_RC=$HOME/.bashrc
      fi
      export LINUX_TOOLCHAIN_SHELL_USER_RC
      exec "$lxtc_shell" --rcfile "$lxtc_shell_init" -i
      ;;
    zsh)
      LINUX_TOOLCHAIN_SHELL_USER_RC=
      if [ -n "${{ZDOTDIR-}}" ]; then
        LINUX_TOOLCHAIN_SHELL_USER_RC=$ZDOTDIR/.zshrc
      elif [ -n "${{HOME-}}" ]; then
        LINUX_TOOLCHAIN_SHELL_USER_RC=$HOME/.zshrc
      fi
      if [ "${{ZDOTDIR+x}}" = x ]; then
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR_SET=1
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR=$ZDOTDIR
        export LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR
      else
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR_SET=0
      fi
      export \
        LINUX_TOOLCHAIN_SHELL_USER_RC \
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ZDOTDIR_SET
      ZDOTDIR=${{lxtc_shell_init%/*}}
      export ZDOTDIR
      exec "$lxtc_shell" -i
      ;;
    sh|dash|ash|ksh|mksh)
      LINUX_TOOLCHAIN_SHELL_USER_RC=${{ENV-}}
      if [ "${{ENV+x}}" = x ]; then
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV_SET=1
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV=$ENV
        export LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV
      else
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV_SET=0
      fi
      export \
        LINUX_TOOLCHAIN_SHELL_USER_RC \
        LINUX_TOOLCHAIN_SHELL_ORIGINAL_ENV_SET
      ENV=$lxtc_shell_init
      export ENV
      exec "$lxtc_shell" -i
      ;;
    *)
      echo "linux-toolchain: unsupported interactive shell: $lxtc_shell_name" >&2
      exit 2
      ;;
  esac
fi
"""
    return f"""#!/bin/sh
set -eu
readonly PREFIX='{PREFIX_TOKEN}'
readonly BINDING="$PREFIX/binding"
readonly BUILD_TOOLS="$PREFIX/tools/bin"
readonly SDK_LOADER={sdk_loader}
readonly SDK_INTERPRETER={sdk_interpreter}
readonly SDK_SYSROOT={sdk_root}
readonly SDK_LIBRARY_PATH={sdk_library_path}
readonly MANAGED_RUNTIME_ROOT={runtime_root}
{runtime_state_declaration}\
export LINUX_TOOLCHAIN_BINDING="$BINDING"
PATH="$BUILD_TOOLS${{PATH:+:$PATH}}"
export PATH
. "$BINDING/env/toolchain.env"
{runtime_library_function}{runtime_functions}{conan_functions}{conan_environment}{select_conan_runtime}{refresh_conan_info}{runtime_management}{runtime_initialization}{conan_init}if [ "${{1-}}" = info ]; then
  if [ "$#" -ne 1 ]; then
    echo "usage: $0 info" >&2
    exit 2
  fi
  cat "$BINDING/env/toolchain.info"
{runtime_info}{conan_info}  exit 0
fi
{run_command}{shell_command}\
if [ "${{1-}}" = "--" ]; then
  shift
fi
if [ "$#" -eq 0 ]; then
  echo "linux-toolchain: launcher requires a command" >&2
  exit 2
fi
exec "$@"
"""


def render_installer_header(
    *,
    host_arch: str,
    host_floor: str,
    target_arch: str,
    target_floor: str,
    bundle_id: str,
    default_installation_name: str,
    conan: bool,
    cxx_runtimes: Sequence[str],
    default_cxx_runtime: str,
    payload_bytes: int,
) -> bytes:
    conan_flag = "1" if conan else "0"
    runtime_switch = {"libstdc++", "libc++"}.issubset(cxx_runtimes)
    runtime_switch_flag = "1" if runtime_switch else "0"
    conan_home_name = default_conan_home_name(bundle_id)
    conan_functions = _CONAN_HOME_FUNCTIONS if conan else ""
    runtime_functions = (
        _RUNTIME_STATE_READ_FUNCTIONS if conan and runtime_switch else ""
    )
    runtime_state_declaration = (
        f"RUNTIME_STATE_ID={shlex.quote(_bundle_digest(bundle_id))}\n"
        if conan and runtime_switch
        else ""
    )
    template = f"""#!/bin/sh
set -eu
EXPECTED_ARCH={shlex.quote(host_arch)}
EXPECTED_GLIBC={shlex.quote(host_floor)}
TARGET_ARCH={shlex.quote(target_arch)}
TARGET_GLIBC={shlex.quote(target_floor)}
DEFAULT_CXX_RUNTIME={shlex.quote(default_cxx_runtime)}
CONAN_HOME_NAME={shlex.quote(conan_home_name)}
DEFAULT_LAUNCHER={shlex.quote(DEFAULT_LAUNCHER_NAME)}
DEFAULT_INSTALL_NAME={shlex.quote(default_installation_name)}
HAS_CONAN={conan_flag}
RUNTIME_SWITCH={runtime_switch_flag}
{runtime_state_declaration}\
PAYLOAD_LINE=__PAYLOAD_LINE__
PAYLOAD_BYTES=$(( {payload_bytes:20d} ))

{runtime_functions}{conan_functions}\
live_progress=0
color=0
if [ -t 2 ] && [ "${{TERM-}}" != dumb ]; then
  live_progress=1
  if [ -z "${{NO_COLOR+x}}" ]; then
    color=1
  fi
fi

progress_completed=-1
draw_progress() {{
  completed=$1
  percent=$((completed * 100 / PAYLOAD_BYTES))
  [ "$percent" -le 100 ] || percent=100
  [ "$completed" -ne "$progress_completed" ] || return 0
  progress_completed=$completed
  completed_mib=$(((completed + 1048575) / 1048576))
  total_mib=$(((PAYLOAD_BYTES + 1048575) / 1048576))
  filled=$((percent * 24 / 100))
  bar=
  position=0
  while [ "$position" -lt 24 ]; do
    if [ "$position" -lt "$filled" ]; then
      bar="${{bar}}="
    elif [ "$position" -eq "$filled" ] && [ "$percent" -lt 100 ]; then
      bar="${{bar}}>"
    else
      bar="${{bar}} "
    fi
    position=$((position + 1))
  done
  if [ "$color" -eq 1 ]; then
    printf '\r    [\033[36m%s\033[0m] %3d%% %d/%d MiB' \
      "$bar" "$percent" "$completed_mib" "$total_mib" >&2
  else
    printf '\r    [%s] %3d%% %d/%d MiB' \
      "$bar" "$percent" "$completed_mib" "$total_mib" >&2
  fi
}}

print_install_stage() {{
  detail=$1
  if [ "$color" -eq 1 ]; then
    printf '\033[1;36m==>\033[0m \033[1minstall:\033[0m %s\n' "$detail" >&2
  else
    printf '==> install: %s\n' "$detail" >&2
  fi
}}

usage() {{
  echo "usage: $0 [--prefix PREFIX] [--launcher-name NAME] [--conan-home PATH] [--conan-build-profile NAME_OR_PATH] [--conan-cppstd VALUE]" >&2
  exit 2
}}
prefix=
launcher_name=$DEFAULT_LAUNCHER
conan_home=
conan_build_profile=
conan_cppstd=
conan_home_option=0
conan_build_profile_option=0
conan_cppstd_option=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) [ "$#" -ge 2 ] || usage; prefix=$2; shift 2 ;;
    --launcher-name) [ "$#" -ge 2 ] || usage; launcher_name=$2; shift 2 ;;
    --conan-home) [ "$#" -ge 2 ] || usage; conan_home=$2; conan_home_option=1; shift 2 ;;
    --conan-build-profile) [ "$#" -ge 2 ] || usage; conan_build_profile=$2; conan_build_profile_option=1; shift 2 ;;
    --conan-cppstd) [ "$#" -ge 2 ] || usage; conan_cppstd=$2; conan_cppstd_option=1; shift 2 ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done
case "$launcher_name" in
  ""|*[!A-Za-z0-9_.+-]*|[!A-Za-z0-9]*) echo "linux-toolchain: invalid launcher name: $launcher_name" >&2; exit 2 ;;
esac
if [ "$HAS_CONAN" -ne 1 ] && {{ [ "$conan_home_option" -eq 1 ] || [ "$conan_build_profile_option" -eq 1 ] || [ "$conan_cppstd_option" -eq 1 ]; }}; then
  echo "linux-toolchain: Conan options require a bundle with Conan integration" >&2
  exit 2
fi
if [ "$conan_cppstd_option" -eq 1 ]; then
  case "$conan_cppstd" in
    98|gnu98|11|gnu11|14|gnu14|17|gnu17|20|gnu20|23|gnu23) ;;
    *) echo "linux-toolchain: unsupported Conan C++ standard: $conan_cppstd" >&2; exit 2 ;;
  esac
fi

machine=$(uname -m 2>/dev/null || true)
case "$machine" in
  amd64) machine=x86_64 ;;
  arm64) machine=aarch64 ;;
esac
if [ "$machine" != "$EXPECTED_ARCH" ]; then
  echo "linux-toolchain: installer requires $EXPECTED_ARCH, current host is $machine" >&2
  exit 2
fi
if [ "$HAS_CONAN" -eq 1 ] && [ "$conan_build_profile_option" -eq 0 ] && \
   [ "$TARGET_ARCH" != "$EXPECTED_ARCH" ]; then
  echo "linux-toolchain: default lxtc Conan build profile requires a native target; use --conan-build-profile for $TARGET_ARCH" >&2
  exit 2
fi

glibc=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
glibc=${{glibc#glibc }}
version_at_least() {{
  actual_version=$1
  required_version=$2
  old_ifs=$IFS
  IFS=.
  set -- $actual_version
  actual_major=${{1:-0}}
  actual_minor=${{2:-0}}
  set -- $required_version
  required_major=${{1:-0}}
  required_minor=${{2:-0}}
  IFS=$old_ifs
  [ "$actual_major" -gt "$required_major" ] ||
    {{ [ "$actual_major" -eq "$required_major" ] && [ "$actual_minor" -ge "$required_minor" ]; }}
}}
required_glibc=$EXPECTED_GLIBC
if [ "$HAS_CONAN" -eq 1 ] && [ "$conan_build_profile_option" -eq 0 ] && \
   ! version_at_least "$required_glibc" "$TARGET_GLIBC"; then
  required_glibc=$TARGET_GLIBC
fi
if [ -z "$glibc" ] || ! version_at_least "$glibc" "$required_glibc"; then
  echo "linux-toolchain: installer requires glibc $required_glibc or newer, current host reports $glibc" >&2
  exit 2
fi

canonical_home=
if [ -n "${{HOME-}}" ] && [ -d "$HOME" ]; then
  canonical_home=$(CDPATH= cd "$HOME" && pwd -P) || {{
    echo "linux-toolchain: cannot resolve HOME: $HOME" >&2
    exit 2
  }}
fi
if [ -z "$prefix" ]; then
  if [ -z "$canonical_home" ]; then
    echo "linux-toolchain: HOME is required when --prefix is omitted" >&2
    exit 2
  fi
  prefix=$canonical_home/.local/lib/linux-toolchain/$DEFAULT_INSTALL_NAME
fi

case "$prefix" in
  ""|*[!\ A-Za-z0-9/._+@=-]*) echo "linux-toolchain: unsupported installation prefix: $prefix" >&2; exit 2 ;;
  /*) ;;
  *) prefix=$PWD/$prefix ;;
esac
parent=${{prefix%/*}}
base=${{prefix##*/}}
case "$base" in
  ""|.|..) echo "linux-toolchain: unsupported installation prefix: $prefix" >&2; exit 2 ;;
esac
mkdir -p -- "$parent"
parent=$(CDPATH= cd "$parent" && pwd -P)
prefix=$parent/$base
case "$prefix" in
  /|"$canonical_home") echo "linux-toolchain: unsupported installation prefix: $prefix" >&2; exit 2 ;;
esac
if [ -e "$prefix" ] || [ -L "$prefix" ]; then
  if [ ! -d "$prefix" ] || [ -L "$prefix" ] || [ -n "$(ls -A -- "$prefix" 2>/dev/null)" ]; then
    echo "linux-toolchain: installation prefix must be absent or empty: $prefix" >&2
    exit 2
  fi
  rmdir -- "$prefix"
fi
work=$(mktemp -d "$parent/.${{base}}.install.XXXXXXXX")
conan_temporary_file=
cleanup() {{
  if [ -n "$conan_temporary_file" ]; then
    rm -f -- "$conan_temporary_file"
  fi
  rm -rf -- "$work"
}}
trap cleanup EXIT HUP INT TERM

if [ "$live_progress" -eq 1 ]; then
  print_install_stage "extracting bundle"
  payload_archive="$work/payload.tar.gz"
  : >"$payload_archive"
  draw_progress 0
  tail -n "+$PAYLOAD_LINE" "$0" | \
    tee "$payload_archive" | \
    tar -xzf - -C "$work" &
  tar_pid=$!
  while kill -0 "$tar_pid" 2>/dev/null; do
    completed=$(wc -c <"$payload_archive")
    if [ "$completed" -ge "$PAYLOAD_BYTES" ]; then
      completed=$((PAYLOAD_BYTES - 1))
    fi
    draw_progress "$completed"
    sleep 0.1
  done
  wait "$tar_pid" || {{ status=$?; printf '\n' >&2; exit "$status"; }}
  completed=$(wc -c <"$payload_archive")
  if [ "$completed" -ne "$PAYLOAD_BYTES" ]; then
    printf '\nlinux-toolchain: bundle payload is truncated: expected %s bytes, read %s\n' \
      "$PAYLOAD_BYTES" "$completed" >&2
    exit 2
  fi
  draw_progress "$PAYLOAD_BYTES"
  printf '\n' >&2
  rm -- "$payload_archive"
else
  printf '==> install: extracting bundle ... ' >&2
  tail -n "+$PAYLOAD_LINE" "$0" | tar -xzf - -C "$work"
  printf 'DONE\n' >&2
fi
payload="$work/payload"

while IFS= read -r relative; do
  [ -n "$relative" ] || continue
  file="$payload/$relative"
  mode=0644
  [ -x "$file" ] && mode=0755
  sed \
    -e "s|{PREFIX_TOKEN}|$prefix|g" \
    "$file" >"$file.installed"
  chmod "$mode" "$file.installed"
  mv -- "$file.installed" "$file"
done <"$payload/template-files"
rm -- "$payload/template-files"

if [ "$launcher_name" != "$DEFAULT_LAUNCHER" ]; then
  mv -- "$payload/bin/$DEFAULT_LAUNCHER" "$payload/bin/$launcher_name"
fi

if [ "$HAS_CONAN" -eq 1 ]; then
  if [ -z "$conan_home" ]; then
    [ -n "${{HOME-}}" ] || {{
      echo "linux-toolchain: HOME is required for the default Conan home" >&2
      exit 2
    }}
    conan_home=$HOME/$CONAN_HOME_NAME
  fi
  case "$conan_home" in
    ""|*[!\ A-Za-z0-9/._+@=-]*) echo "linux-toolchain: unsupported Conan home: $conan_home" >&2; exit 2 ;;
    /*) ;;
    *) conan_home=$PWD/$conan_home ;;
  esac
  case "$conan_home" in
    */../*|*/./*|*//*|*/..|*/.) echo "linux-toolchain: Conan home must be canonical: $conan_home" >&2; exit 2 ;;
  esac
  conan_parent=${{conan_home%/*}}
  conan_base=${{conan_home##*/}}
  case "$conan_base" in
    ""|.|..) echo "linux-toolchain: unsupported Conan home: $conan_home" >&2; exit 2 ;;
  esac
  mkdir -p -- "$conan_parent"
  conan_parent=$(CDPATH= cd "$conan_parent" && pwd -P)
  conan_home=$conan_parent/$conan_base
  case "$conan_home" in
    /|"$canonical_home") echo "linux-toolchain: unsafe Conan home: $conan_home" >&2; exit 2 ;;
  esac
  conan_profiles=$conan_home/profiles

  if [ "$conan_build_profile_option" -eq 0 ]; then
    build_profile=$prefix/binding/conan/build.profile
  else
    case "$conan_build_profile" in
      /*)
        case "$conan_build_profile" in
          *[!\ A-Za-z0-9/._+@=-]*|*/../*|*/./*|*//*|*/..|*/.)
            echo "linux-toolchain: invalid Conan build profile path: $conan_build_profile" >&2
            exit 2 ;;
        esac
        build_profile=$conan_build_profile
        ;;
      */*)
        echo "linux-toolchain: Conan build profile must be a name or absolute path" >&2
        exit 2
        ;;
      ""|lxtc-build|*[!A-Za-z0-9_.+-]*|[!A-Za-z0-9]*)
        echo "linux-toolchain: invalid Conan build profile: $conan_build_profile" >&2
        exit 2
        ;;
      *) build_profile=$conan_build_profile ;;
    esac
  fi
  if [ "$build_profile" = "$conan_profiles/lxtc-build" ]; then
    echo "linux-toolchain: Conan build profile cannot select the generated lxtc-build selector itself" >&2
    exit 2
  fi

  if [ "$conan_cppstd_option" -eq 1 ]; then
    printf '\n[settings]\ncompiler.cppstd=%s\n' "$conan_cppstd" \
      >>"$payload/binding/conan/default.profile"
  fi
  prepare_conan_home "$payload/binding/conan" "$conan_home" "$prefix"
  lxtc_runtime_persistent=$DEFAULT_CXX_RUNTIME
  lxtc_runtime_profile=default
  if [ "$RUNTIME_SWITCH" -eq 1 ]; then
    load_runtime_selection "$DEFAULT_CXX_RUNTIME"
  fi
  write_conan_info \
    "$conan_home" \
    "$payload/binding/env/toolchain.info" \
    "$lxtc_runtime_persistent" \
    "$lxtc_runtime_profile" \
    lxtc-build
  if [ "$conan_build_profile_option" -eq 1 ]; then
    selected_build_profile=$build_profile
    case "$build_profile" in
      /*) ;;
      *) selected_build_profile=$conan_profiles/$build_profile ;;
    esac
    if [ ! -f "$selected_build_profile" ]; then
      echo "linux-toolchain: note: explicit Conan build profile is not present yet: $selected_build_profile" >&2
    fi
  fi
  printf '%s\n' "$conan_home" >"$payload/binding/conan/conan-home"
  printf '%s\n' "$build_profile" >"$payload/binding/conan/build-profile"
fi

mv -T -- "$payload" "$prefix"
trap - EXIT HUP INT TERM
rm -rf -- "$work"
if [ "$color" -eq 1 ]; then
  printf '\033[1;36m==>\033[0m \033[1minstall:\033[0m ready ... \033[1;32mDONE\033[0m\n' >&2
else
  printf '==> install: ready ... DONE\n' >&2
fi
launcher_path="$prefix/bin/$launcher_name"
path_command='export PATH="'"$prefix"'/bin:$PATH"'
printf '%s\\n' \
  'Add launcher to PATH:' \
  '  Current shell:' \
  "    $path_command" \
  '  Bash (~/.bashrc):' \
  "    printf '\\n%s\\n' '$path_command' >> \\\"\\$HOME/.bashrc\\\"" \
  '  Zsh (~/.zshrc):' \
  "    printf '\\n%s\\n' '$path_command' >> \\\"\\$HOME/.zshrc\\\"" >&2
echo "$launcher_path"
exit 0
{_PAYLOAD_MARKER}
"""
    line_count = template.count("\n")
    return template.replace("__PAYLOAD_LINE__", str(line_count + 1)).encode("utf-8")
