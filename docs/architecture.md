# Architecture

[English](architecture.md) | [简体中文](zh-CN/architecture.md)

`linux-toolchain` produces controlled Linux C and C++ build inputs for an
explicit glibc ABI floor. It does not manage consumer repositories or replace
their build systems.

The product has four artifact layers:

1. a glibc SDK;
2. a Compiler Kit in managed mode (external mode references a machine-local
   compiler installation instead);
3. a target runtime overlay;
4. a binding that joins the selected inputs and renders consumer integrations.

A sysroot controls libc-facing inputs. It does not by itself pin libstdc++,
libgcc, libc++, compiler-rt, compiler headers or compiler CRT objects.

## Operating modes

```text
external mode

  glibc SDK -----+---- optional runtime overlay ----+---- external compiler
                 +--------------- binding ----------+

managed mode

  pinned catalog ----> deterministic lock
                              |
                   Compiler Kit + runtime
                              |
                        SDK -> binding
```

External mode inspects compiler drivers and target tools installed on the
current machine. Its binding records absolute executable paths and is
machine-local.

Managed mode builds exact compiler and runtime selections from pinned upstream
sources. Target tools come from the selected SDK workspace, not the host
`PATH`. Managed mode adds compiler-production provenance without replacing
external mode for locally managed compilers.

High-level `setup` orchestrates managed mode through three independent paths:

- the work directory owns one immutable selection, binding and validation
  state;
- the shared producer store owns reusable producer inputs and build outputs;
- the optional final prefix owns only the self-contained installed toolchain.

`--jobs` is an execution option and does not define a different producer
artifact. `--prepare-only` stops after producer validation, allowing bundle
creation without first publishing an installation prefix. Consumer projects do
not create or commit setup configuration. Matching, validated content-addressed
producer artifacts are reused. High-level `--force` repairs or replaces only
matching generator-owned selection outputs and does not deliberately rebuild an
already-valid immutable producer artifact.

High-level setup supports native x86-64 and AArch64 producer hosts. The target
defaults to the host architecture and must match it; managed cross production
is outside this workflow.

## Relocation and reuse

SDK, Compiler Kit and runtime manifests use relative payload paths and are
relocatable. Bindings deliberately contain absolute compiler and target-tool
paths; regenerate them after moving artifacts or changing machines.

An installed launcher loads the binding in its own prefix. It does not depend
on Python, the management CLI, the producer work directory or a consumer
repository layout.

Managed setup and prepared-bundle flows hold shared producer leases while they
read SDK and managed artifact identities; writers for the same identities use
exclusive leases. This coordination provides those managed flows with either
the previous stable tree or a validated replacement. It does not promise
lock-free hot-replacement visibility to arbitrary external filesystem readers.

## glibc SDK

An SDK recipe fixes:

- target architecture and CPU baseline;
- glibc and Linux UAPI releases;
- minimum Linux kernel configuration;
- crosstool-NG, compiler-backend GCC and binutils releases;
- builder image and source identities.

The builder identity includes the digest-pinned base image, packaged
Dockerfile, native platform and Ubuntu package-source selection. An empty
`apt_snapshot` selects the live Ubuntu archives; a timestamp selects the
corresponding snapshot. The resolved image ID is retained as provenance.

A normal SDK production workspace builds the sysroot and the separately owned
target binutils without building a private C/C++ compiler. Managed setup also
builds one complete, pinned GCC 9.5 compiler backend for the native producer
architecture and selected Compiler Kit host floor, then reuses it across
managed GCC and Clang builds. When the target SDK has the same architecture
and glibc floor, the complete workspace satisfies both roles and is built only
once. Source acquisition and checksum verification complete before the
network-disabled crosstool-NG container starts, and the verified archives are
reused from the producer store. Publication exports glibc headers and
libraries, Linux UAPI headers, the dynamic loader, startup objects and SDK
metadata. Target tools, compiler-owned headers, runtimes and executables are
not part of the public SDK payload.

Linux UAPI headers and the minimum kernel are separate policies. Header
availability does not guarantee that an older target kernel implements a
declared system call.

## Managed selection and Compiler Kit

A managed spec resolves catalog selectors to exact source identities and a
deterministic lock. The lock describes Compiler Kit, runtime and variant
relationships without timestamps or local filesystem paths. GCC variants use
the same exact GCC release for their runtime; Clang variants explicitly select
same-release LLVM libc++ or one exact GCC runtime.

