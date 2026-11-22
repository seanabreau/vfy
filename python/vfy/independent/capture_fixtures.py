"""Declare capture providers on the invocation's existing capture plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .fixtures import fixture

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest


def _capture(
    request: NativeIndependentRequest,
    name: Literal["capsys", "capfd", "capsysbinary", "capfdbinary", "capteesys"],
) -> object:
    from vfy.native import _pytest  # noqa: PLC0415 - defer the facade/bootstrap import cycle

    return _pytest.independent_capture_fixture(request, name)


@fixture
def capsys(request: NativeIndependentRequest) -> object:
    return _capture(request, "capsys")


@fixture
def capfd(request: NativeIndependentRequest) -> object:
    return _capture(request, "capfd")


@fixture
def capsysbinary(request: NativeIndependentRequest) -> object:
    return _capture(request, "capsysbinary")


@fixture
def capfdbinary(request: NativeIndependentRequest) -> object:
    return _capture(request, "capfdbinary")


@fixture
def capteesys(request: NativeIndependentRequest) -> object:
    return _capture(request, "capteesys")
