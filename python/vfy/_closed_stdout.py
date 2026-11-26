"""End a process quietly when the reader of its standard output has gone away.

A command such as ``vfy test | head -1`` closes the pipe while vfy is still
writing. Each process boundary catches the resulting ``BrokenPipeError`` once
and follows CPython's documented recipe: send further output to the null
device, so the interpreter's final flush cannot fail again, and exit.
"""

from __future__ import annotations

import os
import sys

# A test process exits with the shell's status for a SIGPIPE ending (128 + 13)
# so the controller can tell a closed pipe apart from a failed test run.
CLOSED_STDOUT_STATUS = 141


def discard_stdout() -> None:
    """Redirect standard output to the null device after its reader closed."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, sys.stdout.fileno())
    finally:
        os.close(devnull)
