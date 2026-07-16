from __future__ import annotations

import shlex
from collections.abc import Mapping


def _quote(value: str) -> str:
    return shlex.quote(value)


def render_build_tools_build_script(
    *,
    arch: str,
    triplet: str,
    cmake_version: str,
    openssl_version: str,
    make_version: str,
    ninja_version: str,
    ccache_version: str,
    archives: Mapping[str, str],
) -> str:
    openssl_target = {
        "x86_64": "linux-x86_64",
        "aarch64": "linux-aarch64",
    }[arch]
    ccache_directory = f"ccache-{ccache_version}-linux-{arch}-musl-static"
    values = {
        "TRIPLET": triplet,
        "CMAKE_VERSION": cmake_version,
        "OPENSSL_VERSION": openssl_version,
        "MAKE_VERSION": make_version,
        "NINJA_VERSION": ninja_version,
        "CCACHE_VERSION": ccache_version,
        "OPENSSL_TARGET": openssl_target,
        "CCACHE_DIRECTORY": ccache_directory,
        "CMAKE_ARCHIVE": archives["cmake"],
        "OPENSSL_ARCHIVE": archives["openssl"],
        "MAKE_ARCHIVE": archives["make"],
        "NINJA_ARCHIVE": archives["ninja"],
        "CCACHE_ARCHIVE": archives["ccache"],
    }
    assignments = "\n".join(
        f"readonly {name}={_quote(value)}" for name, value in values.items()
    )
    return f"""#!/usr/bin/env bash
set -euo pipefail

{assignments}
readonly JOBS="${{LINUX_TOOLCHAIN_JOBS:?LINUX_TOOLCHAIN_JOBS is required}}"
readonly SOURCE_ROOT=/work/sources
readonly BUILD_ROOT=/work/build
readonly OPENSSL_PREFIX=/work/deps/openssl
readonly TOOLCHAIN_BIN=/compiler-backend/toolchain/bin
readonly CC="$TOOLCHAIN_BIN/$TRIPLET-gcc"
readonly CXX="$TOOLCHAIN_BIN/$TRIPLET-g++"
readonly AR="$TOOLCHAIN_BIN/$TRIPLET-ar"
readonly RANLIB="$TOOLCHAIN_BIN/$TRIPLET-ranlib"

extract_source() {{
  local archive="$1"
  local destination="$2"
  local staging="$SOURCE_ROOT/.$(basename "$destination").extracting"
  if [[ -d "$destination" ]]; then
    return
  fi
  rm -rf "$staging"
  mkdir -p "$staging"
  tar -xf "/sources/$archive" -C "$staging" --strip-components=1
  mv "$staging" "$destination"
}}

mkdir -p "$SOURCE_ROOT" "$BUILD_ROOT" /work/deps /output/bin /output/licenses
export CC CXX AR RANLIB
export CFLAGS="-O2 -g0"
export CXXFLAGS="-O2 -g0"
export CPPFLAGS=""

extract_source "$OPENSSL_ARCHIVE" "$SOURCE_ROOT/openssl-$OPENSSL_VERSION"
mkdir -p "$OPENSSL_PREFIX"
(
  cd "$SOURCE_ROOT/openssl-$OPENSSL_VERSION"
  ./Configure "$OPENSSL_TARGET" \
    --prefix="$OPENSSL_PREFIX" \
    --openssldir="$OPENSSL_PREFIX/ssl" \
    no-module no-shared no-tests \
    -O2 -g0
  make -j"$JOBS" build_libs
  make install_dev
)

extract_source "$CMAKE_ARCHIVE" "$SOURCE_ROOT/cmake-$CMAKE_VERSION"
mkdir -p "$BUILD_ROOT/cmake"
(
  export LDFLAGS="-static-libstdc++ -static-libgcc"
  cd "$BUILD_ROOT/cmake"
  "$SOURCE_ROOT/cmake-$CMAKE_VERSION/bootstrap" \
    --prefix=/ \
    --parallel="$JOBS" \
    --no-qt-gui \
    --no-system-libs \
    -- \
    -DBUILD_CursesDialog=OFF \
    -DBUILD_TESTING=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_FLAGS_RELEASE="-O2 -g0 -DNDEBUG" \
    -DCMAKE_CXX_FLAGS_RELEASE="-O2 -g0 -DNDEBUG" \
    -DCMAKE_EXE_LINKER_FLAGS="-static-libstdc++ -static-libgcc" \
    -DCMAKE_SKIP_INSTALL_RPATH=ON \
    -DOPENSSL_ROOT_DIR="$OPENSSL_PREFIX" \
    -DOPENSSL_USE_STATIC_LIBS=ON
  make -j"$JOBS"
  DESTDIR=/output make install
)

extract_source "$MAKE_ARCHIVE" "$SOURCE_ROOT/make-$MAKE_VERSION"
mkdir -p "$BUILD_ROOT/make"
(
  cd "$BUILD_ROOT/make"
  "$SOURCE_ROOT/make-$MAKE_VERSION/configure" \
    --prefix=/ \
    --disable-nls \
    --without-guile
  make -j"$JOBS"
  DESTDIR=/output make install
)

extract_source "$NINJA_ARCHIVE" "$SOURCE_ROOT/ninja-$NINJA_VERSION"
mkdir -p "$BUILD_ROOT/ninja"
/output/bin/cmake \
  -S "$SOURCE_ROOT/ninja-$NINJA_VERSION" \
  -B "$BUILD_ROOT/ninja" \
  -G "Unix Makefiles" \
  -DBUILD_TESTING=OFF \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_COMPILER="$CXX" \
  -DCMAKE_CXX_FLAGS_RELEASE="-O2 -g0 -DNDEBUG" \
  -DCMAKE_EXE_LINKER_FLAGS="-static-libstdc++ -static-libgcc" \
  -DCMAKE_INSTALL_PREFIX=/ \
  -DCMAKE_SKIP_INSTALL_RPATH=ON
/output/bin/cmake --build "$BUILD_ROOT/ninja" --parallel "$JOBS"
DESTDIR=/output /output/bin/cmake --install "$BUILD_ROOT/ninja"

extract_source "$CCACHE_ARCHIVE" "$SOURCE_ROOT/$CCACHE_DIRECTORY"
install -m 0755 "$SOURCE_ROOT/$CCACHE_DIRECTORY/ccache" /output/bin/ccache

install -D -m 0644 \
  "$SOURCE_ROOT/cmake-$CMAKE_VERSION/Copyright.txt" \
  /output/licenses/cmake/Copyright.txt
install -D -m 0644 \
  "$SOURCE_ROOT/openssl-$OPENSSL_VERSION/LICENSE.txt" \
  /output/licenses/openssl/LICENSE.txt
install -D -m 0644 \
  "$SOURCE_ROOT/make-$MAKE_VERSION/COPYING" \
  /output/licenses/make/COPYING
install -D -m 0644 \
  "$SOURCE_ROOT/ninja-$NINJA_VERSION/COPYING" \
  /output/licenses/ninja/COPYING
install -D -m 0644 \
  "$SOURCE_ROOT/$CCACHE_DIRECTORY/LICENSE.md" \
  /output/licenses/ccache/LICENSE.md
install -D -m 0644 \
  "$SOURCE_ROOT/$CCACHE_DIRECTORY/GPL-3.0.txt" \
  /output/licenses/ccache/GPL-3.0.txt
"""
