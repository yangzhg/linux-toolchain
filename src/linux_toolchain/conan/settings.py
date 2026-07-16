from pathlib import Path

from linux_toolchain.errors import ConfigurationError

SETTINGS_USER_YAML = """os:
  Linux:
    libc:
      - null
      - gnu
      - musl
    libc_version:
      - null
      - ANY
    kernel_headers_version:
      - null
      - ANY
    minimum_kernel_version:
      - null
      - ANY
compiler:
  gcc:
    version:
      - ANY
  clang:
    version:
      - ANY
"""


def write_settings_user(path: Path, *, force: bool = False) -> Path:
    try:
        if path.exists():
            current = path.read_text(encoding="utf-8")
            if current == SETTINGS_USER_YAML:
                return path
            if not force:
                raise ConfigurationError(
                    f"refusing to overwrite existing Conan settings file {path}; "
                    "merge the generated Linux libc keys or pass --force"
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SETTINGS_USER_YAML, encoding="utf-8")
    except ConfigurationError:
        raise
    except OSError as error:
        raise ConfigurationError(
            f"cannot write Conan settings {path}: {error}"
        ) from error
    return path
