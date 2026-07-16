# CLI reference

[English](cli-reference.md) | [简体中文](zh-CN/cli-reference.md)

The command line has a small user-facing layer and a set of lower-level
production primitives. Run `linux-toolchain COMMAND --help` for the complete
option list and installed defaults.

## Output and exit status

Successful write commands print their primary result path on stdout. Catalogs,
diagnostics, assembly and ELF audit offer JSON where documented. Build progress
and concise child-process output go to stderr, so scripts can capture stdout
without also capturing Docker logs. Human-readable progress, status and error
labels use color when their output stream is an interactive terminal. Color is
disabled when output is redirected, `NO_COLOR` is present, or `TERM=dumb`;
JSON output never contains terminal escapes.

| Status | Meaning |
| --- | --- |
| 0 | command completed successfully |
| 1 | diagnostic or ELF-policy check completed and found a violation |
| 2 | invalid input, invalid state or operational failure |
| 130 | interrupted by the user |

Management commands use these exit-status rules. After successful state validation, a
generated launcher returns the consumer command's status unchanged;
termination by a signal uses the conventional `128 + signal` shell status.

Argument and operational errors do not expose implementation tracebacks. JSON
output is for successful reports; callers must still inspect the process status
and stderr for failures. `linux-toolchain` is the management and publication
interface. Setup generates `lxtc`; bundle installation may select another
launcher name. Internal modules are not a public API.

## Set up and use a managed toolchain

```bash
linux-toolchain setup COMPILER --glibc FLOOR [--prefix PREFIX] \
  [--work-dir WORK_DIR] [--store-dir STORE_DIR] \
  [--arch ARCH] [--integration cmake|shell|conan] \
  [--runtime libc++|gcc@VERSION] \
  [--host-glibc-floor FLOOR] [--jobs N] [--runner RUNNER] \
  [--conan-cppstd VALUE] [--conan-build-type VALUE] \
  [--conan-build-profile NAME] [--prepare-only] \
  [--no-path-instructions] [--force]
PREFIX/bin/lxtc COMMAND [ARG ...]
```

`setup` owns one machine-local managed selection below an independent prefix;
it does not read or write a consumer repository. Managed setup runs natively
on x86-64 or AArch64. The target architecture defaults to the producer
architecture and a different `--arch` is rejected. Managed AArch64 GCC and GCC
runtime selections require GCC 10 or newer. Managed GCC infers its exact
matching runtime; managed Clang requires an explicit runtime selection. The
primary integration defaults to `shell` and selects producer smoke
qualification. Every high-level setup installation carries CMake, shell, and
Conan adapters.

`--host-glibc-floor` is the independent audit ceiling for every Compiler Kit
host ELF. When omitted, setup resolves it to the target `--glibc` value. The
published compiler and helper executables must not require newer `GLIBC_*`
versions, while its binutils must have no dynamic glibc dependency. An
explicit host floor may differ from the target SDK floor. `--jobs`
controls producer parallelism but is not part of the
content-addressed SDK or managed artifact identity. CPU instruction options
such as `-march`, `-mcpu` and `-mtune` belong to the consumer build and pass
through the compiler wrapper. Setup's Conan profile options require
`--integration conan`; they configure that producer smoke run. They do not
control whether the static Conan adapter is carried in the final installation.

Builder images use Ubuntu's normal archive mirrors when
`LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT` is unset or empty. Set it to a timestamp in
`YYYYMMDDTHHMMSSZ` form to select an Ubuntu snapshot. The source-checkout Make
wrapper exposes the same setting as `UBUNTU_SNAPSHOT`. The value changes the
builder identity and must remain the same across separate producer commands
that build, export or validate one prepared state.

`WORK_DIR` stores one strict format-1 setup selection and its lock, binding,
smoke result and prepared state. `STORE_DIR` is a shared content-addressed
producer store for verified sources, SDK workspaces, managed build trees and
logs. Both default below the user cache directory. When omitted, `WORK_DIR` is
derived from the normalized `PREFIX` basename plus a stable short hash, so
equal basenames at different paths remain independent. Work-directory
selections are immutable, while matching store identities can be reused across
selections. `PREFIX` is the final self-contained installation and is required
for normal setup. An existing prefix must be empty or contain the same
validated selection. `--force` authorizes repair or replacement only for
matching generator-owned selection outputs. It reuses already-valid immutable
producer artifacts instead of deliberately rebuilding them.

`--prepare-only` completes producer validation and prints the
`state/prepared.json` path without publishing an installation or printing
launcher PATH instructions. With an explicit `--work-dir`, this mode may omit
`--prefix`. It is the setup phase used before packaging directly from prepared
artifacts. The prepared state is qualified only while its format-1 passed smoke
result matches the current binding and selected integration.

On success, progress and child output go to stderr and stdout contains only the
launcher path. The stderr handoff includes a command for the current shell and
direct append commands that persist the launcher's directory in `~/.bashrc` or
`~/.zshrc`. The launcher depends only on the installed prefix, not Python, the
management command or `WORK_DIR`.
`--no-path-instructions` omits this human handoff while preserving the launcher
path on stdout for command composition.

