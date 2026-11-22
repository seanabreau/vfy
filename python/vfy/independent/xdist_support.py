"""pytest-xdist's worker-facing fixtures for the independent runner without pytest-xdist."""

from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.fixture(scope="session")
def worker_id(request: pytest.FixtureRequest) -> str:
    """Return the worker's id (``gw0``, ``gw1``, ...) or ``master`` in a serial run."""
    workerinput = getattr(request.config, "workerinput", None)
    return "master" if workerinput is None else str(workerinput["workerid"])


@pytest.fixture(scope="session")
def testrun_uid(request: pytest.FixtureRequest) -> str:
    """Return the id that every worker of one run shares, or a fresh id in a serial run."""
    workerinput = getattr(request.config, "workerinput", None)
    return uuid4().hex if workerinput is None else str(workerinput["testrunuid"])
