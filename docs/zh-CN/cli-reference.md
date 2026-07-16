# 命令行参考

[English](../cli-reference.md) | [简体中文](cli-reference.md)

命令行分为简洁的用户层和底层生产原语。完整参数以
`linux-toolchain COMMAND --help` 为准。

## 输出与退出状态

成功的写命令只在 stdout 输出主要结果路径；进度、诊断和简洁的子进程输出使用 stderr。
进度、状态和错误标签仅在交互终端中使用颜色；输出被重定向、环境中存在 `NO_COLOR`
或 `TERM=dumb` 时不使用颜色，JSON 输出始终不包含终端转义序列。

| 状态 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 诊断或 ELF 策略检查发现违规 |
| 2 | 输入错误、状态错误或操作失败 |
| 130 | 用户中断 |

管理命令遵循这些退出状态规则。生成的 launcher 验证状态后原样返回使用方命令状态，signal
终止映射为 `128 + signal`。预期错误不暴露 traceback。`linux-toolchain` 是管理和
发布接口；setup 固定生成 `lxtc`，bundle 安装时可以选择其他 launcher 名称。内部模块
不是公共 API。

## 设置并使用托管工具链

```bash
linux-toolchain setup COMPILER --glibc FLOOR [--prefix PREFIX] \
  [--work-dir WORK_DIR] [--store-dir STORE_DIR] \
  [--arch ARCH] [--integration cmake|shell|conan] \
  [--libstdcxx gcc@VERSION] \
  [--host-glibc-floor FLOOR] [--cmake-version VERSION] \
  [--jobs N] [--runner RUNNER] \
  [--conan-cppstd VALUE] [--conan-build-type VALUE] \
  [--conan-build-profile NAME] [--prepare-only] \
  [--no-path-instructions] [--force]
PREFIX/bin/lxtc COMMAND [ARG ...]
```

`setup` 在独立 prefix 下管理一套本机托管工具链，不读取或修改业务仓库。托管 setup
在 x86-64 或 AArch64 上原生运行，target 默认取 producer 架构；传入不同的 `--arch`
会直接报错。AArch64 上的 managed GCC 和 GCC runtime 要求 GCC 10 或更新版本。GCC
自动选择同一精确版本的 runtime。Clang 同时包含匹配 LLVM libc++ 和固定 GCC
libstdc++/libgcc runtime；`--libstdcxx` 选择 GCC provider，默认是 `gcc@12`。主要
接入方式默认为 `shell`，用于选择生成端使用方验证；每套高层 setup 安装都会携带
CMake、shell 和 Conan adapter。

`--host-glibc-floor` 是 Compiler Kit 内所有 host ELF 的独立审计上限。未指定时，setup
把它解析为 target `--glibc` 的值。发布的 compiler 和辅助程序不得要求更新的
`GLIBC_*`，其中 binutils 不能有动态 glibc 依赖；显式 host floor 可以与 target SDK
floor 不同。`--jobs` 控制 producer 并行度，但不属于内容寻址的 SDK 或 managed
artifact 身份。`-march`、
`-mcpu`、`-mtune` 等 CPU 指令选项属于使用方构建，
会由 compiler wrapper 透传。`--conan-cppstd` 和 `--conan-build-type` 独立于所选
验证 integration，用于配置生成的 Conan host profile；选择 Conan 验证时也会使用该
build type。`--conan-build-profile` 选择这次验证使用的生成端原生 profile，因此要求
`--integration conan`。非 Conan 验证不会调用 Conan，也不会创建生成端 Conan 运行状态。

`--cmake-version` 选择随架构变化的补充性 build-tools artifact 中 CMake、CTest 和
CPack 的 release，默认是 `3.31.12`；已固定的可选值为 `3.31.10`、`3.31.11` 和
`3.31.12`。源码仓库的 Make 包装使用 `CMAKE_VERSION` 暴露同一选择。该值属于不可变
setup state，并参与 build-tools identity。

