"""pytest's xunit-style setup and teardown functions as autouse fixtures.

pytest runs ``setup_module``/``teardown_module``, ``setup_function``/
``teardown_function``, ``setup_class``/``teardown_class``, and
``setup_method``/``teardown_method`` through generated autouse fixtures with
these names and scopes; the native collector registers the same declarations.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import TYPE_CHECKING, Protocol, cast

from .facade import fixture
from .fixtures import FixtureFunctionDefinition

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentRequest


class _XunitInstance(Protocol):
    """The xunit-style hooks of a test class instance, present only when declared."""

    def setup_method(self, *args: object) -> object: ...

    def teardown_method(self, *args: object) -> object: ...


def _first_function(holder: object, names: tuple[str, ...]) -> Callable[..., object] | None:
    for name in names:
        function = getattr(holder, name, None)
        if function is not None and not hasattr(function, "__vfy_fixture__"):
            return cast("Callable[..., object]", function)
    return None


def _call_with_optional_argument(function: Callable[..., object], argument: object) -> None:
    code = getattr(function, "__code__", None)
    count = 0 if code is None else code.co_argcount - int(inspect.ismethod(function))
    if count:
        function(argument)
    else:
        function()


def module_fixtures(module: ModuleType) -> list[FixtureFunctionDefinition]:
    """Return the module and function setup fixtures that ``module`` declares."""
    fixtures: list[FixtureFunctionDefinition] = []
    setup_module = _first_function(module, ("setUpModule", "setup_module"))
    teardown_module = _first_function(module, ("tearDownModule", "teardown_module"))
    if setup_module is not None or teardown_module is not None:

        @fixture(
            name=f"_xunit_setup_module_fixture_{module.__name__}", scope="module", autouse=True
        )
        def xunit_setup_module_fixture(request: NativeIndependentRequest) -> Iterator[None]:
            if setup_module is not None:
                _call_with_optional_argument(setup_module, request.module)
            yield
            if teardown_module is not None:
                _call_with_optional_argument(teardown_module, request.module)

        fixtures.append(xunit_setup_module_fixture)
    setup_function = _first_function(module, ("setup_function",))
    teardown_function = _first_function(module, ("teardown_function",))
    if setup_function is not None or teardown_function is not None:

        @fixture(name=f"_xunit_setup_function_fixture_{module.__name__}", autouse=True)
        def xunit_setup_function_fixture(request: NativeIndependentRequest) -> Iterator[None]:
            if request.instance is not None:
                # Methods use setup_method instead.
                yield
                return
            if setup_function is not None:
                _call_with_optional_argument(setup_function, request.function)
            yield
            if teardown_function is not None:
                _call_with_optional_argument(teardown_function, request.function)

        fixtures.append(xunit_setup_function_fixture)
    return fixtures


def class_fixtures(cls: type) -> list[FixtureFunctionDefinition]:
    """Return the class and method setup fixtures that a plain test class declares."""
    fixtures: list[FixtureFunctionDefinition] = []
    setup_class = _first_function(cls, ("setup_class",))
    teardown_class = _first_function(cls, ("teardown_class",))
    if setup_class is not None or teardown_class is not None:

        @fixture(name=f"_xunit_setup_class_fixture_{cls.__qualname__}", scope="class", autouse=True)
        def xunit_setup_class_fixture() -> Iterator[None]:
            if setup_class is not None:
                _call_with_optional_argument(setup_class, cls)
            yield
            if teardown_class is not None:
                _call_with_optional_argument(teardown_class, cls)

        fixtures.append(xunit_setup_class_fixture)
    has_setup = _first_function(cls, ("setup_method",)) is not None
    has_teardown = _first_function(cls, ("teardown_method",)) is not None
    if has_setup or has_teardown:

        @fixture(name=f"_xunit_setup_method_fixture_{cls.__qualname__}", autouse=True)
        def xunit_setup_method_fixture(request: NativeIndependentRequest) -> Iterator[None]:
            method = request.function
            if has_setup:
                _call_with_optional_argument(
                    cast("_XunitInstance", request.instance).setup_method, method
                )
            yield
            if has_teardown:
                _call_with_optional_argument(
                    cast("_XunitInstance", request.instance).teardown_method, method
                )

        fixtures.append(xunit_setup_method_fixture)
    return fixtures
