"""vfy's public Python API.

Coverage objects load lazily so version checks, configuration commands, and
plug-in discovery do not initialize monitoring machinery.
"""

from typing import TYPE_CHECKING, Any

from ._version import __version__

if TYPE_CHECKING:
    from . import asyncio as asyncio
    from .api import Coverage as Coverage, CoverageData as CoverageData
    from .asyncio_facade import async_fixture as async_fixture
    from .errors import VfyError as VfyError

__all__ = [
    "Coverage",
    "CoverageData",
    "VfyError",
    "__version__",
    "async_fixture",
    "asyncio",
]

_LAZY_EXPORTS = {
    "Coverage": ("vfy.api", "Coverage"),
    "CoverageData": ("vfy.api", "CoverageData"),
    "VfyError": ("vfy.errors", "VfyError"),
    "async_fixture": ("vfy.asyncio_facade", "async_fixture"),
    "asyncio": ("vfy.asyncio", None),
}


def __getattr__(name: str) -> Any:  # noqa: ANN401 - runtime lazy lookup; TYPE_CHECKING exports provide concrete consumer types
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    module = __import__(module_name, fromlist=[attribute] if attribute else ["*"])
    value = module if attribute is None else getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
