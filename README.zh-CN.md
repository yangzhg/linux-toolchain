# linux-toolchain

[English](README.md) | [简体中文](README.zh-CN.md)

`linux-toolchain` 为 Linux C/C++ 构建生成具有明确 glibc ABI 下限的输入。它既可绑定
已有 GCC/Clang，也可从固定源码构建托管编译器。生成的 binding 不依赖某个业务仓库，
支持 CMake、shell/Make 和可选 Conan 工作流。

项目目前处于 alpha 阶段。检查通过只能作为构建证据，不能替代在代表性目标环境中测试
最终产品。

## 制品模型

工具把四类制品分开管理：

| 层 | 内容 |
| --- | --- |
| glibc SDK | glibc 头文件和库、启动对象、动态加载器和 Linux UAPI 头文件 |
| Compiler Kit | 精确版本的托管 GCC/Clang 驱动和目标工具 |
| runtime overlay | GCC 或 LLVM 的 C++ 头文件、CRT 对象和运行时库 |
| binding | compiler launcher、目标工具、检查策略和所选项目接入文件 |

sysroot 只控制 libc 相关输入，不能单独固定 libstdc++、libgcc、libc++、compiler-rt
及其头文件和启动对象；这些属于 runtime overlay。

支持两种编译器模式：

- **外部模式**绑定机器上已有的 GCC 或 Clang。
- **托管模式**按内置 catalog 构建精确的 compiler/runtime 组合。托管 binding 的目标
  工具全部来自 Compiler Kit，不从主机 `PATH` 查找。托管 GCC 和 Clang 构建统一使用
  固定的 crosstool-NG compiler backend，而不是 host compiler。

制品归属和复用规则见[架构](docs/zh-CN/architecture.md)。

## 支持范围

- target：Linux x86-64 和小端 AArch64 ELF64；
- SDK catalog：固定的 glibc 2.17、2.19 和 2.23 至 2.42 条目；以
  `linux-toolchain sdk list` 的实际输出为准；
- 外部编译器：GCC 10+ 或 Clang 16+；
- 托管编译器：`linux-toolchain managed catalog` 列出的精确 GCC/LLVM 版本；
- 托管 Compiler Kit host：原生 `linux/x86_64` 和 `linux/aarch64`；managed target
  必须与 producer 架构一致。

catalog 中有条目只说明输入已经建模并固定，不代表所有 compiler、runtime、架构和
glibc 组合都完成了发布认证。具体说明见[兼容性边界](docs/zh-CN/compatibility.md)。

## 生成端要求

构建 SDK 或托管工具链需要：

- x86-64 或 AArch64 Linux 主机；
- Python 3.10 及以上版本和 `linux-toolchain` 命令；
- `readelf`、非 root 用户和本机 Linux Docker daemon；
- 获取源码和构建 builder image 所需的网络；
- 标准 smoke 流程所需的 CMake 和 Make；
- 验证 glibc 早于 2.36 的 AArch64 SDK 时，需要 `unshare`、`mount` 和已启用的非特权
  user namespace；
- 只有实际执行 Conan smoke 或 Conan 使用方构建时才需要 Conan 2。

开始生产构建前运行对应诊断：

```bash
linux-toolchain doctor --workflow managed --summary
```

## 设置托管工具链

在源码仓库中，最短流程是：

```bash
make setup COMPILER=gcc@12 GLIBC=2.19
```

默认 prefix 类似
`$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64`。`INTEGRATION` 选择生成端
主要 smoke 路径，默认是 `shell`；高层 setup 会在每套安装中同时生成 CMake、
shell/Make 和 Conan adapter。`JOBS` 默认是在线 CPU 数的四分之一，最少为 1。
`JOBS` 只控制执行并行度，
不会形成另一套 SDK 或 managed artifact 缓存身份。Make 工作流把 selection state 放在
`out/work/`，把可复用的生成端输入放在 `out/store/`。普通 `make clean` 会保留 store，
`make purge` 才会删除整棵仓库内输出目录。常见覆盖方式如下：

```bash
make setup \
  COMPILER=clang@22 \
  GLIBC=2.19 \
  RUNTIME=libc++ \
  INTEGRATION=cmake \
  PREFIX="$HOME/.local/lib/linux-toolchain/clang22-glibc219"
```

`LINUX_TOOLCHAIN_GNU_MIRROR` 用于选择 GNU 源码归档的下载基地址。producer 会先校验
并缓存 crosstool-NG 所需的全部源码归档，再启动禁止联网的编译容器。因此 mirror URL
只影响传输方式，不会形成另一套 SDK 身份。

builder image 默认使用 Ubuntu 普通软件源。如需固定软件包版本，可显式选择 Ubuntu
snapshot：

```bash
make bundle COMPILER=gcc@12 GLIBC=2.19 \
  UBUNTU_SNAPSHOT=20260701T000000Z
```

