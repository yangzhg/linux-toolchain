# linux-toolchain

[English](README.md) | [简体中文](README.zh-CN.md)

`linux-toolchain` creates Linux C and C++ build inputs for an explicit glibc
ABI floor. It can use an external GCC or Clang, or build a managed compiler
from pinned sources. Generated bindings work independently of any consumer
repository and support CMake, shell/Make and optional Conan workflows.

The project is alpha. A passing audit is build evidence, not a replacement for
testing the final product on a representative target system.

## Artifact model

The tool keeps four layers separate:

| Layer | Contents |
| --- | --- |
| glibc SDK | glibc headers and libraries, startup objects, loader and Linux UAPI headers |
| Compiler Kit | exact managed GCC or Clang drivers and target tools |
| runtime overlay | GCC or LLVM C++ headers, CRT objects and runtime libraries |
| binding | compiler launchers, target tools, audit policy and selected integrations |

A sysroot controls libc-facing inputs. It does not by itself pin libstdc++,
libgcc, libc++, compiler-rt or their headers and startup objects. Those inputs
belong to the runtime overlay.

Managed setup also produces a supplemental host build-tools artifact containing
CMake, CTest, CPack, GNU Make, Ninja, and ccache. It is not a fifth compiler
artifact layer: these programs drive consumer builds but do not own compiler or
target runtime inputs.

Two compiler modes are supported:

- **External mode** binds an existing GCC or Clang installation.
- **Managed mode** builds an exact compiler and runtime selection from the
  installed catalog. Managed bindings take all target tools from the Compiler
  Kit rather than the host `PATH`. Managed GCC and Clang builds use the same
  pinned crosstool-NG compiler backend instead of the host compiler.

See [Architecture](docs/architecture.md) for artifact ownership and reuse
rules.

## Supported scope

- Targets: Linux x86-64 and little-endian AArch64 ELF64.
- SDK catalog: pinned glibc 2.17, 2.19, and 2.23 through 2.42 entries. Run
  `linux-toolchain sdk list` for the installed catalog.
- The glibc 2.17 and 2.19 backend entries carry the upstream BZ #16381 loader
  fix for explicit `ld.so` execution of PIE main programs.
- External compilers: GCC 10+ or Clang 16+.
- Managed compilers: exact GCC and LLVM releases listed by
  `linux-toolchain managed catalog`.
- Managed compiler hosts: native `linux/x86_64` and `linux/aarch64`. The
  managed target must match the producer architecture.

A catalog entry means that the inputs are modeled and pinned. It does not mean
that every compiler, runtime, architecture and glibc combination has completed
release qualification. See the
[compatibility boundaries](docs/compatibility.md) and
[qualification ledger](docs/release-qualification.md).

## Producer requirements

Creating an SDK or managed toolchain requires:

- Linux on an x86-64 or AArch64 host;
- Python 3.10 or newer and the `linux-toolchain` command;
- `readelf`, a non-root user and a local Linux Docker daemon;
- network access for source acquisition and builder-image creation;
- `unshare`, `mount`, and enabled unprivileged user namespaces when validating
  an AArch64 SDK with glibc older than 2.36;
- Conan 2 only when Conan verification or a Conan consumer is actually run.

Run the matching diagnostic before a production build:

```bash
linux-toolchain doctor --workflow managed --summary
```

## Set up a managed toolchain

From a source checkout, the shortest workflow is:

```bash
make setup COMPILER=gcc@12 GLIBC=2.19
```

By default this creates a prefix such as
`$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64`. `INTEGRATION`
selects the primary producer verification path and defaults to `shell`; high-level
setup renders CMake, shell/Make and Conan adapters into every installation.
`JOBS` defaults to one quarter of the online CPU
count, with a minimum of one. `JOBS` controls execution parallelism and does
not create a different cached SDK or managed artifact identity. The Make
workflow keeps selection state under `out/work/` and reusable producer inputs
under `out/store/`. A normal `make clean` preserves the store; `make purge`
removes the complete repository-local output tree. Common overrides are:

