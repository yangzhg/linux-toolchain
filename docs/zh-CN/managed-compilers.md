# 托管编译器

[English](../managed-compilers.md) | [简体中文](managed-compilers.md)

托管模式从身份明确的上游源码构建编译器和目标运行时。一套完整工具链包括：

1. 提供目标 ABI 下限的 glibc SDK；
2. 对应精确编译器和目标架构的 Compiler Kit；
3. 一个或多个目标运行时层；
4. 验证并连接这些制品的 binding。

用于生成这些制品的固定 compiler backend 不属于 Compiler Kit。目标工具必须作为显式
输入提供，不能从生成端主机的 `PATH` 发现。

## 当前支持范围

- producer platform 与 Compiler Kit host：原生 `linux/amd64`/x86-64 和
  `linux/arm64`/AArch64；
- target：x86-64 和 AArch64，且必须与 producer 架构一致；
- AArch64 上选择 managed GCC 或 GCC runtime 时要求 GCC 10 或更新版本；
- GCC：由同一精确 GCC release 提供 libstdc++ 和 libgcc；x86-64 托管生成还会把
  libquadmath 的公开头文件及静态/共享库装入 runtime overlay，AArch64 则禁用这个
  不受目标支持的组件；
- Clang：同时携带同 release LLVM libc++ 和一个精确 GCC libstdc++/libgcc runtime，
  GCC provider 默认为 `gcc@12`；
- Compiler Kit host glibc floor 与 target glibc floor 相互独立。

catalog 中存在某个组合，只表示它已建模并固定。发布认证仍需真实构建、target-like
执行和使用方证据。

## 设置托管工具链

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --cmake-version 3.31.12 \
  --integration conan \
  --work-dir /var/tmp/linux-toolchain/gcc12-glibc219 \
  --store-dir /var/tmp/linux-toolchain/store \
  --prefix /opt/linux-toolchain/gcc12-glibc219
