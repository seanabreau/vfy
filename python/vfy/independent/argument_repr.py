"""Capture ordinary argument repr protocols for the native diagnostic formatter."""

from __future__ import annotations

from collections.abc import Callable
from typing import override

from .saferepr import SafeRepr

type Limit = Callable[[str], str]
type Dispatch = Callable[[SafeRepr, object], str]
type ExceptionExpression = Callable[[str, str], str]
type UnpresentableExpression = Callable[[str], str]
type FailureExpression = Callable[[str, str, int], str]


def _exception_expression(error: BaseException, expression: ExceptionExpression) -> str:
    try:
        return repr(error)
    # Mirrors pytest's saferepr guard: a failing repr becomes text; interpreter exits propagate.
    except BaseException as failure:
        if isinstance(failure, KeyboardInterrupt | SystemExit):
            raise
        return expression(format(type(error).__name__, ""), format(error, ""))


def _failure_expression(
    value: object,
    error: BaseException,
    expression: ExceptionExpression,
    unpresentable: UnpresentableExpression,
    failure: FailureExpression,
) -> str:
    try:
        rendered = _exception_expression(error, expression)
    # A failing exception formatter is rendered as unpresentable text, as pytest's saferepr does.
    except BaseException as nested:
        if isinstance(nested, KeyboardInterrupt | SystemExit):
            raise
        rendered = unpresentable(format(_exception_expression(nested, expression), ""))
    return failure(format(rendered, ""), format(type(value).__name__, ""), id(value))


def _instance_representation(
    value: object,
    limit: Limit,
    expression: ExceptionExpression,
    unpresentable: UnpresentableExpression,
    failure: FailureExpression,
) -> str:
    try:
        rendered = repr(value)
    # A failing user repr becomes the diagnostic text, as pytest's saferepr does.
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        rendered = _failure_expression(value, error, expression, unpresentable, failure)
    return limit(rendered)


class _ArgumentSafeRepr(SafeRepr):
    def __init__(
        self,
        maxsize: int | None,
        limit: Limit,
        expression: ExceptionExpression,
        unpresentable: UnpresentableExpression,
        failure: FailureExpression,
    ) -> None:
        super().__init__(maxsize)
        self._limit = limit
        self._expression = expression
        self._unpresentable = unpresentable
        self._failure = failure

    @override
    def repr_instance(self, x: object, level: int) -> str:
        return _instance_representation(
            x, self._limit, self._expression, self._unpresentable, self._failure
        )


def argument_representation(
    value: object,
    maxsize: int | None,
    dispatch: Dispatch,
    limit: Limit,
    expression: ExceptionExpression,
    unpresentable: UnpresentableExpression,
    failure: FailureExpression,
) -> str:
    adapter = _ArgumentSafeRepr(maxsize, limit, expression, unpresentable, failure)
    try:
        rendered = dispatch(adapter, value)
    # A failing reprlib dispatch becomes the diagnostic text, as pytest's saferepr does.
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt | SystemExit):
            raise
        rendered = _failure_expression(value, error, expression, unpresentable, failure)
    return limit(rendered)
