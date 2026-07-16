# 架构

[English](../architecture.md) | [简体中文](architecture.md)

`linux-toolchain` 为明确的 glibc ABI 下限生成受控的 Linux C/C++ 构建输入。它不管理
使用方仓库，也不替换项目自身的构建系统。

产品包含四层制品：

1. glibc SDK；
2. 托管模式下的 Compiler Kit（external 模式则引用机器本地的 compiler
   installation）；
3. 一个或多个目标运行时层；
4. 连接所选输入并生成使用方 integration 的 binding。

sysroot 只控制 libc 相关输入，不能单独固定 libstdc++、libgcc、libc++、compiler-rt、
compiler headers 或 compiler CRT objects。

## 运行模式

```text
external mode

  glibc SDK -----+---- optional runtime overlay ----+---- external compiler
                 +--------------- binding ----------+

managed mode

  pinned catalog ----> deterministic lock
                              |
                 Compiler Kit + runtime set
                              |
                        SDK -> binding
```

external mode 检查当前机器安装的 compiler driver 和 target tools。binding 记录绝对
可执行文件路径，因此只适用于当前机器。

managed mode 从固定上游源码构建精确的 compiler/runtime 组合。target tools 来自所选
SDK workspace，不从 host `PATH` 发现。managed mode 增加 compiler 生产和来源边界，
但不替代本机管理 compiler 时使用的 external mode。

高层 `setup` 通过三个相互独立的路径编排 managed mode：

- work directory 保存一套不可变 selection、binding 和验证状态；
- 共享 producer store 保存可复用的生成输入和构建输出；
- 可选最终 prefix 只保存自包含的已安装工具链。

`--jobs` 只是执行选项，不会定义另一套 producer artifact。`--prepare-only` 在生成端
验证完成后停止，使 Bundle 无需先发布安装 prefix。使用方项目无需创建或提交 setup
配置。输入身份相同且验证通过的内容寻址 producer artifact 会被复用。高层
`--force` 只修复或替换选择相同且由生成器管理的 selection 输出，不会故意重编已经
验证通过的不可变 producer artifact。

高层 setup 支持原生 x86-64 和 AArch64 producer host。target 默认取 host 架构并且
必须与它一致；该流程不支持 managed 交叉生成。

四层 compiler artifact 仍与 managed setup 生成的补充性 host build-tools artifact
保持独立。该 artifact 与 producer 使用相同的原生架构，并独立保存和验证。

## 可迁移性和复用

SDK、Compiler Kit 和 runtime manifest 使用相对 payload 路径，可以迁移。binding
记录 compiler 和 target tools 的绝对路径；移动制品或更换机器后必须重新生成。

安装后的 launcher 加载同一 prefix 中的 binding，不依赖 Python、管理 CLI、producer
work directory 或使用方仓库布局。

managed setup 和从准备态创建 Bundle 的流程在读取 SDK、build-tools 与 managed
artifact identity 期间持有共享 producer lease；同一 identity 的 writer 使用独占
lease。因此这些受管流程看到的是之前的稳定目录或验证后的替换目录，但这不表示任意
外部 filesystem reader 都能获得无锁热替换可见性保证。

## 信任和校验边界

校验集中在数据或文件跨越所有权边界的位置。CLI 和环境输入、公共 JSON、下载归档、
外部 compiler、host 能力、用户指定路径以及从磁盘重新加载的 artifact tree，都不能仅
凭名称符合预期就被接受。项目会根据边界检查严格 schema、固定源码摘要、路径包含关系、
symlink 安全、生成器所有权、selection/provenance identity、ELF 架构与 glibc needs、
动态路径闭包，以及真实 compiler/linker 行为。

catalog 常量、由其确定性派生的值，以及已通过所属领域校验的类型化不可变对象，在同一次
操作中可以信任。最终路径发布校验器会把已验证对象返回调用方；立即重新加载并证明同一
目录不会增加可信度。producer 复用则不同：reader 先推导要加锁的 identity，取得 lease
后再在锁内校验 selection 和 artifact。锁前与锁后的两次 identity 检查分别确保加锁对象
正确，并关闭替换竞态。

源码摘要用于标识下载的固定输入。生成的 SDK、Compiler Kit、runtime、build-tools 和
binding tree 则通过严格 manifest、结构校验、真实 compiler/ELF probe 和原子发布完成
认证，不再增加第二套 tree hash 或逐文件 identity。目录在最终路径校验通过后，后续
Bundle 传输和安装可以信任其 producer 认证记录，同时检查本阶段自己的架构、布局、包含
关系和重定位边界；无需重复完整的 compile、link、loader 和验证矩阵。原地编辑已发布
目录会破坏 immutable-artifact 约定，应通过 producer workflow 重新生成。

license manifest 是清单证据：它证明所需且非空的源码或 package notice 存在，并且记录
的文件列表与目录一致；它不是第二套内容完整性系统。使用方 compiler arguments 仍由
driver 处理，不作为 producer 端安全策略进行过滤。

