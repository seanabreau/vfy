"""``pytest.approx`` with pytest 9.1.1's types, tolerances, and comparison explanations."""

from __future__ import annotations

import builtins
import itertools
import math
import pprint
import sys
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence, Sized
from datetime import datetime, timedelta
from decimal import Decimal
from numbers import Complex
from typing import Any, ClassVar, cast, override


def _compare_approx(
    message_data: Sequence[tuple[str, str, str]],
    number_of_elements: int,
    different_ids: Sequence[object],
    max_abs_diff: float,
    max_rel_diff: float,
) -> list[str]:
    rows = [("Index", "Obtained", "Expected"), *message_data]
    widths = [max(len(row[column]) for row in rows) for column in range(3)]
    return [
        f"comparison failed. Mismatched elements: {len(different_ids)} / {number_of_elements}:",
        f"Max absolute difference: {max_abs_diff}",
        f"Max relative difference: {max_rel_diff}",
        *(
            f"{index:<{widths[0]}} | {obtained:<{widths[1]}} | {expected:<{widths[2]}}"
            for index, obtained, expected in rows
        ),
    ]


class ApproxBase:
    """Provide shared utilities for making approximate comparisons."""

    __array_ufunc__ = None
    __array_priority__ = 100

    def __init__(
        self,
        expected: Any,  # noqa: ANN401 - any supported expectation
        rel: Any = None,  # noqa: ANN401 - float, Decimal, or timedelta
        abs: Any = None,  # noqa: A002, ANN401 - pytest public keyword
        nan_ok: bool = False,
    ) -> None:
        __tracebackhide__ = True
        self.expected = expected
        self.abs = abs
        self.rel = rel
        self.nan_ok = nan_ok
        self._check_type()

    @override
    def __repr__(self) -> str:
        raise NotImplementedError

    def _repr_compare(self, other_side: Any) -> list[str]:  # noqa: ANN401 - any compared value
        return ["comparison failed", f"Obtained: {other_side}", f"Expected: {self}"]

    @override
    def __eq__(self, actual: object) -> bool:
        return all(a == self._approx_scalar(x) for a, x in self._yield_comparisons(actual))

    def __bool__(self) -> bool:
        __tracebackhide__ = True
        raise AssertionError(
            "approx() is not supported in a boolean context.\n"
            "Did you mean: `assert a == approx(b)`?"
        )

    __hash__: ClassVar[None] = None

    @override
    def __ne__(self, actual: object) -> bool:
        return not actual == self

    def _approx_scalar(self, x: Any) -> ApproxBase:  # noqa: ANN401 - any expected element
        if isinstance(x, Decimal):
            return ApproxDecimal(x, rel=self.rel, abs=self.abs, nan_ok=self.nan_ok)
        if isinstance(x, datetime | timedelta):
            return ApproxTimedelta(x, rel=self.rel, abs=self.abs, nan_ok=self.nan_ok)
        return ApproxScalar(x, rel=self.rel, abs=self.abs, nan_ok=self.nan_ok)

    def _yield_comparisons(self, actual: Any) -> Iterable[tuple[Any, Any]]:  # noqa: ANN401 - pytest compares arbitrary objects
        raise NotImplementedError

    def _check_type(self) -> None:
        pass


def _recursive_sequence_map(f: Any, x: Any) -> Any:  # noqa: ANN401 - mirrors nested input
    if isinstance(x, list | tuple):
        return type(x)(_recursive_sequence_map(f, xi) for xi in x)
    if _is_sequence_like(x):
        return [_recursive_sequence_map(f, xi) for xi in x]
    return f(x)


