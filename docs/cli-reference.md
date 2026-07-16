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
  [--libstdcxx gcc@VERSION] \
  [--host-glibc-floor FLOOR] [--cmake-version VERSION] \
  [--jobs N] [--runner RUNNER] \
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
matching runtime. Managed Clang includes matching LLVM libc++ plus a pinned GCC
libstdc++/libgcc runtime; `--libstdcxx` selects the GCC provider and defaults
to `gcc@12`. The primary integration defaults to `shell` and selects producer
consumer verification. Every high-level setup installation carries CMake,
shell, and Conan adapters.

`--host-glibc-floor` is the independent audit ceiling for every Compiler Kit
host ELF. When omitted, setup resolves it to the target `--glibc` value. The
published compiler and helper executables must not require newer `GLIBC_*`
versions, while its binutils must have no dynamic glibc dependency. An
explicit host floor may differ from the target SDK floor. `--jobs`
controls producer parallelism but is not part of the
content-addressed SDK or managed artifact identity. CPU instruction options
such as `-march`, `-mcpu` and `-mtune` belong to the consumer build and pass
through the compiler wrapper. `--conan-cppstd` and `--conan-build-type`
configure the generated Conan host profile independently of the selected
verification integration. Conan verification also uses that build type.
`--conan-build-profile` selects the producer-native profile used by that
verification and therefore requires `--integration conan`. Non-Conan
verification neither invokes Conan nor creates producer-side Conan run state.

`--cmake-version` selects the CMake, CTest, and CPack release in the
architecture-specific supplemental build-tools artifact. It defaults to
`3.31.12`; the supported pinned values are `3.31.10`, `3.31.11`, and
`3.31.12`. The source-checkout Make wrapper exposes the same selection as
`CMAKE_VERSION`. This value is immutable setup state and participates in the
build-tools identity.

Builder images use Ubuntu's normal archive mirrors when
`LINUX_TOOLCHAIN_UBUNTU_SNAPSHOT` is unset or empty. Set it to a timestamp in
`YYYYMMDDTHHMMSSZ` form to select an Ubuntu snapshot. The source-checkout Make
wrapper exposes the same setting as `UBUNTU_SNAPSHOT`. The value changes the
builder identity and must remain the same across separate producer commands
that build, export or validate one prepared state. SDK, build-tools, compiler
and runtime production share one exact Docker image, so its package
installation and crosstool-NG setup are not repeated between those stages.

`WORK_DIR` stores one strict format-1 setup selection and its lock, binding,
verification result and prepared state. `STORE_DIR` is a shared content-addressed
producer store for verified sources, SDK workspaces, build tools, managed build
trees and logs. Both default below the user cache directory. When omitted,
`WORK_DIR` is
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
artifacts. The prepared state is qualified only while its format-1 passed
verification result matches the current binding and selected integration.

On success, human-readable progress goes to stderr and stdout contains only the
launcher path. Long-running producer stages may also stream or summarize child
output on stderr. The stderr handoff includes a command for the current shell
and direct append commands that persist the launcher's directory in `~/.bashrc`
or `~/.zshrc`. The launcher depends only on the installed prefix, not Python,
the management command or `WORK_DIR`.
`--no-path-instructions` omits this human handoff while preserving the launcher
path on stdout for command composition.

Producer verification keeps configure, build, audit, loader and runtime output
in its verification directory. A successful setup reports only a concise
verification `PASS` on stderr; a failed command replays its output and names the
corresponding log.

The launcher does not search the current directory or its parents for
configuration. It loads the installed binding environment and executes the
remaining argument array unchanged. It searches the installed binding tools
first, the bundled CMake/CTest/CPack/Make/Ninja/ccache directory second, and the
inherited host `PATH` last. ccache is available but is not configured as a
compiler launcher automatically. Every high-level installation selects a
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
the producer path only. An existing output is rejected by default. `--force`
atomically replaces an existing regular file after the new installer is
complete; it does not replace a directory, symlink, or other non-regular path.
The Makefile form is `make bundle ... BUNDLE_OPTIONS=--force`.

The low-level release interface is intentionally separate:

```bash
linux-toolchain bundle create-artifacts \
  --sdk SDK --build-tools BUILD_TOOLS \
  --compiler-kit COMPILER_KIT --runtime RUNTIME \
  --lock LOCK --variant VARIANT --output INSTALLER.run \
  [--id ID] \
  [--integration cmake|shell|conan ...] \
  [--conan-cppstd VALUE] [--conan-libcxx VALUE] \
  [--conan-build-type VALUE] [--force]
```

