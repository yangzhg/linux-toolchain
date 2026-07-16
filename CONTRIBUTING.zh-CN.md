# 参与贡献

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

`linux-toolchain` 控制 Linux C/C++ 构建使用的 libc、编译器运行时和链接器输入。修改
这些边界时，测试既要证明预期行为，也要覆盖不兼容的输入。

## 准备开发环境

项目支持 Python 3.10 及以上版本，运行时不依赖第三方 Python 包。在仓库根目录执行：

```bash
make bootstrap
make lint
make check
```

`make bootstrap` 创建 `.venv`。如需使用其他专用目录，可传
`VENV=/path/to/venv`。`make lint` 使用固定版本的 Ruff 检查导入、常见错误和格式；
`make check` 编译 Python 源码并运行全部单元测试。单元测试不需要 Docker 或网络。

## 设计边界

四层制品必须保持独立：

- SDK 包含 glibc、动态加载器、启动文件和 Linux UAPI 头文件。
- 托管 Compiler Kit 包含固定版本的 GCC 或 Clang 驱动和清单声明的目标工具，不包含
  目标 C++ 运行时。
- 运行时层包含编译器提供的头文件、CRT 文件以及 GCC 或 LLVM 运行时库，不包含编译器
  可执行文件。
- 绑定把 SDK 和运行时层连接到外部编译器或托管 Compiler Kit，并生成封装命令、检查
  策略和所选 CMake、shell 或 Conan 接入文件。

glibc 符号下限不是完整的运行时兼容保证。

管理流程独立于使用方仓库。`linux-toolchain setup` 在显式生成端工作目录下维护选择、
binding 和验证状态，从独立 store 复用生成端制品，再按需向显式 prefix 发布可独立使用的
安装。不要重新引入项目根目录初始化、需要提交的业务项目配置、向上查找目录的入口发现
方式，或随 Python 包安装的全局 launcher。工作目录和安装目录中的选择不可变；
`--force` 只能重建或替换选择相同且由生成器管理的输出。

代码使用 `pathlib.Path`、类型标注、确定性 JSON 和 `linux_toolchain.errors` 中的领域
错误。调用外部程序时向进程执行层传递参数数组，不要用用户输入拼接 shell 命令。

## 修改版本目录

SDK 和托管编译器目录只说明解析器支持哪些固定组合，不能代替发布测试。增加 glibc
版本或 SDK 后端时必须：

1. 固定兼容的 crosstool-NG 版本和构建基础镜像。
2. 固定 Linux、GCC、binutils、glibc 和源码包的版本与哈希。
3. 添加架构约束和最低内核规则。
4. 用单元测试覆盖目录解析、配置渲染、压缩包校验和 SDK 导出。
5. 在声称支持前真实构建 SDK，并运行一个代表性的项目验收组合。

未知组合必须失败，不能自动沿用其他 backend family 的组件。

增加托管 GCC 或 LLVM 版本时，需要官方源码身份、精确的发行源码包 SHA-512、版本选择
测试和确定性 lockfile 测试。文档把该版本标为已验证前，至少要完成一次真实的 Compiler
Kit 和运行时构建。不要在清单模型中加入最高版本保护；可解析范围由固定目录决定。

## 修改兼容性边界

编译器 launcher 和目标工具链接属于兼容性输入；修改固定参数或工具选择时，需要有
代表性的编译和链接测试。托管源码获取、原始运行时发布、输出所有权和制品清单需要
有针对性的正确性测试。发布前要运行完整编译器和 glibc 矩阵。

ELF 策略比较符号需求而不是符号定义。必须继续无条件拒绝 `GLIBC_PRIVATE`，保留架构
和动态加载器检查，并区分 glibc、内核、CPU 与 C++ 运行时兼容性。

## 构建发布 Bundle

Bundle 可以来自验证通过的 setup 准备态、完整安装，或一组显式指定的完整 managed
制品。源码仓库中的普通路径先执行 `setup --prepare-only`，再执行
`bundle create --config`，不需要中间安装目录。发布记录应保留 Bundle manifest、生成端
验证结果、binding smoke 结果，以及代表性目标环境中的真实项目结果。单元测试不能认证
真实工具链矩阵；每个正式发布组合都要在声明的最低主机上运行安装器和已安装 launcher。

`make clean` 会删除仓库内的 setup 工作目录、Bundle 输出、Python 构建产物和工具缓存，
但保留 `.venv` 以及显式放在 `out/store` 的 store。Make 工作流默认把 producer store
放在 `out/` 之外的用户缓存目录，因此两个清理目标都不会删除其中可复用的生成端制品。
`make purge` 还会删除 `.venv` 和整个 `out/`，包括显式放在其中的 producer store，但
不会遍历默认或外部 producer store。两个命令都不会删除仓库外的安装目录。

## 提交前检查

- CI 支持的每个 Python 小版本都通过 `make lint` 和 `make check`。
- 解析器、生成器和策略修改有对应测试。
- `git diff --check` 没有空白字符错误。
- 源码树中没有 SDK、下载包、工具链、绑定、凭据或构建输出。
- `.run` 文件已在声明的最低主机上安装和运行。
- 中英文用户文档描述相同的最终行为，不记录修改过程。
