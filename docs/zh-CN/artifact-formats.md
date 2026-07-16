# 版本与制品格式

[English](../artifact-formats.md) | [简体中文](artifact-formats.md)

本项目遵循语义化版本。公共 CLI 命令、退出码、JSON 文档和生成的制品布局都属于
稳定的发布接口；面向人的表格和进度信息不是机器接口。Python 模块不提供受支持的公共
API。

## 结构化文档

每个公共顶层 JSON 文档都有明确的身份和表示版本。读取方要求 `schema` 精确匹配，
`format` 必须是整数 `1`；缺失、未知或类型错误的字段都会被拒绝。

| 文档 | Schema |
| --- | --- |
| SDK 输入规范 | `linux-toolchain-sdk-spec` |
| SDK 构建 workspace | `linux-toolchain-sdk-workspace` |
| 已发布 SDK manifest | `linux-toolchain-sdk` |
| 托管编译器规范 | `linux-toolchain-managed-spec` |
| 托管 lock | `linux-toolchain-managed-lock` |
| 托管构建 workspace | `linux-toolchain-managed-workspace` |
| 托管构建制品 | `linux-toolchain-managed-build-artifact` |
| 托管 runtime 发布 | `linux-toolchain-managed-publication` |
| 托管组装结果 | `linux-toolchain-managed-assembly` |
| Compiler Kit manifest | `linux-toolchain-compiler-kit` |
| GCC runtime manifest | `linux-toolchain-gcc-runtime` |
| LLVM runtime manifest | `linux-toolchain-llvm-runtime` |
| 编译器 binding | `linux-toolchain-binding` |
| ELF 审计策略 | `linux-toolchain-elf-audit-policy` |
| ELF 审计报告 | `linux-toolchain-elf-audit-report` |
| Doctor 报告 | `linux-toolchain-doctor` |
| 快速验证结果 | `linux-toolchain-smoke-result` |
| SDK catalog 结果 | `linux-toolchain-sdk-catalog` |
| 托管发布索引 | `linux-toolchain-managed-release-index` |
| 托管 lock 制品列表 | `linux-toolchain-managed-lock-artifacts` |
| 本机 setup 配置 | `linux-toolchain-setup` |
| setup 准备态 | `linux-toolchain-prepared-setup` |
| 已安装工具链与可迁移 Bundle manifest | `linux-toolchain-bundle` |

密码学哈希只用于对应文档明确声明的下载源码和固定构建输入。生成制品由 schema
元数据标识，并通过实际 compiler 和 ELF 行为验证。format-1 managed lock 中每个
source 的键固定为 `id`、`family`、`version`、`kind`、`url` 和 `sha512`；`kind` 固定为
`archive`，`sha512` 标识 GCC 或 LLVM 官方发行源码包的字节内容。managed build action
用 `{"kind":"archive","sha512":...}` 记录这份内容身份，`provenance.source` 只记录获取
URL。`managed-artifact` 类型的 LLVM runtime 源码证据只包含 `kind`、`version`、
`target`、`url` 和 `sha512`。本机绑定和构建工作目录可以记录
绝对路径；已发布的 SDK、Compiler Kit、运行时层和 lockfile 可以迁移。
托管构建 workspace 分别记录 SDK、该 SDK workspace 生成的 target tools 和 compiler
backend workspace 这三类本机输入。raw managed artifact 的顶层键固定为 `schema`、
`format`、`action`、`action_sha256`、`provenance`、`licenses` 和 `elf_audit`。
`action` 是唯一的静态构建身份；源码获取 URL、实际 builder image 和实际执行脚本
只作为 `provenance` 证据。
artifact 不复制 lock 或 catalog 身份。runtime publication receipt 的顶层键固定为
`schema`、`format`、`raw_action`、`publication_action`、
`publication_action_sha256` 和 `licenses`。raw action digest 只在
`publication_action.raw_action_sha256` 记录一次。

