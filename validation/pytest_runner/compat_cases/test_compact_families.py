"""Compact cases keep independent instances, request views and parameter marks."""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

seen_instances: list[object] = []
seen_class_values: list[object] = []
active_overridden_fixtures: set[int] = set()


@pytest.fixture
def case_identity(request: pytest.FixtureRequest) -> Iterator[pytest.FixtureRequest]:
    assert request.node.name.endswith(f"[{request.node.callspec.id}]")
    assert request.node.nodeid.endswith(request.node.name)
    item = request.node
    yield request
    assert request.node is item


class TestFreshInstances:
    def setup_method(self) -> None:
        assert not hasattr(self, "ran")

    @pytest.mark.parametrize("value", [1, 2, 3], ids=["one", "two::value", "three"])
    def test_case(self, value: int, case_identity: pytest.FixtureRequest) -> None:
        assert all(self is not previous for previous in seen_instances)
        seen_instances.append(self)
        assert case_identity.instance is self
        assert case_identity.function.__self__ is self
        assert case_identity.node.callspec.params["value"] == value
        self.ran = True


class TestMethodKinds:
    @staticmethod
    @pytest.mark.parametrize("value", [1, 2])
    def test_static(value: int, request: pytest.FixtureRequest) -> None:
        assert value in (1, 2)
        assert type(request.instance) is TestMethodKinds
        assert request.function is TestMethodKinds.test_static

    @classmethod
    @pytest.mark.parametrize("value", [1, 2])
    def test_class(cls, value: int, request: pytest.FixtureRequest) -> None:
        assert value in (1, 2)
        assert type(request.instance) is cls
        assert request.function.__self__ is cls


@pytest.fixture(scope="class")
def standalone_class_value() -> object:
    return object()


@pytest.mark.parametrize("value", [1, 2])
def test_class_scope_without_class(value: int, standalone_class_value: object) -> None:
    assert value in (1, 2)
    assert all(standalone_class_value is not previous for previous in seen_class_values)
    seen_class_values.append(standalone_class_value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, id="pass"),
        pytest.param(2, id="skip", marks=pytest.mark.skip(reason="case skip")),
        pytest.param(3, id="xfail", marks=pytest.mark.xfail(reason="case xfail")),
    ],
)
def test_case_marks(value: int) -> None:
    assert value == 1


@pytest.mark.filterwarnings("error:family warning:UserWarning")
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(
            1,
            marks=pytest.mark.filterwarnings("ignore:family warning:UserWarning"),
        ),
        pytest.param(2, marks=pytest.mark.xfail(raises=UserWarning)),
    ],
)
def test_case_warning_precedence(value: int) -> None:
    assert value in (1, 2)
    warnings.warn("family warning", UserWarning, stacklevel=1)


@pytest.fixture(scope="class")
def class_fixture_at_function_scope(request: pytest.FixtureRequest) -> Iterator[int]:
    assert request.scope == "function"
    assert not active_overridden_fixtures
    value = request.param
    active_overridden_fixtures.add(value)
    yield value
    active_overridden_fixtures.remove(value)


@pytest.mark.parametrize(
    "class_fixture_at_function_scope",
    [1, 2, 3],
    indirect=True,
    scope="function",
)
class TestFixtureScopeOverride:
    def test_each_case_tears_down_before_next(self, class_fixture_at_function_scope: int) -> None:
        assert active_overridden_fixtures == {class_fixture_at_function_scope}
