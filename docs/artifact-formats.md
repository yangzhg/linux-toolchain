# Versions and artifact formats

[English](artifact-formats.md) | [简体中文](zh-CN/artifact-formats.md)

The product follows Semantic Versioning. Public CLI commands, exit codes, JSON
documents and generated artifact layouts are stable release interfaces.
Human-readable tables and progress messages are not machine-parsing interfaces.
Python modules are not a supported public API.

## Structured documents

Every public top-level JSON document has an explicit identity and representation
version. Readers require an exact `schema` match and the integer `format` value
`1`; missing, unknown or malformed fields are rejected.

The public document identities are:

| Document | Schema |
| --- | --- |
| SDK input specification | `linux-toolchain-sdk-spec` |
| SDK build workspace | `linux-toolchain-sdk-workspace` |
| Published SDK manifest | `linux-toolchain-sdk` |
| Host build-tools workspace | `linux-toolchain-build-tools-workspace` |
| Host build-tools manifest | `linux-toolchain-build-tools` |
| Managed compiler specification | `linux-toolchain-managed-spec` |
| Managed lock | `linux-toolchain-managed-lock` |
| Managed build workspace | `linux-toolchain-managed-workspace` |
| Managed build artifact | `linux-toolchain-managed-build-artifact` |
| Managed runtime publication | `linux-toolchain-managed-publication` |
| Managed runtime set | `linux-toolchain-managed-runtime-set` |
| Managed assembly result | `linux-toolchain-managed-assembly` |
| Compiler Kit manifest | `linux-toolchain-compiler-kit` |
| GCC runtime manifest | `linux-toolchain-gcc-runtime` |
| LLVM runtime manifest | `linux-toolchain-llvm-runtime` |
| Compiler binding | `linux-toolchain-binding` |
| ELF audit policy | `linux-toolchain-elf-audit-policy` |
| ELF audit report | `linux-toolchain-elf-audit-report` |
| Doctor report | `linux-toolchain-doctor` |
| Consumer verification result | `linux-toolchain-smoke-result` |
| SDK catalog result | `linux-toolchain-sdk-catalog` |
| Managed release index | `linux-toolchain-managed-release-index` |
| Managed lock artifact listing | `linux-toolchain-managed-lock-artifacts` |
| Machine-local setup configuration | `linux-toolchain-setup` |
| Prepared setup state | `linux-toolchain-prepared-setup` |
| Installed toolchain and portable bundle manifest | `linux-toolchain-bundle` |

Cryptographic hashes are reserved for downloaded sources and fixed build inputs
where the relevant document declares one. Generated artifacts are identified by
their schema metadata and validated through their actual compiler and ELF
behavior.
Every format-1 managed-lock source has exactly `id`, `family`, `version`,
`kind`, `url` and `sha512`; `kind` is `archive`, and `sha512` identifies the
official GCC or LLVM release archive bytes. A managed build action records this
content identity as `{"kind":"archive","sha512":...}`, while
`provenance.source` records only the acquisition `url`. Managed LLVM runtime
source evidence records exactly `kind`, `version`, `target`, `url` and
`sha512` for a `managed-artifact` source.
Each managed-lock variant contains `id`, `compiler_kit_id`, `runtimes`,
`default_cxx_runtime`, `family`, `version`, and `target`. Its `runtimes` array
contains `{kind, runtime_id}` entries in canonical order. A GCC variant contains
only libstdc++; a high-level Clang variant contains libstdc++ first as the
default and libc++ second.
Machine-local bindings and producer workspaces may record absolute paths.
Published SDKs, Compiler Kits, runtime artifacts and lockfiles are relocatable.
Managed build workspaces record the SDK, target tools produced by that SDK
workspace and compiler-backend workspace as separate local inputs. A raw
managed artifact has exactly `schema`, `format`, `action`, `action_sha256`,
`provenance`, `licenses` and `elf_audit`. `action` is the single static build
identity; acquisition URLs, the resolved builder image and the executed script
are evidence below `provenance`. The artifact does not copy lock or catalog
identities. A runtime publication receipt has exactly `schema`, `format`,
`raw_action`, `publication_action`, `publication_action_sha256` and `licenses`.
The raw action digest is recorded once as
`publication_action.raw_action_sha256`.

