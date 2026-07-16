from __future__ import annotations

from linux_toolchain.errors import ConfigurationError
from linux_toolchain.managed.build_script_common import (
    BuildSelection,
    render_common_script,
)


def render_llvm_build_script(
    selection: BuildSelection,
    triplet: str,
    backend_triplet: str,
    backend_version: str,
    *,
    paired_runtime: bool,
) -> str:
    llvm_target = {"x86_64": "X86", "aarch64": "AArch64"}.get(selection.target_arch)
    if llvm_target is None:
        raise ConfigurationError(
            f"unsupported managed LLVM target architecture: {selection.target_arch}"
        )
    prefix = render_common_script(
        selection,
        triplet,
        backend_triplet,
        backend_version,
        paired_runtime=paired_runtime,
    )
    compiler_install = r"""
mkdir -p "$ARTIFACTS/compiler/bin" "$ARTIFACTS/compiler/lib/clang"
install -m 0755 -T "$BUILD_ROOT/llvm/bin/clang" \
  "$ARTIFACTS/compiler/bin/clang"
ln -s clang "$ARTIFACTS/compiler/bin/clang++"
readonly RESOURCE_DIR="$("$BUILD_ROOT/llvm/bin/clang" -print-resource-dir)"
case "$RESOURCE_DIR" in
  "$BUILD_ROOT/llvm/lib/clang/"*) ;;
  *) echo "clang reported an unexpected resource directory: $RESOURCE_DIR" >&2; exit 1 ;;
esac
test -d "$RESOURCE_DIR"
cp -a -- "$RESOURCE_DIR" "$ARTIFACTS/compiler/lib/clang/"
install_target_tools "$ARTIFACTS/compiler"
vendor_host_dependencies "$ARTIFACTS/compiler"
"$ARTIFACTS/compiler/bin/clang" --version | \
  grep -Eq "^clang version $VERSION([[:space:]-]|$)"
"$ARTIFACTS/compiler/bin/clang++" --version | \
  grep -Eq "^clang version $VERSION([[:space:]-]|$)"
test "$("$ARTIFACTS/compiler/bin/clang" --target="$TARGET" -dumpmachine)" = "$TARGET"
test "$("$ARTIFACTS/compiler/bin/clang++" --target="$TARGET" -dumpmachine)" = "$TARGET"
test -d "$("$ARTIFACTS/compiler/bin/clang" -print-resource-dir)"
"""
    runtime_artifacts = "$RUNTIME_ARTIFACTS" if paired_runtime else "$ARTIFACTS"
    paired_licenses = (
        r"""
mkdir -p "$RUNTIME_ARTIFACTS/licenses"
cp -a -- "$PRIMARY_AVAILABLE_ARTIFACTS/licenses/llvm-project" \
  "$RUNTIME_ARTIFACTS/licenses/llvm-project"
"""
        if paired_runtime
        else ""
    )
    runtime_install = rf"""
cmake --build "$BUILD_ROOT/llvm" --target runtimes --parallel "$JOBS"
env DESTDIR="{runtime_artifacts}/.runtime-install" \
  cmake --build "$BUILD_ROOT/llvm" \
  --target install-builtins install-runtimes install-clang-resource-headers \
  --parallel "$JOBS"
mv "{runtime_artifacts}/.runtime-install$PREFIX" \
  "{runtime_artifacts}/runtime"
rm -rf -- "{runtime_artifacts}/.runtime-install"
{paired_licenses}
if find "{runtime_artifacts}/runtime" \
    \( -name 'libstdc++.so*' -o -name 'libgcc_s.so*' \) -print -quit | grep -q .; then
  echo "GCC runtime leaked into managed LLVM runtime" >&2
  exit 1
fi
"""
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
            + runtime_install
            + r"""
fi
"""
            if paired_runtime
            else compiler_install
        )
    else:
        publish = runtime_install
    compiler_build = (
        r"""
if test "$PRESERVE_PRIMARY" = 0; then
  cmake --build "$BUILD_ROOT/llvm" --parallel "$JOBS" \
    --target clang clang-resource-headers
fi
"""
        if paired_runtime
        else r"""
cmake --build "$BUILD_ROOT/llvm" --parallel "$JOBS" \
  --target clang clang-resource-headers
"""
    )
    return (
        prefix
        + rf"""
test -f /sources/source.tar.xz
readonly SOURCE_SHA512={selection.source.sha512!r}
readonly LLVM_SOURCE_ID="format=1 sha512=$SOURCE_SHA512"
readonly LLVM_SOURCE_MARKER="$SOURCE_ROOT/llvm-project/.linux-toolchain-source-ready"
llvm_source_ready=0
if test -f "$LLVM_SOURCE_MARKER" && \
    test "$(cat "$LLVM_SOURCE_MARKER")" = "$LLVM_SOURCE_ID" && \
    test -f "$SOURCE_ROOT/llvm-project/llvm/CMakeLists.txt"; then
  llvm_source_ready=1
fi
if test "$llvm_source_ready" -ne 1; then
  readonly LLVM_SOURCE_STAGING="$SOURCE_ROOT/.llvm-project.extracting"
  rm -rf -- "$LLVM_SOURCE_STAGING" "$SOURCE_ROOT/llvm-project"
  mkdir "$LLVM_SOURCE_STAGING"
  tar -xf /sources/source.tar.xz \
    -C "$LLVM_SOURCE_STAGING" --strip-components=1
  test -f "$LLVM_SOURCE_STAGING/llvm/CMakeLists.txt"
  printf '%s\n' "$LLVM_SOURCE_ID" \
    >"$LLVM_SOURCE_STAGING/.linux-toolchain-source-ready"
  mv -- "$LLVM_SOURCE_STAGING" "$SOURCE_ROOT/llvm-project"
fi
if test "$PRESERVE_PRIMARY" = 0; then
  copy_source_licenses "$SOURCE_ROOT/llvm-project" llvm-project
fi
readonly LLVM_CONFIG_ID="format=1 sha512=$SOURCE_SHA512 target=$TARGET backend=$BACKEND_TARGET-$BACKEND_VERSION linkage=both"
readonly LLVM_CONFIG_MARKER="$BUILD_ROOT/llvm/.linux-toolchain-configured"
if test ! -f "$LLVM_CONFIG_MARKER" || \
    test "$(cat "$LLVM_CONFIG_MARKER" 2>/dev/null || true)" != "$LLVM_CONFIG_ID" || \
    test ! -f "$BUILD_ROOT/llvm/CMakeCache.txt" || \
    test ! -f "$BUILD_ROOT/llvm/build.ninja"; then
  rm -rf -- "$BUILD_ROOT/llvm"
  cmake -G Ninja \
  -S "$SOURCE_ROOT/llvm-project/llvm" \
  -B "$BUILD_ROOT/llvm" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER="$BACKEND_CC" \
  -DCMAKE_CXX_COMPILER="$BACKEND_CXX" \
  -DCMAKE_ASM_COMPILER="$BACKEND_CC" \
  -DCMAKE_AR=/compiler-backend/bin/$BACKEND_TARGET-ar \
  -DCMAKE_RANLIB=/compiler-backend/bin/$BACKEND_TARGET-ranlib \
  -DCMAKE_NM=/compiler-backend/bin/$BACKEND_TARGET-nm \
  -DCMAKE_STRIP=/compiler-backend/bin/$BACKEND_TARGET-strip \
  -DCMAKE_LINKER=/compiler-backend/bin/$BACKEND_TARGET-ld \
  '-DCMAKE_EXE_LINKER_FLAGS=-static-libstdc++ -static-libgcc' \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DLLVM_ENABLE_PROJECTS=clang \
  '-DLLVM_ENABLE_RUNTIMES=compiler-rt;libunwind;libcxxabi;libcxx' \
  '-DBUILTINS_CMAKE_ARGS=-DCMAKE_SYSROOT=/sdk/sysroot' \
  "-DRUNTIMES_CMAKE_ARGS=-DCMAKE_SYSROOT=/sdk/sysroot;-DCMAKE_LINKER=/target-tools/$TARGET-ld;-DCMAKE_C_FLAGS=--ld-path=/target-tools/$TARGET-ld;-DCMAKE_CXX_FLAGS=--ld-path=/target-tools/$TARGET-ld;-DCMAKE_ASM_FLAGS=--ld-path=/target-tools/$TARGET-ld;-DCMAKE_SHARED_LINKER_FLAGS=-rtlib=compiler-rt;-DCMAKE_EXE_LINKER_FLAGS=-rtlib=compiler-rt;-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY" \
  -DCOMPILER_RT_INCLUDE_TESTS=OFF \
  -DCOMPILER_RT_BUILD_SANITIZERS=OFF \
  -DCOMPILER_RT_BUILD_XRAY=OFF \
  -DCOMPILER_RT_BUILD_LIBFUZZER=OFF \
  -DCOMPILER_RT_BUILD_MEMPROF=OFF \
  -DCOMPILER_RT_BUILD_ORC=OFF \
  -DCOMPILER_RT_BUILD_CTX_PROFILE=OFF \
  -DCOMPILER_RT_BUILD_GWP_ASAN=OFF \
  -DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON \
  -DCOMPILER_RT_BUILD_CRT=ON \
  -DLIBUNWIND_ENABLE_SHARED=ON \
  -DLIBUNWIND_ENABLE_STATIC=ON \
  -DLIBCXXABI_ENABLE_SHARED=ON \
  -DLIBCXXABI_ENABLE_STATIC=ON \
  -DLIBCXX_ENABLE_SHARED=ON \
  -DLIBCXX_ENABLE_STATIC=ON \
  -DLIBCXX_ENABLE_STATIC_ABI_LIBRARY=ON \
  -DLIBCXX_STATICALLY_LINK_ABI_IN_STATIC_LIBRARY=ON \
  -DLIBCXX_STATICALLY_LINK_ABI_IN_SHARED_LIBRARY=OFF \
  -DLIBCXX_ENABLE_ABI_LINKER_SCRIPT=ON \
  -DLIBCXX_HAS_ATOMIC_LIB=OFF \
  -DLIBUNWIND_USE_COMPILER_RT=ON \
  -DLIBCXX_USE_COMPILER_RT=ON \
  -DLIBCXXABI_USE_COMPILER_RT=ON \
  -DLIBCXXABI_USE_LLVM_UNWINDER=ON \
  -DLLVM_DEFAULT_TARGET_TRIPLE="$TARGET" \
  -DLLVM_TARGETS_TO_BUILD={llvm_target} \
  -DLLVM_INSTALL_TOOLCHAIN_ONLY=ON \
  -DLLVM_APPEND_VC_REV=OFF \
  -DLLVM_ENABLE_ASSERTIONS=OFF \
  -DLLVM_ENABLE_ZLIB=OFF \
  -DLLVM_ENABLE_ZSTD=OFF \
  -DLLVM_ENABLE_CURL=OFF \
  -DLLVM_ENABLE_LIBXML2=OFF \
  -DLLVM_ENABLE_TERMINFO=OFF \
  -DLLVM_INCLUDE_TESTS=OFF \
  -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DCLANG_INCLUDE_TESTS=OFF \
  -DLLVM_ENABLE_RTTI=OFF \
  -DLLVM_ENABLE_EH=OFF
  printf '%s\n' "$LLVM_CONFIG_ID" >"$LLVM_CONFIG_MARKER"
fi
{compiler_build}
{publish}
"""
    )