The launcher does not search the current directory or its parents for
configuration. It loads the installed binding environment and executes the
remaining argument array unchanged. Every high-level installation selects a
dedicated Conan home, generated target profile, and generated managed-native
build profile. Their effective paths are available as
`LINUX_TOOLCHAIN_CONAN_HOST_PROFILE` and
`LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE`; command-line profile arguments still
take precedence.

## Create and install a single-file bundle

```bash
linux-toolchain bundle create \
  --config SETUP.json [--state-directory STATE] --output INSTALLER.run \
  [--id ID] [--force]
linux-toolchain bundle create \
  --prefix PREFIX --output INSTALLER.run \
  [--id ID] [--force]
```

With `--config`, `bundle create` loads validated prepared state from the explicit
state directory or the `state/` directory beside `setup.json`; no installed
prefix is required. With `--prefix`, it validates an existing setup
installation. Both paths derive the SDK, Compiler Kit, runtime, variant and
integrations, reuse the producer-validated binding as a relocatable template,
and write the same deterministic shell-based installer. Python is required on
the producer path only.

The low-level release interface is intentionally separate:

```bash
linux-toolchain bundle create-artifacts \
  --sdk SDK --compiler-kit COMPILER_KIT --runtime RUNTIME \
  --lock LOCK --variant VARIANT --output INSTALLER.run \
  [--id ID] \
  [--integration cmake|shell|conan ...] \
  [--conan-cppstd VALUE] [--conan-libcxx VALUE] \
  [--conan-build-type VALUE] [--force]
```

`bundle create-artifacts` accepts an explicitly assembled managed combination
and performs the same producer validation. With no `--integration` option it
selects all three adapters; explicit selections retain the low-level control.
The installed toolchain contains a consumer launcher but no Python runtime or
management CLI. Its launcher is named `lxtc` until installation.

Install the generated file directly:

```bash
./INSTALLER.run --prefix PREFIX \
  [--launcher-name NAME] [--conan-home PATH] \
  [--conan-cppstd VALUE] [--conan-build-profile NAME_OR_PATH]
```

The shell installer requires Linux, the recorded host architecture and minimum
glibc, a POSIX shell, and common Unix archive tools. `--launcher-name` selects
the installed command name. A Conan-capable bundle defaults to
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`, using the first 16 hexadecimal
characters of the bundle ID's SHA-256 digest, and generated
`default`/`lxtc-build` profiles.
`--conan-cppstd` overrides only the target profile; when omitted, the profile
contains the compiler default modeled by Conan 2 for the managed compiler
family and major. `--conan-build-profile` explicitly replaces the generated build
context; it cannot point back to the generated `lxtc-build` selector itself.
The Conan home and installation prefix cannot contain one another. Each bundle
must be installed into an absent or empty `PREFIX`; install a new bundle into a
new prefix.

Successful installation prints only the launcher path on stdout. On stderr it
prints commands for the current shell and direct append commands for
`~/.bashrc` and `~/.zshrc`. The launcher loads the installed binding shell
environment, exports the selected Conan environment when present, executes the
consumer command array unchanged, and returns its status.

Inspect an installed bundle without invoking a consumer command:

```bash
lxtc info
```

The command prints stable `key=value` lines for the bundle and variant IDs,
installation prefix, compiler, target triplet and sysroot, libc floor, C++
runtime, integrations, CMake toolchain and current Conan home/profile
selection. It requires only the installed bundle. Use `lxtc -- info` when a
consumer executable literally named `info` must be invoked.

## Environment diagnostics

```bash
linux-toolchain doctor --workflow sdk
linux-toolchain doctor --workflow managed
linux-toolchain doctor --workflow external
linux-toolchain doctor --workflow consumer
linux-toolchain doctor --workflow consumer --integration shell
linux-toolchain doctor --workflow consumer --integration conan
linux-toolchain doctor --workflow managed --summary
linux-toolchain doctor --workflow all --json
```

The consumer workflow checks CMake prerequisites by default. Repeat
`--integration` to check the executable prerequisites for `cmake`, `shell`, or
`conan`. These diagnostics do not build a consumer or qualify a release.

Each workflow classifies tools as required or optional. Docker is not a required
external-binding or consumer dependency. `all` is deliberately conservative and
requires every production capability. Managed GCC and LLVM source acquisition
uses verified release archives and does not require host Git.

`--summary` prints only `==> doctor: PASS` when all required checks pass. If a
required check fails, it prints the full report so the failure remains
actionable. Without `--summary`, human-readable output remains detailed.

## SDK commands

- `sdk list` lists pinned glibc recipes and architecture support.
- `sdk create` resolves, renders, builds and exports an SDK in one command.
- `sdk render` emits a reviewable workspace without starting Docker.
- `sdk build` builds a rendered workspace.

`amd64` and `arm64` are accepted CLI aliases and normalized to `x86_64` and
`aarch64` in manifests. The public SDK is always `WORKSPACE/sdk`; the sibling
`toolchain/` is private producer state.

`sdk list --json` emits `linux-toolchain-sdk-catalog` format 1; catalog rows
are in the `recipes` array. Each row records the backend version in the exact
`crosstool-ng` field.

## Import a runtime overlay

- `runtime import-gcc` filters and validates a GCC target runtime prefix. It
  requires the target glibc floor and architecture plus license evidence;
  `--probe-gxx` proves an externally built prefix when no managed provenance
  exists.
- `runtime import-llvm` filters libc++, libc++abi, libunwind and compiler-rt
  from an LLVM prefix. It requires an exact LLVM version, target triplet,
  architecture, glibc floor and either managed `--provenance` or an external
  `--probe-clang`.

LLVM runtime imports always publish and validate both shared and static
libraries. Both commands publish relocatable runtime artifacts without compiler
executables; binding creation performs the final dynamic and static
compiler/runtime link probes.

## Create a binding

- `bind external` binds a host-managed GCC or Clang. It requires `--runtime` or
  an explicit development-only `--allow-unpinned-runtime` choice.
- `bind managed` binds already produced managed artifacts. The selected lock
  variant determines whether the actual runtime is libc++ or libstdc++.

Both commands accept repeatable `--integration cmake|shell|conan`. With no
selection they generate CMake and shell integrations; Conan is opt-in.
`--conan-cppstd`, `--conan-libcxx` and `--conan-build-type` configure only the
Conan host profile and are valid only when that integration is selected. They
do not add flags to direct wrapper, CMake or shell invocations. Omitting
`--conan-cppstd` writes the compiler default modeled by Conan 2 for the bound
compiler family and major. Binding commands do not
select a Conan build-context profile; generic bindings may be cross-targeted.
The managed-native build profile is assembled only by a full bundle.

Bindings write `binding.json` with schema `linux-toolchain-binding` and format
1. Its C++ runtime records the actual runtime kind, while the `integrations`
object records only rendered adapters. Consumer build type and Conan vocabulary
are not part of the binding format.

## Managed build commands

The normal path is:

```bash
linux-toolchain managed catalog
linux-toolchain managed lock --spec SPEC.json --output managed.lock.json
linux-toolchain managed artifacts --lock managed.lock.json
linux-toolchain managed assemble \
  --lock managed.lock.json \
  --variant VARIANT_ID \
  --sdk-workspace SDK_WORKSPACE \
  --compiler-backend-workspace COMPILER_BACKEND_WORKSPACE \
  --workspace MANAGED_WORKSPACE \
  --output BINDING