```

托管 setup 在 x86-64 或 AArch64 上原生运行，target 默认取 producer 架构；传入不同的
`--arch` 会直接报错。GCC 自动选择同 release runtime。Clang 同时包含两套受支持的
C++ runtime；`--libstdcxx gcc@VERSION` 用于选择 libstdc++ provider，默认是
`gcc@12`。

`--host-glibc-floor` 选择独立的 Compiler Kit host 策略。高层 setup 未指定该选项时，
会把它解析为 target `--glibc` 的值。解析后的 floor 递归约束 managed GCC/Clang 中每个
host ELF，包括辅助程序和随包 library；Compiler Kit 中的 binutils 必须是没有 glibc
依赖的静态 host ELF。

补充性 build-tools artifact 使用同一解析后的 host floor 和原生架构。CMake 默认版本
是 3.31.12；`--cmake-version 3.31.10|3.31.11|3.31.12` 可以选择其他已固定 release。
CMake、CTest、CPack、GNU Make 和 Ninja 按该 host floor 构建，并静态链接 C++ 依赖。
ccache 使用匹配架构的 static-musl release，在使用方显式启用之前不会作为 compiler
launcher 生效。

主要 integration 默认为 shell，可选择 `cmake`、`shell` 或 `conan`，用于选择生成端
验证路径。高层 setup 会同时生成 CMake、shell 和 Conan adapter；仅渲染或安装这些
静态文件不要求 Conan。选择 Conan 作为主要验证路径时，仍会在本机准备态中记录生成端
Conan home 和原生 build profile。
Conan host profile 的设置属于静态 adapter 配置，可与任意主要验证路径搭配。只有
Conan 验证会调用 Conan，并接受生成端原生 build profile 选择。
原生验证 glibc 早于 2.36 的 AArch64 producer 时，需要启用非特权 user
namespace，并提供 host `unshare` 和 `mount` 工具。

三个生成端路径各有独立职责：

- `--work-dir` 保存一套不可变 selection 及其验证准备态；
- `--store-dir` 保存共享的内容寻址 SDK、build tools、已验证源码、managed 编译树和
  日志；
- `--prefix` 是最终自包含安装目录。

`--jobs` 只控制执行并行度，不属于 SDK 或 managed artifact 的缓存身份。多套 selection
可以复用 store 中输入相同的内容。只修改 `--jobs` 时可以继续使用同一个 work directory，
并保留匹配的准备态和生成端输出。高层 `--force` 只修复或替换选择相同且由生成器管理的
selection 输出；已经验证通过的不可变 producer artifact 会继续复用，不会故意重编。

builder image 的复用与这些文件系统路径相互独立。SDK、build-tools 和 managed builder
会先在当前 Docker daemon 中检查 builder 身份完全匹配的 image；删除 work directory
或 `out/` 不会删除该 image。这些构建执行同一个随包 Dockerfile target，其中一次性
安装完整的生成端依赖和经过验证的 crosstool-NG release，因此 SDK、build-tools、
compiler 和 runtime 构建不会重复执行 `apt update` 或安装软件包。默认使用 Ubuntu
普通软件源；把
`LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT` 设为 `20260701T000000Z` 这类时间戳，或给 Make
传入同值的 `UBUNTU_SNAPSHOT`，会让共享 image 使用对应 Ubuntu snapshot。
builder 身份包含这个选择，实际解析出的不可变 image ID 会记录在 provenance 中。
普通源避开了较慢的 snapshot 服务，但 daemon cache 丢失后可能解析到更新的软件包；
需要软件包级重复性时再显式选择 snapshot。

Dockerfile 输入、软件包来源选择或平台发生变化时会得到不同的 image 身份；清理 daemon
image/cache 或使用临时 daemon 也会失去复用。producer store 不会隐式导出或导入
Docker image。

安装后的 launcher 不依赖生成端工作目录：

```bash
cd /home/user/workspace/project-a
/opt/linux-toolchain/gcc12-glibc219/bin/lxtc make release
```

## Catalog 和 lock

应检查已安装 catalog，而不是把当前版本列表写死在自动化中：

```bash
linux-toolchain managed catalog
linux-toolchain managed catalog --json
```

selector 可以是精确 release 或无歧义的 major version。解析结果会记录 GCC 或 LLVM
官方发行源码包的精确 URL 和 SHA-512。未知、歧义或未固定的 release 会失败。

严格的 `linux-toolchain-managed-spec` format 1 描述 build platform、Compiler Kit
host、target、compiler 和 runtime 选择。将它解析为确定性 lock，并检查生成的制品图：

```bash
linux-toolchain managed lock \
  --spec examples/managed/compiler-matrix.json \
  --output out/managed.lock.json
