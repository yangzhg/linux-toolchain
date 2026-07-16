from __future__ import annotations

from linux_toolchain.managed.build_script_common import (
    BuildSelection,
    render_common_script,
)
from linux_toolchain.versions import major_version


def render_gcc_build_script(
    selection: BuildSelection,
    triplet: str,
    backend_triplet: str,
    backend_version: str,
    *,
    paired_runtime: bool,
) -> str:
    prefix = render_common_script(
        selection,
        triplet,
        backend_triplet,
        backend_version,
        paired_runtime=paired_runtime,
    )
    build_atomic = major_version(selection.version) >= 16
    atomic_build = (
        'make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-target-libatomic\n'
        if build_atomic
        else ""
    )
    paired_atomic_install = (
        'make -C "$BUILD_ROOT/gcc" install-target-libatomic \\\n'
        '  DESTDIR="$RUNTIME_ARTIFACTS/.runtime-install"\n'
        if build_atomic
        else ""
    )
    standalone_atomic_install = (
        'make -C "$BUILD_ROOT/gcc" install-target-libatomic \\\n'
        '  DESTDIR="$ARTIFACTS/.runtime-install"\n'
        if build_atomic
        else ""
    )
    build_quadmath = selection.target_arch == "x86_64"
    quadmath_build = (
        'make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-target-libquadmath\n'
        if build_quadmath
        else ""
    )
    paired_quadmath_install = (
        'make -C "$BUILD_ROOT/gcc" install-target-libquadmath \\\n'
        '  DESTDIR="$RUNTIME_ARTIFACTS/.runtime-install"\n'
        if build_quadmath
        else ""
    )
    standalone_quadmath_install = (
        'make -C "$BUILD_ROOT/gcc" install-target-libquadmath \\\n'
        '  DESTDIR="$ARTIFACTS/.runtime-install"\n'
        if build_quadmath
        else ""
    )
    quadmath_mode = "enabled" if build_quadmath else "disabled"
    quadmath_option = (
        "--enable-libquadmath" if build_quadmath else "--disable-libquadmath"
    )
    compiler_install = r"""
make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-gcc
make -C "$BUILD_ROOT/gcc" install-gcc \
  DESTDIR="$ARTIFACTS/.compiler-install"
mv "$ARTIFACTS/.compiler-install$PREFIX" "$ARTIFACTS/compiler"
rm -rf -- "$ARTIFACTS/.compiler-install"
install_target_tools "$ARTIFACTS/compiler"
if find "$ARTIFACTS/compiler" -type f \
    \( -name 'libstdc++.so*' -o -name 'libgcc_s.so*' \) -print -quit | grep -q .; then
  echo "target shared runtime leaked into compiler kit" >&2
  exit 1
fi
vendor_host_dependencies "$ARTIFACTS/compiler"
test "$("$ARTIFACTS/compiler/bin/$TARGET-gcc" -dumpfullversion)" = "$VERSION"
test "$("$ARTIFACTS/compiler/bin/$TARGET-g++" -dumpfullversion)" = "$VERSION"
test "$("$ARTIFACTS/compiler/bin/$TARGET-gcc" -dumpmachine)" = "$TARGET"
test "$("$ARTIFACTS/compiler/bin/$TARGET-g++" -dumpmachine)" = "$TARGET"
"""
    paired_runtime_install = (
        r"""
make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-target-libgcc
"""
        + atomic_build
        + quadmath_build
        + r"""
make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-target-libstdc++-v3
make -C "$BUILD_ROOT/gcc" install-target-libgcc \
  DESTDIR="$RUNTIME_ARTIFACTS/.runtime-install"
"""
        + paired_atomic_install
        + paired_quadmath_install
        + r"""
make -C "$BUILD_ROOT/gcc" install-target-libstdc++-v3 \
  DESTDIR="$RUNTIME_ARTIFACTS/.runtime-install"
mv "$RUNTIME_ARTIFACTS/.runtime-install$PREFIX" \
  "$RUNTIME_ARTIFACTS/runtime"
rm -rf -- "$RUNTIME_ARTIFACTS/.runtime-install"
readonly RUNTIME_GCC_DIR="$RUNTIME_ARTIFACTS/runtime/lib/gcc/$TARGET/$VERSION"
readonly HEADER_GCC_DIR="$PRIMARY_AVAILABLE_ARTIFACTS/compiler/lib/gcc/$TARGET/$VERSION"
for header_dir in include include-fixed; do
  test -d "$HEADER_GCC_DIR/$header_dir"
  mkdir -p "$RUNTIME_GCC_DIR/$header_dir"
  cp -a -- "$HEADER_GCC_DIR/$header_dir/." "$RUNTIME_GCC_DIR/$header_dir/"
done
mkdir -p "$RUNTIME_ARTIFACTS/licenses"
cp -a -- "$PRIMARY_AVAILABLE_ARTIFACTS/licenses/gcc" \
  "$RUNTIME_ARTIFACTS/licenses/gcc"
"""
    )
    if selection.artifact_kind == "compiler-kit":
        publish = (
            r"""
if test "$PRESERVE_PRIMARY" = 0; then
"""
            + compiler_install
            + r"""
fi
if test "$PRESERVE_RUNTIME" = 0; then
"""
            + paired_runtime_install
            + r"""
fi
"""
            if paired_runtime
            else compiler_install
        )
    else:
        publish = (
            r"""
make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-gcc
make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-target-libgcc
"""
            + atomic_build
            + quadmath_build
            + r"""
make -C "$BUILD_ROOT/gcc" -j"$JOBS" all-target-libstdc++-v3
make -C "$BUILD_ROOT/gcc" install-target-libgcc \
  DESTDIR="$ARTIFACTS/.runtime-install"
"""
            + standalone_atomic_install
            + standalone_quadmath_install
            + r"""
make -C "$BUILD_ROOT/gcc" install-target-libstdc++-v3 \
  DESTDIR="$ARTIFACTS/.runtime-install"
make -C "$BUILD_ROOT/gcc" install-gcc \
  DESTDIR="$ARTIFACTS/.runtime-headers-install"
mv "$ARTIFACTS/.runtime-install$PREFIX" "$ARTIFACTS/runtime"
readonly RUNTIME_GCC_DIR="$ARTIFACTS/runtime/lib/gcc/$TARGET/$VERSION"
readonly HEADER_GCC_DIR="$ARTIFACTS/.runtime-headers-install$PREFIX/lib/gcc/$TARGET/$VERSION"
for header_dir in include include-fixed; do
  test -d "$HEADER_GCC_DIR/$header_dir"
  mkdir -p "$RUNTIME_GCC_DIR/$header_dir"
  cp -a -- "$HEADER_GCC_DIR/$header_dir/." "$RUNTIME_GCC_DIR/$header_dir/"
done
rm -rf -- \
  "$ARTIFACTS/.runtime-install" \
  "$ARTIFACTS/.runtime-headers-install"
"""
        )
    return (
        prefix
        + rf"""
test -f /sources/source.tar.xz
readonly GCC_SOURCE_ID="format=1 source=gcc-$VERSION"
readonly GCC_SOURCE_MARKER="$SOURCE_ROOT/gcc/.linux-toolchain-source-ready"
if test ! -f "$GCC_SOURCE_MARKER" || \
    test "$(cat "$GCC_SOURCE_MARKER" 2>/dev/null || true)" != "$GCC_SOURCE_ID" || \
    test ! -x "$SOURCE_ROOT/gcc/configure" || \
    test ! -d "$SOURCE_ROOT/gcc/gmp" || \
    test ! -d "$SOURCE_ROOT/gcc/mpfr" || \
    test ! -d "$SOURCE_ROOT/gcc/mpc"; then
  readonly GCC_SOURCE_STAGING="$SOURCE_ROOT/.gcc.extracting"
  rm -rf -- "$GCC_SOURCE_STAGING" "$SOURCE_ROOT/gcc"
  mkdir "$GCC_SOURCE_STAGING"
  tar -xf /sources/source.tar.xz \
    -C "$GCC_SOURCE_STAGING" --strip-components=1
  for archive in \
      gmp-6.3.0.tar.xz mpfr-4.2.2.tar.xz mpc-1.3.1.tar.gz; do
    component="${{archive%%-*}}"
    mkdir "$GCC_SOURCE_STAGING/$component"
    tar -xf "/compiler-backend-sources/$archive" \
      -C "$GCC_SOURCE_STAGING/$component" --strip-components=1
  done
  test -x "$GCC_SOURCE_STAGING/configure"
  printf '%s\n' "$GCC_SOURCE_ID" \
    >"$GCC_SOURCE_STAGING/.linux-toolchain-source-ready"
  mv -- "$GCC_SOURCE_STAGING" "$SOURCE_ROOT/gcc"
fi
test -x "$SOURCE_ROOT/gcc/configure"
if test "$PRESERVE_PRIMARY" = 0; then
  copy_source_licenses "$SOURCE_ROOT/gcc" gcc
fi
rm -rf -- "$BUILD_ROOT/target-tools"
mkdir "$BUILD_ROOT/target-tools"
for tool in $TARGET_TOOL_NAMES; do
  test -x "/target-tools/$TARGET-$tool"
  ln -s "/target-tools/$TARGET-$tool" "$BUILD_ROOT/target-tools/$tool"
done
# GCC's native plugin probe invokes objdump without a target prefix. Keep that
# probe, and any equivalent build-time tool lookup, on the selected tools
# rather than the producer image PATH.
export PATH="$BUILD_ROOT/target-tools:$PATH"

export CC="$BACKEND_CC"
export CXX="$BACKEND_CXX"
export AR=/compiler-backend/bin/$BACKEND_TARGET-ar
export AS=/compiler-backend/bin/$BACKEND_TARGET-as
export LD=/compiler-backend/bin/$BACKEND_TARGET-ld
export NM=/compiler-backend/bin/$BACKEND_TARGET-nm
export RANLIB=/compiler-backend/bin/$BACKEND_TARGET-ranlib
export STRIP=/compiler-backend/bin/$BACKEND_TARGET-strip
export CC_FOR_BUILD="$BACKEND_CC"
export CXX_FOR_BUILD="$BACKEND_CXX"

readonly GCC_RELEASE_FLAGS="-O2 -g0"
readonly GCC_CONFIG_ID="format=1 source=gcc-$VERSION target=$TARGET backend=$BACKEND_TARGET-$BACKEND_VERSION libquadmath={quadmath_mode} release_flags=$GCC_RELEASE_FLAGS"
readonly GCC_CONFIG_MARKER="$BUILD_ROOT/gcc/.linux-toolchain-configured"
if test ! -f "$GCC_CONFIG_MARKER" || \
    test "$(cat "$GCC_CONFIG_MARKER" 2>/dev/null || true)" != "$GCC_CONFIG_ID" || \
    test ! -f "$BUILD_ROOT/gcc/Makefile"; then
  rm -rf -- "$BUILD_ROOT/gcc"
  mkdir "$BUILD_ROOT/gcc"
  (cd "$BUILD_ROOT/gcc" && \
  CFLAGS="$GCC_RELEASE_FLAGS" \
  CXXFLAGS="$GCC_RELEASE_FLAGS" \
  CFLAGS_FOR_TARGET="$GCC_RELEASE_FLAGS" \
  CXXFLAGS_FOR_TARGET="$GCC_RELEASE_FLAGS" \
  "$SOURCE_ROOT/gcc/configure" \
  --build="$BACKEND_TARGET" \
  --host="$BACKEND_TARGET" \
  --target="$TARGET" \
  --prefix="$PREFIX" \
  --with-sysroot="$PREFIX/$TARGET/sysroot" \
  --with-build-sysroot=/sdk/sysroot \
  --with-native-system-header-dir=/usr/include \
  --with-build-time-tools="$BUILD_ROOT/target-tools" \
  --with-pkgversion='linux-toolchain managed GCC' \
  --with-host-libstdcxx='-static-libgcc -Wl,-Bstatic,-lstdc++,-Bdynamic -lm' \
  --without-isl \
  --disable-bootstrap \
  --disable-multilib \
  --disable-nls \
  --disable-werror \
  --disable-lto \
  --disable-libsanitizer \
  --disable-libgomp \
  {quadmath_option} \
  --disable-libssp \
  --disable-libvtv \
  --disable-libstdcxx-pch \
  --enable-shared \
  --enable-threads=posix \
  --enable-languages=c,c++)
  printf '%s\n' "$GCC_CONFIG_ID" >"$GCC_CONFIG_MARKER"
fi
{publish}
"""
    )
