# Consumer integration

[English](integrations.md) | [简体中文](zh-CN/integrations.md)

A generated binding records one compiler/runtime configuration and its selected
consumer integrations. Every binding provides compiler launchers, selected
binary tools and an ELF audit policy. Repeated `--integration cmake|shell|conan` options
select its integration files; omitting the option selects CMake and shell.
Conan is opt-in. The consumer does not need to know how the SDK, compiler or
runtime was produced.

The examples below assume:

```bash
BINDING="$PWD/out/binding-managed"
```

Use a different generated binding path when selecting another target floor,
architecture, compiler or runtime policy. Do not copy individual files between
bindings.

## Support matrix

The project distinguishes a native adapter from compatibility through the
standard shell environment. It does not claim a native adapter merely because a
build system can eventually invoke the generated compiler wrappers.

| Consumer | Selection | Generated input | Validation |
| --- | --- | --- | --- |
| CMake | `--integration cmake` | standalone toolchain file | dedicated smoke mode |
| POSIX shell / Make | `--integration shell` | compiler, target-tool and pkg-config environment | dedicated Make smoke mode |
| Conan 2 | `--integration conan` | host profile and CMake composition fragments | dedicated smoke mode |
| Autotools | `--integration shell` | standard variables plus target triplet | compatibility path; consumer configure test required |
| hand-written Ninja | `--integration shell` | wrappers available to graph or generating process | compatibility path; no native Ninja adapter |
| Meson | unavailable | no generated cross file | no native adapter |
| Bazel | unavailable | no registered C++ toolchain | no native adapter |

Check the executable prerequisites for the selected integration before
building. The consumer workflow checks CMake by default; select each additional
integration explicitly:

```bash
linux-toolchain doctor --workflow consumer --integration cmake
linux-toolchain doctor --workflow consumer --integration shell
linux-toolchain doctor --workflow consumer --integration conan
```

Create a machine-local installation in a prefix that is independent of any
consumer repository. Producer state defaults to the user cache directory; the
consumer project does not need a configuration file:

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --prefix /path/to/toolchains/gcc12-glibc219
export PATH=/path/to/toolchains/gcc12-glibc219/bin:"$PATH"
```

The high-level setup command runs on x86-64 and defaults to an x86-64 target,
so `--arch` is unnecessary for the normal native case. Release producers may
also select `--host-glibc-floor`, `--jobs`, and `--runner`. Clang uses
`--runtime` to choose libc++ or an explicit managed GCC runtime. High-level
setup always renders CMake, shell, and Conan adapters. `--integration` chooses
which one receives producer smoke qualification; choosing Conan also enables
its producer-smoke `--conan-*` options.

The generated launcher loads the binding's shell environment before executing the
consumer command. It can be invoked from any project directory:

```bash
lxtc make -j8
```

Setup always creates the launcher as `lxtc`. It loads the binding below its own
prefix and does not depend on producer state, Python or the management
executable. Installing the Python package alone does not create a global
launcher. Every setup selection is immutable within its work directory and
installation prefix. Use new paths for a different selection; `--force` only
repairs or replaces matching generator-owned selection outputs. Already-valid
immutable producer artifacts remain reusable rather than being deliberately
rebuilt.

The installed launcher selects a dedicated Conan home plus generated `default`
and `lxtc-build` profiles. Both contexts use the installed managed toolchain;
the build context also carries its runtime search path. A different existing
settings or profile file is rejected rather than overwritten. A consumer that
passes `--profile:host` or `--profile:build` explicitly can read the effective
paths from `LINUX_TOOLCHAIN_CONAN_HOST_PROFILE` and
`LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE`.

A prebuilt bundle exposes its selected integration at `PREFIX/bin/NAME`.
Installation defaults to `lxtc`; `--launcher-name NAME` selects the final name.
The selected integrations run without producer tools, Python, or Docker on the
consumer machine. The installed launcher is tied to its prefix; copy the
original `.run` file and reinstall instead of moving the installed directory.

For example, generate only the two package-manager-neutral integrations:

```bash
linux-toolchain bind external \
  --sdk "${SDK}" \
  --runtime "${RUNTIME}" \
  --cc "${CC}" \
  --cxx "${CXX}" \
  --integration cmake \
  --integration shell \
  --output out/binding
```

## Direct CMake

For a binding created with `--integration cmake`, the standalone CMake entry
point is `cmake/toolchain.cmake`. It selects the compiler wrappers, sysroot,
target tools and root-path policy without requiring Conan:

```bash
cmake -S . -B build/target \
  -DCMAKE_TOOLCHAIN_FILE="${BINDING}/cmake/toolchain.cmake" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/target