linux-toolchain managed artifacts --lock out/managed.lock.json
```

`linux-toolchain-managed-lock` format 1 记录精确源码身份、逻辑 Compiler Kit/runtime ID
和全部合法 variant，不包含时间和本机路径。Compiler Kit 集合既包含各 variant 所选
compiler，也包含每个已锁定 runtime 的 provider compiler，从而由一棵 provider build
tree 同时发布两份制品。构建脚本应使用 lock 输出的 ID，不要自行拼接。

## 构建一套可用工具链

managed setup 会针对原生 producer 架构和所选 Compiler Kit host floor 准备一份完整的
GCC 9.5 compiler backend workspace，供 build-tools artifact、managed GCC 和 Clang
共同复用。普通底层 `sdk create` 只构建与 compiler 无关的 SDK，以及 workspace 中归属
独立的 target tools（binutils 与 Mold），不会生成 compiler backend。target SDK 与
backend 的架构和 glibc floor 相同时，setup 只构建一次完整 workspace，并同时把它作为
target SDK 输入；否则两份原生 workspace 分开保存在内容寻址 producer store 中。

`managed assemble` 构建并验证缺少的 Compiler Kit 和 runtime，发布 runtime set，再
创建 binding。只有 lock 中对应 artifact selection、manifest 和生成输入全部匹配时才
复用已有制品。GCC Compiler Kit 与匹配的 GCC runtime 共用一棵 GCC build tree；
Clang Compiler Kit 与匹配的 LLVM runtime 也共用一棵 LLVM build tree 和一次容器执行。
lock 会包含每个 runtime provider 对应的 Compiler Kit。Clang assembly 发现所选 GCC
provider 的 Kit 和 runtime 都缺失时，会在同一次构建中生成两者，把 Kit 留在 producer
store，只把 runtime 放入 Clang Bundle。之后构建对应 GCC variant 会直接复用两份产物，
不会再次编译 GCC。runtime 组件和 build tree 在不同 variant 间按内容寻址。

managed GCC 和 LLVM 产物使用 release 优化，发布的 compiler 与 runtime 不保留构建树
DWARF。这不会改变使用方的调试参数；应用仍可通过 `-g` 生成自己的调试信息。构建中断后
重新执行同一条命令，匹配的生成工作经验证后会被复用。

## 底层构建命令

只有在源码获取、构建、发布和 binding 需要拆分执行或单独审核时，才使用底层命令：

- `managed render`：在本机 workspace 记录 lock artifact、SDK、该 SDK workspace 的
  target tools、固定 compiler backend 和 builder 输入；
- `managed fetch`：可选地预取并验证所选源码；
- `managed build`：验证或获取缺少的源码，准备 builder image 并执行 compiler build；
- `managed publish-runtime`：把 raw runtime build 转换为验证后的 GCC/LLVM runtime；
- `bind managed`：验证完整组合并生成所选使用方 integration。

准确参数见 [CLI 参考](cli-reference.md#托管构建命令)。raw runtime build 只有在
`managed publish-runtime` 成功后才能作为 binding 输入。

`managed build` 不要求先执行 `managed fetch`，会自行获取并验证缺少的源码包。GCC 和
LLVM 共用按内容寻址的下载与 SHA-512 校验链路；managed 源码获取不依赖 host Git。

构建并行度由 `managed build --jobs` 控制，输入匹配的续建之间可以调整。jobs 不改变
内容寻址的 producer identity。

## 发布和 binding 校验

Compiler Kit 发布会递归校验每个 host ELF 的架构和 glibc needs、声明的 binutils 是否
静态且没有动态依赖，并校验 driver target、vendored DSO、license 和 manifest。runtime
发布校验 target 与 ABI floor、ELF 和 archive、动态依赖闭包、symlink、路径、license
和源码证据。LLVM 发布始终同时包含并验证 libc++、libc++abi、libunwind 的共享库与
静态库；所有受支持 LLVM release 的托管 libc++ 发布还必须包含
`libc++experimental.a`。它是可供使用方显式选择的 library，不是 binding 默认值；
不会为使用方注入启用宏或链接选项。完整输出及其最终位置通过验证后才会保留，失败时
回滚。稳定替换由 managed lease/state lock 流程协调；任意外部 filesystem reader
不具备无锁热替换保证。

managed binding 必须与 lock variant、SDK、Compiler Kit 和已发布 runtime set 一致。
目标、ABI floor、GCC compiler/runtime release 或 Clang/LLVM runtime release 不一致
都会失败。GCC binding 使用匹配的 libstdc++/libgcc。Clang binding 通过原生的
`-stdlib`、`--rtlib` 和 `--unwindlib` 同时提供 libstdc++/libgcc 与
libc++/compiler-rt/libunwind。双 runtime binding 的安装默认值是 libstdc++；经 `lxtc`
启动命令时，C++ wrapper 在 driver 层应用实际 runtime 选择，因此使用方覆盖 CMake
flags 也不会丢失该选择。未设置 `LINUX_TOOLCHAIN_CXX_RUNTIME` 而直接调用 wrapper 时，
使用安装默认值。使用方参数仍原样传递，后出现的显式 runtime 选项继续遵循 Clang 的
正常优先级。重复传入当前 `-stdlib` 仍由 driver 正常处理，不会产生
unused-argument warning。每个选项仍保持原生作用范围：只传 `-stdlib` 不会同时改变
compiler runtime 或 unwind library。
发布验证会针对每套 C++ runtime 执行动态、共享库和全静态 C++ 链接。

managed LLVM runtime 使用 Clang 按 Linux 架构组织的 resource 布局，因此
compiler-rt 查找不依赖 target 的 vendor 拼写。Clang binding 默认使用所记录的
target，Compiler Kit 在没有 target 选项时必须报告这个 target。命令行、Clang 配置
文件和 response file 中的显式 target 仍属于使用方输入，由 Clang 自己解释。
只改变等价 triple 字段且仍兼容 Linux ABI 的 target 写法会接受验证；架构、操作系统
或 ABI 不兼容时会自然失败。managed GCC 继续使用构建时写入目标专用 driver 的固定
target，也不接受 Clang 的 `--target` 选项。

所有 managed Compiler Kit 都发布 BFD、Gold 和 Mold 2.41.0；Clang Compiler Kit 还
发布 LLD，默认 linker 仍是 BFD。binding 会通过 driver 验证每个已发布 linker。Clang
和 GCC 12 及以上版本使用 `-fuse-ld=mold`；GCC 10 和 11 的 binding 提供固定使用
`-B` 选择 Mold 的 `cc-mold` 与 `c++-mold`，无需改写使用方参数。

## 发布单文件 Bundle

setup 准备态可以直接打包，不必先发布安装 prefix：

```bash
python3 -m pip install .
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --work-dir out/work/gcc12-glibc219 \
  --store-dir out/store \
  --prepare-only
