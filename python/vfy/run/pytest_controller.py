"""Prepare and execute pytest invocations for a selected engine."""

from __future__ import annotations

from importlib.util import find_spec
from typing import Literal

from vfy.errors import VfyError
from vfy.independent.configuration import (
    PytestConfiguration as _PytestConfiguration,
    worker_arguments,
)
from vfy.runner import PytestRunner, RunnerBackend, RunnerRequest
from vfy.workers import worker_count

from .engine import TestEngine, _explicit_numprocesses, _resolve_pytest_configuration
from .reports import _pytest_option


class TestController:
    """Prepare and execute pytest invocations."""

    def __init__(self, workers: str | int | None, backend: RunnerBackend | None = None) -> None:
        self.workers = workers
        self.backend = backend or PytestRunner()

    def resolve_pytest_args(
        self,
        pytest_args: list[str],
        *,
        selected_count: int | None = None,
        route: TestEngine = TestEngine.PYTEST,
        configuration: _PytestConfiguration | None = None,
    ) -> list[str]:
        if route is TestEngine.INDEPENDENT:
            return worker_arguments(pytest_args, self.workers, selected_count)
        if self.workers is None or route is TestEngine.NATIVE:
            return pytest_args
        if configuration is None:
            configuration = _resolve_pytest_configuration(pytest_args)
        effective_args = configuration.effective_args
        explicit = _explicit_numprocesses(configuration.projection["cli_args"])
        configured_explicit = _explicit_numprocesses(effective_args)
        if explicit and self.workers != "auto":
            raise VfyError("--workers cannot be combined with pytest -n/--numprocesses")
        if self.workers == "auto" and configured_explicit:
            return pytest_args
        workers = self.workers
        distribution = _pytest_option(effective_args, "--dist")
        if workers == "auto" and distribution is None and find_spec("xdist") is None:
            return pytest_args
        if workers == "auto" and selected_count is not None and distribution is None:
            workers = worker_count(workers, selected_count)
        if workers == 1 and distribution is None and not configured_explicit:
            return pytest_args
        value = "0" if workers == 1 else str(workers)
        return [*pytest_args, "-n", value]

    def run(
        self,
        pytest_args: list[str],
        environment: dict[str, str],
        *,
        stdout: Literal["inherit", "stderr"] = "inherit",
    ) -> int:
        return self.backend.run(
            RunnerRequest(arguments=tuple(pytest_args), environment=environment, stdout=stdout)
        )


def _controller_for_engine(workers: str | int | None, engine: TestEngine) -> TestController:
    if engine is TestEngine.INDEPENDENT:
        from vfy.independent.runner import (  # noqa: PLC0415 - selected child frontend
            IndependentRunner,
        )

        return TestController(workers, IndependentRunner())
    return TestController(workers)
