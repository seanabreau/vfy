"""Controller for independent execution without coverage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from vfy.invocation_preparation import PreparedInvocation, finish, start
from vfy.output import OutputPolicy
from vfy.workers import xdist_requested

from .configuration import (
    PytestConfiguration,
    full_selection_message,
    resolve,
    unavailability_reason,
    validate,
    worker_arguments,
)

if TYPE_CHECKING:
    import argparse

    from vfy.config_types import Config


class _Controller:
    def __init__(self, workers: str | int | None) -> None:
        self.workers = workers

    def run(
        self,
        pytest_args: list[str],
        environment: dict[str, str],
        *,
        stdout: Literal["inherit", "stderr"] = "inherit",
    ) -> int:
        from vfy.runner import RunnerRequest  # noqa: PLC0415 - child launch only

        from .runner import IndependentRunner  # noqa: PLC0415 - child launch only

        return IndependentRunner().run(
            RunnerRequest(arguments=tuple(pytest_args), environment=environment, stdout=stdout)
        )


class _IndependentInvocation(NamedTuple):
    controller: _Controller
    configuration: PytestConfiguration
    history_path: str
    native_scheduling: str | None


def run_once(args: argparse.Namespace, config: Config) -> int:
    if os.name != "posix":
        from vfy.run.controller import _run_test_selection  # noqa: PLC0415 - platform route

        return _run_test_selection(args, config)
    owner = start(args)
    configuration = resolve(owner.pytest_args)
    plugins = configuration.plugins
    effective_args = configuration.effective_args
    plan = finish(args, owner, configuration, force_full=False, native_xdist_requested=False)
    if configuration.coverage_enabled or owner.location or args.mode not in {"safe", "full"}:
        from vfy.run.selected_run import (  # noqa: PLC0415 - selection route
            _run_test_selection_once,
        )

        return _run_test_selection_once(
            args, config, prepared=PreparedInvocation(owner, configuration, plan)
        )
    validate(args, plugins, xdist_requested(effective_args, plugins), unavailability_reason())
    args._test_engine = "native"
    args._watch_full_run = True
    output = OutputPolicy.from_args(args)
    if owner.pytest_args.count("--version") + owner.pytest_args.count("-V") != 1:
        output.diagnostic(full_selection_message())
    if getattr(args, "verbose", 0):
        output.notice("Backend: native (vfy owns selection and evidence)")
    history_path = str(Path(args.history).resolve())
    controller = _Controller(plan["controller_workers"])
    pytest_args = worker_arguments(list(owner.pytest_args), controller.workers)
    from vfy.native import NativeTestInvocation  # noqa: PLC0415 - shared invocation policy

    pytest_args = [
        *NativeTestInvocation.independent_plugin_prefix(plugins, pytest_args),
        *pytest_args,
    ]
    invocation = _IndependentInvocation(controller, configuration, history_path, plan["scheduling"])
    from vfy.run.execution import prepare_execution  # noqa: PLC0415 - child execution only

    with prepare_execution(args, invocation, pytest_args) as execution:
        completed = execution.run()
        execution.record(completed)
        return int(completed)
