# SDK 后端目录

[English](../recipe-catalog.md) | [简体中文](recipe-catalog.md)

`sdk render --glibc VERSION --arch ARCH` 通过固定的 crosstool-NG 后端目录解析
glibc 版本。

| Family | 可用 glibc 条目 | crosstool-NG | Builder 平台 | Linux headers | 编译 backend | binutils |
| --- | --- | --- | --- | --- | --- | --- |
| `crosstool-ng-1.28.0` | 2.17、2.19、2.23 至 2.42 | 1.28.0 | `linux/amd64`、`linux/arm64` | 6.12.41 | GCC 9.5.0 | 2.45；AArch64 搭配低于 2.26 的 glibc 时使用 2.29.1 |

glibc 条目是离散列表，不是连续数值范围。每个条目都提供 x86-64 和 AArch64
target。crosstool-NG 要求 AArch64 搭配低于 2.26 的 glibc 时使用低于 2.30 的
binutils，因此这些条目固定使用 binutils 2.29.1，而不是 2.45。使用
`linux-toolchain sdk list` 查看当前生成器内置的目录；`--arch` 筛选
target，`--json` 输出稳定的 `linux-toolchain-sdk-catalog` format 1 文档。

target 架构选择匹配的原生 builder platform：x86-64 使用 `linux/amd64`，AArch64 使用
`linux/arm64`。crosstool-NG 在固定多平台镜像中选择对应 platform；Docker daemon 必须
报告完全相同的平台，架构模拟不属于生产路径。

普通 SDK 构建生成 glibc sysroot，并把生成流程所需的 target tools 作为独立私有状态
保留；它不会完成一套供使用方使用的 C/C++ compiler。managed setup 会另外构建完整的
原生 compiler backend，用于生成托管 GCC 和 Clang Compiler Kit。因此 managed 构建
不会从 host `PATH` 选择编译器。该 backend 只属于生成端，不进入公共 SDK 或 Compiler
Kit，也不是 consumer 使用的编译器。

compiler backend 使用 producer 架构和所选 Compiler Kit host glibc floor。高层 setup
未显式传入 `--host-glibc-floor` 时，该值跟随 target glibc floor。发布的 managed
compiler ELF 按解析后的 host floor 审计；target binutils 必须静态链接，避免 builder
container 的 libc 泄漏进 Compiler Kit。

目录条目只说明解析器能够生成固定的构建输入，不代表组合已通过发布验证。发布仍需
真实构建、smoke test、ELF audit、loader closure 和接近 target 环境的 consumer
证据。

Linux UAPI headers 独立于 glibc floor 和 `minimum_kernel`。新 headers 只提供声明，
不能让旧 kernel 支持新 syscall；可选 kernel 能力仍需运行时检测和回退。

扩展目录需要验证 crosstool-NG 版本，并精确固定 Linux、GCC、binutils、builder
平台、源码与 hash。解析器不会为未知 glibc 版本猜测 backend。

x86-64 默认 `x86-64`/Linux 3.2，AArch64 默认 `armv8-a`/Linux 3.7。请求可以提高但
不能降低 baseline：

```shell
linux-toolchain sdk create \
  --glibc 2.42 \
  --arch aarch64 \
  --name portable-arm64-compat \
  --minimum-kernel 4.4.0 \
  --jobs 2 \
  --workspace out/portable-arm64-compat
```

`--spec` 接受完整显式 `SdkSpec`；`--name` 和 `--minimum-kernel` 仍是 selection
override。`--jobs` 只控制本次构建执行，不写入 SDK spec 或内容身份。显式 spec 仍受
固定 backend 目录和源码包校验约束。
