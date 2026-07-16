# 项目接入

[English](../integrations.md) | [简体中文](integrations.md)

生成的绑定描述一套确定的编译器和运行时配置，并只包含创建时选择的项目接入文件。
每个绑定都提供编译器及二进制工具的封装命令和 ELF 检查策略。重复传入
`--integration cmake|shell|conan` 可以选择多种接入方式；不传时默认生成 CMake 和
shell 接入，Conan 需要显式选择。项目不需要了解 SDK、编译器或运行时如何生成。

以下示例假设：

```bash
BINDING="$PWD/out/binding-managed"
```

目标 floor、架构、编译器或 runtime 策略变化后使用另一个完整 binding，不能在 binding
之间复制单个文件。

## 支持哪些构建系统

| 使用方 | 选择 | 生成内容 | 验证方式 |
| --- | --- | --- | --- |
| CMake | `--integration cmake` | 独立 toolchain file | 专用 smoke |
| POSIX shell / Make | `--integration shell` | compiler、目标工具和 pkg-config 环境 | Make smoke |
| Conan 2 | `--integration conan` | host profile 与 CMake 组合片段 | 专用 smoke |
| Autotools | `--integration shell` | 标准变量与 target triplet | 兼容路径，需真实 configure 测试 |
| 手写 Ninja | `--integration shell` | graph/生成过程使用 wrapper | 兼容路径，无原生 adapter |
| Meson | 不可用 | 未生成 cross file | 无原生 adapter |
| Bazel | 不可用 | 未注册 C++ toolchain | 无原生 adapter |

开始构建前，用对应选项检查所选 integration 的可执行工具依赖：

```bash
linux-toolchain doctor --workflow consumer --integration cmake
linux-toolchain doctor --workflow consumer --integration shell
linux-toolchain doctor --workflow consumer --integration conan
```

在独立于所有使用方仓库的 prefix 中创建本机安装。生成端状态默认放在用户缓存目录，
业务项目无需配置文件：

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --prefix /path/to/toolchains/gcc12-glibc219
export PATH=/path/to/toolchains/gcc12-glibc219/bin:"$PATH"
```

高层 setup 在 x86-64 主机运行，target 默认也是 x86-64，因此普通 native 场景无需
指定 `--arch`。发布者还可设置 `--host-glibc-floor`、`--jobs` 和 `--runner`；Clang
通过 `--runtime` 选择 libc++ 或明确版本的托管 GCC runtime。高层 setup 始终生成
CMake、shell 和 Conan adapter；`--integration` 选择其中哪一个接受生成端 smoke
认证，选择 Conan 时还会启用该生成端 smoke 的 `--conan-*` 选项。

launcher 会先加载 binding 的 shell 环境，再执行使用方命令；可从任意项目目录调用：

```bash
lxtc make -j8
```

setup 固定生成名为 `lxtc` 的 launcher。它加载自身 prefix 下的 binding，不依赖生成端
状态、Python 或管理程序。只安装 Python 包不会生成全局 launcher。setup 选择在工作
目录和安装 prefix 中都不可变；需要不同选择时应使用新路径。`--force` 只修复或替换
选择相同且由生成器管理的 selection 输出；已经验证通过的不可变 producer artifact
仍会复用，不会故意重编。

安装后的 launcher 会选择专属 Conan home，以及生成的 `default`、`lxtc-build`
profile。两个 context 都使用安装后的托管工具链，build context 还携带 runtime 搜索
路径；不同的既有 settings 或 profile 会被拒绝。显式传
`--profile:host`/`--profile:build` 时，可读取实际路径
`LINUX_TOOLCHAIN_CONAN_HOST_PROFILE` 和
`LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE`。

预构建 Bundle 在 `PREFIX/bin/NAME` 提供创建时选择的接入方式。安装默认使用
`lxtc`，也可通过 `--launcher-name NAME` 选择最终名称。所选 integration 在使用方机器
上不需要生成工具、Python 或 Docker。已安装 launcher 固定其 prefix；移动时应复制
原始 `.run` 并重新安装。

仅生成两个 package-manager-neutral 接入的示例：

```bash
linux-toolchain bind external \
  --sdk "${SDK}" \
  --runtime "${RUNTIME}" \
  --cc "${CC}" \
  --cxx "${CXX}" \
  --integration cmake \
  --integration shell \
  --output out/binding
```

## 直接使用 CMake

带 `--integration cmake` 的 binding 提供 `cmake/toolchain.cmake`：

```bash
cmake -S . -B build/target \
  -DCMAKE_TOOLCHAIN_FILE="${BINDING}/cmake/toolchain.cmake" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/target
