# 故障排查

[English](../troubleshooting.md) | [简体中文](troubleshooting.md)

先运行与目标命令最接近的诊断：

```bash
linux-toolchain doctor --workflow managed
```

文本报告适合直接阅读，`--json` 适合交给 CI 保存和分析。某条工作流不需要的可选工具
即使缺失，也不会让该工作流失败。

## 无法连接 Docker

SDK 和托管生产要求非 root 用户及通过 Unix endpoint 提供的本地 Linux Docker
daemon。远程 TCP context 会被拒绝，因为构建使用 client 本地只读 bind mount。
检查当前 context、daemon 权限和 server 平台。外部 binding、审计和已安装使用方
接入不需要 Docker。

## 找不到请求的 glibc 版本

运行 `linux-toolchain sdk list --arch ARCH`。生成器只接受经过评审并固定的 backend
条目，不会为未知 glibc 版本猜测 toolchain 配置。应携带源码 hash 和验证证据扩展
backend，或选择已安装 catalog 条目。

## 程序仍然要求更高版本的 GLIBCXX 或 GCC

glibc SDK 只控制与 libc 相关的输入。使用 GCC 运行时层固定 libstdc++、libgcc、
编译器 CRT 和头文件，或使用托管 Clang libc++ 配置从已验证的 C++ 依赖闭包中移除
GCC 运行时。检查范围应覆盖完整部署目录，而不只是主共享库。

## 托管构建中断

重新运行同一个 `managed assemble`。完成的制品只有重新验证后才会复用，匹配的生成工作
可以继续；`managed fetch` 只是可选预取。所选 artifact、源码、SDK、compiler backend
或目标工具变化时，应使用新的 workspace。`--rebuild` 只用于选择匹配且由生成器管理的
workspace。

## 绑定输出目录已经存在

绑定属于当前机器，并包含可执行文件的绝对路径和身份。移动 SDK、运行时层或编译器后
应使用新的输出目录。`--force` 只替换可识别为生成器所有的输出，不会删除任意目录。

## 快速验证可以编译，但程序无法运行

检查 smoke 构建目录中的 `audit-report.json`、`loader-closure.txt` 和
`runtime-output.txt`。交叉目标需要显式执行器（如 `qemu-aarch64`），正式发布仍需在
接近真实目标的机器上测试。CPU 和最低内核版本必须独立于 glibc 符号下限验证。

## 绑定中缺少需要的接入文件

底层 binding 只包含创建时选择的 integration，默认 CMake 和 shell，Conan 需显式
选择；高层 setup binding 同时包含三种。若缺少 `cmake/toolchain.cmake`、
`env/toolchain.env` 或 `conan/host.profile`，应使用相应 `--integration` 重新生成底层
binding，或重新生成高层 setup binding。不要跨 binding 复制接入文件，其中包含一套
精确 binding 的路径和身份输入。

## 宿主进程加载了另一份 libstdc++

host 应用或其他 plugin 可能先加载了同 SONAME C++ runtime。固定 runtime 控制构建和
部署证据，但不会创建独立动态加载器 namespace。应测试真实 host 加载路径、异常和
线程，并检查进程库映射；JVM 加载 JNI 库就是此类场景。参见
[兼容性边界](compatibility.md#嵌入现有进程)。

## 捕获命令输出

写命令结果和 JSON 报告使用 stdout，长构建进度和 Docker 输出使用 stderr：

```bash
BINDING_MANIFEST="$(linux-toolchain managed assemble ... 2>build.log)"
```

使用捕获结果前必须检查退出状态。
