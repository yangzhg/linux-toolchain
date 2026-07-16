class LinuxToolchainError(Exception):
    """Base error for expected CLI failures."""


class ConfigurationError(LinuxToolchainError):
    """The requested SDK or binding configuration is invalid."""


class ExternalToolError(LinuxToolchainError):
    """An external tool failed or is unavailable."""
