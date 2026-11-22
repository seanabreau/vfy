# ruff: noqa: ANN001, ANN201, B011, PT015
from __future__ import annotations

import pytest

partial_events: list[str] = []
imperative_xfail_events: list[str] = []


def test_pass():
    pass


def test_fail():
    assert False, "native failure"


@pytest.fixture
def broken_setup():
    raise RuntimeError("setup failure")


def test_setup_error(broken_setup):
    pass


@pytest.fixture
def partial_setup(request):
    # A registered finalizer must run even when setup never reaches a yield.
    request.addfinalizer(lambda: partial_events.append("finalized"))  # noqa: PT021
    raise RuntimeError("partial setup failure")


def test_partial_setup_error(partial_setup):
    pass


def test_partial_setup_finalized():
    assert partial_events == ["finalized"]


@pytest.mark.xfail(reason="non-strict native xpass")
def test_xpass():
    pass


@pytest.mark.xfail(reason="strict native xpass", strict=True)
def test_strict_xpass():
    pass


def test_imperative_xfail_call():
    pytest.xfail("imperative call xfail")


@pytest.fixture
def imperative_xfail_setup(request):
    # A yield fixture cannot reach teardown after pytest.xfail raises in setup.
    request.addfinalizer(lambda: imperative_xfail_events.append("setup finalized"))  # noqa: PT021
    pytest.xfail("imperative setup xfail")
    return 1


def test_imperative_xfail_setup(imperative_xfail_setup):
    assert imperative_xfail_setup == 1


def test_imperative_xfail_setup_finalized():
    assert imperative_xfail_events == ["setup finalized"]


@pytest.fixture
def imperative_xfail_teardown():
    yield
    pytest.xfail("imperative teardown xfail")


def test_imperative_xfail_teardown(imperative_xfail_teardown):
    assert True


@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown failure")


def test_teardown_error(broken_teardown):
    pass
