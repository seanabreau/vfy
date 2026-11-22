"""Platform requirements declared by individual integration tests."""

import sys

import pytest

requires_native_pytest = pytest.mark.skipif(
    sys.platform == "win32", reason="native pytest requires Unix"
)

requires_independent_mutation = pytest.mark.skipif(
    sys.platform != "linux", reason="independent mutation requires Linux descendant reaping"
)

native_engines = pytest.mark.parametrize(
    "engine", ["stock", pytest.param("native", marks=requires_native_pytest)]
)
