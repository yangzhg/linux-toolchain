from __future__ import annotations

import re
from dataclasses import dataclass

from linux_toolchain.errors import ConfigurationError

_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


@dataclass(frozen=True, order=True)
class AbiVersion:
    parts: tuple[int, ...]

    @classmethod
    def parse(cls, value: str) -> "AbiVersion":
        if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
            raise ConfigurationError(f"invalid numeric version: {value!r}")
        parts = [int(part) for part in value.split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return cls(tuple(parts))

    def __str__(self) -> str:
        return ".".join(str(part) for part in self.parts)


def major_version(value: str) -> int:
    match = re.match(r"^(\d+)", value)
    if not match:
        raise ConfigurationError(f"cannot determine major version from {value!r}")
    return int(match.group(1))
