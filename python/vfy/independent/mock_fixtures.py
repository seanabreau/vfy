"""Fixture registration for the independent pytest-mock adapter."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Literal

import pytest

if TYPE_CHECKING:
    from .mock_support import MockerFixture
    from .protocol import Parser


def _mocker(config: object) -> Generator[MockerFixture]:
    from .mock_support import MockerFixture  # noqa: PLC0415 - fixture acquisition only

    result = MockerFixture(config)
    yield result
    result.stopall()


def _fixture(
    scope: Literal["function", "class", "module", "package", "session"], name: str
) -> Callable[..., Generator[MockerFixture]]:
    def fixture(pytestconfig: object) -> Generator[MockerFixture]:
        yield from _mocker(pytestconfig)

    fixture.__name__ = name
    fixture.__module__ = "vfy.independent.mock_support"
    fixture.__doc__ = f"Return a {scope}-scoped ``MockerFixture``."
    return pytest.fixture(scope=scope, name=name)(fixture)


mocker = _fixture("function", "mocker")
class_mocker = _fixture("class", "class_mocker")
module_mocker = _fixture("module", "module_mocker")
package_mocker = _fixture("package", "package_mocker")
session_mocker = _fixture("session", "session_mocker")


def pytest_addoption(parser: Parser) -> None:
    parser.addini(
        "mock_traceback_monkeypatch",
        "Monkeypatch the mock library to improve reporting of the assert_called_... methods",
        default=True,
    )
    parser.addini(
        "mock_use_standalone_module",
        'Use standalone "mock" (from PyPI) instead of builtin "unittest.mock" on Python 3',
        default=False,
    )
