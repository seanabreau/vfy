"""Pytest version policy and class resolution for the native adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from _pytest.python import CallSpec2


@dataclass(frozen=True)
class PytestCompatibilityPolicy:
    """Upstream lifecycle differences within the verified pytest envelope."""

    fixturedefs_use_nodeid: bool
    legacy_fixture_order: bool
    group_fixture_finalizer_errors: bool
    unittest_wide_scope_instance: bool
    fixture_parameter_identity: bool
    fixture_scope_override_lookup: Literal["optional", "required"]
    cached_fixture_error_traceback: bool
    initial_fixture_scope_uses_parametrization: bool
    repeated_fixture_scope_check: bool
    warn_dynamic_fixture_teardown: bool
    legacy_fixture_scope_diagnostic: bool
    removed_in9_warning_is_error: bool
    unhandled_async_fixture: Literal["allow", "warn", "error"]
    returned_test_value: Literal["warn_value", "fail_value", "warn_type"]
    junit_case_count: Literal["original_case", "phase_group"]
    junit_message_style: Literal["full", "bounded"]
    stepwise_cache_format: Literal["nodeid", "metadata"]


def pytest_compatibility_policy(version: str) -> PytestCompatibilityPolicy:
    """Resolve supported stable releases; reject versions beyond tested minors."""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    release = tuple(int(part) for part in match.groups()) if match else ()
    if release < (9, 1, 0):
        raise ValueError(f"vfy test supports stable pytest >=9.1; found pytest {version}")
    return PytestCompatibilityPolicy(
        fixturedefs_use_nodeid=release < (8, 1),
        legacy_fixture_order=release < (8, 2),
        group_fixture_finalizer_errors=release >= (8, 1),
        unittest_wide_scope_instance=release < (8, 2),
        fixture_parameter_identity=release < (8, 3),
        fixture_scope_override_lookup="optional" if release < (8, 1) else "required",
        cached_fixture_error_traceback=release >= (8, 3),
        initial_fixture_scope_uses_parametrization=release < (8, 4),
        repeated_fixture_scope_check=release >= (8, 2),
        warn_dynamic_fixture_teardown=release >= (9, 1),
        legacy_fixture_scope_diagnostic=release < (8, 2),
        removed_in9_warning_is_error=(9, 0) <= release < (9, 1),
        unhandled_async_fixture=(
            "error" if release >= (9, 1) else "warn" if release >= (8, 4) else "allow"
        ),
        returned_test_value=(
            "warn_value"
            if release < (8, 4)
            else "fail_value"
            if release == (8, 4, 0)
            else "warn_type"
        ),
        junit_case_count="phase_group" if release >= (9, 1) else "original_case",
        junit_message_style="bounded" if release >= (9, 2) else "full",
        stepwise_cache_format="metadata" if release >= (8, 4) else "nodeid",
    )


def pytest_callspec_type() -> type[CallSpec2]:
    """Resolve pytest's callspec class without its 9.2 deprecated alias warning."""
    from _pytest import python  # noqa: PLC0415 - only native collection needs pytest

    callspec = vars(python).get("CallSpec")
    return python.CallSpec2 if callspec is None else cast("type[CallSpec2]", callspec)
