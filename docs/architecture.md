# Architecture

[English](architecture.md) | [简体中文](zh-CN/architecture.md)

`linux-toolchain` produces controlled Linux C and C++ build inputs for an
explicit glibc ABI floor. It does not manage consumer repositories or replace
their build systems.

The product has four artifact layers:

1. a glibc SDK;
2. a Compiler Kit in managed mode (external mode references a machine-local
   compiler installation instead);
3. one or more target runtime overlays;
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
                 Compiler Kit + runtime set
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

The four compiler artifact layers remain separate from the supplemental
host build-tools artifact assembled by managed setup. That artifact is native
to the same producer architecture and is stored and validated independently.

## Relocation and reuse

SDK, Compiler Kit and runtime manifests use relative payload paths and are
relocatable. Bindings deliberately contain absolute compiler and target-tool
paths; regenerate them after moving artifacts or changing machines.

An installed launcher loads the binding in its own prefix. It does not depend
on Python, the management CLI, the producer work directory or a consumer
repository layout.

Managed setup and prepared-bundle flows hold shared producer leases while they
read SDK, build-tools, and managed artifact identities; writers for the same
identities use exclusive leases. This coordination provides those managed
flows with either the previous stable tree or a validated replacement. It does
not promise lock-free hot-replacement visibility to arbitrary external
filesystem readers.

## Trust and validation boundaries

Validation is concentrated where data or files cross an ownership boundary.
CLI and environment input, public JSON, downloaded archives, external
compilers, host capabilities, user-selected paths and artifact trees loaded
from disk are not accepted merely because they have the expected name.
Depending on the boundary, the project checks strict schemas, pinned source
digests, path containment, symlink safety, generator ownership, selection and
provenance identity, ELF architecture and glibc needs, dynamic-path closure,
and real compiler or linker behavior.

Catalog constants, deterministic values derived from them and typed immutable
objects that have already passed their domain validation may be trusted within
the same operation. A final-path publication validator returns that validated
object to its caller; immediately loading and proving the same tree again adds
no confidence. Producer reuse is different: the reader first derives the
identity needed for the lease, acquires that lease, then revalidates the
selection and artifact under it. The two identity checks bind the reader to the
correct lease and close the replacement race.

Source digests identify downloaded fixed inputs. Generated SDK, Compiler Kit,
runtime, build-tools and binding trees are instead qualified by strict
manifests, structural checks, real compiler or ELF probes and atomic
publication; they do not receive a second tree hash or per-file identity
scheme. Once such a tree has passed final-path validation, later bundle
transport and installation may trust its recorded producer qualification while
checking their own architecture, layout, containment and relocation
boundaries. They do not repeat the complete compiler, link, loader and
verification matrix. Editing a published tree in place violates the
immutable-artifact contract; recreate it through the producer workflow.

License manifests are inventory evidence: they prove that the required,
non-empty source and package notices are present and that the recorded file
list matches the tree. They are not a second content-integrity system.
Consumer compiler arguments remain driver input and are not filtered as a
producer-side safety policy.

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
corresponding snapshot. SDK, build-tools, compiler and runtime builds use the
same exact builder image, which installs the producer packages and
crosstool-NG once. The resolved image ID is retained as provenance.

A normal SDK production workspace builds the sysroot and the separately owned
target tools (binutils and Mold) without building a private C/C++ compiler. Managed setup also
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
the same exact GCC release for their runtime. High-level Clang variants include
same-release LLVM libc++ and one exact GCC libstdc++/libgcc runtime, with
libstdc++ as the default.

A Compiler Kit owns the selected compiler drivers and declared target tools.
It does not own the target C++ runtime. Publication recursively validates the
architecture and glibc needs of every host ELF in the kit, plus driver target,
target-tool identity, vendored dependencies, licenses and provenance. The
declared binutils and Mold linker are additionally required to be static host
ELF with no dynamic-loader, shared-library or glibc-version dependency.
Every managed kit declares BFD, Gold and Mold; Clang kits also declare LLD.

Managed GCC and Clang builds use the pinned native crosstool-NG compiler
backend as their C/C++ build compiler. Private target tools from the selected
SDK producer workspace supply the assembler, linkers and related binary tools
that are copied into the Compiler Kit; they are not part of the exported SDK
sysroot. Neither build input is discovered from the host toolchain.

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

