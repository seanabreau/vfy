"""A deliberately varied source file for differential source analysis."""

from typing import TYPE_CHECKING


def decorator(function):
    return function


@decorator
def classify(value: int) -> str:
    """Exercise branches, exclusions, and nested code objects."""
    try:
        if value > 0:
            return "positive"
    except ValueError:  # pragma: no cover
        return "invalid"

    for candidate in range(2):
        if candidate == value:
            break
    else:
        return "other"

    match value:
        case 0:
            return "zero"
        case _:
            return "matched"


if TYPE_CHECKING:
    from imaginary_package import Imaginary  # noqa: F401
