# 示例

[English](README.md) | [简体中文](README.zh-CN.md)

这些文件只作为源码仓库中的示例，不会安装到 Wheel。请在源码仓库根目录执行本文命令。

`sdk/` 下的文件展示两个 target 的严格 `--spec` 格式：

- `sdk/glibc-2.19-x86_64.json` 选择 x86-64 SDK。
- `sdk/glibc-2.36-aarch64.json` 选择 AArch64 SDK。

可以用下面的命令渲染任一配置：

```bash
linux-toolchain sdk render \
  --spec examples/sdk/glibc-2.19-x86_64.json \
  --workspace out/glibc-2.19-x86_64
```

常规使用时，`sdk render --glibc VERSION --arch ARCH` 更简短，并会解析到同一套固定
目录。示例文件不定义可用版本范围，实际范围见 [SDK 后端目录](../docs/zh-CN/recipe-catalog.md)。

`managed/compiler-matrix.json` 展示严格的托管编译器矩阵。它有意组合多个编译器版本、
目标和 C++ 运行时策略，使一个 lock 文件可以对共享源码和制品节点去重：

```bash
linux-toolchain managed lock \
  --spec examples/managed/compiler-matrix.json \
  --output out/managed.lock.json

linux-toolchain managed artifacts \
  --lock out/managed.lock.json
```

示例中的版本选择不是永久上限。已安装程序中的托管版本目录决定精确源码身份，详见
[托管编译器](../docs/zh-CN/managed-compilers.md)。

仓库不提供固定的检查策略示例。`bind external` 会根据所选 SDK 和架构生成
`audit-policy.json`，避免误用来自其他 glibc 下限或目标架构的策略。
