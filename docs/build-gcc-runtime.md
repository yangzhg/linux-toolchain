# Build a GCC runtime for an SDK

[English](build-gcc-runtime.md) | [简体中文](zh-CN/build-gcc-runtime.md)

A runtime-pinned C++ build needs GCC-owned headers, CRT objects, `libgcc`, and
`libstdc++` that were built against the selected glibc SDK. The build described
here produces those inputs in a staging prefix and then lets
`runtime import-gcc` filter and validate them.

Three compilers have different roles:

| Compiler | Purpose | Published artifact |
| --- | --- | --- |
| crosstool-NG compiler backend | Builds the SDK and managed Compiler Kits | None; it is producer build machinery |
| GCC build-tree `xgcc`/`xg++` | Builds the selected GCC target runtime against the SDK | None; the importer uses `xg++` only as an identity probe |
| External GCC or Clang | Compiles the consumer and target packages through a binding | None; the binding records its path |

The GCC source build is a one-time production step for a GCC version, target
architecture, runtime glibc floor, and relevant GCC configuration. It is not
repeated for each consumer build. A runtime built at a lower floor may be reused
with SDKs at newer floors when `bind external` accepts the pair. GCC bindings
require the external frontend and runtime provider to have the same major
version; Clang bindings may use the same GCC runtime overlay.

## Inputs

Prepare these inputs before configuring GCC:

- a published SDK directory containing `manifest.json` and `sysroot/`;
- GCC release sources matching the runtime version to publish, including GCC's
  GMP, MPFR, MPC, and ISL prerequisites or equivalent host development
  packages;
- a native C and C++ compiler plus the normal GCC build tools;
- target binutils for the SDK triplet (`as`, `ld`, `ar`, `nm`, `ranlib`,
  `objcopy`, `objdump`, `readelf`, and `strip`).

The private `toolchain/bin` directory retained in an SDK build workspace is a
suitable source of target binutils. It is build machinery, not part of the
published SDK or runtime overlay. A separately built target-binutils
installation is also valid when its provenance is recorded.

Use fresh GCC build, staging-prefix, and runtime-output directories. Do not
install this staging prefix into `/usr`; it temporarily contains compiler
executables that the importer must remove.

## Select the SDK and GCC source

Set the paths that belong to the build environment. The target triplet,
architecture, and runtime floor are read from the SDK manifest so the GCC build
cannot silently target a different SDK identity.

```bash
set -eu

export SDK_ROOT="$HOME/linux-toolchain/sdk/glibc-2.19"
export GCC_VERSION="13.4.0"
export GCC_SRC="$HOME/src/gcc-${GCC_VERSION}"
export TARGET_TOOLS="$HOME/linux-toolchain/workspaces/glibc-2.19/toolchain/bin"
export BUILD_ROOT="$HOME/linux-toolchain/build/gcc-runtime"
JOBS="$(getconf _NPROCESSORS_ONLN)"
export JOBS

export SYSROOT="${SDK_ROOT}/sysroot"
TARGET="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["target"]["triplet"])' \
  "${SDK_ROOT}/manifest.json")"
export TARGET
ARCH="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["target"]["arch"])' \
  "${SDK_ROOT}/manifest.json")"
export ARCH
GLIBC_FLOOR="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["target"]["libc_version"])' \
  "${SDK_ROOT}/manifest.json")"
export GLIBC_FLOOR
BUILD_TRIPLET="$(gcc -dumpmachine)"
export BUILD_TRIPLET

export GCC_BUILD="${BUILD_ROOT}/gcc-${GCC_VERSION}-${TARGET}-glibc-${GLIBC_FLOOR}"
export GCC_PREFIX="${BUILD_ROOT}/prefix-${GCC_VERSION}-${TARGET}-glibc-${GLIBC_FLOOR}"
export RUNTIME_OUT="$HOME/linux-toolchain/runtimes/gcc-${GCC_VERSION}-${ARCH}-glibc-${GLIBC_FLOOR}"
```

Check the inputs before creating any output:

```bash
test -f "${SDK_ROOT}/manifest.json"
test -d "${SYSROOT}/usr/include"
test -x "${GCC_SRC}/configure"

for tool in as ld ar nm ranlib objcopy objdump readelf strip; do
  test -x "${TARGET_TOOLS}/${TARGET}-${tool}"
done

test ! -e "${GCC_BUILD}"
test ! -e "${GCC_PREFIX}"
test ! -e "${RUNTIME_OUT}"

mkdir -p "${BUILD_ROOT}" "$(dirname "${RUNTIME_OUT}")"
mkdir "${GCC_BUILD}" "${GCC_PREFIX}"
```

Use a clean build environment. In particular, do not let compiler search-path
variables redirect the GCC build to unrelated host headers or libraries:

