# 快速验证项目

[English](../smoke-project.md) | [简体中文](smoke-project.md)

这个小型项目用于快速检查生成的编译器绑定。它可以在日常开发中为每个绑定运行，
但不能替代真实项目的发布测试矩阵。

项目会构建一个 C++17 共享库和一个可执行文件，覆盖 C++ 运行时、线程、`memcpy`、
`std::string`、异常、`dlopen`/`dlsym`，以及 x86-64 和 AArch64 共用的预处理汇编源。
随后，验证程序检查生成文件和动态加载依赖闭包，并在立即符号绑定模式下运行程序。

该项目随 Wheel 安装。`BINDING` 是 `bind external`、`bind managed` 或
`managed assemble` 生成的绑定目录，不是清单文件：

```bash
BINDING="$PWD/out/binding-managed"
```

`--build-type` 只配置这次项目构建，默认值为 `Release`，不会改变绑定策略。

## 使用 CMake 验证

直接 CMake 是默认模式，使用 `${BINDING}/cmake/toolchain.cmake`，不需要 Conan。
创建绑定时必须选择 `--integration cmake`：

```bash
linux-toolchain smoke \
  --integration cmake \
  --build-type Release \
  --binding "${BINDING}" \
  --build-dir out/smoke-cmake
```

验证程序会确认 CMake 使用绑定中的 C/C++ 封装命令、编译器驱动提供的 ASM 路径和
目标工具，然后构建并检查共享库与可执行文件。

## 使用 Conan 验证

验证带 `--integration conan` 的绑定时使用 Conan 模式。它会检查生成的 host profile，
以及 Conan `CMakeToolchain` 与绑定的组合方式：

```bash
linux-toolchain smoke \
  --integration conan \
  --build-type Release \
  --binding "${BINDING}" \
  --build-dir out/smoke-conan
```

默认情况下，程序会在构建目录下创建隔离的 Conan home，安装项目提供的
`settings_user.yml`，并检测原生 `smoke-build` profile。绑定 profile 只用于 Conan
的 host context，不能用于 build requirements。也可以显式指定已有的原生 profile
和 Conan home：

```bash
linux-toolchain smoke \
  --integration conan \
  --build-type Release \
  --binding "${BINDING}" \
  --build-profile "${BUILD_PROFILE}" \
  --build-dir out/smoke-conan \
  --conan-home "${CONAN_HOME}"
```

runner 只能在默认受管 Conan home 中替换 settings。显式 home 中相同文件可复用，
不同内容会失败；显式 home 不得与 build 目录重叠，且必须同时指定 build profile。
`--build-type` 必须与 binding host profile 一致。runner 不下载依赖或访问 remote。
所需 executable 不在 `PATH` 时可以显式传入工具路径。

## 使用 shell 接入和 Make 验证

`env/toolchain.env` 是 Make、Autotools、手写 Ninja 等系统的中立入口。binding 必须
包含 `--integration shell`：

```bash
linux-toolchain smoke \
  --integration shell \
  --build-type Release \
  --binding "${BINDING}" \
  --build-dir out/smoke-make
```

该模式在 POSIX shell 中加载环境，通过 Make 构建相同 C++/汇编源，并执行相同 ELF、
loader closure 和运行检查。这里只验证这条 Make 流程使用的标准编译器变量；真实
Autotools/Ninja 使用方仍应保留自身的 configure/build probe。

## 运行交叉编译结果

如果交叉编译结果能在用户态模拟器中运行，可传 `--runner qemu-aarch64`。正式发布仍
优先在目标机器上执行，configure 过程不得隐式运行目标程序。只有绑定到固定 GCC 或
LLVM 运行时层的配置才能通过受控运行时依赖闭包检查；`external-unpinned` 即使编译
成功，也不能控制部署时使用的 C++ 运行时。

## 输出文件和构建目录复用

build 目录必须为空，或由之前的 smoke 运行创建。

验证通过后，构建目录会保留 `audit-report.json`、`loader-closure.txt`、
`runtime-output.txt` 和 `result.json`。结果中记录构建类型、接入方式、策略下限，以及
检查到的最高 GLIBC 版本。日常开发应为每个绑定运行这个项目。正式发布仍需从已知来源
构建真实项目及其依赖，递归检查部署目录，并在符合最低内核、动态加载器、架构和 CPU
策略的环境中运行。