```

`assemble` derives the Compiler Kit and runtime IDs from the variant. It reuses
matching artifacts only after validation, and the same invocation may be run
again after interruption. A new workspace is required when the selected
artifact, source, SDK, compiler backend or target-tool inputs change.
`--rebuild` recreates matching generator-owned artifact workspaces;
`--force` separately authorizes replacement of a generator-owned binding. Its
repeatable `--integration` option
has the same CMake-plus-shell default as the binding commands. When Conan is
selected, use `--conan-cppstd`, `--conan-build-type` and, when needed,
`--conan-libcxx libstdc++|libstdc++11|libc++`. The pinned runtime determines the
default and rejects incompatible values.

`managed render`, `fetch`, `build` and `publish-runtime` are lower-level
primitives for distributed execution and review gates. `managed render`
requires the SDK, that SDK workspace's `toolchain/bin` as `--target-tools`, and
`--compiler-backend-workspace` explicitly.
`managed fetch` is optional: `managed build` verifies or acquires a missing
source itself. `--jobs` on `build` is an execution option and may change between
matching resumptions. `managed publish-runtime` reads the raw managed build
output from its required `--artifact-dir`.

`managed catalog --json` emits
`linux-toolchain-managed-release-index` format 1 with a `releases` array.
`managed artifacts --json` emits
`linux-toolchain-managed-lock-artifacts` format 1 with `compiler_kits`,
`runtimes` and `variants` arrays. `managed assemble --json` emits
`linux-toolchain-managed-assembly` format 1.

## Validate a consumer and deployment

- `smoke` builds the installed C++/ASM integration project, audits its outputs,
  checks the dynamic-loader closure and runs it with eager symbol binding. Its
  integration choices are `cmake`, `shell` and `conan`; `--build-type` belongs
  to this consumer build rather than to the binding and defaults to `Release`.
  Native execution with an AArch64 glibc older than 2.36 uses an unprivileged
  user and mount namespace so the kernel enters the SDK loader through the
  declared interpreter without changing the host filesystem.
  The shell mode uses the packaged Make consumer. A successful run writes
  `result.json` with schema `linux-toolchain-smoke-result` and format 1.
- `audit` applies a binding's ELF policy to one or more files or a recursive
  deployment tree. `audit --json` emits
  `linux-toolchain-elf-audit-report` format 1.
- `conan settings` writes the settings extension required by generated Conan
  host profiles.
