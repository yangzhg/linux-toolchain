# Troubleshooting

[English](troubleshooting.md) | [简体中文](zh-CN/troubleshooting.md)

Start with the narrowest diagnostic workflow for the command you intend to run:

```bash
linux-toolchain doctor --workflow managed
```

The text report is for humans; `--json` is suitable for CI evidence. A missing
optional consumer tool does not fail a production workflow that does not need
it.

## Docker cannot be reached

SDK and managed production require a non-root user and a local Linux Docker
daemon exposed through a Unix endpoint. Remote TCP contexts are rejected because
the build uses client-local read-only bind mounts. Check the active context,
daemon permissions and server platform. External binding, audit and installed
consumer integrations do not require Docker.

## The requested glibc version is unavailable

Run `linux-toolchain sdk list --arch ARCH`. The generator accepts only reviewed,
pinned backend entries; it does not guess a toolchain configuration for an
unknown glibc release. Extend the backend with source hashes and qualification
evidence, or choose an installed catalog entry.

## The program still requires a newer GLIBCXX or GCC version

A glibc SDK controls libc-facing inputs only. Pin a GCC runtime overlay to
control libstdc++, libgcc, compiler CRT and their headers, or use a managed Clang
libc++ variant to remove the GCC runtime from the validated C++ closure. Audit
the final deployment tree, not only the main shared object.

## A managed build was interrupted

Run the same `managed assemble` command again. A completed artifact is reused
only after validation, and matching producer work can be continued. A separate
`managed fetch` is optional. Use a new workspace when the selected artifact,
source, SDK, compiler backend or target-tool input changes. Use `--rebuild` only
for a matching generator-owned workspace.

## A binding output already exists

Bindings are machine-local and contain absolute executable identities. Use a new
output after moving SDKs, runtimes or compilers. `--force` replaces only an
output that is recognized as generator-owned; it will not erase an arbitrary
directory.

## The smoke project compiles but does not run

Inspect `audit-report.json`, `loader-closure.txt` and `runtime-output.txt` in the
smoke build directory. Cross targets need an explicit runner such as
`qemu-aarch64`, but a representative target-machine run is still required for
release qualification. Confirm the target CPU and minimum kernel independently
of the glibc symbol floor.

## A selected integration file is missing

Low-level bindings contain only integrations selected when they were created;
CMake and shell are their defaults, while Conan is opt-in. High-level setup
bindings contain all three. If smoke or a consumer reports a missing
`cmake/toolchain.cmake`, `env/toolchain.env` or `conan/host.profile`, regenerate
the low-level binding with the corresponding repeated `--integration` value,
or regenerate the high-level setup binding. Do not copy integration files
between bindings because they contain paths and identity inputs for one exact
binding.

## An embedding process loads a different libstdc++

The host application or another plugin may have loaded a same-SONAME C++
runtime before the generated component. A pinned runtime controls build and
deployment evidence but does not create a separate dynamic-loader namespace.
Test the real host load path, exceptions and threads, then inspect the process
library map. A JVM loading a JNI library is one example. See the
[compatibility boundaries](compatibility.md#embedding-in-an-existing-process).

## Capturing command output

Write-command results and JSON reports use stdout. Long build progress and
Docker output use stderr:

```bash
BINDING_MANIFEST="$(linux-toolchain managed assemble ... 2>build.log)"
```

Always check the exit status before consuming the captured result.
