from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from linux_toolchain.errors import ConfigurationError


def canonical_json_bytes(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"value is not canonical JSON: {error}") from error
    return text.encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def read_json_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"cannot read {context} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must contain a JSON object")
    return value


def object_value(
    value: object,
    required: set[str],
    context: str,
    *,
    optional: set[str] | None = None,
    allowed: set[str] | None = None,
) -> dict[str, Any]:
    """Return a strict JSON object with one exact set of accepted keys."""

    if optional is not None and allowed is not None:
        raise ValueError("object schema cannot use both optional and allowed keys")
    if not isinstance(value, dict):
        raise ConfigurationError(f"{context} must be an object")
    accepted = allowed if allowed is not None else required | (optional or set())
    missing = sorted(required.difference(value))
    unknown = sorted(set(value).difference(accepted))
    if missing:
        raise ConfigurationError(f"{context} is missing: {', '.join(missing)}")
    if unknown:
        raise ConfigurationError(f"{context} has unknown keys: {', '.join(unknown)}")
    return value


def non_empty_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{context} must be a non-empty string")
    return value


def positive_integer(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigurationError(f"{context} must be a positive integer")
    return value


def relative_posix_path(value: object, context: str) -> str:
    text = non_empty_string(value, context)
    if "\\" in text:
        raise ConfigurationError(f"{context} must use POSIX path separators")
    path = PurePosixPath(text)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ConfigurationError(f"{context} must be a normalized relative path")
    if path.as_posix() != text:
        raise ConfigurationError(f"{context} is not a normalized relative path")
    return text
