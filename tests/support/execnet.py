"""Process isolation for tests that change execnet's live transport globals."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import cast

import pytest

from .vfy_project import VfyProject


def isolated_execnet[**P](test: Callable[P, None]) -> Callable[P, None]:
    """Run codec mutations outside an enclosing xdist worker's live channel."""

    @wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        request = cast(pytest.FixtureRequest, kwargs["request"])
        if not hasattr(request.config, "workerinput"):
            test(*args, **kwargs)
            return
        # The outer xdist receiver can use execnet helpers during the test body.
        project = VfyProject(cast(Path, request.getfixturevalue("tmp_path")))
        relative_path, separator, selection = request.node.nodeid.partition("::")
        absolute_nodeid = str(request.config.rootpath / relative_path) + separator + selection
        result = project.run_python(
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:xdist",
            absolute_nodeid,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    return run