直接使用 CLI 时，需要在创建或验证 prepared artifact 的每条生成端命令上设置
`LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT=20260701T000000Z`。不设置或设为空值时使用普通软件源。
所选模式属于 builder 身份，因此 snapshot 与普通源生成的 artifact 不会混用。

Make target 会显示为当前 shell、Bash 和 Zsh 配置 PATH 的命令；直接执行
`linux-toolchain setup` 时还会把 launcher 路径写入 stdout。默认的 `shell` selection
可直接在任意项目目录用于 Make 和其他 shell 驱动的构建：

```bash
export PATH="$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64/bin:$PATH"

lxtc make -j8
```

launcher 可以在生成的编译器和目标工具环境中执行任意普通命令，并且不解析或改写使用方
参数。`INTEGRATION=cmake` 或 `INTEGRATION=conan` 用于选择对应 adapter 做生成端 smoke
认证，并开启该 smoke 路径的专用选项；高层安装仅为了携带 adapter 时无需指定。

如果已经安装 `linux-toolchain`，可直接执行：

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64"
```

`PREFIX` 是可独立使用的最终安装目录。`--work-dir` 只对应一套不可变 setup selection，
保存 `setup.json`、lock、binding、smoke 结果和准备态。`--store-dir` 按生成输入进行内容
寻址，保存可复用的 SDK workspace、已验证源码、managed 编译树和日志。两者默认位于
用户缓存目录。Make 工作流使用 `out/work/TOOLCHAIN_VARIANT` 作为 work directory，
使用 `out/store` 作为 producer store。需要跨 checkout
或 producer 共享时，可把 `STORE_DIR` 设为一个绝对共享路径。普通 `make clean` 会删除
selection 和 bundle 输出但保留仓库内 store；`make purge` 会删除它。

托管 setup 在 x86-64 或 AArch64 上原生构建，target 默认取 host 架构；传入不同的
`--arch` 会直接报错。因此 AArch64 producer 会从头构建 AArch64 SDK、compiler
backend、Compiler Kit 和 runtime。AArch64 上选择 managed GCC 时要求 GCC 10 或更新
版本。GCC 自动选择同版本 runtime；Clang 必须指定 `--runtime libc++` 或
`--runtime gcc@VERSION`。
托管 libc++ runtime 会同时发布 libc++、libc++abi、libunwind 的共享库与静态库，并
验证普通及全静态 C/C++ 链接。

Compiler Kit host floor 与 target SDK floor 是两条独立策略。高层流程未指定
`--host-glibc-floor` 时会让它跟随 `--glibc`。例如仅传 `--glibc 2.19`，发布的 managed
compiler 全部 host ELF 都不得依赖高于 `GLIBC_2.19` 的符号版本；随包 binutils 必须是
没有 glibc 依赖的静态 host ELF。只有确实要让两条策略不同，才显式传入
`--host-glibc-floor`。

work directory 和安装 prefix 各自只对应一套不可变选择。compiler、target、runtime、
integration 或策略变化时应使用新路径。多个 selection 可以共享一个 producer store，
输入身份相同的内容经验证后复用。`--force` 只授权修复或替换选择相同且由生成器管理的
selection 输出；已经验证通过的不可变 producer artifact 会继续复用，不会为执行
`--force` 而故意重编。只有 format-1、状态为 passed，且仍与当前 binding 和所选
integration 匹配的 smoke 结果，才能使准备态成为合格状态。

## 创建和安装 Bundle

在源码仓库中创建自解压安装器：

```bash
make bundle COMPILER=gcc@12 GLIBC=2.19
```

默认输出是 `out/linux-toolchain-gcc12-glibc219-x86_64.run`。`make bundle` 先准备并
验证生成端制品，再直接打包，不会先发布安装 prefix。需要时可覆盖 `WORK_DIR`、
`STORE_DIR` 或 `BUNDLE_OUTPUT`；`SETUP_OPTIONS` 和 `BUNDLE_OPTIONS` 分别向对应命令
传递附加参数。

对应的直接命令读取验证通过的准备态：

```bash
linux-toolchain bundle create \
  --config out/work/gcc12-glibc219-x86_64/setup.json \
  --state-directory out/work/gcc12-glibc219-x86_64/state \
  --output out/linux-toolchain-gcc12-glibc219-x86_64.run
```

也可以从已有安装 prefix 创建：

```bash
linux-toolchain bundle create \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64" \
  --output out/linux-toolchain-gcc12-glibc219-x86_64.run
```

把 Bundle 安装到不存在或为空的 prefix：

```bash
./out/linux-toolchain-gcc12-glibc219-x86_64.run \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219" \
  --launcher-name gcc12

