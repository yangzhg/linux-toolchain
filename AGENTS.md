# Repository guide for coding agents

This file applies to the entire repository.

## Product boundary

`linux-toolchain` generates controlled, auditable Linux C and C++ build inputs
for an explicit glibc ABI floor. Its product model is independent of any
consumer repository or build system, and its portable artifacts must not assume
the development host or a local filesystem layout. External bindings
intentionally inspect a host compiler and are machine-local.

Keep the four artifact layers separate:

1. A glibc SDK owns glibc headers and libraries, the dynamic loader, startup
   objects, and Linux UAPI headers.
2. A Compiler Kit owns exact managed GCC or Clang drivers and its declared
   target tools. It does not own the target C++ runtime.
3. A runtime overlay owns compiler runtime headers, CRT objects, and GCC or LLVM
   runtime libraries. It does not contain compiler executables.
4. A binding joins an SDK and optional runtime overlay to an external compiler
   or managed Compiler Kit, then generates wrappers, an audit policy, and the
   selected consumer integrations.

A sysroot controls libc-facing inputs. It does not by itself pin libstdc++,
libgcc, libc++, compiler-rt, their headers, or their CRT objects.

External compilers are a first-class mode. Managed mode adds pinned compiler
and target-tool provenance. A managed workflow must not discover target tools
from the host `PATH`.

The target glibc floor and the Compiler Kit host glibc floor are independent
policies. Neither is a complete runtime-compatibility guarantee. Kernel APIs,
CPU instructions, loader configuration, dependency closure, plugins, and
process-wide C++ runtime interactions remain separate concerns.

The product provides direct CMake and shell/Make integrations, with Conan 2 as
an opt-in adapter. Autotools and hand-written Ninja builds can use the generated
shell environment. Do not claim native Meson, Bazel, or other build-system
support until an adapter and its validation exist.

The management workflow is independent of consumer repositories. There is no
project-root `init` or `prepare`, no consumer configuration that must be
committed, and no package-installed global launcher. `linux-toolchain` is the
Python management and release CLI used on producer machines. `setup` keeps one
machine-local selection and its prepared validation state below an explicit
work directory. Reusable SDKs, verified sources, managed build trees and logs
belong to an explicit shared content-addressed producer store. Build jobs are an
execution option, not a producer artifact identity. Normal setup publishes a
self-contained installation below an explicit prefix; `--prepare-only` stops at
validated prepared state so bundle creation can package those artifacts without
first installing them. A shell bundle installer instantiates the same
producer-validated payload and may rename its `lxtc` launcher at installation.
Work-directory and installation selections are immutable; `--force` may rebuild
or replace only matching generator-owned outputs, and a different selection
requires new paths. High-level setup is native on x86-64 and AArch64 producer
hosts. The selected target must match the producer architecture.

Bolt is only an optional external-consumer acceptance test under
`tests/consumers/bolt/`. Never add Bolt-specific behavior, names, defaults, or
dependencies to `src/`, the CLI, artifact schemas, or package metadata.

## Read before changing public behavior

- `README.md` and `README.zh-CN.md`: user-facing model and workflows
- `docs/architecture.md`: artifact ownership and trust boundaries
- `docs/compatibility.md`: what an ABI floor does and does not prove
- `docs/managed-compilers.md`: managed setup, build and runtime model
- `docs/artifact-formats.md`: public JSON formats and filesystem layouts
- `docs/cli-reference.md`: command, output, and exit-status behavior
- `CONTRIBUTING.md`: development and release expectations

English documents are canonical when reviewing public behavior. User-facing
documents have separate Chinese counterparts: `README.zh-CN.md`, `docs/zh-CN/`,
`CONTRIBUTING.zh-CN.md`, `SECURITY.zh-CN.md`, and `CHANGELOG.zh-CN.md`. Keep
each pair semantically aligned when behavior changes. Write each language
naturally instead of mixing languages in one file or translating sentence by
sentence. Preserve commands, schema names, filenames, and public field names
exactly. Every paired document must link to its counterpart near the title.

## Module routing

Put behavior in the module responsible for it. Keep CLI handlers thin.
Paths in the table are relative to `src/linux_toolchain/`.

