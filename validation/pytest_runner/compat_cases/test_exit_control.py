# ruff: noqa: ANN001, ANN201
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def session_resource():
    yield
    Path(os.environ["PYTEST_CONTROL_TEARDOWN"]).write_text("torn down\n", encoding="utf-8")


def test_exit_runs_teardown(session_resource):
    pytest.exit("controlled exit", returncode=7)
