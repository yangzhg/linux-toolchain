# 变更记录

[English](CHANGELOG.md) | [简体中文](CHANGELOG.zh-CN.md)

## 0.1.0

Linux 工具链生成器首次发布，包含：

- 面向 x86-64 和 AArch64 的固定版本 glibc SDK；
- 外部和托管 GCC/Clang 编译器绑定；
- 托管 libstdc++/libgcc runtime（包括 x86-64 libquadmath 输入），以及同时发布
  shared/static libc++、libc++abi、libunwind 和 compiler-rt 的 runtime；
- 单命令托管 setup 将可恢复的生成端状态与可独立使用的本机安装 prefix 分开，业务
  项目无需配置文件；工作目录和安装目录中的选择不可变；
- 确定性的 shell 自解压托管工具链 Bundle，包括生成端验证的 binding 模板、空 prefix
  安装、目标端无需 Python、安装时选择 launcher 名称，以及从已验证安装目录直接
  创建发布包；
- CMake、shell 和可选 Conan 项目接入；
- ELF ABI 下限检查和随包提供的项目快速验证；
- 确定性清单、构建输入校验和制品原子发布。
