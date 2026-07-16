from __future__ import annotations

import argparse
import sys
from importlib.resources import files
from pathlib import Path

from linux_toolchain import __version__
from linux_toolchain.container import BUILDER_DOCKERFILE_NAME
from linux_toolchain.diagnostics import DOCTOR_INTEGRATIONS, DOCTOR_WORKFLOWS
from linux_toolchain.integrations import SUPPORTED_INTEGRATIONS
from linux_toolchain.models import SUPPORTED_ARCHITECTURES
from linux_toolchain.smoke import add_arguments as add_smoke_arguments

_ARCHITECTURE_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}
_ARCHITECTURE_METAVAR = "{x86_64,amd64,aarch64,arm64}"
_JSON_HELP = "emit stable JSON instead of human-readable text"


class HelpFormatter(argparse.HelpFormatter):
    def _get_help_string(self, action: argparse.Action) -> str:
        help_text = action.help or ""
        if (
            help_text
            and "(default:" not in help_text
            and "%(default)" not in help_text
            and action.default not in (None, False, argparse.SUPPRESS)
            and isinstance(action.default, (str, int, float))
            and not isinstance(action.default, bool)
            and action.option_strings
        ):
            help_text += " (default: %(default)s)"
        return help_text


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("formatter_class", HelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: {message}\n"
            f"Try '{self.prog} --help' for more information.\n",
        )


def parse_architecture(value: str) -> str:
    return _ARCHITECTURE_ALIASES.get(value, value)


def default_dockerfile() -> Path:
    return Path(
        str(files("linux_toolchain.resources").joinpath(BUILDER_DOCKERFILE_NAME))
    )


def _add_sdk_selection(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--spec",
        type=Path,
        help="strict SDK specification JSON",
    )
    selection.add_argument(
        "--glibc",
        metavar="VERSION",
        help="resolve VERSION through the pinned backend-family catalog",
    )
    parser.add_argument(
        "--arch",
        type=parse_architecture,
        choices=SUPPORTED_ARCHITECTURES,
        metavar=_ARCHITECTURE_METAVAR,
        help=(
            "target architecture; required with --glibc; amd64 and arm64 are aliases"
        ),
    )
    parser.add_argument("--name", help="override the generated SDK name")
    parser.add_argument(
        "--minimum-kernel",
        help="override the recipe or spec's declared minimum Linux version",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="owned workspace; the exported SDK is written to WORKSPACE/sdk",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace a compatible generator-owned workspace",
    )


def _add_sdk_builder(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel crosstool-NG build jobs",
    )
    parser.add_argument(
        "--dockerfile",
        type=Path,
        default=default_dockerfile(),
        help=(
            "packaged builder Dockerfile or a byte-identical copy; modified "
            "Dockerfiles are rejected because base-image provenance is unverified"
        ),
    )
    parser.add_argument("--image", help="Docker builder image name")