| Area | Primary modules |
| --- | --- |
| CLI parsing, output, and exit behavior | `cli_parser.py`, `cli.py` |
| Setup config, prepared state, and producer orchestration | `setup_models.py`, `setup.py` |
| Installed payload publication, bundle assembly, and installation-derived selection | `bundle.py` |
| Shell installer, binding templates, and deterministic archive | `bundle_installer.py` |
| SDK models, targets, versions, and catalog | `models.py`, `versions.py`, `recipes.py` |
| crosstool-NG rendering, Docker build, and SDK export | `sdk/crosstool_ng.py` |
| Managed catalog, models, and deterministic locks | `managed/catalog.py`, `managed/models.py`, `managed/lockfile.py` |
| Managed source acquisition, selection, workspaces, build scripts, artifact finalization, publication, and assembly | `managed/contracts.py`, `managed/selection.py`, `managed/sources.py`, `managed/builder.py`, `managed/*_build_script.py`, `managed/artifacts.py`, `managed/publication.py`, `managed/assemble.py` |
| Compiler discovery, Compiler Kit manifests, and bindings | `compiler/toolchain.py`, `compiler/managed.py`, `compiler/binding.py`, `compiler/managed_binding.py` |
| GCC runtime import | `runtime/models.py`, `runtime/importer.py` |
| LLVM runtime import | `runtime/llvm_models.py`, `runtime/llvm.py` |
| CMake, shell, and Conan rendering | `integrations/` |
| ELF parsing, policy, and reports | `elf/` |
| Build-input hashes and atomic publication | `integrity.py`, `publication.py` |
| License evidence | `licenses.py` |
| External process execution | `process.py` |
| Diagnostics and packaged consumer smoke tests | `diagnostics.py`, `smoke.py`, `resources/` |
| Conan settings extension | `conan/settings.py` |

Validation and artifact semantics belong in domain modules, not argument
parsing or integration renderers.

## Development workflow

The project supports Python 3.10 and newer and has no runtime Python
dependencies. From the repository root:

```bash
make bootstrap
make lint
make check
git diff --check
```

`make bootstrap` creates `.venv` and installs the package with pinned
development tools. Set `VENV=/path/to/venv` when a different dedicated
environment is needed.

