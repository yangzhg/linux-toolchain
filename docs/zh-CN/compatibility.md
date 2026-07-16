# 兼容性边界

[English](../compatibility.md) | [简体中文](compatibility.md)

本文说明生成的绑定和 ELF 检查分别能够确认什么。选择某个版本下限只是兼容策略，
不表示所有满足该版本的机器都已经过测试。

## 两个独立的 glibc 下限

| 下限 | 适用对象 | 目的 |
| --- | --- | --- |
| Compiler Kit 主机下限 | Compiler Kit 和随包 build-tools artifact 中所有链接 glibc 的 host ELF，包括 GCC/Clang、辅助程序、library、binutils、CMake、Make 和 Ninja | 这些程序能运行的最老构建机 |
| 目标下限 | 生成的可执行文件、共享库和目标运行时层 | 允许的最高 `GLIBC_*` 需求 |

主机下限不会变成产品的目标下限，较低的目标下限也不能让编译器在同样老的主机上
运行。主机下限既是构建输入，也是发布门槛：managed compiler 使用针对该 floor 构建的
固定原生 GCC 9.5 backend，发布时再递归检查全部 host ELF。Compiler Kit 中的 binutils
与 Mold 必须静态链接，不能带 dynamic loader、shared-library 或 glibc-version 依赖；
builder container 的 libc 不是 Compiler Kit 兼容策略。

托管流程生成原生 `linux/x86_64` 或 `linux/aarch64` Compiler Kit，target 必须与 host
架构一致。高层 setup 默认让 host floor 跟随 target floor；显式指定 host floor 时，两条
策略仍然相互独立。

随包 build tools 使用同一原生架构。CMake、CTest、CPack、GNU Make 和 Ninja 会递归
校验 Compiler Kit host floor，且 C++ 依赖静态链接。ccache 是匹配架构的 static-musl
executable，因此没有 glibc version requirement、interpreter 或 `DT_NEEDED` closure。
这不表示两个架构可以混用；每个工具仍必须通过所选 x86-64 或 AArch64 ELF 架构校验。

## `glibc-floor` 保证

每个被检查 ELF 对 `GLIBC_*` 的最高数值需求不得超过目标下限。
`GLIBC_PRIVATE` 和未知非数值 token 被拒绝，唯一显式建模的例外是
`GLIBC_ABI_DT_RELR`。计算读取 version needs，而不是 ELF 定义的 version。

SDK 同时提供编译/链接使用的 glibc headers、启动对象、linker scripts、loader 和
libraries，避免使用构建机 libc。架构和 loader 也是策略的一部分：

| Target | ELF | Loader |
| --- | --- | --- |
| x86-64 | x86-64、ELF64、小端 | `/lib64/ld-linux-x86-64.so.2` |
| AArch64 | AArch64、ELF64、小端 | `/lib/ld-linux-aarch64.so.1` |

x32、AArch64 ILP32 和大端 AArch64 不受支持。floor 低于 glibc 2.36 时拒绝
`DT_RELR`/`GLIBC_ABI_DT_RELR`；2.36 及以上允许但不强制。

## 编译器和运行时层分别负责什么

| 策略 | binding 控制的输入 |
| --- | --- |
| `external-unpinned` | 仅 glibc SDK；compiler headers、CRT、C++ runtime 仍在外部 |
| `pinned-gcc-runtime` | GCC builtin/fixed/C++ headers、CRT、libgcc、libstdc++ 和已审计可选组件 |
| `pinned-llvm-runtime` | libc++/Clang resource headers、compiler-rt builtins、libc++abi、libunwind、libc++ |

x86-64 托管 GCC 生成的 `pinned-gcc-runtime` 必须包含 libquadmath headers 及静态/共享
库；AArch64 托管生成不要求这个不受目标支持的组件。底层 external import 只在所选
GCC prefix 已提供 libquadmath 时将其导入。

运行时声明的目标下限不得高于 SDK；LLVM 运行时要求与 SDK 使用相同下限。
外部 GCC 前端和运行时按主版本匹配，托管 GCC 则按完整版本匹配。Clang 使用明确选择的
GCC runtime 提供 libstdc++，Clang 与 GCC 的版本号相互独立；libc++ 则要求托管 Clang
和 LLVM runtime 来自同一个完整 LLVM 版本。生成托管 lock 和创建 binding 时都会检查
这些关系及来源记录。

