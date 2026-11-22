# ruff: noqa: ANN201, I001
from __future__ import annotations

import os
from pathlib import Path

import pytest


EVENTS: list[str] = []


def record(event: str) -> None:
    EVENTS.append(event)
    destination = os.environ.get("PYTEST_NATIVE_EVENT_LOG")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as stream:
            stream.write(f"{event}\n")


@pytest.fixture(scope="session")
def session_value():
    record("session setup")
    yield 10
    record("session teardown")


@pytest.fixture
def overridden():
    record("base setup")
    yield "base"
    record("base teardown")


@pytest.fixture
def dynamic_value():
    record("dynamic setup")
    yield 7
    record("dynamic teardown")
