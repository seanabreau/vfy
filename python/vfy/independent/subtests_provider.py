"""Register subtest fixtures."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vfy.native import _pytest

from .facade import fixture, hookimpl
from .stash import StashKey

failed_subtests_key = StashKey[object]()

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentReport, NativeIndependentRequest

    from .protocol import Config, Parser


class SubtestsFixture(Protocol):
    """The context protocol supplied by the native subtest owner."""

    def test(self, msg: str | None = None, **kwargs: object) -> AbstractContextManager[None]: ...


type TestStatus = tuple[str, str, str | Mapping[str, bool]] | None


@fixture
def subtests(request: NativeIndependentRequest) -> SubtestsFixture:
    return _pytest.create_independent_subtests(request)


class SubtestsProvider:
    """Retain the declaration and forward its hooks."""

    subtests = staticmethod(subtests)

    def __init__(
        self,
        configure: Callable[[Config], None],
        report_status: Callable[[object, Config], TestStatus],
    ) -> None:
        self.pytest_configure = configure
        self.pytest_report_teststatus = report_status

    @hookimpl
    def pytest_addoption(self, parser: Parser) -> None:
        parser.addini(
            "verbosity_subtests",
            help=(
                "Specify verbosity level for subtests. Higher levels will generate output "
                "for passed subtests. Failed subtests are always reported."
            ),
            default="auto",
        )

    @hookimpl
    def pytest_report_to_serializable(
        self, report: NativeIndependentReport
    ) -> dict[str, object] | None:
        return pytest_report_to_serializable(report)

    @hookimpl
    def pytest_report_from_serializable(
        self, data: dict[str, object]
    ) -> _pytest.SubtestReport | None:
        return pytest_report_from_serializable(data)


def pytest_report_to_serializable(report: NativeIndependentReport) -> dict[str, object] | None:
    if isinstance(report, _pytest.SubtestReport):
        return report._to_json()
    return None


def pytest_report_from_serializable(data: dict[str, object]) -> _pytest.SubtestReport | None:
    if data.get("_report_type") == "SubTestReport":
        return _pytest.SubtestReport._from_json(data)
    return None


@dataclass
class Captured:
    out: str = ""
    err: str = ""


@dataclass
class CapturedLogs:
    handler: object
