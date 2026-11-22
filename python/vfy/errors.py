"""Public exception hierarchy for vfy."""


class VfyError(Exception):
    """Base class for errors that should be presented without a traceback."""


class ConfigError(VfyError):
    """A configuration file could not be interpreted."""


class PytestConfigurationError(ConfigError):
    """A pytest configuration error with pytest's usage-error exit status."""


class DataError(VfyError):
    """Coverage data is invalid, incompatible, or cannot be combined."""


class NoDataError(DataError):
    """No usable coverage data was found."""


class CoverageWarning(Warning):
    """A condition that Coverage.py reports as a warning, such as an unimported source package."""


class NoSource(VfyError):  # noqa: N818 - Match Coverage.py's public exception name.
    """Measured code refers to a source file that is no longer available."""


class NotPython(VfyError):  # noqa: N818 - Match Coverage.py's public exception name.
    """A measured source file cannot be parsed as Python."""


def _source_failure(path: str, kind: str | None, detail: str | None) -> VfyError | SyntaxError:
    """Translate one Rust source-analysis failure at the Python boundary."""

    if kind == "syntax-error":
        return SyntaxError(detail)
    if kind == "no-source":
        suffix = "." if detail is None else f": {detail}"
        return NoSource(f"No source for code: {path!r}{suffix}")
    suffix = detail or "invalid syntax"
    return NotPython(f"Couldn't parse {path!r} as Python source: {suffix}")


class UnsupportedFeatureError(VfyError):
    """A requested compatibility feature has not been implemented."""
