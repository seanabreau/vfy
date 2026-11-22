"""Identity checks for the supported upstream pytest-benchmark adapter."""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

from vfy.native import pytest_plugin_version_supported

from .plugin_support import installed_plugin_distribution

# pytest registers the entry point as `benchmark`; `-p` can also name the module.
PYTEST_BENCHMARK_PLUGIN_NAMES = frozenset({"benchmark", "pytest_benchmark.plugin"})
# The session object the upstream plug-in registers during `pytest_configure`.
PYTEST_BENCHMARK_SESSION_NAME = "pytest-benchmark"
# Hook specifications pytest-benchmark adds; only its own reporting code calls them.
PYTEST_BENCHMARK_HOOKS = frozenset(
    {
        "pytest_benchmark_compare_machine_info",
        "pytest_benchmark_generate_commit_info",
        "pytest_benchmark_generate_json",
        "pytest_benchmark_generate_machine_info",
        "pytest_benchmark_group_stats",
        "pytest_benchmark_scale_unit",
        "pytest_benchmark_update_commit_info",
        "pytest_benchmark_update_json",
        "pytest_benchmark_update_machine_info",
    }
)


def _supported_version(version: str) -> bool:
    return pytest_plugin_version_supported("benchmark", version)


def pytest_benchmark_supported_version() -> bool:
    """Return whether an installed pytest-benchmark supports the native adapter."""
    installed = installed_plugin_distribution("pytest-benchmark")
    return installed is not None and _supported_version(installed.version)


def _installed_module_file(relative: str) -> Path | None:
    installed = installed_plugin_distribution("pytest-benchmark")
    if installed is None or not _supported_version(installed.version):
        return None
    return Path(str(installed.locate_file(relative))).resolve()


def trusted_pytest_benchmark_plugin(plugin: object, plugin_name: str) -> bool:
    """Recognize the installed upstream plug-in module or its registered session."""
    if plugin_name == PYTEST_BENCHMARK_SESSION_NAME:
        module = type(plugin).__module__
        expected = _installed_module_file("pytest_benchmark/session.py")
        source = getattr(sys.modules.get(module), "__file__", None)
        return (
            module == "pytest_benchmark.session"
            and type(plugin).__name__ == "BenchmarkSession"
            and isinstance(source, str)
            and expected is not None
            and Path(source).resolve() == expected
        )
    if (
        plugin_name not in PYTEST_BENCHMARK_PLUGIN_NAMES
        or not isinstance(plugin, ModuleType)
        or plugin.__name__ != "pytest_benchmark.plugin"
    ):
        return False
    origin = getattr(plugin, "__file__", None)
    expected = _installed_module_file("pytest_benchmark/plugin.py")
    return isinstance(origin, str) and expected is not None and Path(origin).resolve() == expected


def benchmark_plugin_loaded(manager: object) -> bool:
    """Return whether the upstream plug-in is registered under a pytest load name."""
    get_plugin = getattr(manager, "get_plugin", None)
    return get_plugin is not None and any(
        get_plugin(name) is not None for name in PYTEST_BENCHMARK_PLUGIN_NAMES
    )


def disable_worker_benchmarks(config: object) -> None:
    """Run benchmarked functions once in a native worker, without measuring.

    Upstream disables measurement under pytest-xdist for the same reason:
    concurrent workers make timings unreliable, and each worker's results
    would be summarized and saved separately.
    """
    session = getattr(config, "_benchmarksession", None)
    if session is not None and not session.skip and not session.disabled:
        session.disabled = True


def worker_benchmarks_note(config: object, benchmark_fixture: bool) -> str | None:
    """Explain unmeasured benchmarks after a run that used native workers."""
    session = getattr(config, "_benchmarksession", None)
    if session is None or session.skip or session.disabled:
        return None
    if not benchmark_fixture:
        return None
    return (
        "vfy: pytest-benchmark does not measure benchmarks in native workers; "
        "pass --workers=1 to measure them"
    )


def pytest_runtest_call(item: object) -> Generator[None, None, None]:
    """Mark pytest-benchmark's fixture skipped when the test body skips.

    Upstream sets this flag from ``pytest_runtest_makereport`` when a report's
    outcome is ``skipped``; native execution does not call that hook. Without
    the flag, teardown warns that a skipped or expected-to-fail test never used
    the fixture. The native lifecycle runs this wrapper around the call phase
    while pytest-benchmark is loaded, and throws the test's exception into it.
    """
    import pytest  # noqa: PLC0415 - pytest is loaded by the time tests run

    try:
        yield
    except BaseException as error:
        # A failure under an xfail marker is reported as xfailed, a skipped outcome.
        skipped = isinstance(error, (pytest.skip.Exception, pytest.xfail.Exception)) or (
            getattr(item, "get_closest_marker", lambda _name: None)("xfail") is not None
        )
        fixture = getattr(item, "funcargs", {}).get("benchmark")
        if skipped and fixture is not None and hasattr(fixture, "skipped"):
            fixture.skipped = True
        raise