## glibc SDK

SDK recipe 固定：

- target 架构和 CPU baseline；
- glibc 和 Linux UAPI release；
- minimum Linux kernel 配置；
- crosstool-NG、compiler backend GCC 和 binutils release；
- builder image 和源码身份。

builder 身份包含 digest 固定的 base image、随包 Dockerfile、原生 platform 和
Ubuntu 软件包来源选择。空的 `apt_snapshot` 表示 Ubuntu 普通软件源，时间戳表示对应
snapshot。SDK、build-tools、compiler 和 runtime 构建使用完全相同的 builder image，
生成端软件包和 crosstool-NG 只安装一次；实际解析出的 image ID 作为 provenance 保留。

普通 SDK 生成 workspace 只构建 sysroot 和归属独立的 target tools（binutils 与
Mold），不构建私有 C/C++ compiler。managed setup 还会针对原生 producer 架构和所选 Compiler Kit host
floor 构建一份固定的完整 GCC 9.5 compiler backend，供 managed GCC 和 Clang 共同
复用。target SDK 的架构和 glibc floor 与该 backend 相同时，同一个完整 workspace
同时承担两种角色，只构建一次。producer 会先完成源码下载和 checksum 校验，再启动
禁止联网的 crosstool-NG 编译容器；已验证的归档由 producer store 复用。发布只导出
glibc headers/libraries、Linux UAPI、dynamic loader、startup objects 和 SDK
metadata；target tools、compiler 自有 headers、runtime 和 executable 不属于公共
SDK payload。

Linux UAPI headers 与 minimum kernel 是独立策略。能看到某个声明，不表示旧 target
kernel 已实现对应 syscall。

## 托管选择和 Compiler Kit

managed spec 通过 catalog 把 selector 解析为精确源码身份和确定性 lock。lock 描述
Compiler Kit、runtime 和 variant 的关系，不包含时间或本机路径。GCC variant 使用
同一精确 GCC release 的 runtime。高层 Clang variant 同时包含同 release LLVM
libc++ 和一个精确 GCC libstdc++/libgcc runtime，并以 libstdc++ 为默认选择。

Compiler Kit 包含所选 compiler driver 和声明的 target tools，不包含目标 C++ runtime。
发布时递归校验 kit 内每个 host ELF 的架构和 glibc needs，同时校验 driver target、
target-tool identity、vendored dependency、license 和 provenance。声明的 binutils 与
Mold linker 还必须是静态 host ELF，不能带 dynamic loader、shared-library 或
glibc-version 依赖。所有 managed kit 都声明 BFD、Gold 和 Mold；Clang kit 还声明 LLD。

managed GCC 和 Clang 都使用固定的原生 crosstool-NG compiler backend 作为 C/C++
build compiler。所选 SDK producer workspace 中的私有 target tools 提供 assembler、
linker 等 binary tools，再复制进 Compiler Kit；它们不属于导出的 SDK sysroot。两类
build input 都不从 host toolchain 发现。

## 运行时层

runtime overlay 对应特定 provider、target 架构和 glibc floor，包含 compiler runtime
headers、CRT objects 和 runtime libraries，不包含 compiler executable。

GCC overlay 包含所选 GCC/C++ headers、libgcc、libstdc++ 及相关 runtime 输入。LLVM
overlay 包含 Clang resource inputs、compiler-rt builtins，以及 libc++、libc++abi 和
libunwind 的共享库与静态库。发布会过滤无关 compiler payload，并验证 target ELF、
archive member、SONAME 和 dependency closure、symlink、动态路径及 license。

runtime set 把一个 variant 使用的多个独立验证 overlay 组合起来，manifest 记录默认及
可用的 C++ runtime，每个 overlay 仍是可独立复用的组件。因此 managed Clang binding
可使用 Clang 原生 runtime selector，同时不混合 GCC 与 LLVM payload。

x86-64 上的托管 GCC 生成还会把 libquadmath 的公开头文件及静态/共享库放入 runtime
overlay。GCC 不在 AArch64 target 上提供对应的 GNU `__float128` API，因此 AArch64
托管生成会禁用 libquadmath。

## 托管构建边界

managed compiler 构建记录 lock 中对应 artifact selection、SDK、target tools、
compiler backend、build script 和 builder identity。实际 compiler build 以非 root
用户在原生 `linux/amd64` 或 `linux/arm64` container 中运行，禁用网络并只读挂载生成
输入。Docker 模拟不属于生产路径；daemon platform 必须与所选 producer platform
完全一致。

源码获取和 builder image 准备在隔离的 compiler build 之前完成；源码身份按 managed
catalog 验证。同一 compiler family 的 Compiler Kit 和匹配 runtime 共用 build tree。
Clang 与 LLVM runtime 由一次容器执行生成。每个 runtime provider 也有 lock 固定的
Compiler Kit，因此为 Clang variant 生成 GCC runtime 时，会从同一 build tree 缓存匹配
的 GCC Kit。Clang Bundle 不携带这个 Kit，之后的 GCC variant 可以直接复用。

