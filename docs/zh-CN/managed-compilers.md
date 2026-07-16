# 托管编译器

[English](../managed-compilers.md) | [简体中文](managed-compilers.md)

托管模式从身份明确的上游源码构建编译器和目标运行时。一套完整工具链包括：

1. 提供目标 ABI 下限的 glibc SDK；
2. 对应精确编译器和目标架构的 Compiler Kit；
3. 目标运行时层；
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
- Clang：显式选择同 release LLVM libc++，或一个精确 GCC runtime；
- Compiler Kit host glibc floor 与 target glibc floor 相互独立。

catalog 中存在某个组合，只表示它已建模并固定。发布认证仍需真实构建、target-like
执行和使用方证据。

## 设置托管工具链

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --integration conan \
  --work-dir /var/tmp/linux-toolchain/gcc12-glibc219 \
  --store-dir /var/tmp/linux-toolchain/store \
  --prefix /opt/linux-toolchain/gcc12-glibc219
```

托管 setup 在 x86-64 或 AArch64 上原生运行，target 默认取 producer 架构；传入不同的
`--arch` 会直接报错。GCC 自动选择同 release runtime；Clang 必须指定 `--runtime
libc++` 或 `--runtime gcc@VERSION`。

`--host-glibc-floor` 选择独立的 Compiler Kit host 策略。高层 setup 未指定该选项时，
会把它解析为 target `--glibc` 的值。解析后的 floor 递归约束 managed GCC/Clang 中每个
host ELF，包括辅助程序和随包 library；Compiler Kit 中的 binutils 必须是没有 glibc
依赖的静态 host ELF。

主要 integration 默认为 shell，可选择 `cmake`、`shell` 或 `conan`，用于选择生成端
smoke 路径。高层 setup 会同时生成 CMake、shell 和 Conan adapter；仅渲染或安装这些
静态文件不要求 Conan。选择 Conan primary smoke 时，仍会在本机准备态中记录生成端
Conan home 和原生 build profile。
原生验证 glibc 早于 2.36 的 AArch64 producer smoke 时，需要启用非特权 user
namespace，并提供 host `unshare` 和 `mount` 工具。

三个生成端路径各有独立职责：

- `--work-dir` 保存一套不可变 selection 及其验证准备态；
- `--store-dir` 保存共享的内容寻址 SDK、已验证源码、managed 编译树和日志；
- `--prefix` 是最终自包含安装目录。

`--jobs` 只控制执行并行度，不属于 SDK 或 managed artifact 的缓存身份。多套 selection
可以复用 store 中输入相同的内容。只修改 `--jobs` 时可以继续使用同一个 work directory，
并保留匹配的准备态和生成端输出。高层 `--force` 只修复或替换选择相同且由生成器管理的
selection 输出；已经验证通过的不可变 producer artifact 会继续复用，不会故意重编。

builder image 的复用与这些文件系统路径相互独立。SDK 和 managed builder 会先在当前
Docker daemon 中检查 builder 身份完全匹配的 image；删除 work
directory 或 `out/` 不会删除该 image。两个角色对应同一个随包 Dockerfile 中的不同
target：managed target 在一个共享层中安装完整的生成端依赖，crosstool-NG target
再在该层上安装经过验证的 crosstool-NG release。默认使用 Ubuntu 普通软件源；把
`LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT` 设为 `20260701T000000Z` 这类时间戳，或给 Make
传入同值的 `UBUNTU_SNAPSHOT`，会让两个 target 同时切换到对应 Ubuntu snapshot。
builder 身份包含这个选择，实际解析出的不可变 image ID 会记录在 provenance 中。
普通源避开了较慢的 snapshot 服务，但 daemon cache 丢失后可能解析到更新的软件包；
需要软件包级重复性时再显式选择 snapshot。

共享软件包层一旦存在，SDK 构建后创建 managed image 会直接复用它，不再更新软件源或
重复安装软件包。切换 Docker context、清理 daemon image/cache 或使用临时 daemon
会越过这条复用边界。producer store 不会隐式导出或导入 Docker image。

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
和全部合法 variant，不包含时间和本机路径。构建脚本应使用 lock 输出的 ID，不要自行
拼接。

## 构建一套可用工具链

managed setup 会针对原生 producer 架构和所选 Compiler Kit host floor 准备一份完整的
GCC 9.5 compiler backend workspace，供 managed GCC 和 Clang 共同复用。普通底层
`sdk create` 只构建与 compiler 无关的 SDK，以及 workspace 中归属独立的 target
binutils，不会生成 compiler backend。target SDK 与 backend 的架构和 glibc floor 相同
时，setup 只构建一次完整 workspace，并同时把它作为 target SDK 输入；否则两份原生
workspace 分开保存在内容寻址 producer store 中。

`managed assemble` 构建并验证缺少的 Compiler Kit 和 runtime，发布 runtime，再创建
binding。只有 lock 中对应 artifact selection、manifest 和生成输入全部匹配时才复用
已有制品。同一 compiler family 的 Compiler Kit 与 runtime 来自一次共享 compiler
build。构建中断后重新执行同一条命令；匹配的生成工作经验证后会被复用。

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
静态库。完整输出及其最终位置通过验证后才会保留，失败时回滚。稳定替换由 managed
lease/state lock 流程协调；任意外部 filesystem reader 不具备无锁热替换保证。

managed binding 必须与 lock variant、SDK、Compiler Kit 和已发布 runtime 一致。目标、
ABI floor、GCC compiler/runtime release、Clang/LLVM runtime release 或 runtime family
不一致都会失败。Clang 配合 GCC runtime 时使用 libstdc++/libgcc；配合 LLVM runtime
时使用 libc++/compiler-rt/libunwind，并拒绝 GCC runtime 依赖。
绑定了 runtime 的 binding 还会用所选 SDK 和 runtime overlay 验证全静态 C/C++ 链接。

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

Bundle 创建会验证准备态，复用其中验证过的 binding 作为模板，并把所选可迁移制品树
直接写入安装器。已有匹配安装时也可使用 `--prefix`。`bundle create-artifacts` 是面向
独立组装 SDK、Compiler Kit、runtime 和 lock 的高级入口。只有准备态中的 format-1
passed smoke 结果仍与所记录 binding 和所选 integration 匹配时，该准备态才保持合格。

安装时不需要 Python、Docker、Conan、CMake 或 Make：

```bash
./linux-toolchain-VARIANT_ID.run \
  --prefix /opt/linux-toolchain/VARIANT_ID \
  --launcher-name gcc12
/opt/linux-toolchain/VARIANT_ID/bin/gcc12 make release
```

安装 prefix 必须不存在或为空。launcher 默认名为 `lxtc`，可在安装时通过
`--launcher-name` 修改。完整 Bundle 还接受 `--conan-home PATH`、
`--conan-cppstd VALUE` 和 `--conan-build-profile NAME_OR_PATH`。home 默认为
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`，其中 `BUNDLE_DIGEST` 是 Bundle ID 的
SHA-256 摘要前 16 个十六进制字符；安装器会在其中同时写入目标 `default` 和托管原生
`lxtc-build` profile。build-profile 名称覆盖在该专属 home 中解析，可稍后创建。不指定
`--conan-cppstd` 时，生成的 profile 会写入 Conan 2 针对该托管编译器家族及主版本
建模的默认值。安装只写静态配置，不调用任何这些使用方工具。

## 发布认证

单元测试只覆盖解析和确定性状态转换，不能认证某个 compiler 组合。每个发布
组合都必须真实构建 SDK、Compiler Kit 和 runtime，执行 binding smoke，递归验证 ELF
和 loader closure，并在声明的最低 host/target 环境运行代表性使用方。JVM/JNI 等嵌入
场景还需由真实宿主进程完成加载测试。
