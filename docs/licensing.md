# License files in published artifacts

[English](licensing.md) | [简体中文](zh-CN/licensing.md)

Generated toolchains redistribute code and data from several upstream
projects. The repository license covers the generator only; it does not replace
the terms attached to an SDK, compiler, target tool or runtime payload.

## Files included with each artifact

The following generated artifacts contain a top-level `licenses/` directory:

- an SDK: notices extracted from the checksum-verified glibc, Linux,
  compiler-backend GCC and binutils source archives;
- a managed Compiler Kit: GCC or llvm-project notices, binutils notices copied
  from the SDK that supplied the target tools, and Ubuntu package copyright
  files for each vendored host DSO;
- a raw managed GCC or LLVM runtime and its published runtime export: notices
  from the pinned compiler source, including GCC's Runtime Library Exception or
  the relevant LLVM runtime component licenses.

SDK `manifest.json` and raw managed `artifact.json` contain an exhaustive
license inventory. A published managed runtime carries the same inventory in
`managed-publication.json`. Each entry is a normalized artifact-relative path
below `licenses/`. Readers require the listed files and each component's
required notices. The inventory records what is shipped; it is not a
per-file content-attestation mechanism.

The managed builder resolves a vendored host DSO to its installed Ubuntu
package with `dpkg-query`, copies `/usr/share/doc/<package>/copyright`, and
records the library-to-package mapping in `licenses/ubuntu/dependencies.tsv`.
The artifact is rejected unless every vendored DSO has a mapping and copyright
file. Compiler Kits similarly require the binutils `COPYING` file inherited
from the SDK source evidence.

## What this inventory does not decide

The inventory records which notices accompanied one generated artifact. It does
not classify licenses, decide whether a particular distribution is permitted,
or generate attribution for an independently imported external runtime.
Distributors remain responsible for reviewing the exact upstream terms and for
shipping any additional material their product or jurisdiction requires.
