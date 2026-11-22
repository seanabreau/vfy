"""Public pytest configuration used by the controlled pytest corpus pair."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

SUBPROCESS_RUNPYTEST_NODEIDS = frozenset(
    {
        "testing/test_junitxml.py::test_random_report_log_xdist",
        "testing/test_junitxml.py::test_runs_twice_xdist",
        "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info",
        "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal_count",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_verbose",
        "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_times",
        "testing/test_terminal.py::TestProgressWithTeardown::test_xdist_normal",
    }
)


@pytest.fixture(autouse=True)
def _subprocess_pytester_for_late_hypothesis_autoload(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Isolate named late-autoload cases while preserving pytester's defaults."""
    if request.node.nodeid not in SUBPROCESS_RUNPYTEST_NODEIDS:
        yield
        return

    option = request.config.option
    if not hasattr(option, "runpytest"):
        raise RuntimeError("pytester did not register its public runpytest option")
    original = option.runpytest
    option.runpytest = "subprocess"
    try:
        yield
    finally:
        option.runpytest = original
