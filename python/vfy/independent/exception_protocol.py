"""Select the expression that names an AssertionError in failure output."""

from __future__ import annotations

from collections.abc import Callable


def _assertion_expression(
    error: AssertionError, limit_representation: Callable[[object], object]
) -> object:
    expression = getattr(error, "msg", None)
    if expression is not None:
        return expression
    try:
        # Stock's first limit runs inside its repr guard, so a failure becomes repr text.
        expression = limit_representation(repr(error))
    except BaseException as failure:
        if isinstance(failure, KeyboardInterrupt | SystemExit):
            raise
        # A failed repr cannot select the assertion prefix, so the exception keeps its full name.
        return None
    # Stock's second limit runs outside that guard, so its failure propagates.
    return limit_representation(expression)