class ApproxNumpy(ApproxBase):  # noqa: PLW1641 - unhashable, as in pytest
    """Perform approximate comparisons where the expected value is a numpy array."""

    @override
    def __repr__(self) -> str:
        values = _recursive_sequence_map(self._approx_scalar, self.expected.tolist())
        return f"approx({values!r})"

    @override
    def _repr_compare(self, other_side: Any) -> list[str]:
        def value_at(nested: Any, index: tuple[Any, ...]) -> Any:  # noqa: ANN401 - nested values are arbitrary
            for i in index:
                nested = nested[i]
            return nested

        shape = self.expected.shape
        approx_side = _recursive_sequence_map(self._approx_scalar, self.expected.tolist())
        other_array = _as_numpy_array(other_side)
        assert other_array is not None
        if shape != other_array.shape:
            return [
                "Impossible to compare arrays with different shapes.",
                f"Shapes: {shape} and {other_array.shape}",
            ]
        max_abs, max_rel = -math.inf, -math.inf
        different = []
        for index in itertools.product(*(range(i) for i in shape)):
            approx_value = value_at(approx_side, index)
            other_value = value_at(other_array, index)
            if approx_value != other_value:
                abs_diff = abs(approx_value.expected - other_value)
                max_abs = max(max_abs, abs_diff)
                if other_value == 0.0:
                    max_rel = math.inf
                else:
                    max_rel = max(max_rel, abs_diff / abs(other_value))
                different.append(index)
        rows = [
            (str(index), str(value_at(other_array, index)), str(value_at(approx_side, index)))
            for index in different
        ]
        return _compare_approx(rows, self.expected.size, different, max_abs, max_rel)

    @override
    def __eq__(self, actual: object) -> bool:
        np: Any = sys.modules["numpy"]
        if not np.isscalar(actual):
            try:
                actual = np.asarray(actual)
            except Exception as error:
                raise TypeError(f"cannot compare '{actual}' to numpy.ndarray") from error
        if not np.isscalar(actual) and cast("Any", actual).shape != self.expected.shape:
            return False
        return super().__eq__(actual)

    @override
    def _yield_comparisons(self, actual: Any) -> Iterator[tuple[Any, Any]]:
        np: Any = sys.modules["numpy"]
        for i in np.ndindex(self.expected.shape):
            value = actual if np.isscalar(actual) else actual[i].item()
            yield value, self.expected[i].item()


class ApproxMapping(ApproxBase):  # noqa: PLW1641 - unhashable, as in pytest
    """Perform approximate comparisons where the expected value is a mapping of numbers."""

    @override
    def __repr__(self) -> str:
        return f"approx({ {k: self._approx_scalar(v) for k, v in self.expected.items()}!r})"

    @override
    def _repr_compare(self, other_side: Any) -> list[str]:
        if len(self.expected) != len(other_side):
            return [
                "Impossible to compare mappings with different sizes.",
                f"Lengths: {len(self.expected)} and {len(other_side)}",
            ]
        if self.expected.keys() != other_side.keys():
            return [
                "comparison failed.",
                f"Mappings has different keys: expected {self.expected.keys()} "
                f"but got {other_side.keys()}",
            ]
        approx_side = {k: self._approx_scalar(v) for k, v in self.expected.items()}
        max_abs, max_rel = -math.inf, -math.inf
        different = []
        for key, approx_value in approx_side.items():
            other_value = other_side[key]
            if approx_value != other_value:
                if approx_value.expected is not None and other_value is not None:
                    try:
                        max_abs = max(max_abs, abs(approx_value.expected - other_value))
                        if approx_value.expected == 0.0:
                            max_rel = math.inf
                        else:
                            max_rel = max(
                                max_rel,
                                abs((approx_value.expected - other_value) / approx_value.expected),
                            )
                    except ZeroDivisionError:
                        pass
                different.append(key)
        rows = [(str(key), str(other_side[key]), str(approx_side[key])) for key in different]
        return _compare_approx(rows, len(approx_side), different, max_abs, max_rel)

    @override
    def __eq__(self, actual: object) -> bool:
        try:
            if set(cast("Mapping[object, object]", actual).keys()) != set(self.expected.keys()):
                return False
        except AttributeError:
            return False
        return super().__eq__(actual)

    @override
    def _yield_comparisons(self, actual: Any) -> Iterator[tuple[Any, Any]]:
        for key in self.expected:
            yield actual[key], self.expected[key]

    @override
    def _check_type(self) -> None:
        __tracebackhide__ = True
        for key, value in self.expected.items():
            if isinstance(value, type(self.expected)):
                raise TypeError(
                    f"pytest.approx() does not support nested dictionaries: key={key!r} "
                    f"value={value!r}\n  full mapping={pprint.pformat(self.expected)}"
                )


