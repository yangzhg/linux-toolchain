# Examples

[English](README.md) | [简体中文](README.zh-CN.md)

These files are repository examples and are not installed by the wheel. Run
the commands in this document from a source checkout.

The SDK files illustrate the strict `--spec` schema for two targets:

- `sdk/glibc-2.19-x86_64.json` selects an x86-64 SDK.
- `sdk/glibc-2.36-aarch64.json` selects an AArch64 SDK.

Render either file with:

```bash
linux-toolchain sdk render \
  --spec examples/sdk/glibc-2.19-x86_64.json \
  --workspace out/glibc-2.19-x86_64
```

For normal use, `sdk render --glibc VERSION --arch ARCH` is shorter and resolves
the same pinned catalog. The examples do not define the available version set;
[the recipe catalog](../docs/recipe-catalog.md) does.

`managed/compiler-matrix.json` illustrates the strict managed compiler spec.
It intentionally combines several compiler releases, targets, and C++ runtime
policies so one lock can deduplicate shared source and artifact nodes. Resolve
it with:

```bash
linux-toolchain managed lock \
  --spec examples/managed/compiler-matrix.json \
  --output out/managed.lock.json

linux-toolchain managed artifacts \
  --lock out/managed.lock.json
```

Version selectors in an example are not permanent limits. The installed
managed catalog defines the exact source pins; see
[Managed compilers](../docs/managed-compilers.md).

Audit policies are not checked in as examples. `bind external` writes the policy
for the selected SDK and architecture to `audit-policy.json`, which avoids
copying a policy from the wrong glibc floor or target.
