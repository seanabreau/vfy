"""Inactive pytest-codspeed 5.0.3 and 5.0.4 fixture and pytest hooks."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from collections.abc import Callable
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast, override

import pytest

from vfy import native

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest

    from .protocol import Config, Parser


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("CodSpeed benchmarking")
    group.addoption("--codspeed", action="store_true", default=False)
    group.addoption("--codspeed-mode", action="store", choices=["simulation", "memory", "walltime"])
    group.addoption("--codspeed-warmup-time", action="store", type=float)
    group.addoption("--codspeed-max-time", action="store", type=float)
    group.addoption("--codspeed-max-rounds", action="store", type=int)


def pytest_configure(config: Config) -> None:
    if config.getoption("--codspeed") or "CODSPEED_ENV" in os.environ:
        raise native._pytest.IndependentUsageError(
            "CodSpeed measurement is not supported by the independent engine; use --engine=pytest"
        )
    # Preserve the installed plugin's configuration conversion even when
    # measurement is disabled; the options remain observable through pytest.
    codspeed_config = importlib.import_module("pytest_codspeed.config")
    codspeed_config.CodSpeedConfig.from_pytest_config(config)
    config.addinivalue_line(
        "markers", "codspeed_benchmark: mark an entire test for codspeed benchmarking"
    )
    config.addinivalue_line("markers", "benchmark: mark an entire test for codspeed benchmarking")


class _PedanticOptions(Protocol):
    teardown: Callable[..., object] | None

    def setup_and_get_args_kwargs(self) -> tuple[tuple[object, ...], dict[str, object]]: ...


class BenchmarkFixture:
    """Run the installed CodSpeed fixture's unmeasured callable contract."""

    @property
    @override
    def __class__(self) -> type:
        if importlib.util.find_spec("pytest_benchmark") is not None:
            # This optional public fixture class is available only when installed.
            from pytest_benchmark.fixture import (  # noqa: PLC0415 - optional distribution
                BenchmarkFixture as PytestBenchmarkFixture,
            )

            return PytestBenchmarkFixture
        return BenchmarkFixture

    def __init__(self, request: NativeIndependentRequest) -> None:
        self.extra_info: dict[str, object] = {}
        self._called = False

    def __call__(self, target: Callable[..., object], *args: object, **kwargs: object) -> object:
        if self._called:
            raise RuntimeError("The benchmark fixture can only be used once per test")
        self._called = True
        return target(*args, **kwargs)

    def pedantic(
        self,
        target: Callable[..., object],
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] = {},  # noqa: B006 - preserve the upstream fixture signature
        setup: Callable[..., object] | None = None,
        teardown: Callable[..., object] | None = None,
        rounds: int = 1,
        warmup_rounds: int = 0,
        iterations: int = 1,
    ) -> object:
        if self._called:
            raise RuntimeError("The benchmark fixture can only be used once per test")
        self._called = True
        # The admitted package's public options object owns the upstream
        # validation and setup protocol. The scoped plugin alias lets the real
        # package initialize without importing pytest's private FixtureManager.
        codspeed_config = importlib.import_module("pytest_codspeed.config")
        constructor = cast("Callable[..., _PedanticOptions]", codspeed_config.PedanticOptions)
        options = constructor(
            target=target,
            args=args,
            kwargs=kwargs,
            setup=setup,
            teardown=teardown,
            rounds=rounds,
            warmup_rounds=warmup_rounds,
            iterations=iterations,
        )
        args, kwargs = options.setup_and_get_args_kwargs()
        result = target(*args, **kwargs)
        if options.teardown is not None:
            options.teardown(*args, **kwargs)
        return result


def install_import_alias(config: Config) -> None:
    """Let the real package export the inactive fixture during this session."""
    name = "pytest_codspeed.plugin"
    if name in sys.modules or "pytest_codspeed" in sys.modules:
        raise native._pytest.IndependentUsageError(
            "pytest_codspeed is already imported; use --engine=pytest"
        )
    plugin = ModuleType(name, "vfy's inactive pytest-codspeed fixture adapter")
    plugin.__package__ = "pytest_codspeed"
    plugin.__spec__ = ModuleSpec(name, loader=None)
    vars(plugin)["BenchmarkFixture"] = BenchmarkFixture
    sys.modules[name] = plugin
    package: ModuleType | None = None
    try:
        package = importlib.import_module("pytest_codspeed")
        # Cached submodules do not automatically attach to a reimported parent.
        vars(package).setdefault("plugin", plugin)
        config_module = importlib.import_module("pytest_codspeed.config")
        vars(package).setdefault("config", config_module)
    except BaseException:
        if sys.modules.get(name) is plugin:
            del sys.modules[name]
        if (
            package is not None
            and sys.modules.get("pytest_codspeed") is package
            and getattr(package, "BenchmarkFixture", None) is BenchmarkFixture
        ):
            del sys.modules["pytest_codspeed"]
        raise

    def close() -> None:
        if sys.modules.get(name) is plugin:
            del sys.modules[name]
        if (
            sys.modules.get("pytest_codspeed") is package
            and getattr(package, "BenchmarkFixture", None) is BenchmarkFixture
        ):
            del sys.modules["pytest_codspeed"]

    config.add_cleanup(close)


@pytest.fixture
def codspeed_benchmark(request: NativeIndependentRequest) -> BenchmarkFixture:
    return BenchmarkFixture(request)


if importlib.util.find_spec("pytest_benchmark") is None:

    @pytest.fixture
    def benchmark(codspeed_benchmark: BenchmarkFixture) -> BenchmarkFixture:
        return codspeed_benchmark
