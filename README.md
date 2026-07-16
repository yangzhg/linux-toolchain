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
- External compilers: GCC 10+ or Clang 16+.
- Managed compilers: exact GCC and LLVM releases listed by
  `linux-toolchain managed catalog`.
- Managed compiler hosts: native `linux/x86_64` and `linux/aarch64`. The
  managed target must match the producer architecture.

A catalog entry means that the inputs are modeled and pinned. It does not mean
that every compiler, runtime, architecture and glibc combination has completed
release qualification. See the
[compatibility boundaries](docs/compatibility.md).

## Producer requirements

Creating an SDK or managed toolchain requires:

- Linux on an x86-64 or AArch64 host;
- Python 3.10 or newer and the `linux-toolchain` command;
- `readelf`, a non-root user and a local Linux Docker daemon;
- network access for source acquisition and builder-image creation;
- CMake and Make for the standard smoke workflow;
- `unshare`, `mount`, and enabled unprivileged user namespaces when validating
  an AArch64 SDK with glibc older than 2.36;
- Conan 2 only when the Conan smoke path or a Conan consumer is actually run.

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
selects the primary producer smoke path and defaults to `shell`; high-level
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
  RUNTIME=libc++ \
  INTEGRATION=cmake \
  PREFIX="$HOME/.local/lib/linux-toolchain/clang22-glibc219"
```

Set `LINUX_TOOLCHAIN_GNU_MIRROR` to select the base URL used to acquire GNU
source archives. The producer verifies and caches all crosstool-NG source
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
selects that adapter for producer smoke qualification and enables its
smoke-specific setup options; it is not required merely to carry the adapter
in a high-level installation.

When `linux-toolchain` is already installed, the direct command is:

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219-x86_64"
```

`PREFIX` is the self-contained installed toolchain. `--work-dir` owns one
immutable setup selection: `setup.json`, the resolved lock, binding, smoke
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
GCC selects its matching runtime automatically; Clang requires `--runtime
libc++` or `--runtime gcc@VERSION`.
The managed libc++ runtime publishes both shared and static libc++, libc++abi
and libunwind libraries, and validates normal and fully static C/C++ links.

The Compiler Kit host floor is independent from the target SDK floor. For the
high-level workflow, omitting `--host-glibc-floor` makes it follow `--glibc`.
For example, `--glibc 2.19` requires every published managed compiler host ELF
to need no newer than `GLIBC_2.19`; the bundled binutils must be static host
ELF and have no glibc dependency. Pass `--host-glibc-floor` only when the two
policies intentionally differ.

One work directory and one installed prefix each represent an immutable
selection. Use new paths when the compiler, target, runtime, integration or
policy changes. A producer store may be shared by many selections; matching
content-addressed inputs are validated and reused. `--force` authorizes repair
or replacement of matching generator-owned selection outputs; already-valid
immutable producer artifacts are reused rather than deliberately rebuilt.
Prepared state is qualified only by a format-1 passed smoke result that still
matches the current binding and selected integration.

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

Install the bundle into an absent or empty prefix:

```bash
./out/linux-toolchain-gcc12-glibc219-x86_64.run \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219" \
  --launcher-name gcc12

export PATH="$HOME/.local/lib/linux-toolchain/gcc12-glibc219/bin:$PATH"
gcc12 make release
gcc12 info
```

The launcher name defaults to `lxtc`. Installing a bundle does not invoke or
require Python, Docker, Conan, CMake, Make, a source checkout or network
access. The host must satisfy the recorded architecture. It must also satisfy
the Compiler Kit host glibc floor and, when the default lxtc Conan build
profile is used, the target glibc floor. `lxtc info` (or the selected launcher
name followed by `info`) prints the installed compiler, target, libc, C++
runtime, integration and Conan selections as stable `key=value` lines.

A normal high-level bundle creates a dedicated Conan home named
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>` using static files from the installer.
`BUNDLE_DIGEST` is the first 16 hexadecimal characters of the bundle ID's
SHA-256 digest. Its
`default` target profile and `lxtc-build` build profile both delegate to the
installed managed toolchain. Override the home or the target C++ standard
without running Conan during installation:

```bash
./out/linux-toolchain-gcc12-glibc219-x86_64.run \
  --prefix "$HOME/.local/lib/linux-toolchain/gcc12-glibc219" \
  --conan-home "$HOME/.conan2_lxtc_gcc12" \
  --conan-cppstd gnu20
```

Omitting `--conan-cppstd` writes the compiler default modeled by Conan 2 for
the managed compiler family and major into the generated profile.
`--conan-build-profile NAME_OR_PATH` is an explicit
escape hatch: a name refers to the dedicated home and an absolute path refers
to that file. The selected override may be created later; the generated
`lxtc-build` profile remains the default.

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

Run the packaged consumer smoke project for each binding:

```bash
linux-toolchain smoke \
  --binding "${BINDING}" \
  --integration cmake \
  --build-dir out/smoke-cmake
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
