# Release qualification

[English](release-qualification.md) | [简体中文](zh-CN/release-qualification.md)

This page is the index of toolchain combinations that the project claims as
release-qualified. Catalog entries and passing unit tests prove that inputs are
modeled and that generator policy is enforced; they do not qualify a real
bundle.

## Current ledger

No release-qualified cells are recorded in this checkout. Catalog selections
remain available for production and validation, but documentation must not
describe one as qualified until its evidence is linked here.

## What constitutes one cell

A qualification result belongs to one exact combination of:

- source revision or release tag and bundle identity;
- producer architecture, Compiler Kit version and host glibc floor;
- target architecture, SDK glibc floor and runtime versions;
- Clang's selected GCC runtime provider, when applicable;
- published linker set and host build-tools versions;
- consumer integration and the minimum-host environment used for execution.

Changing one of these inputs creates another cell. Jobs, output directories and
download mirrors are execution details unless an artifact identity explicitly
includes them.

## Required evidence

A qualified cell retains all of the following:

1. The SDK, build-tools, Compiler Kit, runtime, binding and bundle manifests,
   plus verified source identities.
2. A clean producer build with the compiler, runtime and build-tools ELF audits.
3. Installation and launcher execution on the declared minimum host.
4. C and C++ compile/link evidence, shared and fully static links, every
   published linker, and every published C++ runtime. A dual-runtime Clang cell
   must exercise both libstdc++ and libc++.
5. Recursive loader-closure and symbol-version evidence for the installed
   outputs.
6. A representative target-like consumer build and execution result. An
   embedded library additionally needs its real host-process loading test.
7. Reproducible commands, relevant environment details and complete logs or
   immutable CI/release links.

Mocked tests, catalog resolution, a successful source download or one compiler
verification alone are not release evidence.

## Published results

| Release | Qualification cell | Evidence | Status |
| --- | --- | --- | --- |
| None | None | No evidence record has been published | Not qualified |

Add a row only after the complete evidence above exists. Link to an immutable
release asset, CI run or checked-in report that identifies the exact cell.
Partial runs and known issues belong in the evidence report, but do not receive
`qualified` status.
