# Integration smoke project

[English](smoke-project.md) | [简体中文](zh-CN/smoke-project.md)

The smoke project is the fast consumer check for a generated compiler binding.
It is intentionally small enough to run for every binding during normal
development. It does not replace a real consumer release matrix.

The project builds a C++17 shared library and executable. Its sources exercise
the C++ runtime, threads, `memcpy`, `std::string`, exceptions,
`dlopen`/`dlsym`, and a preprocessed assembly source shared by x86-64 and
AArch64. The runner then audits the outputs, checks their loader closure and
runs the executable with eager binding.

The smoke project is installed with the wheel. `BINDING` is the directory
produced by `linux-toolchain bind external`, `linux-toolchain bind managed` or
`linux-toolchain managed assemble`, not its manifest path:

```bash
BINDING="$PWD/out/binding-managed"
```

`--build-type` configures this consumer build and defaults to `Release`. It is
not read from, or written into, the core binding policy.

## Direct CMake smoke

Direct CMake is the default integration mode. It configures the smoke project
with `${BINDING}/cmake/toolchain.cmake` and does not require Conan. The binding
must have been created with `--integration cmake`, which is part of the default
binding selection:

```bash
linux-toolchain smoke \
  --integration cmake \
  --build-type Release \
  --binding "${BINDING}" \
  --build-dir out/smoke-cmake
```

The runner verifies that CMake selected the binding's C and C++ wrappers,
compiler-driver ASM path and recorded target tools. It then builds and audits
the shared library and executable.

## Conan smoke

Use the explicit Conan mode when validating a binding created with
`--integration conan`, including its generated host profile and the composition
between Conan's CMakeToolchain and the binding:

```bash
linux-toolchain smoke \
  --integration conan \
  --build-type Release \
  --binding "${BINDING}" \
  --build-dir out/smoke-conan
```

By default this mode creates an isolated Conan home under the build directory,
installs `linux-toolchain`'s `settings_user.yml`, and detects a native
`smoke-build` profile. The binding profile is used only in the host context and
is never reused for build requirements. An existing native build profile and
Conan home can be selected explicitly:

```bash
linux-toolchain smoke \
  --integration conan \
  --build-type Release \
  --binding "${BINDING}" \
  --build-profile "${BUILD_PROFILE}" \
  --build-dir out/smoke-conan \
  --conan-home "${CONAN_HOME}"
```

The runner may replace `settings_user.yml` only in its default managed Conan
home. For an explicitly selected home, an existing identical file is accepted
but different settings fail closed. An explicit Conan home must not overlap the
build directory and requires an explicit build profile.

In Conan mode, `--build-type` must match the build type recorded in the
binding's Conan host profile. The runner rejects a mismatch before invoking
Conan so dependency and consumer configurations cannot diverge.

The runner does not download dependencies or use Conan remotes. Explicit tool
paths may be supplied when the required executables are not on `PATH`.

## Make smoke for the shell integration

`env/toolchain.env` is the build-system-neutral entry point for Make,
Autotools, hand-written Ninja graphs and other configure systems. The smoke
project includes a small Makefile that exercises this entry point directly. The
binding must have been created with `--integration shell`, which is also a
default selection:

```bash
linux-toolchain smoke \
  --integration shell \
  --build-type Release \
  --binding "${BINDING}" \
  --build-dir out/smoke-make
```

This mode sources the generated file in a POSIX shell, builds the same C++ and
assembly sources through Make, and applies the same ELF, loader-closure and
runtime checks. `--build-type` selects matching optimization and debug flags in
the packaged Makefile. It verifies the standard compiler variables for this
Make workflow. Each real Autotools or Ninja consumer should retain a focused
configure/build probe for the interface it uses. See
[Consumer integration](integrations.md) for examples.

## Cross-target execution

For a cross-built executable that can run under user-mode emulation, pass an
explicit runner such as `--runner qemu-aarch64`. A target-machine run is still
preferred for release qualification. Configure steps must not execute target
programs implicitly.

Runtime closure is accepted only for bindings created with a pinned GCC or LLVM
runtime export. An `external-unpinned` binding may compile successfully, but
its C++ runtime deployment inputs are not owned by the binding and cannot pass
the controlled loader check.

## Smoke outputs and build-directory reuse

The build directory must be empty or have been created by a previous smoke run.

A passing run leaves `audit-report.json`, `loader-closure.txt`,
`runtime-output.txt`, and `result.json` under the build directory. The result
records the smoke build type, integration mode, policy floor and highest GLIBC
version observed in the audited outputs.

Use the smoke project on every binding used in normal development. A release
matrix must still build the real consumer and target
dependencies from known sources, recursively audit the deployment tree and run
in an environment representative of the minimum kernel, glibc loader,
architecture and CPU policy.
