# SDK backend catalog

[English](recipe-catalog.md) | [简体中文](zh-CN/recipe-catalog.md)

`sdk render --glibc VERSION --arch ARCH` resolves a requested glibc release
through the pinned crosstool-NG backend catalog.

| Family | Available glibc entries | crosstool-NG | Builder platform | Linux headers | Compiler backend | binutils |
| --- | --- | --- | --- | --- | --- | --- |
| `crosstool-ng-1.28.0` | 2.17, 2.19, and 2.23 through 2.42 | 1.28.0 | `linux/amd64`, `linux/arm64` | 6.12.41 | GCC 9.5.0 | 2.45; AArch64 with glibc older than 2.26 uses 2.29.1 |

The glibc list is discrete rather than a numerical range. Both x86-64 and
AArch64 targets are modeled for each listed release. crosstool-NG requires
binutils older than 2.30 for AArch64 with glibc older than 2.26, so those
entries pin binutils 2.29.1 rather than 2.45. Use
`linux-toolchain sdk list` to inspect the catalog bundled with the installed
generator. `--arch` filters one target, while `--json` emits the stable
`linux-toolchain-sdk-catalog` format 1 document.

The target architecture selects the matching native builder platform:
x86-64 uses `linux/amd64` and AArch64 uses `linux/arm64`. crosstool-NG runs in
the selected platform from the pinned multi-platform image. The Docker daemon
must report that exact platform; architecture emulation is not a production
path.

A normal SDK build produces the glibc sysroot and keeps the target tools needed
by producer workflows as separate private state; it does not complete a
consumer C/C++ compiler. Managed setup separately builds the complete native
compiler backend used to produce managed GCC and Clang Compiler Kits. Managed
builds therefore do not select a host compiler from `PATH`. The backend is
producer machinery: it is not included in the public SDK or a Compiler Kit, and
it is not the compiler used by consumers.

The compiler backend uses the producer architecture and selected Compiler Kit
host glibc floor. High-level setup makes that floor follow the target glibc
floor unless `--host-glibc-floor` is explicit. Published managed compiler ELF
is audited against the resolved host floor, and target binutils are required
to be static so the builder-container libc cannot leak into the Compiler Kit.

An available catalog entry means that the resolver can render pinned build
inputs. It does not qualify every combination. Release qualification still
requires a real build, smoke test, ELF audit, loader-closure check and
representative target-like consumer evidence.

Linux UAPI headers are independent from the glibc floor and `minimum_kernel`.
New headers provide declarations but do not make new syscalls available on an
old kernel; optional kernel features need runtime detection and a fallback.

Extending the catalog requires a validated crosstool-NG release and exact
Linux, GCC, binutils, builder-platform, source and hash selections. The resolver
does not guess a backend for an unknown glibc release.

Architecture defaults are `x86-64` with Linux 3.2 for x86-64 and `armv8-a`
with Linux 3.7 for AArch64. A request can raise, but cannot lower, that
baseline:

```shell
linux-toolchain sdk create \
  --glibc 2.42 \
  --arch aarch64 \
  --name portable-arm64-compat \
  --minimum-kernel 4.4.0 \
  --jobs 2 \
  --workspace out/portable-arm64-compat
```

`--spec` accepts a fully explicit `SdkSpec`; `--name` and `--minimum-kernel`
remain selection overrides. `--jobs` controls this build execution and is not
serialized into the SDK specification or content identity. Explicit specs
remain subject to the pinned backend catalog and archive verification.