SDK 和 managed builder 元数据用 `apt_snapshot` 记录软件包来源选择。空字符串表示
Ubuntu 普通软件源，`YYYYMMDDTHHMMSSZ` 表示对应的 Ubuntu snapshot；这个值参与静态
builder 身份。实际解析出的 image ID 仍属于 provenance，不形成第二套 artifact 身份。
在普通源模式下，Docker image 和 build cache 被清理后，即使记录的 builder 输入相同，
也可能解析到不同版本的软件包字节。

LLVM runtime manifest 的 `abi.linkage` 固定为 `both`。`locations` 分别记录排序后的
`shared_libraries` 和 `static_libraries`；静态数组精确包含 `libc++.a`、
`libc++abi.a` 和 `libunwind.a`。

setup 配置和准备态都是显式生成端工作目录下的本机文档。`setup.json` 选择一个托管
编译器、目标和主要使用方 smoke 接入，由 `linux-toolchain setup` 生成，不是业务项目
的输入。
`state/prepared.json` 记录不可变 selection 的 hash，以及 lock、SDK workspace、managed
workspace、Compiler Kit、已发布 runtime、binding、smoke 结果和可选 Conan
home/build profile 的精确绝对路径。高层 setup 会先把未指定的
`--host-glibc-floor` 解析为 target glibc floor，再写入 `setup.json`。每个 format-1 setup file
都必须显式记录解析后的 `host_glibc_floor`；缺失该字段表示文档损坏，不能解释为隐含策略。
记录路径移动后必须重新执行 `setup`。一个工作目录的选择不可变；compiler、target、runtime、
integration 或策略变化时必须使用新的工作目录。并行 jobs 属于执行状态，不属于
selection，可以在同一个工作目录内调整。高层 `--force` 只授权修复或替换选择相同且由
生成器管理的 selection 输出。已经验证通过的不可变 producer artifact 仍可复用，不会
故意重编。

高层 setup binding 会同时生成 `cmake`、`shell` 和 `conan`。format-1 的
`integration` 字段标识使准备态合格的那一条生成端 smoke 结果，不是安装能力数组。
Conan primary smoke 可以记录生成端 Conan 运行状态；其他 primary integration 仅生成
未激活的 Conan adapter，不会创建这份生成端状态。

producer store 是独立的本机内容寻址命名空间。SDK workspace 身份来自 target、渲染后的
配置、源码、builder 输入和 export 规则；managed 父 workspace 身份来自 target SDK 和
compiler backend 身份，Compiler Kit、raw runtime 和 runtime publication 再按各自
build action 分目录，其中 runtime publication 还包含 runtime adapter revision。
源码缓存按源码内容寻址。并行 jobs
只影响执行，不改变这些身份，因此多套 setup selection 可以复用同一份验证通过的输入。

managed setup、安装发布和从准备态创建 Bundle 时，会为实际消费的 producer identity
取得共享 lease；同一 identity 的 writer 使用独占 lease。稳定目录保证只适用于这些
协调后的管理流程，不承诺任意外部 filesystem reader 在替换期间获得无锁可见性。

自解压 payload 中的 Bundle manifest 以规范 JSON 保存为 `manifest.json`。顶层键
固定为 `schema`、`format`、`id`、`variant`、`compiler`、`target`、`host`、
`runtime_kind` 和 `binding`。SDK、Compiler Kit、runtime、managed lock 与 binding
模板使用 format-1 规定的固定 payload 路径。host glibc 下限与 target glibc 下限
相互独立。
binding 记录选择的 integration 和可选 Conan host settings。高层 setup Bundle 选择
全部三种 adapter 并携带默认 Conan host settings；显式底层 binding 和 Bundle 保留
各自记录的选择。

payload 根目录只包含 `manifest.json`、`artifacts/`、`binding/`、`bin/` 和
`template-files`。Bundle 复用 setup 已验证的 binding 作为模板，把 binding 和 artifact
根路径替换为 prefix 占位符，因此生成模板不包含 work directory 或 producer store
路径。`template-files` 按顺序列出 shell 安装器需要实例化
的普通文件。公共格式不为安装器外层定义额外 schema。

