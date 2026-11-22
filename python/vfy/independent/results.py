"""Stage report paths and publish finalized independent results for the controller."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from vfy.native import stage_watch_outputs

from .facade import hookimpl
from .worker import result_status

if TYPE_CHECKING:
    from vfy._pytest_native import NativeRunResult

    from .protocol import Config


@hookimpl(tryfirst=True)
def pytest_configure(config: Config) -> None:
    """Stage resolved independent report paths before native writers acquire them."""
    stage = os.environ.get("VFY_WATCH_STAGE")
    if stage is None or "VFY_NATIVE_PARALLEL_DIR" in os.environ:
        return
    for option, kind in [("junitxml", "junit"), ("native_result_json", "native-result")]:
        destination = config.getoption(option, None)
        if destination:
            target = stage_watch_outputs(stage, [(kind, str(destination))])[0][1]
            setattr(config.option, option, target)


def publish_result(result: NativeRunResult) -> None:
    """Persist completed phases for the controller's Rust history recorder."""
    if destination := os.environ.get("VFY_TEST_RESULTS"):
        result.write_vfy_result(destination, result_status(result) not in {0, 1})
