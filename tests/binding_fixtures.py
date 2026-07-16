def sdk_manifest(
    *,
    arch: str = "x86_64",
    triplet: str = "x86_64-portable-linux-gnu",
    cpu: str = "x86-64",
) -> dict[str, object]:
    return {
        "schema": "linux-toolchain-sdk",
        "format": 1,
        "compatibility_scope": "glibc-floor",
        "target": {
            "arch": arch,
            "vendor": "portable",
            "libc": "glibc",
            "libc_version": "2.18",
            "linux_headers": "3.10.108",
            "minimum_kernel": "3.10.0" if arch == "aarch64" else "3.2.0",
            "cpu": cpu,
            "triplet": triplet,
        },
    }