## 发布运行时层前的检查

GCC runtime export 过滤已安装 target prefix，验证 ELF 架构/version needs、archive、
CRT、SONAME、DT_NEEDED、符号链接和动态路径，并排除 driver、`cc1`、plugin 与无关
multilib。manifest 记录文件位置及 `GLIBC`、`GLIBCXX`、`CXXABI`、`GCC` 报告。

LLVM export 拥有 libc++ headers、Clang resource、compiler-rt builtins、libc++、
libc++abi 和 libunwind，验证同样边界与精确源码证据，并同时发布共享与静态形式。
共享库按内部 SONAME closure 校验；`libc++.a`、`libc++abi.a`、`libunwind.a` 必须
完整存在，并逐个校验其中的 relocatable member 是否属于所选 target。托管 libc++
构建还必须包含并发布 `libc++experimental.a`；外部 LLVM prefix 提供该归档时，导入
流程会保留并验证它。binding 不会隐式启用 experimental libc++ 功能，也不会自动链接
该归档。LLVM runtime closure 禁止 `libstdc++.so.6` 和 `libgcc_s.so.1`。

runtime-bound wrapper 不添加部署 RPATH/RUNPATH。动态输出必须通过受控 loader 布局
提供所选共享库，并递归审计全部 DSO。GCC 通常包括 `libstdc++.so.6`、
`libgcc_s.so.1` 及按需的 `libatomic.so.1`/`libquadmath.so.0`；LLVM 包括所选
libc++、libc++abi、libunwind。

`lxtc shell` 是开发便利环境：子 shell 只暴露所选 C++ runtime 目录，直接执行仍使用
主机 loader 和 libc。`lxtc run` 提供更严格的本地检查，使用 SDK loader，并拒绝
closure 回退到未选择的 managed runtime 或系统库目录。两个命令都不会修改输出文件，
也不能代替部署阶段的 loader 布局和递归 ELF 审计。`external-unpinned` 不属于严格
runtime-pinned 隔离模式。

## 编译器与目标工具选择

外部 binding 通过 driver 查找 target `as`、`ar`、`ranlib`、`nm`、`strip`、
`objcopy`、`objdump`，runtime-pinned 模式还解析 linker，并记录 invocation path。
托管 binding 只从 Compiler Kit manifest 加载 driver 和目标工具，不做 host `PATH`
发现。所有 managed kit 都提供 BFD、Gold 和 Mold，Clang kit 还提供 LLD。

生成的 `cc`、`c++` launcher 添加所选 sysroot 和 runtime 输入，然后原样透传使用方
参数。目标 binary tools 和 runtime 选择的 linker 直接链接到所选可执行文件，不在
每次调用时解析参数或复查身份。外部 binding 的路径描述一套本机编译器安装，因此仍是
machine-local 制品。命令行、Clang 配置文件和 response file 中的 target 选项由 Clang
解释，binding 不会把它们改写成另一种 target。managed runtime 布局允许架构相同而
vendor 写法不同的 target；不兼容 target 会由 driver 或后续验证拒绝。GCC 使用构建时
确定的 target-specific driver，不支持 Clang 的 `--target` 选项。

## 生成端验证

runtime-pinned binding 提供精确 SDK sysroot、runtime 目录、startup files 和所选
linker。wrapper 清除编译器搜索路径环境变量，使生成的构建环境不依赖调用它的 shell；它不
分类或拒绝使用方传入的编译器、链接器、response file 或 plugin 参数。
binding 自有的 linker 搜索路径只在 driver 真正调用所选 linker 时加入。LLVM binding
还分别提供共享和静态 libunwind linker 入口；静态入口携带 libunwind 所需的 pthread
和动态加载依赖，不会改变未选择 libunwind 的调用。

发布前会编译 C/C++ probe、运行目标 archive/binary tools，并链接普通 executable、
shared library 及全静态 C/C++ executable；link map 和 ELF 必须证明 libc 来自 SDK、
runtime 来自 overlay，静态 probe 不能有 dynamic interpreter 或 `DT_NEEDED`；每个
发布 linker 都会通过 driver 验证。Clang 和 GCC 12 及以上版本使用原生 `-fuse-ld`
选择；GCC 10 和 11 不接受 `-fuse-ld=mold`，对应 binding 会发布带有固定、自有 `-B`
选择器的 `cc-mold` 与 `c++-mold` driver。

