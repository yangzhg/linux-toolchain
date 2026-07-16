# Contributing

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING.zh-CN.md)

`linux-toolchain` controls the libc, compiler-runtime and linker inputs used by
portable Linux C and C++ builds. Changes to those boundaries need tests that
demonstrate the intended behavior and reject incompatible inputs.

## Development setup

The project supports Python 3.10 and newer and has no runtime Python
dependencies. Bootstrap an editable installation and the pinned development
tools in one command:

```bash
make bootstrap
make lint
make check
```

`make bootstrap` creates `.venv`; set `VENV=/path/to/venv` to choose another
directory. `make lint` uses the pinned Ruff from that environment when present
and checks imports, common Python errors and formatting. `make check` compiles
the Python sources and runs the complete unit-test suite. The tests do not
require Docker or network access.

## Design boundaries

Keep these layers separate:

- An SDK contains glibc, its loader and startup files, and Linux UAPI headers.
- A managed Compiler Kit contains exact GCC or Clang drivers and its declared
  target tools, but no target C++ runtime.
- A runtime export contains compiler-owned headers, CRT files, and GCC or LLVM
  runtime libraries, but no compiler executable.
- A binding joins an SDK and runtime to either an external compiler installation
  or a managed Compiler Kit, then generates compiler launchers, selected target
  tools, an audit policy and the
  selected CMake, shell or Conan consumer integrations.
- A glibc symbol floor is not a complete runtime-compatibility guarantee.

The management workflow is independent of a consumer repository.
`linux-toolchain setup` keeps its selection, binding and validation state in an
explicit producer work directory, reuses producer artifacts from a separate
store, then optionally publishes a self-contained installation below an
explicit prefix. Do not reintroduce project-root initialization, committed
consumer configuration, upward directory discovery, or a package-installed
global launcher. Work-directory and installation selections are immutable;
`--force` may rebuild or replace only matching generator-owned outputs.

Use `pathlib.Path`, type annotations, deterministic JSON, and the domain errors
from `linux_toolchain.errors`. Pass argument arrays to the process runner. Do not
construct shell command strings from user-controlled values.

## Changing a catalog

The SDK and managed compiler catalogs define which combinations the resolver
makes available; they do not by themselves define release qualification.
Adding a glibc release or SDK backend requires all of the following:

1. Pin a compatible crosstool-NG release and builder base image.
2. Pin Linux, GCC, binutils, glibc, and source archive versions and hashes.
3. Add architecture-specific constraints and minimum-kernel rules.
4. Cover catalog resolution, rendered configuration, archive verification, and
   export validation with unit tests.
5. Build the SDK and run a representative real-consumer acceptance cell before
   claiming support.

Unknown combinations must fail instead of inheriting components from another
backend family.

Adding a managed GCC or LLVM release requires an official source identity, an
exact release-archive SHA-512, selector-resolution tests, deterministic lockfile
coverage, and at least one real Compiler Kit/runtime build before the release is
described as validated. Do not add a maximum-version guard to the manifest
model; the pinned catalog controls what the generator can resolve.

## Compatibility-sensitive changes

Compiler launchers and target-tool links are compatibility inputs; changes to
their fixed flags or selection require representative compile and link tests.
Managed source acquisition, raw-runtime publication, output ownership and
artifact manifests require focused correctness tests. Run the full release
compiler/glibc matrix before publishing these changes.

ELF policy compares version needs, not version definitions. Preserve the
unconditional rejection of `GLIBC_PRIVATE`, architecture and loader checks,
and the distinction between glibc, kernel, CPU, and C++ runtime compatibility.

## Building release bundles

Build bundles from validated prepared setup state, a completed installation, or
an explicit complete set of managed artifacts. The normal source-checkout path
uses `setup --prepare-only` followed by `bundle create --config`; it does not
need an intermediate installation. A release must retain its bundle manifest,
producer-side validation output, binding smoke result, and representative
target-like consumer evidence. Unit tests do not qualify a real toolchain
matrix; run the installer and installed launcher on every published
minimum-host cell.

`make clean` removes repository-local setup work, bundle outputs, Python build
artifacts, and tool caches. It preserves `.venv` and any explicit `out/store`.
The Make workflow's default producer store is below the user cache directory,
outside `out/`, so reusable producer artifacts survive either cleanup target.
`make purge` also removes `.venv` and all of `out/`, including a producer store
explicitly placed there, but it does not traverse a default or external
producer store. Neither command removes an installation outside the repository.

## Pull-request checklist

- `make lint` and `make check` pass on every supported Python minor used by CI.
- Parser, generator, and policy changes have unit coverage.
- `git diff --check` reports no whitespace errors.
- The source tree contains no SDKs, downloaded archives, toolchains, bindings,
  credentials, or generated build output.
- A `.run` release was installed and executed on its declared minimum host.
- English and Chinese user documentation describe the same resulting behavior
  rather than the sequence of edits that produced it.
