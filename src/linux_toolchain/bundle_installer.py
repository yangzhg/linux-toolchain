from __future__ import annotations

import gzip
import hashlib
import os
import shlex
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Mapping, Sequence

from linux_toolchain.errors import ConfigurationError

PREFIX_TOKEN = "@LINUX_TOOLCHAIN_PREFIX@"
DEFAULT_LAUNCHER_NAME = "lxtc"
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
_CONAN_HOME_DIGEST_LENGTH = 16


def default_conan_home_name(bundle_id: str) -> str:
    digest = hashlib.sha256(bundle_id.encode("utf-8")).hexdigest()
    return f"{_CONAN_HOME_PREFIX}{digest[:_CONAN_HOME_DIGEST_LENGTH]}"


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
    with archive.open("wb") as raw:
        if header is not None:
            raw.write(header(len(paths)))
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


def render_launcher(*, conan: bool) -> str:
    conan_environment = (
        """if [ -f "$BINDING/conan/conan-home" ]; then
  IFS= read -r CONAN_HOME < "$BINDING/conan/conan-home"
  IFS= read -r CONAN_BUILD_PROFILE < "$BINDING/conan/build-profile"
  export CONAN_HOME
  export CONAN_DEFAULT_PROFILE=default
  export CONAN_DEFAULT_BUILD_PROFILE=lxtc-build
  export LINUX_TOOLCHAIN_CONAN_HOST_PROFILE="$BINDING/conan/host.profile"
  export LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE="$CONAN_BUILD_PROFILE"
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
    return f"""#!/bin/sh
set -eu
readonly PREFIX='{PREFIX_TOKEN}'
readonly BINDING="$PREFIX/binding"
export LINUX_TOOLCHAIN_BINDING="$BINDING"
. "$BINDING/env/toolchain.env"
{conan_environment}if [ "${{1-}}" = info ]; then
  if [ "$#" -ne 1 ]; then
    echo "usage: $0 info" >&2
    exit 2
  fi
  cat "$BINDING/env/toolchain.info"
{conan_info}  exit 0
fi
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
    conan: bool,
    payload_entries: int,
) -> bytes:
    conan_flag = "1" if conan else "0"
    conan_home_name = default_conan_home_name(bundle_id)
    template = f"""#!/bin/sh
set -eu
EXPECTED_ARCH={shlex.quote(host_arch)}
EXPECTED_GLIBC={shlex.quote(host_floor)}
TARGET_ARCH={shlex.quote(target_arch)}
TARGET_GLIBC={shlex.quote(target_floor)}
CONAN_HOME_NAME={shlex.quote(conan_home_name)}
DEFAULT_LAUNCHER={shlex.quote(DEFAULT_LAUNCHER_NAME)}
HAS_CONAN={conan_flag}
PAYLOAD_LINE=__PAYLOAD_LINE__
PAYLOAD_ENTRIES={payload_entries}

live_progress=0
color=0
if [ -t 2 ] && [ "${{TERM-}}" != dumb ]; then
  live_progress=1
  if [ -z "${{NO_COLOR+x}}" ]; then
    color=1
  fi
fi

progress_percent=-1
draw_progress() {{
  completed=$1
  percent=$((completed * 100 / PAYLOAD_ENTRIES))
  [ "$percent" -le 100 ] || percent=100
  [ "$percent" -ne "$progress_percent" ] || return 0
  progress_percent=$percent
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
    printf '\r    [\033[36m%s\033[0m] %3d%% %d/%d files' \
      "$bar" "$percent" "$completed" "$PAYLOAD_ENTRIES" >&2
  else
    printf '\r    [%s] %3d%% %d/%d files' \
      "$bar" "$percent" "$completed" "$PAYLOAD_ENTRIES" >&2
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
  echo "usage: $0 --prefix PREFIX [--launcher-name NAME] [--conan-home PATH] [--conan-build-profile NAME_OR_PATH] [--conan-cppstd VALUE]" >&2
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
[ -n "$prefix" ] || usage
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

case "$prefix" in
  ""|*[!A-Za-z0-9/._+@=-]*) echo "linux-toolchain: unsupported installation prefix: $prefix" >&2; exit 2 ;;
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
  extracted_files="$work/extracted-files"
  : >"$extracted_files"
  tail -n "+$PAYLOAD_LINE" "$0" | \
    tar -xzvf - -C "$work" >"$extracted_files" &
  tar_pid=$!
  while kill -0 "$tar_pid" 2>/dev/null; do
    completed=$(wc -l <"$extracted_files")
    draw_progress "$completed"
    sleep 0.2
  done
  wait "$tar_pid" || {{ status=$?; printf '\n' >&2; exit "$status"; }}
  draw_progress "$PAYLOAD_ENTRIES"
  printf '\n' >&2
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
    ""|*[!A-Za-z0-9/._+@=-]*) echo "linux-toolchain: unsupported Conan home: $conan_home" >&2; exit 2 ;;
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
    "$prefix"|"$prefix"/*) echo "linux-toolchain: Conan home and installation prefix cannot overlap: $conan_home and $prefix" >&2; exit 2 ;;
  esac
  case "$prefix" in
    "$conan_home"/*) echo "linux-toolchain: Conan home and installation prefix cannot overlap: $conan_home and $prefix" >&2; exit 2 ;;
  esac
  if [ -L "$conan_home" ] || {{ [ -e "$conan_home" ] && [ ! -d "$conan_home" ]; }}; then
    echo "linux-toolchain: Conan home is not a directory: $conan_home" >&2
    exit 2
  fi
  conan_profiles=$conan_home/profiles
  if [ -L "$conan_profiles" ] || {{ [ -e "$conan_profiles" ] && [ ! -d "$conan_profiles" ]; }}; then
    echo "linux-toolchain: Conan profiles path is not a directory: $conan_profiles" >&2
    exit 2
  fi
  mkdir -p -- "$conan_profiles"

  if [ "$conan_build_profile_option" -eq 0 ]; then
    build_profile=$prefix/binding/conan/build.profile
  else
    case "$conan_build_profile" in
      /*)
        case "$conan_build_profile" in
          *[!A-Za-z0-9/._+@=-]*|*/../*|*/./*|*//*|*/..|*/.)
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

  install_conan_file() {{
    source_file=$1
    destination_file=$2
    if [ -L "$destination_file" ] || {{ [ -e "$destination_file" ] && [ ! -f "$destination_file" ]; }}; then
      echo "linux-toolchain: Conan configuration is not a regular file: $destination_file" >&2
      exit 2
    fi
    if [ -e "$destination_file" ]; then
      cmp -s -- "$source_file" "$destination_file" || {{
        echo "linux-toolchain: refusing to replace different Conan configuration: $destination_file" >&2
        exit 2
      }}
    else
      conan_temporary_file=$(mktemp "$conan_home/.lxtc-config.XXXXXXXX")
      cp -- "$source_file" "$conan_temporary_file"
      chmod 0644 "$conan_temporary_file"
      mv -- "$conan_temporary_file" "$destination_file"
      conan_temporary_file=
    fi
  }}
  settings="$conan_home/settings_user.yml"
  install_conan_file "$payload/binding/conan/settings_user.yml" "$settings"
  default_profile_source="$payload/binding/conan/default.profile"
  if [ "$conan_cppstd_option" -eq 1 ]; then
    default_profile_source="$work/conan-default.profile"
    cp -- "$payload/binding/conan/default.profile" "$default_profile_source"
    printf '\n[settings]\ncompiler.cppstd=%s\n' "$conan_cppstd" >>"$default_profile_source"
  fi
  install_conan_file \
    "$default_profile_source" \
    "$conan_profiles/default"
  install_conan_file \
    "$payload/binding/conan/lxtc-build.profile" \
    "$conan_profiles/lxtc-build"
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
path_command='export PATH='"$prefix"'/bin:"$PATH"'
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
