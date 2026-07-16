# Bolt acceptance test

This test treats Bolt as an external consumer of a generated binding. It builds
the caller-provided checkout through Bolt's public Makefile, runs the generic
integration smoke test, and audits the selected Bolt outputs.

This repository-only test is excluded from the wheel and source distribution.

## Prerequisites

- an installed `linux-toolchain` command;
- a Bolt checkout prepared for its Conan-backed Make workflow;
- a binding containing the CMake and Conan integrations;
- a Conan home available to the Bolt build.

Create the binding with both integrations:

```text
--integration cmake --integration conan
```

Install the generated custom settings in the Conan home:

```bash
CONAN_HOME="$(conan config home)"
linux-toolchain conan settings \
  --output "${CONAN_HOME}/settings_user.yml"
```

A matching settings file is accepted. A different file is rejected unless the
caller reviews the replacement and supplies `--force`. Dependency preparation
and Conan remote configuration belong to the caller; the harness does not clone,
download, patch, vendor, or clean Bolt.

## Run

```bash
python3 tests/consumers/bolt/acceptance.py \
  --bolt-checkout /path/to/bolt \
  --binding /path/to/binding \
  --conan-home "${CONAN_HOME}" \
  --audit-path _build/Release \
  --build-type Release \
  --jobs 8
```

The harness:

1. runs consumer diagnostics for the selected integrations;
2. validates the binding with the generic CMake smoke project;
3. invokes `make release PROFILE=/path/to/binding/conan/host.profile` in the
   supplied Bolt checkout;
4. recursively audits the selected Bolt artifacts with the binding policy.

Inspect the command plan without requiring either path to exist:

```bash
python3 tests/consumers/bolt/acceptance.py \
  --bolt-checkout /path/to/bolt \
  --binding /path/to/binding \
  --audit-path /path/to/bolt-product \
  --dry-run
```

## Audit scope

Relative `--audit-path` and `--work-dir` values resolve below the Bolt checkout.
Repeat `--audit-path` to select shared libraries, executables, or deployment
directories. Without that option, the harness audits `_build/Release`
recursively.

Use `--skip-build` to audit a completed build, `--skip-smoke` to omit the generic
smoke project, or `--smoke-integration shell|conan` to exercise another binding
adapter. `--build-type` configures the generic smoke project; `--make-target`
selects the Bolt build configuration. Repeat `--make-variable NAME=VALUE` for
additional Bolt Makefile settings:

```bash
python3 tests/consumers/bolt/acceptance.py \
  --bolt-checkout /path/to/bolt \
  --binding /path/to/binding \
  --conan-home "${CONAN_HOME}" \
  --audit-path _build/Release/lib/libbolt.so \
  --make-variable BUILD_VERSION=acceptance \
  --make-variable 'CONAN_CONFIG=-c bolt/*:tools.build:skip_test=False'
```

`PROFILE` is derived from `--binding` and cannot be overridden. Use a fresh Bolt
build directory when changing bindings, or follow the cleanup guidance for the
tested Bolt revision.