未设置 `LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT` 或值为空时，builder image 使用 Ubuntu
普通软件源；设为 `YYYYMMDDTHHMMSSZ` 格式的时间戳时使用对应 Ubuntu snapshot。源码
仓库的 Make 包装把同一设置暴露为 `UBUNTU_SNAPSHOT`。这个值会改变 builder 身份；对
同一份 prepared state 分开执行 build、export 或验证命令时，必须保持一致。SDK、
build-tools、compiler 和 runtime 生产共用一个完全相同的 Docker image，因此各阶段
不会重复安装软件包或 crosstool-NG。

`WORK_DIR` 保存一套严格的 format-1 setup selection，以及 lock、binding、验证结果和
准备态。`STORE_DIR` 是共享的内容寻址 producer store，保存已验证源码、SDK workspace、
build tools、managed 编译树和日志；两者默认位于用户缓存目录。work directory 的
selection 不可变，
未指定 `WORK_DIR` 时，默认目录由规范化 `PREFIX` 的 basename 和稳定短 hash 共同派生，
因此不同路径下的同名 prefix 仍相互独立。多个 selection 可以复用 store 中身份相同的
内容。普通 setup 必须通过 `PREFIX` 指定最终自包含安装目录；已有 prefix 必须为空或包含
同一套验证通过的 selection。`--force` 只授权修复或替换选择相同且可证明由生成器拥有的
selection 输出；已经验证通过的不可变 producer artifact 仍会复用，不会故意重编。

`--prepare-only` 完成生成端验证并输出 `state/prepared.json`，不发布安装目录，也不输出
launcher PATH 提示。显式指定 `--work-dir` 时，该模式可以省略 `--prefix`；直接从准备态
创建 Bundle 时使用这一模式。只有 format-1、状态为 passed，且与当前 binding 和所选
integration 匹配的验证结果，才能使准备态保持合格。

成功时，面向人的进度写入 stderr，stdout 只输出 launcher 路径。耗时较长的生成阶段也
可能在 stderr 流式输出或汇总子进程信息。stderr 还会给出当前 shell 立即生效的命令，
以及直接追加到 `~/.bashrc` 或 `~/.zshrc` 的持久化命令。launcher 只依赖已安装
prefix，不依赖 Python、管理命令或 `WORK_DIR`。
`--no-path-instructions` 可省略这些面向人的提示，同时仍在 stdout 输出 launcher
路径，便于组合命令。

生成端验证会把 configure、build、audit、loader 和 runtime 输出保存在验证目录中。
setup 成功时只在 stderr 报告简洁的验证 `PASS`；命令失败时会回放输出并指出对应日志。

launcher 不会从当前目录或父目录查找配置。它加载已安装 binding 环境，并原样执行
剩余参数。搜索顺序是已安装 binding tools、Bundle 中的
CMake/CTest/CPack/Make/Ninja/ccache 目录，最后才是继承的 host `PATH`。ccache 可用，
但不会自动配置为 compiler launcher。每套高层安装都会选择专属 Conan home、生成的目标
profile 和托管原生 build profile；实际路径可通过
`LINUX_TOOLCHAIN_CONAN_HOST_PROFILE` 和
`LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE` 读取，命令行显式传入的 profile 仍然优先。

## 创建和安装单文件 Bundle

```bash
linux-toolchain bundle create \
  --config SETUP.json [--state-directory STATE] --output INSTALLER.run \
  [--id ID] [--force]
linux-toolchain bundle create \
  --prefix PREFIX --output INSTALLER.run \
  [--id ID] [--force]
```

使用 `--config` 时，`bundle create` 从显式 state directory 或 `setup.json` 同级的
`state/` 读取验证通过的准备态，不要求已有安装 prefix；使用 `--prefix` 时则验证已有
setup 安装。两条路径都会推导 SDK、Compiler Kit、runtime、variant 和 integrations，
复用生成端已验证的 binding 作为可迁移模板，并写出相同的确定性 shell 安装器。Python
只在生成端使用。

底层发布接口：