A runtime set groups the independently validated overlays used by one variant.
Its manifest names the default and available C++ runtimes while retaining each
overlay as a separate reusable component. A managed Clang binding therefore
uses Clang's native runtime selectors without merging GCC and LLVM payloads.

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
The Compiler Kit and matching runtime for one compiler family share a build
tree. Clang and its LLVM runtime are produced by one container execution. Every
runtime provider also has a locked Compiler Kit, so producing a GCC runtime for
a Clang variant caches the matching GCC Kit from the same build tree. The Clang
bundle does not include that Kit, while a later GCC variant can reuse it.

## Supplemental host build tools

Managed setup builds one content-addressed host build-tools artifact for the
native producer architecture and resolved Compiler Kit host glibc floor. Its
identity includes the architecture, host floor, selected CMake version, pinned
source archives, compiler backend, and build script. Parallel job count is not
part of that identity.

CMake, CTest, CPack, GNU Make, and Ninja are built from verified sources with
the same pinned native compiler backend used for managed compiler production.
Their host ELF files are recursively audited for the selected architecture and
glibc floor, including the architecture's glibc interpreter. C++ dependencies
are linked statically and dynamic paths must be relocatable. ccache is taken
from the matching official x86-64 or AArch64 static-musl release and must have
neither an interpreter nor `DT_NEEDED` dependencies. It is packaged as an
available tool but is not configured as the compiler launcher by default.

This artifact drives producer verification and is copied to `tools/` in every managed
installation and bundle. The installed launcher places `binding/bin` first,
then `tools/bin`, then the inherited host `PATH`. Build tools therefore cannot
override the managed compiler and target tools.

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
selects the producer verification path rather than reducing the installed capability
set. Carrying the Conan adapter is static and does not require a Conan
executable. Its host-profile settings are likewise static configuration; only
Conan verification owns producer-side Conan execution and native build-profile
state.

Runtime-bound binding validation includes compile and link probes for normal,
shared-library and fully static outputs, every available C++ runtime, and each
published linker. It also inspects link maps, ELF policy and loader closure.
Compiler options such as Clang's `--target`, `-stdlib`, `--rtlib`,
`--unwindlib` and `-fuse-ld` remain driver input. Validation proves the selected
build inputs; it does not prove kernel feature availability, CPU compatibility,
third-party dependency closure or process-wide C++ runtime coexistence.

## Portable bundle

A bundle is a transport envelope for one validated SDK, Compiler Kit, runtime,
build-tools artifact, lock, and binding template. It is not another artifact
layer.

`bundle create` may consume an installed prefix or validated setup config and
prepared state. Prepared-state creation reuses the setup-validated binding as
the template, replaces producer paths with prefix placeholders and streams the
portable artifact trees into the archive. It neither regenerates the binding
nor requires an intermediate installation prefix. Prepared state is qualified
only while its format-1 verification result has status `passed` and still
matches the recorded binding and selected integration.

The target shell installer checks host architecture and glibc requirements,
extracts beside the destination and publishes into an absent or empty prefix.
Python and Docker are producer-only dependencies. A host installation of Conan,
CMake, Make, Ninja, or ccache is not an installer dependency. When the binding
carries Conan, the installer
writes its strict settings plus dynamic `default` and `lxtc-build` profiles
into a dedicated `$HOME/.conan2_lxtc_<BUNDLE_DIGEST>` by default, using the
first 16 hexadecimal characters of the bundle ID's SHA-256 digest. The target profile
delegates to the installed binding. Build profiles are assembled only for the
native managed bundle and use that same controlled toolchain plus the selected
runtime libraries; the default target and build contexts follow the launcher's
runtime selection. This is not a generic low-level binding assumption. The
installer never detects a compiler or invokes Conan. An explicit build-profile
override is recorded as machine-local state and is not remapped by the
launcher.
Copy the `.run` file and install it again when changing machines or prefixes;
do not move an installed prefix. Final installation validation checks the
relocated manifests, declared paths and instantiated templates; it does not
repeat compile, link, loader or target-like consumer qualification.

## Release validation

Catalog resolution and unit tests prove that inputs are modeled and resolve
deterministically, not that the result is compatible. Every published compiler,
runtime, glibc floor and architecture combination requires a real build,
binding verification, ELF and loader-closure audit, and representative execution
on the declared minimum host and target environments.