linux-toolchain bundle create \
  --config out/work/gcc12-glibc219/setup.json \
  --state-directory out/work/gcc12-glibc219/state \
  --output out/linux-toolchain-VARIANT_ID.run
```

Bundle 创建会验证准备态，复用其中验证过的 binding 作为模板，并把所选可迁移制品树与
build tools 直接写入安装器。已有匹配安装时也可使用 `--prefix`。
`bundle create-artifacts` 是面向独立组装 SDK、build-tools、Compiler Kit、runtime 和
lock 的高级入口。只有准备态中的验证结果为 format 1、状态为 `passed`，且仍与所记录
binding 和所选 integration 匹配时，该准备态才保持合格。

安装时不需要主机预装 Python、Docker、Conan、CMake、Make、Ninja 或 ccache：

```bash
./linux-toolchain-VARIANT_ID.run \
  --prefix /opt/linux-toolchain/VARIANT_ID \
  --launcher-name gcc12
/opt/linux-toolchain/VARIANT_ID/bin/gcc12 make release
```

安装 prefix 必须不存在或为空。launcher 默认名为 `lxtc`，可在安装时通过
`--launcher-name` 修改。不指定 `--prefix` 时，安装器使用 Bundle 内置的选择名，
安装到 `$HOME/.local/lib/linux-toolchain/` 下；重命名 `.run` 文件不会改变该名称。
完整 Bundle 还接受 `--conan-home PATH`、
`--conan-cppstd VALUE` 和 `--conan-build-profile NAME_OR_PATH`。home 默认为
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`，其中 `BUNDLE_DIGEST` 是 Bundle ID 的
SHA-256 摘要前 16 个十六进制字符；安装器会在其中同时写入目标 `default` 和托管原生
`lxtc-build` profile。build-profile 名称覆盖在该专属 home 中解析，可稍后创建。不指定
`--conan-cppstd` 时，生成的 profile 会写入 Conan 2 针对该托管编译器家族及主版本
建模的默认值。安装只写静态配置，不调用任何这些使用方工具。

Clang Bundle 同时包含两种 C++ runtime 时，可用 `lxtc runtime set libc++` 持久选择，
用 `lxtc runtime reset` 恢复安装默认值，或用
`lxtc --runtime libstdc++ COMMAND` 只覆盖一条命令。`lxtc info` 会同时报告安装默认值和
当前命令实际选择。`lxtc conan-init` 只重新创建缺失的 Conan 静态配置。默认 Conan
host/build context 都随 launcher 选择切换；安装时显式指定的 Conan build profile
不会随之变化。`conan-init`、`runtime set` 和 `runtime reset` 都会刷新 Conan home 中
的 `lxtc.info` 快照。

## 发布认证

单元测试只覆盖解析和确定性状态转换，不能认证某个 compiler 组合。每个发布组合都必须
在声明的架构上真实构建 SDK、build-tools、Compiler Kit 和 runtime，执行 binding
验证，递归验证 ELF 和 loader closure，并在声明的最低 host/target 环境运行代表性
使用方。JVM/JNI 等嵌入场景还需由真实宿主进程完成加载测试。
