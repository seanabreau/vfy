"""Version-specific pytest adaptations resolved once per native session."""

from __future__ import annotations

import warnings
from typing import Never, cast

import pytest
from _pytest.config import Config
from _pytest.fixtures import FixtureDef, SubRequest

from . import PytestCompatibilityPolicy, pytest_compatibility_policy as _pytest_compatibility_policy
from .native_state import NativeRequest, UnsupportedExtensionError, _compatibility_policy_key


def raise_protocol_teardown_error(error: BaseException, _teardowns: list[object]) -> Never:
    """Retain an aborted Pluggy 1.5 wrapper stack in the exception traceback."""
    raise error


def compatibility_policy(config: Config) -> PytestCompatibilityPolicy:
    """Resolve upstream adaptations once for each native pytest session."""
    policy = config.stash.get(_compatibility_policy_key, None)
    if policy is None:
        try:
            policy = _pytest_compatibility_policy(pytest.__version__)
        except ValueError as exc:
            raise UnsupportedExtensionError(str(exc)) from exc
        config.stash[_compatibility_policy_key] = policy
    return policy


def fixture_scope_mismatch_message(
    request: NativeRequest,
    name: str,
    requesting_scope: str,
    fixture_scope: str,
    fixture_definition: object,
) -> str:
    """Render scope failures with the session's upstream fixture diagnostics."""
    prefix = (
        f"ScopeMismatch: You tried to access the {fixture_scope} scoped fixture {name} "
        f"with a {requesting_scope} scoped request object"
    )
    # NativeRequest supplies the request protocol used by these unbound pytest methods.
    upstream_request = cast(SubRequest, request)
    if compatibility_policy(request.config).legacy_fixture_scope_diagnostic:
        traceback = cast(list[str], vars(SubRequest)["_factorytraceback"](upstream_request))
        return f"{prefix}, involved factories:\n" + "\n".join(traceback)
    stack = "\n".join(
        SubRequest._format_fixturedef_line(upstream_request, fixture)
        for fixture in upstream_request._get_fixturestack()
    )
    requested = SubRequest._format_fixturedef_line(
        upstream_request, cast(FixtureDef[object], fixture_definition)
    )
    return f"{prefix}. Requesting fixture stack:\n{stack}\nRequested fixture:\n{requested}"


def apply_upstream_warning_defaults(config: Config) -> None:
    """Apply pytest's version-specific defaults before user warning filters."""
    if compatibility_policy(config).removed_in9_warning_is_error:
        category = cast(type[Warning], vars(pytest)["PytestRemovedIn9Warning"])
        warnings.filterwarnings("error", category=category)