```

切换 binding 时使用新的 build 目录，避免 `CMakeCache.txt` 保留旧 compiler/sysroot。
toolchain file 选择 `CMAKE_SYSROOT`、C/C++ compiler、linker 及 archive/binary tools；
使用方照常添加项目 flags、target dependencies 和 `-march` 等 CPU 指令选项。

## Make、Autotools 和 Ninja

带 `--integration shell` 的 binding 提供 shell-quoted `env/toolchain.env`：

```bash
. "${BINDING}/env/toolchain.env"
make -j8
```

它导出 `CC`、`CXX`、`AR`、`RANLIB`、`AS`、`NM`、`STRIP`、`OBJCOPY`、
`OBJDUMP`、`PATH`、`LINUX_TOOLCHAIN_TARGET`、`LINUX_TOOLCHAIN_SYSROOT` 和 target
pkg-config 设置；同时选 CMake 时还有 `CMAKE_TOOLCHAIN_FILE`。binding 选择 linker
时会导出 `LD`。wrapper 添加所选 sysroot/runtime flags，并透传使用方参数。

```bash
. "${BINDING}/env/toolchain.env"
./configure --host="${LINUX_TOOLCHAIN_TARGET}" --build="$(./config.guess)"
make -j8

. "${BINDING}/env/toolchain.env"
ninja -C build/target
```

生成 `build.ninja` 的程序必须读取这些变量或写入 wrapper 路径。预处理 `.S` 使用
`CC`，`AS` 是直接调用 raw assembler 的接口。

## Conan

Conan 是可选 adapter：

```bash
linux-toolchain bind external \
  --sdk "${SDK}" --runtime "${RUNTIME}" \
  --cc "${CC}" --cxx "${CXX}" \
  --integration conan \
  --conan-cppstd gnu17 \
  --conan-libcxx libstdc++11 \
  --conan-build-type Release \
  --output out/binding-conan
```

这些 `--conan-*` 只描述 host profile，不改变直接 wrapper、CMake 或 shell。不指定
`--conan-cppstd` 时，profile 会写入 Conan 2 针对所绑定编译器家族及主版本建模的
默认值。托管 binding
的 variant 决定真实 libc++/libstdc++，Conan 值必须一致。

```bash
conan_home="$(conan config home)"
linux-toolchain conan settings --output "${conan_home}/settings_user.yml"
```

相同 settings 可幂等复用，不同文件需显式 `--force` 或人工合并。生成的 profile 只
用于 Conan host context，build requirements 使用独立原生 profile：

```bash
conan install . \
  --profile:host="${BINDING}/conan/host.profile" \
  --profile:build=default \
  --build=missing \
  --output-folder=build/target

cmake -S . -B build/target \
  -DCMAKE_TOOLCHAIN_FILE=build/target/conan_toolchain.cmake \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/target
```

Conan toolchain 已组合 binding，不要同时再传 `${BINDING}/cmake/toolchain.cmake`。
Conan 填充 CMake 依赖搜索列表后，binding 会把 `CMAKE_PREFIX_PATH`、
`CMAKE_LIBRARY_PATH` 和 `CMAKE_INCLUDE_PATH` 中每个已存在的绝对路径视为显式可信的
target 输入 root。library、include 和 package 查找仍限制在可信 root 内，不会因此
回退到任意 host 路径；使用方手工向这些变量追加路径，也等于显式信任对应 target 输入。
`CMAKE_PROGRAM_PATH` 不会纳入 target root，原生 build tool 仍通过 program 查找使用。

完整的原生 managed Bundle 会同时提供两个 context，使用方通常无需传 profile：

```bash
lxtc conan install . --build=missing --output-folder=build/target
```

`.run` 安装器不调用 Conan，直接在专属 Conan home 中创建 `default` 和 `lxtc-build`。
`--conan-cppstd VALUE` 只覆盖目标 profile；`--conan-build-profile NAME_OR_PATH` 显式替换
生成的 build context，是使用方决定自行管理该 context 时的逃生口。这一 Bundle 行为
依赖高层 managed production 为原生模式，不会扩散到通用底层 binding。

release 构建应使用专用 Conan home，并为每个 target package 重建或建立来源证据；旧
cache 可能引入更高 libc、不同架构或不同 C++ runtime。

## 原生 managed 生成

高层 managed setup 只支持原生生成：x86-64 流程在 x86-64 producer 上运行，AArch64
流程在 AArch64 producer 上运行。AArch64 producer 会从头构建 AArch64 SDK、compiler
backend、Compiler Kit 和 runtime，不使用 amd64 模拟 builder：

```bash
linux-toolchain setup gcc@12 \
  --arch aarch64 \
  --glibc 2.19 \
  --prefix /path/to/toolchains/gcc12-aarch64-glibc219
```

setup 会拒绝与 producer 不同的 target 架构，Docker daemon 也必须报告所选原生
platform。生成的 binding 可使用创建时选择的任意 integration。feature detection 应
描述 target；优先使用 target 配置、编译/链接 probe 和显式 cache 答案。smoke 和最终
consumer test 要在有代表性的 target 机器上运行；静态 configure 成功不等于 target
认证通过。

## 检查部署结果

```bash
linux-toolchain audit \
  --policy "${BINDING}/audit-policy.json" \
  --recursive \
  /path/to/product
```

检查范围必须包括完整产品目录、随产品发布的 C++ 运行时和可选插件。部署目录还要用
明确的动态加载路径提供共享运行时，最后在目标动态加载器、内核、CPU 基线和实际进程
边界下运行测试。