```bash
linux-toolchain bundle create-artifacts \
  --sdk SDK --build-tools BUILD_TOOLS \
  --compiler-kit COMPILER_KIT --runtime RUNTIME \
  --lock LOCK --variant VARIANT --output INSTALLER.run \
  [--id ID] \
  [--integration cmake|shell|conan ...] \
  [--conan-cppstd VALUE] [--conan-libcxx VALUE] \
  [--conan-build-type VALUE] [--force]
```

这里的 `BUILD_TOOLS` 是验证通过的原生 build-tools artifact，`RUNTIME` 是 variant
选择的已发布 runtime-set 目录。
`bundle create-artifacts` 接受显式组装的完整 managed 组合，并执行相同的生成端验证。
不传 `--integration` 时默认选择全部三种 adapter；显式选择仍保留底层控制。安装后的
工具链包含使用方 launcher，但不包含 Python runtime 或管理 CLI。安装前的 launcher
名称固定为 `lxtc`。输出已存在时默认报错；`--force` 会在新安装器完整生成后原子替换
已有普通文件，但不会替换目录、符号链接或其他非普通路径。Makefile 形式为
`make bundle ... BUNDLE_OPTIONS=--force`。

直接运行生成的文件进行安装：

```bash
./INSTALLER.run [--prefix PREFIX] \
  [--launcher-name NAME] [--conan-home PATH] \
  [--conan-cppstd VALUE] [--conan-build-profile NAME_OR_PATH]
```