```bash
make setup \
  COMPILER=clang@22 \
  GLIBC=2.19 \
  LIBSTDCXX=gcc@12 \
  CMAKE_VERSION=3.31.12 \
  INTEGRATION=cmake \
  PREFIX="$HOME/.local/lib/linux-toolchain/clang22-glibc219-gcc12"
```

Every managed setup and bundle includes CMake, CTest and CPack 3.31.12, GNU
Make 4.4.1, Ninja 1.13.2, and ccache 4.13.6 by default.
`CMAKE_VERSION=3.31.10|3.31.11|3.31.12`, or the direct
`--cmake-version` option, selects another pinned CMake release. The selection is
part of the immutable setup and build-tools identity. The other tool versions
are fixed by this release.

The build-tools artifact is native for the producer architecture: x86-64 setup
produces x86-64 tools and AArch64 setup produces AArch64 tools. CMake, CTest,
CPack, GNU Make, and Ninja are built with the same pinned compiler backend and
Compiler Kit host glibc floor used by the selection; C++ dependencies are
linked statically. ccache uses the corresponding official x86-64 or AArch64
static-musl release. It is installed in the launcher `PATH` but is not enabled
as a compiler launcher automatically.

GNU release archives are acquired from `https://mirrors.kernel.org/gnu` first,
then `https://ftpmirror.gnu.org`, and finally `https://ftp.gnu.org/gnu`. For SDK
acquisition, set `LINUX_TOOLCHAIN_GNU_MIRROR` to try another base URL before
that fallback chain. The producer verifies and caches all crosstool-NG source
archives before starting its network-disabled build container. A mirror URL is
therefore a transport choice and does not create another SDK identity.

Builder images use Ubuntu's normal archive mirrors by default. To pin Ubuntu
packages to a snapshot, set:

```bash
make bundle COMPILER=gcc@12 GLIBC=2.19 \
  UBUNTU_SNAPSHOT=20260701T000000Z
```

For direct CLI workflows, set
`LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT=20260701T000000Z` on every producer command
that creates or validates the prepared artifacts. Leaving it unset or empty
uses the normal mirrors. The selected mode is part of the builder identity, so
snapshot and live-mirror artifacts are not mixed.

The Make target shows commands for adding the launcher directory to the current
shell, Bash or Zsh configuration. The direct `linux-toolchain setup` command
also writes the launcher path to stdout. The default `shell` selection is ready
for Make and other shell-driven builds from any project directory:

```bash
export PATH="$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64/bin:$PATH"

lxtc make -j8
```

The launcher executes the supplied command with the generated compiler and
target-tool environment. It can execute any ordinary command and does not parse
or rewrite consumer arguments. `INTEGRATION=cmake` or `INTEGRATION=conan`
selects that adapter for producer verification; it is not required
merely to carry or configure the adapter in a high-level installation. Only
Conan verification invokes Conan or consumes a producer-native build profile.