managed Clang binding 把原生 GCC 查找固定到包内 GCC runtime，并按 Clang 标准安装
布局提供 libc++ headers 和 compiler-rt。`-stdlib`、`--rtlib`、`--unwindlib` 仍由
Clang 自己解析；Linux 默认保持 libstdc++/libgcc，选择
libc++/compiler-rt/libunwind 时使用匹配的 LLVM overlay。生成端 probe 会覆盖两种选择、
`-Werror=unused-command-line-argument` 下重复的 `-stdlib`、架构相同而 vendor 不同的
target 写法以及静态链接。选择 LLVM 时产物不得依赖 GCC runtime SONAME。发布验证仍需
审计使用方实际参数构建的产物。

## 嵌入现有进程

链接时固定不能控制已运行进程的全局动态加载 namespace。host 或其他 plugin 可能先
加载同 SONAME 的 `libstdc++.so.6`/`libgcc_s.so.1`，loader 随后会复用它；再携带一份
同 SONAME 文件不会隔离 runtime。

因此 `pinned-gcc-runtime` 证明构建和独立链接闭包，但不能单独证明嵌入进程最终使用
哪份 libstdc++。可控制进程级 loader path、要求兼容系统 runtime，或把 native 组件
隔离到独立进程。静态链接也不是通用隔离：仍需审查再分发条款、symbol interposition，
且 C++ 对象、异常、内存或 TLS 不得跨不兼容 runtime 边界。Clang+libc++ 避免直接
SONAME 冲突，但仍需定义 ABI 边界并验证 unwind/loader。

release 测试应由真实 host 加载共享库，覆盖初始化、异常、线程、分配/释放和重复使用，
并记录进程库映射。JVM/JNI 是典型场景；独立 executable 验证不能替代该测试。

## 运行时实现和性能

SDK 设置 ABI 上限，并不把旧 glibc 实现随产品发布。兼容的新 host 由其 loader 提供
公开 symbol 的新实现，`memcpy` 等 IFUNC 仍可选择针对部署 CPU 优化的实现。代价在
API 边界：旧 floor 构建不能直接依赖新 glibc 才有的声明/symbol，需要单独建模并运行
时检测。性能还依赖 allocator、kernel、CPU 和 workload，ABI 审计不作性能保证。

## 这些检查无法保证什么

通过 glibc-floor 审计不证明：kernel syscall 可用性、CPU 指令兼容、可选/plugin
正确性、未审计 library closure、启动环境改变的 loader 行为、用其他输入构建的 cache
package、旧目标机真实运行行为，以及带预加载 native runtime 的进程内兼容性。

binding 不设置 CPU 指令集基线。使用方可以通过 `-march`、`-mcpu`、`-mtune`、source
attribute、汇编或 runtime dispatch 选择编译器支持的任意指令集。该选择与 glibc ABI
下限相互独立，必须写入产品自己的 CPU 部署约束。默认审计允许 `$ORIGIN` 锚定路径并拒绝
普通相对/绝对动态路径；打包必须证明展开后仍在发布树内。带 `/` 的 DT_NEEDED 和
path-valued SONAME 被拒绝。

## 发布前必须完成的验证

1. 保存所选模式使用的 SDK、build-tools、编译器、运行时层和绑定清单。
2. 通过所选 CMake、shell 或包管理器接入重建全部目标依赖，或核实每个依赖的来源。
3. 递归检查所有可执行文件、共享库和随产品发布的运行时 DSO。
4. 按实际部署目录验证完整的动态加载依赖闭包。
5. 为每个绑定运行小型 C++ 共享库和可执行文件验证。
6. 在符合目标内核、glibc、动态加载器、架构和 CPU 策略的环境中测试。
7. 嵌入式组件必须由真实宿主进程加载并测试，同时检查进程的库映射；JVM/JNI 属于此类场景。

小型验证项目适合日常检查，正式发布仍需构建真实项目并在宿主进程中测试。如果无法
确定最老的生产系统，应选择多个目录中可用的版本下限作为产品策略，并保留对应的测试
环境。随着部署环境发生变化，可以增加或淘汰这些下限。目录让选择可以复核，但不会
替产品自动决定正确的版本下限。