class ApproxSequenceLike(ApproxBase):  # noqa: PLW1641 - unhashable, as in pytest
    """Perform approximate comparisons where the expected value is a sequence of numbers."""

    @override
    def __repr__(self) -> str:
        sequence_type = type(self.expected)
        if sequence_type not in (tuple, list):
            sequence_type = list
        return f"approx({sequence_type(self._approx_scalar(x) for x in self.expected)!r})"

    @override
    def _repr_compare(self, other_side: Any) -> list[str]:
        if len(self.expected) != len(other_side):
            return [
                "Impossible to compare lists with different sizes.",
                f"Lengths: {len(self.expected)} and {len(other_side)}",
            ]
        approx_side = _recursive_sequence_map(self._approx_scalar, self.expected)
        max_abs, max_rel = -math.inf, -math.inf
        different = []
        for i, (approx_value, other_value) in enumerate(zip(approx_side, other_side, strict=True)):
            if approx_value != other_value:
                try:
                    abs_diff = abs(approx_value.expected - other_value)
                    max_abs = max(max_abs, abs_diff)
                except TypeError:
                    pass
                else:
                    if other_value == 0.0:
                        max_rel = math.inf
                    else:
                        max_rel = max(max_rel, abs_diff / abs(other_value))
                different.append(i)
        rows = [(str(i), str(other_side[i]), str(approx_side[i])) for i in different]
        return _compare_approx(rows, len(approx_side), different, max_abs, max_rel)

    @override
    def __eq__(self, actual: object) -> bool:
        try:
            if len(cast("Sized", actual)) != len(self.expected):
                return False
        except TypeError:
            return False
        return super().__eq__(actual)

    @override
    def _yield_comparisons(self, actual: Any) -> Iterable[tuple[Any, Any]]:
        return zip(actual, self.expected, strict=True)

    @override
    def _check_type(self) -> None:
        __tracebackhide__ = True
        for index, x in enumerate(self.expected):
            if isinstance(x, type(self.expected)):
                raise TypeError(
                    f"pytest.approx() does not support nested data structures: {x!r} at index "
                    f"{index}\n  full sequence: {pprint.pformat(self.expected)}"
                )


def _is_bool(value: object) -> bool:
    if isinstance(value, bool):
        return True
    numpy = sys.modules.get("numpy")
    return numpy is not None and isinstance(value, numpy.bool_)


class ApproxScalar(ApproxBase):
    """Perform approximate comparisons where the expected value is a single number."""

    DEFAULT_ABSOLUTE_TOLERANCE: ClassVar[float | Decimal] = 1e-12
    DEFAULT_RELATIVE_TOLERANCE: ClassVar[float | Decimal] = 1e-6

    @override
    def __repr__(self) -> str:
        if (
            isinstance(self.expected, bool)
            or not isinstance(self.expected, Complex | Decimal)
            or math.isinf(abs(self.expected))
        ):
            return str(self.expected)
        try:
            tolerance = self.tolerance
            vetted = f"{tolerance:n}" if 1e-3 <= tolerance < 1e3 else f"{tolerance:.1e}"
            if (
                isinstance(self.expected, Complex)
                and self.expected.imag
                and not math.isinf(tolerance)
            ):
                vetted += " ∠ ±180°"
        except ValueError:
            vetted = "???"
        return f"{self.expected} ± {vetted}"

    @override
    def __eq__(self, actual: object) -> bool:
        array = _as_numpy_array(actual)
        if array is not None:
            return all(self.__eq__(a) for a in array.flat)
        if _is_bool(self.expected) and not _is_bool(actual):
            return False
        if actual == self.expected:
            return True
        if _is_bool(self.expected) or not (
            isinstance(self.expected, Complex | Decimal) and isinstance(actual, Complex | Decimal)
        ):
            return False
        if math.isnan(abs(self.expected)):
            return self.nan_ok and math.isnan(abs(cast("complex", actual)))
        if math.isinf(abs(self.expected)):
            return False
        return bool(abs(self.expected - cast("Any", actual)) <= self.tolerance)

    __hash__: ClassVar[None] = None

    @property
    def tolerance(self) -> Any:  # noqa: ANN401 - float or Decimal, as the expectation
        absolute = self.DEFAULT_ABSOLUTE_TOLERANCE if self.abs is None else self.abs
        if absolute < 0:
            raise ValueError(f"absolute tolerance can't be negative: {absolute}")
        if math.isnan(absolute):
            raise ValueError("absolute tolerance can't be NaN.")
        if self.rel is None and self.abs is not None:
            return absolute
        relative = (self.DEFAULT_RELATIVE_TOLERANCE if self.rel is None else self.rel) * abs(
            self.expected
        )
        if relative < 0:
            raise ValueError(f"relative tolerance can't be negative: {relative}")
        if math.isnan(relative):
            raise ValueError("relative tolerance can't be NaN.")
        return max(relative, absolute)


class ApproxDecimal(ApproxScalar):
    """Perform approximate comparisons where the expected value is a Decimal."""

    DEFAULT_ABSOLUTE_TOLERANCE = Decimal("1e-12")
    DEFAULT_RELATIVE_TOLERANCE = Decimal("1e-6")

    @override
    def __repr__(self) -> str:
        rel = Decimal(self.rel) if isinstance(self.rel, float) else self.rel
        absolute = Decimal(self.abs) if isinstance(self.abs, float) else self.abs
        tolerance = "???"
        if rel is not None and Decimal("1e-3") <= rel <= Decimal("1e3"):
            tolerance = f"{rel:.1e}"
        elif absolute is not None:
            tolerance = f"{absolute:.1e}"
        return f"{self.expected} ± {tolerance}"


