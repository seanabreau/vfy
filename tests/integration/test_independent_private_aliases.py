"""Names that installed plug-ins import resolve on the independent engine as in pytest."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROBE = """
import importlib
import pytest

CODE_NAMES = (
    "ExceptionChainRepr", "ExceptionInfo", "ExceptionRepr", "ReprEntry", "ReprExceptionInfo",
    "ReprFileLocation", "ReprFuncArgs", "ReprLocals", "ReprTraceback", "TerminalRepr",
    "Traceback", "TracebackEntry",
)

def test_names(pytestconfig):
    from _pytest._code import ExceptionInfo as package_info
    code = importlib.import_module("_pytest._code.code")
    present = {name: hasattr(code, name) for name in CODE_NAMES}
    assert all(present.values()), present
    assert package_info is code.ExceptionInfo is pytest.ExceptionInfo
    assert isinstance(pytestconfig.pluginmanager, pytest.PytestPluginManager)
    assert hasattr(pytest.PytestPluginManager, "add_hookspecs")
    assert issubclass(code.ExceptionChainRepr, code.ExceptionRepr)
    assert issubclass(code.ExceptionRepr, code.TerminalRepr)
"""


def test_plugin_visible_private_names_exist_in_stock_and_independent_runs(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_names.py", _PROBE)

    stock = project.run_engine("stock", "test_names.py", "-q")
    candidate = project.run_engine("native", "test_names.py", "-q")

    stock.expect(0)
    candidate.expect(0)