For a focused test, invoke its `unittest` module directly:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest tests.test_models
```

Run the complete suite before handing off a code change. Unit tests must not
require Docker, network access, downloaded toolchains, or a particular host
compiler.

Run `make python-dist DIST_DIR=/path/to/empty/directory` for Python distribution
checks.
The destination must be empty so artifacts from different builds cannot mix.

Use the relevant diagnostic before a production workflow:

```bash
.venv/bin/linux-toolchain doctor --workflow sdk
.venv/bin/linux-toolchain doctor --workflow managed
.venv/bin/linux-toolchain doctor --workflow external
.venv/bin/linux-toolchain doctor --workflow consumer
```

A failed diagnostic describes the current environment. Do not weaken a product
requirement to accommodate one host.

## Code conventions

- Use `pathlib.Path`, type annotations, explicit domain models, and
  deterministic data structures.
- Raise `ConfigurationError` for invalid input and
  `ExternalToolError` for failed external operations. Expected CLI failures
  must not expose Python tracebacks.
- Invoke external programs with argument arrays through `process.py`. Never
  construct a shell command from user-controlled values or use `shell=True`.
- Do not assume `/usr/bin`, a Linux distribution, a compiler version, the host
  architecture, a username, or a developer-specific path.
- Keep successful machine-readable output on stdout. Progress, diagnostics,
  and child-process output belong on stderr.
- Keep generated JSON canonical and deterministic. Do not put local time,
  random identifiers, current working directories, or machine paths into
  relocatable artifacts unless their format explicitly permits it.
- Document the resulting behavior and boundary, not the experiments or edit
  sequence that produced it.
- Do not commit generated SDKs, source archives, Compiler Kits, runtime exports,
  bindings, credentials, caches, virtual environments, or build trees.

## Public interface rules

Public CLI commands, exit statuses, top-level JSON documents, and generated
artifact layouts are stable release interfaces. Human-readable tables and
progress messages are not machine-readable APIs.

`docs/artifact-formats.md` is the canonical inventory of public schema
identities. For every public JSON document:

- `schema` is required and must match the exact document identity;
- `format` is required, must be an integer, and currently must equal `1`;
- missing, malformed, unknown, or unsupported fields fail closed where the
  model defines an exact key set;
- serialization stays deterministic;
- relocatable artifacts use relative payload paths.

This is a greenfield format-1 implementation. Do not add legacy schema names,
pre-format readers, compatibility aliases, guessed defaults, or speculative
future readers.
A breaking representation change requires an intentionally designed new
format, tests, documentation, and a release decision.

Hashes identify downloaded source and fixed build inputs where their formats
declare one. Generated SDK, Compiler Kit, runtime, binding, and license trees
are validated structurally and by real compiler/ELF checks; do not add a second
tree-hash or per-file identity layer.

## Filesystem and correctness rules

Compiler arguments are ordinary compiler input and pass through to the selected
driver. The project validates generated toolchain behavior; compiler-argument
filtering and producer-side sandboxing are outside its scope.

- Validate archive-member and relative-path containment before writing outside
  an intended output tree.
- Build complete outputs in sibling staging directories, validate at the final
  location, and publish atomically. Failed validation must preserve the previous
  valid publication.
- For artifact-directory publication, `--force` may replace only a destination
  proven to be generator-owned. Never recursively remove an unowned,
  ambiguous, symlinked, or root destination. File-oriented commands must follow
  their separately documented replacement rules.
- Treat published SDKs, Compiler Kits, runtimes, locks, and bindings as
  immutable. Recreate them instead of editing them in place.
- Bindings are machine-local because they record absolute executable paths.
  Regenerate them after moving machines or filesystem layouts.
- Verify official source identities and cryptographic hashes before extraction,
  and keep archive extraction within its destination.
- `managed fetch` is optional prefetch. A managed build verifies or acquires a
  missing source before starting its network-disabled compiler container.
- Keep managed builds non-root, with source, SDK, compiler backend, and
  target-tool inputs mounted read-only. Preserve matching extracted source and
  compiler build trees for continuation; recreate only stale trees and partial
  artifact staging.
- Do not weaken source verification, output ownership checks, ELF closure checks,
  or compatibility validation to make an unsupported artifact pass.

## Testing policy

Add a test when it protects stable public behavior, a real regression, or an
operation that could replace unrelated user output. Prefer extending an
existing scenario or table over creating a new file or many single-assertion
variants.

Keep representative coverage for GCC and Clang, x86-64 and AArch64 where their
behavior differs, the main integration paths, package boundaries, and the
external Bolt harness. Preserve focused negative coverage for:

- target, architecture, ABI floor, loader, and runtime mismatches;
- archive traversal and replacement of unowned output;
- compiler, linker, archive-tool, and target-tool selection changes;
- malformed manifests and inconsistent lock or provenance selections;
- incorrect ELF closure, SONAME, RPATH/RUNPATH, and symbol-version evidence;
- partial publication, rollback, and replacement of unowned output.

Avoid tests of private helper structure, exact help prose, every JSON field in
isolation, source-token scans, and repeated permutations of the same malformed
input or fault. Test behavior through the public binding, runtime, artifact, or
CLI when possible.

Catalog resolution and unit tests prove that a combination is modeled and
pinned. They do not prove release qualification. A new glibc, GCC, LLVM,
architecture, runtime, or backend entry requires a real build, smoke test, ELF
audit, loader-closure check, and representative target-like consumer evidence
before documentation may call it qualified.

## Definition of done

- The change respects the SDK, Compiler Kit, runtime, and binding boundaries.
- Focused tests cover the intended behavior and important failures without
  duplicating equivalent cases.
- `make lint`, `make check`, and `git diff --check` pass.
- User-facing English and Chinese documentation agree.
- Public artifact or CLI changes are reflected in the relevant reference docs.
- The tree contains no machine-specific assumptions or generated products.
- Claims of real compatibility are backed by real artifact and target-like
  execution evidence, not catalog presence or mocked tests.