binding 带有 Conan 时，`binding/conan/settings_user.yml` 是写入专属 Conan home 的
精确 settings 扩展。Bundle 组装会增加与 prefix 无关的 `default.profile` 和
`lxtc-build.profile` 选择器，分别通过 `LINUX_TOOLCHAIN_CONAN_HOST_PROFILE`、
`LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE` 委托；还会增加 `build.profile`，把安装后的
host profile 与托管 runtime 搜索路径组合起来供原生 build requirements 使用。该
build-context 文件属于原生 managed Bundle 格式，不属于通用底层 binding renderer。
安装后的 `binding/conan/conan-home` 和 `binding/conan/build-profile` 记录专属 home
与实际 build profile。Conan cache 不属于不可变安装 prefix。

## 制品的生成、复用和替换

已生成的制品按不可变输入处理。SDK、编译器、运行时、接入方式或策略变化时，应在
新目录重新生成并完成验证，再让项目切换到新制品。绑定包含本机可执行文件绝对路径，
不能直接移动到另一台机器或文件系统布局，必须重新生成。

生成端工作目录独立于所有使用方源码树，format 1 布局由生成器管理：

```text
WORK_DIR/
  .linux-toolchain-setup-root
  setup.json
  state/
    .linux-toolchain-setup-state
    prepared.json
    ...
```

可复用生成端输入位于 selection tree 之外：

```text
STORE_DIR/
  .linux-toolchain-producer-store
  sdk/IDENTITY/
  managed/IDENTITY/
  sdk-sources/
  sources/
  locks/
```

两个 marker 的内容都是 `format=1`。生成器拒绝缺少对应 marker 的非空工作目录
或 state 目录。只有 binding 和所选 smoke 路径成功后才原子发布
`state/prepared.json`。准备态合格还要求 smoke 结果为 format 1、状态为 `passed`，且其
binding 和 integration 仍与当前 selection 匹配。中断后可复用已验证的不可变产物，
但替换 binding 仍需证明生成器所有权。已有工作目录不能改作另一套 setup 选择。
producer store marker 同样是 `format=1`；非空且不属于生成器的 store 会被拒绝。

最终安装目录与已安装 Bundle 使用相同的顶层布局：

```text
PREFIX/
  manifest.json
  artifacts/
    sdk/
    compiler-kit/
    runtime/
    managed.lock.json
  binding/
  bin/
    lxtc
```

launcher 加载自身 prefix 下的 binding，可从任意工作目录调用，不依赖生成端状态或
Python CLI。安装 prefix 的选择不可变，复用或由生成器替换前都要验证。最终发布验证会
检查已知 manifest 字段、迁移后的声明路径和实例化文本模板，不会重复 compiler、linker、
loader 或 target-like smoke 认证，也不能当作 release qualification。

Bundle 安装器只接受不存在或为空的 prefix。它在同级临时目录解包，实例化清单中的
模板文件，删除 `template-files`，按安装时的 `--launcher-name` 重命名 `bin/lxtc`，
再把完整 payload 目录移动到 prefix。安装后的顶层是 `manifest.json`、`artifacts/`、
`binding/` 和 `bin/`。每个新 bundle 使用新的 prefix。
对于带 Conan 的 Bundle，安装器还会把匹配的静态 settings、目标 `default` profile
和 build context 的 `lxtc-build` profile 写入
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`，其中 `BUNDLE_DIGEST` 是 Bundle ID 的
SHA-256 摘要前 16 个十六进制字符；也可以显式指定 `--conan-home`。默认 build
context 是 Bundle 生成的托管原生 profile。
`--conan-build-profile` 可接受该专属 home 中的其他 profile 名称或绝对路径；显式覆盖
可在真正使用前再创建，但不能选择生成的 `lxtc-build` selector 自身。Conan home 与安装
prefix 不能互相包含。`--conan-cppstd` 只给目标 profile 增加
`compiler.cppstd` 覆盖；省略时，目标 profile 会写入 Conan 2 针对该托管编译器家族及
主版本建模的默认值。安装器不会调用 Conan。既有配置不同会失败关闭，安装器不会递归
替换 Conan home。
