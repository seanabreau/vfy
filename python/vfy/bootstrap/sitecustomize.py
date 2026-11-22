"""Interpreter bootstrap for vfy-managed subprocesses."""

from __future__ import annotations

import importlib
import os
import sys


def _application_sitecustomize() -> None:
    """Import the application's hook under its usual name, without loading this file again."""
    bootstrap = os.path.realpath(os.path.dirname(__file__))
    # The private directory exists only to find this hook. Application imports
    # use their original search path, including changes made by sitecustomize.
    # Import search ignores symlink loops. Resolve non-strictly so those entries
    # do not prevent the application hook running.
    sys.path[:] = [entry for entry in sys.path if os.path.realpath(entry) != bootstrap]
    sys.modules.pop("sitecustomize")
    # site.execsitecustomize suppresses the missing-hook error. Leaving it to
    # that boundary also preserves the absence of an application module.
    importlib.import_module("sitecustomize")


if os.environ.get("VFY_MUTATION"):
    from vfy import mutation as _vfy_mutation

    _vfy_mutation.install_from_environment()

if "vfy.process_startup" in sys.modules:
    from vfy import process_startup as _vfy_process_startup

    try:
        _application_sitecustomize()
    finally:
        # Test workers measure from the end of interpreter customization, as
        # pytest-cov and the serial runner do.
        if (
            _vfy_process_startup.start_worker() is None
            and os.environ.get("VFY_PROCESS_START") == "1"
        ):
            _vfy_process_startup.start()
else:
    if os.environ.get("VFY_PROCESS_START") == "1":
        from vfy import process_startup as _vfy_process_startup

        _vfy_process_startup.start()
    # Coverage.py's subprocess .pth starts collection before application startup.
    # Let CPython handle errors from the application's hook as it normally does.
    _application_sitecustomize()
