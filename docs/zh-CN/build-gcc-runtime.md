# 为 SDK 构建 GCC 运行时

[English](../build-gcc-runtime.md) | [简体中文](build-gcc-runtime.md)

如果希望固定 libstdc++ 和 libgcc，只准备 glibc SDK 还不够。还需要用同一个 SDK 构建
GCC 提供的 C++ 头文件、CRT 对象、libgcc 和 libstdc++，再把这些文件导入运行时层。

这个过程会用到三类编译器，它们的用途不同：

| 编译器 | 用途 | 是否发布 |
| --- | --- | --- |
| crosstool-NG compiler backend | 构建 SDK 和托管 Compiler Kit | 不发布，只属于生产端构建过程 |
| GCC 构建目录中的 `xgcc`/`xg++` | 针对所选 SDK 构建 GCC 运行时 | 不发布；导入时只用 `xg++` 核对身份 |
| 外部 GCC 或 Clang | 通过绑定编译最终项目及其依赖 | 不发布；绑定记录路径 |

同一个 GCC 版本、目标架构、glibc 下限和 GCC 配置只需构建一次运行时。只要
`bind external` 验证通过，针对较低 glibc 下限构建的运行时可以与较新的 SDK 配合。
外部 GCC 与 GCC 运行时的主版本必须一致；Clang 也可以使用这个 GCC 运行时层。

## 准备构建输入

开始前准备好：

- 一个已发布的 SDK 目录，其中包含 `manifest.json` 和 `sysroot/`；
- 要发布的 GCC 源码，以及 GCC 所需的 GMP、MPFR、MPC 和 ISL；
- 本机 C/C++ 编译器和常规 GCC 构建工具；
- 与 SDK target triplet 匹配的 `as`、`ld`、`ar`、`nm`、`ranlib`、`objcopy`、
  `objdump`、`readelf` 和 `strip`。

SDK 构建工作目录中的私有 `toolchain/bin` 可以提供这些目标工具，也可以使用单独构建
且来源有记录的 target binutils。它们只是构建输入，不属于发布的 SDK 或运行时层。

GCC 构建目录、临时安装前缀和最终运行时目录都必须是新目录。不要把临时前缀安装到
`/usr`，因为其中会暂时出现导入程序必须排除的编译器可执行文件。

## 选择 SDK 和 GCC 源码

下面的示例从 SDK 清单读取 target triplet、架构和 glibc 下限，避免 GCC 构建意外
使用另一套目标配置：

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

创建输出目录前检查所有输入：

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

清除会把 GCC 引向主机头文件或库的搜索路径变量：

```bash
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH LD_RUN_PATH
unset LD_LIBRARY_PATH COMPILER_PATH GCC_EXEC_PREFIX CCC_OVERRIDE_OPTIONS
export PATH="${TARGET_TOOLS}:${PATH}"
```

## 构建核心运行时

参考配置为 SDK 的原生 target 构建非 bootstrap GCC，只构建核心运行时需要的目标库。
`--disable-multilib` 可以避免 x86-64 运行时中混入 32 位或 x32 文件。

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

`install-gcc` 会安装 GCC 内建头文件、修正后的头文件以及 `crtbegin`/`crtend`，同时也会
把编译器程序放入临时前缀。这些程序只在构建运行时时需要。`runtime import-gcc` 会
拒绝把 `bin/`、`cc1`、编译器插件和其他编译器程序带入最终运行时层。导入后的绑定
禁用 LTO，也不会使用本配置生成的 LTO 程序。

导入前核对构建目录中的 `xg++`：

```bash
export PROBE_GXX="${GCC_BUILD}/gcc/xg++"

test -x "${PROBE_GXX}"
test "$("${PROBE_GXX}" -dumpmachine)" = "${TARGET}"
test "$("${PROBE_GXX}" -dumpfullversion -dumpversion)" = "${GCC_VERSION}"
```

## 可选运行时库

本节描述底层 external runtime 工作流。GCC 16 及更新版本的托管生成总会构建并安装
`libatomic`，因为这些版本的驱动默认按需链接它。x86-64 托管生成会随配套 GCC
runtime 一起构建 libquadmath，AArch64 托管生成则会禁用它。

如果目标或项目需要通过函数调用实现的原子操作，在导入前构建 `libatomic`：

```bash
make -C "${GCC_BUILD}" -j"${JOBS}" all-target-libatomic
make -C "${GCC_BUILD}" install-target-libatomic
```

核心配置默认禁用 `libquadmath`。只有目标支持 `__float128` 且项目确实需要时，才应从
同一份 GCC 源码单独构建它。构建时必须通过 `-B` 明确指定与目标匹配的 `as` 和 `ld`：

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

导入程序只在临时前缀具有可供链接的库时包含 `libatomic` 或 `libquadmath`，并对它们
执行与核心运行时相同的架构、归档、符号版本、SONAME、RPATH/RUNPATH 和符号链接检查。
托管 GCC 16 及更新版本发布 runtime 时要求安装结果包含
`libgcc_s_asneeded.so`、`libatomic_asneeded.so` 和 `libatomic_asneeded.a`；
缺少任一输入都会在创建 binding 前失败。两个共享库脚本只有在语法与 GCC 自包含形式
完全一致、且引用的常规库也存在于导出的 runtime 中时才会被接受；静态别名必须解析到
runtime 内部，并通过其 `libatomic.a` 目标接受校验。

## 导入并验证运行时层

安装完所有需要的可选库后，先复制 GCC 许可证文件，再执行导入：

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

导入命令要求运行时层至少包含：

- 目标专用 C++ 头文件，包括 `bits/c++config.h`；
- GCC 内建头文件和修正后的头文件；
- `crtbegin*`、`crtend*`、`libgcc.a`，以及目标提供的 `libgcc_eh.a`；
- 共享 `libgcc_s`，以及共享和静态 `libstdc++`；
- 与声明架构一致的 ELF64 小端 CRT 和归档成员。

导入过程还会检查共享运行时是否超过 `GLIBC_FLOOR`，并拒绝 `GLIBC_PRIVATE`、不可迁移
动态路径、旧 glibc 下限中的 `DT_RELR`、越界符号链接、thin archive 和编译器程序。
生成的 `manifest.json` 会记录 GCC 版本、目标、文件位置和符号版本报告。

## 创建绑定并做链接验证

运行时导入完成后，创建绑定来验证 C/C++ 链接、异常和共享库。外部 GCC 的主版本必须
与运行时一致；Clang 可以使用同一个 GCC 运行时层。

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

AArch64 使用同一套流程，但必须在 AArch64 producer 上运行，SDK、原生 target binutils
和外部编译器都必须选择 AArch64。
使用方通过普通 `-march` 或 `-mcpu` 编译参数选择自己的 AArch64 指令集基线。

## 发布时应归档的材料

每个已发布运行时至少应保存：

- GCC 源码及其前置依赖压缩包的哈希；
- `${SDK_ROOT}/manifest.json`；
- target binutils 的版本和位置；
- `${GCC_BUILD}/config.status --config` 的输出；
- `${RUNTIME_OUT}/manifest.json` 和完整运行时目录；
- 绑定清单和最终部署目录的递归 ELF 检查结果。

这些记录说明运行时由哪些输入生成、通过了哪些检查。最终发布仍需验证完整动态加载
依赖闭包，并在目标环境和真实项目中运行。