```

Configure into a fresh build directory when changing bindings. CMake evaluates
a toolchain file during its first configure and may otherwise retain the old
compiler, sysroot or target checks in `CMakeCache.txt`.

The generated file selects the target compiler, sysroot, linker, archive and
binary tools. Consumer projects add their normal compile and link flags,
including CPU instruction options such as `-march`.

## Make, Autotools and Ninja

For a binding created with `--integration shell`, `env/toolchain.env` is a
generated, shell-quoted environment file. Source it in a POSIX shell before
configuring or building:

```bash
. "${BINDING}/env/toolchain.env"
make -j8
```

It exports `CC`, `CXX`, `AR`, `RANLIB`, `AS`, `NM`, `STRIP`, `OBJCOPY`,
`OBJDUMP`, `PATH`, `LINUX_TOOLCHAIN_TARGET` and `LINUX_TOOLCHAIN_SYSROOT`.
`CMAKE_TOOLCHAIN_FILE` is present when the CMake integration was also selected.
The environment also exports target `pkg-config` sysroot/search settings. `LD`
is present when the binding selects a linker; an `external-unpinned` binding
may omit it. The compiler wrappers add the selected sysroot and runtime flags
and pass consumer arguments through.

For Autotools, use the recorded target triplet as the host and keep build tools
native:

```bash
. "${BINDING}/env/toolchain.env"
./configure --host="${LINUX_TOOLCHAIN_TARGET}" --build="$(./config.guess)"
make -j8
```

For a preconfigured Ninja graph or a project whose configure step reads the
standard compiler variables:

```bash
. "${BINDING}/env/toolchain.env"
ninja -C build/target
```

Ninja itself does not define a compiler model. The program that generates
`build.ninja` must consume these variables or write the binding wrapper paths
into its rules. Use `CC` rather than `AS` for preprocessed `.S` sources; `AS`
is the raw assembler interface for build systems that invoke one directly.

## Conan

Conan is an optional adapter. Select it while creating the binding and keep its
consumer policy in explicitly Conan-named options:

```bash
linux-toolchain bind external \
  --sdk "${SDK}" \
  --runtime "${RUNTIME}" \
  --cc "${CC}" \
  --cxx "${CXX}" \
  --integration conan \
  --conan-cppstd gnu17 \
  --conan-libcxx libstdc++11 \
  --conan-build-type Release \
  --output out/binding-conan
```

`--conan-cppstd`, `--conan-libcxx` and `--conan-build-type` describe the host
profile only. Omitting `--conan-cppstd` writes the compiler default modeled by
Conan 2 for the bound compiler family and major. These options do not alter direct
wrapper, CMake or shell invocations. For managed bindings, the selected variant
determines whether the actual runtime is libc++ or libstdc++; the Conan libcxx
value must agree with it.

Install the custom settings in every Conan home used for a target build:

```bash
conan_home="$(conan config home)"
linux-toolchain conan settings \
  --output "${conan_home}/settings_user.yml"
```

The command is idempotent when the file already matches. It refuses to replace
a different file unless `--force` is supplied. If a Conan home already has a
custom settings file, generate to a temporary path and merge the Linux and
compiler entries deliberately.

Use the generated profile only in Conan's host context. Build requirements and
generators need a separate native profile:

```bash
conan install . \
  --profile:host="${BINDING}/conan/host.profile" \
  --profile:build=default \
  --build=missing \
  --output-folder=build/target

cmake -S . -B build/target \
  -DCMAKE_TOOLCHAIN_FILE=build/target/conan_toolchain.cmake \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/target
```

The host profile carries the binding's compiler wrappers, sysroot, CMake
include and package-identity settings. Conan's generated toolchain composes
with the binding; do not also pass the standalone
`${BINDING}/cmake/toolchain.cmake` in the same configure.

After Conan has populated its CMake dependency search lists, the binding treats
each existing absolute entry in `CMAKE_PREFIX_PATH`, `CMAKE_LIBRARY_PATH` and
`CMAKE_INCLUDE_PATH` as an explicit target-input root. Library, include and
package lookup remain restricted to trusted roots, so this does not enable an
arbitrary host fallback. A consumer that manually adds to those variables is
therefore making the same explicit trust decision. `CMAKE_PROGRAM_PATH` is not
admitted as a target root; native build tools remain available through program
lookup.

A full native managed bundle supplies both contexts, so a consumer normally
needs no profile arguments:

```bash
lxtc conan install . --build=missing --output-folder=build/target
```

The `.run` installer creates `default` and `lxtc-build` below its dedicated
Conan home without invoking Conan. `--conan-cppstd VALUE` overrides only the
target profile. `--conan-build-profile NAME_OR_PATH` replaces the generated
build context explicitly; it is an escape hatch for a consumer that chooses to
own that context. This bundle behavior relies on high-level managed production
being native and is intentionally not added to generic low-level bindings.

Release builds should use a dedicated Conan home and rebuild or otherwise
establish provenance for every target package. A cached package built with a
newer libc, another target architecture or a different C++ runtime can raise
the final artifact's requirements even when the source compilation uses the
binding.

## Native managed production

High-level managed setup is native-only. Run the x86-64 workflow on an x86-64
producer and the AArch64 workflow on an AArch64 producer. An AArch64 producer
builds the entire AArch64 SDK, compiler backend, Compiler Kit and runtime
without an amd64-emulated builder:

```bash
linux-toolchain setup gcc@12 \
  --arch aarch64 \
  --glibc 2.19 \
  --prefix /path/to/toolchains/gcc12-aarch64-glibc219
```

The setup command rejects a target architecture different from the producer,
and the Docker daemon must report the selected native platform. The resulting
binding then configures through any integration selected in that binding.

Feature detection must describe the target, not the build host. Prefer target
configuration options, compile/link probes and explicit cache answers. Disable
optional features whose dependency closure is unavailable for the target.

Run smoke and final consumer tests on a representative target machine. Static
configuration success alone does not qualify a target.

## Check the deployed product

Audit the complete product tree, not only the main shared object:

```bash
linux-toolchain audit \
  --policy "${BINDING}/audit-policy.json" \
  --recursive \
  /path/to/product
```

The deployment must supply the selected shared C++ runtime through a controlled
loader layout. Include those DSOs and optional plugins in the recursive audit,
then test with the intended loader, kernel, CPU baseline and process boundary.
