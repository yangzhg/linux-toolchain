# Compatibility boundaries

[English](compatibility.md) | [简体中文](zh-CN/compatibility.md)

This document defines what a generated binding and a passing ELF audit do and
do not prove. A selected version is a compatibility policy, not a claim that
every machine with that version has been tested.

## Two independent glibc floors

Managed mode uses two glibc floors with different subjects:

| Floor | Applies to | Purpose |
| --- | --- | --- |
| Compiler Kit host floor | every host ELF in the Compiler Kit, including GCC or Clang, helper executables, libraries and binutils | oldest build host on which the kit may run |
| target floor | generated executables, shared objects and target runtime overlay | highest permitted target `GLIBC_*` requirement |

The host floor does not become the product's target floor, and a low target
floor does not make a compiler executable run on an equally old build host.
Each payload is audited against its own policy.

The host floor is both a build input and a publication gate. Managed compiler
construction uses a pinned native GCC 9.5 backend built for that floor, and
publication recursively audits every resulting host ELF. Compiler Kit
binutils are statically linked and must expose no dynamic-loader,
shared-library or glibc-version dependency. The builder container's libc is
not the Compiler Kit compatibility policy.

Managed production builds native `linux/x86_64` or `linux/aarch64` Compiler
Kits. The target must match that host architecture. High-level setup defaults
the host floor to the target floor, while an explicit host floor keeps the two
policies independent.

## What `glibc-floor` guarantees

For each audited ELF consumer, the highest numeric version needed from the
`GLIBC_*` namespace must be no newer than the configured target floor.
`GLIBC_PRIVATE` is rejected because it is not a public stable ABI. Unknown
non-numeric `GLIBC_*` feature tokens are rejected rather than omitted from the
decision; `GLIBC_ABI_DT_RELR` is the one explicitly modeled token.

The calculation reads version needs, not versions defined by the binary. An
exported symbol is not a runtime requirement.

The selected SDK also supplies the glibc headers, startup objects, linker
scripts, dynamic loader and libraries used for compile and link. This controls
the target libc-facing inputs instead of relying on the build host's libc.

The architecture and loader are part of the same policy:

| Target | ELF requirements | Loader |
| --- | --- | --- |
| x86-64 | x86-64, ELF64, little-endian | `/lib64/ld-linux-x86-64.so.2` |
| AArch64 | AArch64, ELF64, little-endian | `/lib/ld-linux-aarch64.so.1` |

An ELF for the wrong architecture cannot pass merely because its symbols are
old enough. x32, AArch64 ILP32 and big-endian AArch64 are unsupported.

For floors older than glibc 2.36, `DT_RELR` and the
`GLIBC_ABI_DT_RELR` requirement are rejected. They are permitted, but not
required, at a floor of 2.36 or newer.

## What the compiler and runtime each own

A sysroot alone does not pin compiler-owned C++ inputs. The binding records one
of these runtime policies:

| Policy | Inputs controlled by the binding |
| --- | --- |
| `external-unpinned` | glibc SDK only; compiler headers, CRT and C++ runtimes remain external |
| `pinned-gcc-runtime` | GCC builtin/fixed and C++ headers, compiler CRT, libgcc, libstdc++, and audited optional components |
| `pinned-llvm-runtime` | libc++ and Clang resource headers, compiler-rt builtins, libc++abi, libunwind and libc++ |

Managed x86-64 GCC production requires libquadmath headers and static/shared
libraries in `pinned-gcc-runtime`. Managed AArch64 production does not require
that unsupported target component. A lower-level external import includes
libquadmath only when it is present in the selected GCC prefix.

An imported runtime's declared target floor may not be newer than the SDK
floor. LLVM runtime bindings require the same target floor as the SDK.

Version rules depend on ownership mode:

- an external GCC frontend and GCC runtime must match by GCC major;
- a managed GCC Compiler Kit and GCC runtime must match by exact GCC release;
- Clang with libstdc++ uses an explicitly selected GCC runtime and does not
  pretend that the Clang and GCC version numbers correspond;
