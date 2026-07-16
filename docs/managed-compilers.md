# Managed compilers

[English](managed-compilers.md) | [简体中文](zh-CN/managed-compilers.md)

Managed mode builds a compiler and target runtimes from exact upstream source
pins. A complete managed toolchain combines:

1. a glibc SDK for the target ABI floor;
2. a Compiler Kit for one exact compiler and target architecture;
3. one or more target runtime overlays;
4. a binding that validates and connects those artifacts.

The pinned compiler backend used to produce these artifacts is not part of the
Compiler Kit. Target tools are explicit inputs and are never discovered from
the producer host `PATH`.

## Current support

- Producer platforms and Compiler Kit hosts: native `linux/amd64`/x86-64 and
  `linux/arm64`/AArch64.
- Targets: x86-64 and AArch64; the target must match the producer architecture.
- Managed AArch64 GCC and GCC-runtime selections require GCC 10 or newer.
- GCC: the exact same GCC release supplies libstdc++ and libgcc. Managed x86-64
  production also installs libquadmath's public headers and static/shared
  libraries; managed AArch64 production disables that unsupported component.
- Clang: carries same-release LLVM libc++ and one exact GCC libstdc++/libgcc
  runtime. The GCC provider defaults to `gcc@12`.
- Compiler Kit host glibc floor and target glibc floor are independent.

A catalog entry proves that a combination is modeled and pinned. Release
qualification still requires a real build, target-like execution and consumer
evidence.

## Set up a managed toolchain

```bash
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --cmake-version 3.31.12 \
  --integration conan \
  --work-dir /var/tmp/linux-toolchain/gcc12-glibc219 \
  --store-dir /var/tmp/linux-toolchain/store \
  --prefix /opt/linux-toolchain/gcc12-glibc219
```

Managed setup runs natively on x86-64 or AArch64 and defaults the target to the
producer architecture. A different `--arch` is rejected. GCC selects its
same-release runtime automatically. Clang includes both supported C++ runtime
families; `--libstdcxx gcc@VERSION` selects its libstdc++ provider and defaults
to `gcc@12`.

`--host-glibc-floor` selects the independent Compiler Kit host policy. When it
is omitted, high-level setup resolves it to the target `--glibc` value. The
resolved value applies recursively to every host ELF in managed GCC or Clang,
including helper executables and vendored libraries. Compiler Kit binutils are
required to be static host ELF with no glibc dependency.

The same resolved host floor and native architecture select the supplemental
build-tools artifact. CMake 3.31.12 is the default;
`--cmake-version 3.31.10|3.31.11|3.31.12` selects another pinned release.
CMake, CTest, CPack, GNU Make, and Ninja are built for that host floor with
static C++ dependencies. ccache is the matching architecture's static-musl
release and remains disabled as a compiler launcher until the consumer enables
it explicitly.

The primary integration defaults to shell and may be `cmake`, `shell`, or
`conan`. It selects the producer verification path. High-level setup renders CMake,
shell and Conan adapters together, without requiring Conan merely to render or
install those static files. Primary Conan verification still records its producer
Conan home and native build profile in machine-local prepared state.
Conan host-profile settings are static adapter configuration and may be chosen
with any primary verification. Only Conan verification invokes Conan and accepts a
producer-native build-profile selection.
Native producer verification for an AArch64 glibc older than 2.36 requires enabled
unprivileged user namespaces plus the host `unshare` and `mount` tools.

The three producer paths have separate roles:

- `--work-dir` owns one immutable selection and its prepared validation state;
- `--store-dir` owns shared content-addressed SDKs, build tools, verified
  sources, managed build trees and logs;
- `--prefix` is the final self-contained installation.

`--jobs` controls execution parallelism and is not part of a cached SDK or
managed artifact identity. Matching store entries can be reused by different
selections. Changing only `--jobs` is allowed in the same work directory and
retains matching prepared state and producer outputs. High-level `--force`
repairs or replaces only matching generator-owned selection outputs; it reuses
already-valid immutable producer artifacts rather than deliberately rebuilding
them.

Builder-image reuse is independent of those filesystem paths. The SDK,
build-tools, and managed builders first look for an image with the exact builder
identity in the active Docker daemon; deleting a work directory or `out/` does
not remove that image. These roles execute the same target from one packaged
Dockerfile. That image installs the complete producer dependency set and the
verified crosstool-NG release once, so SDK, build-tools, compiler and runtime
builds do not repeat `apt update` or package installation.
The default package-source mode uses Ubuntu's normal archive mirrors. Setting
`LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT` to a timestamp such as
`20260701T000000Z`, or passing the same value as Make's `UBUNTU_SNAPSHOT`,
selects that Ubuntu snapshot for the shared image. This selection is part of the
builder identity. The resolved immutable image ID is recorded as provenance.
Live mirrors avoid the slower snapshot service but can resolve newer package
bytes after the daemon cache is lost; use a snapshot when that package-level
repeatability is required.