A Compiler Kit owns the selected compiler drivers and declared target tools.
It does not own the target C++ runtime. Publication recursively validates the
architecture and glibc needs of every host ELF in the kit, plus driver target,
target-tool identity, vendored dependencies, licenses and provenance. The
declared binutils are additionally required to be static host ELF with no
dynamic-loader, shared-library or glibc-version dependency.

Managed GCC and Clang builds use the pinned native crosstool-NG compiler
backend as their C/C++ build compiler. The selected target SDK supplies the
assembler, linker and related target tools. Neither input is discovered from
the host toolchain.

## Runtime overlays

A runtime overlay is specific to a provider, target architecture and glibc
floor. It owns compiler runtime headers, CRT objects and runtime libraries, but
contains no compiler executable.

The GCC overlay contains the selected GCC and C++ headers, libgcc,
libstdc++ and related runtime inputs. The LLVM overlay contains Clang resource
inputs, compiler-rt builtins, and both shared and static libc++, libc++abi and
libunwind libraries. Publication filters unrelated compiler payloads and
validates target ELF files, archive members, SONAME and dependency closure,
symlinks, dynamic paths and licenses.

On x86-64, managed GCC production also places libquadmath's public headers and
static/shared libraries in the runtime overlay. Managed AArch64 production
disables libquadmath because GCC does not provide its GNU `__float128` API for
that target.

## Managed build boundary

Managed compiler construction records its locked artifact selection, SDK,
target tools, compiler backend, build script and builder identity. The actual
compiler build runs as a non-root user in a native `linux/amd64` or
`linux/arm64` container with networking disabled and producer inputs mounted
read-only. Docker emulation is not a production path: the daemon platform must
exactly match the selected producer platform.

Source acquisition and builder-image preparation occur before that isolated
compiler build. Source identities are verified against the managed catalog.

## Binding and consumer integrations

A binding jointly validates SDK, compiler, runtime, target architecture, ABI
floor and tool selection. It generates C and C++ wrappers, direct target-tool
links, an ELF audit policy and the selected integrations. Compiler arguments
remain ordinary compiler input and pass through to the chosen driver.

Direct CMake and shell/Make integrations are built in. Autotools and
hand-written Ninja builds can use the generated shell environment. Conan 2 is
optional. Other build systems are not claimed as native integrations.

Low-level binding commands render only their selected adapters. High-level
setup renders CMake, shell and Conan adapters together; its primary integration
selects the producer smoke path rather than reducing the installed capability
set. Carrying the Conan adapter is static and does not require a Conan
executable.

Runtime-bound binding validation includes compile and link probes for normal,
shared-library and fully static outputs, link-map inspection, ELF policy checks
and loader closure. It proves the selected build inputs; it does not
prove kernel feature availability, CPU compatibility, third-party dependency
closure or process-wide C++ runtime coexistence.

## Portable bundle

A bundle is a transport envelope for one validated SDK, Compiler Kit, runtime,
lock and binding template. It is not another artifact layer.

`bundle create` may consume an installed prefix or validated setup config and
prepared state. Prepared-state creation reuses the setup-validated binding as
the template, replaces producer paths with prefix placeholders and streams the
portable artifact trees into the archive. It neither regenerates the binding
nor requires an intermediate installation prefix. Prepared state is qualified
only while its format-1 passed smoke result still matches the recorded binding
and selected integration.

The target shell installer checks host architecture and glibc requirements,
extracts beside the destination and publishes into an absent or empty prefix.
Python and Docker are producer-only dependencies. Conan, CMake and Make are
also not installer dependencies. When the binding carries Conan, the installer
writes its strict settings plus dynamic `default` and `lxtc-build` profiles
into a dedicated `$HOME/.conan2_lxtc_<BUNDLE_DIGEST>` by default, using the
first 16 hexadecimal characters of the bundle ID's SHA-256 digest. The target profile
delegates to the installed binding. The build profile is assembled only for
the native managed bundle and uses that same controlled toolchain plus its
runtime libraries; it is not a generic low-level binding assumption. The
installer never detects a compiler or invokes Conan. An explicit build-profile
override is recorded as machine-local state.
Copy the `.run` file and install it again when changing machines or prefixes;
do not move an installed prefix. Final installation validation checks the
relocated manifests, declared paths and instantiated templates; it does not
repeat compile, link, loader or target-like smoke qualification.

## Release validation

Catalog resolution and unit tests prove that inputs are modeled and resolve
deterministically, not that the result is compatible. Every published compiler,
runtime, glibc floor and architecture combination requires a real build,
binding smoke test, ELF and loader-closure audit, and representative execution
on the declared minimum host and target environments.
