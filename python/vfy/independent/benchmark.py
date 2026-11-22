"""The ``benchmark`` fixture when pytest-benchmark and pytest-codspeed are absent.

Calls follow pytest-benchmark's disabled mode: each target runs once without
timing statistics. Skip and only options select tests. Measurement, profiling,
comparison, and artifact options require the corresponding installed plug-in.
Measurement and presentation configuration is checked when the fixture is requested.
"""

from __future__ import annotations

import argparse
import platform
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import pytest

from vfy import native

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest

    from .protocol import Config, Item, Node, Parser

_FLAGS = (
    "--benchmark-skip",
    "--benchmark-disable",
    "--benchmark-enable",
    "--benchmark-only",
    "--benchmark-autosave",
    "--benchmark-disable-gc",
    "--benchmark-verbose",
    "--benchmark-quiet",
    "--benchmark-cprofile-dump",
)
_VALUES = (
    "--benchmark-max-time",
    "--benchmark-min-time",
    "--benchmark-min-rounds",
    "--benchmark-timer",
    "--benchmark-calibration-precision",
    "--benchmark-warmup",
    "--benchmark-warmup-iterations",
    "--benchmark-sort",
    "--benchmark-group-by",
    "--benchmark-columns",
    "--benchmark-name",
    "--benchmark-save",
    "--benchmark-save-data",
    "--benchmark-json",
    "--benchmark-compare",
    "--benchmark-compare-fail",
    "--benchmark-histogram",
    "--benchmark-storage",
    "--benchmark-netrc",
    "--benchmark-cprofile",
    "--benchmark-cprofile-loops",
    "--benchmark-cprofile-top",
    "--benchmark-time-unit",
)
_MEASUREMENT_FLAGS = ("--benchmark-enable", "--benchmark-disable-gc")
_FIXTURE_VALUES = (
    "--benchmark-max-time",
    "--benchmark-min-time",
    "--benchmark-min-rounds",
    "--benchmark-timer",
    "--benchmark-calibration-precision",
    "--benchmark-warmup",
    "--benchmark-warmup-iterations",
    "--benchmark-sort",
    "--benchmark-group-by",
    "--benchmark-columns",
    "--benchmark-name",
    "--benchmark-time-unit",
)
_DISABLED_OPTIONS = frozenset(
    {
        "--benchmark-warmup",
        "--benchmark-warmup-iterations",
        "--benchmark-sort",
        "--benchmark-group-by",
    }
)


def _warmup(value: str) -> bool:
    value = value.lower().strip()
    if value not in {"auto", "off", "false", "no", "on", "true", "yes", ""}:
        raise argparse.ArgumentTypeError(f"Could not parse value: {value!r}.")
    if value == "auto":
        return platform.python_implementation() == "PyPy"
    return value in {"on", "true", "yes", ""}


def _sort(value: str) -> str:
    value = value.lower().strip()
    if value not in {"min", "max", "mean", "stddev", "name", "fullname"}:
        raise argparse.ArgumentTypeError(f"Unacceptable value: {value!r}.")
    return value


class FixtureAlreadyUsed(Exception):  # noqa: N818 - match pytest-benchmark's public exception
    """The benchmark fixture has already been called in this test."""