For this command, `BUILD_TOOLS` is the validated native build-tools artifact
and `RUNTIME` is the published runtime-set directory selected by the variant.
`bundle create-artifacts` accepts an explicitly assembled managed combination
and performs the same producer validation. With no `--integration` option it
selects all three adapters; explicit selections retain the low-level control.
The installed toolchain contains a consumer launcher but no Python runtime or
management CLI. Its launcher is named `lxtc` until installation.

Install the generated file directly:

```bash
./INSTALLER.run [--prefix PREFIX] \
  [--launcher-name NAME] [--conan-home PATH] \
  [--conan-cppstd VALUE] [--conan-build-profile NAME_OR_PATH]
```

The shell installer requires Linux, the recorded host architecture and minimum
glibc, a POSIX shell, and common Unix archive tools. Without `--prefix`, it
installs below `$HOME/.local/lib/linux-toolchain/<NAME>`, where `NAME` is
embedded at bundle creation from the selected compiler, target, runtime and any
non-default CMake version. Renaming the installer does not change this default.
An explicit `--prefix` overrides it; omitting `--prefix` requires a valid `HOME`.
`--launcher-name` selects the installed command name. A Conan-capable bundle
defaults to
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
lxtc runtime show
lxtc runtime set libstdc++|libc++
lxtc runtime reset
lxtc --runtime libstdc++|libc++ COMMAND [ARG ...]
lxtc run EXECUTABLE [ARG ...]
lxtc shell
lxtc conan-init
```

`info` prints stable `key=value` lines for the bundle and variant IDs,
installation prefix, compiler, target triplet and sysroot, libc floor, C++
runtime default, available choices and current selection, integrations, CMake
toolchain, build-tools architecture and host floor, every bundled tool's
version, path, linkage and default-enabled state, and current Conan home/profile
selection. `cxx_runtime.kind` remains the installed default;
`cxx_runtime.selected` reports the effective selection.

`runtime show` prints the persistent launcher selection. On a managed Clang
bundle that publishes both `libstdc++` and `libc++`, `runtime set` stores a new
selection and `runtime reset` restores the installed default. The state file is
`${XDG_CONFIG_HOME:-$HOME/.config}/linux-toolchain/<BUNDLE_DIGEST>/runtime`;
it belongs to the launcher, not to a Conan home or installation prefix.
`--runtime` selects a value only for the supplied command and its children.
Its precedence is command option, inherited `LINUX_TOOLCHAIN_CXX_RUNTIME`,
persistent selection, then installed default. The C++ wrapper applies that
effective value at the compiler-driver layer, independent of CMake or Conan
flag initialization. A direct `binding/bin/c++` invocation outside the
launcher and without the environment variable uses the installed default.
Consumer compiler arguments remain unchanged and retain normal driver
precedence.

`run` executes a dynamically linked target ELF with the SDK dynamic loader.
The executable may contain a slash or be found through the launcher `PATH`.
The loader search starts with the effective C++ runtime and SDK libc
directories, then the executable directory. Before execution, the same SDK
loader lists the recursive closure with its cache disabled. An unresolved
entry, an entry from an unselected managed runtime, or a library resolved below
a system `/lib*`, `/usr/lib*`, or `/usr/local/lib*` directory fails the command.
Application-owned libraries outside those locations remain available through
the executable's normal dynamic tags. `LD_AUDIT`, `LD_PRELOAD`, and inherited
`LD_LIBRARY_PATH` are removed. The program replaces the launcher process, so
its exit status and signals are preserved.

On AArch64 with an SDK glibc older than 2.36, `run` enters a user and mount
namespace and binds the SDK loader over the target interpreter path before
starting the program. That compatibility path requires host `unshare`,
`mount`, and a POSIX shell. `run` is a local execution and validation aid, not
a deployment layout and not a runner for scripts or static executables.

`shell` starts an interactive child shell with the effective launcher
environment. It selects `${SHELL:-/bin/sh}` and supports Bash, Zsh, and the
common POSIX shell names `sh`, `dash`, `ash`, `ksh`, and `mksh`. The launcher
prints the compiler, target, libc floor, and effective C++ runtime on stderr.
After reading the user's normal startup file, it reapplies the binding
environment and gives `binding/bin`, bundled build tools, and the installation
launcher directory precedence over the resulting `PATH`. It replaces the
conventional prompt prefix through the first `:` with an LXTC label while
preserving the working-directory and VCS suffix. The label is
`(lxtc clang-VERSION RUNTIME)` for Clang with a C++ runtime and
`(lxtc gcc-VERSION)` for GCC. A compiler-only binding reports `runtime: none`
and omits it from the label. The command does not edit startup files or modify
the parent shell. After user startup files are read, the shell initializer
prepends only the selected C++ runtime library directories to that child's
`LD_LIBRARY_PATH`, preserving any user value after them. Directly executed
programs therefore find the selected runtime, but still use the host loader and
libc; use `lxtc run` when the SDK libc closure is required.

The child shell exports the runtime selected when it was created. A later
`runtime set` updates the persistent default but cannot mutate that shell; its
children continue to inherit the shell snapshot unless they use `--runtime`.
Use `exec lxtc --runtime libc++ shell` (or `libstdc++`) to replace it with
another runtime snapshot. `exit` returns to the parent shell. When the
installer used `--launcher-name`, substitute that name for `lxtc`.

When Conan is present, the generated target and native build profiles both
follow the effective runtime. An explicit `--conan-build-profile` remains
user-owned and is never remapped by runtime selection. Single-runtime bundles
support `runtime show` but reject switching.

`conan-init` recreates missing static settings and profiles in the Conan home
recorded by the installation. It neither invokes Conan nor restores package
cache entries. Existing identical files are accepted; an existing different
file fails without replacement. On success it prints the Conan home path. The
command is unavailable when the bundle does not contain the Conan integration.
Runtime selection is not part of this command. `conan-init`, `runtime set`, and
`runtime reset` refresh `lxtc.info` through atomic file replacement. The
snapshot matches plain `lxtc info` for the persistent selection. A
command-scoped or inherited override does not rewrite it. The Conan-home suffix
is a one-way bundle-ID digest;
use `lxtc.info` rather than decoding the directory name.

These commands require only the installed bundle. Use `lxtc -- COMMAND ...`
when a consumer executable is literally named `info`, `runtime`, `run`,
`shell`, or `conan-init`.

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
libraries. `libc++experimental.a` is preserved and validated when an external
prefix provides it; managed LLVM runtime production requires it. The import and
binding do not enable or link the experimental archive automatically. Both
commands publish relocatable runtime artifacts without compiler executables;
binding creation performs the final dynamic and static compiler/runtime link
probes.

## Create a binding

- `bind external` binds a host-managed GCC or Clang. It requires `--runtime` or
  an explicit development-only `--allow-unpinned-runtime` choice.
- `bind managed` binds already produced managed artifacts. The selected lock
  variant determines the default and available C++ runtimes; its `--runtime`
  path names the corresponding runtime-set publication.

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
1. `cxx_runtimes` records the default and all available runtime entries, while
the `integrations` object records only rendered adapters. Consumer build type
and Conan vocabulary are not part of the binding format.

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
`--conan-libcxx libstdc++|libstdc++11|libc++`. The selected runtime set
determines the default and accepts only an available choice.

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

```bash
linux-toolchain verify --binding BINDING --integration cmake|shell|conan \
  --build-dir DIRECTORY
linux-toolchain audit --policy POLICY [--recursive] PATH...
```

- `verify` builds the installed C++/ASM integration project, audits its outputs,
  checks the dynamic-loader closure and runs it with eager symbol binding. Its
  integration choices are `cmake`, `shell` and `conan`; `--build-type` belongs
  to this consumer build rather than to the binding and defaults to `Release`.
  Native execution with an AArch64 glibc older than 2.36 uses an unprivileged
  user and mount namespace so the kernel enters the SDK loader through the
  declared interpreter without changing the host filesystem.
  The shell mode uses the packaged Make consumer. A successful run writes
  `result.json` with schema `linux-toolchain-smoke-result` and format 1.
  Detailed command output remains in the build directory, stdout contains only
  the `result.json` path, and failure output is replayed on stderr.
- `audit` applies a binding's ELF policy to one or more files or a recursive
  deployment tree. `audit --json` emits
  `linux-toolchain-elf-audit-report` format 1.
- `conan settings` writes the settings extension required by generated Conan
  host profiles.