When `linux-toolchain` is already installed, the direct command is:

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64"
```

`PREFIX` is the self-contained installed toolchain. `--work-dir` owns one
immutable setup selection: `setup.json`, the resolved lock, binding, verification
result and prepared-state record. `--store-dir` owns reusable SDK workspaces,
verified sources, managed build trees and logs, keyed by their producer inputs.
Both default below the user cache directory. The Make workflow overrides only
the work directory with `out/work/TOOLCHAIN_VARIANT` and the producer store
with `out/store`. Set `STORE_DIR` to an absolute shared path when reuse must
span multiple checkouts or producers. A normal `make clean` removes selections
and bundle outputs but preserves the repository-local store; `make purge`
removes it.

Managed setup builds natively on x86-64 or AArch64 and defaults the target to
the host architecture. It rejects a different `--arch`; an AArch64 producer
therefore builds the AArch64 SDK, compiler backend, Compiler Kit and runtime
from start to finish. Managed AArch64 GCC selections require GCC 10 or newer.
GCC carries the matching libstdc++ and libgcc release. A managed Clang
installation carries both libc++ from the matching LLVM release and libstdc++
from `gcc@12` by default; `--libstdcxx gcc@VERSION` or Make's `LIBSTDCXX`
selects another pinned GCC release. Clang keeps its native Linux default of
libstdc++; consumers can select libc++ with `-stdlib=libc++`, and can select
compiler-rt and libunwind with Clang's native `--rtlib` and `--unwindlib`
options. Both C++ libraries and unwind libraries are published in shared and
static form and validated with dynamic and fully static links.
Every managed libc++ overlay also publishes `libc++experimental.a`. The
binding does not enable experimental libc++ facilities or link that archive
implicitly; consumers opt in through their own compile and link options.

Managed bundles include BFD, Gold and Mold 2.41.0 linkers. Clang bundles also
include LLD. BFD remains the default. Clang and GCC 12 or newer select Mold with
`-fuse-ld=mold`; GCC 10 and 11 bindings publish `cc-mold` and `c++-mold`
drivers because those GCC releases do not accept Mold through `-fuse-ld`.
Clang also interprets consumer `--target` options normally.
A compatible Linux target spelling that changes only equivalent triple fields,
such as `x86_64-pc-linux`, keeps using the bundled SDK and runtimes; an
incompatible architecture, operating system or ABI fails during compilation or
binding validation.

The Compiler Kit host floor is independent from the target SDK floor. For the
high-level workflow, omitting `--host-glibc-floor` makes it follow `--glibc`.
For example, `--glibc 2.19` requires every published managed compiler host ELF
to need no newer than `GLIBC_2.19`; the bundled binutils and Mold linker must
be static host ELF and have no glibc dependency. Pass `--host-glibc-floor`
only when the two policies intentionally differ.

One work directory and one installed prefix each represent an immutable
selection. Use new paths when the compiler, target, runtime, integration or
policy changes. A producer store may be shared by many selections; matching
content-addressed inputs are validated and reused. `--force` authorizes repair
or replacement of matching generator-owned selection outputs; already-valid
immutable producer artifacts are reused rather than deliberately rebuilt.
Prepared state is qualified only by a format-1 verification result whose status
is `passed` and which still matches the current binding and selected integration.

## Create and install a bundle

Create a self-extracting installer from the same source-checkout workflow:

```bash
make bundle COMPILER=gcc@12 GLIBC=2.19
```

The default output is
`out/linux-toolchain-gcc12-glibc219-x86_64.run`. `make bundle` prepares and
validates the selected producer artifacts, then packages them directly; it
does not first publish the installation prefix. Override `WORK_DIR`,
`STORE_DIR`, or `BUNDLE_OUTPUT` when needed. `SETUP_OPTIONS` and
`BUNDLE_OPTIONS` pass additional arguments to the corresponding commands.
Replace an existing regular output file explicitly with:

```bash
make bundle COMPILER=gcc@12 GLIBC=2.19 BUNDLE_OPTIONS=--force
```

The equivalent bundle command consumes validated prepared state:

```bash
linux-toolchain bundle create \
  --config out/work/gcc12-glibc219-x86_64/setup.json \
  --state-directory out/work/gcc12-glibc219-x86_64/state \
  --output out/linux-toolchain-gcc12-glibc219-x86_64.run
```

An existing installed prefix is also accepted:

```bash
linux-toolchain bundle create \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64" \
  --output out/linux-toolchain-gcc12-glibc219-x86_64.run
```

Install the bundle using its generated default prefix:

```bash
./out/linux-toolchain-gcc12-glibc219-x86_64.run \
  --launcher-name gcc12