Changing Dockerfile inputs, the package-source selection or platform creates a
different image identity. Pruning daemon images/cache or using an ephemeral
daemon also removes reuse. The producer store does not silently export or
import Docker images.

The installed launcher is independent of the producer work directory:

```bash
cd /home/user/workspace/project-a
/opt/linux-toolchain/gcc12-glibc219/bin/lxtc make release
```

## Catalog and lock

Inspect the installed catalog instead of hard-coding its current contents:

```bash
linux-toolchain managed catalog
linux-toolchain managed catalog --json
```

A selector is an exact release or an unambiguous major version. Resolution
records the exact official GCC or LLVM release-archive URL and SHA-512.
Unknown, ambiguous and unpinned releases fail.

The strict `linux-toolchain-managed-spec` format 1 describes the build
platform, Compiler Kit host, targets, compilers and runtime choices. Resolve it
to a deterministic lock and inspect the resulting artifact graph:

```bash
linux-toolchain managed lock \
  --spec examples/managed/compiler-matrix.json \
  --output out/managed.lock.json
linux-toolchain managed artifacts --lock out/managed.lock.json
```

The `linux-toolchain-managed-lock` format 1 contains exact source identities,
logical Compiler Kit and runtime IDs, and every valid variant. It contains no
timestamp or local path. Its Compiler Kit collection includes each variant's
selected compiler and the provider compiler for every locked runtime, allowing
one provider build tree to publish both artifacts. Use IDs emitted by the lock;
do not construct them in build scripts.

## Build one usable toolchain

Managed setup prepares one complete GCC 9.5 compiler-backend workspace for the
native producer architecture and selected Compiler Kit host floor, and reuses
it for the build-tools artifact and managed GCC and Clang builds. A normal
low-level `sdk create` builds a compiler-independent SDK plus the separately
owned target tools (binutils and Mold) in its workspace; it does not create a
compiler backend. When the target SDK has the same architecture and glibc floor
as the backend, setup builds that complete workspace once and also uses it as
the target SDK input. Otherwise the two native workspaces remain separate and
are content-addressed in the producer store.

`managed assemble` builds and validates missing Compiler Kit and runtime
artifacts, publishes a runtime set and creates the binding. Existing artifacts
are reused only when their locked artifact selection, manifests and producer
inputs match. A GCC Compiler Kit and its matching GCC runtime use one GCC build
tree. A Clang Compiler Kit and its matching LLVM runtime likewise use one LLVM
build tree and one container execution. The lock includes the Compiler Kit for
every runtime provider. A Clang assembly therefore builds its selected GCC
provider Kit and runtime together when they are both missing, retains the Kit
in the producer store, and places only the runtime in the Clang bundle. A later
GCC assembly reuses both outputs without compiling GCC again. Runtime
components and build trees are content-addressed across variants.

Managed GCC and LLVM outputs use release optimization without retaining
build-tree DWARF in published compiler or runtime artifacts. This does not
change consumer debug flags: applications may still use `-g` and produce their
own debug information. After an interruption, run the same command again;
matching producer work is reused after validation.

## Low-level build commands

Use the individual commands only when acquisition, building, publication and
binding need separate execution or review boundaries:

- `managed render` records the lock artifact, SDK, target tools from that SDK
  workspace, pinned compiler backend and builder inputs in a local workspace;
- `managed fetch` optionally prefetches and verifies the selected source;
- `managed build` verifies or acquires a missing source, prepares the builder
  image and runs the compiler build;
- `managed publish-runtime` converts a raw runtime build into a validated GCC
  or LLVM runtime overlay;
- `bind managed` validates the complete combination and renders the requested
  consumer integrations.