- Clang with libc++ requires a managed Clang Compiler Kit and an LLVM runtime
  from the same exact LLVM release.

The managed lock enforces these relationships before a build. Managed binding
creation requires the lock and selected variant, then checks the Compiler Kit
and runtime artifact provenance again against the published manifests.

## Checks performed before publishing a runtime

A GCC runtime export filters an installed target prefix. It validates target
ELF architecture, version needs, archives and compiler CRT objects, SONAME and
DT_NEEDED values, symlink relocatability and dynamic paths. It excludes compiler
drivers, `cc1`, plugins and unrelated multilib trees. The manifest records
locations and reports for `GLIBC`, `GLIBCXX`, `CXXABI` and `GCC`.

An LLVM runtime export owns libc++ headers, Clang resource inputs,
compiler-rt builtins, libc++, libc++abi and libunwind. It validates the same
target boundaries and records exact source evidence. It publishes both linkage
forms: shared libraries are checked as an internal SONAME closure, while
`libc++.a`, `libc++abi.a` and `libunwind.a` are required and their relocatable
members are checked for the selected target. The LLVM runtime closure forbids
`libstdc++.so.6` and `libgcc_s.so.1`.

Runtime-bound wrappers add no deployment RPATH or RUNPATH. Dynamic outputs must
supply the selected shared libraries through a controlled loader arrangement
and include every deployed DSO in the recursive audit. For GCC this normally
includes `libstdc++.so.6` and `libgcc_s.so.1`, plus `libatomic.so.1` or
`libquadmath.so.0` when the final closure needs them. For LLVM it includes the
selected libc++, libc++abi and libunwind DSOs.

`external-unpinned` supports development workflows, but its `GLIBCXX`, `CXXABI`
and `GCC` requirements need an independent deployment policy. It is outside the
strict runtime-pinned isolation model.

## Compiler and target-tool selection

External binding creation asks the selected driver for target `as`, `ar`,
`ranlib`, `nm`, `strip`, `objcopy` and `objdump`. A runtime-pinned external
binding also resolves the compiler-selected linker. It records each selected
tool's invocation path as creation evidence.

Managed binding creation does not perform that discovery. It loads both
drivers and every target tool from the Compiler Kit manifest.

The generated `cc` and `c++` launchers add the selected sysroot and runtime
inputs, then forward consumer arguments unchanged. Target binary tools and the
runtime-selected linker are direct links to the selected executables; there is
no per-invocation argument parser or identity check. External bindings remain
machine-local because their paths describe one compiler installation.

## Producer validation of selected inputs

A runtime-pinned binding supplies the exact SDK sysroot, runtime directories,
startup files and selected linker. The wrapper clears compiler search-path
environment variables so the generated build environment does not depend on the shell
that invokes it. It does not classify or reject compiler, linker, response-file
or plugin arguments supplied by the consumer.

Before publication, the binding compiles C and C++ probes, exercises target
archive and binary tools, then links normal executables, shared libraries and
fully static C/C++ executables. Link-map and ELF evidence must show that libc
came from the SDK and the compiler runtime came from the selected overlay.
Static probes must have no dynamic interpreter or `DT_NEEDED` entries.

For a GCC runtime, Clang is forced to the selected GCC installation layout with
`-stdlib=libstdc++`, libgcc as the runtime and libgcc as the unwinder. For an
LLVM runtime, Clang is forced to `-stdlib=libc++`, compiler-rt and libunwind,
plus the recorded resource directory and C++ headers. The validated LLVM output
must not depend on GCC runtime SONAMEs.

The producer probes validate the generated default and static-link behavior;
release validation must also audit binaries built with the consumer's own
flags.

## Embedding in an existing process