## 补充性 host build tools

managed setup 会针对原生 producer 架构和解析后的 Compiler Kit host glibc floor
构建一份内容寻址的 host build-tools artifact。其 identity 包含架构、host floor、
所选 CMake 版本、固定源码归档、compiler backend 和 build script；并行 jobs 不属于
该 identity。

CMake、CTest、CPack、GNU Make 和 Ninja 从经过验证的源码构建，使用 managed compiler
生产所用的同一套固定原生 compiler backend。所有 host ELF 都会递归校验所选架构和
glibc floor，包括该架构对应的 glibc interpreter；C++ 依赖静态链接，动态路径必须可
迁移。ccache 使用匹配架构的官方 x86-64 或 AArch64 static-musl release，且必须同时
没有 interpreter 和 `DT_NEEDED`。它会作为可用工具发布，但默认不配置为 compiler
launcher。

该 artifact 用于生成端验证，并复制到每套 managed 安装和 Bundle 的 `tools/`。
安装后的 launcher 依次把 `binding/bin`、`tools/bin` 和继承的 host `PATH` 放入搜索
路径，因此 build tools 不会覆盖 managed compiler 和 target tools。

## Binding 和使用方接入

binding 联合验证 SDK、compiler、runtime、target 架构、ABI floor 和工具选择，生成
C/C++ wrapper、target-tool 直连、ELF audit policy 和所选 integration。compiler
arguments 仍是普通 compiler input，原样传给所选 driver。

项目直接支持 CMake 和 shell/Make；Autotools 与手写 Ninja 可使用生成的 shell 环境；
Conan 2 是可选 adapter。没有对应 adapter 和验证的其他构建系统不宣称原生支持。

底层 binding 命令只生成显式选择的 adapter。高层 setup 同时生成 CMake、shell 和
Conan adapter，其中 primary integration 只选择生成端验证路径，不会缩减安装后的
能力集合。仅携带 Conan adapter 及配置其 host profile 都是静态文件生成过程，不要求
Conan executable；只有 Conan 验证负责生成端 Conan 执行和原生 build-profile 状态。

绑定 runtime 时，binding validation 会对普通输出、共享库和全静态输出执行
compile/link probe，并覆盖每套可用 C++ runtime 和每个发布 linker；同时检查 link
map、ELF policy 和 loader closure。Clang 的 `--target`、`-stdlib`、`--rtlib`、
`--unwindlib` 和 `-fuse-ld` 等选项仍由 driver 解析。验证证明所选构建输入，不证明
kernel feature、CPU 兼容性、第三方 dependency closure 或进程内多个 C++ runtime 的
共存。

## 可迁移 Bundle

Bundle 是一套已验证 SDK、Compiler Kit、runtime、build-tools artifact、lock 和
binding template 的传输封装，不是新的制品层。

`bundle create` 可以读取已安装 prefix，也可以读取验证通过的 setup config 和准备态。
从准备态创建时复用 setup 验证过的 binding 作为模板，把 producer path 替换为 prefix
占位符，并把可迁移制品树直接写入归档；不会重复生成 binding，也不需要中间安装 prefix。
只有准备态记录的验证结果为 format 1、状态为 `passed`，且仍与对应 binding 和所选
integration 匹配时，该准备态才是合格状态。

目标端 shell installer 检查 host 架构和 glibc 要求，在目标目录旁解包，再发布到不
存在或为空的 prefix。Python 和 Docker 只在生成端使用，主机不需要预装 Conan、CMake、
Make、Ninja 或 ccache。binding 带有 Conan 时，安装器默认把严格 settings 以及动态
`default`、
`lxtc-build` profile 写入专属的 `$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`；
`BUNDLE_DIGEST` 是 Bundle ID 的 SHA-256 摘要前 16 个十六进制字符。目标 profile 委托给
安装后的 binding；build profile 只在原生 managed Bundle 层组装，使用同一套受控工具链
及当前选择的 runtime library，默认 target/build context 都随 launcher 的 runtime
选择切换，不把这一假设扩散到底层通用 binding。安装器不会检测 compiler，也不会调用
Conan；显式 build-profile 覆盖只记录为本机状态，launcher 不会重映射它。更换机器或
prefix 时应复制 `.run` 并重新安装，不能直接移动已安装 prefix。最终安装验证会检查
迁移后的 manifest、声明路径和已实例化模板，但不会重复 compile、link、loader 或
target-like 使用方认证。

## 发布验证

catalog 解析和单元测试只能证明输入已经建模并能得到确定的解析结果，不能证明真实
兼容性。每个发布的
compiler、runtime、glibc floor 和架构组合都必须真实构建，执行 binding 验证、ELF
与 loader-closure 审计，并在声明的最低 host/target 环境运行代表性使用方。
