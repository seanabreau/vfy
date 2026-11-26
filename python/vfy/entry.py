"""Dispatch command-line test runs without importing unrelated commands."""

from __future__ import annotations

import json
import os
import signal
import sys

from ._cli_arguments import escape_arguments
from ._closed_stdout import discard_stdout, flush_stdout_before_exit
from .native import is_test_cli


def main() -> int:
    try:
        with flush_stdout_before_exit():
            return _main()
    except BrokenPipeError:
        # pytest and Coverage.py both end with status 1 when stdout closes early.
        discard_stdout()
        return 1


def _main() -> int:
    os.environ["VFY_IN_PROCESS_PYTEST"] = "1"
    # pytest-xdist reports the user's command as workerinput["mainargv"].
    os.environ["VFY_NATIVE_MAINARGV"] = json.dumps(sys.argv)
    try:
        test_command = is_test_cli(sys.argv[1:])
    except UnicodeEncodeError:
        escaped, _ = escape_arguments(sys.argv[1:])
        test_command = is_test_cli(escaped)
    if not test_command:
        from .cli import main as cli_main  # noqa: PLC0415 - coverage and other commands

        return cli_main()
    from .run.entry import main as test_main  # noqa: PLC0415 - test command only

    return _relay_interrupt(test_main(sys.argv[1:]))


def _relay_interrupt(status: int) -> int:
    """End on SIGINT when the test process did, as ``python -m pytest`` does."""
    if status == -signal.SIGINT:
        # CPython re-raises an uncaught KeyboardInterrupt this way so callers see the signal.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)
    return status