class BenchmarkFixture:
    """Run a benchmarked callable once and return its result."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.fullname = name
        self.group: str | None = None
        self.extra_info: dict[str, object] = {}
        self.disabled = True
        self.stats = None
        self._mode: str | None = None

    def _use(self, mode: str) -> None:
        if self._mode is not None:
            raise FixtureAlreadyUsed(
                f"Fixture can only be used once. Previously it was used in {self._mode} mode."
            )
        self._mode = mode

    def __call__(self, function: Callable[..., object], *args: object, **kwargs: object) -> object:
        """Call ``function`` with the given arguments."""
        self._use("benchmark(...)")
        return function(*args, **kwargs)

    def pedantic(
        self,
        target: Callable[..., object],
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        setup: Callable[[], tuple[tuple[object, ...], dict[str, object]] | None] | None = None,
        teardown: Callable[..., Any] | None = None,
        rounds: int = 1,
        warmup_rounds: int = 0,
        iterations: int = 1,
    ) -> object:
        """Run once with pytest-benchmark's disabled-mode argument validation.

        Disabled mode omits warmups, repeated rounds, and teardown, as upstream
        does with ``--benchmark-disable``.
        """
        self._use("benchmark.pedantic(...)")
        if not isinstance(iterations, int) or iterations < 1:
            raise ValueError("Must have positive int for `iterations`.")
        if not isinstance(rounds, int) or rounds < 1:
            raise ValueError("Must have positive int for `rounds`.")
        if not isinstance(warmup_rounds, int) or warmup_rounds < 0:
            raise ValueError("Must have positive int for `warmup_rounds`.")
        if iterations > 1 and setup:
            raise ValueError("Can't use more than 1 `iterations` with a `setup` function.")
        if setup is not None:
            prepared = setup()
            if prepared:
                if args or kwargs:
                    raise TypeError(
                        "Can't use `args` or `kwargs` if `setup` returns the arguments."
                    )
                args, kwargs = prepared
        return target(*args, **(kwargs or {}))

    def weave(self, target: object, **kwargs: object) -> None:
        """Reject instrumentation without the installed benchmark plug-in."""
        raise NotImplementedError("benchmark.weave requires pytest-benchmark")


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("benchmark")
    for flag in _FLAGS:
        group.addoption(flag, action="store_true", default=False)
    for option in _VALUES:
        if option == "--benchmark-time-unit":
            group.addoption(option, default=None, choices=("ns", "us", "ms", "s", "auto"))
        elif option == "--benchmark-warmup":
            group.addoption(option, default=None, nargs="?", type=_warmup)
        elif option == "--benchmark-warmup-iterations":
            group.addoption(option, default=None, type=int)
        elif option == "--benchmark-sort":
            group.addoption(option, default=None, type=_sort)
        else:
            group.addoption(option, default=None)
    codspeed = parser.getgroup("codspeed")
    codspeed.addoption("--codspeed", action="store_true", default=False)
    for option in ("--codspeed-mode", "--codspeed-warmup-time", "--codspeed-max-time"):
        codspeed.addoption(option, default=None)
    codspeed.addoption("--codspeed-max-rounds", default=None)


def pytest_configure(config: Config) -> None:
    supported = {"--benchmark-skip", "--benchmark-disable", "--benchmark-only"}
    for option in (*_FLAGS, *_VALUES):
        if option in (*_MEASUREMENT_FLAGS, *_FIXTURE_VALUES):
            continue
        value = config.getoption(option)
        requested = value is not None if option in _VALUES else bool(value)
        if option not in supported and requested:
            raise native._pytest.IndependentUsageError(f"{option} requires pytest-benchmark")
    for option in (
        "--codspeed",
        "--codspeed-mode",
        "--codspeed-warmup-time",
        "--codspeed-max-time",
        "--codspeed-max-rounds",
    ):
        value = config.getoption(option)
        requested = bool(value) if option == "--codspeed" else value is not None
        if requested:
            raise native._pytest.IndependentUsageError(f"{option} requires pytest-codspeed")
    config.addinivalue_line("markers", "benchmark(**kwargs): benchmark options for this test")
    config.addinivalue_line("markers", "codspeed_benchmark: benchmark this test with CodSpeed")


@pytest.fixture
def benchmark(request: NativeIndependentRequest) -> BenchmarkFixture:
    """Return a fixture that runs a benchmarked callable once."""
    if cast("Config", request.config).getoption("benchmark_skip"):
        pytest.skip("Skipping benchmark (--benchmark-skip active).")
    config = cast("Config", request.config)
    disabled = config.getoption("benchmark_disable") and not config.getoption("benchmark_enable")
    for option in _MEASUREMENT_FLAGS:
        if config.getoption(option) and (option == "--benchmark-enable" or not disabled):
            raise native._pytest.IndependentUsageError(f"{option} requires pytest-benchmark")
    for option in _FIXTURE_VALUES:
        if config.getoption(option) is not None and not (disabled and option in _DISABLED_OPTIONS):
            raise native._pytest.IndependentUsageError(f"{option} requires pytest-benchmark")
    node = cast("Node", request.node)
    fixture = BenchmarkFixture(node.name)
    marker = node.get_closest_marker("benchmark")
    if marker is not None:
        fixture.group = cast("str | None", marker.kwargs.get("group"))
    return fixture


def pytest_collection_modifyitems(items: list[Item]) -> None:
    for item in items:
        marker = item.get_closest_marker("benchmark")
        if marker is not None and (marker.args or set(marker.kwargs) - {"group"}):
            raise native._pytest.IndependentUsageError(
                f"{item.nodeid}: benchmark marker options require pytest-benchmark"
            )


def pytest_runtest_setup(item: Item) -> None:
    if item.config.getoption("benchmark_only") and "benchmark" not in item.fixturenames:
        pytest.skip("Skipping non-benchmark (--benchmark-only active).")