SDK and managed builder metadata record the package-source selection as
`apt_snapshot`. The empty string means Ubuntu's normal archive mirrors; a
`YYYYMMDDTHHMMSSZ` value means the corresponding Ubuntu snapshot. This value
participates in the static builder identity. The resolved image ID remains
provenance rather than a second artifact identity. In live-mirror mode, equal
recorded builder inputs can resolve different package bytes after Docker's
image and build cache have been discarded.

An SDK records the complete builder identity as
`build_environment.contract_sha256`. Its `sources.crosstool-ng.patch` object
contains the packaged backend patch's relative `path` and `sha256`. Managed
consumers reject SDK evidence that does not match either value.

A host build-tools workspace contains exactly `schema`, `format`, and
`identity`. Its identity fixes the native architecture, Compiler Kit host
glibc floor, selected CMake release, source archives, compiler backend, and
build script. The published `linux-toolchain-build-tools` manifest contains
exactly `schema`, `format`, `identity`, `tools`, `builder_image`, `elf_audit`,
and `licenses`. Static selection, source, compiler-backend, and build-script
evidence is recorded once below `identity`; `builder_image` is execution
provenance. `tools` records `cmake`, `ctest`, `cpack`, `make`, `ninja`, and
`ccache`, each with `version`, relative `path`, `linkage`, and
`enabled_by_default`. ccache is `static-musl` and false by default; the other
entries are `glibc-floor` and true.

An LLVM runtime manifest fixes `abi.linkage` to `both`. Its `locations`
contains separate sorted `shared_libraries` and `static_libraries` arrays; the
static array always contains `libc++.a`, `libc++abi.a` and `libunwind.a`.
`libc++experimental.a` is the only additional static archive admitted by
format 1. It is required for a `managed-artifact` source and optional for an
external `clang-probe` source.

The setup configuration and prepared state are machine-local documents below
an explicit producer work directory. `setup.json` selects exactly one managed
compiler, target and primary consumer verification integration; it is generated by
`linux-toolchain setup` and is not a consumer-repository input.
For Clang, `libstdcxx` records the selected `gcc@VERSION` provider and defaults
to `gcc@12`. GCC setup files omit that field. Every setup file records
`cmake_version`; changing it changes the immutable selection.
`state/prepared.json` records the immutable selection hash and exact absolute
paths to the resolved lock, SDK workspace, build-tools workspace and artifact,
managed workspace, Compiler Kit, published runtime set, binding, verification result,
and optional Conan home/build profile.
High-level setup resolves an omitted `--host-glibc-floor` to the target glibc
floor before writing `setup.json`. Every format-1 setup file records the
resolved `host_glibc_floor` explicitly; a missing field is malformed rather
than an implied policy.
Moving a recorded path requires `setup`
again. A work directory's selection is immutable: changing the compiler,
target, Clang libstdc++ provider, integration, selected CMake release, or policy
requires a new work directory. Parallel
job count is execution state rather than selection state and may change in the
same work directory. High-level `--force` authorizes repair or replacement of
matching generator-owned selection outputs. Already-valid immutable producer
artifacts remain reusable and are not deliberately rebuilt.

High-level setup bindings render `cmake`, `shell`, and `conan` together. The
format-1 `integration` field identifies the one producer verification result that
qualifies prepared state; it is not an array of installed
capabilities. A Conan primary verification may record producer-side Conan run state.
Rendering the dormant adapter for another primary integration does not create
that producer-side state. The optional setup `conan` record configures the
static Conan host profile independently of `integration`; its
`build_profile` member is valid only when `integration` selects Conan
verification.

The managed lock's `compiler_kits` array includes every variant compiler and
the matching provider compiler for each entry in `runtimes`. A provider Kit may
therefore be cached in the producer store without being selected for the
current bundle. Compiler Kit and runtime payload ownership remains separate.

The producer store is a separate machine-local, content-addressed namespace.
SDK workspace identity is derived from its target, rendered configuration,
source, builder inputs and export rules. A build-tools workspace is keyed by its
native architecture, host glibc floor, selected CMake release, verified sources,
compiler backend and build script. A managed parent workspace is derived from
the target SDK and compiler backend identities; Compiler Kit, raw runtime and
runtime-publication paths are then keyed by their build actions, including the
runtime adapter revision. A runtime-set path is keyed by its lock, variant and
component publication identities. Source cache entries are keyed by source
content.
Parallel job count is an execution option and does not change these identities,
so multiple setup selections can reuse the same validated inputs.