shell 安装器要求 Linux、记录的 host 架构和最低 glibc、POSIX shell，以及常见 Unix
归档工具。未指定 `--prefix` 时，它安装到
`$HOME/.local/lib/linux-toolchain/<NAME>`；`NAME` 在生成 Bundle 时根据已选择的
compiler、target、runtime 和非默认 CMake 版本写入，重命名安装器不会改变该默认值。
显式 `--prefix` 会覆盖它；不指定 `--prefix` 时必须有有效的 `HOME`。
`--launcher-name` 选择安装后的命令名。带 Conan 的 Bundle 默认使用
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>` 和生成的 `default`/`lxtc-build` profile；
`BUNDLE_DIGEST` 是 Bundle ID 的 SHA-256 摘要前 16 个十六进制字符；
`--conan-cppstd` 只覆盖目标 profile；省略时，profile 会写入 Conan 2 针对该托管
编译器家族及主版本所建模的默认值；
`--conan-build-profile` 显式替换生成的 build context，但不能反过来指向生成的
`lxtc-build` selector 自身。Conan home 与安装 prefix 不能互相包含。每个 bundle 都
必须安装到不存在或为空的 `PREFIX`；新 bundle 使用新的 prefix。

安装成功时 stdout 只输出 launcher 路径。stderr 会输出当前 shell 命令，以及直接追加
到 `~/.bashrc` 和 `~/.zshrc` 的命令。launcher 加载已安装 binding 的 shell 环境，
在选择 Conan 时导出相应环境，原样执行使用方命令数组并返回其退出状态。

无需运行使用方命令即可查询已安装 Bundle：

```bash
lxtc info
lxtc runtime show
lxtc runtime set libstdc++|libc++
lxtc runtime reset
lxtc --runtime libstdc++|libc++ COMMAND [ARG ...]
lxtc run EXECUTABLE [ARG ...]
lxtc shell
lxtc conan-init
```

`info` 以稳定的 `key=value` 格式输出 Bundle 与 variant ID、安装 prefix、编译器、
target triplet 与 sysroot、libc 下限、C++ runtime 的默认值、可用值和当前选择、
integration、CMake toolchain、build-tools 架构与 host floor、每个随包工具的版本、
路径、linkage 和默认启用状态，以及当前 Conan home/profile 选择。
`cxx_runtime.kind` 保留安装时默认值，`cxx_runtime.selected` 表示实际生效的选择。

`runtime show` 输出 launcher 的持久选择。managed Clang Bundle 同时发布
`libstdc++` 和 `libc++` 时，`runtime set` 保存新选择，`runtime reset` 恢复安装默认值。
状态文件位于
`${XDG_CONFIG_HOME:-$HOME/.config}/linux-toolchain/<BUNDLE_DIGEST>/runtime`，
属于 launcher，不属于 Conan home 或安装 prefix。`--runtime` 只为给定命令及其子进程
临时选择 runtime。优先级依次为命令选项、继承的
`LINUX_TOOLCHAIN_CXX_RUNTIME`、持久选择、安装默认值。C++ wrapper 在 compiler driver
层应用实际选择，不依赖 CMake 或 Conan 的 flags 初始化。不经 launcher 且未设置该环境
变量而直接调用 `binding/bin/c++` 时，使用安装默认值。使用方编译器参数保持原样，并
继续按 driver 的正常顺序生效。

`run` 使用 SDK 动态 loader 执行动态链接的 target ELF。可执行文件既可以带路径，
也可以通过 launcher 的 `PATH` 查找。loader 首先搜索实际选择的 C++ runtime 目录和
SDK libc 目录，然后搜索可执行文件所在目录。执行前，同一个 SDK loader 会在
禁用 cache 的情况下列出递归 closure；存在未解析条目、来自未选择 managed runtime
的条目，或从系统 `/lib*`、`/usr/lib*`、`/usr/local/lib*` 目录解析出的库时，命令
失败。可执行文件通过正常 dynamic tag 引用的其他应用自有目录仍可使用。命令会移除
`LD_AUDIT`、`LD_PRELOAD` 和继承的 `LD_LIBRARY_PATH`。程序最终替换 launcher
进程，因此退出状态和 signal 保持不变。

AArch64 SDK 的 glibc 早于 2.36 时，`run` 会进入 user/mount namespace，把 SDK loader
bind 到 target interpreter 路径后再启动程序；该兼容路径要求主机提供 `unshare`、
`mount` 和 POSIX shell。`run` 只用于本地执行与验证，不定义部署布局，也不用于运行
脚本或静态可执行文件。

`shell` 使用实际 launcher 环境启动交互式子 shell。它选择
`${SHELL:-/bin/sh}`，支持 Bash、Zsh，以及常见 POSIX shell 名称 `sh`、`dash`、
`ash`、`ksh`、`mksh`。launcher 在 stderr 输出编译器、target、libc 下限和实际
C++ runtime。读取用户正常的启动文件后，它会重新应用 binding 环境，并让
`binding/bin`、随包 build tools 和安装 launcher 目录排在最终 `PATH` 前面。
对于常规提示符，它把第一个 `:` 之前的前缀替换为 LXTC 标签，保留工作目录和 VCS
后缀。Clang 标签是 `(lxtc clang-VERSION RUNTIME)`，GCC 标签是
`(lxtc gcc-VERSION)`；compiler-only binding 显示 `runtime: none`，标签中不含
runtime。该命令不会编辑启动文件，也不会改变父 shell。读取用户启动文件后，初始化
脚本只把当前 C++ runtime 的共享库目录放到该子 shell 的 `LD_LIBRARY_PATH` 前面，
并在其后保留用户原有值。因此直接执行的程序能够找到所选 runtime，但仍使用主机
loader 和 libc；需要 SDK libc closure 时应使用 `lxtc run`。

子 shell 会导出创建时选择的 runtime。之后执行 `runtime set` 会更新持久默认值，
但不能改变该 shell；除非显式使用 `--runtime`，其子进程仍继承这个 shell 快照。
可用 `exec lxtc --runtime libc++ shell`（或 `libstdc++`）把它替换为另一个
runtime 快照；`exit` 返回父 shell。安装时使用 `--launcher-name` 时，应把示例中的
`lxtc` 换成所选名称。

Bundle 带 Conan 时，生成的 target 和原生 build profile 都随实际 runtime 切换。
显式 `--conan-build-profile` 始终由使用方管理，不会被 runtime 选择重映射。
单 runtime Bundle 支持 `runtime show`，但拒绝切换。

`conan-init` 会在安装记录的 Conan home 中重新创建缺失的静态 settings 和 profile，
但不运行 Conan，也不恢复 package cache。已有相同文件可直接复用；已有不同文件时会
报错且不覆盖。成功时 stdout 输出 Conan home 路径。Bundle 不含 Conan integration 时
不提供该命令。runtime 选择不属于该命令。`conan-init`、`runtime set` 和
`runtime reset` 会通过原子文件替换刷新 `lxtc.info`；其内容对应持久选择下普通
`lxtc info` 的输出，命令级或继承的覆盖不会改写该快照。Conan home 后缀是 Bundle ID
的单向摘要，应读取 `lxtc.info`，而不是尝试从目录名解码配置。

这些命令只依赖已安装的 Bundle。使用方程序名恰好为 `info`、`runtime`、`run`、
`shell` 或 `conan-init` 时，可使用 `lxtc -- COMMAND ...`。

## 环境诊断

```bash
linux-toolchain doctor --workflow sdk
linux-toolchain doctor --workflow managed
linux-toolchain doctor --workflow external
linux-toolchain doctor --workflow consumer
linux-toolchain doctor --workflow consumer --integration shell
linux-toolchain doctor --workflow consumer --integration conan
linux-toolchain doctor --workflow managed --summary
linux-toolchain doctor --workflow all --json
```

consumer 默认检查 CMake 的前置依赖；重复 `--integration` 可以检查 `cmake`、`shell`
或 `conan` 所需的可执行工具。诊断不会构建使用方项目，也不能认证发布组合。Docker 只对
SDK/managed 生产是必需工具。managed GCC 和 LLVM 源码获取使用经过校验的发行源码包，
不要求 host Git。

`--summary` 在全部必需检查通过时只输出 `==> doctor: PASS`；任何必需检查失败时仍输出完整
报告，便于直接定位问题。不指定 `--summary` 时，人类可读输出仍保持详细模式。

## SDK 命令

- `sdk list` 列出固定的 glibc recipe 和架构支持。
- `sdk create` 一次完成 SDK 的解析、渲染、构建和导出。
- `sdk render` 只生成可评审 workspace，不启动 Docker。
- `sdk build` 构建已渲染 workspace 并导出 SDK。

`amd64` 和 `arm64` 是 CLI alias，写入 manifest 时分别规范为 `x86_64` 和
`aarch64`。公共 SDK 位于 `WORKSPACE/sdk`；同级 `toolchain/` 只是生成端状态。
`sdk list --json` 输出 `linux-toolchain-sdk-catalog` format 1，catalog 条目位于
`recipes` 数组中；每个条目都使用精确的 `crosstool-ng` 字段记录 backend 版本。

## 导入运行时层

- `runtime import-gcc` 过滤并验证 GCC target runtime prefix。它要求 target glibc
  floor、架构和 license evidence；外部构建的 prefix 还要通过 `--probe-gxx` 证明。
- `runtime import-llvm` 过滤 libc++、libc++abi、libunwind 和 compiler-rt。它要求
  LLVM 版本、target triplet、架构、glibc floor，以及 managed `--provenance` 或外部
  `--probe-clang`。

两条命令都发布不含 compiler executable 的可迁移 runtime。LLVM 始终同时发布并验证
共享库和静态库；外部 prefix 提供 `libc++experimental.a` 时会保留并验证它，托管
LLVM runtime 则必须包含该归档。导入和 binding 都不会自动启用或链接 experimental
归档。binding 创建会执行最终的动态与静态 compiler/runtime link probe。

## 创建绑定

- `bind external` 绑定主机管理的 GCC 或 Clang，必须提供 `--runtime`，或明确选择仅供
  开发的 `--allow-unpinned-runtime`。
- `bind managed` 绑定已生成的 managed 制品，lock variant 决定默认及可用 C++ runtime；
  `--runtime` 路径指向对应 runtime-set publication。

两条命令都可重复使用 `--integration cmake|shell|conan`。省略时生成 CMake 和 shell，
Conan 为可选 adapter。`--conan-cppstd`、`--conan-libcxx` 和
`--conan-build-type` 只配置 Conan host profile，并且仅在选择 Conan 时有效；这些选项
不会改变直接 wrapper、CMake 或 shell 的编译参数。不指定 `--conan-cppstd` 时，profile
会写入 Conan 2 针对所绑定编译器家族及主版本建模的默认值。绑定命令不选择 Conan
build-context profile；通用 binding 可能是
交叉目标，托管原生 build profile 只由完整 Bundle 组装。

binding 的 `binding.json` 使用 `linux-toolchain-binding` schema 和 format 1。其中 C++
runtime 由 `cxx_runtimes` 记录默认选择和全部可用条目，`integrations` 只记录已生成的
adapter。使用方 build type 和 Conan 术语不属于 binding 格式。

## 托管构建命令

```bash
linux-toolchain managed catalog [--json]
linux-toolchain managed lock --spec FILE --output FILE
linux-toolchain managed artifacts --lock FILE [--json]
linux-toolchain managed assemble \
  --lock FILE --variant ID --sdk-workspace SDK_WORKSPACE \
  --compiler-backend-workspace COMPILER_BACKEND_WORKSPACE \
  --workspace DIRECTORY --output BINDING
