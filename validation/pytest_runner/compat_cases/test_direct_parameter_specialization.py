"""Direct parameters retain identity and bypass only pytest's direct fixture body."""

# Comparison output proves whether pytest captures it in setup or teardown.
# ruff: noqa: T201

from __future__ import annotations

import pytest

SENTINEL = object()
OTHER_SENTINEL = object()


class CountedParameter:
    def __eq__(self, other: object) -> bool:
        print("parameter cache compared", flush=True)
        return self is other

    def __hash__(self) -> int:
        return id(self)


COUNTED = CountedParameter()


@pytest.fixture(scope="module", params=[COUNTED])
def counted_parameter(request: pytest.FixtureRequest) -> object:
    return request.param


@pytest.fixture(scope="module")
def dependent_counted_parameter(counted_parameter: object) -> object:
    return counted_parameter


def test_counted_parameter_first(
    counted_parameter: object,
    dependent_counted_parameter: object,
    request: pytest.FixtureRequest,
) -> None:
    assert counted_parameter is dependent_counted_parameter is COUNTED
    assert request.getfixturevalue("counted_parameter") is COUNTED
    assert request.getfixturevalue("dependent_counted_parameter") is COUNTED


def test_counted_parameter_second(
    counted_parameter: object,
    dependent_counted_parameter: object,
    request: pytest.FixtureRequest,
) -> None:
    assert counted_parameter is dependent_counted_parameter is COUNTED
    assert request.getfixturevalue("counted_parameter") is COUNTED
    assert request.getfixturevalue("dependent_counted_parameter") is COUNTED


class EqualParameter:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, EqualParameter)

    def __hash__(self) -> int:
        return 0


EQUAL_FIRST = EqualParameter()
EQUAL_SECOND = EqualParameter()


@pytest.mark.parametrize("value", [SENTINEL], scope="session")
def test_session_direct_identity(value: object) -> None:
    assert value is SENTINEL


@pytest.mark.parametrize("shared_value", [SENTINEL], scope="module")
def test_first_module_direct_identity(shared_value: object) -> None:
    assert shared_value is SENTINEL


@pytest.mark.parametrize("shared_value", [OTHER_SENTINEL], scope="module")
def test_second_module_direct_identity(shared_value: object) -> None:
    assert shared_value is OTHER_SENTINEL


@pytest.mark.parametrize("equal_value", [EQUAL_FIRST], scope="module")
def test_first_equal_direct_identity(equal_value: object) -> None:
    assert equal_value is EQUAL_FIRST


@pytest.mark.parametrize("equal_value", [EQUAL_SECOND], scope="module")
def test_second_equal_direct_reuses_cached_value(equal_value: object) -> None:
    assert equal_value is EQUAL_FIRST


@pytest.mark.parametrize("value", ["first", "second"], scope="module")
def test_module_scoped_direct(value: str) -> None:
    assert value in {"first", "second"}


class TestClassScopedDirect:
    @pytest.mark.parametrize("value", [1, 2], scope="class")
    def test_value(self, value: int) -> None:
        assert value in {1, 2}


@pytest.fixture
def named_value(request: pytest.FixtureRequest) -> tuple[str, object]:
    return ("fixture", request.param)


@pytest.mark.parametrize("named_value", ["direct"])
def test_direct_parameter_shadows_same_name_fixture(named_value: object) -> None:
    assert named_value == "direct"


@pytest.mark.parametrize("named_value", ["indirect"], indirect=True)
def test_indirect_parameter_still_calls_fixture(named_value: object) -> None:
    assert named_value == ("fixture", "indirect")


@pytest.fixture
def read_direct_during_finalization(request: pytest.FixtureRequest) -> object:
    value = request.getfixturevalue("function_value")

    def finish() -> None:
        assert request.getfixturevalue("function_value") is value
        print("direct value retained through dependent finalizer", flush=True)

    request.addfinalizer(finish)  # noqa: PT021 - exercise explicit finalizer requests
    return value


@pytest.mark.parametrize("function_value", [SENTINEL, OTHER_SENTINEL])
def test_function_direct_dynamic_identity(
    function_value: object,
    read_direct_during_finalization: object,
    request: pytest.FixtureRequest,
) -> None:
    assert function_value is read_direct_during_finalization
    assert request.getfixturevalue("function_value") is function_value


class ReportingParameter:
    def __eq__(self, other: object) -> bool:
        if self is not other:
            print("scoped parameter compared during setup", flush=True)
        return self is other

    def __hash__(self) -> int:
        return id(self)


REPORTING_FIRST = ReportingParameter()
REPORTING_SECOND = ReportingParameter()


@pytest.mark.parametrize("reporting_value", [REPORTING_FIRST], scope="module")
def test_first_reporting_parameter(reporting_value: object) -> None:
    assert reporting_value is REPORTING_FIRST


@pytest.mark.parametrize("reporting_value", [REPORTING_SECOND], scope="module")
def test_second_reporting_parameter(reporting_value: object) -> None:
    assert reporting_value is REPORTING_SECOND


class DirectionParameter:
    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        if self is not other and isinstance(other, DirectionParameter):
            print(f"compare {self.name} to {other.name}", flush=True)
        return self is other

    def __hash__(self) -> int:
        return id(self)


DIRECTION_FIRST = DirectionParameter("new")
DIRECTION_SECOND = DirectionParameter("old")


@pytest.mark.parametrize("direction_value", [DIRECTION_FIRST], scope="module")
def test_first_direction_parameter(direction_value: object) -> None:
    assert direction_value is DIRECTION_FIRST


@pytest.mark.parametrize("direction_value", [DIRECTION_SECOND], scope="module")
def test_second_direction_parameter(direction_value: object) -> None:
    assert direction_value is DIRECTION_SECOND


class RejectingParameter:
    def __eq__(self, other: object) -> bool:
        if self is not other:
            raise TypeError("controlled parameter comparison error")
        return True

    def __hash__(self) -> int:
        return id(self)


REJECTING_FIRST = RejectingParameter()
REJECTING_SECOND = RejectingParameter()


@pytest.mark.parametrize("rejecting_value", [REJECTING_FIRST], scope="module")
def test_first_rejecting_parameter(rejecting_value: object) -> None:
    assert rejecting_value is REJECTING_FIRST


@pytest.mark.parametrize("rejecting_value", [REJECTING_SECOND], scope="module")
def test_second_rejecting_parameter(rejecting_value: object) -> None:
    raise AssertionError("the body must not execute after fixture comparison error")