```bash
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_RUN_PATH
unset LD_LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX CCC_OVERRIDE_OPTIONS
export PATH="${TARGET_TOOLS}:${PATH}"
```

## Configure and build the core runtime

The reference configuration builds a non-bootstrap GCC for the SDK's native
target and only the libraries needed by the core runtime. The example uses GCC
13.4.0.
`--disable-multilib` prevents 32-bit or x32 objects from entering an x86-64
runtime prefix.

```bash
(
  cd "${GCC_BUILD}" || exit
  "${GCC_SRC}/configure" \
    --build="${BUILD_TRIPLET}" \
    --host="${BUILD_TRIPLET}" \
    --target="${TARGET}" \
    --prefix="${GCC_PREFIX}" \
    --with-sysroot="${SYSROOT}" \
    --with-build-sysroot="${SYSROOT}" \
    --with-native-system-header-dir=/usr/include \
    --disable-bootstrap \
    --disable-multilib \
    --disable-nls \
    --disable-werror \
    --disable-libsanitizer \
    --disable-libgomp \
    --disable-libquadmath \
    --disable-libssp \
    --disable-libvtv \
    --disable-libstdcxx-pch \
    --enable-shared \
    --enable-threads=posix \
    --enable-languages=c,c++,lto
)

make -C "${GCC_BUILD}" -j"${JOBS}" all-gcc
make -C "${GCC_BUILD}" -j"${JOBS}" all-target-libgcc
make -C "${GCC_BUILD}" -j"${JOBS}" all-target-libstdc++-v3

make -C "${GCC_BUILD}" install-gcc
make -C "${GCC_BUILD}" install-target-libgcc
make -C "${GCC_BUILD}" install-target-libstdc++-v3
```

`install-gcc` supplies GCC builtin and fixed headers plus `crtbegin` and
`crtend`; it also places compiler programs in the staging prefix. Those programs
are required only while producing the runtime. `runtime import-gcc` rejects
`bin/`, `cc1`, compiler plugins, and other compiler executables in its exported
artifact. The imported binding disables LTO and does not consume the LTO
programs built by this configuration.

Check the build-tree probe before importing the prefix:

```bash
export PROBE_GXX="${GCC_BUILD}/gcc/xg++"

test -x "${PROBE_GXX}"
test "$("${PROBE_GXX}" -dumpmachine)" = "${TARGET}"
test "$("${PROBE_GXX}" -dumpfullversion -dumpversion)" = "${GCC_VERSION}"
```

## Optional GCC libraries

This section describes the lower-level external-runtime workflow. Managed GCC
16 and newer production always builds and installs `libatomic`, because those
drivers link it as needed by default. Managed x86-64 production builds
libquadmath with its paired GCC runtime, while managed AArch64 production
disables it.

Build `libatomic` before import when the target or application needs out-of-line
atomic operations:

```bash
make -C "${GCC_BUILD}" -j"${JOBS}" all-target-libatomic
make -C "${GCC_BUILD}" install-target-libatomic
```

Build only optional libraries that the selected GCC release and target support,
and retain their configuration as part of the runtime production evidence.

The core configuration disables `libquadmath`. If the final target exposes
GCC's `__float128` runtime and the application needs it, build it from the same
GCC source against the just-installed target compiler, then install it into the
same staging prefix before import:

```bash
export QUADMATH_BUILD="${BUILD_ROOT}/libquadmath-${GCC_VERSION}-${TARGET}-glibc-${GLIBC_FLOOR}"
export GCC_DRIVER_TOOLS="${BUILD_ROOT}/gcc-driver-tools-${TARGET}"

test ! -e "${QUADMATH_BUILD}"
test ! -e "${GCC_DRIVER_TOOLS}"
mkdir "${QUADMATH_BUILD}" "${GCC_DRIVER_TOOLS}"
ln -s "${TARGET_TOOLS}/${TARGET}-as" "${GCC_DRIVER_TOOLS}/as"
ln -s "${TARGET_TOOLS}/${TARGET}-ld" "${GCC_DRIVER_TOOLS}/ld"

test "$("${GCC_PREFIX}/bin/${TARGET}-gcc" \
  -B"${GCC_DRIVER_TOOLS}/" -print-prog-name=as)" = \
  "${GCC_DRIVER_TOOLS}/as"
test "$("${GCC_PREFIX}/bin/${TARGET}-gcc" \
  -B"${GCC_DRIVER_TOOLS}/" -print-prog-name=ld)" = \
  "${GCC_DRIVER_TOOLS}/ld"

(
  cd "${QUADMATH_BUILD}" || exit
  env \
    PATH="${TARGET_TOOLS}:${PATH}" \
    CC="${GCC_PREFIX}/bin/${TARGET}-gcc -B${GCC_DRIVER_TOOLS}/" \
    AR="${TARGET_TOOLS}/${TARGET}-ar" \
    RANLIB="${TARGET_TOOLS}/${TARGET}-ranlib" \
    "${GCC_SRC}/libquadmath/configure" \
      --build="${BUILD_TRIPLET}" \
      --host="${TARGET}" \
      --target="${TARGET}" \
      --prefix="${GCC_PREFIX}" \
      --disable-multilib
)

make -C "${QUADMATH_BUILD}" -j"${JOBS}"
make -C "${QUADMATH_BUILD}" install
```