export PATH="$HOME/.local/lib/linux-toolchain/gcc12-glibc219/bin:$PATH"
gcc12 make release
gcc12 info
```

launcher 默认名为 `lxtc`。安装 Bundle 不会调用，也不依赖 Python、Docker、Conan、
CMake、Make、源码仓库或网络；主机必须满足 Bundle 记录的架构和 Compiler Kit host
glibc 下限；使用默认 lxtc Conan build profile 时，还必须满足 target glibc 下限。
`lxtc info`（或安装时指定的 launcher 名加 `info`）会以稳定的 `key=value` 格式输出
已安装的编译器、target、libc、C++ runtime、integration 和 Conan 选择。

普通高层 Bundle 会用安装器中的静态文件创建专属 Conan home：
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`。`BUNDLE_DIGEST` 是 Bundle ID 的 SHA-256
摘要前 16 个十六进制字符。其中目标 `default` profile 和 build context 的
`lxtc-build` profile 都委托给安装后的托管工具链。安装时可覆盖 home 或目标 C++
标准，整个过程不会运行 Conan：

```bash
./out/linux-toolchain-gcc12-glibc219-x86_64.run \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219" \
  --conan-home "$HOME/.conan2_lxtc_gcc12" \
  --conan-cppstd gnu20
```

不指定 `--conan-cppstd` 时，生成的 profile 会写入 Conan 2 针对该托管编译器家族及
主版本所建模的编译器默认值。
`--conan-build-profile NAME_OR_PATH` 只是显式逃生口：名称指向专属 home 中的 profile，
绝对路径指向对应文件。覆盖项可以稍后创建；默认仍是生成的 `lxtc-build`。

安装后的 prefix 含本机路径。更换机器或 prefix 时，应重新运行原始 `.run` 文件，
不要直接移动安装目录。

## 使用方接入

高层 setup 安装及其 Bundle 同时包含三种原生 adapter。底层 binding 命令仍只生成显式
选择的 integration，未指定时默认生成 CMake 和 shell。

| 接入方式 | 生成入口 | 支持形式 |
| --- | --- | --- |
| CMake | `cmake/toolchain.cmake` | 原生 adapter |
| shell / Make | `env/toolchain.env` | 原生 adapter |
| Conan 2 | `conan/host.profile` | 可选 adapter |
| Autotools | shell 环境和 target triplet | 兼容路径 |
| 手写 Ninja | shell 环境或 wrapper 路径 | 兼容路径 |
| Meson / Bazel | 无 | 无原生 adapter |

不经过生成的 launcher 时，可直接使用 binding 入口：

```bash
BINDING="$PWD/out/binding-managed"

cmake -S . -B build/target \
  -DCMAKE_TOOLCHAIN_FILE="${BINDING}/cmake/toolchain.cmake" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/target

. "${BINDING}/env/toolchain.env"
make -j8
```

完整 Bundle 的 launcher 会同时选择生成的 Conan 目标 profile 和独立的托管原生 build
profile。完整示例及底层 binding 边界见[项目接入](docs/zh-CN/integrations.md)。

## 底层工作流

高层 `setup` 是普通托管流程。只有在 SDK 生产、编译器构建和发布需要分开执行或评审时，
才需要底层命令。

创建 SDK：

```bash
linux-toolchain sdk list
linux-toolchain sdk create \
  --glibc 2.19 \
  --arch x86_64 \
  --workspace out/sdk-glibc-2.19
```

把外部编译器绑定到 SDK 和已导入 runtime：

```bash
linux-toolchain bind external \
  --sdk out/sdk-glibc-2.19/sdk \
  --runtime out/runtime-gcc \
  --cc "${CC}" \
  --cxx "${CXX}" \
  --output out/binding-external
```

runtime 导入见[构建 GCC runtime](docs/zh-CN/build-gcc-runtime.md)，lock、构建和组装命令
见[托管编译器](docs/zh-CN/managed-compilers.md)。

## 验证 binding 和产品

为每个 binding 运行随包提供的项目验证：

```bash
linux-toolchain smoke \
  --binding "${BINDING}" \
  --integration cmake \
  --build-dir out/smoke-cmake
```

检查完整部署目录：

```bash
linux-toolchain audit \
  --policy "${BINDING}/audit-policy.json" \
  --recursive \
  /path/to/product
```

glibc floor 只限制公开 `GLIBC_*` 需求。内核 API、CPU 指令、动态加载器配置、依赖闭包、
插件和进程级 C++ runtime 仍是独立部署约束。`-march`、`-mcpu`、`-mtune` 等使用方
选项会原样透传。

## 文档

- [文档目录](docs/zh-CN/README.md)
- [命令行参考](docs/zh-CN/cli-reference.md)
- [架构](docs/zh-CN/architecture.md)
- [托管编译器](docs/zh-CN/managed-compilers.md)
- [项目接入](docs/zh-CN/integrations.md)
- [兼容性边界](docs/zh-CN/compatibility.md)
- [故障排查](docs/zh-CN/troubleshooting.md)
- [版本与制品格式](docs/zh-CN/artifact-formats.md)
- [参与贡献](CONTRIBUTING.zh-CN.md)
- [安全策略](SECURITY.zh-CN.md)
- [Apache 2.0 许可证](LICENSE)
