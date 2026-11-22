# ruff: noqa: ANN001, ANN201, I001, RUF012
from __future__ import annotations

import pytest


events: list[str] = []


@pytest.fixture(scope="module", params=[1, 2], ids=["first", "second"])
def module_parameter(request):
    value = request.param
    events.append(f"parameter {value} setup")
    yield value
    events.append(f"parameter {value} teardown")


@pytest.fixture(scope="module")
def derived_module_value(module_parameter):
    value = module_parameter * 10
    events.append(f"derived {value} setup")
    yield value
    events.append(f"derived {value} teardown")


def test_parameter_invalidates_dependent_fixture(module_parameter, derived_module_value):
    assert derived_module_value == module_parameter * 10
    if derived_module_value == 20:
        assert events == [
            "parameter 1 setup",
            "derived 10 setup",
            "derived 10 teardown",
            "parameter 1 teardown",
            "parameter 2 setup",
            "derived 20 setup",
        ]


@pytest.fixture(scope="class")
def class_resource(request):
    request.cls.seen.append("setup")
    yield
    request.cls.seen.append("teardown")


@pytest.mark.usefixtures("class_resource")
class TestClassScope:
    seen: list[str] = []

    def setup_method(self):
        self.seen.append("method setup")

    def teardown_method(self):
        self.seen.append("method teardown")

    def test_first(self):
        assert self.seen == ["setup", "method setup"]

    def test_second(self):
        assert self.seen == [
            "setup",
            "method setup",
            "method teardown",
            "method setup",
        ]