Managed setup, installation publication and prepared-bundle creation acquire
shared leases for the exact producer identities they consume. Writers for those
identities acquire exclusive leases. The resulting stable-tree guarantee is
limited to these coordinated management flows; arbitrary external filesystem
readers are not promised lock-free visibility during replacement.

The portable bundle manifest is canonically serialized as `manifest.json` in
the self-extracting payload. Its exact top-level keys are `schema`, `format`,
`id`, `variant`, `compiler`, `target`, `host`, `build_tools`, `cxx_runtimes`,
and `binding`. `build_tools` contains the exact `selection` and `tools`
inventory from the validated supplemental artifact.
`cxx_runtimes` contains `default` plus an `available` array of runtime kind,
provider and version records. The SDK, Compiler Kit, runtime set, managed lock,
and binding template use fixed format-1 locations below the payload. Host and
target glibc floors are separate fields. The binding record selects
integrations and optional Conan host settings.
High-level setup bundles select all three adapters and carry default Conan host
settings. Explicit low-level bindings and bundles retain their recorded
selection.

The payload root contains exactly `manifest.json`, `artifacts/`, `tools/`,
`binding/`, `bin/`, and `template-files`. Setup's already validated binding is
reused as the bundle template. Its binding and artifact roots are replaced with
a prefix token, so generated template files contain no producer work-directory
or producer-store path.
`template-files` is a sorted list of regular files that the shell installer
must instantiate. The public format does not define a separate schema for the
installer envelope.

A managed runtime-set manifest contains exactly `schema`, `format`,
`lock_sha256`, `variant`, `default`, and `runtimes`. Each runtime entry contains
`kind`, `artifact_id`, and a relative `path`. The paths are
`runtimes/libstdcxx` and `runtimes/libcxx`; each directory remains a complete,
independently validated runtime publication.

A Compiler Kit manifest records target binary tools separately from
`locations.linkers`. GCC linker entries are `default`, `bfd`, `gold`, and
`mold`; Clang adds `lld`. A binding manifest records runtime choices under
`cxx_runtimes` with exact `default` and `available` keys.

For a binding with Conan, `binding/conan/settings_user.yml` is the exact
settings extension installed into its dedicated Conan home. Bundle assembly
adds prefix-independent `default.profile` and `lxtc-build.profile` selectors;
they delegate through `LINUX_TOOLCHAIN_CONAN_HOST_PROFILE` and
`LINUX_TOOLCHAIN_CONAN_BUILD_PROFILE`. It also adds `build.profile`, which
joins the installed host profile with the managed runtime search path for
native build requirements. This build-context file belongs to the native
managed Bundle format, not to the generic low-level binding renderer.
For a dual-runtime Clang bundle, `lxtc-libstdcxx.profile` and
`lxtc-libcxx.profile` select the matching target package identity and wrapper
runtime. Their Conan build environments also prepend the selected runtime
library directories so native build requirements remain executable when host
packages invoke them. Matching `build-libstdcxx.profile` and
`build-libcxx.profile` files select the native build-context identity, wrapper
runtime, and the same runtime search path; `build.profile` delegates to the
installed default. A single-runtime bundle carries that build-environment path
in both `default.profile` and `build.profile`.
Installed machine state in `binding/conan/conan-home` and
`binding/conan/build-profile` records the dedicated home and effective build
profile. A dual-runtime launcher's persistent selection is stored at
`${XDG_CONFIG_HOME:-$HOME/.config}/linux-toolchain/<BUNDLE_DIGEST>/runtime`,
not in the prefix or Conan home. The dedicated home contains generated
`lxtc.info`, which is regenerated from the installed binding and persistent
selection while command-scoped overrides do not modify it. The home-name and
runtime-state suffixes use the same truncated bundle-ID digest; neither is a
reversible encoding of the toolchain fields. These machine-local files and the
Conan cache are not part of the immutable installation prefix.

## Creating, reusing and replacing artifacts

Generated artifacts are immutable inputs. Recreate an artifact in a separate
output directory when its SDK, compiler, runtime, integration selection or policy
changes. Validate the complete output before switching consumers. Bindings contain
machine-local executable paths and must be regenerated rather than moved to a
different machine or filesystem layout.

