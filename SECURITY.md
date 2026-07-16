# Security policy

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md)

`linux-toolchain` is a development tool that assembles compiler, linker,
sysroot and runtime inputs. It is not a sandbox or a compiler-argument policy
engine. Compiler and linker arguments are forwarded to the selected tools, and
producer-side toolchains and generated workspaces are treated as local inputs.

Security reports are appropriate when an input can make archive extraction or
publication write outside the selected destination, or when command invocation
runs something other than the selected tool. Schema, ABI, ELF and tool-selection
errors are normally correctness bugs unless they cross one of those boundaries.

## Supported versions

Security fixes are made on the latest released minor line. Artifact readers
accept only the exact schemas and formats implemented by that release.

## Reporting a vulnerability

Use the canonical repository host's private vulnerability-reporting channel when
it is available. If this source was obtained through an internal mirror or a
distribution, contact that distributor's security team and ask it to coordinate
with the upstream maintainers. Do not open a public issue until a maintainer has
confirmed that disclosure is safe.

Include:

- the affected generator version and command;
- the smallest reproducer and the path or command boundary that was crossed;
- whether a crafted source archive or output tree is required.

Do not include credentials, private source archives, production binaries or
other secrets. A report can use synthetic paths and minimal generated artifacts.

## Relevant areas

Changes in these areas need focused boundary tests:

- source archive extraction;
- bundle construction, binding-template substitution, and empty-prefix
  installation;
- output containment and replacement of generator-owned directories;
- construction of external process argument arrays.