export PATH="$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64/bin:$PATH"
gcc12 make release
gcc12 info
```

Without `--prefix`, the generated installer uses
`$HOME/.local/lib/linux-toolchain/<NAME>`. The bundle embeds `NAME` when it is
created from the selected compiler, target, runtime and any non-default CMake
version, for example `gcc12-glibc219-x86_64` or
`clang19-glibc219-x86_64-gcc12`; renaming the `.run` file does not change it.
An explicit `--prefix` overrides this default. The selected prefix must be
absent or empty. The launcher name defaults to `lxtc`.

Installing a bundle does not invoke or require a host installation of Python,
Docker, Conan, CMake, Make, Ninja, or ccache, nor a source checkout or network
access. The host must satisfy the recorded architecture. It must also satisfy
the Compiler Kit host glibc floor and, when the default lxtc Conan build profile
is used, the target glibc floor.
The launcher prepends the bundled build-tools directory after the binding tools
and before the host `PATH`. `lxtc info` (or the selected launcher name followed
by `info`) prints the installed compiler, target, libc, build tools, C++ runtime,
integration and Conan selections as stable `key=value` lines.
`cxx_runtime.kind` is the installed default and `cxx_runtime.selected` is the
selection for the current launcher command.

A managed Clang bundle that publishes both C++ runtimes can persist a runtime
selection or override it for one command:

```bash
lxtc runtime show
lxtc runtime set libc++
lxtc make -j8
lxtc --runtime libstdc++ make -j8
lxtc runtime reset
```

Enter an interactive child shell to use the same environment without prefixing
each command:

```console
$ lxtc --runtime libc++ shell
LXTC shell
    compiler: clang 19.1.7
    target:   x86_64-unknown-linux-gnu
    libc:     glibc 2.19
    runtime:  libc++

  Type 'exit' to leave.
```

For a conventional prompt such as
`user@host:~/workspace/project (branch)$`, the child shell displays
`(lxtc clang-19.1.7 libc++):~/workspace/project (branch)$`. GCC omits the
fixed runtime from its label. The launcher reads the normal Bash, Zsh, or
POSIX-shell startup file, then reapplies the installed toolchain environment;
it also prepends only the selected C++ runtime library directories to that
child shell's `LD_LIBRARY_PATH`. This makes newly linked programs and native
build tools directly runnable while preserving the value established by the
user's startup files. It does not expose the SDK libc: direct execution in the
shell still uses the host loader and libc. The launcher does not edit user
startup files or modify the parent shell. An active child shell is a snapshot
of its selected runtime. Replace it with
`exec lxtc --runtime libstdc++ shell` to switch that working shell.

Use the SDK loader when a local executable should run with the selected runtime
and SDK libc closure instead of the host libc:

```bash
lxtc --runtime libc++ run ./build/app [ARG ...]
```

`run` checks the dynamic-loader closure before execution and rejects fallback
to an unselected bundled runtime or a system library directory. Neither
`shell` nor `run` adds RPATH or RUNPATH to consumer outputs; a deployed program
still needs its own controlled loader and library layout.

The persistent choice is a launcher setting shared by commands launched through
`lxtc`, whether they invoke the compiler directly or use CMake, Make, Ninja, or
Conan. It is stored below
`${XDG_CONFIG_HOME:-$HOME/.config}/linux-toolchain/<BUNDLE_DIGEST>/runtime`,
outside the immutable installation and any Conan home. `--runtime` exports a
temporary choice only to that command and its children. The C++ wrapper applies
the effective choice at the driver layer, so replacing ordinary CMake or Conan
C++ flags cannot silently remove it. Invoking `binding/bin/c++` outside the
launcher without `LINUX_TOOLCHAIN_CXX_RUNTIME` uses the installed default.
Compiler arguments still pass through unchanged and retain normal driver
precedence. On GCC bundles and other single-runtime bundles, `runtime show`
reports the only choice while switching is unavailable.

A normal high-level bundle creates a dedicated Conan home named
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>` using static files from the installer.
`BUNDLE_DIGEST` is the first 16 hexadecimal characters of the bundle ID's
SHA-256 digest. Its
`default` target profile and `lxtc-build` build profile both delegate to the
installed managed toolchain and follow the selected runtime. Override the home
or the target C++ standard without running Conan during installation:

```bash
./out/linux-toolchain-gcc12-glibc219-x86_64.run \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64" \
  --conan-home "$HOME/.conan2_lxtc_gcc12" \
  --conan-cppstd gnu20
```