The producer work directory is independent of every consumer source tree and
has this generator-owned format-1 layout:

```text
WORK_DIR/
  .linux-toolchain-setup-root
  setup.json
  state/
    .linux-toolchain-setup-state
    prepared.json
    ...
```

Reusable producer inputs are outside that selection tree:

```text
STORE_DIR/
  .linux-toolchain-producer-store
  sdk/IDENTITY/
  build-tools/IDENTITY/
  managed/IDENTITY/
  source-archives/
  managed-sources/
  locks/
```

`source-archives/` is the shared SHA-256 object cache used by SDK and build-tools
sources. `managed-sources/` is the owned SHA-512 cache for managed compiler
release archives.

Both marker files contain `format=1`. The generator refuses a non-empty setup
work directory or state directory without the corresponding marker. A completed
setup publishes `state/prepared.json` atomically only after the binding and
selected verification path succeed. Qualification requires a format-1
verification result with status `passed` whose binding and integration still
match the current selection. An
interrupted setup can reuse validated immutable artifacts; binding replacement
still requires proof that the existing binding is generator-owned. An existing
work directory cannot be repurposed for a different setup selection. The
producer-store marker also contains `format=1`; a non-empty unowned store is
rejected.

The final installation has the same top-level layout as an installed bundle:

```text
PREFIX/
  manifest.json
  artifacts/
    sdk/
    compiler-kit/
    runtime/
    managed.lock.json
  tools/
  binding/
  bin/
    lxtc
```

The launcher loads the binding and build tools below its own prefix. It searches
`binding/bin` before `tools/bin`, then preserves the inherited host `PATH`. It
may be called from any working directory and does not depend on the producer
state or Python CLI. Bundle generation embeds the install-relative SDK loader,
SDK library directories, and per-runtime library directories in this launcher;
runtime execution does not rediscover them from the host. The installed prefix
selection is immutable and validated before reuse or generator-owned
replacement. Final publication validation checks known manifest fields,
relocated declared paths and instantiated text templates. It does not repeat
compiler, linker, loader or target-like consumer qualification and must not be
treated as release qualification.

`binding/env/lxtc-shell/.zshrc` is the launcher's POSIX-compatible interactive
shell initializer. Bash uses it as an explicit rc file, Zsh discovers it
through a temporary `ZDOTDIR`, and supported POSIX shells use it through
`ENV`. It loads the user's normal startup file and then reapplies
`binding/env/toolchain.env`. It then prepends the launcher's selected C++
runtime library directories to the child shell's `LD_LIBRARY_PATH`; SDK libc
directories are not added. It is internal installed payload, not a user
configuration file.

A bundle installer accepts only an absent or empty prefix. It extracts to a
fresh sibling directory, instantiates the listed template files, removes
`template-files`, optionally renames `bin/lxtc` to the install-time
`--launcher-name`, and moves the complete payload directory to the prefix. The
installed top level is `manifest.json`, `artifacts/`, `tools/`, `binding/`, and
`bin/`. Install each new bundle into a new prefix.
For Conan-capable bundles it also writes matching static settings plus target
`default` and build-context `lxtc-build` profiles into
`$HOME/.conan2_lxtc_<BUNDLE_DIGEST>`, using the first 16 hexadecimal
characters of the bundle ID's SHA-256 digest, or the explicit `--conan-home`. The default
build context is the bundle-generated managed-native profile.
The installed launcher can recreate these static files and `lxtc.info` with
`conan-init`. Runtime selection is managed separately by `runtime show`,
`runtime set`, `runtime reset`, and the command-scoped `--runtime` option.
For a dual-runtime bundle, the default target and native build selectors both
follow that selection.
`--conan-build-profile` accepts another profile name in that dedicated home or
an absolute path; an explicit override may be absent until the consumer needs
it, but it cannot select the generated `lxtc-build` selector itself and is not
remapped by runtime selection. The Conan home and installation prefix cannot
contain one another. `--conan-cppstd` adds only a target-profile
`compiler.cppstd` override;
when omitted, the target profile contains the compiler default modeled by
Conan 2 for the managed compiler family and major. The installer never invokes Conan.
Existing different configuration fails closed,
and neither the installer nor `conan-init` recursively replaces the Conan home.
