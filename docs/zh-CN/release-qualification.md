# 发布认证

[English](../release-qualification.md) | [简体中文](release-qualification.md)

本页是项目对外声明“已完成发布认证”的工具链组合索引。catalog 条目和单元测试只能证明
输入已经建模、生成策略得到执行，不能认证一份真实 Bundle。

## 当前记录

当前 checkout 尚未记录任何完成发布认证的组合。catalog 中的选择仍可用于生产和验证，
但在证据链接到本页之前，文档不得把它描述为已认证。

## 一条认证记录包含什么

认证结果只属于以下输入完全确定的一组组合：

- 源码 revision 或 release tag，以及 Bundle identity；
- producer 架构、Compiler Kit 版本和 host glibc floor；
- target 架构、SDK glibc floor 和 runtime 版本；
- Clang 使用的 GCC runtime provider（如适用）；
- 已发布 linker 集合和 host build-tools 版本；
- consumer integration，以及实际运行验证所用的最低 host 环境。

其中任一输入变化都会形成新的认证组合。jobs、输出目录和下载 mirror 通常只是执行细节，
除非某个 artifact identity 明确包含它们。

## 必需证据

一条完成认证的记录必须保留：

1. SDK、build-tools、Compiler Kit、runtime、binding 和 Bundle manifest，以及已验证的
   源码 identity。
2. 一次干净的 producer 构建，以及 compiler、runtime、build-tools 的 ELF audit。
3. 在声明的最低 host 上安装 Bundle 并执行 launcher 的结果。
4. C/C++ 编译链接、shared 与全静态链接、每个已发布 linker 和每套已发布 C++ runtime
   的证据。双 runtime Clang 必须分别验证 libstdc++ 和 libc++。
5. 对安装产物递归执行的 loader closure 与 symbol-version 检查。
6. 代表性 target-like consumer 的构建和运行结果；嵌入式 library 还必须由真实 host
   process 加载验证。
7. 可复现命令、相关环境信息，以及完整日志或不可变的 CI/release 链接。

mock 测试、catalog 解析成功、源码下载成功或单个 compiler 验证都不能单独作为发布
认证证据。

## 已发布结果

| Release | 认证组合 | 证据 | 状态 |
| --- | --- | --- | --- |
| 无 | 无 | 尚未发布证据记录 | 未认证 |

只有完整证据已经存在时才增加记录，并链接到能够精确标识该组合的不可变 release asset、
CI run 或仓库内报告。部分运行结果和已知问题应写入证据报告，但不能标记为
`qualified`。
