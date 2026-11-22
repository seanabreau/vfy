"""Source for the private pytest aliases that installed plug-ins import, checked in a project."""

from __future__ import annotations

PRIVATE_ALIASES = """import importlib
import sys


def assert_private_aliases():
    # Plug-ins import these private modules; none may come from a real pytest installation.
    for name in ('_pytest.fixtures', '_pytest.outcomes', '_pytest.python'):
        importlib.import_module(name)
    loaded = [name for name in sys.modules if name == '_pytest' or name.startswith('_pytest.')]
    assert loaded
    for name in loaded:
        origin = (getattr(sys.modules[name], '__file__', None) or '').replace('\\\\', '/')
        assert '/site-packages/_pytest/' not in origin, (name, origin)
"""