The importer includes `libatomic` and `libquadmath` only when their linker-facing
libraries are present. It applies the same architecture, archive, symbol-version,
SONAME, RPATH/RUNPATH, and symlink checks used for the core runtime. Managed GCC
16 and newer publication requires the installed `libgcc_s_asneeded.so`,
`libatomic_asneeded.so`, and `libatomic_asneeded.a` linker inputs and fails
before binding creation if any are absent. The two shared-library scripts are
accepted only when their syntax exactly matches GCC's self-contained form and
every referenced regular library is present in the exported runtime. The
static alias must resolve inside the runtime and is validated through its
`libatomic.a` target.

## Import and validate the runtime

Import the staging prefix only after all required optional libraries have been
installed:

```bash
LICENSE_ARTIFACT="${BUILD_ROOT}/runtime-licenses"
install -d "${LICENSE_ARTIFACT}/licenses/gcc"
install -m 0644 "${GCC_SRC}/COPYING" \
  "${LICENSE_ARTIFACT}/licenses/gcc/COPYING"
install -m 0644 "${GCC_SRC}/COPYING.RUNTIME" \
  "${LICENSE_ARTIFACT}/licenses/gcc/COPYING.RUNTIME"

linux-toolchain runtime import-gcc \
  --prefix "${GCC_PREFIX}" \
  --licenses "${LICENSE_ARTIFACT}" \
  --probe-gxx "${PROBE_GXX}" \
  --glibc-floor "${GLIBC_FLOOR}" \
  --arch "${ARCH}" \
  --output "${RUNTIME_OUT}"
```

The command fails unless the exported tree contains:

- target-specific C++ headers, including `bits/c++config.h`;
- GCC builtin and fixed headers;
- `crtbegin*`, `crtend*`, `libgcc.a`, and `libgcc_eh.a` where provided;
- shared `libgcc_s` and shared and static `libstdc++`;
- target ELF64 little-endian CRT and archive members for the declared
architecture.

It also audits shared runtime libraries against `GLIBC_FLOOR`, rejects
`GLIBC_PRIVATE`, non-relocatable dynamic paths, old-floor `DT_RELR`, escaping symlinks,
thin archives, and compiler payloads. The resulting `manifest.json` records the
GCC provider version and major, target, artifact locations, and symbol-version
reports.

Create a binding to perform the C and C++ link probes, including exceptions and
shared libraries. `BIND_SDK_ROOT` may select a newer SDK floor than the one used
to build the runtime. Use a distinct `BINDING_OUT` for every SDK and compiler
combination.

```bash
export BIND_SDK_ROOT="${SDK_ROOT}"
FINAL_CC="$(command -v gcc-13)"
export FINAL_CC
FINAL_CXX="$(command -v g++-13)"
export FINAL_CXX
export BINDING_OUT="$HOME/linux-toolchain/bindings/glibc-${GLIBC_FLOOR}-gcc-${GCC_VERSION}"

linux-toolchain bind external \
  --sdk "${BIND_SDK_ROOT}" \
  --runtime "${RUNTIME_OUT}" \
  --cc "${FINAL_CC}" \
  --cxx "${FINAL_CXX}" \
  --output "${BINDING_OUT}"
```

For Clang, replace `FINAL_CC` and `FINAL_CXX` with the selected Clang drivers
and use a separate binding output. The runtime is the GCC runtime built
above.

For AArch64, run the workflow on an AArch64 producer with an AArch64 SDK and
matching native target binutils. The external GCC or Clang selected during
binding must also report an AArch64 target; consumer builds choose their own
AArch64 instruction baseline through ordinary compiler flags such as `-march`
or `-mcpu`.

## Records to archive with the runtime

Archive these records with a published runtime:

- the GCC source and prerequisite archive hashes;
- `${SDK_ROOT}/manifest.json`;
- the target-binutils versions and locations;
- `${GCC_BUILD}/config.status --config` output;
- `${RUNTIME_OUT}/manifest.json` and the complete imported runtime tree;
- the binding manifest and final recursive ELF audit.

The caller supplies the SDK location, GCC source/version, target-binutils
location, build/output roots, job count, final compiler paths, and
optional runtime-library choice. The SDK manifest supplies the target triplet,
architecture, and runtime glibc floor.