```

`assemble` 从 variant 推导 Compiler Kit 和 runtime IDs，匹配的制品经验证后才复用；
中断后可以重新执行同一条命令。所选 artifact、源码、SDK、compiler backend 或 target
tools 输入变化时需要新的 workspace。`--rebuild` 重建匹配且
由生成器管理的制品 workspace；`--force` 单独允许替换 `--output` 指向且由生成器管理的
binding。可重复使用的 `--integration` 与绑定命令一样，默认生成 CMake 和 shell。
选择 Conan 时可使用 `--conan-cppstd`、`--conan-build-type`，必要时还可指定
`--conan-libcxx libstdc++|libstdc++11|libc++`；默认值由 runtime set 决定，只接受其中
可用的选择。

`managed render`、`fetch`、`build` 和 `publish-runtime` 是用于拆分执行的底层命令。
`managed render` 需要显式传入 SDK、以该 SDK workspace 的 `toolchain/bin` 作为
`--target-tools`，并传入 `--compiler-backend-workspace`。
`managed fetch` 只是可选预取；`managed build` 会自行验证已有源码或获取缺少的源码。
`build --jobs` 是执行选项，输入匹配的续建之间可以调整。`publish-runtime` 接收
`--artifact-dir` 中的 raw build output。完整示例见[托管编译器](managed-compilers.md)。

`managed catalog --json` 输出 `linux-toolchain-managed-release-index` format 1，条目位于
`releases` 数组中。`managed artifacts --json` 输出
`linux-toolchain-managed-lock-artifacts` format 1，包含 `compiler_kits`、`runtimes` 和
`variants` 数组。`managed assemble --json` 输出
`linux-toolchain-managed-assembly` format 1。

## 验证项目和部署结果

```bash
linux-toolchain verify --binding BINDING --integration cmake|shell|conan \
  --build-dir DIRECTORY
linux-toolchain audit --policy POLICY [--recursive] PATH...
```

- `verify` 构建随包提供的 C++/ASM 接入项目，检查输出、动态加载依赖闭包和程序运行。
  可选接入方式为 `cmake`、`shell` 和 `conan`；`--build-type` 属于本次使用方构建，默认
  为 `Release`，不属于 binding。原生执行 glibc 早于 2.36 的 AArch64 产物时，会使用
  非特权 user 和 mount namespace，让内核通过声明的 interpreter 进入 SDK loader，且不
  修改 host 文件系统。shell 模式使用随包提供的 Make 构建。成功时写出
  `result.json`，其 schema 为 `linux-toolchain-smoke-result`，format 为 1。
  详细命令输出保留在 build 目录，stdout 只输出 `result.json` 路径；失败输出会回放到
  stderr。
- `audit` 按 binding 的 ELF 策略检查一个或多个文件或递归部署树；`audit --json` 输出
  `linux-toolchain-elf-audit-report` format 1。
- `conan settings` 写出生成 Conan host profile 所需的 settings 扩展。

正式发布还要覆盖完整的项目依赖和部署树，并在接近目标的环境中运行。
