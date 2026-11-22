"""Expose the vfy under test to an interpreter without its surrounding dependencies."""

import atexit
import importlib.util
import shutil
import tempfile
from functools import cache
from pathlib import Path


@cache
def isolated_vfy_path() -> str:
    """Return a PYTHONPATH entry containing only the active vfy package."""
    checkout = Path(__file__).resolve().parents[2] / "python"
    spec = importlib.util.find_spec("vfy")
    if spec is None or spec.origin is None:
        return str(checkout)
    package = Path(spec.origin).resolve().parent
    if package == checkout / "vfy":
        return str(checkout)

    # The clean interpreter must see the wheel's extension without discovering
    # pytest and pluggy alongside vfy in the parent interpreter's site-packages.
    root = tempfile.mkdtemp(prefix="vfy-only-import-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    shutil.copytree(package, Path(root) / "vfy", ignore=shutil.ignore_patterns("__pycache__"))
    return root