See the [CLI reference](cli-reference.md#managed-build-commands) for exact
options. A raw runtime build is not a binding input until
`managed publish-runtime` succeeds.

`managed fetch` is not required before `managed build`; the build command
acquires and verifies a missing source archive itself. GCC and LLVM use the
same content-addressed download and SHA-512 verification path; host Git is not
part of managed source acquisition.

Build parallelism belongs to `managed build --jobs` and may change between
matching resumptions. Jobs do not change the content-addressed producer
identity.

## Publication and binding checks

Compiler Kit publication recursively validates the architecture and glibc
needs of every host ELF, the static/no-dynamic-dependency property of its
declared binutils, driver target, vendored DSOs, licenses and manifest.
Runtime publication validates the target and ABI floor, ELF and archive
contents, dynamic dependency closure, symlinks, paths, licenses and source
evidence. An LLVM publication always contains and validates both shared and
static libc++, libc++abi and libunwind libraries. Managed libc++ publication
also requires `libc++experimental.a` for every supported LLVM release. It is an
available opt-in library, not a binding default: no enabling macro or link
option is injected for consumers. Publication validates the complete output and
its final location, with rollback on failure. Stable replacement is coordinated
by the managed lease/state-lock flows; arbitrary external filesystem readers do
not receive a lock-free hot-replacement guarantee.

A managed binding must match the lock variant, SDK, Compiler Kit and published
runtime set. It rejects target or ABI-floor mismatches, different GCC compiler
and runtime releases, and different Clang and LLVM runtime releases. GCC
bindings use their matching libstdc++/libgcc. Clang bindings expose both
libstdc++/libgcc and libc++/compiler-rt/libunwind through Clang's native
`-stdlib`, `--rtlib` and `--unwindlib` options. A dual-runtime binding has an
installed default of libstdc++. For commands launched through `lxtc`, the C++
wrapper applies the effective runtime selection at the driver layer, so
replacing consumer CMake flags cannot erase it. A direct wrapper invocation
without `LINUX_TOOLCHAIN_CXX_RUNTIME` uses the installed default. Consumer
arguments still pass through unchanged, so a later explicit runtime option
retains normal Clang precedence. Repeating the selected `-stdlib` remains a
normal driver option and does not produce an unused-argument warning. Each
option retains its native scope: `-stdlib` alone does not also change the
compiler runtime or unwind library.
Publication validates dynamic, shared-library and fully static C++ links for
each available C++ runtime.

Managed LLVM runtimes use Clang's Linux architecture-specific resource layout,
so compiler-rt lookup does not depend on the target vendor spelling. A Clang
Compiler Kit must report its recorded target when no target option is present.
Explicit target options from command lines, Clang configuration files and
response files remain ordinary consumer input and are interpreted by Clang.
A compatible Linux target spelling that changes only equivalent triple fields
is validated; an incompatible architecture, operating system or ABI fails
naturally. Managed GCC keeps the target configured into its target-specific
driver and does not accept Clang's `--target` option.

Every managed Compiler Kit publishes BFD, Gold and Mold 2.41.0. A Clang
Compiler Kit also publishes LLD. BFD remains the default. Binding validation
exercises every published linker through the driver. Clang and GCC 12 or newer
select Mold with `-fuse-ld=mold`; GCC 10 and 11 bindings expose `cc-mold` and
`c++-mold`, whose fixed `-B` selector avoids unsupported argument rewriting.

## Publish a single-file bundle

Prepared setup state can be packaged without first publishing an installation
prefix:

```bash
python3 -m pip install .
linux-toolchain setup gcc@12 \
  --glibc 2.19 \
  --work-dir out/work/gcc12-glibc219 \
  --store-dir out/store \
  --prepare-only
linux-toolchain bundle create \
  --config out/work/gcc12-glibc219/setup.json \
  --state-directory out/work/gcc12-glibc219/state \
  --output out/linux-toolchain-VARIANT_ID.run
```

Bundle creation validates prepared state, reuses its validated binding as a
template and streams the selected portable artifact trees and build tools into
the installer. An existing matching installation may instead be supplied with
`--prefix`. `bundle create-artifacts` is the advanced entry point for
independently assembled SDK, build-tools, Compiler Kit, runtime and lock
inputs. Prepared state remains qualified only when its format-1 verification
result has status `passed` and matches the recorded binding and selected
integration.

Installation needs no host Python, Docker, Conan, CMake, Make, Ninja, or
ccache:

```bash
./linux-toolchain-VARIANT_ID.run \
  --prefix /opt/linux-toolchain/VARIANT_ID \
  --launcher-name gcc12
/opt/linux-toolchain/VARIANT_ID/bin/gcc12 make release
```

The installation prefix must be absent or empty. The launcher defaults to
`lxtc`; `--launcher-name` changes it at installation time. Omitting `--prefix`
uses the bundle's embedded selection name below
`$HOME/.local/lib/linux-toolchain/`; renaming the `.run` file does not change
that name. A full bundle also accepts `--conan-home PATH`,
`--conan-cppstd VALUE`, and `--conan-build-profile NAME_OR_PATH`. The default
home is
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`, using the first 16 hexadecimal
characters of the bundle ID's SHA-256 digest; installation writes both target `default` and
managed-native `lxtc-build` profiles there. A build-profile name override is
resolved in that dedicated home and may be created later. Omitting
`--conan-cppstd` writes the compiler default modeled by Conan 2 for the managed
compiler family and major. Installation writes static configuration only and
invokes none of these consumer tools.

For a Clang bundle with both C++ runtimes, use `lxtc runtime set libc++` to
persist a choice, `lxtc runtime reset` to restore the installed default, or
`lxtc --runtime libstdc++ COMMAND` for one command. `lxtc info` reports both
the installed default and the effective command selection. `lxtc conan-init`
only recreates missing static Conan configuration. The default Conan host and
build contexts follow the launcher selection; an explicit install-time Conan
build profile does not. `conan-init`, `runtime set`, and `runtime reset`
refresh the Conan-home `lxtc.info` snapshot.

## Release qualification

Unit tests cover parsing and deterministic state transitions; they do not
qualify a compiler combination. Each published combination requires real SDK,
build-tools, Compiler Kit and runtime builds on its declared architecture,
binding verification, recursive ELF and loader-closure validation, and
representative consumers on the declared minimum host and target environments.
Embedded uses such as JVM/JNI also need a real host-process loading test.
