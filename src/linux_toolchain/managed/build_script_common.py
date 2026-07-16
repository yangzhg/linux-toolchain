from __future__ import annotations

from typing import Protocol

from linux_toolchain.managed.contracts import MANAGED_TARGET_TOOL_NAMES

_OUTPUT_MARKER = ".linux-toolchain-managed-output"


class SourceSelection(Protocol):
    sha512: str


class BuildSelection(Protocol):
    artifact_kind: str
    family: str
    target_arch: str
    version: str
    source: SourceSelection


def render_common_script(
    selection: BuildSelection,
    triplet: str,
    backend_triplet: str,
    backend_version: str,
    *,
    paired_runtime: bool = False,
) -> str:
    version_line = (
        f"readonly VERSION={selection.version!r}"
        if selection.family == "gcc" or selection.artifact_kind == "compiler-kit"
        else ""
    )
    target_tool_names = " ".join(MANAGED_TARGET_TOOL_NAMES)
    paired_output = (
        rf"""
test -f /runtime-output/{_OUTPUT_MARKER}
readonly RUNTIME_FINAL_ARTIFACTS=/runtime-output/artifacts
readonly RUNTIME_ARTIFACTS=/runtime-output/.artifacts.staging
prepare_artifact_staging \
  /runtime-output "$RUNTIME_ARTIFACTS" "$PRESERVE_RUNTIME"
if test "$PRESERVE_RUNTIME" = 1; then
  readonly RUNTIME_AVAILABLE_ARTIFACTS="$RUNTIME_FINAL_ARTIFACTS"
else
  readonly RUNTIME_AVAILABLE_ARTIFACTS="$RUNTIME_ARTIFACTS"
fi
"""
        if paired_runtime
        else ""
    )
    return rf"""#!/usr/bin/env bash
set -euo pipefail
umask 022
export LC_ALL=C
export LANG=C
export TZ=UTC
export SOURCE_DATE_EPOCH=1
export CONFIG_SHELL=/bin/bash
export PATH=/compiler-backend/bin:/target-tools:/usr/bin:/bin
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_RUN_PATH
unset LD_LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX CCC_OVERRIDE_OPTIONS

{version_line}
readonly TARGET={triplet!r}
readonly BACKEND_TARGET={backend_triplet!r}
readonly BACKEND_VERSION={backend_version!r}
readonly BACKEND_CC=/compiler-backend/bin/$BACKEND_TARGET-gcc
readonly BACKEND_CXX=/compiler-backend/bin/$BACKEND_TARGET-g++
readonly TARGET_TOOL_NAMES={target_tool_names!r}
readonly JOBS="${{LINUX_TOOLCHAIN_JOBS:?LINUX_TOOLCHAIN_JOBS is required}}"
readonly PRESERVE_PRIMARY="${{LINUX_TOOLCHAIN_PRESERVE_PRIMARY:?LINUX_TOOLCHAIN_PRESERVE_PRIMARY is required}}"
readonly PRESERVE_RUNTIME="${{LINUX_TOOLCHAIN_PRESERVE_RUNTIME:?LINUX_TOOLCHAIN_PRESERVE_RUNTIME is required}}"
readonly PREFIX=/opt/linux-toolchain/managed
readonly BUILD_ROOT=/output/build
readonly SOURCE_ROOT=/output/src
readonly FINAL_ARTIFACTS=/output/artifacts
readonly ARTIFACTS=/output/.artifacts.staging

test -f /output/{_OUTPUT_MARKER}
test -d /sdk/sysroot
mkdir -p "$BUILD_ROOT" "$SOURCE_ROOT"

prepare_artifact_staging() {{
  local output_root="$1"
  local staging="$2"
  local preserve="$3"

  test -f "$output_root/{_OUTPUT_MARKER}"
  rm -rf -- "$staging"
  if test "$preserve" = 1; then
    return
  fi
  mkdir "$staging"
}}

prepare_artifact_staging \
  /output "$ARTIFACTS" "$PRESERVE_PRIMARY"
if test "$PRESERVE_PRIMARY" = 1; then
  readonly PRIMARY_AVAILABLE_ARTIFACTS="$FINAL_ARTIFACTS"
else
  readonly PRIMARY_AVAILABLE_ARTIFACTS="$ARTIFACTS"
fi
{paired_output}

vendor_host_dependencies() {{
  local root="$1"
  local host_lib="$root/lib/linux-toolchain-host"
  mkdir -p "$host_lib"
  local elf canonical dependency name relative owner package package_name
  local package_version package_arch copyright current_rpath host_rpath
  local index=0
  local -a queue=()
  local -a processed=()
  local -A seen=()
  mapfile -d '' -t queue \
    < <(find "$root/bin" "$root/lib" "$root/libexec" -type f -print0 2>/dev/null)
  while (( index < ${{#queue[@]}} )); do
    elf="${{queue[$index]}}"
    index=$((index + 1))
    canonical="$(readlink -f "$elf")"
    test -n "$canonical" || continue
    if [[ -n ${{seen[$canonical]+present}} ]]; then
      continue
    fi
    seen["$canonical"]=1
    processed+=("$elf")
    while IFS= read -r dependency; do
      test -n "$dependency" || continue
      case "$dependency" in
        "$root"/*) continue ;;
      esac
      name="$(basename "$dependency")"
      case "$name" in
        ld-linux*.so*|libc.so*|libm.so*|libdl.so*|libpthread.so*|librt.so*|libutil.so*|libresolv.so*)
          continue
          ;;
      esac
      if test ! -f "$host_lib/$name"; then
        cp -L -- "$dependency" "$host_lib/$name"
        chmod 0755 "$host_lib/$name"
        queue+=("$host_lib/$name")
        owner="$(dpkg-query --search -- "$dependency" 2>/dev/null | head -n 1 || true)"
        if test -z "$owner"; then
          owner="$(dpkg-query --search -- "$(readlink -f "$dependency")" \
            2>/dev/null | head -n 1 || true)"
        fi
        test -n "$owner"
        package="${{owner%%: /*}}"
        package_name="${{package%%:*}}"
        package_version="$(dpkg-query --show \
          --showformat='${{Version}}' "$package")"
        package_arch="$(dpkg-query --show \
          --showformat='${{Architecture}}' "$package")"
        copyright="/usr/share/doc/$package_name/copyright"
        test -f "$copyright"
        mkdir -p "$ARTIFACTS/licenses/ubuntu/$package_name"
        install -m 0644 -T "$copyright" \
          "$ARTIFACTS/licenses/ubuntu/$package_name/copyright"
        printf '%s\t%s\t%s\t%s\t%s\n' \
          "$name" "$package" "$package_version" "$package_arch" \
          "licenses/ubuntu/$package_name/copyright" \
          >>"$ARTIFACTS/licenses/ubuntu/dependencies.tsv"
      fi
    done < <(ldd "$elf" 2>/dev/null | awk '$2 == "=>" && $3 ~ /^\// {{print $3}} $1 ~ /^\// {{print $1}}' || true)
  done
  for elf in "${{processed[@]}}"; do
    if patchelf --print-rpath "$elf" >/dev/null 2>&1; then
      relative="$(realpath --relative-to="$(dirname "$elf")" "$host_lib")"
      host_rpath="\$ORIGIN/$relative"
      current_rpath="$(patchelf --print-rpath "$elf")"
      current_rpath="$(printf '%s\n' "$current_rpath" | \
        sed -E 's/^:+//; s/:+/:/g; s/:+$//')"
      if test -n "$current_rpath"; then
        case ":$current_rpath:" in
          *":$host_rpath:"*) host_rpath="$current_rpath" ;;
          *) host_rpath="$current_rpath:$host_rpath" ;;
        esac
      fi
      patchelf --set-rpath "$host_rpath" "$elf"
    fi
  done
  if test -f "$ARTIFACTS/licenses/ubuntu/dependencies.tsv"; then
    sort -u -o "$ARTIFACTS/licenses/ubuntu/dependencies.tsv" \
      "$ARTIFACTS/licenses/ubuntu/dependencies.tsv"
  fi
}}

copy_source_licenses() {{
  local source="$1"
  local component="$2"
  local destination="$ARTIFACTS/licenses/$component"
  local path relative
  test -d "$source"
  mkdir -p "$destination"
  while IFS= read -r -d '' path; do
    relative="${{path#"$source"/}}"
    mkdir -p "$destination/$(dirname "$relative")"
    install -m 0644 -T "$path" "$destination/$relative"
  done < <(find "$source" -type f \
    \( -iname 'COPYING' -o -iname 'COPYING.*' \
       -o -iname 'LICENSE' -o -iname 'LICENSE.*' \
       -o -iname 'LICENCE' -o -iname 'LICENCE.*' \
       -o -iname 'NOTICE' -o -iname 'NOTICE.*' \
       -o -iname 'COPYRIGHT' -o -iname 'COPYRIGHT.*' \) \
    -print0)
  test "$(find "$destination" -type f | wc -l)" -gt 0
}}

install_target_tools() {{
  local root="$1"
  local tool
  mkdir -p "$root/bin"
  for tool in $TARGET_TOOL_NAMES; do
    install -m 0755 -T "/target-tools/$TARGET-$tool" \
      "$root/bin/$TARGET-$tool"
  done
  cp -a -- /sdk/licenses/binutils "$ARTIFACTS/licenses/binutils"
}}
"""
