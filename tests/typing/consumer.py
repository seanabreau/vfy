"""Type-check installed public contracts without executing collection."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_type

from _pytest.fixtures import FixtureFunctionDefinition

from vfy import Coverage, CoverageData, async_fixture, asyncio
from vfy.native import NativeTestInvocation, mutation_timeout
from vfy.native_types import MutationBaselineCoveragePolicy


@async_fixture(loop_scope="module", scope="module")
async def resource(value: int) -> str:
    return str(value)


@asyncio.fixture
async def simple_resource() -> int:
    return 1


if TYPE_CHECKING:
    assert_type(resource, FixtureFunctionDefinition)
    assert_type(simple_resource, FixtureFunctionDefinition)
    coverage = Coverage(config_file=False)
    assert_type(coverage, Coverage)
    assert_type(coverage.get_data(), CoverageData)
    assert_type(coverage.get_data().lines("example.py"), list[int] | None)
    assert_type(coverage.get_data().arcs("example.py"), list[tuple[int, int]] | None)
    assert_type(coverage.analysis2("example.py"), tuple[str, list[int], list[int], list[int], str])
    assert_type(coverage.report(), float)
    assert_type(coverage.report(include=("*.py",), omit=(), contexts=("test",)), float)
    assert_type(mutation_timeout(("test_app.py::test_value",), {}, 3.0, "fixed"), float)
    assert_type(
        NativeTestInvocation.mutation_cli_arguments(None, ("tests",)),
        tuple[str | None, list[str]],
    )
    assert_type(
        NativeTestInvocation.mutation_baseline_coverage_policy(("tests",)),
        MutationBaselineCoveragePolicy,
    )

    # Named ignores also fail when these deliberately invalid calls stop being rejected.
    Coverage(branch="yes")  # ty: ignore[invalid-argument-type] - invalid consumer input must remain rejected
    coverage.report(output_format=3)  # ty: ignore[invalid-argument-type] - invalid consumer input must remain rejected
    async_fixture(loop_scope="invalid")  # ty: ignore[invalid-argument-type] - invalid consumer input must remain rejected
    mutation_timeout((), {}, 3.0, "disabled")  # ty: ignore[invalid-argument-type] - invalid consumer input must remain rejected