Link-time pinning does not control an already-running process's global dynamic
loader namespace. The host application or another plugin may load
`libstdc++.so.6` or `libgcc_s.so.1` before the generated component. If that
component needs the same SONAME, the loader can reuse the already-loaded
implementation instead of the runtime selected during its build. Shipping a
second file with the same SONAME does not create an isolated C++ runtime.

This means a `pinned-gcc-runtime` binding proves the build and standalone link
closure, but it does not by itself prove which libstdc++ instance an embedding
process will use. Deployment options include controlling the process-wide
loader path, requiring a host-compatible system runtime, or isolating the native
component in a separate process. Static runtime linking may remove some
DT_NEEDED entries, but it is not a general isolation mechanism: redistribution
terms must be reviewed, symbol interposition applies, and C++ objects,
exceptions, allocations or thread-local state must not cross incompatible
runtime boundaries.

Clang with libc++ uses different primary C++ runtime SONAMEs and avoids the
direct `libstdc++.so.6` collision, but requires a carefully defined
host/component ABI boundary and complete unwind/loader validation.

A release test should load the real shared object through its actual host and
exercise at least initialization, C++ exceptions, threads,
allocation/deallocation paths and repeated load/use behavior. It should record
the process's loaded-library map. A JVM loading a JNI library is one example of
this boundary; a standalone executable smoke test cannot replace the real
host-process check.

## Runtime implementation and performance

The SDK sets an ABI ceiling; it does not ship its old glibc implementation with
the product. On a newer compatible host, the host loader supplies newer
implementations for the requested public symbols. IFUNC-selected routines such
as `memcpy`, `memmove`, `memset` and string functions can therefore still use
the implementation optimized for the deployment CPU.

The tradeoff is at the API boundary. Code built for an older floor cannot
directly require declarations or symbols introduced only by a newer glibc.
Such features need a separately modeled, runtime-detected path. Performance
also depends on the allocator, kernel, CPU and workload; the ABI audit makes no
performance guarantee.

## What these checks cannot guarantee

A passing glibc-floor audit does not prove:

- availability or behavior of kernel syscalls;
- compatibility with the selected CPU instruction stream;
- correctness of optional or dynamically loaded plugins;
- closure of libraries not included in the audited deployment tree;
- loader behavior changed by the launch environment;
- compatibility of cached target packages built with other inputs;
- runtime behavior on an old target machine, container or emulator;
- compatibility inside an existing process with preloaded native runtimes.

The binding does not set a CPU instruction baseline. Consumer options such as
`-march`, `-mcpu` and `-mtune`, source attributes, assembly and runtime dispatch
may select any instruction set supported by the compiler. That choice is
independent of the glibc ABI floor and must be reflected in the product's CPU
deployment requirements.

The default audit permits `$ORIGIN`-anchored dynamic paths and rejects plain
relative or absolute entries. Packaging must prove that every expanded
`$ORIGIN` path stays inside the shipped tree. DT_NEEDED values containing `/`
and path-valued SONAMEs are rejected.

## Validation required before release

A releasable native artifact should satisfy all of these checks:

1. Archive the SDK, compiler, runtime and binding manifests that apply to the
   selected mode.
2. Rebuild or establish provenance for every target dependency under the
   selected CMake, shell-environment or package-manager integration.
3. Recursively audit every executable, shared library and shipped runtime DSO.
4. Verify the complete loader closure with the intended deployment layout.
5. Run the small C++ shared-library/executable smoke project for each binding.
6. Test on a representative target environment for the selected kernel, glibc,
   loader, architecture and CPU policy.
7. For an embedded native component, run the real host load and behavior test
   and inspect the process-wide native-library map; JNI/JVM delivery is one
   example.

Use the small smoke project during routine development. Full consumer builds
and the host-process test belong in release qualification because they exercise
consumer-specific dependency and process behavior.

When the oldest production system is unknown, choose several available catalog
floors as explicit product policies, keep representative runtime environments
for them, and add or retire floors as deployment evidence improves. A version
catalog makes the choice reviewable; it does not discover the correct floor for
the product.