class ApproxTimedelta(ApproxBase):  # noqa: PLW1641 - unhashable, as in pytest
    """Perform approximate comparisons of datetimes and timedeltas."""

    def __init__(
        self,
        expected: Any,  # noqa: ANN401 - any supported expectation
        rel: Any = None,  # noqa: ANN401 - float, Decimal, or timedelta
        abs: Any = None,  # noqa: A002, ANN401 - pytest public keyword
        nan_ok: bool = False,
    ) -> None:
        __tracebackhide__ = True
        if isinstance(expected, datetime) and rel is not None:
            raise TypeError(
                "pytest.approx() does not support relative tolerance for datetime comparisons. "
                "Use abs=timedelta(...) instead."
            )
        if nan_ok:
            raise TypeError(
                "pytest.approx() does not support nan_ok for datetime/timedelta comparisons."
            )
        if abs is None and rel is None:
            raise TypeError(
                "pytest.approx() requires an explicit tolerance for datetime/timedelta "
                "comparisons: e.g. approx(expected, abs=timedelta(seconds=1)) or "
                "approx(expected, rel=0.01)"
            )
        if abs is not None and not isinstance(abs, timedelta):
            raise TypeError(
                "absolute tolerance for datetime/timedelta must be a timedelta, "
                f"got {type(abs).__name__}"
            )
        if abs is not None and abs < timedelta(0):
            raise ValueError(f"absolute tolerance can't be negative: {abs}")
        if rel is not None:
            if not isinstance(rel, int | float):
                raise TypeError(
                    f"relative tolerance for timedelta must be a number, got {type(rel).__name__}"
                )
            if rel < 0:
                raise ValueError(f"relative tolerance can't be negative: {rel}")
            if math.isnan(rel):
                raise ValueError("relative tolerance can't be NaN.")
        relative = rel * builtins.abs(expected) if rel is not None else None
        tolerance = abs if relative is None else relative if abs is None else max(abs, relative)
        super().__init__(expected, rel=rel, abs=tolerance, nan_ok=False)

    @override
    def __repr__(self) -> str:
        return f"{self.expected} ± {self.abs}"

    @override
    def __eq__(self, actual: object) -> bool:
        try:
            return bool(builtins.abs(self.expected - actual) <= self.abs)
        except (TypeError, OverflowError):
            return False

    @override
    def _yield_comparisons(self, actual: Any) -> Iterator[tuple[Any, Any]]:
        yield actual, self.expected

    @override
    def _repr_compare(self, other_side: Any) -> list[str]:
        try:
            difference: object = builtins.abs(self.expected - other_side)
        except (TypeError, OverflowError):
            difference = "N/A"
        return [
            "comparison failed",
            f"Obtained: {other_side}",
            f"Expected: {self.expected} ± {self.abs}",
            f"Absolute difference: {difference}",
            f"Tolerance: {self.abs}",
        ]


def approx(
    expected: Any,  # noqa: ANN401 - any supported expectation
    rel: Any = None,  # noqa: ANN401 - float, Decimal, or timedelta
    abs: Any = None,  # noqa: A002, ANN401 - pytest public keyword
    nan_ok: bool = False,
) -> ApproxBase:
    """Assert that two numbers, or collections of numbers, are equal within a tolerance."""
    __tracebackhide__ = True
    cls: type[ApproxBase]
    if isinstance(expected, Decimal):
        cls = ApproxDecimal
    elif isinstance(expected, Mapping):
        cls = ApproxMapping
    elif (array := _as_numpy_array(expected)) is not None:
        expected = array
        cls = ApproxNumpy
    elif _is_sequence_like(expected):
        cls = ApproxSequenceLike
    elif isinstance(expected, Collection) and not isinstance(expected, str | bytes):
        raise TypeError(f"pytest.approx() only supports ordered sequences, but got: {expected!r}")
    elif isinstance(expected, datetime | timedelta):
        cls = ApproxTimedelta
    else:
        cls = ApproxScalar
    return cls(expected, rel, abs, nan_ok)


def _is_sequence_like(expected: object) -> bool:
    return (
        hasattr(expected, "__getitem__")
        and isinstance(expected, Sized)
        and not isinstance(expected, str | bytes)
    )


def _as_numpy_array(obj: object) -> Any:  # noqa: ANN401 - numpy ndarray or None
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        if numpy.isscalar(obj):
            return None
        if isinstance(obj, numpy.ndarray):
            return obj
        if hasattr(obj, "__array__") or hasattr(obj, "__array_interface__"):
            return numpy.asarray(obj)
    return None
