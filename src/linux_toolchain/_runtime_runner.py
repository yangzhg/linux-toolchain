from __future__ import annotations

import os
import sys
from pathlib import Path

from linux_toolchain.errors import LinuxToolchainError
from linux_toolchain.process import run


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 4:
        print(
            "linux-toolchain runtime runner: expected LOADER INTERPRETER "
            "LIBRARY_PATH PROGRAM [ARG ...]",
            file=sys.stderr,
        )
        return 2
    loader = Path(arguments[0]).resolve()
    interpreter = Path(arguments[1])
    library_path = arguments[2]
    program = Path(arguments[3]).resolve()
    try:
        if not loader.is_file():
            raise LinuxToolchainError(f"SDK loader does not exist: {loader}")
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise LinuxToolchainError(
                f"host interpreter mount point does not exist: {interpreter}"
            )
        if not program.is_file():
            raise LinuxToolchainError(f"smoke executable does not exist: {program}")
        run(("mount", "--make-rprivate", "/"))
        run(("mount", "--bind", loader, interpreter))
        environment = os.environ.copy()
        environment["LD_LIBRARY_PATH"] = library_path
        os.execve(program, (str(program), *arguments[4:]), environment)
    except (LinuxToolchainError, OSError) as error:
        print(f"linux-toolchain runtime runner: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
