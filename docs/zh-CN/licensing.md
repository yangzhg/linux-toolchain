# 发布制品中的许可证文件

[English](../licensing.md) | [简体中文](licensing.md)

生成的工具链会重新分发多个上游项目的代码和数据。仓库许可证只覆盖生成器，不能
替代 SDK、编译器、目标工具或运行时文件自身的许可证条款。

## 每类制品包含哪些文件

以下生成制品包含顶层 `licenses/` 目录：

- SDK：从已经校验哈希的 glibc、Linux、compiler backend GCC 和 binutils 源码包提取
  许可证声明；
- 托管 Compiler Kit：GCC 或 llvm-project 的许可证声明、提供目标工具的 SDK 中的
  binutils 声明，以及每个随包主机 DSO 对应的 Ubuntu 软件包 copyright 文件；
- 原始托管 GCC/LLVM 运行时及其发布目录：固定编译器源码中的许可证声明，包括 GCC
  Runtime Library Exception 或相应 LLVM 运行时组件的许可证。

SDK `manifest.json` 和原始托管 `artifact.json` 包含完整许可证清单；托管运行时
发布在 `managed-publication.json` 中携带相同清单。每项都是 `licenses/` 下规范的
制品相对路径。读取方要求清单中的文件和各组件规定的许可证声明存在；该清单记录随包
内容，不提供逐文件内容认证。

托管构建程序使用 `dpkg-query` 将随包主机 DSO 映射到已安装的 Ubuntu 软件包，并复制
`/usr/share/doc/<package>/copyright`，并把映射写入
`licenses/ubuntu/dependencies.tsv`。每个 DSO 都必须有映射和 copyright 文件。
Compiler Kit 还要求继承自 SDK 源码证据的 binutils `COPYING`。

## 这份清单不能替代什么

清单只记录某个生成制品附带了哪些声明文件，不负责许可证分类、判断某种分发是否合法，
也不为独立导入的外部运行时自动生成归属材料。分发者仍需审查准确的上游条款，并
补充产品或所在司法辖区要求的材料。