def _add_binding_integrations(
    parser: argparse.ArgumentParser,
    *,
    conan_libcxx: bool,
    default_description: str = "cmake and shell",
) -> None:
    parser.add_argument(
        "--integration",
        action="append",
        choices=SUPPORTED_INTEGRATIONS,
        help=(
            "integration to render; repeat for multiple integrations; "
            f"defaults to {default_description}"
        ),
    )
    parser.add_argument(
        "--conan-cppstd",
        help="Conan compiler.cppstd setting; requires --integration conan",
    )
    if conan_libcxx:
        parser.add_argument(
            "--conan-libcxx",
            choices=("libstdc++", "libstdc++11", "libc++"),
            help=(
                "Conan compiler.libcxx setting; inferred from the pinned runtime "
                "when omitted; requires --integration conan"
            ),
        )
    parser.add_argument(
        "--conan-build-type",
        choices=("Debug", "Release", "RelWithDebInfo", "MinSizeRel"),
        help="Conan host-profile build type; requires --integration conan",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="linux-toolchain",
        description=(
            "Generate compiler-independent glibc SDKs, build or bind compiler "
            "toolchains, and audit resulting ELF files."
        ),
        epilog=(
            "Exit status: 0 success; 1 failed diagnostic or ABI-policy check; "
            "2 invalid input or operational failure; 130 interrupted. "
            "Build progress is written to stderr."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser(
        "setup",
        help="build and install a machine-local managed toolchain",
        description=(
            "Build, validate and install one managed toolchain below an independent "
            "prefix. The generated launcher can run consumer commands from any "
            "working directory on this machine. Production is native on x86_64 "
            "and AArch64 hosts."
        ),
    )
    setup.add_argument("compiler", help="managed compiler, for example gcc@12")
    setup.add_argument(
        "--arch",
        type=parse_architecture,
        choices=SUPPORTED_ARCHITECTURES,
        metavar=_ARCHITECTURE_METAVAR,
        help="native target architecture; defaults to the current host architecture",
    )
    setup.add_argument("--glibc", required=True, help="target glibc ABI floor")
    setup.add_argument(
        "--integration",
        default="shell",
        choices=("cmake", "shell", "conan"),
        help="primary consumer smoke integration; all adapters are included",
    )
    setup.add_argument(
        "--runtime",
        help="required for Clang: libc++ or gcc@VERSION; omitted for GCC",
    )
    setup.add_argument(
        "--prefix",
        type=Path,
        help="final installed toolchain directory; required unless --prepare-only",
    )
    setup.add_argument(
        "--work-dir",
        type=Path,
        help="selection-specific setup state; defaults below the user cache directory",
    )
    setup.add_argument(
        "--store-dir",
        type=Path,
        help="shared producer store for reusable SDKs and managed builds",
    )
    setup_advanced = setup.add_argument_group("advanced producer options")
    setup_advanced.add_argument(
        "--host-glibc-floor",
        help=(
            "maximum glibc baseline allowed for Compiler Kit host executables; "
            "defaults to --glibc"
        ),
    )
    setup_advanced.add_argument(
        "--jobs", type=int, default=1, help="parallel build jobs"
    )
    setup_advanced.add_argument(
        "--runner",
        help="optional command prefix for smoke validation",
    )
    setup_advanced.add_argument(
        "--no-path-instructions",
        action="store_true",
        help="omit human shell PATH instructions; stdout still contains the launcher",
    )
    setup_advanced.add_argument(
        "--prepare-only",
        action="store_true",
        help="build and validate producer artifacts without publishing --prefix",
    )
    setup_conan = setup.add_argument_group("Conan options")
    setup_conan.add_argument(
        "--conan-cppstd",
        help="Conan host-profile compiler.cppstd; requires --integration conan",
    )
    setup_conan.add_argument(
        "--conan-build-type",
        choices=("Debug", "Release", "RelWithDebInfo", "MinSizeRel"),
        help="Conan host-profile build type; requires --integration conan",
    )
    setup_conan.add_argument(
        "--conan-build-profile",
        help=(
            "native build profile for the producer Conan smoke; requires "
            "--integration conan"
        ),
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help=(
            "repair or replace matching generator-owned selection outputs; "
            "reuse valid producer artifacts"
        ),
    )
    setup.set_defaults(handler="setup_toolchain")

    doctor = commands.add_parser(
        "doctor",
        help="check local build and integration prerequisites",
        description=(
            "Check production prerequisites and the selected consumer "
            "integration tools without changing the system."
        ),
    )
    doctor.add_argument(
        "--workflow",
        choices=DOCTOR_WORKFLOWS,
        default="all",
        help=(
            "classify prerequisites for one workflow; all conservatively "
            "requires every production capability"
        ),
    )
    doctor.add_argument(
        "--integration",
        action="append",
        choices=DOCTOR_INTEGRATIONS,
        help=(
            "packaged integration qualification whose executable prerequisites "
            "must be available; repeat to check multiple integrations; consumer "
            "workflow defaults to cmake"
        ),
    )
    doctor_output = doctor.add_mutually_exclusive_group()
    doctor_output.add_argument("--json", action="store_true", help=_JSON_HELP)
    doctor_output.add_argument(
        "--summary",
        action="store_true",
        help="print one line on success and full details on failure",
    )
    doctor.set_defaults(handler="doctor")

    sdk = commands.add_parser("sdk", help="render and build glibc SDKs")
    sdk_commands = sdk.add_subparsers(dest="sdk_command", required=True)

    list_sdks = sdk_commands.add_parser(
        "list", help="list available pinned SDK catalog entries"
    )
    list_sdks.add_argument(
        "--arch",
        type=parse_architecture,
        choices=SUPPORTED_ARCHITECTURES,
        metavar=_ARCHITECTURE_METAVAR,
        help="show only one target architecture; amd64 and arm64 are aliases",
    )
    list_sdks.add_argument("--json", action="store_true", help=_JSON_HELP)
    list_sdks.set_defaults(handler="list_sdks")

    render = sdk_commands.add_parser(
        "render",
        help="render a reviewable crosstool-NG workspace",
        description=(
            "Resolve an SDK selection and render its immutable build inputs "
            "without starting Docker."
        ),
    )
    _add_sdk_selection(render)
    render.set_defaults(handler="render_sdk")

    create_sdk = sdk_commands.add_parser(
        "create",
        help="render, build and export an SDK in one command",
        description=(
            "Create a compiler-independent glibc SDK. Build logs go to stderr; "
            "stdout contains the exported SDK path."
        ),
    )
    _add_sdk_selection(create_sdk)
    _add_sdk_builder(create_sdk)
    create_sdk.set_defaults(handler="create_sdk")

    build = sdk_commands.add_parser(
        "build",
        help="build a rendered SDK workspace",
        description=(
            "Build and export a rendered workspace. The SDK is written to "
            "WORKSPACE/sdk and its path is printed on stdout."
        ),
    )
    build.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="workspace produced by `linux-toolchain sdk render`",
    )
    _add_sdk_builder(build)
    build.set_defaults(handler="build_sdk")

    bind = commands.add_parser("bind", help="create external or managed bindings")
    bind_commands = bind.add_subparsers(dest="bind_command", required=True)
    create = bind_commands.add_parser(
        "external",
        help="bind an externally installed compiler",
        description=(
            "Create a compiler and glibc-floor binding around an external GCC "
            "or Clang, then render the selected build-system integrations. A "
            "pinned runtime is required unless the development-only unpinned "
            "mode is selected explicitly."
        ),
    )
    create.add_argument("--sdk", required=True, type=Path)
    create.add_argument("--cc", required=True, help="C compiler driver")
    create.add_argument("--cxx", required=True, help="C++ compiler driver")
    runtime_selection = create.add_mutually_exclusive_group(required=True)
    runtime_selection.add_argument(
        "--runtime",
        type=Path,
        help="validated GCC runtime export to pin for headers, CRT, and libraries",
    )
    runtime_selection.add_argument(
        "--allow-unpinned-runtime",
        action="store_true",
        help=(
            "development only: use the compiler's unbounded libstdc++/libgcc closure"
        ),
    )
    create.add_argument("--output", required=True, type=Path)
    _add_binding_integrations(create, conan_libcxx=True)
    create.add_argument("--force", action="store_true")
    create.set_defaults(handler="create_binding")

    managed_binding = bind_commands.add_parser(
        "managed", help="bind a managed compiler kit and target runtime"
    )
    managed_binding.add_argument("--sdk", required=True, type=Path)
    managed_binding.add_argument("--compiler-kit", required=True, type=Path)
    managed_binding.add_argument("--lock", required=True, type=Path)
    managed_binding.add_argument("--variant", required=True)
    managed_binding.add_argument("--runtime", required=True, type=Path)
    managed_binding.add_argument("--output", required=True, type=Path)
    _add_binding_integrations(managed_binding, conan_libcxx=True)
    managed_binding.add_argument("--force", action="store_true")
    managed_binding.set_defaults(handler="create_managed_binding")

    bundle = commands.add_parser(
        "bundle",
        help="create portable shell-based managed toolchain installers",
    )
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_create = bundle_commands.add_parser(
        "create",
        help="package a validated setup selection",
        description=(
            "Package validated prepared artifacts or an existing installation "
            "as a shell-only installer."
        ),
    )
    bundle_source = bundle_create.add_mutually_exclusive_group(required=True)
    bundle_source.add_argument(
        "--prefix",
        type=Path,
        help="installed prefix populated by linux-toolchain setup",
    )
    bundle_source.add_argument(
        "--config",
        type=Path,
        help="setup.json whose validated producer artifacts should be packaged",
    )
    bundle_create.add_argument(
        "--state-directory",
        type=Path,
        help="state directory for --config; defaults beside setup.json",
    )
    bundle_create.add_argument("--output", required=True, type=Path)
    bundle_create.add_argument(
        "--id", help="portable installation identifier; defaults from lock and variant"
    )
    bundle_create.add_argument("--force", action="store_true")
    bundle_create.set_defaults(handler="create_setup_bundle")

    bundle_artifacts = bundle_commands.add_parser(
        "create-artifacts",
        help="package explicitly selected low-level managed artifacts",
        description=(
            "Advanced release interface. Validate and package one SDK, Compiler "
            "Kit, runtime publication, managed lock and binding template."
        ),
    )
    bundle_artifacts.add_argument("--sdk", required=True, type=Path)
    bundle_artifacts.add_argument("--compiler-kit", required=True, type=Path)
    bundle_artifacts.add_argument("--runtime", required=True, type=Path)
    bundle_artifacts.add_argument("--lock", required=True, type=Path)
    bundle_artifacts.add_argument("--variant", required=True)
    bundle_artifacts.add_argument("--output", required=True, type=Path)
    bundle_artifacts.add_argument("--id")
    _add_binding_integrations(
        bundle_artifacts,
        conan_libcxx=True,
        default_description="cmake, shell and conan",
    )
    bundle_artifacts.add_argument("--force", action="store_true")
    bundle_artifacts.set_defaults(handler="create_artifact_bundle")

    runtime = commands.add_parser(
        "runtime", help="import compiler runtime artifacts without compiler executables"
    )
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    import_gcc = runtime_commands.add_parser(
        "import-gcc", help="import a filtered GCC target runtime prefix"
    )
    import_gcc.add_argument("--prefix", required=True, type=Path)
    import_gcc.add_argument("--glibc-floor", required=True)
    import_gcc.add_argument(
        "--arch",
        required=True,
        type=parse_architecture,
        choices=SUPPORTED_ARCHITECTURES,
        metavar=_ARCHITECTURE_METAVAR,
        help="target architecture; amd64 and arm64 are aliases",
    )
    import_gcc.add_argument("--output", required=True, type=Path)
    import_gcc.add_argument(
        "--licenses",
        type=Path,
        help="artifact root containing licenses/; defaults to --prefix",
    )
    import_gcc.add_argument(
        "--probe-gxx",
        type=Path,
        help=(
            "absolute path to a temporary g++/xg++ used only to identify "
            "the runtime prefix"
        ),
    )
    import_gcc.add_argument("--force", action="store_true")
    import_gcc.set_defaults(handler="import_gcc_runtime")

    import_llvm = runtime_commands.add_parser(
        "import-llvm",
        help="import a filtered libc++ and compiler-rt target runtime prefix",
    )
    import_llvm.add_argument("--prefix", required=True, type=Path)
    import_llvm.add_argument("--llvm-version", required=True)
    import_llvm.add_argument("--glibc-floor", required=True)
    import_llvm.add_argument(
        "--arch",
        required=True,
        type=parse_architecture,
        choices=SUPPORTED_ARCHITECTURES,
        metavar=_ARCHITECTURE_METAVAR,
        help="target architecture; amd64 and arm64 are aliases",
    )
    import_llvm.add_argument(
        "--target",
        required=True,
        help="target Linux glibc triplet recorded in the runtime manifest",
    )
    llvm_source_proof = import_llvm.add_mutually_exclusive_group(required=True)
    llvm_source_proof.add_argument(
        "--provenance",
        type=Path,
        help="managed artifact.json or its artifact directory",
    )
    llvm_source_proof.add_argument(
        "--probe-clang",
        type=Path,
        help="absolute Clang executable used to prove an external runtime",
    )
    import_llvm.add_argument("--output", required=True, type=Path)
    import_llvm.add_argument(
        "--licenses",
        type=Path,
        help="artifact root containing licenses/; defaults to --prefix",
    )
    import_llvm.add_argument("--force", action="store_true")
    import_llvm.set_defaults(handler="import_llvm_runtime")

    managed = commands.add_parser(
        "managed", help="resolve and build pinned managed compiler matrices"
    )
    managed_commands = managed.add_subparsers(dest="managed_command", required=True)
    managed_catalog = managed_commands.add_parser(
        "catalog", help="list pinned managed compiler releases"
    )
    managed_catalog.add_argument("--family", choices=("gcc", "clang"))
    managed_catalog.add_argument("--json", action="store_true", help=_JSON_HELP)
    managed_catalog.set_defaults(handler="list_managed_catalog")

    managed_lock = managed_commands.add_parser(
        "lock", help="resolve a managed compiler spec into an immutable build DAG"
    )
    managed_lock.add_argument("--spec", required=True, type=Path)
    managed_lock.add_argument("--output", required=True, type=Path)
    managed_lock.add_argument("--force", action="store_true")
    managed_lock.set_defaults(handler="lock_managed")

    managed_artifacts = managed_commands.add_parser(
        "artifacts",
        help="list build artifacts and binding variants in a lockfile",
    )
    managed_artifacts.add_argument("--lock", required=True, type=Path)
    managed_artifacts.add_argument("--json", action="store_true", help=_JSON_HELP)
    managed_artifacts.set_defaults(handler="list_managed_artifacts")

    managed_assemble = managed_commands.add_parser(
        "assemble",
        help="build and bind one lock variant end to end",
        description=(
            "Resolve a variant's Compiler Kit and runtime IDs from the lock, "
            "resume validated artifacts, publish the runtime and create a binding. "
            "Progress and build logs go to stderr."
        ),
    )
    managed_assemble.add_argument(
        "--lock",
        required=True,
        type=Path,
        help="immutable lock produced by `linux-toolchain managed lock`",
    )
    managed_assemble.add_argument(
        "--variant",
        required=True,
        help="variant ID shown by `linux-toolchain managed artifacts`",
    )
    managed_assemble.add_argument(
        "--sdk-workspace",
        required=True,
        type=Path,
        help="SDK build workspace containing sdk/ and toolchain/bin/",
    )
    managed_assemble.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="owned root for resumable compiler and runtime builds",
    )
    managed_assemble.add_argument(
        "--output",
        required=True,
        type=Path,
        help="final managed binding directory",
    )
    managed_assemble.add_argument(
        "--compiler-backend-workspace",
        required=True,
        type=Path,
        help="built crosstool-NG compiler backend workspace",
    )
    managed_assemble.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="parallel compiler build jobs",
    )
    _add_binding_integrations(managed_assemble, conan_libcxx=True)
    managed_assemble.add_argument(
        "--dockerfile",
        type=Path,
        help="packaged builder Dockerfile or a byte-identical copy",
    )
    managed_assemble.add_argument("--image", help="Docker builder image name")
    managed_assemble.add_argument(
        "--rebuild",
        action="store_true",
        help="recreate matching generator-owned artifact workspaces",
    )
    managed_assemble.add_argument(
        "--force",
        action="store_true",
        help="replace the generator-owned binding at --output",
    )
    managed_assemble.add_argument("--json", action="store_true", help=_JSON_HELP)
    managed_assemble.set_defaults(handler="assemble_managed")

    managed_render = managed_commands.add_parser(
        "render", help="render a workspace for one locked managed artifact"
    )
    managed_render.add_argument("--lock", required=True, type=Path)
    managed_render.add_argument("--artifact", required=True)
    managed_render.add_argument("--sdk", required=True, type=Path)
    managed_render.add_argument("--target-tools", required=True, type=Path)
    managed_render.add_argument(
        "--compiler-backend-workspace", required=True, type=Path
    )
    managed_render.add_argument("--workspace", required=True, type=Path)
    managed_render.add_argument("--force", action="store_true")
    managed_render.set_defaults(handler="render_managed")

    managed_fetch = managed_commands.add_parser(
        "fetch", help="fetch and verify the source for one rendered artifact"
    )
    managed_fetch.add_argument("--lock", required=True, type=Path)
    managed_fetch.add_argument("--artifact", required=True)
    managed_fetch.add_argument("--workspace", required=True, type=Path)
    managed_fetch.set_defaults(handler="fetch_managed")

    managed_build = managed_commands.add_parser(
        "build", help="build one rendered and fetched artifact in Docker"
    )
    managed_build.add_argument("--lock", required=True, type=Path)
    managed_build.add_argument("--artifact", required=True)
    managed_build.add_argument("--workspace", required=True, type=Path)
    managed_build.add_argument(
        "--dockerfile",
        type=Path,
        help="packaged builder Dockerfile or a byte-identical copy",
    )
    managed_build.add_argument("--image", help="Docker builder image name")
    managed_build.add_argument(
        "--jobs", type=int, default=1, help="parallel compiler build jobs"
    )
    managed_build.set_defaults(handler="build_managed")

    managed_publish = managed_commands.add_parser(
        "publish-runtime",
        help="publish one raw managed runtime for use by a binding",
    )
    managed_publish.add_argument("--lock", required=True, type=Path)
    managed_publish.add_argument("--artifact", required=True)
    managed_publish.add_argument(
        "--artifact-dir",
        required=True,
        type=Path,
        help="managed build output containing artifact.json and runtime/",
    )
    managed_publish.add_argument("--output", required=True, type=Path)
    managed_publish.add_argument("--force", action="store_true")
    managed_publish.set_defaults(handler="publish_managed_runtime")

    conan = commands.add_parser("conan", help="generate Conan integration files")
    conan_commands = conan.add_subparsers(dest="conan_command", required=True)
    settings = conan_commands.add_parser(
        "settings",
        help="write Linux toolchain's libc and Linux baseline settings extension",
    )
    settings.add_argument("--output", required=True, type=Path)
    settings.add_argument("--force", action="store_true")
    settings.set_defaults(handler="write_conan_settings")

    smoke = commands.add_parser(
        "smoke",
        help="build, audit and run the packaged integration project",
        description=(
            "Validate a pinned-runtime binding with the packaged C++/ASM "
            "project, ELF policy and isolated dynamic-loader closure."
        ),
    )
    add_smoke_arguments(smoke)
    smoke.set_defaults(handler="smoke")

    audit = commands.add_parser("audit", help="enforce ABI policy on final ELF files")
    audit.add_argument("--policy", required=True, type=Path)
    audit.add_argument("--recursive", action="store_true")
    audit.add_argument("--json", action="store_true", help=_JSON_HELP)
    audit.add_argument("paths", nargs="+", type=Path)
    audit.set_defaults(handler="audit")
    return parser
