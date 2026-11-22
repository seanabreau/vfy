# ruff: noqa: ANN001, ANN201, I001, PT021, T201
from __future__ import annotations

import pytest

from conftest import EVENTS, record


@pytest.fixture(scope="module")
def module_value(session_value):
    record("module setup")
    yield session_value + 1
    record("module teardown")


@pytest.fixture
def function_value(request, module_value):
    record("function setup")
    request.addfinalizer(lambda: record("function finalizer"))
    yield module_value + 1
    record("function teardown")


@pytest.fixture(params=[20, 30], ids=["twenty", "thirty"])
def fixture_parameter(request):
    return request.param


@pytest.fixture
def overridden(overridden):
    return f"local:{overridden}"


def test_00_fixture_chain(function_value):
    assert function_value == 12


def test_01_previous_teardown(function_value):
    assert EVENTS[-3:] == ["function teardown", "function finalizer", "function setup"]
    assert function_value == 12


@pytest.mark.parametrize("direct", [1, 2], ids=["one", "two"])
def test_direct_parameter(direct):
    assert direct in {1, 2}


def test_fixture_parameter(fixture_parameter):
    assert fixture_parameter in {20, 30}


def test_override(overridden):
    assert overridden == "local:base"


def test_dynamic_request(request):
    assert request.getfixturevalue("dynamic_value") == 7


def test_capture():
    print("native stdout")


@pytest.mark.skip(reason="native skip")
def test_skip():
    raise AssertionError("unreachable")


@pytest.mark.xfail(reason="native xfail")
def test_xfail():
    raise ValueError("expected")
