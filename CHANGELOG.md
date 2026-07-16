# Changelog

[English](CHANGELOG.md) | [简体中文](CHANGELOG.zh-CN.md)

## 0.1.0

Initial release of the Linux toolchain generator:

- pinned glibc SDK generation for x86-64 and AArch64;
- external and managed GCC/Clang compiler bindings;
- managed libstdc++/libgcc runtimes, including x86-64 libquadmath inputs, and
  shared/static libc++/libc++abi/libunwind plus compiler-rt runtimes;
- one-command managed setup with separate resumable producer state and a
  self-contained machine-local prefix, with no consumer-project configuration;
  work-directory and installation selections are immutable;
- deterministic shell self-extracting managed-toolchain bundles with
  producer-validated binding templates, empty-prefix installation, no target
  Python requirement, install-time launcher naming, and release creation
  directly from a validated installation;
- CMake, shell and optional Conan consumer integrations;
- ELF ABI-floor auditing and packaged consumer smoke tests;
- deterministic manifests, build-input verification and atomic artifact
  publication.
