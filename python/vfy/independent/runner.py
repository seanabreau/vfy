"""Launch independent children through the controller's process runner."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from vfy.runner import PytestRunner

from .bootstrap import prepare_bootstrap

if TYPE_CHECKING:
    from vfy.runner import RunnerRequest


class IndependentRunner:
    """Use the owned independent frontend for one controller request."""

    def run(self, request: RunnerRequest) -> int:
        arguments, environment = prepare_bootstrap(
            ["-m", "vfy.independent.entry", *request.arguments],
            request.environment,
            controller_parent=os.getpid(),
        )
        return PytestRunner((sys.executable, *arguments)).run(
            request._replace(arguments=(), environment=environment)
        )
