"""Upstream protocol warning scopes retain their filter order and callbacks."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")) or importlib.util.find_spec("xdist") is None,
    reason="native xdist execution requires Linux or macOS and pytest-xdist",
)


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize("warnings_enabled", [True, False], ids=["warnings", "no-warnings"])
def test_protocol_keeps_installed_warning_filters_without_repeating_helpers(
    tmp_path: Path, workers: str, warnings_enabled: bool
) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            dedent("""\
                import os
                import warnings
                import pytest
                import _pytest.warnings as warning_plugin
                if not hasattr(warning_plugin, 'apply_warning_filters'):
                    import _pytest.config as warning_plugin

                filter_calls = 0
                protocol_filter_calls = None

                @pytest.hookimpl(trylast=True)
                def pytest_configure(config):
                    original = warning_plugin.apply_warning_filters
                    def counted_filters(config_filters, cmdline_filters):
                        global filter_calls
                        filter_calls += 1
                        return original(config_filters, cmdline_filters)
                    warning_plugin.apply_warning_filters = counted_filters
                    config.add_cleanup(
                        lambda: setattr(warning_plugin, 'apply_warning_filters', original)
                    )

                def pytest_runtest_logstart(nodeid, location):
                    global protocol_filter_calls
                    protocol_filter_calls = filter_calls
                    warnings.filterwarnings(
                        'error', message='logstart filter sentinel', category=UserWarning
                    )
                """),
        )
        marker = (
            "@pytest.mark.filterwarnings('ignore:logstart filter sentinel:UserWarning')\n"
            if warnings_enabled
            else ""
        )
        project.write(
            "test_warning_scopes.py",
            dedent("""\
                import warnings
                import pytest
                import conftest

                def test_helpers_are_not_repeated_after_logstart():
                    assert conftest.protocol_filter_calls is not None
                    assert conftest.filter_calls == conftest.protocol_filter_calls
                """)
            + f"    assert conftest.protocol_filter_calls {'>' if warnings_enabled else '=='} 0\n\n"
            + marker
            + dedent("""\
                def test_logstart_filter_keeps_its_precedence():
                    with pytest.raises(UserWarning, match='logstart filter sentinel'):
                        warnings.warn('logstart filter sentinel', UserWarning)
                """),
        )
        arguments = [f"--numprocesses={workers}", "--dist=loadfile", "-q"]
        if not warnings_enabled:
            arguments += ["-p", "no:warnings"]
        result = (
            project.run_pytest("-p", "xdist.plugin", *arguments, load_xdist=False)
            if route == "upstream"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--no-replay",
                "--no-cov",
                *arguments,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout, result.stdout + result.stderr