Omitting `--conan-cppstd` writes the compiler default modeled by Conan 2 for
the managed compiler family and major into the generated profile.
`--conan-build-profile NAME_OR_PATH` is an explicit
escape hatch: a name refers to the dedicated home and an absolute path refers
to that file. The selected override may be created later; the generated
`lxtc-build` profile remains the default. An explicit build profile is
user-owned and is not changed by `lxtc runtime`.

Recreate its static settings and profiles:

```bash
lxtc conan-init
```

The command does not run Conan or restore package-cache content. Matching files
are reused; a different existing configuration is not overwritten. Runtime
selection remains an `lxtc` operation, independent of Conan. The generated
`lxtc.info` records the plain `lxtc info` output for the persistent selection
and is refreshed by `conan-init`, `runtime set`, and `runtime reset`; a
command-scoped override does not modify it. The Conan-home suffix itself is
only a bundle-ID digest and cannot be decoded into a toolchain configuration.

An installed prefix contains machine-local paths. To use a bundle on another
machine or under another prefix, run the original `.run` file again instead of
moving the installed directory.

## Consumer integrations

High-level setup installations and their bundles contain all three native
adapters. Lower-level binding commands still contain only explicitly selected
integrations and default to CMake plus shell.

| Integration | Generated entry point | Support |
| --- | --- | --- |
| CMake | `cmake/toolchain.cmake` | native adapter |
| shell / Make | `env/toolchain.env` | native adapter |
| Conan 2 | `conan/host.profile` | opt-in adapter |
| Autotools | shell environment and target triplet | compatible path |
| hand-written Ninja | shell environment or wrapper paths | compatible path |
| Meson / Bazel | none | no native adapter |

Without the generated launcher, use the selected binding entry point directly:

```bash
BINDING="$PWD/out/binding-managed"

cmake -S . -B build/target \
  -DCMAKE_TOOLCHAIN_FILE="${BINDING}/cmake/toolchain.cmake" \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/target

. "${BINDING}/env/toolchain.env"
make -j8
```

The full-bundle launcher selects both its generated Conan target profile and
its separate managed-native build profile. See [Consumer
integration](docs/integrations.md) for complete examples and the lower-level
binding boundary.

## Lower-level workflows

The high-level `setup` command is the normal managed workflow. Lower-level
commands are available when SDK production, compiler builds and publication
need separate execution or review boundaries.

Create an SDK:

```bash
linux-toolchain sdk list
linux-toolchain sdk create \
  --glibc 2.19 \
  --arch x86_64 \
  --workspace out/sdk-glibc-2.19
```

Bind an external compiler to an SDK and imported runtime:

```bash
linux-toolchain bind external \
  --sdk out/sdk-glibc-2.19/sdk \
  --runtime out/runtime-gcc \
  --cc "${CC}" \
  --cxx "${CXX}" \
  --output out/binding-external
```

See [Building a GCC runtime](docs/build-gcc-runtime.md) for runtime import and
[Managed compilers](docs/managed-compilers.md) for lock, build and assembly
commands.

## Validate a binding and product

Run the packaged consumer verification project for each binding:

```bash
linux-toolchain verify \
  --binding "${BINDING}" \
  --integration cmake \
  --build-dir out/verify-cmake
```

Audit the complete deployment tree:

```bash
linux-toolchain audit \
  --policy "${BINDING}/audit-policy.json" \
  --recursive \
  /path/to/product
```

The glibc floor limits public `GLIBC_*` requirements. Kernel APIs, CPU
instructions, loader configuration, dependency closure, plugins and
process-wide C++ runtime interactions remain separate deployment constraints.
Consumer options such as `-march`, `-mcpu` and `-mtune` pass through unchanged.

## Documentation

- [Documentation index](docs/README.md)
- [CLI reference](docs/cli-reference.md)
- [Architecture](docs/architecture.md)
- [Managed compilers](docs/managed-compilers.md)
- [Consumer integration](docs/integrations.md)
- [Compatibility boundaries](docs/compatibility.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Versions and artifact formats](docs/artifact-formats.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Apache 2.0 license](LICENSE)
